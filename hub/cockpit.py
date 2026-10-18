"""Dados do cockpit do painel (plano docs/plans/feitos/2026-10-09-cockpit.md): agentes ao vivo, fila, Linear por dentro e o
que depende do dono. Só leitura. Linear com cache de 60 s (o painel recarrega sozinho)."""
import contextlib
import datetime as dt
import json
import os
import re
import time
from pathlib import Path

from hub import agenda, conta, data, linear, propostas

_MEMO = {}


def _cache(chave, ttl, fn):
    agora = time.monotonic()
    if chave in _MEMO and agora - _MEMO[chave][0] < ttl:
        return _MEMO[chave][1]
    v = fn()
    _MEMO[chave] = (agora, v)
    return v


def _base(base):
    return Path(base or data())


def _json(p, padrao=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _issue(nome):
    m = re.fullmatch(r"linear-([a-z]+-\d+)", nome or "", re.I)
    return m.group(1).upper() if m else None


# palavra sensível no NOME; auth só como palavra (AUTH, X_AUTH, AUTH_TOKEN, authorization), para não pegar "author"
_NOME = (r"(?:[\w-]*(?:token|secret|pass|pwd|senha|key|credential)[\w-]*"
         r"|(?:[\w-]*[_-])?auth(?:orization)?(?:[_-][\w-]*)?)")
_VALOR = r"""(?!•)(?:'[^']*'|"[^"]*"|[^\s'"]+)"""
_SEGREDOS = [
    (re.compile(r"(\w[\w+.-]*://[^\s:/@'\"]+:)[^\s'\"]*@"), r"\1•••@"),  # senha na URL, até o último @
    (re.compile(r"""((?:\s-u|--user)(?:\s+|=)['"]?[^\s:'"]+:)[^\s'"]+"""), r"\1•••"),  # curl -u user:senha  # gitleaks:allow
    (re.compile(r"""(\b(?:mysql|mysqldump|mariadb)\b[^|;&]*?\s-p)(?!•)(?:'[^']*'|"[^"]*"|\S+)"""), r"\1•••"),  # mysql -pSENHA
    (re.compile(rf"(authorization\s*[:=]\s*)(?:(?:bearer|token|basic)\s+)?{_VALOR}", re.I), r"\1•••"),
    (re.compile(rf"(\bbearer\s+){_VALOR}", re.I), r"\1•••"),
    (re.compile(rf"""(--{_NOME}(?:=|\s+)|(['"]?)\b{_NOME}\2\s*[=:]\s*){_VALOR}""", re.I), r"\1•••"),
    (re.compile(r"\b(?:gh[pousr]_\w+|github_pat_\w+|sk-[\w-]{10,}|lin_api_\w+|xox[bap]-[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)"), "•••"),
]


def _sem_segredo(passo):
    """Passo do agente sem credencial (o painel mostra comandos crus: senha em URL, TOKEN=..., Bearer ...)."""
    passo = passo[:400]  # o regex de máscara é quadrático em linha longa com hífens
    for rx, troca in _SEGREDOS:
        passo = rx.sub(troca, passo)
    return passo


def agentes(base=None, agora=None):
    base, agora = _base(base), agora or dt.datetime.now(dt.timezone.utc)
    uso = (_json(base / "claude" / "uso-contas.json", {}) or {}).get("contas") or {}
    rodando = {str(r.get("conta", "1")): r for r in agenda.rodando_todas(base).values()}
    saida = []
    for n in conta.com_login():
        r, u = rodando.get(n) or {}, uso.get(n) or {}
        desde = r.get("inicio")
        minutos = None
        try:
            minutos = int((agora - dt.datetime.fromisoformat(desde.replace("Z", "+00:00"))).total_seconds() // 60)
        except (ValueError, TypeError, AttributeError):  # inicio ausente ou malformado: só essa conta fica sem minutos
            pass
        saida.append({"conta": n, "faixa": conta.faixa(n), "tarefa": r.get("nome"), "issue": _issue(r.get("nome")),
                      "titulo": r.get("titulo"),
                      "desde": desde, "minutos": minutos,
                      "ultimos_passos": [_sem_segredo(str(x)) for x in linear.passos_do_registro(r["log"], 5)] if r.get("log") else [],
                      "cinco_horas": (u.get("cinco_horas") or {}).get("pct"), "reset_5h": (u.get("cinco_horas") or {}).get("reset"),
                      "semana": (u.get("semana") or {}).get("pct")})
    return saida


def fila(base=None):
    return [{"nome": i["nome"], "issue": _issue(i["nome"]), "desde": i.get("enfileirada_em")}
            for i in agenda.fila(_base(base))]


Q_ABERTAS = ('{ issues(first: 60, orderBy: updatedAt, filter: {state: {type: {nin: ["completed", "canceled"]}}}) '
             '{ nodes { id identifier title url updatedAt state { name } project { name } labels { nodes { name } } } } }')

# inclui concluídas: o progresso do plano precisa delas
Q_PLANOS = ('{ issues(first: 250, filter: {labels: {some: {name: {startsWith: "plano-"}}}}) '
            '{ nodes { identifier state { name type } labels { nodes { name } } } } }')


def linear_resumo(agora=None, gql=None, base=None):
    gql, agora = gql or linear.gql, agora or dt.datetime.now(dt.timezone.utc)

    def ler():
        nos = gql(Q_ABERTAS)["issues"]["nodes"]
        issues = [{"id": i["id"], "identifier": i["identifier"], "titulo": i["title"], "estado": i["state"]["name"],
                   "projeto": (i.get("project") or {}).get("name", "—"), "url": i["url"],
                   "labels": [lb["name"] for lb in i["labels"]["nodes"]], "atualizada": i["updatedAt"]} for i in nos]
        por_estado, planos = dict.fromkeys(("Todo", "In Progress", "In Review", "Backlog"), 0), {}
        for i in issues:
            por_estado[i["estado"]] = por_estado.get(i["estado"], 0) + 1
        for i in gql(Q_PLANOS)["issues"]["nodes"]:
            if i["state"]["type"] == "canceled":
                continue
            for lb in (x["name"] for x in i["labels"]["nodes"]):
                if lb.startswith("plano-"):
                    p = planos.setdefault(lb, {"label": lb, "feitas": 0, "total": 0})
                    p["total"] += 1
                    p["feitas"] += i["state"]["type"] == "completed" or i["state"]["name"] == "In Review"
        est = _json(_base(base) / "claude" / "esteira.json", {}) or {}
        return {"por_estado": por_estado, "issues": issues, "planos": [p for p in planos.values() if p["feitas"] < p["total"]],
                "esteira": {"estoque": (est.get("estoque") or {}).get("total", 0),
                            "despachadas_7d": len(est.get("despachadas") or [])}}
    return _cache("linear_resumo", 60, ler)


# Backlog com comentário de falha do Claude (hub.linear.concluir comenta "Falhou (conta): ...")
Q_FALHAS = ('{ issues(first: 100, filter: {state: {name: {eq: "Backlog"}}}) '
            '{ nodes { identifier comments(first: 50) { nodes { body } } } } }')

Q_PERGUNTAS = ('{ agentSessions(first: 30, orderBy: updatedAt) { nodes { status issue { identifier title url } '
               'activities(first: 50) { nodes { createdAt content { __typename ... on AgentActivityElicitationContent '
               '{ body } } } } } } }')


def _numeros_do_bot():
    """{id da proposta: N} dos pedidos que o bot ainda espera ("faz N" só vale para esses); arquivo ruim → {}."""
    try:
        itens = _json(os.environ.get("PEDIDOS_BOT", "/opt/asite01-wpp/data/pedidos.json"), {}).get("itens") or {}
        return {i["plano"]["id"]: int(n) for n, i in itens.items()
                if i.get("estado") == "aguardando" and (i.get("plano") or {}).get("tipo") == "proposta"}
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}


# Operação (servidor, merge) é do orquestrador, que aprova sozinho na varredura: não entra na lista do dono (10/10).
DO_ORQUESTRADOR = ("vps", "pr_merge")


def pendencias(base=None, agora=None, gql=None):
    base, gql = _base(base), gql or linear.gql
    agora = agora or dt.datetime.now(dt.timezone.utc)
    numeros = _numeros_do_bot()
    pedidos = [{"n": numeros.get(p.get("id")), "id": p.get("id"), "resumo": p.get("resumo", ""), "tipo": p.get("tipo", ""),
                "criada_em": p.get("criada_em")}
               for e in ("pendente", "enviada") for p in propostas.listar(e, base)
               if p.get("tipo") not in DO_ORQUESTRADOR]

    def ler_linear():
        perguntas = []
        for s in gql(Q_PERGUNTAS, app=True)["agentSessions"]["nodes"]:
            if s.get("status") != "awaitingInput" or not s.get("issue"):
                continue
            el = [a for a in s["activities"]["nodes"] if a["content"]["__typename"] == "AgentActivityElicitationContent"]
            if el:
                ult = max(el, key=lambda a: a["createdAt"])
                perguntas.append({"identifier": s["issue"]["identifier"], "titulo": s["issue"]["title"],
                                  "url": s["issue"]["url"], "pergunta": ult["content"].get("body", "")})
        return perguntas
    try:
        perguntas = _cache("perguntas", 60, ler_linear)
    except Exception:  # noqa: BLE001 — sem Linear, o painel mostra o resto
        perguntas = []
    travadas = []

    def _com_espera_viva(agora_, base_):  # espera de CI/deploy tem prazo próprio (esperas.TETO_*): não é travada
        from hub import esperas
        vivas = set()
        with contextlib.suppress(Exception):
            for e in esperas.todas(base_):
                if agora_ - dt.datetime.fromisoformat(e["criada_em"].replace("Z", "+00:00")) < dt.timedelta(
                        hours=float(e.get("max_horas") or esperas.MAX_HORAS)):
                    vivas.add(e["ident"])
        return vivas

    def _ocupadas(base_):  # issue com execução rodando ou na fila não está parada (a varredura de hub.travadas pula igual)
        with contextlib.suppress(Exception):
            return ({i["nome"] for i in agenda.fila(base_)} | {r["nome"] for r in agenda.rodando_todas(base_).values()})
        return set()
    try:
        falhas = _cache("falhas", 60, lambda: {i["identifier"] for i in gql(Q_FALHAS)["issues"]["nodes"]
                                               if any("Falhou (" in (c.get("body") or "") for c in i["comments"]["nodes"])})
        for i in linear_resumo(agora, gql, base)["issues"]:
            motivo = None
            if i["estado"] == "Backlog" and i["identifier"] in falhas:
                motivo = "falhou e voltou para o Backlog"
            elif i["estado"] == "In Progress" and agora - dt.datetime.fromisoformat(
                    i["atualizada"].replace("Z", "+00:00")) > dt.timedelta(hours=3) \
                    and i["identifier"] not in _com_espera_viva(agora, base) \
                    and f"linear-{i['identifier'].lower()}" not in _ocupadas(base):  # 3 h (o vigia chama o orquestrador)
                motivo = "parada há mais de 3 h em andamento"
            if motivo:
                travadas.append({"identifier": i["identifier"], "titulo": i["titulo"], "url": i["url"], "motivo": motivo})
    except Exception:  # noqa: BLE001
        pass
    return {"pedidos": sorted(pedidos, key=lambda p: p["criada_em"] or ""), "travadas": travadas, "perguntas": perguntas}
