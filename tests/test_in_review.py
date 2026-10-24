"""In Review parada > 2 h não fica esperando. Casos reais de 21/10: (recibo ok e no ar, ninguém fechava),
(no ar, falta o par antes/depois), (repasse sem resposta), (merge sem deploy) e o laço sem fim."""
import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from hub import conta, esperas, in_review, recibo

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
PR = "https://github.com/example-org/clienteh/pull/1210"


def issue(ident, horas=5, prio=3, labels=(), corpo="", desc=""):
    quando = conta.iso(T0 - dt.timedelta(hours=horas))
    return {"id": f"id-{ident}", "identifier": ident, "title": "t", "updatedAt": quando, "priority": prio, "description": desc,
            "labels": {"nodes": [{"name": n} for n in labels]},
            "comments": {"nodes": [{"body": corpo, "createdAt": quando}]}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        p = mock.patch.dict(os.environ, {"HUB_DATA": tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.fechadas, self.reabertas, self.chamados = [], [], []

    def rodar(self, issues, ociosas=1, ocupadas=(), pendentes=(), fundido=True, recibo=None, **kw):
        recibo = recibo or {"ok": True, "faltas": []}
        consulta = lambda q, **v: ({"issues": {"nodes": issues, "pageInfo": {"hasNextPage": False}}} if "issues(" in q  # noqa: E731
                                    else {"issue": {"identifier": v["i"]}})
        gh = lambda caminho: {"merged": fundido, "state": "closed" if fundido else "open"}  # noqa: E731
        item = lambda ident: {"id": f"id-{ident}", "identifier": ident, "estados": {}}  # noqa: E731
        with mock.patch("hub.linear.item_por_ident", side_effect=item):
            return in_review.varrer(
                consulta=consulta, gh=gh, avaliar=lambda i: recibo, agora=T0, base_dados=self.dados, log=lambda *a: None,
                ociosas=ociosas, ocupadas=set(ocupadas), pendentes=set(pendentes), forcar=True,
                fechar=lambda it, estado, texto: self.fechadas.append((it["identifier"], estado, texto)),
                reabrir=lambda ident, fala: self.reabertas.append((ident, fala)) or mock.Mock(nome=f"linear-{ident.lower()}"),
                enfileirar=kw.pop("enfileirar", lambda t, motivo, base=None: True),
                chamar_orq=lambda ident, motivo, origem, base, agora: self.chamados.append(ident) or True, **kw)


class FecharTest(Base):
    def test_recibo_completo_e_no_ar_fecha_sem_ia(self):  #
        r = self.rodar([issue("KRN-1583", corpo=f"PR {PR} mergeado")])
        self.assertEqual(r, {"KRN-1583": "fechada"})
        self.assertEqual(self.fechadas[0][:2], ("KRN-1583", "Done"))
        self.assertIn("Fechado pelo hub, sem IA", self.fechadas[0][2])
        self.assertEqual(self.reabertas, [])

    def test_fecha_mesmo_sem_conta_ociosa(self):
        self.assertEqual(self.rodar([issue("KRN-1583", corpo=PR)], ociosas=0), {"KRN-1583": "fechada"})

    def test_seo_fix_nao_fecha_sem_ia(self):  # INC-117: a sessão repete a auditoria
        r = self.rodar([issue("KRN-655", labels=["seo-fix"], corpo=PR)])
        self.assertEqual(r, {"KRN-655": "reenfileirada"})
        self.assertEqual(self.fechadas, [])

    def test_recibo_com_falta_volta_para_a_fila_com_a_falta_na_fala(self):  #
        r = self.rodar([issue("KRN-1584", corpo=PR)], recibo={"ok": False, "faltas": ["falta o par antes e depois"]})
        self.assertEqual(r, {"KRN-1584": "reenfileirada"})
        self.assertIn("falta o par antes e depois", self.reabertas[0][1])

    def test_pr_aberto_nao_fecha(self):
        self.assertEqual(self.rodar([issue("KRN-1513", corpo=PR)], fundido=False), {"KRN-1513": "reenfileirada"})
        self.assertEqual(self.fechadas, [])


class ReenfileirarTest(Base):
    def test_inc_349_fila_criada_na_rodada_zera_alarme(self):
        in_review._gravar({"acima_desde": conta.iso(T0 - dt.timedelta(hours=1))}, self.dados)
        with mock.patch("hub.incidentes.registrar") as registrar:
            feitos = self.rodar([issue(f"KRN-{n}") for n in range(98)], ociosas=4)
        self.assertEqual(len(feitos), 4)
        est = in_review.estado(self.dados)
        self.assertEqual(est["paradas"], 94)
        self.assertNotIn("acima_desde", est)
        registrar.assert_not_called()

    def test_sem_conta_ociosa_nada_volta(self):
        self.assertEqual(self.rodar([issue("KRN-1")], ociosas=0), {})

    def test_orcamento_esgotado_grava_varrida_e_retoma_do_que_ficou(self):  # 22/10: 117 itens, varrida_em parado há 7 h
        itens = [issue(f"KRN-{n}", prio=3) for n in range(1, 5)]
        tempos = iter(range(0, 1000, 60))  # cada leitura do relógio passa 60 s
        r = self.rodar(itens, ociosas=0, orcamento=100, relogio=lambda: next(tempos))
        self.assertEqual(r, {})
        est = in_review.estado(self.dados)
        self.assertIn("varrida_em", est)
        self.assertEqual(sorted(est["checadas"]), ["KRN-1"])
        tempos = iter(range(0, 1000, 60))
        in_review.varrer(consulta=lambda q, **v: {"issues": {"nodes": itens, "pageInfo": {"hasNextPage": False}}},
                         gh=lambda c: {}, avaliar=lambda i: {"ok": False, "faltas": []},
                         agora=T0, base_dados=self.dados, log=lambda *a: None, ociosas=0, ocupadas=set(), pendentes=set(),
                         forcar=True, orcamento=100, relogio=lambda: next(tempos))
        self.assertEqual(sorted(in_review.estado(self.dados)["checadas"]), ["KRN-1", "KRN-2"])  # retomou por, não repetiu

    def test_uma_por_conta_ociosa_urgente_primeiro_depois_a_mais_velha(self):
        r = self.rodar([issue("KRN-1", horas=30, prio=3), issue("KRN-2", horas=3, prio=1), issue("KRN-3", horas=9, prio=3)], ociosas=2)
        self.assertEqual(list(r), ["KRN-2", "KRN-1"])

    def test_fila_que_ja_existe_ocupa_as_contas_ociosas(self):
        self.assertEqual(self.rodar([issue("KRN-1")], ociosas=1, ocupadas=["linear-krn-9"]), {})

    def test_tarefa_rodando_nao_come_a_vaga_da_conta_ociosa(self):
        r = self.rodar([issue("KRN-1")], ociosas=1, ocupadas=["linear-krn-9"], esperando=set())
        self.assertEqual(r, {"KRN-1": "reenfileirada"})

    def test_fila_recusou_devolve_ao_in_review(self):
        devolvidas = []
        r = self.rodar([issue("KRN-1")], enfileirar=lambda t, m, base=None: False, devolver=devolvidas.append)
        self.assertEqual((r, devolvidas), ({}, ["KRN-1"]))

    def test_fora_da_varredura(self):
        issues = [issue("KRN-1"), issue("KRN-2"), issue("KRN-3"), issue("KRN-4", desc="voltar-em: 2026-10-25"),
                  issue("KRN-5", corpo="voltar-em: 2026-10-20")]
        esperas.gravar("KRN-3", PR, chamar=lambda c: {"head": {"sha": "a"}, "merged": False}, base_dados=self.dados, agora=T0)
        r = self.rodar(issues, ociosas=9, ocupadas=["linear-krn-1"], pendentes=["KRN-2"])
        self.assertEqual(list(r), ["KRN-5"])  # data vencida volta; data futura, fila, proposta e espera viva ficam fora

    def test_cooldown_e_teto_de_voltas_chamam_o_orquestrador(self):
        i = [issue("KRN-1")]
        self.assertEqual(self.rodar(i), {"KRN-1": "reenfileirada"})
        self.assertEqual(self.rodar(i), {})  # dentro do cooldown
        est = in_review.estado(self.dados)
        for n in (1, 2):
            est["voltas"]["KRN-1"] = {"n": n, "em": conta.iso(T0 - dt.timedelta(hours=7))}
            in_review._gravar(est, self.dados)
            self.assertEqual(self.rodar(i), {"KRN-1": "reenfileirada"})
        est["voltas"]["KRN-1"] = {"n": 3, "em": conta.iso(T0 - dt.timedelta(hours=7))}
        in_review._gravar(est, self.dados)
        self.assertEqual(self.rodar(i), {"KRN-1": "orquestrador"})
        self.assertEqual(self.chamados, ["KRN-1"])


class ParaleloTest(Base):
    """a conferência (GitHub, Linear) corre em paralelo; em série o orçamento de 90 s acabava em toda rodada."""

    def test_fechavel_roda_em_varias_threads_ao_mesmo_tempo_e_resultado_igual_ao_serial(self):
        itens = [issue(f"KRN-{n}", prio=n, corpo=f"PR {PR}") for n in range(1, 5)]
        barreira = threading.Barrier(4, timeout=5)  # em série a barreira quebra e nada fecha
        threads = set()
        real = in_review.fechavel

        def fechavel(i, gh, avaliar, consulta):
            threads.add(threading.get_ident())
            barreira.wait()
            return real(i, gh, avaliar, consulta)
        with mock.patch.object(in_review, "fechavel", fechavel):
            r = self.rodar(itens, ociosas=0)
        paralelo = (r, list(self.fechadas))
        self.assertGreater(len(threads), 1)
        self.assertEqual(r, {f"KRN-{n}": "fechada" for n in range(1, 5)})
        self.fechadas = []
        self.setUp()  # outra pasta de dados: mesma entrada, sem barreira
        self.assertEqual((self.rodar(itens, ociosas=0), self.fechadas), paralelo)
        self.assertEqual([f[0] for f in paralelo[1]], ["KRN-1", "KRN-2", "KRN-3", "KRN-4"])  # gravação na ordem de prioridade

    def test_sem_rede_confere_inline_sem_pool(self):
        principal, vistos = threading.get_ident(), []
        real = in_review.fechavel

        def fechavel(i, *a):
            vistos.append(threading.get_ident())
            return real(i, *a)
        with mock.patch.object(in_review, "fechavel", fechavel):
            r = self.rodar([issue("KRN-655", labels=["seo-fix"], corpo=PR), issue("KRN-656")], ociosas=2)
        self.assertEqual(r, {"KRN-655": "reenfileirada", "KRN-656": "reenfileirada"})
        self.assertEqual(vistos, [principal, principal])

    def test_leitura_que_nao_volta_no_orcamento_fica_para_a_proxima_rodada(self):
        solta = threading.Event()
        self.addCleanup(solta.set)

        def gh(caminho):
            solta.wait(5)
            return {"merged": True}
        r = in_review.varrer(consulta=lambda q, **v: {"issues": {"nodes": [issue("KRN-1", corpo=PR)], "pageInfo": {"hasNextPage": False}}},
                             gh=gh, avaliar=lambda i: {"ok": True}, agora=T0, base_dados=self.dados, log=lambda *a: None,
                             ociosas=1, ocupadas=set(), pendentes=set(), forcar=True, orcamento=0.2, relogio=time.monotonic,
                             reabrir=lambda *a: self.fail("não reabre sem a leitura"))
        self.assertEqual(r, {})
        est = in_review.estado(self.dados)
        self.assertIn("varrida_em", est)
        self.assertEqual(est["checadas"], {})  # não entra em `novas`: volta primeiro na próxima rodada


class OrcamentoPorCargaTest(Base):
    def test_orcamento_pela_carga(self):
        self.assertEqual(in_review.orcamento_s(0.3), 300)
        self.assertEqual(in_review.orcamento_s(0.5), 90)
        self.assertEqual(in_review.orcamento_s(1.2), 90)
        for carga, esperado in ((None, 90), (0.1, 300), (0.7, 90)):
            with self.subTest(carga=carga), mock.patch("hub.agenda.carga_por_nucleo", return_value=carga):
                self.assertEqual(in_review.orcamento_s(), esperado)

    def test_varrer_sem_orcamento_usa_o_da_maquina(self):
        itens = [issue(f"KRN-{n}", prio=3) for n in range(1, 5)]
        for carga, checadas in ((0.2, ["KRN-1", "KRN-2", "KRN-3", "KRN-4"]), (0.9, ["KRN-1"])):
            with self.subTest(carga=carga), mock.patch("hub.agenda.carga_por_nucleo", return_value=carga):
                in_review._gravar({}, self.dados)
                tempos = iter(range(0, 1000, 60))  # cada leitura do relógio passa 60 s
                self.rodar(itens, ociosas=0, relogio=lambda: next(tempos))
                self.assertEqual(sorted(in_review.estado(self.dados)["checadas"]), checadas)


class MedirTest(Base):
    def medir(self, n, agora, ociosas=2, esperando=()):
        consulta = lambda q, **v: {"issues": {"nodes": [issue(f"KRN-{k}") for k in range(n)], "pageInfo": {"hasNextPage": False}}}  # noqa: E731
        incs = []
        with mock.patch("hub.agenda.fila", return_value=[{"nome": n} for n in esperando]):
            est = in_review.medir(consulta, agora, self.dados, set(), set(), ociosas, lambda *a: None,
                                  incidente=lambda *a, **k: incs.append((a, k)))
        return est, incs

    def test_painel_recebe_paradas_e_ociosas(self):
        est, incs = self.medir(4, T0)
        self.assertEqual((est["paradas"], est["ociosas"]), (4, 2))
        self.assertEqual(incs, [])

    def test_mais_de_10_por_30_min_abre_p1(self):
        est, incs = self.medir(11, T0)
        self.assertEqual(incs, [])  # começou agora
        est, incs = self.medir(11, T0 + dt.timedelta(minutes=29))
        self.assertEqual(incs, [])
        est, incs = self.medir(11, T0 + dt.timedelta(minutes=30))
        self.assertEqual(len(incs), 1)
        self.assertEqual(incs[0][1]["prioridade"], 1)
        self.assertEqual(incs[0][0][1], "in_review_parada")

    def test_inc399_cooldown_nao_e_omissao_e_expiracao_exige_nova_janela(self):
        in_review._gravar({"voltas": {f"KRN-{k}": {"n": 1, "em": conta.iso(T0)}
                                     for k in range(21)},
                           "acima_desde": conta.iso(T0 - dt.timedelta(hours=1))}, self.dados)
        for minutos in (30, 359):
            est, incs = self.medir(21, T0 + dt.timedelta(minutes=minutos), ociosas=4)
            self.assertEqual((est["paradas"], est.get("elegiveis")), (21, 0))
            self.assertNotIn("acima_desde", est)
            self.assertEqual(incs, [])
        self.assertEqual(self.medir(21, T0 + dt.timedelta(hours=6))[1], [])
        self.assertEqual(self.medir(21, T0 + dt.timedelta(hours=6, minutes=29))[1], [])
        est, incs = self.medir(21, T0 + dt.timedelta(hours=6, minutes=30))
        self.assertEqual(len(incs), 1)
        self.assertEqual(est["elegiveis"], 21)

    def test_inc_292_fila_cobre_conta_ociosa_sem_alarme(self):
        for minutos in (0, 30, 60):
            est, incs = self.medir(26, T0 + dt.timedelta(minutes=minutos),
                                   ociosas=1, esperando=["linear-krn-1600"])
            self.assertEqual(incs, [])
            self.assertNotIn("acima_desde", est)
            self.assertEqual((est["paradas"], est["ociosas"]), (26, 1))

    def test_sem_vaga_zera_janela_e_exige_novos_30_min(self):
        for esperando, ociosas in (((), 0), (("linear-krn-1600",), 1)):
            with self.subTest(esperando=esperando, ociosas=ociosas):
                self.medir(26, T0)
                est, incs = self.medir(26, T0 + dt.timedelta(minutes=29),
                                       ociosas=ociosas, esperando=esperando)
                self.assertNotIn("acima_desde", est)
                self.assertEqual(incs, [])
                self.assertEqual(self.medir(26, T0 + dt.timedelta(minutes=30))[1], [])
                self.assertEqual(self.medir(26, T0 + dt.timedelta(minutes=59))[1], [])
                self.assertEqual(len(self.medir(26, T0 + dt.timedelta(minutes=60))[1]), 1)

    def test_fila_parcial_ainda_deixa_vaga_e_dispara(self):
        self.medir(26, T0, ociosas=2, esperando=["linear-krn-1600"])
        self.assertEqual(len(self.medir(26, T0 + dt.timedelta(minutes=30),
                                       ociosas=2, esperando=["linear-krn-1600"])[1]), 1)

    def test_voltar_a_10_zera_a_contagem(self):
        self.medir(11, T0)
        self.medir(10, T0 + dt.timedelta(minutes=20))
        est, incs = self.medir(11, T0 + dt.timedelta(minutes=40))
        self.assertEqual(incs, [])


if __name__ == "__main__":
    unittest.main()


class Inc391ConsultaRecente(unittest.TestCase):
    def test_data_futura_nos_comentarios_recentes(self):
        from hub import in_review
        agora = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)
        def consulta(q, **v):
            recente = "comments(first: 10)" in q
            return {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "description": "", "comments": {"nodes": [{"createdAt": "2026-10-22T20:54:10Z",
                "body": "voltar-em: 2026-10-29" if recente else "PR no CI"}]}}]}}
        issue = in_review.paradas_na_linear(consulta, agora)[0]
        self.assertEqual(in_review.data_declarada(issue), dt.date(2026, 10, 29))

class Inc388(Base):
    PAUSA = (f"PR {PR} mergeado e no ar. Os testes de simulação passaram. "
             f"Falta (depende do {recibo._DONO}): recarregar a conta da OpenAI; depois uma chamada mini real "
             f"e relatorio --dias 7 no PR. Nenhuma conta deve retomar a issue antes da decisão do {recibo._DONO}.")

    def test_pausa_por_dinheiro_nao_fecha_nem_reenfileira(self):
        for recibo in ({"ok": True}, {"ok": False, "faltas": ["prova de uso real ausente"]}):
            with self.subTest(recibo=recibo):
                self.assertEqual(self.rodar([issue("KRN-1511", corpo=self.PAUSA)], recibo=recibo), {})
                self.assertEqual((self.fechadas, self.reabertas, self.chamados), ([], [], []))
                self.assertNotIn("KRN-1511", in_review.estado(self.dados).get("voltas", {}))

    def test_fala_nova_libera_e_pausa_historica_nao_prevalece(self):
        i = issue("KRN-1511", corpo=self.PAUSA)
        i["comments"]["nodes"].append({"body": "Saldo resolvido; retome e faça a prova.",
                                        "createdAt": conta.iso(T0)})
        self.assertEqual(self.rodar([i], recibo={"ok": False, "faltas": ["prova ausente"]}),
                         {"KRN-1511": "reenfileirada"})

    def test_pr_sem_pausa_ou_sem_dependencia_continua_elegivel(self):
        for texto in (f"PR {PR} mergeado; depende do Fulano revisar.",
                      f"PR {PR} mergeado. Nenhuma conta deve retomar a issue."):
            with self.subTest(texto=texto):
                self.assertEqual(self.rodar([issue("KRN-1511", corpo=texto)]), {"KRN-1511": "fechada"})

    def test_thread_de_sessao_nao_libera_a_pausa(self):
        i = issue("KRN-1511", corpo=self.PAUSA)
        i["comments"]["nodes"].append({"body": "This thread is for an agent session with claude.",
                                        "createdAt": conta.iso(T0)})
        self.assertEqual(self.rodar([i]), {})
