"""Espera de CI e revisão FORA da sessão: a tarefa grava a espera, termina e volta sozinha.

Antes, o fim da sessão era um laço `until gh api .../statuses` esperando o `local-ci` ou o comentário "Revisão do
Codex"; com a fila do local-ci lenta, a tarefa ia até o teto de 3 h e morria por TIMEOUT sem fechar (
180). Agora:

    python3 -m hub.linear voltar-quando -pr https://github.com/<org>/x/pull/12 [--max-horas 6]

grava $HUB_DATA/claude/esperas/.json ({pr, repo, numero, sha, criada_em, max_horas}) e a sessão termina. A
cada tick da conta 1 (hub/agenda.py, junto do puxar_linear), `prontas()` olha cada espera (no máximo a cada
INTERVALO): status `local-ci` do commit da ponta do PR e comentário "Revisão do Codex/Claude". Pronta quando o
local-ci saiu (verde ou vermelho) e há revisão, ou passaram ESPERA_REVISAO desde o local-ci; também quando o PR
foi mergeado/fechado ou a espera passou de max_horas. A agenda reabre a issue com a fala e apaga a espera.

Só o clienteh publica `local-ci`; nos outros repos o status nunca chega. Por isso `gravar` olha os PRs recentes do
repo (`com_ci`): sem local-ci neles, a espera volta com a revisão ou ESPERA_REVISAO depois de gravada. Só contam PRs
já mergeados: um PR aberto pode estar na fila do local-ci sem status ainda, e a fila lenta deixaria todos assim.

Depois do merge: `voltar-quando -pr <url> --no-ar [sha] [--url <página>]` espera o deploy. Antes, cada
tarefa conferia o deploy, via commit velho (o ClienteJ espaça os deploys em 2 h, 7h-23h BRT), comentava "ainda não
subiu" e era reaberta: 48 comentários assim em 24 h. Agora o hub confere sozinho (recibo.no_ar: /api/versao do
ClienteJ, HEAD do hub/runner, deploy do Dokploy; com --url, o commit que a página mostra) e só reabre quando o
commit está no ar, quando não há como conferir ou no prazo (max_horas).
"""
import contextlib
import datetime as dt
import json
import re
import subprocess
import urllib.request
from pathlib import Path

from hub import backoff, config, conta

UTC = dt.timezone.utc
CONTEXTO = "local-ci"
ESPERA_REVISAO = dt.timedelta(minutes=20)
INTERVALO_NO_AR = dt.timedelta(minutes=10)  # deploy leva de minutos a 2 h (ClienteJ); 10 min basta
INTERVALO = dt.timedelta(minutes=3)  # cada espera custa 3 chamadas ao GitHub; o token é o mesmo das sessões
MAX_HORAS = 3  # nenhuma espera passa de 3 h sem voltar para a fila (era 6 h; o painel marca "Travada" aos 3)
TETO_HORAS = 3  # `--max-horas` maior que isso é cortado: voltar-quando sempre tem prazo
TETO_NO_AR_HORAS = 12  # espera de deploy: o ClienteJ só publica de 7h a 23h BRT, então um merge da noite espera a manhã
REVISAO_TETO = dt.timedelta(hours=2)  # nunca esperar mais de 2 h por revisor; depois o executor revisa o diff e segue
SEM_STATUS = dt.timedelta(hours=2)  # repo com local-ci, mas nenhum status no PR em 2 h: a fila travou; não espere mais
SEM_CI = ("Este repo NÃO tem CI: nenhum status vai chegar. Rode agora os testes do repo na sua worktree (o que o "
          "README/AGENTS.md/package.json manda) e, verdes, faça o merge; não grave `voltar-quando` de novo por causa de CI.")
TRAVADO = ("O local-ci não começou em 2 h (fila travada). Não espere mais: rode a checagem de tipos e os testes dos "
           f"arquivos que você mudou; verdes, faça o merge e diga no PR que o local-ci não rodou (pedido do {config.dono()}, "
           "13/10/2026: nunca ficar esperando status que não chega).")
AMOSTRA_CI = 5  # PRs mergeados recentes olhados para saber se o repo usa local-ci
RE_PR = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
RE_SHA = re.compile(r"\b[0-9a-f]{40}\b")
RE_REVISAO = re.compile(r"\*\*Revisão do (Codex|Claude)\*\*")
RE_SERIO = re.compile(r"\[P[0-2]\]")  # P0, P1 e P2 são achado a corrigir; P3 é sugestão
RE_LEVE = re.compile(r"\[P3\]")
RE_DE_NOVO = re.compile(r"^\s*/local-ci\b")  # comentário que pede ao runner rodar o mesmo commit de novo
QUEM_PEDE = ("OWNER", "MEMBER", "COLLABORATOR")  # os mesmos que o vigia do ci-local.sh aceita
SEM_REVISAO = ("Passaram 2 h do CI e nenhuma revisão chegou (KRN-968): não espere mais. Leia você mesmo o diff do PR "
               "(`gh pr diff`), procurando bug real, regressão e teste que não testa; corrija o que achar, diga no PR "
               "que a revisão não veio e que você revisou, e siga para o merge (se a issue tem a label `merge`).")
FIM = ("Só termine: corrija se vermelho, faça o merge, confira no ar. Vermelho: leia o log antes (trecho abaixo); só "
       "comente `/local-ci` no PR (o runner roda de novo, na fila) e `voltar-quando` de novo se o log mostrar timeout, "
       "ENOSPC ou falha de infra; erro de teste ou build é do código: corrija. Nunca rode scripts/ci-local.sh.")
# Log do scripts/ci-local.sh do clienteh, gravado pelo runner nesta VPS; o claude lê pela ACL do instalar.sh do
# clientej-teste (bot_whatsapp). Sem o trecho, a tarefa chutava "timeout" e re-disparava às cegas.
LOG_CI = "/home/usuario/.cache/clienteh-ci-local/logs/{sha}.log"
LINHAS_LOG = 60
MAX_FALHAS = 15
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_FALHA = re.compile(r"^\s*FAIL\s+(.+)$")  # vitest: " FAIL  tests/x.test.ts > grupo > nome"


def _pasta(base_dados=None):
    return conta.pasta(base_dados) / "esperas"


def _arq(ident, base_dados=None):
    return _pasta(base_dados) / f"{ident.upper()}.json"


def _espera_gh(e):
    """5xx, 429 e timeout repetem (como o `espera_github` do `gh`); cota esgotada (CotaBaixa) não."""
    from hub import github_api
    if isinstance(e, subprocess.TimeoutExpired) or (isinstance(e, github_api.ErroHTTP) and e.status in (429, 500, 502, 503, 504)):
        return 0.0
    return None


def gh_api(caminho):
    """GET medido e com cache curto; credencial do gh até a App ser configurada. Repete 5xx/429/timeout com backoff."""
    from hub import github_api
    return backoff.com_backoff(lambda: github_api.get(caminho), _espera_gh)


def _pr(url):
    m = RE_PR.search(url or "")
    if not m:
        raise ValueError(f"não é URL de PR do GitHub: {url!r}")
    return m.group(1), int(m.group(2))


def _usa_ci(repo, numero, chamar):
    """Algum PR mergeado recente do repo recebeu `local-ci`? Na dúvida (sem rede, nenhum PR mergeado), sim: espera o
    CI como antes. PR aberto não conta: sem status pode ser só a fila do local-ci atrasada (revisão do #175)."""
    try:
        outros = [p for p in chamar(f"repos/{repo}/pulls?state=closed&per_page={AMOSTRA_CI * 4}")
                  if p.get("number") != numero and p.get("merged_at")][:AMOSTRA_CI]
        if not outros:
            return True
        for p in outros:
            status = chamar(f"repos/{repo}/commits/{p['head']['sha']}/status")
            if any(s.get("context") == CONTEXTO for s in status.get("statuses") or []):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True


def gravar(ident, pr, max_horas=MAX_HORAS, chamar=None, base_dados=None, agora=None, no_ar=None, url=None):
    """Grava (ou troca) a espera da issue. O sha é só registro: a conferência sempre olha a ponta atual do PR.
    `no_ar` (sha, ou "" = o merge do PR): espera o deploy em vez do CI; `url`: página que mostra o commit no ar."""
    repo, numero = _pr(pr)
    chamar = chamar or gh_api
    try:
        sha = chamar(f"repos/{repo}/pulls/{numero}")["head"]["sha"]
    except Exception:  # noqa: BLE001 — sem rede agora: a conferência acha a ponta depois
        sha = ""
    teto = TETO_NO_AR_HORAS if no_ar is not None else TETO_HORAS
    e = {"ident": ident.upper(), "pr": f"https://github.com/{repo}/pull/{numero}", "repo": repo, "numero": numero,
         "sha": sha, "criada_em": conta.iso(agora or conta.agora()), "max_horas": min(float(max_horas), teto)}
    if no_ar is None:
        e["com_ci"] = _usa_ci(repo, numero, chamar)
        e["voltas"] = _contar_volta(ident.upper(), sha, base_dados)
    else:
        e.update(no_ar=no_ar, url=url or "")
    arq = _arq(ident, base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(arq)
    return e


def _contar_volta(ident, sha, base_dados=None):
    """Quantas vezes seguidas a issue voltou a esperar a revisão do mesmo commit (20 voltas em 30 min
    com o CI verde e a revisão que não vinha). Commit novo zera."""
    arq = _pasta(base_dados).parent / "esperas-voltas.json"  # fora da pasta: todas() lê todo *.json de lá
    try:
        m = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m = {}
    ant = m.get(ident) or {}
    n = ant.get("n", -1) + 1 if sha and ant.get("sha") == sha else 0
    m[ident] = {"sha": sha, "n": n}
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(".esperas-voltas.json.tmp")
    tmp.write_text(json.dumps(m), encoding="utf-8")
    tmp.replace(arq)
    return n


def espera_revisao(e):
    """Espera pela revisão depois do CI: 20 min e dobra a cada volta no mesmo commit, até 2 h (era 5 h 20)."""
    return min(ESPERA_REVISAO * 2 ** min(int(e.get("voltas") or 0), 4), REVISAO_TETO)


def ler(ident, base_dados=None):
    try:
        return json.loads(_arq(ident, base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def todas(base_dados=None):
    itens = []
    for arq in sorted(_pasta(base_dados).glob("*.json")):
        try:
            itens.append(json.loads(arq.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return itens


def apagar(ident, base_dados=None):
    _arq(ident, base_dados).unlink(missing_ok=True)


def gravada_desde(ident, inicio, base_dados=None):
    """A execução que começou em `inicio` terminou com `voltar-quando`? Espera mais velha que a execução é de uma
    rodada anterior (esta rodada já cuidou do PR, senão teria gravado de novo): sai, para não reabrir à toa."""
    e = ler(ident, base_dados)
    if not e:
        return False
    if inicio and conta.de_iso(e["criada_em"]) < conta.de_iso(inicio):
        apagar(ident, base_dados)
        return False
    return True


def comentarios_pr(repo, numero, chamar):
    """Todas as páginas, em ordem: a revisão mais recente pode vir após 100 comentários."""
    caminho = f"repos/{repo}/issues/{numero}/comments?per_page=100"
    comentarios = []
    pagina = 1
    while True:
        lote = chamar(caminho if pagina == 1 else f"{caminho}&page={pagina}")
        comentarios.extend(lote)
        if len(lote) < 100:
            return comentarios
        pagina += 1


def _revisao(comentarios, sha, desde):
    """Último comentário de revisão deste commit (ou, sem marca de commit, feito depois da espera)."""
    achadas = []
    for c in comentarios:
        corpo = c.get("body") or ""
        if not RE_REVISAO.search(corpo):
            continue
        marca = re.search(r"commit `([0-9a-f]{7,40})`", corpo)
        if (marca and sha.startswith(marca.group(1))) or (not marca and conta.de_iso(c["created_at"]) >= desde):
            achadas.append(c)
    return achadas[-1] if achadas else None


def _pedida_de_novo(comentarios, ci):
    """`/local-ci` comentado depois do status: o vigia vai rodar o mesmo commit de novo, então o vermelho é velho."""
    quando = conta.de_iso(ci.get("updated_at") or ci.get("created_at"))
    return any(RE_DE_NOVO.match(c.get("body") or "") and c.get("author_association") in QUEM_PEDE
               and conta.de_iso(c["created_at"]) > quando for c in comentarios)


def revisao_limpa(rev, sha):
    """Revisão DESTE commit (com a marca `commit `<sha>``) que diz "Sem achados sérios" sem ressalva."""
    if not rev:
        return False
    corpo = rev.get("body") or ""
    marca = re.search(r"commit `([0-9a-f]{7,40})`", corpo)
    if not (marca and sha.startswith(marca.group(1))):
        return False
    if "parcial" in corpo.lower() or RE_SERIO.search(corpo):  # cabeçalho incluso: "diff cortado: revisão parcial"
        return False
    resto = corpo.split("\n", 1)[1] if "\n" in corpo else ""
    # só achado P3 (baixo) não é "achado sério": CI verde + revisão assim também fecha sem sessão
    return resto.strip().lower().startswith("sem achados sérios") or bool(RE_LEVE.search(corpo))


def desfecho_sem_ia(e, chamar=None, agora=None, get=None):
    """None = a volta precisa de IA (caminho de sempre); senão {"tipo": "no_ar"|"pronto", "texto", "sha"}.
    "pronto" = PR aberto, CI verde (ou repo sem CI) e revisão limpa do commit da ponta; o merge o hub decide pela
    label da issue (agenda.voltar_das_esperas)."""
    out = {}
    situacao(e, chamar, agora, get, _out=out)
    return out.get("desfecho")


def merge_pr(e, sha, rodar=None, get=None):
    """`gh pr merge` com --match-head-commit: só funde o commit que foi revisado. Squash; se o repo não permite,
    --merge. Devolve (ok, erro curto). `rodar(args)` → (código, saída); os testes trocam por um falso.
    Depois de qualquer falha (timeout incluso) confere no GitHub se o PR já está fundido: aí é sucesso."""
    rodar = rodar or _gh_rodar
    base = ["pr", "merge", str(e["numero"]), "--repo", e["repo"]]
    saida = ""
    for modo in ("--squash", "--merge"):
        try:
            codigo, saida = rodar(base + [modo, "--match-head-commit", sha])
        except (subprocess.TimeoutExpired, OSError) as erro:
            codigo, saida = 124, f"{type(erro).__name__}"
        if codigo == 0:
            return True, ""
        if modo == "--squash" and not re.search(r"squash.*(not allowed|disabled|não)|not allowed.*squash", saida, re.I):
            break
    if _ja_fundido(e, get):
        return True, ""
    return False, " ".join(saida.split())[:200]


def _ja_fundido(e, get=None):
    try:
        pr = (get or gh_api)(f"repos/{e['repo']}/pulls/{e['numero']}")
        return bool(pr and pr.get("merged") and pr.get("merge_commit_sha"))
    except Exception:  # noqa: BLE001
        return False


def _gh_rodar(args):
    from hub import gh
    r = gh.executar(args, "esperas", urgente=args[:2] == ["pr", "merge"], capture_output=True, text=True, encoding="utf-8", timeout=120)
    return r.returncode, (r.stderr or "") + (r.stdout or "")


def registrar_sem_ia(ident, tipo, pr, base_dados=None, agora=None):
    """Uma linha por volta resolvida sem IA, para o relatório de economia."""
    arq = conta.pasta(base_dados) / "sem-ia.jsonl"
    arq.parent.mkdir(parents=True, exist_ok=True)
    linha = {"ident": ident, "tipo": tipo, "quando": conta.iso(agora or conta.agora()), "pr": pr}
    with arq.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def _resumo(c):
    linhas = [ln.strip() for ln in (c.get("body") or "").splitlines()]
    corpo = " ".join(ln for ln in linhas[1:] if ln) or linhas[0]
    quem = RE_REVISAO.search(c["body"]).group(1)
    return f"{quem}: {corpo[:400]}{'…' if len(corpo) > 400 else ''} ({c.get('html_url', '')})"


def _gh_texto(caminho):
    """GET que devolve texto (log de job do Actions); `gh api` segue o redirecionamento. Os testes trocam por um falso."""
    from hub import gh
    r = gh.executar(["api", caminho], "esperas", capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return r.stdout


def _log_do_job(repo, sha, chamar, ler_texto):
    """Fallback: o log do local-ci não abre para o hub; o job vermelho do `ci.yml` neste commit roda a mesma
    suíte. Devolve o texto do log do primeiro job que falhou, ou "" se não achou."""
    runs = chamar(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=10").get("workflow_runs") or []
    for run in runs:
        if run.get("conclusion") != "failure":
            continue
        for job in (chamar(f"repos/{repo}/actions/runs/{run['id']}/jobs").get("jobs") or []):
            if job.get("conclusion") == "failure":
                return ler_texto(f"repos/{repo}/actions/jobs/{job['id']}/logs")
    return ""


RE_BLOCO_LOG = re.compile(r"```\n(.*?)\n?```\s*$", re.S)


def _log_do_comentario(repo, sha, chamar):
    """o local-ci (vps-parceiro) comenta no PR, ou no commit se é a main, `<!-- local-ci-log <sha> -->` com os
    testes que falharam e o fim do log. Devolve (onde, falhas, texto do log) do comentário mais novo deste sha, ou None."""
    marca = f"<!-- local-ci-log {sha} -->"
    fontes = [(f"repos/{repo}/commits/{sha}/comments?per_page=100", f"commit {sha[:8]}")]
    for pr in chamar(f"repos/{repo}/commits/{sha}/pulls?per_page=10") or []:
        fontes.append((f"repos/{repo}/issues/{pr['number']}/comments?per_page=100", f"PR #{pr['number']}"))
    achados = []
    for caminho, onde in fontes:
        with contextlib.suppress(Exception):
            achados += [(c, onde) for c in chamar(caminho) or [] if (c.get("body") or "").startswith(marca)]
    if not achados:
        return None
    c, onde = max(achados, key=lambda a: a[0].get("updated_at") or a[0].get("created_at") or "")
    corpo = c["body"]
    bloco = RE_BLOCO_LOG.search(corpo)
    if not bloco:
        return None
    falhas = []
    if "Testes que falharam:" in corpo:
        for ln in corpo.split("Testes que falharam:", 1)[1].splitlines()[1:]:
            if not ln.startswith("- "):
                break
            falhas.append(ln[2:].strip())
    return f"comentário do local-ci no {onde}", falhas, bloco.group(1)


def trecho_log(sha, repo=None, chamar=None, ler_texto=None):
    """Testes que falharam e as últimas LINHAS_LOG linhas do log do local-ci deste commit, para a fala de volta. Sem
    acesso ao log do runner, tenta o comentário que o local-ci deixa no PR e, depois, o job vermelho do `ci.yml` (o hub abre a sessão JÁ com o erro no prompt)."""
    caminho = LOG_CI.format(sha=sha)
    try:
        texto = RE_ANSI.sub("", Path(caminho).read_text(encoding="utf-8", errors="replace"))
    except OSError as erro:
        if repo:
            with contextlib.suppress(Exception):  # o runner (outra máquina) deixa o fim do log num comentário
                achado = _log_do_comentario(repo, sha, chamar or gh_api)
                if achado:
                    return _formatar_log(achado[2], achado[0], achado[1])
            with contextlib.suppress(Exception):
                texto = RE_ANSI.sub("", _log_do_job(repo, sha, chamar or gh_api, ler_texto or _gh_texto))
                if texto.strip():
                    caminho = f"job do ci.yml de {repo}@{sha[:8]}"
                    return _formatar_log(texto, caminho)
        return (f"\nNão consegui ler o log {caminho} ({erro.strerror or type(erro).__name__}): sem o erro, NÃO "
                "re-dispare às cegas; rode os testes dos arquivos que você mudou e diga no PR que o log não abriu.")
    return _formatar_log(texto, caminho)


def _formatar_log(texto, caminho, falhas=None):
    linhas = texto.splitlines()
    falhas = falhas or list(dict.fromkeys(m.group(1).strip() for ln in linhas if (m := RE_FALHA.match(ln))))
    saida = ""
    if falhas:
        mais = f" (+{len(falhas) - MAX_FALHAS})" if len(falhas) > MAX_FALHAS else ""
        saida += "\nTestes que falharam" + mais + ":\n" + "\n".join(f"- {f}" for f in falhas[:MAX_FALHAS])
    return saida + f"\nÚltimas {LINHAS_LOG} linhas de {caminho}:\n```\n" + "\n".join(linhas[-LINHAS_LOG:]) + "\n```"


def _get_texto(url):
    req = urllib.request.Request(url, headers={"User-Agent": "krohnos-esperas"})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 — URL dada pela tarefa, só leitura
        return r.read().decode("utf-8", "replace")


def _situacao_no_ar(e, chamar, agora, vencida, aviso, get=None, _out=None):
    """Espera do deploy: fala quando o commit está no ar, quando não há como conferir ou quando venceu."""
    from hub import recibo  # import tardio: recibo importa esperas
    try:
        pr = chamar(f"repos/{e['repo']}/pulls/{e['numero']}")
        sha = e.get("no_ar") or pr.get("merge_commit_sha") or ""
        if not (pr.get("merged") or pr.get("merged_at")) and not e.get("no_ar"):
            if pr.get("state") == "closed":
                _causa(_out, "pr_fechado")
                return f"{aviso}PR {e['pr']} foi fechado sem merge: não há o que esperar no ar. Veja por quê e termine."
            if vencida:
                _causa(_out, "pr_sem_merge")
            return f"{aviso}PR {e['pr']} ainda não tem merge: nada para conferir no ar. Faça o merge (ou espere o CI com `voltar-quando --pr`)." if vencida else None
        if e.get("url"):
            corpo = (get or _get_texto)(e["url"])
            prod = (RE_SHA.findall(corpo) or [""])[0]
            ok = recibo._contem(e["repo"], sha, prod, chamar) if prod else None
            nota = f"{e['url']} mostra {prod[:8]}" if prod else f"{recibo.NAO_LEU}: {e['url']} não mostra commit"
        else:
            ok, nota = recibo.no_ar(e["repo"], sha, chamar, base=(pr.get("base") or {}).get("ref"))
    except Exception as erro:  # noqa: BLE001 — rede fora: tenta no próximo tick, menos se venceu
        ok, nota = None, f"{recibo.NAO_LEU} ({type(erro).__name__})"
    if ok:
        if _out is not None:  # no ar confirmado não precisa de IA; o hub fecha a issue
            _out["desfecho"] = {"tipo": "no_ar", "texto": f"No ar: {nota}. Fechado pelo hub, sem IA.", "sha": sha}
        return f"O merge {sha[:8]} do PR {e['pr']} está no ar ({nota}). Confira a página uma vez com curl e termine."
    if ok is None and not nota.startswith(recibo.NAO_LEU):  # sem deploy que o hub confira: esperar não adianta
        _causa(_out, "deploy_nao_confere")
        if _out is not None and sha and ("promoção" in nota or not _tem_site(e["repo"])):
            # develop (nada novo no ar até a promoção) ou repo sem site: não há o que conferir, o merge basta.
            # Com site, um 200 pode ser a página antiga (deploy falho): a IA confere e pede o deploy-app.
            _out["desfecho"] = {"tipo": "no_ar", "sha": sha, "texto": f"Merge {sha[:8]} feito; {nota}. Fechado pelo hub, sem IA."}
        return f"PR {e['pr']}: o hub não sabe conferir o deploy deste repo ({nota}). Confira uma vez com curl; não subiu: `deploy-app` e termine."
    if not vencida:
        return None
    _causa(_out, "deploy_vencido")
    return (f"{aviso}O merge {sha[:8]} do PR {e['pr']} ainda não está no ar ({nota}). Registre no diário "
            "(`hub.consertos registrar`) e termine; no ClienteJ não peça deploy-app, nos outros sites peça uma vez.")


def _causa(out, causa):
    """por que a espera do deploy abre uma sessão (vai no motivo da execução, como a causa da espera do CI)."""
    if out is not None:
        out["causa"] = causa


def _tem_site(repo):
    """o repo publica um site cadastrado? Sem site, o merge basta (como no recibo)."""
    from hub import recibo  # import tardio: recibo importa esperas
    s = recibo._site(repo)
    return bool(s and s.get("dominio"))


def quando_ci(e, ci, sem_ci):
    """De quando se conta a espera pela revisão: da espera (repo sem CI) ou do status do local-ci, o que for mais novo."""
    criada = conta.de_iso(e["criada_em"])
    if sem_ci or not ci:
        return criada
    return max(criada, conta.de_iso(ci.get("updated_at") or ci.get("created_at")))


def situacao(e, chamar=None, agora=None, get=None, _out=None):
    """Fala de volta para a issue, ou None se ainda é para esperar. `_out` (dict): recebe "desfecho" quando a volta
    não precisa de IA (ver `desfecho_sem_ia`); assim a mesma leitura do GitHub serve às duas decisões."""
    chamar, agora = chamar or gh_api, agora or conta.agora()
    vencida = agora - conta.de_iso(e["criada_em"]) >= dt.timedelta(hours=float(e.get("max_horas") or MAX_HORAS))
    aviso = f"Passou de {float(e.get('max_horas') or MAX_HORAS):g} h esperando. " if vencida else ""
    if "no_ar" in e:
        return _situacao_no_ar(e, chamar, agora, vencida, aviso, get, _out)
    try:
        pr = chamar(f"repos/{e['repo']}/pulls/{e['numero']}")
        if pr.get("merged") or pr.get("merged_at"):
            # "falta só conferir no ar" nunca segura a issue (e 594 ficaram horas em In Progress): a
            # espera vira a do deploy e o hub fecha sozinho quando subir (ver `prontas`, que grava a troca)
            if _out is not None:
                _out["fundido"] = True
            return _situacao_no_ar({**e, "no_ar": ""}, chamar, agora, vencida, aviso, get, _out)
        if pr.get("state") == "closed":
            return f"{aviso}PR {e['pr']} foi fechado sem merge. Veja por quê e termine."
        sha = pr["head"]["sha"]
        status = chamar(f"repos/{e['repo']}/commits/{sha}/status")
        ci = next((s for s in status.get("statuses") or [] if s.get("context") == CONTEXTO), None)
        comentarios = comentarios_pr(e["repo"], e["numero"], chamar)
        rev = _revisao(comentarios, sha, conta.de_iso(e["criada_em"]))
    except Exception as erro:  # noqa: BLE001 — rede ou GitHub fora: tenta no próximo tick, menos se venceu
        if vencida:
            return f"{aviso}Não consegui ler o PR {e['pr']} ({type(erro).__name__}). Confira você e termine."
        return None
    if ci and ci.get("state") in ("failure", "error") and _pedida_de_novo(comentarios, ci):
        ci = None  # espera a rodada nova em vez de voltar na hora com o vermelho velho
    sem_ci = not ci and not e.get("com_ci", True)  # repo sem local-ci: só a revisão importa
    if _out is not None and (sem_ci and e.get("com_ci") is False or ci and ci.get("state") == "success") \
            and revisao_limpa(rev, sha):
        _out["desfecho"] = {"tipo": "pronto", "sha": sha, "texto": "local-ci verde" if ci else "repo sem CI"}
    estado = ci["state"] if ci else "pendente"
    saiu = estado in ("success", "failure", "error")
    travado = not ci and not sem_ci and agora - conta.de_iso(e["criada_em"]) >= SEM_STATUS
    if _out is not None and not rev and (sem_ci or travado or estado == "success"):
        _out["mecanica"] = True  # sem revisão e sem vermelho, a volta é rodar os testes e fundir
    if travado and not vencida:
        revisao = _resumo(rev) if rev else "nenhuma no PR"
        return f"PR {e['pr']} (commit {sha[:8]}): nenhum status local-ci em 2 h, revisão: {revisao}. {TRAVADO}"
    if not vencida:
        if not saiu and not sem_ci:
            return None
        quando = quando_ci(e, ci, sem_ci)
        if not rev and agora - quando < espera_revisao(e):
            return None
    nome = "não usado neste repo" if sem_ci else {"success": "verde", "failure": "vermelho",
                                                   "error": "vermelho (erro)"}.get(estado, "ainda sem resultado")
    desc = f" ({ci['description']})" if ci and ci.get("description") else ""
    revisao = _resumo(rev) if rev else "nenhuma no PR"
    log = trecho_log(sha, e["repo"], chamar) if estado in ("failure", "error") else ""
    if _out is not None:  # por que a espera abre uma sessão (vai no motivo da execução e no relatório)
        _out["causa"] = ("vencida" if vencida else "vermelho" if estado in ("failure", "error") else
                         "sem_revisao" if not rev else "revisao_com_achado" if not revisao_limpa(rev, sha) else "mecanica")
    sem_rev = "" if rev or not (saiu or sem_ci) or not quando_ci(e, ci, sem_ci) + REVISAO_TETO <= agora else f" {SEM_REVISAO}"
    return f"{aviso}PR {e['pr']} (commit {sha[:8]}): local-ci {nome}{desc}, revisão: {revisao}. {SEM_CI if sem_ci else FIM}{sem_rev}{log}"


def prontas(chamar=None, base_dados=None, agora=None, pular=(), desfechos=None):
    """[(espera, fala)] das esperas prontas. `pular`: idents com a tarefa na fila ou rodando (ficam para depois).
    Grava `conferida_em` para não perguntar ao GitHub a cada minuto."""
    agora = agora or conta.agora()
    saida = []
    from hub import main_vermelho  # import tardio: evita ciclo
    for e in todas(base_dados):
        if e.get("ident") in pular:
            continue
        mv = main_vermelho.aberto(e.get("repo", ""), base_dados)
        if mv and e.get("ident") != mv["issue"]:
            continue  # main vermelho segura a fila do repo até o conserto (#695)
        ultima = e.get("conferida_em")
        if ultima and agora - conta.de_iso(ultima) < (INTERVALO_NO_AR if "no_ar" in e else INTERVALO):
            continue
        out = {}
        fala = situacao(e, chamar, agora, _out=out)
        if fala:
            if desfechos is not None and out.get("desfecho"):
                desfechos[e["ident"]] = out["desfecho"]
            if (out.get("desfecho") or {}).get("tipo") == "no_ar":
                with contextlib.suppress(Exception):  # (F2): issue→no ar na telemetria
                    from hub import telemetria
                    telemetria.marcar_no_ar(e["pr"], agora, base_dados)
            if SEM_REVISAO in fala:  # mede a recorrência (a retro lista) em vez de só mandar o executor revisar
                with contextlib.suppress(Exception):
                    from hub import incidentes
                    incidentes.registrar(f"PR {e['pr']} sem revisão do Codex 2 h depois do CI ({e['ident']})", "revisao_nunca_chega",
                                         "hub/esperas.py (SEM_REVISAO: o executor lê o diff e segue)", e["ident"],
                                         chave=f"revisao:{e['repo']}#{e['numero']}:{(e.get('sha') or '')[:8]}", base_dados=base_dados, agora=agora)
            if out.get("causa"):
                e = {**e, "causa": out["causa"]}
            if out.get("mecanica"):
                e = {**e, "mecanica": True}
            if out.get("fundido"):
                e = {**e, "no_ar": ""}  # a volta é conferência de deploy (modelo barato), não revisão de CI
            saida.append((e, fala))
            continue
        e["conferida_em"] = conta.iso(agora)
        if out.get("fundido"):
            e["no_ar"] = ""  # PR já fundido: daqui em diante é espera de deploy (intervalo e teto próprios)
            e["max_horas"] = min(max(float(e.get("max_horas") or 0), TETO_HORAS), TETO_NO_AR_HORAS)
        arq = _arq(e["ident"], base_dados)
        atual = ler(e["ident"], base_dados)
        if atual and atual.get("criada_em") == e["criada_em"]:  # a sessão pode ter apagado/trocado no meio
            tmp = arq.with_name(f".{arq.name}.tmp")
            tmp.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(arq)
    return saida
