"""Verificador do padrão de SEO (PADRAO-SEO.md): confere no site no ar os 10 itens e devolve ok/falta com a prova.

    python verificar_site.py fsite05                    # um site
    python verificar_site.py --todos                  # todos os sites ativos
    python verificar_site.py --todos --avisar   # como roda no cron da VPS

Saída: $HUB_DATA/verificador/<site>/<AAAA-MM-DD>.json, latest.json e latest.md. Com --avisar, WhatsApp (deploy/avisar.sh) só com o que piorou (item que estava ok e agora falta).
Site com `padrao.fora_do_google` (ex.: fsite06) não é verificado: o noindex dele é intencional.

Cada item volta com `ok` true (cumpre), false (falta) ou null (não deu para verificar daqui, ex.: sem credencial),
e as provas: URL, status, trecho.
"""
import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from hub import RAIZ, data, home, sites

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 NimbusVerificador/1.0"
AMOSTRA_SITEMAP = 12
MAX_SITEMAPS = 25  # arquivos de sitemap lidos por verificação (índices grandes ficam amostrados)
BOTS_BUSCA_IA = ("OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Claude-SearchBot", "Claude-User")
BOTS_TREINO = ("GPTBot", "ClaudeBot", "Google-Extended", "CCBot")
TIPOS_ORG = {"Organization", "Corporation", "LocalBusiness", "ProfessionalService", "MedicalBusiness", "MedicalClinic",
             "MedicalOrganization", "Physician", "LegalService", "Attorney", "EducationalOrganization", "NGO",
             "OnlineBusiness", "HealthAndBeautyBusiness", "Person"}
TITULO = (30, 65)
DESCRICAO = (70, 160)
NOMES = {1: "Host canônico e redirecionamentos 301", 2: "Sem noindex em página pública", 3: "Canonical absoluto e próprio",
         4: "robots.txt (IA de busca, treino, Sitemap, Content-Signal)", 5: "Sitemap válido e com lastmod real",
         6: "llms.txt gerado do conteúdo", 7: "JSON-LD Organization/Person + WebSite com @id estável",
         8: "og:locale pt_BR, título, descrição e og:image", 9: "GA4 e Clarity no ar; GSC e Bing verificados com sitemap",
         10: "IndexNow: chave no ar e aviso na publicação"}


# ---------- HTTP ----------

class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_abridor = urllib.request.build_opener(_SemRedirecionar)


class Resposta:
    def __init__(self, url, status=None, headers=None, corpo="", erro=None):
        self.url, self.status, self.headers, self.corpo, self.erro = url, status, headers or {}, corpo, erro

    @property
    def local(self):
        loc = self.headers.get("location")
        return urllib.parse.urljoin(self.url, loc) if loc else None


def buscar(url, limite=3_000_000):
    """GET sem seguir redirecionamento. Nunca levanta: erro de rede vira Resposta com `erro`."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                               "Accept-Language": "pt-BR,pt;q=0.9"})
    try:
        with _abridor.open(req, timeout=25) as r:
            return Resposta(url, r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(limite).decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        corpo = ""
        try:
            corpo = e.read(200_000).decode("utf-8", "ignore")
        except Exception:
            pass
        return Resposta(url, e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, corpo)
    except Exception as e:
        return Resposta(url, erro=f"{type(e).__name__}: {str(e)[:120]}")


class Cache:
    def __init__(self, buscar_=buscar):
        self._buscar, self._vistos = buscar_, {}

    def get(self, url, limite=3_000_000):
        if url not in self._vistos:
            self._vistos[url] = self._buscar(url, limite)
        return self._vistos[url]


def cadeia(cache, url, maximo=4):
    """Segue redirecionamentos um a um: (ok_permanente, destino_final, texto). ok só se todos forem 301/308."""
    passos, atual, permanente = [], url, True
    for _ in range(maximo):
        r = cache.get(atual)
        if r.status in (301, 302, 303, 307, 308) and r.local:
            permanente &= r.status in (301, 308)
            passos.append(f"{r.status} {r.local}")
            atual = r.local
            continue
        passos.append(str(r.status or r.erro))
        return permanente and len(passos) > 1, atual, f"{url} → " + " → ".join(passos)
    return False, atual, f"{url} → " + " → ".join(passos) + " (redirecionamentos demais)"


# ---------- HTML ----------

class Pagina(HTMLParser):
    """O que interessa do <head> e os blocos JSON-LD."""

    def __init__(self, texto):
        super().__init__(convert_charrefs=True)
        self.titulo, self.metas, self.canonicals, self.jsonld, self.scripts_src = None, [], [], [], []
        self._no_titulo, self._no_ld, self._buf = False, False, []
        try:
            self.feed(texto or "")
            self.close()
        except Exception:
            pass

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.titulo is None:
            self._no_titulo, self._buf = True, []
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonicals.append(a.get("href", ""))
        elif tag == "script":
            if a.get("type", "").lower().strip() == "application/ld+json":
                self._no_ld, self._buf = True, []
            if a.get("src"):
                self.scripts_src.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title" and self._no_titulo:
            self.titulo, self._no_titulo = re.sub(r"\s+", " ", "".join(self._buf)).strip(), False
        elif tag == "script" and self._no_ld:
            self.jsonld.append("".join(self._buf))
            self._no_ld = False

    def handle_data(self, d):
        if self._no_titulo or self._no_ld:
            self._buf.append(d)

    def meta(self, chave):
        """Conteúdo de <meta name=...> ou <meta property=...>."""
        for m in self.metas:
            if m.get("name", "").lower() == chave or m.get("property", "").lower() == chave:
                return m.get("content", "").strip()
        return None

    def robots(self):
        return " ".join(m.get("content", "") for m in self.metas if m.get("name", "").lower() in ("robots", "googlebot")).lower()


def noindex(resp, pagina):
    return "noindex" in pagina.robots() or "noindex" in resp.headers.get("x-robots-tag", "").lower()


def mesma_url(a, b):
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    caminho = lambda p: p.path or "/"
    return (pa.scheme, (pa.hostname or "").lower(), caminho(pa), pa.query) == (pb.scheme, (pb.hostname or "").lower(), caminho(pb), pb.query)


# ---------- robots.txt ----------

def ler_robots(texto):
    """{'grupos': [(agentes, regras)], 'sitemaps': [...], 'content_signal': [...]} (RFC 9309)."""
    grupos, sitemaps, sinais = [], [], []
    agentes, regras, lendo_agentes = [], [], False
    for linha in (texto or "").splitlines():
        linha = linha.split("#", 1)[0].strip()
        if ":" not in linha:
            continue
        chave, valor = (x.strip() for x in linha.split(":", 1))
        chave = chave.lower()
        if chave == "user-agent":
            if not lendo_agentes and (agentes or regras):
                grupos.append((agentes, regras))
                agentes, regras = [], []
            agentes.append(valor.lower())
            lendo_agentes = True
        elif chave in ("allow", "disallow"):
            regras.append((chave, valor))
            lendo_agentes = False
        elif chave == "sitemap":
            sitemaps.append(valor)
        elif chave == "content-signal":
            sinais.append(valor)
            lendo_agentes = False
        else:
            lendo_agentes = False
    if agentes or regras:
        grupos.append((agentes, regras))
    return {"grupos": grupos, "sitemaps": sitemaps, "content_signal": sinais}


def _casa_regra(regra, caminho):
    padrao = re.escape(regra).replace(r"\*", ".*")
    if padrao.endswith(r"\$"):
        padrao = padrao[:-2] + "$"
    return re.match(padrao, caminho) is not None


def permite(robots, agente, caminho="/"):
    """O robô pode buscar o caminho? Grupo do agente (ou *); regra mais longa vence; empate, allow vence."""
    agente = agente.lower()
    regras = [r for ags, rs in robots["grupos"] if agente in ags for r in rs]
    if not any(agente in ags for ags, _ in robots["grupos"]):
        regras = [r for ags, rs in robots["grupos"] if "*" in ags for r in rs]
    melhor = None
    for tipo, valor in regras:
        if valor == "" and tipo == "disallow":
            continue  # "Disallow:" vazio libera tudo
        if _casa_regra(valor, caminho):
            chave = (len(valor), tipo == "allow")
            if melhor is None or chave > melhor[0]:
                melhor = (chave, tipo)
    return melhor is None or melhor[1] == "allow"


def nomeado(robots, agente):
    return any(agente.lower() in ags for ags, _ in robots["grupos"])


# ---------- sitemap ----------

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def ler_sitemap(texto):
    """('urlset'|'sitemapindex', [(loc, lastmod)]). XML inválido levanta ValueError."""
    try:
        raiz = ET.fromstring(texto.encode("utf-8") if isinstance(texto, str) else texto)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}") from None
    tipo = raiz.tag.replace(NS, "")
    if tipo not in ("urlset", "sitemapindex"):
        raise ValueError(f"raiz <{tipo}> não é urlset nem sitemapindex")
    itens = []
    for el in raiz:
        loc = el.find(NS + "loc")
        mod = el.find(NS + "lastmod")
        if loc is not None and loc.text:
            itens.append((loc.text.strip(), mod.text.strip()[:10] if mod is not None and mod.text else None))
    return tipo, itens


def lastmod_real(itens, hoje):
    """(ok, motivo). Falta lastmod, ou todas iguais (data do build) quando há pelo menos 5 URLs, não é real."""
    datas = [m for _, m in itens if m]
    if not itens:
        return False, "sitemap sem URL"
    if len(datas) < 0.8 * len(itens):
        return False, f"lastmod em {len(datas)} de {len(itens)} URLs"
    distintas = set(datas)
    if len(itens) >= 5 and len(distintas) == 1:
        d = next(iter(distintas))
        return False, f"todas as {len(datas)} URLs com lastmod {d}" + (" (hoje: data do build, não da página)" if d == hoje.isoformat() else "")
    # lastmod costuma vir em UTC (date-only); até meia-noite de BRT o "amanhã" UTC é legítimo
    limite = (hoje + dt.timedelta(days=1)).isoformat()
    futuras = [d for d in datas if d > limite]
    if futuras:
        return False, f"{len(futuras)} lastmod no futuro (ex.: {futuras[0]})"
    return True, f"{len(distintas)} datas distintas em {len(datas)} URLs; mais recente {max(datas)}"


def amostra(itens, extras, n=AMOSTRA_SITEMAP):
    """Amostra estável semana a semana (por hash da URL) mais as 3 mais recentes por lastmod."""
    estavel = sorted((u for u, _ in itens), key=lambda u: hashlib.sha1(u.encode()).hexdigest())[:n]
    recentes = [u for u, m in sorted((x for x in itens if x[1]), key=lambda x: x[1], reverse=True)[:3]]
    saida, chaves = [], set()
    for u in list(extras) + recentes + estavel:
        p = urllib.parse.urlsplit(u)
        chave = (p.hostname, p.path.rstrip("/") or "/", p.query)  # https://x.com e https://x.com/ são a mesma página
        if chave not in chaves:
            chaves.add(chave)
            saida.append(u)
    return saida


# ---------- JSON-LD ----------

def entidades(blocos):
    """(entidades, erros): achata @graph e listas; cada bloco precisa ser JSON válido."""
    saida, erros = [], []
    for i, bruto in enumerate(blocos):
        try:
            dado = json.loads(bruto)
        except ValueError as e:
            erros.append(f"bloco {i + 1} não é JSON válido ({e.msg})")
            continue
        fila = dado if isinstance(dado, list) else [dado]
        while fila:
            x = fila.pop(0)
            if isinstance(x, dict):
                if "@graph" in x:
                    fila += x["@graph"] if isinstance(x["@graph"], list) else [x["@graph"]]
                if "@type" in x:
                    saida.append(x)
            elif isinstance(x, list):
                fila += x
    return saida, erros


def tipos(e):
    t = e.get("@type")
    return set(t if isinstance(t, list) else [t])


# ---------- itens ----------

def item(n, ok, provas, falhas=()):
    return {"n": n, "item": NOMES[n], "ok": ok, "provas": list(provas)[:12], "falhas": list(falhas)[:12]}


def verificar(site, cache=None, hoje=None, extras=None):
    """Os 10 itens do padrão para um site. `extras` injeta GSC/Bing (testes); padrão consulta as APIs."""
    cache = cache or Cache()
    hoje = hoje or dt.date.today()
    p = site["padrao"]
    base = f"https://{site['dominio']}"
    resultados = []

    # 1. host canônico
    provas, falhas = [], []
    home_resp = cache.get(base + "/")
    if home_resp.status == 200:
        provas.append(f"{base}/ → 200")
    else:
        falhas.append(f"{base}/ → {home_resp.status or home_resp.erro} {home_resp.local or ''}".strip())
    caminho_teste = next((c for c in p["paginas_amostra"] if c != "/"), "/")
    alvo = f"{base}{caminho_teste}"
    origens = [f"{esq}://{h}{caminho_teste}" for h in site["hosts_alternativos"] for esq in ("https", "http")]
    for origem in origens + [f"http://{site['dominio']}{caminho_teste}"]:
        permanente, final, texto = cadeia(cache, origem)
        if permanente and mesma_url(final, alvo):
            provas.append(texto)
        else:
            falhas.append(f"{texto} (esperado 301/308 para {alvo})")
    resultados.append(item(1, not falhas, provas, falhas))

    # robots e sitemap (base dos itens 2-6)
    robots_resp = cache.get(base + "/robots.txt")
    robots = ler_robots(robots_resp.corpo if robots_resp.status == 200 else "")
    urls_sitemap, sitemap_erros, sitemap_provas = [], [], []
    fila = [u for u in robots["sitemaps"] if u.startswith("http")] or [base + "/sitemap.xml"]
    vistos = set()
    while fila and len(vistos) < MAX_SITEMAPS:
        alvo = fila.pop(0)
        if alvo in vistos:
            continue
        vistos.add(alvo)
        r = cache.get(alvo, 60_000_000)  # o protocolo permite até 50 MB por arquivo
        if r.status != 200:
            sitemap_erros.append(f"{alvo} → {r.status or r.erro}")
            continue
        try:
            tipo, itens = ler_sitemap(r.corpo)
        except ValueError as e:
            sitemap_erros.append(f"{alvo}: {e}")
            continue
        sitemap_provas.append(f"{alvo}: {tipo} com {len(itens)} itens")
        if tipo == "sitemapindex":
            fila += [u for u, _ in itens]
        else:
            urls_sitemap += itens
    paginas_alvo = amostra(urls_sitemap, [base + c for c in p["paginas_amostra"]])
    analisadas = {}
    for url in paginas_alvo:
        r = cache.get(url)
        analisadas[url] = (r, Pagina(r.corpo) if r.status == 200 else None)

    def excecao(url):
        caminho = urllib.parse.urlsplit(url).path or "/"
        return any(fnmatch.fnmatch(caminho, padrao) for padrao in p["noindex_permitido"])

    # 2. noindex
    com_noindex = [u for u, (r, pg) in analisadas.items() if pg and noindex(r, pg) and not excecao(u)]
    resultados.append(item(2, not com_noindex and bool(analisadas),
                           [f"{len(analisadas)} páginas amostradas (sitemap + páginas principais), nenhuma com noindex fora das exceções"]
                           if not com_noindex else [], [f"noindex em {u}" for u in com_noindex] or
                           ([] if analisadas else ["nenhuma página amostrada"])))

    # 3. canonical
    provas, falhas = [], []
    for u, (r, pg) in analisadas.items():
        if not pg:
            continue
        if not pg.canonicals:
            falhas.append(f"{u}: sem <link rel=canonical>")
        elif len(set(pg.canonicals)) > 1:
            falhas.append(f"{u}: {len(set(pg.canonicals))} canonicals diferentes")
        elif not pg.canonicals[0].startswith("https://"):
            falhas.append(f"{u}: canonical relativo ou sem https: {pg.canonicals[0]!r}")
        elif not mesma_url(pg.canonicals[0], u):
            falhas.append(f"{u}: canonical aponta para {pg.canonicals[0]}")
        else:
            provas.append(f"{u}: canonical = própria URL")
    resultados.append(item(3, not falhas and bool(provas), provas[:5], falhas))

    # 4. robots.txt
    provas, falhas = [], []
    if robots_resp.status != 200:
        falhas.append(f"{base}/robots.txt → {robots_resp.status or robots_resp.erro}")
    else:
        for bot in BOTS_BUSCA_IA:
            (provas if permite(robots, bot) else falhas).append(f"{bot}: {'liberado' if permite(robots, bot) else 'bloqueado'} em /")
        for bot in BOTS_TREINO:
            if not nomeado(robots, bot):
                falhas.append(f"{bot}: sem grupo próprio (escolha sobre treino não declarada)")
                continue
            liberado = permite(robots, bot)
            esperado = {"permitir": True, "bloquear": False}.get(p["treino_ia"])
            if esperado is not None and liberado != esperado:
                falhas.append(f"{bot}: {'liberado' if liberado else 'bloqueado'}, mas o site escolheu {p['treino_ia']}")
            else:
                provas.append(f"{bot}: declarado ({'liberado' if liberado else 'bloqueado'})")
        (provas if robots["sitemaps"] else falhas).append("Sitemap: " + (", ".join(robots["sitemaps"]) or "ausente"))
        (provas if robots["content_signal"] else falhas).append("Content-Signal: " + ("; ".join(robots["content_signal"]) or "ausente"))
    resultados.append(item(4, not falhas, provas, falhas))

    # 5. sitemap
    provas, falhas = list(sitemap_provas), list(sitemap_erros)
    if urls_sitemap:
        fora = [u for u, _ in urls_sitemap if (urllib.parse.urlsplit(u).hostname or "") != site["dominio"]]
        if fora:
            falhas.append(f"{len(fora)} URLs fora do host canônico (ex.: {fora[0]})")
        lm_ok, lm_motivo = lastmod_real(urls_sitemap, hoje)
        (provas if lm_ok else falhas).append("lastmod: " + lm_motivo)
        for u, (r, pg) in analisadas.items():
            if u.rstrip("/") in {x.rstrip("/") for x, _ in urls_sitemap}:
                if r.status != 200:
                    falhas.append(f"{u} no sitemap → {r.status or r.erro} {r.local or ''}".strip())
                elif pg and noindex(r, pg):
                    falhas.append(f"{u} no sitemap tem noindex")
        provas.append(f"{len(urls_sitemap)} URLs; amostra conferida: {sum(1 for u in analisadas)} páginas"
                      + (f"; lidos {MAX_SITEMAPS} arquivos de sitemap, {len(fila)} ficaram de fora da conferência" if fila else ""))
    elif not sitemap_erros:
        falhas.append("nenhuma URL no sitemap")
    resultados.append(item(5, not falhas and bool(urls_sitemap), provas, falhas))

    # 6. llms.txt
    provas, falhas = [], []
    r = cache.get(base + "/llms.txt")
    if r.status != 200:
        falhas.append(f"{base}/llms.txt → {r.status or r.erro}")
    else:
        if not r.corpo.lstrip().startswith("# "):
            falhas.append("não começa com título '# '")
        links = set(re.findall(r"\((https?://[^)\s]+)\)", r.corpo)) | set(re.findall(r"https?://[^\s)>\]]+", r.corpo))
        do_site = {l.rstrip("/.,") for l in links if (urllib.parse.urlsplit(l).hostname or "").removeprefix("www.") == site["dominio"]}
        provas.append(f"{len(do_site)} links do site")
        if len(do_site) < 5:
            falhas.append(f"só {len(do_site)} links do site")
        principais = [base + c for c in p["paginas_amostra"] if c != "/"]
        faltando = [c for c in principais if c.rstrip("/") not in do_site]
        if faltando:
            falhas.append("páginas principais fora: " + ", ".join(faltando))
        # As principais aparecem até numa lista fixa; o que prova geração é trazer as páginas novas.
        fixas = {(base + c).rstrip("/") for c in p["paginas_amostra"]}
        ordem = [x for x in sorted((x for x in urls_sitemap if x[1]), key=lambda x: x[1], reverse=True)
                 if x[0].rstrip("/") not in fixas]
        recentes = [u for u, _ in ordem[:5]]
        # lastmod empatado (build em massa): "as 5 mais recentes" seria sorteio, não prova de lista fixa
        empatado = len(ordem) > 5 and sum(1 for _, m in ordem if m >= ordem[4][1]) > 5
        if empatado:
            provas.append("lastmod empatado entre mais de 5 páginas: recência não julga se o llms.txt é gerado")
        elif recentes:
            presentes = [u for u in recentes if u.rstrip("/") in do_site]
            if presentes:
                provas.append(f"traz {len(presentes)} das {len(recentes)} páginas mais recentes do sitemap (ex.: {presentes[0]}): gerado do conteúdo")
            else:
                falhas.append(f"nenhuma das {len(recentes)} páginas mais recentes do sitemap (ex.: {recentes[0]}): parece lista fixa")
        elif urls_sitemap:
            cobertura = len(do_site & {u.rstrip("/") for u, _ in urls_sitemap}) / len(urls_sitemap)
            (provas if cobertura >= 0.3 else falhas).append(f"sem lastmod para julgar; cobre {cobertura:.0%} do sitemap")
    resultados.append(item(6, not falhas, provas, falhas))

    # 7. JSON-LD
    provas, falhas = [], []
    ids_org, ids_web, tipos_home = set(), set(), set()
    for u, (r, pg) in analisadas.items():
        if not pg:
            continue
        ents, erros = entidades(pg.jsonld)
        falhas += [f"{u}: {e}" for e in erros]
        org = [e for e in ents if tipos(e) & TIPOS_ORG and e.get("@id")]
        web = [e for e in ents if "WebSite" in tipos(e) and e.get("@id")]
        if not org:
            falhas.append(f"{u}: sem Organization/Person com @id")
        if not web:
            falhas.append(f"{u}: sem WebSite com @id")
        ids_org |= {e["@id"] for e in org}
        ids_web |= {e["@id"] for e in web}
        if u.rstrip("/") == base:
            tipos_home = set().union(*(tipos(e) for e in ents)) if ents else set()
    if len(ids_web) > 1:
        falhas.append(f"@id do WebSite muda entre páginas: {sorted(ids_web)[:3]}")
    if ids_org:
        provas.append(f"Organization/Person @id: {', '.join(sorted(ids_org)[:3])}")
    if ids_web:
        provas.append(f"WebSite @id: {', '.join(sorted(ids_web))}")
    if p["tipo_negocio"]:
        (provas if p["tipo_negocio"] in tipos_home else falhas).append(
            f"tipo do negócio {p['tipo_negocio']} {'na' if p['tipo_negocio'] in tipos_home else 'ausente da'} home")
    resultados.append(item(7, not falhas and bool(ids_org) and bool(ids_web), provas, falhas))

    # 8. metadados
    provas, falhas = [], []
    for u, (r, pg) in analisadas.items():
        if not pg:
            continue
        erros = []
        if (pg.meta("og:locale") or "") != "pt_BR":
            erros.append(f"og:locale={pg.meta('og:locale')!r}")
        t = pg.titulo or ""
        if not TITULO[0] <= len(t) <= TITULO[1]:
            erros.append(f"título com {len(t)} caracteres")
        d = pg.meta("description") or ""
        if not DESCRICAO[0] <= len(d) <= DESCRICAO[1]:
            erros.append(f"descrição com {len(d)} caracteres")
        img = pg.meta("og:image") or ""
        if not img.startswith("http"):
            erros.append("sem og:image absoluta")
        if erros:
            falhas.append(f"{u}: " + ", ".join(erros))
        else:
            provas.append(f"{u}: ok (título {len(t)}, descrição {len(d)})")
    resultados.append(item(8, not falhas and bool(provas), provas[:4], falhas))

    # 9. medição e buscadores
    provas, falhas, nao_verificado = medicao(site, home_resp, cache, (extras or {}).get("navegador", navegador))
    buscadores = (extras or {}).get("buscadores") or consultar_buscadores(site, robots["sitemaps"] or [base + "/sitemap.xml"])
    for nome in ("gsc", "bing"):
        b = buscadores[nome]
        if b["ok"] is None:
            nao_verificado.append(f"{nome}: {b['motivo']}")
        else:
            (provas if b["ok"] else falhas).append(f"{nome}: {b['motivo']}")
    ok9 = False if falhas else (None if nao_verificado else True)
    resultados.append(item(9, ok9, provas + nao_verificado, falhas))

    # 10. IndexNow
    inx = p["indexnow"]
    if not inx:
        resultados.append(item(10, False, [], ["sem chave IndexNow no cadastro (padrao.indexnow)"]))
    else:
        r = cache.get(f"{base}/{inx['chave']}.txt")
        provas, falhas = [], []
        if r.status == 200 and r.corpo.strip() == inx["chave"]:
            provas.append(f"{base}/{inx['chave']}.txt → 200, conteúdo confere")
        else:
            falhas.append(f"{base}/{inx['chave']}.txt → {r.status or r.erro}" + ("" if r.status != 200 else ", conteúdo não confere"))
        codigo = (extras or {}).get("indexnow_codigo") or aviso_indexnow_no_codigo(site)
        if codigo["aviso"] == "publicacao":
            provas += codigo["provas"]
            ok10 = not falhas
        elif codigo.get("erro"):
            provas.append(f"código não conferido: {codigo['erro']}"
                          + (f" (o cadastro declara aviso {inx['aviso']}: {inx.get('onde')})" if inx.get("aviso") else ""))
            ok10 = False if falhas else None
        else:
            falhas.append(f"aviso {codigo['aviso'] or 'ausente'} no código de {codigo['onde']}: precisa avisar na publicação")
            provas += codigo["provas"]
            ok10 = False
        resultados.append(item(10, ok10, provas, falhas))
    return resultados


# ---------- 9. medição (GA4 e Clarity) ----------

HOSTS_MEDICAO = {"GA4": ("googletagmanager.com", "google-analytics.com", "analytics.google.com"), "Clarity": ("clarity.ms",)}
RE_CONSENTIMENTO = re.compile(r"consent|consentimento|aceit[aeo]|opt[-_]?in", re.I)
JANELA_MODULO = 2500  # caracteres em volta do ID no chunk: o módulo que carrega a tag


def achar_node():
    for c in (shutil.which("node"), Path.home() / ".local/node/bin/node", "/home/nimbus/.local/node/bin/node"):
        if c and Path(c).is_file():
            return str(c)
    return None


def navegador(url, cookie=None, espera=8000):
    """Abre a URL no Chrome headless sem cookies (navegador.mjs). None se não há Node/Chrome; {'erro'} se falhar."""
    node = achar_node()
    if not node:
        return None
    cmd = [node, str(RAIZ / "navegador.mjs"), url, "--espera", str(espera)] + (["--cookie", cookie] if cookie else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired:
        return {"erro": "navegador demorou demais"}
    if r.returncode == 3:
        return None  # sem Chrome
    if r.returncode != 0:
        return {"erro": (r.stderr or r.stdout).strip()[-200:] or f"código {r.returncode}"}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"erro": "saída do navegador não é JSON"}


def carregou(res, rotulo, valor):
    """Requisições da página para a ferramenta com este ID (gtag/js?id=G-…, collect?tid=G-…, clarity.ms/tag/…)."""
    saida = []
    for u in res.get("requisicoes", []):
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
        if valor.lower() in u.lower() and any(host == h or host.endswith("." + h) for h in HOSTS_MEDICAO[rotulo]):
            saida.append(u)
    return saida


def medicao(site, home_resp, cache, abrir):
    """(provas, falhas, nao_verificado) do GA4 e do Clarity.

    Critério comportamental: a home aberta num navegador sem cookies carrega as duas tags e, com o cookie de "não
    medir" (padrao.cookie_nao_medir, padrão nao_medir; valor 1), não carrega nenhuma. Sem navegador aqui, cai para a
    leitura estática: ID no HTML, ou no chunk JS do próprio site desde que o módulo não dependa de consentimento."""
    base = f"https://{site['dominio']}"
    ferramentas = [(r, v) for r, v in (("GA4", site["ids"].get("ga4_medicao")), ("Clarity", site["ids"].get("clarity"))) if v]
    provas, falhas = [], []
    if not site["ids"].get("ga4_medicao"):
        falhas.append("sem ids.ga4_medicao no cadastro")
    if not ferramentas:
        return provas, falhas, []
    cookie = (site["padrao"].get("cookie_nao_medir") or "nao_medir") + "=1"
    sem = abrir(base + "/", None)
    com = abrir(base + "/", cookie) if sem and not sem.get("erro") else None
    if sem and not sem.get("erro") and com and not com.get("erro"):
        for rotulo, valor in ferramentas:
            achou = carregou(sem, rotulo, valor)
            if achou:
                provas.append(f"{rotulo} {valor} carrega por padrão (navegador sem cookies): {achou[0][:120]}")
            else:
                falhas.append(f"{rotulo} {valor} não carregou na home aberta num navegador sem cookies")
            vazou = carregou(com, rotulo, valor)
            if vazou:
                falhas.append(f"{rotulo} {valor} carrega mesmo com o cookie {cookie}: {vazou[0][:120]}")
            else:
                provas.append(f"{rotulo} não carrega com o cookie {cookie}")
        g = sem.get("globais") or {}
        provas.append(f"sem cookies: window.gtag {'existe' if g.get('gtag') else 'ausente'}, window.clarity "
                      f"{'existe' if g.get('clarity') else 'ausente'}; cookies gravados: {', '.join(sem.get('cookies') or []) or 'nenhum'}")
        return provas, falhas, []
    erro = (sem or {}).get("erro") or (com or {}).get("erro")
    p2, f2 = medicao_estatica(site, home_resp, cache, ferramentas)
    aviso = f"teste no navegador não rodou ({f'falhou: {erro}' if erro else 'sem Node/Chrome aqui'}): opt-out pelo cookie não conferido"
    return provas + p2 + [aviso], falhas + f2, []


def medicao_estatica(site, home_resp, cache, ferramentas):
    """Plano B sem navegador: ID no HTML da home, ou num chunk JS do próprio site sem consentimento no mesmo módulo."""
    base = f"https://{site['dominio']}"
    corpo = home_resp.corpo or ""
    pagina = Pagina(corpo)
    provas, falhas = [], []
    for rotulo, valor in ferramentas:
        if valor in corpo:
            provas.append(f"{rotulo} {valor} no HTML da home (carrega por padrão)")
            continue
        achado = None
        for src in pagina.scripts_src[:40]:
            url = urllib.parse.urljoin(base + "/", src)
            if urllib.parse.urlsplit(url).hostname != site["dominio"]:
                continue
            js = cache.get(url).corpo or ""
            i = js.find(valor)
            if i >= 0:
                achado = (url, RE_CONSENTIMENTO.search(js[max(0, i - JANELA_MODULO): i + JANELA_MODULO]))
                if not achado[1]:
                    break
        if achado and not achado[1]:
            provas.append(f"{rotulo} {valor}: ok (JS), no chunk {achado[0]}, sem consentimento no mesmo módulo")
        elif achado:
            falhas.append(f"{rotulo} {valor} só no JavaScript {achado[0]}, junto de {achado[1].group(0)!r}: pode esperar consentimento")
        else:
            falhas.append(f"{rotulo} {valor} nem no HTML nem nos scripts da home")
    return provas, falhas


# ---------- 10. IndexNow na publicação ----------

RE_ENDPOINT_INDEXNOW = re.compile(
    r"https?://(?:api\.indexnow\.org|(?:www\.)?bing\.com|yandex\.(?:com|ru)|search\.seznam\.cz|searchadvisor\.naver\.com"
    r"|indexnow\.yep\.com|indexnow\.amazonbot\.amazon)/indexnow", re.I)
RE_TESTE = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|\.(test|teste|spec)\.[a-z]+$", re.I)
RE_CODIGO = re.compile(r"\.(ts|tsx|mts|cts|js|jsx|mjs|cjs|py|rb|php|go|sh)$", re.I)
RE_SCRIPT = re.compile(r"(^|/)(scripts?|bin|tools)/", re.I)
RE_EXPORTADA = re.compile(r"export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"
                          r"|export\s+const\s+(\w+)\s*=\s*(?:async\b|\()|^(?:async\s+)?def\s+(\w+)", re.M)
EVENTOS_PUBLICACAO = {"push", "release", "deployment", "deployment_status", "workflow_run", "repository_dispatch",
                      "page_build", "registry_package"}
LIFECYCLE_PUBLICACAO = {"postbuild", "deploy", "postdeploy", "release", "postrelease", "publish", "postpublish",
                        "start", "prestart", "poststart"}
ORDEM_AVISO = {"publicacao": 3, "agendado": 2, "manual": 1}


def eventos_workflow(texto):
    """Gatilhos do `on:` de um workflow do GitHub Actions (leitura do texto, sem YAML)."""
    linhas = texto.splitlines()
    for i, linha in enumerate(linhas):
        m = re.match(r"""^(?:on|"on"|'on'|true)\s*:\s*(.*)$""", linha)
        if not m:
            continue
        resto = m.group(1).split("#", 1)[0].strip()
        if resto:
            return {e.strip(" '\"") for e in resto.strip("[]").split(",") if e.strip()}
        eventos, recuo = set(), None
        for prox in linhas[i + 1:]:
            if not prox.strip() or prox.lstrip().startswith("#"):
                continue
            nivel = len(prox) - len(prox.lstrip())
            if nivel == 0:
                break
            recuo = recuo or nivel
            if nivel == recuo:
                eventos.add(prox.strip().split(":", 1)[0].strip(" -'\""))
        return eventos
    return set()


def _modulo(caminho):
    return re.sub(r"\.[a-z]+$", "", caminho, flags=re.I)


def classificar_indexnow(arquivos):
    """Onde o código avisa o IndexNow. `arquivos`: {caminho: texto} do branch base (os que citam IndexNow e os que
    chamam as funções do emissor). Devolve {'aviso': publicacao|agendado|manual|None, 'provas': [...]}.

    Emissor: arquivo de código, fora de teste, que faz a requisição ao endpoint do IndexNow. Avisa na publicação:
    código do app (fora de scripts/ e de teste) que importa o emissor e chama uma função exportada dele; workflow que
    roda o IndexNow em push/release/deployment; script do IndexNow num lifecycle de publicação do package.json ou no
    Dockerfile/entrypoint. Workflow só com schedule é agendado; script avulso ou workflow_dispatch é manual."""
    emissores = {c: t for c, t in arquivos.items() if RE_CODIGO.search(c) and not RE_TESTE.search(c) and RE_ENDPOINT_INDEXNOW.search(t)}
    achados = []  # (aviso, prova)
    for emissor, texto in emissores.items():
        funcoes = sorted({n for m in RE_EXPORTADA.finditer(texto) for n in m.groups() if n})
        mod = _modulo(emissor)
        sufixos = {mod, mod.split("/", 1)[-1], "/".join(mod.split("/")[-2:])}
        for c, t in arquivos.items():
            if c == emissor or RE_TESTE.search(c) or not RE_CODIGO.search(c):
                continue
            importa = any(re.search(r"""['"][^'"\n]*""" + re.escape(s) + r"""(?:\.[a-z]+)?['"]""", t) for s in sufixos)
            chama = next((f for f in funcoes if re.search(r"\b" + re.escape(f) + r"\s*\(", t)), None)
            if importa and chama:
                if RE_SCRIPT.search(c):
                    achados.append(("manual", f"{c} chama {chama} ({emissor}): script avulso"))
                else:
                    achados.append(("publicacao", f"{c} chama {chama} ({emissor}): no fluxo de publicação do app"))
        if RE_SCRIPT.search(emissor):
            achados.append(("manual", f"{emissor} faz a requisição ao IndexNow: script avulso"))
    for c, t in arquivos.items():
        if c.rsplit("/", 1)[-1] == "package.json":
            try:
                cmds = json.loads(t).get("scripts") or {}
            except (ValueError, AttributeError):
                continue
            for nome, cmd in cmds.items():
                if "indexnow" in str(cmd).lower():
                    publica = nome in LIFECYCLE_PUBLICACAO
                    achados.append(("publicacao" if publica else "manual",
                                    f"{c}: script \"{nome}\" roda o IndexNow" + (" (lifecycle de publicação)" if publica else "")))
        elif re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", c) and re.search(r"indexnow", t, re.I):
            eventos = eventos_workflow(t)
            aviso = "publicacao" if eventos & EVENTOS_PUBLICACAO else ("agendado" if "schedule" in eventos else "manual")
            achados.append((aviso, f"{c}: workflow em {', '.join(sorted(eventos)) or '?'}"))
        elif re.search(r"(^|/)(Dockerfile|[\w-]*entrypoint\.sh|Procfile)$", c) and re.search(r"indexnow", t, re.I):
            achados.append(("publicacao", f"{c}: roda o IndexNow quando o app sobe"))
    if not achados:
        return {"aviso": None, "provas": [f"{', '.join(sorted(emissores))} existe, mas nada o chama" if emissores
                                          else "nenhuma chamada ao endpoint do IndexNow no código"]}
    achados.sort(key=lambda a: -ORDEM_AVISO[a[0]])
    return {"aviso": achados[0][0], "provas": [p for _, p in achados]}


def aviso_indexnow_no_codigo(site, repo=None):
    """git fetch do branch base do origin e classificar_indexnow. Sem acesso: {'aviso': None, 'erro': ...}."""
    from hub import repos
    r = site.get("repo") or {}
    onde = f"{r.get('github')}@{r.get('base')}"
    if not r.get("github"):
        return {"aviso": None, "erro": "site sem repo no cadastro", "provas": [], "onde": onde}
    try:
        repo = (repo or repos.Repo(r["github"], r["base"])).buscar()
        caminhos = set(repo.grep("indexnow", ignorar_caixa=True))
        arquivos = {c: repo.ler(c) for c in caminhos}
        funcoes = {n for c, t in arquivos.items() if RE_CODIGO.search(c) and RE_ENDPOINT_INDEXNOW.search(t)
                   for m in RE_EXPORTADA.finditer(t) for n in m.groups() if n}
        if funcoes:
            for c in set(repo.grep(*sorted(funcoes), palavra=True)) - caminhos:
                arquivos[c] = repo.ler(c)
    except repos.SemAcesso as e:
        return {"aviso": None, "erro": str(e), "provas": [], "onde": onde}
    saida = classificar_indexnow(arquivos)
    saida["onde"] = f"{onde} ({(repo.sha or '')[:7]})"
    saida["provas"] = [f"{x} [{r['github']}@{(repo.sha or '')[:7]}]" for x in saida["provas"]]
    return saida


def consultar_buscadores(site, sitemaps_declarados):
    """GSC (conta de serviço) e Bing (chave de API): propriedade verificada e sitemap enviado."""
    from hub import google
    alvo_sitemaps = {s.rstrip("/") for s in sitemaps_declarados}
    saida = {}
    prop = site["ids"].get("gsc")
    try:
        if not prop:
            saida["gsc"] = {"ok": False, "motivo": "sem propriedade no cadastro"}
        else:
            entrada = next((e for e in google.propriedades() if e["siteUrl"] == prop), None)
            if not entrada or entrada.get("permissionLevel") == "siteUnverifiedUser":
                saida["gsc"] = {"ok": False, "motivo": f"{prop} não verificada para a conta de serviço"}
            else:
                enviados = {s["path"].rstrip("/") for s in google.sitemaps(prop)}
                achados = enviados & alvo_sitemaps
                saida["gsc"] = {"ok": bool(achados), "motivo": f"{prop} verificada ({entrada['permissionLevel']}); "
                                + (f"sitemap enviado: {', '.join(sorted(achados))}" if achados
                                   else f"sitemap do robots não enviado (enviados: {', '.join(sorted(enviados)) or 'nenhum'})")}
    except (FileNotFoundError, google.ErroCredencial):
        saida["gsc"] = {"ok": None, "motivo": "sem credencial da conta de serviço aqui"}
    except Exception as e:
        saida["gsc"] = {"ok": None, "motivo": f"consulta falhou: {type(e).__name__}"}
    try:
        chave = json.loads((home() / "credenciais/bing.json").read_text(encoding="utf-8"))["chave"]
        api = "https://ssl.bing.com/webmaster/api.svc/json/"
        with urllib.request.urlopen(f"{api}GetUserSites?apikey={chave}", timeout=40) as r:
            lista = json.load(r).get("d") or []
        s = next((x for x in lista if (urllib.parse.urlsplit(x.get("Url", "")).hostname or "").lower() == site["dominio"]), None)
        if not s or not s.get("IsVerified"):
            saida["bing"] = {"ok": False, "motivo": "site ausente ou não verificado no Bing"}
        else:
            q = urllib.parse.urlencode({"siteUrl": s["Url"], "apikey": chave})
            with urllib.request.urlopen(f"{api}GetFeeds?{q}", timeout=40) as r:
                feeds = {f.get("Url", "").rstrip("/") for f in (json.load(r).get("d") or [])}
            achados = feeds & alvo_sitemaps
            saida["bing"] = {"ok": bool(achados), "motivo": f"{s['Url']} verificado; "
                             + (f"sitemap enviado: {', '.join(sorted(achados))}" if achados
                                else f"sitemap do robots não enviado (enviados: {', '.join(sorted(feeds)) or 'nenhum'})")}
    except FileNotFoundError:
        saida["bing"] = {"ok": None, "motivo": "sem chave do Bing aqui"}
    except Exception as e:
        saida["bing"] = {"ok": None, "motivo": f"consulta falhou: {type(e).__name__}"}
    return saida


# ---------- relatório ----------

def pioras(anterior, atual):
    """Itens que estavam ok e agora faltam (null não conta: só o que foi verificado e caiu)."""
    antes = {i["n"]: i["ok"] for i in (anterior or {}).get("itens", [])}
    return [i for i in atual["itens"] if antes.get(i["n"]) is True and i["ok"] is False]


def markdown(site, r):
    ok = sum(i["ok"] is True for i in r["itens"])
    linhas = [f"# Padrão de SEO · {site['nome']} · {r['data']}", "",
              f"https://{site['dominio']} · {ok} de {len(r['itens'])} itens ok. Padrão em PADRAO-SEO.md (nimbus-hub).", ""]
    for i in r["itens"]:
        marca = {True: "ok", False: "FALTA", None: "não verificado"}[i["ok"]]
        linhas.append(f"## {i['n']}. {i['item']}: {marca}")
        linhas += [f"- falta: {f}" for f in i["falhas"]] + [f"- prova: {p}" for p in i["provas"]] + [""]
    if r.get("pioras"):
        linhas += ["## Piorou desde a última verificação", ""] + [f"- {i['n']}. {i['item']}" for i in r["pioras"]] + [""]
    return "\n".join(linhas)


def rodar(site, publicar=False, hoje=None, saida=None):
    hoje = hoje or dt.date.today()
    itens = verificar(site, hoje=hoje)
    pasta = Path(saida) if saida else data() / "verificador" / site["id"]
    pasta.mkdir(parents=True, exist_ok=True)
    anterior_p = pasta / "latest.json"
    anterior = json.loads(anterior_p.read_text(encoding="utf-8")) if anterior_p.exists() else None
    r = {"site": site["id"], "dominio": site["dominio"], "data": hoje.isoformat(),
         "em": dt.datetime.now(dt.timezone.utc).isoformat(), "itens": itens}
    r["pioras"] = pioras(anterior, r)
    md = markdown(site, r)
    for nome, conteudo in ((f"{hoje.isoformat()}.json", json.dumps(r, ensure_ascii=False, indent=1)), (f"{hoje.isoformat()}.md", md),
                           ("latest.json", json.dumps(r, ensure_ascii=False, indent=1)), ("latest.md", md)):
        tmp = pasta / (nome + ".tmp")
        tmp.write_text(conteudo, encoding="utf-8")
        tmp.replace(pasta / nome)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site", nargs="?")
    ap.add_argument("--todos", action="store_true", help="todos os sites ativos")
    ap.add_argument("--publicar", action="store_true", help=argparse.SUPPRESS)  # aceito e ignorado: quem chama com a opção não quebra
    ap.add_argument("--avisar", action="store_true", help="WhatsApp só com o que piorou")
    ap.add_argument("--saida", help="pasta de saída (um site só), no lugar de $HUB_DATA/verificador/<site>")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.todos == bool(args.site):
        ap.error("informe um site ou --todos")
    if args.saida and args.todos:
        ap.error("--saida vale para um site só")
    if args.site:
        s = sites.site(args.site)
        if s["padrao"]["fora_do_google"]:
            print(f"{s['id']} fica fora do Google de propósito (noindex intencional): não é verificado.")
            return 0
        alvo = [s]
    else:
        alvo = sites.ativos()
    pioras_todas, falhas = [], 0
    for s in alvo:
        try:
            r = rodar(s, publicar=args.publicar, saida=args.saida)
        except Exception as e:
            falhas += 1
            print(f"ERRO {s['id']}: {type(e).__name__}: {str(e)[:200]}")
            continue
        ok = sum(i["ok"] is True for i in r["itens"])
        print(f"{s['id']}: {ok}/{len(r['itens'])} ok · falta: " + (", ".join(str(i["n"]) for i in r["itens"] if i["ok"] is False) or "nada")
              + (" · não verificado: " + ", ".join(str(i["n"]) for i in r["itens"] if i["ok"] is None) if any(i["ok"] is None for i in r["itens"]) else ""))
        pioras_todas += [f"{s['nome']}: {i['n']}. {i['item']} ({'; '.join(i['falhas'][:2])})" for i in r["pioras"]]
    if args.avisar and pioras_todas:
        texto = "Padrão de SEO piorou:\n" + "\n".join(f"- {x}" for x in pioras_todas) + f"\n\nDetalhes em {data()}/verificador/<site>/latest.md"
        subprocess.run([str(RAIZ / "deploy/avisar.sh"), texto[:3500]], check=False)
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
