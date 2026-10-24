"""(princípio 14): toda falha deixa o harness melhor. Registro, classe, peça que muda e a retro."""
import datetime as dt, tempfile, unittest
from pathlib import Path
from hub import incidentes

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 20, 15, tzinfo=UTC)


class Incidentes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_registra_bru667_e_aparece_pendente_na_retro(self):
        i = incidentes.registrar("KRN-667 foi para Done sem os vídeos", "pronto_sem_recibo",
                                 "hub/recibo.py: Done só com recibo", issue="KRN-667", base_dados=self.dados, agora=T0)
        self.assertTrue(i["id"].startswith("INC-"))
        texto = incidentes.texto_retro(24, base_dados=self.dados, agora=T0 + dt.timedelta(hours=1))
        self.assertIn("KRN-667", texto)
        self.assertIn("pendente", texto)

    def test_marcar_feito_tira_dos_pendentes(self):
        i = incidentes.registrar("x", "outra", "y", base_dados=self.dados, agora=T0)
        incidentes.marcar(i["id"], "feito", "PR #240; atualizar-hub rc=0; tests/test_recibo.py", base_dados=self.dados, agora=T0,
                          gh=lambda caminho: {"state": "closed", "merged": True})
        self.assertEqual([], incidentes.listar(so_pendentes=True, base_dados=self.dados, agora=T0))

    def test_feito_sem_teste_citado_recusa(self):
        # incidente fechado exige o teste de regressão citado; o recusado continua pendente
        i = incidentes.registrar("x", "outra", "y", base_dados=self.dados, agora=T0)
        for nota in ("PR #240; atualizar-hub rc=0", "", "sem teste:"):
            with self.assertRaises(ValueError):
                incidentes.marcar(i["id"], "feito", nota, base_dados=self.dados, agora=T0)
        self.assertEqual(1, len(incidentes.listar(so_pendentes=True, base_dados=self.dados, agora=T0)))

    def test_feito_com_teste_ou_isencao_explicita(self):
        for nota in ("conserto em hub/recibo.py; python3 -m unittest tests.test_recibo", "teste novo em test_recibo.py",
                     "descartado: duplicata do INC-3", "sem mudança de código: recuperado pelo alarme",
                     "sem teste: só operação, a fila voltou sozinha"):
            i = incidentes.registrar(nota, "outra", "y", base_dados=self.dados, agora=T0)
            incidentes.marcar(i["id"], "feito", nota, base_dados=self.dados, agora=T0)
        self.assertEqual([], incidentes.listar(so_pendentes=True, base_dados=self.dados, agora=T0))
        self.assertTrue(incidentes.cita_teste("tests/test_x.py"))
        self.assertFalse(incidentes.isento_de_teste("tests/test_x.py; descartado"))

    def test_chave_nao_duplica_incidente_pendente(self):
        a = incidentes.registrar("parada", "tarefa_parada", "p", chave="parada:KRN-589", base_dados=self.dados, agora=T0)
        b = incidentes.registrar("parada", "tarefa_parada", "p", chave="parada:KRN-589", base_dados=self.dados, agora=T0)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(1, len(incidentes.listar(base_dados=self.dados, agora=T0)))

    def test_mesma_issue_classe_e_falha_com_chaves_diferentes_vira_um_incidente(self):
        a = incidentes.registrar("URL com crase  dá 404", "pronto_sem_recibo", "p", issue="KRN-521",
                                 chave="recibo:KRN-521:1", base_dados=self.dados, agora=T0)
        b = incidentes.registrar("url com crase dá 404", "pronto_sem_recibo", "p", issue="KRN-521",
                                 chave="recibo:KRN-521:2", base_dados=self.dados, agora=T0 + dt.timedelta(hours=1))
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(2, b["ocorrencias"])
        itens = incidentes.listar(base_dados=self.dados, agora=T0)
        self.assertEqual(1, len(itens))
        self.assertEqual(2, itens[0]["ocorrencias"])

    def test_texto_ou_issue_diferente_gera_outro_incidente(self):
        incidentes.registrar("falta A", "pronto_sem_recibo", "p", issue="KRN-521", chave="a", base_dados=self.dados, agora=T0)
        incidentes.registrar("falta B", "pronto_sem_recibo", "p", issue="KRN-521", chave="b", base_dados=self.dados, agora=T0)
        incidentes.registrar("falta A", "pronto_sem_recibo", "outra peça", issue="KRN-738", chave="c", base_dados=self.dados, agora=T0)
        self.assertEqual(3, len(incidentes.listar(base_dados=self.dados, agora=T0)))

    def test_incidente_feito_nao_absorve_nova_ocorrencia(self):
        a = incidentes.registrar("falta A", "pronto_sem_recibo", "p", issue="KRN-521", chave="a", base_dados=self.dados, agora=T0)
        incidentes.marcar(a["id"], nota="sem mudança de código", base_dados=self.dados, agora=T0)
        b = incidentes.registrar("falta A", "pronto_sem_recibo", "p", issue="KRN-521", chave="b", base_dados=self.dados, agora=T0)
        self.assertNotEqual(a["id"], b["id"])

    def test_mesma_classe_e_peca_em_issues_diferentes_vira_um_incidente(self):
        for n, issue in enumerate(["KRN-1097", "KRN-1528", "KRN-997", "KRN-1136", "KRN-1000"]):
            inc = incidentes.registrar(f"execução de linear-{issue.lower()} sem tipo", "execucao_sem_tipo", "hub/retomada.py",
                                       issue, chave=f"sem-tipo:{n}", base_dados=self.dados, agora=T0)
        itens = incidentes.listar(base_dados=self.dados, agora=T0)
        self.assertEqual(1, len(itens))
        self.assertEqual(5, itens[0]["ocorrencias"])
        self.assertEqual(["KRN-1097", "KRN-1528", "KRN-997", "KRN-1136", "KRN-1000"], itens[0]["issues"])
        self.assertEqual(itens[0]["id"], inc["id"])
        self.assertIn("×5", incidentes.texto_retro(24, base_dados=self.dados, agora=T0))

    def test_registro_depois_do_feito_abre_outro_e_conserto_cobre_os_anteriores(self):
        a = incidentes.registrar("x", "execucao_sem_tipo", "p", issue="KRN-1", base_dados=self.dados, agora=T0)
        incidentes.marcar(a["id"], "feito", "tests/test_retomada.py", base_dados=self.dados, agora=T0 + dt.timedelta(hours=1),
                          gh=lambda caminho: {"state": "closed", "merged": True})
        b = incidentes.registrar("y", "execucao_sem_tipo", "p", issue="KRN-2", base_dados=self.dados, agora=T0 + dt.timedelta(hours=2))
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(1, b["ocorrencias"] if "ocorrencias" in b else 1)

    def test_feito_com_conserto_fecha_pendentes_antigos_da_mesma_causa(self):
        a = incidentes.registrar("x", "outra", "p", base_dados=self.dados, agora=T0)
        b = incidentes.registrar("z", "outra", "p", base_dados=self.dados, agora=T0 + dt.timedelta(minutes=5))
        c = incidentes.registrar("w", "outra", "outra peça", base_dados=self.dados, agora=T0)
        incidentes.marcar(a["id"], "feito", "tests/test_x.py", base_dados=self.dados, agora=T0 + dt.timedelta(hours=1),
                          gh=lambda caminho: {"state": "closed", "merged": True})
        pend = [i["id"] for i in incidentes.listar(so_pendentes=True, base_dados=self.dados, agora=T0)]
        self.assertEqual([c["id"]], pend)
        nota = {i["id"]: i.get("nota") for i in incidentes.listar(base_dados=self.dados, agora=T0)}
        self.assertEqual(f"coberto por {a['id']}", nota[b["id"]])

    def test_registros_simultaneos_tem_ids_distintos(self):
        # sem a trava, duas leituras do mesmo tamanho dão o mesmo INC-N e o listar funde os dois
        import threading
        def rodar():
            for k in range(20):
                incidentes.registrar(f"falha {k}", "outra", "p", base_dados=self.dados, agora=T0)
        ts = [threading.Thread(target=rodar) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ids = [i["id"] for i in incidentes.listar(base_dados=self.dados, agora=T0)]
        self.assertEqual(40, len(ids))
        self.assertEqual(40, len(set(ids)))

    def test_linha_ruim_nao_derruba_a_leitura(self):
        arq = Path(self.dados) / "claude" / "incidentes.jsonl"
        arq.parent.mkdir(parents=True)
        arq.write_text('[1, 2]\n{"sem_id": 1}\n{"truncad', encoding="utf-8")
        incidentes.registrar("x", "outra", "y", base_dados=self.dados, agora=T0)
        self.assertEqual(1, len(incidentes.listar(base_dados=self.dados, agora=T0)))

    def test_classe_invalida(self):
        with self.assertRaises(ValueError):
            incidentes.registrar("x", "nao-existe", "y", base_dados=self.dados)

    def test_cli(self):
        from unittest import mock
        import os
        from hub import linear
        with mock.patch.dict(os.environ, {"HUB_DATA": self.dados}):
            self.assertEqual(0, linear.main(["incidente", "registrar", "--falha", "main vermelho #695",
                                             "--classe", "main_vermelho", "--peca", "hub/main_vermelho.py"]))
            self.assertEqual(0, linear.main(["incidente", "listar", "--pendentes"]))
        self.assertEqual(1, len(incidentes.listar(base_dados=self.dados)))
