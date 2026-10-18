"""`gh` medido por consumidor: contagem, cache de leitura e pausa com cota baixa.

Telemetria só guarda consumidor, tipo e status: nunca args, corpo ou token (o
ident é um hash curto do token). Tipos: 'gh' (chamada efetiva), 'cache', 'pausa'.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import time

from hub import github_api, github_app

CACHE_S = 60


LEITURAS = {("pr", "view"), ("pr", "checks"), ("pr", "list"), ("repo", "view"), ("repo", "list"), ("run", "view"), ("run", "list")}
ESCRITA_API = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")


def _cacheavel(args):
    """Allowlist: só leitura. Escrita (pr comment/merge/create...) e `api` não-GET nunca usam o cache."""
    a = [str(x) for x in args]
    if a[:1] == ["search"]:
        return True
    if a[:1] == ["api"]:
        return not any(x == k or x.startswith(k + "=") or (len(k) == 2 and x.startswith(k) and x != k and not x.startswith("--"))
                       for x in a[1:] for k in ESCRITA_API) and not _sem_cache(a)
    return tuple(a[:2]) in LEITURAS


def _sem_cache(args):
    """Saída bruta de `pr diff`, logs de run/job e downloads nunca vai para o sqlite."""
    a = [str(x) for x in args]
    if a[:2] == ["pr", "diff"] or a[:2] == ["run", "download"] or a[:1] == ["job"]:
        return True
    if a[:2] == ["run", "view"] and any(x.startswith("--log") for x in a):
        return True
    return a[:1] == ["api"] and any("/logs" in x or "/artifacts" in x for x in a)


def executar(args, consumidor, urgente=False, cache=False, base_dados=None, agora=None, rodar=None, **kw):
    """Roda `gh <args>`; devolve o CompletedProcess. `cache=True` só para leituras.

    Com a App configurada, o GH_TOKEN dela vai no env; senão vale o ambiente atual.
    A chamada é contada mesmo se o processo falhar (check=True, timeout).
    """
    agora = time.time() if agora is None else agora
    rodar = rodar or subprocess.run
    cache = cache and _cacheavel(args) and not _sem_cache(args)
    try:
        app = github_app.token()
    except Exception:  # App mal configurada não derruba o gh: cai para o ambiente atual
        app = None
    token = github_api.resolver_token(app, rodar)
    ident = github_api.ident(token)
    chave = hashlib.sha256((ident + "\n" + json.dumps([str(a) for a in args])).encode()).hexdigest()
    try:
        con = github_api._banco(base_dados)
    except (sqlite3.Error, OSError):  # medição é melhor-esforço: sem banco, o gh segue
        con = None
    try:
        if con is not None:
            try:
                if cache:
                    c = con.execute("SELECT em, corpo FROM cache WHERE chave=?", (chave,)).fetchone()
                    if c and 0 <= agora-c[0] < CACHE_S:
                        github_api._contar(con, agora, consumidor, ident, "cache")
                        saved = json.loads(c[1])
                        return subprocess.CompletedProcess(["gh", *args], 0, stdout=saved["o"], stderr=saved["e"])
                cota = con.execute("SELECT restante, reset FROM cota WHERE ident=?", (ident,)).fetchone()
            except sqlite3.Error:
                cota = None
            if cota and cota[0] < 500 and agora < cota[1] and not urgente:
                github_api._contar(con, agora, consumidor, ident, "pausa")
                raise github_api.CotaBaixa("GitHub: consumidor pausado até o reset da cota")
        if app:
            kw["env"] = {**(kw.get("env") or os.environ), "GH_TOKEN": app}
        status, r = 0, None
        try:
            r = rodar(["gh", *args], **kw)
            status = r.returncode
        except subprocess.CalledProcessError as e:
            status = e.returncode
            raise
        finally:
            if con is not None:
                github_api._contar(con, agora, consumidor, ident, "gh", status)
        if con is not None and cache and r.returncode == 0 and isinstance(r.stdout, str):
            try:
                con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                            (chave, agora, "", json.dumps({"o": r.stdout, "e": r.stderr if isinstance(r.stderr, str) else ""})))
                con.commit()
            except sqlite3.Error:
                pass
        return r
    finally:
        if con is not None:
            con.close()
