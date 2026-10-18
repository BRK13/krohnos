# tests/test_esteira.py
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import esteira

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
TRES, QUATRO = ("1", "2", "3"), ("1", "2", "3", "4")


def issue(ident, projeto="Cliente A", cat="seo-fix", prio=3, criada="2026-10-08T10:00:00Z"):
    return {"id": f"id-{ident}", "identifier": ident, "priority": prio, "createdAt": criada,
            "project": {"name": projeto}, "labels": {"nodes": [{"name": "esteira"}, {"name": cat}]},
            "team": {"states": {"nodes": [{"id": "todo", "name": "Todo"}]}}}


def uso(cinco, semana, reset_semana="2026-10-12T12:00:00+00:00"):
    return {"cinco_horas": {"pct": cinco, "reset": None}, "semana": {"pct": semana, "reset": reset_semana}, "fonte": "oauth"}


class FolgaTest(unittest.TestCase):
    def test_conta_no_ritmo_tem_folga_e_acima_nao(self):
        # reset em 3 dias = 96 h já passaram da semana: ritmo = 100*96/168 + 10 = 67,1
        d = {"contas": {"1": uso(30, 50), "2": uso(30, 80), "3": uso(80, 10), "4": uso(10, 10)}}
        self.assertEqual(esteira.folga(d, AGORA, ocupadas=0, contas_ativas=TRES), {"1": True, "2": False, "3": False})
        self.assertEqual(esteira.folga(d, AGORA, ocupadas=0, contas_ativas=QUATRO),
                         {"1": True, "2": False, "3": False, "4": True})

    def test_reset_semanal_none_nao_quebra_e_cai_no_fallback(self):
        # de_iso(None) levanta AttributeError; sem reset, _ritmo devolve 100 e a conta com 50% tem folga
        self.assertTrue(esteira.folga({"contas": {"1": uso(30, 50, reset_semana=None)}}, AGORA, ocupadas=0)["1"])

    def test_sem_dado_so_tem_folga_com_fila_vazia(self):
        self.assertEqual(esteira.folga({"contas": {}}, AGORA, ocupadas=0, contas_ativas=TRES), {"1": True, "2": True, "3": True})
        self.assertEqual(esteira.folga({"contas": {}}, AGORA, ocupadas=1, contas_ativas=QUATRO), dict.fromkeys(QUATRO, False))

    def test_conta_sem_login_fica_fora(self):
        # a 4 ainda sem login: não conta como folga (senão a esteira despacharia para quem não roda)
        home = home_com_login(self, TRES)
        Path(home, ".claude-4").mkdir()
        self.assertEqual(set(esteira.folga({"contas": {}}, AGORA, ocupadas=0)), set(TRES))
        Path(home, ".claude-4", ".credentials.json").write_text("{}")
        self.assertEqual(set(esteira.folga({"contas": {}}, AGORA, ocupadas=0)), set(QUATRO))

    def test_vagas_desconta_o_que_ja_esta_na_fila_ou_rodando(self):
        v = esteira.VAGAS_POR_CONTA
        self.assertEqual(esteira.vagas({"1": True, "2": True, "3": False}, ocupadas=1), 2 * v - 1)
        self.assertEqual(esteira.vagas({"1": True, "2": True, "3": True}, ocupadas=3 * v), 0)


class EscolherTest(unittest.TestCase):
    def test_prioridade_depois_antiguidade_e_teto_por_projeto(self):
        hoje = [{"issue": f"X-{i}", "projeto": "Cliente A", "categoria": "seo-fix", "em": "2026-10-09T08:00:00+00:00"}
                for i in range(30)]
        iss = [issue("A", prio=3), issue("B", prio=2, projeto="ClienteJ"), issue("C", prio=2, criada="2026-10-01T00:00:00Z")]
        escolhidas = [i for i, _ in esteira.escolher(iss, 3, hoje, AGORA)]
        # sem teto por projeto/dia: prioridade, depois a mais antiga
        self.assertEqual([i["identifier"] for i in escolhidas], ["C", "B", "A"])

    def test_animacao_fica_no_backlog(self):
        iss = [issue("A", cat="animacao", prio=1), issue("B")]
        self.assertEqual([i["identifier"] for i, _ in esteira.escolher(iss, 5, [], AGORA)], ["B"])

    def test_criar_recusa_animacao(self):
        from hub import linear
        with mock.patch.object(linear, "criar_issue") as criar, self.assertRaises(SystemExit):
            linear.main(["criar", "--repo", "example-org/filmes", "--titulo", "Gerar o clipe 'x'", "--label", "esteira",
                         "--label", "Animacao"])
        criar.assert_not_called()

    def test_teto_em_andamento(self):
        iss = [issue(f"I{i}", projeto=f"P{i}") for i in range(20)]
        self.assertEqual(len(esteira.escolher(iss, 20, [], AGORA, em_andamento=esteira.MAX_EM_ANDAMENTO - 1)), 1)


def home_com_login(teste, contas):
    """CLAUDE_HOME num tmp com login nas contas dadas: o teste nunca lê o /home/nimbus de verdade."""
    t = tempfile.TemporaryDirectory()
    teste.addCleanup(t.cleanup)
    for n in contas:
        if n != "1":
            Path(t.name, f".claude-{n}").mkdir()
            Path(t.name, f".claude-{n}", ".credentials.json").write_text("{}")
    p = mock.patch.dict(os.environ, {"CLAUDE_HOME": t.name})
    p.start()
    teste.addCleanup(p.stop)
    return t.name


class DespacharTest(unittest.TestCase):
    def setUp(self):
        home_com_login(self, TRES)
        p = mock.patch.object(esteira, "vagas_codex", return_value=0)  # sem Codex: só as contas Claude
        p.start()
        self.addCleanup(p.stop)

    def test_move_para_todo_e_grava_estado(self):
        movidas = []
        with tempfile.TemporaryDirectory() as d:
            Path(d, "claude").mkdir()
            Path(d, "claude", "uso-contas.json").write_text(json.dumps({"contas": {}}))

            def gql(q, app=False, **v):
                if "inProgress" in q:  # Q_ANDAMENTO: nada da esteira em andamento
                    return {"issues": {"nodes": []}}
                return {"issues": {"nodes": [issue("KRN-40"), issue("KRN-41", projeto="ClienteJ")]}}
            # uso-contas vazio + fila e rodando vazios = as 3 contas com folga: as 2 issues saem
            feitas = esteira.despachar(base_dados=d, agora=AGORA, gql=gql, mover=lambda i, s: movidas.append(i))
            est = json.loads(Path(d, "claude", "esteira.json").read_text())
        self.assertEqual(sorted(feitas), ["KRN-40", "KRN-41"])
        self.assertEqual(sorted(movidas), ["id-KRN-40", "id-KRN-41"])
        self.assertEqual(est["estoque"]["total"], 0)
        self.assertEqual({x["issue"] for x in est["despachadas"]}, {"KRN-40", "KRN-41"})

    def _rodar(self, estoque, mover, antes=None):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "claude").mkdir()
            Path(d, "claude", "uso-contas.json").write_text(json.dumps({"contas": {}}))
            if antes:
                Path(d, "claude", "esteira.json").write_text(json.dumps({"despachadas": antes}))
            gql = lambda q, app=False, **v: {"issues": {"nodes": [] if "inProgress" in q else estoque}}
            feitas = esteira.despachar(base_dados=d, agora=AGORA, gql=gql, mover=mover)
            return feitas, json.loads(Path(d, "claude", "esteira.json").read_text())

    def test_ja_despachada_nao_e_movida_de_novo(self):
        movidas = []
        antes = [{"issue": "KRN-40", "projeto": "Cliente A", "categoria": "seo-fix", "em": "2026-10-09T08:00:00Z"}]
        feitas, est = self._rodar([issue("KRN-40"), issue("KRN-41")], lambda i, s: movidas.append(i), antes)
        self.assertEqual(feitas, ["KRN-41"])
        self.assertEqual(movidas, ["id-KRN-41"])
        self.assertEqual(est["estoque"]["total"], 0)

    def test_mover_que_falha_nao_trava_a_rodada(self):
        movidas = []

        def mover(i, s):
            if i == "id-KRN-40":
                raise RuntimeError("boom")
            movidas.append(i)
        feitas, est = self._rodar([issue("KRN-40"), issue("KRN-41", projeto="ClienteJ")], mover)
        self.assertEqual(feitas, ["KRN-41"])
        self.assertEqual(movidas, ["id-KRN-41"])
        self.assertEqual([x["issue"] for x in est["despachadas"]], ["KRN-41"])

    def test_estoque_cheio_sai_so_o_que_cabe_pela_prioridade(self):
        movidas = []
        estoque = [issue(f"F{n}", prio=3, cat="conteudo") for n in range(10)]
        estoque += [issue(f"P{n}", projeto="ClienteJ", prio=4) for n in range(9)] + [issue("URG", prio=1)]
        feitas, est = self._rodar(estoque, lambda i, s: movidas.append(i))
        # 3 contas com folga e fila vazia: 3 x VAGAS_POR_CONTA vagas; urgente primeiro, depois as de prioridade 3
        n = min(3 * esteira.VAGAS_POR_CONTA, esteira.MAX_EM_ANDAMENTO)
        self.assertEqual(feitas[0], "URG")  # urgente primeiro, depois prioridade 3 antes da 4
        self.assertEqual(len(movidas), n)
        self.assertTrue(all(f.startswith("F") for f in feitas[1:11]))
        self.assertEqual(est["estoque"]["total"], 20 - n)


def com_desc(i, desc):
    return {**i, "description": desc}


class CodexTest(unittest.TestCase):
    def test_apta(self):
        self.assertTrue(esteira.apta_codex(issue("A", cat="seo-fix")))
        self.assertTrue(esteira.apta_codex(issue("A", cat="conteudo")))
        self.assertTrue(esteira.apta_codex(com_desc(issue("A"), "Execute com a skill `superpowers:subagent-driven-development`")))
        self.assertFalse(esteira.apta_codex(issue("A", cat="pagina-nova")))
        self.assertFalse(esteira.apta_codex(issue("A", cat="buraco")))
        for desc in ("grave um Pedido vps", "rode python3 -m hub.raiz pedir", "mande WhatsApp ao cliente",
                     "envie o e-mail", "migração do banco", "use a skill `seo-audit`", "na VPS"):
            self.assertFalse(esteira.apta_codex(com_desc(issue("A"), desc)), desc)

    def test_prioridade_alta_e_planos_ficam_no_claude(self):
        self.assertFalse(esteira.apta_codex(issue("A", prio=2)))
        i = issue("B", cat="melhoria")
        i["labels"]["nodes"].append({"name": "plano"})
        self.assertFalse(esteira.apta_codex(i))

    def test_fatia_de_dois_tercos_com_folga(self):
        iss = [issue(f"A{n}", criada=f"2026-10-08T10:0{n}:00Z") for n in range(5)]
        r = esteira.escolher(iss, 10, [], AGORA, vagas_codex=4)
        self.assertEqual([x for _, x in r], ["codex", "codex", "claude", "codex", "codex"])

    def test_sem_vaga_claude_aptas_vao_todas_para_o_codex(self):
        iss = [issue("A"), issue("B", cat="pagina-nova"), issue("C", cat="conteudo"), issue("D")]
        r = esteira.escolher(iss, 0, [], AGORA, vagas_codex=2)
        self.assertEqual([(i["identifier"], x) for i, x in r], [("A", "codex"), ("C", "codex")])

    def test_sem_codex_tudo_fica_no_claude(self):
        r = esteira.escolher([issue("A"), issue("B")], 5, [], AGORA, vagas_codex=0)
        self.assertEqual([x for _, x in r], ["claude", "claude"])

    def test_rotulada_codex_sem_vaga_codex_espera(self):
        rot = issue("R", cat="pagina-nova")
        rot["labels"]["nodes"].append({"name": "codex"})
        r = esteira.escolher([rot, issue("B", cat="pagina-nova")], 5, [], AGORA, vagas_codex=0)
        self.assertEqual([(i["identifier"], x) for i, x in r], [("B", "claude")])
        r = esteira.escolher([rot], 5, [], AGORA, vagas_codex=1)
        self.assertEqual([x for _, x in r], ["codex"])

    def test_despacho_rotula_antes_de_mover(self):
        home_com_login(self, TRES)
        ordem = []
        est = dict(issue("KRN-1"), team={"states": {"nodes": [{"id": "todo", "name": "Todo"}]},
                                         "labels": {"nodes": [{"id": "lb-codex", "name": "codex"}]}})

        def gql(q, app=False, **v):
            if "issueAddLabel" in q:
                ordem.append(("label", v["i"], v["l"]))
                return {}
            return {"issues": {"nodes": [] if "inProgress" in q else [est]}}
        with tempfile.TemporaryDirectory() as d:
            Path(d, "claude").mkdir()
            Path(d, "claude", "uso-contas.json").write_text(json.dumps({"contas": {}}))
            feitas = esteira.despachar(base_dados=d, agora=AGORA, gql=gql, livres_codex=1,
                                       mover=lambda i, s: ordem.append(("mover", i)))
            gravado = json.loads(Path(d, "claude", "esteira.json").read_text())
        self.assertEqual(feitas, ["KRN-1"])
        self.assertEqual(ordem, [("label", "id-KRN-1", "lb-codex"), ("mover", "id-KRN-1")])
        self.assertEqual(gravado["despachadas"][0]["executor"], "codex")
        self.assertEqual(gravado["rodizio_codex"], {"aptas": 1, "codex": 1})


if __name__ == "__main__":
    unittest.main()
