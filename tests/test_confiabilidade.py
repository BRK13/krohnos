"""F2 T3: resumo, avisos falsos (definição de caso), conciliação, alarme, linha de base e tela. Sem rede."""
import datetime as dt, json, tempfile, unittest

from hub import conta, incidentes, telemetria, confiabilidade as C
from hub.comum import porteiro

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


def t(h):  # h horas antes de AGORA, em iso
    return conta.iso(AGORA - dt.timedelta(hours=h))


def uso(h, nome="linear-krn-1", conta_="1", **kw):
    r = {"inicio": t(h), "fim": t(h - 0.1), "origem": "tarefa", "conta": conta_, "nome": nome, "modelo": "sonnet",
         "ok": True, "limite": False, "interrompida": False, "custo_usd": 1.0, "tokens": {"entrada": 1, "saida": 1},
         "turnos": 5, "duracao_s": 10, "erro": None, "tipo": "primeira"}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        conta.pasta(self.d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _uso(self, *regs):
        (conta.pasta(self.d) / "uso.jsonl").write_text("".join(json.dumps(r) + "\n" for r in regs), encoding="utf-8")

    def _jsonl(self, nome, *regs):
        (conta.pasta(self.d) / nome).write_text("".join(json.dumps(r) + "\n" for r in regs), encoding="utf-8")

    def _envios(self, *envios):
        (conta.pasta(self.d) / "zap-dono.json").write_text(json.dumps({"envios": list(envios)}), encoding="utf-8")

    def _banco(self, *regs):
        self._uso(*regs)
        telemetria.importar(base_dados=self.d)

    def _merge(self, issue, merge_h):
        con = telemetria.conectar(self.d)
        with con:
            con.execute("UPDATE execucao SET merge_em=? WHERE issue=?", (t(merge_h), issue))
        con.close()

    def _inc(self, classe, issue, h, estado="pendente", chave="", nota=""):
        inc = incidentes.registrar("falha de teste", classe, "peca x", issue=issue, chave=chave, base_dados=self.d,
                                   agora=AGORA - dt.timedelta(hours=h))
        if estado == "feito":
            incidentes.marcar(inc["id"], "feito", nota, base_dados=self.d, agora=AGORA, gh=lambda *a, **k: None)
        return inc

    def casos(self, dias=7):
        return [a["caso"] for a in C.avisos_falsos(dias, self.d, AGORA)]


class AvisosFalsos(Base):
    def test_a1_limite_sem_chamar(self):
        self._uso(uso(2, ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user ID 20546254"))
        itens = C.avisos_falsos(7, self.d, AGORA)
        self.assertEqual([i["caso"] for i in itens], ["limite_sem_chamar"])
        self.assertTrue(itens[0]["chave"].startswith("limite_sem_chamar:1:2026-10-17T16"))
        self.assertEqual(itens[0]["conta"], "1")

    def test_limite_do_claude_sem_turnos_nao_e_falso(self):
        # recusa de cota do Claude também vem sem turnos: só a cota do GitHub prova que o Claude não foi chamado
        self._uso(uso(2, ok=False, limite=True, turnos=None, erro="You've hit your session limit · resets 8am (UTC)"),
                  uso(3, ok=False, limite=True, turnos=None, erro='{"type":"rate_limit_error","message":"429"}'),
                  uso(4, ok=False, limite=True, turnos=None))
        self.assertEqual(C.avisos_falsos(7, self.d, AGORA), [])

    def test_limite_com_turnos_nao_e_falso(self):
        self._uso(uso(2, ok=False, limite=True, turnos=3))
        self.assertEqual(self.casos(), [])

    def test_a1_dedupe_por_conta_e_hora(self):
        base = dict(ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user ID 1")
        self._uso(uso(2.0, **base), uso(1.9, **base), uso(1.95, **base), uso(2.0, conta_="2", **base),
                  uso(5, **base))  # 3 na mesma hora da conta 1, 1 da conta 2, 1 em outra hora
        self.assertEqual(len(C.avisos_falsos(7, self.d, AGORA)), 3)

    def test_a2_pausa_desmentida(self):
        self._uso(uso(3, conta_="2"))  # ok com turnos 5 dentro da pausa
        self._jsonl("pausas.jsonl", {"conta": "2", "desde": t(4), "ate": t(1), "origem": "limite"})
        itens = C.avisos_falsos(7, self.d, AGORA)
        self.assertEqual([i["caso"] for i in itens], ["pausa_desmentida"])
        self.assertEqual(itens[0]["chave"], f"pausa:2:{t(4)}")

    def test_pausa_real_nao_e_falsa(self):
        # outra conta rodou; a própria só rodou antes e depois, e uma execução com falha dentro
        self._uso(uso(3, conta_="1"), uso(6, conta_="2"), uso(0.5, conta_="2"),
                  uso(3, conta_="2", ok=False, turnos=0))
        self._jsonl("pausas.jsonl", {"conta": "2", "desde": t(4), "ate": t(1), "origem": "limite"})
        with open(conta.pasta(self.d) / "pausas.jsonl", "a") as f:
            f.write('{"conta": "2", "desde"')  # linha cortada
        self.assertEqual(self.casos(), [])

    def test_sem_arquivo_de_pausas(self):
        self._uso(uso(3))
        self.assertEqual(self.casos(), [])

    def test_b_pronto_desmentido(self):
        self._envios({"em": t(30), "origem": "x", "issue": "KRN-9", "tipo": "enviado"})
        self._inc("pronto_sem_recibo", "KRN-9", 20)
        itens = C.avisos_falsos(7, self.d, AGORA)
        self.assertEqual([i["caso"] for i in itens], ["pronto_desmentido"])
        self.assertEqual(itens[0]["issue"], "KRN-9")
        self.assertEqual(itens[0]["chave"], f"pronto:KRN-9:{t(30)}")

    def test_envio_sem_incidente_nao_e_falso(self):
        self._envios({"em": t(30), "origem": "x", "issue": "KRN-9", "tipo": "resumo"},
                     {"em": t(30), "origem": "x", "issue": None, "tipo": "enviado"})
        self._inc("pronto_sem_recibo", "KRN-10", 20)  # outra issue
        self.assertEqual(self.casos(), [])

    def test_incidente_depois_de_48h_nao_e_falso(self):
        self._envios({"em": t(100), "origem": "x", "issue": "KRN-9", "tipo": "enviado"})
        self._inc("pronto_sem_recibo", "KRN-9", 40)  # 60 h depois
        self.assertEqual(self.casos(), [])

    def test_envio_que_nao_foi_ao_dono_nao_conta(self):
        self._envios({"em": t(30), "origem": "x", "issue": "KRN-9", "tipo": "fila"})
        self._inc("pronto_sem_recibo", "KRN-9", 20)
        self.assertEqual(self.casos(), [])

    def test_c_manual_e_ordem_e_janela(self):
        C.marcar_aviso_falso("avisou pronto sem estar", "KRN-5", self.d, AGORA - dt.timedelta(hours=3))
        C.marcar_aviso_falso("velho", "", self.d, AGORA - dt.timedelta(days=30))
        self._uso(uso(2, ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user ID 1"))
        itens = C.avisos_falsos(7, self.d, AGORA)
        self.assertEqual([i["caso"] for i in itens], ["manual", "limite_sem_chamar"])  # por em: 3 h antes, 2 h antes
        self.assertEqual(itens[0]["chave"], f"manual:{t(3)}")
        self.assertEqual(itens[0]["detalhe"], "avisou pronto sem estar")
        self.assertEqual(itens[0]["issue"], "KRN-5")


class Resumo(Base):
    def test_sucesso_por_tipo_executor_e_vazio(self):
        r = C.resumo(7, self.d, AGORA)
        self.assertEqual(r["janela"]["execucoes"], 0)
        self.assertIsNone(r["issue_merge_horas"]["mediana"])
        self._banco(uso(1, tipo="primeira"), uso(2, tipo="primeira", ok=False),
                    uso(3, tipo="conferencia"), uso(4, conta_="codex", custo_usd=None))
        r = C.resumo(7, self.d, AGORA)
        self.assertEqual(r["janela"]["execucoes"], 4)
        self.assertEqual(r["sucesso_por_tipo"]["primeira"], {"n": 3, "ok": 2, "taxa": 0.667})
        self.assertEqual(r["sucesso_por_tipo"]["_todas"]["n"], 4)
        self.assertEqual(r["por_executor"]["codex"], {"n": 1, "ok": 1, "taxa": 1.0, "usd": 0})
        self.assertEqual(r["por_executor"]["claude"]["usd"], 3.0)

    def test_mediana_p90_com_primeira_fora_da_janela(self):
        # 1ª execução há 20 dias (fora de 7), merge há 5 h -> ~475 h
        # 1ª há 10 h, merge há 6 h -> 4 h; 1ª há 12 h, merge há 6 h -> 6 h
        self._banco(uso(480, "linear-krn-1"), uso(2, "linear-krn-1"), uso(10, "linear-krn-2"),
                    uso(12, "linear-krn-3"), uso(9, "linear-krn-3"))
        self._merge("KRN-1", 5)
        self._merge("KRN-2", 6)
        self._merge("KRN-3", 6)
        im = C.resumo(7, self.d, AGORA)["issue_merge_horas"]
        self.assertEqual(im["n"], 3)
        self.assertEqual(im["mediana"], 6.0)
        self.assertEqual(im["p90"], 475.0)

    def test_merge_fora_da_janela_nao_conta(self):
        self._banco(uso(480, "linear-krn-1"))
        self._merge("KRN-1", 400)
        self.assertEqual(C.resumo(7, self.d, AGORA)["issue_merge_horas"]["n"], 0)

    def test_custo_ignora_null_e_conta_sem_custo(self):
        self._banco(uso(10, "linear-krn-1", custo_usd=2.0), uso(9, "linear-krn-1", custo_usd=None),
                    uso(8, "linear-krn-2", custo_usd=4.0), uso(7, "linear-krn-3", custo_usd=100.0))
        self._merge("KRN-1", 1)
        self._merge("KRN-2", 1)  # não fechou
        c = C.resumo(7, self.d, AGORA)["custo_por_issue_fechada"]
        self.assertEqual(c, {"usd": 3.0, "issues": 2, "sem_custo": 1})

    def test_retrabalho_e_desperdicio(self):
        self._banco(uso(10, "linear-krn-1", tipo="conferencia"), uso(9, "linear-krn-1", tipo="recibo"),
                    uso(8, "linear-krn-2"), uso(7, "linear-krn-3", ok=False, limite=True, custo_usd=0.5),
                    uso(6, "rotina-x", ok=False, interrompida=True, custo_usd=0.25))
        r = C.resumo(7, self.d, AGORA)
        self.assertEqual(r["retrabalho"], {"execucoes": 2, "issues": 1, "taxa": 0.333})
        self.assertEqual(r["desperdicio_infra"], {"execucoes": 2, "usd": 0.75})

    def test_incidentes_do_resumo(self):
        self._inc("outra", "", 5)
        self._inc("main_vermelho", "", 4, "feito", nota="tests/test_x.py")
        self._inc("cota_estourada", "", 3, "feito", nota="descartado: duplicata do INC-1")
        self._inc("outra", "", 2)
        inc = incidentes.registrar("p1", "tarefa_parada", "p", prioridade=1, base_dados=self.d, agora=AGORA)
        self._inc("outra", "", 24 * 20)  # fora da janela
        i = C.resumo(7, self.d, AGORA)["incidentes"]
        self.assertEqual(i, {"abertos": 3, "fechados": 2, "fechados_sem_teste": 0, "fechados_isentos": 1, "p1_abertos": 1})
        self.assertTrue(inc)


class Conciliar(Base):
    def test_faltando_e_depois_de_importar(self):
        self._uso(uso(1, "linear-krn-1", custo_usd=1.5), uso(2, "linear-krn-2", custo_usd=2.0))
        telemetria.importar(base_dados=self.d)
        self.assertTrue(C.conciliar(7, self.d, AGORA)["ok"])
        extra = uso(3, "linear-krn-3", custo_usd=1.0)
        self._uso(uso(1, "linear-krn-1", custo_usd=1.5), uso(2, "linear-krn-2", custo_usd=2.0), extra, extra)  # cópia colapsa
        c = C.conciliar(7, self.d, AGORA)
        self.assertEqual((c["uso"], c["banco"], c["faltando"], c["ok"]), (3, 2, 1, False))
        self.assertAlmostEqual(c["usd_uso"] - c["usd_banco"], 1.0)
        telemetria.importar(base_dados=self.d)
        c = C.conciliar(7, self.d, AGORA)
        self.assertEqual((c["uso"], c["banco"], c["faltando"], c["ok"]), (3, 3, 0, True))

    def test_sobrando_no_banco_com_custo_zero_nao_fecha(self):
        self._uso(uso(1, "linear-krn-1", custo_usd=1.5), uso(2, "linear-krn-2", custo_usd=0))
        telemetria.importar(base_dados=self.d)
        self._uso(uso(1, "linear-krn-1", custo_usd=1.5))  # a de custo zero some do uso e fica no banco
        c = C.conciliar(7, self.d, AGORA)
        self.assertEqual((c["uso"], c["banco"], c["faltando"], c["sobrando"], c["ok"]), (1, 2, 0, 1, False))


class Alarme(Base):
    def test_cria_p1_uma_vez_e_nao_reabre(self):
        self._uso(uso(2, ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user ID 1"))
        novos = C.alarme(self.d, AGORA)
        self.assertEqual(len(novos), 1)
        self.assertEqual((novos[0]["classe"], novos[0]["prioridade"], novos[0]["estado"]),
                         ("aviso_falso_ao_dono", 1, "pendente"))
        self.assertIn("limite_sem_chamar", novos[0]["peca"])
        self.assertEqual(C.alarme(self.d, AGORA), [])
        incidentes.marcar(novos[0]["id"], "feito", "tests/test_confiabilidade.py", base_dados=self.d, agora=AGORA,
                          gh=lambda *a, **k: None)
        self.assertEqual(C.alarme(self.d, AGORA), [])
        self.assertEqual(len(incidentes.listar(base_dados=self.d, agora=AGORA)), 1)


class LinhaDeBase(Base):
    def test_caso_de_17_10(self):
        lim = dict(ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user")
        self._uso(uso(2.0, conta_="1", **lim), uso(1.9, conta_="1", **lim), uso(2.0, conta_="2", **lim),
                  uso(1.0, conta_="3"), uso(30, conta_="3"))  # a última é de 16/10
        self._inc("tarefa_parada", "KRN-1", 3)
        self._inc("tarefa_parada", "KRN-2", 4)
        self._inc("cota_estourada", "", 2)
        self._inc("tarefa_parada", "KRN-3", 40)  # outro dia
        lb = C.linha_de_base("2026-10-17", self.d)
        self.assertEqual(lb["dia"], "2026-10-17")
        self.assertEqual(len(lb["fila_parada"]), 2)
        self.assertEqual(len(lb["pausa_falsa"]), 2)  # contas 1 e 2 na mesma hora
        self.assertEqual(lb["rate_limit_github"], 3 + 1)  # 3 linhas + 1 incidente cota_estourada
        self.assertEqual((lb["execucoes"], lb["ok"], lb["taxa"]), (4, 1, 0.25))


class Tela(Base):
    ROTULOS = ("Sucesso", "Issue→merge", "Custo por issue fechada", "Retrabalho", "Avisos falsos")

    def test_banco_vazio(self):
        h = C.tela(self.d, AGORA)
        self.assertIn("telemetria importar", h)

    def test_com_dados_e_escape(self):
        self._banco(uso(10, "linear-krn-1"), uso(2, ok=False, limite=True, turnos=None, erro="API rate limit exceeded for user ID 1"))
        C.marcar_aviso_falso("<script>alert(1)</script>", "KRN-1", self.d, AGORA)
        h = C.tela(self.d, AGORA)
        for r in self.ROTULOS:
            self.assertIn(r, h)
        self.assertIn("US$ (equivalente API nas contas Claude)", h)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("Linha de base 17/10", h)
        self.assertIn("Conciliação", h)


class Cli(Base):
    def test_aviso_falso_e_resumo_json(self):
        import contextlib, io
        from unittest import mock
        with mock.patch("hub.conta.data", return_value=self.d), contextlib.redirect_stdout(io.StringIO()) as o:
            self.assertEqual(C.main(["aviso-falso", "--motivo", "teste", "--issue", "KRN-7"]), 0)
            self.assertEqual(C.main(["resumo", "--json"]), 0)
            self.assertEqual(C.main(["resumo"]), 0)
            self.assertEqual(C.main(["conciliar"]), 0)
        self.assertIn('"janela"', o.getvalue())
        self.assertIn("manual", o.getvalue())


if __name__ == "__main__":
    unittest.main()
