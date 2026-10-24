"""Grupo palavras: pesquisa de palavras-chave, palavras salvas por site e busca de locais do Google."""
import contextlib
import fcntl
import json
import os
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from hub import data, dataforseo
from hub.config import dono
from hub.mcp_grupos import ErroUso, dfs, local, pasta

CARO = " Custa crédito do DataForSEO."
PROJETO = {"projectId": {"type": "string", "description": "id do site no hub (o mesmo que site)"},
           "site": {"type": "string", "description": "id do site no hub (ex.: psite15), domínio ou nome"}}
LOCAL = {"locationCode": {"type": "integer", "minimum": 1, "description": "código de local do DataForSEO (padrão: Brasil)"},
         "languageCode": {"type": "string", "description": "código de idioma (ex.: pt, en); padrão: pt"}}
ESCOPOS = ("exact_url", "subfolder", "domain", "subdomains")
LIMITES = (150, 300, 500)
ISO_DO_LOCAL = {2076: "br", 2620: "pt", 2840: "us", 2372: "ie", 2826: "gb", 2124: "ca", 2276: "de", 2380: "it", 2724: "es", 2392: "jp", 2036: "au"}  # subconjunto de sites.LOCAIS
MIN_FORA_DA_SEMENTE = 5  # abaixo disso o modo auto cai para "relacionadas"

FERRAMENTAS = {
    "research_keywords": {
        "description": "Pesquisa de palavras (volume, dificuldade, CPC e ideias relacionadas) para 1 a 5 palavras-semente numa chamada. "
                       "Cada semente é pesquisada sozinha e devolve resultado próprio: uma semente ruim não derruba as outras." + CARO,
        "inputSchema": {"type": "object", "properties": {
            **PROJETO,
            "seeds": {"type": "array", "minItems": 1, "maxItems": 5, "description": "1 a 5 sementes; cada uma volta com palavras relacionadas, volume, dificuldade e CPC.",
                      "items": {"type": "object", "required": ["seed"], "properties": {
                          "seed": {"type": "string", "minLength": 1, "description": "palavra-semente a pesquisar"}, **LOCAL,
                          "locationName": {"type": "string", "minLength": 1, "description": "cidade ou região do país da semente, para volume local (use o locationName exato de search_serp_locations). Volume, CPC e concorrência vêm do Google Ads dessa área; ideias, dificuldade e intenção seguem nacionais. Custa um pouco mais por semente."}}}},
            "resultLimit": {"type": "integer", "enum": list(LIMITES), "description": "máximo de palavras por semente (padrão 150); sugestões e ideias se misturam, sem repetir"},
            "includeClickstreamData": {"type": "boolean", "description": "refina o volume com dados de clickstream; DOBRA o custo de cada semente (padrão false)"},
            "groupKeywords": {"type": "boolean", "description": "inclui variantes que dividem volume (ignore_synonyms=false); padrão false só traz as palavras principais"}},
            "required": ["seeds"]}},
    "get_domain_keyword_suggestions": {
        "description": "Palavras orgânicas em que um domínio aparece, com posição e métricas. Serve para a lista detalhada de oportunidades de um concorrente ou referência." + CARO,
        "inputSchema": {"type": "object", "properties": {
            **PROJETO,
            "domain": {"type": "string", "minLength": 1, "maxLength": 2048, "description": "domínio ou URL do concorrente/referência de onde extrair as palavras"},
            "scope": {"type": "string", "enum": list(ESCOPOS), "description": "escopo: domain (host sem subdomínios), subdomains (host e subdomínios), subfolder (caminho e filhos) ou exact_url (uma página). Padrão: subdomains para raiz, subfolder quando há caminho."},
            **LOCAL},
            "required": ["domain"]}},
}


GRATIS = " Não gasta crédito: não chama o DataForSEO."
TAGS = {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 64}}
INTENCOES = ("informational", "commercial", "transactional", "navigational", "unknown")
METRICAS = ("searchVolume", "keywordDifficulty", "cpc", "competition", "intent")

FERRAMENTAS.update({
    "save_keywords": {
        "description": "Salva palavras na lista de palavras salvas do site. Idempotente: salvar de novo uma palavra existente não duplica (só atualiza métricas e tags, se vierem). "
                       "Por padrão as tags se somam; com tagMode=replace trocam as tags dessas palavras. Confirme com o usuário antes de aplicar ou trocar tags em muitas palavras." + GRATIS,
        "inputSchema": {"type": "object", "properties": {
            **PROJETO,
            "keywords": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "minLength": 1}, "description": "palavras a salvar (1 a 100)"},
            "metrics": {"type": "array", "maxItems": 100, "description": "métricas opcionais, copiadas das linhas de research_keywords ou get_keyword_metrics; cada uma casa com a palavra pelo campo keyword",
                        "items": {"type": "object", "required": ["keyword"], "properties": {
                            "keyword": {"type": "string", "minLength": 1},
                            "searchVolume": {"type": ["integer", "null"], "minimum": 0}, "cpc": {"type": ["number", "null"], "minimum": 0},
                            "competition": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                            "keywordDifficulty": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                            "intent": {"type": ["string", "null"], "enum": [*INTENCOES, None]}, "monthlySearches": {"type": "array"}}}},
            "tags": {**TAGS, "description": "tags opcionais para todas as palavras salvas; peça confirmação ao usuário antes de usar, principalmente ao criar tags novas"},
            "tagMode": {"type": "string", "enum": ["append", "replace"], "description": "como aplicar as tags (padrão append); replace tira as tags atuais dessas palavras antes"},
            **LOCAL}, "required": ["keywords"]}},
    "list_saved_keywords": {
        "description": "Lista as palavras salvas do site (com métricas em cache: volume, dificuldade, CPC e tags). Cada linha traz o id para remove_saved_keywords. "
                       "Use os filtros de tags para um segmento salvo; várias tags casam com QUALQUER uma." + GRATIS,
        "inputSchema": {"type": "object", "properties": {
            **PROJETO,
            "search": {"type": "string", "minLength": 1, "maxLength": 200, "description": "filtro opcional pelo texto da palavra"},
            "tags": {**TAGS, "description": "filtro opcional por nomes de tag; várias tags casam com QUALQUER uma"},
            "limit": {"type": "integer", "enum": [50, 100, 250], "description": "máximo de linhas (padrão 100)"}}}},
    "remove_saved_keywords": {
        "description": "Apaga de vez palavras da lista de salvas do site, pelo id da linha (savedKeywordIds, de list_saved_keywords) ou pelo texto (keywords). "
                       "Id ou palavra que não existe é ignorado; deletedCount diz quantas linhas saíram." + GRATIS,
        "inputSchema": {"type": "object", "properties": {
            **PROJETO,
            "savedKeywordIds": {"type": "array", "minItems": 1, "maxItems": 2000, "items": {"type": "string", "minLength": 1}, "description": "ids das linhas salvas, os `id` de list_saved_keywords"},
            "keywords": {"type": "array", "minItems": 1, "maxItems": 2000, "items": {"type": "string", "minLength": 1}, "description": "alternativa aos ids: o texto das palavras (ignora caixa)"}}}},
    "search_serp_locations": {
        "description": "Acha o nome exato, no DataForSEO, de uma cidade, condado ou região (nomes em inglês, como vêm do Google). Devolve até 50 resultados; use o `locationName` sem mudar "
                       "em research_keywords, para volume local. Não gasta crédito.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 100, "description": 'nome do lugar, ex.: "Belo Horizonte" ou "Lisbon"'},
            "countryCode": {"type": "string", "pattern": "^[a-zA-Z]{2}$", "description": 'código ISO de 2 letras do país, ex.: "br"'}},
            "required": ["query", "countryCode"]}},
})


def _normal(k):
    return k.strip().lower()


def _linha(item, clickstream):
    """Uma linha de pesquisa a partir de um item de keyword_data do Labs (sem a série mensal, que pesa e ninguém lê)."""
    info = item.get("keyword_info") or {}
    cs = item.get("keyword_info_normalized_with_clickstream") or {}
    vol = cs if clickstream and cs.get("search_volume") else info
    intencao = ((item.get("search_intent_info") or {}).get("main_intent") or "").lower()
    return {"keyword": _normal(item["keyword"]), "searchVolume": vol.get("search_volume"),
            "keywordDifficulty": (item.get("keyword_properties") or {}).get("keyword_difficulty"),
            "cpc": info.get("cpc"), "competition": info.get("competition"),
            "intent": next((n for p, n in (("inform", "informational"), ("commerc", "commercial"),
                                           ("transact", "transactional"), ("navig", "navigational")) if p in intencao), "unknown")}


def _linhas(itens, clickstream):
    vistas, linhas = set(), []
    for i in itens:
        i = i["keyword_data"] if "keyword_data" in i else i  # related embrulha o item um nível abaixo
        if not i or not i.get("keyword"):
            continue
        l = _linha(i, clickstream)
        if l["keyword"] not in vistas:
            vistas.add(l["keyword"])
            linhas.append(l)
    return linhas


def _intercalar(a, b, limite):
    """Alterna as duas listas sem repetir palavra, para uma fonte ordenada por volume não esgotar o limite sozinha."""
    vistas, out = set(), []
    for i in range(max(len(a), len(b))):
        for fonte in (a, b):
            if i < len(fonte) and fonte[i]["keyword"] not in vistas and len(out) < limite:
                vistas.add(fonte[i]["keyword"])
                out.append(fonte[i])
    return out


def _fonte(fonte, semente, loc, limite, args):
    comum = {**loc, "limit": limite, "include_serp_info": False, "ignore_synonyms": not args["groupKeywords"],
             "include_clickstream_data": args["includeClickstreamData"]}
    if fonte == "related":
        caminho, tarefa = "related_keywords", {"keyword": semente, **comum, "depth": 3}
    elif fonte == "suggestions":
        caminho, tarefa = "keyword_suggestions", {"keyword": semente, **comum, "include_seed_keyword": True, "exact_match": False}
    else:
        caminho, tarefa = "keyword_ideas", {"keywords": [semente], **comum, "closely_variants": False}
    return dfs(f"dataforseo_labs/google/{caminho}/live", tarefa, lambda itens: {"linhas": _linhas(itens, args["includeClickstreamData"])})


def _localizar(linhas, nome, loc):
    """Troca volume, CPC e concorrência pelos do Google Ads para a cidade/região; palavra que o Ads não devolve fica com null, nunca com o número nacional.
    O nome já foi conferido em _semente."""
    # uma palavra recusada derruba a tarefa inteira no Ads: só vão as aceitas
    aceitas = [l["keyword"] for l in linhas if len(l["keyword"]) <= 80 and len(l["keyword"].split()) <= 10
               and not re.search(r"[!@%,*(){}<>|^~;=?`]", l["keyword"])]
    por_palavra, custo = {}, 0
    if aceitas:
        tarefa = {"keywords": aceitas, "location_name": nome, "language_code": loc["language_code"]}
        try:
            tarefas = dataforseo.post("keywords_data/google_ads/search_volume/live", [tarefa])
        except (RuntimeError, OSError, KeyError, ValueError):
            return linhas, 0, "DataForSEO indisponível para o volume local"
        custo = round(sum(t.get("cost") or 0 for t in tarefas), 6)
        t = tarefas[0] if tarefas else {}
        if t.get("status_code") != 20000:
            return linhas, custo, f"DataForSEO {t.get('status_code')}: {t.get('status_message')}"
        por_palavra = {_normal(i["keyword"]): i for i in t.get("result") or [] if i.get("keyword")}
    def nova(l):
        i = por_palavra.get(l["keyword"]) or {}
        ci = i.get("competition_index")
        return {**l, "searchVolume": i.get("search_volume"), "cpc": i.get("cpc"), "competition": None if ci is None else ci / 100}
    return [nova(l) for l in linhas], custo, None


def _semente(item, args):
    """Pesquisa uma semente (modo auto do original). Devolve (resultado, custo); falha vira ok False só dessa semente."""
    semente = _normal(item["seed"])
    if item.get("locationName"):  # o volume local substitui o de clickstream: não paga por ele
        args = {**args, "includeClickstreamData": False}
    loc = local(item)
    nome = item.get("locationName")
    if nome and loc["location_code"] in ISO_DO_LOCAL:  # confere o nome antes de qualquer chamada paga (grátis, com cache)
        iso = ISO_DO_LOCAL[loc["location_code"]]
        try:
            if not any(l["locationName"] == nome for l in _locais_do_pais(iso)):
                return {"seed": item["seed"], "ok": False, "error": f"locationName {nome!r} não é cidade/condado/região de {iso.upper()}; use o nome exato de search_serp_locations"}, 0
        except (RuntimeError, OSError, KeyError, ValueError):
            return {"seed": item["seed"], "ok": False, "error": "não deu para conferir o locationName no DataForSEO agora"}, 0
    custo, limite = 0, args["resultLimit"]
    partes = {}
    for f in ("suggestions", "ideas"):
        r = _fonte(f, semente, loc, -(-limite // 2), args)
        custo += r.get("custo") or 0
        if not r["ok"]:
            return {"seed": item["seed"], "ok": False, "error": r["message"]}, custo
        partes[f] = r["linhas"]
    linhas, usou_fallback = _intercalar(partes["suggestions"], partes["ideas"], limite), False
    if sum(l["keyword"] != semente for l in linhas) < MIN_FORA_DA_SEMENTE:
        r = _fonte("related", semente, loc, limite, args)
        custo += r.get("custo") or 0
        if not r["ok"]:
            return {"seed": item["seed"], "ok": False, "error": r["message"]}, custo
        linhas, usou_fallback = _intercalar(linhas, r["linhas"], limite), True
    if item.get("locationName"):
        linhas, custo_local, erro = _localizar(linhas, item["locationName"], loc)
        custo += custo_local
        if erro:
            return {"seed": item["seed"], "ok": False, "error": erro}, custo
    return {"seed": item["seed"], **({"locationName": nome} if nome else {}), "ok": True, "rowCount": len(linhas), "source": "blended", "usedFallback": usou_fallback, "rows": linhas}, custo


def _research_keywords(args):
    seeds = args.get("seeds")
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 5:
        raise ErroUso("seeds tem que ser uma lista de 1 a 5 itens com seed")
    for s in seeds:
        if not isinstance(s, dict) or not isinstance(s.get("seed"), str) or not s["seed"].strip():
            raise ErroUso("cada item de seeds precisa de seed (texto não vazio)")
        if "locationName" in s and not (isinstance(s["locationName"], str) and s["locationName"].strip()):
            raise ErroUso("locationName, se informado, tem que ser um texto não vazio")
        c = s.get("locationCode")
        if c is not None and (not isinstance(c, int) or isinstance(c, bool) or c < 1):
            raise ErroUso("locationCode tem que ser um inteiro a partir de 1")
        if s.get("languageCode") is not None and not isinstance(s["languageCode"], str):
            raise ErroUso("languageCode tem que ser um texto")
    limite = args.get("resultLimit") or 150
    if not isinstance(limite, int) or isinstance(limite, bool) or limite not in LIMITES:
        raise ErroUso(f"resultLimit tem que ser um de {', '.join(map(str, LIMITES))}")
    a = {"resultLimit": limite, "includeClickstreamData": bool(args.get("includeClickstreamData")), "groupKeywords": bool(args.get("groupKeywords"))}
    with ThreadPoolExecutor(len(seeds)) as ex:  # em paralelo como o original; map preserva a ordem
        pares = list(ex.map(dataforseo.na_thread(lambda s: _semente(s, a)), seeds))
    return {"ok": True, "results": [p[0] for p in pares], "custo": round(sum(p[1] for p in pares), 6)}


def _pesquisa(texto, escopo):
    """Domínio ou URL -> (host sem www, caminho, escopo, rótulo). ErroUso se o texto não for domínio."""
    t = (texto or "").strip()
    if escopo is not None and escopo not in ESCOPOS:
        raise ErroUso(f"scope tem que ser um de {', '.join(ESCOPOS)}")
    try:
        u = urlsplit(t if re.match(r"^[a-zA-Z][a-zA-Z\d+.-]*://", t) else "https://" + t)
        host = (u.hostname or "").lower().removeprefix("www.")
    except ValueError:
        host = ""
    if not t or u.username or u.password or "." not in host or not re.fullmatch(r"[a-z\d.-]+", host):
        raise ErroUso("domain tem que ser um domínio ou URL válido, como exemplo.com")
    caminho = u.path.rstrip("/")
    if escopo == "subfolder" and not caminho:
        raise ErroUso("scope subfolder precisa de caminho (ex.: exemplo.com/blog)")
    escopo = escopo or ("subfolder" if caminho else "subdomains")
    return host, caminho, escopo, host + caminho if escopo in ("exact_url", "subfolder") else host


def _like(s):
    return re.sub(r"([\\%_])", r"\\\1", s)


def _filtro(host, caminho, escopo):
    """Escopo do original em filtros do ranked_keywords (o alvo já cobre host e subdomínios)."""
    if escopo == "subdomains":
        return None
    url = "ranked_serp_element.serp_item.relative_url"
    cam = caminho or "/"
    pin = ["ranked_serp_element.serp_item.domain", "in", [host, "www." + host]]
    if escopo == "domain":
        return [pin]
    if escopo == "subfolder":
        ou = [[url, "=", cam], "or", [url, "like", _like(cam) + "/%"], "or", [url, "like", _like(cam) + "?%"]]
    else:
        ou = [[url, "in", [cam, cam + "/"]], "or", [url, "like", _like(cam) + "?%"], "or", [url, "like", _like(cam) + "/?%"]]
    return [pin, "and", ou]


def _inteiro_ou_none(v):
    return None if v is None else round(v)


def _sugestoes(itens):
    out = []
    for i in itens:
        kd, ki = i.get("keyword_data") or {}, (i.get("keyword_data") or {}).get("keyword_info") or {}
        el = i.get("ranked_serp_element") or {}
        si = el.get("serp_item") or {}
        keyword = kd.get("keyword") or i.get("keyword")
        if not keyword:
            continue
        dif = (kd.get("keyword_properties") or {}).get("keyword_difficulty")
        if dif is None:
            dif = ki.get("keyword_difficulty")
        pos = si.get("rank_absolute", el.get("rank_absolute"))
        trafego = si.get("etv", el.get("etv"))
        out.append({"keyword": keyword, "position": _inteiro_ou_none(pos), "searchVolume": _inteiro_ou_none(ki.get("search_volume")),
                    "traffic": trafego, "cpc": ki.get("cpc"), "keywordDifficulty": _inteiro_ou_none(dif)})
    return out


def _domain_keyword_suggestions(args):
    host, caminho, escopo, rotulo = _pesquisa(args.get("domain"), args.get("scope"))
    tarefa = {"target": host, **local(args), "limit": 100, "order_by": ["ranked_serp_element.serp_item.etv,desc"]}
    if (f := _filtro(host, caminho, escopo)):
        tarefa["filters"] = f
    return dfs("dataforseo_labs/google/ranked_keywords/live", tarefa,
               lambda itens: {"keywords": _sugestoes(itens), "target": rotulo, "scope": escopo})


# ---- palavras salvas: pasta("palavras", site)/salvas.json, lista de registros {id, keyword, location_code, language_code, salvaEm, metricas?, tags} ----

def _gravar(arq, dados):
    tmp = arq.with_name(f".{uuid.uuid4().hex}.tmp")  # nome único: gravações concorrentes não pisam no temp uma da outra
    try:
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, arq)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _trava(site):
    """Lock exclusivo do site (o servidor é multi-thread): ler-modificar-gravar salvas.json não pode se intercalar."""
    if not site:
        raise ErroUso("esta ferramenta precisa do argumento site (id do site no hub, domínio ou nome); list_projects lista os sites")
    with open(pasta("palavras", site["id"]) / "salvas.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield  # o lock cai ao fechar o arquivo


def _salvas(site, criar=True):
    if not site:
        raise ErroUso("esta ferramenta precisa do argumento site (id do site no hub, domínio ou nome); list_projects lista os sites")
    arq = (pasta("palavras", site["id"]) if criar else data() / "mcp" / "palavras" / site["id"]) / "salvas.json"  # criar=False não toca no disco
    try:
        return arq, json.loads(arq.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return arq, []
    except ValueError:  # não apaga o arquivo estragado: quem salva por cima perderia tudo sem aviso
        raise ErroUso(f"{arq.name} do site está ilegível; avise o {dono()}") from None


def mesclar_salvas(site, registros, simular=False):
    """Acrescenta registros prontos ao salvas.json do site (mesma trava e gravação atômica), pulando palavra já salva. Devolve quantos entraram."""
    with _trava(site) if not simular else contextlib.nullcontext():  # simular só lê: sem pasta, sem lock
        arq, salvas = _salvas(site, criar=not simular)
        tem = {r["keyword"] for r in salvas}
        novos = []
        for r in registros:
            r = {**r, "keyword": _normal(r["keyword"])}
            if r["keyword"] not in tem:
                tem.add(r["keyword"])
                novos.append(r)
        if novos and not simular:
            _gravar(arq, [*salvas, *novos])
        return len(novos)


def _lista_de_textos(args, chave, maximo, minimo=1, tam=None):
    v = args.get(chave)
    if v is None and minimo == 0:
        return []
    if not isinstance(v, list) or not minimo <= len(v) <= maximo or not all(isinstance(x, str) and x.strip() and (tam is None or len(x) <= tam) for x in v):
        raise ErroUso(f"{chave} tem que ser uma lista de {minimo} a {maximo} textos não vazios" + (f" de até {tam} caracteres" if tam else ""))
    return v


def _metricas(args):
    m = args.get("metrics") or []
    if not isinstance(m, list) or len(m) > 100 or not all(isinstance(x, dict) and isinstance(x.get("keyword"), str) and x["keyword"].strip() for x in m):
        raise ErroUso("metrics tem que ser uma lista de até 100 itens com keyword")
    return {_normal(x["keyword"]): {k: x[k] for k in METRICAS if k in x} for x in m}


def _save_keywords(args, site):
    arq, salvas = _salvas(site)
    palavras = _lista_de_textos(args, "keywords", 100)
    tags = _lista_de_textos(args, "tags", 20, 0, 64)
    modo = args.get("tagMode") or "append"
    if modo not in ("append", "replace"):
        raise ErroUso("tagMode tem que ser append ou replace")
    if modo == "replace" and not tags:
        raise ErroUso("tags é obrigatório quando tagMode é replace")
    metricas, loc = _metricas(args), local(args)
    if not isinstance(loc["language_code"], str) or not isinstance(loc["location_code"], int):
        raise ErroUso("locationCode tem que ser inteiro e languageCode um texto")
    por_palavra = {r["keyword"]: r for r in salvas}
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for k in palavras:
        k = _normal(k)
        r = por_palavra.get(k)
        if not r:
            r = por_palavra[k] = {"id": str(uuid.uuid4()), "keyword": k, "location_code": loc["location_code"],
                                  "language_code": loc["language_code"], "salvaEm": agora, "tags": []}
            salvas.append(r)
        if k in metricas:
            r["metricas"] = {**r.get("metricas", {}), **metricas[k]}
        r["tags"] = list(dict.fromkeys(tags if modo == "replace" else [*r.get("tags", []), *tags]))
    _gravar(arq, salvas)
    return {"ok": True, "projectId": site["id"], "savedCount": len(palavras), "keywords": palavras, "tags": tags, "tagMode": modo,
            "locationCode": loc["location_code"], "languageCode": loc["language_code"]}


def _list_saved_keywords(args, site):
    _, salvas = _salvas(site)
    busca = args.get("search")
    if busca is not None and not (isinstance(busca, str) and 1 <= len(busca) <= 200):
        raise ErroUso("search tem que ser um texto de 1 a 200 caracteres")
    filtro = {t.lower() for t in _lista_de_textos(args, "tags", 20, 0, 64)}
    limite = args.get("limit", 100)
    if not isinstance(limite, int) or isinstance(limite, bool) or limite not in (50, 100, 250):
        raise ErroUso("limit tem que ser 50, 100 ou 250")
    contagem = {}
    for r in salvas:
        for t in r.get("tags", []):
            contagem[t] = contagem.get(t, 0) + 1
    achadas = [r for r in reversed(salvas)  # mais novas primeiro, como o original (createdAt desc)
               if (not busca or busca.lower() in r["keyword"]) and (not filtro or filtro & {t.lower() for t in r.get("tags", [])})]
    rows = [{"id": r["id"], "keyword": r["keyword"], **{k: (r.get("metricas") or {}).get(k) for k in METRICAS}, "tags": r.get("tags", [])} for r in achadas[:limite]]
    return {"ok": True, "rows": rows, "totalCount": len(achadas), "tags": [{"name": n, "keywordCount": c} for n, c in sorted(contagem.items())]}


def _remove_saved_keywords(args, site):
    arq, salvas = _salvas(site)
    ids = set(_lista_de_textos(args, "savedKeywordIds", 2000, 0))
    palavras = {_normal(k) for k in _lista_de_textos(args, "keywords", 2000, 0)}
    if not ids and not palavras:
        raise ErroUso("informe savedKeywordIds (ids de list_saved_keywords) ou keywords")
    ficam = [r for r in salvas if r["id"] not in ids and r["keyword"] not in palavras]
    if len(ficam) != len(salvas):
        _gravar(arq, ficam)
    return {"ok": True, "projectId": site["id"], "requested": len(ids) + len(palavras), "deletedCount": len(salvas) - len(ficam)}


# ---- locais do Google (serp/google/locations/{pais}, grátis), em cache de 30 dias ----

TIPOS_LOCAL = {"City", "County", "Municipality", "DMA Region", "Region"}
# mais específico primeiro, como o original; CEP, aeroporto e universidade nunca são o que "Cidade, UF" quer dizer
ORDEM_TIPO = {"City": 0, "Municipality": 1, "City Region": 2, "Borough": 2, "County": 3, "State": 4, "Province": 4, "Region": 4,
              "Country": 5, "DMA Region": 6, "Neighborhood": 7, "District": 7}
# siglas de região depois do nome da cidade, por país, como no original
SIGLAS = {
    "us": dict(zip("al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc".split(),
                   "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia".split("|"))),
    "ca": dict(zip("ab bc mb nb nl ns nt nu on pe qc sk yt".split(),
                   "Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Nova Scotia|Northwest Territories|Nunavut|Ontario|Prince Edward Island|Quebec|Saskatchewan|Yukon".split("|"))),
    "au": dict(zip("nsw vic qld sa tas act".split(), ["New South Wales", "Victoria", "Queensland", "South Australia", "Tasmania", "Australian Capital Territory"])),
}
VALIDADE_LOCAIS = timedelta(days=30)


def _dobrar(t):
    """Minúsculas, sem acento, espaços colapsados."""
    return " ".join("".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c)).lower().split())


def _ranquear(consulta, locais, pais):
    siglas = SIGLAS.get(pais.lower(), {})
    tokens = [t for t in re.split(r"[\s,]+", _dobrar(consulta)) if t]
    tokens = [_dobrar(siglas[t]) if i and t in siglas else t for i, t in enumerate(tokens)]  # nunca expande o 1º token: "La Crosse" é cidade
    if not tokens:
        return []
    frase, cabeca, achados = " ".join(tokens), tokens[0], []
    for l in locais:
        nome = _dobrar(l["locationName"])
        if not all(t in nome for t in tokens):
            continue
        lugar = nome.split(",")[0].strip()
        nota = 0 if lugar == frase else 1 if lugar == cabeca else 2 if lugar.startswith(cabeca) else 3
        achados.append((nota, ORDEM_TIPO.get(l["locationType"], 9), nome, l))
    return [a[3] for a in sorted(achados, key=lambda a: a[:3])[:50]]


def _locais_do_pais(pais):
    """Lista enxuta de locais do país, do cache de 30 dias ou do DataForSEO (a resposta crua tem ~9 MB nos EUA). RuntimeError se a fonte falhar."""
    arq = pasta("palavras") / "locations.json"
    try:
        cache = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # só descarta o arquivo se não parsear; país que falta não apaga os outros
        cache = None
    if not isinstance(cache, dict):
        cache = {}
    try:
        if datetime.now(timezone.utc) - datetime.fromisoformat(cache[pais]["em"]) < VALIDADE_LOCAIS:
            return cache[pais]["itens"]
    except (ValueError, KeyError, TypeError):
        pass
    tarefas = dataforseo.get(f"serp/google/locations/{pais}")
    t = tarefas[0] if tarefas else {}
    if t.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO {t.get('status_code')}: {t.get('status_message')}")
    itens = [{"locationCode": i["location_code"], "locationName": i["location_name"], "locationType": i.get("location_type") or ""}
             for i in t.get("result") or [] if i.get("location_name") and isinstance(i.get("location_code"), int) and i.get("location_type") in TIPOS_LOCAL]
    cache[pais] = {"em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "itens": itens}
    try:
        _gravar(arq, cache)
    except OSError:  # cache é só otimização: falha ao gravar não derruba a busca
        pass
    return itens


def _search_serp_locations(args):
    q, pais = args.get("query"), args.get("countryCode")
    if not isinstance(q, str) or not 1 <= len(q) <= 100:
        raise ErroUso("query tem que ser um texto de 1 a 100 caracteres")
    if not isinstance(pais, str) or not re.fullmatch(r"[a-zA-Z]{2}", pais):
        raise ErroUso("countryCode tem que ter 2 letras (código ISO do país, ex.: br)")
    pais = pais.lower()
    try:
        locais = _locais_do_pais(pais)
    except RuntimeError as e:
        return {"ok": False, "reason": "dataforseo_erro", "message": str(e)}
    except (OSError, KeyError, ValueError):
        return {"ok": False, "reason": "dataforseo_erro", "message": f"chave do DataForSEO não configurada no hub; avise o {dono()}"}
    return {"ok": True, "locations": _ranquear(q, locais, pais)}


def executar(nome, args, site):
    if nome == "search_serp_locations":
        return _search_serp_locations(args)
    if nome == "list_saved_keywords":  # leitura: o os.replace é atômico, não precisa de lock
        return _list_saved_keywords(args, site)
    if nome in ("save_keywords", "remove_saved_keywords"):
        with _trava(site):
            return (_save_keywords if nome == "save_keywords" else _remove_saved_keywords)(args, site)
    return {"research_keywords": _research_keywords, "get_domain_keyword_suggestions": _domain_keyword_suggestions}[nome](args)
