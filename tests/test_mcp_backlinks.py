import os, tempfile, unittest
from unittest import mock

from hub import dataforseo, mcp_grupos, mcp_proxy
from hub.mcp_grupos import backlinks

SPAM = [["backlink_spam_score", "<", 40], "or", ["backlink_spam_score", "=", None]]


def tarefa(result, custo=0.02):
    return {"status_code": 20000, "cost": custo, "result": result}


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "DATAFORSEO_AUTH": "x"})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def chama(self, nome, respostas, **args):
        with mock.patch.object(dataforseo, "post", side_effect=[[r] for r in respostas]) as p:
            r = mcp_proxy.executar(nome, {"site": "psite15", **args})
        return r, [(c[0][0], c[0][1][0]) for c in p.call_args_list]

    def test_tools_list_tem_as_propriedades_do_original(self):
        o = mcp_proxy.FERRAMENTAS["get_backlinks_overview"]
        self.assertTrue(o["description"].endswith(" Custa crédito do DataForSEO."))
        self.assertTrue({"projectId", "target", "scope", "hideSpam"} <= set(o["inputSchema"]["properties"]))
        p = mcp_proxy.FERRAMENTAS["get_backlinks_profile"]["inputSchema"]["properties"]
        self.assertTrue({"projectId", "target", "scope", "page", "pageSize", "sortField", "sortOrder", "filters", "mode", "hideSpam"} <= set(p))
        self.assertEqual(p["pageSize"]["enum"], [50, 100, 200])
        self.assertEqual(p["mode"]["default"], "one_per_domain")
        self.assertEqual(p["sortField"]["enum"], ["rank", "domainRank", "spamScore", "firstSeen"])

    def test_overview_domain_chama_tres_endpoints_e_soma_custo(self):
        resumo = tarefa([{"rank": 12, "backlinks": 10, "referring_domains": 3, "referring_pages": 7, "info": {"target_spam_score": 2}, "new_reffering_domains": 1}], 0.02)
        hist = tarefa([{"items": [{"date": "2026-09-01 00:00:00 +00:00", "backlinks": 9, "referring_domains": 3, "rank": 11, "new_backlinks": 2}]}], 0.03)
        doms = tarefa([{"total_count": 1, "items": [{"domain": "a-com.example", "backlinks": 4, "referring_pages": 2, "rank": 50, "backlinks_spam_score": 1}]}], 0.04)
        r, chamadas = self.chama("get_backlinks_overview", [resumo, hist, doms], scope="domain")
        self.assertEqual([c[0] for c in chamadas], ["backlinks/summary/live", "backlinks/history/live", "backlinks/referring_domains/live"])
        c = chamadas[0][1]
        self.assertEqual((c["target"], c["include_subdomains"], c["backlinks_status_type"], c["rank_scale"]), ("site-e.exemplo.com", False, "live", "one_hundred"))
        self.assertEqual(set(chamadas[1][1]), {"target", "date_from", "date_to", "rank_scale"})
        d = chamadas[2][1]
        self.assertEqual((d["limit"], d["offset"], d["order_by"], d["filters"]), (100, 0, ["backlinks,desc"], [["backlinks_spam_score", "<=", 40]]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["custo"], 0.09)
        self.assertEqual((r["target"], r["scope"]), ("site-e.exemplo.com", "domain"))
        self.assertIn("scopeNote", r)
        s = r["overview"]["overview"]["summary"]
        self.assertEqual((s["rank"], s["backlinks"], s["referringDomains"], s["referringPages"], s["targetSpamScore"], s["newReferringDomains"]), (12, 10, 3, 7, 2, 1))
        self.assertEqual(r["overview"]["overview"]["trends"], [{"date": "2026-09-01", "backlinks": 9, "referringDomains": 3, "rank": 11}])
        self.assertEqual(r["overview"]["overview"]["newLostTrends"][0]["newBacklinks"], 2)
        self.assertEqual(r["referringDomains"]["rows"][0]["domain"], "a-com.example")
        self.assertEqual((r["referringDomains"]["totalCount"], r["referringDomains"]["hasMore"]), (1, False))

    def test_overview_exact_url_sem_historico_e_subfolder_por_totais(self):
        r, ch = self.chama("get_backlinks_overview", [tarefa([{"backlinks": 1}]), tarefa([{"items": []}])], target="http://x.com/pg/", scope="page")
        self.assertEqual([c[0] for c in ch], ["backlinks/summary/live", "backlinks/referring_domains/live"])
        self.assertEqual(ch[0][1]["target"], "http://x.com/pg")
        r, ch = self.chama("get_backlinks_overview", [tarefa([{"total_count": 30}], 0.01), tarefa([{"total_count": 8}], 0.01)], target="x.com/blog")
        self.assertEqual([(c[0], c[1]["mode"], c[1]["limit"], c[1]["order_by"], "offset" in c[1]) for c in ch],
                         [("backlinks/backlinks/live", "as_is", 1, ["rank,desc"], False), ("backlinks/backlinks/live", "one_per_domain", 1, ["rank,desc"], False)])
        self.assertNotIn(SPAM, ch[0][1]["filters"])
        self.assertEqual(ch[0][1]["filters"][0][1:3], ["or", ["url_to", "like", "%://x.com/blog/%"]])
        self.assertEqual((r["scope"], r["overview"]["overview"]["summary"]["backlinks"], r["overview"]["overview"]["summary"]["referringDomains"], r["custo"]), ("subfolder", 30, 8, 0.02))
        self.assertNotIn("referringDomains", r)

    def test_overview_top_pages_chama_domain_pages_summary_e_mapeia(self):
        pagina = {"url": "https://site-e.exemplo.com/a", "rank": 30, "backlinks": 9, "referring_domains": 4, "referring_pages": 6, "backlinks_spam_score": 2,
                  "broken_backlinks": 1, "first_seen": "2026-01-01"}
        resp = [tarefa([{}], 0.02), tarefa([{"items": []}], 0.03), tarefa([{"items": []}], 0.04), tarefa([{"items": [pagina]}], 0.05)]
        r, ch = self.chama("get_backlinks_overview", resp, scope="domain", topPages=True)
        self.assertEqual(ch[3][0], "backlinks/domain_pages_summary/live")
        self.assertEqual((ch[3][1]["limit"], ch[3][1]["order_by"], ch[3][1]["target"]), (100, ["backlinks,desc"], "site-e.exemplo.com"))
        self.assertEqual(r["topPages"], [{"url": "https://site-e.exemplo.com/a", "rank": 30, "backlinks": 9, "referringDomains": 4, "referringPages": 6,
                                          "spamScore": 2, "brokenBacklinks": 1, "firstSeen": "2026-01-01"}])
        self.assertEqual(r["custo"], 0.14)

    def test_overview_sem_top_pages_nao_chama_domain_pages_summary(self):
        r, ch = self.chama("get_backlinks_overview", [tarefa([{}]), tarefa([{"items": []}]), tarefa([{"items": []}])], scope="domain")
        self.assertNotIn("backlinks/domain_pages_summary/live", [c[0] for c in ch])
        self.assertNotIn("topPages", r)

    def test_top_pages_so_para_domain_e_subdomains(self):
        for args in ({"target": "x.com/blog"}, {"target": "x.com/pg", "scope": "exact_url"}):
            with self.subTest(args=args), mock.patch.object(dataforseo, "post") as p:
                with self.assertRaises(mcp_grupos.ErroUso):
                    mcp_proxy.executar("get_backlinks_overview", {"site": "psite15", "topPages": True, **args})
                p.assert_not_called()

    def test_overview_falha_devolve_ok_false_com_custo_ja_gasto(self):
        falha = {"status_code": 40200, "status_message": "saldo", "cost": 0, "result": None}
        r, ch = self.chama("get_backlinks_overview", [tarefa([{}], 0.02), falha, tarefa([])], scope="subdomains")
        self.assertEqual((r["ok"], r["reason"], r["custo"], len(ch)), (False, "dataforseo_erro", 0.02, 2))

    def test_profile_corpo_e_campos(self):
        linha = {"domain_from": "a-com.example", "url_from": "https://a-com.example/x", "url_to": "https://site-e.exemplo.com/", "anchor": "psi", "type": "anchor", "dofollow": True,
                 "attributes": ["nofollow"], "rank": 40, "domain_from_rank": 60, "backlink_spam_score": 3, "first_seen": "2026-01-01", "is_lost": False}
        filtros = {"include": "blog,news", "minDomainRank": "20", "linkType": "dofollow", "hideLost": True}
        r, ch = self.chama("get_backlinks_profile", [tarefa([{"total_count": 120, "items": [linha]}], 0.03)],
                           page=2, pageSize=50, sortField="domainRank", sortOrder="asc", filters=filtros, mode="as_is")
        self.assertEqual(ch[0][0], "backlinks/backlinks/live")
        c = ch[0][1]
        self.assertEqual((c["target"], c["limit"], c["offset"], c["mode"], c["order_by"]), ("site-e.exemplo.com", 50, 50, "as_is", ["domain_from_rank,asc"]))
        self.assertEqual(c["filters"], [[["url_from", "ilike", "%blog%"], "or", ["url_from", "ilike", "%news%"]], "and", ["domain_from_rank", ">=", 20.0],
                                        "and", ["dofollow", "=", True], "and", ["is_lost", "=", False], "and", SPAM])
        b = r["backlinks"]
        self.assertEqual((r["ok"], r["custo"], b["totalCount"], b["hasMore"], b["page"], b["pageSize"]), (True, 0.03, 120, True, 2, 50))
        self.assertEqual((c["backlinks_status_type"], c["include_indirect_links"], c["exclude_internal_backlinks"]), ("live", True, True))
        self.assertEqual({k: b["rows"][0][k] for k in ("domainFrom", "urlFrom", "isDofollow", "domainFromRank", "spamScore", "isLost", "relAttributes")},
                         {"domainFrom": "a-com.example", "urlFrom": "https://a-com.example/x", "isDofollow": True, "domainFromRank": 60, "spamScore": 3, "isLost": False, "relAttributes": ["nofollow"]})

    def test_profile_mapeia_campos_alternativos_e_has_more_sem_total(self):
        itens = [{"item_type": "anchor", "rel_attributes": ["sponsored"], "backlinks_spam_score": 5, "lost_date": "2026-02-01", "last_seen": "2026-01-01"},
                 {"last_visited": "2026-03-01", "is_lost": False, "attributes": ["ugc"]}]
        r, _ = self.chama("get_backlinks_profile", [tarefa([{"items": itens}])], pageSize=50)
        a, b = r["backlinks"]["rows"]
        self.assertEqual((a["itemType"], a["relAttributes"], a["spamScore"], a["lastSeen"], a["isLost"], a["isBroken"]), ("anchor", ["sponsored"], 5, "2026-02-01", True, False))
        self.assertEqual((b["lastSeen"], b["isLost"], b["relAttributes"]), ("2026-03-01", False, ["ugc"]))
        self.assertEqual((r["backlinks"]["totalCount"], r["backlinks"]["hasMore"]), (None, False))

    def test_profile_padroes_e_hidespam_false(self):
        r, ch = self.chama("get_backlinks_profile", [tarefa([{"total_count": 0, "items": []}])], hideSpam=False)
        c = ch[0][1]
        self.assertEqual((c["limit"], c["offset"], c["mode"], c["order_by"]), (50, 0, "one_per_domain", ["first_seen,desc"]))
        self.assertNotIn("filters", c)
        _, ch = self.chama("get_backlinks_profile", [tarefa([{"items": []}])])
        self.assertEqual(ch[0][1]["filters"], [SPAM])
        self.assertEqual(r["backlinks"]["rows"], [])

    def test_pedido_errado_vira_erro_de_uso(self):
        for args in ({"pageSize": 75}, {"page": 0}, {"page": 1.5}, {"page": float("inf")}, {"pageSize": 50.5}, {"sortField": "x"}, {"scope": "pasta"}, {"target": "sem-ponto"}, {"scope": "subfolder"},
                     {"target": "x.com/a?b=1", "scope": "exact_url"}, {"filters": {"minSpamScore": "abc"}},
                     {"filters": {"include": "a,b,c,d,e", "exclude": "f,g"}}):
            with self.subTest(args=args), mock.patch.object(dataforseo, "post") as p:
                with self.assertRaises(mcp_grupos.ErroUso):
                    mcp_proxy.executar("get_backlinks_profile", {"site": "psite15", **args})
                p.assert_not_called()
        with self.assertRaises(mcp_grupos.ErroUso):
            backlinks.executar("get_backlinks_overview", {}, None)

    def test_dfs_resultado_passa_result_e_trata_erro(self):
        with mock.patch.object(dataforseo, "post", return_value=[tarefa([{"a": 1}], 0.5)]) as p:
            r = mcp_grupos.dfs_resultado("x/y", {"t": 1}, lambda res: {"res": res})
        self.assertEqual(p.call_args[0], ("x/y", [{"t": 1}]))
        self.assertEqual(r, {"ok": True, "res": [{"a": 1}], "custo": 0.5})
        with mock.patch.object(dataforseo, "post", return_value=[{"status_code": 40501, "status_message": "ruim", "cost": 0.1}]):
            r = mcp_grupos.dfs_resultado("x/y", {}, lambda res: {})
        self.assertEqual((r["ok"], r["custo"]), (False, 0.1))
        with mock.patch.object(dataforseo, "post", side_effect=RuntimeError("DataForSEO HTTP 500")):
            self.assertEqual(mcp_grupos.dfs_resultado("x/y", {}, lambda res: {})["message"], "DataForSEO HTTP 500")


if __name__ == "__main__":
    unittest.main()
