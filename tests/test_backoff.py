"""Backoff nos limites de API: Linear 2.500/h e GitHub estouraram às 01:44 UTC de 16/10."""
import io
import subprocess
import unittest
import urllib.error
from unittest import mock

from hub import backoff, esperas, linear


def http(code, corpo=b"", cab=None):
    return urllib.error.HTTPError("https://api.linear.app/graphql", code, "x", cab or {}, io.BytesIO(corpo))


class Nucleo(unittest.TestCase):
    def test_repete_e_espera_em_dobro(self):
        falhas, esperas_ = [OSError("a"), OSError("b")], []

        def chamar():
            if falhas:
                raise falhas.pop(0)
            return "ok"
        r = backoff.com_backoff(chamar, lambda e: 0.0, dormir=esperas_.append)
        self.assertEqual((r, esperas_), ("ok", [1.0, 2.0]))

    def test_nao_e_limite_sobe_na_hora(self):
        dormiu = []
        with self.assertRaises(ValueError):
            backoff.com_backoff(mock.Mock(side_effect=ValueError), lambda e: None, dormir=dormiu.append)
        self.assertEqual(dormiu, [])

    def test_espera_maior_que_o_teto_desiste(self):
        """Cota por hora esgotada: dormir 40 min dentro de uma tarefa não ajuda; sobe para a próxima volta."""
        dormiu = []
        with self.assertRaises(OSError):
            backoff.com_backoff(mock.Mock(side_effect=OSError), lambda e: 3000.0, dormir=dormiu.append)
        self.assertEqual(dormiu, [])

    def test_acaba_depois_das_tentativas(self):
        chamar = mock.Mock(side_effect=OSError)
        with self.assertRaises(OSError):
            backoff.com_backoff(chamar, lambda e: 0.0, tentativas=3, dormir=lambda s: None)
        self.assertEqual(chamar.call_count, 4)


class Linear(unittest.TestCase):
    def test_espera_do_linear(self):
        self.assertEqual(backoff.espera_linear(http(429, cab={"Retry-After": "7"}), b""), 7.0)
        self.assertEqual(backoff.espera_linear(http(503), b""), 0.0)
        self.assertEqual(backoff.espera_linear(http(400), b'{"errors":[{"extensions":{"code":"RATELIMITED"}}]}'), 0.0)
        self.assertIsNone(backoff.espera_linear(http(400), b'{"errors":[{"message":"Argument invalido"}]}'))
        self.assertIsNone(backoff.espera_linear(http(401), b""))
        # mutação: 502 pode ter gravado antes; só o limite repete
        self.assertIsNone(backoff.espera_linear(http(502), b"", mutacao=True))
        self.assertEqual(backoff.espera_linear(http(429), b"", mutacao=True), 0.0)

    def test_gql_troca_de_chave_no_429(self):  # limite da hora com a chave do dono: passa para a reserva, sem esperar
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"data": {"x": 1}}'
        with mock.patch.object(linear, "_fontes", return_value=[("app2", lambda r: "k", False), ("fulano", lambda r: "k2", False)]), \
                mock.patch.object(linear.urllib.request, "urlopen", side_effect=[http(429), ok]) as u, \
                mock.patch.object(backoff.time, "sleep") as dorme:
            self.assertEqual(linear.gql("{ x }"), {"x": 1})
        self.assertEqual((u.call_count, dorme.call_count), (2, 0))

    def test_gql_erro_de_query_nao_repete(self):
        with mock.patch.object(linear, "_fontes", return_value=[("app2", lambda r: "k", False)]), \
                mock.patch.object(linear.urllib.request, "urlopen", side_effect=[http(400, b'{"errors":[]}')]) as u:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                linear.gql("{ x }")
        self.assertEqual(u.call_count, 1)


class GitHub(unittest.TestCase):
    def erro(self, msg):
        return subprocess.CalledProcessError(1, ["gh"], stderr=msg)

    def test_espera_do_github(self):
        self.assertEqual(backoff.espera_github(self.erro("gh: You have exceeded a secondary rate limit (HTTP 403)")), 0.0)
        self.assertEqual(backoff.espera_github(self.erro("gh: Bad Gateway (HTTP 502)")), 0.0)
        self.assertEqual(backoff.espera_github(self.erro("HTTP 429: Too Many Requests")), 0.0)
        # cota por hora esgotada: tentar de novo só gasta chamada
        self.assertIsNone(backoff.espera_github(self.erro("gh: API rate limit exceeded for user ID 1. (HTTP 403)")))
        self.assertIsNone(backoff.espera_github(self.erro("gh: Not Found (HTTP 404)")))

    def test_gh_api_tenta_de_novo(self):
        from hub import github_api
        with mock.patch.object(github_api, "get", side_effect=[github_api.ErroHTTP(502), {"a": 1}]) as g, \
                mock.patch.object(backoff.time, "sleep"):
            self.assertEqual(esperas.gh_api("repos/x/y"), {"a": 1})
        self.assertEqual(g.call_count, 2)


if __name__ == "__main__":
    unittest.main()
