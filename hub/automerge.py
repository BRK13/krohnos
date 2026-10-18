"""Merge mecânico: issue In Review com label `merge`, PR aberto, local-ci verde (ou repo sem CI) e Revisão
do Codex do commit da ponta sem achados sérios → o hub manda a execução fazer o merge, sem LLM no meio. Uma vez por ponta."""
import json
import re
import os

from hub import github_api, config, conta, debugger, esperas, incidentes, linear, memorias, recibo, revisor_merge

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


def _gravar(est, base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(est), encoding="utf-8")
    os.replace(tmp, arq)


OK_CHECK = ("success", "skipped", "neutral")


def _pronto(repo, n, sha, chamar, usa_ci):
    """(nota do CI, quem revisou) se o PR pode ir; None se não."""
    runs = chamar(f"repos/{repo}/commits/{sha}/check-runs").get("check_runs") or []
    if any(r.get("status") != "completed" or r.get("conclusion") not in OK_CHECK for r in runs):
        return None
    if usa_ci:
        ci = next((s for s in chamar(f"repos/{repo}/commits/{sha}/status").get("statuses") or []
                   if s.get("context") == esperas.CONTEXTO), None)
        if not ci or ci.get("state") != "success":
            return None
        nota = "local-ci verde"
    else:
        nota = "checks do GitHub verdes" if runs else "repo sem CI"
    rev = esperas._revisao(esperas.comentarios_pr(repo, n, chamar), sha, conta.agora())
    corpo = (rev or {}).get("body") or ""
    if LIMPA not in corpo or ACHADO in corpo or "revisão parcial" in corpo:
        return None
    return nota, esperas.RE_REVISAO.search(corpo).group(1)


def vigiar(consulta=None, chamar=None, base_dados=None, ocupadas=(), log=print, falar_fn=None, reabrir_fn=None,
           aceitar=None, diff_fn=None):
    """Devolve as Tarefas aceitas. `aceitar(tarefa)` é quem enfileira (disjuntor incluso) e diz se entrou: só então
    o hub comenta e grava a ponta como mandada; recusada, tenta de novo no próximo tick. Erro de rede num PR: loga e segue."""
    aceitar, diff_fn = aceitar or (lambda t: True), diff_fn or memorias.diff_do_pr
    chamar, falar_fn, reabrir_fn = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge")), falar_fn or linear.falar, reabrir_fn or linear.reabrir
    est, voltar, ci_repo = _estado(base_dados), [], {}
    for issue in revisor_merge.em_revisao(consulta):
        ident = issue["identifier"]
        if "merge" not in {l["name"] for l in issue["labels"]["nodes"]} or f"linear-{ident.lower()}" in ocupadas \
                or esperas.ler(ident, base_dados):  # na fila/rodando ou com espera gravada: a execução volta sozinha
            continue
        for repo, n in recibo._prs(issue):
            try:
                pr = chamar(f"repos/{repo}/pulls/{n}")
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
                ci, quem = r
                diff = diff_fn(repo, n)
                if not diff:  # sem ler o diff não há como conferir as decisões do dono: adia, nunca libera
                    log(f"{ident}: merge mecânico adiado em {repo}#{n} (não consegui ler o diff)")
                    continue
                contra = memorias.violacoes_do_diff(diff, repo.split("/")[-1])  # decisão do dono no diff
                if contra:
                    log(f"{ident}: merge mecânico barrado em {repo}#{n} (decisão do dono): {contra[0]}")
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
    return voltar


REPOS_DEBUGGER = ("pereiraos-privado", "triagem-wpp")


def testes_do_diff(diff):
    """Arquivos de teste que o diff toca (`+++ b/tests/test_x.py`): a prova de regressão que fecha o incidente."""
    return sorted(set(re.findall(r"^\+\+\+ b/((?:\S*/)?tests?/test_[\w.-]+\.py)$", diff or "", re.M)))


def _do_debugger(pr):
    return pr["head"]["ref"].startswith("codex/debugger-") or (pr.get("title") or "").lower().startswith("fix: inc")


def vigiar_debugger(chamar=None, base_dados=None, log=print, rodar_gh=None, pedir=None, diff_fn=None, repos=None):
    """PR do Debugger (branch `codex/debugger-*` ou título `fix: INC-…`) nos repos do hub entra no merge
    mecânico, como issue com label `merge`: local-ci verde + revisão sem achado sério → `gh pr merge` + pedido
    `atualizar-hub`/`atualizar-runner`. Sem sessão. O incidente citado no PR só vira [feito] quando o pedido volta rc=0
    (`fechar_incidentes`). Uma tentativa por ponta; erro de rede num PR: loga e segue."""
    chamar, diff_fn = chamar or (lambda caminho: github_api.get(caminho, consumidor="automerge")), diff_fn or memorias.diff_do_pr
    est, ci_repo, feitos = _estado(base_dados), {}, []
    for nome in repos or REPOS_DEBUGGER:
        repo = config.repo(nome)
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
