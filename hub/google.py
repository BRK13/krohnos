"""Search Console pela conta de serviço do hub ($NIMBUS_HOME/credenciais/google-conta-servico.json).

Só leitura. Propriedade do GSC que a conta de serviço não vê (403) é tentada de novo com o OAuth do dono
(credenciais/google-oauth-fulano.json, gerado pelo deploy a partir do /etc/agente-fsite05.env), que é usuário em propriedades
de terceiros (ClienteE). Sem acesso por nenhum dos dois, devolve SemAcesso com o e-mail a autorizar.
"""
import base64
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

from hub import config, home
from hub.comum import env as comum_env

ESCOPO_GSC = "https://www.googleapis.com/auth/webmasters.readonly"
ESCOPO_GSC_ESCRITA = "https://www.googleapis.com/auth/webmasters"
ESCOPO_GA4 = "https://www.googleapis.com/auth/analytics.readonly"
OAUTH_ARQUIVO = "google-oauth-fulano.json"
OAUTH_ENV = ("GOOGLE_AGENDA_CLIENT_ID", "GOOGLE_AGENDA_CLIENT_SECRET", "GOOGLE_AGENDA_REFRESH_TOKEN")
_tokens = {}
_via_oauth = set()  # propriedades em que a conta de serviço deu 403 e o OAuth respondeu: vai direto nas próximas


class ErroCredencial(RuntimeError):
    """O Google recusou o token da conta de serviço (chave inválida/revogada): problema da credencial, não do relatório."""


class SemAcesso(RuntimeError):
    def __init__(self, propriedade, email):
        super().__init__(f"conta de serviço sem acesso a {propriedade}: dar leitura a {email}")
        self.propriedade, self.email = propriedade, email


def _credencial(nome):
    cred = Path(os.environ.get("CREDENTIALS_DIRECTORY", "")) / nome  # systemd LoadCredential (proxy)
    return cred if os.environ.get("CREDENTIALS_DIRECTORY") and cred.exists() else home() / "credenciais" / nome


def conta_servico():
    try:
        sa = json.loads(_credencial("google-conta-servico.json").read_text(encoding="utf-8"))
        sa["client_email"], sa["private_key"]
        return sa
    except (OSError, ValueError, KeyError, TypeError) as e:  # só o tipo: a mensagem original pode ter pedaço da chave
        raise ErroCredencial(f"chave da conta de serviço ilegível ({type(e).__name__})") from None


def token(escopo=ESCOPO_GSC):
    agora = int(time.time())
    if escopo in _tokens and _tokens[escopo][1] > agora + 60:
        return _tokens[escopo][0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    sa = conta_servico()
    if not (isinstance(sa, dict) and sa.get("client_email") and sa.get("private_key")):
        raise ErroCredencial("chave da conta de serviço ilegível (KeyError)")
    b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=")
    corpo = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + b64(json.dumps({
        "iss": sa["client_email"], "scope": escopo, "aud": "https://oauth2.googleapis.com/token",
        "iat": agora, "exp": agora + 3600}).encode())
    try:
        chave = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    except (ValueError, TypeError, AttributeError) as e:  # só o tipo: a mensagem pode citar a chave
        raise ErroCredencial(f"chave da conta de serviço ilegível ({type(e).__name__})") from None
    jwt = corpo + b"." + b64(chave.sign(corpo, padding.PKCS1v15(), hashes.SHA256()))
    dados = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt.decode()}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=dados), timeout=30) as r:
            tk = json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        raise ErroCredencial(f"Google recusou o token da conta de serviço (HTTP {e.code})") from None
    _tokens[escopo] = (tk, agora + 3500)
    return tk


def oauth_fulano():
    """{client_id, client_secret, refresh_token} do dono, ou None se não houver (o .service manda "{}" sem o arquivo)."""
    try:
        o = json.loads(_credencial(OAUTH_ARQUIVO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return o if isinstance(o, dict) and all(isinstance(o.get(k), str) and o[k] for k in
                                            ("client_id", "client_secret", "refresh_token")) else None


def token_oauth(escrita=False):
    """Access token do dono pelo refresh token. None sem credencial, se o Google recusar ou se o token não tiver o escopo
    webmasters: quem chama fica com o SemAcesso da conta de serviço. Recusa fica 10 min em cache (sem martelar o Google
    nem repetir o aviso). Nunca loga o token, só o código HTTP."""
    agora = int(time.time())
    if "oauth" in _tokens and _tokens["oauth"][1] > agora + 60:
        return _tokens["oauth"][0] if _tokens["oauth"][0] and (not escrita or _tokens.get("oauth_escrita")) else None
    _tokens["oauth"] = (None, agora + 600)  # vale até dar certo abaixo
    o = oauth_fulano()
    if not o:
        return None
    dados = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": o["client_id"],
                                    "client_secret": o["client_secret"], "refresh_token": o["refresh_token"]}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=dados), timeout=30) as r:
            resp = json.load(r)
        tk = resp["access_token"]
        if "webmasters" not in (resp.get("scope") or "webmasters"):
            print(f"OAuth do {config.dono()} sem o escopo webmasters: não serve para o Search Console", file=sys.stderr)
            return None
        # só webmasters.readonly não serve para enviar sitemap; sem o campo scope, assume completo (como na leitura)
        _tokens["oauth_escrita"] = ESCOPO_GSC_ESCRITA in (resp.get("scope") or ESCOPO_GSC_ESCRITA).split()
    except urllib.error.HTTPError as e:
        print(f"OAuth do {config.dono()} recusado pelo Google (HTTP {e.code})", file=sys.stderr)
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"OAuth do {config.dono()} indisponível ({type(e).__name__})", file=sys.stderr)
        return None
    _tokens["oauth"] = (tk, agora + min(int(resp.get("expires_in") or 3600), 3600) - 100)
    return tk if not escrita or _tokens["oauth_escrita"] else None


def _chamar(url, corpo, tk, metodo=None):
    req = urllib.request.Request(url, data=json.dumps(corpo).encode() if corpo is not None else None, method=metodo,
                                 headers={"Authorization": "Bearer " + tk, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read() or b"{}")


def _sem_permissao(e):
    """403 de permissão (não de cota/limite, que outra credencial não resolve)."""
    if e.code != 403:
        return False
    try:
        corpo = (e.read() or b"").decode("utf-8", "replace").lower()
    except Exception:
        corpo = ""
    return not any(x in corpo for x in ("quota", "ratelimit", "rate limit", "userratelimit"))


def _api(url, propriedade, corpo=None, escopo=ESCOPO_GSC, metodo=None):
    if escopo == ESCOPO_GSC and propriedade in _via_oauth and (tk := token_oauth()):
        try:
            return _chamar(url, corpo, tk, metodo)
        except urllib.error.HTTPError as e:
            if not _sem_permissao(e):
                raise
            _via_oauth.discard(propriedade)  # o dono perdeu o acesso: volta a tentar a conta de serviço primeiro
    try:
        return _chamar(url, corpo, token(escopo), metodo)
    except urllib.error.HTTPError as e:
        if not _sem_permissao(e):
            raise
    if escopo in (ESCOPO_GSC, ESCOPO_GSC_ESCRITA) and (tk := token_oauth(escopo == ESCOPO_GSC_ESCRITA)):  # só GSC
        try:
            r = _chamar(url, corpo, tk, metodo)
            if escopo == ESCOPO_GSC:  # o atalho do OAuth é só de leitura
                _via_oauth.add(propriedade)
            return r
        except urllib.error.HTTPError as e:
            if not _sem_permissao(e):
                raise
            print(f"OAuth do {config.dono()} também sem acesso a {propriedade}", file=sys.stderr)
    raise SemAcesso(propriedade, conta_servico()["client_email"])


def _ler_env(caminho):
    """KEY=valor de um arquivo de ambiente (aceita `export ` e aspas), sem executar nada."""
    return comum_env.ler_env(caminho, estrito=True)


def gravar_oauth(env, destino):
    """Deploy (root): copia o OAuth do dono do env do agente para o arquivo de credencial, 600 e com o mesmo dono da
    chave da conta de serviço ao lado. Só regrava se mudou. Sem o OAuth no env (ou sem o env), apaga a cópia: tirar do env
    desliga o acesso. Devolve "gravado", "igual", "removido" ou "sem-oauth"; nunca imprime o token."""
    try:
        v = _ler_env(env)
    except FileNotFoundError:
        v = {}
    if not all(v.get(k) for k in OAUTH_ENV):
        if Path(destino).exists():
            Path(destino).unlink()
            return "removido"
        return "sem-oauth"
    novo = json.dumps(dict(zip(("client_id", "client_secret", "refresh_token"), (v[k] for k in OAUTH_ENV))), indent=1) + "\n"
    destino = Path(destino)
    try:
        if destino.read_text(encoding="utf-8") == novo:
            return "igual"
    except OSError:
        pass
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(novo)
    vizinho = destino.parent / "google-conta-servico.json"
    if vizinho.exists() and os.geteuid() == 0:
        st = vizinho.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, 0o600)
    os.replace(tmp, destino)
    return "gravado"


def _base(propriedade):
    return "https://searchconsole.googleapis.com/webmasters/v3/sites/" + urllib.parse.quote(propriedade, safe="")


def periodo(dias, atraso=3, hoje=None):
    """Os últimos `dias` que o GSC já fechou (os dados chegam com ~2-3 dias de atraso)."""
    fim = (hoje or dt.date.today()) - dt.timedelta(days=atraso)
    return (fim - dt.timedelta(days=dias - 1)).isoformat(), fim.isoformat()


def consultas(propriedade, dias=28, dimensoes=("query", "page"), limite=25000):
    """Linhas query × página do período, paginando até `limite`."""
    inicio, fim = periodo(dias)
    linhas, inicio_linha = [], 0
    while len(linhas) < limite:
        lote = min(25000, limite - len(linhas))
        r = _api(_base(propriedade) + "/searchAnalytics/query", propriedade,
                 {"startDate": inicio, "endDate": fim, "dimensions": list(dimensoes), "rowLimit": lote,
                  "startRow": inicio_linha, "type": "web", "dataState": "final"})
        novas = r.get("rows", [])
        linhas += novas
        if len(novas) < lote:
            break
        inicio_linha += lote
    return {"inicio": inicio, "fim": fim, "linhas": linhas}


def sitemaps(propriedade):
    return _api(_base(propriedade) + "/sitemaps", propriedade).get("sitemap", [])


def enviar_sitemap(propriedade, url):
    """Reenvia o sitemap ao Search Console (PUT, resposta vazia)."""
    _api(_base(propriedade) + "/sitemaps/" + urllib.parse.quote(url, safe=""), propriedade, escopo=ESCOPO_GSC_ESCRITA, metodo="PUT")


def propriedades():
    req = urllib.request.Request("https://searchconsole.googleapis.com/webmasters/v3/sites",
                                 headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("siteEntry", [])


# ---- Formato das ferramentas do OpenSEO (structuredContent idêntico; o proxy MCP responde por elas) ----
# Fonte: open-seo/src/server/{mcp/tools,features/gsc,features/ga4}. `agora` (datetime UTC) é injetável nos testes.
VIA = "hub-conta-servico"
FERRAMENTAS_GA4 = (
    "get_google_analytics_organic_landing_pages", "get_google_analytics_page_performance",
    "get_google_analytics_key_events", "get_google_analytics_organic_overview",
    "get_google_analytics_traffic_acquisition", "get_google_analytics_ecommerce_performance",
    "get_google_analytics_site_search", "get_google_analytics_audience_breakdown",
    "get_google_analytics_measurement_health")
_GSC_MAX, _GSC_PADRAO, _GSC_ATRASO = 1000, 250, 3
_MESES = {"last_3_months": 3, "last_6_months": 6, "last_12_months": 12, "last_16_months": 16}
_DIAS = {"last_7_days": 7, "last_28_days": 28}
_GSC_DIMENSOES = ("query", "page", "country", "device", "date", "searchAppearance")


def _agora(agora):
    return agora or dt.datetime.now(dt.timezone.utc)


def _menos_meses(d, n):
    m = d.year * 12 + d.month - 1 - n
    ano, mes = divmod(m, 12)
    mes += 1
    dia = min(d.day, (dt.date(ano + (mes == 12), mes % 12 + 1, 1) - dt.timedelta(days=1)).day)
    return dt.date(ano, mes, dia)


def _datas_gsc(args, agora):
    hoje = _agora(agora).astimezone(dt.timezone.utc).date()
    piso = _menos_meses(hoje, 16).isoformat()
    if args.get("startDate") and args.get("endDate"):
        return max(args["startDate"], piso), args["endDate"]
    fim = hoje - dt.timedelta(days=_GSC_ATRASO)
    faixa = args.get("dateRange") or "last_28_days"
    ini = fim - dt.timedelta(days=_DIAS[faixa]) if faixa in _DIAS else _menos_meses(fim, _MESES[faixa])
    return max(ini.isoformat(), piso), fim.isoformat()


def _arredonda(x, casas):  # Math.round do JS: meio para cima
    return math.floor(x * 10 ** casas + 0.5) / 10 ** casas


def _gsc_linhas(site_url, args, agora=None):
    """searchAnalytics.query como o GscService do OpenSEO: (pedido, linhas do Google)."""
    ini, fim = _datas_gsc(args, agora)
    corpo = {"startDate": ini, "endDate": fim, "dimensions": list(args.get("dimensions") or ["query"]),
             "rowLimit": min(max(args.get("rowLimit") or _GSC_PADRAO, 1), _GSC_MAX),
             "type": args.get("type") or "web", "dataState": args.get("dataState") or "all"}
    if args.get("startRow"):
        corpo["startRow"] = args["startRow"]
    if args.get("filters"):
        corpo["dimensionFilterGroups"] = [{"groupType": "and", "filters": [
            {"dimension": f["dimension"], "operator": f.get("operator", "equals"), "expression": f["expression"]}
            for f in args["filters"]]}]
    linhas = _api(_base(site_url) + "/searchAnalytics/query", site_url, corpo).get("rows", [])
    if corpo["type"] in ("discover", "googleNews"):  # o Google não informa posição nesses tipos
        linhas = [{k: v for k, v in l.items() if k != "position"} for l in linhas]
    return corpo, linhas


def desempenho_gsc(site_url, args, agora=None):
    """get_search_console_performance."""
    dims = args.get("dimensions") or ["query"]
    if (("searchAppearance" in dims and len(dims) > 1) or bool(args.get("startDate")) != bool(args.get("endDate"))
            or (args.get("dateRange") or "last_28_days") not in {**_DIAS, **_MESES}):
        return {"ok": False, "reason": "invalid_request"}
    pedido = min(max(args.get("rowLimit") or _GSC_PADRAO, 1), _GSC_MAX)
    filtro = {k: args[k] for k in ("minPosition", "maxPosition", "minImpressions") if args.get(k) is not None}
    corpo, buscadas = _gsc_linhas(site_url, {**args, "rowLimit": _GSC_MAX if filtro else pedido}, agora)
    limite_busca = corpo["rowLimit"]

    def passa(l):
        p = l.get("position")
        return (l["impressions"] >= filtro.get("minImpressions", 0)
                and ("minPosition" not in filtro or (p is not None and p >= filtro["minPosition"]))
                and ("maxPosition" not in filtro or (p is not None and p <= filtro["maxPosition"])))

    mantidas = [l for l in buscadas if passa(l)] if filtro else buscadas
    linhas = [{**l, "ctr": _arredonda(l["ctr"], 4), **({"position": _arredonda(l["position"], 1)} if "position" in l else {})}
              for l in mantidas[:pedido]]
    inicio = args.get("startRow") or 0
    truncou = len(mantidas) > len(linhas)
    tem_mais = truncou or len(buscadas) >= limite_busca
    proxima = inicio + buscadas.index(mantidas[len(linhas) - 1]) + 1 if truncou and linhas else inicio + len(buscadas)
    return {"ok": True, "siteUrl": site_url, "startDate": corpo["startDate"], "endDate": corpo["endDate"], "dimensions": dims,
            "rowCount": len(linhas), "rows": linhas, "hasMore": tem_mais,
            **({"nextStartRow": proxima} if tem_mais else {}), "source": VIA}


def inspecionar_urls(site_url, args):
    """inspect_urls: falha por URL fica na própria linha."""
    urls = args.get("urls") or []
    if not 1 <= len(urls) <= 10:
        return {"ok": False, "reason": "invalid_request"}
    resultados = []
    for u in urls:
        corpo = {"siteUrl": site_url, "inspectionUrl": u, **({"languageCode": args["languageCode"]} if args.get("languageCode") else {})}
        try:
            resultados.append({"url": u, "result": _api("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                                                        site_url, corpo).get("inspectionResult")})
        except SemAcesso:
            raise
        except urllib.error.HTTPError as e:
            resultados.append({"url": u, "result": None, "error": str(e) or "Inspection failed"})
    return {"ok": True, "siteUrl": site_url, "results": resultados, "source": VIA}


# ---- GA4 ----
class _ErroGa4(Exception):
    def __init__(self, codigo, mensagem):
        super().__init__(mensagem)
        self.codigo, self.mensagem = codigo, mensagem


_METRICAS_VISAO = ["sessions", "activeUsers", "engagedSessions", "engagementRate", "keyEvents", "transactions", "purchaseRevenue"]
_RELATORIOS = {  # tipo da ferramenta -> (kind, dimensões, métricas, ordenar por)
    "get_google_analytics_organic_landing_pages": ("landing_pages", ["hostName", "landingPage"], [
        "sessions", "activeUsers", "engagedSessions", "engagementRate", "keyEvents", "sessionKeyEventRate", "transactions", "purchaseRevenue"], "sessions"),
    "get_google_analytics_page_performance": ("page_performance", ["hostName", "pagePath"], [
        "screenPageViews", "activeUsers", "userEngagementDuration", "keyEvents"], "screenPageViews"),
    "get_google_analytics_key_events": ("key_events", ["eventName"], ["keyEvents", "totalUsers"], "keyEvents"),
    "get_google_analytics_traffic_acquisition": ("traffic_acquisition", ["sessionDefaultChannelGroup"], [
        "sessions", "activeUsers", "engagedSessions", "engagementRate", "keyEvents", "transactions", "purchaseRevenue"], "sessions"),
    "get_google_analytics_ecommerce_performance": ("ecommerce_performance", ["itemName", "itemId"], [
        "itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"], "itemRevenue"),
    "get_google_analytics_site_search": ("site_search", ["searchTerm"], [
        "eventCount", "activeUsers", "sessions", "engagedSessions", "engagementRate"], "eventCount"),
    "get_google_analytics_audience_breakdown": ("audience_breakdown", ["deviceCategory"], [
        "activeUsers", "sessions", "engagementRate", "keyEvents"], "activeUsers"),
}
_ACQ = {"channel_group": "sessionDefaultChannelGroup", "source_medium": "sessionSourceMedium", "campaign": "sessionCampaignName"}
_AUD = {"device": "deviceCategory", "country": "country", "new_vs_returning": "newVsReturning"}
_ORGANICO = {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"matchType": "EXACT", "value": "Organic Search"}}}
_LIMITE_COMPLETO = 1000
_ANALYTICS = "https://analyticsdata.googleapis.com/v1beta/properties/"
_ADMIN = "https://analyticsadmin.googleapis.com/"


def _prop_id(propriedade):
    return "properties/" + str(propriedade).removeprefix("properties/")


def _ga(url, propriedade, corpo=None):
    try:
        return _api(url, propriedade, corpo, ESCOPO_GA4)
    except urllib.error.HTTPError as e:
        raise _ErroGa4(*{400: ("ga4_report_incompatible", "This report is not compatible with the selected Analytics property."),
                         401: ("ga4_reconnect_required", "The Google Analytics connection has expired or was revoked."),
                         404: ("ga4_property_inaccessible", "The selected Google Analytics property is no longer available."),
                         429: ("ga4_quota_exhausted", "Google Analytics reporting quota is exhausted. Try again later.")}.get(
                             e.code, ("ga4_upstream_unavailable", "Google Analytics reporting is temporarily unavailable."))) from None


def _info_propriedade(propriedade):
    p = _ga(_ADMIN + "v1beta/" + _prop_id(propriedade), propriedade)
    return {"id": _prop_id(propriedade), "nome": p.get("displayName", ""), "fuso": p.get("timeZone") or "UTC", "moeda": p.get("currencyCode", "")}


def _dia(d, dias):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=dias)).isoformat()


def _hoje_na_propriedade(agora, fuso):
    try:
        return _agora(agora).astimezone(zoneinfo.ZoneInfo(fuso)).date().isoformat()
    except Exception:  # shortcut: sem tzdata (Windows) cai para UTC, errando no máximo 1 dia na virada
        return _agora(agora).astimezone(dt.timezone.utc).date().isoformat()


def _datas_ga4(args, fuso, agora):
    ini, fim = args.get("startDate"), args.get("endDate")
    if bool(ini) != bool(fim):
        raise _ErroGa4("validation_error", "Provide both startDate and endDate, or neither.")
    if ini:
        try:
            ok = dt.date.fromisoformat(ini).isoformat() == ini and dt.date.fromisoformat(fim).isoformat() == fim and ini <= fim
        except ValueError:
            ok = False
        if not ok:
            raise _ErroGa4("validation_error", "Dates must be valid YYYY-MM-DD values with startDate on or before endDate.")
    ultimo = _dia(_hoje_na_propriedade(agora, fuso), -1)
    fim = fim or ultimo
    ini = ini or _dia(fim, -27)
    avisos = []
    if fim > ultimo:
        fim, avisos = ultimo, ["end_date_clamped"]
    if ini > fim:
        raise _ErroGa4("validation_error", "The resolved startDate is after the last complete Analytics day.")
    return {"requestedDateRange": {"startDate": args["startDate"], "endDate": args["endDate"]} if args.get("startDate") else None,
            "resolvedDateRange": {"startDate": ini, "endDate": fim}}, avisos


def _periodo_anterior(r):
    dias = (dt.date.fromisoformat(r["endDate"]) - dt.date.fromisoformat(r["startDate"])).days + 1
    fim = _dia(r["startDate"], -1)
    return {"startDate": _dia(fim, -(dias - 1)), "endDate": fim}


def _metrica_num(valor):
    n = float(valor)
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError
    return int(n) if n == int(n) else n


def _normaliza(resp, corpo):
    dims = [d["name"] for d in corpo["dimensions"]]
    mets = [m["name"] for m in corpo["metrics"]]
    hd = [h["name"] for h in resp.get("dimensionHeaders") or []]
    hm = [h["name"] for h in resp.get("metricHeaders") or []]
    vazia = "dimensionHeaders" not in resp and "metricHeaders" not in resp and not resp.get("rows")
    if not vazia and (hd != dims or hm != mets):
        raise _ErroGa4("ga4_malformed_response", "Google Analytics returned an invalid report.")
    meta = resp.get("metadata") or {}
    restritas = [{"metricName": a["metricName"], "restrictedMetricTypes": a.get("restrictedMetricTypes") or []}
                 for a in (meta.get("schemaRestrictionResponse") or {}).get("activeMetricRestrictions") or []]
    nomes_restritos = {a["metricName"] for a in restritas}
    linhas = []
    try:
        for r in resp.get("rows") or []:
            dv, mv = r.get("dimensionValues") or [], r.get("metricValues") or []
            if len(dv) != len(hd) or len(mv) != len(hm):
                raise ValueError
            linha = {n: v.get("value", "") for n, v in zip(hd, dv)}
            linha.update({n: None if n in nomes_restritos else _metrica_num(v.get("value", "")) for n, v in zip(hm, mv)})
            linhas.append(linha)
    except ValueError:
        raise _ErroGa4("ga4_malformed_response", "Google Analytics returned an invalid report.") from None
    amostra = meta.get("samplingMetadatas") or []
    return {"rows": linhas, "totalRowCount": resp.get("rowCount", len(linhas)), "quota": resp.get("propertyQuota"),
            "reportMetadata": {"dataLossFromOtherRow": meta.get("dataLossFromOtherRow", False),
                               "subjectToThresholding": meta.get("subjectToThresholding", False), "sampling": amostra,
                               "restrictedMetrics": restritas, "emptyReason": meta.get("emptyReason"),
                               "hasLimitedData": bool(meta.get("dataLossFromOtherRow") or meta.get("subjectToThresholding") or amostra or restritas)}}


def _corpo_relatorio(kind, dims, mets, ordem, ini, fim, canal, limite, offset, a):
    filtro = _ORGANICO if canal == "organic_search" else None
    filtro_metrica = None
    if kind == "site_search":
        filtro = {"andGroup": {"expressions": [
            {"filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": "view_search_results"}}},
            {"notExpression": {"filter": {"fieldName": "searchTerm", "stringFilter": {"matchType": "EXACT", "value": "(not set)"}}}}]}}
    mais = lambda campo: {"filter": {"fieldName": campo, "numericFilter": {"operation": "GREATER_THAN", "value": {"doubleValue": 0}}}}
    if kind == "key_events":
        filtro_metrica = mais("keyEvents")
    if kind == "ecommerce_performance" and a.get("breakdown") == "landing_page" and a.get("onlyWithTransactions"):
        filtro_metrica = mais("transactions")
    corpo = {"dateRanges": [{"startDate": ini, "endDate": fim}], "dimensions": [{"name": d} for d in dims],
             "metrics": [{"name": m} for m in mets], "dimensionFilter": filtro, "metricFilter": filtro_metrica,
             "offset": str(offset), "limit": str(limite), "orderBys": [{"metric": {"metricName": ordem}, "desc": True}],
             "keepEmptyRows": False, "returnPropertyQuota": True}
    return {k: v for k, v in corpo.items() if v is not None}


def _config_relatorio(tipo, a):
    """kind, breakdown efetivo, dimensões, métricas, ordem e canal, como Ga4ReportDefinitions."""
    kind, dims, mets, ordem = _RELATORIOS[tipo]
    bd = a.get("breakdown")
    canal = {"landing_pages": "organic_search", "traffic_acquisition": "all", "site_search": "all"}.get(kind) or a.get("channel") or "organic_search"
    if kind == "traffic_acquisition":
        bd = bd or "channel_group"
        dims, efetivo = [_ACQ[bd]], bd
    elif kind == "audience_breakdown":
        bd = bd or "device"
        dims, efetivo = [_AUD[bd]], bd
    else:
        dims, efetivo = list(dims), {"landing_pages": "landing_page", "site_search": "search_term"}.get(kind)
        if kind == "page_performance":
            efetivo = "page_and_date" if a.get("includeDate") else "page"
            if a.get("includeDate"):
                dims.append("date")
        elif kind == "key_events":
            efetivo = bd or "event"
            if efetivo == "event_and_landing_page":
                dims += ["hostName", "landingPage"]
        elif kind == "ecommerce_performance":
            efetivo = bd or "item"
            if efetivo == "landing_page":
                dims, mets, ordem = ["hostName", "landingPage"], ["sessions", "transactions", "purchaseRevenue"], "purchaseRevenue"
    return kind, efetivo, dims, list(mets), ordem, canal, bd


def _compara_valor(atual, anterior):
    mudanca = atual - anterior if atual is not None and anterior is not None else None
    return {"current": atual, "previous": anterior, "absoluteChange": mudanca,
            "percentChange": mudanca / anterior if mudanca is not None and anterior else None}


def _num(linha, m):
    v = (linha or {}).get(m)
    return v if isinstance(v, (int, float)) else None


def _comparacao(atual, ant, periodo_ant, dims, mets):
    chave = lambda l: json.dumps([l.get(d) for d in dims])
    pa, pp = {chave(l): l for l in atual["rows"]}, {chave(l): l for l in ant["rows"]}
    chaves = list(pa) + [k for k in pp if k not in pa]
    completo = lambda r: len(r["rows"]) == r["totalRowCount"]
    return {"previousDateRange": periodo_ant, "dimensions": dims, "metrics": mets,
            "rows": [{"dimensions": {d: (pa.get(k) or pp.get(k)).get(d) for d in dims},
                      "metrics": {m: _compara_valor(_num(pa.get(k), m), _num(pp.get(k), m)) for m in mets}} for k in chaves],
            "coverage": {"complete": completo(atual) and completo(ant),
                         "current": {"fetchedRowCount": len(atual["rows"]), "totalRowCount": atual["totalRowCount"]},
                         "previous": {"fetchedRowCount": len(ant["rows"]), "totalRowCount": ant["totalRowCount"]}},
            "reportMetadata": {"hasLimitedData": atual["reportMetadata"]["hasLimitedData"] or ant["reportMetadata"]["hasLimitedData"],
                               "current": atual["reportMetadata"], "previous": ant["reportMetadata"]},
            "quota": ant["quota"]}


def _host_interno(valor):
    origem = valor.split(" / ")[0].lower()
    sem = origem.removeprefix("https://").removeprefix("http://")
    if sem == "::1" or sem.startswith("[::1]"):
        return True
    host = sem.split(":")[0]
    if host == "localhost" or host.startswith("127."):
        return True
    try:
        p = [int(x) for x in host.split(".")]
    except ValueError:
        return False
    return len(p) == 4 and (p[0] == 10 or (p[0] == 172 and 16 <= p[1] <= 31) or (p[0] == 192 and p[1] == 168))


def _soma(linhas, m):
    return sum(_num(l, m) or 0 for l in linhas)


def _diagnosticos_atribuicao(rel):
    completo = len(rel["rows"]) == rel["totalRowCount"]
    cob = {"complete": completo, "limitedData": rel["reportMetadata"]["hasLimitedData"],
           "fetchedRowCount": len(rel["rows"]), "totalRowCount": rel["totalRowCount"]}
    if not completo or cob["limitedData"]:
        return {"diagnostics": [], "diagnosticCoverage": cob}
    diag, linhas = [], rel["rows"]
    total = _soma(linhas, "sessions")
    sem = _soma([l for l in linhas if l.get("sessionSourceMedium") == "(not set)"], "sessions")
    if (sem / total if total > 0 else 0) >= 0.05:
        diag.append({"code": "attribution_not_set_share_high", "severity": "warning",
                     "message": "A notable share of sessions has no source/medium attribution.",
                     "evidence": {"sessions": sem, "totalSessions": total, "share": sem / total}, "threshold": {"share": 0.05}})
    internas = [l for l in linhas if isinstance(l.get("sessionSourceMedium"), str) and _host_interno(l["sessionSourceMedium"])]
    if _soma(internas, "sessions") > 0:
        diag.append({"code": "internal_referral_traffic_detected", "severity": "warning",
                     "message": "Local or private-network referral sources appear in acquisition data.",
                     "evidence": {"sessions": _soma(internas, "sessions"), "sources": [l["sessionSourceMedium"] for l in internas]},
                     "threshold": {"sessions": 0}})
    grupos = {}
    for l in linhas:
        if isinstance(l.get("sessionSourceMedium"), str):
            grupos.setdefault(l["sessionSourceMedium"].lower(), []).append(l["sessionSourceMedium"])
    variantes = [list(dict.fromkeys(v)) for v in grupos.values() if len(set(v)) > 1]
    if variantes:
        diag.append({"code": "source_medium_case_variants_detected", "severity": "info",
                     "message": "Source/medium values differ only by letter casing.",
                     "evidence": {"variantGroups": variantes}, "threshold": {"variantGroups": 0}})
    return {"diagnostics": diag, "diagnosticCoverage": cob}


def _estado(rel, detectado):
    if rel["reportMetadata"]["hasLimitedData"] or len(rel["rows"]) != rel["totalRowCount"]:
        return "unknown"
    return "detected" if detectado else "none"


def _extras(rel, kind, a, canal, datas):
    if kind == "traffic_acquisition" and a.get("breakdown") == "source_medium":
        return _diagnosticos_atribuicao(rel)
    if kind == "ecommerce_performance":
        bd = a.get("breakdown") or "item"
        mets = ["itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"] if bd == "item" else ["transactions", "purchaseRevenue"]
        evid = {m: _soma(rel["rows"], m) for m in mets}
        st = _estado(rel, any(v > 0 for v in evid.values()))
        motivo = {"none": "No matching ecommerce activity was reported for this period and channel.",
                  "unknown": "The fetched report is incomplete or limited, so ecommerce activity cannot be determined."}.get(st)
        return {"diagnostics": [{"code": "no_ecommerce_activity", "severity": "info", "message": motivo, "evidence": evid,
                                 "threshold": {"matchingActivity": 0}}] if st == "none" else [],
                "ecommerceActivity": {"status": st, "dateRange": datas, "channel": canal, "breakdown": bd, "evidence": evid, "reason": motivo}}
    if kind == "site_search":
        n = _soma(rel["rows"], "eventCount")
        st = _estado(rel, n > 0)
        motivo = {"none": "No measured site-search terms were reported for this period.",
                  "unknown": "The fetched report is incomplete or limited, so site-search activity cannot be determined."}.get(st)
        return {"diagnostics": [{"code": "no_site_search_activity", "severity": "info", "message": motivo,
                                 "evidence": {"searchTermCount": rel["totalRowCount"], "searchEventCount": n},
                                 "threshold": {"searchEvents": 0}}] if st == "none" else [],
                "siteSearchActivity": {"status": st, "dateRange": datas, "searchTermCount": rel["totalRowCount"],
                                       "searchEventCount": n, "reason": motivo}}
    return {"diagnostics": []}


def _roda(prop, corpo):
    return _normaliza(_ga(_ANALYTICS + prop["id"].removeprefix("properties/") + ":runReport", prop["id"], corpo), corpo)


def _relatorio(propriedade, tipo, a, agora):
    limite, offset = a.get("limit", 100), a.get("offset", 0)
    if not isinstance(limite, int) or not 1 <= limite <= 1000:
        raise _ErroGa4("validation_error", "limit must be an integer from 1 to 1000.")
    if not isinstance(offset, int) or offset < 0:
        raise _ErroGa4("validation_error", "offset must be a non-negative integer.")
    prop = _info_propriedade(propriedade)
    datas, avisos = _datas_ga4(a, prop["fuso"], agora)
    r = datas["resolvedDateRange"]
    kind, efetivo, dims, mets, ordem, canal, bd = _config_relatorio(tipo, a)
    comparar = bool(a.get("comparePreviousPeriod"))
    suporta = (kind == "key_events" and efetivo == "event") or (kind == "traffic_acquisition" and efetivo == "channel_group") \
        or (kind == "audience_breakdown" and efetivo in ("device", "new_vs_returning"))
    if comparar and not suporta:
        raise _ErroGa4("validation_error", "Previous-period comparison is only available for event key events, channel-group acquisition, device audiences, and new-versus-returning audiences.")
    completo = comparar or kind in ("ecommerce_performance", "site_search") or (kind == "traffic_acquisition" and efetivo == "source_medium")
    montar = lambda ini, fim, lim, off: _corpo_relatorio(kind, dims, mets, ordem, ini, fim, canal, lim, off, {**a, "breakdown": bd})
    corpo = montar(r["startDate"], r["endDate"], _LIMITE_COMPLETO if completo else limite, 0 if completo else offset)
    atual = _roda(prop, corpo)
    ant, periodo_ant = None, None
    if comparar:
        periodo_ant = _periodo_anterior(r)
        ant = _roda(prop, montar(periodo_ant["startDate"], periodo_ant["endDate"], _LIMITE_COMPLETO, 0))
    linhas = atual["rows"][offset:offset + limite] if completo else atual["rows"]
    if completo and not (atual["totalRowCount"] <= len(atual["rows"]) or offset + limite <= len(atual["rows"])):
        linhas = _roda(prop, {**corpo, "offset": str(offset), "limit": str(limite)})["rows"]
    if not linhas and offset < atual["totalRowCount"]:
        raise _ErroGa4("ga4_malformed_response", "Google Analytics returned an invalid report.")
    proximo = offset + len(linhas)
    tem_mais = proximo < atual["totalRowCount"]
    comp = _comparacao(atual, ant, periodo_ant, dims, mets) if ant else None
    saida = {"status": "ok", "source": {"provider": "google_analytics", "propertyId": prop["id"], "propertyDisplayName": prop["nome"], "via": VIA},
             "request": {**datas, "propertyTimeZone": prop["fuso"], "currencyCode": prop["moeda"], "channel": canal,
                         "reportKind": kind, "breakdown": efetivo, "dimensions": dims, "metrics": mets,
                         "flags": {"includeDate": bool(a.get("includeDate")), "onlyWithTransactions": bool(a.get("onlyWithTransactions"))},
                         "limit": limite, "offset": offset},
             "rowCount": len(linhas), "totalRowCount": atual["totalRowCount"], "rows": linhas,
             "pageInfo": {"offset": offset, "limit": limite, "hasMore": tem_mais, "nextOffset": proximo if tem_mais else None},
             "reportMetadata": atual["reportMetadata"], "quota": atual["quota"],
             "warnings": avisos + (["comparison_incomplete"] if comp and not comp["coverage"]["complete"] else []),
             **_extras(atual, kind, {**a, "breakdown": bd}, canal, r)}
    if comp:
        saida["comparison"] = comp
    return saida


def _visao(propriedade, a, agora):
    prop = _info_propriedade(propriedade)
    datas, avisos = _datas_ga4(a, prop["fuso"], agora)
    r = datas["resolvedDateRange"]
    ant = _periodo_anterior(r)
    tendencia = a.get("trend") or "daily"

    def corpo(ini, fim, trend=None):
        dim = [{"name": "date" if tendencia == "daily" else "yearWeek"}] if trend else []
        return {"dateRanges": [{"startDate": ini, "endDate": fim}], "dimensions": dim, "metrics": [{"name": m} for m in _METRICAS_VISAO],
                "dimensionFilter": _ORGANICO, "offset": "0", "limit": "1000" if trend else "1",
                "orderBys": [{"dimension": {"dimensionName": dim[0]["name"]}}] if trend else [],
                "keepEmptyRows": False, "returnPropertyQuota": True}
    atual = _roda(prop, corpo(r["startDate"], r["endDate"]))
    anterior = _roda(prop, corpo(ant["startDate"], ant["endDate"]))
    serie = _roda(prop, corpo(r["startDate"], r["endDate"], True))
    ca, cp = (atual["rows"] or [None])[0], (anterior["rows"] or [None])[0]
    limitado = any(x["reportMetadata"]["hasLimitedData"] for x in (atual, anterior, serie))
    diag = []
    va, vp = _num(ca, "keyEvents"), _num(cp, "keyEvents")
    if not limitado and va is not None and vp is not None and vp >= 5 and (va - vp) / vp <= -0.5:
        diag.append({"code": "key_events_sharp_decline", "severity": "warning",
                     "message": "Organic key events declined sharply compared with the previous equal-length period.",
                     "evidence": {"current": va, "previous": vp, "percentChange": (va - vp) / vp},
                     "threshold": {"minimumPreviousKeyEvents": 5, "percentChange": -0.5}})
    return {"status": "ok", "source": {"provider": "google_analytics", "propertyId": prop["id"], "propertyDisplayName": prop["nome"], "via": VIA},
            "request": {**datas, "previousDateRange": ant, "propertyTimeZone": prop["fuso"], "currencyCode": prop["moeda"],
                        "channel": "organic_search", "trend": tendencia},
            "current": ca, "previous": cp,
            "comparison": {m: _compara_valor(_num(ca, m), _num(cp, m)) for m in _METRICAS_VISAO},
            "trend": serie["rows"], "diagnostics": diag,
            "reportMetadata": {"hasLimitedData": limitado, "reports": [x["reportMetadata"] for x in (atual, anterior, serie)]},
            "quota": serie["quota"] or atual["quota"],
            "warnings": avisos + (["trend_truncated"] if serie["totalRowCount"] > len(serie["rows"]) else [])}


def _saude(propriedade):
    prop = _info_propriedade(propriedade)
    v1b, v1a = _ADMIN + "v1beta/", _ADMIN + "v1alpha/"
    pid = prop["id"]
    fluxos = _ga(f"{v1a}{pid}/dataStreams?pageSize=200", propriedade).get("dataStreams") or []
    padrao = {"streamEnabled": False, "scrollsEnabled": False, "outboundClicksEnabled": False, "siteSearchEnabled": False,
              "videoEngagementEnabled": False, "fileDownloadsEnabled": False, "pageChangesEnabled": False,
              "formInteractionsEnabled": False, "searchQueryParameter": "", "uriQueryParameter": ""}
    web = []
    for f in fluxos:
        if f.get("type") != "WEB_DATA_STREAM":
            continue
        em = _ga(f"{v1a}{f['name']}/enhancedMeasurementSettings", propriedade)
        web.append({"streamId": f["name"].split("/")[-1], "displayName": f.get("displayName", ""),
                    "measurementId": (f.get("webStreamData") or {}).get("measurementId"),
                    "defaultUri": (f.get("webStreamData") or {}).get("defaultUri"),
                    "createTime": f.get("createTime"), "updateTime": f.get("updateTime"),
                    "enhancedMeasurement": {k: em.get(k, v) for k, v in padrao.items()}})
    lista = lambda rec, chave: _ga(f"{v1b}{pid}/{rec}?pageSize=200", propriedade).get(chave) or []
    eventos = [{k: e[k] for k in ("eventName", "createTime", "deletable", "custom", "countingMethod", "defaultValue") if k in e}
               for e in lista("keyEvents", "keyEvents")]
    dimensoes = [{"parameterName": d["parameterName"], "displayName": d["displayName"], "description": d.get("description", ""),
                  "scope": d["scope"], "disallowAdsPersonalization": d.get("disallowAdsPersonalization", False)}
                 for d in lista("customDimensions", "customDimensions")]
    metricas = [{"parameterName": m["parameterName"], "displayName": m["displayName"], "description": m.get("description", ""),
                 "measurementUnit": m["measurementUnit"], "scope": m["scope"], "restrictedMetricType": m.get("restrictedMetricType", [])}
                for m in lista("customMetrics", "customMetrics")]
    em = [w["enhancedMeasurement"] for w in web]
    problemas = (["no_web_stream"] if not web else []) \
        + (["enhanced_measurement_disabled"] if web and not any(e["streamEnabled"] for e in em) else []) \
        + (["site_search_measurement_disabled"] if web and not any(e["streamEnabled"] and e["siteSearchEnabled"] for e in em) else []) \
        + (["no_key_events_configured"] if not eventos else [])
    return {"status": "ok", "source": {"provider": "google_analytics_admin", "propertyId": pid, "propertyDisplayName": prop["nome"], "via": VIA},
            "summary": {"dataStreamCount": len(fluxos), "webStreamCount": len(web), "keyEventCount": len(eventos),
                        "customDimensionCount": len(dimensoes), "customMetricCount": len(metricas), "issueCount": len(problemas)},
            "issues": problemas, "webStreams": web,
            "otherStreams": [{"streamId": f["name"].split("/")[-1], "type": f.get("type"), "displayName": f.get("displayName", "")}
                             for f in fluxos if f.get("type") != "WEB_DATA_STREAM"],
            "keyEvents": eventos, "customDefinitions": {"dimensions": dimensoes, "metrics": metricas}}


def _erro_ga4(e):
    return {"status": "error", "error": {"code": e.codigo, "message": e.mensagem}}


def relatorio_ga4(propriedade, args, tipo, agora=None):
    """Uma das ferramentas get_google_analytics_* (tipo = nome da ferramenta)."""
    if tipo not in FERRAMENTAS_GA4:
        raise ValueError("ferramenta GA4 desconhecida: " + tipo)
    try:
        if tipo.endswith("organic_overview"):
            return _visao(propriedade, args, agora)
        if tipo.endswith("measurement_health"):
            return _saude(propriedade)
        return _relatorio(propriedade, tipo, args, agora)
    except _ErroGa4 as e:
        return _erro_ga4(e)


def _chave_pagina(valor):
    v = valor.strip()
    if not v or v == "(not set)":
        return None
    try:
        u = urllib.parse.urlsplit(v if "://" in v else "https://" + v)
        host, porta = (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    if not host:
        return None
    if porta and porta != {"http": 80, "https": 443}.get(u.scheme):
        host += f":{porta}"
    caminho = u.path or "/"
    return host + (caminho.rstrip("/") or "/" if len(caminho) > 1 else caminho)


def _percentis(valores):
    if len(valores) <= 1:
        return [1] * len(valores)
    return [sum(1 for c in valores if c < v) / (len(valores) - 1) for v in valores]


def oportunidades_busca(site_url, propriedade_ga4, args, agora=None):
    """get_search_opportunities: páginas do GSC nas posições 4-20 cruzadas com landing pages orgânicas do GA4."""
    limite = args.get("limit", 50)
    try:
        if not isinstance(limite, int) or not 1 <= limite <= 100:
            raise _ErroGa4("validation_error", "limit must be an integer from 1 to 100.")
        prop = _info_propriedade(propriedade_ga4)
        if not args.get("startDate") and not args.get("endDate"):
            fim = _dia(_hoje_na_propriedade(agora, prop["fuso"]), -3)
            datas = {"startDate": _dia(fim, -27), "endDate": fim}
        else:
            datas = _datas_ga4(args, prop["fuso"], agora)[0]["resolvedDateRange"]
        _, gsc = _gsc_linhas(site_url, {"dimensions": ["page"], **datas, "rowLimit": 1000, "type": "web", "dataState": "final"}, agora)
        ga = _relatorio(propriedade_ga4, "get_google_analytics_organic_landing_pages", {**datas, "limit": 1000}, agora)
    except _ErroGa4 as e:
        return _erro_ga4(e)
    por_pagina, invalidas = {}, 0
    for l in ga["rows"]:
        k = _chave_pagina(f"{l.get('hostName') or ''}{l.get('landingPage') or ''}")
        if k is None:
            invalidas += 1
        else:
            por_pagina[k] = l
    num = lambda l, n: l[n] if isinstance(l.get(n), (int, float)) else 0
    cand = []
    for l in gsc:
        if "position" not in l or not 4 <= l["position"] <= 20:
            continue
        pagina = (l.get("keys") or [""])[0]
        norm = _chave_pagina(pagina)
        an = por_pagina.get(norm) if norm else None
        cand.append({"page": pagina, "normalizedPage": norm, "clicks": l["clicks"], "impressions": l["impressions"],
                     "ctr": l["ctr"], "position": l["position"], "joinStatus": "joined" if an else "gsc_only",
                     "ga4": {m: num(an, m) for m in ("sessions", "activeUsers", "engagedSessions", "engagementRate", "keyEvents",
                                                      "sessionKeyEventRate", "transactions")}
                     | {"purchaseRevenue": an["purchaseRevenue"] if isinstance(an.get("purchaseRevenue"), (int, float)) else None} if an else None,
                     "score": None, "scoreComponents": None})
    unidos = [c for c in cand if c["ga4"]]
    fallback = bool(unidos) and all(c["ga4"]["keyEvents"] == 0 for c in unidos)
    demanda = _percentis([math.log1p(c["impressions"]) for c in unidos])
    valor = _percentis([c["ga4"]["engagementRate" if fallback else "sessionKeyEventRate"] for c in unidos])
    alcance = _percentis([20 - c["position"] for c in unidos])
    for c, d, v, a in zip(unidos, demanda, valor, alcance):
        c["scoreComponents"] = {"demand": _arredonda(d, 4), "businessValue": _arredonda(v, 4), "reachability": _arredonda(a, 4)}
        k = c["scoreComponents"]
        c["score"] = math.floor(100 * (0.5 * k["demand"] + 0.3 * k["businessValue"] + 0.2 * k["reachability"]) + 0.5)
    cand.sort(key=lambda c: (c["score"] is None, -(c["score"] or 0), -c["impressions"]))
    devolvidas = cand[:limite]
    return {"status": "ok", "source": {"searchConsoleSiteUrl": site_url, "googleAnalyticsPropertyId": ga["source"]["propertyId"],
                                       "googleAnalyticsPropertyDisplayName": ga["source"]["propertyDisplayName"], "via": VIA},
            "request": {"dateRange": datas, "limit": limite, "searchConsoleTimeZone": "America/Los_Angeles",
                        "googleAnalyticsTimeZone": ga["request"]["propertyTimeZone"]},
            "rowCount": len(devolvidas), "totalCandidateRows": len(cand), "rows": devolvidas,
            "scoring": {"formula": "round(100 * (0.5 * demand + 0.3 * businessValue + 0.2 * reachability))",
                        "businessValueMetric": "engagementRate" if fallback else "sessionKeyEventRate",
                        "engagementFallback": fallback, "scoreDataLimited": ga["reportMetadata"]["hasLimitedData"]},
            "coverage": {"gscRowsConsidered": len(gsc), "ga4RowsConsidered": len(ga["rows"]), "matchedRows": len(unidos),
                         "unmatchedGscRows": len(cand) - len(unidos),
                         "unmatchedGa4Rows": max(len(por_pagina) - len(unidos), 0) + invalidas},
            "truncated": {"gsc": len(gsc) >= 1000, "ga4": ga["totalRowCount"] > len(ga["rows"]), "candidates": len(devolvidas) < len(cand)},
            "warnings": ga["warnings"] + ([] if ga["request"]["propertyTimeZone"] == "America/Los_Angeles" else ["source_time_zones_differ"]),
            "reportMetadata": ga["reportMetadata"], "quota": ga["quota"]}


def periodo_utc(dias, hoje=None):
    """Mesma janela do `datasRelativas` do so.mjs: termina 3 dias antes de hoje na data UTC (e não na local)."""
    return periodo(dias, 3, hoje or dt.datetime.now(dt.timezone.utc).date())


def gsc_consultas_cru(propriedade, dias=90, dimensoes=("query",), limite=1000, hoje=None):
    """Um POST de searchAnalytics/query, com o corpo e a resposta que o so.mjs `google gsc-consultas` sempre teve (snapshots da coleta das 4h)."""
    inicio, fim = periodo_utc(dias, hoje)
    return _api(_base(propriedade) + "/searchAnalytics/query", propriedade,
                {"startDate": inicio, "endDate": fim, "dimensions": list(dimensoes), "rowLimit": limite})


def _corpo_runreport(dimensoes, metricas, inicio, fim, limite, eventos=()):
    corpo = {"dateRanges": [{"startDate": inicio, "endDate": fim}], "dimensions": [{"name": d} for d in dimensoes],
             "metrics": [{"name": m} for m in metricas], "limit": limite}
    if eventos:  # só os eventos pedidos: o corte do limite não leva evento do funil embora
        corpo["dimensionFilter"] = {"filter": {"fieldName": "eventName", "inListFilter": {"values": list(eventos)}}}
    return corpo


def ga4_relatorio_cru(propriedade, dias=28, dimensoes=(), metricas=("sessions",), limite=1000, hoje=None):
    """Um POST de runReport, igual ao so.mjs `google ga4-relatorio`."""
    inicio, fim = periodo_utc(dias, hoje)
    return _ga(_ANALYTICS + str(propriedade).removeprefix("properties/") + ":runReport", propriedade,
               _corpo_runreport(dimensoes, metricas, inicio, fim, limite))


def ga4_paginado(propriedade, dimensoes, metricas, inicio, fim, limite=100000, buscar=None, eventos=()):
    """runReport paginado por `offset`: nenhuma linha fica de fora. Único lugar que pagina o GA4 (o bi_historico e os
    consumidores dos snapshots passam por aqui). `buscar(corpo) -> linhas` troca o transporte (testes)."""
    buscar = buscar or (lambda corpo: _ga(_ANALYTICS + str(propriedade).removeprefix("properties/") + ":runReport",
                                          propriedade, corpo).get("rows", []))
    corpo, todas = {**_corpo_runreport(dimensoes, metricas, inicio, fim, limite, eventos), "offset": 0}, []
    while True:
        lote = buscar(dict(corpo))
        todas += lote
        if len(lote) < limite:
            return todas
        corpo["offset"] += limite


def _cli_cru(argv):
    """`python3 -m hub.google gsc-consultas|ga4-relatorio ...`: a saída (JSON, indent 2) que o so.mjs imprimia."""
    import argparse
    ap = argparse.ArgumentParser(prog="hub.google")
    ap.add_argument("comando", choices=("gsc-consultas", "ga4-relatorio"))
    ap.add_argument("--site")
    ap.add_argument("--propriedade")
    ap.add_argument("--dias", type=int)
    ap.add_argument("--dimensoes")
    ap.add_argument("--metricas")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--eventos", help="ga4-relatorio: só estes eventName (vírgula) e paginado até a última linha (KRN-1031)")
    a = ap.parse_args(argv)
    lista = lambda v, padrao: [x.strip() for x in v.split(",") if x.strip()] if v else list(padrao)
    nao_nulos = lambda **kw: {k: v for k, v in kw.items() if v is not None}
    if a.comando == "gsc-consultas":
        if not a.site:
            sys.exit("falta --site")
        r = gsc_consultas_cru(a.site, **nao_nulos(dias=a.dias, limite=a.limite), dimensoes=lista(a.dimensoes, ("query",)))
    elif a.eventos:
        if not a.propriedade:
            sys.exit("falta --propriedade")
        inicio, fim = periodo_utc(a.dias if a.dias is not None else 28)
        r = {"rows": ga4_paginado(a.propriedade, lista(a.dimensoes, ()), lista(a.metricas, ("sessions",)), inicio, fim,
                                  **nao_nulos(limite=a.limite), eventos=lista(a.eventos, ()))}
    else:
        if not a.propriedade:
            sys.exit("falta --propriedade")
        r = ga4_relatorio_cru(a.propriedade, **nao_nulos(dias=a.dias, limite=a.limite), dimensoes=lista(a.dimensoes, ()),
                              metricas=lista(a.metricas, ("sessions",)))
    print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":  # deploy: python3 -m hub.google gravar-oauth <env> <destino>
    if len(sys.argv) == 4 and sys.argv[1] == "enviar-sitemap" and sys.argv[2] == "--site":
        from hub import sites
        _s = sites.site(sys.argv[3])
        enviar_sitemap(_s["ids"]["gsc"], f"https://{_s['dominio']}/sitemap.xml")
        print("sitemap enviado:", _s["id"])
    elif len(sys.argv) > 1 and sys.argv[1] in ("gsc-consultas", "ga4-relatorio"):
        _cli_cru(sys.argv[1:])
    elif len(sys.argv) == 4 and sys.argv[1] == "gravar-oauth":
        print(f"OAuth do {config.dono()}:", gravar_oauth(sys.argv[2], sys.argv[3]))
    else:
        sys.exit("uso: python3 -m hub.google gravar-oauth <env> <destino> | enviar-sitemap --site <id> | gsc-consultas --site <p> | ga4-relatorio --propriedade <id>")
