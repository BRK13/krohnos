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

# Software WebGL so the shader runs on machines without a GPU.
GL_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]

# Counts shader draws and animation frames, so the tests can tell what the hero is doing.
COUNT_JS = """
window.__draws = 0; window.__raf = 0;
for (const C of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
  if (!C) continue;
  const d = C.prototype.drawArrays;
  C.prototype.drawArrays = function () { window.__draws++; return d.apply(this, arguments); };
}
const r = window.requestAnimationFrame.bind(window);
window.requestAnimationFrame = function (cb) { window.__raf++; return r(cb); };
"""

NO_WEBGL_JS = """
const g = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function (t) {
  return /webgl/i.test(t) ? null : g.apply(this, arguments);
};
"""

HIDE_TEXT_CSS = ("h1,h1 *,.hero .sub,.hero .status,.hero .status *{color:transparent!important;"
                 "text-shadow:none!important}")

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


def lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rel_lum(rgb):
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def parse_rgb(css):
    return [float(x) for x in css[css.index("(") + 1:css.index(")")].replace("/", ",").split(",")[:3]]


def png_image(data):
    import io
    from PIL import Image
    return Image.open(io.BytesIO(data)).convert("RGB")


def hero_contrast(page, label):
    """The ribbon sits behind the text with no overlay: check that the text keeps WCAG AA against the brightest
    pixel of the canvas under each text line (3:1 for large text, 4.5:1 otherwise)."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("SKIP hero contrast (Pillow missing)")
        return
    probes = page.evaluate("""() => ['h1', 'h1 .alt', '.hero .sub', '.hero .status'].map(sel => {
        const el = document.querySelector(sel);
        const cs = getComputedStyle(el);
        const rg = document.createRange(); rg.selectNodeContents(el);
        const rects = [...rg.getClientRects()].filter(r => r.width > 2 && r.height > 2)
          .map(r => [r.left, r.top, r.right, r.bottom]);
        const px = parseFloat(cs.fontSize), w = parseInt(cs.fontWeight, 10);
        return {sel, color: cs.color, large: px >= 24 || (px >= 18.66 && w >= 700), rects};
    })""")
    page.add_style_tag(content=HIDE_TEXT_CSS)
    img = png_image(page.screenshot())
    for pr in probes:
        worst = 0.0
        for (l, t, r, b) in pr["rects"]:
            box = (max(0, int(l)), max(0, int(t)), min(img.width, int(r) + 1), min(img.height, int(b) + 1))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            worst = max(worst, max(rel_lum(p) for p in img.crop(box).getdata()))
        lt = rel_lum(parse_rgb(pr["color"]))
        ratio = (lt + 0.05) / (worst + 0.05)
        need = 3.0 if pr["large"] else 4.5
        check("[%s] contrast %s over ribbon %.1f:1 (need %.1f)" % (label, pr["sel"], ratio, need), ratio >= need, ratio)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory()
    dist = Path(tmp.name) / "dist"
    subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(dist)], check=True, capture_output=True)
    base = "http://127.0.0.1:%d" % PORT
    with serve(dist), sync_playwright() as pw:
        browser = pw.chromium.launch(args=GL_ARGS)
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

        # ---- WebGL ribbon in the hero ----
        def new_page(width=1440, height=900, scheme="dark", motion="no-preference", init=COUNT_JS):
            c = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion=motion)
            p = c.new_page()
            p.add_init_script(init)
            errs = []
            p.on("pageerror", lambda e: errs.append(str(e)))
            p.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            p.goto(base + "/", wait_until="networkidle")
            p.wait_for_timeout(1800)
            return c, p, errs

        def hero_bounds(p):
            return p.evaluate("(() => { const r = document.getElementById('hero-gl').getBoundingClientRect();"
                              " return {x: r.x, y: r.y, width: r.width, height: r.height}; })()")

        def lit(png):
            from PIL import Image  # noqa: F401
            im = png_image(png)
            px = list(im.getdata())
            return max(max(p) for p in px), sum(1 for p in px if max(p) > 40) / len(px)

        c, p, errs = new_page()
        info = p.evaluate("""() => { const c = document.getElementById('hero-gl'); const cs = getComputedStyle(c);
          return {tag: c.tagName, aria: c.getAttribute('aria-hidden'), pe: cs.pointerEvents, gl: c.dataset.gl,
                  ctx: !!c.getContext('webgl'), lost: c.getContext('webgl').isContextLost(), w: c.width, h: c.height,
                  inHero: !!c.closest('.hero'), cw: c.clientWidth, ch: c.clientHeight, op: cs.opacity,
                  heroH: document.querySelector('.hero').clientHeight}; }""")
        check("webgl: canvas exists in the hero", info["tag"] == "CANVAS" and info["inHero"], info)
        check("webgl: canvas aria-hidden and pointer-events none", info["aria"] == "true" and info["pe"] == "none", info)
        check("webgl: context created and alive", info["gl"] == "on" and info["ctx"] and not info["lost"], info)
        check("webgl: canvas covers the hero, no overlay (opacity 1)", info["ch"] == info["heroH"] and info["cw"] == 1440 and info["op"] == "1", info)
        check("webgl: DPR capped at 2", info["w"] <= 1440 * 2, info)
        clip = hero_bounds(p)
        top = dict(clip, height=clip["height"] * 0.3)
        mx, frac = lit(p.screenshot(clip=top))
        check("webgl: ribbon renders non-black pixels", mx > 120 and frac > 0.05, (mx, frac))
        # animated: two moments differ; frame cap 30 fps
        a1 = p.screenshot(clip=top)
        p.wait_for_timeout(1200)
        a2 = p.screenshot(clip=top)
        check("webgl: animates (two frames differ)", a1 != a2)
        d0 = p.evaluate("window.__draws")
        p.wait_for_timeout(2000)
        d1 = p.evaluate("window.__draws")
        check("webgl: capped at 30 fps (%d draws in 2 s)" % (d1 - d0), 5 <= d1 - d0 <= 63, d1 - d0)
        # pause on visibilitychange
        p.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        p.wait_for_timeout(300)
        d0 = p.evaluate("window.__draws")
        p.wait_for_timeout(800)
        check("webgl: paused while the tab is hidden", p.evaluate("window.__draws") == d0)
        p.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        p.wait_for_timeout(600)
        check("webgl: resumes when the tab is back", p.evaluate("window.__draws") > d0)
        # paused when the hero is off screen
        p.evaluate("window.scrollTo(0, document.querySelector('.hero').offsetHeight + 400)")
        p.wait_for_timeout(500)
        d0 = p.evaluate("window.__draws")
        p.wait_for_timeout(600)
        check("webgl: paused while the hero is off screen", p.evaluate("window.__draws") == d0)
        p.evaluate("window.scrollTo(0, 0)")
        check("webgl: no layout shift (canvas is out of flow)", p.evaluate(
            "getComputedStyle(document.getElementById('hero-gl')).position") == "absolute")
        check("webgl: no console/page errors", not errs, errs)
        c.close()

        # contrast over the moving ribbon: 5 moments of the loop
        c, p, _ = new_page()
        for i in range(5):
            if i:
                p.wait_for_timeout(2300)
                p.reload(wait_until="networkidle")
                p.wait_for_timeout(1800 + i * 1300)
            hero_contrast(p, "desktop t%d" % i)
        c.close()
        c, p, _ = new_page(390, 844)
        for i in range(3):
            if i:
                p.reload(wait_until="networkidle")
                p.wait_for_timeout(1800 + i * 3100)
            hero_contrast(p, "mobile t%d" % i)
        c.close()

        # reduced motion: one still frame that does not change
        for (w, h, nm) in ((1440, 900, "desktop"), (390, 844, "mobile")):
            c, p, errs = new_page(w, h, motion="reduce")
            clip = hero_bounds(p)
            top = dict(clip, height=clip["height"] * 0.4)
            s1 = p.screenshot(clip=top)
            p.wait_for_timeout(1500)
            s2 = p.screenshot(clip=top)
            mx, frac = lit(s1)
            check("reduced-motion %s: still frame is lit" % nm, mx > 120 and frac > 0.04, (mx, frac))
            check("reduced-motion %s: frame does not change between two moments" % nm, s1 == s2)
            d0 = p.evaluate("window.__draws")
            p.wait_for_timeout(700)
            d1 = p.evaluate("window.__draws")
            check("reduced-motion %s: one draw, no loop (%d draws)" % (nm, d1), d1 == d0 and d1 <= 3, (d0, d1))
            if nm == "desktop":
                hero_contrast(p, "reduced-motion desktop")
            else:
                hero_contrast(p, "reduced-motion mobile")
            check("reduced-motion %s: no errors" % nm, not errs, errs)
            c.close()
        c, p, _ = new_page(motion="reduce")
        p.screenshot(path=str(SHOTS / "desktop-dark-hero-still.png"))
        c.close()

        # light theme: no canvas, flat background, nothing renders
        c, p, _ = new_page(scheme="light")
        check("light theme: canvas hidden", p.evaluate("getComputedStyle(document.getElementById('hero-gl')).display") == "none")
        d0 = p.evaluate("window.__draws")
        p.wait_for_timeout(600)
        check("light theme: nothing is drawn", p.evaluate("window.__draws") == d0 == 0, d0)
        p.click("#theme")
        p.wait_for_timeout(900)
        check("theme toggle to dark brings the ribbon back", p.evaluate("window.__draws") > 0)
        c.close()

        # no WebGL: black hero, page keeps working
        c, p, errs = new_page(init=NO_WEBGL_JS)
        gl_state = p.evaluate("(() => { const c = document.getElementById('hero-gl'); return {gl: c.dataset.gl || '', "
                              "op: getComputedStyle(c).opacity, bg: getComputedStyle(document.body).backgroundColor}; })()")
        check("no WebGL: canvas stays off, page background black", gl_state["gl"] == "" and gl_state["op"] == "0"
              and gl_state["bg"] == "rgb(0, 0, 0)", gl_state)
        clip = hero_bounds(p)
        zone = dict(x=0, y=clip["y"] + 90, width=1000, height=140)
        mx, _ = lit(p.screenshot(clip=zone))
        check("no WebGL: hero background is black", mx <= 3, mx)
        p.locator("#c-hero").scroll_into_view_if_needed()
        b = p.locator("#c-hero").locator("xpath=ancestor::div[contains(@class,'cmd')]//button")
        p.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        b.click()
        check("no WebGL: copy button still works", p.evaluate("navigator.clipboard.readText()") ==
              p.evaluate("document.getElementById('c-hero').textContent"))
        check("no WebGL: headline visible, no errors", p.locator("h1").is_visible() and not errs, errs)
        p.evaluate("window.scrollTo(0,0)")
        p.screenshot(path=str(SHOTS / "desktop-dark-hero-no-webgl.png"))
        c.close()
        browser.close()

    tmp.cleanup()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
