"""Tela Rotinas e Metas (/config/rotinas): uma linha por tarefa do Claude e por rotina (do sistema e de plugin), com
liga/desliga, horário com a próxima execução, último resultado e "rodar agora". Edita a tarefa do Claude num formulário
com o contrato de tarefas/README.md. Registrada em config_telas.ROTAS_GET/ROTAS_POST. HTML só por ui.*; texto de fora escapado.

Fusos: tarefa do Claude em Brasília; rotina em UTC (a tela diz isso em cada horário).
Liga/desliga grava `rotinas.desligadas` na sobreposição do painel (config.gravar): o nome da rotina, ou "tarefa:<nome>"
para tarefa do Claude (rotinas.chave_tarefa). Desligada não dispara sozinha (agenda.agendar e rotinas.devidas respeitam);
"rodar agora" continua valendo. Tarefa editada ou criada é gravada SEMPRE na camada do painel, <dados>/tarefas/ (config.tarefas_extra(), atômico), que a agenda
lê por cima da pasta de tarefas do repositório (origem: repo, painel ou sobrescrita); "voltar ao do repositório" arquiva a cópia
do painel em <dados>/tarefas/arquivo/. A tela nunca escreve em outro lugar.
"""
import contextlib
import datetime as dt
import html
import os
import re
import tempfile
import urllib.parse
from pathlib import Path

from hub import agenda, config, conta, metas, raiz, rotinas, tarefas as T, ui
from hub.comum.datas import BRT

e = html.escape
UTC = dt.timezone.utc
RE_NOME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # fullmatch: sem barra, ponto, maiúscula nem quebra de linha
CAMPOS = (("quando", "Quando (cron em horário de Brasília, ou manual)", "0 8 * * *"),
          ("prioridade", "Prioridade", None), ("modelo", "Modelo", None),
          ("timeout", "Tempo limite (90s, 15m, 2h; de 1 min a 6 h)", "10m"),
          ("repos", "Repositórios (dono/nome, separados por vírgula)", ""),
          ("externo", "Produz algo que sai para terceiros?", None),
          ("descricao", "Descrição (uma linha)", ""), ("pauta", "Pauta (app e tarefa, opcional)", ""),
          ("avisar", "Manda a saída no WhatsApp?", None), ("skills", "Skills (separadas por vírgula, opcional)", ""),
          ("conta", "Conta (vazio, api ou codex)", None))
OPCOES = {"prioridade": ("1", "2", "3"), "modelo": T.MODELOS, "externo": ("false", "true"), "avisar": ("false", "true"),
          "conta": ("", "api", "codex")}
DICA_OPCAO = {"prioridade": {"1": "1 alta", "2": "2 normal", "3": "3 baixa"}}
MAX_LOG = 30_000


def _pode(usuario):
    from hub import config_telas  # import tardio: config_telas importa este módulo
    return config_telas.pode(usuario, "rotinas.escrever")


def _csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _pasta():
    """Onde a tela grava: a camada do painel."""
    return Path(config.tarefas_extra())


def _entradas():
    """[{nome, origem, tarefa, erro, arquivo}] (tarefas.listar): arquivo com erro ou nome inválido aparece, não derruba a tela."""
    return T.listar(config.tarefas(), config.tarefas_extra())


def _lista_tarefas():
    """[(nome, Tarefa|None, erro|None)]."""
    return [(x["nome"], x["tarefa"], x["erro"]) for x in _entradas()]


def _q(nome):
    return urllib.parse.quote(nome, safe="")


def _ultimos_usos(base=None, agora=None):
    """{nome da tarefa: registro mais novo do uso.jsonl} dos últimos 45 dias."""
    agora = agora or conta.agora()
    try:
        regs = conta.ler_uso(agora - dt.timedelta(days=45), base)
    except OSError:
        return {}
    ult = {}
    for r in regs:
        n = r.get("nome")
        if isinstance(n, str) and (n not in ult or r["inicio"] > ult[n]["inicio"]):
            ult[n] = r
    return ult


def _dur(s):
    if s is None:
        return ""
    s = float(s)
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min"


def _celula_resultado(r, link):
    """r: {ok, em, duracao_s, estado?} ou None. link: href do log ou None."""
    if not r:
        return '<span class="mu">sem registro</span>'
    tom, texto = ("ok", "ok") if r["ok"] else ("ruim", "erro")
    if r.get("estado"):
        tom, texto = "alerta", r["estado"]
    partes = [ui.selo(texto, tom), f'<small class="mu">{e(ui.ha(r["em"]))}</small>']
    if r.get("duracao_s") is not None:
        partes.append(f'<small class="mu">{e(_dur(r["duracao_s"]))}</small>')
    if link:
        partes.append(f'<a href="{e(link)}">log</a>')
    return " ".join(partes)


def _resultado_tarefa(reg):
    if not reg:
        return None
    estado = "limite de uso" if reg.get("limite") else ("interrompida" if reg.get("interrompida") else None)
    return {"ok": bool(reg.get("ok")), "em": reg.get("fim") or reg.get("inicio"), "duracao_s": reg.get("duracao_s"), "estado": estado}


def _resultado_rotina(r):
    if not r:
        return None
    estado = ("pulada: já estava rodando" if r.get("pulada") else
              f"pedido {r['pendente']} enviado ao root, sem resultado ainda" if r.get("pendente") else None)
    return {"ok": bool(r["ok"]), "em": r["em"], "duracao_s": r["duracao_s"], "estado": estado}


def _form(csrf, acao, nome, tipo, rotulo, *, botao="neutro", extra=""):
    return (f'<form class="form" method="post" action="/config/rotinas">{_csrf(csrf)}'
            f'<input type="hidden" name="acao" value="{e(acao)}"><input type="hidden" name="nome" value="{e(nome)}">'
            f'<input type="hidden" name="tipo" value="{e(tipo)}">{extra}{ui.botao(rotulo, tipo=botao)}</form>')


def _celula_horario(expr, fuso, agora, ligada):
    nome_fuso = "Brasília" if fuso is BRT else "UTC"
    if expr is None or expr == "manual":
        return '<span class="mu">só a pedido</span>'
    try:
        prox = f"próxima: {rotinas.rotulo_proxima(rotinas.proxima(expr, agora, fuso), agora.astimezone(fuso))}"
    except ValueError:
        return f'<code class="mono">{e(expr)}</code><br><small class="mu">horário inválido</small>'
    return (f'<code class="mono">{e(expr)}</code> <small class="mu">{e(nome_fuso)}</small><br>'
            f'<small class="mu">{e(prox) if ligada else "desligada: não dispara sozinha"}</small>')


def _linha(csrf, edita, *, nome, tipo, origem, descricao, expr, fuso, ligada, resultado, link, agora, pode_ligar=True, edita_href=None):
    nome_html = f'<b class="mono">{e(nome)}</b>' + (f'<br><small class="mu">{e(descricao)}</small>' if descricao else "")
    estado = ui.selo("ligada", "ok") if ligada else ui.selo("desligada", "alerta")
    acoes = ""
    if edita:
        if pode_ligar:
            acoes += _form(csrf, "desligar" if ligada else "ligar", nome, tipo, "Desligar" if ligada else "Ligar")
        acoes += _form(csrf, "rodar", nome, tipo, "Rodar agora", botao="prim")
        if edita_href:
            acoes += ui.botao("Editar", href=edita_href)
    return [ui.td(nome_html, html_=True), ui.td(origem), ui.td(estado, html_=True),
            ui.td(_celula_horario(expr, fuso, agora, ligada), html_=True),
            ui.td(_celula_resultado(resultado, link), html_=True), ui.td(acoes, html_=True)]


CAB = ["Nome", "Origem", "Estado", "Horário", "Último resultado", ""]


ORIGEM = {"repo": "repositório", "painel": "painel", "sobrescrita": "painel (sobrescreve o repositório)"}


def _bloco_tarefas(csrf, edita, agora, base):
    fora, usos = rotinas.desligadas(), _ultimos_usos(base, agora)
    linhas = []
    for x in _entradas():
        nome, t, erro = x["nome"], x["tarefa"], x["erro"]
        origem = f"tarefa do Claude · {ORIGEM[x['origem']]}"
        editar = (ui.botao("Editar", href=f"/config/rotinas?editar={_q(nome)}") if edita and RE_NOME.fullmatch(nome) else "")
        if edita and x["origem"] == "sobrescrita":
            editar += _form(csrf, "restaurar", nome, "tarefa", "Voltar ao do repositório")
        if t is None:
            motivo = "nome inválido (só minúsculas, números e hífen): renomeie o arquivo" if not RE_NOME.fullmatch(nome) else erro
            linhas.append([ui.td(f'<b class="mono">{e(nome)}</b>', html_=True), ui.td(origem), ui.td(ui.selo("com erro", "ruim"), html_=True),
                           ui.td(f'<small class="mu">{e(motivo)}</small>', html_=True), "", ui.td(editar, html_=True)])
            continue
        reg = usos.get(nome)
        link = f"/config/rotinas/log?tipo=tarefa&nome={_q(nome)}" if reg and reg.get("saida") else None
        descricao = t.descricao + (f" (a cópia do painel está com erro e foi ignorada: {erro.splitlines()[0]})" if erro else "")
        linhas.append(_linha(csrf, edita, nome=nome, tipo="tarefa", origem=origem, descricao=descricao, expr=t.quando, fuso=BRT,
                             ligada=rotinas.chave_tarefa(nome) not in fora, resultado=_resultado_tarefa(reg), link=link, agora=agora,
                             pode_ligar=t.quando != "manual", edita_href=f"/config/rotinas?editar={_q(nome)}"))
    return linhas


def _bloco_rotinas(csrf, edita, agora, base):
    fora, linhas = rotinas.desligadas(), []
    for i in rotinas.catalogo(todas=True):
        ult = rotinas.ultimo(i, base)
        origem_r = ult.get("origem") if ult else None
        fonte = {"manual": "&fonte=manual", "raiz": "&fonte=raiz"}.get(origem_r, "")
        link = (f"/config/rotinas/log?tipo=rotina&nome={_q(i['nome'])}{fonte}"
                if (origem_r == "manual" or (origem_r == "raiz" and not ult.get("pendente")) or (i.get("log") and origem_r not in ("manual", "raiz"))) else None)
        origem = "rotina do sistema" if i["fonte"] == "sistema" else "rotina de plugin"
        linhas.append(_linha(csrf, edita, nome=i["nome"], tipo="rotina", origem=origem, descricao="", expr=i["horario"], fuso=UTC,
                             ligada=i["nome"] not in fora, resultado=_resultado_rotina(ult), link=link, agora=agora,
                             pode_ligar=i["horario"] is not None))
    return linhas


def _metas_card():
    try:
        ativas = sum(1 for m in metas.listar() if m.get("estado") == "ativa")
    except OSError:
        ativas = 0
    corpo = (f'<p>{ui.num(ativas)} meta(s) ativa(s). Metas se criam, medem e se encerram sozinhas pelas rodadas: não têm liga/desliga, '
             f'só o estado (ativa, batida, travada).</p><p>{ui.botao("Abrir as Metas", href="/metas")}</p>')
    return ui.card("Metas", corpo)


def rotinas_html(csrf, usuario, agora=None, base=None):
    edita = _pode(usuario)
    agora = agora or conta.agora()
    tar = _bloco_tarefas(csrf, edita, agora, base)
    rot = _bloco_rotinas(csrf, edita, agora, base)
    novo = f'<p>{ui.botao("Nova tarefa", tipo="prim", href="/config/rotinas?novo=1")}</p>' if _pode(usuario) else ""
    corpo = ui.card("Tarefas do Claude", novo + ui.tabela(CAB, tar, titulo=0, largas=(3, 4)) if tar else ui.vazio("Nenhuma tarefa na pasta de tarefas.", "Veja caminhos.tarefas."),
                    conta=len(tar), caixa=False)
    if rot:
        corpo += ui.card("Rotinas do sistema e de plugins", ui.tabela(CAB, rot, titulo=0, largas=(3, 4)), conta=len(rot), caixa=False)
    else:
        corpo += ui.card("Rotinas do sistema e de plugins", ui.vazio("Nenhuma rotina configurada.", "Aponte rotinas.cron para um crontab do sistema ou ligue um plugin com rotinas."))
    corpo += ('<p class="mu">Horários: tarefas do Claude em horário de Brasília; rotinas em UTC (Brasília = UTC menos 3 h). '
              'Desligada não dispara sozinha, mas "Rodar agora" continua valendo. Tarefa roda pela fila do Claude (entra na vez, no próximo '
              'minuto do tick); rotina roda na hora, a partir do painel.</p>')
    return corpo + _metas_card()


def _erro_nome(nome, existe=True):
    """Erro para o nome vindo da tela, ou None. existe=True: precisa ser tarefa conhecida (repo ou painel); False: nome livre."""
    if not RE_NOME.fullmatch(nome or ""):
        return "Nome de tarefa inválido: só minúsculas, números e hífen."
    achada = any(n == nome for n, _, _ in _lista_tarefas())
    if existe and not achada:
        return "Essa tarefa não existe."
    if not existe and achada:
        return "Já existe uma tarefa com esse nome: use Editar."
    return None


def _valores(t):
    v = {"quando": t.quando, "prioridade": str(t.prioridade), "modelo": t.modelo, "timeout": f"{t.timeout_s // 60}m" if t.timeout_s % 60 == 0 else f"{t.timeout_s}s",
         "repos": ", ".join(t.repos), "externo": "true" if t.externo else "false", "descricao": t.descricao,
         "pauta": " ".join(t.pauta) if t.pauta else "", "avisar": "true" if t.avisar else "false", "skills": ", ".join(t.skills),
         "conta": t.conta or "", "corpo": t.prompt}
    return v


def _gravavel(pasta):
    """A pasta (ou o ancestral mais próximo que existe, se ela ainda não foi criada) aceita escrita?"""
    p = pasta
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.access(p, os.W_OK)


def _aviso_pasta(origem="repo"):
    """(nivel, texto): leitura = a camada do painel não aceita escrita; senão explica de onde a tarefa vem."""
    if not _gravavel(_pasta()):
        return ("leitura", "A pasta de dados do painel não aceita escrita para este usuário, então não dá para guardar tarefas daqui. "
                "Veja as permissões de caminhos.dados. Nada foi alterado.")
    if origem == "repo":
        return ("ok", "Esta tarefa vem do repositório. Guardar cria uma cópia do painel que a sobrescreve; o arquivo do repositório não é tocado, "
                      "e \"Voltar ao do repositório\" desfaz.")
    if origem == "sobrescrita":
        return ("ok", "Esta tarefa tem uma cópia do painel que sobrescreve a do repositório.")
    if origem == "painel":
        return ("ok", "Esta tarefa existe só no painel (não há uma igual no repositório).")
    return ("ok", "")


def _campo(nome, rotulo, dica, v, erros):
    erro = f'<p class="mu" role="alert">{ui.selo(erros[nome], "ruim")}</p>' if nome in erros else ""
    if nome in OPCOES:
        rot = DICA_OPCAO.get(nome, {})
        ops = "".join(f'<option value="{e(o)}"{" selected" if v.get(nome, "") == o else ""}>{e(rot.get(o, o) or "—")}</option>' for o in OPCOES[nome])
        entrada = f'<select name="{nome}">{ops}</select>'
    else:
        entrada = (f'<input name="{nome}" value="{e(v.get(nome, ""))}" placeholder="{e(dica or "")}" autocomplete="off" spellcheck="false" maxlength="400">')
    return f'<label class="campo"><span class="mu">{e(rotulo)}</span>{entrada}{erro}</label>'


def _formulario(csrf, nome, v, erros, geral="", origem="repo", criar=False):
    nivel, aviso = _aviso_pasta("novo" if criar else origem)
    campos = "".join(_campo(n, r, d, v, erros) for n, r, d in CAMPOS)
    erro_corpo = f'<p class="mu" role="alert">{ui.selo(erros["corpo"], "ruim")}</p>' if "corpo" in erros else ""
    corpo = (f'<label class="campo cheio"><span class="mu">Corpo (o prompt, em português)</span>'
             f'<textarea name="corpo" rows="16" spellcheck="false">{e(v.get("corpo", ""))}</textarea>{erro_corpo}</label>')
    botoes = (ui.botao("Guardar tarefa", tipo="prim") if nivel != "leitura" else "") + " " + ui.botao("Cancelar", href="/config/rotinas")
    nota = f'<p class="mu" role="status">{e(aviso)}</p>' if aviso else ""
    erro_nome = f'<p class="mu" role="alert">{ui.selo(erros["nome"], "ruim")}</p>' if "nome" in erros else ""
    campo_nome = (f'<label class="campo"><span class="mu">Nome da tarefa (minúsculas, números e hífen)</span>'
                  f'<input name="nome" value="{e(nome)}" autocomplete="off" spellcheck="false" maxlength="60" required>{erro_nome}</label>' if criar else
                  f'<input type="hidden" name="nome" value="{e(nome)}"><label class="campo"><span class="mu">Nome da tarefa</span>'
                  f'<input value="{e(nome)}" readonly></label>')
    form = (f'{geral}{nota}<form class="form" method="post" action="/config/rotinas">{_csrf(csrf)}'
            f'<input type="hidden" name="acao" value="salvar"><input type="hidden" name="modo" value="{"criar" if criar else "editar"}">'
            f'{campo_nome}{campos}{corpo}{botoes}</form>')
    if not criar and origem in ("sobrescrita", "painel"):
        rotulo = "Voltar ao do repositório" if origem == "sobrescrita" else "Remover do painel (arquiva)"
        form += _form(csrf, "restaurar", nome, "tarefa", rotulo, botao="perigo")
    return form + '<p class="mu">O contrato dos campos está em tarefas/README.md. Comentários do cabeçalho do arquivo não são mantidos ao guardar.</p>'


def _pagina(corpo, sub=None):
    return ui.pagina("Rotinas e Metas", corpo, "/config/rotinas", olho="Configurar",
                     sub=sub or "Tarefas do Claude e rotinas do sistema: liga, desliga, horário, último resultado e rodar agora.")


def rotinas_get(q, usuario, csrf):
    edita = _pode(usuario)
    if edita and q.get("novo"):
        v = {"quando": "manual", "prioridade": "2", "modelo": "sonnet", "timeout": "15m", "externo": "false", "avisar": "false"}
        return 200, _pagina(_formulario(csrf, "", v, {}, criar=True), sub="Tarefa nova (fica na camada do painel).")
    if edita and q.get("editar") is not None:
        nome = q["editar"]
        erro = _erro_nome(nome)
        x = None
        if not erro:
            x = next(x for x in _entradas() if x["nome"] == nome)
            if x["tarefa"] is None:
                erro = "O arquivo desta tarefa está com erro: corrija direto no arquivo" + (
                    " ou volte ao do repositório pela lista." if x["origem"] == "sobrescrita" else ".")
        if erro:
            return 404, _pagina(ui.vazio(erro, "Volte à lista."))
        return 200, _pagina(_formulario(csrf, nome, _valores(x["tarefa"]), {}, origem=x["origem"]), sub=f"Editando {e(nome)}.")
    return 200, _pagina(rotinas_html(csrf, usuario))


# ---- log ----

def _cauda(caminho):
    with open(caminho, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - MAX_LOG))
        return f.read().decode("utf-8", "replace")


def log_get(q, usuario, csrf):
    nome, tipo = q.get("nome", ""), q.get("tipo", "")
    arq = None
    if tipo == "rotina":
        item = next((i for i in rotinas.catalogo(todas=True) if i["nome"] == nome), None)
        if item and q.get("fonte") == "raiz":
            r = rotinas._raiz(nome)  # o texto do resultado do root (últimos 1500 caracteres), não um arquivo
            reg = raiz.resultado(r["pedido"]) if r and r.get("pedido") else None
            texto = reg.get("saida") if reg else None
            if reg and not texto and item.get("log"):  # a linha do cron redirecionou a saída para um arquivo: o log de verdade
                arq = Path(item["log"]).expanduser()
            elif texto is None:
                return 404, _pagina(ui.vazio("Esse resultado ainda não existe.", "Volte à lista."), sub="Log.")
            else:
                return 200, _pagina(f'<p class="mu">Fim da saída devolvida pelo root.</p><pre class="mono">{e(str(texto))}</pre>'
                                    f'<p>{ui.botao("Voltar", href="/config/rotinas")}</p>', sub=f"Resultado do root: {e(nome)}.")
        if item and q.get("fonte") == "manual":
            arq = rotinas.log_manual(nome)  # nome já validado pelo catálogo
        else:
            arq = item and item.get("log") and Path(item["log"]).expanduser()
    elif tipo == "tarefa" and RE_NOME.fullmatch(nome):
        reg = _ultimos_usos().get(nome)
        base = (agenda.HOME / "saida" / nome).resolve()
        if reg and reg.get("saida"):
            cand = Path(reg["saida"]).resolve()
            arq = cand if cand.is_relative_to(base) else None
    try:
        texto = _cauda(arq) if arq else None
    except OSError:
        texto = None
    if texto is None:
        return 404, _pagina(ui.vazio("Esse log não existe ou não pode ser lido.", "Volte à lista."), sub="Log.")
    corpo = (f'<p class="mu">Fim do arquivo (até {MAX_LOG // 1000} mil caracteres).</p><pre class="mono">{e(texto)}</pre>'
             f'<p>{ui.botao("Voltar", href="/config/rotinas")}</p>')
    return 200, _pagina(corpo, sub=f"Log de {e(nome)}.")


# ---- POST ----

def _conhecidos():
    return {n for n, _, _ in _lista_tarefas()}, {i["nome"] for i in rotinas.catalogo(todas=True)}


def _gravar(valores, ok):
    try:
        config.gravar(valores)
    except OSError:
        return False, "Não consegui gravar a configuração agora. Veja as permissões da pasta de dados."
    except config.ConfigError:
        return False, "Valor recusado pela configuração."
    return True, ok


def _chave(tipo, nome):
    tars, rots = _conhecidos()
    if tipo == "tarefa" and nome in tars:
        return rotinas.chave_tarefa(nome)
    if tipo == "rotina" and nome in rots:
        return nome
    return None


def _ligar(c, ligar):
    nome = c.get("nome") or ""
    chave = _chave(c.get("tipo") or "", nome)
    if chave is None:
        return False, "Rotina ou tarefa desconhecida."

    def novos():
        atual = [x for x in (config.get("rotinas.desligadas") or []) if isinstance(x, str) and x != chave]
        return {"rotinas.desligadas": sorted(atual if ligar else atual + [chave])}
    return _gravar(novos, f"{nome} ligada." if ligar else f"{nome} desligada: não dispara sozinha (Rodar agora continua valendo).")


def _rodar(c, usuario=""):
    nome, tipo = c.get("nome") or "", c.get("tipo") or ""
    if _chave(tipo, nome) is None:
        return False, "Rotina ou tarefa desconhecida."
    if tipo == "rotina":
        item = next(i for i in rotinas.catalogo(todas=True) if i["nome"] == nome)
        if item["fonte"] == "sistema":  # lock em /var/lock e ambiente do root: pelo canal do hub (hub.raiz), não como usuário do painel
            try:
                pid = raiz.pedir("rodar-rotina-agora", nome, "painel: rodar agora", f"painel:{usuario}"[:80])
                rotinas.registrar_pedido(nome, pid)
            except (ValueError, OSError) as ex:
                return False, f"Não consegui pedir ao root: {ex}"
            return True, f"Pedido {pid} enviado ao root: o resultado (ok, erro ou pulada) aparece em Último resultado ao recarregar."
        try:
            rotinas.disparar_capturado(item)
        except OSError as ex:
            return False, f"Não consegui iniciar a rotina agora: {ex}"
        return True, (f"{nome} rodando em segundo plano: quando terminar, o resultado real (ok ou erro, com o log) aparece em Último resultado. "
                      "A trava da rotina impede duas ao mesmo tempo.")
    t = next((x for n, x, _ in _lista_tarefas() if n == nome), None)
    if t is None:
        return False, "O arquivo desta tarefa está com erro: corrija antes de rodar."
    try:
        entrou = agenda.enfileirar(t, "manual (painel)")
    except OSError:
        return False, "Não consegui colocar na fila agora. Veja a pasta de dados."
    return True, f"{nome} na fila: roda na vez, no próximo tick." if entrou else f"{nome} já estava na fila."


_RE_CAMPO = re.compile(r"'(\w+)'")


def _erros_por_campo(ex, nome):
    """Mensagens da TarefaInvalida -> ({campo: texto}, [gerais]) em português, sem o prefixo do arquivo."""
    erros, gerais = {}, []
    for linha in str(ex).splitlines():
        msg = re.sub(rf"^{re.escape(nome)}\.md(:\d+)?: ", "", linha)
        m = _RE_CAMPO.search(msg)
        campo = m.group(1) if m and m.group(1) in {c[0] for c in CAMPOS} else next(
            (c for c, chave in (("timeout", "timeout"), ("repos", "repo "), ("skills", "skill "), ("corpo", "corpo")) if chave in msg), None)
        if campo:
            erros.setdefault(campo, msg)
        else:
            gerais.append(msg)
    return erros, gerais


def _montar(c):
    """Formulário -> texto do .md. Campo de uma linha com quebra de linha vira erro no campo (não deixa injetar chave)."""
    erros, linhas = {}, []
    for chave, _, _ in CAMPOS:
        v = (c.get(chave) or "").strip()
        if "\n" in v or "\r" in v:
            erros[chave] = "Escreva numa linha só."
        elif v or chave in T.OBRIGATORIAS:
            linhas.append(f"{chave}: {v}")
    corpo = (c.get("corpo") or "").replace("\r\n", "\n").strip()
    return erros, "---\n" + "\n".join(linhas) + "\n---\n" + corpo + "\n"


def _escrever(pasta, nome, texto):
    destino = pasta / f"{nome}.md"
    try:
        modo = destino.stat().st_mode & 0o777
    except OSError:
        modo = 0o644
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix=f".{nome}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, modo)
        os.replace(tmp, destino)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _salvar(c):
    nome = c.get("nome") or ""
    criar = c.get("modo") == "criar"
    erro = _erro_nome(nome, existe=not criar)
    if erro and not criar:
        return False, erro
    csrf = c.get("csrf", "")
    valores = {k: (c.get(k) or "") for k, _, _ in CAMPOS} | {"corpo": c.get("corpo") or ""}
    erros, texto = _montar(c)
    gerais = []
    if erro:
        erros["nome"] = erro
    elif not erros:
        try:
            T.validar(texto, nome)
        except T.TarefaInvalida as ex:
            erros, gerais = _erros_por_campo(ex, nome)
    if erros or gerais:
        banner = (f'<p role="alert">{ui.selo("Não gravei. Corrija os campos marcados.", "ruim")}</p>'
                  + "".join(f'<p class="mu" role="alert">{e(g)}</p>' for g in gerais))
        origem = next((x["origem"] for x in _entradas() if x["nome"] == nome), "repo")
        return False, "Há campos com erro.", _pagina(_formulario(csrf, nome, valores, erros, banner, origem=origem, criar=criar),
                                                      sub="Tarefa nova." if criar else f"Editando {e(nome)}.")
    if _aviso_pasta()[0] == "leitura":
        return False, "A pasta de dados do painel não aceita escrita; nada foi gravado."
    try:
        _pasta().mkdir(parents=True, exist_ok=True)
        _escrever(_pasta(), nome, texto)
    except OSError:
        return False, "Não consegui gravar a tarefa agora. Veja as permissões da pasta de dados."
    return True, f"Tarefa {nome} guardada no painel."


def _restaurar(c):
    """Tira a cópia do painel (arquiva em <dados>/tarefas/arquivo/): a do repositório volta a valer; tarefa só do painel some."""
    nome = c.get("nome") or ""
    x = next((x for x in _entradas() if x["nome"] == nome), None)
    if not RE_NOME.fullmatch(nome) or x is None or x["origem"] == "repo":
        return False, "Essa tarefa não tem cópia do painel."
    arq = _pasta() / f"{nome}.md"
    destino = _pasta() / "arquivo" / f"{conta.agora():%Y-%m-%d-%H%M%S}-{nome}.md"
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        os.replace(arq, destino)
    except OSError:
        return False, "Não consegui arquivar a cópia do painel agora. Veja as permissões da pasta de dados."
    return True, (f"{nome}: voltou ao do repositório (a cópia do painel foi arquivada)." if x["origem"] == "sobrescrita"
                  else f"{nome} removida do painel (arquivada).")


def rotinas_post(campos, usuario, ip):
    acao = campos.get("acao", "")
    if acao == "ligar":
        return _ligar(campos, True)
    if acao == "desligar":
        return _ligar(campos, False)
    if acao == "rodar":
        return _rodar(campos, usuario)
    if acao == "salvar":
        return _salvar(campos)
    if acao == "restaurar":
        return _restaurar(campos)
    return False, "Ação desconhecida."
