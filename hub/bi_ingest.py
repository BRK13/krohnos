"""Ingestão dos snapshots diários (gsc, ga4-dia, clarity) no armazém do BI.
    python3 -m hub.bi_ingest [--tudo | --dias N]      (padrão: as 5 pastas mais recentes)
GA4 por origem e visitas de IA não entram aqui: vêm por dia da API (hub/bi_historico)."""
import argparse
import json
import sys
from pathlib import Path

from hub import armazem, data, sites


def _fato(dia, negocio, fonte, metrica, valor):
    return {"dia": dia, "negocio": negocio, "fonte": fonte, "metrica": metrica, "valor": valor}


def _gsc(j, dia, n):
    for r in j["rows"]:
        d = r["keys"][0]
        for m, k in (("cliques", "clicks"), ("impressoes", "impressions"), ("ctr", "ctr"), ("posicao", "position")):
            if m == "posicao" and not r["impressions"]:
                continue  # sem impressão a posição vem 0: lacuna, não posição 0
            yield _fato(d, n, "gsc", m, r[k])


def _ga4_dia(j, dia, n):
    nomes = [h["name"] for h in j["metricHeaders"]]
    for r in j["rows"]:
        v = r["dimensionValues"][0]["value"]
        d = f"{v[:4]}-{v[4:6]}-{v[6:]}"
        for m, k in (("sessoes", "sessions"), ("conversoes", "keyEvents")):
            if k in nomes:
                yield _fato(d, n, "ga4", m, r["metricValues"][nomes.index(k)]["value"])


def _clarity(j, dia, n):
    por = {x["metricName"]: (x.get("information") or [{}])[0] for x in j}
    for m, nome, campo in (("cliques_mortos", "DeadClickCount", "subTotal"), ("cliques_raiva", "RageClickCount", "subTotal"),
                           ("rolagem_media", "ScrollDepth", "averageScrollDepth"), ("tempo_engajado", "EngagementTime", "activeTime")):
        if campo in por.get(nome, {}):
            yield _fato(dia, n, "clarity", m, por[nome][campo])


def _medicao(negocio):
    return next((s.get("medicao") or {} for s in sites.carregar() if s["id"] == negocio), {})


def _ga4_eventos(j, dia, n):
    """GA4 date × eventName: só os eventos declarados em sites/<id>.json (medicao.eventos); page_view e afins ficam fora."""
    eventos = set(_medicao(n).get("eventos") or [])
    nomes = [h["name"] for h in j["metricHeaders"]]
    for r in j["rows"]:
        v, evento = r["dimensionValues"][0]["value"], r["dimensionValues"][1]["value"]
        if evento not in eventos:
            continue
        d = f"{v[:4]}-{v[4:6]}-{v[6:]}"
        for m, k in (("ocorrencias", "eventCount"), ("usuarios", "totalUsers")):
            if k in nomes:
                yield {**_fato(d, n, "ga4_evento", m, r["metricValues"][nomes.index(k)]["value"]), "dim": evento}


def _clarity_recortes(j, dia, n):
    """Eventos e tags do Clarity, se a resposta trouxer (a doc da API não os lista: sem eles, nada é gravado). Só o que o site declarou."""
    med = _medicao(n)
    for x in j:
        nome_metrica = str(x.get("metricName", "")).lower()
        for fonte, chave, aceitos in (("clarity_evento", "event", med.get("eventos")), ("clarity_tag", "tag", med.get("tags"))):
            if chave not in nome_metrica:
                continue
            for linha in x.get("information") or []:
                nome = next((linha[k] for k in ("name", "eventName", "tagName", "tag") if isinstance(linha.get(k), str)), None)
                if nome in (aceitos or []) and "sessionsCount" in linha:
                    yield _fato(dia, n, fonte, "sessoes", linha["sessionsCount"]) | {"dim": nome}


def _clarity_tudo(j, dia, n):
    yield from _clarity(j, dia, n)
    yield from _clarity_recortes(j, dia, n)


LEITORES = {"gsc": _gsc, "ga4-dia": _ga4_dia, "ga4-eventos": _ga4_eventos, "clarity": _clarity_tudo}


def ingerir_snapshot(pasta, base_dados=None):
    """Grava os fatos de uma pasta de snapshot (nome = dia). Retorna quantos. JSON ruim ou fora do formato: aviso e segue."""
    pasta = Path(pasta)
    ids = [s["id"] for s in sites.carregar()]
    fatos = []
    for arq in sorted(pasta.glob("*.json")):
        for tipo in LEITORES:
            if arq.stem.endswith("-" + tipo):
                break
        else:
            continue  # status, ga4 (janela), latest etc.
        # prefixo mais longo vence (fsite06 antes de fsite05)
        negocio = max((i for i in ids if arq.stem == f"{i}-{tipo}"), key=len, default=None)
        if not negocio:
            continue
        try:
            lidos = [f for f in LEITORES[tipo](json.loads(arq.read_text(encoding="utf-8")), pasta.name, negocio) if f["valor"] is not None]
        except (ValueError, KeyError, TypeError, IndexError) as e:
            print(f"aviso: {arq} ignorado ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        for f in lidos:
            try:  # um valor não numérico derrubaria o gravar() da pasta inteira (e o --tudo)
                f["valor"] = float(f["valor"])
            except (TypeError, ValueError):
                print(f"aviso: {arq}: {f['metrica']} {f['dia']} ignorado (valor não numérico: {f['valor']!r})", file=sys.stderr)
                continue
            fatos.append(f)
    return armazem.gravar(fatos, base_dados)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.bi_ingest")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tudo", action="store_true")
    g.add_argument("--dias", type=int, default=5)
    a = ap.parse_args(argv)
    if a.dias < 1:
        ap.error("--dias precisa ser 1 ou mais")
    pastas = sorted(p for p in (data() / "snapshots").glob("*") if p.is_dir())
    if not a.tudo:
        pastas = pastas[-a.dias:]
    total = sum(ingerir_snapshot(p) for p in pastas)
    print(f"{total} fatos gravados de {len(pastas)} pastas de snapshot")


if __name__ == "__main__":
    main()
