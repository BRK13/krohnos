"""Cliente mínimo do DataForSEO (Basic auth). Nunca devolve nem registra a chave.

Toda chamada vai para $HUB_DATA/dataforseo/custos.jsonl (em, endpoint, origem, site, custo que a própria resposta
devolve, nº de tarefas). Teto diário: com o gasto do dia (BRT) em TETO_DIA_USD ou mais, POST pago é recusado
(TetoAtingido), menos o de origem essencial (ranking semanal). Pausa por gasto anormal barra todo POST,
inclusive essencial. GET (locations, task_get, user_data) não é barrado.
Quem chama a API fora deste cliente (o coletor de SERP do ClienteJ) importa este módulo e usa
conferir_teto() antes de cada lote e registrar() depois de cada chamada: um registro e um teto para todos.

    python3 -m hub.dataforseo saldo [--avisar]   # saldo em saldo.json e na série saldos.jsonl; queda do saldo maior que
                                                 # o registrado (+US$ 1) avisa na hora; abaixo de US$ 10 avisa (1x/dia)
    python3 -m hub.dataforseo resumo [--dias 7]  # gasto por dia e por origem
"""
import argparse
import contextlib
import contextvars
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from hub import config, data, home
from hub.comum.datas import BRT

URL = "https://api.dataforseo.com/v3/"
TETO_DIA_USD = 2.0
SALDO_BAIXO_USD = 10.0
FORA_DO_REGISTRO_USD = 1.0  # queda do saldo acima do registrado no mesmo período: chamador esquecido ou chave vazada

_origem = contextvars.ContextVar("dataforseo_origem", default=None)


class TetoAtingido(RuntimeError):
    pass


def chave():
    # VPS: DATAFORSEO_AUTH vem de dataforseo.env na pasta de segredos. PC: arquivo de credenciais do Nimbus.
    return os.environ.get("DATAFORSEO_AUTH", "").strip() or json.loads(
        (home() / "credenciais/dataforseo.json").read_text(encoding="utf-8"))["api_key"]


def pasta(base=None):
    """base: o $HUB_DATA de quem lê (o painel); sem ela, DATAFORSEO_PASTA (testes) ou $HUB_DATA/dataforseo."""
    if base is not None:
        return Path(base) / "dataforseo"
    return Path(os.environ["DATAFORSEO_PASTA"]) if os.environ.get("DATAFORSEO_PASTA") else data() / "dataforseo"


def teto():
    try:
        return float(os.environ.get("DATAFORSEO_TETO_DIA_USD") or TETO_DIA_USD)
    except ValueError:
        return TETO_DIA_USD


@contextlib.contextmanager
def origem(nome, site=None, essencial=False, cliente=None):
    """Quem está gastando (rotina:<nome>, mcp:<ferramenta>, script:<arquivo>); cliente: quem chamou o MCP.
    essencial=True passa do teto (só o ranking semanal)."""
    tok = _origem.set({"origem": nome, "site": site, "essencial": essencial, "cliente": cliente})
    try:
        yield
    finally:
        _origem.reset(tok)


def anotar_site(site):
    """O MCP só sabe o site depois de resolver os argumentos: completa a origem já aberta."""
    o = _origem.get()
    if o is not None:
        o["site"] = site


def na_thread(fn):
    """fn para ex.map: as threads do ThreadPoolExecutor não herdam a origem aberta (contextvar)."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.copy().run(fn, *a, **k)


def _contexto():
    o = _origem.get()
    if o:
        return o
    # sem origem aberta: a variável (as threads do ThreadPoolExecutor não herdam o contextvar) ou o script
    nome = os.environ.get("DATAFORSEO_ORIGEM") or "script:" + Path(sys.argv[0] if sys.argv and sys.argv[0] else "?").name
    return {"origem": nome, "site": None, "essencial": os.environ.get("DATAFORSEO_ESSENCIAL") == "1"}


def _ler_linhas_desde(arq, dia):
    """Registros do dia (BRT, AAAA-MM-DD) em diante, lendo o fim do arquivo para trás (o jsonl só cresce)."""
    try:
        f = open(arq, "rb")
    except OSError:
        return []
    with f:
        f.seek(0, os.SEEK_END)
        pos, resto, linhas = f.tell(), b"", []
        while pos > 0:
            n = min(65536, pos)
            pos -= n
            f.seek(pos)
            bloco = f.read(n) + resto
            partes = bloco.split(b"\n")
            resto = partes.pop(0) if pos > 0 else b""
            antigas = False
            for p in reversed(partes):
                if not p.strip():
                    continue
                try:
                    r = json.loads(p)
                except ValueError:
                    continue
                if str(r.get("em", ""))[:10] < dia:
                    antigas = True
                    break
                linhas.append(r)
            if antigas:
                break
    return linhas[::-1]


def registros(dias=1, agora=None, base=None):
    """Registros dos últimos `dias` dias (hoje incluso), em BRT."""
    hoje = (agora or dt.datetime.now(BRT)).astimezone(BRT).date()
    return _ler_linhas_desde(pasta(base) / "custos.jsonl", (hoje - dt.timedelta(days=dias - 1)).isoformat())


def gasto_hoje(agora=None):
    return round(sum(r.get("custo") or 0 for r in registros(1, agora)), 6)


def _registrar(caminho, metodo, ctx, custo, tarefas, status):
    reg = {"em": dt.datetime.now(BRT).isoformat(timespec="seconds"), "endpoint": caminho.split("?")[0], "metodo": metodo,
           "origem": ctx["origem"], "site": ctx.get("site"), "custo": custo, "tarefas": tarefas, "status": status}
    if ctx.get("cliente"):
        reg["cliente"] = ctx["cliente"]
    if ctx.get("essencial"):
        reg["essencial"] = True
    try:
        p = pasta()
        p.mkdir(parents=True, exist_ok=True)
        fd = os.open(p / "custos.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except OSError as e:  # o registro nunca derruba a chamada que já foi paga
        print(f"dataforseo: não gravou custos.jsonl ({type(e).__name__})", file=sys.stderr)


def registrar(endpoint, custo, origem, tarefas=1, status=20000, site=None, metodo="POST"):
    """Linha no custos.jsonl para quem chama o DataForSEO fora deste cliente (custo = o `cost` da resposta)."""
    _registrar(endpoint, metodo, {"origem": origem, "site": site}, round(float(custo or 0), 6), tarefas, status)


def conferir_teto(origem=None, essencial=False, avisar=None):
    """TetoAtingido se o gasto do dia (todos os chamadores) chegou ao teto; avisa o dono uma vez por dia."""
    from hub import gasto_anormal  # gasto anormal por hora pausa o serviço (inclusive o coletor de SERP do ClienteJ)
    pausa = gasto_anormal.pausado("dataforseo")
    if pausa:
        raise TetoAtingido(f"DataForSEO {pausa}; {origem or _contexto()['origem']} não chamou.")
    if essencial:
        return
    gasto, limite = gasto_hoje(), teto()
    if gasto < limite:
        return
    nome = origem or _contexto()["origem"]
    msg = (f"teto diário do DataForSEO atingido: US$ {gasto:.2f} gastos hoje (teto US$ {limite:.2f}, "
           f"DATAFORSEO_TETO_DIA_USD); {nome} não chamou. Volta amanhã ou o {config.dono()} sobe o teto.")
    _avisar_teto(msg, avisar)
    raise TetoAtingido(msg)


def _avisar_teto(msg, avisar=None):
    marca, hoje = pasta() / "teto-avisado.txt", dt.datetime.now(BRT).date().isoformat()
    try:
        if marca.exists() and marca.read_text(encoding="utf-8").strip() == hoje:
            return
        if avisar is None:
            from hub import conta
            avisar = conta.avisar
        avisar("DataForSEO: " + msg)
        marca.write_text(hoje, encoding="utf-8")
    except Exception as e:  # o aviso nunca troca o TetoAtingido por outro erro
        print(f"dataforseo: aviso do teto não saiu ({type(e).__name__})", file=sys.stderr)


def post(caminho, tarefas, timeout=120):
    """POST de uma lista de tarefas; devolve a lista `tasks`. RuntimeError (sem a chave) se a API recusar;
    TetoAtingido (subclasse) se o gasto do dia já chegou ao teto."""
    return _chamar(caminho, "POST", json.dumps(tarefas).encode(), timeout, len(tarefas))


def get(caminho, timeout=120):
    """GET (endpoints grátis, como serp/google/locations/{pais}); mesmo retorno e erros do post."""
    return _chamar(caminho, "GET", None, timeout, 0)


def resposta(caminho, timeout=60):
    """GET que devolve a resposta inteira (appendix/user_data), sem a chave."""
    return _chamar(caminho, "GET", None, timeout, 0, inteira=True)


def _chamar(caminho, metodo, corpo, timeout, n_tarefas, inteira=False):
    ctx = _contexto()
    if metodo == "POST":
        conferir_teto(ctx["origem"], ctx.get("essencial"))
    k = chave()
    req = urllib.request.Request(URL + caminho, corpo, method=metodo,
                                 headers={"Authorization": f"Basic {k}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        _registrar(caminho, metodo, ctx, 0, n_tarefas, f"http_{e.code}")
        raise RuntimeError(f"DataForSEO respondeu HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        _registrar(caminho, metodo, ctx, 0, n_tarefas, "sem_resposta")
        raise RuntimeError(f"DataForSEO sem resposta: {type(e).__name__}") from None
    tasks = resp.get("tasks") or []
    custo = resp.get("cost")
    if not isinstance(custo, (int, float)):
        custo = sum(t.get("cost") or 0 for t in tasks if isinstance(t, dict))
    _registrar(caminho, metodo, ctx, round(float(custo), 6), n_tarefas or len(tasks), resp.get("status_code"))
    if resp.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO {resp.get('status_code')}: {resp.get('status_message')}".replace(k, "***"))
    return resp if inteira else tasks


def resumo(dias=7, agora=None, base=None):
    """Gasto de hoje, dos `dias` dias e por origem/dia (para o painel e o aviso)."""
    agora = agora or dt.datetime.now(BRT)
    regs = registros(dias, agora, base)
    hoje = agora.astimezone(BRT).date().isoformat()
    por_origem, por_dia, dia_origem = defaultdict(float), defaultdict(float), defaultdict(lambda: defaultdict(float))
    for r in regs:
        c = r.get("custo") or 0
        por_origem[r.get("origem") or "?"] += c
        por_dia[str(r.get("em", ""))[:10]] += c
        dia_origem[str(r.get("em", ""))[:10]][r.get("origem") or "?"] += c
    total = sum(por_dia.values())
    return {"hoje": round(por_dia.get(hoje, 0), 4), "total": round(total, 4), "dias": dias,
            "media_dia": round(total / dias, 4), "chamadas": len(regs), "teto": teto(),
            "por_origem": sorted(((o, round(c, 4)) for o, c in por_origem.items()), key=lambda x: -x[1]),
            "por_dia": sorted((d, round(c, 4)) for d, c in por_dia.items()),
            "por_dia_origem": {d: sorted(((o, round(c, 4)) for o, c in os_.items() if c), key=lambda x: -x[1])
                               for d, os_ in dia_origem.items()}}


def dias_restantes(saldo, r):
    """Quantos dias o saldo dura na média; None sem base (menos de 3 dias com gasto: o registro começou em 12/10)."""
    if not isinstance(saldo, (int, float)) or len(r["por_dia"]) < 3 or r["media_dia"] <= 0:
        return None
    return saldo / r["media_dia"]


def ler_saldo(base=None):
    try:
        return json.loads((pasta(base) / "saldo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def saldos(dias=14, agora=None, base=None):
    """Série do saldo (uma leitura a cada 4 h): em, saldo, queda, registrado e fora do registro desde a leitura anterior."""
    hoje = (agora or dt.datetime.now(BRT)).astimezone(BRT).date()
    return _ler_linhas_desde(pasta(base) / "saldos.jsonl", (hoje - dt.timedelta(days=dias - 1)).isoformat())


def conferir(anterior, saldo, agora):
    """Queda do saldo desde a leitura anterior contra o que o custos.jsonl registrou no mesmo período.
    Recarga no meio (saldo subiu): o período não é conferido (fora = None), senão a recarga com consumo registrado
    viraria `fora` negativo e apagaria, na soma do dia, gasto sem registro de outro período."""
    agora = agora.astimezone(BRT)
    try:
        de = dt.datetime.fromisoformat(anterior["em"])
        queda = round(float(anterior["saldo"]) - saldo, 4)
    except (KeyError, TypeError, ValueError):
        return None
    regs = _ler_linhas_desde(pasta() / "custos.jsonl", de.astimezone(BRT).date().isoformat())
    registrado = 0.0
    for r in regs:
        try:
            if de < dt.datetime.fromisoformat(r["em"]) <= agora:
                registrado += r.get("custo") or 0
        except (KeyError, TypeError, ValueError):
            continue
    registrado = round(registrado, 4)
    if queda < 0:
        return {"de": anterior["em"], "queda": queda, "registrado": registrado, "fora": None, "recarga": True}
    return {"de": anterior["em"], "queda": queda, "registrado": registrado, "fora": round(max(queda - registrado, 0), 4)}


def texto_fora(c, saldo):
    return (f"DataForSEO: gasto FORA do registro. Desde {c['de'][11:16]} de {c['de'][8:10]}/{c['de'][5:7]} o saldo caiu "
            f"US$ {c['queda']:.2f} e o hub registrou US$ {c['registrado']:.2f}: US$ {c['fora']:.2f} sem dono "
            f"(chamador fora do hub ou chave vazada). Saldo agora US$ {saldo:.2f}. Confira em app.dataforseo.com "
            "(API usage); se não reconhecer, troque a senha da API.")


def _gravar_json(arq, obj):
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def texto_aviso(saldo, r):
    maior = r["por_origem"][0] if r["por_origem"] else None
    d = dias_restantes(saldo, r)
    dura = f", dá ~{d:.0f} dias nesse ritmo" if d is not None else " (registro com menos de 3 dias: sem previsão)"
    return (f"DataForSEO: saldo baixo, US$ {saldo:.2f} (abaixo de US$ {SALDO_BAIXO_USD:.0f}). "
            f"Média dos últimos {r['dias']} dias registrados no hub: US$ {r['media_dia']:.2f}/dia{dura}. "
            + (f"Maior consumidor: {maior[0]} (US$ {maior[1]:.2f} em {r['dias']} dias). " if maior else "")
            + "Recarga: app.dataforseo.com.")


def verificar_saldo(avisar=False, agora=None, consultar=None, enviar=None):
    """Lê o saldo (grátis), grava saldo.json e, abaixo de SALDO_BAIXO_USD, avisa no máximo uma vez por dia (BRT)."""
    agora = agora or dt.datetime.now(BRT)
    with origem("rotina:saldo"):
        resp = (consultar or (lambda: resposta("appendix/user_data")))()
    res = ((resp.get("tasks") or [{}])[0].get("result") or [{}])[0] or {}
    saldo = (res.get("money") or {}).get("balance")
    if not isinstance(saldo, (int, float)):
        raise RuntimeError("DataForSEO: appendix/user_data sem money.balance")
    p = pasta()
    p.mkdir(parents=True, exist_ok=True)
    anterior = ler_saldo() or {}
    hoje = agora.astimezone(BRT).date().isoformat()
    estado = {"saldo": round(saldo, 2), "em": agora.astimezone(BRT).isoformat(timespec="seconds"),
              "avisado_em": anterior.get("avisado_em")}
    conf = conferir(anterior, saldo, agora)
    with open(p / "saldos.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"em": estado["em"], "saldo": round(saldo, 4), **(conf or {})}, ensure_ascii=False) + "\n")
    if enviar is None:
        from hub import conta
        enviar = conta.avisar
    if avisar and conf and (conf["fora"] or 0) > FORA_DO_REGISTRO_USD:
        estado["fora_do_registro"] = conf
        enviar(texto_fora(conf, saldo), agora_=True)  # na hora: é o sinal de vazamento
    texto = None
    if saldo < SALDO_BAIXO_USD and avisar and anterior.get("avisado_em") != hoje:
        texto = texto_aviso(saldo, resumo(7, agora))
        enviar(texto)
        estado["avisado_em"] = hoje
    _gravar_json(p / "saldo.json", estado)
    return estado, texto


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.dataforseo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("saldo")
    s.add_argument("--avisar", action="store_true", help="abaixo de US$ 10 manda no WhatsApp (uma vez por dia)")
    sub.add_parser("resumo").add_argument("--dias", type=int, default=7)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "saldo":
        estado, texto = verificar_saldo(a.avisar)
        print(f"saldo US$ {estado['saldo']:.2f}" + (f"\naviso: {texto}" if texto else "")
              + (f"\nfora do registro: {estado['fora_do_registro']}" if estado.get("fora_do_registro") else ""))
        return 0
    r = resumo(a.dias)
    print(f"hoje US$ {r['hoje']:.2f} (teto {r['teto']:.2f}) · {a.dias} dias US$ {r['total']:.2f} em {r['chamadas']} chamadas")
    for o, c in r["por_origem"]:
        print(f"  {c:8.4f}  {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
