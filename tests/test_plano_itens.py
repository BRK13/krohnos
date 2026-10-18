import tempfile
import unittest
from pathlib import Path

from hub import plano_itens as P

EXEMPLO = Path(__file__).resolve().parent.parent / "exemplos" / "plano-com-check.md"


class FluxoCom5ItensTest(unittest.TestCase):
    """plano de 5 itens (2 em paralelo, 1 dependente, 1 sem check → Sonnet) pelo fluxo inteiro."""

    def setUp(self):
        self.itens = P.parse(EXEMPLO.read_text(encoding="utf-8"))
        self.por_id = {i.id: i for i in self.itens}

    def test_parse_e_validacao(self):
        self.assertEqual([i.id for i in self.itens], ["I1", "I2", "I3", "I4", "I5"])
        self.assertEqual(self.por_id["I3"].after, ["I1", "I2"])
        self.assertEqual(self.por_id["I1"].files, ["hub/a.py", "tests/test_a.py"])
        self.assertEqual(self.por_id["I1"].checks, ["python3 -m unittest tests.test_a"])
        self.assertEqual(P.validar(self.itens), [])
        self.assertEqual(P.avisos(self.itens), ["I5: sem check:, vai para o Sonnet"])

    def test_ondas_sem_conflito(self):
        ids = [[i.id for i in o] for o in P.ondas(self.itens)]
        self.assertEqual(ids, [["I1", "I2", "I4"], ["I3"], ["I5"]])

    def test_roteamento(self):
        r = {i: P.rotear(self.por_id[i])[0] for i in self.por_id}
        self.assertEqual(r, {"I1": "haiku", "I2": "haiku", "I3": "haiku", "I4": "haiku", "I5": "sonnet"})
        self.assertEqual(P.rotear(self.por_id["I5"])[1], "sem check:")

    def test_prompts(self):
        self.assertIn("SOMENTE nestes arquivos: hub/a.py, tests/test_a.py", P.prompt_executor(self.por_id["I1"]))
        rev = P.prompt_revisor([self.por_id["I1"], self.por_id["I2"]])
        self.assertIn("APROVADO", rev)
        self.assertIn("REFAZER", rev)
        self.assertIn("I2:", rev)

    def test_checks_executam_e_viram_prova_para_o_portao(self):
        saida = P.rodar_check("echo ok", cwd=tempfile.gettempdir())
        self.assertEqual((saida["res"], saida["saida"]), ("PASSOU", "ok"))
        ruim = P.rodar_check("exit 3")
        self.assertEqual(ruim["res"], "FALHOU")
        desc = "- check: `echo ok`\n- check: `exit 3`\n"
        bloco = P.bloco_prova([saida, ruim])
        faltas = P.faltas_checks(desc, [bloco])
        self.assertEqual(len(faltas), 1)
        self.assertIn("FALHOU", faltas[0])
        self.assertEqual(P.faltas_checks(desc, [P.bloco_prova([saida, {**ruim, "res": "PASSOU"}])]), [])


class RegrasTest(unittest.TestCase):
    def test_haiku_sem_check_e_erro_e_exec_haiku_nao_burla_a_regra(self):
        itens = P.parse("- [ ] **A** x\n  - files: a.py\n  - exec: haiku\n")
        self.assertIn("A: sem check: não vai para o Haiku", P.validar(itens))
        self.assertEqual(P.rotear(itens[0])[0], "sonnet")

    def test_limites_do_haiku(self):
        def item(extra, titulo="Ajuste"):
            return P.parse(f"- [ ] **A** {titulo}\n  - check: `true`\n{extra}")[0]
        self.assertEqual(P.rotear(item("  - files: a.py, b.py, c.py\n"))[0], "haiku")
        self.assertEqual(P.rotear(item("  - files: a.py, b.py, c.py, d.py\n"))[0], "sonnet")
        self.assertEqual(P.rotear(item("  - files: a.py\n", "Migração do banco"))[0], "sonnet")
        self.assertEqual(P.rotear(item("  - files: hub/auth.py\n"))[0], "sonnet")
        self.assertEqual(P.rotear(item("  - files: a.py\n  - exec: opus\n"))[0], "opus")
        self.assertEqual(P.rotear(item(""))[1], "sem files:")

    def test_ciclo_dependencia_e_id_repetido_e_check_proibido(self):
        ciclo = P.parse("- [ ] **A** a\n  - after: B\n- [ ] **B** b\n  - after: A\n")
        self.assertTrue(any("ciclo" in e for e in P.validar(ciclo)))
        self.assertTrue(any("não existe" in e for e in P.validar(P.parse("- [ ] **A** a\n  - after: Z\n"))))
        self.assertTrue(any("repetido" in e for e in P.validar(P.parse("- [ ] **A** a\n- [ ] **A** b\n"))))
        self.assertTrue(any("proibido" in e for e in P.validar(P.parse("- [ ] **A** a\n  - check: `yarn next build`\n"))))
        self.assertEqual(P.rodar_check("sudo ls")["res"], "FALHOU")

    def test_item_sem_files_roda_sozinho(self):
        itens = P.parse("- [ ] **A** a\n  - files: a.py\n- [ ] **B** b\n- [ ] **C** c\n  - files: c.py\n")
        self.assertEqual([[i.id for i in o] for o in P.ondas(itens)], [["A", "C"], ["B"]])

    def test_checks_da_issue_ignora_prosa_e_codigo(self):
        desc = "O campo `check:` prova.\n```\ncheck: `rm x`\n```\n**Pronto quando:**\n- check: `pytest a`\n- [ ] check: `pytest a`\n- check: curl -fs https://x\n"
        self.assertEqual(P.checks_da_issue(desc), ["pytest a", "curl -fs https://x"])
        self.assertEqual(P.faltas_checks("sem check nenhum", []), [])
        self.assertEqual(len(P.faltas_checks(desc, [""])), 2)

    def test_relatorio_e_vereditos(self):
        rel = "- check: `pytest a` → PASSOU\n  ```\n  1 passed\n  ```\n- check: `pytest b` → FALHOU\n- check: `pytest c` → PASSOU\n"
        self.assertEqual(P.ler_relatorio(rel), {"pytest a": "PASSOU", "pytest b": "FALHOU", "pytest c": "SEM_SAIDA"})
        self.assertEqual(P.ler_vereditos("I1: APROVADO — ok\n- I2: **REFAZER** — grande"), {"I1": "APROVADO", "I2": "REFAZER"})


class RetrabalhoTest(unittest.TestCase):
    def test_taxa_alarme_e_comparacao(self):
        with tempfile.TemporaryDirectory() as d:
            for n in range(10):
                P.registrar("p", f"h{n}", "haiku", "APROVADO" if n < 8 else "AJUSTADO", "docs", 0.01, d)
                P.registrar("p", f"s{n}", "sonnet", "APROVADO", "docs", 0.05, d)
            for n in range(6):
                P.registrar("p", f"c{n}", "haiku", "REFAZER" if n < 3 else "APROVADO", "codigo", 0.01, d)
            regs = P.registros(d)
            t = P.taxa(regs)
            self.assertAlmostEqual(t[("docs", "haiku")]["taxa"], 0.2)
            self.assertFalse(t[("docs", "haiku")]["alarme"])
            self.assertTrue(t[("codigo", "haiku")]["alarme"])
            self.assertEqual(t[("codigo", "haiku")]["refazer"], 3)
            c = P.comparar(regs)
            self.assertEqual(c["docs"]["veredito"], "padrão")
            self.assertAlmostEqual(c["docs"]["economia_pct"], 80.0)
            self.assertEqual(len(P.registros(d, plano="outro")), 0)
            self.assertIn("Retrabalho", P.card_html(d))

    def test_registrar_recusa_valor_torto(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                P.registrar("p", "i", "gpt", "APROVADO", base_dados=d)
            with self.assertRaises(ValueError):
                P.registrar("p", "i", "haiku", "TALVEZ", base_dados=d)


class SegurancaCheckTest(unittest.TestCase):
    def test_rm_recursivo_em_qualquer_forma_e_proibido(self):
        for cmd in ("rm -rf x", "rm -r -f x", "rm -fr x", "rm -f -r x", "rm --recursive x", "ls; rm -R x"):
            self.assertEqual(P.rodar_check(cmd, run=lambda *a, **k: self.fail("rodou"))["res"], "FALHOU", cmd)
        self.assertIsNone(P.RE_PROIBIDO.search("rm -f x.tmp"))

    def test_conferir_com_stdin_roda_os_checks(self):
        import io, contextlib
        from unittest import mock
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("- check: `false`\n")), contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["conferir", "-"]), 1)
        self.assertIn("FALHOU", buf.getvalue())


class PortaoDeDoneTest(unittest.TestCase):
    def test_recibo_cobra_check_sem_saida(self):
        from hub import recibo
        issue = {"identifier": "KRN-1", "title": "t", "description": "**Pronto quando:**\n- check: `echo ok`\n",
                 "createdAt": "2026-10-17T10:00:00.000Z", "completedAt": None, "comments": {"nodes": []}, "attachments": {"nodes": []}}
        with tempfile.TemporaryDirectory() as d:
            r = recibo.conferir(issue, gh=lambda *_: {}, get=lambda *_: None, base_dados=d, comentario="Feito.")
            self.assertTrue(any("check `echo ok`" in f for f in r["faltas"]), r)
            r = recibo.conferir(issue, gh=lambda *_: {}, get=lambda *_: None, base_dados=d,
                                comentario="Feito.\n- check: `echo ok` → PASSOU\n")
            self.assertTrue(any("sem a saída" in f for f in r["faltas"]), r)
            r = recibo.conferir(issue, gh=lambda *_: {}, get=lambda *_: None, base_dados=d,
                                comentario="Feito.\n- check: `echo ok` → PASSOU\n  ```\n  ok\n  ```\n")
            self.assertFalse(any("check `echo ok`" in f for f in r["faltas"]), r)


if __name__ == "__main__":
    unittest.main()
