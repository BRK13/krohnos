import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest import mock

from hub import mcp_proxy, sites, snapshots_api

CONEXAO = http.client.HTTPConnection


class SnapshotsApiTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        p = mock.patch.dict(os.environ, {"HUB_DATA": str(self.base)})
        p.start()
        self.addCleanup(p.stop)
        for dia, v in (("2026-10-20", 1), ("2026-10-21", 2)):
            for site in ("fsite06", "asite01"):
                d = self.base / "snapshots" / dia
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{site}-gsc.json").write_text(json.dumps({"rows": [{"keys": [dia], "clicks": v}], "site": site}, indent=2))
        self.tok = snapshots_api.emitir("fsite06")
        self.srv = mcp_proxy.criar_servidor("127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, caminho, token="", host=None, **h):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=10)
        hs = dict(h)
        if token:
            hs["Authorization"] = "Bearer " + token
        if host:
            hs["Host"] = host
        try:
            c.request("GET", caminho, headers=hs)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_devolve_os_bytes_do_arquivo_do_dia_e_o_latest(self):
        arq = self.base / "snapshots/2026-10-20/fsite06-gsc.json"
        st, corpo = self.get("/api/snapshots/fsite06/2026-10-20/gsc", self.tok)
        self.assertEqual((st, corpo), (200, arq.read_bytes()))
        st, corpo = self.get("/api/snapshots/fsite06/latest/gsc", self.tok)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(corpo)["rows"][0]["clicks"], 2)

    def test_token_so_abre_o_proprio_site(self):
        self.assertEqual(self.get("/api/snapshots/asite01/latest/gsc", self.tok)[0], 401)
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc")[0], 401)
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", "snap_errado")[0], 401)
        self.assertEqual(self.get("/api/snapshots/asite01/latest/gsc", snapshots_api.emitir("asite01"))[0], 200)

    def test_host_remoto_passa_com_token_mas_origin_de_navegador_nao(self):
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", self.tok, host="hub.exemplo.com")[0], 200)
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", self.tok, Origin="https://x.com")[0], 403)

    def test_caminho_invalido_ou_sem_arquivo(self):
        for c in ("/api/snapshots/fsite06/latest/outro", "/api/snapshots/fsite06/../gsc", "/api/snapshots/fsite06/2026-10-20",
                  "/api/snapshots/..%2Fx/latest/gsc", "/api/snapshots/fsite06/20261020/gsc", "/api/snapshots/fsite06/latest/gsc/x"):
            self.assertEqual(self.get(c, self.tok)[0], 404, c)
        self.assertEqual(self.get("/api/snapshots/fsite06/2026-01-01/gsc", self.tok)[0], 404)

    def test_so_get_e_revogar_fecha(self):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=10)
        c.request("POST", "/api/snapshots/fsite06/latest/gsc", body="{}", headers={"Authorization": "Bearer " + self.tok})
        self.assertEqual(c.getresponse().status, 404)
        c.close()
        snapshots_api.revogar("fsite06")
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", self.tok)[0], 401)

    def test_so_o_hash_fica_no_disco_e_emitir_de_novo_gira(self):
        txt = snapshots_api._arquivo_tokens().read_text()
        self.assertNotIn(self.tok, txt)
        novo = snapshots_api.emitir("fsite06")
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", self.tok)[0], 401)
        self.assertEqual(self.get("/api/snapshots/fsite06/latest/gsc", novo)[0], 200)


class ListenerSoSnapshotsTest(unittest.TestCase):
    """o listener da bridge do Dokploy só serve snapshots e leitura Google por token de site; /mcp não abre."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        p = mock.patch.dict(os.environ, {"HUB_DATA": str(self.base)})
        p.start()
        self.addCleanup(p.stop)
        d = self.base / "snapshots" / "2026-10-21"
        d.mkdir(parents=True)
        (d / "fsite06-gsc.json").write_text("{}")
        self.tok = snapshots_api.emitir("fsite06")
        self.srv = mcp_proxy.criar_servidor("127.0.0.1", 0, so_snapshots=True)
        threading.Thread(target=self.srv.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def pedir(self, metodo, caminho, corpo=None, **h):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            c.request(metodo, caminho, body=corpo, headers=h)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_mcp_e_o_resto_nao_abrem_nem_com_token(self):
        auth = {"Authorization": "Bearer " + self.tok}
        corpo = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(self.pedir("POST", "/mcp", corpo, **auth)[0], 404)
        self.assertEqual(self.pedir("POST", "/mcp", corpo)[0], 404)
        self.assertEqual(self.pedir("GET", "/mcp", **auth)[0], 404)
        for c in ("/", "/api/health", "/api/outra", "/mcp/x"):
            self.assertEqual(self.pedir("GET", c, **auth)[0], 404, c)

    def test_snapshot_continua_servido(self):
        self.assertEqual(self.pedir("GET", "/api/snapshots/fsite06/latest/gsc", Authorization="Bearer " + self.tok), (200, b"{}"))
        self.assertEqual(self.pedir("GET", "/api/snapshots/fsite06/latest/gsc")[0], 401)

    def test_recusa_escutar_em_todas_as_interfaces(self):
        with self.assertRaises(SystemExit):
            mcp_proxy.main(["--so-snapshots", "--host", "0.0.0.0"])


class GoogleCruPorPeriodoTest(unittest.TestCase):
    """Segundo GET: consulta crua do Google num período escolhido, propriedade vinda do cadastro do site."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        p = mock.patch.dict(os.environ, {"HUB_DATA": str(self.base)})
        p.start()
        self.addCleanup(p.stop)
        cad = {"fsite06": {"ids": {"gsc": "sc-domain:clientea-com.example", "ga4": "111111112"}}, "naoexiste": None}
        def site(id_):
            if not cad.get(id_):
                raise sites.SiteInvalido("sem site")
            return cad[id_]
        p2 = mock.patch("hub.sites.site", site)
        p2.start()
        self.addCleanup(p2.stop)
        self.tok = snapshots_api.emitir("fsite06")
        self.outro = snapshots_api.emitir("asite01")
        self.srv = mcp_proxy.criar_servidor("127.0.0.1", 0, so_snapshots=True)
        threading.Thread(target=self.srv.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, caminho, token=None):
        c = CONEXAO("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            c.request("GET", caminho, headers={"Authorization": "Bearer " + (token or self.tok)})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_gsc_chama_o_google_com_o_periodo_e_a_propriedade_do_cadastro(self):
        with mock.patch("hub.google._api", return_value={"rows": [{"keys": ["a"], "clicks": 3}]}) as api:
            st, corpo = self.get("/api/google/fsite06/gsc?de=2026-09-01&ate=2026-09-30&dimensoes=query,page&limite=50")
        self.assertEqual((st, json.loads(corpo)), (200, {"rows": [{"keys": ["a"], "clicks": 3}]}))
        url, prop, body = api.call_args.args[:3]
        self.assertEqual(unquote(url), "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain:clientea-com.example/searchAnalytics/query")
        self.assertEqual(prop, "sc-domain:clientea-com.example")
        self.assertEqual(body, {"startDate": "2026-09-01", "endDate": "2026-09-30", "dimensions": ["query", "page"], "rowLimit": 50})

    def test_ga4_manda_dimensoes_e_metricas_pedidas(self):
        with mock.patch("hub.google._ga", return_value={"rows": []}) as ga:
            st, _ = self.get("/api/google/fsite06/ga4?de=2026-09-01&ate=2026-09-30&dimensoes=pagePath&metricas=sessions,activeUsers&limite=5000")
        self.assertEqual(st, 200)
        url, prop, body = ga.call_args.args
        self.assertTrue(url.endswith("properties/111111112:runReport"))
        self.assertEqual(body, {"dateRanges": [{"startDate": "2026-09-01", "endDate": "2026-09-30"}], "dimensions": [{"name": "pagePath"}],
                                "metrics": [{"name": "sessions"}, {"name": "activeUsers"}], "limit": 5000})

    def test_token_de_outro_site_ou_sem_token_nao_le(self):
        q = "/api/google/fsite06/gsc?de=2026-09-01&ate=2026-09-30"
        with mock.patch("hub.google._api") as api:
            self.assertEqual(self.get(q, self.outro)[0], 401)
            self.assertEqual(self.get(q, "snap_errado")[0], 401)
        api.assert_not_called()

    def test_parametros_fora_da_regra_dao_400_sem_chamar_o_google(self):
        base = "/api/google/fsite06/gsc?"
        ruins = ("", "de=2026-09-30&ate=2026-09-01", "de=2026-9-1&ate=2026-09-30", "de=2024-01-01&ate=2026-09-30",
                 "de=2026-09-01&ate=2026-09-30&limite=0", "de=2026-09-01&ate=2026-09-30&limite=99999",
                 "de=2026-09-01&ate=2026-09-30&dimensoes=query;drop", "de=2026-09-01&ate=2026-09-30&dimensoes=naoexiste")
        with mock.patch("hub.google._api") as api:
            for q in ruins:
                self.assertEqual(self.get(base + q)[0], 400, q)
            self.assertEqual(self.get("/api/google/fsite06/ga4?de=2026-09-01&ate=2026-09-30&metricas=a-b")[0], 400)
        api.assert_not_called()

    def test_falha_do_google_vira_502_sem_vazar_detalhe(self):
        import urllib.error
        with mock.patch("hub.google._api", side_effect=urllib.error.URLError("segredo interno")):
            st, corpo = self.get("/api/google/fsite06/gsc?de=2026-09-01&ate=2026-09-30")
        self.assertEqual(st, 502)
        self.assertNotIn(b"segredo", corpo)

    def test_site_ou_tipo_inexistente(self):
        self.assertEqual(self.get("/api/google/fsite06/outro?de=2026-09-01&ate=2026-09-30")[0], 404)
        snapshots_api.emitir("naoexiste")
        self.assertEqual(self.get("/api/google/naoexiste/gsc?de=2026-09-01&ate=2026-09-30", snapshots_api.emitir("naoexiste"))[0], 404)

    def test_cru_sem_periodo_escolhido_continua_com_a_janela_de_dias(self):
        from hub import google
        with mock.patch("hub.google._api", return_value={}) as api:
            google.gsc_consultas_cru("sc-domain:x.com", dias=7, hoje=__import__("datetime").date(2026, 10, 20))
        self.assertEqual(api.call_args.args[2]["startDate"], "2026-10-11")

    def get_corpo(self, tipo, corpo, token=None):
        from urllib.parse import quote
        return self.get(f"/api/google/fsite06/{tipo}?corpo=" + quote(json.dumps(corpo)), token)

    def test_corpo_gsc_vai_ao_google_como_o_app_montou(self):
        corpo = {"startDate": "2026-09-01", "endDate": "2026-09-28", "dimensions": ["query"], "rowLimit": 20, "dataState": "final",
                 "type": "web", "startRow": 0, "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "contains", "expression": "/blog"}]}]}
        with mock.patch("hub.google._api", return_value={"rows": []}) as api:
            st, _ = self.get_corpo("gsc", corpo)
        self.assertEqual(st, 200)
        url, prop, body = api.call_args.args[:3]
        self.assertTrue(unquote(url).endswith("sc-domain:clientea-com.example/searchAnalytics/query"))
        self.assertEqual((prop, body), ("sc-domain:clientea-com.example", corpo))

    def test_corpo_gsc_so_com_periodo_da_os_totais(self):
        with mock.patch("hub.google._api", return_value={"rows": []}) as api:
            self.assertEqual(self.get_corpo("gsc", {"startDate": "2026-09-01", "endDate": "2026-09-28"})[0], 200)
        self.assertEqual(api.call_args.args[2], {"startDate": "2026-09-01", "endDate": "2026-09-28"})

    def test_corpo_ga4_com_ordem_e_filtro_vai_igual(self):
        corpo = {"dateRanges": [{"startDate": "2026-09-01", "endDate": "2026-09-28"}], "dimensions": [{"name": "eventName"}],
                 "metrics": [{"name": "eventCount"}], "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}],
                 "dimensionFilter": {"filter": {"fieldName": "eventName", "inListFilter": {"values": ["contato"]}}}, "limit": 50}
        with mock.patch("hub.google._ga", return_value={"rows": []}) as ga:
            self.assertEqual(self.get_corpo("ga4", corpo)[0], 200)
        url, prop, body = ga.call_args.args
        self.assertTrue(url.endswith("properties/111111112:runReport"))
        self.assertEqual(body, corpo)

    def test_corpo_ga4_aceita_limit_e_offset_como_string_de_digitos(self):
        corpo = {"dateRanges": [{"startDate": "2026-09-01", "endDate": "2026-09-28"}], "metrics": [{"name": "sessions"}], "limit": "50", "offset": "100"}
        with mock.patch("hub.google._ga", return_value={"rows": []}) as ga:
            self.assertEqual(self.get_corpo("ga4", corpo)[0], 200)
        self.assertEqual(ga.call_args.args[2], corpo)
        with mock.patch("hub.google._ga") as ga:
            self.assertEqual(self.get_corpo("ga4", {**corpo, "limit": "5x"})[0], 400)
            self.assertEqual(self.get_corpo("ga4", {**corpo, "limit": "999999999"})[0], 400)
        ga.assert_not_called()

    def test_ga4_agora_e_o_tempo_real(self):
        corpo = {"dimensions": [{"name": "countryId"}], "metrics": [{"name": "activeUsers"}],
                 "orderBys": [{"metric": {"metricName": "activeUsers"}, "desc": True}], "limit": 50}
        with mock.patch("hub.google._ga", return_value={"rows": []}) as ga:
            self.assertEqual(self.get_corpo("ga4-agora", corpo)[0], 200)
        url, _, body = ga.call_args.args
        self.assertTrue(url.endswith("properties/111111112:runRealtimeReport"))
        self.assertEqual(body, corpo)
        with mock.patch("hub.google._ga") as ga:
            self.assertEqual(self.get("/api/google/fsite06/ga4-agora?de=2026-09-01&ate=2026-09-28")[0], 400)
        ga.assert_not_called()

    def test_corpo_fora_da_regra_da_400_sem_chamar_o_google(self):
        per = {"startDate": "2026-09-01", "endDate": "2026-09-28"}
        rng = {"dateRanges": [per], "metrics": [{"name": "sessions"}]}
        ruins = [("gsc", {**per, "siteUrl": "sc-domain:outro-com.example"}), ("gsc", {"startDate": "2026-09-01"}),
                 ("gsc", {**per, "startDate": "2024-01-01"}), ("gsc", {**per, "rowLimit": 99999}), ("gsc", {**per, "dimensions": ["x"]}),
                 ("gsc", {**per, "dataState": "qualquer"}), ("gsc", [per]), ("ga4", {**rng, "property": "properties/1"}),
                 ("ga4", {"metrics": [{"name": "sessions"}]}), ("ga4", {**rng, "metrics": []}), ("ga4", {**rng, "metrics": [{"name": "a-b"}]}),
                 ("ga4", {**rng, "dateRanges": [{"startDate": "ontem", "endDate": "2026-09-28"}]}), ("ga4", {**rng, "limit": 0}),
                 ("ga4-agora", {"metrics": [{"name": "activeUsers"}], "dateRanges": [per]})]
        with mock.patch("hub.google._api") as api, mock.patch("hub.google._ga") as ga:
            for tipo, corpo in ruins:
                self.assertEqual(self.get_corpo(tipo, corpo)[0], 400, (tipo, corpo))
            self.assertEqual(self.get("/api/google/fsite06/gsc?corpo=nao-e-json")[0], 400)
            self.assertEqual(self.get("/api/google/fsite06/gsc?corpo=" + "a" * 9000)[0], 400)
        api.assert_not_called()
        ga.assert_not_called()

    def test_google_sem_acesso_vira_502_nos_dois_modos(self):
        from hub import google
        with mock.patch("hub.google._api", side_effect=google.SemAcesso("sc-domain:clientea-com.example", "sa@x")):
            self.assertEqual(self.get_corpo("gsc", {"startDate": "2026-09-01", "endDate": "2026-09-28"})[0], 502)
            self.assertEqual(self.get("/api/google/fsite06/gsc?de=2026-09-01&ate=2026-09-28")[0], 502)

    def test_corpo_com_token_de_outro_site_nao_le(self):
        with mock.patch("hub.google._api") as api:
            self.assertEqual(self.get_corpo("gsc", {"startDate": "2026-09-01", "endDate": "2026-09-28"}, self.outro)[0], 401)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestConcorrencia(unittest.TestCase):
    def test_emitir_simultaneo_nao_perde_sites(self):
        import tempfile
        import threading
        from unittest import mock
        from pathlib import Path
        from hub import snapshots_api as sa

        with tempfile.TemporaryDirectory() as t:
            with mock.patch.object(sa, "_arquivo_tokens", lambda: Path(t) / "tokens.json"):
                sites = [f"site{i}" for i in range(12)]
                ths = [threading.Thread(target=sa.emitir, args=(s,)) for s in sites]
                [x.start() for x in ths]
                [x.join() for x in ths]
                self.assertEqual(sorted(sa._hashes()), sorted(sites))
