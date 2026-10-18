"""Tela Metas e faixa "Nave" das Pendências. HTML só por ui.*; texto de fora sempre escapado."""
from html import escape as e

from hub import metas, sites, ui, velocidade

ESTADO = {"ativa": "neutro", "batida": "ok", "travada": "ruim", "definir": "alerta"}


def _v(meta, v):
    """Valor no formato da meta: ms vira segundos."""
    if v is None:
        return "—"
    u = meta.get("unidade") or ""
    return ui.num(v / 1000, casas=1) + " s" if u == "ms" else ui.num(v, casas=1 if v != int(v) else 0) + (f" {u}" if u else "")


def _acumulado(meta):
    out, b = [], None
    for r in meta.get("rodadas") or []:
        if r.get("conta"):
            b = r["valor"] if b is None else (min if meta["direcao"] == "menor" else max)(b, r["valor"])
        out.append(b)
    return out


def _card(m):
    n, b, f = len(m.get("rodadas") or []), metas.melhor(m), metas.faltam(m)
    ref = m.get("issue")
    linha = [f'{e(_v(m, b))} × {e(_v(m, m.get("alvo")))}' if m.get("alvo") else e(_v(m, b))]
    if f is not None:
        linha.append(f"faltam {e(_v(m, f))}")
    linha.append(f"rodada {n}/{metas.TETO}")
    if ref:
        linha.append(f'<a href="{e(m.get("url_issue") or "#")}">{e(ref)}</a>' if str(m.get("url_issue") or "").startswith("https://")
                     else e(ref))
    k = 1000 if m.get("unidade") == "ms" else 1
    acum = [None if x is None else x / k for x in _acumulado(m)]
    alvo = (m["alvo"] or 0) / k
    pts = [x for x in acum if x is not None] + [alvo]
    # eixo sem o zero: o progresso ocupa a altura toda (a faixa de 10% em volta é só margem)
    graf = ui.grafico([acum, [alvo] * n], tipo="linha", legenda=("Melhor resultado", "Meta"), minimo=min(pts) * .9,
                      maximo=max(pts) * 1.05, unidade=" s" if k == 1000 else (f" {m['unidade']}" if m.get("unidade") else ""),
                      rotulo=m.get("objetivo", "")) if n and m.get("alvo") else ui.vazio("Esperando a linha de base.")
    corpo = f'<p>{ui.selo(m.get("estado", ""), ESTADO.get(m.get("estado"), "neutro"))} {" · ".join(linha)}</p>{graf}'
    return ui.card(m.get("objetivo", ""), corpo, id_=m["id"])


def tela(form_html, base_dados=None):
    ms = metas.listar(base_dados)
    c = lambda s: sum(1 for m in ms if m.get("estado") == s)
    corpo = ui.kpis(ui.kpi("Ativas", ui.num(c("ativa")), contar=c("ativa")), ui.kpi("Batidas", ui.num(c("batida")), contar=c("batida")),
                    ui.kpi("Travadas", ui.num(c("travada")), contar=c("travada")))
    corpo += ui.card("Nova meta", form_html)
    cards = ""
    for m in ms[::-1]:  # a mais nova primeiro (o id começa pela data)
        try:
            cards += _card(m)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # meta com campo faltando: pula, não derruba a tela
    return corpo + (cards or ui.card("Metas", ui.vazio("Nenhuma meta ainda.", "Crie a primeira no formulário acima.")))


def faixa(base_dados=None):
    itens = []
    for m in [m for m in metas.listar(base_dados) if m.get("estado") == "ativa"][:4]:
        obj = m.get("objetivo", "")
        try:
            itens.append(ui.kpi(obj if len(obj) <= 28 else obj[:27] + "…", _v(m, metas.melhor(m)),
                                nota=f"alvo {e(_v(m, m.get('alvo')))}", href=f"/metas#{e(m['id'])}",
                                tom={"batida": "ok", "travada": "ruim"}.get(m["estado"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    for s in sites.ativos():
        tom, pior = velocidade.saude(s["id"], base_dados)
        itens.append(ui.kpi(s.get("nome") or s["id"], "—" if pior is None else ui.num(pior / 1000, casas=1) + " s",
                            nota="LCP mobile, pior página" if pior is not None else "sem coleta ainda", tom=tom, href=f"/bi/{s['id']}"))
    return ui.kpis(*itens, miudos=True) if itens else ""
