"""Tarefa parada tem alarme e orçamento; esperar CI não gasta sessão (princípio 10 da receita Lunar).

A cada 5 min (tick da conta 1, agenda.puxar_linear):
- sessão rodando que repete consulta ao CI (3 de 8 últimos comandos: gh pr checks, statuses, sleep longo) → para a
  sessão e grava a espera (voltar-quando): ela acorda no status, sem gastar sessão (#682 ficou horas assim);
- sessão rodando sem sinal há 45 min (evento no stream, commit ou arquivo novo na worktree/saída) → para e volta para
  a fila UMA vez, com o motivo no prompt (o retry muda a condição);
- issue In Progress sem nada rodando, na fila, em espera ou proposta, quieta há 45 min → volta para a fila UMA vez
  (4 h parada depois de "o agente está rodando, aguardo");
- esperando o dono (RE_DONO no último comentário do Claude), ou a 2ª parada da mesma issue em 24 h → escala UMA vez
  ("Travada: precisa do dono" + comentário). Proposta pendente é a vez do orquestrador: não mexe.
Teto de tempo: linear.TIMEOUT; de tentativas: agenda.em_loop; de consumo: agenda.TETO_GASTO_USD (custo equivalente de API, só como medida do consumo)."""
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

from hub import config, conta, esperas, incidentes, linear, retomada

SILENCIO = dt.timedelta(minutes=45)
JANELA = dt.timedelta(hours=24)
INTERVALO = dt.timedelta(minutes=5)
# só comando que ESPERA o CI conta: depurar CI vermelho (gh run list/view --log-failed) é trabalho, não espera
RE_LONGO = re.compile(r"gh pr checks .*--watch|gh run watch|\buntil\b.*\bgh\b|\bwhile\b.*\bgh\b.*\bsleep\b|"
                      r"\bsleep\s+([3-9]\d{2}|\d{4,})", re.I)
RE_POLL = re.compile(r"gh pr checks|" + RE_LONGO.pattern, re.I)
RE_SUJEITO = re.compile(r"\b(agentes?|subagentes?|scripts?|jobs?|renders?|processos?)\b", re.I)
RE_RODANDO = re.compile(r"\brodando\b|segundo plano", re.I)
RE_AGUARDO = re.compile(r"\baguardo\b(?!\s+(a\s+)?(sua|seu|resposta|aprova|ok\b|voc[êe]))", re.I)
RE_QUANDO = re.compile(r"quando terminar,? (eu )?|assim que (sair|terminar)", re.I)
IGNORA = {"node_modules", ".git", ".next", "dist", "build", ".cache", "__pycache__", ".venv", "venv", "target", "coverage",
          ".turbo"}
MAX_ARQUIVOS = 20000
Q_EM_ANDAMENTO = ("query($d: DateTimeOrDuration!) { issues(first: 50, filter: {state: {name: {eq: \"In Progress\"}}, "
                  "updatedAt: {lt: $d}}) { nodes { id identifier updatedAt comments(last: 5) { nodes { createdAt body "
                  "user { app } } } team { id states { nodes { id name } } } } } }")
Q_TIME = "query($i: String!) { issue(id: $i) { team { id states { nodes { id name } } } } }"
MUDOU = ("nada roda depois que a sessão termina: termine o trabalho em primeiro plano nesta sessão, e para esperar CI "
         "use `voltar-quando` (nunca `sleep` nem laço consultando o PR)")


def _pasta(base_dados=None):
    return conta.pasta(base_dados) / "parada"


def comandos(log_path, n=8):
    cmds = []
    with contextlib.suppress(OSError):
        for linha in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                for b in (json.loads(linha).get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use" and b.get("name") == "Bash":
                        cmds.append(str((b.get("input") or {}).get("command") or ""))
    return cmds[-n:]


def _mtime_max(raiz):
    mais, vistos = 0.0, 0
    for dirpath, dirs, files in os.walk(raiz):
        for d in dirs:  # I4: podado conta pelo mtime do próprio diretório (npm ci, next build criam entradas nele)
            if d in IGNORA:
                with contextlib.suppress(OSError):
                    mais = max(mais, os.stat(os.path.join(dirpath, d)).st_mtime)
        dirs[:] = [d for d in dirs if d not in IGNORA]
        for f in files:
            vistos += 1
            with contextlib.suppress(OSError):
                mais = max(mais, os.stat(os.path.join(dirpath, f)).st_mtime)
            if vistos >= MAX_ARQUIVOS:
                return mais
    return mais


def _commit_max(raiz):
    mais = 0.0
    for wt in Path(raiz).glob("*"):
        with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
            r = subprocess.run(["git", "-C", str(wt), "log", "-1", "--format=%ct"], capture_output=True, text=True,
                               timeout=30)
            mais = max(mais, float(r.stdout.strip() or 0))
    return mais


def ultimo_sinal(reg, trabalho=None):
    from hub import agenda
    trabalho = Path(trabalho or agenda.TRABALHO) / reg["nome"]
    tempos = []
    with contextlib.suppress(OSError):
        tempos.append(Path(reg["log"]).stat().st_mtime)
        tempos.append(_mtime_max(Path(reg["log"]).parent))  # ~/saida/<nome>: relatório, arquivos gerados
    if trabalho.exists():
        tempos += [_mtime_max(trabalho), _commit_max(trabalho)]
    t = max(tempos, default=0)
    return dt.datetime.fromtimestamp(t, dt.timezone.utc) if t else None


def classificar_rodando(reg, agora, trabalho=None):
    cmds = comandos(reg.get("log") or "")
    polls = []  # só a sequência no FIM conta: consulta antiga seguida de trabalho não é espera (Codex, 104569a)
    for c in reversed(cmds):
        if not RE_POLL.search(c):
            break
        polls.append(c)
    # I5: sem `gh` em nenhum, é só sleep (render, retry): não é espera de CI
    if len(polls) >= 3 and any(RE_LONGO.search(c) for c in polls) and any(re.search(r"\bgh\b", c) for c in polls):
        prs = [m.group(0) for c in cmds for m in esperas.RE_PR.finditer(c)]
        return ("esperando_ci", f"https://{prs[-1]}" if prs else "")
    with contextlib.suppress(OSError):  # stream andando: sem varrer worktree nem chamar git (custo)
        if agora - dt.datetime.fromtimestamp(Path(reg["log"]).stat().st_mtime, dt.timezone.utc) < SILENCIO:
            return None
    sinal = ultimo_sinal(reg, trabalho)
    if sinal and agora - sinal >= SILENCIO and agora - conta.de_iso(reg["inicio"]) >= SILENCIO:
        return ("travou", f"sem sinal desde {sinal:%H:%M} UTC (último comando: {(cmds or ['?'])[-1][:120]})")
    return None


def promessa_sem_gancho(texto):
    """"o agente está rodando... aguardo". Só vale agente/script/job rodando SEM URL na frase (app no ar não
    é promessa) e "aguardo" sem pedir algo ao dono (isso é entrega)."""
    from hub import recibo
    if not texto or recibo.RE_DONO.search(texto):
        return False
    for frase in re.split(r"[.!?\n]+", texto):
        if RE_SUJEITO.search(frase) and RE_RODANDO.search(frase) and "http" not in frase:
            return True
    return bool(RE_AGUARDO.search(texto) or RE_QUANDO.search(texto))


def _arq_pedido(nome, base_dados=None):
    return conta.pasta(base_dados) / "parar" / f"{nome}.json"


def pedido_de_parada(nome, base_dados=None, consumir=False):
    arq = _arq_pedido(nome, base_dados)
    try:
        p = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if consumir:
        arq.unlink(missing_ok=True)
    return p


def _systemctl(*a):
    # C1: o cron não traz XDG_RUNTIME_DIR; sem ele o systemctl --user não acha o barramento (como agenda._rodar_cli)
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        return subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60, check=False,
                              env=env).returncode == 0
    return False


def parar(nome, acao, motivo, base_dados=None, systemctl=None, inicio=None):
    """Grava o pedido (o executar da sessão lê no ramo `interrompida`; `inicio` amarra o pedido a esta execução) e derruba o scope da tarefa."""
    arq = _arq_pedido(nome, base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps({"acao": acao, "motivo": motivo, "inicio": inicio}, ensure_ascii=False), encoding="utf-8")
    limpo = re.sub(r"[^A-Za-z0-9_-]+", "-", nome).strip("-")[:60]
    parou = (systemctl or _systemctl)("stop", f"tarefa-{limpo}-*.scope") is not False
    arq.write_text(json.dumps({"acao": acao, "motivo": motivo, "inicio": inicio, "parado": parou},
                              ensure_ascii=False), encoding="utf-8")  # stop que falhou é retentado no próximo tick
    return parou


def _estado(ident, base_dados, agora):
    arq = _pasta(base_dados) / f"{ident}.json"
    try:
        e = json.loads(arq.read_text(encoding="utf-8"))
        if agora - conta.de_iso(e["desde"]) < JANELA:
            return arq, e
    except (OSError, ValueError, KeyError):
        pass
    return arq, {"tentativas": 0, "escalada_em": None, "desde": conta.iso(agora)}


def _gravar(arq, e):
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")


def tratar_morta(ident, motivo, base_dados=None, agora=None, log=print):
    """(lição 4): execução que morreu sem o alarme (sinal 143/137, timeout, interrompida): a 1ª em 24 h devolve
    "reenfileirar" (quem chama põe na fila com o resumo da última rodada); a 2ª devolve "escalar" e chama o orquestrador
    uma vez, com o incidente. Nada fica "em andamento sem nada rodando" esperando alguém destravar à mão."""
    agora = agora or conta.agora()
    arq, est = _estado(ident, base_dados, agora)
    est["mortas"] = est.get("mortas", 0) + 1
    if est["mortas"] == 1:
        _gravar(arq, est)
        _incidente(ident, f"{ident} morreu ({motivo}); reenfileirada sozinha", "hub/parada.py (tratar_morta)",
                   f"morta:{ident}:{agora:%Y%m%d%H}", base_dados, agora, log)
        return "reenfileirar"
    if not est.get("morta_escalada_em"):
        est["morta_escalada_em"] = conta.iso(agora)
        _gravar(arq, est)
        from hub import travadas
        travadas.chamar_sem_falhar(ident, f"morreu {est['mortas']}× em 24 h ({motivo})", "execução morta", base_dados, log)
        _incidente(ident, f"{ident} morreu de novo ({motivo}); orquestrador chamado", "hub/parada.py (tratar_morta)",
                   f"morta2:{ident}:{agora:%Y%m%d}", base_dados, agora, log)
    else:
        _gravar(arq, est)
    return "escalar"


def _escalar(alvo, ident, motivo, consulta, log):
    """alvo: issue do Linear (com team) ou item da fila (com estados). Move para "Travada" quando dá; sempre comenta."""
    from hub import recibo
    with contextlib.suppress(Exception):  # X1: item da fila sem o estado no time → busca o time e cria, como o recibo
        estado = (alvo.get("estados") or {}).get(recibo.TRAVADA) or recibo.estado_travada(
            alvo if alvo.get("team") else consulta(Q_TIME, i=alvo["id"])["issue"], consulta)
        linear.mover(alvo["id"], estado)
    try:  # o comentário é o aviso: se falhou, não conta como escalada e o próximo tick tenta de novo
        linear.comentar(alvo["id"], f"Parei esta issue: {motivo}. Preciso de você para seguir.\n\n"
                                    f"_{config.get('produto.nome')}, alarme de tarefa parada (KRN-733)._")
    except Exception as e:  # noqa: BLE001
        log(f"{ident}: escalada falhou ({type(e).__name__}: {str(e)[:150]}); tento no próximo tick")
        return False
    log(f"{ident}: escalada ao {config.dono()} ({motivo})")
    return True


def _incidente(ident, falha, peca, chave, base_dados, agora, log):
    try:
        incidentes.registrar(falha[:500], "tarefa_parada", peca, ident, chave=chave, base_dados=base_dados, agora=agora)
    except Exception as e:  # noqa: BLE001 — o incidente nunca derruba o alarme
        log(f"{ident}: incidente não registrado ({type(e).__name__}: {str(e)[:150]})")


def vigiar(base_dados=None, agora=None, consulta=None, systemctl=None, trabalho=None, log=print):
    from hub import agenda, recibo
    agora, consulta = agora or conta.agora(), consulta or linear.gql
    voltar = []
    rodando = list(agenda.rodando_todas(base_dados).values())
    for reg in rodando:
        item = reg.get("linear") or {}
        ident = item.get("identifier")
        if not ident or not reg.get("log"):
            continue
        classe = classificar_rodando(reg, agora, trabalho)
        if not classe:
            continue
        tipo, detalhe = classe
        arq, est = _estado(ident, base_dados, agora)
        # já pedida para esta execução: nem regrava a espera nem conta 2ª parada; stop que falhou é refeito
        pedido = pedido_de_parada(reg["nome"], base_dados) or {}
        if pedido.get("inicio") == reg["inicio"]:
            if pedido.get("parado") is False and not parar(reg["nome"], pedido["acao"], pedido["motivo"], base_dados,
                                                           systemctl, reg["inicio"]):
                log(f"{ident}: systemctl stop falhou de novo; tento no próximo tick")
            continue
        if tipo == "esperando_ci" and detalhe:
            esperas.gravar(ident, detalhe, base_dados=base_dados)
            if not parar(reg["nome"], "esperar_ci", f"consultava o CI dentro da sessão ({detalhe})", base_dados,
                         systemctl, reg["inicio"]):
                log(f"{ident}: systemctl stop falhou; o próximo tick refaz o stop")
            with contextlib.suppress(Exception):
                if item.get("sessao"):
                    linear.atividade(item["sessao"], "response", "Parei a sessão: ela estava esperando o CI dentro da "
                                     "sessão. Volto sozinho quando o local-ci sair e a revisão chegar.")
            try:
                incidentes.registrar(f"{ident} esperava o CI dentro da sessão ({detalhe})", "espera_ci_na_sessao",
                                     "hub/parada.py (para e grava voltar-quando)", ident,
                                     chave=f"ci:{ident}:{agora:%Y%m%d}", base_dados=base_dados, agora=agora)
            except Exception as e:  # noqa: BLE001
                log(f"{ident}: incidente não registrado ({type(e).__name__}: {str(e)[:150]})")
            continue
        if est["escalada_em"]:
            continue  # já escalou: uma vez só
        est["tentativas"] += 1
        motivo = detalhe if tipo == "travou" else "repetia consultas ao CI dentro da sessão (sem URL de PR nos comandos)"
        if est["tentativas"] == 1:
            parou = parar(reg["nome"], "reenfileirar", f"{config.get('produto.nome')}: a execução anterior foi parada pelo alarme ({motivo}); "
                          f"{MUDOU}.", base_dados, systemctl, reg["inicio"])
        else:
            parou = parar(reg["nome"], "nada", motivo, base_dados, systemctl, reg["inicio"])
            if _escalar(item, ident, f"parou 2× em 24 h ({motivo})", consulta, log):
                est["escalada_em"] = conta.iso(agora)
        if not parou:
            log(f"{ident}: systemctl stop falhou; o próximo tick refaz o stop")
        _gravar(arq, est)
        _incidente(ident, f"{ident} parada: {motivo}", "hub/parada.py (alarme de 45 min)",
                   f"parada:{ident}:{agora:%Y%m%d}", base_dados, agora, log)
    ocupadas = {r["nome"] for r in rodando} | {i["nome"] for i in agenda.fila(base_dados)}
    try:
        issues = consulta(Q_EM_ANDAMENTO, d=f"-PT{int(SILENCIO.total_seconds() // 60)}M")["issues"]["nodes"]
    except Exception as e:  # noqa: BLE001
        log(f"parada: Linear: {type(e).__name__}")
        issues = []
    for i in issues:
        ident = i["identifier"]
        # proposta pendente é a vez do orquestrador/dono: sem escalada aqui
        if f"linear-{ident.lower()}" in ocupadas or esperas.ler(ident, base_dados) \
                or recibo._proposta_pendente(ident, base_dados):
            continue
        do_claude = recibo._do_claude(i)
        if not do_claude:
            continue  # issue que o hub não tocou: não é dele
        arq, est = _estado(ident, base_dados, agora)
        if est["escalada_em"]:
            continue
        ultimo = do_claude[-1]["body"] or ""
        if recibo.RE_DONO.search(ultimo):
            if _escalar(i, ident, "esperando você (decisão sua pendente)", consulta, log):
                est["escalada_em"] = conta.iso(agora)
        elif est["tentativas"] == 0:
            if est.get("recusa_em") and agora - conta.de_iso(est["recusa_em"]) < agenda.FREIO_ORQ_ESPERA:
                continue  # já recusada e avisada: sem nova consulta ao Linear nem novo chamado
            try:
                item = linear.item_por_ident(ident)
            except Exception as e:
                log(f"{ident}: não leu o contexto do orçamento ({type(e).__name__}); tento no próximo tick")
                continue
            recusa = agenda.em_loop(f"linear-{ident.lower()}", base_dados,
                                    ultima_fala=item.get("ultima_fala"), estado_desde=item.get("estado_desde"))
            if recusa:  # orçamento vale aqui também; recusa não gasta a tentativa
                agenda._chamar_orq_uma_vez(ident, recusa, "parada", base_dados)
                est["recusa_em"] = conta.iso(agora)
                _gravar(arq, est)
                log(f"{ident}: parada, mas a issue {recusa}; não volta")
                continue
            try:  # o último texto cita um PR: o hub grava o `voltar-quando` por código, sem abrir sessão
                if retomada.gancho_mecanico(ident, ultimo, base_dados=base_dados, agora=agora):
                    esperas.registrar_sem_ia(ident, "parada", "", base_dados, agora)
                    log(f"{ident}: parada, mas o PR citado ganhou a espera por código; sem sessão")
                    continue
            except Exception as e:  # noqa: BLE001 — sem ler o PR: caminho de sempre (sessão)
                log(f"{ident}: gancho mecânico falhou ({type(e).__name__}: {str(e)[:100]}); volta com sessão")
            try:  # I7: só gasta a tentativa se a tarefa voltou de fato
                voltar.append(linear.reabrir(ident, f"{config.get('produto.nome')}: esta issue ficou {SILENCIO.seconds // 60}+ min em "
                                                    f"andamento sem nada rodando. Última fala sua: «{ultimo[:300]}». "
                                                    f"Atenção: {MUDOU}."))
            except Exception as e:  # noqa: BLE001
                log(f"{ident}: reabrir falhou ({type(e).__name__}: {str(e)[:150]}); tento no próximo tick")
                continue
            est["tentativas"] = 1
        else:
            if _escalar(i, ident, "ficou parada de novo depois de voltar para a fila", consulta, log):
                est["escalada_em"] = conta.iso(agora)
        _gravar(arq, est)
        _incidente(ident, f"{ident} em andamento sem nada rodando", "hub/parada.py (alarme de 45 min)",
                   f"parada:{ident}:{agora:%Y%m%d}", base_dados, agora, log)
    return voltar


def vigiar_no_tick(base_dados=None, agora=None, log=print):
    agora = agora or conta.agora()
    marca = _pasta(base_dados) / "_ultimo"
    with contextlib.suppress(OSError, ValueError):
        if agora - conta.de_iso(marca.read_text().strip()) < INTERVALO:
            return []
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(conta.iso(agora))
    return vigiar(base_dados=base_dados, agora=agora, log=log)
