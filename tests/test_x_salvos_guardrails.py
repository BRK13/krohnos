"""Guardrails do agente de conhecimento do X: cada um tem que falhar sem o código (hub.teste_vermelho)."""
import contextlib
import io
import json
import os
import re
import stat
import sys
import types
import tempfile
import unittest
from pathlib import Path

try:
    from hub.x_salvos import coletor as K
    from hub.x_salvos import config as C
    from hub.x_salvos import guardrails as G
except ImportError:
    G = C = K = None

PACOTE = Path(__file__).resolve().parent.parent / "hub" / "x_salvos"
SENTINELA = "SENTINELA_COOKIE_0123456789abcdef"
SENTINELA2 = "SENTINELA_CT0_fedcba9876543210"


class Base(unittest.TestCase):
    def setUp(self):
        if G is None:
            self.fail("pacote hub.x_salvos ausente")
        G._SEGREDOS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class TestSomenteLeitura(Base):
    def test_metodos_de_escrita_bloqueados(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "post"):
            with self.assertRaises(G.EscritaBloqueada):
                G.checar_requisicao(m, "https://x.com/i/api/1.1/qualquer")

    def test_leitura_passa(self):
        for m in ("GET", "HEAD", "OPTIONS", "get"):
            G.checar_requisicao(m, "https://x.com/i/api/graphql/abc/Bookmarks?variables=%7B%7D")

    def test_endpoints_de_escrita_bloqueados_mesmo_em_get(self):
        for u in ("https://x.com/i/api/graphql/h/CreateTweet", "https://x.com/i/api/graphql/h/FavoriteTweet",
                  "https://x.com/i/api/graphql/h/CreateRetweet", "https://x.com/i/api/graphql/h/CreateBookmark",
                  "https://x.com/i/api/graphql/h/DeleteBookmark", "https://x.com/i/api/1.1/friendships/create.json",
                  "https://x.com/i/api/1.1/friendships/destroy.json", "https://x.com/i/api/1.1/dm/new2.json",
                  "https://x.com/i/api/graphql/h/CreateFriendship", "https://x.com/i/api/1.1/favorites/create.json"):
            with self.assertRaises(G.EscritaBloqueada, msg=u):
                G.checar_requisicao("GET", u)

    def test_instalar_aborta_escrita_e_registra_sem_corpo(self):
        class Req:
            def __init__(self, m, u):
                self.method, self.url, self.post_data = m, u, "corpo-secreto"

        class Rota:
            def __init__(self, m, u):
                self.request, self.fim = Req(m, u), None

            def abort(self):
                self.fim = "abort"

            def continue_(self):
                self.fim = "continue"

        class Ctx:
            def route(self, padrao, handler):
                self.padrao, self.handler = padrao, handler

        ctx, bloqueadas = Ctx(), []
        G.instalar(ctx, registrar=bloqueadas.append)
        self.assertEqual(ctx.padrao, "**/*")
        ruim = Rota("POST", "https://x.com/i/api/graphql/h/CreateTweet?x=1")
        ok = Rota("GET", "https://x.com/i/bookmarks")
        ctx.handler(ruim, ruim.request)
        ctx.handler(ok, ok.request)
        self.assertEqual((ruim.fim, ok.fim), ("abort", "continue"))
        self.assertEqual(len(bloqueadas), 1)
        self.assertNotIn("corpo-secreto", json.dumps(bloqueadas))

    def test_clique_de_acao_recusado(self):
        class Pagina:
            cliques = 0

            def query_selector_all(self, s):
                return [self]

            def click(self):
                Pagina.cliques += 1

        for s in ('[data-testid="like"]', '[data-testid="reply"]', '[data-testid="retweet"]', '[data-testid="follow"]',
                  '[data-testid="unfollow"]', '[data-testid="bookmark"]', '[data-testid="dm"]', '[data-testid="tweetButton"]',
                  'button'):
            with self.assertRaises(G.EscritaBloqueada, msg=s):
                G.clicar_seguro(Pagina(), s)
        self.assertEqual(Pagina.cliques, 0)
        self.assertEqual(G.clicar_seguro(Pagina(), 'button:has-text("Show more replies")'), 1)
        self.assertEqual(Pagina.cliques, 1)

    def test_leitura_nao_e_confundida_com_escrita(self):
        for u in ("https://x.com/newsreport/status/1", "https://x.com/i/bookmarks?q=report",
                  "https://x.com/i/api/graphql/abc/Bookmarks?variables=%7B%22q%22%3A%22report%20createtweet%22%7D",
                  "https://x.com/i/api/graphql/abc/BookmarkFoldersSlice", "https://x.com/i/api/graphql/abc/BookmarkFolderTimeline"):
            G.checar_requisicao("GET", u)
        for u in ("https://x.com/i/api/graphql/abc/CreateTweet", "https://x.com/i/api/graphql/abc/createtweet?x=1",
                  "https://api.x.com/1.1/users/report_spam.json", "https://x.com/i/api/1.1/mutes/users/create.json"):
            with self.assertRaises(G.EscritaBloqueada, msg=u):
                G.checar_requisicao("GET", u)
        with self.assertRaises(G.EscritaBloqueada):
            G.checar_requisicao("POST", "https://x.com/newsreport/status/1")

    def test_url_ilegivel_aborta_fail_closed(self):
        ctx = FakeCtx()
        G.instalar(ctx, registrar=lambda r: None)
        r = RotaFalsa("GET", "https://[x.com/i")
        ctx.handler(r, r.request)
        self.assertEqual(r.fim, "abort")

    def test_brechas_de_codificacao_e_subdominio(self):
        for u in ("https://x.com/i/api/graphql/abc/Create%54weet", "https://x.com/i/api/1.1/friendships%2Fcreate.json",
                  "https://mobile.x.com/i/api/graphql/abc/CreateTweet", "https://upload.twitter.com/1.1/favorites/create.json",
                  "https://API.X.COM/1.1/dm/new2.json"):
            with self.assertRaises(G.EscritaBloqueada, msg=u):
                G.checar_requisicao("GET", u)
        for u in ("https://x.com./i/api/graphql/abc/CreateTweet", "https://api.twitter.com./1.1/dm/new2.json"):
            with self.assertRaises(G.EscritaBloqueada, msg=u):
                G.checar_requisicao("GET", u)
        G.checar_requisicao("GET", "https://notx-com.example/i/api/graphql/abc/CreateTweet")  # outro domínio: não é do X

    def test_telemetria_aborta_em_silencio(self):
        class Req:
            method, url = "POST", "https://x.com/i/api/1.1/jot/client_event.json"

        class Rota:
            request, fim = Req(), None

            def abort(self):
                self.fim = "abort"
        ctx, bloq = FakeCtx(), []
        G.instalar(ctx, registrar=bloq.append)
        r = Rota()
        ctx.handler(r, r.request)
        self.assertEqual((r.fim, bloq), ("abort", []))

    def test_abrir_instala_a_trava_no_contexto(self):
        ctx = FakeCtx()
        cfg = C.carregar(None, env={"KROHNOS_X_SALVOS_COOKIES": str(self.dir / "nao.json"), "KROHNOS_X_SALVOS_CHROME": "/nao/existe"})
        _, c, _, _ = K.abrir(FakePw(ctx), cfg)
        self.assertIs(c, ctx)
        self.assertEqual(ctx.padrao, "**/*")
        self.assertEqual(ctx.kwargs.get("service_workers"), "block")  # Service Worker escaparia do route
        post, get = RotaFalsa("POST", "https://x.com/i/api/graphql/h/CreateTweet"), RotaFalsa("GET", "https://x.com/i/bookmarks")
        with contextlib.redirect_stdout(io.StringIO()):
            ctx.handler(post, post.request)
        ctx.handler(get, get.request)
        self.assertEqual((post.fim, get.fim), ("abort", "continue"))

    def test_refazer_fio_abre_pelo_abrir(self):
        import sys
        import types
        from unittest import mock
        ctx, chamadas = FakeCtx(), []
        mod = types.ModuleType("playwright.sync_api")

        @contextlib.contextmanager
        def sync_playwright():
            yield FakePw(ctx)
        mod.sync_playwright = sync_playwright
        real = K.abrir
        arq = self.dir / "2026-10-21.jsonl"
        arq.write_text(json.dumps({"id": "1", "url": "https://x.com/a/status/1", "texto": "oi"}) + "\n")
        cfg = C.carregar(None, env={"KROHNOS_X_SALVOS_COOKIES": str(self.dir / "nao.json"), "KROHNOS_X_SALVOS_CHROME": "/nao/existe",
                                    "KROHNOS_X_SALVOS_SAIDA": str(self.dir)})

        class Pagina:
            url = "https://x.com/a/status/1"
        ctx.new_page = lambda: Pagina()
        with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": mod}), \
                mock.patch.object(K, "abrir", side_effect=lambda *a: chamadas.append(1) or real(*a)), \
                mock.patch.object(K, "detalhar", side_effect=lambda page, it, taxa: {**it, "fio": [{"id": "2"}], "links": []}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(K.refazer_fio(cfg, arq), 0)
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(ctx.padrao, "**/*")
        saida = [json.loads(l) for l in (self.dir / "2026-10-21-fio.jsonl").read_text().splitlines()]
        self.assertEqual([(x["id"], x["fio"]) for x in saida], [("1", [{"id": "2"}])])


class FakeCtx:
    padrao = handler = None

    def route(self, padrao, handler):
        self.padrao, self.handler = padrao, handler

    def add_cookies(self, c):
        pass

    def new_page(self):
        return object()

    def cookies(self, url=None):
        return getattr(self, "cookies_novos", [])


class FakeNav:
    def __init__(self, ctx):
        self.ctx = ctx

    def new_context(self, **k):
        self.ctx.kwargs = k
        return self.ctx

    def close(self):
        pass


class FakePw:
    def __init__(self, ctx):
        self.chromium = self
        self.ctx = ctx

    def launch(self, **k):
        return FakeNav(self.ctx)


class RotaFalsa:
    def __init__(self, m, u):
        self.request = type("R", (), {"method": m, "url": u, "post_data": None})()
        self.fim = None

    def abort(self):
        self.fim = "abort"

    def continue_(self):
        self.fim = "continue"


class TestSessaoDoUsuario(Base):
    def test_cookies_so_os_tres(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"auth_token": SENTINELA, "ct0": SENTINELA2, "twid": "u%3D1", "outro": "x"}))
        os.chmod(p, 0o600)
        nomes = sorted(c["name"] for c in G.ler_cookies(p))
        self.assertEqual(nomes, ["auth_token", "ct0", "twid"])

    def test_rejeita_credencial_de_login(self):
        for chave in ("password", "senha", "username", "Password"):
            p = self.dir / "c.json"
            p.write_text(json.dumps({"auth_token": SENTINELA, chave: "x"}))
            os.chmod(p, 0o600)
            with self.assertRaises(G.CookiesInvalidos, msg=chave) as e:
                G.ler_cookies(p)
            self.assertNotIn(SENTINELA, str(e.exception))

    def test_json_malformado_erro_claro_sem_valor(self):
        p = self.dir / "c.json"
        p.write_text('{"auth_token": "' + SENTINELA)
        os.chmod(p, 0o600)
        with self.assertRaises(G.CookiesInvalidos) as e:
            G.ler_cookies(p)
        self.assertNotIn(SENTINELA, str(e.exception))

    def test_sem_arquivo_lista_vazia(self):
        self.assertEqual(G.ler_cookies(self.dir / "nao.json"), [])

    def test_fonte_sem_fluxo_de_login(self):
        fontes = sorted(PACOTE.glob("*.py"))
        self.assertGreaterEqual(len(fontes), 4)
        for f in fontes:
            txt = f.read_text()
            self.assertIsNone(re.search(r"\.(fill|type|press_sequentially)\(|keyboard\.(type|insert_text)", txt), f"{f.name}: digita em campo")
            self.assertNotIn("/i/flow/login", txt, f.name)
            self.assertIsNone(re.search(r"goto\([^)]*login", txt), f"{f.name}: navega para login")

    def test_nao_navega_para_login(self):
        class Pagina:
            visitou = []

            def goto(self, u, **k):
                self.visitou.append(u)

        for u in ("https://x.com/i/flow/login", "https://x.com/login", "https://x.com/logout"):
            with self.assertRaises(G.SessaoCaiu):
                G.ir(Pagina(), u, G.LimiteTaxa(0, relogio=lambda: 0, dormir=lambda s: None))
        self.assertEqual(Pagina.visitou, [])


class TestLimiteETeto(Base):
    def test_intervalo_minimo_entre_navegacoes(self):
        t = [100.0]
        dormiu = []

        def dormir(s):
            dormiu.append(s)
            t[0] += s
        lim = G.LimiteTaxa(1.5, relogio=lambda: t[0], dormir=dormir)
        lim.espera()
        self.assertEqual(dormiu, [])
        t[0] += 0.5
        lim.espera()
        self.assertEqual(dormiu, [1.0])
        t[0] += 5
        lim.espera()
        self.assertEqual(dormiu, [1.0])

    def test_ir_respeita_a_taxa(self):
        t = [0.0]
        dormiu = []

        def dormir(s):
            dormiu.append(s)
            t[0] += s

        class Pagina:
            def goto(self, u, **k):
                pass
        lim = G.LimiteTaxa(2.0, relogio=lambda: t[0], dormir=dormir)
        G.ir(Pagina(), "https://x.com/a/status/1", lim)
        G.ir(Pagina(), "https://x.com/a/status/2", lim)
        self.assertEqual(dormiu, [2.0])

    def test_teto_de_rolagens(self):
        r = G.Rodada(max_paginas=3, max_itens=10)
        for _ in range(3):
            r.rolar()
        with self.assertRaises(G.TetoAtingido):
            r.rolar()

    def test_teto_de_itens(self):
        r = G.Rodada(max_paginas=40, max_itens=2)
        r.item()
        r.item()
        with self.assertRaises(G.TetoAtingido):
            r.item()

    def test_sessao_caiu_por_url(self):
        for u in ("https://x.com/i/flow/login", "https://x.com/i/jf/onboarding/web?redirect_after_login=%2F&mode=login",
                  "https://x.com/logout", "https://x.com/login?x=1"):
            self.assertTrue(G.sessao_caiu(u), u)
        self.assertFalse(G.sessao_caiu("https://x.com/i/bookmarks"))
        self.assertFalse(G.sessao_caiu("https://x.com/loginradius/status/1"))
        self.assertFalse(G.sessao_caiu("https://x.com/login/status/1"))
        self.assertFalse(G.sessao_caiu("https://x.com/logout/status/1"))
        self.assertTrue(G.sessao_caiu("https://x.com/login"))
        self.assertFalse(G.sessao_caiu("https://x.com/fulano/status/1?q=login"))
        self.assertFalse(G.sessao_caiu("https://x.com/fulano/status/123"))


class TestSegredos(Base):
    def test_mascarar(self):
        cookies = [{"name": "auth_token", "value": SENTINELA}, {"name": "ct0", "value": SENTINELA2}]
        s = G.mascarar(f"falha {SENTINELA} e {SENTINELA2}", cookies)
        self.assertNotIn(SENTINELA, s)
        self.assertNotIn(SENTINELA2, s)
        self.assertEqual(s.count("***"), 2)
        self.assertNotIn(SENTINELA, G.mascarar(f"x {SENTINELA}", {"auth_token": SENTINELA}))

    def test_log_nunca_mostra_valor(self):
        G.registrar_segredos([SENTINELA])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            G.log(f"cookie {SENTINELA} apareceu")
            G.log_erro(RuntimeError(f"quebrou com {SENTINELA}"))
        self.assertNotIn(SENTINELA, out.getvalue() + err.getvalue())
        self.assertIn("***", out.getvalue())

    def test_erro_cortado_no_meio_do_segredo_nao_vaza(self):
        G.registrar_segredos([SENTINELA])
        e = RuntimeError("x" * (150 - len(SENTINELA) // 2) + SENTINELA)  # o corte de 150 cairia no meio do valor
        d = G.descrever_erro(e)
        self.assertLessEqual(len(d), 150)
        for i in range(4, len(SENTINELA) + 1):
            self.assertNotIn(SENTINELA[:i], d)

    def test_ler_cookies_registra_para_o_log(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"auth_token": SENTINELA}))
        os.chmod(p, 0o600)
        G.ler_cookies(p)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            G.log(f"vazou {SENTINELA}")
        self.assertNotIn(SENTINELA, out.getvalue())

    def test_gravar_cookies_600_atomico(self):
        p = self.dir / "sub" / "c.json"
        G.gravar_cookies(p, {"auth_token": SENTINELA, "ct0": SENTINELA2, "lixo": "x"})
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(json.loads(p.read_text()), {"auth_token": SENTINELA, "ct0": SENTINELA2})
        self.assertEqual([x.name for x in p.parent.iterdir()], ["c.json"])
        G.gravar_cookies(p, {"auth_token": SENTINELA})  # regravar mantém 600
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_permissao_mais_aberta_e_corrigida_sem_valor(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"auth_token": SENTINELA}))
        os.chmod(p, 0o644)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            corrigiu = G.garantir_permissao(p)
        self.assertTrue(corrigiu)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertNotIn(SENTINELA, out.getvalue())
        self.assertTrue(out.getvalue())
        self.assertFalse(G.garantir_permissao(p))

    def test_ler_cookies_corrige_permissao(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"auth_token": SENTINELA}))
        os.chmod(p, 0o666)
        with contextlib.redirect_stdout(io.StringIO()):
            G.ler_cookies(p)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_erro_gravado_vai_mascarado(self):
        G.registrar_segredos([SENTINELA])
        with contextlib.redirect_stdout(io.StringIO()):
            K.gravar_erro(self.dir, f"sessão {SENTINELA}")
        self.assertNotIn(SENTINELA, (self.dir / "ERRO").read_text())


class TestTermos(Base):
    def test_aviso_tem_o_essencial(self):
        t = G.AVISO_TERMOS.lower()
        for k in ("uso pessoal", "salvos", "scraping em massa", "500 itens por coleta", "uma coleta por dia", "--forcar", "somente leitura"):
            self.assertIn(k, t)
        self.assertIn("fica fora deste agente", t)  # o código não faz busca pública: o aviso não pode dizer que faz
        self.assertNotIn("e a busca pública;", t)

    def test_teto_500(self):
        self.assertEqual(G.validar_max_itens(500), 500)
        with self.assertRaises(ValueError):
            G.validar_max_itens(501)
        with self.assertRaises(ValueError):
            G.validar_max_itens(0)

    def test_uma_coleta_por_dia(self):
        self.assertFalse(G.pode_coletar({"ultima_coleta": "2026-10-21"}, "2026-10-21", forcar=False))
        self.assertTrue(G.pode_coletar({"ultima_coleta": "2026-10-20"}, "2026-10-21", forcar=False))
        self.assertTrue(G.pode_coletar({"ultima_coleta": "2026-10-21"}, "2026-10-21", forcar=True))
        self.assertTrue(G.pode_coletar({}, "2026-10-21", forcar=False))

    def test_ajuda_e_primeira_rodada_imprimem_aviso(self):
        from hub.x_salvos import __main__ as M
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            M.main(["--ajuda"])
        self.assertIn(G.AVISO_TERMOS, out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            K.avisar_primeira_rodada({"ids": [], "ultima_coleta": None})
            K.avisar_primeira_rodada({"ids": ["1"], "ultima_coleta": "2026-10-20"})
        self.assertEqual(out.getvalue().count("uso pessoal"), 1)


class TestColetorPuro(Base):
    def test_estado_e_jsonl(self):
        self.assertEqual(K.carregar_estado(self.dir), {"ids": [], "ultima_coleta": None})
        K.gravar_json(self.dir / "vistos.json", {"ids": ["1"], "ultima_coleta": "2026-10-21"})
        self.assertEqual(K.carregar_estado(self.dir)["ids"], ["1"])
        arq = K.arquivo_do_dia(self.dir, "2026-10-21")
        self.assertEqual(arq.name, "2026-10-21.jsonl")
        K.anexar_jsonl(arq, {"id": "1", "texto": "olá"})
        K.anexar_jsonl(arq, {"id": "2"})
        self.assertEqual([json.loads(l)["id"] for l in arq.read_text().splitlines()], ["1", "2"])
        self.assertIn("olá", arq.read_text())

    def test_expandir_so_t_co(self):
        self.assertEqual(K.expandir("https://exemplo.com/a"), "https://exemplo.com/a")

    def test_trava_impede_segunda_coleta(self):
        caminho = self.dir / "run" / "x.lock"  # pasta ainda inexistente
        a = K.travar(caminho)
        self.assertIsNotNone(a)
        self.assertIsNone(K.travar(caminho))
        a.close()
        b = K.travar(caminho)
        self.assertIsNotNone(b)
        b.close()

    def test_main_usa_a_trava_da_config(self):
        from hub.x_salvos import __main__ as M
        trava = self.dir / "outra.lock"
        cfgp = self.dir / "x.toml"
        cfgp.write_text(f'saida = "{self.dir}/s"\ntrava = "{trava}"\n')
        seg = K.travar(trava)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.main(["coletar", "--config", str(cfgp)]), 0)
        self.assertIn("outra coleta já está rodando", out.getvalue())
        seg.close()


if __name__ == "__main__":
    unittest.main()


class TestCookiesOrigem(Base):
    """Cookies exportados em outra máquina: o coletor copia a origem quando ela é mais nova."""

    def cfg(self, origem):
        env = {"KROHNOS_X_SALVOS_COOKIES": str(self.dir / "c" / "cookies.json"), "KROHNOS_X_SALVOS_SAIDA": str(self.dir)}
        if origem is not None:
            env["KROHNOS_X_SALVOS_COOKIES_ORIGEM"] = str(origem)
        return C.carregar(None, env=env)

    def origem(self, nome="x.json", valor=SENTINELA, mtime=None):
        o = self.dir / nome
        o.write_text(json.dumps({"auth_token": valor, "ct0": SENTINELA2, "twid": "u%3D1"}))
        if mtime:
            os.utime(o, (mtime, mtime))
        return o

    def test_copia_quando_cookies_nao_existe_com_600_e_log_sem_valor(self):
        o, cfg = self.origem(), None
        cfg = self.cfg(o)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(K.atualizar_cookies(cfg))
        self.assertEqual(json.loads(cfg.cookies.read_text())["auth_token"], SENTINELA)
        self.assertEqual(stat.S_IMODE(cfg.cookies.stat().st_mode), 0o600)
        self.assertNotIn(SENTINELA, buf.getvalue())
        self.assertNotIn(SENTINELA2, buf.getvalue())

    def test_so_copia_quando_a_origem_e_mais_nova(self):
        o = self.origem(mtime=2_000_000_000)
        cfg = self.cfg(o)
        G.gravar_cookies(cfg.cookies, {"auth_token": "velho", "ct0": "velho"})
        os.utime(cfg.cookies, (2_100_000_000, 2_100_000_000))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(K.atualizar_cookies(cfg))
            self.assertEqual(json.loads(cfg.cookies.read_text())["auth_token"], "velho")
            os.utime(o, (2_200_000_000, 2_200_000_000))
            self.assertTrue(K.atualizar_cookies(cfg))
        self.assertEqual(json.loads(cfg.cookies.read_text())["auth_token"], SENTINELA)

    def test_pasta_de_chegada_usa_o_mesmo_nome_do_arquivo(self):
        self.origem("cookies.json")
        cfg = self.cfg(self.dir)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(K.atualizar_cookies(cfg))

    def test_pasta_de_chegada_aceita_x_cookies_json(self):
        self.origem("x-cookies.json")
        cfg = self.cfg(self.dir)  # o arquivo de destino se chama cookies.json
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(K.atualizar_cookies(cfg))
        self.assertEqual(json.loads(cfg.cookies.read_text())["auth_token"], SENTINELA)

    def test_sem_origem_ou_origem_ausente_nao_faz_nada(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(K.atualizar_cookies(self.cfg(None)))
            self.assertFalse(K.atualizar_cookies(self.cfg(self.dir / "nao.json")))

    def test_origem_com_login_e_recusada(self):
        o = self.dir / "x.json"
        o.write_text(json.dumps({"auth_token": SENTINELA, "password": "s"}))
        cfg = self.cfg(o)
        with self.assertRaises(G.CookiesInvalidos):
            K.atualizar_cookies(cfg)
        self.assertFalse(cfg.cookies.exists())

    def test_origem_ilegivel_erro_claro_sem_valor(self):
        from unittest import mock
        o = self.origem()
        cfg = self.cfg(o)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "negado", str(o))):
            with self.assertRaises(G.CookiesInvalidos) as e:
                K.atualizar_cookies(cfg)
        self.assertNotIn(str(o), str(e.exception))
        self.assertFalse(cfg.cookies.exists())

    def test_main_grava_erro_e_sai_2_quando_a_origem_e_ilegivel(self):
        from unittest import mock
        from hub.x_salvos import __main__ as M
        o = self.origem()
        cfgp = self.dir / "x.toml"
        cfgp.write_text(f'cookies = "{self.dir}/c/cookies.json"\ncookies_origem = "{o}"\nsaida = "{self.dir}/s"\n')
        real = Path.read_text

        def ler(self_, *a, **k):
            if self_ == o:
                raise PermissionError(13, "negado")
            return real(self_, *a, **k)
        fake = types.ModuleType("playwright.sync_api")

        @contextlib.contextmanager
        def sync_playwright():
            yield FakePw(FakeCtx())
        fake.sync_playwright = sync_playwright
        with mock.patch.object(Path, "read_text", ler), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}):
            self.assertEqual(M.main(["coletar", "--forcar", "--config", str(cfgp)]), 2)
        self.assertIn("cookies inválidos", (self.dir / "s" / "ERRO").read_text())

    def test_login_ampliado_recusado(self):
        for chave in ("email", "login", "otp", "token_2fa", "user"):
            p = self.dir / "c.json"
            p.write_text(json.dumps({"auth_token": SENTINELA, chave: "x"}))
            os.chmod(p, 0o600)
            with self.assertRaises(G.CookiesInvalidos, msg=chave):
                G.ler_cookies(p)

    def test_abrir_atualiza_antes_de_ler(self):
        cfg = self.cfg(self.origem())
        cfg.chrome = "/nao/existe"
        with contextlib.redirect_stdout(io.StringIO()):
            _, _, _, cookies = K.abrir(FakePw(FakeCtx()), cfg)
        self.assertIn("auth_token", [c["name"] for c in cookies])


def _modulo_playwright(ctx):
    mod = types.ModuleType("playwright.sync_api")

    @contextlib.contextmanager
    def sync_playwright():
        yield FakePw(ctx)
    mod.sync_playwright = sync_playwright
    return {"playwright": types.ModuleType("playwright"), "playwright.sync_api": mod}


class Pagina:
    """Página falsa: goto vai para a URL (ou para o login), evaluate devolve os cards, mouse.wheel conta rolagens."""

    def __init__(self, cards=(), login_ao_ir=False):
        self.url, self.cards, self.login_ao_ir, self.visitados, self.rolagens = "", list(cards), login_ao_ir, [], 0
        self.mouse = self

    def goto(self, u, **k):
        self.visitados.append(u)
        self.url = "https://x.com/i/flow/login" if self.login_ao_ir else u

    def wait_for_selector(self, *a, **k):
        pass

    def evaluate(self, js):
        return list(self.cards)

    def wheel(self, x, y):
        self.rolagens += 1


class TestParada(Base):
    """Guardrail 3 de ponta a ponta: sessão caída para sem insistir; teto de rolagens para e termina normal."""

    def montar(self, principal, aba=None, **env):
        from unittest import mock
        cookies = self.dir / "c.json"
        G.gravar_cookies(cookies, {"auth_token": SENTINELA, "ct0": SENTINELA2})
        e = {"KROHNOS_X_SALVOS_COOKIES": str(cookies), "KROHNOS_X_SALVOS_SAIDA": str(self.dir / "s"), "KROHNOS_X_SALVOS_CHROME": "/nao/existe",
             "KROHNOS_X_SALVOS_LIMITE_TAXA_S": "0.001", **env}
        self.cfg = C.carregar(None, env=e)
        self.ctx = FakeCtx()
        paginas = [principal, aba or Pagina()]
        self.ctx.new_page = lambda: paginas.pop(0)
        for alvo in (mock.patch.object(K.time, "sleep"), mock.patch.object(K, "pausa"),
                     mock.patch.dict(sys.modules, _modulo_playwright(self.ctx)), contextlib.redirect_stdout(io.StringIO())):
            alvo.__enter__()
            self.addCleanup(alvo.__exit__, None, None, None)

    def cards(self, n):
        return [{"id": str(i), "autor": "a", "url": f"https://x.com/a/status/{i}", "texto": "t"} for i in range(1, n + 1)]

    def test_sessao_caida_na_lista_grava_erro_e_nao_tenta_login(self):
        principal = Pagina(login_ao_ir=True)
        self.montar(principal)
        self.assertEqual(K.coletar(self.cfg, forcar=True), 2)
        self.assertIn("sessão do X expirada", (self.cfg.saida / "ERRO").read_text())
        self.assertEqual(principal.visitados, ["https://x.com/i/bookmarks"])  # uma navegação só: sem login, sem nova tentativa
        self.assertFalse((self.cfg.saida / "vistos.json").exists())  # ultima_coleta intocada
        self.assertEqual(K.carregar_estado(self.cfg.saida)["ultima_coleta"], None)

    def test_aba_de_detalhe_cai_no_meio(self):
        from unittest import mock
        for modo in ("excecao", "url"):
            with self.subTest(modo):
                self.setUp()
                chamadas = []

                def detalhar(page, item, taxa):
                    chamadas.append(item["id"])
                    if len(chamadas) == 2:
                        if modo == "excecao":
                            raise G.SessaoCaiu("caiu")
                        page.url = "https://x.com/i/flow/login"
                    return item
                aba = Pagina()
                self.montar(Pagina(cards=self.cards(3)), aba)
                with mock.patch.object(K, "detalhar", side_effect=detalhar):
                    self.assertEqual(K.coletar(self.cfg, forcar=True), 2)
                self.assertEqual(chamadas, ["1", "2"])  # parou no que caiu, não insistiu no 3
                gravados = [json.loads(l)["id"] for l in K.arquivo_do_dia(self.cfg.saida, __import__("datetime").date.today().isoformat()).read_text().splitlines()]
                self.assertEqual(gravados, ["1"])
                est = K.carregar_estado(self.cfg.saida)
                self.assertEqual(est["ids"], ["1"])
                self.assertEqual(est["ultima_coleta"], __import__("datetime").date.today().isoformat())
                self.assertIn("caiu no meio da coleta", (self.cfg.saida / "ERRO").read_text())

    def test_teto_de_rolagens_para_e_retorna_normal(self):
        from unittest import mock
        principal = Pagina(cards=self.cards(1))
        self.montar(principal, KROHNOS_X_SALVOS_MAX_PAGINAS="2")
        with mock.patch.object(K, "detalhar", side_effect=lambda page, item, taxa: item):
            self.assertEqual(K.coletar(self.cfg, forcar=True), 0)
        self.assertEqual(principal.rolagens, 2)  # sem o teto rolaria até 4 sem novidade
        self.assertFalse((self.cfg.saida / "ERRO").exists())
        self.assertEqual(K.carregar_estado(self.cfg.saida)["ids"], ["1"])


class TestRefazerFioTeto(Base):
    def test_teto_de_itens_e_salva_cookies(self):
        from unittest import mock
        ctx = FakeCtx()
        ctx.cookies_novos = [{"name": "auth_token", "value": SENTINELA2}, {"name": "ct0", "value": "novo"}]
        ctx.new_page = lambda: Pagina()
        arq = self.dir / "2026-10-21.jsonl"
        arq.write_text("".join(json.dumps({"id": str(i), "url": f"https://x.com/a/status/{i}", "texto": "t"}) + "\n" for i in range(1, 4)))
        cookies = self.dir / "c.json"
        G.gravar_cookies(cookies, {"auth_token": SENTINELA})
        cfg = C.carregar(None, env={"KROHNOS_X_SALVOS_COOKIES": str(cookies), "KROHNOS_X_SALVOS_CHROME": "/nao/existe",
                                    "KROHNOS_X_SALVOS_SAIDA": str(self.dir), "X_MAX_ITENS": "2", "KROHNOS_X_SALVOS_LIMITE_TAXA_S": "0.001"})
        with mock.patch.dict(sys.modules, _modulo_playwright(ctx)), mock.patch.object(K, "detalhar", side_effect=lambda p, it, t: it), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(K.refazer_fio(cfg, arq), 0)
        feitos = [json.loads(l)["id"] for l in (self.dir / "2026-10-21-fio.jsonl").read_text().splitlines()]
        self.assertEqual(feitos, ["1", "2"])
        self.assertEqual(json.loads(cookies.read_text())["auth_token"], SENTINELA2)  # ct0 que o X rodou é regravado
        self.assertEqual(stat.S_IMODE(cookies.stat().st_mode), 0o600)


class TestMainExcecao(Base):
    def test_excecao_inesperada_vira_erro_mascarado_e_codigo_2(self):
        from unittest import mock
        from hub.x_salvos import __main__ as M
        cfgp = self.dir / "x.toml"
        cfgp.write_text(f'saida = "{self.dir}/s"\ntrava = "{self.dir}/t.lock"\n')
        G.registrar_segredos([SENTINELA])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(K, "coletar", side_effect=RuntimeError(f"estourou com {SENTINELA}")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(M.main(["coletar", "--config", str(cfgp)]), 2)
        erro = (self.dir / "s" / "ERRO").read_text()
        self.assertIn("erro inesperado", erro)
        for texto in (out.getvalue(), err.getvalue(), erro):
            self.assertNotIn(SENTINELA, texto)
