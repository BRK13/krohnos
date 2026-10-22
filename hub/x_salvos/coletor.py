"""Coletor dos salvos (bookmarks) do X, pela sessão do próprio usuário (cookies exportados), sem API paga.

Porta do coletor original, parametrizado pela Config e com os guardrails em código. Grava os novos em
<saida>/AAAA-MM-DD.jsonl e os ids vistos em <saida>/vistos.json. Sessão expirada ou sem cookies: grava ERRO e PARA (código 2),
sem tentar logar nem repetir. `playwright` só é importado dentro de `abrir`.
"""
import datetime as dt
import fcntl
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import guardrails as G
from .guardrails import log

RE_ARTIGO = re.compile(r"x\.com/(?:i|[A-Za-z0-9_]+)/article/\d+")
SELETOR_MAIS_RESPOSTAS = 'button:has-text("Mostrar mais respostas"), button:has-text("Show more replies")'
SEL_TWEET = 'article[data-testid="tweet"]'


# ---------- puro (sem navegador) ----------
def pausa(a=1.5, b=4.5):
    f = 0.15 if os.environ.get("X_RAPIDO") else 1  # modo rápido só para a carga inicial
    time.sleep(random.uniform(a, b) * f)


def gravar_json(p, obj, modo=None):
    p = Path(p)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    if modo:
        os.chmod(tmp, modo)
    os.replace(tmp, p)


def carregar_estado(saida):
    v = Path(saida) / "vistos.json"
    return json.loads(v.read_text()) if v.exists() else {"ids": [], "ultima_coleta": None}


def arquivo_do_dia(saida, hoje):
    return Path(saida) / f"{hoje}.jsonl"


def anexar_jsonl(arq, item):
    with open(arq, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def gravar_erro(saida, msg):
    msg = G.mascarar(msg)
    Path(saida).mkdir(parents=True, exist_ok=True)
    (Path(saida) / "ERRO").write_text(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {msg}\n", encoding="utf-8")
    log(f"ERRO: {msg}")


def avisar_primeira_rodada(est):
    if not est.get("ultima_coleta") and not est.get("ids"):
        log(G.AVISO_TERMOS)


def travar(caminho):
    """flock contra duas coletas na mesma conta. Devolve o arquivo aberto (segure-o) ou None se já há outra."""
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    f = open(caminho, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def expandir(url):
    """t.co -> destino, sem seguir o resto da cadeia (HEAD, só leitura)."""
    if "t.co/" not in url:
        return url
    G.checar_requisicao("HEAD", url)

    class SemRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(SemRedirect).open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": "curl/8"}), timeout=15)
    except urllib.error.HTTPError as e:
        return e.headers.get("Location") or url
    except Exception:  # noqa: BLE001 — link não expandido não derruba a coleta
        pass
    return url


# ---------- navegador ----------
JS_CARDS = r"""
() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const t = [...a.querySelectorAll('a[href*="/status/"]')].find(x => x.querySelector('time'));
  const m = t && t.getAttribute('href').match(/^\/([^/]+)\/status\/(\d+)/);
  return m ? {id: m[2], autor: m[1], url: 'https://x.com' + t.getAttribute('href').replace(/\/(photo|analytics).*$/, ''),
              data: t.querySelector('time').getAttribute('datetime'),
              texto: (a.querySelector('[data-testid="tweetText"]') || {}).innerText || ''} : null;
}).filter(Boolean)
"""

JS_POST = r"""
() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const t = [...a.querySelectorAll('a[href*="/status/"]')].find(x => x.querySelector('time'));
  const m = t && t.getAttribute('href').match(/^\/([^/]+)\/status\/(\d+)/);
  const tx = a.querySelector('[data-testid="tweetText"]');
  return {id: m ? m[2] : null, autor: m ? m[1] : null, data: t ? t.querySelector('time').getAttribute('datetime') : null,
          nome: (a.querySelector('[data-testid="User-Name"]') || {}).innerText || '',
          texto: tx ? tx.innerText : '',
          links: [...a.querySelectorAll('[data-testid="tweetText"] a[href^="http"], [data-testid="card.wrapper"] a[href^="http"], a[href*="/article/"]')]
                   .map(x => x.href).filter(h => !/x\.com\/(hashtag|search)|\/status\//.test(h) || /article/.test(h)),
          imagens: [...a.querySelectorAll('[data-testid="tweetPhoto"] img, [data-testid="card.wrapper"] img')].map(i => i.src),
          citados: [...new Set([...a.querySelectorAll('a[href*="/status/"]')].map(x => (x.getAttribute('href') || '').match(/^\/([^/]+)\/status\/(\d+)/)).filter(Boolean).map(m => 'https://x.com/' + m[1] + '/status/' + m[2]))]};
})
"""


def detalhar(page, item, taxa):
    """Abre o post: texto completo, links expandidos, imagens, fio do autor, citados e artigo."""
    G.ir(page, item["url"], taxa)
    try:
        page.wait_for_selector(SEL_TWEET, timeout=20000)
    except Exception:  # noqa: BLE001
        return item
    pausa(2, 4)
    # o fio inteiro do autor: rola até parar de aparecer post novo (as respostas dele com o link costumam vir abaixo)
    vistos_ids, parado = set(), 0
    for _ in range(25):
        arts = page.evaluate(JS_POST)
        novos_ids = {a["id"] for a in arts} - vistos_ids
        vistos_ids |= {a["id"] for a in arts}
        parado = parado + 1 if not novos_ids else 0
        if parado >= 3:
            break
        G.clicar_seguro(page, SELETOR_MAIS_RESPOSTAS, pausa=lambda: pausa(1, 2))
        page.mouse.wheel(0, random.randint(700, 1200))
        pausa(1, 2)
    arts = page.evaluate(JS_POST)
    i = next((k for k, a in enumerate(arts) if a["id"] == item["id"]), None)
    if i is None:
        return item
    foco = arts[i]
    autor = foco["autor"]
    fio = []
    for k, a in enumerate(arts):  # todo post do autor na conversa, antes e depois do salvo
        if k != i and a["autor"] == autor:
            fio.append({"id": a["id"], "texto": a["texto"], "links": list(dict.fromkeys(expandir(u) for u in a["links"])),
                        "imagens": list(dict.fromkeys(a["imagens"]))})
    links = list(dict.fromkeys([expandir(u) for u in foco["links"]] + [u for f in fio for u in f["links"]]))
    item.update(nome=foco["nome"].split("\n")[0], texto=foco["texto"] or item["texto"], links=links,
                imagens=list(dict.fromkeys(foco["imagens"])), fio=fio)
    proprio = f"/status/{item['id']}"
    citados = [u for u in foco.get("citados", []) if proprio not in u and not any(f"/status/{f['id']}" in u for f in fio)][:2]
    item["citados"] = []
    for u in citados:  # post retuitado/citado: texto, links e artigo dele também
        try:
            G.ir(page, u, taxa)
            page.wait_for_selector(SEL_TWEET, timeout=15000)
            pausa(1, 2)
            c = page.evaluate(JS_POST)[0]
            cl = list(dict.fromkeys(expandir(x) for x in c["links"]))
            item["citados"].append({"url": u, "autor": c["autor"], "texto": c["texto"], "links": cl,
                                    "imagens": list(dict.fromkeys(c["imagens"]))})
            links = list(dict.fromkeys(links + cl))
        except G.SessaoCaiu:
            raise
        except Exception as e:  # noqa: BLE001
            item["citados"].append({"url": u, "erro": G.descrever_erro(e)})
    item["links"] = links
    artigos = [u for u in links if RE_ARTIGO.search(u)][:3]
    if artigos:
        item["artigo"] = {"url": artigos[0], "texto": ler_artigo(page, artigos[0], taxa)}
        item["artigos"] = [item["artigo"]] + [{"url": u, "texto": ler_artigo(page, u, taxa)} for u in artigos[1:]]
    return item


def ler_artigo(page, url, taxa):
    pausa()
    G.ir(page, url, taxa)
    pausa(3, 6)
    for sel in ('[data-testid="twitterArticleRichTextView"]', '[data-testid="twitterArticleReadView"]', 'article', 'main'):
        el = page.query_selector(sel)
        if el and len(el.inner_text()) > 200:
            return el.inner_text()
    return ""


def atualizar_cookies(cfg):
    """Cookies exportados em outra máquina: copia cookies_origem (arquivo, ou pasta com o mesmo nome do arquivo de cookies)
    para cfg.cookies quando a origem é mais nova ou o destino não existe. 600, passa só os três cookies, log sem valor."""
    if not cfg.cookies_origem:
        return False
    origem = Path(cfg.cookies_origem)
    if origem.is_dir():  # na pasta: o mesmo nome do arquivo de cookies, ou x-cookies.json
        origem = next((c for c in (origem / Path(cfg.cookies).name, origem / "x-cookies.json") if c.is_file()), origem / Path(cfg.cookies).name)
    destino = Path(cfg.cookies)
    if not origem.is_file() or (destino.exists() and origem.stat().st_mtime <= destino.stat().st_mtime):
        return False
    try:
        dados = json.loads(origem.read_text(encoding="utf-8"))
    except ValueError:
        raise G.CookiesInvalidos("o arquivo de cookies de origem não é um JSON válido") from None
    except OSError as e:
        raise G.CookiesInvalidos(f"não consegui ler o arquivo de cookies de origem ({type(e).__name__})") from None
    if not isinstance(dados, dict) or any(k.lower() in G.CHAVES_DE_LOGIN for k in dados):
        raise G.CookiesInvalidos("a origem dos cookies deve ser um objeto só com auth_token, ct0 e twid (sem credencial de login)")
    G.gravar_cookies(destino, dados)
    log(f"cookies atualizados a partir de {origem.name}")
    return True


def abrir(pw, cfg):
    """Navegador com UA do Chrome real, cookies da sessão do usuário e a trava de somente leitura instalada."""
    atualizar_cookies(cfg)
    cookies = G.ler_cookies(cfg.cookies)
    try:
        versao = subprocess.run([cfg.chrome, "--version"], capture_output=True, text=True).stdout.split()[-1].split(".")[0]
    except (IndexError, OSError):
        versao = "130"
    ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{versao}.0.0.0 Safari/537.36"
    nav = pw.chromium.launch(executable_path=cfg.chrome, headless=True,
                             args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    ctx = nav.new_context(user_agent=ua, locale="pt-BR", timezone_id="America/Sao_Paulo", viewport={"width": 1366, "height": 900},
                          service_workers="block")  # ctx.route não vê requisição de Service Worker
    G.instalar(ctx)
    if cookies:
        ctx.add_cookies(cookies)
    return nav, ctx, ctx.new_page(), cookies


def salvar_cookies(ctx, cfg):
    """O X roda o ct0; guardar o atual mantém a sessão viva."""
    novos = {c["name"]: c["value"] for c in ctx.cookies("https://x.com") if c["name"] in G.NOMES_COOKIES}
    if novos.get("auth_token"):
        G.gravar_cookies(cfg.cookies, novos)


def refazer_fio(cfg, arq):
    """Relê o fio completo dos salvos já coletados e grava em <arq>-fio.jsonl."""
    from playwright.sync_api import sync_playwright  # só aqui: os testes não precisam do navegador
    itens = [json.loads(linha) for linha in Path(arq).read_text(encoding="utf-8").splitlines() if linha.strip()]
    saida = Path(str(arq).replace(".jsonl", "-fio.jsonl"))
    feitos = {json.loads(linha)["id"] for linha in saida.read_text(encoding="utf-8").splitlines() if linha.strip()} if saida.exists() else set()
    taxa = G.LimiteTaxa(cfg.limite_taxa_s)
    rodada = G.Rodada(cfg.max_paginas, cfg.max_itens)
    with sync_playwright() as pw:
        nav, ctx, page, _ = abrir(pw, cfg)
        try:
            for it in itens:
                if it["id"] in feitos:
                    continue
                try:
                    rodada.item()
                except G.TetoAtingido as e:
                    log(f"teto atingido: {e}")
                    break
                try:
                    it = detalhar(page, dict(it), taxa)
                except G.SessaoCaiu:
                    raise
                except Exception as e:  # noqa: BLE001
                    it["erro"] = G.descrever_erro(e)
                if G.sessao_caiu(page.url):
                    raise G.SessaoCaiu("sessão caiu")
                anexar_jsonl(saida, it)
                log("fio refeito: " + str(it.get("url")) + " (" + str(len(it.get("fio") or [])) + " posts do autor, "
                    + str(len(it.get("links") or [])) + " links)")
            salvar_cookies(ctx, cfg)
        except G.SessaoCaiu:
            gravar_erro(cfg.saida, f"sessão do X caiu: exporte cookies novos para {cfg.cookies}")
            return 2
        finally:
            nav.close()
    return 0


def coletar(cfg, forcar=False):
    """Uma coleta. Devolve o código de saída (0 ok, 2 sessão caída ou sem cookies)."""
    from playwright.sync_api import sync_playwright  # só aqui: os testes não precisam do navegador
    saida = Path(cfg.saida)
    saida.mkdir(parents=True, exist_ok=True)
    hoje = dt.date.today().isoformat()
    est = carregar_estado(saida)
    if not G.pode_coletar(est, hoje, forcar):
        log("já coletou hoje")
        return 0
    avisar_primeira_rodada(est)
    vistos = set(est["ids"])
    taxa = G.LimiteTaxa(cfg.limite_taxa_s)
    rodada = G.Rodada(cfg.max_paginas, cfg.max_itens)
    arq = arquivo_do_dia(saida, hoje)
    novos, caiu = [], False
    with sync_playwright() as p:
        nav, ctx, page, cookies = abrir(p, cfg)
        try:
            G.ir(page, "https://x.com/i/bookmarks", taxa)
            pausa(4, 7)
            if not cookies or G.sessao_caiu(page.url):
                gravar_erro(saida, f"sessão do X expirada ou sem cookies: exporte cookies novos para {cfg.cookies}" if cookies
                            else f"sem cookies: falta {cfg.cookies}")
                return 2
            try:
                page.wait_for_selector(SEL_TWEET, timeout=25000)
            except Exception:  # noqa: BLE001
                if G.sessao_caiu(page.url):
                    gravar_erro(saida, f"sessão do X expirada: exporte cookies novos para {cfg.cookies}")
                    return 2
                log("nenhum salvo na tela (lista vazia ou página não carregou)")
            achados, parado, achou_visto = {}, 0, False
            aba = ctx.new_page()  # posts abertos numa 2a aba: a lista segue rolando e cada salvo já sai para a esteira
            try:
                while parado < 4 and not achou_visto:
                    antes = len(achados)
                    for c in page.evaluate(JS_CARDS):
                        if c["id"] in vistos:
                            if forcar:
                                continue  # carga inicial: pula o já visto e segue rolando
                            achou_visto = True  # os salvos vêm do mais novo pro mais velho
                            break
                        if c["id"] in achados:
                            continue
                        rodada.item()
                        item = dict(c)
                        try:
                            item = detalhar(aba, item, taxa)
                        except G.SessaoCaiu:
                            caiu = True
                            break
                        except Exception as e:  # noqa: BLE001 — um post ruim não perde os outros
                            item["erro"] = G.descrever_erro(e)
                        if G.sessao_caiu(aba.url):
                            caiu = True
                            break
                        novos.append(item)
                        anexar_jsonl(arq, item)  # esteira: grava na hora, o avaliador pega enquanto a coleta segue
                        est["ids"] = list(vistos | {it["id"] for it in novos})
                        gravar_json(saida / "vistos.json", est)
                        log("salvo " + str(len(novos)) + ": " + str(item.get("url", "")))
                        achados[c["id"]] = c
                    if caiu:
                        break
                    parado = parado + 1 if len(achados) == antes else 0
                    rodada.rolar()
                    page.mouse.wheel(0, random.randint(700, 1400))
                    time.sleep(random.uniform(2.5, 4.5))  # rolagem sempre no ritmo normal: o X precisa carregar a lista
            except G.TetoAtingido as e:
                log(f"teto atingido: {e}")
            log(f"{len(achados)} salvos novos na lista")
            salvar_cookies(ctx, cfg)
        except G.SessaoCaiu:
            gravar_erro(saida, f"sessão do X caiu: exporte cookies novos para {cfg.cookies}")
            return 2
        finally:
            nav.close()
    est["ids"] = list(vistos | {it["id"] for it in novos})
    est["ultima_coleta"] = hoje
    gravar_json(saida / "vistos.json", est)
    if caiu:
        gravar_erro(saida, f"sessão do X caiu no meio da coleta ({len(novos)} gravados): exporte cookies novos para {cfg.cookies}")
        return 2
    (saida / "ERRO").unlink(missing_ok=True)
    log(f"gravados {len(novos)} itens novos")
    return 0
