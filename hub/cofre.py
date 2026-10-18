"""Cofre de segredos: SQLite com cada valor cifrado (Fernet). Nada daqui imprime valor em log nem em erro.

Arquivos em `hub.home()` (caminhos.cofre): `cofre.db` e `chave` (0600, criada no primeiro uso). `KROHNOS_CHAVE_MESTRA`
(chave Fernet) vence o arquivo. Nome = `escopo.item` (`linear.token`); `env("linear")` vira `LINEAR_TOKEN`.
`cofre.legado = true` no toml: se o nome não está no cofre, lê os `.env` de `hub.segredos()` (adaptador da VPS antiga).

CLI: `python3 -m hub.cofre definir NOME` (valor pela entrada padrão), `listar`, `apagar NOME`, `env ESCOPO`.
"""
import contextlib
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import hub
from hub import config

NOME = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_CANARIO = b"cofre-ok"


class CofreError(Exception):
    pass


def _pasta():
    return hub.home()


def _garantir_pasta():
    p = _pasta()
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p


def _ler_chave(arq):
    if arq.stat().st_mode & 0o077:
        raise CofreError(f"{arq.name}: permissão aberta demais; rode chmod 600 na chave do cofre")
    chave = arq.read_bytes().strip()
    if not chave:
        raise CofreError("arquivo da chave mestra vazio")
    return chave


def _chave():
    env = os.environ.get("KROHNOS_CHAVE_MESTRA", "").strip()
    if env:
        return env.encode()
    arq = _pasta() / "chave"
    if arq.is_file():
        return _ler_chave(arq)
    _garantir_pasta()
    chave = Fernet.generate_key()
    # grava num temporário 0600 e publica com link (atômico): o arquivo final nunca aparece vazio nem pela metade
    fd, tmp = tempfile.mkstemp(dir=arq.parent, prefix=".chave-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(chave + b"\n")
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, arq)
        except FileExistsError:  # perdeu a corrida: vale a chave do vencedor
            return _ler_chave(arq)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return chave


def _fernet():
    try:
        return Fernet(_chave())
    except (ValueError, TypeError):
        raise CofreError("chave mestra inválida (não é uma chave Fernet)") from None


def _agora():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def _abrir():
    """Conexão com o esquema pronto e a chave conferida contra o canário do próprio banco."""
    f = _fernet()
    _garantir_pasta()
    db = _pasta() / "cofre.db"
    if not db.exists():
        os.close(os.open(db, os.O_WRONLY | os.O_CREAT, 0o600))
    os.chmod(db, 0o600)
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "CREATE TABLE IF NOT EXISTS segredo(nome TEXT PRIMARY KEY, cifrado BLOB NOT NULL, definido_em TEXT NOT NULL,"
            " por TEXT NOT NULL, fim4 TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS auditoria(quando TEXT NOT NULL, quem TEXT NOT NULL, acao TEXT NOT NULL,"
            " nome TEXT NOT NULL, ip TEXT NOT NULL DEFAULT '');"
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v BLOB NOT NULL);")
        linha = con.execute("SELECT v FROM meta WHERE k='canario'").fetchone()
        if linha is None:
            if con.execute("SELECT 1 FROM segredo LIMIT 1").fetchone():
                try:
                    f.decrypt(con.execute("SELECT cifrado FROM segredo LIMIT 1").fetchone()[0])
                except InvalidToken:
                    raise CofreError("chave mestra errada: não abre este cofre") from None
            con.execute("INSERT INTO meta VALUES('canario', ?)", (f.encrypt(_CANARIO),))
            con.commit()
        else:
            try:
                f.decrypt(linha[0])
            except InvalidToken:
                raise CofreError("chave mestra errada: não abre este cofre") from None
        yield con, f
        con.commit()
    finally:
        con.close()


def _validar(nome):
    if not isinstance(nome, str) or not NOME.match(nome):
        raise CofreError("nome inválido: use letras, números, _ e - separados por ponto (ex.: linear.token)")


def _existe():
    try:
        return (_pasta() / "cofre.db").is_file()
    except OSError:
        return False


def definir(nome, valor, por, ip=""):
    _validar(nome)
    if not valor:
        raise CofreError("valor vazio")
    if any(c in valor for c in "\r\n\0"):
        raise CofreError("valor com quebra de linha não é aceito")
    with _abrir() as (con, f):
        for outro, in con.execute("SELECT nome FROM segredo WHERE nome<>?", (nome,)):
            if _nome_env(outro) == _nome_env(nome):
                raise CofreError(f"{nome} colide com {outro} (mesma variável de ambiente)")
        fim = valor[-4:] if len(valor) >= 10 else ""
        con.execute("INSERT OR REPLACE INTO segredo VALUES(?,?,?,?,?)",
                    (nome, f.encrypt(valor.encode()), _agora(), por, fim))
        con.execute("INSERT INTO auditoria VALUES(?,?,?,?,?)", (_agora(), por, "definir", nome, ip))


def _nome_env(nome):
    return re.sub(r"[.\-]", "_", nome).upper()


def _legado():
    """{VARIAVEL: valor} dos .env da pasta de segredos; vazio com `cofre.legado` desligado."""
    if not config.get("cofre.legado"):
        return {}
    from hub.comum.env import ler_env
    juntos = {}
    for arq in sorted(hub.segredos().glob("*.env")):
        for k, v in ler_env(arq).items():
            juntos.setdefault(k, v)
    return juntos


def ler(nome):
    _validar(nome)
    if _existe():
        with _abrir() as (con, f):
            linha = con.execute("SELECT cifrado FROM segredo WHERE nome=?", (nome,)).fetchone()
            if linha:
                try:
                    return f.decrypt(linha[0]).decode()
                except InvalidToken:
                    raise CofreError(f"não consegui abrir {nome}: chave mestra errada ou valor corrompido") from None
    return _legado().get(_nome_env(nome))


def listar():
    if not _existe():
        return []
    with _abrir() as (con, _):
        return [{"nome": n, "definido_em": d, "por": p, "fim": fim}
                for n, d, p, fim in con.execute("SELECT nome, definido_em, por, fim4 FROM segredo ORDER BY nome")]


def apagar(nome, por, ip=""):
    """True se existia. Grava auditoria só quando apagou."""
    _validar(nome)
    with _abrir() as (con, _):
        if not con.execute("DELETE FROM segredo WHERE nome=?", (nome,)).rowcount:
            return False
        con.execute("INSERT INTO auditoria VALUES(?,?,?,?,?)", (_agora(), por, "apagar", nome, ip))
        return True


def auditoria(limite=200):
    """Mais recentes primeiro. Nunca tem valor."""
    if not _existe():
        return []
    with _abrir() as (con, _):
        return [{"quando": q, "quem": w, "acao": a, "nome": n, "ip": i} for q, w, a, n, i in con.execute(
            "SELECT quando, quem, acao, nome, ip FROM auditoria ORDER BY rowid DESC LIMIT ?", (int(limite),))]


def env(escopo):
    """{ESCOPO_ITEM: valor} de tudo no escopo (prefixo exato antes do ponto); o cofre vence o legado."""
    _validar(escopo)
    prefixo = _nome_env(escopo) + "_"
    saida = {k: v for k, v in _legado().items() if escopo == escopo.lower() and k.startswith(prefixo)}
    if not _existe():
        return saida
    with _abrir() as (con, f):
        for nome, cifrado in con.execute("SELECT nome, cifrado FROM segredo ORDER BY nome"):
            if nome.split(".")[0] != escopo or "." not in nome:
                continue
            try:
                saida[_nome_env(nome)] = f.decrypt(cifrado).decode()
            except InvalidToken:
                raise CofreError(f"não consegui abrir {nome}: chave mestra errada ou valor corrompido") from None
    return saida


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog=f"{config.get('produto.cli')} cofre")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("definir", help="grava NOME; o valor vem da entrada padrão")
    d.add_argument("nome")
    sub.add_parser("listar", help="nome, quando, quem e os 4 últimos caracteres")
    a_ = sub.add_parser("apagar")
    a_.add_argument("nome")
    e = sub.add_parser("env", help="imprime ESCOPO_ITEM=valor do escopo")
    e.add_argument("escopo")
    a = ap.parse_args(argv)
    quem = os.environ.get("USER") or "cli"
    try:
        if a.cmd == "definir":
            definir(a.nome, sys.stdin.read().rstrip("\r\n"), por=quem)
        elif a.cmd == "listar":
            for i in listar():
                print(f"{i['nome']}\t{i['definido_em']}\t{i['por']}\t{('…' + i['fim']) if i['fim'] else ''}")
        elif a.cmd == "apagar":
            if not apagar(a.nome, por=quem):
                print(f"erro: {a.nome} não existe", file=sys.stderr)
                return 1
        else:
            for k, v in env(a.escopo).items():
                if any(c in v for c in "\r\n\0"):
                    raise CofreError(f"{k}: valor com quebra de linha não sai pelo env")
                print(f"{k}={v}")
    except CofreError as ex:
        print(f"erro: {ex}", file=sys.stderr)
        return 1
    except OSError as ex:
        print(f"erro: {type(ex).__name__} em {ex.filename or 'arquivo do cofre'}: {ex.strerror or ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
