"""Done só com recibo (princípios 7 e 13 da receita Lunar). Em 14/10 a foi para Done sem os vídeos
(a voz dependia de assinatura) e o dono ficou esperando. Agora, a cada 5 min (tick da conta 1, agenda.puxar_linear),
toda issue que entrou em Done passa pelo recibo:
- todo PR anexado mergeado E no ar (sha em produção: /api/versao do ClienteJ, HEAD do hub/runner, último deploy do
  Dokploy em vps/deploys.json); repo sem deploy conhecido: merged basta;
- o que o "Pronto quando" pede (vídeo, imagem, PDF, planilha, anexo, aviso ao dono) existe;
- URLs de sites cadastrados no aceite e no comentário final abrem em produção.
Sem recibo: depende do dono (proposta pendente, assinatura, pagamento, resposta dele) → "Travada: precisa do dono";
senão volta para In Progress com o motivo e para a fila. Cada caso vira incidente (hub/incidentes.py)."""
import contextlib
import datetime as dt
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hub import config, codigo, conta, data, entrega_visual, esperas, incidentes, linear, plano_itens, portao_done

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TRAVADA = f"Travada: precisa do {config.dono()}"
INTERVALO = dt.timedelta(minutes=5)
DEPLOY_LENTO = dt.timedelta(minutes=20)  # ClienteJ sobe sozinho no merge; o build leva minutos
RE_PRONTO = re.compile(r"\*{0,2}Pronto quando:?\*{0,2}:?\s*(.+?)(?:\n\s*\n|\n#|\n\*\*|\Z)", re.S | re.I)  # para no próximo rótulo em negrito ("**Medir depois:**")
ANEXA = r"anexad\w*|\banex(?:e|ar)\b"


def _perto(a, b):
    return rf"(?:{a})[^.\n]{{0,40}}(?:{b})|(?:{b})[^.\n]{{0,40}}(?:{a})"


RE_ANEXO = re.compile(r"\banexad\w*|\banex(?:e|ar)\b", re.I)
_DONO = re.escape(config.dono())
EXIGE = {
    # vídeo só como entrega; "perfis de exemplo com vídeo" descreve uma função
    "video": re.compile(r"\bmp4\b|v[íi]deos?\s+(?:anexad|final|finais|renderizad)|anex\w+[^.\n]{0,40}v[íi]deo|render final", re.I),
    # imagem/PDF só como entrega, junto de "anexad*/na issue/..." ("a imagem do hero carrega", "gera o PDF no servidor" não)
    "imagem": re.compile(_perto(r"\bprints?\b|screenshots?|\bimagens?\b|\bcapturas?\b", ANEXA + r"|na issue|no linear") +
                         r"|\b(?:png|jpe?g)\b[^.\n]{0,30}anexad", re.I),
    "pdf": re.compile(_perto(r"\bpdf\b", ANEXA + r"|na issue|caminho|mande|envie"), re.I),
    "planilha": re.compile(r"planilha|\bxlsx\b", re.I),
    # aviso = mensagem ao dono; "aviso de mês diferente" descreve a interface
    "aviso": re.compile(rf"\baviso\s+(?:ao|para o|pro)\s+{_DONO}|\bavis(?:ar|e)\s+o\s+{_DONO}|\baviso\s+(?:no|pelo|via)\s+whatsapp|"
                        rf"\bm[ao]nd(?:e|a|ar)\s+ao\s+{_DONO}|whatsapp do {_DONO}", re.I),
}
EXT = {"video": (".mp4", ".mov", ".webm"), "imagem": (".png", ".jpg", ".jpeg", ".gif", ".webp"), "pdf": (".pdf",),
       "planilha": (".xlsx", ".csv", ".ods")}
NOME = {"video": "vídeo", "imagem": "imagem", "pdf": "PDF", "planilha": "planilha", "anexo": "anexo",
        "aviso": f"aviso ao {config.dono()}"}
RE_ARQ = re.compile(r"https?://\S+?\.(mp4|mov|webm|png|jpe?g|gif|webp|pdf|xlsx|csv|ods)\b", re.I)
# "cartão" sozinho é card de UI ("cartão do Conector"); só o de crédito depende do dono (I3)
RE_DONO = re.compile(r"assinar|assinatura|plano pago|pagamento|cart[ãa]o de cr[ée]dito|depende de voc[êe]|depende do "
                      rf"{_DONO}|precisa do {_DONO}|sua resposta|voc[êe] responder|aprova[çc][ãa]o sua", re.I)
RE_FULANO = RE_DONO  # compatibilidade com consumidores anteriores à frente 1
RE_URL = re.compile(r"https?://[^\s)>\]`]+")  # crase fecha código Markdown; não faz parte da URL (INC-25)
DEPLOY_BRANCH = {config.repo("cliente-c"): "main", config.repo("bot_whatsapp"): "master"}  # branch que publica, quando não é a base do site (cliente-c: PR vai para develop)
NAO_LEU = "não consegui conferir o que está no ar"
MAX_TENTATIVAS = 3  # falhas seguidas no recibo de uma issue antes do incidente e do backoff (nunca marca visto)
PRAZO_ESGOTADA = dt.timedelta(days=3)  # erro que não passa (404, bug): depois disso larga a pendência, com incidente, para não prender a janela
BACKOFF_ESGOTADA = dt.timedelta(hours=6)  # depois de esgotar, uma tentativa a cada 6 h até o GitHub voltar
ESPERA_LEITURA = dt.timedelta(hours=6)  # leitura da produção falhando: espera até aqui, depois vira n/a
ESPERA_DEPLOY = dt.timedelta(hours=6)  # execução do workflow com o SHA na fila/rodando: espera até aqui, depois é falta
DEPLOY_CRON = {config.repo("clienteh"): "main"}  # INC-63: coleta/vps-cron.txt, vps-deploy-local.sh
WORKFLOW_DEPLOY = "imagem"  # só o workflow que publica conta; testes/outros no mesmo SHA não seguram o recibo
PENDENTE = ("queued", "in_progress", "waiting", "pending", "requested")
CHECKOUT = {config.repo("repo-privado"): str(codigo()), config.repo("nimbus-hub"): str(codigo()), config.repo("bot_whatsapp"): "/opt/bot/bot_whatsapp"}
# first: 50 basta: a janela é de ~1 h (ultimo - 1 h) ou vai até a pendência mais antiga (no máximo ~6 h)
Q_DONE = ("query($d: DateTimeOrDuration!) { issues(first: 50, filter: {state: {type: {eq: \"completed\"}}, "
          "completedAt: {gt: $d}}) { nodes { id identifier title description createdAt completedAt state { name type } "
          "attachments { nodes { url title } } comments(first: 100) { nodes { createdAt body user { app } } } "
          "team { id states { nodes { id name } } } } } }")
FILA_AVISO = dt.timedelta(minutes=30)  # aviso na fila da caixa há mais que isso: o bot está parado, não conta


def _secao_aceite(descricao):
    # Termina no próximo título de nível igual ou superior; subtítulos e linhas
    # "# ..." dentro de bloco de código ficam na seção.
    dentro, nivel, linhas = False, None, []
    for linha in descricao.splitlines():
        if linha.lstrip().startswith("```"):
            dentro = not dentro
        m = None if dentro else re.match(r"^(#{1,6})\s+(.*)", linha)
        if nivel is None:
            if m and re.match(r"Aceite\b", m.group(2), re.I):
                nivel = len(m.group(1))
            continue
        if m and len(m.group(1)) <= nivel:
            break
        linhas.append(linha)
    return "\n".join(linhas).strip() if nivel else ""


def pronto_quando(descricao):
    m = RE_PRONTO.search(descricao or "")
    if m:
        return m.group(1).strip()
    # INC-85: issues como usam "## Aceite da Frente 1" com vários
    # parágrafos. O fallback do último parágrafo descartava parte do aceite.
    aceite = _secao_aceite(descricao or "")
    if aceite:
        return aceite
    paragrafos = [x.strip() for x in re.split(r"\n\s*\n", descricao or "") if x.strip()]
    return paragrafos[-1] if paragrafos else ""  # sem a seção: só o último parágrafo (a descrição toda dava ruído)


def exige(texto):
    # INC-45: uma proibição de aviso técnico não é uma entrega pendente.
    # Retira só a expressão negada; um aviso afirmativo em outra cláusula vale.
    sem_proibicao = re.sub(
        rf"\b(?:nunca|jamais|não)\s+(?:(?:para o|pro|ao|no|pelo|via)\s+)?whatsapp do {_DONO}\b",
        "", texto or "", flags=re.I)
    ks = {k for k, r in EXIGE.items()
          if r.search(sem_proibicao if k == "aviso" else texto or "")}
    texto = texto or ""
    # "PR anexado à issue" não é entrega de arquivo
    if any(not re.search(r"\bPR\b", texto[max(0, m.start() - 25):m.end() + 25]) for m in RE_ANEXO.finditer(texto)):
        ks.add("anexo")
    return ks - {"anexo"} if ks & set(EXT) else ks  # "PDF anexado" cobra o PDF, não um anexo qualquer


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "krohnos-recibo"})
    with urllib.request.urlopen(req, timeout=20) as r:  # roda dentro da trava da agenda  # noqa: S310 — URL fixa do cadastro
        return json.loads(r.read().decode("utf-8"))


def _contem(repo, merge_sha, prod_sha, gh):
    if not prod_sha:
        return False
    if prod_sha.startswith(merge_sha[:12]):
        return True
    return gh(f"repos/{repo}/compare/{merge_sha}...{prod_sha}").get("status") in ("identical", "ahead")


def _site(repo):
    from hub import sites
    return next((s for s in sites.ativos() if ((s.get("repo") or {}).get("github") or "").lower() == repo.lower()), None)


def _sha_dokploy(s, base_dados=None):
    """(app, sha): app None = repo sem Dokploy; sha "" = o vigia ainda não registrou deploy desse app."""
    app = ((s or {}).get("ids") or {}).get("dokploy_app")
    if not app:
        return None, ""
    with contextlib.suppress(OSError, ValueError, KeyError):
        return app, json.loads((Path(base_dados or data()) / "vps" / "deploys.json").read_text())[app]["sha"]
    return app, ""


def _sha_versao(s, get):
    """Commit servido em https://<domínio>/api/versao (contrato único dos sites, F5: {commit, buildStamp, startedAt}).
    "" se o site não tem a rota ainda, não respondeu ou não traz um SHA: quem chama cai no registro do Dokploy."""
    dominio = (s or {}).get("dominio")
    if not dominio:
        return ""
    with contextlib.suppress(Exception):  # rota ausente (404), site fora do ar ou JSON estranho: sem leitura
        c = str(get(f"https://{dominio}/api/versao").get("commit") or "").strip().lower()
        return c if re.fullmatch(r"[0-9a-f]{40}", c) else ""
    return ""


def no_ar(repo, merge_sha, gh=None, get=None, base_dados=None, base=None):
    """(True|False|None, nota). None: n/a (sem deploy que o hub confira, PR fora da branch que publica) ou leitura da
    produção falhou (nota começa com NAO_LEU: quem chama espera ou ignora, nunca cobra)."""
    gh, get = gh or esperas.gh_api, get or _get_json
    s = _site(repo)
    deploya = DEPLOY_BRANCH.get(repo) or ((s or {}).get("repo") or {}).get("base") or "main"
    if base and base != deploya:  # cliente-c: PR vai para develop; PR empilhado: base é outra branch de feature
        return None, f"no ar: n/a (base {base}; sobe na promoção para {deploya})"
    try:
        if repo == config.repo("clienteh"):
            prod = str(get("https://clientej-com-br.example/api/versao").get("commit") or "")  # 1º acesso pós-deploy leva ~60 s
        elif repo in CHECKOUT:
            r = subprocess.run(["git", "-C", CHECKOUT[repo], "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
            prod = r.stdout.strip() if r.returncode == 0 else ""
        else:
            app, registro = _sha_dokploy(s, base_dados)
            prod = _sha_versao(s, get) or registro  # o que o site serve vale mais que o registro do vigia
            if app is None and not prod:
                return None, "no ar: n/a"
            if not prod:
                return None, f"no ar: n/a (sem registro de deploy do {app})"
        if not prod:
            return None, f"{NAO_LEU}: produção de {repo} não devolveu o commit"
        if _contem(repo, merge_sha, prod, gh):
            return True, f"no ar ({prod[:8]})"
    except Exception as e:  # noqa: BLE001 — leitura falhou (rede, timeout, compare): não é falta
        return None, f"{NAO_LEU}: {repo} ({type(e).__name__}: {str(e)[:100]})"
    return False, f"o merge {merge_sha[:8]} não está no ar em {repo} (produção em {prod[:8]})"


def deploy_pendente(repo, merge_sha, branch, gh=None):
    """("pendente"|"falhou"|"nenhum"|"sem_sucessora", nota). O workflow `imagem` cancela a execução anterior a cada
    merge e a sucessora pode estar na fila do único runner: execução queued/in_progress cujo head_sha contém o merge é
    deploy atrasado, não falta. "falhou": a última que contém o SHA terminou failure/cancelled; "nenhum": nada o contém."""
    if DEPLOY_CRON.get(repo) == branch:
        # O cron pode espaçar trocas por 2 h. Não há execução no Actions a consultar.
        # conferir continua exigindo SHA no ar e cobra a falta após ESPERA_DEPLOY.
        return "pendente", "deploy por cron local; publicação ainda não confirmada em /api/versao"
    gh = gh or esperas.gh_api
    runs = gh(f"repos/{repo}/actions/runs?branch={branch}&per_page=30").get("workflow_runs") or []
    runs = [r for r in runs if WORKFLOW_DEPLOY in f"{r.get('name') or ''} {r.get('path') or ''}".lower()]
    contem = [r for r in runs if r.get("head_sha") and _contem(repo, merge_sha, r["head_sha"], gh)]
    pend = [r for r in contem if r.get("status") in PENDENTE]
    if pend:
        return "pendente", f"execução {pend[0].get('name') or pend[0].get('id')} ({pend[0]['head_sha'][:8]}) {pend[0]['status']}"
    if not contem:
        return "nenhum", f"nenhuma execução do workflow em {branch} contém o merge {merge_sha[:8]} (nenhum deploy disparado)"
    ult = max(contem, key=lambda r: r.get("created_at") or "")
    return "falhou", f"a última execução que contém o merge ({ult['head_sha'][:8]}) terminou {ult.get('conclusion') or ult.get('status')} (deploy falhou)"


def _prs(issue):
    textos = [a.get("url") or "" for a in (issue.get("attachments") or {}).get("nodes") or []]
    vistos, out = set(), []
    for t in textos:
        for repo, n in esperas.RE_PR.findall(t):
            if (repo, n) not in vistos:
                vistos.add((repo, n))
                out.append((repo, int(n)))
    return out


def _do_claude(issue):
    return [c for c in (issue.get("comments") or {}).get("nodes") or [] if (c.get("user") or {}).get("app")]


def _anexos(ident, issue, base_dados=None):
    links = []
    with contextlib.suppress(OSError):
        for l in (Path(base_dados or data()) / "claude" / "anexos" / f"{ident.upper()}.jsonl").read_text().splitlines():
            with contextlib.suppress(ValueError):
                links.append(json.loads(l).get("link") or "")
    links += [m.group(0) for c in _do_claude(issue) for m in RE_ARQ.finditer(c.get("body") or "")]
    return links


def _avisou(ident, base_dados=None, criada_em=None, agora=None):
    """True, False ou "esperar" (aviso ainda na fila da caixa há menos de FILA_AVISO: olha de novo na próxima rodada)."""
    nome, agora, na_fila = f"linear-{ident.lower()}", agora or conta.agora(), False
    # o agente também grava o aviso direto na caixa do bot, que move o enviado para enviados/
    caixa = Path(conta.CAIXA_AVISOS)
    desde = conta.de_iso(criada_em).timestamp() if criada_em else 0
    for arq in [*caixa.glob("*.json"), *(caixa / "enviados").glob("*.json")]:
        with contextlib.suppress(OSError, ValueError):
            a = json.loads(arq.read_text(encoding="utf-8"))
            mtime = arq.stat().st_mtime
            if mtime > desde and (a.get("tarefa") == nome or re.search(rf"\b{re.escape(ident)}\b", a.get("texto") or "", re.I)):
                if arq.parent.name == "enviados":
                    return True
                na_fila = na_fila or agora.timestamp() - mtime < FILA_AVISO.total_seconds()
    with contextlib.suppress(OSError):
        for l in (Path(base_dados or data()) / "claude" / "avisos-enviados.jsonl").read_text().splitlines():
            with contextlib.suppress(ValueError):
                if json.loads(l).get("tarefa") == nome:
                    return True
    return "esperar" if na_fila else False


def _proposta_pendente(ident, base_dados=None):
    nome = f"linear-{ident.lower()}"
    for arq in (Path(base_dados or data()) / "propostas").glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            p = json.loads(arq.read_text())
            if p.get("tarefa") == nome and p.get("estado") == "pendente":
                return True
    return False


# INC-50: entregas de dados precisam de resultados além do merge. Olha o
# pedido inteiro e a conversa: o escopo vigente pode estar em comentário.
RE_DADOS = re.compile(r"gabarito|backfill|reprocess|amostragem|amostra estratificada|"
                      r"rotul(?:ar|adas|agem)|(?:classifica|processa|coleta)[^\n.]{0,60}"
                      r"(?:decisões|decisoes|registros|milhões|milhoes)|precisão por regra", re.I)


def conferir_dados(issue, comentario=None):
    comentarios = [c.get("body") or "" for c in (issue.get("comments") or {}).get("nodes") or []]
    descricao = issue.get("description") or ""
    if not RE_DADOS.search("\n".join([issue.get("title") or "", descricao, *comentarios])):
        return ""
    item = {"titulo": issue.get("title"), "descricao": descricao, "comentarios": comentarios}
    entrega = "\n\n".join(c.get("body") or "" for c in sorted(_do_claude(issue), key=lambda c: c.get("createdAt") or "")[-3:])
    if comentario is not None:
        entrega = comentario
    return linear.conferir(item, entrega, estrito=True)


def conferir(issue, gh=None, get=None, base_dados=None, agora=None, comentario=None):
    """`comentario`: o texto que vai junto do Done (portão de escrita); vale no lugar do último comentário da issue."""
    gh, agora = gh or esperas.gh_api, agora or conta.agora()
    ident, faltas, notas, esperar = issue["identifier"], [], [], False
    for repo, n in portao_done.prs_citados(issue, comentario):
        # INC-39: falha de leitura não prova entrega ausente. vigiar mantém a
        # issue pendente e limita as retentativas com MAX_TENTATIVAS.
        pr = gh(f"repos/{repo}/pulls/{n}")
        if not pr.get("merged"):
            if pr.get("state") != "closed":
                faltas.append(f"o PR {repo}#{n} ainda está aberto (não mergeado)")
            elif not portao_done.fechado_com_motivo(issue, repo, n, comentario):  # PR só fechado exige o "fechado porque…"
                faltas.append(f"o PR {repo}#{n} foi fechado sem merge e nenhum comentário diz \"fechado porque…\"")
            continue
        ok, nota = no_ar(repo, pr.get("merge_commit_sha") or "", gh, get, base_dados, (pr.get("base") or {}).get("ref"))
        idade = agora - conta.de_iso(pr.get("merged_at") or conta.iso(agora))
        if ok is False and idade < DEPLOY_LENTO:
            esperar = True  # deploy ainda subindo: decide na próxima rodada
        elif ok is None and nota.startswith(NAO_LEU) and idade < ESPERA_LEITURA:
            esperar = True  # não deu para ler a produção: tenta de novo na próxima rodada
        elif ok is False:
            motivo = ""
            try:
                estado, motivo = deploy_pendente(repo, pr.get("merge_commit_sha") or "", DEPLOY_BRANCH.get(repo) or (pr.get("base") or {}).get("ref") or "main", gh)
            except Exception as e:  # noqa: BLE001 — sem ler as execuções, vale a regra antiga
                estado = ""
            if estado == "pendente" and idade < ESPERA_DEPLOY:
                esperar = True  # deploy só atrasado (fila do runner), não é falta
            else:
                faltas.append(f"{nota}; {motivo}" if motivo else nota)
        else:
            notas.append(f"{repo}#{n}: {nota}")
    pq = pronto_quando(issue.get("description"))
    anexos = " ".join(_anexos(ident, issue, base_dados)).lower()
    ks = exige(pq)
    # PDF/WhatsApp prometido em qualquer parte da issue; o hub manda no recibo de pronto, então só cobra 1 h depois do Done
    if portao_done.promete_aviso(issue) and issue.get("completedAt") and agora - conta.de_iso(issue["completedAt"]) > DEPLOY_LENTO * 3:
        ks.add("aviso")
    for k in sorted(ks):
        if k == "aviso":
            avisou = _avisou(ident, base_dados, issue.get("createdAt"), agora)
            if avisou == "esperar":
                esperar = True
            elif not avisou:
                faltas.append(f"o aviso ao {config.dono()} que o \"Pronto quando\" pede não saiu")
        elif k == "anexo":
            if not anexos.strip():
                faltas.append("nenhum anexo, e o \"Pronto quando\" pede")
        elif not any(e in anexos for e in EXT[k]):
            faltas.append(f"falta o {NOME[k]} anexado que o \"Pronto quando\" pede")
    if entrega_visual.exige_olhei(issue.get("title"), pq, issue.get("completedAt")):  # visual só com evidência
        faltas += entrega_visual.faltas(entrega_visual.ler(ident, base_dados), issue.get("title"), pq)
    from hub import sites
    dominios = {s["dominio"] for s in sites.ativos() if s.get("dominio")}
    comentarios = sorted(_do_claude(issue), key=lambda c: c.get("createdAt") or "")
    ultimo = comentario if comentario is not None else (comentarios or [{}])[-1].get("body") or ""
    # INC-60: o SHA publicado não prova que a rota funciona; confira também
    # as URLs entregues no comentário final, mesmo sem seção "Pronto quando".
    dominios = {d.lower().strip().rstrip("/") for d in dominios}
    de_pq = set(RE_URL.findall(pq))
    for url in dict.fromkeys(RE_URL.findall(pq + "\n" + ultimo)):
        url = url.rstrip(".,;:!?")  # pontuação colada no fim da frase não é da URL
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if any(host == d or host.endswith("." + d) for d in dominios):
            try:  # UA de navegador: o firewall barra o do urllib
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20):  # noqa: S310
                    pass
            except urllib.error.HTTPError as e:
                # "Pronto quando" é aceite: qualquer >= 400. No comentário final só 404/410 e 5xx
                # (login 401/403 e rota só POST 405 são citações legítimas).
                if e.code in (404, 410) or e.code >= 500 or (e.code >= 400 and url in de_pq):
                    faltas.append(f"{url} respondeu {e.code}")
            except Exception as e:  # noqa: BLE001 — o hub não alcançar o domínio não prova rota quebrada; sem espera sem teto
                notas.append(f"{url} não abriu ({type(e).__name__}); não conferida")
    texto_faltas, delegadas = portao_done.faltas_do_texto(issue, comentario)
    faltas += texto_faltas
    # cada `check:` da descrição precisa de saída PASSOU no comentário final (ou nos comentários do Claude)
    faltas += plano_itens.faltas_checks(issue.get("description"), [c.get("body") or "" for c in comentarios] + ([comentario] if comentario is not None else []))
    if not faltas and not esperar:
        try:
            falta_dados = conferir_dados(issue, comentario)
        except Exception as e:  # noqa: BLE001 — modelo fora ou resposta ruim: não aprova nem derruba o vigia; tenta de novo
            notas.append(f"conferência de dados indisponível ({type(e).__name__}); nova tentativa")
            esperar = True
            falta_dados = ""
        if falta_dados:
            faltas.append(falta_dados)
    depende = bool(faltas) and (_proposta_pendente(ident, base_dados) or bool(RE_DONO.search(ultimo)))
    return {"ok": not faltas and not esperar, "esperar": esperar and not faltas, "faltas": faltas,
            "depende_fulano": depende, "notas": notas, "delegadas": delegadas}


def estado_travada(issue, consulta=None):
    consulta = consulta or linear.gql
    for s in issue["team"]["states"]["nodes"]:
        if s["name"] == TRAVADA or s["name"].startswith("Travada: precisa do "):
            return s["id"]
    return consulta("mutation($i: WorkflowStateCreateInput!) { workflowStateCreate(input: $i) { workflowState { id } } }",
                    i={"teamId": issue["team"]["id"], "name": TRAVADA, "type": "started", "color": "#d97706",
                       "description": f"Sem recibo de pronto e depende do {config.dono()} (KRN-733)"})["workflowStateCreate"]["workflowState"]["id"]


def _estado_arq(base_dados=None):
    return conta.pasta(base_dados) / "recibos.json"


def vigiar(consulta=None, gh=None, get=None, mover_fn=None, comentar_fn=None, reabrir_fn=None, base_dados=None,
           agora=None, log=print, ocupadas=()):
    """Confere o recibo das issues que entraram em Done desde a última rodada. Devolve as tarefas para a fila.
    `ocupadas`: nomes na fila ou rodando; essas ficam pendentes para a próxima rodada (a sessão ainda pode entregar)."""
    consulta, agora = consulta or linear.gql, agora or conta.agora()
    mover_fn, comentar_fn, reabrir_fn = mover_fn or linear.mover, comentar_fn or linear.comentar, reabrir_fn or linear.reabrir
    arq = _estado_arq(base_dados)
    try:
        est = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        est = {}
    if not est.get("desde"):  # 1ª rodada: só marca; nada concluído antes do deploy volta
        est = {"desde": conta.iso(agora), "ultimo": conta.iso(agora), "vistos": {}}
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps(est), encoding="utf-8")
        return []
    if agora - conta.de_iso(est.get("rodou") or est["ultimo"]) < INTERVALO:
        return []
    est["rodou"] = conta.iso(agora)  # cadência separada da janela: issue pendente não faz rodar a cada tick

    def salvar():
        arq.write_text(json.dumps(est), encoding="utf-8")
    salvar()
    tentativas, pendentes = est.setdefault("tentativas", {}), est.setdefault("pendentes", {})
    esgotadas = est.setdefault("esgotadas", {})  # ident -> última tentativa depois de esgotar
    # a janela alcança a pendência mais antiga (espera de deploy/leitura, falha); sem pendência, ultimo - 1 h
    inicio = min([conta.de_iso(est["ultimo"]) - dt.timedelta(hours=1)] +
                 [conta.de_iso(c) - dt.timedelta(minutes=1) for c in pendentes.values()])
    voltar, novas = [], {}

    def vista(ident, concluida):
        est["vistos"][ident] = concluida
        tentativas.pop(ident, None)
        esgotadas.pop(ident, None)
        salvar()
    for i in consulta(Q_DONE, d=conta.iso(max(conta.de_iso(est["desde"]), inicio)))["issues"]["nodes"]:
        ident = i["identifier"]
        if est["vistos"].get(ident) == i["completedAt"] or conta.de_iso(i["completedAt"]) <= conta.de_iso(est["desde"]):
            continue
        if f"linear-{ident.lower()}" in ocupadas:
            novas[ident] = i["completedAt"]
            continue
        if pendentes.get(ident) not in (None, i["completedAt"]):  # saiu do Done e voltou: conclusão nova, contagem nova
            tentativas.pop(ident, None)
            esgotadas.pop(ident, None)
        if ident in esgotadas and agora - conta.de_iso(i["completedAt"]) > PRAZO_ESGOTADA:
            log(f"{ident}: recibo sem leitura há mais de {PRAZO_ESGOTADA.days} dias; largo a pendência (incidente já registrado)")
            vista(ident, i["completedAt"])
            continue
        if ident in esgotadas and agora - conta.de_iso(esgotadas[ident]) < BACKOFF_ESGOTADA:
            novas[ident] = i["completedAt"]  # esgotada: espera o backoff, segue pendente
            continue
        try:  # uma falha no meio não repete o que já foi feito nas outras issues (vistos é salvo a cada issue)
            r = conferir(i, gh, get, base_dados, agora)
            if r.get("esperar"):  # conferir limita a espera (20 min de deploy, 6 h de leitura)
                novas[ident] = i["completedAt"]
                continue
            if r["ok"]:
                portao_done._delegar(i, r, base_dados)  # pendência passada ao dono entra na lista "Pendências do dono"
                vista(ident, i["completedAt"])
                continue
            faltas = "; ".join(r["faltas"])
            if r["depende_fulano"]:
                mover_fn(i["id"], estado_travada(i, consulta))
                comentar_fn(i["id"], f"Voltou de Done: sem recibo de pronto ({faltas}). Isso depende de você: "
                                     f"{((_do_claude(i) or [{}])[-1].get('body') or '')[:400]}\n\n_{config.get('produto.nome')}, recibo de pronto (KRN-733)._")
            else:
                mover_fn(i["id"], {s["name"]: s["id"] for s in i["team"]["states"]["nodes"]}["In Progress"])
                comentar_fn(i["id"], f"Voltou de Done: sem recibo de pronto ({faltas}). Volto para fazer.\n\n"
                                     f"_{config.get('produto.nome')}, recibo de pronto (KRN-733)._")
                try:
                    voltar.append(reabrir_fn(ident, f"{config.get('produto.nome')} (recibo de pronto): a issue foi para Done sem {faltas}. "
                                                    f"Faça o que falta agora e prove (PR no ar, anexo, aviso)."))
                except Exception as e:  # noqa: BLE001 — o comentário já avisou; sem fila, o dono vê pelo estado
                    log(f"{ident}: reabrir falhou ({type(e).__name__}: {str(e)[:150]})")
            vista(ident, i["completedAt"])  # antes do incidente: falha nele não repete o comentário
            log(f"{ident}: Done sem recibo ({faltas}) → {'Travada' if r['depende_fulano'] else 'In Progress'}")
            try:
                incidentes.registrar(f"{ident} foi para Done sem recibo: {faltas}"[:500], "pronto_sem_recibo",
                                     "hub/recibo.py (Done só com recibo)", issue=ident,
                                     chave=f"recibo:{ident}:{i['completedAt']}", base_dados=base_dados, agora=agora)
            except Exception as e:  # noqa: BLE001
                log(f"{ident}: incidente não registrado ({type(e).__name__}: {str(e)[:150]})")
        except Exception as e:  # noqa: BLE001 — tenta de novo nas próximas rodadas, até MAX_TENTATIVAS
            tentativas[ident] = tentativas.get(ident, 0) + 1
            log(f"{ident}: recibo falhou ({type(e).__name__}: {str(e)[:150]}), tentativa {tentativas[ident]}")
            if tentativas[ident] >= MAX_TENTATIVAS:
                primeira = ident not in esgotadas
                esgotadas[ident] = conta.iso(agora)
                # falha de leitura não é recibo conferido: segue pendente, com incidente
                try:
                    if not primeira:
                        raise LookupError("incidente já registrado nesta conclusão")  # uma chave por ident+completedAt: as rodadas seguintes não repetem o incidente
                    incidentes.registrar(f"{ident}: recibo não conferido, {tentativas[ident]} falhas lendo "
                                         f"({type(e).__name__}: {str(e)[:150]})"[:500], "recibo_leitura_falhou",
                                         "hub/recibo.py (vigiar)", issue=ident,
                                         chave=f"recibo-leitura:{ident}:{i['completedAt']}", base_dados=base_dados, agora=agora)
                except LookupError:
                    pass
                except Exception as e2:  # noqa: BLE001
                    log(f"{ident}: incidente não registrado ({type(e2).__name__}: {str(e2)[:150]})")
            novas[ident] = i["completedAt"]  # nunca marca visto aqui: a próxima rodada tenta de novo
    for d in (tentativas, esgotadas):  # issue que saiu da consulta não deixa contagem para trás
        for k in [k for k in d if k not in novas]:
            d.pop(k)
    est["pendentes"], est["ultimo"] = novas, conta.iso(agora)
    salvar()
    return voltar
