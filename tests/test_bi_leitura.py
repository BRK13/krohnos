import datetime as dt
import io
import shutil
import tempfile
import unittest
from unittest import mock

from hub import armazem, bi_leitura as L

HOJE = dt.date(2026, 10, 9)  # ontem = 08/10; GSC avaliado em 05/10


def dia(n):
    return (HOJE - dt.timedelta(days=n)).isoformat()


def f(d, valor, fonte, metrica, dim="", negocio="asite01"):
    return {"dia": d, "negocio": negocio, "fonte": fonte, "metrica": metrica, "dim": dim, "valor": valor}


class ResumoTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def resumo(self):
        return L.resumo(7, HOJE, self.d)

    def test_semana_contra_media_de_quatro_semanas(self):
        # semana (dias 1..7 atrás): 20 por dia = 140; base (8..35): 10 por dia, com uma lacuna que não conta como zero
        armazem.gravar([f(dia(n), 20, "ga4", "sessoes") for n in range(1, 8)] +
                       [f(dia(n), 10, "ga4", "sessoes") for n in range(8, 36) if n != 9], self.d)
        r = self.resumo()
        self.assertIn("- Visitas: 140 (+100% contra a média das 4 semanas anteriores) ⚠", r)

    def test_desvio_abaixo_de_20_nao_marca(self):
        armazem.gravar([f(dia(n), 11, "app", "leads") for n in range(1, 8)] +
                       [f(dia(n), 10, "app", "leads") for n in range(8, 36)], self.d)
        r = self.resumo()
        self.assertIn("- Contatos novos: 77 (+10% contra a média das 4 semanas anteriores)\n", r)

    def test_base_vazia_e_semana_sem_dado(self):
        armazem.gravar([f(dia(1), 5, "app", "leads"), f(dia(20), 3, "app", "faltas")], self.d)
        r = self.resumo()
        self.assertIn("- Contatos novos: 5 (sem base)", r)
        self.assertNotIn("Faltas", r)
        self.assertNotIn("∞", r)

    def test_negocio_sem_dado_na_semana_some(self):
        armazem.gravar([f(dia(20), 3, "app", "leads", negocio="velho")], self.d)
        self.assertNotIn("velho", self.resumo())

    def test_ctr_e_soma_de_cliques_sobre_impressoes(self):
        # semana do GSC: dias 10..4 atrás. Dia A 1/1, dia B 10/1000: 11/1001, não a média 50,5%
        armazem.gravar([f(dia(5), 1, "gsc", "cliques"), f(dia(5), 1, "gsc", "impressoes"), f(dia(5), 1.0, "gsc", "ctr"),
                        f(dia(6), 10, "gsc", "cliques"), f(dia(6), 1000, "gsc", "impressoes"), f(dia(6), 0.01, "gsc", "ctr")], self.d)
        v, n = L._media("asite01", "gsc", "ctr", HOJE - dt.timedelta(days=10), HOJE - dt.timedelta(days=4), self.d)
        self.assertAlmostEqual(v, 11 / 1001)
        self.assertEqual(n, 2)
        self.assertIn("- Taxa de clique no Google: 1,1% (sem base)", self.resumo())  # a da semana, não 100% do último dia

    def test_estoque_mostra_ultimo_valor_e_gsc_recua_tres_dias(self):
        armazem.gravar([f(dia(n), 1000 + n, "stripe", "mrr") for n in range(1, 36)] +
                       [f(dia(4), 100, "gsc", "cliques"), f(dia(1), 999, "gsc", "cliques")], self.d)
        r = self.resumo()
        self.assertIn("- Receita mensal recorrente: R$ 1.001,00", r)
        self.assertIn("- Cliques no Google: 100 (sem base)", r)  # o dia 1 (parcial) fica de fora
        self.assertIn("Google até 05/10", r)

    def test_top3_origens_pela_diferenca(self):
        dados = []
        for origem, antes, depois in (("a", 1, 2), ("b", 10, 3), ("c", 5, 5.5), ("d", 0, 4), ("e", 7, 7)):
            dados += [f(dia(n), depois, "ga4", "sessoes", origem) for n in range(1, 8)]
            dados += [f(dia(n), antes, "ga4", "sessoes", origem) for n in range(8, 36)]
        armazem.gravar(dados + [f(dia(n), 1, "ga4", "sessoes") for n in range(1, 36)], self.d)
        r = self.resumo()
        origens = [x.strip() for x in r.splitlines() if x.startswith("  - ")]
        self.assertEqual([x.split(":")[0] for x in origens], ["- b", "- d", "- a"])
        self.assertIn("- b: 10,0 → 3,0 visitas por dia", origens[0])


    def test_lacuna_inteira_nao_inventa_movimento(self):
        dados = [f(dia(n), 10, "ga4", "sessoes", o) for n in range(1, 36) if n != 3 for o in ("a", "b")]
        dados += [f(dia(n), 20, "ga4", "sessoes") for n in range(1, 36) if n != 3]
        armazem.gravar(dados, self.d)
        self.assertNotIn("  - ", self.resumo())

    def test_variacao_minima_sai_zero(self):
        armazem.gravar([f(dia(n), 1000 if n > 7 else 1001, "app", "leads") for n in range(1, 36)], self.d)
        self.assertIn("(0% contra", self.resumo())


class VigiaTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.avisos = []

    def base(self, fonte="app", metrica="leads", valor=10, ontem=5, semanas=(7, 14, 21, 28), negocio="fsite06"):
        d = [f(dia(1 + k), valor, fonte, metrica, negocio=negocio) for k in semanas]
        if ontem is not None:
            d.append(f(dia(1), ontem, fonte, metrica, negocio=negocio))
        armazem.gravar(d, self.d)

    def vigiar(self, hoje=HOJE):
        return L.vigiar(hoje, self.d, self.avisos.append)

    def test_queda_de_50_dispara(self):
        self.base()
        q = L.quedas(HOJE, self.d)
        self.assertEqual([(x["negocio"], x["sinal"]) for x in q], [("fsite06", "contatos novos")])
        self.assertAlmostEqual(q[0]["queda"], 0.5)
        msg = self.vigiar()
        self.assertIn("https://painel.exemplo.com.br/bi/fsite06", msg)
        self.assertTrue(msg.startswith("Queda forte no BI (Krohnos):"))  # o WhatsApp cita o painel pelo nome

    def test_queda_de_40_exata_ou_menor_nao_dispara(self):
        self.base(ontem=7)
        self.assertEqual(L.quedas(HOJE, self.d), [])

    def test_lacuna_no_dia_avaliado_nao_dispara(self):
        self.base(ontem=None)
        self.assertEqual(L.quedas(HOJE, self.d), [])

    def test_base_com_so_uma_semana_nao_dispara(self):
        self.base(semanas=(7,))
        self.assertEqual(L.quedas(HOJE, self.d), [])

    def test_media_pequena_nao_dispara_leads_mas_dispara_mrr(self):
        self.base(valor=4, ontem=0)
        self.assertEqual(L.quedas(HOJE, self.d), [])
        self.base(fonte="stripe", metrica="mrr", valor=4, ontem=0, negocio="asite01")
        self.assertEqual([x["sinal"] for x in L.quedas(HOJE, self.d)], ["receita"])

    def test_gsc_avalia_tres_dias_antes(self):
        d = [f(dia(4 + k), 10, "gsc", "cliques") for k in (7, 14, 21)] + [f(dia(4), 2, "gsc", "cliques")]
        armazem.gravar(d, self.d)
        self.assertEqual([x["dia"] for x in L.quedas(HOJE, self.d)], [dia(4)])

    def test_dois_disparos_viram_uma_mensagem(self):
        self.base()
        self.base(negocio="asite01")
        self.vigiar()
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("fsite06", self.avisos[0])
        self.assertIn("asite01", self.avisos[0])

    def test_so_avisa_uma_vez_por_dia(self):
        self.base()
        self.vigiar()
        self.vigiar()
        self.assertEqual(len(self.avisos), 1)
        armazem.gravar([f(dia(0), 4, "app", "leads", negocio="fsite06")] +
                       [f(dia(k), 10, "app", "leads", negocio="fsite06") for k in (7, 14, 21, 28)], self.d)
        self.vigiar(HOJE + dt.timedelta(days=1))
        self.assertEqual(len(self.avisos), 2)

    def test_sem_queda_nao_avisa(self):
        self.base(ontem=10)
        self.assertIsNone(self.vigiar())
        self.assertEqual(self.avisos, [])


class GravarTest(unittest.TestCase):
    def test_grava_e_vazio_nao_grava(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        armazem.caminho(d).parent.mkdir()  # em produção o atualizar.sh cria a pasta e o arquivo
        L.gravar("Leitura\n", d)
        arq = armazem.caminho(d).parent / "leitura-semana.md"
        self.assertEqual(arq.read_text(), "Leitura\n")
        with self.assertRaises(ValueError):
            L.gravar("  \n", d)
        self.assertEqual(arq.read_text(), "Leitura\n")

    def test_cli_vazio_sai_com_erro(self):
        with mock.patch("sys.stdin", io.StringIO("")), self.assertRaises(SystemExit):
            L.main(["gravar"])


if __name__ == "__main__":
    unittest.main()
