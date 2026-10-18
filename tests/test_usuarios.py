import datetime as dt
import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from hub import usuarios

T0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
SENHA = "senha-longa-123"


class UsuariosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def _linhas(self, sql):
        con = sqlite3.connect(usuarios.banco())
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_criar_guarda_hash_scrypt(self):
        assert usuarios.criar(" Ana@X.test ", SENHA, papel="dono", agora=T0) == "ana@x.test"
        (h,), = self._linhas("select hash from usuario")
        self.assertTrue(h.startswith("scrypt$16384$8$1$"))
        self.assertNotIn(SENHA, h)

    def test_banco_modo_0600(self):
        usuarios.criar("ana@x.test", SENHA)
        self.assertEqual(os.stat(usuarios.banco()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(usuarios.banco().parent).st_mode & 0o777, 0o700)

    def test_entrar_certo_grava_so_sha256(self):
        usuarios.criar("ana@x.test", SENHA, agora=T0)
        tok = usuarios.entrar("ana@x.test", SENHA, ip="1.2.3.4", agora=T0)
        self.assertIsInstance(tok, str)
        self.assertGreaterEqual(len(tok), 43)
        linhas = self._linhas("select * from sessao")
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0][0], hashlib.sha256(tok.encode()).hexdigest())
        self.assertNotIn(tok, linhas[0])

    def test_entrar_errado_e_inexistente_gastam_scrypt(self):
        usuarios.criar("ana@x.test", SENHA)
        real = usuarios.hashlib.scrypt
        with mock.patch.object(usuarios.hashlib, "scrypt", side_effect=real) as m:
            self.assertIsNone(usuarios.entrar("ana@x.test", "errada-errada-1"))
            self.assertEqual(m.call_count, 1)
        with mock.patch.object(usuarios.hashlib, "scrypt", side_effect=real) as m:
            self.assertIsNone(usuarios.entrar("naoexiste@x.test", SENHA))
            self.assertEqual(m.call_count, 1)
        usuarios.desativar("ana@x.test")
        with mock.patch.object(usuarios.hashlib, "scrypt", side_effect=real) as m:
            self.assertIsNone(usuarios.entrar("ana@x.test", SENHA))
            self.assertEqual(m.call_count, 1)

    def _logar(self, papel="operador"):
        usuarios.criar("ana@x.test", SENHA, papel=papel, agora=T0)
        return usuarios.entrar("ana@x.test", SENHA, agora=T0)

    def test_sessao_valida(self):
        tok = self._logar()
        s = usuarios.sessao(tok, agora=T0 + dt.timedelta(minutes=5))
        self.assertEqual(s["email"], "ana@x.test")
        self.assertEqual(s["papel"], "operador")
        self.assertTrue(s["csrf"])
        self.assertIsNone(usuarios.sessao("", agora=T0))
        self.assertIsNone(usuarios.sessao("lixo", agora=T0))

    def test_sessao_ociosa_12h(self):
        tok = self._logar()
        self.assertIsNone(usuarios.sessao(tok, agora=T0 + dt.timedelta(hours=12, seconds=1)))
        self.assertEqual(self._linhas("select * from sessao"), [])

    def test_sessao_12h_exatas_ainda_vale(self):
        tok = self._logar()
        self.assertIsNotNone(usuarios.sessao(tok, agora=T0 + dt.timedelta(hours=12)))

    def test_sessao_maxima_7_dias_mesmo_com_uso(self):
        tok = self._logar()
        t = T0
        while t < T0 + dt.timedelta(days=7):
            t += dt.timedelta(hours=6)
            if t <= T0 + dt.timedelta(days=7):
                self.assertIsNotNone(usuarios.sessao(tok, agora=t))
        self.assertIsNone(usuarios.sessao(tok, agora=T0 + dt.timedelta(days=7, hours=6)))

    def test_visto_escreve_no_maximo_1x_por_minuto(self):
        tok = self._logar()
        usuarios.sessao(tok, agora=T0 + dt.timedelta(seconds=10))
        (v,), = self._linhas("select visto from sessao")
        self.assertEqual(v, T0.isoformat())
        usuarios.sessao(tok, agora=T0 + dt.timedelta(seconds=61))
        (v,), = self._linhas("select visto from sessao")
        self.assertEqual(v, (T0 + dt.timedelta(seconds=61)).isoformat())

    def test_sair_e_desativar(self):
        tok = self._logar()
        usuarios.sair(tok)
        self.assertIsNone(usuarios.sessao(tok, agora=T0))
        tok = usuarios.entrar("ana@x.test", SENHA, agora=T0)
        usuarios.desativar("ana@x.test")
        self.assertIsNone(usuarios.sessao(tok, agora=T0))
        self.assertEqual(self._linhas("select * from sessao"), [])

    def test_trocar_senha_apaga_sessoes(self):
        tok = self._logar()
        usuarios.trocar_senha("ana@x.test", "outra-senha-456")
        self.assertIsNone(usuarios.sessao(tok, agora=T0))
        self.assertIsNone(usuarios.entrar("ana@x.test", SENHA))
        self.assertIsNotNone(usuarios.entrar("ana@x.test", "outra-senha-456"))

    def test_entrar_com_senha_trocada_no_meio_nao_cria_sessao(self):
        usuarios.criar("ana@x.test", SENHA, agora=T0)
        real = usuarios._conferir

        def conferir_e_troca(senha, guardado):
            ok = real(senha, guardado)
            usuarios.trocar_senha("ana@x.test", "outra-senha-456")  # a troca entra depois da conferência
            return ok

        with mock.patch.object(usuarios, "_conferir", conferir_e_troca):
            self.assertIsNone(usuarios.entrar("ana@x.test", SENHA, agora=T0))
        self.assertEqual(self._linhas("select * from sessao"), [])

    def test_trocar_senha_e_desativar_matam_convites_abertos(self):
        usuarios.criar("ana@x.test", SENHA)
        usuarios.criar("bia@x.test", SENHA)
        velho, outro = usuarios.convidar("ana@x.test", "leitura"), usuarios.convidar("bia@x.test", "leitura")
        usuarios.trocar_senha("ana@x.test", "outra-senha-456")
        self.assertIsNone(usuarios.ver_convite(velho))
        self.assertIsNone(usuarios.aceitar_convite(velho, "senha-do-atacante"))
        self.assertIsNotNone(usuarios.entrar("ana@x.test", "outra-senha-456"))
        self.assertEqual(usuarios.ver_convite(outro), "bia@x.test")  # convite de outro usuário segue valendo
        usuarios.desativar("bia@x.test")
        self.assertIsNone(usuarios.ver_convite(outro))

    def test_aceitar_convite_mata_os_outros_do_mesmo_email(self):
        usuarios.criar("ana@x.test", SENHA)
        a, b = usuarios.convidar("ana@x.test", "leitura"), usuarios.convidar("ana@x.test", "leitura")
        self.assertEqual(usuarios.aceitar_convite(a, "senha-nova-789"), "ana@x.test")
        self.assertIsNone(usuarios.aceitar_convite(b, "senha-do-atacante"))
        self.assertIsNotNone(usuarios.entrar("ana@x.test", "senha-nova-789"))

    def test_trocar_papel_listar_contar(self):
        usuarios.criar("ana@x.test", SENHA)
        usuarios.trocar_papel("ana@x.test", "dono")
        self.assertEqual(usuarios.contar(), 1)
        l = usuarios.listar()
        self.assertEqual(l[0]["papel"], "dono")
        self.assertEqual(set(l[0]), {"email", "papel", "criado", "ativo"})
        with self.assertRaises(ValueError):
            usuarios.trocar_papel("ana@x.test", "root")
        with self.assertRaises(ValueError):
            usuarios.trocar_papel("nao@x.test", "dono")
        with self.assertRaises(ValueError):
            usuarios.desativar("nao@x.test")
        with self.assertRaises(ValueError):
            usuarios.trocar_senha("nao@x.test", SENHA)

    def test_validacoes(self):
        with self.assertRaises(ValueError):
            usuarios.criar("ana@x.test", "curta")
        with self.assertRaises(ValueError):
            usuarios.criar("ana@x.test", SENHA, papel="chefe")
        usuarios.criar("ana@x.test", SENHA)
        with self.assertRaises(ValueError) as c:
            usuarios.criar("ANA@x.test", SENHA)
        self.assertIn("Já existe", str(c.exception))
        for ruim in ("", "a b@x.test", "@x.test", "x", "ana@@x.test", "A$B"):
            with self.assertRaises(ValueError, msg=ruim):
                usuarios.criar(ruim, SENHA)
        self.assertEqual(usuarios.criar("dono", SENHA, papel="dono"), "dono")


class MigracaoTest(unittest.TestCase):
    """PAINEL_SENHA antiga (env ou painel.env) vira o 1º dono; só com 0 usuários."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seg = os.path.join(self.tmp.name, "seg")
        os.mkdir(self.seg)
        p = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "KROHNOS_CAMINHOS_SEGREDOS": self.seg,
                                         "KROHNOS_CONFIG": os.path.join(self.tmp.name, "nao.toml"), "PAINEL_SENHA": ""})
        p.start()
        self.addCleanup(p.stop)
        from hub import config
        config.recarregar()
        self.addCleanup(config.recarregar)

    def test_env_sem_toml_cria_dono(self):
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": "antiga-curta"}), \
                mock.patch("hub.config.get", side_effect=lambda k: "" if k == "dono.email" else None):
            self.assertEqual(usuarios.migrar_senha_antiga(), "dono")
        self.assertEqual([(u["email"], u["papel"]) for u in usuarios.listar()], [("dono", "dono")])
        self.assertTrue(usuarios.entrar("dono", "antiga-curta"))  # menor que SENHA_MIN: entra assim mesmo

    def test_com_usuario_nao_faz_nada(self):
        usuarios.criar("ana@x.test", SENHA, papel="dono")
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": "outra-senha-longa"}):
            self.assertIsNone(usuarios.migrar_senha_antiga())
        self.assertEqual(usuarios.contar(), 1)

    def test_sem_senha_nao_faz_nada(self):
        self.assertIsNone(usuarios.migrar_senha_antiga())
        self.assertEqual(usuarios.contar(), 0)

    def test_painel_env_e_email_do_toml(self):
        with open(os.path.join(self.seg, "painel.env"), "w") as f:
            f.write("PAINEL_PORTA=1\nPAINEL_SENHA=do-arquivo-123\n")
        with mock.patch("hub.config.get", side_effect=lambda k: "Ana@X.test" if k == "dono.email" else None):
            self.assertEqual(usuarios.migrar_senha_antiga(), "ana@x.test")
        self.assertTrue(usuarios.entrar("ana@x.test", "do-arquivo-123"))

    def test_pasta_explicita(self):
        outra = os.path.join(self.tmp.name, "outra")
        usuarios.criar("ana@x.test", SENHA, papel="dono", pasta=outra)
        self.assertEqual(usuarios.contar(pasta=outra), 1)
        self.assertEqual(usuarios.contar(), 0)
        self.assertTrue(str(usuarios.banco(outra)).startswith(outra))

    def test_cli_senha(self):
        import io
        usuarios.criar("ana@x.test", SENHA)
        with mock.patch("sys.stdin", io.StringIO("nova-senha-longa\n")), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(usuarios.main(["senha", "ana@x.test"]), 0)
        self.assertTrue(usuarios.entrar("ana@x.test", "nova-senha-longa"))
        with mock.patch("sys.stdin", io.StringIO("curta\n")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(usuarios.main(["senha", "ana@x.test"]), 1)

    def test_cli_root_sem_banco_recusa(self):
        import io
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", io.StringIO(SENHA + "\n")):
            self.assertEqual(usuarios.main(["criar", "ana@x.test", "--papel", "dono"]), 2)
        self.assertIn("usuário do painel", err.getvalue())
        self.assertFalse((usuarios.hub.data() / "painel" / "usuarios.db").exists())
        usuarios.criar("ana@x.test", SENHA)  # banco já existe (criado pelo usuário do painel): root pode listar
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(usuarios.main(["listar"]), 0)

    def test_alvo_login_senha_no_campo_nao_vira_email(self):
        usuarios.criar("dono", SENHA, papel="dono")
        self.assertEqual(usuarios.alvo_login("Dono"), "dono")  # login curto que existe
        self.assertEqual(usuarios.alvo_login("S3nh@forte"), "(inválido)")
        self.assertEqual(usuarios.alvo_login("Ana@X.test"), "ana@x.test")
        self.assertEqual(usuarios.alvo_login("ninguem"), "(inválido)")


if __name__ == "__main__":
    unittest.main()
