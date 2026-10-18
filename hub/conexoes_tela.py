"""Tela Conexões (/config/conexoes): um cartão por serviço. Registrada em config_telas.ROTAS_GET/ROTAS_POST.

Nenhum valor de segredo chega ao HTML: só ••••fim4, quando e por quem foi definido. O e-mail da conta de serviço do Google
não é segredo e aparece com o botão "copiar".
"""
import html

from hub import cofre, conexoes, ui

e = html.escape
MASCARA = "••••"
COPIAR_JS = ("<script>document.querySelectorAll('[data-copiar]').forEach(b=>{b.onclick=()=>navigator.clipboard"
             ".writeText(b.dataset.copiar).then(()=>{b.textContent='Copiado';setTimeout(()=>{b.textContent='Copiar'},1500)})})</script>")


def _pode(usuario):
    from hub import config_telas
    return config_telas.pode(usuario, "conexoes.escrever")


def _csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _situacao(c, segredos):
    """Linha 'definido ••••fim4 · 17/10 14:17 por ana' ou 'não definido' de um campo secreto."""
    s = segredos.get(c["id"])
    if not s:
        return '<small class="mu">não definido</small>'
    fim = MASCARA + s["fim"] if s["fim"] else MASCARA
    return f'<small class="mu">definido <span class="mono">{e(fim)}</span> em {e(ui.quando(s["definido_em"]))} por {e(s["por"])}</small>'


def _campo(c, segredos, estado):
    nome, rot = e(c["id"]), e(c["rotulo"])
    if c["tipo"] == "textarea":
        entrada = (f'<textarea name="{nome}" rows="5" autocomplete="off" spellcheck="false" aria-label="{rot}" '
                   f'placeholder="{e(c["dica"])}"></textarea>')
    elif c["segredo"]:
        entrada = (f'<input type="password" name="{nome}" autocomplete="off" spellcheck="false" aria-label="{rot}" '
                   f'placeholder="{"deixe vazio para manter" if c["id"] in segredos else e(c["dica"])}">')
    else:
        entrada = (f'<input name="{nome}" value="{e(estado)}" autocomplete="off" spellcheck="false" maxlength="200" '
                   f'aria-label="{rot}" placeholder="{e(c["dica"])}">')
    extra = _situacao(c, segredos) if c["segredo"] and c["id"] != "google.conta_servico" else ""
    return f'<label class="campo cheio"><span class="mu">{rot}</span>{entrada}{extra}</label>'


def _lista(itens):
    return "<ol>" + "".join(f"<li>{e(p)}</li>" for p in itens) + "</ol>"


def _cartao(nome, csrf, edita, segredos, estado):
    d = conexoes.CONEXOES[nome]
    if d.get("em_breve"):
        return ui.card(d["titulo"], f'<p>{ui.selo("em breve", "neutro")}</p><p class="mu">{e(d["o_que_e"])} '
                       'Ainda não funciona: use a chave pessoal no cartão do Linear.</p>', id_=f"c-{nome}")
    selos = ""
    if d.get("paga"):
        selos += ui.selo("paga, desligada por padrão", "alerta") + " "
    t = conexoes.ultimo_teste(nome)
    if t:
        selos += ui.selo(("ok " if t["ok"] else "erro ") + ui.quando(t.get("em")), "ok" if t["ok"] else "ruim")
        ultimo = f'<p class="mu" role="status">{e(str(t.get("mensagem", "")))}</p>'
    else:
        selos += ui.selo("nunca testado", "alerta")
        ultimo = ""
    corpo = f'<p>{selos}</p><p>{e(d["o_que_e"])}</p>'
    corpo += f'<details class="mais"><summary>Onde pegar</summary>{_lista(d["passos"])}</details>'
    if nome == "google":
        email = conexoes.email_conta_servico()
        if email:
            corpo += (f'<p>Conta de serviço: <code class="mono">{e(email)}</code> '
                      f'<button type="button" class="bt" data-copiar="{e(email)}">Copiar</button></p>')
        else:
            corpo += '<p class="mu">Nenhuma conta de serviço guardada ainda.</p>'
        corpo += f'<details class="mais" open><summary>Dar acesso a essa conta</summary>{_lista(d["acesso"])}</details>'
    if edita and d["campos"]:
        campos = "".join(_campo(c, segredos, conexoes.campo(c["id"]) if not c["segredo"] else "") for c in d["campos"])
        corpo += (f'<form class="form" method="post" action="/config/conexoes">{_csrf(csrf)}'
                  f'<input type="hidden" name="acao" value="salvar"><input type="hidden" name="conexao" value="{e(nome)}">'
                  f'{campos}{ui.botao("Guardar", tipo="prim")}</form>')
    corpo += f'<p class="mu">Teste: {e(d["teste"])}</p>'
    if edita:
        corpo += (f'<form class="form" method="post" action="/config/conexoes">{_csrf(csrf)}'
                  f'<input type="hidden" name="acao" value="testar"><input type="hidden" name="conexao" value="{e(nome)}">'
                  f'{ui.botao("Testar")}</form>')
    return ui.card(d["titulo"], corpo + ultimo, id_=f"c-{nome}")


def conexoes_html(csrf, usuario):
    edita = _pode(usuario)
    try:
        segredos = {s["nome"]: s for s in cofre.listar()}
        aviso = ""
    except (cofre.CofreError, OSError):
        segredos = {}
        aviso = f'<p role="alert">{ui.selo("O cofre não abriu: não dá para mostrar o que está definido.", "ruim")}</p>'
    return (aviso + '<div class="grade">' + "".join(_cartao(n, csrf, edita, segredos, "") for n in conexoes.CONEXOES) + "</div>"
            + COPIAR_JS)


def conexoes_get(q, usuario, csrf):
    return 200, ui.pagina("Conexões", conexoes_html(csrf, usuario), "/config/conexoes", olho="Configurar",
                          sub=f"Serviços de fora que o {e(ui.NOME_PRODUTO)} usa. Chaves vão para o cofre; o valor nunca volta para a tela.")


def conexoes_post(campos, usuario, ip):
    """(ok, mensagem em português). Nunca devolve o valor de um segredo."""
    nome, acao = (campos.get("conexao") or "").strip(), campos.get("acao", "")
    if nome not in conexoes.CONEXOES:
        return False, "Conexão desconhecida."
    try:
        if acao == "salvar":
            return conexoes.salvar(nome, campos, usuario, ip)
        if acao == "testar":
            return conexoes.testar(nome, TRANSPORTES.get(nome))
    except OSError:
        return False, "Não consegui gravar agora. Veja as permissões da pasta de dados e do cofre."
    except Exception:  # noqa: BLE001  (a mensagem de uma exceção qualquer pode carregar o valor: não repassa)
        return False, "Não consegui concluir agora. Tente de novo."
    return False, "Ação desconhecida."


TRANSPORTES = {}  # {conexão: transporte} injetável (testes); vazio = rede real
