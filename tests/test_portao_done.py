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


class Texto(unittest.TestCase):
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

    def test_pendencia_com_destino_nao_barra(self):
        self.assertEqual(([], ["Falta o token da conta: vai para a KRN-1000."]), portao_done.pendencias("Falta o token da conta: vai para a KRN-1000.", "KRN-1"))
        ab, de = portao_done.pendencias("Falta a chave do Stripe, depende de você.", "KRN-1")
        self.assertEqual((0, 1), (len(ab), len(de)))

    def test_bru_em_outro_paragrafo_nao_e_destino(self):
        self.assertTrue(portao_done.pendencias("Falta executar a migração.\n\nReferência: KRN-2.", "KRN-1")[0])

    def test_nao_falta_nada_nao_e_pendencia(self):
        self.assertEqual(([], []), portao_done.pendencias("Não falta nada: tudo no ar.", "KRN-1"))

    def test_o_proprio_bru_nao_e_destino(self):
        self.assertTrue(portao_done.pendencias("Falta conferir; segue na KRN-1.", "KRN-1")[0])

    def test_ultimo_comentario_humano_nao_cobra(self):
        i = issue("KRN-1", comentarios=[("Falta o merge", True), ("fechei assim mesmo", False)])
        self.assertEqual(([], []), portao_done.faltas_do_texto(i))


class Portao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def avaliar(self, i, gh, comentario=None):
        return recibo.conferir(i, gh, lambda u: {"commit": "a" * 40}, self.dados, AGORA, comentario=comentario)

    def test_krn887_com_prs_abertos(self):
        i = issue("KRN-887", prs=["https://github.com/example-org/nimbus-hub/pull/294", "https://github.com/example-org/nimbus-hub/pull/322"],
                  comentarios=[("PRs abertos, aguardo a revisão.", True)])
        r = self.avaliar(i, gh_falso({"example-org/nimbus-hub#294": ("open", False), "example-org/nimbus-hub#322": ("open", False)}))
        self.assertFalse(r["ok"])
        self.assertEqual(2, sum("ainda está aberto" in f for f in r["faltas"]))

    def test_inc147_emissor_exige_recibo_dos_dois_prs_e_conferencia(self):
        repo = "example-org/Emissor_nf_exemplo"
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

    def test_inc171_krn1114_merge_nao_substitui_medicoes(self):
        texto = ('Volto quando saírem. O ajuste não foi medido no `seomator audit`: '
                 'a razão texto/HTML só se confirma depois do deploy.\n\n'
                 '**Falta:** 1. Merge do #309 (a label `merge` está liberada). '
                 '2. Reroda do `seomator audit` depois do deploy. '
                 '3. Medição no GSC da consulta "tabela emolumentos not".')
        for merged in (False, True):
            for proposto in (False, True):
                with self.subTest(merged=merged, proposto=proposto):
                    i = issue("KRN-1114", prs=["https://github.com/example-org/cliente-c/pull/309"],
                              comentarios=[("Conferido." if proposto else texto, True)])
                    with patch.object(recibo, "no_ar", return_value=(True, "no ar")):
                        r = self.avaliar(i, gh_falso({"example-org/cliente-c#309":
                                                     ("closed" if merged else "open", merged)}),
                                         comentario=texto if proposto else None)
                    self.assertFalse(r["ok"])
                    self.assertEqual([], r["delegadas"])
                    self.assertEqual(2, sum("pendência aberta" in f for f in r["faltas"]))
                    self.assertEqual(not merged, any("ainda está aberto" in f for f in r["faltas"]))

    def test_krn954_falta_no_fim(self):
        i = issue("KRN-954", comentarios=[("Merge feito, mas ainda não está no ar.\n\nFalta conferir depois do deploy:\n- curl", True)])
        r = self.avaliar(i, gh_falso({}))
        self.assertFalse(r["ok"])
        self.assertIn("pendência aberta", r["faltas"][0])

    def test_inc144_primeira_execucao_futura_nao_e_recibo(self):
        textos = (
            "A medida está gravando no BI.\n\n**Falta**\n"
            "- A primeira execução automática da rotina, segunda 19/10, às 7h. "
            "É a única prova que ainda não existe, e depende do calendário.",
            "A rotina está registrada e válida.\n\n"
            "- **Falta:** só a primeira execução automática de 7h, que depende do calendário. "
            "A conferência fica na segunda: contagem dos editais triados e se aparece algum fsite05.",
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
        gh = gh_falso({"example-org/x#5": ("closed", False)})
        sem = issue("KRN-2", prs=["https://github.com/example-org/x/pull/5"], comentarios=[("Pronto.", True)])
        com = issue("KRN-2", prs=["https://github.com/example-org/x/pull/5"], comentarios=[("PR #5 fechado porque a KRN-9 já entregou.", True)])
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
        gh = gh_falso({"example-org/x#5": ("closed", False), "example-org/x#6": ("closed", False)})
        i = issue("KRN-2", prs=["https://github.com/example-org/x/pull/5", "https://github.com/example-org/x/pull/6"],
                  comentarios=[("PR #5 fechado porque a KRN-9 já entregou.", True)])
        r = self.avaliar(i, gh)
        self.assertEqual(1, len(r["faltas"]))
        self.assertIn("#6", r["faltas"][0])

    def test_inc124_pr_consolidado_exige_merge_e_publicacao(self):
        repo = "example-org/repo-privado"
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
        gh = gh_falso({"example-org/x#5": ("closed", False), "example-org/x#6": ("closed", False)})
        i = issue("KRN-2", prs=["https://github.com/example-org/x/pull/5", "https://github.com/example-org/x/pull/6"])
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
        r = self.avaliar(i, gh_falso({"example-org/x#7": ("open", False)}), comentario="Pronto: https://github.com/example-org/x/pull/7")
        self.assertFalse(r["ok"])

    def test_pr_citado_so_no_comentario_do_claude(self):
        i = issue("KRN-3", comentarios=[("PR https://github.com/example-org/x/pull/7 aberto.", True)])
        self.assertFalse(self.avaliar(i, gh_falso({"example-org/x#7": ("open", False)}))["ok"])

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

    def test_dispensa_aviso_nenhum_krn979(self):
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
        f = portao_done.conferir_uma("KRN-8", desfazer=True, consulta=lambda q, **v: {"issue": i}, base_dados=self.dados,
                                     avaliar_fn=lambda *a, **k: {"ok": False, "faltas": ["pendência"]},
                                     mover_fn=lambda a, b: self.mov.append(b), comentar_fn=lambda a, t: self.comentarios.append(t))
        self.assertEqual(["pendência"], f)
        self.assertEqual(["s-ip"], self.mov)

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
