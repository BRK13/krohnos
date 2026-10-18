"""Tela Mapa: navega o segundo cérebro (hub/grafo.py). /mapa busca; /mapa/<id> mostra a entidade, o grafo da vizinhança
(SVG, cada nó leva à própria ficha) e as relações. Só leitura. HTML só por ui.*; texto de fora sempre escapado."""
import functools
import json
import math
import urllib.parse
from html import escape as e

from hub import grafo as G, ui
from hub.grafo_saude import projetos, auditar

COR = {"projeto": "#B0976A", "repo": "#8EA382", "dominio": "#C4AD82", "app": "#C98E4E", "vps": "#D07F6E", "banco": "#D07F6E",
       "cron": "#8A8378", "servico": "#8A8378", "segredo": "#A88BA0", "pessoa": "#F5F1E7", "decisao": "#D9D2C3", "issue": "#BDB6A8",
       "pr": "#BDB6A8", "receita": "#8EA382", "pagina": "#E3C98F", "documento": "#8F897D", "memoria": "#A88BA0", "relatorio": "#8F897D", "tarefa": "#8A8378"}
MAX_NOS = 18


def href(eid):
    return "/mapa/" + urllib.parse.quote(eid, safe=":@/")


def _meta(x):
    return " ".join([ui.selo(x["tipo"], mono=True)] + ([e(x["data"])] if x.get("data") else []))


def _grafo_svg(centro, relacoes):
    """Vizinhança de 1 salto num anel em volta do centro; menções por último e só se sobrar lugar."""
    nos = []
    for r in relacoes:
        if r["entidade"]["id"] not in {n["entidade"]["id"] for n in nos}:
            nos.append(r)
        if len(nos) >= MAX_NOS:
            break
    if not nos:
        return ""
    w = h = 520
    cx = cy = w / 2
    raio = 190
    partes = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Vizinhança de {e(centro["nome"])}" style="width:100%;max-width:560px;height:auto">']
    pos = []
    for i, r in enumerate(nos):
        a = -math.pi / 2 + 2 * math.pi * i / len(nos)
        pos.append((cx + raio * math.cos(a), cy + raio * math.sin(a)))
    for (x, y), r in zip(pos, nos):
        partes.append(f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{x:.0f}" y2="{y:.0f}" stroke="#3A3833" stroke-width="1"/>')
        partes.append(f'<text x="{(cx + x) / 2:.0f}" y="{(cy + y) / 2:.0f}" fill="#8F897D" font-size="9" text-anchor="middle">{e(r["rel"])}</text>')
    for (x, y), r in zip(pos, nos):
        n = r["entidade"]
        nome = n["nome"] if len(n["nome"]) <= 22 else n["nome"][:21] + "…"
        partes.append(f'<a href="{e(href(n["id"]))}"><circle cx="{x:.0f}" cy="{y:.0f}" r="9" fill="{COR.get(n["tipo"], "#8A8378")}"/>'
                      f'<text x="{x:.0f}" y="{y + 22:.0f}" fill="#F5F1E7" font-size="11" text-anchor="middle">{e(nome)}</text></a>')
    partes.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="16" fill="{COR.get(centro["tipo"], "#B0976A")}"/>'
                  f'<text x="{cx:.0f}" y="{cy + 34:.0f}" fill="#F5F1E7" font-size="12" text-anchor="middle">{e(centro["nome"][:28])}</text></svg>')
    return "".join(partes)


def tela(q="", tipo=""):
    if not G.disponivel():
        return abas() + ui.card("Mapa", ui.vazio("O grafo ainda não foi montado.", "O cron de 10 min (deploy/grafo.sh) monta na primeira volta."))
    st = G.estado()
    tipo = tipo if tipo in G.TIPOS else ""
    busca = (f'<form class="form" method="get" action="/mapa" role="search">'
             f'<input class="largo" type="search" name="q" value="{e(q)}" maxlength="200" '
             f'placeholder="Pergunte (ex.: onde está a credencial do Supabase, qual repo serve clientea-com.example)" aria-label="Pergunta">'
             + (f'<input type="hidden" name="tipo" value="{e(tipo)}">' if tipo else "")
             + '<button class="prim" type="submit">Buscar</button></form>')
    filtros = ui.filtro("tipo", [("", "Todos")] + [(t, f"{t} ({n})") for t, n in sorted(st["tipos"].items())], tipo, rotulo="Tipo",
                        url=lambda v: "/mapa?" + urllib.parse.urlencode({k: x for k, x in {"q": q, "tipo": v}.items() if x}))
    corpo = ui.card("Perguntar ao cérebro", f'<div class="pilha">{busca}<div class="form">{filtros}</div></div>')
    if q.strip():
        r = G.responder(q, tipo or None, limite=12)
        resposta = "".join(f"<p>{e(l)}</p>" for l in r["resposta"].splitlines())
        itens = [{"titulo": f["nome"], "href": href(f["entidade"]), "meta": _meta(f), "fala": f["trecho"]} for f in r["fontes"]]
        corpo += ui.card("Resposta", resposta) + ui.card("Fontes", ui.lista(itens, solta=True) if itens else ui.vazio("Nada casou."), conta=str(len(itens)))
    else:
        por_tipo = ui.kv([(t, str(n)) for t, n in sorted(st["tipos"].items(), key=lambda x: -x[1])])
        corpo += ui.card("O que o grafo tem", por_tipo, conta=f"{st['entidades']} entidades · {st['relacoes']} relações")
    return abas() + corpo


def entidade(eid):
    """(titulo, html) da entidade ou None."""
    r = G.vizinhos(eid, limite=200)
    if not r or not G.disponivel():
        return None
    c = r["entidade"]
    ix = G._indice()
    full = ix.ent[c["id"]]
    ficha = [("tipo", full["tipo"]), ("id", full["id"])]
    for k in ("caminho", "link", "data"):
        if full[k]:
            ficha.append((k, full[k]))
    if full["alias"]:
        ficha.append(("também", ", ".join(full["alias"])))
    corpo = ui.card(full["nome"], f"<p>{e(full['desc'])}</p>" + ui.kv(ficha))
    fortes = [x for x in r["relacoes"] if x["rel"] != "menciona"]
    svg = _grafo_svg(c, fortes + [x for x in r["relacoes"] if x["rel"] == "menciona"])
    if svg:
        corpo += ui.card("Vizinhança", svg, conta=f"{r['total']} relações")
    grupos = {}
    for x in r["relacoes"]:
        grupos.setdefault(f"{'→' if x['sentido'] == 'sai' else '←'} {x['rel']}", []).append(x["entidade"])
    for nome, ents in grupos.items():
        itens = [{"titulo": n["nome"], "href": href(n["id"]), "meta": _meta(n), "fala": n["desc"][:200]} for n in ents]
        corpo += ui.card(nome, ui.lista(itens, solta=True), conta=str(len(itens)))
    return full["nome"], corpo


def abas(grafo=False):
    return ('<nav class="form" aria-label="Vistas do mapa"><a href="/mapa?busca=1" '
            + ('aria-current="page"' if not grafo else '') + '>Busca</a> · <a href="/mapa/grafo" '
            + ('aria-current="page"' if grafo else '') + '>Grafo</a> · <a href="/mapa/saude">Saúde do mapa</a></nav>')


@functools.lru_cache(maxsize=1)
def _json_inteiro(ix):
    # Projeção explícita: nunca serializar entidades, descrições ou extras inteiros.
    if ix is None:
        return '{"nos":[],"arestas":[]}'
    donos = projetos(ix)
    arestas = [{"de": de, "para": para, "tipo": rel} for de, rs in ix.sai.items() for rel, para in rs]
    nos = [{"id": n["id"], "tipo": n["tipo"], "nome": n["nome"],
            "projeto": donos.get(n["id"]),
            "grau": len(ix.sai.get(n["id"], [])) + len(ix.entra.get(n["id"], []))}
           for n in ix.ent.values()]
    return json.dumps({"nos": nos, "arestas": arestas}, ensure_ascii=False, separators=(",", ":"))


def grafo_json():
    return _json_inteiro(G._indice())


def inteiro():
    return abas(True) + """<section data-ui class="mapa-inteiro" aria-label="Grafo completo">
<style>
.mapa-inteiro{background:#171816;color:#F5F1E7;margin-top:20px;padding:16px;min-width:0}
.mapa-barra{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.mapa-barra form{display:flex;gap:8px;flex:1;min-width:0}.mapa-barra input{min-width:0;width:100%;font:inherit}
.mapa-barra button,.mapa-legenda button{min-height:44px;font:inherit}.mapa-marca{width:36px;height:36px}
.mapa-legenda{display:flex;flex-wrap:wrap;gap:4px 12px;margin:12px 0}
.mapa-legenda button{background:transparent;color:#F5F1E7;border:0;padding:4px;cursor:pointer}
.mapa-legenda button[aria-pressed="false"]{opacity:.4}.mapa-legenda i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
#mapa-canvas{height:clamp(360px,65dvh,850px);width:100%;touch-action:none;overflow:hidden}
@media(max-width:600px){.mapa-barra form{flex-basis:calc(100% - 52px)}.mapa-barra #mapa-ajustar{margin-left:auto}#mapa-projetos{flex-wrap:nowrap;overflow-x:auto}#mapa-projetos button{flex-shrink:0}}
#mapa-status{min-height:24px;margin:8px 0;color:#D9D2C3}.mapa-ajuda{font-size:14px;margin:8px 0 0}
#mapa-tipos summary{display:flex;align-items:center;min-height:44px;cursor:pointer}
</style>
<div class="mapa-barra"><img class="mapa-marca" src="/marca/kronos-icone-escuro.svg" alt="Ampulheta">
<form id="mapa-busca" role="search"><input id="mapa-termo" type="search" aria-label="Buscar entidade no grafo" placeholder="Encontre uma entidade" maxlength="200" list="mapa-opcoes"><datalist id="mapa-opcoes"></datalist><button type="submit">Localizar</button></form>
<button id="mapa-ajustar" type="button">Enquadrar</button></div>
<details id="mapa-tipos"><summary>Tipos de entidade · filtrar</summary><div id="mapa-legenda" class="mapa-legenda" aria-label="Tipos visíveis"></div></details>
<p id="mapa-status" role="status">Carregando o segundo cérebro…</p>
<div id="mapa-projetos" class="mapa-legenda" aria-label="Cores dos projetos"></div>
<div id="mapa-canvas" aria-label="Grafo interativo. Use a busca para localizar uma entidade."></div>
<a id="mapa-ficha" hidden>Abrir entidade selecionada →</a>
<p class="mapa-ajuda">Cor = projeto · Anel = tipo · VPS = âncora grande · Arestas: contínua roda_em, tracejada serve, pontilhada usa/depende_de.</p><p class="mapa-ajuda">Arraste os pontos · Role ou use pinça para ampliar · Clique para abrir a ficha</p>
</section><script src="/mapa/force-graph-1.49.4.min.js" defer></script><script src="/mapa/grafo.js" defer></script>"""


def saude():
    r = auditar(G._indice())
    corpo = ui.card("Saúde do mapa", f"<p>{r['baixo_grau']} de {r['total']} nós com até 1 vizinho ({r['percentual']}%). Meta: menos de 10%.</p>" + ''.join(f'<p>{e(x)}</p>' for x in r['limitacoes']))
    if r.get('arquitetura'):
        a = r['arquitetura']
        corpo += ui.card('Projetos da arquitetura', f"<p>{a['sem_projeto']} de {a['total']} nós sem projeto ({a['percentual_sem_projeto']}%). VPS compartilhadas não entram nesta conta. Ocultos no grafo; revisar os achados abaixo.</p>")
    if r.get('cobertura'):
        c = r['cobertura']
        corpo += ui.card('Cobertura', f"<p>Segredos com hash: {c['segredos_com_hash']}/{c['segredos']}. Issues com estado: {c['issues_com_estado']}/{c['issues']}.</p>")
    grupos = {}
    for a in r['achados']:
        grupos.setdefault(a['tipo'], []).append(a)
    for tipo, itens in sorted(grupos.items()):
        corpo += ui.card(tipo, ''.join('<p>' + ', '.join(f'<a href="{e(href(x))}">{e(x)}</a>' for x in a['nos']) + ' — ' + e(a['acao']) + '</p>' for a in itens), conta=str(len(itens)))
    return abas() + corpo
