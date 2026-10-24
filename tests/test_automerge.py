"""merge mecânico. GitHub (`chamar`) e Linear (`consulta`, falar, reabrir) são falsos."""
import json
import tempfile
import unittest
import unittest.mock

from hub import automerge, config, conta, esperas, memorias

SHA = "392907284aeecef990cf13655d6441a2a2f719a8"
ORG = config.repo("x").split("/")[0]  # a org da config (nos testes, a fictícia; na VPS, a do legado)
PR = f"https://github.com/{ORG}/clienteh/pull/524"
LIMPA = {"body": f"**Revisão do Codex** (commit `{SHA[:8]}`)\n\nSem achados sérios.", "created_at": "2026-10-12T12:30:00Z"}


def issue(labels=("merge",), ident="KRN-6"):
    return {"id": "uuid-6", "identifier": ident, "title": "t", "createdAt": "2026-10-12T10:00:00Z", "description": "",
            "labels": {"nodes": [{"name": n} for n in labels]}, "attachments": {"nodes": [{"url": PR}]}}


class GitHubFalso:
    def __init__(self, ci="success", comentarios=(LIMPA,), recentes=("local-ci",), pr=None, erro=False, checks=()):
        self.checks, self.chamadas = list(checks), []
        self.ci, self.comentarios, self.recentes, self.erro = ci, list(comentarios), recentes, erro
        self.pr = pr or {"state": "open", "merged": False, "draft": False, "head": {"sha": SHA}}

    def __call__(self, caminho):
        self.chamadas.append(caminho)
        if self.erro:
            raise OSError("rede")
        if caminho.endswith(f"/commits/{SHA}/check-runs"):
            return {"check_runs": self.checks}
        if caminho.endswith("/pulls/524"):
            return self.pr
        if "/pulls?state=closed" in caminho:
            return [{"number": 500, "head": {"sha": "a" * 40}, "merged_at": "2026-10-11T10:00:00Z"}]
        if caminho.endswith(f"/commits/{SHA}/status"):
            return {"statuses": [{"context": "local-ci", "state": self.ci}] if self.ci else []}
        if caminho.endswith("/commits/" + "a" * 40 + "/status"):
            return {"statuses": [{"context": c, "state": "success"} for c in self.recentes]}
        if "/issues/524/comments" in caminho:
            return self.comentarios
        raise AssertionError(caminho)


class AutomergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados, self.falas, self.logs = tmp.name, [], []
        self.diff = "+++ b/hub/x.py\n@@ -0,0 +1 @@\n+x = 1\n"  # o diff do PR que o gh devolveria (o portão das decisões do dono lê este diff)
        p = unittest.mock.patch.object(memorias, "diff_do_pr", lambda repo, n: self.diff)
        p.start()
        self.addCleanup(p.stop)

    def rodar(self, gh=None, issues=None, ocupadas=()):
        def consulta(q, **v):
            return {"issues": {"nodes": issues if issues is not None else [issue()], "pageInfo": {"hasNextPage": False}}}
        return automerge.vigiar(consulta=consulta, chamar=gh or GitHubFalso(), base_dados=self.dados, ocupadas=ocupadas,
                                log=self.logs.append, falar_fn=lambda i, t, repasse=False: self.falas.append((i, t, repasse)),
                                reabrir_fn=lambda ident, fala: f"tarefa-{ident}")

    def estado(self):
        return json.loads((conta.pasta(self.dados) / "automerge.json").read_text())

    def test_caminho_feliz(self):
        self.assertEqual(self.rodar(), ["tarefa-KRN-6"])
        (i, fala, repasse), = self.falas
        self.assertEqual((i, repasse), ("uuid-6", True))
        self.assertIn(f"PR {PR} (commit {SHA[:8]}): local-ci verde", fala)
        self.assertIn(f"gh pr merge 524 -R {ORG}/clienteh --squash --match-head-commit {SHA}", fala)
        self.assertIn(f"voltar-quando KRN-6 --pr {PR} --no-ar", fala)
        self.assertEqual(self.estado(), {f"KRN-6 {ORG}/clienteh#524": SHA})

    # test_pr_que_contraria_decisao_do_dono_nao_vai e test_renomeacao_pura_para_workflow_barra moram em
    # tests/test_automerge_decisoes.py (leem as decisões do dono, hub/memorias_decisoes.json, fora do snapshot público)

    def test_diff_ilegivel_adia_em_vez_de_liberar(self):
        # revisão Codex: gh com erro/timeout devolvia "" e o portão liberava sem examinar nada
        for ruim in (None, ""):
            self.diff = ruim
            self.logs.clear()
            self.assertEqual(self.rodar(), [])
            self.assertEqual(self.falas, [])
            self.assertTrue(any("não consegui ler o diff" in l for l in self.logs))

    def nada(self, **k):
        self.assertEqual(self.rodar(**k), [])
        self.assertEqual(self.falas, [])

    def test_ci_vermelho(self):
        self.nada(gh=GitHubFalso(ci="failure"))

    def test_ci_pendente(self):
        self.nada(gh=GitHubFalso(ci=None))

    def test_revisao_com_p1(self):
        self.nada(gh=GitHubFalso(comentarios=[{**LIMPA, "body": LIMPA["body"] + "\n[P1] bug"}]))

    def test_revisao_de_commit_antigo(self):
        self.nada(gh=GitHubFalso(comentarios=[{**LIMPA, "body": LIMPA["body"].replace(SHA[:8], "deadbeef")}]))

    def test_sem_revisao(self):
        self.nada(gh=GitHubFalso(comentarios=[]))

    def test_sem_label_merge(self):
        self.nada(issues=[issue(labels=("bug",))])

    def test_pr_draft_ou_fechado(self):
        self.nada(gh=GitHubFalso(pr={"state": "open", "draft": True, "head": {"sha": SHA}}))
        self.nada(gh=GitHubFalso(pr={"state": "closed", "merged": True, "head": {"sha": SHA}}))

    def test_issue_ocupada(self):
        self.nada(ocupadas={"linear-krn-6"})

    def test_mesma_ponta_nao_manda_duas_vezes(self):
        self.assertEqual(len(self.rodar()), 1)
        self.assertEqual(self.rodar(), [])
        self.assertEqual(len(self.falas), 1)

    def test_issue_com_espera_gravada(self):
        esperas.gravar("KRN-6", PR, chamar=GitHubFalso(), base_dados=self.dados)
        self.nada()

    @unittest.skipUnless(automerge.vigia, "portão de evals é do privado: fora do snapshot público não existe")
    def test_evals_exigido_no_sha_da_ponta(self):
        """PR que mexe em prompt vigiado só vai com o status `evals` success no commit da ponta."""
        self.diff = "--- a/prompts/peca/v1.md\n+++ b/prompts/peca/v1.md\n@@ -1 +1 @@\n-a\n+b\n"
        antigo = "b" * 40

        class ComEvals(GitHubFalso):
            def __init__(self, evals, **kw):
                super().__init__(**kw)
                self.evals = evals

            def __call__(self, caminho):
                if caminho.endswith(f"/commits/{SHA}/status"):
                    base = super().__call__(caminho)["statuses"]
                    return {"statuses": base + [{"context": "evals", "state": self.evals.get(SHA, "pending")}]}
                if caminho.endswith(f"/commits/{antigo}/status"):
                    return {"statuses": [{"context": "evals", "state": self.evals.get(antigo, "pending")}]}
                return super().__call__(caminho)

        for estado in ({antigo: "success"}, {SHA: "failure"}, {SHA: "error"}, {}):
            self.nada(gh=ComEvals(estado))
        self.assertTrue(any("(evals)" in l and "adiado" in l for l in self.logs))
        self.assertEqual(self.rodar(gh=ComEvals({antigo: "failure", SHA: "success"})), ["tarefa-KRN-6"])

    def test_diff_sem_caminho_vigiado_nao_exige_evals(self):
        self.assertEqual(self.rodar(), ["tarefa-KRN-6"])  # o diff padrão toca só hub/x.py

    def test_repo_sem_local_ci_com_revisao_limpa_manda(self):
        self.assertEqual(self.rodar(gh=GitHubFalso(ci=None, recentes=())), ["tarefa-KRN-6"])
        self.assertIn("repo sem CI", self.falas[0][1])

    def test_check_run_vermelho_ou_pendente_barra(self):
        self.nada(gh=GitHubFalso(ci=None, recentes=(), checks=[{"status": "completed", "conclusion": "failure", "name": "tsc"}]))
        self.nada(gh=GitHubFalso(ci=None, recentes=(), checks=[{"status": "in_progress", "conclusion": None, "name": "tsc"}]))

    def test_check_runs_verdes_sem_local_ci_manda(self):
        gh = GitHubFalso(ci=None, recentes=(), checks=[{"status": "completed", "conclusion": "success", "name": "t"}])
        self.assertEqual(self.rodar(gh=gh), ["tarefa-KRN-6"])
        self.assertIn("checks do GitHub verdes", self.falas[0][1])

    def test_revisao_parcial_barra(self):
        self.nada(gh=GitHubFalso(comentarios=[{**LIMPA, "body": "— diff de 300 kB cortado em 150 kB: revisão parcial\n" + LIMPA["body"]}]))

    def test_recusada_pelo_disjuntor_nao_grava_e_volta_a_tentar(self):
        def consulta(q, **v):
            return {"issues": {"nodes": [issue()], "pageInfo": {"hasNextPage": False}}}

        def rodar(aceita):
            return automerge.vigiar(consulta=consulta, chamar=GitHubFalso(), base_dados=self.dados, log=self.logs.append,
                                    falar_fn=lambda i, t, repasse=False: self.falas.append(t),
                                    reabrir_fn=lambda ident, fala: f"tarefa-{ident}", aceitar=lambda t: aceita)
        self.assertEqual(rodar(False), [])
        self.assertEqual(self.falas, [])
        self.assertFalse((conta.pasta(self.dados) / "automerge.json").exists())
        self.assertEqual(rodar(True), ["tarefa-KRN-6"])  # liberado: manda no tick seguinte
        self.assertEqual(len(self.falas), 1)

    def test_reabrir_falha_nao_comenta(self):
        def reabrir(ident, fala):
            raise RuntimeError("x")
        automerge.vigiar(consulta=lambda q, **v: {"issues": {"nodes": [issue()], "pageInfo": {"hasNextPage": False}}},
                         chamar=GitHubFalso(), base_dados=self.dados, log=self.logs.append,
                         falar_fn=lambda *a, **k: self.falas.append(a), reabrir_fn=reabrir)
        self.assertEqual(self.falas, [])
        self.assertFalse((conta.pasta(self.dados) / "automerge.json").exists())

    def test_dois_prs_na_mesma_issue_um_envio_cada(self):
        pr2 = f"https://github.com/{ORG}/clienteh/pull/525"
        i = issue()
        i["attachments"]["nodes"].append({"url": pr2})
        gh = GitHubFalso()
        orig = gh.__call__
        def chamar(c):
            return orig(c.replace("525", "524"))
        for _ in range(2):
            self.rodar(gh=chamar, issues=[i])
        self.assertEqual(len(self.falas), 2)  # uma Tarefa por issue por tick: um PR em cada tick
        self.rodar(gh=chamar, issues=[i])
        self.assertEqual(len(self.falas), 2)
        self.assertEqual(len(self.estado()), 2)

    def test_ponta_ja_mandada_so_chama_o_pr(self):
        self.rodar()
        gh = GitHubFalso()
        self.assertEqual(self.rodar(gh=gh), [])
        self.assertEqual(gh.chamadas, [f"repos/{ORG}/clienteh/pulls/524"])

    def test_erro_de_rede_loga_e_segue(self):
        self.nada(gh=GitHubFalso(erro=True))
        self.assertTrue(any("falhou" in l for l in self.logs))


if __name__ == "__main__":
    unittest.main()
