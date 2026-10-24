"""Esteira: a cota das contas da VPS vira trabalho sem o dono pedir (plano docs/plans/feitos/2026-10-09-esteira.md).

O Linear é o estoque (issues com a label `esteira` no Backlog, criadas pelo abastecedor). O despachante roda de 10 em
10 min (cron do claude) e, se houver folga nas contas, move as melhores para Todo; a ponte (hub/linear.py) executa.
Faixa Codex: issue apta (apta_codex) ganha a label `codex` ao sair quando as contas Claude não têm vaga, e
numa fatia fixa (FATIA_CODEX) mesmo com folga; o Codex tem as vagas dele (hub.json → codex.vagas).

    python3 -m hub.esteira despachar   # o cron
    python3 -m hub.esteira estado      # o que tem no estoque, folga e o que saiu nos últimos 7 dias
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from hub import config, conta, data, roteamento

TETO_CINCO_HORAS = 70


def folga(uso, ocupadas, contas_ativas=None):
    """{conta: tem folga?}, só das contas com login (conta sem login não pega trabalho). Sem dado de uso de nenhuma
    conta: só com fila e rodando vazios."""
    contas = (uso or {}).get("contas") or {}
    sem_dado = not any((c or {}).get("fonte") == "oauth" for c in contas.values())
    saida = {}
    for n in contas_ativas or conta.com_login():
        c = contas.get(n) or {}
        if sem_dado or c.get("fonte") != "oauth":
            saida[n] = ocupadas == 0
            continue
        cinco = (c.get("cinco_horas") or {}).get("pct")
        saida[n] = cinco is None or cinco < TETO_CINCO_HORAS  # conta Claude não tem limite semanal
    return saida


VAGAS_POR_CONTA = 5  # = agenda.VAGAS: cada conta roda até 5 sessões; ocupadas conta tudo que roda nas contas


def vagas(folgas, ocupadas):
    """Quantas issues despachar agora: vagas das contas com folga menos o que já espera ou roda."""
    return max(VAGAS_POR_CONTA * sum(1 for v in folgas.values() if v) - ocupadas, 0)


MAX_EM_ANDAMENTO = 20  # o dono, 12/10: moer sem dó (4 contas × 5 vagas)
MAX_POR_PROJETO_DIA = None  # o dono, 11/10: sem teto por projeto/dia
Q_ESTOQUE = ('{ issues(first: 100, filter: {labels: {name: {eq: "esteira"}}, state: {type: {eq: "backlog"}}}) '
             '{ nodes { id identifier title description priority createdAt project { name } labels { nodes { name } } '
             'team { states { nodes { id name } } labels { nodes { id name } } } } } }')
Q_ANDAMENTO = ('{ issues(first: 50, filter: {labels: {name: {eq: "esteira"}}, '
               'state: {type: {in: ["unstarted", "started"]}, name: {neq: "In Review"}}}) { nodes { identifier } } } # inProgress')
CATEGORIAS = ("seo-fix", "buraco", "pagina-nova", "conteudo", "melhoria")
# o dono, 15/10: clipe/reel automático "ficou um lixo completo". Issue com estas labels não sai do Backlog nem é criada.
DESLIGADAS = {"animacao"}


def desligada(labels):
    return bool(DESLIGADAS & {lb.lower() for lb in labels})


# (lição 8): clipe, reel, versão animada e fábrica de cortes só existem se o dono pediu com essas palavras na própria
# issue: label `pedido-fulano` + a fala citada. A mesma lista vale na criação (`linear.criar_issue`) e na esteira (`escolher`).
RE_MOTION = re.compile(r"\bclipes?\b(?!-)|\breels?\b|vers[ãa]o animada|pe[çc]a animada|v[ií]deo animado|carross(?:el|[eé]is) animad|"
                       r"f[áa]brica de (?:cortes|clipes|reels|motion)|motion design|anima[çc][ãa]o autom[áa]tica", re.I)
RE_FALA = re.compile(r"^\s*>\s*(.+)$", re.M)  # só citação em bloco (`>`); aspas soltas no texto não são fala do dono
PEDIDO = "pedido-fulano"


def sem_pedido(titulo, descricao, labels):
    """"" se pode criar/despachar; senão o motivo (pede clipe/reel/animação sem o pedido do dono citado)."""
    if not RE_MOTION.search(f"{titulo or ''}\n{descricao or ''}"):
        return ""
    if PEDIDO in {lb.lower() for lb in labels} and any(RE_MOTION.search(f) for f in RE_FALA.findall(descricao or "")):
        return ""  # a fala citada tem de ser o pedido (clipe, reel, animação...), não qualquer texto
    return (f"clipe, reel, versão animada ou fábrica de cortes está desligado pelo {config.dono()} (KRN-825): só com a label "
            f"`{PEDIDO}` e a fala dele citada na descrição (linha começando com `>`)")


# Faixa Codex: o que o Codex faz bem vai para ele; nunca VPS/Pedido, banco, terceiro (WhatsApp/e-mail) ou skill.
APTAS_CODEX = set(roteamento.config().get("codex_primeiro") or ("seo-fix", "conteudo", "melhoria", "testes", "lint", "tsc"))  # hub.json → roteamento
FORA_CODEX = {"vps", "pedido", "migracao", "migração", "banco", "whatsapp", "email", "skill", "produto", "plano", "grande"}
RE_FORA_CODEX = re.compile(r"pedido vps|hub\.raiz|hub\.propostas|migra[çc][ãa]o|migration|whatsapp|e-?mail|\bvps\b|"
                           r"skill `(?!superpowers:)", re.I)  # a linha-padrão "Execute com a skill superpowers:..." não conta
FATIA_CODEX = 2 / 3  # o dono, 15/10: fatia fixa das aptas para o Codex, mesmo com folga nas contas Claude


def _labels(i):
    return {lb["name"].lower() for lb in i["labels"]["nodes"]}


def apta_codex(i):
    ls = _labels(i)
    if "codex" in ls:
        return True
    return i.get("priority") == 3 and bool(APTAS_CODEX & ls) and not FORA_CODEX & ls and \
        not RE_FORA_CODEX.search(f"{i.get('title') or ''}\n{i.get('description') or ''}")


def _categoria(i):
    nomes = {lb["name"].lower() for lb in i["labels"]["nodes"]}
    return next((c for c in CATEGORIAS if c in nomes), "outro")


def _prio(i):
    return i.get("priority") or 5  # 0 = sem prioridade: vai por último


def escolher(issues, vagas, despachadas, agora, em_andamento=0, vagas_codex=0, rodizio=None):
    """As melhores do estoque: prioridade (1 urgente … 4 baixa), depois a mais antiga; teto por projeto no dia e teto
    de issues da esteira em andamento (só das contas Claude). Devolve [(issue, "claude"|"codex")]: a apta vai para o
    Codex quando as contas Claude não têm vaga, ou na fatia FATIA_CODEX (contagem em `rodizio`, que é atualizado)."""
    hoje = agora.date().isoformat()
    por_projeto = {}
    for x in despachadas:
        if x["em"][:10] == hoje:
            por_projeto[x["projeto"]] = por_projeto.get(x["projeto"], 0) + 1
    vagas = max(min(vagas, MAX_EM_ANDAMENTO - em_andamento), 0)
    rodizio = {"aptas": 0, "codex": 0} if rodizio is None else rodizio
    escolhidas = []
    for i in sorted(issues, key=lambda i: (_prio(i), i["createdAt"])):
        if not vagas and not vagas_codex:
            break
        if desligada(lb["name"] for lb in i["labels"]["nodes"]) or sem_pedido(
                i.get("title"), i.get("description"), (lb["name"] for lb in i["labels"]["nodes"])):
            continue
        p = (i.get("project") or {}).get("name", "?")
        if MAX_POR_PROJETO_DIA and por_projeto.get(p, 0) >= MAX_POR_PROJETO_DIA:
            continue
        executor = "claude"
        if "codex" in _labels(i):  # já rotulada: a ponte manda ao Codex, então só sai com vaga Codex
            if not vagas_codex:
                continue
            executor = "codex"
        elif vagas_codex and apta_codex(i):
            if not vagas:
                executor = "codex"
            else:
                rodizio["aptas"] += 1
                if rodizio["codex"] < FATIA_CODEX * rodizio["aptas"]:
                    rodizio["codex"] += 1
                    executor = "codex"
        if executor == "codex":
            vagas_codex -= 1
        elif vagas:
            vagas -= 1
        else:
            continue
        por_projeto[p] = por_projeto.get(p, 0) + 1
        escolhidas.append((i, executor))
    return escolhidas


def vagas_codex(base_dados=None):
    """Vagas livres da faixa Codex: hub.json → codex.vagas menos o que roda ou espera na fila; 0 sem login ou pausado."""
    from hub import agenda
    c = agenda.estado_codex_executor(base_dados)
    if not c["login_ok"] or c["pausa"]:
        return 0
    return max(c["vagas"] - len(c["rodando"]) - c["na_fila"], 0)


def rotular_codex(i, gql):
    """Põe a label `codex` na issue (antes de ir para Todo: a ponte lê a label ao puxar)."""
    if "codex" in _labels(i):
        return
    lid = next((lb["id"] for lb in i["team"]["labels"]["nodes"] if lb["name"].lower() == "codex"), None)
    if lid is None:
        raise RuntimeError("label codex não existe no time")
    gql("mutation($i: String!, $l: String!) { issueAddLabel(id: $i, labelId: $l) { success } }", i=i["id"], l=lid)


def _arq(base):
    return Path(base or data()) / "claude" / "esteira.json"


def estado(base_dados=None):
    try:
        return json.loads(_arq(base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"em": None, "folga": {}, "despachadas": [], "estoque": {"total": 0, "por_categoria": {}}}


def _gravar(base, est):
    arq = _arq(base)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(est, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, arq)


def despachar(base_dados=None, agora=None, gql=None, mover=None, livres_codex=None):
    """Uma rodada: mede a folga, escolhe e move para Todo. Devolve os identificadores despachados."""
    from hub import agenda, linear
    gql = gql or linear.gql
    mover = mover or linear.mover
    agora = agora or conta.agora()
    try:
        uso = json.loads((Path(base_dados or data()) / "claude" / "uso-contas.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        uso = {}
    # só o das contas Claude: a faixa Codex tem as vagas dela
    ocupadas = (sum(1 for x in agenda.fila(base_dados) if not agenda._eh_codex(x))
                + sum(1 for n in agenda.rodando_todas(base_dados) if not n.startswith("rodando-codex")))
    livres_codex = vagas_codex(base_dados) if livres_codex is None else livres_codex
    folgas = folga(uso, ocupadas)
    est = estado(base_dados)
    limite = conta.iso(agora - dt.timedelta(days=7))
    est["despachadas"] = [x for x in est.get("despachadas", []) if x["em"] >= limite]
    ja = {x["issue"] for x in est["despachadas"]}  # já despachadas: estacionadas, não voltam nem contam
    estoque = [i for i in gql(Q_ESTOQUE)["issues"]["nodes"] if i["identifier"] not in ja]
    em_andamento = len(gql(Q_ANDAMENTO)["issues"]["nodes"])
    feitas = []
    rodizio = est.setdefault("rodizio_codex", {"aptas": 0, "codex": 0})
    for i, executor in escolher(estoque, vagas(folgas, ocupadas), est["despachadas"], agora, em_andamento,
                                livres_codex, rodizio):
        ident = i["identifier"]
        todo = next((s["id"] for s in i["team"]["states"]["nodes"] if s["name"] == "Todo"), None)
        if todo is None:
            continue
        try:
            if executor == "codex":
                rotular_codex(i, gql)
            mover(i["id"], todo)
        except Exception as e:
            print(f"esteira: {ident} falhou: {e}", file=sys.stderr)
            continue
        feitas.append(ident)
        est["despachadas"].append({"issue": i["identifier"], "projeto": (i.get("project") or {}).get("name", "?"),
                                   "categoria": _categoria(i), "executor": executor, "em": conta.iso(agora)})
    por_cat = {}
    for i in estoque:
        if i["identifier"] not in feitas:
            por_cat[_categoria(i)] = por_cat.get(_categoria(i), 0) + 1
    est.update({"em": conta.iso(agora), "folga": folgas,
                "estoque": {"total": sum(por_cat.values()), "por_categoria": por_cat}})
    _gravar(base_dados, est)
    return feitas


def main(argv=None):
    cmd = (argv or sys.argv[1:] or ["estado"])[0]
    sys.stdout.reconfigure(encoding="utf-8")
    if cmd == "despachar":
        feitas = despachar()
        if feitas:
            print(f"{conta.iso(conta.agora())} esteira: despachadas {', '.join(feitas)}")
        return 0
    print(json.dumps(estado(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
