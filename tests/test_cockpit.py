import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import cockpit

AGORA = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        cockpit._MEMO.clear()
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.pedidos_bot = Path(t.name, "pedidos.json")  # não existe até o teste gravar: nunca lê /opt/asite01-wpp
        self.home = Path(t.name, "home")  # contas com login: 2 e 3 (a 4 entra em test_quatro_contas)
        for n in "23":
            (self.home / f".claude-{n}").mkdir(parents=True)
            (self.home / f".claude-{n}" / ".credentials.json").write_text("{}")
        env = mock.patch.dict(os.environ, {"PEDIDOS_BOT": str(self.pedidos_bot), "CLAUDE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)


class AgentesTest(Base):
    def setUp(self):
        super().setUp()
        # registros com `inicio` fixo e o pid deste teste: sem a hora do boot, não parecem pid reaproveitado
        p = mock.patch("hub.agenda._boot", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def test_tres_contas_com_o_que_cada_uma_faz(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "claude")
            c.mkdir()
            log = Path(d, "x.stream.jsonl")
            log.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}}) + "\n", encoding="utf-8")
            (c / "rodando-2.json").write_text(json.dumps({"nome": "linear-krn-16", "conta": "2", "pid": os.getpid(),
                                                          "inicio": "2026-10-09T11:30:00+00:00", "log": str(log)}))
            (c / "uso-contas.json").write_text(json.dumps({"contas": {"2": {"cinco_horas": {"pct": 40}, "semana": {"pct": 20},
                                                                            "fonte": "oauth"}}}))
            a = cockpit.agentes(d, AGORA)
        self.assertEqual([x["conta"] for x in a], ["1", "2", "3"])
        self.assertEqual({x["faixa"] for x in a}, {"construtor"})
        self.assertIsNone(a[0]["tarefa"])
        self.assertEqual((a[1]["issue"], a[1]["minutos"], a[1]["cinco_horas"]), ("KRN-16", 30, 40))
        self.assertEqual(a[1]["ultimos_passos"], ["[Bash] npm test"])

    def test_quatro_contas_quando_a_4_tem_login(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "claude").mkdir()
            (self.home / ".claude-4").mkdir()
            self.assertEqual([x["conta"] for x in cockpit.agentes(d, AGORA)], ["1", "2", "3"])  # pasta sem login
            (self.home / ".claude-4" / ".credentials.json").write_text("{}")
            (Path(d, "claude") / "rodando-4.json").write_text(json.dumps({"nome": "aio-fsite05", "conta": "4", "pid": os.getpid(),
                                                                         "inicio": "2026-10-09T11:50:00+00:00"}))
            a = cockpit.agentes(d, AGORA)
        self.assertEqual([(x["conta"], x["faixa"]) for x in a],
                         [("1", "construtor"), ("2", "construtor"), ("3", "construtor"), ("4", "operador")])
        self.assertEqual((a[3]["tarefa"], a[3]["minutos"]), ("aio-fsite05", 10))

    def test_passos_sem_credencial(self):
        cmd = "export DATABASE_URL=postgres://clienteh:krn18@127.0.0.1:55499/clienteh"
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "claude")
            c.mkdir()
            log = Path(d, "x.stream.jsonl")
            log.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n", encoding="utf-8")
            (c / "rodando-1.json").write_text(json.dumps({"nome": "linear-krn-18", "conta": "1", "pid": os.getpid(),
                                                          "log": str(log)}))
            a = cockpit.agentes(d, AGORA)
        self.assertEqual(a[0]["ultimos_passos"],
                         ["[Bash] export DATABASE_URL=postgres://clienteh:•••@127.0.0.1:55499/clienteh"])

    def test_sem_segredo_mascara_cada_forma(self):
        casos = {
            "export DATABASE_URL=postgres://clienteh:krn18@127.0.0.1:55499/clienteh":
                "export DATABASE_URL=postgres://clienteh:•••@127.0.0.1:55499/clienteh",
            "git clone https://x-access-token:abc@github.com/a/b": "git clone https://x-access-token:•••@github.com/a/b",
            "GITHUB_TOKEN=abc123 gh pr list": "GITHUB_TOKEN=••• gh pr list",
            "export PGPASSWORD='s3 cret' && psql": "export PGPASSWORD=••• && psql",
            "client_secret: xyz": "client_secret: •••",
            "SENHA=x PWD=y AUTH_X=z CREDENTIALS=w": "SENHA=••• PWD=••• AUTH_X=••• CREDENTIALS=•••",
            "psql --password=hunter2 db": "psql --password=••• db",
            "tool --api-key hunter2 run": "tool --api-key ••• run",
            "curl -H 'Authorization: Bearer xyz' https://x": "curl -H 'Authorization: •••' https://x",
            "curl -H 'authorization: token abc' x": "curl -H 'authorization: •••' x",
            "Bearer abc.def": "Bearer •••",
            "echo ghp_abc gho_x ghs_y github_pat_11AB_cd": "echo ••• ••• ••• •••",
            "sk-ant-api03-abcdefghijk lin_api_xyz xoxb-12-ab eyJhbGc.eyJzdWIi.sig": "••• ••• ••• •••",
            # rodada 3
            "psql postgres://u:p/ss@h/db": "psql postgres://u:•••@h/db",
            "curl -u user:pw https://x": "curl -u user:••• https://x",  # gitleaks:allow
            "curl --user=bob:s3c https://x": "curl --user=bob:••• https://x",
            "curl -u 'bob:pw' x": "curl -u 'bob:•••' x",
            "mysql -pSECRET db": "mysql -p••• db",
            "mysqldump -u root -p'x y' db": "mysqldump -u root -p••• db",
            "password = abc": "password = •••",
            "api_key : abc": "api_key : •••",
            '{"password":"abc","user":"x"}': '{"password":•••,"user":"x"}',
            "{'api_key': 'x'}": "{'api_key': •••}",
            "AUTH=x X_AUTH=y AUTH_TOKEN=z authorization=w": "AUTH=••• X_AUTH=••• AUTH_TOKEN=••• authorization=•••",
        }
        for cru, limpo in casos.items():
            self.assertEqual(cockpit._sem_segredo(cru), limpo, cru)
        for inofensivo in ("[Bash] npm test", "[Read] /home/nimbus/hub/keys.py", "[Bash] git push origin main",
                           "[Bash] git clone https://github.com/example-org/x.git", "[Edit] hub/painel.py", "[Bash] ssh-keygen -t ed25519",
                           "author: Fulano", "[Bash] git log --author=Fulano", "[Bash] mysql -p db", "[Bash] ls -p dir",
                           "[Bash] sort -u lista", "echo user2@example.com https://site-com.example/x"):
            self.assertEqual(cockpit._sem_segredo(inofensivo), inofensivo)

    def test_sem_segredo_linha_longa_e_rapido(self):
        import time
        t0 = time.monotonic()
        cockpit._sem_segredo("a-" * 10000)
        self.assertLess(time.monotonic() - t0, 1)

    def test_inicio_malformado_nao_derruba(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "claude")
            c.mkdir()
            (c / "rodando-2.json").write_text(json.dumps({"nome": "linear-krn-16", "conta": "2", "pid": os.getpid(),
                                                          "inicio": "ontem"}))
            a = cockpit.agentes(d, AGORA)
        self.assertEqual((a[1]["issue"], a[1]["minutos"]), ("KRN-16", None))


class PendenciasTest(Base):
    def test_pedidos_pendentes_e_perguntas_dos_agentes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "propostas")
            p.mkdir()
            (p / "a.json").write_text(json.dumps({"id": "a", "estado": "enviada", "resumo": "e-mail ao cliente", "tipo": "email",
                                                  "criada_em": "2026-10-09T10:00:00+00:00"}))
            # pedido de servidor/merge é do orquestrador: não entra na lista do Fulano (10/10)
            (p / "c.json").write_text(json.dumps({"id": "c", "estado": "pendente", "resumo": "x", "tipo": "vps"}))
            (p / "b.json").write_text(json.dumps({"id": "b", "estado": "feita", "resumo": "x", "tipo": "vps",
                                                  "criada_em": "2026-10-09T09:00:00+00:00"}))

            def gql(q, app=False, **v):
                if "agentSessions" in q:
                    return {"agentSessions": {"nodes": [{"status": "awaitingInput", "issue": {
                        "identifier": "KRN-30", "title": "t", "url": "u"}, "activities": {"nodes": [
                        {"createdAt": "2", "content": {"__typename": "AgentActivityElicitationContent", "body": "Posso seguir?"}}]}}]}}
                return {"issues": {"nodes": []}}
            self.pedidos_bot.write_text(json.dumps({"seq": 33, "itens": {"33": {"estado": "aguardando", "plano": {
                "tipo": "proposta", "id": "a"}}}}))
            r = cockpit.pendencias(d, AGORA, gql=gql)
        self.assertEqual([x["n"] for x in r["pedidos"]], [33])
        self.assertEqual(r["perguntas"][0]["pergunta"], "Posso seguir?")


class NumerosDoBotTest(Base):
    def test_so_pedido_aguardando_de_proposta_tem_numero(self):
        self.pedidos_bot.write_text(json.dumps({"seq": 34, "itens": {
            "31": {"estado": "aguardando", "plano": {"tipo": "proposta", "id": "p31"}},
            "32": {"estado": "feito", "plano": {"tipo": "proposta", "id": "p32"}},
            "33": {"estado": "aguardando", "plano": {"tipo": "outro", "id": "p33"}},
            "34": {"estado": "planejando"}}}))
        self.assertEqual(cockpit._numeros_do_bot(), {"p31": 31})

    def test_arquivo_ausente_ou_estragado_da_vazio(self):
        self.assertEqual(cockpit._numeros_do_bot(), {})
        for ruim in ("{nao e json", "[]", '{"itens": []}', '{"itens": {"x": {"estado": "aguardando", "plano": {"tipo": "proposta", "id": "p"}}}}',
                     '{"itens": {"1": {"estado": "aguardando", "plano": {"tipo": "proposta"}}}}', '{"itens": {"1": null}}'):
            self.pedidos_bot.write_text(ruim)
            self.assertEqual(cockpit._numeros_do_bot(), {}, ruim)


class IssueTest(unittest.TestCase):
    def test_so_identificador_de_issue(self):
        self.assertEqual(cockpit._issue("linear-krn-16"), "KRN-16")
        self.assertIsNone(cockpit._issue("linear-retro"))
        self.assertIsNone(cockpit._issue(None))


class CacheTest(Base):
    def test_cache_de_60s(self):
        chamadas = []
        with mock.patch.object(cockpit.time, "monotonic", side_effect=[0, 10, 100]):
            for _ in range(3):
                cockpit._cache("k", 60, lambda: chamadas.append(1) or len(chamadas))
        self.assertEqual(len(chamadas), 2)


class TravadasTest(Base):
    def test_backlog_e_em_andamento_parada(self):
        def no(i, estado, upd, labels=()):
            return {"id": i, "identifier": i, "title": "t", "url": "u", "updatedAt": upd, "state": {"name": estado},
                    "labels": {"nodes": [{"name": x} for x in labels]}}
        nos = [no("B1", "Backlog", "2026-10-09T11:00:00Z"), no("B2", "Backlog", "2026-10-09T11:00:00Z", ["esteira"]),
               no("B3", "Backlog", "2026-10-09T11:00:00Z"),  # ideia guardada pelo Fulano: sem falha, não é travada
               no("P1", "In Progress", "2026-10-09T05:00:00Z"), no("P2", "In Progress", "2026-10-09T10:00:00Z")]
        falhas = [{"identifier": "B1", "comments": {"nodes": [{"body": "Falhou (conta 2):\n\n```\nboom\n```"}]}},
                  {"identifier": "B3", "comments": {"nodes": [{"body": "lembrar de ver isso"}]}}]

        def gql(q, app=False, **v):
            if "agentSessions" in q:
                return {"agentSessions": {"nodes": []}}
            return {"issues": {"nodes": falhas if "comments" in q else [] if "plano-" in q else nos}}
        with tempfile.TemporaryDirectory() as d:
            r = cockpit.pendencias(d, AGORA, gql=gql)
        self.assertEqual([x["identifier"] for x in r["travadas"]], ["B1", "P1"])
        self.assertIn("3 h", r["travadas"][1]["motivo"])
        self.assertIn("falhou", r["travadas"][0]["motivo"])

    def test_em_andamento_com_execucao_rodando_ou_na_fila_nao_e_travada(self):
        no = lambda i: {"id": i, "identifier": i, "title": "t", "url": "u", "updatedAt": "2026-10-09T05:00:00Z",  # noqa: E731
                        "state": {"name": "In Progress"}, "labels": {"nodes": []}}
        gql = lambda q, app=False, **v: ({"agentSessions": {"nodes": []}} if "agentSessions" in q  # noqa: E731
                                         else {"issues": {"nodes": [] if "comments" in q or "plano-" in q else [no("R1"), no("F1"), no("P1")]}})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cockpit.agenda, "rodando_todas", return_value={"x.json": {"nome": "linear-r1"}}), \
                mock.patch.object(cockpit.agenda, "fila", return_value=[{"nome": "linear-f1"}]):
            r = cockpit.pendencias(d, AGORA, gql=gql)
        self.assertEqual([x["identifier"] for x in r["travadas"]], ["P1"])

    def test_por_estado_sempre_com_os_4_estados(self):
        gql = lambda q, app=False, **v: {"issues": {"nodes": []}}  # noqa: E731
        with tempfile.TemporaryDirectory() as d:
            r = cockpit.linear_resumo(AGORA, gql, d)
        self.assertEqual(r["por_estado"], {"Todo": 0, "In Progress": 0, "In Review": 0, "Backlog": 0})
        self.assertEqual(set(r), {"por_estado", "issues", "planos", "esteira"})
        self.assertEqual(r["esteira"], {"estoque": 0, "despachadas_7d": 0})


class PlanosTest(Base):
    def test_progresso_conta_concluidas_e_omite_plano_pronto(self):
        def no(tipo, nome, *labels):
            return {"identifier": "X", "state": {"name": nome, "type": tipo}, "labels": {"nodes": [{"name": x} for x in labels]}}
        nos = ([no("completed", "Done", "plano-a")] * 8 + [no("started", "In Review", "plano-a")] * 2 +
               [no("unstarted", "Todo", "plano-a"), no("canceled", "Canceled", "plano-a"), no("completed", "Done", "plano-b")])

        def gql(q, app=False, **v):
            return {"issues": {"nodes": nos if "plano-" in q else []}}
        with tempfile.TemporaryDirectory() as d:
            r = cockpit.linear_resumo(AGORA, gql, d)
        self.assertEqual(r["planos"], [{"label": "plano-a", "feitas": 10, "total": 11}])
