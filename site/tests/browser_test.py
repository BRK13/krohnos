"""Focused browser regression checks; generates a temporary site fixture."""
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
SHOTS = Path(os.environ.get('SHOTS_DIR', 'shots')).resolve()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, str(SITE / 'build.py'), '--out', tmp + '/dist'], check=True)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=tmp + '/dist'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--use-gl=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
                for lang, path in [('en', '/'), ('pt', '/pt/')]:
                    for width, height in [(1440, 1000), (390, 844), (320, 740), (768, 1024)]:
                        page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        response = page.goto(url + path)
                        assert response.status == 200
                        page.evaluate('document.fonts.ready')
                        page.wait_for_timeout(150)
                        assert page.locator('h1').is_visible()
                        assert page.locator('.brand img').evaluate('(e) => e.complete && e.naturalWidth > 0')
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (lang, width, 'overflow')
                        assert not errors, errors
                        # Header/menu must remain above, rather than overlay, the heading.
                        if width <= 760:
                            page.locator('#menu').click()
                            assert page.locator('#menu').get_attribute('aria-expanded') == 'true'
                            assert page.locator('#nav').bounding_box()['y'] + page.locator('#nav').bounding_box()['height'] < page.locator('h1').bounding_box()['y']
                            page.screenshot(path=str(SHOTS / f'{lang}-{width}-menu.png'))
                            page.keyboard.press('Escape')
                            assert page.locator('#menu').get_attribute('aria-expanded') == 'false'
                            page.locator('#menu').click()
                            page.locator('#nav a[href="#orquestrador"]').click()
                            assert page.locator('#menu').get_attribute('aria-expanded') == 'false'
                            page.evaluate('scrollTo(0,0)')
                        else:
                            assert page.locator('.top').bounding_box()['y'] + page.locator('.top').bounding_box()['height'] < page.locator('h1').bounding_box()['y']
                        assert page.locator('footer a[href="https://github.com/BRK13/krohnos"]').count() == 1
                        assert page.locator('footer a[href="https://github.com/BRK13/krohnos/issues"]').count() == 1
                        assert page.locator('canvas#hero-gl').count() == 1
                        if width in (1440, 390):
                            page.screenshot(path=str(SHOTS / f'{lang}-{width}.png'), full_page=True)
                        if width == 1440:
                            page.locator('#theme').click()
                            assert page.locator('html').get_attribute('data-theme') == 'light'
                            page.reload()
                            assert page.locator('html').get_attribute('data-theme') == 'light'
                            page.screenshot(path=str(SHOTS / f'{lang}-{width}-light.png'), full_page=True)
                        print(f'PASS {lang} {width}: links, brand, no overflow, menu, no overlap, no JS errors')
                        page.close()
                # The ribbon is a fixed full-screen background and the hero is exactly one screen.
                for lang, path in [('en', '/'), ('pt', '/pt/')]:
                    for width, height in [(1366, 768), (1440, 900), (1920, 1080)]:
                        page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                        page.goto(url + path)
                        page.evaluate('document.fonts.ready')
                        page.wait_for_timeout(500)
                        m = page.evaluate('''() => {
                            const r = (s) => document.querySelector(s).getBoundingClientRect();
                            const c = document.getElementById('hero-gl'), cr = c.getBoundingClientRect();
                            const hero = document.querySelector('.hero');
                            return {ih: innerHeight, sh: document.documentElement.scrollHeight, hero: hero.offsetHeight,
                              heroScroll: hero.scrollHeight, heroTop: r('.hero').top, h1t: r('h1').top, h1b: r('h1').bottom,
                              hdr: r('.top').bottom, fpb: r('.flow-preview').bottom, ctab: r('.cta').bottom,
                              pos: getComputedStyle(c).position, cw: cr.width, ch: cr.height, cl: cr.left, ct: cr.top,
                              gl: c.dataset.gl, hidden: c.getAttribute('aria-hidden'), border: getComputedStyle(document.querySelector('.top')).borderBottomWidth}
                        }''')
                        tag = (lang, width, height, m)
                        assert m['hero'] <= m['ih'], tag
                        assert m['heroScroll'] <= m['hero'], ('hero content cut', tag)
                        assert m['sh'] <= m['ih'], ('desktop page must fit one screen', tag)
                        assert m['h1t'] >= m['hdr'], ('header covers title', tag)
                        assert m['h1b'] <= m['ih'] and m['fpb'] <= m['ih'] and m['ctab'] <= m['ih'], tag
                        assert m['pos'] == 'fixed' and m['cl'] == 0 and m['ct'] == 0, tag
                        assert m['cw'] == width and m['ch'] == height and m['hidden'] == 'true' and m['gl'] == 'on', tag
                        assert m['border'] == '0px', ('header border', tag)
                        png = page.screenshot()
                        from PIL import Image
                        import io
                        im = Image.open(io.BytesIO(png)).convert('L')
                        lower = im.crop((0, height // 2, width, height))
                        bright = sum(1 for v in lower.getdata() if v > 60)
                        assert bright > 0.02 * lower.width * lower.height, ('ribbon not painted', tag, bright)
                        page.close()
                        print(f'PASS hero one screen {lang} {width}x{height}')
                # Copy buttons put the real commands and the whole page, as Markdown, on the clipboard.
                for lang, path in [('en', '/'), ('pt', '/pt/')]:
                    context = browser.new_context(viewport={'width': 1440, 'height': 900}, permissions=['clipboard-read', 'clipboard-write'])
                    page = context.new_page()
                    page.goto(url + path)
                    page.locator('button[data-copy="c-install-hero"]').click()
                    page.wait_for_timeout(200)
                    cmds = page.evaluate('navigator.clipboard.readText()')
                    assert len(cmds.strip().splitlines()) == 5 and cmds.startswith('git clone https://github.com/BRK13/krohnos.git krohnos'), cmds
                    assert 'kro' + 'nos ' not in cmds
                    page.locator('button[data-md="md-page"]').click()
                    page.wait_for_timeout(200)
                    md = page.evaluate('navigator.clipboard.readText()')
                    assert md.startswith('# Krohnos') and cmds.splitlines()[0] in md, md[:200]
                    context.close()
                    print(f'PASS copy buttons {lang}')
                page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
                page.goto(url + '/')
                page.wait_for_timeout(300)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.close()
                # Page remains readable and useful without JavaScript.
                context = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
                page = context.new_page()
                page.goto(url + '/pt/')
                assert page.locator('#hero-install pre').is_visible()
                assert page.locator('.cta a[href="#hero-install"]').is_visible()
                context.close()
                browser.close()
            inspect = os.environ.get('INSPECT_SCRIPT')
            if inspect:
                # Two rules are waived by brand and policy, not by measurement (see VALIDATION.md):
                # few-pictures (dev-tool identity, no photographs) and no-footage (no generated video, BRU-825).
                allow = 'few-pictures,no-footage'
                for lang, path in [('en', '/'), ('pt', '/pt/')]:
                    subprocess.run(['node', inspect, url + path, '--out', str(SHOTS / ('inspect-' + lang)), '--widths', '1440,768,390', '--allow', allow], check=True)
        finally:
            server.shutdown()
    print('PASS browser regressions')


if __name__ == '__main__':
    main()
