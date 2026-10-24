"""Papéis do painel (4.3): dono / operador / leitura em toda rota e ação, convite por link e a tela de usuários.
Servidor real numa porta efêmera; HUB_DATA, segredos e toml em pasta temporária; nada vai ao Linear."""
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hub import acoes, config, config_telas, limite, painel, plugins, ui, usuarios
from tests import sessao_teste
from tests.test_painel import cockpit_neutro

SENHA = sessao_teste.SENHA


class Sem(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Base(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        (self.d / "seg").mkdir()
        env = mock.patch.dict(os.environ, {"HUB_DATA": str(self.d), "KROHNOS_CAMINHOS_SEGREDOS": str(self.d / "seg"),
                                           "KROHNOS_CONFIG": str(self.d / "nao.toml"), "PAINEL_SENHA": ""})
        env.start()
        self.addCleanup(env.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)


class Servidor(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), painel.Painel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        limite.esquecer()  # a cota de POST por IP (/convite) é do processo: cada teste começa do zero
        self.addCleanup(limite.esquecer)
        # cliente tem portão próprio (tests/test_cliente_login.py): aqui só os papéis do painel
        self.sessoes = {p: sessao_teste.cookie_e_csrf(p) for p in usuarios.PAPEIS if p != "cliente"}

    def req(self, caminho, dados=None, cookie=None, origin=True, cab=None):
        h = dict(cab or {})
        if cookie:
            h["Cookie"] = cookie
        if dados is not None and origin:
            h["Origin"] = self.base
        r = urllib.request.Request(self.base + caminho, headers=h,
                                   data=urllib.parse.urlencode(dados).encode() if dados is not None else None)
        try:
            resp = urllib.request.build_opener(Sem).open(r)
        except urllib.error.HTTPError as ex:
            resp = ex
        return resp.status, resp.read().decode(), resp.headers


class PermissoesTest(unittest.TestCase):
    def test_pode(self):
        self.assertTrue(usuarios.pode("leitura", "ver"))
        self.assertFalse(usuarios.pode("leitura", "acao:comentar"))
        self.assertTrue(usuarios.pode("operador", "acao:comentar"))
        self.assertFalse(usuarios.pode("operador", "acao:convidar"))
        self.assertFalse(usuarios.pode("operador", "config"))
        self.assertTrue(usuarios.pode("dono", "config"))
        self.assertTrue(usuarios.pode("dono", "acao:qualquer"))
        for papel in (None, "", "admin", "DONO"):
            self.assertFalse(usuarios.pode(papel, "ver"), papel)

    def test_permissao_get(self):
        for c in ("/config", "/config/", "/config/usuarios", "/config/auditoria", "//config//usuarios", "/comecar"):
            self.assertEqual(usuarios.permissao_get(c), "config", c)
        for c in ("/execucao", "//execucao/abc"):
            self.assertEqual(usuarios.permissao_get(c), "execucao", c)
        self.assertTrue(usuarios.pode("operador", "execucao"))
        self.assertFalse(usuarios.pode("leitura", "execucao"))
        for c in ("/", "/pendencias", "/configx", "/metas/config", "/entrar", "/r/abc"):
            self.assertEqual(usuarios.permissao_get(c), "ver", c)

    def test_toda_acao_tem_dono_e_toda_permissao_tem_acao(self):
        oper = {p[5:] for p in usuarios.PODE["operador"] if p.startswith("acao:")}
        for a in acoes.ACOES:
            self.assertTrue(a in oper or a in usuarios.SO_DONO, a)
            self.assertFalse(a in oper and a in usuarios.SO_DONO, a)
        for papel, perms in usuarios.PODE.items():
            if perms == "*":
                continue
            for p in perms:
                if p.startswith("acao:"):
                    self.assertIn(p[5:], acoes.ACOES, (papel, p))
        self.assertEqual(set(usuarios.SO_DONO) - set(acoes.ACOES), set())
        self.assertNotIn("acao:", " ".join(usuarios.PODE["leitura"]))

    def test_executar_recusa_fora_do_registro(self):
        self.assertEqual(acoes.executar("apagar-tudo", {}, None), (False, "Ação desconhecida."))


class RotasTest(Servidor):
    def caminhos(self):
        cs = [c for _, _, itens in ui.menu() for c, _ in itens]
        return cs + ["/config/usuarios", "/config/auditoria", "/config", "/comecar"] + list(config_telas.ROTAS_GET)

    def test_execucao_so_operador_e_dono(self):
        from hub import rastreio
        rastreio.gravar("abcd1234abcd1234", {"n": 1, "tipo": "fala", "texto": "x"})
        for c in ("/execucao", "/execucao/abcd1234", "//execucao//abcd1234?passo=1"):
            st, _, h = self.req(c)
            self.assertEqual((st, h["Location"].startswith("/entrar")), (303, True), c)  # sem sessão
            self.assertEqual(self.req(c, cookie=self.sessoes["leitura"][0])[0], 403, c)
            for papel in ("operador", "dono"):
                self.assertEqual(self.req(c, cookie=self.sessoes[papel][0])[0], 200, (c, papel))
        st, corpo, _ = self.req("/execucao/abcd1234?passo=%C2%B2", cookie=self.sessoes["dono"][0])
        self.assertEqual(st, 200)  # "²" não é passo: ignorado
        self.assertNotIn("<h3>Entrada</h3>", corpo)

    def test_post_replay_exige_permissao(self):
        from hub import rastreio
        for papel in usuarios.PAPEIS:  # o replay segue a permissão da tela /execucao
            self.assertEqual(usuarios.pode(papel, "acao:replay"), usuarios.pode(papel, "execucao"), papel)
        dados = {"ref": "abcd1234", "passo": "2", "correcao": "faça Y"}
        with mock.patch.object(rastreio, "rodar_de_novo", return_value={"ok": True, "motivo": "na fila"}) as rd:
            st, _, h = self.req("/acao/replay", {**dados, "csrf": "x"})
            self.assertEqual((st, h["Location"].startswith("/entrar")), (303, True))  # sem sessão
            ck, csrf = self.sessoes["leitura"]
            self.assertEqual(self.req("/acao/replay", {**dados, "csrf": csrf}, cookie=ck)[0], 403)
            ck, csrf = self.sessoes["operador"]
            self.assertEqual(self.req("/acao/replay", {**dados, "csrf": "errado"}, cookie=ck)[0], 403)
            self.assertEqual(self.req("/acao/replay", {**dados, "csrf": csrf}, cookie=ck, origin=False)[0], 403)
            rd.assert_not_called()
            st, _, h = self.req("/acao/replay", {**dados, "csrf": csrf}, cookie=ck)
            self.assertEqual(st, 303)
            self.assertIn("ok=", h["Location"])
            rd.assert_called_once()
            self.assertEqual(rd.call_args[0], ("abcd1234", 2, "faça Y"))
            self.assertEqual(rd.call_args[1]["quem"], usuarios.sessao(ck.split("=", 1)[1])["email"])
            self.assertEqual(rd.call_args[1]["via"], "painel")
            ck, csrf = self.sessoes["dono"]
            st, _, h = self.req("/acao/replay", {**dados, "passo": "2x", "csrf": csrf}, cookie=ck)
            self.assertEqual((st, rd.call_count), (303, 1))  # passo inválido não chega ao rastreio
            self.assertIn("erro=", h["Location"])
        aud = [a for a in usuarios.auditoria() if a["acao"] == "replay"]
        self.assertEqual([a["alvo"] for a in aud], ["abcd1234 passo 2x", "abcd1234 passo 2"])
        self.assertNotIn("faça Y", str(aud))  # a correção (texto livre) não vai à auditoria
        rastreio.gravar("abcd1234abcd1234", {"n": 1, "tipo": "fala", "texto": "x"})
        _, corpo, _ = self.req("/execucao/abcd1234?passo=1", cookie=self.sessoes["operador"][0])
        self.assertIn("não é de uma issue", corpo)  # gravada sem cabeçalho: sem issue, sem formulário
        reg = {"nome": "linear-krn-7", "inicio": "2026-10-17T10:00:00+00:00"}
        eid = rastreio.execucao_id(reg)
        rastreio.gravar(eid, {"n": 1, "tipo": "fala", "texto": "x"})
        con = rastreio.conectar()
        with con:
            rastreio._marcar(con, reg, eid, "x", 1)
        con.close()
        ck, csrf = self.sessoes["operador"]
        _, corpo, _ = self.req(f"/execucao/{eid}?passo=1", cookie=ck)
        self.assertIn('action="/acao/replay"', corpo)
        self.assertIn(f'name="csrf" value="{csrf}"', corpo)

    def test_telas_de_config_post_so_dono(self):
        for caminho in config_telas.ROTAS_POST:
            for papel, (ck, csrf) in self.sessoes.items():
                with self.subTest(caminho=caminho, papel=papel), \
                        mock.patch.dict(config_telas.ROTAS_POST, {caminho: mock.Mock(return_value=(True, "ok"))}):
                    st, _, h = self.req(caminho, {"csrf": csrf, "acao": "x"}, cookie=ck)
                    if papel == "dono":
                        self.assertEqual((st, h["Location"]), (303, caminho + "?ok=ok"))
                        config_telas.ROTAS_POST[caminho].assert_called_once()
                        self.assertEqual(config_telas.ROTAS_POST[caminho].call_args[0][1], usuarios.sessao(ck.split("=", 1)[1])["email"])
                    else:
                        self.assertEqual(st, 403)
                        config_telas.ROTAS_POST[caminho].assert_not_called()
        self.assertIn("config", [a["acao"] for a in usuarios.auditoria()])

    def test_telas_de_config_post_sem_csrf_da_sessao(self):
        ck, _ = self.sessoes["dono"]
        _, outro = self.sessoes["operador"]
        for csrf in ("", outro):
            st, _, _ = self.req("/config/cofre", {"csrf": csrf, "acao": "definir", "nome": "a.b", "valor": "v"}, cookie=ck)
            self.assertEqual(st, 403)

    def test_video_arquivo_so_com_sessao(self):
        st, _, h = self.req("/video-arquivo/x.mp4")
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].startswith("/entrar"))
        with mock.patch.object(plugins, "arquivo", return_value=None) as arq:
            st, _, _ = self.req("/video-arquivo/x.mp4", cookie=self.sessoes["leitura"][0])
        self.assertEqual(st, 404)
        arq.assert_called_once_with("/video-arquivo/x.mp4")

    def test_toda_rota_por_papel(self):
        for caminho in self.caminhos():
            config_ = usuarios.permissao_get(caminho) == "config"
            for papel, (ck, _) in self.sessoes.items():
                with self.subTest(caminho=caminho, papel=papel):
                    st, corpo, _ = self.req(caminho, cookie=ck)
                    if papel == "dono":
                        self.assertNotEqual(st, 403)
                        self.assertNotEqual(st, 500)
                    elif config_:
                        self.assertEqual(st, 403)
                        self.assertIn("Sem permissão", corpo)
                    else:
                        self.assertIn(st, (200, 302))

    def test_toda_acao_por_papel(self):
        for acao in acoes.ACOES:
            for papel, (ck, csrf) in self.sessoes.items():
                with self.subTest(acao=acao, papel=papel), \
                        mock.patch.object(acoes, "executar", return_value=(True, "ok")) as ex:
                    st, _, _ = self.req(f"/acao/{acao}", {"csrf": csrf}, cookie=ck)
                    if papel == "leitura" or (papel == "operador" and acao in usuarios.SO_DONO):
                        self.assertEqual(st, 403)
                        ex.assert_not_called()
                    else:
                        self.assertEqual(st, 303)
                        ex.assert_called_once()

    def test_csrf_antes_do_papel(self):
        ck, _ = self.sessoes["leitura"]
        with mock.patch.object(acoes, "executar") as ex:
            self.assertEqual(self.req("/acao/comentar", {"csrf": "errado"}, cookie=ck)[0], 403)
            self.assertEqual(self.req("/acao/convidar", {"csrf": self.sessoes["operador"][1]},
                                      cookie=self.sessoes["operador"][0], origin=False)[0], 403)
            ex.assert_not_called()

    def test_acao_desconhecida_operador_403_dono_erro(self):
        with mock.patch("hub.linear.comentar"):
            self.assertEqual(self.req("/acao/apagar-tudo", {"csrf": self.sessoes["operador"][1]},
                                      cookie=self.sessoes["operador"][0])[0], 403)
            st, _, h = self.req("/acao/apagar-tudo", {"csrf": self.sessoes["dono"][1]}, cookie=self.sessoes["dono"][0])
        self.assertEqual(st, 303)
        self.assertIn("erro=", h["Location"])

    def test_quem_vai_para_executar(self):
        ck, csrf = sessao_teste.cookie_e_csrf("operador", "op@exemplo.test")
        with mock.patch.object(acoes, "executar", return_value=(True, "ok")) as ex:
            self.req("/acao/comentar", {"csrf": csrf, "issue": "KRN-1", "texto": "oi"}, cookie=ck)
        self.assertEqual(ex.call_args.kwargs.get("quem"), "op@exemplo.test")

    def test_botoes_escondidos_para_leitura(self):
        from hub import cockpit
        with mock.patch.object(cockpit, "pendencias", lambda *a, **k: {"pedidos": [{"id": "p1", "n": 7, "resumo": "r", "tipo": "x"}]}):
            painel._CK.clear()
            _, leitura, _ = self.req("/pendencias", cookie=self.sessoes["leitura"][0])
            painel._CK.clear()
            _, oper, _ = self.req("/pendencias", cookie=self.sessoes["operador"][0])
        self.assertNotIn('action="/acao/', leitura)
        self.assertIn('action="/acao/pedido"', oper)
        for papel in ("leitura", "operador"):
            _, metas, _ = self.req("/metas", cookie=self.sessoes[papel][0])
            self.assertEqual('action="/acao/criar-meta"' in metas, papel == "operador", papel)

    def test_usuarios_no_menu_so_do_dono(self):
        for papel, (ck, _) in self.sessoes.items():
            _, corpo, _ = self.req("/pendencias", cookie=ck)
            self.assertEqual('href="/config/usuarios"' in corpo, papel == "dono", papel)

    def test_rota_direta_sem_sessao_mostra_botoes(self):
        painel._pedido.sessao = None
        _, h = painel.rota("/metas")
        self.assertIn('action="/acao/criar-meta"', h)


class ConviteTest(Base):
    AGORA = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)

    def test_gera_aceita_e_entra(self):
        t = usuarios.convidar("Nova@Exemplo.test", "operador", agora=self.AGORA)
        self.assertEqual(usuarios.ver_convite(t, agora=self.AGORA), "nova@exemplo.test")
        self.assertEqual(usuarios.aceitar_convite(t, SENHA, agora=self.AGORA), "nova@exemplo.test")
        u = {x["email"]: x for x in usuarios.listar()}["nova@exemplo.test"]
        self.assertEqual(u["papel"], "operador")
        self.assertTrue(usuarios.entrar("nova@exemplo.test", SENHA))
        self.assertIsNone(usuarios.aceitar_convite(t, SENHA, agora=self.AGORA))  # uso único
        self.assertIsNone(usuarios.ver_convite(t, agora=self.AGORA))

    def test_vence_em_24h(self):
        t = usuarios.convidar("nova@exemplo.test", "leitura", agora=self.AGORA)
        depois = self.AGORA + usuarios.CONVITE + dt.timedelta(seconds=1)
        self.assertIsNone(usuarios.ver_convite(t, agora=depois))
        self.assertIsNone(usuarios.aceitar_convite(t, SENHA, agora=depois))
        self.assertEqual(usuarios.contar(), 0)

    def test_token_puro_nao_vai_ao_banco(self):
        t = usuarios.convidar("nova@exemplo.test", "leitura")
        self.assertNotIn(t.encode(), usuarios.banco().read_bytes())
        con = sqlite3.connect(usuarios.banco())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM convite").fetchone()[0], 1)
        con.close()

    def test_lixo_e_invalido(self):
        for t in ("", None, "x" * 43):
            self.assertIsNone(usuarios.ver_convite(t))
            self.assertIsNone(usuarios.aceitar_convite(t, SENHA))

    def test_existente_troca_senha_e_ignora_papel(self):
        usuarios.criar("ana@exemplo.test", SENHA, papel="leitura")
        velho = usuarios.entrar("ana@exemplo.test", SENHA)
        t = usuarios.convidar("ana@exemplo.test", "dono")
        self.assertEqual(usuarios.aceitar_convite(t, "outra-senha-longa"), "ana@exemplo.test")
        self.assertIsNone(usuarios.entrar("ana@exemplo.test", SENHA))
        self.assertTrue(usuarios.entrar("ana@exemplo.test", "outra-senha-longa"))
        self.assertEqual(usuarios.listar()[0]["papel"], "leitura")
        self.assertIsNone(usuarios.sessao(velho))  # troca de senha derruba as sessões

    def test_desativado_nao_volta(self):
        usuarios.criar("dono@exemplo.test", SENHA, papel="dono")
        usuarios.criar("ana@exemplo.test", SENHA, papel="operador")
        t = usuarios.convidar("ana@exemplo.test", "operador")
        usuarios.desativar("ana@exemplo.test")
        self.assertIsNone(usuarios.aceitar_convite(t, "outra-senha-longa"))
        self.assertFalse(usuarios.listar()[1]["ativo"])
        with self.assertRaises(ValueError):
            usuarios.convidar("ana@exemplo.test", "operador")

    def test_dois_convites_do_mesmo_email_so_um_cria(self):
        a = usuarios.convidar("nova@exemplo.test", "leitura")
        b = usuarios.convidar("nova@exemplo.test", "dono")
        self.assertEqual(usuarios.aceitar_convite(a, SENHA), "nova@exemplo.test")
        real = usuarios._con

        class Corrida:  # o 2º aceite não vê o usuário (o 1º ainda não tinha gravado) e o INSERT bate na chave
            def __init__(self, c):
                self.c = c

            def execute(self, sql, *a):
                if sql.startswith("SELECT ativo FROM usuario"):
                    return mock.Mock(fetchone=lambda: None)
                return self.c.execute(sql, *a)

            def __enter__(self):
                return self.c.__enter__()

            def __exit__(self, *a):
                return self.c.__exit__(*a)

            def close(self):
                self.c.close()

        with mock.patch.object(usuarios, "_con", lambda *a: Corrida(real(*a))):
            self.assertIsNone(usuarios.aceitar_convite(b, "outra-senha-longa"))
        self.assertEqual([(u["email"], u["papel"]) for u in usuarios.listar()], [("nova@exemplo.test", "leitura")])
        self.assertTrue(usuarios.entrar("nova@exemplo.test", SENHA))

    def test_entrada_invalida(self):
        for ruim in ("a<b@x.test", "a>b@x.test", 'a"b@x.test', "a'b@x.test", "a@x<.test", "a\x01b@x.test",
                     "<td/x=1>@a.test"):
            with self.assertRaises(ValueError, msg=ruim):
                usuarios.convidar(ruim, "leitura")
            with self.assertRaises(ValueError, msg=ruim):
                usuarios.criar(ruim, SENHA)
        with self.assertRaises(ValueError):
            usuarios.convidar("nao é email", "leitura")
        with self.assertRaises(ValueError):
            usuarios.convidar("ana@exemplo.test", "admin")
        t = usuarios.convidar("ana@exemplo.test", "leitura")
        with self.assertRaises(ValueError):
            usuarios.aceitar_convite(t, "curta")
        self.assertEqual(usuarios.aceitar_convite(t, SENHA), "ana@exemplo.test")  # senha curta não gastou o convite


class UltimoDonoTest(Base):
    def test_ultimo_dono(self):
        usuarios.criar("dono@exemplo.test", SENHA, papel="dono")
        usuarios.criar("ana@exemplo.test", SENHA, papel="leitura")
        for f in (lambda: usuarios.trocar_papel("dono@exemplo.test", "operador"),
                  lambda: usuarios.desativar("dono@exemplo.test")):
            with self.assertRaisesRegex(ValueError, "Precisa sobrar um dono ativo."):
                f()
        usuarios.trocar_papel("ana@exemplo.test", "dono")
        usuarios.trocar_papel("dono@exemplo.test", "operador")  # agora sobra a outra
        with self.assertRaisesRegex(ValueError, "Precisa sobrar um dono ativo."):
            usuarios.desativar("ana@exemplo.test")
        with self.assertRaisesRegex(ValueError, "não existe"):
            usuarios.trocar_papel("ninguem@exemplo.test", "dono")

    def test_dono_inativo_nao_conta(self):
        usuarios.criar("a@exemplo.test", SENHA, papel="dono")
        usuarios.criar("b@exemplo.test", SENHA, papel="dono")
        usuarios.desativar("b@exemplo.test")
        with self.assertRaises(ValueError):
            usuarios.trocar_papel("a@exemplo.test", "leitura")
        usuarios.trocar_papel("b@exemplo.test", "leitura")  # inativo pode mudar à vontade

    def test_acoes_de_usuario(self):
        usuarios.criar("dono@exemplo.test", SENHA, papel="dono")
        usuarios.criar("ana@exemplo.test", SENHA, papel="leitura")
        self.assertEqual(acoes.executar("papel", {"email": "dono@exemplo.test", "papel": "leitura"}, None),
                         (False, "Precisa sobrar um dono ativo."))
        self.assertTrue(acoes.executar("papel", {"email": "ana@exemplo.test", "papel": "operador"}, None)[0])
        self.assertFalse(acoes.executar("papel", {"email": "ana@exemplo.test", "papel": "rei"}, None)[0])
        self.assertTrue(acoes.executar("desativar", {"email": "ana@exemplo.test"}, None)[0])
        self.assertFalse(acoes.executar("desativar", {"email": "dono@exemplo.test"}, None)[0])
        r = acoes.executar("convidar", {"email": "nova@exemplo.test", "papel": "leitura"}, None, quem="dono@exemplo.test")
        self.assertEqual(r[:2], (True, "Convite criado."))
        self.assertEqual(usuarios.ver_convite(r[2]["convite"]), "nova@exemplo.test")


class TelaUsuariosTest(Servidor):
    def test_tela_lista_escapada_e_formularios(self):
        ck, _ = self.sessoes["dono"]
        con = sqlite3.connect(usuarios.banco())  # e-mail hostil gravado por fora da validação
        with con:
            con.execute("INSERT INTO usuario(email, hash, papel, criado) VALUES (?,?,?,?)",
                        ("<td/x=1>@a.test", "x", "<b>rei</b>", "2026-10-17T00:00:00+00:00"))
        con.close()
        st, corpo, _ = self.req("/config/usuarios", cookie=ck)
        self.assertEqual(st, 200)
        for u in usuarios.listar():
            if "<" not in u["email"]:
                self.assertIn(u["email"], corpo)
        self.assertRegex(corpo, r"<td[^>]*>&lt;td/x=1&gt;@a.test</td>")  # a célula, não só o input escondido
        self.assertNotIn("<td/x=1", corpo)
        self.assertNotIn("<b>rei</b>", corpo)
        for a in ("convidar", "papel", "desativar"):
            self.assertIn(f'action="/acao/{a}"', corpo)

    def test_convidar_mostra_link_uma_vez_e_nao_na_url(self):
        ck, csrf = self.sessoes["dono"]
        st, _, h = self.req("/acao/convidar", {"csrf": csrf, "email": "nova@exemplo.test", "papel": "operador"},
                            cookie=ck, cab={"Referer": self.base + "/config/usuarios"})
        self.assertEqual(st, 303)
        loc = h["Location"]
        self.assertTrue(loc.startswith("/config/usuarios?ok="), loc)
        self.assertNotIn("t=", loc)
        _, corpo, _ = self.req(loc, cookie=ck)
        self.assertIn("Convite criado.", corpo)
        host = urllib.parse.urlsplit(self.base).netloc
        i = corpo.index(f"https://{host}/convite?t=")
        token = corpo[i:].split("t=", 1)[1].split('"', 1)[0]
        self.assertEqual(usuarios.ver_convite(token), "nova@exemplo.test")
        _, de_novo, _ = self.req("/config/usuarios", cookie=ck)
        self.assertNotIn(token, de_novo)  # aparece uma vez só
        # outro dono não vê o link de quem convidou
        self.assertNotIn("/convite?t=", self.req("/config/usuarios", cookie=sessao_teste.cookie("dono"))[1])

    def test_rebaixar_ultimo_dono_pela_tela(self):
        ck, csrf = sessao_teste.cookie_e_csrf("dono", "unico@exemplo.test")
        for u in usuarios.listar():  # só este dono fica ativo
            if u["email"] != "unico@exemplo.test" and u["papel"] == "dono":
                usuarios.trocar_papel(u["email"], "leitura")
        st, _, h = self.req("/acao/papel", {"csrf": csrf, "email": "unico@exemplo.test", "papel": "leitura"},
                            cookie=ck, cab={"Referer": self.base + "/config/usuarios"})
        self.assertEqual(st, 303)
        self.assertIn("erro=" + urllib.parse.quote("Precisa sobrar um dono ativo."), h["Location"])


class ConviteRotaTest(Servidor):
    def test_fluxo(self):
        t = usuarios.convidar("nova@exemplo.test", "operador")
        st, corpo, h = self.req("/convite?t=" + urllib.parse.quote(t))
        self.assertEqual(st, 200)
        self.assertIn('action="/convite"', corpo)
        self.assertIn(f'name="t" value="{t}"', corpo)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA}, origin=False)[0], 403)
        self.assertEqual(self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA},
                                  cab={"Origin": "https://mal.test"}, origin=False)[0], 403)
        st, corpo, _ = self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA + "x"})
        self.assertEqual(st, 200)
        self.assertIn("As senhas não conferem.", corpo)
        st, corpo, _ = self.req("/convite", {"t": t, "senha": "curta", "senha2": "curta"})
        self.assertIn(f"pelo menos {usuarios.SENHA_MIN}", corpo)
        st, _, h = self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA})
        self.assertEqual((st, h["Location"]), (303, "/"))
        ck = h["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertEqual(self.req("/pendencias", cookie=ck)[0], 200)
        # reuso
        st, corpo, _ = self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA})
        self.assertEqual(st, 200)
        self.assertIn("Convite inválido ou vencido", corpo)

    def test_invalido_sem_detalhes_e_escapado(self):
        for q in ("", "?t=nao-existe", "?t=%22%3E%3Cb%3Ezz"):
            st, corpo, _ = self.req("/convite" + q)
            self.assertEqual(st, 200, q)
            self.assertIn("Convite inválido ou vencido", corpo)
            self.assertNotIn("<b>zz", corpo)
            self.assertNotIn('action="/convite"', corpo)


if __name__ == "__main__":
    unittest.main()
