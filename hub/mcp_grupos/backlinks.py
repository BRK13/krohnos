"""Grupo backlinks: get_backlinks_overview e get_backlinks_profile, restauradas da ferramenta antiga (mesmos argumentos, endpoints e campos).

Corpos das tarefas e mapeamento das linhas seguem o cliente do DataForSEO e os mapeadores do original.
"""
import datetime as dt
import re
from urllib.parse import urlparse

from hub.mcp_grupos import ErroUso, alvo, dfs_resultado

CARO = " Custa crédito do DataForSEO."
ESCOPOS = ("exact_url", "subfolder", "domain", "subdomains")
CAMPOS_LINHA = {"rank": "rank", "domainRank": "domain_from_rank", "spamScore": "backlink_spam_score", "firstSeen": "first_seen"}
TAMANHOS = (50, 100, 200)
LIMITE_SPAM = 40
MAX_CONDICOES = 8  # teto de condições de filtro por tarefa do DataForSEO

_SITE = {"site": {"type": "string", "description": "id do site no hub (ex.: psite13), domínio ou nome"},
         "projectId": {"type": "string", "description": "o mesmo que site (nome usado pela ferramenta antiga)"}}
_ALVO = {"type": "string", "minLength": 1, "maxLength": 2048,
         "description": "Domínio ou URL a analisar (ex.: 'example.com' ou 'https://example.com/blog'). Sem ele, vale domain/dominio ou o domínio do site."}
_ESCOPO = {"type": "string", "enum": [*ESCOPOS, "page"],
           "description": "Escopo: 'domain' (domínio sem subdomínios), 'subdomains' (domínio e todos os subdomínios), 'subfolder' (o caminho e o que há abaixo) "
                          "ou 'exact_url' (uma página). Padrão: 'subdomains' para domínio puro e 'subfolder' quando há caminho. 'page' é apelido antigo de 'exact_url'. "
                          "Em subfolder, as contagens vêm de totais filtrados; rank, tendências e o detalhamento por domínio não existem."}
_SPAM = ("Esconde backlinks com spam score de 40 ou mais. Padrão: true. Score desconhecido continua visível. É uma heurística, não um julgamento do link. "
         "Com true sobram seis condições de filtro do usuário (duas em subfolder); com false, oito (quatro em subfolder). Cada termo e cada limite numérico conta à parte.")
_NUM = {"type": ["number", "string"]}
_FILTROS = {"type": "object", "default": {}, "properties": {
    "include": {"type": "string"}, "exclude": {"type": "string"},
    "minDomainRank": _NUM, "maxDomainRank": _NUM, "minLinkAuthority": _NUM, "maxLinkAuthority": _NUM,
    "minSpamScore": _NUM, "maxSpamScore": _NUM,
    "linkType": {"type": "string", "enum": ["dofollow", "nofollow"]},
    "hideLost": {"type": "boolean"}, "hideBroken": {"type": "boolean"},
    "domainFrom": {"type": "string", "maxLength": 255}}}

FERRAMENTAS = {
    "get_backlinks_overview": {
        "description": "Resumo do perfil de backlinks (total de backlinks, domínios de origem, principais domínios de origem). Custa créditos (~50 para um domínio, ~25 para uma página). "
                       "Domínio puro usa o escopo 'subdomains'; passe scope 'domain' para excluir subdomínios dos totais. Alvo com caminho usa 'subfolder', cujas contagens vêm de totais "
                       "filtrados (sem rank, tendências nem detalhamento por domínio). A tendência sempre inclui subdomínios (limite da fonte)." + CARO,
        "inputSchema": {"type": "object", "properties": {
            **_SITE, "target": _ALVO, "scope": _ESCOPO,
            "hideSpam": {"type": "boolean", "description": "Esconde domínios de origem com spam. Padrão: true."},
            "topPages": {"type": "boolean", "description": "Também traz as páginas do alvo com mais backlinks (backlinks/domain_pages_summary, até 100). Custa crédito extra. Padrão: false."}}}},
    "get_backlinks_profile": {
        "description": "Uma página limitada de backlinks detalhados de um domínio ou página: URL de origem, URL de destino, âncora, dofollow/nofollow, sinais de autoridade e spam e se o link "
                       "está perdido ou quebrado. Aceita filtros, ordenação, modo one_per_domain/as_is e paginação. Custa créditos (~30 por página)." + CARO,
        "inputSchema": {"type": "object", "properties": {
            **_SITE, "target": _ALVO, "scope": _ESCOPO,
            "page": {"type": "integer", "exclusiveMinimum": 0, "default": 1, "description": "Página de resultados, a partir de 1. Padrão: 1."},
            "pageSize": {"type": "integer", "enum": list(TAMANHOS), "default": 50, "description": "Linhas por página: 50, 100 ou 200."},
            "sortField": {"type": "string", "enum": list(CAMPOS_LINHA), "default": "firstSeen", "description": "Campo de ordenação das linhas."},
            "sortOrder": {"type": "string", "enum": ["asc", "desc"], "default": "desc", "description": "Sentido da ordenação."},
            "filters": {**_FILTROS, "description": "Filtros das linhas: termos de URL de origem a incluir/excluir, faixas de autoridade e spam, dofollow/nofollow, esconder perdidos/quebrados ou domainFrom exato."},
            "mode": {"type": "string", "enum": ["one_per_domain", "as_is"], "default": "one_per_domain",
                     "description": "Agrupamento do DataForSEO: one_per_domain devolve o link mais forte de cada domínio de origem; as_is devolve cada backlink."},
            "hideSpam": {"type": "boolean", "description": _SPAM}}}},
}


def _agora():
    return dt.datetime.now(dt.timezone.utc)


def _alvo_api(texto, escopo):
    """Alvo do DataForSEO a partir do texto e do escopo pedido (mesmas regras do original). ErroUso se o alvo for inválido."""
    texto = texto.strip()
    if escopo is not None and escopo not in (*ESCOPOS, "page"):
        raise ErroUso(f"scope tem que ser um destes: {', '.join(ESCOPOS)}")
    escopo = "exact_url" if escopo == "page" else escopo
    p = urlparse(texto if re.match(r"^[a-zA-Z][a-zA-Z\d+.-]*://", texto) else "https://" + texto)
    try:
        url_host = (p.hostname or "").lower()
    except ValueError:
        url_host = ""
    host = re.sub(r"^www\.", "", url_host)
    if p.username or p.password or "." not in host or not re.fullmatch(r"[a-z\d.-]+", host):
        raise ErroUso("target tem que ser um domínio como example.com ou uma URL")
    caminho = p.path.rstrip("/")
    if escopo == "subfolder" and not caminho:
        raise ErroUso("subfolder precisa de um caminho (ex.: example.com/blog)")
    escopo = escopo or ("subfolder" if caminho else "subdomains")
    if escopo != "exact_url":
        return {"api": host, "exibir": host + caminho if escopo == "subfolder" else host, "escopo": escopo,
                "subdominios": escopo == "subdomains", "caminho": caminho if escopo == "subfolder" else ""}
    if re.search(r"[?#]", texto):
        raise ErroUso("URL de página com query string ou fragmento não é aceita")
    url = f"{'http' if texto.lower().startswith('http://') else 'https'}://{url_host}{caminho or '/'}"
    return {"api": url, "exibir": url, "escopo": escopo, "subdominios": True, "caminho": ""}


def _corpo(a, **extra):
    # padrões do original: só links vivos de fora do domínio, rank de 0 a 100
    return {"target": a["api"], "include_subdomains": a["subdominios"], "include_indirect_links": True,
            "exclude_internal_backlinks": True, "backlinks_status_type": "live", "rank_scale": "one_hundred", **extra}


def _like(termo):
    return re.sub(r"([\\%_])", r"\\\1", termo)


def _termos(valor):
    return [t.strip() for t in re.split(r"[,+]", valor.lower()) if t.strip()] if isinstance(valor, str) else []


def _e(clausulas):
    """Junta condições com "and"."""
    return [x for i, c in enumerate(clausulas) for x in (["and", c] if i else [c])]


def _condicoes(expr):
    return sum(_condicoes(c) if isinstance(c[0], list) else 1 for c in expr if isinstance(c, list))


def _filtro_escopo(a):
    """Subfolder: a API não tem prefixo, então url_to casa o caminho por like (host com e sem www)."""
    if a["escopo"] != "subfolder":
        return []
    c = _like(a["caminho"])
    ors = [["url_to", "like", padrao] for h in (_like(a["api"]), "www." + _like(a["api"])) for padrao in (f"%://{h}{c}", f"%://{h}{c}/%")]
    return [[x for i, o in enumerate(ors) for x in (["or", o] if i else [o])]]


def _faixa(saida, campo, minimo, maximo):
    for op, v in ((">=", minimo), ("<=", maximo)):
        if v is not None and v != "":
            try:
                saida.append([campo, op, float(v) if isinstance(v, str) else v])
            except ValueError:
                raise ErroUso(f"filters: {campo} tem que ser um número") from None


def _filtros_linhas(f):
    """Filtros do usuário para backlinks/backlinks: include (OU) e as demais condições, tudo ligado por "and"."""
    if not isinstance(f, dict):
        raise ErroUso("filters tem que ser um objeto")
    inc = [["url_from", "ilike", f"%{_like(t)}%"] for t in _termos(f.get("include"))]
    cond = [["url_from", "not_ilike", f"%{_like(t)}%"] for t in _termos(f.get("exclude"))]
    _faixa(cond, "domain_from_rank", f.get("minDomainRank"), f.get("maxDomainRank"))
    _faixa(cond, "rank", f.get("minLinkAuthority"), f.get("maxLinkAuthority"))
    _faixa(cond, "backlink_spam_score", f.get("minSpamScore"), f.get("maxSpamScore"))
    if f.get("linkType") in ("dofollow", "nofollow"):
        cond.append(["dofollow", "=", f["linkType"] == "dofollow"])
    if f.get("hideLost"):
        cond.append(["is_lost", "=", False])
    if f.get("hideBroken"):
        cond.append(["is_broken", "=", False])
    if f.get("domainFrom"):
        cond.append(["domain_from", "=", str(f["domainFrom"])])
    grupo = []
    if inc:
        grupo = [inc[0] if len(inc) == 1 else [x for i, o in enumerate(inc) for x in (["or", o] if i else [o])]]
    return _e(grupo + cond)


def _com_spam(filtros, campo, esconder):
    if not esconder:
        return filtros
    # linhas: score < 40 ou desconhecido; domínios de origem: só score <= 40 (como o original)
    spam = [[campo, "<", LIMITE_SPAM], "or", [campo, "=", None]] if campo == "backlink_spam_score" else [campo, "<=", LIMITE_SPAM]
    return [*filtros, "and", spam] if filtros else [spam]


def _final(escopo, usuario, campo_spam, esconder):
    """Filtro final: escopo, depois o do usuário, depois o de spam. Estoura o teto de condições -> ErroUso."""
    f = _e(escopo)
    f = [*f, "and", *usuario] if f and usuario else f or usuario
    f = _com_spam(f, campo_spam, esconder)
    if _condicoes(f) > MAX_CONDICOES:
        raise ErroUso(f"filtros demais ({_condicoes(f)} condições de no máximo {MAX_CONDICOES}); tire alguns ou use hideSpam false")
    return f or None


def _primeiro(*v):
    return next((x for x in v if x is not None), None)


def _linha(i):
    return {"domainFrom": i.get("domain_from"), "urlFrom": i.get("url_from"), "urlTo": i.get("url_to"), "anchor": i.get("anchor"),
            "itemType": i.get("item_type"), "isDofollow": i.get("dofollow"), "relAttributes": i.get("rel_attributes") or i.get("attributes") or [],
            "rank": i.get("rank"), "domainFromRank": i.get("domain_from_rank"), "pageFromRank": i.get("page_from_rank"),
            "spamScore": _primeiro(i.get("backlink_spam_score"), i.get("backlinks_spam_score")), "firstSeen": i.get("first_seen"),
            "lastSeen": _primeiro(i.get("lost_date"), i.get("last_seen"), i.get("last_visited")),
            "isLost": i["is_lost"] if i.get("is_lost") is not None else bool(i.get("lost_date")), "isBroken": bool(i.get("is_broken")), "linksCount": i.get("links_count")}


def _pagina_top(i):
    return {"url": i.get("url"), "rank": i.get("rank"), "backlinks": i.get("backlinks"), "referringDomains": i.get("referring_domains"),
            "referringPages": i.get("referring_pages"), "spamScore": i.get("backlinks_spam_score"), "brokenBacklinks": i.get("broken_backlinks"),
            "firstSeen": i.get("first_seen")}


def _dominio(i):
    return {"domain": i.get("domain"), "backlinks": i.get("backlinks"), "referringPages": i.get("referring_pages"), "rank": i.get("rank"),
            "spamScore": i.get("backlinks_spam_score"), "firstSeen": i.get("first_seen"), "brokenBacklinks": i.get("broken_backlinks"),
            "brokenPages": i.get("broken_pages")}


def _pagina(mapa, pagina, tamanho, deslocamento):
    """montar() de uma lista paginada (result[0] com items e total_count)."""
    def montar(res):
        r = (res[0] if res else None) or {}
        linhas = [mapa(i) for i in r.get("items") or []]
        total = r.get("total_count")
        return {"rows": linhas, "totalCount": total, "hasMore": deslocamento + len(linhas) < total if total is not None else len(linhas) == tamanho,
                "page": pagina, "pageSize": tamanho, "fetchedAt": _agora().isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    return montar


def _em_serie(*chamadas):
    """Roda as chamadas em ordem; custo é a soma. A primeira que falhar devolve o erro dela, com o custo já gasto."""
    total, partes = 0, {}
    for c in chamadas:
        r = c()
        total += r.get("custo") or 0
        if not r["ok"]:
            return {**r, "custo": round(total, 6)}
        partes.update({k: v for k, v in r.items() if k not in ("ok", "custo")})
    return {"ok": True, **partes, "custo": round(total, 6)}


def _linhas(a, limite, deslocamento, modo, filtros, ordem):
    # sem offset (leituras de total do subfolder) ele não vai no corpo; referring_domains não tem mode
    extra = {"offset": deslocamento, "mode": modo, "filters": filtros}
    return _corpo(a, limit=limite, order_by=ordem, **{k: v for k, v in extra.items() if v is not None and v != []})


def _resumo(res):
    s = (res[0] if res else None) or {}
    return {"rank": s.get("rank"), "backlinks": s.get("backlinks"), "referringPages": s.get("referring_pages"),
            "referringDomains": s.get("referring_domains"), "brokenBacklinks": s.get("broken_backlinks"), "brokenPages": s.get("broken_pages"),
            "backlinksSpamScore": s.get("backlinks_spam_score"), "targetSpamScore": (s.get("info") or {}).get("target_spam_score"),
            "newBacklinks": s.get("new_backlinks"),  "lostBacklinks": s.get("lost_backlinks"),
            "newReferringDomains": s.get("new_referring_domains", s.get("new_reffering_domains")),
            "lostReferringDomains": s.get("lost_referring_domains", s.get("lost_reffering_domains"))}


def _historico(res):
    itens = [i for r in res for i in (r or {}).get("items") or [] if i.get("date")]
    d = lambda i: str(i["date"])[:10]
    return {"trends": [{"date": d(i), "backlinks": i.get("backlinks"), "referringDomains": i.get("referring_domains"), "rank": i.get("rank")} for i in itens],
            "newLostTrends": [{"date": d(i), "newBacklinks": i.get("new_backlinks"), "lostBacklinks": i.get("lost_backlinks"),
                               "newReferringDomains": i.get("new_referring_domains", i.get("new_reffering_domains")),
                               "lostReferringDomains": i.get("lost_referring_domains", i.get("lost_reffering_domains"))} for i in itens]}


def _visao(args, site):
    a = _alvo_api(alvo(args, site), args.get("scope"))
    esconder = args.get("hideSpam") is not False
    top = args.get("topPages") is True
    if top and a["escopo"] not in ("domain", "subdomains"):
        raise ErroUso("topPages só vale para os escopos domain e subdomains")
    agora = _agora()
    sem_resumo = {"rank": None, "backlinks": None, "referringPages": None, "referringDomains": None, "brokenBacklinks": None, "brokenPages": None,
                  "backlinksSpamScore": None, "targetSpamScore": None, "newBacklinks": None, "lostBacklinks": None,
                  "newReferringDomains": None, "lostReferringDomains": None}
    if a["escopo"] == "subfolder":
        # summary e history só aceitam alvo inteiro: os totais vêm de total_count de duas leituras filtradas (todos os links e um por domínio)
        f = _final(_filtro_escopo(a), [], "backlink_spam_score", False)
        r = _em_serie(
            lambda: dfs_resultado("backlinks/backlinks/live", _linhas(a, 1, None, "as_is", f, ["rank,desc"]), lambda res: {"_todos": (res[0] or {}).get("total_count") if res else None}),
            lambda: dfs_resultado("backlinks/backlinks/live", _linhas(a, 1, None, "one_per_domain", f, ["rank,desc"]), lambda res: {"_dominios": (res[0] or {}).get("total_count") if res else None}))
        if not r["ok"]:
            return r
        resumo = {**sem_resumo, "backlinks": r.pop("_todos"), "referringDomains": r.pop("_dominios")}
        partes, dominios, paginas_top = {"trends": [], "newLostTrends": []}, None, None
    else:
        fim = agora.date() - dt.timedelta(days=1)
        ini = fim.replace(year=fim.year - 1) if (fim.month, fim.day) != (2, 29) else dt.date(fim.year - 1, 3, 1)  # como o setUTCFullYear do original
        # history/live só aceita o domínio e não tem include_subdomains: a tendência inclui subdomínios
        hist = [] if a["escopo"] == "exact_url" else [lambda: dfs_resultado(
            "backlinks/history/live", {"target": a["api"], "date_from": ini.isoformat(), "date_to": fim.isoformat(), "rank_scale": "one_hundred"}, _historico)]
        tarefa_dom = _linhas(a, 100, 0, None, _final([], [], "backlinks_spam_score", esconder), ["backlinks,desc"])
        paginas = [lambda: dfs_resultado("backlinks/domain_pages_summary/live", _corpo(a, limit=100, order_by=["backlinks,desc"]),
                                         lambda res: {"topPages": [_pagina_top(i) for i in ((res[0] if res else None) or {}).get("items") or []]})] if top else []
        r = _em_serie(lambda: dfs_resultado("backlinks/summary/live", _corpo(a), lambda res: {"summary": _resumo(res)}),
                      *hist,
                      lambda: dfs_resultado("backlinks/referring_domains/live", tarefa_dom, lambda res: {"referringDomains": _pagina(_dominio, 1, 100, 0)(res)}),
                      *paginas)
        if not r["ok"]:
            return r
        resumo, partes, dominios = r.pop("summary"), {"trends": r.pop("trends", []), "newLostTrends": r.pop("newLostTrends", [])}, r.pop("referringDomains")
        paginas_top = r.pop("topPages", None)
    nota = {"domain": "O resumo exclui subdomínios; a tendência inclui subdomínios (limite da fonte).",
            "subfolder": "As contagens vêm de totais filtrados; rank, tendências e o detalhamento por domínio de origem não existem para subfolder."}.get(a["escopo"])
    return {"ok": True, "target": a["exibir"], "scope": a["escopo"], **({"scopeNote": nota} if nota else {}),
            "overview": {"overview": {"target": a["api"], "displayTarget": a["exibir"], "scope": a["escopo"], "summary": resumo, **partes,
                                      "fetchedAt": agora.isoformat(timespec="milliseconds").replace("+00:00", "Z")}},
            **({"referringDomains": dominios} if dominios else {}), **({"topPages": paginas_top} if top else {}), "custo": r["custo"]}


def _perfil(args, site):
    a = _alvo_api(alvo(args, site), args.get("scope"))
    try:
        pagina, tamanho = args.get("page", 1), args.get("pageSize", TAMANHOS[0])
        if isinstance(pagina, str) or isinstance(tamanho, str) or int(pagina) != pagina or int(tamanho) != tamanho:
            raise ValueError
        pagina, tamanho = int(pagina), int(tamanho)
    except (TypeError, ValueError, OverflowError):  # OverflowError: page=Infinity
        raise ErroUso("page e pageSize têm que ser números") from None
    if pagina < 1 or tamanho not in TAMANHOS:
        raise ErroUso(f"page começa em 1 e pageSize é um destes: {', '.join(map(str, TAMANHOS))}")
    campo, ordem, modo = args.get("sortField") or "firstSeen", args.get("sortOrder") or "desc", args.get("mode") or "one_per_domain"
    if campo not in CAMPOS_LINHA or ordem not in ("asc", "desc") or modo not in ("one_per_domain", "as_is"):
        raise ErroUso("sortField, sortOrder ou mode inválido")
    desloc = (pagina - 1) * tamanho
    f = _final(_filtro_escopo(a), _filtros_linhas(args.get("filters") or {}), "backlink_spam_score", args.get("hideSpam") is not False)
    r = dfs_resultado("backlinks/backlinks/live", _linhas(a, tamanho, desloc, modo, f, [f"{CAMPOS_LINHA[campo]},{ordem}"]),
                      lambda res: {"backlinks": _pagina(_linha, pagina, tamanho, desloc)(res)})
    return r if not r["ok"] else {"ok": True, "target": a["exibir"], "scope": a["escopo"], "backlinks": r["backlinks"], "custo": r["custo"]}


def executar(nome, args, site):
    return (_visao if nome == "get_backlinks_overview" else _perfil)(args, site)
