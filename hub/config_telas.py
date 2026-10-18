"""Telas de configuração do painel (grupo Configurar do menu). Funções puras: o painel só repassa a requisição.

Registro de rotas (as próximas telas só acrescentam entradas):
- `ROTAS_GET[caminho](q, usuario, csrf) -> (status, html)`; q = query já em dict.
- `ROTAS_POST[caminho](campos, usuario, ip) -> (ok, mensagem)` ou (False, mensagem, html) para devolver o formulário com o erro no campo; o painel chama `tratar_post`, que confere CSRF e permissão
  antes de entrar na rota e devolve (status, destino do redirecionamento com ?ok=/?erro=), ou (200, html) quando a rota devolve a página.
Nenhuma tela mostra, registra nem devolve em erro o valor de um segredo.
"""
import copy
import html
import json
import urllib.parse

from hub import agentes_tela, cofre, conexoes_tela, plugins_tela, rotinas_tela, sites, ui, verificar_fontes

e = html.escape
MASCARA = "••••"


def pode(usuario, acao):
    """Ponto único de permissão das telas. `usuario` é o e-mail da sessão; o painel só chega aqui com o papel dono
    (GET e POST de /config/... pedem a permissão "config", hub/usuarios.permissao_get), então quem chega edita."""
    return True


def _abas(atual):
    return ui.filtro("aba", [("segredos", "Segredos"), ("auditoria", "Auditoria")], atual,
                     url=lambda k: "/config/cofre" + ("?aba=auditoria" if k == "auditoria" else ""), abas=True, select=False)


def _campo_csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _senha(rotulo="Valor"):
    return (f'<input type="password" name="valor" autocomplete="off" required placeholder="{e(rotulo)}" '
            f'aria-label="{e(rotulo)}" spellcheck="false">')


def _id(nome):
    return "d-" + "".join(c if c.isalnum() else "-" for c in nome)


def _linha(s, csrf, edita):
    nome = s["nome"]
    acoes = ""
    if edita:
        troca, apaga = _id(nome) + "-t", _id(nome) + "-a"
        acoes = (ui.botao("Trocar", abre=troca) + " " + ui.botao("Apagar", tipo="perigo", abre=apaga)
                 + ui.dialogo(troca, f"Trocar {nome}",
                              f'<form class="form" method="post" action="/config/cofre">{_campo_csrf(csrf)}'
                              f'<input type="hidden" name="acao" value="definir"><input type="hidden" name="nome" value="{e(nome)}">'
                              f'{_senha("Novo valor")}{ui.botao("Trocar", tipo="prim")}</form>')
                 + ui.dialogo(apaga, f"Apagar {nome}?",
                              f'<p class="mu">O segredo some do cofre e quem usa {e(nome)} deixa de achá-lo. Não dá para desfazer.</p>'
                              f'<form class="form" method="post" action="/config/cofre">{_campo_csrf(csrf)}'
                              f'<input type="hidden" name="acao" value="apagar"><input type="hidden" name="nome" value="{e(nome)}">'
                              f'{ui.botao("Apagar de vez", tipo="perigo")}</form>'))
    return [ui.td(nome, classe="mono"), f"{ui.quando(s['definido_em'])} por {s['por']}",
            ui.td(MASCARA + s["fim"], classe="mono"), ui.td(acoes, html_=True)]


def _segredos(csrf, edita):
    itens = cofre.listar()
    tab = (ui.tabela(["Nome", "Definido", "Valor", ""], [_linha(s, csrf, edita) for s in itens]) if itens
           else ui.vazio("Nenhum segredo no cofre ainda.", "Grave o primeiro abaixo."))
    if not edita:
        return tab
    return (tab + '<h2>Novo segredo</h2>'
            f'<form class="form" method="post" action="/config/cofre">{_campo_csrf(csrf)}'
            '<input type="hidden" name="acao" value="definir">'
            '<input name="nome" required maxlength="100" placeholder="escopo.item (ex.: linear.token)" aria-label="Nome" '
            'autocomplete="off" spellcheck="false">'
            f'{_senha()}{ui.botao("Guardar", tipo="prim")}</form>'
            '<p class="mu">O valor é cifrado ao gravar e nunca volta para a tela: só aparecem os 4 últimos caracteres.</p>')


def _auditoria():
    linhas = [[a["quando"], a["quem"], a["acao"], ui.td(a["nome"], classe="mono"), a["ip"] or "—"] for a in cofre.auditoria(200)]
    if not linhas:
        return ui.vazio("Nada na auditoria ainda.")
    return ui.tabela(["Quando", "Quem", "Ação", "Nome", "IP"], linhas) + ('<p class="mu">Últimas 200 ações. O nome em "Quem" é o usuário informado no acesso; '
                                                'login por pessoa chega na frente 4.</p>')


def cofre_html(aba, csrf, usuario):
    """Corpo da página (HTML). Texto de fora sai escapado por ui.* ou por e()."""
    edita = pode(usuario, "cofre.escrever")
    return _abas(aba) + (_auditoria() if aba == "auditoria" else _segredos(csrf, edita))


def cofre_get(q, usuario, csrf):
    aba = "auditoria" if q.get("aba") == "auditoria" else "segredos"
    try:
        corpo = cofre_html(aba, csrf, usuario)
    except cofre.CofreError as ex:
        corpo = ui.vazio("O cofre não abriu.", str(ex))
    except OSError:
        corpo = ui.vazio("O cofre não abriu.", "Erro de arquivo; confira as permissões da pasta do cofre.")
    return 200, ui.pagina("Cofre", corpo, "/config/cofre", olho="Configurar",
                          sub=f"Segredos cifrados: tokens e chaves que o {e(ui.NOME_PRODUTO)} usa. O valor nunca é exibido.")


def cofre_post(campos, usuario, ip):
    """(ok, mensagem em português). Nunca devolve o valor, nem em erro."""
    acao, nome = campos.get("acao", ""), (campos.get("nome") or "").strip()
    try:
        if acao == "definir":
            cofre.definir(nome, campos.get("valor", ""), por=usuario, ip=ip)
            return True, f"{nome} guardado."
        if acao == "apagar":
            if cofre.apagar(nome, por=usuario, ip=ip):
                return True, f"{nome} apagado."
            return False, f"{nome} não existe no cofre."
        return False, "Ação desconhecida."
    except cofre.CofreError as ex:
        return False, str(ex)
    except Exception:  # noqa: BLE001  (a mensagem de uma exceção qualquer pode carregar o valor: não repassa)
        return False, "Não consegui gravar no cofre agora. Veja a pasta do cofre e tente de novo."


# ---------------- Projetos (/config/projetos) ----------------
CLIENTES = {}  # {fonte: cliente} injetável (testes); vazio = cliente real de hub/verificar_fontes.py
PACOTES = ("npm", "yarn", "pnpm", "bun")
CONSELHOS = ("",) + sites.CONSELHOS
# (nome no formulário, rótulo, dica)
_TEXTO = (("nome", "Nome", ""), ("id", "Id (minúsculas, números e hífen)", "loja-exemplo"), ("dominio", "Domínio (só o host)", "exemplo-loja.test"),
          ("repo_github", "Repositório (dono/nome)", "dono/exemplo-loja"), ("repo_base", "Branch base", "main"),
          ("repo_deploy", "Deploy", "dokploy"), ("gsc", "Search Console (propriedade)", "sc-domain:exemplo-loja.test"),
          ("ga4", "GA4 (ID da propriedade)", "123456789"), ("ga4_medicao", "GA4 (ID de medição)", "G-XXXXXXXXXX"),
          ("clarity", "Clarity (ID do projeto)", ""), ("gmb_cid", "Google Meu Negócio (CID, ou sem_perfil)", ""),
          ("marca_nome", "Marca (vazio = o nome)", ""))
_IDS = ("gsc", "ga4", "ga4_medicao", "clarity", "gmb_cid")
# campo do validar() -> campo do formulário
_CAMPO_FORM = {"nome": "nome", "id": "id", "dominio": "dominio", "repo.github": "repo_github", "repo.base": "repo_base",
               "repo.pacotes": "repo_pacotes", "ids.gsc": "gsc", "ids.ga4": "ga4", "ids.ga4_medicao": "ga4_medicao",
               "ids.clarity": "clarity", "ids.gmb_cid": "gmb_cid", "conselho": "conselho", "marca.nome": "marca_nome",
               "marca.aliases": "aliases", "concorrentes": "concorrentes", "fichas_concorrentes": "fichas", "contexto": "contexto", "ativo": "ativo"}


def _lista(texto):
    return [x.strip() for x in (texto or "").splitlines() if x.strip()]


def _padrao_site(sid):
    return {"id": sid, "ids_antigos": [], "ativo": True, "nome": "", "dominio": "", "hosts_alternativos": [], "hosts_citacao": [],
            "repo": None, "ids": {}, "marca": {"nome": "", "aliases": [], "identificador": None, "nome_generico": False},
            "conselho": None, "contexto": "", "frase_canonica": None, "meta": None, "concorrentes": [], "monitor": None,
            "padrao": {"fora_do_google": False, "noindex_permitido": [], "treino_ia": None, "tipo_negocio": None,
                       "paginas_amostra": [], "indexnow": None}, "ranking": None}


def _mesmo_texto(novo, antigo):
    """Navegador devolve \r\n e pode aparar espaço: se só isso mudou, vale o texto original (edição sem mudança = JSON igual)."""
    return isinstance(antigo, str) and novo.replace("\r\n", "\n").strip() == antigo.replace("\r\n", "\n").strip()


def _montar(campos, existente=None):
    """Formulário -> dict de site. Parte do cadastro existente (merge): o que o formulário não cobre fica como estava, e o
    que o formulário cobre mas não mudou mantém a forma original (null continua null)."""
    c = {k: (v or "") if k in ("concorrentes", "aliases", "contexto", "fichas") else (v or "").strip() for k, v in campos.items()}
    s = copy.deepcopy(existente) if existente else _padrao_site(c.get("id", ""))
    s["id"] = existente["id"] if existente else c.get("id", "")
    s["nome"], s["dominio"], s["ativo"] = c.get("nome", ""), c.get("dominio", ""), bool(c.get("ativo"))
    if not _mesmo_texto(c.get("contexto", ""), s["contexto"]):
        s["contexto"] = c.get("contexto", "").strip()
    novos = _lista(c.get("concorrentes"))
    if novos != s["concorrentes"]:
        s["concorrentes"] = novos
    fichas = _lista(c.get("fichas"))
    if fichas != (s.get("fichas_concorrentes") or []):
        s["fichas_concorrentes"] = fichas  # [] fica gravado: é o que diz ao hub "não semear de novo"
    s["conselho"] = c.get("conselho") or None
    antigo = s["repo"]
    campos_repo = {k: c.get(f"repo_{k}", "") for k in ("github", "base", "pacotes", "deploy")}
    if not any(campos_repo.values()):
        s["repo"] = None
    else:
        repo = dict(antigo or {})
        for k in ("github", "base", "pacotes"):
            if campos_repo[k]:
                repo[k] = campos_repo[k]
            else:
                repo.pop(k, None)  # campo limpo: a chave sai e o validar reclama no campo (nada de ignorar em silêncio)
        if campos_repo["deploy"]:
            repo["deploy"] = campos_repo["deploy"]
        elif repo.get("deploy") is not None:
            repo.pop("deploy", None)  # null existente continua null
        s["repo"] = repo
    for k in _IDS:
        if c.get(k):
            s["ids"][k] = c[k]
        elif s["ids"].get(k) is not None:
            s["ids"].pop(k)  # só some o que tinha valor; `null` existente fica `null`
    marca = dict(s["marca"])
    marca["nome"] = c.get("marca_nome") or c.get("nome", "")
    aliases = _lista(c.get("aliases"))
    if aliases != marca.get("aliases"):
        marca["aliases"] = aliases
    s["marca"] = marca
    return s


def _valores(s):
    """Site -> valores do formulário."""
    r, i, m = s.get("repo") or {}, s.get("ids") or {}, s.get("marca") or {}
    v = {"nome": s["nome"], "id": s["id"], "dominio": s["dominio"], "repo_github": r.get("github", ""), "repo_base": r.get("base", ""),
         "repo_pacotes": r.get("pacotes", ""), "repo_deploy": r.get("deploy", ""), "conselho": s.get("conselho") or "",
         "marca_nome": "" if m.get("nome") == s["nome"] else m.get("nome", ""), "aliases": "\n".join(m.get("aliases") or []),
         "concorrentes": "\n".join(s.get("concorrentes") or []), "fichas": "\n".join(s.get("fichas_concorrentes") or []), "contexto": s.get("contexto", ""), "ativo": "1" if s.get("ativo") else ""}
    return v | {k: i.get(k, "") for k in _IDS}


def _erro_campo(erros, nome):
    return f'<p class="mu" role="alert">{ui.selo(erros[nome], "ruim")}</p>' if nome in erros else ""


def _campo_texto(nome, rotulo, dica, v, erros, somente_leitura=False, obrigatorio=False):
    return (f'<label class="campo"><span class="mu">{e(rotulo)}</span>'
            f'<input name="{nome}" value="{e(v.get(nome, ""))}" placeholder="{e(dica)}" autocomplete="off" spellcheck="false" maxlength="300"'
            f'{" readonly" if somente_leitura else ""}{" required" if obrigatorio else ""}>{_erro_campo(erros, nome)}</label>')


def _select(nome, rotulo, opcoes, v, erros):
    ops = "".join(f'<option value="{e(o)}"{" selected" if v.get(nome, "") == o else ""}>{e(o or "—")}</option>' for o in opcoes)
    return f'<label class="campo"><span class="mu">{e(rotulo)}</span><select name="{nome}">{ops}</select>{_erro_campo(erros, nome)}</label>'


def _area(nome, rotulo, v, erros, linhas=4):
    return (f'<label class="campo cheio"><span class="mu">{e(rotulo)}</span>'
            f'<textarea name="{nome}" rows="{linhas}" spellcheck="false">{e(v.get(nome, ""))}</textarea>{_erro_campo(erros, nome)}</label>')


def _formulario(csrf, v, erros, criar, geral=""):
    t = {n: (r, d) for n, r, d in _TEXTO}
    txt = lambda n, **k: _campo_texto(n, *t[n], v, erros, **k)  # noqa: E731
    pacotes = _select("repo_pacotes", "Gerenciador de pacotes", ("",) + PACOTES, v, erros)
    ativo = (f'<label class="campo cheio"><span><input type="checkbox" name="ativo" value="1"{" checked" if v.get("ativo") else ""}> '
             f'Ativo (as rotinas e o monitor rodam nele)</span>{_erro_campo(erros, "ativo")}</label>')
    return (f'{geral}<form class="form" method="post" action="/config/projetos">{_campo_csrf(csrf)}'
            f'<input type="hidden" name="acao" value="salvar"><input type="hidden" name="modo" value="{"criar" if criar else "editar"}">'
            + txt("nome", obrigatorio=True) + txt("id", somente_leitura=not criar, obrigatorio=True) + txt("dominio", obrigatorio=True)
            + txt("repo_github") + txt("repo_base") + pacotes + txt("repo_deploy")
            + txt("gsc") + txt("ga4") + txt("ga4_medicao") + txt("clarity") + txt("gmb_cid")
            + _select("conselho", "Conselho profissional", CONSELHOS, v, erros)
            + txt("marca_nome") + _area("aliases", "Aliases da marca (um por linha)", v, erros, 3)
            + _area("concorrentes", "Concorrentes (um por linha)", v, erros, 4)
            + _area("fichas", "Fichas de concorrente (um por linha: Nome | dominio-com.example | x:perfil | linkedin:company/slug)", v, erros, 4)
            + _area("contexto", "Contexto (o que os agentes precisam saber do projeto)", v, erros, 8) + ativo
            + f'{ui.botao("Guardar projeto", tipo="prim")} {ui.botao("Cancelar", href="/config/projetos")}</form>'
            '<p class="mu">Campos que este formulário não cobre (monitor, ranking, hosts alternativos, IndexNow...) ficam como estão ao editar.</p>')


def _data_curta(iso):
    try:
        return f"{iso[8:10]}/{iso[5:7]}"
    except (TypeError, IndexError):
        return "?"


def _celula_fonte(site, fonte, rotulo, v, csrf, edita):
    """Selo da fonte. Verde só se o ID atual é o que foi verificado; ID que mudou volta a âmbar "não verificado"."""
    sid = site["id"]
    atual = verificar_fontes.id_da_fonte(site, fonte)
    msg, tem_botao = "", True
    if atual is None:
        selo, tem_botao = ui.selo("não configurado", "alerta"), False
    elif not v or v.get("id_verificado") != atual or v["status"] == "nao_configurado":
        selo = ui.selo("não verificado", "alerta")
    elif v["status"] == "ok":
        selo = ui.selo("verificado " + _data_curta(v.get("em")), "ok")
    elif v["status"] == "manual":
        selo = ui.selo("conferir à mão", "alerta")
    else:
        selo = ui.selo("erro", "ruim")
    if v and atual is not None and v.get("id_verificado") == atual and v["status"] != "nao_configurado":
        msg = f'<small class="mu">{e(str(v.get("mensagem", "")))}</small>'
    botao = ""
    if edita and tem_botao:
        botao = (f'<form class="form" method="post" action="/config/projetos">{_campo_csrf(csrf)}'
                 f'<input type="hidden" name="acao" value="verificar"><input type="hidden" name="id" value="{e(sid)}">'
                 f'<input type="hidden" name="fonte" value="{e(fonte)}">{ui.botao("Verificar")}</form>')
    return f'<div title="{e(rotulo)}">{selo}{msg}{botao}</div>'


def _cadastro():
    """[(site|None, arquivo, erros)] de sites/*.json, tolerante: arquivo com erro aparece na lista, não derruba a tela."""
    saida = []
    for arq in sorted(sites.PASTA.glob("*.json")):
        try:
            s, err = sites._ler(arq)
        except OSError as ex:
            s, err = None, [f"{arq.name}: {type(ex).__name__}"]
        saida.append((None if err else s, arq, err))
    return saida


def _lista_projetos(csrf, edita):
    cab = ["Projeto", *[r for _, r in verificar_fontes.FONTES], ""]
    linhas = []
    for s, arq, err in _cadastro():
        if s is None:
            linhas.append([ui.td(arq.stem, classe="mono"), ui.td(f'<span class="mu">{e("; ".join(err))}</span>', html_=True)]
                          + [""] * (len(cab) - 2))
            continue
        ver = verificar_fontes.ler(s["id"])
        nome = f'{e(s["nome"])}<br><small class="mu">{e(s["dominio"])}' + ("" if s["ativo"] else " · inativo") + "</small>"
        linhas.append([ui.td(nome, html_=True)]
                      + [ui.td(_celula_fonte(s, f, r, ver.get(f), csrf, edita), html_=True) for f, r in verificar_fontes.FONTES]
                      + [ui.td(ui.botao("Editar", href="/config/projetos?editar=" + urllib.parse.quote(s["id"])) if edita else "", html_=True)])
    corpo = (ui.tabela(cab, linhas) if linhas else ui.vazio("Nenhum projeto cadastrado ainda.", "Crie o primeiro com o botão acima."))
    return corpo + '<p class="mu">Verde: verificado na data. Âmbar: ainda não verificado. Vermelho: a última verificação falhou (a mensagem vem logo abaixo).</p>'


def _pagina_projetos(corpo, acoes="", sub=None):
    return ui.pagina("Projetos", corpo, "/config/projetos", olho="Configurar", acoes=acoes,
                     sub=sub or "Cada projeto e as fontes de dados dele. Verifique uma fonte para ver se o acesso funciona.")


def projetos_get(q, usuario, csrf):
    edita = pode(usuario, "projetos.escrever")
    if edita and q.get("novo"):
        return 200, _pagina_projetos(_formulario(csrf, {"ativo": "1"}, {}, True), sub="Projeto novo.")
    if edita and q.get("editar") is not None:
        sid = q["editar"]
        arq = sites.PASTA / f"{sid}.json" if sites.RE_ID.match(sid) else None
        s, err = sites._ler(arq) if arq and arq.is_file() else (None, ["falta"])
        if s is None or err:
            return 404, _pagina_projetos(ui.vazio("Esse projeto não existe ou o cadastro dele está com erro.", "Volte à lista."))
        return 200, _pagina_projetos(_formulario(csrf, _valores(s), {}, False), sub=f"Editando {e(s['nome'])}.")
    acoes = ui.botao("Novo projeto", tipo="prim", href="/config/projetos?novo=1") if edita else ""
    return 200, _pagina_projetos(_lista_projetos(csrf, edita), acoes)


def _salvar(campos):
    criar = campos.get("modo") == "criar"
    sid = (campos.get("id") or "").strip()
    existente = None
    if not criar:
        arq = sites.PASTA / f"{sid}.json" if sites.RE_ID.match(sid) else None
        existente, err = sites._ler(arq) if arq and arq.is_file() else (None, ["falta"])
        if existente is None or err:
            return False, "Esse projeto não existe ou o cadastro dele está com erro."
    s = _montar(campos, existente)
    try:
        sites.gravar(s, criar=criar)
    except sites.SiteInvalido as ex:
        erros, geral = {}, []
        for campo, msg in ex.campos.items():
            alvo = _CAMPO_FORM.get(campo)
            if alvo:
                erros[alvo] = msg
            else:
                geral.append(f"{campo or 'cadastro'}: {msg}")
        banner = f'<p role="alert">{ui.selo("Não gravei. Corrija os campos marcados.", "ruim")}</p>' + "".join(
            f'<p class="mu" role="alert">{e(g)}</p>' for g in geral)
        valores = {k: (campos.get(k) or "") for k in ("nome", "id", "dominio", "repo_github", "repo_base", "repo_pacotes", "repo_deploy",
                                                     "conselho", "marca_nome", "aliases", "concorrentes", "fichas", "contexto", "ativo", *_IDS)}
        if not criar:
            valores["id"] = sid
        return False, "Há campos com erro.", _pagina_projetos(_formulario(campos.get("csrf", ""), valores, erros, criar, banner),
                                                              sub="Projeto novo." if criar else f"Editando {e(s['nome'])}.")
    except OSError:
        return False, "Não consegui gravar o cadastro agora. Veja as permissões da pasta de projetos."
    return True, f"Projeto {s['nome']} guardado."


def _verificar(campos):
    sid, fonte = (campos.get("id") or "").strip(), campos.get("fonte", "")
    if fonte not in verificar_fontes.FUNCOES:
        return False, "Fonte desconhecida."
    arq = sites.PASTA / f"{sid}.json" if sites.RE_ID.match(sid) else None
    s, err = sites._ler(arq) if arq and arq.is_file() else (None, ["falta"])
    if s is None or err:
        return False, "Esse projeto não existe ou o cadastro dele está com erro."
    try:
        status, msg = verificar_fontes.verificar(s, fonte, cliente=CLIENTES.get(fonte))
    except OSError:
        return False, "Não consegui guardar o resultado da verificação. Veja a pasta de dados."
    return status in ("ok", "manual", "nao_configurado"), msg


def projetos_post(campos, usuario, ip):
    acao = campos.get("acao", "")
    if acao == "salvar":
        return _salvar(campos)
    if acao == "verificar":
        return _verificar(campos)
    return False, "Ação desconhecida."


ROTAS_GET = {"/config/cofre": cofre_get, "/config/projetos": projetos_get, "/config/conexoes": conexoes_tela.conexoes_get,
             "/config/agentes": agentes_tela.agentes_get, "/config/agentes/login": agentes_tela.login_get,
             "/config/rotinas": rotinas_tela.rotinas_get, "/config/rotinas/log": rotinas_tela.log_get,
             "/config/plugins": plugins_tela.plugins_get}
ROTAS_POST = {"/config/cofre": cofre_post, "/config/projetos": projetos_post, "/config/conexoes": conexoes_tela.conexoes_post,
              "/config/agentes": agentes_tela.agentes_post, "/config/rotinas": rotinas_tela.rotinas_post,
              "/config/plugins": plugins_tela.plugins_post}
_ACAO = {"/config/cofre": "cofre.escrever", "/config/projetos": "projetos.escrever", "/config/conexoes": "conexoes.escrever",
         "/config/agentes": "agentes.escrever", "/config/rotinas": "rotinas.escrever",
         "/config/plugins": "plugins.escrever"}  # ação que `pode` confere em cada rota POST


def tratar_post(caminho, campos, usuario, ip, csrf_valido):
    """(status, destino). 404 rota desconhecida, 403 sem CSRF ou sem permissão, 303 com ?ok=/?erro= no destino."""
    fn = ROTAS_POST.get(caminho)
    if fn is None:
        return 404, None
    if not csrf_valido or not pode(usuario, _ACAO.get(caminho, caminho)):
        return 403, None
    ok, msg, *pagina = fn(campos, usuario, ip)
    if pagina:  # a rota devolve a própria página (formulário com o erro no campo)
        return 200, pagina[0]
    return 303, f"{caminho}?{'ok' if ok else 'erro'}={urllib.parse.quote(msg)}"
