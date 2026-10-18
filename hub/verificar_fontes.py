"""Verificação das fontes de um projeto (tela /config/projetos): GSC, GA4, Clarity, GMB, PageSpeed, repo e deploy.

Cada função recebe o site (dict de sites/*.json) e um cliente injetável (nos testes não há rede) e devolve
(status, mensagem em português): "ok" (provado), "erro" (a chamada falhou), "nao_configurado" (o projeto não tem o ID) ou
"manual" (não há como provar daqui; confirmar à mão). A mensagem nunca leva token, chave nem texto cru de erro (só o tipo da
exceção ou o código HTTP). O resultado fica em <dados>/verificacoes/<projeto>.json; arquivo corrompido vale como vazio.

Clientes: gsc() -> lista de propriedades; ga4(id) -> relatório; clarity/gmb/deploy(url, headers=None) -> (código, texto);
pagespeed(url) -> JSON do PSI; repo(args_do_gh) -> (código de saída, texto).
"""
import datetime as dt
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from hub import data, gh, home, psi
from hub.sites import RE_GMB_CID, RE_ID

FONTES = (("gsc", "Search Console"), ("ga4", "GA4"), ("clarity", "Clarity"), ("gmb", "Google Meu Negócio"),
          ("pagespeed", "PageSpeed"), ("repo", "Repositório"), ("deploy", "Deploy"))
ROTULO = dict(FONTES)
STATUS = ("ok", "erro", "nao_configurado", "manual")
CLARITY_URL = "https://www.clarity-ms.example/export-data/api/v1/project-live-insights?numOfDays=1"


def _pasta():
    return data() / "verificacoes"


def _arquivo(projeto):
    if not isinstance(projeto, str) or not RE_ID.match(projeto):
        raise ValueError(f"id de projeto inválido: {projeto!r}")
    return _pasta() / f"{projeto}.json"


def ler(projeto):
    """{fonte: {status, mensagem, em}}. Id inválido, arquivo ausente, ilegível ou fora do formato: {}."""
    try:
        d = json.loads(_arquivo(projeto).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict) and v.get("status") in STATUS} if isinstance(d, dict) else {}


def id_da_fonte(site, fonte):
    """O valor cadastrado que a verificação de `fonte` prova (o selo verde só vale enquanto ele não muda)."""
    i = site.get("ids") or {}
    return {"gsc": i.get("gsc"), "ga4": i.get("ga4"), "clarity": i.get("clarity"), "gmb": i.get("gmb_cid"),
            "pagespeed": site.get("dominio"), "deploy": site.get("dominio"), "repo": (site.get("repo") or {}).get("github")}.get(fonte) or None


def _gravar(projeto, fonte, status, mensagem, agora, id_verificado=None):
    arq = _arquivo(projeto)
    d = ler(projeto)
    d[fonte] = {"status": status, "mensagem": mensagem, "em": agora, "id_verificado": id_verificado}
    arq.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=arq.parent, prefix=f".{projeto}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, arq)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _falha(fonte, ex):
    return "erro", f"{ROTULO[fonte]}: a chamada falhou ({type(ex).__name__}). Confira o acesso e tente de novo."


def _http(url, headers=None, timeout=30):
    """Transporte padrão: (código, texto). Código HTTP de erro volta como código, não como exceção."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ""


def _http_json(url):
    codigo, texto = _http(url, timeout=180)
    if codigo != 200:
        raise OSError(f"HTTP {codigo}")
    return json.loads(texto)


def _gh(args):
    r = gh.executar(args, "verificar_fontes", cache=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.returncode, r.stdout if r.returncode == 0 else r.stderr


def _google_gsc():
    from hub import google
    return google.propriedades()


def _google_ga4(propriedade):
    from hub import google
    hoje = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return google._ga(google._ANALYTICS + propriedade + ":runReport", propriedade,
                      {"dateRanges": [{"startDate": hoje, "endDate": hoje}], "metrics": [{"name": "sessions"}], "limit": 1})


def _token_clarity(projeto):
    """Token do projeto (credenciais/clarity/<id>.json, o mesmo do so.mjs). Nunca é registrado nem devolvido."""
    try:
        t = json.loads((home() / "credenciais" / "clarity" / f"{projeto}.json").read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError, AttributeError):
        return None
    return t if isinstance(t, str) and t else None


def _ids(site):
    return site.get("ids") or {}


def gsc(site, cliente=None):
    prop = _ids(site).get("gsc")
    if not prop:
        return "nao_configurado", "Search Console: o projeto não tem a propriedade cadastrada."
    try:
        entradas = (cliente or _google_gsc)()
    except Exception as ex:  # noqa: BLE001
        return _falha("gsc", ex)
    if any(isinstance(x, dict) and x.get("siteUrl") == prop for x in entradas or []):
        return "ok", f"Search Console: a propriedade {prop} aparece na conta de serviço."
    return "erro", f"Search Console: a conta de serviço não enxerga a propriedade {prop}. Dê acesso a ela no Search Console."


def ga4(site, cliente=None):
    prop = _ids(site).get("ga4")
    if not prop:
        return "nao_configurado", "GA4: o projeto não tem o ID da propriedade cadastrado."
    try:
        (cliente or _google_ga4)(prop)
    except Exception as ex:  # noqa: BLE001
        return _falha("ga4", ex)
    return "ok", f"GA4: relatório de 1 dia da propriedade {prop} respondeu."


def clarity(site, cliente=None):
    if not _ids(site).get("clarity"):
        return "nao_configurado", "Clarity: o projeto não tem o ID do Clarity cadastrado."
    token = _token_clarity(site["id"])
    if not token:
        return "erro", "Clarity: falta o token do projeto (deploy/clarity-token.sh grava no servidor)."
    try:
        codigo, _ = (cliente or _http)(CLARITY_URL, headers={"Authorization": "Bearer " + token})
    except Exception as ex:  # noqa: BLE001
        return _falha("clarity", ex)
    if codigo == 200:
        return "ok", "Clarity: a API respondeu com o token do projeto."
    if codigo == 429:
        return "ok", "Clarity: token aceito (a cota diária da API já foi usada hoje)."
    return "erro", f"Clarity: a API recusou o token (HTTP {codigo})."


def gmb(site, cliente=None):
    """Sem integração utilizável com o Perfil da Empresa no hub (hub/apps.py só serve a fsite05): nunca verde sem prova.
    Confere só o formato do CID; `cliente` fica na assinatura por simetria e não é usado."""
    cid = _ids(site).get("gmb_cid")
    if not cid:
        return "nao_configurado", "Google Meu Negócio: o projeto não tem o CID cadastrado."
    if not RE_GMB_CID.match(str(cid)):
        return "erro", "Google Meu Negócio: o CID cadastrado tem formato inválido."
    if cid == "sem_perfil":
        return "manual", "Google Meu Negócio: cadastrado como sem Perfil da Empresa; só o formato foi conferido, confirme no Perfil da Empresa."
    return "manual", "Google Meu Negócio: só o formato do CID foi conferido; confirme no Perfil da Empresa."


def pagespeed(site, cliente=None):
    try:
        r = psi.rodada(f"https://{site['dominio']}/", psi.chave(), cliente or _http_json)
    except Exception as ex:  # noqa: BLE001
        return _falha("pagespeed", ex)
    if r.get("nota") is None:
        return "erro", "PageSpeed: a resposta veio sem nota de desempenho."
    return "ok", f"PageSpeed: a home mede {r['nota']} de 100 no celular."


def repo(site, cliente=None):
    github = (site.get("repo") or {}).get("github")
    if not github:
        return "nao_configurado", "Repositório: o projeto não tem repositório cadastrado."
    try:
        codigo, _ = (cliente or _gh)(["api", f"repos/{github}"])
    except Exception as ex:  # noqa: BLE001
        return _falha("repo", ex)
    if codigo == 0:
        return "ok", f"Repositório: {github} responde no GitHub."
    return "erro", f"Repositório: o GitHub não achou {github} ou a conta do hub não tem acesso."


def deploy(site, cliente=None):
    url = f"https://{site['dominio']}/"
    try:
        codigo, _ = (cliente or _http)(url)
    except Exception as ex:  # noqa: BLE001
        return _falha("deploy", ex)
    if codigo == 200:
        return "ok", f"Deploy: {url} respondeu 200."
    return "erro", f"Deploy: {url} respondeu HTTP {codigo} (esperado 200)."


FUNCOES = {"gsc": gsc, "ga4": ga4, "clarity": clarity, "gmb": gmb, "pagespeed": pagespeed, "repo": repo, "deploy": deploy}


def verificar(site, fonte, cliente=None, agora=None):
    """Roda uma fonte, guarda o resultado e devolve (status, mensagem)."""
    if fonte not in FUNCOES:
        raise ValueError(f"fonte desconhecida: {fonte!r}")
    _arquivo(site["id"])  # id inválido falha antes de qualquer chamada
    try:
        status, msg = FUNCOES[fonte](site, cliente)
    except Exception as ex:  # noqa: BLE001
        status, msg = _falha(fonte, ex)
    _gravar(site["id"], fonte, status, msg, agora or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            id_da_fonte(site, fonte))
    return status, msg
