"""Visibilidade em IA: rastreador de prompts por site, nos motores de IA, com agenda e estimativa de custo.

Estado em pasta("visibilidade_ia", <site>):
    tracker.json          {"tracker": {...agenda, motores, mercado}, "prompts": [...]} (outras chaves são preservadas)
    rodadas/<runId>.json  uma rodada: id, status, trigger, createdAt, completedAt, observations [{promptId, status, answerMarkdown, ...}]
Marca e concorrentes vêm do cadastro do site (sites/<id>.json).
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import random
import sys
import threading
import uuid
from zoneinfo import ZoneInfo

from hub import citacao, dataforseo
from hub.config import dono
from hub.mcp_grupos import ErroUso, padrao, pasta, so

MOTORES = ["chatgpt", "gemini", "google_ai_overview", "claude", "perplexity"]
ROTULOS = {"chatgpt": "ChatGPT", "gemini": "Gemini", "google_ai_overview": "Google AI Overviews", "claude": "Claude",
           "perplexity": "Perplexity"}
API_MODELO = {"claude", "perplexity"}  # respondem pela API do modelo, a preço de API ao vivo
# US$ do DataForSEO por resposta ao vivo (o hub coleta sempre ao vivo): raspagem ao vivo, ou API do modelo com busca na web.
PRECO = {"chatgpt": 0.004, "gemini": 0.004, "google_ai_overview": 0.004, "claude": 0.18, "perplexity": 0.04}
INTERVALOS = ["daily", "weekly", "monthly"]
CHECAGENS_MES = {"daily": 30, "weekly": 4, "monthly": 1}
TOPICO_PADRAO = "General"
MAX_PROMPTS, MAX_TOPICOS = 100, 30
# Países do mercado (código DataForSEO:idioma padrão); "!" = o Claude não coleta nesse país. O ChatGPT não coleta em 2275.
PAISES = [(int(c), i.rstrip("!"), i.endswith("!")) for c, i in (x.split(":") for x in """
2008:sq! 2012:fr! 2020:ca! 2024:pt! 2032:es 2051:hy! 2036:en 2040:de 2031:az! 2044:en! 2048:ar! 2050:bn! 2052:en!
2056:nl 2084:en! 2068:es! 2070:bs! 2072:en! 2076:pt 2096:ms! 2100:bg! 2854:fr! 2116:en! 2120:fr! 2124:en 2152:es
2170:es! 2188:es! 2384:fr! 2191:hr! 2196:el! 2203:cs! 2208:da 2214:es! 2218:es! 2818:ar! 2222:es! 2233:et! 2231:en!
2242:en! 2246:fi 2250:fr 2268:en! 2276:de 2288:en! 2300:el! 2320:es! 2831:en! 2328:en! 2332:fr! 2340:es! 2344:zh-TW
2348:hu! 2352:is! 2356:en 2360:id 2368:ar! 2372:en! 2833:en! 2376:he! 2380:it 2388:en! 2392:ja 2832:en! 2400:ar!
2398:ru! 2404:en! 2414:ar! 2417:ru! 2418:en! 2428:lv! 2422:ar! 2438:de! 2440:lt! 2442:fr! 2450:fr! 2454:en! 2458:en
2462:en! 2470:en! 2480:en! 2484:es 2498:ro! 2492:fr! 2496:en! 2499:sr! 2504:ar! 2508:pt! 2104:en! 2516:en! 2524:en!
2528:nl 2554:en 2558:es! 2566:en! 2807:mk! 2578:nb 2512:ar! 2586:en! 2275:ar! 2591:es! 2598:en! 2600:es! 2604:es!
2608:en 2616:pl 2620:pt 2634:ar! 2642:ro! 2646:en! 2674:it! 2682:ar 2686:fr! 2688:sr! 2702:en! 2703:sk! 2705:sl!
2710:en 2410:ko 2724:es 2144:en! 2740:nl! 2752:sv 2756:de 2158:zh-TW 2762:ru! 2834:en! 2764:th! 2780:en! 2788:ar!
2792:tr 2795:ru! 2800:en! 2804:uk! 2784:en! 2826:en 2840:en 2858:es! 2860:ru! 2862:es! 2704:vi! 2894:en! 2716:en!
""".split())]
IDIOMA = {c: i for c, i, _ in PAISES}
IDIOMAS = {2012: ["ar", "fr"], 2056: ["de", "fr", "nl"], 2124: ["en", "fr"], 2196: ["el", "en"], 2300: ["el", "en"],
           2344: ["en", "zh-TW"], 2356: ["en", "hi"], 2360: ["en", "id"], 2376: ["ar", "he"], 2458: ["en", "ms"],
           2504: ["ar", "fr"], 2586: ["en", "ur"], 2608: ["en", "tl"], 2702: ["en", "zh-CN"], 2756: ["de", "fr", "it"],
           2784: ["ar", "en"], 2804: ["ru", "uk"], 2818: ["ar", "en"], 2840: ["en", "es"], 2704: ["en", "vi"]}
SEM_PAIS = {"chatgpt": [2275], "gemini": [], "google_ai_overview": [], "claude": [c for c, _, sem in PAISES if sem], "perplexity": []}
NOTA_SITE = "Uma resposta do site de consumo. Coleta em qualquer país do mercado, exceto unsupportedLocationCodes."
NOTA_API = ("Uma resposta da API ao vivo do modelo, com busca na web, a preço de API ao vivo. Coleta em qualquer país do mercado, "
            "exceto unsupportedLocationCodes.")
NOTAS = {"chatgpt": NOTA_SITE, "gemini": NOTA_SITE, "claude": NOTA_API, "perplexity": NOTA_API,
         "google_ai_overview": "O AI Overview de uma página de resultados do Google. Página sem AI Overview é resultado sem resposta, não marca ausente."}
_TRAVA = threading.Lock()

UUID = {"type": "string", "format": "uuid"}
SITE = {"site": {"type": "string", "description": "id do site no hub (ex.: psite13), domínio ou nome"},
        "projectId": {"type": "string", "description": "o mesmo que site"}}
INTERVALO = {"type": "string", "enum": INTERVALOS, "description": "Frequência da checagem agendada. A mensal roda no último dia do mês."}
PATCH = {
    "engines": {"type": "array", "items": {"type": "string", "enum": MOTORES}, "minItems": 1, "maxItems": len(MOTORES)},
    "locationCode": {"type": "integer", "exclusiveMinimum": 0,
                     "description": "País da coleta, em código de local do DataForSEO (2076 = Brasil, 2840 = Estados Unidos). "
                                    "Rastreador novo usa o mercado padrão do hub. Veja em capabilities os países que um motor não atende."},
    "languageCode": {"type": "string", "minLength": 2, "maxLength": 8,
                     "description": "Idioma da coleta. Omita ao trocar de país para usar o idioma padrão do país."},
    "prompts": {"type": "array", "maxItems": MAX_PROMPTS, "items": {"type": "object", "required": ["text"], "properties": {
        "id": {**UUID, "description": "Id do prompt existente, para editar ou pausar. Omita em prompt novo. Mudar o texto arquiva o "
                                      "prompt e cria um novo com o texto novo, para as respostas antigas manterem o prompt exato."},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000,
                 "description": "Prompt ou palavra exata do cliente, até 2.000 caracteres; maiúsculas mantidas. Não amplie nem reescreva sem pedido."},
        "topic": {"type": "string", "minLength": 1, "maxLength": 100,
                  "description": "Tópico que agrupa o prompt. Prompt novo cai em General; edição sem tópico mantém o salvo. "
                                 "Para renomear um tópico, mova todos os prompts dele."},
        "paused": {"type": "boolean"}}}},
    "archivePromptIds": {"type": "array", "items": UUID, "maxItems": MAX_PROMPTS},
}
HORARIO = {"type": "object", "required": ["hour", "minute"], "properties": {
    "weekday": {"type": "integer", "minimum": 0, "maximum": 6, "description": "Dia da semana, 0 = domingo. Exigido no semanal, ignorado nos outros."},
    "hour": {"type": "integer", "minimum": 0, "maximum": 23, "description": "Hora, 0-23."},
    "minute": {"type": "integer", "minimum": 0, "maximum": 59, "description": "Minuto, 0-59."},
    "timeZone": {"type": "string", "description": 'Fuso IANA do dia e da hora, ex. "America/Sao_Paulo". Padrão: UTC.'}}}


def _f(descricao, props, obrigatorios=()):
    esquema = {"type": "object", "properties": {**SITE, **props}}
    return {"description": descricao, "inputSchema": {**esquema, "required": list(obrigatorios)} if obrigatorios else esquema}


FERRAMENTAS = {
    "save_ai_visibility_tracker": _f(
        "Cria ou edita em lote os prompts rastreados nas IAs, com tópicos, motores, país e idioma. Para pausar ou arquivar um tópico, "
        "pause ou arquive cada prompt dele. Marca, domínio e concorrentes vêm do cadastro do site. Leia o rastreador antes e edite "
        "prompts pelo id. Rastreador novo começa com agenda semanal; salvar é de graça, e a primeira checagem paga roda cerca de uma "
        "semana depois. Mostre a estimativa ao usuário antes de criar o rastreador ou acrescentar prompts ou motores, e pause com "
        "set_ai_visibility_schedule se ele não quiser checagens agendadas. Não reescreva prompts do cliente sem pedido.", PATCH),
    "get_ai_visibility_tracker": _f(
        "Lê o rastreamento em IA: agenda, tópicos, prompts exatos, a marca e os concorrentes do cadastro com que as respostas são "
        "comparadas, o que cada motor atende por país, e as rodadas recentes. Devolve configured=false quando não existe. De graça; "
        "nunca inicia nem consulta coleta.", {}),
    "set_ai_visibility_schedule": _f(
        "Liga o rastreamento em IA diário, semanal ou mensal, muda a frequência ou o horário, ou pausa as próximas checagens. Ligado, "
        "cada checagem gasta crédito do DataForSEO: mostre ao usuário a estimativa de estimate_ai_visibility_cost e peça aprovação "
        "antes. No hub as checagens rodam no cron diário: ao ligar pela primeira vez, a rodada base fica para a próxima passada do "
        "cron (run volta null). Pausar não apaga nada.",
        {"enabled": {"type": "boolean"},
         "scheduleInterval": {**INTERVALO, "description": "Frequência ao ligar. Padrão: a salva, ou semanal. Ignorada ao pausar."},
         "scheduleTime": {**HORARIO, "description": "Horário ao ligar. Omita para manter a próxima checagem quando a frequência não "
                                                   "muda, ou para sortear um horário entre 04:00 e 10:00 UTC."}}, ["enabled"]),
    "estimate_ai_visibility_cost": _f(
        "Estima uma checagem e o custo mensal da agenda diária, semanal ou mensal, sem coletar respostas. patch estima mudanças "
        "propostas antes de salvar; promptIds estima uma checagem só desses prompts. O hub coleta sempre ao vivo, então costUsd "
        "(checagem agendada) e runNowCostUsd (checagem avulsa) usam o mesmo preço. Mostre o custo ao usuário antes de ligar o "
        "rastreamento ou rodar uma checagem paga, e passe o runNowCostUsd aprovado a run_ai_visibility_check como maxCostUsd. De graça.",
        {"scheduleInterval": {**INTERVALO, "description": "Frequência da estimativa mensal. Padrão: a salva, ou semanal."},
         "patch": {"type": "object", "properties": PATCH, "description": "Mudanças propostas para estimar antes de salvar."},
         "promptIds": {"type": "array", "items": UUID, "minItems": 1, "maxItems": MAX_PROMPTS}}),
}


def _iso(d):
    return d.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _agora():
    return dt.datetime.now(dt.timezone.utc)


def _arq(site):
    return pasta("visibilidade_ia", site["id"]) / "tracker.json"


def ler(site):
    try:
        return json.loads(_arq(site).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def gravar(site, estado):
    arq = _arq(site)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, arq)


@contextlib.contextmanager
def trava(site):
    """Trava do ler-mudar-gravar do tracker.json do site: entre threads (_TRAVA) e entre processos (o MCP e o cron diário)."""
    with _TRAVA, open(pasta("visibilidade_ia", site["id"]) / ".tracker.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def rodadas(site):
    """As rodadas gravadas, mais nova primeiro. Arquivo corrompido ou que não é objeto é pulado (com uma linha no stderr)."""
    p = pasta("visibilidade_ia", site["id"]) / "rodadas"
    lidas = []
    for a in sorted(p.glob("*.json")) if p.is_dir() else []:
        try:
            r = json.loads(a.read_text(encoding="utf-8"))
        except ValueError:
            r = None
        if isinstance(r, dict):
            lidas.append(r)
        else:
            print(f"{site['id']}: rodada ilegível, pulei: {a.name}", file=sys.stderr)
    return sorted(lidas, key=lambda r: r.get("createdAt") or "", reverse=True)


def visao_rodada(r):
    obs = r.get("observations") or []
    feitas = sum(o.get("status") == "completed" for o in obs)
    falhas = sum(o.get("status") == "failed" for o in obs)
    return {**so(r, ["id", "status", "trigger"]), "expected": len(obs), "completed": feitas, "failed": falhas,
            "pending": len(obs) - feitas - falhas, **so(r, ["createdAt", "completedAt"]), "pollAfterSeconds": 0}


# --- validação do pedido ---

def _uuid(v, campo):
    try:
        return str(uuid.UUID(v))
    except (TypeError, ValueError, AttributeError):
        raise ErroUso(f"{campo} tem que ser um uuid") from None


def _lista(args, chave, maximo, minimo=0):
    v = args.get(chave)
    if v is None:
        return None
    if not isinstance(v, list) or not minimo <= len(v) <= maximo:
        raise ErroUso(f"{chave} tem que ser uma lista de {minimo} a {maximo} itens")
    return v


def _intervalo(args):
    v = args.get("scheduleInterval")
    if v is not None and v not in INTERVALOS:
        raise ErroUso("scheduleInterval tem que ser daily, weekly ou monthly")
    return v


def _patch(args):
    motores = _lista(args, "engines", len(MOTORES), 1)
    if motores and any(m not in MOTORES for m in motores):
        raise ErroUso(f"engines aceita: {', '.join(MOTORES)}")
    loc, lang = args.get("locationCode"), args.get("languageCode")
    if loc is not None and (type(loc) is not int or loc <= 0):
        raise ErroUso("locationCode tem que ser um inteiro positivo")
    if lang is not None and not (isinstance(lang, str) and 2 <= len(lang) <= 8):
        raise ErroUso("languageCode tem que ter de 2 a 8 caracteres")
    prompts = []
    for p in _lista(args, "prompts", MAX_PROMPTS) or []:
        texto = p.get("text").strip() if isinstance(p, dict) and isinstance(p.get("text"), str) else ""
        if not 1 <= len(texto) <= 2000:
            raise ErroUso("cada prompt precisa de text, de 1 a 2.000 caracteres")
        topico = p.get("topic")
        if topico is not None and not (isinstance(topico, str) and 1 <= len(topico.strip()) <= 100):
            raise ErroUso("topic tem que ter de 1 a 100 caracteres")
        if p.get("paused") is not None and not isinstance(p["paused"], bool):
            raise ErroUso("paused tem que ser true ou false")
        prompts.append({"id": p.get("id") and _uuid(p["id"], "id do prompt"), "text": texto,
                        "topic": topico and topico.strip(), "paused": p.get("paused")})
    arquivar = [_uuid(i, "archivePromptIds") for i in _lista(args, "archivePromptIds", MAX_PROMPTS) or []]
    return {"engines": motores and list(dict.fromkeys(motores)), "locationCode": loc, "languageCode": lang,
            "prompts": prompts, "archivePromptIds": arquivar}


# --- regras do rastreador ---

def _primeira_semanal(agora):
    """Primeira checagem de um rastreador novo: pelo menos uma semana inteira depois (nunca cobra ao criar)."""
    p = proxima(agora, "weekly")
    return p + dt.timedelta(days=1) if p < agora + dt.timedelta(days=7) else p


def proxima(agora, intervalo, horario=None, anterior=None):
    """Próxima checagem: a partir da anterior (âncora, como computeNextCheckAt do original), no horário escolhido (no fuso
    dele), ou num horário sorteado entre 04:00 e 10:00 UTC. Sempre depois de agora."""
    if anterior is not None:
        return _avancar(agora, intervalo, anterior)
    if horario is None:
        h, m, desloc, dia_sem = random.randint(4, 9), random.randint(0, 59), 0, None
    else:
        fuso = ZoneInfo(_fuso(horario))
        minutos = horario["hour"] * 60 + horario["minute"] - int(agora.astimezone(fuso).utcoffset().total_seconds() // 60)
        desloc, minutos = divmod(minutos, 1440)
        h, m = divmod(minutos, 60)
        dia_sem = None if horario.get("weekday") is None else (horario["weekday"] + desloc) % 7
    if intervalo == "monthly":  # último dia do mês (no fuso de quem escolheu), daí o deslocamento de -1, 0 ou +1 dia
        mes = agora.month - 1  # começa no fim do mês passado (meses contados de 0 a partir de janeiro de agora.year)
        while True:
            fim = dt.datetime(agora.year + mes // 12, mes % 12 + 1, 1, h, m, tzinfo=dt.timezone.utc) + dt.timedelta(days=desloc - 1)
            if fim > agora:
                return fim
            mes += 1
    if horario is None:
        return (agora + dt.timedelta(days=1 if intervalo == "daily" else 7)).replace(hour=h, minute=m, second=0, microsecond=0)
    p = agora.replace(hour=h, minute=m, second=0, microsecond=0)
    if intervalo == "weekly" and dia_sem is not None:
        p += dt.timedelta(days=(dia_sem - (p.isoweekday() % 7)) % 7)
    if p <= agora:
        p += dt.timedelta(days=1 if intervalo == "daily" or dia_sem is None else 7)
    return p


def _avancar(agora, intervalo, anterior):
    """Pula de intervalo em intervalo desde a âncora; no mensal mantém a hora e a relação dela com o fim do mês (dia 1 = +1,
    penúltimo dia = -1, senão o último dia)."""
    if intervalo != "monthly":
        passo = dt.timedelta(days=1 if intervalo == "daily" else 7)
        return anterior + passo * (max(dt.timedelta(0), agora - anterior) // passo + 1)
    ultimo = ((anterior.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)).day
    desloc = 1 if anterior.day == 1 else -1 if anterior.day == ultimo - 1 else 0
    mes = anterior.month + (desloc != 1)  # índice (de 0) do mês seguinte ao do fim de mês; a âncora do dia 1 é do mês anterior
    while True:
        p = dt.datetime(anterior.year + mes // 12, mes % 12 + 1, 1, anterior.hour, anterior.minute,
                        tzinfo=dt.timezone.utc) + dt.timedelta(days=desloc - 1)
        if p > agora:
            return p
        mes += 1


def _mercado(t, patch, novo):
    loc = patch["locationCode"] or t["locationCode"]
    lang = patch["languageCode"] or (t["languageCode"] if loc == t["locationCode"] else IDIOMA.get(loc, "en"))
    erro = _erro_mercado(t["engines"], loc, lang)
    if erro and novo and patch["locationCode"] is None and patch["languageCode"] is None:
        loc, lang, erro = 2840, "en", None  # o mercado padrão não atende: começa nos EUA em vez de falhar num valor não pedido
    if erro:
        raise ErroUso(erro)
    t["locationCode"], t["languageCode"] = loc, lang


def _erro_mercado(motores, loc, lang):
    if loc not in IDIOMA:
        return "locationCode fora da lista de países do mercado (ex.: 2076 Brasil, 2840 Estados Unidos)"
    idiomas = IDIOMAS.get(loc, [IDIOMA[loc]])
    if lang not in idiomas:
        return f"o país {loc} aceita languageCode {' ou '.join(idiomas)}. Omita languageCode para usar o idioma padrão do país."
    sem = [ROTULOS[m] for m in motores if loc in SEM_PAIS[m]]
    if sem:
        return f"{' e '.join(sem)} não coleta respostas no país {loc}. Escolha outro país ou tire o motor."
    return None


def projetar(site, estado, patch):
    """Aplica o patch ao rastreador salvo, ou a um novo no mercado padrão. Devolve (estado, criados, editados, pulados)."""
    agora = _iso(_agora())
    novo = estado is None
    if novo:
        loc, lang = padrao()
        # Rastreador novo roda semanal; a primeira checagem fica uma semana à frente, então criar não cobra.
        estado = {"tracker": {"id": str(uuid.uuid4()), "projectId": site["id"], "enabled": True, "engines": ["chatgpt", "gemini"],
                              "locationCode": loc, "languageCode": lang, "scheduleInterval": "weekly",
                              "nextCheckAt": _iso(_primeira_semanal(_agora())), "lastSkipReason": None, "createdAt": agora},
                  "prompts": []}
    else:
        estado = json.loads(json.dumps(estado))
    t = estado["tracker"]
    if patch["engines"]:
        t["engines"] = [m for m in MOTORES if m in patch["engines"]]
    _mercado(t, patch, novo)
    contas = _aplicar_prompts(estado["prompts"], patch, agora)
    vivos = [p for p in estado["prompts"] if not p["archived"]]
    if len(vivos) > MAX_PROMPTS:
        raise ErroUso("o rastreamento aceita até 100 prompts não arquivados por site. Arquive os que não usa antes.")
    if len({p["topic"] for p in vivos}) > MAX_TOPICOS:
        raise ErroUso("o rastreamento aceita até 30 tópicos por site. Arquive os prompts de um tópico sem uso antes.")
    return estado, *contas


def _aplicar_prompts(prompts, patch, agora):
    """Texto de prompt nunca muda no lugar: texto novo arquiva o editado e entra como prompt próprio, restaurando um
    arquivado de mesmo texto para o histórico continuar."""
    criados = editados = pulados = 0
    mexidos = set()
    for p in patch["prompts"]:
        ex = next((q for q in prompts if q["id"] == p["id"]), None) if p["id"] else None
        if p["id"] and not ex:
            raise ErroUso("um id de prompt não é deste rastreador. Leia o rastreador e use os ids dele.")
        if ex and ex["id"] in mexidos:
            raise ErroUso("cada prompt só pode ser editado uma vez por pedido")
        if ex:
            mexidos.add(ex["id"])
        topico = p["topic"] or (ex and ex["topic"]) or TOPICO_PADRAO
        pausado = p["paused"] if p["paused"] is not None else bool(ex and ex["paused"])
        if ex and ex["text"] == p["text"]:
            ex.update(topic=topico, paused=pausado)
            editados += 1
            continue
        if any(q["text"] == p["text"] and not q["archived"] for q in prompts):
            if ex:
                raise ErroUso("esta edição repete o texto exato de outro prompt ativo")
            pulados += 1
            continue
        if ex:
            ex["archived"] = True
            editados += 1
        arquivado = next((q for q in prompts if q["archived"] and q["text"] == p["text"]), None)
        if arquivado:
            arquivado.update(archived=False, topic=topico, paused=pausado)
            mexidos.add(arquivado["id"])
        else:
            novo = {"id": str(uuid.uuid4()), "text": p["text"], "topic": topico, "paused": pausado, "archived": False, "createdAt": agora}
            mexidos.add(novo["id"])
            prompts.append(novo)
        criados += 1
    for i in patch["archivePromptIds"]:
        q = next((q for q in prompts if q["id"] == i), None)
        if not q:
            raise ErroUso("um id de archivePromptIds não é deste rastreador")
        if not q["archived"]:
            q["archived"] = True
            editados += 1
    return criados, editados, pulados


def ativos(estado):
    return [p for p in estado["prompts"] if not p["paused"] and not p["archived"]]


def _com_chave():
    try:
        return bool(dataforseo.chave())
    except (OSError, KeyError, ValueError):
        return False


def marcas(site):
    return [{"name": site["marca"]["nome"], "domain": site["dominio"], "own": True},
            *({"name": c, "domain": "", "own": False} for c in site["concorrentes"])]  # o cadastro só tem o nome do concorrente


def com_marca(texto, a):
    """O texto cita a marca (aliases, nome ou host do cadastro; a = citacao.alvo(site)): a regra do hub para prompt com marca."""
    return citacao.mencoes(texto, a["nomes"])[0] > 0 or citacao.mencoes(texto, a["hosts"])[0] > 0


def estado_publico(site):
    estado = ler(site)
    cap = [{"engine": m, "label": ROTULOS[m], "unsupportedLocationCodes": SEM_PAIS[m], "maxPromptLength": 2000, "note": NOTAS[m],
            "modelApi": m in API_MODELO} for m in MOTORES]
    base = {"configured": False, "providerConfigured": _com_chave(), "tracker": None, "topics": [], "prompts": [],
            "brands": marcas(site), "engines": [], "capabilities": cap, "recentRuns": []}
    if not estado:
        return base
    rs = rodadas(site)
    respondidos = {o.get("promptId") for r in rs for o in r.get("observations") or [] if o.get("status") == "completed" and o.get("answerMarkdown")}
    a = citacao.alvo(site)
    t = estado["tracker"]
    return {**base, "configured": True, "engines": t["engines"],
            "tracker": {**so(t, ["id", "projectId", "enabled", "locationCode", "languageCode", "scheduleInterval", "nextCheckAt",
                                 "lastSkipReason", "createdAt"]), "baselinePending": bool(t.get("baselinePending"))},
            "topics": sorted({p["topic"] for p in estado["prompts"] if not p["archived"]}),
            "prompts": [{**so(p, ["id", "text", "topic", "paused", "archived"]),
                         "branded": com_marca(p["text"], a),
                         "hasResults": p["id"] in respondidos} for p in estado["prompts"]],
            "recentRuns": [visao_rodada(r) for r in rs[:10]]}


# --- ferramentas ---

def _salvar(site, args):
    patch = _patch(args)
    with trava(site):
        estado, criados, editados, pulados = projetar(site, ler(site), patch)
        gravar(site, estado)
    return {"ok": True, "state": estado_publico(site), "created": criados, "updated": editados, "skipped": pulados}


def _fuso(horario):
    return "UTC" if horario.get("timeZone") is None else horario["timeZone"]  # "" é fuso inválido, não UTC


def _horario(v):
    if v is None:
        return None
    ok = isinstance(v, dict) and all(type(v.get(k)) is int and 0 <= v[k] <= n for k, n in (("hour", 23), ("minute", 59)))
    if not ok or (v.get("weekday") is not None and not (type(v["weekday"]) is int and 0 <= v["weekday"] <= 6)):
        raise ErroUso("scheduleTime precisa de hour (0-23) e minute (0-59); weekday é 0-6")
    try:
        ZoneInfo(_fuso(v))
    except (ValueError, KeyError, TypeError):  # ZoneInfoNotFoundError é KeyError
        raise ErroUso("timeZone desconhecido (use um fuso IANA, ex. America/Sao_Paulo)") from None
    return v


def _agenda(site, args):
    if not isinstance(args.get("enabled"), bool):
        raise ErroUso("enabled tem que ser true ou false")
    intervalo, horario = _intervalo(args), _horario(args.get("scheduleTime"))
    with trava(site):
        estado = ler(site)
        if not estado:
            raise ErroUso("salve prompts com save_ai_visibility_tracker antes de ligar a coleta")
        t = estado["tracker"]
        if not args["enabled"]:
            t["enabled"] = False
        else:
            if not ativos(estado):
                raise ErroUso("acrescente prompts ativos antes de ligar o rastreamento agendado")
            if not _com_chave():
                raise ErroUso(f"o DataForSEO não está configurado no hub; avise o {dono()}")
            intervalo = intervalo or t["scheduleInterval"]
            agora = _agora()
            if horario or intervalo != t["scheduleInterval"] or not t["nextCheckAt"]:
                t["nextCheckAt"] = _iso(proxima(agora, intervalo, horario))
            # Sem fila no hub: a rodada base não é coletada aqui; o cron diário a trata como vencida e limpa a marca.
            if not any(r.get("trigger") in ("baseline", "scheduled") and r.get("status") != "failed" for r in rodadas(site)):
                t["baselinePending"] = True
            t.update(enabled=True, scheduleInterval=intervalo, lastSkipReason=None)
        gravar(site, estado)
    return {"ok": True, "state": estado_publico(site), "run": None}


def _estimar(site, args):
    intervalo = _intervalo(args)
    patch = args.get("patch")
    if patch is not None and not isinstance(patch, dict):
        raise ErroUso("patch tem que ser um objeto")
    ids = [_uuid(i, "promptIds") for i in _lista(args, "promptIds", MAX_PROMPTS, 1) or []] or None
    atual = ler(site)
    estado = projetar(site, atual, _patch(patch))[0] if patch is not None else atual
    if not estado:
        raise ErroUso("passe os prompts propostos em patch, ou salve o rastreamento antes de estimar")
    vivos = ativos(estado)
    if ids and any(i not in {p["id"] for p in vivos} for i in ids):
        raise ErroUso("escolha ids de prompts ativos deste rastreador; arquivados ou pausados não entram na checagem")
    prompts = [p for p in vivos if not ids or p["id"] in ids]
    motores = estado["tracker"]["engines"]
    custo = round(sum(PRECO[m] for m in motores) * len(prompts), 6)
    intervalo = intervalo or (atual and atual["tracker"]["scheduleInterval"]) or "weekly"
    n = CHECAGENS_MES[intervalo]
    api = [ROTULOS[m] for m in motores if m in API_MODELO]
    avisos = ["Custo estimado na sua própria conta do DataForSEO; não há crédito de plataforma. O hub coleta sempre ao vivo.",
              f"Uma resposta nova por prompt e motor. A estimativa {intervalo} assume {n} {'checagem' if n == 1 else 'checagens'} por mês.",
              *([f"{' e '.join(api)} {'usa' if len(api) == 1 else 'usam'} preço de API ao vivo, que custa mais por resposta."] if api else []),
              *([] if _com_chave() else ["O DataForSEO não está configurado no hub. Dá para montar o rastreador; a coleta precisa da chave."])]
    return {"ok": True, "promptCount": len(prompts), "engineCount": len(motores), "observations": len(prompts) * len(motores),
            "providerCostUsd": custo, "costUsd": custo, "costCredits": 0, "runNowCostUsd": custo, "runNowCostCredits": 0,
            "scheduleInterval": intervalo, "checksPerMonth": n, "monthlyCostUsd": round(custo * n, 6), "currency": "USD",
            "warnings": avisos}


def executar(nome, args, site):
    if not site:
        raise ErroUso(f"{nome} precisa do argumento site (id do site no hub, domínio ou nome); list_projects lista os sites")
    if nome == "get_ai_visibility_tracker":
        return {"ok": True, **estado_publico(site)}
    return {"save_ai_visibility_tracker": _salvar, "set_ai_visibility_schedule": _agenda,
            "estimate_ai_visibility_cost": _estimar}[nome](site, args)


if __name__ == "__main__":
    # python3 -m hub.mcp_grupos.visibilidade_ia rodar-agendados: o cron diário (deploy/nimbus.cron).
    # python3 -m hub.mcp_grupos.visibilidade_ia ia-pendentes: a fila de IA (deploy/nimbus-mcp-ia.path).
    # A lógica fica nos outros módulos: importados daqui, eles usam este módulo pelo nome do pacote, não este __main__.
    if sys.argv[1:] == ["rodar-agendados"]:
        from hub.mcp_grupos.visibilidade_ia_rodada import rodar_agendados
        sys.exit(rodar_agendados())
    if sys.argv[1:] == ["ia-pendentes"]:
        from hub.mcp_grupos.visibilidade_ia_pesquisa import ia_pendentes
        sys.exit(ia_pendentes())
    sys.exit("uso: python3 -m hub.mcp_grupos.visibilidade_ia rodar-agendados | ia-pendentes")
