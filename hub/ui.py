"""Biblioteca de UI do painel (painel F1 fase 1): estrutura da direção 2,
identidade da marca (ampulheta: carvão, creme e latão), tema escuro padrão e claro.

Única fonte de CSS e de HTML de componente. As telas (painel.py, bi_painel.py, mercado_tela.py, seo_local_tela.py) só
montam dados e chamam estas funções. Sem framework e sem build: HTML em f-string, CSS com custom properties.

Regras (os testes em tests/test_ui.py travam):
- nada de `style=` no HTML: largura de barra sai das classes .p0..p100, atraso de entrada de .i1..i12;
- nenhum número solto no CSS fora de TOKENS (só filetes de 1-2 px e as quebras de tela);
- texto que vem de fora passa por html.escape; parâmetro chamado `corpo`, `direita`, `meta`, `acoes` ou célula de
  tabela já é HTML (quem chama escapa, de preferência com td()).
- movimento: entrada sobe 10 px com desfoque (520 ms, 55 ms entre itens); tudo desliga com prefers-reduced-motion.

Contrato (docs/plans/feitos/2026-10-14-painel-f1.md): pagina, card, kpi, kpis, tabela, td, selo, vazio, erro, barra, grafico,
filtro, botao, lista, num, quando, ha.
"""
import datetime as dt
import html
import json
import re
import urllib.parse
from pathlib import Path

from hub import config, data, sites
from hub.comum.datas import BRT

e = html.escape

# O nome do produto mora só aqui (título, cabeçalho, login, rodapé e mensagens que citam o painel).
NOME_PRODUTO = config.get("produto.nome")

TONS = ("ok", "alerta", "ruim", "neutro")

# Menu agrupado por intenção (docs/painel-f1/arquitetura.md). (grupo, ícone do celular, [(caminho, rótulo)])
MENU = [
    ("Hoje", "◉", [("/pendencias", "Pendências"), ("/metas", "Metas"), ("/operacao", "Agora"), ("/cerebro", "Cérebro"), ("/fluxo", "Fluxo"),
                   ("/propostas", "Propostas")]),
    ("Negócios", "◆", [("/bi", "Visão geral"), ("/bi/comparar", "Comparar"), ("/virais", "Virais"),
                 ("/destaques", "Destaques"), ("/portfolio", "Portfólio"), ("/fabrica", "Fábrica"), ("/provas", "Provas")]),
    ("Máquina", "▲", [("/agentes", "Agentes"), ("/linear", "Linear"), ("/uso", "Uso e cotas"), ("/github", "GitHub"), ("/monitor", "Monitor"), ("/redteam", "Red team"),
                      ("/biblioteca", "Biblioteca"), ("/mapa", "Mapa"), ("/erros", "Erros"),
                      ("/confiabilidade", "Confiabilidade")]),
    ("SEO", "●", [("/seo", "Auditoria"), ("/velocidade", "Velocidade de campo"), ("/concorrentes", "Concorrentes"), ("/relatorio", "Coleta diária")]),
    # Configurar: cada tarefa da frente 3 acrescenta o seu item quando a tela existe. Alvo final: Projetos
    # (/config/projetos), Cofre, Conexões (/config/conexoes), Agentes (/agentes), Rotinas (/config/rotinas), Plugins (/config/plugins).
    ("Configurar", "\u2699\ufe0e", [("/config/projetos", "Projetos"), ("/config/cofre", "Cofre"), ("/config/conexoes", "Conexões"),
                              ("/config/agentes", "Agentes"), ("/config/rotinas", "Rotinas"), ("/config/plugins", "Plugins")]),
]  # só o núcleo: os plugins ligados entram por menu() (hub/plugins.py: MENU de cada pacote)


MENU_DONO = ("Config", "■", [("/config/usuarios", "Usuários")])  # telas de dono: só entram com dono=True
MARCA_DEMO = "demo.json"  # escrito por `kronos demo` (hub/demo.py) em <dados>/; existe = há dado de demonstração
FAIXA_DEMO = "Dados de demonstração: tudo o que aparece aqui é inventado. Para tirar, rode kronos demo --limpar."


def demo_ativa():
    """Um stat por página (a faixa sai em todas); pasta ou arquivo ausente = sem demo."""
    try:
        return (data() / MARCA_DEMO).is_file()
    except OSError:
        return False


def menu(dono=False):
    """MENU com os itens dos plugins ligados. Item (grupo, caminho, rótulo[, antes]): grupo novo vira o último, ícone ◇.
    dono: acrescenta o grupo de configuração (só o dono abre essas telas)."""
    from hub import plugins
    grupos = [(g, ic, list(itens)) for g, ic, itens in MENU]
    for grupo, caminho, rotulo, *resto in plugins.menu():
        alvo = next((i for g, _, i in grupos if g == grupo), None)
        if alvo is None:
            alvo = []
            grupos.append((grupo, "◇", alvo))
        pos = next((n for n, (h, _) in enumerate(alvo) if resto and h == resto[0]), len(alvo))
        alvo.insert(pos, (caminho, rotulo))
    if dono:
        grupos.append((MENU_DONO[0], MENU_DONO[1], list(MENU_DONO[2])))
    return grupos


def negocios():
    """[(id, nome)] dos sites ativos do cadastro; o menu dos negócios sai daqui, nada de nome colado no código."""
    try:
        return [(s["id"], s["marca"]["nome"]) for s in sites.ativos()]
    except sites.SiteInvalido:  # cadastro com erro não derruba toda tela; `hub.sites validar` mostra o campo
        return []

FONTES = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" '
          'crossorigin><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&'
          'family=Geist+Mono:wght@400;500&family=Marcellus&display=swap">')

# ---------- tokens: a marca (ampulheta): carvão, creme e latão. Todo número do CSS mora aqui. ----------
# Escuro é o padrão (:root); o claro (:root[data-tema=claro]) é a prancha da logo. Os dois definem as mesmas cores.
# --ac (latão) é fundo: botão principal, item ativo, foco, série principal. Texto/link/ícone em latão usa --ac-forte.
# Ajustes de contraste (≥ 4,5:1, inclusive em selo tingido): --ru escuro #C4705F→#D07F6E, --al claro #9A6424→#8C5A1E,
# --ac-forte claro #7D6A44→#76643F.

TOKENS = """
:root{color-scheme:dark;
--f-texto:Geist,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--f-titulo:Marcellus,Georgia,serif;
--fonte-numero:Geist,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--f-mono:"Geist Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
--bg:#161616;--sup:#1C1C1C;--sup-2:#232220;--linha:#2B2A27;--linha-2:#3A3833;--sel:#201F1D;--campo:#6B675F;
--tx:#F5F1E7;--tx-2:#BDB6A8;--tx-3:#8F897D;--ac:#B0976A;--ac-tx:#1C1C1C;--ac-forte:#C4AD82;--ac-fundo:rgba(176,151,106,.12);
--ok:#8EA382;--al:#C98E4E;--ru:#D07F6E;
--g1:#B0976A;--g2:#D9D2C3;--g3:#8EA382;--g4:#C4705F;--g5:#8A8378;--g6:#A88BA0;
--sombra:0 1px 2px rgba(0,0,0,.28);--sombra-borda:rgba(0,0,0,.45);--sombra-aviso:0 6px 16px -4px rgba(0,0,0,.5);--veu:rgba(10,10,10,.62);--grao-op:.07;
--grao:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
--fs:15px;--fs-s:14px;--fs-xs:12px;--fs-xxs:11px;--lh:1.55;--h1:38px;--h2:15px;--h3:20px;--kpi:46px;--kpi-m:30px;
--r:14px;--r-s:10px;--r-p:6px;--pad:22px;--item-h:36px;--lado:248px;--max:1120px;--toque:44px;--bt:40px;--linha-h:52px;
--s1:4px;--s2:8px;--s3:14px;--s4:20px;--s5:28px;--s6:40px;--s7:56px;--fim:96px;
--ponto:6px;--ponto-g:10px;--icone:28px;--spark:32px;--graf:180px;--graf-a:320px;--graf-b:64px;--med-h:6px;
--cerebro-h:440px;--aviso-w:440px;--texto-w:70ch;--col-min:210px;--kpi-min:170px;--grao-t:180px;--kpi-cel:22px;--kpi-flui:17cqi;--sel-seta:28px;
--marca-esp:.24em;--simbolo:30px;--simbolo-g:136px;--nome-g:30px;--pescoco:18px;--pescoco-v:24px;--carrega:64px;--carrega-p:40px;--carrega-fundo:#F5F1E7;
--vidro:14px;--desfoque:4px;--sobe:10px;--sobe-vt:8px;--hover-sobe:-1px;
--pilula:999px;--dur:200ms;--dur-entra:520ms;--dur-rapida:90ms;--passo:55ms;--ease:cubic-bezier(.16,1,.3,1);--pulso:2.4s;--aviso-s:6s}
:root[data-tema=claro]{color-scheme:light;
--bg:#F5F1E7;--sup:#FBF9F3;--sup-2:#EFEADF;--linha:#DDD9D0;--linha-2:#C9C3B7;--sel:#EDE7DA;--campo:#8F897D;
--tx:#1C1C1C;--tx-2:#57534B;--tx-3:#6E695F;--ac:#B0976A;--ac-tx:#1C1C1C;--ac-forte:#76643F;--ac-fundo:rgba(176,151,106,.16);
--ok:#4F6B47;--al:#8C5A1E;--ru:#A2483A;
--g1:#B0976A;--g2:#57534B;--g3:#6F8763;--g4:#A2483A;--g5:#A39D92;--g6:#8A6C82;
--carrega-fundo:transparent;--sombra:0 1px 2px rgba(28,28,28,.05);--sombra-borda:rgba(28,28,28,.16);--sombra-aviso:0 6px 16px -4px rgba(28,28,28,.18);--veu:rgba(28,28,28,.32);--grao-op:.06}
@media (max-width:860px){:root{--h1:30px;--kpi:36px;--kpi-m:26px;--pad:16px;--bt:44px}}
"""

CSS = """
*{box-sizing:border-box}html{position:relative;min-height:100%;-webkit-text-size-adjust:100%;scrollbar-gutter:stable}
body{margin:0;background:var(--bg);color:var(--tx);font:var(--fs)/var(--lh) var(--f-texto);font-variant-numeric:tabular-nums lining-nums;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;overflow-wrap:break-word}
/* quebra: entre palavras (hifeniza em pt-BR quando preciso); quebra em qualquer ponto só em URL e domínio (.url) */
.url{overflow-wrap:anywhere}td,.item .tit,.kpi .r,.cartao,.vazio{hyphens:auto}
/* grão de papel da prancha: textura parada na página inteira, atrás do conteúdo, nunca recebe clique */
body::before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;background:var(--grao);background-size:var(--grao-t);opacity:var(--grao-op)}
a{color:var(--ac-forte);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--ac-forte) 40%,transparent);text-underline-offset:3px;
transition:color var(--dur),text-decoration-color var(--dur)}a:hover{text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--ac-forte);outline-offset:2px;border-radius:var(--r-p)}
.demo-faixa{margin:0 0 var(--s4);padding:var(--s2) var(--s3);border:1px solid var(--ac);border-radius:var(--r-s);background:var(--ac-fundo);color:var(--ac-forte);font-size:var(--fs-s)}
.pular{position:absolute;left:var(--s2);top:calc(-1 * var(--s7));z-index:20;background:var(--ac);color:var(--ac-tx);padding:var(--s3);
border-radius:var(--r-s);font-weight:600;text-decoration:none}.pular:focus{top:var(--s2)}
.app{display:grid;grid-template-columns:var(--lado) minmax(0,1fr);min-height:100dvh}
/* menu lateral */
nav.lado{position:sticky;top:0;height:100dvh;overflow-y:auto;display:flex;flex-direction:column;padding:var(--s5) var(--s3) var(--s3);border-right:1px solid var(--linha);
view-transition-name:lado;scrollbar-width:thin}
/* a marca: ampulheta (traço na cor do texto, areia em latão) + o nome em Marcellus, caixa alta e espaçado */
.marca{display:flex;align-items:center;gap:var(--s3);padding:0 var(--s2);margin:0 0 var(--s4);text-decoration:none;color:var(--tx);
font:400 var(--h2)/1 var(--f-titulo);letter-spacing:var(--marca-esp);text-transform:uppercase;white-space:nowrap}
.simbolo{display:block;flex:none;height:var(--simbolo);width:auto;color:var(--tx)}.simbolo .areia{fill:var(--ac)}
.marca.cel{display:none}
.grupo{margin:0 0 var(--s3)}.grupo h3{margin:0 0 var(--s1);padding:0 var(--s2);font-size:var(--fs-xs);font-weight:600;color:var(--tx-3);
text-transform:uppercase;letter-spacing:.08em}
.grupo a{position:relative;display:flex;align-items:center;gap:var(--s2);min-height:var(--item-h);padding:0 var(--s2);border-radius:var(--r-s);
color:var(--tx-2);text-decoration:none;font-size:var(--fs-s);transition:color var(--dur),background-color var(--dur)}
.grupo a:hover{color:var(--tx);background:var(--sel)}
.grupo a[aria-current]{color:var(--tx);background:var(--sel);font-weight:500}
.grupo a[aria-current]::before{content:"";position:absolute;left:calc(-1 * var(--s3));top:25%;bottom:25%;width:2px;background:var(--ac);
border-radius:2px;view-transition-name:marcador}
.grupo .n{margin-left:auto;font:500 var(--fs-xs)/var(--s4) var(--f-texto);min-width:var(--s4);padding:0 var(--r-p);text-align:center;
border-radius:var(--pilula);background:var(--ac);color:var(--ac-tx)}
.negocios{display:flex;flex-wrap:wrap;gap:var(--s1);padding:var(--s1) var(--s2) 0}
.grupo .negocios a{min-height:var(--s5);padding:0 var(--s2);font-size:var(--fs-xs);border:1px solid var(--linha);border-radius:var(--pilula)}
.grupo .negocios a[aria-current]::before{display:none}.grupo .negocios a[aria-current]{border-color:var(--ac);color:var(--tx)}
.rodape{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:var(--s2);margin:var(--s3) 0 0;padding:0 var(--s2);
font-size:var(--fs-xs);color:var(--tx-3)}.rodape .pescoco{flex:1 1 100%}nav.lado .rodape{margin-top:auto}
/* pescoço da ampulheta: divisor discreto entre seções (filetes com as duas curvas que se estreitam no meio) */
.pescoco{display:flex;align-items:center;gap:var(--s2);color:var(--linha-2)}.pescoco::before,.pescoco::after{content:"";flex:1;height:1px;background:var(--linha)}
.pescoco-g{display:block;flex:none;width:var(--pescoco);height:var(--pescoco);fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round}
.pescoco-g circle{fill:var(--ac);stroke:none}
.tema-bt{display:inline-flex;align-items:center;gap:var(--s2);min-height:var(--toque);margin:0;padding:0 var(--s3) 0 var(--s2);
border:1px solid var(--linha);border-radius:var(--pilula);background:none;color:var(--tx-2);font:500 var(--fs-xs)/1 var(--f-texto);cursor:pointer;
transition:color var(--dur),border-color var(--dur)}.tema-bt:hover{color:var(--tx);border-color:var(--linha-2)}
.tema-bt[aria-pressed=true]{border-color:var(--ac);background:var(--ac-fundo);color:var(--tx)}
.tema-bt::before{content:"";width:var(--ponto-g);height:var(--ponto-g);border-radius:50%;border:1px solid currentColor;
box-shadow:inset calc(var(--ponto-g) / 2) 0 0 currentColor}.fim-tema{display:none}.publica .fim-tema{display:flex;justify-content:center}
/* topo da tela */
main{padding:var(--s6) var(--s7) var(--fim);max-width:calc(var(--max) + 2 * var(--s7));width:100%;min-width:0}
.topo{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--s4);flex-wrap:wrap;margin:0 0 var(--s6)}
.olho{font-size:var(--fs-xs);font-weight:600;color:var(--tx-3);text-transform:uppercase;letter-spacing:.1em;margin:0 0 var(--s2)}
h1{margin:0;font:400 var(--h1)/1.1 var(--f-titulo);letter-spacing:.005em;view-transition-name:titulo;text-wrap:balance}
.sub{margin:var(--s2) 0 0;color:var(--tx-2);font-size:var(--fs-s)}
.topo .acoes{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center}
.vivo-ponto{display:inline-flex;align-items:center;gap:var(--r-p)}.vivo-ponto::before{content:"";width:var(--ponto);height:var(--ponto);
border-radius:50%;background:var(--ok);animation:pulso var(--pulso) ease-out infinite}
@keyframes pulso{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent)}70%,100%{box-shadow:0 0 0 var(--ponto) transparent}}
h2{margin:0;font-size:var(--h2);font-weight:600;letter-spacing:-.01em;line-height:1.3;color:var(--tx)}
h3{margin:0 0 var(--s2);font-size:var(--fs-s);font-weight:600}
p{margin:0 0 var(--s3)}p:last-child{margin-bottom:0}
.mu{color:var(--tx-2);font-size:var(--fs-s);font-weight:400}.pp{color:var(--tx-3);font-size:var(--fs-xs)}
.ok{color:var(--ok)}.alerta{color:var(--al)}.ruim{color:var(--ru)}
/* blocos e caixas */
.bloco{margin:0 0 var(--s6);min-width:0}
.bloco>header{display:flex;align-items:center;flex-wrap:wrap;gap:var(--s2);margin:0 0 var(--s3)}
.bloco>header .dir{margin-left:auto;font-size:var(--fs-xs);color:var(--tx-3);display:flex;gap:var(--s2);align-items:center}
.cont{font:500 var(--fs-xs)/var(--s4) var(--f-texto);padding:0 var(--s2);border-radius:var(--pilula);background:var(--sup-2);color:var(--tx-2)}
.cont.quente{background:var(--ac-fundo);color:var(--ac-forte)}
.caixa{background:var(--sup);border:1px solid var(--linha);border-radius:var(--r);padding:var(--pad);box-shadow:var(--sombra);min-width:0;
transition:border-color var(--dur)}.caixa.justa{padding:var(--s2) 0 0;overflow:hidden}.caixa.justa>.vazio{margin:var(--s3) var(--pad) var(--pad)}
.caixa+.caixa{margin-top:var(--s3)}.caixa.alerta-faixa{border-color:color-mix(in srgb,var(--ru) 50%,transparent);
background:color-mix(in srgb,var(--ru) 8%,var(--sup))}
.grade{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,var(--col-min)),1fr));gap:var(--s3);margin:0 0 var(--s6)}
.grade.n2{grid-template-columns:repeat(2,minmax(0,1fr))}.grade.n3{grid-template-columns:repeat(3,minmax(0,1fr))}
.grade.n4{grid-template-columns:repeat(4,minmax(0,1fr))}.grade>.bloco{margin:0}
.dupla{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:var(--s3);margin:0 0 var(--s6)}.dupla>.bloco{margin:0}
.cerebro-mini{display:block;width:100%;height:max(var(--cerebro-h),40vh);border:0;margin-top:calc(-1 * var(--s2))}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,var(--kpi-min)),1fr));gap:var(--s3);margin:0 0 var(--s6);
view-transition-name:kpis}
.kpi{background:var(--sup);border:1px solid var(--linha);border-radius:var(--r);padding:var(--s4);box-shadow:var(--sombra);min-width:0;container-type:inline-size;
display:flex;flex-direction:column;transition:border-color var(--dur),transform var(--dur) var(--ease)}
a.kpi{color:inherit;text-decoration:none}a.kpi:hover{border-color:var(--linha-2);transform:translateY(var(--hover-sobe))}
.kpi .r{font-size:var(--fs-xs);color:var(--tx-2);display:flex;align-items:center;gap:var(--r-p)}
.kpi .v{display:block;margin:var(--s2) 0 var(--s1);font:600 var(--kpi)/1.05 var(--fonte-numero);letter-spacing:-.01em;font-variant-numeric:tabular-nums lining-nums;
font-size:clamp(var(--kpi-cel),var(--kpi-flui),var(--kpi));text-wrap:balance}
.kpi .v.quente{color:var(--ac-forte)}.kpi .v.alerta{color:var(--al)}.kpi .v.ruim{color:var(--ru)}
.kpi .d{font-size:var(--fs-xs);font-weight:500}.kpi .d.neutro{color:var(--tx-3)}
.kpi .nota{font-size:var(--fs-xs);color:var(--tx-3);margin-top:var(--s1)}.kpi .spark{margin-top:auto;padding-top:var(--s2)}
.kpis.miudos .kpi .v{font-size:clamp(var(--kpi-cel),var(--kpi-flui),var(--kpi-m))}
/* selos */
.selo{display:inline-flex;align-items:center;gap:var(--r-p);height:var(--s4);padding:0 var(--s2);border-radius:var(--r-p);
font:500 var(--fs-xs)/1 var(--f-texto);background:var(--sup-2);color:var(--tx-2);white-space:nowrap;vertical-align:1px;max-width:100%}
.selo.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.selo.alerta{color:var(--al);background:color-mix(in srgb,var(--al) 13%,transparent)}
.selo.ruim{color:var(--ru);background:color-mix(in srgb,var(--ru) 13%,transparent)}
.selo.mono{font-family:var(--f-mono);color:var(--tx)}
.chips{display:flex;flex-wrap:wrap;gap:var(--r-p)}
/* lista de itens (pedidos, issues, consertos) */
.lista{display:grid;gap:var(--s2)}
.cartao{background:var(--sup);border:1px solid var(--linha);border-radius:var(--r);padding:var(--pad);box-shadow:var(--sombra);min-width:0;
transition:border-color var(--dur),transform var(--dur) var(--ease)}.cartao:hover{border-color:var(--linha-2);transform:translateY(var(--hover-sobe))}
.item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:var(--s2) var(--s4);align-items:center}
.item .tit{display:block;margin:0;font-size:var(--h2);font-weight:500;line-height:1.35;color:var(--tx);text-decoration:none;text-wrap:pretty}a.tit:hover{color:var(--ac-forte)}
.item .meta{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s1) var(--s3);margin-top:var(--r-p);font-size:var(--fs-xs);color:var(--tx-2)}
.item .meta a{color:var(--ac-forte);text-decoration:none}.item .meta a:hover{text-decoration:underline}
.item .fala{grid-column:1/-1;margin:2px 0 0;padding:var(--s2) var(--s3);border-left:2px solid var(--ac);color:var(--tx);font-size:var(--fs-s);
background:var(--ac-fundo);border-radius:0 var(--r-s) var(--r-s) 0}
.item .acoes,.item>form{display:flex;flex-wrap:wrap;gap:var(--s2);margin:0}
.item .mais{grid-column:1/-1}
.linhas>.item{padding:var(--s3) 0;border-bottom:1px solid var(--linha)}.linhas>.item:first-child{padding-top:0}
.linhas>.item:last-child{border-bottom:0;padding-bottom:0}
/* botões e formulários */
.bt,.form button{display:inline-flex;align-items:center;justify-content:center;gap:var(--r-p);min-height:var(--bt);padding:0 var(--s4);
border-radius:var(--pilula);border:1px solid var(--linha-2);background:var(--sup-2);color:var(--tx);font:500 var(--fs-s)/1 var(--f-texto);cursor:pointer;
text-decoration:none;transition:background-color var(--dur),border-color var(--dur),transform var(--dur-rapida) var(--ease),filter var(--dur)}
.bt:hover,.form button:hover{border-color:var(--tx-3)}.bt:active,.form button:active{transform:scale(.97)}
.bt.prim,.form button.prim{background:var(--ac);border-color:var(--ac);color:var(--ac-tx);font-weight:600}
.bt.prim:hover,.form button.prim:hover{filter:brightness(1.08)}
.bt.perigo,.form button.perigo{background:none;color:var(--tx-2)}
.bt.perigo:hover,.form button.perigo:hover{color:var(--ru);border-color:color-mix(in srgb,var(--ru) 50%,transparent)}
.form button:disabled{opacity:.6;cursor:wait}
.form{display:flex;flex-wrap:wrap;gap:var(--s2);margin:0}
.form input,.form select,.form textarea{background:var(--bg);color:var(--tx);border:1px solid var(--campo);border-radius:var(--r-s);
padding:var(--s2) var(--s3);font:inherit;min-height:var(--toque);min-width:0;flex:1 1 100%;transition:border-color var(--dur)}
.form label.campo{display:flex;flex-direction:column;gap:var(--s1);flex:1 1 var(--col-min);min-width:0}.form label.campo.cheio{flex-basis:100%}
.form select{flex:1 1 var(--col-min)}.form .largo{flex:999 1 var(--col-min)}.form textarea{min-height:var(--s7);resize:vertical}
.form ::placeholder{color:var(--tx-3)}.form input:hover,.form select:hover,.form textarea:hover{border-color:var(--tx-3)}
.form input:focus,.form select:focus,.form textarea:focus{outline:2px solid var(--ac-forte);outline-offset:1px;border-color:var(--ac-forte)}
details.mais summary{display:inline-flex;align-items:center;min-height:var(--toque);cursor:pointer;color:var(--tx-2);list-style:none;
font-size:var(--fs-s);transition:color var(--dur)}details.mais summary::-webkit-details-marker{display:none}
details.mais summary:hover{color:var(--tx)}details.mais summary::before{content:"+";margin-right:var(--r-p);font-weight:600}
details.mais[open] summary::before{content:"−"}
dialog{background:var(--sup);color:var(--tx);border:1px solid var(--linha-2);border-radius:var(--r);padding:var(--pad);box-shadow:var(--sombra);
width:min(100% - 2 * var(--s3),calc(var(--max) / 2))}dialog::backdrop{background:var(--veu);backdrop-filter:blur(var(--desfoque))}
dialog h2{margin:0 0 var(--s3)}dialog .fecha{margin-left:auto}
/* vazio e erro */
/* vazio: o pescoço da ampulheta em cima do texto, centrado; erro (.fora): âmbar com "!" ao lado */
.vazio{display:flex;flex-direction:column;align-items:center;gap:var(--s2);margin:0;padding:var(--s4);border:1px dashed var(--linha);
border-radius:var(--r);color:var(--tx-2);font-size:var(--fs-s);text-align:center;text-wrap:balance}.vazio b{color:var(--tx);font-weight:500}
.vazio>.pescoco-g{width:var(--pescoco-v);height:var(--pescoco-v);color:var(--tx-3)}
.vazio.fora{flex-direction:row;gap:var(--s3);text-align:left;border:1px solid color-mix(in srgb,var(--al) 35%,transparent);
background:color-mix(in srgb,var(--al) 7%,transparent)}
.vazio.fora::before{content:"!";display:inline-grid;place-items:center;flex:none;width:var(--icone);height:var(--icone);border-radius:50%;
font-weight:700;font-size:var(--fs-xs);background:color-mix(in srgb,var(--al) 16%,transparent);color:var(--al)}
.caixa>.vazio,.lista+.vazio{border:0;padding:var(--s2) 0}main>.vazio.fora,.bloco>.vazio.fora{margin:0 0 var(--s4)}
/* tabela */
.rola{overflow-x:auto;min-width:0;scrollbar-width:thin;scrollbar-color:var(--linha-2) transparent}
.rola-x{transition:box-shadow var(--dur)}.rola-x[data-mais=dir],.rola-x[data-mais=ambos]{box-shadow:inset calc(-1 * var(--s3)) 0 var(--s3) calc(-1 * var(--s3)) var(--sombra-borda)}
.rola-x th:first-child,.rola-x td:first-child{position:sticky;left:0;z-index:1;background:var(--sup);box-shadow:1px 0 0 var(--linha)}
.rola-x tbody tr:hover td:first-child{background:var(--sel)}.rola-dica{display:none;margin:var(--s2) 0 0;font-size:var(--fs-xs);color:var(--tx-3)}
table{border-collapse:collapse;width:100%;font-size:var(--fs-s)}
th{text-align:left;font-weight:500;font-size:var(--fs-xs);color:var(--tx-3);padding:0 var(--s3) var(--s2);border-bottom:1px solid var(--linha-2);
white-space:nowrap;vertical-align:bottom}
td{padding:var(--s3);border-bottom:1px solid var(--linha);vertical-align:middle}
tr:last-child td{border-bottom:0}tbody tr{transition:background-color var(--dur)}tbody tr:hover{background:var(--sel)}
td.n,th.n{text-align:right;white-space:nowrap}td.q{color:var(--tx-2);white-space:nowrap;font-size:var(--fs-xs)}
td.nw{white-space:nowrap}td.r{width:100%}td.r a{color:var(--tx);text-decoration:none}td.r a:hover{color:var(--ac-forte)}
.caixa.justa table th:first-child,.caixa.justa table td:first-child{padding-left:var(--pad)}
.caixa.justa table th:last-child,.caixa.justa table td:last-child{padding-right:var(--pad)}
table.ord th{cursor:pointer;user-select:none}table.ord th:hover{color:var(--tx)}
table.ord th[data-o=a]::after{content:" ↑"}table.ord th[data-o=d]::after{content:" ↓"}
.resumo{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
/* medidor (ui.barra) */
.med{margin:0 0 var(--s3)}.med:last-child{margin-bottom:0}.med .t{display:flex;justify-content:space-between;gap:var(--s2);font-size:var(--fs-s)}
.med .t span:first-child{color:var(--tx-2)}.med .t span:last-child{font-weight:500}
.med .bar{height:var(--med-h);border-radius:var(--pilula);background:var(--sup-2);overflow:hidden;margin-top:var(--r-p)}
.med .bar i{display:block;height:100%;width:var(--p);border-radius:var(--pilula);background:var(--ok);transition:width var(--dur-entra) var(--ease)}
.med.alerta .bar i{background:var(--al)}.med.ruim .bar i{background:var(--ru)}.med.plano .bar i{background:var(--ac)}
.med.alerta .t span:last-child{color:var(--al)}.med.ruim .t span:last-child{color:var(--ru)}
.med .nota{font-size:var(--fs-xs);color:var(--tx-3);margin-top:var(--s1)}
.meds{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,var(--col-min)),1fr));gap:var(--s3) var(--s5)}.meds .med{margin:0}
/* filtro segmentado (links) */
/* pílula nunca quebra linha (quebrada vira oval): rola na horizontal sem barra; .muitos quebra e usa o raio do card */
.filtro{display:inline-flex;flex-wrap:nowrap;align-items:center;gap:2px;padding:3px;border:1px solid var(--linha);border-radius:var(--pilula);
background:var(--sup-2);max-width:100%;overflow-x:auto;scrollbar-width:none;white-space:nowrap}.filtro::-webkit-scrollbar{display:none}
.filtro{scroll-snap-type:x proximity;scroll-padding-inline:var(--s2)}.filtro>*{flex:none;scroll-snap-align:start}
.filtro[data-mais=dir]{mask-image:linear-gradient(to right,var(--bg) calc(100% - var(--s6)),transparent)}
.filtro[data-mais=esq]{mask-image:linear-gradient(to left,var(--bg) calc(100% - var(--s6)),transparent)}
.filtro[data-mais=ambos]{mask-image:linear-gradient(to right,transparent,var(--bg) var(--s6),var(--bg) calc(100% - var(--s6)),transparent)}
.filtro-sel{display:none;align-items:center;gap:var(--s2);max-width:100%;font-size:var(--fs-xs);color:var(--tx-3)}
.filtro-sel select{min-height:var(--toque);min-width:0;flex:1;max-width:100%;padding:0 var(--sel-seta) 0 var(--s3);border:1px solid var(--linha);
border-radius:var(--pilula);background:var(--sup-2);color:var(--tx);font:500 var(--fs-s)/1 var(--f-texto)}.filtro-sel.abas{margin:0 0 var(--s5)}
.filtro>span{padding:0 var(--s2);font-size:var(--fs-xs);color:var(--tx-3)}
.filtro a{display:inline-grid;place-items:center;min-height:var(--toque);min-width:var(--toque);padding:0 var(--s3);border-radius:var(--pilula);
text-decoration:none;font-size:var(--fs-s);color:var(--tx-2);transition:background-color var(--dur),color var(--dur)}
.filtro a:hover{color:var(--tx)}.filtro a[aria-current]{background:var(--ac);color:var(--ac-tx);font-weight:600;view-transition-name:filtro-atual}
.filtros{display:flex;flex-wrap:wrap;gap:var(--s3) var(--s4);margin:0 0 var(--s5)}
.filtro.abas{margin:0 0 var(--s5)}
/* gráfico (ui.grafico): um SVG, eixos em HTML */
.graf{position:relative;margin:var(--s3) 0 0}.graf svg{display:block;width:100%;height:var(--graf);overflow:visible}
.graf.spark svg{height:var(--spark)}.graf.alto svg{height:var(--graf-a)}.graf.baixo svg{height:var(--graf-b)}
.graf .gr{stroke:var(--linha);stroke-width:1;vector-effect:non-scaling-stroke}
.graf .l{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.graf .pt{stroke-width:var(--r-p);stroke-linecap:round;vector-effect:non-scaling-stroke}.graf .a{stroke:none;opacity:.22}
.graf .s1{stroke:var(--g1);fill:var(--g1)}.graf .s2{stroke:var(--g2);fill:var(--g2)}.graf .s3{stroke:var(--g3);fill:var(--g3)}
.graf .s4{stroke:var(--g4);fill:var(--g4)}.graf .s5{stroke:var(--g5);fill:var(--g5)}.graf .s6{stroke:var(--g6);fill:var(--g6)}
.graf .l.s1,.graf .l.s2,.graf .l.s3,.graf .l.s4,.graf .l.s5,.graf .l.s6{fill:none}.graf rect{stroke:none}
.graf .area{position:relative}
.graf .y{position:absolute;left:0;font-size:var(--fs-xxs);line-height:1;color:var(--tx-3);pointer-events:none;padding:2px var(--s1);
border-radius:var(--r-p);background:color-mix(in srgb,var(--sup) 80%,transparent)}.graf .y.topo-y{top:0}
.graf .y.base-y{bottom:var(--s1)}
.graf .x{display:flex;justify-content:space-between;font-size:var(--fs-xxs);color:var(--tx-3);margin-top:var(--r-p)}
.leg{display:flex;flex-wrap:wrap;gap:var(--s1) var(--s4);font-size:var(--fs-xs);color:var(--tx-2);margin:var(--s3) 0 0}
.leg i{display:inline-block;width:var(--ponto-g);height:var(--ponto-g);border-radius:3px;margin-right:var(--r-p);vertical-align:-1px}
.leg .s1{background:var(--g1)}.leg .s2{background:var(--g2)}.leg .s3{background:var(--g3)}.leg .s4{background:var(--g4)}
.leg .s5{background:var(--g5)}.leg .s6{background:var(--g6)}
/* lista chave → valor, markdown, aviso */
.kv{display:grid;grid-template-columns:max-content 1fr;gap:var(--s1) var(--s3);margin:0}.kv dt{color:var(--tx-2)}
.kv dd{margin:0}
.md{max-width:var(--texto-w)}.md h1,.md h2,.md h3{font-size:var(--h2);margin:var(--s4) 0 var(--s2);view-transition-name:none}
.md table{width:auto;display:block;overflow-x:auto}.md pre,pre.raw{white-space:pre-wrap;font:var(--fs-xs)/1.6 var(--f-mono)}
.md code{font-family:var(--f-mono);font-size:var(--fs-xs)}
.md .cod{position:relative}.md .cod pre{background:var(--sup-2);border:1px solid var(--linha);border-radius:var(--r-s);padding:var(--s3) var(--s7) var(--s3) var(--s3);overflow-x:auto}
.md .cod .copiar{position:absolute;top:var(--s1);right:var(--s1);min-height:0;padding:var(--s1) var(--s2);font-size:var(--fs-xs)}
.pilha>*+*{margin-top:var(--s3)}
.aviso{position:fixed;left:50%;bottom:var(--s5);z-index:30;transform:translateX(-50%);max-width:min(var(--aviso-w),calc(100vw - 2 * var(--s3)));
padding:var(--s3) var(--s4);border-radius:var(--r-s);background:var(--tx);color:var(--bg);font-size:var(--fs-s);font-weight:500;
box-shadow:var(--sombra-aviso);animation:aviso-entra var(--dur-entra) var(--ease) both;view-transition-name:aviso}
.aviso.erro{background:var(--ru);color:var(--sup)}
@keyframes aviso-entra{from{opacity:0;transform:translate(-50%,var(--s3)) scale(.98)}}
.passos{list-style:none;margin:0 0 var(--s3);padding:0;font:var(--fs-xs)/1.5 var(--f-mono);color:var(--tx-3)}
.passos li{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;padding:3px 0 3px var(--s2);
border-left:2px solid var(--linha)}.passos li.atual{-webkit-line-clamp:4;color:var(--tx);border-left-color:var(--ac)}
/* entrada: sobe 10 px com desfoque, escalonada por .i1..i12 */
.entra,.kpis>.kpi,.bloco,.lista>.cartao{animation:entra-sobe var(--dur-entra) var(--ease) both}
@keyframes entra-sobe{from{opacity:0;transform:translateY(var(--sobe));filter:blur(var(--desfoque))}to{opacity:1;transform:none;filter:none}}
.vivo .bloco,.vivo .cartao,.vivo .kpi,.vivo .entra{animation:none}
/* entre páginas: o menu fica, o resto troca */
@view-transition{navigation:auto}
::view-transition-group(*){animation-duration:var(--dur-entra);animation-timing-function:var(--ease)}
::view-transition-old(root){animation:vt-sai calc(var(--dur-entra) * .5) var(--ease) both}
::view-transition-new(root){animation:vt-entra var(--dur-entra) var(--ease) both}
@keyframes vt-sai{to{opacity:0}}@keyframes vt-entra{from{opacity:0;transform:translateY(var(--sobe-vt))}}
/* tela: propostas */
.resumo-2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pag{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:var(--s3);margin-top:var(--s4);font-size:var(--fs-s);color:var(--tx-2)}
.pag .bt[aria-disabled=true]{opacity:.4;pointer-events:none}
.barra-filtro{margin-bottom:var(--s4)}
/* tela: agora e agentes (cartão de conta único) */
.conta{display:flex;flex-direction:column;gap:var(--s3)}.grade>.conta{margin:0}
.conta>header{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);flex-wrap:wrap}
.conta h3{margin:0;font-size:var(--fs);font-weight:600}.conta .faixa{color:var(--tx-3);font-weight:400}
.conta .faz{color:var(--tx-2);font-size:var(--fs-s)}.conta .faz b{color:var(--tx);font-weight:500}
.conta .faz .mu{display:block;margin-top:var(--s1)}.conta .faz p{margin:0 0 var(--s1)}
.conta .passos{margin:0}.conta .cotas{margin-top:auto;display:grid;gap:var(--s3)}.conta .cotas .med{margin:0}
.conta.ativa{border-color:color-mix(in srgb,var(--ac) 35%,var(--linha))}
/* tela: seo */
.lgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--col-min),1fr));gap:var(--s5) var(--s4)}.lg{margin:0}
.lg-m{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--s1);aspect-ratio:1;max-width:var(--col-min)}
.lg-m.c3{grid-template-columns:repeat(3,minmax(0,1fr))}.lg-m.c7{grid-template-columns:repeat(7,minmax(0,1fr))}
.lg-m.c9{grid-template-columns:repeat(9,minmax(0,1fr))}
.lg-m span{display:grid;place-items:center;border-radius:var(--r-p);font:600 var(--fs-xs)/1 var(--f-texto)}
.lg figcaption{display:grid;gap:var(--s1);margin-top:var(--s2);font-size:var(--fs-s)}.lg figcaption .mu{font-size:var(--fs-xs)}
/* texto sobre cor de função: a superfície do tema (carvão no escuro, creme no claro) passa 4,5:1 nos dois */
.g-1{background:var(--ok);color:var(--sup)}.g-2{background:var(--al);color:var(--sup)}.g-3{background:var(--ru);color:var(--sup)}
.g-fora{background:var(--sup-2);color:var(--tx-3)}.g-vazio{color:var(--tx-3)}
.leg i.g-1{background:var(--ok)}.leg i.g-2{background:var(--al)}.leg i.g-3{background:var(--ru)}.leg i.g-fora{background:var(--sup-2)}
td.hc{text-align:center;font-weight:600;min-width:var(--toque)}table.hm th:not(:first-child){text-align:center}
td.hc.g-1{background:color-mix(in srgb,var(--ok) 13%,transparent);color:var(--ok)}
td.hc.g-2{background:color-mix(in srgb,var(--al) 13%,transparent);color:var(--al)}
td.hc.g-3{background:color-mix(in srgb,var(--ru) 13%,transparent);color:var(--ru)}
td.hc.g-fora,td.hc.g-vazio{background:none;color:var(--tx-3)}
.filtro.muitos{flex-wrap:wrap;border-radius:var(--r)}.seo td .mu{font-size:var(--fs-xs)}.seo table:not(.hm) td:first-child{min-width:var(--col-min)}.seo-mais{margin:var(--s3) 0 0}.seo-nota{margin:0 0 var(--s3)}
td.mk-acao{min-width:var(--col-min);max-width:var(--texto-w);white-space:normal}.mk-exec{margin:0 0 var(--s2)}
.mk-pos{display:block;color:var(--tx-3)}.mk-pos polyline{fill:none;stroke:var(--ac);stroke-width:1.5}.mk-pos .med{fill:var(--ac)}
.mk-pos .fora{fill:none;stroke:currentColor;stroke-width:1;opacity:.6}.mk-pos .guia{stroke:currentColor;stroke-dasharray:2 2;opacity:.35}
.mk-pos text{font-size:var(--fs-xxs);fill:currentColor}.mk-pos .eixo{text-anchor:end;opacity:.7}.mk-pos .atual{font-weight:600;fill:var(--tx)}
.rel-tab td.q{white-space:normal}
table.sites .graf.spark{min-width:var(--graf-b)}
/* tela: bi (gráficos do ECharts: o JS acende .on; sem JS o quadro fica escondido) */
.ec{display:none;position:relative;margin:var(--s3) 0 0}.ec.on{display:block;height:var(--graf)}
.ec.baixo.on{height:var(--graf-b)}.ec.alto.on{height:var(--graf-a)}.kpi>.ec{margin-top:auto}
.bi .kpi .v{white-space:normal;overflow:visible}
.bi .ec svg{display:block;width:100%;height:100%;margin:0}
.bi .ec.alto{margin-bottom:var(--s4)}.bi .leg{margin:var(--s3) 0 var(--s5)}
.bi-neg .kpi .v{font-size:var(--h3)}
.cor{display:inline-block;width:var(--ponto-g);height:var(--ponto-g);border-radius:3px;margin-right:var(--s2)}
.cor.s1{background:var(--g1)}.cor.s2{background:var(--g2)}.cor.s3{background:var(--g3)}.cor.s4{background:var(--g4)}
.cor.s5{background:var(--g5)}.cor.s6{background:var(--g6)}
.leg i.tr{width:var(--s4);height:0;border-radius:0;background:none;border-top:2px solid var(--g1);vertical-align:middle;margin-right:var(--s2)}
.leg i.tr.comp{border-top:2px dashed var(--tx-3)}.leg i.tr.parcial{border-top:2px dotted var(--g1)}
.leg i.tr.alg{width:var(--s3);height:var(--s3);border:0;border-radius:2px;background:color-mix(in srgb,var(--al) 22%,transparent);
box-shadow:inset 2px 0 0 color-mix(in srgb,var(--al) 70%,transparent)}
/* carregando: o loading aprovado (ampulheta parada, grãos subindo), sem zoom nem giro; no escuro, sobre a placa creme */
.carregando{display:grid;place-items:center;height:100%}
.carregando img{display:block;height:var(--carrega);width:auto;padding:var(--s2);border-radius:var(--r-s);background:var(--carrega-fundo)}
.ec.baixo .carregando img{height:var(--carrega-p);padding:var(--s1)}
.so-leitor{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
/* página pública (relatório aberto, entrada): sem menu, coluna única */
.app.publica{display:block}.publica main{margin:0 auto}
/* entrada (corpo do 401): a ampulheta grande e o nome, como na prancha */
.entrar{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:var(--s3);min-height:calc(100dvh - 2 * var(--fim) - var(--s5));text-align:center}
.entrar .simbolo{height:var(--simbolo-g);margin:0 0 var(--s4)}
.entrar h1{font-size:var(--nome-g);letter-spacing:var(--marca-esp);text-transform:uppercase;margin-right:calc(-1 * var(--marca-esp))}
.entrar p{margin:0}.entrar .bt{margin-top:var(--s3);min-width:calc(3 * var(--s6))}
.entrar .form{flex-direction:column;align-items:stretch;width:100%;max-width:var(--aviso-w);text-align:left}
.entrar .form label{display:flex;flex-direction:column;gap:var(--r-p);font-size:var(--fs-xs);color:var(--tx-2)}
.entrar .form button{margin-top:var(--s2)}.rodape .sair{color:var(--tx-3)}
/* celular */
.embaixo{display:none}
@media (max-width:1100px){.grade.n4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:860px){
.app{display:block}nav.lado{display:none}.fim-tema{display:flex}.marca.cel{display:flex;margin:0 0 var(--s4);padding:0}main{padding:var(--s4) var(--s4) calc(var(--fim) + var(--s2) + env(safe-area-inset-bottom))}
.topo{margin-bottom:var(--s5)}.kpis{grid-template-columns:1fr 1fr;gap:var(--s2)}.kpi{padding:var(--s3)}
.cerebro-mini{height:max(var(--cerebro-h),min(34rem,75vh))}
.grade.n2,.grade.n3,.grade.n4,.dupla{grid-template-columns:minmax(0,1fr)}
.item{grid-template-columns:minmax(0,1fr)}.item .acoes .bt,.item>form button{flex:1;min-height:var(--toque)}
.some-cel,table .some-cel{display:none}td{padding:var(--s2)}
.grupo-topo{display:flex;margin:0 0 var(--s4);overflow-x:auto}
.embaixo{position:fixed;left:0;right:0;bottom:0;z-index:10;display:grid;grid-template-columns:repeat(4,1fr);
padding:var(--r-p) var(--r-p) calc(var(--r-p) + env(safe-area-inset-bottom));background:color-mix(in srgb,var(--bg) 88%,transparent);
backdrop-filter:blur(var(--vidro));-webkit-backdrop-filter:blur(var(--vidro));border-top:1px solid var(--linha);view-transition-name:embaixo}
.embaixo a{position:relative;display:grid;justify-items:center;gap:2px;min-height:var(--toque);padding:var(--r-p) 0;text-decoration:none;
color:var(--tx-3);font-size:var(--fs-xxs);border-radius:var(--r-s)}.embaixo a b{font-size:var(--fs-s);line-height:1}
.embaixo a[aria-current]{color:var(--ac-forte)}.embaixo .n{position:absolute;top:2px;left:calc(50% + var(--s2));min-width:var(--s4);padding:0 var(--s1);
border-radius:var(--pilula);background:var(--ac);color:var(--ac-tx);font:600 var(--fs-xxs)/var(--s4) var(--f-texto)}
}
@media (min-width:861px){.grupo-topo{display:none}}
/* celular estreito: cada linha de tabela vira um cartão (título em destaque, rótulo em cima de cada valor); a tabela que
   precisa ficar tabela (.rola-x) avisa que desliza. Filtro com mais de 3 opções vira select nativo (só com JS: sem ele, as pílulas) */
@media (max-width:640px){
.cartoes table,.cartoes tbody,.cartoes td{display:block}.cartoes thead,.cartoes tr:has(th){display:none}
.cartoes tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s2) var(--s3);padding:var(--s3) var(--pad);border-bottom:1px solid var(--linha)}
.caixa:not(.justa) .cartoes tr{padding-left:0;padding-right:0}.caixa:not(.justa) .cartoes tr:first-child{padding-top:0}
.cartoes tr:last-child{border-bottom:0}.cartoes td,.cartoes td.n,.cartoes td.q,.cartoes td.nw{border:0;padding:0;text-align:left;white-space:normal;
min-width:0;max-width:none;width:auto}.cartoes td:empty,.cartoes td.some-cel{display:none}
.cartoes td.c-tit{grid-column:1/-1;order:-1;font-size:var(--h2);font-weight:600}.cartoes td.c-tit .mu{font-weight:400}
.cartoes td.c-larga{grid-column:1/-1}.cartoes td.c-grande{font-size:var(--kpi-m);font-weight:600;line-height:1.1}
.cartoes td[data-rotulo]:not(.c-tit)::before{content:attr(data-rotulo);display:block;margin:0 0 2px;font:400 var(--fs-xxs)/1.4 var(--f-texto);color:var(--tx-3)}
.rola-x+.rola-dica{display:block}.selo{white-space:normal;height:auto;min-height:var(--s4);padding:2px var(--s2);line-height:1.3}
.js .filtro.tem-sel{display:none}.js .filtro-sel{display:flex}}
/* movimento reduzido: nada desliza, nada pisca; o conteúdo aparece pronto */
@media (prefers-reduced-motion:reduce){@view-transition{navigation:none}
*,::before,::after{animation:none!important;transition:none!important}
/* o SVG do loading dentro de <img> não recebe a preferência (Chromium): troca pela ampulheta parada, mesma geometria */
.carregando img{content:url(/marca/kronos.svg)}}
"""


def _classes_geradas():
    """Atraso escalonado (.i1..i12) e largura de barra (.p0..p100) sem style= inline."""
    atrasos = "".join(f".i{i}{{animation-delay:calc({i} * var(--passo))}}" for i in range(1, 13))
    larguras = "".join(f".p{i}{{--p:{i}%}}" for i in range(101))
    return atrasos + larguras


# Tema: roda no <head> antes de pintar (sem valor gravado = escuro). O botão .tema-bt troca, grava e recarrega
# (o ECharts do BI lê as cores dos tokens na carga).
TEMA_HEAD = ("<script>document.documentElement.classList.add('js');"
             "try{if(localStorage.tema=='claro')document.documentElement.dataset.tema='claro'}catch(e){}</script>")
TEMA_BT = '<button class="tema-bt" type="button" aria-pressed="false">Tema claro</button>'  # rótulo fixo; o estado é o aria-pressed

# A ampulheta inline (hub/marca/kronos.svg, a única fonte da geometria): traço em currentColor (a cor do texto, funciona nos
# dois temas) e areia em latão (.areia).
_KRONOS = (Path(__file__).with_name("marca") / "kronos.svg").read_text()


def simbolo(classe="simbolo"):
    s = re.split(r"</title>", _KRONOS, maxsplit=1)[1]
    s = s.replace('fill="#171717"', 'fill="currentColor"').replace('fill="#B69A64"', 'class="areia"')
    return f'<svg class="{classe}" viewBox="0 0 432 514" aria-hidden="true" focusable="false">{s.strip()}'


# O pescoço da ampulheta: duas curvas espelhadas que se estreitam e o grão de latão no meio.
PESCOCO_G = ('<svg class="pescoco-g" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
             '<path d="M7 3.5C7 9 10 10.2 10 12S7 15 7 20.5M17 3.5C17 9 14 10.2 14 12S17 15 17 20.5"/>'
             '<circle cx="12" cy="12" r="1.3"/></svg>')
PESCOCO = f'<div class="pescoco" role="separator">{PESCOCO_G}</div>'

JS = """<script>
(()=>{const claro=document.documentElement.dataset.tema=='claro';
document.querySelectorAll('.tema-bt').forEach(b=>{b.setAttribute('aria-pressed',claro);
b.onclick=()=>{try{claro?localStorage.removeItem('tema'):localStorage.tema='claro'}catch(e){}location.reload()}})})();
(()=>{const calmo=matchMedia('(prefers-reduced-motion: reduce)').matches;
const troca=fn=>document.startViewTransition&&!calmo?document.startViewTransition(fn):fn();window.uiTroca=troca;
/* números contam até o valor (primeira carga, sem movimento reduzido) */
if(!calmo)document.querySelectorAll('.kpi .v[data-v]').forEach(el=>{const fim=+el.dataset.v,txt=el.textContent;if(!fim||!isFinite(fim))return;
const t0=performance.now(),d=700;const f=t=>{const p=Math.min(1,(t-t0)/d),k=1-Math.pow(1-p,4);
el.textContent=p<1?Math.round(fim*k).toLocaleString('pt-BR'):txt;if(p<1)requestAnimationFrame(f)};el.textContent='0';requestAnimationFrame(f)});
/* tabela ordenável: data-v manda no valor */
document.querySelectorAll('table.ord th').forEach(th=>{const t=th.closest('table');th.tabIndex=0;
const f=()=>{const i=th.cellIndex,s=th.dataset.o=th.dataset.o=='a'?'d':'a',v=r=>{const c=r.cells[i];if(!c)return'';const x=c.dataset.v??c.textContent,
n=parseFloat(x);return isNaN(n)?x.toLowerCase():n};t.querySelectorAll('th').forEach(h=>h!=th&&delete h.dataset.o);
const corpo=t.tBodies[0];troca(()=>[...corpo.rows].filter(r=>!r.querySelector('th')).sort((a,b)=>{const x=v(a),y=v(b);return(x>y?1:x<y?-1:0)*(s=='a'?1:-1)})
.forEach(r=>corpo.appendChild(r)))};th.onclick=f;th.onkeydown=e=>{if(e.key=='Enter')f()}});
/* enviar: trava os botões do formulário */
document.addEventListener('submit',ev=>{const f=ev.target,b=ev.submitter;setTimeout(()=>{f.querySelectorAll('button').forEach(x=>x.disabled=true);
if(b)b.textContent='enviando…'},0)});
/* filtro que rola: a pílula ativa vem para a vista (só a faixa rola, a página não) */
document.querySelectorAll('.filtro [aria-current]').forEach(a=>{const f=a.parentElement;if(f.scrollWidth>f.clientWidth)
f.scrollLeft+=a.getBoundingClientRect().left-f.getBoundingClientRect().left-(f.clientWidth-a.offsetWidth)/2});
/* fade (pílulas) e sombra (tabela .rola-x) na borda que ainda tem conteúdo escondido: esquerda, direita ou as duas */
const borda=f=>{const m=f.scrollWidth-f.clientWidth;if(m<2){delete f.dataset.mais;return}const esq=f.scrollLeft>1,dir=f.scrollLeft<m-1;
f.dataset.mais=esq&&dir?'ambos':esq?'esq':'dir'};document.querySelectorAll('.filtro,.rola-x').forEach(f=>{borda(f);f.addEventListener('scroll',()=>borda(f),{passive:true})});
addEventListener('resize',()=>document.querySelectorAll('.filtro,.rola-x').forEach(borda));
/* select do filtro no celular: trocar a opção abre a URL dela (só URL do próprio painel, relativa ou não) */
document.querySelectorAll('select[data-vai]').forEach(s=>s.onchange=()=>{const u=new URL(s.value,location.href);if(u.origin===location.origin)location.href=u});
document.querySelectorAll('.aviso').forEach(a=>setTimeout(()=>troca(()=>a.remove()),6000));
/* <dialog>: data-abre="id" abre, .fecha fecha */
document.addEventListener('click',ev=>{const a=ev.target.closest('[data-abre]');if(a){document.getElementById(a.dataset.abre)?.showModal();return}
const f=ev.target.closest('dialog .fecha');if(f)f.closest('dialog').close()});
/* miolo que se renova sozinho: troca com View Transition, sem piscar */
const recarrega=()=>{if(document.hidden)return;document.querySelectorAll('[data-recarrega]').forEach(el=>{
if(el.dataset.busy||el.querySelector('details[open],input:focus,textarea:focus'))return;el.dataset.busy=1;
fetch(el.dataset.recarrega,{credentials:'same-origin'}).then(r=>r.ok?r.text():Promise.reject())
.then(h=>troca(()=>{el.classList.add('vivo');el.innerHTML=h})).catch(()=>{}).finally(()=>delete el.dataset.busy)})};
setInterval(recarrega,15000);document.addEventListener('visibilitychange',recarrega)})()</script>"""

JS_MD = """<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>document.querySelectorAll('textarea.md-src').forEach(t=>{const d=document.createElement('div');
d.className='md';d.innerHTML=DOMPurify.sanitize(marked.parse(t.value));t.replaceWith(d);
d.querySelectorAll('pre').forEach(p=>{const w=document.createElement('div'),b=document.createElement('button');w.className='cod';
b.type='button';b.className='bt copiar';b.textContent='Copiar';b.onclick=()=>navigator.clipboard.writeText(p.innerText)
.then(()=>{b.textContent='Copiado';setTimeout(()=>{b.textContent='Copiar'},1500)});p.replaceWith(w);w.append(p,b)})})</script>"""


def _speculation():
    """Prefetch das telas do menu quando o dedo/mouse para em cima. Nunca /acao/* (POST) nem /r/* (público)."""
    regras = {"prefetch": [{"where": {"and": [{"selector_matches": "nav.lado a, .embaixo a, .grupo-topo a"},
                                              {"not": {"href_matches": "/acao/*"}}, {"not": {"href_matches": "/r/*"}}]},
                            "eagerness": "moderate"}]}
    return f'<script type="speculationrules">{json.dumps(regras)}</script>'


# ---------- formatos únicos ----------

def num(v, *, moeda=None, casas=0, pct=False):
    """pt-BR: 1.397 · R$ 1.397,00 · US$ 80,39 · 12,5%. None → —."""
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if moeda and not casas:
        casas = 2
    s = f"{v:,.{casas}f}".translate(str.maketrans(",.", ".,"))
    if s.startswith("-"):
        s = "−" + s[1:]
    if pct:
        s += "%"
    return f"{moeda} {s}" if moeda else s


def _dt(iso):
    if isinstance(iso, dt.datetime):
        d = iso
    else:
        try:
            d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(BRT)


def quando(iso):
    """'14/10 14:17' em Brasília; texto vazio se a data não vier."""
    d = _dt(iso)
    return f"{d:%d/%m %H:%M}" if d else ""


def ha(iso=None, *, minutos=None, agora=None):
    """'agora', 'há 18 min', 'há 3 h', 'há 2 dias'."""
    if minutos is None:
        d = _dt(iso)
        if not d:
            return ""
        minutos = ((agora or dt.datetime.now(dt.timezone.utc)) - d).total_seconds() / 60
    m = max(0, int(minutos))
    if m < 1:
        return "agora"
    if m < 60:
        return f"há {m} min"
    if m < 48 * 60:
        return f"há {m // 60} h"
    return f"há {m // 1440} dias"


# ---------- componentes ----------

def selo(texto, tom="neutro", *, mono=False):
    return f'<span class="selo {tom if tom in TONS else "neutro"}{" mono" if mono else ""}">{e(str(texto))}</span>'


def vazio(forte, resto=""):
    """Estado vazio que diz o que acontece: 'Nada esperando você.' + explicação."""
    return f'<p class="vazio">{PESCOCO_G}<span><b>{e(forte)}</b>{" " + e(resto) if resto else ""}</span></p>'


def carregando(rotulo="Carregando"):
    """Loading aprovado da marca (hub/marca/kronos-loading.svg, 432:514, com movimento reduzido fica parado) com status
    acessível. O BI põe dentro de cada .ec até o ECharts desenhar."""
    return (f'<div class="carregando" role="status"><img src="/marca/kronos-loading.svg" width="432" height="514" alt="">'
            f'<span class="so-leitor">{e(rotulo)}</span></div>')


def erro(fonte, resto=""):
    """Fonte fora do ar (âmbar, nunca 500): 'Linear indisponível agora.'"""
    return f'<p class="vazio fora" role="status"><span><b>{e(fonte)}</b>{" " + e(resto) if resto else ""}</span></p>'


def card(titulo, corpo, *, conta=None, direita="", id_=None, caixa=True, justa=False, classe="", i=None):
    """Bloco com título (h2), contador opcional, ação à direita e corpo numa caixa (caixa=False: corpo solto,
    para listas de cartões). justa: caixa sem respiro interno (tabela encostada na borda)."""
    cont = f'<span class="cont{" quente" if conta else ""}">{e(str(conta))}</span>' if conta is not None else ""
    dir_ = f'<span class="dir">{direita}</span>' if direita else ""
    cab = f'<header><h2>{e(str(titulo))}</h2>{cont}{dir_}</header>' if titulo or direita else ""
    miolo = f'<div class="caixa{" justa" if justa else ""}">{corpo}</div>' if caixa else corpo
    cls = " ".join(x for x in ("bloco", classe, f"i{i}" if i else "") if x)
    return f'<section class="{cls}"{f" id={e(id_)}" if id_ else ""}>{cab}{miolo}</section>'


def variacao(delta, menor_melhor=False, comp="", motivo=None):
    """Seta, sinal e cor (nunca só cor); sem delta, o motivo em cinza ou nada."""
    if delta is None:
        return f'<div class="d neutro">{e(motivo)}</div>' if motivo else ""
    delta = round(float(delta), 1)
    tom = "neutro" if not delta else "ok" if (delta > 0) != menor_melhor else "ruim"
    seta = "▲" if delta > 0 else "▼" if delta < 0 else "="
    sinal = "+" if delta > 0 else "−" if delta < 0 else ""
    return (f'<div class="d {tom}">{seta} {sinal}{num(abs(delta), casas=1)}%'
            + (f' <span class="mu">{e(comp)}</span>' if comp else "") + "</div>")


def kpi(rotulo, valor, *, nota="", delta=None, menor_melhor=False, serie=None, quente=False, tom=None, comp="", motivo=None,
        href=None, contar=None, rotulo_html=False):
    """Número grande com rótulo. nota: HTML (já escapado). contar: número cru para a animação que conta até o valor.
    tom: 'alerta'/'ruim' pinta o número; quente: latão de destaque."""
    cls = "v" + (" quente" if quente else f" {tom}" if tom in ("alerta", "ruim", "ok") else "")
    dv = f' data-v="{e(str(contar))}"' if contar is not None else ""
    rot = rotulo if rotulo_html else e(str(rotulo))
    miolo = (f'<span class="r">{rot}</span><span class="{cls}"{dv}>{e(str(valor))}</span>'
             f'{variacao(delta, menor_melhor, comp, motivo)}{f"<div class=nota>{nota}</div>" if nota else ""}'
             f'{grafico([serie], tipo="spark") if serie else ""}')
    return f'<a class="kpi" href="{e(href)}">{miolo}</a>' if href else f'<div class="kpi">{miolo}</div>'


def kpis(*itens, miudos=False):
    return f'<div class="kpis{" miudos" if miudos else ""}">{"".join(itens)}</div>'


def td(texto, v=None, *, n=False, classe="", rotulo=None, html_=False):
    """Célula: texto escapado (html_=True: já é HTML). v: valor cru para ordenar (data-v). n: número à direita."""
    cls = " ".join(x for x in ("n" if n else "", classe) if x)
    attrs = (f' class="{cls}"' if cls else "") + (f' data-v="{e(str(v))}"' if v is not None else "") + \
            (f' data-rotulo="{e(rotulo)}"' if rotulo else "")
    return f"<td{attrs}>{texto if html_ else e(str(texto))}</td>"


def _td_mais(x, classes=(), rotulo=None):
    """Acrescenta classes (e o rótulo do cartão, se faltar) no primeiro <td> de uma célula já pronta."""
    abre = x[:x.index(">") + 1]
    resto = x[len(abre):]
    if classes:
        if ' class="' in abre:
            abre = abre.replace(' class="', f' class="{" ".join(classes)} ', 1)
        else:
            abre = abre[:-1] + f' class="{" ".join(classes)}">'
    if rotulo and "data-rotulo=" not in abre:
        abre = abre[:-1] + f' data-rotulo="{e(rotulo)}">'
    return abre + resto


def tabela(cab, linhas, *, num=(), ord_=False, celular="cartoes", classe="", titulo=0, grande=(), largas=()):
    """cab: rótulos (texto). linhas: listas de células já em HTML (<td> de td()) ou texto, que vira <td> escapado.
    num: índices numéricos (à direita).
    celular (abaixo de 640 px): 'cartoes' (padrão; 'pilha' é o mesmo): cada linha vira um cartão, a célula
    `titulo` em destaque no topo, as `grande` com número grande, as `largas` na largura toda e cada valor com o rótulo
    da coluna em cima. 'some:1,2': cartões sem essas colunas (que também somem da tabela até 860 px). 'rola': fica tabela
    (muitas colunas numéricas): 1ª coluna fixa, rolagem visível e sombra na borda, com o aviso de que desliza."""
    some = set()
    if celular.startswith("some:"):
        some = {int(x) for x in celular[5:].split(",") if x.strip().isdigit()}
    rola = celular == "rola"
    grande, largas = set(grande if not isinstance(grande, int) else (grande,)), set(largas)

    def cel(i, x):
        x = str(x)
        if not x.startswith("<td"):
            x = td(x, n=i in num)
        cls = [c for c, sim in (("some-cel", i in some), ("c-tit", i == titulo), ("c-grande", i in grande),
                                ("c-larga", i in largas)) if sim]
        return _td_mais(x, cls, None if rola or i >= len(cab) else str(cab[i]))

    def classe_th(i):
        c = " ".join(x for x in ("n" if i in num else "", "some-cel" if i in some else "") if x)
        return f' class="{c}"' if c else ""

    th = "".join(f"<th{classe_th(i)}>{e(str(c))}</th>" for i, c in enumerate(cab))
    corpo = "".join("<tr>" + "".join(cel(i, x) for i, x in enumerate(linha)) + "</tr>" for linha in linhas)
    tcls = " ".join(x for x in ("ord" if ord_ else "", classe) if x)
    tab = f'<table{f" class=\"{tcls}\"" if tcls else ""}><thead><tr>{th}</tr></thead><tbody>{corpo}</tbody></table>'
    if rola:
        return f'<div class="rola rola-x" tabindex="0">{tab}</div><p class="rola-dica">Deslize a tabela para o lado →</p>'
    return f'<div class="rola cartoes">{tab}</div>'


def barra(rotulo, valor, total=100, *, limites=(85, 95), plano=False, nota="", texto=None):
    """Medidor de uso: limiar único (alerta a partir de limites[0], ruim a partir de limites[1]). plano: latão de
    destaque (meta, não alarme). texto: o que aparece à direita (padrão: '37%')."""
    try:
        pct = 0.0 if not total else max(0.0, min(100.0, float(valor) / float(total) * 100))
    except (TypeError, ValueError):
        pct = 0.0
    tom = "plano" if plano else "ruim" if pct >= limites[1] else "alerta" if pct >= limites[0] else "ok"
    direita = e(texto) if texto is not None else f"{pct:.0f}%"
    return (f'<div class="med {tom}"><div class="t"><span>{e(str(rotulo))}</span><span>{direita}</span></div>'
            f'<div class="bar" role="meter" aria-label="{e(str(rotulo))}" aria-valuenow="{pct:.0f}" aria-valuemin="0" '
            f'aria-valuemax="100"><i class="p{round(pct)}"></i></div>{f"<div class=nota>{nota}</div>" if nota else ""}</div>')


def _escala(series, minimo=None, maximo=None):
    vals = [float(x) for s in series for x in (s or []) if x is not None]
    if not vals:
        return None
    lo = min(vals) if minimo is None else minimo
    hi = max(vals) if maximo is None else maximo
    return lo, (hi if hi > lo else lo + 1)


def grafico(series, rotulos=(), *, tipo="linha", unidade="", minimo=None, maximo=None, legenda=(), rotulo="", altura=""):
    """Um SVG só para tudo: 'spark' (sem eixos), 'linha', 'area' e 'barra' (série 1). series: listas de números
    (None é lacuna). Eixos em HTML: máximo e mínimo à esquerda, primeiro/meio/último rótulo embaixo.
    altura: '' | 'alto' | 'baixo'. legenda: nomes das séries."""
    series = [list(s or []) for s in series if s]
    if tipo in ("linha", "area", "spark") and minimo is None and tipo != "spark":
        minimo = 0
    esc = _escala(series, minimo, maximo)
    if not esc:
        return ""
    lo, hi = esc
    w, h = 300, 100
    n = max(len(s) for s in series)
    grade = "" if tipo == "spark" else "".join(f'<line class="gr" x1="0" x2="{w}" y1="{y}" y2="{y}"/>' for y in (0, h / 2, h))
    desenho = ""
    if tipo == "barra":
        s = series[0]
        larg = w / max(n, 1)
        for i, x in enumerate(s):
            if x is None:
                continue
            alt = (float(x) - lo) / (hi - lo) * h
            desenho += (f'<rect class="s1" x="{i * larg + larg * .15:.2f}" y="{h - alt:.2f}" width="{larg * .7:.2f}" '
                        f'height="{max(alt, .5):.2f}" rx="1"/>')
    else:
        for k, s in enumerate(series):
            cor = f"s{k % 6 + 1}"
            pts = [None if x is None else (w * (i / max(n - 1, 1)), h - (float(x) - lo) / (hi - lo) * h) for i, x in enumerate(s)]
            trechos, atual = [], []
            for p in pts + [None]:
                if p is None:
                    trechos += [atual] if atual else []
                    atual = []
                else:
                    atual.append(p)
            for t in trechos:
                if len(t) == 1:
                    x, y = t[0]
                    desenho += f'<line class="pt {cor}" x1="{x - .01:.2f}" y1="{y:.2f}" x2="{x:.2f}" y2="{y:.2f}"/>'
                    continue
                cam = " ".join(f"{x:.2f},{y:.2f}" for x, y in t)
                if tipo == "area" and k == 0:
                    desenho += f'<polygon class="a {cor}" points="{t[0][0]:.2f},{h} {cam} {t[-1][0]:.2f},{h}"/>'
                desenho += f'<polyline class="l {cor}" points="{cam}"/>'
            fim = next((p for p in reversed(pts) if p), None)
            if fim and tipo == "spark":
                desenho += f'<line class="pt {cor}" x1="{fim[0] - .01:.2f}" y1="{fim[1]:.2f}" x2="{fim[0]:.2f}" y2="{fim[1]:.2f}"/>'
    rot = rotulo or ("tendência" if tipo == "spark" else "gráfico")
    svg = (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="{e(rot)}">{grade}{desenho}</svg>')
    if tipo == "spark":
        return f'<div class="graf spark">{svg}</div>'
    fmt = (lambda v: f"{num(v, casas=0 if abs(v) >= 10 or v == int(v) else 1)}{unidade}")
    eixo_y = f'<span class="y topo-y">{e(fmt(hi))}</span><span class="y base-y">{e(fmt(lo))}</span>'
    rs = list(rotulos)
    eixo_x = ""
    if rs:
        marcas = [rs[0], rs[len(rs) // 2], rs[-1]] if len(rs) > 2 else rs
        eixo_x = '<div class="x">' + "".join(f"<span>{e(str(r))}</span>" for r in marcas) + "</div>"
    leg = ('<div class="leg">' + "".join(f'<span><i class="s{k % 6 + 1}"></i>{e(str(nm))}</span>' for k, nm in enumerate(legenda))
           + "</div>") if legenda else ""
    return f'<div class="graf{f" {altura}" if altura else ""}"><div class="area">{eixo_y}{svg}</div>{eixo_x}{leg}</div>'


def _q(url, nome, valor):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    q[nome] = valor
    return urllib.parse.urlunsplit(("", "", p.path, urllib.parse.urlencode(q), ""))


def filtro(nome, opcoes, atual, *, rotulo="", base="", url=None, abas=False, classe="", select=None):
    """Seletor segmentado feito de links (funciona sem JS). opcoes: {valor: rótulo} ou [(valor, rótulo)].
    base: URL atual (a query existente é mantida e só `nome` muda). url(valor): monta o href (vence base).
    No celular as pílulas rolam com snap e fade na borda; com mais de 3 opções (select=None) ou select=True, abaixo de
    640 px vira um <select> nativo que navega ao trocar (sem JS ficam as pílulas)."""
    itens = [(k, t, url(k) if url else _q(base, nome, k)) for k, t in (opcoes.items() if isinstance(opcoes, dict) else opcoes)]
    links = "".join(f'<a href="{e(h)}"{" aria-current=true" if str(k) == str(atual) else ""}>{e(str(t))}</a>' for k, t, h in itens)
    com_sel = len(itens) > 3 if select is None else select
    cls = "".join((" abas" if abas else "", f" {classe}" if classe else "", " tem-sel" if com_sel else ""))
    pilulas = (f'<div class="filtro{cls}" role="group" aria-label="{e(rotulo or nome)}">'
               f'{f"<span>{e(rotulo)}</span>" if rotulo else ""}{links}</div>')
    if not com_sel:
        return pilulas
    opts = "".join(f'<option value="{e(h)}"{" selected" if str(k) == str(atual) else ""}>{e(str(t))}</option>' for k, t, h in itens)
    return (pilulas + f'<label class="filtro-sel{" abas" if abas else ""}">{f"<span>{e(rotulo)}</span>" if rotulo else ""}'
            f'<select data-vai aria-label="{e(rotulo or nome)}">{opts}</select></label>')


def botao(rotulo, *, tipo="neutro", name=None, value=None, submit=True, abre=None, href=None):
    """tipo: neutro | prim | perigo. abre: id de um <dialog> que o botão abre. href: vira link com cara de botão."""
    cls = "bt" + ("" if tipo == "neutro" else f" {tipo}")
    if href:
        return f'<a class="{cls}" href="{e(href)}">{e(rotulo)}</a>'
    attrs = f' name="{e(name)}" value="{e(str(value))}"' if name else ""
    attrs += f' data-abre="{e(abre)}"' if abre else ""
    return f'<button class="{cls}" type="{"submit" if submit and not abre else "button"}"{attrs}>{e(rotulo)}</button>'


def lista(itens, *, solta=False):
    """itens: dicts {titulo, href, meta (HTML), fala (texto), acoes (HTML), mais (HTML), id}. solta=True: linhas
    separadas por régua dentro de uma caixa; senão, um cartão por item."""
    out = []
    for k, x in enumerate(itens):
        tit = (f'<a class="tit" href="{e(x["href"])}">{e(str(x.get("titulo", "")))}</a>' if x.get("href")
               else f'<p class="tit">{e(str(x.get("titulo", "")))}</p>')
        meta = f'<div class="meta">{x["meta"]}</div>' if x.get("meta") else ""
        fala = f'<p class="fala">{e(str(x["fala"]))}</p>' if x.get("fala") else ""
        did = f' data-id="{e(str(x["acoes_id"]))}"' if x.get("acoes_id") else ""
        acoes = f'<div class="acoes"{did}>{x["acoes"]}</div>' if x.get("acoes") else ""
        mais = f'<div class="mais">{x["mais"]}</div>' if x.get("mais") else ""
        cls = "item" if solta else f"cartao item i{min(k + 1, 12)}"
        idx = f' id="{e(x["id"])}"' if x.get("id") else ""
        out.append(f'<article class="{cls}"{idx}><div>{tit}{meta}</div>{acoes}{fala}{mais}</article>')
    return f'<div class="{"linhas" if solta else "lista"}">{"".join(out)}</div>'


def md(texto):
    """Markdown vira HTML no navegador (marked + DOMPurify, só nas telas que têm markdown)."""
    if not str(texto).strip():
        return vazio("Nada gravado ainda.")
    return f'<textarea class="md-src" hidden>{e(texto)}</textarea><noscript><pre class="raw">{e(texto)}</pre></noscript>'


def kv(pares):
    """Lista chave → valor (<dl>). Valores são texto (escapados)."""
    return '<dl class="kv">' + "".join(f"<dt>{e(str(k))}</dt><dd>{e(str(v))}</dd>" for k, v in pares) + "</dl>"


def dialogo(id_, titulo, corpo):
    return (f'<dialog id="{e(id_)}" aria-labelledby="{e(id_)}-t"><header class="topo-d"><h2 id="{e(id_)}-t">{e(titulo)}</h2></header>'
            f'{corpo}<p><button class="bt fecha" type="button">Fechar</button></p></dialog>')


# ---------- casca ----------

def _ativo(caminho):
    """Item do menu que acende: telas de detalhe acendem a mãe (/proposta/x → Propostas)."""
    c = urllib.parse.urlsplit(caminho or "").path.rstrip("/") or "/"
    partes = [p for p in c.split("/") if p]
    if partes[:1] == ["proposta"]:
        return "/propostas"
    if partes[:1] == ["site"]:
        return "/operacao"
    if partes[:1] == ["claude"]:
        return "/uso"
    if partes[:1] == ["vps"]:
        return "/monitor"
    if partes[:1] == ["biblioteca"]:
        return "/biblioteca"
    if partes[:1] == ["mapa"]:
        return "/mapa"
    if partes[:1] == ["bi"] and len(partes) == 3 and partes[2] in ("local", "mercado"):
        return f"/bi/fsite05/{partes[2]}"
    return c


def _menu(ativo, n_pend, caminho, dono=False):
    atual = _ativo(ativo or caminho)
    neg_atual = None
    partes = [p for p in urllib.parse.urlsplit(caminho or ativo or "").path.split("/") if p]
    if partes[:1] == ["bi"] and len(partes) >= 2:
        neg_atual = partes[1]
    grupos, grupo_atual = "", None
    grupos_menu = menu(dono)
    for g, _, itens in grupos_menu:
        links = ""
        for h, t in itens:
            if h == atual:
                grupo_atual = g
            links += (f'<a href="{h}"{" aria-current=page" if h == atual else ""}>{e(t)}'
                      f'{f"<span class=n>{n_pend}</span>" if h == "/pendencias" and n_pend else ""}</a>')
        extra = ""
        if g == "Negócios":
            extra = '<div class="negocios">' + "".join(
                f'<a href="/bi/{n}"{" aria-current=page" if n == neg_atual and len(partes) == 2 else ""}>{e(t)}</a>'
                for n, t in negocios()) + "</div>"
            if neg_atual and len(partes) == 2:
                grupo_atual = g
        grupos += f'<div class="grupo"><h3>{e(g)}</h3>{links}{extra}</div>'
    lado = (f'<nav class="lado" aria-label="Menu"><a class="marca" href="/pendencias">{simbolo()}{e(NOME_PRODUTO)}</a>'
            f'{grupos}<div class="rodape">{PESCOCO}<span>Painel privado</span><a class="sair" href="/sair">Sair</a>{TEMA_BT}</div></nav>')
    baixo = "".join(
        f'<a href="{itens[0][0]}"{" aria-current=page" if g == grupo_atual else ""}><b aria-hidden="true">{ic}</b>{e(g)}'
        f'{f"<span class=n>{n_pend}</span>" if g == "Hoje" and n_pend else ""}</a>' for g, ic, itens in grupos_menu)
    topo_cel = ""
    if grupo_atual:
        itens = next(i for g, _, i in grupos_menu if g == grupo_atual)
        if grupo_atual == "Negócios":
            itens = itens + [(f"/bi/{n}", t) for n, t in negocios()]
        alvo = f"/bi/{neg_atual}" if grupo_atual == "Negócios" and neg_atual and len(partes) == 2 else atual
        topo_cel = filtro("menu", itens, alvo, rotulo="", url=lambda h: h, select=False).replace('class="filtro"', 'class="filtro grupo-topo"', 1)
    return lado, f'<nav class="embaixo" aria-label="Menu">{baixo}</nav>', topo_cel


def pagina(titulo, corpo, ativo=None, *, olho="", sub="", acoes="", cabeca="", n_pend=0, publica=False, caminho=None, topo=True,
           dono=False):
    """Página inteira: menu agrupado (lateral no desktop, barra embaixo no celular), topo com olho/título/sub/ações,
    View Transitions, Speculation Rules e JS no fim. publica: sem menu, com noindex (relatório aberto /r/...).
    cabeca: HTML extra no <head> (ex.: <script> do ECharts). corpo/acoes/sub já são HTML. topo=False: o corpo traz o h1.
    dono: o menu mostra as telas de dono (Usuários)."""
    if publica:
        lado = baixo = topo_cel = ""
    else:
        lado, baixo, topo_cel = _menu(ativo, n_pend, caminho, dono)
    js_md = JS_MD if "md-src" in corpo else ""
    sair = "" if publica else '<a class="sair" href="/sair">Sair</a>'  # no celular o menu lateral some
    marca_cel = "" if publica else f'<a class="marca cel" href="/pendencias">{simbolo()}{e(NOME_PRODUTO)}</a>'
    topo = "" if not topo else (f'<div class="topo"><div>{f"<p class=olho>{e(olho)}</p>" if olho else ""}<h1>{e(titulo)}</h1>'
            f'{f"<p class=sub>{sub}</p>" if sub else ""}</div>{f"<div class=acoes>{acoes}</div>" if acoes else ""}</div>')
    robots = '<meta name="robots" content="noindex">'
    faixa_demo = f'<div class="demo-faixa" role="status">{e(FAIXA_DEMO)}</div>' if demo_ativa() else ""
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">{robots}'
            f'<meta name="view-transition" content="same-origin"><meta name="color-scheme" content="dark light">{TEMA_HEAD}'
            f'<title>{e(titulo)} · {e(NOME_PRODUTO)}</title><link rel="icon" href="/favicon.svg" type="image/svg+xml">{FONTES}'
            f'<style>{TOKENS}{CSS}{_classes_geradas()}</style>{"" if publica else _speculation()}{cabeca}</head>'
            f'<body><a class="pular" href="#conteudo">Pular para o conteúdo</a><div class="app{" publica" if publica else ""}">{lado}'
            f'<main id="conteudo">{faixa_demo}{marca_cel}{topo_cel}{topo}{corpo}<div class="rodape fim-tema">{PESCOCO}{sair}{TEMA_BT}</div></main></div>{baixo}{js_md}{JS}</body></html>')


def entrar(volta="/", erro=None):
    """Tela pública de login (/entrar): a ampulheta grande, o nome e o formulário (POST /entrar). volta = para onde ir
    depois (o servidor valida de novo); erro = aviso já em português. Tudo escapado aqui."""
    av = f'<div class="aviso erro" role="alert">{e(erro)}</div>' if erro else ""
    corpo = (f'<section class="entrar">{simbolo("simbolo entra")}'
             f'<h1 class="entra i2">{e(NOME_PRODUTO)}</h1>'
             f'<form class="form entra i3" method="post" action="/entrar"><input type="hidden" name="volta" value="{e(volta or "/")}">'
             '<label>E-mail ou login<input name="email" type="text" autocomplete="username" autocapitalize="none" required autofocus></label>'
             '<label>Senha<input name="senha" type="password" autocomplete="current-password" required></label>'
             f'<button class="prim entra i4" type="submit">Entrar</button></form></section>{av}')
    return pagina("Entrar", corpo, publica=True, topo=False)


def sair(csrf):
    """GET /sair: confirma com um POST (CSRF da sessão); sair por link puro deixaria outra página deslogar o dono."""
    corpo = (f'<section class="entrar">{simbolo("simbolo entra")}<h1 class="entra i2">{e(NOME_PRODUTO)}</h1>'
             f'<form class="form entra i3" method="post" action="/sair"><input type="hidden" name="csrf" value="{e(csrf)}">'
             '<button class="prim" type="submit">Sair</button></form></section>')
    return pagina("Sair", corpo, publica=True, topo=False)


def convite(token, erro=None):
    """Tela pública do convite (/convite?t=): senha e repetir, POST /convite com o token escondido. Tudo escapado aqui."""
    av = f'<div class="aviso erro" role="alert">{e(erro)}</div>' if erro else ""
    corpo = (f'<section class="entrar">{simbolo("simbolo entra")}<h1 class="entra i2">{e(NOME_PRODUTO)}</h1>'
             f'<form class="form entra i3" method="post" action="/convite"><input type="hidden" name="t" value="{e(token)}">'
             '<label>Nova senha<input name="senha" type="password" autocomplete="new-password" required autofocus></label>'
             '<label>Repita a senha<input name="senha2" type="password" autocomplete="new-password" required></label>'
             f'<button class="prim entra i4" type="submit">Criar acesso</button></form></section>{av}')
    return pagina("Convite", corpo, publica=True, topo=False)


def convite_invalido():
    """Convite que não vale (não existe, usado ou vencido): sem dizer qual."""
    corpo = (f'<section class="entrar">{simbolo("simbolo entra")}<h1 class="entra i2">{e(NOME_PRODUTO)}</h1>'
             '<p>Convite inválido ou vencido. Peça um novo a quem convidou você.</p></section>')
    return pagina("Convite", corpo, publica=True, topo=False)


def salto():
    """Página mínima que recarrega a mesma URL: navegação vinda de outro site não leva o cookie Strict; a 2ª, sim.
    Nada da URL pedida entra aqui."""
    return ('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="robots" content="noindex">'
            f'<meta http-equiv="refresh" content="0"><title>{e(NOME_PRODUTO)}</title></head><body></body></html>')


def _so_tokens_ok(css):
    """Linhas com número em px fora dos tokens (filetes de 1-2 px, sombras de foco de 3 px e quebras de tela valem)."""
    import re
    ruins = []
    for m in re.finditer(r"(?<![\w.#-])(\d+(?:\.\d+)?)px", css):
        v = float(m.group(1))
        antes = css[max(0, m.start() - 12):m.start()]
        if v <= 3 or "width:" in antes and ("max-" in antes or "min-" in antes) and "@media" in css[max(0, m.start() - 30):m.start()]:
            continue
        ruins.append(css[max(0, m.start() - 30):m.end() + 5])
    return ruins

