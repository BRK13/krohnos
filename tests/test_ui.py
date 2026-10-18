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
        self.assertNotIn("<select", _corpo(ui.pagina("x", "", "/agentes")))  # o menu de cima do celular rola, nunca vira select
        self.assertIn("select[data-vai]", ui.JS)
        self.assertIn("scroll-snap-type:x", ui.CSS)
        self.assertIn(".filtro[data-mais=dir]{mask-image", ui.CSS)

    def test_toque_e_area_segura(self):
        self.assertRegex(ui.TOKENS, r"@media \(max-width:860px\)\{:root\{[^}]*--bt:44px")
        self.assertTrue(any("env(safe-area-inset-bottom)" in r for r in re.findall(r"\.embaixo\{([^}]*)\}", ui.CSS)))

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
        h = ui.filtro("periodo", {"7": "7 dias", "28": "28 dias"}, "28", base="/bi/fsite05?comparar=ano")
        self.assertIn('href="/bi/fsite05?comparar=ano&amp;periodo=7"', h)
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
        self.assertEqual(ui.NOME_PRODUTO, "Pereiraos")

    # test_detalhe_acende_a_tela_mae e test_negocio_acende_o_chip moram em tests/test_ui_negocios.py: dependem das
    # telas do BI dos nossos negócios (/bi), fora do snapshot público (lá o painel tira /bi do menu).

    def test_pendencias_no_menu_e_na_barra_de_baixo(self):
        h = ui.pagina("x", "", "/bi", n_pend=4)
        self.assertEqual(h.count("<span class=n>4</span>"), 2)
        self.assertNotIn("<span class=n>", ui.pagina("x", "", "/bi"))

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

    def test_marcellus_so_nos_titulos_e_numero_em_sans(self):
        self.assertIn("family=Marcellus", ui.FONTES)
        self.assertIn("--f-titulo:Marcellus,Georgia,serif", ui.TOKENS)
        self.assertIn("--fonte-numero:Geist,", ui.TOKENS)  # a serifada ilegível nos números (0 vira círculo)
        self.assertIn("font:600 var(--kpi)/1.05 var(--fonte-numero)", ui.CSS)
        self.assertIn("font-variant-numeric:tabular-nums lining-nums", ui.CSS)
        self.assertEqual([x.split("*/")[-1].strip() for x in re.findall(r"([^{}]+)\{[^}]*var\(--f-titulo\)", ui.CSS)], [".marca", "h1"])

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
