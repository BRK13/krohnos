import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from hub import claude, dataforseo, mcp_proxy, sites
from hub.mcp_grupos import ErroUso, pasta, visibilidade_ia
from hub.mcp_grupos import visibilidade_ia_pesquisa as pesquisa
from hub.mcp_grupos import visibilidade_ia_rodada as rodada

MERCADO = {"engines", "locationCode", "languageCode", "prompts", "archivePromptIds"}
ORIGINAL = {
    "save_ai_visibility_tracker": {"projectId", *MERCADO},
    "get_ai_visibility_tracker": {"projectId"},
    "set_ai_visibility_schedule": {"projectId", "enabled", "scheduleInterval", "scheduleTime"},
    "estimate_ai_visibility_cost": {"projectId", "scheduleInterval", "patch", "promptIds"},
}


def agora():
    return dt.datetime.now(dt.timezone.utc)


def data_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class VisibilidadeIATest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "DATAFORSEO_AUTH": "x"})
        self.env.start()
        self.rede = mock.patch.object(dataforseo, "post", side_effect=AssertionError("sem rede"))
        self.rede.start()

    def tearDown(self):
        self.rede.stop(); self.env.stop(); self.tmp.cleanup()

    def x(self, nome, **args):
        return mcp_proxy.executar(nome, {"site": "psite15", **args})

    def salvar(self, *textos, **extra):
        return self.x("save_ai_visibility_tracker", prompts=[{"text": t} for t in textos], **extra)

    def test_tools_list_tem_as_propriedades_do_original(self):
        for nome, props in ORIGINAL.items():
            f = mcp_proxy.FERRAMENTAS[nome]
            self.assertEqual(set(f["inputSchema"]["properties"]) - {"site"}, props, nome)
            self.assertNotIn("Custa crédito", f["description"])
        s = mcp_proxy.FERRAMENTAS["save_ai_visibility_tracker"]["inputSchema"]["properties"]
        self.assertEqual(s["prompts"]["maxItems"], 100)
        self.assertEqual(s["engines"]["items"]["enum"], ["chatgpt", "gemini", "google_ai_overview", "claude", "perplexity"])
        self.assertEqual(s["prompts"]["items"]["properties"]["text"]["maxLength"], 2000)
        self.assertEqual(mcp_proxy.FERRAMENTAS["set_ai_visibility_schedule"]["inputSchema"]["required"], ["enabled"])

    def test_sem_tracker(self):
        r = self.x("get_ai_visibility_tracker")
        self.assertEqual((r["ok"], r["configured"], r["tracker"], r["recentRuns"]), (True, False, None, []))
        self.assertTrue(r["providerConfigured"])
        self.assertEqual(r["brands"][0], {"name": "Cliente G", "domain": "site-e.exemplo.com", "own": True})
        self.assertEqual(len(r["capabilities"]), 5)

    def test_salvar_e_ler_do_disco(self):
        r = self.x("save_ai_visibility_tracker", prompts=[{"text": "  melhor psicólogo online  "}, {"text": "terapia", "topic": "Preço"},
                                                          {"text": "melhor psicólogo online"}, {"text": "Cliente G é bom?"}])
        self.assertEqual((r["created"], r["updated"], r["skipped"]), (3, 0, 1))
        t = r["state"]["tracker"]
        self.assertEqual((t["enabled"], t["scheduleInterval"], t["projectId"], t["locationCode"], t["languageCode"]),
                         (True, "weekly", "psite15", 2076, "pt"))
        self.assertGreaterEqual(data_iso(t["nextCheckAt"]), agora() + dt.timedelta(days=7) - dt.timedelta(seconds=5))
        self.assertLess(data_iso(t["nextCheckAt"]), agora() + dt.timedelta(days=9))
        arq = pasta("visibilidade_ia", "psite15") / "tracker.json"
        salvo = json.loads(arq.read_text(encoding="utf-8"))
        self.assertEqual(salvo["tracker"]["engines"], ["chatgpt", "gemini"])
        g = self.x("get_ai_visibility_tracker")
        self.assertEqual(g["prompts"], r["state"]["prompts"])
        self.assertEqual(g["topics"], ["General", "Preço"])
        self.assertEqual([(p["text"], p["topic"], p["branded"]) for p in g["prompts"]],
                         [("melhor psicólogo online", "General", False), ("terapia", "Preço", False), ("Cliente G é bom?", "General", True)])
        self.assertEqual(g["engines"], ["chatgpt", "gemini"])

    def test_editar_texto_arquiva_o_antigo(self):
        p = self.salvar("a")["state"]["prompts"][0]
        r = self.x("save_ai_visibility_tracker", prompts=[{"id": p["id"], "text": "b"}])
        self.assertEqual((r["created"], r["updated"]), (1, 1))
        ps = {q["text"]: q for q in r["state"]["prompts"]}
        self.assertTrue(ps["a"]["archived"])
        self.assertEqual(ps["a"]["id"], p["id"])
        self.assertFalse(ps["b"]["archived"])
        # voltar ao texto antigo restaura o prompt arquivado (o histórico continua)
        r = self.x("save_ai_visibility_tracker", prompts=[{"id": ps["b"]["id"], "text": "a", "topic": "Novo"}])
        a = next(q for q in r["state"]["prompts"] if q["text"] == "a")
        self.assertEqual((a["id"], a["archived"], a["topic"]), (p["id"], False, "Novo"))
        r = self.x("save_ai_visibility_tracker", archivePromptIds=[p["id"]], engines=["claude", "claude"])
        self.assertEqual(r["updated"], 1)
        self.assertEqual(r["state"]["engines"], ["claude"])
        self.assertEqual(r["state"]["topics"], [])

    def test_agenda_liga_e_pausa(self):
        with self.assertRaises(ErroUso):
            self.x("set_ai_visibility_schedule", enabled=True)
        self.salvar("a")
        r = self.x("set_ai_visibility_schedule", enabled=False)
        self.assertEqual((r["ok"], r["state"]["tracker"]["enabled"], r["run"]), (True, False, None))
        r = self.x("set_ai_visibility_schedule", enabled=True, scheduleInterval="daily")
        t = r["state"]["tracker"]
        self.assertEqual((t["enabled"], t["scheduleInterval"], r["run"], t["baselinePending"]), (True, "daily", None, True))
        self.assertGreater(data_iso(t["nextCheckAt"]), agora())
        rodadas = pasta("visibilidade_ia", "psite15") / "rodadas"
        rodadas.mkdir()
        (rodadas / "r1.json").write_text(json.dumps({
            "id": "r1", "status": "completed", "trigger": "baseline", "createdAt": "2026-10-09T05:00:00.000Z",
            "completedAt": "2026-10-09T05:01:00.000Z", "observations": [
                {"promptId": r["state"]["prompts"][0]["id"], "status": "completed", "answerMarkdown": "x"},
                {"promptId": "z", "status": "failed", "answerMarkdown": None}]}))
        r = self.x("set_ai_visibility_schedule", enabled=True, scheduleInterval="weekly",
                   scheduleTime={"weekday": 1, "hour": 6, "minute": 30, "timeZone": "America/Sao_Paulo"})
        n = data_iso(r["state"]["tracker"]["nextCheckAt"])
        self.assertEqual((n.weekday(), n.hour, n.minute), (0, 9, 30))
        self.assertGreater(n, agora())
        g = self.x("get_ai_visibility_tracker")
        self.assertEqual(g["recentRuns"], [{"id": "r1", "status": "completed", "trigger": "baseline", "expected": 2, "completed": 1,
                                            "failed": 1, "pending": 0, "createdAt": "2026-10-09T05:00:00.000Z",
                                            "completedAt": "2026-10-09T05:01:00.000Z", "pollAfterSeconds": 0}])
        self.assertTrue(g["prompts"][0]["hasResults"])
        # mesma cadência sem horário mantém o próximo
        r2 = self.x("set_ai_visibility_schedule", enabled=True)
        self.assertEqual(r2["state"]["tracker"]["nextCheckAt"], r["state"]["tracker"]["nextCheckAt"])

    def test_primeiro_liga_com_horario_mantem_o_horario_e_marca_a_base(self):
        self.salvar("a")
        r = self.x("set_ai_visibility_schedule", enabled=True, scheduleInterval="weekly",
                   scheduleTime={"weekday": 1, "hour": 6, "minute": 30, "timeZone": "America/Sao_Paulo"})
        t = r["state"]["tracker"]
        n = data_iso(t["nextCheckAt"])
        self.assertEqual((n.weekday(), n.hour, n.minute, t["baselinePending"], r["run"]), (0, 9, 30, True, None))
        self.assertTrue(json.loads((pasta("visibilidade_ia", "psite15") / "tracker.json").read_text())["tracker"]["baselinePending"])

    def test_proxima_mensal_com_fuso(self):
        a = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
        # 23:00 do último dia do mês em São Paulo (UTC-3) = 02:00 UTC do dia 1º seguinte
        self.assertEqual(visibilidade_ia.proxima(a, "monthly", {"hour": 23, "minute": 0, "timeZone": "America/Sao_Paulo"}),
                         dt.datetime(2026, 11, 1, 2, 0, tzinfo=dt.timezone.utc))

    def test_estimativa(self):
        r = self.x("estimate_ai_visibility_cost", patch={"prompts": [{"text": t} for t in "abc"],
                                                         "engines": ["chatgpt", "gemini", "claude", "perplexity"]})
        self.assertEqual((r["promptCount"], r["engineCount"], r["observations"]), (3, 4, 12))
        # 3 × (0,004 + 0,004 + 0,18 + 0,04), preço ao vivo nos dois campos
        self.assertAlmostEqual(r["costUsd"], 0.684)
        self.assertEqual((r["providerCostUsd"], r["runNowCostUsd"], r["costCredits"], r["runNowCostCredits"]), (r["costUsd"], r["costUsd"], 0, 0))
        self.assertEqual((r["scheduleInterval"], r["checksPerMonth"], r["currency"]), ("weekly", 4, "USD"))
        self.assertAlmostEqual(r["monthlyCostUsd"], 2.736)
        self.assertFalse((pasta("visibilidade_ia", "psite15") / "tracker.json").exists())  # estimar não grava
        ps = self.salvar("a", "b", "c")["state"]["prompts"]
        r = self.x("estimate_ai_visibility_cost", scheduleInterval="daily", promptIds=[ps[0]["id"]])
        self.assertEqual((r["observations"], r["checksPerMonth"]), (2, 30))
        self.assertAlmostEqual(r["costUsd"], 0.008)
        self.assertAlmostEqual(r["monthlyCostUsd"], 0.24)

    def test_pedido_errado(self):
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("get_ai_visibility_tracker", {})
        with self.assertRaises(ErroUso):
            self.x("estimate_ai_visibility_cost")  # sem tracker e sem patch
        ruins = [dict(engines=["bing"]), dict(engines=[]), dict(prompts=[{"text": str(i)} for i in range(101)]),
                 dict(prompts=[{"text": " "}]), dict(prompts=[{"text": "a", "id": "nao-uuid"}]),
                 dict(prompts=[{"text": "a", "id": "4b1f6c7e-2d7a-4a7e-9a55-0d1c3e0f9a11"}]),
                 dict(locationCode=9999), dict(locationCode=2076, languageCode="en"), dict(engines=["chatgpt"], locationCode=2275),
                 dict(prompts="a")]
        for args in ruins:
            with self.subTest(args=args), self.assertRaises(ErroUso):
                self.x("save_ai_visibility_tracker", **args)
        self.salvar(*[str(i) for i in range(60)])
        with self.assertRaises(ErroUso):
            self.salvar(*[f"n{i}" for i in range(41)])  # passa de 100 não arquivados
        for args in [dict(enabled="sim"), dict(enabled=True, scheduleInterval="hourly"),
                     dict(enabled=True, scheduleTime={"hour": 25, "minute": 0}),
                     dict(enabled=True, scheduleTime={"hour": 5, "minute": 0, "timeZone": "Lua/Base"}),
                     dict(enabled=True, scheduleTime={"hour": 5, "minute": 0, "timeZone": ""})]:
            with self.subTest(args=args), self.assertRaises(ErroUso):
                self.x("set_ai_visibility_schedule", **args)
        with self.assertRaises(ErroUso):
            self.x("estimate_ai_visibility_cost", scheduleInterval="hourly")
        with self.assertRaises(ErroUso):
            self.x("estimate_ai_visibility_cost", promptIds=["4b1f6c7e-2d7a-4a7e-9a55-0d1c3e0f9a11"])

    def test_trocar_pais_usa_o_idioma_do_pais(self):
        r = self.salvar("a", engines=["claude"])
        self.assertEqual(r["state"]["tracker"]["locationCode"], 2076)
        r = self.x("save_ai_visibility_tracker", locationCode=2840)
        self.assertEqual((r["state"]["tracker"]["locationCode"], r["state"]["tracker"]["languageCode"]), (2840, "en"))


PESQUISA = {"research_ai_visibility_prompts": {"projectId", "keyword"},
            "generate_ai_visibility_prompts": {"projectId", "topic", "excludePrompts", "locationCode", "languageCode"},
            "complete_ai_research_setup": {"projectId"}}
MENCOES = [
    {"question": "ups tracking issue", "answer": "x", "ai_search_volume": 900, "sources": [{"domain": "ups-com.example", "url": "https://ups-com.example/a"}]},
    {"question": "best online therapists for expats", "answer": "Cliente G lists them.", "ai_search_volume": 100,
     "sources": [{"domain": "www.site-e.exemplo.com", "url": "https://www.site-e.exemplo.com/x", "title": "X"}]},
    {"question": "the best online therapist for expats in 2025", "answer": "y", "ai_search_volume": 50,
     "sources": [{"domain": "blog.betterhelp-com.example", "url": "https://blog.betterhelp-com.example/b", "title": None},
                 {"domain": "www.site-e.exemplo.com", "url": "https://www.site-e.exemplo.com/x", "title": "X"}]},
    {"question": "is an online therapist worth it", "answer": "z", "ai_search_volume": 10, "sources": [],
     "search_results": [{"domain": "nytimes.com"}]},
    {"question": "online cooking class", "answer": "w", "ai_search_volume": 5, "sources": [{"domain": "reddit.com", "url": "https://reddit.com/r"}]},
    {"question": "  ", "answer": "", "ai_search_volume": 1},
]
SERP = [{"type": "organic", "domain": "www.betterhelp-com.example"}, {"type": "organic", "domain": "reddit.com"},
        {"type": "paid", "domain": "ups-com.example"}]


def resposta(itens, custo):
    return [{"status_code": 20000, "cost": custo, "result": [{"items": itens}]}]


def topicos(n=5):
    return {"suggestedTopics": [{"name": f"tema {i}", "prompts": [f"pergunta {i}.{j}" for j in range(5)]} for i in range(n)],
            "suggestedKeywords": ["terapia online", " ", "x" * 101, "psicólogo brasileiro"]}


class PesquisaTest(unittest.TestCase):
    setUp, tearDown, x, salvar = VisibilidadeIATest.setUp, VisibilidadeIATest.tearDown, VisibilidadeIATest.x, VisibilidadeIATest.salvar

    @staticmethod
    def worker():
        with contextlib.redirect_stdout(io.StringIO()) as out:
            codigo = pesquisa.ia_pendentes()
        return codigo, out.getvalue()

    @contextlib.contextmanager
    def ia(self, **efeito):
        """claude.perguntar com mock, e a espera do MCP pela fila roda o worker (como o .path faria)."""
        with mock.patch.object(claude, "perguntar", **efeito) as p, \
                mock.patch.object(pesquisa.time, "sleep", side_effect=lambda _: self.worker()):
            yield p

    @contextlib.contextmanager
    def sem_worker(self):
        with mock.patch.object(pesquisa, "ESPERA_IA", 0.2), mock.patch.object(pesquisa, "INTERVALO_IA", 0.01), \
                mock.patch.object(claude, "perguntar", side_effect=AssertionError("o MCP não chama o claude")):
            yield

    def fila(self):
        return pesquisa._fila()

    def test_tools_list(self):
        for nome, props in PESQUISA.items():
            f = mcp_proxy.FERRAMENTAS[nome]
            self.assertEqual(set(f["inputSchema"]["properties"]) - {"site"}, props, nome)
            self.assertEqual(f["description"].endswith(" Custa crédito do DataForSEO."), nome == "research_ai_visibility_prompts")
        self.assertEqual(mcp_proxy.FERRAMENTAS["research_ai_visibility_prompts"]["inputSchema"]["required"], ["keyword"])

    def test_pesquisa(self):
        self.salvar("Is an online   therapist worth it")
        def post(caminho, tarefas):
            return resposta(MENCOES, 0.1) if "llm_mentions" in caminho else resposta(SERP, 0.002)
        with mock.patch.object(dataforseo, "post", side_effect=post) as p:
            r = self.x("research_ai_visibility_prompts", keyword=" Online Therapist ")
            self.assertEqual([c[0][0] for c in p.call_args_list],
                             ["ai_optimization/llm_mentions/search/live", "serp/google/organic/live/advanced"])
            self.assertEqual(p.call_args_list[0][0][1], [{
                "target": [{"keyword": "online therapist", "search_scope": ["question", "answer"], "match_type": "partial_match"}],
                "platform": "chat_gpt", "location_code": 2840, "language_code": "en", "limit": 100, "order_by": ["ai_search_volume,desc"]}])
            self.assertEqual(p.call_args_list[1][0][1], [{"keyword": "online therapist", "location_code": 2840, "language_code": "en",
                                                          "device": "desktop", "os": "windows", "depth": 50}])
            self.assertEqual((r["ok"], r["keyword"], r["custo"]), (True, "Online Therapist", 0.102))
            self.assertEqual([q["text"] for q in r["prompts"]], ["Best online therapists for expats", "Is an online therapist worth it"])
            a, b = r["prompts"]
            self.assertEqual(a["variants"], ["The best online therapist for expats in 2025"])
            self.assertEqual(a["sources"], [{"domain": "www.site-e.exemplo.com", "url": "https://www.site-e.exemplo.com/x", "title": "X", "own": True},
                                            {"domain": "blog.betterhelp-com.example", "url": "https://blog.betterhelp-com.example/b", "title": None, "own": False}])
            self.assertEqual((a["ownDomainCited"], a["brandMentioned"], a["tracked"]), (True, True, False))
            self.assertEqual((b["ownDomainCited"], b["brandMentioned"], b["tracked"], b["sources"]), (False, False, True, []))
            # cache de 24 h: a segunda vez não paga
            r2 = self.x("research_ai_visibility_prompts", keyword="online therapist")
            self.assertEqual(p.call_count, 2)
            self.assertEqual((r2["custo"], r2["prompts"]), (0, r["prompts"]))
        self.assertTrue(list((pasta("visibilidade_ia", "psite15") / "cache").glob("*.json")))

    def test_pesquisa_erro_do_dataforseo(self):
        with mock.patch.object(dataforseo, "post", side_effect=RuntimeError("DataForSEO respondeu HTTP 500")):
            r = self.x("research_ai_visibility_prompts", keyword="terapia")
        self.assertEqual((r["ok"], r["reason"]), (False, "dataforseo_erro"))

    def test_gerar(self):
        self.salvar("Melhor psicólogo online")
        antes = (pasta("visibilidade_ia", "psite15") / "tracker.json").read_text()
        ia = '```json\n{"name": "Psicólogo no exterior", "prompts": ["melhor  psicólogo ONLINE", "Terapia online barata", ' \
             '"psicólogo brasileiro em Lisboa", "Psicólogo brasileiro em lisboa", "como achar psicólogo em português"]}\n```'
        with self.ia(return_value=ia) as p:
            r = self.x("generate_ai_visibility_prompts", excludePrompts=[" terapia online  BARATA"], locationCode=2076, languageCode="pt")
        self.assertEqual(r, {"ok": True, "topic": "Psicólogo no exterior",
                             "prompts": ["psicólogo brasileiro em Lisboa", "como achar psicólogo em português"]})
        self.assertEqual((p.call_args.kwargs["modelo"], p.call_args.kwargs["nome"]), ("sonnet", "mcp-visibilidade-ia"))
        self.assertIn("Melhor psicólogo online", p.call_args[0][0])
        self.assertIn("Zenklub", p.call_args[0][0])
        self.assertNotIn("Regras profissionais", p.call_args[0][0])  # regras do conselho saíram do contexto (#88)
        self.assertEqual((pasta("visibilidade_ia", "psite15") / "tracker.json").read_text(), antes)  # não grava
        # tópico salvo com outra caixa: usa o nome salvo
        with self.ia(return_value='{"name": "x", "prompts": ["novo"]}'):
            self.assertEqual(self.x("generate_ai_visibility_prompts", topic=" general ")["topic"], "General")
        for ruim in ["não é json", '{"name": "a", "prompts": "b"}', claude.ContaPausada("pausada"), RuntimeError("caiu")]:
            efeito = {"side_effect": ruim} if isinstance(ruim, Exception) else {"return_value": ruim}
            with self.subTest(ruim=ruim), self.ia(**efeito):
                r = self.x("generate_ai_visibility_prompts")
                self.assertEqual((r["ok"], r["reason"]), (False, "ia_erro"))
        with self.ia(return_value='{"name": "a", "prompts": ["MELHOR psicólogo online"]}'), \
                self.assertRaises(ErroUso):
            self.x("generate_ai_visibility_prompts")  # nada novo

    def test_gerar_aceita_texto_em_volta_do_json(self):
        self.salvar("Melhor psicólogo online")
        ia = 'Aqui está:\n```json\n{"name": "X", "prompts": ["a","b","c","d","e"]}\n```\nEspero que ajude.'
        with self.ia(return_value=ia):
            r = self.x("generate_ai_visibility_prompts")
        self.assertEqual((r["ok"], r["topic"], len(r["prompts"])), (True, "X", 5))

    def test_setup_semeia_e_e_idempotente(self):
        with self.ia(return_value=json.dumps(topicos())) as p:
            r = self.x("complete_ai_research_setup")
            r2 = self.x("complete_ai_research_setup")
        p.assert_called_once()
        kws = ["tema 0", "tema 1", "tema 2", "tema 3", "tema 4", "terapia online", "psicólogo brasileiro"]
        self.assertEqual(r, {"ok": True, "keywords": kws, "status": "ready", "review": None})
        self.assertEqual(r2, r)
        g = self.x("get_ai_visibility_tracker")
        self.assertEqual(g["topics"], ["tema 0", "tema 1", "tema 2"])
        self.assertEqual(len(g["prompts"]), 15)
        t = g["tracker"]
        self.assertEqual((g["engines"], t["scheduleInterval"], t["enabled"]), (["chatgpt"], "weekly", True))
        self.assertGreaterEqual(data_iso(t["nextCheckAt"]), agora() + dt.timedelta(days=7) - dt.timedelta(seconds=5))

    def test_setup_com_rastreador_nao_semeia(self):
        self.salvar("a")
        with self.ia(return_value=json.dumps(topicos())):
            r = self.x("complete_ai_research_setup")
        self.assertEqual(r["status"], "ready")
        g = self.x("get_ai_visibility_tracker")
        self.assertEqual([p["text"] for p in g["prompts"]], ["a"])
        self.assertEqual(json.loads((pasta("visibilidade_ia", "psite15") / "tracker.json").read_text())["researchKeywords"], r["keywords"])

    def test_setup_falha_da_ia(self):
        for ruim in ["{}", "[", RuntimeError("caiu"), json.dumps({"suggestedTopics": [{"name": "a", "prompts": ["p", "p"]}]})]:
            efeito = {"side_effect": ruim} if isinstance(ruim, Exception) else {"return_value": ruim}
            with self.subTest(ruim=ruim), self.ia(**efeito):
                r = self.x("complete_ai_research_setup")
                self.assertEqual((r["ok"], r["status"], r["keywords"], r["review"]), (False, "failed", [], None))
                self.assertTrue(r["message"])
        self.assertFalse((pasta("visibilidade_ia", "psite15") / "tracker.json").exists())

    def test_cinco_prompts_por_topico(self):
        with self.ia(return_value=json.dumps({"name": "a", "prompts": [f"p{i}" for i in range(7)]})):
            self.assertEqual(self.x("generate_ai_visibility_prompts")["prompts"], ["p0", "p1", "p2", "p3", "p4"])
        sete = {"suggestedTopics": [{"name": f"tema {i}", "prompts": [f"q{i}.{j}" for j in range(7)]} for i in range(3)]}
        with self.ia(return_value=json.dumps(sete)):
            self.assertEqual(self.x("complete_ai_research_setup")["status"], "ready")
        self.assertEqual(len(self.x("get_ai_visibility_tracker")["prompts"]), 15)

    def test_fila_resposta_pronta(self):
        pedidos, respostas = self.fila()
        chave = hashlib.sha256("oi".encode()).hexdigest()
        (respostas / f"{chave}.json").write_text('{"texto": "olá"}', encoding="utf-8")
        with self.sem_worker():
            self.assertEqual(pesquisa._perguntar("oi"), "olá")
            self.assertEqual((list(respostas.iterdir()), list(pedidos.iterdir())), ([], []))  # lida, apagada, sem pedido
            (respostas / f"{chave}.json").write_text('{"erro": "conta pausada"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "conta pausada"):
                pesquisa._perguntar("oi")

    def test_fila_demora_devolve_em_andamento(self):
        self.salvar("a")
        with self.sem_worker():
            r = self.x("generate_ai_visibility_prompts")
            self.assertEqual(r, {"ok": False, "reason": "ia_em_andamento", "message": pesquisa.EM_ANDAMENTO})
            [pedido] = self.fila()[0].glob("*.json")
            self.assertIn("Responda só com um objeto JSON", json.loads(pedido.read_text(encoding="utf-8"))["prompt"])
            (pasta("visibilidade_ia", "psite15") / "tracker.json").unlink()
            r = self.x("complete_ai_research_setup")
        self.assertEqual((r["ok"], r["status"], r["keywords"], r["review"]), (True, "running", [], None))
        self.assertIn("20 segundos", r["message"])
        self.assertEqual(len(list(self.fila()[0].glob("*.json"))), 2)
        self.assertFalse((pasta("visibilidade_ia", "psite15") / "tracker.json").exists())

    def test_worker_da_fila(self):
        pedidos, respostas = self.fila()
        for nome, corpo in [("a.json", {"prompt": "bom"}), ("b.json", {"prompt": "ruim"}), ("c.json", "nem json"),
                            ("d.json", {"prompt": "velho"})]:
            (pedidos / nome).write_text(corpo if isinstance(corpo, str) else json.dumps(corpo), encoding="utf-8")
        velho = time.time() - 2 * 86400
        os.utime(pedidos / "d.json", (velho, velho))
        (respostas / "antiga.json").write_text('{"texto": "x"}', encoding="utf-8")
        os.utime(respostas / "antiga.json", (velho, velho))
        def perguntar(prompt, **kw):
            if prompt == "ruim":
                raise claude.ContaPausada("pausada")
            return f"resposta de {prompt}"
        with mock.patch.object(claude, "perguntar", side_effect=perguntar) as p:
            codigo, log = self.worker()
        self.assertEqual(codigo, 0)
        self.assertEqual([c[0][0] for c in p.call_args_list], ["bom", "ruim"])
        self.assertEqual((p.call_args.kwargs["modelo"], p.call_args.kwargs["nome"]), ("sonnet", "mcp-visibilidade-ia"))
        self.assertEqual(list(pedidos.iterdir()), [])
        lidas = {a.name: json.loads(a.read_text(encoding="utf-8")) for a in respostas.iterdir()}
        self.assertEqual(lidas["a.json"], {"texto": "resposta de bom"})
        self.assertEqual(lidas["b.json"], {"erro": "pausada"})
        self.assertIn("erro", lidas["c.json"])  # pedido ilegível vira erro, não para a fila
        self.assertEqual(set(lidas), {"a.json", "b.json", "c.json"})  # velhos descartados
        self.assertIn("d.json: pedido de mais de um dia", log)
        self.assertEqual(list(pasta("visibilidade_ia", "_fila_ia").glob(".*.tmp")), [])

    def test_fila_de_ponta_a_ponta(self):
        self.salvar("Melhor psicólogo online")
        with self.sem_worker():
            self.assertEqual(self.x("generate_ai_visibility_prompts", topic="Exterior")["reason"], "ia_em_andamento")
        with mock.patch.object(claude, "perguntar", return_value='{"prompts": ["psicólogo em Lisboa"]}') as p:
            self.assertEqual(self.worker()[0], 0)
        p.assert_called_once()
        with self.sem_worker():
            r = self.x("generate_ai_visibility_prompts", topic="Exterior")
        self.assertEqual(r, {"ok": True, "topic": "Exterior", "prompts": ["psicólogo em Lisboa"]})
        self.assertEqual([list(d.iterdir()) for d in self.fila()], [[], []])

    def test_cron_dos_agendados_pula_a_fila(self):
        (self.fila()[0] / "x.json").write_text('{"prompt": "p"}', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rodada.rodar_agendados(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_pesquisa_pedido_errado(self):
        ruins = [("research_ai_visibility_prompts", {}), ("research_ai_visibility_prompts", {"keyword": " "}),
                 ("research_ai_visibility_prompts", {"keyword": "x" * 101}),
                 ("generate_ai_visibility_prompts", {"excludePrompts": ["a"] * 11}),
                 ("generate_ai_visibility_prompts", {"locationCode": -1}), ("generate_ai_visibility_prompts", {"languageCode": "p"}),
                 ("generate_ai_visibility_prompts", {"topic": "x" * 101}),
                 ("generate_ai_visibility_prompts", {"locationCode": 0}), ("generate_ai_visibility_prompts", {"languageCode": ""})]
        for nome, args in ruins:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                self.x(nome, **args)
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("complete_ai_research_setup", {})



RODADA = {
    "run_ai_visibility_check": {"projectId", "maxCostUsd", "promptIds"},
    "get_ai_visibility_run": {"projectId", "runId"},
    "get_ai_visibility_results": {"projectId", "runId", "topic", "engines", "promptId", "includeHistory", "competitorGap", "branded",
                                  "cursor", "limit"},
    "get_ai_visibility_answer": {"projectId", "observationId"},
}
CHATGPT = "ai_optimization/chat_gpt/llm_scraper/live/advanced"
CLAUDE = "ai_optimization/claude/llm_responses/live"


def raspagem(markdown, fontes, custo=0.004):
    return [{"status_code": 20000, "cost": custo, "result": [{"datetime": "2026-09-30 18:41:09 +00:00", "markdown": markdown, "sources": fontes}]}]


def modelo(texto, notas, custo=0.17):
    return [{"status_code": 20000, "cost": custo, "result": [{"items": [{"type": "reasoning", "sections": [{"text": "pensando"}]},
                                                                        {"type": "message", "sections": [{"text": texto, "annotations": notas}]}]}]}]


class RodadaTest(unittest.TestCase):
    setUp, tearDown, x = VisibilidadeIATest.setUp, VisibilidadeIATest.tearDown, VisibilidadeIATest.x

    def preparar(self):
        r = self.x("save_ai_visibility_tracker", engines=["claude", "chatgpt"],
                   prompts=[{"text": "melhor psicólogo online"}, {"text": "terapia para brasileiros no exterior", "topic": "Exterior"}])
        return {p["text"]: p["id"] for p in r["state"]["prompts"]}

    def post(self, falha_claude=False):
        def post(caminho, tarefas, timeout=None):
            corpo = tarefas[0]
            if caminho == CHATGPT and corpo["keyword"] == "melhor psicólogo online":
                return raspagem("**Cliente G** lista psicólogos. [site-e.exemplo.com](https://site-e.exemplo.com/x?utm_source=chatgpt.com) "
                                "Veja também o Zenklub.",
                                [{"url": "https://site-e.exemplo.com/x?utm_source=chatgpt.com", "title": "X"}, {"url": "javascript:alert(1)"}])
            if caminho == CHATGPT:  # cita o site sem citar o nome: citação sem menção
                return raspagem("Procure um diretório confiável.", [{"url": "https://www.site-e.exemplo.com/p", "title": None}])
            if falha_claude:
                raise RuntimeError("DataForSEO HTTP 500")
            if "melhor" in corpo["user_prompt"]:  # menciona a marca sem citar o site
                return modelo("O Vittude e o Cliente G atendem online.", [{"url": "https://vittude-com.example/a", "title": "A"}])
            return [{"status_code": 20000, "cost": 0.17, "result": [{"items": []}]}]  # sem resposta
        return post

    def rodar(self, **args):
        return self.x("run_ai_visibility_check", maxCostUsd=args.pop("maxCostUsd", 0.368), **args)

    def test_tools_list(self):
        for nome, props in RODADA.items():
            f = mcp_proxy.FERRAMENTAS[nome]
            self.assertEqual(set(f["inputSchema"]["properties"]) - {"site"}, props, nome)
            self.assertEqual(f["description"].endswith(" Custa crédito do DataForSEO."), nome == "run_ai_visibility_check")
        p = mcp_proxy.FERRAMENTAS["run_ai_visibility_check"]["inputSchema"]
        self.assertEqual((p["required"], p["properties"]["maxCostUsd"]["maximum"]), (["maxCostUsd"], 100))
        r = mcp_proxy.FERRAMENTAS["get_ai_visibility_results"]["inputSchema"]["properties"]
        self.assertEqual((r["limit"]["maximum"], r["limit"]["default"], r["branded"]["default"]), (50, 25, "neutral"))

    def test_rodar_dois_motores_dois_prompts(self):
        ids = self.preparar()
        with mock.patch.object(dataforseo, "post", side_effect=self.post()) as p:
            r = self.rodar()
        chamadas = sorted((c[0][0], json.dumps(c[0][1], ensure_ascii=False, sort_keys=True)) for c in p.call_args_list)
        self.assertEqual(len(chamadas), 4)
        self.assertEqual([c[0] for c in chamadas], [CHATGPT, CHATGPT, CLAUDE, CLAUDE])
        corpos = [json.loads(c[1])[0] for c in chamadas]
        self.assertEqual(corpos[2], {"user_prompt": "melhor psicólogo online", "model_name": "claude-sonnet-5", "web_search": True,
                                     "force_web_search": True, "max_output_tokens": 4096, "web_search_country_iso_code": "BR"})
        g = corpos[0]
        self.assertEqual({k: g[k] for k in ("keyword", "location_code", "language_code")}, {"keyword": "melhor psicólogo online",
                                                                                            "location_code": 2076, "language_code": "pt"})
        self.assertEqual((r["ok"], r["status"], r["trigger"], r["expected"], r["completed"], r["failed"], r["pending"], r["pollAfterSeconds"]),
                         (True, "completed", "manual", 4, 4, 0, 0, 0))
        self.assertEqual(r["custo"], 0.348)
        self.assertTrue(r["completedAt"])
        salvo = json.loads((pasta("visibilidade_ia", "psite15") / "rodadas" / f"{r['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual((salvo["status"], salvo["custo"], salvo["locationCode"], salvo["languageCode"]), ("completed", 0.348, 2076, "pt"))
        self.assertGreaterEqual(salvo["updatedAt"], salvo["createdAt"])  # cada gravação marca a hora: é dela que conta a rodada parada
        obs = {(o["prompt"], o["engine"]): o for o in salvo["observations"]}
        self.assertEqual({o["runId"] for o in obs.values()}, {r["id"]})
        a = obs[("melhor psicólogo online", "chatgpt")]
        self.assertEqual(g["tag"], a["id"])
        self.assertEqual(a["sources"], [{"url": "https://site-e.exemplo.com/x", "domain": "site-e.exemplo.com", "title": "X", "position": 1}])
        self.assertEqual(a["collectedAt"], "2026-09-30T18:41:09.000Z")
        marca = {b["name"]: b for b in a["brands"]}
        self.assertEqual((marca["Cliente G"]["mentioned"], marca["Cliente G"]["cited"], marca["Cliente G"]["firstMention"]), (True, True, 0))
        self.assertEqual((marca["Zenklub"]["mentioned"], marca["Zenklub"]["cited"]), (True, False))
        so_cita = {b["name"]: b for b in obs[("terapia para brasileiros no exterior", "chatgpt")]["brands"]}["Cliente G"]
        self.assertEqual((so_cita["mentioned"], so_cita["cited"]), (False, True))
        so_mencao = {b["name"]: b for b in obs[("melhor psicólogo online", "claude")]["brands"]}
        self.assertEqual((so_mencao["Cliente G"]["mentioned"], so_mencao["Cliente G"]["cited"], so_mencao["Vittude"]["mentioned"]),
                         (True, False, True))
        vazia = obs[("terapia para brasileiros no exterior", "claude")]
        self.assertEqual((vazia["status"], vazia["answerMarkdown"]), ("completed", None))
        # o rastreador vê as respostas e a rodada
        t = self.x("get_ai_visibility_tracker")
        self.assertEqual(t["recentRuns"][0]["id"], r["id"])
        self.assertTrue(all(p["hasResults"] for p in t["prompts"]))
        self.assertEqual(self.x("get_ai_visibility_run", runId=r["id"])["completed"], 4)

        # resultados: sem runId pega só baseline/agendada; a manual pede o runId
        nada = self.x("get_ai_visibility_results")
        self.assertEqual((nada["runId"], nada["rows"], nada["totalCount"]), (None, [], 0))
        res = self.x("get_ai_visibility_results", runId=r["id"])
        self.assertEqual((res["totalCount"], res["coverage"]), (4, {"expected": 4, "completed": 3, "noAnswer": 1, "failed": 0, "pending": 0}))
        self.assertEqual([(w["topic"], w["engine"]) for w in res["rows"]],
                         [("Exterior", "chatgpt"), ("Exterior", "claude"), ("General", "chatgpt"), ("General", "claude")])
        self.assertEqual(res["rows"][0]["answerStatus"], "answered")
        self.assertEqual(res["rows"][1]["answerStatus"], "no_answer")
        self.assertEqual(res["rows"][2]["citationCount"], 1)
        self.assertNotIn("answerMarkdown", res["rows"][0])
        own = res["summaries"][0]
        self.assertEqual((own["name"], own["own"], own["answers"], own["mentions"], own["citations"]), ("Cliente G", True, 3, 2, 2))
        self.assertEqual(res["appliedFilters"], {"competitorGap": False, "branded": "neutral"})
        self.assertFalse(res["truncated"])
        pag = self.x("get_ai_visibility_results", runId=r["id"], limit=3)
        self.assertEqual((len(pag["rows"]), pag["nextCursor"]), (3, "3"))
        pag2 = self.x("get_ai_visibility_results", runId=r["id"], limit=3, cursor="3")
        self.assertEqual((len(pag2["rows"]), pag2["nextCursor"]), (1, None))
        f = self.x("get_ai_visibility_results", runId=r["id"], engines=["claude"], topic="General")
        self.assertEqual([(w["prompt"], w["engine"]) for w in f["rows"]], [("melhor psicólogo online", "claude")])
        self.assertEqual(f["appliedFilters"]["engines"], ["claude"])
        self.assertEqual(self.x("get_ai_visibility_results", runId=r["id"], branded="branded")["totalCount"], 0)
        h = self.x("get_ai_visibility_results", promptId=ids["melhor psicólogo online"], includeHistory=True)
        self.assertEqual((h["runId"], h["totalCount"]), (r["id"], 2))

        # resposta
        oid = a["id"]
        ans = self.x("get_ai_visibility_answer", observationId=oid)
        self.assertEqual(ans["answerText"], "Cliente G lista psicólogos.  Veja também o Zenklub.")
        self.assertTrue(ans["answerMarkdown"].startswith("**Cliente G**"))
        self.assertEqual(ans["sources"], a["sources"])
        self.assertEqual((ans["requestedLocationCode"], ans["requestedLanguageCode"], ans["truncated"]), (2076, "pt", False))
        self.assertEqual(ans["observation"]["id"], oid)
        spans = {m["name"]: (m["domain"], m["spans"]) for m in ans["mentions"]}
        self.assertEqual(spans["Cliente G"], ("site-e.exemplo.com", [{"start": 0, "end": len("Cliente G")}]))  # o fim é o tamanho do nome
        self.assertEqual(spans["Zenklub"][0], "")

    def test_gap_e_resultado_agendado(self):
        self.preparar()
        with mock.patch.object(dataforseo, "post", side_effect=self.post()):
            r = rodada.rodar(SITE(), visibilidade_ia.ler(SITE()), "scheduled")[0]
        res = self.x("get_ai_visibility_results")
        self.assertEqual((res["runId"], res["run"]["trigger"]), (r["id"], "scheduled"))
        gap = self.x("get_ai_visibility_results", competitorGap=True)
        self.assertEqual(gap["totalCount"], 0)  # onde há concorrente, a marca também aparece

    def test_resposta_longa_trunca(self):
        self.preparar()
        longo = "Cliente G " + "a" * 30000
        with mock.patch.object(dataforseo, "post", side_effect=lambda c, t, timeout=None: raspagem(longo, []) if c == CHATGPT else modelo("x", [])):
            r = self.rodar()
        oid = next(o["id"] for o in rodada.ler_rodada(SITE(), r["id"])["observations"] if o["engine"] == "chatgpt")
        ans = self.x("get_ai_visibility_answer", observationId=oid)
        self.assertEqual((ans["truncated"], len(ans["answerMarkdown"]), len(ans["answerText"])), (True, 24000, 24000))

    def test_um_motor_falha(self):
        self.preparar()
        with mock.patch.object(dataforseo, "post", side_effect=self.post(falha_claude=True)):
            r = self.rodar()
        self.assertEqual((r["status"], r["completed"], r["failed"], r["custo"]), ("partial", 2, 2, 0.008))
        falhas = [o for o in rodada.ler_rodada(SITE(), r["id"])["observations"] if o["status"] == "failed"]
        self.assertEqual({o["engine"] for o in falhas}, {"claude"})
        self.assertIn("HTTP 500", falhas[0]["error"])
        res = self.x("get_ai_visibility_results", runId=r["id"], branded="all")
        self.assertEqual(res["coverage"]["failed"], 2)

    def test_teto_recusa_sem_chamar(self):
        ids = self.preparar()
        with self.assertRaisesRegex(ErroUso, r"0\.3680"):
            self.rodar(maxCostUsd=0.3)
        self.assertEqual(visibilidade_ia.rodadas(SITE()), [])
        # um prompt só cabe no teto
        with mock.patch.object(dataforseo, "post", side_effect=self.post()) as p:
            r = self.rodar(maxCostUsd=0.184, promptIds=[ids["melhor psicólogo online"]])
        self.assertEqual((p.call_count, r["expected"]), (2, 2))

    def test_teto_real_para_de_comecar_respostas(self):
        self.x("save_ai_visibility_tracker", engines=["chatgpt"], prompts=[{"text": "a"}, {"text": "b"}])
        with mock.patch.object(rodada, "SIMULTANEAS", 1), \
                mock.patch.object(dataforseo, "post", return_value=raspagem("Resposta.", [], custo=0.01)) as p:
            r = self.rodar(maxCostUsd=0.008)  # estimativa US$ 0,008 cabe; a conta real da primeira já passa
        self.assertEqual((p.call_count, r["status"], r["completed"], r["failed"], r["custo"]), (1, "partial", 1, 1, 0.01))
        falha = [o for o in rodada.ler_rodada(SITE(), r["id"])["observations"] if o["status"] == "failed"]
        self.assertEqual(falha[0]["error"], "teto de custo atingido")

    def test_rodada_corrompida(self):
        p = pasta("visibilidade_ia", "psite15") / "rodadas"
        p.mkdir()
        rid = "0b6c1f53-3b1d-4b8e-9d55-2a3a52f1f0a1"
        for conteudo in ("nem json", "[1, 2]"):
            (p / f"{rid}.json").write_text(conteudo, encoding="utf-8")
            with self.subTest(conteudo=conteudo), self.assertRaisesRegex(ErroUso, "corrompido"):
                self.x("get_ai_visibility_run", runId=rid)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(visibilidade_ia.rodadas(SITE()), [])

    def test_rodada_em_andamento_recusa(self):
        self.preparar()
        d = pasta("visibilidade_ia", "psite15") / "rodadas"
        d.mkdir()
        rid = "11111111-1111-4111-8111-111111111111"
        base = {"id": rid, "status": "running", "trigger": "scheduled", "completedAt": None,
                "observations": [{"id": "o1", "status": "pending", "promptId": "p", "engine": "chatgpt"}]}
        (d / f"{rid}.json").write_text(json.dumps({**base, "createdAt": visibilidade_ia._iso(agora())}))
        with self.assertRaisesRegex(ErroUso, rid):
            self.rodar()
        # rodada grande e viva: começou há 2 h, mas gravou uma resposta agora há pouco
        (d / f"{rid}.json").write_text(json.dumps({**base, "createdAt": visibilidade_ia._iso(agora() - dt.timedelta(hours=2)),
                                                   "updatedAt": visibilidade_ia._iso(agora() - dt.timedelta(minutes=5))}))
        with self.assertRaisesRegex(ErroUso, rid):
            self.rodar()
        self.assertEqual(self.x("get_ai_visibility_run", runId=rid)["status"], "running")
        # parada há mais de 1 h: falha a velha e roda
        (d / f"{rid}.json").write_text(json.dumps({**base, "createdAt": visibilidade_ia._iso(agora() - dt.timedelta(hours=2))}))
        with mock.patch.object(dataforseo, "post", side_effect=self.post()):
            self.assertEqual(self.rodar()["status"], "completed")
        velha = self.x("get_ai_visibility_run", runId=rid)
        self.assertEqual((velha["status"], velha["failed"], velha["pending"]), ("failed", 1, 0))

    def test_corpos_dos_motores(self):
        t = rodada._tarefa
        self.assertEqual(t("gemini", "a+b 5%", "tag", 2076, "pt"), ("ai_optimization/gemini/llm_scraper/live/advanced",
                         {"keyword": "a%2Bb 5%25", "location_code": 2076, "language_code": "pt-BR", "tag": "tag"}))
        self.assertEqual(t("google_ai_overview", "x", "tag", 2076, "pt"), ("serp/google/organic/live/advanced",
                         {"keyword": "x", "location_code": 2076, "language_code": "pt", "depth": 10, "load_async_ai_overview": True, "tag": "tag"}))
        self.assertEqual(t("perplexity", "x", "tag", 2840, "en"), ("ai_optimization/perplexity/llm_responses/live",
                         {"user_prompt": "x", "model_name": "sonar-reasoning-pro", "web_search": True, "max_output_tokens": 4096,
                          "web_search_country_iso_code": "US"}))
        aio = rodada._ler("google_ai_overview", {"items": [{"type": "organic"}, {"type": "ai_overview", "markdown": "Use X.[[1]](https://a-com.example/b)",
                                                                                   "references": [{"url": "https://a-com.example/b"}],
                                                                                   "items": [{"references": [{"url": "https://c-org.example/"}]}]}]})
        self.assertEqual((aio["texto"], [f["domain"] for f in aio["fontes"]]), ("Use X.", ["a-com.example", "c-org.example"]))
        self.assertIsNone(rodada._ler("google_ai_overview", {"items": [{"type": "organic"}]})["markdown"])

    def test_pedido_errado(self):
        self.preparar()
        for nome, args in [("run_ai_visibility_check", {}), ("run_ai_visibility_check", {"maxCostUsd": 0}),
                           ("run_ai_visibility_check", {"maxCostUsd": 101}),
                           ("run_ai_visibility_check", {"maxCostUsd": 1, "promptIds": ["22222222-2222-4222-8222-222222222222"]}),
                           ("get_ai_visibility_run", {"runId": "x"}),
                           ("get_ai_visibility_run", {"runId": "22222222-2222-4222-8222-222222222222"}),
                           ("get_ai_visibility_results", {"runId": "22222222-2222-4222-8222-222222222222"}),
                           ("get_ai_visibility_results", {"includeHistory": True}),
                           ("get_ai_visibility_results", {"cursor": "all"}), ("get_ai_visibility_results", {"limit": 51}),
                           ("get_ai_visibility_results", {"branded": "x"}), ("get_ai_visibility_results", {"engines": ["bing"]}),
                           ("get_ai_visibility_answer", {"observationId": "22222222-2222-4222-8222-222222222222"}),
                           ("get_ai_visibility_answer", {})]:
            with self.assertRaises(ErroUso, msg=(nome, args)):
                self.x(nome, **args)
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("get_ai_visibility_run", {"runId": "22222222-2222-4222-8222-222222222222"})


ANALISE = {
    "get_ai_visibility_sources": {*RODADA["get_ai_visibility_results"], "groupBy", "ownership"},
    "get_ai_visibility_trend": {"projectId", "days"},
    "export_ai_visibility_data": {*RODADA["get_ai_visibility_results"], "format"},
    "explore_prompt": {"projectId", "prompt", "models", "highlightBrand", "webSearch", "webSearchCountryCode"},
}


class AnaliseTest(unittest.TestCase):
    setUp, tearDown, x = VisibilidadeIATest.setUp, VisibilidadeIATest.tearDown, VisibilidadeIATest.x
    preparar, post, rodar = RodadaTest.preparar, RodadaTest.post, RodadaTest.rodar

    def rodada_real(self):
        self.preparar()
        with mock.patch.object(dataforseo, "post", side_effect=self.post()):
            return self.rodar()

    def test_tools_list(self):
        for nome, props in ANALISE.items():
            f = mcp_proxy.FERRAMENTAS[nome]
            self.assertEqual(set(f["inputSchema"]["properties"]) - {"site"}, props, nome)
            self.assertEqual(f["description"].endswith(" Custa crédito do DataForSEO."), nome == "explore_prompt")
        p = {n: mcp_proxy.FERRAMENTAS[n]["inputSchema"]["properties"] for n in ANALISE}
        self.assertEqual((p["get_ai_visibility_sources"]["groupBy"]["default"], p["get_ai_visibility_sources"]["ownership"]["enum"]),
                         ("url", ["all", "own", "competitor", "other"]))
        self.assertEqual((p["get_ai_visibility_trend"]["days"]["enum"], p["get_ai_visibility_trend"]["days"]["default"]), ([7, 28, 90], 7))
        self.assertEqual(p["export_ai_visibility_data"]["format"], {"type": "string", "enum": ["json", "csv"], "default": "json"})
        e = p["explore_prompt"]
        self.assertEqual((e["prompt"]["maxLength"], e["models"]["default"], e["models"]["maxItems"], e["models"]["items"]["enum"]),
                         (500, ["chat_gpt"], 4, ["chat_gpt", "claude", "gemini", "perplexity"]))
        self.assertEqual((e["webSearch"]["default"], len(e["webSearchCountryCode"]["enum"]), e["highlightBrand"]["maxLength"]), (True, 249, 250))
        self.assertEqual(mcp_proxy.FERRAMENTAS["explore_prompt"]["inputSchema"]["required"], ["prompt"])

    def test_fontes(self):
        r = self.rodada_real()
        f = self.x("get_ai_visibility_sources", runId=r["id"])
        self.assertEqual((f["ok"], f["runId"], f["groupBy"], f["totalCount"], f["nextCursor"], f["truncated"]), (True, r["id"], "url", 3, None, False))
        self.assertEqual(f["coverage"], {"expected": 4, "completed": 3, "noAnswer": 1, "failed": 0, "pending": 0})
        self.assertEqual(f["appliedFilters"], {"competitorGap": False, "branded": "neutral", "ownership": "all"})
        linhas = {w["key"]: w for w in f["rows"]}
        self.assertEqual(set(linhas), {"https://site-e.exemplo.com/x", "https://www.site-e.exemplo.com/p", "https://vittude-com.example/a"})
        x = linhas["https://site-e.exemplo.com/x"]
        self.assertEqual((x["url"], x["domain"], x["title"], x["ownership"], x["answerCount"], x["promptCount"], x["engines"], x["truncated"]),
                         ("https://site-e.exemplo.com/x", "site-e.exemplo.com", "X", "own", 1, 1, [{"engine": "chatgpt", "answerCount": 1}], False))
        self.assertEqual(len(x["observationIds"]), 1)
        self.assertEqual(linhas["https://vittude-com.example/a"]["ownership"], "other")
        own = self.x("get_ai_visibility_sources", runId=r["id"], ownership="own", groupBy="domain")
        self.assertEqual([(w["key"], w["url"], w["title"]) for w in own["rows"]], [("site-e.exemplo.com", None, None), ("www.site-e.exemplo.com", None, None)])
        self.assertEqual(self.x("get_ai_visibility_sources", runId=r["id"], ownership="competitor")["totalCount"], 0)
        pag = self.x("get_ai_visibility_sources", runId=r["id"], limit=2)
        self.assertEqual((len(pag["rows"]), pag["nextCursor"]), (2, "2"))
        so_claude = self.x("get_ai_visibility_sources", runId=r["id"], engines=["claude"])
        self.assertEqual([w["key"] for w in so_claude["rows"]], ["https://vittude-com.example/a"])
        with self.assertRaises(ErroUso):
            self.x("get_ai_visibility_sources", runId=r["id"], groupBy="page")

    def test_exportar_csv_e_json(self):
        r = self.rodada_real()
        with self.assertRaisesRegex(ErroUso, "runId"):
            self.x("export_ai_visibility_data")  # sem rodada base ou agendada
        c = self.x("export_ai_visibility_data", runId=r["id"], format="csv")
        arq = pasta("visibilidade_ia", "psite15") / "exportacoes"
        self.assertEqual((c["ok"], c["format"], os.path.dirname(c["url"])), (True, "csv", str(arq)))
        self.assertTrue(c["url"].endswith(".csv"))
        self.assertGreater(c["expiresAt"], visibilidade_ia._iso(agora()))
        with open(c["url"], encoding="utf-8", newline="") as fh:
            texto = fh.read()
        self.assertEqual(texto, c["conteudo"])
        linhas = texto.split("\r\n")
        self.assertEqual(linhas[0], '"run_id","observation_id","prompt","engine","status","collected_at","brand_mentions","citation_urls","answer"')
        self.assertEqual(len(linhas), 5)
        import csv
        rows = list(csv.DictReader(texto.splitlines()))
        primeira = next(w for w in rows if w["prompt"] == "melhor psicólogo online" and w["engine"] == "chatgpt")
        self.assertEqual((primeira["run_id"], primeira["brand_mentions"], primeira["citation_urls"], primeira["collected_at"]),
                         (r["id"], "Cliente G; Zenklub", "https://site-e.exemplo.com/x", "2026-09-30T18:41:09.000Z"))
        self.assertEqual(primeira["answer"], "Cliente G lista psicólogos.  Veja também o Zenklub.")
        j = self.x("export_ai_visibility_data", runId=r["id"])
        self.assertTrue(j["url"].endswith(".json"))
        dados = json.loads(j["conteudo"])
        self.assertEqual(set(dados), {"exportedAt", "projectId", "run", "coverage", "answers"})
        self.assertEqual((dados["projectId"], dados["run"]["id"], len(dados["answers"])), ("psite15", r["id"], 4))
        self.assertEqual(json.loads(open(j["url"], encoding="utf-8").read()), dados)
        gap = json.loads(self.x("export_ai_visibility_data", runId=r["id"], competitorGap=True)["conteudo"])
        self.assertEqual(gap["answers"], [])
        with self.assertRaises(ErroUso):
            self.x("export_ai_visibility_data", runId=r["id"], format="xml")
        with self.assertRaises(ErroUso):
            self.x("export_ai_visibility_data", promptId=r["id"], includeHistory=True)

    def test_exportar_resposta_inteira_e_arquivo_grande(self):
        self.preparar()
        longo = "Cliente G " + "a" * 120_000
        with mock.patch.object(dataforseo, "post", side_effect=lambda c, t, timeout=None: raspagem(longo, []) if c == CHATGPT else modelo("x", [])):
            r = self.rodar()
        e = self.x("export_ai_visibility_data", runId=r["id"])
        self.assertIsNone(e["conteudo"])  # grande demais para o resultado: só o caminho
        dados = json.loads(open(e["url"], encoding="utf-8").read())
        self.assertIn(len(longo), [len(a["answerMarkdown"]) for a in dados["answers"]])

    # --- tendência: rodadas gravadas à mão ---

    def gravar_rodada(self, dias_atras, obs, status="completed", trigger="scheduled", loc=2076):
        rid = str(visibilidade_ia.uuid.uuid4())
        quando = visibilidade_ia._iso(agora() - dt.timedelta(days=dias_atras))
        d = pasta("visibilidade_ia", "psite15") / "rodadas"
        d.mkdir(exist_ok=True)
        (d / f"{rid}.json").write_text(json.dumps({
            "id": rid, "status": status, "trigger": trigger, "locationCode": loc, "languageCode": "pt", "createdAt": quando,
            "completedAt": quando, "observations": [{"id": str(visibilidade_ia.uuid.uuid4()), "runId": rid, **o} for o in obs]}))
        return rid

    @staticmethod
    def o(prompt, engine, mencionada=False, citada=False, status="completed", resposta=True, branded=False):
        marcas = [{"name": "Cliente G", "domain": "site-e.exemplo.com", "own": True, "mentioned": mencionada, "cited": citada, "firstMention": None},
                  {"name": "Zenklub", "domain": "", "own": False, "mentioned": True, "cited": False, "firstMention": None}]
        return {"promptId": prompt, "prompt": prompt, "topic": "General", "branded": branded, "engine": engine, "status": status,
                "answerMarkdown": "texto" if resposta and status == "completed" else None, "sources": [],
                "brands": marcas if status == "completed" else []}

    def test_tendencia_comparavel(self):
        o = self.o
        a = self.gravar_rodada(10, [o("p1", "chatgpt", True, True), o("p1", "claude", True), o("p2", "chatgpt", True, branded=True)])
        b = self.gravar_rodada(8, [o("p1", "chatgpt"), o("p1", "claude", True)])
        c = self.gravar_rodada(1, [o("p1", "chatgpt", True, True), o("p1", "claude")])
        self.gravar_rodada(0.5, [o("p1", "chatgpt")], status="running")  # em andamento não entra
        self.gravar_rodada(20, [o("p1", "chatgpt")])  # antes dos dois períodos
        t = self.x("get_ai_visibility_trend")
        self.assertEqual((t["ok"], t["days"], t["comparison"]), (True, 7, "comparable"))
        self.assertEqual(t["mentions"], {"current": 50.0, "previous": 75.0, "change": -25.0, "matchedCells": 2, "currentCells": 2, "previousCells": 2})
        self.assertEqual(t["citations"], {"current": 50.0, "previous": 25.0, "change": 25.0, "matchedCells": 2, "currentCells": 2, "previousCells": 2})
        self.assertEqual(t["current"]["coverage"], {"runs": 1, "expected": 2, "answered": 2, "noAnswer": 0, "failed": 0})
        self.assertEqual(t["previous"]["coverage"], {"runs": 2, "expected": 4, "answered": 4, "noAnswer": 0, "failed": 0})
        self.assertLess(t["previous"]["start"], t["previous"]["end"])
        self.assertEqual(t["previous"]["end"], t["current"]["start"])
        motores = {e["engine"]: e for e in t["engines"]}
        self.assertEqual(list(motores), ["chatgpt", "claude"])
        self.assertEqual((motores["chatgpt"]["mentions"]["current"], motores["chatgpt"]["mentions"]["previous"], motores["chatgpt"]["mentions"]["change"]),
                         (100.0, 50.0, 50.0))
        self.assertEqual([(p["runId"], p["mentionRate"], p["citationRate"]) for p in t["runs"]], [(a, 100.0, 50.0), (b, 50.0, 0.0), (c, 50.0, 50.0)])
        self.assertEqual(t["runs"][0]["coverage"], {"runs": 1, "expected": 2, "answered": 2, "noAnswer": 0, "failed": 0})

    def test_tendencia_incompleta_sem_anterior_sem_dados_e_escopo(self):
        o = self.o
        self.assertEqual(self.x("get_ai_visibility_trend")["comparison"], "no_data")
        self.gravar_rodada(2, [o("p1", "chatgpt", True), o("p1", "claude", status="failed"), o("p2", "chatgpt", resposta=False)])
        t = self.x("get_ai_visibility_trend", days=28)
        self.assertEqual((t["days"], t["comparison"], t["mentions"]["current"], t["mentions"]["change"]), (28, "no_previous", 100.0, None))
        self.assertEqual(t["current"]["coverage"], {"runs": 1, "expected": 3, "answered": 1, "noAnswer": 1, "failed": 1})
        self.gravar_rodada(40, [o("p1", "chatgpt"), o("p1", "claude"), o("p2", "chatgpt", True)])
        t = self.x("get_ai_visibility_trend", days=28)
        # 1 de 3 coletadas agora: abaixo de 90%, a taxa aparece mas a mudança não
        self.assertEqual((t["comparison"], t["mentions"]["current"], t["mentions"]["previous"], t["mentions"]["change"], t["mentions"]["matchedCells"]),
                         ("incomplete", 100.0, 0.0, None, 1))
        self.assertEqual(t["engines"][0]["mentions"]["change"], None)
        # outro mercado no período anterior: nenhuma célula em comum
        self.tmp.cleanup(); os.makedirs(self.tmp.name)
        self.gravar_rodada(2, [o("p1", "chatgpt", True)])
        self.gravar_rodada(10, [o("p1", "chatgpt", True)], loc=2840)
        self.assertEqual(self.x("get_ai_visibility_trend")["comparison"], "scope_changed")
        with self.assertRaises(ErroUso):
            self.x("get_ai_visibility_trend", days=30)

    # --- explore_prompt ---

    @staticmethod
    def llm(texto, notas, buscou=True, custo=0.01, modelo_nome="gpt-5.6-luna"):
        return [{"status_code": 20000, "cost": custo, "result": [{"model_name": modelo_nome, "web_search": buscou, "output_tokens": 812.4,
                                                                  "fan_out_queries": [f"q{i}" for i in range(25)],
                                                                  "items": [{"type": "message", "sections": [{"text": texto, "annotations": notas}]}]}]}]

    def test_explorar(self):
        notas = [{"url": "https://www.site-e.exemplo.com/a?utm_source=x", "title": "Diretório"}, {"url": "javascript:alert(1)"},
                 {"url": "https://www.site-e.exemplo.com/a?utm_source=x", "title": "dup"}, {"url": "https://vittude-com.example/", "title": None}]

        def post(caminho, tarefas, timeout=None):
            if "perplexity" in caminho:
                return [{"status_code": 40501, "cost": 0, "status_message": "Invalid Field"}]
            if "claude" in caminho:
                return self.llm("Procure o Cliente G.", [], custo=0.18, modelo_nome="claude-sonnet-5")
            return self.llm("Veja diretórios.", notas)

        args = {"prompt": "  melhor   psicólogo online ", "models": ["chat_gpt", "claude", "perplexity", "chat_gpt"], "webSearchCountryCode": "BR",
                "highlightBrand": "site-e"}
        with mock.patch.object(dataforseo, "post", side_effect=post) as p:
            r = self.x("explore_prompt", **args)
        corpos = {c[0][0]: c[0][1] for c in p.call_args_list}
        self.assertEqual(p.call_count, 3)
        self.assertEqual(corpos["ai_optimization/chat_gpt/llm_responses/live"], [{"user_prompt": "melhor   psicólogo online", "model_name": "gpt-5.6-luna",
                                                                                  "web_search": True, "max_output_tokens": 4096,
                                                                                  "web_search_country_iso_code": "BR"}])
        self.assertEqual(corpos["ai_optimization/claude/llm_responses/live"][0]["force_web_search"], True)
        self.assertEqual(corpos["ai_optimization/perplexity/llm_responses/live"][0]["model_name"], "sonar-reasoning-pro")
        self.assertEqual((r["ok"], r["prompt"], r["highlightBrand"], r["custo"]), (True, "melhor   psicólogo online", "site-e", 0.19))
        self.assertTrue(r["fetchedAt"])
        g, c, px = r["results"]
        self.assertEqual([x["model"] for x in r["results"]], ["chat_gpt", "claude", "perplexity"])
        self.assertEqual((g["status"], g["modelName"], g["text"], g["outputTokens"], g["webSearch"], g["webSearchCountryCode"], len(g["fanOutQueries"])),
                         ("success", "gpt-5.6-luna", "Veja diretórios.", 812, True, "BR", 20))
        self.assertEqual(g["citations"], [{"url": "https://www.site-e.exemplo.com/a?utm_source=x", "domain": "site-e.exemplo.com", "title": "Diretório", "matchedBrand": True},
                                          {"url": "https://vittude-com.example/", "domain": "vittude-com.example", "title": None, "matchedBrand": False}])
        self.assertEqual(g["brandMentioned"], True)  # citada, mesmo sem o nome no texto
        self.assertEqual((c["status"], c["brandMentioned"], c["citations"]), ("success", False, []))  # "site-e" não está no texto
        self.assertEqual((px["status"], px["errorCode"]), ("error", "UPSTREAM_ERROR"))
        # cache: os modelos que deram certo não custam de novo; o que falhou chama de novo
        with mock.patch.object(dataforseo, "post", side_effect=post) as p:
            r2 = self.x("explore_prompt", **{**args, "prompt": "melhor psicólogo online", "highlightBrand": "Cliente G"})
        self.assertEqual([c[0][0] for c in p.call_args_list], ["ai_optimization/perplexity/llm_responses/live"])
        self.assertEqual((r2["custo"], r2["results"][1]["brandMentioned"], r2["results"][0]["text"]), (0, True, "Veja diretórios."))
        self.assertTrue(list((pasta("visibilidade_ia", "psite15") / "cache").glob("*.json")))

    def test_explorar_repete_quando_nao_buscou_e_pais(self):
        respostas = [self.llm("de memória", [], buscou=False), self.llm("buscou", [{"url": "https://a-com.example/"}])]
        with mock.patch.object(dataforseo, "post", side_effect=respostas) as p:
            r = self.x("explore_prompt", prompt="x", models=["chat_gpt", "gemini"], webSearchCountryCode="BR")
        self.assertEqual(p.call_count, 2)  # o Gemini não aceita país: erro sem chamada paga
        self.assertEqual((r["results"][0]["text"], r["results"][0]["webSearch"], r["custo"]), ("buscou", True, 0.02))
        self.assertEqual((r["results"][1]["status"], r["results"][1]["errorCode"]), ("error", "UNSUPPORTED_COUNTRY"))
        # a repetição falha: fica a primeira resposta, já paga
        with mock.patch.object(dataforseo, "post", side_effect=[self.llm("de memória", [], buscou=False), RuntimeError("HTTP 500")]):
            r = self.x("explore_prompt", prompt="y", models=["perplexity"])
        self.assertEqual((r["results"][0]["status"], r["results"][0]["webSearch"], r["custo"]), ("success", False, 0.01))
        # sem busca: sem país no corpo, sem repetição; país fora da lista do Claude
        with mock.patch.object(dataforseo, "post", side_effect=[self.llm("z", [], buscou=False)]) as p:
            r = self.x("explore_prompt", prompt="z", models=["claude"], webSearch=False, webSearchCountryCode="BR")
        self.assertEqual(p.call_args[0], ("ai_optimization/claude/llm_responses/live", [{"user_prompt": "z", "model_name": "claude-sonnet-5",
                                                                                          "web_search": False, "max_output_tokens": 4096}]))
        self.assertEqual((p.call_count, r["results"][0]["webSearchCountryCode"]), (1, None))
        with mock.patch.object(dataforseo, "post") as p:
            r = self.x("explore_prompt", prompt="w", models=["claude"], webSearchCountryCode="BG")
        self.assertEqual((p.call_count, r["results"][0]["errorCode"], r["custo"]), (0, "UNSUPPORTED_COUNTRY", 0))

    def test_explorar_corpo_malformado_isola_o_modelo(self):
        ruim = self.llm("x", [], custo=0.03)
        ruim[0]["result"][0]["output_tokens"] = "muitos"
        def post(caminho, tarefas, timeout=None):
            return ruim if "claude" in caminho else self.llm("ok", [])
        with mock.patch.object(dataforseo, "post", side_effect=post):
            r = self.x("explore_prompt", prompt="p", models=["chat_gpt", "claude"])
        g, c = r["results"]
        self.assertEqual((g["status"], c["status"], c["errorCode"], r["custo"]), ("success", "error", "UPSTREAM_ERROR", 0.04))

    def test_pedido_errado(self):
        ruins = [("explore_prompt", {}), ("explore_prompt", {"prompt": "  "}), ("explore_prompt", {"prompt": "a" * 501}),
                 ("explore_prompt", {"prompt": "a", "models": []}), ("explore_prompt", {"prompt": "a", "models": ["chatgpt"]}),
                 ("explore_prompt", {"prompt": "a", "webSearchCountryCode": "XX"}), ("explore_prompt", {"prompt": "a", "webSearch": "sim"}),
                 ("explore_prompt", {"prompt": "a", "highlightBrand": " "}), ("explore_prompt", {"prompt": "a", "highlightBrand": "a" * 251}),
                 ("get_ai_visibility_trend", {"days": "7"}), ("get_ai_visibility_sources", {"ownership": "mine"}),
                 ("get_ai_visibility_sources", {"limit": 0}), ("export_ai_visibility_data", {"runId": "x"})]
        for nome, args in ruins:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                self.x(nome, **args)
        for nome in ANALISE:
            with self.assertRaises(ErroUso):
                mcp_proxy.executar(nome, {"prompt": "a"})


class AgendadosTest(unittest.TestCase):
    """Cron diário (python3 -m hub.mcp_grupos.visibilidade_ia rodar-agendados): roda os vencidos, com teto de US$ 1."""
    setUp, tearDown, x = VisibilidadeIATest.setUp, VisibilidadeIATest.tearDown, VisibilidadeIATest.x

    def preparar(self, site="psite15", prompts=("melhor psicólogo online",), engines=("chatgpt",), **tracker):
        mcp_proxy.executar("save_ai_visibility_tracker", {"site": site, "engines": list(engines),
                                                          "prompts": [{"text": t} for t in prompts]})
        arq = pasta("visibilidade_ia", site) / "tracker.json"
        e = json.loads(arq.read_text(encoding="utf-8"))
        e["tracker"].update(tracker)
        arq.write_text(json.dumps(e), encoding="utf-8")
        return arq

    def cron(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codigo = rodada.rodar_agendados()
        return codigo, out.getvalue()

    def tracker(self, site="psite15"):
        return json.loads((pasta("visibilidade_ia", site) / "tracker.json").read_text(encoding="utf-8"))["tracker"]

    def corridas(self, site="psite15"):
        with contextlib.redirect_stderr(io.StringIO()):  # rodada ilegível é pulada com uma linha no stderr
            return visibilidade_ia.rodadas({"id": site})

    def test_vencido_roda_e_avanca_da_ancora(self):
        ancora = (agora() - dt.timedelta(hours=2)).replace(microsecond=0)
        self.preparar(nextCheckAt=visibilidade_ia._iso(ancora), scheduleInterval="weekly", lastSkipReason="antigo")
        with mock.patch.object(dataforseo, "post", return_value=raspagem("Cliente G ajuda.", [])) as p:
            codigo, log = self.cron()
        self.assertEqual((codigo, p.call_count, p.call_args[0][0]), (0, 1, CHATGPT))
        [r] = self.corridas()
        self.assertEqual((r["trigger"], r["status"]), ("scheduled", "completed"))
        t = self.tracker()
        self.assertEqual(data_iso(t["nextCheckAt"]), ancora + dt.timedelta(days=7))
        self.assertIsNone(t["lastSkipReason"])
        self.assertIn("psite15: rodou", log)
        self.assertIn("0.0040", log)

    def test_base_pendente_roda_como_baseline_e_limpa(self):
        futuro = visibilidade_ia._iso(agora() + dt.timedelta(days=3))
        self.preparar(nextCheckAt=futuro, baselinePending=True)
        with mock.patch.object(dataforseo, "post", return_value=raspagem("Resposta.", [])):
            self.assertEqual(self.cron()[0], 0)
        [r] = self.corridas()
        self.assertEqual(r["trigger"], "baseline")
        t = self.tracker()
        self.assertNotIn("baselinePending", t)
        self.assertEqual(t["nextCheckAt"], futuro)  # a base não gasta o horário escolhido

    def test_base_que_falhou_continua_pendente(self):
        self.preparar(nextCheckAt=visibilidade_ia._iso(agora() + dt.timedelta(days=3)), baselinePending=True)
        with mock.patch.object(dataforseo, "post", side_effect=RuntimeError("DataForSEO HTTP 500")):
            self.assertEqual(self.cron()[0], 0)
        [r] = self.corridas()
        self.assertEqual((r["trigger"], r["status"]), ("baseline", "failed"))
        self.assertTrue(self.tracker()["baselinePending"])
        # ligar de novo a agenda não conta a base que falhou como feita
        t = self.tracker()
        t["baselinePending"] = False
        arq = pasta("visibilidade_ia", "psite15") / "tracker.json"
        e = json.loads(arq.read_text(encoding="utf-8"))
        e["tracker"] = t
        arq.write_text(json.dumps(e), encoding="utf-8")
        self.assertTrue(self.x("set_ai_visibility_schedule", enabled=True)["state"]["tracker"]["baselinePending"])

    def test_nao_vencido_e_pausado_ficam_intactos(self):
        a = self.preparar(nextCheckAt=visibilidade_ia._iso(agora() + dt.timedelta(days=1)))
        b = self.preparar("asite01", enabled=False, baselinePending=True, nextCheckAt=visibilidade_ia._iso(agora() - dt.timedelta(days=1)))
        antes = (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))
        codigo, log = self.cron()  # dataforseo.post sem mock: qualquer chamada estoura
        self.assertEqual(codigo, 0)
        self.assertEqual((a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")), antes)
        self.assertEqual(self.corridas() + self.corridas("asite01"), [])
        self.assertIn("psite15: não venceu", log)
        self.assertIn("asite01: pausado", log)

    def test_acima_do_teto_nao_roda_registra_e_avanca(self):
        ancora = (agora() - dt.timedelta(minutes=5)).replace(microsecond=0)
        # 6 prompts × Claude (US$ 0,18) = US$ 1,08, acima do teto de US$ 1
        self.preparar(prompts=[f"prompt {i}" for i in range(6)], engines=["claude"], scheduleInterval="daily",
                      nextCheckAt=visibilidade_ia._iso(ancora))
        codigo, log = self.cron()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.corridas(), [])
        t = self.tracker()
        self.assertIn("1.0800", t["lastSkipReason"])
        self.assertIn("teto", t["lastSkipReason"])
        self.assertTrue(t["lastSkipAt"])
        self.assertEqual(data_iso(t["nextCheckAt"]), ancora + dt.timedelta(days=1))
        self.assertIn("psite15: pulou", log)
        r = self.x("get_ai_visibility_tracker")
        self.assertIn("teto", r["tracker"]["lastSkipReason"])

    def test_rodada_em_andamento_pula_com_motivo(self):
        self.preparar(nextCheckAt=visibilidade_ia._iso(agora() - dt.timedelta(minutes=1)))
        p = pasta("visibilidade_ia", "psite15") / "rodadas"
        p.mkdir()
        (p / "viva.json").write_text(json.dumps({"id": "viva", "status": "running", "createdAt": visibilidade_ia._iso(agora()),
                                                  "observations": []}), encoding="utf-8")
        self.assertEqual(self.cron()[0], 0)
        self.assertIn("viva", self.tracker()["lastSkipReason"])

    def test_um_site_com_problema_nao_para_os_outros(self):
        (pasta("visibilidade_ia", "psite15") / "tracker.json").write_text("{quebrado", encoding="utf-8")
        (pasta("visibilidade_ia", "fantasma") / "tracker.json").write_text("{}", encoding="utf-8")  # sem cadastro
        self.preparar("asite01", nextCheckAt=visibilidade_ia._iso(agora() - dt.timedelta(hours=1)))
        p = pasta("visibilidade_ia", "asite01") / "rodadas"
        p.mkdir()
        (p / "ruim.json").write_text("nem json", encoding="utf-8")
        with mock.patch.object(dataforseo, "post", return_value=raspagem("Resposta.", [])):
            codigo, log = self.cron()
        self.assertEqual(codigo, 1)  # tracker ilegível vai para o resumo das rotinas que falharam
        self.assertIn("psite15: tracker.json ilegível", log)
        self.assertIn("fantasma:", log)
        self.assertEqual([r["trigger"] for r in self.corridas("asite01")], ["scheduled"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            r = mcp_proxy.executar("get_ai_visibility_tracker", {"site": "asite01"})  # a rodada corrompida é pulada na leitura
        self.assertIn("ruim.json", err.getvalue())
        self.assertEqual(len(r["recentRuns"]), 1)

    def test_proxima_a_partir_da_ancora(self):
        u = lambda *a: dt.datetime(*a, tzinfo=dt.timezone.utc)
        casos = [("daily", u(2026, 10, 1, 5, 50), u(2026, 10, 9, 12), u(2026, 10, 10, 5, 50)),
                 ("daily", u(2026, 10, 9, 5, 50), u(2026, 10, 9, 5, 50), u(2026, 10, 10, 5, 50)),  # estritamente no futuro
                 ("weekly", u(2026, 10, 1, 5, 50), u(2026, 10, 9, 12), u(2026, 10, 15, 5, 50)),
                 ("weekly", u(2026, 10, 12, 5), u(2026, 10, 9, 12), u(2026, 10, 19, 5)),
                 ("monthly", u(2026, 8, 31, 5, 50), u(2026, 10, 9), u(2026, 10, 31, 5, 50)),
                 ("monthly", u(2026, 9, 1, 2), u(2026, 9, 1, 3), u(2026, 10, 1, 2)),  # dia 1 = fim do mês +1 dia
                 ("monthly", u(2026, 9, 29, 6), u(2026, 9, 29, 7), u(2026, 10, 30, 6)),  # penúltimo dia = fim do mês -1 dia
                 ("monthly", u(2026, 12, 31, 5), u(2027, 1, 1), u(2027, 1, 31, 5)),
                 ("monthly", u(2027, 1, 31, 5), u(2027, 2, 1), u(2027, 2, 28, 5))]
        for intervalo, ancora, quando, esperado in casos:
            with self.subTest(intervalo=intervalo, ancora=ancora):
                self.assertEqual(visibilidade_ia.proxima(quando, intervalo, anterior=ancora), esperado)


def SITE():
    return sites.site("psite15")


if __name__ == "__main__":
    unittest.main()
