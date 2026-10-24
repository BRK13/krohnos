# tests/test_impacto.py
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from hub import impacto


class ImpactoTest(unittest.TestCase):
    def test_delta_compara_14_dias_antes_e_depois(self):
        serie = [(f"2026-10-{d:02d}", 10 if d < 15 else 20, 100) for d in range(1, 29)]
        r = impacto.delta(serie, "2026-10-15", dias=14)
        self.assertEqual((r["cliques_antes"], r["cliques_depois"]), (140, 280))
        self.assertAlmostEqual(r["variacao_cliques"], 1.0)

    def test_sem_dado_antes_nao_inventa_nota(self):
        r = impacto.delta([("2026-10-20", 5, 50)], "2026-10-15", dias=14)
        self.assertIsNone(r["variacao_cliques"])

    def test_notas_media_por_categoria_e_site(self):
        e = [{"categoria": "buraco", "projeto": "Cliente A", "variacao_cliques": 1.0},
             {"categoria": "buraco", "projeto": "Cliente A", "variacao_cliques": 0.0},
             {"categoria": "seo-fix", "projeto": "Cliente A", "variacao_cliques": None}]
        self.assertEqual(impacto.notas(e), {"buraco": {"Cliente A": 0.5}})

    def test_estreias_media_de_cliques_depois_sem_historico(self):
        e = [{"categoria": "pagina-nova", "projeto": "Cliente A", "cliques_antes": 0, "cliques_depois": 10},
             {"categoria": "pagina-nova", "projeto": "Cliente A", "cliques_antes": 0, "cliques_depois": 4},
             {"categoria": "seo-fix", "projeto": "Cliente A", "cliques_antes": 5, "cliques_depois": 9},
             {"categoria": "buraco", "projeto": "Cliente A", "variacao_cliques": None}]
        self.assertEqual(impacto.estreias(e), {"pagina-nova": {"Cliente A": 7.0}})


class SiteDoProjetoTest(unittest.TestCase):
    def test_projetos_do_linear_ligam_ao_site_certo(self):
        ids = {p: (impacto.site_do_projeto(p) or {}).get("id") for p in
               ("Cliente A", "Cliente F", "Cliente G", "ClienteJ", "Cliente C", "Nimbus")}
        self.assertEqual(ids, {"Cliente A": "fsite06", "Cliente F": "clientei", "Cliente G": "psite15",
                               "ClienteJ": "clientej", "Cliente C": "asite01", "Nimbus": None})

    def test_url_da_linha_medir_depois(self):
        d = "**O que fazer:** x\n**Medir depois:** site-b.exemplo.com/dores/ciatico, consulta \"dor ciático\""
        self.assertEqual(impacto.url_medir(d, "site-b.exemplo.com"), "https://site-b.exemplo.com/dores/ciatico")
        self.assertEqual(impacto.url_medir("**Medir depois:** `/blog/x` (cliques)", "site-b.exemplo.com"),
                         "https://site-b.exemplo.com/blog/x")
        self.assertEqual(impacto.url_medir("**Medir depois:** https://site-b.exemplo.com/.", "site-b.exemplo.com"),
                         "https://site-b.exemplo.com/")
        self.assertEqual(impacto.url_medir("**Medir depois:** [site-b.exemplo.com/dores](https://site-b.exemplo.com/dores)",
                                           "site-b.exemplo.com"), "https://site-b.exemplo.com/dores")
        self.assertEqual(impacto.url_medir("**Medir depois:**\n- site-b.exemplo.com/x", "site-b.exemplo.com"),
                         "https://site-b.exemplo.com/x")
        self.assertIsNone(impacto.url_medir("sem a linha", "site-b.exemplo.com"))


class MedirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "claude").mkdir()
        desp = [{"issue": i, "projeto": "Cliente A", "categoria": "seo-fix", "em": em} for i, em in
                [("KRN-1", "2026-09-01T12:00:00+00:00"), ("KRN-2", "2026-09-01T12:00:00+00:00"),
                 ("KRN-3", "2026-09-01T12:00:00+00:00"), ("KRN-4", "2026-10-01T12:00:00+00:00")]]
        (self.base / "claude" / "esteira.json").write_text(json.dumps({"despachadas": desp}))
        anterior = {"entregas": [{"issue": "KRN-9", "projeto": "Cliente C", "categoria": "buraco",
                                  "em": "2026-08-01T00:00:00+00:00", "variacao_cliques": 0.5}]}
        (self.base / "claude" / "esteira-impacto.json").write_text(json.dumps(anterior))
        self.estados = {"KRN-1": "completed", "KRN-2": "started", "KRN-3": "canceled", "KRN-4": "completed"}
        self.gsc_args = []
        self.gsc_sites = []

    def tearDown(self):
        self.tmp.cleanup()

    def gql(self, q, **v):
        return {"issue": {"description": "**Medir depois:** site-b.exemplo.com/x", "state": {"type": self.estados[v["id"]]}}}

    def gsc(self, site_url, args):
        self.gsc_args.append(args)
        self.gsc_sites.append(site_url)
        return {"rows": [{"keys": [f"2026-08-{d:02d}"], "clicks": 1, "impressions": 10} for d in range(18, 32)] +
                        [{"keys": [f"2026-09-{d:02d}"], "clicks": 3, "impressions": 20} for d in range(1, 15)]}

    def test_mede_so_concluida_madura_e_acumula_historico(self):
        medidas = impacto.medir(hoje=dt.date(2026, 10, 9), base_dados=self.base, gql=self.gql, gsc=self.gsc)
        self.assertEqual([m["issue"] for m in medidas], ["KRN-1"])
        self.assertEqual(self.gsc_sites[0], "sc-domain:site-b.exemplo.com")
        self.assertNotIn("projectId", self.gsc_args[0])
        self.assertEqual(self.gsc_args[0]["filters"][0]["expression"], "https://site-b.exemplo.com/x")
        self.assertEqual((self.gsc_args[0]["startDate"], self.gsc_args[0]["endDate"]), ("2026-08-18", "2026-09-14"))
        salvo = json.loads((self.base / "claude" / "esteira-impacto.json").read_text())
        por = {e["issue"]: e for e in salvo["entregas"]}
        self.assertEqual(sorted(por), ["KRN-1", "KRN-2", "KRN-3", "KRN-4", "KRN-9"])
        self.assertEqual((por["KRN-1"]["cliques_antes"], por["KRN-1"]["cliques_depois"]), (14, 42))
        self.assertNotIn("variacao_cliques", por["KRN-2"])  # aberta: espera
        self.assertIsNone(por["KRN-3"]["variacao_cliques"])  # cancelada: nunca mede
        self.assertNotIn("variacao_cliques", por["KRN-4"])  # nova demais
        self.assertEqual(salvo["notas"], {"seo-fix": {"Cliente A": 2.0}, "buraco": {"Cliente C": 0.5}})

    def test_erro_no_gsc_deixa_pendente_para_a_proxima(self):
        def falha(site_url, args):
            raise RuntimeError("Google fora")
        self.assertEqual(impacto.medir(hoje=dt.date(2026, 10, 9), base_dados=self.base, gql=self.gql, gsc=falha), [])
        salvo = json.loads((self.base / "claude" / "esteira-impacto.json").read_text())
        self.assertNotIn("variacao_cliques", {e["issue"]: e for e in salvo["entregas"]}["KRN-1"])
