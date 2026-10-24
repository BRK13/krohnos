import json
import re
import unittest

from hub import ui

MAU = '<script>alert("x")</script>'


def _corpo(h):
    """HTML fora do <style> e dos <script> (onde style= seria atributo de verdade)."""
    h = re.sub(r"<style>.*?</style>", "", h, flags=re.S)
    return re.sub(r"<script.*?</script>", "", h, flags=re.S)


class FormatosTest(unittest.TestCase):
    def test_num(self):
        self.assertEqual(ui.num(1397), "1.397")
        self.assertEqual(ui.num(1397, moeda="R$"), "R$ 1.397,00")
        self.assertEqual(ui.num(80.389, moeda="US$"), "US$ 80,39")
        self.assertEqual(ui.num(12.5, casas=1, pct=True), "12,5%")
        self.assertEqual(ui.num(-3), "−3")
        self.assertEqual((ui.num(None), ui.num("x")), ("—", "—"))

    def test_quando_e_ha(self):
        self.assertEqual(ui.quando("2026-10-14T17:17:00Z"), "14/10 14:17")
        self.assertEqual(ui.quando(None), "")
        self.assertEqual((ui.ha(minutos=0), ui.ha(minutos=18), ui.ha(minutos=190), ui.ha(minutos=3 * 1440)),
                         ("agora", "há 18 min", "há 3 h", "há 3 dias"))


class ComponentesTest(unittest.TestCase):
    def test_tudo_escapa_texto_de_fora(self):
        saidas = [ui.selo(MAU), ui.vazio(MAU, MAU), ui.erro(MAU, MAU), ui.card(MAU, ""), ui.kpi(MAU, MAU, comp=MAU),
                  ui.barra(MAU, 3), ui.botao(MAU, name="n", value=MAU), ui.filtro("f", {"a": MAU}, "a", rotulo=MAU),
                  ui.lista([{"titulo": MAU, "fala": MAU, "href": "/x"}]), ui.td(MAU), ui.tabela([MAU], [[MAU]]),
                  ui.md(MAU), ui.kv([(MAU, MAU)]), ui.dialogo("d", MAU, ""),
                  ui.grafico([[1, 2, 3]], [MAU, MAU, MAU], legenda=[MAU]), ui.pagina(MAU, "", olho=MAU)]
        for h in saidas:
            self.assertNotIn(MAU, h)

    def test_sem_style_inline(self):
        h = ui.pagina("T", ui.kpis(ui.kpi("A", 1, serie=[1, 2, None, 4])) + ui.barra("CPU", 37) +
                      ui.grafico([[1, 2], [2, 1]], ["a", "b"], tipo="area") + ui.grafico([[3, 1, 2]], tipo="barra") +
                      ui.card("x", ui.tabela(["a"], [["1"]], ord_=True)) + ui.lista([{"titulo": "t"}]), "/uso", n_pend=3)
        self.assertNotRegex(_corpo(h), r"\sstyle=")

    def test_css_so_tokens(self):
        self.assertEqual(ui._so_tokens_ok(ui.CSS), [])

    def test_barra_limiar_unico_e_classe_de_largura(self):
        self.assertIn('class="med ok"', ui.barra("a", 10))
        self.assertIn('class="med alerta"', ui.barra("a", 85))
        self.assertIn('class="med ruim"', ui.barra("a", 95))
        self.assertIn('class="p50"', ui.barra("a", 1, 2))
        self.assertIn('class="p100"', ui.barra("a", 300))

    def test_tabela_numero_ordem_e_celular(self):
        h = ui.tabela(["Nome", "Valor"], [[ui.td("a"), ui.td("1.234", 1234, n=True)]], num=(1,), ord_=True, celular="some:0")
        self.assertIn('class="ord"', h)
        self.assertIn('data-v="1234"', h)
        self.assertIn('<th class="some-cel">Nome</th>', h)
        self.assertIn('<td class="some-cel c-tit" data-rotulo="Nome">a</td>', h)
        self.assertIn('<td class="n" data-v="1234" data-rotulo="Valor">1.234</td>', h)

    def test_tabela_vira_cartao_no_celular(self):
        # abaixo de 640 px cada linha é um cartão; título, número grande e célula larga escolhidos por coluna
        h = ui.tabela(["Tipo", "Palavra", "Potencial", "Ação"], [[ui.td(ui.selo("Gap"), html_=True), "dor", ui.td("200", 200, n=True),
                      ui.td("ampliar a página", classe="mk-acao")]], num=(2,), titulo=1, grande=2, largas=(3,))
        self.assertIn('<div class="rola cartoes">', h)
        self.assertIn('<td class="c-tit" data-rotulo="Palavra">dor</td>', h)
        self.assertIn('<td class="c-grande n" data-v="200" data-rotulo="Potencial">200</td>', h)
        self.assertIn('<td class="c-larga mk-acao" data-rotulo="Ação">', h)
        self.assertIn('class="rola cartoes"', ui.tabela(["a"], [["b"]], celular="pilha"))
        m = re.search(r"@media \(max-width:640px\)\{(.*)", ui.CSS, re.S).group(1)
        self.assertIn(".cartoes tr{display:grid", m)
        self.assertIn(".cartoes td[data-rotulo]:not(.c-tit)::before{content:attr(data-rotulo)", m)

    def test_tabela_que_rola_tem_coluna_fixa_e_aviso(self):
        h = ui.tabela(["Busca", "jan", "fev"], [["a", "1", "2"]], celular="rola")
        self.assertIn('<div class="rola rola-x" tabindex="0">', h)
        self.assertIn('class="rola-dica"', h)
        self.assertNotIn("cartoes", h)
        self.assertIn(".rola-x th:first-child,.rola-x td:first-child{position:sticky;left:0", ui.CSS)
        self.assertIn(".rola-x[data-mais=dir]", ui.CSS)

    def test_palavra_nao_quebra_no_meio(self):
        # anywhere encolhe a coluna até "tendinit/e"; só URL e domínio (.url) podem quebrar em qualquer ponto
        semurl = ui.CSS.replace(".url{overflow-wrap:anywhere}", "")
        self.assertNotIn("anywhere", semurl)
        self.assertNotIn("break-all", ui.CSS)
        self.assertIn("hyphens:auto", ui.CSS)
        self.assertIn('<html lang="pt-BR">', ui.pagina("x", ""))

    def test_kpi_numero_encolhe_e_nunca_corta(self):
        regra = re.search(r"\.kpi \.v\{([^}]*)\}", ui.CSS).group(1)
        self.assertIn("font-size:clamp(", regra)
        self.assertNotIn("ellipsis", regra)
        self.assertNotIn("nowrap", regra)
        self.assertIn("container-type:inline-size", re.search(r"\.kpi\{([^}]*)\}", ui.CSS).group(1))

    def test_filtro_com_mais_de_3_opcoes_ganha_select(self):
        quatro = ui.filtro("t", {"a": "A", "b": "B", "c": "C", "d": "D"}, "b", base="/x", rotulo="Tipo")
        self.assertIn('class="filtro tem-sel"', quatro)
        self.assertIn('<select data-vai aria-label="Tipo">', quatro)
        self.assertIn('<option value="/x?t=b" selected>B</option>', quatro)
        tres = ui.filtro("t", {"a": "A", "b": "B", "c": "C"}, "a", base="/x")
        self.assertNotIn("<select", tres)
        self.assertEqual(_corpo(ui.pagina("x", "", "/agentes")).count("<select"), 1)  # o único é o seletor de negócio do menu
        self.assertIn("select[data-vai]", ui.JS)
        self.assertIn("scroll-snap-type:x", ui.CSS)
        self.assertIn(".filtro[data-mais=dir]{mask-image", ui.CSS)

    def test_toque_e_area_segura(self):
        self.assertRegex(ui.TOKENS, r"@media \(max-width:860px\)\{:root\{[^}]*--bt:44px")
        self.assertIn("env(safe-area-inset-bottom)", ui.CSS)  # o respiro de baixo da página no iPhone
        self.assertRegex(ui.TOKENS, r"@media \(max-width:860px\)\{:root\{[^}]*--item-h:44px")  # item do menu na gaveta é alvo de toque

    def test_nenhum_min_width_maior_que_o_celular(self):
        # nada em ui.py pode exigir mais largura que um celular de 375 px (menos o respiro da página)
        tok = {k: v for k, v in re.findall(r"(--[\w-]+):([^;}]+)", ui.TOKENS)}

        def px(v, fundo=0):
            v = v.strip()
            m = re.fullmatch(r"var\((--[\w-]+)\)", v)
            if m and fundo < 5:
                return px(tok.get(m.group(1), "0"), fundo + 1)
            m = re.fullmatch(r"calc\((\d+) \* var\((--[\w-]+)\)\)", v)
            if m:
                return int(m.group(1)) * px(f"var({m.group(2)})", fundo + 1)
            m = re.fullmatch(r"(\d+(?:\.\d+)?)px", v)
            return float(m.group(1)) if m else 0
        for v in re.findall(r"(?<![\w-])min-width:([^;}]+)", ui.CSS):
            self.assertLessEqual(px(v), 375 - 2 * 16, v)

    def test_pilula_nao_quebra_linha(self):
        #.filtro com raio de pílula que quebra em 3 linhas vira oval; rola na horizontal, só.muitos quebra (raio de card)
        import re
        regra = re.search(r"\.filtro\{([^}]*)\}", ui.CSS).group(1)
        self.assertIn("flex-wrap:nowrap", regra)
        self.assertIn("overflow-x:auto", regra)
        self.assertIn(".filtro.muitos{flex-wrap:wrap;border-radius:var(--r)}", ui.CSS)
        self.assertIn(".filtro [aria-current]", ui.JS)

    def test_filtro_mantem_query(self):
        h = ui.filtro("periodo", {"7": "7 dias", "28": "28 dias"}, "28", base="/bi/fsite06?comparar=ano")
        self.assertIn('href="/bi/fsite06?comparar=ano&amp;periodo=7"', h)
        self.assertIn('aria-current=true>28 dias', h)

    def test_grafico_lacuna_e_vazio(self):
        self.assertEqual(ui.grafico([[None, None]]), "")
        h = ui.grafico([[1, None, 3, 4]], ["a", "b", "c", "d"])
        self.assertIn('class="pt s1"', h)  # ponto isolado antes da lacuna
        self.assertIn("<polyline", h)
        self.assertIn('<div class="x">', h)


class PaginaTest(unittest.TestCase):
    def test_nome_do_produto_no_titulo_e_no_menu(self):
        h = ui.pagina("Pendências", "", "/pendencias")
        self.assertIn(f"<title>Pendências · {ui.NOME_PRODUTO}</title>", h)
        self.assertRegex(h, rf'(?s)<a class="marca" href="/pendencias"><svg class="simbolo"[^>]*aria-hidden="true".*?</svg>{ui.NOME_PRODUTO}</a>')
        self.assertEqual(ui.NOME_PRODUTO, "Krohnos")

    # test_detalhe_acende_a_tela_mae e test_negocio_acende_o_chip moram em tests/test_ui_negocios.py: dependem das
    # telas do BI dos nossos negócios (/bi), fora do snapshot público (lá o painel tira /bi do menu).

    def test_pendencias_no_menu_e_na_barra_de_baixo(self):
        h = ui.pagina("x", "", "/bi", n_pend=4)
        self.assertEqual(h.count("<span class=n>4</span>"), 2)
        self.assertNotIn("<span class=n>", ui.pagina("x", "", "/bi"))

    def test_menu_acordeao_com_aria_e_a_tela_atual_aberta(self):
        # um acordeão por intenção (botão com aria-expanded/aria-controls), o grupo da tela atual abre sozinho, nada some
        h = ui.pagina("x", "", "/uso")
        nav = h.split('<nav class="lado"')[1].split("</nav>")[0]
        botoes = re.findall(r'<button class="grupo-bt"[^>]*>', nav)
        self.assertEqual(len(botoes), len(ui.menu()))
        self.assertIn(len(botoes), range(4, 7))  # 4 a 6 categorias
        abertos = [b for b in botoes if 'aria-expanded="true"' in b]
        self.assertEqual(len(abertos), 1)
        self.assertIn('data-atual=1', abertos[0])
        self.assertIn('title="Operação"', abertos[0])
        for n in range(len(botoes)):
            self.assertIn(f'id="gb{n}"', nav)
            self.assertIn(f'<div class="grupo-itens" id="gi{n}" role="group" aria-labelledby="gb{n}">', nav)
        for caminho, _ in (i for _, _, itens in ui.menu() for i in itens):
            self.assertIn(f'<a href="{caminho}"', nav)  # nada some
        self.assertIn('<a href="/uso" aria-current=page>', nav)
        for tecla in ("ArrowDown", "ArrowUp", "Home", "End", "Escape"):
            self.assertIn(tecla, ui.JS_MENU)

    def test_menu_item_com_icone_de_traco_e_contador_so_onde_ha_pendencia(self):
        h = ui.pagina("x", "", "/propostas", n_pend=4, contadores={"/propostas": 3, "/metas": 0})
        nav = h.split('<nav class="lado"')[1].split("</nav>")[0]
        self.assertRegex(nav, r'<a href="/propostas" aria-current=page><svg class="ic"[^>]*><path d="[^"]+"/></svg><span class="t">Propostas</span><span class=n>3</span></a>')
        self.assertIn('<span class="t">Pendências</span><span class=n>4</span>', nav)
        self.assertNotIn('<span class="t">Metas</span><span class=n>', nav)
        self.assertEqual(nav.count("<span class=n>"), 2)
        self.assertNotIn("<span class=n>", ui.pagina("x", "", "/uso"))
        for _, _, itens in ui.menu():
            for caminho, _ in itens:
                self.assertIn(caminho, ui.ICONES_ITEM, caminho)  # item sem ícone cai no ponto: o conjunto cobre o menu inteiro
        self.assertRegex(ui.CSS, r"\.ic\{[^}]*stroke-width:1\.5")

    def test_negocios_viram_seletor_no_topo_e_a_gaveta_do_celular(self):
        h = ui.pagina("x", "", "/pendencias")
        nav = h.split('<nav class="lado"')[1].split("</nav>")[0]
        self.assertLess(nav.index('<select data-vai aria-label="Negócio">'), nav.index('class="menu"'))
        self.assertNotIn('class="negocios"', nav)  # as pílulas soltas saíram da barra
        self.assertIn('aria-controls="lado"', h)
        self.assertIn("html.gaveta nav.lado", ui.CSS)
        self.assertIn("visibility:hidden", re.search(r"nav\.lado\{position:fixed[^}]*\}", ui.CSS).group(0))  # fechada, não recebe foco
        self.assertIn("html[data-menu=mini]", ui.CSS)  # recolhido: só os ícones
        self.assertIn("localStorage.menu=='mini'", ui.TEMA_HEAD)
        self.assertNotIn("<nav", ui.pagina("x", "", publica=True))

    def test_view_transitions_speculation_e_movimento_reduzido(self):
        h = ui.pagina("x", "", "/uso")
        self.assertIn("@view-transition{navigation:auto}", h)
        self.assertIn("prefers-reduced-motion:reduce){@view-transition{navigation:none}", h)
        regras = json.loads(re.search(r'<script type="speculationrules">(.*?)</script>', h).group(1))
        excl = json.dumps(regras)
        self.assertIn("/acao/*", excl)
        self.assertIn("/r/*", excl)
        self.assertEqual(regras["prefetch"][0]["eagerness"], "moderate")

    def test_markdown_so_quando_tem(self):
        self.assertNotIn("marked", ui.pagina("x", "<p>a</p>"))
        self.assertIn("marked", ui.pagina("x", ui.md("# a")))

    def test_publica_sem_menu_e_sem_prefetch(self):
        h = ui.pagina("x", "", publica=True)
        self.assertNotIn('<nav class="lado"', h)
        self.assertNotIn("speculationrules", h)
        self.assertIn('name="robots" content="noindex"', h)


class MarcaTest(unittest.TestCase):
    def _cores(self, bloco):
        return set(re.findall(r"(--(?:bg|sup|sup-2|linha|linha-2|sel|campo|tx|tx-2|tx-3|ac|ac-tx|ac-forte|ac-fundo|ok|al|ru|g\d)):", bloco))

    def test_sem_verde_azul_gradiente_nem_hex_no_css(self):
        # gradiente só como máscara de transparência (fade das pílulas que rolam); cor em gradiente, nunca
        tudo = re.sub(r"mask-image:linear-gradient\([^{}]*?\)\}", "", (ui.TOKENS + ui.CSS).lower())
        for velho in ("#86d99a", "#7aa7ff", "#0b0c0e", "134,217,154", "gradient"):
            self.assertNotIn(velho, tudo)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", ui.CSS), [])  # cor só nos TOKENS

    def test_dois_temas_com_as_mesmas_cores(self):
        escuro = re.search(r":root\{color-scheme:dark;(.*?)\}", ui.TOKENS, re.S).group(1)
        claro = re.search(r":root\[data-tema=claro\]\{color-scheme:light;(.*?)\}", ui.TOKENS, re.S).group(1)
        self.assertEqual(self._cores(escuro), self._cores(claro))
        self.assertEqual(len(self._cores(escuro)), 23)
        for t in ("--sombra:", "--sombra-aviso:", "--veu:", "--grao-op:"):
            self.assertIn(t, claro)

    def test_tema_bt_marcado_e_grao_claro(self):
        self.assertIn(".tema-bt[aria-pressed=true]{border-color:var(--ac);background:var(--ac-fundo)", ui.CSS)
        claro = re.search(r":root\[data-tema=claro\]\{color-scheme:light;(.*?)\}", ui.TOKENS, re.S).group(1)
        self.assertIn("--grao-op:.06", claro)

    def test_identidade_da_lp_inter_e_sem_caixa_alta_espacada(self):
        # a identidade é a da LP krohnos.com (Inter, verde-escuro); nada de Marcellus, dourado nem rótulo em caixa alta espaçada
        self.assertIn("family=Inter", ui.FONTES)
        self.assertNotIn("Marcellus", ui.FONTES + ui.TOKENS + ui.CSS)
        self.assertIn("--f-texto:Inter,", ui.TOKENS)
        self.assertIn("--fonte-numero:Inter,", ui.TOKENS)
        self.assertIn("font:600 var(--kpi)/1.05 var(--fonte-numero)", ui.CSS)
        self.assertIn("font-variant-numeric:tabular-nums lining-nums", ui.CSS)
        self.assertNotIn("text-transform:uppercase", ui.CSS)
        self.assertNotRegex(ui.CSS, r"letter-spacing:\.(0[6-9]|[1-9])\d*em")
        escuro = re.search(r":root\{color-scheme:dark;(.*?)\}", ui.TOKENS, re.S).group(1)
        self.assertIn("--bg:#010805", escuro)  # o fundo da LP
        for dourado in ("#B0976A", "#C4AD82", "#F5F1E7", "#1C1C1C"):
            self.assertNotIn(dourado, ui.TOKENS)

    def test_pagina_tem_script_e_botao_de_tema(self):
        for h in (ui.pagina("x", "", "/uso"), ui.pagina("x", "", publica=True)):
            cabeca = h.split("</head>")[0]
            self.assertIn("localStorage.tema=='claro'", cabeca)
            self.assertLess(cabeca.index("dataset.tema"), cabeca.index("<style>"))  # antes do CSS pintar
            self.assertIn('<meta name="color-scheme" content="dark light">', cabeca)
            self.assertIn('class="tema-bt" type="button" aria-pressed="false">Tema claro</button>', h)
        self.assertIn('aria-label="Menu"', ui.pagina("x", "", "/uso").split('class="tema-bt"')[0])  # botão no rodapé do menu


if __name__ == "__main__":
    unittest.main()
