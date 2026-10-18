"""A conta Claude da VPS (usuário `claude`, Max dedicada): trava única, registro de uso e pausa por limite.

Tudo que chama `claude` na VPS (tarefas do hub e o runner do agente do WhatsApp) segue três regras:
1. Um trabalho por vez: segura a trava exclusiva (flock) em /var/lock/claude-conta.lock durante a chamada.
   Do shell: `flock -w 3600 /var/lock/claude-conta.lock claude -p ...`.
2. Registra cada chamada em $HUB_DATA/claude/uso.jsonl (uma linha JSON; campos em `registrar`), com o que vem do
   `claude -p --output-format json` (total_cost_usd, usage, num_turns, duration_ms).
3. Respeita a pausa: com $HUB_DATA/claude/pausa.json no futuro ({"ate": ISO}), não chama o Claude. Quem vê a CLI
   responder limite de uso grava a pausa até o reset (`pausar`), e só o primeiro avisa no WhatsApp.

    python -m hub.conta uso [--dias 7] [--avisar]   # quanto da cota virou o quê
    python -m hub.conta pausa                       # mostra a pausa ativa
    python -m hub.conta retomar                     # apaga a pausa (depois de conferir que a cota voltou)

Várias contas: CLAUDE_CONTA=N (padrão 1) escolhe a conta do processo. A 1 é o usuário claude com o login de sempre
(~/.claude, trava claude-conta.lock, pausa.json); a N>1 usa CLAUDE_CONFIG_DIR=~/.claude-N (login próprio) com trava
claude-conta-N.lock e pausa-N.json. O agente do WhatsApp e o hub/claude.py começam na 1 e, no limite, passam para a
próxima conta e por fim para a reserva de API.
    echo "<mensagem da CLI>" | python -m hub.conta limite --origem agente   # pausa até o reset; imprime o horário
"""
import argparse
import calendar
import contextlib
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from hub import config, data, home_agentes
from hub.comum.datas import BRT
from hub.comum import caixa as comum_caixa

UTC = dt.timezone.utc
TRAVA = os.environ.get("CLAUDE_CONTA_LOCK", "/var/lock/claude-conta.lock")


def contas_config():
    """Contas que o hub conhece (agentes.contas); sem toml nem env, o legado da VPS (4 contas, faixas no hub.json)."""
    return tuple(map(str, config.definido("agentes.contas") or ("1", "2", "3", "4")))


# Conta sem login (~/.claude-N/.credentials.json) fica parada (ver com_login). CONTAS é só a foto do import (legado):
# quem decide por tick lê contas_config(), assim uma conta adicionada pela tela vale sem reiniciar.
CONTAS = contas_config()


def com_login(home=None):
    """As contas de CONTAS que têm login (~/.claude-N/.credentials.json); a 1 sempre. É o que painel, uso e esteira
    mostram: a 4 aparece sozinha quando ganha login."""
    home = Path(home or home_agentes())
    return tuple(n for n in contas_config() if n == "1" or (home / f".claude-{n}" / ".credentials.json").exists())


def faixa(n):
    """A faixa da conta em hub.json ("faixas": {"1": "construtor", ..., "4": "operador"}); None se não houver."""
    if (f := config.definido("agentes.faixas")) is not None:
        return f.get(str(n))  # chaves do toml são sempre texto
    try:
        return (config.ler_hub_json()
                .get("faixas") or {}).get(str(n))
    except (OSError, ValueError):
        return None


def numero():
    return os.environ.get("CLAUDE_CONTA", "1").strip() or "1"


def sufixo(n=None):
    n = str(n or numero())
    return "" if n == "1" else f"-{n}"


def caminho_trava(n=None):
    return TRAVA if not sufixo(n) else TRAVA.replace(".lock", f"{sufixo(n)}.lock")
CAIXA_AVISOS = comum_caixa.CAIXA

RE_LIMITE = re.compile(r"usage limit|session limit|weekly limit|hour limit|limit reached|hit your limit|"
                       r"rate[ _-]?limit|\b429\b|resets\s+\w", re.I)
RE_EPOCH = re.compile(r"\|(\d{10})\b")
MESES = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# Claude: "resets 3pm", "resets Oct 10, 5pm"; Codex: "try again at 4:31 PM", "try again at Oct 14th, 2026 3:42 PM"
RE_RESET = re.compile(r"(?:resets?|try again)\s+(?:at\s+)?(?:(?P<mes>[A-Za-z]{3})[a-z]*\s+(?P<dia>\d{1,2})(?:st|nd|rd|th)?,?\s*"
                      r"(?:(?P<ano>\d{4}),?\s*)?(?:at\s+)?)?"
                      r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?(?:\s*\((?P<tz>[^)]+)\))?", re.I)


def pasta(base_dados=None):
    return Path(base_dados or data()) / "claude"


def agora():
    return dt.datetime.now(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat(timespec="seconds")


def de_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- trava ----

@contextlib.contextmanager
def trava(esperar=None, caminho=None):
    """Trava exclusiva da conta. esperar=None: espera para sempre; 0: não espera (devolve False se ocupada);
    n: espera até n segundos. Dentro do `with`, o valor é True se pegou a trava."""
    import fcntl  # só existe no Linux; os testes de Windows não chegam aqui
    caminho = caminho or caminho_trava()
    try:  # sem O_CREAT primeiro: em /run/lock (sticky), O_CREAT em arquivo de outro usuário é negado até ao root
        fd = os.open(caminho, os.O_RDWR)
    except FileNotFoundError:
        fd = os.open(caminho, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o666)  # o primeiro a criar deixa os outros usuários abrirem
        pegou, limite = False, None if esperar is None else time.monotonic() + esperar
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if esperar is None else fcntl.LOCK_NB))
                pegou = True
                break
            except BlockingIOError:
                if time.monotonic() >= limite:
                    break
                time.sleep(min(5, max(0.1, limite - time.monotonic())))
        if pegou:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {iso(agora())}\n".encode())
        yield pegou
    finally:
        os.close(fd)  # fechar solta o flock


# ---- uso ----

TIPO_POR_ORIGEM = {"hub": "agendada", "agente": "manual", "manual": "manual", "tarefa": "agendada"}


def registrar(registro, base_dados=None):
    """Acrescenta uma execução a uso.jsonl. Campos: inicio, fim (ISO UTC), origem (tarefa|agente|manual), nome,
    modelo, ok, limite (bool), custo_usd, tokens {entrada, saida, cache_leitura, cache_escrita}, turnos,
    duracao_s, erro (curto), saida (caminho do resultado). `tipo` nunca fica vazio: quem não o informa (rotinas do
    hub, conversas do painel) recebe o da origem, e o que não se conhece vira "outro"."""
    if not registro.get("tipo"):
        registro = {**registro, "tipo": TIPO_POR_ORIGEM.get(registro.get("origem"), "outro")}
    p = pasta(base_dados)
    p.mkdir(parents=True, exist_ok=True)
    with open(p / "uso.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def tokens_de(saida_json):
    u = (saida_json or {}).get("usage") or {}
    return {"entrada": u.get("input_tokens", 0) or 0, "saida": u.get("output_tokens", 0) or 0,
            "cache_leitura": u.get("cache_read_input_tokens", 0) or 0,
            "cache_escrita": u.get("cache_creation_input_tokens", 0) or 0}


def ler_uso(desde=None, base_dados=None):
    arq = pasta(base_dados) / "uso.jsonl"
    if not arq.exists():
        return []
    saida = []
    for linha in arq.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
            if desde is None or de_iso(r["inicio"]) >= desde:
                saida.append(r)
        except (ValueError, KeyError):
            continue
    return saida


def gasto_dia(n=None, base_dados=None, quando=None):
    """US$ gastos hoje (dia em Brasília) pela conta `n` (padrão: a do processo): custo_usd das execuções que terminaram
    hoje (campo `fim`; sem ele, o `inicio`), do uso.jsonl."""
    quando = quando or agora()
    inicio = quando.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    n = str(n or numero())
    total = 0.0
    for r in ler_uso(inicio - dt.timedelta(days=1), base_dados):  # começou ontem, terminou hoje também conta
        if str(r.get("conta") or "1") != n:
            continue
        try:
            terminou = de_iso(r.get("fim") or r["inicio"])
            if terminou >= inicio:
                total += float(r.get("custo_usd") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return total


def relatorio_uso(dias=7, base_dados=None, quando=None):
    """Participação no consumo registrado; valores monetários apenas para a reserva API."""
    quando = quando or agora()
    regs = ler_uso(quando - dt.timedelta(days=dias), base_dados)
    if not regs:
        return f"Uso do Claude da VPS, últimos {dias} dias: nenhuma execução registrada."
    grupos = {}
    reserva = [r for r in regs if r.get("conta") == API]  # gasto real, não valor de referência
    for r in regs:
        if r.get("conta") == API:
            continue
        g = grupos.setdefault((r.get("origem", "?"), r.get("nome", "?")), {"n": 0, "falhas": 0, "custo": 0.0, "seg": 0.0, "limite": 0})
        g["n"] += 1
        g["falhas"] += 0 if r.get("ok") else 1
        g["limite"] += 1 if r.get("limite") else 0
        g["custo"] += float(r.get("custo_usd") or 0)
        g["seg"] += float(r.get("duracao_s") or 0)
    total = sum(g["custo"] for g in grupos.values()) or 0.0
    horas = sum(g["seg"] for g in grupos.values()) / 3600
    linhas = [f"Consumo das assinaturas Claude da VPS, últimos {dias} dias: "
              f"{sum(g['n'] for g in grupos.values())} execuções, {horas:.1f} h de trabalho. "
              "Percentuais abaixo: participação no consumo registrado das assinaturas, não na cota disponível."]
    for (origem, nome), g in sorted(grupos.items(), key=lambda x: -x[1]["custo"]):
        parte = f"{100 * g['custo'] / total:.0f}%" if total else "-"
        extra = (f", {g['falhas']} falha{'s' if g['falhas'] > 1 else ''}" if g["falhas"] else "") + \
                (f", {g['limite']}× no limite" if g["limite"] else "")
        linhas.append(f"- {nome} ({origem}): {parte} · {g['n']}× · {g['seg'] / 60:.0f} min{extra}")
    if reserva:
        x = sum(float(r.get("custo_usd") or 0) for r in reserva)
        linhas.append(f"Reserva API (gasto real): US$ {x:.2f} em {len(reserva)} execuções nos últimos {dias} dias.")
    p = pausa_ativa(base_dados, quando)
    if p:
        linhas.append(f"Fila pausada por limite até {de_iso(p['ate']).astimezone(BRT):%d/%m %H:%M} (Brasília).")
    return "\n".join(linhas)


# ---- limite e pausa ----

def eh_limite(texto):
    return bool(RE_LIMITE.search(texto or ""))


def _fuso(nome):
    if not nome:
        return UTC  # a CLI escreve no fuso da máquina; a VPS é UTC
    if nome.strip() in ("UTC", "Etc/UTC", "GMT"):
        return UTC
    if nome.strip() in ("America/Sao_Paulo", "BRT", "-03"):
        return BRT
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(nome.strip())
    except Exception:
        return UTC


def horario_reset(texto, quando=None, fuso_padrao=UTC, sem_horario=dt.timedelta(hours=1)):
    """Quando a cota volta, lido da mensagem da CLI. Sem horário legível: daqui a `sem_horario`. Sempre +2 min de folga."""
    quando = quando or agora()
    folga = dt.timedelta(minutes=2)
    m = RE_EPOCH.search(texto or "")
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)), UTC) + folga
    m = RE_RESET.search(texto or "")
    if m:
        fuso = _fuso(m.group("tz")) if m.group("tz") else fuso_padrao
        h, mi = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h < 24 and mi < 60:
            local = quando.astimezone(fuso)
            if m.group("mes") and m.group("mes")[:3].lower() in MESES:
                alvo = local.replace(year=int(m.group("ano") or local.year), month=MESES[m.group("mes")[:3].lower()],
                                     day=int(m.group("dia")), hour=h, minute=mi, second=0, microsecond=0)
                if not m.group("ano") and alvo < local - dt.timedelta(days=1):
                    alvo = alvo.replace(year=alvo.year + 1)
            else:
                alvo = local.replace(hour=h, minute=mi, second=0, microsecond=0)
                if alvo <= local:
                    alvo += dt.timedelta(days=1)
            return alvo.astimezone(UTC) + folga
    return quando + sem_horario


def pausa_ativa(base_dados=None, quando=None, n=None):
    arq = pasta(base_dados) / f"pausa{sufixo(n)}.json"
    try:
        p = json.loads(arq.read_text(encoding="utf-8"))
        return p if de_iso(p["ate"]) > (quando or agora()) else None
    except (OSError, ValueError, KeyError):
        return None


LIMITE_JANELA_PCT = 95.0  # % real da janela de 5 h (uso-contas.json) a partir do qual a conta OAuth conta como esgotada


def janela_esgotada(n, base_dados=None, quando=None, limite=None):
    """(lição 3): a janela de 5 h da conta `n` já passou do limite e ainda não zerou (uso-contas.json, da CLI)?
    Sem dado = tem folga. Quem escolhe a conta olha isto ANTES de iniciar: assim o limite é espera, não falha."""
    quando = quando or agora()
    try:
        uso = json.loads((pasta(base_dados) / "uso-contas.json").read_text(encoding="utf-8"))
        c = ((uso.get("contas") or {}).get(str(n)) or {}).get("cinco_horas") or {}
        return float(c["pct"]) >= (LIMITE_JANELA_PCT if limite is None else limite) and de_iso(c["reset"]) > quando
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def indisponivel(n, base_dados=None, quando=None, limite=None):
    """A conta `n` não deve receber trabalho agora: pausa gravada ou janela esgotada. Uma função só para a agenda, o bot e
    as rotinas (antes cada um olhava só a pausa, e a primeira chamada depois do limite virava falha `limite=True`)."""
    return bool(pausa_ativa(base_dados, quando, n)) or janela_esgotada(n, base_dados, quando, limite)


def pausar(texto, origem, base_dados=None, quando=None, avisar_fn=None, sem_horario=None, n=None):
    """Grava a pausa da conta `n` (padrão: a do processo) até o reset. Avisa no WhatsApp só se não havia pausa ativa.
    Devolve o horário de volta. `sem_horario`: quanto pausar sem horário legível (None = 6 h no codex, 1 h no resto)."""
    quando = quando or agora()
    n = str(n or numero())
    ja = pausa_ativa(base_dados, quando, n)
    # a cota do Codex volta em horas ou dias (janela semanal): sem horário legível, 1 h só faria falhar de novo
    ate = horario_reset(texto, quando, sem_horario=sem_horario or dt.timedelta(hours=6 if n == "codex" else 1))
    if ja and de_iso(ja["ate"]) >= ate:
        return de_iso(ja["ate"])
    p = pasta(base_dados)
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / f"pausa{sufixo(n)}.json.tmp"
    tmp.write_text(json.dumps({"ate": iso(ate), "desde": iso(quando), "origem": origem, "mensagem": (texto or "")[-300:]},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p / f"pausa{sufixo(n)}.json")
    if not ja:
        with contextlib.suppress(OSError):  # (F2): a pausa avisada ao dono, para conferir depois se era falsa
            with open(p / "pausas.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"conta": n, "desde": iso(quando), "ate": iso(ate), "origem": str(origem)[:120]},
                                   ensure_ascii=False) + "\n")
        (avisar_fn or avisar)(f"Claude VPS: a cota da conta {n} acabou ({origem}). Fila pausada até "
                              f"{ate.astimezone(BRT):%d/%m %H:%M} (Brasília); retoma sozinha.", agora_=True)
    return ate


def retomar(base_dados=None):
    with contextlib.suppress(FileNotFoundError):
        (pasta(base_dados) / f"pausa{sufixo()}.json").unlink()


# ---- reservas de API (em lista na) ----
# O teto é checado antes de começar, reservando US$ 5 por execução em curso (agenda.reserva_api): o dia nunca passa do
# teto (a tarefa para em US$ 5 pelo --max-budget-usd), ao custo de não começar outra com o dia US$ 5 abaixo dele.
# hub.json → "reservas_api" é a lista, na ordem de uso: cada uma {id, arquivo, rotulo, teto_dia, teto_total, credito,
# mensal, ativa}. `mensal` (crédito do plano Max: use-ou-perca) conta o mês civil em BRT e o teto do dia é o saldo do mês
# dividido pelos dias que restam, para gastar tudo até o fim do mês sem estourar no dia 1.

API = "api"  # conta das reservas: fora de CONTAS (só pega quando as OAuth não podem)
TETO_API_DIA_USD = 20
TETO_API_TOTAL_USD = 950
CREDITO_API_USD = 1000
AVISOS_API = (50, 80, 95)  # % de teto_total da reserva
LEGADO = "fulano"  # registro de uso sem "reserva" (anterior à lista) é desta
SEM_SALDO_HORAS = 6  # reserva que recusou por crédito: espera isto (a mensal, até o próximo mês)


def chave_api_arquivo_padrao():
    return Path(os.environ.get("ANTHROPIC_API_KEY_FILE") or home_agentes() / ".anthropic-api.key")


def reservas():
    """As reservas na ordem de uso, normalizadas: {id, arquivo (Path), rotulo, teto_dia, teto_total, credito, mensal, ativa}.
    ANTHROPIC_API_KEY_FILE (teste, ou troca de chave sem mexer no hub.json) vale uma lista de uma só."""
    try:
        bruto = config.ler_hub_json().get("reservas_api")
    except (OSError, ValueError, AttributeError):
        bruto = None
    if os.environ.get("ANTHROPIC_API_KEY_FILE") or not isinstance(bruto, list) or not bruto:
        bruto = [{"id": LEGADO, "rotulo": config.dono(True), "arquivo": str(chave_api_arquivo_padrao()), "teto_dia": TETO_API_DIA_USD,
                  "teto_total": TETO_API_TOTAL_USD, "credito": CREDITO_API_USD}]
    saida = []
    for r in bruto:
        if not isinstance(r, dict) or not r.get("id") or not r.get("arquivo"):
            continue
        credito = float(r.get("credito") or CREDITO_API_USD)
        arq = Path(os.path.expanduser(str(r["arquivo"])))
        saida.append({"id": str(r["id"]), "rotulo": r.get("rotulo") or str(r["id"]),
                      "arquivo": arq if arq.is_absolute() else Path(home_agentes()) / arq,
                      "teto_dia": float(r["teto_dia"]) if r.get("teto_dia") else None,
                      "teto_total": float(r.get("teto_total") or credito), "credito": credito,
                      "mensal": bool(r.get("mensal")), "ativa": r.get("ativa", True) is not False})
    return saida


def reserva(id_=None):
    """A reserva `id_`; sem id, a primeira da lista."""
    lista = reservas()
    return next((r for r in lista if r["id"] == id_), None) if id_ else (lista[0] if lista else None)


def _alvo(r):
    return r if isinstance(r, dict) else (reserva(r) if r else (escolher_reserva() or reserva()))


def chave_api_arquivo(r=None):
    """Arquivo da chave da reserva `r` (dict ou id); sem `r`, a que seria escolhida agora."""
    r = _alvo(r)
    return r["arquivo"] if r else chave_api_arquivo_padrao()


def chave_api(r=None):
    """A chave (sem quebra de linha) ou None. Nunca vai para log nem para registro."""
    with contextlib.suppress(OSError, ValueError):  # UTF-8 inválido = sem chave
        return chave_api_arquivo(r).read_text(encoding="utf-8").strip() or None
    return None


def chaves_api():
    """Todas as chaves que existem: para varrer a saída da CLI antes de gravar qualquer coisa."""
    return [k for r in reservas() if (k := chave_api(r))]


def _mes_brt(quando):
    return quando.astimezone(BRT).strftime("%Y-%m")


def _inicio_proximo_mes(quando):
    l = quando.astimezone(BRT)
    return dt.datetime(l.year + (l.month == 12), l.month % 12 + 1, 1, tzinfo=BRT).astimezone(UTC)


def gasto_api(base_dados=None, quando=None, r=None):
    """{"hoje": US$, "total": US$, "execucoes_hoje": n, "saldo": credito - total} da reserva `r` (dict ou id; padrão: a
    escolhida agora). hoje = dia civil em BRT; total = desde sempre, ou só do mês na reserva mensal."""
    r = _alvo(r)
    quando = quando or agora()
    dia = quando.astimezone(BRT).date()
    total = hoje = 0.0
    n = 0
    for u in ler_uso(None, base_dados):
        if u.get("conta") != API or (u.get("reserva") or LEGADO) != (r or {}).get("id", LEGADO):
            continue
        t = de_iso(u["inicio"]).astimezone(BRT)
        if r and r["mensal"] and t.strftime("%Y-%m") != _mes_brt(quando):
            continue
        custo = float(u.get("custo_usd") or 0)
        total += custo
        if t.date() == dia:
            hoje += custo
            n += 1
    credito = r["credito"] if r else CREDITO_API_USD
    return {"hoje": round(hoje, 2), "total": round(total, 2), "execucoes_hoje": n, "saldo": round(credito - total, 2)}


def teto_dia_api(r=None, base_dados=None, quando=None):
    """Quanto a reserva pode gastar hoje. Fixo (teto_dia); na mensal, o saldo de ontem ÷ dias que restam no mês
    (hoje incluído), no máximo teto_dia se houver."""
    r = _alvo(r)
    if not r:
        return TETO_API_DIA_USD
    if not r["mensal"]:
        return r["teto_dia"] or TETO_API_DIA_USD
    quando = quando or agora()
    local = quando.astimezone(BRT)
    restam = calendar.monthrange(local.year, local.month)[1] - local.day + 1
    g = gasto_api(base_dados, quando, r)
    dinamico = max(0.0, r["teto_total"] - (g["total"] - g["hoje"])) / restam
    return round(min(dinamico, r["teto_dia"]) if r["teto_dia"] else dinamico, 2)


def _arq_sem_saldo(base_dados=None):
    return pasta(base_dados) / "api-sem-saldo.json"


def sem_saldo_ate(r, base_dados=None, quando=None):
    """Até quando a reserva está de fora por crédito recusado (datetime) ou None."""
    try:
        est = json.loads(_arq_sem_saldo(base_dados).read_text(encoding="utf-8"))
        ate = de_iso(est[_alvo(r)["id"]])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return ate if ate > (quando or agora()) else None


def marcar_sem_saldo(r, base_dados=None, quando=None):
    """A reserva recusou por crédito (402 ou 429 de crédito): sai da escolha por SEM_SALDO_HORAS (a mensal, até o
    próximo mês: o crédito não volta antes). Devolve até quando."""
    r = _alvo(r)
    quando = quando or agora()
    ate = _inicio_proximo_mes(quando) if r["mensal"] else quando + dt.timedelta(hours=SEM_SALDO_HORAS)
    pasta(base_dados).mkdir(parents=True, exist_ok=True)
    arq = _arq_sem_saldo(base_dados)
    with trava(esperar=10, caminho=str(arq.with_suffix(".lock"))):  # recusas concorrentes: ler-alterar-gravar sem perder chave
        try:
            est = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            est = {}
        est = est if isinstance(est, dict) else {}
        est[r["id"]] = iso(ate)
        tmp = arq.with_name(f"{arq.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(est), encoding="utf-8")
        os.replace(tmp, arq)
    return ate


def bloqueio_reserva(r, base_dados=None, quando=None):
    """Motivo para a reserva `r` não rodar agora, ou None: "sem chave", "sem saldo", "teto total ... atingido",
    "teto do dia ... atingido; volta 0h BRT"."""
    r = _alvo(r)
    if not r["ativa"]:
        return "desligada no hub.json (ativa: false)"
    if not chave_api(r):
        return f"sem chave em {chave_api_arquivo(r)}"
    if ate := sem_saldo_ate(r, base_dados, quando):
        return f"sem saldo (crédito recusado); volta {ate.astimezone(BRT):%d/%m %H:%M}"
    g = gasto_api(base_dados, quando, r)
    if g["total"] >= r["teto_total"]:
        return f"teto {'do mês' if r['mensal'] else 'total'} de US$ {r['teto_total']:g} atingido (US$ {g['total']:.2f})"
    teto = teto_dia_api(r, base_dados, quando)
    if g["hoje"] >= teto:
        return f"teto do dia de US$ {teto:g} atingido (US$ {g['hoje']:.2f}); volta 0h BRT"
    return None


def escolher_reserva(base_dados=None, quando=None, so_mensal=False):
    """A primeira reserva da lista que pode rodar agora, ou None. `so_mensal`: só as de crédito use-ou-perca (trabalho
    pequeno que não precisa gastar a reserva paga)."""
    return next((r for r in reservas() if (r["mensal"] or not so_mensal) and not bloqueio_reserva(r, base_dados, quando)), None)


def bloqueio_api(base_dados=None, quando=None):
    """Motivo para a conta api não rodar agora (nenhuma reserva pode), ou None. Com mais de uma, junta os motivos."""
    lista = reservas()
    if not lista:
        return f"sem chave em {chave_api_arquivo_padrao()}"
    motivos = []
    for r in lista:
        m = bloqueio_reserva(r, base_dados, quando)
        if not m:
            return None
        motivos.append(m if len(lista) == 1 else f"{r['rotulo']}: {m}")
    return "; ".join(motivos)


def _arq_avisos(r, base_dados=None):
    return pasta(base_dados) / ("api-avisos.json" if r["id"] == LEGADO else f"api-avisos-{r['id']}.json")


def avisar_orcamento_api(base_dados=None, quando=None, avisar_fn=None, r=None):
    """Manda (uma vez cada) os avisos de 50/80/95% do teto total e o de desligada em 100%, por reserva (a mensal
    recomeça todo mês). Sem `r`, todas. Devolve os % avisados agora."""
    if r is None:
        return [n for x in reservas() for n in avisar_orcamento_api(base_dados, quando, avisar_fn, x)]
    r = _alvo(r)
    quando = quando or agora()
    g = gasto_api(base_dados, quando, r)
    arq = _arq_avisos(r, base_dados)
    try:
        est = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        est = {}
    est = est if isinstance(est, dict) else {}
    ja = est.get("avisados") if isinstance(est.get("avisados"), list) else []  # estado estragado = nada avisado ainda
    if r["mensal"] and est.get("mes") != _mes_brt(quando):
        ja = []
    novos = [n for n in AVISOS_API + (100,) if g["total"] >= n * r["teto_total"] / 100 and n not in ja]
    if not novos:
        return []
    nivel = max(novos)
    periodo = "do mês" if r["mensal"] else "total"
    if nivel < 100:
        texto = (f"Reserva API {r['rotulo']}: gasto {periodo} US$ {g['total']:.2f} = {nivel}% do teto de US$ {r['teto_total']:g} "
                 f"(crédito de US$ {r['credito']:g}; saldo estimado US$ {g['saldo']:.2f}).")
    else:
        texto = (f"Reserva API {r['rotulo']} desligada: gasto {periodo} US$ {g['total']:.2f} chegou ao teto de "
                 f"US$ {r['teto_total']:g}. A esteira segue na próxima reserva ou só nas contas OAuth.")
    (avisar_fn or avisar)(texto, agora_=True)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"avisados": sorted(ja + novos), "mes": _mes_brt(quando)}), encoding="utf-8")
    os.replace(tmp, arq)
    return novos


# ---- aviso no WhatsApp (caixa de avisos do bot_whatsapp) ----

def avisar(texto, agora_=False, caixa=None, arquivos=None, para=None, origem=None, tipo=None, pedido=False):
    """Aviso no WhatsApp pela caixa do bot (hub/comum/caixa.py é o escritor único do formato).
    arquivos: caminhos relativos ao DATA_DIR do bot (ex. reels/<id>.mp4), mandados junto do texto.
    para: número (55DDD...) que recebe no lugar do dono; só para destino que o dono autorizou.
    ao dono vale o porteiro (hub/comum/porteiro.py): `tipo="tecnico"` vai ao orquestrador, nunca ao WhatsApp;
    madrugada e teto viram resumo; `origem` nomeia o remetente no relatório."""
    comum_caixa.escrever(texto, agora=agora_, caixa=caixa or CAIXA_AVISOS, arquivos=arquivos, para=para,
                         origem=origem, tipo=tipo, pedido=pedido)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.conta", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("uso")
    u.add_argument("--dias", type=int, default=7)
    u.add_argument("--avisar", action="store_true", help="manda no WhatsApp (janela do bot)")
    sub.add_parser("pausa")
    sub.add_parser("retomar")
    li = sub.add_parser("limite", help="a CLI respondeu limite: pausa até o reset (texto da mensagem no stdin)")
    li.add_argument("--origem", required=True)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "uso":
        texto = relatorio_uso(args.dias)
        print(texto)
        if args.avisar:
            avisar(texto)
    elif args.cmd == "pausa":
        p = pausa_ativa()
        print(json.dumps(p, ensure_ascii=False) if p else "sem pausa")
    elif args.cmd == "limite":
        ate = pausar(sys.stdin.read(), args.origem)
        print(iso(ate))
    else:
        retomar()
        print("pausa apagada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
