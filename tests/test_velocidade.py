import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import armazem, velocidade as V

SITE = {"id": "fsite06", "nome": "fsite06", "dominio": "fsite06-com-br.example", "repo": {"github": "acme/cliente-a-v2"}, "ids": {"ga4": "1"}}


def _semear(d, lcp=2000, nota=90, dias=(7, 14, 21, 28), path="/"):
    hoje = dt.date.fromisoformat("2026-10-15")
    fs = []
    for n in dias:
        dia = (hoje - dt.timedelta(days=n)).isoformat()
        fs += [{"dia": dia, "negocio": "fsite06", "fonte": "velocidade", "metrica": "lcp_ms", "dim": path, "valor": lcp},
               {"dia": dia, "negocio": "fsite06", "fonte": "velocidade", "metrica": "nota", "dim": path, "valor": nota}]
    armazem.gravar(fs, d)


class VelocidadeTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def reg(self, lcp, nota, path="/"):
        return V.regressoes("fsite06", "2026-10-15", {path: {"lcp_ms": lcp, "nota": nota}}, self.d)

    def test_regressao_por_lcp(self):
        _semear(self.d)
        self.assertEqual(self.reg(2500, 90)[0]["base_lcp"], 2000)

    def test_regressao_por_nota(self):
        _semear(self.d)
        self.assertEqual(len(self.reg(2000, 78)), 1)

    def test_sem_regressao_com_15_por_cento(self):
        _semear(self.d)
        self.assertEqual(self.reg(2300, 85), [])

    def test_pagina_sem_quatro_semanas_nao_acusa(self):
        _semear(self.d, dias=(7, 14, 21), path="/nova")
        self.assertEqual(self.reg(9000, 10, "/nova"), [])

    def test_abrir_issue_nao_repete_em_28_dias(self):
        criar = mock.Mock(return_value="KRN-9 http://x")
        regs = [{"pagina": "/", "lcp_ms": 3000, "base_lcp": 2000, "nota": 70, "base_nota": 90}]
        with mock.patch("hub.linear.projeto_do_repo", return_value="fsite06"):
            self.assertEqual(V.abrir_issue(SITE, regs, self.d, criar), "KRN-9")
            self.assertIsNone(V.abrir_issue(SITE, regs, self.d, criar))
        self.assertEqual(criar.call_count, 1)
        args = criar.call_args.args
        self.assertEqual(args[3:], ("Todo", ["velocidade"]))
        self.assertIn("Auditoria em 4 frentes", args[2])

    def test_paginas(self):
        top = lambda s: ["/", "/blog/a?x=1", "/b", "/c", "/d"]  # noqa: E731
        self.assertEqual(V.paginas(SITE, top), ["/", "/blog/a", "/b", "/c"])

    def test_coletar_grava_fatos_e_ultima(self):
        medir = lambda url: {"nota": 80, "lcp_ms": 2100, "tbt_ms": 10, "cls": 0.1}  # noqa: E731
        with mock.patch("hub.sites.ativos", return_value=[SITE]), mock.patch("hub.psi.chave", return_value=None):
            r = V.coletar("2026-10-15", self.d, medir, lambda s: ["/"])
        self.assertEqual(armazem.serie("fsite06", "velocidade", "lcp_ms", "2026-10-15", "2026-10-15", "", self.d)[0][1], 2100)
        self.assertEqual(armazem.serie("fsite06", "velocidade", "lcp_ms", "2026-10-15", "2026-10-15", "/", self.d)[0][1], 2100)
        self.assertEqual(json.loads((Path(self.d) / "velocidade" / "ultima.json").read_text())["dia"], "2026-10-15")
        self.assertIn("fsite06", r["sites"])

    def test_issues_de_dois_sites_em_paralelo(self):
        criar = lambda *a: "KRN-" + str(len(a[1])) + " u"  # noqa: E731
        regs = [{"pagina": "/", "lcp_ms": 3000, "base_lcp": 2000, "nota": 70, "base_nota": 90}]
        sites = [{**SITE, "id": f"s{i}"} for i in range(6)]
        from concurrent.futures import ThreadPoolExecutor
        with mock.patch("hub.linear.projeto_do_repo", return_value="P"), ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda s: V.abrir_issue(s, regs, self.d, criar), sites))
        self.assertEqual(set(json.loads((Path(self.d) / "velocidade" / "issues.json").read_text())), {s["id"] for s in sites})

    def test_systemexit_na_issue_nao_aborta(self):
        _semear(self.d)
        medir = lambda url: {"nota": 90, "lcp_ms": 5000, "tbt_ms": 1, "cls": 0}  # noqa: E731
        def abrir(*a):
            raise SystemExit("sem projeto")
        with mock.patch("hub.sites.ativos", return_value=[SITE]), mock.patch("hub.psi.chave", return_value=None):
            r = V.coletar("2026-10-15", self.d, medir, lambda s: ["/"], abrir)
        self.assertEqual(r["sites"]["fsite06"]["issue"], None)
        self.assertTrue((Path(self.d) / "velocidade" / "ultima.json").exists())

    def test_ga4_falha_mede_so_a_home(self):
        def top(s):
            raise SystemExit("sem ga4")
        self.assertEqual(V.paginas(SITE, top), ["/"])
        self.assertEqual(V.paginas({**SITE, "ids": {}}), ["/"])

    def test_saude(self):
        self.assertEqual(V.saude("fsite06", self.d), (None, None))
        _semear(self.d, lcp=2600, dias=(1,))
        self.assertEqual(V.saude("fsite06", self.d), ("alerta", 2600))
        _semear(self.d, lcp=4100, dias=(0,))
        self.assertEqual(V.saude("fsite06", self.d)[0], "ruim")

    def test_bloco_resumo_velho_vazio(self):
        v = Path(self.d) / "velocidade"
        v.mkdir()
        velho = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        (v / "ultima.json").write_text(json.dumps({"dia": velho, "sites": {"fsite06": {"paginas": {}, "regressoes": []}}}))
        self.assertEqual(V.bloco_resumo(self.d), [])


if __name__ == "__main__":
    unittest.main()
