"""Plugins do hub: pacotes `krohnos_plugin_<nome>` numa pasta (krohnos.toml: [plugins] caminho, ligados).

Um plugin é um pacote Python que pode definir no `__init__.py`:
    ROTAS   = {"/x": fn, "/bi/<negocio>/y": fn}   fn(q, **trechos) -> (status, html[, cabecalhos]); q = query achatada (1º valor)
    ACOES    = {"nome": fn(campos, agora)}  vira POST /acao/<nome> (CSRF e senha do painel); devolve (ok, mensagem)
    ARQUIVOS = {"/x/<id>/<nome>": fn(id, nome)}  GET de arquivo binário (mp4): devolve Path ou None (404); atrás da senha
    MENU    = [("Grupo", "/x", "Rótulo")]         opcional 4º item: caminho antes do qual o link entra no grupo
    ROTINAS = [("nome", "python3 -m hub.x args")]  comando que o deploy/rotina.sh roda (alimenta `raiz pedir rerodar-rotina`)
Só os pacotes em `ligados` são lidos ("*" liga todos; vale o nome com ou sem o prefixo krohnos_plugin_). Plugin ligado que levanta
exceção no import não derruba o painel: vai para o stderr e para erros() (aviso na tela /monitor). Desligado: rota 404 e some do menu.

Sem toml na nossa VPS vale o legado (hub/config.py LEGADO_VALOR): pasta privado/plugins ao lado do hub, todos ligados. Se essa pasta não existe,
vale <repo>/privado/plugins (onde os plugins moram até a frente 5 separar o repositório privado).

Compatibilidade: `hub/<modulo>.py` é um shim de 3 linhas (`legado`) que devolve o módulo do plugin sob o nome antigo,
mesmo com o plugin desligado (o import antigo e `python3 -m hub.<modulo>` do cron seguem valendo). Os shims saem na frente 5.
"""
import importlib
import re
import runpy
import sys
from pathlib import Path

from hub import RAIZ, config

PREFIXO = "krohnos_plugin_"
_CARGA = None  # {"nomes": [...], "rotas": [...], "menu": [...], "rotinas": [...], "erros": {nome: msg}}


def caminhos():
    """Pastas onde procurar plugins: a configurada (se existe) e <repo>/privado/plugins (se existe)."""
    achados = []
    cfg = config.get("plugins.caminho")
    if cfg and Path(cfg).expanduser().is_dir():
        achados.append(Path(cfg).expanduser())
    interno = RAIZ / "privado" / "plugins"
    if interno.is_dir() and interno not in achados:
        achados.append(interno)
    return achados


def descobertos():
    """{nome sem prefixo: pasta} dos pacotes krohnos_plugin_* encontrados (a primeira pasta vence)."""
    achados = {}
    for pasta in caminhos():
        for p in sorted(pasta.glob(PREFIXO + "*")):
            if (p / "__init__.py").is_file():
                achados.setdefault(p.name[len(PREFIXO):], p.parent)
    return achados


def _preparar_path():
    for pasta in caminhos():
        if str(pasta) not in sys.path:
            sys.path.append(str(pasta))


def _nome(n):
    return str(n).removeprefix(PREFIXO)


def ligados():
    """Nomes (sem prefixo) ligados no config; "*" liga todos os descobertos."""
    pedidos = [_nome(n) for n in config.get("plugins.ligados")]
    achados = descobertos()
    return list(achados) if "*" in pedidos else pedidos


def ligado(nome):
    return _nome(nome) in ligados()


def recarregar():
    global _CARGA
    _CARGA = None


def _carregar():
    global _CARGA
    if _CARGA is not None:
        return _CARGA
    carga = {"nomes": [], "rotas": [], "menu": [], "rotinas": [], "acoes": {}, "arquivos": [], "erros": {}, "por": {}}
    _preparar_path()
    for nome in ligados():
        try:
            mod = importlib.import_module(PREFIXO + nome)
            rotas = {str(k): v for k, v in dict(getattr(mod, "ROTAS", {})).items()}
            menu = [tuple(i) for i in getattr(mod, "MENU", [])]
            rotinas = [(str(n), str(c)) for n, c in getattr(mod, "ROTINAS", [])]
            acoes = {str(k): v for k, v in dict(getattr(mod, "ACOES", {})).items()}
            arquivos = {str(k): v for k, v in dict(getattr(mod, "ARQUIVOS", {})).items()}
        except Exception as ex:  # noqa: BLE001  (plugin quebrado nunca derruba o painel)
            carga["erros"][nome] = f"{type(ex).__name__}: {str(ex)[:200]}"
            print(f"plugin {nome}: não carregou ({carga['erros'][nome]})", file=sys.stderr)
            continue
        carga["nomes"].append(nome)
        carga["rotas"] += [(_padrao(k), v) for k, v in rotas.items()]
        carga["menu"] += menu
        carga["rotinas"] += rotinas
        carga["por"][nome] = {"rotas": list(rotas), "menu": menu, "rotinas": [n for n, _ in rotinas]}
        carga["acoes"].update(acoes)
        carga["arquivos"] += [(_padrao(k), v) for k, v in arquivos.items()]
    _CARGA = carga
    return carga


def _padrao(caminho):
    """'/bi/<negocio>/local' vira regex com um grupo nomeado por trecho."""
    return re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", re.escape(caminho)) + "$")


def rotas():
    """[(regex do caminho, função)] das rotas dos plugins ligados."""
    return _carregar()["rotas"]


def rota(caminho):
    """(função, trechos) da rota do plugin que casa com o caminho, ou None."""
    for rx, fn in rotas():
        m = rx.match(caminho)
        if m:
            return fn, m.groupdict()
    return None


def acao(nome):
    """Função da ação POST /acao/<nome> de plugin ligado, ou None."""
    return _carregar()["acoes"].get(nome)


def arquivo(caminho):
    """Path do arquivo que a rota ARQUIVOS de plugin ligado serve para o caminho; None se não há rota ou arquivo."""
    for rx, fn in _carregar()["arquivos"]:
        m = rx.match(caminho)
        if m:
            return fn(**m.groupdict())
    return None


def menu():
    """[(grupo, caminho, rótulo[, antes])] dos plugins ligados."""
    return list(_carregar()["menu"])


def rotinas():
    """{nome: comando completo com deploy/rotina.sh} das rotinas dos plugins ligados."""
    return {n: f"deploy/rotina.sh {n} {c}" for n, c in _carregar()["rotinas"]}


def carregado(nome):
    """{"rotas": [caminho], "menu": [item], "rotinas": [nome]} do plugin ligado que carregou, ou None (não lê nem importa nada além da carga)."""
    return _carregar()["por"].get(_nome(nome))


def erros():
    """{plugin: mensagem} dos que não carregaram."""
    return dict(_carregar()["erros"])


def legado(pacote, modulo, nome_shim):
    """Corpo do shim hub/<modulo>.py: o módulo vive em <pasta>/krohnos_plugin_<pacote>/<modulo>.py.
    Como `python3 -m hub.<modulo>` (nome_shim == "__main__"), roda o módulo como programa."""
    _preparar_path()
    alvo = f"{PREFIXO}{pacote}.{modulo}"
    try:
        if nome_shim == "__main__":
            runpy.run_module(alvo, run_name="__main__", alter_sys=True)
            return sys.modules["__main__"]
        return importlib.import_module(alvo)
    except ModuleNotFoundError as ex:
        if ex.name and alvo.startswith(ex.name):
            raise ImportError(f"plugin {PREFIXO}{pacote} não encontrado (krohnos.toml [plugins] caminho; procurei em "
                              f"{[str(c) for c in caminhos()]})") from ex
        raise
