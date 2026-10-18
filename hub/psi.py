"""PageSpeed Insights mobile (laboratório): uma rodada e a mediana de 3, como na."""
import json, os, statistics, time, urllib.parse, urllib.request
from pathlib import Path
from hub import faltou, home, segredos
from hub.comum import env as comum_env

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
ENV = segredos() / "pagespeed.env"
METRICAS = ("nota", "lcp_ms", "tbt_ms", "cls")


def chave():
    if (k := comum_env.segredo("PAGESPEED_CHAVE", (ENV,))):
        return k
    try:
        return json.loads((home() / "credenciais" / "pagespeed.json").read_text(encoding="utf-8")).get("chave")
    except (OSError, ValueError, AttributeError):
        return None


def lab(j):
    lh = j.get("lighthouseResult") or {}
    a, nota = lh.get("audits") or {}, ((lh.get("categories") or {}).get("performance") or {}).get("score")
    v = lambda k: (a.get(k) or {}).get("numericValue")  # noqa: E731
    return {"nota": round(nota * 100) if nota is not None else None, "lcp_ms": v("largest-contentful-paint"),
            "tbt_ms": v("total-blocking-time"), "cls": v("cumulative-layout-shift")}


def _get_json(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "nimbus-hub/seo-local"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def rodada(url, chave=None, buscar=None):
    try:
        from hub.seo_local import _get
    except ImportError as e:  # snapshot público: sem o plugin seo_local, o mesmo GET daqui
        if not faltou(e, "hub.seo_local"):
            raise
        _get = _get_json
    q = [("url", url), ("strategy", "mobile"), ("category", "performance")] + ([("key", chave)] if chave else [])
    return lab((buscar or (lambda u: _get(u, 180)))(API + "?" + urllib.parse.urlencode(q)))


def mediana(url, rodadas=3, chave=None, buscar=None, pausa=5):
    rs = []
    for i in range(rodadas):
        try:
            r = rodada(url, chave, buscar)
            if any(r[m] is not None for m in METRICAS):  # 200 sem lighthouseResult não é rodada
                rs.append(r)
        except (OSError, ValueError):
            pass  # 429/timeout: a mediana sai das rodadas que vieram
        if pausa and i < rodadas - 1:
            time.sleep(pausa)
    if not rs:
        return None
    med = {m: (statistics.median(x) if (x := [r[m] for r in rs if r[m] is not None]) else None) for m in METRICAS}
    return {"url": url, "rodadas": len(rs), **med}
