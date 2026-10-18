"""Confiabilidade do hub (F2): SLIs sobre o telemetria.db, avisos falsos com definição de caso fechada,
conciliação com o uso.jsonl, linha de base e o alarme P1.

    python3 -m hub.confiabilidade resumo [--dias 7] [--json]
    python3 -m hub.confiabilidade conciliar [--dias 7]          # sai 1 se uso.jsonl e banco não batem
    python3 -m hub.confiabilidade linha-de-base --dia 2026-10-17
    python3 -m hub.confiabilidade alarme                        # cron de hora em hora
    python3 -m hub.confiabilidade aviso-falso --motivo "..." [--issue]

Definição de caso de "aviso falso ao dono" (só estes): limite_sem_chamar, pausa_desmentida, pronto_desmentido, manual.
"""
import argparse
import contextlib
import datetime as dt
import json
import math
import re
import statistics
import sys
from html import escape as e

from hub import conta, incidentes, telemetria, ui
from hub.comum import porteiro

BRT = conta.BRT
RE_ISSUE = re.compile(r"KRN-\d+")
RE_RATE = re.compile(r"rate limit|403", re.I)
# limite que não é do Claude: a cota do GitHub (sem turnos não basta, uma recusa de cota do Claude também vem sem turnos)
RE_LIMITE_GITHUB = re.compile(r"API rate limit exceeded for (user|installation)|secondary rate limit|api\.github\.com", re.I)
ENVIO_AO_DONO = ("enviado", "resumo")
JANELA_PRONTO = dt.timedelta(hours=48)
USD = "US$ (equivalente API nas contas Claude)"


def _t(s):
    try:
        return conta.de_iso(s)
    except (AttributeError, TypeError, ValueError):
        return None


def _janela(dias, agora):
    agora = agora or conta.agora()
    return agora - dt.timedelta(days=dias), agora


def _taxa(ok, n):
    return round(ok / n, 3) if n else 0.0


def _lista_jsonl(nome, base_dados):
    try:
        texto = (conta.pasta(base_dados) / nome).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for l in texto.splitlines():
        with contextlib.suppress(ValueError):
            r = json.loads(l)
            if isinstance(r, dict):
                out.append(r)
    return out


# ---------- avisos falsos ----------

def marcar_aviso_falso(motivo, issue="", base_dados=None, agora=None):
    """Caso (c): o dono (ou quem confere) marca à mão um aviso que não era verdade."""
    if not str(motivo).strip():
        raise ValueError("falta o motivo")
    reg = {"em": conta.iso(agora or conta.agora()), "issue": str(issue).strip()[:20], "motivo": str(motivo).strip()[:300]}
    arq = conta.pasta(base_dados) / "avisos-falsos.jsonl"
    arq.parent.mkdir(parents=True, exist_ok=True)
    cortada = arq.exists() and arq.stat().st_size and not arq.read_bytes().endswith(b"\n")
    with open(arq, "a", encoding="utf-8") as f:
        f.write(("\n" if cortada else "") + json.dumps(reg, ensure_ascii=False) + "\n")
    return reg


def _avisos(ini, fim, base_dados):
    """Todos os avisos falsos com `em` em [ini, fim], por em."""
    itens = []
    usos = conta.ler_uso(base_dados=base_dados)
    # (a1) limite registrado sem o Claude ter sido chamado; um por conta por hora cheia
    vistos = set()
    for r in usos:
        ini_r = _t(r.get("inicio"))
        if not (r.get("limite") and r.get("turnos") is None and ini_r and RE_LIMITE_GITHUB.search(str(r.get("erro") or ""))):
            continue
        c = str(r.get("conta") or "")
        hora = conta.iso(ini_r)[:13]
        if (c, hora) in vistos:
            continue
        vistos.add((c, hora))
        itens.append({"em": conta.iso(ini_r), "caso": "limite_sem_chamar", "conta": c, "chave": f"limite_sem_chamar:{c}:{hora}",
                      "detalhe": f"conta {c} registrou limite sem chamar o Claude ({str(r.get('erro') or 'sem erro')[:120]})"})
    # (a2) pausa avisada e a conta rodou ok dentro dela
    for p in _lista_jsonl("pausas.jsonl", base_dados):
        desde, ate, c = _t(p.get("desde")), _t(p.get("ate")), str(p.get("conta") or "")
        if not (desde and ate):
            continue
        for r in usos:
            i = _t(r.get("inicio"))
            if (str(r.get("conta") or "") == c and r.get("ok") is True and (r.get("turnos") or 0) >= 1
                    and i and desde < i < ate):
                itens.append({"em": conta.iso(desde), "caso": "pausa_desmentida", "conta": c, "chave": f"pausa:{c}:{p['desde']}",
                              "detalhe": f"conta {c} pausada até {p['ate']} rodou ok em {r['inicio']} ({r.get('nome', '')})"})
                break
    # (b) pronto avisado e incidente pronto_sem_recibo da mesma issue em até 48 h
    sem_recibo = [(i, _t(i.get("em"))) for i in incidentes.listar(base_dados=base_dados) if i.get("classe") == "pronto_sem_recibo"]
    for env in porteiro._ler(base_dados)["envios"]:
        em = _t(env.get("em"))
        if env.get("tipo") not in ENVIO_AO_DONO or not env.get("issue") or not em:
            continue
        for inc, ie in sem_recibo:
            if inc.get("issue") == env["issue"] and ie and em <= ie <= em + JANELA_PRONTO:
                itens.append({"em": conta.iso(em), "caso": "pronto_desmentido", "issue": env["issue"],
                              "chave": f"pronto:{env['issue']}:{env['em']}",
                              "detalhe": f"{env['issue']} avisada como pronta e {inc['id']} (pronto_sem_recibo) abriu em {inc['em']}"})
                break
    # (c) marcação manual
    for m in _lista_jsonl("avisos-falsos.jsonl", base_dados):
        if _t(m.get("em")):
            itens.append({"em": m["em"], "caso": "manual", "issue": m.get("issue") or "", "chave": f"manual:{m['em']}",
                          "detalhe": str(m.get("motivo") or "")})
    itens = [i for i in itens if ini <= _t(i["em"]) <= fim]
    return sorted(itens, key=lambda i: _t(i["em"]))


def avisos_falsos(dias, base_dados=None, agora=None):
    ini, fim = _janela(dias, agora)
    return _avisos(ini, fim, base_dados)


# ---------- resumo ----------

def _execucoes(base_dados):
    con = telemetria.conectar(base_dados)
    con.row_factory = __import__("sqlite3").Row
    try:
        return [dict(r, _t=_t(r["inicio"])) for r in con.execute("SELECT * FROM execucao")]
    finally:
        con.close()


def _p90(valores):
    v = sorted(valores)
    return v[max(0, math.ceil(0.9 * len(v)) - 1)]


def _grupo(linhas):
    n = len(linhas)
    ok = sum(1 for r in linhas if r["resultado"] == "ok")
    return {"n": n, "ok": ok, "taxa": _taxa(ok, n)}


def resumo(dias, base_dados=None, agora=None):
    ini, fim = _janela(dias, agora)
    todas = _execucoes(base_dados)
    jan = [r for r in todas if r["_t"] and ini <= r["_t"] <= fim]
    por_issue = {}
    for r in todas:
        if RE_ISSUE.fullmatch(r["issue"] or "") and r["_t"]:
            por_issue.setdefault(r["issue"], []).append(r)
    por_tipo = {}
    for r in jan:
        por_tipo.setdefault(r["motivo"] or "outro", []).append(r)
    sucesso = {k: _grupo(v) for k, v in sorted(por_tipo.items())}
    sucesso["_todas"] = _grupo(jan)
    exe = {}
    for r in jan:
        exe.setdefault(r["executor"] or "claude", []).append(r)
    por_exec = {k: {**_grupo(v), "usd": round(sum(x["custo_usd"] or 0 for x in v), 4)} for k, v in sorted(exe.items())}
    # issue fechada = menor merge_em da issue dentro da janela; início = 1ª execução do banco inteiro
    horas, fechadas = [], []
    for issue, rs in por_issue.items():
        merges = [m for m in (_t(r["merge_em"]) for r in rs) if m]
        if merges and ini <= min(merges) <= fim:
            fechadas.append(issue)
            horas.append(max(0.0, (min(merges) - min(r["_t"] for r in rs)).total_seconds() / 3600))
    im = {"n": len(horas), "mediana": round(statistics.median(horas), 1) if horas else None,
          "p90": round(_p90(horas), 1) if horas else None}
    exec_fechadas = [r for i in fechadas for r in por_issue[i]]
    usd = sum(r["custo_usd"] for r in exec_fechadas if r["custo_usd"] is not None)
    custo = {"usd": round(usd / len(fechadas), 4) if fechadas else 0.0, "issues": len(fechadas),
             "sem_custo": sum(1 for r in exec_fechadas if r["custo_usd"] is None)}
    issues_jan = {r["issue"] for r in jan if RE_ISSUE.fullmatch(r["issue"] or "")}
    com_retr = {r["issue"] for r in jan if (r["retrabalho"] or 0) > 0 and r["issue"] in issues_jan}
    retr = {"execucoes": sum(r["retrabalho"] or 0 for r in jan), "issues": len(com_retr),
            "taxa": _taxa(len(com_retr), len(issues_jan))}
    desp = [r for r in jan if r["resultado"] in ("limite", "parada")]
    falsos = avisos_falsos(dias, base_dados, agora)
    incs = [i for i in incidentes.listar(base_dados=base_dados) if _t(i["em"]) and ini <= _t(i["em"]) <= fim]
    feitos = [i for i in incs if i.get("estado") == "feito"]
    return {
        "janela": {"de": conta.iso(ini), "ate": conta.iso(fim), "dias": dias, "execucoes": len(jan)},
        "sucesso_por_tipo": sucesso, "por_executor": por_exec, "issue_merge_horas": im,
        "custo_por_issue_fechada": custo, "retrabalho": retr,
        "desperdicio_infra": {"execucoes": len(desp), "usd": round(sum(r["custo_usd"] or 0 for r in desp), 4)},
        "avisos_falsos": {"n": len(falsos), "itens": falsos},
        "incidentes": {"abertos": sum(1 for i in incs if i.get("estado") == "pendente"), "fechados": len(feitos),
                       # sem teste e sem isenção (os anteriores à regra da); os isentos (descartado, sem mudança) à parte
                       "fechados_sem_teste": sum(1 for i in feitos if not incidentes.cita_teste(i.get("nota"))
                                                 and not incidentes.isento_de_teste(i.get("nota"))),
                       "fechados_isentos": sum(1 for i in feitos if incidentes.isento_de_teste(i.get("nota"))),
                       "p1_abertos": sum(1 for i in incs if i.get("estado") == "pendente" and i.get("prioridade") == 1)},
    }


# ---------- conciliação, linha de base, alarme ----------

def conciliar(dias, base_dados=None, agora=None):
    ini, fim = _janela(dias, agora)
    do_uso = {}
    for r in conta.ler_uso(base_dados=base_dados):
        i = _t(r.get("inicio"))
        if i and ini <= i <= fim:
            linha = telemetria.de_uso(r)
            do_uso[linha["id"]] = linha
    todas = _execucoes(base_dados)
    banco = [r for r in todas if r["_t"] and ini <= r["_t"] <= fim]
    ids_banco = {r["id"] for r in todas}
    usd_uso = round(sum(l["custo_usd"] or 0 for l in do_uso.values()), 4)
    usd_banco = round(sum(r["custo_usd"] or 0 for r in banco), 4)
    faltando = sum(1 for k in do_uso if k not in ids_banco)
    sobrando = sum(1 for r in banco if r["id"] not in do_uso)  # no banco e não no uso (o banco só nasce do uso)
    return {"uso": len(do_uso), "banco": len(banco), "faltando": faltando, "sobrando": sobrando,
            "usd_uso": usd_uso, "usd_banco": usd_banco,
            "ok": faltando == 0 and sobrando == 0 and len(do_uso) == len(banco) and abs(usd_uso - usd_banco) < 0.005}


def linha_de_base(dia, base_dados=None):
    ini = dt.datetime.strptime(dia, "%Y-%m-%d").replace(tzinfo=BRT)
    fim = ini + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    do_dia = lambda s: (t := _t(s)) is not None and ini <= t <= fim
    incs = [i for i in incidentes.listar(base_dados=base_dados) if do_dia(i["em"])]
    usos = [r for r in conta.ler_uso(base_dados=base_dados) if do_dia(r.get("inicio"))]
    rate = sum(1 for r in usos if r.get("turnos") is None and RE_RATE.search(str(r.get("erro") or "")))
    rate += sum(1 for i in incs if i.get("classe") == "cota_estourada")
    ok = sum(1 for r in usos if r.get("ok"))
    return {"dia": dia, "fila_parada": [i for i in incs if i.get("classe") == "tarefa_parada"],
            "pausa_falsa": [a for a in _avisos(ini, fim, base_dados) if a["caso"] in ("limite_sem_chamar", "pausa_desmentida")],
            "rate_limit_github": rate, "execucoes": len(usos), "ok": ok, "taxa": _taxa(ok, len(usos))}


def alarme(base_dados=None, agora=None):
    """Cada aviso falso novo dos últimos 7 dias vira incidente P1; chave já alarmada (mesmo tratada) não reabre."""
    arq = conta.pasta(base_dados) / "confiabilidade-alarmes.json"
    try:
        feitas = set(json.loads(arq.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        feitas = set()
    pendentes = {i.get("chave") for i in incidentes.listar(so_pendentes=True, base_dados=base_dados, agora=agora)}
    criados = []
    for it in avisos_falsos(7, base_dados, agora):
        if it["chave"] in feitas:
            continue
        if it["chave"] not in pendentes:
            criados.append(incidentes.registrar(
                f"Aviso falso ao dono ({it['caso']}): {it['detalhe']}", "aviso_falso_ao_dono",
                "hub/confiabilidade.py (definição de caso: " + it["caso"] + ")", issue=it.get("issue") or "",
                chave=it["chave"], prioridade=1, base_dados=base_dados, agora=agora))
        feitas.add(it["chave"])
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(sorted(feitas)), encoding="utf-8")
    return criados


# ---------- tela ----------

def _pct(x):
    return ui.num(x * 100, casas=1, pct=True)


def tela(base_dados=None, agora=None):
    r = resumo(7, base_dados, agora)
    if not r["janela"]["execucoes"] and not _execucoes(base_dados):
        return ui.card("Confiabilidade", ui.vazio("Sem execuções no banco de telemetria ainda.",
                                                  "Rode `python3 -m hub.telemetria importar` para trazer o uso.jsonl."))
    im, cu, rt, av = r["issue_merge_horas"], r["custo_por_issue_fechada"], r["retrabalho"], r["avisos_falsos"]
    sem = f"{cu['sem_custo']} execuções sem custo medido (Codex)" if cu["sem_custo"] else ""
    kp = ui.kpis(
        ui.kpi("Sucesso", _pct(r["sucesso_por_tipo"]["_todas"]["taxa"]), nota=e(f"{r['janela']['execucoes']} execuções em 7 dias")),
        ui.kpi("Issue→merge (mediana)", "—" if im["mediana"] is None else ui.num(im["mediana"], casas=1) + " h",
               nota=e(f"p90 {'—' if im['p90'] is None else ui.num(im['p90'], casas=1) + ' h'} · {im['n']} issues")),
        ui.kpi("Custo por issue fechada", ui.num(cu["usd"], moeda="US$"), nota=e(f"{USD} · {cu['issues']} issues" + (f" · {sem}" if sem else ""))),
        ui.kpi("Retrabalho", _pct(rt["taxa"]), nota=e(f"{rt['issues']} issues · {rt['execucoes']} voltas")),
        ui.kpi("Avisos falsos", str(av["n"]), tom="ruim" if av["n"] else "ok", quente=av["n"] > 0, nota=e("últimos 7 dias")))
    tipos = ui.tabela(["Tipo", "n", "ok", "Taxa"], [[k, ui.num(v["n"]), ui.num(v["ok"]), _pct(v["taxa"])]
                                                    for k, v in r["sucesso_por_tipo"].items()], num=(1, 2, 3))
    execs = ui.tabela(["Executor", "n", "ok", "Taxa", USD], [[k, ui.num(v["n"]), ui.num(v["ok"]), _pct(v["taxa"]),
                                                              ui.num(v["usd"], moeda="US$")]
                                                             for k, v in r["por_executor"].items()], num=(1, 2, 3, 4))
    falsos = (ui.tabela(["Quando", "Caso", "Detalhe"], [[ui.quando(i["em"]), ui.td(ui.selo(i["caso"], "ruim", mono=True), html_=True), i["detalhe"]]
                                                       for i in av["itens"]], celular="cartoes")
              if av["itens"] else ui.vazio("Nenhum aviso falso nos últimos 7 dias."))
    inc = r["incidentes"]
    incs = ui.tabela(["Abertos", "Fechados", "Fechados sem teste", "Isentos (descartado/sem mudança)", "P1 abertos"],
                     [[ui.num(inc["abertos"]), ui.num(inc["fechados"]), ui.num(inc["fechados_sem_teste"]),
                       ui.num(inc["fechados_isentos"]), ui.num(inc["p1_abertos"])]], num=(0, 1, 2, 3, 4))
    lb = linha_de_base("2026-10-17", base_dados)
    base = ui.tabela(["Fila parada", "Pausa falsa", "Rate limit do GitHub", "Execuções", "ok", "Taxa"],
                     [[ui.num(len(lb["fila_parada"])), ui.num(len(lb["pausa_falsa"])), ui.num(lb["rate_limit_github"]),
                       ui.num(lb["execucoes"]), ui.num(lb["ok"]), _pct(lb["taxa"])]], num=(0, 1, 2, 3, 4, 5))
    c = conciliar(7, base_dados, agora)
    conc = ui.tabela(["uso.jsonl", "Banco", "Faltando", "US$ uso", "US$ banco", "Confere"],
                     [[ui.num(c["uso"]), ui.num(c["banco"]), ui.num(c["faltando"]), ui.num(c["usd_uso"], casas=2),
                       ui.num(c["usd_banco"], casas=2), ui.selo("sim", "ok") if c["ok"] else ui.selo("não", "ruim")]],
                     num=(0, 1, 2, 3, 4))
    return (kp + ui.card("Sucesso por tipo", tipos, justa=True) + ui.card("Por executor", execs, justa=True)
            + ui.card("Avisos falsos (7 dias)", falsos, conta=av["n"], justa=True) + ui.card("Incidentes", incs, justa=True)
            + ui.card("Linha de base 17/10", base, justa=True) + ui.card("Conciliação", conc, justa=True))


# ---------- CLI ----------

def _texto(r):
    t, im, cu, rt = r["sucesso_por_tipo"]["_todas"], r["issue_merge_horas"], r["custo_por_issue_fechada"], r["retrabalho"]
    linhas = [f"Últimos {r['janela']['dias']} dias: {r['janela']['execucoes']} execuções, sucesso {t['taxa'] * 100:.1f}% ({t['ok']}/{t['n']})",
              f"Issue→merge: mediana {im['mediana']} h, p90 {im['p90']} h ({im['n']} issues)",
              f"Custo por issue fechada: {cu['usd']} {USD} ({cu['issues']} issues, {cu['sem_custo']} execuções sem custo)",
              f"Retrabalho: {rt['issues']} issues, {rt['execucoes']} voltas ({rt['taxa'] * 100:.1f}%)",
              f"Desperdício de infra: {r['desperdicio_infra']['execucoes']} execuções, US$ {r['desperdicio_infra']['usd']}",
              f"Avisos falsos: {r['avisos_falsos']['n']}"]
    linhas += [f"  - {i['em']} {i['caso']}: {i['detalhe']}" for i in r["avisos_falsos"]["itens"]]
    i = r["incidentes"]
    linhas.append(f"Incidentes: {i['abertos']} abertos ({i['p1_abertos']} P1), {i['fechados']} fechados, {i['fechados_sem_teste']} sem teste")
    return "\n".join(linhas)


def main(argv=None):
    p = argparse.ArgumentParser(prog="hub.confiabilidade")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("resumo")
    a.add_argument("--dias", type=int, default=7)
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("conciliar")
    a.add_argument("--dias", type=int, default=7)
    a = sub.add_parser("linha-de-base")
    a.add_argument("--dia", required=True)
    sub.add_parser("alarme")
    a = sub.add_parser("aviso-falso")
    a.add_argument("--motivo", required=True)
    a.add_argument("--issue", default="")
    args = p.parse_args(argv)
    if args.cmd == "resumo":
        r = resumo(args.dias)
        print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else _texto(r))
    elif args.cmd == "conciliar":
        c = conciliar(args.dias)
        print(json.dumps(c, ensure_ascii=False))
        return 0 if c["ok"] else 1
    elif args.cmd == "linha-de-base":
        print(json.dumps(linha_de_base(args.dia), ensure_ascii=False, indent=2))
    elif args.cmd == "alarme":
        novos = alarme()
        print(f"{len(novos)} incidentes P1 criados")
    else:
        print(json.dumps(marcar_aviso_falso(args.motivo, args.issue), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
