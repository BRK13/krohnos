import contextlib, datetime as dt, io, tempfile, unittest
from pathlib import Path
from unittest import mock

from hub import armazem, bi_historico, google

HOJE = dt.date(2026, 10, 9)
SITE = {"id": "asite01", "ids": {"gsc": "sc-domain:asite01-com.example", "ga4": "123456"}}


def _d(s):
    return dt.date.fromisoformat(s)


def _dias(ini, fim):
    d = _d(ini)
    while d <= _d(fim):
        yield d
        d += dt.timedelta(days=1)


class Google:
    """consultar falso: um dia de dado por dia pedido, a partir de `desde`; registra cada chamada."""

    def __init__(self, desde="2000-01-01", falha=(), erro=None):
        self.desde, self.falha, self.erro, self.chamadas = desde, falha, erro, []

    def __call__(self, fonte, prop, corpo):
        if prop in self.falha:
            raise self.erro or google.SemAcesso(prop, "sa@exemplo")
        if fonte == "gsc":
            ini, fim = corpo["startDate"], corpo["endDate"]
            self.chamadas.append(("gsc", ini, fim))
            return [{"keys": [d.isoformat()], "clicks": 2, "impressions": 0 if d.day == 1 else 10, "ctr": 0.2, "position": 4.5}
                    for d in _dias(max(ini, self.desde), fim)]
        r = corpo["dateRanges"][0]
        dims = tuple(x["name"] for x in corpo["dimensions"])
        self.chamadas.append((dims, r["startDate"], r["endDate"]))
        linhas = []
        for d in _dias(max(r["startDate"], self.desde), r["endDate"]):
            v = d.strftime("%Y%m%d")
            if dims == ("date",):
                linhas.append({"dimensionValues": [{"value": v}], "metricValues": [{"value": "10"}, {"value": "6"}, {"value": "1"}]})
            elif dims == ("date", "sessionSourceMedium"):
                linhas += [{"dimensionValues": [{"value": v}, {"value": o}], "metricValues": [{"value": "5"}, {"value": "3"}, {"value": "0"}]}
                           for o in ("google / organic", "(direct) / (none)")]
            elif d.day == 2:  # IA só no dia 2 de cada mês
                linhas += [{"dimensionValues": [{"value": v}, {"value": o}], "metricValues": [{"value": s}]}
                           for o, s in (("chatgpt.com", "3"), ("chat.openai.com", "1"), ("www.perplexity.ai", "2"),
                                        ("google", "50"), ("notclaude.ai", "9"))]
            else:
                linhas.append({"dimensionValues": [{"value": v}, {"value": "google"}], "metricValues": [{"value": "7"}]})
        return linhas


class HistoricoTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def rodar(self, g, site=SITE, dias=None):
        with mock.patch.object(bi_historico, "consultar", g):
            return bi_historico.historico_site(site, HOJE, dias, self.d)

    def checa_contiguo(self, chamadas, fim, piso, tamanho):
        self.assertEqual(chamadas[0][2], fim)
        self.assertEqual(chamadas[-1][1], piso)
        for (_, ini, f), (_, ini2, f2) in zip(chamadas, chamadas[1:]):
            self.assertEqual(_d(f2) + dt.timedelta(days=1), _d(ini))  # sem buraco nem sobreposição
        self.assertTrue(all((_d(f) - _d(i)).days + 1 <= tamanho for _, i, f in chamadas))

    def test_blocos_contiguos_e_cortados_no_limite(self):
        g = Google()
        contagem, erros, sem_acesso = self.rodar(g)
        self.assertEqual((erros, sem_acesso), ([], []))
        gsc = [c for c in g.chamadas if c[0] == "gsc"]
        self.checa_contiguo(gsc, "2026-10-06", "2025-06-09", 90)       # hoje-3 .. 16 meses
        for dims, tam in ((("date",), 90), (("date", "sessionSourceMedium"), 30), (("date", "sessionSource"), 30)):
            self.checa_contiguo([c for c in g.chamadas if c[0] == dims], "2026-10-08", "2025-08-09", tam)  # ontem .. 14 meses
        self.assertEqual(contagem["gsc"], (_d("2026-10-06") - _d("2025-06-09")).days + 1)
        self.assertEqual(contagem["ga4"], (_d("2026-10-08") - _d("2025-08-09")).days + 1)
        self.assertEqual(contagem["ia"], contagem["ga4"])  # todo dia com dado no GA4 tem total de IA (0 se não houve)

    def test_para_depois_de_dois_blocos_vazios(self):
        g = Google(desde="2026-08-01")
        self.rodar(g)
        gsc = [c for c in g.chamadas if c[0] == "gsc"]
        # 07-09..10-06 tem dado, 04-10..07-08 vazio, 01-10..04-09 vazio: para
        self.assertEqual([c[1] for c in gsc], ["2026-07-09", "2026-04-10", "2026-01-10"])
        self.assertEqual(armazem.soma("asite01", "gsc", "cliques", "2025-01-01", "2026-12-31", base_dados=self.d)[1], 67)

    def test_um_bloco_vazio_no_meio_nao_para(self):
        g = Google()
        orig = g.__call__
        vazio = lambda f, p, c: [] if f == "gsc" and c["endDate"] == "2026-07-08" else orig(f, p, c)
        self.rodar(vazio)
        self.assertEqual(armazem.serie("asite01", "gsc", "cliques", "2025-06-09", "2025-06-09", base_dados=self.d), [("2025-06-09", 2.0)])

    def test_origem_e_ia(self):
        self.rodar(Google())
        q = lambda f, m, dim, dia="2026-09-02": armazem.serie("asite01", f, m, dia, dia, dim, self.d)[0][1]
        self.assertEqual(q("ga4", "sessoes", ""), 10.0)
        self.assertEqual(q("ga4", "sessoes_engajadas", "google / organic"), 3.0)
        self.assertEqual(q("ga4", "conversoes", "(direct) / (none)"), 0.0)
        self.assertEqual(q("ia", "visitas", "chatgpt"), 4.0)          # chatgpt.com + chat.openai.com
        self.assertEqual(q("ia", "visitas", "perplexity"), 2.0)       # www.perplexity.ai termina em .perplexity.ai
        self.assertEqual(q("ia", "visitas", ""), 6.0)                 # total do dia, sem google nem notclaude.ai
        self.assertIsNone(q("ia", "visitas", "claude"))
        self.assertEqual(q("ia", "visitas", "", "2026-09-03"), 0.0)    # dia com GA4 e sem IA: zero, não lacuna
        self.assertIsNone(q("ia", "visitas", "chatgpt", "2026-09-03"))  # por assistente só quando > 0
        self.assertIsNone(q("gsc", "posicao", "", "2026-09-01"))       # sem impressão: sem posição
        self.assertEqual(q("gsc", "ctr", "", "2026-09-01"), 0.2)

    def test_paginacao_ga4(self):
        with mock.patch.object(bi_historico, "LIMITE_GA4", 2), mock.patch.object(bi_historico, "consultar") as c:
            c.side_effect = [[1, 2], [3, 4], [5]]
            self.assertEqual(bi_historico._ga4_relatorio("1", ["date"], ["sessions"], "a", "b"), [1, 2, 3, 4, 5])
        self.assertEqual([x.args[2]["offset"] for x in c.call_args_list], [0, 2, 4])

    def test_dias_so_blocos_recentes(self):
        g = Google()
        contagem, _, _ = self.rodar(g, dias=5)
        self.assertEqual(g.chamadas[0], ("gsc", "2026-10-02", "2026-10-06"))
        self.assertEqual([c[1:] for c in g.chamadas[1:]], [("2026-10-04", "2026-10-08")] * 3)
        self.assertEqual((contagem["gsc"], contagem["ga4"]), (5, 5))

    def test_site_sem_gsc_pula_a_fonte(self):
        g = Google()
        contagem, erros, _ = self.rodar(g, {"id": "fc", "ids": {"gsc": None, "ga4": "9"}}, dias=3)
        self.assertNotIn("gsc", contagem)
        self.assertFalse([c for c in g.chamadas if c[0] == "gsc"])


class CliTest(unittest.TestCase):
    LISTA = [{"id": "a", "ids": {"gsc": "sc-domain:a-com.example", "ga4": "1"}},
             {"id": "b", "ids": {"gsc": "sc-domain:b-com.example", "ga4": "2"}},
             {"id": "c", "ids": {"gsc": None, "ga4": None}}]

    def rodar(self, g):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bi_historico, "consultar", g), \
                mock.patch.object(bi_historico.sites, "carregar", return_value=self.LISTA), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bi_historico.main(["--site", "todos", "--dias", "3"], HOJE, tempfile.mkdtemp())
        return rc, out.getvalue().splitlines(), err.getvalue()

    def test_sem_acesso_e_sem_fonte_nao_sao_falha(self):
        rc, out, err = self.rodar(Google(falha=("sc-domain:a-com.example",)))
        self.assertEqual(rc, 0)  # sem acesso não deixa a rotina vermelha (mas o site fica sem marcador)
        self.assertEqual(err, "a gsc: sem acesso (dar leitura a sa@exemplo)\n")
        self.assertEqual(out, ["a: ga4 3 dias, ia 3 dias", "b: gsc 3 dias, ga4 3 dias, ia 3 dias", "c: sem Search Console e sem GA4, nada a buscar"])

    def test_erro_de_verdade_falha_e_nao_para_os_outros(self):
        rc, out, err = self.rodar(Google(falha=("sc-domain:a-com.example",), erro=google._ErroGa4("x", "cota esgotada")))
        self.assertEqual(rc, 1)  # próxima rodada tenta de novo
        self.assertIn("erro: a gsc: _ErroGa4: cota esgotada", err)
        self.assertEqual(out, ["a: ga4 3 dias, ia 3 dias (com falha, ver erros)", "b: gsc 3 dias, ga4 3 dias, ia 3 dias",
                               "c: sem Search Console e sem GA4, nada a buscar"])

    def auto(self, g, base):
        out = io.StringIO()
        with mock.patch.object(bi_historico, "consultar", g), \
                mock.patch.object(bi_historico.sites, "carregar", return_value=self.LISTA[:2]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = bi_historico.main(["--site", "todos", "--auto"], HOJE, base)
        return rc, out.getvalue().splitlines()

    def test_auto_sem_marcador_faz_completo_e_cria_marcador(self):
        base, g = tempfile.mkdtemp(), Google()
        rc, out = self.auto(g, base)
        self.assertEqual(rc, 0)
        self.assertTrue(all(l.endswith("(histórico completo)") for l in out))
        self.assertIn(("gsc", "2025-06-09", "2025-07-13"), g.chamadas)  # chegou ao piso de 16 meses
        for i in ("a", "b"):
            self.assertTrue(bi_historico.marcador(i, base).exists())
            self.assertEqual(bi_historico.marcador(i, base), Path(base) / "bi" / "historico" / f"{i}.ok")

    def test_auto_com_marcador_so_5_dias(self):
        base, g = tempfile.mkdtemp(), Google()
        self.auto(Google(), base)
        rc, out = self.auto(g, base)
        self.assertEqual((rc, out), (0, ["a: gsc 5 dias, ga4 5 dias, ia 5 dias", "b: gsc 5 dias, ga4 5 dias, ia 5 dias"]))
        self.assertEqual({c[1] for c in g.chamadas if c[0] == "gsc"}, {"2026-10-02"})

    def test_auto_sem_acesso_ou_erro_nao_cria_marcador(self):
        base = tempfile.mkdtemp()
        self.auto(Google(falha=("sc-domain:a-com.example",)), base)
        self.assertFalse(bi_historico.marcador("a", base).exists())
        self.assertTrue(bi_historico.marcador("b", base).exists())
        rc, _ = self.auto(Google(falha=("1",), erro=google._ErroGa4("x", "cota esgotada")), base)
        self.assertEqual(rc, 1)
        self.assertFalse(bi_historico.marcador("a", base).exists())
        g = Google()
        self.auto(g, base)  # acesso chegou: agora sim o completo e o marcador
        self.assertTrue(bi_historico.marcador("a", base).exists())
        self.assertIn(("gsc", "2025-06-09", "2025-07-13"), g.chamadas)

    def test_site_unico_e_desconhecido(self):
        lista = [{"id": "a", "ids": {"gsc": None, "ga4": "1"}}]
        with mock.patch.object(bi_historico, "consultar", Google()), \
                mock.patch.object(bi_historico.sites, "carregar", return_value=lista), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bi_historico.main(["--site", "a", "--dias", "1"], HOJE, tempfile.mkdtemp()), 0)
            with self.assertRaises(SystemExit):
                bi_historico.main(["--site", "zzz"], HOJE)
            with self.assertRaises(SystemExit):
                bi_historico.main(["--site", "a", "--dias", "0"], HOJE)
            with self.assertRaises(SystemExit):
                bi_historico.main(["--site", "a", "--dias", "5", "--auto"], HOJE)


if __name__ == "__main__":
    unittest.main()
