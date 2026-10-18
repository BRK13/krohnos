"""hub/comum/email.py com transporte falso: 403 sem User-Agent, 429 com Retry-After, timeout, nunca lança."""
import json
import unittest

from hub.comum import email as E


def cloudflare(status=200, corpo=b'{"id": "abc-123"}', resp_cab=None, visto=None):
    """Transporte falso que se comporta como o Cloudflare na frente do Resend: sem User-Agent, 403."""
    def t(url, cab, corpo_req, timeout):
        if visto is not None:
            visto.append((url, cab, json.loads(corpo_req), timeout))
        if not cab.get("User-Agent"):
            return 403, {}, b"error code: 1010"
        return status, resp_cab or {}, corpo
    return t


class EnviarTest(unittest.TestCase):
    def test_ok_manda_user_agent_timeout_e_idempotencia(self):
        visto = []
        r = E.enviar("A <user3@example.com>", "user4@example.com", "oi", texto="corpo", chave="re_SEGREDO", idempotencia="nota-7",
                     transporte=cloudflare(visto=visto))
        self.assertEqual(r, {"ok": True, "id": "abc-123", "status": 200, "erro": None, "esperarS": None})
        url, cab, corpo, timeout = visto[0]
        self.assertEqual(url, "https://api.resend.com/emails")
        self.assertEqual((cab["User-Agent"], cab["Idempotency-Key"], cab["Authorization"]), (E.USER_AGENT, "nota-7", "Bearer re_SEGREDO"))
        self.assertEqual(timeout, E.TIMEOUT)
        self.assertEqual(corpo, {"from": "A <user3@example.com>", "to": ["user4@example.com"], "subject": "oi", "text": "corpo"})

    def test_sem_idempotencia_gera_uma_nova_por_chamada(self):
        visto = []
        for _ in range(2):
            E.enviar("user3@example.com", ["user4@example.com", "user5@example.com"], "s", html="<p>x</p>", chave="k", transporte=cloudflare(visto=visto))
        self.assertNotEqual(visto[0][1]["Idempotency-Key"], visto[1][1]["Idempotency-Key"])
        self.assertEqual(visto[0][2]["to"], ["user4@example.com", "user5@example.com"])

    def test_403_do_cloudflare_sem_user_agent(self):
        """O cliente de 16/10 (semanal.py) só funciona por causa do UA; se alguém o tirar, o teste pega."""
        antes, E.USER_AGENT = E.USER_AGENT, ""
        try:
            r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k", transporte=cloudflare())
        finally:
            E.USER_AGENT = antes
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 403)
        self.assertIn("1010", r["erro"])

    def test_429_devolve_esperarS_do_retry_after(self):
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k",
                     transporte=cloudflare(429, b'{"message":"rate"}', {"Retry-After": "7"}))
        self.assertEqual((r["ok"], r["status"], r["esperarS"]), (False, 429, 7))

    def test_429_retry_after_em_data_http_e_cabecalho_minusculo(self):
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k",
                     transporte=cloudflare(429, b"{}", {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        self.assertEqual(r["esperarS"], 0)  # data no passado: não espera
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k", transporte=cloudflare(429, b"{}", {"Retry-After": "lixo"}))
        self.assertIsNone(r["esperarS"])

    def test_timeout_nao_lanca(self):
        def t(*a):
            raise TimeoutError("lento")
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k", transporte=t)
        self.assertEqual((r["ok"], r["erro"]), (False, "timeout"))

    def test_erro_de_rede_nao_lanca_e_nao_vaza_a_chave(self):
        def t(*a):
            raise OSError("falha com re_SEGREDO no meio")
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="re_SEGREDO", transporte=t)
        self.assertFalse(r["ok"])
        self.assertNotIn("re_SEGREDO", r["erro"])

    def test_corpo_de_erro_cortado_e_sem_a_chave(self):
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="re_SEGREDO",
                     transporte=cloudflare(422, ("re_SEGREDO " + "x" * 1000).encode()))
        self.assertLessEqual(len(r["erro"]), E.CORPO_MAX + 6)
        self.assertNotIn("re_SEGREDO", r["erro"])

    def test_sem_chave(self):
        antes = E.ARQUIVOS_CHAVE
        E.ARQUIVOS_CHAVE = ()
        try:
            import os
            guardada = os.environ.pop("RESEND_API_KEY", None)
            try:
                r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", transporte=cloudflare())
            finally:
                if guardada is not None:
                    os.environ["RESEND_API_KEY"] = guardada
        finally:
            E.ARQUIVOS_CHAVE = antes
        self.assertEqual((r["ok"], r["erro"]), (False, "sem RESEND_API_KEY"))

    def test_resposta_2xx_sem_json_ainda_e_ok(self):
        r = E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k", transporte=cloudflare(200, b"nao-json"))
        self.assertEqual((r["ok"], r["id"]), (True, None))

    def test_provedor_numa_linha(self):
        visto = []
        antes = dict(E.PROVEDOR)
        E.PROVEDOR["url"] = "https://smtp.exemplo.com/v1/send"
        try:
            E.enviar("user3@example.com", "user4@example.com", "s", texto="t", chave="k", transporte=cloudflare(visto=visto))
        finally:
            E.PROVEDOR.update(antes)
        self.assertEqual(visto[0][0], "https://smtp.exemplo.com/v1/send")


if __name__ == "__main__":
    unittest.main()
