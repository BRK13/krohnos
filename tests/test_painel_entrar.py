"""Login do painel (4.2): formulário, cookie de sessão, CSRF por sessão, /sair e migração da senha antiga.
Servidor real numa porta efêmera; HUB_DATA, segredos e toml em pasta temporária."""
import datetime as dt
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hub import config, limite, painel, usuarios
from tests import sessao_teste
from tests.test_painel import cockpit_neutro


class Sem(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class EntrarTest(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        (self.d / "seg").mkdir()
        env = mock.patch.dict(os.environ, {"HUB_DATA": str(self.d), "KRONOS_CAMINHOS_SEGREDOS": str(self.d / "seg"),
                                           "KRONOS_CONFIG": str(self.d / "nao.toml"), "PAINEL_SENHA": ""})
        env.start()
        self.addCleanup(env.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), painel.Painel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        limite.esquecer()
        self.addCleanup(limite.esquecer)

    def req(self, caminho, dados=None, cookie=None, origin=True, cab=None):
        h = dict(cab or {})
        if cookie:
            h["Cookie"] = cookie
        if dados is not None and origin:
            h["Origin"] = self.base
        r = urllib.request.Request(self.base + caminho, headers=h,
                                   data=urllib.parse.urlencode(dados).encode() if dados is not None else None)
        try:
            resp = urllib.request.build_opener(Sem).open(r)
        except urllib.error.HTTPError as ex:
            resp = ex
        return resp.status, resp.read().decode(), resp.headers

    def entrar(self, email, senha, volta="/"):
        return self.req("/entrar", {"email": email, "senha": senha, "volta": volta})

    # ---- GET sem sessão ----
    def test_sem_cookie_vai_para_entrar_com_volta(self):
        st, _, h = self.req("/pendencias?aba=x")
        self.assertEqual((st, h["Location"]), (303, "/entrar?volta=" + urllib.parse.quote("/pendencias?aba=x", safe="")))

    def test_cookie_invalido_e_lixo_303(self):
        for c in ("__Host-kronos_sessao=nao-existe", "__Host-kronos_sessao=", 'a="b; __Host-kronos_sessao', "lixo;;=;"):
            self.assertEqual(self.req("/pendencias", cookie=c)[0], 303, c)

    def test_sessao_vencida_303(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        velho = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
        token = usuarios.entrar("ana@exemplo.test", sessao_teste.SENHA, agora=velho)
        self.assertEqual(self.req("/pendencias", cookie=f"__Host-kronos_sessao={token}")[0], 303)

    def test_com_sessao_200_e_cabecalhos_de_moldura(self):
        st, corpo, h = self.req("/pendencias", cookie="outro=1; " + sessao_teste.cookie())
        self.assertEqual(st, 200)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertIn('href="/sair"', corpo)

    def test_salto_cross_site_sem_cookie(self):
        st, corpo, h = self.req("/pendencias?x=%3Cscript%3E", cab={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 200)
        self.assertIn('http-equiv="refresh" content="0"', corpo)
        self.assertNotIn("pendencias", corpo)
        self.assertNotIn("script", corpo.lower().replace("noscript", ""))
        self.assertEqual(h["X-Frame-Options"], "DENY")
        # com cookie (mesmo inválido) não há salto: vai ao /entrar
        self.assertEqual(self.req("/pendencias", cookie="__Host-kronos_sessao=x", cab={"Sec-Fetch-Site": "cross-site"})[0], 303)

    def test_publicas_sem_sessao(self):
        self.assertEqual(self.req("/saude")[:2], (200, "ok"))
        self.assertEqual(self.req("/favicon.svg")[0], 200)
        self.assertEqual(self.req("/r/" + "a" * 30)[0], 404)  # pública: não pede login (relatório não existe)

    # ---- /entrar ----
    def test_get_entrar_formulario(self):
        st, corpo, h = self.req("/entrar?volta=%22%3E%3Cb%3E")
        self.assertEqual(st, 200)
        self.assertIn('action="/entrar"', corpo)
        self.assertIn('name="senha" type="password"', corpo)
        self.assertNotIn('"><b>', corpo)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])

    def test_get_entrar_com_sessao_volta(self):
        st, _, h = self.req("/entrar?volta=/uso", cookie=sessao_teste.cookie())
        self.assertEqual((st, h["Location"]), (303, "/uso"))

    def test_login_certo_set_cookie(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        st, _, h = self.entrar("ANA@exemplo.test", sessao_teste.SENHA, "/uso?aba=contas")
        self.assertEqual((st, h["Location"]), (303, "/uso?aba=contas"))
        ck = h["Set-Cookie"]
        for attr in ("__Host-kronos_sessao=", "HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=604800"):
            self.assertIn(attr, ck)
        self.assertEqual(self.req("/pendencias", cookie=ck.split(";")[0])[0], 200)

    def test_login_errado_mesma_mensagem(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        a = self.entrar("ana@exemplo.test", "errada-errada-1")
        b = self.entrar("ninguem@exemplo.test", "errada-errada-1")
        for st, corpo, h in (a, b):
            self.assertEqual(st, 200)
            self.assertIn("E-mail ou senha incorretos.", corpo)
            self.assertIsNone(h["Set-Cookie"])

    def test_volta_externa_vira_raiz(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        for v in ("//mal.test", "/\\mal.test", "https://mal.test/", "x", "/a\r\nSet-Cookie: x=1"):
            st, _, h = self.entrar("ana@exemplo.test", sessao_teste.SENHA, v)
            self.assertEqual((st, h["Location"]), (303, "/"), v)

    def test_post_entrar_exige_origin(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        dados = {"email": "ana@exemplo.test", "senha": sessao_teste.SENHA}
        self.assertEqual(self.req("/entrar", dados, origin=False)[0], 403)
        self.assertEqual(self.req("/entrar", dados, origin=False, cab={"Origin": "https://mal.test"})[0], 403)

    # ---- ações e CSRF ----
    def acao(self, cookie, csrf=None, **extra):
        dados = {"issue": "KRN-1", "texto": "oi", **({"csrf": csrf} if csrf is not None else {})}
        return self.req("/acao/comentar", dados, cookie=cookie, **extra)

    def test_acao_sem_sessao_vai_para_entrar(self):
        st, _, h = self.acao(None, "x")
        self.assertEqual((st, h["Location"]), (303, "/entrar"))

    def test_acao_csrf(self):
        ck, csrf = sessao_teste.cookie_e_csrf()
        _, csrf_outra = sessao_teste.cookie_e_csrf()
        with mock.patch("hub.linear.comentar") as m:
            self.assertEqual(self.acao(ck)[0], 403)
            self.assertEqual(self.acao(ck, "")[0], 403)
            self.assertEqual(self.acao(ck, csrf_outra)[0], 403)
            self.assertEqual(self.acao(ck, csrf, origin=False)[0], 403)  # sem Origin
            m.assert_not_called()
            st, _, h = self.acao(ck, csrf, cab={"Referer": self.base + "/linear"})
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].startswith("/linear?ok="), h["Location"])
        m.assert_called_once()

    def test_form_da_pagina_leva_o_csrf_da_sessao(self):
        ck, csrf = sessao_teste.cookie_e_csrf()
        _, corpo, _ = self.req("/metas", cookie=ck)
        self.assertIn(f'name="csrf" value="{csrf}"', corpo)

    # ---- /sair ----
    def test_sair(self):
        ck, csrf = sessao_teste.cookie_e_csrf()
        st, corpo, h = self.req("/sair", cookie=ck)
        self.assertEqual(st, 200)
        self.assertIn('action="/sair"', corpo)
        self.assertIn(csrf, corpo)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(self.req("/sair", {"csrf": "errado"}, cookie=ck)[0], 403)
        st, _, h = self.req("/sair", {"csrf": csrf}, cookie=ck)
        self.assertEqual((st, h["Location"]), (303, "/entrar"))
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        self.assertEqual(self.req("/pendencias", cookie=ck)[0], 303)  # sessão invalidada no servidor

    # ---- migração ----
    def test_migracao_env_entra_como_dono(self):
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": "senha-antiga"}), \
                mock.patch("hub.config.get", side_effect=lambda k, g=config.get: "" if k == "dono.email" else g(k)):
            self.assertEqual(usuarios.migrar_senha_antiga(), "dono")
        st, _, h = self.entrar("dono", "senha-antiga")
        self.assertEqual(st, 303)
        self.assertIn("__Host-kronos_sessao=", h["Set-Cookie"])

    def test_migracao_painel_env(self):
        (self.d / "seg" / "painel.env").write_text("PAINEL_SENHA=do-arquivo-123\n")
        with mock.patch("hub.config.get", side_effect=lambda k, g=config.get: "" if k == "dono.email" else g(k)):
            self.assertEqual(usuarios.migrar_senha_antiga(), "dono")
        self.assertEqual(self.entrar("dono", "do-arquivo-123")[0], 303)

    def test_migracao_com_usuario_nao_faz_nada(self):
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        with mock.patch.dict(os.environ, {"PAINEL_SENHA": "senha-antiga"}):
            self.assertIsNone(usuarios.migrar_senha_antiga())
        self.assertEqual(usuarios.contar(), 1)

    def test_painel_migrar_sucesso_avisa_login_sem_senha(self):
        import io
        err = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "KRONOS_DONO_EMAIL"} | {"PAINEL_SENHA": "senha-antiga-xyz"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr", err):
            self.assertEqual(painel.migrar(), "dono")  # sem kronos.toml nem dono.email: login "dono"
        self.assertEqual(usuarios.contar(), 1)
        self.assertIn("entre como dono", err.getvalue())
        self.assertNotIn("senha-antiga-xyz", err.getvalue())

    def test_painel_migrar_erro_nao_levanta_nem_mostra_senha(self):
        import io
        for ex in (usuarios.sqlite3.OperationalError("disk I/O error senha-antiga-xyz"), OSError("senha-antiga-xyz")):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"PAINEL_SENHA": "senha-antiga-xyz"}), mock.patch("sys.stderr", err), \
                    mock.patch.object(usuarios, "migrar_senha_antiga", side_effect=ex):
                self.assertIsNone(painel.migrar())
            self.assertIn(type(ex).__name__, err.getvalue())
            self.assertNotIn("senha-antiga-xyz", err.getvalue())

    # ---- banco fora do ar no /entrar ----
    def test_entrar_com_banco_quebrado_500_curto(self):
        import io
        err = mock.patch("sys.stderr", io.StringIO())  # o painel registra só o tipo do erro
        err.start()
        self.addCleanup(err.stop)
        usuarios.criar("ana@exemplo.test", sessao_teste.SENHA, papel="dono")
        for alvo, ex in ((usuarios, "entrar"), (usuarios, "alvo_login"), (limite, "tentar")):
            with self.subTest(ex), mock.patch.object(alvo, ex, side_effect=usuarios.sqlite3.OperationalError("database is locked /x/usuarios.db")):
                st, corpo, _ = self.entrar("ana@exemplo.test", sessao_teste.SENHA)
            self.assertEqual(st, 500)
            self.assertIn("Não consegui entrar agora; tente de novo.", corpo)
            self.assertNotIn("usuarios.db", corpo)
            self.assertNotIn("locked", corpo)
        with mock.patch.object(usuarios, "entrar", side_effect=OSError("disco cheio")):
            self.assertEqual(self.entrar("ana@exemplo.test", sessao_teste.SENHA)[0], 500)
        self.assertEqual(self.entrar("ana@exemplo.test", sessao_teste.SENHA)[0], 303)  # o painel segue no ar


if __name__ == "__main__":
    unittest.main()
