"""Sessão de teste do painel: cria um usuário no HUB_DATA atual e devolve o cabeçalho Cookie (ou o csrf junto)."""
import secrets

from hub import usuarios

SENHA = "senha-de-teste-123"


def cookie_e_csrf(papel="dono", email=None):
    email = email or f"{papel}-{secrets.token_hex(4)}@exemplo.test"
    if not any(u["email"] == email for u in usuarios.listar()):
        usuarios.criar(email, SENHA, papel=papel)
    token = usuarios.entrar(email, SENHA)
    return f"__Host-kronos_sessao={token}", usuarios.sessao(token)["csrf"]


def cookie(papel="dono", email=None):
    """Valor do cabeçalho Cookie de uma sessão nova (usuário criado no HUB_DATA atual)."""
    return cookie_e_csrf(papel, email)[0]
