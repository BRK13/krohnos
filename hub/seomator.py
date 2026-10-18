"""Auditoria de SEO com o SEOmator: todo dia de madrugada, em rodízio pelos sites, um site por vez.

    python3 -m hub.seomator noite [--agora]     # cron diário 01:00 BRT: lotes em rodízio até as 06h; --agora ignora a janela
    python3 -m hub.seomator site <id> [--tudo]  # um lote do site agora (--tudo: o site inteiro, em lotes)
    python3 -m hub.seomator resumo [--avisar]   # texto de WhatsApp com nota e piores falhas por site

Como varre: a lista de páginas vem do sitemap (cada sitemap folha é uma seção). Um lote são ~60 páginas auditadas uma a uma
por `deploy/seomator/lote.mjs` (memória constante: o `--crawl` do SEOmator estourava o heap do node em ~300 páginas, saída -6).
Site de uma seção só varre em sequência; site com muitas seções (ClienteJ) pega uma fatia de cada seção por vez, para
toda seção passar pelo menos uma vez por semana. Estado por site em $HUB_DATA/seomator/<site>/: varredura.json (última
auditoria de cada página), cursor.json (onde parou), status.json (último lote e falha), <dia>.json (resumo do dia).
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from hub import armazem, config, conta, data, linear, sites
from hub.comum.datas import BRT

FONTE = "seomator"
INSTALACAO = Path("/opt/seomator")
LOTE = INSTALACAO / "lote.mjs"
REGRAS = INSTALACAO / "regras.mjs"
TIMEOUT_LOTE_S = 45 * 60
SEG_POR_PAGINA = 20  # estimativa até medir (a medida real fica em status.json)
JANELA = (1, 6)  # só começa lote entre 01h e 06h BRT
FATIA = 60  # páginas por lote (~10-20 min)
FATIA_SECAO = 8  # site com mais de MUITAS seções: páginas de cada seção por passagem (60 // 8 seções por lote)
MUITAS = 12
VALIDADE_DIAS = 14  # página não auditada há mais que isso sai do resumo do site
CARGA_MAX = 0.9  # fração dos núcleos: acima disso a HUB está ocupada e o lote espera
ESPERA_S, ESPERA_MAX_S = 120, 30 * 60
ERRO_REGRA = "sitemap-pagina-com-erro"
META_ERRO = {"name": "Página do sitemap com erro HTTP", "description": "Página listada no sitemap responde 4xx/5xx",
             "fix": "Corrija a página ou tire a URL do sitemap; página no sitemap tem que responder 200."}
PESO_ERRO = 5
_PIOR = {"fail": 2, "warn": 1}
MAX_ISSUES_DIA, MAX_LINKS, MAX_REABERTURAS = 10, 30, 2
TERCEIROS = {"jpexemplo/clientee-moraes-site"}  # repo de terceiro: sem label merge
_TRAVA = threading.Lock()


def _dir(base_dados=None):
    return Path(base_dados or data()) / FONTE


def _ler_json(arq, padrao):
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _pasta(p):
    """Pasta 755 e arquivos 644: o painel lê como claude e o rotina.sh usa umask 027."""
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o755)
    return p


def _gravar_json(arq, obj):
    _pasta(arq.parent)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, arq)


def alvos():
    return sites.ativos()


# --- sitemap ---
def _baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Hub-SEOmator"})
    with urllib.request.urlopen(req, timeout=60) as r:
        corpo = r.read()
    if url.endswith(".gz") or corpo[:2] == b"\x1f\x8b":
        import gzip
        corpo = gzip.decompress(corpo)
    return corpo.decode("utf-8", "replace")


def _locs(xml):
    return [u.strip().replace("&amp;", "&") for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


def secoes_do_site(site, baixar=None, base_dados=None, agora=None):
    """Sitemaps folha do site (cada um é uma seção). Índice de sitemap é seguido; lista em cache por 2 dias."""
    baixar = baixar or _baixar
    arq = _dir(base_dados) / site["id"] / "secoes.json"
    cache = _ler_json(arq, {})
    agora = agora or time.time()
    if cache.get("secoes") and agora - cache.get("em", 0) < 2 * 86400:
        return cache["secoes"]
    dominio = site["dominio"]
    raizes = [f"https://{dominio}/sitemap.xml"]
    try:
        robots = baixar(f"https://{dominio}/robots.txt")
        raizes += [u for u in re.findall(r"(?im)^sitemap:\s*(\S+)", robots) if u not in raizes]
    except Exception:  # noqa: BLE001  # sem robots.txt vale o sitemap.xml
        pass
    folhas, vistos, erros = [], set(), []
    fila = list(raizes)
    while fila and len(vistos) < 400:
        u = fila.pop(0)
        if u in vistos:
            continue
        vistos.add(u)
        try:
            xml = baixar(u)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{u}: {e}")
            continue
        if "<sitemapindex" in xml:
            fila += _locs(xml)
        elif _locs(xml):
            folhas.append(u)
    if not folhas:
        raise RuntimeError("sitemap não encontrado: " + ("; ".join(erros)[:200] or "vazio"))
    _gravar_json(arq, {"em": agora, "secoes": folhas})
    return folhas


def urls_da_secao(site, secao, baixar=None):
    """URLs https do domínio do site na seção, sem repetir."""
    hosts = {site["dominio"], f"www.{site['dominio']}"}
    urls = [u for u in _locs((baixar or _baixar)(secao)) if urlsplit(u).scheme == "https" and urlsplit(u).hostname in hosts]
    return list(dict.fromkeys(urls))


def proximo_lote(site, secoes, cursor, baixar=None):
    """Páginas do próximo lote e o cursor novo. Uma seção: sequência (volta ao início no fim). Muitas: fatia de cada seção."""
    cur = {"i": cursor.get("i", 0), "off": dict(cursor.get("off") or {}), "voltas": cursor.get("voltas", 0)}
    por_secao = len(secoes) > MUITAS
    fatia, quantas = (FATIA_SECAO, FATIA // FATIA_SECAO) if por_secao else (FATIA, 1)
    urls, total = [], 0
    for _ in range(min(quantas, len(secoes))):
        secao = secoes[cur["i"] % len(secoes)]
        todas = urls_da_secao(site, secao, baixar)
        off = cur["off"].get(secao, 0)
        if off >= len(todas):
            off = 0
            cur["voltas"] += 0 if por_secao else 1
        urls += todas[off:off + fatia]
        cur["off"][secao] = off + fatia
        total += len(todas)
        if por_secao and cur["i"] % len(secoes) == len(secoes) - 1:
            cur["voltas"] += 1
        cur["i"] = (cur["i"] + 1) % len(secoes)
    return list(dict.fromkeys(urls)), cur, total


# --- executor ---
def _env(home):
    # SEOMATOR_HOME temporário: o audits.db (~35 KB por página) morre com o lote
    return {**os.environ, "SEOMATOR_HOME": str(home), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}


def _limitado(cmd):
    """Fora da unidade systemd da rodada (seomator-rodar.sh) o comando ganha o mesmo teto: CPU 200%, 4 GB, nice 19."""
    if os.environ.get("SEOMATOR_LIMITADO"):
        return cmd
    escopo = ["systemd-run", "--scope", "-q"] if os.geteuid() == 0 else ["systemd-run", "--user", "--scope", "-q"]
    return [*escopo, "-p", "CPUQuota=200%", "-p", "MemoryMax=4G", "nice", "-n", "19", *cmd]


def rodar_lote(urls, rodar=None):
    """Roda lote.mjs nas URLs e devolve o JSON dele. Falha do node levanta com o fim do stderr (o motivo que a tela mostra)."""
    rodar = rodar or subprocess.run
    pasta = _pasta(_dir())
    with tempfile.TemporaryDirectory(dir=pasta) as tmp:
        ent, sai = Path(tmp) / "entrada.json", Path(tmp) / "saida.json"
        ent.write_text(json.dumps({"urls": urls}), encoding="utf-8")
        cmd = _limitado(["node", "--max-old-space-size=3072", str(LOTE), str(ent), str(sai)])
        try:
            r = rodar(cmd, cwd=INSTALACAO, env=_env(tmp), timeout=TIMEOUT_LOTE_S, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"lote passou de {TIMEOUT_LOTE_S // 60} min") from None
        if r.returncode != 0:
            fim = " ".join((r.stderr or "").split())[-240:]
            raise RuntimeError(f"seomator saiu {r.returncode}" + (f": {fim}" if fim else ""))
        return json.loads(sai.read_text(encoding="utf-8"))


def _lote_resiliente(urls, rodar=None):
    """Lote que falha é refeito em duas metades (uma página ruim não derruba as outras); falha da metade levanta."""
    try:
        return rodar_lote(urls, rodar)
    except RuntimeError as e:
        if len(urls) < 2 or "passou de" in str(e):
            raise
        meio = len(urls) // 2
        a, b = rodar_lote(urls[:meio], rodar), rodar_lote(urls[meio:], rodar)
        return {"paginas": a["paginas"] + b["paginas"], "erros": a["erros"] + b["erros"], "meta": {**b["meta"], **a["meta"]}}


# --- estado do site ---
def _arq(site_id, nome, base_dados=None):
    return _dir(base_dados) / site_id / nome


def status(site_id, base_dados=None):
    return _ler_json(_arq(site_id, "status.json", base_dados), {})


def _regras(ids, base_dados, meta_novo):
    """{regra: {name, description, fix}}: cache em regras.json, completado com os metadados que o lote devolveu."""
    arq = _dir(base_dados) / "regras.json"
    cache = _ler_json(arq, {})
    cache.setdefault(ERRO_REGRA, META_ERRO)
    if not set(meta_novo or {}) <= set(cache):
        cache.update(meta_novo)
        _gravar_json(arq, cache)
    return cache


def aplicar_lote(site_id, bruto, dia, base_dados=None):
    """Grava no mapa de páginas do site o que o lote achou; devolve o mapa."""
    arq = _arq(site_id, "varredura.json", base_dados)
    v = _ler_json(arq, {"paginas": {}, "info": {}})
    info = v.setdefault("info", {})
    for p in bruto["paginas"]:
        regras = {}
        for a in p["achados"]:
            regras[a["ruleId"]] = "fail" if a["status"] == "fail" or regras.get(a["ruleId"]) == "fail" else "warn"
            i = info.setdefault(a["ruleId"], {"cat": a["cat"], "peso": a["weight"], "msg": a.get("message")})
            if a["status"] == "fail":
                i.update(peso=a["weight"], msg=a.get("message") or i.get("msg"))
        v["paginas"][p["url"]] = {"dia": dia, "nota": p["nota"], "cats": p["cats"], "regras": regras}
    for e in bruto["erros"]:
        if e.get("http") and int(e["http"]) >= 400:  # erro HTTP numa página do sitemap é achado; timeout não
            info.setdefault(ERRO_REGRA, {"cat": "crawl", "peso": PESO_ERRO, "msg": e["erro"]})
            v["paginas"][e["url"]] = {"dia": dia, "nota": None, "cats": {}, "regras": {ERRO_REGRA: "fail"}}
        # timeout e falha de render são do ambiente: a medida anterior da página fica
    corte = (dt.date.fromisoformat(dia) - dt.timedelta(days=VALIDADE_DIAS)).isoformat()
    v["paginas"] = {u: p for u, p in v["paginas"].items() if p["dia"] >= corte}
    _gravar_json(arq, v)
    return v


def consolidar(varredura, dia, meta=None, total=None, url=None):
    """Resumo do site a partir da última auditoria de cada página: pior status por regra, impacto = peso × páginas com fail."""
    meta = meta or {}
    paginas = varredura.get("paginas") or {}
    regras = {}
    for u, p in paginas.items():
        for r, st in p["regras"].items():
            i = (varredura.get("info") or {}).get(r, {})
            g = regras.setdefault(r, {"regra": r, "cat": i.get("cat"), "status": st, "peso": i.get("peso") or 0, "falhas": [],
                                      "alertas": [], "msg": i.get("msg")})
            g["falhas" if st == "fail" else "alertas"].append(u)
    lista = []
    for g in regras.values():
        fail = bool(g["falhas"])
        g.update(status="fail" if fail else "warn", paginas=g.pop("falhas") if fail else g["alertas"])
        g.pop("alertas")
        m = meta.get(g["regra"]) or {}
        g.update(nome=m.get("name") or g["regra"], fix=m.get("fix") or "", impacto=g["peso"] * len(g["paginas"]) if fail else 0)
        lista.append(g)
    lista.sort(key=lambda g: (-g["impacto"], -_PIOR[g["status"]], -g["peso"] * len(g["paginas"]), g["regra"]))
    notas = [p["nota"] for p in paginas.values() if isinstance(p.get("nota"), (int, float))]
    cats = {}
    for p in paginas.values():
        for c, s in (p.get("cats") or {}).items():
            if isinstance(s, (int, float)):
                cats.setdefault(c, []).append(s)
    return {"dia": dia, "url": url, "nota": round(sum(notas) / len(notas)) if notas else None, "paginas": len(paginas),
            "total": total, "categorias": {c: round(sum(v) / len(v)) for c, v in cats.items()}, "regras": lista}


def fatos(site_id, resumo):
    base = {"dia": resumo["dia"], "negocio": site_id, "fonte": FONTE}
    saida = [{**base, "metrica": m, "dim": "", "valor": resumo[m]} for m in ("nota", "paginas") if resumo[m] is not None]
    saida += [{**base, "metrica": "nota_cat", "dim": c, "valor": v} for c, v in resumo["categorias"].items() if v is not None]
    for g in resumo["regras"]:
        falha = g["status"] == "fail"
        saida.append({**base, "metrica": "falhas" if falha else "alertas", "dim": g["regra"], "valor": len(g["paginas"])})
        if falha:
            saida += [{**base, "metrica": "falha_pagina", "dim": f"{g['regra']}|{urlsplit(u).path or '/'}", "valor": 1}
                      for u in g["paginas"]]
    return saida


def ultimo_resumo(site_id, base_dados=None):
    arqs = sorted(_dir(base_dados).joinpath(site_id).glob("????-??-??.json"))
    return _ler_json(arqs[-1], None) if arqs else None


def auditar(site, dia, base_dados=None, rodar=None, urls=None, baixar=None):
    """Um lote do site (ou as `urls` dadas): audita, atualiza o mapa de páginas, grava resumo do dia e fatos.
    Devolve o resumo consolidado; erro do node ou do sitemap levanta."""
    pasta = _pasta(_dir(base_dados))
    for velho in pasta.glob("tmp*"):  # sobra de execução morta (só as mais velhas que o timeout: outra pode estar rodando)
        if velho.is_dir() and time.time() - velho.stat().st_mtime > TIMEOUT_LOTE_S + 600:
            shutil.rmtree(velho, ignore_errors=True)
    cursor_arq = _arq(site["id"], "cursor.json", base_dados)
    total = None
    cursor = None
    if urls is None:
        secoes = secoes_do_site(site, baixar, base_dados)
        urls, cursor, total = proximo_lote(site, secoes, _ler_json(cursor_arq, {}), baixar)
    if not urls:
        raise RuntimeError("sitemap sem páginas do domínio")
    inicio = time.monotonic()
    bruto = _lote_resiliente(urls, rodar)
    dur = time.monotonic() - inicio
    if not bruto["paginas"] and not bruto["erros"]:
        raise RuntimeError("lote sem nenhuma página auditada")
    varredura = aplicar_lote(site["id"], bruto, dia, base_dados)
    meta = _regras(set(varredura["info"]), base_dados, bruto["meta"])
    st = status(site["id"], base_dados)
    resumo = consolidar(varredura, dia, meta, total or st.get("total"), f"https://{site['dominio']}")
    resumo["duracao_s"] = round(dur)
    _gravar_json(pasta / site["id"] / f"{dia}.json", resumo)
    armazem.gravar(fatos(site["id"], resumo), base_dados)
    if cursor is not None:
        _gravar_json(cursor_arq, cursor)
    feitas = len(bruto["paginas"]) + len(bruto["erros"])
    _gravar_json(_arq(site["id"], "status.json", base_dados), {
        "ultimo_lote": dt.datetime.now(dt.timezone.utc).isoformat(), "dia": dia, "total": total or st.get("total"),
        "s_por_pagina": round(dur / max(feitas, 1), 1), "falha": None, "voltas": (cursor or {}).get("voltas", st.get("voltas", 0))})
    return resumo


def registrar_falha(site_id, motivo, base_dados=None):
    """A tela mostra o card do site com 'falhou: <motivo>'; o resumo do último lote bom continua valendo."""
    st = status(site_id, base_dados)
    st["falha"] = {"em": dt.datetime.now(dt.timezone.utc).isoformat(), "motivo": str(motivo)[:300]}
    _gravar_json(_arq(site_id, "status.json", base_dados), st)


# --- janela e carga ---
def _cabe(agora_dt, duracao_s):
    fim = agora_dt.replace(hour=JANELA[1], minute=0, second=0, microsecond=0)
    return JANELA[0] <= agora_dt.hour < JANELA[1] and agora_dt + dt.timedelta(seconds=duracao_s) <= fim


def _carga_alta():
    return os.getloadavg()[0] > CARGA_MAX * (os.cpu_count() or 1)


def _esperar_folga(carga_alta=None, dormir=time.sleep):
    """Se a HUB está ocupada, espera (até 30 min) em vez de brigar; False = não abriu folga."""
    carga_alta = carga_alta or _carga_alta
    esperado = 0
    while carga_alta():
        if esperado >= ESPERA_MAX_S:
            return False
        dormir(ESPERA_S)
        esperado += ESPERA_S
    return True


# --- issues ---
def _estado_linear(ident):
    return linear.gql("query($id: String!) { issue(id: $id) { state { type } } }", id=ident)["issue"]["state"]["type"]


def _cerca(texto):
    """Texto do site auditado num bloco de código, sem crases que fechem a cerca."""
    return ["```", str(texto).replace("`", "'"), "```"] if texto else ["(sem mensagem)"]


def _corpo(g, resumo):
    links = [f"- {u}" for u in g["paginas"][:MAX_LINKS]]
    if len(g["paginas"]) > MAX_LINKS:
        links.append(f"- e mais {len(g['paginas']) - MAX_LINKS}")
    return "\n".join([
        "## O que é", f"- Regra: {g['nome']} (`{g['regra']}`)", f"- Categoria: {g['cat']}",
        "- Mensagem de exemplo:", *_cerca(g.get("msg")), "", "## Páginas afetadas", *links, "",
        "## Correção sugerida pelo SEOmator", *[f"> {x}" for x in (g.get("fix") or "(sem sugestão)").splitlines()], "",
        "## Como conferir",
        f"Rode `seomator audit <url> --format llm` numa página afetada antes e depois da mudança; "
        f"a regra `{g['regra']}` tem que sair dos `<issue>`. A auditoria noturna do hub reconfere as páginas depois que a "
        f"issue for concluída e reabre se a regra continuar.", "",
        f"Veio da auditoria diária do hub (KRN-809, `hub/seomator.py`) de {resumo['dia']}."])


def abrir_issues(site, resumo, base_dados=None, criar=None):
    """Abre issue (Backlog) para cada regra grave nova do site (uma por regra e site), por impacto, até 10 por dia no total."""
    if site.get("sem_issue"):
        return []
    with _TRAVA:
        return _abrir_issues(site, resumo, base_dados, criar or linear.criar_issue)


def _abrir_issues(site, resumo, base_dados, criar):
    arq = _dir(base_dados) / "issues.json"
    reg = _ler_json(arq, {})
    do_site = reg.setdefault(site["id"], {})
    dia = resumo["dia"]
    # conta as de hoje em todos os sites, inclusive as anteriores de regras reabertas, senão reabrir fura o teto
    hoje = sum(1 for s in reg.values() for v in s.values() for d in [v["dia"], *(a["dia"] for a in v.get("anteriores", []))]
               if d == dia)
    github = (site.get("repo") or {}).get("github")
    labels = ["esteira", "seo-fix"] + ([] if github in TERCEIROS else ["merge"])
    projeto, novas = None, []
    for g in resumo["regras"]:  # já ordenadas por impacto
        if g["status"] != "fail":
            continue
        if hoje >= MAX_ISSUES_DIA:
            break
        ant = do_site.get(g["regra"])
        if ant and not ant.get("verificada"):  # a reconferência noturna verifica (ou reabre) a issue anterior
            continue
        projeto = projeto or linear.projeto_do_repo(github)
        titulo = f"SEO {site['nome']}: {g['nome']} ({len(g['paginas'])} página(s))"
        ident = criar(projeto, titulo, _corpo(g, resumo), "Backlog", labels, 3).split()[0]
        anteriores = (ant.get("anteriores", []) + [{"issue": ant["issue"], "dia": ant["dia"]}]) if ant else []
        do_site[g["regra"]] = {"issue": ident, "dia": dia, "paginas": g["paginas"][:MAX_LINKS],
                               **({"anteriores": anteriores} if anteriores else {})}
        _gravar_json(arq, reg)  # a cada issue: falha na próxima não perde o registro desta
        hoje += 1
        novas.append(ident)
    return novas


def reconferir(site, dia, base_dados=None, rodar=None, estado_de=None, reabrir=None, baixar=None, max_issues=40):
    """Issue `seo-fix` concluída: audita de novo as páginas dela. Regra sumiu = verificada; continua = reabre a issue.
    Devolve {issue: 'verificada'|'reaberta'}. Issue cancelada fica como está (falso positivo)."""
    arq = _dir(base_dados) / "issues.json"
    reg = _ler_json(arq, {})
    do_site = reg.get(site["id"]) or {}
    estado_de = estado_de or _estado_linear
    reabrir = reabrir or (lambda ident, texto: linear.mover_por_nome(ident, "Backlog", texto))
    saida, pendentes = {}, []
    abertas = [(r, v) for r, v in do_site.items() if not (v.get("verificada") or v.get("cancelada"))]
    for regra, v in abertas[:max_issues]:
        try:
            st = estado_de(v["issue"])
        except Exception:  # noqa: BLE001  # consulta falhou: trata como aberta
            continue
        if st == "canceled":
            v["cancelada"] = dia
        elif st == "completed":
            pendentes.append((regra, v))
    if pendentes:
        urls = list(dict.fromkeys(u for _, v in pendentes for u in (v.get("paginas") or [])))
        if urls:
            aplicar_lote(site["id"], _lote_resiliente(urls, rodar), dia, base_dados)
        varredura = _ler_json(_arq(site["id"], "varredura.json", base_dados), {"paginas": {}})
        for regra, v in pendentes:
            pags = v.get("paginas") or []
            ainda = [u for u in pags if (varredura["paginas"].get(u) or {}).get("regras", {}).get(regra) == "fail"]
            if not pags:  # registro antigo, sem a lista de páginas: vale o mapa do site, se ele tiver medida
                ainda = [u for u, p in varredura["paginas"].items() if p["regras"].get(regra) == "fail"]
                medidas = bool(varredura["paginas"])
            else:  # só vale a página medida agora: timeout/falha de render mantém a medida velha e não prova nada
                medidas = all((varredura["paginas"].get(u) or {}).get("dia") == dia for u in pags)
            if not ainda and not medidas:
                continue  # sem medição válida: fica concluída e é reconferida na próxima noite
            if not ainda:
                v["verificada"] = dia
                saida[v["issue"]] = "verificada"
            elif v.get("reaberturas", 0) < MAX_REABERTURAS:
                v["reaberturas"] = v.get("reaberturas", 0) + 1
                reabrir(v["issue"], f"A auditoria de {dia} reconferiu a regra `{regra}` e ela continua em {len(ainda)} página(s), "
                                    f"por exemplo {ainda[0]}. Reaberta: a tarefa só fecha quando a regra some.")
                saida[v["issue"]] = "reaberta"
    if pendentes or any(v.get("cancelada") for v in do_site.values()):
        _gravar_json(arq, reg)
    return saida


# --- rodada ---
def _falha(erros, base_dados=None):
    """Como o deploy/rotina.sh: a unidade systemd roda fora dele, então a falha entra aqui no falhas.jsonl (resumo das 8h)."""
    try:
        pasta = Path(base_dados or data()) / "rotinas"
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / "falhas.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"em": dt.datetime.now(dt.timezone.utc).isoformat(), "nome": FONTE, "codigo": 1,
                                "fim": "\n".join(erros)[-600:]}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001  # gravar a falha não derruba a rodada
        print(f"falhas.jsonl não gravado: {e}", file=sys.stderr)


def _ordem(lista, base_dados=None):
    """Quem ficou mais tempo sem lote vai primeiro; site que nunca rodou, antes de todos."""
    return sorted(lista, key=lambda s: status(s["id"], base_dados).get("ultimo_lote") or "")


def _lote_do_site(s, dia, base_dados, rodar, abrir, baixar):
    resumo = auditar(s, dia, base_dados, rodar, baixar=baixar)
    try:
        (abrir or abrir_issues)(s, resumo, base_dados)
    except (Exception, SystemExit) as e:  # noqa: BLE001  # Linear fora: o resumo fica, a issue não
        print(f"{s['id']}: issue não aberta: {e}", file=sys.stderr)
    return resumo


def noite(agora=False, hoje=None, base_dados=None, rodar=None, abrir=None, baixar=None, dormir=time.sleep,
          carga_alta=None, estado_de=None, reabrir=None, passada=False, max_lotes=None):
    """Rodízio de lotes até as 06h BRT. agora: ignora a janela. passada: uma única volta por todos os sites (primeira rodada).
    hoje: datetime BRT fixo ou função que devolve o relógio (testes); max_lotes: teto de lotes (testes). Devolve (último resumo por site, erros); erros também vão para rotinas/falhas.jsonl."""
    relogio = hoje if callable(hoje) else (lambda: hoje) if hoje else (lambda: dt.datetime.now(BRT))
    lista, saida, erros, mortos, feitos = alvos(), {}, [], set(), 0
    # 1. issues concluídas: a página do conserto é conferida de novo antes do rodízio
    for s in lista if (_dir(base_dados) / "issues.json").exists() else []:
        dia = relogio().date().isoformat()
        if not agora and not _cabe(relogio(), 15 * 60):
            break
        try:
            for ident, r in reconferir(s, dia, base_dados, rodar, estado_de, reabrir, baixar).items():
                print(f"{s['id']}: {ident} {r}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001  # reconferência falhar não impede o rodízio
            print(f"{s['id']}: reconferência: {e}", file=sys.stderr)
    # 2. rodízio de lotes
    while True:
        andou = False
        for s in _ordem([x for x in lista if x["id"] not in mortos], base_dados):
            agora_dt = relogio()
            st = status(s["id"], base_dados)
            estimativa = FATIA * (st.get("s_por_pagina") or SEG_POR_PAGINA)
            if not (agora or _cabe(agora_dt, estimativa)):
                print("fora da janela ou sem tempo para outro lote, fica para a próxima madrugada", file=sys.stderr)
                return _fim(saida, erros, base_dados)
            if not _esperar_folga(carga_alta, dormir):
                print("HUB ocupada: a rodada espera a próxima madrugada", file=sys.stderr)
                return _fim(saida, erros, base_dados)
            try:
                saida[s["id"]] = _lote_do_site(s, agora_dt.date().isoformat(), base_dados, rodar, abrir, baixar)
            except Exception as e:  # noqa: BLE001  # um site com erro não derruba os outros
                print(f"{s['id']}: {e}", file=sys.stderr)
                erros.append(f"{s['id']}: {e}")
                registrar_falha(s["id"], e, base_dados)
                mortos.add(s["id"])  # não insiste no mesmo site na mesma noite
            andou, feitos = True, feitos + 1
            if passada:
                mortos.add(s["id"])
            if max_lotes and feitos >= max_lotes:
                return _fim(saida, erros, base_dados)
        if passada or not andou or len(mortos) >= len(lista):
            return _fim(saida, erros, base_dados)


def _fim(saida, erros, base_dados):
    if erros:
        _falha(erros, base_dados)
    return saida, erros


def passada_inicial(base_dados=None, **kw):
    """Primeira rodada depois de instalar esta versão: uma volta por todos os sites fora da janela, e o resumo ao dono."""
    arq = _dir(base_dados) / "estado.json"
    estado = _ler_json(arq, {})
    if estado.get("passada_v2"):
        return None
    saida, erros = noite(agora=True, passada=True, base_dados=base_dados, **kw)
    if not erros:  # site que falhou = a primeira rodada não terminou: repete na próxima chamada
        estado["passada_v2"] = dt.date.today().isoformat()
        _gravar_json(arq, estado)
    return saida, erros


def _nota_falha(st):
    f = st.get("falha")
    return f" — falhou: {f['motivo']}" if f else ""


def resumo_zap(base_dados=None, hoje=None, lista=None):
    hoje = hoje or dt.datetime.now(BRT).date()
    linhas = [f"*SEO — {hoje:%d/%m}*"]
    for s in alvos() if lista is None else lista:
        r, st = ultimo_resumo(s["id"], base_dados), status(s["id"], base_dados)
        if not r:
            linhas.append(f"\n{s['nome']}{_nota_falha(st) or ' — ainda sem auditoria'}")
            continue
        dia = dt.date.fromisoformat(r["dia"])
        velho = f" (de {dia:%d/%m})" if (hoje - dia).days >= 7 else ""
        cob = f", {r.get('paginas')} pág. auditadas" if r.get("paginas") else ""
        linhas.append(f"\n{s['nome']} — nota {r.get('nota')}{velho}{cob}{_nota_falha(st)}")
        falhas = sorted((g for g in r.get("regras") or [] if g["status"] == "fail"), key=lambda g: -g["impacto"])
        linhas += [f"• {g['nome']} ({len(g['paginas'])} pág.)" for g in falhas[:3]]
    linhas.append("\nTela: https://nimbus.clientec-com.example/seo")
    return "\n".join(linhas)


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] in (["noite"], ["semanal"]):  # semanal: nome antigo, o cron e o rerodar-rotina ainda podem chamar
        if "--agora" not in a:
            r = passada_inicial()
            if r is not None:
                if not r[1]:
                    _avisar_primeira()
                return 1 if r[1] else 0
        return 1 if noite(agora="--agora" in a)[1] else 0
    if a[:1] == ["site"] and len(a) >= 2:
        s = next((x for x in alvos() if x["id"] == a[1]), None)
        if not s:
            print(f"site {a[1]!r} não está ativo", file=sys.stderr)
            return 2
        dia = dt.datetime.now(BRT).date().isoformat()
        try:
            volta0 = status(s["id"]).get("voltas", 0)
            r = auditar(s, dia)
            while "--tudo" in a and status(s["id"]).get("voltas", 0) == volta0:  # uma volta completa do sitemap
                r = auditar(s, dia)
        except Exception as e:  # noqa: BLE001
            registrar_falha(s["id"], e)
            _falha([f"{s['id']}: {e}"])
            raise
        print(f"{s['id']}: nota {r['nota']}, {sum(g['status'] == 'fail' for g in r['regras'])} falhas, {r['paginas']} páginas")
        try:
            print(f"issues novas: {abrir_issues(s, r) or 'nenhuma'}")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            print(f"{s['id']}: issue não aberta: {e}", file=sys.stderr)
        return 0
    if a[:1] == ["resumo"]:
        texto = resumo_zap()
        print(texto)
        if "--avisar" in a:
            conta.avisar(texto, origem="seomator")
        return 0
    print(__doc__)
    return 2


def _avisar_primeira(base_dados=None):
    """Fim da primeira passada: o resumo de 1 tela ao dono (caixa de avisos) e o mesmo texto na issue."""
    texto = resumo_zap(base_dados)
    for passo in (lambda: conta.avisar(texto, origem="seomator"), lambda: linear.falar("KRN-809", texto)):
        try:
            passo()
        except (Exception, SystemExit) as e:  # noqa: BLE001  # aviso falhar não desfaz a rodada
            print(f"aviso da primeira passada: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
