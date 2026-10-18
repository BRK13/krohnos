"""Histórico longo do BI direto das APIs do Google: GSC por dia (16 meses) e GA4 por dia, por origem/mídia
e visitas vindas de assistentes de IA (14 meses). Grava no armazém (hub/armazem.py).
    python3 -m hub.bi_historico --site <id|todos> [--dias N | --auto]
Sem --dias: histórico inteiro (blocos de 90/30 dias, para depois de dois blocos vazios seguidos).
Com --dias N: só os últimos N dias, um bloco por consulta.
Com --auto (rotina diária): por site, histórico inteiro enquanto não houver $HUB_DATA/bi/historico/<site>.ok, senão 5 dias.
O marcador só nasce quando o site rodou sem erro e com acesso a todas as fontes configuradas."""
import argparse
import datetime as dt
import sys
import zoneinfo
from pathlib import Path

from hub import armazem, data, google, sites

BLOCO_GSC, BLOCO_GA4, BLOCO_ORIGEM = 90, 90, 30
MESES_GSC, MESES_GA4 = 16, 14
LIMITE_GA4 = 100000
METRICAS_GA4 = (("sessoes", "sessions"), ("sessoes_engajadas", "engagedSessions"), ("conversoes", "keyEvents"))
# mesma lista do collect.mjs (aiHosts)
ASSISTENTES = {"chatgpt.com": "chatgpt", "chat.openai.com": "chatgpt", "perplexity.ai": "perplexity",
               "gemini.google.com": "gemini", "claude.ai": "claude", "copilot.microsoft.com": "copilot"}
ERROS = (google.ErroCredencial, google._ErroGa4, OSError, ValueError)


def consultar(fonte, propriedade, corpo):
    """A única chamada ao Google (os testes trocam esta função). Devolve as linhas da resposta."""
    if fonte == "gsc":
        r = google._api(google._base(propriedade) + "/searchAnalytics/query", propriedade, corpo)
    else:
        r = google._ga(google._ANALYTICS + str(propriedade).removeprefix("properties/") + ":runReport", propriedade, corpo)
    return r.get("rows", [])


def _hoje():
    return dt.datetime.now(zoneinfo.ZoneInfo("America/Sao_Paulo")).date()


def blocos(fim, piso, tamanho):
    """(início, fim) do mais novo para o mais velho, contíguos, o último cortado no piso."""
    while fim >= piso:
        ini = max(fim - dt.timedelta(days=tamanho - 1), piso)
        yield ini.isoformat(), fim.isoformat()
        fim = ini - dt.timedelta(days=1)


def _linhas(busca, fim, piso, tamanho, dias):
    if dias:
        piso, tamanho = max(piso, fim - dt.timedelta(days=dias - 1)), dias
    vazios = 0
    for ini, f in blocos(fim, piso, tamanho):
        linhas = busca(ini, f)
        vazios = 0 if linhas else vazios + 1
        if vazios == 2:  # site novo: antes disso não há nada
            break
        yield from linhas


def _fato(dia, negocio, fonte, metrica, valor, dim=""):
    return {"dia": dia, "negocio": negocio, "fonte": fonte, "metrica": metrica, "dim": dim, "valor": valor}


def _gsc(site, hoje, dias):
    prop, n = site["ids"]["gsc"], site["id"]

    def busca(ini, fim):
        return consultar("gsc", prop, {"startDate": ini, "endDate": fim, "dimensions": ["date"], "rowLimit": 25000,
                                       "type": "web", "dataState": "all"})
    for r in _linhas(busca, hoje - dt.timedelta(days=3), google._menos_meses(hoje, MESES_GSC), BLOCO_GSC, dias):
        for m, k in (("cliques", "clicks"), ("impressoes", "impressions"), ("ctr", "ctr"), ("posicao", "position")):
            if m == "posicao" and not r["impressions"]:
                continue  # sem impressão a posição vem 0: lacuna, não posição 0
            yield _fato(r["keys"][0], n, "gsc", m, r[k])


def _ga4_relatorio(prop, dimensoes, metricas, ini, fim):
    """runReport paginado (a paginação é do google.ga4_paginado); `consultar` continua sendo o ponto que os testes trocam."""
    return google.ga4_paginado(prop, dimensoes, metricas, ini, fim, LIMITE_GA4,
                               buscar=lambda corpo: consultar("ga4", prop, corpo))


def _dia_ga4(v):
    return f"{v[:4]}-{v[4:6]}-{v[6:]}"


def _ga4(site, hoje, dias):
    prop, n = site["ids"]["ga4"], site["id"]
    fim, piso = hoje - dt.timedelta(days=1), google._menos_meses(hoje, MESES_GA4)
    mets = [k for _, k in METRICAS_GA4]
    for dims, bloco in ((["date"], BLOCO_GA4), (["date", "sessionSourceMedium"], BLOCO_ORIGEM)):
        busca = lambda ini, f, dims=dims: _ga4_relatorio(prop, dims, mets, ini, f)
        for r in _linhas(busca, fim, piso, bloco, dias):
            v = [x["value"] for x in r["dimensionValues"]]
            for i, (m, _) in enumerate(METRICAS_GA4):
                yield _fato(_dia_ga4(v[0]), n, "ga4", m, r["metricValues"][i]["value"], v[1] if len(v) > 1 else "")
    ia = {}
    busca = lambda ini, f: _ga4_relatorio(prop, ["date", "sessionSource"], ["sessions"], ini, f)
    for r in _linhas(busca, fim, piso, BLOCO_ORIGEM, dias):
        dia, origem = r["dimensionValues"][0]["value"], r["dimensionValues"][1]["value"].lower()
        nome = next((a for h, a in ASSISTENTES.items() if origem == h or origem.endswith("." + h)), None)
        ia.setdefault((dia, ""), 0)  # dia com dado no GA4 e sem IA: zero de verdade, não lacuna
        if nome:
            s = float(r["metricValues"][0]["value"])
            for dim in (nome, ""):
                ia[dia, dim] = ia.get((dia, dim), 0) + s
    for (dia, dim), s in ia.items():
        yield _fato(_dia_ga4(dia), n, "ia", "visitas", s, dim)


def historico_site(site, hoje, dias=None, base_dados=None):
    """Grava o histórico de um site. Devolve ({fonte: dias distintos gravados}, [erros], [fontes sem acesso]);
    sem acesso só avisa, não é erro."""
    contagem, erros, sem_acesso = {}, [], []
    for chave, gerar in (("gsc", _gsc), ("ga4", _ga4)):
        if not site["ids"].get(chave):
            continue
        try:
            fatos = list(gerar(site, hoje, dias))
        except google.SemAcesso as e:  # configuração da conta, não falha: rodar de novo não resolve
            print(f"{site['id']} {chave}: sem acesso (dar leitura a {e.email})", file=sys.stderr)
            sem_acesso.append(chave)
            continue
        except ERROS as e:
            erros.append(f"{site['id']} {chave}: {type(e).__name__}: {e}")
            continue
        armazem.gravar(fatos, base_dados)
        for f in fatos:
            contagem.setdefault(f["fonte"], set()).add(f["dia"])
    return {k: len(v) for k, v in contagem.items()}, erros, sem_acesso


def marcador(site_id, base_dados=None):
    return Path(base_dados or data()) / "bi" / "historico" / f"{site_id}.ok"


def main(argv=None, hoje=None, base_dados=None):
    ap = argparse.ArgumentParser(prog="hub.bi_historico")
    ap.add_argument("--site", required=True, help="id do site ou 'todos'")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--dias", type=int)
    modo.add_argument("--auto", action="store_true", help="completo até o site ter marcador, depois 5 dias")
    a = ap.parse_args(argv)
    if a.dias is not None and a.dias < 1:
        ap.error("--dias precisa ser 1 ou mais")
    todos = sites.carregar()
    escolhidos = todos if a.site == "todos" else [s for s in todos if s["id"] == a.site]
    if not escolhidos:
        ap.error(f"site desconhecido: {a.site}")
    hoje, falhou = hoje or _hoje(), False
    for s in escolhidos:
        if not (s["ids"].get("gsc") or s["ids"].get("ga4")):
            print(f"{s['id']}: sem Search Console e sem GA4, nada a buscar")
            continue
        ok = marcador(s["id"], base_dados)
        dias = (5 if ok.exists() else None) if a.auto else a.dias
        contagem, erros, sem_acesso = historico_site(s, hoje, dias, base_dados)
        if a.auto and dias is None and not erros and not sem_acesso:
            ok.parent.mkdir(parents=True, exist_ok=True)
            ok.touch()
        for e in erros:
            print(f"erro: {e}", file=sys.stderr)
        falhou |= bool(erros)
        partes = [f"{f} {contagem[f]} dias" for f in ("gsc", "ga4", "ia") if f in contagem]
        print(f"{s['id']}: " + (", ".join(partes) or "nenhum dia gravado") + (" (com falha, ver erros)" if erros else "")
              + (" (histórico completo)" if a.auto and dias is None else ""))
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
