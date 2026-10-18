"""Login das contas Claude pelo painel (tela Agentes): roda `claude auth login` num processo filho com
CLAUDE_CONFIG_DIR da conta, mostra o link (e o código, se a CLI imprimir um) e espera o `.credentials.json` aparecer,
o mesmo fluxo do `ativar-contas`. O código que a pessoa pega no link volta pelo `enviar_codigo` (stdin do filho).

Nada do token passa por aqui: lemos da saída só o link e o código de aparelho (e descartamos qualquer coisa com cara de
token), e do `.credentials.json` só se existe, quando mudou e quando vence. Nada disso vai para log.
Comando e `popen` são injetáveis (testes usam um processo falso).
"""
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

from hub import conta, config, home_agentes

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
URL = re.compile(r"https?://[^\s<>\"'`]+")
CODIGO = re.compile(r"(?i:\b(?:code|c[oó]digo))\b[^\n]{0,30}?(?-i:\b([A-Z0-9]{3,8}(?:-[A-Z0-9]{3,8})+|(?=[A-Z0-9]*\d)[A-Z0-9]{6,12}))\b")
TOKEN = re.compile(r"sk-ant-\S*|eyJ[A-Za-z0-9_-]{20,}")
NUMERO = re.compile(r"[1-9][0-9]{0,2}")
COLADO = re.compile(r"[A-Za-z0-9._~#\-]{6,256}")
ATIVOS = ("aguardando", "concluindo")
AMBIENTE = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
            "no_proxy", "SSL_CERT_FILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")  # nada de PAINEL_*, chaves de API ou segredos para o filho
LOGINS = {}
TRAVA = threading.Lock()


class Captura:
    """Pega link e código da saída do `claude auth login`, tolerante a cor, texto em volta e pontuação."""

    def __init__(self):
        self.link = ""
        self.codigo = ""

    def alimentar(self, linha):
        linha = ANSI.sub("", linha).strip()
        if not linha or TOKEN.search(linha):
            return
        if not self.link and (m := URL.search(linha)):
            self.link = m.group(0).rstrip(".,;:)]}>")
        if not self.codigo and (m := CODIGO.search(URL.sub(" ", linha))):
            self.codigo = m.group(1)


def _pasta(n, home=None):
    return Path(home or home_agentes()) / (".claude" if str(n) == "1" else f".claude-{n}")


def _assinatura(arq):
    try:
        st = arq.stat()
        return (st.st_mtime_ns, st.st_size) if st.st_size else None
    except OSError:
        return None


def situacao(n, home=None, agora=None):
    """"ok" (login e token válidos, ou vencido mas renovável pela CLI), "expirado" (arquivo sem uso: vencido sem
    renovação ou ilegível) ou "sem_login". Só olha a validade; o token não sai daqui."""
    arq = _pasta(n, home) / ".credentials.json"
    if not arq.exists():
        return "sem_login"
    try:
        o = json.loads(arq.read_text(encoding="utf-8"))["claudeAiOauth"]
        if o.get("refreshToken"):
            return "ok"
        agora = agora or conta.agora()
        return "ok" if float(o["expiresAt"]) / 1000 > agora.timestamp() else "expirado"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "expirado"


def proximo_numero(home=None):
    usados = {int(n) for n in conta.contas_config() if str(n).isdigit()}
    for p in Path(home or home_agentes()).glob(".claude-*"):
        if p.name[8:].isdigit():
            usados.add(int(p.name[8:]))
    return str(max(usados | {1}) + 1)  # depois do maior: o número de uma conta tirada não volta


def comando_padrao(home=None):
    b = os.environ.get("CLAUDE_BIN") or str(Path(home or home_agentes()) / ".local/bin/claude")
    return [b if Path(b).exists() else "claude", "auth", "login"]


def registrar_conta(n, faixa):
    """Põe a conta em agentes.contas e agentes.faixas (sobreposição do painel, krohnos.painel.toml) (sem perder as faixas que vinham do hub.json). Lê e grava sob a
    trava do config: um POST da tela ao mesmo tempo não perde nenhuma das duas gravações."""
    def novos():
        contas = list(conta.contas_config())
        nova = n not in contas
        if nova:
            contas.append(n)
        faixas = {c: f for c in contas if (f := conta.faixa(c))}
        if faixa and (nova or n not in faixas):  # entrar de novo numa conta antiga não muda a faixa dela
            faixas[n] = faixa
        return {"agentes.contas": contas, "agentes.faixas": faixas}
    config.gravar(novos)


class Login:
    def __init__(self, n, faixa, pasta, timeout):
        self.id, self.n, self.faixa, self.pasta = secrets.token_urlsafe(9), str(n), faixa, pasta
        self.captura, self.estado, self.aviso, self.proc = Captura(), "aguardando", "", None
        self.fim = time.monotonic() + timeout
        self.inicio_cred = _assinatura(pasta / ".credentials.json")
        self.cancelado = threading.Event()
        self.trava = threading.Lock()

    link = property(lambda s: s.captura.link)
    codigo = property(lambda s: s.captura.codigo)

    def resumo(self):
        return {"id": self.id, "conta": self.n, "estado": self.estado, "link": self.link, "codigo": self.codigo,
                "aviso": self.aviso}


def _mover(lg, de, para):
    """Transição de estado sob trava: só sai de um estado em `de`. Devolve se mudou (cancelar não é desfeito por um ok)."""
    with lg.trava:
        if lg.estado not in de:
            return False
        lg.estado = para
        return True


def _matar(proc, prazo=1.0):
    """Mata o filho e o grupo de processos dele (o `claude` abre netos: navegador, ajudantes): SIGTERM no grupo, espera
    curta e SIGKILL no grupo mesmo que o líder já tenha saído (os netos podem ter ficado)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError, TypeError, AttributeError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=prazo)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError, TypeError, AttributeError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
        proc.wait(timeout=prazo)


def _ler(lg):
    with contextlib.suppress(Exception):
        for linha in lg.proc.stdout:
            lg.captura.alimentar(linha)
    for f in (lg.proc.stdout, lg.proc.stdin):
        with contextlib.suppress(Exception):
            f.close()


def _vigiar(lg, intervalo, ao_concluir):
    arq = lg.pasta / ".credentials.json"
    while lg.estado == "aguardando":
        a = _assinatura(arq)
        if a is not None and a != lg.inicio_cred:
            if _mover(lg, ("aguardando",), "concluindo"):
                try:
                    ao_concluir(lg.n, lg.faixa)
                except Exception:  # noqa: BLE001  (a mensagem pode ter caminho/valor: só um aviso fixo)
                    lg.aviso = "Entrou, mas não consegui gravar a conta na sobreposição do painel (krohnos.painel.toml)."
                _mover(lg, ("concluindo",), "ok")  # cancelado durante a gravação continua cancelado
                time.sleep(min(intervalo * 4, 1))  # deixa a CLI terminar de escrever sozinha
            break
        if lg.cancelado.is_set():
            return
        if lg.proc.poll() is not None:
            time.sleep(intervalo)  # a credencial pode chegar junto com a saída do filho
            if _assinatura(arq) in (None, lg.inicio_cred):
                _mover(lg, ("aguardando",), "erro")
            continue
        if time.monotonic() > lg.fim:
            _mover(lg, ("aguardando",), "tempo")
            break
        time.sleep(intervalo)
    if lg.proc.poll() is None:
        _matar(lg.proc)


def _podar():
    for k, lg in list(LOGINS.items()):
        if lg.estado not in ATIVOS and time.monotonic() > lg.fim + 3600:
            LOGINS.pop(k, None)


def iniciar(n, faixa="construtor", home=None, comando=None, popen=None, timeout=900, intervalo=1.0, ao_concluir=None):
    """Começa o login da conta n (pasta ~/.claude-N; a 1 é ~/.claude). Devolve o Login (estado, link, código).
    Já existe um em andamento para a conta: devolve o mesmo. Comando que não abre vira estado "erro"."""
    n = str(n)
    if not NUMERO.fullmatch(n):
        raise ValueError("número de conta inválido")
    pasta = _pasta(n, home)
    with TRAVA:
        _podar()
        for lg in LOGINS.values():
            if lg.n == n and lg.estado in ATIVOS:
                return lg
        pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
        lg = Login(n, faixa, pasta, timeout)
        LOGINS[lg.id] = lg
    env = {**{k: os.environ[k] for k in AMBIENTE if k in os.environ}, "CLAUDE_CONFIG_DIR": str(pasta), "NO_COLOR": "1"}
    env["TERM"] = "dumb"
    try:
        lg.proc = (popen or subprocess.Popen)(
            comando or comando_padrao(home), env=env, cwd=str(pasta), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    except (OSError, ValueError):
        lg.estado = "erro"
        return lg
    threading.Thread(target=_ler, args=(lg,), daemon=True).start()
    threading.Thread(target=_vigiar, args=(lg, intervalo, ao_concluir or registrar_conta), daemon=True).start()
    return lg


def obter(id_):
    return LOGINS.get(id_)


def cancelar(id_):
    lg = LOGINS.get(id_)
    if not lg or not _mover(lg, ("aguardando",), "cancelado"):  # já concluindo/ok: o cancelamento chegou tarde
        return False
    lg.cancelado.set()
    if lg.proc is not None:
        _matar(lg.proc)
    return True


def enviar_codigo(id_, texto):
    """Cola o código que a página de login deu (vai para o stdin do filho). Formato conferido; nada é registrado."""
    lg = LOGINS.get(id_)
    texto = (texto or "").strip()
    if not lg or lg.estado not in ATIVOS or lg.proc is None or not COLADO.fullmatch(texto):
        return False
    try:
        lg.proc.stdin.write(texto + "\n")
        lg.proc.stdin.flush()
    except (OSError, ValueError):
        return False
    return True
