# Kronos Hub: self-host site

Static, bilingual (en, pt-BR) page for the self-host install. No framework, no npm, no external assets.

- `content.json`: the single source of the text (en + pt-BR) and the 5 install commands.
- `build.py`: Python stdlib, deterministic. Writes `dist/` with `index.html`, `pt/index.html`, `install.md`,
  `install.pt.md`, `llms.txt`, `robots.txt`, `sitemap.xml`, `favicon.svg`, `assets/`.
- `assets/`: CSS, JS and the self-hosted fonts (Inter and Instrument Serif, SIL OFL 1.1; see `assets/fonts/LICENSES.txt`).
- `Dockerfile` + `nginx.conf`: build with `python:3.12-alpine`, serve with `nginx:alpine` (correct types for
  `.md`/`.txt`/`.xml`, asset cache, security headers with a strict CSP, gzip, `www` to apex 301).

## Build and preview

```bash
cd site
python3 build.py                  # writes site/dist
python3 -m http.server 8099 -d dist
```

Paths in the HTML are absolute (`/assets/...`), so serve `dist/` from a web root, not from `file://`.

## Tests

```bash
cd site
python3 -m unittest discover -s tests -v     # build output, commands, links, forbidden terms, no CDN
python3 tests/browser_test.py                # Playwright (chromium): copy buttons, i18n, theme, hero, mobile menu; screenshots
```

`browser_test.py` writes screenshots to `$SHOTS_DIR` (default `./shots`, ignored by git).

## Deploy on Dokploy

Create an application from the repository `BRK13/krohnos`, branch `main`:

- Build type: Dockerfile
- Build path: `site`
- Dockerfile: `Dockerfile`
- Port: 80
- Domains: `krohnos.com` and `www.krohnos.com` (nginx redirects `www` to the root with a 301; TLS comes from Dokploy).

No GitHub Actions, no Vercel. To change the text, edit `content.json` and redeploy.
