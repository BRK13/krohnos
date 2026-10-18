import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import painel, seomator_tela, ui
from tests.test_painel import cockpit_neutro

MAU = '<script>alert(1)</script>'


def resumo(dia, nota, regras=()):
    return {"dia": dia, "url": "https://psite13-com.example", "nota": nota, "paginas": 10,
            "categorias": {"perf": 84, "core": 100, "novacat": None}, "regras": list(regras), "duracao_s": 152}


class SeomatorTelaTest(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        alvos = mock.patch.object(seomator_tela.seomator, "alvos", return_value=[
            {"id": "psite13", "nome": "Cliente G", "dominio": "psite13-com.example"}, {"id": "fsite05", "nome": "fsite05", "dominio": "fsite05-com-br.example"}])
        alvos.start()
        self.addCleanup(alvos.stop)

    def gravar(self):
        d = self.base / "seomator" / "psite13"
        d.mkdir(parents=True)
        (self.base / "seomator" / "db").mkdir()
        (self.base / "seomator" / "tmpx").mkdir()
        (self.base / "seomator" / "regras.json").write_text("{}")
        (self.base / "seomator" / "issues.json").write_text(json.dumps({"psite13": {"perf-lazy": {"issue": "KRN-900", "dia": "2026-10-15"}}}))
        (d / "2026-10-08.json").write_text(json.dumps(resumo("2026-10-08", 90)))
        paginas = [f"https://psite13-com.example/p{i}" for i in range(25)] + ["javascript:alert(1)", f"https://x.com/{MAU}"]
        regras = [{"regra": "perf-lazy", "nome": "Lazy Loading Above Fold", "cat": "perf", "status": "fail", "peso": 15,
                   "paginas": paginas, "msg": f"msg {MAU}", "fix": "Tire o lazy", "impacto": 405},
                  {"regra": "eeat-autor", "nome": "Autor", "cat": "eeat", "status": "warn", "peso": 5,
                   "paginas": ["https://psite13-com.example/a"], "msg": "sem autor", "fix": "", "impacto": 5}]
        (d / "2026-10-15.json").write_text(json.dumps(resumo("2026-10-15", 93, regras)))

    def test_sem_auditoria_todo_site_tem_card(self):
        st, h = painel._rota("/seo")
        self.assertEqual(st, 200)
        self.assertEqual(h.count("Ainda sem auditoria."), 2)
        self.assertIn('href="/seo/fsite05"', h)
        self.assertEqual(painel._rota("/seo/fsite05")[0], 200)

    def test_site_que_falhou_mostra_o_motivo_e_nao_some(self):
        (self.base / "seomator" / "fsite05").mkdir(parents=True)
        (self.base / "seomator" / "fsite05" / "status.json").write_text(json.dumps(
            {"falha": {"em": "2026-10-17T05:00:00+00:00", "motivo": f"seomator saiu -6: heap {MAU}"}}))
        h = painel._rota("/seo")[1]
        self.assertIn("falhou: seomator saiu -6: heap", h)
        self.assertNotIn(MAU, h)
        self.assertIn("falhou: seomator saiu -6", painel._rota("/seo/fsite05")[1])
        # com resumo anterior: o card mostra a nota e o aviso de falha
        (self.base / "seomator" / "fsite05" / "2026-10-15.json").write_text(json.dumps(resumo("2026-10-15", 88)))
        h = painel._rota("/seo")[1]
        self.assertIn("falhou: seomator saiu -6", h)
        self.assertRegex(h, r'<span class="r">Nota</span><span class="v[^"]*">88</span>')
        self.assertIn("falhou:", painel._rota("/seo/fsite05")[1])

    def test_lista_e_detalhe(self):
        self.gravar()
        st, h = painel._rota("/seo")
        self.assertEqual(st, 200)
        self.assertIn('href="/seo/psite13"', h)
        self.assertIn("+3 vs auditoria anterior", h)
        self.assertNotIn("/seo/db", h)
        self.assertNotIn("/seo/tmpx", h)
        self.assertNotIn("/seo/casamento", h)
        st, h = painel._rota("/seo/psite13")
        self.assertEqual(st, 200)
        self.assertIn("KRN-900", h)
        self.assertIn("Tire o lazy", h)
        self.assertIn("e mais 7", h)  # 27 páginas, 20 mostradas
        self.assertIn("Nota geral", h)  # gráfico de evolução
        self.assertLess(h.index("erro grave"), h.index(">alerta<"))  # fail antes de warn
        self.assertNotIn(MAU, h)
        self.assertNotIn('href="javascript:', h)
        self.assertIn('href="https://psite13-com.example/p0"', h)
        self.assertIn(">Desempenho<", h)  # categoria em português
        self.assertIn(">novacat<", h)  # id sem rótulo aparece cru

    def test_nota_none_vira_traco(self):
        d = self.base / "seomator" / "psite13"
        d.mkdir(parents=True)
        (d / "2026-10-15.json").write_text(json.dumps(resumo("2026-10-15", None)))
        for rota in ("/seo", "/seo/psite13"):
            h = painel._rota(rota)[1]
            self.assertRegex(h, r'<span class="r">Nota</span><span class="v[^"]*">—</span>', rota)

    def test_site_inexistente(self):
        self.gravar()
        for c in ("/seo/nao-existe", "/seo/db", "/seo/..", "/seo/tmpx"):
            self.assertEqual(painel._rota(c)[0], 404, c)

    def test_menu(self):
        self.assertIn(("/seo", "Auditoria"), dict((g, i) for g, _, i in ui.MENU)["SEO"])


if __name__ == "__main__":
    unittest.main()
