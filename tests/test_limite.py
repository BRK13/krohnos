"""Limite de taxa do painel (4.4): login (5 por IP+e-mail, 20 por IP, em 15 min, antes do scrypt) e POSTs gerais
(60 por minuto por sessão). Servidor real numa porta efêmera; HUB_DATA em pasta temporária."""
import datetime as dt
import hashlib
import sqlite3
import threading
from unittest import mock

from hub import acoes, limite, usuarios
from tests import sessao_teste
from tests.test_papeis import Servidor

SENHA = sessao_teste.SENHA
T0 = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)


class LimiteTest(Servidor):
    def setUp(self):
        super().setUp()
        limite.esquecer()
        self.addCleanup(limite.esquecer)
        self.t = T0
        rel = mock.patch.object(limite, "agora", lambda: self.t)
        rel.start()
        self.addCleanup(rel.stop)
        usuarios.criar("ana@exemplo.test", SENHA, papel="dono")

    def entrar(self, email, senha, ip="203.0.113.7"):
        return self.req("/entrar", {"email": email, "senha": senha, "volta": "/"}, cab={"X-Forwarded-For": ip})

    def errar(self, n, email="ana@exemplo.test", ip="203.0.113.7"):
        for _ in range(n):
            self.assertEqual(self.entrar(email, "senha-errada-123", ip)[0], 200)

    def test_5_erros_6a_e_429_sem_scrypt(self):
        self.errar(5)
        with mock.patch("hashlib.scrypt", wraps=hashlib.scrypt) as espiao:
            st, corpo, h = self.entrar("ana@exemplo.test", SENHA)
        self.assertEqual(st, 429)
        self.assertEqual(h["Retry-After"], "900")
        self.assertIn("Muitas tentativas. Tente de novo em 15 minutos.", corpo)
        self.assertIn('action="/entrar"', corpo)
        espiao.assert_not_called()

    def test_mesma_mensagem_exista_ou_nao_o_email(self):
        self.errar(5)
        self.errar(5, "ninguem@exemplo.test")
        a = self.entrar("ana@exemplo.test", "x")
        b = self.entrar("ninguem@exemplo.test", "x")
        self.assertEqual((a[0], b[0]), (429, 429))
        self.assertEqual(a[1], b[1])

    def test_outro_email_do_mesmo_ip_ate_o_teto_de_20(self):
        self.errar(5)
        for i in range(15):  # 5 + 15 = 20 erros do IP, nenhum e-mail passa de 5
            self.errar(1, f"u{i}@exemplo.test")
        st, _, _ = self.entrar("outro@exemplo.test", "x")
        self.assertEqual(st, 429)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA, ip="198.51.100.9")[0], 303)  # outro IP segue

    def test_15_min_depois_libera(self):
        self.errar(5)
        self.t = T0 + dt.timedelta(minutes=14)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 429)
        self.t = T0 + dt.timedelta(minutes=15, seconds=1)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 303)

    def test_restart_mantem_o_bloqueio_pela_tabela(self):
        self.errar(5)
        limite.esquecer()  # processo novo: a memória some, a tabela fica
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 429)

    def test_login_certo_zera(self):
        self.errar(4)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 303)
        self.errar(4)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 303)
        limite.esquecer()
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 303)  # na tabela também zerou

    def test_tabela_sem_senha_e_limpa_as_velhas(self):
        self.errar(3)
        self.t = T0 + dt.timedelta(minutes=20)
        self.errar(1)
        con = sqlite3.connect(usuarios.banco())
        linhas = con.execute("SELECT chave, em FROM tentativa").fetchall()
        con.close()
        self.assertEqual(len(linhas), 2)  # só as do último erro (ip + ip-email)
        self.assertNotIn("senha-errada", repr(linhas))

    def test_rajada_paralela_nao_passa_de_5_scrypt(self):
        from hub import painel
        n, barreira, sts = 12, threading.Barrier(12), []

        def um():
            barreira.wait()
            sts.append(self.entrar("ana@exemplo.test", "senha-errada-123")[0])
        # teto de CPU folgado: quem decide é o limite (5 por IP+e-mail), não o timeout de 1 s numa máquina carregada
        with mock.patch.object(painel, "_SCRYPT", threading.BoundedSemaphore(12)), \
                mock.patch("hashlib.scrypt", wraps=hashlib.scrypt) as espiao:
            ts = [threading.Thread(target=um) for _ in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertLessEqual(espiao.call_count, 5)
        self.assertEqual(sorted(sts).count(429), n - espiao.call_count)
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 429)

    def test_cpu_ocupada_responde_429_e_nao_conta(self):
        from hub import painel
        with mock.patch.object(painel, "_SCRYPT", threading.BoundedSemaphore(1)) as sem:
            sem.acquire()
            with mock.patch.object(sem, "acquire", return_value=False):
                st, corpo, h = self.entrar("ana@exemplo.test", SENHA)
            sem.release()
        self.assertEqual((st, h["Retry-After"]), (429, "900"))
        self.errar(4)  # a recusa por CPU não contou como erro
        self.assertEqual(self.entrar("ana@exemplo.test", SENHA)[0], 303)

    def test_61o_post_no_minuto_e_429(self):
        ck, csrf = sessao_teste.cookie_e_csrf("dono")
        with mock.patch.object(acoes, "executar", return_value=(True, "ok")):
            for i in range(60):
                self.assertEqual(self.req("/acao/comentar", {"csrf": csrf}, cookie=ck)[0], 303, i)
            self.assertEqual(self.req("/acao/comentar", {"csrf": csrf}, cookie=ck)[0], 429)
            self.assertEqual(self.req("/sair", {"csrf": csrf}, cookie=ck)[0], 429)
            outra, csrf2 = sessao_teste.cookie_e_csrf("dono")  # outra sessão tem a cota dela
            self.assertEqual(self.req("/acao/comentar", {"csrf": csrf2}, cookie=outra)[0], 303)

    def test_post_janela_deslizante(self):
        for i in range(60):
            self.assertTrue(limite.post_ok("x", 100.0 + i * 0.1))
        self.assertFalse(limite.post_ok("x", 110.0))
        self.assertTrue(limite.post_ok("x", 160.01))  # o 1º saiu da janela

    def test_convite_sem_sessao_por_ip(self):
        for _ in range(60):
            self.assertEqual(self.req("/convite", {"t": "nada"})[0], 200)
        self.assertEqual(self.req("/convite", {"t": "nada"})[0], 429)


class SessoesVencidasTest(Servidor):
    def test_entrar_apaga_sessoes_vencidas(self):
        usuarios.criar("ana@exemplo.test", SENHA, papel="dono")
        velho = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
        ocioso = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=13)
        usuarios.entrar("ana@exemplo.test", SENHA, agora=velho)
        usuarios.entrar("ana@exemplo.test", SENHA, agora=ocioso)
        usuarios.entrar("ana@exemplo.test", SENHA)
        con = sqlite3.connect(usuarios.banco())
        n = con.execute("SELECT COUNT(*) FROM sessao WHERE email='ana@exemplo.test'").fetchone()[0]
        con.close()
        self.assertEqual(n, 1)
