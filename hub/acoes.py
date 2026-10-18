"""Ações do painel (plano docs/plans/feitos/2026-10-09-cockpit.md, Task 3): criar pedido para os Claudes, comentar numa
issue, aprovar/recusar Pedido N. Toda ação passa no painel por sessão (login), CSRF da sessão e Origin; aqui só valida
a entrada e executa. Aprovar/recusar usa o mesmo caminho do WhatsApp: escreve "faz N"/"nao N" na caixa de comandos
que o bot lê (triagem-wpp)."""
import json
import os
import re
import time
from pathlib import Path

from hub import agenda, data, faltou, linear, metas, tarefas, usuarios

try:  # o cofre de provas de clientes é nosso: fora do snapshot público, a ação responde que não existe
    import hub.provas as provas
except ImportError as _e:
    if not faltou(_e, "hub.provas"):
        raise
    provas = None

# nomes conferidos contra os projetos reais do Linear em 2026-10-09
PROJETOS = ("Cliente C", "ClienteJ", "Cliente A", "Cliente F", "Cliente G", "Nimbus",
            "Triagem WhatsApp", "Infra VPS")
# Registro único das ações: o que não está aqui é recusado. Quem pode cada uma: usuarios.PODE / usuarios.SO_DONO.
ACOES = ("criar-issue", "comentar", "pedido", "criar-meta", "clonar-viral", "convidar", "papel", "desativar",
         "autorizar-prova", "revogar-prova", "replay")


# Alvo da linha de auditoria: o identificador principal do campo, nunca o texto livre (comentário, descrição) nem token.
ALVO = {"criar-issue": "titulo", "comentar": "issue", "pedido": "n", "criar-meta": "objetivo", "clonar-viral": "viral",
        "autorizar-prova": "id", "revogar-prova": "id", "convidar": "email", "papel": "email", "desativar": "email", "replay": "ref"}


def executar(acao, campos, agora, quem=None, ip=None):  # noqa: C901
    """(ok, mensagem em português para mostrar) ou, no convite, (ok, mensagem, {"convite": token}): o painel monta o
    link e o mostra uma vez; o token nunca vai para a mensagem. Toda chamada vira uma linha de auditoria (quem = e-mail
    da sessão, ip do pedido); exceção também (resultado "erro: <tipo>") e sobe igual."""
    alvo = ((campos or {}).get(ALVO.get(acao, "")) or "").strip()[:80]
    if acao == "replay":  # quem, ref e passo; a correção é texto livre e não vai à auditoria
        alvo = f"{alvo[:40]} passo {str((campos or {}).get('passo') or '')[:6]}"
    try:
        r = _executar(acao, campos, agora, quem)
    except (Exception, SystemExit) as ex:  # noqa: BLE001
        usuarios.auditar(quem, acao, alvo, f"erro: {type(ex).__name__}", ip, agora)
        raise
    usuarios.auditar(quem, acao, alvo, "ok" if r[0] else f"erro: {r[1]}", ip, agora)
    return r


def _executar(acao, campos, agora, quem=None):
    from hub import plugins
    if acao not in ACOES:
        fn = plugins.acao(acao)  # ação de plugin ligado (ex.: video-pedir)
        return fn(campos, agora) if fn else (False, "Ação desconhecida.")
    if acao in ("autorizar-prova", "revogar-prova"):
        return _prova(acao, campos, agora, quem)
    if acao in usuarios.SO_DONO:
        return _usuario(acao, campos, agora)
    if acao == "criar-issue":
        projeto, titulo = campos.get("projeto", ""), (campos.get("titulo") or "").strip()
        if projeto not in PROJETOS or not titulo:
            return False, "Escolha o projeto e escreva o que você quer."
        r = linear.criar_issue(projeto, titulo[:200], (campos.get("descricao") or "").strip(), "Todo", [], 3)
        return True, f"Criado: {r.split()[0]}. Um Claude pega em até 1 minuto."
    if acao == "comentar":
        issue, texto = campos.get("issue", ""), (campos.get("texto") or "").strip()
        if not re.fullmatch(r"[A-Z]+-\d+", issue) or not texto:
            return False, "Escreva o comentário."
        linear.comentar(issue, texto[:5000])
        return True, f"Enviado em {issue}."
    if acao == "pedido":
        n, decisao = campos.get("n", ""), campos.get("decisao", "")
        if not re.fullmatch(r"\d{1,5}", n) or decisao not in ("faz", "nao"):
            return False, "Pedido inválido."
        caixa = Path(os.environ.get("CAIXA_COMANDOS", "/opt/asite01-wpp/data/comandos"))
        caixa.mkdir(parents=True, exist_ok=True)
        nome = caixa / f"{time.time_ns():020d}"
        tmp = nome.with_suffix(".tmp")
        tmp.write_text(json.dumps({"texto": f"{decisao} {n}", "de": "painel", "em": agora.isoformat()}), encoding="utf-8")
        os.replace(tmp, nome.with_suffix(".json"))
        return True, f"{'Aprovado' if decisao == 'faz' else 'Recusado'}: Pedido {n}. O resultado chega no WhatsApp."
    if acao == "criar-meta":
        g = lambda k: (campos.get(k) or "").strip()
        metrica, alvo = g("metrica"), g("alvo").replace(",", ".")
        medir = {"tipo": "pagespeed", "url": g("url"), "metrica": metrica} if g("url") else \
            {"tipo": "manual", "como": g("como")} if g("como") else None
        try:
            if alvo and metrica in ("lcp_ms", "tbt_ms") and medir and medir["tipo"] == "pagespeed" and 0 < float(alvo) < 100:
                alvo = float(alvo) * 1000  # "2,0" com LCP é segundos
            m = metas.criar(g("objetivo"), site=g("site") or None, medir=medir, alvo=alvo or None,
                            direcao=g("direcao") or "menor", unidade=g("unidade") or ("ms" if medir and metrica in ("lcp_ms", "tbt_ms") else ""), esforco=g("esforco") or "medio", origem="painel")
        except ValueError as ex:
            return False, str(ex)
        return True, f"Meta criada: {m.get('issue') or m['id']}. Um Claude pega em até 1 minuto."
    if acao == "replay":  # rodar de novo deste passo com a correção (permissão = a da tela /execucao)
        from hub import rastreio
        passo = campos.get("passo", "")
        if not re.fullmatch(r"[0-9]{1,6}", passo):
            return False, "Passo inválido."
        r = rastreio.rodar_de_novo(campos.get("ref", ""), int(passo), campos.get("correcao", ""), quem=quem,
                                   via="painel")
        return r["ok"], r["motivo"]
    if acao == "clonar-viral":
        vid = campos.get("viral", "")
        if not re.fullmatch(r"(x|ig):[0-9A-Za-z_]{1,40}", vid):
            return False, "Viral inválido."
        pasta = data() / "claude" / "virais" / "clonar"
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / (vid.replace(":", "-") + ".json")).write_text(json.dumps({"viral": vid, "pedido_em": agora.isoformat()}), encoding="utf-8")
        arq = tarefas.PASTA / "clientej-virais.md"
        if arq.exists():  # a tarefa nasce na Task 7; sem ela, o pedido fica gravado para a próxima rodada
            agenda.enfileirar(tarefas.parse(arq.read_text(encoding="utf-8"), arq.stem), "clonar pedido no painel")
        return True, "Pedido de clone anotado. O carrossel chega no seu WhatsApp para aprovar."
    return False, "Ação desconhecida."


def _usuario(acao, campos, agora):
    """Ações de dono sobre usuários (convidar, papel, desativar); ValueError vira a mensagem."""
    email = (campos.get("email") or "").strip()
    try:
        if acao == "convidar":
            return True, "Convite criado.", {"convite": usuarios.convidar(email, campos.get("papel", ""), agora)}
        if acao == "papel":
            usuarios.trocar_papel(email, campos.get("papel", ""))
            return True, "Papel trocado."
        usuarios.desativar(email)
        return True, "Usuário desativado."
    except ValueError as ex:
        return False, str(ex)


def _prova(acao, campos, agora, quem):
    """Autorizar ou revogar uma prova do cofre (só dono). Quem autorizou = o e-mail da sessão; `por` do formulário é quem
    deu o aval (o cliente) e o canal onde ele respondeu."""
    if provas is None:
        return False, "Cofre de provas indisponível nesta instalação."
    produto, pid = (campos.get("produto") or "").strip(), (campos.get("id") or "").strip()
    try:
        if acao == "autorizar-prova":
            por = (campos.get("por") or "").strip()
            canal = (campos.get("canal") or "").strip()
            if not por or not canal:
                return False, "Diga quem autorizou e por qual canal (ex.: Maria, WhatsApp)."
            provas.autorizar(produto, pid, por=f"{por} (registrado por {quem})" if quem else por, canal=canal, agora=agora)
            return True, "Prova autorizada."
        provas.revogar(produto, pid, motivo=(campos.get("motivo") or "").strip(), agora=agora)
        return True, "Autorização retirada."
    except ValueError as ex:
        return False, str(ex)
