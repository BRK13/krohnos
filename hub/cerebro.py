"""Cérebro: o grafo da operação e os eventos reais que passam por ele. Só leitura.

    python3 -m hub.cerebro json [--horas 24]                  # {"agora", "nos", "arestas", "eventos"} no stdout
    python3 -m hub.cerebro prototipo saida.html [--horas 24]  # HTML de 1 tela com os dados de agora embutidos

No painel: /cerebro (tela inteira, ao vivo), /cerebro?compacto=1 (o quadro da tela inicial) e /cerebro.json (polling de 5 s).

O desenho nasce da config de quem instalou ([agentes] contas, [cerebro] no krohnos.toml) e do cadastro de sites,
nunca de constantes: sem config, Você → Orquestrador → Fila → Conta 1 e "configure seus sites" (`topologia`).

Cada evento é algo que o hub já gravou (nada inventado; sem evento, a aresta fica parada):
uso.jsonl (conta pegou / terminou tarefa), propostas/, avisos-enviados.jsonl, incidentes.jsonl, vigia-*/ (vigias de plugin),
vps/resultados (ação do root), esperas/ (commit no ar), saude.json (deploy do Dokploy) e o jsonl de `cerebro.codex_revisoes`;
do GitHub (gh): PR aberto, commit, local-ci e merge; do Linear: issue criada.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import os
import math
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

from hub import RAIZ, config, conta, data, gh, sites as cadastro

UTC = dt.timezone.utc
CODEX_SESSOES = Path.home() / ".codex" / "sessions"
RE_ISSUE = re.compile(r"KRN-?(\d+)", re.I)
BRT = dt.timezone(dt.timedelta(hours=-3))
# a cor de cada modelo vale na legenda, no anel do nó da conta e nos pulsos das tarefas dele
MODELOS = (("opus", "Opus", "#A88BA0"), ("sonnet", "Sonnet", "#D3B06C"), ("haiku", "Haiku", "#7FA6C4"),
           ("codex", "Codex", "#74B3A6"))
SEM_SITES = "_sites"  # o lugar dos sites numa instalação sem cadastro: "configure seus sites" (id que nenhum site pode ter)
FIXOS = {"voce", "orch", "fila", "codex", "ci", "deploy", "debugger", "pc", SEM_SITES}


# ---------- o desenho: config e cadastro de quem instalou ----------

def _norm(texto):
    """"Loja Exemplo Ltda." -> "lojaexemploltda": casa projeto do Dokploy com o nome do site sem tabela fixa."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def _sites_cadastro(base=None):
    """Sites ativos do cadastro. O padrão neutro exemplos/projetos (instalação sem cadastro) só vale com o `krohnos demo`
    ligado; os sites criados pelo MCP valem sempre. Cadastro vazio ou com erro: nenhum site (o mapa pede o cadastro)."""
    try:
        lista = cadastro.ativos()
    except cadastro.SiteInvalido:
        return []
    try:
        exemplo = Path(cadastro.PASTA).resolve() == (RAIZ / "exemplos" / "projetos").resolve()
    except OSError:
        exemplo = False
    if exemplo:
        from hub import demo
        if not demo.ativa(base or data()):
            lista = [s for s in lista if cadastro.origem(s) == "mcp"]
    return lista


def topologia(base=None):
    """O que existe no mapa, lido agora da config ([agentes], [cerebro]) e do cadastro de sites. Nada aqui é fixo no código:
    sem config, 1 conta e nenhum site; cada nó de infraestrutura só existe com a chave dele."""
    c = lambda k: config.get(f"cerebro.{k}")  # noqa: E731
    faixas = {str(k): str(v) for k, v in (config.get("agentes.faixas") or {}).items()}
    brutas = [str(x) for x in (config.get("agentes.contas") or ["1"])]
    contas = tuple(x for x in brutas if x != "codex") or ("1",)
    codex = bool(c("codex")) or "codex" in brutas or "codex" in faixas.values()
    cad = {s["id"]: s for s in _sites_cadastro(base)}
    rotulos = c("sites") or {}
    # [cerebro.sites] é a lista fechada do desenho (só os ids dela, na ordem dela); sem ela, todos os ativos do cadastro
    ordem = [i for i in rotulos if i in cad] if rotulos else list(cad)
    sites = {i: str(rotulos.get(i) or cad[i].get("nome") or i) for i in ordem}
    servidor = str(c("servidor") or "").strip()
    if servidor in FIXOS or servidor in sites or servidor.startswith("c") and servidor[1:] in contas:
        print(f"cerebro: servidor {servidor!r} ignorado (id já usado por outro nó)", file=sys.stderr)
        servidor = ""
    gpu = str(c("gpu_estacao") or "").strip()
    t = {"contas": contas, "codex": codex, "ci": bool(c("ci")), "debugger": bool(c("debugger")),
         "sites": sites, "cadastro": cad, "servidor": servidor, "servidor_rotulo": c("servidor_rotulo") or "Servidor",
         "pc": Path(gpu).expanduser() if gpu else None, "pc_rotulo": c("pc_rotulo") or "PC · GPU",
         "codex_revisoes": Path(str(c("codex_revisoes"))).expanduser() if c("codex_revisoes") else None,
         "versao": {str(x) for x in (c("versao") or [])}}
    t["deploy"] = bool(sites or servidor)
    t["servidor_deploys"] = c("servidor_deploys") if servidor and c("servidor_deploys") in sites else ""
    ids = {n[0] for n in layout(t)}
    # repo → nó e projeto do Dokploy → nó: do cadastro (repo.github, nome e marca do site); a config só soma o que o cadastro
    # não diz (o repo do próprio hub, um projeto com outro nome) e só para nó que existe
    repo_no = {str((s.get("repo") or {}).get("github") or "").split("/")[-1]: i for i, s in cad.items() if i in sites}
    repo_no.pop("", None)
    repo_no.update({str(k): str(v) for k, v in (c("repos") or {}).items() if str(v) in ids})
    projeto_no = {}
    for i, s in cad.items():
        if i in sites:
            m = s.get("marca") or {}
            for nome in filter(None, map(_norm, (i, s.get("nome"), m.get("nome"), *(m.get("aliases") or [])))):
                projeto_no.setdefault(nome, i)
    projeto_no.update({_norm(k): str(v) for k, v in (c("projetos") or {}).items() if str(v) in ids})
    t["repo_no"], t["projeto_no"] = repo_no, projeto_no
    return t


def _espalhar(n, ini, fim, meio=.42):
    """y de n nós numa coluna: um só fica no meio; vários se distribuem entre ini e fim."""
    return [meio] if n == 1 else [ini + i * (fim - ini) / (n - 1) for i in range(n)]


def layout(t):
    """[(id, rótulo, grupo, x, y)] no desktop (fluxo da esquerda para a direita; no celular x e y trocam). y distribuído
    pelo número real de nós de cada coluna."""
    contas = [(f"c{n}", f"Conta {n}") for n in t["contas"]]
    saida = [("voce", "Você", "voce", .05, .42), ("orch", "Orquestrador", "orch", .17, .42), ("fila", "Fila · Linear", "fila", .30, .42)]
    saida += [(i, rot, "conta", .45, y) for (i, rot), y in zip(contas, _espalhar(len(contas), .12, .72))]
    meio = [(i, rot, g) for i, rot, g, liga in (("codex", "Codex", "codex", t["codex"]), ("ci", "CI · local-ci", "ci", t["ci"])) if liga]
    saida += [(i, rot, g, .60, y) for (i, rot, g), y in zip(meio, _espalhar(len(meio), .27, .57))]
    if t["deploy"]:
        saida.append(("deploy", "Deploy", "deploy", .74, .42))
    if t["debugger"]:
        saida.append(("debugger", "Debugger", "debugger", .38, .67))
    sites = list(t["sites"].items()) or [(SEM_SITES, "Configure seus sites")]
    grupo = "site" if t["sites"] else "vazio"
    saida += [(i, rot, grupo, .92, y) for (i, rot), y in zip(sites, _espalhar(len(sites), .07, .77))]
    infra = ([("pc", t["pc_rotulo"])] if t["pc"] else []) + ([(t["servidor"], t["servidor_rotulo"])] if t["servidor"] else [])
    saida += [(i, rot, "infra", x, .93) for (i, rot), x in zip(infra, _espalhar(len(infra), .14, .40, meio=.27))]
    return saida


def arestas_fixas(t):
    """As ligações do fluxo, só entre nós que existem."""
    contas, srv = [f"c{n}" for n in t["contas"]], t["servidor"]
    a = [("voce", "orch"), ("voce", "fila"), ("orch", "fila"), *[("fila", c) for c in contas]]
    if t["codex"]:
        a += [(c, "codex") for c in contas]
    if t["ci"]:
        a += [(c, "ci") for c in contas]
    a += [(c, "orch") for c in contas]
    if t["codex"] and t["ci"]:
        a.append(("codex", "ci"))
    if t["ci"] and t["debugger"]:
        a.append(("ci", "debugger"))
    if t["debugger"]:
        a.append(("debugger", "orch"))
    if t["deploy"]:
        a += [("ci", "deploy")] if t["ci"] else [(c, "deploy") for c in contas + (["codex"] if t["codex"] else [])]
        a += [("deploy", s) for s in t["sites"]] + ([("deploy", srv)] if srv else [])
    # onde cada parte roda: orquestrador, contas, Codex, CI, deploy e o site que sobe direto no servidor
    if srv:
        a.append(("orch", srv))
    if t["pc"]:
        a.append(("orch", "pc"))
    if srv:
        a += [(n, srv) for n in (*contas, *(["codex"] if t["codex"] else []), *(["ci"] if t["ci"] else []),
                                 *(["deploy"] if t["deploy"] else []), *([t["servidor_deploys"]] if t["servidor_deploys"] else []))]
    return a


def _hosts(s, t=None):
    """Domínio do site e os alternativos do cadastro (www. etc.)."""
    c = (t or {}).get("cadastro", {}).get(s)
    if c is None:
        try:
            c = cadastro.site(s)
        except cadastro.SiteInvalido:
            return set()
    return {h for h in [c.get("dominio"), *(c.get("hosts_alternativos") or [])] if h}


def arestas(remotos, t=None):
    """Ligações do fluxo + site → servidor que o atende (monitor/remotos.json, campo "sites"). Site sem leitura no servidor
    fica sem aresta de máquina: melhor sem linha que linha para o servidor errado."""
    t = t or topologia()
    saida = arestas_fixas(t)
    srv = t["servidor"]
    for s in t["sites"] if srv else ():
        if _hosts(s, t) & set((remotos.get(srv) or {}).get("sites") or []) and (s, srv) not in saida:
            saida.append((s, srv))
    return saida


def _ler(p, padrao=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _linhas(p):
    try:
        with open(p, encoding="utf-8") as f:
            for linha in f:
                try:
                    yield json.loads(linha)
                except ValueError:
                    continue
    except OSError:
        return


def _iso(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return None


def _ev(t, tipo, de, para, rot, url="", modelo=None):
    # tudo em UTC: o corte por data (montar, ao_vivo e o navegador) compara texto
    e = {"t": t.astimezone(UTC).isoformat(timespec="seconds"), "tipo": tipo, "de": de, "para": para, "rot": str(rot)[:140], "url": url}
    if modelo:
        e["modelo"] = modelo
    return e


def _modelo(m):
    """opus | sonnet | haiku | codex (nome de modelo qualquer, ex. claude-sonnet-5-5) ou None."""
    m = str(m or "").lower()
    return next((k for k, _, _ in MODELOS if k in m), None)


def _issue_url(ident):
    ws = str(config.get("linear.workspace") or "").strip()
    return f"https://linear.app/{ws}/issue/{ident}" if ident and ws else ""


def _ident(nome):
    m = RE_ISSUE.search(nome or "")
    return f"KRN-{m.group(1)}" if m else None


# ---------- eventos do disco ----------

def eventos_disco(base, desde, codex=None, t=None):
    """Eventos gravados pelo hub a partir de `desde`. Devolve (eventos, {KRN-n: conta}) — o 2º liga PR à conta.
    codex: o jsonl das revisões (padrão: `cerebro.codex_revisoes`; sem ele, sem leitura)."""
    base, ev, quem = Path(base), [], {}
    top = t or topologia(base)
    hub_no = top["servidor"] or "orch"  # o que é do servidor do hub: sem servidor no desenho, fica no orquestrador
    codex = codex or top["codex_revisoes"]
    for r in _linhas(base / "claude" / "uso.jsonl"):
        c, ini, fim = str(r.get("conta")), _iso(r.get("inicio")), _iso(r.get("fim"))
        if c not in top["contas"] or not ini:
            continue
        ident = _ident(r.get("nome"))
        if ident:
            quem[ident] = f"c{c}"
        if ini >= desde:
            ev.append(_ev(ini, "pegou", "fila", f"c{c}", f"Conta {c} pegou {r.get('nome')} ({r.get('modelo') or '?'})",
                          _issue_url(ident), _modelo(r.get("modelo"))))
        if fim and fim >= desde:
            if r.get("ok"):
                ev.append(_ev(fim, "fim", f"c{c}", f"c{c}", f"Conta {c} terminou {r.get('nome')} em {int(r.get('duracao_s') or 0) // 60} min",
                              modelo=_modelo(r.get("modelo"))))
            else:
                ev.append(_ev(fim, "erro", f"c{c}", f"c{c}", f"Conta {c}: {r.get('nome')} falhou ({str(r.get('erro') or 'erro')[:60]})",
                              _issue_url(ident), _modelo(r.get("modelo"))))
    for p in (base / "propostas").glob("*.json"):
        d = _ler(p, {}) or {}
        t, conta_ = _iso(d.get("criada_em")), quem.get(_ident(d.get("tarefa")), "orch")
        if t and t >= desde:
            ev.append(_ev(t, "proposta", conta_, "orch", f"Proposta {d.get('tipo')}: {d.get('resumo', '')}"))
        for h in d.get("historico") or []:
            th = _iso(h.get("em"))
            if th and th >= desde and h.get("estado") in ("feita", "falhou"):
                alvo = "voce" if d.get("tipo") in ("whatsapp", "email", "post_gmb", "resposta_avaliacao") else hub_no
                ev.append(_ev(th, "pedido" if h["estado"] == "feita" else "erro", "orch", alvo,
                              f"Proposta {h['estado']}: {d.get('resumo', '')}"))
    for r in _linhas(base / "claude" / "avisos-enviados.jsonl"):
        t = _iso(r.get("em"))
        if t and t >= desde:
            ev.append(_ev(t, "aviso", "orch", "voce", "Aviso no WhatsApp: " + str(r.get("texto", "")).split("\n")[0]))
    for r in _linhas(base / "claude" / "respostas.jsonl"):  # só o tempo de resposta, sem o texto da conversa
        t = _iso(r.get("em"))
        if t and t >= desde:
            ev.append(_ev(t - dt.timedelta(seconds=float(r.get("segundos") or 0)), "msg", "voce", "orch", "Mensagem sua no WhatsApp"))
            ev.append(_ev(t, "resposta", "orch", "voce", f"Resposta do orquestrador em {float(r.get('segundos') or 0):.0f} s"))
    for r in _linhas(base / "claude" / "incidentes.jsonl"):
        t = _iso(r.get("em"))
        if t and t >= desde:
            ev.append(_ev(t, "alerta", "orch", "orch", f"{r.get('id')}: {r.get('falha', '')}", _issue_url(r.get("issue"))))
    for p in base.glob("vigia-*/*.json"):  # o que os vigias de plugin gravam (estado != ok vira alerta)
        d = _ler(p, {}) or {}
        t = _iso(d.get("criado_em"))
        if t and t >= desde and d.get("estado") != "ok":
            ev.append(_ev(t, "alerta", hub_no, "orch", f"Vigia: {d.get('rotulo')} ({d.get('estado')})"))
    for p in (base / "vps" / "resultados").glob("*.json"):
        d = _ler(p, {}) or {}
        t = _iso(d.get("em"))
        if t and t >= desde:
            ev.append(_ev(t, "pedido" if d.get("ok") else "erro", "orch", hub_no, f"Root: {d.get('acao')} {d.get('alvo') or ''} ({d.get('por') or ''})"))
    for p in (base / "claude" / "esperas").glob("*.json"):
        d = _ler(p, {}) or {}
        t, no = _iso(d.get("conferida_em")), top["repo_no"].get(str(d.get("repo", "")).split("/")[-1])
        if d.get("no_ar") and t and t >= desde and no:
            ev.append(_ev(t, "deploy", "deploy", no, f"No ar: {d.get('repo')} {str(d.get('sha', ''))[:8]} ({d.get('ident')})", d.get("pr", "")))
    for d in (_ler(base / "vps" / "saude.json", {}) or {}).get("deploys") or []:
        t, no = _iso(d.get("em")), top["projeto_no"].get(_norm(d.get("projeto")))
        if t and t >= desde and no:
            ok = d.get("status") == "done"
            ev.append(_ev(t, "deploy" if ok else "erro", "deploy", no, f"Deploy {d.get('projeto')}/{d.get('app')}: {d.get('titulo', '').splitlines()[0] if d.get('titulo') else d.get('status')}"))
    alvo = top["servidor_deploys"]  # o site que sobe direto no servidor (fora do Dokploy): deploys no remotos.json
    for d in ((_ler(base / "monitor" / "remotos.json", {}) or {}).get(top["servidor"]) or {}).get("deploys") or [] if alvo else []:
        t = _iso(d.get("em"))
        if t and t >= desde:
            ev.append(_ev(t, "deploy", "deploy", alvo, f"No ar: {dominio(alvo, top) or alvo} {d.get('sha')}"))
    for r in _linhas(codex) if codex else ():
        t = _iso(r.get("em"))
        if t and t >= desde:
            ev.append(_ev(t, "codex_achado" if r.get("achados") else "codex", "codex", "ci",
                          f"Revisão do Codex em {r.get('repo')}#{r.get('pr')}: {'com achados' if r.get('achados') else 'sem achados sérios'}",
                          r.get("url", ""), "codex"))
    return ev, quem


# ---------- GitHub e Linear ----------

Q_PRS = """query($q: String!, $depois: String) { search(query: $q, type: ISSUE, first: 100, after: $depois) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number title url createdAt mergedAt headRefName repository { name }
    commits(last: 30) { nodes { commit { oid committedDate status { contexts { context state createdAt } } } } } } } } }"""


def _gh_graphql(query, **v):
    args = ["api", "graphql", "-f", f"query={query}"] + [x for k, val in v.items() if val is not None
                                                               for x in ("-f", f"{k}={val}")]
    return json.loads(gh.executar(args, "cerebro", capture_output=True, text=True, timeout=90, check=True).stdout)["data"]


def prs(desde, gql=_gh_graphql):
    saida = []
    org = config.get("github.org")
    consultas = {f"org:{org}"} if org else set()
    repos = [(s.get("repo") or {}).get("github", "") for s in cadastro.ativos()]
    consultas.update(f"repo:{r}" for r in repos if r and r.partition("/")[0] != org)
    for dono in sorted(consultas):
        depois = None
        for _ in range(10):
            r = gql(Q_PRS, q=f"{dono} is:pr updated:>={desde.strftime('%Y-%m-%dT%H:%M:%SZ')}", depois=depois)["search"]
            saida += [n for n in r["nodes"] if n]
            if not r["pageInfo"]["hasNextPage"]:
                break
            depois = r["pageInfo"]["endCursor"]
    return saida


def eventos_github(lista, desde, quem):
    ev = []
    for p in lista:
        repo, url = p["repository"]["name"], p["url"]
        rot = f"{repo}#{p['number']} {p['title']}"
        conta_ = quem.get(_ident(p["title"]) or _ident(p.get("headRefName")))
        t = _iso(p.get("createdAt"))
        if t and t >= desde:
            ev.append(_ev(t, "pr", conta_, "codex", "PR aberto: " + rot, url))
        for c in (x["commit"] for x in p["commits"]["nodes"]):
            t = _iso(c.get("committedDate"))
            if t and t >= desde:
                ev.append(_ev(t, "commit", conta_, "ci", f"Commit {c['oid'][:8]} em {rot}", url))
            for s in ((c.get("status") or {}).get("contexts") or []):
                ts = _iso(s.get("createdAt"))
                if s.get("context") == "local-ci" and ts and ts >= desde and s.get("state") in ("SUCCESS", "FAILURE", "ERROR"):
                    ok = s["state"] == "SUCCESS"
                    ev.append(_ev(ts, "ci_ok" if ok else "ci_falha", "ci", conta_, f"local-ci {'verde' if ok else 'vermelho'} em {rot}", url))
        t = _iso(p.get("mergedAt"))
        if t and t >= desde:
            ev.append(_ev(t, "merge", "ci", "deploy", "Merge: " + rot, url))
    return ev


Q_ISSUES = ('query($desde: DateTimeOrDuration!) { issues(first: 250, filter: {createdAt: {gte: $desde}}) '
            '{ nodes { identifier title url createdAt creator { name } } } }')


def eventos_linear(desde, gql=None):
    if gql is None:
        from hub import linear
        gql = linear.gql
    ev = []
    for i in gql(Q_ISSUES, desde=desde.isoformat())["issues"]["nodes"]:
        t = _iso(i["createdAt"])
        de = "orch" if (i.get("creator") or {}).get("name") == "Claude" else "voce"  # o que você pede no WhatsApp o orch cria
        ev.append(_ev(t, "issue", de, "fila", f"{i['identifier']} criada: {i['title']}", i["url"]))
    return ev


# ---------- estado agora (nós) ----------

def _http(url, timeout=8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "krohnos-cerebro"}), timeout=timeout) as r:
            return r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (OSError, ValueError):
        return None, ""


def dominio(s, t=None):
    """Domínio do site `s` no cadastro (projetos/*.json; já lido em `t`, a topologia); "" se o site não está lá."""
    if t and s in t["cadastro"]:
        return t["cadastro"][s].get("dominio") or ""
    try:
        return cadastro.site(s).get("dominio") or ""
    except cadastro.SiteInvalido:
        return ""


def sites_agora(http=_http, t=None):
    t = t or topologia()

    def um(s):
        d = dominio(s, t)
        if not d:
            return s, {"status": None, "versao": ""}
        st, _ = http(f"https://{d}/")
        _, v = http(f"https://{d}/api/versao") if s in t["versao"] else (None, "")
        try:
            v = json.loads(v)
            v = str(v.get("commit") or v.get("gitSha") or "")[:8]
        except ValueError:
            v = ""
        return s, {"status": st, "versao": v}
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        return dict(ex.map(um, t["sites"]))


def _estourou(base, agora, n, uso):
    """Hora (Brasília) em que a conta volta, se estourou: pausa ativa ou 5 h ≥ 95% com reset no futuro (a mesma régua de
    agenda.oauth_esgotadas). Senão, ""."""
    pausa = conta.pausa_ativa(base, agora, n=n)
    fim = pausa and pausa.get("ate")
    if not fim:
        c = (uso.get(n) or {}).get("cinco_horas") or {}
        try:
            fim = c["reset"] if float(c["pct"]) >= 95 and conta.de_iso(c["reset"]) > agora else None
        except (KeyError, TypeError, ValueError, AttributeError):
            fim = None
    if not fim:
        return ""
    try:
        fim = conta.de_iso(fim)
        formato = "%d/%m %H:%M" if fim - agora >= dt.timedelta(hours=24) else "%H:%M"
        return (fim - dt.timedelta(hours=3)).strftime(formato)
    except (TypeError, ValueError, AttributeError):
        return "?"


def _pid_vivo(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
def _pct(valor):
    try:
        n = float(valor)
        return max(0, min(100, n)) if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def nos(base, agora, sites=None, t=None):
    base = Path(base)
    top = t or topologia(base)
    srv = top["servidor"]
    rodando = {}
    for r in (_ler(p, {}) for p in sorted((base / "claude").glob("rodando*.json"))):
        if r and _pid_vivo(r.get("pid")):
            rodando.setdefault(str(r.get("conta")), []).append(r)
    uso = (_ler(base / "claude" / "uso-contas.json", {}) or {}).get("contas") or {}
    modelo = {}
    for r in _linhas(base / "claude" / "uso.jsonl"):
        modelo[str(r.get("conta"))] = r.get("modelo")
    fila = sorted(p.stem for p in (base / "claude" / "fila").glob("*.json"))
    vermelho = [p.stem for p in (base / "claude" / "main-vermelho").glob("*.json")]
    incid = [r for r in _linhas(base / "claude" / "incidentes.jsonl") if r.get("estado") == "pendente"]
    mon = (_ler(base / "monitor" / "atual.json", {}) or {}).get("ponto") or {}
    saude = _ler(base / "vps" / "saude.json", {}) or {}
    orch = (_ler(base / "orch" / "estado.json", {}) or {}).get("varredura") or {}
    sites = sites or {}
    st = {}
    for n in top["contas"]:
        rs, u = rodando.get(n) or [], ((uso.get(n) or {}).get("cinco_horas") or {}).get("pct")
        lin, maior = [], 0
        for r in rs:
            ini = _iso(r.get("inicio"))
            minutos = int((agora - ini).total_seconds() // 60) if ini else 0
            maior = max(maior, minutos)
            lin.append(f"{_ident(r.get('nome')) or r.get('nome')} · {minutos} min")
        lin = lin or ["Parada agora"]
        lin.append(f"Modelo {modelo.get(n) or '?'} · 5 h: {f'{u:.0f}%' if u is not None else 'sem leitura'}")
        volta = _estourou(base, agora, n, uso)
        if volta:
            lin.insert(0, f"Limite estourado · volta {volta}")
        r0 = rs[0] if rs else {}
        mod = _modelo(((r0.get("linear") or {}).get("modelo")) or modelo.get(n))
        recentes = [r for r in _uso_lista(base) if str(r.get("conta")) == n and r["_fim"] and r["_fim"] >= agora - dt.timedelta(hours=24)
                    and not r.get("interrompida")]
        st[f"c{n}"] = {"modelo": mod, "tarefa": _ident(r0.get("nome")) or r0.get("nome") or "",
                       "titulo": str((r0.get("linear") or {}).get("titulo") or ""), "minutos": maior if rs else None,
                       "ok24": sum(1 for r in recentes if r.get("ok")), "falha24": sum(1 for r in recentes if not r.get("ok")),
                       "carga": min(1, (u or 0) / 100), "alerta": maior > 360, "linhas": lin, "ativo": len(rs),
                       "cota": _pct(u), "cota_pct": _pct(u),
                       "estourou": volta, "url": _issue_url(_ident(rs[0].get("nome"))) if rs else ""}
    st["fila"] = {"carga": min(1, len(fila) / 12), "na_fila": len(fila), "linhas": [f"{len(fila)} na fila"] + fila[:5]}
    st["orch"] = {"carga": .5 if orch.get("rodando") else .15, "alerta": bool(incid),
                 "linhas": [f"{len(incid)} incidente(s) pendente(s)" if incid else "Sem incidente pendente",
                            "Varredura rodando" if orch.get("rodando") else f"Última varredura {str((orch.get('ultima') or {}).get('fim', ''))[11:16]} UTC"]}
    if top["debugger"]:
        from hub import debugger
        dbg = debugger.painel(base, agora)
        ativos = sum(r.get("nome", "").startswith("debugger-") for rs in rodando.values() for r in rs)
        abertos = dbg['abertos']
        velhos = any(i['tipo'] == 'ci' and (agora - conta.de_iso(i['desde'])).total_seconds() > 3600 for i in abertos)
        linhas = [f"{ativos} consertando", f"{dbg['ci_aberto']} CI vermelho aberto agora",
                  f"{dbg['ci_resolvido_24h']} vermelhos resolvidos em 24 h",
                  f"Média vermelho → verde: {dbg['ci_media_min']} min" if dbg['ci_media_min'] is not None else "Média vermelho → verde: sem amostra",
                  f"Média alerta → tratado: {dbg['alerta_media_min']} min" if dbg['alerta_media_min'] is not None else "Média alerta → tratado: sem amostra",
                  "Meta: PR vermelho < 1 h; alerta sem tratamento < 30 min"]
        linhas += [f"{i['repo']} · {i['alvo']} · {int((agora - conta.de_iso(i['desde'])).total_seconds() // 60)} min · {i['erro']}" for i in abertos]
        st['debugger'] = {"ativo": ativos, "alerta": velhos, "linhas": linhas, "medidas": {k: v for k, v in dbg.items() if k != 'abertos'}}
    if top["ci"]:
        st["ci"] = {"alerta": bool(vermelho), "linhas": ["Main vermelho: " + ", ".join(vermelho)] if vermelho else ["Main verde"]}
    sal = (_ler(base / "saldos" / "estado.json", {}) or {}).get("servicos") or {}
    sal_linhas = [f"Saldo {k}: " + (f"US$ {v['saldo']:.2f} " if v.get("saldo") is not None else "") + v.get("estado", "?")
                  for k, v in sal.items() if k != "openai" or v.get("estado") != "indisponivel"]
    rem = _ler(base / "monitor" / "remotos.json", {}) or {}
    for s in [srv] if srv else []:
        r = rem.get(s) or {}
        medidas = [f"{rot} {r[campo]:g}%" for campo, rot in
                   (("cpu", "CPU"), ("mem_pct", "RAM"), ("disco_pct", "disco")) if r.get(campo) is not None]
        t = _iso(r.get("t"))
        idade = int((agora - t).total_seconds() // 60) if t else None
        if medidas and idade is not None:
            medidas.append(f"há {max(0, idade)} min")
        servidos = [rot for x, rot in top["sites"].items() if _hosts(x, top) & set(r.get("sites") or [])]
        # leitura velha (mais de 15 min) ou servidor sem resposta: alerta; sem arquivo nenhum não inventa alerta
        st[s] = {"papel": "servidor", "carga": min(1, (r.get("cpu") or 0) / 100), "alerta": bool(r.get("erro")) or (idade is not None and idade > 15),
                 "linhas": [" · ".join(medidas) if medidas else "sem leitura",
                            *([f"Sem resposta: {r['erro']}"] if r.get("erro") else []),
                            *([f"Sites: {', '.join(servidos)}"] if servidos else [])]}
    # o hub roda no servidor: saúde da máquina (saude.json), saldos e a CPU do monitor local vão para o nó dele
    if srv:
        h = st[srv]
        h["alerta"] = h["alerta"] or any("fora do ar" in p for p in saude.get("problemas") or []) or any(v.get("estado") == "zerado" for v in sal.values())
        h["linhas"] += [*sal_linhas, *(saude.get("problemas") or [])[:3]]
        if mon.get("cpu") is not None and not (rem.get(srv) or {}).get("cpu"):
            h["carga"] = min(1, mon["cpu"] / 100)
    # todos sem resposta na mesma rodada é a leitura do painel que falhou (rede do processo), não os 6 sites fora
    lido = bool(sites) and any((sites.get(s) or {}).get("status") is not None for s in top["sites"])
    for s in top["sites"]:
        i = sites.get(s) or {}
        fora = lido and (i.get("status") is None or i.get("status") >= 500)
        estado = (("fora do ar" if fora else f"HTTP {i.get('status')}") if lido else
                  "sem leitura (painel)" if sites else "sem leitura")
        idade = _iso(i.get("em"))
        idade = int((agora - idade).total_seconds() // 60) if idade else 0
        dom = dominio(s, top)
        st[s] = {"alerta": fora, "url": f"https://{dom}" if dom else "",
                 "linhas": [dom or s, estado + (f" · leitura de {idade} min atrás" if lido and idade >= 5 else ""),
                            *([f"commit no ar {i['versao']}"] if i.get("versao") else [])]}
    if not top["sites"]:
        st[SEM_SITES] = {"linhas": ["Nenhum site cadastrado", "Cadastre em projetos/ (ou rode krohnos demo para ver o exemplo)"]}
    if top["codex"]:
        rc = rodando.get("codex") or []
        semanal = ((_ler(base / "orch" / "estado.json", {}) or {}).get("codex") or {}).get("semana") or {}
        from hub.orq_estado import limite_codex
        try:
            semanal = limite_codex(list(CODEX_SESSOES.rglob("*.jsonl"))) or semanal
        except (OSError, ValueError):
            pass
        pct = _pct(semanal.get("pct"))
        volta_codex = ""
        if pct is not None and pct >= 100:
            reset = _iso(semanal.get("reset"))
            if reset and reset > agora:
                volta_codex = reset.astimezone(dt.timezone(dt.timedelta(hours=-3))).strftime("%d/%m %H:%M" if reset - agora >= dt.timedelta(hours=24) else "%H:%M")
        st["codex"] = {"modelo": "codex", "carga": min(1, (pct or 0) / 100), "ativo": len(rc),
                       "cota_pct": pct, "semana_pct": pct, "cota": pct, "semana": pct, "janela": "semana", "estourou": volta_codex,
                       "linhas": ([f"{_ident(r.get('nome')) or r.get('nome')}" for r in rc] or ["Parado agora"]) +
                                 [f"Semana: {f'{pct:.0f}%' if pct is not None else 'sem leitura'}"]}
    if top["pc"]:
        pc = _ler(top["pc"], {}) or {}
        visto = _iso(pc.get("visto_em"))
        idade = (agora - visto).total_seconds() if visto else None
        estado = ("ligado · " + ("ocupado" if pc.get("ocupada") else "ocioso")) if idade is not None and idade < 300 else (
            "desligado" if idade is not None and idade > 900 else "sinal atrasado")
        st["pc"] = {"papel": "pc", "carga": 1 if pc.get("ocupada") and idade is not None and idade < 300 else 0,
                    "linhas": [estado, *([pc["gpu"]] if pc.get("gpu") else [])]}
    saida = []
    for id_, rot, grupo, x, y in layout(top):
        saida.append({"id": id_, "rot": rot, "grupo": grupo, "x": x, "y": y, **{"carga": 0, "alerta": False, "url": "", "linhas": []},
                      **st.get(id_, {})})
    return saida


# ---------- placar por etapa, session log e rodapé ----------

_USO = {"chave": None, "lista": []}


def _uso_lista(base):
    """uso.jsonl inteiro (uns 3 MB): relido só quando o arquivo muda, o painel pergunta a cada 5 s."""
    p = Path(base) / "claude" / "uso.jsonl"
    try:
        st = p.stat()
    except OSError:
        return []
    chave = (str(p), st.st_mtime_ns, st.st_size)
    if _USO["chave"] != chave:
        lista = []
        for r in _linhas(p):
            r["_ini"], r["_fim"] = _iso(r.get("inicio")), _iso(r.get("fim"))
            lista.append(r)
        _USO.update(chave=chave, lista=lista)
    return _USO["lista"]


def _usd(x):
    return "US$ " + f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _etapa(no, rot, ok, falha, linhas, vazio, grande=None):
    """Uma etapa do fluxo: número grande + barra (ok × falhou) + linhas curtas. Sem execução: "sem leitura" e o motivo."""
    tot = ok + falha
    pct = (100 if not falha else min(99, int(100 * ok / tot))) if tot else None  # 99,7% não vira 100%
    return {"no": no, "rot": rot, "ok": ok, "falha": falha, "pct": pct,
            "grande": grande if grande is not None else (f"{pct}%" if pct is not None else "sem leitura"),
            "linhas": linhas if tot or grande is not None else [vazio]}


def _resultado(rs):
    """(ok, falhou, custo médio por execução com custo). Interrompida não conta: ninguém errou."""
    rs = [r for r in rs if r.get("_fim") and not r.get("interrompida")]
    ok = sum(1 for r in rs if r.get("ok"))
    custos = [r["custo_usd"] for r in rs if isinstance(r.get("custo_usd"), (int, float))]
    return ok, len(rs) - ok, (sum(custos) / len(custos) if custos else None)


def placar(base, agora, ev, fila_n=0, medidas=None, contas=None):
    """Tudo o que a tela mostra além do grafo, de dado real: legenda de modelos, placar por etapa, session log e rodapé.
    Retrabalho: issues da janela que o portão mandou refazer (a tarefa voltou por conferência ou promessa) ÷ issues trabalhadas."""
    desde = agora - dt.timedelta(hours=24)
    hoje = agora.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    uso = [r for r in _uso_lista(base) if r["_fim"] and r["_fim"] >= desde]
    cnt = lambda tipo, de=None: sum(1 for e in ev if e["tipo"] == tipo and (de is None or e["de"] == de))  # noqa: E731
    medidas = medidas or {}
    contas_ = tuple(contas) if contas is not None else topologia(base)["contas"]

    modelos = [{"id": k, "rot": rot, "cor": cor, "n": sum(1 for r in uso if _modelo(r.get("modelo")) == k)}
               for k, rot, cor in MODELOS]

    def custo(c, por):
        return f"{_usd(c)}/{por}" if c is not None else f"custo/{por}: sem leitura"

    ok, falha, c = _resultado([r for r in uso if r.get("origem") == "agente"])
    orch = _etapa("orch", "Orquestrador", ok, falha, [f"{ok} ok · {falha} falhou", custo(c, "execução")],
                 "sem leitura: nenhuma execução do orquestrador em 24 h")
    criadas = cnt("issue")
    fila = _etapa("fila", "Fila", 0, 0, ["na fila agora", f"{criadas} criada(s) em 24 h" if criadas else "nenhuma criada em 24 h"],
                  "", grande=str(fila_n))
    tarefas = [r for r in uso if r.get("origem") == "tarefa" and str(r.get("conta")) in contas_]
    ok, falha, c = _resultado(tarefas)
    brus = {_ident(r.get("nome")) for r in tarefas} - {None}
    refeitas = {_ident(r.get("nome")) for r in uso if r.get("tipo") in ("conferencia", "promessa")} & brus
    retrab = f"{round(100 * len(refeitas) / len(brus))}%" if brus else "sem leitura"
    contas = _etapa("c1", "Contas", ok, falha, [f"{ok} ok · {falha} falhou", f"{custo(c, 'tarefa')}", f"retrabalho {retrab} ({len(refeitas)} de {len(brus)})"
                                                 if brus else "retrabalho: sem leitura"], "sem leitura: nenhuma tarefa de conta em 24 h")
    ci_ok, ci_f = cnt("ci_ok"), cnt("ci_falha")
    media = medidas.get("ci_media_min")
    ci = _etapa("ci", "CI", ci_ok, ci_f, [f"{ci_ok} verde · {ci_f} vermelho",
                                            f"vermelho → verde {round(media)} min" if media is not None else "vermelho → verde: sem amostra"],
                "sem leitura: nenhum local-ci terminou em 24 h")
    achados, limpas = cnt("codex_achado"), cnt("codex")
    revisao = _etapa("codex", "Revisão", limpas, achados, [f"{limpas} sem achado", f"{achados} com achado"],
                     "sem leitura: nenhuma revisão do Codex em 24 h")
    d_ok, d_f = cnt("deploy"), cnt("erro", "deploy")
    deploy = _etapa("deploy", "No ar", d_ok, d_f, [f"{d_ok} no ar · {d_f} falhou"], "sem leitura: nenhum deploy em 24 h")

    # session log: as últimas 10 execuções que terminaram (a conta, o modelo, a issue, o resultado)
    log = []
    for r in sorted(uso, key=lambda r: r["_fim"])[-10:][::-1]:
        log.append({"hora": r["_fim"].astimezone(BRT).strftime("%H:%M:%S"), "conta": f"c{r['conta']}" if str(r.get("conta")) in contas_ else str(r.get("conta") or "hub"),
                    "modelo": _modelo(r.get("modelo")) or "?", "nome": _ident(r.get("nome")) or str(r.get("nome") or "")[:24],
                    "ok": bool(r.get("ok")), "interrompida": bool(r.get("interrompida")), "turnos": r.get("turnos"),
                    "custo": r.get("custo_usd") if isinstance(r.get("custo_usd"), (int, float)) else None,
                    "erro": str(r.get("erro") or "")[:40]})
    dia = [r for r in uso if r["_fim"] >= hoje and not r.get("interrompida")]
    custos = [r["custo_usd"] for r in dia if isinstance(r.get("custo_usd"), (int, float))]
    rodape = {"ok": sum(1 for r in dia if r.get("ok")), "falha": sum(1 for r in dia if not r.get("ok")), "custo": sum(custos),
              "custo_txt": _usd(sum(custos))}
    return {"modelos": modelos, "etapas": [orch, fila, contas, revisao, ci, deploy], "log": log, "rodape": rodape}


def montar(base=None, horas=24, agora=None, github=True, linear_=True, http=_http, fora=None):
    """fora: {"prs", "linear", "sites"} já lidos (o painel guarda por 1 min); sem ele, lê agora o que github/linear_/http ligam."""
    base, agora = Path(base or data()), agora or dt.datetime.now(UTC)
    desde = agora - dt.timedelta(hours=horas)
    t = topologia(base)
    if fora is None:
        fora = {"prs": prs(desde) if github else [], "linear": eventos_linear(desde) if linear_ else [],
                "sites": sites_agora(http, t) if http else None}
    ev, quem = eventos_disco(base, desde, t=t)
    ev += eventos_github(fora["prs"], desde, quem) + fora["linear"]
    ini, fim = desde.isoformat(timespec="seconds"), agora.isoformat(timespec="seconds")
    ev = sorted((e for e in ev if ini <= e["t"] <= fim), key=lambda e: e["t"])
    grafo = nos(base, agora, fora["sites"], t)
    ids = {n["id"] for n in grafo}
    visiveis = [e for e in ev if e["de"] in ids and e["para"] in ids]  # evento de nó que não está no desenho não vai à tela
    fila = next((n for n in grafo if n["id"] == "fila"), {})
    dbg = next((n for n in grafo if n["id"] == "debugger"), {}).get("medidas") or {}
    if not dbg and t["ci"]:  # CI sem o nó do Debugger: a média vermelho → verde vem da mesma leitura
        from hub import debugger
        dbg = {k: v for k, v in debugger.painel(base, agora).items() if k != "abertos"}
    p = placar(base, agora, ev, fila.get("na_fila", 0), dbg, t["contas"])
    p["rodape"]["contas"] = sum(1 for n in grafo if n["grupo"] == "conta" and n.get("ativo"))
    p["rodape"]["contas_total"] = len(t["contas"])
    return {"agora": fim, "desde": ini, "nos": grafo, "arestas": arestas(_ler(base / "monitor" / "remotos.json", {}) or {}, t),
            "eventos": visiveis, "placar": p}


# ---------- painel (ao vivo) ----------

_FORA = {"em": None, "prs": [], "linear": [], "sites": None}
_TRAVA = threading.Lock()
IDADE_MAX_SITES = dt.timedelta(minutes=30)  # leitura boa mais velha que isto não vale no lugar da rodada toda None
ATRASO = dt.timedelta(minutes=30)  # o GitHub e o Linear chegam com até 1 min de cache: reenvia esta janela, o navegador tira o repetido


def _todos_sem_resposta(sites):
    return bool(sites) and all((v or {}).get("status") is None for v in sites.values())


def _sites_processo_novo(timeout=60):
    """sites_agora num processo novo: o painel que subiu na virada de 22/10 ficou horas com todos None e o mesmo código
    fora dele dava 200 (rede do processo presa num estado ruim). None se o processo novo também falhar."""
    try:
        r = subprocess.run([sys.executable, "-m", "hub.cerebro", "sites"], capture_output=True, text=True, timeout=timeout,
                           cwd=str(RAIZ))
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _leitura_nova(sites, agora):
    """True se a leitura mais recente de `sites` tem no máximo IDADE_MAX_SITES."""
    ts = [t for t in (_iso((v or {}).get("em")) for v in sites.values()) if t]
    return bool(ts) and agora - max(ts) <= IDADE_MAX_SITES


def _ler_sites(agora, ler=None, novo=_sites_processo_novo):
    """Uma rodada dos sites. Todos sem resposta ao mesmo tempo: tenta num processo novo; ainda todos None, fica a leitura
    anterior boa (com o "em" dela) e a próxima rodada tenta de novo. Sem leitura boa anterior, vai a de todos None."""
    s = (ler or sites_agora)()
    if _todos_sem_resposta(s):
        s = novo() or s
    if _todos_sem_resposta(s) and _FORA.get("sites") and not _todos_sem_resposta(_FORA["sites"]) and _leitura_nova(_FORA["sites"], agora):
        return _FORA["sites"]
    em = agora.astimezone(UTC).isoformat(timespec="seconds")
    return {k: {**(v or {}), "em": em} for k, v in s.items()}


def _ler_fora(agora):
    desde = agora - dt.timedelta(hours=24)
    for k, f in (("prs", lambda: prs(desde)), ("linear", lambda: eventos_linear(desde)), ("sites", lambda: _ler_sites(agora))):
        try:
            _FORA[k] = f()
        except Exception:  # noqa: BLE001  (gh, Linear ou rede fora: fica a leitura anterior, a tela segue com o resto)
            pass


def _fora(agora, ttl=60, fundo=True):
    """GitHub, Linear e sites no máximo 1 vez por `ttl` s (o painel pergunta a cada 5 s). Só a 1ª leitura espera (~15 s);
    depois a releitura corre numa thread e quem pergunta recebe a anterior."""
    with _TRAVA:
        velho = _FORA["em"] is None or time.monotonic() - _FORA["em"] >= ttl
        primeira = _FORA["em"] is None
        if velho:
            _FORA["em"] = time.monotonic()
        if velho and (primeira or not fundo):
            _ler_fora(agora)
        elif velho:
            threading.Thread(target=_ler_fora, args=(agora,), daemon=True).start()
        return dict(_FORA)


def ao_vivo(desde=None, base=None, agora=None, fora=None):
    """Dados de /cerebro.json. desde (o "agora" da resposta anterior): só os eventos de ATRASO antes dele para cá."""
    agora = agora or dt.datetime.now(UTC)
    d = montar(base, agora=agora, fora=fora or _fora(agora))
    t = _iso(desde) if desde else None
    if t:
        corte = (t - ATRASO).astimezone(UTC).isoformat(timespec="seconds")
        d["eventos"] = [e for e in d["eventos"] if e["t"] >= corte]
    return d


def html(dados, vivo=None, compacto=False):
    """O HTML de cerebro.html com os dados embutidos. vivo: URL do polling (None = foto parada, o protótipo)."""
    j = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    return ((RAIZ / "hub" / "cerebro.html").read_text(encoding="utf-8")
            .replace("/*DADOS*/null", j(dados)).replace("/*VIVO*/null", j(vivo)).replace("/*COMPACTO*/false", j(compacto)))


def tela(compacto=False):
    return html(ao_vivo(), "/cerebro.json", compacto)


def prototipo(dados, saida):
    Path(saida).write_text(html(dados), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.cerebro")
    ap.add_argument("acao", choices=("json", "prototipo", "sites"))
    ap.add_argument("saida", nargs="?")
    ap.add_argument("--horas", type=float, default=24)
    a = ap.parse_args(argv)
    if a.acao == "sites":  # a leitura dos sites sozinha, para o painel conferir num processo novo (_sites_processo_novo)
        json.dump(sites_agora(), sys.stdout)
        return
    d = montar(horas=a.horas)
    if a.acao == "json":
        json.dump(d, sys.stdout, ensure_ascii=False)
    else:
        prototipo(d, a.saida or "cerebro.html")
        print(f"{a.saida}: {len(d['eventos'])} eventos")


if __name__ == "__main__":
    main()
