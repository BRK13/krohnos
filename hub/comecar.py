"""Assistente de primeiro acesso do painel (/comecar): dono -> GitHub -> Linear -> primeiro projeto -> primeira conta Claude.

Quando liga (instalação nova): existe krohnos.toml, não existe o marcador <dados>/comecar.concluido.
Sem krohnos.toml (config legada, a nossa VPS) o assistente NUNCA liga: só o login (/entrar) de sempre.
Dois estados:
- ABERTO (toml sem [dono] email e nenhum usuário do painel): toda rota (menos /saude e /comecar) leva a /comecar. Quem
  abrir primeiro NÃO vira dono: o painel gera um token de configuração de uso único (<segredos>/comecar.token, 0600),
  mostrado só no terminal e no log do contêiner. Sem o token, /comecar mostra a tela neutra "Configuração pendente";
  com o token errado recusa (5 erros = bloqueio de 15 min, gravado em disco); com o certo, o passo 1 cria o usuário dono
  (hub/usuarios.py), grava o e-mail no toml e queima o token. O painel já entra com ele.
- depois do dono: as rotas pedem login (/entrar) e /comecar passa a exigir a sessão de um dono até o marcador ser
  gravado ("concluir" ou o último passo). Marcador gravado: /comecar = 404.
Se já existe usuário, ou a senha antiga ainda não migrada (PAINEL_SENHA no ambiente ou painel.env), o assistente nunca
fica aberto ao público.

CSRF: cookie duplo. A página (GET) define o cookie HttpOnly SameSite=Strict `krohnos_comecar` com um valor aleatório e o
repete no campo oculto `csrf`; o POST só vale se os dois coincidem (comparação em tempo constante) e se o Origin, quando
vem, tem o mesmo host do cabeçalho Host. Não depende de sessão (ainda não existe no passo 1).
Segredos (token do GitHub e do Linear) vão só para arquivo 0600 na pasta de segredos; o HTML mostra só os 4 últimos.
Gravações: tmp + os.replace (init.escrever_atomico).
"""
import hmac
import json
import re
import secrets
import sys
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path

from hub import config, init, sites, ui, usuarios

MARCADOR = "comecar.concluido"
COOKIE = "krohnos_comecar"
PASSOS = ("dono", "github", "linear", "projeto", "conta")
OPCIONAIS = ("github", "linear", "conta")
TITULOS = {"dono": "Quem é você", "github": "GitHub", "linear": "Linear", "projeto": "Seu primeiro projeto",
           "conta": "Conta do Claude"}
RE_GH_TOKEN = re.compile(r"^[A-Za-z0-9_]{20,255}$")
RE_LINEAR_KEY = re.compile(r"^[A-Za-z0-9_-]{20,255}$")
RE_TIME = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
RE_CONTA = re.compile(r"^[1-9]$")
SENHA_MIN = init.SENHA_MIN
ARQ_TOKEN = "comecar.token"
ARQ_TENTATIVAS = "comecar.tentativas"
MAX_TENTATIVAS = 5
BLOQUEIO_S = 900
_trava = threading.RLock()  # todo ler-alterar-gravar do toml e o passo do dono passam por ele
e = ui.e


class Erro(ValueError):
    pass


# ---------- estado ----------

def _dados():
    return Path(config.get("caminhos.dados")).expanduser()


def _segredos():
    return Path(config.get("caminhos.segredos")).expanduser()


def concluido():
    return (_dados() / MARCADOR).exists()


def ativo():
    """Assistente existe: krohnos.toml presente e marcador ausente. Config legada (sem toml) nunca liga."""
    return config.arquivo() is not None and not concluido()


def _tem_usuario():
    """Já há quem entre: um usuário do painel, ou a senha antiga (env/painel.env) que vira dono na subida do painel."""
    return usuarios.contar() > 0 or bool(usuarios.senha_antiga())


def aberto():
    """Sem dono configurado: assistente ativo, sem e-mail do dono no toml e nenhum usuário. Público."""
    return ativo() and not config.definido("dono.email") and not _tem_usuario()


def primeiro_passo():
    return "dono" if not config.definido("dono.email") else "github"


# ---------- token de configuração (uso único) ----------

def token_config():
    """Token de uso único do primeiro acesso: lê ou cria <segredos>/comecar.token (0600). Só existe enquanto aberto()."""
    arq = _segredos() / ARQ_TOKEN
    with _trava:
        try:
            v = arq.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            pass
        v = secrets.token_urlsafe(24)
        init._pasta_privada(arq.parent)
        init.escrever_atomico(arq, v + "\n", 0o600)
        return v


_anunciado = False


def anunciar(saida=None, uma_vez=False):
    """Mostra o token no terminal/log do contêiner enquanto o assistente está aberto. Devolve o token, ou None.
    uma_vez=True (chamado a cada GET do /comecar): só imprime se este processo ainda não imprimiu."""
    global _anunciado
    if not aberto() or (uma_vez and _anunciado):
        return None
    _anunciado = True
    try:
        v = token_config()
    except OSError as ex:
        print(f"painel: não consegui gravar o token de configuração ({type(ex).__name__})", file=saida or sys.stderr, flush=True)
        return None
    print(f"\n  {ui.NOME_PRODUTO}: configuração pendente. Para abrir /comecar use este token (uso único):\n"
          f"  {v}\n", file=saida or sys.stderr, flush=True)
    return v


def _tentativas():
    try:
        d = json.loads((_segredos() / ARQ_TENTATIVAS).read_text(encoding="utf-8"))
        return int(d.get("falhas", 0)), float(d.get("ate", 0))
    except (OSError, ValueError, AttributeError):
        return 0, 0.0


def _gravar_tentativas(falhas, ate):
    init.escrever_atomico(_segredos() / ARQ_TENTATIVAS, json.dumps({"falhas": falhas, "ate": ate}), 0o600)


def verificar_token(informado):
    """(ok, mensagem). Comparação em tempo constante; MAX_TENTATIVAS erros seguidos bloqueiam por BLOQUEIO_S."""
    with _trava:
        falhas, ate = _tentativas()
        agora = time.time()
        if ate > agora:
            return False, f"Muitas tentativas. Tente de novo em {int((ate - agora) // 60) + 1} min."
        if ate and ate <= agora:
            falhas = 0
        try:  # sem gravar o contador o limite não vale: falha fechada
            esperado = token_config()
            if informado and hmac.compare_digest(str(informado).strip().encode(), esperado.encode()):
                _gravar_tentativas(0, 0)
                return True, ""
            falhas += 1
            _gravar_tentativas(falhas, agora + BLOQUEIO_S if falhas >= MAX_TENTATIVAS else 0)
        except OSError:
            return False, "Configuração pendente: use o token exibido na instalação."
        return False, "Token incorreto."


def invalidar_token():
    for nome in (ARQ_TOKEN, ARQ_TENTATIVAS):
        try:
            (_segredos() / nome).unlink()
        except OSError:
            pass


# ---------- csrf (cookie duplo) ----------

def token_novo():
    return secrets.token_urlsafe(24)


def cookie_do(cabecalho):
    for parte in (cabecalho or "").split(";"):
        nome, _, valor = parte.strip().partition("=")
        if nome == COOKIE:
            return valor
    return ""


def set_cookie(token, seguro=False):
    """seguro=True (X-Forwarded-Proto: https do proxy TLS) acrescenta Secure."""
    return f"{COOKIE}={token}; Path=/comecar; HttpOnly; SameSite=Strict; Max-Age=7200" + ("; Secure" if seguro else "")


def csrf_ok(cookie, campo):
    return bool(cookie) and bool(campo) and hmac.compare_digest(cookie.encode(), campo.encode())


def origem_ok(origin, host):
    """Sem Origin (alguns navegadores em POST same-origin) vale o cookie SameSite; com Origin, o host tem de bater."""
    if not origin:
        return True
    try:
        return urllib.parse.urlsplit(origin).netloc == (host or "")
    except ValueError:  # Origin malformado (ex.: colchete sem par)
        return False


# ---------- gravação ----------

def _ler_toml():
    arq = config.arquivo()
    return tomllib.loads(arq.read_text(encoding="utf-8")) if arq else {}


def _gravar_toml(alterar):
    """Lê o toml, aplica alterar(dict), valida (sem segredo, chaves conhecidas) e grava de forma atômica."""
    with _trava:
        arq = config.arquivo()
        if arq is None:
            raise Erro("krohnos.toml não encontrado")
        d = _ler_toml()
        alterar(d)
        texto = init.montar_toml(d)
        try:
            init.validar_toml(texto)
        except config.ConfigError as ex:
            raise Erro(str(ex))
        init.escrever_atomico(arq, texto)
        config.recarregar()


def _gravar_env(arq, chave, valor):
    """Troca/insere CHAVE=valor num .env da pasta de segredos, 0600, atômico, preservando as outras linhas."""
    init._pasta_privada(arq.parent)
    linhas = []
    if arq.is_file():
        linhas = [x for x in arq.read_text(encoding="utf-8").splitlines() if not x.startswith(f"{chave}=")]
    linhas.append(f"{chave}={valor}")
    init.escrever_atomico(arq, "\n".join(linhas) + "\n", 0o600)


def _ultimos4(arq, chave):
    try:
        for x in arq.read_text(encoding="utf-8").splitlines():
            if x.startswith(f"{chave}="):
                v = x.split("=", 1)[1].strip()
                return v[-4:] if len(v) >= 8 else ""
    except OSError:
        pass
    return ""


def concluir():
    init.escrever_atomico(_dados() / MARCADOR, "concluido\n")


# ---------- passos: cada um valida, grava e devolve a mensagem ----------

def passo_dono(c):
    nome = (c.get("nome") or "").strip()[:80]
    email = init._email(c.get("email"))
    senha = c.get("senha") or ""
    with _trava:
        if config.definido("dono.email"):
            raise Erro("o dono já está cadastrado")
        publico = aberto()
        if publico:  # nenhum usuário existe: este passo cria o dono, e só com o token de configuração
            ok, msg = verificar_token(c.get("token_config"))
            if not ok:
                raise Erro(msg)
            init._senha(senha)  # mesmo mínimo e mesmas regras do krohnos init
            if senha != (c.get("senha2") or ""):
                raise Erro("as duas senhas não são iguais")
            try:
                usuarios.criar(email, senha, "dono")
            except ValueError as ex:
                raise Erro(str(ex))
            invalidar_token()  # uso único: queimado assim que o dono existe

        def altera(d):
            d.setdefault("dono", {}).update({"nome": nome or "Dono", "email": email})
        _gravar_toml(altera)  # o e-mail por último: é ele que fecha o modo aberto
    return "Dono cadastrado. Daqui em diante o painel pede e-mail e senha."


def passo_github(c):
    org = init._org(c.get("org"))
    token = (c.get("token") or "").strip()
    if token and not RE_GH_TOKEN.match(token):
        raise Erro("o token do GitHub tem formato inesperado")
    if not org and not token:
        raise Erro("informe a organização ou o token, ou toque em pular")
    if token:
        _gravar_env(_segredos() / "github.env", "GH_TOKEN", token)
    if org:
        _gravar_toml(lambda d: d.setdefault("github", {}).update({"org": org}))
    return "GitHub salvo."


def passo_linear(c):
    time = (c.get("time") or "").strip().upper()
    chave = (c.get("chave") or "").strip()
    if time and not RE_TIME.match(time):
        raise Erro("a chave do time do Linear tem 2 a 10 letras ou números (ex.: EXE)")
    if chave and not RE_LINEAR_KEY.match(chave):
        raise Erro("a chave de API do Linear tem formato inesperado")
    if not time and not chave:
        raise Erro("informe o time ou a chave de API, ou toque em pular")
    if chave:
        _gravar_env(_segredos() / "linear.env", "LINEAR_API_KEY", chave)
    if time or chave:
        def altera(d):
            if time:
                d.setdefault("linear", {}).update({"time": time})
            if chave:  # chave salva = Linear liga como fila; só o time não basta (sem chave nada conversa com ele)
                d.setdefault("issues", {})["provedor"] = "linear"
        _gravar_toml(altera)
    return "Linear salvo."


def _pular_linear():
    """Sem Linear as tarefas ficam na fila própria do hub; não rebaixa quem já escolheu o Linear nem quem tem a chave
    salva (pular o passo de novo desligaria o Linear calado)."""
    from hub import linear
    if linear._segredo("LINEAR_API_KEY") or linear._segredo("LINEAR_APP2_CLIENT_ID"):
        return

    def altera(d):
        if d.get("issues", {}).get("provedor") != "linear":
            d.setdefault("issues", {})["provedor"] = "local"
    _gravar_toml(altera)


def passo_projeto(c):
    dom = init._host(c.get("dominio"))
    pid = init._id(c.get("id") or dom.split(".")[0])
    nome = (c.get("nome") or "").strip()[:80] or pid.replace("-", " ").title()
    org = config.get("github.org")
    repo = init._repo(c.get("repo"), org)
    base = (c.get("base") or "main").strip() or "main"
    destino = _dados() / "projetos" / f"{pid}.json"
    if destino.exists():
        raise Erro(f"já existe um projeto com o id {pid}")
    try:
        init.gravar_projeto(_dados(), init.montar_projeto(pid, dom, repo, nome, base))
    except init.InitErro as ex:
        raise Erro(str(ex))
    return f"Projeto {pid} salvo."


def passo_conta(c):
    n = (c.get("conta") or "").strip()
    if not RE_CONTA.match(n):
        raise Erro("o número da conta é de 1 a 9")

    def altera(d):
        ag = d.setdefault("agentes", {})
        contas = [str(x) for x in ag.get("contas", config.SECOES["agentes"]["contas"])]
        if n not in contas:
            contas.append(n)
        ag["contas"] = contas
        faixas = dict(ag.get("faixas", {}))
        faixas.setdefault(n, "construtor")
        ag["faixas"] = faixas
    _gravar_toml(altera)
    return f"Conta {n} registrada. Falta entrar nela (comando na tela)."


FUNCOES = {"dono": passo_dono, "github": passo_github, "linear": passo_linear, "projeto": passo_projeto, "conta": passo_conta}


def executar(passo, campos):
    """('ok'|'erro', mensagem, próximo passo ou None quando concluiu). Passo opcional com acao=pular só avança."""
    if passo not in PASSOS:
        return "erro", "Passo desconhecido.", primeiro_passo()
    tem_dono = bool(config.definido("dono.email"))
    if passo == "dono" and tem_dono:
        return "erro", "O dono já está cadastrado.", "github"
    if passo != "dono" and not tem_dono:
        return "erro", "Cadastre o dono primeiro.", "dono"
    proximo = PASSOS[PASSOS.index(passo) + 1] if passo != PASSOS[-1] else None
    if passo in OPCIONAIS and campos.get("acao") == "pular":
        msg = "Pulado."
        if passo == "linear":
            try:
                _pular_linear()
            except (Erro, init.InitErro) as ex:
                return "erro", str(ex), passo
            except OSError as ex:
                return "erro", f"Não consegui gravar ({type(ex).__name__}).", passo
    else:
        try:
            msg = FUNCOES[passo](campos)
        except (Erro, init.InitErro, sites.SiteInvalido) as ex:
            return "erro", str(ex), passo
        except OSError as ex:
            return "erro", f"Não consegui gravar ({type(ex).__name__}).", passo
    if proximo is None:
        try:
            concluir()
        except OSError as ex:
            return "erro", f"Não consegui gravar ({type(ex).__name__}).", passo
    return "ok", msg, proximo


# ---------- telas (HTML só pelo hub/ui.py; todo texto de usuário escapado) ----------

def _campo(nome, rotulo, valor="", tipo="text", dica="", obrigatorio=False, extra=""):
    return (f'<label class="largo">{e(rotulo)}<input class="largo" name="{e(nome)}" type="{tipo}" value="{e(valor)}" '
            f'autocomplete="off"{" required" if obrigatorio else ""}{extra} placeholder="{e(dica)}"></label>')


def _corpo_passo(passo, token, token_config=""):
    t = _segredos()
    if passo == "dono":
        sem_senha = aberto()
        campos = (_campo("nome", "Seu nome", config.get("dono.nome") if config.definido("dono.nome") else "")
                  + _campo("email", "Seu e-mail", tipo="email", obrigatorio=True))
        if sem_senha:
            campos += f'<input type="hidden" name="token_config" value="{e(token_config)}">'
            campos += (_campo("senha", f"Senha do painel (mínimo {SENHA_MIN} caracteres)", tipo="password", obrigatorio=True,
                              extra=f' minlength="{SENHA_MIN}" autocomplete="new-password"')
                       + _campo("senha2", "Repita a senha", tipo="password", obrigatorio=True,
                                extra=f' minlength="{SENHA_MIN}" autocomplete="new-password"'))
        ajuda = "Você é o dono deste painel. A senha entra pelo navegador e fica só no servidor." if sem_senha else \
            "Você é o dono deste painel."
    elif passo == "github":
        u = _ultimos4(t / "github.env", "GH_TOKEN")
        campos = (_campo("org", "Organização ou usuário do GitHub", config.get("github.org"), dica="minha-empresa")
                  + _campo("token", "Token de acesso" + (f" (já salvo, termina em {u}; deixe vazio para manter)" if u else ""),
                           tipo="password", dica="só se quiser que os Claudes abram PRs"))
        ajuda = "Opcional. O token fica num arquivo restrito do servidor e nunca volta para esta tela."
    elif passo == "linear":
        u = _ultimos4(t / "linear.env", "LINEAR_API_KEY")
        campos = (_campo("time", "Chave do time (ex.: EXE)", config.get("linear.time"), dica="EXE")
                  + _campo("chave", "Chave de API" + (f" (já salva, termina em {u}; deixe vazio para manter)" if u else ""),
                           tipo="password"))
        ajuda = "Opcional. Sem Linear, as tarefas ficam na fila própria do hub (painel → Issues)."
    elif passo == "projeto":
        campos = (_campo("dominio", "Domínio do site (ex.: minhaempresa-com.example)", obrigatorio=True)
                  + _campo("id", "Id curto (minúsculas e hífen; vazio = primeira parte do domínio)")
                  + _campo("nome", "Nome do projeto")
                  + _campo("repo", "Repositório (dono/repo ou só repo; vazio = nenhum)")
                  + _campo("base", "Branch base", "main"))
        ajuda = "O primeiro site que o painel vai acompanhar. Os outros entram depois pela pasta de projetos."
    else:
        home = config.get("caminhos.home_agentes")
        campos = _campo("conta", "Número da conta (1 a 9)", "1", obrigatorio=True, extra=' maxlength="1" inputmode="numeric"')
        ajuda = (f"Registra a conta. Depois, no servidor, entre nela uma vez: <code>CLAUDE_CONFIG_DIR={e(home)}/.claude-N claude login</code> "
                 "(troque N pelo número).")
    pular = ('<button class="perigo" type="submit" name="acao" value="pular" formnovalidate>Pular</button>'
             if passo in OPCIONAIS else "")
    ultimo = passo == PASSOS[-1]
    form = (f'<form class="form" method="post" action="/comecar"><input type="hidden" name="csrf" value="{e(token)}">'
            f'<input type="hidden" name="passo" value="{e(passo)}">{campos}'
            f'<button class="prim" type="submit" name="acao" value="salvar">{"Concluir" if ultimo else "Continuar"}</button>{pular}</form>')
    return f'<p class="mu">{ajuda}</p>{form}'


def pagina_pendente(token, aviso=None):
    """Tela neutra (aberto, sem token válido): nunca 404 nem erro. Não revela nada além de como obter o token."""
    av = f'<div class="aviso erro" role="status">{e(aviso[:300])}</div>' if aviso else ""
    form = (f'<form class="form" method="post" action="/comecar"><input type="hidden" name="csrf" value="{e(token)}">'
            f'<input type="hidden" name="passo" value="token">'
            + _campo("token_config", "Token de configuração", tipo="password", obrigatorio=True)
            + '<button class="prim" type="submit">Continuar</button></form>')
    corpo = (f'<section class="entrar"><h1>{e(ui.NOME_PRODUTO)}: configuração pendente</h1>'
             '<p class="mu">Configuração pendente: use o token exibido na instalação (no terminal do <code>krohnos init</code> '
             'ou no log do contêiner, <code>docker compose logs painel</code>).</p></section>'
             + ui.card("Token de configuração", form) + av)
    return ui.pagina("Configuração pendente", corpo, publica=True, topo=False)


def pagina(passo, token, aviso=None, token_config=""):
    """Página do passo. aviso = ('ok'|'erro', texto) já vindo do servidor; texto escapado aqui."""
    if passo not in PASSOS:
        passo = primeiro_passo()
    n = PASSOS.index(passo) + 1
    av = f'<div class="aviso {"erro" if aviso[0] == "erro" else "ok"}" role="status">{e(aviso[1][:300])}</div>' if aviso else ""
    corpo = (f'<section class="entrar"><h1>{e(ui.NOME_PRODUTO)}: primeiros passos</h1>'
             f'<p class="mu">Passo {n} de {len(PASSOS)}: {e(TITULOS[passo])}</p></section>'
             + ui.card(TITULOS[passo], _corpo_passo(passo, token, token_config)) + av)
    return ui.pagina(TITULOS[passo], corpo, publica=True, topo=False)


def pagina_fim():
    return ui.pagina("Pronto", ui.card("Tudo pronto", '<p>Configuração concluída. <a href="/">Abrir o painel</a>.</p>'),
                     publica=True, topo=False)
