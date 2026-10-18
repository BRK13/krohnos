"""Auditoria do painel (4.4): toda escrita vira uma linha (só INSERT) em usuarios.db; tela /config/auditoria só do
dono, tudo escapado; senha e token nunca em coluna nenhuma."""
import datetime as dt
import sqlite3
from unittest import mock

from hub import acoes, comecar, limite, usuarios
from tests import sessao_teste
from tests.test_papeis import Base, Servidor

SENHA = sessao_teste.SENHA
AGORA = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)


def dump():
    con = sqlite3.connect(usuarios.banco())
    try:
        return con.execute("SELECT * FROM auditoria ORDER BY id").fetchall()
    finally:
        con.close()


class FuncoesTest(Base):
    def test_auditar_e_ler(self):
        usuarios.auditar("ana@exemplo.test", "comentar", "KRN-1", ip="203.0.113.7", agora=AGORA)
        usuarios.auditar("ana@exemplo.test", "cofre-ler", "cofre:linear", resultado="erro: não achei")
        l = usuarios.auditoria()
        self.assertEqual([x["acao"] for x in l], ["cofre-ler", "comentar"])  # mais nova primeiro
        self.assertEqual(l[1], {"em": AGORA.isoformat(), "quem": "ana@exemplo.test", "acao": "comentar",
                                "alvo": "KRN-1", "resultado": "ok", "ip": "203.0.113.7"})
        self.assertEqual([x["alvo"] for x in usuarios.auditoria(alvo_prefixo="cofre:")], ["cofre:linear"])
        self.assertEqual(usuarios.auditoria(alvo_prefixo="%"), [])  # prefixo é literal, não LIKE
        self.assertEqual(len(usuarios.auditoria(limite=1)), 1)

    def test_corta_o_longo(self):
        usuarios.auditar("a", "x", "y" * 500, resultado="erro: " + "z" * 500)
        l = usuarios.auditoria()[0]
        self.assertLessEqual(len(l["alvo"]), 80)
        self.assertLessEqual(len(l["resultado"]), 120)

    def test_falha_ao_gravar_nao_derruba(self):
        with mock.patch.object(usuarios, "_con", side_effect=sqlite3.OperationalError("travado")), \
                mock.patch("sys.stderr") as err:
            usuarios.auditar("a", "x")  # não levanta
        self.assertTrue(err.write.called)

    def test_so_insert(self):
        usuarios.auditar("ana@exemplo.test", "comentar", "KRN-1", agora=AGORA)
        antes = dump()
        usuarios.auditar("bia@exemplo.test", "pedido", "abc")
        self.assertEqual(dump()[:1], antes)  # gravar de novo não mexe no que já estava
        con = sqlite3.connect(usuarios.banco())
        try:
            for sql in ("UPDATE auditoria SET quem='x'", "DELETE FROM auditoria"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "só aceita INSERT"):
                    with con:
                        con.execute(sql)
        finally:
            con.close()
        self.assertEqual(len(dump()), 2)
        self.assertEqual(dump()[:1], antes)

    def test_executar_grava_com_quem_alvo_e_resultado(self):
        with mock.patch.object(acoes.linear, "comentar"):
            acoes.executar("comentar", {"issue": "KRN-7", "texto": "segredo longo do texto"}, AGORA, quem="op@exemplo.test",
                           ip="203.0.113.7")
        acoes.executar("pedido", {"n": "abc"}, AGORA, quem="op@exemplo.test")
        with mock.patch.object(acoes.linear, "criar_issue", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                acoes.executar("criar-issue", {"projeto": "Nimbus", "titulo": "T" * 300}, AGORA, quem="op@exemplo.test")
        l = usuarios.auditoria()
        self.assertEqual((l[2]["quem"], l[2]["acao"], l[2]["alvo"], l[2]["resultado"], l[2]["ip"]),
                         ("op@exemplo.test", "comentar", "KRN-7", "ok", "203.0.113.7"))
        self.assertEqual((l[1]["acao"], l[1]["alvo"], l[1]["resultado"]), ("pedido", "abc", "erro: Pedido inválido."))
        self.assertEqual((l[0]["acao"], l[0]["alvo"], l[0]["resultado"]), ("criar-issue", "T" * 80, "erro: SystemExit"))
        self.assertNotIn("segredo longo", repr(dump()))

    def test_convite_nao_grava_o_token(self):
        ok, _, extra = acoes.executar("convidar", {"email": "nova@exemplo.test", "papel": "leitura"}, AGORA, quem="d@x.test")
        self.assertTrue(ok)
        self.assertNotIn(extra["convite"], repr(dump()))
        self.assertEqual(usuarios.auditoria()[0]["alvo"], "nova@exemplo.test")

    def test_migrar_senha(self):
        with mock.patch.dict("os.environ", {"PAINEL_SENHA": "senha-antiga-xyz"}):
            self.assertEqual(usuarios.migrar_senha_antiga(), "dono")
        l = usuarios.auditoria()[0]
        self.assertEqual((l["quem"], l["acao"], l["alvo"]), ("sistema", "migrar-senha", "dono"))
        self.assertNotIn("senha-antiga-xyz", repr(dump()))


class TelaTest(Servidor):
    def setUp(self):
        super().setUp()
        limite.esquecer()
        self.addCleanup(limite.esquecer)

    def entrar(self, email, senha):
        return self.req("/entrar", {"email": email, "senha": senha, "volta": "/"}, cab={"X-Forwarded-For": "203.0.113.7"})

    def test_login_certo_errado_bloqueado_e_sair(self):
        usuarios.criar("ana@exemplo.test", SENHA, papel="dono")
        for _ in range(5):
            self.entrar("ANA@exemplo.test", "senha-errada-123")
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 429)
        limite.esquecer()
        with mock.patch.object(limite, "agora", lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=16)):
            st, _, h = self.entrar("ana@exemplo.test", SENHA)
        self.assertEqual(st, 303)
        ck = h["Set-Cookie"].split(";")[0]
        self.req("/sair", {"csrf": usuarios.sessao(ck.split("=", 1)[1])["csrf"]}, cookie=ck)
        l = [x for x in reversed(usuarios.auditoria()) if x["acao"].startswith(("entrar", "sair"))]
        self.assertEqual([x["acao"] for x in l], ["entrar-falhou"] * 5 + ["entrar-bloqueado", "entrar", "sair"])
        self.assertEqual({x["alvo"] for x in l[:6]}, {"ana@exemplo.test"})
        self.assertEqual((l[6]["quem"], l[6]["ip"]), ("ana@exemplo.test", "203.0.113.7"))
        self.assertEqual(l[7]["quem"], "ana@exemplo.test")
        tudo = repr(dump())
        self.assertNotIn(SENHA, tudo)
        self.assertNotIn("senha-errada-123", tudo)
        self.assertNotIn(ck.split("=", 1)[1], tudo)

    def test_alvo_invalido_e_um_bloqueado_por_janela(self):
        usuarios.criar("ana@exemplo.test", SENHA, papel="dono")
        self.entrar("minhasenhasecreta", "x")  # senha digitada no campo do e-mail
        self.entrar("ana", "x")  # login curto que não existe
        self.entrar("a b<c>", "x")
        for _ in range(5):
            self.entrar("ana@exemplo.test", "senha-errada-123")
        for _ in range(4):
            self.assertEqual(self.entrar("ana@exemplo.test", "x")[0], 429)
        l = usuarios.auditoria()
        self.assertEqual([x["alvo"] for x in l if x["acao"] == "entrar-falhou"][-3:], ["(inválido)"] * 3)
        self.assertEqual(len([x for x in l if x["acao"] == "entrar-bloqueado"]), 1)
        self.assertNotIn("minhasenhasecreta", repr(dump()))

    def test_acao_pelo_painel_grava_quem_e_ip(self):
        ck, csrf = sessao_teste.cookie_e_csrf("operador", "op@exemplo.test")
        with mock.patch.object(acoes.linear, "comentar"):
            self.req("/acao/comentar", {"csrf": csrf, "issue": "KRN-9", "texto": "oi"}, cookie=ck,
                     cab={"X-Forwarded-For": "10.0.0.1, 198.51.100.4"})
        l = usuarios.auditoria()[0]
        self.assertEqual((l["quem"], l["acao"], l["alvo"], l["ip"]), ("op@exemplo.test", "comentar", "KRN-9", "198.51.100.4"))

    def test_convite_aceito(self):
        t = usuarios.convidar("nova@exemplo.test", "leitura")
        st, _, _ = self.req("/convite", {"t": t, "senha": SENHA, "senha2": SENHA})
        self.assertEqual(st, 303)
        acoes_ = [x["acao"] for x in usuarios.auditoria()]
        self.assertIn("convite-aceito", acoes_)
        self.assertNotIn(t, repr(dump()))
        self.assertNotIn(SENHA, repr(dump()))

    def test_tela_escapa_e_so_dono(self):
        usuarios.auditar("<script>x</script>", "comentar", '<td><script>alert(1)</script>', resultado="<b>ok</b>")
        st, corpo, _ = self.req("/config/auditoria", cookie=self.sessoes["dono"][0])
        self.assertEqual(st, 200)
        self.assertNotIn("<script>alert", corpo)
        self.assertNotIn("<script>x", corpo)
        self.assertNotIn("<td><script", corpo)
        self.assertNotIn("<b>ok", corpo)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", corpo)
        for papel in ("leitura", "operador"):
            self.assertEqual(self.req("/config/auditoria", cookie=self.sessoes[papel][0])[0], 403)
        self.assertIn('href="/config/auditoria"', self.req("/config/usuarios", cookie=self.sessoes["dono"][0])[1])

    def test_comecar_grava_o_passo_sem_o_que_foi_digitado(self):
        ck, _ = self.sessoes["dono"]
        with mock.patch.object(comecar, "ativo", return_value=True), \
                mock.patch.object(comecar, "aberto", return_value=False), \
                mock.patch.object(comecar, "csrf_ok", return_value=True), \
                mock.patch.object(comecar, "executar", return_value=("erro", "token ghp_segredo123 inválido", "github")):
            st, _, _ = self.req("/comecar", {"passo": "github", "token": "ghp_segredo123", "csrf": "x"}, cookie=ck)  # gitleaks:allow
        self.assertEqual(st, 303)
        l = usuarios.auditoria()[0]
        self.assertEqual((l["acao"], l["resultado"]), ("comecar:github", "erro"))
        self.assertTrue(l["quem"].startswith("dono-"))
        self.assertNotIn("ghp_segredo123", repr(dump()))
