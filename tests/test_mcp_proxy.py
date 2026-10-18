import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hub import dataforseo, google, mcp_proxy

CONEXAO = http.client.HTTPConnection  # a do teste; a do módulo fica proibida (nada sai para a 4182)
CHAVE = "Q0hBVkUtU0VDUkVUQS1ERlM="
TABELA = ["list_projects", "get_project_context", "get_search_console_performance", "inspect_urls", "get_search_opportunities",
          *google.FERRAMENTAS_GA4, "get_clarity_insights", "get_rank_tracker", "get_site_audit", "get_hub_opportunities",
          "get_ai_overview", "get_bi_metrics", "get_keyword_metrics", "get_serp_results", "get_domain_overview",
          "get_ranked_keywords", "find_serp_competitors"]
TABELA += ["save_ai_visibility_tracker", "get_ai_visibility_tracker", "set_ai_visibility_schedule", "estimate_ai_visibility_cost"]
TABELA += ["research_ai_visibility_prompts", "generate_ai_visibility_prompts", "complete_ai_research_setup"]
TABELA += ["run_ai_visibility_check", "get_ai_visibility_run", "get_ai_visibility_results", "get_ai_visibility_answer"]
TABELA += ["get_ai_visibility_sources", "get_ai_visibility_trend", "export_ai_visibility_data", "explore_prompt"]


def _grava(arq, conteudo):
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False), encoding="utf-8")


def fixtures(base):
    _grava(base / "snapshots/2026-10-08/psite13-clarity.json", [{"metricName": "Antiga"}])
    _grava(base / "snapshots/2026-10-09/psite13-clarity.json", [{"metricName": "DeadClickCount", "information": [{"sessionsCount": 44}]}])
    _grava(base / "verificador/psite13/latest.json", {"site": "psite13", "data": "2026-10-08", "itens": [{"n": 1, "ok": True}], "pioras": []})
    _grava(base / "verificador/psite13/latest.md", "# Auditoria\n" + "x" * 30000)
    _grava(base / "gsc/psite13/latest.json", {"site": "psite13", "data": "2026-10-08", "periodo": "p", "oportunidades": [
        {"tipo": "vazamento", "busca": "a"}, {"tipo": "buraco", "busca": "b"}, {"tipo": "vazamento", "busca": "c"}]})
    busca = base / "aio/psite13/psicologo-brasileiro-online"
    _grava(busca / "2026-10-06.json", {"data": "2026-10-06", "keyword": "velha"})
    _grava(busca / "2026-10-07.json", {"data": "2026-10-07", "keyword": "psicólogo brasileiro online", "local": "brasil",
                                       "tem_ai_overview": True, "citado": ["psite13-com.example"],
                                       "fontes": [{"domain": "psite13-com.example", "url": "https://psite13-com.example/x"}],
                                       "organico": [{"rank": 1, "domain": "outro-com.example", "url": "https://outro-com.example/"},
                                                    {"rank": 4, "domain": "www.psite13-com.example", "url": "https://www.psite13-com.example/"}]})
    _grava(base / "aio/psite13/painel/history.jsonl", "{}\n")
    _grava(base / "aio/psite13/ciclo-2026-10-01.md", "velho")
    _grava(base / "aio/psite13/ciclo-2026-10-08.md", "análise nova")
    _grava(base / "ranking/psite13/2026-10-04.json", {"site": "psite13", "dia": "2026-10-04", "fonte": "dataforseo", "local": 2076,
                                                       "dispositivo": "mobile", "profundidade": 40,
                                                       "itens": [{"palavra": "cliente g", "posicao": 3, "url": "https://psite13-com.example/"}]})
    (base / "bi").mkdir()
    c = sqlite3.connect(base / "bi/metricas.db")
    c.execute("create table fato(dia text, negocio text, fonte text, metrica text, dim text default '', valor real, "
              "primary key (dia, negocio, fonte, metrica, dim))")
    c.executemany("insert into fato values (?, ?, ?, ?, ?, ?)", [
        ("2026-10-01", "psite13", "ga4", "sessoes", "", 10), ("2026-10-03", "psite13", "ga4", "sessoes", "", 5),
        ("2026-10-01", "psite13", "ga4", "sessoes", "organic", 7), ("2026-10-03", "psite13", "ga4", "sessoes", "organic", 1),
        ("2026-10-02", "psite13", "gsc", "cliques", "", 3), ("2026-10-02", "asite01", "ga4", "sessoes", "", 99)])
    c.commit()
    c.close()


class McpTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        fixtures(self.base)
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": str(self.base), "DATAFORSEO_AUTH": CHAVE}),
                  mock.patch.object(http.client, "HTTPConnection", side_effect=AssertionError("conexão de saída proibida")),
                  mock.patch.object(dataforseo, "post", side_effect=AssertionError("DataForSEO sem mock")),
                  mock.patch.object(google, "conta_servico", return_value={"client_email": "user11@example.com", "private_key": "PK"})):
            p.start()
            self.addCleanup(p.stop)
        self.srv = mcp_proxy.criar_servidor("127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.respostas = []

    def req(self, metodo, caminho, corpo=None, headers=None):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            c.request(metodo, caminho, body=corpo, headers=headers or {})
            r = c.getresponse()
            st, h, b = r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()
        self.respostas.append(b)
        return st, h, b

    def rpc(self, metodo, params=None, accept="application/json, text/event-stream", **extra):
        corpo = json.dumps({"jsonrpc": "2.0", "id": 3, "method": metodo, **({"params": params} if params is not None else {}), **extra})
        return self.req("POST", "/mcp", corpo, {"Content-Type": "application/json", "Accept": accept})

    def chama(self, nome, **args):
        st, _, corpo = self.rpc("tools/call", {"name": nome, "arguments": args})
        self.assertEqual(st, 200)
        r = json.loads(corpo)["result"]
        if not r.get("isError"):
            self.assertEqual(json.loads(r["content"][0]["text"]), r["structuredContent"])
        return r

    def dados(self, nome, **args):
        r = self.chama(nome, **args)
        self.assertNotIn("isError", r)
        return r["structuredContent"]

    def tearDown(self):
        for b in self.respostas:  # (k) a chave nunca aparece
            self.assertNotIn(CHAVE.encode(), b)

    def test_a_initialize_e_tools_list(self):
        r = json.loads(self.rpc("initialize", {"protocolVersion": "2025-03-26"})[2])["result"]
        self.assertEqual(r["protocolVersion"], "2025-03-26")
        self.assertEqual(r["serverInfo"], {"name": "nimbus-hub", "version": "2"})
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("list_projects", r["instructions"])
        self.assertEqual(json.loads(self.rpc("initialize", {"protocolVersion": "1999-01-01"})[2])["result"]["protocolVersion"], "2025-06-18")
        tools = json.loads(self.rpc("tools/list")[2])["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), sorted(set(TABELA) | set(mcp_proxy.GRUPOS)))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
        caro = {t["name"]: t["description"] for t in tools}
        self.assertIn("DataForSEO", caro["get_keyword_metrics"])
        self.assertEqual(json.loads(self.rpc("ping")[2])["result"], {})

    def test_navegador_e_rebinding_recusados(self):
        corpo = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        for h in ({"Origin": "https://evil.example"}, {"Host": "evil.example:4181"}):
            self.assertEqual(self.req("POST", "/mcp", corpo, {"Content-Type": "text/plain", **h})[0], 403)
            self.assertEqual(self.req("GET", "/api/health", None, h)[0], 403)
        self.assertEqual(self.req("POST", "/mcp", corpo, {"Host": "localhost:4181"})[0], 200)

    def test_jsonrpc_erros(self):
        self.assertEqual(json.loads(self.rpc("resources/list")[2])["error"]["code"], -32601)
        st, _, b = self.req("POST", "/mcp", b"{nao json", {"Content-Type": "application/json"})
        self.assertEqual(json.loads(b)["error"]["code"], -32700)
        for corpo in (b"[]", b"3"):
            self.assertEqual(json.loads(self.req("POST", "/mcp", corpo)[2])["error"]["code"], -32600)

    def test_b_site_por_id_dominio_e_nome(self):
        with mock.patch.object(google, "desempenho_gsc", return_value={"ok": True, "rows": [1]}) as m:
            for args in ({"site": "psite13"}, {"projectId": "psite13-com.example"}, {"site": "Cliente G"},
                         {"domain": "https://www.psite13-com.example/blog"}, {"site": "PSITE13"}):
                self.assertEqual(self.dados("get_search_console_performance", dateRange="last_7_days", **args), {"ok": True, "rows": [1]})
                self.assertEqual(m.call_args[0][0], "sc-domain:psite13-com.example")
                self.assertEqual(m.call_args[0][1]["dateRange"], "last_7_days")

    def test_ga4_oportunidades_inspecao_e_sem_acesso(self):
        with mock.patch.object(google, "relatorio_ga4", return_value={"x": 1}) as g:
            self.dados("get_google_analytics_site_search", site="psite13")
        self.assertEqual(g.call_args[0][0], "100000006")
        self.assertEqual(g.call_args[0][2], "get_google_analytics_site_search")
        with mock.patch.object(google, "oportunidades_busca", return_value={"o": 1}) as o:
            self.dados("get_search_opportunities", site="psite13")
        self.assertEqual(o.call_args[0][:2], ("sc-domain:psite13-com.example", "100000006"))
        with mock.patch.object(google, "desempenho_gsc", side_effect=google.SemAcesso("sc-domain:x", "user11@example.com")):
            d = self.dados("get_search_console_performance", site="psite13")
        self.assertEqual(d["reason"], "hub_sem_acesso")
        self.assertIn("user11@example.com", d["comoResolver"])
        self.assertTrue(d["dadosDoHub"].endswith("gsc/psite13/latest.md"))
        with mock.patch.object(google, "desempenho_gsc", side_effect=google.ErroCredencial("PK secreta")):
            r = self.chama("get_search_console_performance", site="psite13")
        self.assertTrue(r["isError"])
        self.assertIn("credencial do Google", r["content"][0]["text"])
        self.assertNotIn("secreta", json.dumps(r))

    def test_c_site_desconhecido_e_uuid(self):
        d = self.dados("get_site_audit", site="nao-existe")
        self.assertEqual((d["ok"], d["reason"]), (False, "site_desconhecido"))
        self.assertIn("psite13", d["sitesDoHub"])
        self.assertIn("psite13", d["comoResolver"])
        self.assertNotIn("OpenSEO", d["comoResolver"])
        d = self.dados("get_search_console_performance", projectId="e8bb077e-a115-4cd0-acc3-04b5add8df27")
        self.assertEqual(d["reason"], "site_desconhecido")
        self.assertIn("ids de projeto do OpenSEO não valem mais", d["comoResolver"])
        r = self.chama("get_site_audit")
        self.assertTrue(r["isError"])
        self.assertIn("site", r["content"][0]["text"])

    def test_d_ferramenta_desconhecida(self):
        r = self.chama("nao_existe")
        self.assertTrue(r["isError"])
        t = r["content"][0]["text"]
        self.assertTrue(t.startswith('Ferramenta "nao_existe" não existe no hub de SEO do Ana (o OpenSEO foi desligado). Use: '))
        self.assertIn("get_site_audit", t)

    def test_list_projects_e_contexto(self):
        ps = {p["id"]: p for p in self.dados("list_projects")["projects"]}
        self.assertEqual(ps["psite13"]["dominio"], "psite13-com.example")
        self.assertEqual(ps["psite13"]["fontes"], {"gsc": True, "ga4": True, "clarity": True, "ranking": True})
        c = self.dados("get_project_context", site="psite13")
        self.assertEqual((c["id"], c["conselho"]), ("psite13", "CFP"))
        self.assertIn("Zenklub", c["concorrentes"])
        self.assertIn("palavras 1 psite13", c["ranking"]["palavras"])
        self.assertEqual(c["ids"]["gsc"], "sc-domain:psite13-com.example")
        self.assertNotIn("openseo", c["ids"])
        self.assertTrue(c["dados"]["gsc"].endswith("gsc/psite13/latest.md"))

    def test_e_leitura_dos_arquivos_do_hub(self):
        d = self.dados("get_clarity_insights", site="psite13")
        self.assertEqual((d["ok"], d["coletadoEm"], d["janelaDias"]), (True, "2026-10-09", 3))
        self.assertEqual(d["metricas"][0]["metricName"], "DeadClickCount")
        self.assertEqual(self.dados("get_clarity_insights", site="asite01")["reason"], "sem_clarity")
        d = self.dados("get_site_audit", site="psite13")
        self.assertEqual((d["ok"], d["data"], d["itens"][0]["n"]), (True, "2026-10-08", 1))
        self.assertEqual(len(d["relatorio"]), 20000)
        self.assertEqual(self.dados("get_site_audit", site="asite01")["reason"], "sem_auditoria")
        d = self.dados("get_hub_opportunities", site="psite13", tipo="vazamento", limite=1)
        self.assertEqual([o["busca"] for o in d["oportunidades"]], ["a"])
        self.assertEqual(d["total"], 2)
        self.assertEqual(len(self.dados("get_hub_opportunities", site="psite13")["oportunidades"]), 3)
        d = self.dados("get_ai_overview", site="psite13")
        self.assertEqual(d["analise"], "análise nova")
        self.assertEqual(len(d["buscas"]), 1)
        b = d["buscas"][0]
        self.assertEqual((b["data"], b["busca"], b["temAiOverview"], b["citado"], b["posicaoOrganica"]),
                         ("2026-10-07", "psicólogo brasileiro online", True, True, 4))
        self.assertEqual(b["fontes"], ["https://psite13-com.example/x"])
        self.assertEqual(self.dados("get_ai_overview", site="asite01")["reason"], "sem_aio")
        d = self.dados("get_rank_tracker", site="psite13")
        self.assertEqual((d["ok"], d["palavras"][0]["posicao"]), (True, 3))
        self.assertEqual(self.dados("get_rank_tracker", site="asite01")["reason"], "sem_medicao")

    def test_f_bi(self):
        d = self.dados("get_bi_metrics", site="psite13")
        self.assertEqual(d["metricas"], [
            {"fonte": "ga4", "metrica": "sessoes", "primeiroDia": "2026-10-01", "ultimoDia": "2026-10-03", "dias": 2},
            {"fonte": "gsc", "metrica": "cliques", "primeiroDia": "2026-10-02", "ultimoDia": "2026-10-02", "dias": 1}])
        d = self.dados("get_bi_metrics", site="psite13", fonte="ga4", metrica="sessoes", de="2026-10-01", ate="2026-10-03")
        self.assertEqual(d["serie"], [{"dia": "2026-10-01", "valor": 10}, {"dia": "2026-10-02", "valor": None},
                                      {"dia": "2026-10-03", "valor": 5}])
        self.assertEqual((d["soma"], d["diasComDado"]), (15, 2))
        self.assertEqual(d["dims"], [{"dim": "organic", "valor": 8}])
        d = self.dados("get_bi_metrics", site="psite13", fonte="ga4", metrica="sessoes", de="2026-10-01", ate="2026-10-02", dim="organic")
        self.assertEqual(d["serie"], [{"dia": "2026-10-01", "valor": 7}, {"dia": "2026-10-02", "valor": None}])
        self.assertNotIn("dims", d)
        self.assertTrue(self.chama("get_bi_metrics", site="psite13", fonte="ga4", metrica="sessoes", de="ontem")["isError"])
        (self.base / "bi/metricas.db").unlink()
        self.assertEqual(self.dados("get_bi_metrics", site="psite13")["reason"], "sem_armazem")

    def test_g_dataforseo(self):
        tarefa = {"status_code": 20000, "status_message": "Ok.", "cost": 0.0101, "result": [{"items": [
            {"keyword": "psicólogo online", "keyword_info": {"search_volume": 900, "cpc": 1.5, "competition": 0.3},
             "keyword_properties": {"keyword_difficulty": 22}, "search_intent_info": {"main_intent": "commercial"}}]}]}
        with mock.patch.object(dataforseo, "post", return_value=[tarefa]) as p:
            d = self.dados("get_keyword_metrics", keywords=["psicólogo online"])
        self.assertEqual(p.call_args[0][0], "dataforseo_labs/google/keyword_overview/live")
        self.assertEqual(p.call_args[0][1], [{"keywords": ["psicólogo online"], "location_code": 2076, "language_code": "pt"}])
        self.assertEqual(d["itens"], [{"keyword": "psicólogo online", "search_volume": 900, "cpc": 1.5, "competition": 0.3,
                                       "keyword_difficulty": 22, "search_intent": "commercial"}])
        self.assertEqual((d["ok"], d["custo"]), (True, 0.0101))
        serp = {"status_code": 20000, "cost": 0.002, "result": [{"items": [
            {"type": "organic", "rank_group": 1, "domain": "a-com.example", "url": "https://a-com.example/", "title": "A", "extra": 1}]}]}
        with mock.patch.object(dataforseo, "post", return_value=[serp]) as p:
            d = self.dados("get_serp_results", keyword="x", depth=500)
        self.assertEqual(p.call_args[0][1][0]["depth"], 100)
        self.assertEqual(d["itens"], [{"type": "organic", "rank_group": 1, "domain": "a-com.example", "url": "https://a-com.example/", "title": "A"}])
        with mock.patch.object(dataforseo, "post", return_value=[{"status_code": 20000, "cost": 0.01, "result": [{"items": [
                {"keyword_data": {"keyword": "k", "keyword_info": {"search_volume": 10, "cpc": None}},
                 "ranked_serp_element": {"serp_item": {"rank_group": 2, "url": "https://psite13-com.example/"}}}]}]}]) as p:
            d = self.dados("get_ranked_keywords", site="psite13")
        self.assertEqual(p.call_args[0][1][0]["target"], "psite13-com.example")
        self.assertEqual(d["itens"], [{"keyword": "k", "rank_group": 2, "url": "https://psite13-com.example/", "search_volume": 10, "cpc": None}])
        with mock.patch.object(dataforseo, "post", return_value=[{"status_code": 20000, "cost": 0.02, "result": [{"items": [
                {"se_type": "google", "metrics": {"organic": {"etv": 12.5, "count": 7, "pos_1": 1, "pos_2_3": 2, "impressions_etv": 9},
                                                  "paid": {"etv": 0, "count": 0}}}]}]}]):
            d = self.dados("get_domain_overview", target="psite13-com.example")
        self.assertEqual(d["organico"], {"etv": 12.5, "count": 7, "pos_1": 1, "pos_2_3": 2})
        self.assertIsNone(d["pago"])
        with mock.patch.object(dataforseo, "post", return_value=[{"status_code": 20000, "cost": 0.01, "result": [{"items": [
                {"domain": "b-com.example", "avg_position": 3.2, "etv": 40, "keywords_count": 2, "rating": 9}]}]}]):
            d = self.dados("find_serp_competitors", keywords=["a", "b"])
        self.assertEqual(d["itens"], [{"domain": "b-com.example", "avg_position": 3.2, "etv": 40, "keywords_count": 2}])
        self.assertTrue(self.chama("find_serp_competitors", keywords=[str(i) for i in range(21)])["isError"])
        self.assertTrue(self.chama("get_domain_overview")["isError"])  # sem target nem site

    def test_dataforseo_alvo_por_target_domain_e_site(self):
        ok = [{"status_code": 20000, "cost": 0, "result": [{"items": []}]}]
        casos = [({"site": "psite13", "domain": "zenklub-com-br.example"}, "zenklub-com-br.example"), ({"domain": "zenklub-com-br.example"}, "zenklub-com-br.example"),
                 ({"site": "psite13", "target": "a-com.example", "domain": "b-com.example"}, "a-com.example"), ({"site": "psite13"}, "psite13-com.example")]
        for args, esperado in casos:
            with mock.patch.object(dataforseo, "post", return_value=ok) as p:
                self.assertTrue(self.dados("get_ranked_keywords", **args)["ok"])
            self.assertEqual(p.call_args[0][1][0]["target"], esperado)
        self.assertEqual(self.dados("get_domain_overview", site="nao-existe", target="a-com.example")["reason"], "site_desconhecido")

    def test_site_com_barra_nao_vira_host(self):
        for v in ("asite01/../fsite05", "fsite05/qualquer"):
            self.assertEqual(self.dados("get_project_context", site=v)["reason"], "site_desconhecido")
        self.assertEqual(self.dados("get_project_context", site="https://www.psite13-com.example/blog")["id"], "psite13")

    def test_sem_permissao_e_bi_sem_fato(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            d = self.dados("get_clarity_insights", site="psite13")
        self.assertEqual((d["ok"], d["reason"]), (False, "sem_permissao"))
        self.assertIn("Ana", d["comoResolver"])
        arq = self.base / "bi/metricas.db"
        arq.unlink()
        sqlite3.connect(arq).close()  # banco sem a tabela fato
        self.assertEqual(self.dados("get_bi_metrics", site="psite13")["reason"], "sem_armazem")

    def test_dataforseo_erro_sem_chave(self):
        with mock.patch.object(dataforseo, "post", side_effect=RuntimeError("DataForSEO 40100: não autorizado")):
            d = self.dados("get_serp_results", keyword="x")
        self.assertEqual((d["ok"], d["reason"]), (False, "dataforseo_erro"))
        self.assertIn("40100", d["message"])
        with mock.patch.object(dataforseo, "post", return_value=[{"status_code": 40501, "status_message": "Invalid Field", "cost": 0}]):
            d = self.dados("get_keyword_metrics", keywords=["a"])
        self.assertEqual(d["reason"], "dataforseo_erro")
        self.assertIn("Invalid Field", d["message"])

    def test_custo_registra_a_ferramenta_o_site_e_quem_chamou(self):
        vistos = []

        def post(caminho, tarefas, timeout=120):
            vistos.append(dict(dataforseo._contexto()))
            return [{"status_code": 20000, "cost": 0.002, "result": [{"items": []}]}]
        with mock.patch.object(dataforseo, "post", side_effect=post):
            corpo = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "get_serp_results", "arguments": {"keyword": "x", "site": "psite13"}}})
            self.req("POST", "/mcp", corpo, {"Content-Type": "application/json", "Accept": "application/json",
                                             "X-Origem": "seo-local<script>"})
        self.assertEqual([(v["origem"], v["site"], v["cliente"]) for v in vistos],
                         [("mcp:get_serp_results", "psite13", "seo-localscript")])

    def test_h_sse_so_quando_pede_so_sse(self):
        _, h, corpo = self.rpc("ping", accept="text/event-stream")
        self.assertEqual(h["Content-Type"], "text/event-stream")
        self.assertTrue(corpo.startswith(b"event: message\ndata: {"))
        self.assertEqual(json.loads(corpo.split(b"data: ")[1])["result"], {})
        self.assertEqual(self.rpc("ping")[1]["Content-Type"], "application/json")

    def test_i_get(self):
        st, _, b = self.req("GET", "/api/health")
        self.assertEqual((st, json.loads(b)), (200, {"ok": True}))
        st, h, _ = self.req("GET", "/mcp")
        self.assertEqual((st, h["Allow"]), (405, "POST"))
        st, _, b = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"/mcp", b)
        self.assertEqual(self.req("GET", "/api/x")[0], 404)
        self.assertEqual(self.req("POST", "/outro", b"{}")[0], 404)

    def test_j_notificacao_202(self):
        corpo = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        st, _, b = self.req("POST", "/mcp", corpo, {"Content-Type": "application/json"})
        self.assertEqual((st, b), (202, b""))

    def test_content_length_negativo_nao_trava(self):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=5)
        self.addCleanup(c.close)
        c.putrequest("POST", "/mcp")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)

    def test_log_sem_argumentos(self):
        with mock.patch.object(mcp_proxy, "_log") as lg:
            self.chama("get_site_audit", site="psite13", segredo="nao-logar")
            for _ in range(100):  # o log sai depois da resposta, na thread do servidor
                if lg.call_args:
                    break
                time.sleep(0.02)
        linha = lg.call_args[0][0]
        self.assertRegex(linha, r"^tool=get_site_audit site=psite13 resultado=ok ms=\d+$")
        self.assertNotIn("nao-logar", linha)

    def test_l_excecao_vira_isError_sem_traceback(self):
        with mock.patch.object(mcp_proxy.ranking, "ler", side_effect=ValueError("detalhe interno")):
            r = self.chama("get_rank_tracker", site="psite13")
        self.assertTrue(r["isError"])
        self.assertNotIn("detalhe interno", json.dumps(r))


if __name__ == "__main__":
    unittest.main()
