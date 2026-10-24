"""nenhuma issue fica travada mais de 3 h. Um teste por padrão achado em 16/10
e a regra das frentes do plano em sequência."""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

from hub import agenda, conta, esperas, linear, travadas

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
PR = "https://github.com/acme/clienteh/pull/805"


def comentario(corpo, quando="2026-10-16T10:00:00Z", app=True):
    return {"body": corpo, "createdAt": quando, "user": {"name": "Claude", "app": app}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        p = mock.patch.dict(os.environ, {"HUB_DATA": tmp.name})
        p.start()
        self.addCleanup(p.stop)


class ChamadoTest(Base):
    def test_chamado_e_gravado_uma_vez_por_cooldown(self):
        self.assertTrue(travadas.chamar("krn-845", "teto", "freio", self.dados, T0))
        self.assertFalse(travadas.chamar("KRN-845", "teto", "freio", self.dados, T0 + dt.timedelta(hours=2)))
        self.assertTrue(travadas.chamar("KRN-845", "teto", "freio", self.dados, T0 + dt.timedelta(hours=3, minutes=1)))
        self.assertEqual(travadas.ler("KRN-845", self.dados)["motivo"], "teto")

    def test_rodar_chama_o_orquestrador_com_o_resumo_e_marca_o_feito(self):  # padrão 2: o freio nunca chamava ninguém
        travadas.chamar("KRN-845", "Teto: 8 execuções sem mudar de estado", "freio de teto", self.dados, T0)
        textos = []
        consulta = lambda q, **v: {"issue": {"identifier": "KRN-845", "title": "Titulo", "url": "https://linear/x",  # noqa: E731
                                            "updatedAt": "2026-10-16T10:00:00Z", "state": {"name": "In Progress"},
                                            "comments": {"nodes": [{"body": "Teto: 8 execuções", "createdAt": "2026-10-16T11:00:00Z"}]}}}
        feitos = travadas.rodar_chamados(lambda t: textos.append(t) or (0, "decidi: cancelar"), consulta, T0, self.dados, log=lambda *a: None)
        self.assertEqual(feitos, ["KRN-845"])
        self.assertIn("Teto: 8 execuções sem mudar de estado", textos[0])
        self.assertIn("hub.linear comentar KRN-845", textos[0])
        self.assertIn("cd ~/repos/nimbus-hub &&", textos[0])  # o orch só tem a chave do Linear no clone dele
        self.assertIn("Titulo", textos[0])
        c = travadas.ler("KRN-845", self.dados)
        self.assertEqual((c["codigo"], c["resultado"]), (0, "decidi: cancelar"))
        self.assertEqual(travadas.rodar_chamados(lambda t: self.fail("chamou de novo"), consulta, T0, self.dados), [])

    def test_sem_linear_o_orquestrador_ainda_recebe_o_motivo(self):
        travadas.chamar("KRN-828", "25 vezes em 24 h", "freio de laço", self.dados, T0)
        textos = []

        def fora(q, **v):
            raise RuntimeError("Linear fora")
        travadas.rodar_chamados(lambda t: textos.append(t) or (0, ""), fora, T0, self.dados, log=lambda *a: None)
        self.assertIn("25 vezes em 24 h", textos[0])

    def test_freio_de_teto_chama_o_orquestrador(self):
        item = {"tipo": "issue", "id": "uuid", "identifier": "KRN-845", "estado_desde": "2026-10-16T01:00:00Z", "ultima_fala": None}
        with mock.patch.object(linear, "comentar"):
            self.assertTrue(agenda.avisar_teto_estado(item, "rodou 8× sem mudar de estado", self.dados))
        self.assertIn("rodou 8×", travadas.ler("KRN-845", self.dados)["motivo"])
        self.assertEqual([c["ident"] for c in travadas.pendentes(self.dados)], ["KRN-845"])

    def test_freio_de_laco_chama_o_orquestrador(self):  # "25 vezes em 24 h"
        t = linear.tarefa({"identifier": "KRN-828", "id": "i", "tipo": "issue", "titulo": "x", "url": "u", "descricao": "d",
                           "comentarios": [], "repos": [], "modelo": "opus"})
        with mock.patch.object(linear, "comentar"):
            agenda._pausa_por_loop(t, "rodou 25 vezes em 24 h sem fechar", self.dados)
        self.assertIn("25 vezes", travadas.ler("KRN-828", self.dados)["motivo"])

    def test_chamado_que_falha_nao_derruba_o_freio(self):
        with mock.patch.object(travadas, "chamar", side_effect=OSError("disco")):
            travadas.chamar_sem_falhar("KRN-1", "m", "o", self.dados, log=lambda *a: None)


class ComentarioReabreTest(unittest.TestCase):
    def issue(self, ultimo, estado="started"):
        return {"state": {"type": estado}, "creator": {"app": False},
                "comments": {"nodes": [comentario("Na fila do Claude da VPS.", "2026-10-16T10:00:00Z"), ultimo]}}

    def test_repasse_do_orquestrador_reabre_a_execucao(self):  # padrão 3:, rumo dado às 11:03 e ninguém retomou
        rumo = comentario("Retome pelo PR #12.\n\n_Repasse do orquestrador._", "2026-10-16T11:03:00Z")
        self.assertTrue(linear.quer_resposta(self.issue(rumo)))

    def test_comentario_do_proprio_claude_continua_sem_reabrir(self):
        self.assertFalse(linear.quer_resposta(self.issue(comentario("Fiz X.", "2026-10-16T11:03:00Z"))))

    def test_repasse_em_issue_fechada_nao_reabre(self):
        rumo = comentario("Ok.\n\n_Repasse do orquestrador._", "2026-10-16T11:03:00Z")
        self.assertFalse(linear.quer_resposta(self.issue(rumo, "completed")))


class GravarTest(Base):
    def test_temporario_nao_e_previsivel_nem_segue_symlink(self):  # revisão do Codex no PR #384
        alvo = os.path.join(self.dados, "alvo.txt")
        open(alvo, "w").write("intacto")
        travadas.chamar("KRN-845", "teto", "freio", self.dados, T0)  # cria a pasta
        pasta = travadas._pasta(self.dados)
        os.symlink(alvo, pasta / ".KRN-845.json.tmp")
        travadas.chamar("KRN-845", "teto 2", "freio", self.dados, T0 + dt.timedelta(hours=4))
        self.assertEqual(open(alvo).read(), "intacto")
        self.assertEqual(travadas.ler("KRN-845", self.dados)["motivo"], "teto 2")


class VarrerTest(Base):
    def issue(self, ident="KRN-741", comentarios=(), upd="2026-10-16T14:00:00Z"):
        return {"id": "u-" + ident, "identifier": ident, "title": "t", "url": "u", "updatedAt": upd, "description": "",
                "comments": {"nodes": list(comentarios)}}

    def varrer(self, issues, gh=None, ocupadas=()):
        consulta = lambda q, **v: {"issues": {"nodes": issues}}  # noqa: E731
        return travadas.varrer(consulta, gh or (lambda c: {"state": "open", "merged": False, "head": {"sha": "a" * 40}}),
                               T0, set(ocupadas), self.dados, log=lambda *a: None)

    def test_merge_feito_e_issue_parada_vira_espera_do_deploy(self):  # padrão 4: e 594, "conferir no ar"
        i = self.issue(comentarios=[comentario(f"Merge feito: {PR}")])
        gh = lambda c: {"state": "closed", "merged": True, "merge_commit_sha": "b" * 40, "head": {"sha": "a" * 40}}  # noqa: E731
        self.assertEqual(self.varrer([i], gh), {"KRN-741": "no_ar"})
        e = esperas.ler("KRN-741", self.dados)
        self.assertEqual(e["no_ar"], "")
        self.assertIsNone(travadas.ler("KRN-741", self.dados))  # não incomodou o orquestrador

    def test_pr_aberto_e_issue_parada_grava_a_espera_da_revisao(self):  # padrão 1:, PR #805 sem ninguém esperando
        i = self.issue("KRN-519", [comentario(f"PR {PR} aberto, aguardo a revisão.")])
        self.assertEqual(self.varrer([i]), {"KRN-519": "espera"})
        self.assertNotIn("no_ar", esperas.ler("KRN-519", self.dados))

    def test_pr_do_comentario_novo_vale_mais_que_o_da_descricao(self):  # revisão do Codex no PR #384
        i = self.issue("KRN-519", [comentario(f"PR novo {PR} aberto.")])
        i["description"] = "Antes: https://github.com/acme/clienteh/pull/100"
        self.assertEqual(travadas._prs(i), PR)

    def test_sem_pr_chama_o_orquestrador(self):  # padrão 5:, timer vencido sem ninguém ler
        i = self.issue("KRN-819", [comentario("Volto às 10:05 UTC.")], "2026-10-16T10:05:00Z")
        self.assertEqual(self.varrer([i]), {"KRN-819": "orquestrador"})
        self.assertIn("In Progress há 10 h", travadas.ler("KRN-819", self.dados)["motivo"])
        self.assertEqual(self.varrer([i]), {})  # cooldown: uma chamada só

    def test_o_que_roda_ou_espera_com_prazo_nao_e_travada(self):
        rodando, esperando = self.issue("KRN-1"), self.issue("KRN-2")
        esperas.gravar("KRN-2", PR, chamar=lambda c: {"head": {"sha": "a" * 40}}, base_dados=self.dados, agora=T0 - dt.timedelta(hours=1), no_ar="")
        self.assertEqual(self.varrer([rodando, esperando], ocupadas={"linear-krn-1"}), {})

    def test_espera_vencida_sem_ninguem_ler_chama_o_orquestrador(self):
        esperas.gravar("KRN-2", PR, chamar=lambda c: {"head": {"sha": "a" * 40}}, base_dados=self.dados, agora=T0 - dt.timedelta(hours=5))
        self.assertEqual(self.varrer([self.issue("KRN-2")], gh=lambda c: {"state": "closed", "merged": False}), {"KRN-2": "orquestrador"})


class FrentesTest(unittest.TestCase):
    """O dono, 17/10: frentes em paralelo; o hub só solta a seguinte, nunca segura."""
    ESTADOS = {"nodes": [{"id": "s-bl", "name": "Backlog"}, {"id": "s-td", "name": "Todo"}]}

    def i(self, ident, titulo, tipo, labels=("plano-x",), concluida=None, comentarios=()):
        return {"id": ident, "identifier": ident, "title": titulo, "completedAt": concluida,
                "state": {"type": tipo, "name": {"unstarted": "Todo", "backlog": "Backlog", "started": "In Progress",
                                                 "completed": "Done"}[tipo]},
                "labels": {"nodes": [{"name": n} for n in labels]}, "comments": {"nodes": [{"body": b} for b in comentarios]},
                "team": {"states": self.ESTADOS}}

    def rodar(self, issues):
        movidas = []
        consulta = lambda q, **v: {"issues": {"nodes": issues, "pageInfo": {"hasNextPage": False}}}  # noqa: E731
        r = linear.sequenciar_frentes(consulta, lambda i, s, c=None: movidas.append((i, s, c)), log=lambda *a: None, agora=T0)
        return r, movidas

    def test_numero_da_frente(self):
        self.assertEqual(linear.numero_da_frente("Plano X · Frente 3: testes"), 3)
        self.assertIsNone(linear.numero_da_frente("sem ordem"))

    def test_frente_adiantada_em_todo_fica_em_todo(self):
        (devolvidas, liberadas), movidas = self.rodar([self.i("A1", "frente 1", "started"), self.i("A2", "frente 2", "unstarted")])
        self.assertEqual((devolvidas, liberadas, movidas), ([], [], []))

    def test_frente_anterior_pronta_libera_a_segurada(self):
        seg = f"_{linear.SEGURA_FRENTE}._"
        (devolvidas, liberadas), movidas = self.rodar([self.i("A1", "frente 1", "completed", concluida="2026-10-16T19:00:00Z"),
                                                       self.i("A2", "frente 2", "backlog", comentarios=[seg])])
        self.assertEqual((devolvidas, liberadas), ([], ["A2"]))
        self.assertEqual(movidas[0][:2], ("A2", "s-td"))

    def test_so_libera_se_todas_as_issues_da_frente_anterior_fecharam(self):
        (_, liberadas), _ = self.rodar([self.i("A1", "frente 1", "completed", concluida="2026-10-16T19:00:00Z"),
                                        self.i("B1", "frente 1 (b)", "started"), self.i("A2", "frente 2", "backlog")])
        self.assertEqual(liberadas, [])

    def test_plano_abandonado_nao_e_solto_sem_ter_sido_segurado(self):
        (_, liberadas), _ = self.rodar([self.i("A1", "frente 1", "completed", concluida="2026-09-01T10:00:00Z"),
                                        self.i("A2", "frente 2", "backlog")])
        self.assertEqual(liberadas, [])

    def test_proposta_e_planos_diferentes_nao_se_misturam(self):
        (_, liberadas), movidas = self.rodar([self.i("A1", "frente 1", "completed", concluida="2026-10-16T19:00:00Z"),
                                              self.i("A2", "frente 2", "backlog", ("plano-x", "proposta")),
                                              self.i("B2", "frente 2", "unstarted", ("plano-y",))])
        self.assertEqual((liberadas, movidas), ([], []))

    def test_frente_em_andamento_nao_e_interrompida(self):
        (devolvidas, _), _ = self.rodar([self.i("A1", "frente 1", "started"), self.i("A2", "frente 2", "started")])
        self.assertEqual(devolvidas, [])


class PrazoDoCockpitTest(Base):
    def pendencias(self):
        from hub import cockpit
        cockpit._MEMO.clear()  # o cache de 60 s do painel vaza entre testes (test_cockpit)

        def no(i):
            return {"id": i, "identifier": i, "title": "t", "url": "u", "updatedAt": "2026-10-16T14:00:00Z",
                    "state": {"name": "In Progress"}, "labels": {"nodes": []}}

        def gql(q, app=False, **v):
            if "agentSessions" in q:
                return {"agentSessions": {"nodes": []}}
            return {"issues": {"nodes": [] if "comments" in q or "plano-" in q else [no("KRN-1"), no("KRN-2")]}}
        return cockpit.pendencias(self.dados, T0, gql=gql)["travadas"]

    def test_in_progress_parada_aparece_e_espera_com_prazo_nao(self):
        self.assertEqual([x["identifier"] for x in self.pendencias()], ["KRN-1", "KRN-2"])
        esperas.gravar("KRN-2", PR, chamar=lambda c: {"head": {"sha": ""}}, base_dados=self.dados, agora=T0 - dt.timedelta(hours=1), no_ar="")
        self.assertEqual([x["identifier"] for x in self.pendencias()], ["KRN-1"])


if __name__ == "__main__":
    unittest.main()
