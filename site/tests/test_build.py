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
                    "sitemap.xml", "favicon.svg", "assets/style.css", "assets/app.js", "assets/boot.js", "assets/hero-gl.js",
                    "assets/fonts/inter-latin-wght-normal.woff2",
                    "assets/fonts/instrument-serif-latin-400-italic.woff2"]:
            self.assertTrue((self.out / rel).is_file(), rel)

    def test_deterministic(self):
        other = Path(self.tmp.name) / "dist2"
        subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(other)], check=True, capture_output=True)
        for rel in ["index.html", "pt/index.html", "install.md", "install.pt.md", "llms.txt", "sitemap.xml"]:
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

    def test_early_access_without_unverified_commands(self):
        self.assertEqual(self.data["install_steps"], [])
        for rel in ["install.md", "install.pt.md", "index.html", "pt/index.html", "llms.txt"]:
            text = self.read(rel)
            for command in ("git clone", "krohnos init", "docker compose", "docker compose down"):
                self.assertNotIn(command, text)
            self.assertIn("https://github.com/BRK13/krohnos", text)
        self.assertIn("Acesso antecipado", self.read("pt/index.html"))
        self.assertIn("Early access", self.read("index.html"))

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
            self.assertIn("62.101.237/0001-16", text)
            self.assertIn("Krohn Capital &amp; Participações Ltda.", text)
            self.assertIn('href="mailto:brunokrohn@gmail.com"', text)
            self.assertNotIn("Status:", text)
            self.assertIn("EVIDENCE.md", text)
            self.assertIn("EVIDENCE.md", text)
            for domain in ["precedentes.com.br", "actacontrol.com", "fisiogentil.com", "fernandaferro.com", "psiporai.com"]:
                links = re.findall(r'<a[^>]+href="https://' + re.escape(domain) + r'"[^>]*>', text)
                self.assertGreaterEqual(len(links), 2, domain)
                for link in links:
                    self.assertNotRegex(link, "nofollow|sponsored|ugc")

    def test_institutional_homepage(self):
        for rel in ["index.html", "pt/index.html"]:
            text = self.read(rel)
            for term in ["Atenção", "Status:", "/comecar", "404"]:
                self.assertNotIn(term, text)
            self.assertLessEqual(text.count("<pre>"), 1)
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
        for u in ['href="#inside"',
                  'href="https://github.com/BRK13/krohnos/releases"', 'href="#install"']:
            self.assertIn(u, t)

    def test_no_forbidden_terms(self):
        """Every built text file, content.json and nginx.conf: none of the terms in tests/neutro_termos.txt."""
        regras, permitidos = neutro_lib.carregar()
        files = [p for p in self.out.rglob("*") if p.is_file() and p.suffix in
                 (".html", ".md", ".txt", ".xml", ".css", ".js", ".svg")]
        files += [SITE / "content.json", SITE / "nginx.conf"]
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
        self.assertNotIn('<canvas', text)
        self.assertNotIn('/assets/hero-gl.js', text)
        self.assertNotIn("unsafe-eval", (SITE / "nginx.conf").read_text())
        self.assertNotIn("unsafe-inline", (SITE / "nginx.conf").read_text())

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
        """BRU-1121: position both languages as self-hosted multi-agent orchestration."""
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


if __name__ == "__main__":
    unittest.main()
