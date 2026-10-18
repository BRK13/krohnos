"""GSC/GA4 da conta de serviço no formato do structuredContent das ferramentas do OpenSEO."""
import datetime as dt
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from hub import google

F = Path(__file__).parent / "fixtures"
GSC = json.loads((F / "gsc_api.json").read_text(encoding="utf-8"))
GA4 = json.loads((F / "ga4_api.json").read_text(encoding="utf-8"))
HOJE = dt.datetime(2026, 10, 8, 12, tzinfo=dt.timezone.utc)
SITE = "https://example.com/"


def api_gsc(chamadas):
    def falso(url, propriedade, corpo=None, escopo=google.ESCOPO_GSC):
        chamadas.append((url, corpo, escopo))
        if "index:inspect" in url:
            if corpo["inspectionUrl"].endswith("/ruim"):
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            return GSC["inspect"]
        return GSC["discover"] if corpo["type"] == "discover" else GSC["query"]
    return falso


def api_ga4(chamadas):
    def falso(url, propriedade, corpo=None, escopo=google.ESCOPO_GSC):
        chamadas.append((url, corpo, escopo))
        if ":runReport" in url:
            chave = corpo["dimensions"][0]["name"] if corpo["dimensions"] else "total"
            return GA4["runReport"][chave]
        return GA4["propriedade"]
    return falso


class ContaServicoTest(unittest.TestCase):
    def test_ausente_ou_ilegivel_vira_erro_credencial(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(google, "home", return_value=Path(d)):
                with self.assertRaises(google.ErroCredencial):
                    google.conta_servico()
                (Path(d) / "credenciais").mkdir()
                arq = Path(d) / "credenciais/google-conta-servico.json"
                for conteudo in ("{nao json PK-SECRETA", '{"client_email": "a"}'):
                    arq.write_text(conteudo)
                    with self.assertRaises(google.ErroCredencial) as c:
                        google.conta_servico()
                    self.assertNotIn("SECRETA", str(c.exception))
                arq.write_text('{"client_email": "a", "private_key": "k"}')
                self.assertEqual(google.conta_servico()["client_email"], "a")


class GscTest(unittest.TestCase):
    def rodar(self, args):
        ch = []
        with mock.patch.object(google, "_api", api_gsc(ch)):
            return google.desempenho_gsc(SITE, args, agora=HOJE), ch

    def test_last_28_days_fim_hoje_menos_3(self):
        r, ch = self.rodar({"dateRange": "last_28_days"})
        self.assertEqual((r["startDate"], r["endDate"]), ("2026-09-07", "2026-10-05"))
        corpo = ch[0][1]
        self.assertEqual((corpo["type"], corpo["dataState"], corpo["rowLimit"], corpo["dimensions"]), ("web", "all", 250, ["query"]))
        self.assertEqual(ch[0][2], google.ESCOPO_GSC)
        self.assertEqual((r["ok"], r["source"], r["dimensions"], r["rowCount"]), (True, "hub-conta-servico", ["query"], 4))

    def test_preset_3_meses_e_filtros_viram_grupo_and(self):
        r, ch = self.rodar({"dateRange": "last_3_months", "filters": [{"dimension": "page", "expression": "/a"}]})
        self.assertEqual(r["startDate"], "2026-07-05")
        self.assertEqual(ch[0][1]["dimensionFilterGroups"], [{"groupType": "and", "filters": [{"dimension": "page", "operator": "equals", "expression": "/a"}]}])

    def test_filtro_de_metricas_no_servidor(self):
        r, ch = self.rodar({"minImpressions": 50, "maxPosition": 20, "rowLimit": 10})
        self.assertEqual(ch[0][1]["rowLimit"], 1000)  # busca tudo para filtrar aqui
        self.assertEqual([x["keys"][0][-2:] for x in r["rows"]], ["a/", "/b", "/d"])
        self.assertEqual(r["rows"][0]["ctr"], 0.0333)
        self.assertEqual(r["rows"][0]["position"], 6.0)
        self.assertFalse(r["hasMore"])
        self.assertNotIn("nextStartRow", r)

    def test_has_more_quando_google_devolve_row_limit(self):
        r, _ = self.rodar({"rowLimit": 4})
        self.assertTrue(r["hasMore"])
        self.assertEqual(r["nextStartRow"], 4)

    def test_has_more_por_truncamento_do_filtro(self):
        r, _ = self.rodar({"minImpressions": 50, "rowLimit": 2})
        self.assertEqual(r["rowCount"], 2)
        self.assertTrue(r["hasMore"])
        self.assertEqual(r["nextStartRow"], 2)

    def test_discover_sem_position(self):
        r, _ = self.rodar({"type": "discover"})
        self.assertNotIn("position", r["rows"][0])

    def test_date_range_desconhecido(self):
        self.assertEqual(self.rodar({"dateRange": "ontem"})[0], {"ok": False, "reason": "invalid_request"})

    def test_sem_acesso_propaga(self):
        with mock.patch.object(google, "_api", side_effect=google.SemAcesso(SITE, "x@y")):
            with self.assertRaises(google.SemAcesso):
                google.desempenho_gsc(SITE, {}, agora=HOJE)

    def test_pedido_invalido(self):
        self.assertEqual(self.rodar({"startDate": "2026-01-01"})[0], {"ok": False, "reason": "invalid_request"})
        self.assertFalse(self.rodar({"dimensions": ["searchAppearance", "query"]})[0]["ok"])

    def test_sem_credencial_na_resposta(self):
        r, _ = self.rodar({})
        self.assertNotIn("private_key", json.dumps(r))


class Ga4Test(unittest.TestCase):
    def rodar(self, tipo, args=None):
        ch = []
        with mock.patch.object(google, "_api", api_ga4(ch)):
            return google.relatorio_ga4("123", args or {}, tipo, agora=HOJE), ch

    def test_ferramentas(self):
        self.assertEqual(len(google.FERRAMENTAS_GA4), 9)
        self.assertIn("get_google_analytics_organic_overview", google.FERRAMENTAS_GA4)

    def test_overview_organico(self):
        r, ch = self.rodar("get_google_analytics_organic_overview")
        runs = [c for c in ch if ":runReport" in c[0]]
        self.assertEqual(len(runs), 3)
        for url, corpo, escopo in runs:
            self.assertTrue(url.endswith("/properties/123:runReport"))
            self.assertEqual(escopo, google.ESCOPO_GA4)
            self.assertEqual(corpo["dimensionFilter"], {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"matchType": "EXACT", "value": "Organic Search"}}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["request"]["resolvedDateRange"], {"startDate": "2026-09-10", "endDate": "2026-10-07"})
        self.assertEqual(r["request"]["previousDateRange"], {"startDate": "2026-08-13", "endDate": "2026-09-09"})
        self.assertEqual(r["request"]["propertyTimeZone"], "America/Sao_Paulo")
        self.assertEqual(r["source"]["propertyId"], "properties/123")
        self.assertEqual(r["source"]["via"], "hub-conta-servico")
        self.assertEqual(r["current"]["sessions"], 100)
        self.assertEqual(r["comparison"]["sessions"]["absoluteChange"], 0)
        self.assertEqual(len(r["trend"]), 2)
        self.assertEqual(r["trend"][0]["date"], "20260901")

    def test_landing_pages(self):
        r, ch = self.rodar("get_google_analytics_organic_landing_pages", {"limit": 10})
        corpo = [c[1] for c in ch if ":runReport" in c[0]][0]
        self.assertEqual([d["name"] for d in corpo["dimensions"]], ["hostName", "landingPage"])
        self.assertEqual(corpo["limit"], "10")
        self.assertEqual(corpo["orderBys"], [{"metric": {"metricName": "sessions"}, "desc": True}])
        self.assertEqual((r["rowCount"], r["totalRowCount"], r["pageInfo"]["hasMore"]), (2, 2, False))
        self.assertEqual(r["rows"][0], {"hostName": "example.com", "landingPage": "/a", "sessions": 90, "activeUsers": 70, "engagedSessions": 60,
                                        "engagementRate": 0.8, "keyEvents": 0, "sessionKeyEventRate": 0, "transactions": 0, "purchaseRevenue": 0})
        self.assertEqual(r["request"]["channel"], "organic_search")

    def test_trafego_ignora_canal_organico(self):
        r, ch = self.rodar("get_google_analytics_traffic_acquisition", {"breakdown": "channel_group"})
        corpo = [c[1] for c in ch if ":runReport" in c[0]][0]
        self.assertIsNone(corpo.get("dimensionFilter"))
        self.assertEqual(r["request"]["channel"], "all")

    def test_erros_e_datas(self):
        r, _ = self.rodar("get_google_analytics_organic_landing_pages", {"startDate": "2026-10-01"})
        self.assertEqual((r["status"], r["error"]["code"]), ("error", "validation_error"))
        r, _ = self.rodar("get_google_analytics_organic_landing_pages", {"startDate": "2026-10-01", "endDate": "2026-10-20"})
        self.assertEqual(r["warnings"], ["end_date_clamped"])
        self.assertEqual(r["request"]["resolvedDateRange"]["endDate"], "2026-10-07")

    def test_403_propaga_sem_acesso(self):
        with mock.patch.object(google, "_api", side_effect=google.SemAcesso("123", "x@y")):
            for tipo in google.FERRAMENTAS_GA4:
                with self.assertRaises(google.SemAcesso, msg=tipo):
                    google.relatorio_ga4("123", {}, tipo, agora=HOJE)

    def test_404_continua_propriedade_inacessivel(self):
        erro = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch.object(google, "_api", side_effect=erro):
            r = google.relatorio_ga4("123", {}, "get_google_analytics_organic_landing_pages", agora=HOJE)
        self.assertEqual(r["error"]["code"], "ga4_property_inaccessible")

    def test_token_recusado_nao_vira_erro_de_relatorio(self):
        erro = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 400, "Bad Request", {}, None)
        with mock.patch.object(google, "conta_servico", return_value={"client_email": "x@y", "private_key": "k"}), \
                mock.patch.object(google, "_tokens", {}), mock.patch("urllib.request.urlopen", side_effect=erro), \
                mock.patch("cryptography.hazmat.primitives.serialization.load_pem_private_key") as chave:
            chave.return_value.sign.return_value = b"assinatura"
            with self.assertRaises(google.ErroCredencial) as c:
                google.relatorio_ga4("123", {}, "get_google_analytics_organic_landing_pages", agora=HOJE)
        self.assertNotIn("private_key", str(c.exception))

    def test_limite_invalido_antes_de_chamar_o_google(self):
        with mock.patch.object(google, "_api", side_effect=AssertionError("não devia chamar")):
            r = google.relatorio_ga4("123", {"limit": 0}, "get_google_analytics_organic_landing_pages", agora=HOJE)
        self.assertEqual(r["error"]["code"], "validation_error")


class OportunidadesTest(unittest.TestCase):
    def test_une_gsc_e_ga4(self):
        ch = []
        def falso(url, propriedade, corpo=None, escopo=google.ESCOPO_GSC):
            return api_gsc(ch)(url, propriedade, corpo, escopo) if "searchconsole" in url else api_ga4(ch)(url, propriedade, corpo, escopo)
        with mock.patch.object(google, "_api", falso):
            r = google.oportunidades_busca(SITE, "123", {}, agora=HOJE)
        gsc = [c for c in ch if "searchAnalytics" in c[0]][0][1]
        self.assertEqual((gsc["startDate"], gsc["endDate"], gsc["dataState"], gsc["dimensions"]), ("2026-09-08", "2026-10-05", "final", ["page"]))
        self.assertEqual((r["status"], r["rowCount"], r["totalCandidateRows"]), ("ok", 3, 3))
        self.assertEqual([(x["page"][-2:], x["joinStatus"], x["score"]) for x in r["rows"]], [("a/", "joined", 100), ("/d", "joined", 0), ("/b", "gsc_only", None)])
        self.assertEqual(r["rows"][0]["normalizedPage"], "example.com/a")
        self.assertEqual(r["rows"][0]["ga4"]["engagementRate"], 0.8)
        self.assertEqual(r["rows"][0]["scoreComponents"], {"demand": 1, "businessValue": 1, "reachability": 1})
        self.assertTrue(r["scoring"]["engagementFallback"])
        self.assertEqual(r["coverage"], {"gscRowsConsidered": 4, "ga4RowsConsidered": 2, "matchedRows": 2, "unmatchedGscRows": 1, "unmatchedGa4Rows": 0})
        self.assertEqual(r["warnings"], ["source_time_zones_differ"])
        self.assertEqual(r["source"]["searchConsoleSiteUrl"], SITE)

    def test_sem_acesso_do_gsc_propaga(self):
        def falso(url, propriedade, corpo=None, escopo=google.ESCOPO_GSC):
            if "searchconsole" in url:
                raise google.SemAcesso(SITE, "x@y")
            return GA4["propriedade"]
        with mock.patch.object(google, "_api", falso):
            with self.assertRaises(google.SemAcesso):
                google.oportunidades_busca(SITE, "123", {}, agora=HOJE)


class InspecaoTest(unittest.TestCase):
    def test_sem_acesso_propaga(self):
        with mock.patch.object(google, "_api", side_effect=google.SemAcesso(SITE, "x@y")):
            with self.assertRaises(google.SemAcesso):
                google.inspecionar_urls(SITE, {"urls": ["https://example.com/a"]})

    def test_resultados_e_erro_por_url(self):
        ch = []
        with mock.patch.object(google, "_api", api_gsc(ch)):
            r = google.inspecionar_urls(SITE, {"urls": ["https://example.com/a", "https://example.com/ruim"], "languageCode": "pt-BR"})
        self.assertTrue(r["ok"])
        self.assertEqual(ch[0][0], "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect")
        self.assertEqual(ch[0][1], {"siteUrl": SITE, "inspectionUrl": "https://example.com/a", "languageCode": "pt-BR"})
        self.assertEqual(r["results"][0]["result"]["indexStatusResult"]["verdict"], "PASS")
        self.assertIsNone(r["results"][1]["result"])
        self.assertIn("400", r["results"][1]["error"])
        self.assertEqual(r["source"], "hub-conta-servico")

    def test_limite_de_urls(self):
        self.assertEqual(google.inspecionar_urls(SITE, {"urls": []}), {"ok": False, "reason": "invalid_request"})


class TestContaServicoCredencial(unittest.TestCase):
    def test_le_de_credentials_directory(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "google-conta-servico.json").write_text('{"client_email": "a@b", "private_key": "k"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": d}):
                self.assertEqual(google.conta_servico()["client_email"], "a@b")

    def test_padrao_vazio_do_systemd_vira_erro_credencial(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "google-conta-servico.json").write_text("{}", encoding="utf-8")  # SetCredential do .service
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": d}):
                with self.assertRaises(google.ErroCredencial):
                    google.conta_servico()

    def test_pem_malformado_vira_erro_credencial(self):
        google._tokens.clear()
        with mock.patch.object(google, "conta_servico", return_value={"client_email": "a@b", "private_key": "NAO-E-PEM"}):
            with self.assertRaises(google.ErroCredencial) as c:
                google.token()
        self.assertNotIn("NAO-E-PEM", str(c.exception))
        with mock.patch.object(google, "conta_servico", return_value={"client_email": "a@b"}):
            with self.assertRaises(google.ErroCredencial):
                google.token()

    def test_arquivo_ilegivel_vira_erro_credencial(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "google-conta-servico.json").write_text("lixo", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": d}):
                with self.assertRaises(google.ErroCredencial):
                    google.conta_servico()


if __name__ == "__main__":
    unittest.main()
