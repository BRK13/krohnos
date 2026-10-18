"""Tela Auditoria (/seo e /seo/<site>): resultados do SEOmator lidos dos resumos em $HUB_DATA/seomator.
Todo site ativo tem card, com nota ou com "falhou: <motivo>"; nenhum some da tela.
HTML só por ui.*; mensagens e URLs vêm do site auditado: sempre escapadas, link só para https."""
import json
from html import escape as e

from hub import seomator, sites, ui

MAX_PAGINAS = 20
# Ids de categoria do SEOmator 6.0.0; id novo aparece cru (ROTULOS.get(c, c)).
ROTULOS = {"core": "Essencial", "technical": "Técnico", "perf": "Desempenho", "links": "Links", "images": "Imagens",
           "security": "Segurança", "crawl": "Rastreamento", "schema": "Dados estruturados", "a11y": "Acessibilidade",
           "content": "Conteúdo", "social": "Redes sociais", "eeat": "E-E-A-T", "url": "URLs", "mobile": "Celular",
           "i18n": "Idiomas", "legal": "Legal e privacidade", "js": "JavaScript", "redirect": "Redirecionamentos",
           "htmlval": "Validação de HTML", "geo": "Busca por IA (GEO)"}


def _ler(arq):
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sites(base_dados=None):
    """Ids dos sites ativos: o card existe mesmo sem resumo (ainda não auditado ou falhou)."""
    return [x["id"] for x in seomator.alvos()]


def _resumos(site_id, base_dados=None):
    """Resumos do site, do mais antigo ao mais novo (os ilegíveis ficam de fora)."""
    rs = [_ler(a) for a in sorted((seomator._dir(base_dados) / site_id).glob("????-??-??.json"))]
    return [r for r in rs if isinstance(r, dict)]


def nome(site_id):
    try:
        return sites.site(site_id).get("nome") or site_id
    except Exception:  # cadastro ausente ou site fora dele: mostra o id
        return site_id


def _dia(d):
    d = str(d or "")
    return f"{d[8:10]}/{d[5:7]}" if len(d) >= 10 else e(d)


def _url(u):
    u = str(u or "")
    return f'<a href="{e(u)}" rel="noopener noreferrer">{e(u)}</a>' if u.startswith("https://") else e(u)


def _conta(r, status):
    return sum(1 for x in r.get("regras") or [] if x.get("status") == status)


def _delta(rs):
    if len(rs) < 2 or not isinstance(rs[-1].get("nota"), (int, float)) or not isinstance(rs[-2].get("nota"), (int, float)):
        return "primeira auditoria"
    d = rs[-1]["nota"] - rs[-2]["nota"]
    return f'{"▲ +" if d > 0 else "▼ −" if d < 0 else "= "}{ui.num(abs(d))} vs auditoria anterior'


def _issues(base_dados=None):
    v = _ler(seomator._dir(base_dados) / "issues.json")
    return v if isinstance(v, dict) else {}


def _falha(sid, base_dados=None):
    f = seomator.status(sid, base_dados).get("falha")
    return f"falhou: {f.get('motivo') or 'sem motivo registrado'}" if f else ""


def _aviso_falha(sid, base_dados=None):
    f = _falha(sid, base_dados)
    return f'<p>{ui.selo(f, "ruim")}</p>' if f else ""


def tela(base_dados=None):
    cards = ""
    for sid in _sites(base_dados):
        rs = _resumos(sid, base_dados)
        if not rs:
            sem = _falha(sid, base_dados)
            cards += ui.card(nome(sid), ui.vazio(sem, "Nova tentativa na próxima madrugada.") if sem
                             else ui.vazio("Ainda sem auditoria.", "O primeiro lote sai na próxima rodada."),
                             direita=f'<a href="/seo/{e(sid)}">Detalhes</a>')
            continue
        r, graves = rs[-1], _conta(rs[-1], "fail")
        notas = [x.get("nota") for x in rs]
        corpo = ui.kpis(
            ui.kpi("Nota", ui.num(r.get("nota")), nota=e(_delta(rs)), serie=notas if len(rs) > 1 else None,
                   href=f"/seo/{e(sid)}"),
            ui.kpi("Erros graves", ui.num(graves), tom="ruim" if graves else "ok", href=f"/seo/{e(sid)}"),
            ui.kpi("Alertas", ui.num(_conta(r, "warn")), href=f"/seo/{e(sid)}"), miudos=True)
        cards += ui.card(nome(sid), _aviso_falha(sid, base_dados) + corpo,
                         direita=f'{_dia(r.get("dia"))} · <a href="/seo/{e(sid)}">Detalhes</a>')
    return cards or ui.card("Auditoria", ui.vazio("A primeira rodada ainda não rodou.",
                                                  "O SEOmator audita um site por madrugada; os resultados aparecem aqui."))


def _paginas(ps):
    ps = [p for p in ps or [] if p]
    mais = f'<li class="mu">e mais {len(ps) - MAX_PAGINAS}</li>' if len(ps) > MAX_PAGINAS else ""
    return f'<ul>{"".join(f"<li>{_url(p)}</li>" for p in ps[:MAX_PAGINAS])}{mais}</ul>'


def site_tela(site_id, base_dados=None):
    """Corpo de /seo/<site>; None se o site não tem resumo (vira 404)."""
    if site_id not in _sites(base_dados):
        return None
    rs = _resumos(site_id, base_dados)
    if not rs:
        sem = _falha(site_id, base_dados)
        return ui.card("Auditoria", ui.vazio(sem, "Nova tentativa na próxima madrugada.") if sem
                       else ui.vazio("Ainda sem auditoria.", "O primeiro lote sai na próxima rodada."))
    r = rs[-1]
    regras = [x for x in r.get("regras") or [] if isinstance(x, dict)]
    fails = [x for x in regras if x.get("status") == "fail"]
    warns = [x for x in regras if x.get("status") == "warn"]
    iss = _issues(base_dados).get(site_id) or {}
    st = seomator.status(site_id, base_dados)
    total = f"de ~{ui.num(r['total'])} no sitemap · " if r.get("total") else ""
    volta = f" · {st['voltas']} volta(s) completa(s)" if st.get("voltas") else ""
    corpo = _aviso_falha(site_id, base_dados) + ui.kpis(
                    ui.kpi("Nota", ui.num(r.get("nota")), nota=e(_delta(rs))),
                    ui.kpi("Páginas auditadas", ui.num(r.get("paginas") or 0), nota=f"{total}atualizado em {_dia(r.get('dia'))}{volta}"),
                    ui.kpi("Erros graves", ui.num(len(fails)), tom="ruim" if fails else "ok"),
                    ui.kpi("Alertas", ui.num(len(warns)), tom="alerta" if warns else None))
    linhas = [[ui.td(f'{x.get("nome") or x.get("regra")} ({x.get("regra")})'), ui.td(len(x.get("paginas") or []), n=True),
               ui.td(x.get("impacto") or 0, n=True), ui.td((iss.get(x.get("regra")) or {}).get("issue") or "—")] for x in fails]
    corpo += ui.card("Erros graves", ui.tabela(["Regra", "Páginas", "Impacto", "Issue"], linhas, num=(1, 2), celular="pilha"),
                     conta=len(fails), justa=True) if fails else ui.card("Erros graves", ui.vazio("Nenhum erro grave nas páginas auditadas."))
    cats = sorted((r.get("categorias") or {}).items(), key=lambda kv: (kv[1] if isinstance(kv[1], (int, float)) else 0))
    corpo += ui.card("Nota por categoria", ui.tabela(["Categoria", "Nota"], [[ROTULOS.get(c, c), ui.td(ui.num(v), n=True)] for c, v in cats],
                                                    num=(1,)), justa=True) if cats else ""
    corpo += ui.card("Evolução", ui.grafico([[x.get("nota") for x in rs]], [_dia(x.get("dia")) for x in rs], maximo=100,
                                            legenda=("Nota geral",), rotulo="Nota geral por dia")
                     if len(rs) > 1 else ui.vazio("Um dia só.", "A linha aparece a partir da segunda auditoria."))
    lista = ""
    for x in fails + warns:
        tom = "ruim" if x.get("status") == "fail" else "alerta"
        lista += (f'<h3>{ui.selo("erro grave" if tom == "ruim" else "alerta", tom)} {e(str(x.get("nome") or x.get("regra")))}'
                  f' <span class="mu">{e(str(x.get("regra")))}</span></h3><p>{e(str(x.get("msg") or ""))}</p>'
                  + (f'<p><b>Correção:</b> {e(str(x.get("fix")))}</p>' if x.get("fix") else "") + _paginas(x.get("paginas")))
    corpo += ui.card("Erros e alertas", lista or ui.vazio("Nada a corrigir."), conta=len(fails) + len(warns))
    return corpo
