"""Painel web do hub: https://nimbus.clientec-com.example → Traefik → aqui (só leitura).

Mostra o que as rotinas já gravam em HUB_DATA: verificador, Search Console, GA4, AI Overview, propostas, uso do Claude
e saúde da VPS. Lê só caminhos montados aqui dentro (nunca um caminho vindo da URL) e não mostra credencial nenhuma
(claude/*token*, a pasta de segredos). Entrada por usuário e senha (hub/usuarios.py): formulário em /entrar, cookie de
sessão __Host-kronos_sessao (HttpOnly, Secure, SameSite=Strict) e CSRF por sessão em todo POST. Na subida, a senha única antiga
(PAINEL_SENHA) vira o 1º dono se ainda não há usuário. Roda como o usuário claude (deploy/nimbus-painel.service).

    python3 -m hub.painel            # serve em PAINEL_HOST:PAINEL_PORTA (172.19.0.1:8791)
"""
import collections
import datetime as dt
import functools
import hmac
import html
import ipaddress
import json
import math
import os
import re
import sys
import threading
import sqlite3
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub import comecar, config, config_telas, faltou, data as hub_data, limite, linear_uso, plugins, ui, usuarios
from hub import rastreio_tela, portao_done, roteamento, acoes, agenda, biblioteca_tela, cerebro, grafo_tela, metas_tela, seomator_tela, cockpit, conta as C, dataforseo as DFS, propostas as P, monitor, resumo, sites as S, uso_contas as U

# Telas do negócio nosso (BI dos nossos bancos, virais, lições, RUM dos nossos sites) ficam fora do snapshot público
#: sem o módulo, a rota cai no 404 e o item some do menu. Na main todas existem e nada muda.
try:
    import hub.bi_painel as bi_painel
except ImportError as _e:
    if not faltou(_e, "hub.bi_painel"):
        raise
    bi_painel = None
try:
    import hub.erros_tela as erros_tela
except ImportError as _e:
    if not faltou(_e, "hub.erros_tela"):
        raise
    erros_tela = None
try:
    import hub.virais_tela as virais_tela
except ImportError as _e:
    if not faltou(_e, "hub.virais_tela"):
        raise
    virais_tela = None
try:
    import hub.velocidade_campo_tela as velocidade_campo_tela
except ImportError as _e:
    if not faltou(_e, "hub.velocidade_campo_tela"):
        raise
    velocidade_campo_tela = None
try:
    import hub.destaques_tela as destaques_tela
except ImportError as _e:
    if not faltou(_e, "hub.destaques_tela"):
        raise
    destaques_tela = None
try:
    import hub.portfolio_tela as portfolio_tela
except ImportError as _e:
    if not faltou(_e, "hub.portfolio_tela"):
        raise
    portfolio_tela = None
try:
    import hub.fabrica_tela as fabrica_tela
except ImportError as _e:
    if not faltou(_e, "hub.fabrica_tela"):
        raise
    fabrica_tela = None
try:
    import hub.concorrentes_tela as concorrentes_tela
except ImportError as _e:
    if not faltou(_e, "hub.concorrentes_tela"):
        raise
    concorrentes_tela = None
try:
    import hub.provas_tela as provas_tela
except ImportError as _e:
    if not faltou(_e, "hub.provas_tela"):
        raise
    provas_tela = None
try:
    import hub.redteam.tela as redteam_tela
except ImportError as _e:
    if not faltou(_e, "hub.redteam"):
        raise
    redteam_tela = None
_SEM_TELA = {c for mod, cs in ((bi_painel, ("/bi", "/bi/comparar")), (virais_tela, ("/virais",)), (erros_tela, ("/erros",)),
                               (velocidade_campo_tela, ("/velocidade",)), (destaques_tela, ("/destaques",)), (portfolio_tela, ("/portfolio",)),
                               (fabrica_tela, ("/fabrica",)),
                               (provas_tela, ("/provas",)), (redteam_tela, ("/redteam",)), (concorrentes_tela, ("/concorrentes",))) if mod is None for c in cs}
if _SEM_TELA:
    ui.MENU[:] = [(g, ic, [i for i in itens if i[0] not in _SEM_TELA]) for g, ic, itens in ui.MENU
                  if any(i[0] not in _SEM_TELA for i in itens)]

ENDERECO = (os.environ.get("PAINEL_HOST", "172.19.0.1"), int(os.environ.get("PAINEL_PORTA", "8791")))
RE_ID = re.compile(r"^[a-z0-9-]{1,60}$")
RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
COOKIE = "__Host-kronos_sessao"
MAX_IDADE = 7 * 24 * 3600  # igual a usuarios.MAXIMA
_pedido = threading.local()  # .sessao do pedido em curso ({"email","papel","csrf"} ou None): _csrf() e o papel (4.3)
# Link de convite recém-criado, por e-mail do dono que convidou: aparece uma vez em /config/usuarios e sai daqui.
# Só na memória do processo (nunca em query string, log ou disco).
_LINKS = {}
_LINKS_TRAVA = threading.Lock()


def dados():
    from hub import config
    # VPS: sem toml/env, o caminho legado vale mesmo sem a pasta (nunca cair para <repo>/data)
    if not config.definido("caminhos.dados") and Path(config.LEGADO["caminhos.codigo"]).is_dir():
        return Path(config.LEGADO["caminhos.dados"])
    return hub_data()


def _json(p, padrao=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _texto(p):
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


def _jsonl(p, n):
    linhas = _texto(p).splitlines()[-n:]
    saida = []
    for linha in linhas:
        try:
            saida.append(json.loads(linha))
        except ValueError:
            pass
    return saida


def token_do(cabecalho):
    """Token do cookie __Host-kronos_sessao no cabeçalho Cookie; "" se não há. Cookie com lixo de outro site não derruba nada:
    cada par é lido sozinho."""
    for parte in (cabecalho or "").split(";"):
        try:
            c = SimpleCookie()
            c.load(parte.strip())
        except CookieError:
            continue
        if COOKIE in c and c[COOKIE].value:
            return c[COOKIE].value
    return ""


def sessao_atual():
    """Sessão do pedido em curso (None fora de um pedido ou sem login)."""
    return getattr(_pedido, "sessao", None)


def volta_ok(v):
    """Só caminho do próprio painel: começa com "/", não com "//" nem "/\\", sem controle nem espaço."""
    v = v or ""
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(c) <= 32 or ord(c) > 126 for c in v):
        return "/"
    return v


def _proxy_confiavel(ip):
    """Proxy que pode dizer o IP do cliente: painel.proxies (IPs/CIDRs) se definida; senão loopback e rede privada."""
    try:
        end = ipaddress.ip_address(ip)
    except ValueError:
        return False
    redes = config.get("painel.proxies") or []
    if not redes:
        return end.is_private or end.is_loopback
    for r in redes:
        try:
            if end in ipaddress.ip_network(str(r), strict=False):
                return True
        except ValueError:
            continue
    return False


def ip_cliente(conexao, xff):
    """IP do cliente: o último item de X-Forwarded-For (o que o Traefik pôs) só se a conexão vem de proxy confiável e o
    item é IP; senão o da conexão (cliente da internet não forja o IP do limite nem da auditoria)."""
    ultimo = (xff or "").split(",")[-1].strip()
    if ultimo and _proxy_confiavel(conexao):
        try:
            return str(ipaddress.ip_address(ultimo))
        except ValueError:
            pass
    return conexao


def cookie_sessao(token, max_idade=MAX_IDADE):
    return f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={max_idade}"


# ---------- números ----------

def ultimo_snapshot():
    pastas = sorted(p for p in (dados() / "snapshots").glob("20*") if p.is_dir())
    return pastas[-1] if pastas else None


def _fontes(sid):
    return _fontes_em(str(dados()), sid)


@functools.lru_cache(maxsize=64)  # limpo a cada requisição em rota()
def _fontes_em(base, sid):
    """Pontos diários por data, de todos os snapshots (o mais recente vence: as janelas de 7 dias se sobrepõem).
    gsc: data -> (cliques, impressões, posição×impressões); ga4: data -> (sessões, eventos). Sem snapshot com data no
    GA4 (coletas antigas), um ponto por snapshot (total de 7 dias) e datado=False."""
    g, a, a_snap = {}, {}, {}
    # 35 snapshots bastam para 28 dias de janelas de 7 dias; mais que isso só custaria leitura
    for snap in sorted(p for p in (Path(base) / "snapshots").glob("20*") if p.is_dir())[-35:]:
        for r in (_json(snap / f"{sid}-gsc.json") or {}).get("rows") or []:
            try:
                i = r.get("impressions", 0)
                g[dt.date.fromisoformat(r["keys"][0]).isoformat()] = (r.get("clicks", 0), i, r.get("position", 0) * i)
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        for r in (_json(snap / f"{sid}-ga4-dia.json") or {}).get("rows") or []:
            try:
                d = r["dimensionValues"][0]["value"]
                a[dt.date.fromisoformat(f"{d[:4]}-{d[4:6]}-{d[6:]}").isoformat()] = (float(r["metricValues"][0]["value"]), float(r["metricValues"][1]["value"]))
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        rows = (_json(snap / f"{sid}-ga4.json") or {}).get("rows") or []
        if rows:
            try:
                a_snap[snap.name] = (sum(float(r["metricValues"][0]["value"]) for r in rows),
                                     sum(float(r["metricValues"][2]["value"]) for r in rows))
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    return g, (a or a_snap), bool(a)


def _valor(metrica, t):
    """Valor de uma métrica a partir de pontos somados; CTR e posição são ponderados, não somados."""
    if metrica in ("cliques", "sessoes"):
        return t[0]
    if metrica in ("impressoes", "eventos"):
        return t[1]
    if metrica == "ctr":
        return t[0] / t[1] if t[1] else None
    if metrica == "posicao":
        return t[2] / t[1] if t[1] else None
    raise ValueError(metrica)


def _janela(sid, metrica, dias):
    fonte = _fontes(sid)
    gsc = metrica in ("cliques", "impressoes", "ctr", "posicao")
    pts, datado = (fonte[0], True) if gsc else (fonte[1], fonte[2])
    if not pts:
        return {}, datado
    fim = dt.date.fromisoformat(max(pts))
    return {d: t for d, t in pts.items() if (fim - dt.date.fromisoformat(d)).days < dias}, datado


def serie(sid, metrica, dias=28):
    """[(data, valor)] por dia nos últimos `dias` dias da série (dias sem dado ficam de fora). Métricas: cliques,
    impressoes, ctr, posicao, sessoes, eventos."""
    pts, _ = _janela(sid, metrica, dias)
    v = [(d, _valor(metrica, t)) for d, t in sorted(pts.items())]
    return [(d, x) for d, x in v if x is not None]


def total(sid, metrica, dias=28):
    """Valor da métrica nos últimos `dias` dias (CTR e posição ponderados); None sem dado."""
    pts, datado = _janela(sid, metrica, dias)
    if not pts or not datado:  # sem data, somar totais de 7 dias sobrepostos contaria ~7x
        return None
    return _valor(metrica, tuple(sum(t[i] for t in pts.values()) for i in range(len(next(iter(pts.values()))))))


def dias(sid, metrica, dias=28):
    """Quantos dias com dado a janela de `dias` cobre (0 sem série datada): o rótulo do total não pode prometer 28."""
    pts, datado = _janela(sid, metrica, dias)
    return len(pts) if datado else 0


def delta(sid, metrica):
    """% dos últimos 7 dias contra os 7 anteriores, dentro da série de 28 dias; None se uma das janelas está
    incompleta, a base é zero ou a série não tem datas (um ponto por snapshot)."""
    pts, datado = _janela(sid, metrica, 28)
    if not pts or not datado:
        return None
    fim = dt.date.fromisoformat(max(pts))
    w = ([], [])
    for d, t in pts.items():
        k = (fim - dt.date.fromisoformat(d)).days
        if k < 14:
            w[k // 7].append(t)
    if len(w[0]) < 7 or len(w[1]) < 7:
        return None
    novo, velho = (_valor(metrica, tuple(sum(c) for c in zip(*x))) for x in w)
    if novo is None or not velho:
        return None
    return round((novo - velho) / velho * 100, 1)  # 0,04% vira 0,0% e fica neutro, não "▲ 0,0%" verde


def verificador(sid):
    v = _json(dados() / "verificador" / sid / "latest.json")
    if not v:
        return None
    itens = v.get("itens") or []
    return {"data": v.get("data"), "ok": sum(1 for i in itens if i.get("ok")), "total": len(itens),
            "falhas": [i for i in itens if not i.get("ok")]}


def ciclo_aio(sid):
    ciclos = sorted((dados() / "aio" / sid).glob("ciclo-*.md"))
    return ciclos[-1] if ciclos else None


# ---------- html ----------

e = html.escape
TONS = ("ok", "alerta", "ruim", "neutro")


def _csrf():
    s = sessao_atual()
    return s["csrf"] if s else ""


def csrf_ok(dado):
    s = sessao_atual()
    return bool(s and dado) and hmac.compare_digest(dado.encode(), s["csrf"].encode())


def _form(acao, campos, botoes):
    """Formulário POST /acao/<acao> com CSRF; botoes = [(rótulo, classe, name, value)]. "" quando o papel da sessão não
    pode a ação (sem sessão, como no teste que chama rota() direto, mostra)."""
    s = sessao_atual()
    if s and not usuarios.pode(s.get("papel"), "acao:" + acao):
        return ""
    tom = {"ok": "prim", "ruim": "perigo"}  # nomes antigos → classes do ui (.form button.prim / .perigo)
    bs = "".join(f'<button class="{tom.get(c, c)}" type="submit"{f" name=\"{n}\" value=\"{e(v)}\"" if n else ""}>{e(r)}</button>'
                 for r, c, n, v in botoes)
    return (f'<form class="form" method="post" action="/acao/{acao}"><input type="hidden" name="csrf" value="{_csrf()}">'
            f'{campos}{bs}</form>')


def _pedir_form():
    opts = "".join(f"<option>{e(p)}</option>" for p in acoes.PROJETOS)
    return _form(
        "criar-issue", f'<select name="projeto" required aria-label="Projeto">{opts}</select>'
        '<input class="largo" name="titulo" required maxlength="200" placeholder="O que você quer?" aria-label="Título">'
        '<textarea class="largo" name="descricao" placeholder="Detalhes (opcional)" aria-label="Detalhes"></textarea>',
        [("Pedir", "ok", None, "")])


def metas_pagina():
    sel = lambda nome, ops, rot, padrao=None: (f'<select name="{nome}" aria-label="{rot}">'
                                  + "".join(f'<option value="{e(v)}"{" selected" if v == padrao else ""}>{e(t)}</option>' for v, t in ops) + "</select>")
    sites_ = [("", "Sem site")] + [(x["id"], x["nome"]) for x in S.ativos()]
    form = _form("criar-meta",
                 '<input class="largo" name="objetivo" required maxlength="300" placeholder="Objetivo (ex.: LCP da home abaixo de 2,5 s)" aria-label="Objetivo">'
                 + sel("site", sites_, "Site")
                 + '<input name="url" placeholder="URL medida (PageSpeed)" aria-label="URL">'
                 + sel("metrica", [(m, m) for m in ("lcp_ms", "nota", "tbt_ms", "cls")], "Métrica")
                 + '<input name="como" placeholder="Ou: como medir (manual)" aria-label="Medida manual">'
                 '<input name="alvo" placeholder="Alvo" aria-label="Alvo">'
                 + sel("direcao", [("menor", "Menor é melhor"), ("maior", "Maior é melhor")], "Direção")
                 + '<input name="unidade" maxlength="20" placeholder="Unidade (ms, pontos…)" aria-label="Unidade">'
                 + sel("esforco", [("baixo", "Esforço baixo"), ("medio", "Esforço médio"), ("alto", "Esforço alto")], "Esforço", "medio"),
                 [("Criar meta", "prim", None, "")])
    return pagina("Metas", metas_tela.tela(form), "/metas", olho="Hoje",
                  sub="Objetivo, medida e alvo. O hub acorda o agente até bater ou travar.")


def _prova_form(acao, x):
    """Formulário de uma linha do cofre: autorizar pede quem e por qual canal; revogar só o motivo."""
    ocultos = f'<input type="hidden" name="produto" value="{e(x["produto"])}"><input type="hidden" name="id" value="{e(x["id"])}">'
    if acao == "autorizar-prova":
        return _form(acao, ocultos + '<input name="por" required maxlength="120" placeholder="Quem autorizou" aria-label="Quem autorizou">'
                     '<input name="canal" required maxlength="120" placeholder="Canal (WhatsApp, e-mail)" aria-label="Canal">',
                     [("Autorizar", "prim", None, "")])
    return _form(acao, ocultos + '<input name="motivo" maxlength="300" placeholder="Motivo (opcional)" aria-label="Motivo">',
                 [("Retirar", "perigo", None, "")])


def provas_pagina(produto):
    return pagina("Provas", provas_tela.tela(produto, _prova_form), "/provas", olho="Negócios",
                  sub="Elogios, números medidos e antes/depois por produto. Nada é publicado sem a sua autorização.")


def virais_pagina(query):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    clonar = lambda vid: _form("clonar-viral", f'<input type="hidden" name="viral" value="{e(vid)}">',
                               [("Clonar para o ClienteJ", "prim", None, "")])
    return pagina("Virais", virais_tela.tela(q, clonar), "/virais", olho="Negócios",
                  sub="O que viralizou no X e no Instagram, e os carrosséis do ClienteJ feitos a partir disso.")


def fabrica_pagina():
    return pagina("Fábrica", fabrica_tela.tela(), "/fabrica", olho="Negócios",
                  sub=f"Produtos da fábrica: gasto contra o teto de {fabrica_tela._rs(fabrica_tela.fabrica.TETO_CENTAVOS)}, "
                      f"janela de {fabrica_tela.fabrica.JANELA_MORTE_DIAS} dias e veredito.")


def destaques_pagina(query):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    return pagina("Destaques", destaques_tela.tela(q), "/destaques", olho="Negócios",
                  sub="O que explodiu na semana: item com 5x a mediana das 4 semanas anteriores.")


def portfolio_pagina(query):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    return pagina("Portfólio", portfolio_tela.tela(q), "/portfolio", olho="Negócios",
                  sub="As propostas da semana ranqueadas por R$ estimado e custo, e a calibração do que já foi medido.")


def _pedir_acao():
    """Botão do topo da tela que abre o <dialog> de _pedir_dialogo()."""
    return ui.botao("Pedir aos Claudes", tipo="prim", abre="pedir")


def _pedir_dialogo():
    return ui.dialogo("pedir", "Pedir aos Claudes", _pedir_form())


def _comentar(issue):
    """'Comentar' recolhido (details.mais do ui) com o formulário real: POST /acao/comentar com CSRF."""
    return '<details class="mais"><summary>Comentar</summary>' + _form(
        "comentar", f'<input type="hidden" name="issue" value="{e(str(issue))}">'
        '<input class="largo" name="texto" required maxlength="5000" placeholder="Comentar…" aria-label="Comentar">',
        [("Enviar", "prim", None, "")]) + "</details>"


NOINDEX = '<meta name="robots" content="noindex">'


def pagina(titulo, corpo, ativo=None, cabeca="", publica=False, **kw):
    """Casca de hub/ui.py com o número de pendências no menu. ativo: caminho do item de menu em destaque.
    cabeca: HTML extra no <head>. publica: sem menu nem pendências (páginas abertas sem senha), com noindex.
    kw: olho, sub, acoes, caminho (vão para ui.pagina)."""
    p = {} if publica else _ck("pendencias") or {}
    n = sum(len(p.get(k) or []) for k in ("pedidos", "travadas", "perguntas"))
    dono = not publica and (sessao_atual() or {}).get("papel") == "dono"
    return ui.pagina(titulo, corpo, ativo, cabeca=cabeca, n_pend=n, publica=publica, dono=dono, **kw)


def usuarios_tela():
    """/config/usuarios (só dono): quem entra, o papel, trocar papel, desativar e convidar. O link de um convite novo
    aparece uma vez, só para o dono que convidou."""
    s = sessao_atual()
    link = ""
    if s:
        with _LINKS_TRAVA:
            link = _LINKS.pop(s["email"], "")
    sel = lambda atual: ('<select name="papel" aria-label="Papel">' + "".join(
        f'<option value="{p}"{" selected" if p == atual else ""}>{p}</option>' for p in usuarios.PAPEIS) + "</select>")
    linhas = []
    for u in usuarios.listar():
        oculto = f'<input type="hidden" name="email" value="{e(u["email"])}">'
        mexer = _form("papel", oculto + sel(u["papel"]), [("Trocar papel", "neutro", None, "")])
        if u["ativo"]:
            mexer += _form("desativar", oculto, [("Desativar", "perigo", None, "")])
        linhas.append([ui.td(u["email"]), ui.td(u["papel"]), ui.td(ui.quando(u["criado"])), "sim" if u["ativo"] else "não",
                       ui.td(mexer, html_=True)])
    corpo = ""
    if link:
        corpo += ui.card("Link do convite", '<p class="mu">Mande para a pessoa. Aparece só agora; vale 24 h e uma vez.</p>'
                         f'<input class="largo" readonly value="{e(link)}" aria-label="Link do convite">')
    corpo += ui.card("Usuários", ui.tabela(["E-mail", "Papel", "Criado", "Ativo", ""], linhas, largas=(4,)),
                     direita='<a href="/config/auditoria">Auditoria</a>')
    corpo += ui.card("Convidar", _form(
        "convidar", '<input name="email" type="text" required maxlength="320" autocapitalize="none" placeholder="E-mail"'
        ' aria-label="E-mail">' + sel("leitura"), [("Convidar", "prim", None, "")]))
    return pagina("Usuários", corpo, "/config/usuarios", olho="Config",
                  sub="Quem entra no painel e o que cada papel pode: leitura vê, operador age, dono configura.")


def auditoria_tela():
    """/config/auditoria (só dono): as últimas 200 escritas. Toda célula por ui.td (escapada): o alvo vem de fora."""
    linhas = [[ui.td(ui.quando(a["em"]), a["em"]), ui.td(a["quem"]), ui.td(a["acao"]), ui.td(a["alvo"]),
               ui.td(a["resultado"]), ui.td(a["ip"])] for a in usuarios.auditoria(200)]
    corpo = ui.card("Últimas 200", ui.tabela(["Quando", "Quem", "Ação", "Alvo", "Resultado", "IP"], linhas, largas=(3, 4))
                    if linhas else '<p class="mu">Nada gravado ainda.</p>')
    return pagina("Auditoria", corpo, "/config/usuarios", olho="Config",
                  sub="Toda escrita do painel: ações, entradas, saídas e convites. Só se acrescenta; nada se apaga.")


def fmt(n):
    return "—" if n is None else f"{n:,.0f}".replace(",", ".")


def _pos(x):
    return "—" if x is None else f"{float(x):.1f}".replace(".", ",")


def _num(v, f=fmt):
    return f(v) if v is not None else "—"


def _kpis_site(sid):
    """(rótulo, métrica, formatador, menor_melhor) → faixa de KPIs de 28 dias."""
    f = fmt
    cfg = [("Cliques", "cliques", f, False), ("Impressões", "impressoes", f, False),
           ("CTR", "ctr", lambda x: f"{x * 100:.2f}%".replace(".", ","), False),
           ("Posição média", "posicao", lambda x: f"{x:.1f}".replace(".", ","), True),
           ("Sessões", "sessoes", f, False), ("Conversões", "eventos", f, False)]
    return [(r, m, fm, mm, total(sid, m), delta(sid, m), [v for _, v in serie(sid, m)], dias(sid, m)) for r, m, fm, mm in cfg]


def _opps(sid):
    o = (_json(dados() / "gsc" / sid / "latest.json") or {}).get("oportunidades")
    return [x for x in o if isinstance(x, dict)] if isinstance(o, list) else []


TIPOS = {"vazamento": ("alerta", "Vazamento", "reescrever título/descrição"), "reforcar": ("ok", "Reforçar", "subir a página"),
         "buraco": ("ruim", "Buraco", "criar página")}


def _link(u):
    u = str(u or "")
    return f'<a href="{e(u)}" rel="noopener">{e(u)}</a>' if u.startswith("https://") else e(u) or "—"


# ---------- cockpit (hub.cockpit: contas, fila, Linear, pendências) ----------

_CK = {}  # limpo a cada requisição em rota(): uma chamada por função e por página, dando certo ou não


def _ck(nome):
    """Dados do cockpit ou None se a fonte caiu (Linear fora do ar etc.): o bloco avisa, a página não dá 500."""
    if nome not in _CK:
        try:
            _CK[nome] = getattr(cockpit, nome)()
        except Exception:  # noqa: BLE001
            _CK[nome] = None
    return _CK[nome]


FORA_CONTAS = ui.erro("Leitura das contas indisponível agora.")
SEM_ORQ = ui.erro("Sem estado do orquestrador ainda.", "O orquestrador grava a cada 10 min.")
NAO_ACHEI = '<p class="mu">Essa página não existe. <a href="/bi">Voltar para a visão geral</a></p>'
SEM_PEDIDO = '<p class="pergunta">Ainda não foi para o WhatsApp — os botões aparecem quando virar Pedido.</p>'
TIPO_PT = {"vps": "servidor", "email": "e-mail", "whatsapp": "WhatsApp", "post_gmb": "post no Google",
           "resposta_avaliacao": "resposta a avaliação", "pr_merge": "juntar código",
           "carrossel": "carrossel no Instagram"}
ESTADO_PT = {"Todo": ("A fazer", "neutro"), "In Progress": ("Em andamento", "ok"), "In Review": ("Em revisão", "alerta"),
             "Backlog": ("Estoque", "neutro"), "Done": ("Feita", "ok")}


def _ha(iso=None, minutos=None):
    """'há 30 min' / 'há 3 h' / 'há 2 dias' a partir de um ISO ou de minutos; vazio se não dá para saber."""
    if minutos is None:
        try:
            minutos = (C.agora() - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds() / 60
        except (TypeError, ValueError):
            return ""
    m = max(0, int(minutos))
    return f"há {m} min" if m < 60 else f"há {m // 60} h" if m < 48 * 60 else f"há {m // 1440} dias"


def _a(url, texto):
    """Link só para https:// (nada de javascript: vindo do Linear); senão, só o texto."""
    u = str(url or "")
    return f'<a href="{e(u)}" rel="noopener">{e(str(texto))}</a>' if u.startswith("https://") else e(str(texto))


def _restante(reset):
    """'volta em 2h 15min' até o reset da janela de 5 h; sem reset, só '5 h'."""
    try:
        seg = (dt.datetime.fromisoformat(str(reset).replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return "5 h"
    if seg <= 0:
        return "voltando agora"
    h, m = int(seg // 3600), int(seg % 3600 // 60)
    if h >= 48:
        return f"volta em {h // 24}d {h % 24}h"
    return f"volta em {h}h {m:02d}min" if h else f"volta em {m}min"


def _barra(rotulo, pct):
    """Cota de uma conta (ui.barra, limiar único); sem leitura, a barra vazia diz isso."""
    if pct is None:
        return ui.barra(rotulo, 0, texto="sem leitura")
    return ui.barra(rotulo, pct)


def _progresso(rotulo, feitas, total_):
    """Barra de plano: feitas/total, na cor de destaque (100% é bom, não alerta como no medidor de uso)."""
    feitas, total_ = feitas or 0, total_ or 0
    return ui.barra(rotulo, feitas, total_, plano=True, texto=f"{feitas}/{total_}")


def _cartao(titulo, faixa="", estado="", faz="", passos=(), cota="", ativa=False, classe=""):
    """Cartão de conta único: nome (+ faixa), estado com selo, o que está fazendo, passos e cota. Textos já escapados."""
    passos = [str(x) for x in passos][-3:]
    lis = "".join(f'<li{" class=atual" if i == len(passos) - 1 else ""}>{e(x)}</li>' for i, x in enumerate(passos))
    return (f'<article class="{classe or "conta"} caixa{" ativa" if ativa else ""}"><header><h3>{e(titulo)}'
            f'{f" <span class=\"faixa\">· {e(faixa)}</span>" if faixa else ""}</h3>{estado}</header>'
            f'{f"<div class=faz>{faz}</div>" if faz else ""}'
            f'{f"<ol class=passos aria-label=\"Últimos passos\">{lis}</ol>" if lis else ""}'
            f'{f"<div class=cotas>{cota}</div>" if cota else ""}</article>')


def _rodando(itens):
    return "".join(f'<p>Rodando: <b>{e(str(r.get("nome")))}</b> {ui.ha(r.get("inicio"))}</p>' for r in itens or []) \
        or '<span class="mu">Livre</span>'


def _cartao_conta(a, urls):
    n = str(a.get("conta", ""))
    if a.get("tarefa"):
        alvo = a.get("issue") or a["tarefa"]
        ha = ui.ha(a.get("desde"), minutos=a.get("minutos"))
        faz = (f'Trabalhando em <b>{_a(urls.get(alvo), alvo)}</b>{" · " + ha if ha else ""}'
               + (f'<span class="mu">{e(str(a["titulo"]))}</span>' if a.get("titulo") else ""))
        estado = ui.selo("Trabalhando", "ok")
    else:
        faz, estado = '<span class="mu">Esperando a próxima tarefa.</span>', ui.selo("Livre")
    return _cartao(f"Conta {n}", a.get("faixa") or "", estado, faz, a.get("ultimos_passos") or [],
                   _barra("5 h · " + _restante(a.get("reset_5h")), a.get("cinco_horas")), bool(a.get("tarefa")),
                   f"conta conta-{e(n)}")


def _contas():
    ag, lr = _ck("agentes"), _ck("linear_resumo") or {}
    if ag is None:
        return FORA_CONTAS
    urls = {i.get("identifier"): i.get("url") for i in lr.get("issues") or []}
    return f'<div class="grade n{len(ag)}">{"".join(_cartao_conta(a, urls) for a in ag)}</div>'


def _cartao_orq(est, cota):
    v = est.get("varredura") or {}
    r, u = v.get("rodando"), v.get("ultima")
    if r:
        estado = ui.selo("Varrendo", "ok")
        faz = f'Varrendo {ui.ha(r.get("desde"))}<span class="mu">{e(str(r.get("motivo") or ""))}</span>'
    else:
        estado = ui.selo("Em espera", "ok")
        faz = f'<span class="mu">Última checagem {ui.ha(v.get("checada_em")) or "—"}.</span>'
    passos = []
    if u:
        passos = [x.strip().lstrip("-* ").replace("**", "") for x in str(u.get("resumo") or "").splitlines() if x.strip()][:3]
        faz += (f'<p>Última varredura {ui.quando(u.get("inicio"))}'
                f'{"" if u.get("codigo") == 0 else " " + ui.selo("código " + str(u.get("codigo")), "ruim")}'
                f'<span class="mu">motivo: {e(str(u.get("motivo") or "—"))}</span></p>')
    else:
        faz += '<span class="mu">Nenhuma varredura nos últimos 2 dias.</span>'
    reset = ((cota or {}).get("cinco_horas") or {}).get("reset")
    return _cartao("Orquestrador", f"conta do {config.dono()}", estado, faz, passos,
                   _med_conta("orch", cota, "cinco_horas", "5 h · " + _restante(reset), True), bool(r), "conta orch")


def _cartao_codex(c):
    h, u = c.get("hoje") or {}, c.get("ultimo")
    modelo = " · ".join(str(x) for x in (c.get("modelo"), c.get("raciocinio") and f'raciocínio {c["raciocinio"]}') if x)
    estado = ui.selo("Login ok", "ok") if c.get("login_ok") else ui.selo("Sem login", "ruim")
    faz = (f'<p><b>{e(str(h.get("revisados", 0)))}</b> PRs revisados hoje · {e(str(h.get("com_achado", 0)))} com achado'
           f' · {e(str(h.get("sem_achados", 0)))} sem achados</p>')
    if not c.get("login_ok"):
        faz += '<span class="mu">Rode codex login --device-auth.</span>'
    if u:
        rotulo = f'{u.get("repo")}#{u.get("pr")}'
        faz += (f'<span class="mu">Último: {_a(u.get("url"), rotulo)} {ui.ha(u.get("em"))} '
                f'{ui.selo("com achado", "alerta") if u.get("achados") else ui.selo("sem achados", "ok")}</span>')
    else:
        faz += '<span class="mu">Nenhuma revisão registrada ainda.</span>'
    return _cartao("Revisor Codex", modelo, estado, faz, (), _cotas_codex(c.get("semana")), False, "conta codex")


def _cartao_codex_executor(c):
    if not c.get("login_ok"):
        estado = ui.selo("Sem login", "ruim")
    elif c.get("pausa"):
        estado = ui.selo("Pausado até " + ui.quando(c["pausa"]), "alerta")
    else:
        estado = ui.selo("Rodando", "ok") if c.get("rodando") else ui.selo("Livre")
    h = c.get("hoje") or {}
    faz = (_rodando(c.get("rodando"))
           + ('' if c.get("login_ok") else '<span class="mu">Rode codex login --device-auth (usuário claude).</span>')
           + f'<span class="mu">{e(str(c.get("na_fila", 0)))} na fila · teto {e(str(c.get("vagas", 2)))}</span>'
           f'<span class="mu">Hoje: {e(str(h.get("total", 0)))} execuções · {e(str(h.get("ok", 0)))} ok · '
           f'{e(str(h.get("falhas", 0)))} falhas · {e(str(h.get("transbordos", 0)))} transbordos · {e(str(h.get("merges", 0)))} merges</span>')
    sem = c.get("semana")
    if sem and sem.get("pct") is not None:
        faz += f'<span class="mu">Meta: ~80% da semana (KRN-866) · usado {e(str(round(sem["pct"])))}%</span>'
    return _cartao("Executor Codex", "faixa codex", estado, faz, (), _cotas_codex(sem), bool(c.get("rodando")),
                   "conta codex")


def _cartao_reserva_api(c):
    if c.get("pausa"):
        estado = ui.selo("Pausada até " + ui.quando(c["pausa"]), "alerta")
    elif c.get("bloqueio"):
        estado = ui.selo("Parada: " + str(c["bloqueio"]), "alerta")
    elif c.get("oauth_esgotadas"):
        estado = ui.selo("Em uso: contas OAuth no limite", "alerta")
    else:
        estado = ui.selo("De reserva", "ok")
    usd = lambda v: ui.num(v, moeda="US$", casas=2)  # noqa: E731
    # uma faixa por reserva da lista, na ordem de uso: saldo, gasto do dia e do período
    faixas = ""
    for r in (c.get("reservas") or ()) if len(c.get("reservas") or ()) > 1 else ():
        uso = " · em uso" if r["id"] == c.get("em_uso") else ""
        faixas += (f'<span class="mu"><b>{e(r["rotulo"])}</b>{uso}: saldo {usd(r["saldo"])}'
                   f' · {"mês" if r["mensal"] else "total"} {usd(r["total"])} de {usd(r["teto_total"])}'
                   f' · hoje {usd(r["hoje"])} de {usd(r["teto_dia"])}'
                   + (f' · {e(str(r["bloqueio"]))}' if r.get("bloqueio") else "") + "</span>")
    faz = (_rodando(c.get("rodando")) + (faixas or f'<span class="mu">Saldo estimado: {usd(c["saldo"])}</span>')
           + f'<span class="mu">Execuções hoje: {e(str(c["execucoes_hoje"]))}</span>')
    cota = (ui.barra("Hoje", c["hoje"], c["teto_dia"], texto=f'{usd(c["hoje"])} de {usd(c["teto_dia"])}')
            + ui.barra("Total", c["total"], c["teto_total"], texto=f'{usd(c["total"])} de {usd(c["teto_total"])}'))
    return _cartao("Reserva API", "chaves de API", estado, faz, (), cota, bool(c.get("rodando")), "conta api")


def _cotas_codex(s):
    """Codex tem limite semanal (o Claude não): barra com o tempo até voltar."""
    if not s or s.get("pct") is None:
        return ""
    return _barra("Semana · " + _restante(s.get("reset")), s["pct"])


_LOGIN_CODEX = (0.0, False)  # (monotonic, resultado): o fragmento atualiza a cada 15 s por aba


def _login_codex_rapido():
    global _LOGIN_CODEX
    if time.monotonic() - _LOGIN_CODEX[0] > 60 or not _LOGIN_CODEX[0]:
        _LOGIN_CODEX = (time.monotonic(), agenda.codex_com_login(timeout=2))
    return _LOGIN_CODEX[1]


def _semana_codex(*medidas):
    """Executor e revisor são a mesma conta: vale a medida da semana mais nova (reset mais tarde, depois o maior %)."""
    ok = [m for m in medidas if m and m.get("pct") is not None]
    return max(ok, key=lambda m: (m.get("reset") or "", m["pct"]), default=None)


def _roteamento():
    """a tabela de modelo por tipo de trabalho (hub.json → roteamento)."""
    return '<p class="mu">Roteamento de modelo: ' + " · ".join(f"{e(m)} ({e(p)})" for m, p in roteamento.tabela()) + "</p>"


def _orquestrador(login_fn=None):
    est = _json(dados() / "orch" / "estado.json")
    # login com timeout curto: o painel nunca trava no `codex login status`
    c = agenda.estado_codex_executor(login_fn=login_fn or (lambda: _login_codex_rapido()))
    c["semana"] = _semana_codex(c.get("semana"), ((est if isinstance(est, dict) else {}).get("codex") or {}).get("semana"))
    exe = _cartao_codex_executor(c)
    api = _cartao_reserva_api(agenda.estado_reserva_api())
    if not isinstance(est, dict):
        return f'<div class="grade n2">{exe}{api}</div>' + _roteamento() + SEM_ORQ
    cota = (_json(dados() / "claude" / "uso-contas.json", {}) or {}).get("orquestrador")
    return (f'<div class="grade n4">{_cartao_orq(est, cota)}{_cartao_codex(est.get("codex") or {})}{exe}{api}</div>'
            f'{_roteamento()}<p class="mu">Estado gravado {ui.ha(est.get("em")) or "—"}.</p>')


def _planos(lr):
    ps = lr.get("planos") or []
    return "".join(_progresso(p.get("label", ""), p.get("feitas", 0), p.get("total", 0)) for p in ps) or '<p class="mu">Nenhum plano aberto.</p>'


def _kpi_contas(ag):
    """Número principal das telas de máquina: quantas contas Claude estão trabalhando agora."""
    if ag is None:
        return ui.kpi("Contas trabalhando", "—", quente=True, nota="Leitura das contas indisponível agora.")
    n = sum(1 for a in ag if a.get("tarefa"))
    return ui.kpi("Contas trabalhando", f"{n} de {len(ag)}", quente=True, contar=None,
                  nota="Todas livres." if not n else "")


SELO_SALDO = {"ok": ("Com saldo", "ok"), "baixo": ("Baixo", "alerta"), "zerado": ("Zerado", "alerta"),
              "indisponivel": ("Sem leitura", "neutro")}


def _cartao_saldos(est):
    """saldo das APIs pagas (coletor hub.saldos, a cada 30 min)."""
    from hub import saldos
    sv = (est or {}).get("servicos") or {}
    if not sv:
        return ui.card("Saldos", '<p class="mu">Coletor de saldos ainda não rodou.</p>')
    usd = lambda v: ui.num(v, moeda="US$", casas=2)  # noqa: E731
    linhas = []
    for k, rot in saldos.ROTULO.items():
        s = sv.get(k)
        if not s:
            continue
        texto, tom = SELO_SALDO.get(s.get("estado"), SELO_SALDO["indisponivel"])
        if k == "openai":
            fx = s.get("faixas") or {}
            det = " · ".join(f"{e(str(n))} {e(str(f.get('usado', 0)))}/{e(str(f.get('cota', '?')))}" for n, f in fx.items()) or e(s.get("erro") or "—")
        else:
            det = f'saldo {usd(s["saldo"])}' if s.get("saldo") is not None else e(s.get("erro") or "—")
            if s.get("gasto_hoje") is not None:
                det += f' · gasto hoje {usd(s["gasto_hoje"])}'
        linhas.append(f'<p>{ui.selo(texto, tom)} <strong>{e(rot)}</strong> <span class="mu">{det}</span></p>')
    return ui.card("Saldos", "".join(linhas) + f'<p class="mu">Lido {ui.ha(est.get("em")) or "—"}.</p>')


def agora_fragmento():
    """Miolo da tela "Agora" (o JS troca o innerHTML de #agora com isto a cada 15 s)."""
    pe, lr = _ck("pendencias"), _ck("linear_resumo")
    if pe is None:
        k_pend = ui.kpi("Pendências", "—", nota=e("Linear indisponível agora."))
    else:
        n = sum(len(pe.get(k) or []) for k in ("pedidos", "travadas", "perguntas"))
        decidivel = any(isinstance(p.get("n"), int) for p in pe.get("pedidos") or [])  # só pedido com número tem botões
        k_pend = ui.kpi("Pendências esperando você", n, href="/pendencias", tom="alerta" if n else None,
                        nota="Ver e decidir →" if n and decidivel else "Ver →" if n else "Nada esperando você.")
    if lr is None:
        k_plan = ui.kpi("Planos abertos", "—", nota=e("Linear indisponível agora."))
    else:
        ps = lr.get("planos") or []
        k_plan = ui.kpi("Planos abertos", len(ps), nota=f'{sum(p.get("feitas") or 0 for p in ps)} de '
                                                         f'{sum(p.get("total") or 0 for p in ps)} tarefas feitas' if ps else "")
    return (ui.kpis(_kpi_contas(_ck("agentes")), k_pend, k_plan) + _contas()
            + _cartao_saldos(_json(dados() / "saldos" / "estado.json", {}))
            + ui.card("Planos", ui.erro("Linear indisponível agora.") if lr is None else _planos(lr)))


def agentes_fragmento():
    fila = _ck("fila")
    nomes = [str(f.get("issue") or f.get("nome") or "") for f in fila or []]
    if fila is None:
        corpo, k_fila = ui.erro("Leitura da fila indisponível agora."), ui.kpi("Na fila", "—")
    else:
        corpo = '<div class="chips">' + "".join(ui.selo(x, mono=True) for x in nomes) + "</div>" if nomes else '<p class="mu">Fila vazia.</p>'
        k_fila = ui.kpi("Na fila", len(nomes), tom="alerta" if nomes else None)
    return (ui.kpis(_kpi_contas(_ck("agentes")), k_fila) + ui.card("Contas Claude", _contas(), caixa=False)
            + ui.card("Orquestrador e Codex", _orquestrador(), caixa=False)
            + ui.card("Na fila", corpo, conta=len(nomes) if nomes else None))


def agentes_tela():
    return pagina("Agentes", f'<div id="agentes" data-recarrega="/agentes.fragmento">{agentes_fragmento()}</div>', "/agentes",
                  olho="Máquina", sub="Quem está rodando, quem está parado e o que espera na fila.",
                  acoes=ui.botao("Configurar contas", href="/config/agentes"))


ORDEM_ESTADO = {"In Progress": 0, "In Review": 1, "Todo": 2, "Backlog": 3}


def _linear_uso_card():
    """Chamadas por chave do Linear na hora e o pico das últimas 24 h (alarme ao orquestrador acima de 70%)."""
    try:
        u = linear_uso.uso()
    except Exception:  # noqa: BLE001
        return ""
    if not u:
        return ""
    nomes = {"app2": "claude2 (tick, contas, Codex)", "fulano": f"chave do {config.dono()} (reserva)", "app": "app Claude (comentários)"}
    barras = "".join(ui.barra(nomes.get(k, k), round(v["pct_hora"] * 100), 100, plano=True,
                              texto=f'{v["hora"]} nesta hora · pico {v["pico_pct"]:.0%}') for k, v in sorted(u.items()))
    return ui.card("Uso da API do Linear", barras)


def linear_tela():
    lr = _ck("linear_resumo")
    sub = "O que os Claudes estão fazendo, o que espera revisão e o que travou."
    if lr is None:
        return pagina("Linear", ui.erro("Linear indisponível agora.", "As issues voltam quando o Linear responder.") + _pedir_dialogo(),
                      "/linear", olho="Máquina", sub=sub, acoes=_pedir_acao())
    por_estado, pe = lr.get("por_estado") or {}, _ck("pendencias")
    travadas = len((pe or {}).get("travadas") or []) if pe is not None else None
    corpo = ui.kpis(ui.kpi("Em andamento", ui.num(por_estado.get("In Progress", 0)), quente=True),
                    ui.kpi("Em revisão", ui.num(por_estado.get("In Review", 0))),
                    ui.kpi("Travadas", ui.num(travadas), tom="ruim" if travadas else None))
    corpo += _linear_uso_card()
    por_proj = collections.defaultdict(list)
    for i in lr.get("issues") or []:
        por_proj[i.get("projeto") or "Sem projeto"].append(i)
    for proj, iss in sorted(por_proj.items()):
        iss = sorted(iss, key=lambda i: ORDEM_ESTADO.get(i.get("estado"), 9))
        linhas = [[ui.td(ui.selo(*ESTADO_PT.get(i.get("estado"), (i.get("estado") or "—", "neutro"))), html_=True, rotulo="Estado"),
                   ui.td(_a(i.get("url"), i.get("identifier", "")), html_=True, rotulo="Issue"),
                   ui.td(f'{e(str(i.get("titulo", "")))}{_comentar(i.get("identifier", ""))}', html_=True, rotulo="Título"),
                   ui.td(ui.ha(i.get("atualizada")) or "—", rotulo="Atualizada")] for i in iss]
        corpo += ui.card(proj, ui.tabela(["Estado", "Issue", "Título", "Atualizada"], linhas, celular="pilha"),
                         conta=len(iss), justa=True)
    if not por_proj:
        corpo += ui.card("Issues", ui.vazio("Nenhuma issue aberta.", "Quando o Linear tiver trabalho, ele aparece aqui."))
    planos = lr.get("planos") or []
    if planos:
        corpo += ui.card("Planos", "".join(ui.barra(p.get("label", ""), p.get("feitas", 0), p.get("total", 0) or 1, plano=True,
                                                    texto=f'{p.get("feitas", 0)}/{p.get("total", 0)}') for p in planos))
    return pagina("Linear", corpo + _pedir_dialogo(), "/linear", olho="Máquina", sub=sub, acoes=_pedir_acao())


def _pendencias_fulano():
    """lista única do que ficou para o dono em issues já concluídas (token, conta, decisão); sem WhatsApp."""
    lista = portao_done.pendencias_fulano()
    itens = [{"titulo": f'{ident} · {v.get("titulo", "")}', "fala": v.get("pendencia", ""), "meta": f'<span>{e(ui.ha(v.get("desde")) or "—")}</span>'}
             for ident, v in sorted(lista.items())]
    return ui.card(f"Pendências do {config.dono()}", ui.lista(itens) if itens else ui.vazio("Nada pendente em issue concluída."), conta=len(itens),
                   caixa=False, id_="pendencias-fulano")


def pendencias_tela():
    pe = _ck("pendencias")
    if pe is None:
        return pagina("Pendências", ui.erro("Linear indisponível agora.", "Tente de novo em instantes."), "/pendencias", olho="Hoje")
    sem_linear = ui.erro("Linear indisponível agora.", "Não deu para ler as issues.") if _ck("linear_resumo") is None \
        else ui.vazio("Nada esperando você.")
    peds, trav, perg = pe.get("pedidos") or [], pe.get("travadas") or [], pe.get("perguntas") or []

    def pedido(p):
        n = p.get("n")
        it = {"titulo": p.get("resumo", ""), "href": f"/proposta/{urllib.parse.quote(str(p.get('id', '')))}",
              "meta": ui.selo(TIPO_PT.get(p.get("tipo"), str(p.get("tipo", ""))), "alerta")
              + (f"<span>Pedido #{e(str(n))}</span>" if n is not None else "") + f"<span>{e(ui.ha(p.get('criada_em')) or '—')}</span>"}
        if isinstance(n, int):
            it["acoes_id"] = str(p.get("id", ""))
            it["acoes"] = _form("pedido", f'<input type="hidden" name="n" value="{e(str(n))}">',
                                [("Não fazer", "perigo", "decisao", "nao"), ("Fazer", "prim", "decisao", "faz")])
        else:
            it["fala"] = "Ainda não foi para o WhatsApp — os botões aparecem quando virar Pedido."
        return it

    def issue(i, campo):
        it = {"titulo": i.get("titulo", ""), "meta": (ui.selo(i.get("motivo"), "ruim") if campo == "motivo" and i.get("motivo") else "")
              + _a(i.get("url"), i.get("identifier", ""))}
        if campo == "pergunta":
            it["fala"] = i.get("pergunta", "")
        it["mais"] = _comentar(i.get("identifier", "")).replace('class="comentar"', 'class="mais"', 1)
        return it

    ag = _ck("agentes") or []
    ativos = sum(1 for a in ag if a.get("tarefa"))
    hoje = dt.datetime.now(C.BRT).date().isoformat()
    props = _lista_propostas()
    n_hoje = sum(1 for p in props if _quando(p.get("criada_em")).startswith(hoje))
    total = len(peds) + len(trav) + len(perg)
    faixa = ui.kpis(
        ui.kpi("Esperando você", ui.num(total), nota="pedidos, travadas e perguntas", quente=bool(total), contar=total),
        ui.kpi("Pedidos", ui.num(len(peds)), nota="precisam do seu ok", contar=len(peds)),
        ui.kpi("Agentes", ui.num(ativos), nota=f"trabalhando de {len(ag)} contas", contar=ativos),
        ui.kpi("Propostas hoje", ui.num(n_hoje), nota="criadas pelos agentes", contar=n_hoje))
    try:
        nave = metas_tela.faixa()
    except Exception:  # a faixa é extra: nunca derruba as Pendências
        nave = ""
    corpo = faixa + nave + ui.card("Cérebro", '<iframe class="cerebro-mini" src="/cerebro?compacto=1" loading="lazy" '
                                   'title="Mapa da operação ao vivo"></iframe>', direita='<a href="/cerebro">abrir →</a>',
                                   justa=True, id_="cerebro")
    corpo += ui.card("Pedidos esperando seu ok", ui.lista([pedido(p) for p in peds]) if peds
                     else ui.vazio("Nada esperando você.", "Pedidos novos aparecem aqui."), conta=len(peds), caixa=False, id_="pedidos")
    for titulo, chave, campo, xs, ok in (("Travadas", "travadas", "motivo", trav, "Nenhuma travada. Tudo andando."),
                                          ("Perguntas dos agentes", "perguntas", "pergunta", perg,
                                           "Nenhuma pergunta. Os agentes seguem sozinhos.")):
        corpo += ui.card(titulo, ui.lista([issue(i, campo) for i in xs]) if xs else sem_linear, conta=len(xs), caixa=False, id_=chave)
    corpo += _pendencias_fulano()
    corpo += ui.card("Propostas recentes", _tabela_propostas(props[:8]) if props else ui.vazio("Nenhuma proposta ainda."),
                     direita='<a href="/propostas">ver todas →</a>', justa=True, id_="recentes")
    return pagina("Pendências", corpo, "/pendencias", olho="Hoje", sub="O que precisa de você agora.")


def visao_geral():
    linhas = []
    abertas = [p for p in _lista_propostas() if p.get("estado") in ("pendente", "enviada", "aprovada")]
    for s in S.carregar():
        sid, v = s["id"], verificador(s["id"])
        # shortcut: casa o id do site como token entre traços em id/tarefa; some se as propostas ganharem campo `site`
        nprop = sum(1 for p in abertas if any(f"-{sid}-" in f"-{p.get(k, '')}-" for k in ("id", "tarefa")))
        pad = (ui.selo(f'{v["ok"]}/{v["total"]}', "ok" if v["ok"] == v["total"] else "ruim") if v
               else '<span class="mu">—</span>')
        cont = {t: 0 for t in TIPOS}
        for o in _opps(sid):
            if o.get("tipo") in cont:
                cont[o["tipo"]] += 1
        d, cl, ss, ev = delta(sid, "cliques"), total(sid, "cliques"), total(sid, "sessoes"), total(sid, "eventos")
        cob = lambda t, m: "" if t is None or dias(sid, m) >= 28 else f'<span class="mu">{dias(sid, m)} dias</span>'  # noqa: E731
        sp = ui.grafico([[x for _, x in serie(sid, "cliques")]], tipo="spark")
        linhas.append([ui.td(f'<a href="/site/{e(sid)}">{e(s["nome"])}</a><div class="mu">{e(s["dominio"])}'
                             f'{"" if s["ativo"] else " · inativo"}</div>', html_=True),
                       ui.td(pad, html_=True),
                       ui.td(ui.num(cl) + (" " + cob(cl, "cliques") if cob(cl, "cliques") else ""), cl, n=True, html_=True),
                       ui.td(ui.variacao(d) if d is not None else "—", d, n=True, html_=True),
                       ui.td(sp or "—", html_=True),
                       ui.td(ui.num(ss) + (" " + cob(ss, "sessoes") if cob(ss, "sessoes") else ""), ss, n=True, html_=True),
                       ui.td(ui.num(ev) + (" " + cob(ev, "eventos") if cob(ev, "eventos") else ""), ev, n=True, html_=True),
                       ui.td(nprop or "—", nprop, n=True),
                       ui.td(" ".join(ui.selo(f"{n} {TIPOS[t][1].lower()}", TIPOS[t][0]) for t, n in cont.items() if n)
                             or '<span class="mu">—</span>', html_=True)])
    snap = ultimo_snapshot()
    linha = resumo.esteira_do_arquivo(dados(), dt.datetime.now(dt.timezone.utc))
    corpo = (f'<div id="agora" data-recarrega="/agora.fragmento">{agora_fragmento()}</div>'
             + (f'<p class="mu">{e(linha)}</p>' if linha else "") + _pedir_dialogo())
    corpo += card_dataforseo()
    corpo += ui.card("Sites", ui.tabela(
        ["Site", "Padrão", "Cliques", "Δ 7d", "Tendência", "Sessões", "Conversões", "Propostas", "Oportunidades"],
        linhas, num=(2, 3, 5, 6, 7), ord_=True, celular="some:1,4,5,6,8", classe="sites"),
        direita=f"até 28 dias · coleta {e(f'{snap.name[8:10]}/{snap.name[5:7]}' if snap and len(snap.name) == 10 else '—')}", justa=True)
    corpo += ui.card("Propostas em aberto", _tabela_propostas(abertas) if abertas else '<p class="mu">Nenhuma.</p>',
                     conta=len(abertas))
    return pagina("Agora", corpo, "/operacao", olho="Operação", sub="O que cada Claude está fazendo agora?",
                  acoes=_pedir_acao())


def _dia(iso):
    """'2026-10-14' (ou ISO com hora) → '14/10'; o que não for data volta como veio."""
    try:
        return f"{dt.date.fromisoformat(str(iso)[:10]):%d/%m}"
    except ValueError:
        return str(iso or "—")


def _janela_txt(j):
    """Janela de medição da oportunidade: texto, ou o registro de mudanças.jsonl (julga em dd/mm, com o PR)."""
    if isinstance(j, dict):
        pr = j.get("pr")
        return f"julgar em {e(_dia(j.get('julgar_em')))}" + (f' · <a href="{e(pr)}">PR</a>' if str(pr).startswith("https://") else "")
    return e(str(j or "ainda sem janela"))


def _tabela_mudancas(ms):
    if not ms:
        return ui.vazio("Nenhuma mudança registrada.")
    return ui.tabela(["Data", "Site", "Página", "Tipo", "Julgar em"],
                     [[ui.td(_dia(m.get("data", "")), m.get("data", ""), classe="nw"), ui.td(m.get("site", "")),
                       ui.td(e(str(m.get("pagina", ""))) + (f' · <a href="{e(m["pr"])}">PR</a>' if str(m.get("pr", "")).startswith("https://") else ""),
                             html_=True),
                       ui.td(m.get("tipo", "")), ui.td(_dia(m.get("julgar_em", "")), m.get("julgar_em", ""), classe="nw")]
                      for m in reversed(ms)], celular="some:1,3")


def _tabela_opps(opps, tipo):
    cols = {"vazamento": ["Busca", "Página", "Impressões", "Cliques", "Posição", "CTR", "Esperado", "Perdidos"],
            "reforcar": ["Busca", "Página", "Impressões", "Cliques", "Posição"],
            "buraco": ["Busca", "Páginas que aparecem", "Impressões", "Cliques", "Posição"]}[tipo]
    pct = lambda x: ui.num(None if x is None else float(x) * 100, casas=1, pct=True)  # noqa: E731
    n = lambda v: ui.td(ui.num(v), v, n=True)  # noqa: E731
    linhas = []
    for o in (x for x in opps if x.get("tipo") == tipo):
        pg = o.get("pagina") if tipo != "buraco" else " ".join(map(str, o.get("paginas_que_aparecem") or []))
        cel = [ui.td(o.get("busca", "")), ui.td(_link(pg) if tipo != "buraco" else e(pg) or "—", html_=True),
               n(o.get("impressoes")), n(o.get("cliques")), ui.td(_pos(o.get("posicao")), o.get("posicao"), n=True)]
        if tipo == "vazamento":
            cel += [ui.td(pct(o.get("ctr")), o.get("ctr"), n=True), ui.td(pct(o.get("ctr_esperado")), o.get("ctr_esperado"), n=True),
                    n(o.get("cliques_perdidos"))]
        linhas.append(cel)
    if not linhas:
        return ui.vazio("Nenhum por enquanto.")
    return ui.tabela(cols, linhas, num=range(2, len(cols)), ord_=True, celular="some:1,3,6" if tipo == "vazamento" else "some:3")


def site(sid):
    s = next((x for x in S.carregar() if x["id"] == sid), None)
    if not s:
        return None
    v, opps = verificador(sid), _opps(sid)
    corpo = '<div class="filtros">' + ui.filtro("site", {x["id"]: x["nome"] for x in S.carregar()}, sid, rotulo="Site", classe="muitos",
                                                 url=lambda i: f"/site/{i}") + "</div>"
    corpo += ui.kpis(*(ui.kpi(f"{r} · {min(n, 28) if t is not None else 28} dias", "—" if t is None else fm(t), delta=d, serie=sp,
                              menor_melhor=mm, comp="vs. 7 d", quente=m == "cliques")
                       for r, m, fm, mm, t, d, sp, n in _kpis_site(sid)))
    # todos na mesma unidade (impressões); o maior de cada tipo presente tem vaga garantida, o resto completa as 5
    cand = sorted((x for x in opps if x.get("tipo") in TIPOS), key=lambda x: -(x.get("impressoes") or 0))
    escolha = list({x["tipo"]: x for x in reversed(cand)}.values())
    escolha += [x for x in cand if all(x is not y for y in escolha)][:5 - len(escolha)]
    top = ui.lista([{
        "titulo": f'{TIPOS[o["tipo"]][2]}: {o.get("busca", "")}',
        "meta": (f'{ui.selo(TIPOS[o["tipo"]][1], TIPOS[o["tipo"]][0])}<span>{_link(o.get("pagina")) if o.get("pagina") else "sem página"}</span>'
                 f'<span>{ui.num(o.get("impressoes"))} impressões · posição {_pos(o.get("posicao"))}</span>'
                 f'<span>medição: {_janela_txt(o.get("janela"))}</span>')}
        for o in sorted(escolha, key=lambda x: -(x.get("impressoes") or 0))], solta=True) if escolha else ui.vazio("Nenhuma oportunidade.")
    corpo += ui.card("O que fazer agora", top)
    for t, nome in (("vazamento", "Vazamentos: já na 1ª página, mas o resultado não convence"),
                    ("reforcar", "Reforçar: a página existe, falta subir"), ("buraco", "Buracos: falta página")):
        corpo += ui.card(nome, _tabela_opps(opps, t), conta=sum(1 for o in opps if o.get("tipo") == t), justa=True)
    if v:
        falhas = ui.lista([{"titulo": f'{i.get("n", "")}. {i.get("item", "")}', "meta": f'<span>{e("; ".join(i.get("falhas") or []))}</span>'}
                           for i in v["falhas"]], solta=True)
        corpo += ui.card("Padrão SEO", ui.barra(f'{v["ok"]} de {v["total"]} itens ok · {_dia(v["data"])}', v["ok"], max(v["total"], 1),
                                               limites=(0, 101), plano=not v["falhas"], texto=f'{v["ok"]}/{v["total"]}')
                         + (f'<div class="seo-mais">{falhas}</div>' if v["falhas"] else ""))
    c = ciclo_aio(sid)
    corpo += ui.card("Respostas de IA" + (f" · {c.stem}" if c else ""), ui.md(_texto(c) if c else ""))
    corpo += ui.card("Mudanças e janelas", _tabela_mudancas([m for m in _jsonl(dados() / "mudancas.jsonl", 500)
                                                              if m.get("site") == sid][-20:]), justa=True)
    return pagina(s["nome"], corpo, "/operacao", olho=f'Site · {s["dominio"]}',
                  sub="O que mexer neste site agora para ganhar cliques no Google.")


def _lista_propostas(limite=200):
    saida = []
    for arq in sorted((dados() / "propostas").glob("*.json"), reverse=True)[:limite]:
        p = _json(arq)
        if isinstance(p, dict):
            saida.append(p)
    return saida


ESTADO_TOM = {"pendente": "alerta", "enviada": "neutro", "aprovada": "ok", "feita": "ok", "recusada": "neutro", "falhou": "ruim"}


POR_PAGINA = 50


def _propostas(estado=None, pagina_n=1):
    ativo = estado if estado in P.ESTADOS else None
    ps = _lista_propostas(None)
    if ativo:
        ps = [p for p in ps if p.get("estado") == ativo]
    paginas = max(1, -(-len(ps) // POR_PAGINA))
    pg = min(max(pagina_n, 1), paginas)
    base = "/propostas" + (f"?estado={ativo}" if ativo else "")
    link = lambda n: base + (("&" if ativo else "?") + f"pagina={n}" if n > 1 else "")  # noqa: E731
    filtro = ui.filtro("estado", [("", "Todas")] + [(x, x) for x in P.ESTADOS], ativo or "", rotulo="Estado",
                       url=lambda v: "/propostas" + (f"?estado={v}" if v else ""))
    nav = ""
    if paginas > 1:
        ant = ui.botao("← Anterior", href=link(pg - 1)) if pg > 1 else ""
        prox = ui.botao("Próxima →", href=link(pg + 1)) if pg < paginas else ""
        nav = f'<nav class="pag" aria-label="Páginas"><span>{ant}</span><span>Página {pg} de {paginas}</span><span>{prox}</span></nav>'
    corpo = filtro + ui.card(f"{ui.num(len(ps))} propostas" + (f" · {ativo}" if ativo else ""),
                             _tabela_propostas(ps[(pg - 1) * POR_PAGINA:pg * POR_PAGINA]) if ps else ui.vazio("Nenhuma proposta aqui."),
                             justa=True) + nav
    return pagina("Propostas", corpo, "/propostas", olho="Hoje", sub="O que os agentes propuseram, da mais recente para a mais antiga.")


def _tabela_propostas(ps):
    """Quando e Tipo somem no celular: o Resumo (2 linhas) e o Estado ficam legíveis."""
    return ui.tabela(["Quando", "Tipo", "Resumo", "Estado"],
                     [[ui.td(ui.quando(p.get("criada_em")), classe="q"),
                       ui.td(ui.selo(TIPO_PT.get(p.get("tipo"), str(p.get("tipo") or "—"))), html_=True),
                       ui.td(f'<a class="resumo-2" href="/proposta/{e(urllib.parse.quote(str(p.get("id", ""))))}">'
                             f'{e(str(p.get("resumo", "")))}</a>', classe="r", html_=True),
                       ui.td(ui.selo(p.get("estado", "—"), ESTADO_TOM.get(p.get("estado"), "neutro")), html_=True)] for p in ps],
                     celular="some:0,1")


def proposta(pid):
    if not P.RE_ID.match(pid):
        return None
    p = _json(dados() / "propostas" / f"{pid}.json")
    if not isinstance(p, dict):
        return None
    destino = str(p.get("destino", ""))
    destino_html = f'<a href="{e(destino)}" rel="noopener">{e(destino)}</a>' if destino.startswith("https://") else e(destino or "—")
    est = str(p.get("estado", "—"))
    cab = ui.kv([("Tarefa", p.get("tarefa") or "—"), ("Tipo", TIPO_PT.get(p.get("tipo"), str(p.get("tipo") or "—"))),
                 ("Criada", ui.quando(p.get("criada_em")) or "—")])
    cab = cab.replace("</dl>", f"<dt>Destino</dt><dd>{destino_html}</dd></dl>")
    corpo = ui.card("Resumo", f'<p>{e(str(p.get("resumo", "")))}</p>{cab}', direita=ui.selo(est, ESTADO_TOM.get(est, "neutro")))
    corpo += ui.card("Conteúdo", ui.md(str(p.get("conteudo", ""))))
    if p.get("como_executar"):
        corpo += ui.card("Como executar", ui.md(str(p["como_executar"])))
    hist = "".join(f'<li>{e(ui.quando(h.get("em")))} · {e(str(h.get("estado", "")))} {e(str(h.get("nota", "")))}</li>'
                   for h in p.get("historico") or [])
    if hist:
        corpo += ui.card("Histórico", f"<ul>{hist}</ul>")
    return pagina("Proposta " + pid, corpo, "/propostas", olho="Hoje", acoes=ui.botao("← Propostas", href="/propostas"))


def _kv(d):
    """Lista chave → valor legível (dict/lista aninhados viram texto separado por vírgula, nunca JSON)."""
    def v(x):
        if isinstance(x, dict):
            return ", ".join(f"{k}: {v(y)}" for k, y in x.items()) or "—"
        if isinstance(x, (list, tuple)):
            return ", ".join(v(y) for y in x) or "—"
        return "sim" if x is True else "não" if x is False else "—" if x in (None, "") else str(x)
    return ('<dl class="kv">' + "".join(f"<dt>{e(str(k))}</dt><dd>{e(v(x))}</dd>" for k, x in d.items()) + "</dl>"
            if d else '<p class="mu">Nada.</p>')


def _quando(iso):
    """ISO com fuso → 'AAAA-MM-DD HH:MM' em Brasília; sem fuso, como veio."""
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo:
            return e(f"{t.astimezone(C.BRT):%Y-%m-%d %H:%M}")
    except ValueError:
        pass
    return e(str(iso or "")[:16].replace("T", " "))


def _pausas():
    """Um aviso âmbar por conta em pausa (limite de uso atingido)."""
    out = ""
    for n in C.contas_config():
        pausa = C.pausa_ativa(dados(), C.agora(), n)
        if pausa:
            out += ui.erro(f"Em pausa · conta {n}", f"até {ui.quando(pausa['ate'])}")
    return out


def _tokens_execucao(u):
    return sum(v for v in (u.get("tokens") or {}).values() if isinstance(v, (int, float)))


def _consumo_execucao(u):
    if u.get("conta") == "api":
        return ui.num(u.get("custo_usd") or 0, moeda="US$")
    return ui.num(_tokens_execucao(u)) + " tokens"


def _cache_prompt(usos):
    """Taxa por tokens de entrada, sem contar saída; CLI e API normalizadas no registro."""
    grupos = collections.defaultdict(lambda: [0, 0, 0])
    for u in usos:
        if "codex" in str(u.get("origem", "")).lower():
            continue
        if "claude" not in str(u.get("modelo", "")).lower() and u.get("origem") == "openrouter":
            continue
        t = u.get("tokens") or {}
        entrada, leitura, escrita = (t.get(k) or 0 for k in ("entrada", "cache_leitura", "cache_escrita"))
        if entrada + leitura + escrita:
            g = grupos[u.get("nome") or u.get("origem") or "Claude"]
            g[0] += entrada + leitura + escrita
            g[1] += leitura
            g[2] += escrita
    linhas = [[ui.td(nome), ui.td(f"{100 * l / total:.1f}%", n=True),
               ui.td(ui.num(l), n=True), ui.td(ui.num(e), n=True)]
              for nome, (total, l, e) in sorted(grupos.items())]
    return ui.card("Cache de prompt", (ui.tabela(["Rota", "Entrada lida do cache", "Tokens lidos", "Tokens gravados"], linhas)
                   if linhas else ui.vazio("Sem tokens de entrada registrados."))
                   + '<p class="mu">Meta nas rotas repetitivas: mais de 60%. Amostra recente; entrada = novos + leitura + escrita. '
                     'Prefixos abaixo do mínimo do modelo podem não gerar cache.</p>')


def _execucoes():
    usos = _jsonl(dados() / "claude" / "uso.jsonl", 40)
    dia = lambda u: _quando(u.get("inicio"))[:10]  # noqa: E731  (data de Brasília, não UTC)
    hoje = dia(usos[-1]) if usos else ""
    tokens = sum(_tokens_execucao(u) for u in usos if dia(u) == hoje and u.get("conta") != "api")
    falhas = sum(1 for u in usos if not u.get("ok"))
    linhas = [[ui.td(ui.quando(u.get("inicio")), classe="mu", rotulo="Início"),
               ui.td(str(u.get("nome", "")), rotulo="Trabalho"), ui.td(str(u.get("modelo", "")), rotulo="Modelo"),
               ui.td(str(u["conta"]) if u.get("conta") else "—", rotulo="Conta"),
               ui.td(ui.selo("ok", "ok") if u.get("ok") else ui.selo(str(u.get("erro") or "falhou")[:60], "ruim"),
                     html_=True, rotulo="Resultado"),
               ui.td(f'{ui.num((u.get("duracao_s") or 0) / 60, casas=1)} min', n=True, rotulo="Duração"),
               ui.td(_consumo_execucao(u), n=True, rotulo="Consumo / API")] for u in reversed(usos)]
    corpo = _pausas() + _cache_prompt(usos)
    corpo += ui.kpis(ui.kpi(f"Tokens · {hoje[8:10] + '/' + hoje[5:7] if hoje else 'hoje'}",
                            ui.num(tokens), quente=True),
                     ui.kpi("Execuções recentes", ui.num(len(usos))),
                     ui.kpi("Falhas", ui.num(falhas), tom="ruim" if falhas else None))
    corpo += ui.card("Execuções recentes", ui.tabela(["Início", "Trabalho", "Modelo", "Conta", "Resultado", "Duração", "Consumo / API"], linhas,
                                                    celular="pilha") if linhas else ui.vazio("Nenhuma execução ainda."), justa=True)
    return corpo


def claude():
    """Página da aba Execuções (a rota /claude responde 302 para ela)."""
    return uso("execucoes")


def _med_conta(n, c, janela, rotulo, curto=False):
    j = (c or {}).get(janela) or {}
    if j.get("pct") is None:
        return ui.barra(rotulo, 0, texto="sem leitura")
    return ui.barra(rotulo, j["pct"], nota="" if curto or not j.get("reset") else f'volta às {ui.quando(j["reset"])}')


def uso_linha(usos=None, leitura=None, codex=None):
    """Visão geral: um medidor da janela de 5 h por conta Claude (sem leitura do endpoint, mostra as execuções
    das 5 h) e o limite semanal do Codex (o revisor; só tem limite de semana), lido do estado do orquestrador."""
    usos = _jsonl(dados() / "claude" / "uso.jsonl", 3000) if usos is None else usos
    leitura = _json(dados() / "claude" / "uso-contas.json", {}) if leitura is None else leitura
    if codex is None:
        codex = ((_json(dados() / "orch" / "estado.json", {}) or {}).get("codex") or {}).get("semana")
    res, itens = U.resumo(usos, C.agora()), []
    for n in C.com_login():
        c = ((leitura or {}).get("contas") or {}).get(n) or {}
        if (c.get("cinco_horas") or {}).get("pct") is None:
            itens.append(ui.barra(f"Conta {n} · 5 h", 0, texto=f'{res[n]["janelas"]["5h"]["execucoes"]} exec.'))
        else:
            itens.append(_med_conta(n, c, "cinco_horas", f"Conta {n} · 5 h", True))
    itens.append(_med_conta("orch", (leitura or {}).get("orquestrador"), "cinco_horas", "Orquestrador · 5 h", True))
    itens.append(_med_conta("codex", {"semana": codex}, "semana", "Codex · semana", True))
    return f'<a class="meds" href="/uso">{"".join(itens)}</a>'


def grafico_horas(h, agora=None):
    """Minutos de execução por hora, 48 h: uma linha por conta com login, com legenda (ui.grafico)."""
    contas = [c for c in C.com_login() if c in h]
    n = len(next(iter(h.values()))) if h else 0
    if not contas or not n or not any(x for c in contas for x in h[c]):
        return ui.vazio("Sem execuções nas últimas 48 h.")
    fim = (agora or C.agora()).astimezone(C.BRT).replace(minute=0, second=0, microsecond=0)
    rotulos = [f"{fim - dt.timedelta(hours=n - 1 - i):%d/%m %Hh}" for i in range(n)]
    return ui.grafico([[round(x, 1) for x in h[c]] for c in contas], rotulos, tipo="linha", unidade=" min",
                      legenda=[f"Conta {c}" for c in contas], rotulo="Minutos de execução por hora, 48 h")


def _usd(v):
    return ui.num(v, moeda="US$", casas=2 if v == 0 or abs(v) >= 0.1 else 3)  # 0,002 não vira 0,00


def card_dataforseo():
    """Saldo (hub.dataforseo saldo, no cron), gasto de hoje contra o teto, 7 dias e os 5 que mais gastaram."""
    s, r = DFS.ler_saldo(dados()) or {}, DFS.resumo(7, base=dados())
    saldo = s.get("saldo")
    nota_saldo = (f'<div class="d {"ruim" if saldo < DFS.SALDO_BAIXO_USD else "neutro"}">{ui.quando(s.get("em"))}'
                  + (f' · dura ~{d:.0f} dias' if (d := DFS.dias_restantes(saldo, r)) is not None else "") + "</div>"
                  if isinstance(saldo, (int, float)) else '<div class="d neutro">sem leitura ainda</div>')
    tom_hoje = "ruim" if r["hoje"] >= r["teto"] else "alerta" if r["hoje"] >= r["teto"] * 0.8 else "neutro"
    topo = ui.kpis(ui.kpi("Saldo", _usd(saldo) if isinstance(saldo, (int, float)) else "—", nota=nota_saldo),
                   ui.kpi("Gasto hoje", _usd(r["hoje"]), nota=f'<div class="d {tom_hoje}">teto {_usd(r["teto"])}/dia</div>'),
                   ui.kpi("Últimos 7 dias", _usd(r["total"]), nota=f'<div class="d neutro">{_usd(r["media_dia"])}/dia · '
                                                                  f'{ui.num(r["chamadas"])} chamadas</div>'))
    linhas = [[ui.td(o), ui.td(_usd(c), n=True)] for o, c in r["por_origem"][:5]]
    # por dia: o registrado (com quem gastou) ao lado da queda do saldo da conta; a diferença é gasto fora do registro
    serie = DFS.saldos(7, base=dados())
    queda, fora = collections.defaultdict(float), collections.defaultdict(float)
    for x in serie:
        queda[x["em"][:10]] += max(x.get("queda") or 0, 0)
        fora[x["em"][:10]] += x.get("fora") or 0
    dias = sorted(set(d for d, _ in r["por_dia"]) | set(queda), reverse=True)
    por_dia = [[ui.td(f"{d[8:10]}/{d[5:7]}"), ui.td(_usd(dict(r["por_dia"]).get(d, 0)), n=True),
                ui.td(", ".join(f"{o} {_usd(c)}" for o, c in r["por_dia_origem"].get(d, [])[:4]) or "—"),
                ui.td(_usd(queda[d]) if d in queda else "—", n=True),
                ui.td(ui.selo(_usd(fora[d]), "ruim" if fora[d] > DFS.FORA_DO_REGISTRO_USD else "neutro") if d in fora else "—", html_=True)]
               for d in dias]
    grafico = ui.grafico([[x["saldo"] for x in serie]], [x["em"][5:16].replace("T", " ") for x in serie], minimo=None,
                         unidade=" US$", rotulo="Saldo da conta, 7 dias") if len(serie) > 1 else ""
    return ui.card("DataForSEO", topo + (ui.tabela(["Quem gastou · 7 dias", "Custo"], linhas) if linhas
                                         else ui.vazio("Nenhuma chamada registrada."))
                   + (ui.tabela(["Dia", "Registrado", "Por chamador", "Queda do saldo", "Fora do registro"], por_dia)
                      if por_dia else "") + grafico
                   + '<p class="mu">Fonte: custos.jsonl do hub (todos os chamadores, inclusive o coletor de SERP do '
                     f'ClienteJ) e a série do saldo lida a cada 4 h. Fora do registro acima de US$ 1 avisa o {config.dono()}.</p>')


def _barra_conta(c, janela, rotulo):
    """Medidor da janela da conta; com a data de volta como 'volta 14/10 19:10 (há 3 h)' na nota."""
    j = (c or {}).get(janela) or {}
    if j.get("pct") is None:
        return f'<div class="med"><div class="t"><span>{e(rotulo)}</span><span class="mu">sem leitura</span></div></div>'
    nota = f'volta {ui.quando(j["reset"])}' if j.get("reset") else ""
    return ui.barra(rotulo, j["pct"], nota=e(nota))


def _uso_contas(usos, agora):
    arq = _json(dados() / "claude" / "uso-contas.json", {}) or {}
    leitura, orch = arq.get("contas") or {}, arq.get("orquestrador") or {}
    res, contas = U.resumo(usos, agora), C.com_login()
    pcts = [p for n in contas if (p := ((leitura.get(n) or {}).get("cinco_horas") or {}).get("pct")) is not None]
    cheia = max(pcts) if pcts else None
    corpo = _pausas() + _cache_prompt(usos)
    corpo += ui.kpis(ui.kpi("Janela de 5 h mais cheia", ui.num(cheia, pct=True) if cheia is not None else "—", quente=True,
                            tom="ruim" if (cheia or 0) >= 95 else "alerta" if (cheia or 0) >= 85 else None),
                     ui.kpi("Execuções · 24 h", ui.num(sum(res[n]["janelas"]["24h"]["execucoes"] for n in contas))),
                     ui.kpi("Tokens · 7 dias", ui.num(sum(res[n]["janelas"]["7d"]["tokens"] for n in contas))))
    cards = ""
    for n in contas:
        r, c = res[n], leitura.get(n) or {}
        jan = ui.tabela(["Janela", "Execuções", "Minutos", "Volume"],
                        [[ui.td(k), ui.td(ui.num(v["execucoes"]), n=True), ui.td(ui.num(v["minutos"]), n=True),
                          ui.td(ui.num(v["tokens"]), n=True)]
                         for k, v in r["janelas"].items()])
        selos = (ui.selo(r["modelo"]) + " " + ui.selo(f'{r["falhas"]} falhas', "ruim" if r["falhas"] else "neutro") + " "
                 + ui.selo(f'{r["limites"]} limites', "alerta" if r["limites"] else "neutro"))
        cards += ui.card(f"Conta {n}", _barra_conta(c, "cinco_horas", "Janela de 5 h") + jan
                         + f'<p class="mu">Cota: {e(c.get("fonte") or "estimativa")}</p>', direita=selos, classe="uso-conta")
    cards += ui.card("Orquestrador", _barra_conta(orch, "cinco_horas", "Janela de 5 h")
                     + f'<p class="mu">Cota: {e(orch.get("fonte") or "estimativa")}</p>', classe="uso-conta")
    corpo += f'<div class="grade n2">{cards}</div>'
    corpo += ui.card("Minutos de execução por hora · últimas 48 h", grafico_horas(U.horas(usos, agora), agora))
    return corpo


def _mi(v):
    return f"{ui.num(v / 1e6, casas=1)} mi"


def _tokens(usos=None, agora=None):
    """Aba Tokens: 24 h por tipo de execução e modelo, top 10 issues e alerta de issue acima de 5% do dia."""
    from hub import tokens as TK
    r = TK.resumo(_jsonl(dados() / "claude" / "uso.jsonl", 8000) if usos is None else usos, agora or C.agora())
    t = r["total"]
    # médias por execução só das contas Claude: o Codex conta o cache dentro da entrada e distorce a meta
    cl = [g for m, g in r["por_modelo"].items() if m != "codex"]
    n = sum(g["execucoes"] for g in cl) or 1
    lido, novo = sum(g["cache_leitura"] for g in cl), sum(g["entrada"] + g["cache_escrita"] for g in cl)
    corpo = ""
    if r["alertas"]:
        itens = ", ".join(f'{x["issue"]} ({ui.num(x["pct"], casas=1)}%)' for x in r["alertas"])
        corpo += f'<div class="aviso erro" role="status">Acima de {ui.num(TK.ALERTA_PCT)}% do custo do dia: {e(itens)}</div>'
    corpo += ui.kpis(ui.kpi("Custo equivalente · 24 h", ui.num(t["custo_usd"], moeda="US$"), quente=True),
                     ui.kpi("Execuções · 24 h", ui.num(t["execucoes"])),
                     ui.kpi("Cache lido por execução (Claude)", _mi(lido / n), nota="meta: menos de 0,3 mi"),
                     ui.kpi("Entrada nova por execução (Claude)", ui.num(novo / n), nota="meta: menos de 60 mil"),
                     ui.kpi("Curtas (< 90 s)", ui.num(t["curtas"]), tom="alerta" if t["curtas"] > n / 3 else None))
    cab = ["", "Execuções", "Custo", "Entrada nova", "Cache lido", "Cache escrito", "Saída", "Curtas"]

    def linhas(grupo, chave=None):
        itens = grupo if isinstance(grupo, list) else [{"issue": k, **v} for k, v in
                                                       sorted(grupo.items(), key=lambda kv: -kv[1]["custo_usd"])]
        return [[ui.td(x["issue"]), ui.td(ui.num(x["execucoes"]), n=True),
                 ui.td(ui.num(x["custo_usd"], moeda="US$") + (f' · {ui.num(x["pct"], casas=1)}%' if chave else ""), n=True),
                 ui.td(_mi(x["entrada"]), n=True), ui.td(_mi(x["cache_leitura"]), n=True),
                 ui.td(_mi(x["cache_escrita"]), n=True), ui.td(_mi(x["saida"]), n=True),
                 ui.td(ui.num(x["curtas"]), n=True)] for x in itens]

    corpo += ui.card("Top 10 issues mais caras · 24 h", ui.tabela(["Issue"] + cab[1:], linhas(r["top"], "pct")) if r["top"]
                     else ui.vazio("Nada nas últimas 24 h."), justa=True)
    corpo += ui.card("Por tipo de execução · 24 h", ui.tabela(["Tipo"] + cab[1:], linhas(r["por_tipo"])), justa=True)
    corpo += ui.card("Por modelo · 24 h", ui.tabela(["Modelo"] + cab[1:], linhas(r["por_modelo"])), justa=True)
    return corpo + ('<p class="mu">Custo = equivalente de API que a CLI informa (o Codex não informa). Tipo vem do motivo '
                    'com que a execução entrou na fila; registros anteriores à KRN-910 aparecem como sem_tipo. '
                    'Tabela antes × depois: <code>python3 -m hub.tokens antes-depois --corte &lt;ISO&gt;</code>.</p>')


def _retomadas(usos=None, sem_ia=None, agora=None, dias=7):
    """Aba Retomadas: execuções por tarefa (meta ≤ 2 nas pequenas, ≤ 4 nas grandes), % de retomadas sem IA
    (meta ≥ 70%) e a série diária por tipo."""
    from hub import retomada as RT
    r = RT.medir(_jsonl(dados() / "claude" / "uso.jsonl", 8000) if usos is None else usos,
                 RT.ler_sem_ia() if sem_ia is None else sem_ia, agora or C.agora(), dias)

    def tom(ok):
        return None if ok else "alerta"
    corpo = ui.kpis(
        ui.kpi(f"Execuções por tarefa pequena · {dias} d", ui.num(r["media_pequenas"], casas=1), tom=tom(r["metas"]["pequenas"]),
               nota=f"meta: até {RT.META_PEQUENA}"),
        ui.kpi(f"Execuções por tarefa grande · {dias} d", ui.num(r["media_grandes"], casas=1), tom=tom(r["metas"]["grandes"]),
               nota=f"meta: até {RT.META_GRANDE}"),
        ui.kpi("Retomadas sem IA", f"{ui.num(r['pct_sem_ia'], casas=1)}%", tom=tom(r["metas"]["sem_ia"]),
               nota=f"meta: {ui.num(RT.META_SEM_IA)}% ou mais ({r['retomadas_sem_ia']} sem × {r['retomadas_com_ia']} com)"),
        ui.kpi("Execuções sem tipo", ui.num(r["sem_tipo"]), tom=None if not r["sem_tipo"] else "ruim",
               nota="meta: zero (vira incidente)"))
    dias_ord = list(r["diario"])
    soma = collections.Counter()
    for d in r["diario"].values():
        soma.update(d)
    tipos = [k for k, _ in soma.most_common(6)]
    if dias_ord and tipos:
        series = [[r["diario"][d].get(k, 0) for d in dias_ord] for k in tipos]
        corpo += ui.card("Por dia e por tipo", ui.grafico(series, [d[5:] for d in dias_ord], tipo="linha", legenda=tipos,
                                                         rotulo="Execuções por dia e por tipo"))
    linhas = [[ui.td(t["issue"]), ui.td(ui.num(t["execucoes"]), n=True), ui.td("grande" if t["grande"] else "pequena"),
               ui.td(ui.num(t["custo_usd"], moeda="US$"), n=True)] for t in r["por_tarefa"][:15]]
    corpo += ui.card(f"Tarefas com mais execuções · {dias} d", ui.tabela(["Tarefa", "Execuções", "Porte", "Custo"], linhas)
                     if linhas else ui.vazio("Nada no período."), justa=True)
    return corpo + (f'<p class="mu">Retomada sem IA = volta resolvida pelo código (espera de CI e de deploy, conferência, '
                    f'parada), registrada em sem-ia.jsonl. Tarefa grande: somou {RT.GRANDE_MIN} min de sessão ou US$ {RT.GRANDE_USD}. '
                    f'Aos {RT.AVISO_EXECUCOES} execuções desde a última fala a issue recebe aviso; aos {RT.TETO_EXECUCOES} pausa '
                    f'e o orquestrador é chamado. Terminal: <code>python3 -m hub.retomada medir</code>.</p>')


def uso(aba="contas"):
    aba = aba if aba in ("contas", "execucoes", "tokens", "retomadas") else "contas"
    abas = ui.filtro("aba", {"contas": "Contas", "execucoes": "Execuções", "tokens": "Tokens", "retomadas": "Retomadas"}, aba,
                     rotulo="Seção", base="/uso", abas=True)
    if aba == "retomadas":
        corpo = _retomadas()
    elif aba == "tokens":
        corpo = _tokens()
    elif aba == "execucoes":
        corpo = _execucoes()
        usos, agora = _jsonl(dados() / "claude" / "uso.jsonl", 5000), C.agora()
        todas = collections.Counter()
        for r in U.resumo(usos, agora).values():
            for t in r["tarefas"]:
                todas[t["nome"]] += t["execucoes"]
        linhas = [[ui.td(nome), ui.td(ui.num(v), n=True)] for nome, v in todas.most_common(10)]
        corpo += ui.card("Consumo por tarefa · 7 dias", ui.tabela(["Tarefa", "Execuções"], linhas) if linhas
                         else ui.vazio("Nada ainda."))
    else:
        corpo = _uso_contas(_jsonl(dados() / "claude" / "uso.jsonl", 5000), C.agora())
    return pagina("Uso e cotas", abas + corpo, "/uso", olho="Máquina",
                  sub="Quanto das cotas dos Claudes já foi usado e o que rodou.")


CONSERTO_TOM = {"consertado": "ok", "tentando": "alerta", "precisa_fulano": "ruim"}
CONHECIDAS = {"em", "disco", "memoria", "zumbis", "reboot", "atualizacoes", "containers", "vigia", "backups",
              "certificados", "certificados_em", "deploys", "deploys_em", "problemas"}
DEPLOY_ESTADO = {"done": ("concluído", "ok"), "error": ("erro", "ruim")}


def _saude():
    s = _json(dados() / "vps" / "saude.json", {})
    return s if isinstance(s, dict) else {}


def _sistema(s):
    """Aba Sistema (o que era /vps): problemas, saúde, reboot/atualizações, bots, backups, certificados e consertos."""
    g = lambda k: s.get(k) if isinstance(s.get(k), dict) else {}  # noqa: E731
    lista = lambda k: [x for x in s.get(k) or [] if isinstance(x, dict)] if isinstance(s.get(k), list) else []  # noqa: E731
    probs = [str(p) for p in s.get("problemas") or []] if isinstance(s.get("problemas"), list) else []
    pend, att, zb = g("reboot"), g("atualizacoes"), g("zumbis")
    falhas = sum(1 for b in lista("backups") if not b.get("ok"))
    corpo = ui.kpis(
        ui.kpi("Problemas", len(probs), tom="ruim" if probs else "ok", nota="nenhum aberto" if not probs else ""),
        ui.kpi("Atualizações", att.get("total", 0) or 0, tom="alerta" if att.get("total") else "ok",
               nota=f'{e(str(att.get("seguranca", 0) or 0))} de segurança'),
        ui.kpi("Backups com falha", falhas, tom="ruim" if falhas else "ok"),
        ui.kpi("Dado de", ui.quando(s.get("em")) or "—", nota=e(ui.ha(s.get("em")))))
    if probs:
        corpo += ui.card("Problemas", ui.lista([{"titulo": p, "meta": ui.selo("atenção", "ruim")} for p in probs], solta=True))
    med = ""
    at = _mon_atual()
    pm = at.get("ponto") if at and isinstance(at.get("ponto"), dict) else {}
    if _f(pm.get("cpu")) is not None:  # CPU e carga vêm do coletor da aba Agora
        cg = [c for c in pm["carga"] if _f(c) is not None][:3] if isinstance(pm.get("carga"), list) else []
        nc = _f(pm.get("n_cpu"))
        med += ui.barra("CPU", pm["cpu"], nota=(f'Carga {" · ".join(_br(c, 2) for c in cg)}'
                                                f'{f" ({nc:.0f} núcleos)" if nc else ""}') if cg else "")
    if isinstance(g("memoria").get("pct"), (int, float)):
        sw = g("memoria").get("swap_mb")
        med += ui.barra("Memória", g("memoria")["pct"], nota=f"Swap em uso: {e(ui.num(sw))} MB" if sw is not None else "")
    if isinstance(g("disco").get("pct"), (int, float)):
        lv = g("disco").get("livre_gb")
        med += ui.barra("Disco", g("disco")["pct"], nota=f"{e(ui.num(lv, casas=1))} GB livres" if lv is not None else "")
    corpo += ui.card("Saúde", f'<div class="meds">{med}</div>' if med else '<p class="mu">Sem dados.</p>')
    sel = [ui.selo("Reboot pendente" if pend.get("pendente") else "Sem reboot pendente", "alerta" if pend.get("pendente") else "ok"),
           ui.selo(f'{att.get("total", 0)} atualizações ({att.get("seguranca", 0)} de segurança)', "alerta" if att.get("total") else "ok"),
           ui.selo(f'{zb.get("total", 0)} zumbis', "alerta" if zb.get("total") else "ok"),
           ui.selo("containers caídos: " + (", ".join(map(str, g("containers")["caidos"])) if g("containers").get("caidos")
                                            else "nenhum"), "ruim" if g("containers").get("caidos") else "ok")]
    det = ""
    if pend.get("pacotes"):
        det += f'<p class="mu">Pacotes do reboot: {e(", ".join(map(str, pend["pacotes"])))}</p>'
    if isinstance(zb.get("por_pai"), dict) and zb["por_pai"]:
        det += f'<p class="mu">Zumbis por processo pai:</p>{_kv(zb["por_pai"])}'
    corpo += ui.card("Estado do servidor", " ".join(sel) + det)
    if g("vigia"):
        corpo += ui.card("Vigia dos bots", ui.tabela(["Bot", "Conexão", "Sem sinal há"], [
            [ui.td(str(k)), ui.td(ui.selo("conectado", "ok") if (v or {}).get("conectado") else ui.selo("desconectado", "ruim"), html_=True),
             ui.td(f'{(v or {}).get("min_sem_sinal", "—")} min', n=True)] for k, v in g("vigia").items()]), justa=True)
    if lista("backups"):
        corpo += ui.card("Backups", ui.tabela(["Backup", "Estado", "Idade"], [
            [ui.td(str(b.get("nome", ""))), ui.td(ui.selo("ok", "ok") if b.get("ok") else ui.selo("sem backup", "ruim"), html_=True),
             ui.td("—" if b.get("idade_h") is None else f'{b["idade_h"]} h', n=True)] for b in lista("backups")]), justa=True)
    if lista("certificados"):
        corpo += ui.card("Certificados", ui.tabela(["Host", "Dias"], [
            [ui.td(str(c.get("host", ""))),
             ui.td(ui.selo("—" if c.get("dias") is None else c["dias"], "ruim" if (c.get("dias") or 0) < 15 else "alerta" if (c.get("dias") or 0) < 30 else "ok"),
                   n=True, html_=True)]
            for c in sorted(lista("certificados"), key=lambda c: c.get("dias") or 0)]), justa=True,
            direita=e(f'conferido {ui.ha(s.get("certificados_em"))}'.strip()) if s.get("certificados_em") else "")
    cs = _jsonl(dados() / "consertos.jsonl", 20)
    corpo += ui.card("Consertos recentes", ui.tabela(["Quando", "Ação", "Alvo", "Resultado"], [
        [ui.td(ui.quando(c.get("em")) or str(c.get("em") or "")), ui.td(str(c.get("conserto", ""))), ui.td(str(c.get("quebrou", ""))),
         ui.td(ui.selo(c.get("estado", "—"), CONSERTO_TOM.get(c.get("estado"), "neutro")), html_=True)] for c in reversed(cs)],
        celular="some:2") if cs else '<p class="mu">Nenhum.</p>', justa=bool(cs))
    outros = {k: v for k, v in s.items() if k not in CONHECIDAS}
    if outros:
        corpo += ui.card("Outros", _kv(outros))
    return corpo


def _deploys(s):
    """Aba Deploys: o vigia de deploy (o que subiu, o mais recente primeiro)."""
    ds = [x for x in s.get("deploys") or [] if isinstance(x, dict)] if isinstance(s.get("deploys"), list) else []
    if not ds:
        return ui.card("Deploys", '<p class="mu">Nenhum deploy registrado ainda.</p>')
    linhas = []
    for d in sorted(ds, key=lambda d: str(d.get("em") or ""), reverse=True):
        txt, tom = DEPLOY_ESTADO.get(d.get("status"), (d.get("status") or "—", "alerta"))
        tit = str(d.get("titulo") or "").splitlines()[0] if d.get("titulo") else ""
        linhas.append([ui.td(ui.quando(d.get("em")) or str(d.get("em") or ""), classe="mu"), ui.td(str(d.get("projeto") or d.get("app") or "")),
                       ui.td(ui.selo(txt, tom), html_=True), ui.td(tit)])
    falhos = sum(1 for d in ds if d.get("status") == "error")
    corpo = ui.kpis(ui.kpi("Deploys registrados", len(ds), nota=e(f'conferido {ui.ha(s.get("deploys_em"))}') if s.get("deploys_em") else ""),
                    ui.kpi("Com erro", falhos, tom="ruim" if falhos else "ok"))
    return corpo + ui.card("Deploys recentes", ui.tabela(["Quando", "App", "Estado", "Título"], linhas, celular="some:3"), justa=True)


# ---------- monitor (hub.monitor: coletor de 1 em 1 min gravando em HUB_DATA/monitor) ----------

JANELAS_MON = (("1h", "1 h"), ("6h", "6 h"), ("24h", "24 h"), ("7d", "7 dias"))
ABAS_MON = (("agora", "Agora"), ("sistema", "Sistema"), ("deploys", "Deploys"))
SUB_MON = {"agora": "O servidor aguenta a carga agora?", "sistema": "O que no servidor precisa de cuidado?",
           "deploys": "O que subiu e o que deu errado?"}
MON_VAZIO = '<p class="mu">Sem dados ainda (o coletor grava de 1 em 1 min).</p>'


def _f(x):
    """Número finito ou None: dado do coletor pode vir null, texto, bool ou NaN e a tela não pode cair por isso."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _br(x, casas=1):
    """Número no formato brasileiro (1.234,5); '—' sem valor."""
    return ui.num(_f(x), casas=casas)


def _taxa(x):
    """MB/s com casas conforme a grandeza (0,52 · 3,1 · 120)."""
    x = _f(x)
    return _br(x, 0 if x is not None and x >= 100 else 1 if x is not None and x >= 10 else 2)


def _mon_atual():
    try:
        a = monitor.atual()
    except Exception:  # noqa: BLE001
        return None
    return a if isinstance(a, dict) else None


def _mon_serie(janela):
    try:
        s = monitor.serie(janela)
    except Exception:  # noqa: BLE001
        return []
    return [p for p in s if isinstance(p, dict)] if isinstance(s, list) else []


def _rotulos_x(rotulos):
    """Rótulos do eixo X em Brasília: HH:MM se a janela cabe em 2 dias, senão dd/mm."""
    ts = []
    for r in rotulos or []:
        try:
            t = dt.datetime.fromisoformat(str(r).replace("Z", "+00:00"))
            ts.append(t.astimezone(C.BRT) if t.tzinfo else t)
        except ValueError:
            pass
    if not ts:
        return []
    f = "%d/%m" if ts[-1] - ts[0] >= dt.timedelta(days=2) else "%H:%M"
    return [f"{t:{f}}" for t in ts]


def _graf(series, ts, legenda=(), unidade="%", maximo=None):
    """ui.grafico de área com o eixo X em Brasília; vazio se nenhuma série tem valor."""
    return ui.grafico(series, _rotulos_x(ts), tipo="area", unidade=unidade, maximo=maximo, legenda=legenda if len(series) > 1 else (),
                      rotulo=" e ".join(legenda) or "gráfico")


def _cartao_mon(titulo, barra="", graf="", extra=""):
    """Cartão do Monitor: barra (limiar único do ui), gráfico e detalhe. Sem nada disso, a mensagem de vazio."""
    return ui.card(titulo, f"{barra}{graf}{extra}" if barra or graf else MON_VAZIO)


def _med_mon(rotulo, pct, nota=""):
    pct = _f(pct)
    return "" if pct is None else ui.barra(rotulo, pct, nota=nota)


def _col(pts, campo):
    return [_f(p.get(campo)) for p in pts]


def _processos(lista):
    return [[ui.td(str(p.get("nome") or "—")), ui.td(str(p.get("usuario") or "—"), classe="mu"),
             ui.td(_br(p.get("cpu")), _f(p.get("cpu")), n=True), ui.td(_br(p.get("mem_mb"), 0), _f(p.get("mem_mb")), n=True)]
            for p in (lista if isinstance(lista, list) else []) if isinstance(p, dict)][:5]


def _sessoes_claude():
    try:
        regs = [r for r in agenda.rodando_todas().values() if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        return ui.erro("Leitura das sessões indisponível agora.")
    if not regs:
        return '<p class="mu">Nenhuma sessão rodando agora.</p>'
    linhas = [[ui.td(f"Conta {r.get('conta') or '—'}"), ui.td(str(r.get("nome") or "—")), ui.td(ui.ha(r.get("inicio")) or "—", classe="mu")]
              for r in sorted(regs, key=lambda r: str(r.get("conta") or ""))]
    return ui.tabela(["Conta", "Tarefa", "Iniciou"], linhas, celular="some:2")


def _agora(janela):
    """Aba Agora: números, cartões com barra e gráfico, núcleos, sessões Claude, processos e containers."""
    at, pts = _mon_atual(), _mon_serie(janela)
    ponto = at.get("ponto") if at and isinstance(at.get("ponto"), dict) else (pts[-1] if pts else {})
    ts, g = [p.get("t") for p in pts], lambda k: _col(pts, k)  # noqa: E731
    idade = ui.ha(ponto.get("t")) if ponto else ""
    topo = f'<p class="mu">Dado de {e(ui.quando(ponto.get("t")))} (Brasília) · {e(idade)}</p>' if idade else ""
    try:
        parado = bool(idade) and (C.agora() - dt.datetime.fromisoformat(str(ponto["t"]).replace("Z", "+00:00"))).total_seconds() > 300
    except (KeyError, TypeError, ValueError):
        parado = False
    if parado:
        topo += ui.erro("O coletor parece parado.", "O último dado tem mais de 5 minutos.")
    v = lambda k: _f(ponto.get(k))  # noqa: E731
    gb = lambda a, b: f"{_br(v(a))} de {_br(v(b))} GB" if v(a) is not None and v(b) is not None else ""  # noqa: E731
    carga = ponto.get("carga")
    carga = [c for c in carga if _f(c) is not None] if isinstance(carga, list) else []
    nuc = _f(ponto.get("n_cpu"))
    cpu_extra = (f'<p class="mu">Carga 1/5/15 min: {" · ".join(_br(c, 2) for c in carga[:3])}'
                 f'{f" ({nuc:.0f} núcleos)" if nuc else ""}</p>') if carga else ""
    swap = (f'<p class="mu">Swap: {gb("swap_usado_gb", "swap_total_gb")}</p>' if v("swap_total_gb") else
            '<p class="mu">Sem swap.</p>' if v("swap_total_gb") == 0 else "")
    dk = at.get("docker") if at and isinstance(at.get("docker"), dict) else {}
    dk_gb = _f(dk.get("total_gb")) if _f(dk.get("total_gb")) is not None else v("docker_gb")
    partes = [f"{r} {_br(dk.get(k))} GB" for r, k in (("imagens", "imagens_gb"), ("containers", "containers_gb"),
                                                       ("volumes", "volumes_gb"), ("cache", "cache_gb")) if _f(dk.get(k)) is not None]
    aprox = " " + ui.selo("aprox.", "alerta") if dk.get("aprox") else ""
    disco_t = v("disco_total_gb")
    pct = lambda k: "—" if v(k) is None else f"{_br(v(k))}%"  # noqa: E731
    faixa = ui.kpis(ui.kpi("CPU", pct("cpu")), ui.kpi("Memória", pct("mem_pct"), nota=e(gb("mem_usado_gb", "mem_total_gb"))),
                    ui.kpi("Disco", pct("disco_pct"), nota=e(gb("disco_usado_gb", "disco_total_gb"))),
                    ui.kpi("Docker", "—" if dk_gb is None else f"{_br(dk_gb)} GB", nota=e(" · ".join(partes)) + aprox))
    io = lambda a, b, ra, rb: f'<p class="mu">{ra} {_taxa(v(a))} MB/s · {rb} {_taxa(v(b))} MB/s</p>' if v(a) is not None or v(b) is not None else ""  # noqa: E731
    cards = [
        _cartao_mon("CPU", _med_mon("Uso", v("cpu")), _graf([g("cpu")], ts, ("CPU",), maximo=100), cpu_extra),
        _cartao_mon("Memória", _med_mon("Uso", v("mem_pct")), _graf([g("mem_pct")], ts, ("Memória",), maximo=100), swap),
        _cartao_mon("Disco", _med_mon("Uso", v("disco_pct")), _graf([g("disco_pct")], ts, ("Disco",), maximo=100)),
        _cartao_mon("Docker", _med_mon("Do disco", dk_gb / disco_t * 100 if dk_gb is not None and disco_t else None),
                    _graf([g("docker_gb")], ts, ("Docker",), unidade=" GB")),
        _cartao_mon("Block I/O", "", _graf([g("io_ler_mbs"), g("io_gravar_mbs")], ts, ("Leitura", "Gravação"), unidade=" MB/s"),
                    io("io_ler_mbs", "io_gravar_mbs", "leitura", "gravação")),
        _cartao_mon("Network I/O", "", _graf([g("rede_rx_mbs"), g("rede_tx_mbs")], ts, ("Recebido", "Enviado"), unidade=" MB/s"),
                    io("rede_rx_mbs", "rede_tx_mbs", "recebido", "enviado")),
    ]
    corpo = topo + faixa + f'<div class="grade n2">{"".join(cards)}</div>'
    nucleos = ponto.get("nucleos")
    nucleos = [n_ for n_ in (_f(c) for c in nucleos) if n_ is not None] if isinstance(nucleos, list) else []
    corpo += ui.card("Carga por núcleo", '<div class="meds">' + "".join(ui.barra(f"#{i}", c) for i, c in enumerate(nucleos)) + "</div>"
                     if nucleos else MON_VAZIO)
    corpo += ui.card("Sessões Claude rodando", _sessoes_claude())
    proc = at.get("processos") if at and isinstance(at.get("processos"), dict) else {}
    cab = ["Processo", "Usuário", "CPU %", "RAM MB"]
    corpo += ('<div class="grade n2">' + ui.card("Top 5 processos por CPU", ui.tabela(cab, _processos(proc.get("cpu")), num=(2, 3)) if _processos(proc.get("cpu")) else MON_VAZIO)
              + ui.card("Top 5 processos por RAM", ui.tabela(cab, _processos(proc.get("mem")), num=(2, 3)) if _processos(proc.get("mem")) else MON_VAZIO) + "</div>")
    cs = at.get("containers") if at else None
    if not at:
        cont = MON_VAZIO
    elif not isinstance(cs, list):
        cont = ui.erro("Docker indisponível agora.", "Não veio na última leitura.")
    else:
        cs = sorted((c for c in cs if isinstance(c, dict)), key=lambda c: -(_f(c.get("cpu")) or 0))
        cont = ui.tabela(["Container", "CPU %", "RAM MB", "RAM %"], [
            [ui.td(str(c.get("nome") or "—")), ui.td(_br(c.get("cpu")), _f(c.get("cpu")), n=True), ui.td(_br(c.get("mem_mb"), 0), _f(c.get("mem_mb")), n=True),
             ui.td(_br(c.get("mem_pct")), _f(c.get("mem_pct")), n=True)] for c in cs], num=(1, 2, 3)) if cs else '<p class="mu">Nenhum container rodando.</p>'
    return corpo + ui.card("Containers", cont, conta=len(cs) if isinstance(cs, list) and cs else None)


def monitor_fragmento(janela="1h", aba="agora"):
    """O miolo da tela (recarregado por /monitor.fragmento pelo data-recarrega do ui): o conteúdo da aba."""
    janela = janela if janela in dict(JANELAS_MON) else "1h"
    if aba == "sistema":
        return _sistema(_saude())
    if aba == "deploys":
        return _deploys(_saude())
    return _agora(janela)


def monitor_tela(janela="1h", aba="agora"):
    janela = janela if janela in dict(JANELAS_MON) else "1h"
    aba = aba if aba in dict(ABAS_MON) else "agora"
    abas = ui.filtro("aba", ABAS_MON, aba, rotulo="", base=f"/monitor?janela={janela}", abas=True)
    jan = ui.filtro("janela", JANELAS_MON, janela, rotulo="Janela dos gráficos", base=f"/monitor?aba={aba}") if aba == "agora" else ""
    avisos = "".join(ui.erro(f"Plugin {n} não carregou", m) for n, m in plugins.erros().items())
    return pagina("Monitor", f'{avisos}{abas}<div id="monitor" data-recarrega="/monitor.fragmento?aba={aba}&amp;janela={janela}">'
                  f'{monitor_fragmento(janela, aba)}</div>', "/monitor", sub=e(SUB_MON[aba]), acoes=jan)


RE_SEPARADOR = re.compile(r"^\|[\s:|-]+\|$")
SELO_COLETA = (("ok", "ok"), ("sem", "alerta"), ("falta", "alerta"), ("erro", "ruim"), ("falh", "ruim"))


def _tabela_do_relatorio(linhas):
    """Primeira tabela markdown (cabeçalho, separador, linhas todas com o mesmo número de colunas) → (início, fim, cab, linhas)
    ou None se o arquivo não tiver esse formato: nesse caso o relatório inteiro vai como markdown."""
    for i in range(len(linhas) - 2):
        if not (linhas[i].startswith("|") and RE_SEPARADOR.match(linhas[i + 1].strip())):
            continue
        celulas = lambda x: [c.strip() for c in x.strip().strip("|").split("|")]  # noqa: E731
        cab, fim = celulas(linhas[i]), i + 2
        rows = []
        while fim < len(linhas) and linhas[fim].startswith("|"):
            rows.append(celulas(linhas[fim]))
            fim += 1
        if len(cab) >= 2 and rows and all(len(r) == len(cab) for r in rows):
            return i, fim, cab, rows
    return None


def relatorio_tela():
    """/relatorio: a coleta diária (latest-report.md). Tabela com um selo por fonte quando o arquivo traz a tabela de
    projetos × fontes; senão o markdown inteiro, renderizado."""
    arq = dados() / "latest-report.md"
    texto = _texto(arq)
    try:
        feito = dt.datetime.fromtimestamp(arq.stat().st_mtime, dt.timezone.utc)
    except OSError:
        feito = None
    dia = re.search(r"\d{4}-\d{2}-\d{2}", texto.splitlines()[0]) if texto.strip() else None
    sub = " · ".join(x for x in (f"Coleta de {_dia(dia.group(0))}" if dia else "", f"arquivo gravado {ui.ha(feito)}" if feito else "") if x)
    ped = {"sub": e(sub)} if sub else {}
    if not texto.strip():
        return pagina("Coleta diária", ui.vazio("Nenhuma coleta gravada ainda.", "A rotina da coleta diária grava o latest-report.md."),
                      "/relatorio", **ped)
    linhas = texto.splitlines()
    t = _tabela_do_relatorio(linhas)
    if not t:
        return pagina("Coleta diária", ui.card("Relatório da coleta", ui.md(texto)), "/relatorio", **ped)
    ini, fim, cab, rows = t
    inativos = {x["id"] for x in S.carregar() if not x.get("ativo", True)}  # projeto inativo não entra em tabela nem KPI
    rows = [r for r in rows if r[0] not in inativos]
    if not rows:
        return pagina("Coleta diária", ui.vazio("Nenhum projeto ativo na coleta.", "Só os sites com ativo: true entram."), "/relatorio", **ped)
    fontes = [c for r in rows for c in r[1:]]
    ok = sum(c.lower() == "ok" for c in fontes)
    sem = len(fontes) - ok
    completos = sum(all(c.lower() == "ok" for c in r[1:]) for r in rows)
    corpo = ui.kpis(ui.kpi("Fontes com dado", ui.num(ok), quente=not sem, tom="alerta" if sem else None,  # "23 de 24" cortava a 390 px
                           nota=f'de {ui.num(len(fontes))} · {ui.num(ok / len(fontes) * 100, pct=True)} das combinações projeto × fonte'),
                    ui.kpi("Projetos", ui.num(len(rows)), nota=f"{ui.num(completos)} com todas as fontes"),
                    ui.kpi("Fontes sem dado", ui.num(sem), tom="alerta" if sem else None, nota="sem acesso ou sem dado no período"))

    def selo(c):
        tom = next((t for k, t in SELO_COLETA if c.lower().startswith(k)), "neutro")
        return ui.td(ui.selo(c, tom), html_=True)

    corpo += ui.card("Projeto por fonte", ui.tabela(cab, [[ui.td(r[0], classe="nw")] + [selo(c) for c in r[1:]] for r in rows]), justa=True)
    antes = "\n".join(x for x in linhas[:ini] if not x.startswith("#")).strip()
    depois = "\n".join(linhas[fim:]).strip()
    if antes:
        corpo += ui.card("Como ler", ui.md(antes))
    if depois:
        corpo += ui.card("Observações da coleta", ui.md(depois))
    return pagina("Coleta diária", corpo, "/relatorio", **ped)


def relatorio_publico(token):
    """Página aberta sem senha do relatório com este shareToken; só título e resumo (nunca o campo html)."""
    if RE_TOKEN.match(token):
        for arq in (dados() / "mcp" / "relatorios").glob("*/*.json"):
            if arq.parent.name == "modelos":
                continue
            r = _json(arq, {})
            if isinstance(r, dict) and isinstance(r.get("shareToken"), str) and hmac.compare_digest(r["shareToken"], token):
                return 200, pagina(str(r.get("title", "")), ui.card("Resumo", ui.md(str(r.get("summary", "")))), publica=True)
    return 404, pagina("Não achei", ui.erro("Essa página não existe."), publica=True)


_SCRYPT = threading.BoundedSemaphore(4)  # scrypt do /entrar em paralelo (cada um ~16 MB e um núcleo)
SEM_REFERER = {"Referrer-Policy": "no-referrer"}  # /convite: o token da URL não sai em Referer
RE_PUBLICA = re.compile(r"/r/[A-Za-z0-9_-]{20,100}")
RE_LICITACAO = re.compile(r"/l/([A-Za-z0-9._-]{1,80})/(entrar|passar)")  # link assinado do e-mail das licitações: o token é a senha
# Arquivos da marca, públicos (a tela de entrada e o favicon aparecem antes da senha). Lista fechada: o caminho pedido só
# serve de chave, nunca vira caminho de disco, então "..", nome fora da lista e subpasta dão 404.
MARCA = Path(__file__).with_name("marca")
ASSETS = {f"/marca/{n}": n for n in ("kronos.svg", "kronos-icone-claro.svg", "kronos-icone-escuro.svg", "kronos-loading.svg")}
ASSETS["/favicon.svg"] = "kronos-icone-escuro.svg"  # a placa carvão lê bem na aba clara e na escura do navegador


def rota(caminho, usuario="dono"):
    """_rota + aviso de ?ok=/?erro= (escapado) que some em 6 s. Volta (status, html) ou (status, corpo, cabeçalhos)."""
    status, corpo, *cab = _rota(caminho, usuario)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(caminho).query)
    for k in ("ok", "erro"):
        if q.get(k) and not RE_PUBLICA.fullmatch(urllib.parse.urlsplit(caminho).path):  # /r/ é público: nada de texto da query
            corpo = corpo.replace("</main>", f'<div class="aviso {k}" role="status">{e(q[k][0][:300])}</div></main>', 1)
            break
    return (status, corpo, *cab)


def _rota(caminho, usuario="dono"):
    """(status, html) para o caminho com query (`?estado=` só vale se estiver em P.ESTADOS)."""
    _fontes_em.cache_clear()
    _CK.clear()
    u = urllib.parse.urlsplit(caminho)
    caminho, estado = u.path, (urllib.parse.parse_qs(u.query).get("estado") or [None])[0]
    pg = (urllib.parse.parse_qs(u.query).get("pagina") or ["1"])[0]
    pg = int(pg) if pg.isdigit() and len(pg) < 6 else 1
    partes = [p for p in caminho.split("/") if p]
    if not partes:  # a home é Pendências (docs/painel-f1/arquitetura.md); a query vai junto (aviso de ?ok= depois de uma ação)
        return 302, "", {"Location": "/pendencias" + (f"?{u.query}" if u.query else "")}
    if len(partes) == 2 and partes[0] == "r":
        return relatorio_publico(partes[1])
    if "/" + "/".join(partes) in config_telas.ROTAS_GET:  # barra final vale igual; vem ANTES dos plugins: plugin não sombreia /config/*
        return config_telas.ROTAS_GET["/" + "/".join(partes)]({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()},
                                               usuario, _csrf())
    achou = plugins.rota("/" + "/".join(partes))
    if achou:  # rota de plugin ligado (hub/plugins.py); plugin desligado não registra: cai no 404
        fn, trechos = achou
        return fn({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}, **trechos)
    if partes == ["github"]:
        from hub import github_api
        try:
            linhas, cotas_ = github_api.resumo(), github_api.cotas()
        except (sqlite3.Error, OSError):  # medição é melhor-esforço: banco ilegível não derruba a tela
            linhas, cotas_ = [], []
        corpo = '<p>Cobertura: gh e REST do hub medidos; local-ci das VPS, app da fsite05 e deploy ainda não.</p>'
        corpo += '<h2>Cota por credencial</h2><table><tr><th>Credencial</th><th>Restante</th><th>Limite</th><th>Reset</th></tr>'
        for ident_, rotulo, restante, limite, reset in cotas_:
            reset_txt = time.strftime("%H:%M", time.localtime(reset)) if reset else ""
            corpo += '<tr>' + ''.join(f'<td>{e(str(v))}</td>' for v in (
                f"{rotulo} {ident_[:6]}".strip(), restante, limite if limite is not None else "", reset_txt)) + '</tr>'
        corpo += '</table>'
        corpo += '<h2>Por consumidor (24 h)</h2><table><tr><th>Consumidor</th><th>http</th><th>cache</th><th>pausa</th><th>gh</th><th>total</th></tr>'
        corpo += ''.join('<tr>' + ''.join(f'<td>{e(str(v))}</td>' for v in linha) + '</tr>' for linha in github_api.pivo(linhas))
        corpo += '</table>'
        corpo += '<h2>Detalhe 24 h</h2><table><tr><th>Consumidor</th><th>Tipo</th><th>HTTP</th><th>Quantidade</th></tr>'
        corpo += ''.join('<tr>' + ''.join(f'<td>{e(str(v))}</td>' for v in linha) + '</tr>' for linha in linhas)
        corpo += '</table>'
        if not linhas:
            corpo += '<p>Sem amostra coletada ainda.</p>'
        return 200, pagina("Consumo GitHub", corpo, "/github", olho="Máquina")
    if partes == ["config", "usuarios"]:
        return 200, usuarios_tela()
    if partes == ["config", "auditoria"]:
        return 200, auditoria_tela()
    if partes == ["config"]:
        return 302, "", {"Location": "/config/usuarios"}
    if partes == ["virais"] and virais_tela:
        return 200, virais_pagina(u.query)
    if partes == ["destaques"] and destaques_tela:
        return 200, destaques_pagina(u.query)
    if partes == ["portfolio"] and portfolio_tela:
        return 200, portfolio_pagina(u.query)
    if partes == ["fabrica"] and fabrica_tela:
        return 200, fabrica_pagina()
    if partes == ["metas"]:
        return 200, metas_pagina()
    if partes == ["provas"] and provas_tela:
        return 200, provas_pagina(urllib.parse.parse_qs(u.query).get("produto", [""])[0][:40])
    if partes == ["confiabilidade"]:  # (F2)
        from hub import confiabilidade
        return 200, pagina("Confiabilidade", confiabilidade.tela(), "/confiabilidade", olho="Máquina",
                           sub="Toda execução medida: sucesso por tipo, issue→merge, custo por issue fechada, retrabalho e avisos falsos.")
    if partes == ["velocidade"] and velocidade_campo_tela:
        return 200, pagina("Velocidade de campo", velocidade_campo_tela.tela(), "/velocidade", olho="SEO",
                           sub="As piores páginas por visita real: LCP, INP, diagnóstico e a issue de conserto de cada uma.")
    if partes == ["concorrentes"] and concorrentes_tela:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        return 200, pagina("Concorrentes", concorrentes_tela.tela(q.get("produto", "")), "/concorrentes", olho="SEO",
                           sub="Uma ficha por concorrente: anúncios da Meta, tecnologia, Reddit, páginas novas, X/LinkedIn e SEO, com histórico diário.")
    if partes == ["seo"]:
        return 200, pagina("Auditoria", seomator_tela.tela(), "/seo", olho="SEO",
                           sub="Auditoria semanal do SEOmator: nota, erros graves e o que corrigir em cada site.")
    if len(partes) == 2 and partes[0] == "seo" and RE_ID.match(partes[1]):
        r = seomator_tela.site_tela(partes[1])
        return (200, pagina(f"Auditoria · {seomator_tela.nome(partes[1])}", r, "/seo", olho="SEO")) if r \
            else (404, pagina("Não achei", NAO_ACHEI))
    if partes == ["erros"] and erros_tela:
        return 200, pagina("Erros", erros_tela.tela(), "/erros", olho="Máquina",
                           sub="Os erros que o sistema repete: classes, tendência por semana, o que voltou depois do conserto e o que falta.")
    if partes == ["biblioteca"]:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        return 200, pagina("Biblioteca", biblioteca_tela.tela(q.get("q", "")[:200], q.get("tema", ""), q.get("projeto", ""), q.get("vista", "")),
                           "/biblioteca", olho="Máquina", sub=f"Receitas do {config.repo('biblioteca')}: o que já sabemos fazer, com código.")
    if partes[0] == "biblioteca":
        r = biblioteca_tela.receita(urllib.parse.unquote("/".join(partes[1:])))
        return (200, pagina(r[0], r[1], "/biblioteca", olho="Biblioteca")) if r else (404, pagina("Não achei", NAO_ACHEI))
    if partes == ["execucao"]:
        return 200, pagina("Execuções", rastreio_tela.lista(50), "/execucao", olho="Máquina",
                           sub="As últimas execuções do agente, com os passos de cada uma.")
    if len(partes) == 2 and partes[0] == "execucao":
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        passo = int(q["passo"]) if re.fullmatch(r"[0-9]{1,6}", q.get("passo", "")) else None
        r = rastreio_tela.tela(urllib.parse.unquote(partes[1]), passo, formulario=_form)
        return (200, pagina(f"Execução {partes[1][:40]}", r, "/execucao", olho="Máquina")) if r \
            else (404, pagina("Não achei", NAO_ACHEI))
    if partes == ["mapa", "saude"]:
        return 200, pagina("Saúde do mapa", grafo_tela.saude(), "/mapa")
    if partes == ["mapa", "grafo.json"]:
        return 200, grafo_tela.grafo_json(), {"Content-Type": "application/json; charset=utf-8"}
    if partes == ["mapa", "grafo"]:
        return 200, pagina("Mapa", grafo_tela.inteiro(), "/mapa", sub="O segundo cérebro, inteiro.")
    if partes in (["mapa", "grafo.js"], ["mapa", "force-graph-1.49.4.min.js"]):
        nome = partes[1]
        return 200, (Path(__file__).parent / "assets" / "grafo" / nome).read_text(), {"Content-Type": "text/javascript; charset=utf-8"}
    if partes == ["mapa"]:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if not any(k in q for k in ("q", "tipo", "busca")):
            return 200, pagina("Mapa", grafo_tela.inteiro(), "/mapa", sub="Arquitetura por projeto")
        return 200, pagina("Mapa", grafo_tela.tela(q.get("q", "")[:200], q.get("tipo", "")), "/mapa", olho="Máquina",
                           sub="Segundo cérebro: repos, domínios, apps, crons, segredos (só o nome), decisões e issues, ligados entre si.")
    if partes[0] == "mapa":
        r = grafo_tela.entidade(urllib.parse.unquote("/".join(partes[1:])))
        return (200, pagina(r[0], r[1], "/mapa", olho="Mapa")) if r else (404, pagina("Não achei", NAO_ACHEI))
    if partes == ["fluxo"]:
        from hub import fluxo
        return 200, fluxo.tela()
    if partes == ["cerebro"]:  # só leitura, sem botão: a tela inicial pode embutir (versão compacta)
        return 200, cerebro.tela("compacto" in urllib.parse.parse_qs(u.query)), \
            {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'"}
    if partes == ["cerebro.json"]:
        d = cerebro.ao_vivo((urllib.parse.parse_qs(u.query).get("desde") or [None])[0])
        return 200, json.dumps(d, ensure_ascii=False), {"Content-Type": "application/json; charset=utf-8"}
    if partes == ["operacao"]:
        return 200, visao_geral()
    if partes[0] == "bi" and bi_painel and partes[1:] != ["dados.json"]:
        return bi_painel.rota(partes[1:], {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
    if partes == ["bi", "dados.json"] and bi_painel:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        r = bi_painel.dados(q.get("negocio", "todos"), q.get("periodo", "28"), q.get("comparar", "anterior"))
        return (200 if r else 404), json.dumps(r or {"erro": "negócio desconhecido"}, ensure_ascii=False), \
            {"Content-Type": "application/json; charset=utf-8"}
    if partes == ["relatorio"]:
        return 200, relatorio_tela()
    if partes == ["propostas"]:
        return 200, _propostas(estado, pg)
    if partes == ["claude"]:
        return 302, "", {"Location": "/uso?aba=execucoes"}
    if partes == ["uso"]:
        return 200, uso((urllib.parse.parse_qs(u.query).get("aba") or ["contas"])[0])
    if partes in (["monitor"], ["monitor.fragmento"]):
        qm = urllib.parse.parse_qs(u.query)
        j, ab = (qm.get("janela") or ["1h"])[0], (qm.get("aba") or ["agora"])[0]
        return 200, monitor_tela(j, ab) if partes == ["monitor"] else monitor_fragmento(j, ab)
    if partes == ["vps"]:  # a VPS virou a aba Sistema do Monitor
        return 302, "", {"Location": "/monitor?aba=sistema"}
    if partes == ["redteam"] and redteam_tela:  # F7; sem o módulo (snapshot público) cai no 404
        return 200, pagina("Red team", redteam_tela.corpo(), "/redteam")
    if partes == ["agentes"]:
        return 200, agentes_tela()
    if partes == ["linear"]:
        return 200, linear_tela()
    if partes == ["pendencias"]:
        return 200, pendencias_tela()
    if partes == ["agora.fragmento"]:
        return 200, agora_fragmento()
    if partes == ["agentes.fragmento"]:
        return 200, agentes_fragmento()
    if len(partes) == 2 and partes[0] == "site" and RE_ID.match(partes[1]):
        r = site(partes[1])
        return (200, r) if r else (404, pagina("Não achei", NAO_ACHEI))
    if len(partes) == 2 and partes[0] == "proposta":
        r = proposta(partes[1])
        return (200, r) if r else (404, pagina("Não achei", NAO_ACHEI))
    return 404, pagina("Não achei", NAO_ACHEI)


class Painel(BaseHTTPRequestHandler):
    def _sessao(self):
        """(token, sessão) do cookie __Host-kronos_sessao; sessão None se não há, venceu ou o usuário saiu."""
        token = token_do(self.headers.get("Cookie"))
        return token, (usuarios.sessao(token) if token else None)

    def _ip(self):
        return ip_cliente(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def _origem_ok(self):
        """POST só com Origin presente e do mesmo host (sem Origin, nada passa)."""
        o = self.headers.get("Origin")
        return bool(o) and comecar.origem_ok(o, self.headers.get("Host"))

    def _campos(self, maximo=20000):
        """Campos do formulário (corpo ≤ maximo bytes) ou None se o tamanho não vale."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= n <= maximo:
            return None
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}

    def _sem_sessao(self):
        """GET sem login vai ao /entrar com a volta. Vindo de outro site e sem o cookie (o Strict não vai na navegação
        cross-site), recarrega a mesma URL antes: a 2ª navegação já é same-origin e leva o cookie."""
        if not token_do(self.headers.get("Cookie")) and self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._html(200, ui.salto(), {})
        self._fim(303, "/entrar?volta=" + urllib.parse.quote(self.path, safe=""))

    def _comecar(self, caminho, sessao):
        """Assistente de primeiro acesso (hub/comecar.py). True se tratou a requisição. Só liga com kronos.toml sem
        marcador; na config legada (sem toml) volta False e nada muda. Aberto (nenhum usuário): /comecar é pública e
        tudo leva a ela. Com dono: /comecar exige a sessão de um dono até o marcador."""
        if not comecar.ativo():
            if caminho == "/comecar":  # concluído (ou sem toml): a rota não existe mais
                self._fim(404)
                return True
            return False
        if caminho != "/comecar":
            if comecar.aberto():  # nenhum dono ainda: tudo leva ao assistente
                self._fim(303, "/comecar")
                return True
            return False
        if not comecar.aberto():
            if not sessao:
                self._sem_sessao()
                return True
            if sessao["papel"] != "dono":
                self._fim(403)
                return True
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        token = comecar.cookie_do(self.headers.get("Cookie")) or comecar.token_novo()
        passo = (q.get("passo") or [""])[0]
        aviso = None
        for k in ("ok", "erro"):
            if q.get(k):
                aviso = (k, q[k][0])
        cab = {"Set-Cookie": comecar.set_cookie(token, self.headers.get("X-Forwarded-Proto") == "https")}
        if comecar.aberto():  # sem o token de configuração ninguém vira dono: tela neutra (200, nunca 404)
            comecar.anunciar(uma_vez=True)  # painel que subiu antes do toml: o token ainda chega ao log
            self._html(200, comecar.pagina_pendente(token, aviso[1] if aviso and aviso[0] == "erro" else None), cab)
            return True
        if passo not in comecar.PASSOS or (passo != "dono" and not config.definido("dono.email")):
            passo = comecar.primeiro_passo()
        self._html(200, comecar.pagina(passo, token, aviso), {"Set-Cookie": comecar.set_cookie(
            token, self.headers.get("X-Forwarded-Proto") == "https")})
        return True

    def _comecar_post(self, sessao):
        """POST /comecar: cookie duplo + Origin do mesmo host; sem login só enquanto o assistente está aberto. O passo do
        dono cria o usuário e já entra com ele (Set-Cookie da sessão), para seguir no assistente."""
        if not comecar.ativo():
            return self._fim(404)
        if not comecar.aberto():
            if not sessao:
                return self._fim(303, "/entrar?volta=" + urllib.parse.quote("/comecar", safe=""))
            if sessao["papel"] != "dono":
                return self._fim(403)
        campos = self._campos()
        if campos is None or not comecar.origem_ok(self.headers.get("Origin"), self.headers.get("Host")):
            return self._fim(403)
        if not comecar.csrf_ok(comecar.cookie_do(self.headers.get("Cookie")), campos.get("csrf")):
            return self._fim(403)
        passo = campos.get("passo", "")
        if passo == "token" and comecar.aberto():  # confere o token; certo = mostra o passo do dono (sem gravar nada)
            ok, msg = comecar.verificar_token(campos.get("token_config"))
            usuarios.auditar("", "comecar:token", "", "ok" if ok else "erro", self._ip())
            cab = {"Set-Cookie": comecar.set_cookie(campos["csrf"], self.headers.get("X-Forwarded-Proto") == "https")}
            if ok:
                return self._html(200, comecar.pagina("dono", campos["csrf"], token_config=campos["token_config"].strip()), cab)
            return self._html(429 if msg.startswith("Muitas") else 403, comecar.pagina_pendente(campos["csrf"], msg), cab)
        estado, msg, proximo = comecar.executar(passo, campos)
        quem = sessao["email"] if sessao else (campos.get("email") or "").strip().lower() if passo == "dono" else ""
        # só o passo e ok/erro: a mensagem pode citar o que foi digitado, e os campos têm token e senha
        usuarios.auditar(quem, "comecar:" + (passo if passo in comecar.PASSOS else "?"), "", estado, self._ip())
        cab = {}
        if passo == "dono" and estado == "ok" and not sessao and campos.get("senha"):
            token = usuarios.entrar(campos.get("email"), campos["senha"], self._ip())
            if token:
                cab["Set-Cookie"] = cookie_sessao(token)
        if proximo is None and estado == "ok":
            return self._fim(303, "/?ok=" + urllib.parse.quote("Tudo pronto."), cab)
        self._fim(303, f"/comecar?passo={proximo}&{estado}={urllib.parse.quote(msg)}", cab)

    def do_GET(self):  # noqa: N802
        _pedido.sessao = None
        u = urllib.parse.urlsplit(self.path)
        caminho = u.path
        if caminho == "/saude":  # healthcheck do contêiner: pública, sem login, sem tocar em dado nenhum
            return self._texto(200, "ok")
        if caminho in ASSETS or caminho.startswith("/marca/"):
            return self._marca(ASSETS.get(caminho))
        if caminho == "/convite":  # pública: o token do link é a credencial
            t = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
            return self._html(200, ui.convite(t) if usuarios.ver_convite(t) else ui.convite_invalido(), SEM_REFERER)
        _, sessao = self._sessao()
        if sessao and not usuarios.pode(sessao["papel"], usuarios.permissao_get(caminho)):
            return self._sem_permissao(sessao)
        if self._comecar(caminho, sessao):
            return
        lic = RE_LICITACAO.fullmatch(caminho)  # link assinado do e-mail: o token da URL é a credencial
        if lic:
            return self._html(*self._licitacao_get(lic))
        if caminho == "/entrar":
            volta = volta_ok((urllib.parse.parse_qs(u.query).get("volta") or ["/"])[0])
            return self._fim(303, volta) if sessao else self._html(200, ui.entrar(volta), {})
        publica = RE_PUBLICA.fullmatch(caminho)  # único caminho de página sem login (relatório aberto /r/...)
        if not publica and not sessao:
            return self._sem_sessao()
        if caminho == "/sair":
            return self._html(200, ui.sair(sessao["csrf"]), {})
        if caminho.startswith("/video-arquivo/"):  # já com sessão e o papel conferido ("ver")
            return self._arquivo(plugins.arquivo(caminho))
        _pedido.sessao = sessao
        try:
            status, corpo, *cab = rota(self.path, sessao["email"] if sessao else "")
        except Exception as ex:  # noqa: BLE001
            status, corpo, cab = 500, pagina("Erro", f'<p class="mu">Algo falhou ({e(type(ex).__name__)}). Recarregue.</p>', publica=bool(publica)), []
        finally:
            _pedido.sessao = None
        self._html(status, corpo, cab[0] if cab else {})

    def _sem_permissao(self, sessao):
        """403 com a casca do painel (o menu do papel dele); nada vindo do pedido entra na página."""
        _pedido.sessao = sessao
        try:
            corpo = pagina("Sem permissão", '<p class="mu">Seu papel não abre esta tela. Peça a um dono.</p>')
        finally:
            _pedido.sessao = None
        self._html(403, corpo, {})

    def _convite_post(self):
        """POST /convite: Origin do mesmo host; senhas iguais e ≥ SENHA_MIN → cria (ou troca a senha), entra e vai à home."""
        if not limite.post_ok("ip:" + self._ip()):
            return self._fim(429, None, {"Retry-After": "60"})
        campos = self._campos() if self._origem_ok() else None
        if campos is None:
            return self._fim(403)
        t, senha = campos.get("t", ""), campos.get("senha", "")
        if not usuarios.ver_convite(t):
            return self._html(200, ui.convite_invalido(), SEM_REFERER)
        if senha != campos.get("senha2", ""):
            return self._html(200, ui.convite(t, "As senhas não conferem."), SEM_REFERER)
        try:
            email = usuarios.aceitar_convite(t, senha)
        except ValueError as ex:  # senha curta (mensagem nossa, sem a senha)
            return self._html(200, ui.convite(t, str(ex)), SEM_REFERER)
        if not email:
            return self._html(200, ui.convite_invalido(), SEM_REFERER)
        usuarios.auditar(email, "convite-aceito", email, ip=self._ip())
        token = usuarios.entrar(email, senha, self._ip())
        self._fim(303, "/", {"Set-Cookie": cookie_sessao(token)} if token else None)

    def _arquivo(self, arq):
        """MP4 de plugin (ARQUIVOS) com Range, para o <video> do navegador buscar e pular. Só chega aqui com sessão."""
        if not arq:
            return self._fim(404)
        total = arq.stat().st_size
        ini, fim, status = 0, total - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m[1] or m[2]):
            ini = int(m[1]) if m[1] else max(total - int(m[2]), 0)
            fim = min(int(m[2]), total - 1) if m[1] and m[2] else total - 1
            if ini > fim:
                return self._fim(416)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - ini + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{total}")
        self.send_header("Cache-Control", "private, max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        with open(arq, "rb") as f:
            f.seek(ini)
            restante = fim - ini + 1
            while restante > 0:
                bloco = f.read(min(1 << 20, restante))
                if not bloco:
                    break
                self.wfile.write(bloco)
                restante -= len(bloco)

    def _html(self, status, corpo, cab):
        # Sem a trava de moldura, outra página poderia embutir o painel (sessão aberta no navegador) e levar o dono a clicar
        # em "Aprovar" de um Pedido. Só a rota de leitura (/cerebro) afrouxa para a mesma origem.
        cab = {"Content-Type": "text/html; charset=utf-8", "X-Frame-Options": "DENY",
               "Content-Security-Policy": "frame-ancestors 'none'"} | cab  # rota pode trocar o tipo ou pôr Location
        b = corpo.encode()
        self.send_response(status)
        for k, v in cab.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        # Evita interpretar conteúdo como um tipo diferente do declarado.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def _texto(self, status, corpo):
        b = corpo.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(b)

    def _marca(self, nome):
        if not nome:
            return self._fim(404)
        b = (MARCA / nome).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "public, max-age=604800")
        # SVG aberto direto na aba não roda script nem busca nada (o loading só precisa do <style> dele)
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def _entrar_post(self):
        """POST /entrar. Banco de usuários fora do ar (disco, trava) vira 500 curto, sem detalhe: a conexão não cai."""
        try:
            return self._entrar_tentar()
        except (usuarios.sqlite3.Error, OSError) as ex:
            print(f"painel: /entrar falhou ({type(ex).__name__})", file=sys.stderr)
            try:
                return self._html(500, ui.entrar("/", "Não consegui entrar agora; tente de novo."), {})
            except OSError:  # o cliente já foi embora
                return None

    def _entrar_tentar(self):
        """Origin do mesmo host; certo → cookie de sessão e volta; errado → a mesma mensagem sempre."""
        campos = self._campos() if self._origem_ok() else None
        if campos is None:
            return self._fim(403)
        volta = volta_ok(campos.get("volta"))
        ip, email = self._ip(), (campos.get("email") or "").strip().lower()
        alvo = usuarios.alvo_login(email)
        reserva, avisar = limite.tentar(ip, email)  # antes do scrypt; a mesma resposta exista ou não o e-mail
        if reserva is None:
            if avisar:  # só o 1º 429 da janela vai à auditoria
                usuarios.auditar("", "entrar-bloqueado", alvo, "erro: 429", ip)
            return self._html(429, ui.entrar(volta, limite.MSG), {"Retry-After": str(limite.ESPERA)})
        if not _SCRYPT.acquire(timeout=1):  # rajada: no máximo 4 scrypt de login ao mesmo tempo na VPS dividida
            limite.liberar(ip, email, reserva)
            return self._html(429, ui.entrar(volta, limite.MSG), {"Retry-After": str(limite.ESPERA)})
        try:
            token = usuarios.entrar(email, campos.get("senha"), ip)
        except BaseException:
            limite.liberar(ip, email, reserva)
            raise
        finally:
            _SCRYPT.release()
        if not token:
            limite.falhou(ip, email, reserva)
            usuarios.auditar("", "entrar-falhou", alvo, "erro: senha", ip)
            return self._html(200, ui.entrar(volta, "E-mail ou senha incorretos."), {})
        limite.zerar(ip, email, reserva)
        usuarios.auditar(email, "entrar", email, ip=ip)
        self._fim(303, volta, {"Set-Cookie": cookie_sessao(token)})

    def _licitacao_get(self, m):
        """GET só mostra a confirmação (leitor de e-mail pré-carrega links): quem decide é o POST do botão."""
        try:
            from hub.licitacoes import email_diario as lic
        except ImportError as ex:  # snapshot público: licitações ficam fora
            if not faltou(ex, "hub.licitacoes"):
                raise
            return 404, pagina("Não achei", NAO_ACHEI, publica=True), {}
        pid, acao = m.groups()
        t = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t", [""])[0]
        if not lic.token_ok(pid, acao, t):
            return 403, pagina("Link inválido", ui.erro("Esse link não vale."), publica=True), {}
        verbo = "Entrar nesta licitação" if acao == "entrar" else "Passar desta licitação"
        form = (f'<form method="post" action="/l/{e(pid)}/{acao}"><input type="hidden" name="t" value="{e(t)}">'
                f'<button type="submit">{verbo}</button></form>')
        return 200, pagina(verbo, ui.card(verbo, form), publica=True), {}

    def _licitacao_post(self, m):
        try:
            from hub.licitacoes import email_diario as lic
        except ImportError as ex:  # snapshot público: licitações ficam fora
            if not faltou(ex, "hub.licitacoes"):
                raise
            return self._fim(404)
        if not limite.post_ok("ip:" + self._ip()):
            return self._fim(429, None, {"Retry-After": "60"})
        campos = self._campos(2000) if self._origem_ok() else None
        if campos is None:
            return self._fim(403)
        try:
            ok, msg = lic.decidir(m.group(1), m.group(2), campos.get("t", ""))
        except (Exception, SystemExit) as ex:  # noqa: BLE001  (criar_issue dá SystemExit; o servidor não pode cair)
            ok, msg = False, f"Não consegui agora ({type(ex).__name__}). Tente de novo pelo e-mail."
        corpo = pagina("Licitação", ui.card("Licitação", f"<p>{e(msg)}</p>") if ok else ui.erro(msg), publica=True)
        self._html(200 if ok else 400, corpo, {})

    def _config_post(self, caminho, campos, sessao):
        """Telas de configuração (hub/config_telas.py): CSRF da sessão e papel com a permissão "config" (só dono), como
        o GET de /config/...; quem agiu (e-mail da sessão) vai à rota e à auditoria."""
        _pedido.sessao = sessao
        try:
            if not csrf_ok(campos.get("csrf")) or not usuarios.pode(sessao["papel"], usuarios.permissao_get(caminho)):
                return self._fim(403)
            status, destino = config_telas.tratar_post(caminho, campos, sessao["email"], self._ip(), True)
        finally:
            _pedido.sessao = None
        ok = status == 303 and "?ok=" in destino
        usuarios.auditar(sessao["email"], "config", f"{caminho} {campos.get('acao', '')}".strip(), "ok" if ok else "erro",
                         self._ip())
        if status == 200:  # a rota devolveu a página (formulário com erro no campo)
            return self._html(200, destino, {})
        return self._fim(status, destino)

    def do_POST(self):  # noqa: N802
        _pedido.sessao = None
        caminho = urllib.parse.urlsplit(self.path).path
        lic = RE_LICITACAO.fullmatch(caminho)
        if lic:  # link assinado do e-mail: o token do formulário é a credencial, sem sessão
            return self._licitacao_post(lic)
        token, sessao = self._sessao()
        if caminho == "/comecar":
            return self._comecar_post(sessao)
        if caminho == "/entrar":
            return self._entrar_post()
        if caminho == "/convite":
            return self._convite_post()
        if comecar.aberto():  # nenhum dono ainda: nada de ação, só o assistente
            return self._fim(303, "/comecar")
        if not sessao:
            return self._fim(303, "/entrar")
        if not limite.post_ok("sessao:" + usuarios._sha(token)[:16]):  # 60 por minuto por sessão
            return self._fim(429, None, {"Retry-After": "60"})
        partes = [p for p in caminho.split("/") if p]
        campos = self._campos(400000 if partes[-1:] == ["video-salvar"] else 20000) if self._origem_ok() else None
        if campos is None:
            return self._fim(403)
        if "/" + "/".join(partes) in config_telas.ROTAS_POST:
            return self._config_post("/" + "/".join(partes), campos, sessao)
        if caminho != "/sair" and (len(partes) != 2 or partes[0] != "acao"):
            return self._fim(404)
        _pedido.sessao = sessao
        try:
            if not csrf_ok(campos.get("csrf")):  # sem token ou de outra sessão
                return self._fim(403)
            if caminho == "/sair":
                usuarios.sair(token)
                usuarios.auditar(sessao["email"], "sair", ip=self._ip())
                return self._fim(303, "/entrar", {"Set-Cookie": cookie_sessao("", 0)})
            if not usuarios.pode(sessao["papel"], "acao:" + partes[1]):  # depois do CSRF: quem forja não sabe o papel
                return self._fim(403)
            agora = dt.datetime.now(dt.timezone.utc)
            try:
                ok, msg, *extra = acoes.executar(partes[1], campos, agora, quem=sessao["email"], ip=self._ip())
            except (Exception, SystemExit) as ex:  # noqa: BLE001  (criar_issue dá SystemExit; o servidor não pode cair)
                ok, msg, extra = False, f"Não consegui agora ({type(ex).__name__}). Tente de novo.", []
            if extra and extra[0].get("convite"):  # o link só aparece na tela, uma vez, para este dono
                with _LINKS_TRAVA:
                    _LINKS[sessao["email"]] = f"https://{self.headers.get('Host', '')}/convite?t={extra[0]['convite']}"
        finally:
            _pedido.sessao = None
        ref = urllib.parse.urlsplit(self.headers.get("Referer") or "")
        mesmo = bool(ref.netloc) and ref.netloc == self.headers.get("Host") and ref.path.startswith("/")
        destino = urllib.parse.quote("/" + ref.path.lstrip("/")) if mesmo else "/"
        self._fim(303, f"{destino}?{'ok' if ok else 'erro'}={urllib.parse.quote(msg)}")

    def _fim(self, status, local=None, cab=None):
        self.send_response(status)
        if local:
            self.send_header("Location", local)
        for k, v in (cab or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def migrar():
    """Senha única antiga → 1º dono (só com 0 usuários). Falha aqui não derruba o painel: /saude segue no ar."""
    try:
        login = usuarios.migrar_senha_antiga()
    except (OSError, ValueError, usuarios.sqlite3.Error) as ex:
        print(f"painel: não migrei a senha antiga ({type(ex).__name__})", file=sys.stderr)
        return None
    if login:
        print(f"dono migrado da senha antiga: entre como {login} e troque a senha", file=sys.stderr)
    return login


if __name__ == "__main__":  # serve pelo módulo hub.painel: o bi_painel o importa, e uma cópia só tem um cache (_CK)
    from hub import painel
    painel.migrar()  # a VPS (systemd) migra sozinha na 1ª subida
    comecar.anunciar()  # instalação sem dono: o token do primeiro acesso aparece no log do contêiner
    ThreadingHTTPServer(painel.ENDERECO, painel.Painel).serve_forever()
