"""Leitura dos snapshots da coleta (`$HUB_DATA/snapshots/<data>/<site>-<tipo>.json`) pelos apps, com um token por site.

    GET /api/snapshots/<site>/<data|latest>/<tipo>      Authorization: Bearer <token do site>

    GET /api/google/<site>/gsc?de=AAAA-MM-DD&ate=AAAA-MM-DD[&dimensoes=query,page][&limite=1000]
    GET /api/google/<site>/ga4?de=...&ate=...[&dimensoes=pagePath][&metricas=sessions,activeUsers][&limite=1000]
                                                        mesmo token; resposta crua do Google para o período escolhido
    GET /api/google/<site>/<gsc|ga4|ga4-agora>?corpo=<json>
                                                        o corpo que o app montaria para o Google (searchAnalytics/query, runReport,
                                                        runRealtimeReport), só com campos de leitura; vai ao Google como está

A propriedade (GSC/GA4) vem do cadastro do site: o token nunca escolhe outra. Só leitura; devolve os bytes do arquivo como estão (mesma forma que `collect.mjs` grava). O token só abre o próprio site.
O cofre guarda só o hash (sha256) de cada token, em `$HUB_DATA/mcp/snapshots/tokens.json`; o valor aparece uma vez, na emissão:

    python3 -m hub.snapshots_api emitir <site>     (imprime o token; gira o anterior)
    python3 -m hub.snapshots_api revogar <site>
"""
import hashlib
import contextlib
import fcntl
import hmac
import json
import os
import re
import secrets
import sys
import datetime as dt
import urllib.error

from hub import data
from hub import mcp_grupos

TIPOS = ("gsc", "ga4", "ga4-dia", "ga4-eventos", "clarity", "status")
_SITE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _arquivo_tokens():
    return mcp_grupos.pasta("snapshots") / "tokens.json"


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _hashes():
    try:
        d = json.loads(_arquivo_tokens().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _trava():
    """Lock exclusivo do cofre: emitir/revogar leem e gravam sob ele, para execuções simultâneas não se sobrescreverem."""
    arq = _arquivo_tokens()
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq.with_name(arq.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _gravar(d):
    arq = _arquivo_tokens()
    tmp = arq.with_name(f"{arq.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(arq)


def emitir(site):
    if not _SITE.match(site):
        raise ValueError("site inválido")
    token = "snap_" + secrets.token_urlsafe(32)
    with _trava():
        d = _hashes()
        d[site] = _hash(token)
        _gravar(d)
    return token


def revogar(site):
    with _trava():
        d = _hashes()
        existia = d.pop(site, None) is not None
        _gravar(d)
    return existia


def autorizado(token, site):
    """True só se o token é o do `site` (comparação em tempo constante; site sem token emitido nunca abre)."""
    esperado = _hashes().get(site)
    if not token or not esperado:
        return False
    return hmac.compare_digest(_hash(token), esperado)


def token_do_cabecalho(valor):
    partes = (valor or "").split(None, 1)
    return partes[1].strip() if len(partes) == 2 and partes[0].lower() == "bearer" else ""


def responder(caminho, authorization):
    """(status, bytes, tipo) para `/api/snapshots/<site>/<data|latest>/<tipo>`. 401 sem token do site; 404 sem arquivo."""
    partes = caminho.strip("/").split("/")[2:]
    if len(partes) != 3:
        return 404, b"nao encontrado", "text/plain"
    site, dia, tipo = partes
    if not _SITE.match(site) or tipo not in TIPOS or not (dia == "latest" or _DATA.match(dia)):
        return 404, b"nao encontrado", "text/plain"
    if not autorizado(token_do_cabecalho(authorization), site):
        return 401, b"token invalido para este site", "text/plain"
    raiz = data() / "snapshots"
    if dia == "latest":
        arqs = sorted(raiz.glob(f"*/{site}-{tipo}.json"), key=lambda a: a.parent.name)
        arqs = [a for a in arqs if _DATA.match(a.parent.name)]
        alvo = arqs[-1] if arqs else None
    else:
        alvo = raiz / dia / f"{site}-{tipo}.json"
    try:
        corpo = alvo.read_bytes() if alvo else None
    except OSError:
        corpo = None
    if corpo is None:
        return 404, b"sem snapshot", "text/plain"
    return 200, corpo, "application/json"


_NOME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_GSC_DIMENSOES = ("query", "page", "country", "device", "date", "searchAppearance")
_MAX_DIAS = 500


def _lista(valor, padrao):
    return [x.strip() for x in valor.split(",") if x.strip()] if valor is not None else list(padrao)


def _periodo(q):
    try:
        de, ate = dt.date.fromisoformat(q["de"][0]), dt.date.fromisoformat(q["ate"][0])
    except (KeyError, ValueError):
        return None
    if not (_DATA.match(q["de"][0]) and _DATA.match(q["ate"][0])) or de > ate or (ate - de).days >= _MAX_DIAS:
        return None
    return de.isoformat(), ate.isoformat()


_MAX_CORPO = 8000
_GSC_ENUMS = {"dataState": ("final", "all", "hourly_all"), "aggregationType": ("auto", "byPage", "byProperty"),
              "type": ("web", "image", "video", "news", "discover", "googleNews"),
              "searchType": ("web", "image", "video", "news", "discover", "googleNews")}
_GA4_LIVRES = ("orderBys", "dimensionFilter", "metricFilter", "keepEmptyRows", "metricAggregations")


def _faixa_ok(f):
    """{startDate, endDate[, name]} com as datas na regra de `_periodo` (AAAA-MM-DD, início <= fim, até 500 dias)."""
    return isinstance(f, dict) and set(f) <= {"startDate", "endDate", "name"} \
        and all(isinstance(f.get(k), str) for k in ("startDate", "endDate")) \
        and _periodo({"de": [f["startDate"]], "ate": [f["endDate"]]}) is not None


def _inteiro(v, minimo, maximo):
    """Inteiro na faixa; o GA4 (int64 no JSON do proto) também aceita o número como string de dígitos."""
    if isinstance(v, str) and re.fullmatch(r"\d{1,9}", v):
        v = int(v)
    return isinstance(v, int) and not isinstance(v, bool) and minimo <= v <= maximo


def _nomes(lista, minimo, maximo):
    return isinstance(lista, list) and minimo <= len(lista) <= maximo \
        and all(isinstance(x, dict) and set(x) == {"name"} and isinstance(x["name"], str) and _NOME.match(x["name"]) for x in lista)


def _corpo_valido(tipo, c):
    """O corpo que o app montou para o Google, só com os campos de leitura permitidos (a propriedade e a URL são do hub).
    gsc: searchAnalytics/query; ga4: runReport; ga4-agora: runRealtimeReport. Campo desconhecido reprova."""
    if not isinstance(c, dict):
        return False
    if tipo == "gsc":
        permitidos = {"startDate", "endDate", "dimensions", "rowLimit", "startRow", "dimensionFilterGroups", *_GSC_ENUMS}
        dims = c.get("dimensions", [])
        return set(c) <= permitidos and _faixa_ok({"startDate": c.get("startDate"), "endDate": c.get("endDate")}) \
            and isinstance(dims, list) and len(dims) <= 6 and all(d in _GSC_DIMENSOES for d in dims) \
            and _inteiro(c.get("rowLimit", 1), 1, 25000) and _inteiro(c.get("startRow", 0), 0, 1_000_000) \
            and isinstance(c.get("dimensionFilterGroups", []), list) and len(c.get("dimensionFilterGroups", [])) <= 5 \
            and all(c[k] in v for k, v in _GSC_ENUMS.items() if k in c)
    comuns = {"dimensions", "metrics", "limit", *_GA4_LIVRES}
    if tipo == "ga4":
        faixas = c.get("dateRanges")
        if not (set(c) <= comuns | {"dateRanges", "offset"} and isinstance(faixas, list) and 1 <= len(faixas) <= 4
                and all(_faixa_ok(f) for f in faixas) and _inteiro(c.get("offset", 0), 0, 1_000_000)):
            return False
    elif not set(c) <= comuns | {"minuteRanges"} or not isinstance(c.get("minuteRanges", []), list):
        return False
    return _nomes(c.get("dimensions", []), 0, 9) and _nomes(c.get("metrics"), 1, 10) and _inteiro(c.get("limit", 1), 1, 100000) \
        and all(isinstance(c[k], (list, dict, bool)) for k in _GA4_LIVRES if k in c)


def _responder_corpo(site, tipo, bruto):
    """`?corpo=<json>`: o corpo do app vai ao Google como está (mesmos números que o app tinha chamando o Google direto)."""
    from hub import google, sites as S
    try:
        corpo = json.loads(bruto) if len(bruto) <= _MAX_CORPO else None
    except ValueError:
        corpo = None
    if not _corpo_valido(tipo, corpo):
        return 400, b"corpo fora da regra (campos de leitura, datas AAAA-MM-DD ate 500 dias, limites)", "text/plain"
    try:
        prop = (S.site(site).get("ids") or {}).get("gsc" if tipo == "gsc" else "ga4")
    except S.SiteInvalido:
        prop = None
    if not prop:
        return 404, b"site sem propriedade " + tipo.encode(), "text/plain"
    try:
        r = google.gsc_corpo_cru(prop, corpo) if tipo == "gsc" else google.ga4_corpo_cru(prop, corpo, tempo_real=tipo == "ga4-agora")
    except (urllib.error.URLError, google.ErroCredencial, google.SemAcesso, google._ErroGa4, OSError, ValueError) as e:
        return 502, ("google recusou: " + type(e).__name__).encode(), "text/plain"
    return 200, json.dumps(r, indent=2).encode(), "application/json"


def responder_google(caminho, query, authorization):
    """(status, bytes, tipo) para `/api/google/<site>/<gsc|ga4|ga4-agora>?de&ate&...` ou `?corpo=<json>`: a consulta crua do Google.
    401 sem o token do site; 400 parâmetro fora da regra; 404 site sem a propriedade; 502 Google recusou."""
    from hub import google, sites as S  # tardio: o listener de snapshots pura leitura não carrega a conta de serviço à toa
    partes = caminho.strip("/").split("/")[2:]
    if len(partes) != 2 or not _SITE.match(partes[0]) or partes[1] not in ("gsc", "ga4", "ga4-agora"):
        return 404, b"nao encontrado", "text/plain"
    site, tipo = partes
    if not autorizado(token_do_cabecalho(authorization), site):
        return 401, b"token invalido para este site", "text/plain"
    q = {k: v for k, v in query.items()}
    if "corpo" in q:
        return _responder_corpo(site, tipo, q["corpo"][0])
    if tipo == "ga4-agora":
        return 400, b"ga4-agora pede corpo", "text/plain"
    per = _periodo(q)
    if per is None:
        return 400, b"de e ate (AAAA-MM-DD, de <= ate, ate 500 dias) sao obrigatorios", "text/plain"
    try:
        limite = int((q.get("limite") or ["1000"])[0])
    except ValueError:
        limite = 0
    dims = _lista((q.get("dimensoes") or [None])[0], ())
    if not 1 <= limite <= 25000 or len(dims) > 9 or not all(_NOME.match(d) for d in dims) \
            or (tipo == "gsc" and not set(dims) <= set(_GSC_DIMENSOES)):
        return 400, b"dimensoes ou limite invalidos", "text/plain"
    try:
        prop = (S.site(site).get("ids") or {}).get(tipo)
    except S.SiteInvalido:
        prop = None
    if not prop:
        return 404, b"site sem propriedade " + tipo.encode(), "text/plain"
    try:
        if tipo == "gsc":
            r = google.gsc_consultas_cru(prop, dimensoes=dims, limite=limite, inicio=per[0], fim=per[1])
        else:
            mets = _lista((q.get("metricas") or [None])[0], ("sessions",))
            if not mets or len(mets) > 10 or not all(_NOME.match(m) for m in mets):
                return 400, b"metricas invalidas", "text/plain"
            r = google.ga4_relatorio_cru(prop, dimensoes=dims, metricas=mets, limite=limite, inicio=per[0], fim=per[1])
    except (urllib.error.URLError, google.ErroCredencial, google.SemAcesso, google._ErroGa4, OSError, ValueError) as e:
        return 502, ("google recusou: " + type(e).__name__).encode(), "text/plain"
    return 200, json.dumps(r, indent=2).encode(), "application/json"


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    if len(a) == 2 and a[0] == "emitir":
        print(emitir(a[1]))
        return 0
    if len(a) == 2 and a[0] == "revogar":
        print("revogado" if revogar(a[1]) else "sem token")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
