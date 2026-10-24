"""Main vermelho é incidente (princípios 7 e 14). Em 14/10 o #695 quebrou o teste do cartão do Conector e
todos os PRs da fila do ClienteJ falharam nele; os agentes seguiram pedindo `/local-ci`.
A cada 10 min (tick da conta 1): local-ci vermelho na ponta do main, ou as 2 últimas rodadas de PRs diferentes
vermelhas no mesmo teste que nenhum dos dois tocou → abre UMA issue P1 "main vermelho" com o teste e o commit
culpado, registra o incidente e segura as esperas do repo (hub/esperas.py) até o teste voltar a passar.
O nome do teste vem do status do runner ("falhou em npm test: <testes> (...)"); o formato antigo, sem nomes, dá []
e então a regra dos 2 PRs não abre nada (não dá para provar que é o mesmo teste). Sem status no main = nada a fazer."""
import contextlib
import datetime as dt
import functools
import json
import re
from pathlib import Path

from hub import github_api, config, conta, esperas, incidentes

REPO = config.repo("clienteh")
CONTEXTO = "local-ci"
INTERVALO = dt.timedelta(minutes=10)
POR_PAGINA = 100
PAGINAS = 3
MAX_STATUS = 100  # teto de chamadas de status por checagem (o backlog real tem ~19 de 20 PRs sem local-ci ainda)
MAX_ABERTO = dt.timedelta(hours=24)  # sem conserto em 24 h, solta as esperas mesmo assim
RE_TESTES = re.compile(r"falhou em npm test:\s*([^()]+?)\s*\(")


def testes(descricao):
    m = RE_TESTES.search(descricao or "")
    return [t.strip() for t in m.group(1).split(",") if t.strip()] if m else []


def _arq(repo, base_dados=None):
    return conta.pasta(base_dados) / "main-vermelho" / f"{repo.replace('/', '__')}.json"


def _fechado(repo, base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_arq(repo, base_dados).with_suffix(".fechado").read_text(encoding="utf-8"))
    return None


def _fechar(repo, atual, motivo, base_dados, agora, log):
    """Solta as esperas e deixa a marca: só uma rodada NOVA vermelha reabre (as velhas ainda aparecem na amostra).
    Guarda sempre a issue: qualquer que seja o motivo, a próxima rodada vermelha do mesmo teste a readota
    se ainda estiver aberta, em vez de abrir duplicata."""
    arq = _arq(repo, base_dados)
    marca = {"teste": atual["teste"], "fechado_em": conta.iso(agora), "issue": atual["issue"]}
    arq.with_suffix(".fechado").write_text(json.dumps(marca), encoding="utf-8")
    arq.unlink(missing_ok=True)
    log(motivo)


def _issue_encerrada(ident):
    from hub import linear
    with contextlib.suppress(Exception):  # Linear fora: segue segurando, tenta no próximo tick
        t = linear.gql("query($i: String!) { issue(id: $i) { state { type } } }", i=ident)["issue"]["state"]["type"]
        return t in ("completed", "canceled")
    return False


def _issue_aberta_do_teste(teste, projeto):
    """id da issue ainda aberta com o título `main vermelho: <teste>` NO PROJETO do repo (o vigia perdeu a marca,
    ou outra conta a abriu). Teste homônimo em outro projeto não conta."""
    from hub import linear
    titulo = f"main vermelho: {teste}"
    with contextlib.suppress(Exception, SystemExit):  # Linear fora: segue para criar, como antes
        nos = linear.gql('query($t: String!, $p: String!) { issues(first: 20, filter: {title: {eq: $t}, project: {name: {eqIgnoreCase: $p}}, '
                         'state: {type: {nin: ["completed", "canceled"]}}}) { nodes { identifier title } } }', t=titulo, p=projeto)["issues"]["nodes"]
        return next((n["identifier"] for n in nos if n["title"] == titulo), None)
    return None


def aberto(repo, base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_arq(repo, base_dados).read_text(encoding="utf-8"))
    return None


def _ci(gh, repo, ref):
    st = gh(f"repos/{repo}/commits/{ref}/status")
    return next((s for s in st.get("statuses") or [] if s.get("context") == CONTEXTO), None)


def _rodadas(gh, repo):
    """Rodadas concluídas do local-ci, da mais antiga à mais nova. Pagina os PRs até ter as 2 últimas mais um verde."""
    out, chamadas = [], 0
    for pag in range(1, PAGINAS + 1):
        prs = gh(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={POR_PAGINA}&page={pag}")
        for p in prs:
            if chamadas >= MAX_STATUS:
                break
            chamadas += 1
            with contextlib.suppress(Exception):  # um PR sem status/erro pontual não derruba a análise
                s = _ci(gh, repo, p["head"]["sha"])
                if s and s.get("state") in ("success", "failure"):
                    out.append({"pr": p["number"], "estado": s["state"], "testes": testes(s.get("description")),
                                "em": s.get("updated_at") or ""})
            if len(out) >= 2 and any(r["estado"] == "success" for r in out):
                break
        else:
            if len(prs) == POR_PAGINA and chamadas < MAX_STATUS:
                continue
        break
    return sorted(out, key=lambda r: r["em"])


@functools.lru_cache(maxsize=8)  # create falhando não repete as ~20 chamadas a cada tick
def _suspeitos(gh, repo, desde, ate, teste):
    q = f"repos/{repo}/commits?sha=main&per_page=20" + (f"&since={desde}" if desde else "") + (f"&until={ate}" if ate else "")
    base = Path(teste).name.split(".")[0]
    linhas = []
    for c in gh(q):
        msg = (c.get("commit") or {}).get("message", "").splitlines()[0][:100]
        mexeu = False
        with contextlib.suppress(Exception):  # a lista não traz arquivos; um commit a um commit
            mexeu = any(base in Path(f["filename"]).name for f in gh(f"repos/{repo}/commits/{c['sha']}").get("files") or [])
        linhas.append(f"- `{c['sha'][:8]}` {msg}{' ← mexeu em arquivo ' + base if mexeu else ''}")
    return tuple(linhas)


def _abrir(repo, teste, sha, suspeitos, origem, criar, base_dados, agora, log, encerrada=None):
    from hub import linear
    marca = _fechado(repo, base_dados) or {}
    # I6 + fechada por qualquer motivo, a P1 do mesmo teste pode seguir aberta: readota em vez de abrir a 2ª
    existente = None
    if marca.get("teste") == teste and marca.get("issue") and not (encerrada or _issue_encerrada)(marca["issue"]):
        existente = marca["issue"]
    else:  # sem marca: procura no Linear
        with contextlib.suppress(Exception, SystemExit):
            existente = _issue_aberta_do_teste(teste, linear.projeto_do_repo(repo))
    if existente:
        est = {"issue": existente, "teste": teste, "sha": sha, "desde": conta.iso(agora)}
        _arq(repo, base_dados).parent.mkdir(parents=True, exist_ok=True)
        _arq(repo, base_dados).write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
        log(f"main vermelho em {repo}: {teste} de novo → segue em {existente}")
        return est
    # `merge`: o AGENTS.md do clienteh (seção 5) libera o squash com o local-ci verde; sem a label o conserto ficava parado
    criar = criar or (lambda t, d, p: linear.criar_issue(linear.projeto_do_repo(repo), t, d, "Backlog", ["main-vermelho", "merge"], p))
    desc = (f"**Por quê:** {origem}. Enquanto isso, todo PR do {repo} falha nesse teste e a fila para "
            "(caso real: #695 em 14/10).\n\n"
            f"**Teste:** `{teste}`\n**Commit culpado (suspeitos, do mais novo):**\n" + "\n".join(suspeitos() or [f"- `{sha[:8]}`"]) +
            "\n\n**Dono do conserto:** faixa Debugger (hub/debugger.py).\n\n**Mudança:** consertar o teste ou o código no main (PR próprio) e conferir o local-ci verde.\n"
            f"**Pronto quando:** local-ci verde num PR depois do conserto; o {config.get('produto.nome')} solta as esperas sozinho.")
    try:
        ident = criar(f"main vermelho: {teste}", desc, 1).split()[0]
    except (Exception, SystemExit) as e:  # noqa: BLE001  # sem issue não grava estado: tenta de novo na próxima rodada
        log(f"main vermelho em {repo}: {teste}; não abriu a issue ({type(e).__name__}: {str(e)[:200]})")
        return None
    est = {"issue": ident, "teste": teste, "sha": sha, "desde": conta.iso(agora)}
    arq = _arq(repo, base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
    incidentes.registrar(f"main vermelho em {repo}: {teste} ({origem})", "main_vermelho",
                         "hub/main_vermelho.py + esperas seguras", ident, chave=f"main:{repo}:{teste}",
                         base_dados=base_dados, agora=agora)
    log(f"main vermelho em {repo}: {teste} → {ident}")
    return est


def checar(repo=REPO, gh=None, criar=None, base_dados=None, agora=None, log=print, encerrada=None):
    gh, agora = gh or (lambda caminho: github_api.get(caminho, consumidor="main_vermelho")), agora or conta.agora()
    try:
        return _checar(repo, gh, criar, base_dados, agora, log, encerrada)
    except Exception as e:  # noqa: BLE001  # rede/gh fora: tenta no próximo tick
        log(f"main vermelho: {repo}: {type(e).__name__}: {str(e)[:200]}")
        return None


def _checar(repo, gh, criar, base_dados, agora, log, encerrada=None):
    atual = aberto(repo, base_dados)
    ponta = gh(f"repos/{repo}/commits/main")["sha"]
    ci_main = _ci(gh, repo, "main")
    rodadas = _rodadas(gh, repo)
    if atual:
        if (ci_main and ci_main["state"] == "success") or any(
                r["estado"] == "success" and r["em"] > atual["desde"] for r in rodadas):
            _fechar(repo, atual, f"main de {repo} voltou a passar; esperas liberadas ({atual['issue']})", base_dados, agora, log)
        elif (encerrada or _issue_encerrada)(atual["issue"]):
            _fechar(repo, atual, f"{atual['issue']} encerrada; soltei as esperas de {repo}", base_dados, agora, log)
        elif agora - conta.de_iso(atual["desde"]) >= MAX_ABERTO:
            _fechar(repo, atual, f"main vermelho há 24 h: soltei as esperas de {repo} ({atual['issue']})", base_dados, agora, log)
        return None
    marca = _fechado(repo, base_dados)

    def novo(t, em):  # depois de fechar, só rodada posterior ao fechamento reabre o mesmo teste
        return not marca or t != marca["teste"] or (em or "") > marca["fechado_em"]
    if ci_main and ci_main["state"] == "failure":
        t = (testes(ci_main.get("description")) or ["(ver o log do local-ci)"])[0]
        if novo(t, ci_main.get("updated_at")):
            return _abrir(repo, t, ponta, lambda: [f"- `{ponta[:8]}` (ponta do main)"], "local-ci vermelho no main",
                          criar, base_dados, agora, log, encerrada)
    if len(rodadas) < 2:
        return None
    a, b = rodadas[-2], rodadas[-1]
    if a["pr"] == b["pr"] or a["estado"] != "failure" or b["estado"] != "failure":
        return None
    for t in [t for t in a["testes"] if t in b["testes"]]:  # sem nomes (formato antigo): lista vazia, nada abre
        if not novo(t, b["em"]):
            continue
        tocados = {f["filename"] for n in (a["pr"], b["pr"]) for f in gh(f"repos/{repo}/pulls/{n}/files?per_page=100")}
        if t in tocados:
            continue
        verde = next((r["em"] for r in reversed(rodadas[:-2]) if r["estado"] == "success"), "")
        return _abrir(repo, t, ponta, lambda: _suspeitos(gh, repo, verde, a["em"], t),
                      f"PRs #{a['pr']} e #{b['pr']} seguidos falharam em `{t}` sem mexer nele", criar, base_dados, agora, log,
                      encerrada)
    return None


def vigiar_no_tick(base_dados=None, agora=None, log=print):
    agora = agora or conta.agora()
    marca = conta.pasta(base_dados) / "main-vermelho" / "_ultimo"
    with contextlib.suppress(OSError, ValueError):
        if agora - conta.de_iso(marca.read_text().strip()) < INTERVALO:
            return None
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(conta.iso(agora))
    return checar(base_dados=base_dados, agora=agora, log=log)
