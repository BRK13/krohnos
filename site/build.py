#!/usr/bin/env python3
"""Static site build for the Krohnos self-host page. Python stdlib only, deterministic.

Single source of content: content.json (en + pt-BR). Output (default ./dist):
  index.html, pt/index.html, llms.txt, install.md, install.pt.md,
  robots.txt, sitemap.xml, favicon.svg, assets/ (css, js, fonts).
Usage: python3 build.py [--out DIR]
"""
import argparse
import html
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANGS = ("en", "pt")


def load():
    return json.loads((HERE / "content.json").read_text(encoding="utf-8"))


# ---------- inline markup: `code`, **bold**, [text](url) ----------
def inline_html(text):
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\(((?:https?://|mailto:)[^)\s]+)\)",
                 lambda m: '<a href="%s" rel="noopener">%s</a>' % (m.group(2), m.group(1)), out)
    return out


# ---------- commands ----------
def commands(data):
    return [s["cmd"] for s in data["install_steps"]]


def install_commented(data, lang):
    lines = []
    for i, s in enumerate(data["install_steps"], 1):
        lines.append("# %d. %s" % (i, s[lang].replace("`", "")))
        lines.append(s["cmd"])
        if i < len(data["install_steps"]):
            pass
    return "\n".join(lines)


def install_short(data):
    return "\n".join(commands(data))


# ---------- markdown ----------
def block_md(b, data, lang):
    t = b["t"]
    if t == "projects":
        return " · ".join("[%s](%s)" % (x["name"], x["url"]) for x in b["items"])
    if t == "p":
        return b["x"]
    if t == "ul":
        return "\n".join("- " + x for x in b["x"])
    if t == "warn":
        return "> **" + ("Warning" if lang == "en" else "Atenção") + ":** " + b["x"]
    if t == "code":
        return "```%s\n%s\n```" % (b.get("lang", "bash"), b["x"])
    if t == "install":
        return "```bash\n%s\n```" % install_commented(data, lang)
    raise ValueError(t)


def section_md(sec, data, lang):
    parts = ["## " + sec["title"]] + [block_md(b, data, lang) for b in sec["blocks"]]
    return "\n\n".join(parts) + "\n"


def page_md(data, lang):
    c = data[lang]
    head = "# %s\n\n> %s\n\n%s\n" % (c["md_title"], c["summary"], c["hero"]["status"])
    return head + "\n" + "\n".join(section_md(s, data, lang) for s in c["sections"])


# ---------- html ----------
def code_block(cid, text, ui, label):
    return (
        '<div class="cmd"><div class="cmd-bar"><span class="cmd-lang">%s</span>'
        '<button class="copy" type="button" data-copy="%s" data-label="%s" data-done="%s">%s</button></div>'
        '<pre><code id="%s">%s</code></pre></div>'
    ) % (html.escape(label), cid, html.escape(ui["copy"]), html.escape(ui["copied"]),
         html.escape(ui["copy"]), cid, html.escape(text))


def block_html(b, data, lang, sid, idx):
    ui = data[lang]["ui"]
    t = b["t"]
    if t == "projects":
        return '<div class="project-grid">' + ''.join(
            '<a class="project" href="%s"><img src="/assets/projects/%s.jpg" width="1000" height="650" loading="lazy" alt="%s"><span>%s <span aria-hidden="true">↗</span></span></a>' %
            (html.escape(x["url"], quote=True), html.escape(x["slug"], quote=True), html.escape(x["name"], quote=True), html.escape(x["name"]))
            for x in b["items"]) + '</div>'
    if t == "p":
        return "<p>%s</p>" % inline_html(b["x"])
    if t == "ul":
        return "<ul>%s</ul>" % "".join("<li>%s</li>" % inline_html(x) for x in b["x"])
    if t == "warn":
        return '<p class="warn"><strong>%s:</strong> %s</p>' % (
            "Warning" if lang == "en" else "Atenção", inline_html(b["x"]))
    if t == "code":
        return code_block("c-%s-%d" % (sid, idx), b["x"], ui, b.get("lang", "bash"))
    if t == "install":
        return code_block("c-install-full", install_commented(data, lang), ui, "bash")
    raise ValueError(t)


def section_html(sec, data, lang):
    ui = data[lang]["ui"]
    sid = sec["id"]
    body = "\n".join(block_html(b, data, lang, sid, i) for i, b in enumerate(sec["blocks"]))
    md = section_md(sec, data, lang)
    return (
        '<section class="sec appear" id="%s" aria-labelledby="h-%s">\n'
        '<div class="sec-head"><h2 id="h-%s">%s</h2>'
        '<button class="copy md" type="button" data-md="md-%s" data-label="%s" data-done="%s">%s</button></div>\n'
        '<div class="sec-body">\n%s\n</div>\n'
        '<script type="text/plain" id="md-%s">%s</script>\n</section>'
    ) % (sid, sid, sid, html.escape(sec["title"]), sid, html.escape(ui["copy_md_short"]),
         html.escape(ui["copied_md"]), html.escape(ui["copy_md_short"]), body, sid, md)


def terminal_html(c):
    rows = []
    for i, (k, v) in enumerate(c["terminal"]):
        rows.append('<li class="t%d"><span class="tv">%s</span></li>' % (
            i, html.escape(v)))
    return '<ol class="term">%s</ol>' % "".join(rows)


def page_html(data, lang):
    s = data["site"]
    c = data[lang]
    other = "pt" if lang == "en" else "en"
    ui = c["ui"]
    nav = c["nav"]
    url = {"en": s["domain"] + "/", "pt": s["domain"] + "/pt/"}
    prefix = "" if lang == "en" else "../"
    hero = c["hero"]
    md_full = page_md(data, lang)
    md_name = "install.md" if lang == "en" else "install.pt.md"
    h1_l1 = (html.escape(hero["line1_before"]) + '<em class="alt">' + html.escape(hero["line1_em"]) +
             "</em>" + html.escape(hero["line1_after"]))
    links = (
        '<a class="pill" href="#install">%s</a>'
        '<a class="pill" href="%s" rel="noopener">%s</a>'
        '<a class="pill" href="%s" rel="noopener">%s</a>'
        '<a class="pill" href="%s" rel="noopener">%s</a>'
    ) % (html.escape(nav["install"]), "#inside", html.escape(nav["docs"]), s["repo"],
         html.escape(nav["github"]), s["releases"], html.escape(nav["changelog"]))
    sections = "\n".join(section_html(sec, data, lang) for sec in c["sections"])
    other_href = "/pt/" if lang == "en" else "/"
    return f"""<!doctype html>
<html lang="{c['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="krohnos-release" content="2026-10-17">
<meta name="color-scheme" content="dark light">
<title>{html.escape(c['meta_title'])}</title>
<meta name="description" content="{html.escape(c['meta_description'])}">
<link rel="canonical" href="{url[lang]}">
<link rel="alternate" hreflang="en" href="{url['en']}">
<link rel="alternate" hreflang="pt-BR" href="{url['pt']}">
<link rel="alternate" hreflang="x-default" href="{url['en']}">
<link rel="alternate" type="text/markdown" href="{s['domain']}/{md_name}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{html.escape(c['name'])}">
<meta property="og:title" content="{html.escape(c['meta_title'])}">
<meta property="og:description" content="{html.escape(c['meta_description'])}">
<meta property="og:url" content="{url[lang]}">
<meta property="og:locale" content="{'en_US' if lang == 'en' else 'pt_BR'}">
<meta property="og:locale:alternate" content="{'pt_BR' if lang == 'en' else 'en_US'}">
<meta name="twitter:card" content="summary">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preload" href="/assets/fonts/inter-latin-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/assets/fonts/instrument-serif-latin-400-italic.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/assets/style.css">
<script src="/assets/boot.js"></script>
<script src="/assets/app.js" defer></script>

</head>
<body>
<a class="skip" href="#main">{html.escape(ui['skip'])}</a>
<header class="top">
  <a class="brand" href="{'/' if lang == 'en' else '/pt/'}" aria-label="{html.escape(c['name'])}"><img src="/assets/krohnos-mark.svg" width="32" height="38" alt=""><span>{html.escape(c['name'])}</span></a>
  <nav class="links" id="nav" aria-label="Main">{links}</nav>
  <div class="tools">
    <a class="pill small" href="{other_href}" hreflang="{'pt-BR' if lang == 'en' else 'en'}" lang="{'pt-BR' if lang == 'en' else 'en'}" aria-label="{html.escape(nav['language_label'])}">{html.escape(nav['language'])}</a>
    <button class="pill small" id="theme" type="button" aria-label="{html.escape(nav['theme'])}"><span class="th-dark">Dark</span><span class="th-light">Light</span></button>
    <button class="pill small menu-btn" id="menu" type="button" aria-expanded="false" aria-controls="nav">{html.escape(nav['menu'])}</button>
  </div>
</header>
<main id="main">
<section class="hero" aria-labelledby="h1">


  <div class="hero-in">

    <h1 id="h1"><span class="ln"><span class="ln-in l1">{h1_l1}</span></span> <span class="ln"><span class="ln-in l2">{html.escape(hero['line2'])}</span></span></h1>
    <p class="sub ap a3">{html.escape(hero['sub'])}</p>
    <div class="cta ap a5">
      <a class="btn solid" href="#install">{html.escape(nav['install'])} <span aria-hidden="true">↗</span></a>
      <a class="btn ghost" href="#inside">{html.escape(nav['docs'])}</a>
      <button class="btn ghost" type="button" data-md="md-page" data-label="{html.escape(ui['copy_md'])}" data-done="{html.escape(ui['copied_md'])}">{html.escape(ui['copy_md'])}</button>
    </div>

  </div>
  <div class="flow-preview"><p class="eyebrow">{"Da direção à entrega" if lang == "pt" else "From direction to delivery"}</p>{terminal_html(c)}<p class="flow-note">{"Claude Code + Codex · WhatsApp + painel" if lang == "pt" else "Claude Code + Codex · WhatsApp + panel"}</p></div>
</section>
<div class="rest">
{sections}
</div>
</main>
<footer class="foot">
  <p class="footer-brand">Krohnos</p>
  <p>Krohn Capital &amp; Participações Ltda. · CNPJ 62.101.237/0001-16</p>
  <p><a href="mailto:brunokrohn@gmail.com">brunokrohn@gmail.com</a></p>
  <p><a href="https://precedentes.com.br">Precedentes</a> · <a href="https://actacontrol.com">Acta Control</a> · <a href="https://fisiogentil.com">Fisio Gentil</a> · <a href="https://fernandaferro.com">Fernanda Ferro</a> · <a href="https://psiporai.com">Psi Por Aí</a></p>
  <p><a href="{s['repo']}" rel="noopener">github.com/BRK13/krohnos</a> · <a href="{s['license']}" rel="noopener">AGPL-3.0</a> · <a href="/{md_name}">{md_name}</a> · <a href="/llms.txt">llms.txt</a></p>
</footer>
<div class="sr" id="live" role="status" aria-live="polite"></div>
<script type="text/plain" id="md-page">{md_full}</script>
</body>
</html>
"""


def llms_txt(data):
    s = data["site"]
    en = data["en"]
    d = s["domain"]
    return f"""# {en['name']}

> {en['summary']}

Early access: the described system runs on our server. Follow the public repository with Watch or Star for the self-hosted distribution. No installation commands are published until a clean-machine install is verified.

## Early access

- [Install guide (Markdown, English)]({d}/install.md): capabilities, production evidence and early access
- [Guia de instalação (Markdown, português)]({d}/install.pt.md): funcionalidades, provas de produção e acesso antecipado

## Project

- [GitHub repository]({s['repo']}): source code, README and roadmap
- [Docs]({s['docs']}): documentation folder of the repository
- [Roadmap]({s['roadmap']}): what ships first
- [Security policy]({s['security']}): private reports through GitHub Security Advisories
- [License (AGPL-3.0)]({s['license']})
- [Releases / changelog]({s['releases']})

## Optional

- [Site, English]({d}/)
- [Site, português]({d}/pt/)
"""


def sitemap(data):
    d = data["site"]["domain"]
    def u(loc):
        return (f'  <url><loc>{loc}</loc>'
                f'<xhtml:link rel="alternate" hreflang="en" href="{d}/"/>'
                f'<xhtml:link rel="alternate" hreflang="pt-BR" href="{d}/pt/"/></url>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            + u(d + "/") + "\n" + u(d + "/pt/") + "\n</urlset>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "dist"))
    out = Path(ap.parse_args().out)
    data = load()
    if out.exists():
        shutil.rmtree(out)
    (out / "pt").mkdir(parents=True)
    (out / "index.html").write_text(page_html(data, "en"), encoding="utf-8")
    (out / "pt" / "index.html").write_text(page_html(data, "pt"), encoding="utf-8")
    (out / "install.md").write_text(page_md(data, "en"), encoding="utf-8")
    (out / "install.pt.md").write_text(page_md(data, "pt"), encoding="utf-8")
    (out / "llms.txt").write_text(llms_txt(data), encoding="utf-8")
    (out / "sitemap.xml").write_text(sitemap(data), encoding="utf-8")
    d = data["site"]["domain"]
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {d}/sitemap.xml\n", encoding="utf-8")
    shutil.copytree(HERE / "assets", out / "assets")
    shutil.copy(HERE / "favicon.svg", out / "favicon.svg")
    print("built", out)


if __name__ == "__main__":
    main()
