"""Static checks on the build output. Run: python3 -m unittest discover -s tests (from site/)."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE.parent / "tests"))
import neutro_lib  # noqa: E402  (shared term list: tests/neutro_termos.txt, also used by the repo-wide gate)


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "dist"
        subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(cls.out)], check=True, capture_output=True)
        cls.data = json.loads((SITE / "content.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, rel):
        return (self.out / rel).read_text(encoding="utf-8")

    def test_files_exist(self):
        for rel in ["index.html", "pt/index.html", "llms.txt", "install.md", "install.pt.md", "robots.txt",
                    "sitemap.xml", "favicon.svg", "assets/style.css", "assets/guide.css",
                    "guides/comparison-pages-ai-citation/index.html", "guides/comparison-pages-ai-citation.md",
                    "pt/guias/paginas-de-comparacao-citacao-ia/index.html", "pt/guias/paginas-de-comparacao-citacao-ia.md", "assets/app.js", "assets/boot.js", "assets/hero-gl.js",
                    "assets/fonts/inter-latin-wght-normal.woff2",
                    "assets/fonts/instrument-serif-latin-400-italic.woff2"]:
            self.assertTrue((self.out / rel).is_file(), rel)

    def test_deterministic(self):
        other = Path(self.tmp.name) / "dist2"
        subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(other)], check=True, capture_output=True)
        for rel in ["index.html", "pt/index.html", "install.md", "install.pt.md", "llms.txt", "sitemap.xml",
                    "guides/comparison-pages-ai-citation/index.html", "pt/guias/paginas-de-comparacao-citacao-ia/index.html",
                    "guides/comparison-pages-ai-citation.md", "pt/guias/paginas-de-comparacao-citacao-ia.md",
                    "guias/x/index.html", "pt/guias/x/index.html"]:
            self.assertEqual(self.read(rel), (other / rel).read_text(encoding="utf-8"), rel)

    def test_lang_and_hreflang(self):
        en, pt = self.read("index.html"), self.read("pt/index.html")
        self.assertIn('<html lang="en">', en)
        self.assertIn('<html lang="pt-BR">', pt)
        for page in (en, pt):
            self.assertIn('hreflang="en" href="https://krohnos.com/"', page)
            self.assertIn('hreflang="pt-BR" href="https://krohnos.com/pt/"', page)
            self.assertIn('hreflang="x-default"', page)
            self.assertIn('property="og:title"', page)

    def test_install_commands_come_from_the_repo_guide(self):
        commands = [s["cmd"] for s in self.data["install_steps"]]
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0], "git clone https://github.com/BRK13/krohnos.git krohnos && cd krohnos")
        self.assertEqual(commands[2], "docker compose run --rm --no-deps --entrypoint krohnos painel init")
        self.assertEqual(commands[4], "curl -fsS http://127.0.0.1:8787/saude")
        guide = (SITE.parent / "docs" / "INSTALAR.md").read_text(encoding="utf-8")
        for command in commands[1:]:
            self.assertIn(command, guide)
        for rel in ["index.html", "pt/index.html", "install.md", "install.pt.md"]:
            text = self.read(rel)
            for command in commands:
                self.assertIn(command, text)
        self.assertIn('data-copy="', self.read("index.html"))
        self.assertIn("Install", self.read("index.html"))
        self.assertIn("Instalar", self.read("pt/index.html"))

    def test_site_and_readme_use_the_krohnos_name_only(self):
        for path in [SITE / "build.py", SITE / "content.json", SITE.parent / "README.md", SITE.parent / "README.pt-BR.md"]:
            self.assertNotIn("kro" + "nos ", path.read_text(encoding="utf-8").lower(), path.name)
        for rel in ["index.html", "pt/index.html", "install.md", "install.pt.md", "llms.txt"]:
            self.assertNotIn("kro" + "nos ", self.read(rel).lower(), rel)

    def test_all_code_blocks_same_in_html_and_md(self):
        import html as h
        for lang, page, md in (("en", "index.html", "install.md"), ("pt", "pt/index.html", "install.pt.md")):
            htm, mdt = h.unescape(self.read(page)), self.read(md)
            for sec in self.data[lang]["sections"]:
                for b in sec["blocks"]:
                    if b["t"] == "code":
                        self.assertIn(b["x"], htm)
                        self.assertIn(b["x"], mdt)

    def test_page_markdown_equals_install_md(self):
        for page, md in (("index.html", "install.md"), ("pt/index.html", "install.pt.md")):
            m = re.search(r'<script type="text/plain" id="md-page">(.*?)</script>', self.read(page), re.S)
            self.assertEqual(m.group(1), self.read(md))
            self.assertNotIn("</script", self.read(md))

    def test_production_proof_and_legal_footer(self):
        for rel in ["index.html", "pt/index.html"]:
            text = self.read(rel)
            self.assertNotIn("CNPJ", text)
            self.assertNotIn("mailto:", text)
            self.assertIn("abra uma issue no GitHub", text)
            self.assertIn("AGPL-3.0", text)
            self.assertNotIn("Status:", text)
            self.assertIn("EVIDENCE.md", text)
            self.assertIn("EVIDENCE.md", text)

    def test_institutional_homepage(self):
        for rel in ["index.html", "pt/index.html"]:
            text = self.read(rel)
            for term in ["Atenção", "Status:", "/comecar", "404"]:
                self.assertNotIn(term, text)
            self.assertLessEqual(text.count("<pre>"), 12)  # hero + install guide + one copyable block per guide section
        for lang in ("pt", "en"):
            inside = next(s for s in self.data[lang]["sections"] if s["id"] == "inside")
            self.assertTrue(3 <= len(inside["blocks"]) <= 6)

    def test_llms_txt(self):
        t = self.read("llms.txt")
        self.assertTrue(t.startswith("# Krohnos\n\n> "))
        for u in ["https://krohnos.com/install.md", "https://krohnos.com/install.pt.md",
                  "https://github.com/BRK13/krohnos", "https://github.com/BRK13/krohnos/releases",
                  "https://github.com/BRK13/krohnos/blob/main/LICENSE",
                  "https://github.com/BRK13/krohnos/blob/main/docs/security.md",
                  "https://github.com/BRK13/krohnos/blob/main/ROADMAP.md"]:
            self.assertIn(u, t)

    def test_nav_links(self):
        t = self.read("index.html")
        for u in ['href="#orquestrador"', 'href="#seo"', 'href="#comparacao"',
                  'href="https://github.com/BRK13/krohnos/releases"', 'href="#hero-install"']:
            self.assertIn(u, t)

    def test_no_forbidden_terms(self):
        """Every built text file, content.json and nginx.conf: none of the terms in tests/neutro_termos.txt."""
        regras, permitidos = neutro_lib.carregar()
        files = [p for p in self.out.rglob("*") if p.is_file() and p.suffix in
                 (".html", ".md", ".txt", ".xml", ".css", ".js", ".svg")]
        files += [SITE / "content.json", SITE / "guide.json", SITE / "nginx.conf"]
        for p in files:
            hits = neutro_lib.achados(p.read_text(encoding="utf-8"), regras, permitidos)
            self.assertEqual(hits, [], "%s contains %s" % (p.name, hits))

    def test_no_external_assets(self):
        for rel in ["index.html", "pt/index.html"]:
            t = self.read(rel)
            for m in re.finditer(r'<(?:link|script|img|source|video|audio|iframe)\b[^>]*?\b(?:src|href)="([^"]+)"', t):
                url = m.group(1)
                tag = m.group(0)
                if tag.startswith("<link") and ('rel="canonical"' in tag or 'rel="alternate"' in tag):
                    continue
                self.assertFalse(url.startswith(("http://", "https://", "//")), url)
        css = self.read("assets/style.css")
        self.assertNotRegex(css, r"url\(\s*['\"]?https?:")
        self.assertNotRegex(css, r"@import")
        self.assertNotIn("<style", self.read("index.html"))
        self.assertNotRegex(self.read("index.html"), r'\sstyle="')

    def test_real_brand_and_no_competing_animation(self):
        text = self.read("index.html")
        self.assertIn('/assets/krohnos-mark.svg', text)
        self.assertIn('<canvas id="hero-gl" aria-hidden="true">', text)
        self.assertIn('/assets/hero-gl.js', text)
        css = self.read("assets/style.css")
        self.assertRegex(css, r"#hero-gl\{[^}]*position:fixed;[^}]*inset:0")
        self.assertIn("100svh", css)
        self.assertNotIn("unsafe-eval", (SITE / "nginx.conf").read_text())
        self.assertNotIn("unsafe-inline", (SITE / "nginx.conf").read_text())

    def test_guide_pages(self):
        """The comparison-pages guide: both languages, hreflang pair, JSON-LD, sitemap, llms.txt, home link, no external asset."""
        urls = {"en": "https://krohnos.com/guides/comparison-pages-ai-citation/",
                "pt": "https://krohnos.com/pt/guias/paginas-de-comparacao-citacao-ia/"}
        pages = {"en": self.read("guides/comparison-pages-ai-citation/index.html"),
                 "pt": self.read("pt/guias/paginas-de-comparacao-citacao-ia/index.html")}
        for lang, page in pages.items():
            self.assertIn('<html lang="%s">' % ("en" if lang == "en" else "pt-BR"), page)
            self.assertIn('<link rel="canonical" href="%s">' % urls[lang], page)
            self.assertIn('hreflang="en" href="%s"' % urls["en"], page)
            self.assertIn('hreflang="pt-BR" href="%s"' % urls["pt"], page)
            self.assertEqual(len(re.findall(r"<h1[ >]", page)), 1)
            self.assertIn('"@type": "TechArticle"', page)
            self.assertNotIn("<style", page)
            self.assertNotRegex(page, r'\sstyle="')
            for m in re.finditer(r'<(?:link|script|img)\b[^>]*?\b(?:src|href)="([^"]+)"', page):
                if 'rel="canonical"' in m.group(0) or 'rel="alternate"' in m.group(0):
                    continue
                self.assertFalse(m.group(1).startswith(("http://", "https://", "//")), m.group(1))
            ids = [s["id"] for s in json.loads((SITE / "guide.json").read_text(encoding="utf-8"))[lang]["sections"]]
            self.assertEqual(ids, ["what", "terms", "templates", "gate", "waves", "measure", "command"])
            for sid in ids:
                self.assertIn('id="g-%s"' % sid, page)
        sm, llms = self.read("sitemap.xml"), self.read("llms.txt")
        for u in urls.values():
            self.assertIn("<loc>%s</loc>" % u, sm)
            self.assertIn("(%s)" % u, llms)
        for lang, home in (("en", "index.html"), ("pt", "pt/index.html")):
            self.assertIn('href="/%s/"' % ("guides/comparison-pages-ai-citation" if lang == "en" else "pt/guias/paginas-de-comparacao-citacao-ia"), self.read(home))

    def test_guide_gate_and_references(self):
        """The guide names the gate rules and the Brazilian references, and keeps the command real."""
        md = self.read("guides/comparison-pages-ai-citation.md")
        for needle in ["CONAR", "article 32", "Consumer Defense Code", "9,279/1996", "not legal advice", "Jaccard",
                       "python3 -m hub.paginas_comparacao termos", "python3 -m hub.paginas_comparacao rascunhos",
                       "python3 -m hub.paginas_comparacao portao", "Wave 0"]:
            self.assertIn(needle, md)
        pt = self.read("pt/guias/paginas-de-comparacao-citacao-ia.md")
        for needle in ["CONAR", "art. 32", "9.279/1996", "não é aconselhamento jurídico", "Jaccard", "Onda 0"]:
            self.assertIn(needle, pt)
        # the command the guide shows exists and has the three subcommands
        src = (SITE.parent / "hub" / "paginas_comparacao.py").read_text(encoding="utf-8")
        for sub in ('add_parser("termos")', 'add_parser("rascunhos")', 'add_parser("portao")'):
            self.assertIn(sub, src)

    def test_robots_sitemap(self):
        self.assertIn("Sitemap: https://krohnos.com/sitemap.xml", self.read("robots.txt"))
        self.assertIn("https://krohnos.com/pt/", self.read("sitemap.xml"))

    def test_no_unsupported_claims(self):
        for rel in ["index.html", "install.md"]:
            t = self.read(rel)
            self.assertNotIn("grátis", t.lower())
            self.assertNotRegex(t, r"(?i)for free|free plan|free tier|free trial")
            self.assertNotRegex(t, r"(?i)sign.?up|log.?in required|systemd")

    def test_message_in_title_and_h1(self):
        """position both languages as self-hosted multi-agent orchestration."""
        import html as h
        cases = (("index.html", "multi-agent", "Multi-agent AI. Local control."),
                 ("pt/index.html", "multiagente", "IA multiagente. Controle local."))
        for rel, in_title, h1_text in cases:
            page = self.read(rel)
            title = h.unescape(re.search(r"<title>(.*?)</title>", page, re.S).group(1))
            self.assertIn(in_title, title, rel)
            self.assertLessEqual(len(title), 60, rel)
            self.assertIn('property="og:title" content="%s"' % h.escape(title), page, rel)
            h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S).group(1)
            h1 = re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", "", h1))).strip()
            self.assertEqual(h1, h1_text, rel)


    def test_explanation_sections_in_page_markdown_and_llms(self):
        ids = ["orquestrador", "seo", "comparacao", "precisa"]
        for lang, page, md in (("en", "index.html", "install.md"), ("pt", "pt/index.html", "install.pt.md")):
            have = [s["id"] for s in self.data[lang]["sections"]]
            self.assertEqual(have[:5], ["inside"] + ids)
            self.assertIn("proof", have)
            for sid in ids:
                self.assertIn('id="%s"' % sid, self.read(page))
                title = next(s["title"] for s in self.data[lang]["sections"] if s["id"] == sid)
                self.assertIn("## " + title, self.read(md))
        llms = self.read("llms.txt")
        for sid in ids:
            self.assertIn("https://krohnos.com/#" + sid, llms)

    def test_every_process_links_an_existing_file(self):
        repo = SITE.parent
        seen = 0
        for lang in ("en", "pt"):
            for sec in self.data[lang]["sections"]:
                for b in sec["blocks"]:
                    if b["t"] != "proc":
                        continue
                    self.assertGreaterEqual(len(b["flow"]), 2, b["title"])
                    self.assertTrue(b["code"], b["title"])
                    for path in b["code"]:
                        seen += 1
                        self.assertTrue((repo / path).is_file(), path)
                        self.assertIn("https://github.com/BRK13/krohnos/blob/main/" + path,
                                      self.read("index.html" if lang == "en" else "pt/index.html"))
        self.assertGreaterEqual(seen, 40)

    def test_orchestrator_steps_use_real_module_names(self):
        orq = next(s for s in self.data["pt"]["sections"] if s["id"] == "orquestrador")
        files = {p for b in orq["blocks"] if b["t"] == "proc" for p in b["code"]}
        for need in ["hub/agenda.py", "hub/linear.py", "hub/recibo.py", "hub/portao_done.py", "hub/vigia_deploy.py",
                     "hub/debugger.py", "hub/biblioteca_indice.py", "hub/automerge.py", "hub/revisar_codex.py"]:
            self.assertIn(need, files)
        self.assertGreaterEqual(sum(b["t"] == "proc" for b in orq["blocks"]), 12)

    def test_comparison_has_official_price_links_and_date(self):
        for lang, date in (("pt", "20/10/2026"), ("en", "2026-10-20")):
            sec = next(s for s in self.data[lang]["sections"] if s["id"] == "comparacao")
            text = json.dumps(sec, ensure_ascii=False)
            for url in ["https://www.semrush.com/prices/", "https://ahrefs.com/pricing",
                        "https://www.similarweb.com/corp/pricing/", "https://dataforseo.com/pricing",
                        "https://claude.com/pricing", "https://linear.app/pricing"]:
                self.assertIn(url, text)
            self.assertIn(date, text)
            for row in next(b for b in sec["blocks"] if b["t"] == "table")["rows"]:
                self.assertRegex(row[0], r"\]\(https://")
            self.assertNotRegex(text, r"(?i)melhor que|better than|cheaper than|mais barato que")


    # ---------- guide: X knowledge agent (guias/x) ----------
    GUIDE = {"en": "guias/x/index.html", "pt": "pt/guias/x/index.html"}
    GUIDE_INSTALL = ["pip install playwright",
                     "python3 -m hub.x_salvos coletar --config x_salvos.toml",
                     "python3 -m hub.x_salvos extrair --config x_salvos.toml",
                     "python3 -m hub.x_salvos coletar --jitter --config x_salvos.toml"]
    TOML_KEYS = ["cookies", "cookies_origem", "saida", "temas", "destino", "max_itens", "max_paginas",
                 "limite_taxa_s", "jitter_max_s", "chrome"]

    def guide_readme(self):
        import os
        for cand in (os.environ.get("X_SALVOS_README"), str(SITE.parent / "hub" / "x_salvos" / "README.md")):
            if cand and Path(cand).is_file():
                return Path(cand).read_text(encoding="utf-8")
        return None

    def test_guide_pages_exist_with_flowchart_and_alternative_text(self):
        import html as h
        for lang, rel in self.GUIDE.items():
            t = self.read(rel)
            self.assertIn('<html lang="%s">' % ("en" if lang == "en" else "pt-BR"), t)
            self.assertEqual(t.count("<svg"), 1, rel)
            self.assertIn('role="img"', t)
            self.assertRegex(t, r"<title id=\"fc-[^\"]+-t\">[^<]+</title><desc id=\"fc-[^\"]+-d\">[^<]+</desc>")
            self.assertIn('aria-labelledby="fc-', t)
            self.assertEqual(t.count('class="fc-node"'), 5)  # salvos, coleta, extracao, receita, uso
            self.assertEqual(len(re.findall(r'<line class="fc-arrow"', t)), 4)
            self.assertRegex(t, r'<figcaption><p class="fc-cap">[^<]+</p><ol>(<li>.*?</li>){5}</ol></figcaption>')
            self.assertNotRegex(t, r'\sstyle="')
            self.assertNotIn("<style", t)
            self.assertNotIn("hero-gl", t)
            self.assertIn('hreflang="en" href="https://krohnos.com/guias/x/"', t)
            self.assertIn('hreflang="pt-BR" href="https://krohnos.com/pt/guias/x/"', t)
            for step in self.data["guide_x"][lang]["sections"][1]["blocks"][0]["steps"]:
                self.assertIn(h.escape(step[0]), t)
        css = self.read("assets/style.css")
        for cls in (".fc-node", ".fc-arrow", ".fc-t"):
            self.assertIn(cls, css)
        self.assertIn("var(--surface)", css[css.index(".fc-node"):css.index(".fc-node") + 80])  # follows the light/dark variables

    def test_guide_install_commands_equal_the_readme(self):
        import html as h
        readme = self.guide_readme()
        for lang, rel in self.GUIDE.items():
            t = h.unescape(self.read(rel))
            for cmd in self.GUIDE_INSTALL:
                self.assertIn(cmd, t, rel)
                if readme is not None:
                    self.assertIn(cmd, readme, cmd)
            for key in self.TOML_KEYS:
                self.assertRegex(t, r"(?m)^%s = " % key, key)
                if readme is not None:
                    self.assertRegex(readme, r"(?m)^%s = " % key, key)
            for name in ("auth_token", "ct0", "twid", "chmod 600"):
                self.assertIn(name, t)
            self.assertIn("biblioteca", t)
            self.assertIn("issue", t)
            self.assertNotRegex(t, r"(?i)password:|senha:")

    def test_guide_has_six_guardrails_each_with_a_real_test_name(self):
        import html as h
        sec = next(s for s in self.data["guide_x"]["en"]["sections"] if s["id"] == "guardrails")
        rows = next(b for b in sec["blocks"] if b["t"] == "table")["rows"]
        self.assertEqual(len(rows), 6)
        names = re.compile(r"`([A-Za-z_.0-9]+)`")
        for lang, rel in self.GUIDE.items():
            t = h.unescape(self.read(rel))
            table = re.search(r"<table><caption>(?:Guardrails and tests|Guardrails e testes)</caption>.*?</table>", t, re.S).group(0)
            self.assertEqual(table.count("<tr>"), 7, rel)  # header + 6 guardrails
            for i in range(1, 7):
                self.assertIn(">%d. " % i, table)
            for row in self.data["guide_x"][lang]["sections"][5]["blocks"][1]["rows"]:
                self.assertRegex(row[2], r"tests/test_x_salvos_\w+\.py")
                self.assertTrue(names.findall(row[2]), row[0])
        # the test names the guide cites are, in the module's own README, the ones that prove the guardrails
        readme = self.guide_readme()
        if readme is not None:
            for row in rows:
                for name in names.findall(row[2]):
                    if name.startswith("test_x_salvos"):
                        continue
                    self.assertIn(name.split(".")[-1], readme, name)

    def test_guide_linked_from_home_nav_footer_sitemap_and_llms(self):
        for lang, home in (("en", "index.html"), ("pt", "pt/index.html")):
            t = self.read(home)
            path = "/guias/x/" if lang == "en" else "/pt/guias/x/"
            self.assertIn('<a class="pill" href="%s">' % path, t)
            self.assertIn('<a href="%s">' % path, t)
            self.assertIn('href="%s"' % ("/pt/guias/x/" if lang == "en" else "/guias/x/"), self.read(self.GUIDE[lang]))
        sm = self.read("sitemap.xml")
        self.assertIn("<loc>https://krohnos.com/guias/x/</loc>", sm)
        self.assertIn("<loc>https://krohnos.com/pt/guias/x/</loc>", sm)
        llms = self.read("llms.txt")
        self.assertIn("https://krohnos.com/guias/x/", llms)
        self.assertIn("https://krohnos.com/pt/guias/x/", llms)

    def test_guide_external_assets_and_claims(self):
        for rel in self.GUIDE.values():
            t = self.read(rel)
            for m in re.finditer(r'<(?:link|script|img|source|iframe)\b[^>]*?\b(?:src|href)="([^"]+)"', t):
                tag = m.group(0)
                if tag.startswith("<link") and ('rel="canonical"' in tag or 'rel="alternate"' in tag):
                    continue
                self.assertFalse(m.group(1).startswith(("http://", "https://", "//")), m.group(1))
            self.assertNotRegex(t, r"(?i)palpite|guess")
            self.assertNotRegex(t, r"(?i)\b(100%|garantid[oa]|guaranteed)\b")


if __name__ == "__main__":
    unittest.main()
