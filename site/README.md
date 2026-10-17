# Krohnos: self-host site

Static, bilingual (en, pt-BR) page for the self-host install. No framework, no npm, no external assets.

- `content.json`: the single source of the text (en + pt-BR) and the 5 install commands.
- `build.py`: Python stdlib, deterministic. Writes `dist/` with `index.html`, `pt/index.html`, `install.md`,
  `install.pt.md`, `llms.txt`, `robots.txt`, `sitemap.xml`, `favicon.svg`, `assets/`.
- `assets/hero-gl.js`: hero background, a ribbon of light drawn by a WebGL1 fragment shader (see below).
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
python3 tests/browser_test.py                # Playwright (chromium, software WebGL): copy buttons, i18n, theme, hero, mobile menu, WebGL ribbon; screenshots
```

`browser_test.py` writes screenshots to `$SHOTS_DIR` (default `./shots`, ignored by git).

## Hero background (WebGL shader)

`<canvas id="hero-gl">` behind the hero text, drawn by `assets/hero-gl.js`: two twisting sinusoidal surfaces,
fresnel alpha, soft blurred borders, fine animated grain, monochrome on pure black. 0 bytes of video or image, sharp at any size.

- Perfect loop (tested: the frame at t = T equals the frame at t = 0): all time enters through the phase (period T = 12 s, integer harmonics); the grain is seeded by an integer frame index in [0, 360).
- Cap of 30 fps; paused when the tab is hidden (`visibilitychange`), when the hero is scrolled away, or when the canvas is hidden.
- DPR capped at 2; the render scale drops (down to 0.5) when the median frame cost passes 20 ms (and grows back when it stays under 8 ms). The loop clock only advances while frames run, so pause/resume has no jump.
- `prefers-reduced-motion`: one still frame, no loop. No WebGL (or shader failure): the canvas stays transparent over the black page.
- Dark theme only: in the light theme the canvas is `display:none` and the hero is a flat background (decision: a white ribbon on a light page would not read).
- Legibility without an overlay: the shader draws only in the free band above the hero text (`uFree`, measured from `.hero-in`), so no line of text sits on the ribbon; `browser_test.py` measures WCAG AA against the brightest canvas pixel under each text line, in samples of the loop (5 moments on desktop, 3 on mobile, plus the still frames), not in every frame; the band keeps the ribbon off the text by construction.
- CSP unchanged (no `unsafe-eval`, no inline script): a canvas and `gl.shaderSource` need no relaxation.

## Deploy on Dokploy

Create an application from the repository `BRK13/krohnos`, branch `main`:

- Build type: Dockerfile
- Build path: `site`
- Dockerfile: `Dockerfile`
- Port: 80
- Domains: `krohnos.com` and `www.krohnos.com` (nginx redirects `www` to the root with a 301; TLS comes from Dokploy).

No GitHub Actions, no Vercel. To change the text, edit `content.json` and redeploy.
