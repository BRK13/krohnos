"""Propostas: tudo que sairia para terceiros vira proposta, e só o dono manda executar (`faz N` no WhatsApp).

Uma proposta por arquivo em $HUB_DATA/propostas/<id>.json:

    {"id", "criada_em", "tarefa", "tipo", "destino", "resumo", "conteudo", "como_executar", "dados", "estado",
     "historico": [{"em", "estado", "nota"}], "assinatura"?}

- tipo: um de TIPOS. Toda proposta é executável pelo `faz N` do dono, sem passo manual (não existe "outro"):
  email               Resend, pelo domínio do remetente      dados: {"de": "Nome <x@dominio>", "assunto": "..."}
  whatsapp            pelo número dono da conversa           dados: {"remetente": "fsite05", "contato_id": <uuid do CRM>,
                      (fsite05 = WhatsApp da clínica, CRM;             "reativacao": true|false}
                       asite01 = bot do número da asite01)          ou {"remetente": "asite01", "numero": "55..."}
                                                             ou {"remetente": "asite01", "lote": [{"numero", "texto", ...}]}
                                                             (até 10; o bot agenda e manda aos poucos, hub/prospeccao_wpp.py)
  post_gmb            Google da fsite05 (Post Bridge)          dados: {"unidade": "alfa|beta", "link"?, "imagem"?}
  resposta_avaliacao  API do Google da fsite05                 dados: {"unidade": "alfa|beta", "review_id": "..."}
  pr_merge            CI verde → merge → deploy do Dokploy   destino ou dados.pr = URL do PR
  vps                 o root roda dados.comandos (bash) exatamente como o dono viu (hash), com timeout
                                                             dados: {"comandos": "...", "timeout_min"?: 1..30}
  carrossel           Instagram @clientej-com-br.example          dados: {"carrossel_id": "<id>", "slides": [sha256 dos PNG],
                      (Post Bridge, hub/farejador_social.py           "legenda_sha256": "..."}; conteudo = a legenda
                       publicar; o runner marca aprovada)
- destino: para quem/onde (e-mail, telefone, unidade, URL do PR, "VPS")
- conteudo: o texto exato que sai (e-mail: corpo; post, resposta e WhatsApp: o texto; pr_merge e vps: por quê e risco)
- como_executar: opcional, uma linha (quem executa é sempre o executor do tipo)
- estado: pendente → enviada (virou Pedido N no WhatsApp) → feita | recusada (nao N) | falhou
  (`aprovada`: o `faz N` do carrossel; o hub publica e marca feita. Fora isso, só em propostas antigas do tipo "outro")

    python -m hub.propostas criar --tarefa <nome> --tipo email --destino x@y --resumo "..." \
        --conteudo-arquivo corpo.txt --como "..." [--dados '{"de": "...", "assunto": "..."}'] [--aprovada]
    --aprovada (só root): assina a proposta (HMAC-SHA256, `assinatura`) e o bot executa sem pedir `faz N`.
    python -m hub.propostas listar [--estado pendente] [--json]
    python -m hub.propostas marcar <id> --estado feita [--nota "..."]
"""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path

from hub import RAIZ, config, data

TIPOS = ("email", "whatsapp", "post_gmb", "resposta_avaliacao", "pr_merge", "vps", "carrossel")
ESTADOS = ("pendente", "enviada", "aprovada", "feita", "recusada", "falhou")
RE_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[a-z]{2,}$", re.I)
RE_DE = re.compile(r"^(?:[^<>]+<)?[^@\s<>]+@[^@\s<>]+\.[a-z]{2,}>?$", re.I)
RE_PR = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
RE_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]+-[0-9a-f]{4}$")
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
RE_TELEFONE = re.compile(r"^\d{10,15}$")
RE_REVIEW = re.compile(r"^[A-Za-z0-9_-]{6,200}$")
RE_SHA = re.compile(r"^[0-9a-f]{64}$")
RE_CARROSSEL = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")  # = carrossel_clientej.RE_ID (lá importa Pillow)
UNIDADES = ("alfa", "beta")
PASTA_PROJETOS = RAIZ / "projetos"


def remetentes():
    """{id: tipo} dos donos de número: projetos/<id>.json `remetentes` (dict id→tipo ou lista, que vale "numero");
    mesclados com whatsapp.remetentes da config (projeto tem precedência). Tipo "crm" = contato do CRM (contato_id),
    "numero" = número com DDI (ou lote)."""
    saida = dict(config.get("whatsapp.remetentes"))
    for arq in sorted(PASTA_PROJETOS.glob("*.json")):
        try:
            r = json.loads(arq.read_text(encoding="utf-8")).get("remetentes") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(r, list):
            saida.update({k: "numero" for k in r if isinstance(k, str)})
        elif isinstance(r, dict):
            saida.update({k: v for k, v in r.items() if isinstance(k, str) and v in ("crm", "numero")})
    return saida

def __getattr__(nome):
    if nome == "REMETENTES":
        return remetentes()
    raise AttributeError(nome)


VPS_MAX_COMANDOS = 4000
CHAVE_APROVACAO = Path(os.environ.get("CHAVE_APROVACAO", "/opt/asite01-wpp/data/root-fila/.chave-aprovacao"))  # env só p/ teste
UTC = dt.timezone.utc


class PropostaInvalida(ValueError):
    pass


def pasta(base_dados=None):
    return Path(base_dados or data()) / "propostas"


def validar(p, base_dados=None):
    erros = []
    if p.get("tipo") not in TIPOS:
        erros.append(f"tipo tem que ser um de {', '.join(TIPOS)}")
    for campo in ("tarefa", "destino", "resumo", "conteudo"):
        if not isinstance(p.get(campo), str) or not p[campo].strip():
            erros.append(f"falta '{campo}'")
    dados = p.get("dados") or {}
    if not isinstance(dados, dict):
        erros.append("'dados' tem que ser objeto")
        dados = {}
    if p.get("tipo") == "email":
        if not RE_EMAIL.match(str(p.get("destino", "")).strip()):
            erros.append("email: 'destino' tem que ser um endereço de e-mail")
        if not str(dados.get("assunto", "")).strip():
            erros.append("email: falta dados.assunto")
        if not RE_DE.match(str(dados.get("de", "")).strip()):
            erros.append("email: falta dados.de (\"Nome <x@dominio>\")")
    if p.get("tipo") == "pr_merge" and not RE_PR.match(str(dados.get("pr") or p.get("destino") or "")):
        erros.append("pr_merge: dados.pr (ou destino) tem que ser a URL do PR no GitHub")
    erros += _erros_do_tipo(p.get("tipo"), p, dados, base_dados)
    if p.get("estado", "pendente") not in ESTADOS:
        erros.append(f"estado tem que ser um de {', '.join(ESTADOS)}")
    if erros:
        raise PropostaInvalida("; ".join(erros))


def _erros_do_tipo(tipo, p, dados, base_dados=None):
    """O que o executor de cada tipo precisa para rodar sem ninguém completar à mão."""
    erros, conteudo = [], str(p.get("conteudo") or "")
    if tipo == "whatsapp":
        rem, donos = dados.get("remetente"), remetentes()
        if rem not in donos:
            erros.append(f"whatsapp: dados.remetente tem que ser um de {', '.join(donos)} (dono do número)")
        elif donos[rem] not in ("crm", "numero"):
            erros.append("whatsapp: tipo de remetente inválido")
        elif Path(config.LEGADO["caminhos.codigo"]).is_dir() and (
                rem not in config.LEGADO_VALOR["whatsapp.remetentes"]
                or donos[rem] != config.LEGADO_VALOR["whatsapp.remetentes"][rem]):
            erros.append("whatsapp: remetente sem executor instalado na VPS (ou com tipo diferente do executor)")
        elif donos[rem] == "crm" and not RE_UUID.match(str(dados.get("contato_id", ""))):
            erros.append(f"whatsapp {rem}: dados.contato_id (uuid do contato no CRM) é obrigatório")
        elif donos[rem] == "numero" and "lote" in dados:
            erros += _erros_do_lote(dados.get("lote"))
        elif donos[rem] == "numero" and not RE_TELEFONE.match(str(dados.get("numero", ""))):
            erros.append(f"whatsapp {rem}: dados.numero só com dígitos e DDI (ex.: 5500000000001)")
        limite = 15000 if "lote" in dados else 1000
        if not 1 <= len(conteudo.strip()) <= limite:
            erros.append(f"whatsapp: conteudo de 1 a {limite} caracteres")
    elif tipo in ("post_gmb", "resposta_avaliacao"):
        if dados.get("unidade") not in UNIDADES:
            erros.append(f"{tipo}: dados.unidade tem que ser {' ou '.join(UNIDADES)}")
        if re.search("[\u2014\u2013]", conteudo):
            erros.append(f"{tipo}: sem travessão no texto")
        if tipo == "resposta_avaliacao" and not RE_REVIEW.match(str(dados.get("review_id", ""))):
            erros.append("resposta_avaliacao: dados.review_id (id da avaliação no Google) é obrigatório")
        if tipo == "post_gmb":
            if not 50 <= len(conteudo.strip()) <= 1500:
                erros.append("post_gmb: texto de 50 a 1500 caracteres")
            link = dados.get("link")
            if link and not str(link).startswith("https://clientea-com.example"):
                erros.append("post_gmb: dados.link tem que ser uma página de https://clientea-com.example")
    elif tipo == "vps":
        cmds = dados.get("comandos")
        if not isinstance(cmds, str) or not cmds.strip():
            erros.append("vps: dados.comandos (o bash exato que o root vai rodar) é obrigatório")
        elif len(cmds) > VPS_MAX_COMANDOS:
            erros.append(f"vps: dados.comandos com mais de {VPS_MAX_COMANDOS} caracteres")
        t = dados.get("timeout_min", 10)
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 30:
            erros.append("vps: dados.timeout_min de 1 a 30")
    elif tipo == "carrossel":
        cid = str(dados.get("carrossel_id") or "")
        if not RE_CARROSSEL.match(cid):
            erros.append("carrossel: dados.carrossel_id inválido")
        elif not (Path(base_dados or data()) / "claude" / "virais" / "carrosseis" / cid / "carrossel.json").is_file():
            erros.append(f"carrossel: {cid} sem carrossel.json (rode carrossel_clientej montar)")
        slides = dados.get("slides")
        if not isinstance(slides, list) or not 5 <= len(slides) <= 10 or not all(RE_SHA.match(str(x)) for x in slides):
            erros.append("carrossel: dados.slides com 5 a 10 sha256 dos PNG")
        if not RE_SHA.match(str(dados.get("legenda_sha256", ""))):
            erros.append("carrossel: dados.legenda_sha256 é obrigatório")
        if not 1 <= len(conteudo.strip()) <= 2200:
            erros.append("carrossel: legenda de 1 a 2200 caracteres")
        if re.search("[\u2014\u2013]", conteudo):
            erros.append("carrossel: sem travessão na legenda")
    return erros


LOTE_MAX = 10  # prospecção por WhatsApp da asite01: o bot também trava em 10 envios por dia
RE_LINK = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|net|org|br)(\.br)?\b", re.I)


def _erros_do_lote(lote):
    """whatsapp asite01 com dados.lote (hub/prospeccao_wpp.py): até LOTE_MAX mensagens, uma por número, sem link."""
    if not isinstance(lote, list) or not 1 <= len(lote) <= LOTE_MAX:
        return [f"whatsapp asite01: dados.lote com 1 a {LOTE_MAX} mensagens"]
    erros, vistos = [], set()
    for i, it in enumerate(lote, 1):
        it = it if isinstance(it, dict) else {}
        num, texto = str(it.get("numero", "")), str(it.get("texto", "")).strip()
        if not RE_TELEFONE.match(num):
            erros.append(f"lote {i}: numero só com dígitos e DDI")
        if num in vistos:
            erros.append(f"lote {i}: número repetido")
        vistos.add(num)
        if not 1 <= len(texto) <= 1000:
            erros.append(f"lote {i}: texto de 1 a 1000 caracteres")
        if RE_LINK.search(texto):
            erros.append(f"lote {i}: primeira mensagem sem link")
    return erros


def assinar(p, chave_arq=None):
    """HMAC-SHA256 hex de id, tipo, destino, conteudo, numero e dados canônico (mesma mensagem que o bot verifica)."""
    chave_arq = Path(chave_arq or CHAVE_APROVACAO)
    try:
        try:
            chave = chave_arq.read_bytes()
        except FileNotFoundError:
            chave_arq.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = chave_arq.with_name(f".{chave_arq.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
            try:
                os.link(tmp, chave_arq)  # atômico; se outro criou antes, usa a dele
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
            chave = chave_arq.read_bytes()
    except OSError:
        raise PropostaInvalida(f"sem acesso à chave de aprovação ({chave_arq}): --aprovada só roda como root") from None
    d = p["dados"]
    canon = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    msg = "\n".join([p["id"], p["tipo"], p["destino"], p["conteudo"], str(d.get("numero", "")), canon])
    return hmac.new(chave, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def criar(tarefa, tipo, destino, resumo, conteudo, como_executar="", dados=None, base_dados=None, quando=None,
          aprovada=False, chave=None):
    quando = quando or dt.datetime.now(UTC)
    slug = re.sub(r"[^a-z0-9]+", "-", tarefa.lower()).strip("-")[:40] or "manual"
    p = {"id": f"{quando:%Y%m%d-%H%M%S}-{slug}-{secrets.token_hex(2)}", "criada_em": quando.isoformat(timespec="seconds"),
         "tarefa": tarefa, "tipo": tipo, "destino": destino.strip(), "resumo": resumo.strip(), "conteudo": conteudo,
         "como_executar": (como_executar or "").strip(), "dados": dados or {}, "estado": "pendente", "historico": []}
    validar(p, base_dados)
    if aprovada:
        p["assinatura"] = assinar(p, chave)
    _gravar(p, base_dados)
    return p


def _gravar(p, base_dados=None):
    d = pasta(base_dados)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{p['id']}.tmp"
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, d / f"{p['id']}.json")


def ler(pid, base_dados=None):
    if not RE_ID.match(pid):
        raise PropostaInvalida(f"id inválido: {pid!r}")
    return json.loads((pasta(base_dados) / f"{pid}.json").read_text(encoding="utf-8"))


def listar(estado=None, base_dados=None):
    saida = []
    for arq in sorted(pasta(base_dados).glob("*.json")):
        try:
            p = json.loads(arq.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if estado is None or p.get("estado") == estado:
            saida.append(p)
    return saida


def marcar(pid, estado, nota="", base_dados=None, quando=None):
    if estado not in ESTADOS:
        raise PropostaInvalida(f"estado tem que ser um de {', '.join(ESTADOS)}")
    p = ler(pid, base_dados)
    p["estado"] = estado
    p.setdefault("historico", []).append({"em": (quando or dt.datetime.now(UTC)).isoformat(timespec="seconds"),
                                          "estado": estado, "nota": nota})
    _gravar(p, base_dados)
    if estado in ("feita", "falhou") and p.get("tarefa", "").startswith("linear-"):
        _voltar_issue(p, estado, nota, base_dados)
    return p


def _voltar_issue(p, estado, nota, base_dados=None):
    """A issue não fica parada esperando o "faz": encerra ao gravar a proposta e volta aqui com o resultado."""
    from hub import agenda, linear  # agenda importa propostas
    fala = (f"Resultado do Pedido (proposta {p['id']}, {p['resumo']}): {estado}. {nota}".strip()
            + " Continue a issue a partir daqui.")
    try:
        ident = p["tarefa"].removeprefix("linear-").upper()
        item = linear.item_por_ident(ident)
        # mesmo contexto que o enfileirar usa para o item reaberto
        motivo = agenda.em_loop(p["tarefa"], base_dados, item.get("ultima_fala"), item.get("estado_desde"))
        if motivo:
            # INC-28: resultado de Pedido também consome o orçamento da issue.
            # Guarda o resultado na conversa sem abrir sessão nem simular fala do dono.
            linear.comentar(item["id"], f"{fala}\n\nRetomada bloqueada pelo disjuntor: {motivo}. "
                            f"Resultado preservado no Pedido; aguarda novo rumo.\n\n_{linear.MARCA}._")
            agenda._log(f"{p['tarefa']}: proposta {estado}, mas a issue {motivo}; não volta")
            return
        if not agenda.enfileirar(linear.reabrir(ident, fala), f"proposta {estado}", base_dados):
            agenda._log(f"{p['tarefa']}: proposta {estado}, mas a fila não aceitou (já na fila ou freio)")
    except Exception as e:  # noqa: BLE001 — marcar a proposta não pode falhar por causa do Linear
        print(f"aviso: {p['tarefa']} não voltou para a fila: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)


def texto_curto(p):
    return f"[{p['tipo']}] {p['resumo']} → {p['destino']} ({p['tarefa']}, {p['criada_em'][:10]}, id {p['id']})"


FRONTEIRA = """## Você resolve, não relata (fronteira: faz sozinho × pede "faz")
FAZ SOZINHO, sem perguntar, tudo que é seguro e reversível; depois confere que resolveu:
- Conserto na VPS pelo root (lista fechada; o root confere o alvo):
    python3 -m hub.raiz pedir <acao> [alvo] --motivo "<por quê>"     → imprime o id
    python3 -m hub.raiz resultado <id> --esperar 900                  → código e saída
  Ações: limpar-journal, limpar-docker, limpar-apt; reiniciar-servico <serviço do Swarm> (zumbi, travado, sem
  sinal); reiniciar-container <contêiner fora do Swarm>; rerodar-rotina <nome no deploy/nimbus.cron: coleta,
  geo, leitura...>; atualizar-hub (depois de merge no repo-privado); atualizar-runner (depois de merge no
  bot_whatsapp); deploy-app <app do Dokploy> (deploy que não subiu). Lista: `python3 -m hub.raiz acoes`.
  Banco, Dokploy e proxy (traefik) o root recusa: isso é Pedido "vps".
- Código: branch, correção, testes, PR e MERGE quando os testes/CI passam, nos repos do dono (repo-privado,
  bot_whatsapp, ferramentas_seo). Nos sites (cliente-a-v2, clienteipsi-v2, psite13, fsite06,
  cliente-q-clientei...): PR e merge se o AGENTS.md/CLAUDE.md do repo deixa e o CI/teste passa; o Dokploy
  publica sozinho, confira com curl. cliente-c: com a label `merge` na issue, merge na develop com `tsc` + `vitest` +
  `build` verdes e a revisão do Codex sem achado sério (docs/GOVERNANCE.md regra 5, decisão de 15/10, PR #219); a
  promoção develop → main (`--no-ff`, depois do build do combinado, regra 7) é do orquestrador; sem a label, proposta
  pr_merge; preço, cobrança, migração destrutiva e segredo continuam com o dono. clienteh (ClienteJ): com a
  label `merge`, `gh pr merge --squash` depois do `local-ci` verde (AGENTS.md, seção 5). Nunca force-push, nunca push direto na base.
  Esta é a regra do orquestrador e das contas com a label; a conta sem label segue o "Como trabalhar" (só `melhoria`).
- Reabrir trabalho que falhou: `python3 {codigo}/deploy/agenda.py enfileirar <tarefa>`.
- Todo conserto vai para o diário (o resumo das 8h mostra):
    python3 -m hub.consertos registrar --quebrou "..." --causa "..." --conserto "..." --estado consertado|precisa_fulano [--prova "..."] [--pedido <id>]
PEDE "faz" (vira proposta, abaixo) só o que: sai para terceiro (e-mail, WhatsApp, post, resposta pública, texto
publicado em nome de profissional), gasta dinheiro (crédito pago, compra, plano), derruba serviço por mais de alguns
segundos (banco, Dokploy, proxy, reboot, restart do Docker), é irreversível (apagar dado, migration em produção,
force-push) ou mexe em segredo e permissão.
"""


INSTRUCOES = """## Propostas: só o que pede o "faz" (o resto você faz sozinho; fronteira acima)
Você NÃO manda e-mail, WhatsApp, post ou resposta de avaliação a terceiros nem roda comando como root. Quando isso
for preciso, grave uma proposta EXECUTÁVEL (o "faz N" executa direto, ninguém completa nada à mão) e siga.
Quem aprova: operação (`vps`, `pr_merge`) é aprovada pelo ORQUESTRADOR (a cada 15 min, sem mensagem ao dono; ele
só encaminha ao dono o que envolve terceiro, dinheiro ou decisão de produto); `email`, `whatsapp`, `post_gmb`,
`resposta_avaliacao` e `carrossel` vão para o dono no WhatsApp. O resultado volta para esta tarefa/issue do mesmo jeito.
  cd {codigo} && python3 -m hub.propostas criar --tarefa {tarefa} --tipo <tipo> \\
    --destino "<para quem>" --resumo "<uma linha>" --conteudo-arquivo <arquivo com o texto exato> --dados '<json>'
Tipos e o que --dados precisa (a validação recusa proposta incompleta):
- email: {{"de": "Nome <x@dominio-do-site>", "assunto": "..."}}; --destino = o e-mail.
- whatsapp: {{"remetente": "fsite05", "contato_id": "<uuid do CRM>", "reativacao": true|false}} (WhatsApp da clínica;
  reativacao=true manda a mensagem pronta aprovada se a janela de 24 h estiver fechada) ou
  {{"remetente": "asite01", "numero": "55DDDNUMERO"}} (número da asite01). Conteúdo = só a mensagem.
- post_gmb: {{"unidade": "alfa|beta", "link": "https://clientea-com.example/...", "imagem": "/images/..."}}; texto de 50 a 1500.
- resposta_avaliacao: {{"unidade": "alfa|beta", "review_id": "<id>"}}; conteúdo = a resposta.
- pr_merge: --destino = URL do PR (só quando a regra do repo não deixa você mesmo fazer o merge).
- vps: {{"comandos": "<bash exato, um comando por linha>", "timeout_min": 10}}; conteúdo = por quê e o risco.
  O orquestrador vê os comandos e o risco; o "faz N" dele faz o root rodar exatamente aquilo e o resultado volta aqui.
- carrossel: não crie à mão; `python3 -m hub.carrossel_clientej propor <id>` grava a proposta (legenda até 2200, sem
  travessão). Aprovada, o hub agenda no Instagram @clientej-com-br.example.
Não existe proposta "para o dono fazer à mão": se não cabe em um tipo, faça você ou diga no texto final por que não dá.
Não repita proposta pendente: `python3 -m hub.propostas listar --estado pendente`.
Desistiu de uma proposta já gravada: marque-a na hora (`python3 -m hub.propostas marcar <id> --estado recusada --nota "..."`) e confira com `listar --estado pendente`: o orquestrador lê a fila, não o texto da issue, e executa o que ficou pendente.
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.propostas", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar")
    c.add_argument("--tarefa", default="manual")
    c.add_argument("--tipo", required=True, choices=TIPOS)
    c.add_argument("--destino", required=True)
    c.add_argument("--resumo", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--conteudo")
    g.add_argument("--conteudo-arquivo")
    c.add_argument("--como", default="")
    c.add_argument("--dados", default="{}")
    c.add_argument("--aprovada", action="store_true")
    li = sub.add_parser("listar")
    li.add_argument("--estado", choices=ESTADOS)
    li.add_argument("--json", action="store_true")
    m = sub.add_parser("marcar")
    m.add_argument("id")
    m.add_argument("--estado", required=True, choices=ESTADOS)
    m.add_argument("--nota", default="")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.cmd == "criar":
            conteudo = args.conteudo if args.conteudo is not None else Path(args.conteudo_arquivo).read_text(encoding="utf-8")
            try:
                dados = json.loads(args.dados)
            except ValueError as e:
                raise PropostaInvalida(f"--dados não é JSON: {e}") from None
            p = criar(args.tarefa, args.tipo, args.destino, args.resumo, conteudo, args.como, dados, aprovada=args.aprovada)
            print(f"proposta gravada: {p['id']}" + (" (aprovada: executa sem pedir faz)" if args.aprovada else ""))
        elif args.cmd == "listar":
            ps = listar(args.estado)
            if args.json:
                print(json.dumps(ps, ensure_ascii=False, indent=2))
            else:
                print("\n".join(texto_curto(p) for p in ps) or "nenhuma proposta")
        else:
            p = marcar(args.id, args.estado, args.nota)
            print(f"{p['id']}: {p['estado']}")
    except (PropostaInvalida, OSError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
