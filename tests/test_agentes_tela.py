import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import hub
from hub import agenda, config, config_telas, contas_login, conta, ui

HOSTIL = '<script>alert(1)</script>"x'
TOKEN = "sk-ant-oat01-SEGREDO-NAO-VAZA"
FALSO = ("import sys,time\nprint('Abra https://login.exemplo.test/authorize?code=true&state=s1', flush=True)\n"
         "print('Codigo: ABCD-1234', flush=True)\nfor _ in sys.stdin: pass\ntime.sleep(60)\n")


def espera(cond, tempo=5.0):
    fim = time.monotonic() + tempo
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.dados, self.home, self.toml = d / "dados", d / "home", d / "kronos.toml"
        self.dados.mkdir()
        self.home.mkdir()
        (d / "falso.py").write_text(FALSO)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KRONOS_", "CLAUDE_", "HUB_"))}
        env.update(KRONOS_CONFIG=str(self.toml), KRONOS_CAMINHOS_DADOS=str(self.dados), CLAUDE_HOME=str(self.home),
                   PAINEL_SENHA="senha-de-teste")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)
        config.gravar({"agentes.contas": ["1", "2"], "agentes.faixas": {"1": "construtor", "2": "operador"}})
        self.addCleanup(self.encerrar)
        self.comando = [sys.executable, str(d / "falso.py")]
        p = mock.patch.object(contas_login, "comando_padrao", lambda home=None: self.comando)
        p.start()
        self.addCleanup(p.stop)
        self.credencial("1")

    def encerrar(self):
        for lg in list(contas_login.LOGINS.values()):
            contas_login.cancelar(lg.id)
        contas_login.LOGINS.clear()

    def credencial(self, n, expira=9999999999999, refresh=None):
        pasta = self.home / (".claude" if n == "1" else f".claude-{n}")
        pasta.mkdir(exist_ok=True)
        o = {"accessToken": TOKEN, "expiresAt": expira, **({"refreshToken": refresh} if refresh else {})}
        (pasta / ".credentials.json").write_text(json.dumps({"claudeAiOauth": o}))

    def get(self, caminho="/config/agentes", **q):
        st, html = config_telas.ROTAS_GET[caminho](q, "ana", "CSRF-X")
        self.assertEqual(st, 200)
        return html

    def post(self, campos, csrf=True, caminho="/config/agentes"):
        return config_telas.tratar_post(caminho, campos, "ana", "1.2.3.4", csrf)


class RotasTest(Base):
    def test_rotas_e_menu(self):
        for r in ("/config/agentes", "/config/agentes/login"):
            self.assertIn(r, config_telas.ROTAS_GET)
        self.assertIn("/config/agentes", config_telas.ROTAS_POST)
        self.assertIn("/config/agentes", [c for g, _, itens in ui.MENU if g == "Configurar" for c, _ in itens])

    def test_post_sem_csrf_403_e_nada_muda(self):
        for campos in ({"acao": "conta", "conta": "2", "faixa": "codex", "limite": "1"}, {"acao": "limite", "limite": "9"},
                       {"acao": "modelos", "construtor": "opus"}, {"acao": "adicionar", "faixa": "operador"},
                       {"acao": "cancelar", "login": "x"}):
            self.assertEqual(self.post(campos, csrf=False), (403, None))
        self.assertEqual(config.get("agentes.faixas")["2"], "operador")
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)
        self.assertEqual(contas_login.LOGINS, {})


class ListaTest(Base):
    def test_status_de_login_e_faixa(self):
        self.credencial("2", expira=1000)  # vencido e sem renovação
        h = self.get()
        self.assertIn("Login ok", h)
        self.assertIn("Login expirado", h)
        self.assertIn("Conta 1", h)
        self.assertIn("Conta 2", h)
        self.assertIn('<option value="operador" selected>', h)
        self.assertIn('name="csrf" value="CSRF-X"', h)

    def test_sem_login_e_entrar_de_novo(self):
        h = self.get()  # a 2 nunca entrou
        self.assertIn("Sem login", h)
        self.assertIn('value="entrar"', h)

    def test_uso_de_hoje_limite_e_pausa(self):
        self.credencial("2")
        agora = conta.agora()
        conta.registrar({"inicio": conta.iso(agora), "conta": "2", "custo_usd": 1.5, "nome": "x"}, str(self.dados))
        conta.registrar({"inicio": conta.iso(agora), "conta": "1", "custo_usd": 0.25, "nome": "x"}, str(self.dados))
        config.gravar({"agentes.limite_usd_dia": 3})
        conta.pausar("resets 11pm", "teste", str(self.dados), agora, lambda *a, **k: None, n="2")
        h = self.get()
        self.assertIn("US$ 1,50", h)
        self.assertIn("US$ 0,25", h)
        self.assertIn("limite US$ 3,00", h)
        self.assertIn("Pausada até", h)

    def test_limite_estourado_aparece(self):
        self.credencial("2")
        conta.registrar({"inicio": conta.iso(conta.agora()), "conta": "2", "custo_usd": 4, "nome": "x"}, str(self.dados))
        config.gravar({"agentes.limite_usd_dia": 3})
        self.assertIn("Limite do dia atingido", self.get())

    def test_o_que_esta_fazendo_e_a_fila_da_faixa(self):
        self.credencial("2")
        agenda._gravar_json(self.dados / "claude" / "rodando-2.json",
                            {"nome": "linear-krn-77", "inicio": conta.iso(conta.agora() - dt.timedelta(minutes=12)),
                             "pid": os.getpid(), "conta": "2", "repos": []})
        agenda.enfileirar(mock.Mock(nome="linear-krn-90", prioridade=2, linear={"identifier": "KRN-90"}, debugger=None,
                                    conta=None), "teste", str(self.dados), conta.agora())
        with mock.patch.object(agenda, "_vivo", lambda pid, inicio: True):
            h = self.get()
        self.assertIn("KRN-77", h)
        self.assertIn("há 12 min", h)
        self.assertIn("KRN-90", h)  # fila da faixa construtor

    def test_texto_de_fora_escapado(self):
        self.credencial("2")
        agenda._gravar_json(self.dados / "claude" / "rodando-2.json",
                            {"nome": HOSTIL, "inicio": conta.iso(conta.agora()), "pid": os.getpid(), "conta": "2", "repos": []})
        config.gravar({"agentes.modelos": {"construtor": HOSTIL}})
        with mock.patch.object(agenda, "_vivo", lambda pid, inicio: True):
            h = self.get()
        self.assertNotIn("<script>alert(1)", h)
        self.assertIn("&lt;script&gt;", h)

    def test_html_nao_tem_token(self):
        self.credencial("2")
        self.assertNotIn("SEGREDO-NAO-VAZA", self.get())

    def test_so_leitura_sem_permissao(self):
        with mock.patch.object(config_telas, "pode", lambda u, a: False):
            h = self.get()
        self.assertNotIn("<form", h)


class SalvarTest(Base):
    def test_faixa_grava_no_toml_e_preserva_as_outras(self):
        st, destino = self.post({"acao": "conta", "conta": "2", "faixa": "codex", "limite": ""})
        self.assertEqual(st, 303)
        self.assertIn("ok=", destino)
        self.assertEqual(config.get("agentes.faixas"), {"1": "construtor", "2": "codex"})
        sobre = self.dados / "kronos.painel.toml"
        self.assertIn('"2" = "codex"', sobre.read_text())
        self.assertNotIn("codex", self.toml.read_text() if self.toml.exists() else "")  # o principal não é tocado

    def test_limite_por_conta_e_padrao(self):
        self.post({"acao": "limite", "limite": "10"})
        self.post({"acao": "conta", "conta": "2", "faixa": "operador", "limite": "2,5"})
        self.assertEqual(config.get("agentes.limites"), {"2": 2.5})
        h = self.get()
        self.assertIn("limite US$ 2,50", h)
        self.assertIn("limite US$ 10,00 (padrão)", h)
        self.post({"acao": "conta", "conta": "2", "faixa": "operador", "limite": ""})  # vazio volta ao padrão
        self.assertEqual(config.get("agentes.limites"), {})

    def test_texto_de_modelos_e_honesto(self):
        self.assertIn("passa a usar quando ler esta chave", self.get())

    def test_orquestrador_do_estado_json(self):
        (self.dados / "orch").mkdir()
        (self.dados / "orch" / "estado.json").write_text(json.dumps(
            {"varredura": {"rodando": {"desde": conta.iso(conta.agora()), "motivo": "x"}}, "codex": {"hoje": {"revisados": 4}}}))
        h = self.get()
        self.assertIn("Varrendo", h)
        self.assertIn("4 PRs revisados hoje", h)

    def test_faixa_invalida_ou_conta_desconhecida_recusa(self):
        for campos in ({"acao": "conta", "conta": "2", "faixa": "chefe"}, {"acao": "conta", "conta": "9", "faixa": "operador"},
                       {"acao": "conta", "conta": "../x", "faixa": "operador"},
                       {"acao": "conta", "conta": "2", "faixa": "operador", "limite": "abc"},
                       {"acao": "conta", "conta": "2", "faixa": "operador", "limite": "-3"}):
            st, destino = self.post(campos)
            self.assertIn("erro=", destino)
        self.assertEqual(config.get("agentes.faixas")["2"], "operador")

    def test_modelos_por_papel(self):
        self.post({"acao": "modelos", "construtor": "opus", "operador": "sonnet", "revisao": " "})
        self.assertEqual(config.get("agentes.modelos"), {"construtor": "opus", "operador": "sonnet"})
        self.post({"acao": "modelos", "construtor": "fable", "operador": "", "revisao": "claude-sonnet-5-5"})
        self.assertEqual(config.get("agentes.modelos"), {"construtor": "fable", "revisao": "claude-sonnet-5-5"})

    def test_modelos_preserva_chaves_de_outros(self):
        config.gravar({"agentes.modelos": {"painel": ["a", "b"], "construtor": "opus"}})
        self.post({"acao": "modelos", "construtor": "haiku", "operador": "", "revisao": ""})
        self.assertEqual(config.get("agentes.modelos"), {"painel": ["a", "b"], "construtor": "haiku"})

    def test_modelo_com_lixo_recusa(self):
        _, destino = self.post({"acao": "modelos", "construtor": "opus; rm -rf", "operador": "", "revisao": ""})
        self.assertIn("erro=", destino)
        self.assertEqual(config.get("agentes.modelos"), {})

    def test_limite(self):
        self.post({"acao": "limite", "limite": "12,5"})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 12.5)
        self.post({"acao": "limite", "limite": "0"})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)
        for ruim in ("-1", "abc", "1e9", "nan", ""):
            _, destino = self.post({"acao": "limite", "limite": ruim})
            self.assertIn("erro=", destino, ruim)
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)

    def test_falha_ao_gravar_da_mensagem_sem_derrubar(self):
        with mock.patch.object(config, "gravar", side_effect=OSError("/caminho/secreto")):
            st, destino = self.post({"acao": "limite", "limite": "3"})
        self.assertEqual(st, 303)
        self.assertIn("erro=", destino)
        self.assertNotIn("secreto", destino)


class LoginTelaTest(Base):
    def adicionar(self, faixa="operador"):
        st, pagina = self.post({"acao": "adicionar", "faixa": faixa})
        self.assertEqual(st, 200)
        return pagina

    def test_adicionar_mostra_link_e_codigo_na_tela_de_progresso(self):
        pagina = self.adicionar()
        lg = next(iter(contas_login.LOGINS.values()))
        self.assertEqual(lg.n, "3")
        self.assertTrue(espera(lambda: lg.link and lg.codigo))
        h = self.get("/config/agentes/login", id=lg.id)
        self.assertIn("https://login.exemplo.test/authorize?code=true&amp;state=s1", h)
        self.assertIn("ABCD-1234", h)
        self.assertIn("Conta 3", h)
        self.assertIn('http-equiv="refresh"', h)
        self.assertIn(lg.id, pagina)  # a resposta do POST já leva para a tela de progresso
        self.assertIn('name="csrf" value="CSRF-X"', h)
        self.assertNotIn("SEGREDO-NAO-VAZA", h)

    def test_credencial_aparece_status_ok_e_conta_na_lista(self):
        self.adicionar()
        lg = next(iter(contas_login.LOGINS.values()))
        self.assertTrue(espera(lambda: lg.link))
        self.credencial("3")
        self.assertTrue(espera(lambda: lg.estado == "ok"))
        h = self.get("/config/agentes/login", id=lg.id)
        self.assertIn("Entrou", h)
        self.assertNotIn('http-equiv="refresh"', h)
        self.assertNotIn("SEGREDO-NAO-VAZA", h)
        lista = self.get()
        self.assertIn("Conta 3", lista)
        self.assertEqual(config.get("agentes.faixas")["3"], "operador")

    def test_cancelar_mata_o_filho(self):
        self.adicionar()
        lg = next(iter(contas_login.LOGINS.values()))
        self.assertTrue(espera(lambda: lg.link))
        st, destino = self.post({"acao": "cancelar", "login": lg.id})
        self.assertEqual(st, 303)
        self.assertEqual(lg.estado, "cancelado")
        self.assertTrue(espera(lambda: lg.proc.poll() is not None))
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])

    def test_cancelar_que_chega_tarde_diz_concluido(self):
        self.adicionar()
        lg = next(iter(contas_login.LOGINS.values()))
        lg.estado = "concluindo"
        st, destino = self.post({"acao": "cancelar", "login": lg.id})
        self.assertIn("ok=", destino)
        self.assertIn("chegou", urllib.parse.unquote(destino))
        self.assertEqual(lg.estado, "concluindo")

    def test_colar_codigo_vai_para_o_filho(self):
        self.adicionar()
        lg = next(iter(contas_login.LOGINS.values()))
        self.assertTrue(espera(lambda: lg.link))
        with mock.patch.object(contas_login, "enviar_codigo", return_value=True) as m:
            st, _ = self.post({"acao": "codigo", "login": lg.id, "codigo": "abc-123456"})
        m.assert_called_once_with(lg.id, "abc-123456")
        self.assertEqual(st, 200)  # volta para a tela de progresso

    def test_entrar_de_novo_na_conta_existente(self):
        st, _ = self.post({"acao": "entrar", "conta": "2"})
        self.assertEqual(st, 200)
        self.assertEqual(next(iter(contas_login.LOGINS.values())).n, "2")
        st, destino = self.post({"acao": "entrar", "conta": "9"})
        self.assertIn("erro=", destino)

    def test_login_desconhecido(self):
        h = self.get("/config/agentes/login", id="nao-existe")
        self.assertIn("Não achei", h)

    def test_link_hostil_escapado(self):
        lg = contas_login.Login("5", "operador", self.home, 60)
        lg.captura.link = 'https://x.example.test/"><script>alert(1)</script>'
        contas_login.LOGINS[lg.id] = lg
        h = self.get("/config/agentes/login", id=lg.id)
        self.assertNotIn("<script>alert(1)", h)

    def test_painel_serve_a_tela_e_o_post(self):
        from hub import painel
        st, corpo, *_ = painel.rota("/config/agentes", "ana")
        self.assertEqual(st, 200)
        self.assertIn("Adicionar conta", corpo)
        st, corpo, *_ = painel.rota("/config/agentes?ok=Salvo", "ana")
        self.assertIn("Salvo", corpo)


if __name__ == "__main__":
    unittest.main()
