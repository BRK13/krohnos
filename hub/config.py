"""Config única do hub: krohnos.toml + variáveis de ambiente.

Precedência: KROHNOS_<SECAO>_<CHAVE> > apelido legado (HUB_DATA, NIMBUS_HOME…) > krohnos.toml > padrão neutro.
O arquivo é $KROHNOS_CONFIG; sem ele, /etc/krohnos/krohnos.toml e depois ~/.krohnos/krohnos.toml (o primeiro que existir).
Segredo nunca vai no toml: o toml só diz o nome do segredo no cofre.

Sobreposição gravável (3.5): o krohnos.toml da VPS é do root (somente leitura para o painel). O que as telas
mudam (`gravar`, ex.: tela Agentes) vai para `<caminhos.dados>/krohnos.painel.toml`, que vale por cima do krohnos.toml
chave a chave, MAS só para as chaves de SOBREPOSICAO_PERMITIDA (rotinas.desligadas, agentes.*, plugins.ligados): caminhos, plugins.caminho,
cofre e o resto vêm só do krohnos.toml/env (do root), porque o root importa código e lê segredos a partir deles; `sem_sobreposicao()` desliga
até as permitidas (usado por hub/raiz.py). Precedência: env > sobreposição > krohnos.toml > legado > padrão. `gravar` só escreve nela (atômico, trava flock+RLock,
modo 0644 no arquivo novo (a config não guarda segredo e os ticks de outro usuário a leem), o existente mantém o modo) e nunca toca no krohnos.toml, nem nos comentários dele. Sem o arquivo
de sobreposição tudo é como antes. Sobreposição ilegível é ignorada (e guardada em `.corrompido` na próxima gravação).
"""
import contextlib
import functools
import json
import os
import shutil
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

SECOES = {
    "caminhos": {"dados": "/var/lib/krohnos", "cofre": "/etc/krohnos/cofre", "repos": "~/repos",
                 "trabalho": "~/trabalho", "codigo": None, "home_agentes": "~",  # codigo None = RAIZ
                 "segredos": "/etc/krohnos",  # pasta dos .env (linear.env, apps/...)
                 # dados do dono (1.6): None = resolvedor `dado()` (ver abaixo); privado = clone do repo privado (atualizar.sh)
                 "projetos": None, "tarefas": None, "hub_json": None, "privado": None},
    "cofre": {"legado": False},  # true = ler também os .env de caminhos.segredos quando o nome não está no cofre
    "dono": {"nome": "", "email": "", "whatsapp": "", "fuso": "America/Sao_Paulo", "idioma": "pt-BR"},
    "github": {"org": "", "base_padrao": "main"},
    "bi": {"negocios": {}},  # id do armazém → nome curto; independente do cadastro SEO
    "whatsapp": {"remetentes": {}},  # {id: "crm"|"numero"}: quem é dono de número nas propostas (projetos/<id>.json pode somar)
    "linear": {"time": "", "projeto_padrao": "", "workspace": ""},  # workspace: o da URL linear.app/<workspace>/issue/... (vazio = sem link)
    # Cérebro: o desenho vem daqui e do cadastro, nunca do código. Vazio = sem o nó (e sem a leitura dele).
    # sites: {id: rótulo} é a lista fechada do mapa (sem ela, todos os ativos do cadastro).
    "cerebro": {"sites": {}, "versao": [], "servidor": "", "servidor_rotulo": "Servidor", "servidor_deploys": "",
                "repos": {}, "projetos": {}, "codex": False, "codex_revisoes": "", "gpu_estacao": "", "pc_rotulo": "PC · GPU",
                "ci": False, "debugger": False},
    "issues": {"provedor": "", "prefixo": "KRO"},  # provedor: "local" (fila SQLite própria) | "linear"; vazio = linear se há segredo do Linear, senão local
    "painel": {"dominio": "", "porta": 8787, "proxies": []},  # proxies vazio = loopback e rede privada
    "rotinas": {"horarios": {}, "cron": "", "desligadas": [], "padroes": {}},  # {nome da rotina de plugin: "m h dom mon dow" em UTC}; a agenda do contêiner dispara; desligadas: nomes (rotina ou tarefa) que não disparam sozinhos (tela Rotinas) | padroes: {VARIÁVEL: valor} que os scripts de deploy usam quando o .env do app não define (remetentes, URLs); lidos por `python3 -m hub.config get rotinas.padroes <VARIÁVEL>`
    "mcp": {"url": "http://localhost:4181/mcp"},
    "agentes": {"contas": ["1"], "faixas": {"1": "construtor"}, "modelos": {}, "limite_usd_dia": 0,
                "limites": {}},  # limites: {conta: usd/dia}; a conta sem entrada usa limite_usd_dia (0 = sem limite)
    "plugins": {"caminho": "", "ligados": []},
    "nucleo": {"tag": "", "repo": ""},  # núcleo aberto numa tag fixa (hub/nucleo.py); tag vazia = embutido
    "marcas": {"dominios": {}, "emails": {}},  # {id do negócio: domínio} e {papel: endereço} dos negócios do dono (o código só cita o id/papel)
    "produto": {"nome": "Krohnos", "cli": "krohnos", "site": ""},  # marca: só aqui
}

# Legado da nossa VPS: vale só sem env nem toml, e só se a pasta existe (hub._caminho). Único lugar com estes literais.
LEGADO = {"caminhos.dados": "/var/lib/nimbus/reports", "caminhos.cofre": "/etc/nimbus/so",
          "caminhos.codigo": "/opt/nimbus/hub", "caminhos.segredos": "/etc/nimbus",
          "caminhos.home_agentes": "/home/nimbus"}

# Variáveis antigas que continuam valendo até a frente 5 (a nossa VPS não para).
APELIDOS = {
    "caminhos.dados": "HUB_DATA",
    "caminhos.cofre": "NIMBUS_HOME",
    "caminhos.home_agentes": "CLAUDE_HOME",
}

# Valores da nossa VPS: valem só sem env nem toml e só na VPS (pasta do código existe). `migrar` os grava no toml.
LEGADO_VALOR = {"bi.negocios": {"asite01": "asite01", "clientej": "ClienteJ", "fsite06": "Cliente A",
                                  "clientei": "ClienteI", "psite15": "Cliente G", "clientee": "ClienteE"},
                "produto.nome": "Krohnos", "dono.nome": "Fulano",
                "whatsapp.remetentes": {"fsite06": "crm", "asite01": "numero"},
                "plugins.caminho": "/opt/nimbus/privado/plugins", "plugins.ligados": ["*"],
                # a VPS roda o crontab do repositório: sem [rotinas] cron no toml, a tela Rotinas lê o mesmo arquivo (= o que raiz.rotinas() lê)
                "rotinas.cron": str(Path(__file__).resolve().parent.parent / "deploy" / "nimbus.cron")}  # "*" = todos os encontrados


def _legado_privado():
    """Valores da nossa VPS que são identidade (org do GitHub, domínio do painel, URL do Dokploy): ficam em privado/legado-vps.json,
    arquivo FORA do snapshot público (publico.permitidos não o nomeia). Sem o arquivo (snapshot, instalação de terceiro) não há legado."""
    try:
        dados = json.loads((Path(__file__).resolve().parent.parent / "privado" / "legado-vps.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


LEGADO_VALOR.update(_legado_privado())

PROIBIDAS = ("senha", "token", "secret", "segredo", "password", "api_key", "apikey")
PERMITIDAS = ("caminhos.segredos",)  # só o caminho da pasta de segredos, nunca um valor
SOBREPOSICAO = "krohnos.painel.toml"
# Segurança: a sobreposição é gravável pelo painel e pelo usuário claude, então só pode definir ESTAS chaves (nada que mude
# onde o código procura plugins, segredos ou cofre: plugins.caminho, caminhos.*, cofre.*, github.org... só do krohnos.toml/env, que
# são do root). Chave fora da lista na sobreposição é ignorada (aviso no stderr) e `gravar` a recusa. plugins.ligados só liga
# plugin já descoberto na pasta do krohnos.toml; o root (hub/raiz.py) nem isso: ignora a sobreposição (`sem_sobreposicao`).
SOBREPOSICAO_PERMITIDA = frozenset({"rotinas.desligadas", "agentes.contas", "agentes.faixas", "agentes.modelos", "agentes.limites",
                                    "agentes.limite_usd_dia", "plugins.ligados"})
PADRAO_ARQUIVO = (Path("/etc/krohnos/krohnos.toml"), Path("~/.krohnos/krohnos.toml"))


class ConfigError(ValueError):
    pass


def arquivo():
    """Caminho do toml em uso, ou None se não existe."""
    if os.environ.get("KROHNOS_CONFIG"):
        p = Path(os.environ["KROHNOS_CONFIG"]).expanduser()
        return p if p.is_file() else None
    for p in PADRAO_ARQUIVO:
        if p.expanduser().is_file():
            return p.expanduser()
    return None


def _procurar_segredo(tabela, caminho=""):
    for k, v in tabela.items():
        if f"{caminho}{k}" not in PERMITIDAS and any(p in k.lower() for p in PROIBIDAS):
            raise ConfigError(f"{caminho}{k}: segredo vai no cofre, não no krohnos.toml")
        if isinstance(v, dict):
            _procurar_segredo(v, f"{caminho}{k}.")


@functools.lru_cache(maxsize=None)
def _toml(caminho):
    if caminho is None:
        return {}
    with open(caminho, "rb") as f:
        dados = tomllib.load(f)
    _procurar_segredo(dados)
    return dados


def recarregar():
    _toml.cache_clear()
    _SOBRE.clear()


def _dir_dados():
    """caminhos.dados sem olhar a sobreposição (é ela quem mora lá): env, apelido, krohnos.toml, legado da VPS, padrão."""
    for var in ("KROHNOS_CAMINHOS_DADOS", "HUB_DATA"):
        if os.environ.get(var):
            return Path(os.environ[var]).expanduser()
    try:
        v = _toml(arquivo()).get("caminhos", {}).get("dados")
    except (OSError, ValueError, ConfigError):  # krohnos.toml ilegível não impede de achar (e gravar) a sobreposição
        v = None
    if v:
        return Path(v).expanduser()
    legado = Path(LEGADO["caminhos.dados"])
    return legado if legado.is_dir() else RAIZ / "data"


def sobreposicao():
    """Caminho do krohnos.painel.toml (pode não existir)."""
    return _dir_dados() / SOBREPOSICAO


_SOBRE = {}  # (caminho, mtime_ns, tamanho) -> dados; o painel grava, os outros processos leem a versão nova


_semsobre = threading.local()


@contextlib.contextmanager
def sem_sobreposicao():
    """Dentro do bloco (nesta thread) só valem env e krohnos.toml: é como o root lê a config (hub/raiz.py), porque a
    sobreposição é gravável por quem não é root. Reentrante."""
    _semsobre.n = getattr(_semsobre, "n", 0) + 1
    try:
        yield
    finally:
        _semsobre.n -= 1


def _so_permitidas(dados, origem):
    """Dados da sobreposição só com as chaves de SOBREPOSICAO_PERMITIDA; o resto é ignorado com aviso no stderr."""
    saida = {}
    for secao, tabela in dados.items():
        if not isinstance(tabela, dict):
            print(f"config: {origem.name}: {secao!r} ignorada (fora da lista permitida)", file=sys.stderr)
            continue
        for nome, v in tabela.items():
            if f"{secao}.{nome}" in SOBREPOSICAO_PERMITIDA:
                saida.setdefault(secao, {})[nome] = v
            else:
                print(f"config: {origem.name}: chave {secao}.{nome} ignorada (a sobreposição só define {', '.join(sorted(SOBREPOSICAO_PERMITIDA))})",
                      file=sys.stderr)
    return saida


def _ler_sobreposicao():
    if getattr(_semsobre, "n", 0):
        return {}
    p = sobreposicao()
    try:
        st = p.stat()
    except OSError:
        return {}
    chave = (str(p), st.st_mtime_ns, st.st_size)
    if chave not in _SOBRE:
        try:
            dados = tomllib.loads(p.read_text(encoding="utf-8"))
            _procurar_segredo(dados)
            dados = _so_permitidas(dados, p)
        except (OSError, ValueError, ConfigError):  # ilegível, sem permissão, corrompido ou com segredo: ignora
            dados = {}
        _SOBRE.clear()
        _SOBRE[chave] = dados
    return _SOBRE[chave]


def _carregado():
    base, sobre = _toml(arquivo()), _ler_sobreposicao()
    if not sobre:
        return base
    saida = {s: dict(t) for s, t in base.items()}
    for secao, tabela in sobre.items():
        if isinstance(tabela, dict):
            saida.setdefault(secao, {}).update(tabela)
    return saida


def _separar(chave):
    secao, _, nome = chave.partition(".")
    if secao not in SECOES or nome not in SECOES[secao]:
        raise ConfigError(f"chave desconhecida: {chave}")
    return secao, nome


def _converter(texto, padrao):
    if isinstance(padrao, bool):
        return texto.lower() in ("1", "true", "sim", "yes")
    if isinstance(padrao, int):
        return int(texto)
    if isinstance(padrao, (list, dict)):
        return json.loads(texto)
    return texto


def definido(chave):
    """Valor vindo de env, apelido ou toml; None se ninguém definiu (vale o padrão)."""
    secao, nome = _separar(chave)
    padrao = SECOES[secao][nome]
    for var in (f"KROHNOS_{secao}_{nome}".upper(), APELIDOS.get(chave)):
        if var and os.environ.get(var):
            return _converter(os.environ[var], padrao)
    return _carregado().get(secao, {}).get(nome)


def legado(chave):
    """Valor legado da nossa VPS para `chave` (também as que não são chave do toml, ex. dokploy.url); None fora da VPS."""
    return LEGADO_VALOR.get(chave) if Path(LEGADO["caminhos.codigo"]).is_dir() else None


def painel_url():
    """`https://<painel.dominio>` ou "" sem domínio configurado (na nossa VPS vem do legado)."""
    dominio = (get("painel.dominio") or "").strip()
    return f"https://{dominio}" if dominio else ""


def dominio(negocio, padrao=""):
    """Domínio do negócio (marcas.dominios) ou `padrao`; na nossa VPS vem do legado."""
    return (get("marcas.dominios") or {}).get(negocio) or padrao


def email(papel, padrao=""):
    """Endereço do papel (marcas.emails: remetente, contato da pesquisa...) ou `padrao`; na nossa VPS vem do legado."""
    return (get("marcas.emails") or {}).get(papel) or padrao


def get(chave):
    valor = definido(chave)
    if valor is None and chave in LEGADO_VALOR and Path(LEGADO["caminhos.codigo"]).is_dir():
        return LEGADO_VALOR[chave]
    return SECOES[_separar(chave)[0]][_separar(chave)[1]] if valor is None else valor


# ---- dados do dono (cadastro de sites, tarefas, hub.json): fora do código, achados por precedência ----
RAIZ = Path(__file__).resolve().parent.parent
# nome → (variável curta, chave do toml, nome dentro de caminhos.dados, nome do legado em RAIZ, é pasta?)
DADOS = {"projetos": ("KROHNOS_PROJETOS", "caminhos.projetos", "projetos", "sites", True),
         "tarefas": ("KROHNOS_TAREFAS", "caminhos.tarefas", "tarefas", "tarefas", True),
         "hub.json": ("KROHNOS_HUB_JSON", "caminhos.hub_json", "hub.json", "hub.json", False)}


def _existe(p, pasta):
    return p.is_dir() if pasta else p.is_file()


def dado(nome):
    """Onde estão `projetos`, `tarefas` ou `hub.json`. Ordem: KROHNOS_PROJETOS/_TAREFAS/_HUB_JSON (ou KROHNOS_CAMINHOS_*) >
    toml (`caminhos.projetos`...; ou `caminhos.dados`/<nome> se existir) > legado RAIZ/sites|tarefas|hub.json se existir >
    padrão neutro exemplos/. `caminhos.dados` só vale aqui se a subpasta existe: ele também é a pasta de relatórios."""
    var, chave, sub, legado, pasta = DADOS[nome]
    explicito = os.environ.get(var) or definido(chave)
    if explicito:
        return Path(explicito).expanduser()
    base = os.environ.get("KROHNOS_CAMINHOS_DADOS") or _carregado().get("caminhos", {}).get("dados")  # sem o apelido HUB_DATA
    # tarefas: <dados>/tarefas é a camada do painel (tarefas_extra), carregada POR CIMA da pasta base; nunca a substitui
    if nome != "tarefas" and base and _existe(Path(base).expanduser() / sub, pasta):
        return Path(base).expanduser() / sub
    if _existe(RAIZ / legado, pasta):
        return RAIZ / legado
    return RAIZ / "exemplos" / sub


def projetos():
    return dado("projetos")


def tarefas():
    return dado("tarefas")


def fichas_licitacoes():
    """Pasta das fichas de empresa da triagem de licitações: `<caminhos.privado>/licitacoes/fichas` (ou KROHNOS_CAMINHOS_PRIVADO)
    se existir; senão a ficha fictícia de exemplos/licitacoes/fichas. Nunca dado real dentro do repo público."""
    privado = get("caminhos.privado")
    if privado:
        p = Path(privado).expanduser() / "licitacoes" / "fichas"
        if p.is_dir():
            return p
    return RAIZ / "exemplos" / "licitacoes" / "fichas"


def e_exemplo_fichas(pasta):
    """True se `pasta` é a ficha fictícia de exemplos/: triagem e pacote não podem tratá-la como ficha real."""
    return Path(pasta).resolve() == (RAIZ / "exemplos" / "licitacoes" / "fichas").resolve()


def tarefas_extra():
    """Camada do painel: <dados>/tarefas/*.md. `tarefas.carregar` a lê por cima de `tarefas()` (mesmo nome = sobrescreve;
    nome novo = tarefa nova). A pasta pode não existir (então tudo é como antes); a tela Rotinas é quem grava nela."""
    return _dir_dados() / "tarefas"


def hub_json():
    return dado("hub.json")


def ler_hub_json():
    """hub.json do caminho configurado, como dict. Levanta OSError/ValueError como a leitura direta de antes."""
    return json.loads(dado("hub.json").read_text(encoding="utf-8"))


def dono(maiuscula=False):
    """Nome do dono para texto. Sem artigo: escreva "o {dono}", "do {dono}" (nome vazio vira "dono")."""
    nome = get("dono.nome") or "dono"
    return nome[:1].upper() + nome[1:] if maiuscula else nome


def repo(nome):
    """`<org>/<nome>` no GitHub da config (github.org)."""
    org = get("github.org")
    if not org:
        raise ConfigError("github.org vazio: defina [github] org no krohnos.toml (ou KROHNOS_GITHUB_ORG)")
    return f"{org}/{nome}"


def validar():
    """Levanta ConfigError (com o nome da chave) se o toml tem seção/chave desconhecida ou segredo."""
    for secao, tabela in _carregado().items():
        if secao not in SECOES:
            raise ConfigError(f"seção desconhecida: {secao}")
        for nome in tabela:
            if nome not in SECOES[secao]:
                raise ConfigError(f"chave desconhecida: {secao}.{nome}")


# ---- migração: gera o krohnos.toml a partir do que a nossa VPS usa hoje ----

def _ler_env(arquivo):
    from hub.comum.env import ler_env
    return ler_env(arquivo)


def _toml_valor(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_valor(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {_toml_valor(x)}" for k, x in v.items()) + "}"
    return json.dumps(str(v), ensure_ascii=False)


def _serializar(dados):
    linhas = ["# krohnos.toml (migrar e a tela Agentes gravam aqui); sem segredos (eles ficam no cofre)"]
    for secao, tabela in dados.items():
        simples = {k: v for k, v in tabela.items() if not isinstance(v, dict) or not v}
        tabelas = {k: v for k, v in tabela.items() if isinstance(v, dict) and v}
        linhas += ["", f"[{secao}]"] + [f"{k} = {_toml_valor(v)}" for k, v in simples.items()]
        for k, v in tabelas.items():
            linhas += ["", f"[{secao}.{k}]"] + [f"{json.dumps(str(a))} = {_toml_valor(b)}" for a, b in v.items()]
    return "\n".join(linhas) + "\n"


_RTRAVA = threading.RLock()
_nivel = threading.local()


@contextlib.contextmanager
def _travar():
    """Trava de ler-alterar-gravar: RLock entre threads + flock entre processos (reentrante na mesma thread)."""
    with _RTRAVA:
        if getattr(_nivel, "n", 0):
            _nivel.n += 1
            try:
                yield
            finally:
                _nivel.n -= 1
            return
        destino = sobreposicao()
        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # Windows: só a trava de threads
            fcntl = None
        lk = open(destino.with_name(destino.name + ".lock"), "a+")
        try:
            if fcntl:
                fcntl.flock(lk, fcntl.LOCK_EX)
            _nivel.n = 1
            yield
        finally:
            _nivel.n = 0
            lk.close()  # fechar solta o flock


def gravar(valores):
    """Grava chaves "secao.chave" (só as de SECOES; segredo recusado: ConfigError, nada é escrito) na sobreposição
    `krohnos.painel.toml` da pasta de dados; o krohnos.toml principal (da VPS, do root) nunca é tocado. `valores` é um
    dict ou uma função sem argumentos que devolve o dict: ela roda já com a trava e a config recarregada, para o
    ler-alterar-gravar não perder gravação concorrente. Escrita atômica; arquivo ilegível vira `<arquivo>.corrompido` e
    recomeça. Devolve o caminho."""
    with _travar():
        if callable(valores):
            recarregar()
            valores = valores()
        novos = {}
        for chave, valor in valores.items():
            secao, nome = _separar(chave)
            if chave not in SOBREPOSICAO_PERMITIDA:
                raise ConfigError(f"{chave}: a sobreposição do painel não pode definir essa chave (só o krohnos.toml)")
            _procurar_segredo({secao: {nome: valor}})
            novos.setdefault(secao, {})[nome] = valor
        destino = sobreposicao()
        try:
            with open(destino, "rb") as f:
                dados = tomllib.load(f)
        except FileNotFoundError:
            dados = {}
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            dados = {}
            with contextlib.suppress(OSError):
                shutil.copy2(destino, destino.with_name(destino.name + ".corrompido"))
        dados = _so_permitidas(dados, destino)  # o que já estava no arquivo fora da lista nunca valeu; sai ao regravar
        for secao, tabela in novos.items():
            dados.setdefault(secao, {}).update(tabela)
        try:
            modo = destino.stat().st_mode & 0o777
        except OSError:
            modo = 0o644
        fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_serializar(dados))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, modo)
            os.replace(tmp, destino)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        recarregar()
        return destino


def migrar(saida, hub_json=None, painel_env=None):
    """Escreve o toml com os caminhos, faixas, modelos e URL do MCP de hoje. Devolve o dicionário gravado."""
    import hub
    hub_json = Path(hub_json) if hub_json else dado("hub.json")
    antigo = json.loads(hub_json.read_text(encoding="utf-8")) if hub_json.is_file() else {}
    painel = _ler_env(painel_env) if painel_env else {}
    dados = {"caminhos": {"dados": str(hub.data()), "cofre": str(hub.home())}}
    if os.environ.get("CLAUDE_HOME"):
        dados["caminhos"]["home_agentes"] = os.environ["CLAUDE_HOME"]
    dados["dono"], dados["github"] = {"nome": LEGADO_VALOR["dono.nome"]}, {"org": LEGADO_VALOR.get("github.org", "")}
    dados["produto"] = {"nome": get("produto.nome"), "cli": get("produto.cli")}
    dados["bi"] = {"negocios": get("bi.negocios")}
    dados["whatsapp"] = {"remetentes": LEGADO_VALOR["whatsapp.remetentes"]}
    if get("plugins.ligados"):  # VPS (legado) ou já configurado: grava a pasta onde os plugins estão e quais estão ligados
        from hub import plugins
        achados = plugins.descobertos()
        pasta = next(iter(achados.values()), None) or get("plugins.caminho")
        dados["plugins"] = {"caminho": str(pasta), "ligados": sorted(achados) or list(get("plugins.ligados"))}
    if antigo.get("mcp"):
        dados["mcp"] = {"url": antigo["mcp"]}
    agentes = {}
    if antigo.get("faixas"):
        agentes["contas"] = sorted(antigo["faixas"])
        agentes["faixas"] = dict(antigo["faixas"])
    if antigo.get("painel_modelos"):
        agentes["modelos"] = {"painel": list(antigo["painel_modelos"])}
    if agentes:
        dados["agentes"] = agentes
    painel_toml = {}
    if painel.get("PAINEL_DOMINIO"):
        painel_toml["dominio"] = painel["PAINEL_DOMINIO"]
    if painel.get("PAINEL_PORTA", "").isdigit():
        painel_toml["porta"] = int(painel["PAINEL_PORTA"])
    if painel_toml:
        dados["painel"] = painel_toml
    Path(saida).parent.mkdir(parents=True, exist_ok=True)
    Path(saida).write_text(_serializar(dados), encoding="utf-8")
    # VPS migrada já tem dono e senha: o assistente /comecar nunca deve aparecer (marcador na pasta de dados)
    marcador = Path(dados["caminhos"]["dados"]).expanduser() / "comecar.concluido"
    try:
        marcador.parent.mkdir(parents=True, exist_ok=True)
        marcador.write_text("concluido\n", encoding="utf-8")
    except OSError as ex:
        print(f"aviso: não gravei {marcador} ({type(ex).__name__}); crie-o à mão para o assistente /comecar não ligar", file=sys.stderr)
    return dados


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m hub.config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrar", help="gera o krohnos.toml do que a VPS usa hoje")
    m.add_argument("--saida", required=True)
    m.add_argument("--hub-json")
    m.add_argument("--painel-env", default=None)
    sub.add_parser("validar", help="valida o krohnos.toml em uso")
    g = sub.add_parser("get", help="imprime o valor da chave (texto puro; dict com subchave); vazio se não houver (usado pelos scripts de deploy)")
    g.add_argument("chave")
    g.add_argument("subchave", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "get":
        try:
            v = get(a.chave)
        except (KeyError, ValueError, ConfigError):
            return 0
        if a.subchave is not None:
            v = v.get(a.subchave, "") if isinstance(v, dict) else ""
        print("" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
        return 0
    try:
        if a.cmd == "migrar":
            import hub
            migrar(a.saida, a.hub_json, a.painel_env or hub.segredos() / "painel.env")
            os.environ["KROHNOS_CONFIG"] = a.saida
            recarregar()
        validar()
    except ConfigError as e:
        print(f"erro: {e}")
        return 1
    print(f"ok: {arquivo() or 'sem arquivo (padrões)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
