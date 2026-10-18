"""hub/comum/env.py: a tabela única de casos (export, aspas, comentário, CRLF, arquivo × ambiente)."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hub.comum import env

# (linha do arquivo, valor esperado de K)
TABELA = [
    ("K=abc", "abc"),
    ("export K=abc", "abc"),
    ("export   K=abc", "abc"),
    ("  K  =  abc  ", "abc"),
    ('K="abc"', "abc"),
    ("K='abc'", "abc"),
    ('K="a # b"', "a # b"),
    ('K="abc" # nota', "abc"),
    ("K=abc # nota", "abc"),
    ("K=abc#colado", "abc#colado"),
    ("K=a=b=c", "a=b=c"),
    ('K="abc', '"abc'),
    ("K='abc\"", "'abc\""),
    ("K=", ""),
    ('K=""', ""),
    ("K=abc\r", "abc"),
    ("K=https://x.com/a?b=1#frag", "https://x.com/a?b=1#frag"),
]


class TabelaTest(unittest.TestCase):
    def test_tabela(self):
        for linha, esperado in TABELA:
            with self.subTest(linha=linha), tempfile.TemporaryDirectory() as d:
                p = Path(d) / ".env"
                p.write_bytes(f"# cabeçalho\n\nOUTRA=1\n{linha}\n".encode())
                self.assertEqual(env.ler_env(p).get("K"), esperado)
                self.assertEqual(env.ler_env(p)["OUTRA"], "1")

    def test_linhas_ignoradas(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# K=1\nsem igual\n=sem chave\nA B=1\nOK=1\n")
            self.assertEqual(env.ler_env(p), {"OK": "1"})

    def test_arquivo_ausente(self):
        ausente = Path(tempfile.gettempdir()) / "comum-env-nao-existe"
        self.assertEqual(env.ler_env(ausente), {})
        with self.assertRaises(OSError):
            env.ler_env(ausente, estrito=True)


class PrecedenciaTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.a, self.b = Path(self.d.name, "a.env"), Path(self.d.name, "b.env")
        self.a.write_text("K=do-a\nSO_A=a\n")
        self.b.write_text("K=do-b\nSO_B=b\nVAZIO=\n")

    def test_ambiente_ganha_do_arquivo(self):
        self.assertEqual(env.segredo("K", [self.a], ambiente={"K": "amb"}), "amb")

    def test_primeiro_arquivo_que_tem(self):
        self.assertEqual(env.segredo("K", [self.a, self.b], ambiente={}), "do-a")
        self.assertEqual(env.segredo("SO_B", [self.a, self.b], ambiente={}), "b")

    def test_vazio_conta_como_ausente(self):
        self.assertIsNone(env.segredo("VAZIO", [self.b], ambiente={}))
        self.assertEqual(env.segredo("K", [self.a], ambiente={"K": "  "}), "do-a")
        self.assertIsNone(env.segredo("NADA", [self.a, "/nao/existe"], ambiente={}))

    def test_carregar_env(self):
        amb = {"K": "ja"}
        self.assertEqual(env.carregar_env(self.a, ambiente=amb), ["SO_A"])
        self.assertEqual(amb, {"K": "ja", "SO_A": "a"})
        self.assertEqual(sorted(env.carregar_env(self.a, sobrescrever=True, ambiente=amb)), ["K", "SO_A"])
        self.assertEqual(amb["K"], "do-a")


class CliTest(unittest.TestCase):
    def test_get(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, ".env")
            p.write_text("export SEGREDO_BRU920_X='valor'\n")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(env.main(["get", "SEGREDO_BRU920_X", str(p)]), 0)
            self.assertEqual(out.getvalue(), "valor\n")
            self.assertEqual(env.main(["get", "NAO_EXISTE_BRU920", str(p)]), 1)


if __name__ == "__main__":
    unittest.main()
