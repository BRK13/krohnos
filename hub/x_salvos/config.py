"""Configuração: padrões genéricos < x_salvos.toml (opcional) < variáveis de ambiente."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .guardrails import validar_max_itens

DESTINOS = ("biblioteca", "issue")


class ConfigInvalida(ValueError):
    pass


@dataclass
class Config:
    cookies: Path = field(default_factory=lambda: Path.home() / ".config" / "krohnos" / "x-cookies.json")
    cookies_origem: Path | None = None  # opcional: onde os cookies chegam de outra máquina (arquivo ou pasta)
    saida: Path = field(default_factory=lambda: Path.home() / "krohnos" / "x-salvos")
    temas: list = field(default_factory=list)
    destino: str = "biblioteca"
    max_itens: int = 150
    max_paginas: int = 40
    limite_taxa_s: float = 1.5
    jitter_max_s: int = 1200
    chrome: str = "/usr/bin/google-chrome"
    trava: Path | None = None  # arquivo do flock; padrão <saida>/.trava (a produção usa a mesma trava do cron antigo)

    def __post_init__(self):
        if self.trava is None:
            self.trava = Path(self.saida) / ".trava"


ENV = {  # campo: variável de ambiente
    "cookies": "KROHNOS_X_SALVOS_COOKIES", "cookies_origem": "KROHNOS_X_SALVOS_COOKIES_ORIGEM", "saida": "KROHNOS_X_SALVOS_SAIDA", "temas": "KROHNOS_X_SALVOS_TEMAS",
    "destino": "KROHNOS_X_SALVOS_DESTINO", "max_itens": "X_MAX_ITENS", "max_paginas": "KROHNOS_X_SALVOS_MAX_PAGINAS",
    "limite_taxa_s": "KROHNOS_X_SALVOS_LIMITE_TAXA_S", "jitter_max_s": "KROHNOS_X_SALVOS_JITTER_MAX_S",
    "chrome": "KROHNOS_X_SALVOS_CHROME", "trava": "KROHNOS_X_SALVOS_TRAVA",
}


def carregar(caminho=None, env=None):
    env = os.environ if env is None else env
    explicito = caminho or env.get("KROHNOS_X_SALVOS_CONFIG")
    dados = {}
    if explicito:
        p = Path(explicito).expanduser()
        if not p.is_file():
            raise ConfigInvalida(f"arquivo de configuração não encontrado: {p}")
        try:
            dados = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigInvalida(f"{p}: toml inválido ({e})") from e
    for campo, var in ENV.items():
        if env.get(var):
            dados[campo] = env[var]
    desconhecidos = set(dados) - set(Config.__dataclass_fields__)
    if desconhecidos:
        raise ConfigInvalida("chave desconhecida: " + ", ".join(sorted(desconhecidos)))
    if isinstance(dados.get("temas"), str):
        dados["temas"] = [t.strip() for t in dados["temas"].split(",") if t.strip()]
    try:
        for k in ("max_itens", "max_paginas", "jitter_max_s"):
            if k in dados:
                dados[k] = int(dados[k])
        if "limite_taxa_s" in dados:
            dados["limite_taxa_s"] = float(dados["limite_taxa_s"])
    except (TypeError, ValueError) as e:
        raise ConfigInvalida(f"valor numérico inválido na configuração ({e})") from e
    for k in ("cookies", "cookies_origem", "saida", "trava"):
        if k in dados:
            dados[k] = Path(str(dados[k])).expanduser()
    c = Config(**dados)
    if c.destino not in DESTINOS:
        raise ConfigInvalida(f"destino inválido {c.destino!r}: use " + " ou ".join(DESTINOS))
    try:
        validar_max_itens(c.max_itens)
    except ValueError as e:
        raise ConfigInvalida(str(e)) from e
    if c.max_paginas < 1 or c.limite_taxa_s <= 0 or c.jitter_max_s < 0:
        raise ConfigInvalida("max_paginas >= 1, limite_taxa_s > 0 e jitter_max_s >= 0")
    return c
