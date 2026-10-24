"""o porteiro do WhatsApp do dono (técnico, madrugada, teto, dedupe, versão do dia, resumo, relatório)."""
import datetime as dt, json, os, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock

from hub.comum import caixa, porteiro
from hub.comum.datas import BRT

DIA = dt.datetime(2026, 10, 16, 10, 0, tzinfo=BRT)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        self.bot, self.dados = raiz / "bot", raiz / "dados"
        self.caixa = self.bot / "avisos"
        self.caixa.mkdir(parents=True)
        self.saida, self.desviados = [], []
        p = mock.patch.dict(os.environ, {"TRIAGEM_ADMIN": "5500000000002"})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TAREFA_NOME", None)

    def arq(self, nome, conteudo=b"x"):
        a = self.bot / "envios" / nome
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_bytes(conteudo)
        return f"envios/{nome}"

    def enviar(self, texto, quando=DIA, arquivos=None, **kw):
        def gravar(t, a):
            self.saida.append((t, list(a or [])))
            return Path("aviso.json")
        return porteiro.passar(texto, caixa=self.caixa, arquivos=arquivos, base_dados=str(self.dados), quando=quando,
                               gravar=gravar, desviar=self.desviados.append, **kw)

    def descarregar(self, quando):
        def gravar(t, a):
            self.saida.append((t, list(a or [])))
        return porteiro.descarregar(self.caixa, str(self.dados), quando, gravar)


class AvisosQueFuraram(Base):
    """o que chegou ao zap do Fulano em 17/10 (resumo vazio 2x, FALHOU/OAuth, serviço parado)."""

    def test_resumo_vazio_nao_sai(self):
        for hora in (18, 18):
            self.enviar(f"📋 Resumo {hora}:00: nada pendente nas últimas 24h.", quando=DIA.replace(hour=hora))
        self.assertEqual([], self.saida)
        self.assertEqual([], self.desviados)
        self.assertEqual(2, sum(e["tipo"] == "vazio" for e in porteiro._ler(str(self.dados))["envios"]))

    def test_um_resumo_por_janela_e_conteudo_igual_nao_repete(self):
        r1 = "📋 Resumo das últimas 24h (2 pedidos)\n• a"
        self.enviar(r1, quando=DIA.replace(hour=18), origem="resumo-diario")
        self.enviar(r1.replace("2 pedidos", "3 pedidos"), quando=DIA.replace(hour=18, minute=9), origem="resumo-diario")
        self.assertEqual(1, len(self.saida))  # mesma janela
        dia2 = DIA + dt.timedelta(days=1)
        self.enviar(r1, quando=dia2.replace(hour=18), origem="resumo-diario")  # outro dia, mesmo conteúdo
        self.assertEqual(1, len(self.saida))
        self.enviar("📋 Resumo das últimas 24h (4 pedidos)\n• b", quando=dia2.replace(hour=18, minute=5), origem="resumo-diario")
        self.assertEqual(2, len(self.saida))

    def test_resumo_de_outro_assunto_nao_e_engolido_pelo_resumo_do_dia(self):
        self.enviar("📋 Resumo das últimas 24h (2 pedidos)\n• a", quando=DIA.replace(hour=18), origem="resumo-diario")
        self.enviar("Resumo do contrato da Cleide: assinatura amanhã", quando=DIA.replace(hour=18, minute=20))
        self.assertEqual(2, len(self.saida))

    def test_resumo_so_marca_o_dia_quando_sai(self):
        self.enviar("📋 Resumo das últimas 24h (2 pedidos)\n• a", quando=DIA.replace(hour=22, minute=30), origem="resumo-diario")
        self.assertEqual([], self.saida)  # madrugada: cai na fila, sem marcar o dia
        self.assertEqual({}, porteiro._ler(str(self.dados))["resumos"])
        self.enviar("📋 Resumo das últimas 24h (2 pedidos)\n• a", quando=DIA.replace(hour=18), origem="resumo-diario")
        self.assertEqual(1, len(self.saida))  # saiu agora: marca o dia
        self.enviar("📋 Resumo das últimas 24h (3 pedidos)\n• b", quando=DIA.replace(hour=18, minute=5), origem="resumo-diario")
        self.assertEqual(1, len(self.saida))  # mesma janela: repetido

    def test_entrega_com_nextjs_parada_e_anexo_sai(self):
        arq = self.arq("relatorio.pdf")
        self.enviar("KRN-1 pronta: relatório do Next.js, a parada ficou boa", arquivos=[arq], tipo="entrega")
        self.assertEqual(1, len(self.saida))
        self.assertEqual([], self.desviados)
    def test_hora_do_conteudo_diferencia_resumos_so_a_do_titulo_e_ignorada(self):
        dia2 = DIA + dt.timedelta(days=1)
        self.enviar("📋 Resumo 18:00\n• reunião às 10:00", quando=DIA.replace(hour=18))
        self.enviar("📋 Resumo 18:05\n• reunião às 11:00", quando=dia2.replace(hour=18))  # mudou de verdade: sai
        self.assertEqual(2, len(self.saida))
        self.enviar("📋 Resumo 19:00\n• reunião às 11:00", quando=dia2.replace(hour=19) + dt.timedelta(days=1))
        self.assertEqual(2, len(self.saida))  # só a hora do título mudou: repetido

    def test_falhou_oauth_servico_parado_claude_vps_vao_ao_orquestrador(self):
        textos = ["Claude VPS · resumo-diario FALHOU\nFALHOU: OAuth session expired · Please run /login",
                  "Orquestrador: o serviço está parado há mais de 30 min.",
                  "Deu erro ao gerar o relatório", "veja /var/lib/nimbus/reports/x.log"]
        for t in textos:
            self.enviar(t)
        self.assertEqual([], self.saida)
        self.assertEqual(textos, self.desviados)

    def test_falha_de_tarefa_nao_vai_nem_com_arquivo(self):
        self.enviar("Claude VPS · coleta FALHOU", arquivos=[self.arq("a.pdf")])
        self.assertEqual([], self.saida)

    def test_entrega_com_erro_no_titulo_passa(self):
        self.enviar("KRN-9 pronta: corrige o erro de login do site")
        self.assertEqual(1, len(self.saida))

    def test_falha_com_tipo_explicito_e_anexo_ainda_desvia(self):
        for texto in ("OAuth expirou", "FALHOU no coletor"):
            self.enviar(texto, tipo="entrega", arquivos=[self.arq("prova.pdf")])
        self.assertEqual([], self.saida)
        self.assertEqual(2, len(self.desviados))


class EvidenciaPronto(Base):
    def test_cabecalho_afirmativo_sem_confundir_negacao(self):
        for texto in ("KRN-9 não está pronta", "KRN-9 ainda não pronta", "Preciso de acesso (KRN-9)"):
            self.assertEqual([], porteiro.issues_prontas(texto))
        self.assertEqual(["KRN-9"], porteiro.issues_prontas("Versão nova de KRN-9: relatório"))

    def test_pedido_de_acesso_nao_vira_aviso_de_conclusao(self):
        self.enviar("Preciso do token na VPS para destravar o DNS (KRN-1069).")
        self.assertEqual([], porteiro._ler(str(self.dados))["envios"][-1]["issues_prontas"])
        self.enviar("KRN-9 pronta: relatório")
        self.assertEqual(["KRN-9"], porteiro._ler(str(self.dados))["envios"][-1]["issues_prontas"])

    def test_resumo_separa_pedido_e_entrega_sem_mudar_numero_de_envios(self):
        self.enviar("Preciso de acesso (KRN-1069)", quando=DIA.replace(hour=3))
        self.enviar("KRN-9 pronta: relatório", quando=DIA.replace(hour=3))
        texto = self.descarregar(DIA.replace(hour=8))
        self.assertIn("2 avisos juntados", texto)
        envios = porteiro._ler(str(self.dados))["envios"]
        self.assertEqual(3, len(envios))
        self.assertEqual(["KRN-9"], envios[-1]["issues_prontas"])


class Tecnico(Base):
    def test_tecnico_vai_ao_orquestrador_nao_ao_whatsapp(self):
        for t in ("DataForSEO: teto diário do DataForSEO atingido: US$ 5", "❌ Linear: 2 tarefa(s) com falha",
                  "KRN-907 design-taste: FAIL 3 WARN 2", "Site x.com fora do ar", "deploy do asite01 falhou"):
            self.enviar(t, tipo="tecnico" if "Site" in t else None)
        self.assertEqual([], self.saida)
        self.assertEqual(5, len(self.desviados))

    def test_entrega_com_arquivo_nunca_e_tecnica(self):
        a = self.arq("a.pdf")
        self.enviar("KRN-1 pronta: auditoria de deploy do site", arquivos=[a])
        self.assertEqual(1, len(self.saida))

    def test_tipo_explicito_decide(self):
        self.enviar("Preciso da sua decisão sobre o deploy", tipo="decisao")
        self.assertEqual(1, len(self.saida))

    def test_desvio_nao_conta_no_teto(self):
        for _ in range(20):
            self.enviar("tarefa com falha")
        self.assertEqual((0, 0), porteiro._contar(porteiro._ler(str(self.dados)), DIA))


class Horario(Base):
    def test_madrugada_nada_sai_e_as_8h_vai_um_resumo(self):
        for i, h in enumerate((22, 23, 2, 6)):
            q = DIA.replace(hour=h, day=15 if h >= 22 else 16)
            self.enviar(f"KRN-{i + 10} pronta: coisa {i}", quando=q, arquivos=[self.arq(f"{i}.pdf", bytes([i]))])
        self.assertEqual([], self.saida)
        self.assertIsNone(self.descarregar(DIA.replace(hour=7, minute=30)))  # ainda não são 8h
        texto = self.descarregar(DIA.replace(hour=8))
        self.assertEqual(1, len(self.saida))
        self.assertIn("4 entregas prontas", texto)
        self.assertEqual(4, len(self.saida[0][1]))
        self.assertIsNone(self.descarregar(DIA.replace(hour=8, minute=5)))  # fila vazia: não repete

    def test_inc241_envios_das_21h_brt_aguardam_resumo(self):
        # Envios reais: 20/10 00:02 e 00:15 UTC = 19/10 21:02 e 21:15 BRT.
        for minuto, origem in ((0, "hub"), (2, "destaques-semanal"), (15, "entrega_zap"), (41, "hub")):
            quando = dt.datetime(2026, 10, 20, 0, minuto, tzinfo=dt.timezone.utc)
            self.enviar(f"KRN-{minuto + 100} pronta: documento", quando=quando,
                        arquivos=[self.arq(f"inc241-{minuto}.pdf", bytes([minuto]))],
                        origem=origem, agora=True, pedido=True)
        self.assertEqual([], self.saida)
        est = porteiro._ler(str(self.dados))
        self.assertEqual(4, len(est["fila"]))
        self.assertTrue(all(f["motivo"] == "madrugada" for f in est["fila"]))
        self.assertIsNone(self.descarregar(dt.datetime(2026, 10, 19, 21, 59, tzinfo=BRT)))
        self.assertIn("4 entregas prontas", self.descarregar(dt.datetime(2026, 10, 20, 8, tzinfo=BRT)))
        self.assertEqual(1, len(self.saida))

    def test_7h_ja_sai_direto(self):
        self.enviar("oi", quando=DIA.replace(hour=7))
        self.assertEqual(1, len(self.saida))
        self.enviar("tarde", quando=DIA.replace(hour=22, minute=1))
        self.assertEqual(1, len(self.saida))

    def test_resumo_com_mais_de_4_arquivos_deixa_o_resto_nos_links(self):
        for i in range(8):
            self.enviar(f"KRN-{i + 10} pronta", quando=DIA.replace(hour=3), arquivos=[self.arq(f"i{i}.png", bytes([i]))])
        self.descarregar(DIA.replace(hour=8))
        texto, arquivos = self.saida[0]
        self.assertFalse(any(a.endswith(".zip") for a in arquivos))
        self.assertLessEqual(len(arquivos), 4)
        self.assertEqual(1, sum(a.endswith(".png") for a in arquivos))  # no máximo uma imagem solta
        self.assertIn("8 entregas prontas", texto)
        self.assertIn("KRN-17", texto)

    def test_zip_nunca_vai_ao_dono(self):
        z = self.bot / "x.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("a.pdf", b"1")
        self.enviar("KRN-3 pronta", arquivos=[z, self.arq("b.pdf", b"2")])
        texto, arquivos = self.saida[0]
        self.assertEqual(["envios/b.pdf"], arquivos)
        self.assertIn("zip", texto)

    def test_pdf_antes_de_imagem_no_resumo(self):
        self.enviar("KRN-1 pronta", quando=DIA.replace(hour=3), arquivos=[self.arq("a.png", b"1")])
        self.enviar("KRN-2 pronta", quando=DIA.replace(hour=3), arquivos=[self.arq("b.pdf", b"2")])
        self.descarregar(DIA.replace(hour=8))
        self.assertEqual("envios/b.pdf", self.saida[0][1][0])


class Teto(Base):
    def test_11a_mensagem_da_hora_vai_para_o_resumo(self):
        for i in range(12):
            self.enviar(f"aviso {i}", quando=DIA + dt.timedelta(minutes=i))
        self.assertEqual(10, len(self.saida))
        self.assertIsNone(self.descarregar(DIA + dt.timedelta(minutes=30)))  # ainda sem cota na hora
        texto = self.descarregar(DIA + dt.timedelta(minutes=75))
        self.assertIn("2 avisos juntados", texto)
        self.assertEqual(11, len(self.saida))

    def test_teto_do_dia(self):
        for i in range(45):  # uma por hora e pouco, espalhadas na janela 8h-21h
            self.enviar(f"aviso {i}", quando=DIA.replace(hour=8) + dt.timedelta(minutes=17 * i))
        enviados_dia = len(self.saida)
        self.assertLessEqual(enviados_dia, porteiro.TETO_DIA)
        est = porteiro._ler(str(self.dados))
        self.assertTrue(est["fila"] or enviados_dia == 45)

    def test_fila_do_dia_sai_no_dia_seguinte(self):
        est = porteiro._ler(str(self.dados))
        est["envios"] = [{"em": porteiro._iso(DIA), "origem": "x", "issue": None, "tipo": "enviado"}] * 40
        est["fila"] = [{"em": porteiro._iso(DIA), "origem": "x", "issue": None, "texto": "sobrou", "arquivos": []}]
        porteiro._gravar(est, str(self.dados))
        self.assertIsNone(self.descarregar(DIA + dt.timedelta(hours=2)))
        self.assertIsNotNone(self.descarregar(DIA + dt.timedelta(days=1)))

    def test_outro_numero_nao_conta_nem_passa_pelo_porteiro(self):
        with mock.patch.object(caixa, "CAIXA", str(self.caixa)):
            for _ in range(15):
                caixa.escrever("oi ClienteI", caixa=str(self.caixa), para="5500000000003", base_dados=str(self.dados))
        self.assertEqual(15, len(list(self.caixa.glob("*.json"))))


class Dedupe(Base):
    def test_mesmo_arquivo_por_dois_remetentes_vai_uma_vez(self):
        a = self.arq("decl.pdf", b"mesmo")
        self.enviar("KRN-913 pronta: declaração", arquivos=[a], origem="entrega_zap")
        self.enviar("KRN-913 pronta: declaração", arquivos=[a], origem="conta", quando=DIA + dt.timedelta(hours=1))
        self.assertEqual(1, len(self.saida))
        self.assertEqual({"entrega_zap": 1}, porteiro.relatorio(1, str(self.dados), DIA)["2026-10-16"]["origens"])

    def test_arquivo_ja_mandado_sai_da_mensagem_e_o_novo_vai(self):
        self.enviar("KRN-7 pronta", arquivos=[self.arq("a.pdf", b"1")])
        self.enviar("KRN-7 pronta", arquivos=[self.arq("a.pdf", b"1"), self.arq("b.pdf", b"2")], quando=DIA + dt.timedelta(days=1))
        self.assertEqual(["envios/b.pdf"], self.saida[1][1])


class VersaoFinal(Base):
    def test_anexos_distintos_da_mesma_issue_na_madrugada_ficam_na_fila(self):
        q = DIA.replace(hour=2)
        self.enviar("KRN-20 pronta: pdf", quando=q, arquivos=[self.arq("a.pdf", b"a")])
        self.enviar("KRN-20 pronta: video", quando=q, arquivos=[self.arq("b.mp4", b"b")])
        fila = porteiro._ler(str(self.dados))["fila"]
        self.assertEqual(1, len(fila))
        self.assertEqual({"envios/a.pdf", "envios/b.mp4"}, set(fila[0]["arquivos"]))

    def test_resumo_nao_sai_depois_das_22h(self):
        self.enviar("KRN-21 pronta: x", quando=DIA.replace(hour=23), arquivos=[self.arq("c.pdf", b"c")])
        self.assertIsNone(self.descarregar(DIA.replace(hour=23, minute=5)))
        self.assertEqual([], self.saida)

    def test_versao_nova_no_mesmo_dia_espera_e_substitui(self):
        self.enviar("KRN-8 pronta: PDF", arquivos=[self.arq("p.pdf", b"v1")])
        self.enviar("KRN-8 pronta: PDF", arquivos=[self.arq("p.pdf", b"v2")], quando=DIA + dt.timedelta(hours=1))
        self.enviar("KRN-8 pronta: PDF", arquivos=[self.arq("p.pdf", b"v3")], quando=DIA + dt.timedelta(hours=2))
        self.assertEqual(1, len(self.saida))
        fila = porteiro._ler(str(self.dados))["fila"]
        self.assertEqual(1, len(fila))  # a v3 substituiu a v2
        self.assertTrue(fila[0]["texto"].lower().startswith("versão nova de krn-8"))

    def test_ticks_nao_reenviam_versoes_no_mesmo_dia(self):
        self.enviar("KRN-8 pronta", arquivos=[self.arq("p.pdf", b"v1")])
        for i in range(1, 4):
            q = DIA + dt.timedelta(minutes=5 * i)
            self.enviar("KRN-8 pronta", arquivos=[self.arq("p.pdf", f"v{i+1}".encode())], quando=q)
            self.assertIsNone(self.descarregar(q))
        # Outras issues podem sair sem liberar a versão retida.
        self.enviar("KRN-9 pronta", arquivos=[self.arq("v.mp4", b"video")], quando=DIA.replace(hour=3))
        self.descarregar(DIA + dt.timedelta(hours=1))
        self.assertEqual(2, len(self.saida))
        self.assertNotIn("KRN-8", self.saida[-1][0])
        self.assertEqual(1, len(porteiro._ler(str(self.dados))["fila"]))
        self.descarregar((DIA + dt.timedelta(days=1)).replace(hour=8))
        self.assertEqual(["envios/p.pdf"], self.saida[-1][1])
        self.assertEqual(3, len(self.saida))
        self.assertIsNone(self.descarregar((DIA + dt.timedelta(days=1)).replace(hour=8, minute=5)))

    def test_pedido_do_dono_manda_na_hora(self):
        self.enviar("KRN-8 pronta", arquivos=[self.arq("p.pdf", b"v1")])
        self.enviar("KRN-8 pronta", arquivos=[self.arq("p.pdf", b"v2")], quando=DIA + dt.timedelta(hours=1), pedido=True)
        self.assertEqual(2, len(self.saida))
        self.assertTrue(self.saida[1][0].startswith("Versão nova de KRN-8"))

    def test_versao_do_dia_seguinte_dentro_de_24h_vai_com_o_prefixo(self):
        self.enviar("KRN-8 pronta: PDF", arquivos=[self.arq("p.pdf", b"v1")], quando=DIA.replace(hour=20))
        self.enviar("KRN-8 pronta: PDF", arquivos=[self.arq("p.pdf", b"v2")], quando=DIA.replace(hour=20) + dt.timedelta(hours=12))
        self.assertEqual(2, len(self.saida))
        self.assertTrue(self.saida[1][0].startswith("Versão nova de KRN-8: PDF"))

    def test_issue_diferente_nao_se_mistura(self):
        self.enviar("KRN-8 pronta", arquivos=[self.arq("a.pdf", b"1")])
        self.enviar("KRN-9 pronta", arquivos=[self.arq("b.pdf", b"2")])
        self.assertEqual(2, len(self.saida))


class Caixa(Base):
    def test_caixa_de_producao_passa_pelo_porteiro_e_marca_o_recibo(self):
        with mock.patch.object(caixa, "CAIXA", str(self.caixa)), mock.patch.dict(os.environ, {"TAREFA_NOME": "linear-krn-50"}), \
                mock.patch("hub.debugger.alerta_tecnico") as desvio:
            r = caixa.escrever("tarefa com falha", base_dados=str(self.dados))
        self.assertIsNone(r)
        desvio.assert_called_once()
        self.assertEqual([], list(self.caixa.glob("*.json")))
        # o recibo de pronto não reabre a issue por causa do porteiro
        self.assertIn("linear-krn-50", (self.dados / "claude" / "avisos-enviados.jsonl").read_text())

    def test_entrega_normal_grava_na_caixa(self):
        with mock.patch.object(caixa, "CAIXA", str(self.caixa)), \
                mock.patch.object(porteiro, "madrugada", return_value=False), \
                mock.patch.object(porteiro.datas, "agora", return_value=DIA):
            r = caixa.escrever("KRN-1 pronta", base_dados=str(self.dados))
        self.assertTrue(r.exists())

    def test_descarregar_grava_pela_caixa(self):
        with mock.patch.object(caixa, "CAIXA", str(self.caixa)):
            porteiro.passar("KRN-3 pronta", caixa=self.caixa, base_dados=str(self.dados), quando=DIA.replace(hour=2),
                            gravar=lambda *a: None)
            self.assertIn("1 entrega", caixa.descarregar(self.caixa, str(self.dados), DIA.replace(hour=8)))
        a = json.loads(next(self.caixa.glob("*.json")).read_text())
        self.assertTrue(a["agora"])


class EntregaComPara(Base):
    """17/10: o recibo manda COM `para` (o admin do bot) e o hub não conhecia o número do dono: eh_dono(para) dava falso e o
    aviso saía direto pela caixa, sem madrugada, teto nem dedupe. Aqui o ambiente é o de produção: sem TRIAGEM_ADMIN."""
    ADMIN = "5500000000002"
    HORAS = [(16, 21, 50), (16, 23, 47), (17, 2, 4), (17, 2, 16), (17, 2, 55)]  # BRT, os avisos que furaram

    def setUp(self):
        super().setUp()
        os.environ.pop("TRIAGEM_ADMIN", None)
        for p in (mock.patch.object(caixa, "CAIXA", str(self.caixa)),
                  mock.patch("hub.config.get", side_effect=lambda k, *a: "" if k == "dono.whatsapp" else None)):
            p.start()
            self.addCleanup(p.stop)
        porteiro._numero_do_bot.cache_clear()

    def escrever(self, quando, issue, docker=ADMIN, para=ADMIN):
        with mock.patch.object(porteiro, "_ler_admin_do_bot", return_value=docker), \
                mock.patch.object(porteiro.datas, "agora", return_value=quando):
            return caixa.escrever(f"{issue} pronta: algo", agora=True, para=para, origem="entrega_zap", base_dados=str(self.dados))

    def test_madrugada_com_para_nao_sai_nos_horarios_que_furaram(self):
        for i, (d, h, m) in enumerate(self.HORAS):
            porteiro._numero_do_bot.cache_clear()
            r = self.escrever(dt.datetime(2026, 10, d, h, m, tzinfo=BRT), f"KRN-{1000 + i}")
            if h >= 21 or h < 7:
                self.assertIsNone(r, f"{h}:{m:02d} BRT não podia sair")
            else:  # fora da janela 21h-7h: sai
                self.assertIsNotNone(r)
        self.assertEqual(0, len(list(self.caixa.glob("*.json"))))
        self.assertEqual(5, len(porteiro._ler(str(self.dados))["fila"]))

    def test_sem_saber_o_numero_do_dono_o_para_vale_como_dono(self):
        """Falha fechada: número do dono ilegível não abre a porta (o teto e a madrugada continuam valendo)."""
        r = self.escrever(dt.datetime(2026, 10, 17, 2, 4, tzinfo=BRT), "KRN-1005", docker="")
        self.assertIsNone(r)
        self.assertEqual([], list(self.caixa.glob("*.json")))

    def test_falha_do_docker_nao_fica_no_cache(self):
        """Review do #503: "" de uma falha transitória não pode ficar guardado depois que o Docker volta."""
        quando = dt.datetime(2026, 10, 17, 2, 4, tzinfo=BRT)
        self.assertIsNone(self.escrever(quando, "KRN-1005", docker=""))
        r = self.escrever(quando, "KRN-1006", para="5500000000004", docker=self.ADMIN)
        self.assertIsNotNone(r)  # número lido de novo: terceiro deixa de valer como dono

    def test_outro_numero_conhecido_dono_nao_passa_pelo_porteiro(self):
        r = self.escrever(dt.datetime(2026, 10, 17, 2, 4, tzinfo=BRT), "KRN-1005", para="5500000000004")
        self.assertIsNotNone(r)  # número de terceiro: sem porteiro (como antes)


class Relatorio(Base):
    def test_conta_por_dia_e_origem(self):
        self.enviar("a", origem="entrega_zap")
        self.enviar("b", origem="reels")
        self.enviar("c", origem="reels", quando=DIA + dt.timedelta(minutes=3))
        self.enviar("tarefa com falha")
        r = porteiro.relatorio(7, str(self.dados), DIA)["2026-10-16"]
        self.assertEqual(3, r["total"])
        self.assertEqual({"entrega_zap": 1, "reels": 2}, r["origens"])
        self.assertEqual(1, r["desviados"])
        self.assertIn("reels 2", porteiro.texto_relatorio({"2026-10-16": r}))


if __name__ == "__main__":
    unittest.main()
