"""Guardrails do agente do X, em código (cada um tem teste em tests/test_x_salvos_guardrails.py).

1. SOMENTE LEITURA: `checar_requisicao`, `instalar`, `clicar_seguro`.
2. SESSÃO É DO USUÁRIO: só cookies exportados (`ler_cookies`); nenhum fluxo de login existe no pacote.
3. LIMITE DE TAXA E TETO: `LimiteTaxa`, `Rodada`, `sessao_caiu`, `ir`.
4. SEGREDOS: `mascarar`, `log`, `gravar_cookies` (600), `garantir_permissao`.
5. TERMOS: `AVISO_TERMOS`, `validar_max_itens` (≤ 500), `pode_coletar` (1 coleta por dia).
"""
import datetime as dt
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

NOMES_COOKIES = ("auth_token", "ct0", "twid")
CHAVES_DE_LOGIN = ("password", "senha", "username", "passwd", "pass", "email", "e-mail", "login", "user", "usuario", "usuário",
                   "otp", "totp", "2fa", "token_2fa", "codigo_2fa", "mfa", "phone", "telefone")
TETO_ITENS = 500

AVISO_TERMOS = (
    "AVISO: uso pessoal. Esta ferramenta lê só os salvos (bookmarks) da sua própria conta, pela sua sessão, em modo somente leitura; "
    "a busca pública fica fora deste agente. Sem scraping em massa: no máximo 500 itens por coleta e uma coleta por dia "
    "(--forcar serve para a carga inicial ou uma correção e também respeita o teto). "
    "Você é responsável por cumprir os termos de uso do X."
)


class EscritaBloqueada(Exception):
    """Tentativa de escrever no X (método, endpoint ou clique de ação)."""


class CookiesInvalidos(Exception):
    pass


class TetoAtingido(Exception):
    pass


class SessaoCaiu(Exception):
    pass


# ---------- 1. somente leitura ----------
METODOS_LEITURA = {"GET", "HEAD", "OPTIONS"}
DOMINIOS_API = ("x.com", "twitter.com")  # e qualquer subdomínio
PREFIXOS_API = ("/i/api/", "/2/", "/1.1/")
OPERACOES_ESCRITA = {  # GraphQL: último segmento do path (nome da operação), minúsculo, igualdade exata
    "createtweet", "createnotetweet", "deletetweet", "favoritetweet", "unfavoritetweet", "createretweet", "deleteretweet",
    "createbookmark", "deletebookmark", "createfriendship", "destroyfriendship", "followuser", "unfollowuser", "senddm",
}
PREFIXOS_REST = (  # REST: começo do path sem /i/api e sem /1.1 ou /2
    "friendships/create", "friendships/destroy", "favorites/create", "favorites/destroy", "statuses/update", "statuses/retweet",
    "dm/new", "blocks/create", "mutes/users/create", "users/report_spam", "report_story",
)
TELEMETRIA = ("/jot/", "client_event")  # POST de métrica do próprio site: aborta em silêncio
CLIQUES_PERMITIDOS = (  # o único clique do coletor: expandir respostas do fio
    'button:has-text("Mostrar mais respostas")', 'button:has-text("Show more replies")',
    'button:has-text("Mostrar mais respostas"), button:has-text("Show more replies")',
)


def _host_x(host):
    host = (host or "").lower().rstrip(".")  # x.com. (ponto final) é o mesmo host
    return any(host == d or host.endswith("." + d) for d in DOMINIOS_API)


def _telemetria(url):
    p = urlsplit(str(url))
    return _host_x(p.hostname) and any(m in unquote(p.path).lower() for m in TELEMETRIA)


def checar_requisicao(metodo, url):
    """Bloqueia método de escrita e endpoint de escrita de API; só olha host e path (nunca a query nem páginas de post/perfil)."""
    if str(metodo).upper() not in METODOS_LEITURA:
        raise EscritaBloqueada(f"método {str(metodo).upper()} bloqueado (somente leitura)")
    p = urlsplit(str(url))
    path = unquote(p.path).lower()
    if not _host_x(p.hostname) or not any(path.startswith(x) for x in PREFIXOS_API):
        return
    op = path.rstrip("/").rsplit("/", 1)[-1]
    if op in OPERACOES_ESCRITA:
        raise EscritaBloqueada(f"operação de escrita bloqueada ({op})")
    rest = path
    for pre in ("/i/api", "/1.1", "/2"):
        if rest.startswith(pre + "/"):
            rest = rest[len(pre):]
    rest = rest.lstrip("/")
    for m in PREFIXOS_REST:
        if rest.startswith(m):
            raise EscritaBloqueada(f"endpoint de escrita bloqueado ({m})")


def instalar(ctx, registrar=None):
    """Aborta toda requisição de escrita do contexto do navegador; registra só método e caminho (sem query nem corpo)."""
    def tratar(route, request=None):
        req = request or route.request
        try:
            checar_requisicao(req.method, req.url)
        except Exception as e:  # noqa: BLE001 — fail-closed: URL ilegível também aborta
            try:
                if not _telemetria(req.url):  # métrica do site: cai calada
                    parte = urlsplit(req.url)
                    reg = {"metodo": req.method, "caminho": parte.netloc + parte.path, "motivo": str(e)}
                    (registrar or (lambda r: log("bloqueado: " + json.dumps(r, ensure_ascii=False))))(reg)
            except Exception:  # noqa: BLE001
                pass
            route.abort()
            return
        route.continue_()
    ctx.route("**/*", tratar)


def clicar_seguro(page, seletor, pausa=None):
    """Clica em todos os elementos do seletor, se e só se for o de 'mostrar mais respostas'. Devolve quantos clicou."""
    if " ".join(seletor.split()) not in CLIQUES_PERMITIDOS:
        raise EscritaBloqueada(f"clique recusado: {seletor!r} não é o de mostrar mais respostas")
    n = 0
    for el in page.query_selector_all(seletor):
        try:
            el.click()
            n += 1
            if pausa:
                pausa()
        except Exception:  # noqa: BLE001 — botão que sumiu não derruba a coleta
            pass
    return n


# ---------- 2/4. sessão do usuário e segredos ----------
_SEGREDOS = set()


def registrar_segredos(valores):
    _SEGREDOS.update(str(v) for v in valores if v)


def _valores(cookies):
    if isinstance(cookies, dict):
        return list(cookies.values())
    return [c["value"] if isinstance(c, dict) else c for c in (cookies or [])]


def mascarar(texto, cookies=None):
    """Troca valores de cookie (os dados e os já registrados) por ***."""
    t = str(texto)
    for v in sorted({str(v) for v in _valores(cookies) if v} | _SEGREDOS, key=len, reverse=True):
        t = t.replace(v, "***")
    return t


def log(msg):
    print(f"{dt.datetime.now().isoformat(timespec='seconds')} {mascarar(msg)}", flush=True)


def descrever_erro(e):
    return mascarar(f"{type(e).__name__}: {str(e)[:150]}")


def log_erro(e, contexto=""):
    log(f"ERRO {contexto}{descrever_erro(e)}")


def garantir_permissao(caminho):
    """Arquivo de cookies mais aberto que 600: corrige e avisa (sem valor). True se corrigiu."""
    p = Path(caminho)
    if not p.exists() or not (stat.S_IMODE(p.stat().st_mode) & 0o077):
        return False
    os.chmod(p, 0o600)
    log(f"aviso: {p.name} estava mais aberto que 600; corrigido")
    return True


def ler_cookies(caminho):
    """Lista de cookies do playwright, só auth_token/ct0/twid. Rejeita arquivo com credencial de login."""
    p = Path(caminho)
    if not p.exists():
        return []
    garantir_permissao(p)
    try:
        c = json.loads(p.read_text())
    except ValueError:
        raise CookiesInvalidos("o arquivo de cookies não é um JSON válido") from None
    if not isinstance(c, dict):
        raise CookiesInvalidos("o arquivo de cookies deve ser um objeto com auth_token, ct0 e twid")
    if any(k.lower() in CHAVES_DE_LOGIN for k in c):
        raise CookiesInvalidos("o arquivo tem credencial de login (senha/usuário); exporte só os cookies da sessão")
    registrar_segredos(c.get(k) for k in NOMES_COOKIES)
    return [{"name": k, "value": c[k], "domain": ".x.com", "path": "/", "secure": True, "httpOnly": k == "auth_token",
             "sameSite": "None"} for k in NOMES_COOKIES if c.get(k)]


def gravar_cookies(caminho, cookies):
    """Grava só os três cookies, com 600 desde a criação (temporário + os.replace)."""
    p = Path(caminho)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dados = {k: cookies[k] for k in NOMES_COOKIES if cookies.get(k)}
    registrar_segredos(dados.values())
    tmp = p.with_name(p.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dados, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ---------- 3. taxa, teto, sessão ----------
class LimiteTaxa:
    """Intervalo mínimo entre navegações; relógio e sono injetáveis."""

    def __init__(self, intervalo_s, relogio=time.monotonic, dormir=time.sleep):
        self.intervalo, self.relogio, self.dormir, self.ultimo = intervalo_s, relogio, dormir, None

    def espera(self):
        if self.ultimo is not None:
            falta = self.intervalo - (self.relogio() - self.ultimo)
            if falta > 0:
                self.dormir(falta)
        self.ultimo = self.relogio()


class Rodada:
    """Teto de rolagens e de itens de uma rodada."""

    def __init__(self, max_paginas, max_itens):
        self.max_paginas, self.max_itens, self.paginas, self.itens = max_paginas, max_itens, 0, 0

    def rolar(self):
        if self.paginas >= self.max_paginas:
            raise TetoAtingido(f"teto de {self.max_paginas} rolagens")
        self.paginas += 1

    def item(self):
        if self.itens >= self.max_itens:
            raise TetoAtingido(f"teto de {self.max_itens} itens")
        self.itens += 1


def sessao_caiu(url):
    # visto em 11/10/2026: sem sessão o X manda para /i/jf/onboarding/web?redirect_after_login=...&mode=login
    path = urlsplit(str(url)).path.lower()
    return path.startswith(("/i/flow/", "/i/jf/")) or path.rstrip("/") in ("/login", "/logout")


def ir(page, url, taxa, **kw):
    """Única porta de navegação: respeita a taxa e nunca vai a página de login."""
    if sessao_caiu(url):
        raise SessaoCaiu("recusado navegar para página de login")
    taxa.espera()
    return page.goto(url, wait_until="domcontentloaded", **kw)


# ---------- 5. termos ----------
def validar_max_itens(n):
    n = int(n)
    if not 1 <= n <= TETO_ITENS:
        raise ValueError(f"max_itens deve ficar entre 1 e {TETO_ITENS} (veio {n})")
    return n


def pode_coletar(estado, hoje, forcar=False):
    """Uma coleta por dia; `--forcar` ignora."""
    return bool(forcar) or estado.get("ultima_coleta") != hoje
