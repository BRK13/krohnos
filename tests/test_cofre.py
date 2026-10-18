import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from hub import config, cofre

VALOR = "lin_abc123"
CHAVES = ("KROHNOS_CHAVE_MESTRA", "KROHNOS_CONFIG", "KROHNOS_CAMINHOS_COFRE", "KROHNOS_CAMINHOS_SEGREDOS",
          "KROHNOS_COFRE_LEGADO", "NIMBUS_HOME")


class CofreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in CHAVES}
        env["KROHNOS_CONFIG"] = str(self.dir / "nao-existe.toml")
        env["KROHNOS_CAMINHOS_COFRE"] = str(self.dir / "cofre")
        env["KROHNOS_CAMINHOS_SEGREDOS"] = str(self.dir / "env")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.recarregar)
        config.recarregar()
        self.db = self.dir / "cofre" / "cofre.db"


class TestCofre(CofreBase):
    def test_definir_e_ler(self):
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        self.assertIsNone(cofre.ler("nao.existe"))

    def test_listar_nunca_devolve_valor(self):
        cofre.definir("linear.token", VALOR, por="ana")
        itens = cofre.listar()
        self.assertEqual(len(itens), 1)
        self.assertEqual(set(itens[0]), {"nome", "definido_em", "por", "fim"})
        self.assertEqual((itens[0]["nome"], itens[0]["por"], itens[0]["fim"]), ("linear.token", "ana", "c123"))
        self.assertNotIn(VALOR, repr(itens))

    def test_valor_curto_nao_vaza_fim(self):
        cofre.definir("a.b", "xyz1234", por="ana")
        self.assertEqual(cofre.listar()[0]["fim"], "")

    def test_arquivo_sem_valor_em_claro(self):
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertNotIn(VALOR.encode(), self.db.read_bytes())

    def test_chave_gerada_0600(self):
        cofre.definir("linear.token", VALOR, por="ana")
        chave = self.dir / "cofre" / "chave"
        self.assertEqual(stat.S_IMODE(chave.stat().st_mode), 0o600)

    def test_permissoes_db_e_pasta(self):
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)

    def test_chave_criada_duas_vezes_e_corrida(self):
        cofre.definir("linear.token", VALOR, por="ana")
        k1 = (self.dir / "cofre" / "chave").read_bytes()
        cofre.definir("outro.x", "valor-qualquer", por="ana")
        self.assertEqual((self.dir / "cofre" / "chave").read_bytes(), k1)
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        # corrida: o vencedor publica a chave entre o is_file e o link; o perdedor usa a dele
        (self.dir / "cofre" / "chave").unlink()
        real = os.link

        def link_perdendo(src, dst, *a, **k):
            Path(dst).write_bytes(k1 + b"\n")
            os.chmod(dst, 0o600)
            return real(src, dst, *a, **k)  # levanta FileExistsError
        with mock.patch("hub.cofre.os.link", link_perdendo):
            self.assertEqual(cofre.ler("linear.token"), VALOR)
        self.assertEqual([p.name for p in (self.dir / "cofre").iterdir() if p.name.startswith(".chave-")], [])

    def test_chave_aberta_demais_ou_vazia(self):
        cofre.definir("linear.token", VALOR, por="ana")
        chave = self.dir / "cofre" / "chave"
        os.chmod(chave, 0o644)
        with self.assertRaisesRegex(cofre.CofreError, "permiss"):
            cofre.ler("linear.token")
        os.chmod(chave, 0o600)
        chave.write_bytes(b"")
        with self.assertRaises(cofre.CofreError):
            cofre.ler("linear.token")

    def test_valor_com_quebra_de_linha_recusado(self):
        for ruim in ("a\nB=1" * 3, "abc\rdef"):
            with self.assertRaises(cofre.CofreError):
                cofre.definir("linear.token", ruim, por="ana")

    def test_escopo_exato_e_colisao(self):
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertEqual(cofre.env("Linear"), {})
        self.assertEqual(cofre.env("lin"), {})
        with self.assertRaisesRegex(cofre.CofreError, "colide"):
            cofre.definir("linear-token", "outro_valor9", por="ana")
        self.assertEqual(cofre.env("linear"), {"LINEAR_TOKEN": VALOR})

    def test_redefinir_substitui(self):
        cofre.definir("linear.token", VALOR, por="ana")
        cofre.definir("linear.token", "outro_valor9", por="bia")
        self.assertEqual(cofre.ler("linear.token"), "outro_valor9")
        self.assertEqual([i["por"] for i in cofre.listar()], ["bia"])

    def test_apagar_some_e_audita(self):
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertTrue(cofre.apagar("linear.token", por="bia", ip="10.0.0.1"))
        self.assertIsNone(cofre.ler("linear.token"))
        self.assertEqual(cofre.listar(), [])
        a = cofre.auditoria()
        self.assertEqual([(x["acao"], x["quem"], x["nome"]) for x in a],
                         [("apagar", "bia", "linear.token"), ("definir", "ana", "linear.token")])
        self.assertEqual(a[0]["ip"], "10.0.0.1")
        self.assertNotIn(VALOR, repr(a))
        self.assertFalse(cofre.apagar("linear.token", por="bia"))

    def test_chave_mestra_errada_erro_claro(self):
        cofre.definir("linear.token", VALOR, por="ana")
        os.environ["KROHNOS_CHAVE_MESTRA"] = Fernet.generate_key().decode()
        with self.assertRaises(cofre.CofreError) as c:
            cofre.ler("linear.token")
        self.assertNotIn(VALOR, str(c.exception))
        with self.assertRaises(cofre.CofreError):
            cofre.definir("outro.x", "v", por="ana")

    def test_chave_mestra_por_env(self):
        os.environ["KROHNOS_CHAVE_MESTRA"] = Fernet.generate_key().decode()
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        self.assertFalse((self.dir / "cofre" / "chave").exists())

    def test_env_do_escopo(self):
        cofre.definir("linear.token", VALOR, por="ana")
        cofre.definir("linear.time", "KRN", por="ana")
        cofre.definir("github.token", "ghp_zzzz1234", por="ana")
        self.assertEqual(cofre.env("linear"), {"LINEAR_TOKEN": VALOR, "LINEAR_TIME": "KRN"})
        self.assertEqual(cofre.env("nada"), {})

    def test_nome_invalido(self):
        for ruim in ("", "Linear Token", "../x", "a=b"):
            with self.assertRaises(cofre.CofreError):
                cofre.definir(ruim, "v", por="ana")

    def test_valor_vazio_recusado(self):
        with self.assertRaises(cofre.CofreError):
            cofre.definir("linear.token", "", por="ana")


class TestLegado(CofreBase):
    def env_legado(self):
        pasta = self.dir / "env"
        pasta.mkdir()
        (pasta / "linear.env").write_text("LINEAR_TOKEN=lin_legado77\nLINEAR_TIME=KRN\n")

    def test_desligado_por_padrao(self):
        self.env_legado()
        self.assertIsNone(cofre.ler("linear.token"))

    def test_ligado_le_env_quando_nao_esta_no_cofre(self):
        self.env_legado()
        os.environ["KROHNOS_COFRE_LEGADO"] = "1"
        self.assertEqual(cofre.ler("linear.token"), "lin_legado77")
        self.assertEqual(cofre.env("linear")["LINEAR_TIME"], "KRN")
        self.assertEqual(cofre.listar(), [])

    def test_cofre_vence_legado(self):
        self.env_legado()
        os.environ["KROHNOS_COFRE_LEGADO"] = "1"
        cofre.definir("linear.token", VALOR, por="ana")
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        self.assertEqual(cofre.env("linear")["LINEAR_TOKEN"], VALOR)

    def test_so_legado_nao_cria_cofre_nem_estoura(self):
        self.env_legado()
        os.environ["KROHNOS_COFRE_LEGADO"] = "1"
        os.makedirs(self.dir / "cofre")
        os.chmod(self.dir / "cofre", 0o500)
        self.addCleanup(os.chmod, self.dir / "cofre", 0o700)
        self.assertEqual(cofre.ler("linear.token"), "lin_legado77")
        self.assertEqual(cofre.env("linear")["LINEAR_TOKEN"], "lin_legado77")
        self.assertEqual(cofre.listar(), [])
        self.assertEqual(os.listdir(self.dir / "cofre"), [])

    def test_toml(self):
        self.env_legado()
        arq = self.dir / "k.toml"
        arq.write_text("[cofre]\nlegado = true\n")
        os.environ["KROHNOS_CONFIG"] = str(arq)
        config.recarregar()
        self.assertEqual(cofre.ler("linear.token"), "lin_legado77")


class TestCli(CofreBase):
    def rodar(self, *args, entrada=""):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(entrada)), redirect_stdout(out):
            codigo = cofre.main(list(args))
        return codigo, out.getvalue()

    def test_erro_de_arquivo_vira_mensagem(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with mock.patch("hub.cofre.definir", side_effect=PermissionError(13, "Permission denied", "/x/cofre.db")), \
                redirect_stderr(err):
            self.assertEqual(self.rodar("definir", "a.b", entrada="valorvalor1")[0], 1)
        self.assertIn("PermissionError", err.getvalue())

    def test_fluxo(self):
        self.assertEqual(self.rodar("definir", "linear.token", entrada=VALOR + "\n")[0], 0)  # gitleaks:allow (valor falso de teste)
        codigo, saida = self.rodar("listar")
        self.assertIn("linear.token", saida)
        self.assertIn("c123", saida)
        self.assertNotIn(VALOR, saida)
        self.assertEqual(self.rodar("env", "linear")[1].strip(), f"LINEAR_TOKEN={VALOR}")
        self.assertEqual(self.rodar("apagar", "linear.token")[0], 0)
        self.assertEqual(self.rodar("listar")[1].strip(), "")


if __name__ == "__main__":
    unittest.main()
