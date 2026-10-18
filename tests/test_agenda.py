import contextlib
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import agenda, conta, tarefas as T

BRT = T.BRT
_SEM_REDE = []


def setUpModule():
    """Teste nunca fala com o Linear nem com o WhatsApp de verdade (em 08/10 um teste postou comentário real na
    e na): gql sem mock quebra o teste, o app fica sem token e o aviso do zap vai para o vazio."""
    def proibido(*a, **k):
        raise AssertionError("teste tentou falar com o Linear de verdade: faça mock de linear.gql")
    _SEM_REDE.extend([mock.patch("hub.debugger.vigiar"), mock.patch("hub.linear.gql", proibido), mock.patch("hub.linear.token_app", lambda **k: None),
                      mock.patch("hub.conta.avisar", lambda *a, **k: None)])
    for p in _SEM_REDE:
        p.start()


def tearDownModule():
    for p in _SEM_REDE:
        p.stop()


def tarefa(nome, quando="0 8 * * *", prioridade=2, timeout="10m", externo="false", avisar="false"):
    return T.parse(f"---\nquando: {quando}\nprioridade: {prioridade}\nmodelo: haiku\ntimeout: {timeout}\n"
                   f"externo: {externo}\navisar: {avisar}\ndescricao: teste\n---\nFaça {nome}.\n", nome)


@contextlib.contextmanager
def livre():
    yield True


@contextlib.contextmanager
def ocupada():
    yield False


class AgendaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.agente = Path(tmp.name) / "agente-fila"
        self.agente.mkdir()
        self.tarefas = {t.nome: t for t in (
            tarefa("resumo", "0 8 * * *", 1), tarefa("seo", "0 8 * * *", 2, "2h"), tarefa("leve", "5 8 * * *", 3),
            tarefa("na-mao", "manual", 2))}

    def test_enfileira_o_que_venceu_uma_vez(self):
        t0 = dt.datetime(2026, 10, 8, 7, 58, tzinfo=BRT)
        self.assertEqual(agenda.agendar(self.tarefas, t0, self.dados), [])
        novas = agenda.agendar(self.tarefas, t0 + dt.timedelta(minutes=5), self.dados)
        self.assertEqual(sorted(novas), ["resumo", "seo"])
        novas = agenda.agendar(self.tarefas, t0 + dt.timedelta(minutes=10), self.dados)
        self.assertEqual(novas, ["leve"])
        self.assertEqual([i["nome"] for i in agenda.fila(self.dados)], ["resumo", "seo", "leve"])
        # o mesmo horário de novo (cron atrasado) não duplica
        self.assertEqual(agenda.agendar(self.tarefas, t0 + dt.timedelta(minutes=10), self.dados), [])

    def test_desligada_nao_dispara_sozinha_mas_enfileira_na_mao(self):
        t0 = dt.datetime(2026, 10, 8, 7, 58, tzinfo=BRT)
        agenda.agendar(self.tarefas, t0, self.dados)
        with mock.patch("hub.rotinas.desligadas", return_value={"tarefa:seo", "resumo"}):  # "resumo" sem prefixo é rotina
            novas = agenda.agendar(self.tarefas, t0 + dt.timedelta(minutes=5), self.dados)
        self.assertEqual(novas, ["resumo"])
        self.assertTrue(agenda.enfileirar(self.tarefas["seo"], "manual", self.dados))  # rodar agora continua possível
        self.assertIn("seo", [i["nome"] for i in agenda.fila(self.dados)])

    def test_sem_a_chave_comportamento_de_hoje(self):
        t0 = dt.datetime(2026, 10, 8, 7, 58, tzinfo=BRT)
        agenda.agendar(self.tarefas, t0, self.dados)
        with mock.patch("hub.rotinas.config.get", return_value=None):  # nem a chave, nem o padrão
            novas = agenda.agendar(self.tarefas, t0 + dt.timedelta(minutes=5), self.dados)
        self.assertEqual(sorted(novas), ["resumo", "seo"])

    def test_vence_depois_de_24h(self):
        t0 = dt.datetime(2026, 10, 8, 8, 2, tzinfo=BRT)
        agenda.enfileirar(self.tarefas["seo"], "teste", self.dados, t0 - dt.timedelta(hours=25))
        agenda.agendar(self.tarefas, t0, self.dados)
        self.assertNotIn("seo", [i["nome"] for i in agenda.fila(self.dados)])

    def test_regras_de_vez(self):
        dia = dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT)
        noite = dt.datetime(2026, 10, 8, 23, 0, tzinfo=BRT)
        seo, leve, resumo = self.tarefas["seo"], self.tarefas["leve"], self.tarefas["resumo"]
        self.assertIsNone(agenda.por_que_espera(resumo, dia, agente=True))
        self.assertIn("WhatsApp", agenda.por_que_espera(leve, dia, agente=True))
        self.assertIsNone(agenda.por_que_espera(leve, dia, agente=False))
        with mock.patch.object(agenda, "maquina_folgada", lambda c=None: False):
            self.assertIn("pesada", agenda.por_que_espera(seo, dia, agente=False))
        with mock.patch.object(agenda, "maquina_folgada", lambda c=None: True):
            self.assertIsNone(agenda.por_que_espera(seo, dia, agente=False))  # VPS folgada: pesada roda de dia
        self.assertIsNone(agenda.por_que_espera(seo, noite, agente=True))

    @mock.patch.object(agenda, "maquina_folgada", lambda c=None: False)
    def test_proximo_pula_quem_espera(self):
        dia = dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT)
        for nome in ("leve", "seo"):
            agenda.enfileirar(self.tarefas[nome], "teste", self.dados, dia)
        self.assertEqual(agenda.proximo(self.tarefas, dia, False, self.dados).nome, "leve")  # seo é pesada
        self.assertIsNone(agenda.proximo(self.tarefas, dia, True, self.dados))
        self.assertEqual(agenda.proximo(self.tarefas, dia.replace(hour=23), True, self.dados).nome, "seo")

    def test_proximo_nao_pega_o_que_ja_esta_rodando(self):
        # resultado de proposta reabre a issue com a sessão ainda rodando: não pode abrir a segunda
        dia = dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT)
        agenda.enfileirar(self.tarefas["leve"], "proposta feita", self.dados, dia)
        agenda._gravar_json(agenda._rodando_arq(self.dados, n=2), {"nome": "leve", "pid": os.getpid()})
        self.assertIsNone(agenda.proximo(self.tarefas, dia, False, self.dados))
        agenda._rodando_arq(self.dados, n=2).unlink()
        self.assertEqual(agenda.proximo(self.tarefas, dia, False, self.dados).nome, "leve")

    def test_agente_ocupado(self):
        self.assertFalse(agenda.agente_ocupado(self.agente))
        (self.agente / ".00001-planejar.json.tmp").write_text("{}")
        self.assertFalse(agenda.agente_ocupado(self.agente))
        (self.agente / "00001-planejar.json").write_text("{}")
        self.assertTrue(agenda.agente_ocupado(self.agente))
        self.assertFalse(agenda.agente_ocupado(Path(self.dados) / "nao-existe"))

    def test_despachar_um_por_vez(self):
        noite = dt.datetime(2026, 10, 8, 23, 0, tzinfo=BRT)
        for nome in ("leve", "seo", "resumo"):
            agenda.enfileirar(self.tarefas[nome], "teste", self.dados, noite)
        rodou = []
        ex = lambda t, base_dados=None: rodou.append(t.nome)  # noqa: E731
        self.assertEqual(agenda.despachar(self.tarefas, noite, self.dados, ocupada, ex, self.agente), "conta ocupada")
        self.assertEqual(agenda.despachar(self.tarefas, noite, self.dados, livre, ex, self.agente), "rodou resumo")
        self.assertEqual(agenda.despachar(self.tarefas, noite, self.dados, livre, ex, self.agente), "rodou seo")
        conta.pausar("resets 11pm", "teste", self.dados, noite, lambda *a, **k: None)
        self.assertEqual(agenda.despachar(self.tarefas, noite, self.dados, livre, ex, self.agente),
                         "pausada por limite de uso")
        self.assertEqual(rodou, ["resumo", "seo"])


class ExecutarTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.saida = os.path.join(tmp.name, "saida")
        self.avisos = []
        p = mock.patch.object(agenda, "montar_prompt", lambda t, base_dados=None: "prompt")
        p.start()
        self.addCleanup(p.stop)

    def avisar(self, texto, agora_=False):
        self.avisos.append((texto, agora_))

    def rodar(self, t, resposta, quando):
        with mock.patch.object(conta, "agora", lambda: quando):
            return agenda.executar(t, base_dados=self.dados, chamar=lambda *a: resposta, avisar_fn=self.avisar,
                                   saida_dir=self.saida)

    def test_ok_registra_uso_e_avisa_de_dia(self):
        j = {"result": "feito", "is_error": False, "total_cost_usd": 0.12, "num_turns": 3,
             "usage": {"input_tokens": 10, "output_tokens": 20}}
        reg = self.rodar(tarefa("resumo", prioridade=1, avisar="true"), (0, j, ""), dt.datetime(2026, 10, 8, 11, 0, tzinfo=dt.timezone.utc))
        self.assertTrue(reg["ok"])
        self.assertEqual(reg["custo_usd"], 0.12)
        self.assertEqual(reg["tokens"]["saida"], 20)
        linha = json.loads(Path(self.dados, "claude", "uso.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(linha["nome"], "resumo")
        self.assertEqual(Path(reg["saida"]).read_text(encoding="utf-8"), "feito")
        self.assertEqual(len(self.avisos), 1)
        self.assertTrue(self.avisos[0][1])

    def test_de_noite_nao_avisa(self):
        reg = self.rodar(tarefa("seo"), (0, {"result": "ok"}, ""), dt.datetime(2026, 10, 8, 4, 0, tzinfo=dt.timezone.utc))
        self.assertTrue(reg["ok"])
        self.assertEqual(self.avisos, [])

    def test_limite_pausa_e_volta_para_fila(self):
        j = {"result": "You've hit your session limit · resets 3pm (UTC)", "is_error": True}
        t0 = dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.timezone.utc)
        reg = self.rodar(tarefa("seo"), (1, j, ""), t0)
        self.assertFalse(reg["ok"])
        self.assertTrue(reg["limite"])
        p = conta.pausa_ativa(self.dados, t0)
        self.assertEqual(p["ate"], "2026-10-08T15:02:00+00:00")
        self.assertEqual([i["nome"] for i in agenda.fila(self.dados)], ["seo"])
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("cota", self.avisos[0][0])

    def test_falha_comum_nao_pausa(self):
        reg = self.rodar(tarefa("seo"), (1, None, "Traceback: boom"), dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.timezone.utc))
        self.assertFalse(reg["ok"])
        self.assertFalse(reg["limite"])
        self.assertIsNone(conta.pausa_ativa(self.dados))
        self.assertEqual(self.avisos, [])  # sem avisar:true a falha vai para o resumo das 8h, não para o WhatsApp

    def test_avisar_true_manda_falha(self):
        self.rodar(tarefa("resumo", avisar="true"), (1, None, "boom"), dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.timezone.utc))
        self.assertIn("FALHOU", self.avisos[0][0])


    def test_rodar_e_concluir_grava_a_telemetria(self):  # (F2): toda execução vira linha de `execucao`
        from hub import telemetria
        reg = {"inicio": "2026-10-17T12:00:00+00:00", "fim": "2026-10-17T12:05:00+00:00", "conta": "2", "nome": "linear-krn-7",
               "ok": True, "custo_usd": 0.5, "tipo": "conferencia", "duracao_s": 300.0,
               "motivo_volta": "voltou da espera do CI (https://github.com/example-org/x/pull/9)"}
        agenda._rodar_e_concluir(tarefa("resumo"), self.dados, lambda t, base_dados=None: reg)
        con = telemetria.conectar(self.dados)
        linha = con.execute("SELECT issue, resultado, pr, retrabalho, custo_usd FROM execucao").fetchall()
        con.close()
        self.assertEqual(linha, [("KRN-7", "ok", "https://github.com/example-org/x/pull/9", 1, 0.5)])
        with mock.patch("hub.telemetria.registrar", side_effect=RuntimeError("disco")):  # nunca derruba a entrega
            agenda._rodar_e_concluir(tarefa("resumo"), self.dados, lambda t, base_dados=None: reg)


class InterrompidaTest(ExecutarTest):
    """o ramo `interrompida` lê o pedido de parada do alarme (só o desta execução)."""
    T0 = dt.datetime(2026, 10, 14, 13, 0, tzinfo=dt.timezone.utc)

    def issue(self):
        from hub import linear
        return linear.tarefa({"tipo": "issue", "id": "u", "identifier": "KRN-5", "titulo": "t", "descricao": "", "url": "x",
                              "modelo": "sonnet", "repos": [], "comentarios": ["Ana: oi"], "estados": {}})

    def interromper(self, acao=None, inicio="agora"):
        from hub import linear, parada
        t = self.issue()
        if acao:
            parada.parar(t.nome, acao, "sem sinal há 50 min", self.dados, systemctl=lambda *a: None,
                         inicio=conta.iso(self.T0) if inicio == "agora" else inicio)
        with mock.patch.object(linear, "reler"):
            self.rodar(t, (143, None, ""), self.T0)
        return t

    def test_esperar_ci_e_nada_nao_voltam_para_a_fila(self):
        for acao in ("esperar_ci", "nada"):
            self.interromper(acao)
            self.assertEqual([], agenda.fila(self.dados))

    def test_reenfileirar_volta_com_o_motivo_no_prompt(self):
        self.interromper("reenfileirar")
        item = agenda.fila(self.dados)[0]
        self.assertIn("sem sinal há 50 min", item["linear"]["comentarios"][-1])

    def test_sem_pedido_volta_com_o_resumo_da_ultima_rodada(self):
        self.interromper()  # a 1ª morte sem o alarme volta sozinha, com o resumo no prompt
        item = agenda.fila(self.dados)[0]
        self.assertEqual("Ana: oi", item["linear"]["comentarios"][0])
        self.assertIn("a execução anterior morreu (sinal (código 143)", item["linear"]["comentarios"][-1])

    def test_pedido_velho_de_outra_execucao_nao_derruba_a_tarefa(self):
        self.interromper("esperar_ci", inicio="2026-10-14T09:00:00+00:00")
        self.assertEqual(["linear-krn-5"], [i["nome"] for i in agenda.fila(self.dados)])


class PromptTest(unittest.TestCase):
    def test_prompt_tem_repos_propostas_e_sites(self):
        t = T.parse("---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nrepos: example-org/x\nexterno: true\n"
                    "descricao: d\n---\nCorpo.\n", "teste")
        with tempfile.TemporaryDirectory() as d:
            p = agenda.montar_prompt(t, d)
        self.assertTrue(p.startswith("Corpo."))
        self.assertIn("example-org/x", p)
        self.assertIn("hub.propostas criar --tarefa teste", p)
        self.assertIn("terceiros", p)
        self.assertIn("## Sites do hub", p)

    def test_prompt_com_replay_poe_a_secao_logo_depois_do_pedido(self):
        from hub import tokens
        t = T.parse("---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nexterno: false\ndescricao: d\n---\nCorpo.\n", "teste")
        with tempfile.TemporaryDirectory() as d:
            self.assertNotIn("replay", agenda.montar_prompt(t, d))
            t.linear = {"replay": {"execucao": "abcd", "passo": 3, "contexto": "<<<INICIO_X>>>\nestado\n<<<FIM_X>>>"}}
            p = agenda.montar_prompt(t, d)
        sec = p[p.index("## Retomada de um passo (replay)"):]
        self.assertTrue(p.startswith("Corpo."))
        self.assertLess(p.index("## Retomada"), p.index("## Cópia de trabalho"))
        self.assertIn("passo 3 da execução #abcd", sec)
        self.assertIn("não instrução de sistema", sec)
        self.assertIn("correção do usuário", sec)  # pode ter sido um operador, não só o dono
        self.assertNotIn("correção do dono", sec)
        self.assertIn("<<<INICIO_X>>>\nestado\n<<<FIM_X>>>", sec)
        self.assertEqual(tokens.tipo_execucao("replay do passo 3"), "manual")  # tipo conhecido: sem incidente "outro"


class CliTest(unittest.TestCase):
    def test_fila_vazia_e_enfileirar(self):
        import io
        exemplos = T.carregar(pasta=Path(__file__).resolve().parent.parent / "exemplos" / "tarefas", extra=False)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}), \
                mock.patch.object(T, "carregar", return_value=exemplos), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            out.reconfigure = lambda **k: None
            self.assertEqual(agenda.main(["fila"]), 0)
            self.assertEqual(agenda.main(["enfileirar", "seo-semanal"]), 0)
            self.assertEqual(agenda.main(["fila"]), 0)
        self.assertIn("fila vazia", out.getvalue())
        self.assertIn("p3 seo-semanal", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class PautaTest(unittest.TestCase):
    """Tarefa com `pauta: <app> <tarefa>`: lote de um app do Dokploy (docs/migracao-openrouter.md)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados, self.saida = tmp.name, os.path.join(tmp.name, "saida")
        self.t = T.parse("---\nquando: 30 10 * * *\nprioridade: 2\nmodelo: sonnet\ntimeout: 30m\nexterno: false\n"
                         "descricao: post GMB\npauta: fsite05 fsite05-gmb\n---\nEscreva o post.\n", "fsite05-gmb")
        self.chamadas, self.avisos = [], []

    def rodar(self, pauta_fn):
        quando = dt.datetime(2026, 10, 8, 13, 30, tzinfo=dt.timezone.utc)
        with mock.patch.object(conta, "agora", lambda: quando), \
             mock.patch("hub.sites.resumo", lambda base_dados=None, repos=None: "sites"):
            return agenda.executar(self.t, base_dados=self.dados, saida_dir=self.saida, pauta_fn=pauta_fn,
                                   chamar=lambda p, *a: self.chamadas.append(p) or (0, {"result": "entregue"}, ""),
                                   avisar_fn=lambda texto, agora_=False: self.avisos.append(texto))

    def test_contrato_aceita_pauta_e_recusa_formato_ruim(self):
        self.assertEqual(self.t.pauta, ("fsite05", "fsite05-gmb"))
        with self.assertRaises(T.TarefaInvalida):
            T.parse("---\nquando: manual\nprioridade: 2\nmodelo: haiku\ntimeout: 5m\nexterno: false\n"
                    "descricao: x\npauta: fsite05\n---\nx\n", "x")

    def test_sem_pauta_nao_acorda_o_claude(self):
        self.assertIsNone(self.rodar(lambda t, pasta, inicio: (None, None)))
        self.assertEqual((self.chamadas, self.avisos), ([], []))
        self.assertFalse(Path(self.dados, "claude", "uso.jsonl").exists())

    def test_com_pauta_o_prompt_leva_o_caminho_e_a_entrega(self):
        reg = self.rodar(lambda t, pasta, inicio: (os.path.join(pasta, "p.json"), None))
        self.assertTrue(reg["ok"])
        [prompt] = self.chamadas
        self.assertIn("p.json", prompt)
        self.assertIn("app.sh fsite05 entrega fsite05-gmb", prompt)

    def test_app_falhou_registra_falha_sem_chamar(self):
        reg = self.rodar(lambda t, pasta, inicio: (None, "HTTP 503"))
        self.assertFalse(reg["ok"])
        self.assertIn("HTTP 503", reg["erro"])
        self.assertEqual(self.chamadas, [])
        self.assertEqual(self.avisos, [])

    def test_rate_limit_do_github_na_pauta_nao_pausa_a_conta(self):
        """17/10: o 403 de rate limit do GitHub na pauta pausou 3 contas e avisou o dono como cota do Claude."""
        reg = self.rodar(lambda t, pasta, inicio: (None, 'HTTP 500 {"erro":"GitHub API 403: API rate limit exceeded"}'))
        self.assertFalse(reg["ok"])
        self.assertEqual(self.chamadas, [])
        self.assertEqual(self.avisos, [])
        self.assertFalse(list(Path(self.dados, "claude").glob("pausa*.json")))


class VariasContasTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.agente = Path(tmp.name) / "agente-fila"
        self.agente.mkdir()
        (self.agente / "00001-pedido.json").write_text("{}")  # WhatsApp ocupado
        from hub import linear
        # pedidos do Linear (prioridade 2 para a regra do WhatsApp valer): "a" e "b" no asite01, "c" na fsite05
        mk = lambda nome, repo: linear.tarefa({"tipo": "issue", "id": nome, "identifier": f"KRN-{nome}", "titulo": "t",  # noqa: E731
                                               "descricao": "", "url": "x", "modelo": "haiku", "repos": [repo],
                                               "comentarios": [], "estados": {}})
        self.tarefas = {t.nome: t for t in (mk("a", "example-org/asite01"), mk("b", "example-org/asite01"), mk("c", "example-org/fsite05"))}
        for t in self.tarefas.values():
            agenda.enfileirar(t, "teste", self.dados, dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT))
        self.dia = dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT)

    def test_conta_2_ignora_whatsapp_e_pula_repo_ocupado(self):
        rodou = []
        ex = lambda t, base_dados=None: rodou.append(t.nome)  # noqa: E731
        with mock.patch.object(agenda.linear, "tarefa", lambda it: next(t for t in self.tarefas.values() if t.linear["id"] == it["id"])):
            # a conta 1 está rodando um pedido no repo do asite01
            agenda._gravar_json(Path(self.dados, "claude", "rodando.json"),
                                {"nome": "linear-krn-z", "inicio": "", "pid": os.getpid(), "repos": ["example-org/asite01"]})
            with mock.patch.dict(os.environ, {"CLAUDE_CONTA": "2"}):
                self.assertEqual(conta.caminho_trava(), "/var/lock/claude-conta-2.lock")
                # mesmo repo que a conta 1 está usando: roda junto (cada tarefa na sua worktree); ordem não importa
                for _ in range(3):
                    self.assertTrue(agenda.despachar(self.tarefas, self.dia, self.dados, livre, ex, self.agente).startswith("rodou"))
                conta.pausar("resets 11pm", "teste", self.dados, self.dia, lambda *a, **k: None)
                self.assertIsNotNone(conta.pausa_ativa(self.dados, self.dia))
        self.assertIsNone(conta.pausa_ativa(self.dados, self.dia))  # a pausa da 2 não para a 1
        self.assertFalse(agenda.tirar("linear-krn-c", self.dados))  # já saiu: ninguém pega de novo
        self.assertEqual(sorted(rodou), ["linear-krn-a", "linear-krn-b", "linear-krn-c"])

    def test_rotina_de_fundo_nao_trava_pedido_do_linear(self):
        self.assertEqual(agenda._repos_de(T.parse("---\nquando: manual\nprioridade: 2\nmodelo: haiku\ntimeout: 10m\n"
                                                  "repos: example-org/asite01\nexterno: false\ndescricao: t\n---\nx\n", "g")), [])


class LinearTest(unittest.TestCase):
    ISSUE = {"id": "u1", "identifier": "KRN-7", "title": "Arrumar rodapé", "description": "O rodapé quebra no celular.",
             "url": "https://linear.app/x/issue/KRN-7", "labels": {"nodes": [{"name": "opus"}]},
             "project": {"name": "asite01", "description": "SaaS dos cartórios\nrepo: example-org/cliente-c"},
             "comments": {"nodes": [{"body": "use o tema novo", "createdAt": "2026-10-08T10:00:00Z", "user": {"name": "Ana"}}]},
             "team": {"states": {"nodes": [{"id": s, "name": s} for s in ("Todo", "In Progress", "In Review", "Backlog")]}}}

    def setUp(self):  # o estado do webhook vive em /var/lib: sem isto o puxar pularia a varredura conforme a hora da última
        from hub import linear_uso
        p = mock.patch.object(linear_uso, "plano_tick", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def test_criar_por_repo(self):
        from hub import linear
        projetos = {"projects": {"nodes": [{"name": "Cliente C", "description": "x\nrepo: example-org/cliente-c"},
                                           {"name": "Cliente A", "description": "repo: example-org/cliente-a-v2"}]}}
        criadas = []
        with mock.patch.object(linear, "gql", lambda q, app=False, **v: projetos), \
                mock.patch.object(linear, "criar_issue", lambda *a: criadas.append(a) or "KRN-9 u"), \
                mock.patch("sys.stdin", mock.Mock(buffer=mock.Mock(read=lambda: "desc".encode()))), \
                mock.patch("sys.stdout", mock.Mock()):
            self.assertEqual(linear.projeto_do_repo("cliente-a-v2"), "Cliente A")
            self.assertEqual(linear.projeto_do_repo("example-org/Cliente-C"), "Cliente C")
            with self.assertRaises(SystemExit):
                linear.projeto_do_repo("example-org/nada")
            linear.main(["criar", "--repo", "example-org/cliente-c", "--titulo", "T", "--estado", "Todo", "--prioridade", "1"])
        self.assertEqual(criadas, [("Cliente C", "T", "desc", "Todo", [], 1)])

    def test_sem_app_issue_vira_tarefa_por_comentario(self):
        from hub import linear
        movidas, comentarios = [], []
        with tempfile.TemporaryDirectory() as d,                 mock.patch.object(linear, "chave", lambda: "k"), mock.patch.object(linear, "sessoes_ok", lambda: False),                 mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issues": {"nodes": [self.ISSUE]}, "projectUpdates": {"nodes": []}}),                 mock.patch.object(linear, "comentar", lambda i, txt, campo="issueId": comentarios.append(txt)),                 mock.patch.object(linear, "mover", lambda i, s, c=None: movidas.append((s, c))), mock.patch.object(linear, "conferir", lambda *a, **k: ""):
            self.assertEqual(agenda.puxar_linear({}, d), ["linear-krn-7"])
            t = agenda.da_fila(d)["linear-krn-7"]  # remontada só do item da fila
            self.assertEqual((t.modelo, t.repos, t.prioridade), ("opus", ["example-org/cliente-c"], 1))
            self.assertIn("use o tema novo", t.prompt)
            self.assertIn("Fixes KRN-7", t.prompt)
            saida = Path(d, "r.md")
            saida.write_text("PR aberto: https://github.com/example-org/cliente-c/pull/9", encoding="utf-8")
            linear.concluir(t, {"ok": True, "saida": str(saida), "conta": "2", "duracao_s": 600})
            linear.concluir(t, {"ok": False, "limite": True})  # limite: não mexe
            linear.concluir(t, {"ok": False, "saida": None, "erro": "boom", "duracao_s": 60})
        self.assertIn("Na fila", comentarios[0])
        self.assertEqual([m[0] for m in movidas], ["In Progress", "In Review", "Backlog"])
        self.assertIn("pull/9", movidas[1][1])
        self.assertIn("boom", movidas[2][1])

    def test_recibo_enfileira_o_que_voltou_de_done(self):  #
        from hub import linear, recibo
        t = T.Tarefa("linear-krn-667", "manual", 1, "opus", 600, [], False, "x", "volte")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "chave", lambda: "k"), \
                mock.patch.object(linear, "sessoes_ok", lambda: False), mock.patch.object(linear, "conferir", lambda *a, **k: ""), \
                mock.patch.object(recibo, "vigiar", lambda **k: [t]), \
                mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issues": {"nodes": []}, "projectUpdates": {"nodes": []}}):
            self.assertEqual(agenda.puxar_linear({}, d), ["linear-krn-667"])
            self.assertTrue(Path(d, "claude", "fila", "linear-krn-667.json").exists())

    def test_recibo_recebe_ocupadas_e_respeita_o_orcamento(self):  # I2, M2
        from hub import linear, main_vermelho, recibo
        t = T.Tarefa("linear-krn-667", "manual", 1, "opus", 600, [], False, "x", "volte")
        visto = {}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "chave", lambda: "k"), \
                mock.patch.object(linear, "sessoes_ok", lambda: False), mock.patch.object(linear, "conferir", lambda *a, **k: ""), \
                mock.patch.object(recibo, "vigiar", lambda **k: visto.update(k) or [t]), \
                mock.patch.object(main_vermelho, "vigiar_no_tick", lambda **k: None), \
                mock.patch.object(agenda, "rodando_todas", lambda b=None: {"r.json": {"nome": "linear-krn-9"}}), \
                mock.patch.object(agenda, "em_loop", lambda *a, **k: "gastou US$ 70"), \
                mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issues": {"nodes": []}, "projectUpdates": {"nodes": []}}):
            self.assertEqual(agenda.puxar_linear({}, d), [])
            self.assertFalse(Path(d, "claude", "fila", "linear-krn-667.json").exists())
        self.assertIn("linear-krn-9", visto["ocupadas"])

    def test_teto_de_gasto_por_issue(self):
        # 3 rodadas de US$ 25 em 24 h param a issue mesmo com fala nova do Ana
        with tempfile.TemporaryDirectory() as d:
            for h in (1, 2, 3):
                conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=h)), "nome": "linear-krn-9",
                                 "custo_usd": 25.0, "ok": True}, d)
            motivo = agenda.em_loop("linear-krn-9", d, ultima_fala=conta.iso(conta.agora()))
            self.assertIn("consumiu 100%", motivo)
            self.assertNotIn("US$", motivo)

    def test_teto_desde_a_fala_cruza_a_janela_de_24h(self):
        # revisão Codex: US$ 25 antes da janela de 24 h + US$ 20 hoje, ambos depois da última fala (30 h atrás)
        def gasto(d, horas, usd, tipo="retomada"):
            conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=horas)), "nome": "linear-krn-9",
                             "custo_usd": usd, "ok": True, "tipo": tipo}, d)
        with tempfile.TemporaryDirectory() as d:
            gasto(d, 28, 25.0, "fala")
            gasto(d, 2, 20.0, "fala")
            motivo = agenda.em_loop("linear-krn-9", d, ultima_fala=conta.iso(conta.agora() - dt.timedelta(hours=30)))
            self.assertIn("sem fala nova sua (teto US$ 40", motivo)
        with tempfile.TemporaryDirectory() as d:  # gasto antes da fala não entra na soma
            gasto(d, 35, 25.0, "fala")
            gasto(d, 2, 20.0, "fala")
            self.assertIsNone(agenda.em_loop("linear-krn-9", d, ultima_fala=conta.iso(conta.agora() - dt.timedelta(hours=30))))

    def test_teto_de_gasto_conta_execucao_interrompida(self):
        with tempfile.TemporaryDirectory() as d:
            for h in (1, 2):
                conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=h)), "nome": "linear-krn-9",
                                 "custo_usd": 35.0, "ok": False, "interrompida": True}, d)
            self.assertIn("consumiu", agenda.em_loop("linear-krn-9", d))

    def test_teto_de_gasto_na_reserva_api_fala_em_dolar(self):
        with tempfile.TemporaryDirectory() as d:
            for h in (1, 2, 3):
                conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=h)), "nome": "linear-krn-9",
                                 "custo_usd": 25.0, "ok": True, "conta": "api"}, d)
            self.assertIn("US$", agenda.em_loop("linear-krn-9", d))

    def test_consumo_misto_nao_vira_dinheiro_da_reserva(self):
        with tempfile.TemporaryDirectory() as d:
            for conta_id, custo in (("1", 74), ("api", 1)):
                conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=1)),
                                 "nome": "linear-krn-9", "conta": conta_id,
                                 "custo_usd": custo, "ok": True}, d)
            motivo = agenda.em_loop("linear-krn-9", d)
            self.assertIn("consumiu 100%", motivo)
            self.assertNotIn("US$", motivo)

    def test_teto_soma_api_e_assinatura_abaixo_do_teto_em_cada_lado(self):
        with tempfile.TemporaryDirectory() as d:
            for conta_id, custo in (("api", 30), ("1", 31)):
                conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(hours=1)),
                                 "nome": "linear-krn-9", "conta": conta_id,
                                 "custo_usd": custo, "ok": True}, d)
            motivo = agenda.em_loop("linear-krn-9", d)
            self.assertIn("consumiu", motivo)
            self.assertNotIn("US$", motivo)

    def _runs(self, d, n, desde_h=10, **extra):
        for i in range(n):
            conta.registrar({"inicio": conta.iso(conta.agora() - dt.timedelta(minutes=desde_h * 60 - i * 5)),
                             "nome": "linear-krn-9", "custo_usd": 0.1, "ok": True, **extra}, d)

    def _h(self, horas):
        return conta.iso(conta.agora() - dt.timedelta(hours=horas))

    def test_teto_sem_estado_8_bloqueia_7_passa(self):
        with tempfile.TemporaryDirectory() as d:
            self._runs(d, 7)
            fala = conta.iso(conta.agora())  # fala recente: os 4× sem fala não disparam
            self.assertIsNone(agenda.em_loop("linear-krn-9", d, fala, self._h(12)))
            self._runs(d, 1, desde_h=1)
            self.assertEqual(agenda.em_loop("linear-krn-9", d, fala, self._h(12)), "rodou 8× sem mudar de estado")

    def test_teto_sem_estado_ignora_limite_e_interrompida(self):
        with tempfile.TemporaryDirectory() as d:
            self._runs(d, 7)
            self._runs(d, 3, desde_h=2, limite=True)
            self._runs(d, 3, desde_h=1, interrompida=True)
            self.assertIsNone(agenda.em_loop("linear-krn-9", d, conta.iso(conta.agora()), self._h(12)))

    def test_teto_sem_estado_mudanca_de_estado_zera(self):
        with tempfile.TemporaryDirectory() as d:
            self._runs(d, 9)
            fala = conta.iso(conta.agora())
            self.assertIn("sem mudar de estado", agenda.em_loop("linear-krn-9", d, fala, self._h(12)))
            self.assertIsNone(agenda.em_loop("linear-krn-9", d, fala, self._h(0.01)))

    def test_teto_sem_estado_desligado_sem_estado_desde(self):
        with tempfile.TemporaryDirectory() as d:
            self._runs(d, 12)
            self.assertIsNone(agenda.em_loop("linear-krn-9", d, conta.iso(conta.agora()), None))

    def test_teto_sem_estado_fala_depois_do_aviso_zera_e_aviso_so_uma_vez(self):
        from hub import linear
        item = {"tipo": "issue", "id": "uuid", "identifier": "KRN-9", "estado_desde": self._h(12),
                "ultima_fala": self._h(0.01)}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "comentar") as com:
            self._runs(d, 8)
            motivo = agenda.em_loop("linear-krn-9", d, item["ultima_fala"], item["estado_desde"])
            self.assertEqual(motivo, "rodou 8× sem mudar de estado")
            self.assertTrue(agenda.avisar_teto_estado(item, motivo, d))
            self.assertTrue(agenda.avisar_teto_estado(item, motivo, d))  # segundo tick: não repete
            self.assertEqual(com.call_count, 1)
            self.assertIn("Teto: 8 execuções", com.call_args[0][1])
            self.assertIn("linear-krn-9", json.loads(Path(d, "claude", "teto-avisos.json").read_text()))
            # fala depois do aviso: recomeça a contar
            fala = conta.iso(conta.agora() + dt.timedelta(seconds=5))
            with mock.patch("hub.agenda.conta.agora", lambda: conta.de_iso(fala) + dt.timedelta(seconds=1)):
                self.assertIsNone(agenda.em_loop("linear-krn-9", d, fala, item["estado_desde"]))

    def test_teto_sem_estado_isolado_ignora_regra_4x(self):
        with tempfile.TemporaryDirectory() as d:
            self._runs(d, 5)  # 5 rodadas sem fala: a regra 4× do em_loop barraria, a por estado (8) não
            self.assertIsNone(agenda.teto_sem_estado("linear-krn-9", d, self._h(48), self._h(12)))
            self.assertIn("sem fala nova", agenda.em_loop("linear-krn-9", d, self._h(48), self._h(12)))

    def test_aviso_de_teto_fecha_o_painel_quando_tem_sessao(self):
        from hub import linear
        item = {"tipo": "issue", "id": "uuid", "identifier": "KRN-9", "estado_desde": self._h(12), "ultima_fala": None,
                "sessao": "sess-1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "comentar") as com, \
                mock.patch.object(linear, "atividade") as ativ:
            self.assertTrue(agenda.avisar_teto_estado(item, "rodou 8× sem mudar de estado", d))
            self.assertTrue(agenda.avisar_teto_estado(item, "rodou 8× sem mudar de estado", d))
            self.assertEqual(ativ.call_count, 1)
            self.assertEqual(ativ.call_args[0][:2], ("sess-1", "response"))
            com.assert_not_called()

    def test_fila_respeita_teto_em_todas_as_entradas(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dados = tmp.name
        from hub import linear
        t = linear.tarefa({"identifier": "KRN-844", "id": "i", "tipo": "issue", "titulo": "x",
                           "url": "u", "descricao": "d", "comentarios": [], "repos": [], "modelo": "opus"})
        agora = conta.agora()
        for n in range(20):
            conta.registrar({"nome": t.nome, "inicio": conta.iso(agora - dt.timedelta(minutes=n + 1))}, dados)
        t.linear["ultima_fala"] = conta.iso(agora)  # o teto de execuções conta desde a última fala; o do dia (20) segue valendo
        with mock.patch.object(agenda, "_chamar_orq"):
            for origem in ("proposta feita", "proposta falhou", "manual"):
                self.assertFalse(agenda.enfileirar(t, origem, dados, substituir=True))
            self.assertEqual([], agenda.fila(dados))
            arq = agenda._pasta_disjuntor(dados) / f"{t.nome}.json"
            agenda._gravar_json(arq, {"retomada_em": conta.iso(agora)})
            self.assertTrue(agenda.enfileirar(t, "linear KRN-844", dados, retomada_disjuntor=True))
            anterior = agenda.fila(dados)
            self.assertFalse(agenda.enfileirar(t, "proposta feita", dados, substituir=True))
            self.assertEqual(anterior, agenda.fila(dados))
            agenda._gravar_json(arq, {"retomada_em": conta.iso(agora), "escalada_em": conta.iso(agora)})
            self.assertFalse(agenda.enfileirar(t, "linear KRN-844", dados,
                                             substituir=True, retomada_disjuntor=True))

    def test_pausa_por_loop_retoma_apos_2h_e_escala_se_continuar(self):
        from hub import incidentes, linear
        item = {"identifier": "KRN-9", "id": "i", "tipo": "issue", "titulo": "x", "url": "u", "descricao": "d",
                "comentarios": [], "repos": [], "modelo": "opus"}
        t = linear.tarefa(item)
        avisos = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "comentar", lambda *a, **k: avisos.append(a)):
            self.assertFalse(agenda._pausa_por_loop(t, "rodou 4 vezes sem fala nova sua", d))
            self.assertIn("Retomo sozinho em 2 h", avisos[0][1])
            self.assertFalse(agenda._pausa_por_loop(t, "rodou 4 vezes sem fala nova sua", d))  # ainda cedo: calada
            self.assertEqual(1, len(avisos))
            arq = Path(d, "claude", "disjuntor", "linear-krn-9.json")
            est = json.loads(arq.read_text())
            est["em"] = conta.iso(conta.agora() - dt.timedelta(hours=3))
            arq.write_text(json.dumps(est))
            self.assertTrue(agenda._pausa_por_loop(t, "rodou 4 vezes sem fala nova sua", d))
            self.assertIn("retomada automática", t.prompt)
            self.assertFalse(agenda._pausa_por_loop(t, "m", d))  # na fila, ainda não rodou: não escala
            self.assertEqual([], incidentes.listar(base_dados=d))
            conta.registrar({"inicio": conta.iso(conta.agora() + dt.timedelta(minutes=1)), "nome": "linear-krn-9", "ok": True}, d)
            self.assertFalse(agenda._pausa_por_loop(t, "m", d))
            self.assertFalse(agenda._pausa_por_loop(t, "m", d))
            self.assertEqual(1, len(incidentes.listar(base_dados=d)))

    def test_pausa_nao_retoma_acima_dos_tetos_absolutos(self):
        from hub import linear
        item = {"identifier": "KRN-9", "id": "i", "tipo": "issue", "titulo": "x", "url": "u",
                "descricao": "d", "comentarios": [], "repos": [], "modelo": "opus"}
        for motivo in ("rodou 22 vezes em 24 h sem fechar",
                       "gastou US$ 60 nas últimas 24 h na reserva de API (teto US$ 60)",
                       "consumiu 100% do uso da semana das contas em 24 h"):
            with self.subTest(motivo=motivo), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(linear, "comentar") as aviso, \
                    mock.patch.object(agenda, "_chamar_orq") as orch:
                t = linear.tarefa(item)
                self.assertFalse(agenda._pausa_por_loop(t, motivo, d))
                arq = Path(d, "claude", "disjuntor", "linear-krn-9.json")
                est = json.loads(arq.read_text())
                est["em"] = self._h(3)
                arq.write_text(json.dumps(est))
                self.assertFalse(agenda._pausa_por_loop(t, motivo, d))
                self.assertFalse(agenda._pausa_por_loop(t, motivo, d))
                self.assertNotIn("retomada_em", json.loads(arq.read_text()))
                self.assertEqual(aviso.call_count, 2)  # pausa + bloqueio, sem repetir no tick
                self.assertEqual(orch.call_count, 2)
                # A janela liberou orçamento: o loop comum ainda pode retomar uma vez.
                self.assertTrue(agenda._pausa_por_loop(t, "rodou 4 vezes sem fala nova sua", d))

    def test_retomada_recusada_tenta_novamente_e_enfileira(self):
        from hub import linear
        item = {"identifier": "KRN-9", "id": "i", "tipo": "issue", "titulo": "x", "url": "u",
                "descricao": "d", "comentarios": [], "repos": [], "modelo": "opus"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "comentar") as aviso, \
                mock.patch.object(linear, "puxar", side_effect=lambda ocup, aceitar, **kw: aceitar(linear.tarefa(item))), \
                mock.patch.object(agenda, "em_loop", return_value="rodou 9 vezes"), \
                mock.patch.object(linear, "entregar_respostas_zap", return_value=[]), \
                mock.patch.object(agenda, "voltar_das_esperas", return_value=[]), \
                mock.patch.object(agenda.recibo, "vigiar", return_value=[]), \
                mock.patch.object(agenda.automerge, "vigiar", return_value=[]), \
                mock.patch.object(agenda.automerge, "vigiar_debugger", return_value=[]), \
                mock.patch.object(agenda.automerge, "fechar_incidentes"), \
                mock.patch.object(agenda.parada, "vigiar_no_tick", return_value=[]), \
                mock.patch.object(agenda.main_vermelho, "vigiar_no_tick"), \
                mock.patch.object(agenda, "enfileirar", side_effect=[False, False, True]) as fila:
            t = linear.tarefa(item)
            agenda._pausa_por_loop(t, "rodou 9 vezes", d)
            arq = Path(d, "claude", "disjuntor", "linear-krn-9.json")
            est = json.loads(arq.read_text())
            est["em"] = conta.iso(conta.agora() - dt.timedelta(hours=3))
            arq.write_text(json.dumps(est))
            agenda.puxar_linear({}, d)
            self.assertNotIn("retomada_em", json.loads(arq.read_text()))
            self.assertIn("fila recusou", aviso.call_args.args[1])
            agenda.puxar_linear({}, d)
            self.assertEqual(2, aviso.call_count)  # pausa inicial e aviso único da fila
            self.assertIn("linear-krn-9", agenda.puxar_linear({}, d))
            self.assertEqual(3, fila.call_count)
            self.assertIn("retomada automática", fila.call_args.args[0].prompt)

    def test_puxar_linear_acorda_metas(self):
        from hub import linear, metas
        t = mock.Mock()
        t.nome = "linear-krn-800"

        def acordar(ocup, base, reabrir, enfileirar, em_loop):
            self.assertIs(enfileirar(t, "meta m-1"), True)
            return ["linear-krn-800"]
        tarefas = {}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "entregar_respostas_zap"), \
                mock.patch.object(linear, "puxar"), mock.patch.object(agenda, "voltar_das_esperas", lambda *a: []), \
                mock.patch.object(agenda, "enfileirar", lambda *a: True), mock.patch.object(metas, "acordar", acordar):
            self.assertEqual(agenda.puxar_linear(tarefas, d), ["linear-krn-800"])
        self.assertIs(tarefas["linear-krn-800"], t)

    def test_disjuntor_loga_uma_vez_por_dia_por_issue(self):
        from hub import linear
        logs = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(linear, "chave", lambda: "k"), \
                mock.patch.object(linear, "sessoes_ok", lambda: False), mock.patch.object(linear, "conferir", lambda *a, **k: ""), \
                mock.patch.object(linear, "comentar", lambda *a, **k: None), mock.patch.object(agenda, "_log", logs.append), \
                mock.patch.object(agenda, "em_loop", lambda *a, **k: "rodou 9 vezes em 24 h sem fechar"), \
                mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issues": {"nodes": [self.ISSUE]}, "projectUpdates": {"nodes": []}}):
            for _ in range(3):
                self.assertEqual(agenda.puxar_linear({}, d), [])
        self.assertEqual(sum("disjuntor" in l for l in logs), 1)

    def test_com_app_sessao_do_agente(self):
        from hub import linear
        sessao = {"id": "s1", "status": "pending", "updatedAt": "2026-10-08T11:00:00Z", "issue": self.ISSUE,
                  "activities": {"nodes": []}}

        def gql(q, app=False, **v):
            if "agentSessions" in q:
                return {"agentSessions": {"nodes": [sessao]}}
            return {"issues": {"nodes": [self.ISSUE]}, "projectUpdates": {"nodes": []}}
        atividades, movidas = [], []
        with tempfile.TemporaryDirectory() as d,                 mock.patch.object(linear, "chave", lambda: "k"), mock.patch.object(linear, "token_app", lambda **k: "t"),                 mock.patch.object(linear, "gql", gql),                 mock.patch.object(linear, "atividade", lambda s, tipo, corpo=None, **k: atividades.append((s, tipo, corpo))),                 mock.patch.object(linear, "mover", lambda i, s, c=None: movidas.append(s)), mock.patch.object(linear, "conferir", lambda *a, **k: ""):
            # a mesma issue também aparece como "comentário do Ana": a sessão já cobre, não duplica
            self.assertEqual(agenda.puxar_linear({}, d), ["linear-krn-7"])
            t = agenda.da_fila(d)["linear-krn-7"]
            self.assertEqual(t.linear["sessao"], "s1")
            saida = Path(d, "r.md")
            saida.write_text("feito", encoding="utf-8")
            linear.concluir(t, {"ok": True, "saida": str(saida), "conta": "3", "duracao_s": 60})
            linear.concluir(t, {"ok": False, "saida": None, "erro": "x\nPARADO pelo Ana no Linear", "duracao_s": 60})
        self.assertEqual([a[1] for a in atividades], ["thought", "response", "response"])
        self.assertIn("Parei", atividades[2][2])
        self.assertEqual(movidas, ["In Progress", "In Review"])

    def test_sessao_quer_resposta_e_stop(self):
        from hub import linear
        p = lambda t, sig=None: {"createdAt": t, "signal": sig, "content": {"__typename": "AgentActivityPromptContent", "body": "oi"}}  # noqa: E731
        r = lambda t: {"createdAt": t, "signal": None, "content": {"__typename": "AgentActivityResponseContent"}}  # noqa: E731
        s = {"status": "pending", "activities": {"nodes": []}}
        self.assertTrue(linear.sessao_quer_resposta(s))
        th = {"createdAt": "0", "signal": None, "content": {"__typename": "AgentActivityThoughtContent"}}
        self.assertTrue(linear.sessao_quer_resposta({"status": "active", "activities": {"nodes": [th]}}))  # "Recebi" não fecha
        s = {"status": "complete", "activities": {"nodes": [p("1"), r("2")]}}
        self.assertFalse(linear.sessao_quer_resposta(s))
        s["activities"]["nodes"].append(p("3", "stop"))
        self.assertTrue(linear.sessao_quer_resposta(s))
        self.assertTrue(linear.parar_pedido(s))

    def test_sessao_done_so_acorda_com_prompt_posterior_ao_fechamento(self):
        from hub import linear
        s = {"issue": {"state": {"type": "completed"}, "history": {"nodes": [
            {"createdAt": "2026-10-15T20:54:49Z", "toState": {"type": "completed"}},
            {"createdAt": "2026-10-15T22:50:41Z", "toState": None}]}},
             "activities": {"nodes": []}}
        self.assertFalse(linear.sessao_quer_resposta(s))
        def prompt(em):
            return {"createdAt": em, "content": {"__typename": "AgentActivityPromptContent"}}
        s["activities"]["nodes"] = [prompt("2026-10-15T20:00:00Z")]
        self.assertFalse(linear.sessao_quer_resposta(s))
        s["activities"]["nodes"].append(prompt("2026-10-15T21:00:00Z"))
        self.assertTrue(linear.sessao_quer_resposta(s))
        s["activities"]["nodes"].append({"createdAt": "2026-10-15T21:01:00Z",
            "content": {"__typename": "AgentActivityResponseContent"}})
        self.assertFalse(linear.sessao_quer_resposta(s))
        s["issue"].pop("history")
        self.assertFalse(linear.sessao_quer_resposta(s))

    def test_ao_vivo_manda_a_acao_atual_sem_inundar(self):
        from hub import linear
        enviados, agora = [], [0.0]
        av = linear.AoVivo("s1", enviar=lambda tipo, **c: enviados.append((tipo, c)), relogio=lambda: agora[0] + 100)
        uso = lambda nome, entrada: {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": nome, "input": entrada}]}}  # noqa: E731
        av(uso("Bash", {"command": "npm test\nmais"}))
        av(uso("Read", {"file_path": "lib/filme.ts"}))  # dentro do intervalo: guarda
        self.assertEqual(enviados, [("action", {"action": "Bash", "parameter": "npm test"})])
        agora[0] += linear.AoVivo.INTERVALO
        av.descarregar()
        self.assertEqual(enviados[-1], ("action", {"action": "Read", "parameter": "lib/filme.ts"}))

    def test_comentario_do_fulano_reabre_conversa(self):
        from hub import linear
        c = lambda b, t: {"body": b, "createdAt": t, "user": {"name": "Ana Pereira"}}  # noqa: E731
        i = {"comments": {"nodes": [c("Na fila do Claude da VPS.", "1"), c("feito. _Claude da VPS (conta 1)._", "2")]}}
        self.assertFalse(linear.quer_resposta(i))
        i["comments"]["nodes"].append(c("faz a proposta?", "3"))
        self.assertTrue(linear.quer_resposta(i))
        self.assertTrue(linear.quer_resposta({"comments": {"nodes": [c("ideia minha", "1")]}}))  # qualquer issue aberta
        self.assertTrue(linear.quer_resposta({"comments": {"nodes": []}}))  # criada em qualquer coluna, sem comentário
        fechada = {"state": {"type": "completed"}, "comments": {"nodes": []}}
        self.assertFalse(linear.quer_resposta(fechada))  # Done sem fala: não acorda
        fechada["comments"]["nodes"].append(c("faz o que te pedi", "9"))
        self.assertTrue(linear.quer_resposta(fechada))  # Done com fala nova do Ana: acorda
        u = {"body": "ta funcionando?", "comments": {"nodes": []}}
        self.assertTrue(linear.update_quer_resposta(u))
        u["comments"]["nodes"].append(c("Na fila do Claude da VPS.", "1"))
        self.assertFalse(linear.update_quer_resposta(u))
        u["comments"]["nodes"].append(c("e agora?", "2"))
        self.assertTrue(linear.update_quer_resposta(u))


    def test_comentario_automatico_do_linear_nao_e_fala_do_fulano(self):
        """cada sessão nova faz o Linear postar "This thread is for an agent session" com user=null; contado
        como fala do Ana, a issue rodava de novo à toa até o disjuntor (12×, 8×)."""
        from hub import linear
        sessao = {"body": "This thread is for an agent session with claude.", "createdAt": "2026-10-11T00:50:00Z", "user": None}
        claude = {"body": "Feito.", "createdAt": "2026-10-11T00:40:00Z", "user": {"name": "Claude", "app": True}}
        fulano = {"body": "faz a parte 2", "createdAt": "2026-10-11T00:30:00Z", "user": {"name": "Ana Pereira", "app": False}}
        i = {"comments": {"nodes": [fulano, claude, sessao]}}
        self.assertFalse(linear.quer_resposta(i))
        self.assertEqual(linear.item_da_issue({**i, "id": "x", "identifier": "KRN-1", "title": "t", "url": "u",
                                               "labels": {"nodes": []}, "team": {"states": {"nodes": []}}})["ultima_fala"],
                         fulano["createdAt"])  # o disjuntor conta a partir da fala real, não do comentário do Linear
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}), \
                mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issue": {"comments": {"nodes": [fulano, claude, sessao]}}}):
            self.assertEqual(linear.falas_nao_lidas({"identifier": "KRN-1"}, "2026-10-11T00:35:00Z"), [])
            fala = {"body": "e o teste?", "createdAt": "2026-10-11T00:55:00Z", "user": {"name": "Ana Pereira", "app": False}}
            with mock.patch.object(linear, "gql", lambda q, app=False, **v: {"issue": {"comments": {"nodes": [claude, sessao, fala]}}}):
                self.assertEqual(linear.falas_nao_lidas({"identifier": "KRN-1"}, "2026-10-11T00:35:00Z"), [fala])
        i["comments"]["nodes"].append({**fulano, "createdAt": "2026-10-11T01:00:00Z"})
        self.assertTrue(linear.quer_resposta(i))  # fala de verdade do Ana continua acordando
        self.assertFalse(linear._eh_meu({"body": "oi"}))  # consulta sem o campo user: não vira "sem autor"


class LinearCorridaTest(unittest.TestCase):
    def test_outra_conta_nao_joga_fora_item_do_linear(self):
        item = {"tipo": "issue", "id": "u", "identifier": "KRN-9", "titulo": "t", "descricao": "d", "url": "x",
                "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {}}
        from hub import linear
        with tempfile.TemporaryDirectory() as d:
            agenda.enfileirar(linear.tarefa(item), "linear", d)
            agenda.agendar({}, conta.agora(), d)  # conta que carregou as tarefas antes do item entrar
            self.assertEqual([i["nome"] for i in agenda.fila(d)], ["linear-krn-9"])
            self.assertEqual(agenda.proximo({}, dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT), True, d).nome, "linear-krn-9")


class StatusTest(unittest.TestCase):
    def test_como_esta_le_o_registro_e_responde_uma_vez_por_fala(self):
        from hub import linear
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}):
            log = Path(d, "x.stream.jsonl")
            ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Vou renderizar o story."},
                                                                {"type": "tool_use", "name": "Bash", "input": {"command": "node filme/renderizar.mjs"}}]}}
            log.write_text(json.dumps(ev) + "\n{lixo\n", encoding="utf-8")
            self.assertEqual(linear.passos_do_registro(log), ["(escreveu) Vou renderizar o story.", "[Bash] node filme/renderizar.mjs"])
            disparos = []
            with mock.patch("subprocess.Popen", lambda *a, **k: disparos.append(a)):
                item = {"tipo": "issue", "id": "u", "identifier": "KRN-6", "titulo": "t"}
                linear.disparar_status(item, {"pid": 1, "log": str(log)}, "e aí?")
                linear.disparar_status(item, {"pid": 1, "log": str(log)}, "e aí?")  # mesma fala: não repete
                linear.disparar_status(item, {"enfileirada_em": "x"}, "outra")    # só na fila: não precisa
            self.assertEqual(len(disparos), 1)
            comentarios, prompts = [], []
            with mock.patch.object(linear, "comentar", lambda i, txt, campo="issueId": comentarios.append(txt)):
                linear.responder_status({"item": item, "registro": {"log": str(log), "inicio": "t0", "conta": "1"}, "fala": "e aí?"},
                                        chamar=lambda p: prompts.append(p) or "Story renderizando agora.")
            self.assertIn("node filme/renderizar.mjs", prompts[0])
            self.assertIn("Story renderizando agora.", comentarios[0])
            self.assertIn("Claude da VPS", comentarios[0])


class ConferenciaTest(unittest.TestCase):
    def test_faltou_algo_volta_para_a_fila_uma_vez_e_em_silencio(self):
        from hub import linear
        item = {"tipo": "issue", "id": "u", "identifier": "KRN-9", "titulo": "posta o story", "descricao": "", "url": "x",
                "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {"In Review": "r", "Backlog": "b"}}
        comentarios, movidas = [], []
        with tempfile.TemporaryDirectory() as d,                 mock.patch.object(linear, "comentar", lambda i, txt, campo="issueId": comentarios.append(txt)),                 mock.patch.object(linear, "mover", lambda i, s, c=None: movidas.append(s)):
            saida = Path(d, "r.md")
            saida.write_text("Gerei o story.", encoding="utf-8")
            reg = {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60}
            t = linear.concluir(linear.tarefa(item), reg, conferir_fn=lambda i, txt: "postar o story no Instagram.")
            self.assertIsNotNone(t)
            self.assertIn("faltou postar o story", t.prompt)
            self.assertIsNone(linear.concluir(t, reg, conferir_fn=lambda i, txt: "ainda falta"))  # 2ª: entrega como está
        self.assertEqual(movidas, ["r"])  # id do estado In Review: só a entrega final vai para revisão
        self.assertEqual(comentarios, [])  # a refação não notifica; a entrega final vai junto com o mover

    def test_conferencia_le_o_json_e_falha_nao_trava(self):
        from hub import linear
        item = {"titulo": "t", "descricao": "d", "comentarios": []}
        self.assertEqual(linear.conferir(item, "x", chamar=lambda p: 'ok {"completo": true, "faltou": ""}'), "")
        self.assertEqual(linear.conferir(item, "x", chamar=lambda p: '{"completo": false, "faltou": "anexar o vídeo"}'), "anexar o vídeo")
        self.assertEqual(linear.conferir(item, "x", chamar=lambda p: "lixo"), "")


class VisivelTest(unittest.TestCase):
    def test_entrega_a_vista_com_anexos_e_aviso_no_zap(self):
        from hub import linear
        item = {"tipo": "issue", "id": "u", "identifier": "KRN-11", "titulo": "Redesenho LP", "descricao": "", "url": "https://l/KRN-11",
                "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {"In Review": "r", "Backlog": "b"}, "sessao": "s1"}
        falas, atividades, zap = [], [], []
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}),                 mock.patch.object(linear, "falar", lambda alvo, txt, update=False: falas.append((alvo, txt))),                 mock.patch.object(linear, "atividade", lambda s, tipo, corpo=None, **k: atividades.append((tipo, corpo))),                 mock.patch.object(linear, "_mover_no_fim", lambda *a, **k: None),                 mock.patch.object(linear, "conferir", lambda *a, **k: ""),                 mock.patch("hub.conta.avisar", lambda txt, agora_=False: zap.append(txt)):
            linear.registrar_anexo("KRN-11", "[proposta.md](https://uploads.linear.app/x)", "# Proposta\nHero novo.")
            saida = Path(d, "r.md")
            saida.write_text("A proposta segue anexada.", encoding="utf-8")
            linear.concluir(linear.tarefa(item), {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60, "inicio": "2000-01-01T00:00:00Z"})
        self.assertEqual(falas, [])  # uma notificação só: a response do agente
        [(tipo, corpo)] = atividades
        self.assertEqual(tipo, "response")
        self.assertIn("https://uploads.linear.app/x", corpo)  # o link que o texto esqueceu entra sozinho
        self.assertIn("Hero novo.", corpo)  # e o conteúdo do .md, para ler sem baixar
        self.assertEqual(zap, [])  # tarefa pronta não vai ao WhatsApp, só a com falha (#187)


class VagasTest(unittest.TestCase):
    """Várias sessões por conta (09/10): cada vaga tem trava e arquivo de rodando próprios."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_nome_do_arquivo_por_vaga(self):
        self.assertEqual(agenda._rodando_arq(self.d, vaga=0).name, "rodando.json")
        self.assertEqual(agenda._rodando_arq(self.d, vaga=2).name, "rodando-v2.json")

    @unittest.skipIf(os.name != "posix", "fcntl só no Linux")
    def test_segunda_vaga_quando_a_primeira_esta_presa(self):
        trava = os.path.join(self.d, "conta.lock")
        with mock.patch.object(agenda.conta, "caminho_trava", lambda n=None: trava), \
                mock.patch.object(agenda.conta, "TRAVA", trava):
            with agenda._trava_vaga(2) as a:
                self.assertTrue(a)
                self.assertEqual(agenda._vaga, 0)
                pid = os.fork()
                if pid == 0:  # outro processo: a vaga 0 está presa, pega a 1; com 1 vaga só, não pega nada
                    with agenda._trava_vaga(2) as b:
                        ok = b and agenda._vaga == 1
                    with agenda._trava_vaga(1) as c:
                        ok = ok and not c
                    os._exit(0 if ok else 1)
                _, st = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(st), 0)
            self.assertEqual(agenda._vaga, 0)

    def test_conta_livre_enquanto_sobra_vaga(self):
        with mock.patch.object(agenda, "VAGAS", 2), mock.patch.object(agenda.conta, "pausa_ativa", lambda *a, **k: None):
            agenda._gravar_json(agenda._rodando_arq(self.d, vaga=0), {"nome": "x", "pid": os.getpid()})
            self.assertTrue(agenda._conta_livre("1", self.d))
            agenda._gravar_json(agenda._rodando_arq(self.d, vaga=1), {"nome": "y", "pid": os.getpid()})
            self.assertFalse(agenda._conta_livre("1", self.d))
            self.assertEqual(len(agenda.rodando_todas(self.d)), 2)


class RegistroMortoTest(unittest.TestCase):
    """Reboot de 12/10: registro de processo morto (ou de pid reaproveitado) segurava vaga e issue por horas."""

    ITEM = {"tipo": "issue", "id": "u331", "identifier": "KRN-331", "titulo": "t", "descricao": "", "url": "x",
            "modelo": "haiku", "repos": ["example-org/asite01"], "comentarios": [], "estados": {}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name

    def _registro(self, pid, inicio, vaga=0):
        agenda._gravar_json(agenda._rodando_arq(self.d, vaga=vaga),
                            {"nome": "linear-krn-331", "inicio": inicio, "pid": pid, "conta": "1",
                             "repos": ["example-org/asite01"], "linear": self.ITEM})

    def _soltou(self):
        self.assertEqual(agenda.rodando_todas(self.d), {})
        with mock.patch.object(agenda, "_log", lambda *a: None):
            self.assertEqual(agenda.limpar_mortos(self.d), ["linear-krn-331"])
        self.assertFalse(agenda._rodando_arq(self.d, vaga=0).exists())
        self.assertEqual([i["nome"] for i in agenda.fila(self.d)], ["linear-krn-331"])  # a issue voltou para a fila
        self.assertEqual(agenda.fila(self.d)[0]["linear"]["identifier"], "KRN-331")

    def test_pid_inexistente_solta_a_vaga_e_a_issue(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()  # pid que não existe mais
        self._registro(p.pid, "2026-10-12T17:30:00-03:00")
        self._soltou()

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "precisa de /proc")
    def test_pid_reaproveitado_depois_do_reboot(self):
        # o pid existe, mas o processo começou depois do registro: é outro processo com o mesmo número
        self._registro(os.getpid(), "2020-01-01T00:00:00+00:00")
        self._soltou()

    @unittest.skipUnless(os.path.exists("/proc/1/stat") and os.getuid() != 0, "precisa de /proc e usuário comum")
    def test_pid_reaproveitado_por_processo_de_root(self):
        # kill(pid, 0) em processo de root dá PermissionError: antes contava como vivo para sempre
        self._registro(1, "1990-01-01T00:00:00+00:00")
        self._soltou()

    def test_falha_ao_reenfileirar_mantem_o_registro(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        self._registro(p.pid, "2026-10-12T17:30:00-03:00")
        with mock.patch.object(agenda, "_log", lambda *a: None):
            with mock.patch.object(agenda, "enfileirar", side_effect=OSError("disco cheio")):
                self.assertEqual(agenda.limpar_mortos(self.d), [])
            self.assertEqual(agenda.rodando_todas(self.d), {})  # já não segura a vaga
            # outra tarefa pega a vaga: o registro guardado não é sobrescrito
            agenda._gravar_json(agenda._rodando_arq(self.d, vaga=0), {"nome": "outra", "inicio": conta.iso(conta.agora()),
                                                                      "pid": os.getpid()})
            self.assertEqual(agenda.limpar_mortos(self.d), ["linear-krn-331"])  # no despacho seguinte a issue volta
        self.assertEqual([i["nome"] for i in agenda.fila(self.d)], ["linear-krn-331"])
        self.assertEqual(list(agenda.rodando_todas(self.d)), ["rodando.json"])
        self.assertEqual(list(Path(self.d).glob("morto-*.json")), [])

    def test_falha_ao_reenfileirar_e_ao_guardar_para_o_despacho(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        self._registro(p.pid, "2026-10-12T17:30:00-03:00")
        with mock.patch.object(agenda, "_log", lambda *a: None), \
                mock.patch.object(agenda, "enfileirar", side_effect=OSError("disco cheio")), \
                mock.patch.object(Path, "replace", side_effect=OSError("somente leitura")):
            with self.assertRaises(OSError):
                agenda.limpar_mortos(self.d)
        self.assertTrue(agenda._rodando_arq(self.d, vaga=0).exists())  # o registro com a issue continua lá

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "precisa de /proc")
    def test_pid_reaproveitado_logo_depois_do_boot(self):
        # registro gravado segundos antes do reboot e pid que voltou em processo que nasceu logo depois:
        # a folga não protege, mas o registro é de antes do boot
        boot = agenda._boot()
        self._registro(os.getpid(), conta.iso(dt.datetime.fromtimestamp(boot - 1, dt.timezone.utc)))
        with mock.patch.object(agenda, "_inicio_processo", lambda pid: boot + 2):
            self._soltou()

    def test_registro_vivo_continua(self):
        self._registro(os.getpid(), conta.iso(conta.agora()))
        self.assertEqual(list(agenda.rodando_todas(self.d)), ["rodando.json"])
        self.assertEqual(agenda.limpar_mortos(self.d), [])
        self.assertTrue(agenda._rodando_arq(self.d, vaga=0).exists())
        self.assertEqual(agenda.fila(self.d), [])

    def test_vaga_com_registro_morto_fica_livre(self):
        with mock.patch.object(agenda, "VAGAS", 1), mock.patch.object(agenda.conta, "pausa_ativa", lambda *a, **k: None):
            self._registro(os.getpid(), conta.iso(conta.agora()))
            self.assertFalse(agenda._conta_livre("1", self.d))
            self._registro(os.getpid(), "2020-01-01T00:00:00+00:00")
            self.assertTrue(agenda._conta_livre("1", self.d))

    def test_registro_marcado_leva_o_item_do_linear(self):
        from hub import linear
        t = linear.tarefa(self.ITEM)
        agenda._marcar_rodando(t, self.d)
        r = json.loads(agenda._rodando_arq(self.d).read_text(encoding="utf-8"))
        self.assertEqual((r["nome"], r["pid"], r["linear"]["identifier"]), ("linear-krn-331", os.getpid(), "KRN-331"))


class MaquinaTest(unittest.TestCase):
    def test_carga_alta_fecha_vaga_extra(self):
        with mock.patch.object(agenda.os, "getloadavg", lambda: (27.0, 20.0, 15.0)), mock.patch.object(agenda.os, "cpu_count", lambda: 8):
            self.assertFalse(agenda.maquina_folgada())

    def test_carga_baixa_e_memoria_sobrando_abre(self):
        meminfo = "MemTotal: 20000000 kB\nMemAvailable: 8000000 kB\n"
        with mock.patch.object(agenda.os, "getloadavg", lambda: (2.0, 2.0, 2.0)), mock.patch.object(agenda.os, "cpu_count", lambda: 8), \
                mock.patch("builtins.open", mock.mock_open(read_data=meminfo)):
            self.assertTrue(agenda.maquina_folgada())

    def test_pouca_memoria_fecha(self):
        meminfo = "MemTotal: 20000000 kB\nMemAvailable: 1000000 kB\n"
        with mock.patch.object(agenda.os, "getloadavg", lambda: (2.0, 2.0, 2.0)), mock.patch.object(agenda.os, "cpu_count", lambda: 8), \
                mock.patch("builtins.open", mock.mock_open(read_data=meminfo)):
            self.assertFalse(agenda.maquina_folgada())


class FaixasTest(unittest.TestCase):
    """Cada conta pega primeiro a fila da sua faixa (construtor = Linear, operador = tarefas agendadas)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rotina = T.Tarefa("conteudo-dia", "manual", 2, "sonnet", 600, [], False, "x", "p")
        self.issue = T.Tarefa("linear-krn-1", "manual", 2, "opus", 600, [], False, "x", "p")
        self.issue.linear = {"identifier": "KRN-1"}

    def escolhe(self, faixa, *tarefas):
        with mock.patch.object(agenda, "minha_faixa", lambda: faixa), \
                mock.patch.object(agenda, "fila", lambda *a: [{"nome": t.nome, "prioridade": t.prioridade} for t in tarefas]), \
                mock.patch.object(agenda.linear, "tarefa", lambda item: self.issue):
            return agenda.proximo({self.rotina.nome: self.rotina}, conta.agora(), False, self.d)

    def test_operador_pega_a_rotina_mesmo_atras_na_fila(self):
        self.issue_item = self.issue
        with mock.patch.object(agenda, "minha_faixa", lambda: "operador"), \
                mock.patch.object(agenda, "fila", lambda *a: [{"nome": "linear-krn-1", "prioridade": 2, "linear": {"identifier": "x"}},
                                                              {"nome": "conteudo-dia", "prioridade": 2}]), \
                mock.patch.object(agenda.linear, "tarefa", lambda item: self.issue):
            self.assertIs(agenda.proximo({"conteudo-dia": self.rotina}, conta.agora(), False, self.d), self.rotina)

    def test_fila_da_faixa_vazia_ajuda_nas_outras(self):
        with mock.patch.object(agenda, "minha_faixa", lambda: "operador"), \
                mock.patch.object(agenda, "fila", lambda *a: [{"nome": "linear-krn-1", "prioridade": 2, "linear": {"identifier": "x"}}]), \
                mock.patch.object(agenda.linear, "tarefa", lambda item: self.issue):
            self.assertIs(agenda.proximo({}, conta.agora(), False, self.d), self.issue)

    def test_faixa_nao_fura_prioridade_maior(self):
        urgente = T.Tarefa("linear-krn-2", "manual", 1, "opus", 600, [], False, "x", "p")
        urgente.linear = {"identifier": "KRN-2"}
        with mock.patch.object(agenda, "minha_faixa", lambda: "operador"), \
                mock.patch.object(agenda, "fila", lambda *a: [{"nome": "linear-krn-2", "prioridade": 1, "linear": {"identifier": "x"}},
                                                              {"nome": "conteudo-dia", "prioridade": 2}]), \
                mock.patch.object(agenda.linear, "tarefa", lambda item: urgente):
            self.assertIs(agenda.proximo({"conteudo-dia": self.rotina}, conta.agora(), False, self.d), urgente)

    def test_conta_sem_login_nao_pega_nada(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(agenda, "HOME", Path(home)), \
                mock.patch.dict(os.environ, {"CLAUDE_CONTA": "4"}), mock.patch.object(agenda.conta, "pausa_ativa", lambda *a, **k: None):
            self.assertEqual(agenda.despachar({}, base_dados=self.d), "conta sem login")


class EspalhaTest(unittest.TestCase):
    def test_vaga_extra_cede_para_conta_parada(self):
        d = tempfile.mkdtemp()
        agenda._gravar_json(agenda._rodando_arq(d, vaga=0), {"nome": "x", "pid": os.getpid()})

        @contextlib.contextmanager
        def trava_vaga_1():
            agenda._vaga = 1
            try:
                yield True
            finally:
                agenda._vaga = 0
        with mock.patch.object(agenda, "conta_ociosa", lambda n, *a: n == "2"), \
                mock.patch.object(agenda.conta, "pausa_ativa", lambda *a, **k: None):
            self.assertEqual(agenda.despachar({}, base_dados=d, trava=trava_vaga_1), "cedi: outra conta está parada")

    def test_conta_ociosa_olha_todas_as_vagas(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(agenda, "_conta_livre", lambda *a: True):
            self.assertTrue(agenda.conta_ociosa("1", d))
            agenda._gravar_json(agenda._rodando_arq(d, vaga=1), {"nome": "y", "pid": os.getpid()})
            self.assertFalse(agenda.conta_ociosa("1", d))


class CanceladaTest(unittest.TestCase):
    def test_issue_cancelada_sai_da_fila_sem_rodar(self):
        d = tempfile.mkdtemp()
        tiradas = []
        with mock.patch.object(agenda, "fila", lambda *a: [{"nome": "linear-krn-104", "prioridade": 1, "linear": {"identifier": "KRN-104"}}]), \
                mock.patch.object(agenda, "_cancelada", lambda item: True), \
                mock.patch.object(agenda, "tirar", lambda n, *a: tiradas.append(n)):
            self.assertIsNone(agenda.proximo({}, conta.agora(), False, d))
        self.assertEqual(tiradas, ["linear-krn-104"])


class PromptGrandeTest(unittest.TestCase):
    """12/10: issue com muitos comentários dava "Argument list too long" no exec; prompt grande vai pela entrada."""

    def _captura(self, prompt):
        visto = {}

        def falso(cmd, cwd, env, timeout_s, ao_vivo=None, traduzir=None, entrada=None):
            visto.update(cmd=cmd, entrada=entrada)
            return None, 0, "", ""
        with mock.patch.object(agenda, "_rodar_cli", falso), mock.patch.object(agenda, "claude_bin", lambda: "claude"):
            agenda.chamar_claude(prompt, "sonnet", 60, "/tmp", {})
        return visto

    def test_prompt_curto_vai_no_argumento(self):
        v = self._captura("faz x")
        self.assertIn("faz x", v["cmd"])
        self.assertIsNone(v["entrada"])

    def test_prompt_grande_vai_pela_entrada(self):
        grande = "á" * (agenda.PROMPT_MAX_ARG)
        v = self._captura(grande)
        self.assertNotIn(grande, v["cmd"])
        self.assertEqual(v["entrada"], grande)
        self.assertEqual(v["cmd"][:2], ["claude", "-p"])


class TickLinearIndisponivelTest(unittest.TestCase):
    def test_falha_da_coleta_nao_impede_despacho_da_fila_local(self):
        import io
        for erro in (RuntimeError("Linear: limite da hora nas duas chaves"), OSError("rede")):
            with self.subTest(erro=type(erro).__name__), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(agenda.sys, "stdout", io.TextIOWrapper(io.BytesIO())), \
                    mock.patch.object(agenda.T, "carregar", return_value={}), \
                    mock.patch.object(agenda, "da_fila", return_value={"local": "tarefa"}), \
                    mock.patch.object(agenda, "tem_login", return_value=True), \
                    mock.patch.object(agenda, "_pasta", return_value=Path(d)), \
                    mock.patch.object(agenda.conta, "trava", lambda **k: livre()), \
                    mock.patch.object(agenda.conta, "sufixo", return_value=""), \
                    mock.patch.object(agenda, "agendar", return_value=[]), \
                    mock.patch.object(agenda, "puxar_linear", side_effect=erro), \
                    mock.patch.object(agenda, "_log") as log, \
                    mock.patch.object(agenda, "despachar", return_value="rodou local") as despachar:
                self.assertEqual(agenda.main(["tick"]), 0)
                despachar.assert_called_once_with({"local": "tarefa"})
                self.assertIn("despachando fila local", log.call_args_list[0].args[0])


class LimiteGastoTest(unittest.TestCase):
    """Limite de gasto por dia (agentes.limite_usd_dia): estourou, a conta não pega trabalho. Sem limite = como sempre."""

    def setUp(self):
        self.dados = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        cred = Path(self.home, ".claude-2")
        cred.mkdir()
        (cred / ".credentials.json").write_text("{}")
        self.dia = dt.datetime(2026, 10, 8, 10, 0, tzinfo=BRT)
        self.t = tarefa("rotina")
        agenda.enfileirar(self.t, "teste", self.dados, self.dia)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KRONOS_")}
        env.update(CLAUDE_CONTA="2", KRONOS_CONFIG=str(Path(self.dados, "nao-existe.toml")),
                   KRONOS_CAMINHOS_DADOS=str(Path(self.dados, "config-dados")))
        for p in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(agenda, "HOME", Path(self.home))):
            p.start()
            self.addCleanup(p.stop)
        from hub import config
        config.recarregar()
        self.addCleanup(config.recarregar)

    def gasta(self, usd, n="2", quando=None):
        conta.registrar({"inicio": conta.iso(quando or self.dia), "conta": n, "custo_usd": usd, "nome": "x"}, self.dados)

    def despacha(self):
        rodou = []
        r = agenda.despachar({"rotina": self.t}, self.dia, self.dados, livre, lambda t, base_dados=None: rodou.append(t.nome),
                             Path(self.dados, "sem-fila"))
        return r, rodou

    def test_sem_limite_roda_como_hoje(self):
        self.gasta(500)
        self.assertEqual(self.despacha(), ("rodou rotina", ["rotina"]))

    def test_limite_estourado_nao_pega_trabalho(self):
        os.environ["KRONOS_AGENTES_LIMITE_USD_DIA"] = "5"
        self.gasta(3)
        self.gasta(2.5)
        r, rodou = self.despacha()
        self.assertTrue(r.startswith("limite de gasto do dia"), r)
        self.assertEqual(rodou, [])
        self.assertIsNotNone(agenda.tirar("rotina", self.dados))  # continua na fila (tirar acha)

    def test_abaixo_do_limite_ou_outra_conta_ou_outro_dia_roda(self):
        os.environ["KRONOS_AGENTES_LIMITE_USD_DIA"] = "5"
        self.gasta(4.99)
        self.gasta(50, n="3")  # gasto de outra conta
        self.gasta(50, quando=self.dia - dt.timedelta(days=1))  # ontem
        self.assertEqual(self.despacha()[0], "rodou rotina")

    def test_limite_da_conta_vale_mais_que_o_padrao(self):
        os.environ["KRONOS_AGENTES_LIMITE_USD_DIA"] = "100"
        os.environ["KRONOS_AGENTES_LIMITES"] = '{"2": 1}'
        self.gasta(1.5)
        self.assertTrue(self.despacha()[0].startswith("limite de gasto do dia"))
        os.environ["KRONOS_AGENTES_LIMITES"] = '{"3": 1}'  # a 2 volta ao padrão (100)
        self.assertEqual(self.despacha()[0], "rodou rotina")

    def test_gasto_conta_pelo_fim_da_execucao_no_dia_de_brasilia(self):
        os.environ["KRONOS_AGENTES_LIMITE_USD_DIA"] = "1"
        ontem_tarde = self.dia - dt.timedelta(hours=11)  # 23h de ontem em Brasília
        conta.registrar({"inicio": conta.iso(ontem_tarde), "fim": conta.iso(self.dia - dt.timedelta(hours=9)), "conta": "2",
                         "custo_usd": 5, "nome": "x"}, self.dados)  # terminou hoje, 01h
        self.assertTrue(self.despacha()[0].startswith("limite de gasto do dia"))

    def test_conta_adicionada_pela_config_aparece_sem_reimportar(self):
        from hub import config
        self.assertEqual(sorted(agenda.carga_contas(self.dados, self.dia)), ["1", "2", "3", "4"])  # legado, sem toml
        config.gravar({"agentes.contas": ["1", "2", "7"]})
        self.assertEqual(sorted(agenda.carga_contas(self.dados, self.dia)), ["1", "2", "7"])
        self.assertEqual(conta.com_login(self.home), ("1", "2"))
        Path(self.home, ".claude-7").mkdir()
        Path(self.home, ".claude-7", ".credentials.json").write_text("{}")
        self.assertEqual(conta.com_login(self.home), ("1", "2", "7"))
        self.assertFalse(agenda._conta_livre("9", self.dados, self.dia))

    def test_conta_no_limite_nao_cede_vez_nem_conta_como_livre(self):
        os.environ["KRONOS_AGENTES_LIMITE_USD_DIA"] = "1"
        self.gasta(1)
        self.assertFalse(agenda._conta_livre("2", self.dados, self.dia))


class ChamarOrqUmaVez(unittest.TestCase):
    def test_mesmo_freio_e_motivo_chama_o_orquestrador_uma_vez(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch("hub.agenda._chamar_orq") as chamar:
            self.assertTrue(agenda._chamar_orq_uma_vez("KRN-1", "teto", "freio da fila", d))
            self.assertFalse(agenda._chamar_orq_uma_vez("KRN-1", "teto", "freio da fila", d))
            self.assertTrue(agenda._chamar_orq_uma_vez("KRN-1", "outro motivo", "freio da fila", d))
        self.assertEqual(2, chamar.call_count)
