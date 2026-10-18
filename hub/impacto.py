"""Impacto das entregas da esteira: cliques e impressões do GSC 14 dias antes × 14 dias depois do despacho, e a nota
média por categoria e site que o abastecedor usa para priorizar (tarefas/esteira-impacto.md).

    python3 -m hub.impacto medir   # a tarefa: mede as entregas maduras, grava e comenta nas issues
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from hub import data, esteira, sites

ATRASO_GSC = 3  # o GSC publica com ~3 dias de atraso
Q_ISSUE = "query($id: String!) { issue(id: $id) { description state { type } } }"


def delta(serie, marco, dias=14):
    """serie: [(data_iso, cliques, impressoes)]; marco: data do despacho (AAAA-MM-DD)."""
    m = dt.date.fromisoformat(marco[:10])
    antes = [s for s in serie if m - dt.timedelta(days=dias) <= dt.date.fromisoformat(s[0][:10]) < m]
    depois = [s for s in serie if m <= dt.date.fromisoformat(s[0][:10]) < m + dt.timedelta(days=dias)]
    ca, cd = sum(s[1] for s in antes), sum(s[1] for s in depois)
    ia, idp = sum(s[2] for s in antes), sum(s[2] for s in depois)
    var = (cd - ca) / ca if antes and ca else None
    return {"cliques_antes": ca, "cliques_depois": cd, "impressoes_antes": ia, "impressoes_depois": idp,
            "variacao_cliques": var}


def notas(entregas):
    """{categoria: {site: média da variação de cliques}}, ignorando entregas sem dado."""
    soma = {}
    for e in entregas:
        if e.get("variacao_cliques") is None:
            continue
        s = soma.setdefault(e["categoria"], {}).setdefault(e["projeto"], [0.0, 0])
        s[0] += e["variacao_cliques"]
        s[1] += 1
    return {c: {p: round(v[0] / v[1], 3) for p, v in sites.items()} for c, sites in soma.items()}


def estreias(entregas):
    """{categoria: {site: média de cliques nos 14 dias depois}} das entregas sem clique antes (página nova, buraco),
    que não têm variação para entrar em notas()."""
    soma = {}
    for e in entregas:
        if e.get("cliques_antes") != 0 or "cliques_depois" not in e:
            continue
        s = soma.setdefault(e["categoria"], {}).setdefault(e["projeto"], [0, 0])
        s[0] += e["cliques_depois"]
        s[1] += 1
    return {c: {p: round(v[0] / v[1], 1) for p, v in sites_.items()} for c, sites_ in soma.items()}


def site_do_projeto(projeto, pasta=None):
    """Site ativo cujo nome ou marca é o nome do projeto no Linear; None sem site (ex.: Nimbus)."""
    alvo = (projeto or "").casefold()
    for s in sites.ativos(pasta):
        m = s.get("marca") or {}
        if alvo in {n.casefold() for n in [s["nome"], m.get("nome"), *(m.get("aliases") or [])] if n}:
            return s
    return None


def url_medir(descricao, dominio):
    """A página da linha "Medir depois:" da issue (ou da linha logo abaixo), com https:// e o domínio do site."""
    if "Medir depois:" not in (descricao or ""):
        return None
    linha = descricao.split("Medir depois:", 1)[1].lstrip("* \t\r\n").split("\n", 1)[0]
    fim = r"[^\s)\]>`'\",;]*"
    m = re.search(rf"https?://{fim}|{re.escape(dominio)}{fim}|(?<![\w.])/{fim}", linha)
    if not m:
        return None
    u = m.group().rstrip(".")
    return f"https://{dominio}{u}" if u.startswith("/") else u if u.startswith("http") else f"https://{u}"


def _arq(base):
    return Path(base or data()) / "claude" / "esteira-impacto.json"


def medir(hoje=None, base_dados=None, gql=None, gsc=None):
    """Junta as despachadas ao histórico, mede as concluídas há 14 + 3 dias e grava. Devolve as medidas agora.
    Aberta fica pendente; cancelada, sem site ou sem URL ganha variacao_cliques None (não tenta de novo).
    Erro no Linear ou no Google deixa pendente para a próxima rodada."""
    from hub import google, linear
    gql, gsc = gql or linear.gql, gsc or google.desempenho_gsc
    hoje = hoje or dt.date.today()
    try:
        entregas = json.loads(_arq(base_dados).read_text(encoding="utf-8")).get("entregas", [])
    except (OSError, ValueError):
        entregas = []
    ja = {e["issue"] for e in entregas}
    entregas += [{k: d[k] for k in ("issue", "projeto", "categoria", "em")}
                 for d in esteira.estado(base_dados).get("despachadas", []) if d["issue"] not in ja]
    medidas = []
    for e in entregas:
        em = dt.date.fromisoformat(e["em"][:10])
        if "variacao_cliques" in e or em + dt.timedelta(days=14 + ATRASO_GSC) > hoje:
            continue
        try:
            issue = gql(Q_ISSUE, id=e["issue"])["issue"]
            tipo = issue["state"]["type"]
            if tipo not in ("completed", "canceled"):
                continue
            site = site_do_projeto(e["projeto"])
            url = site and url_medir(issue.get("description"), site["dominio"])
            if tipo == "canceled" or not url:
                e["variacao_cliques"] = None
                continue
            if not site["ids"].get("gsc"):
                raise RuntimeError(f"{site['id']} sem ids.gsc")
            r = gsc(site["ids"]["gsc"], {
                "dimensions": ["date"], "rowLimit": 100,
                "filters": [{"dimension": "page", "expression": url}],
                "startDate": (em - dt.timedelta(days=14)).isoformat(), "endDate": (em + dt.timedelta(days=13)).isoformat()})
        except Exception as erro:  # noqa: BLE001 — uma entrega com erro não derruba as outras
            print(f"{e['issue']}: {erro}", file=sys.stderr)
            continue
        e.update(url=url, **delta([(x["keys"][0], x["clicks"], x["impressions"]) for x in r.get("rows") or []], e["em"]))
        medidas.append(e)
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps({"em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "notas": notas(entregas), "estreias": estreias(entregas), "entregas": entregas},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, arq)
    return medidas


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["medir"]:
        print(__doc__)
        return 2
    from hub import linear
    for m in medir():
        print(f"{m['issue']} {m['projeto']} {m['categoria']}: cliques {m['cliques_antes']} → {m['cliques_depois']}")
        if not (m["impressoes_antes"] or m["impressoes_depois"]):
            continue  # sem dado no Google: nada a comentar
        try:
            linear.falar(m["issue"], f"Impacto no Google (14 dias antes → depois): cliques {m['cliques_antes']} → "
                                     f"{m['cliques_depois']}, impressões {m['impressoes_antes']} → {m['impressoes_depois']}.")
        except Exception as erro:  # noqa: BLE001 — o arquivo já foi gravado; um comentário não derruba os outros
            print(f"{m['issue']}: comentário falhou: {erro}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
