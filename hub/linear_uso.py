"""Apoio do hub.linear ao limite de chamadas: medida por hora e por chave, cache curto e estado do webhook.

- `registrar(rotulo, cabecalhos)`: conta a chamada na hora UTC, guarda o limite/restante que o Linear devolve nos
  cabeçalhos X-RateLimit-Requests-* e dá o alarme ao orquestrador acima de 70% de qualquer chave. Só o RÓTULO da chave
  (app2, fulano, app) vai para o arquivo, nunca o valor.
- `cache_ler`/`cache_gravar`/`cache_limpar`: respostas de leitura por 60-120 s, em arquivo (valem entre processos);
  qualquer mutação limpa tudo, para não ler estado velho logo depois de mexer.
- `hook_*`: o receptor do webhook (deploy/linear-webhook.py) anota as issues dos eventos; o tick só consulta essas
  issues enquanto o webhook estiver vivo (evento nos últimos 5 min) e varre tudo a cada 15 min como rede de segurança.
"""
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows (PC do dono): só despacha issue, não roda o tick
    fcntl = None

from hub import data

LIMITE_PADRAO = {"app2": 5000, "app": 5000, "fulano": 2500}  # chamadas/hora quando o cabeçalho não veio
ALARME = 0.70
HOOK_VIVO_S = 300  # evento recebido nos últimos 5 min: o webhook está vivo
VARREDURA_S = 900  # varredura completa de fallback
VARREDURA_MUDO_S = 300  # webhook sem evento: varre a cada 5 min, não a cada tick (cada varredura gasta dezenas de chamadas)
MAX_HORAS = 48


def _pasta():
    p = Path(data()) / "linear"
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def _trava(nome):
    if fcntl is None:
        # ponytail: sem trava no Windows; corrida entre processos no PC só perde contagem de uso/cache.
        # Se o tick/webhook passar a rodar no Windows, trocar por msvcrt.locking.
        yield
        return
    with open(_pasta() / f"{nome}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ler(nome):
    try:
        return json.loads((_pasta() / nome).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar(nome, dados):
    arq = _pasta() / nome
    tmp = arq.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dados), encoding="utf-8")
    os.replace(tmp, arq)


# ---- medida ----

def _hora(agora):
    return time.strftime("%Y-%m-%dT%H", time.gmtime(agora))


def _inteiro(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pct(r, rotulo):
    limite = r.get("limite") or LIMITE_PADRAO.get(rotulo, 2500)
    usado = r["n"]
    if r.get("restante") is not None and r.get("limite"):
        usado = max(usado, limite - r["restante"])
    return usado / limite


def registrar(rotulo, cabecalhos=None, agora=None):
    """Conta uma chamada da chave `rotulo` na hora atual. Devolve a fração usada do limite (0-1)."""
    agora = agora or time.time()
    h = _hora(agora)
    with _trava("uso"):
        d = _ler("uso.json")
        horas = d.setdefault("horas", {})
        r = horas.setdefault(h, {}).setdefault(rotulo, {"n": 0})
        r["n"] += 1
        if cabecalhos is not None:
            lim = _inteiro(cabecalhos.get("X-RateLimit-Requests-Limit"))
            rest = _inteiro(cabecalhos.get("X-RateLimit-Requests-Remaining"))
            if lim:
                r["limite"] = lim
            if rest is not None:
                r["restante"] = rest
        for velha in sorted(horas)[:-MAX_HORAS]:
            horas.pop(velha)
        pct = _pct(r, rotulo)
        alertar = pct >= ALARME and f"{h}|{rotulo}" not in d.setdefault("alertas", {})
        if alertar:
            d["alertas"] = {f"{h}|{rotulo}": 1}  # um alarme por chave por hora
        _gravar("uso.json", d)
    if alertar:
        _alarme(rotulo, pct, r["n"])
    return pct


def _alarme(rotulo, pct, n):
    from hub import consertos  # import tardio: consertos puxa módulos que usam o linear
    with contextlib.suppress(Exception):
        consertos.registrar(f"Linear: chave {rotulo} em {pct:.0%} do limite da hora ({n} chamadas medidas)",
                            "uso acima de 70% do limite por hora (KRN-986)",
                            "ver `python3 -m hub.linear uso` e quem consulta demais", "tentando", por="linear-uso")


def uso(agora=None, horas=24):
    """{rotulo: {"hora": n, "pct_hora": f, "limite": n, "restante": n|None, "pico_pct": f, "pico_hora": h, "total": n}}
    das últimas `horas` (a hora corrente em "hora"/"pct_hora")."""
    agora = agora or time.time()
    d = _ler("uso.json").get("horas", {})
    corte = _hora(agora - horas * 3600)
    h0 = _hora(agora)
    saida = {}
    for h in sorted(d):
        if h < corte:
            continue
        for rotulo, r in d[h].items():
            s = saida.setdefault(rotulo, {"hora": 0, "pct_hora": 0.0, "limite": None, "restante": None, "pico_pct": 0.0,
                                          "pico_hora": None, "total": 0})
            p = _pct(r, rotulo)
            s["total"] += r["n"]
            if p >= s["pico_pct"]:
                s["pico_pct"], s["pico_hora"] = p, h
            if h == h0:
                s.update(hora=r["n"], pct_hora=p, limite=r.get("limite") or LIMITE_PADRAO.get(rotulo),
                         restante=r.get("restante"))
    return saida


def texto_uso(agora=None):
    u = uso(agora)
    if not u:
        return "Linear: nenhuma chamada medida nas últimas 24 h."
    linhas = []
    for rotulo, s in sorted(u.items()):
        linhas.append(f"{rotulo}: {s['hora']} chamadas nesta hora ({s['pct_hora']:.0%} do limite), pico {s['pico_pct']:.0%} "
                      f"às {s['pico_hora']} UTC, {s['total']} em 24 h")
    return "\n".join(linhas)


# ---- cache ----

def _arq_cache(consulta, variaveis):
    h = hashlib.sha256(json.dumps([consulta, variaveis], sort_keys=True, default=str).encode()).hexdigest()[:32]
    p = _pasta() / "cache"
    p.mkdir(exist_ok=True)
    return p / f"{h}.json"


def cache_ler(consulta, variaveis, agora=None):
    try:
        c = json.loads(_arq_cache(consulta, variaveis).read_text(encoding="utf-8"))
        if c["ate"] > (agora or time.time()):
            return c["dados"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def cache_gravar(consulta, variaveis, dados, ttl, agora=None):
    arq = _arq_cache(consulta, variaveis)
    tmp = arq.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"ate": (agora or time.time()) + ttl, "dados": dados}), encoding="utf-8")
    os.replace(tmp, arq)


def cache_limpar():
    for f in (_pasta() / "cache").glob("*.json"):
        with contextlib.suppress(OSError):
            f.unlink()


# ---- webhook ----

def _ids_do_evento(evento):
    tipo, dado = evento.get("type"), evento.get("data") or {}
    if tipo == "Issue":
        ids = [dado.get("id")]
    elif tipo == "Comment":
        ids = [dado.get("issueId") or (dado.get("issue") or {}).get("id")]
    elif tipo == "AgentSessionEvent":
        ids = [((evento.get("agentSession") or {}).get("issue") or {}).get("id")]
    else:
        ids = []
    return [i for i in ids if i]


def hook_registrar(evento, agora=None):
    """Evento assinado recebido: marca o webhook como vivo e guarda a issue para o próximo tick. Evento sem issue
    (update de projeto, tipo novo) pede uma varredura completa."""
    agora = agora or time.time()
    ids = _ids_do_evento(evento)
    with _trava("hook"):
        e = _ler("webhook.json")
        e["ultimo"] = agora
        for i in ids:
            e.setdefault("pendentes", {})[i] = agora
        if not ids:
            e["completo"] = True
        _gravar("webhook.json", e)


def hook_devolver(ids, agora=None):
    """Issue que o tick não resolveu (fila cheia, erro): fica para o próximo tick, sem esperar os 15 min."""
    ids = [i for i in ids if i]
    if not ids:
        return
    agora = agora or time.time()
    with _trava("hook"):
        e = _ler("webhook.json")
        for i in ids:
            e.setdefault("pendentes", {})[i] = agora
        _gravar("webhook.json", e)


def plano_tick(agora=None):
    """None = varredura completa; lista = só estas issues (vazia: nada a fazer neste tick).
    Completa quando o webhook está mudo há mais de 5 min, passaram 15 min da última completa ou veio evento sem issue."""
    agora = agora or time.time()
    with _trava("hook"):
        e = _ler("webhook.json")
        vivo = agora - e.get("ultimo", 0) <= HOOK_VIVO_S
        intervalo = VARREDURA_S if vivo else VARREDURA_MUDO_S
        if agora - e.get("ultima_completa", 0) >= intervalo or e.get("completo"):
            e.update(ultima_completa=agora, pendentes={}, completo=False)
            _gravar("webhook.json", e)
            return None
        ids = list(e.get("pendentes", {}))
        e["pendentes"] = {}
        _gravar("webhook.json", e)
        return ids
