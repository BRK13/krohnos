"""a tarefa grava a espera do CI/revisão, termina e volta sozinha. Nada aqui fala com o GitHub nem com o
Linear: `chamar` é um GitHub falso e `linear.reabrir`/`gql` são trocados."""
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import agenda, conta, esperas, linear

UTC = dt.timezone.utc
ENCERRAR = linear.encerrar_sem_ia  # o de verdade, antes do mock do SemIaTest
EM_LOOP = agenda.em_loop  # o de verdade, antes do mock do setUp
T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
PR = "https://github.com/acme/clienteh/pull/524"
SHA = "392907284aeecef990cf13655d6441a2a2f719a8"
REVISAO = {"body": f"**Revisão do Codex** (commit `{SHA[:8]}`)\n\nSem achados sérios.", "created_at": "2026-10-12T12:30:00Z",
           "html_url": "https://github.com/acme/clienteh/pull/524#issuecomment-1"}


_ENV = None
_GH = None


def setUpModule():
    """Sem `gh auth token` real e sem memo de credencial vazando entre testes."""
    global _ENV
    from unittest import mock
    from hub import github_api
    _ENV = mock.patch.dict("os.environ", {"GH_TOKEN": "token-de-teste"})
    _ENV.start()
    github_api._AUTH.clear()
    global _GH
    _GH = mock.patch("hub.gh.executar", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", ""))  # sem `gh pr diff` de verdade
    _GH.start()


def tearDownModule():
    from hub import github_api
    _GH.stop()
    _ENV.stop()
    github_api._AUTH.clear()


class GitHubFalso:
    def __init__(self, ci=None, comentarios=(), pr=None, recentes=None, sem_merge=()):
        self.ci, self.comentarios, self.chamadas = ci, list(comentarios), []
        self.pr = pr or {"state": "open", "merged": False, "head": {"sha": SHA}}
        self.recentes = recentes  # {sha de PR recente: [contextos]}; None = GitHub não responde a lista
        self.sem_merge = set(sem_merge)  # shas de PRs recentes fechados sem merge

    def __call__(self, caminho):
        self.chamadas.append(caminho)
        if caminho.endswith("/pulls/524"):
            return self.pr
        if "/pulls?state=closed" in caminho and self.recentes is not None:
            return [{"number": 524, "head": {"sha": SHA}, "merged_at": None}] + [
                {"number": 500 + i, "head": {"sha": h}, "merged_at": None if h in self.sem_merge else "2026-10-11T10:00:00Z"}
                for i, h in enumerate(self.recentes)]
        if self.recentes and caminho.split("/commits/")[-1].removesuffix("/status") in self.recentes:
            ctx = self.recentes[caminho.split("/commits/")[-1].removesuffix("/status")]
            return {"statuses": [{"context": c, "state": "success"} for c in ctx]}
        if caminho.endswith(f"/commits/{SHA}/status"):
            return {"statuses": [self.ci] if self.ci else []}
        if "/issues/524/comments" in caminho:
            return self.comentarios
        raise AssertionError(f"chamada inesperada: {caminho}")


def ci(estado, em="2026-10-12T12:20:00Z"):
    return {"context": "local-ci", "state": estado, "updated_at": em, "description": "testes com banco e build ok"}


class EsperasTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": tmp.name}),
                  mock.patch("hub.linear.gql", side_effect=AssertionError("teste falou com o Linear")),
                  mock.patch("hub.agenda.em_loop", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        self.reabertas = []
        p = mock.patch.object(linear, "reabrir", self._reabrir)
        p.start()
        self.addCleanup(p.stop)

    def _reabrir(self, ident, fala):
        self.reabertas.append((ident, fala))
        item = {"tipo": "issue", "id": "u", "identifier": ident, "titulo": "t", "descricao": "", "url": "x",
                "modelo": "sonnet", "repos": [], "comentarios": [fala], "estados": {}}
        return linear.tarefa(item)

    def gravar(self, gh=None, max_horas=6):
        return esperas.gravar("krn-164", PR, max_horas, chamar=gh or GitHubFalso(), base_dados=self.dados, agora=T0)

    def voltar(self, gh, agora):
        return agenda.voltar_das_esperas({}, self.dados, chamar=gh, agora=agora)

    def test_gravar_guarda_pr_e_sha(self):
        e = self.gravar()
        self.assertEqual((e["ident"], e["repo"], e["numero"], e["sha"]), ("KRN-164", "acme/clienteh", 524, SHA))
        arq = Path(self.dados, "claude", "esperas", "KRN-164.json")
        self.assertEqual(json.loads(arq.read_text(encoding="utf-8"))["pr"], PR)
        with self.assertRaises(ValueError):
            esperas.gravar("KRN-1", "https://linear.app/x", base_dados=self.dados)

    def test_pendente_nao_faz_nada(self):
        self.gravar()
        gh = GitHubFalso(ci("pending"), [REVISAO])
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(minutes=40)), [])
        self.assertEqual(self.reabertas, [])
        self.assertIsNotNone(esperas.ler("KRN-164", self.dados))
        # conferida há 1 min: não pergunta ao GitHub de novo
        n = len(gh.chamadas)
        self.voltar(gh, T0 + dt.timedelta(minutes=41))
        self.assertEqual(len(gh.chamadas), n)

    def test_verde_com_revisao_enfileira_com_a_fala(self):
        self.gravar()
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO]), T0 + dt.timedelta(minutes=35)), ["linear-krn-164"])
        ident, fala = self.reabertas[0]
        self.assertEqual(ident, "KRN-164")
        self.assertIn("local-ci verde", fala)
        self.assertIn("revisão: Codex: Sem achados sérios.", fala)
        self.assertIn("Só termine: corrija se vermelho, faça o merge, confira no ar.", fala)
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        item = agenda.fila(self.dados)[0]
        self.assertEqual(item["nome"], "linear-krn-164")
        self.assertIn("local-ci verde", item["linear"]["comentarios"][-1])

    def test_volta_do_ci_com_8_rodadas_no_dia_enfileira(self):  # (ficou sem merge)
        self.gravar()
        for m in range(8):  # rodadas de fala: o freio de voltas automáticas não conta pessoa, nem o "outro" do registrar
            conta.registrar({"inicio": conta.iso(T0 - dt.timedelta(hours=3, minutes=-m)), "nome": "linear-krn-164",
                             "tipo": "fala"}, self.dados)
        quando = T0 + dt.timedelta(minutes=35)
        with mock.patch("hub.agenda.em_loop", EM_LOOP), mock.patch("hub.conta.agora", lambda: quando):
            self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO]), quando), ["linear-krn-164"])

    def test_volta_do_ci_com_4_rodadas_sem_fala_ainda_volta(self):  # review 2ª checagem só olha o teto por estado
        self.gravar()
        for m in range(4):  # a fala é a da issue; voltas de espera legítimas não somam como inúteis
            conta.registrar({"inicio": conta.iso(T0 - dt.timedelta(hours=3, minutes=-m)), "nome": "linear-krn-164", "tipo": "espera_ci"}, self.dados)
        quando = T0 + dt.timedelta(minutes=35)
        item = {"tipo": "issue", "id": "u", "identifier": "KRN-164", "ultima_fala": "2026-10-12T05:00:00Z",
                "estado_desde": None, "merge": False, "estados": {}}
        with mock.patch("hub.agenda.em_loop", EM_LOOP), mock.patch("hub.conta.agora", lambda: quando), \
                mock.patch.object(linear, "item_por_ident", lambda i: item):
            self.assertEqual(self.voltar(GitHubFalso(ci("failure"), [REVISAO]), quando), ["linear-krn-164"])

    def test_verde_sem_revisao_espera_20_min(self):
        self.gravar()
        gh = GitHubFalso(ci("success", "2026-10-12T12:20:00Z"))
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(minutes=30)), [])
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(minutes=41)), ["linear-krn-164"])
        self.assertIn("revisão: nenhuma no PR", self.reabertas[0][1])

    def test_sem_revisao_no_mesmo_commit_espera_dobra(self):
        gh = GitHubFalso(ci("success", "2026-10-12T12:20:00Z"))
        self.gravar()
        e = esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0 + dt.timedelta(hours=1))
        self.assertEqual(e["voltas"], 1)
        t1 = T0 + dt.timedelta(hours=1)
        self.assertEqual(self.voltar(gh, t1 + dt.timedelta(minutes=39)), [])  # 40 min na 2ª volta, contados da espera
        self.assertEqual(self.voltar(gh, t1 + dt.timedelta(minutes=43)), ["linear-krn-164"])  # 3 min entre conferências

    def test_revisao_que_nunca_chega_manda_o_executor_revisar_apos_2_h(self):  # e 594
        gh = GitHubFalso(ci("success", "2026-10-12T12:20:00Z"))
        self.gravar()
        for _ in range(6):  # voltas no mesmo commit não esticam a espera além do teto
            e = esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0)
        self.assertEqual(esperas.espera_revisao(e), esperas.REVISAO_TETO)
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(hours=1, minutes=59)), [])
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(hours=2, minutes=21)), ["linear-krn-164"])
        self.assertIn(esperas.SEM_REVISAO, self.reabertas[0][1])

    def test_revisao_cedo_nao_manda_revisar_sozinho(self):
        self.gravar()
        gh = GitHubFalso(ci("success", "2026-10-12T12:20:00Z"))
        self.voltar(gh, T0 + dt.timedelta(minutes=41))
        self.assertNotIn(esperas.SEM_REVISAO, self.reabertas[0][1])

    def test_revisao_de_outro_commit_nao_conta(self):
        self.gravar()
        velha = {**REVISAO, "body": "**Revisão do Codex** (commit `deadbeef`)\n\nAchado sério."}
        self.assertEqual(self.voltar(GitHubFalso(ci("failure"), [velha]), T0 + dt.timedelta(minutes=25)), [])

    def test_vermelho_com_revisao_volta(self):
        self.gravar()
        self.voltar(GitHubFalso(ci("failure"), [REVISAO]), T0 + dt.timedelta(minutes=25))
        self.assertIn("local-ci vermelho", self.reabertas[0][1])

    def test_vermelho_traz_testes_que_falharam_e_o_fim_do_log(self):  #
        log = Path(self.dados) / f"{SHA}.log"
        log.write_text("\n".join(["inicio"] + [f"linha {i}" for i in range(100)]
                                  + ["\x1b[31m FAIL  tests/a.test.ts > grupo > soma\x1b[39m",
                                     " FAIL  tests/a.test.ts > grupo > soma", "fim do log"]), encoding="utf-8")
        self.gravar()
        with mock.patch.object(esperas, "LOG_CI", str(Path(self.dados) / "{sha}.log")):
            self.voltar(GitHubFalso(ci("failure"), [REVISAO]), T0 + dt.timedelta(minutes=25))
        fala = self.reabertas[0][1]
        self.assertIn("Testes que falharam:\n- tests/a.test.ts > grupo > soma\n", fala)
        self.assertEqual(fala.count("- tests/a.test.ts"), 1)  # repetido no log, uma vez na lista
        self.assertIn("fim do log\n```", fala)
        self.assertNotIn("inicio", fala)  # só o fim do log
        self.assertIn("leia o log antes", fala)

    def test_vermelho_sem_acesso_ao_log_diz_que_nao_leu(self):  #
        self.gravar()
        with mock.patch.object(esperas, "LOG_CI", str(Path(self.dados) / "nada" / "{sha}.log")):
            self.voltar(GitHubFalso(ci("failure"), [REVISAO]), T0 + dt.timedelta(minutes=25))
        self.assertIn("Não consegui ler o log", self.reabertas[0][1])

    def test_verde_nao_le_log(self):
        self.gravar()
        with mock.patch.object(esperas, "trecho_log", side_effect=AssertionError("leu log de verde")):
            self.voltar(GitHubFalso(ci("success"), [REVISAO]), T0 + dt.timedelta(minutes=25))
        self.assertNotIn("Últimas", self.reabertas[0][1])

    PEDIDO = {"body": "/local-ci", "author_association": "OWNER", "created_at": "2026-10-12T12:25:00Z"}

    def test_vermelho_com_local_ci_comentado_depois_espera_a_rodada_nova(self):
        self.gravar()
        self.assertEqual(self.voltar(GitHubFalso(ci("failure"), [REVISAO, self.PEDIDO]), T0 + dt.timedelta(minutes=25)), [])

    def test_local_ci_de_fora_do_repo_nao_segura_o_vermelho(self):
        self.gravar()
        outro = {**self.PEDIDO, "author_association": "NONE"}
        self.assertEqual(self.voltar(GitHubFalso(ci("failure"), [REVISAO, outro]), T0 + dt.timedelta(minutes=25)),
                         ["linear-krn-164"])

    def test_local_ci_anterior_ao_vermelho_nao_segura(self):
        self.gravar()
        velho = {**self.PEDIDO, "created_at": "2026-10-12T12:10:00Z"}
        self.assertEqual(self.voltar(GitHubFalso(ci("failure"), [REVISAO, velho]), T0 + dt.timedelta(minutes=25)),
                         ["linear-krn-164"])

    def test_vencida_enfileira_com_aviso(self):
        self.gravar(max_horas=6)  # o teto é 3 h, mesmo pedindo 6
        gh = GitHubFalso(ci("pending"))  # local-ci rodando (pendente) sem sair; sem status nenhum volta em 2 h
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(hours=2, minutes=59)), [])
        self.assertEqual(self.voltar(gh, T0 + dt.timedelta(hours=3, minutes=5)), ["linear-krn-164"])
        fala = self.reabertas[0][1]
        self.assertTrue(fala.startswith("Passou de 3 h esperando."))
        self.assertIn("local-ci ainda sem resultado", fala)

    def test_vencida_sem_rede_tambem_volta(self):
        self.gravar()

        def sem_rede(caminho):
            raise OSError("sem rede")
        self.assertEqual(self.voltar(sem_rede, T0 + dt.timedelta(hours=1)), [])  # antes de vencer: tenta depois
        self.assertEqual(self.voltar(sem_rede, T0 + dt.timedelta(hours=4)), ["linear-krn-164"])
        self.assertIn("Não consegui ler o PR", self.reabertas[0][1])

    # test_issue_apagada_no_linear_apaga_a_espera, test_pr_mergeado_volta_para_conferir_no_ar, test_volta_do_deploy_vai_no_haiku
    # e test_pr_fundido_na_espera_do_ci_vira_espera_do_deploy moram em tests/test_esperas_producao.py: leem o
    # /api/versao da produção de verdade (recibo.no_ar sem mock); fora do snapshot público.

    def test_linear_fora_mantem_a_espera(self):
        self.gravar()
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "head": {"sha": SHA}})
        with mock.patch.object(linear, "reabrir", side_effect=RuntimeError("Linear: timeout")):
            self.assertEqual(self.voltar(gh, T0 + dt.timedelta(minutes=5)), [])
        self.assertIsNotNone(esperas.ler("KRN-164", self.dados))

    def test_no_ar_confirmado_marca_a_telemetria(self):  # (F2): issue→no ar
        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0, no_ar="")
        def situacao(e, chamar=None, agora=None, get=None, _out=None):
            _out["desfecho"] = {"tipo": "no_ar", "sha": SHA, "texto": "No ar."}
            return "está no ar"
        with mock.patch.object(esperas, "situacao", situacao), mock.patch("hub.telemetria.marcar_no_ar") as marcar:
            esperas.prontas(GitHubFalso(), self.dados, T0 + dt.timedelta(minutes=30))
        marcar.assert_called_once_with(PR, T0 + dt.timedelta(minutes=30), self.dados)

    def test_pr_fundido_sem_o_deploy_no_ar_grava_a_troca_para_no_ar(self):
        self.gravar()
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "head": {"sha": SHA}, "merge_commit_sha": "a" * 40})
        with mock.patch("hub.recibo.no_ar", return_value=(False, "ainda não")):
            self.assertEqual(self.voltar(gh, T0 + dt.timedelta(minutes=5)), [])
        e = esperas.ler("KRN-164", self.dados)
        self.assertEqual(e["no_ar"], "")
        self.assertLessEqual(e["max_horas"], esperas.TETO_NO_AR_HORAS)

    def test_tarefa_na_fila_fica_para_depois(self):
        self.gravar()
        agenda.enfileirar(self._reabrir("KRN-164", "Fulano: e aí?"), "linear KRN-164", self.dados)
        self.reabertas.clear()
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO]), T0 + dt.timedelta(minutes=35)), [])
        self.assertIsNotNone(esperas.ler("KRN-164", self.dados))

    def test_repo_com_local_ci_nos_prs_recentes_espera_o_ci(self):
        e = self.gravar(GitHubFalso(recentes={"a1": [], "b2": ["local-ci"]}))
        self.assertTrue(e["com_ci"])
        self.assertEqual(self.voltar(GitHubFalso(None, [REVISAO]), T0 + dt.timedelta(minutes=30)), [])

    def test_repo_sem_local_ci_volta_com_a_revisao(self):
        e = self.gravar(GitHubFalso(recentes={"a1": [], "b2": ["vercel"]}))
        self.assertFalse(e["com_ci"])
        self.assertEqual(self.voltar(GitHubFalso(None), T0 + dt.timedelta(minutes=5)), [])
        self.assertEqual(self.voltar(GitHubFalso(None, [REVISAO]), T0 + dt.timedelta(minutes=10)), ["linear-krn-164"])
        fala = self.reabertas[0][1]
        self.assertIn("local-ci não usado neste repo", fala)
        self.assertIn("revisão: Codex: Sem achados sérios.", fala)
        self.assertIn("NÃO tem CI", fala)
        self.assertIn("faça o merge", fala)

    def test_repo_com_ci_sem_nenhum_status_em_2_h_volta_mandando_testar_e_mergear(self):
        self.gravar(GitHubFalso(recentes={"a1": ["local-ci"]}))
        self.assertEqual(self.voltar(GitHubFalso(None), T0 + dt.timedelta(minutes=110)), [])
        self.assertEqual(self.voltar(GitHubFalso(None), T0 + dt.timedelta(minutes=125)), ["linear-krn-164"])
        fala = self.reabertas[0][1]
        self.assertIn("não começou em 2 h", fala)
        self.assertIn("faça o merge", fala)

    def test_repo_sem_local_ci_e_sem_revisao_volta_em_20_min(self):
        self.gravar(GitHubFalso(recentes={"a1": []}))
        self.assertEqual(self.voltar(GitHubFalso(None), T0 + dt.timedelta(minutes=19)), [])
        self.assertEqual(self.voltar(GitHubFalso(None), T0 + dt.timedelta(minutes=23)), ["linear-krn-164"])
        self.assertIn("revisão: nenhuma no PR", self.reabertas[0][1])

    def test_sem_saber_se_o_repo_usa_ci_espera_o_ci(self):
        self.assertTrue(self.gravar(GitHubFalso())["com_ci"])  # lista de PRs falhou
        self.assertTrue(self.gravar(GitHubFalso(recentes={}))["com_ci"])  # repo sem outro PR
        self.assertTrue(self.gravar(GitHubFalso(recentes={"a1": []}, sem_merge={"a1"}))["com_ci"])  # nenhum mergeado

    def test_pr_sem_merge_nao_conta_como_repo_sem_ci(self):
        # revisão do #175: PR fora do merge não diz nada (fila do local-ci lenta); o mergeado com local-ci decide
        e = self.gravar(GitHubFalso(recentes={"a1": [], "b2": [], "c3": ["local-ci"]}, sem_merge={"a1", "b2"}))
        self.assertTrue(e["com_ci"])
        gh = GitHubFalso(recentes={"a1": [], "c3": ["local-ci"]}, sem_merge={"a1"})
        self.gravar(gh)
        self.assertNotIn("repos/acme/clienteh/commits/a1/status", gh.chamadas)

    def test_gh_api_usa_o_gh_sem_rede_no_teste(self):
        with mock.patch("hub.github_api.get", return_value={"ok": 1}) as get:
            self.assertEqual(esperas.gh_api("repos/acme/x/pulls/1"), {"ok": 1})
        get.assert_called_once_with("repos/acme/x/pulls/1")



class EntregaTest(unittest.TestCase):
    """A entrega que terminou com `voltar-quando` não é cobrada pela conferência e a issue fica In Progress."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        p = mock.patch.dict(os.environ, {"HUB_DATA": tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.saida = Path(tmp.name, "r.md")
        self.saida.write_text(f"PR {PR} no CI; volto quando sair.", encoding="utf-8")
        self.item = {"tipo": "issue", "id": "u", "identifier": "KRN-164", "titulo": "t", "descricao": "", "url": "x",
                     "modelo": "sonnet", "repos": [], "comentarios": [],
                     "estados": {"In Review": "r", "In Progress": "p", "Backlog": "b"}}

    def concluir(self, inicio):
        movidas, cobrou = [], []
        reg = {"ok": True, "saida": str(self.saida), "conta": "1", "duracao_s": 60, "inicio": inicio}
        with mock.patch.object(linear, "mover", lambda i, s, c=None: movidas.append(s)), \
                mock.patch.object(linear, "gql", side_effect=OSError("sem rede")), \
                mock.patch.object(linear, "avisar_zap", lambda *a, **k: None):
            volta = linear._concluir(linear.tarefa(self.item), reg,
                                     conferir_fn=lambda i, t: cobrou.append(t) or "esperar o CI e fazer o merge")
        return volta, movidas, cobrou

    def test_com_espera_nao_cobra_e_fica_in_progress(self):
        esperas.gravar("KRN-164", PR, chamar=GitHubFalso(), agora=T0 + dt.timedelta(minutes=10))
        volta, movidas, cobrou = self.concluir(conta.iso(T0))
        self.assertIsNone(volta)
        self.assertEqual(cobrou, [])
        self.assertEqual(movidas, ["p"])

    def test_espera_de_rodada_anterior_sai_e_conferencia_cobra(self):
        esperas.gravar("KRN-164", PR, chamar=GitHubFalso(), agora=T0 - dt.timedelta(hours=1))
        volta, _, cobrou = self.concluir(conta.iso(T0))
        self.assertIsNotNone(volta)
        self.assertEqual(len(cobrou), 1)
        self.assertIsNone(esperas.ler("KRN-164"))


class PromptTest(unittest.TestCase):
    def test_como_e_merge_liberado_mandam_voltar_quando(self):
        item = {"tipo": "issue", "id": "u", "identifier": "KRN-164", "titulo": "t", "descricao": "", "url": "x",
                "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {}, "merge": True}
        p = linear.tarefa(item).prompt
        self.assertIn("python3 -m hub.linear voltar-quando KRN-164 --pr", p)
        self.assertIn("por mais de 10 min", p)
        self.assertNotIn("Sem o comentário em 20 min, siga", p)
        self.assertNotIn("Espere o status do CI", p)
        upd = linear.tarefa({**item, "tipo": "update", "merge": False}).prompt
        self.assertNotIn("voltar-quando", upd)  # update de projeto não reabre: segue esperando como antes

    def test_cli_grava_a_espera(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}), \
                mock.patch.object(esperas, "gh_api", GitHubFalso()):
            self.assertEqual(linear.main(["voltar-quando", "KRN-164", "--pr", PR, "--max-horas", "2"]), 0)
            self.assertEqual(esperas.ler("KRN-164")["max_horas"], 2.0)
            self.assertEqual(linear.main(["voltar-quando", "KRN-164", "--pr", PR, "--max-horas", "9"]), 0)
            self.assertEqual(esperas.ler("KRN-164")["max_horas"], esperas.TETO_HORAS)  # todo prazo tem teto
            self.assertEqual(linear.main(["voltar-quando", "KRN-164", "--pr", "lixo"]), 1)


MERGE = "a" * 40
PROD = "b" * 40


class NoArTest(unittest.TestCase):
    """`voltar-quando --no-ar` espera o deploy fora da sessão; nada de comentar "ainda não subiu"."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        p = mock.patch.dict(os.environ, {"HUB_DATA": tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def gh(self, compare="ahead", merged=True):
        def chamar(caminho):
            if caminho.endswith("/pulls/524"):
                return {"state": "closed" if merged else "open", "merged": merged, "merge_commit_sha": MERGE if merged else None,
                        "base": {"ref": "main"}, "head": {"sha": SHA}}
            if "/compare/" in caminho:
                self.assertIn(f"{MERGE}...{PROD}", caminho)
                return {"status": compare}
            raise AssertionError(f"chamada inesperada: {caminho}")
        return chamar

    def espera(self, gh, **k):
        return esperas.gravar("krn-713", PR, 6, chamar=gh, base_dados=self.dados, agora=T0, no_ar="", **k)

    def situacao(self, gh, horas=1, versao=PROD, **k):
        e = self.espera(gh, **k)
        with mock.patch("hub.recibo._get_json", lambda u: {"commit": versao}):
            return esperas.situacao(e, gh, T0 + dt.timedelta(hours=horas), get=lambda u: f'{{"commit":"{versao}"}}')

    def test_gravar_nao_olha_ci(self):
        e = self.espera(self.gh())
        self.assertEqual((e["no_ar"], e["url"]), ("", ""))
        self.assertNotIn("com_ci", e)

    def test_contem_volta_com_o_commit(self):
        fala = self.situacao(self.gh("ahead"))
        self.assertIn("está no ar", fala)
        self.assertIn(MERGE[:8], fala)

    def test_nao_contem_espera_calado(self):
        self.assertIsNone(self.situacao(self.gh("behind")))

    def test_prazo_volta_com_o_que_viu(self):
        fala = self.situacao(self.gh("behind"), horas=6)
        self.assertIn("Passou de 6 h", fala)
        self.assertIn("ainda não está no ar", fala)

    def test_producao_fora_espera(self):
        gh = self.gh()
        e = self.espera(gh)
        with mock.patch("hub.recibo._get_json", side_effect=OSError("timeout")):
            self.assertIsNone(esperas.situacao(e, gh, T0 + dt.timedelta(hours=1)))

    def test_pr_sem_merge_espera(self):
        self.assertIsNone(self.situacao(self.gh(merged=False)))

    def test_url_mostra_o_commit(self):
        self.assertIn("está no ar", self.situacao(self.gh("identical"), url="https://x.com/api/versao"))
        self.assertIsNone(self.situacao(self.gh("behind"), url="https://x.com/api/versao"))

    def test_repo_sem_deploy_conhecido_volta_na_hora(self):
        e = dict(self.espera(self.gh()), repo="acme/sem-deploy")
        gh = lambda c: {"merged": True, "merge_commit_sha": MERGE, "base": {"ref": "main"}}  # noqa: E731
        with mock.patch("hub.recibo._site", return_value=None):
            self.assertIn("não sabe conferir", esperas.situacao(e, gh, T0))

    def test_cli_no_ar(self):
        with mock.patch.object(esperas, "gh_api", self.gh()):
            self.assertEqual(linear.main(["voltar-quando", "KRN-713", "--pr", PR, "--no-ar"]), 0)
        self.assertEqual(esperas.ler("KRN-713")["no_ar"], "")


class SemIaTest(unittest.TestCase):
    """volta da espera que não precisa de IA o hub resolve (comenta, move, funde) e não enfileira."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.eventos, self.reabertas, self.merge = [], [], {"codigo": 0, "saida": "", "args": []}
        self.item = {"tipo": "issue", "id": "uid", "identifier": "KRN-164", "merge": False,
                     "estados": {"Done": "d", "In Review": "r"}}

        def reabrir(ident, fala):
            self.reabertas.append(fala)
            return linear.tarefa({"tipo": "issue", "id": "u", "identifier": ident, "titulo": "t", "descricao": "", "url": "x",
                                  "modelo": "sonnet", "repos": [], "comentarios": [fala], "estados": {}})

        def rodar(args):
            self.merge["args"].append(args)
            c = self.merge["codigo"]
            return (c.pop(0) if isinstance(c, list) else c), self.merge["saida"]
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": tmp.name}),
                  mock.patch("hub.agenda.em_loop", lambda *a, **k: None),
                  mock.patch.object(linear, "reabrir", reabrir),
                  mock.patch.object(linear, "item_por_ident", lambda i: self.item),
                  mock.patch.object(linear, "gql", lambda *a, **k: {"issue": self.item}),
                  mock.patch("hub.portao_done.avaliar", return_value={"ok": True, "faltas": [], "esperar": False}),
                  mock.patch.object(linear, "encerrar_sem_ia", lambda it, est, txt: self.eventos.append((est, txt))),
                  mock.patch.object(linear, "comentar", lambda i, t, **k: self.eventos.append(("comentario", t))),
                  mock.patch.object(esperas, "_gh_rodar", rodar),
                  mock.patch("hub.raiz.pedir", lambda acao, **k: self.eventos.append(("pedido", acao)) or "pid-1")):
            p.start()
            self.addCleanup(p.stop)

    def voltar(self, gh, repo_ci=True, minutos=35, **k):
        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0, **k)
        if not repo_ci:
            e = esperas.ler("KRN-164", self.dados)
            e["com_ci"] = False
            Path(self.dados, "claude", "esperas", "KRN-164.json").write_text(json.dumps(e), encoding="utf-8")
        return agenda.voltar_das_esperas({}, self.dados, chamar=gh, agora=T0 + dt.timedelta(minutes=minutos))

    def linhas(self):
        arq = Path(self.dados, "claude", "sem-ia.jsonl")
        return [json.loads(l) for l in arq.read_text(encoding="utf-8").splitlines()] if arq.exists() else []

    def test_pronto_sem_label_merge_vai_para_in_review(self):
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO])), [])
        self.assertEqual(self.reabertas, [])
        self.assertEqual(agenda.fila(self.dados), [])
        est, txt = self.eventos[0]
        self.assertEqual(est, "In Review")
        self.assertIn("a issue não tem a label `merge`", txt)
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        self.assertEqual([(l["ident"], l["tipo"]) for l in self.linhas()], [("KRN-164", "pronto")])

    def test_repo_sem_ci_com_revisao_limpa_tambem(self):
        self.assertEqual(self.voltar(GitHubFalso(None, [REVISAO]), repo_ci=False), [])
        self.assertEqual(self.eventos[0][0], "In Review")
        self.assertIn("sem CI", self.eventos[0][1])

    def test_com_label_merge_funde_e_grava_espera_no_ar(self):
        self.item["merge"] = True
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO])), [])
        self.assertEqual(self.merge["args"], [["pr", "merge", "524", "--repo", "acme/clienteh", "--squash",
                                                "--match-head-commit", SHA]])
        e = esperas.ler("KRN-164", self.dados)
        self.assertEqual(e["no_ar"], "")
        self.assertEqual(self.reabertas, [])
        self.assertIn("Merge feito pelo hub", self.eventos[0][1])
        self.assertEqual([l["tipo"] for l in self.linhas()], ["merge"])
        self.assertNotIn(("pedido", "atualizar-hub"), self.eventos)  # clienteh não é o hub

    def test_merge_no_repo_do_hub_pede_atualizar(self):
        self.item["merge"] = True
        for repo, acao in (("acme/nimbus-hub", "atualizar-hub"), ("acme/bot_whatsapp", "atualizar-runner")):
            self.eventos.clear()
            e = {"ident": "KRN-164", "pr": f"https://github.com/{repo}/pull/524", "repo": repo, "numero": 524, "max_horas": 6}
            gh = lambda c: {"head": {"sha": SHA}}  # noqa: E731
            self.assertIsNone(agenda._sem_ia(e, "fala", {"tipo": "pronto", "sha": SHA, "texto": "x"}, self.dados, gh, T0))
            self.assertIn(("pedido", acao), self.eventos)


    def test_squash_nao_permitido_cai_para_merge(self):
        self.item["merge"] = True
        self.merge.update(codigo=[1, 0], saida="Squash merges are not allowed on this repository.")
        self.voltar(GitHubFalso(ci("success"), [REVISAO]))
        self.assertEqual([a[4 + 1] for a in self.merge["args"]], ["--squash", "--merge"])
        self.assertIsNotNone(esperas.ler("KRN-164", self.dados).get("no_ar"))

    def test_merge_falha_reabre_como_hoje(self):
        self.item["merge"] = True
        self.merge.update(codigo=1, saida="GraphQL: Pull request is not mergeable")
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO])), ["linear-krn-164"])
        self.assertIn("local-ci verde", self.reabertas[0])
        self.assertIn("O hub tentou o merge e falhou: GraphQL", self.reabertas[0])
        self.assertEqual(len(self.merge["args"]), 1)
        self.assertEqual(self.linhas(), [])

    def test_revisao_com_achados_parcial_ou_ci_vermelho_reabrem(self):
        casos = [(ci("success"), "**Revisão do Codex** (commit `%s`)\n\n[P1] quebra x. Sem achados sérios no resto." % SHA[:8]),
                 (ci("success"), "**Revisão do Codex** (commit `%s`)\n\nSem achados sérios (revisão parcial)." % SHA[:8]),
                 (ci("success"), "**Revisão do Codex** (commit `%s`)\n\nAchei um problema." % SHA[:8]),
                 (ci("failure"), REVISAO["body"])]
        for status, corpo in casos:
            self.item["merge"] = True
            self.reabertas.clear()
            revisao = dict(REVISAO, body=corpo)
            self.assertEqual(self.voltar(GitHubFalso(status, [revisao])), ["linear-krn-164"], corpo)
            self.assertEqual(len(self.reabertas), 1)
            self.assertEqual(self.merge["args"], [])
            agenda.tirar("linear-krn-164", self.dados)

    def test_revisao_parcial_no_cabecalho_ou_p0_nao_e_limpa(self):
        def rev(corpo):
            return {"body": corpo}
        cab = f"**Revisão do Claude** (commit `{SHA[:8]}`)"
        self.assertTrue(esperas.revisao_limpa(rev(f"{cab}\n\nSem achados sérios."), SHA))
        self.assertFalse(esperas.revisao_limpa(rev(f"{cab} — diff de 200 kB cortado em 150 kB: revisão parcial\n\nSem achados sérios."), SHA))
        self.assertFalse(esperas.revisao_limpa(rev(f"{cab}\n\n[P0] quebra tudo. Fora isso, sem achados sérios."), SHA))
        self.assertFalse(esperas.revisao_limpa(rev(f"{cab}\n\nTudo bem. Sem achados sérios."), SHA))

    def test_comentarios_do_hub_nao_sao_fala_do_fulano(self):
        textos = []
        with mock.patch.object(linear, "_mover_no_fim", lambda it, est, c=None: textos.append(c)):
            ENCERRAR(dict(self.item, estados={"Done": "d"}), "Done", "No ar: x. Fechado pelo hub, sem IA.")
            ENCERRAR(self.item, "Nada", "PR pronto. Fechado pelo hub, sem IA.")  # sem a coluna: comentar direto
        self.item["merge"] = True
        e = {"ident": "KRN-164", "pr": PR, "repo": "acme/clienteh", "numero": 524, "max_horas": 6}
        agenda._sem_ia(e, "fala", {"tipo": "pronto", "sha": SHA, "texto": "x"}, self.dados, GitHubFalso(), T0)
        textos += [t for ev, t in self.eventos if ev == "comentario"]
        self.assertEqual(len(textos), 3)
        self.assertIn("Merge feito pelo hub", textos[2])
        for t in textos:
            self.assertFalse(linear._eh_fala({"body": t, "user": {"name": "Fulano", "app": False}}), t)

    def test_issue_cancelada_nao_funde_nem_reabre(self):
        self.item.update(merge=True, estado_tipo="canceled")
        self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO])), [])
        self.assertEqual(self.merge["args"], [])
        self.assertEqual(self.reabertas, [])
        self.assertIsNone(esperas.ler("KRN-164", self.dados))

    def test_issue_encerrada_preserva_estado_em_todos_os_desfechos(self):
        for estado in ("canceled", "completed"):
            for merge in (False, True):
                for tipo in ("pronto", "no_ar"):
                    with self.subTest(estado=estado, merge=merge, tipo=tipo):
                        self.item.update(merge=merge, estado_tipo=estado)
                        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0)
                        e = esperas.ler("KRN-164", self.dados)
                        self.assertIsNone(agenda._sem_ia(
                            e, "fala", {"tipo": tipo, "sha": SHA, "texto": "x"},
                            self.dados, GitHubFalso(), T0))
                        self.assertEqual(self.eventos, [])
                        self.assertEqual(self.merge["args"], [])
                        self.assertEqual(self.reabertas, [])
                        self.assertEqual(self.linhas(), [])
                        self.assertIsNone(esperas.ler("KRN-164", self.dados))

    def test_timeout_no_merge_mas_pr_fundido_vale_como_sucesso(self):
        def estoura(args):
            raise subprocess.TimeoutExpired("gh", 120)
        e = {"repo": "acme/clienteh", "numero": 524}
        fundido = lambda c: {"merged": True, "merge_commit_sha": MERGE}  # noqa: E731
        with mock.patch.object(esperas, "_gh_rodar", estoura):
            self.assertEqual(esperas.merge_pr(e, SHA, get=fundido), (True, ""))
            ok, erro = esperas.merge_pr(e, SHA, get=GitHubFalso())
        self.assertFalse(ok)
        self.assertIn("TimeoutExpired", erro)

    def test_erro_no_caminho_sem_ia_reabre(self):
        with mock.patch.object(linear, "item_por_ident", side_effect=OSError("linear fora")):
            self.assertEqual(self.voltar(GitHubFalso(ci("success"), [REVISAO])), ["linear-krn-164"])
        self.assertEqual(len(self.reabertas), 1)

    def test_no_ar_sem_print_ou_com_pendencias_reabre_para_concluir(self):
        from hub import recibo
        self.item.update(description="Pronto quando: print da página anexado na issue e lista de cinco páginas.",
                         comments={"nodes": [{"body": "Falta a lista medida no ar.",
                                              "user": {"app": True}}]})
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "merge_commit_sha": SHA})
        with mock.patch("hub.portao_done.avaliar", wraps=recibo.conferir), mock.patch("hub.recibo.no_ar", return_value=(True, "no ar")):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), ["linear-krn-164"])
        self.assertEqual(self.eventos, [])
        self.assertIn("imagem anexado", self.reabertas[0])
        self.assertIn("Falta a lista", self.reabertas[0])
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        self.assertEqual(self.linhas(), [])

    def test_inc119_clarity_deploy_nao_substitui_aceite_em_gravacao(self):
        from hub import recibo
        self.item.update(
            description="Eventos e tags aparecendo no Clarity; máscara conferida em gravação.",
            comments={"nodes": [{
                "body": "O aceite da KRN-1026 não está fechado. "
                        "Falta: conferir por curl o data-clarity-mask no HTML público "
                        "e olhar as tags numa gravação.",
                "user": {"app": True},
            }]},
        )
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "merge_commit_sha": SHA})
        with mock.patch("hub.portao_done.avaliar", wraps=recibo.conferir), \
             mock.patch("hub.recibo.no_ar", return_value=(True, "no ar")):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), ["linear-krn-164"])
        self.assertEqual(self.eventos, [])
        self.assertIn("gravação", self.reabertas[0])
        self.assertIn("data-clarity-mask", self.reabertas[0])
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        self.assertEqual(self.linhas(), [])

    def test_no_ar_recibo_indisponivel_mantem_espera_sem_sessao(self):
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "merge_commit_sha": SHA})
        with mock.patch("hub.portao_done.avaliar", return_value={"ok": False, "esperar": True, "faltas": []}), \
             mock.patch("hub.recibo.no_ar", return_value=(True, "no ar")):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), [])
        self.assertEqual(self.eventos, [])
        self.assertEqual(self.reabertas, [])
        self.assertEqual(self.linhas(), [])
        self.assertIsNotNone(esperas.ler("KRN-164", self.dados))

    def test_recibo_indisponivel_vence_e_reabre_para_investigar(self):
        e = {"ident": "KRN-164", "pr": PR, "criada_em": conta.iso(T0)}
        with mock.patch("hub.portao_done.avaliar", return_value={"ok": False, "esperar": True, "faltas": []}), \
             mock.patch.object(linear, "item_por_ident", return_value=self.item), \
             mock.patch.object(linear, "gql", return_value={"issue": self.item}):
            fala = agenda._sem_ia(e, "Deploy pronto", {"tipo": "no_ar", "texto": "no ar"},
                                  self.dados, GitHubFalso(), T0 + dt.timedelta(hours=3))
        self.assertIn("Investigue", fala)
        self.assertEqual(self.eventos, [])

    def test_no_ar_erro_de_leitura_nao_perde_retomada(self):
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "merge_commit_sha": SHA})
        with mock.patch("hub.portao_done.avaliar", side_effect=OSError("Linear fora")), \
             mock.patch("hub.recibo.no_ar", return_value=(True, "no ar")):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), ["linear-krn-164"])
        self.assertEqual(self.eventos, [])
        self.assertEqual(self.linhas(), [])

    def test_no_ar_fecha_em_done_sem_enfileirar(self):
        def gh(caminho):
            if caminho.endswith("/pulls/524"):
                return {"state": "closed", "merged": True, "merge_commit_sha": MERGE, "base": {"ref": "main"}, "head": {"sha": SHA}}
            return {"status": "ahead"}
        with mock.patch("hub.recibo._get_json", lambda u: {"commit": PROD}):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), [])
        self.assertEqual(self.reabertas, [])
        self.assertEqual(agenda.fila(self.dados), [])
        self.assertEqual(self.eventos[0][0], "Done")
        self.assertIn("Fechado pelo hub, sem IA", self.eventos[0][1])
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        self.assertEqual([l["tipo"] for l in self.linhas()], ["no_ar"])

    def test_seo_fix_no_ar_retorna_para_auditoria_sem_fechar(self):
        self.item["labels"] = ["merge", "seo-fix"]

        def gh(caminho):
            if caminho.endswith("/pulls/524"):
                return {"state": "closed", "merged": True, "merge_commit_sha": MERGE,
                        "base": {"ref": "main"}, "head": {"sha": SHA}}
            return {"status": "ahead"}

        with mock.patch("hub.recibo._get_json", lambda u: {"commit": PROD}):
            self.assertEqual(self.voltar(gh, no_ar="", minutos=60), ["linear-krn-164"])
        self.assertEqual(self.eventos, [])
        self.assertEqual(len(self.reabertas), 1)
        self.assertIn("repita a auditoria", self.reabertas[0])
        self.assertEqual([t["nome"] for t in agenda.fila(self.dados)], ["linear-krn-164"])
        self.assertIsNone(esperas.ler("KRN-164", self.dados))
        self.assertEqual(self.linhas(), [])


if __name__ == "__main__":
    unittest.main()
