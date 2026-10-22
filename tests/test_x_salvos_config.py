"""Config do agente de conhecimento do X: padrões genéricos, arquivo toml opcional, env e validação."""
import os
import tempfile
import unittest
from pathlib import Path

try:
    from hub.x_salvos import config as C
except ImportError:
    C = None


class TestConfig(unittest.TestCase):
    def setUp(self):
        if C is None:
            self.fail("pacote hub.x_salvos ausente")

    def test_padroes(self):
        c = C.carregar(None, env={})
        self.assertEqual((c.destino, c.max_itens, c.max_paginas, c.limite_taxa_s, c.jitter_max_s), ("biblioteca", 150, 40, 1.5, 1200))
        self.assertEqual(c.temas, [])
        self.assertTrue(str(c.cookies).endswith(".config/krohnos/x-cookies.json"))
        self.assertTrue(str(c.saida).endswith("krohnos/x-salvos"))

    def test_toml_e_env_sobrepoe(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x_salvos.toml"
            p.write_text('cookies = "/tmp/c.json"\nsaida = "/tmp/s"\ntemas = ["seo", "video"]\ndestino = "issue"\nmax_itens = 20\n'
                         'max_paginas = 5\nlimite_taxa_s = 3.0\nchrome = "/tmp/chrome"\n')
            c = C.carregar(p, env={})
            self.assertEqual((c.destino, c.max_itens, c.max_paginas, c.limite_taxa_s), ("issue", 20, 5, 3.0))
            self.assertEqual((c.temas, str(c.cookies), str(c.saida), c.chrome), (["seo", "video"], "/tmp/c.json", "/tmp/s", "/tmp/chrome"))
            c = C.carregar(p, env={"X_MAX_ITENS": "30", "KROHNOS_X_SALVOS_DESTINO": "biblioteca"})
            self.assertEqual((c.max_itens, c.destino), (30, "biblioteca"))

    def test_trava_padrao_e_configuravel(self):
        c = C.carregar(None, env={"KROHNOS_X_SALVOS_SAIDA": "/tmp/s"})
        self.assertEqual(str(c.trava), "/tmp/s/.trava")
        c = C.carregar(None, env={"KROHNOS_X_SALVOS_SAIDA": "/tmp/s", "KROHNOS_X_SALVOS_TRAVA": "/run/x-salvos.lock"})
        self.assertEqual(str(c.trava), "/run/x-salvos.lock")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.toml"
            p.write_text('trava = "/run/x-salvos.lock"\n')
            self.assertEqual(str(C.carregar(p, env={}).trava), "/run/x-salvos.lock")

    def test_cookies_origem_opcional(self):
        self.assertIsNone(C.carregar(None, env={}).cookies_origem)
        c = C.carregar(None, env={"KROHNOS_X_SALVOS_COOKIES_ORIGEM": "/tmp/chegada"})
        self.assertEqual(str(c.cookies_origem), "/tmp/chegada")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.toml"
            p.write_text('cookies_origem = "/tmp/o.json"\n')
            self.assertEqual(str(C.carregar(p, env={}).cookies_origem), "/tmp/o.json")

    def test_caminho_por_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.toml"
            p.write_text("max_paginas = 7\n")
            self.assertEqual(C.carregar(None, env={"KROHNOS_X_SALVOS_CONFIG": str(p)}).max_paginas, 7)

    def test_destino_invalido_erro_claro(self):
        with self.assertRaises(C.ConfigInvalida) as e:
            C.carregar(None, env={"KROHNOS_X_SALVOS_DESTINO": "email"})
        self.assertIn("destino", str(e.exception))
        self.assertIn("biblioteca", str(e.exception))

    def test_max_itens_acima_de_500_erro(self):
        with self.assertRaises(C.ConfigInvalida):
            C.carregar(None, env={"X_MAX_ITENS": "501"})
        self.assertEqual(C.carregar(None, env={"X_MAX_ITENS": "500"}).max_itens, 500)

    def test_valores_absurdos_erro(self):
        for env in ({"X_MAX_ITENS": "0"}, {"X_MAX_ITENS": "abc"}, {"KROHNOS_X_SALVOS_LIMITE_TAXA_S": "0"}):
            with self.assertRaises(C.ConfigInvalida):
                C.carregar(None, env=env)

    def test_arquivo_inexistente_explicito_erro(self):
        with self.assertRaises(C.ConfigInvalida):
            C.carregar("/nao/existe/x_salvos.toml", env={})


if __name__ == "__main__":
    unittest.main()
