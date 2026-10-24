import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub import armazem, seomator as S

ABRIR = S.abrir_issues  # o de verdade; setUp troca S.abrir_issues por um falso
META = {"perf-page-weight": {"name": "Page Weight", "description": "d", "fix": "Reduza o peso"}}
PSI = {"id": "psi", "nome": "Cliente G", "dominio": "site-e.exemplo.com"}
FSITE06 = {"id": "fsite06", "nome": "fsite06", "dominio": "fsite06-com-br.example"}


def _brt(dia, h, m=0):
    return dt.datetime.fromisoformat(f"{dia}T{h:02d}:{m:02d}").replace(tzinfo=S.BRT)


def urlset(*urls):
    return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"


def indice(*docs):
    return "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in docs) + "</sitemapindex>"


def achado(regra, st="fail", peso=5, cat="perf"):
    return {"ruleId": regra, "cat": cat, "status": st, "weight": peso, "message": f"msg {regra}"}


def pagina(url, nota=90, achados=()):
    return {"url": url, "nota": nota, "cats": {"perf": 80, "core": 100}, "achados": list(achados)}


class Sitemaps:
    """baixar() falso: {url: xml}; o que não está no dicionário dá erro 404."""

    def __init__(self, docs):
        self.docs = docs
        self.pedidos = []

    def __call__(self, url):
        self.pedidos.append(url)
        if url not in self.docs:
            raise OSError(f"404 {url}")
        return self.docs[url]


class Rodar:
    """subprocess.run falso para lote.mjs: devolve uma página por URL; `quebra` = domínios cujo lote sai com -6;
    `achados` = {url: [achados]}; `http` = {url: 404}."""

    def __init__(self, quebra=(), achados=None, http=None):
        self.quebra, self.achados, self.http, self.lotes, self.cmds = quebra, achados or {}, http or {}, [], []

    def __call__(self, cmd, **kw):
        self.cmds.append((cmd, kw))
        ent, sai = Path(cmd[-2]), Path(cmd[-1])
        urls = json.loads(ent.read_text())["urls"]
        self.lotes.append(urls)
        if any(d in u for u in urls for d in self.quebra):
            return SimpleNamespace(returncode=-6, stderr="FATAL ERROR: JavaScript heap out of memory\n  ok")
        sai.write_text(json.dumps({
            "paginas": [pagina(u, achados=self.achados.get(u, [])) for u in urls if u not in self.http],
            "erros": [{"url": u, "erro": f"The page returned HTTP {self.http[u]}.", "http": str(self.http[u])}
                      for u in urls if u in self.http], "meta": META}))
        return SimpleNamespace(returncode=0, stderr="")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"SEOMATOR_LIMITADO": "1"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(S, "alvos", return_value=[PSI, FSITE06])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(S, "abrir_issues")  # a noite nunca chama o Linear de verdade
        p.start()
        self.addCleanup(p.stop)

    def mapa(self, dominio, n, secao="sitemap.xml"):
        return {f"https://{dominio}/{secao}": urlset(*[f"https://{dominio}/p{i}" for i in range(n)])}


class SitemapTest(Base):
    def test_secoes_seguem_indice_e_robots(self):
        b = Sitemaps({"https://x.com/robots.txt": "Sitemap: https://x.com/s.xml\nSitemap: https://x.com/s.xml",
                      "https://x.com/s.xml": indice("https://x.com/a.xml", "https://x.com/b.xml"),
                      "https://x.com/a.xml": urlset("https://x.com/1"), "https://x.com/b.xml": urlset("https://x.com/2"),
                      "https://x.com/sitemap.xml": urlset("https://x.com/")})
        self.assertEqual(S.secoes_do_site({"id": "x", "dominio": "x.com"}, b, self.d),
                         ["https://x.com/sitemap.xml", "https://x.com/a.xml", "https://x.com/b.xml"])
        n = len(b.pedidos)
        S.secoes_do_site({"id": "x", "dominio": "x.com"}, b, self.d)
        self.assertEqual(len(b.pedidos), n)  # cache de 2 dias

    def test_sem_sitemap_levanta_com_motivo(self):
        with self.assertRaisesRegex(RuntimeError, "sitemap não encontrado"):
            S.secoes_do_site({"id": "x", "dominio": "x.com"}, Sitemaps({}), self.d)

    def test_urls_so_https_do_dominio_sem_repetir(self):
        b = Sitemaps({"s": urlset("https://x.com/a", "http://x.com/b", "https://outro-com.example/c", "https://www.x.com/d", "https://x.com/a")})
        self.assertEqual(S.urls_da_secao({"dominio": "x.com"}, "s", b), ["https://x.com/a", "https://www.x.com/d"])

    def test_uma_secao_varre_em_sequencia_e_volta(self):
        b = Sitemaps(self.mapa("x.com", 150))
        site, cur, vistos = {"id": "x", "dominio": "x.com"}, {}, []
        for _ in range(4):
            urls, cur, total = S.proximo_lote(site, ["https://x.com/sitemap.xml"], cur, b)
            vistos.append(urls)
        self.assertEqual((len(vistos[0]), len(vistos[1]), len(vistos[2]), total), (60, 60, 30, 150))
        self.assertEqual(vistos[3][0], "https://x.com/p0")  # deu a volta
        self.assertEqual(cur["voltas"], 1)

    def test_muitas_secoes_pegam_fatia_de_cada_e_cobrem_todas_na_semana(self):
        docs = {f"https://x.com/s{i}.xml": urlset(*[f"https://x.com/{i}/{j}" for j in range(100)]) for i in range(20)}
        b, site, cur, secoes_vistas = Sitemaps(docs), {"id": "x", "dominio": "x.com"}, {}, set()
        for _ in range(3):  # 3 lotes × 7 seções = 21 ≥ 20
            urls, cur, _ = S.proximo_lote(site, list(docs), cur, b)
            self.assertEqual(len(urls), 56)
            secoes_vistas |= {u.split("/")[3] for u in urls}
        self.assertEqual(len(secoes_vistas), 20)


class AuditarTest(Base):
    def test_resumo_consolida_pior_status_e_impacto(self):
        v = S.aplicar_lote("psi", {"paginas": [
            pagina("https://p/1", achados=[achado("perf-page-weight", peso=8), achado("dom", "warn", 3)]),
            pagina("https://p/2", achados=[achado("perf-page-weight", peso=8), achado("dom", "fail", 3)]),
            pagina("https://p/3", nota=70)], "erros": [], "meta": {}}, "2026-10-15", self.d)
        r = S.consolidar(v, "2026-10-15", META)
        self.assertEqual((r["nota"], r["paginas"]), (83, 3))
        g = {x["regra"]: x for x in r["regras"]}
        self.assertEqual((g["dom"]["status"], g["dom"]["paginas"], g["dom"]["impacto"]), ("fail", ["https://p/2"], 3))
        self.assertEqual((g["perf-page-weight"]["impacto"], g["perf-page-weight"]["nome"]), (16, "Page Weight"))
        self.assertEqual(r["regras"][0]["regra"], "perf-page-weight")  # maior impacto primeiro
        self.assertEqual(r["categorias"], {"perf": 80, "core": 100})

    def test_regra_so_aviso_nunca_vira_fail_nem_abre_issue(self):
        v = S.aplicar_lote("psi", {"paginas": [
            pagina("https://p/1", achados=[achado("content-text-html-ratio", peso=3), achado("perf-page-weight", peso=8)])],
            "erros": [], "meta": {}}, "2026-10-15", self.d)
        self.assertEqual(v["paginas"]["https://p/1"]["regras"], {"content-text-html-ratio": "warn", "perf-page-weight": "fail"})
        v["paginas"]["https://p/2"] = {"dia": "2026-10-14", "nota": 80, "cats": {}, "regras": {"content-text-html-ratio": "fail"}}  # gravado antes
        r = S.consolidar(v, "2026-10-15", META)
        g = {x["regra"]: x for x in r["regras"]}
        self.assertEqual((g["content-text-html-ratio"]["status"], g["content-text-html-ratio"]["paginas"]),
                         ("warn", ["https://p/1", "https://p/2"]))  # segue no relatório como alerta
        self.assertEqual(g["perf-page-weight"]["status"], "fail")
        fatos = S.fatos("psi", r)
        self.assertIn(("alertas", "content-text-html-ratio", 2), [(x["metrica"], x["dim"], x["valor"]) for x in fatos])
        self.assertFalse(any(x["dim"].startswith("content-text-html-ratio|") for x in fatos))  # sem falha_pagina por página

    def test_erro_http_vira_achado_e_timeout_nao(self):
        bruto = {"paginas": [], "meta": {}, "erros": [
            {"url": "https://p/404", "erro": "The page returned HTTP 404.", "http": "404"},
            {"url": "https://p/lenta", "erro": "timeout", "http": None}]}
        r = S.consolidar(S.aplicar_lote("psi", bruto, "2026-10-15", self.d), "2026-10-15", S._regras([], self.d, {}))
        self.assertEqual([(g["regra"], g["paginas"], g["nome"]) for g in r["regras"]],
                         [(S.ERRO_REGRA, ["https://p/404"], S.META_ERRO["name"])])

    def test_pagina_antiga_sai_do_resumo_e_a_nova_substitui(self):
        S.aplicar_lote("psi", {"paginas": [pagina("https://p/velha", achados=[achado("a")]),
                                           pagina("https://p/1", achados=[achado("a")])], "erros": [], "meta": {}}, "2026-10-01", self.d)
        v = S.aplicar_lote("psi", {"paginas": [pagina("https://p/1")], "erros": [], "meta": {}}, "2026-10-20", self.d)
        self.assertEqual(list(v["paginas"]), ["https://p/1"])  # a velha passou de 14 dias
        self.assertEqual(v["paginas"]["https://p/1"]["regras"], {})  # consertada

    def test_auditar_grava_resumo_status_fatos_e_cursor(self):
        velho = Path(self.d) / "seomator/tmpvelho"
        velho.mkdir(parents=True)
        os.utime(velho, (0, 0))
        novo = Path(self.d) / "seomator/tmpnovo"  # pode ser de outra auditoria rodando
        novo.mkdir()
        rod, b = Rodar(achados={"https://site-e.exemplo.com/p1": [achado("perf-page-weight", peso=8)]}), Sitemaps(self.mapa("site-e.exemplo.com", 3))
        r = S.auditar(PSI, "2026-10-15", self.d, rod, baixar=b)
        self.assertFalse(velho.exists())
        self.assertTrue(novo.exists())
        novo.rmdir()
        self.assertEqual((r["paginas"], r["total"], r["nota"]), (3, 3, 90))
        self.assertEqual(rod.lotes, [["https://site-e.exemplo.com/p0", "https://site-e.exemplo.com/p1", "https://site-e.exemplo.com/p2"]])
        cmd, kw = rod.cmds[0]
        self.assertIn("--max-old-space-size=3072", cmd)
        self.assertEqual(kw["env"]["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"], "1")
        self.assertFalse(Path(kw["env"]["SEOMATOR_HOME"]).exists())  # banco do audit morre com o lote
        arq = Path(self.d) / "seomator/psi/2026-10-15.json"
        self.assertEqual((arq.stat().st_mode & 0o777, arq.parent.stat().st_mode & 0o777), (0o644, 0o755))
        self.assertEqual(S.status("psi", self.d)["falha"], None)
        self.assertEqual(S.status("psi", self.d)["total"], 3)
        self.assertEqual(json.loads((Path(self.d) / "seomator/psi/cursor.json").read_text())["off"], {"https://site-e.exemplo.com/sitemap.xml": 60})
        self.assertEqual(armazem.ultimo("psi", "seomator", "nota", self.d), ("2026-10-15", 90.0))
        dims = lambda m: dict(armazem.dims("psi", "seomator", m, "2026-10-15", "2026-10-15", self.d))
        self.assertEqual(dims("falhas"), {"perf-page-weight": 1})
        self.assertEqual(dims("falha_pagina"), {"perf-page-weight|/p1": 1})
        self.assertEqual(list((Path(self.d) / "seomator").glob("tmp*")), [])

    def test_node_morto_levanta_com_o_motivo_e_refaz_em_metades(self):
        rod = Rodar(quebra=["site-e"])
        with self.assertRaisesRegex(RuntimeError, r"seomator saiu -6: FATAL ERROR: JavaScript heap out of memory"):
            S.auditar(PSI, "2026-10-15", self.d, rod, baixar=Sitemaps(self.mapa("site-e.exemplo.com", 4)))
        self.assertEqual([len(x) for x in rod.lotes], [4, 2])  # inteiro, depois a 1ª metade; a falha levanta

    def test_lote_que_falha_so_inteiro_passa_em_metades(self):
        chamadas = []

        def rodar(cmd, **kw):
            urls = json.loads(Path(cmd[-2]).read_text())["urls"]
            chamadas.append(len(urls))
            if len(urls) > 2:
                return SimpleNamespace(returncode=-6, stderr="boom")
            Path(cmd[-1]).write_text(json.dumps({"paginas": [pagina(u) for u in urls], "erros": [], "meta": {}}))
            return SimpleNamespace(returncode=0, stderr="")
        r = S.auditar(PSI, "2026-10-15", self.d, rodar, baixar=Sitemaps(self.mapa("site-e.exemplo.com", 4)))
        self.assertEqual((chamadas, r["paginas"]), ([4, 2, 2], 4))

    def test_timeout_do_lote(self):
        def rodar(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        with self.assertRaisesRegex(RuntimeError, "lote passou de 60 min"):
            S.auditar(PSI, "2026-10-15", self.d, rodar, baixar=Sitemaps(self.mapa("site-e.exemplo.com", 4)))

    def test_fora_da_unidade_o_comando_ganha_o_teto(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEOMATOR_LIMITADO", None)
            cmd = S._limitado(["node", "x"])
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("CPUQuota=200%", cmd)
        self.assertIn("MemoryMax=4G", cmd)
        self.assertIn("nice", cmd)
        self.assertEqual(cmd[-2:], ["node", "x"])
        self.assertEqual(S._limitado(["node", "x"]), ["node", "x"])  # na unidade (SEOMATOR_LIMITADO) fica como está

    def test_fatos_sem_nota(self):
        f = S.fatos("psi", {"dia": "2026-10-15", "nota": None, "paginas": None, "categorias": {}, "regras": []})
        self.assertEqual(f, [])


class NoiteTest(Base):
    def setUp(self):
        super().setUp()
        self.b = Sitemaps({**self.mapa("site-e.exemplo.com", 100), **self.mapa("fsite06-com-br.example", 100)})

    def noite(self, rodar=None, hoje=None, **kw):
        return S.noite(hoje=hoje or _brt("2026-10-15", 1), base_dados=self.d, rodar=rodar or Rodar(), baixar=self.b,
                       carga_alta=lambda: False, dormir=lambda s: None, **kw)

    def test_rodizio_alterna_sites_ate_acabar_a_janela(self):
        rod = Rodar()
        for sid in ("psi", "fsite06"):  # um lote de 60 páginas a 20 s cada = 20 min
            S._gravar_json(Path(self.d) / f"seomator/{sid}/status.json", {"s_por_pagina": 20})
        relogio = iter([_brt("2026-10-15", h, m) for h, m in [(1, 0), (1, 20), (3, 0), (5, 45)]])
        S.noite(base_dados=self.d, rodar=rod, baixar=self.b, carga_alta=lambda: False, dormir=lambda s: None,
                hoje=lambda: next(relogio), max_lotes=50)
        quem = ["site-e" if "site-e" in l[0] else "fsite06" for l in rod.lotes]
        self.assertEqual(quem, ["site-e", "fsite06", "site-e"])  # às 05:45 o lote de 20 min não cabe até as 06h
        self.assertEqual(len(rod.lotes[0]), 60)
        self.assertEqual(rod.lotes[2][0], "https://site-e.exemplo.com/p60")  # o cursor seguiu adiante

    def test_fora_da_janela_nao_comeca_e_agora_ignora(self):
        rod = Rodar()
        self.noite(rod, _brt("2026-10-15", 7))
        self.noite(rod, _brt("2026-10-15", 0, 30))
        self.assertEqual(rod.lotes, [])
        self.noite(rod, _brt("2026-10-15", 7), agora=True, passada=True)
        self.assertEqual(len(rod.lotes), 2)

    def test_nao_comeca_lote_que_nao_cabe_ate_as_6h(self):
        S._gravar_json(Path(self.d) / "seomator/psi/status.json", {"s_por_pagina": 10, "ultimo_lote": "2026-10-01"})  # 60 × 10 s = 10 min
        rod = Rodar()
        self.noite(rod, _brt("2026-10-15", 5, 51), passada=True, agora=False)
        self.assertEqual(rod.lotes, [])  # fsite06 nunca rodou (estimativa 60 × 20 s = 20 min): não cabe
        rod = Rodar()
        self.noite(rod, _brt("2026-10-15", 5, 39), passada=True)
        self.assertEqual(len(rod.lotes), 2)

    def test_passada_faz_uma_volta_e_site_com_falha_nao_derruba_os_outros(self):
        rod = Rodar(quebra=["site-e"])
        saida, erros = self.noite(rod, passada=True)
        self.assertEqual(list(saida), ["fsite06"])
        self.assertEqual(len(erros), 1)
        self.assertRegex(erros[0], r"^psi: seomator saiu -6: FATAL ERROR")
        st = S.status("psi", self.d)
        self.assertRegex(st["falha"]["motivo"], "heap out of memory")
        f = [json.loads(x) for x in (Path(self.d) / "rotinas/falhas.jsonl").read_text().splitlines()]
        self.assertEqual([(x["nome"], x["codigo"]) for x in f], [("seomator", 1)])
        self.assertTrue(f[0]["em"].endswith("+00:00"))

    def test_falha_nao_repete_na_mesma_noite_e_sucesso_limpa_a_falha(self):
        rod = Rodar(quebra=["site-e"])
        self.noite(rod, agora=True, max_lotes=6)  # sem passada: gira até todos "mortos" ou o teto
        self.assertEqual(sum("site-e" in l[0] for l in rod.lotes), 2)  # inteiro + metade, uma vez só
        self.assertGreater(sum("fsite06" in l[0] for l in rod.lotes), 1)  # o outro site segue sem ele
        rod = Rodar()
        self.noite(rod, passada=True)
        self.assertIsNone(S.status("psi", self.d)["falha"])

    def test_hub_ocupada_espera_e_desiste(self):
        esperas, rod = [], Rodar()
        S.noite(hoje=_brt("2026-10-15", 1), base_dados=self.d, rodar=rod, baixar=self.b, carga_alta=lambda: True,
                dormir=esperas.append, passada=True)
        self.assertEqual(rod.lotes, [])
        self.assertEqual(sum(esperas), S.ESPERA_MAX_S)
        cargas = iter([True, True, False])
        self.assertTrue(S._esperar_folga(lambda: next(cargas), lambda s: None))

    def test_passada_inicial_so_marca_sem_erro_e_so_roda_uma_vez(self):
        kw = dict(base_dados=self.d, hoje=_brt("2026-10-15", 14), baixar=self.b, carga_alta=lambda: False, dormir=lambda s: None)
        saida, erros = S.passada_inicial(rodar=Rodar(quebra=["fsite06"]), **kw)
        self.assertEqual(len(erros), 1)
        self.assertNotIn("passada_v2", json.loads((Path(self.d) / "seomator/estado.json").read_text()) if (Path(self.d) / "seomator/estado.json").exists() else {})
        rod = Rodar()
        saida, erros = S.passada_inicial(rodar=rod, **kw)
        self.assertEqual((sorted(saida), erros), (["fsite06", "psi"], []))
        self.assertIn("passada_v2", json.loads((Path(self.d) / "seomator/estado.json").read_text()))
        rod = Rodar()
        self.assertIsNone(S.passada_inicial(rodar=rod, **kw))
        self.assertEqual(rod.lotes, [])

    def test_resumo_zap_mostra_falha_e_site_sem_auditoria(self):
        self.noite(Rodar(achados={"https://fsite06-com-br.example/p1": [achado("perf-page-weight", peso=8)]}), passada=True)
        S.registrar_falha("psi", "seomator saiu -6: boom", self.d)
        t = S.resumo_zap(self.d, hoje=dt.date(2026, 10, 15), lista=[PSI, FSITE06, {"id": "novo", "nome": "Novo", "dominio": "n-com.example"}])
        self.assertIn("Cliente G — nota 90, 60 pág. auditadas — falhou: seomator saiu -6: boom", t)
        self.assertIn("fsite06 — nota 90", t)
        self.assertIn("• Page Weight (1 pág.)", t)
        self.assertIn("Novo — ainda sem auditoria", t)
        self.assertIn("15/10", t.splitlines()[0])
        self.assertIn("/seo", t.splitlines()[-1])


class IssuesTest(Base):
    def _res(self, regras, dia="2026-10-15"):
        return {"dia": dia, "regras": [{"regra": r, "nome": r.upper(), "cat": "c", "status": st, "peso": 1,
                                        "paginas": [f"https://x/{i}" for i in range(n)], "msg": "m", "fix": "Fix it",
                                        "impacto": n if st == "fail" else 0} for r, st, n in regras]}

    def _abrir(self, site, res):
        chamadas = []

        def criar(projeto, titulo, corpo, est, labels, prio):
            chamadas.append((projeto, titulo, corpo, est, labels, prio))
            return f"KRN-{len(chamadas)} http://x"
        with mock.patch.object(S.linear, "projeto_do_repo", return_value="Proj"):
            novas = ABRIR(site, res, self.d, criar)
        return novas, chamadas

    def test_issue_so_fail_com_corpo_e_labels(self):
        site = {**FSITE06, "repo": {"github": "acme/fsite06"}}
        novas, c = self._abrir(site, self._res([("a", "fail", 40), ("b", "warn", 5)]))
        self.assertEqual(novas, ["KRN-1"])
        projeto, titulo, corpo, est, labels, prio = c[0]
        self.assertEqual((projeto, titulo, est, labels, prio),
                         ("Proj", "SEO fsite06: A (40 página(s))", "Backlog", ["esteira", "seo-fix", "merge"], 3))
        self.assertIn("e mais 10", corpo)
        self.assertEqual(corpo.count("- https://x/"), 30)
        self.assertIn("> Fix it", corpo)
        self.assertIn("```\nm\n```", corpo)
        self.assertIn("seomator audit <url> --format llm", corpo)
        self.assertIn("KRN-809", corpo)

    def test_regra_com_excecao_nao_abre_issue(self):
        site = {**FSITE06, "repo": {"github": "dono-x/site-f"}, "padrao": {"excecoes": {"a": "motivo"}}}
        novas, c = self._abrir(site, self._res([("a", "fail", 40), ("b", "fail", 5)]))
        self.assertEqual(novas, ["KRN-1"])
        self.assertIn("SEO fsite06: B (5 página(s))", c[0][1])

    def test_exceção_retirada_do_cadastro_volta_a_abrir_issue(self):
        reg = {"a": {"issue": "KRN-7", "dia": "2026-10-10", "excecao": "2026-10-14"}}
        S._gravar_json(S._dir(self.d) / "issues.json", {"fsite06": reg})
        site = {**FSITE06, "repo": {"github": "dono-x/site-f"}}  # sem padrao.excecoes: a regra voltou a valer
        novas, c = self._abrir(site, self._res([("a", "fail", 40)]))
        self.assertEqual(novas, ["KRN-1"])
        nova = S._ler_json(S._dir(self.d) / "issues.json", {})["fsite06"]["a"]
        self.assertEqual((nova["issue"], nova["anteriores"]), ("KRN-1", [{"issue": "KRN-7", "dia": "2026-10-10"}]))

    def test_corpo_cerca_a_mensagem_do_site(self):
        g = {"regra": "a", "nome": "A", "cat": "c", "paginas": [], "msg": "x ``` <b>`y`</b>", "fix": "um\ndois"}
        corpo = S._corpo(g, {"dia": "2026-10-15"})
        self.assertIn("```\nx ''' <b>'y'</b>\n```", corpo)  # crase do site não fecha a cerca
        self.assertIn("> um\n> dois", corpo)
        self.assertNotIn("None", S._corpo({**g, "msg": None, "fix": ""}, {"dia": "2026-10-15"}))

    def test_toda_regra_grave_nova_abre_uma_issue_por_regra_sem_teto_semanal(self):
        regras = [(f"r{i}", "fail", 20 - i) for i in range(6)]
        novas, c = self._abrir(FSITE06, self._res(regras))
        self.assertEqual(len(novas), 6)  # antes eram 3 por semana
        self.assertEqual([x[1].split(": ")[1][:2] for x in c], ["R0", "R1", "R2", "R3", "R4", "R5"])  # por impacto

    def test_teto_de_10_por_dia_somando_os_sites(self):
        self.assertEqual(len(self._abrir(FSITE06, self._res([(f"a{i}", "fail", 5) for i in range(7)]))[0]), 7)
        self.assertEqual(len(self._abrir(PSI, self._res([(f"b{i}", "fail", 5) for i in range(7)]))[0]), 3)  # 7 + 3 = 10
        self.assertEqual(self._abrir(PSI, self._res([("c", "fail", 5)]))[0], [])
        self.assertEqual(len(self._abrir(PSI, self._res([("c", "fail", 5)], "2026-10-16"))[0]), 1)  # outro dia

    def test_nao_duplica_enquanto_nao_verificada(self):
        res = self._res([("a", "fail", 2)])
        self.assertEqual(len(self._abrir(FSITE06, res)[1]), 1)
        self.assertEqual(self._abrir(FSITE06, self._res([("a", "fail", 2)], "2026-10-16"))[1], [])
        reg = S._ler_json(S._dir(self.d) / "issues.json", {})
        reg["fsite06"]["a"]["verificada"] = "2026-10-17"  # a regra sumiu e depois voltou: issue nova
        S._gravar_json(S._dir(self.d) / "issues.json", reg)
        novas, _ = self._abrir(FSITE06, self._res([("a", "fail", 2)], "2026-10-20"))
        self.assertEqual(len(novas), 1)
        self.assertEqual(len(S._ler_json(S._dir(self.d) / "issues.json", {})["fsite06"]["a"]["anteriores"]), 1)

    def test_evento_exemplo_e_site_sem_issue_nao_abre(self):
        self.assertEqual(self._abrir({**FSITE06, "sem_issue": True}, self._res([("a", "fail", 2)])), ([], []))

    def test_clientee_sem_merge(self):
        site = {**FSITE06, "repo": {"github": "terceiro/clientee-moraes-site"}}
        self.assertEqual(self._abrir(site, self._res([("a", "fail", 2)]))[1][0][4], ["esteira", "seo-fix"])

    def test_falha_no_criar_nao_derruba_a_noite_nem_grava(self):
        def quebra(*a):
            raise RuntimeError("Linear fora")
        b = Sitemaps(self.mapa("site-e.exemplo.com", 2) | self.mapa("fsite06-com-br.example", 2))
        with mock.patch.object(S.linear, "projeto_do_repo", return_value="P"), \
                mock.patch.object(S, "abrir_issues", lambda s, r, bd: ABRIR(s, r, bd, quebra)):
            saida, erros = S.noite(hoje=_brt("2026-10-15", 1), base_dados=self.d, passada=True, baixar=b,
                                   rodar=Rodar(achados={"https://site-e.exemplo.com/p0": [achado("a")]}),
                                   carga_alta=lambda: False, dormir=lambda s: None)
        self.assertEqual((sorted(saida), erros), (["fsite06", "psi"], []))
        self.assertFalse((Path(self.d) / "seomator/issues.json").exists())


class ReconferirTest(Base):
    def setUp(self):
        super().setUp()
        S._gravar_json(S._dir(self.d) / "issues.json", {"psi": {
            "a": {"issue": "KRN-1", "dia": "2026-10-10", "paginas": ["https://site-e.exemplo.com/p0"]},
            "b": {"issue": "KRN-2", "dia": "2026-10-10", "paginas": ["https://site-e.exemplo.com/p1"]},
            "c": {"issue": "KRN-3", "dia": "2026-10-10", "paginas": ["https://site-e.exemplo.com/p2"]},
            "d": {"issue": "KRN-4", "dia": "2026-10-10", "paginas": ["https://site-e.exemplo.com/p3"]}}})
        self.estados = {"KRN-1": "completed", "KRN-2": "completed", "KRN-3": "started", "KRN-4": "canceled"}

    def rodar(self, rod, reabrir):
        return S.reconferir(PSI, "2026-10-15", self.d, rod, self.estados.get, reabrir)

    def test_regra_que_sumiu_verifica_e_a_que_ficou_reabre(self):
        reaberta = []
        rod = Rodar(achados={"https://site-e.exemplo.com/p1": [achado("b")]})  # p0 consertada, p1 ainda com a regra b
        r = self.rodar(rod, lambda ident, texto: reaberta.append((ident, texto)))
        self.assertEqual(r, {"KRN-1": "verificada", "KRN-2": "reaberta"})
        self.assertEqual(sorted(rod.lotes[0]), ["https://site-e.exemplo.com/p0", "https://site-e.exemplo.com/p1"])  # só as páginas das concluídas
        self.assertEqual([i for i, _ in reaberta], ["KRN-2"])
        self.assertIn("regra `b` e ela continua em 1 página(s)", reaberta[0][1])
        reg = S._ler_json(S._dir(self.d) / "issues.json", {})["psi"]
        self.assertEqual(reg["a"]["verificada"], "2026-10-15")
        self.assertEqual(reg["b"]["reaberturas"], 1)
        self.assertEqual(reg["d"]["cancelada"], "2026-10-15")
        self.assertNotIn("verificada", reg["c"])  # em andamento: não mexe

    def test_reabre_no_maximo_duas_vezes(self):
        rod, reaberta = Rodar(achados={"https://site-e.exemplo.com/p1": [achado("b")]}), []
        for _ in range(3):
            self.rodar(rod, lambda ident, texto: reaberta.append(ident))
        self.assertEqual(reaberta, ["KRN-2", "KRN-2"])

    def test_consulta_do_estado_falha_nao_verifica(self):
        def estado(i):
            raise RuntimeError("Linear fora")
        self.assertEqual(S.reconferir(PSI, "2026-10-15", self.d, Rodar(), estado, lambda *a: 1 / 0), {})

    def test_sem_medicao_valida_nao_verifica(self):
        rod = Rodar(achados={})
        with mock.patch.object(S, "_lote_resiliente", return_value={"paginas": [], "erros": [{"url": "https://site-e.exemplo.com/p0", "erro": "timeout"}], "meta": {}}):
            r = self.rodar(rod, lambda *a: 1 / 0)
        self.assertEqual(r, {})
        reg = S._ler_json(S._dir(self.d) / "issues.json", {})["psi"]
        self.assertNotIn("verificada", reg["a"])

    def test_verificadas_nao_ocupam_o_limite(self):
        reg = {f"r{i}": {"issue": f"KRN-9{i}", "dia": "2026-10-01", "verificada": "2026-10-02"} for i in range(5)}
        reg["nova"] = {"issue": "KRN-1", "dia": "2026-10-10", "paginas": ["https://site-e.exemplo.com/p0"]}
        S._gravar_json(S._dir(self.d) / "issues.json", {"psi": reg})
        r = S.reconferir(PSI, "2026-10-15", self.d, Rodar(), self.estados.get, lambda *a: 1 / 0, max_issues=2)
        self.assertEqual(r, {"KRN-1": "verificada"})

    def test_reabre_pelo_linear_no_backlog(self):
        with mock.patch.object(S.linear, "mover_por_nome") as mv:
            Rodar_ = Rodar(achados={"https://site-e.exemplo.com/p1": [achado("b")]})
            S.reconferir(PSI, "2026-10-15", self.d, Rodar_, self.estados.get)
        mv.assert_called_once()
        self.assertEqual(mv.call_args.args[:2], ("KRN-2", "Backlog"))

    def test_regra_so_aviso_com_registro_antigo_nao_reabre_por_dado_gravado_antes(self):  #
        S._gravar_json(S._dir(self.d) / "issues.json", {"psi": {"content-text-html-ratio": {"issue": "KRN-7", "dia": "2026-10-10"}}})  # sem lista de páginas
        self.estados["KRN-7"] = "completed"
        S._gravar_json(S._dir(self.d) / "psi" / "varredura.json", {"paginas": {
            "https://site-e.exemplo.com/antiga": {"dia": "2026-10-12", "nota": 80, "cats": {}, "regras": {"content-text-html-ratio": "fail"}}},
            "info": {}})
        reaberta = []
        r = S.reconferir(PSI, "2026-10-15", self.d, Rodar(achados={}), self.estados.get, lambda ident, texto: reaberta.append(ident))
        self.assertEqual(r, {"KRN-7": "verificada"})
        self.assertEqual(reaberta, [])

    def test_regra_com_excecao_concluida_nao_reabre_nem_mede(self):
        site = {**PSI, "padrao": {"excecoes": {"b": "beacon da Cloudflare"}}}
        rod, reaberta = Rodar(achados={"https://site-e.exemplo.com/p1": [achado("b")]}), []
        r = S.reconferir(site, "2026-10-15", self.d, rod, self.estados.get, lambda ident, texto: reaberta.append(ident))
        self.assertEqual(r, {"KRN-1": "verificada", "KRN-2": "excecao"})
        self.assertEqual(reaberta, [])
        self.assertEqual(rod.lotes[0], ["https://site-e.exemplo.com/p0"])  # a regra excepcionada não entra na medição
        reg = S._ler_json(S._dir(self.d) / "issues.json", {})["psi"]
        self.assertEqual(reg["b"]["excecao"], "2026-10-15")
        self.assertNotIn("reaberturas", reg["b"])


if __name__ == "__main__":
    unittest.main()
