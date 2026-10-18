"""Conexões com serviços de fora (tela /config/conexoes): catálogo, teste de cada uma e o último resultado.

`testar(nome, transporte=None) -> (ok, mensagem em português)`. O segredo vem do cofre (nome fixo por conexão, ex.:
`linear.token`); o que não é segredo (time, organização, projeto do Clarity) fica em <dados>/conexoes.json, junto do último
teste de cada conexão. Esse arquivo nunca tem segredo, e nenhuma mensagem leva o valor de um segredo nem texto cru de
resposta: só o código HTTP ou o tipo do erro, traduzido pela tabela ERROS.

Transporte (injetável; nos testes não há rede): `transporte(metodo, url, headers, corpo, timeout) -> (codigo, texto)`;
falha de rede/timeout sai como exceção. O Codex usa um comando: `comando(args, env) -> (codigo de saída, texto)`.
"""
import contextlib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import hub
from hub import cofre, config

ESTADO = "conexoes.json"
GOOGLE_ESCOPOS = ("https://www.googleapis.com/auth/webmasters.readonly "
                  "https://www.googleapis.com/auth/analytics.readonly")
CLARITY_URL = "https://www.clarity-ms.example/export-data/api/v1/project-live-insights?numOfDays=1"

# Campo: id = nome no cofre (segredo=True) ou chave em conexoes.json (segredo=False).
# tipo: password | text | textarea. obrigatorio: o teste não roda sem ele.
# env: nome da variável de ambiente que os módulos do hub já leem para esse valor (None = não é lido por variável).
_C = lambda id_, rotulo, segredo=True, tipo="password", dica="", obrigatorio=True, env=None: {  # noqa: E731
    "id": id_, "rotulo": rotulo, "segredo": segredo, "tipo": tipo, "dica": dica, "obrigatorio": obrigatorio, "env": env}

CONEXOES = {
    "linear": {
        "titulo": "Linear",
        "o_que_e": "Onde ficam as tarefas. O hub lê e cria issues com a chave pessoal.",
        "passos": ["No Linear, abra Settings > Account > Security & access.",
                   "Em Personal API keys, crie uma chave nova e copie.",
                   "Cole abaixo e informe a chave do time (ex.: ENG), se quiser conferir o time."],
        "campos": [_C("linear.token", "Chave pessoal da API", env="LINEAR_API_KEY"), _C("linear.time", "Chave do time (opcional)", False, "text", "ENG", False)],
        "teste": "Pergunta ao Linear quem é você (viewer) e lista os times.",
    },
    "linear_oauth": {
        "titulo": "Linear (app OAuth)",
        "o_que_e": "Conectar por app OAuth, com renovação automática da chave.",
        "passos": [], "campos": [], "em_breve": True, "teste": "",
    },
    "github": {
        "titulo": "GitHub",
        "o_que_e": "Repositórios, PRs e deploy dos projetos. Aqui vale o token fino; o GitHub App fica para depois.",
        "passos": ["No GitHub: Settings > Developer settings > Personal access tokens > Fine-grained tokens.",
                   "Escolha a organização, dê acesso aos repositórios e as permissões Contents e Pull requests (leitura e escrita).",
                   "Copie o token, cole abaixo e informe a organização."],
        "campos": [_C("github.token", "Token fino", env="GH_TOKEN"),
                   _C("github.org", "Organização", False, "text", "minha-org", True)],
        "teste": "Lê a organização pela API do GitHub (GET /orgs/<org>).",
    },
    "google": {
        "titulo": "Google (Search Console e GA4)",
        "o_que_e": "Conta de serviço que lê o Search Console e o GA4 dos projetos.",
        "passos": ["No Google Cloud, crie uma conta de serviço e gere uma chave JSON (Chaves > Adicionar chave).",
                   "Ative as APIs Search Console, Analytics Data e Analytics Admin no mesmo projeto do Cloud.",
                   "Cole abaixo o conteúdo do arquivo .json. Ele vai inteiro para o cofre; a tela só mostra o e-mail da conta."],
        "campos": [_C("google.conta_servico", "Conteúdo do JSON da conta de serviço", True, "textarea", '{"type": "service_account", ...}')],
        "teste": "Entra com a conta de serviço e lista as propriedades do Search Console e as contas do GA4 que ela enxerga.",
        "acesso": ["Search Console: abra a propriedade > Configurações > Usuários e permissões > Adicionar usuário, "
                   "cole o e-mail da conta de serviço e dê permissão Completa (ou Restrita para só ler).",
                   "GA4: Admin > Gerenciamento de acesso da propriedade > Adicionar usuário, cole o e-mail e dê o papel Visualizador."],
    },
    "clarity": {
        "titulo": "Clarity",
        "o_que_e": "Mapas de calor e sessões. O token é de cada projeto e fica guardado no projeto, não aqui.",
        "passos": ["No Clarity, abra o projeto > Settings > Data export > Generate new API token.",
                   "O token é de cada projeto e fica no servidor, em credenciais/clarity/<id do projeto>.json (deploy/clarity-token.sh grava); "
                   "ele não passa por esta tela nem pelo cofre.",
                   "Informe abaixo o id do projeto no hub para testar o token dele."],
        "campos": [_C("clarity.projeto", "Id do projeto no hub", False, "text", "loja-exemplo", True)],
        "teste": "Faz o export de 1 dia com o token do projeto informado. Cota diária esgotada (429) conta como erro.",
    },
    "pagespeed": {
        "titulo": "PageSpeed",
        "o_que_e": "Mede velocidade das páginas. A chave é opcional: sem ela vale a cota anônima, que acaba rápido.",
        "passos": ["No Google Cloud, ative a PageSpeed Insights API.", "Em Credenciais, crie uma chave de API e cole abaixo."],
        "campos": [_C("pagespeed.chave", "Chave da API (opcional)", obrigatorio=False, env="PAGESPEED_CHAVE")],
        "teste": "Mede https://example.com no celular.",
    },
    "dataforseo": {
        "titulo": "DataForSEO",
        "o_que_e": "Dados de SERP e palavras-chave. Paga e desligada por padrão: só gasta quando uma rotina a chama.",
        "passos": ["No painel da DataForSEO, abra API Access.", "Copie o login e a senha da API (não é a senha do site) e cole abaixo."],
        "campos": [_C("dataforseo.login", "Login", True, "password"), _C("dataforseo.senha", "Senha da API")],
        "teste": "Consulta appendix/user_data (grátis) e confere o saldo.", "paga": True,
    },
    "bing": {
        "titulo": "Bing Webmaster",
        "o_que_e": "Dados de busca do Bing.",
        "passos": ["Entre no Bing Webmaster Tools > Settings > API access.", "Clique em API Key, gere e copie."],
        "campos": [_C("bing.chave", "Chave da API do Webmaster")],
        "teste": "Lista os sites da conta (GetUserSites).",
    },
    "codex": {
        "titulo": "Codex",
        "o_que_e": "Executor de código da OpenAI usado em algumas tarefas.",
        "passos": ["Opção 1: crie uma chave em platform.openai-com.example > API keys e cole abaixo.",
                   "Opção 2: no servidor, rode `codex login --device-auth` e deixe o campo vazio."],
        "campos": [_C("codex.chave", "Chave da OpenAI (opcional se entrou pelo aparelho)", obrigatorio=False, env="OPENAI_API_KEY")],
        "teste": "Roda `codex --version` e uma chamada mínima.",
    },
}

# Tabela de erros conhecidos: quando a resposta vem com este código, o que dizer e o que fazer.
ERROS = {
    400: "recusou o pedido (400): a chave ou o formato do que foi gravado está errado. Confira o valor e grave de novo.",
    401: "recusou a credencial (401): o valor está errado, venceu ou foi revogado. Gere um novo no serviço e grave de novo aqui.",
    403: "reconheceu a credencial mas negou o acesso (403): falta permissão. Revise os acessos e escopos dela no serviço.",
    404: "não achou o que foi pedido (404): confira o nome ou id informado (organização, projeto, time).",
    429: "pediu para esperar (429): a cota acabou. Espere alguns minutos (ou até amanhã, se for cota diária) e teste de novo.",
}
ERRO_SERVIDOR = "está com problema do lado dele ({c}). Não é a sua configuração: tente de novo daqui a pouco."
ERRO_TIMEOUT = "não respondeu a tempo. Tente de novo; se repetir, confira se o servidor tem saída para a internet."
ERRO_REDE = "não foi alcançado pela rede. Confira a conexão e o DNS do servidor."
ERRO_OUTRO = "respondeu com um código inesperado ({c}). Tente de novo e, se repetir, avise quem cuida do hub."


def env():
    """{VARIAVEL: valor} das conexões, com os nomes que os módulos do hub já leem (LINEAR_API_KEY, GH_TOKEN, ...).
    Valor só sai daqui para quem vai pôr no ambiente de um processo: nunca para log, tela ou mensagem."""
    saida = {}

    def ler(nome):
        try:
            return cofre.ler(nome)
        except cofre.CofreError as ex:  # cofre fechado ou chave errada: segue sem esse valor, dizendo por quê (a mensagem não tem valor)
            if not avisou:
                print(f"conexoes.env: o cofre não abriu ({ex}); as variáveis das conexões ficam de fora.", file=sys.stderr)
                avisou.append(1)
            return None
    avisou = []
    for d in CONEXOES.values():
        for c in d["campos"]:
            if c["segredo"] and c["env"]:
                v = ler(c["id"])
                if v:
                    saida[c["env"]] = v
    login, senha = ler("dataforseo.login"), ler("dataforseo.senha")
    if login and senha:
        import base64
        saida["DATAFORSEO_AUTH"] = base64.b64encode(f"{login}:{senha}".encode()).decode()
    return saida


def titulo(nome):
    return CONEXOES[nome]["titulo"]


def erro_http(nome, codigo):
    base = ERROS.get(codigo)
    if base is None:
        base = ERRO_SERVIDOR.format(c=codigo) if codigo >= 500 else ERRO_OUTRO.format(c=codigo)
    return f"{titulo(nome)} {base}"


def erro_excecao(nome, ex):
    """Só o TIPO da exceção decide a frase: o texto dela pode ter URL com chave."""
    if isinstance(ex, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)) or (
            isinstance(ex, urllib.error.URLError) and isinstance(ex.reason, (TimeoutError, socket.timeout))):
        return f"{titulo(nome)} {ERRO_TIMEOUT}"
    if isinstance(ex, FileNotFoundError):
        return f"{titulo(nome)}: o programa não está instalado neste servidor."
    if isinstance(ex, (OSError, urllib.error.URLError)):
        return f"{titulo(nome)} {ERRO_REDE}"
    return f"{titulo(nome)}: o teste falhou ({type(ex).__name__}). Tente de novo."


# ---------------- estado (último teste + campos que não são segredo) ----------------
def _arquivo():
    return hub.data() / ESTADO


_trava = threading.RLock()


@contextlib.contextmanager
def _exclusivo():
    """Uma escrita por vez: entre threads (RLock) e entre processos (flock no arquivo de trava ao lado do estado)."""
    with _trava:
        _arquivo().parent.mkdir(parents=True, exist_ok=True)
        with open(_arquivo().with_suffix(".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def ler_estado():
    try:
        d = json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"testes": {}, "campos": {}}
    if not isinstance(d, dict):
        return {"testes": {}, "campos": {}}
    return {"testes": d.get("testes") if isinstance(d.get("testes"), dict) else {},
            "campos": d.get("campos") if isinstance(d.get("campos"), dict) else {}}


def _gravar_estado(d):
    arq = _arquivo()
    arq.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=arq.parent, prefix=".conexoes.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, arq)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def ultimo_teste(nome):
    t = ler_estado()["testes"].get(nome)
    return t if isinstance(t, dict) and "ok" in t else None


def campo(id_):
    """Valor de um campo que não é segredo (vazio se não há)."""
    v = ler_estado()["campos"].get(id_)
    if v:
        return str(v)
    return str(config.get("github.org") or "") if id_ == "github.org" else ""


def guardar_campos(novos):
    """Grava vários campos sem segredo de uma vez (vazio apaga)."""
    with _exclusivo():
        d = ler_estado()
        for id_, valor in novos.items():
            if valor:
                d["campos"][id_] = valor
            else:
                d["campos"].pop(id_, None)
        _gravar_estado(d)


def guardar_campo(id_, valor):
    guardar_campos({id_: valor})


def _registrar(nome, ok, msg, agora=None):
    with _exclusivo():
        d = ler_estado()
        d["testes"][nome] = {"ok": bool(ok), "mensagem": msg,
                             "em": agora or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _gravar_estado(d)


# ---------------- Google: JSON da conta de serviço ----------------
def validar_conta_servico(texto):
    """(conta, None) ou (None, erro em português). Aceita só JSON de conta de serviço com e-mail e chave privada."""
    try:
        sa = json.loads(texto or "")
    except ValueError:
        return None, "Isso não é um JSON. Cole o conteúdo inteiro do arquivo baixado do Google Cloud."
    if not isinstance(sa, dict) or sa.get("type") != "service_account":
        return None, 'Esse JSON não é de uma conta de serviço (falta "type": "service_account"). Gere a chave em Contas de serviço.'
    email = sa.get("client_email")
    if not isinstance(email, str) or "@" not in email or any(c.isspace() for c in email):
        return None, "O JSON não tem um client_email válido."
    chave = sa.get("private_key")
    if not isinstance(chave, str) or "PRIVATE KEY" not in chave:
        return None, "O JSON não tem a chave privada (private_key). Gere uma chave nova do tipo JSON."
    return sa, None


def salvar_conta_servico(texto, por, ip=""):
    """Valida e manda o JSON inteiro, compactado em uma linha, para o cofre. (ok, mensagem); nunca repete o conteúdo."""
    sa, erro = validar_conta_servico(texto)
    if erro:
        return False, erro
    cofre.definir("google.conta_servico", json.dumps(sa, separators=(",", ":")), por=por, ip=ip)
    return True, f"Conta de serviço {sa['client_email']} guardada no cofre."


def email_conta_servico():
    """E-mail da conta de serviço no cofre (não é segredo), ou None."""
    try:
        sa, erro = validar_conta_servico(cofre.ler("google.conta_servico"))
    except cofre.CofreError:
        return None
    return None if erro else sa["client_email"]


# ---------------- transportes padrão ----------------
class _SemRedirect(urllib.request.HTTPRedirectHandler):
    """Recusa 3xx: o cabeçalho Authorization (ou a chave na URL) nunca segue para outro endereço. O 3xx volta como código."""

    def redirect_request(self, *a, **k):
        return None


def _http(metodo, url, headers=None, corpo=None, timeout=30):
    req = urllib.request.Request(url, data=corpo, headers=headers or {}, method=metodo)
    try:
        with urllib.request.build_opener(_SemRedirect).open(req, timeout=timeout) as r:
            return r.status, r.read(500000).decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ""


def _comando(args, env=None):
    base = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}  # ambiente mínimo: nada do resto do servidor
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=120, env={**base, **(env or {})})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _json(texto):
    try:
        d = json.loads(texto)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


class _Falta(Exception):
    """Falta um campo obrigatório: a mensagem já é para o usuário."""


def _segredo(nome, rotulo, obrigatorio=True):
    try:
        v = cofre.ler(nome)
    except cofre.CofreError as ex:
        raise _Falta(f"O cofre não abriu: {ex}") from None
    if not v and obrigatorio:
        raise _Falta(f"Falta {rotulo}: grave no cartão e teste de novo.")
    return v or ""


# ---------------- testes (cada um devolve (ok, mensagem)) ----------------
def _linear_tipo(d):
    """401 (autenticação), 429 (limite) ou None, pelo extensions.type dos erros GraphQL (qualquer status HTTP)."""
    tipos = {str(((x.get("extensions") or {}) if isinstance(x, dict) else {}).get("type", "")).lower().replace("_", "").replace(" ", "")
             for x in (d.get("errors") if isinstance(d.get("errors"), list) else [])}
    if any("ratelimit" in x for x in tipos):
        return 429
    if any("auth" in x for x in tipos):
        return 401
    return None


def _t_linear(t):
    token = _segredo("linear.token", "a chave pessoal")
    q = json.dumps({"query": "{ viewer { id name } teams { nodes { key } } }"}).encode()
    codigo, texto = t("POST", "https://api.linear.app/graphql", {"Authorization": token, "Content-Type": "application/json"}, q, 30)
    d = _json(texto)
    viewer = (d.get("data") or {}).get("viewer") if isinstance(d.get("data"), dict) else None
    if codigo != 200 or not isinstance(viewer, dict) or not viewer.get("id"):
        tipo = _linear_tipo(d)  # o Linear real pode responder erro GraphQL com status 400
        if tipo:
            return False, erro_http("linear", tipo)
        if codigo != 200:
            return False, erro_http("linear", codigo)
        return False, "Linear: a resposta veio sem o usuário e sem erro de autenticação. Tente de novo; se repetir, confira a chave."
    times = [n.get("key") for n in ((d["data"].get("teams") or {}).get("nodes") or []) if isinstance(n, dict)]
    time_ = campo("linear.time")
    if time_ and time_ not in times:
        return False, f"Linear: a chave funciona, mas não enxerga o time {time_}. Confira a chave do time ou o acesso dela."
    return True, f"Linear: chave aceita (usuário {viewer.get('name') or 'sem nome'}); enxerga {len(times)} time(s)."


def _t_github(t):
    token = _segredo("github.token", "o token fino")
    org = campo("github.org")
    if not org:
        raise _Falta("Falta a organização: informe no cartão e teste de novo.")
    codigo, _ = t("GET", "https://api.github.com/orgs/" + urllib.parse.quote(org, safe=""),
                  {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "krohnos-hub"}, None, 30)
    if codigo != 200:
        return False, erro_http("github", codigo)
    return True, f"GitHub: token aceito e a organização {org} respondeu."


def _assinar_rs256(sa, escopos, agora=None):
    """JWT de troca por token da conta de serviço (RS256). A chave privada só existe dentro desta função."""
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")
    agora = int(agora or time.time())
    cab = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    corpo = b64(json.dumps({"iss": sa["client_email"], "scope": escopos, "aud": "https://oauth2.googleapis.com/token",
                            "iat": agora, "exp": agora + 3600}).encode())
    chave = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = chave.sign(cab + b"." + corpo, padding.PKCS1v15(), hashes.SHA256())
    return (cab + b"." + corpo + b"." + b64(sig)).decode()


def _t_google(t):
    sa, erro = validar_conta_servico(_segredo("google.conta_servico", "o JSON da conta de serviço"))
    if erro:
        return False, "Google: o JSON guardado no cofre não é uma conta de serviço válida. Cole de novo o arquivo baixado do Google Cloud."
    try:
        jwt = _assinar_rs256(sa, GOOGLE_ESCOPOS)
    except Exception as ex:  # noqa: BLE001 (a mensagem pode ter pedaço da chave: só o tipo)
        return False, f"Google: a chave privada da conta de serviço não abriu ({type(ex).__name__}). Gere uma chave nova."
    corpo = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt}).encode()
    codigo, texto = t("POST", "https://oauth2.googleapis.com/token", {"Content-Type": "application/x-www-form-urlencoded"}, corpo, 30)
    if codigo != 200:
        return False, erro_http("google", codigo)
    token = _json(texto).get("access_token")
    if not token:
        return False, erro_http("google", 401)
    auth = {"Authorization": "Bearer " + token}
    c1, t1 = t("GET", "https://www.googleapis.com/webmasters/v3/sites", auth, None, 30)
    if c1 != 200:
        return False, erro_http("google", c1) + " (Search Console)"
    c2, t2 = t("GET", "https://analyticsadmin.googleapis.com/v1beta/accountSummaries", auth, None, 30)
    if c2 != 200:
        return False, erro_http("google", c2) + " (GA4)"
    gsc = len(_json(t1).get("siteEntry") or [])
    ga4 = sum(len(c.get("propertySummaries") or []) for c in (_json(t2).get("accountSummaries") or []) if isinstance(c, dict))
    if not gsc and not ga4:
        return False, (f"Google: a conta {sa['client_email']} entrou, mas ainda não enxerga nada. "
                       "Dê acesso a ela no Search Console e no GA4 (passo a passo no cartão).")
    return True, f"Google: a conta de serviço enxerga {gsc} propriedade(s) no Search Console e {ga4} no GA4."


def _t_clarity(t):
    from hub import verificar_fontes
    projeto = campo("clarity.projeto")
    if not projeto:
        raise _Falta("Falta o id do projeto: informe no cartão e teste de novo.")
    token = verificar_fontes._token_clarity(projeto) if verificar_fontes.RE_ID.match(projeto) else None
    if not token:
        return False, f"Clarity: não há token gravado para o projeto {projeto}. Grave o token do projeto no servidor."
    codigo, _ = t("GET", CLARITY_URL, {"Authorization": "Bearer " + token}, None, 30)
    if codigo != 200:
        return False, erro_http("clarity", codigo)
    return True, f"Clarity: o export de 1 dia do projeto {projeto} respondeu."


def _t_pagespeed(t):
    chave = _segredo("pagespeed.chave", "a chave", obrigatorio=False)
    q = [("url", "https://example.com"), ("strategy", "mobile"), ("category", "performance")] + ([("key", chave)] if chave else [])
    codigo, texto = t("GET", "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + urllib.parse.urlencode(q), {}, None, 120)
    if codigo != 200:
        return False, erro_http("pagespeed", codigo)
    nota = (((_json(texto).get("lighthouseResult") or {}).get("categories") or {}).get("performance") or {}).get("score")
    if nota is None:
        return False, "PageSpeed: a resposta veio sem nota de desempenho."
    return True, f"PageSpeed: example.com mede {round(nota * 100)} de 100 no celular" + ("." if chave else " (sem chave: cota anônima).")


def _t_dataforseo(t):
    import base64
    login, senha = _segredo("dataforseo.login", "o login"), _segredo("dataforseo.senha", "a senha da API")
    auth = base64.b64encode(f"{login}:{senha}".encode()).decode()
    codigo, texto = t("GET", "https://api.dataforseo.com/v3/appendix/user_data", {"Authorization": "Basic " + auth}, None, 30)
    if codigo != 200:
        return False, erro_http("dataforseo", codigo)
    d = _json(texto)
    sc = d.get("status_code")
    if sc not in (None, 20000):
        if isinstance(sc, int) and 40100 <= sc < 40200:
            return False, erro_http("dataforseo", 401)
        if isinstance(sc, int) and 40200 <= sc < 40300:
            return False, "DataForSEO: saldo insuficiente ou limite da conta atingido. Recarregue o saldo ou espere o limite renovar."
        return False, f"DataForSEO: a API recusou o pedido (status {sc}). Confira a conta no painel da DataForSEO."
    saldo = (((d.get("tasks") or [{}])[0].get("result") or [{}])[0].get("money") or {}).get("balance")
    return True, "DataForSEO: login aceito" + (f"; saldo US$ {saldo:.2f}." if isinstance(saldo, (int, float)) else ".")


def _t_bing(t):
    chave = _segredo("bing.chave", "a chave da API")
    codigo, texto = t("GET", "https://ssl.bing.com/webmaster/api.svc/json/GetUserSites?" + urllib.parse.urlencode({"apikey": chave}),
                      {}, None, 30)
    if codigo != 200:
        return False, erro_http("bing", codigo)
    sites_ = _json(texto).get("d")
    return True, f"Bing: chave aceita; a conta tem {len(sites_) if isinstance(sites_, list) else 0} site(s)."


_CODEX_401 = re.compile(r"(?im)\b401 unauthorized\b|\bstatus(?: code)?[:= ]+401\b|\binvalid_api_key\b|\bincorrect api key provided\b")
_CODEX_429 = re.compile(r"(?im)\b429 too many requests\b|\bstatus(?: code)?[:= ]+429\b|\brate_limit_exceeded\b|\binsufficient_quota\b")


def _t_codex(comando):
    chave = _segredo("codex.chave", "a chave", obrigatorio=False)
    env = {"OPENAI_API_KEY": chave} if chave else {}
    rc, texto = comando(["codex", "--version"], env)
    if rc != 0:
        return False, "Codex: `codex --version` falhou. Confira se o Codex está instalado neste servidor."
    versao = " ".join(texto.split())[:40]
    rc, saida = comando(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "Responda apenas: ok"], env)
    if rc != 0:
        if _CODEX_401.search(saida):
            return False, erro_http("codex", 401)
        if _CODEX_429.search(saida):
            return False, erro_http("codex", 429)
        return False, f"Codex: instalado ({versao}), mas a chamada mínima falhou (código {rc}). Confira a chave ou rode `codex login --device-auth`."
    return True, f"Codex: instalado ({versao}) e a chamada mínima respondeu."


_TESTES = {"linear": _t_linear, "github": _t_github, "google": _t_google, "clarity": _t_clarity, "pagespeed": _t_pagespeed,
           "dataforseo": _t_dataforseo, "bing": _t_bing, "codex": _t_codex}


def _segredos_da(nome):
    saida = []
    for c in CONEXOES[nome]["campos"]:
        if c["segredo"]:
            try:
                v = cofre.ler(c["id"])
            except cofre.CofreError:
                v = None
            if v:
                saida.append(v)
    return saida


def _limpar(msg, valores):
    for v in sorted(valores, key=len, reverse=True):
        if v and len(v) >= 4:
            msg = msg.replace(v, "••••")
    return msg


def testar(nome, transporte=None, agora=None):
    """Roda o teste da conexão, guarda o resultado em conexoes.json e devolve (ok, mensagem em português)."""
    if nome not in CONEXOES:
        return False, "Conexão desconhecida."
    if CONEXOES[nome].get("em_breve") or nome not in _TESTES:
        return False, f"{titulo(nome)}: ainda não disponível (em breve)."
    try:
        ok, msg = _TESTES[nome](transporte or (_comando if nome == "codex" else _http))
    except _Falta as ex:
        ok, msg = False, str(ex)
    except Exception as ex:  # noqa: BLE001
        ok, msg = False, erro_excecao(nome, ex)
    msg = _limpar(msg, _segredos_da(nome))
    _registrar(nome, ok, msg, agora)
    return ok, msg


# ---------------- gravação dos campos de um cartão ----------------
def salvar(nome, campos, por, ip=""):
    """(ok, mensagem). Tudo é validado antes de gravar qualquer campo (ou grava tudo, ou nada de erro de validação).
    Segredo vazio = mantém o do cofre; campo `obrigatorio` sem valor novo nem antigo recusa o cartão."""
    if nome not in CONEXOES or CONEXOES[nome].get("em_breve"):
        return False, "Conexão desconhecida."
    segredos, simples, rotulos = {}, {}, []
    for c in CONEXOES[nome]["campos"]:
        valor = campos.get(c["id"]) or ""
        valor = valor if c["tipo"] == "textarea" else valor.strip()
        if c["segredo"]:
            if valor:
                if c["id"] == "google.conta_servico":
                    sa, erro = validar_conta_servico(valor)
                    if erro:
                        return False, erro
                    valor = json.dumps(sa, separators=(",", ":"))
                elif any(ch in valor for ch in "\r\n\0"):
                    return False, f"{c['rotulo']}: o valor não pode ter quebra de linha."
                segredos[c["id"]] = valor
                rotulos.append(c["rotulo"])
            elif c["obrigatorio"]:
                try:
                    tem = bool(cofre.ler(c["id"]))
                except cofre.CofreError as ex:
                    return False, str(ex)
                if not tem:
                    return False, f"Falta {c['rotulo']}: nada foi gravado."
        else:
            if not valor and c["obrigatorio"]:  # obrigatório vazio mantém o que já está (não apaga)
                if not campo(c["id"]):
                    return False, f"Falta {c['rotulo']}: nada foi gravado."
                continue
            simples[c["id"]] = valor
            rotulos.append(c["rotulo"])
    try:
        antes = {id_: cofre.ler(id_) for id_ in segredos}
    except cofre.CofreError as ex:
        return False, str(ex)
    feitos = []
    try:
        for id_, valor in segredos.items():
            cofre.definir(id_, valor, por=por, ip=ip)
            feitos.append(id_)
    except cofre.CofreError as ex:
        for id_ in feitos:  # desfaz o que já entrou: nada de estado parcial
            try:
                if antes[id_]:
                    cofre.definir(id_, antes[id_], por=por, ip=ip)
                else:
                    cofre.apagar(id_, por=por, ip=ip)
            except cofre.CofreError:
                pass
        return False, f"{ex} Nada foi gravado."
    if simples:
        guardar_campos(simples)
    return True, f"{titulo(nome)}: " + (f"guardado ({', '.join(rotulos)})." if rotulos else "nada mudou.")
