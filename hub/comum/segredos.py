"""Fonte única de segredos (/F7): um arquivo por segredo, só o valor, em <hub.segredos>/segredos/<NOME>.

Ordem de leitura de carregar_segredo: $CREDENTIALS_DIRECTORY/<NOME> (systemd LoadCredential=), a pasta da fonte única,
a variável de ambiente <NOME> e, por último, os arquivos legados passados em `legado` (valor solto num arquivo).
Valor vazio não conta; origem ilegível cai para a próxima. Nenhuma mensagem de erro ou saída leva o valor: para
comparar ou citar, use hash8().
"""
import hashlib
import os
from pathlib import Path

import hub

DIR_PADRAO = hub.segredos() / "segredos"


def dir_fonte(env=None):
    env = os.environ if env is None else env
    return Path(env.get("NIMBUS_SEGREDOS") or DIR_PADRAO)


def hash8(valor):
    """sha256 truncado do valor sem espaços nas pontas: o único jeito de falar de um segredo em log ou relatório."""
    return hashlib.sha256(valor.strip().encode()).hexdigest()[:8]


def _arquivo(p):
    try:
        v = Path(p).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return v or None


def carregar_segredo(nome, env=None, legado=()):
    if not nome or "/" in nome or nome.startswith("."):
        raise ValueError("nome de segredo inválido")
    env = os.environ if env is None else env
    pastas = [Path(env["CREDENTIALS_DIRECTORY"])] if env.get("CREDENTIALS_DIRECTORY") else []
    pastas.append(dir_fonte(env))
    for pasta in pastas:
        v = _arquivo(pasta / nome)
        if v:
            return v
    v = (env.get(nome) or "").strip()
    if v:
        return v
    for p in legado:
        v = _arquivo(p)
        if v:
            return v
    return None


def chave_openrouter(env=None):
    """OPENROUTER_API_KEY ("" se não houver): fonte única, ambiente ou o ~/.config/openrouter.key de antes da F7."""
    return carregar_segredo("OPENROUTER_API_KEY", env=env, legado=[Path.home() / ".config" / "openrouter.key"]) or ""
