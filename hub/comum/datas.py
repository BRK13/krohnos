"""Datas em Brasília e no formato pt-BR. O Brasil não tem horário de verão desde 2019: UTC-3 fixo, sem tzdata."""
import datetime as dt
import os
import re

BRT = dt.timezone(dt.timedelta(hours=-3))
UTC = dt.timezone.utc
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")
_RE_DATA = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def agora():
    """Agora, em Brasília. `HUB_AGORA` (ISO) congela o relógio: os testes do porteiro (22h-7h BRT segura o aviso) não podem
    depender da hora em que rodam."""
    fixo = os.environ.get("HUB_AGORA")
    return dt.datetime.fromisoformat(fixo).astimezone(BRT) if fixo else dt.datetime.now(BRT)


def hoje():
    return agora().date()


def em_brt(t):
    """O instante `t` (datetime com fuso, ou naive tratado como UTC) em Brasília."""
    return t.replace(tzinfo=UTC).astimezone(BRT) if t.tzinfo is None else t.astimezone(BRT)


def data_br(texto):
    """'dd/mm/aaaa' (em qualquer lugar do texto, ex. '05/03/2024 14:00') -> date; None se não achar ou for data impossível."""
    m = _RE_DATA.search(texto or "")
    if not m:
        return None
    d, mes, a = map(int, m.groups())
    try:
        return dt.date(a, mes, d)
    except ValueError:
        return None


def fmt_data(d):
    """date/datetime -> 'dd/mm/aaaa' (datetime com fuso vai para Brasília antes)."""
    if isinstance(d, dt.datetime) and d.tzinfo:
        d = d.astimezone(BRT)
    return f"{d:%d/%m/%Y}"


def fmt_data_hora(t):
    """datetime -> 'dd/mm/aaaa HH:MM' em Brasília."""
    return f"{em_brt(t):%d/%m/%Y %H:%M}"
