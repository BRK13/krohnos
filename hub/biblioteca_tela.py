"""Tela Biblioteca: busca nas receitas do <org>/biblioteca, filtro por tema e projeto, receita em Markdown
com blocos de código copiáveis. HTML só por ui.*; texto de fora sempre escapado."""
import urllib.parse
from html import escape as e

from hub import config, biblioteca as B, biblioteca_indice as I, biblioteca_uso as U, ui

STATUS = {"completo": "ok", "reconstruído": "alerta", "reconstruido": "alerta", "fechado": "neutro"}


def _href(caminho):
    return "/biblioteca/" + urllib.parse.quote(caminho.removeprefix("receitas/"))


def _url(atual, **troca):
    v = atual | troca
    qs = urllib.parse.urlencode({k: x for k, x in v.items() if x})
    return "/biblioteca" + (f"?{qs}" if qs else "")


def _meta(x):
    st = (x.get("status") or "").split(" ")[0].strip("()").lower()
    partes = [ui.selo(x["tema"], mono=True)]
    if x["projetos"]:
        partes.append(e(", ".join(B.PROJETOS[p][0] for p in x["projetos"])))
    if st:
        partes.append(ui.selo(st, STATUS.get(st, "neutro")))
    if x.get("data"):
        partes.append(e(x["data"]))
    return " ".join(partes)


def _issue(x):
    return f'<a href="https://linear.app/issue/{e(x)}">{e(x)}</a>' if x else "—"


def uso(dias=30, r=None, db=None):
    """Aba "Uso": por receita, em quais issues o despacho injetou e em qual PR/issue ela foi aplicada; as que ficaram
    `dias` dias sem PR citando "Receita: receitas/…" vão num cartão à parte."""
    rel = U.relatorio(dias, r=r, db=db)
    base = ui.kpis(ui.kpi("Issues com receita", str(rel["issues_com_receita"]), nota=f"de {rel['issues_consultadas']} despachadas"),
                   ui.kpi("PRs com receita", str(rel["prs_com_receita"]), nota="citam \"Receita: receitas/…\""),
                   ui.kpi(f"Sem uso em {dias} dias", str(len(rel["sem_uso"])), nota=f"de {len(rel['receitas'])} receitas", quente=bool(rel["sem_uso"])))
    usadas = [x for x in rel["receitas"] if x["usada_em"]]
    linhas = []
    for x in sorted(usadas, key=lambda x: x["ultimo_uso"], reverse=True):
        aplic = " ".join(f'{_issue(u["issue"])} · <a href="{e(u["pr"])}">PR</a> ({e(u["data"])})' for u in x["usada_em"][:5])
        linhas.append([ui.td(f'<a href="{_href(x["caminho"])}">{e(x["titulo"])}</a>', html_=True),
                       ui.td(str(len(x["injetada_em"])), n=True), ui.td(aplic, html_=True)])
    tab = (ui.tabela(["Receita", "Injetada em", "Aplicada em"], linhas, num=(1,), celular="cartoes") if linhas
           else ui.vazio("Nenhuma receita citada em PR ainda.", "O PR que usa receita cita \"Receita: receitas/<tema>/<arquivo>.md\" no corpo."))
    sem = [x for x in rel["receitas"] if x["caminho"] in set(rel["sem_uso"])]
    itens = [{"titulo": x["titulo"], "href": _href(x["caminho"]), "meta": _meta(x),
              "fala": f"injetada em {len(x['injetada_em'])} issue(s)" if x["injetada_em"] else "nunca injetada"} for x in sem]
    return (base + ui.card("Receitas aplicadas", tab, conta=len(usadas), justa=bool(linhas))
            + ui.card(f"Sem uso em {dias} dias", ui.lista(itens, solta=True) if itens else ui.vazio("Todas foram usadas."), conta=len(sem)))


def abas(vista):
    return ui.filtro("vista", [("", "Receitas"), ("uso", "Uso")], vista, abas=True, url=lambda v: "/biblioteca" + (f"?vista={v}" if v else ""))


def tela(q="", tema="", projeto="", vista=""):
    if vista == "uso":
        return abas(vista) + uso()
    todas = B.receitas()
    if not todas:
        return ui.card("Biblioteca", ui.vazio("Biblioteca ainda não clonada.",
                                              f"O cron de 10 min (deploy/biblioteca.sh) clona o {config.repo('biblioteca')} na primeira volta."))
    temas = B.temas(todas)
    tema = tema if tema in temas else ""
    projeto = projeto if projeto in B.PROJETOS else ""
    achados = I.buscar_tela(q, tema or None, projeto or None, limite=500)
    atual = {"q": q, "tema": tema, "projeto": projeto}
    busca = (f'<form class="form" method="get" action="/biblioteca" role="search">'
             f'<input class="largo" type="search" name="q" value="{e(q)}" maxlength="200" placeholder="Buscar nas receitas (ex.: gsap scroll, remotion, schema)" aria-label="Buscar">'
             + (f'<input type="hidden" name="tema" value="{e(tema)}">' if tema else "")
             + (f'<input type="hidden" name="projeto" value="{e(projeto)}">' if projeto else "")
             + '<button class="prim" type="submit">Buscar</button></form>')
    filtros = '<div class="form">' + (ui.filtro("tema", [("", "Todos")] + [(t, t) for t in temas], tema, rotulo="Tema",
                         url=lambda v: _url(atual, tema=v))
               + ui.filtro("projeto", [("", "Todos")] + [(k, n) for k, (n, _) in B.PROJETOS.items()], projeto,
                           rotulo="Onde aplicar", url=lambda v: _url(atual, projeto=v))) + "</div>"
    itens = [{"titulo": x["titulo"], "href": _href(x["caminho"]), "meta": _meta(x), "fala": x["trecho"]} for x in achados]
    lista = ui.lista(itens, solta=True) if itens else ui.vazio("Nada casou.", "Tire um filtro ou troque as palavras.")
    conta = f"{len(achados)} de {len(todas)}" if (q or tema or projeto) else str(len(todas))
    return abas(vista) + ui.card("Buscar", f'<div class="pilha">{busca}{filtros}</div>') + ui.card("Receitas", lista, conta=conta)


def receita(caminho):
    """(titulo, html) da receita ou None."""
    texto = B.ler(caminho)
    if texto is None:
        return None
    rel = "receitas/" + caminho.removeprefix("receitas/")
    x = next((r for r in B.receitas() if r["caminho"] == rel), None)
    titulo = x["titulo"] if x else rel
    meta = (f'<p class="mu">{_meta(x)}</p>' if x else "")
    gh = f"https://github.com/{B.REPO}/blob/main/{urllib.parse.quote(rel)}"
    acoes = ui.botao("Voltar", href="/biblioteca") + ui.botao("Abrir no GitHub", href=gh)
    return titulo, meta + ui.card(rel, ui.md(texto), direita=acoes)
