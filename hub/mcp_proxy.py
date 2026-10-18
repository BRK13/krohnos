"""MCP do hub: as ferramentas de SEO e analytics dos sites do dono, respondidas aqui mesmo (o OpenSEO foi desligado).

    python3 -m hub.mcp_proxy [--host 127.0.0.1] [--porta 4181]

MCP Streamable HTTP sem estado: `POST /mcp` com um objeto JSON-RPC. Search Console e GA4 pela conta de serviço do hub,
pesquisa paga pelo DataForSEO, o resto pelos arquivos do hub ($HUB_DATA). Nada é repassado. Nada de credencial em
resposta ou log.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hub import config, citacao, data, dataforseo, google, mcp_grupos, ranking, sites as S
from hub.mcp_grupos import ErroUso, projetos
from hub.mcp_grupos import alvo as _alvo, dfs as _dfs, dfs_resultado as _dfs_resultado, inteiro as _int, local as _local, palavras as _palavras, so as _so

VERSOES = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCOES = (f"Ferramentas de SEO e analytics do hub do {config.dono()}. Escolha o site por `site` (id do cadastro, domínio ou nome); "
              "list_projects lista os sites.")
MAX_TEXTO = 20000
CARO = " Custa crédito do DataForSEO."
SITE = {"site": {"type": "string", "description": "id do site no hub (ex.: psite13), domínio ou nome"}}
DATAS = {"dateRange": {"type": "string", "description": "last_7_days, last_28_days, last_3_months..."},
         "startDate": {"type": "string", "description": "AAAA-MM-DD"}, "endDate": {"type": "string", "description": "AAAA-MM-DD"}}
LOCAL = {"location_code": {"type": "integer", "description": "código de local do DataForSEO (padrão: Brasil)"},
         "language_code": {"type": "string", "description": "padrão: pt"}}
PALAVRAS = {"type": "array", "items": {"type": "string"}}


def _f(descricao, **props):
    return {"description": descricao, "inputSchema": {"type": "object", "properties": {**SITE, **props}}}


FERRAMENTAS = {
    "list_projects": {"description": "Lista os sites do hub, com as fontes de dados de cada um.", "inputSchema": {"type": "object", "properties": {}}},
    "get_project_context": _f("Cadastro do site: contexto, conselho, concorrentes, buscas monitoradas, palavras do ranking, ids e onde ficam os dados do hub."),
    "get_search_console_performance": _f("Desempenho no Google Search Console (conta de serviço do hub).", **DATAS,
                                         dimensions={"type": "array", "items": {"type": "string"}}, filters={"type": "array"},
                                         rowLimit={"type": "integer"}),
    "inspect_urls": _f("Inspeção de URLs no Search Console (indexação).", urls={"type": "array", "items": {"type": "string"}}),
    "get_search_opportunities": _f("Oportunidades de busca cruzando Search Console e GA4.", **DATAS, limit={"type": "integer"}),
    **{n: _f("GA4 pela conta de serviço do hub: " + n.removeprefix("get_google_analytics_").replace("_", " ") + ".", **DATAS,
             limit={"type": "integer"}) for n in google.FERRAMENTAS_GA4},
    "get_clarity_insights": _f("Métricas do Microsoft Clarity (coleta diária do hub, janela de 3 dias)."),
    "get_rank_tracker": _f("Posição das palavras do site no Google (medição semanal do hub), com histórico.",
                           semanas={"type": "integer", "description": "padrão 12"}),
    "get_site_audit": _f("Última auditoria técnica de SEO do site (verificador do hub): itens, pioras e relatório."),
    "get_hub_opportunities": _f("Oportunidades semanais do Search Console calculadas pelo hub (vazamento, reforcar, buraco).",
                                tipo={"type": "string", "enum": ["vazamento", "reforcar", "buraco"]},
                                limite={"type": "integer", "description": "padrão 50"}),
    "get_ai_overview": _f("AI Overview do Google nas buscas monitoradas: se aparece, se o site é citado, fontes, e a análise do ciclo."),
    "get_bi_metrics": _f("Métricas diárias do armazém do BI. Sem metrica: lista o que existe. Com fonte e metrica: série por dia.",
                         fonte={"type": "string"}, metrica={"type": "string"}, de={"type": "string", "description": "AAAA-MM-DD"},
                         ate={"type": "string", "description": "AAAA-MM-DD"}, dim={"type": "string"}),
    "get_keyword_metrics": _f("Volume, CPC, concorrência, dificuldade e intenção de palavras (até 100; até 1000 com fonte google_ads)." + CARO,
                              keywords=PALAVRAS, **LOCAL,
                              fonte={"type": "string", "enum": ["labs", "google_ads"], "description": "labs (padrão: volume, dificuldade e intenção) ou "
                                     "google_ads (volume e CPC direto do Google Ads, também para palavra que o Labs não conhece; sem dificuldade nem intenção)"}),
    "get_serp_results": _f("Resultados orgânicos do Google para uma palavra." + CARO, keyword={"type": "string"}, **LOCAL,
                           device={"type": "string", "enum": ["desktop", "mobile"]}, depth={"type": "integer", "description": "até 100, padrão 10"},
                           aiOverview={"type": "boolean", "description": "carrega o AI Overview e devolve as fontes que ele cita (padrão false)"}),
    "get_domain_overview": _f("Tráfego orgânico estimado e palavras por faixa de posição de um domínio (informe target; sem ele, o domínio do site)." + CARO,
                              target={"type": "string", "description": "domínio a analisar (ex.: zenklub-com-br.example); sem ele vale domain/dominio ou o domínio do site"}, **LOCAL),
    "get_ranked_keywords": _f("Palavras em que um domínio aparece no Google (informe target; sem ele, o domínio do site)." + CARO,
                              target={"type": "string", "description": "domínio a analisar (ex.: zenklub-com-br.example); sem ele vale domain/dominio ou o domínio do site"}, limit={"type": "integer", "description": "até 200, padrão 100"}, **LOCAL,
                              filters={"type": "array", "description": 'filtro do DataForSEO Labs, ex.: [["ranked_serp_element.serp_item.rank_group", "<=", 20]]'},
                              orderBy={"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": 'ordem, ex.: ["keyword_data.keyword_info.search_volume,desc"]'}),
    "find_serp_competitors": _f("Domínios que disputam as mesmas palavras (até 20; site/projectId só escolhem o site, o domínio analisado vem de target)." + CARO, keywords=PALAVRAS, **LOCAL),
}
GOOGLE = {"get_search_console_performance", "inspect_urls", "get_search_opportunities", *google.FERRAMENTAS_GA4}
SUBSTITUTAS = {}
# Grupos em hub/mcp_grupos/ (um módulo por grupo, achados sozinhos). Nome repetido com o núcleo é erro de programação.
GRUPOS = mcp_grupos.carregar()
if set(GRUPOS) & set(FERRAMENTAS):
    raise RuntimeError(f"ferramentas duplicadas entre o núcleo e hub/mcp_grupos: {sorted(set(GRUPOS) & set(FERRAMENTAS))}")
FERRAMENTAS.update({n: m.FERRAMENTAS[n] for n, m in GRUPOS.items()})


def _log(msg):
    print("mcp_proxy: " + msg, file=sys.stderr, flush=True)


def _email():
    try:
        return google.conta_servico()["client_email"]
    except Exception:  # arquivo ausente/ilegível: a resposta ainda serve, só sem o e-mail
        return None


def _sem_acesso(site_id, email=None):
    email = email or _email()
    quem = f"a conta de serviço {email}" if email else "a conta de serviço do hub"
    return {"ok": False, "reason": "hub_sem_acesso", "comoResolver": f"Dar leitura a {quem} no Search Console / GA4 da propriedade.",
            "dadosDoHub": str(data() / "gsc" / site_id / "latest.md")}


def _norm(v):
    return citacao.normalizar(str(v)).strip()


def _site(args, chaves=("site", "projectId", "domain", "dominio")):
    """(site, None) ou (None, resposta site_desconhecido). Sem argumento de site: (None, None)."""
    valor = next((args[k] for k in chaves if args.get(k)), None)
    if valor is None:
        return None, None
    v = _norm(valor)
    alvo = {v} if "://" not in v and "/" in v else {v, citacao.host(v)} - {""}  # "asite01/../fsite05" não vira host
    todos = S.carregar()
    for s in todos:
        nomes = [s["id"], *s["ids_antigos"], s["dominio"], *s["hosts_alternativos"], s["nome"], s["marca"]["nome"], *s["marca"]["aliases"]]
        if alvo & {_norm(n) for n in nomes}:
            return s, None
    ids = [s["id"] for s in todos]
    como = "use site com um destes ids: " + ", ".join(ids)
    if S.RE_UUID.match(_norm(valor)):
        como += "; ids de projeto do OpenSEO não valem mais"
    return None, {"ok": False, "reason": "site_desconhecido", "comoResolver": como, "sitesDoHub": ids}


def _google(nome, args, site):
    gsc, ga4 = site["ids"].get("gsc"), site["ids"].get("ga4")
    ga = nome in google.FERRAMENTAS_GA4 or nome == "get_search_opportunities"
    if (not gsc and nome not in google.FERRAMENTAS_GA4) or (ga and not ga4):
        return _sem_acesso(site["id"])
    try:
        if nome == "get_search_console_performance":
            return google.desempenho_gsc(gsc, args)
        if nome == "inspect_urls":
            return google.inspecionar_urls(gsc, args)
        if nome == "get_search_opportunities":
            return google.oportunidades_busca(gsc, ga4, args)
        return google.relatorio_ga4(ga4, args, nome)
    except google.SemAcesso as e:
        return _sem_acesso(site["id"], e.email)
    except (urllib.error.URLError, TimeoutError):  # HTTPError é URLError; GA4 segue para o isError
        if nome in google.FERRAMENTAS_GA4:
            raise
        return {"ok": False, "reason": "api_error", "message": "Falha ao consultar a API do Google Search Console."}


def _ler(arq, limite=None):
    return arq.read_text(encoding="utf-8")[:limite] if arq.is_file() else None



def _projetos():
    return {"ok": True, "projects": [{"id": s["id"], "nome": s["nome"], "dominio": s["dominio"], "ativo": s["ativo"], "fontes": {
        "gsc": bool(s["ids"].get("gsc")), "ga4": bool(s["ids"].get("ga4")), "clarity": bool(s["ids"].get("clarity")),
        "ranking": bool(s.get("ranking"))}, "origem": S.origem(s)} for s in S.carregar()]}


def _contexto(s):
    base, i = data(), s["ids"]
    return {"ok": True, "id": s["id"], "nome": s["nome"], "dominio": s["dominio"], "contexto": s["contexto"], "conselho": s["conselho"],
            "concorrentes": s["concorrentes"], "monitor": {"buscas": (s.get("monitor") or {}).get("buscas") or []},
            "ranking": {"palavras": (s.get("ranking") or {}).get("palavras") or []},
            "ids": {k: i.get(k) for k in ("gsc", "ga4", "ga4_medicao", "clarity")},
            "dados": {"gsc": str(base / "gsc" / s["id"] / "latest.md"), "verificador": str(base / "verificador" / s["id"] / "latest.md"),
                      "aio": str(base / "aio" / s["id"]) + "/"}, "secoes": projetos.ler_contexto(s["id"])}


def _clarity(s):
    arqs = sorted(data().glob(f"snapshots/*/{s['id']}-clarity.json"), key=lambda a: a.parent.name)
    if not s["ids"].get("clarity") or not arqs:
        return {"ok": False, "reason": "sem_clarity", "comoResolver": "o site não tem projeto do Clarity no cadastro ou a coleta diária ainda não rodou"}
    return {"ok": True, "coletadoEm": arqs[-1].parent.name, "janelaDias": 3,
            "metricas": json.loads(arqs[-1].read_text(encoding="utf-8"))}


def _auditoria(s):
    pasta = data() / "verificador" / s["id"]
    if not (pasta / "latest.json").is_file():
        return {"ok": False, "reason": "sem_auditoria", "comoResolver": "o verificador do hub ainda não rodou para este site"}
    return {"ok": True, **json.loads((pasta / "latest.json").read_text(encoding="utf-8")), "relatorio": _ler(pasta / "latest.md", MAX_TEXTO)}


def _oportunidades(s, args):
    arq = data() / "gsc" / s["id"] / "latest.json"
    if not arq.is_file():
        return {"ok": False, "reason": "sem_oportunidades", "comoResolver": "a análise semanal do Search Console ainda não rodou para este site"}
    tipo = args.get("tipo")
    if tipo not in (None, "vazamento", "reforcar", "buraco"):
        raise ErroUso("tipo tem que ser vazamento, reforcar ou buraco")
    d = json.loads(arq.read_text(encoding="utf-8"))
    ops = [o for o in d.get("oportunidades") or [] if tipo in (None, o.get("tipo"))]
    return {"ok": True, "data": d.get("data"), "periodo": d.get("periodo"), "total": len(ops),
            "oportunidades": ops[:_int(args, "limite", 50, 10000)]}


def _aio(s):
    pasta = data() / "aio" / s["id"]
    if not pasta.is_dir():
        return {"ok": False, "reason": "sem_aio", "comoResolver": "o site não tem buscas de AI Overview monitoradas"}
    meus = set(S.hosts(s))
    buscas = []
    for p in sorted(x for x in pasta.iterdir() if x.is_dir()):
        caps = sorted(p.glob("????-??-??.json"))
        if not caps:
            continue
        c = json.loads(caps[-1].read_text(encoding="utf-8"))
        pos = next((o.get("rank") for o in c.get("organico") or []
                    if citacao.host(o.get("url") or "") in meus or (o.get("domain") or "").lower() in meus), None)
        buscas.append({"pasta": p.name, "data": c.get("data") or caps[-1].stem, "busca": c.get("keyword"), "local": c.get("local"),
                       "temAiOverview": bool(c.get("tem_ai_overview")), "citado": bool(c.get("citado")),
                       "fontes": [f.get("url") for f in c.get("fontes") or []], "posicaoOrganica": pos})
    ciclos = sorted(pasta.glob("ciclo-*.md"))
    return {"ok": True, "buscas": buscas, "analise": _ler(ciclos[-1], MAX_TEXTO) if ciclos else None}


def _dia(args, chave, padrao):
    try:
        return dt.date.fromisoformat(args[chave]) if args.get(chave) else padrao
    except (TypeError, ValueError):
        raise ErroUso(f"{chave} tem que ser uma data AAAA-MM-DD") from None


def _bi(s, args):
    arq = data() / "bi/metricas.db"
    if not arq.is_file():
        return {"ok": False, "reason": "sem_armazem", "comoResolver": "o armazém do BI ainda não existe neste servidor"}
    c = None
    for modo in ("mode=ro", "immutable=1"):  # sem permissão de escrita na pasta (-wal/-shm): o 2º lê como imutável
        try:
            c = sqlite3.connect(f"file:{arq}?{modo}", uri=True)
            c.execute("select 1 from fato limit 1")
            break
        except sqlite3.OperationalError:
            if c:
                c.close()
            c = None
    if c is None:  # sem a tabela fato
        return {"ok": False, "reason": "sem_armazem", "comoResolver": "o armazém do BI ainda não existe neste servidor"}
    try:
        if not args.get("metrica"):
            linhas = c.execute("select fonte, metrica, min(dia), max(dia), count(distinct dia) from fato where negocio = ? "
                               "group by fonte, metrica order by fonte, metrica", (s["id"],)).fetchall()
            return {"ok": True, "site": s["id"], "metricas": [{"fonte": f, "metrica": m, "primeiroDia": a, "ultimoDia": b, "dias": n}
                                                              for f, m, a, b, n in linhas]}
        if not args.get("fonte"):
            raise ErroUso("com metrica, informe também fonte (sem metrica, a ferramenta lista as que existem)")
        ate = _dia(args, "ate", dt.datetime.now(ranking.BRT).date())
        de = _dia(args, "de", ate - dt.timedelta(days=27))
        if de > ate:
            raise ErroUso("de tem que ser antes de ate")
        filtro = (s["id"], args["fonte"], args["metrica"], de.isoformat(), ate.isoformat())
        sql = "from fato where negocio = ? and fonte = ? and metrica = ? and dia between ? and ?"
        valores = dict(c.execute(f"select dia, sum(valor) {sql} and dim = ? group by dia", (*filtro, args.get("dim") or "")).fetchall())
        serie = [{"dia": d, "valor": valores.get(d)} for d in ((de + dt.timedelta(days=i)).isoformat() for i in range((ate - de).days + 1))]
        r = {"ok": True, "site": s["id"], "fonte": args["fonte"], "metrica": args["metrica"], "de": de.isoformat(), "ate": ate.isoformat(),
             "serie": serie, "soma": sum(valores.values()), "diasComDado": len(valores)}
        if not args.get("dim"):
            dims = c.execute(f"select dim, sum(valor) {sql} and dim != '' group by dim order by 2 desc", filtro).fetchall()
            if dims:
                r["dims"] = [{"dim": d, "valor": v} for d, v in dims]
        return r
    finally:
        c.close()








def _palavra(i):
    info, prop = i.get("keyword_info") or {}, i.get("keyword_properties") or {}
    return {"keyword": i.get("keyword"), "search_volume": info.get("search_volume"), "cpc": info.get("cpc"),
            "competition": info.get("competition"), "keyword_difficulty": prop.get("keyword_difficulty"),
            "search_intent": (i.get("search_intent_info") or {}).get("main_intent")}


def _ads(i):
    ci = i.get("competition_index")
    return {"keyword": i["keyword"], "search_volume": i.get("search_volume"), "cpc": i.get("cpc"),
            "competition": None if ci is None else ci / 100, "keyword_difficulty": None, "search_intent": None}


def _visao_dominio(alvo, itens):
    m = (itens[0].get("metrics") if itens else None) or {}
    corte = lambda x: {k: v for k, v in x.items() if k in ("etv", "count") or k.startswith("pos_")} if x and x.get("count") else None
    return {"target": alvo, "organico": corte(m.get("organic")), "pago": corte(m.get("paid"))}


def _ranqueada(i):
    kd, el = i.get("keyword_data") or {}, (i.get("ranked_serp_element") or {}).get("serp_item") or {}
    info = kd.get("keyword_info") or {}
    return {"keyword": kd.get("keyword"), "rank_group": el.get("rank_group"), "url": el.get("url"),
            "search_volume": info.get("search_volume"), "cpc": info.get("cpc")}


def _fontes_aio(node, out):
    """Toda referência com url, em qualquer nível do item ai_overview (como aio_monitor.refs)."""
    if isinstance(node, dict):
        if node.get("url") and node.get("type") in (None, "ai_overview_reference"):
            out.append({"domain": node.get("domain"), "url": node["url"], "title": node.get("title")})
        for v in node.values():
            _fontes_aio(v, out)
    elif isinstance(node, list):
        for v in node:
            _fontes_aio(v, out)
    return out


def _serp(itens, aio):
    r = {"itens": [_so(i, ("type", "rank_group", "domain", "url", "title")) for i in itens if i.get("type") != "ai_overview"]}
    if aio:
        bloco = next((i for i in itens if i.get("type") == "ai_overview"), None)
        fontes = list({f["url"]: f for f in _fontes_aio(bloco, [])}.values()) if bloco else []
        r["aiOverview"] = {"presente": bool(bloco), "fontes": fontes}
    return r


def _dataforseo(nome, args, site):
    if nome == "get_keyword_metrics":
        if args.get("fonte") not in (None, "labs", "google_ads"):
            raise ErroUso("fonte tem que ser labs ou google_ads")
        if args.get("fonte") == "google_ads":
            return _dfs_resultado("keywords_data/google_ads/search_volume/live", {"keywords": _palavras(args, 1000), **_local(args)},
                                  lambda res: {"itens": [_ads(i) for i in res if i and i.get("keyword")]})
        return _dfs("dataforseo_labs/google/keyword_overview/live", {"keywords": _palavras(args, 100), **_local(args)},
                    lambda it: {"itens": [_palavra(i) for i in it]})
    if nome == "get_serp_results":
        if not args.get("keyword"):
            raise ErroUso("informe keyword")
        tarefa = {"keyword": args["keyword"], **_local(args), "device": args.get("device") or "desktop", "depth": _int(args, "depth", 10, 100)}
        aio = args.get("aiOverview") is True
        if aio:
            tarefa["load_async_ai_overview"] = True
        return _dfs("serp/google/organic/live/advanced", tarefa, lambda it: _serp(it, aio))
    if nome == "get_domain_overview":
        alvo = _alvo(args, site)
        return _dfs("dataforseo_labs/google/domain_rank_overview/live", {"target": alvo, **_local(args)}, lambda it: _visao_dominio(alvo, it))
    if nome == "get_ranked_keywords":
        alvo = _alvo(args, site)
        tarefa = {"target": alvo, **_local(args), "limit": _int(args, "limit", 100, 200)}
        if args.get("filters") is not None:
            if not isinstance(args["filters"], list) or not args["filters"]:
                raise ErroUso("filters tem que ser uma lista no formato do DataForSEO Labs")
            tarefa["filters"] = args["filters"]
        if args.get("orderBy") is not None:
            if not isinstance(args["orderBy"], list) or not 1 <= len(args["orderBy"]) <= 3 or not all(isinstance(o, str) for o in args["orderBy"]):
                raise ErroUso("orderBy tem que ser uma lista de 1 a 3 textos (ex.: keyword_data.keyword_info.search_volume,desc)")
            tarefa["order_by"] = args["orderBy"]
        return _dfs("dataforseo_labs/google/ranked_keywords/live", tarefa, lambda it: {"target": alvo, "itens": [_ranqueada(i) for i in it]})
    return _dfs("dataforseo_labs/google/serp_competitors/live", {"keywords": _palavras(args, 20), **_local(args)},
                lambda it: {"itens": [_so(i, ("domain", "avg_position", "etv", "keywords_count")) for i in it]})


DFS = {"get_keyword_metrics", "get_serp_results", "get_domain_overview", "get_ranked_keywords", "find_serp_competitors"}


def executar(nome, args, nota=None):
    """Dados da ferramenta; o id do site resolvido vai em nota["site"] (para o log). ErroUso: pedido errado."""
    nota = {} if nota is None else nota
    if nome not in FERRAMENTAS:
        msg = f'Ferramenta "{nome}" não existe no hub de SEO do {config.dono()} (o OpenSEO foi desligado). Use: {", ".join(FERRAMENTAS)}.'
        raise ErroUso(msg + (f" No lugar de {nome}, {SUBSTITUTAS[nome]}." if nome in SUBSTITUTAS else ""))
    if nome == "list_projects":
        return _projetos()
    site, desconhecido = _site(args, ("site", "projectId") if nome in DFS or nome in GRUPOS else ("site", "projectId", "domain", "dominio"))
    if desconhecido:
        return desconhecido
    nota["site"] = site and site["id"]
    dataforseo.anotar_site(nota["site"])
    if nome in GRUPOS:
        return GRUPOS[nome].executar(nome, args, site)
    if nome in DFS:
        return _dataforseo(nome, args, site)
    if not site:
        raise ErroUso(f"{nome} precisa do argumento site (id do site no hub, domínio ou nome); list_projects lista os sites")
    if nome in GOOGLE:
        return _google(nome, args, site)
    if nome == "get_project_context":
        return _contexto(site)
    if nome == "get_clarity_insights":
        return _clarity(site)
    if nome == "get_rank_tracker":
        return ranking.ler(site["id"], _int(args, "semanas", 12, 520))
    if nome == "get_site_audit":
        return _auditoria(site)
    if nome == "get_hub_opportunities":
        return _oportunidades(site, args)
    if nome == "get_ai_overview":
        return _aio(site)
    return _bi(site, args)


def _cliente(v):
    """X-Origem (quem chama o MCP: mercado, seo-local, tarefa...) só com letras, números e -_:."""
    v = "".join(c for c in str(v or "")[:60] if c.isalnum() or c in "-_:.")
    return v or None


class Servidor(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _enviar(self, status, corpo, tipo, headers=()):
        self.send_response_only(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, msg, status=200):
        env = json.dumps(msg, ensure_ascii=False)
        accept = self.headers.get("Accept", "")
        if "text/event-stream" in accept and "application/json" not in accept:
            return self._enviar(status, f"event: message\ndata: {env}\n\n".encode(), "text/event-stream")
        self._enviar(status, env.encode(), "application/json")

    def _erro(self, id_, codigo, msg, status=200):
        self._json({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": msg}}, status)

    def _ferramenta(self, nome, args, cliente=None):
        t0 = time.monotonic()
        nota, rotulo = {}, "erro"
        try:
            try:
                with dataforseo.origem(f"mcp:{nome}", cliente=cliente):  # custos.jsonl: quem gastou
                    dados = executar(nome, args, nota)
            except PermissionError:  # o serviço (DynamicUser) não lê o arquivo de $HUB_DATA
                dados = {"ok": False, "reason": "sem_permissao", "comoResolver": f"o serviço do MCP não consegue ler o arquivo; avise o {config.dono()}"}
            rotulo = (dados["reason"] if dados.get("ok") is False else
                      "erro:" + str((dados.get("error") or {}).get("code")) if dados.get("status") == "error" else "ok")
            res = {"content": [{"type": "text", "text": json.dumps(dados, ensure_ascii=False)}], "structuredContent": dados}
        except ErroUso as e:
            rotulo = "uso"
            res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # nunca derruba a thread: o cliente recebe isError, sem traceback nem credencial
            msg = (f"Falha na credencial do Google do hub; avise o {config.dono()}." if isinstance(e, google.ErroCredencial)
                   else f"Erro no hub ao responder esta ferramenta; tente de novo ou avise o {config.dono()}.")
            rotulo = "erro:" + type(e).__name__
            res = {"content": [{"type": "text", "text": msg}], "isError": True}
        return res, lambda: _log(f"tool={nome} site={nota.get('site')} resultado={rotulo} ms={int((time.monotonic() - t0) * 1000)}")

    def _mcp(self, corpo):
        try:
            msg = json.loads(corpo)
        except ValueError:
            return self._erro(None, -32700, "corpo não é JSON", 400)
        if not isinstance(msg, dict):
            return self._erro(None, -32600, "mande um objeto JSON-RPC (lote não é aceito)", 400)
        if "id" not in msg:  # notificação (ou resposta do cliente): nada a responder
            return self._enviar(202, b"", "text/plain")
        id_, metodo = msg["id"], msg.get("method")
        p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        log = None
        if metodo == "initialize":
            v = p.get("protocolVersion")
            res = {"protocolVersion": v if v in VERSOES else VERSOES[0], "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "nimbus-hub", "version": "2"}, "instructions": INSTRUCOES}
        elif metodo == "ping":
            res = {}
        elif metodo == "tools/list":
            res = {"tools": [{"name": n, **f} for n, f in FERRAMENTAS.items()]}
        elif metodo == "tools/call":
            a = p.get("arguments")
            res, log = self._ferramenta(str(p.get("name")), a if isinstance(a, dict) else {}, _cliente(self.headers.get("X-Origem")))
        else:
            return self._erro(id_, -32601, f"método {metodo!r} não existe")
        self._json({"jsonrpc": "2.0", "id": id_, "result": res})
        if log:
            log()

    def _recusa_navegador(self):
        """Sem autenticação, o túnel em localhost:4181 ficaria aberto a qualquer página do navegador do dono (POST cego com
        text/plain) e a DNS rebinding. Navegador sempre manda Origin em POST entre origens; cliente MCP de linha de comando não.
        Host tem que ser loopback ou o endereço em que o servidor escuta. Devolve True se já respondeu 403."""
        try:
            host = urlsplit("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            host = None
        if self.headers.get("Origin") is None and host in {"localhost", "127.0.0.1", "::1", self.server.server_address[0]}:
            return False
        self.close_connection = True
        self._enviar(403, b"origem nao permitida", "text/plain")
        return True

    def do_POST(self):
        try:
            if self._recusa_navegador():
                return
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True  # corpo chunked não é lido: não deixar sobra virar a próxima requisição
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                self.close_connection = True
                return self._enviar(400, b"Content-Length invalido", "text/plain")
            corpo = self.rfile.read(n) if n else b""  # shortcut: sem suporte a request chunked; clientes MCP mandam Content-Length
            if self.path.split("?")[0] != "/mcp":
                return self._enviar(404, b"nao encontrado", "text/plain")
            self._mcp(corpo)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            _log("falha: " + type(e).__name__)
            self.close_connection = True

    def do_GET(self):
        if self._recusa_navegador():
            return
        caminho = self.path.split("?")[0]
        if caminho == "/mcp":
            return self._enviar(405, b"use POST", "text/plain", [("Allow", "POST")])
        if caminho == "/api/health":
            return self._enviar(200, b'{"ok": true}', "application/json")
        if caminho == "/":
            return self._enviar(200, f"<!doctype html><meta charset=utf-8><p>Hub MCP de SEO do {config.dono()}: use POST em <code>/mcp</code>.</p>"
                                .encode(), "text/html; charset=utf-8")
        self._enviar(404, b"nao encontrado", "text/plain")


def criar_servidor(host, porta):
    srv = ThreadingHTTPServer((host, porta), Servidor)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.mcp_proxy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("MCP_PROXY_HOST", "127.0.0.1"))
    ap.add_argument("--porta", type=int, default=int(os.environ.get("MCP_PROXY_PORTA", "4181")))
    a = ap.parse_args(argv)
    srv = criar_servidor(a.host, a.porta)
    _log(f"em {a.host}:{a.porta}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
