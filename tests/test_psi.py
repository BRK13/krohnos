import os
import unittest
from unittest import mock

from hub import psi


def _resp(lcp, nota=0.87):
    return {"lighthouseResult": {"categories": {"performance": {"score": nota}},
                                 "audits": {"largest-contentful-paint": {"numericValue": lcp},
                                            "total-blocking-time": {"numericValue": 120},
                                            "cumulative-layout-shift": {"numericValue": 0.02}}}}


class PsiTest(unittest.TestCase):
    def test_lab_extrai_metricas(self):
        self.assertEqual(psi.lab(_resp(2345.6)), {"nota": 87, "lcp_ms": 2345.6, "tbt_ms": 120, "cls": 0.02})

    def test_mediana_de_tres_rodadas(self):
        respostas = iter([_resp(3000), _resp(2000), _resp(2500)])
        r = psi.mediana("https://x.com/", pausa=0, buscar=lambda u: next(respostas))
        self.assertEqual(r["rodadas"], 3)
        self.assertEqual(r["lcp_ms"], 2500)
        self.assertEqual(r["url"], "https://x.com/")

    def test_resposta_sem_lighthouse_nao_conta_rodada(self):
        respostas = iter([{}, _resp(2000), {"error": "x"}])
        r = psi.mediana("https://x.com/", pausa=0, buscar=lambda u: next(respostas))
        self.assertEqual((r["rodadas"], r["lcp_ms"]), (1, 2000))
        self.assertIsNone(psi.mediana("https://x.com/", rodadas=2, pausa=0, buscar=lambda u: {}))

    def test_rodada_que_falha_fica_fora_da_mediana(self):
        respostas = iter([_resp(3000), OSError("429"), _resp(2000)])

        def buscar(u):
            v = next(respostas)
            if isinstance(v, Exception):
                raise v
            return v

        r = psi.mediana("https://x.com/", pausa=0, buscar=buscar)
        self.assertEqual(r["rodadas"], 2)
        self.assertEqual(r["lcp_ms"], 2500)

    def test_todas_falham_devolve_none(self):
        def buscar(u):
            raise OSError("timeout")

        self.assertIsNone(psi.mediana("https://x.com/", pausa=0, buscar=buscar))

    def test_chave_le_ambiente(self):
        with mock.patch.dict(os.environ, {"PAGESPEED_CHAVE": "abc"}):
            self.assertEqual(psi.chave(), "abc")


if __name__ == "__main__":
    unittest.main()
