"""Armazém do BI: fatos diários de todos os negócios em $HUB_DATA/bi/metricas.db (SQLite, só biblioteca padrão).
Contrato em docs/plans/feitos/2026-10-09-bi-supremo.md. Reconstruível a partir dos arquivos (hub/bi_ingest.py --tudo)."""
import datetime as dt
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from hub import data

ESQUEMA = """create table if not exists fato (dia text not null, negocio text not null, fonte text not null,
  metrica text not null, dim text not null default '', valor real not null,
  primary key (dia, negocio, fonte, metrica, dim))"""
_DIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def caminho(base_dados=None):
    return Path(base_dados or data()) / "bi" / "metricas.db"


def abrir(base_dados=None):
    """Quem chama é dono da conexão e deve fechá-la (as funções deste módulo já fecham)."""
    p = caminho(base_dados)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)  # journal padrão (sem WAL): banco WAL não abre em diretório só leitura
    c.execute(ESQUEMA)
    return c


def _ler(base_dados=None):
    """Conexão só leitura: o painel (User=claude, ProtectSystem=strict) não escreve em bi/. Sem banco ainda: vazio."""
    p = caminho(base_dados)
    if not p.exists():
        c = sqlite3.connect(":memory:")
        c.execute(ESQUEMA)
        return c
    return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def gravar(fatos, base_dados=None):
    linhas = []
    for f in fatos:
        if not _DIA.match(str(f.get("dia", ""))) or not all(f.get(k) for k in ("negocio", "fonte", "metrica")):
            raise ValueError(f"fato inválido: {f}")
        try:
            valor = float(f["valor"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"fato inválido (valor): {f}") from None
        linhas.append((f["dia"], f["negocio"], f["fonte"], f["metrica"], f.get("dim") or "", valor))
    with closing(abrir(base_dados)) as c:
        c.executemany("insert into fato values (?,?,?,?,?,?) on conflict do update set valor = excluded.valor", linhas)
        c.commit()
    return len(linhas)


def _dias(de, ate):
    d, fim = dt.date.fromisoformat(de), dt.date.fromisoformat(ate)
    while d <= fim:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def serie(negocio, fonte, metrica, de, ate, dim="", base_dados=None):
    with closing(_ler(base_dados)) as c:
        tem = dict(c.execute("select dia, valor from fato where negocio=? and fonte=? and metrica=? and dim=? "
                             "and dia between ? and ?", (negocio, fonte, metrica, dim, de, ate)))
    return [(d, tem.get(d)) for d in _dias(de, ate)]


def soma(negocio, fonte, metrica, de, ate, dim="", base_dados=None):
    with closing(_ler(base_dados)) as c:
        s, n = c.execute("select coalesce(sum(valor), 0), count(*) from fato where negocio=? and fonte=? and metrica=? "
                         "and dim=? and dia between ? and ?", (negocio, fonte, metrica, dim, de, ate)).fetchone()
    return float(s), n


def ultimo(negocio, fonte, metrica, base_dados=None):
    """Último dia com algum dado; valor = linha sem recorte (dim='') se houver, senão a soma dos recortes."""
    with closing(_ler(base_dados)) as c:
        r = c.execute("select dia, coalesce(sum(case when dim='' then valor end), sum(valor)) from fato "
                      "where negocio=? and fonte=? and metrica=? group by dia order by dia desc limit 1",
                      (negocio, fonte, metrica)).fetchone()
    return (r[0], float(r[1])) if r else None


def primeiro(negocio, fonte, metrica, base_dados=None):
    """Primeiro dia com total do dia (dim=''); None sem nenhum."""
    with closing(_ler(base_dados)) as c:
        return c.execute("select min(dia) from fato where negocio=? and fonte=? and metrica=? and dim=''",
                         (negocio, fonte, metrica)).fetchone()[0]


def dims(negocio, fonte, metrica, de, ate, base_dados=None):
    with closing(_ler(base_dados)) as c:
        return [(d, float(v)) for d, v in c.execute(
            "select dim, sum(valor) from fato where negocio=? and fonte=? and metrica=? and dim<>'' and dia between ? and ? "
            "group by dim order by 2 desc", (negocio, fonte, metrica, de, ate))]
