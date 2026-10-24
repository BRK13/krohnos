"""itens 2 e 3 (princípio 10): tarefa parada tem alarme e orçamento; esperar CI não gasta sessão.
Casos reais de 14/10: parada 4 h (a execução de 12:11 terminou dizendo "o agente das correções está rodando,
aguardo" e nada mais aconteceu até o orquestrador cutucar às 15:55); #682: sessão consultando o CI de 10 em 10 min."""
import datetime as dt, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from hub import incidentes, linear, parada

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 13, 0, tzinfo=UTC)
PR = "https://github.com/acme/clienteh/pull/682"


def bash(cmd):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name)
        self.trabalho = self.dados / "trabalho"
        self.log = self.dados / "saida" / "x.stream.jsonl"
        self.log.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def reg(self, inicio=T0 - dt.timedelta(hours=2)):
        return {"nome": "linear-krn-589", "inicio": inicio.isoformat(), "pid": os.getpid(), "log": str(self.log),
                "linear": {"identifier": "KRN-589", "id": "id-589", "sessao": None, "estados": {"In Progress": "s-ip"}}}

    def envelhecer(self, caminho, minutos):
        t = (T0 - dt.timedelta(minutes=minutos)).timestamp()
        os.utime(caminho, (t, t))


class Classificar(Base):
    def test_682_loop_de_ci_na_sessao(self):
        self.log.write_text("\n".join([bash(f"gh pr create ... {PR}"), bash("sleep 600"), bash(f"gh pr checks {PR}"),
                                       bash("sleep 600"), bash(f"gh pr checks {PR}")]) + "\n")
        self.envelhecer(self.log, 1)  # o stream anda (a cada 10 min): o silêncio de 45 min nunca chegaria
        self.assertEqual(("esperando_ci", PR), parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_um_gh_pr_checks_so_nao_e_loop(self):
        self.log.write_text(bash("git push") + "\n" + bash(f"gh pr checks {PR}") + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_silencio_de_45_min_e_travou(self):
        self.log.write_text(bash("python3 render.py") + "\n")
        self.envelhecer(self.log, 50)
        classe, _ = parada.classificar_rodando(self.reg(), T0, self.trabalho)
        self.assertEqual("travou", classe)

    def test_render_longo_com_arquivo_novo_na_worktree_nao_e_parada(self):  # Review Focus 3
        self.log.write_text(bash("python3 render.py") + "\n")
        self.envelhecer(self.log, 50)
        saida = self.trabalho / "linear-krn-589" / "filmes" / "saida" / "reel-A.mp4"
        saida.parent.mkdir(parents=True)
        saida.write_bytes(b"x")
        self.envelhecer(saida, 3)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_node_modules_conta_pelo_proprio_mtime_sem_descer(self):  # I4: npm ci/next build de 45+ min não é parada
        self.log.write_text(bash("npm ci") + "\n")
        self.envelhecer(self.log, 50)
        nm = self.trabalho / "linear-krn-589" / "x" / "node_modules"
        (nm / "pkg").mkdir(parents=True)
        (nm / "pkg" / "a.js").write_text("x")
        self.envelhecer(nm / "pkg" / "a.js", 1)  # arquivo fundo não é varrido
        self.envelhecer(nm / "pkg", 1)
        self.envelhecer(nm, 50)
        self.assertEqual("travou", parada.classificar_rodando(self.reg(), T0, self.trabalho)[0])
        self.envelhecer(nm, 1)  # entrada nova em node_modules muda o mtime do próprio diretório
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_consulta_antiga_seguida_de_trabalho_nao_e_espera(self):  # Codex 104569a
        self.log.write_text("\n".join([bash("sleep 600"), bash(f"gh pr checks {PR}"), bash("sleep 600"),
                                       bash(f"gh pr checks {PR}"), bash("vim hub/x.py"), bash("python3 -m pytest")]) + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_sleeps_longos_sem_gh_nao_sao_espera_de_ci(self):  # I5
        self.log.write_text("\n".join([bash("sleep 600")] * 3) + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_depurar_ci_vermelho_nao_e_espera(self):
        self.log.write_text("\n".join([bash("gh run list --limit 5"), bash("gh run view 123 --log-failed"),
                                       bash("gh run view 123 --log")]) + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_curl_de_status_nao_e_espera(self):
        self.log.write_text("\n".join([bash("curl -s https://x/status")] * 3) + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_checks_sem_espera_longa_nao_basta(self):
        self.log.write_text("\n".join([bash(f"gh pr checks {PR}")] * 3) + "\n")
        self.envelhecer(self.log, 1)
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_espera_na_fila_do_pesado_com_processo_vivo_no_trabalho_nao_e_travou(self):  #
        self.log.write_text(bash("pesado nice -n 19 npm run build") + "\n")
        self.envelhecer(self.log, 50)
        wt = self.trabalho / "linear-krn-589"
        wt.mkdir(parents=True)
        self.envelhecer(wt, 50)
        p = subprocess.Popen(["python3", "-c", "import time; time.sleep(60)", "hub.pesado"], cwd=wt)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_monitor_esperando_na_worktree_nao_e_travou(self):  # `timeout 60 tail -f /dev/null`
        self.log.write_text(bash("timeout 60 tail -f /dev/null") + "\n")
        self.envelhecer(self.log, 50)
        wt = self.trabalho / "linear-krn-589"
        wt.mkdir(parents=True)
        self.envelhecer(wt, 50)
        p = subprocess.Popen(["tail", "-f", "/dev/null"], cwd=wt)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))

    def test_processo_qualquer_na_worktree_nao_segura_sessao_travada(self):  # revisão do #601: shell, servidor, travado
        self.log.write_text(bash("python3 render.py") + "\n")
        self.envelhecer(self.log, 50)
        wt = self.trabalho / "linear-krn-589"
        wt.mkdir(parents=True)
        self.envelhecer(wt, 50)
        p = subprocess.Popen(["sleep", "60"], cwd=wt)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual("travou", parada.classificar_rodando(self.reg(), T0, self.trabalho)[0])

    def test_processo_fora_da_worktree_nao_segura_sessao_parada(self):  # o sinal é só o da própria sessão
        self.log.write_text(bash("python3 render.py") + "\n")
        self.envelhecer(self.log, 50)
        wt = self.trabalho / "linear-krn-589"
        wt.mkdir(parents=True)
        self.envelhecer(wt, 50)
        p = subprocess.Popen(["sleep", "60"], cwd=self.dados)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual("travou", parada.classificar_rodando(self.reg(), T0, self.trabalho)[0])

    def test_stream_andando_nao_varre_worktree_nem_git(self):
        self.log.write_text(bash("python3 render.py") + "\n")
        self.envelhecer(self.log, 1)
        with mock.patch("os.walk") as walk, mock.patch("subprocess.run") as git:
            self.assertIsNone(parada.classificar_rodando(self.reg(), T0, self.trabalho))
        walk.assert_not_called()
        git.assert_not_called()


class PromessaSemGancho(unittest.TestCase):
    def test_entregas_normais_com_as_palavras_nao_sao_promessa(self):
        for t in ("O app está rodando em https://app.exemplo.com.br e testado.", "O cron roda em segundo plano todo dia.",
                  "Aguardo sua aprovação para publicar."):
            self.assertFalse(parada.promessa_sem_gancho(t), t)

    def test_depois_da_promessa_a_conferencia_ainda_roda(self):
        with tempfile.TemporaryDirectory() as d:
            saida = Path(d, "r.md")
            saida.write_text("O agente está rodando e aguardo.", encoding="utf-8")
            item = {"tipo": "issue", "id": "u", "identifier": "KRN-589", "titulo": "t", "descricao": "", "url": "x",
                    "modelo": "sonnet", "repos": [], "comentarios": [], "promessas": 1,
                    "estados": {"In Review": "r", "In Progress": "p"}}
            reg = {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60, "inicio": "2026-10-14T12:00:00+00:00"}
            with mock.patch.object(linear, "gql", side_effect=OSError("x")), mock.patch.object(linear, "atividade"), \
                 mock.patch.object(linear, "_mover_no_fim"), mock.patch.object(linear, "avisar_zap", lambda *a, **k: None):
                volta = linear._concluir(linear.tarefa(item), reg, conferir_fn=lambda i, t: "o merge")
        self.assertEqual(1, volta.linear["conferencias"])
        self.assertEqual(1, volta.linear["promessas"])

    def test_bru589(self):
        self.assertTrue(parada.promessa_sem_gancho("O agente das correções no modelo está rodando. Mandei o progresso para o Linear e aguardo."))

    def test_entrega_normal(self):
        self.assertFalse(parada.promessa_sem_gancho("PR #77 mergeado e no ar. Relatório em projeto/REVISAO.md."))

    def test_pedir_algo_ao_fulano_nao_e_promessa(self):
        self.assertFalse(parada.promessa_sem_gancho("Aguardo sua resposta sobre o plano pago."))


class Vigiar(Base):
    def _vigiar(self, rodando, issues=(), agora=T0, pendente=False, consulta=None, systemctl=None, reabrir=None, comentar=None):
        paradas, comentarios, self.logs = [], [], []
        with mock.patch("hub.agenda.rodando_todas", return_value={"rodando.json": r for r in rodando}), \
             mock.patch("hub.agenda.fila", return_value=[]), \
             mock.patch("hub.linear.item_por_ident", return_value=getattr(self, "contexto", {})), \
             mock.patch("hub.agenda._chamar_orq") as self.chamar_orq, \
             mock.patch("hub.recibo._proposta_pendente", return_value=pendente), \
             mock.patch("hub.esperas.gravar", side_effect=lambda ident, pr, *a, **k: {"ident": ident, "pr": pr}) as gravar, \
             mock.patch("hub.linear.reabrir", side_effect=reabrir or (lambda ident, fala: mock.Mock(nome=f"linear-{ident.lower()}", fala=fala))), \
             mock.patch("hub.linear.comentar", side_effect=comentar or (lambda *a, **k: comentarios.append(a))), \
             mock.patch("hub.linear.mover") as self.mover, mock.patch("hub.linear.atividade"):
            out = parada.vigiar(base_dados=str(self.dados), agora=agora, trabalho=self.trabalho,
                                consulta=consulta or (lambda q, **v: {"issues": {"nodes": list(issues)}}),
                                systemctl=systemctl or (lambda *a: paradas.append(a)), log=self.logs.append)
            return out, paradas, gravar, comentarios

    def issue(self, corpo):
        return {"id": "id-589", "identifier": "KRN-589", "updatedAt": (T0 - dt.timedelta(hours=4)).isoformat(),
                "comments": {"nodes": [{"createdAt": "2026-10-14T12:11:00Z", "user": {"app": True}, "body": corpo}]},
                "team": {"id": "t", "states": {"nodes": [{"id": "s-ip", "name": "In Progress"}, {"id": "s-tr", "name": "Travada: precisa do Fulano"}]}}}

    def test_682_para_a_sessao_e_grava_a_espera(self):
        self.log.write_text("\n".join([bash(f"gh pr checks {PR}"), bash("sleep 600")] * 2) + "\n")
        self.envelhecer(self.log, 1)
        out, paradas, gravar, _ = self._vigiar([self.reg()])
        gravar.assert_called_once()
        self.assertEqual(("KRN-589", PR), gravar.call_args[0][:2])
        self.assertEqual(1, len(paradas))
        self.assertEqual("esperar_ci", parada.pedido_de_parada("linear-krn-589", str(self.dados))["acao"])
        self.assertEqual(["espera_ci_na_sessao"], [i["classe"] for i in incidentes.listar(base_dados=str(self.dados))])

    def test_nao_regrava_a_espera_de_sessao_ja_parada(self):
        self.log.write_text("\n".join([bash(f"gh pr checks {PR}"), bash("sleep 600")] * 2) + "\n")
        self.envelhecer(self.log, 1)
        self._vigiar([self.reg()])
        _, paradas, gravar, _ = self._vigiar([self.reg()])
        gravar.assert_not_called()
        self.assertEqual([], paradas)

    def test_orcamento_estourado_nao_reabre_nem_gasta_tentativa(self):
        with mock.patch("hub.agenda.em_loop", return_value="gastou US$ 70"):
            out, _, _, _ = self._vigiar([], issues=[self.issue("Aguardo.")])
        self.assertEqual([], out)
        self.assertEqual(0, json.loads((self.dados / "claude" / "parada" / "KRN-589.json").read_text())["tentativas"])

    def test_orcamento_estourado_nao_repete_consulta_nem_chamado_no_tick_seguinte(self):
        with mock.patch("hub.agenda.em_loop", return_value="gastou US$ 70"):
            self._vigiar([], issues=[self.issue("Aguardo.")])
            with mock.patch("hub.linear.item_por_ident") as ler:
                self._vigiar([], issues=[self.issue("Aguardo.")], agora=T0 + dt.timedelta(minutes=10))
        ler.assert_not_called()
        self.chamar_orq.assert_not_called()

    def test_orcamento_recebe_contexto_do_executor_e_chama_orquestrador(self):
        self.contexto = {"ultima_fala": "2026-10-14T12:00:00Z", "estado_desde": "2026-10-13T12:00:00Z"}
        def limite(nome, base, **contexto):
            return "rodou 10× sem mudar de estado" if contexto == self.contexto else None
        with mock.patch("hub.agenda.em_loop", side_effect=limite) as verificar:
            out, _, _, _ = self._vigiar([], issues=[self.issue("Aguardo.")])
        self.assertEqual([], out)
        verificar.assert_called_once_with("linear-krn-589", str(self.dados), **self.contexto)
        self.chamar_orq.assert_called_once_with("KRN-589", "rodou 10× sem mudar de estado", "parada", str(self.dados))
        self.assertEqual(0, json.loads((self.dados / "claude" / "parada" / "KRN-589.json").read_text())["tentativas"])

    def test_travou_reenfileira_uma_vez_depois_escala_uma_vez(self):
        self.log.write_text(bash("sleep 9999") + "\n")
        self.envelhecer(self.log, 50)
        self._vigiar([self.reg()])
        pedido = parada.pedido_de_parada("linear-krn-589", str(self.dados))
        self.assertEqual("reenfileirar", pedido["acao"])
        self.assertIn("sem sinal", pedido["motivo"])  # a condição muda: o motivo vai no prompt da volta
        # a volta é outra execução (outro inicio): o pedido da 1ª só vale para ela
        _, _, _, c1 = self._vigiar([self.reg(inicio=T0)], agora=T0 + dt.timedelta(hours=1))   # parou de novo: escala
        _, _, _, c2 = self._vigiar([self.reg(inicio=T0 + dt.timedelta(minutes=10))], agora=T0 + dt.timedelta(hours=2))  # e não escala de novo
        self.assertEqual((1, 0), (len(c1), len(c2)))
        est = json.loads((self.dados / "claude" / "parada" / "KRN-589.json").read_text())
        self.assertIsNotNone(est["escalada_em"])
        self.assertEqual(2, est["tentativas"])

    def test_stop_que_falhou_nao_conta_como_2a_parada(self):  # C1
        self.log.write_text(bash("sleep 9999") + "\n")
        self.envelhecer(self.log, 50)
        self._vigiar([self.reg()], systemctl=lambda *a: False)
        self.assertTrue(any("stop" in l for l in self.logs), self.logs)
        _, _, _, c = self._vigiar([self.reg()], agora=T0 + dt.timedelta(minutes=10), systemctl=lambda *a: False)
        self.assertEqual([], c)
        est = json.loads((self.dados / "claude" / "parada" / "KRN-589.json").read_text())
        self.assertEqual((1, None), (est["tentativas"], est["escalada_em"]))
        # Codex 4edd600: o stop que falhou é refeito no tick seguinte (sem contar 2ª parada) até pegar
        _, paradas, _, _ = self._vigiar([self.reg()], agora=T0 + dt.timedelta(minutes=20))
        self.assertEqual(1, len(paradas))
        self.assertTrue(parada.pedido_de_parada("linear-krn-589", str(self.dados))["parado"])
        _, paradas, _, _ = self._vigiar([self.reg()], agora=T0 + dt.timedelta(minutes=30))
        self.assertEqual([], paradas)

    def test_escalada_que_falha_no_linear_tenta_de_novo(self):  # Codex 4edd600
        def comentar(*a, **k):
            raise OSError("Linear fora")
        fala = "A voz depende do Fulano: precisa da assinatura."
        self._vigiar([], issues=[self.issue(fala)], comentar=comentar)
        arq = self.dados / "claude" / "parada" / "KRN-589.json"
        self.assertIsNone(json.loads(arq.read_text())["escalada_em"])
        _, _, _, c = self._vigiar([], issues=[self.issue(fala)], agora=T0 + dt.timedelta(minutes=10))
        self.assertEqual(1, len(c))
        self.assertIsNotNone(json.loads(arq.read_text())["escalada_em"])

    def test_systemctl_usa_o_barramento_do_usuario(self):  # C1: o cron não tem XDG_RUNTIME_DIR
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stderr=b"Failed to connect to bus")) as run:
            self.assertFalse(parada._systemctl("stop", "tarefa-x-*.scope"))
        self.assertEqual(f"/run/user/{os.getuid()}", run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"])

    def test_escala_sessao_rodando_cria_o_estado_travada(self):  # X1
        self.log.write_text(bash("sleep 9999") + "\n")
        self.envelhecer(self.log, 50)
        self._vigiar([self.reg()])

        def consulta(q, **v):
            if "workflowStateCreate" in q:
                return {"workflowStateCreate": {"workflowState": {"id": "s-novo"}}}
            if "issue(id" in q:
                return {"issue": {"team": {"id": "t", "states": {"nodes": [{"id": "s-ip", "name": "In Progress"}]}}}}
            return {"issues": {"nodes": []}}
        self._vigiar([self.reg(inicio=T0)], agora=T0 + dt.timedelta(hours=1), consulta=consulta)
        self.mover.assert_called_once_with("id-589", "s-novo")

    def test_reabrir_que_falha_nao_gasta_a_tentativa(self):  # I7
        def reabrir(ident, fala):
            raise OSError("fila")
        out, _, _, _ = self._vigiar([], issues=[self.issue("Aguardo.")], reabrir=reabrir)
        self.assertEqual([], out)
        self.assertFalse((self.dados / "claude" / "parada" / "KRN-589.json").exists())

    def test_bru589_sessao_morreu_issue_em_progresso_sem_nada_rodando(self):
        out, _, _, _ = self._vigiar([], issues=[self.issue("O agente das correções está rodando. Aguardo.")])
        self.assertEqual(["linear-krn-589"], [t.nome for t in out])
        self.assertIn("nada roda depois que a sessão termina", out[0].fala)

    def test_inc391_consulta_recente_preserva_data_de_medicao(self):
        # Linear ordena comentários do mais novo para o mais antigo:
        # last: 5 traz os primeiros relatos históricos, sem a espera atual.
        antiga = self.issue("PR antigo; aguardo CI.")
        recente = self.issue("Medição em uma semana. voltar-em: 2026-10-29")
        def consulta(q, **v):
            return {"issues": {"nodes": [recente if "comments(first: 5)" in q else antiga]}}
        out, _, _, comentarios = self._vigiar([], consulta=consulta)
        self.assertEqual(out, [])
        self.assertEqual(comentarios, [])
        self.assertFalse((self.dados / "claude" / "parada" / "KRN-589.json").exists())

    def test_inc392_gancho_recebe_pr_recente_em_vez_da_coleta_antiga(self):
        # last: 5 ainda descrevia o bench e a coleta pendente,
        # enquanto o recibo recente já apontava o PR do resultado.
        antiga = self.issue("O bench roda no contêiner; falta coletar o resultado.")
        recente = self.issue(f"Resultado entregue em {PR}; aguardo o local-ci.")
        def consulta(q, **v):
            return {"issues": {"nodes": [recente if "comments(first: 5)" in q else antiga]}}
        from hub.retomada import gancho_mecanico
        def gancho(ident, texto, **kwargs):
            return gancho_mecanico(
                ident, texto, gh=lambda _: {"state": "open"},
                gravar=lambda *a, **k: {"pr": a[1]}, **kwargs)
        with mock.patch("hub.agenda.em_loop", return_value=None), \
             mock.patch("hub.retomada.gancho_mecanico", side_effect=gancho) as mecanico, \
             mock.patch("hub.esperas.registrar_sem_ia") as registrar:
            out, _, _, comentarios = self._vigiar([], consulta=consulta)
        self.assertEqual([], out)
        self.assertEqual([], comentarios)
        self.assertIn(PR, mecanico.call_args.args[1])
        registrar.assert_called_once()
        self.assertFalse((self.dados / "claude" / "parada" / "KRN-589.json").exists())

    def test_proposta_pendente_e_a_vez_de_outro_nao_escala(self):
        out, _, _, coment = self._vigiar([], issues=[self.issue("Aguardo.")], pendente=True)
        self.assertEqual(([], []), (out, coment))
        self.assertFalse((self.dados / "claude" / "parada" / "KRN-589.json").exists())

    def test_esperando_o_fulano_escala_uma_vez_sem_reenfileirar(self):
        i = self.issue("Isso depende do Fulano: assinar o plano pago.")
        out, _, _, c1 = self._vigiar([], issues=[i])
        _, _, _, c2 = self._vigiar([], issues=[i], agora=T0 + dt.timedelta(hours=1))
        self.assertEqual(([], 1, 0), (out, len(c1), len(c2)))


class Concluir(unittest.TestCase):
    def concluir(self, texto, titulo="t"):
        with tempfile.TemporaryDirectory() as d:
            saida = Path(d, "r.md")
            saida.write_text(texto, encoding="utf-8")
            item = {"tipo": "issue", "id": "u", "identifier": "KRN-589", "titulo": titulo, "descricao": "", "url": "x",
                    "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {"In Review": "r", "In Progress": "p"}}
            reg = {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60, "inicio": "2026-10-14T12:00:00+00:00"}
            with mock.patch.object(linear, "gql", side_effect=OSError("sem rede")), \
                 mock.patch.object(linear, "atividade"), mock.patch.object(linear, "_mover_no_fim"), \
                 mock.patch.object(linear, "avisar_zap", lambda *a, **k: None):
                return linear._concluir(linear.tarefa(item), reg, conferir_fn=lambda i, t: "")

    def test_promessa_sem_gancho_volta_para_a_fila(self):
        volta = self.concluir("O agente das correções no modelo está rodando. Mandei o progresso para o Linear e aguardo.")
        self.assertIn("nada roda depois que a sessão termina", volta.linear["comentarios"][-1])

    def test_entrega_normal_nao_volta(self):
        self.assertIsNone(self.concluir("PR #77 mergeado e no ar."))

    def test_973_issue_de_dado_sem_prova_volta_para_a_fila(self):
        volta = self.concluir("PR aberto, o código de coleta está pronto.", titulo="Coletar os acórdãos do STJ")
        self.assertIn("Portão de Done", volta.linear["comentarios"][-1])
        self.assertEqual(volta.linear["portoes"], 1)

    def test_973_issue_de_dado_com_prova_passa(self):
        prova = ("PR aberto.\n## Prova de execução\n- Antes: 0 acórdãos. Depois: 900.\n- Agendamento: cron instalado, diário.")
        self.assertIsNone(self.concluir(prova, titulo="Coletar os acórdãos do STJ"))


if __name__ == "__main__":
    unittest.main()
