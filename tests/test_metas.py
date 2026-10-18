import datetime as dt
import tempfile
import unittest
from unittest import mock

from hub import metas as M

T = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
SITES = [{"id": "fsite05", "repo": {"github": "example-org/cliente-a-v2", "base": "master"}}, {"id": "sem", "repo": None}]
PS = {"tipo": "pagespeed", "url": "https://f-com.example/", "metrica": "lcp_ms"}


class MetasTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch("hub.metas.sites.carregar", return_value=SITES)
        p.start()
        self.addCleanup(p.stop)

    def nova(self, **kw):
        kw.setdefault("abrir", lambda m: "KRN-800 https://l/x")
        return M.criar("home da fsite05 com LCP < 2 s", base_dados=self.d, agora=T, **kw)

    def regs(self, id, seq):
        return [M.registrar(id, v, "x", self.d)[0] for v in seq]

    def test_criar_grava_e_abre(self):
        m = self.nova(site="fsite05", medir=PS, alvo=2000, unidade="ms")
        self.assertEqual((m["issue"], m["url_issue"], m["estado"]), ("KRN-800", "https://l/x", "ativa"))
        self.assertEqual(M.ler(m["id"], self.d)["issue"], "KRN-800")
        self.assertEqual(m["id"], "m-20261015-fsite05-home-da-fsite05")
        self.assertTrue(self.nova(site="fsite05")["id"].endswith("-2"))

    def test_linear_falha_deixa_issue_nula(self):
        def cai(m):
            raise SystemExit("sem projeto")
        m = self.nova(abrir=cai)
        self.assertIsNone(M.ler(m["id"], self.d)["issue"])
        self.assertEqual(m["estado"], "definir")

    def test_validacao(self):
        for kw in ({"objetivo": " a "}, {"objetivo": "x" * 301}, {"site": "nao"}, {"esforco": "zzz"},
                   {"direcao": "x"}, {"alvo": "nan"}, {"alvo": float("inf")}, {"alvo": 0}, {"alvo": "abc"}):
            kw.setdefault("objetivo", "objetivo valido")
            with self.assertRaises(ValueError, msg=kw):
                M.criar(base_dados=self.d, abrir=lambda m: "B-1 u", **kw)

    def test_menor(self):
        m = self.nova(medir=PS, alvo=2000)
        self.assertEqual(self.regs(m["id"], [2900, 2800, 2700, 2650, 1990]),
                         ["base", "reverta", "conta", "reverta", "bateu"])
        self.assertEqual(M.ler(m["id"], self.d)["estado"], "batida")

    def test_maior(self):
        m = self.nova(medir=PS, alvo=90, direcao="maior")
        self.assertEqual(self.regs(m["id"], [70, 74, 76, 90]), ["base", "conta", "reverta", "bateu"])

    def test_teto(self):
        m = self.nova(medir=PS, alvo=1)
        v = self.regs(m["id"], [1000] * 51)
        self.assertEqual(v[-1], "travou")
        self.assertEqual(M.ler(m["id"], self.d)["estado"], "travada")

    def test_faltam_melhor(self):
        m = self.nova(medir=PS, alvo=2000)
        self.regs(m["id"], [2900, 2700])
        m = M.ler(m["id"], self.d)
        self.assertEqual((M.melhor(m), M.faltam(m)), (2700, 700))

    def test_json_quebrado(self):
        m = self.nova()
        (M.pasta(self.d) / "m-quebrada.json").write_text("{meio")
        self.assertEqual([x["id"] for x in M.listar(self.d)], [m["id"]])

    def test_descricao_definir_tem_passo_0(self):
        self.assertIn("0. Esta meta veio só com o texto", M.descricao(self.nova()))
        self.assertNotIn("0. Esta meta", M.descricao(self.nova(medir=PS, alvo=2000)))

    def test_medir(self):
        m = self.nova(medir=PS, alvo=2000)
        self.assertEqual(M.medir(m["id"], self.d, medidor=lambda u: {"lcp_ms": 2500, "nota": 80}), 2500)
        man = self.nova(medir={"tipo": "manual", "como": "npm run build"}, alvo=5)
        with self.assertRaises(ValueError):
            M.medir(man["id"], self.d)

    def test_abrir_issue_labels(self):
        with mock.patch("hub.metas.linear") as L:
            L.projeto_do_repo.return_value = "fsite05"
            M.abrir_issue({**self.nova(site="fsite05", esforco="alto")})
            self.assertEqual(L.criar_issue.call_args.args[4], ["meta", "opus"])
            L.projeto_do_repo.assert_called_with("example-org/cliente-a-v2")
            M.abrir_issue({**self.nova(site="sem")})
            L.projeto_do_repo.assert_called_with("example-org/repo-privado")

    def test_fechada_nao_registra(self):
        m = self.nova(medir=PS, alvo=2000)
        self.regs(m["id"], [2900, 1000])
        with self.assertRaises(ValueError):
            M.registrar(m["id"], 900, "x", self.d)

    def test_medir_sem_medida(self):
        with self.assertRaises(ValueError):
            M.medir(self.nova()["id"], self.d)

    def test_cli_criar_stdin(self):
        import io
        with mock.patch("sys.stdin", io.StringIO("objetivo via stdin")), \
             mock.patch("hub.metas.abrir_issue", return_value="KRN-1 https://l/1"), \
             mock.patch("hub.metas.data", return_value=self.d), mock.patch("sys.stdout", io.StringIO()) as o:
            M.main(["criar", "--objetivo", "-"])
        self.assertEqual(o.getvalue().strip(), "KRN-1 https://l/1")

    def acordar(self, *, ocup=(), loop=None, abrir=None, agora=T + dt.timedelta(hours=3)):
        enf, reab = [], []
        t = mock.Mock()
        t.nome = "linear-krn-800"
        r = M.acordar(set(ocup), self.d, agora, lambda i, f: (reab.append((i, f)), t)[1],
                      lambda t, m: enf.append((t.nome, m)) or True, lambda *a: loop, abrir)
        return r, enf, reab

    def test_acordar_reabre_e_enfileira(self):
        m = self.nova(medir=PS, alvo=2000)
        r, enf, reab = self.acordar()
        self.assertEqual((r, enf), (["linear-krn-800"], [("linear-krn-800", f"meta {m['id']}")]))
        self.assertIn("rodada 0/50", reab[0][1])
        self.assertIsNotNone(M.ler(m["id"], self.d)["acordada_em"])
        self.assertEqual(self.acordar(agora=T + dt.timedelta(hours=4))[0], [])  # acabou de acordar

    def test_acordar_nao_acorda(self):
        m = self.nova(medir=PS, alvo=2000)
        self.assertEqual(self.acordar(ocup=["linear-krn-800"])[0], [])
        self.assertEqual(self.acordar(agora=T + dt.timedelta(hours=1))[0], [])
        self.assertEqual(self.acordar(loop="rodou 9×")[0], [])
        self.regs(m["id"], [3000, 1000])  # bateu
        self.assertEqual(self.acordar()[0], [])

    def test_acordar_abre_issue_que_faltou(self):
        self.nova(abrir=lambda m: 1 / 0)
        abertas = []
        self.acordar(abrir=lambda m: abertas.append(m) or "KRN-9 https://l/9")
        self.assertEqual(len(abertas), 1)
        self.assertEqual(M.listar(self.d)[0]["issue"], "KRN-9")

    def test_coletor_bi_melhor_do_dia(self):
        m = self.nova(site="fsite05", medir=PS, alvo=2000)
        with mock.patch("hub.metas.conta.agora", return_value=T):
            self.regs(m["id"], [2800, 2700, 2600])  # 2700 não melhora 5 %: revertida, fica fora
        self.assertEqual(M.coletor_bi(T.date(), self.d), [{"dia": "2026-10-15", "negocio": "fsite05", "fonte": "meta",
                                                           "metrica": m["id"], "dim": "", "valor": 2600.0}])

    def test_coletor_dia_em_brt(self):
        m = self.nova(medir=PS, alvo=2000)
        with mock.patch("hub.metas.conta.agora", return_value=dt.datetime(2026, 10, 16, 2, tzinfo=dt.timezone.utc)):
            M.registrar(m["id"], 2800, "x", self.d)
        self.assertEqual(M.coletor_bi(None, self.d)[0]["dia"], "2026-10-15")

    def test_acordar_falha_de_uma_nao_para_outra(self):
        a, b = self.nova(medir=PS, alvo=2000), self.nova(medir=PS, alvo=2000)
        enf = []

        def reab(i, f):
            if f.startswith(f"Meta {a['id']}:"):
                raise RuntimeError("linear fora")
            return mock.Mock(nome="linear-krn-800")
        with mock.patch("sys.stderr"):
            r = M.acordar(set(), self.d, T + dt.timedelta(hours=3), reab, lambda t, m: enf.append(m) or False, lambda *x: None)
        self.assertEqual((r, enf), ([], [f"meta {b['id']}"]))  # enfileirar False: não conta
        self.assertIsNone(M.ler(a["id"], self.d)["acordada_em"])
        self.assertIsNotNone(M.ler(b["id"], self.d)["acordada_em"])
        self.assertEqual(M.ler(b["id"], self.d).get("acordadas_sem_rodada", 0), 0)  # fila recusou: sessão não conta

    def test_acordar_systemexit_nao_derruba(self):
        a = self.nova(abrir=lambda m: 1 / 0)
        b = self.nova(medir=PS, alvo=2000)

        def abrir(m):
            raise SystemExit("sem projeto")
        with mock.patch("sys.stderr"):
            r, _, _ = self.acordar(abrir=abrir)
        self.assertEqual(r, ["linear-krn-800"])
        self.assertIsNotNone(M.ler(b["id"], self.d)["acordada_em"])
        self.assertIsNone(M.ler(a["id"], self.d)["issue"])

    def test_registrar_alvo_conta_mesmo_sem_5_por_cento(self):
        m = self.nova(medir=PS, alvo=2000)
        v = self.regs(m["id"], [2050, 1995])
        self.assertEqual(v, ["base", "bateu"])
        self.assertEqual(M.ler(m["id"], self.d)["estado"], "batida")

    def test_acordar_trava_apos_6_sem_rodada_e_registrar_zera(self):
        m = self.nova(medir=PS, alvo=2000)
        for i in range(6):
            self.assertEqual(len(self.acordar(agora=T + dt.timedelta(hours=3 + 3 * i))[0]), 1)
        self.assertEqual(self.acordar(agora=T + dt.timedelta(hours=30))[0], [])
        x = M.ler(m["id"], self.d)
        self.assertEqual((x["estado"], x["motivo_travada"]), ("travada", "6 sessões sem registrar rodada"))
        n = self.nova(medir=PS, alvo=2000)
        n["acordadas_sem_rodada"] = 5
        M.gravar(n, self.d)
        M.registrar(n["id"], 3000, "x", self.d)
        self.assertEqual(M.ler(n["id"], self.d)["acordadas_sem_rodada"], 0)

    def test_registrar_concorrente_nao_perde_rodada(self):
        import threading
        m = self.nova(medir=PS, alvo=1)
        ts = [threading.Thread(target=M.registrar, args=(m["id"], 3000 - i, "x", self.d)) for i in range(8)]
        [t.start() for t in ts], [t.join() for t in ts]
        self.assertEqual(len(M.ler(m["id"], self.d)["rodadas"]), 8)

    def test_acordar_gravar_falha_apos_abrir_avisa(self):
        self.nova(abrir=lambda m: 1 / 0)
        with mock.patch("hub.metas.gravar", side_effect=OSError("disco")), mock.patch("sys.stderr") as e:
            self.acordar(abrir=lambda m: "KRN-9 https://l/9")
        self.assertIn("KRN-9", "".join(c.args[0] for c in e.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
