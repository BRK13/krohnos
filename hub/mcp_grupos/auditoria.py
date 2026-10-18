"""Auditoria do site sob demanda. Cada auditoria é uma pasta pasta("auditoria", <site>)/<id>/ (id = hora UTC AAAAMMDDTHHMMSS):
    status.json  {id, site, origem, status: rodando|pronta|falhou, inicio, fim, pid, erro, ...args do pedido}
    latest.json, latest.md, <data>.json, <data>.md   saída do verificar_site.py --saida (mesmo formato do cron)
    saida.log    o que o verificador imprimiu
    issues.json, pages.json  só nas importadas (scripts/importar_auditorias_antigas.py): linhas já nos campos do original
O Popen roda este módulo (python3 -m ...): ele chama o verificador e grava o status final depois que o pedido MCP já respondeu.
A auditoria diária do cron ($HUB_DATA/verificador/<site>/latest.json) entra como id "diaria": lê como as outras, não se apaga.

Mapa da saída do verificador (itens[{n, item, ok, falhas, provas}]) para os campos do original:
    problema: cada falha de item com ok False (ou o item, se não listou falhas) -> severity "warning" (o verificador não gradua),
        issueType "check-<n>", title = item, url = URL que abre a falha (senão https://<dominio>/), details {"falha": texto},
        howToFix null (o verificador não tem).
    página: cada URL que abre uma prova ou falha -> url, statusCode (o número depois de "→", senão null), fetchClass "ok";
        os outros campos do original (title, wordCount, isIndexable, crawlDepth, contagem de links...) ele não mede: null.
"""
import shutil
import datetime as dt
import json
import os
import subprocess
import sys

from hub import RAIZ, data
from hub.mcp_grupos import ErroUso, pasta

_ID = {"type": "string", "description": "Audit ID. If omitted, uses the project's most recent audit."}
_PROJ = {"type": "string", "description": "Projeto (id do site)."}
_LIM = {"type": "integer", "minimum": 1, "maximum": 1000}
DIARIA = "diaria"
_ORDEM = {"critical": 0, "warning": 1, "info": 2}
_FETCH = ["ok", "blocked", "rate_limited", "error"]
_NULOS = ("title", "metaDescription", "wordCount", "isIndexable", "crawlDepth", "internalLinkCount", "externalLinkCount")
FERRAMENTAS = {
    "run_site_audit": {
        "description": "Start a site audit of the site (SEO standard checks: robots, sitemap, canonical, noindex, JSON-LD, metadata, llms.txt, IndexNow). Runs in the background: poll get_audit_status, then read get_audit_issues. One audit per site at a time: if one is running, its id is returned.",
        "inputSchema": {"type": "object", "properties": {
            "projectId": _PROJ, "site": _PROJ,
            "url": {"type": "string", "minLength": 1, "maxLength": 2048, "description": "Start URL to crawl."},
            "maxPages": {"type": "integer", "minimum": 10, "maximum": 10000, "description": "Page budget for the crawl (default 50)."},
            "renderJavaScript": {"type": "boolean", "description": "Render JavaScript before auditing. Default false."},
            "runLighthouse": {"type": "boolean", "description": "Run Lighthouse on a sample of up to 10 representative pages (default false)."}},
            "required": []}},
    "get_audit_status": {
        "description": "Check the progress of a site audit (status, start and end). Free. Omit auditId for the most recent audit.",
        "inputSchema": {"type": "object", "properties": {"projectId": _PROJ, "site": _PROJ, "auditId": _ID}, "required": []}},
    "get_audit_issues": {
        "description": "Read the prioritized issue report from a site audit (summary by type + issue rows). Free. Omit auditId for the most recent audit; auditId \"diaria\" is the daily audit.",
        "inputSchema": {"type": "object", "properties": {
            "projectId": _PROJ, "site": _PROJ, "auditId": _ID,
            "severity": {"type": "string", "enum": list(_ORDEM), "description": "Only return issues of this severity."},
            "issueType": {"type": "string", "description": "Only return issues of this type. Hub audits use check-<n> (n = verifier item); imported audits keep the original types (missing-title, broken-internal-link, ...)."},
            "limit": {**_LIM, "description": "Max issues to return (default 200)."}}, "required": []}},
    "get_audit_pages": {
        "description": "List pages from a site audit with per-page SEO data (status, title, description, word count, indexability, crawl depth, link counts; null where the audit did not measure it). Free. Omit auditId for the most recent audit.",
        "inputSchema": {"type": "object", "properties": {
            "projectId": _PROJ, "site": _PROJ, "auditId": _ID,
            "fetchClass": {"type": "string", "enum": _FETCH, "description": 'Filter by fetch outcome ("blocked" = the site\'s bot protection challenged the crawler; "rate_limited" = a 429 prevented the crawler from reading the page).'},
            "statusCode": {"type": "integer", "description": "Filter by exact HTTP status code."},
            "urlContains": {"type": "string", "description": "Filter to URLs containing this substring."},
            "limit": {**_LIM, "description": "Max pages to return (default 100)."}}, "required": []}},
    "list_site_audits": {
        "description": "Lists this project's site audits with IDs, URLs, status, page counts, dates and origem (sob_demanda, importada, diaria), newest first. Uses no credits. Use this to find an older audit to read or delete; get_audit_status defaults to the newest audit only.",
        "inputSchema": {"type": "object", "properties": {"projectId": _PROJ, "site": _PROJ}, "required": []}},
    "delete_site_audit": {
        "description": "Permanently deletes one site audit (on-demand or imported) and its pages and issues. The daily audit (id \"diaria\") cannot be deleted. Uses no credits.",
        "inputSchema": {"type": "object", "properties": {
            "projectId": _PROJ, "site": _PROJ,
            "auditId": {"type": "string", "minLength": 1, "description": "Exact audit ID from list_site_audits or run_site_audit. Required; never defaults to the latest audit."}},
            "required": ["auditId"]}},
}


def _agora():
    return dt.datetime.now(dt.timezone.utc)


def _gravar(d, status):
    tmp = d / "status.json.tmp"
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "status.json")


def _json(f):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ler(d):
    return _json(d / "status.json")


def _num(args, k, lo=None, hi=None):
    if args.get(k) is None:
        return None
    try:
        v = int(args[k])
    except (TypeError, ValueError):
        raise ErroUso(f"{k} tem que ser um número") from None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ErroUso(f"{k} tem que ficar entre {lo} e {hi}")
    return v


def _viva(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: existe
        pass
    return True


def _rodando(raiz):
    for d in sorted(raiz.iterdir(), reverse=True):
        s = _ler(d)
        if s and s["status"] == "rodando":
            # o processo morreu sem fechar o status; sem pid por mais de 1 min = o serviço caiu entre gravar e disparar
            morto = not _viva(s["pid"]) if s.get("pid") else _agora() - dt.datetime.fromisoformat(s["inicio"]) > dt.timedelta(minutes=1)
            if morto:
                _gravar(d, {**s, "status": "falhou", "fim": _agora().isoformat(), "erro": "processo encerrado sem terminar"})
                continue
            return s
    return None


def _auditoria(site, args, id_):
    if not site:
        raise ErroUso("informe site (ou projectId)")
    _num(args, "maxPages", 10, 10000)
    raiz = pasta("auditoria", site["id"])
    if (s := _rodando(raiz)):
        return {"id": s["id"], "status": "rodando"}
    d = raiz / id_
    d.mkdir(exist_ok=True)
    status = {"id": id_, "site": site["id"], "origem": "sob_demanda", "status": "rodando", "inicio": _agora().isoformat(), "fim": None, "erro": None,
              **{k: args[k] for k in ("url", "maxPages", "renderJavaScript", "runLighthouse") if k in args}}
    _gravar(d, status)  # antes do Popen: o filho já encontra o status.json e o pedido concorrente vê "rodando"
    # shortcut: o verificador fixo ignora url/maxPages/renderJavaScript/runLighthouse (só guardados no status), troque se ele passar a crawlear
    try:
        p = subprocess.Popen([sys.executable, "-m", "hub.mcp_grupos.auditoria", site["id"], str(d)], cwd=RAIZ, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:  # não iniciou: fecha o status para não travar o site
        _gravar(d, {**status, "status": "falhou", "fim": _agora().isoformat(), "erro": f"verificador não iniciou: {e}"})
        raise ErroUso(f"auditoria {id_} não iniciou: {e}") from None
    _gravar(d, {**status, "pid": p.pid})
    return {"id": id_, "status": "rodando"}


def _cron(site):
    return data() / "verificador" / site["id"] / "latest.json"


def _todas(site):
    """Status de todas as auditorias do site (as da pasta e a diária), da mais nova para a mais velha."""
    _rodando(pasta("auditoria", site["id"]))  # cura "rodando" de processo morto também na leitura
    r = [{**s, "id": d.name} for d in pasta("auditoria", site["id"]).iterdir() if (s := _ler(d))]
    if (v := _json(_cron(site))):
        r.append({"id": DIARIA, "site": site["id"], "origem": "diaria", "status": "pronta", "inicio": v.get("em"), "fim": v.get("em"),
                  "erro": None, "url": f"https://{site['dominio']}/"})
    return sorted(r, key=lambda s: s.get("inicio") or "", reverse=True)


def _achar(site, id_):
    todas = _todas(site)  # id só casa com o que existe: sem caminho montado do pedido
    s = next((s for s in todas if s["id"] == id_), None) if id_ else (todas[0] if todas else None)
    if not s:
        raise ErroUso(f"auditoria {id_} não existe" if id_ else "este site ainda não tem auditoria; inicie com run_site_audit")
    return s


def _url(texto):
    p = texto.split()
    return (p[0].rstrip(":") if p and p[0].startswith("http") else None), p


def _problemas(v, site):
    return [{"severity": "warning", "issueType": f"check-{i['n']}", "title": i["item"], "url": _url(f)[0] or f"https://{site['dominio']}/",
             "details": {"falha": f}, "howToFix": None}
            for i in v.get("itens") or [] if i.get("ok") is False for f in i.get("falhas") or [i["item"]]]


def _paginas(v):
    r = {}
    for i in v.get("itens") or []:
        for t in (i.get("provas") or []) + (i.get("falhas") or []):
            u, p = _url(t)
            if u:
                pg = r.setdefault(u, {"url": u, "statusCode": None, "fetchClass": "ok", **dict.fromkeys(_NULOS)})
                if pg["statusCode"] is None and len(p) > 2 and p[1] == "→" and p[2].isdigit():
                    pg["statusCode"] = int(p[2])
    return list(r.values())


def _linhas(site, s, nome):
    """Linhas nos campos do original: issues.json/pages.json da importada, ou a saída do verificador mapeada."""
    d = None if s["id"] == DIARIA else pasta("auditoria", site["id"]) / s["id"]
    if d and (d / f"{nome}.json").exists():
        return _json(d / f"{nome}.json") or []
    v = _json(d / "latest.json" if d else _cron(site)) or {}
    return _problemas(v, site) if nome == "issues" else _paginas(v)


def _issues(site, s, args):
    sev, tipo = args.get("severity"), args.get("issueType")
    if sev and sev not in _ORDEM:
        raise ErroUso("severity tem que ser critical, warning ou info")
    limite = _num(args, "limit", 1, 1000) or 200
    rows = sorted((r for r in _linhas(site, s, "issues") if (not sev or r["severity"] == sev) and (not tipo or r["issueType"] == tipo)),
                  key=lambda r: (_ORDEM.get(r["severity"], 2), r["issueType"]))  # grave primeiro: o corte do limit tira info
    resumo = {}
    for r in rows:
        resumo.setdefault(r["issueType"], {"issueType": r["issueType"], "title": r["title"], "severity": r["severity"], "count": 0})["count"] += 1
    summary = sorted(resumo.values(), key=lambda e: (_ORDEM.get(e["severity"], 2), -e["count"]))
    return {"ok": True, "auditId": s["id"], "summary": summary, "issues": rows[:limite], "total": len(rows)}


def _pages(site, s, args):
    fc, trecho = args.get("fetchClass"), args.get("urlContains")
    if fc and fc not in _FETCH:
        raise ErroUso(f"fetchClass tem que ser um de: {', '.join(_FETCH)}")
    codigo, limite = _num(args, "statusCode"), _num(args, "limit", 1, 1000) or 100
    pages = [p for p in _linhas(site, s, "pages") if (not fc or p.get("fetchClass") == fc)
             and (codigo is None or p.get("statusCode") == codigo) and (not trecho or trecho in p["url"])]
    return {"ok": True, "auditId": s["id"], "pages": pages[:limite], "total": len(pages)}


def _apagar(site, id_):
    if not id_:
        raise ErroUso("informe auditId (veja list_site_audits); nunca apaga a mais recente por padrão")
    if id_ == DIARIA:
        raise ErroUso("a auditoria diária é do cron do verificador e é refeita todo dia: não se apaga. Só as de run_site_audit e as importadas")
    raiz = pasta("auditoria", site["id"])
    if id_ not in {d.name for d in raiz.iterdir() if d.is_dir()}:  # só nome existente: barra ../ e afins
        raise ErroUso(f"auditoria {id_} não existe")
    if (r := _rodando(raiz)) and r["id"] == id_:
        raise ErroUso(f"auditoria {id_} está rodando; espere terminar (get_audit_status) e apague depois")
    shutil.rmtree(raiz / id_)
    return {"ok": True, "auditId": id_, "deleted": True}


def executar(nome, args, site):
    if nome == "run_site_audit":
        return _auditoria(site, args, _agora().strftime("%Y%m%dT%H%M%S"))
    if not site:
        raise ErroUso("informe site (ou projectId)")
    if nome == "list_site_audits":
        return {"ok": True, "audits": [{"id": s["id"], "startUrl": s.get("url") or f"https://{site['dominio']}/", "status": s["status"],
                                        "pagesCrawled": len(_linhas(site, s, "pages")), "ranLighthouse": bool(s.get("runLighthouse")),
                                        "startedAt": s.get("inicio"), "completedAt": s.get("fim"), "origem": s.get("origem")} for s in _todas(site)]}
    if nome == "delete_site_audit":
        return _apagar(site, args.get("auditId"))
    s = _achar(site, args.get("auditId"))
    if nome in ("get_audit_issues", "get_audit_pages"):
        r = (_issues if nome == "get_audit_issues" else _pages)(site, s, args)
        return r if s["status"] == "pronta" else {**r, "status": s["status"]}  # 0 resultados sem pronta não é "limpo"
    return {"ok": True, "status": s}


def _correr(site_id, d):
    """Roda o verificador e fecha o status.json (pronta só se saiu 0 e gravou latest.json)."""
    from pathlib import Path
    d = Path(d)
    with open(d / "saida.log", "w", encoding="utf-8") as log:
        rc = subprocess.run([sys.executable, str(RAIZ / "verificar_site.py"), site_id, "--saida", str(d)], stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    ok = rc == 0 and (d / "latest.json").exists()
    _gravar(d, {**_ler(d), "status": "pronta" if ok else "falhou", "fim": _agora().isoformat(),
                "erro": None if ok else f"verificador saiu {rc}" + ("" if rc else " sem resultado (site fora do Google?)") + "; veja saida.log"})
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _correr(sys.argv[1], sys.argv[2]) else 1)
