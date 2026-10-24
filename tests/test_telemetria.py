"""F2: telemetria das execuções (uso.jsonl -> telemetria.db), PRs e merge. Sem rede."""
import datetime as dt, json, tempfile, unittest
from unittest import mock

from hub import conta, github_api, telemetria

UTC = dt.timezone.utc
PR = "https://github.com/acme/repo-privado/pull/20"
REG = {"inicio": "2026-10-17T16:42:15+00:00", "fim": "2026-10-17T16:50:15+00:00", "origem": "tarefa", "conta": "4",
       "nome": "linear-krn-1083", "modelo": "haiku", "ok": True, "limite": False, "interrompida": False,
       "custo_usd": 0.0187, "tokens": {"entrada": 24, "saida": 7429, "cache_leitura": 591797, "cache_escrita": 45586},
       "turnos": 12, "duracao_s": 480.5, "erro": None, "tipo": "espera_deploy",
       "motivo_volta": f"voltou da espera do CI ({PR})"}


class Telemetria(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _uso(self, *regs, extra=""):
        p = conta.pasta(self.d)
        p.mkdir(parents=True, exist_ok=True)
        (p / "uso.jsonl").write_text("".join(json.dumps(r) + "\n" for r in regs) + extra, encoding="utf-8")

    def _linhas(self, sql="select * from execucao order by inicio"):
        con = telemetria.conectar(self.d)
        con.row_factory = __import__("sqlite3").Row
        try:
            return [dict(r) for r in con.execute(sql)]
        finally:
            con.close()

    def test_caminho_e_colunas(self):
        self.assertEqual(telemetria.caminho(self.d), conta.pasta(self.d) / "telemetria.db")
        con = telemetria.conectar(self.d)
        cols = [r[1] for r in con.execute("pragma table_info(execucao)")]
        self.assertEqual(cols, ["id", "issue", "conta", "executor", "modelo", "inicio", "fim", "segundos", "custo_usd",
                                "tokens_in", "tokens_out", "resultado", "motivo", "pr", "merge_em", "no_ar_em",
                                "retrabalho"])
        self.assertEqual([r[1] for r in con.execute("pragma table_info(pr)")],
                         ["url", "merge_em", "estado", "conferido_em", "no_ar_em"])
        self.assertEqual(con.execute("pragma journal_mode").fetchone()[0], "wal")
        con.close()

    def test_de_uso_campos(self):
        e = telemetria.de_uso(REG)
        self.assertEqual(len(e["id"]), 16)
        self.assertEqual(e["id"], telemetria.de_uso(dict(REG))["id"])
        self.assertEqual((e["issue"], e["executor"], e["segundos"], e["custo_usd"]), ("KRN-1083", "claude", 480.5, 0.0187))
        self.assertEqual((e["tokens_in"], e["tokens_out"]), (24 + 45586 + 591797, 7429))
        self.assertEqual((e["resultado"], e["motivo"], e["pr"], e["retrabalho"]), ("ok", "espera_deploy", PR, 0))
        self.assertIsNone(e["merge_em"])
        self.assertIsNone(e["no_ar_em"])

    def test_mesmo_inicio_com_fim_diferente_sao_duas_linhas(self):
        outra = {**REG, "fim": "2026-10-17T16:51:00+00:00", "custo_usd": 0.5}
        self._uso(REG, REG, outra)
        self.assertEqual(telemetria.importar(base_dados=self.d), 2)
        self.assertNotEqual(telemetria.de_uso(REG)["id"], telemetria.de_uso(outra)["id"])

    def test_pr_da_saida_pega_a_ultima_url(self):
        arq = conta.pasta(self.d) / "saida.md"
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text("antes https://github.com/acme/a/pull/1 e no fim https://github.com/acme/b/pull/2/files\n")
        r = {**REG, "motivo_volta": "sem link", "saida": str(arq)}
        self.assertEqual(telemetria.de_uso(r)["pr"], "https://github.com/acme/b/pull/2")
        self.assertIsNone(telemetria.de_uso({**r, "saida": str(arq) + ".nao"})["pr"])
        self.assertIsNone(telemetria.de_uso({**r, "nome": "retro-diaria"})["pr"])  # só issue KRN-N

    def test_issue_de_rotina_fica_com_o_nome(self):
        self.assertEqual(telemetria.de_uso({**REG, "nome": "retro-diaria"})["issue"], "retro-diaria")

    def test_codex_custo_null_e_executor(self):
        e = telemetria.de_uso({**REG, "conta": "codex", "custo_usd": None})
        self.assertEqual(e["executor"], "codex")
        self.assertIsNone(e["custo_usd"])
        self.assertEqual(telemetria.de_uso({**REG, "conta": "api"})["executor"], "api")
        self._uso({**REG, "conta": "codex", "custo_usd": None})
        telemetria.importar(base_dados=self.d)
        self.assertIsNone(self._linhas()[0]["custo_usd"])
        self.assertEqual(self._linhas("select count(*) c from execucao where custo_usd is null")[0]["c"], 1)

    def test_linha_antiga_sem_campos(self):
        e = telemetria.de_uso({"inicio": "2026-10-08T10:00:00+00:00", "nome": "x", "ok": False})
        self.assertEqual((e["resultado"], e["motivo"], e["tokens_in"], e["tokens_out"], e["executor"], e["retrabalho"]),
                         ("falha", "outro", 0, 0, "claude", 0))

    def test_pr_extraido_do_motivo_ou_campo_pr(self):
        r = {**REG, "motivo_volta": f"ver {PR}/ e https://github.com/acme/x/pull/9#issuecomment-1"}
        self.assertEqual(telemetria.de_uso(r)["pr"], PR)
        r = {**REG, "motivo_volta": "sem link", "pr": "https://github.com/acme/x/pull/9#diff"}
        self.assertEqual(telemetria.de_uso(r)["pr"], "https://github.com/acme/x/pull/9")
        self.assertIsNone(telemetria.de_uso({**REG, "motivo_volta": "nada"})["pr"])

    def test_retrabalho_por_tipo(self):
        for tipo, esperado in (("conferencia", 1), ("recibo", 1), ("promessa", 1), ("espera_ci", 0), ("limite", 0),
                               ("primeira", 0)):
            self.assertEqual(telemetria.de_uso({**REG, "tipo": tipo})["retrabalho"], esperado, tipo)

    def test_resultado(self):
        base = {**REG, "ok": False, "limite": False, "interrompida": False}
        self.assertEqual(telemetria.de_uso({**base, "ok": True})["resultado"], "ok")
        self.assertEqual(telemetria.de_uso({**base, "limite": True})["resultado"], "limite")
        self.assertEqual(telemetria.de_uso({**base, "interrompida": True})["resultado"], "parada")
        self.assertEqual(telemetria.de_uso(base)["resultado"], "falha")

    def test_importar_idempotente_e_linha_cortada(self):
        outra = {**REG, "inicio": "2026-10-17T17:00:00+00:00"}
        self._uso(REG, outra, extra='{"inicio": "2026-10-17T18:00:00+00:00", "nom')
        self.assertEqual(telemetria.importar(base_dados=self.d), 2)
        self.assertEqual(telemetria.importar(base_dados=self.d), 0)
        self.assertEqual(len(self._linhas()), 2)

    def test_registrar_e_importar_nao_duplicam(self):
        telemetria.registrar(telemetria.de_uso(REG), base_dados=self.d)
        telemetria.registrar(telemetria.de_uso(REG), base_dados=self.d)
        self._uso(REG)
        self.assertEqual(telemetria.importar(base_dados=self.d), 0)
        self.assertEqual(len(self._linhas()), 1)

    def test_importar_desde(self):
        self._uso(REG, {**REG, "inicio": "2026-10-10T10:00:00+00:00"})
        n = telemetria.importar(desde=dt.datetime(2026, 10, 15, tzinfo=UTC), base_dados=self.d)
        self.assertEqual(n, 1)

    def test_segredo_mascarado(self):
        with mock.patch.object(conta, "chaves_api", return_value=["chave-falsa-123456"]):
            e = telemetria.de_uso({**REG, "motivo_volta": "x", "tipo": "chave-falsa-123456 ghp_abc123DEF Bearer xyz.987 "
                                   "sk-ant-api03-ABCDEFGHIJ github_pat_11AAA lin_api_zzz9"})
            e["modelo"] = "haiku sk-ant-api03-ABCDEFGHIJ"
            telemetria.registrar(e, base_dados=self.d)
        l = self._linhas()[0]
        for texto in (l["motivo"], l["modelo"]):
            for vazado in ("chave-falsa", "ghp_abc", "xyz.987", "sk-ant", "github_pat_", "lin_api_"):
                self.assertNotIn(vazado, texto)
        self.assertIn("•••", l["motivo"])

    def test_registrar_copia_merge_em_do_pr_ja_conhecido(self):
        con = telemetria.conectar(self.d)
        con.execute("insert into pr values (?, ?, 'merged', ?, NULL)", (PR, "2026-10-17T20:00:00+00:00", "2026-10-17T20:05:00+00:00"))
        con.commit()
        con.close()
        telemetria.registrar(telemetria.de_uso(REG), base_dados=self.d)
        self.assertEqual(self._linhas()[0]["merge_em"], "2026-10-17T20:00:00+00:00")

    def test_marcar_no_ar(self):
        telemetria.registrar(telemetria.de_uso(REG), base_dados=self.d)
        telemetria.registrar(telemetria.de_uso({**REG, "inicio": "2026-10-17T17:00:00+00:00"}), base_dados=self.d)
        q = dt.datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
        self.assertEqual(telemetria.marcar_no_ar(PR, q, base_dados=self.d), 2)
        self.assertEqual(telemetria.marcar_no_ar(PR, q + dt.timedelta(hours=1), base_dados=self.d), 0)
        self.assertEqual({l["no_ar_em"] for l in self._linhas()}, {"2026-10-17T21:00:00+00:00"})

    def test_no_ar_antes_da_execucao_entrar_no_banco_nao_se_perde(self):  # revisão do Codex: gancho falhou, cron importa
        q = dt.datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
        self.assertEqual(telemetria.marcar_no_ar(PR, q, base_dados=self.d), 0)
        telemetria.registrar(telemetria.de_uso(REG), base_dados=self.d)
        self.assertEqual(self._linhas()[0]["no_ar_em"], "2026-10-17T21:00:00+00:00")
        telemetria.atualizar_prs(get=lambda c: {"merged_at": "2026-10-17T20:00:00Z"}, base_dados=self.d,
                                 agora=q + dt.timedelta(hours=1))  # a conferência do merge não apaga o no ar do PR
        self.assertEqual(self._linhas("select no_ar_em from pr")[0]["no_ar_em"], "2026-10-17T21:00:00+00:00")

    def test_conectar_migra_tabela_pr_sem_no_ar(self):
        import sqlite3
        arq = telemetria.caminho(self.d)
        arq.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(arq)
        con.execute("CREATE TABLE pr (url TEXT PRIMARY KEY, merge_em, estado, conferido_em)")
        con.commit()
        con.close()
        telemetria.marcar_no_ar(PR, dt.datetime(2026, 10, 17, 21, 0, tzinfo=UTC), base_dados=self.d)
        self.assertEqual(len(self._linhas("select no_ar_em from pr")), 1)

    # ---- atualizar_prs ----

    def _prs(self, n):
        for i in range(n):
            telemetria.registrar(telemetria.de_uso({**REG, "inicio": f"2026-10-17T1{i}:00:00+00:00",
                                                    "motivo_volta": f"https://github.com/acme/r/pull/{i + 1}"}),
                                 base_dados=self.d)

    def test_atualizar_prs_grava_e_propaga_merge(self):
        self._prs(2)
        pedidos = []

        def get(c):
            pedidos.append(c)
            return {"merged_at": "2026-10-17T20:00:00Z"} if c.endswith("/1") else {"state": "open", "merged_at": None}

        agora = dt.datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
        self.assertEqual(telemetria.atualizar_prs(get=get, base_dados=self.d, agora=agora), 2)
        self.assertCountEqual(pedidos, ["repos/acme/r/pulls/1", "repos/acme/r/pulls/2"])
        por_pr = {l["pr"][-1]: l["merge_em"] for l in self._linhas()}
        self.assertEqual(por_pr["1"], "2026-10-17T20:00:00+00:00")
        self.assertIsNone(por_pr["2"])
        estados = {r["u"]: r["e"] for r in self._linhas("select substr(url,-1) u, estado e from pr")}
        self.assertEqual(estados, {"1": "merged", "2": "open"})

    def test_atualizar_prs_nao_pergunta_mergeado_nem_fechado_e_espera_30min(self):
        self._prs(3)
        agora = dt.datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
        con = telemetria.conectar(self.d)
        con.execute("insert into pr values ('https://github.com/acme/r/pull/1', 'x', 'merged', ?, NULL)", (conta.iso(agora),))
        con.execute("insert into pr values ('https://github.com/acme/r/pull/2', NULL, 'closed', ?, NULL)",
                    (conta.iso(agora - dt.timedelta(hours=2)),))
        con.execute("insert into pr values ('https://github.com/acme/r/pull/3', NULL, 'open', ?, NULL)",
                    (conta.iso(agora - dt.timedelta(minutes=10)),))
        con.commit()
        con.close()
        pedidos = []
        n = telemetria.atualizar_prs(get=lambda c: pedidos.append(c) or {}, base_dados=self.d, agora=agora)
        self.assertEqual((n, pedidos), (0, []))
        n = telemetria.atualizar_prs(get=lambda c: pedidos.append(c) or {"state": "open"}, base_dados=self.d,
                                     agora=agora + dt.timedelta(minutes=25))
        self.assertEqual((n, len(pedidos)), (1, 1))

    def test_atualizar_prs_fechado_sem_merge(self):
        self._prs(1)
        telemetria.atualizar_prs(get=lambda c: {"state": "closed", "merged_at": None}, base_dados=self.d)
        self.assertEqual(self._linhas("select estado from pr")[0]["estado"], "closed")

    def test_atualizar_prs_fechado_volta_a_ser_conferido_no_dia_seguinte(self):  # revisão do Codex: reaberto e mergeado
        self._prs(1)
        agora = dt.datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
        telemetria.atualizar_prs(get=lambda c: {"state": "closed", "merged_at": None}, base_dados=self.d, agora=agora)
        merge = lambda c: {"state": "closed", "merged_at": "2026-10-18T10:00:00Z"}
        self.assertEqual(telemetria.atualizar_prs(get=merge, base_dados=self.d, agora=agora + dt.timedelta(hours=23)), 0)
        self.assertEqual(telemetria.atualizar_prs(get=merge, base_dados=self.d, agora=agora + dt.timedelta(hours=25)), 1)
        self.assertEqual(self._linhas()[0]["merge_em"], "2026-10-18T10:00:00+00:00")

    def test_atualizar_prs_respeita_limite_e_mais_antigo_primeiro(self):
        self._prs(5)
        pedidos = []
        agora = dt.datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
        self.assertEqual(telemetria.atualizar_prs(get=lambda c: pedidos.append(c) or {"state": "open"}, limite=2,
                                                  base_dados=self.d, agora=agora), 2)
        self.assertEqual(len(pedidos), 2)
        # 2ª rodada 1 h depois: os 3 nunca conferidos vêm antes dos 2 já conferidos
        primeiros = set(pedidos)
        pedidos.clear()
        telemetria.atualizar_prs(get=lambda c: pedidos.append(c) or {"state": "open"}, limite=3, base_dados=self.d,
                                 agora=agora + dt.timedelta(hours=1))
        self.assertEqual(len(set(pedidos)), 3)
        self.assertTrue(set(pedidos).isdisjoint(primeiros))
        con = telemetria.conectar(self.d)
        self.assertEqual(con.execute("select count(*) from pr where conferido_em is not null").fetchone()[0], 5)
        con.close()

    def test_atualizar_prs_para_na_excecao(self):
        self._prs(4)
        pedidos = []

        def get(c):
            pedidos.append(c)
            if len(pedidos) == 2:
                raise RuntimeError("rate limit")
            return {"state": "open"}

        n = telemetria.atualizar_prs(get=get, base_dados=self.d)
        self.assertEqual((n, len(pedidos)), (1, 2))

    def test_atualizar_prs_404_marca_sumido_e_segue(self):
        self._prs(3)
        pedidos = []

        def get(c):
            pedidos.append(c)
            if c.endswith("/1"):
                raise github_api.ErroHTTP(404)
            return {"state": "open"}

        agora = dt.datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
        self.assertEqual(telemetria.atualizar_prs(get=get, base_dados=self.d, agora=agora), 3)
        self.assertEqual(self._linhas("select estado from pr where url like '%/1'")[0]["estado"], "sumido")
        pedidos.clear()
        telemetria.atualizar_prs(get=get, base_dados=self.d, agora=agora + dt.timedelta(hours=1))
        self.assertFalse(any(c.endswith("/1") for c in pedidos))

    def test_atualizar_prs_500_e_cota_baixa_encerram(self):
        for exc in (github_api.ErroHTTP(500), github_api.CotaBaixa("x")):
            self._prs(3)
            pedidos = []

            def get(c, exc=exc):
                pedidos.append(c)
                raise exc

            self.assertEqual(telemetria.atualizar_prs(get=get, base_dados=self.d), 0)
            self.assertEqual(len(pedidos), 1)

    def test_get_padrao_usa_consumidor_telemetria_nao_urgente(self):
        self._prs(1)
        with mock.patch("hub.github_api.get", return_value={"state": "open"}) as g:
            telemetria.atualizar_prs(base_dados=self.d)
        g.assert_called_once()
        self.assertEqual(g.call_args.args[0], "repos/acme/r/pulls/1")
        self.assertEqual((g.call_args.kwargs["consumidor"], g.call_args.kwargs["urgente"]), ("telemetria", False))

    def test_main_importar(self):
        self._uso(REG)
        with mock.patch.object(conta, "data", return_value=self.d), mock.patch("builtins.print") as p:
            telemetria.main(["importar"])
        self.assertEqual(p.call_args.args[0], "1 novas, total 1")


if __name__ == "__main__":
    unittest.main()
