"""Alarme de gasto anormal por HORA em toda API paga (estende a).

    python3 -m hub.gasto_anormal coletar [--sem-avisar]   # cron de 10 min: lê, compara a hora com a média, alarma, pausa
    python3 -m hub.gasto_anormal mostrar                  # leituras, gasto da hora, média e o que está pausado
    python3 -m hub.gasto_anormal liberar <servico>        # tira a pausa (depois de revisar o incidente)

Lê o acumulado de cada serviço (OpenRouter, reserva da Anthropic, DataForSEO pelo livro de custos, DeepSeek pelo
saldo, AWS SES pelas estatísticas de envio); guarda a série em $HUB_DATA/gasto/series.json (48 h). Gasto da última hora
> 3× a média das horas anteriores (e acima do piso de US$ 0,50), ou > US$ 5 numa hora mesmo sem histórico = incidente P1
(`cota_estourada`) + aviso técnico + PAUSA: o serviço entra em pausadas.json e `saldos.sem_saldo` e o teto do DataForSEO
passam a recusar (os jobs do hub param sozinhos). Pausa não revoga a chave: quem a revoga é o orquestrador (primeiro
revoga, depois investiga). Serviço sem leitor (Google Cloud, Cloudflare, Anthropic do Parceiro) aparece como `sem_leitor` com o
motivo: aqui não há credencial de leitura de custo; o teto é o orçamento da conta (ver "Pendências do dono").
"""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from hub import conta, data, home_agentes, incidentes

CURVA = 3.0
PISO_HORA_USD = 0.5
TETO_HORA_USD = 5.0
MIN_HORAS = 6
JANELA_H = 48
USD_POR_ENVIO_SES = 0.0001
# Serviço com gasto ZERO (regra do dono, 18/10): qualquer queda de 1 centavo na hora já é incidente P1.
GASTO_ZERO = {"deepseek"}
# Serviço de volume previsto em lote (SES: ~3.400 envios às 12:01 e 13:01 UTC, INC-284 falso positivo): só vale o teto absoluto
# por hora, sem a regra de 3× a média (dono, 21/10).
SO_TETO = {"aws-ses"}
CENTAVO = 0.005
# Rotina agendada de gasto conhecido (clientej:seo_serp, 225 SERPs, ~US$ 0,62, seg/qui 10:00 UTC, pausou o DataForSEO em
# INC-220 e INC-371): a rodada (origem + dia) abaixo do teto próprio não entra no acumulado, logo não vira média nem pausa; acima
# dele entra inteira (uma rodada 5× maior alarma como qualquer gasto).
ORIGENS_AGENDADAS = {"clientej:seo_serp"}
TETO_RODADA_USD = 2.0
SEM_LEITOR = {
    "google-cloud": "sem credencial de custo aqui; alerta de orçamento de US$ 5 e cota diária são do console (Pendências do dono)",
    "cloudflare": "sem token de leitura de uso/billing; alerta de gasto é do painel da Cloudflare (Pendências do dono)",
    "anthropic-parceiro": "chave de uso comum não lê custo (só chave admin); teto é o crédito Max do Parceiro",
}


def pasta(base=None):
    return Path(base or data()) / "gasto"


def _json(p, padrao):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---- regras puras ----

def gasto_da_hora(serie, agora):
    """US$ por hora: a diferença do acumulado até a leitura mais próxima de 1 h atrás (30 a 90 min), normalizada pelo tempo
    real entre as duas (com leituras a cada 10 min cai em 60 min exatos: gasto constante de US$ 6/h dá 6). None sem leitura."""
    if len(serie) < 2:
        return None
    candidatas = []
    for t, v in serie[:-1]:
        idade = (agora - conta.de_iso(t)).total_seconds() / 60
        if 30 <= idade <= 90:
            candidatas.append((abs(idade - 60), idade, v))
    if not candidatas:
        return None
    _d, idade, v0 = min(candidatas)
    return max(0.0, serie[-1][1] - v0) * 60 / idade


def horas_anteriores(serie, agora):
    """Gasto de cada hora fechada antes da última (a diferença entre leituras de hora em hora), mais antiga primeiro."""
    por_hora = {}
    for t, v in serie:
        d = conta.de_iso(t)
        por_hora[d.replace(minute=0, second=0, microsecond=0)] = v  # última leitura de cada hora
    chaves = sorted(por_hora)
    corte = agora.replace(minute=0, second=0, microsecond=0)
    return [max(0.0, por_hora[b] - por_hora[a]) for a, b in zip(chaves, chaves[1:])
            if b < corte and b - a == dt.timedelta(hours=1)]


def anormal(hora, anteriores, zero=False, so_teto=False):
    """(bool, motivo). Média das horas anteriores com piso; sem histórico só vale o teto absoluto por hora.
    zero=True (GASTO_ZERO): qualquer gasto de 1 centavo na hora já é anormal."""
    if hora is None:
        return False, ""
    if zero and hora >= CENTAVO:
        return True, f"US$ {hora:.2f} na hora num serviço de gasto zero"
    if hora >= TETO_HORA_USD:
        return True, f"US$ {hora:.2f} numa hora (teto de US$ {TETO_HORA_USD:.0f}/h)"
    if not so_teto and len(anteriores) >= MIN_HORAS:
        media = sum(anteriores) / len(anteriores)
        if hora >= PISO_HORA_USD and hora > CURVA * max(media, PISO_HORA_USD / CURVA):
            return True, f"US$ {hora:.2f} na hora, mais de {CURVA:.0f}× a média de {len(anteriores)} h (US$ {media:.2f}/h)"
    return False, ""


# ---- leitores (cumulativo em US$; None = sem leitura) ----

def ler_openrouter():
    from hub import saldos
    l = saldos.ler_openrouter()
    return None if "erro" in l else l.get("usado")


ALARME_TETO = 0.8


def alarme_teto_dia(leitura, base=None, agora=None):
    """chave do WhatsApp com ≥ 80% do teto diário (`limit`) = incidente para o orquestrador (nunca WhatsApp)."""
    agora = agora or conta.agora()
    k = (leitura or {}).get("chave") or {}
    limite, dia = k.get("limite"), k.get("dia")
    if not limite or dia is None or dia < ALARME_TETO * limite or k.get("reinicio") not in ("daily", None):
        return None
    return incidentes.registrar(
        f"Chave da OpenRouter (WhatsApp) em {100 * dia / limite:.0f}% do teto diário: US$ {dia:.2f} de US$ {limite:.2f}. "
        "Ao bater o teto a IA do WhatsApp para até o reinício; investigue o volume antes de subir o limite.", "cota_estourada",
        "hub/gasto_anormal.py (teto diário da chave do WhatsApp)", issue="KRN-1445", chave=f"teto-dia:openrouter:{agora.strftime('%Y%m%d')}",
        prioridade=2, base_dados=base, agora=agora)


def ler_anthropic(base=None):
    from hub import saldos
    return saldos.ler_anthropic(base)["usado"]


def _dataforseo_total(base=None):
    """Soma do livro de custos (todas as chamadas, de todos os chamadores), menos as rodadas agendadas conhecidas
    (ORIGENS_AGENDADAS, por origem e dia) que ficaram abaixo de TETO_RODADA_USD."""
    from hub import dataforseo
    p = dataforseo.pasta(base) / "custos.jsonl"
    total, rodadas = 0.0, {}
    try:
        for l in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l)
                c = float(r.get("custo") or 0)
            except (ValueError, AttributeError):
                continue
            if r.get("origem") in ORIGENS_AGENDADAS:
                k = (r["origem"], str(r.get("em", ""))[:10])
                rodadas[k] = rodadas.get(k, 0.0) + c
            else:
                total += c
    except OSError:
        return None
    total += sum(c for c in rodadas.values() if c >= TETO_RODADA_USD)
    return round(total, 6)


def ler_deepseek(estado, http=None):
    """DeepSeek só informa saldo: gasto = soma das quedas (recarga sobe o saldo e não conta). `estado` guarda o último saldo."""
    chave = _env_valor(_config("deepseek.env"), "DEEPSEEK_API_KEY")
    if not chave:
        return None
    try:
        if http:
            d = http("https://api.deepseek-com.example/user/balance", chave)
        else:
            req = urllib.request.Request("https://api.deepseek-com.example/user/balance", headers={"Authorization": f"Bearer {chave}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        saldo = sum(float(b["total_balance"]) for b in d.get("balance_infos", []) if b.get("currency") == "USD") or \
            sum(float(b["total_balance"]) for b in d.get("balance_infos", []))
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None
    ant = estado.get("saldo")
    acumulado = float(estado.get("acumulado") or 0) + (max(0.0, ant - saldo) if ant is not None else 0.0)
    estado.update({"saldo": saldo, "acumulado": acumulado})
    return acumulado


def _config_claude():
    return home_agentes() / ".config"  # a rotina roda como root (cron), mas as chaves moram com a conta claude


def _config(nome):
    """`~/.config/<nome>`; como root, a da conta claude quando a de root não existe (sem isso a DeepSeek e o SES ficavam
    "indisponível" e o vigia não via nenhuma queda de saldo)."""
    arq = Path.home() / ".config" / nome
    try:
        existe = arq.is_file()
    except OSError:
        existe = False
    if not existe and hasattr(os, "geteuid") and os.geteuid() == 0:
        return _config_claude() / nome
    return arq


def _env_valor(arq, nome):
    try:
        for l in Path(arq).read_text(encoding="utf-8").splitlines():
            m = re.match(rf"^\s*(?:export\s+)?{nome}\s*=\s*(.*?)\s*$", l)
            if m:
                return m.group(1).strip("'\"")
    except OSError:
        pass
    return None


def _sigv4_get(host, regiao, servico, query, ak, sk, agora):
    amz, dia = agora.strftime("%Y%m%dT%H%M%SZ"), agora.strftime("%Y%m%d")
    vazio = hashlib.sha256(b"").hexdigest()
    canon = f"GET\n/\n{query}\nhost:{host}\nx-amz-date:{amz}\n\nhost;x-amz-date\n{vazio}"
    escopo = f"{dia}/{regiao}/{servico}/aws4_request"
    a_assinar = f"AWS4-HMAC-SHA256\n{amz}\n{escopo}\n{hashlib.sha256(canon.encode()).hexdigest()}"
    k = ("AWS4" + sk).encode()
    for parte in (dia, regiao, servico, "aws4_request"):
        k = hmac.new(k, parte.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, a_assinar.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{escopo}, SignedHeaders=host;x-amz-date, Signature={sig}"
    req = urllib.request.Request(f"https://{host}/?{query}", headers={"x-amz-date": amz, "Authorization": auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def ler_aws_ses(agora=None, get=_sigv4_get, estado=None):
    """Acumulado de envios do SES × US$ 0,10/mil (o gasto do SES é só envio). O GetSendStatistics devolve uma janela móvel de 2
    semanas, então não serve como acumulado: soma só os pontos fechados (15 min) que ainda não foram vistos e guarda o total e o
    último instante em `estado`. A 1ª leitura só marca o ponto de partida (o histórico antigo não é gasto da hora). None sem leitura."""
    arq = _config("aws-ses.env")
    ak, sk = _env_valor(arq, "AWS_ACCESS_KEY_ID"), _env_valor(arq, "AWS_SECRET_ACCESS_KEY")
    regiao = _env_valor(arq, "AWS_REGION") or "us-east-1"
    if not (ak and sk):
        return None
    agora = agora or dt.datetime.now(dt.timezone.utc)
    try:
        xml = get(f"email.{regiao}.amazonaws.com", regiao, "ses", "Action=GetSendStatistics&Version=2010-12-01", ak, sk, agora)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    estado = estado if estado is not None else {}
    fechado = conta.iso(agora - dt.timedelta(minutes=15))
    pontos = []
    for m in re.findall(r"<member>(.*?)</member>", xml, re.S):
        ts, n = re.search(r"<Timestamp>([^<]+)</Timestamp>", m), re.search(r"<DeliveryAttempts>(\d+)</DeliveryAttempts>", m)
        if ts and n and conta.iso(conta.de_iso(ts.group(1))) <= fechado:
            pontos.append((conta.iso(conta.de_iso(ts.group(1))), int(n.group(1))))
    visto = estado.get("ses_visto_ate")
    if visto is None:
        estado["ses_visto_ate"] = max((t for t, _ in pontos), default=conta.iso(agora - dt.timedelta(minutes=15)))
        estado["ses_acumulado"] = 0
    else:
        novos = [(t, n) for t, n in pontos if t > visto]
        estado["ses_acumulado"] = estado.get("ses_acumulado", 0) + sum(n for _, n in novos)
        estado["ses_visto_ate"] = max([visto, *(t for t, _ in novos)])
    return estado["ses_acumulado"] * USD_POR_ENVIO_SES


# ---- pausa ----

def pausado(servico, base=None):
    """Motivo se o serviço está pausado por gasto anormal, senão None. Consultado por saldos.sem_saldo e pelo teto do DataForSEO."""
    p = _json(pasta(base) / "pausadas.json", {})
    r = p.get(servico) if isinstance(p, dict) else None
    return f"pausado por gasto anormal ({r.get('motivo')}); o orquestrador libera depois de revisar" if r else None


def liberar(servico, base=None, enfileirar_fn=None, agora=None):
    """Tira a pausa e enfileira os jobs que ficaram esperando (registrados por saldos.sem_saldo)."""
    p = _json(pasta(base) / "pausadas.json", {})
    existia = isinstance(p, dict) and p.pop(servico, None) is not None
    _gravar(pasta(base) / "pausadas.json", p if isinstance(p, dict) else {})
    if existia:
        from hub import saldos
        chave = {"anthropic-reserva": "anthropic"}.get(servico, servico)
        saldos._retomar(chave, base, agora or conta.agora(), enfileirar_fn, f"gasto liberado em {chave}")
    return existia


# ---- coleta ----

def coletar(base=None, agora=None, avisar_fn=None, leitores=None):
    """leitores = {servico: fn(estado_do_servico) -> acumulado US$ | None} (padrão: os reais). Devolve o resumo por serviço."""
    agora = agora or conta.agora()
    estado = _json(pasta(base) / "estado.json", {})
    series = _json(pasta(base) / "series.json", {})
    pausadas = _json(pasta(base) / "pausadas.json", {})
    pausadas = pausadas if isinstance(pausadas, dict) else {}
    padrao = leitores is None
    leitores = leitores if leitores is not None else {
        "openrouter": lambda e: ler_openrouter(), "anthropic-reserva": lambda e: ler_anthropic(base),
        "dataforseo": lambda e: _dataforseo_total(base), "deepseek": ler_deepseek, "aws-ses": lambda e: ler_aws_ses(agora, estado=e)}
    if padrao:
        from hub import saldos
        alarme_teto_dia(saldos.ler_openrouter(), base, agora)
    resumo = {s: {"estado": "sem_leitor", "motivo": m} for s, m in SEM_LEITOR.items()}
    for s, fn in leitores.items():
        est = estado.setdefault(s, {})
        v = fn(est)
        if v is None:
            resumo[s] = {"estado": "indisponivel"}
            continue
        serie = series.setdefault(s, [])
        serie.append([conta.iso(agora), v])
        corte = agora - dt.timedelta(hours=JANELA_H)
        series[s] = serie = [x for x in serie if conta.de_iso(x[0]) >= corte]
        hora = gasto_da_hora(serie, agora)
        ante = horas_anteriores(serie, agora)
        ruim, motivo = anormal(hora, ante, zero=s in GASTO_ZERO, so_teto=s in SO_TETO)
        resumo[s] = {"estado": "pausado" if s in pausadas else "ok", "hora": None if hora is None else round(hora, 4),
                     "media_h": round(sum(ante) / len(ante), 4) if ante else None, "horas_na_media": len(ante)}
        if ruim and s not in pausadas:
            chave = f"gasto:{s}:{agora.strftime('%Y%m%d%H')}"
            inc = incidentes.registrar(
                f"GASTO ANORMAL em {s}: {motivo}. Pausado no hub; revogue a chave primeiro, investigue depois.", "cota_estourada",
                "hub/gasto_anormal.py (alarme de gasto por hora)", issue="KRN-1137", chave=chave, prioridade=1, base_dados=base, agora=agora)
            pausadas[s] = {"em": conta.iso(agora), "motivo": motivo, "incidente": inc["id"]}
            resumo[s].update(estado="pausado", incidente=inc["id"])
            if avisar_fn:
                avisar_fn(f"🚨 Gasto anormal em {s}: {motivo}. {inc['id']} (P1). Jobs do hub pausados; revogar a chave primeiro.")
    _gravar(pasta(base) / "series.json", series)
    _gravar(pasta(base) / "estado.json", estado)
    _gravar(pasta(base) / "pausadas.json", pausadas)
    _gravar(pasta(base) / "ultimo.json", {"em": conta.iso(agora), "servicos": resumo})
    return resumo


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.gasto_anormal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coletar")
    c.add_argument("--sem-avisar", action="store_true")
    sub.add_parser("mostrar")
    l = sub.add_parser("liberar")
    l.add_argument("servico")
    a = ap.parse_args(argv)
    if a.cmd == "coletar":
        r = coletar(avisar_fn=None if a.sem_avisar else (lambda t: conta.avisar(t, agora_=True, tipo="tecnico", origem="gasto-anormal")))
        print(json.dumps({k: v["estado"] for k, v in r.items()}))
    elif a.cmd == "mostrar":
        print(json.dumps(_json(pasta() / "ultimo.json", {}), ensure_ascii=False, indent=2))
    else:
        enf = None
        try:
            from hub import agenda, tarefas
            todas = tarefas.carregar()
            enf = lambda nome, motivo: nome in todas and agenda.enfileirar(todas[nome], motivo)  # noqa: E731
        except Exception as e:  # noqa: BLE001  retomada é bônus; a liberação não cai por ela
            print(f"aviso: sem enfileirar ({type(e).__name__})", file=sys.stderr)
        print("liberado" if liberar(a.servico, enfileirar_fn=enf) else "não estava pausado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
