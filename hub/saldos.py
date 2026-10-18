"""Saldo das APIs pagas: coletor de 30 min, alerta por mudança de estado e a trava "sem saldo" dos jobs.
Root, cron (deploy/nimbus.cron). Tudo em $HUB_DATA/saldos/.

    python3 -m hub.saldos coletar [--sem-avisar]    # lê os saldos, grava estado.json, avisa só na mudança de estado
    python3 -m hub.saldos mostrar                   # o estado gravado
    python3 -m hub.saldos sem-saldo <servico> [--job <nome>]
                                                    # código 3 + motivo se o serviço está sem saldo (o job para sem quebrar);
                                                    # --job registra o job para a retomada

Serviços: openrouter (GET /api/v1/credits: total_credits − total_usage; chave em OPENROUTER_API_KEY ou no .env do
clienteh), anthropic (reserva de API: o saldo estimado de conta.gasto_api) e openai (cota grátis do dia por faixa
de modelo, lida de openai-dados-publicos.json quando a coleta dele existir).

Estados: ok, baixo (OpenRouter < US$ 5), zerado, indisponivel (sem leitura; nunca alerta nem trava).
Só a mudança de estado manda WhatsApp (ao dono, que avisa quem paga). Gasto diário acima de 3× a média dos
7 dias anteriores manda 1 aviso por dia, com o maior consumidor.
Jobs que dependem de saldo chamam `sem_saldo(servico, job)`: com saldo zerado param e ficam em pendentes.json;
quando o saldo volta, a coleta enfileira a tarefa do hub deles (se houver) e registra a retomada em retomadas.jsonl.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from hub import conta, data
from hub.comum import env as comum_env
from hub.comum.segredos import carregar_segredo

BRT = conta.BRT
LIMITE_BAIXO_USD = 5.0
FORA_DA_CURVA = 3.0
DIAS_MEDIA = 7
MIN_MEDIA_USD = 0.5  # média menor que isso: qualquer gasto vira "3×"; só vale acima do piso
ENV_CLIENTEH = Path("/opt/clienteh/.env")
URL_CREDITOS = "https://openrouter.ai/api/v1/credits"
URL_ATIVIDADE = "https://openrouter.ai/api/v1/activity"
ROTULO = {"openrouter": "OpenRouter", "anthropic": "Reserva de API da Anthropic", "openai": "OpenAI (cota grátis)"}
# job (nome da tarefa do hub) a enfileirar quando o saldo do serviço volta; os jobs dos apps retomam sozinhos pelo guard
TAREFA_DO_JOB = {}


def pasta(base_dados=None):
    return Path(base_dados or data()) / "saldos"


def _json(p, padrao):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _dia(quando):
    return quando.astimezone(BRT).date().isoformat()


# ---- leituras (efeitos) ----

def chave_openrouter(env=None, arquivo=ENV_CLIENTEH):
    return carregar_segredo("OPENROUTER_API_KEY", env=env) or comum_env.segredo("OPENROUTER_API_KEY", (arquivo,), ambiente=env)


def _get(url, chave):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {chave}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ler_openrouter(http=_get, chave=None):
    """{"saldo": US$, "creditos", "usado", "consumidores": {modelo: US$ de ontem}} ou {"erro": ...} (nunca com a chave)."""
    chave = chave or chave_openrouter()
    if not chave:
        return {"erro": "sem OPENROUTER_API_KEY"}
    try:
        d = http(URL_CREDITOS, chave)["data"]
        total, usado = float(d["total_credits"]), float(d["total_usage"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        return {"erro": f"{type(e).__name__} {str(e)[:120]}"}
    out = {"saldo": round(total - usado, 4), "creditos": total, "usado": usado}
    try:  # melhor esforço: a rota de atividade pede chave de gestão
        linhas = http(URL_ATIVIDADE, chave)["data"]
        por = {}
        for r in linhas:
            por[r.get("model") or "?"] = por.get(r.get("model") or "?", 0.0) + float(r.get("usage") or 0)
        out["consumidores"] = dict(sorted(por.items(), key=lambda kv: -kv[1])[:5])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        pass
    return out


def ler_anthropic(base_dados=None, quando=None):
    g = conta.gasto_api(base_dados, quando)
    por = {}
    dia = _dia(quando or conta.agora())
    for r in conta.ler_uso(None, base_dados):
        if r.get("conta") == conta.API and _dia(conta.de_iso(r["inicio"])) == dia:
            por[r.get("nome") or "?"] = por.get(r.get("nome") or "?", 0.0) + float(r.get("custo_usd") or 0)
    return {"saldo": g["saldo"], "usado": g["total"], "hoje": g["hoje"],
            "consumidores": dict(sorted(por.items(), key=lambda kv: -kv[1])[:5])}


def ler_openai(base_dados=None):
    """Faixas de modelo com o uso grátis do dia ({faixa: {"usado": tokens, "cota": tokens}}), da coleta da."""
    arq = pasta(base_dados) / "openai-dados-publicos.json"
    d = _json(arq, None)
    if not isinstance(d, dict) or not isinstance(d.get("faixas"), dict):
        return {"erro": "coleta dos dados-publicos (KRN-898) ainda não grava"}
    return {"faixas": d["faixas"], "em": d.get("em")}


# ---- regras (puras) ----

def estado_openrouter(saldo):
    if saldo is None:
        return "indisponivel"
    return "zerado" if saldo <= 0 else "baixo" if saldo < LIMITE_BAIXO_USD else "ok"


def estado_openai(faixas):
    """zerado = toda faixa com cota esgotada; baixo = alguma esgotada; sem cota lida = indisponível."""
    cheias = [f for f in (faixas or {}).values() if isinstance(f, dict) and f.get("cota")]
    if not cheias:
        return "indisponivel"
    esgot = [f for f in cheias if (f.get("usado") or 0) >= f["cota"]]
    return "zerado" if len(esgot) == len(cheias) else "baixo" if esgot else "ok"


def estado_anthropic(saldo):
    if saldo is None:
        return "indisponivel"
    return "zerado" if saldo <= 0 else "baixo" if saldo < LIMITE_BAIXO_USD else "ok"


def fora_da_curva(hoje, anteriores):
    """True se o gasto de hoje passa de 3× a média dos dias anteriores (até 7, só os que existem) e do piso."""
    dias = [v for v in anteriores[-DIAS_MEDIA:] if v is not None]
    if len(dias) < 3:  # sem histórico não há curva
        return False
    media = sum(dias) / len(dias)
    return hoje > FORA_DA_CURVA * max(media, MIN_MEDIA_USD / FORA_DA_CURVA) and hoje >= MIN_MEDIA_USD


def texto_mudanca(servico, antes, depois, leitura):
    nome = ROTULO[servico]
    saldo = leitura.get("saldo")
    usd = f"US$ {saldo:.2f}" if saldo is not None else "sem leitura"
    if depois == "zerado":
        return (f"🔴 {nome} ZEROU ({usd}). Os jobs que dependem dele pararam e voltam sozinhos quando o saldo voltar. "
                + ("João precisa recarregar." if servico == "openrouter" else ""))
    if depois == "baixo":
        extra = " Avise o João (quem paga) para recarregar." if servico == "openrouter" else ""
        return f"🟠 {nome} abaixo de US$ {LIMITE_BAIXO_USD:.0f}: saldo {usd}.{extra}"
    if depois == "ok" and antes in ("zerado", "baixo"):
        return f"🟢 {nome} voltou: saldo {usd}. Os jobs parados retomam sozinhos."
    return None


def texto_curva(servico, hoje, anteriores, consumidores):
    dias = [v for v in anteriores[-DIAS_MEDIA:] if v is not None]
    media = sum(dias) / len(dias)
    c = next(iter(consumidores or {}), None)
    maior = f" Maior consumidor: {c} (US$ {consumidores[c]:.2f})." if c else " Maior consumidor não identificado."
    return f"⚠️ {ROTULO[servico]}: gasto de hoje US$ {hoje:.2f}, mais de 3× a média de {len(dias)} dias (US$ {media:.2f}).{maior}"


# ---- coleta ----

def coletar(base_dados=None, quando=None, avisar_fn=None, enfileirar_fn=None, leituras=None):
    """Lê, compara com o estado anterior, avisa só mudança, grava. Devolve o estado novo.
    leituras = {servico: dict} injeta as leituras (testes)."""
    quando = quando or conta.agora()
    avisar_fn = avisar_fn or (lambda t: conta.avisar(t, agora_=True))
    arq = pasta(base_dados) / "estado.json"
    antes = _json(arq, {})
    antes = antes if isinstance(antes, dict) else {}
    lei = leituras if leituras is not None else {"openrouter": ler_openrouter(), "anthropic": ler_anthropic(base_dados, quando),
                                                 "openai": ler_openai(base_dados)}
    novo = {"em": conta.iso(quando), "servicos": {}, "avisos_curva": (antes.get("avisos_curva") or {})}
    hist_arq = pasta(base_dados) / "historico.json"
    hist = _json(hist_arq, {})
    hist = hist if isinstance(hist, dict) else {}
    dia = _dia(quando)
    for s, l in lei.items():
        est = (estado_openai(l.get("faixas")) if s == "openai" else estado_openrouter(l.get("saldo")) if s == "openrouter"
               else estado_anthropic(l.get("saldo"))) if "erro" not in l else "indisponivel"
        ant = (antes.get("servicos") or {}).get(s, {})
        # leitura que falhou mantém o estado conhecido: erro de rede não "zera" nem "recupera" nada
        if est == "indisponivel" and ant.get("estado") in ("ok", "baixo", "zerado"):
            est_efetivo, novo["servicos"][s] = ant["estado"], {**ant, "erro": l.get("erro"), "lido_em": ant.get("lido_em")}
        else:
            est_efetivo, novo["servicos"][s] = est, {**l, "estado": est, "lido_em": conta.iso(quando)}
        if "erro" in l:
            continue
        if s != "openai":
            h = hist.setdefault(s, {})
            usado = l.get("usado")
            if usado is not None:
                h[dia] = usado  # último total acumulado visto no dia: o gasto do dia é a diferença para o dia anterior
            for d in sorted(h)[:-(DIAS_MEDIA + 2)]:
                del h[d]
        if est_efetivo != ant.get("estado") and (ant.get("estado") or est_efetivo != "ok"):
            t = texto_mudanca(s, ant.get("estado"), est_efetivo, l)
            if t:
                avisar_fn(t)
            if est_efetivo == "ok" and ant.get("estado") in ("zerado", "baixo"):
                _retomar(s, base_dados, quando, enfileirar_fn)
        if s != "openai":
            dias = sorted(hist.get(s, {}))
            gasto = {d: hist[s][d] - hist[s][p] for p, d in zip(dias, dias[1:])}
            hoje_v = gasto.get(dia)
            ante = [gasto[d] for d in sorted(gasto) if d < dia]
            if hoje_v is not None and fora_da_curva(hoje_v, ante) and novo["avisos_curva"].get(s) != dia:
                avisar_fn(texto_curva(s, hoje_v, ante, l.get("consumidores")))
                novo["avisos_curva"][s] = dia
            novo["servicos"][s]["gasto_hoje"] = round(hoje_v, 4) if hoje_v is not None else None
    _gravar(hist_arq, hist)
    _gravar(arq, novo)
    return novo


def _retomar(servico, base_dados, quando, enfileirar_fn):
    """Saldo voltou: enfileira a tarefa do hub dos jobs pendentes e registra a retomada."""
    pend = _json(pasta(base_dados) / "pendentes.json", {})
    jobs = pend.pop(servico, []) if isinstance(pend, dict) else []
    retomados = []
    for j in jobs:
        nome = TAREFA_DO_JOB.get(j)
        if nome and enfileirar_fn:
            enfileirar_fn(nome, f"saldo de {servico} voltou")
        retomados.append(j)
    _gravar(pasta(base_dados) / "pendentes.json", pend if isinstance(pend, dict) else {})
    with open(pasta(base_dados) / "retomadas.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"em": conta.iso(quando), "servico": servico, "jobs": retomados}, ensure_ascii=False) + "\n")


# ---- consulta dos jobs ----

def sem_saldo(servico, job=None, base_dados=None):
    """Motivo ("sem saldo, retomo sozinho: ...") se o serviço está zerado, senão None. Estado desconhecido não trava.
    Com job, registra-o em pendentes.json para a retomada."""
    s = (_json(pasta(base_dados) / "estado.json", {}).get("servicos") or {}).get(servico) or {}
    if s.get("estado") != "zerado":
        return None
    if job:
        p = _json(pasta(base_dados) / "pendentes.json", {})
        p = p if isinstance(p, dict) else {}
        if job not in p.setdefault(servico, []):
            p[servico].append(job)
            _gravar(pasta(base_dados) / "pendentes.json", p)
    return f"sem saldo, retomo sozinho: {ROTULO.get(servico, servico)} zerado desde a última leitura"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.saldos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coletar")
    c.add_argument("--sem-avisar", action="store_true")
    sub.add_parser("mostrar")
    s = sub.add_parser("sem-saldo")
    s.add_argument("servico", choices=sorted(ROTULO))
    s.add_argument("--job")
    a = ap.parse_args(argv)
    if a.cmd == "coletar":
        enf = None
        try:
            from hub import agenda, tarefas
            todas = tarefas.carregar()
            enf = lambda nome, motivo: nome in todas and agenda.enfileirar(todas[nome], motivo)  # noqa: E731
        except Exception as e:  # noqa: BLE001  retomada de tarefa é bônus; a coleta não cai por ela
            print(f"aviso: sem enfileirar ({type(e).__name__})", file=sys.stderr)
        est = coletar(avisar_fn=(lambda t: print(t)) if a.sem_avisar else None, enfileirar_fn=enf)
        print(json.dumps({k: v.get("estado") for k, v in est["servicos"].items()}))
    elif a.cmd == "mostrar":
        print(json.dumps(_json(pasta() / "estado.json", {}), ensure_ascii=False, indent=2))
    else:
        m = sem_saldo(a.servico, a.job)
        if m:
            print(m)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
