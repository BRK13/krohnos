"""fila de issues própria (SQLite), com o nó no formato do GraphQL do Linear para o resto do hub não mudar.
`consulta` aceita só as operações que o hub usa (issue, commentCreate, issueUpdate, issueAddLabel, issueCreate, teams,
projects); o resto levanta SemEquivalente."""
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from hub import RAIZ, config, conta, data, esperas

ESTADOS = {"Backlog": "backlog", "Todo": "unstarted", "In Progress": "started", "In Review": "started",
           "Done": "completed", "Canceled": "canceled"}
TICK_MIN_S = 10  # no máximo um `agenda.py tick` por este intervalo
RE_REPO_LINHA = re.compile(r"^\s*repo:\s*(\S+)\s*$", re.I)


class SemEquivalente(RuntimeError):
    """Consulta do Linear sem equivalente na fila local."""


def provedor():
    """"local" ou "linear": config/env; vazio decide só pelo NOME do segredo (chave() pode ir à rede)."""
    explicito = (config.get("issues.provedor") or "").strip().lower()
    if explicito in ("local", "linear"):
        return explicito
    from hub import linear
    return "linear" if linear._segredo("LINEAR_API_KEY") or linear._segredo("LINEAR_APP2_CLIENT_ID") else "local"


def local():
    return provedor() == "local"


def _prefixo():
    return (config.get("issues.prefixo") or "KRO").strip().upper()


def _agora():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pasta():
    # na pasta da conta Claude: o usuário `claude` (que grava a fila pelo CLI) não cria nada em <dados>
    return conta.pasta() / "issues"


def _pasta_antiga():
    return Path(data()) / "issues"  # até o PR #797; _migrar_antiga traz a fila de lá


def _migrar_antiga(destino):
    """Fila criada no caminho antigo e nenhuma no novo: copia o banco (API de backup, leva o WAL junto) e publica
    com os.link, que não sobrescreve; outro processo que migrou antes ganha. Banco antigo ilegível levanta, para
    não começar uma fila vazia que reusaria os identificadores KRO-N."""
    antigo = _pasta_antiga() / "fila.db"
    if destino.exists() or not antigo.exists():
        return
    tmp = destino.with_name(f"fila.migrando.{os.getpid()}.db")
    try:
        origem = sqlite3.connect(f"file:{antigo}?mode=ro", uri=True, timeout=5)
        try:
            alvo = sqlite3.connect(tmp)
            try:
                origem.backup(alvo)
            finally:
                alvo.close()
        finally:
            origem.close()
        with contextlib.suppress(FileExistsError):
            os.link(tmp, destino)
    except sqlite3.Error as e:
        raise PermissionError(f"fila antiga em {antigo} não abriu ({e}); dê leitura ao usuário {os.getenv('USER', '')} "
                              f"ou copie-a para {destino}") from e
    finally:
        tmp.unlink(missing_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS issues(ident TEXT PRIMARY KEY, titulo TEXT NOT NULL, descricao TEXT NOT NULL DEFAULT '',
  projeto TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '', estado TEXT NOT NULL, prioridade INTEGER NOT NULL DEFAULT 3,
  labels TEXT NOT NULL DEFAULT '[]', app INTEGER NOT NULL DEFAULT 1, criada TEXT NOT NULL, atualizada TEXT NOT NULL,
  concluida TEXT);
CREATE TABLE IF NOT EXISTS comentarios(id INTEGER PRIMARY KEY AUTOINCREMENT, ident TEXT NOT NULL, texto TEXT NOT NULL,
  app INTEGER NOT NULL, autor TEXT NOT NULL, criado TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS comentarios_ident ON comentarios(ident);
CREATE TABLE IF NOT EXISTS historico(id INTEGER PRIMARY KEY AUTOINCREMENT, ident TEXT NOT NULL, quando TEXT NOT NULL,
  estado TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS historico_ident ON historico(ident);
CREATE TABLE IF NOT EXISTS contador(prefixo TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


_INICIADOS = set()  # caminhos já preparados (WAL + esquema) neste processo
_TRAVA = threading.Lock()


def _tentar(fn, vezes=40):
    """O SQLite devolve "database is locked" na troca de modo/esquema sem respeitar o busy_timeout: repete rápido."""
    for i in range(vezes):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower() or i == vezes - 1:
                raise
            time.sleep(0.05 + 0.02 * i)


def _abrir(arq):
    c = sqlite3.connect(arq, timeout=5, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _preparar(arq):
    """WAL e esquema uma vez por processo e caminho, serializado; entre processos, com repetição."""
    with _TRAVA:
        if arq in _INICIADOS:
            return

        def ini():
            c = _abrir(arq)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
            finally:
                c.close()
        _tentar(ini)
        _INICIADOS.add(arq)


@contextlib.contextmanager
def _db(escrita=False):
    """Conexão por chamada (thread-safe); escrita abre BEGIN IMMEDIATE e confirma ao sair."""
    _pasta().mkdir(parents=True, exist_ok=True)
    arq = str(_pasta() / "fila.db")
    if arq not in _INICIADOS:
        _migrar_antiga(Path(arq))
    _preparar(arq)
    c = _abrir(arq)
    try:
        if escrita:
            _tentar(lambda: c.execute("BEGIN IMMEDIATE"))
        try:
            yield c
        except BaseException:
            if escrita:
                c.execute("ROLLBACK")
            raise
        if escrita:
            c.execute("COMMIT")
    finally:
        c.close()


def _estado(nome):
    for k in ESTADOS:
        if k.lower() == (nome or "").strip().lower():
            return k
    raise ValueError(f"estado desconhecido: {nome!r} (valem: {', '.join(ESTADOS)})")


def _prs(*textos):
    vistos = {}
    for t in textos:
        for m in esperas.RE_PR.finditer(t or ""):
            vistos.setdefault(f"https://github.com/{m[1]}/pull/{m[2]}", f"PR #{m[2]} {m[1]}")
    return [{"url": u, "title": n} for u, n in vistos.items()]


def _repo_do(c, projeto):
    """Repo do projeto: o último usado na fila, senão o cadastro."""
    r = c.execute("SELECT repo FROM issues WHERE lower(projeto)=lower(?) AND repo<>'' ORDER BY criada DESC LIMIT 1",
                  (projeto,)).fetchone()
    return r["repo"] if r else next((p["description"][6:].strip() for p in _cadastro()
                                     if p["name"].lower() == projeto.lower()), "")


def _no(c, r):
    cs = c.execute("SELECT * FROM comentarios WHERE ident=? ORDER BY criado, id", (r["ident"],)).fetchall()
    hs = c.execute("SELECT * FROM historico WHERE ident=? ORDER BY quando, id", (r["ident"],)).fetchall()
    labels = json.loads(r["labels"])
    comentarios = [{"id": str(x["id"]), "body": x["texto"], "createdAt": x["criado"],
                    "user": {"name": x["autor"], "app": bool(x["app"])}} for x in cs]
    projeto = ({"name": r["projeto"], "description": f"repo: {r['repo']}" if r["repo"] else ""} if r["projeto"] else None)
    return {"id": r["ident"], "identifier": r["ident"], "title": r["titulo"], "description": r["descricao"],
            "url": f"{config.painel_url()}/issues/{r['ident']}", "priority": r["prioridade"],
            "createdAt": r["criada"], "updatedAt": r["atualizada"], "completedAt": r["concluida"],
            "creator": {"app": bool(r["app"]), "name": "Claude" if r["app"] else config.dono(True)},
            "state": {"name": r["estado"], "type": ESTADOS[r["estado"]]},
            "labels": {"nodes": [{"name": n} for n in labels]},
            "project": projeto,
            "comments": {"nodes": comentarios},
            "history": {"nodes": [{"createdAt": h["quando"], "toState": {"type": ESTADOS[h["estado"]], "name": h["estado"]}}
                                  for h in hs]},
            "attachments": {"nodes": _prs(r["descricao"], *[x["texto"] for x in cs])},
            "team": _time()}


def _time():
    return {"id": _prefixo(), "states": {"nodes": [{"id": n, "name": n} for n in ESTADOS]}}


def _linha(c, ident):
    r = c.execute("SELECT * FROM issues WHERE ident=? COLLATE NOCASE", (ident or "",)).fetchone()
    if not r:
        raise LookupError(f"issue não existe: {ident}")
    return r


def criar(projeto, titulo, descricao="", estado="Backlog", labels=(), prioridade=3, repo="", app=True, ident=None,
          criada=None):
    """Cria a issue e devolve o nó. `ident` e `criada` só na importação (mantêm o número e a data de origem)."""
    estado = _estado(estado)
    agora = _agora()
    criada = criada or agora
    with _db(True) as c:
        if ident:
            ident = ident.upper()
            pref, _, n = ident.rpartition("-")
            if pref == _prefixo() and n.isdigit():  # número importado não pode ser reusado pelo contador
                c.execute("INSERT INTO contador(prefixo, n) VALUES(?, ?) ON CONFLICT(prefixo) DO UPDATE SET n=max(n, ?)",
                          (pref, int(n), int(n)))
        else:
            pref = _prefixo()
            c.execute("INSERT INTO contador(prefixo, n) VALUES(?, 1) ON CONFLICT(prefixo) DO UPDATE SET n=n+1", (pref,))
            ident = f"{pref}-{c.execute('SELECT n FROM contador WHERE prefixo=?', (pref,)).fetchone()['n']}"
        repo = repo or (_repo_do(c, projeto) if projeto else "")
        c.execute("INSERT INTO issues(ident,titulo,descricao,projeto,repo,estado,prioridade,labels,app,criada,atualizada,concluida)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ident, titulo, descricao or "", projeto or "", repo, estado, int(prioridade),
                   json.dumps(list(dict.fromkeys(labels))), int(bool(app)), criada, agora,
                   criada if ESTADOS[estado] == "completed" else None))
        c.execute("INSERT INTO historico(ident, quando, estado) VALUES(?,?,?)", (ident, criada, estado))
        return _no(c, _linha(c, ident))


def comentar(ident, texto, app=False, autor=None, criado=None, acordar=True):
    """Comentário do dono (app=False, acorda a agenda) ou do Claude (app=True). Devolve o comentário.
    `acordar=False` (importação) grava a fala do dono sem disparar a agenda."""
    criado = criado or _agora()
    with _db(True) as c:
        r = _linha(c, ident)
        cur = c.execute("INSERT INTO comentarios(ident, texto, app, autor, criado) VALUES(?,?,?,?,?)",
                        (r["ident"], texto, int(bool(app)), autor or ("Claude" if app else config.dono(True)), criado))
        c.execute("UPDATE issues SET atualizada=? WHERE ident=?", (max(criado, _agora()), r["ident"]))
        x = c.execute("SELECT * FROM comentarios WHERE id=?", (cur.lastrowid,)).fetchone()
    if not app and acordar:
        acordar_agenda()
    return {"id": str(x["id"]), "body": x["texto"], "createdAt": x["criado"], "user": {"name": x["autor"], "app": bool(x["app"])}}


def mover(ident, estado):
    estado = _estado(estado)
    agora = _agora()
    with _db(True) as c:
        r = _linha(c, ident)
        c.execute("UPDATE issues SET estado=?, atualizada=?, concluida=? WHERE ident=?",
                  (estado, agora, agora if ESTADOS[estado] == "completed" else None, r["ident"]))
        c.execute("INSERT INTO historico(ident, quando, estado) VALUES(?,?,?)", (r["ident"], agora, estado))


def por_label(ident, label):
    with _db(True) as c:
        r = _linha(c, ident)
        ls = json.loads(r["labels"])
        if label not in ls:
            c.execute("UPDATE issues SET labels=?, atualizada=? WHERE ident=?",
                      (json.dumps(ls + [label]), _agora(), r["ident"]))


def item_por_ident(ident):
    with _db() as c:
        r = c.execute("SELECT * FROM issues WHERE ident=? COLLATE NOCASE", (ident or "",)).fetchone()
        return _no(c, r) if r else None


def listar(estados=None, desde=None):
    """Nós por `atualizada` (mais antigas primeiro); `estados` por nome sem caixa; `desde` ISO (atualizada >= desde)."""
    sql, args = "SELECT * FROM issues WHERE 1=1", []
    if estados is not None:
        nomes = [_estado(e) for e in estados]
        sql += f" AND estado IN ({','.join('?' * len(nomes))})"
        args += nomes
    if desde:
        sql += " AND atualizada >= ?"
        args.append(desde)
    with _db() as c:
        return [_no(c, r) for r in c.execute(sql + " ORDER BY atualizada, ident", args).fetchall()]


def _cadastro():
    """Projetos de config.projetos() (*.json com nome e repo)."""
    achados = []
    with contextlib.suppress(OSError):
        for arq in sorted(Path(config.projetos()).glob("*.json")):
            try:
                j = json.loads(arq.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            repo = j.get("repo")
            repo = repo.get("github") if isinstance(repo, dict) else repo
            if isinstance(j, dict) and j.get("nome"):
                achados.append({"name": j["nome"], "description": f"repo: {repo}" if repo else ""})
    return achados


def projetos():
    """Cadastro mais os projetos já usados na fila: [{"name", "description"}]."""
    por_nome = {p["name"].lower(): p for p in _cadastro()}
    with _db() as c:
        for r in c.execute("SELECT projeto, repo FROM issues WHERE projeto<>'' ORDER BY criada").fetchall():
            atual = por_nome.setdefault(r["projeto"].lower(), {"name": r["projeto"], "description": ""})
            if r["repo"] and not atual["description"]:
                atual["description"] = f"repo: {r['repo']}"
    return list(por_nome.values())


def projeto_do_repo(repo):
    alvo = (repo or "").strip().lower()
    for p in projetos():
        m = RE_REPO_LINHA.match(p["description"])
        if alvo and m and m[1].lower() == alvo:
            return p["name"]
    return ""


# ---- resolvedor no formato do gql ----

def _arg(query, chave, v):
    """Valor do argumento `chave: $var` ou `chave: "literal"` da consulta; None se não está lá."""
    m = re.search(rf'(?<![\w$]){chave}:\s*(?:\$(\w+)|"([^"]*)")', query)
    if not m:
        return None
    return v.get(m[1]) if m[1] else m[2]


def _unico(v):
    """O valor da única variável (o nome varia: i, id, ident...)."""
    return next(iter(v.values()), None)


def consulta(query, app=False, **v):
    """Resolve no SQLite as consultas do Linear que o hub usa; devolve o `data` (como `linear.gql`)."""
    q = query
    if "commentCreate" in q:
        if _arg(q, "issueId", v) is None:  # comentário de update de projeto etc.
            raise SemEquivalente("commentCreate sem issueId")
        comentar(_arg(q, "issueId", v), _arg(q, "body", v), app=app)
        return {"commentCreate": {"success": True}}
    if "issueUpdate" in q:
        if _arg(q, "stateId", v) is None:
            raise SemEquivalente("issueUpdate sem stateId")
        mover(_arg(q, "id", v), _arg(q, "stateId", v))
        return {"issueUpdate": {"success": True}}
    if "issueAddLabel" in q:
        por_label(_arg(q, "id", v), _arg(q, "labelId", v))
        return {"issueAddLabel": {"success": True}}
    if "issueCreate" in q:
        i = next((x for x in v.values() if isinstance(x, dict) and "title" in x), None)
        if i is None:
            raise SemEquivalente("issueCreate sem input com title")
        projeto = i.get("projectId") or ""
        n = criar(projeto, i.get("title") or "", i.get("description") or "", i.get("stateId") or "Backlog",
                  i.get("labelIds") or (), i.get("priority", 3), app=app)
        return {"issueCreate": {"issue": {"identifier": n["identifier"], "url": n["url"]}}}
    if re.search(r"\bissue\(", q):
        ident = _arg(q, "id", v) or _unico(v)
        return {"issue": item_por_ident(ident)}
    if re.search(r"\bteams\(", q):
        with _db() as c:
            ls = sorted({n for r in c.execute("SELECT labels FROM issues").fetchall() for n in json.loads(r["labels"])})
        return {"teams": {"nodes": [{**_time(), "labels": {"nodes": [{"id": n, "name": n} for n in ls]}}]}}
    if re.search(r"\bprojects\(", q):
        return {"projects": {"nodes": [{"id": p["name"], **p} for p in projetos()]}}
    raise SemEquivalente(f"sem equivalente na fila local: {q.strip()[:80]}")


def acordar_agenda():
    """Dispara `deploy/agenda.py tick` solto (a fala do dono não espera o cron); no máximo um a cada 10 s."""
    if os.environ.get("KROHNOS_SEM_TICK") == "1":
        return
    marca = _pasta() / "tick.ts"
    _pasta().mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if time.time() - marca.stat().st_mtime < TICK_MIN_S:
            return
    marca.touch()
    subprocess.Popen([sys.executable, str(RAIZ / "deploy" / "agenda.py"), "tick"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---- migração opcional do Linear ----

ABERTAS = ("backlog", "unstarted", "started")  # tipos de estado do Linear: Backlog, Todo, In Progress e In Review
POR_TIPO = {"backlog": "Backlog", "unstarted": "Todo", "started": "In Progress", "completed": "Done",
            "canceled": "Canceled"}
_CAMPOS_COMENTARIO = "nodes { body createdAt user { name app } } pageInfo { hasNextPage endCursor }"
_CAMPOS_IMPORTAR = ("identifier title description priority createdAt updatedAt completedAt creator { app } "
                    "state { name type } labels { nodes { name } } project { name description } "
                    "comments(first: 100) { %s }" % _CAMPOS_COMENTARIO)
_Q_MAIS_COMENTARIOS = ("query($i: String!, $a: String) { issue(id: $i) { comments(first: 100, after: $a) { %s } } }"
                       % _CAMPOS_COMENTARIO)


def _q_importar(todas):
    filtro = "" if todas else ', filter: {state: {type: {in: [%s]}}}' % ", ".join(f'"{t}"' for t in ABERTAS)
    return ("query($a: String) { issues(first: 50, after: $a%s) { nodes { %s } pageInfo { hasNextPage endCursor } } }"
            % (filtro, _CAMPOS_IMPORTAR))


def _nos_do_linear(consulta, todas):
    q, depois = _q_importar(todas), None
    while True:
        r = consulta(q, a=depois)["issues"]
        yield from r["nodes"]
        pg = r.get("pageInfo") or {}
        if not pg.get("hasNextPage") or not pg.get("endCursor"):
            return
        depois = pg["endCursor"]


def _comentarios_do_linear(consulta, n):
    """Todos os comentários da issue: a primeira página vem na listagem; o resto, página a página."""
    pagina = n.get("comments") or {}
    while True:
        yield from pagina.get("nodes") or []
        pg = pagina.get("pageInfo") or {}
        if not pg.get("hasNextPage") or not pg.get("endCursor"):
            return
        pagina = (consulta(_Q_MAIS_COMENTARIOS, i=n["identifier"], a=pg["endCursor"]).get("issue") or {}).get(
            "comments") or {}


def _estado_importado(st):
    nome = (st or {}).get("name") or ""
    for k in ESTADOS:
        if k.lower() == nome.lower():
            return k
    return POR_TIPO.get((st or {}).get("type"), "Backlog")


def _repo_da_descricao(descricao):
    m = re.search(r"^\s*repo:\s*(\S+)\s*$", descricao or "", re.I | re.M)
    return m[1] if m else ""


def importar_linear(consulta=None, todas=False, confirmar=True):
    """Copia as issues do Linear para a fila local, mantendo identificador, estado, labels, prioridade, projeto/repo,
    comentários e datas. Repetir não duplica issue nem comentário. `confirmar=False` só diz o que faria.
    `consulta` padrão: o Linear real (`linear._gql_linear`), mesmo com provedor local."""
    if consulta is None:
        from hub import linear
        consulta = linear._gql_linear
    importadas, ja_existiam, conflitos = [], [], []
    for n in _nos_do_linear(consulta, todas):
        ident = (n.get("identifier") or "").upper()
        if not ident:
            continue
        existente = item_por_ident(ident)
        if existente and existente.get("createdAt") != n.get("createdAt"):  # KRO-1 criada aqui ≠ KRO-1 do Linear
            conflitos.append(ident)
            continue
        (ja_existiam if existente else importadas).append(ident)
        if not confirmar:
            continue
        if not existente:
            proj = n.get("project") or {}
            criar(proj.get("name") or "", n.get("title") or ident, n.get("description") or "",
                  _estado_importado(n.get("state")), [lb["name"] for lb in (n.get("labels") or {}).get("nodes", [])],
                  n.get("priority") or 0, repo=_repo_da_descricao(proj.get("description")),
                  app=bool((n.get("creator") or {}).get("app")), ident=ident, criada=n.get("createdAt"))
        with _db() as c:
            tem = {(x["texto"], x["criado"], x["app"]) for x in
                   c.execute("SELECT texto, criado, app FROM comentarios WHERE ident=?", (ident,)).fetchall()}
        for cm in _comentarios_do_linear(consulta, n):
            u = cm.get("user") or {}
            app = bool(u.get("app"))
            if (cm.get("body") or "", cm.get("createdAt"), int(app)) in tem:
                continue
            comentar(ident, cm.get("body") or "", app=app, autor=u.get("name") or "Linear", criado=cm.get("createdAt"),
                     acordar=False)
        if not existente:  # a data de mexida e a de conclusão são as de origem, não as da importação
            with _db(True) as c:
                c.execute("UPDATE issues SET atualizada=?, concluida=? WHERE ident=?",
                          (n.get("updatedAt") or n.get("createdAt") or _agora(),
                           n.get("completedAt") if _estado_importado(n.get("state")) == "Done" else None, ident))
    return {"importadas": importadas, "ja_existiam": ja_existiam, "conflitos": conflitos}


# ---- CLI ----

def _resumo(no):
    return f"{no['identifier']}  {no['state']['name']:<11} {no['title']}"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m hub.fila_issues", description="Fila de issues própria do hub.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar", help="issues da fila local")
    ver = sub.add_parser("ver", help="uma issue com a conversa")
    ver.add_argument("ident")
    imp = sub.add_parser("importar-linear", help="copia as issues do Linear para a fila local")
    imp.add_argument("--todas", action="store_true", help="inclui Done e Canceled (padrão: só as abertas)")
    imp.add_argument("--confirmar", action="store_true", help="grava (sem isto só mostra o que faria)")
    a = ap.parse_args(argv)
    if a.cmd == "listar":
        for no in listar():
            print(_resumo(no))
    elif a.cmd == "ver":
        no = item_por_ident(a.ident)
        if not no:
            print(f"issue não existe: {a.ident}", file=sys.stderr)
            return 1
        print(_resumo(no))
        print(f"projeto: {(no['project'] or {}).get('name', '-')}  labels: {', '.join(x['name'] for x in no['labels']['nodes']) or '-'}")
        print(f"\n{no['description']}")
        for cm in no["comments"]["nodes"]:
            print(f"\n[{cm['createdAt']}] {cm['user']['name']}{' (app)' if cm['user']['app'] else ''}:\n{cm['body']}")
    else:
        try:
            r = importar_linear(todas=a.todas, confirmar=a.confirmar)
        except RuntimeError as e:
            print(f"importar-linear: {e}", file=sys.stderr)
            return 1
        quando = "importadas" if a.confirmar else "seriam importadas (rode com --confirmar para gravar)"
        print(f"{len(r['importadas'])} {quando}: {', '.join(r['importadas']) or '-'}")
        print(f"{len(r['ja_existiam'])} já existiam: {', '.join(r['ja_existiam']) or '-'}")
        if r.get("conflitos"):
            print(f"{len(r['conflitos'])} puladas: o identificador já é de outra issue criada aqui: {', '.join(r['conflitos'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
