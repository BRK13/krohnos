"""Tela Agentes de configuração (/config/agentes): contas Claude, faixa, modelo por papel, limite de gasto por dia e
"Adicionar conta" (login por código de aparelho, hub/contas_login.py). Registrada em config_telas.ROTAS_GET/ROTAS_POST.

Faixas, modelos e limite gravam na sobreposição do painel (krohnos.painel.toml, seção agentes) por config.gravar. Nenhum token chega ao HTML: só o status
do login, o link e o código de aparelho que a CLI mostrou. A tela de monitoramento (/agentes) continua em painel.py.
"""
import html
import json
import re
from pathlib import Path

from hub import agenda, cockpit, conta, config, contas_login, plano_itens, ui
from hub import data as dados_dir

e = html.escape
FAIXAS = (("construtor", "Construtor (pedidos do Linear)"), ("operador", "Operador (rotinas agendadas)"),
          ("codex", "Codex"))
PAPEIS = (("construtor", "Construtor"), ("operador", "Operador"), ("revisao", "Revisão"))
MODELO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]:-]{0,59}")
LIMITE_MAX = 100000
LOGIN = {"aguardando": ("Esperando você entrar", "alerta"), "concluindo": ("Gravando a conta", "alerta"), "ok": ("Entrou", "ok"), "cancelado": ("Cancelado", "neutro"),
         "erro": ("Não deu certo", "ruim"), "tempo": ("Tempo esgotado", "ruim")}
SELO = {"ok": ("Login ok", "ok"), "expirado": ("Login expirado", "ruim"), "sem_login": ("Sem login", "ruim")}


def _pode(usuario):
    return config_telas().pode(usuario, "agentes.escrever")


def config_telas():
    from hub import config_telas as c  # import tardio: config_telas importa este módulo
    return c


def _csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _form(csrf, acao, corpo, extra=""):
    return (f'<form class="form" method="post" action="/config/agentes">{_csrf(csrf)}'
            f'<input type="hidden" name="acao" value="{e(acao)}">{extra}{corpo}</form>')


def _usd(v):
    return ui.num(v, moeda="US$", casas=2)


def _faixa_item(item):
    """Faixa de um item da fila sem carregar a tarefa: pedido do Linear é construtor (ou codex), rotina é operador."""
    if item.get("debugger"):
        return "codex" if (item["debugger"] or {}).get("executor", "codex") == "codex" else "construtor"
    if item.get("linear"):
        return "codex" if (item["linear"] or {}).get("executor") == "codex" else "construtor"
    return "operador"


def _select(nome, atual):
    ops = "".join(f'<option value="{e(k)}"{" selected" if k == atual else ""}>{e(r)}</option>' for k, r in FAIXAS)
    if atual and atual not in dict(FAIXAS):
        ops = f'<option value="{e(atual)}" selected>{e(atual)}</option>' + ops
    return f'<select name="{nome}" aria-label="Faixa">{ops}</select>'


def _faz_agora(n, rodando):
    r = next((x for x in rodando if str(x.get("conta") or "1") == n), None)
    if not r:
        return '<span class="mu">Livre</span>'
    nome = str(r.get("nome") or "")
    alvo = cockpit._issue(nome) or nome
    return f'<b class="mono">{e(alvo)}</b> <span class="mu">{e(ui.ha(r.get("inicio")))}</span>'


def _limite_da_conta(n, padrao):
    proprios = config.get("agentes.limites") or {}
    try:
        return float(proprios[n]), True
    except (KeyError, TypeError, ValueError):
        return padrao, False


def _linha(n, csrf, edita, base, rodando, agora, padrao):
    s = contas_login.situacao(n)
    selo = ui.selo(*SELO[s])
    faixa = conta.faixa(n) or ""
    limite, proprio = _limite_da_conta(n, padrao)
    gasto = conta.gasto_dia(n, base, agora)
    uso = _usd(gasto)
    if limite:
        uso += f' <span class="mu">limite {e(_usd(limite))}{"" if proprio else " (padrão)"}</span>'
        if gasto >= limite:
            uso += " " + ui.selo("Limite do dia atingido", "ruim")
    p = conta.pausa_ativa(base, agora, n=n)
    pausa = ui.selo("Pausada até " + ui.quando(p["ate"]), "alerta") if p else '<span class="mu">Não</span>'
    if edita:
        campo = (f'<input name="limite" inputmode="decimal" maxlength="9" aria-label="Limite por dia em dólares" '
                 f'placeholder="padrão" value="{e(f"{limite:g}") if proprio else ""}" autocomplete="off">')
        edicao = _form(csrf, "conta", f'{_select("faixa", faixa)}{campo}{ui.botao("Salvar")}',
                       f'<input type="hidden" name="conta" value="{e(n)}">')
        acoes = _form(csrf, "entrar", ui.botao("Entrar de novo"), f'<input type="hidden" name="conta" value="{e(n)}">') if s != "ok" else ""
    else:
        edicao, acoes = e(faixa or "—"), ""
    return [ui.td(f"Conta {n}", classe="mono"), ui.td(selo, html_=True), ui.td(edicao, html_=True), ui.td(uso, html_=True),
            ui.td(pausa, html_=True), ui.td(_faz_agora(n, rodando), html_=True), ui.td(acoes, html_=True)]


def _orq(base):
    """Orquestrador e revisor Codex, do estado.json que o hub.orq_estado grava ao fim de cada varredura."""
    try:
        est = json.loads((Path(base) / "orch" / "estado.json").read_text(encoding="utf-8"))
        rodando = (est.get("varredura") or {}).get("rodando")
        cx = (est.get("codex") or {}).get("hoje") or {}
    except (OSError, ValueError, AttributeError):
        return ui.card("Orquestrador e Codex", '<p class="mu">Estado ainda não gravado.</p>')
    faz = (f'Varrendo {e(ui.ha(rodando.get("desde")))}' if rodando else "Em espera")
    return ui.card("Orquestrador e Codex", f'<p><b>Orquestrador</b>: {faz}</p>'
                   f'<p><b>Codex</b>: {e(str(cx.get("revisados", 0)))} PRs revisados hoje</p>')


def _fila(base):
    try:
        itens = agenda.fila(base)
    except OSError:
        itens = []
    por = {k: [] for k, _ in FAIXAS}
    for i in itens:
        por[_faixa_item(i)].append(cockpit._issue(i.get("nome")) or str(i.get("nome") or ""))
    corpo = ""
    for k, rot in FAIXAS:
        chips = "".join(ui.selo(x, mono=True) for x in por[k]) or '<span class="mu">vazia</span>'
        corpo += f'<p><b>{e(rot)}</b> <span class="mu">{len(por[k])} na fila</span></p><div class="chips">{chips}</div>'
    return ui.card("Fila por faixa", corpo)


def _modelos(csrf, edita):
    atual = config.get("agentes.modelos") or {}
    campos = "".join(
        f'<label class="campo"><span class="mu">{e(rot)}</span><input name="{k}" list="modelos-sugeridos" maxlength="60" '
        f'value="{e(str(atual.get(k, "")))}" autocomplete="off" spellcheck="false" placeholder="padrão do hub"></label>'
        for k, rot in PAPEIS)
    lista = '<datalist id="modelos-sugeridos"><option value="opus"><option value="sonnet"><option value="haiku"></datalist>'
    corpo = ('<p class="mu">Gravado na sobreposição do painel (agentes.modelos). O roteamento passa a usar quando ler esta chave: '
             'hoje ele ainda escolhe o modelo pelo tipo de tarefa.</p>')
    corpo += _form(csrf, "modelos", campos + lista + ui.botao("Guardar modelos", tipo="prim")) if edita else \
        "".join(f"<p>{e(rot)}: {e(str(atual.get(k, '—')))}</p>" for k, rot in PAPEIS)
    return ui.card("Modelo por papel", corpo)


def _limite(csrf, edita, limite):
    corpo = ('<p class="mu">Padrão para as contas sem limite próprio (a coluna Faixa edita o de cada conta). Gasto no dia '
             '(Brasília): passou do limite, a conta não pega trabalho novo até o dia seguinte. 0 = sem limite.</p>')
    if edita:
        corpo += _form(csrf, "limite", f'<label class="campo"><span class="mu">US$ por conta por dia</span><input name="limite" '
                       f'inputmode="decimal" value="{limite:g}" autocomplete="off" maxlength="9"></label>'
                       f'{ui.botao("Guardar limite", tipo="prim")}')
    else:
        corpo += f"<p>{e(_usd(limite)) if limite else 'Sem limite'}</p>"
    return ui.card("Limite de gasto por dia (padrão)", corpo)


def _adicionar(csrf):
    n = contas_login.proximo_numero()
    corpo = (f'<p class="mu">Cria a pasta da conta {e(n)} e abre o login: você recebe um link e um código aqui, entra '
             'no navegador e a conta aparece sozinha na lista.</p>'
             + _form(csrf, "adicionar", f'<label class="campo"><span class="mu">Faixa</span>{_select("faixa", "operador")}</label>'
                     + ui.botao("Adicionar conta", tipo="prim")))
    return ui.card("Adicionar conta", corpo)


def _logins_abertos(csrf):
    abertos = [lg for lg in contas_login.LOGINS.values() if lg.estado in contas_login.ATIVOS]
    if not abertos:
        return ""
    itens = "".join(f'<p>{ui.selo(*LOGIN[lg.estado])} Conta {e(lg.n)} '
                    f'<a href="/config/agentes/login?id={e(lg.id)}">ver link e código</a></p>' for lg in abertos)
    return ui.card("Logins em andamento", itens)


def agentes_html(csrf, usuario, agora=None):
    edita = _pode(usuario)
    agora = agora or conta.agora()
    base = dados_dir()
    try:
        limite = float(config.get("agentes.limite_usd_dia") or 0)
    except (TypeError, ValueError):
        limite = 0.0
    try:
        rodando = list(agenda.rodando_todas(base).values())
    except OSError:
        rodando = []
    contas = conta.contas_config()
    linhas = [_linha(n, csrf, edita, base, rodando, agora, limite) for n in contas]
    tabela = ui.tabela(["Conta", "Login", "Faixa e limite/dia (US$)", "Uso de hoje", "Pausa", "Fazendo agora", ""], linhas, titulo=0, largas=(5,))
    corpo = (_logins_abertos(csrf) + ui.card("Contas Claude", tabela, conta=len(contas), caixa=False)
             + '<div class="grade">' + _modelos(csrf, edita) + _limite(csrf, edita, limite) + "</div>")
    if edita:
        corpo += _adicionar(csrf)
    return corpo + _fila(base) + _orq(base) + plano_itens.card_html(base)


def agentes_get(q, usuario, csrf):
    acoes = ui.botao("Ver o monitor", href="/agentes")
    return 200, ui.pagina("Agentes", agentes_html(csrf, usuario), "/config/agentes", olho="Configurar", acoes=acoes,
                          sub="Contas Claude, faixas, modelos e limite de gasto. O que muda aqui vai para a sobreposição do painel (krohnos.painel.toml).")


def login_html(lg, csrf, usuario):
    if lg is None:
        return ui.card("Login", '<p>Não achei esse login (já terminou ou o hub reiniciou).</p>'
                       + ui.botao("Voltar às contas", href="/config/agentes"))
    texto, tom = LOGIN[lg.estado]
    corpo = f"<p>{ui.selo(texto, tom)}</p>"
    if lg.estado == "ok":
        corpo += f'<p>Entrou. A conta {e(lg.n)} já aparece na lista.</p>'
        corpo += f'<p class="mu">{e(lg.aviso)}</p>' if lg.aviso else ""
    elif lg.estado == "aguardando":
        if lg.link:
            corpo += (f'<p>1. Abra o link e entre com a conta Claude:</p><p><a class="mono" href="{e(lg.link)}" target="_blank" '
                      f'rel="noopener noreferrer">{e(lg.link)}</a></p>')
        else:
            corpo += '<p class="mu">Abrindo o login, o link já aparece.</p>'
        if lg.codigo:
            corpo += f'<p>Código do aparelho: <b class="mono" data-codigo>{e(lg.codigo)}</b></p>'
        if lg.link and _pode(usuario):
            corpo += ('<p>2. Se a página mostrar um código, cole aqui:</p>'
                      + _form(csrf, "codigo", '<input name="codigo" autocomplete="off" spellcheck="false" maxlength="256" '
                              'aria-label="Código da página de login" required>' + ui.botao("Enviar código", tipo="prim"),
                              f'<input type="hidden" name="login" value="{e(lg.id)}">'))
    else:
        corpo += '<p class="mu">Nada foi gravado. Tente adicionar a conta de novo.</p>'
    if lg.estado == "aguardando" and _pode(usuario):
        corpo += _form(csrf, "cancelar", ui.botao("Cancelar login", tipo="perigo"), f'<input type="hidden" name="login" value="{e(lg.id)}">')
    corpo += ui.botao("Voltar às contas", href="/config/agentes")
    return ui.card(f"Conta {lg.n}" if lg.n else "Login", corpo)


def _pagina_login(lg, csrf, usuario):
    atualiza = ""
    if lg is not None and lg.estado == "aguardando":
        atualiza = f'<meta http-equiv="refresh" content="3;url=/config/agentes/login?id={e(lg.id)}">'
    return ui.pagina("Entrar na conta", login_html(lg, csrf, usuario), "/config/agentes", olho="Configurar", cabeca=atualiza,
                     sub="Login por código de aparelho. O token fica só na pasta da conta, nunca aparece aqui.")


def login_get(q, usuario, csrf):
    return 200, _pagina_login(contas_login.obter(q.get("id", "")), csrf, usuario)


# ---- POST ----

def _gravar(valores, ok):
    try:
        config.gravar(valores)
    except OSError:
        return False, "Não consegui gravar a configuração agora. Veja as permissões da pasta de dados."
    except config.ConfigError:
        return False, "Valor recusado pela configuração."
    return True, ok


def _numero(texto):
    """Dólares por dia: 0 a LIMITE_MAX, duas casas; None se inválido (nan e infinito caem fora na comparação)."""
    try:
        v = float(texto.strip().replace(",", "."))
    except ValueError:
        return None
    return round(v, 2) if 0 <= v <= LIMITE_MAX else None


def _post_conta(c):
    n, faixa, texto = (c.get("conta") or "").strip(), (c.get("faixa") or "").strip(), (c.get("limite") or "").strip()
    if n not in conta.contas_config():
        return False, "Conta desconhecida."
    if faixa not in dict(FAIXAS):
        return False, "Faixa inválida: construtor, operador ou codex."
    limite = _numero(texto) if texto else None
    if texto and limite is None:
        return False, "Limite inválido: de 0 a 100000 dólares por dia (vazio = usar o padrão)."

    def novos():
        proprios = dict(config.get("agentes.limites") or {})
        proprios.pop(n, None)
        if texto:
            proprios[n] = int(limite) if limite == int(limite) else limite
        faixas = {k: f for k in conta.contas_config() if (f := conta.faixa(k))}
        faixas[n] = faixa
        return {"agentes.faixas": faixas, "agentes.limites": proprios}
    return _gravar(novos, f"Conta {n}: faixa {faixa}, " + (f"limite US$ {limite:g} por dia." if texto else "limite padrão."))


def _post_modelos(c):
    novos = {}
    for k, _ in PAPEIS:
        v = (c.get(k) or "").strip()
        if v and not MODELO.fullmatch(v):
            return False, "Modelo inválido: use só letras, números, ponto, hífen e dois-pontos."
        novos[k] = v
    def novos_modelos():
        atual = dict(config.get("agentes.modelos") or {})
        for k, v in novos.items():
            atual.pop(k, None)
            if v:
                atual[k] = v
        return {"agentes.modelos": atual}
    return _gravar(novos_modelos, "Modelos guardados.")


def _post_limite(c):
    v = _numero(c.get("limite") or "")
    if v is None:
        return False, "Limite inválido: de 0 a 100000 dólares por dia (0 = sem limite)."
    return _gravar({"agentes.limite_usd_dia": int(v) if v == int(v) else v}, "Limite padrão guardado." if v else "Limite padrão desligado.")


def _comeca(n, faixa):
    try:
        lg = contas_login.iniciar(n, faixa)
    except ValueError:
        return False, "Número de conta inválido.", None
    if lg.estado == "erro":
        return False, "Não consegui abrir o login (a CLI do Claude não abriu). Veja se ela está instalada para o usuário do hub.", None
    return True, "", lg


def _post_adicionar(c, csrf, usuario):
    faixa = (c.get("faixa") or "operador").strip()
    if faixa not in dict(FAIXAS):
        return False, "Faixa inválida: construtor, operador ou codex."
    ok, msg, lg = _comeca(contas_login.proximo_numero(), faixa)
    return (True, "", _pagina_login(lg, csrf, usuario)) if ok else (False, msg)


def _post_entrar(c, csrf, usuario):
    n = (c.get("conta") or "").strip()
    if n not in conta.contas_config():
        return False, "Conta desconhecida."
    ok, msg, lg = _comeca(n, conta.faixa(n))
    return (True, "", _pagina_login(lg, csrf, usuario)) if ok else (False, msg)


def _post_codigo(c, csrf, usuario):
    lg = contas_login.obter((c.get("login") or "").strip())
    if lg is None or not contas_login.enviar_codigo(lg.id, c.get("codigo") or ""):
        return False, "Não consegui enviar o código (login encerrado ou código com formato estranho)."
    return True, "", _pagina_login(lg, csrf, usuario)


def agentes_post(campos, usuario, ip):
    """(ok, mensagem) ou (ok, "", html) quando a resposta é a tela de progresso do login."""
    acao = campos.get("acao", "")
    csrf = campos.get("csrf", "")
    try:
        if acao == "conta":
            return _post_conta(campos)
        if acao == "modelos":
            return _post_modelos(campos)
        if acao == "limite":
            return _post_limite(campos)
        if acao == "adicionar":
            return _post_adicionar(campos, csrf, usuario)
        if acao == "entrar":
            return _post_entrar(campos, csrf, usuario)
        if acao == "codigo":
            return _post_codigo(campos, csrf, usuario)
        if acao == "cancelar":
            lg = contas_login.obter((campos.get("login") or "").strip())
            if contas_login.cancelar(lg.id if lg else ""):
                return True, "Login cancelado."
            if lg is not None and lg.estado in ("concluindo", "ok"):
                return True, "Concluído (o cancelamento chegou tarde): a conta já entrou."
            return False, "Esse login já terminou."
    except OSError:
        return False, "Não consegui concluir agora. Tente de novo."
    return False, "Ação desconhecida."
