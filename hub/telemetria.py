"""Telemetria das execuções (F2): cada linha do uso.jsonl vira uma linha de `execucao` em
$HUB_DATA/claude/telemetria.db, com o que veio depois (PR, merge, no ar, retrabalho).

    python3 -m hub.telemetria importar              # uso.jsonl -> banco (idempotente: id = sha1 de inicio|conta|nome|fim|custo_usd,
                                                    # então duas execuções com o mesmo início são 2 linhas; cópia idêntica colapsa)
    python3 -m hub.telemetria atualizar-prs [--limite 20]   # merge_em pelo GitHub (hub.github_api, com orçamento)

F5 e F8 usam `caminho()`, nunca o caminho literal. Custo NULL (Codex) fica NULL: nunca soma zero.
"""
import argparse
import datetime as dt
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

from hub import conta, github_api

# Volta por trabalho mal entregue; voltas de infraestrutura (limite, parada) não entram como retrabalho
RETRABALHO = frozenset({"conferencia", "recibo", "promessa"})
COLUNAS = ("id", "issue", "conta", "executor", "modelo", "inicio", "fim", "segundos", "custo_usd", "tokens_in",
           "tokens_out", "resultado", "motivo", "pr", "merge_em", "no_ar_em", "retrabalho")
RE_PR = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
RE_ISSUE = re.compile(r"linear-(krn-\d+)", re.I)
# Padrões de token que não são chave nossa (a chave nossa sai por conta.chaves_api)
RE_SEGREDO = re.compile(r"sk-[\w-]{10,}|ghp_\w+|github_pat_\w+|lin_api_\w+|Bearer\s+\S+")
CAMPOS_TEXTO = ("motivo", "pr", "modelo", "issue")
STATUS_SUMIDO = (404, 410, 422)
ESPERA_PR = 30 * 60  # segundos entre duas conferências do mesmo PR
ESPERA_FECHADO = 24 * 3600  # PR fechado sem merge ainda é conferido 1x por dia: pode ser reaberto e mergeado


def caminho(base_dados=None) -> Path:
    return conta.pasta(base_dados) / "telemetria.db"


def conectar(base_dados=None):
    arq = caminho(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    # gancho da agenda e cron escrevem ao mesmo tempo: WAL e espera de 10 s
    con = sqlite3.connect(arq, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
      CREATE TABLE IF NOT EXISTS execucao (
        id TEXT PRIMARY KEY, issue, conta, executor, modelo, inicio, fim, segundos REAL, custo_usd REAL,
        tokens_in INTEGER, tokens_out INTEGER, resultado, motivo, pr, merge_em, no_ar_em, retrabalho INTEGER);
      CREATE INDEX IF NOT EXISTS execucao_issue ON execucao(issue);
      CREATE INDEX IF NOT EXISTS execucao_inicio ON execucao(inicio);
      CREATE INDEX IF NOT EXISTS execucao_pr ON execucao(pr);
      CREATE TABLE IF NOT EXISTS pr (url TEXT PRIMARY KEY, merge_em, estado, conferido_em, no_ar_em);
    """)
    if "no_ar_em" not in {r[1] for r in con.execute("PRAGMA table_info(pr)")}:  # banco de antes da coluna
        con.execute("ALTER TABLE pr ADD COLUMN no_ar_em")
    return con


def _pr_de(*textos):
    for t in textos:
        m = RE_PR.search(str(t or ""))
        if m:
            return "https://github.com/{}/{}/pull/{}".format(*m.groups())  # sem barra final nem fragmento
    return None


def _pr_da_saida(reg):
    """Última URL de PR do .md de saída (o texto final costuma terminar com o PR da entrega); últimos 20 KB."""
    try:
        with open(reg["saida"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20480))
            texto = f.read().decode("utf-8", "replace")
    except (OSError, KeyError, TypeError, ValueError):
        return None
    achados = RE_PR.findall(texto)
    return "https://github.com/{}/{}/pull/{}".format(*achados[-1]) if achados else None


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def id_de(reg) -> str:
    """Id estável da execução: o mesmo no gancho da agenda (reg em memória) e no cron (a linha relida do uso.jsonl)."""
    c = str(reg.get("conta") or "")
    nome = str(reg.get("nome") or "")
    return hashlib.sha1(f"{reg.get('inicio')}|{c}|{nome}|{reg.get('fim')}|{reg.get('custo_usd')!r}".encode()).hexdigest()[:16]


def de_uso(reg) -> dict:
    """Linha do uso.jsonl -> linha de `execucao`. Linhas antigas podem não ter conta, tipo, interrompida ou tokens."""
    nome = str(reg.get("nome") or "")
    c = str(reg.get("conta") or "")
    tok = reg.get("tokens") if isinstance(reg.get("tokens"), dict) else {}
    tipo = reg.get("tipo") or "outro"
    m = RE_ISSUE.fullmatch(nome)
    if reg.get("ok"):
        resultado = "ok"
    elif reg.get("limite"):
        resultado = "limite"
    elif reg.get("interrompida"):
        resultado = "parada"
    else:
        resultado = "falha"
    return {
        "id": id_de(reg),
        "issue": m.group(1).upper() if m else nome,
        "conta": c or None,
        "executor": c if c in ("codex", "api") else "claude",
        "modelo": reg.get("modelo"),
        "inicio": reg.get("inicio"),
        "fim": reg.get("fim"),
        "segundos": reg.get("duracao_s"),
        "custo_usd": reg.get("custo_usd"),  # None fica None: Codex não mede custo
        "tokens_in": _int(tok.get("entrada")) + _int(tok.get("cache_escrita")) + _int(tok.get("cache_leitura")),
        "tokens_out": _int(tok.get("saida")),
        "resultado": resultado,
        "motivo": tipo,
        "pr": _pr_de(reg.get("motivo_volta"), reg.get("pr")) or (_pr_da_saida(reg) if m else None),
        "merge_em": None,
        "no_ar_em": None,
        "retrabalho": 1 if tipo in RETRABALHO else 0,
    }


def _limpar(texto, chaves=None):
    if texto is None:
        return None
    texto = str(texto)
    for k in conta.chaves_api() if chaves is None else chaves:
        if k:
            texto = texto.replace(k, "•••")
    return RE_SEGREDO.sub("•••", texto)


def _inserir(con, e, chaves=None):
    chaves = conta.chaves_api() if chaves is None else chaves
    e = {k: e.get(k) for k in COLUNAS}
    for k in CAMPOS_TEXTO:
        e[k] = _limpar(e[k], chaves)
    if e["pr"]:  # o PR guarda merge e no ar: a execução importada depois do evento também os recebe
        r = con.execute("SELECT merge_em, no_ar_em FROM pr WHERE url=?", (e["pr"],)).fetchone()
        if r:
            e["merge_em"] = e["merge_em"] or r[0]
            e["no_ar_em"] = e["no_ar_em"] or r[1]
    con.execute(f"INSERT OR IGNORE INTO execucao ({','.join(COLUNAS)}) VALUES ({','.join('?' * len(COLUNAS))})",
                [e[k] for k in COLUNAS])


def registrar(execucao, base_dados=None) -> None:
    con = conectar(base_dados)
    try:
        with con:
            _inserir(con, execucao)
    finally:
        con.close()


def importar(desde=None, base_dados=None) -> int:
    """Lê o uso.jsonl (linha cortada já é ignorada por conta.ler_uso) e devolve quantas linhas NOVAS entraram."""
    con = conectar(base_dados)
    try:
        antes = con.execute("SELECT count(*) FROM execucao").fetchone()[0]
        ja = {r[0] for r in con.execute("SELECT id FROM execucao")}
        chaves = [k for k in conta.chaves_api() if k]  # uma leitura do disco por rodada, não por campo
        with con:
            for reg in conta.ler_uso(desde=desde, base_dados=base_dados):
                if id_de(reg) not in ja:  # já importada: nem relê o .md de saída (de_uso procura o PR nele)
                    _inserir(con, de_uso(reg), chaves)
        return con.execute("SELECT count(*) FROM execucao").fetchone()[0] - antes
    finally:
        con.close()


def marcar_no_ar(pr, quando, base_dados=None) -> int:
    con = conectar(base_dados)
    try:
        url, q = _pr_de(pr) or pr, conta.iso(quando)
        with con:  # grava no PR também: se a execução ainda não está no banco, o importar a copia de lá
            con.execute("INSERT INTO pr (url, no_ar_em) VALUES (?, ?) "
                        "ON CONFLICT(url) DO UPDATE SET no_ar_em=COALESCE(pr.no_ar_em, excluded.no_ar_em)", (url, q))
            return con.execute("UPDATE execucao SET no_ar_em=? WHERE pr=? AND no_ar_em IS NULL", (q, url)).rowcount
    finally:
        con.close()


def _get_padrao(caminho_api):
    return github_api.get(caminho_api, consumidor="telemetria", urgente=False)


def atualizar_prs(get=None, limite=20, base_dados=None, agora=None) -> int:
    """merge_em dos PRs ainda abertos, pelo GitHub: no máximo `limite` por rodada, os menos conferidos primeiro,
    nunca um PR mergeado, o fechado só 1x por dia, nunca o mesmo em menos de 30 min. Erro de rede ou rate limit encerra a rodada."""
    get = get or _get_padrao
    agora = agora or conta.agora()
    corte = conta.iso(agora - dt.timedelta(seconds=ESPERA_PR))
    corte_fechado = conta.iso(agora - dt.timedelta(seconds=ESPERA_FECHADO))
    con = conectar(base_dados)
    feitos = 0
    try:
        alvos = con.execute("""
          SELECT e.pr FROM (SELECT DISTINCT pr FROM execucao WHERE pr IS NOT NULL) e LEFT JOIN pr p ON p.url = e.pr
          WHERE COALESCE(p.estado, '') NOT IN ('merged', 'sumido') AND (p.conferido_em IS NULL OR p.conferido_em <=
                CASE WHEN p.estado = 'closed' THEN ? ELSE ? END)
          ORDER BY p.conferido_em IS NOT NULL, p.conferido_em, e.pr LIMIT ?""", (corte_fechado, corte, limite)).fetchall()
        for (url,) in alvos:
            m = RE_PR.fullmatch(url)
            if not m:
                continue
            try:
                r = get("repos/{}/{}/pulls/{}".format(*m.groups()))
            except github_api.ErroHTTP as e:
                if e.status not in STATUS_SUMIDO:
                    break
                r = {"sumido": True}  # repo renomeado ou URL errada: marca e segue, senão trava a fila
            except Exception:
                break  # cota baixa, rede ou outro status: não insiste com o resto
            r = r if isinstance(r, dict) else {}
            merge = conta.iso(conta.de_iso(r["merged_at"])) if r.get("merged_at") else None
            estado = ("sumido" if r.get("sumido") else "merged" if merge
                      else "closed" if r.get("state") == "closed" else "open")
            with con:
                con.execute("INSERT INTO pr (url, merge_em, estado, conferido_em) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(url) DO UPDATE SET merge_em=excluded.merge_em, estado=excluded.estado, "
                            "conferido_em=excluded.conferido_em", (url, merge, estado, conta.iso(agora)))
                if merge:
                    con.execute("UPDATE execucao SET merge_em=? WHERE pr=?", (merge, url))
            feitos += 1
    finally:
        con.close()
    return feitos


def main(argv=None):
    p = argparse.ArgumentParser(prog="hub.telemetria")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("importar")
    a = sub.add_parser("atualizar-prs")
    a.add_argument("--limite", type=int, default=20)
    args = p.parse_args(argv)
    if args.cmd == "importar":
        n = importar()
        con = conectar()
        total = con.execute("SELECT count(*) FROM execucao").fetchone()[0]
        con.close()
        print(f"{n} novas, total {total}")
    else:
        print(f"{atualizar_prs(limite=args.limite)} PRs conferidos")


if __name__ == "__main__":
    sys.exit(main())
