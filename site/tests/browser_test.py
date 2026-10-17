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
                browser = pw.chromium.launch(headless=True)
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
                            page.locator('#nav a[href="#inside"]').click()
                            assert page.locator('#menu').get_attribute('aria-expanded') == 'false'
                            page.evaluate('scrollTo(0,0)')
                        else:
                            assert page.locator('.top').bounding_box()['y'] + page.locator('.top').bounding_box()['height'] < page.locator('h1').bounding_box()['y']
                        assert page.locator('#install a[href="https://github.com/BRK13/krohnos"]').count() == 1
                        assert page.locator('footer a[href="mailto:brunokrohn@gmail.com"]').count() == 1
                        assert page.locator('canvas').count() == 0
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
                # Page remains readable and useful without JavaScript.
                context = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
                page = context.new_page()
                page.goto(url + '/pt/')
                assert page.locator('#inside').is_visible()
                assert page.locator('.cta a[href="#install"]').is_visible()
                context.close()
                browser.close()
            inspect = os.environ.get('INSPECT_SCRIPT')
            if inspect:
                for lang, path in [('en', '/'), ('pt', '/pt/')]:
                    subprocess.run(['node', inspect, url + path, '--out', str(SHOTS / ('inspect-' + lang)), '--widths', '1440,768,390'], check=True)
        finally:
            server.shutdown()
    print('PASS browser regressions')


if __name__ == '__main__':
    main()
