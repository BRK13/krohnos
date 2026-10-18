import json
import os
import tempfile
import unittest
from unittest import mock

from hub import dataforseo, mcp_proxy
from hub.mcp_grupos import ErroUso


def _kd(palavra, vol, kd=30, intencao="informational", cpc=1.5):
    return {"keyword": palavra, "keyword_info": {"search_volume": vol, "cpc": cpc, "competition": 0.4, "monthly_searches": [{"year": 2026, "month": 1, "search_volume": vol}]},
            "keyword_properties": {"keyword_difficulty": kd}, "search_intent_info": {"main_intent": intencao}}


def _tarefa(itens, custo=0.01):
    return {"status_code": 20000, "cost": custo, "result": [{"items": itens}]}


SUGESTOES = [_kd("Terapia Online", 1000), _kd("terapia online preço", 300, intencao="commercial"), _kd("psicólogo online", 500),
             _kd("terapia para ansiedade", 200), _kd("terapia de casal", 100), _kd("terapia em grupo", 90)]
IDEIAS = [_kd("psicólogo online", 500), _kd("saúde mental", 800, intencao="transactional"), _kd("terapia cognitiva", 70), _kd("meditação", 60)]


BH = "Belo Horizonte,State of Minas Gerais,Brazil"
LOCAIS_BR = [{"status_code": 20000, "result": [{"location_code": 1001773, "location_name": BH, "location_type": "City"}]}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "DATAFORSEO_AUTH": "x"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class ResearchKeywordsTest(Base):
    def test_aparece_no_tools_list_com_as_propriedades_do_original(self):
        f = mcp_proxy.FERRAMENTAS["research_keywords"]
        self.assertTrue(f["description"].endswith(" Custa crédito do DataForSEO."))
        self.assertLessEqual({"projectId", "seeds", "resultLimit", "includeClickstreamData", "groupKeywords"}, set(f["inputSchema"]["properties"]))
        self.assertEqual(set(f["inputSchema"]["properties"]["seeds"]["items"]["properties"]), {"seed", "locationCode", "languageCode", "locationName"})

    def test_modo_auto_mistura_sugestoes_e_ideias(self):
        def post(caminho, tarefas):
            return [_tarefa(SUGESTOES if "suggestions" in caminho else IDEIAS)]
        with mock.patch.object(dataforseo, "post", side_effect=post) as p:
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "seeds": [{"seed": "Terapia Online", "locationCode": 2076, "languageCode": "pt"}]})
        caminhos = [c.args[0] for c in p.call_args_list]
        self.assertEqual(caminhos, ["dataforseo_labs/google/keyword_suggestions/live", "dataforseo_labs/google/keyword_ideas/live"])
        sug, ide = (c.args[1][0] for c in p.call_args_list)
        self.assertEqual(sug, {"keyword": "terapia online", "location_code": 2076, "language_code": "pt", "limit": 75, "include_clickstream_data": False,
                               "include_serp_info": False, "include_seed_keyword": True, "ignore_synonyms": True, "exact_match": False})
        self.assertEqual(ide, {"keywords": ["terapia online"], "location_code": 2076, "language_code": "pt", "limit": 75, "include_clickstream_data": False,
                               "include_serp_info": False, "ignore_synonyms": True, "closely_variants": False})
        self.assertTrue(r["ok"])
        self.assertEqual(r["custo"], 0.02)
        s = r["results"][0]
        self.assertEqual((s["seed"], s["ok"], s["source"], s["usedFallback"]), ("Terapia Online", True, "blended", False))
        self.assertEqual(s["rowCount"], len(s["rows"]))
        palavras = [x["keyword"] for x in s["rows"]]
        self.assertEqual(len(palavras), len(set(palavras)))  # "psicólogo online" vem das duas fontes e aparece uma vez
        self.assertEqual(s["rows"][0], {"keyword": "terapia online", "searchVolume": 1000, "keywordDifficulty": 30, "cpc": 1.5, "competition": 0.4, "intent": "informational"})
        self.assertEqual(next(x for x in s["rows"] if x["keyword"] == "saúde mental")["intent"], "transactional")

    def test_poucas_palavras_cai_para_relacionadas(self):
        pouco = [_kd("terapia online", 10), _kd("terapia", 5)]
        relacionadas = [{"keyword_data": _kd(k, 10)} for k in ("a b", "c d", "e f", "g h", "i j")] + [{"keyword_data": None}]
        def post(caminho, tarefas):
            return [_tarefa(relacionadas if "related" in caminho else pouco)]
        with mock.patch.object(dataforseo, "post", side_effect=post) as p:
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "includeClickstreamData": True, "groupKeywords": True, "resultLimit": 300,
                                                        "seeds": [{"seed": "terapia online"}]})
        self.assertEqual(p.call_args[0][0], "dataforseo_labs/google/related_keywords/live")
        rel = p.call_args[0][1][0]
        self.assertEqual((rel["depth"], rel["limit"], rel["ignore_synonyms"], rel["include_clickstream_data"]), (3, 300, False, True))
        s = r["results"][0]
        self.assertTrue(s["usedFallback"])
        self.assertEqual(s["rowCount"], 7)
        self.assertEqual(r["custo"], 0.03)

    def test_semente_com_falha_nao_derruba_as_outras(self):
        def post(caminho, tarefas):
            if tarefas[0].get("keyword", tarefas[0].get("keywords", [""])[0]) == "ruim":
                return [{"status_code": 40501, "status_message": "Invalid Field", "cost": 0}]
            return [_tarefa(SUGESTOES if "suggestions" in caminho else IDEIAS)]
        with mock.patch.object(dataforseo, "post", side_effect=post):
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "seeds": [{"seed": "ruim"}, {"seed": "terapia online"}]})
        self.assertTrue(r["ok"])
        self.assertEqual([x["ok"] for x in r["results"]], [False, True])
        self.assertIn("40501", r["results"][0]["error"])

    def test_volume_local_troca_so_volume_cpc_e_concorrencia(self):
        def post(caminho, tarefas):
            if "search_volume" in caminho:
                self.assertEqual(tarefas[0]["location_name"], "Belo Horizonte,State of Minas Gerais,Brazil")
                self.assertNotIn("terapia online?", tarefas[0]["keywords"])  # recusada pelo Ads: não vai na tarefa
                return [{"status_code": 20000, "cost": 0.05, "result": [{"keyword": "terapia online", "search_volume": 90, "cpc": 0.7, "competition_index": 25}]}]
            return [_tarefa([_kd("terapia online", 1000, kd=33), _kd("terapia online?", 5)] + SUGESTOES[2:])]
        with mock.patch.object(dataforseo, "post", side_effect=post), mock.patch.object(dataforseo, "get", return_value=LOCAIS_BR):
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "seeds": [{"seed": "terapia online", "locationName": "Belo Horizonte,State of Minas Gerais,Brazil"}]})
        rows = {x["keyword"]: x for x in r["results"][0]["rows"]}
        self.assertEqual(rows["terapia online"], {"keyword": "terapia online", "searchVolume": 90, "keywordDifficulty": 33, "cpc": 0.7, "competition": 0.25, "intent": "informational"})
        self.assertEqual((rows["psicólogo online"]["searchVolume"], rows["psicólogo online"]["cpc"], rows["psicólogo online"]["keywordDifficulty"]), (None, None, 30))
        self.assertAlmostEqual(r["custo"], 0.07)
        self.assertEqual(r["results"][0]["locationName"], BH)

    def test_locationName_desconhecido_nao_chama_chamada_paga(self):
        with mock.patch.object(dataforseo, "post") as p, mock.patch.object(dataforseo, "get", return_value=LOCAIS_BR):
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "seeds": [{"seed": "a", "locationName": "Atlantis"}]})
        p.assert_not_called()
        s = r["results"][0]
        self.assertFalse(s["ok"])
        self.assertIn("não é cidade/condado/região de BR", s["error"])
        self.assertEqual(r["custo"], 0)

    def test_sementes_em_ordem_mesmo_em_paralelo(self):
        with mock.patch.object(dataforseo, "post", return_value=[_tarefa(SUGESTOES)]):
            r = mcp_proxy.executar("research_keywords", {"site": "psite13", "seeds": [{"seed": x} for x in "abcde"]})
        self.assertEqual([x["seed"] for x in r["results"]], list("abcde"))

    def test_volume_local_desliga_clickstream_nas_chamadas_labs(self):
        def post(caminho, tarefas):
            if "search_volume" in caminho:
                return [{"status_code": 20000, "cost": 0, "result": []}]
            return [_tarefa(SUGESTOES)]
        with mock.patch.object(dataforseo, "post", side_effect=post) as p, mock.patch.object(dataforseo, "get", return_value=LOCAIS_BR):
            mcp_proxy.executar("research_keywords", {"site": "psite13", "includeClickstreamData": True,
                                                    "seeds": [{"seed": "a"}, {"seed": "b", "locationName": "Belo Horizonte,State of Minas Gerais,Brazil"}]})
        labs = [(c.args[1][0]["keyword" if "keyword" in c.args[1][0] else "keywords"], c.args[1][0]["include_clickstream_data"])
                for c in p.call_args_list if "dataforseo_labs" in c.args[0]]
        self.assertEqual({(str(k), v) for k, v in labs}, {("a", True), ("['a']", True), ("['b']", False), ("b", False)})

    def test_pedido_errado_vira_erro_de_uso(self):
        for args in ({"site": "psite13"}, {"site": "psite13", "seeds": []}, {"site": "psite13", "seeds": [{"seed": " "}]},
                     {"site": "psite13", "seeds": [{"seed": "x"}] * 6}, {"site": "psite13", "seeds": [{"seed": "x"}], "resultLimit": 200},
                     {"site": "psite13", "seeds": [{"seed": "x", "locationName": " "}]},
                     {"site": "psite13", "seeds": [{"seed": "x"}], "resultLimit": 150.0},
                     {"site": "psite13", "seeds": [{"seed": "x", "locationCode": 0}]}, {"site": "psite13", "seeds": [{"seed": "x", "locationCode": 2076.0}]},
                     {"site": "psite13", "seeds": [{"seed": "x", "languageCode": 5}]}):
            with mock.patch.object(dataforseo, "post") as p, self.assertRaises(ErroUso, msg=args):
                mcp_proxy.executar("research_keywords", args)
            p.assert_not_called()


RANKED = [{"keyword_data": {"keyword": "terapia online", "keyword_info": {"search_volume": 1000.4, "cpc": 2.1}, "keyword_properties": {"keyword_difficulty": 41.6}},
           "ranked_serp_element": {"serp_item": {"rank_absolute": 3, "etv": 250.5, "url": "https://zenklub-com-br.example/terapia/"}}},
          {"keyword_data": {"keyword": "psicólogo", "keyword_info": {"search_volume": 500, "cpc": None}},
           "ranked_serp_element": {"serp_item": {"rank_absolute": 7, "etv": 10}}}]


class DomainKeywordSuggestionsTest(Base):
    def test_aparece_no_tools_list_com_as_propriedades_do_original(self):
        f = mcp_proxy.FERRAMENTAS["get_domain_keyword_suggestions"]
        self.assertTrue(f["description"].endswith(" Custa crédito do DataForSEO."))
        self.assertLessEqual({"projectId", "domain", "scope", "locationCode", "languageCode"}, set(f["inputSchema"]["properties"]))
        self.assertEqual(f["inputSchema"]["properties"]["scope"]["enum"], ["exact_url", "subfolder", "domain", "subdomains"])

    def test_raiz_usa_subdominios_sem_filtro(self):
        with mock.patch.object(dataforseo, "post", return_value=[_tarefa(RANKED, 0.05)]) as p:
            r = mcp_proxy.executar("get_domain_keyword_suggestions", {"site": "psite13", "domain": "https://www.zenklub-com-br.example/"})
        self.assertEqual(p.call_args[0][0], "dataforseo_labs/google/ranked_keywords/live")
        t = p.call_args[0][1][0]
        self.assertEqual({k: t[k] for k in ("target", "limit", "order_by")}, {"target": "zenklub-com-br.example", "limit": 100, "order_by": ["ranked_serp_element.serp_item.etv,desc"]})
        self.assertNotIn("filters", t)
        self.assertEqual((r["ok"], r["custo"], r["target"], r["scope"]), (True, 0.05, "zenklub-com-br.example", "subdomains"))
        self.assertEqual(r["keywords"][0], {"keyword": "terapia online", "position": 3, "searchVolume": 1000, "traffic": 250.5, "cpc": 2.1, "keywordDifficulty": 42})
        self.assertEqual(r["keywords"][1]["keywordDifficulty"], None)

    def test_caminho_vira_subpasta_com_filtro(self):
        with mock.patch.object(dataforseo, "post", return_value=[_tarefa(RANKED)]) as p:
            r = mcp_proxy.executar("get_domain_keyword_suggestions", {"site": "psite13", "domain": "zenklub-com-br.example/blog/", "locationCode": 2076, "languageCode": "pt"})
        t = p.call_args[0][1][0]
        self.assertEqual(t["filters"][0], ["ranked_serp_element.serp_item.domain", "in", ["zenklub-com-br.example", "www.zenklub-com-br.example"]])
        self.assertEqual(t["filters"][2][0], ["ranked_serp_element.serp_item.relative_url", "=", "/blog"])
        self.assertEqual((r["scope"], r["target"]), ("subfolder", "zenklub-com-br.example/blog"))

    def test_escopo_domain_filtra_so_o_host(self):
        with mock.patch.object(dataforseo, "post", return_value=[_tarefa([])]) as p:
            mcp_proxy.executar("get_domain_keyword_suggestions", {"site": "psite13", "domain": "zenklub-com-br.example", "scope": "domain"})
        self.assertEqual(p.call_args[0][1][0]["filters"], [["ranked_serp_element.serp_item.domain", "in", ["zenklub-com-br.example", "www.zenklub-com-br.example"]]])

    def test_pedido_errado_vira_erro_de_uso(self):
        for args in ({"site": "psite13"}, {"site": "psite13", "domain": "nao e dominio"}, {"site": "psite13", "domain": "semponto"},
                     {"site": "psite13", "domain": "a-com.example", "scope": "subfolder"}, {"site": "psite13", "domain": "a-com.example", "scope": "xis"},
                     {"site": "psite13", "domain": "https://u:user9@example.com"}):
            with mock.patch.object(dataforseo, "post") as p, self.assertRaises(ErroUso, msg=args):
                mcp_proxy.executar("get_domain_keyword_suggestions", args)
            p.assert_not_called()

    def test_falha_da_fonte_devolve_ok_false(self):
        with mock.patch.object(dataforseo, "post", side_effect=RuntimeError("DataForSEO respondeu HTTP 500")):
            r = mcp_proxy.executar("get_domain_keyword_suggestions", {"site": "psite13", "domain": "a-com.example"})
        self.assertFalse(r["ok"])



def _salvar(**kw):
    return mcp_proxy.executar("save_keywords", {"site": "psite13", **kw})


class PalavrasSalvasTest(Base):
    def test_aparecem_no_tools_list_sem_cobrar_credito(self):
        esperado = {"save_keywords": {"projectId", "keywords", "metrics", "tags", "tagMode", "locationCode", "languageCode"},
                    "list_saved_keywords": {"projectId", "search", "tags", "limit"},
                    "remove_saved_keywords": {"projectId", "savedKeywordIds", "keywords"}}
        for nome, props in esperado.items():
            f = mcp_proxy.FERRAMENTAS[nome]
            self.assertLessEqual(props, set(f["inputSchema"]["properties"]))
            self.assertNotIn("Custa crédito", f["description"])

    def test_salva_lista_e_remove_no_arquivo_do_site(self):
        r = _salvar(keywords=["Terapia Online", "ansiedade"], tags=["lp"], metrics=[{"keyword": "terapia online", "searchVolume": 900, "cpc": 1.2, "intent": "commercial"}])
        self.assertEqual((r["ok"], r["projectId"], r["savedCount"], r["tags"], r["tagMode"], r["locationCode"], r["languageCode"]), (True, "psite13", 2, ["lp"], "append", 2076, "pt"))
        arq = os.path.join(self.tmp.name, "mcp", "palavras", "psite13", "salvas.json")
        self.assertTrue(os.path.exists(arq))
        self.assertFalse(os.path.exists(arq + ".tmp"))
        l = mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})
        self.assertEqual(l["totalCount"], 2)
        self.assertEqual([x["keyword"] for x in l["rows"]], ["ansiedade", "terapia online"])  # mais nova primeiro
        t = l["rows"][1]
        self.assertEqual((t["searchVolume"], t["cpc"], t["intent"], t["keywordDifficulty"], t["tags"]), (900, 1.2, "commercial", None, ["lp"]))
        self.assertEqual(l["tags"], [{"name": "lp", "keywordCount": 2}])
        d = mcp_proxy.executar("remove_saved_keywords", {"site": "psite13", "savedKeywordIds": [t["id"], "nao-existe"]})
        self.assertEqual((d["projectId"], d["requested"], d["deletedCount"]), ("psite13", 2, 1))
        self.assertEqual([x["keyword"] for x in mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})["rows"]], ["ansiedade"])

    def test_salvar_duas_vezes_nao_duplica_e_atualiza_metricas_e_tags(self):
        _salvar(keywords=["Terapia Online"], tags=["a"])
        id1 = mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})["rows"][0]["id"]
        _salvar(keywords=["terapia online "], tags=["b"], metrics=[{"keyword": "Terapia Online", "searchVolume": 5}])
        l = mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})
        self.assertEqual((l["totalCount"], l["rows"][0]["id"], l["rows"][0]["tags"], l["rows"][0]["searchVolume"]), (1, id1, ["a", "b"], 5))
        _salvar(keywords=["terapia online"], tags=["c"], tagMode="replace")
        self.assertEqual(mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})["rows"][0]["tags"], ["c"])

    def test_lista_filtra_por_texto_tag_e_limite(self):
        _salvar(keywords=["terapia online", "terapia de casal"], tags=["x"])
        _salvar(keywords=["meditação"], tags=["y"])
        ler = lambda **a: mcp_proxy.executar("list_saved_keywords", {"site": "psite13", **a})
        self.assertEqual([r["keyword"] for r in ler(search="TERAPIA")["rows"]], ["terapia de casal", "terapia online"])
        self.assertEqual([r["keyword"] for r in ler(tags=["Y", "zzz"])["rows"]], ["meditação"])
        self.assertEqual((len(ler(limit=50)["rows"]), ler(limit=50)["totalCount"]), (3, 3))

    def test_duas_threads_salvando_no_mesmo_site_nao_perdem_palavras(self):
        import threading
        def salvar(i):
            for j in range(15):
                _salvar(keywords=[f"t{i}-{j}"])
        ts = [threading.Thread(target=salvar, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(mcp_proxy.executar("list_saved_keywords", {"site": "psite13", "limit": 50})["totalCount"], 30)
        pasta = os.path.join(self.tmp.name, "mcp", "palavras", "psite13")
        self.assertEqual(sorted(os.listdir(pasta)), ["salvas.json", "salvas.lock"])

    def test_remove_pelas_palavras(self):
        _salvar(keywords=["um", "dois", "três"])
        d = mcp_proxy.executar("remove_saved_keywords", {"site": "psite13", "keywords": ["UM", "três", "nada"]})
        self.assertEqual((d["requested"], d["deletedCount"]), (3, 2))
        self.assertEqual([r["keyword"] for r in mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})["rows"]], ["dois"])

    def test_estado_e_por_site(self):
        _salvar(keywords=["um"])
        self.assertEqual(mcp_proxy.executar("list_saved_keywords", {"site": "psite13"})["totalCount"], 1)
        outro = next(s for s in mcp_proxy.executar("list_projects", {})["projects"] if s["id"] != "psite13")["id"]
        self.assertEqual(mcp_proxy.executar("list_saved_keywords", {"site": outro})["totalCount"], 0)

    def test_pedido_errado_vira_erro_de_uso(self):
        casos = [("save_keywords", {"site": "psite13"}), ("save_keywords", {"site": "psite13", "keywords": []}),
                 ("save_keywords", {"site": "psite13", "keywords": ["a"] * 101}), ("save_keywords", {"site": "psite13", "keywords": ["a"], "tagMode": "replace"}),
                 ("save_keywords", {"site": "psite13", "keywords": ["a"], "tagMode": "x", "tags": ["t"]}),
                 ("save_keywords", {"site": "psite13", "keywords": ["a"], "tags": ["t" * 65]}),
                 ("save_keywords", {"keywords": ["a"]}), ("list_saved_keywords", {}),
                 ("list_saved_keywords", {"site": "psite13", "limit": 10}), ("list_saved_keywords", {"site": "psite13", "limit": 100.0}), ("list_saved_keywords", {"site": "psite13", "search": ""}),
                 ("remove_saved_keywords", {"site": "psite13"}), ("remove_saved_keywords", {"site": "psite13", "savedKeywordIds": []}),
                 ("remove_saved_keywords", {})]
        for nome, args in casos:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                mcp_proxy.executar(nome, args)


LOCAIS = {"status_code": 20000, "cost": 0, "result": [
    {"location_code": 1, "location_name": "Lisbon,Lisbon,Portugal", "location_type": "Municipality"},
    {"location_code": 2, "location_name": "Porto,Porto,Portugal", "location_type": "Municipality"},
    {"location_code": 3, "location_name": "Sao Paulo,State of Sao Paulo,Brazil", "location_type": "City"},
    {"location_code": 4, "location_name": "Sao Paulo,Brazil", "location_type": "State"},
    {"location_code": 5, "location_name": "04000,Sao Paulo,State of Sao Paulo,Brazil", "location_type": "Postal Code"}]}


class SerpLocationsTest(Base):
    def test_aparece_no_tools_list_com_as_propriedades_do_original(self):
        f = mcp_proxy.FERRAMENTAS["search_serp_locations"]
        self.assertEqual(set(f["inputSchema"]["properties"]), {"query", "countryCode"})
        self.assertEqual(f["inputSchema"]["required"], ["query", "countryCode"])
        self.assertNotIn("Custa crédito", f["description"])

    def test_acha_o_local_e_usa_o_cache_na_segunda_chamada(self):
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]) as g:
            r = mcp_proxy.executar("search_serp_locations", {"query": "Lisbon", "countryCode": "PT"})
            r2 = mcp_proxy.executar("search_serp_locations", {"query": "porto", "countryCode": "pt"})
        g.assert_called_once_with("serp/google/locations/pt")
        self.assertEqual(r["locations"], [{"locationCode": 1, "locationName": "Lisbon,Lisbon,Portugal", "locationType": "Municipality"}])
        self.assertEqual([l["locationCode"] for l in r2["locations"]], [2])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "mcp", "palavras", "locations.json")))

    def test_buscar_outro_pais_nao_apaga_o_cache_dos_demais(self):
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]):
            mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "pt"})
            mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "us"})
        with open(os.path.join(self.tmp.name, "mcp", "palavras", "locations.json")) as f:
            self.assertEqual(set(json.load(f)), {"pt", "us"})

    def test_falha_ao_gravar_o_cache_nao_vira_erro_de_chave(self):
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]), mock.patch("os.replace", side_effect=OSError("disco cheio")):
            r = mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "pt"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["locations"]), 1)

    def test_cache_vencido_busca_de_novo(self):
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]) as g:
            mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "pt"})
            arq = os.path.join(self.tmp.name, "mcp", "palavras", "locations.json")
            with open(arq) as f:
                c = json.load(f)
            c["pt"]["em"] = "2020-01-01T00:00:00+00:00"
            with open(arq, "w") as f:
                json.dump(c, f)
            mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "pt"})
        self.assertEqual(g.call_count, 2)

    def test_ignora_acento_filtra_tipos_e_ordena_por_tipo(self):
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]):
            r = mcp_proxy.executar("search_serp_locations", {"query": "São Paulo", "countryCode": "br"})
        # State e Postal Code ficam de fora (só City, County, Municipality, DMA Region, Region)
        self.assertEqual([l["locationCode"] for l in r["locations"]], [3])

    def test_nome_em_portugues_nao_casa_com_o_nome_em_ingles(self):
        # o original só compara com location_name (inglês) e não traduz: "lisboa" não acha "Lisbon"
        with mock.patch.object(dataforseo, "get", return_value=[LOCAIS]):
            r = mcp_proxy.executar("search_serp_locations", {"query": "lisboa", "countryCode": "pt"})
        self.assertEqual(r["locations"], [])

    def test_falha_do_dataforseo_devolve_ok_false(self):
        with mock.patch.object(dataforseo, "get", side_effect=RuntimeError("DataForSEO respondeu HTTP 500")):
            r = mcp_proxy.executar("search_serp_locations", {"query": "lisbon", "countryCode": "pt"})
        self.assertEqual((r["ok"], r["reason"]), (False, "dataforseo_erro"))

    def test_pedido_errado_vira_erro_de_uso(self):
        for args in ({}, {"query": "x"}, {"countryCode": "pt"}, {"query": "", "countryCode": "pt"}, {"query": "x" * 101, "countryCode": "pt"},
                     {"query": "x", "countryCode": "prt"}, {"query": "x", "countryCode": "p1"}):
            with self.subTest(args=args), self.assertRaises(ErroUso):
                mcp_proxy.executar("search_serp_locations", args)


if __name__ == "__main__":
    unittest.main()
