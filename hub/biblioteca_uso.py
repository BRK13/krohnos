"""Uso da biblioteca: o que o despacho injetou em cada issue e o que os PRs declararam ter usado.

- Injeção: `bloco_da_issue(item)` (chamado por linear.tarefa) acha até 3 receitas pelo índice híbrido, devolve o bloco do prompt e
  grava a injeção por issue (a mesma issue com o mesmo texto reaproveita o bloco: nada de chamar embeddings a cada tick).
- Prova de uso: o PR que usou receita cita no corpo "Receita: receitas/<tema>/<arquivo>.md". `coletar()` (cron de 10 min, pelo
  deploy/biblioteca.sh) lê os PRs pela busca do GitHub e grava (receita, PR, issue, data). A tela /biblioteca?vista=uso mostra por
  receita onde foi aplicada e quais ficaram 30 dias sem uso.

SQLite em $HUB_DATA/biblioteca-uso.db (fora do clone da biblioteca, que o cron apaga e recria).

    python3 -m hub.biblioteca_uso coletar            lê os PRs e grava o uso
    python3 -m hub.biblioteca_uso relatorio [dias]   uso por receita e as que ficaram sem uso (padrão 30 dias)
    python3 -m hub.biblioteca_uso balanco [dias]     issues com receita injetada × receitas citadas em PR (o comentário de 7 dias)
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from hub import biblioteca as B, biblioteca_indice as I, data, gh

RE_RECEITA = re.compile(r"receitas/[\w.+-]+/[\w.+/-]+?\.md")
RE_LINHA = re.compile(r"(?im)^[ \t>*-]*receitas?\s*:\s*(.+)$")
RE_ISSUE = re.compile(r"\bKRN-\d+\b")
TIMEOUT_EMBED = 8  # o despacho não espera mais que isso pelo OpenRouter: sem vetor, sem injeção


def caminho_db():
    return Path(os.environ.get("BIBLIOTECA_USO") or data() / "biblioteca-uso.db")


def _conn(p=None):
    p = Path(p or caminho_db())
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
        create table if not exists consulta(issue text primary key, hash text, ts text, bloco text);
        create table if not exists injecao(issue text, caminho text, nota real, ts text, primary key(issue, caminho));
        create table if not exists uso(caminho text, pr text, repo text, issue text, titulo text, data text, primary key(caminho, pr));""")
    return c


def _agora():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- injeção no despacho ----

def bloco_da_issue(item, db=None, indice=None, embed=None):
    """Bloco "Receitas da biblioteca que se aplicam" para o prompt da issue ("" se nenhuma passa do limiar ou se algo falhar:
    a biblioteca nunca derruba o despacho)."""
    if item.get("tipo", "issue") != "issue":
        return ""
    try:
        consulta = I.consulta_da_issue(item.get("titulo", ""), item.get("descricao", ""), item.get("labels") or [])
        try:  # a versão do índice entra no hash: receita nova, mudada ou arquivada refaz o bloco
            versao = str(Path(indice or I.caminho_db()).stat().st_mtime_ns)
        except OSError:
            versao = ""
        h = hashlib.sha1((consulta + "\0" + versao).encode()).hexdigest()
        with contextlib.closing(_conn(db)) as c, c:
            ja = c.execute("select hash, bloco from consulta where issue=?", (item["identifier"],)).fetchone()
            if ja and ja["hash"] == h:
                return ja["bloco"]
            if not I.disponivel(indice):
                return ""
            ix = I._indice(indice)
            if not ix.tem_vec.any():  # índice ainda sem vetores: tenta de novo quando o cron completar
                return ""
            # o vetor da consulta sai daqui (e levanta se o OpenRouter falhar): falha passageira não pode ficar gravada como
            # "nenhuma receita serve" para a issue inteira
            vetor = (embed or (lambda t: I.embeddar(t, timeout=TIMEOUT_EMBED)))([consulta])[0]
            achados = I.relevantes(consulta, db=indice, vetor=vetor)
            bloco = I.bloco_prompt(achados)
            ts = _agora()
            c.execute("insert or replace into consulta values(?,?,?,?)", (item["identifier"], h, ts, bloco))
            for s in achados:
                c.execute("insert or ignore into injecao values(?,?,?,?)", (item["identifier"], s["caminho"], s["cos"], ts))
            return bloco
    except Exception as ex:  # índice sem vetor, OpenRouter fora, disco: segue sem o bloco (e sem gravar: tenta no próximo tick)
        print(f"aviso: biblioteca sem injeção em {item.get('identifier')}: {type(ex).__name__}: {str(ex)[:100]}", file=sys.stderr)
        return ""


# ---- prova de uso ----

def receitas_citadas(corpo):
    """Caminhos citados em linhas "Receita: receitas/…" (várias por linha, separadas por vírgula ou espaço); sem repetir."""
    out = []
    for linha in RE_LINHA.findall(corpo or ""):
        for m in RE_RECEITA.findall(linha):
            if m not in out:
                out.append(m)
    return out


def registrar_pr(c, pr):
    """pr: {url, repo, titulo, corpo, data}. Devolve quantas receitas gravou."""
    cit = receitas_citadas(pr.get("corpo"))
    m = RE_ISSUE.search(pr.get("corpo") or "") or RE_ISSUE.search(pr.get("titulo") or "")
    for cam in cit:
        c.execute("insert or replace into uso values(?,?,?,?,?,?)",
                  (cam, pr["url"], pr.get("repo", ""), m.group(0) if m else "", (pr.get("titulo") or "")[:120], (pr.get("data") or "")[:10]))
    return len(cit)


def _gh(args):
    r = gh.executar(args, "biblioteca_uso", cache=args[0] in ("search", "pr"), capture_output=True, text=True, encoding="utf-8", timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200])
    return json.loads(r.stdout or "[]")


def coletar(db=None, gh=_gh, donos=None):
    """Busca no GitHub os PRs (abertos, fechados e fundidos) cujo corpo cita "Receita: receitas/" e grava o uso."""
    donos = donos or [B.REPO.split("/")[0], "jpexemplo"]
    total = 0
    c = _conn(db)
    with c:
        for dono in dict.fromkeys(donos):
            try:
                prs = gh(["search", "prs", "Receita: receitas/", "--owner", dono, "--match", "body", "--limit", "100",
                          "--json", "url,repository,title,body,createdAt"])
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as ex:
                print(f"aviso: busca de PRs de {dono}: {ex}", file=sys.stderr)
                continue
            for p in prs:
                total += registrar_pr(c, {"url": p["url"], "repo": (p.get("repository") or {}).get("nameWithOwner", ""),
                                          "titulo": p.get("title"), "corpo": p.get("body"), "data": p.get("createdAt")})
    c.close()
    return total


def medindo_desde(db=None):
    """AAAA-MM-DD da primeira consulta do despacho (quando a medição de uso começou); "" se nunca rodou."""
    with contextlib.closing(_conn(db)) as c:
        x = c.execute("select min(ts) t from consulta").fetchone()["t"]
    return (x or "")[:10]


# ---- relatório (tela do hub) ----

def relatorio(dias=30, agora=None, db=None, r=None):
    """{"receitas": [{caminho, titulo, tema, status, injetada_em: [issues], usada_em: [{issue, pr, data}], ultimo_uso}],
        "sem_uso": [caminho… sem PR nos últimos `dias`], "issues_com_receita", "issues_consultadas", "prs_com_receita"}"""
    agora = agora or dt.datetime.now(dt.timezone.utc)
    corte = (agora - dt.timedelta(days=dias)).strftime("%Y-%m-%d")
    c = _conn(db)
    inj, uso = {}, {}
    for x in c.execute("select issue, caminho from injecao order by ts"):
        inj.setdefault(x["caminho"], []).append(x["issue"])
    for x in c.execute("select caminho, pr, issue, data from uso order by data desc"):
        uso.setdefault(x["caminho"], []).append({"issue": x["issue"], "pr": x["pr"], "data": x["data"]})
    consultadas = c.execute("select count(*) n from consulta").fetchone()["n"]
    com = c.execute("select count(distinct issue) n from injecao").fetchone()["n"]
    prs = c.execute("select count(distinct pr) n from uso").fetchone()["n"]
    c.close()
    itens = []
    for x in B.receitas(r):
        u = uso.get(x["caminho"], [])
        itens.append({"caminho": x["caminho"], "titulo": x["titulo"], "tema": x["tema"], "status": x["status"],
                      "data": x["data"], "projetos": x["projetos"], "injetada_em": inj.get(x["caminho"], []), "usada_em": u,
                      "ultimo_uso": u[0]["data"] if u else ""})
    return {"receitas": itens, "dias": dias, "corte": corte, "issues_consultadas": consultadas, "issues_com_receita": com,
            "prs_com_receita": prs,
            "sem_uso": [i["caminho"] for i in itens if not i["ultimo_uso"] or i["ultimo_uso"] < corte]}


def balanco(dias=7, agora=None, db=None):
    """O comentário de 7 dias: issues que receberam receita, e quantas delas viram PR citando alguma receita."""
    agora = agora or dt.datetime.now(dt.timezone.utc)
    corte = (agora - dt.timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = _conn(db)
    cons = c.execute("select count(*) n from consulta where ts >= ?", (corte,)).fetchone()["n"]
    inj = {x["issue"] for x in c.execute("select distinct issue from injecao where ts >= ?", (corte,))}
    usadas = {x["issue"] for x in c.execute("select distinct issue from uso where data >= ?", (corte[:10],)) if x["issue"]}
    c.close()
    return {"dias": dias, "issues_despachadas": cons, "com_receita": len(inj), "usaram": len(inj & usadas),
            "pr_com_receita_fora_da_injecao": len(usadas - inj)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["coletar"]:
        print(coletar(), "citações gravadas")
        return 0
    if argv[:1] == ["relatorio"]:
        r = relatorio(int(argv[1]) if len(argv) > 1 else 30)
        print(json.dumps({k: v for k, v in r.items() if k != "receitas"}, ensure_ascii=False, indent=1))
        return 0
    if argv[:1] == ["balanco"]:
        print(json.dumps(balanco(int(argv[1]) if len(argv) > 1 else 7), ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
