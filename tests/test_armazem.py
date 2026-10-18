import os, tempfile, unittest
from hub import armazem


def f(dia, valor, dim="", negocio="asite01", fonte="gsc", metrica="cliques"):
    return {"dia": dia, "negocio": negocio, "fonte": fonte, "metrica": metrica, "dim": dim, "valor": valor}


class ArmazemTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_upsert_e_serie_com_lacuna(self):
        armazem.gravar([f("2026-10-01", 10), f("2026-10-03", 30)], self.d)
        armazem.gravar([f("2026-10-01", 12)], self.d)
        self.assertEqual(armazem.serie("asite01", "gsc", "cliques", "2026-10-01", "2026-10-03", base_dados=self.d),
                         [("2026-10-01", 12.0), ("2026-10-02", None), ("2026-10-03", 30.0)])
        self.assertEqual(armazem.soma("asite01", "gsc", "cliques", "2026-10-01", "2026-10-03", base_dados=self.d), (42.0, 2))

    def test_dims_e_ultimo_sem_total_soma_as_dims(self):
        g = dict(negocio="fsite05", fonte="ga4", metrica="sessoes")
        armazem.gravar([f("2026-10-01", 5, "google / organic", **g), f("2026-10-02", 7, "google / organic", **g),
                        f("2026-10-02", 3, "(direct) / (none)", **g)], self.d)
        self.assertEqual(armazem.dims("fsite05", "ga4", "sessoes", "2026-10-01", "2026-10-02", base_dados=self.d),
                         [("google / organic", 12.0), ("(direct) / (none)", 3.0)])
        self.assertEqual(armazem.ultimo("fsite05", "ga4", "sessoes", base_dados=self.d), ("2026-10-02", 10.0))

    def test_ultimo_com_total_usa_so_o_total(self):
        armazem.gravar([f("2026-10-01", 99), f("2026-10-02", 20), f("2026-10-02", 12, "a"), f("2026-10-02", 8, "b")], self.d)
        self.assertEqual(armazem.ultimo("asite01", "gsc", "cliques", base_dados=self.d), ("2026-10-02", 20.0))
        self.assertIsNone(armazem.ultimo("asite01", "gsc", "nada", base_dados=self.d))

    def test_fato_invalido_e_recusado(self):
        with self.assertRaises(ValueError):
            armazem.gravar([f("01/10/2026", 1)], self.d)
        for v in (None, "abc"):
            with self.assertRaises(ValueError):
                armazem.gravar([f("2026-10-01", v)], self.d)
        with self.assertRaises(ValueError):
            armazem.gravar([{"dia": "2026-10-01", "negocio": "asite01", "fonte": "gsc", "metrica": "cliques"}], self.d)

    @unittest.skipIf(os.geteuid() == 0, "root lê mesmo sem permissão")
    def test_leitura_com_banco_e_pasta_so_leitura(self):
        armazem.gravar([f("2026-10-01", 5, "a"), f("2026-10-02", 7)], self.d)
        p = armazem.caminho(self.d)
        os.chmod(p, 0o444); os.chmod(p.parent, 0o555)
        try:
            self.assertEqual(armazem.serie("asite01", "gsc", "cliques", "2026-10-02", "2026-10-02", base_dados=self.d), [("2026-10-02", 7.0)])
            self.assertEqual(armazem.soma("asite01", "gsc", "cliques", "2026-10-01", "2026-10-02", base_dados=self.d), (7.0, 1))
            self.assertEqual(armazem.ultimo("asite01", "gsc", "cliques", base_dados=self.d), ("2026-10-02", 7.0))
            self.assertEqual(armazem.dims("asite01", "gsc", "cliques", "2026-10-01", "2026-10-02", base_dados=self.d), [("a", 5.0)])
            self.assertEqual(os.listdir(p.parent), ["metricas.db"])  # nada de -wal/-shm/-journal
        finally:
            os.chmod(p.parent, 0o755); os.chmod(p, 0o644)

    @unittest.skipIf(os.geteuid() == 0, "root escreve mesmo sem permissão")
    def test_leitura_antes_do_primeiro_banco_nao_cria_nada(self):
        os.chmod(self.d, 0o555)
        try:
            self.assertEqual(armazem.serie("asite01", "gsc", "cliques", "2026-10-01", "2026-10-01", base_dados=self.d), [("2026-10-01", None)])
            self.assertEqual(armazem.soma("asite01", "gsc", "cliques", "2026-10-01", "2026-10-01", base_dados=self.d), (0.0, 0))
            self.assertIsNone(armazem.ultimo("asite01", "gsc", "cliques", base_dados=self.d))
            self.assertEqual(armazem.dims("asite01", "gsc", "cliques", "2026-10-01", "2026-10-01", base_dados=self.d), [])
        finally:
            os.chmod(self.d, 0o755)
