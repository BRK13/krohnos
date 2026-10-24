"""Merge mecânico: issue In Review com label `merge`, PR aberto, local-ci verde (ou repo sem CI) e Revisão
do Codex do commit da ponta sem achados sérios → o hub manda a execução fazer o merge, sem LLM no meio. Uma vez por ponta."""
import datetime as dt
import json
import re
import os
import time

from hub import faltou, github_api, config, conta, consertos, debugger, esperas, incidentes, linear, memorias, recibo, revisor_merge

try:  # os evals de agente são nossos: fora do snapshot público não há portão de evals no merge
    from hub.evals import vigia
except ImportError as _e:
    if not faltou(_e, "hub.evals"):
        raise
    vigia = None

LIMPA, ACHADO = "Sem achados sérios", "[P1]"
FALA = ("PR {url} (commit {sha8}): {ci}, Revisão do {quem} sem achados sérios e label merge. Faça o merge com "
        "`gh pr merge {n} -R {repo} --squash --match-head-commit {sha}` (ou o método do repo), confira o deploy "
        "(`voltar-quando {ident} --pr {url} --no-ar`) e termine.")


def _arq(base_dados=None):
    return conta.pasta(base_dados) / "automerge.json"


def _estado(base_dados=None):
    try:
        return json.loads(_arq(base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


DESCANSO_PR = 300  # s: PR ainda não pronto não é reavaliado antes disso (231 chamadas gh em série por passada, 147 s)
DONOS_EXTERNOS = {"clientee-moraes-site": "jpexemplo"}  # repo que não está na org do hub (a org do hub dá 404 nele)


def repo_de(nome):
    """`<dono>/<nome>`: a org do hub, ou o dono real do repo que mora fora dela."""
    return f"{DONOS_EXTERNOS[nome]}/{nome}" if nome in DONOS_EXTERNOS else config.repo(nome)


def _arq_visto(base_dados=None):
    return conta.pasta(base_dados) / "automerge-visto.json"


def _ler_visto(base_dados=None):
    try:
        return json.loads(_arq_visto(base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar_visto(visto, agora, base_dados=None):
    visto = {k: v for k, v in visto.items() if agora - v < 86400}
    arq = _arq_visto(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(visto), encoding="utf-8")
    os.replace(tmp, arq)


def _gravar(est, base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(est), encoding="utf-8")
    os.replace(tmp, arq)


OK_CHECK = ("success", "skipped", "neutral")


def _ci_verde(repo, sha, chamar, usa_ci):
    """Nota do CI se a ponta está verde (checks do GitHub e, no repo com local-ci, o status); None se não."""
    runs = chamar(f"repos/{repo}/commits/{sha}/check-runs").get("check_runs") or []
    if any(r.get("status") != "completed" or r.get("conclusion") not in OK_CHECK for r in runs):
        return None
    if usa_ci:
        ci = next((s for s in chamar(f"repos/{repo}/commits/{sha}/status").get("statuses") or []
                   if s.get("context") == esperas.CONTEXTO), None)
        if not ci or ci.get("state") != "success":
            return None
        return "local-ci verde"
    return "checks do GitHub verdes" if runs else "repo sem CI"


def _pronto(repo, n, sha, chamar, usa_ci):
    """(nota do CI, quem revisou) se o PR pode ir; None se não."""
    nota = _ci_verde(repo, sha, chamar, usa_ci)
    if not nota:
        return None
    rev = esperas._revisao(esperas.comentarios_pr(repo, n, chamar), sha, conta.agora())
    corpo = (rev or {}).get("body") or ""
    if LIMPA not in corpo or ACHADO in corpo or "revisão parcial" in corpo:
        return None
    return nota, esperas.RE_REVISAO.search(corpo).group(1)


def evals_exigido(repo, sha, diff, chamar):
    """True se o diff mexe em caminho vigiado pelos evals e o status `evals` do commit da ponta não é success."""
    if vigia is None or not vigia.exige(repo, diff):
        return False
    status = chamar(f"repos/{repo}/commits/{sha}/status").get("statuses") or []
    return not any(s.get("context") == vigia.CONTEXTO and s.get("state") == "success" for s in status)


def vigiar(consulta=None, chamar=None, base_dados=None, ocupadas=(), log=print, falar_fn=None, reabrir_fn=None,
           aceitar=None, diff_fn=None, prazo=None, agora=None):
    """Devolve as Tarefas aceitas. `aceitar(tarefa)` é quem enfileira (disjuntor incluso) e diz se entrou: só então
    o hub comenta e grava a ponta como mandada; recusada, tenta de novo no próximo tick. Erro de rede num PR: loga e segue.
    PR não pronto descansa DESCANSO_PR s (sem chamada ao GitHub) e `prazo` (time.monotonic) corta a passada;
    o que ficou de fora é o primeiro da próxima, porque quem foi avaliado descansa."""
    aceitar, diff_fn = aceitar or (lambda t: True), diff_fn or memorias.diff_do_pr
    chamar, falar_fn, reabrir_fn = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge")), falar_fn or linear.falar, reabrir_fn or linear.reabrir
    est, voltar, ci_repo = _estado(base_dados), [], {}
    agora = time.time() if agora is None else agora
    visto = _ler_visto(base_dados)
    for issue in revisor_merge.em_revisao(consulta):
        ident = issue["identifier"]
        if "merge" not in {l["name"] for l in issue["labels"]["nodes"]} or f"linear-{ident.lower()}" in ocupadas \
                or esperas.ler(ident, base_dados):  # na fila/rodando ou com espera gravada: a execução volta sozinha
            continue
        for repo, n in recibo._prs(issue):
            if prazo is not None and time.monotonic() > prazo:
                log("automerge: orçamento de tempo da passada acabou; o resto fica para a próxima")
                _gravar_visto(visto, agora, base_dados)
                return voltar
            vkey = f"{ident} {repo}#{n}"
            if agora - visto.get(vkey, 0) < DESCANSO_PR:
                continue
            try:
                pr = chamar(f"repos/{repo}/pulls/{n}")
                visto[vkey] = agora
                if pr.get("state") != "open" or pr.get("merged") or pr.get("draft"):
                    continue
                sha, chave = pr["head"]["sha"], f"{ident} {repo}#{n}"
                if est.get(chave) == sha:  # já mandado: nenhuma outra chamada
                    continue
                if repo not in ci_repo:
                    ci_repo[repo] = esperas._usa_ci(repo, n, chamar)
                r = _pronto(repo, n, sha, chamar, ci_repo[repo])
                if not r:
                    continue
                visto.pop(vkey, None)  # pronto: se o aceitar recusar, tenta de novo no próximo tick
                ci, quem = r
                diff = diff_fn(repo, n)
                if not diff:  # sem ler o diff não há como conferir as decisões do dono: adia, nunca libera
                    log(f"{ident}: merge mecânico adiado em {repo}#{n} (não consegui ler o diff)")
                    continue
                contra = memorias.violacoes_do_diff(diff, repo.split("/")[-1])  # decisão do dono no diff
                if contra:
                    log(f"{ident}: merge mecânico barrado em {repo}#{n} (decisão do dono): {contra[0]}")
                    continue
                if evals_exigido(repo, sha, diff, chamar):  # prompt vigiado só entra com `evals` verde na ponta
                    log(f"{ident}: merge mecânico adiado em {repo}#{n} (evals)")
                    continue
                url = f"https://github.com/{repo}/pull/{n}"
                fala = FALA.format(url=url, sha8=sha[:8], ci=ci, n=n, repo=repo, sha=sha, ident=ident, quem=quem)
                tarefa = reabrir_fn(ident, fala)  # não posta nada: se falhar, nada a repetir
                if not aceitar(tarefa):  # disjuntor ou fila recusou: não grava a ponta, tenta no próximo tick
                    break
                falar_fn(issue["id"], fala, repasse=True)
                voltar.append(tarefa)
                est[chave] = sha
                _gravar(est, base_dados)
                log(f"{ident}: merge mecânico mandado ({url}, {sha[:8]})")
                break  # uma Tarefa por issue
            except Exception as e:  # noqa: BLE001
                log(f"{ident}: automerge {repo}#{n} falhou ({type(e).__name__}: {str(e)[:150]})")
    _gravar_visto(visto, agora, base_dados)
    return voltar


REPOS_DEBUGGER = ("repo-privado", "bot_whatsapp")


def testes_do_diff(diff):
    """Arquivos de teste que o diff toca (`+++ b/tests/test_x.py`): a prova de regressão que fecha o incidente."""
    return sorted(set(re.findall(r"^\+\+\+ b/((?:\S*/)?tests?/test_[\w.-]+\.py)$", diff or "", re.M)))


RE_PR_INCIDENTE = re.compile(r"^fix\b[^\n]*\binc-\d+", re.I)


def _do_debugger(pr):
    """Branch `codex/debugger-*` ou `codex/inc-*`, ou título `fix: INC-…` / `fix: … (INC-xx)`."""
    ref = pr["head"]["ref"]
    return ref.startswith(("codex/debugger-", "codex/inc-")) or bool(RE_PR_INCIDENTE.search(pr.get("title") or ""))


def vigiar_debugger(chamar=None, base_dados=None, log=print, rodar_gh=None, pedir=None, diff_fn=None, repos=None):
    """PR do Debugger (branch `codex/debugger-*` ou título `fix: INC-…`) nos repos do hub entra no merge
    mecânico, como issue com label `merge`: local-ci verde + revisão sem achado sério → `gh pr merge` + pedido
    `atualizar-hub`/`atualizar-runner`. Sem sessão. O incidente citado no PR só vira [feito] quando o pedido volta rc=0
    (`fechar_incidentes`). Uma tentativa por ponta; erro de rede num PR: loga e segue."""
    chamar, diff_fn = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge")), diff_fn or memorias.diff_do_pr
    est, ci_repo, feitos = _estado(base_dados), {}, []
    for nome in repos or REPOS_DEBUGGER:
        repo = repo_de(nome)
        try:
            pulls = chamar(f"repos/{repo}/pulls?state=open&per_page=100")
        except Exception as e:  # noqa: BLE001
            log(f"automerge debugger: {repo} sem lista de PRs ({type(e).__name__})")
            continue
        for p in pulls:
            n = p["number"]
            try:
                if p.get("draft") or not _do_debugger(p):
                    continue
                sha, chave = p["head"]["sha"], f"debugger {repo}#{n}"
                if est.get(chave) == sha:
                    continue
                if repo not in ci_repo:
                    ci_repo[repo] = esperas._usa_ci(repo, n, chamar)
                if not _pronto(repo, n, sha, chamar, ci_repo[repo]):
                    continue
                diff = diff_fn(repo, n)
                if not diff:
                    log(f"debugger {repo}#{n}: merge adiado (não consegui ler o diff)")
                    continue
                contra = memorias.violacoes_do_diff(diff, repo.split("/")[-1])
                if contra:
                    log(f"debugger {repo}#{n}: merge barrado (decisão do dono): {contra[0]}")
                    est[chave] = sha
                    _gravar(est, base_dados)
                    continue
                if evals_exigido(repo, sha, diff, chamar):  # o Debugger também não pula o portão de prompt
                    log(f"debugger {repo}#{n}: merge adiado (evals)")
                    continue
                ok, erro = esperas.merge_pr({"repo": repo, "numero": n}, sha, rodar_gh, chamar)
                est[chave] = sha  # uma tentativa por ponta; commit novo tenta de novo
                _gravar(est, base_dados)
                if not ok:
                    log(f"debugger {repo}#{n}: merge falhou: {erro}")
                    continue
                incs = sorted(set(debugger.RE_INC.findall(f"{p.get('title') or ''} {p.get('body') or ''}")))
                acao = debugger.PEDIDO_POS_MERGE.get(nome)
                # registra antes de pedir: se `pedir` falhar, o próximo tick refaz o pedido (sem pedido = nunca [feito])
                est.setdefault("_pos_merge", []).append({"pr": f"{repo}#{n}", "url": f"https://github.com/{repo}/pull/{n}", "acao": acao or "", "pedido": "", "pede": bool(acao), "incidentes": incs,
                                                         "testes": testes_do_diff(diff)})
                _gravar(est, base_dados)
                pid = ""
                if acao:
                    try:
                        from hub import raiz
                        pid = (pedir or raiz.pedir)(acao, motivo=f"merge pelo hub do PR do Debugger {repo}#{n}", por="hub-debugger", base_dados=base_dados)
                        est["_pos_merge"][-1].update(pedido=pid, pede=False)
                    except Exception as e:  # noqa: BLE001
                        log(f"debugger {repo}#{n}: pedido {acao} falhou ({type(e).__name__}); refaço no próximo tick")
                _gravar(est, base_dados)
                log(f"debugger {repo}#{n}: merge feito pelo hub; pedido {acao or '-'} {pid}; incidentes {incs}")
                feitos.append((repo, n))
            except Exception as e:  # noqa: BLE001
                log(f"debugger {repo}#{n}: automerge falhou ({type(e).__name__}: {str(e)[:150]})")
    return feitos


RE_BRANCH_MOEDOR = re.compile(r"moedor/\d{4}-\d{2}-\d{2}")
PASTA_MOEDOR = "docs/referencias/"


def _so_referencias(repo, n, chamar):
    """True se o PR tem arquivos e todos ficam sob `docs/referencias/` (sem `..`). Lista paginada; o teto é 3000 arquivos,
    o que a API devolve no máximo (o PR diário passou de 1000 arquivos e ficou barrado sem motivo)."""
    arquivos = []
    for pagina in range(1, 31):
        lote = chamar(f"repos/{repo}/pulls/{n}/files?per_page=100&page={pagina}")
        for f in lote:
            arquivos += [f.get("filename") or "", f.get("previous_filename") or PASTA_MOEDOR]  # origem de renome também conta
        if len(lote) < 100:
            return bool(arquivos) and all(a.startswith(PASTA_MOEDOR) and ".." not in a.split("/") for a in arquivos)
    return False


def repos_moedor():
    """Repos de saída do moedor (os das sementes) mais a biblioteca."""
    try:  # o moedor não vai para o snapshot público: sem ele só a biblioteca (mesmo padrão do vigia de evals)
        from hub.moedor_daemon import REPOS
    except ImportError as e:
        if not faltou(e, "hub.moedor_daemon"):
            raise
        return ["biblioteca"]
    return sorted(set(REPOS.values()) | {"biblioteca"})


def vigiar_moedor(chamar=None, base_dados=None, log=print, rodar_gh=None, repos=None):
    """PR diário do moedor TURBO (branch `moedor/AAAA-MM-DD`, label `merge`, do próprio repo, só
    `docs/referencias/`) entra no merge mecânico: ponta verde (local-ci onde o repo usa) e revisão sem achado sério
    (sem revisão não trava: o PR é só dado conferido). Sem sessão, sem pedido de deploy. Uma tentativa por ponta."""
    chamar = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge"))
    est, ci_repo, feitos = _estado(base_dados), {}, []
    for nome in repos or repos_moedor():
        repo = repo_de(nome)
        try:
            pulls = chamar(f"repos/{repo}/pulls?state=open&per_page=100")
        except Exception as e:  # noqa: BLE001
            log(f"automerge moedor: {repo} sem lista de PRs ({type(e).__name__})")
            continue
        for p in pulls:
            n = p["number"]
            try:
                head = p["head"]
                if p.get("draft") or "merge" not in {l["name"] for l in p.get("labels") or []} \
                        or not RE_BRANCH_MOEDOR.fullmatch(head["ref"]) or (head.get("repo") or {}).get("full_name") != repo:
                    continue
                sha, chave = head["sha"], f"moedor {repo}#{n}"
                if est.get(chave) == sha:
                    continue
                if not _so_referencias(repo, n, chamar):
                    log(f"moedor {repo}#{n}: merge barrado (o PR sai de {PASTA_MOEDOR})")
                    continue
                if repo not in ci_repo:
                    ci_repo[repo] = esperas._usa_ci(repo, n, chamar)
                if not _ci_verde(repo, sha, chamar, ci_repo[repo]):
                    continue
                rev = esperas._revisao(esperas.comentarios_pr(repo, n, chamar), sha, conta.agora())
                if rev and (ACHADO in (rev.get("body") or "") or esperas.RE_SERIO.search(rev.get("body") or "")):
                    continue
                ok, erro = esperas.merge_pr({"repo": repo, "numero": n}, sha, rodar_gh, chamar)
                est[chave] = sha
                _gravar(est, base_dados)
                if not ok:
                    log(f"moedor {repo}#{n}: merge falhou: {erro}")
                    continue
                log(f"moedor {repo}#{n}: merge feito pelo hub ({sha[:8]})")
                feitos.append((repo, n))
            except Exception as e:  # noqa: BLE001
                log(f"moedor {repo}#{n}: automerge falhou ({type(e).__name__}: {str(e)[:150]})")
    return feitos


def fechar_incidentes(base_dados=None, log=print, resultado=None, agora=None, pedir=None):
    """Depois do merge do PR do Debugger: o pedido `atualizar-*` voltou rc=0 → marca os incidentes do PR como [feito].
    rc≠0: fica pendente (o Debugger volta a tratar). Sem resultado ainda: espera o próximo tick."""
    est = _estado(base_dados)
    resta, mudou = [], False
    if resultado is None:
        from hub import raiz
        resultado = lambda pid: raiz.resultado(pid, base_dados)  # noqa: E731
    for r in est.get("_pos_merge") or []:
        if r.get("pede") and not r["pedido"]:
            try:
                from hub import raiz
                r["pedido"] = (pedir or raiz.pedir)(r["acao"], motivo=f"merge pelo hub do PR do Debugger {r['pr']} (refeito)", por="hub-debugger", base_dados=base_dados)
                r["pede"], mudou = False, True
            except Exception as e:  # noqa: BLE001
                log(f"debugger {r['pr']}: pedido {r['acao']} segue falhando ({type(e).__name__})")
                resta.append(r)
                continue
        res = resultado(r["pedido"]) if r["pedido"] else {"codigo": 0}
        if res is None:
            resta.append(r)
            continue
        mudou = True
        if res.get("codigo") != 0:
            log(f"debugger {r['pr']}: pedido {r['pedido']} voltou rc={res.get('codigo')}; incidentes seguem pendentes")
            continue
        for inc in r["incidentes"]:
            try:
                # [feito] só com o teste de regressão citado; PR sem teste deixa o incidente pendente (ValueError abaixo)
                testes = "; testes " + ", ".join(r["testes"]) if r.get("testes") else ""
                incidentes.marcar(inc, "feito", f"PR {r['url']} mergeado; {r['acao'] or 'deploy'} rc=0{testes}", base_dados=base_dados, agora=agora)
            except ValueError as e:
                log(f"debugger {r['pr']}: {inc}: {e}")
    if mudou:
        est["_pos_merge"] = resta
        _gravar(est, base_dados)


DIAS_BAIXA = 14
PAGINAS_BAIXA = 3


def _deploy_rc0(acao, desde, base_dados=None):
    """True se o pedido `acao` rodou rc=0 começando depois de `desde` (merged_at do PR): o `atualizar-*` puxa a main inteira."""
    from hub import raiz
    for arq in (raiz.pasta(base_dados) / "resultados").glob("*.json"):
        try:
            r = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("acao") == acao and r.get("codigo") == 0 and conta.de_iso(r["em"]) >= desde:
            return True
    return False


def baixar_incidentes_de_prs_mergeados(chamar=None, base_dados=None, log=print, diff_fn=None, repos=None, agora=None,
                                       deploy_ok=None):
    """PR `fix: INC-…` mergeado por fora do hub (sessão, humano) nunca passa por `vigiar_debugger`, e o incidente
    ficava [pendente] com o conserto no ar. Para cada incidente pendente citado (título ou corpo) num PR mergeado nos últimos
    14 dias: com o pedido `atualizar-*` do repo rc=0 depois do merge e `tests/test_*.py` no diff → [feito]. Sem teste no diff:
    não marca e registra no diário (uma vez por PR). Sem deploy ainda: tenta no próximo tick."""
    chamar, diff_fn = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge")), diff_fn or memorias.diff_do_pr
    deploy_ok = deploy_ok or (lambda acao, desde: _deploy_rc0(acao, desde, base_dados))
    agora = agora or conta.agora()
    corte = agora - dt.timedelta(days=DIAS_BAIXA)
    est, baixados = _estado(base_dados), []
    for nome in repos or REPOS_DEBUGGER:
        repo, acao = repo_de(nome), debugger.PEDIDO_POS_MERGE.get(nome)
        if not acao:
            continue
        pulls = []
        try:
            for pagina in range(1, PAGINAS_BAIXA + 1):  # ordenado por updated: para na página que já é mais velha que o corte
                lote = chamar(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50&page={pagina}")
                pulls += lote
                if len(lote) < 50 or all(p.get("updated_at") and conta.de_iso(p["updated_at"]) < corte for p in lote):
                    break
        except Exception as e:  # noqa: BLE001
            log(f"baixa de incidentes: {repo} sem lista de PRs ({type(e).__name__})")
            if not pulls:
                continue
        for p in pulls:
            n = p["number"]
            try:
                if not p.get("merged_at") or conta.de_iso(p["merged_at"]) < corte:
                    continue
                chave = f"baixa {repo}#{n}"
                if est.get(chave):
                    continue
                incs = sorted(set(debugger.RE_INC.findall(f"{p.get('title') or ''} {p.get('body') or ''}")))
                pendentes = [i["id"] for i in incidentes.listar(so_pendentes=True, base_dados=base_dados, agora=agora)
                             if i["id"] in incs]
                if not pendentes:
                    continue
                if not deploy_ok(acao, conta.de_iso(p["merged_at"])):
                    continue
                url = f"https://github.com/{repo}/pull/{n}"
                testes = testes_do_diff(diff_fn(repo, n))
                if not testes:
                    est[chave] = pendentes
                    _gravar(est, base_dados)
                    log(f"baixa {repo}#{n}: {', '.join(pendentes)} seguem pendentes (sem teste de regressão no diff)")
                    consertos.registrar(f"{', '.join(pendentes)}: PR {url} mergeado e no ar, sem tests/ no diff",
                                        "baixa automática exige o teste de regressão (KRN-1095)",
                                        "incidente segue pendente; falta o teste ou 'sem teste: <motivo>' à mão",
                                        "tentando", por="hub-debugger", base_dados=base_dados, agora=agora)
                    continue
                recusados = []
                for inc in pendentes:
                    try:
                        incidentes.marcar(inc, "feito", f"PR {url}; {acao} rc=0; testes {', '.join(testes)}",
                                          base_dados=base_dados, agora=agora, gh=chamar)
                        baixados.append(inc)
                    except ValueError as e:
                        recusados.append(inc)
                        log(f"baixa {repo}#{n}: {inc}: {e}")
                if not recusados:  # recusa não é definitiva: tenta de novo no próximo tick
                    est[chave] = pendentes
                    _gravar(est, base_dados)
            except Exception as e:  # noqa: BLE001
                log(f"baixa {repo}#{n}: falhou ({type(e).__name__}: {str(e)[:150]})")
    return baixados
