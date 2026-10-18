"""Tela Plugins (/config/plugins): os pacotes kronos_plugin_* achados (plugins.descobertos), ligado/desligado, versão, o que cada um
traz (rotas, menu, rotinas) e o erro de import, com liga/desliga. Registrada em config_telas.ROTAS_GET/ROTAS_POST.

NUNCA importa plugin desligado (importar executa código): a versão e o que ele traz vêm de leitura estática do `__init__.py`
(ast, sem executar); só os ligados e carregados mostram o que o carregador de fato registrou (plugins.carregado). Ligar um plugin
executa o código dele no painel: a tela avisa. O nome vem do formulário mas só vale se estiver em plugins.descobertos().
Liga/desliga grava `plugins.ligados` na sobreposição do painel (config.gravar): se o valor efetivo é "*", desligar
materializa a lista explícita dos outros descobertos (nenhum se perde). Sem a chave, o padrão do código segue valendo.
O WhatsApp (triagem-wpp) é um projeto separado: o cartão é só informativo e não importa nada dele.
"""
import ast
import html
import os

from hub import config, plugins, ui

e = html.escape
MAX_FONTE = 300_000  # bytes lidos de um __init__.py para a extração estática
CAB = ["Plugin", "Estado", "Versão", "O que traz", ""]


def _pode(usuario):
    from hub import config_telas  # import tardio: config_telas importa este módulo
    return config_telas.pode(usuario, "plugins.escrever")


def _csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _literal(no):
    try:
        return ast.literal_eval(no)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def ler_estatico(pasta):
    """{"versao", "rotas", "menu", "rotinas"} lidos do __init__.py por ast, SEM importar nem executar. Valor que não é literal
    (ou arquivo ilegível) vira None."""
    vazio = {"versao": None, "rotas": None, "menu": None, "rotinas": None}
    try:
        with open(os.path.join(pasta, "__init__.py"), "rb") as f:
            arvore = ast.parse(f.read(MAX_FONTE))
    except (OSError, SyntaxError, ValueError, MemoryError, RecursionError):
        return vazio
    achado = dict(vazio)
    for no in arvore.body:
        alvos, valor = (no.targets, no.value) if isinstance(no, ast.Assign) else ([no.target], no.value) if isinstance(no, ast.AnnAssign) else ([], None)
        for alvo in alvos:
            nome = getattr(alvo, "id", None)
            if valor is None or nome not in ("__version__", "ROTAS", "MENU", "ROTINAS"):
                continue
            if nome == "ROTAS" and isinstance(valor, ast.Dict):  # só as chaves: os valores são funções
                chaves = [_literal(k) if k is not None else None for k in valor.keys]
                achado["rotas"] = [k for k in chaves if isinstance(k, str)]
            elif nome == "__version__":
                v = _literal(valor)
                achado["versao"] = v if isinstance(v, str) else None
            elif nome == "MENU":
                v = _literal(valor)
                achado["menu"] = [tuple(i) for i in v if isinstance(i, (tuple, list)) and len(i) >= 3] if isinstance(v, (list, tuple)) else None
            elif nome == "ROTINAS":
                v = _literal(valor)
                achado["rotinas"] = [str(i[0]) for i in v if isinstance(i, (tuple, list)) and i] if isinstance(v, (list, tuple)) else None
    return achado


def _efetivos():
    """Nomes ligados (sem prefixo), com o "*" já expandido para os descobertos."""
    return plugins.ligados()


def _traz(info, carregado):
    """(HTML do que traz, nota). Ligado e carregado: o que o carregador registrou; senão, leitura estática."""
    if carregado is not None:
        rotas, menu, rotinas, fonte = carregado["rotas"], carregado["menu"], carregado["rotinas"], "carregado"
    else:
        rotas, menu, rotinas, fonte = info["rotas"], info["menu"], info["rotinas"], "lido do código sem executar"
    partes = []
    if rotas:
        partes.append("Rotas: " + " ".join(f'<code class="mono">{e(r)}</code>' for r in rotas))
    if menu:
        partes.append("Menu: " + ", ".join(e(f"{i[0]} › {i[2]}") for i in menu))
    if rotinas:
        partes.append("Rotinas: " + " ".join(f'<code class="mono">{e(r)}</code>' for r in rotinas))
    if not partes:
        return '<span class="mu">nada declarado</span>', ""
    return "<br>".join(partes) + f'<br><small class="mu">{e(fonte)}</small>', fonte


def _form(csrf, acao, nome, rotulo, botao="neutro"):
    return (f'<form class="form" method="post" action="/config/plugins">{_csrf(csrf)}'
            f'<input type="hidden" name="acao" value="{e(acao)}"><input type="hidden" name="nome" value="{e(nome)}">'
            f'{ui.botao(rotulo, tipo=botao)}</form>')


def _linhas(csrf, edita):
    achados, ligados, erros = plugins.descobertos(), set(_efetivos()), plugins.erros()
    linhas = []
    for nome, pasta in achados.items():
        info = ler_estatico(pasta / (plugins.PREFIXO + nome))
        ligado = nome in ligados
        traz, _ = _traz(info, plugins.carregado(nome) if ligado else None)
        estado = ui.selo("ligado", "ok") if ligado else ui.selo("desligado", "alerta")
        if nome in erros:
            estado += " " + ui.selo("não carregou", "ruim")
            traz = f'<span class="erro-plugin">{e(erros[nome])}</span><br>' + traz
        nome_html = f'<b class="mono">{e(nome)}</b><br><small class="mu">{e(str(pasta))}</small>'
        acao = (_form(csrf, "desligar" if ligado else "ligar", nome, "Desligar" if ligado else "Ligar", "neutro" if ligado else "prim")
                if edita else "")
        linhas.append([ui.td(nome_html, html_=True), ui.td(estado, html_=True),
                       ui.td(info["versao"] or "sem versão"), ui.td(traz, html_=True), ui.td(acao, html_=True)])
    return linhas


def _cartao_whatsapp():
    try:
        link = f"https://github.com/{config.repo('triagem-wpp')}"
        repo = f'<a href="{e(link)}">{e(link)}</a>'
    except config.ConfigError:
        repo = "repositório triagem-wpp (defina github.org na configuração para ver o link)"
    corpo = (f'<p>{ui.selo("desligado por padrão", "alerta")}</p>'
             "<p>O WhatsApp roda como projeto separado (triagem-wpp), em outro processo e outro repositório: não é um pacote "
             "kronos_plugin_* e não é importado por este painel. Ele faz a triagem das conversas, atende os parceiros e lê a "
             "caixa de avisos que o hub escreve. Nada é ligado ou desligado daqui; esta tela só informa onde ele mora.</p>"
             f"<p>Repositório: {repo}</p>")
    return ui.card("Plugin externo: WhatsApp (triagem-wpp)", corpo)


def plugins_html(csrf, usuario):
    edita = _pode(usuario)
    aviso = ('<p class="mu"><b>Atenção:</b> ligar um plugin executa o código dele no painel, com as permissões do painel. '
             "Só ligue plugins em que você confia. O que um plugin desligado traz é lido do código sem executá-lo.</p>")
    linhas = _linhas(csrf, edita)
    if linhas:
        lista = ui.card("Plugins achados", ui.tabela(CAB, linhas, titulo=0, largas=(0, 3)), conta=len(linhas), caixa=False)
    else:
        lista = ui.card("Plugins achados", ui.vazio("Nenhum plugin encontrado.", "Aponte plugins.caminho para uma pasta com pacotes kronos_plugin_<nome>."))
    return aviso + lista + _cartao_whatsapp()


def _pagina(corpo):
    return ui.pagina("Plugins", corpo, "/config/plugins", olho="Configurar",
                     sub="Pacotes que acrescentam telas, rotinas e itens de menu: liga e desliga.")


def plugins_get(q, usuario, csrf):
    return 200, _pagina(plugins_html(csrf, usuario))


def _ligar(campos, ligar):
    nome = str(campos.get("nome") or "").removeprefix(plugins.PREFIXO)
    if nome not in plugins.descobertos():  # nunca nome livre
        return False, "Plugin desconhecido: só os achados na pasta de plugins."

    var = next((v for v in ("KRONOS_PLUGINS_LIGADOS", config.APELIDOS.get("plugins.ligados")) if v and os.environ.get(v)), None)
    if var:  # a variável de ambiente vence a sobreposição: gravar não teria efeito, então nem grava
        return False, f"A lista de plugins está fixada pela variável de ambiente {var}: tire-a do ambiente do painel para ligar e desligar por aqui."
    curinga = "*" in [str(n).removeprefix(plugins.PREFIXO) for n in config.get("plugins.ligados")]

    def novos():
        atuais = plugins.ligados()  # com a config já recarregada pela trava; "*" já expandido (materializa o curinga)
        if ligar:
            lista = list(atuais) if nome in atuais else [*atuais, nome]
        else:
            lista = [n for n in atuais if n != nome]
        return {"plugins.ligados": lista}

    try:
        config.gravar(novos)
    except OSError:
        return False, "Não consegui gravar a configuração agora. Veja as permissões da pasta de dados."
    except config.ConfigError:
        return False, "Valor recusado pela configuração."
    plugins.recarregar()
    if curinga:
        return True, (f"{nome} {'ligado' if ligar else 'desligado'}. A lista agora é explícita (antes era \"*\", todos): plugins novos "
                      "que aparecerem na pasta passam a precisar ser ligados à mão.")
    if ligar:
        return True, f"{nome} ligado. O código dele roda no painel a partir de agora."
    return True, f"{nome} desligado."


def plugins_post(campos, usuario, ip):
    acao = campos.get("acao", "")
    if acao == "ligar":
        return _ligar(campos, True)
    if acao == "desligar":
        return _ligar(campos, False)
    return False, "Ação desconhecida."
