import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import config, init, sites, usuarios


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.env = {"KROHNOS_CONFIG": str(self.tmp / "etc" / "krohnos.toml"),
                    "KROHNOS_CAMINHOS_DADOS": str(self.tmp / "dados"),
                    "KROHNOS_CAMINHOS_COFRE": str(self.tmp / "cofre"),
                    "KROHNOS_CAMINHOS_SEGREDOS": str(self.tmp / "segredos")}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        for var in [v for v in os.environ if v.startswith("KROHNOS_") and v not in self.env]:
            os.environ.pop(var)  # tests/__init__ fixa KROHNOS_GITHUB_ORG etc.; patch.dict restaura no fim
        self.addCleanup(p.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)

    ARGS = ["--sim", "--dominio", "krohnos.exemplo.test", "--dono-email", "ana@exemplo.test", "--dono-nome", "Ana",
            "--org", "exemplo-org", "--projeto-id", "meu-site", "--projeto-dominio", "meu.exemplo.test",
            "--projeto-repo", "meu-site"]

    def rodar(self, args=None):
        with mock.patch("sys.stdout"):
            self.assertEqual(init.main(args or self.ARGS), 0)
        config.recarregar()

    def test_sim_cria_tudo(self):
        self.rodar()
        toml = Path(self.env["KROHNOS_CONFIG"])
        self.assertTrue(toml.is_file())
        init.validar_toml(toml.read_text())
        config.validar()
        self.assertEqual(config.get("dono.email"), "ana@exemplo.test")
        self.assertEqual(config.get("painel.dominio"), "krohnos.exemplo.test")
        self.assertEqual(config.get("github.org"), "exemplo-org")
        chave = self.tmp / "cofre" / init.CHAVE_MESTRA
        self.assertEqual(stat.S_IMODE(chave.stat().st_mode), 0o600)
        con = sqlite3.connect(self.tmp / "cofre" / init.COFRE_DB)
        self.assertEqual(con.execute("select count(*) from segredos").fetchone()[0], 0)
        con.close()
        # o dono vira usuário do painel (antes: PAINEL_SENHA em painel.env)
        self.assertEqual([(u["email"], u["papel"]) for u in usuarios.listar()], [("ana@exemplo.test", "dono")])
        self.assertEqual(stat.S_IMODE(usuarios.banco(self.tmp / "dados").stat().st_mode), 0o600)
        self.assertFalse((self.tmp / "segredos" / "painel.env").exists())
        self.assertNotIn("senha", toml.read_text().lower())
        self.assertEqual([s["id"] for s in sites.carregar(config.projetos())], ["meu-site"])
        self.assertEqual(sites.carregar(config.projetos())[0]["repo"]["github"], "exemplo-org/meu-site")

    def test_segunda_execucao_preserva(self):
        self.rodar()
        chave = self.tmp / "cofre" / init.CHAVE_MESTRA
        antes = (chave.read_text(), usuarios.listar())
        self.rodar()
        self.assertEqual((chave.read_text(), usuarios.listar()), antes)
        self.assertEqual(stat.S_IMODE(chave.stat().st_mode), 0o600)

    def test_senha_por_variavel(self):
        with mock.patch.dict(os.environ, {"MINHA_SENHA": "s3nha-forte-123"}):
            self.rodar(self.ARGS + ["--dono-senha-env", "MINHA_SENHA"])
        self.assertTrue(usuarios.entrar("ana@exemplo.test", "s3nha-forte-123"))

    def test_instalacao_antiga_mantem_painel_senha(self):
        (self.tmp / "segredos").mkdir()
        (self.tmp / "segredos" / "painel.env").write_text("PAINEL_SENHA=curta-antiga\n")
        saida = io.StringIO()
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": ""}), mock.patch("sys.stdout", saida):
            self.assertEqual(init.main(self.ARGS), 0)
        impresso = saida.getvalue()
        self.assertIn("senha antiga do painel", impresso)
        self.assertTrue(usuarios.entrar("ana@exemplo.test", "curta-antiga"))
        self.assertNotIn("curta-antiga", impresso)  # a senha antiga não é impressa

    def test_senha_explicita_vence_a_antiga(self):
        (self.tmp / "segredos").mkdir()
        (self.tmp / "segredos" / "painel.env").write_text("PAINEL_SENHA=curta-antiga\n")
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": "", "MINHA_SENHA": "s3nha-forte-123"}):
            self.rodar(self.ARGS + ["--dono-senha-env", "MINHA_SENHA"])
        self.assertTrue(usuarios.entrar("ana@exemplo.test", "s3nha-forte-123"))
        self.assertFalse(usuarios.entrar("ana@exemplo.test", "curta-antiga"))

    def test_garantir_dono_sem_email_e_com_usuario(self):
        d = self.tmp / "d"
        self.assertEqual(init.garantir_dono(d, "", "senha-boa-12345"), "senha-boa-12345")
        self.assertEqual(usuarios.contar(pasta=d), 1)
        self.assertIsNone(init.garantir_dono(d, "ana@exemplo.test", "outra-senha-123"))
        self.assertEqual(usuarios.contar(pasta=d), 1)

    def test_exemplo_copia(self):
        self.rodar(["--sim", "--dominio", "krohnos.exemplo.test", "--dono-email", "ana@exemplo.test", "--exemplo"])
        self.assertTrue(sites.carregar(config.projetos()))

    def test_entradas_invalidas(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(init.main(["--sim", "--dominio", "https://x.com/a", "--dono-email", "user8@example.com"]), 1)
            self.assertEqual(init.main(["--sim", "--dominio", "x.com", "--dono-email", "ruim"]), 1)
            self.assertEqual(init.main(["--sim", "--dominio", "x.com"]), 1)
            self.assertEqual(init.main(self.ARGS + ["--dono-senha-env", "NAO_EXISTE_XYZ"]), 1)

    def test_sim_exige_projeto_dominio(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(init.main(["--sim", "--dominio", "x.exemplo.test", "--dono-email", "user8@example.com"]), 1)
            self.assertEqual(init.main(["--sim", "--dominio", "x.exemplo.test", "--dono-email", "user8@example.com",
                                        "--projeto-id", "abc"]), 1)
        self.assertFalse((self.tmp / "cofre").exists())

    def test_falha_de_validacao_nao_grava_nada(self):
        with mock.patch("sys.stderr"):
            for ruim in (["--projeto-id", "../fora"], ["--projeto-id", "a" * 80], ["--projeto-repo", ".x/y"],
                         ["--projeto-repo", "../x"]):
                args = [a for a in self.ARGS]
                for k, v in zip(ruim[::2], ruim[1::2]):
                    args[args.index(k) + 1] = v
                self.assertEqual(init.main(args), 1, ruim)
        for pasta in ("cofre", "segredos", "dados", "etc"):
            self.assertFalse((self.tmp / pasta).exists(), pasta)
        self.assertFalse(list(self.tmp.rglob("*.json")))

    def test_senha_invalida(self):
        with mock.patch.dict(os.environ, {"S1": "curta-11chr", "S2": "boa-senha-1\noutra"}), mock.patch("sys.stderr"):
            self.assertEqual(init.main(self.ARGS + ["--dono-senha-env", "S1"]), 1)
            self.assertEqual(init.main(self.ARGS + ["--dono-senha-env", "S2"]), 1)
        self.assertFalse((self.tmp / "segredos").exists())

    def test_senha_gerada_impressa_uma_vez_e_fora_do_toml(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(init.main(self.ARGS), 0)
        senha = buf.getvalue().split("guarde): ", 1)[1].split()[0]
        self.assertTrue(usuarios.entrar("ana@exemplo.test", senha))
        self.assertEqual(buf.getvalue().count(senha), 1)
        self.assertNotIn(senha, Path(self.env["KROHNOS_CONFIG"]).read_text())

    def test_modos_das_pastas_e_do_cofre(self):
        self.rodar()
        for pasta in ("cofre", "segredos"):
            self.assertEqual(stat.S_IMODE((self.tmp / pasta).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.tmp / "cofre" / init.COFRE_DB).stat().st_mode), 0o600)

    def test_cofre_sem_chave_e_recusado(self):
        (self.tmp / "cofre").mkdir()
        (self.tmp / "cofre" / init.COFRE_DB).write_bytes(b"x")
        with mock.patch("sys.stderr"):
            self.assertEqual(init.main(self.ARGS), 1)
        self.assertFalse((self.tmp / "cofre" / init.CHAVE_MESTRA).exists())
        self.assertFalse((self.tmp / "segredos").exists())

    def test_arquivos_vazios_sao_refeitos(self):
        (self.tmp / "cofre").mkdir()
        (self.tmp / "segredos").mkdir()
        (self.tmp / "cofre" / init.CHAVE_MESTRA).write_text("")
        (self.tmp / "segredos" / "painel.env").write_text("")  # resto antigo vazio: ignorado, o dono vira usuário
        self.rodar()
        self.assertTrue((self.tmp / "cofre" / init.CHAVE_MESTRA).read_text().strip())
        self.assertEqual(usuarios.contar(), 1)
        self.assertEqual(stat.S_IMODE((self.tmp / "cofre" / init.CHAVE_MESTRA).stat().st_mode), 0o600)

    def test_escrever_atomico_so_se_novo(self):
        arq = self.tmp / "a.txt"
        self.assertTrue(init.escrever_atomico(arq, "1", so_se_novo=True))
        self.assertFalse(init.escrever_atomico(arq, "2", so_se_novo=True))
        self.assertEqual(arq.read_text(), "1")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    def test_sim_sem_projeto_id_deriva_do_dominio(self):
        self.rodar(["--sim", "--dominio", "krohnos.exemplo.test", "--dono-email", "ana@exemplo.test",
                    "--projeto-dominio", "meu.exemplo.test"])  # nunca pergunta (input fecharia o teste com EOFError)
        self.assertTrue((self.tmp / "dados" / "projetos" / "meu.json").is_file())

    def test_so_se_novo_sem_hardlink(self):
        arq = self.tmp / "b.txt"
        with mock.patch("os.link", side_effect=OSError("sem link")):
            self.assertTrue(init.escrever_atomico(arq, "1", 0o600, so_se_novo=True))
            self.assertFalse(init.escrever_atomico(arq, "2", 0o600, so_se_novo=True))
        self.assertEqual((arq.read_text(), stat.S_IMODE(arq.stat().st_mode)), ("1", 0o600))
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    def test_cofre_existente_frouxo_e_apertado(self):
        cofre = self.tmp / "cofre"
        cofre.mkdir()
        init.garantir_cofre(cofre)
        os.chmod(cofre / init.COFRE_DB, 0o666)
        self.assertFalse(init.garantir_cofre(cofre))
        self.assertEqual(stat.S_IMODE((cofre / init.COFRE_DB).stat().st_mode), 0o600)

    def test_toml_recusa_segredo(self):
        with self.assertRaises(config.ConfigError):
            init.validar_toml('[painel]\nsenha = "x"\n')


if __name__ == "__main__":
    unittest.main()
