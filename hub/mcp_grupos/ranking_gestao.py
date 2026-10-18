"""Gestão do ranking pelo MCP: criar, palavras (pôr, tirar, fixar), estimar e medir agora.

Um rastreador por site (trackerId = id do site). A configuração editada fica em $HUB_DATA/mcp/ranking/<site>/config.json,
que vale no lugar do cadastro (hub.ranking.config) para a medição semanal e para estas ferramentas.
"""
import json
import math
import os
import re
import threading

from hub import citacao, dataforseo, ranking
from hub.mcp_grupos import ErroUso, pasta, padrao

CARO = " Custa crédito do DataForSEO."
SITE = {"site": {"type": "string", "description": "id do site no hub (ex.: psite13), domínio ou nome"},
        "projectId": {"type": "string", "description": "id do site no hub (o mesmo que site); list_projects lista"}}
TRACKER = {"trackerId": {"type": "string", "description": "id do rastreador (é o id do site; opcional)"}}
IDS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2000,
       "description": "palavras do rastreador (get_rank_tracker devolve a palavra)"}
TRAVA = threading.Lock()  # o servidor é multithread: ler-mudar-gravar do config.json um por vez
CHAVES = ("palavras", "fixadas", "local", "idioma", "dispositivo", "profundidade")
# Preço da API live do DataForSEO, como em shared/rank-tracking.ts do original, sem a margem de revenda (o hub paga direto).
OPERADOR = re.compile(r"(allinanchor|allintext|allintitle|allinurl|cache|define|definition|filetype|id|inanchor|info|intext|intitle|inurl|link|site):", re.I)


def _f(descricao, required=(), **props):
    return {"description": descricao, "inputSchema": {"type": "object", "properties": {**SITE, **props}, **({"required": list(required)} if required else {})}}


FERRAMENTAS = {
    "create_rank_tracker": _f(
        "Cria (ou ajusta) o rastreador de posições do site: local, idioma, dispositivo e profundidade. Não mede nem gasta crédito; "
        "as palavras atuais continuam. A medição do hub é semanal (agenda fixa).",
        domain={"type": "string", "minLength": 1, "maxLength": 253, "description": "domínio do site ou um host alternativo (padrão: o domínio do site)"},
        locationCode={"type": "integer", "minimum": 1, "description": "código de local do DataForSEO (padrão: o atual do site)"},
        languageCode={"type": "string", "description": "código de idioma (padrão: o atual do site)"},
        locationName={"type": "string", "minLength": 1, "maxLength": 200, "description": "não aceito no hub: use locationCode"},
        devices={"type": "string", "enum": ["desktop", "mobile", "both"], "description": "dispositivo (padrão mobile; both não vale)"},
        serpDepth={"type": "integer", "minimum": 10, "maximum": 100, "multipleOf": 10, "description": "resultados olhados (padrão 40)"},
        scheduleInterval={"type": "string", "enum": ["manual", "daily", "weekly", "monthly"], "description": "aceito, mas a agenda do hub é semanal"},
        scheduleTime={"type": "object", "description": "aceito, mas a agenda do hub é fixa", "required": ["hour", "minute"], "properties": {
            "weekday": {"type": "integer", "minimum": 0, "maximum": 6, "description": "dia da semana, 0 = domingo"},
            "hour": {"type": "integer", "minimum": 0, "maximum": 23}, "minute": {"type": "integer", "minimum": 0, "maximum": 59},
            "timeZone": {"type": "string", "description": "fuso IANA (ex.: America/Sao_Paulo)"}}}),
    "add_rank_tracking_keywords": _f(
        "Põe palavras no rastreador (as repetidas e as que já estão são puladas). Não mede agora; a medição semanal passa a "
        "incluí-las. Use estimate_rank_tracker_cost com additionalKeywords antes e passe o aprovado em maxEstimatedScheduledCheckCredits.",
        required=("keywords",), **TRACKER,
        keywords={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "minItems": 1, "maxItems": 2000,
                  "description": "palavras a acompanhar"},
        matchCase={"type": "boolean", "description": "guarda como digitado em vez de minúsculas (padrão false)"},
        maxEstimatedScheduledCheckCredits={"type": "integer", "minimum": 1, "description": "créditos aprovados por medição semanal; acima disso, recusa"}),
    "remove_rank_tracking_keywords": _f(
        "Tira palavras do rastreador (e das fixadas). Não gasta crédito e mantém o histórico. Inexistentes e repetidas são ignoradas.",
        required=("keywordIds",), **TRACKER, keywordIds={**IDS, "description": "palavras a tirar (como get_rank_tracker devolve)"}),
    "pin_rank_tracking_keywords": _f(
        "Fixa ou solta palavras do rastreador; fixadas vêm primeiro no get_rank_tracker. Não gasta crédito. Inexistentes e repetidas são ignoradas.",
        required=("keywordIds", "pinned"), **TRACKER, keywordIds={**IDS, "description": "palavras a fixar ou soltar"},
        pinned={"type": "boolean", "description": "true fixa no topo, false solta"}),
    "estimate_rank_tracker_cost": _f(
        "Estima o custo de uma medição (run_rank_tracker) e da semanal, sem gastar crédito. Passe additionalKeywords para ver o custo "
        "depois de pôr palavras; palavras com operador (ex.: site:) custam 5×.",
        **TRACKER,
        additionalKeywordCount={"type": "integer", "minimum": 0, "maximum": 1000, "description": "quantas palavras simples a mais"},
        additionalKeywords={"type": "array", "items": {"type": "string"}, "maxItems": 1000, "description": "palavras a mais (vale mais que additionalKeywordCount)"}),
    "run_rank_tracker": _f(
        "Mede agora a posição de todas as palavras do rastreador (até 100). Chame estimate_rank_tracker_cost, mostre ao usuário e passe "
        "o aprovado em maxCostCredits; estimativa acima disso é recusada. A agenda semanal não muda." + CARO,
        required=("maxCostCredits",), **TRACKER,
        maxCostCredits={"type": "integer", "minimum": 1, "description": "créditos aprovados depois do estimate_rank_tracker_cost"}),
}


def preco(palavras, profundidade):
    """(costUsd, costCredits) da API live: uma chamada por palavra, crédito arredondado para cima por chamada."""
    usd = [(5 if OPERADOR.search(p) else 1) * (0.002 + (profundidade / 10 - 1) * 0.0015) for p in palavras]
    return round(sum(usd), 5), sum(math.ceil(round(u, 5) * 1000) for u in usd)


def _semanal(palavras, profundidade):
    usd, cred = preco(palavras, profundidade)
    return {"scheduleInterval": "weekly", "costUsd": usd, "costCredits": cred, "checksPerMonth": 4,
            "monthlyCostUsd": round(usd * 4, 5), "monthlyCostCredits": cred * 4}


def _gravar(site, cfg):
    arq = pasta("ranking", site["id"]) / "config.json"  # o mesmo de ranking.arquivo_config
    tmp = arq.with_name(f"config.json.{os.getpid()}.{threading.get_ident()}.tmp")  # a semanal nunca lê arquivo pela metade
    tmp.write_text(json.dumps({k: cfg[k] for k in CHAVES}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, arq)


def _config(site):
    cfg = ranking.config(site)
    if not cfg:
        raise ErroUso("o site não tem rastreador; crie com create_rank_tracker")
    return cfg


def _lista(args, chave, maximo, minimo=1):
    v = args.get(chave)
    if not isinstance(v, list) or not minimo <= len(v) <= maximo or not all(isinstance(x, str) for x in v):
        raise ErroUso(f"{chave} tem que ser uma lista de {minimo} a {maximo} textos")
    return v


def _int(args, chave, minimo, maximo, padrao_=None):
    v = args.get(chave, padrao_)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not minimo <= v <= maximo:
        raise ErroUso(f"{chave} tem que ser um inteiro de {minimo} a {maximo}")
    return v


def _bool(args, chave, padrao_=None):
    v = args.get(chave, padrao_)
    if not isinstance(v, bool):
        raise ErroUso(f"{chave} tem que ser true ou false")
    return v


def _criar(args, site):
    if args.get("locationName"):
        raise ErroUso("use locationCode (código de local do DataForSEO), não locationName")
    devices = args.get("devices") or "mobile"
    if devices not in ("desktop", "mobile", "both"):
        raise ErroUso("devices tem que ser desktop, mobile ou both")
    if devices == "both":
        raise ErroUso("o hub mede um dispositivo por site: escolha desktop ou mobile")
    prof = _int(args, "serpDepth", 10, 100, 40)
    if prof % 10:
        raise ErroUso("serpDepth tem que ser múltiplo de 10")
    if args.get("scheduleInterval") not in (None, "manual", "daily", "weekly", "monthly"):
        raise ErroUso("scheduleInterval tem que ser manual, daily, weekly ou monthly")
    meus = {h.removeprefix("www.") for h in (site["dominio"], *site["hosts_alternativos"])}
    dominio = citacao.host(str(args["domain"])).removeprefix("www.") if args.get("domain") else site["dominio"]
    if dominio not in meus:
        raise ErroUso(f"domain tem que ser um domínio do site: {', '.join(sorted(meus))}")
    atual = ranking.config(site) or {}
    loc, lang = padrao()
    cfg = {"palavras": atual.get("palavras") or [], "fixadas": atual.get("fixadas") or [],
           "local": _int(args, "locationCode", 1, 10**9) or atual.get("local") or loc,
           "idioma": args.get("languageCode") or atual.get("idioma") or lang, "dispositivo": devices, "profundidade": prof}
    if not isinstance(cfg["idioma"], str):
        raise ErroUso("languageCode tem que ser texto (ex.: pt)")
    _gravar(site, cfg)
    return {"ok": True, "trackerId": site["id"], "config": {
        "domain": dominio, "locationCode": cfg["local"], "languageCode": cfg["idioma"], "devices": devices, "serpDepth": prof,
        "scheduleInterval": "weekly", "palavras": cfg["palavras"], "fixadas": cfg["fixadas"]}}


def _rodar(args, site, cfg):
    teto = _int(args, "maxCostCredits", 1, 10**9)
    if teto is None:
        raise ErroUso("informe maxCostCredits (o aprovado depois do estimate_rank_tracker_cost)")
    if not cfg["palavras"]:
        raise ErroUso("o rastreador não tem palavras; use add_rank_tracking_keywords")
    if len(cfg["palavras"]) > 100:
        raise ErroUso("mais de 100 palavras: a medição avulsa aceita até 100; a semanal mede todas")
    cred = preco(cfg["palavras"], cfg["profundidade"])[1]
    if cred > teto:
        raise ErroUso(f"A medição custa {cred} créditos, acima do máximo aprovado de {teto}. Chame estimate_rank_tracker_cost "
                      "de novo e peça ao usuário para aprovar o valor atualizado.")
    custos = []

    def post(caminho, tarefas):
        t = dataforseo.post(caminho, tarefas)
        custos.extend(x.get("cost") or 0 for x in t)
        return t

    doc = ranking.medir(site, post=post, pasta=pasta("ranking", site["id"]) / "medicoes")
    r = {"trackerId": site["id"], "runId": doc["dia"], "dia": doc["dia"], "itens": doc["itens"], "custo": round(sum(custos), 6)}
    if all("erro" in i for i in doc["itens"]):  # nada medido: ranking.medir não gravou
        return {"ok": False, "reason": "dataforseo_erro", "started": False, **r}
    return {"ok": True, "started": True, **r}


def executar(nome, args, site):
    if nome in ("estimate_rank_tracker_cost", "run_rank_tracker"):  # só leem: run não segura a trava durante a API
        return _executar(nome, args, site)
    with TRAVA:
        return _executar(nome, args, site)


def _executar(nome, args, site):
    if not site:
        raise ErroUso("informe site ou projectId; list_projects lista os sites")
    if args.get("trackerId") not in (None, "", site["id"]):
        raise ErroUso(f"trackerId não é deste site: o rastreador de {site['id']} tem trackerId {site['id']!r}")
    if nome == "create_rank_tracker":
        return _criar(args, site)
    cfg, tid = _config(site), site["id"]
    if nome == "estimate_rank_tracker_cost":
        extra = (_lista(args, "additionalKeywords", 1000, 0) if args.get("additionalKeywords") is not None
                 else [""] * (_int(args, "additionalKeywordCount", 0, 1000) or 0))  # contagem sem texto: palavras simples
        todas = cfg["palavras"] + extra
        usd, cred = preco(todas, cfg["profundidade"])
        return {"ok": True, "trackerId": tid, "costUsd": usd, "costCredits": cred, "keywordCount": len(todas), "devicesCount": 1,
                "totalChecks": len(todas), "method": "live", "existingKeywordCount": len(cfg["palavras"]),
                "additionalKeywordCount": len(extra), "scheduledEstimate": _semanal(todas, cfg["profundidade"])}
    if nome == "run_rank_tracker":
        return _rodar(args, site, cfg)
    if nome == "add_rank_tracking_keywords":
        pedidas = _lista(args, "keywords", 2000)
        caso = _bool(args, "matchCase", False)
        teto = _int(args, "maxEstimatedScheduledCheckCredits", 1, 10**9)
        if any(not p.strip() or len(p) > 200 for p in pedidas):
            raise ErroUso("cada palavra tem que ter de 1 a 200 caracteres")
        novas = list(dict.fromkeys(p.strip() if caso else p.strip().lower() for p in pedidas))
        novas = [p for p in novas if p not in cfg["palavras"]]
        if len(cfg["palavras"]) + len(novas) > 1000:
            raise ErroUso(f"no máximo 1000 palavras por rastreador; hoje são {len(cfg['palavras'])}")
        est = _semanal(cfg["palavras"] + novas, cfg["profundidade"])
        if teto is not None and est["costCredits"] > teto:
            raise ErroUso(f"Com estas palavras cada medição semanal custaria {est['costCredits']} créditos (~US$ {est['costUsd']:.4f}; "
                          f"~{est['monthlyCostCredits']} créditos/mês), acima do aprovado de {teto}. Chame estimate_rank_tracker_cost "
                          "com additionalKeywords, mostre ao usuário e repita com maxEstimatedScheduledCheckCredits aprovado.")
        _gravar(site, {**cfg, "palavras": cfg["palavras"] + novas})
        return {"ok": True, "trackerId": tid, "requested": len(pedidas), "added": len(novas), "addedIds": novas, "scheduledEstimate": est}
    ids = list(dict.fromkeys(_lista(args, "keywordIds", 2000)))
    if nome == "remove_rank_tracking_keywords":
        fora = [p for p in ids if p in cfg["palavras"]]
        _gravar(site, {**cfg, "palavras": [p for p in cfg["palavras"] if p not in fora], "fixadas": [p for p in cfg["fixadas"] if p not in fora]})
        return {"ok": True, "trackerId": tid, "requested": len(args["keywordIds"]), "removed": len(fora), "removedIds": fora}
    fixar = _bool(args, "pinned")
    mudam = [p for p in ids if p in cfg["palavras"] and (p in cfg["fixadas"]) != fixar]
    fixadas = cfg["fixadas"] + mudam if fixar else [p for p in cfg["fixadas"] if p not in mudam]
    _gravar(site, {**cfg, "fixadas": fixadas})
    return {"ok": True, "trackerId": tid, "pinned": fixar, "requested": len(args["keywordIds"]), "updated": len(mudam), "updatedIds": mudam}
