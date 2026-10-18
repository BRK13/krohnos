"""item 5 (princípios 7 e 14): main vermelho é incidente. Caso real de 14/10: o #695 quebrou o teste do cartão
do Conector e todos os PRs seguintes falharam nele sem tê-lo tocado; a fila seguiu reenfileirando."""
import datetime as dt, tempfile, unittest
from hub import esperas, incidentes, main_vermelho as mv

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
TESTE = "tests/conector-cartao.test.ts"
DESC = f"falhou em npm test: {TESTE} (7 min); log em clientec:/home/runner/.cache/x.log"


class GH:
    def __init__(self, main_estado=None, prs=None):
        self.main_estado, self.main_em = main_estado, "2026-10-14T17:50:00Z"
        self.prs = prs or {}  # numero: (sha, estado, descricao, updated_at, arquivos)

    def __call__(self, c):
        if c.startswith("repos/example-org/clienteh/commits/main") and c.endswith("/status"):
            return {"sha": "m" * 40, "statuses": [{"context": "local-ci", "state": self.main_estado, "description": DESC,
                                                   "updated_at": self.main_em}] if self.main_estado else []}
        if c.startswith("repos/example-org/clienteh/commits/main"):
            return {"sha": "m" * 40}
        if c.startswith("repos/example-org/clienteh/pulls?"):
            return [{"number": n, "head": {"sha": v[0]}} for n, v in self.prs.items()]
        for n, v in self.prs.items():
            if c == f"repos/example-org/clienteh/commits/{v[0]}/status":
                return {"statuses": [{"context": "local-ci", "state": v[1], "description": v[2], "updated_at": v[3]}] if v[1] else []}
            if c.startswith(f"repos/example-org/clienteh/pulls/{n}/files"):
                return [{"filename": f} for f in v[4]]
        if c.startswith("repos/example-org/clienteh/commits?"):
            return [{"sha": "c695" + "0" * 36, "commit": {"message": "Conector: cartão novo (#695)"}}]
        raise AssertionError(c)


class MainVermelho(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.criadas = []

    def tearDown(self):
        self.tmp.cleanup()

    def criar(self, titulo, descricao, prioridade):
        self.criadas.append((titulo, descricao, prioridade))
        return "KRN-740 https://linear.app/x/KRN-740"

    def test_testes_da_descricao(self):
        self.assertEqual([TESTE], mv.testes(DESC))
        self.assertEqual([], mv.testes("falhou em npm run build (9 min); log em x"))

    def test_695_dois_prs_seguidos_no_mesmo_teste_que_nao_tocaram(self):
        gh = GH(prs={701: ("a" * 40, "failure", DESC, "2026-10-14T17:00:00Z", ["src/app/x.tsx"]),
                     703: ("b" * 40, "failure", DESC, "2026-10-14T17:30:00Z", ["src/lib/y.ts"]),
                     690: ("c" * 40, "success", "ok", "2026-10-14T15:00:00Z", ["a"])})
        inc = mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        self.assertEqual("KRN-740", inc["issue"])
        titulo, descricao, prioridade = self.criadas[0]
        self.assertIn(TESTE, titulo)
        self.assertIn("#695", descricao)
        self.assertEqual(1, prioridade)
        self.assertEqual(["main_vermelho"], [i["classe"] for i in incidentes.listar(base_dados=self.dados)])
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(minutes=10))
        self.assertEqual(1, len(self.criadas))  # Review Focus 5: uma issue por incidente

    def test_pr_que_mexeu_no_teste_nao_conta(self):
        gh = GH(prs={701: ("a" * 40, "failure", DESC, "2026-10-14T17:00:00Z", [TESTE]),
                     703: ("b" * 40, "failure", DESC, "2026-10-14T17:30:00Z", ["src/lib/y.ts"])})
        self.assertIsNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0))

    def test_main_falhou_no_local_ci(self):
        inc = mv.checar(gh=GH(main_estado="failure"), criar=self.criar, base_dados=self.dados, agora=T0)
        self.assertEqual(TESTE, inc["teste"])

    def test_esperas_seguram_o_repo_e_soltam_quando_volta_a_passar(self):
        gh = GH(prs={701: ("a" * 40, "failure", DESC, "2026-10-14T17:00:00Z", ["x"]),
                     703: ("b" * 40, "failure", DESC, "2026-10-14T17:30:00Z", ["y"])})
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        esperas.gravar("KRN-700", "https://github.com/example-org/clienteh/pull/703", chamar=lambda c: {"head": {"sha": "b" * 40}} if "pulls/703" in c else [],
                       base_dados=self.dados, agora=T0)
        fala_pronta = lambda e, chamar=None, agora=None, **kw: "local-ci vermelho"
        from unittest import mock
        with mock.patch.object(esperas, "situacao", fala_pronta):
            self.assertEqual([], esperas.prontas(base_dados=self.dados, agora=T0 + dt.timedelta(hours=1)))
            gh.main_estado = "success"
            mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=2))
            self.assertIsNone(mv.aberto(mv.REPO, self.dados))
            self.assertEqual(1, len(esperas.prontas(base_dados=self.dados, agora=T0 + dt.timedelta(hours=3))))

    def test_formato_antigo_sem_nomes_nao_abre_incidente_de_2_prs(self):
        d = "falhou em npm test (7 min); log em x"
        self.assertEqual([], mv.testes(d))
        gh = GH(prs={701: ("a" * 40, "failure", d, "2026-10-14T17:00:00Z", ["x"]),
                     703: ("b" * 40, "failure", d, "2026-10-14T17:30:00Z", ["y"])})
        self.assertIsNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0))
        self.assertEqual([], self.criadas)

    def test_main_sem_status_e_nada(self):
        self.assertIsNone(mv.checar(gh=GH(), criar=self.criar, base_dados=self.dados, agora=T0))

    def test_issue_falhou_nao_grava_estado_e_tenta_de_novo(self):
        logs = []
        def quebra(*a):
            raise SystemExit("linear fora")
        self.assertIsNone(mv.checar(gh=GH(main_estado="failure"), criar=quebra, base_dados=self.dados, agora=T0, log=logs.append))
        self.assertIsNone(mv.aberto(mv.REPO, self.dados))
        self.assertTrue(logs)
        self.assertIsNotNone(mv.checar(gh=GH(main_estado="failure"), criar=self.criar, base_dados=self.dados, agora=T0))

    def test_gh_falhou_volta_none(self):
        def cai(c):
            raise OSError("rede")
        logs = []
        self.assertIsNone(mv.checar(gh=cai, criar=self.criar, base_dados=self.dados, agora=T0, log=logs.append))
        self.assertTrue(logs)

    def _dois_vermelhos(self):
        return GH(prs={701: ("a" * 40, "failure", DESC, "2026-10-14T17:00:00Z", ["x"]),
                       703: ("b" * 40, "failure", DESC, "2026-10-14T17:30:00Z", ["y"])})

    def test_backlog_25_prs_so_os_2_mais_antigos_com_status(self):
        prs = {n: (f"{n:040d}", None, "", "", ["z"]) for n in range(100, 123)}
        prs[701] = ("a" * 40, "failure", DESC, "2026-10-14T17:00:00Z", ["x"])
        prs[703] = ("b" * 40, "failure", DESC, "2026-10-14T17:30:00Z", ["y"])
        inc = mv.checar(gh=GH(prs=prs), criar=self.criar, base_dados=self.dados, agora=T0)
        self.assertEqual("KRN-740", inc["issue"])

    def test_nao_reabre_com_as_rodadas_velhas_so_com_rodada_nova(self):
        gh = self._dois_vermelhos()
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        gh.main_estado = "success"
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=1))
        self.assertIsNone(mv.aberto(mv.REPO, self.dados))
        gh.main_estado = None  # as 2 últimas rodadas concluídas seguem sendo as velhas vermelhas
        self.assertIsNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=2)))
        self.assertEqual(1, len(self.criadas))
        gh.prs[720] = ("d" * 40, "failure", DESC, "2026-10-14T21:00:00Z", ["w"])  # rodada nova, ainda vermelha
        self.assertIsNotNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=4)))
        self.assertEqual(2, len(self.criadas))

    def test_pr_verde_fecha_com_main_ainda_vermelho_e_so_reabre_com_main_novo(self):
        gh = GH(main_estado="failure")
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        gh.prs[800] = ("e" * 40, "success", "ok", "2026-10-14T19:00:00Z", ["a"])  # passou depois do incidente
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=2))
        self.assertIsNone(mv.aberto(mv.REPO, self.dados))
        self.assertIsNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=3)))
        self.assertEqual(1, len(self.criadas))
        gh.main_em = "2026-10-14T21:30:00Z"  # status do main mais novo que o fechamento
        self.assertIsNotNone(mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=4)))

    def test_issue_encerrada_solta(self):
        gh = self._dois_vermelhos()
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=1), encerrada=lambda i: False)
        self.assertIsNotNone(mv.aberto(mv.REPO, self.dados))
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=2), encerrada=lambda i: i == "KRN-740")
        self.assertIsNone(mv.aberto(mv.REPO, self.dados))

    def test_24h_aberto_solta(self):
        gh, logs = self._dois_vermelhos(), []
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=25), encerrada=lambda i: False, log=logs.append)
        self.assertIsNone(mv.aberto(mv.REPO, self.dados))
        self.assertTrue(any("main vermelho há 24 h: soltei as esperas" in l for l in logs))

    def test_24h_fechado_e_nova_rodada_readota_a_issue_ainda_aberta(self):  # I6: não abre 2ª P1
        gh = self._dois_vermelhos()
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0)
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=25), encerrada=lambda i: False)
        gh.prs[720] = ("d" * 40, "failure", DESC, "2026-10-15T20:00:00Z", ["w"])  # rodada nova, mesmo teste
        inc = mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=27), encerrada=lambda i: False)
        self.assertEqual(("KRN-740", 1), (inc["issue"], len(self.criadas)))
        self.assertEqual("KRN-740", mv.aberto(mv.REPO, self.dados)["issue"])
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=52), encerrada=lambda i: False)
        gh.prs[730] = ("f" * 40, "failure", DESC, "2026-10-16T23:00:00Z", ["v"])
        mv.checar(gh=gh, criar=self.criar, base_dados=self.dados, agora=T0 + dt.timedelta(hours=54), encerrada=lambda i: True)
        self.assertEqual(2, len(self.criadas))  # a anterior encerrada: abre outra

    def test_aviso_no_prompt_da_tarefa(self):
        from unittest import mock
        from hub import linear
        item = lambda ident: {"tipo": "issue", "id": "x", "identifier": ident, "titulo": "t", "descricao": "", "url": "u",  # noqa: E731
                              "modelo": "haiku", "repos": [mv.REPO], "comentarios": [], "estados": {}}
        with mock.patch.object(mv, "aberto", return_value={"issue": "KRN-740", "teste": TESTE}):
            self.assertIn(f"**Main vermelho em {mv.REPO} (KRN-740, teste `{TESTE}`):**", linear.tarefa(item("KRN-1")).prompt)
            self.assertNotIn("Main vermelho em", linear.tarefa(item("KRN-740")).prompt)
