import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import claude


def saida(stdout, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        p = mock.patch.object(claude, "_trava", return_value=mock.MagicMock(__enter__=lambda s: True, __exit__=lambda *a: None))
        p.start()
        self.addCleanup(p.stop)
        # só a conta 1 com login e a reserva de API sem chave, salvo quando o teste muda
        for alvo, valor in (("com_login", ("1",)), ("bloqueio_api", "sem chave")):
            p = mock.patch.object(claude.conta, alvo, return_value=valor)
            setattr(self, alvo, p.start())
            self.addCleanup(p.stop)
        # a lista de reservas pergunta por reserva: segue o mesmo valor do bloqueio geral
        p = mock.patch.object(claude.conta, "bloqueio_reserva", side_effect=lambda *a, **k: self.bloqueio_api.return_value)
        p.start()
        self.addCleanup(p.stop)

    def uso(self):
        arq = Path(self.dados) / "claude" / "uso.jsonl"
        return [json.loads(l) for l in arq.read_text(encoding="utf-8").splitlines()] if arq.exists() else []

    def test_comando_sem_ferramentas_e_sem_sessao(self):
        with mock.patch.object(claude.os, "geteuid", create=True, return_value=1000):
            cmd = claude.comando("sonnet", "sys")
        self.assertEqual(cmd[0], claude.BIN)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, cmd)

    def test_root_troca_para_usuario_claude_com_ambiente_limpo(self):
        with mock.patch.object(claude.os, "geteuid", create=True, return_value=0):
            cmd = claude.comando("sonnet", "sys")
        self.assertEqual(cmd[:6], ["runuser", "-u", "claude", "--", "env", "-i"])
        self.assertFalse(any("OPENROUTER" in c or "DATAFORSEO" in c for c in cmd))

    def test_devolve_resultado_e_registra_uso(self):
        r = saida(json.dumps({"is_error": False, "result": "diagnóstico", "total_cost_usd": 0.2, "num_turns": 1}))
        with mock.patch.object(claude.subprocess, "run", return_value=r) as run:
            self.assertEqual(claude.perguntar("prompt", nome="aio-analise", base_dados=self.dados), "diagnóstico")
        self.assertEqual(run.call_args.kwargs["input"], "prompt")
        [u] = self.uso()
        self.assertEqual((u["origem"], u["nome"], u["ok"], u["custo_usd"]), ("hub", "aio-analise", True, 0.2))

    def test_erro_ou_vazio_levanta(self):
        for out in (json.dumps({"is_error": True, "result": "falhou"}), json.dumps({"result": "  "}), "não é json"):
            with mock.patch.object(claude.subprocess, "run", return_value=saida(out, 1)):
                with self.assertRaises(RuntimeError):
                    claude.perguntar("p", base_dados=self.dados)
        self.assertEqual([u["ok"] for u in self.uso()], [False, False, False])

    def test_limite_pausa_a_conta_e_nao_chama_de_novo(self):
        out = json.dumps({"is_error": True, "result": "You've hit your limit · resets 3pm (UTC)"})
        avisos = []
        with mock.patch.object(claude.subprocess, "run", return_value=saida(out, 1)):
            with self.assertRaises(claude.ContaPausada):
                claude.perguntar("p", base_dados=self.dados, avisar_fn=lambda t, **k: avisos.append(t))
        self.assertEqual(len(avisos), 1)
        self.assertTrue((Path(self.dados) / "claude" / "pausa.json").exists())
        with mock.patch.object(claude.subprocess, "run") as run:
            with self.assertRaises(claude.ContaPausada):
                claude.perguntar("p", base_dados=self.dados)
        run.assert_not_called()

    def test_conta_1_pausada_usa_a_2(self):
        self.com_login.return_value = ("1", "2", "3", "4")
        conta_pausa = Path(self.dados) / "claude"
        conta_pausa.mkdir()
        (conta_pausa / "pausa.json").write_text(json.dumps({"ate": "2099-01-01T00:00:00+00:00"}), encoding="utf-8")
        r = saida(json.dumps({"is_error": False, "result": "ok 2"}))
        with mock.patch.object(claude.subprocess, "run", return_value=r) as run, \
                mock.patch.object(claude.os, "geteuid", create=True, return_value=0):
            self.assertEqual(claude.perguntar("p", base_dados=self.dados), "ok 2")
        self.assertIn(f"CLAUDE_CONFIG_DIR=/home/{claude.USUARIO}/.claude-2", run.call_args.args[0])
        claude._trava.assert_called_with(claude.ESPERA_TRAVA, "2")
        self.assertEqual(self.uso()[0]["conta"], "2")

    def test_limite_pausa_so_a_conta_e_passa_para_a_proxima(self):
        self.com_login.return_value = ("1", "2")
        limite = saida(json.dumps({"is_error": True, "result": "You've hit your limit · resets 3pm (UTC)"}), 1)
        ok = saida(json.dumps({"is_error": False, "result": "ok"}))
        with mock.patch.object(claude.subprocess, "run", side_effect=[limite, ok]):
            self.assertEqual(claude.perguntar("p", base_dados=self.dados, avisar_fn=lambda t, **k: None), "ok")
        self.assertTrue((Path(self.dados) / "claude" / "pausa.json").exists())
        self.assertFalse((Path(self.dados) / "claude" / "pausa-2.json").exists())
        self.assertEqual([(u["conta"], u["limite"]) for u in self.uso()], [("1", True), ("2", False)])

    def test_todas_no_limite_usam_a_reserva_de_api(self):
        self.com_login.return_value = ("1", "2", "3", "4")
        self.bloqueio_api.return_value = None
        limite = saida(json.dumps({"is_error": True, "result": "Claude AI usage limit reached|4102444800"}), 1)
        ok = saida(json.dumps({"is_error": False, "result": "pela api", "total_cost_usd": 0.03}))
        with mock.patch.object(claude.subprocess, "run", side_effect=[limite] * 4 + [ok]) as run, \
                mock.patch.object(claude.os, "geteuid", create=True, return_value=0):
            self.assertEqual(claude.perguntar("p", base_dados=self.dados, avisar_fn=lambda t, **k: None), "pela api")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], str(claude.ORCAMENTO_API_USD))
        self.assertIn(f"CLAUDE_CONFIG_DIR=/home/{claude.USUARIO}/.claude-api", cmd)
        self.assertFalse(any(c.startswith("sk-") for c in cmd))  # a chave não vai na linha de comando
        self.assertEqual(self.uso()[-1]["conta"], "api")
        self.assertFalse((Path(self.dados) / "claude" / "pausa-api.json").exists())

    def test_api_fora_do_root_leva_a_chave_no_ambiente(self):
        with mock.patch.object(claude.os, "geteuid", create=True, return_value=1001), \
                mock.patch.object(claude.conta, "chave_api", return_value="sk-x"), \
                mock.patch.dict(claude.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_HOME": "/h"}):
            env = claude._env(claude.conta.API)
        self.assertEqual((env["ANTHROPIC_API_KEY"], env["CLAUDE_CONFIG_DIR"]), ("sk-x", "/h/.claude-api"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_conta_n_fora_do_root_sem_o_token_da_1(self):
        with mock.patch.object(claude.os, "geteuid", create=True, return_value=1001), \
                mock.patch.dict(claude.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_HOME": "/h"}):
            env, env1 = claude._env("3"), claude._env("1")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/h/.claude-3")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env1["CLAUDE_CODE_OAUTH_TOKEN"], "t")

    def test_reserva_fora_da_a_mensagem_de_cota(self):
        self.com_login.return_value = ("1", "2")
        self.bloqueio_api.return_value = "teto do dia de US$ 20 atingido"
        limite = saida(json.dumps({"is_error": True, "result": "You've hit your limit · resets 3pm (UTC)"}), 1)
        with mock.patch.object(claude.subprocess, "run", return_value=limite):
            with self.assertRaisesRegex(claude.ContaPausada, "teto do dia"):
                claude.perguntar("p", base_dados=self.dados, avisar_fn=lambda t, **k: None)

    def test_espera_trava_e_conta_ocupada(self):
        r = saida(json.dumps({"is_error": False, "result": "ok"}))
        with mock.patch.object(claude.subprocess, "run", return_value=r):
            claude.perguntar("p", base_dados=self.dados)
            claude._trava.assert_called_with(claude.ESPERA_TRAVA, "1")
            claude.perguntar("p", base_dados=self.dados, espera_trava=60)
            claude._trava.assert_called_with(60, "1")
        claude._trava.return_value = mock.MagicMock(__enter__=lambda s: False, __exit__=lambda *a: None)
        with mock.patch.object(claude.subprocess, "run") as run:
            with self.assertRaises(claude.ContaOcupada):
                claude.perguntar("p", base_dados=self.dados, espera_trava=60)
        run.assert_not_called()
        self.assertTrue(issubclass(claude.ContaOcupada, RuntimeError))

    def test_trava_repassa_a_espera(self):
        with mock.patch.object(claude.os.path, "isdir", return_value=True), mock.patch.object(claude.os, "name", "posix"), \
                mock.patch.object(claude.conta, "trava") as trava:
            ClaudeTest._trava_real(60, "3")
        trava.assert_called_once_with(esperar=60, caminho=claude.conta.caminho_trava("3"))


ClaudeTest._trava_real = staticmethod(claude._trava)


if __name__ == "__main__":
    unittest.main()
