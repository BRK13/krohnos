import datetime as dt
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hub import conta


def open_text(caminho):
    with open(caminho, encoding="utf-8") as f:
        return f.read()

UTC = dt.timezone.utc


class LimiteTest(unittest.TestCase):
    agora = dt.datetime(2026, 10, 8, 14, 0, tzinfo=UTC)

    def test_detecta(self):
        for msg in ("Claude AI usage limit reached|1759935600", "5-hour limit reached ∙ resets 3pm",
                    "You've hit your session limit · resets 11:30pm (UTC)", "API Error: 429 rate_limit_error"):
            self.assertTrue(conta.eh_limite(msg), msg)
        self.assertFalse(conta.eh_limite("Error: ENOENT no such file"))

    def test_reset_epoch(self):
        self.assertEqual(conta.horario_reset("usage limit reached|1759935600", self.agora),
                         dt.datetime.fromtimestamp(1759935600, UTC) + dt.timedelta(minutes=2))

    def test_reset_hora_mais_tarde_hoje(self):
        self.assertEqual(conta.horario_reset("limit reached ∙ resets 3pm", self.agora),
                         dt.datetime(2026, 10, 8, 15, 2, tzinfo=UTC))

    def test_reset_amanha_e_fuso(self):
        r = conta.horario_reset("hit your limit · resets 9am (America/Sao_Paulo)", self.agora)
        self.assertEqual(r, dt.datetime(2026, 10, 9, 12, 2, tzinfo=UTC))

    def test_reset_semanal_com_data(self):
        r = conta.horario_reset("Weekly limit reached ∙ resets Oct 10, 5pm", self.agora)
        self.assertEqual(r, dt.datetime(2026, 10, 10, 17, 2, tzinfo=UTC))

    def test_sem_horario_uma_hora(self):
        self.assertEqual(conta.horario_reset("429", self.agora), self.agora + dt.timedelta(hours=1))

    def test_reset_do_codex(self):
        msg = "You've hit your usage limit. Upgrade to Pro or try again at 4:31 PM."
        self.assertTrue(conta.eh_limite(msg))
        self.assertEqual(conta.horario_reset(msg, self.agora), dt.datetime(2026, 10, 8, 16, 33, tzinfo=UTC))
        self.assertEqual(conta.horario_reset("You've hit your usage limit. try again at Oct 14th, 2026 3:42 PM.", self.agora),
                         dt.datetime(2026, 10, 14, 15, 44, tzinfo=UTC))
        self.assertEqual(conta.horario_reset("try again at Jan 2nd, 2027 9:05 AM", self.agora),
                         dt.datetime(2027, 1, 2, 9, 7, tzinfo=UTC))


class AvisarRecibo(unittest.TestCase):  # o recibo de pronto confere que o aviso pedido saiu
    def test_aviso_de_tarefa_deixa_linha_no_diario(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TAREFA_NOME": "linear-krn-667", "HUB_DATA": d}):
            (Path(d) / "caixa").mkdir()
            conta.avisar("pronto", caixa=Path(d) / "caixa")
            linha = json.loads(Path(d, "claude", "avisos-enviados.jsonl").read_text().splitlines()[0])
        self.assertEqual((linha["tarefa"], linha["texto"]), ("linear-krn-667", "pronto"))


class PausaUsoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.avisos = []

    def avisar(self, texto, agora_=False):
        self.avisos.append(texto)

    def test_codex_sem_horario_pausa_6h(self):
        t0 = dt.datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
        with mock.patch.dict(os.environ, {"CLAUDE_CONTA": "codex"}):
            self.assertEqual(conta.pausar("usage limit", "t", self.dados, t0, self.avisar), t0 + dt.timedelta(hours=6))
        self.assertEqual(conta.pausar("usage limit", "t", self.dados, t0, self.avisar), t0 + dt.timedelta(hours=1))

    def test_pausa_avisa_uma_vez(self):
        t0 = dt.datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
        ate = conta.pausar("resets 3pm", "tarefa x", self.dados, t0, self.avisar)
        conta.pausar("resets 3pm", "agente", self.dados, t0 + dt.timedelta(minutes=5), self.avisar)
        self.assertEqual(len(self.avisos), 1)
        # a pausa avisada fica em pausas.jsonl (a confiabilidade confere depois se era falsa)
        pausas = [json.loads(l) for l in Path(self.dados, "claude", "pausas.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(p["desde"], p["ate"], p["origem"]) for p in pausas], [(conta.iso(t0), conta.iso(ate), "tarefa x")])
        self.assertIsNotNone(conta.pausa_ativa(self.dados, t0 + dt.timedelta(minutes=30)))
        self.assertIsNone(conta.pausa_ativa(self.dados, ate + dt.timedelta(seconds=1)))
        conta.retomar(self.dados)
        self.assertIsNone(conta.pausa_ativa(self.dados, t0))

    def test_relatorio(self):
        t0 = dt.datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
        for nome, custo, ok in (("seo-semanal", 3.0, True), ("resumo-diario", 1.0, True), ("resumo-diario", 0.0, False)):
            conta.registrar({"inicio": conta.iso(t0), "origem": "tarefa", "nome": nome, "ok": ok, "custo_usd": custo,
                             "duracao_s": 600}, self.dados)
        conta.registrar({"inicio": conta.iso(t0 - dt.timedelta(days=9)), "origem": "tarefa", "nome": "velha",
                         "custo_usd": 50}, self.dados)
        txt = conta.relatorio_uso(7, self.dados, t0 + dt.timedelta(hours=1))
        self.assertIn("3 execuções", txt)
        self.assertNotIn("US$", txt)
        self.assertIn("não na cota disponível", txt)
        self.assertIn("seo-semanal (tarefa): 75%", txt)
        self.assertIn("1 falha", txt)
        self.assertNotIn("velha", txt)

    def test_cli_uso_envia_consumo_sem_dinheiro_ao_whatsapp(self):
        import io
        from unittest import mock
        conta.registrar({"inicio": conta.iso(conta.agora()), "nome": "linear-krn-890",
                         "conta": "1", "custo_usd": 74, "ok": True}, self.dados)
        with mock.patch.dict(os.environ, {"HUB_DATA": self.dados}), \
                mock.patch.object(conta, "avisar") as enviar, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            out.reconfigure = lambda **k: None
            self.assertEqual(conta.main(["uso", "--avisar"]), 0)
        texto = enviar.call_args.args[0]
        self.assertIn("linear-krn-890", texto)
        self.assertIn("100%", texto)
        self.assertNotIn("US$", texto)

    def test_cli_limite(self):
        import io
        from unittest import mock
        with mock.patch.dict(os.environ, {"HUB_DATA": self.dados}), mock.patch("sys.stdin", io.StringIO("resets 3pm")),                 mock.patch.object(conta, "avisar", self.avisar), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            out.reconfigure = lambda **k: None
            self.assertEqual(conta.main(["limite", "--origem", "agente"]), 0)
        self.assertIsNotNone(conta.pausa_ativa(self.dados))
        self.assertEqual(len(self.avisos), 1)

    def test_tokens(self):
        self.assertEqual(conta.tokens_de({"usage": {"input_tokens": 3, "output_tokens": 5}})["saida"], 5)
        self.assertEqual(conta.tokens_de(None)["entrada"], 0)

    def test_avisar_grava_json_na_caixa(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("TAREFA_NOME", None)
            conta.avisar("oi", True, caixa=self.dados)
        arqs = [f for f in os.listdir(self.dados) if f.endswith(".json")]
        self.assertEqual(len(arqs), 1)
        self.assertEqual(json.loads(open_text(os.path.join(self.dados, arqs[0]))), {"texto": "oi", "agora": True})
        with mock.patch.dict(os.environ, {"TAREFA_NOME": "linear-krn-9"}):  # o recibo acha o aviso pela tarefa
            conta.avisar("de novo", caixa=self.dados)
        textos = [json.loads(open_text(os.path.join(self.dados, f))) for f in os.listdir(self.dados) if f.endswith(".json")]
        self.assertIn("linear-krn-9", [x.get("tarefa") for x in textos])


@unittest.skipUnless(hasattr(os, "fork"), "flock só no Linux")
class TravaTest(unittest.TestCase):
    def test_um_por_vez(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = os.path.join(d, "conta.lock")
            with conta.trava(esperar=0, caminho=caminho) as pegou:
                self.assertTrue(pegou)
                with conta.trava(esperar=0, caminho=caminho) as outro:
                    self.assertFalse(outro)
            ordem = []

            def segundo():
                with conta.trava(esperar=5, caminho=caminho) as p:
                    ordem.append(("segundo", p))

            with conta.trava(esperar=0, caminho=caminho):
                th = threading.Thread(target=segundo)
                th.start()
                time.sleep(0.3)
                ordem.append(("primeiro solta", True))
            th.join()
            self.assertEqual(ordem, [("primeiro solta", True), ("segundo", True)])


if __name__ == "__main__":
    unittest.main()


class ContasComLoginTest(unittest.TestCase):
    def test_so_as_com_login_e_a_1_sempre(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(conta.com_login(home), ("1",))
            for n in "23":
                os.makedirs(os.path.join(home, f".claude-{n}"))
                open(os.path.join(home, f".claude-{n}", ".credentials.json"), "w").close()
            os.makedirs(os.path.join(home, ".claude-4"))  # pasta sem login não conta
            self.assertEqual(conta.com_login(home), ("1", "2", "3"))
            open(os.path.join(home, ".claude-4", ".credentials.json"), "w").close()
            self.assertEqual(conta.com_login(home), ("1", "2", "3", "4"))
            with mock.patch.dict(os.environ, {"CLAUDE_HOME": home}):
                self.assertEqual(conta.com_login(), ("1", "2", "3", "4"))

    def test_faixa_vem_do_hub_json(self):
        self.assertEqual([conta.faixa(n) for n in conta.CONTAS], ["construtor", "construtor", "construtor", "operador"])
        self.assertIsNone(conta.faixa("9"))
