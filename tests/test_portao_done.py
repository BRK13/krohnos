"""portão único de Done. Casos reais da auditoria de 16/10: (Done com os PRs #294/#322 abertos) e
(Done com "Falta conferir depois do deploy" no último comentário)."""
import datetime as dt, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from hub import config, portao_done, recibo

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 16, 22, 0, tzinfo=UTC)
ESTADOS = {"nodes": [{"id": "s-ip", "name": "In Progress"}, {"id": "s-done", "name": "Done"}]}


def issue(ident, prs=(), comentarios=(), descricao="Fazer.", titulo="t"):
    return {"id": f"id-{ident}", "identifier": ident, "title": titulo, "description": descricao, "createdAt": "2026-10-10T10:00:00Z",
            "completedAt": "2026-10-16T17:00:00Z", "state": {"name": "Done", "type": "completed"},
            "attachments": {"nodes": [{"url": u} for u in prs]},
            "comments": {"nodes": [{"createdAt": f"2026-10-16T1{i}:00:00Z", "body": b, "user": {"app": app}} for i, (b, app) in enumerate(comentarios)]},
            "team": {"id": "t", "states": ESTADOS}}


def gh_falso(estados):
    """estados: {"repo#n": ("open"|"closed", merged)}"""
    def gh(caminho):
        for chave, (st, merged) in estados.items():
            repo, n = chave.split("#")
            if caminho == f"repos/{repo}/pulls/{n}":
                return {"state": st, "merged": merged, "merge_commit_sha": "a" * 40, "merged_at": "2026-10-16T10:00:00Z", "base": {"ref": "main"}}
        raise AssertionError(caminho)
    return gh


class PrsCitados(unittest.TestCase):
    """o debugger (INC-182) citou o PR #553 na, mas ele é da; o portão barrava em laço."""
    P553 = "https://github.com/acme/repo-privado/pull/553"
    P600 = "https://github.com/acme/repo-privado/pull/600"

    def test_inc280_reconciliacao_separa_pr_do_incidente(self):
        i = issue("KRN-1531", prs=[self.P553, self.P600], comentarios=[
            (f"Debugger · tentativa 1/3 INC-276: {self.P553}", True),
            ("O acme/repo-privado#553 pertence ao INC-276; não implementa o artigo "
             "e não é pendência desta entrega, conforme reconciliação do orquestrador.", False)])
        self.assertEqual([("acme/repo-privado", 600)], portao_done.prs_citados(i))
        i["comments"]["nodes"][-1]["user"]["app"] = True
        self.assertEqual(2, len(portao_done.prs_citados(i)))

    def test_inc280_so_dispensa_pr_citado_antes_da_frase(self):
        """Revisão de #812: 'não implementa o artigo' reprova a entrega, e o PR citado depois da frase continua cobrado."""
        i = issue("KRN-1531", prs=[self.P600, self.P553], comentarios=[
            ("O acme/repo-privado#553 pertence ao INC-276 e não implementa o artigo, refaça.", False)])
        self.assertEqual([("acme/repo-privado", 600), ("acme/repo-privado", 553)], portao_done.prs_citados(i))
        i = issue("KRN-1531", prs=[self.P600], comentarios=[
            ("INC-276 não é pendência desta entrega, ver acme/repo-privado#600.", False)])
        self.assertEqual([("acme/repo-privado", 600)], portao_done.prs_citados(i))

    def test_inc280_pr_de_outra_frase_do_paragrafo_continua_cobrado(self):
        """Revisão de #812: 'Falta #600. O #553 ... não é pendência' dispensa só o #553."""
        i = issue("KRN-1531", prs=[self.P600, self.P553], comentarios=[
            ("Falta acme/repo-privado#600. O acme/repo-privado#553 é do INC-276 e não é pendência desta entrega.", False)])
        self.assertEqual([("acme/repo-privado", 600)], portao_done.prs_citados(i))

    def test_caso_bru_1129(self):
        i = issue("KRN-1129", prs=[self.P600], comentarios=[
            (f"Debugger · tentativa 1/3\n\nIssue dona: KRN-1087. PR {self.P553}", True),
            (f"Incidente INC-182 sobre KRN-1087: {self.P553}", True),
            (f"Done barrado pelo portão (KRN-979): PR {self.P553} aberto. A issue volta para In Progress.", True),
            (f"Voltou de Done (portão, KRN-979): PR {self.P553}", True)])
        self.assertEqual([("acme/repo-privado", 600)], portao_done.prs_citados(i))

    def test_entrega_citada_pela_propria_issue_conta(self):
        i = issue("KRN-1129", comentarios=[(f"Entrega: {self.P600}", True), (f"Debugger · tentativa 1/3\n\nIssue dona: KRN-1129. {self.P553}", True)])
        self.assertEqual([("acme/repo-privado", 600), ("acme/repo-privado", 553)], portao_done.prs_citados(i))

    def test_issue_dona_em_minuscula_conta(self):
        i = issue("KRN-1129", comentarios=[(f"Debugger\nIssue dona: krn-1129 {self.P553}", True)])
        self.assertEqual([("acme/repo-privado", 553)], portao_done.prs_citados(i))

    def test_inc278_relatorio_do_debugger_nao_vira_pr_do_artigo(self):
        i = issue("KRN-1531", comentarios=[
            (f"Artigo entregue: {self.P600}", True),
            (f"Debugger · tentativa 1/3\n\nPR {self.P553} enviado. "
             "INC-276 permanece pendente até merge e atualizar-hub rc=0.", True)])
        self.assertEqual([("acme/repo-privado", 600)], portao_done.prs_citados(i))
        # Um vínculo material ou uma entrega comum continua exigindo merge.
        i["attachments"]["nodes"].append({"url": self.P553})
        self.assertIn(("acme/repo-privado", 553), portao_done.prs_citados(i))


def barrado(frase):
    return f'Done barrado pelo portão (KRN-979): o último comentário deixa pendência aberta: "{frase}"'


class Texto(unittest.TestCase):
    def test_inc264_indexacao_exige_destino_e_recibo_novo(self):
        antigo = (
            "INAS no ar e conferido em produção.\n\n"
            "- **Indexação:** a URL ainda não foi indexada. Ela está no sitemap; "
            "vale conferir no GSC em alguns dias.\n"
            "- **Frente C (fora desta issue):** tirar INAS de OUTROS_CONVENIOS; "
            "sem issue própria, o orquestrador precisa criá-la."
        )
        i = issue("KRN-1481", comentarios=[(antigo, True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])
        novo = (
            "Produção conferida: redirect 301 e página 200.\n\n"
            "- Indexação: a URL ainda não foi indexada; conferir a partir de 24/10 "
            "na KRN-1489.\n\n"
            "- CRM: tirar INAS de OUTROS_CONVENIOS e corrigir os testes na KRN-1488."
        )
        # A recusa antiga e o marcador da sessão não substituem o recibo corrigido.
        i = issue("KRN-1481", comentarios=[
            (antigo, True), (barrado("ainda não foi indexada."), True),
            (novo, True), ("This thread is for an agent session with claude.", False),
        ])
        faltas, delegadas = portao_done.faltas_do_texto(i)
        self.assertEqual([], faltas)
        self.assertEqual(1, len(delegadas))
        self.assertIn("KRN-1489", delegadas[0])
        self.assertFalse(delegadas[0].dono)
        self.assertTrue(portao_done.faltas_do_texto(
            i, comentario=novo.replace("KRN-1489", "KRN-1481"))[0])

    def test_falta_no_fim_e_pendencia(self):
        ab, de = portao_done.pendencias("Merge feito.\n\nFalta conferir depois do deploy:\n- curl nas páginas", "KRN-954")
        self.assertEqual(1, len(ab))
        self.assertEqual([], de)

    def test_expressoes_da_auditoria(self):
        for t in ("PR no CI; volto quando sair", "Volto quando o merge subir", "Ainda não fiz o merge", "o merge ainda não saiu",
                  "Aguardando o deploy", "- **Falta**: aviso", "**Falta**: aviso"):
            self.assertTrue(portao_done.pendencias(t, "KRN-1")[0], t)

    def test_sem_pendencia(self):
        for t in ("Tudo no ar. Conferi com curl.", "Sem falta de dados no relatório.", "auditoria: ok"):
            self.assertEqual(([], []), portao_done.pendencias(t, "KRN-1"), t)

    def test_inc239_entrega_ausente_nao_vira_done_por_citar_outra_issue(self):
        textos = (
            "A lista de decisões recentes não foi entregue. Abri a KRN-1329 no Backlog.",
            "A lista de decisões não foi entregue; segue na KRN-1329.",
            "**Ainda não tem a lista de decisões:** depende do reprocesso KRN-1329.",
            "**Sem prova ainda:** `local-ci`, `seomator audit` e curl 200.",
        )
        for texto in textos:
            with self.subTest(texto=texto):
                i = issue("KRN-1326", comentarios=[(texto, True)])
                self.assertTrue(portao_done.faltas_do_texto(i)[0])
                self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto)[0])

    def test_inc122_merge_do_hub_nao_esconde_pendencia_anterior(self):
        # INC-122: o merge mecânico veio depois da pendência de migração/carga; o Done não pode passar por ele
        pendencia = "Falta em produção: a migração e a carga das URLs ainda não rodaram."
        merge = "Merge feito pelo hub (CI verde, revisão sem achados); volto a conferir o deploy sem IA."
        i = issue("KRN-1009", comentarios=[(pendencia, True), (merge, True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])
        self.assertEqual(portao_done.faltas_do_texto(i)[0], portao_done.faltas_do_texto(issue("KRN-1009", comentarios=[(pendencia, True)]))[0])
        self.assertEqual(([], []), portao_done.faltas_do_texto(issue("KRN-1009", comentarios=[(merge, True)])))

    def test_inc239_recibo_completo_nao_e_barrado(self):
        texto = ("Lista de decisões entregue. Prova: curl 200, sitemap e seomator audit anexados. "
                 "Não foi necessário criar outra página.")
        self.assertEqual(([], []), portao_done.pendencias(texto, "KRN-1326"))

    def test_pendencia_com_destino_nao_barra(self):
        self.assertEqual(([], ["Falta o token da conta: vai para a KRN-1000."]), portao_done.pendencias("Falta o token da conta: vai para a KRN-1000.", "KRN-1"))
        ab, de = portao_done.pendencias("Falta a chave do Stripe, depende de você.", "KRN-1")
        self.assertEqual((0, 1), (len(ab), len(de)))

    def test_bru_em_outro_paragrafo_nao_e_destino(self):
        self.assertTrue(portao_done.pendencias("Falta executar a migração.\n\nReferência: KRN-2.", "KRN-1")[0])

    def test_nao_falta_nada_nao_e_pendencia(self):
        self.assertEqual(([], []), portao_done.pendencias("Não falta nada: tudo no ar.", "KRN-1"))

    def test_inc205_falta_nada_com_markdown(self):
        for texto in ("**Falta:** nada.", "**Falta**: nada.", "Falta: nada."):
            self.assertEqual(([], []), portao_done.pendencias(texto), texto)
        self.assertTrue(portao_done.pendencias("**Falta:** nada. Falta conferir o deploy.")[0])

    def test_inc205_citacao_historica_e_portao(self):
        texto = ('A frase "aguardando runner, merge e `atualizar-hub rc=0`" vinha de um comentário antigo '
                 'e depois foi citada de novo no comentário de bloqueio do próprio portão.\n\n**Falta:** nada.')
        i = issue("KRN-1199", comentarios=[(barrado("aguardando runner, merge e `atualizar-hub rc=0`"), True), (texto, True),
                  ('Done barrado pelo portão (KRN-979): aguardando runner; Falta: nada.', True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))
        self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + "\nAguardando deploy.")[0])
        self.assertTrue(portao_done.pendencias('Situação: "aguardando runner".')[0])

    def test_inc221_citacao_do_comentario_antigo(self):
        texto = ('Conserto mergeado; local-ci success; versão no ar conferida.\n\n'
                 'O mover recusou antes com o comentário antigo "no CI; volto quando sair.". '
                 'Não sei o que mudou para ele aceitar agora, e não mexi no hub para isso.')
        i = issue("KRN-1232", comentarios=[(barrado("no CI; volto quando sair."), True), (texto, True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))
        self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
        self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + "\nAguardando deploy.")[0])
        i["comments"]["nodes"] = i["comments"]["nodes"][1:]
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_inc223_citacao_historica_com_virgula(self):
        antigo = "PR está no CI; volto quando sair."
        for separador in (", ", ": ", " "):
            texto = ('Portão de Done: barrou por causa de um comentário antigo'
                     + separador + '"no CI; volto quando sair". Só passou com --comentario.')
            with self.subTest(separador=separador):
                i = issue("KRN-1240", comentarios=[(barrado(antigo), True), (texto, True)])
                self.assertEqual(([], []), portao_done.faltas_do_texto(i))
                self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
                self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + " Aguardando deploy.")[0])
                inventada = issue("KRN-1240", comentarios=[("Entrega parcial.", True), (texto, True)])
                self.assertTrue(portao_done.faltas_do_texto(inventada)[0])

    def test_inc240_citacao_nos_comentarios_anteriores(self):
        antigo = "Proposta enviada, aguardando o orquestrador aprovar."
        texto = ('Entregue e conferido. O mover lia como pendência a frase '
                 '"aguardando o orquestrador aprovar", que aparecia nos meus comentários anteriores. '
                 'Passei o comentário final pelo parâmetro `--comentario` do próprio `mover`.')
        i = issue("KRN-1284", comentarios=[(barrado(antigo), True), (texto, True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))
        self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
        self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + " Aguardando deploy.")[0])
        inventada = issue("KRN-1284", comentarios=[("Entrega parcial.", True), (texto, True)])
        self.assertTrue(portao_done.faltas_do_texto(inventada)[0])
        self.assertTrue(portao_done.pendencias('Situação: "aguardando o orquestrador aprovar".',
                                              historico=[antigo])[0])

    def test_bru1350_qualquer_redacao_em_volta_da_citacao_do_portao(self):
        # Histórico real da o portão barrou 8 vezes com a mesma frase; as 3 redações do agente passam.
        frase = "aguardando o orquestrador aprovar"
        textos = (
            f'Entregue e conferido por curl. O mover lia "{frase}" como o último comentário, mas já foi aprovado.',
            f'Entregue e conferido. A frase "{frase}", que aparecia nos meus comentários anteriores, já não vale.',
            f'O Done foi barrado por "{frase}"; passei o recibo pelo `--comentario` do `mover`.',
        )
        for texto in textos:
            with self.subTest(texto=texto):
                i = issue("KRN-1284", comentarios=[("Proposta enviada.", True), (barrado(frase), True), (texto, True)])
                self.assertEqual(([], []), portao_done.faltas_do_texto(i))
                self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
                # controle: pendência em prosa, sem aspas, continua barrando
                self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + "\nFalta o deploy.")[0])
                # controle: sem comentário do portão no histórico, a citação não se sustenta
                sem = issue("KRN-1284", comentarios=[("Proposta enviada.", True), (texto, True)])
                self.assertTrue(portao_done.faltas_do_texto(sem)[0])
        # controle: aspas com frase que o portão nunca citou continuam barrando
        i = issue("KRN-1284", comentarios=[(barrado(frase), True)])
        self.assertTrue(portao_done.pendencias('Situação: "aguardando o deploy".', historico=[barrado(frase)])[0])
        self.assertTrue(portao_done.pendencias('Situação: "aguardando o deploy".', historico=["Aguardando o deploy."])[0])
        self.assertTrue(portao_done.pendencias("Falta o deploy.", historico=[barrado("Falta o deploy.")])[0])

    def test_inc261_citacao_abreviada_do_portao(self):
        antigo = barrado("Falta só seomator e o recibo para Done.")
        for fim in ("…", "..."):
            texto = ('Recibo completo: página 200, seomator 95 registrado. '
                     f'O portão lia o repasse ("Falta só seomator{fim}"), e os meus '
                     'recibos não o substituíram. Passei o recibo no próprio '
                     '`mover --comentario`, e aí ele aceitou.')
            with self.subTest(fim=fim):
                i = issue("KRN-1457", comentarios=[(antigo, True), (texto, True)])
                self.assertEqual(([], []), portao_done.faltas_do_texto(i))
                self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
                self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + " Falta o deploy.")[0])
                self.assertTrue(portao_done.pendencias(texto)[0])
                self.assertTrue(portao_done.pendencias(texto, historico=[barrado("Falta o PDF.")])[0])
                curto = f'Recibo completo. O portão citou "Falta o{fim}" antes.'
                self.assertTrue(portao_done.pendencias(curto, historico=[antigo])[0])

    def test_inc231_citacao_da_pendencia_ja_barrada(self):
        antigo = "Merge feito; volto a conferir o deploy sem IA."
        textos = (
            'O primeiro Done foi barrado pelo portão, que ainda lia a pendência '
            '"volto a conferir o deploy". Gravei o recibo junto com o movimento '
            '(`mover --comentario`) e o portão aceitou.',
            'O recibo não tem "volto a conferir", que foi a frase que o portão barrou nas outras vezes.',
        )
        for texto in textos:
            with self.subTest(texto=texto):
                i = issue("KRN-1209", comentarios=[(barrado(antigo), True), (texto, True)])
                self.assertEqual(([], []), portao_done.faltas_do_texto(i))
                self.assertEqual(([], []), portao_done.faltas_do_texto(i, comentario=texto))
                self.assertTrue(portao_done.faltas_do_texto(i, comentario=texto + " Aguardando deploy.")[0])
                inventada = issue("KRN-1209", comentarios=[("Entrega parcial.", True), (texto, True)])
                self.assertTrue(portao_done.faltas_do_texto(inventada)[0])
        self.assertTrue(portao_done.pendencias('A pendência "volto a conferir o deploy" continua aberta.',
                                              historico=[antigo])[0])

    def test_inc205_citacao_inventada_nao_esconde_pendencia(self):
        texto = '"Falta o deploy" vinha de um comentário antigo.'
        i = issue("KRN-1199", comentarios=[("Entrega parcial.", True), (texto, True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_inc205_portao_nao_esconde_pendencia_real(self):
        i = issue("KRN-1199", comentarios=[("Aguardando runner", True),
                  ("Done barrado pelo portão (KRN-979): aguardando runner", True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_o_proprio_bru_nao_e_destino(self):
        self.assertTrue(portao_done.pendencias("Falta conferir; segue na KRN-1.", "KRN-1")[0])

    def test_inc209_thread_sem_usuario_nao_esconde_entrega(self):
        i = issue("KRN-905", comentarios=[
            ("Falta a primeira noite de cron e a tabela do item 4.", True)])
        i["comments"]["nodes"].append({
            "createdAt": "2026-10-18T20:49:16Z",
            "body": "This thread is for an agent session with claude.", "user": None})
        self.assertTrue(portao_done.faltas_do_texto(i)[0])
        # O marcador sozinho também não deve ser atribuído ao dono.
        i["comments"]["nodes"] = i["comments"]["nodes"][-1:]
        self.assertEqual(("", False), portao_done.ultimo_comentario(i))

    def test_ultimo_comentario_humano_nao_cobra(self):
        i = issue("KRN-1", comentarios=[("Falta o merge", True), ("fechei assim mesmo", False)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))


class EsperaDeCiFundida(unittest.TestCase):
    """"no CI; volto quando sair" não pode barrar o Done depois que o PR citado foi fundido (caso)."""
    TEXTO = "PR https://github.com/jpexemplo/clientee-moraes-site/pull/162 no CI; volto quando sair."

    def faltas(self, estado, texto=None):
        i = issue("KRN-1662", comentarios=[(texto or self.TEXTO, True)])
        gh = gh_falso({"jpexemplo/clientee-moraes-site#162": estado})
        r = recibo.conferir(i, gh, lambda u: {"commit": "a" * 40}, tempfile.mkdtemp(), AGORA)
        return [f for f in r["faltas"] if "pendência aberta" in f]

    def test_pr_fundido_libera_a_frase_de_espera(self):
        self.assertEqual([], self.faltas(("closed", True)))

    def test_pr_aberto_continua_barrando(self):
        self.assertEqual(1, len(self.faltas(("open", False))))

    def test_falta_de_verdade_junto_da_espera_continua(self):
        texto = self.TEXTO + "\n\nFalta rodar a migração."
        f = self.faltas(("closed", True), texto)
        self.assertEqual(1, len(f))
        self.assertIn("migração", f[0])


class BarradoRepetido(unittest.TestCase):
    """a sessão e o fim da execução chamam o portão em par; o texto idêntico sai uma vez."""
    def test_dois_barrar_seguidos_um_comentario(self):
        comentarios = []
        i = issue("KRN-7")
        def comentar(a, t):
            comentarios.append(t)
            i["comments"]["nodes"].append({"createdAt": "2026-10-16T22:00:00Z", "body": t, "user": {"app": True}})
        r = {"ok": False, "faltas": ["o PR x#1 ainda está aberto"]}
        for minuto in (0, 1):
            with patch("hub.conta.agora", return_value=AGORA + dt.timedelta(minutes=minuto)):
                portao_done.barrar("KRN-7", consulta=lambda q, **v: {"issue": i}, mover_fn=lambda a, b: None, comentar_fn=comentar,
                                   avaliar_fn=lambda *a, **k: r)
        self.assertEqual(1, len(comentarios))

    def test_comentario_do_portao_com_token_do_dono_tambem_conta(self):
        """(22/10 04:44-04:46): o barrado saiu com autor humano e o filtro por app deixou repetir."""
        i = issue("KRN-7")
        r = {"ok": False, "faltas": ["f"]}
        texto = "Done barrado pelo portão (KRN-979): f. A issue volta para In Progress.\n\n_Krohnos._"
        i["comments"]["nodes"].append({"createdAt": "2026-10-16T22:00:00Z", "body": texto, "user": {"app": False, "name": "Fulano Pereira"}})
        comentarios = []
        with patch("hub.conta.agora", return_value=AGORA):
            portao_done.barrar("KRN-7", consulta=lambda q, **v: {"issue": i}, mover_fn=lambda a, b: None,
                               comentar_fn=lambda a, t: comentarios.append(t), avaliar_fn=lambda *a, **k: r)
        self.assertEqual([], comentarios)

    def test_passada_uma_hora_comenta_de_novo(self):
        comentarios = []
        i = issue("KRN-7")
        def comentar(a, t):
            comentarios.append(t)
            i["comments"]["nodes"].append({"createdAt": "2026-10-16T22:00:00Z", "body": t, "user": {"app": True}})
        r = {"ok": False, "faltas": ["f"]}
        for minuto in (0, 61):
            with patch("hub.conta.agora", return_value=AGORA + dt.timedelta(minutes=minuto)):
                portao_done.barrar("KRN-7", consulta=lambda q, **v: {"issue": i}, mover_fn=lambda a, b: None, comentar_fn=comentar,
                                   avaliar_fn=lambda *a, **k: r)
        self.assertEqual(2, len(comentarios))


class Portao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def avaliar(self, i, gh, comentario=None):
        return recibo.conferir(i, gh, lambda u: {"commit": "a" * 40}, self.dados, AGORA, comentario=comentario)

    def test_inc265_ses_entrega_parcial_e_pr_aberto_nao_encerram(self):
        repo = "example-org/infra"
        texto = ("As chaves não foram entregues: só a do ClienteJ está gravada. "
                 "A KRN-1426 segue **In Progress**.\n\n"
                 "PR https://github.com/example-org/infra/pull/31 no CI; volto quando sair.")
        for proposto in (False, True):
            with self.subTest(proposto=proposto):
                i = issue("KRN-1426", comentarios=[(texto, True)])
                r = self.avaliar(i, gh_falso({f"{repo}#31": ("open", False)}),
                                 comentario=texto if proposto else None)
                self.assertFalse(r["ok"])
                self.assertTrue(any("example-org/infra#31 ainda está aberto" in f for f in r["faltas"]))
                self.assertTrue(any("não foram entregues" in f for f in r["faltas"]))
                self.assertTrue(any("no CI; volto" in f for f in r["faltas"]))
                self.assertEqual([], r["delegadas"])
        # A dona já reaberta não deve sofrer nova avaliação/mutação pelo webhook.
        i["state"] = {"name": "In Progress", "type": "started"}
        with patch.object(portao_done, "avaliar") as avaliar:
            self.assertEqual([], portao_done.conferir_uma(
                "KRN-1426", desfazer=True, consulta=lambda *a, **k: {"issue": i}))
            avaliar.assert_not_called()

    def test_bru887_com_prs_abertos(self):
        i = issue("KRN-887", prs=["https://github.com/acme/nimbus-hub/pull/294", "https://github.com/acme/nimbus-hub/pull/322"],
                  comentarios=[("PRs abertos, aguardo a revisão.", True)])
        r = self.avaliar(i, gh_falso({"acme/nimbus-hub#294": ("open", False), "acme/nimbus-hub#322": ("open", False)}))
        self.assertFalse(r["ok"])
        self.assertEqual(2, sum("ainda está aberto" in f for f in r["faltas"]))

    def test_inc147_emissor_exige_recibo_dos_dois_prs_e_conferencia(self):
        repo = "acme/Emissor_nf_exemplo"
        texto = ("volto quando sair.\n\n"
                 "Falta o merge do #4 e a conferência do `/api/versao` do emissor depois do deploy.")
        for proposto in (False, True):
            for estado4 in (("open", False), ("closed", True)):
                with self.subTest(proposto=proposto, estado4=estado4):
                    i = issue("KRN-921", prs=[f"https://github.com/{repo}/pull/{n}" for n in (4, 3)],
                              comentarios=[("Conferido." if proposto else texto, True)])
                    with patch.object(recibo, "no_ar", return_value=(True, "no ar")):
                        r = self.avaliar(i, gh_falso({f"{repo}#4": estado4,
                                                     f"{repo}#3": ("open", False)}),
                                         comentario=texto if proposto else None)
                    self.assertFalse(r["ok"])
                    self.assertEqual([], r["delegadas"])
                    self.assertEqual(2, sum("pendência aberta" in f for f in r["faltas"]))
                    self.assertTrue(any(f"{repo}#3 ainda está aberto" in f for f in r["faltas"]))
                    if estado4[0] == "open":
                        self.assertTrue(any(f"{repo}#4 ainda está aberto" in f for f in r["faltas"]))

    def test_inc171_bru1114_merge_nao_substitui_medicoes(self):
        texto = ('Volto quando saírem. O ajuste não foi medido no `seomator audit`: '
                 'a razão texto/HTML só se confirma depois do deploy.\n\n'
                 '**Falta:** 1. Merge do #309 (a label `merge` está liberada). '
                 '2. Reroda do `seomator audit` depois do deploy. '
                 '3. Medição no GSC da consulta "tabela emolumentos not".')
        for merged in (False, True):
            for proposto in (False, True):
                with self.subTest(merged=merged, proposto=proposto):
                    i = issue("KRN-1114", prs=["https://github.com/acme/cliente-c/pull/309"],
                              comentarios=[("Conferido." if proposto else texto, True)])
                    with patch.object(recibo, "no_ar", return_value=(True, "no ar")):
                        r = self.avaliar(i, gh_falso({"acme/cliente-c#309":
                                                     ("closed" if merged else "open", merged)}),
                                         comentario=texto if proposto else None)
                    self.assertFalse(r["ok"])
                    self.assertEqual([], r["delegadas"])
                    self.assertEqual(2, sum("pendência aberta" in f for f in r["faltas"]))
                    self.assertEqual(not merged, any("ainda está aberto" in f for f in r["faltas"]))

    def test_bru954_falta_no_fim(self):
        i = issue("KRN-954", comentarios=[("Merge feito, mas ainda não está no ar.\n\nFalta conferir depois do deploy:\n- curl", True)])
        r = self.avaliar(i, gh_falso({}))
        self.assertFalse(r["ok"])
        self.assertIn("pendência aberta", r["faltas"][0])

    def test_inc187_proxima_rodada_sem_issue_nao_encerra_apos_merge(self):
        texto = ("**KRN-1139: encerrada. O PR #311 está mergeado na develop.**\n\n"
                 "**Falta, para a próxima rodada do gerador:**\n"
                 "- `scripts/referencias/cnj_atos.py` sobrescreve os arquivos de saída quando só parte "
                 "das coletas falha. Hoje ele pode apagar referências já coletadas do mesmo tema.\n"
                 "- O teste de HTTP confia no booleano `url_http_200` gravado no JSON.")
        pr = "https://github.com/acme/cliente-c/pull/311"
        gh = gh_falso({"acme/cliente-c#311": ("closed", True)})
        for proposto in (False, True):
            with self.subTest(proposto=proposto):
                i = issue("KRN-1139", prs=[pr], comentarios=[("Conferido." if proposto else texto, True)])
                with patch.object(recibo, "no_ar", return_value=(True, "no ar")):
                    r = self.avaliar(i, gh, comentario=texto if proposto else None)
                self.assertFalse(r["ok"])
                self.assertFalse(r["esperar"])
                self.assertEqual([], r["delegadas"])
                self.assertTrue(any("pendência aberta" in f for f in r["faltas"]))

    def test_inc188_amostras_sem_auditoria_nao_encerram_sem_deploy(self):
        texto = ("195 referências conferidas no Europe PMC, com 0 falhas. "
                 "As 3 amostras estão postadas na issue.\n\n"
                 "**Falta:** o orquestrador auditar as 3 amostras e a triagem. "
                 "Não há deploy a conferir.")
        pr = "https://github.com/acme/cliente-a-v2/pull/257"
        for proposto in (False, True):
            with self.subTest(proposto=proposto):
                i = issue("KRN-1140", prs=[pr], comentarios=[("Conferido." if proposto else texto, True)])
                with patch.object(recibo, "no_ar", return_value=(True, "sem deploy a conferir")):
                    r = self.avaliar(i, gh_falso({"acme/cliente-a-v2#257": ("closed", True)}),
                                     comentario=texto if proposto else None)
                self.assertFalse(r["ok"])
                self.assertFalse(r["esperar"])
                self.assertEqual([], r["delegadas"])
                self.assertTrue(any("orquestrador auditar as 3 amostras e a triagem" in f
                                    for f in r["faltas"]))

    def test_inc196_memoria_sem_commit_e_fluxo_sem_prova_barram_done(self):
        textos = (
            "Acrescentei uma nota na memória, mas ela ainda não está commitada no repositório da memória.",
            "O trecho final do subir não rodou de ponta a ponta numa execução só. Só testei a renovação à mão.",
            "O script não foi validado de ponta a ponta depois da correção.",
        )
        for texto in textos:
            for proposto in (False, True):
                with self.subTest(texto=texto, proposto=proposto):
                    i = issue("KRN-1195", comentarios=[("Conferido." if proposto else texto, True)])
                    r = self.avaliar(i, gh_falso({}), comentario=texto if proposto else None)
                    self.assertFalse(r["ok"])
                    self.assertFalse(r["esperar"])
                    self.assertEqual([], r["delegadas"])
                    self.assertTrue(any("pendência aberta" in f for f in r["faltas"]))

    def test_inc196_fluxo_conferido_ou_com_responsavel(self):
        for texto in (
            "Memória commitada. Script executado de ponta a ponta, código 0.",
            "O script não rodou de ponta a ponta: execução assumida na KRN-1200.",
        ):
            with self.subTest(texto=texto):
                self.assertTrue(self.avaliar(issue("KRN-1195", comentarios=[(texto, True)]), gh_falso({}))["ok"])

    def test_inc189_sem_comando_para_done_nao_e_recibo(self):
        texto = ("Página publicada e PR mergeado.\n\n"
                 "Falta só mover a issue para Done, e não tenho comando para isso nesta sessão.")
        pr = "https://github.com/acme/cliente-a-v2/pull/259"
        for proposto in (False, True):
            with self.subTest(proposto=proposto):
                i = issue("KRN-1174", prs=[pr], comentarios=[("Conferido." if proposto else texto, True)])
                with patch.object(recibo, "no_ar", return_value=(True, "no ar")):
                    r = self.avaliar(i, gh_falso({"acme/cliente-a-v2#259": ("closed", True)}),
                                     comentario=texto if proposto else None)
                self.assertFalse(r["ok"])
                self.assertFalse(r["esperar"])
                self.assertEqual([], r["delegadas"])
                self.assertTrue(any("Falta só mover a issue para Done" in f for f in r["faltas"]))

    def test_inc144_primeira_execucao_futura_nao_e_recibo(self):
        textos = (
            "A medida está gravando no BI.\n\n**Falta**\n"
            "- A primeira execução automática da rotina, segunda 19/10, às 7h. "
            "É a única prova que ainda não existe, e depende do calendário.",
            "A rotina está registrada e válida.\n\n"
            "- **Falta:** só a primeira execução automática de 7h, que depende do calendário. "
            "A conferência fica na segunda: contagem dos editais triados e se aparece algum fsite06.",
        )
        for texto in textos:
            for proposto in (False, True):
                with self.subTest(texto=texto, proposto=proposto):
                    i = issue("KRN-1020", comentarios=[("Conferido." if proposto else texto, True)])
                    r = self.avaliar(i, gh_falso({}), comentario=texto if proposto else None)
                    self.assertFalse(r["ok"])
                    self.assertFalse(r["esperar"])
                    self.assertEqual([], r["delegadas"])
                    self.assertTrue(any("pendência aberta" in f for f in r["faltas"]))

    def test_pr_so_fechado_exige_motivo(self):
        gh = gh_falso({"acme/x#5": ("closed", False)})
        sem = issue("KRN-2", prs=["https://github.com/acme/x/pull/5"], comentarios=[("Pronto.", True)])
        com = issue("KRN-2", prs=["https://github.com/acme/x/pull/5"], comentarios=[("PR #5 fechado porque a KRN-9 já entregou.", True)])
        self.assertIn("fechado sem merge", self.avaliar(sem, gh)["faltas"][0])
        self.assertTrue(self.avaliar(com, gh)["ok"])

    def test_inc78_recibo_de_substituicao_na_issue_com_varios_prs(self):
        antigo = "https://github.com/exemplo/hub/pull/282"
        novo = "https://github.com/exemplo/hub/pull/290"
        i = issue("KRN-848", prs=[antigo, novo])
        gh = gh_falso({"exemplo/hub#282": ("closed", False),
                       "exemplo/hub#290": ("closed", True)})
        self.assertIn("fechado sem merge", self.avaliar(i, gh)["faltas"][0])
        i["comments"]["nodes"].append({
            "createdAt": "2026-10-17T08:48:21Z", "user": {"app": True},
            "body": (f"Recibo do Debugger INC-78: o PR {antigo} foi fechado porque "
                     f"foi substituído pelo PR {novo}, que preservou a implementação "
                     "e corrigiu os dois achados da revisão. #290 mergeado.")})
        self.assertTrue(self.avaliar(i, gh)["ok"])
        self.assertFalse(portao_done.fechado_com_motivo(i, "exemplo/hub", 283))

    def test_dois_prs_fechados_um_motivo_so(self):
        gh = gh_falso({"acme/x#5": ("closed", False), "acme/x#6": ("closed", False)})
        i = issue("KRN-2", prs=["https://github.com/acme/x/pull/5", "https://github.com/acme/x/pull/6"],
                  comentarios=[("PR #5 fechado porque a KRN-9 já entregou.", True)])
        r = self.avaliar(i, gh)
        self.assertEqual(1, len(r["faltas"]))
        self.assertIn("#6", r["faltas"][0])

    def test_inc124_pr_consolidado_exige_merge_e_publicacao(self):
        repo = "acme/repo-privado"
        antigo = f"https://github.com/{repo}/pull/443"
        novo = f"https://github.com/{repo}/pull/533"
        i = issue("KRN-1050", prs=[antigo], comentarios=[(
            f"INC-124: PR {antigo} fechado porque foi consolidado no PR {novo}.", True)])
        # A justificativa do PR antigo não é recibo de entrega do substituto.
        with patch("hub.sites.ativos", return_value=[]), \
                patch.object(recibo, "no_ar") as no_ar, \
                patch.object(recibo, "deploy_pendente", return_value=("nenhum", "")):
            gh = gh_falso({f"{repo}#443": ("closed", False),
                           f"{repo}#533": ("open", False)})
            r = self.avaliar(i, gh)
            self.assertEqual([f"o PR {repo}#533 ainda está aberto (não mergeado)"], r["faltas"])
            self.assertFalse(r["ok"])
            no_ar.assert_not_called()

            gh = gh_falso({f"{repo}#443": ("closed", False),
                           f"{repo}#533": ("closed", True)})
            no_ar.return_value = (False, "merge ainda não publicado")
            r = self.avaliar(i, gh)
            self.assertFalse(r["ok"])
            self.assertIn("merge ainda não publicado", r["faltas"])

            no_ar.return_value = (True, "no ar")
            self.assertTrue(self.avaliar(i, gh)["ok"])

    def test_motivo_no_comentario_do_done_vale(self):
        gh = gh_falso({"acme/x#5": ("closed", False), "acme/x#6": ("closed", False)})
        i = issue("KRN-2", prs=["https://github.com/acme/x/pull/5", "https://github.com/acme/x/pull/6"])
        r = self.avaliar(i, gh, comentario="PRs #5 e #6 fechado porque a KRN-9 já entregou. Tudo no ar.")
        self.assertTrue(r["ok"], r)

    def test_duas_pendencias_do_dono_e_destino_depois_dos_160(self):
        longo = "Falta " + "x" * 200 + " depende de você. Falta outra coisa que depende de você."
        i = issue("KRN-5", comentarios=[(longo, True)])
        r = self.avaliar(i, gh_falso({}))
        self.assertTrue(r["ok"], r)
        portao_done._delegar(i, r, self.dados)
        self.assertEqual(2, portao_done.pendencias_fulano(self.dados)["KRN-5"]["pendencia"].count("Falta"))

    def test_inc150_dados_exclusivos_do_dono_entram_na_lista(self):
        texto = (f"Faltam dados que só o {config.dono()} tem. Não inventei nenhum:\n"
                 "- **Meta e Google Ads:** não há feed automático. Os resultados entram por CSV "
                 "em `$HUB_DATA/anuncios/entrada/`. Recomendo manter assim por enquanto.\n"
                 "- **`anuncios.cpa_alvo`:** está vazio por marca.")
        for proposto in (False, True):
            with self.subTest(proposto=proposto):
                i = issue("KRN-1072", comentarios=[] if proposto else [(texto, True)])
                r = self.avaliar(i, gh_falso({}), comentario=texto if proposto else None)
                self.assertTrue(r["ok"], r)
                portao_done._delegar(i, r, self.dados)
                self.assertIn(f"Faltam dados que só o {config.dono()} tem",
                              portao_done.pendencias_fulano(self.dados)["KRN-1072"]["pendencia"])

        # O destino do parágrafo dos dados não assume a execução futura.
        i = issue("KRN-1072", comentarios=[(texto + "\n\nFalta confirmar o primeiro disparo real em 19/10.", True)])
        self.assertFalse(self.avaliar(i, gh_falso({}))["ok"])
        self.assertTrue(portao_done.pendencias("Faltam dados que só o sistema tem.", "KRN-1072")[0])

    def test_pr_citado_so_no_comentario_do_done(self):
        i = issue("KRN-3")
        r = self.avaliar(i, gh_falso({"acme/x#7": ("open", False)}), comentario="Pronto: https://github.com/acme/x/pull/7")
        self.assertFalse(r["ok"])

    def test_pr_citado_so_no_comentario_do_claude(self):
        i = issue("KRN-3", comentarios=[("PR https://github.com/acme/x/pull/7 aberto.", True)])
        self.assertFalse(self.avaliar(i, gh_falso({"acme/x#7": ("open", False)}))["ok"])

    def test_comentario_do_done_vale_no_lugar_do_ultimo(self):
        i = issue("KRN-4", comentarios=[("Merge feito; volto quando estiver no ar.", True)])
        self.assertFalse(self.avaliar(i, gh_falso({}))["ok"])
        self.assertTrue(self.avaliar(i, gh_falso({}), comentario="Subiu: /api/versao mostra o commit.")["ok"])

    def test_pendencia_do_dono_passa_e_entra_na_lista(self):
        i = issue("KRN-5", comentarios=[("Falta o token da conta, depende de você.", True)])
        r = self.avaliar(i, gh_falso({}))
        self.assertTrue(r["ok"])
        portao_done._delegar(i, r, self.dados)
        self.assertIn("KRN-5", portao_done.pendencias_fulano(self.dados))
        portao_done.resolver_pendencia_fulano("KRN-5", self.dados)
        self.assertEqual({}, portao_done.pendencias_fulano(self.dados))

    def test_promessa_de_pdf_sem_aviso(self):
        i = issue("KRN-6", descricao="Envie o relatório por WhatsApp.\n\nDetalhes: refatorar o gerador.", comentarios=[("Feito.", True)])
        r = self.avaliar(i, gh_falso({}))  # Done há 5 h: o hub já devia ter mandado
        self.assertFalse(r["ok"])
        self.assertIn("aviso", r["faltas"][0])
        recente = {**i, "completedAt": "2026-10-16T21:50:00Z"}
        self.assertTrue(self.avaliar(recente, gh_falso({}))["ok"])  # recém-Done: o envio sai no recibo de pronto

    DESC_979 = ("## Parte 2: triagem\n* (c) Pendência que depende do Fulano: fica Done, e a pendência vai para UMA lista única no Krohnos, sem WhatsApp.\n"
                "* **Entrega ao Fulano:** quando a issue promete PDF ou WhatsApp, o aviso tem que constar em `avisos/enviados`.\n"
                "Se o hub mandar o relatório por WhatsApp, o aviso consta.")

    def test_dispensa_aviso_nenhum_bru979(self):
        sem = issue("KRN-979", descricao=self.DESC_979, comentarios=[("Feito.", True)])
        self.assertTrue(portao_done.promete_aviso(sem))
        com = {**sem, "description": self.DESC_979 + "\n\naviso: nenhum"}
        self.assertFalse(portao_done.promete_aviso(com))
        self.assertTrue(self.avaliar(com, gh_falso({}))["ok"])
        self.assertFalse(self.avaliar(sem, gh_falso({}))["ok"])

    def test_dispensa_em_comentario_humano_so(self):
        i = issue("KRN-8", descricao="Envie o PDF por WhatsApp.", comentarios=[("Aviso: nenhum", True)])
        self.assertTrue(portao_done.promete_aviso(i))  # conta não se dispensa sozinha
        i = issue("KRN-8", descricao="Envie o PDF por WhatsApp.", comentarios=[("Orquestrador:\n\naviso: nenhum", False)])
        self.assertFalse(portao_done.promete_aviso(i))
        i = issue("KRN-8", descricao="Envie o PDF por WhatsApp.", comentarios=[("aviso: nenhum enviado ainda", False)])
        self.assertTrue(portao_done.promete_aviso(i))  # "nenhum enviado ainda" não é dispensa
        i = issue("KRN-8", descricao="Envie o PDF por WhatsApp.", comentarios=[("o aviso: nenhum jeito", False)])
        self.assertTrue(portao_done.promete_aviso(i))


class Escrita(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.mov, self.comentarios = [], []

    def barrar(self, i, comentario=None, r=None):
        return portao_done.barrar(i["identifier"], comentario, consulta=lambda q, **v: {"issue": i}, mover_fn=lambda a, b: self.mov.append(b),
                                  comentar_fn=lambda a, t: self.comentarios.append(t), avaliar_fn=lambda *a, **k: r, base_dados=self.dados)

    def test_barra_e_volta_para_in_progress(self):
        faltas = self.barrar(issue("KRN-7"), "Merge feito.", {"ok": False, "faltas": ["o PR x#1 ainda está aberto"]})
        self.assertEqual(["o PR x#1 ainda está aberto"], faltas)
        self.assertEqual(["s-ip"], self.mov)
        self.assertEqual("Merge feito.", self.comentarios[0])
        self.assertIn("Done barrado", self.comentarios[1])

    def barrar_silencioso(self, i, comentario, r):
        return portao_done.barrar(i["identifier"], comentario, consulta=lambda q, **v: {"issue": i}, mover_fn=lambda a, b: self.mov.append(b),
                                  comentar_fn=lambda a, t: self.comentarios.append(t), avaliar_fn=lambda *a, **k: r, base_dados=self.dados,
                                  silencioso=True)

    def test_bru_1548_execucao_propria_so_espera_nao_move_nem_comenta(self):
        faltas = self.barrar_silencioso(issue("KRN-7"), "PR no CI; volto quando sair.", {"ok": False, "faltas": ["o PR x#1 ainda está aberto (não mergeado)"]})
        self.assertEqual(["o PR x#1 ainda está aberto (não mergeado)"], faltas)
        self.assertEqual(([], []), (self.mov, self.comentarios))

    def test_bru_1548_execucao_propria_pendencia_aberta_e_merge_fora_do_ar_nao_comenta(self):
        faltas = ['o merge f498cfee não está no ar em example-org/x (produção em 71782eb3)',
                  'o último comentário deixa pendência aberta: "Falta: curl 200 e seomator."']
        self.assertEqual(faltas, self.barrar_silencioso(issue("KRN-7"), None, {"ok": False, "faltas": faltas}))
        self.assertEqual(([], []), (self.mov, self.comentarios))

    def test_bru_1548_silencioso_com_falta_real_continua_barrando(self):
        self.barrar_silencioso(issue("KRN-7"), None, {"ok": False, "faltas": ["falta o print anexado"]})
        self.assertEqual(["s-ip"], self.mov)
        self.assertIn("Done barrado", self.comentarios[0])

    def test_bru_1548_orquestrador_sem_espera_comportamento_atual(self):
        self.barrar(issue("KRN-7"), None, {"ok": False, "faltas": ["o PR x#1 ainda está aberto"]})
        self.assertEqual(["s-ip"], self.mov)

    def test_bru_1548_nao_reposta_o_comentario_que_ja_e_o_ultimo(self):
        i = issue("KRN-7", comentarios=[("Merge feito.", True)])
        self.barrar(i, "Merge feito.", {"ok": False, "faltas": ["falta o print anexado"]})
        self.assertEqual(1, len(self.comentarios))
        self.assertIn("Done barrado", self.comentarios[0])

    def test_passa_sem_mexer(self):
        self.assertEqual([], self.barrar(issue("KRN-7"), None, {"ok": True, "faltas": []}))
        self.assertEqual(([], []), (self.mov, self.comentarios))

    def test_leitura_que_falha_barra_sem_expor_erro(self):
        def quebra(*a, **k):
            raise OSError("detalhe privado")
        comentario = ("ainda não está no ar: o primeiro `atualizar-hub` falhou, e o segundo ainda não deu resultado.\n\n"
                      "**Falta:** o segundo `atualizar-hub` (20261017-073658-atualizar-hub-3873) rodar e passar.\n\n"
                      "volto quando estiver no ar.")
        for texto in (None, comentario):
            with self.subTest(comentario=texto):
                self.mov.clear()
                self.comentarios.clear()
                f = portao_done.barrar("KRN-979", texto,
                                      consulta=lambda q, **v: {"issue": issue("KRN-979", comentarios=[(comentario, True)])},
                                      avaliar_fn=quebra, mover_fn=lambda a, b: self.mov.append(b),
                                      comentar_fn=lambda a, t: self.comentarios.append(t))
                self.assertEqual(["s-ip"], self.mov)
                self.assertTrue(any("recibo não conferido" in falta for falta in f))
                self.assertEqual(3, sum("pendência aberta" in falta for falta in f))
                self.assertIn("Done barrado", self.comentarios[-1])
                self.assertNotIn("detalhe privado", " ".join(self.comentarios))

    def test_webhook_desfaz_done_manual(self):
        i = issue("KRN-8", comentarios=[("Falta o merge.", True)])
        with patch("hub.linear.reabrir") as reabrir, patch("hub.agenda.enfileirar") as enfileirar:
            f = portao_done.conferir_uma("KRN-8", desfazer=True, consulta=lambda q, **v: {"issue": i}, base_dados=self.dados,
                                     avaliar_fn=lambda *a, **k: {"ok": False, "faltas": ["pendência"]},
                                     mover_fn=lambda a, b: self.mov.append(b), comentar_fn=lambda a, t: self.comentarios.append(t))
            reabrir.assert_called_once_with("KRN-8", "Portão de Done: faça o que falta e prove: pendência")
            enfileirar.assert_called_once_with(reabrir.return_value, "portão de Done", self.dados)
        self.assertEqual(["pendência"], f)
        self.assertEqual(["s-ip"], self.mov)

    def test_inc390_webhook_respeita_fila_execucao_espera_e_dono(self):
        for caso in ("fila", "rodando", "espera", "dono", "auditoria"):
            with self.subTest(caso=caso), patch("hub.linear.reabrir") as reabrir, \
                    patch("hub.agenda.enfileirar") as enfileirar, \
                    patch("hub.agenda.fila", return_value=[{"nome": "linear-krn-700"}] if caso == "fila" else []), \
                    patch("hub.agenda.rodando_todas", return_value=[{"nome": "linear-krn-700"}] if caso == "rodando" else []), \
                    patch("hub.esperas.ler", return_value={"pr": "url"} if caso == "espera" else None):
                portao_done.conferir_uma(
                    "KRN-700", desfazer=caso != "auditoria", base_dados=self.dados,
                    consulta=lambda *a, **k: {"issue": issue("KRN-700")},
                    avaliar_fn=lambda *a, **k: {"ok": False, "faltas": ["aviso"], "depende_fulano": caso == "dono"},
                    mover_fn=lambda *a: None, comentar_fn=lambda *a: None)
                reabrir.assert_not_called()
                enfileirar.assert_not_called()

    def test_webhook_ignora_o_que_nao_e_done(self):
        i = {**issue("KRN-9"), "state": {"name": "Todo", "type": "unstarted"}}
        self.assertEqual([], portao_done.conferir_uma("KRN-9", desfazer=True, consulta=lambda q, **v: {"issue": i}, mover_fn=self.mov.append))
        self.assertEqual([], self.mov)


class Ronda(unittest.TestCase):
    def test_conta_e_grava(self):
        with tempfile.TemporaryDirectory() as d:
            ruim, bom = issue("KRN-10"), issue("KRN-11")
            r = portao_done.ronda(3, consulta=lambda q, **v: {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": [ruim, bom]}},
                                  base_dados=Path(d), agora=AGORA, log=lambda *a: None,
                                  avaliar_fn=lambda i, **k: {"ok": i is bom, "faltas": ["Falta"], "delegadas": []})
            self.assertEqual(["KRN-10"], [a["issue"] for a in r])
            self.assertEqual(1, json.loads((Path(d) / "claude" / "auditoria-done.json").read_text())["achados"])

    def test_ronda_apenas_audita_sem_silenciar_ou_enfileirar(self):
        with tempfile.TemporaryDirectory() as d:
            apto = {**issue("KRN-20"), "title": "Render do apartamento"}
            aviso, outra = issue("KRN-21"), issue("KRN-22")
            faltas = {id(aviso): ['o aviso ao Fulano que o "Pronto quando" pede não saiu']}
            r = portao_done.ronda(3, consulta=lambda q, **v: {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": [apto, aviso, outra]}},
                                  base_dados=Path(d), agora=AGORA, log=lambda *a: None,
                                  avaliar_fn=lambda i, **k: {"ok": False, "faltas": faltas.get(id(i), ["Falta o merge"]), "delegadas": []})
            self.assertEqual(["KRN-21", "KRN-22"], [a["issue"] for a in r])
            self.assertFalse(portao_done.silenciada("KRN-21", Path(d)))
            self.assertFalse(portao_done.silenciada("KRN-22", Path(d)))
            self.assertEqual([], portao_done._ler_fila(Path(d))["fila"])
            acum = json.loads((Path(d) / "claude" / "achados-ronda.json").read_text())
            self.assertEqual(["KRN-21", "KRN-22"], sorted(acum))

    def test_achado_persiste_entre_rondas_ate_passar(self):
        with tempfile.TemporaryDirectory() as d:
            ruim = issue("KRN-30")
            def rodar(ok):
                portao_done.ronda(3, consulta=lambda q, **v: {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": [ruim] if ok is not None else []}},
                                  base_dados=Path(d), agora=AGORA, log=lambda *a: None,
                                  avaliar_fn=lambda i, **k: {"ok": bool(ok), "faltas": ["Falta o merge"], "delegadas": []})
                return json.loads((Path(d) / "claude" / "achados-ronda.json").read_text())
            self.assertIn("KRN-30", rodar(False))
            self.assertIn("KRN-30", rodar(None))  # saiu da janela de 3 dias: continua para a triagem
            self.assertNotIn("KRN-30", rodar(True))


class Reabertura(unittest.TestCase):
    def consulta(self, estados):
        def c(q, **v):
            return {"issue": {"id": v["i"], "title": "Página qualquer" if v["i"] != "KRN-77" else "Render do apartamento", "state": {"type": estados.get(v["i"], "completed")}, "team": {"states": {"nodes": [{"id": "s-todo", "name": "Todo"}]}}}}
        return c

    def test_lote_de_10_com_as_reabertas_descontadas(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            portao_done._gravar_fila({"fila": [], "reabertas": [f"KRN-10{n}" for n in range(6)]}, d)
            portao_done.enfileirar_reabertura([{"ident": f"KRN-{n}", "pendencia": f"fazer {n}"} for n in range(1, 8)], d)
            mov = []
            feitas = portao_done.reabrir_lote(d, confirmado=True, consulta=self.consulta({f"KRN-10{n}": "started" for n in range(6)}),
                                              mover_fn=lambda a, b, c=None: mov.append((b, c)), log=lambda *a: None)  # 6 abertas: sobram 4
            self.assertEqual(["KRN-1", "KRN-2", "KRN-3", "KRN-4"], feitas)
            self.assertEqual("s-todo", mov[0][0])
            self.assertIn("fazer 1", mov[0][1])
            est = portao_done._ler_fila(d)
            self.assertEqual(3, len(est["fila"]))
            self.assertEqual(10, len(est["reabertas"]))

    def test_concluida_libera_vaga_e_issue_mexida_sai_da_fila(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            portao_done._gravar_fila({"fila": [{"ident": "KRN-1", "pendencia": "x"}, {"ident": "KRN-2", "pendencia": "y"}], "reabertas": ["KRN-9"]}, d)
            mov = []
            feitas = portao_done.reabrir_lote(d, confirmado=True, consulta=self.consulta({"KRN-9": "completed", "KRN-1": "started"}),
                                              mover_fn=lambda a, b, c=None: mov.append(a), log=lambda *a: None)
            self.assertEqual(["KRN-2"], feitas)  # já foi mexida
            self.assertEqual({"fila": [], "reabertas": ["KRN-2"]}, portao_done._ler_fila(d))

    def test_sem_confirmacao_nao_reabre_nada(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            portao_done.enfileirar_reabertura([{"ident": "KRN-1", "pendencia": "x"}], d)
            mov = []
            self.assertEqual([], portao_done.reabrir_lote(d, consulta=self.consulta({}), mover_fn=lambda *a: mov.append(a), log=lambda *a: None))
            self.assertEqual([], mov)

    def test_projeto_superado_nunca_reabre_e_reaberta_fica_em_silencio(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            portao_done._gravar_fila({"fila": [{"ident": "KRN-77", "pendencia": "x"}, {"ident": "KRN-1", "pendencia": "y"}], "reabertas": []}, d)
            feitas = portao_done.reabrir_lote(d, confirmado=True, consulta=self.consulta({}), mover_fn=lambda *a: None, log=lambda *a: None)
            self.assertEqual(["KRN-1"], feitas)
            self.assertTrue(portao_done.silenciada("KRN-1", d))
            self.assertFalse(portao_done.silenciada("KRN-77", d))
            self.assertTrue(portao_done.excluida("Jantar: espelho do apartamento"))


if __name__ == "__main__":
    unittest.main()


class RepasseSemResposta(unittest.TestCase):
    """INC-258: na o pedido do Fulano entrou às 16:01 (repasse) e a issue foi fechada às 16:02 por um comentário
    humano ("Orquestrador: entregue"), sem a execução ver o pedido."""
    REP = "Ajustes no mesmo PR antes do merge.\n\n_Repasse do orquestrador._"

    def test_repasse_depois_da_entrega_barra(self):
        i = issue("KRN-1456", comentarios=[("PR #86 mergeado; relatório pronto.", True), (self.REP, True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_resposta_humana_ao_repasse_passa(self):
        """o `mover --comentario` sai pelo token do Fulano (sem app); a voltou de Done 3 vezes por isso."""
        i = issue("KRN-1479", comentarios=[("PR #86 mergeado; relatório pronto.", True), (self.REP, True),
                                           ("Feito: ajustes no mesmo PR, tabela colada.", False)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))

    def test_entrega_depois_do_repasse_passa(self):
        i = issue("KRN-1456", comentarios=[(self.REP, True), ("Feito: PR #91 mergeado, tabela colada.", True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))

    def test_repasse_de_aceite_ou_fechamento_nao_barra(self):
        """textos reais de, barrados sem motivo."""
        for texto in ("Entrega aceita; PR mergeado e no ar. Mova para Done.", "Fecho.",
                      "Siga com a prova do lote e feche."):
            i = issue("KRN-1549", comentarios=[("PR #1 mergeado.", True), (f"{texto}\n\n_Repasse do orquestrador._", True)])
            self.assertEqual(([], []), portao_done.faltas_do_texto(i), texto)

    def test_repasse_com_pedido_novo_continua_barrando(self):
        i = issue("KRN-1549", comentarios=[("PR #1 mergeado.", True),
                                           ("Rode a prova do lote e responda.\n\n_Repasse do orquestrador._", True)])
        faltas = portao_done.faltas_do_texto(i)[0]
        self.assertTrue(faltas)
        self.assertIn("--comentario", faltas[0])

    def test_repasse_que_pede_e_manda_fechar_continua_barrando(self):
        # achado da revisão do PR #805: "rode X e feche" tem trabalho pendente
        i = issue("KRN-1549", comentarios=[("PR #1 mergeado.", True),
                                           ("Rode a prova do lote e feche.\n\n_Repasse do orquestrador._", True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_repasse_aceito_fechando_nao_barra(self):
        """texto real da (INC-258 barrou o Done que o próprio repasse mandava fazer)."""
        texto = ("Orquestrador: aceito o registro sem par antes/depois (robots não mudou; falso positivo do SEOmator). "
                 "Fechando em Done.")
        i = issue("KRN-1577", comentarios=[("PR #1205 mergeado.", True), (f"{texto}\n\n_Repasse do orquestrador._", True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))

    def test_repasse_nao_aceito_continua_pedido(self):
        """Achado do Codex no PR #821: rejeição não é fechamento."""
        i = issue("KRN-1549", comentarios=[("PR #1 mergeado.", True),
                                           ("Não aceito o resultado. Adicione os testes.\n\n_Repasse do orquestrador._", True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])

    def test_repasse_de_fechamento_nao_apaga_pedido_anterior(self):
        i = issue("KRN-1549", comentarios=[("PR #1 mergeado.", True),
                                           ("Corrija o teste.\n\n_Repasse do orquestrador._", True),
                                           ("Fecho.\n\n_Repasse do orquestrador._", True)])
        self.assertTrue(portao_done.faltas_do_texto(i)[0])


class SugestaoForaDoEscopo(unittest.TestCase):
    def test_inc293_sugestao_nao_e_pendencia_da_pagina_recusada(self):
        texto = ('Não publiquei a página: ela não ganha da SERP real.\n\n'
                 '- **Sugestão, não tarefa aberta:** uma página de cauda longa, '
                 'só com dado conferido. Essa SERP ainda não foi comparada.\n\n'
                 'Registrei a decisão e os motivos.')
        self.assertEqual(([], []), portao_done.faltas_do_texto(
            issue('KRN-1155', comentarios=[(texto, True)])))

    def test_marcador_nao_esconde_pendencia_em_outra_linha(self):
        for separador in ('\n', '\n\n'):
            texto = ('- **Sugestão, não tarefa aberta:** comparar outra SERP.'
                     + separador + 'Falta conferir a página pedida.')
            self.assertTrue(portao_done.pendencias(texto)[0])

    def test_sugestao_comum_e_entrega_ausente_continuam_barrando(self):
        for texto in ('Sugestão: essa SERP ainda não foi comparada.',
                      '- **Sugestão, não tarefa aberta:** os dados não foram entregues.'):
            self.assertTrue(portao_done.pendencias(texto)[0])


class ListaDelegadaINC297(unittest.TestCase):
    CAB = "Ficaram duas pendências fora do escopo desta issue, que abri como KRN-1637 (Backlog):\n"
    ITEM = "- A versão EN do FAQ ainda não tem a pergunta nova."

    def test_destino_no_cabecalho_da_lista(self):
        texto = self.CAB + '- Texto sobre custas.\n' + self.ITEM
        abertas, delegadas = portao_done.pendencias(texto, "KRN-405")
        self.assertEqual([], abertas)
        self.assertEqual(1, len(delegadas))
        self.assertFalse(delegadas[0].dono)

    def test_sem_destino_ou_destino_proprio_bloqueia(self):
        for cab in ("Pendências fora deste PR:\n", self.CAB.replace("KRN-1637", "KRN-405")):
            with self.subTest(cab=cab):
                self.assertTrue(portao_done.pendencias(cab + self.ITEM, "KRN-405")[0])

    def test_destino_nao_vaza_para_outro_paragrafo_ou_prosa(self):
        for separador in ("\n\n", "\n"):
            texto = self.CAB + '- Outra coisa. ' + separador + 'Ainda não tem a pergunta nova.'
            with self.subTest(separador=separador):
                self.assertTrue(portao_done.pendencias(texto, "KRN-405")[0])

    def test_ausencia_explicita_de_entrega_continua_bloqueada(self):
        self.assertTrue(portao_done.pendencias(self.CAB + '- A lista não foi entregue.', "KRN-405")[0])


class CitacaoSemNegritoINC335(unittest.TestCase):
    RELATO = ('O mover recusava por comentários antigos com "Falta: conferir o deploy". '
              'Passei o recibo com --comentario, e o portão aceitou. Não forcei a passagem.')
    DIAGNOSTICO = ('Done barrado pelo portão: o último comentário deixa pendência aberta: '
                   '"Falta:** conferir o deploy. O hub o confere quando subir".')

    def test_citacao_do_diagnostico_sem_negrito_nao_reabre_done(self):
        i = issue("KRN-1674", comentarios=[(self.DIAGNOSTICO, True),
                  ("Conferência fechada. No ar, verificado com curl.", False), (self.RELATO, True)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))
        self.assertEqual(([], []), portao_done.faltas_do_texto(i, self.RELATO))

    def test_sem_diagnostico_ou_com_pendencia_nova_continua_bloqueando(self):
        for texto, historico in [(self.RELATO, []),
                                 (self.RELATO + " Falta testar o formulário.", [self.DIAGNOSTICO]),
                                 ('Falta: conferir o deploy', [self.DIAGNOSTICO]),
                                 ('"Falta: conferir o pagamento"', [self.DIAGNOSTICO])]:
            with self.subTest(texto=texto):
                self.assertTrue(portao_done.pendencias(texto, "KRN-1674", historico)[0])
