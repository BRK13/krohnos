"""Ranking de palavras: mede no DataForSEO (semanal), guarda o JSON do dia e os fatos no armazém do BI.

    python3 -m hub.ranking medir [--site <id>] [--simular]
    python3 -m hub.ranking importar-openseo <arquivo.sqlite>

Saída: $HUB_DATA/ranking/<site>/<AAAA-MM-DD>.json e fatos (fonte ranking) em $HUB_DATA/bi/metricas.db:
metrica "medida" (valor 1) para toda palavra medida; "posicao" só se a palavra está no resultado.
"Medida" sem "posicao" quer dizer fora das N primeiras. Falha vira lacuna, nunca zero.
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys

from hub import data, dataforseo, sites
from hub.comum.datas import BRT

CAMINHO = "serp/google/organic/live/advanced"
CUSTO_10 = 0.002  # US$ por 10 resultados de profundidade, por palavra (estimativa)


def _pasta(site_id):
    return data() / "ranking" / site_id


def arquivo_config(site_id):
    """Sobreposição das palavras (gravada pelo MCP); mesmo layout de hub.mcp_grupos.pasta, sem criar pasta."""
    return data() / "mcp" / "ranking" / site_id / "config.json"


def config(site):
    """Sobreposição, se existe, vale no lugar do cadastro; None se não há acompanhamento."""
    arq = arquivo_config(site["id"])
    if arq.exists():
        return {"fixadas": [], **json.loads(arq.read_text(encoding="utf-8"))}
    return {"fixadas": [], **site["ranking"]} if site["ranking"] else None


def _gravar_fatos(fatos):
    """fatos: (dia, negocio, metrica, dim, valor). Contrato do armazém, escrito direto (hub.armazem não está na main)."""
    arq = data() / "bi/metricas.db"
    arq.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(arq)
    with c:
        c.execute("create table if not exists fato(dia text, negocio text, fonte text, metrica text, "
                  "dim text default '', valor real, primary key (dia, negocio, fonte, metrica, dim))")
        c.executemany("insert into fato values (?, ?, 'ranking', ?, ?, ?) on conflict(dia, negocio, fonte, metrica, dim) "
                      "do update set valor = excluded.valor", fatos)
    c.close()


def _salvar(site_id, dia, fonte, cfg, itens, pasta=None):
    """Grava o JSON do dia e, na pasta semanal, os fatos; devolve (dicionário, nº de fatos)."""
    doc = {"site": site_id, "dia": dia, "fonte": fonte, "local": cfg["local"], "dispositivo": cfg["dispositivo"],
           "profundidade": cfg["profundidade"], "itens": itens}
    destino = pasta or _pasta(site_id)
    destino.mkdir(parents=True, exist_ok=True)
    (destino / f"{dia}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    fatos = []
    if pasta:  # o serviço MCP só grava em $HUB_DATA/mcp, nunca no BI
        return doc, 0
    for i in itens:
        if "erro" in i:
            continue
        fatos.append((dia, site_id, "medida", i["palavra"], 1.0))
        if i["posicao"] is not None:
            fatos.append((dia, site_id, "posicao", i["palavra"], float(i["posicao"])))
    _gravar_fatos(fatos)
    return doc, len(fatos)


def _achar(site, items):
    hosts = {h.removeprefix("www.") for h in (site["dominio"], *site["hosts_alternativos"])}
    for it in items or []:
        if it.get("type") == "organic" and (it.get("domain") or "").removeprefix("www.") in hosts:
            return it.get("rank_group"), it.get("url")
    return None, None


def _consultar(site, palavra, post, r):
    t = post(CAMINHO, [{"keyword": palavra, "location_code": r["local"], "language_code": r["idioma"],
                        "device": r["dispositivo"], "depth": r["profundidade"]}])[0]
    if t.get("status_code") != 20000:
        raise RuntimeError(f"tarefa {t.get('status_code')}: {t.get('status_message')}")
    return _achar(site, ((t.get("result") or [{}])[0]).get("items"))


def medir(site, agora=None, post=None, pasta=None):
    cfg = config(site)
    post = post or dataforseo.post
    dia = (agora or dt.datetime.now(dt.timezone.utc)).astimezone(BRT).date().isoformat()
    itens = []
    for palavra in cfg["palavras"]:
        for tentativa in (1, 2):
            try:
                pos, url = _consultar(site, palavra, post, cfg)
                itens.append({"palavra": palavra, "posicao": pos, "url": url})
                break
            except Exception as e:
                if tentativa == 2:
                    itens.append({"palavra": palavra, "posicao": None, "url": None, "erro": str(e)[:120]})
    arq = (pasta or _pasta(site["id"])) / f"{dia}.json"
    if arq.exists():  # nova rodada no mesmo dia: palavra que falhou agora mantém a medida boa de antes
        antes = {i["palavra"]: i for i in json.loads(arq.read_text(encoding="utf-8"))["itens"] if "erro" not in i}
        itens = [antes.get(i["palavra"], i) if "erro" in i else i for i in itens]
    if all("erro" in i for i in itens):  # nada medido: não grava nada (lacuna), não apaga dado bom
        return {"site": site["id"], "dia": dia, "fonte": "dataforseo", **{k: cfg[k] for k in ("local", "dispositivo", "profundidade")}, "itens": itens}
    return _salvar(site["id"], dia, "dataforseo", cfg, itens, pasta)[0]


def importar_openseo(caminho_sqlite, sites_=None):
    """Histórico do rank tracker do OpenSEO -> JSON diário + fatos. Repetir não duplica. Devolve nº de fatos."""
    sites_ = sites.carregar() if sites_ is None else sites_
    dono = {h: s for s in sites_ for h in (s["dominio"], *s["hosts_alternativos"])}
    c = sqlite3.connect(f"file:{caminho_sqlite}?mode=ro", uri=True)
    linhas = c.execute("select g.domain, g.location_code, g.serp_depth, g.devices, n.keyword, n.device, n.position, n.url, "
                       "n.checked_at from rank_snapshots n join rank_check_runs r on r.id = n.run_id "
                       "join rank_tracking_configs g on g.id = r.config_id order by n.checked_at").fetchall()
    c.close()
    dias = {}  # (site, dia) -> {cfg, itens por palavra}; o último check do dia vale
    for dominio, local, prof, devices, palavra, device, pos, url, quando in linhas:
        s = dono.get(dominio.removeprefix("www."))
        if not s:
            continue
        dia = dt.datetime.strptime(quando, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone(BRT).date().isoformat()
        d = dias.setdefault((s["id"], dia), {"cfg": {"local": local, "profundidade": prof, "dispositivo": device}, "itens": {}})
        d["itens"][palavra] = {"palavra": palavra, "posicao": pos, "url": url}
    total = 0
    for (site_id, dia), d in dias.items():
        arq = _pasta(site_id) / f"{dia}.json"
        if arq.exists() and json.loads(arq.read_text(encoding="utf-8")).get("fonte") == "dataforseo":
            continue  # medição própria do hub vale mais que o histórico antigo
        total += _salvar(site_id, dia, "openseo", d["cfg"], list(d["itens"].values()))[1]
    return total


def ler(site_id, semanas=12):
    por_dia = {a.name: a for a in (arquivo_config(site_id).parent / "medicoes").glob("*.json")}
    por_dia |= {a.name: a for a in _pasta(site_id).glob("*.json")}  # mesmo dia: vale a pasta semanal
    arqs = [por_dia[n] for n in sorted(por_dia)]
    if not arqs:
        return {"ok": False, "reason": "sem_medicao",
                "comoResolver": f"a medição semanal roda domingo; python3 -m hub.ranking medir --site {site_id}"}
    docs = [json.loads(a.read_text(encoding="utf-8")) for a in arqs]
    docs = [d for d in docs if any("erro" not in i for i in d["itens"])]  # dia sem nenhuma medida não conta
    if not docs:
        return {"ok": False, "reason": "sem_medicao",
                "comoResolver": f"a medição semanal roda domingo; python3 -m hub.ranking medir --site {site_id}"}
    ultimo = docs[-1]
    corte = (dt.date.fromisoformat(ultimo["dia"]) - dt.timedelta(weeks=semanas)).isoformat()
    docs = [d for d in docs if d["dia"] >= corte]
    arq_cfg = arquivo_config(site_id)
    fixadas = json.loads(arq_cfg.read_text(encoding="utf-8")).get("fixadas", []) if arq_cfg.exists() else []
    palavras = []
    for it in ultimo["itens"]:
        if "erro" in it:
            continue
        hist = [{"dia": d["dia"], "posicao": i["posicao"]} for d in docs for i in d["itens"]
                if i["palavra"] == it["palavra"] and "erro" not in i]
        ant = hist[-2]["posicao"] if len(hist) > 1 else None
        atual = it["posicao"]
        palavras.append({"palavra": it["palavra"], "posicao": atual, "url": it["url"], "anterior": ant,
                         "variacao": ant - atual if ant is not None and atual is not None else None, "historico": hist,
                         "fixada": it["palavra"] in fixadas})
    palavras.sort(key=lambda p: not p["fixada"])
    return {"ok": True, "site": site_id, "medidoEm": ultimo["dia"], "fonte": ultimo["fonte"], "local": ultimo["local"],
            "dispositivo": ultimo["dispositivo"], "profundidade": ultimo["profundidade"], "palavras": palavras}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.ranking")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("medir")
    m.add_argument("--site")
    m.add_argument("--simular", action="store_true")
    sub.add_parser("importar-openseo").add_argument("arquivo")
    a = ap.parse_args(argv)
    if a.cmd == "importar-openseo":
        print(f"{importar_openseo(a.arquivo)} fatos gravados")
        return 0
    falhou, alvos = False, []
    for s in sites.ativos():
        if a.site not in (None, s["id"]):
            continue
        try:
            r = config(s)
        except (json.JSONDecodeError, KeyError, OSError) as e:  # um config quebrado não para a semanal dos outros
            print(f"{s['id']}: config do ranking ilegível ({type(e).__name__}); site pulado", file=sys.stderr)
            falhou = True
            continue
        if (r or {}).get("palavras"):
            alvos.append((s, r))
    if a.site and not alvos and not falhou:
        print(f"site {a.site!r} não existe, não está ativo ou não tem ranking", file=sys.stderr)
        return 1
    for s, r in alvos:
        if a.simular:
            custo = len(r["palavras"]) * CUSTO_10 * -(-r["profundidade"] // 10)
            print(f"{s['id']}: {len(r['palavras'])} palavras, custo estimado US$ {custo:.2f}: " + "; ".join(r["palavras"]))
            continue
        with dataforseo.origem("rotina:ranking", site=s["id"], essencial=True):  # o teto diário não barra a semanal
            doc = medir(s)
        ok = sum("erro" not in i for i in doc["itens"])
        no_resultado = sum(i["posicao"] is not None for i in doc["itens"])
        print(f"{s['id']}: {ok}/{len(doc['itens'])} palavras medidas, {no_resultado} no resultado")
        falhou |= ok == 0
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
