import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hub import config, contas_login

SCRIPT = textwrap.dedent("""
    import os, subprocess, sys, time
    print("\\x1b[1mAbra este endereco para entrar:\\x1b[0m", flush=True)
    print("https://login.exemplo.test/oauth/authorize?code=true&state=abc123", flush=True)
    print("Codigo do aparelho: ABCD-1234", flush=True)
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if len(sys.argv) > 1:
        open(sys.argv[1], "w").write(str(filho.pid))
    open(os.environ["CLAUDE_CONFIG_DIR"] + "/ambiente.txt", "w").write(" ".join(sorted(os.environ)))
    for linha in sys.stdin:
        open(os.environ["CLAUDE_CONFIG_DIR"] + "/recebido.txt", "w").write(linha)
    time.sleep(60)
""")


def espera(cond, tempo=5.0):
    fim = time.monotonic() + tempo
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.02)
    return False


def vivo(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zumbi conta como morto
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.home, self.script, self.pidarq = d / "home", d / "falso.py", d / "neto.pid"
        self.home.mkdir()
        self.script.write_text(SCRIPT)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KROHNOS_")}
        env["KROHNOS_CONFIG"] = str(d / "krohnos.toml")
        env["KROHNOS_CAMINHOS_DADOS"] = str(d / "dados")
        env["PAINEL_SENHA"] = "senha-do-painel"
        env["ANTHROPIC_API_KEY"] = "chave-de-api-falsa"
        env.update(HTTPS_PROXY="http://proxy.example.test:3128", NO_PROXY="localhost", SSL_CERT_FILE="/tmp/ca.pem",
                   XDG_CONFIG_HOME="/tmp/xdg")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)
        config.gravar({"agentes.contas": ["1"]})  # sem toml valeria o legado da VPS (4 contas)
        self.addCleanup(self.encerrar)

    def encerrar(self):
        for lg in list(contas_login.LOGINS.values()):
            contas_login.cancelar(lg.id)
        contas_login.LOGINS.clear()

    def inicia(self, n="2", **k):
        k.setdefault("intervalo", 0.05)
        return contas_login.iniciar(n, home=self.home, comando=[sys.executable, str(self.script), str(self.pidarq)], **k)

    def credencial(self, n="2", texto='{"claudeAiOauth": {"accessToken": "SEGREDO-NAO-VAZA", "expiresAt": 9999999999999}}'):
        pasta = self.home / f".claude-{n}"
        pasta.mkdir(exist_ok=True)
        (pasta / ".credentials.json").write_text(texto)


class ParserTest(unittest.TestCase):
    def test_link_e_codigo_com_ansi_e_texto_em_volta(self):
        c = contas_login.Captura()
        for linha in ("\x1b[2KVisit: https://x.example.test/a?b=1&c=2.", "Enter code: WXYZ-9876 to continue"):
            c.alimentar(linha)
        self.assertEqual((c.link, c.codigo), ("https://x.example.test/a?b=1&c=2", "WXYZ-9876"))

    def test_code_true_da_url_nao_vira_codigo(self):
        c = contas_login.Captura()
        c.alimentar("https://x.example.test/authorize?code=true&state=abc")
        self.assertEqual(c.codigo, "")
        self.assertTrue(c.link)

    def test_ruido_nao_vira_nada(self):
        c = contas_login.Captura()
        for linha in ("", "Paste code here if prompted >", "Opening browser..."):
            c.alimentar(linha)
        self.assertEqual((c.link, c.codigo), ("", ""))

    def test_token_na_saida_nunca_e_guardado(self):
        c = contas_login.Captura()
        c.alimentar("https://x.example.test/?t=sk-ant-oat01-ABCDEFG")
        c.alimentar("code: sk-ant-oat01-ABCDEFGHIJ")
        self.assertNotIn("sk-ant", c.link + c.codigo)


class SituacaoTest(Base):
    def test_sem_arquivo_ok_e_expirado(self):
        self.assertEqual(contas_login.situacao("2", self.home), "sem_login")
        self.credencial()
        self.assertEqual(contas_login.situacao("2", self.home), "ok")
        self.credencial(texto='{"claudeAiOauth": {"accessToken": "x", "expiresAt": 1000}}')
        self.assertEqual(contas_login.situacao("2", self.home), "expirado")
        self.credencial(texto='{"claudeAiOauth": {"accessToken": "x", "refreshToken": "y", "expiresAt": 1000}}')
        self.assertEqual(contas_login.situacao("2", self.home), "ok")  # a CLI renova sozinha
        self.credencial(texto="lixo")
        self.assertEqual(contas_login.situacao("2", self.home), "expirado")

    def test_conta_1_e_a_pasta_claude(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "x", "expiresAt": 9999999999999}}')
        self.assertEqual(contas_login.situacao("1", self.home), "ok")

    def test_proximo_numero(self):
        self.assertEqual(contas_login.proximo_numero(self.home), "2")
        config.gravar({"agentes.contas": ["1", "2", "4"]})
        self.assertEqual(contas_login.proximo_numero(self.home), "5")


class LoginTest(Base):
    def test_mostra_link_e_codigo_do_processo_falso(self):
        lg = self.inicia()
        self.assertTrue(espera(lambda: lg.link and lg.codigo))
        self.assertEqual(lg.link, "https://login.exemplo.test/oauth/authorize?code=true&state=abc123")
        self.assertEqual(lg.codigo, "ABCD-1234")
        self.assertEqual(lg.estado, "aguardando")
        self.assertTrue((self.home / ".claude-2").is_dir())
        self.assertIs(contas_login.obter(lg.id), lg)

    def test_filho_roda_com_a_pasta_da_conta(self):
        lg = self.inicia()
        self.assertTrue(espera(lambda: lg.codigo))
        self.assertTrue(contas_login.enviar_codigo(lg.id, "abc-DEF_123456"))
        arq = self.home / ".claude-2" / "recebido.txt"  # o falso grava no CLAUDE_CONFIG_DIR que recebeu
        self.assertTrue(espera(lambda: arq.exists() and arq.read_text().strip()))
        self.assertEqual(arq.read_text().strip(), "abc-DEF_123456")

    def test_codigo_colado_invalido_e_recusado(self):
        lg = self.inicia()
        for ruim in ("", "a b", "x" * 400, "a\nb", "$(rm)"):
            self.assertFalse(contas_login.enviar_codigo(lg.id, ruim))

    def test_credencial_aparece_estado_ok_e_conta_registrada(self):
        lg = self.inicia(faixa="operador")
        self.assertTrue(espera(lambda: lg.link))
        self.credencial()
        self.assertTrue(espera(lambda: lg.estado == "ok"))
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])
        self.assertEqual(config.get("agentes.faixas")["2"], "operador")
        self.assertTrue(espera(lambda: lg.proc.poll() is not None))  # o filho não fica à toa

    def test_credencial_antiga_nao_conta_como_login_novo(self):
        self.credencial()  # entrar de novo numa conta que já tinha arquivo
        lg = self.inicia()
        time.sleep(0.3)
        self.assertEqual(lg.estado, "aguardando")
        self.credencial(texto='{"claudeAiOauth": {"accessToken": "novo-valor-maior", "expiresAt": 9999999999999}}')
        self.assertTrue(espera(lambda: lg.estado == "ok"))

    def test_cancelar_mata_o_filho_e_o_grupo(self):
        lg = self.inicia()
        self.assertTrue(espera(lambda: lg.codigo and self.pidarq.exists()))
        neto = int(self.pidarq.read_text())
        self.assertTrue(vivo(neto))
        self.assertTrue(contas_login.cancelar(lg.id))
        self.assertEqual(lg.estado, "cancelado")
        self.assertTrue(espera(lambda: lg.proc.poll() is not None))
        self.assertTrue(espera(lambda: not vivo(neto)))
        self.assertEqual(config.get("agentes.contas"), ["1"])  # nada registrado

    def test_timeout_mata_e_marca(self):
        lg = self.inicia(timeout=0.4)
        self.assertTrue(espera(lambda: lg.estado == "tempo"))
        self.assertTrue(espera(lambda: lg.proc.poll() is not None))

    def test_filho_que_morre_sem_credencial_e_erro(self):
        lg = contas_login.iniciar("2", home=self.home, comando=[sys.executable, "-c", "print('falhou')"], intervalo=0.05)
        self.assertTrue(espera(lambda: lg.estado == "erro"))

    def test_comando_inexistente_vira_erro_sem_quebrar(self):
        lg = contas_login.iniciar("2", home=self.home, comando=["/nao/existe/claude-falso"], intervalo=0.05)
        self.assertEqual(lg.estado, "erro")

    def test_numero_invalido_e_recusado(self):
        for ruim in ("", "../x", "codex", "api", "0", "1000", "a"):
            with self.assertRaises(ValueError):
                contas_login.iniciar(ruim, home=self.home, comando=["true"])

    def test_filho_nao_recebe_segredos_do_ambiente(self):
        lg = self.inicia()
        arq = self.home / ".claude-2" / "ambiente.txt"
        self.assertTrue(espera(lambda: arq.exists() and arq.read_text().strip()))
        nomes = arq.read_text().split()
        self.assertIn("CLAUDE_CONFIG_DIR", nomes)
        self.assertIn("PATH", nomes)
        for n in ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "XDG_CONFIG_HOME"):
            self.assertIn(n, nomes)
        self.assertFalse([n for n in nomes if n.startswith("PAINEL_") or n in ("ANTHROPIC_API_KEY", "KROHNOS_CONFIG")], nomes)

    def test_cancelar_durante_a_gravacao_chega_tarde_e_conclui(self):
        solta, dentro = threading.Event(), threading.Event()

        def devagar(n, faixa):
            dentro.set()
            solta.wait(5)
        lg = self.inicia(ao_concluir=devagar)
        self.assertTrue(espera(lambda: lg.link))
        self.credencial()
        self.assertTrue(dentro.wait(5))
        self.assertEqual(lg.estado, "concluindo")
        self.assertFalse(contas_login.cancelar(lg.id))  # chegou tarde: a conta já entrou na config
        solta.set()
        self.assertTrue(espera(lambda: lg.estado == "ok"))

    def test_estado_final_nao_volta(self):
        lg = self.inicia()
        self.assertTrue(contas_login.cancelar(lg.id))
        self.assertFalse(contas_login.cancelar(lg.id))
        self.assertFalse(contas_login._mover(lg, ("aguardando",), "erro"))
        self.assertEqual(lg.estado, "cancelado")

    def test_matar_escala_para_sigkill_no_grupo(self):
        teimoso = ("import os,subprocess,sys,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   "f=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(60)'])\n"
                   f"open({str(self.pidarq)!r},'w').write(str(f.pid))\nprint('https://x.example.test/a', flush=True)\ntime.sleep(60)\n")
        lg = contas_login.iniciar("2", home=self.home, comando=[sys.executable, "-c", teimoso], intervalo=0.05)
        self.assertTrue(espera(lambda: lg.link and self.pidarq.exists()))
        neto = int(self.pidarq.read_text())
        self.assertTrue(contas_login.cancelar(lg.id))
        self.assertTrue(espera(lambda: not vivo(neto), 6))
        self.assertIsNotNone(lg.proc.poll())

    def test_dois_registros_ao_mesmo_tempo_nao_perdem(self):
        ts = [threading.Thread(target=contas_login.registrar_conta, args=(str(n), "operador")) for n in range(2, 8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(config.get("agentes.contas")), ["1", "2", "3", "4", "5", "6", "7"])
        self.assertEqual({n: f for n, f in config.get("agentes.faixas").items() if n != "1"}, dict.fromkeys("234567", "operador"))

    def test_um_login_por_conta(self):
        a = self.inicia()
        b = self.inicia()
        self.assertIs(a, b)

    def test_resumo_nao_tem_token(self):
        lg = self.inicia()
        self.assertTrue(espera(lambda: lg.codigo))
        self.credencial()
        self.assertTrue(espera(lambda: lg.estado == "ok"))
        self.assertNotIn("SEGREDO-NAO-VAZA", repr(lg.resumo()))
        self.assertNotIn("SEGREDO-NAO-VAZA", repr(vars(lg).get("captura", "")))


if __name__ == "__main__":
    unittest.main()
