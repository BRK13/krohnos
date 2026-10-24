import json
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import hub
from hub import cofre, conexoes, verificar_fontes

SEGREDO = "sk-valor-secreto-ABCD1234wxyz"
CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
CONTA = {"type": "service_account", "client_email": "user10@example.com", "private_key": CHAVE}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(cofre, "_pasta", lambda: Path(self.tmp.name) / "cofre"),
                  mock.patch.object(hub, "data", lambda: Path(self.tmp.name) / "dados"),
                  mock.patch.dict(os.environ, {"KROHNOS_CHAVE_MESTRA": Fernet.generate_key().decode()})):
            p.start()
            self.addCleanup(p.stop)


def falso(codigo=200, texto="{}", exc=None, rotas=None):
    """Transporte falso. `rotas` {trecho da url: (codigo, texto)} vence o padrão. Guarda as chamadas em .chamadas."""
    chamadas = []

    def t(metodo, url, headers=None, corpo=None, timeout=30):
        chamadas.append((metodo, url, headers or {}, corpo))
        if exc:
            raise exc
        for trecho, resp in (rotas or {}).items():
            if trecho in url:
                return resp
        return codigo, texto
    t.chamadas = chamadas
    return t


# conexão -> (preparação no cofre/estado, resposta de sucesso)
def _prep_linear():
    cofre.definir("linear.token", SEGREDO, por="t")


def _prep_github():
    cofre.definir("github.token", SEGREDO, por="t")
    conexoes.guardar_campo("github.org", "org-exemplo")


def _prep_google():
    conexoes.salvar_conta_servico(json.dumps(CONTA, indent=2), "t")


def _prep_pagespeed():
    cofre.definir("pagespeed.chave", SEGREDO, por="t")


def _prep_dataforseo():
    cofre.definir("dataforseo.login", "login@exemplo.test", por="t")
    cofre.definir("dataforseo.senha", SEGREDO, por="t")


def _prep_bing():
    cofre.definir("bing.chave", SEGREDO, por="t")


def _prep_clarity():
    conexoes.guardar_campo("clarity.projeto", "loja-exemplo")


OK = {
    "linear": (_prep_linear, (200, json.dumps({"data": {"viewer": {"id": "1", "name": "Ana"}, "teams": {"nodes": [{"key": "ENG"}]}}}))),
    "github": (_prep_github, (200, "{}")),
    "pagespeed": (_prep_pagespeed, (200, json.dumps({"lighthouseResult": {"categories": {"performance": {"score": 0.91}}}}))),
    "dataforseo": (_prep_dataforseo, (200, json.dumps({"status_code": 20000, "tasks": [{"result": [{"money": {"balance": 12.5}}]}]}))),
    "bing": (_prep_bing, (200, json.dumps({"d": [{"Url": "https://exemplo-loja.test"}]}))),
}
HTTP = tuple(OK)


class TestesPorConexao(Base):
    def test_sucesso(self):
        for nome in HTTP:
            with self.subTest(nome):
                OK[nome][0]()
                ok, msg = conexoes.testar(nome, falso(*OK[nome][1]))
                self.assertTrue(ok, msg)
                self.assertTrue(conexoes.ultimo_teste(nome)["ok"])

    def test_401_429_timeout_e_rede_em_portugues_sem_segredo(self):
        casos = {"401": falso(401), "429": falso(429), "403": falso(403), "500": falso(503),
                 "timeout": falso(exc=socket.timeout("lento " + SEGREDO)),
                 "timeout2": falso(exc=urllib.error.URLError(socket.timeout(SEGREDO))),
                 "timeout3": falso(exc=TimeoutError(SEGREDO)),
                 "rede": falso(exc=urllib.error.URLError("falha com " + SEGREDO))}
        for nome in HTTP:
            OK[nome][0]()
            for rotulo, t in casos.items():
                with self.subTest(f'{nome}-{rotulo}'):
                    ok, msg = conexoes.testar(nome, t)
                    self.assertFalse(ok)
                    self.assertNotIn(SEGREDO, msg)
                    self.assertNotIn("ABCD1234", msg)
                    self.assertNotRegex(msg, r"(?i)\b(rate limit|unauthorized|timed out)\b")
                    self.assertIn(conexoes.titulo(nome), msg)
                    esperado = {"401": "401", "403": "403", "429": "429", "500": "503", "timeout": "a tempo", "timeout2": "a tempo", "timeout3": "a tempo",
                                "rede": "rede"}[rotulo]
                    self.assertIn(esperado, msg)
                    self.assertFalse(conexoes.ultimo_teste(nome)["ok"])
        self.assertNotIn(SEGREDO, (Path(self.tmp.name) / "dados" / "conexoes.json").read_text())

    def test_sem_credencial_diz_o_que_falta(self):
        for nome in ("linear", "github", "dataforseo", "bing"):
            with self.subTest(nome):
                ok, msg = conexoes.testar(nome, falso())
                self.assertFalse(ok)
                self.assertRegex(msg, "Falta")

    def test_linear_200_com_errors_vale_como_recusa(self):
        _prep_linear()
        ok, msg = conexoes.testar("linear", falso(200, json.dumps({"errors": [{"message": "x"}]})))
        self.assertFalse(ok)
        self.assertIn("sem o usuário", msg)

    def test_linear_time_que_nao_aparece(self):
        _prep_linear()
        conexoes.guardar_campo("linear.time", "OUTRO")
        ok, msg = conexoes.testar("linear", falso(*OK["linear"][1]))
        self.assertFalse(ok)
        self.assertIn("OUTRO", msg)
        conexoes.guardar_campo("linear.time", "ENG")
        self.assertTrue(conexoes.testar("linear", falso(*OK["linear"][1]))[0])

    def test_linear_manda_a_chave_pessoal_sem_bearer(self):
        _prep_linear()
        t = falso(*OK["linear"][1])
        conexoes.testar("linear", t)
        self.assertEqual(t.chamadas[0][2]["Authorization"], SEGREDO)

    def test_github_sem_org_pede_a_org(self):
        cofre.definir("github.token", SEGREDO, por="t")
        with mock.patch.object(conexoes.config, "get", lambda k: ""):
            ok, msg = conexoes.testar("github", falso())
        self.assertFalse(ok)
        self.assertIn("organização", msg)

    def test_pagespeed_sem_chave_funciona(self):
        ok, msg = conexoes.testar("pagespeed", falso(*OK["pagespeed"][1]))
        self.assertTrue(ok)
        self.assertIn("anônima", msg)

    def test_dataforseo_usa_user_data_gratis(self):
        _prep_dataforseo()
        t = falso(*OK["dataforseo"][1])
        conexoes.testar("dataforseo", t)
        self.assertIn("appendix/user_data", t.chamadas[0][1])
        self.assertEqual(t.chamadas[0][0], "GET")
        self.assertTrue(conexoes.CONEXOES["dataforseo"]["paga"])

    def test_bing_chave_na_url_nao_vaza_na_mensagem(self):
        _prep_bing()
        ok, msg = conexoes.testar("bing", falso(500))
        self.assertNotIn(SEGREDO, msg)

    def test_limpar_troca_valor_que_escapou_via_testar(self):
        _prep_bing()
        with mock.patch.object(conexoes, "erro_http", lambda n, c: f"Bing vazou {SEGREDO} aqui"):
            ok, msg = conexoes.testar("bing", falso(500))
        self.assertFalse(ok)
        self.assertNotIn(SEGREDO, msg)
        self.assertIn("••••", msg)
        self.assertNotIn(SEGREDO, json.dumps(conexoes.ler_estado()))

    def test_excecao_desconhecida_com_segredo_na_mensagem(self):
        _prep_bing()
        ok, msg = conexoes.testar("bing", falso(exc=RuntimeError("falhou " + SEGREDO)))
        self.assertFalse(ok)
        self.assertNotIn(SEGREDO, msg)
        self.assertIn("RuntimeError", msg)

    def test_linear_errors_autenticacao_x_limite(self):
        _prep_linear()
        def corpo(tipo):
            return (200, json.dumps({"errors": [{"extensions": {"type": tipo}}]}))
        ok, msg = conexoes.testar("linear", falso(*corpo("authentication error")))
        self.assertIn("401", msg)
        ok, msg = conexoes.testar("linear", falso(*corpo("ratelimited")))
        self.assertIn("429", msg)
        ok, msg = conexoes.testar("linear", falso(*corpo("outra coisa")))
        self.assertFalse(ok)
        self.assertNotIn("401", msg)
        self.assertNotIn("429", msg)

    def test_linear_erro_graphql_com_status_nao_200(self):
        _prep_linear()
        def corpo(tipo):
            return json.dumps({"errors": [{"extensions": {"type": tipo}}]})
        self.assertIn("429", conexoes.testar("linear", falso(400, corpo("ratelimited")))[1])
        self.assertIn("401", conexoes.testar("linear", falso(400, corpo("authentication error")))[1])
        self.assertIn("401", conexoes.testar("linear", falso(401, ""))[1])
        self.assertIn("403", conexoes.testar("linear", falso(403, ""))[1])
        self.assertIn("429", conexoes.testar("linear", falso(429, ""))[1])
        ok, msg = conexoes.testar("linear", falso(400, corpo("outra")))
        self.assertIn("400", msg)

    def test_dataforseo_status_interno(self):
        _prep_dataforseo()
        r = lambda sc: falso(200, json.dumps({"status_code": sc}))  # noqa: E731
        self.assertIn("401", conexoes.testar("dataforseo", r(40101))[1])
        self.assertIn("saldo", conexoes.testar("dataforseo", r(40200))[1])
        ok, msg = conexoes.testar("dataforseo", r(50000))
        self.assertIn("50000", msg)
        self.assertNotIn("401", msg)

    def test_conexao_desconhecida_e_em_breve(self):
        self.assertEqual(conexoes.testar("nada"), (False, "Conexão desconhecida."))
        ok, msg = conexoes.testar("linear_oauth")
        self.assertFalse(ok)
        self.assertIn("em breve", msg)


class ClarityTest(Base):
    def prep(self, token="tok-clarity-123456"):  # gitleaks:allow (valor falso de teste)
        _prep_clarity()
        pasta = Path(self.tmp.name) / "cred" / "credenciais" / "clarity"
        pasta.mkdir(parents=True)
        if token:
            (pasta / "loja-exemplo.json").write_text(json.dumps({"token": token}))
        p = mock.patch.object(verificar_fontes, "home", lambda: Path(self.tmp.name) / "cred")
        p.start()
        self.addCleanup(p.stop)

    def test_casos(self):
        self.prep()
        self.assertTrue(conexoes.testar("clarity", falso(200))[0])
        ok, msg = conexoes.testar("clarity", falso(429))  # 429 NÃO é sucesso
        self.assertFalse(ok)
        self.assertIn("429", msg)
        self.assertIn("cota", msg)
        ok, msg = conexoes.testar("clarity", falso(401))
        self.assertFalse(ok)
        self.assertIn("401", msg)
        ok, msg = conexoes.testar("clarity", falso(exc=TimeoutError()))
        self.assertIn("a tempo", msg)
        self.assertNotIn("tok-clarity", msg)

    def test_sem_token_do_projeto(self):
        self.prep(token=None)
        ok, msg = conexoes.testar("clarity", falso(200))
        self.assertFalse(ok)
        self.assertIn("token", msg)


class GoogleTest(Base):
    GSC = {"siteEntry": [{"siteUrl": "sc-domain:exemplo-loja.test"}]}
    GA4 = {"accountSummaries": [{"propertySummaries": [{"property": "properties/1"}]}]}

    def rotas(self, token=(200, '{"access_token": "ya29.token-falso"}'), gsc=None, ga4=None):
        return {"oauth2.googleapis.com": token, "webmasters": gsc or (200, json.dumps(self.GSC)),
                "analyticsadmin": ga4 or (200, json.dumps(self.GA4))}

    def test_sucesso_e_json_vai_inteiro_ao_cofre(self):
        ok, msg = conexoes.salvar_conta_servico(json.dumps(CONTA, indent=2), "ana", "10.0.0.1")
        self.assertTrue(ok, msg)
        self.assertEqual(json.loads(cofre.ler("google.conta_servico")), CONTA)
        self.assertEqual(cofre.auditoria()[0]["nome"], "google.conta_servico")
        self.assertEqual(conexoes.email_conta_servico(), CONTA["client_email"])
        t = falso(rotas=self.rotas())
        ok, msg = conexoes.testar("google", t)
        self.assertTrue(ok, msg)
        self.assertIn("1 propriedade(s) no Search Console e 1 no GA4", msg)
        self.assertNotIn("PRIVATE KEY", msg)
        self.assertIn("jwt-bearer", t.chamadas[0][3].decode())

    def test_nao_enxerga_nada_orienta_dar_acesso(self):
        conexoes.salvar_conta_servico(json.dumps(CONTA), "t")
        ok, msg = conexoes.testar("google", falso(rotas=self.rotas(gsc=(200, "{}"), ga4=(200, "{}"))))
        self.assertFalse(ok)
        self.assertIn("Dê acesso", msg)

    def test_erros(self):
        conexoes.salvar_conta_servico(json.dumps(CONTA), "t")
        for nome, t, esperado in (("401", falso(rotas=self.rotas(token=(401, ""))), "401"),
                                  ("400", falso(rotas=self.rotas(token=(400, ""))), "400"),
                                  ("429", falso(rotas=self.rotas(gsc=(429, ""))), "429"),
                                  ("403", falso(rotas=self.rotas(ga4=(403, ""))), "403"),
                                  ("timeout", falso(exc=socket.timeout()), "a tempo")):
            with self.subTest(nome):
                ok, msg = conexoes.testar("google", t)
                self.assertFalse(ok)
                self.assertIn(esperado, msg)
                self.assertNotIn("PRIVATE KEY", msg)

    def test_json_invalido_recusado(self):
        ruins = ["", "isto não é json", "[1]", json.dumps({"type": "authorized_user", "client_email": "a@b.c"}),
                 json.dumps({**CONTA, "client_email": "sem-arroba"}), json.dumps({**CONTA, "client_email": None}),
                 json.dumps({k: v for k, v in CONTA.items() if k != "private_key"})]
        for r in ruins:
            with self.subTest(r[:30]):
                ok, msg = conexoes.salvar_conta_servico(r, "t")
                self.assertFalse(ok)
                self.assertNotIn("PRIVATE KEY", msg)
        self.assertEqual(cofre.listar(), [])


class CodexTest(Base):
    def cmd(self, versao=(0, "codex-cli 1.2.3"), chamada=(0, "ok"), exc=None):
        chamadas = []

        def c(args, env=None):
            chamadas.append((args, env))
            if exc:
                raise exc
            return versao if "--version" in args else chamada
        c.chamadas = chamadas
        return c

    def test_sucesso_com_chave_so_no_ambiente(self):
        cofre.definir("codex.chave", SEGREDO, por="t")
        c = self.cmd()
        ok, msg = conexoes.testar("codex", c)
        self.assertTrue(ok, msg)
        self.assertIn("1.2.3", msg)
        self.assertEqual(c.chamadas[0][0], ["codex", "--version"])
        self.assertEqual(c.chamadas[1][1], {"OPENAI_API_KEY": SEGREDO})
        self.assertIn("read-only", c.chamadas[1][0])
        self.assertIn("--sandbox", c.chamadas[1][0])
        self.assertNotIn(SEGREDO, " ".join(" ".join(a) for a, _ in c.chamadas))

    def test_ambiente_do_comando_e_minimo(self):
        cap = {}
        def fake_run(args, **k):
            cap.update(k)
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.dict(os.environ, {"OUTRO_SEGREDO": "z", "PATH": "/bin", "HOME": "/h"}), mock.patch("subprocess.run", fake_run):
            conexoes._comando(["codex", "--version"], {"OPENAI_API_KEY": SEGREDO})
        self.assertEqual(set(cap["env"]), {"PATH", "HOME", "OPENAI_API_KEY"})

    def test_substring_solta_nao_classifica(self):
        ok, msg = conexoes.testar("codex", self.cmd(chamada=(1, "processed 4290 tokens; id 1401abc; quota of files")))
        self.assertFalse(ok)
        self.assertNotIn("401", msg)
        self.assertNotIn("429", msg)

    def test_sem_chave_vale_login_do_aparelho(self):
        self.assertTrue(conexoes.testar("codex", self.cmd())[0])

    def test_erros(self):
        for rotulo, c, esperado in (("401", self.cmd(chamada=(1, "unexpected status 401 Unauthorized: " + SEGREDO)), "401"),
                                    ("429", self.cmd(chamada=(1, "ERROR: 429 Too Many Requests")), "429"),
                                    ("timeout", self.cmd(exc=__import__("subprocess").TimeoutExpired("codex", 1)), "a tempo"),
                                    ("ausente", self.cmd(exc=FileNotFoundError("codex")), "não está instalado"),
                                    ("versao", self.cmd(versao=(127, "")), "--version"),
                                    ("outro", self.cmd(chamada=(2, "boom")), "código 2")):
            with self.subTest(rotulo):
                ok, msg = conexoes.testar("codex", c)
                self.assertFalse(ok)
                self.assertIn(esperado, msg)
                self.assertNotIn(SEGREDO, msg)


class RedirectTest(Base):
    def test_302_nao_segue_e_nao_vaza_chave(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        vistos = []

        class Alvo(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                vistos.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
            log_message = lambda *a: None  # noqa: E731

        alvo = ThreadingHTTPServer(("127.0.0.1", 0), Alvo)
        porta = alvo.server_port

        class Origem(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{porta}/roubo")
                self.end_headers()
            log_message = lambda *a: None  # noqa: E731

        origem = ThreadingHTTPServer(("127.0.0.1", 0), Origem)
        for srv in (alvo, origem):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        codigo, _ = conexoes._http("GET", f"http://127.0.0.1:{origem.server_port}/x", {"Authorization": "Bearer " + SEGREDO})
        self.assertEqual(codigo, 302)
        self.assertEqual(vistos, [])

    def test_302_do_transporte_vira_mensagem_em_portugues(self):
        _prep_bing()
        ok, msg = conexoes.testar("bing", falso(302))
        self.assertFalse(ok)
        self.assertIn("302", msg)


class EnvTest(Base):
    # test_fontes_leem_gh_token_e_openai mora em tests/test_conexoes_fontes.py (lê hub/formulas_llm, fora do snapshot)

    def test_env_com_cofre_fechado_nao_estoura(self):
        import io
        from contextlib import redirect_stderr
        conexoes.salvar("linear", {"linear.token": SEGREDO}, "t")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"KROHNOS_CHAVE_MESTRA": Fernet.generate_key().decode()}), redirect_stderr(err):
            self.assertEqual(conexoes.env(), {})
        self.assertIn("cofre não abriu", err.getvalue())
        self.assertNotIn(SEGREDO, err.getvalue())

    def test_env_entrega_os_nomes_que_os_modulos_leem(self):
        from hub import linear, psi
        conexoes.salvar("linear", {"linear.token": SEGREDO}, "t")
        cofre.definir("pagespeed.chave", SEGREDO + "p", por="t")
        _prep_dataforseo()
        e = conexoes.env()
        self.assertEqual(e["LINEAR_API_KEY"], SEGREDO)
        self.assertEqual(e["PAGESPEED_CHAVE"], SEGREDO + "p")
        self.assertIn("DATAFORSEO_AUTH", e)
        import inspect
        from hub import dataforseo
        for nome, mod in (("LINEAR_API_KEY", linear), ("PAGESPEED_CHAVE", psi), ("DATAFORSEO_AUTH", dataforseo)):
            self.assertIn(nome, inspect.getsource(mod))
        for d in conexoes.CONEXOES.values():
            for c in d["campos"]:
                if c["segredo"] and c["id"] != "google.conta_servico" and not c["id"].startswith(("dataforseo", "bing")):  # bing: nenhum módulo lê ainda
                    self.assertTrue(c["env"], c["id"])


class EstadoTest(Base):
    def test_salvar_multicampo_valida_antes_de_gravar(self):
        ok, msg = conexoes.salvar("dataforseo", {"dataforseo.login": "login@exemplo.test"}, "t")
        self.assertFalse(ok)
        self.assertIn("Falta", msg)
        self.assertEqual(cofre.listar(), [])
        with mock.patch.object(conexoes.config, "get", lambda k: ""):
            ok, msg = conexoes.salvar("github", {"github.token": SEGREDO}, "t")
        self.assertFalse(ok)
        self.assertEqual(cofre.listar(), [])
        ok, msg = conexoes.salvar("linear", {"linear.token": "a\nb" + SEGREDO, "linear.time": "ENG"}, "t")
        self.assertFalse(ok)
        self.assertEqual(conexoes.campo("linear.time"), "")

    def test_segundo_segredo_falha_desfaz_o_primeiro(self):
        cofre.definir("dataforseo.login", "login-antigo@exemplo.test", por="t")
        real = cofre.definir

        def falha_na_senha(nome, valor, por, ip=""):
            if nome == "dataforseo.senha":
                raise cofre.CofreError("disco cheio")
            return real(nome, valor, por=por, ip=ip)
        with mock.patch.object(cofre, "definir", falha_na_senha):
            ok, msg = conexoes.salvar("dataforseo", {"dataforseo.login": "login-novo@exemplo.test", "dataforseo.senha": SEGREDO}, "t")
        self.assertFalse(ok)
        self.assertIn("Nada foi gravado", msg)
        self.assertEqual(cofre.ler("dataforseo.login"), "login-antigo@exemplo.test")
        self.assertIsNone(cofre.ler("dataforseo.senha"))
        # e quando o primeiro era novo: apaga
        cofre.apagar("dataforseo.login", por="t")
        with mock.patch.object(cofre, "definir", falha_na_senha):
            conexoes.salvar("dataforseo", {"dataforseo.login": "login-novo@exemplo.test", "dataforseo.senha": SEGREDO}, "t")
        self.assertEqual(cofre.listar(), [])

    def test_campo_obrigatorio_vazio_mantem_o_antigo(self):
        conexoes.guardar_campo("clarity.projeto", "loja-exemplo")
        self.assertTrue(conexoes.salvar("clarity", {"clarity.projeto": ""}, "t")[0])
        self.assertEqual(conexoes.campo("clarity.projeto"), "loja-exemplo")

    def test_escritas_concorrentes_nao_perdem_campos(self):
        import threading
        ts = [threading.Thread(target=conexoes.guardar_campo, args=(f"c.{i}", str(i))) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(conexoes.ler_estado()["campos"]), 12)

    def test_arquivo_atomico_sem_segredo_e_corrompido_vale_vazio(self):
        _prep_linear()
        conexoes.testar("linear", falso(401))
        arq = Path(self.tmp.name) / "dados" / "conexoes.json"
        self.assertNotIn(SEGREDO, arq.read_text())
        self.assertEqual(sorted(p.name for p in arq.parent.iterdir()), ["conexoes.json", "conexoes.lock"])  # sem .tmp sobrando
        arq.write_text("{quebrado")
        self.assertIsNone(conexoes.ultimo_teste("linear"))

    def test_salvar_grava_no_cofre_com_auditoria_e_vazio_mantem(self):
        ok, _ = conexoes.salvar("linear", {"linear.token": SEGREDO, "linear.time": "ENG"}, "ana", "10.0.0.1")
        self.assertTrue(ok)
        self.assertEqual(cofre.ler("linear.token"), SEGREDO)
        self.assertEqual(conexoes.campo("linear.time"), "ENG")
        a = cofre.auditoria()[0]
        self.assertEqual((a["acao"], a["nome"], a["quem"], a["ip"]), ("definir", "linear.token", "ana", "10.0.0.1"))
        conexoes.salvar("linear", {"linear.token": "", "linear.time": ""}, "ana")
        self.assertEqual(cofre.ler("linear.token"), SEGREDO)
        self.assertEqual(conexoes.campo("linear.time"), "")

    def test_nomes_do_cofre_do_catalogo(self):
        ids = {c["id"] for d in conexoes.CONEXOES.values() for c in d["campos"] if c["segredo"]}
        self.assertTrue({"linear.token", "github.token", "google.conta_servico", "pagespeed.chave", "dataforseo.login",
                         "dataforseo.senha", "bing.chave", "codex.chave"} <= ids)
        for d in conexoes.CONEXOES.values():
            if not d.get("em_breve"):
                self.assertTrue(d["o_que_e"] and d["passos"] and d["teste"])


if __name__ == "__main__":
    unittest.main()
