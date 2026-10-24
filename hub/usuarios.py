"""Usuários e sessões do painel (SQLite em <dados>/painel/usuarios.db, só stdlib).

Senha com scrypt; o token de sessão só existe no cookie: no banco fica o sha256 dele.
CLI (cria o primeiro dono sem painel; a senha vem do getpass/stdin, nunca por argumento). Sempre como o usuário do
painel, nunca root (banco criado por root o painel não abre):
    na VPS:       sudo -u claude HUB_DATA=<dados> python3 -m hub.usuarios ...   (<dados> = caminhos.dados da VPS)
    no contêiner: docker compose exec painel python3 -m hub.usuarios ...
    ... criar <email> --papel dono
    ... senha <email>        # esqueceu a senha: troca (e derruba as sessões dele)
    ... listar
Migração: a senha única antiga do painel (PAINEL_SENHA) vira o 1º dono (migrar_senha_antiga, na subida do painel).
"""
import argparse
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

import hub

PAPEIS = ("leitura", "operador", "dono", "cliente")
SENHA_MIN = 12
OCIOSA = dt.timedelta(hours=12)
MAXIMA = dt.timedelta(days=7)
TOQUE = dt.timedelta(minutes=1)  # no máximo 1 escrita de "visto" por minuto
CONVITE = dt.timedelta(hours=24)
# Permissões num lugar só. "ver" = GET de tela comum; "config" = telas de dono (/config/..., /comecar); "acao:<x>" = POST
# /acao/<x> (registro das ações: hub/acoes.ACOES). Dono pode tudo; as ações de SO_DONO só ele (via "*").
SO_DONO = ("convidar", "papel", "desativar", "autorizar-prova", "revogar-prova")
PODE = {"leitura": {"ver"},
        "operador": {"ver", "execucao", "acao:criar-issue", "acao:comentar", "acao:pedido", "acao:criar-meta", "acao:clonar-viral",
                     "acao:replay", "acao:mover-issue"},  # replay = mesmo público da tela /execucao (permissão "execucao")
        "dono": "*",
        # cliente não "vê" tela do painel, não abre config nem /execucao e não age: só a própria tela
        # /cliente/<site>, que o portão do painel (default-deny) libera pelo vínculo em usuario_site, não por esta tabela
        "cliente": {"cliente"}}
RE_SITE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # o mesmo formato dos ids de hub/sites.py
CLIENTE_SEM_SITE = "Cliente precisa de pelo menos um site."
ULTIMO_DONO = "Precisa sobrar um dono ativo."
# sem espaço, controle nem <>"' (o e-mail aparece em tela; defesa extra além do escape)
RE_EMAIL = re.compile(r"^[^@\s<>\"'\x00-\x1f\x7f]{1,64}@[^@\s<>\"'\x00-\x1f\x7f]{1,255}$")
RE_LOGIN = re.compile(r"^[a-z0-9._-]{2,40}$")
_N, _R, _P = 2 ** 14, 8, 1
# hash falso fixo: gasta o mesmo tempo quando o usuário não existe ou está inativo
_FALSO = "scrypt$16384$8$1$" + "00" * 16 + "$" + "00" * 32


def _agora(agora):
    return agora or dt.datetime.now(dt.timezone.utc)


def banco(pasta=None):
    """pasta substitui hub.data() (krohnos init grava antes de existir config)."""
    pasta = (Path(pasta) if pasta else hub.data()) / "painel"
    pasta.mkdir(parents=True, exist_ok=True)
    os.chmod(pasta, 0o700)
    return pasta / "usuarios.db"


def _con(pasta=None):
    caminho = banco(pasta)
    if not caminho.exists():
        os.close(os.open(caminho, os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(caminho, timeout=10)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS usuario(email TEXT PRIMARY KEY, hash TEXT NOT NULL, papel TEXT NOT NULL,"
        " criado TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE IF NOT EXISTS sessao(token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, criada TEXT NOT NULL,"
        " visto TEXT NOT NULL, ip TEXT, csrf TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS convite(token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, papel TEXT NOT NULL,"
        " criado TEXT NOT NULL, usado INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS usuario_site(email TEXT NOT NULL, site TEXT NOT NULL, PRIMARY KEY(email, site));"
        "CREATE TABLE IF NOT EXISTS tentativa(chave TEXT NOT NULL, em TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS tentativa_em ON tentativa(em);"
        "CREATE TABLE IF NOT EXISTS auditoria(id INTEGER PRIMARY KEY, em TEXT NOT NULL, quem TEXT, acao TEXT,"
        " alvo TEXT, resultado TEXT, ip TEXT);"
        # auditoria só cresce: o banco recusa UPDATE e DELETE nela, venha de onde vier
        "CREATE TRIGGER IF NOT EXISTS auditoria_sem_update BEFORE UPDATE ON auditoria"
        " BEGIN SELECT RAISE(ABORT, 'auditoria só aceita INSERT'); END;"
        "CREATE TRIGGER IF NOT EXISTS auditoria_sem_delete BEFORE DELETE ON auditoria"
        " BEGIN SELECT RAISE(ABORT, 'auditoria só aceita INSERT'); END;"
    )
    if "sites" not in {c[1] for c in con.execute("PRAGMA table_info(convite)")}:  # banco de antes do cliente
        try:
            con.execute("ALTER TABLE convite ADD COLUMN sites TEXT")
        except sqlite3.OperationalError:  # outro processo acrescentou junto
            pass
    return con


def _sites(sites, papel):
    """Lista de ids de site limpa (sem repetir, ordem mantida). Só vale para cliente (os outros papéis ignoram); cliente
    sem nenhum site ou com id fora do formato: ValueError."""
    if papel != "cliente":
        return []
    if isinstance(sites, str):
        sites = [sites]
    limpa = []
    for x in sites or []:
        x = (x or "").strip() if isinstance(x, str) else ""
        if not RE_SITE.match(x) or len(x) > 80:
            raise ValueError("Site inválido.")
        if x not in limpa:
            limpa.append(x)
    if not limpa:
        raise ValueError(CLIENTE_SEM_SITE)
    return limpa


def _normalizar(email):
    e = (email or "").strip().lower()
    if not (RE_EMAIL.match(e) or RE_LOGIN.match(e)):
        raise ValueError("Identificador inválido: use um e-mail ou um login curto (letras, números, . _ -).")
    return e


def _validar(senha, papel=None):
    if len(senha or "") < SENHA_MIN:
        raise ValueError(f"A senha precisa ter pelo menos {SENHA_MIN} caracteres.")
    if papel is not None and papel not in PAPEIS:
        raise ValueError("Papel inválido: " + ", ".join(PAPEIS) + ".")


def _hash(senha):
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=sal, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${sal.hex()}${h.hex()}"


def _conferir(senha, guardado):
    try:
        _, n, r, p, sal, h = guardado.split("$")
        novo = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(novo, bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def criar(email, senha, papel="leitura", agora=None, pasta=None, sites=None):
    """sites: só para o papel cliente (pelo menos um); nos outros papéis é ignorado."""
    _validar(senha, papel)
    return _inserir(email, senha, papel, agora, pasta, sites)


def _inserir(email, senha, papel, agora=None, pasta=None, sites=None):
    """criar() sem a checagem de tamanho: só a migração usa (a senha antiga é a que já está em uso)."""
    e = _normalizar(email)
    if papel not in PAPEIS:
        raise ValueError("Papel inválido: " + ", ".join(PAPEIS) + ".")
    sites = _sites(sites, papel)
    h = _hash(senha)
    con = _con(pasta)
    try:
        with con:
            try:
                con.execute("INSERT INTO usuario(email, hash, papel, criado) VALUES (?,?,?,?)",
                            (e, h, papel, _agora(agora).isoformat()))
            except sqlite3.IntegrityError:
                raise ValueError(f"Já existe um usuário {e}.") from None
            con.executemany("INSERT INTO usuario_site(email, site) VALUES (?,?)", [(e, x) for x in sites])
    finally:
        con.close()
    return e


def entrar(email, senha, ip=None, agora=None):
    e = (email or "").strip().lower()
    con = _con()
    try:
        l = con.execute("SELECT hash, ativo FROM usuario WHERE email=?", (e,)).fetchone()
        ok = _conferir(senha or "", l[0] if l and l[1] else _FALSO)
        if not (l and l[1] and ok):
            return None
        token = secrets.token_urlsafe(32)
        agora = _agora(agora)
        t = agora.isoformat()
        with con:
            con.execute("DELETE FROM sessao WHERE visto < ? OR criada < ?",  # as vencidas não ficam no banco
                        ((agora - OCIOSA).isoformat(), (agora - MAXIMA).isoformat()))
            # o hash conferido acima tem de ser o atual no instante do INSERT (troca de senha concorrente)
            c = con.execute("INSERT INTO sessao(token_hash, email, criada, visto, ip, csrf)"
                            " SELECT ?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM usuario WHERE email=? AND hash=? AND ativo=1)",
                            (_sha(token), e, t, t, ip, secrets.token_urlsafe(24), e, l[0]))
            if c.rowcount != 1:
                return None
        return token
    finally:
        con.close()


def sessao(token, agora=None):
    if not token:
        return None
    agora = _agora(agora)
    th = _sha(token)
    con = _con()
    try:
        with con:
            l = con.execute(
                "SELECT s.email, s.criada, s.visto, s.csrf, u.papel, u.ativo FROM sessao s"
                " LEFT JOIN usuario u ON u.email = s.email WHERE s.token_hash=?", (th,)).fetchone()
            if not l:
                return None
            email, criada, visto, csrf, papel, ativo = l
            visto_dt = dt.datetime.fromisoformat(visto)
            if not ativo or agora - visto_dt > OCIOSA or agora - dt.datetime.fromisoformat(criada) > MAXIMA:
                con.execute("DELETE FROM sessao WHERE token_hash=?", (th,))
                return None
            if agora - visto_dt >= TOQUE:
                con.execute("UPDATE sessao SET visto=? WHERE token_hash=?", (agora.isoformat(), th))
            vinculos = [x[0] for x in con.execute("SELECT site FROM usuario_site WHERE email=? ORDER BY rowid", (email,))] \
                if papel == "cliente" else []
            return {"email": email, "papel": papel, "csrf": csrf, "sites": vinculos}
    finally:
        con.close()


def sair(token):
    if not token:
        return
    con = _con()
    try:
        with con:
            con.execute("DELETE FROM sessao WHERE token_hash=?", (_sha(token),))
    finally:
        con.close()


def contar(pasta=None):
    con = _con(pasta)
    try:
        return con.execute("SELECT COUNT(*) FROM usuario").fetchone()[0]
    finally:
        con.close()


def listar():
    con = _con()
    try:
        vinc = {}
        for em, st in con.execute("SELECT email, site FROM usuario_site ORDER BY rowid"):
            vinc.setdefault(em, []).append(st)
        return [{"email": a, "papel": b, "criado": c, "ativo": bool(d), "sites": vinc.get(a, []) if b == "cliente" else []}
                for a, b, c, d in con.execute("SELECT email, papel, criado, ativo FROM usuario ORDER BY criado, email")]
    finally:
        con.close()


# num comando só (atômico): não mexe no último dono ativo
_NAO_ULTIMO = (" AND NOT (papel='dono' AND ativo=1 AND"
               " (SELECT COUNT(*) FROM usuario WHERE papel='dono' AND ativo=1) <= 1)")


def _alterar(email, sql, params, apagar_sessoes=False, guarda_dono=False, tambem=()):
    """tambem: comandos SQL de um parâmetro (o e-mail) que rodam na mesma transação, se a alteração valeu."""
    e = _normalizar(email)
    con = _con()
    try:
        with con:
            if con.execute(sql + (_NAO_ULTIMO if guarda_dono else ""), (*params, e)).rowcount == 0:
                if guarda_dono and con.execute("SELECT 1 FROM usuario WHERE email=?", (e,)).fetchone():
                    raise ValueError(ULTIMO_DONO)
                raise ValueError(f"Usuário {e} não existe.")
            for extra in tambem:
                con.execute(extra, (e,))
            if apagar_sessoes:  # senha nova ou conta desativada: sessões e convites abertos deixam de valer
                con.execute("DELETE FROM sessao WHERE email=?", (e,))
                con.execute("UPDATE convite SET usado=1 WHERE email=? AND usado=0", (e,))
    finally:
        con.close()


def trocar_papel(email, papel):
    """Cliente não nasce por aqui (precisa de convite com site); quem sai de cliente perde os vínculos."""
    _validar("x" * SENHA_MIN, papel)
    if papel == "cliente":
        raise ValueError("Cliente só entra por convite com site: convide de novo escolhendo o papel cliente e o site.")
    sql = "UPDATE usuario SET papel=? WHERE email=?"
    _alterar(email, sql, (papel,), guarda_dono=papel != "dono", tambem=("DELETE FROM usuario_site WHERE email=?",))


def desativar(email):
    _alterar(email, "UPDATE usuario SET ativo=0 WHERE email=?", (), apagar_sessoes=True, guarda_dono=True)


def trocar_senha(email, senha):
    _validar(senha)
    _alterar(email, "UPDATE usuario SET hash=? WHERE email=?", (_hash(senha),), apagar_sessoes=True)


def pode(papel, permissao):
    """Papel desconhecido não pode nada; dono pode tudo."""
    p = PODE.get(papel) if isinstance(papel, str) else None
    return p == "*" or bool(p) and permissao in p


def permissao_get(caminho):
    """Permissão de um GET: "config" para /config, /config/... e /comecar; "execucao" para /execucao (saída bruta de
    ferramentas: operador e dono, não leitura); "ver" no resto. Lê o caminho como o roteador do painel (barras repetidas
    não mudam a rota)."""
    partes = [p for p in (caminho or "").split("/") if p]
    if partes[:1] == ["execucao"]:
        return "execucao"
    return "config" if partes[:1] in (["config"], ["comecar"]) else "ver"


def convidar(email, papel, agora=None, sites=None):
    """Convite de uso único que vale CONVITE (24 h). Devolve o token (no banco só o sha256). E-mail que já é usuário:
    o convite serve para trocar a senha dele (papel ignorado); desativado não ganha convite.
    sites: só para cliente, que exige pelo menos um; nos outros papéis é ignorado."""
    e = _normalizar(email)
    if papel not in PAPEIS:
        raise ValueError("Papel inválido: " + ", ".join(PAPEIS) + ".")
    sites = _sites(sites, papel)
    token = secrets.token_urlsafe(32)
    con = _con()
    try:
        with con:
            l = con.execute("SELECT ativo FROM usuario WHERE email=?", (e,)).fetchone()
            if l and not l[0]:
                raise ValueError(f"Usuário {e} está desativado.")
            con.execute("INSERT INTO convite(token_hash, email, papel, criado, sites) VALUES (?,?,?,?,?)",
                        (_sha(token), e, papel, _agora(agora).isoformat(), json.dumps(sites) if sites else None))
    finally:
        con.close()
    return token


def _convite(con, token, agora):
    """(email, papel, sites) do convite válido (não usado, dentro das 24 h) ou None."""
    if not token or not isinstance(token, str):
        return None
    l = con.execute("SELECT email, papel, criado, sites FROM convite WHERE token_hash=? AND usado=0", (_sha(token),)).fetchone()
    if not l or _agora(agora) - dt.datetime.fromisoformat(l[2]) > CONVITE:
        return None
    try:
        sites = [x for x in json.loads(l[3] or "[]") if isinstance(x, str) and RE_SITE.match(x)]
    except ValueError:
        sites = []
    return l[0], l[1], sites


def ver_convite(token, agora=None):
    """E-mail do convite se ainda vale (não gasta o convite); senão None."""
    con = _con()
    try:
        c = _convite(con, token, agora)
        return c[0] if c else None
    finally:
        con.close()


def aceitar_convite(token, senha, agora=None):
    """Gasta o convite e cria o usuário com o papel dele (ou troca a senha do existente, derrubando as sessões).
    Devolve o e-mail; None se o convite não vale ou o usuário está desativado (não volta por convite). Senha curta:
    ValueError, sem gastar o convite."""
    _validar(senha)
    h = _hash(senha)
    con = _con()
    try:
        with con:
            c = _convite(con, token, agora)
            if not c or con.execute("UPDATE convite SET usado=1 WHERE token_hash=? AND usado=0",
                                    (_sha(token),)).rowcount != 1:
                return None
            e, papel, sites = c
            l = con.execute("SELECT ativo FROM usuario WHERE email=?", (e,)).fetchone()
            if l and not l[0]:
                return None
            if l:
                con.execute("UPDATE usuario SET hash=? WHERE email=?", (h, e))
                con.execute("DELETE FROM sessao WHERE email=?", (e,))
                con.execute("UPDATE convite SET usado=1 WHERE email=? AND usado=0", (e,))
            else:
                try:
                    con.execute("INSERT INTO usuario(email, hash, papel, criado) VALUES (?,?,?,?)",
                                (e, h, papel, _agora(agora).isoformat()))
                except sqlite3.IntegrityError:  # outro convite do mesmo e-mail foi aceito junto: este não vale
                    return None
                if papel == "cliente":
                    con.executemany("INSERT OR IGNORE INTO usuario_site(email, site) VALUES (?,?)", [(e, x) for x in sites])
            return e
    finally:
        con.close()


def senha_antiga(segredos=None):
    """PAINEL_SENHA de antes dos usuários: env, senão a linha de <segredos>/painel.env. "" se não há.
    Única leitura dela no hub: serve só para migrar o 1º dono (subida do painel e `krohnos init`) e para o /comecar não
    abrir ao público antes disso."""
    v = os.environ.get("PAINEL_SENHA", "")
    if v:
        return v
    try:
        for linha in (Path(segredos or hub.segredos()) / "painel.env").read_text(encoding="utf-8").splitlines():
            if linha.startswith("PAINEL_SENHA="):
                return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def migrar_senha_antiga(agora=None):
    """0 usuários + senha antiga: cria o dono (login = dono.email da config, senão "dono"). Devolve o login ou None."""
    if contar() > 0:
        return None
    senha = senha_antiga()
    if not senha:
        return None
    from hub import config
    login = (config.get("dono.email") or "").strip() or "dono"
    try:
        feito = _inserir(login, senha, "dono", agora)
    except ValueError:  # outro processo migrou junto (Já existe) ou e-mail da config fora do formato
        if contar() > 0:
            return None
        feito = _inserir("dono", senha, "dono", agora)
    auditar("sistema", "migrar-senha", feito, agora=agora)
    return feito


# Auditoria: só INSERT (nada aqui apaga nem edita). Cada coluna cortada e sem caractere de controle; quem chama passa só
# identificadores curtos (issue, n, e-mail, título ≤ 80), nunca texto livre longo, senha nem token.
_CORTE = {"quem": 320, "acao": 40, "alvo": 80, "resultado": 120, "ip": 64}
_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def _curto(v, n):
    return _CONTROLE.sub(" ", str(v if v is not None else ""))[:n]


RE_DOMINIO_COM_PONTO = re.compile(r"@[^@]+\.[^@]+$")


def alvo_login(email):
    """O que o login digitado pode ser na auditoria: e-mail no formato, ou login curto de usuário que existe; senão
    "(inválido)" (senha digitada no campo do e-mail não fica gravada para sempre)."""
    e = (email or "").strip().lower()
    if RE_EMAIL.match(e) and RE_DOMINIO_COM_PONTO.search(e):  # "S3nh@forte" não passa por e-mail
        return e
    if RE_LOGIN.match(e):
        con = _con()
        try:
            if con.execute("SELECT 1 FROM usuario WHERE email=?", (e,)).fetchone():
                return e
        finally:
            con.close()
    return "(inválido)"


def auditar(quem, acao, alvo="", resultado="ok", ip=None, agora=None):
    """Grava uma linha. Falha ao gravar não derruba a ação: vai para o stderr (sem os valores)."""
    c = {k: _curto(v, _CORTE[k]) for k, v in (("quem", quem), ("acao", acao), ("alvo", alvo),
                                              ("resultado", resultado), ("ip", ip))}
    try:
        con = _con()
        try:
            with con:
                con.execute("INSERT INTO auditoria(em, quem, acao, alvo, resultado, ip) VALUES (?,?,?,?,?,?)",
                            (_agora(agora).isoformat(), c["quem"], c["acao"], c["alvo"], c["resultado"], c["ip"]))
        finally:
            con.close()
    except Exception as ex:  # noqa: BLE001
        print(f"auditoria: não gravei {c['acao']} ({type(ex).__name__})", file=sys.stderr)


def auditoria(limite=200, alvo_prefixo=None):
    """Últimas linhas, a mais nova primeiro. alvo_prefixo: só as de alvo que começa com ele (literal, ex. "cofre:")."""
    sql, params = "SELECT em, quem, acao, alvo, resultado, ip FROM auditoria", []
    if alvo_prefixo:
        sql += " WHERE substr(alvo, 1, ?) = ?"
        params += [len(alvo_prefixo), alvo_prefixo]
    con = _con()
    try:
        return [dict(zip(("em", "quem", "acao", "alvo", "resultado", "ip"), l)) for l in
                con.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limite)))]
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.usuarios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar")
    c.add_argument("email")
    c.add_argument("--papel", default="leitura", choices=PAPEIS)
    c.add_argument("--site", action="append", default=[], help="id do site (só para --papel cliente; repita para vários)")
    sub.add_parser("listar")
    sub.add_parser("senha", help="troca a senha (lida do stdin ou digitada)").add_argument("email")
    a = ap.parse_args(argv)
    if os.geteuid() == 0 and not (hub.data() / "painel" / "usuarios.db").exists():
        print("rodando como root e o banco de usuários ainda não existe: rode como o usuário do painel"
              " (ex.: sudo -u <usuário do painel> HUB_DATA=<dados> python3 -m hub.usuarios ...)", file=sys.stderr)
        return 2
    if a.cmd == "listar":
        for u in listar():
            print(u["email"], u["papel"], "ativo" if u["ativo"] else "inativo", u["criado"], *([",".join(u["sites"])] if u["sites"] else []))
        return 0
    senha = getpass.getpass("Senha: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    try:
        if a.cmd == "senha":
            trocar_senha(a.email, senha)
            print("Senha trocada:", _normalizar(a.email))
            return 0
        print("Criado:", criar(a.email, senha, papel=a.papel, sites=a.site))
    except ValueError as erro:
        print(erro, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
