import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hub import acoes, cockpit, painel
from tests import sessao_teste

AGORA = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


class CsrfTest(unittest.TestCase):
    """O CSRF saiu da senha (token do dia) para a sessão: o token é o da sessão do pedido em curso."""

    def tearDown(self):
        painel._pedido.sessao = None

    def test_token_da_sessao_vale_e_de_outra_nao(self):
        painel._pedido.sessao = {"email": "a@x.test", "papel": "dono", "csrf": "t" * 32}
        self.assertEqual(painel._csrf(), "t" * 32)
        self.assertTrue(painel.csrf_ok("t" * 32))
        self.assertFalse(painel.csrf_ok("u" * 32))
        self.assertFalse(painel.csrf_ok(""))
        self.assertFalse(painel.csrf_ok(None))
        painel._pedido.sessao = None
        self.assertEqual(painel._csrf(), "")
        self.assertFalse(painel.csrf_ok(""))


class ExecutarTest(unittest.TestCase):
    def test_criar_issue_vai_para_todo(self):
        chamadas = []
        with mock.patch.object(acoes.linear, "criar_issue", lambda *a: chamadas.append(a) or "KRN-50 https://l"):
            ok, msg = acoes.executar("criar-issue", {"projeto": "Cliente A", "titulo": "Arrumar rodapé",
                                                      "descricao": "quebra no celular"}, AGORA)
        self.assertTrue(ok)
        self.assertIn("KRN-50", msg)
        self.assertEqual(chamadas[0][3], "Todo")

    def test_pedido_escreve_faz_n_na_caixa_do_bot(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"CAIXA_COMANDOS": d}):
            ok, _ = acoes.executar("pedido", {"n": "33", "decisao": "faz"}, AGORA)
            [arq] = list(Path(d).glob("*.json"))
            self.assertEqual(json.loads(arq.read_text())["texto"], "faz 33")
        self.assertTrue(ok)

    def test_entrada_invalida_recusa(self):
        self.assertFalse(acoes.executar("pedido", {"n": "33; rm", "decisao": "faz"}, AGORA)[0])
        self.assertFalse(acoes.executar("pedido", {"n": "33", "decisao": "talvez"}, AGORA)[0])
        self.assertFalse(acoes.executar("criar-issue", {"projeto": "X", "titulo": ""}, AGORA)[0])
        self.assertFalse(acoes.executar("apagar-tudo", {}, AGORA)[0])


class PostTest(unittest.TestCase):
    """do_POST num servidor de verdade em porta aleatória; acoes.executar é simulado (nada vai ao Linear)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict("os.environ", {"HUB_DATA": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), painel.Painel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.cookie, self.csrf = sessao_teste.cookie_e_csrf()

    def post(self, acao="comentar", csrf=True, origin=True, auth=True, referer=None, **campos):
        if csrf:
            campos["csrf"] = self.csrf if csrf is True else csrf
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        if auth:
            h["Cookie"] = self.cookie
        if origin:
            origin = self.base if origin is True else origin
            h["Origin"] = origin
        if referer:
            h["Referer"] = referer
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
        c.request("POST", f"/acao/{acao}", urllib.parse.urlencode(campos), h)
        r = c.getresponse()
        r.read()
        return r.status, r.getheader("Location")

    def test_recusas(self):
        with mock.patch.object(acoes, "executar") as ex:
            self.assertEqual(self.post(auth=False), (303, "/entrar"))  # era 401 (Basic)
            self.assertEqual(self.post(csrf=False)[0], 403)  # era 303 "Página velha": agora o CSRF errado é recusado
            self.assertEqual(self.post(csrf=sessao_teste.cookie_e_csrf()[1])[0], 403)  # token de outra sessão
            self.assertEqual(self.post(origin="https://evil.example")[0], 403)
            self.assertEqual(self.post(origin=None)[0], 403)
            ex.assert_not_called()

    def get(self, caminho, auth=True):
        h = {"Cookie": self.cookie} if auth else {}
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
        c.request("GET", caminho, headers=h)
        r = c.getresponse()
        return r.status, r.read().decode()

    def test_get_pede_senha_e_erro_nao_mostra_caminho(self):
        for c in ("/", "/agentes", "/linear", "/pendencias", "/agora.fragmento", "/proposta/x"):
            self.assertEqual(self.get(c, auth=False)[0], 303, c)  # era 401 (Basic): agora vai ao /entrar
        with mock.patch.object(painel, "rota", side_effect=FileNotFoundError("/etc/nimbus/linear.env")):
            status, h = self.get("/")
        self.assertEqual(status, 500)
        self.assertNotIn("/etc/nimbus", h)
        self.assertNotIn(".env", h)

    def test_sucesso_volta_para_a_pagina_sem_query_antiga(self):
        with mock.patch.object(acoes, "executar", return_value=(True, "Enviado em KRN-1.")) as ex:
            status, loc = self.post(referer=self.base + "/linear?erro=velho", issue="KRN-1", texto="oi")
        self.assertEqual((status, loc), (303, "/linear?ok=Enviado%20em%20KRN-1."))
        self.assertEqual(ex.call_args[0][0], "comentar")

    def test_referer_de_fora_vai_para_a_home(self):
        with mock.patch.object(acoes, "executar", return_value=(True, "ok")):
            self.assertEqual(self.post(referer="https://evil.example/x")[1], "/?ok=ok")

    def test_referer_com_barras_duplas_nao_vira_location_externo(self):
        with mock.patch.object(acoes, "executar", return_value=(True, "ok")):
            self.assertEqual(self.post(referer=self.base + "//evil.example/x")[1], "/evil.example/x?ok=ok")

    def test_systemexit_e_excecao_viram_erro_e_o_servidor_segue(self):
        for falha in (SystemExit("projeto"), RuntimeError("boom")):
            with mock.patch.object(acoes, "executar", side_effect=falha):
                status, loc = self.post()
            self.assertEqual(status, 303)
            self.assertTrue(loc.startswith("/?erro="), loc)
        with mock.patch.object(acoes, "executar", return_value=(False, "Pedido inválido.")):
            self.assertIn("erro=", self.post()[1])


class TelasTest(unittest.TestCase):
    def setUp(self):
        painel._CK.clear()
        for nome, ret in (("linear_resumo", {}), ("pendencias", {}), ("agentes", {}), ("fila", [])):
            p = mock.patch.object(cockpit, nome, mock.Mock(return_value=ret))
            p.start()
            self.addCleanup(p.stop)

    def test_pedir_aparece_sem_cockpit_e_aviso_e_escapado(self):
        cockpit.linear_resumo.side_effect = OSError("fora")
        _, h = painel.rota("/linear?erro=%3Cb%3Ex%3C/b%3E")
        self.assertIn('action="/acao/criar-issue"', h)
        self.assertIn('name="csrf"', h)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", h)
        self.assertNotIn("<b>x</b>", h)
        self.assertIn("indisponível agora", h)

    def test_pendencias_mostra_botoes_de_decisao(self):
        cockpit.pendencias.return_value = {"pedidos": [{"id": "p1", "n": 7, "resumo": "r", "tipo": "x"}]}
        _, h = painel.rota("/pendencias")
        self.assertIn('action="/acao/pedido"', h)
        self.assertIn('name="n" value="7"', h)
        for rotulo, valor in (("Fazer", "faz"), ("Não fazer", "nao")):
            self.assertRegex(h, rf'name="decisao"[^>]*value="{valor}"[^>]*>{rotulo}<|value="{valor}"[^>]*name="decisao"[^>]*>{rotulo}<')


if __name__ == "__main__":
    unittest.main()
