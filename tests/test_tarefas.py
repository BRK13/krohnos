import datetime as dt
import unittest
import unittest.mock
from pathlib import Path

from hub import tarefas as T

BRT = T.BRT
OK = """---
quando: 0 8 * * *   # todo dia
prioridade: 1
modelo: haiku
timeout: 10m
repos: [example-org/a, example-org/b]
externo: false
descricao: Resumo # com cerquilha
---
Faça o resumo.
"""


class ContratoTest(unittest.TestCase):
    def test_parse_ok(self):
        t = T.parse(OK, "resumo-diario")
        self.assertEqual((t.quando, t.prioridade, t.modelo, t.timeout_s), ("0 8 * * *", 1, "haiku", 600))
        self.assertEqual(t.repos, ["example-org/a", "example-org/b"])
        self.assertFalse(t.externo)
        self.assertEqual(t.descricao, "Resumo # com cerquilha")
        self.assertEqual(t.prompt, "Faça o resumo.")
        self.assertFalse(t.pesada)

    def test_erros_dizem_o_campo(self):
        ruim = OK.replace("modelo: haiku", "modelo: gpt").replace("prioridade: 1", "prioridade: 5") \
                 .replace("externo: false", "externo: talvez\ncor: azul")
        with self.assertRaises(T.TarefaInvalida) as e:
            T.parse(ruim, "x")
        msg = str(e.exception)
        for trecho in ("cor", "modelo", "externo"):
            self.assertIn(trecho, msg)

    def test_falta_frontmatter_e_corpo(self):
        with self.assertRaises(T.TarefaInvalida):
            T.parse("só texto", "x")
        with self.assertRaises(T.TarefaInvalida):
            T.parse(OK.split("---\n", 2)[0] + "---\n" + OK.split("---\n")[1] + "---\n", "x")

    def test_timeout(self):
        self.assertEqual(T.timeout_segundos("2h"), 7200)
        self.assertEqual(T.timeout_segundos("45"), 2700)
        self.assertEqual(T.timeout_segundos("90s"), 90)
        with self.assertRaises(T.TarefaInvalida):
            T.timeout_segundos("7h")

    def test_repositorio_valido(self):
        tarefas = T.carregar(pasta=Path(__file__).resolve().parent.parent / "exemplos" / "tarefas", extra=False)
        # as de exemplo (as reais são do repo privado)
        self.assertEqual(sorted(tarefas), ["linear-status", "repos-manutencao", "resumo-diario", "seo-semanal"])
        self.assertNotIn("README", tarefas)


class ValidarTest(unittest.TestCase):
    TXT = "---\nquando: 0 8 * * *\nprioridade: 2\nmodelo: haiku\ntimeout: 10m\nexterno: false\ndescricao: x\n---\nCorpo.\n"

    def test_devolve_a_tarefa(self):
        t = T.validar(self.TXT, "minha-tarefa")
        self.assertEqual((t.nome, t.prioridade, t.prompt), ("minha-tarefa", 2, "Corpo."))

    def test_invalida_levanta_com_o_campo(self):
        with self.assertRaises(T.TarefaInvalida) as c:
            T.validar(self.TXT.replace("haiku", "gpt"), "minha-tarefa")
        self.assertIn("'modelo'", str(c.exception))

    def test_nome_com_caminho_recusado(self):
        for nome in ("../x", "a/b", "A", "", "a\n"):
            with self.assertRaises(T.TarefaInvalida):
                T.validar(self.TXT, nome)


class CamadaDoPainelTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.extra = Path(tmp.name) / "repo", Path(tmp.name) / "dados" / "tarefas"
        self.repo.mkdir()
        (self.repo / "a-um.md").write_text(OK, encoding="utf-8")
        (self.repo / "b-dois.md").write_text(OK, encoding="utf-8")

    def escrever(self, nome, texto):
        self.extra.mkdir(parents=True, exist_ok=True)
        (self.extra / f"{nome}.md").write_text(texto, encoding="utf-8")

    def test_sem_a_pasta_como_hoje(self):
        self.assertEqual(sorted(T.carregar(self.repo, self.extra)), ["a-um", "b-dois"])
        self.assertEqual(sorted(T.carregar(self.repo)), ["a-um", "b-dois"])

    def test_sobrescrita_vence_e_nova_aparece(self):
        self.escrever("a-um", OK.replace("prioridade: 1", "prioridade: 3"))
        self.escrever("c-nova", OK)
        t = T.carregar(self.repo, self.extra)
        self.assertEqual(sorted(t), ["a-um", "b-dois", "c-nova"])
        self.assertEqual(t["a-um"].prioridade, 3)
        self.assertEqual(t["b-dois"].prioridade, 1)
        origens = {x["nome"]: x["origem"] for x in T.listar(self.repo, self.extra)}
        self.assertEqual(origens, {"a-um": "sobrescrita", "b-dois": "repo", "c-nova": "painel"})

    def test_remover_a_copia_volta_ao_do_repositorio(self):
        self.escrever("a-um", OK.replace("prioridade: 1", "prioridade: 3"))
        (self.extra / "a-um.md").unlink()
        self.assertEqual(T.carregar(self.repo, self.extra)["a-um"].prioridade, 1)

    def test_copia_invalida_do_painel_e_ignorada_e_a_do_repo_segue(self):
        self.escrever("a-um", "---\nquando: lixo\n---\n")
        t = T.carregar(self.repo, self.extra)
        self.assertEqual(t["a-um"].prioridade, 1)
        x = next(x for x in T.listar(self.repo, self.extra) if x["nome"] == "a-um")
        self.assertIsNotNone(x["erro"])
        self.assertIsNotNone(x["tarefa"])

    def test_erro_na_base_continua_derrubando(self):
        (self.repo / "ruim.md").write_text("sem frontmatter", encoding="utf-8")
        with self.assertRaises(T.TarefaInvalida):
            T.carregar(self.repo, self.extra)

    def test_nome_invalido_entra_na_lista_com_erro(self):
        (self.repo / "Nome_Ruim.md").write_text(OK, encoding="utf-8")
        x = next(x for x in T.listar(self.repo, self.extra) if x["nome"] == "Nome_Ruim")
        self.assertIsNone(x["tarefa"])
        self.assertIn("nome do arquivo", x["erro"])

    def test_extra_igual_a_base_nao_duplica(self):
        self.assertEqual(sorted(T.carregar(self.repo, self.repo)), ["a-um", "b-dois"])

    def test_config_tarefas_extra_e_dados_barra_tarefas_e_nao_substitui_a_base(self):
        import os
        from hub import config
        with unittest.mock.patch.dict(os.environ, {"KROHNOS_CAMINHOS_DADOS": str(self.extra.parent), "KROHNOS_CONFIG": "/nao/existe"}):
            config.recarregar()
            self.escrever("x", OK)
            self.assertEqual(config.tarefas_extra(), self.extra)
            self.assertNotEqual(config.tarefas(), self.extra)  # a pasta de dados com tarefas/ não vira a base
        config.recarregar()


class CronTest(unittest.TestCase):
    def test_bate_em_brt(self):
        c = T.Cron("0 8 * * *")
        self.assertTrue(c.bate(dt.datetime(2026, 10, 8, 11, 0, tzinfo=dt.timezone.utc)))  # 8h BRT
        self.assertFalse(c.bate(dt.datetime(2026, 10, 8, 8, 0, tzinfo=dt.timezone.utc)))

    def test_semana_e_listas(self):
        c = T.Cron("30 6-8/2 * * 1,7")
        self.assertTrue(c.bate(dt.datetime(2026, 10, 11, 6, 30, tzinfo=BRT)))   # domingo
        self.assertTrue(c.bate(dt.datetime(2026, 10, 12, 8, 30, tzinfo=BRT)))   # segunda
        self.assertFalse(c.bate(dt.datetime(2026, 10, 12, 7, 30, tzinfo=BRT)))
        self.assertFalse(c.bate(dt.datetime(2026, 10, 13, 6, 30, tzinfo=BRT)))  # terça

    def test_dia_ou_semana(self):
        c = T.Cron("0 2 1 * 6")  # dia 1 OU sábado, como no cron
        self.assertTrue(c.bate(dt.datetime(2026, 10, 1, 2, 0, tzinfo=BRT)))
        self.assertTrue(c.bate(dt.datetime(2026, 10, 10, 2, 0, tzinfo=BRT)))
        self.assertFalse(c.bate(dt.datetime(2026, 10, 9, 2, 0, tzinfo=BRT)))

    def test_entre(self):
        c = T.Cron("0 8 * * *")
        a = dt.datetime(2026, 10, 8, 7, 57, tzinfo=BRT)
        self.assertEqual(c.entre(a, a + dt.timedelta(minutes=5)), dt.datetime(2026, 10, 8, 8, 0, tzinfo=BRT))
        self.assertIsNone(c.entre(a + dt.timedelta(minutes=3), a + dt.timedelta(minutes=8)))

    def test_cron_invalido(self):
        for expr in ("0 8 * *", "61 * * * *", "* * * * x", "*/0 * * * *"):
            with self.assertRaises(ValueError, msg=expr):
                T.Cron(expr)

    def test_de_dia(self):
        self.assertTrue(T.de_dia(dt.datetime(2026, 10, 8, 8, 0, tzinfo=BRT)))
        self.assertFalse(T.de_dia(dt.datetime(2026, 10, 8, 22, 0, tzinfo=BRT)))
        self.assertFalse(T.de_dia(dt.datetime(2026, 10, 8, 3, 0, tzinfo=BRT)))


if __name__ == "__main__":
    unittest.main()
