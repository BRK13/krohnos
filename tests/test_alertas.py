import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import alertas
from hub.alertas import (achados_clientes, achados_contas, achados_deploys, achados_sites, decidir, executar,
                         horario_comercial)

UTC = dt.timezone.utc
T = dt.datetime(2026, 10, 13, 13, 0, tzinfo=UTC)  # terça 10:00 BRT
SITE = {"chave": "site:x.com", "tipo": "site", "item": "x.com", "texto": "Site x.com fora do ar (erro 502)", "orch": True}
CONTA = {"chave": "conta:1", "tipo": "conta", "item": "1", "texto": "Conta Claude 1 em 90% do limite da semana", "orch": False}


def m(n):
    return T + dt.timedelta(minutes=n)


def rodada(achados, est, agora):
    """decidir + os avisos entregues (a marca do aviso só entra depois de enviado)."""
    acoes, est = decidir(achados, est, agora)
    executar([a for a in acoes if a[0] == "avisar"], est, agora, lambda e: None, avisar=lambda *_: None)
    return acoes, est


class DecidirTest(unittest.TestCase):
    def test_site_ciclo(self):
        acoes, est = rodada([SITE], {}, T)
        self.assertEqual(acoes, [])
        self.assertEqual(rodada([SITE], est, m(3))[0], [])
        acoes, est = rodada([SITE], est, m(6))
        self.assertEqual(acoes, [("orch", "site:x.com")])
        self.assertEqual(rodada([SITE], est, m(7))[0], [])  # orch ainda sem resposta
        est["itens"]["site:x.com"]["orq_o_que"] = "reiniciei o contêiner"
        acoes, est = rodada([SITE], est, m(8))
        self.assertEqual(acoes, [("avisar", "site:x.com")])
        self.assertEqual(rodada([SITE], est, m(10))[0], [])
        self.assertEqual(rodada([SITE], est, m(24 * 60))[0], [("avisar", "site:x.com")])
        acoes, est = rodada([], est, m(30))
        self.assertEqual((acoes, est["itens"]), ([], {}))

    def test_pisca_pisca(self):
        _, est = rodada([SITE], {}, T)
        _, est = rodada([SITE], est, m(6))
        est["itens"]["site:x.com"]["orq_o_que"] = "reiniciei"
        acoes, est = rodada([SITE], est, m(8))
        self.assertEqual(acoes, [("avisar", "site:x.com")])
        _, est = rodada([], est, m(10))  # voltou
        _, est = rodada([SITE], est, m(12))  # fora de novo, mesmo dia
        self.assertEqual(rodada([SITE], est, m(20))[0], [])
        self.assertEqual(json.loads(json.dumps(est))["avisados"], {"site:x.com": "2026-10-13"})
        _, est = rodada([], est, m(24 * 60))  # volta no dia seguinte (e poda os dias velhos)
        self.assertEqual((est["avisados"], est["orq_chamados"]), ({}, {}))
        _, est = rodada([SITE], est, m(24 * 60 + 2))
        self.assertEqual(rodada([SITE], est, m(24 * 60 + 8))[0], [("orch", "site:x.com")])

    def test_volta_no_mesmo_dia_sem_aviso_ainda_avisa_sem_novo_orq(self):
        _, est = rodada([SITE], {}, T)
        _, est = rodada([SITE], est, m(6))
        _, est = rodada([], est, m(8))  # orch consertou
        _, est = rodada([SITE], est, m(10))
        self.assertEqual(rodada([SITE], est, m(16))[0], [("avisar", "site:x.com")])

    def test_orq_que_morreu_vira_aviso(self):
        _, est = rodada([SITE], {}, T)
        _, est = rodada([SITE], est, m(6))
        self.assertEqual(rodada([SITE], est, m(30))[0][0][0], "avisar")

    def test_sem_orq_avisa_na_hora_e_texto_atualiza(self):
        acoes, est = rodada([CONTA], {}, T)
        self.assertEqual(acoes, [("avisar", "conta:1")])
        acoes, est = rodada([{**CONTA, "texto": "novo"}], est, m(2))
        self.assertEqual((acoes, est["itens"]["conta:1"]["texto"]), ([], "novo"))


class RegrasTest(unittest.TestCase):
    def test_sites(self):
        st = {"https://a-com.example/": 200, "https://b-com.example/": 502, "https://c-com.example/": None, "https://d-com.example/": 404}
        a = achados_sites(["a-com.example", "b-com.example", "c-com.example", "d-com.example"], st.get)
        self.assertEqual([x["texto"] for x in a], ["Site b-com.example fora do ar (erro 502)", "Site c-com.example fora do ar (sem resposta)"])
        self.assertTrue(all(x["orch"] for x in a))

    def test_deploys(self):
        d = lambda st, mins: {"app": "site", "appName": "fsite05-abc", "projeto": "fsite05", "status": st,  # noqa: E731
                              "em": (T - dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")}
        foto = lambda mins, *ds: {"deploys_em": m(-mins).isoformat(), "deploys": list(ds)}  # noqa: E731
        a = achados_deploys(foto(0, d("running", 40)), T)  # foto viu rodando 40 min depois do início
        self.assertEqual(a[0]["chave"], "deploy:fsite05-abc")
        self.assertEqual(a[0]["texto"], "Deploy do fsite05/site travado (rodando há 40 min)")
        self.assertEqual(achados_deploys(foto(0, d("running", 10), d("done", 90)), T), [])
        # foto tirada 2 min depois do início, lida 32 min depois: deploy normal, não travado
        self.assertEqual(achados_deploys(foto(30, d("running", 32)), T), [])
        self.assertEqual(achados_deploys(foto(50, d("running", 90)), T), [])  # foto velha
        self.assertEqual(achados_deploys({"deploys": [d("running", 90)]}, T), [])  # sem deploys_em
        self.assertEqual(achados_deploys(None, T), [])
        sem_nome = {**d("running", 40), "appName": None}
        self.assertEqual(achados_deploys(foto(0, sem_nome), T)[0]["chave"], "deploy:site")

    def test_horario_comercial(self):
        self.assertTrue(horario_comercial(T))
        self.assertFalse(horario_comercial(dt.datetime(2026, 10, 10, 16, 0, tzinfo=UTC)))  # sábado 13h BRT
        self.assertTrue(horario_comercial(dt.datetime(2026, 10, 10, 13, 0, tzinfo=UTC)))  # sábado 10h BRT
        self.assertFalse(horario_comercial(dt.datetime(2026, 10, 11, 13, 0, tzinfo=UTC)))  # domingo

    def test_clientes(self):
        a = achados_clientes([{"ultima_em": "2026-10-13T10:30:00+00:00"}, {"ultima_em": "2026-10-13T09:05:00Z"}], T)
        self.assertEqual(a[0]["texto"], "fsite05: 2 cliente(s) esperando resposta no WhatsApp há mais de 2 h "
                                        "(o mais antigo desde 06:05)")
        self.assertIn("desde 16:05 de 12/10)", achados_clientes([{"ultima_em": "2026-10-12T19:05:00Z"}], T)[0]["texto"])
        self.assertEqual((a[0]["chave"], a[0]["orch"]), ("cliente:fsite05", False))
        self.assertEqual(achados_clientes([], T), [])

    def test_cliente_consulta_no_maximo_a_cada_10_min(self):
        chamadas = []
        crm = lambda agora: chamadas.append(agora) or [{"ultima_em": "2026-10-13T10:30:00Z"}]  # noqa: E731
        est = {}
        a = alertas._cliente(est, T, crm)
        _, est = decidir(a, est, T)
        self.assertEqual(alertas._cliente(est, m(4), crm)[0]["texto"], a[0]["texto"])
        self.assertEqual(len(chamadas), 1)
        alertas._cliente(est, m(10), crm)
        self.assertEqual(len(chamadas), 2)
        self.assertEqual(alertas._cliente({}, dt.datetime(2026, 10, 10, 16, 0, tzinfo=UTC), crm), [])

    def test_contas(self):
        uso = {"contas": {"1": {"semana": {"pct": 86.4, "reset": "2026-10-15T15:00:00+00:00"}},
                          "2": {"semana": {"pct": None, "reset": None}}, "3": {"semana": {"pct": 80.0}}},
               "orquestrador": {"semana": {"pct": 99.0, "reset": None}}}
        a = achados_contas(uso)
        self.assertEqual([x["texto"] for x in a], ["Conta Claude 1 em 86% do limite da semana (volta 15/10 12:00)",
                                                  "Conta Claude orquestrador em 99% do limite da semana"])
        self.assertEqual(a[1]["chave"], "conta:orquestrador")


class ExecucaoTest(unittest.TestCase):
    def test_orq_e_aviso(self):
        _, est = decidir([SITE], {}, T)
        acoes, est = decidir([SITE], est, m(6))
        salvos, prompts, msgs = [], [], []
        executar(acoes, est, m(6), salvos.append, chamar_orq=lambda p: prompts.append(p) or "reiniciei o traefik",
                 avisar=lambda t, u: msgs.append(t))
        self.assertIn("Alerta do hub: Site x.com fora do ar (erro 502), desde 10:00 (Brasília)", prompts[0])
        self.assertEqual(est["itens"]["site:x.com"]["orq_o_que"], "reiniciei o traefik")
        acoes, est = decidir([SITE], est, m(8))
        executar(acoes, est, m(8), salvos.append, avisar=lambda t, u: msgs.append(t))
        self.assertEqual(msgs, ["Site x.com fora do ar (erro 502), desde 10:00. Orquestrador: reiniciei o traefik"])
        self.assertEqual(len(salvos), 2)

    def test_aviso_que_falha_tenta_de_novo(self):
        acoes, est = decidir([CONTA], {}, T)

        def falha(*_):
            raise OSError("caixa cheia")
        self.assertEqual(executar(acoes, est, T, lambda e: None, avisar=falha), 1)
        self.assertEqual(decidir([CONTA], est, m(2))[0], [("avisar", "conta:1")])

    def test_falha_qualquer_desfaz_a_marca_e_segue(self):
        _, est = decidir([SITE], {}, T)
        acoes, est = decidir([SITE, CONTA], est, m(6))
        self.assertEqual(acoes, [("orch", "site:x.com"), ("avisar", "conta:1")])
        msgs = []
        codigo = executar(acoes, est, m(6), lambda e: None, chamar_orq=lambda p: 1 / 0,
                          avisar=lambda t, u: msgs.append((t, u)))
        self.assertEqual(codigo, 1)
        self.assertEqual(msgs, [("Conta Claude 1 em 90% do limite da semana, desde 10:06.", False)])  # conta não é urgente
        self.assertEqual(decidir([SITE, CONTA], est, m(8))[0], [("orch", "site:x.com")])

    def test_morte_no_meio_nao_marca_aviso_nao_enviado(self):
        conta2 = {**CONTA, "chave": "conta:2", "item": "2", "texto": "Conta Claude 2 em 95% do limite da semana"}
        acoes, est = decidir([SITE, CONTA, conta2], {"itens": {"site:x.com": {"desde": m(-6).isoformat()}}}, T)
        self.assertEqual(acoes, [("orch", "site:x.com"), ("avisar", "conta:1"), ("avisar", "conta:2")])
        ordem, salvos = [], []

        def avisar(t, u):
            if "Claude 2" in t:
                raise KeyboardInterrupt  # processo morto no meio (BaseException escapa)
            ordem.append("avisar")
        with self.assertRaises(KeyboardInterrupt):
            executar(acoes, est, T, lambda e: salvos.append(json.loads(json.dumps(e))),
                     chamar_orq=lambda p: ordem.append("orch"), avisar=avisar)
        self.assertEqual(ordem, ["avisar"])  # avisos antes do orch
        self.assertEqual(salvos[-1]["avisados"], {"conta:1": "2026-10-13"})
        self.assertIsNone(salvos[-1]["itens"]["conta:2"]["avisado_dia"])

    def test_rodar_orq_morto_avisos_ja_entregues(self):
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / "claude").mkdir()
            (Path(base) / "claude" / "uso-contas.json").write_text(json.dumps(
                {"contas": {"1": {"semana": {"pct": 90.0, "reset": None}}}}))
            alertas.salvar_estado(Path(base) / "alertas" / "estado.json", {"itens": {"site:x.com": {
                "tipo": "site", "item": "x.com", "texto": "t", "desde": m(-6).isoformat()}}})
            ordem = []

            def orch(p):
                ordem.append("orch")
                raise KeyboardInterrupt
            with mock.patch.object(alertas.sites, "ativos", return_value=[{"dominio": "x.com"}]), \
                    mock.patch.object(alertas.saude, "ler", return_value=None), self.assertRaises(KeyboardInterrupt):
                alertas.rodar(base, T, http_get=lambda u: 502, consultar_crm=lambda a: [], chamar_orq=orch,
                              avisar=lambda t, u: ordem.append("avisar"))
            est = json.loads((Path(base) / "alertas" / "estado.json").read_text())
        self.assertEqual(ordem, ["avisar", "orch"])
        self.assertEqual(est["avisados"], {"conta:1": "2026-10-13"})
        self.assertEqual(est["orq_chamados"], {"site:x.com": "2026-10-13"})  # orch marcado antes: não chama de novo

    def test_fonte_com_erro_mantem_os_itens(self):
        est = {"itens": {"site:x.com": {"tipo": "site", "item": "x.com", "texto": "Site x.com fora do ar (erro 502)",
                                        "desde": T.isoformat()}}}
        with mock.patch.object(alertas.sites, "ativos", return_value=[]), \
                mock.patch.object(alertas.saude, "ler", return_value=None):
            a = alertas.coletar(est, m(2), "/nao-existe", http_get=lambda u: 200, consultar_crm=lambda a: [])
        self.assertEqual(a, [SITE])
        with mock.patch.object(alertas.sites, "ativos", side_effect=OSError("x")), \
                mock.patch.object(alertas.saude, "ler", return_value=None):
            self.assertEqual(alertas.coletar(est, m(2), "/nao-existe", http_get=lambda u: 200,
                                             consultar_crm=lambda a: []), [SITE])

    def test_desde_de_outro_dia(self):
        it = {"texto": "X", "desde": "2026-10-12T13:00:00+00:00"}
        self.assertEqual(alertas.mensagem(it, T), "X, desde 10:00 de 12/10.")
        self.assertEqual(alertas.mensagem({**it, "tipo": "cliente"}, T), "X.")

    def test_rodar_grava_estado_no_formato(self):
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / "claude").mkdir()
            (Path(base) / "claude" / "uso-contas.json").write_text(json.dumps(
                {"contas": {"1": {"semana": {"pct": 90.0, "reset": None}}}}))
            msgs = []
            with mock.patch.object(alertas.sites, "ativos", return_value=[{"dominio": "x.com"}]), \
                    mock.patch.object(alertas.saude, "ler", return_value=None):
                codigo = alertas.rodar(base, T, http_get=lambda u: 503, consultar_crm=lambda a: 1 / 0,
                                       chamar_orq=lambda p: self.fail("cedo demais"), avisar=lambda t, u: msgs.append(t))
            self.assertEqual(codigo, 0)
            self.assertEqual(msgs, ["Conta Claude 1 em 90% do limite da semana, desde 10:00."])
            est = json.loads((Path(base) / "alertas" / "estado.json").read_text())
            self.assertEqual(est["cliente_checado_em"], None)  # CRM falhou: tenta de novo
            self.assertEqual(set(est["itens"]), {"site:x.com", "conta:1"})
            for it in est["itens"].values():
                self.assertEqual(set(it), {"tipo", "item", "texto", "desde", "orq_em", "orq_o_que", "avisado_dia"})
            self.assertEqual(est["itens"]["conta:1"]["avisado_dia"], "2026-10-13")
            self.assertEqual(est["itens"]["site:x.com"]["desde"], "2026-10-13T13:00:00+00:00")

    def test_sem_avisar_nao_grava_nem_chama(self):
        with tempfile.TemporaryDirectory() as base, \
                mock.patch.object(alertas.sites, "ativos", return_value=[{"dominio": "x.com"}]), \
                mock.patch.object(alertas.saude, "ler", return_value=None):
            alertas.rodar(base, T, sem_avisar=True, http_get=lambda u: None, consultar_crm=lambda a: [],
                          chamar_orq=lambda p: self.fail("chamou"), avisar=lambda *_: self.fail("avisou"))
            self.assertFalse((Path(base) / "alertas").exists())


class ImportOpcionalTest(unittest.TestCase):
    """só o módulo ausente (snapshot público) some em silêncio; dependência quebrada segue o caminho de erro de antes."""

    def test_faltou_so_reconhece_o_proprio_modulo_ausente(self):
        from hub import faltou
        with self.assertRaises(ImportError) as c:
            import hub.modulo_que_nao_existe  # noqa: F401
        self.assertTrue(faltou(c.exception, "hub.modulo_que_nao_existe"))
        self.assertFalse(faltou(c.exception, "hub.outro"))
        self.assertFalse(faltou(ImportError("cannot import name 'x'", name="hub.modulo_que_nao_existe"), "hub.modulo_que_nao_existe"))

    def test_triagem_ausente_a_regra_do_crm_nao_acha_nada(self):
        import sys
        with mock.patch.dict(sys.modules, {"hub.clientes_esperando": None}):
            self.assertEqual(alertas._cliente({}, T, alertas.consultar_crm), [])

    def test_triagem_quebrada_cai_no_log_e_mantem_o_achado_anterior(self):
        import sys
        import types
        velho = {"cliente_triagem_v1": True, "itens": {"cliente:x": {"tipo": "cliente", "item": "x", "texto": "antigo"}}}
        quebrado = types.ModuleType("hub.clientes_esperando")  # existe, mas sem `selecionar`: ImportError que não é ausência
        with mock.patch.dict(sys.modules, {"hub.clientes_esperando": quebrado}), \
                mock.patch("builtins.print") as log:
            achados = alertas._cliente(velho, T, alertas.consultar_crm)
        self.assertEqual([a["texto"] for a in achados], ["antigo"])
        self.assertIn("falha ao checar cliente: ImportError", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
