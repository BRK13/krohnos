import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import claude, revisar_codex

SHA = "abcdef1234567890"


_ENV = None


def setUpModule():
    """Sem `gh auth token` real e sem memo de credencial vazando entre testes."""
    global _ENV
    from unittest import mock
    from hub import github_api
    _ENV = mock.patch.dict("os.environ", {"GH_TOKEN": "token-de-teste"})
    _ENV.start()
    github_api._AUTH.clear()


def tearDownModule():
    from hub import github_api
    _ENV.stop()
    github_api._AUTH.clear()


def pr(n, head, sha=SHA, draft=False):
    return {"number": n, "headRefOid": sha, "baseRefName": "main", "headRefName": head, "isDraft": draft}


class RevisarCodexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.comentarios = []
        self.head = SHA
        self.prs = [pr(1, "codex/x"), pr(2, "krn-1-y"), pr(3, "codex/z", draft=True)]
        p = mock.patch.object(revisar_codex, "REPOS", ["infra"])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(revisar_codex, "_gh", self.gh)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(revisar_codex, "_abertos", lambda repo: self.prs)
        p.start()
        self.addCleanup(p.stop)

    def gh(self, *a):
        if a[1] == "list":
            return json.dumps(self.prs)
        if a[1] == "diff":
            return "+x\n"
        if a[1] == "view":
            return json.dumps({"headRefOid": self.head})
        self.comentarios.append((a[2], a[-1]))
        return "https://github.com/acme/infra/pull/" + a[2]

    def feitos(self):
        arq = Path(self.dados) / "claude" / "revisoes-claude.txt"
        return arq.read_text().split() if arq.exists() else []

    def test_so_codex_e_uma_vez_por_sha(self):
        with mock.patch.object(claude, "perguntar", return_value="Bug em a.py:1") as q:
            revisar_codex.main(self.dados)
            revisar_codex.main(self.dados)
        self.assertEqual(q.call_count, 1)
        self.assertEqual([c[0] for c in self.comentarios], ["1"])
        self.assertEqual(self.feitos(), [f"infra#1@{SHA}"])
        self.assertTrue(self.comentarios[0][1].startswith("**Revisão do Claude** (commit `abcdef12`)"))
        linha = json.loads((Path(self.dados) / "claude" / "revisoes-claude.jsonl").read_text())
        self.assertTrue(linha["achados"])

    def test_falha_do_claude_nao_grava_feito(self):
        with mock.patch.object(claude, "perguntar", side_effect=claude.ContaPausada("x")):
            revisar_codex.main(self.dados)
        self.assertEqual(self.feitos(), [])
        self.assertEqual(self.comentarios, [])

    def test_push_durante_a_revisao_nao_comenta(self):
        self.head = "fedcba0987654321"
        with mock.patch.object(claude, "perguntar", return_value="Bug em a.py:1"):
            revisar_codex.main(self.dados)
        self.assertEqual(self.comentarios, [])
        self.assertEqual(self.feitos(), [])

    def test_sem_achados(self):
        with mock.patch.object(claude, "perguntar", return_value="Sem achados sérios."):
            revisar_codex.main(self.dados)
        linha = json.loads((Path(self.dados) / "claude" / "revisoes-claude.jsonl").read_text())
        self.assertFalse(linha["achados"])


if __name__ == "__main__":
    unittest.main()


class AbertosTest(unittest.TestCase):
    def test_mapeia_rest_para_o_formato_antigo_e_mede(self):
        rest = [{"number": 7, "draft": True, "head": {"sha": "s1", "ref": "codex/a"}, "base": {"ref": "main"}}]
        with mock.patch.object(revisar_codex.github_api, "get", return_value=rest) as g:
            r = revisar_codex._abertos("infra")
        self.assertEqual(r, [{"number": 7, "headRefOid": "s1", "baseRefName": "main", "headRefName": "codex/a", "isDraft": True}])
        self.assertEqual(g.call_args.kwargs["consumidor"], "revisar_codex")

    def test_pagina_ate_lote_incompleto(self):
        def pr(n):
            return {"number": n, "head": {"sha": f"s{n}", "ref": "codex/x"}, "base": {"ref": "main"}}
        paginas = [[pr(n) for n in range(100)], [pr(n) for n in range(100, 130)]]
        with mock.patch.object(revisar_codex.github_api, "get", side_effect=paginas) as g:
            r = revisar_codex._abertos("infra")
        self.assertEqual(len(r), 130)
        self.assertEqual(g.call_count, 2)
        self.assertIn("page=2", g.call_args.args[0])
