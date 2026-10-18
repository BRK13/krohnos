"""Hub de SEO do dono: cadastro único dos sites (sites/*.json) e a lógica compartilhada pelos scripts.

Caminhos (env ou krohnos.toml valem; sem eles, o legado da nossa VPS quando a pasta existe, senão o padrão do PC):
RAIZ (o repositório), data(), home() (cofre), codigo() (checkout de produção), segredos() (pasta dos .env),
home_agentes() (home das contas Claude). O legado fica só em hub/config.py (LEGADO).
"""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _caminho(chave, pc):
    from hub import config
    valor = config.definido(chave)
    if valor:
        return Path(valor).expanduser()
    # legado: sem env nem toml, o comportamento de hoje (VPS se a pasta existe, senão o PC)
    vps = Path(config.LEGADO[chave])
    return vps if vps.is_dir() else pc


def data():
    return _caminho("caminhos.dados", RAIZ / "data")


def home():
    return _caminho("caminhos.cofre", Path.home() / ".nimbus")


def codigo():
    return _caminho("caminhos.codigo", RAIZ)


def segredos():
    return _caminho("caminhos.segredos", Path.home() / ".krohnos")


def faltou(erro, modulo):
    """Import opcional: o ImportError é só `modulo` ausente (fora do snapshot público)? Dependência quebrada
    dentro do módulo, ou o módulo existindo com erro, não conta: quem chama relança. Use com `from hub.x import ...` ou
    `import hub.x as y`, que dão ModuleNotFoundError com `name` = o módulo (`from hub import x` dá name="hub")."""
    return isinstance(erro, ModuleNotFoundError) and erro.name == modulo


def home_agentes():
    return _caminho("caminhos.home_agentes", Path.home())
