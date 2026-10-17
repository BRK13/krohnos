"""Static checks on the build output. Run: python3 -m unittest discover -s tests (from site/)."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
FORBIDDEN = ["actacontrol", "acta", "precedentes", "jurimetria", "psiporai", "psi por aí", "psi por ai",
             "fisiogentil", "fisio gentil", "fernanda", "schwab", "bruno", "krohn", "whatsapp pessoal",
             "start for free", "pricing", "see it in action"]
# "krohn" is forbidden (owner surname), but the brand is Krohnos: "krohn" followed by "os" is allowed.
ALLOWED = ["BRK13/krohnos", "krohnos.com"]


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

    def test_install_commands_identical_everywhere(self):
        cmds = [s["cmd"] for s in self.data["install_steps"]]
        self.assertEqual(len(cmds), 5)
        import html as h
        for rel in ["install.md", "install.pt.md", "index.html", "pt/index.html"]:
            text = self.read(rel)
            if rel.endswith(".html"):
                text = h.unescape(text)
            for c in cmds:
                self.assertIn(c, text, (rel, c))
        # real commands from the install guide
        self.assertIn("git clone https://github.com/BRK13/krohnos.git krohnos && cd krohnos", cmds)
        self.assertIn("docker compose up -d", cmds)

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

    def test_update_and_uninstall_present(self):
        for rel in ["install.md", "install.pt.md"]:
            t = self.read(rel)
            self.assertIn("git pull && docker compose build painel && docker compose up -d", t)
            self.assertIn("docker compose down -v", t)

    def test_status_notice(self):
        self.assertIn("Status:", self.read("index.html"))
        self.assertIn("compose.yml", self.read("index.html"))
        self.assertIn("Status:", self.read("pt/index.html"))

    def test_llms_txt(self):
        t = self.read("llms.txt")
        self.assertTrue(t.startswith("# Krohnos\n\n> "))
        for u in ["https://krohnos.com/install.md", "https://krohnos.com/install.pt.md",
                  "https://github.com/BRK13/krohnos", "https://github.com/BRK13/krohnos/releases",
                  "https://github.com/BRK13/krohnos/blob/main/LICENSE",
                  "https://github.com/BRK13/krohnos/blob/main/SECURITY.md",
                  "https://github.com/BRK13/krohnos/blob/main/ROADMAP.md"]:
            self.assertIn(u, t)

    def test_nav_links(self):
        t = self.read("index.html")
        for u in ['href="https://github.com/BRK13/krohnos/tree/main/docs"',
                  'href="https://github.com/BRK13/krohnos/releases"', 'href="#install"']:
            self.assertIn(u, t)

    def test_no_forbidden_terms(self):
        files = [p for p in self.out.rglob("*") if p.is_file() and p.suffix in
                 (".html", ".md", ".txt", ".xml", ".css", ".js", ".svg")]
        files += [SITE / "content.json", SITE / "nginx.conf"]
        for p in files:
            text = p.read_text(encoding="utf-8")
            for a in ALLOWED:
                text = text.replace(a, "")
            low = text.lower()
            for w in FORBIDDEN:
                if w == "acta":
                    hit = re.search(r"\bacta\b", low)
                elif w == "krohn":  # a marca é Krohnos (BRU-1069); "krohn" sozinho (sobrenome) continua proibido
                    hit = re.search(r"krohn(?!os)", low)
                else:
                    hit = w in low
                self.assertFalse(hit, "%s contains %r" % (p.name, w))

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

    def test_hero_shader_contract(self):
        js = self.read("assets/hero-gl.js")
        html = self.read("index.html")
        self.assertIn('<canvas class="hero-gl" id="hero-gl" aria-hidden="true">', html)
        self.assertIn("/assets/hero-gl.js", html)
        for needle in ("visibilitychange", "prefers-reduced-motion", "var FPS = 30", "var T = 12", "Math.min(window.devicePixelRatio || 1, 2)"):
            self.assertIn(needle, js)
        # perfect loop: time only enters through the phase (period T); no clock or randomness in the shader
        self.assertNotIn("Math.random", js)
        self.assertNotIn("Date.now", js)
        self.assertNotRegex(js, r"\beval\b|new Function")
        frag = js[js.index("var FRAG"):js.index("var reduce")]
        self.assertIn("uPh", frag)
        self.assertNotRegex(frag, r"\buTime\b|\biTime\b")
        # CSP stays strict: a canvas needs no relaxation
        conf = (SITE / "nginx.conf").read_text(encoding="utf-8")
        self.assertNotIn("unsafe-eval", conf)
        self.assertNotIn("unsafe-inline", conf)
        # light theme: no canvas
        self.assertRegex(self.read("assets/style.css"), r"data-theme=light\] \.hero-gl\{display:none\}")

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
        """BRU-1078: the message (docs/marketing/mensagem-2026-10.md) is in <title>, og:title and <h1>; title <= 60."""
        import html as h
        cases = (("index.html", "prove the work is done", "Coding agents that prove the work is done."),
                 ("pt/index.html", "provam que terminaram", "Agentes de código que provam que o trabalho está pronto."))
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
