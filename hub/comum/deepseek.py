"""Cliente único da DeepSeek: chave, limitador, HTTP com Bearer, saldo e Search/Fetch (`/v1/tools/*`).

Antes viviam em `hub/moedor.py`; o moedor e a captação de backlinks (`hub/backlinks/`) usam este módulo. Quem precisa de um
guarda próprio (saldo mínimo, data de corte, contadores) herda `Cliente` e sobrescreve `_antes_da_chamada()`.

Chave lida de UM lugar: `chave()` (ambiente DEEPSEEK_API_KEY, depois `~/.config/deepseek.env`). O valor nunca vai para log,
mensagem de erro ou saída.
"""
import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from hub import config, data, home_agentes
from hub.comum.segredos import carregar_segredo

API = "https://api.deepseek-com.example"
ENV_CHAVE = Path(os.environ.get("DEEPSEEK_ENV", str(home_agentes() / ".config" / "deepseek.env")))
QPS = 4.0  # o limite da DeepSeek é 5 por conta (Search e Fetch); o hub fica em 4
UA = f"nimbus-moedor/1 (pesquisa publica; {config.email('pesquisa')})"


class FaixaParada(Exception):
    """Faixa desligada, sem saldo ou sem chave: o lote para e o orquestrador é chamado."""
    codigo = None


class ErroHTTP(RuntimeError):
    """HTTP que não é de parada (429, 5xx...); `codigo` é o status."""

    def __init__(self, codigo, caminho):
        super().__init__(f"HTTP {codigo} {caminho}")
        self.codigo = codigo


def chave():
    """Ordem: ambiente DEEPSEEK_API_KEY, fonte única de segredos (`carregar_segredo`), `~/.config/deepseek.env` (cópia legada)."""
    v = os.environ.get("DEEPSEEK_API_KEY")
    if v:
        return v
    v = carregar_segredo("DEEPSEEK_API_KEY")
    if v:
        return v
    try:  # legado: a cópia do .env que o moedor lia antes da fonte única
        for linha in ENV_CHAVE.read_text(encoding="utf-8").splitlines():
            k, _, val = linha.strip().partition("=")
            if k.replace("export ", "").strip() == "DEEPSEEK_API_KEY" and val:
                return val.strip().strip("'\"")
    except OSError:
        pass
    raise FaixaParada("sem DEEPSEEK_API_KEY")


class Limitador:
    """≤ `qps` chamadas por segundo somando TODOS os processos (flock num arquivo com o instante da última)."""

    def __init__(self, arq, qps=QPS, relogio=time.time, dormir=time.sleep):
        self.arq, self.passo, self.relogio, self.dormir = Path(arq), 1.0 / qps, relogio, dormir

    def esperar(self):
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        with open(self.arq, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                ultimo = float(f.read().strip() or 0)
            except ValueError:
                ultimo = 0.0
            agora = self.relogio()
            livre = max(agora, ultimo + self.passo)
            if livre > agora:
                self.dormir(livre - agora)
            f.seek(0)
            f.truncate()
            f.write(repr(livre))
            return livre - agora


def conferir_pausa():
    """Pausa de segurança vale para todos os consumidores da conta, inclusive Search/Fetch."""
    from hub import gasto_anormal
    motivo = gasto_anormal.pausado("deepseek")
    if motivo:
        raise FaixaParada(motivo)


def _http(metodo, caminho, corpo=None, timeout=40, chave_api=None, ua=None):
    """-> dict da resposta JSON. 401/402/403 levanta FaixaParada; outro status, ErroHTTP (ambos com `.codigo`)."""
    if metodo.upper() != "GET":
        conferir_pausa()
    req = urllib.request.Request(
        API + caminho, method=metodo, headers={"Authorization": f"Bearer {chave_api or chave()}", "Content-Type": "application/json", "User-Agent": ua or UA},
        data=json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 402, 403):
            erro = FaixaParada(f"DeepSeek HTTP {e.code} em {caminho}")
            erro.codigo = e.code
            raise erro
        raise ErroHTTP(e.code, caminho)


def saldo_usd(http=_http):
    """Saldo total em USD (GET /user/balance, que não gasta nada). `http(metodo, caminho, corpo=None)` -> dict."""
    d = http("GET", "/user/balance")
    infos = [i for i in d.get("balance_infos", []) if i.get("currency") == "USD"]
    if not d.get("is_available", True) or not infos:
        raise FaixaParada("saldo indisponível na DeepSeek")
    return float(infos[0]["total_balance"])


def pasta_limitador():
    """Mesmo arquivo do moedor (`<data>/moedor/limitador.txt`): o limite de 5 QPS é da conta, então todo cliente soma nele."""
    return Path(data()) / "moedor" / "limitador.txt"


class Cliente:
    """Search e Fetch com limitador global. A base não guarda nada: `_antes_da_chamada()` é o gancho do guarda de quem herda
    (pode levantar FaixaParada para impedir a chamada)."""

    def __init__(self, http=_http, limitador=None):
        self.http = http
        self.limitador = limitador or Limitador(pasta_limitador())

    def _antes_da_chamada(self):
        pass

    def _chamar(self, caminho, corpo):
        conferir_pausa()
        self._antes_da_chamada()
        self.limitador.esperar()
        conferir_pausa()
        return self.http("POST", caminho, corpo)

    def buscar(self, consulta, count=10):
        """POST /v1/tools/search -> lista `results` (title, url, ...)."""
        d = self._chamar("/v1/tools/search", {"query": consulta, "count": max(1, min(50, count))})
        return d.get("results") or []

    def ler(self, url):
        """POST /v1/tools/fetch -> `content` (Markdown da página)."""
        d = self._chamar("/v1/tools/fetch", {"url": url})
        return d.get("content") or ""


# ---- gasto pago de quem divide a conta com o moedor ----
# O moedor para se o saldo cai 1 centavo sem explicação; a captação de backlinks gasta de verdade na mesma conta e registra aqui,
# para o guarda do moedor descontar o que é conhecido.

def _arquivo_gasto(base_dados=None):
    return Path(base_dados or data()) / "moedor" / "gasto-externo.json"


def gasto_registrado(base_dados=None):
    """Soma acumulada (US$) do gasto registrado por `registrar_gasto`."""
    try:
        return float(json.loads(_arquivo_gasto(base_dados).read_text(encoding="utf-8")).get("total_usd") or 0)
    except (OSError, ValueError, AttributeError):
        return 0.0


def registrar_gasto(usd, base_dados=None):
    """Soma `usd` ao acumulado (JSON com flock, vale entre processos). Falha de disco não derruba quem gasta: devolve False."""
    arq = _arquivo_gasto(base_dados)
    try:
        arq.parent.mkdir(parents=True, exist_ok=True)
        with open(arq.with_suffix(".lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            total = round(gasto_registrado(base_dados) + float(usd), 6)
            tmp = arq.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"total_usd": total}), encoding="utf-8")
            tmp.replace(arq)
        return True
    except OSError:
        return False
