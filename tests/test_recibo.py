"""item 1 (princípios 7 e 13): Done só com recibo. Caso real: foi para Done (14/10 20:14) sem os 2 mp4
nem as 3 amostras, porque a voz dependia de assinar a ElevenLabs; o Ana ficou esperando."""
import datetime as dt, json, tempfile, unittest
import urllib.error
from pathlib import Path
from unittest import mock
from hub import incidentes, recibo

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 20, 20, tzinfo=UTC)
MERGE = "a" * 40

BRU667 = {
    "id": "id-667", "identifier": "KRN-667", "title": "Reel ClienteJ: trocar a voz por narradora da ElevenLabs",
    "completedAt": "2026-10-14T20:14:34.860Z", "state": {"name": "Done", "type": "completed"},
    "description": ("Repo: example-org/filmes.\n\n4. Render final A e B com a voz escolhida.\n"
                    "5. Comente aqui o caminho dos 2 mp4 finais e das 3 amostras; eu mando para o WhatsApp dele. PR com merge."),
    "attachments": {"nodes": [{"url": "https://github.com/example-org/filmes/pull/64", "title": "Reel ClienteJ"}]},
    "comments": {"nodes": [{"createdAt": "2026-10-14T20:19:00Z", "user": {"app": True}, "body":
        "Os 2 mp4 finais e as 3 amostras de voz ainda não foram feitos. A conta da chave continua no plano free. "
        "A voz nova depende de assinar o Starter da ElevenLabs (US$ 5/mês)."}]},
    "team": {"id": "t", "states": {"nodes": [{"id": "s-ip", "name": "In Progress"}, {"id": "s-done", "name": "Done"}]}},
}


def gh_falso(merged=True):
    def gh(caminho):
        if caminho.endswith("/pulls/64"):
            return {"merged": merged, "merge_commit_sha": MERGE, "merged_at": "2026-10-14T20:10:00Z",
                    "state": "closed" if merged else "open"}
        raise AssertionError(caminho)
    return gh


class Regras(unittest.TestCase):
    def test_pronto_quando_pega_a_secao(self):
        d = "Faça X.\n\n**Pronto quando:** vídeo anexado e aviso ao Ana.\n\n## Notas\nnada"
        self.assertEqual("vídeo anexado e aviso ao Ana.", recibo.pronto_quando(d))

    def test_inc85_aceite_preserva_todos_os_paragrafos(self):
        descricao = ("## Trabalho\nInstalar Clarity.\n\n## Aceite da Frente 1\n\n"
                     "Eventos confirmados por export.\n\nPrint da máscara anexado na issue.\n\n"
                     "Tags visíveis.\n\n## Notas\nPDF anexado não faz parte do aceite.")
        aceite = recibo.pronto_quando(descricao)
        self.assertIn("Eventos confirmados", aceite)
        self.assertIn("Tags visíveis", aceite)
        self.assertNotIn("Notas", aceite)
        self.assertEqual({"imagem"}, recibo.exige(aceite))

    def test_aceite_continua_depois_de_subtitulo_e_codigo(self):
        descricao = ("## Aceite da Frente 1\n\nPrint anexado.\n\n### Critérios\n\nPDF anexado.\n\n"
                     "```sh\n# comentário\nls\n```\n\nTags visíveis.\n\n## Notas\nvídeo fora")
        aceite = recibo.pronto_quando(descricao)
        self.assertIn("Critérios", aceite)
        self.assertIn("comentário", aceite)
        self.assertIn("Tags visíveis", aceite)
        self.assertNotIn("vídeo fora", aceite)

    def test_sem_secao_usa_o_ultimo_paragrafo(self):
        self.assertIn("mp4", recibo.pronto_quando(BRU667["description"]))

    def test_exige(self):
        self.assertEqual({"video"}, recibo.exige("Comente o caminho dos 2 mp4 finais"))
        self.assertEqual({"pdf", "aviso"}, recibo.exige("PDF anexado e um aviso ao Ana no WhatsApp"))
        self.assertEqual(set(), recibo.exige("PR mergeado e no ar"))

    def test_inc45_proibicao_de_whatsapp_nao_exige_aviso(self):
        descricao = (
            "## O que fazer\n\n"
            "1. Ponha um teto diário de custo no código. "
            "Erro técnico vai para o orquestrador, nunca para o WhatsApp do Ana (KRN-876).\n"
            "2. Comentário final: custo por dia antes e depois."
        )
        self.assertNotIn("aviso", recibo.exige(recibo.pronto_quando(descricao)))
        for negacao in ("nunca", "jamais", "não"):
            with self.subTest(negacao=negacao):
                self.assertNotIn("aviso", recibo.exige(f"Erro técnico {negacao} no WhatsApp do Ana"))
        self.assertEqual({"aviso"}, recibo.exige(
            "Erro técnico nunca para o WhatsApp do Ana; aviso ao Ana com o resultado final."))


    def test_inc46_recibo_nao_consulta_envio_proibido(self):
        descricao = (
            "## O que fazer\n\n"
            "4. Ponha um teto diário de custo no código. Erro técnico vai para o "
            "orquestrador, nunca para o WhatsApp do Ana (KRN-876).\n"
            "5. Outros usos de OpenRouter no repo.\n"
            "6. Comentário final: custo por dia antes e depois."
        )
        issue = {**BRU667, "identifier": "KRN-950", "description": descricao,
                 "attachments": {"nodes": []}, "comments": {"nodes": []}}
        with tempfile.TemporaryDirectory() as dados, \
                mock.patch.object(recibo, "_avisou", side_effect=AssertionError("envio proibido")), \
                mock.patch("hub.sites.ativos", return_value=[]):
            resultado = recibo.conferir(issue, base_dados=dados, agora=T0)
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual([], resultado["faltas"])


class DeployNaFila(unittest.TestCase):
    """merge fora do ar com execução do workflow pendente é deploy atrasado, não falta."""
    ISSUE = {**BRU667, "description": "Corrigir.", "attachments": {"nodes": [{"url": "https://github.com/example-org/clienteh/pull/9"}]}}

    def setUp(self):
        patch = mock.patch.dict(recibo.DEPLOY_CRON, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def conferir(self, runs, merged_at, agora=T0):
        def gh(c):
            if "/pulls/" in c:
                return {"merged": True, "merge_commit_sha": MERGE, "state": "closed", "base": {"ref": "main"}, "merged_at": merged_at}
            if "/actions/runs" in c:
                self.assertIn("branch=main", c)
                return {"workflow_runs": runs}
            return {"status": "identical" if c.endswith("c" * 40) else "behind"}  # compare: só a execução (c) contém o merge; produção (b) não
        return recibo.conferir(self.ISSUE, gh=gh, get=lambda u: {"commit": "b" * 40}, base_dados=self.dados, agora=agora)

    def test_pendente_duas_horas_depois_espera(self):
        run = {"id": 1, "name": "imagem", "head_sha": "c" * 40, "status": "queued", "created_at": "2026-10-14T18:30:00Z"}
        r = self.conferir([run], "2026-10-14T18:20:00Z")
        self.assertEqual((True, []), (r["esperar"], r["faltas"]), r)

    def test_outro_workflow_pendente_nao_segura_o_recibo(self):
        run = {"id": 2, "name": "testes", "head_sha": "c" * 40, "status": "in_progress", "created_at": "2026-10-14T18:30:00Z"}
        r = self.conferir([run], "2026-10-14T18:20:00Z")
        self.assertFalse(r["esperar"])
        self.assertTrue(r["faltas"], r)

    def test_cancelada_sem_pendente_e_falta(self):
        run = {"id": 1, "name": "imagem", "head_sha": "c" * 40, "status": "completed", "conclusion": "cancelled", "created_at": "2026-10-14T18:30:00Z"}
        r = self.conferir([run], "2026-10-14T18:20:00Z")
        self.assertFalse(r["esperar"])
        self.assertIn("deploy falhou", r["faltas"][0])
        r = self.conferir([], "2026-10-14T18:20:00Z")
        self.assertIn("nenhum deploy disparado", r["faltas"][0])

    def test_teto_estourado_e_falta(self):
        run = {"id": 1, "name": "imagem", "head_sha": "c" * 40, "status": "in_progress", "created_at": "2026-10-14T10:00:00Z"}
        r = self.conferir([run], "2026-10-14T10:00:00Z")  # 10 h antes
        self.assertFalse(r["esperar"])
        self.assertTrue(r["faltas"])


class DeployCronLocal(unittest.TestCase):
    ISSUE = DeployNaFila.ISSUE
    conferir = DeployNaFila.conferir

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_cron_nao_consulta_actions(self):
        def gh(c):
            raise AssertionError(c)
        estado, nota = recibo.deploy_pendente("example-org/clienteh", MERGE, "main", gh)
        self.assertEqual("pendente", estado)
        self.assertIn("ainda não confirmada", nota)

    def test_cron_aguarda_sem_declarar_pronto(self):
        r = self.conferir([], "2026-10-14T18:20:00Z")
        self.assertTrue(r["esperar"])
        self.assertFalse(r["faltas"])
        self.assertFalse(r["ok"])

    def test_inc63_producao_contem_merge_dispensa_workflow(self):
        def gh(caminho):
            if "/pulls/" in caminho:
                return {"merged": True, "merge_commit_sha": MERGE,
                        "base": {"ref": "main"}, "merged_at": "2026-10-14T10:00:00Z"}
            if "/compare/" in caminho:
                return {"status": "ahead"}
            raise AssertionError(caminho)  # cron local nunca consulta Actions
        r = recibo.conferir(self.ISSUE, gh=gh, get=lambda u: {"commit": "b" * 40},
                            base_dados=self.dados, agora=T0)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["esperar"], r)
        self.assertEqual([], r["faltas"])

    def test_cron_cobra_publicacao_apos_teto(self):
        r = self.conferir([], "2026-10-14T10:00:00Z")
        self.assertFalse(r["esperar"])
        self.assertIn("produção em", r["faltas"][0])
        self.assertIn("cron local", r["faltas"][0])


class FixRodada1(unittest.TestCase):
    MEDIR = ("página no ar com status 200, no sitemap; testes verdes, deploy conferido com curl.{sep}**Medir depois:** "
             "[https://clientec-com.example/tabela-de-emolumentos](<https://clientec-com.example/tabela-de-emolumentos>) · \"tabela\"")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_a_pronto_quando_para_no_rotulo_em_negrito(self):
        for sep in ("\n\n", "\n"):
            d = "**Pronto quando:** " + self.MEDIR.format(sep=sep)
            self.assertEqual("página no ar com status 200, no sitemap; testes verdes, deploy conferido com curl.",
                             recibo.pronto_quando(d))

    def test_b_sem_secao_so_o_ultimo_paragrafo(self):
        self.assertEqual("5. Comente aqui o caminho dos 2 mp4 finais.", recibo.pronto_quando("Gerar PDF do laudo.\n\n5. Comente aqui o caminho dos 2 mp4 finais."))
        self.assertEqual({"video"}, recibo.exige(recibo.pronto_quando(BRU667["description"])))

    def test_c_video_so_como_entrega_e_csv_nao_e_planilha(self):
        self.assertEqual(set(), recibo.exige("/busca mostra a miniatura nos perfis de exemplo com vídeo"))
        self.assertEqual(set(), recibo.exige("Atualizar `pontos.csv`"))
        self.assertEqual({"planilha"}, recibo.exige("planilha xlsx pronta"))
        self.assertEqual({"video"}, recibo.exige("vídeos finais renderizados"))

    def test_h_aviso_e_mensagem_ao_ana(self):
        self.assertEqual(set(), recibo.exige('verifica que a data padrão é `2026-10-31` e que o aviso de "mês diferente" não aparece. PR na base `develop`'))
        self.assertEqual({"aviso"}, recibo.exige("**Um** aviso ao Ana com o número."))
        self.assertEqual({"aviso"}, recibo.exige("Mande ao Ana no WhatsApp **um** aviso com uma prancha"))

    def test_d_sem_registro_de_deploy_e_base_diferente_sao_na(self):
        site = {"repo": {"github": "example-org/cliente-a-v2"}, "ids": {"dokploy_app": "APP1"}}
        with mock.patch("hub.sites.ativos", return_value=[site]):
            ok, nota = recibo.no_ar("example-org/cliente-a-v2", MERGE, gh=lambda c: {}, base_dados=self.dados)
        self.assertIsNone(ok)
        self.assertIn("sem registro de deploy", nota)
        ok, nota = recibo.no_ar("example-org/cliente-c", MERGE, gh=lambda c: {}, base_dados=self.dados, base="develop")
        self.assertEqual((None, "no ar: n/a (base develop; sobe na promoção para main)"), (ok, nota))

    def test_e_consulta_parte_de_ultimo_menos_1h(self):
        vistas = []
        def consulta(q, **v):
            vistas.append(v.get("d"))
            return {"issues": {"nodes": []}}
        for h in (-1, 0, 2, 4):
            recibo.vigiar(consulta=consulta, base_dados=self.dados, agora=T0 + dt.timedelta(hours=h), log=lambda *a: None)
        # 1ª rodada só marca; depois parte de max(desde, ultimo - 1 h)
        self.assertEqual(["2026-10-14T19:20:00+00:00", "2026-10-14T19:20:00+00:00", "2026-10-14T21:20:00+00:00"], vistas)

    def test_f_aviso_direto_na_caixa_conta(self):
        caixa = Path(self.dados) / "avisos"
        (caixa / "enviados").mkdir(parents=True)
        (caixa / "enviados" / "x.json").write_text(json.dumps({"texto": "KRN-730 pronto"}))
        with mock.patch.object(recibo.conta, "CAIXA_AVISOS", str(caixa)):
            self.assertTrue(recibo._avisou("KRN-730", self.dados, "2020-01-01T00:00:00Z"))
            self.assertFalse(recibo._avisou("KRN-731", self.dados, "2020-01-01T00:00:00Z"))
            self.assertFalse(recibo._avisou("KRN-730", self.dados, "2999-01-01T00:00:00Z"))

    def test_g_url_so_4xx_5xx_e_falta(self):
        import urllib.error
        issue = {**BRU667, "description": "**Pronto quando:** abre https://x.com/a", "attachments": {"nodes": []}}
        site = {"dominio": "x.com", "repo": None, "ids": {}}
        for exc, faltou in ((urllib.error.HTTPError("u", 404, "n", {}, None), True), (OSError("rede"), False)):
            with mock.patch("hub.sites.ativos", return_value=[site]), mock.patch("urllib.request.urlopen", side_effect=exc):
                r = recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados)
            self.assertEqual(faltou, bool(r["faltas"]), r)

    def test_url_em_comando_markdown_nao_inclui_crase(self):
        import urllib.error
        url = "https://clienteeemoraesadv-com.example/tribunais-superiores/sumulas"
        site = {"dominio": "clienteeemoraesadv-com.example", "repo": None, "ids": {}}
        for texto in (f"`{url}`", f"[{url}]({url})",
                      f"`curl -s {url}` mostra ItemList válido.",
                      f"[{url}](<{url}>)", url):
            for status in (200, 404):
                with self.subTest(texto=texto, status=status):
                    issue = {**BRU667, "description": "**Pronto quando:** " + texto,
                             "attachments": {"nodes": []}}
                    def abrir(req, **kwargs):
                        self.assertEqual(url, req.full_url)
                        if status == 404:
                            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                        return mock.MagicMock()
                    with mock.patch("hub.sites.ativos", return_value=[site]), \
                         mock.patch("urllib.request.urlopen", side_effect=abrir) as chamada:
                        r = recibo.conferir(issue, base_dados=self.dados)
                    self.assertTrue(chamada.called)
                    self.assertEqual([f"{url} respondeu 404"] * chamada.call_count if status == 404 else [], r["faltas"])


class ProvaUrlInc60(unittest.TestCase):
    URL = "https://clientej-com-br.example/lei/sp/lei-123"

    def avaliar(self, corpo, abrir, comentario=None):
        issue = {**BRU667, "identifier": "KRN-975", "title": "Leis estaduais no ar",
                 "description": "Prova na issue: URLs reais respondendo no ar.",
                 "attachments": {"nodes": []}, "comments": {"nodes": [
                     {"createdAt": "2026-10-17T02:00:00Z", "user": {"app": True}, "body": corpo},
                     {"createdAt": "2026-10-16T22:00:00Z", "user": {"app": True},
                      "body": "Versão antiga https://clientej-com-br.example/antiga"}]}}
        with tempfile.TemporaryDirectory() as dados, \
                mock.patch("hub.sites.ativos", return_value=[{"dominio": "clientej-com-br.example"}]), \
                mock.patch.object(recibo, "conferir_dados", return_value=""), \
                mock.patch("urllib.request.urlopen", side_effect=abrir) as chamada:
            r = recibo.conferir(issue, base_dados=dados, agora=T0, comentario=comentario)
        return r, [c.args[0].full_url for c in chamada.call_args_list]

    def test_rota_500_no_comentario_final_barra_done_sem_secao_pronto_quando(self):
        erro = urllib.error.HTTPError(self.URL, 500, "DYNAMIC_SERVER_USAGE", {}, None)
        r, urls = self.avaliar(f"Prova: `{self.URL}`", erro)
        self.assertEqual([self.URL], urls)
        self.assertFalse(r["ok"])
        self.assertEqual([f"{self.URL} respondeu 500"], r["faltas"])

    def test_rede_fora_vira_nota_sem_espera_sem_teto(self):
        r, _ = self.avaliar(self.URL, OSError("rede"))
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["esperar"])
        self.assertTrue(any("não conferida" in n for n in r["notas"]))

    def test_subdominio_e_conferido_e_pontuacao_final_sai_da_url(self):
        erro = urllib.error.HTTPError(self.URL, 404, "x", {}, None)
        r, urls = self.avaliar("Veja https://www.clientej-com-br.example/lei/a.", erro)
        self.assertEqual(["https://www.clientej-com-br.example/lei/a"], urls)
        self.assertFalse(r["ok"])

    def test_comentario_final_cita_login_403_sem_barrar(self):
        r, urls = self.avaliar("Rota protegida https://clientej-com-br.example/admin responde 403.",
                               urllib.error.HTTPError(self.URL, 403, "x", {}, None))
        self.assertEqual(1, len(urls))
        self.assertTrue(r["ok"], r)

    def test_url_malformada_nao_derruba_conferir(self):
        r, urls = self.avaliar("ver http://[x", AssertionError("rede indevida"))
        self.assertTrue(r["ok"], r)

    def test_comentario_proposto_e_deduplicado_responde_200(self):
        r, urls = self.avaliar("antigo", lambda *a, **k: mock.MagicMock(),
                               comentario=f"Prova: [{self.URL}]({self.URL})")
        self.assertTrue(r["ok"], r)
        self.assertEqual([self.URL], urls)

    def test_dominio_no_caminho_ou_sufixo_nao_e_site_cadastrado(self):
        r, urls = self.avaliar("https://externo.test/clientej-com-br.example/a "
                               "https://clientej-com-br.example.externo.test/a", AssertionError("rede indevida"))
        self.assertTrue(r["ok"], r)
        self.assertEqual([], urls)


class FixRodada3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_1_falha_no_meio_nao_repete_o_que_ja_foi_feito(self):
        a = {**BRU667, "id": "id-a", "identifier": "KRN-1", "comments": {"nodes": []}, "completedAt": "2026-10-14T20:10:00Z"}
        b = {**a, "id": "id-b", "identifier": "KRN-2"}
        movidas, comentarios, falhar = [], [], [True]

        def mover(i, s, c=None):
            if i == "id-b" and falhar[0]:
                raise OSError("linear fora")
            movidas.append(i)
        kw = dict(consulta=lambda q, **v: {"issues": {"nodes": [a, b]}}, gh=gh_falso(), base_dados=self.dados, mover_fn=mover,
                  comentar_fn=lambda i, t: comentarios.append(i), reabrir_fn=lambda ident, fala: ident, log=lambda *x: None)
        recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
        recibo.vigiar(agora=T0, **kw)
        falhar[0] = False
        recibo.vigiar(agora=T0 + dt.timedelta(minutes=5), **kw)
        self.assertEqual(["id-a", "id-b"], movidas)
        self.assertEqual(["id-a", "id-b"], comentarios)

    def test_falha_lendo_pr_esgota_com_incidente_unico_e_retoma_quando_o_github_volta(self):
        import subprocess
        issue = {**BRU667, "description": "Corrigir."}
        erro = subprocess.CalledProcessError(1, ["gh", "api"])
        with mock.patch.object(recibo.esperas, "gh_api", side_effect=erro) as gh, \
             mock.patch.object(recibo.incidentes, "registrar") as incidente, \
             mock.patch.object(recibo.linear, "mover") as mover, \
             mock.patch.object(recibo.linear, "comentar") as comentar, \
             mock.patch.object(recibo.linear, "reabrir") as reabrir:
            kw = dict(consulta=lambda q, **v: {"issues": {"nodes": [issue]}},
                      base_dados=self.dados, log=lambda *a: None)
            recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
            for minuto in (0, 5, 10, 15, 20, 25):  # as rodadas depois de esgotar caem no backoff
                recibo.vigiar(agora=T0 + dt.timedelta(minutes=minuto), **kw)
            self.assertEqual(recibo.MAX_TENTATIVAS, gh.call_count)
            incidente.assert_called_once()
            self.assertEqual(f"recibo-leitura:{issue['identifier']}:{issue['completedAt']}", incidente.call_args.kwargs["chave"])
            # passado o backoff, o GitHub continua fora: tenta de novo e o incidente é o mesmo (chave igual)
            recibo.vigiar(agora=T0 + recibo.BACKOFF_ESGOTADA + dt.timedelta(minutes=30), **kw)
            self.assertEqual(recibo.MAX_TENTATIVAS + 1, gh.call_count)
            self.assertEqual(1, len({c.kwargs["chave"] for c in incidente.call_args_list}))
        mover.assert_not_called()
        comentar.assert_not_called()
        reabrir.assert_not_called()
        # GitHub voltou: a conferência normal acontece (a issue não foi dada como vista)
        with mock.patch.object(recibo, "conferir", return_value={"ok": True}) as conferir:
            recibo.vigiar(agora=T0 + 3 * recibo.BACKOFF_ESGOTADA, **kw)
        conferir.assert_called_once()

    def test_2_leitura_da_producao_falhando_espera_e_depois_e_na(self):
        def get(url):
            raise TimeoutError("lento")
        pr = {"merged": True, "merge_commit_sha": MERGE, "state": "closed", "base": {"ref": "main"}}
        issue = {**BRU667, "description": "Corrigir.", "attachments": {"nodes": [{"url": "https://github.com/example-org/clienteh/pull/9"}]}}
        for merged_at, esperar in (("2026-10-14T20:00:00Z", True), ("2026-10-14T10:00:00Z", False)):
            r = recibo.conferir(issue, gh=lambda c: {**pr, "merged_at": merged_at}, get=get, base_dados=self.dados, agora=T0)
            self.assertEqual((esperar, []), (r["esperar"], r["faltas"]), r)
        ok, nota = recibo.no_ar("example-org/clienteh", MERGE, gh=lambda c: {}, get=lambda u: {"commit": ""})
        self.assertIsNone(ok)
        self.assertIn(recibo.NAO_LEU, nota)
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
            ok, nota = recibo.no_ar("example-org/bot_whatsapp", MERGE, gh=lambda c: {})
        self.assertIsNone(ok)

    def test_3_pr_empilhado_e_na(self):
        ok, nota = recibo.no_ar("example-org/clienteh", MERGE, gh=lambda c: {}, base="krn-1-a")
        self.assertIsNone(ok)
        self.assertIn("base krn-1-a", nota)
        # Codex 104569a: bot_whatsapp publica de master; PR na master confere o checkout, não vira n/a
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=MERGE)):
            ok, nota = recibo.no_ar("example-org/bot_whatsapp", MERGE, gh=lambda c: {}, base="master")
        self.assertTrue(ok, nota)

    def test_4_exige_so_entregas(self):
        for x in ("a imagem do hero carrega rápido", "remover o print de debug", "gera o PDF do laudo no servidor", "PR anexado à issue"):
            self.assertEqual(set(), recibo.exige(x), x)
        self.assertEqual({"imagem"}, recibo.exige("prints 390/1440 anexados na issue"))
        self.assertEqual({"pdf"}, recibo.exige("PDF anexado"))
        self.assertEqual({"video"}, recibo.exige("mp4 9:16 de 8 s anexado na issue"))

    def test_5_aviso_com_ident_parecido_nao_conta(self):
        caixa = Path(self.dados) / "avisos"
        caixa.mkdir()
        (caixa / "x.json").write_text(json.dumps({"texto": "KRN-733 pronto"}))
        with mock.patch.object(recibo.conta, "CAIXA_AVISOS", str(caixa)):
            self.assertFalse(recibo._avisou("KRN-73", self.dados, "2020-01-01T00:00:00Z"))
            self.assertTrue(recibo._avisou("KRN-733", self.dados, "2020-01-01T00:00:00Z"))

    def test_6_reabrir_que_falha_e_logado(self):
        logs = []
        issue = {**BRU667, "comments": {"nodes": []}}

        def reabrir(ident, fala):
            raise OSError("fila")
        kw = dict(consulta=lambda q, **v: {"issues": {"nodes": [issue]}}, gh=gh_falso(), base_dados=self.dados,
                  mover_fn=lambda *a, **k: None, comentar_fn=lambda *a: None, reabrir_fn=reabrir, log=logs.append)
        recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
        recibo.vigiar(agora=T0, **kw)
        self.assertTrue(any("reabrir falhou" in l for l in logs), logs)


class Conferir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_bru667_sem_videos_e_dependendo_do_ana(self):
        r = recibo.conferir(BRU667, gh=gh_falso(), get=None, base_dados=self.dados)
        self.assertFalse(r["ok"])
        self.assertTrue(any("vídeo" in f for f in r["faltas"]), r["faltas"])
        self.assertTrue(r["depende_fulano"])

    def test_dependencia_usa_data_e_nao_ordem_da_api(self):
        # INC-72: um pedido antigo de credenciais não pode travar a retomada.
        antigo = {"body": "depende de você: credencial", "createdAt": "2026-10-15T14:22:00Z", "user": {"app": True}}
        recente = {"body": "Falta confirmar o deploy.", "createdAt": "2026-10-17T07:43:00Z", "user": {"app": True}}
        for nodes in ([recente, antigo], [antigo, recente]):
            with self.subTest(nodes=nodes):
                issue = {"identifier": "KRN-828", "description": "", "comments": {"nodes": nodes}}
                r = recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados)
                self.assertFalse(r["ok"])
                self.assertFalse(r["depende_fulano"])
                r = recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados,
                                    comentario="Falta confirmar o deploy.\n\nPagamento depende de você.")
                self.assertTrue(r["depende_fulano"])

    def test_com_os_videos_anexados_passa(self):
        anexos = Path(self.dados) / "claude" / "anexos"
        anexos.mkdir(parents=True)
        (anexos / "KRN-667.jsonl").write_text(json.dumps({"quando": "2026-10-14T20:30:00+00:00", "link": "[reel-A.mp4](https://uploads.linear.app/x/reel-A.mp4)", "texto": None}) + "\n")
        r = recibo.conferir(BRU667, gh=gh_falso(), base_dados=self.dados, agora=T0)  # 6 min após o Done: o envio sai no recibo
        self.assertTrue(r["ok"], r)

    def test_pr_aberto_e_falta(self):  # Codex fc916834: Done da conta com PR aberto não passa (o do Ana é pulado)
        issue = {**BRU667, "description": "Corrigir rodapé."}
        r = recibo.conferir(issue, gh=gh_falso(merged=False), base_dados=self.dados)
        self.assertFalse(r["ok"])
        self.assertTrue(any("ainda está aberto" in f for f in r["faltas"]), r)

    def test_issue_sem_pr_e_sem_exigencia_passa(self):  # Review Focus 1
        issue = {**BRU667, "description": "Qual o status da coleta?", "attachments": {"nodes": []}, "comments": {"nodes": []}}
        self.assertTrue(recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados)["ok"])

    def test_repo_sem_deploy_conhecido_nao_cobra_no_ar(self):  # Review Focus 2
        self.assertEqual((None, "no ar: n/a"), recibo.no_ar("example-org/filmes", MERGE, gh=gh_falso()))

    def test_site_com_dokploy_usa_o_sha_do_deploys_json(self):  # repo é dict em sites/*.json: repo["github"]
        site = {"repo": {"github": "example-org/cliente-a-v2"}, "ids": {"dokploy_app": "APP1"}}
        (Path(self.dados) / "vps").mkdir()
        (Path(self.dados) / "vps" / "deploys.json").write_text(json.dumps({"APP1": {"sha": "b" * 40}}))
        with mock.patch("hub.sites.ativos", return_value=[site]):
            ok, _ = recibo.no_ar("example-org/cliente-a-v2", MERGE, gh=lambda c: {"status": "ahead"}, base_dados=self.dados)
        self.assertTrue(ok)

    def test_site_com_api_versao_usa_o_commit_servido_e_cai_no_registro_sem_ela(self):  # F5
        site = {"repo": {"github": "example-org/cliente-a-v2"}, "ids": {"dokploy_app": "APP1"}, "dominio": "clientea-com.example"}
        (Path(self.dados) / "vps").mkdir()
        (Path(self.dados) / "vps" / "deploys.json").write_text(json.dumps({"APP1": {"sha": "b" * 40}}))
        urls = []
        def get(u):
            urls.append(u)
            return {"commit": MERGE, "buildStamp": None, "startedAt": "x"}
        with mock.patch("hub.sites.ativos", return_value=[site]):
            ok, nota = recibo.no_ar("example-org/cliente-a-v2", MERGE, gh=lambda c: {}, get=get, base_dados=self.dados)
            self.assertEqual(["https://clientea-com.example/api/versao"], urls)
            self.assertTrue(ok)
            self.assertIn(MERGE[:8], nota)
            # rota ainda não existe (404) ou sem commit: vale o registro do Dokploy
            def sem_rota(u):
                raise urllib.error.HTTPError(u, 404, "nf", {}, None)
            ok, nota = recibo.no_ar("example-org/cliente-a-v2", MERGE, gh=lambda c: {"status": "ahead"}, get=sem_rota, base_dados=self.dados)
            self.assertTrue(ok)
            self.assertIn("bbbbbbbb", nota)
            ok, _ = recibo.no_ar("example-org/cliente-a-v2", MERGE, gh=lambda c: {"status": "ahead"}, get=lambda u: {"commit": None}, base_dados=self.dados)
            self.assertTrue(ok)

    def test_site_sem_dokploy_mas_com_api_versao_tambem_e_conferido(self):  # F5
        site = {"repo": {"github": "example-org/cliente-q-clientei"}, "ids": {}, "dominio": "clientel"}
        with mock.patch("hub.sites.ativos", return_value=[site]):
            ok, _ = recibo.no_ar("example-org/cliente-q-clientei", MERGE, gh=lambda c: {}, get=lambda u: {"commit": "c" * 40}, base_dados=self.dados)
            self.assertFalse(ok)  # servindo outro commit que não contém o merge
            ok, nota = recibo.no_ar("example-org/cliente-q-clientei", MERGE, gh=lambda c: {}, get=lambda u: {}, base_dados=self.dados)
            self.assertEqual((None, "no ar: n/a"), (ok, nota))

    def test_clientej_no_ar_pela_api_versao(self):
        def gh(c):
            self.assertIn(f"compare/{MERGE}...{'b' * 40}", c)
            return {"status": "ahead"}
        ok, _ = recibo.no_ar("example-org/clienteh", MERGE, gh=gh, get=lambda url: {"commit": "b" * 40})
        self.assertTrue(ok)

    def test_clientej_ainda_sem_o_merge(self):
        ok, nota = recibo.no_ar("example-org/clienteh", MERGE, gh=lambda c: {"status": "behind"}, get=lambda url: {"commit": "b" * 40})
        self.assertFalse(ok)
        self.assertIn("não está no ar", nota)


class Vigiar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.movidas, self.comentarios, self.reabertas = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def _vigiar(self, issues, agora):
        def consulta(q, **v):
            if "workflowStateCreate" in q:
                return {"workflowStateCreate": {"workflowState": {"id": "s-travada"}}}
            return {"issues": {"nodes": issues}}
        return recibo.vigiar(consulta=consulta, gh=gh_falso(), get=None, base_dados=self.dados, agora=agora,
                             mover_fn=lambda i, s, c=None: self.movidas.append((i, s)),
                             comentar_fn=lambda i, t: self.comentarios.append((i, t)),
                             reabrir_fn=lambda ident, fala: self.reabertas.append(ident) or ident, log=lambda *a: None)

    def test_primeira_rodada_so_marca_o_desde(self):  # Review Focus 4: nada de reabrir Done antigos no deploy
        self.assertEqual([], self._vigiar([BRU667], T0))
        self.assertEqual([], self.movidas)

    def test_bru667_vai_para_travada_uma_vez_e_registra_incidente(self):
        self._vigiar([], T0 - dt.timedelta(hours=1))  # 1ª rodada: desde = 19:20
        self._vigiar([BRU667], T0)
        self._vigiar([BRU667], T0 + dt.timedelta(minutes=5))  # mesmo completedAt: não repete
        self.assertEqual([("id-667", "s-travada")], self.movidas)
        self.assertEqual(1, len(self.comentarios))
        self.assertIn("assinar", self.comentarios[0][1])
        self.assertEqual([], self.reabertas)
        inc = incidentes.listar(base_dados=self.dados)
        self.assertEqual(["pronto_sem_recibo"], [i["classe"] for i in inc])

    def test_sem_depender_do_ana_volta_para_in_progress_e_fila(self):
        issue = {**BRU667, "comments": {"nodes": []}}
        self._vigiar([], T0 - dt.timedelta(hours=1))
        tarefas = self._vigiar([issue], T0)
        self.assertEqual([("id-667", "s-ip")], self.movidas)
        self.assertEqual(["KRN-667"], self.reabertas)
        self.assertEqual(["KRN-667"], tarefas)


class FixRodada4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.janelas = []
        self.issue = {**BRU667, "identifier": "KRN-9", "comments": {"nodes": []}, "completedAt": "2026-10-14T20:10:00Z"}

    def consulta(self, q, **v):
        self.janelas.append(v["d"])
        return {"issues": {"nodes": [self.issue]}}

    def test_pendente_nao_roda_a_cada_tick(self):
        kw = dict(consulta=self.consulta, gh=lambda c: (_ for _ in ()).throw(OSError("gh fora")), base_dados=self.dados,
                  log=lambda *a: None)
        with mock.patch.object(recibo, "conferir", return_value={"esperar": True}):
            recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
            recibo.vigiar(agora=T0, **kw)
            recibo.vigiar(agora=T0 + dt.timedelta(minutes=1), **kw)
            self.assertEqual(1, len(self.janelas))
            recibo.vigiar(agora=T0 + dt.timedelta(hours=3), **kw)
            recibo.vigiar(agora=T0 + dt.timedelta(hours=4), **kw)
        # com pendência, a janela parte da conclusão dela (menos 1 min), não de ultimo - 1 h
        self.assertEqual("2026-10-14T20:09:00+00:00", self.janelas[-1])

    def test_issue_envenenada_esgota_sem_marcar_visto_e_segue_pendente(self):
        logs = []
        kw = dict(consulta=self.consulta, base_dados=self.dados, log=logs.append)
        with mock.patch.object(recibo, "conferir", side_effect=RuntimeError("quebrou")) as conferir, \
             mock.patch.object(recibo.incidentes, "registrar") as incidente:
            recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
            for m in range(0, 125, 5):
                recibo.vigiar(agora=T0 + dt.timedelta(minutes=m), **kw)
        self.assertEqual(recibo.MAX_TENTATIVAS, conferir.call_count)
        incidente.assert_called_once()
        self.assertFalse(any("desisti" in l for l in logs))
        self.assertEqual("2026-10-14T20:09:00+00:00", self.janelas[-1])  # a pendência segura a janela

    def test_erro_que_nao_passa_larga_a_pendencia_depois_do_prazo_sem_prender_a_janela(self):
        kw = dict(consulta=self.consulta, base_dados=self.dados, log=lambda *a: None)
        with mock.patch.object(recibo, "conferir", side_effect=RuntimeError("404")) as conferir, \
             mock.patch.object(recibo.incidentes, "registrar") as incidente:
            recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
            for m in (0, 5, 10):
                recibo.vigiar(agora=T0 + dt.timedelta(minutes=m), **kw)
            self.assertEqual(recibo.MAX_TENTATIVAS, conferir.call_count)
            depois = T0 + recibo.PRAZO_ESGOTADA + dt.timedelta(days=1)
            recibo.vigiar(agora=depois, **kw)
            recibo.vigiar(agora=depois + dt.timedelta(hours=7), **kw)
        self.assertEqual(recibo.MAX_TENTATIVAS, conferir.call_count)  # largada: ninguém mais confere
        incidente.assert_called_once()
        self.assertNotEqual("2026-10-14T20:09:00+00:00", self.janelas[-1])  # a janela solta da conclusão antiga

    def test_incidente_falhando_nao_repete_o_comentario(self):
        comentarios = []
        kw = dict(consulta=self.consulta, gh=gh_falso(), base_dados=self.dados, mover_fn=lambda i, s, c=None: None,
                  comentar_fn=lambda i, t: comentarios.append(i), reabrir_fn=lambda ident, fala: ident, log=lambda *a: None)
        with mock.patch.object(recibo.incidentes, "registrar", side_effect=OSError("disco")):
            recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **kw)
            recibo.vigiar(agora=T0, **kw)
            recibo.vigiar(agora=T0 + dt.timedelta(minutes=5), **kw)
        self.assertEqual(1, len(comentarios))


class FixFinal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.movidas, self.reabertas = [], []
        self.issue = {**BRU667, "comments": {"nodes": []}}

    def _vigiar(self, historico=None, **kw):
        def consulta(q, **v):
            if "history" in q:
                if historico is None:
                    raise OSError("linear fora")
                return {"issue": {"history": {"nodes": historico}}}
            return {"issues": {"nodes": [self.issue]}}
        args = dict(consulta=consulta, gh=gh_falso(), base_dados=self.dados, mover_fn=lambda i, s, c=None: self.movidas.append(i),
                    comentar_fn=lambda *a: None, reabrir_fn=lambda ident, fala: self.reabertas.append(ident) or ident,
                    log=lambda *a: None, **kw)
        recibo.vigiar(agora=T0 - dt.timedelta(hours=1), **args)
        return recibo.vigiar(agora=T0, **args)

    def test_done_movido_por_humano_sem_recibo_volta(self):
        """o portão vale para a UI também; o Done manual sem recibo é desfeito."""
        h = [{"createdAt": "2026-10-14T20:14:34Z", "actor": {"id": "u", "name": "Ana"}, "botActor": None,
              "toState": {"type": "completed"}}]
        self.assertEqual(["KRN-667"], self._vigiar(h))
        self.assertEqual(["id-667"], self.movidas)

    def test_i1_done_pela_integracao_sem_recibo_volta(self):
        h = [{"createdAt": "2026-10-14T20:00:00Z", "actor": {"id": "u"}, "botActor": None, "toState": {"type": "started"}},
             {"createdAt": "2026-10-14T20:14:34Z", "actor": None, "botActor": {"id": "b", "name": "GitHub", "type": "github"},
              "toState": {"type": "completed"}}]
        self.assertEqual(["KRN-667"], self._vigiar(h))
        self.assertEqual(["id-667"], self.movidas)

    def test_i1_historico_ilegivel_cobra_como_antes(self):
        self.assertEqual(["KRN-667"], self._vigiar(None))

    def test_i2_tarefa_na_fila_ou_rodando_fica_para_depois(self):
        self.assertEqual([], self._vigiar(None, ocupadas={"linear-krn-667"}))
        self.assertEqual([], self.movidas)
        est = json.loads((Path(self.dados) / "claude" / "recibos.json").read_text())
        self.assertNotIn("KRN-667", est["vistos"])
        self.assertIn("KRN-667", est["pendentes"])

    def test_i3_cartao_de_ui_nao_e_o_ana(self):
        issue = {**BRU667, "comments": {"nodes": [{"createdAt": "2026-10-14T20:19:00Z", "user": {"app": True},
                                                   "body": "Ajustei o cartão do Conector; os mp4 ficam para depois."}]}}
        self.assertFalse(recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados)["depende_fulano"])
        self.assertTrue(recibo.RE_DONO.search("falta o cartão de crédito"))

    def test_x2_aviso_parado_na_fila_da_caixa(self):
        caixa = Path(self.dados) / "avisos"
        caixa.mkdir()
        arq = caixa / "x.json"
        arq.write_text(json.dumps({"texto": "KRN-733 pronto"}))
        t = (T0 - dt.timedelta(minutes=10)).timestamp()
        import os
        os.utime(arq, (t, t))
        with mock.patch.object(recibo.conta, "CAIXA_AVISOS", str(caixa)):
            self.assertEqual("esperar", recibo._avisou("KRN-733", self.dados, "2020-01-01T00:00:00Z", T0))
            t = (T0 - dt.timedelta(minutes=40)).timestamp()
            os.utime(arq, (t, t))
            self.assertFalse(recibo._avisou("KRN-733", self.dados, "2020-01-01T00:00:00Z", T0))
            issue = {**BRU667, "identifier": "KRN-733", "description": "**Pronto quando:** aviso ao Ana.",
                     "attachments": {"nodes": []}, "comments": {"nodes": []}}
            os.utime(arq, (T0.timestamp(), T0.timestamp()))
            self.assertTrue(recibo.conferir(issue, gh=gh_falso(), base_dados=self.dados, agora=T0)["esperar"])

    def test_m1_timeout_curto(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("x")) as u, self.assertRaises(OSError):
            recibo._get_json("https://x")
        self.assertEqual(20, u.call_args.kwargs["timeout"])
