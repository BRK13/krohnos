"""Limite de taxa do painel (4.4), só stdlib.

Login: erros por IP+e-mail (5) e por IP sozinho (20) numa janela de 15 min. A conta é feita ANTES do scrypt (força bruta
não vira CPU cheia), checar e reservar numa trava só (tentar): rajada paralela não passa do limite. Os erros ficam na memória e na tabela tentativa do usuarios.db, então o bloqueio sobrevive ao restart;
a memória carrega a tabela na 1ª consulta. O e-mail entra na chave só como sha256 curto; a senha nunca.
POSTs gerais (/acao/*, /sair, /convite): 60 por minuto por sessão (ou IP), janela deslizante, só memória.
"""
import collections
import datetime as dt
import hashlib
import sqlite3
import sys
import threading
import time

from hub import usuarios

JANELA = dt.timedelta(minutes=15)
POR_IP_EMAIL = 5
POR_IP = 20
ESPERA = 900  # segundos do Retry-After
MSG = "Muitas tentativas. Tente de novo em 15 minutos."
POST_JANELA = 60.0
POST_MAX = 60

VARRE = dt.timedelta(seconds=60)  # varredura completa da memória no máximo 1 vez por minuto
POST_VARRE = 10.0

_trava = threading.Lock()
_erros = {}  # caminho do usuarios.db -> {chave: [quando]} (inclui as tentativas em curso, reservadas)
_varrido = {}  # caminho -> última varredura completa
_avisos = {}  # (caminho, chave) -> quando o 1º 429 da janela foi auditado
_posts = {}  # chave -> deque de time.monotonic()
_post_varrido = [0.0]


def agora():
    return dt.datetime.now(dt.timezone.utc)


def esquecer():
    """Zera a memória (processo novo; testes). A tabela fica."""
    with _trava:
        _erros.clear()
        _varrido.clear()
        _avisos.clear()
        _posts.clear()
        _post_varrido[0] = 0.0


def _chaves(ip, email):
    e = (email or "").strip().lower()
    return f"ip:{ip}", f"ip-email:{ip}|{hashlib.sha256(e.encode()).hexdigest()[:16]}"


def _mem(t):
    """Memória do banco atual (a 1ª vez carrega da tabela os últimos 15 min). Varre tudo no máximo 1 vez por VARRE;
    no resto só as chaves consultadas são podadas (_contar). Chamar com a trava."""
    caminho = str(usuarios.banco())
    corte = t - JANELA
    m = _erros.get(caminho)
    if m is None:
        m = {}
        try:
            con = usuarios._con()
            try:
                for chave, em in con.execute("SELECT chave, em FROM tentativa WHERE em >= ?", (corte.isoformat(),)):
                    m.setdefault(chave, []).append(dt.datetime.fromisoformat(em))
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError) as ex:
            print(f"limite: não li as tentativas ({type(ex).__name__})", file=sys.stderr)
        _erros[caminho] = m
        _varrido[caminho] = t
    elif t - _varrido.get(caminho, t) >= VARRE:
        for k in list(m):
            _contar(m, k, corte)
        for k in [k for k, v in _avisos.items() if v < corte]:
            del _avisos[k]
        _varrido[caminho] = t
    return caminho, m


def _contar(m, k, corte):
    """Poda a chave e devolve quantos erros (e reservas) ela tem desde o corte."""
    v = m.get(k)
    if not v:
        return 0
    v = [x for x in v if x >= corte]
    if v:
        m[k] = v
    else:
        del m[k]
    return len(v)


def tentar(ip, email):
    """Numa trava só: (reserva, avisar). Bloqueado (5 do IP+e-mail ou 20 do IP nos últimos 15 min, contando as
    tentativas ainda em curso): reserva None; avisar = é o 1º 429 desta chave na janela (só ele vai à auditoria).
    Livre: reserva = o instante anotado nas duas chaves; depois falhou() grava, zerar() ou liberar() devolve."""
    ip_k, ie_k = _chaves(ip, email)
    t = agora()
    corte = t - JANELA
    with _trava:
        caminho, m = _mem(t)
        chave = ie_k if _contar(m, ie_k, corte) >= POR_IP_EMAIL else ip_k if _contar(m, ip_k, corte) >= POR_IP else None
        if chave:
            ultimo = _avisos.get((caminho, chave))
            avisar = ultimo is None or ultimo < corte
            if avisar:
                _avisos[(caminho, chave)] = t
            return None, avisar
        for k in (ip_k, ie_k):
            m.setdefault(k, []).append(t)
        return t, False


def _tirar(ip, email, t, tudo_ie=False):
    ip_k, ie_k = _chaves(ip, email)
    with _trava:
        _, m = _mem(agora())
        for k in (ip_k,) if tudo_ie else (ip_k, ie_k):
            if t in m.get(k, ()):
                m[k].remove(t)
        if tudo_ie:
            m.pop(ie_k, None)


def liberar(ip, email, t):
    """A tentativa não chegou a conferir a senha (CPU ocupada, erro): a reserva some, nada conta."""
    _tirar(ip, email, t)


def falhou(ip, email, t):
    """Senha errada: a reserva (já contada na memória) vai para a tabela; o que passou da janela sai dela."""
    corte = agora() - JANELA
    try:
        con = usuarios._con()
        try:
            with con:
                con.executemany("INSERT INTO tentativa(chave, em) VALUES (?,?)", [(k, t.isoformat()) for k in _chaves(ip, email)])
                con.execute("DELETE FROM tentativa WHERE em < ?", (corte.isoformat(),))
        finally:
            con.close()
    except (sqlite3.Error, OSError) as ex:
        print(f"limite: não gravei a tentativa ({type(ex).__name__})", file=sys.stderr)


def zerar(ip, email, t):
    """Login certo: some com os erros daquele IP+e-mail e com a reserva no IP (o resto do teto do IP segue)."""
    _, ie_k = _chaves(ip, email)
    _tirar(ip, email, t, tudo_ie=True)
    try:
        con = usuarios._con()
        try:
            with con:
                con.execute("DELETE FROM tentativa WHERE chave=?", (ie_k,))
        finally:
            con.close()
    except (sqlite3.Error, OSError) as ex:
        print(f"limite: não zerei as tentativas ({type(ex).__name__})", file=sys.stderr)


def post_ok(chave, t=None):
    """True se a chave (sessão ou IP) ainda cabe nos 60 POSTs do último minuto; conta este. O recusado não conta."""
    t = time.monotonic() if t is None else t
    with _trava:
        d = _posts.setdefault(chave, collections.deque())
        while d and d[0] <= t - POST_JANELA:
            d.popleft()
        if len(d) >= POST_MAX:
            return False
        d.append(t)
        if len(_posts) > 5000 and t - _post_varrido[0] >= POST_VARRE:  # sem crescer sem fim; varre no máximo a cada 10 s
            _post_varrido[0] = t
            for k in [k for k, v in _posts.items() if not v or v[-1] <= t - POST_JANELA]:
                del _posts[k]
        return True
