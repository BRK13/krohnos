"""Tela "Execução #id" (F8): linha do tempo dos passos do agente e o estado em cada passo (`?passo=N`).
HTML só por ui.*; todo texto vindo do stream passa por escape (ui.td/ui.selo/ui.card escapam; o resto, por `e`)."""
from html import escape as e

from hub import rastreio as R, ui

RESUMO = 80
MAX_ANTERIORES = 200  # só os últimos passos/falas anteriores entram na tela; o resto vira aviso


def _dur(p):
    return "—" if p["duracao_s"] is None else f"{p['duracao_s']:.1f}s"


def _link(eid, n, texto):
    return f'<a href="/execucao/{e(eid)}?passo={int(n)}">{e(str(texto))}</a>'


def _linha(eid, p, atual):
    nome = p["ferramenta"] or p["tipo"] or "?"
    ent = p["entrada"] if p["tipo"] == "ferramenta" else p["texto"]
    marca = ui.selo("erro", "ruim") if p["erro"] else ""
    return [ui.td(_link(eid, p["n"], p["n"]) + (" ←" if p["n"] == atual else ""), n=True, html_=True),
            ui.td(nome), ui.td(R._uma_linha(ent, RESUMO)), ui.td(R._uma_linha(p["saida"], RESUMO)),
            ui.td(_dur(p), n=True), ui.td(marca, html_=True)]


def _bloco(titulo, texto):
    return f'<h3>{e(titulo)}</h3><pre>{e(texto if texto else "—")}</pre>'


def _omitidos(total):
    return f'<p class="mu">{total - MAX_ANTERIORES} omitidos (só os últimos {MAX_ANTERIORES}).</p>' if total > MAX_ANTERIORES else ""


def _replay(eid, n, issue, formulario):
    """Formulário POST /acao/replay (CSRF e papel pelo `_form` do painel: some para quem não pode). Sem issue, aviso."""
    if formulario is None:
        return ""
    if not issue:
        return '<p class="mu">Esta execução não é de uma issue do Linear: não dá para rodar de novo daqui.</p>'
    campos = (f'<input type="hidden" name="ref" value="{e(eid)}"><input type="hidden" name="passo" value="{int(n)}">'
              f'<textarea class="largo" name="correcao" required maxlength="{R.CORRECAO_MAX}" '
              'placeholder="O que fazer diferente a partir deste passo" aria-label="Correção"></textarea>')
    form = formulario("replay", campos, [("Rodar de novo deste passo com a correção", "ok", None, "")])
    return f"<h3>Rodar de novo ({e(issue)})</h3>{form}" if form else ""


def _passo(eid, est, n, issue=None, formulario=None):
    p = est["passo"]
    if p is None:
        return ui.card(f"Passo {n}", ui.vazio("Esse passo não existe nesta execução."))
    erro = " " + ui.selo("erro", "ruim") if p["erro"] else ""
    corpo = (f'<p>{e(p["tipo"] or "")} {e(p["ferramenta"] or "")} · duração {e(_dur(p))}{erro}</p>'
             + _bloco("Entrada", p["entrada"] or p["texto"]) + _bloco("Saída", p["saida"]))
    ant, falas = est["anteriores"], est["falas"]
    if ant:
        itens = [{"titulo": f"{a['n']} {a['resumo']}", "href": f"/execucao/{eid}?passo={a['n']}"}
                 for a in ant[-MAX_ANTERIORES:]]
        corpo += "<h3>Estado ali: passos anteriores</h3>" + _omitidos(len(ant)) + ui.lista(itens, solta=True)
    if falas:
        corpo += "<h3>Falas anteriores</h3>" + _omitidos(len(falas)) + "".join(
            f"<pre>{e(f)}</pre>" for f in falas[-MAX_ANTERIORES:])
    return ui.card(f"Passo {n}", corpo + _replay(eid, n, issue, formulario))


def tela(ref, passo=None, base_dados=None, formulario=None):
    """HTML da execução, ou None se a referência é inválida ou não existe (o painel responde 404). `formulario` é o
    `_form(acao, campos, botoes)` do painel: com ele, o passo ganha o "Rodar de novo deste passo com a correção"."""
    eid = R.resolver(ref, base_dados)
    if eid is None:
        return None
    passos = R.linha_do_tempo(eid, base_dados)
    cdx = R.codex(eid, base_dados)
    topo = ui.card("Execução", f"<p>{e(R._cabecalho(eid, base_dados))}</p>"
                   + (f'<p class="mu">{e(R.CODEX_SEM_SAIDA)}</p>' if cdx and passos else ""))
    if passos:
        tab = ui.tabela(["N", "Ferramenta", "Entrada", "Saída", "Duração", ""],
                        [_linha(eid, p, passo) for p in passos], num=(0, 4), celular="cartoes")
    elif cdx:
        tab = ui.vazio("Sem passos gravados.", R.CODEX_VAZIO)
    else:
        tab = ui.vazio("Sem passos gravados.", "O stream desta execução ainda não foi indexado.")
    out = topo + ui.card("Linha do tempo", tab, conta=len(passos), justa=bool(passos))
    if passo is not None:
        out += _passo(eid, R.estado_no_passo(eid, passo, base_dados), passo, R.issue_da_execucao(eid, base_dados),
                      formulario)
    return out


def lista(limite=50, base_dados=None):
    # inclui execuções gravadas só em `passo` (sem linha em `indexado`); essas ficam depois, sem cabeçalho
    rs = R._consulta(base_dados, "SELECT * FROM (SELECT execucao_id, nome, issue, inicio, conta, modelo, ok FROM indexado "
                     "UNION ALL SELECT DISTINCT execucao_id, NULL, NULL, NULL, NULL, NULL, NULL FROM passo "
                     "WHERE execucao_id NOT IN (SELECT execucao_id FROM indexado)) "
                     "ORDER BY inicio IS NULL, inicio DESC LIMIT ?", (int(limite),))
    if not rs:
        return ui.card("Execuções", ui.vazio("Nenhuma execução indexada ainda."))
    linhas = []
    for r in rs:
        res = ui.selo("—") if r["ok"] is None else (ui.selo("ok", "ok") if r["ok"] else ui.selo("falhou", "ruim"))
        linhas.append([ui.td(f'<a href="/execucao/{e(r["execucao_id"])}">{e(r["execucao_id"])}</a>', html_=True),
                       ui.td(r["issue"] or r["nome"] or "—"), ui.td(r["conta"] or "—"), ui.td(r["modelo"] or "—"),
                       ui.td(r["inicio"] or "—"), ui.td(res, html_=True)])
    return ui.card("Execuções", ui.tabela(["Execução", "Issue", "Conta", "Modelo", "Início", "Resultado"], linhas),
                   conta=len(rs), justa=True)
