"""Oportunidades no Search Console: buracos e vazamentos, por site, nos últimos 90 dias (consulta × página).

- Buraco: a busca já tem demanda e o Google já mostra o site, mas sem página feita para ela.
  Impressões ≥ 50, posição média > 10, zero clique e nenhuma página do site com a busca no título ou na URL
  (tolera plural, erro de digitação e prefixo: "miofacial" casa "miofascial", "fsite05" casa "fisioterapia").
- Reforçar: mesmo filtro, mas o Google já mostra uma página que cobre ao menos metade dos termos. Não falta página;
  falta a página subir (título, conteúdo, links internos).
Busca com nome de concorrente (sites/<id>.json "concorrentes") não entra: não é demanda para página nova.
- Vazamento: o site já está na primeira página, mas o resultado não convence.
  Posição ≤ 10, impressões ≥ 200 e CTR abaixo da metade do esperado para a posição (CURVA_CTR).
Ordem: vazamentos primeiro (mexer no título/descrição de página que já ranqueia é o ganho mais rápido),
depois buracos; dentro de cada grupo, por impressões.

    python gsc_oportunidades.py [--site fsite05] [--dias 90]

Saída: $HUB_DATA/gsc/<site>/<AAAA-MM-DD>.json e .md, e latest.json/latest.md (lidos pela tarefa seo-semanal).
Limitação: o relatório de desempenho em IA do Search Console (AI Overviews/AI Mode, jun/2026) não sai pela API
(searchAnalytics só aceita web, image, video, news, discover e googleNews); fica de fora, sem raspagem de painel.
"""
import argparse
import datetime as dt
import difflib
import html
import json
import re
import sys
import urllib.request
from urllib.parse import unquote, urlsplit

from hub import citacao, data, google, janelas, sites

MIN_IMPRESSOES_BURACO = 50
MIN_IMPRESSOES_VAZAMENTO = 200
FRACAO_DA_CURVA = 0.5

# CTR orgânico esperado por posição (fração de cliques por impressão), posições 1 a 10.
# Curva do hub, conservadora: segue a forma dos estudos públicos de CTR orgânico no Google (Advanced Web Ranking,
# Backlinko, First Page Sage: queda forte do 1º ao 3º e cauda longa até o 10º), rebaixada porque em 2026 AI Overview,
# anúncios e blocos de mapa tiram clique do resultado orgânico. Não é medida do site; é a régua para achar o que destoa.
# Entre posições inteiras, interpolação linear (posição média 2,4 → 60% do caminho de 2 para 3).
CURVA_CTR = {1: 0.25, 2: 0.14, 3: 0.10, 4: 0.07, 5: 0.055, 6: 0.045, 7: 0.035, 8: 0.03, 9: 0.025, 10: 0.02}

PALAVRAS_VAZIAS = set("""a o as os um uma uns umas de da do das dos em no na nos nas para pra por com sem e ou que
qual quais quanto quanta quantos quantas como onde quando se me meu minha the of and for in to é ao aos à às""".split())


def ctr_esperado(posicao):
    if posicao <= 1:
        return CURVA_CTR[1]
    if posicao >= 10:
        return CURVA_CTR[10]
    baixo = int(posicao)
    frac = posicao - baixo
    return CURVA_CTR[baixo] + (CURVA_CTR[baixo + 1] - CURVA_CTR[baixo]) * frac


def palavras(texto):
    return [p for p in re.split(r"[^a-z0-9]+", citacao.normalizar(unquote(texto or ""))) if p]


def termos_da_busca(busca):
    return [p for p in palavras(busca) if p not in PALAVRAS_VAZIAS and (len(p) >= 3 or p.isdigit())]


def _casa(termo, conjunto):
    if termo in conjunto:
        return True
    if len(termo) < 5:
        return False
    raiz = termo[:-2]  # tolera plural e flexão curta: fisioterapia/fisioterapias, lombar/lombares
    return any(len(w) >= 5 and (w.startswith(raiz) and abs(len(w) - len(termo)) <= 2  # flexão
                                or w.startswith(termo)  # prefixo: fsite05 → fisioterapia
                                or len(termo) >= 6 and difflib.SequenceMatcher(None, termo, w).ratio() >= 0.85)  # digitação
               for w in conjunto)


def fracao_coberta(busca, url=None, titulo=None):
    """Fração dos termos da busca que aparece no caminho da URL ou no título (o melhor dos dois)."""
    termos = termos_da_busca(busca)
    if not termos:
        return 0.0
    melhor = 0.0
    for texto in (urlsplit(url).path if url else None, titulo):
        if texto:
            conjunto = set(palavras(texto))
            melhor = max(melhor, sum(_casa(t, conjunto) for t in termos) / len(termos))
    return melhor


def cobre(busca, url=None, titulo=None):
    """A página foi feita para a busca? Todos os termos da busca aparecem no caminho da URL ou no título."""
    return fracao_coberta(busca, url, titulo) == 1.0


def de_concorrente(busca, concorrentes):
    """A busca traz o nome de um concorrente (todas as palavras do nome, tolerando digitação)?"""
    termos = set(palavras(busca))
    nomes = ([p for p in palavras(c) if p not in PALAVRAS_VAZIAS] for c in concorrentes)
    return any(nome and all(t in termos or _casa(t, termos) for t in nome) for nome in nomes)


def classificar(linhas, titulos=None, urls_site=(), concorrentes=()):
    """linhas: [{keys: [consulta, página], clicks, impressions, ctr, position}] do GSC (query × page).
    titulos: {url: título} das páginas conhecidas. urls_site: URLs do sitemap (para o teste de URL).
    concorrentes: nomes de concorrentes; busca com um deles fica de fora.
    Devolve a lista ordenada de oportunidades."""
    titulos = titulos or {}
    caminhos_site = [(u, set(palavras(urlsplit(u).path))) for u in urls_site]  # uma vez só: o sitemap pode ter milhares
    por_busca = {}
    for l in linhas:
        busca, pagina = l["keys"][0], l["keys"][1]
        b = por_busca.setdefault(busca, {"impressoes": 0, "cliques": 0, "soma_pos": 0.0, "paginas": []})
        b["impressoes"] += l["impressions"]
        b["cliques"] += l["clicks"]
        b["soma_pos"] += l["position"] * l["impressions"]
        b["paginas"].append(pagina)
    saida = []
    for l in linhas:  # vazamento: por consulta × página (a página que ranqueia e não leva clique)
        busca, pagina = l["keys"][0], l["keys"][1]
        pos, imp = l["position"], l["impressions"]
        ctr = l["clicks"] / imp if imp else 0.0
        esperado = ctr_esperado(pos)
        if pos <= 10 and imp >= MIN_IMPRESSOES_VAZAMENTO and ctr < esperado * FRACAO_DA_CURVA:
            saida.append({"tipo": "vazamento", "busca": busca, "pagina": pagina, "impressoes": imp, "cliques": l["clicks"],
                          "posicao": round(pos, 1), "ctr": round(ctr, 4), "ctr_esperado": round(esperado, 4),
                          "cliques_perdidos": round(imp * esperado - l["clicks"])})
    for busca, b in por_busca.items():  # buraco: pela consulta inteira (todas as páginas somadas)
        pos = b["soma_pos"] / b["impressoes"] if b["impressoes"] else 0.0
        if b["impressoes"] < MIN_IMPRESSOES_BURACO or pos <= 10 or b["cliques"] > 0 or de_concorrente(busca, concorrentes):
            continue
        termos = termos_da_busca(busca)
        paginas = sorted(set(b["paginas"]))
        fracao, melhor = max((fracao_coberta(busca, u, titulos.get(u)), u) for u in paginas)
        if fracao < 1 and termos:  # página do sitemap feita para a busca, mesmo que o Google mostre outra
            feita = next((u for u, c in caminhos_site if all(_casa(t, c) for t in termos)), None)
            if feita:
                fracao, melhor = 1.0, feita
        base = {"busca": busca, "paginas_que_aparecem": paginas[:5], "impressoes": b["impressoes"], "cliques": 0,
                "posicao": round(pos, 1)}
        if fracao >= 0.5:  # a página existe: falta subir, não falta página
            saida.append({"tipo": "reforcar", "pagina": melhor, **base})
        else:
            saida.append({"tipo": "buraco", "pagina": None, **base})
    ordem = {"vazamento": 0, "reforcar": 1, "buraco": 2}
    return sorted(saida, key=lambda o: (ordem[o["tipo"]], -o["impressoes"]))


def _baixar(url, limite=800_000):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NimbusHub/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limite).decode("utf-8", "ignore")


def urls_do_sitemap(dominio, maximo=5000):
    """URLs do sitemap (segue um nível de índice). Falha de rede devolve o que conseguiu."""
    urls, fila, vistos = [], [f"https://{dominio}/sitemap.xml"], set()
    while fila and len(urls) < maximo:
        alvo = fila.pop(0)
        if alvo in vistos:
            continue
        vistos.add(alvo)
        try:
            xml = _baixar(alvo, 20_000_000)
        except Exception:
            continue
        locs = [html.unescape(x.strip()) for x in re.findall(r"<loc>([^<]+)</loc>", xml)]
        if "<sitemapindex" in xml:
            fila += locs[:50]
        else:
            urls += locs
    return urls[:maximo]


def titulo_de(url):
    try:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", _baixar(url))
        return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else None
    except Exception:
        return None


def markdown(site, resultado):
    ops = resultado["oportunidades"]
    vaz = [o for o in ops if o["tipo"] == "vazamento"]
    bur = [o for o in ops if o["tipo"] == "buraco"]
    ref = [o for o in ops if o["tipo"] == "reforcar"]
    linhas = [f"# Oportunidades no Search Console · {site['nome']} · {resultado['data']}", "",
              f"Período {resultado['periodo']} (90 dias, consulta × página). {len(vaz)} vazamentos, {len(ref)} para reforçar, "
              f"{len(bur)} buracos.",
              "Critérios e curva de CTR: gsc_oportunidades.py. Antes de mexer numa página, conferir a janela de medição.", ""]
    if vaz:
        linhas += ["## Vazamentos (já na 1ª página, CTR abaixo da metade do esperado)", "",
                   "| Busca | Página | Impressões | Posição | CTR | Esperado | Janela |", "|---|---|---|---|---|---|---|"]
        for o in vaz[:40]:
            j = o.get("janela")
            linhas.append(f"| {o['busca']} | {o['pagina']} | {o['impressoes']} | {o['posicao']} | {o['ctr']:.1%} | "
                          f"{o['ctr_esperado']:.1%} | {'aberta até ' + j['julgar_em'] if j else 'livre'} |")
        linhas.append("")
    if ref:
        linhas += ["## Reforçar (a página existe, mas está fora da 1ª página)", "",
                   "| Busca | Página | Impressões | Posição |", "|---|---|---|---|"]
        linhas += [f"| {o['busca']} | {o['pagina']} | {o['impressoes']} | {o['posicao']} |" for o in ref[:40]]
        linhas.append("")
    if bur:
        linhas += ["## Buracos (demanda sem página feita para ela)", "",
                   "| Busca | Impressões | Posição | Onde aparece hoje |", "|---|---|---|---|"]
        linhas += [f"| {o['busca']} | {o['impressoes']} | {o['posicao']} | {', '.join(o['paginas_que_aparecem'][:2])} |" for o in bur[:40]]
        linhas.append("")
    if not ops:
        linhas += ["Nenhuma oportunidade pelos critérios. Isso não quer dizer que não há o que fazer; quer dizer que o GSC não aponta nada óbvio.", ""]
    linhas += ["Limitação: o relatório de desempenho em IA do Search Console não sai pela API; não está aqui.", ""]
    return "\n".join(linhas)


def rodar(site, dias, hoje=None):
    hoje = hoje or dt.date.today()
    r = google.consultas(site["ids"]["gsc"], dias)
    titulos, urls_site = {}, urls_do_sitemap(site["dominio"])
    concorrentes = site.get("concorrentes") or []
    previa = classificar(r["linhas"], {}, urls_site, concorrentes)
    # Só busca título das páginas dos candidatos (o teste de URL já descartou o resto).
    candidatas = {p for o in previa if o["tipo"] != "vazamento" for p in o["paginas_que_aparecem"]}
    for url in sorted(candidatas)[:80]:
        titulos[url] = titulo_de(url)
    ops = classificar(r["linhas"], titulos, urls_site, concorrentes)
    for o in ops:
        if o["pagina"]:
            o["janela"] = janelas.pode_mexer(o["pagina"], site["dominio"], hoje)
    resultado = {"site": site["id"], "data": hoje.isoformat(), "periodo": f"{r['inicio']} a {r['fim']}", "linhas_gsc": len(r["linhas"]),
                 "criterios": {"buraco_min_impressoes": MIN_IMPRESSOES_BURACO, "vazamento_min_impressoes": MIN_IMPRESSOES_VAZAMENTO,
                               "fracao_da_curva": FRACAO_DA_CURVA, "curva_ctr": CURVA_CTR},
                 "oportunidades": ops}
    pasta = data() / "gsc" / site["id"]
    pasta.mkdir(parents=True, exist_ok=True)
    md = markdown(site, resultado)
    for nome, conteudo in ((f"{hoje.isoformat()}.json", json.dumps(resultado, ensure_ascii=False, indent=1)),
                           (f"{hoje.isoformat()}.md", md), ("latest.json", json.dumps(resultado, ensure_ascii=False, indent=1)),
                           ("latest.md", md)):
        tmp = pasta / (nome + ".tmp")
        tmp.write_text(conteudo, encoding="utf-8")
        tmp.replace(pasta / nome)
    return resultado


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site")
    ap.add_argument("--dias", type=int, default=90)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    alvo = [s for s in sites.ativos() if s["ids"].get("gsc") and (not args.site or s["id"] == args.site)]
    if args.site and not alvo:
        print(f"site {args.site!r} não está ativo ou não tem GSC", file=sys.stderr)
        return 1
    falhas = 0
    for s in alvo:
        try:
            r = rodar(s, args.dias)
            n = {t: sum(o["tipo"] == t for o in r["oportunidades"]) for t in ("vazamento", "reforcar", "buraco")}
            print(f"{s['id']}: {r['linhas_gsc']} linhas · {n['vazamento']} vazamentos · {n['reforcar']} para reforçar · "
                  f"{n['buraco']} buracos")
        except google.SemAcesso as e:
            falhas += 1
            print(f"{s['id']}: {e}")
        except Exception as e:
            falhas += 1
            print(f"ERRO {s['id']}: {type(e).__name__}: {str(e)[:200]}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
