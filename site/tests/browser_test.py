"""Browser test (Playwright, chromium). Run from anywhere: python3 site/tests/browser_test.py
Builds dist/ to a temp dir, serves it on a high port only for the test, and checks behaviour.
Screenshots go to $SHOTS_DIR (default ./shots)."""
import contextlib
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("SHOTS_DIR", "shots")).expanduser()
PORT = int(os.environ.get("TEST_PORT", "48931"))

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def guess_type(self, path):
        if path.endswith(".md"):
            return "text/plain; charset=utf-8"
        return super().guess_type(path)


@contextlib.contextmanager
def serve(directory):
    h = functools.partial(Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), h)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield
    finally:
        srv.shutdown()


def read(p):
    return Path(p).read_text(encoding="utf-8")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory()
    dist = Path(tmp.name) / "dist"
    subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(dist)], check=True, capture_output=True)
    base = "http://127.0.0.1:%d" % PORT
    with serve(dist), sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["clipboard-read", "clipboard-write"],
                                  color_scheme="dark", reduced_motion="no-preference")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        requests = []
        page.on("request", lambda r: requests.append(r.url))

        for lang, path, mdfile in (("en", "/", "install.md"), ("pt", "/pt/", "install.pt.md")):
            page.goto(base + path, wait_until="networkidle")
            page.wait_for_timeout(1500)
            md_full = read(dist / mdfile)
            check("[%s] html lang" % lang, page.evaluate("document.documentElement.lang") == ("en" if lang == "en" else "pt-BR"))
            check("[%s] fonts loaded" % lang, page.evaluate(
                "document.fonts.check('600 20px Inter') && document.fonts.check('italic 20px \"Instrument Serif\"')"))

            # every copy button (code blocks)
            btns = page.locator("button.copy[data-copy]")
            n = btns.count()
            check("[%s] has copy buttons" % lang, n >= 8, n)
            for i in range(n):
                b = btns.nth(i)
                b.scroll_into_view_if_needed()
                expected = page.evaluate("id => document.getElementById(id).textContent", b.get_attribute("data-copy"))
                b.click()
                got = page.evaluate("navigator.clipboard.readText()")
                check("[%s] copy %s" % (lang, b.get_attribute("data-copy")), got == expected and len(got) > 3, repr(got)[:80])
                check("[%s] copy label %s" % (lang, i), b.inner_text() == b.get_attribute("data-done"), b.inner_text())
            live = page.locator("#live")
            check("[%s] aria-live set" % lang, live.get_attribute("aria-live") == "polite")

            # hero command text vs the install block: all five commands in the clipboard
            page.locator("#c-hero").scroll_into_view_if_needed()
            hero_cmds = page.evaluate("document.getElementById('c-hero').textContent").split("\n")
            check("[%s] hero has 5 commands" % lang, len(hero_cmds) == 5 and all(c in md_full for c in hero_cmds))

            # copy as markdown: page
            page.evaluate("window.scrollTo(0,0)")
            page.locator("button.btn.ghost").click()
            got = page.evaluate("navigator.clipboard.readText()")
            check("[%s] copy page markdown == %s" % (lang, mdfile), got == md_full, (len(got), len(md_full)))
            # a section
            sec_btn = page.locator("#install button.copy.md")
            sec_btn.scroll_into_view_if_needed()
            sec_btn.click()
            got = page.evaluate("navigator.clipboard.readText()")
            heading = "## " + page.locator("#install h2").inner_text()
            check("[%s] copy section markdown is the slice of %s" % (lang, mdfile),
                  got.startswith(heading) and got.strip() in md_full, got[:60])
            for sid in ("requirements", "config", "uninstall"):
                b = page.locator("#%s button.copy.md" % sid)
                b.scroll_into_view_if_needed()
                b.click()
                got = page.evaluate("navigator.clipboard.readText()")
                check("[%s] section %s slice" % (lang, sid), got.strip() in md_full and got.startswith("## "))

        # language toggle
        page.goto(base + "/", wait_until="networkidle")
        page.locator("a[hreflang='pt-BR']").first.click()
        page.wait_for_url("**/pt/")
        check("lang toggle en->pt", page.evaluate("document.documentElement.lang") == "pt-BR")
        page.locator("a[hreflang='en']").first.click()
        page.wait_for_url(base + "/")
        check("lang toggle pt->en", page.evaluate("document.documentElement.lang") == "en")

        # theme toggle + persistence
        page.evaluate("localStorage.clear()")
        page.goto(base + "/", wait_until="networkidle")
        bg_dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("dark background is pure black", bg_dark == "rgb(0, 0, 0)", bg_dark)
        page.click("#theme")
        bg_light = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("theme toggled to light", page.evaluate("document.documentElement.dataset.theme") == "light" and bg_light != bg_dark, bg_light)
        page.reload(wait_until="networkidle")
        check("theme remembered after reload", page.evaluate("document.documentElement.dataset.theme") == "light")
        page.click("#theme")
        check("theme back to dark", page.evaluate("document.documentElement.dataset.theme") == "dark")
        page.evaluate("localStorage.clear()")

        # hero: no vertical scroll inside the first screen at >= 901px
        for (w, h) in ((1440, 900), (1280, 720), (1920, 1080), (901, 800)):
            page.set_viewport_size({"width": w, "height": h})
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(1300)
            m = page.evaluate("""() => { const e = document.querySelector('.hero');
              const r = e.getBoundingClientRect(); const inner = document.querySelector('.hero-in').getBoundingClientRect();
              return {h: r.height, sh: e.scrollHeight, ch: e.clientHeight, top: inner.top, vh: innerHeight}; }""")
            check("hero fits one screen %dx%d" % (w, h), m["h"] <= m["vh"] + 1 and m["sh"] <= m["ch"] + 1 and m["top"] >= 0, m)
        page.set_viewport_size({"width": 1440, "height": 900})
        check("no horizontal overflow desktop", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

        # no external requests
        page.goto(base + "/", wait_until="networkidle")
        ext = [u for u in requests if not u.startswith(base)]
        check("no external requests", not ext, ext)
        check("no console/page errors", not errors, errors)

        # screenshots desktop
        def shots(name_prefix, w, h, scheme, path="/"):
            c = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=1)
            p = c.new_page()
            p.goto(base + path, wait_until="networkidle")
            p.wait_for_timeout(1800)
            p.screenshot(path=str(SHOTS / ("%s-hero.png" % name_prefix)))
            total = p.evaluate("document.documentElement.scrollHeight")
            y = 0
            while y < total:
                p.evaluate("window.scrollTo(0,%d)" % y)
                p.wait_for_timeout(120)
                y += h // 2
            p.wait_for_timeout(1000)
            p.evaluate("window.scrollTo(0,0)")
            p.screenshot(path=str(SHOTS / ("%s-full.png" % name_prefix)), full_page=True)
            c.close()

        shots("desktop-dark", 1440, 900, "dark")
        shots("desktop-light", 1440, 900, "light")
        shots("desktop-pt-dark", 1440, 900, "dark", "/pt/")
        shots("mobile-dark", 390, 844, "dark")
        shots("mobile-light", 390, 844, "light")

        # mobile menu
        m = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", permissions=["clipboard-read", "clipboard-write"])
        mp = m.new_page()
        mp.goto(base + "/", wait_until="networkidle")
        check("mobile nav hidden at rest", not mp.locator("#nav").is_visible())
        check("mobile no horizontal overflow", mp.evaluate("document.documentElement.scrollWidth <= innerWidth"),
              mp.evaluate("document.documentElement.scrollWidth"))
        mp.click("#menu")
        mp.wait_for_timeout(300)
        box = mp.locator("#nav").bounding_box()
        check("mobile menu opens full screen", mp.locator("#nav").is_visible() and box["width"] >= 389 and box["height"] >= 843, box)
        check("menu aria-expanded", mp.get_attribute("#menu", "aria-expanded") == "true")
        mp.screenshot(path=str(SHOTS / "mobile-menu-open.png"))
        mp.click("#nav a[href='#install']")
        mp.wait_for_timeout(300)
        check("menu closes on link", not mp.locator("#nav").is_visible())
        mp.click("#menu")
        mp.keyboard.press("Escape")
        check("menu closes on Escape", not mp.locator("#nav").is_visible())
        # copy works on mobile
        b = mp.locator("#c-hero").locator("xpath=ancestor::div[contains(@class,'cmd')]//button")
        b.scroll_into_view_if_needed()
        b.click()
        check("mobile copy", mp.evaluate("navigator.clipboard.readText()") == mp.evaluate("document.getElementById('c-hero').textContent"))
        m.close()

        # without JS: commands readable, nav links reachable
        nj = browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=False)
        np_ = nj.new_page()
        np_.goto(base + "/", wait_until="networkidle")
        check("no-JS: command visible", np_.locator("#c-hero").is_visible())
        check("no-JS: sections visible", np_.locator("#uninstall").count() == 1)
        check("no-JS: section content opacity 1", np_.evaluate("getComputedStyle(document.querySelector('.sec')).opacity") == "1")
        nj.close()

        # reduced motion: content visible
        rm = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        rp = rm.new_page()
        rp.goto(base + "/", wait_until="networkidle")
        check("reduced-motion: h1 and sections visible", rp.evaluate(
            "getComputedStyle(document.querySelector('.sec')).opacity") == "1" and rp.locator("h1").is_visible())
        rm.close()
        browser.close()

    tmp.cleanup()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
