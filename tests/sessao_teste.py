"""Sessão de teste do painel: cria um usuário no HUB_DATA atual e devolve o cabeçalho Cookie (ou o csrf junto)."""
import secrets

from hub import usuarios

SENHA = "senha-de-teste-123"
SITE_PADRAO = "site-a"  # o site do cliente de teste (papel cliente) quando o teste não diz outro


def cookie_e_csrf(papel="dono", email=None, sites=None):
    email = email or f"{papel}-{secrets.token_hex(4)}@exemplo.test"
    if not any(u["email"] == email for u in usuarios.listar()):
        usuarios.criar(email, SENHA, papel=papel, sites=(sites or [SITE_PADRAO]) if papel == "cliente" else None)
    token = usuarios.entrar(email, SENHA)
    return f"__Host-krohnos_sessao={token}", usuarios.sessao(token)["csrf"]


def cookie(papel="dono", email=None, sites=None):
    """Valor do cabeçalho Cookie de uma sessão nova (usuário criado no HUB_DATA atual)."""
    return cookie_e_csrf(papel, email, sites)[0]
