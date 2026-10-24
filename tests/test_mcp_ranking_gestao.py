import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import dataforseo, mcp_proxy, ranking, sites
from hub.mcp_grupos import ErroUso

NOMES = ("create_rank_tracker", "add_rank_tracking_keywords", "remove_rank_tracking_keywords", "pin_rank_tracking_keywords",
         "estimate_rank_tracker_cost", "run_rank_tracker")
PSI = [f"palavras {i} psite15" for i in range(1, 8)]  # as do cadastro de teste (tests/fixtures/dados/projetos/psite15.json)


def ex(nome, **args):
    return mcp_proxy.executar(nome, {"site": "psite15", **args})


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "DATAFORSEO_AUTH": "x"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cfg(self):
        return ranking.config(next(s for s in sites.carregar() if s["id"] == "psite15"))

    def test_schemas_como_o_original(self):
        props = {
            "create_rank_tracker": {"domain", "locationCode", "languageCode", "locationName", "devices", "serpDepth", "scheduleInterval", "scheduleTime"},
            "add_rank_tracking_keywords": {"trackerId", "keywords", "matchCase", "maxEstimatedScheduledCheckCredits"},
            "remove_rank_tracking_keywords": {"trackerId", "keywordIds"},
            "pin_rank_tracking_keywords": {"trackerId", "keywordIds", "pinned"},
            "estimate_rank_tracker_cost": {"trackerId", "additionalKeywordCount", "additionalKeywords"},
            "run_rank_tracker": {"trackerId", "maxCostCredits"},
        }
        for nome, esperado in props.items():
            self.assertEqual(set(mcp_proxy.FERRAMENTAS[nome]["inputSchema"]["properties"]), esperado | {"site", "projectId"}, nome)
            self.assertNotIn(nome, mcp_proxy.SUBSTITUTAS)
        p = mcp_proxy.FERRAMENTAS["create_rank_tracker"]["inputSchema"]["properties"]["serpDepth"]
        self.assertEqual((p["minimum"], p["maximum"], p["multipleOf"]), (10, 100, 10))
        k = mcp_proxy.FERRAMENTAS["add_rank_tracking_keywords"]["inputSchema"]["properties"]["keywords"]
        self.assertEqual((k["minItems"], k["maxItems"], k["items"]["maxLength"]), (1, 2000, 200))
        d = mcp_proxy.FERRAMENTAS["create_rank_tracker"]["inputSchema"]["properties"]["domain"]
        self.assertEqual((d["minLength"], d["maxLength"]), (1, 253))
        self.assertTrue(mcp_proxy.FERRAMENTAS["run_rank_tracker"]["description"].endswith(" Custa crédito do DataForSEO."))
        self.assertEqual([n for n in NOMES if "Custa crédito" in mcp_proxy.FERRAMENTAS[n]["description"]], ["run_rank_tracker"])

    def test_estimativa_confere_a_conta(self):
        r = ex("estimate_rank_tracker_cost")
        # profundidade 40: 0.002 + 3 × 0.0015 = 0.0065 por palavra → 7 créditos
        self.assertEqual((r["trackerId"], r["keywordCount"], r["costCredits"], r["costUsd"], r["method"]), ("psite15", 7, 49, 0.0455, "live"))
        self.assertEqual(r["scheduledEstimate"], {"scheduleInterval": "weekly", "costUsd": 0.0455, "costCredits": 49, "checksPerMonth": 4,
                                                  "monthlyCostUsd": 0.182, "monthlyCostCredits": 196})
        r = ex("estimate_rank_tracker_cost", additionalKeywords=["site:site-e.exemplo.com terapia"], additionalKeywordCount=50)
        self.assertEqual((r["additionalKeywordCount"], r["costCredits"], r["costUsd"]), (1, 49 + 33, 0.078))  # site: custa 5×
        self.assertEqual(ex("estimate_rank_tracker_cost", additionalKeywordCount=3)["totalChecks"], 10)
        self.assertFalse((Path(self.tmp.name) / "mcp/ranking/psite15/config.json").exists())

    def test_create_add_remove_pin_gravam_na_pasta_do_mcp(self):
        arq = Path(self.tmp.name) / "mcp/ranking/psite15/config.json"
        r = ex("create_rank_tracker", domain="https://www.site-e.exemplo.com.br/x", serpDepth=20, devices="desktop", scheduleInterval="daily")
        self.assertEqual(r["config"], {"domain": "site-e.exemplo.com.br", "locationCode": 2076, "languageCode": "pt", "devices": "desktop",
                                       "serpDepth": 20, "scheduleInterval": "weekly", "palavras": PSI, "fixadas": []})
        self.assertEqual(set(json.loads(arq.read_text(encoding="utf-8"))), {"palavras", "fixadas", "local", "idioma", "dispositivo", "profundidade"})
        r = ex("add_rank_tracking_keywords", keywords=["Terapia Online", "terapia online", PSI[0], "Psi Rio"], matchCase=False)
        self.assertEqual((r["requested"], r["added"], r["addedIds"]), (4, 2, ["terapia online", "psi rio"]))
        self.assertEqual(r["scheduledEstimate"]["costCredits"], 9 * 4)  # profundidade 20: 0.0035 → 4 créditos
        self.assertEqual(ex("add_rank_tracking_keywords", keywords=["Psi Rio"], matchCase=True)["addedIds"], ["Psi Rio"])
        self.assertEqual(ex("add_rank_tracking_keywords", keywords=["psi rio"])["added"], 0)
        r = ex("pin_rank_tracking_keywords", keywordIds=["psi rio", "psi rio", "nao existe"], pinned=True)
        self.assertEqual((r["requested"], r["updated"], r["updatedIds"], r["pinned"]), (3, 1, ["psi rio"], True))
        self.assertEqual(ex("pin_rank_tracking_keywords", keywordIds=["psi rio"], pinned=True)["updated"], 0)
        r = ex("remove_rank_tracking_keywords", keywordIds=["psi rio", "nao existe", "psi rio"], trackerId="psite15")
        self.assertEqual((r["requested"], r["removed"], r["removedIds"]), (3, 1, ["psi rio"]))
        c = self.cfg()
        self.assertEqual((c["palavras"], c["fixadas"], c["profundidade"], c["dispositivo"]), (PSI + ["terapia online", "Psi Rio"], [], 20, "desktop"))
        self.assertNotIn("\\u00", arq.read_text(encoding="utf-8"))  # utf-8 sem escape

    def test_teto_do_add_nao_grava(self):
        with self.assertRaises(ErroUso):
            ex("add_rank_tracking_keywords", keywords=["nova"], maxEstimatedScheduledCheckCredits=55)
        self.assertFalse((Path(self.tmp.name) / "mcp/ranking/psite15/config.json").exists())
        self.assertEqual(ex("add_rank_tracking_keywords", keywords=["nova"], maxEstimatedScheduledCheckCredits=56)["added"], 1)

    def test_teto_de_1000_palavras_nao_grava(self):
        arq = Path(self.tmp.name) / "mcp/ranking/psite15/config.json"
        self.assertEqual(ex("add_rank_tracking_keywords", keywords=[f"p{i}" for i in range(992)])["added"], 992)  # 7 + 992 = 999
        antes = arq.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ErroUso, "1000"):
            ex("add_rank_tracking_keywords", keywords=["a", "b"])
        self.assertEqual(arq.read_text(encoding="utf-8"), antes)
        self.assertEqual(ex("add_rank_tracking_keywords", keywords=["a", "p1"])["added"], 1)
        self.assertEqual(len(self.cfg()["palavras"]), 1000)
        self.assertEqual(list(arq.parent.glob("*.tmp")), [])

    def test_pedidos_errados(self):
        errados = [("run_rank_tracker", {"trackerId": "outro", "maxCostCredits": 100}),
                   ("create_rank_tracker", {"devices": "both"}),
                   ("create_rank_tracker", {"locationName": "Lisboa,Portugal"}),
                   ("create_rank_tracker", {"domain": "zenklub-com-br.example"}),
                   ("create_rank_tracker", {"serpDepth": 45}),
                   ("run_rank_tracker", {"maxCostCredits": 48}),
                   ("run_rank_tracker", {}),
                   ("add_rank_tracking_keywords", {"keywords": []}),
                   ("add_rank_tracking_keywords", {"keywords": ["x" * 201]}),
                   ("pin_rank_tracking_keywords", {"keywordIds": [PSI[0]]}),
                   ("estimate_rank_tracker_cost", {"additionalKeywordCount": 1001})]
        for nome, args in errados:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                ex(nome, **args)
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("estimate_rank_tracker_cost", {})
        sem = next(s for s in sites.carregar() if not s["ranking"])
        with self.assertRaisesRegex(ErroUso, "create_rank_tracker"):
            mcp_proxy.executar("add_rank_tracking_keywords", {"site": sem["id"], "keywords": ["a"]})
        r = mcp_proxy.executar("create_rank_tracker", {"site": sem["id"]})
        self.assertEqual((r["config"]["palavras"], r["config"]["devices"], r["config"]["serpDepth"]), ([], "mobile", 40))
        self.assertEqual(mcp_proxy.executar("add_rank_tracking_keywords", {"site": sem["id"], "keywords": ["a"]})["added"], 1)

    def test_run_mede_e_grava_so_na_pasta_do_mcp(self):
        chamadas = []

        def post(caminho, tarefas):
            chamadas.append((caminho, tarefas))
            itens = [{"type": "organic", "domain": "www.site-e.exemplo.com", "rank_group": 3, "url": "https://site-e.exemplo.com/"}]
            return [{"status_code": 20000, "cost": 0.0065, "result": [{"items": itens if tarefas[0]["keyword"] == PSI[0] else []}]}]

        with mock.patch.object(dataforseo, "post", post):
            r = ex("run_rank_tracker", maxCostCredits=49, trackerId="psite15")
        self.assertEqual(len(chamadas), 7)
        self.assertEqual(chamadas[0], ("serp/google/organic/live/advanced", [{"keyword": PSI[0], "location_code": 2076,
                                                                              "language_code": "pt", "device": "mobile", "depth": 40}]))
        self.assertEqual((r["ok"], r["started"], r["runId"], r["custo"]), (True, True, r["dia"], 0.0455))
        self.assertEqual(r["itens"][0], {"palavra": PSI[0], "posicao": 3, "url": "https://site-e.exemplo.com/"})
        base = Path(self.tmp.name)
        self.assertTrue((base / f"mcp/ranking/psite15/medicoes/{r['dia']}.json").exists())
        self.assertFalse((base / "bi/metricas.db").exists())
        self.assertFalse((base / "ranking").exists())
        self.assertEqual(mcp_proxy.executar("get_rank_tracker", {"site": "psite15"})["palavras"][0]["posicao"], 3)


if __name__ == "__main__":
    unittest.main()
