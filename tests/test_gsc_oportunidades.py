import json
import unittest
from pathlib import Path

import gsc_oportunidades as g

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gsc_linhas.json").read_text(encoding="utf-8"))


class CurvaTest(unittest.TestCase):
    def test_curva_decrescente_e_nos_pontos(self):
        valores = [g.CURVA_CTR[p] for p in range(1, 11)]
        self.assertEqual(valores, sorted(valores, reverse=True))
        self.assertEqual(g.ctr_esperado(1), 0.25)
        self.assertEqual(g.ctr_esperado(0.5), 0.25)
        self.assertEqual(g.ctr_esperado(40), 0.02)

    def test_interpolacao(self):
        self.assertAlmostEqual(g.ctr_esperado(2.5), (0.14 + 0.10) / 2)
        self.assertAlmostEqual(g.ctr_esperado(2.25), 0.14 - 0.04 * 0.25)


class CobreTest(unittest.TestCase):
    def test_url_e_titulo(self):
        self.assertTrue(g.cobre("pilates alfa", "https://clientea-com.example/pilates/alfa"))
        self.assertTrue(g.cobre("dor lombar tratamento", None, "Dor lombar: tratamento com fisioterapia"))
        self.assertTrue(g.cobre("fisioterapias lombares", "https://x.com/fisioterapia-lombar"))  # plural/flexão
        self.assertFalse(g.cobre("pilates beta", "https://clientea-com.example/pilates/alfa"))
        self.assertFalse(g.cobre("de para", "https://x.com/de-para"))  # só palavra vazia não conta como cobertura

    def test_dominio_nao_conta_como_url(self):
        self.assertFalse(g.cobre("cliente a", "https://clientea-com.example/"))


class ClassificarTest(unittest.TestCase):
    def setUp(self):
        self.ops = g.classificar(FIXTURE["rows"], FIXTURE["titulos"], FIXTURE["sitemap"])
        self.por_busca = {(o["tipo"], o["busca"]): o for o in self.ops}

    def test_vazamentos(self):
        # posição 3, 900 impressões, CTR 0,2% contra 10% esperado → vazamento
        v = self.por_busca[("vazamento", "quiropraxia brasilia")]
        self.assertEqual(v["pagina"], "https://clientea-com.example/quiropraxia")
        self.assertEqual(v["ctr_esperado"], 0.1)
        self.assertEqual(v["cliques_perdidos"], 88)
        self.assertIn(("vazamento", "cliente a"), self.por_busca)
        self.assertNotIn(("vazamento", "fisioterapia alfa"), self.por_busca)  # CTR 15% na posição 2 é saudável
        self.assertNotIn(("vazamento", "liberacao miofascial preco"), self.por_busca)  # menos de 200 impressões

    def test_buracos(self):
        # 110 impressões somadas, posição média > 10, zero clique, sem página com a busca no título/URL
        b = self.por_busca[("buraco", "fisioterapia para hernia de disco")]
        self.assertEqual(b["impressoes"], 110)
        self.assertGreater(b["posicao"], 10)
        self.assertNotIn(("buraco", "dor lombar tratamento"), self.por_busca)  # título já cobre
        self.assertNotIn(("buraco", "pilates alfa"), self.por_busca)  # URL do sitemap já cobre
        self.assertEqual(self.por_busca[("reforcar", "pilates alfa")]["pagina"], "https://clientea-com.example/pilates/alfa")
        self.assertNotIn(("buraco", "acupuntura brasilia"), self.por_busca)  # teve clique
        self.assertNotIn(("buraco", "drenagem linfatica"), self.por_busca)  # menos de 50 impressões

    def test_falsos_buracos_do_relatorio_da_fsite05(self):
        # Linhas reais do relatório de 08/10/2026: nenhuma era buraco.
        def linha(busca, pagina, imp, pos):
            return {"keys": [busca, "https://clientea-com.example" + pagina], "clicks": 0, "impressions": imp, "ctr": 0, "position": pos}
        linhas = [linha("massagem miofacial", "/blog/liberacao-miofascial-vs-massagem", 401, 10.5),  # erro de digitação
                  linha("cinesioterapia fisioterapia", "/glossario/cinesioterapia", 73, 11.5),
                  linha("dentista atm brasilia", "/blog/dor-mandibula-atm-tratamento-brasilia", 181, 34.1),
                  linha("espaço fsite05 alfa", "/unidades/alfa", 73, 11.9),  # concorrente
                  linha("clientet beta", "/comparativo/cliente-a-vs-clientet", 70, 10.5),  # concorrente
                  linha("fisioterapia para hernia de disco", "/blog/coluna", 80, 18.0)]
        ops = {o["busca"]: o for o in g.classificar(linhas, concorrentes=["Espaço fsite05", "ClienteT"])}
        self.assertEqual(ops["massagem miofacial"]["tipo"], "reforcar")
        self.assertEqual(ops["cinesioterapia fisioterapia"]["tipo"], "reforcar")
        self.assertEqual(ops["dentista atm brasilia"]["tipo"], "reforcar")
        self.assertEqual(ops["dentista atm brasilia"]["pagina"], "https://clientea-com.example/blog/dor-mandibula-atm-tratamento-brasilia")
        self.assertNotIn("espaço fsite05 alfa", ops)
        self.assertNotIn("clientet beta", ops)
        self.assertEqual(ops["fisioterapia para hernia de disco"]["tipo"], "buraco")  # o buraco de verdade continua

    def test_ordem_vazamentos_primeiro_por_impressoes(self):
        tipos = [o["tipo"] for o in self.ops]
        self.assertEqual(tipos, sorted(tipos, key=lambda t: t != "vazamento"))
        vaz = [o["impressoes"] for o in self.ops if o["tipo"] == "vazamento"]
        self.assertEqual(vaz, sorted(vaz, reverse=True))

    def test_markdown(self):
        md = g.markdown({"nome": "Cliente A"}, {"data": "2026-10-07", "periodo": "a a b", "oportunidades": self.ops})
        self.assertIn("## Vazamentos", md)
        self.assertIn("quiropraxia brasilia", md)
        self.assertIn("relatório de desempenho em IA", md)


if __name__ == "__main__":
    unittest.main()
