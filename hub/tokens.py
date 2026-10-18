"""Tokens do hub por issue, tipo de execução e modelo: a aba "Tokens" do /uso e a tabela antes × depois.

Lê só o uso.jsonl. Cada registro novo traz `tipo` (de onde a execução veio: primeira, fala, espera_ci, espera_deploy,
conferencia, ...), gravado pela agenda a partir do motivo da fila (`tipo_execucao`); registro antigo, sem o campo,
entra como "sem_tipo". Custo = o custo equivalente de API que a CLI informa (custo_usd); o Codex não informa e fica
fora dos dólares, mas entra nos tokens.

    python3 -m hub.tokens dia [--horas 24]                       # por tipo, por modelo e o top 10 de issues
    python3 -m hub.tokens antes-depois --corte <ISO> [--horas 48] # tabela para o comentário de 48 h
    python3 -m hub.tokens alertas [--horas 24]                    # issues acima de 5% do custo do dia
"""
import argparse
import collections
import datetime as dt
import re
import statistics
import sys

from hub import conta

ALERTA_PCT = 5.0  # issue que passa disto do custo do dia aparece em alerta no painel
CURTA_S = 90  # execução curta: em geral o "acordar à toa" (volto quando o CI sair, nada mudou)
CAMPOS = ("entrada", "cache_leitura", "cache_escrita", "saida")

# motivo da fila (agenda.enfileirar) → tipo; o primeiro que casar vale
_TIPOS = (
    (r"^voltou da espera do CI", "espera_ci"),
    (r"^volta: conferência", "conferencia"),
    (r"^volta: promessa", "promessa"),
    (r"^volta: fala", "fala_durante"),
    (r"^volta: ", "conferencia"),  # motivo antigo ("volta: conferência ou fala nova")
    (r"^voltou de Done", "recibo"),
    (r"^merge mecânico", "merge"),
    (r"^alarme de tarefa parada", "parada"),
    (r"^voltou para a fila: limite", "limite"),
    (r"^voltou para a fila", "interrompida"),
    (r"^proposta ", "proposta"),
    (r"^retomada após a pausa", "disjuntor"),
    (r"^Debugger", "debugger"),
    (r"^meta ", "meta"),
    (r"^agenda ", "agendada"),
    (r"^replay do passo ", "manual"),  # o dono pediu no painel ou na CLI
)


# o conjunto fechado de tipos. Toda execução grava um deles; o que não casa vira "outro" (e incidente em
# `retomada.exigir_tipo`), nunca vazio. Retomada = a execução que acordou uma issue já iniciada (não a primeira nem a fala).
TIPOS_RETOMADA = frozenset({"espera_ci", "espera_deploy", "conferencia", "promessa", "fala_durante", "recibo", "merge",
                            "parada", "disjuntor", "interrompida", "limite"})
TIPOS = TIPOS_RETOMADA | frozenset({"primeira", "fala", "proposta", "debugger", "meta", "agendada", "manual", "outro"})


def tipo_execucao(motivo, item=None):
    """Tipo da execução pelo motivo com que entrou na fila e pelo item do Linear (se houver). Sempre um de TIPOS."""
    tipo = _tipo_execucao(motivo, item)
    return tipo if tipo in TIPOS else "outro"


def _tipo_execucao(motivo, item=None):
    motivo = (motivo or "").strip()
    if motivo.startswith("voltou da espera do CI") and (item or {}).get("roteado_por") == "retomada de deploy":
        return "espera_deploy"
    for padrao, tipo in _TIPOS:
        if re.search(padrao, motivo):
            return tipo
    if motivo.startswith("linear ") or (item and not motivo):
        # já respondeu antes nesta issue: é volta por fala (do dono, repasse ou sessão); senão, a primeira
        ja = any(str(c).startswith("Claude: ") for c in (item or {}).get("comentarios") or [])
        return "fala" if ja else "primeira"
    return "manual" if motivo.startswith("manual") else ("outro" if motivo else "agendada")


def _tok(r, k):
    return int(((r.get("tokens") or {}).get(k)) or 0)


def novos(r):
    """Entrada nova da execução: tokens que não vieram do cache (entrada + escrita no cache)."""
    return _tok(r, "entrada") + _tok(r, "cache_escrita")


def issue_de(r):
    nome = str(r.get("nome") or "")
    return nome.removeprefix("linear-").upper() if nome.startswith("linear-") else nome


def _janela(usos, agora, horas):
    desde = agora - dt.timedelta(hours=horas)
    return [r for r in usos if _quando(r) and desde <= _quando(r) <= agora]


def _quando(r):
    try:
        return conta.de_iso(r["inicio"])
    except (KeyError, TypeError, ValueError):
        return None


def _vazio():
    return {"execucoes": 0, "custo_usd": 0.0, "curtas": 0, **dict.fromkeys(CAMPOS, 0)}


def _somar(g, r):
    g["execucoes"] += 1
    g["custo_usd"] += float(r.get("custo_usd") or 0)
    g["curtas"] += 1 if (r.get("duracao_s") or 0) < CURTA_S else 0
    for k in CAMPOS:
        g[k] += _tok(r, k)


def resumo(usos, agora=None, horas=24):
    """{total, por_tipo, por_modelo, top (10 issues mais caras), alertas (issues > ALERTA_PCT do custo)}."""
    agora = agora or conta.agora()
    janela = _janela(usos, agora, horas)
    total, por_tipo, por_modelo, por_issue = _vazio(), {}, {}, {}
    for r in janela:
        _somar(total, r)
        _somar(por_tipo.setdefault(r.get("tipo") or "sem_tipo", _vazio()), r)
        _somar(por_modelo.setdefault(str(r.get("modelo") or "?"), _vazio()), r)
        _somar(por_issue.setdefault(issue_de(r), _vazio()), r)
    custo = total["custo_usd"] or 0.0
    for g in por_issue.values():
        g["pct"] = 100 * g["custo_usd"] / custo if custo else 0.0
    ordem = sorted(por_issue.items(), key=lambda kv: (-kv[1]["custo_usd"], -kv[1]["cache_leitura"]))
    top = [{"issue": k, **v} for k, v in ordem[:10]]
    alertas = [{"issue": k, **v} for k, v in ordem if v["pct"] > ALERTA_PCT]
    return {"horas": horas, "total": total, "por_tipo": por_tipo, "por_modelo": por_modelo, "top": top, "alertas": alertas}


def metricas(regs):
    """As metas da para um conjunto de execuções do Linear."""
    if not regs:
        return {"execucoes": 0}  # as outras medidas ficam 0 na tabela
    leitura = [_tok(r, "cache_leitura") for r in regs]
    nov = [novos(r) for r in regs]
    return {"execucoes": len(regs), "custo_usd": round(sum(float(r.get("custo_usd") or 0) for r in regs), 2),
            "curtas": sum(1 for r in regs if (r.get("duracao_s") or 0) < CURTA_S),
            "cache_leitura_mediana": int(statistics.median(leitura)), "cache_leitura_media": int(statistics.mean(leitura)),
            "novos_mediana": int(statistics.median(nov)), "novos_media": int(statistics.mean(nov)),
            "opus": sum(1 for r in regs if r.get("modelo") == "opus")}


def _do_linear(usos):
    return [r for r in usos if str(r.get("nome") or "").startswith("linear-") and r.get("origem") == "tarefa"]


def antes_depois(usos, corte, horas=48, agora=None):
    """Mesma janela antes e depois do corte, só execuções de issue (contas Claude). Cada lado leva as horas que de
    fato cobriu (o "depois" ainda em curso conta só até agora), para a tabela dar valores por dia."""
    janela, agora = dt.timedelta(hours=horas), agora or conta.agora()
    regs = [(q, r) for r in _do_linear(usos) if (q := _quando(r))]
    antes = metricas([r for q, r in regs if corte - janela <= q < corte])
    depois = metricas([r for q, r in regs if corte <= q < corte + janela])
    antes["horas"] = horas
    depois["horas"] = max(0.0, min(horas, (agora - corte).total_seconds() / 3600))
    return antes, depois


def tabela(antes, depois):
    def por_dia(campo):
        return lambda m: round(m.get(campo, 0) * 24 / m["horas"]) if m.get("horas") else 0

    linhas = [("Execuções por dia (issues)", por_dia("execucoes"), "< 300"),
              ("Custo equivalente por dia (US$)", por_dia("custo_usd"), ""),
              ("Execuções curtas (< 90 s) por dia", por_dia("curtas"), ""),
              ("Cache lido por execução (mediana)", lambda m: m.get("cache_leitura_mediana", 0), "< 300 mil"),
              ("Cache lido por execução (média)", lambda m: m.get("cache_leitura_media", 0), ""),
              ("Entrada nova por execução (mediana)", lambda m: m.get("novos_mediana", 0), "< 60 mil"),
              ("Execuções em Opus", lambda m: m.get("opus", 0), "")]
    saida = [f"| Medida | Antes ({antes.get('horas', 0):.0f} h) | Depois ({depois.get('horas', 0):.0f} h) | Meta |",
             "|---|---:|---:|---|"]
    for nome, f, meta in linhas:
        saida.append(f"| {nome} | {f(antes):,} | {f(depois):,} | {meta} |".replace(",", "."))
    return "\n".join(saida)


def _fmt(g):
    return (f"{g['execucoes']:>5} exec · US$ {g['custo_usd']:>8.2f} · lido {g['cache_leitura'] / 1e6:>7.1f} mi · "
            f"novo {(g['entrada'] + g['cache_escrita']) / 1e6:>6.1f} mi · saída {g['saida'] / 1e6:>5.2f} mi · curtas {g['curtas']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="hub.tokens")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dia")
    d.add_argument("--horas", type=float, default=24)
    a = sub.add_parser("antes-depois")
    a.add_argument("--corte", required=True)
    a.add_argument("--horas", type=float, default=48)
    al = sub.add_parser("alertas")
    al.add_argument("--horas", type=float, default=24)
    args = p.parse_args(argv)
    usos = conta.ler_uso()
    if args.cmd == "antes-depois":
        antes, depois = antes_depois(usos, conta.de_iso(args.corte), args.horas)
        print(tabela(antes, depois))
        return 0
    r = resumo(usos, horas=args.horas)
    if args.cmd == "alertas":
        for x in r["alertas"]:
            print(f"{x['issue']}: {x['pct']:.1f}% do dia (US$ {x['custo_usd']:.2f}, {x['execucoes']} exec)")
        return 0
    print(f"Total {args.horas:g} h: {_fmt(r['total'])}")
    for titulo, grupo in (("Por tipo", r["por_tipo"]), ("Por modelo", r["por_modelo"])):
        print(f"\n{titulo}:")
        for k, g in sorted(grupo.items(), key=lambda kv: -kv[1]["custo_usd"]):
            print(f"  {k:<14} {_fmt(g)}")
    print("\nTop 10 issues:")
    for x in r["top"]:
        print(f"  {x['issue']:<28} {x['pct']:>5.1f}% {_fmt(x)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
