import datetime as dt
import tempfile
import unittest

from hub import janelas


class JanelasTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name

    def test_prazos_do_google(self):
        self.assertEqual(janelas.PRAZOS["titulo"]["julgar_apos"], 14)
        self.assertEqual(janelas.PRAZOS["pagina_nova"]["julgar_apos"], 28)
        self.assertEqual(janelas.PRAZOS["canonical"]["julgar_apos"], 42)
        self.assertEqual(janelas.PRAZOS["core_update"]["julgar_apos"], 180)

    def test_normalizar(self):
        self.assertEqual(janelas.normalizar("https://www.site-b.exemplo.com/a/?x=1#y"), "site-b.exemplo.com/a")
        self.assertEqual(janelas.normalizar("/a/", "site-b.exemplo.com"), "site-b.exemplo.com/a")
        self.assertEqual(janelas.normalizar("https://site-b.exemplo.com"), "site-b.exemplo.com/")
        with self.assertRaises(ValueError):
            janelas.normalizar("/a")

    def test_registrar_e_janela_aberta_ate_a_data(self):
        r = janelas.registrar("fsite06", "/quiropraxia", "titulo", "2026-10-01", pr="https://github.com/x/1", base_dados=self.dados)
        self.assertEqual(r["julgar_em"], "2026-10-15")
        self.assertEqual(r["pagina"], "site-b.exemplo.com/quiropraxia")
        dia = dt.date(2026, 10, 14)
        self.assertEqual(len(janelas.abertas(dia, base_dados=self.dados)), 1)
        self.assertIsNotNone(janelas.pode_mexer("https://www.site-b.exemplo.com/quiropraxia/", "site-b.exemplo.com", dia, self.dados))
        self.assertIsNone(janelas.pode_mexer("/outra", "site-b.exemplo.com", dia, self.dados))
        self.assertEqual(janelas.abertas(dt.date(2026, 10, 15), base_dados=self.dados), [])
        self.assertEqual(janelas.abertas(dia, site_id="asite01", base_dados=self.dados), [])

    def test_tipo_e_site_validos(self):
        with self.assertRaises(ValueError):
            janelas.registrar("fsite06", "/a", "cor", base_dados=self.dados)
        with self.assertRaises(ValueError):
            janelas.registrar("nao-existe", "/a", "titulo", base_dados=self.dados)

    def test_texto(self):
        self.assertIn("nenhuma", "\n".join(janelas.texto_abertas([])))
        janelas.registrar("asite01", "/blog/x", "canonical", "2026-10-07", base_dados=self.dados)
        texto = "\n".join(janelas.texto_abertas(janelas.abertas(dt.date(2026, 10, 8), base_dados=self.dados)))
        self.assertIn("site-a.exemplo.com/blog/x · canonical em 2026-10-07 · julgar a partir de 2026-11-18", texto)


if __name__ == "__main__":
    unittest.main()
