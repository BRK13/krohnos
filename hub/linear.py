"""Ponte Linear → fila do Claude da VPS. O Linear é a interface: qualquer fala do dono lá vira pedido para o Claude.

O Claude é um usuário-app do workspace (OAuth app "Claude", token por client_credentials, actor=app). Cada pedido
roda numa sessão de agente na issue: o painel do agente mostra ao vivo o que ele pensa e cada ação (hub/agenda.py
manda os eventos do `claude -p --output-format stream-json`), a pergunta vira elicitation e o fim vira response.

A cada tick da conta 1, `puxar()` pega:
- sessão de agente nova (o dono delegou a issue ao Claude ou mencionou @Claude) ou com fala nova do dono no painel;
- issue em Todo, ou aberta em qualquer coluna (fora Done e Canceled, mexida nos últimos 7 dias) que o Claude nunca
  tocou ou cujo último comentário é do dono: abre uma sessão nela (agentSessionCreateOnIssue);
- update de projeto (últimos 7 dias) sem resposta do Claude: esse continua por comentário, com a chave do dono.
A issue vai para In Progress; a tarefa (linear-, prioridade 1) vai para a fila com toda a conversa no prompt.
No fim, `concluir()`: ok → response e In Review; falha → error e Backlog. Limite de uso: volta para a fila calado.
Pedido "parar" no painel (signal stop) mata a execução. Sem as credenciais do app, tudo cai no modo antigo: comentários
com a chave do dono marcados com MARCA.

Durante a execução, o Claude fala pelo painel (o prompt ensina; LINEAR_SESSAO vem no ambiente):
    python3 -m hub.linear comentar <<'FIM'... FIM # plano e marcos (thought)
    python3 -m hub.linear perguntar -minutos 20 <<'FIM'... FIM # pergunta e espera a resposta
    python3 -m hub.linear novidades -desde <ISO> # falas do dono depois de <ISO>
    python3 -m hub.linear voltar-quando -pr <url> # termina; volta com o local-ci e a revisão (hub/esperas.py)
    python3 -m hub.linear voltar-quando -pr <url> --no-ar # depois do merge: volta quando o deploy subir

Repositório: a descrição do projeto tem linhas `repo: <org>/nome`. O modelo vem de hub.json → "roteamento"
(hub/roteamento.py): label, prioridade e palavras do título; sonnet no resto (economia de 15/10).
Itens de plano (hub/plano_itens.py): `- [ ] **I1** título` com `check: `comando``, `files:`, `after:`, `exec:`; sem `check:` não vai para o Haiku, e o
portão de Done exige a saída PASSOU de cada `check:` da issue (`python3 -m hub.plano_itens conferir <arquivo|->` roda e imprime o bloco). PR com "Fixes" liga e fecha a issue pela integração do GitHub do Linear.

Segredos: LINEAR_API_KEY, LINEAR_APP_CLIENT_ID e LINEAR_APP_CLIENT_SECRET no ambiente ou em
linear.env na pasta de segredos (root:claude 640); no PC, no .env do hub. Token do app em cache em
$HUB_DATA/claude/linear-app-token.json (30 dias).
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hub import backoff, config, RAIZ, codigo, data, faltou, segredos, esperas, incidentes, linear_uso, retomada, roteamento, tarefas as T
from hub.comum import env as comum_env

API = "https://api.linear.app/graphql"
ARQ_CHAVE = segredos() / "linear.env"
TIMEOUT = "3h"
CACHE_S = 90  # issue, labels, projetos e estados repetidos no mesmo minuto vêm do cache (60-120 s)
MARCA = "Claude da VPS"
# `comentar` feito fora da execução da própria issue (o orquestrador repassando uma mudança do dono, ou outra tarefa):
# sai pelo mesmo usuário-app, mas é fala para a execução daquela issue, não dela (entregaram a
# versão velha porque `novidades` descartava o repasse como comentário do Claude)
REPASSE = "Repasse do orquestrador"
RE_REPASSE = re.compile(rf"^_{REPASSE}\._$", re.M)  # linha própria: citar a marca no meio do texto não conta
RE_REPO = re.compile(r"^\s*repo:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*$", re.M | re.I)

COMO = """
## Como trabalhar
- Permissões liberadas: rode o que precisar (instalar, testar, build, git, gh) sem pedir. Só o irreversível para fora
  (mensagem a cliente, publicação, banco de produção) vira proposta.
- Repositórios em ~/repos/<nome> (clone com o GH_TOKEN se faltar). Antes de mexer: `git fetch` e leia o CLAUDE.md e o
  AGENTS.md do repo, que mandam na branch base, no gerenciador de pacote e nos testes.
- CPU da VPS é dividida por até 12 sessões: NÃO rode `next build` nem a suíte inteira por conta própria. Rode a checagem
  de tipos e os testes dos arquivos que você mudou; o build e a suíte completa são do CI (local-ci) e do deploy. Nunca
  rode `scripts/ci-local.sh` nem o build do repo à mão: o local-ci é do runner (um por vez na máquina); espere o
  status `local-ci` no PR (`gh pr checks`). local-ci vermelho: leia o log antes. O log completo
  (/home/usuario/.cache/clienteh-ci-local/logs/<sha>.log) NÃO abre para você (só o runner lê): use o fim que o retorno da
  espera traz e, se faltar, o job do `ci.yml` da mesma branch (`gh api repos/<org>/<repo>/actions/runs?branch=<branch>`,
  depois `.../actions/jobs/<id>/logs`: mesma suíte; `gh run view --log-failed` falha). Só comente `/local-ci` no PR (nova rodada) se o
  log mostrar timeout, ENOSPC ou falha de infra; erro de teste ou build é do código: corrija. Não rode em paralelo.
  O que você deixar rodando em segundo plano morre quando a tarefa termina.{ci}
- Exceção para o build local, nos repos SEM local-ci (o PR sai sem status): cliente-c e clientee-moraes-site exigem o
  build local como prova antes do merge (asite01: `tsc` + `vitest` + `build`, e de novo no combinado antes de cada merge).
  Rode UM, com `nice`, em segundo plano e com timeout longo (no asite01 o build e o vitest levam 20+ min), nunca dois ao
  mesmo tempo. Todo build, render em loop (Playwright), `tsc` grande e suíte completa entra na fila de 1 por vez da
  máquina: `pesado nice -n 19 <comando>` (espera a vez; a servidor_hub caiu por OOM em 17/10 com 6 juntos, KRN-1125). psite13: `typecheck`, `lint` e os testes dos arquivos tocados (o build é do deploy do Dokploy).
  clienteh, clienteipsi-v2 e demais com local-ci: nunca build nem suíte à mão; o `tsc` do clienteh só dos
  arquivos mudados (tsconfig temporário dentro da worktree).
- Código: branch própria a partir da base do repo, commits claros, PR{fixes}. Não faça merge, exceto: melhoria do
  próprio sistema (label `melhoria`, repos repo-privado e bot_whatsapp) com os testes verdes (aí faça o merge e
  `python3 -m hub.raiz pedir atualizar-hub` ou `atualizar-runner`, e confira o `resultado`), ou issue com a label
  `merge` (seção "Merge liberado", se houver). Sem isso, PR aberto e `pr_merge` só se a regra do repo exigir.
- Rotina agendada é tarefa do hub na VPS (tarefas/*.md do repo privado dos dados, lidas pelo hub; contrato em
  docs/tarefas-contrato.md) ou rotina do app no Dokploy; workflow e Vercel o portão `hub.memorias checar --diff` barra.
  Nunca `push --force`.
- Pergunta ou conversa: responda direto, sem mexer em repo.
- O seu texto final vira a resposta no Linear ({id}): curto, em português, com o link do PR quando houver. Ele também é o
  resumo do estado para a próxima execução: diga, em até 10 linhas, o que foi feito, o PR e o que falta.

## Submissão de app a diretórios externos (INC-53)
Antes de enviar ou reenviar, siga `docs/checklist-submissao-app.md`: teste a conta do revisor em dispositivo novo,
sem cookies nem sessão anterior, e registre a prova na issue. Senha aceita em dispositivo conhecido não prova login.
Se exigir código por e-mail, link mágico, MFA ou Device Trust do Clerk, a conta ainda não está pronta para o revisor:
não envie; registre o bloqueio e encaminhe mudança de segurança/permissão pela fronteira de propostas.
O checklist não autoriza envio a terceiros nem alteração de segurança; respeite a autorização vigente.

## Fale com o {dono} pelo Linear enquanto trabalha (ele acompanha ao vivo)
Cada ação sua já aparece sozinha no painel do agente. Além disso (rode de qualquer pasta):
    cd {codigo} && python3 -m hub.linear comentar {alvo} <<'FIM'
    texto em markdown
    FIM
    cd {codigo} && python3 -m hub.linear perguntar {alvo} --minutos 20 <<'FIM'
    pergunta, com a sua recomendação
    FIM
    cd {codigo} && python3 -m hub.linear novidades {alvo} --desde <ISO da última checagem>
- Trabalho com mais de uma etapa: `comentar` o plano curto ao começar e um progresso a cada marco (o que ficou pronto,
  o que vem, link de branch/PR/preview). Marcos, não cada comando.
- Gerou algo para o {dono} ver ou usar (vídeo, imagem, PDF, planilha): ANEXE, nunca só cite o caminho na VPS:
    cd {codigo} && python3 -m hub.linear anexar {alvo} <arquivo> [<arquivo>...] --texto "o que é"
  Entrega visual = anexar na issue. O hub manda sozinho ao WhatsApp do {dono} (no recibo de pronto) tudo o que está
  anexado ou citado no último comentário (PDF, DOCX, imagem, vídeo, áudio, link de prévia), sem repetir o que você já
  mandou. Não precisa avisar à mão só para entregar arquivo. Só a versão final vai (issue em Done, um arquivo por entrega);
  NÃO mande aviso ao WhatsApp por iteração de revisão, nem erro técnico (teto de API, CI, deploy, falha de tarefa): isso
  é do orquestrador. Tem teto de 10 por hora e 40 por dia e nada sai de 22h às 7h (KRN-943).
- Issue de dado (coletar, importar, classificar, carga, leitura por IA, cron): só termina com o bloco "## Prova de execução" no
  texto final: contagem antes/depois (consulta ou log) e o agendamento (cron instalado). Sem ele a issue volta para In
  Progress (portão, KRN-973). Código que não roda em massa: o hub abre a issue de execução; não declare pronto só com código.
- Entre uma etapa e outra, rode `novidades`: se o {dono} falou, responda e ajuste o rumo.
- Decisão que antes esperava o {dono} (aprovar visual ou texto, escolher entre opções, "posso mergear?"): tome com a sua
  recomendação, registre no progresso (`comentar`) e siga; o orquestrador revisa pelo Linear. Só pare e use `perguntar`
  (ou proposta) em risco alto: apagar dado real, dinheiro novo, contrato, mensagem a terceiro, mudança irreversível
  em produção. Tarefa nova no Backlog leva a label `proposta` e espera o orquestrador, não o {dono}.
- Gravou proposta: não espere o "faz" (nada de `sleep` esperando Pedido). Encerre dizendo o id do Pedido e o que
  falta; você volta sozinho, com o resultado, quando ele rodar.
- Deploy: confira uma vez (o `comentar` recusa "ainda não subiu" e grava o `--no-ar`). O ClienteJ/clienteh
  e o ClienteE (repo do Parceiro) não estão no Dokploy: sobem sozinhos no merge (ClienteJ com deploys espaçados de 2 h,
  7h-23h BRT) e `deploy-app` é sempre recusado ali; confira com curl. Nos outros sites, se não subiu, `deploy-app`
  e encerre.
- Depois do merge, a ordem é uma só: confira uma vez (curl em `/api/versao` ou no trecho do HTML que mudou). Subiu:
  encerre. Não subiu e o deploy é de poucos minutos (ClienteE ~4 min; bots do bot_whatsapp ~30 s): espere até 10 min na
  sessão com o Monitor (`timeout 60 tail -f /dev/null` entre as checagens; nada de `sleep` nem laço `until gh api`).
  Passou disso, ou é deploy lento (ClienteJ em janela de 2 h): peça a volta pelo hub (`--no-ar`, abaixo) e TERMINE.
  Nunca termine mandando o {dono} reabrir. "Volto quando X" sem PR (ex.: primeiro envio de uma campanha): tarefa de vida curta em
  `tarefas/` (haiku, prioridade 3, `avisar: false`) que confere, comenta na issue e abre o PR que a apaga.
"""

VOLTAR_QUANDO = """
- NÃO espere CI nem revisão por mais de 10 min dentro da sessão (nada de laço `until gh api ...` nem `sleep`): com o
  PR aberto, rode `cd {codigo} && python3 -m hub.linear voltar-quando {id} --pr <url do PR>` e TERMINE
  com uma linha ("PR X no CI; volto quando sair"). Você volta sozinho quando o local-ci sair e a revisão chegar (ou
  20 min depois do CI sem revisão), com o resultado na conversa. Nunca se espera mais de 2 h por revisor: sem revisão em 2 h,
  você mesmo lê o diff e segue. Todo `voltar-quando` tem teto (3 h; 12 h no `--no-ar`) e, vencido, a issue volta à fila.
- Merge feito e o commit ainda não está no ar (passou da espera de 10 min, ou o deploy é lento): rode `cd {codigo} && python3 -m hub.linear voltar-quando
  {id} --pr <url do PR> --no-ar` e TERMINE ("merge feito; volto quando estiver no ar"). O hub confere o deploy sem
  você e te reabre quando subiu (ou em 6 h), com o resultado."""

# Método "How to be more ambitious" (Felix Rieseberg, Anthropic, 13/10/2026), pedido do dono em 16/10.
# O mesmo texto vai para o orquestrador (deploy/orquestrador/CLAUDE.md) e para a esteira (tarefas/esteira-abastecer.md).
ALTITUDE = """
## Antes do plano: Altitude, Depth, Range, Verificação (obrigatório, até meia página, sem pedir OK)
Abra o plano (e o `comentar` do início) com esta seção; depois siga direto, sem esperar aprovação:
- **Altitude:** qual o resultado de verdade além do pedido.
- **Depth:** o que um profissional de ponta faria, com qual dado bruto e ferramenta.
- **Range:** 2 ou 3 campos de fora, com uma técnica concreta de cada (não persona), mais um "red team" que tente quebrar.
- **Verificação:** como saber que funcionou sem ser o próprio modelo julgando (teste, métrica, dado real).
"""

CODEX = """
## Você é o Codex (executor da faixa codex)
- Você roda via `codex exec` como o usuário claude da VPS. As skills `superpowers:*` e subagentes não existem aqui:
  trabalhe direto, em passos pequenos, com testes.
- A branch tem que começar com `codex/` (ex.: `codex/{branch}`): é assim que os revisores reconhecem PR do Codex.
- O seu texto final vira a resposta na issue.
"""


# ---- acesso ----

def _segredo(nome):
    return comum_env.segredo(nome, (ARQ_CHAVE, RAIZ / ".env"))


def _arq_token(nome="linear-app-token.json"):
    return Path(data()) / "claude" / nome


def _token_oauth(prefixo, nome_arq, escopo, renovar=False):
    """client_credentials do app `prefixo` (~30 dias, em cache em arquivo). None sem as credenciais."""
    cid, segredo = _segredo(f"{prefixo}_CLIENT_ID"), _segredo(f"{prefixo}_CLIENT_SECRET")
    if not (cid and segredo):
        return None
    arq = _arq_token(nome_arq)
    if not renovar:
        try:
            c = json.loads(arq.read_text(encoding="utf-8"))
            if c["expira"] > time.time() + 3600:
                return c["token"]
        except (OSError, ValueError, KeyError):
            pass
    corpo = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": segredo,
                                    "scope": escopo}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://api.linear.app/oauth/token", corpo), timeout=30) as r:
        d = json.loads(r.read())
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps({"token": d["access_token"], "expira": time.time() + d["expires_in"]}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, arq)
    return d["access_token"]


def token_app(renovar=False):
    """Token do usuário-app Claude (actor=app). None sem as credenciais do app."""
    return _token_oauth("LINEAR_APP", "linear-app-token.json", "read,write,app:assignable,app:mentionable", renovar)


def token_app2(renovar=False):
    """Token do app claude2: tick, esteira, contas e Codex, com o limite próprio de 5.000/h. None sem as credenciais."""
    return _token_oauth("LINEAR_APP2", "linear-app2-token.json", "read,write", renovar)


def chave():
    return _segredo("LINEAR_API_KEY") or (f"Bearer {token_app2()}" if _segredo("LINEAR_APP2_CLIENT_ID") else None)


def _fontes(app=False, usuario=False):
    """Chaves em ordem de uso: [(rótulo, auth(renovar) -> str|None, renovável)]. Só o rótulo vai para a medida.
    Padrão: claude2 (renova sozinho por client_credentials; sem as credenciais vale o LINEAR_API_KEY manual) e, na
    reserva, a chave do dono. `usuario=True`: o que exige usuário (criar label/projeto) começa pela chave do dono.
    `app=True`: o usuário-app Claude (actor=app)."""
    if app:
        return [("app", lambda r: f"Bearer {token_app(r)}", True)]
    app2 = ("app2", lambda r: (f"Bearer {token_app2(r)}" if _segredo("LINEAR_APP2_CLIENT_ID") else None) or _segredo("LINEAR_API_KEY"),
            bool(_segredo("LINEAR_APP2_CLIENT_ID") and _segredo("LINEAR_APP2_CLIENT_SECRET")))
    fulano = ("fulano", lambda r: _segredo("LINEAR_API_KEY_FULANO"), False)
    fontes = [fulano, app2] if usuario else [app2, fulano]
    vistas, saida = set(), []
    for rotulo, auth, renovavel in fontes:
        try:
            k = auth(False)
        except Exception:  # noqa: BLE001 — emissão do token OAuth falhou: segue para a próxima chave
            k = None
        if k and k not in vistas:  # LINEAR_API_KEY igual à do dono não vale como duas chaves
            vistas.add(k)
            saida.append((rotulo, auth, renovavel))
    return saida


def chaves():
    """Valores das chaves do dono em ordem (principal claude2, reserva do dono). Pedido do dono, 16/10: "usa as duas chaves"."""
    return [auth(False) for _, auth, _ in _fontes()]


def _limite(e):
    return getattr(e, "code", None) == 429 or b"RATELIMITED" in (getattr(e, "corpo", b"") or b"")


class _Limite(Exception):
    pass


class _Passa(Exception):
    """A chave não serve para esta chamada (401 depois de renovar, ou sem permissão): tenta a próxima."""


def conferir_automacao_git(time="KRN", consulta=None):
    """Regras de automação Git do time que mandam o merge para Done (merge não é Done; o hub fecha com recibo).
    Devolve a lista de textos dos achados; vazia = tudo certo. Só lê."""
    consulta = consulta or gql
    r = consulta('query($t:String!){ team(id:$t){ gitAutomationStates(first:50){ nodes{ id event state{ name type } '
                 'targetBranch{ branchPattern } } } } }', t=time)
    nos = (((r or {}).get("team") or {}).get("gitAutomationStates") or {}).get("nodes") or []
    achados = []
    for n in nos:
        est = n.get("state") or {}
        if n.get("event") == "merge" and (est.get("name") == "Done" or est.get("type") == "completed"):
            ramo = (n.get("targetBranch") or {}).get("branchPattern") or "qualquer ramo"
            achados.append(f"Linear {time}: regra merge → {est.get('name')} ({ramo}) voltou (id {n.get('id')}); "
                           "troque por In Review ou apague (gitAutomationStateUpdate/Delete)")
    return achados


def gql(query, app=False, usuario=False, cache=0, **variaveis):
    """GraphQL do Linear. app=True fala como o usuário-app Claude; senão, com o claude2 e, quando ele bate no limite da
    hora, dá 401 ou não tem permissão para a ação, passa para a chave do dono. 429/5xx/RATELIMITED: backoff.
    cache=<s>: leitura repetida dentro desses segundos vem do cache em arquivo (mutação limpa o cache todo)."""
    mutacao = query.lstrip().startswith("mutation")
    if cache and not mutacao and not app:
        guardada = linear_uso.cache_ler(query, variaveis)
        if guardada is not None:
            return guardada
    fontes = _fontes(app, usuario)
    if not fontes:
        raise RuntimeError("Linear: sem chave (LINEAR_API_KEY ou LINEAR_APP2_CLIENT_ID/SECRET)")
    for i, (rotulo, auth, renovavel) in enumerate(fontes):
        try:
            dados = _gql(query, rotulo, auth, renovavel, **variaveis)
            break
        except _Limite:
            if i == len(fontes) - 1:
                raise RuntimeError("Linear: limite da hora nas duas chaves") from None
        except _Passa as e:
            if i == len(fontes) - 1:
                raise RuntimeError(str(e)) from None
    if mutacao:
        with contextlib.suppress(Exception):
            linear_uso.cache_limpar()
    elif cache and not app:
        with contextlib.suppress(Exception):
            linear_uso.cache_gravar(query, variaveis, dados, cache)
    return dados


def _gql(query, rotulo, auth, renovavel, **variaveis):
    app = rotulo == "app"
    dono = rotulo != "app"
    for tentativa in (0, 1):
        req = urllib.request.Request(API, json.dumps({"query": query, "variables": variaveis}).encode(),
                                     {"Authorization": auth(bool(tentativa)) or "", "Content-Type": "application/json"})

        def chamar():
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    with contextlib.suppress(Exception):
                        linear_uso.registrar(rotulo, r.headers)
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                with contextlib.suppress(Exception):
                    linear_uso.registrar(rotulo, e.headers)
                e.corpo = e.read()[:300]  # o corpo só se lê uma vez
                raise

        try:
            # limite da hora com chave do dono: troca de chave na hora (gql), em vez de esperar
            d = backoff.com_backoff(chamar, lambda e: None if (dono and _limite(e)) else
                                    backoff.espera_linear(e, getattr(e, "corpo", b""), query.lstrip().startswith("mutation")))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401:
                if renovavel and not tentativa:
                    continue  # token expirou ou foi revogado: pede outro uma vez
                if dono:
                    raise _Passa("Linear HTTP 401: chave recusada") from None
            if dono and _limite(e):
                raise _Limite() from None
            raise RuntimeError(f"Linear HTTP {e.code}: {e.corpo!r}") from None
    if d.get("errors"):
        if dono and any((e.get("extensions") or {}).get("code") == "RATELIMITED" for e in d["errors"]):
            raise _Limite()
        msg = d["errors"][0].get("message") or ""
        if dono and "not allowed" in msg.lower():
            raise _Passa(f"Linear: {msg}")  # o app não pode esta ação (ex.: criar label/projeto): o usuário pode
        raise RuntimeError(f"Linear: {msg}")
    return d["data"]


# ---- consultas ----

CAMPOS = """
  id identifier title description url priority createdAt creator { app } state { name type }
  labels { nodes { name } }
  project { name description }
  comments(first: 50) { nodes { body createdAt user { name app } } }
  history(first: 30) { nodes { createdAt toState { type } } }
  team { states { nodes { id name } } }"""
Q_TODO = '{ issues(first: 20, filter: {state: {type: {eq: "unstarted"}}}) { nodes {' + CAMPOS + '} } }'
Q_CONVERSA = ('{ issues(first: 50, orderBy: updatedAt, filter: {state: {type: {neq: "unstarted"}}, '
              'updatedAt: {gt: "-P7D"}}) { nodes {' + CAMPOS + ' state { type } } } }')
Q_IDS = 'query($ids: [ID!]) { issues(first: 50, filter: {id: {in: $ids}}) { nodes {' + CAMPOS + '} } }'
Q_UPDATES = """{ projectUpdates(first: 20, orderBy: updatedAt, filter: {createdAt: {gt: "-P7D"}}) { nodes {
  id body url user { name app } project { name description }
  comments(first: 50) { nodes { body createdAt user { name app } } } } } }"""
Q_SESSOES = ("{ agentSessions(first: 30, orderBy: updatedAt) { nodes { id status updatedAt issue {" + CAMPOS + "} "
             "activities(first: 50) { nodes { createdAt signal content { __typename "
             "... on AgentActivityPromptContent { body } } } } } } }")


def _eh_bot(c):
    """Comentário do próprio Linear (botActor, workflow, "This thread is for an agent session"): vem com user=null.
    Só vale quando a consulta pediu `user` (a chave existe): faltar o campo não é o mesmo que vir sem autor."""
    return "user" in c and c["user"] is None


def _eh_meu(c):
    """Não é fala do dono: comentário do Claude (marca ou usuário-app) ou automático do Linear."""
    return MARCA in (c.get("body") or "") or bool((c.get("user") or {}).get("app")) or _eh_bot(c)


def _eh_repasse(c):
    return bool(RE_REPASSE.search(c.get("body") or ""))


def _eh_fala(c):
    """Fala para a execução: do dono, ou repasse dele pelo orquestrador (sai pelo usuário-app, mas não é desta
    execução). Usada por _falas e pela conversa do prompt; `quer_resposta` também acorda a issue com o repasse."""
    return _eh_repasse(c) or not _eh_meu(c)


def _quem(c):
    return f"{config.dono(True)} (repasse do orquestrador)" if _eh_repasse(c) else (c.get("user") or {}).get("name", config.dono(True))


def da_execucao(ident):
    """Este processo é a execução da própria issue? A agenda põe TAREFA_NOME=linear-<id> no ambiente."""
    nome = os.environ.get("TAREFA_NOME") or os.environ.get("HUB_TAREFA") or ""
    return nome.lower() == f"linear-{ident.lower()}"


def _ordem(nodes):
    return sorted(nodes, key=lambda c: c["createdAt"])


def quer_resposta(issue):
    """Issue que o Claude nunca tocou ou cujo último comentário é do dono. Fechada (Done/Canceled, ex.: o GitHub
    fecha quando o PR entra) só volta com fala nova do dono, nunca por estar sem comentário."""
    cs = _ordem(issue["comments"]["nodes"])
    if (issue.get("state") or {}).get("type") in ("completed", "canceled"):
        return bool(cs) and not _eh_meu(cs[-1])
    if not cs and (issue.get("creator") or {}).get("app"):
        return False  # proposta do próprio Claude (backlog semanal): espera o orquestrador mover para Todo
    # o comentário do orquestrador (repasse, sai pelo usuário-app) reabre a execução na hora; antes a
    # ficou horas parada com o rumo já dado, porque o último comentário "era do Claude"
    return not cs or not _eh_meu(cs[-1]) or _eh_repasse(cs[-1])


def update_quer_resposta(u):
    """Update de projeto do dono sem resposta do Claude depois da última fala dele."""
    cs = _ordem(u["comments"]["nodes"])
    return not _eh_meu(u) and (not cs or not _eh_meu(cs[-1]))


def _prompts(sessao):
    return [a for a in _ordem(sessao["activities"]["nodes"]) if a["content"]["__typename"] == "AgentActivityPromptContent"]


def sessao_quer_resposta(s):
    """Sessão sem resposta final (response/error) do Claude depois da última fala do dono (ou desde que abriu).
    Thought/action/elicitation não contam: o "Recebi" do webhook e o "Na fila" não fecham a conversa."""
    acts = _ordem(s["activities"]["nodes"])
    issue = s.get("issue") or {}
    if (issue.get("state") or {}).get("type") == "completed":
        # Sessões antigas/vazias continuam pending depois de Done (INC-26).
        # Só um pedido explícito posterior ao fechamento pode acordá-las.
        fechamentos = [h["createdAt"] for h in (issue.get("history") or {}).get("nodes", [])
                       if (h.get("toState") or {}).get("type") == "completed"]
        if not fechamentos or not any(_z(a["createdAt"]) > max(map(_z, fechamentos))
                                      for a in _prompts(s)):
            return False
    prompts = [i for i, a in enumerate(acts) if a["content"]["__typename"] == "AgentActivityPromptContent"]
    depois = acts[prompts[-1] + 1:] if prompts else acts
    return not any(a["content"]["__typename"] in ("AgentActivityResponseContent", "AgentActivityErrorContent")
                   for a in depois)


def parar_pedido(s):
    return any(a.get("signal") == "stop" for a in _prompts(s)[-1:])


# ---- escrita ----

def mover(issue_id, estado_id, comentario=None):
    # INC-87: o webhook de Done já deve encontrar o recibo, nunca a promessa anterior.
    # Se publicar falhar, não muda o estado sem a prova que o acompanha.
    # Repetição depois de falha no update não duplica o recibo: se ele já está na issue, só refaz o update.
    if comentario and not _ja_comentado(issue_id, comentario):
        comentar(issue_id, comentario)
    gql("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s}) { success } }",
        id=issue_id, s=estado_id)


def _ja_comentado(issue_id, texto):
    """O mesmo texto já está entre os comentários recentes da issue (leitura que falha conta como "não")."""
    try:
        r = gql("query($id: String!) { issue(id: $id) { comments(last: 50) { nodes { body } } } }", id=issue_id)
        nos = (((r or {}).get("issue") or {}).get("comments") or {}).get("nodes") or []
    except Exception:  # noqa: BLE001 — sem a leitura, publica (pior caso: um duplicado)
        return False
    return any((n.get("body") or "") == texto[:20000] for n in nos)


def comentar(alvo_id, texto, campo="issueId"):
    gql(f"mutation($id: String!, $b: String!) {{ commentCreate(input: {{{campo}: $id, body: $b}}) {{ success }} }}",
        id=alvo_id, b=texto[:20000])


# (lição 6): "o deploy ainda não subiu" nunca vira comentário (119 comentários em 14/10): o hub
# recusa e grava a espera `--no-ar`, que reabre a issue quando o commit subir.
RE_NAO_SUBIU = re.compile(r"n[ãa]o subiu|n[ãa]o est[áa] no ar|ainda n[ãa]o (?:foi )?(?:para o ar|publicad\w+)", re.I)
Q_PR_DA_ISSUE = ("query($i: String!) { issue(id: $i) { description comments(last: 40) { nodes { body createdAt } } } }")


def _pr_da_issue(ident):
    i = gql(Q_PR_DA_ISSUE, i=ident.upper())["issue"]
    textos = [i.get("description") or ""] + [c.get("body") or "" for c in sorted(i["comments"]["nodes"], key=lambda c: c["createdAt"])]
    achados = [m.group(0) for t in textos for m in esperas.RE_PR.finditer(t)]
    return f"https://{achados[-1]}" if achados else ""


def guarda_deploy(ident, texto, achar_pr=None, gravar=None, base_dados=None):
    """Comentário que diz "ainda não subiu" sem `voltar-quando --no-ar` registrado: devolve (código, mensagem) e o texto NÃO
    é postado; com PR achável a espera `--no-ar` é gravada na hora. Devolve None quando o comentário pode seguir."""
    if not RE_NAO_SUBIU.search(texto or ""):
        return None
    e = esperas.ler(ident, base_dados)
    if e and e.get("no_ar") is not None:
        return None
    try:
        m = esperas.RE_PR.search(texto)
        pr = f"https://{m.group(0)}" if m else (achar_pr or _pr_da_issue)(ident)
    except Exception:  # noqa: BLE001 — sem o Linear, cai na instrução manual
        pr = ""
    if pr:
        try:
            (gravar or esperas.gravar)(ident, pr, no_ar="")
        except ValueError:
            pr = ""
    if pr:
        return 0, (f"Comentário NÃO postado: \"ainda não subiu\" é do hub. Gravei `voltar-quando --no-ar` para {pr}: ele "
                   "confere o deploy sem você e reabre a issue quando subiu. Termine agora com uma linha: "
                   "\"Merge feito; volto quando estiver no ar.\"")
    return 1, (f"Comentário NÃO postado: \"ainda não subiu\" é do hub. Rode `python3 -m hub.linear voltar-quando {ident.upper()} "
               "--pr <url do PR> --no-ar` e termine com uma linha.")


def falar(alvo, texto, update=False, repasse=False):
    """O Claude fala na issue À VISTA: comentário do usuário-app no fio da issue (sem app: com a chave do dono e a
    marca). Nunca só como atividade do painel do agente, que fica recolhido e o dono não vê. repasse=True: fala de
    fora da execução da issue (o orquestrador), marcada com REPASSE para a execução ler como fala do dono."""
    if repasse:
        texto = f"{texto}\n\n_{REPASSE}._"
    if update:
        return comentar_update(alvo, f"{texto}\n\n_{MARCA}._")
    if token_app():
        try:
            gql("mutation($i: String!, $b: String!) { commentCreate(input: {issueId: $i, body: $b}) { success } }",
                app=True, i=alvo, b=texto[:20000])
            return
        except Exception:  # noqa: BLE001 — sem o app, cai na chave do dono
            pass
    comentar(alvo, f"{texto}\n\n_{MARCA}._")


def _arq_anexos(ident):
    return Path(data()) / "claude" / "anexos" / f"{ident.upper()}.jsonl"


def _sha(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def registrar_anexo(ident, link, texto=None, arquivo=None):
    """Guarda cada arquivo anexado (link e, se for texto curto, o conteúdo): a entrega final repete todos.
    `arquivo`: caminho local, para o envio automático ao WhatsApp (hub/entrega_zap.py) não precisar baixar."""
    arq = _arq_anexos(ident)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq, "a", encoding="utf-8") as f:
        f.write(json.dumps({"quando": _agora(), "link": link, "texto": texto,
                            **({"arquivo": str(Path(arquivo).resolve()),
                               "sha": _sha(arquivo)} if arquivo else {})}, ensure_ascii=False) + "\n")


def anexos_desde(ident, desde):
    """[(link, texto|None)] anexados desde `desde`, sem repetir link."""
    try:
        linhas = _arq_anexos(ident).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    vistos = {}
    for linha in linhas:
        with contextlib.suppress(ValueError, KeyError):
            r = json.loads(linha)
            if r["quando"] >= desde:
                vistos[r["link"]] = r.get("texto")
    return list(vistos.items())


def comentar_update(update_id, texto):
    """Update de projeto aceita uma conversa só: o primeiro comentário abre, os outros respondem a ele."""
    cs = gql("query($id: String!) { projectUpdate(id: $id) { comments(first: 50) { nodes { id createdAt parent { id } } } } }",
             id=update_id)["projectUpdate"]["comments"]["nodes"]
    raiz = next((c["id"] for c in _ordem(cs) if not c.get("parent")), None)
    if not raiz:
        return comentar(update_id, texto, "projectUpdateId")
    gql("mutation($u: String!, $p: String!, $b: String!) "
        "{ commentCreate(input: {projectUpdateId: $u, parentId: $p, body: $b}) { success } }",
        u=update_id, p=raiz, b=texto[:20000])


def atividade(sessao, tipo, corpo=None, efemera=False, **extra):
    """Atividade no painel do agente: thought, action (action/parameter/result), elicitation, response, error."""
    conteudo = {"type": tipo, **({"body": corpo[:20000]} if corpo is not None else {}), **extra}
    entrada = {"agentSessionId": sessao, "content": conteudo, **({"ephemeral": True} if efemera else {})}
    gql("mutation($i: AgentActivityCreateInput!) { agentActivityCreate(input: $i) { success } }", app=True, i=entrada)


def subir_arquivo(caminho):
    """Sobe um arquivo pro armazenamento do Linear (fileUpload + PUT) e devolve a URL para pôr no markdown."""
    import mimetypes
    p = Path(caminho)
    tipo = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    up = gql("mutation($t: String!, $n: String!, $s: Int!) { fileUpload(contentType: $t, filename: $n, size: $s) "
             "{ success uploadFile { uploadUrl assetUrl headers { key value } } } }",
             app=bool(token_app()), t=tipo, n=p.name, s=p.stat().st_size)["fileUpload"]["uploadFile"]
    cab = {"Content-Type": tipo, "Cache-Control": "public, max-age=31536000", **{h["key"]: h["value"] for h in up["headers"]}}
    urllib.request.urlopen(urllib.request.Request(up["uploadUrl"], p.read_bytes(), cab, method="PUT"), timeout=300)
    return up["assetUrl"]


def markdown_anexo(caminho, url):
    nome = Path(caminho).name
    return f"![{nome}]({url})" if Path(caminho).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") else f"[{nome}]({url})"


def abrir_sessao(issue_id):
    return gql("mutation($i: String!) { agentSessionCreateOnIssue(input: {issueId: $i}) { agentSession { id } } }",
               app=True, i=issue_id)["agentSessionCreateOnIssue"]["agentSession"]["id"]


# ---- tarefa ----

def _comentarios(nodes):
    return [f"{'Linear' if _eh_bot(c) else _quem(c) if _eh_fala(c) else 'Claude'}: {c['body']}" for c in _ordem(nodes)]


def item_da_issue(issue):
    """O que vai no item da fila (JSON) para remontar a tarefa em qualquer tick."""
    labels = {lb["name"].lower() for lb in issue["labels"]["nodes"]}
    modelo, por = roteamento.modelo(labels, issue["title"], issue.get("priority"))
    return {"tipo": "issue", "id": issue["id"], "identifier": issue["identifier"], "titulo": issue["title"],
            "descricao": issue.get("description") or "", "url": issue["url"], "modelo": modelo, "roteado_por": por,
            "repos": RE_REPO.findall((issue.get("project") or {}).get("description") or ""),
            "projeto": (issue.get("project") or {}).get("name") or "",
            "comentarios": _comentarios(issue["comments"]["nodes"]),
            # hora da última fala do dono (o disjuntor só conta rodadas depois dela)
            "ultima_fala": max([c["createdAt"] for c in issue["comments"]["nodes"] if _eh_fala(c)], default=None),
            # até quando a conversa acima foi lida: o que vier depois entra na largada (reler) ou no fim (falas_nao_lidas).
            # O comentário mais novo lido, não a hora atual: a fala entre a consulta e agora ficaria antes do corte
            # última mudança de estado que não seja o vaivém In Progress ↔ In Review (tipo "started") que o
            # próprio hub faz a cada rodada; Todo, Backlog, Done e Canceled contam
            "estado_desde": max([h["createdAt"] for h in ((issue.get("history") or {}).get("nodes") or [])
                                 if (h.get("toState") or {}).get("type") not in (None, "started")], default=None),
            "lido_ate": max([issue.get("createdAt") or ""] + [c["createdAt"] for c in issue["comments"]["nodes"]], key=_z) or _agora(),
            "estados": {s["name"]: s["id"] for s in issue["team"]["states"]["nodes"]},
            # plano despachado do PC (label plano-<slug>): o aviso no WhatsApp traz o progresso do plano inteiro
            "plano": next((lb for lb in sorted(labels) if lb.startswith("plano-")), None),
            "merge": "merge" in labels,
            "estado_tipo": (issue.get("state") or {}).get("type"),  # completed/canceled não leva merge pelo hub
            "labels": sorted(labels),  # entram na busca das receitas da biblioteca
            "skills": sorted(lb[6:] for lb in labels if lb.startswith("skill:")),  # skills da biblioteca ligadas
            "mcp": sorted(lb[4:] for lb in labels if lb.startswith("mcp:")),  # MCP pesado só sob demanda
            "executor": "codex" if labels & {"codex", "visual"} else "claude",  # visual vai ao Codex (dono, 16/10)
            "prioridade": issue.get("priority")}  # 1 = urgente: só ela leva Opus na reserva de API


def item_do_update(u):
    nome = (u.get("project") or {}).get("name", "?")
    return {"tipo": "update", "id": u["id"], "identifier": f"upd-{u['id'][:8]}", "titulo": f"update do projeto {nome}",
            "descricao": u.get("body") or "", "url": u["url"], "modelo": "sonnet",
            "repos": RE_REPO.findall((u.get("project") or {}).get("description") or ""),
            "comentarios": _comentarios(u["comments"]["nodes"]), "estados": {}}


CONVERSA_MAX = 12_000  # ~3 mil tokens; acima disso a conversa vira resumo de estado (era 40 mil)
RESUMO_MAX = 300
FALA_MAX = 600  # fala antiga (fora das 3 últimas) cortada no 2º passo
ULTIMA_MAX = 4_000  # a última resposta do Claude (o estado) no 3º passo
RE_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def _primeira(c, limite=RESUMO_MAX):
    primeira = (c.strip().splitlines() or [""])[0]
    return primeira[:limite - 40].rstrip() + " … (resumido; texto inteiro no Linear)"


def conversa_enxuta(comentarios):
    """(comentários, resumiu). Passou de CONVERSA_MAX, em passos até caber:
    1. os "Claude: " anteriores aos 2 últimos dele viram a primeira linha (até 300 caracteres) + aviso;
    2. avisos "Linear:" fora dos 2 últimos e o penúltimo do Claude viram a primeira linha; falas fora das 3 últimas
       ficam com FALA_MAX caracteres;
    3. a última resposta do Claude (o estado) fica com ULTIMA_MAX.
    As 3 últimas falas (dono, repasse, volta da espera) nunca são cortadas."""
    def total(cs):
        return sum(len(c) for c in cs)

    if total(comentarios) <= CONVERSA_MAX:
        return list(comentarios), False
    saida = list(comentarios)
    do_claude = [i for i, c in enumerate(comentarios) if c.startswith("Claude: ")]
    for i in do_claude[:-2]:
        saida[i] = _primeira(comentarios[i])
    if total(saida) > CONVERSA_MAX:
        avisos = [i for i, c in enumerate(saida) if c.startswith("Linear: ")]
        falas = [i for i, c in enumerate(saida) if not c.startswith(("Claude: ", "Linear: "))]
        for i in avisos[:-2] + do_claude[-2:-1]:
            saida[i] = _primeira(saida[i])
        for i in falas[:-3]:
            if len(saida[i]) > FALA_MAX:
                saida[i] = saida[i][:FALA_MAX].rstrip() + " … (cortado; inteiro no Linear)"
    if total(saida) > CONVERSA_MAX and do_claude and len(saida[do_claude[-1]]) > ULTIMA_MAX:
        saida[do_claude[-1]] = saida[do_claude[-1]][:ULTIMA_MAX].rstrip() + " … (cortado; inteiro no Linear)"
    return saida, True


def prs_citados(comentarios, n=5):
    """Os últimos n PRs citados na conversa (o resumo de estado da retomada os lista mesmo se o texto foi cortado)."""
    return list(dict.fromkeys(m for c in comentarios for m in RE_PR_URL.findall(c)))[-n:]


RE_PLANO = re.compile(r"writing-plans|brainstorming", re.I)


def precisa_altitude(item):
    """Issue de plano: label `plano-*` ou descrição que manda planejar/brainstorming."""
    return bool(item.get("plano") or RE_PLANO.search(item.get("descricao") or ""))


def tarefa(item):
    """Item linear da fila → Tarefa (com .linear = item)."""
    ident = item["identifier"]
    eh_issue = item.get("tipo", "issue") == "issue"
    corpo = [f"Pedido do {config.dono()} pelo Linear: {ident} · {item['titulo']}", item["url"], "", item["descricao"].strip()]
    if item["comentarios"]:
        comentarios, resumiu = conversa_enxuta(item["comentarios"])
        corpo += ["", f"## Conversa (do mais antigo ao mais novo; responda a última fala do {config.dono()})"]
        if resumiu:
            corpo.append("Conversa longa: respostas antigas do Claude resumidas; o estado está na última.")
            if prs := prs_citados(item["comentarios"]):
                corpo.append("PRs citados na conversa: " + ", ".join(prs))
        corpo += [f"- {c}" for c in comentarios]
    fixes = f' com "Fixes {ident}" na descrição (o Linear liga a issue ao PR)' if eh_issue else ""
    alvo = ident if eh_issue else f"--update {item['id']}"
    codex = item.get("executor") == "codex"
    if codex:
        corpo.append(CODEX.format(branch=ident.lower()))
    if eh_issue and precisa_altitude(item):
        corpo.append(ALTITUDE)
    ci = VOLTAR_QUANDO.format(id=ident, codigo=codigo()) if eh_issue else "\n- Espere o status do CI no PR em vez de repetir."
    corpo.append(COMO.format(dono=config.dono(), codigo=codigo(), id=ident, fixes=fixes, alvo=alvo, ci=ci))
    if eh_issue:
        from hub import memorias
        if instrucoes := memorias.instrucoes_da_issue(item):
            corpo += ["", "## Decisões vigentes do dono", instrucoes]
    if item.get("merge"):  # vem depois do COMO: vale mais que o "não faça merge" de lá
        corpo += ["", "## Merge liberado", f"Esta issue tem a label `merge` (o {config.dono()} liberou): com os testes dos arquivos e o CI (local-ci) verdes, "
                  "faça o merge e confira o deploy no ar. Siga a regra de branch do repo (cliente-c: base develop; sem local-ci, "
                  "a prova é `tsc` + `vitest` + `build` do combinado; a promoção develop → main é do orquestrador, GOVERNANCE regras 5 e 7).",
                  "Antes do merge, espere o comentário \"Revisão do Claude\" no PR (sai em até 15 min do último push; "
                  "`gh api repos/<org>/<repo>/issues/<n>/comments` (o `gh pr view --comments` falha por Projects classic)). Achado sério: corrija e faça push. Achado errado: responda no PR em uma "
                  "linha. " + ESPERA_FORA if codex else
                  "Antes do merge, espere o comentário \"Revisão do Codex\" no PR (sai em até 15 min do último push; "
                  "`gh api repos/<org>/<repo>/issues/<n>/comments` (o `gh pr view --comments` falha por Projects classic)). Achado sério: corrija e faça push (ele revisa de novo). Achado que você "
                  "julga errado: responda no PR por quê, em uma linha. " + ESPERA_FORA]
    from hub import main_vermelho  # import tardio: main_vermelho importa linear só dentro da função
    for repo in item["repos"]:
        with contextlib.suppress(Exception):
            mv = main_vermelho.aberto(repo)
            if mv and ident != mv["issue"]:
                corpo.append(f"\n**Main vermelho em {repo} ({mv['issue']}, teste `{mv['teste']}`):** não peça `/local-ci` nem mexa no "
                             f"PR por causa desse teste; o conserto é a {mv['issue']}. Seu PR volta sozinho quando o main passar.")
    if eh_issue:  # as até 3 receitas da biblioteca que se aplicam (índice híbrido, só acima do limiar); nunca falha
        from hub import biblioteca_uso
        bloco = biblioteca_uso.bloco_da_issue(item)
        if bloco:
            corpo += ["", bloco]
        try:
            import hub.busca as busca  # "já existe isto" (issues Done e planos parecidos), acima do limiar; nunca falha
        except ImportError as _e:  # a busca indexa memórias e issues nossas: fora do snapshot público
            if not faltou(_e, "hub.busca"):
                raise
            busca = None
        ja_existe = busca.bloco_da_issue(item) if busca else None
        if ja_existe:
            corpo += ["", ja_existe]
        try:
            import hub.wiki as wiki  # a página da wiki do assunto (estado atual com fonte e data), no lugar de trechos soltos; nunca falha
        except ImportError as _e:  # a wiki é do repo privado: fora do snapshot público
            if not faltou(_e, "hub.wiki"):
                raise
            wiki = None
        pagina = wiki.bloco_da_issue(item) if wiki else None
        if pagina:
            corpo += ["", pagina]
    t = T.Tarefa(f"linear-{ident.lower()}", "manual", 1, item["modelo"], T.timeout_segundos(TIMEOUT), item["repos"],
                 False, f"Linear {ident}: {item['titulo']}"[:200], "\n".join(corpo).strip())
    t.linear = item
    t.skills = item.get("skills") or []
    return t


ESPERA_FORA = ("Não espere o CI nem o comentário dentro da sessão por mais de 10 min: rode `voltar-quando` (acima) e "
               "termine; você volta com o local-ci e a revisão na conversa (ou 20 min depois do CI, sem comentário). Sem revisão em 2 h, revise o diff você mesmo e siga.")


def reabrir(ident, fala):
    """O "faz N" rodou a proposta da issue: ela volta para a fila com o resultado, sem o dono comentar."""
    item = item_da_issue(gql("query($i: String!) { issue(id: $i) {" + CAMPOS + "} }", i=ident)["issue"])
    if token_app():
        with contextlib.suppress(Exception):  # Linear recusou abrir sessão: segue por comentário
            item["sessao"] = abrir_sessao(item["id"])
    item["comentarios"].append(fala)
    return tarefa(item)


def item_por_ident(ident):
    return item_da_issue(gql("query($i: String!) { issue(id: $i) {" + CAMPOS + "} }", i=ident)["issue"])


def encerrar_sem_ia(item, estado, texto):
    """o hub fecha a volta da espera sem abrir sessão: comenta e move (se a coluna existe)."""
    if MARCA not in texto:  # sem a marca o comentário (chave do dono) pareceria fala dele e acordaria a issue (_eh_meu)
        texto = f"{texto}\n\n_{MARCA}._"
    if estado in item["estados"]:
        _mover_no_fim(item, estado, texto)
    else:
        comentar(item["id"], texto)


def _na_fila(item, rodando):
    """Avisa que entrou na fila: no painel do agente (thought) ou, sem sessão, por comentário."""
    if item.get("sessao"):
        atividade(item["sessao"], "thought", "Na fila: a primeira conta livre pega.")
    elif item["tipo"] == "update":
        comentar_update(item["id"], f"Na fila do {MARCA}.")
    else:
        comentar(item["id"], f"Na fila do {MARCA}.")


def sessoes_ok():
    """Sessões de agente disponíveis? Sem credenciais do app, ou o Linear recusando ("agent sessions disabled"):
    modo por comentário. Nunca derruba a ponte."""
    try:
        if not token_app():
            return False
        gql("{ agentSessions(first: 1) { nodes { id } } }", app=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def disparar_status(item, registro, fala):
    """O dono falou numa issue que está rodando: em vez de esperar a sessão terminar a etapa, um processo à parte
    lê o registro das ações (TAREFA_LOG) e responde em segundos o que já foi feito e o que está acontecendo. Uma vez
    por fala; item só na fila (sem pid) não precisa, porque a fala já entra no prompt."""
    if not registro.get("pid") or not fala.strip():
        return
    import hashlib
    marca = Path(data()) / "claude" / "status" / f"{item['identifier']}-{hashlib.sha1(fala.encode()).hexdigest()[:12]}"
    if marca.exists():
        return
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(fala[:500], encoding="utf-8")
    dados = {"item": {k: item[k] for k in ("tipo", "id", "identifier", "titulo") if k in item} | {"sessao": item.get("sessao")},
             "registro": registro, "fala": fala[:2000]}
    import subprocess
    subprocess.Popen([sys.executable, "-m", "hub.linear", "status", json.dumps(dados, ensure_ascii=False)], cwd=str(RAIZ),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def passos_do_registro(caminho, n=50):
    """Últimos passos do `claude -p` (stream-json): ferramentas usadas e o que o Claude escreveu, em linhas curtas."""
    passos = []
    try:
        linhas = Path(caminho).read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except (OSError, TypeError):
        return passos
    for linha in linhas:
        try:
            ev = json.loads(linha)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                passos.append(f"[{b.get('name')}] {_parametro(b.get('name'), b.get('input'))}")
            elif b.get("type") == "text" and b.get("text", "").strip():
                passos.append(f"(escreveu) {b['text'].strip()[:300]}")
    return passos[-n:]


def responder_status(dados, chamar=None):
    item, reg, fala = dados["item"], dados["registro"], dados["fala"]
    passos = passos_do_registro(reg.get("log"))
    prompt = (f"Você é o Claude da VPS trabalhando agora em {item['identifier']} ({item.get('titulo', '')}), desde "
              f"{reg.get('inicio')} (UTC), na conta {reg.get('conta', '1')}. O {config.dono()} acabou de escrever no Linear:\n\n"
              f"«{fala}»\n\nSeus últimos passos (o mais recente por último):\n" + ("\n".join(passos) or "(nenhum ainda)")
              + f"\n\nResponda ao {config.dono()} em 2 a 5 linhas, português direto: o que já ficou pronto, o que está fazendo "
              "agora e o que falta. Se ele pediu algo novo, diga que a sessão que está trabalhando vai ler e seguir. "
              "Não invente nada que não esteja nos passos.")
    texto = (chamar or _claude_rapido)(prompt)
    if not texto:
        return
    if item.get("tipo") == "update":
        comentar_update(item["id"], f"{texto}\n\n_{MARCA}._")
    else:
        falar(item["identifier"], texto)  # à vista na issue (e notifica o celular), nunca só no painel


def _claude_rapido(prompt, modelo="haiku"):
    """Chamada curta e barata (sem ferramentas), fora da trava da conta, como a faixa rápida do WhatsApp."""
    import subprocess
    b = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
    env = dict(os.environ)
    try:  # sai da conta mais folgada na janela de 5 h (antes saía sempre da 1, mesmo com a tarefa na 2 ou na 3)
        from hub import agenda
        carga = agenda.carga_contas()
        n = min((c for c in carga if c == "1" or (agenda.HOME / f".claude-{c}" / ".credentials.json").exists()),
                key=lambda c: carga[c])
        env.pop("CLAUDE_CONFIG_DIR", None)
        if n != "1":
            env["CLAUDE_CONFIG_DIR"] = str(agenda.HOME / f".claude-{n}")
    except Exception:  # noqa: BLE001 — sem dado de carga: a conta do processo
        pass
    r = subprocess.run([b if Path(b).exists() else "claude", "-p", "--model", modelo, "--tools", "",
                        "--no-session-persistence", "--output-format", "json"], input=prompt, capture_output=True,
                       text=True, encoding="utf-8", timeout=120, env=env)
    try:
        return str(json.loads(r.stdout).get("result") or "").strip()
    except ValueError:
        return ""


def _para_in_progress(issue, item):
    """Só tira de Todo/Backlog. Conversa numa issue já em andamento ou em revisão não mexe na coluna: cada mudança
    de coluna é uma notificação no celular do dono."""
    if (issue.get("state") or {}).get("type") in ("unstarted", "backlog", "triage", None):
        mover(item["id"], item["estados"]["In Progress"])


def puxar(rodando=None, aceitar=None, log=print, varredura=None):  # noqa: C901
    """Tudo que pede resposta no Linear → tarefas. rodando: {nome: registro} do que está na fila ou rodando (não puxa
    de novo; a execução em curso lê a fala nova com `novidades`). aceitar(t) põe na fila NA HORA e devolve se entrou;
    só então o Linear é avisado ("Na fila", In Progress). Erro num item não derruba os outros. Sem chave: []."""
    if not chave():
        return []
    rodando = rodando or {}
    aceitar = aceitar or (lambda t: True)
    novas, vistas = [], set()
    # com o webhook vivo o tick só consulta as issues dos eventos (ids); varredura completa (None) a cada 15 min
    # ou quando o webhook cala. Sem evento nenhum, o tick não chama a API.
    ids = linear_uso.plano_tick() if varredura is None else (None if varredura else [])
    if ids is not None and not ids:
        with contextlib.suppress(Exception):
            enviar_resumo_zap()
        return []
    # Com credenciais do app, a lista de sessões é obrigatória: sem ela uma fala com @Claude viraria tarefa duas vezes
    # (uma pela sessão, outra pelo comentário). Se falhar, este tick não puxa nada (gql levanta; o próximo tenta).
    com_app = bool(token_app())
    with contextlib.suppress(Exception):
        enviar_resumo_zap()  # resumo das entregas que esperavam o intervalo
    try:  # frente que ficou no Backlog sobe quando a anterior fecha (nunca segura nenhuma)
        sequenciar_frentes(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"linear: frentes em sequência não conferidas: {type(e).__name__}: {str(e)[:150]}")
    try:
        liberar_fases_finais(log=log)  # a revisão final de um plano entra em Todo quando o resto ficou pronto
    except Exception as e:  # noqa: BLE001
        log(f"linear: fase final não conferida: {type(e).__name__}: {str(e)[:150]}")
    sessoes = gql(Q_SESSOES, app=True)["agentSessions"]["nodes"] if com_app else []
    ultima_sessao = {}  # issue id → updatedAt mais recente das sessões dela
    for s in sessoes:
        if s.get("issue"):
            iid = s["issue"]["id"]
            ultima_sessao[iid] = max(ultima_sessao.get(iid, ""), s["updatedAt"])

    def oferecer(t, item, issue=None, anunciar=True):
        if not aceitar(t):
            linear_uso.hook_devolver([item.get("id")])  # fila cheia ou pausa: o próximo tick olha de novo (sem esperar 15 min)
            return
        novas.append(t)
        if anunciar:
            try:
                if issue is not None:
                    _para_in_progress(issue, item)
                _na_fila(item, rodando)
            except Exception as e:  # noqa: BLE001 — já está na fila: o aviso no Linear é só cortesia
                log(f"{t.nome}: na fila, mas o aviso no Linear falhou: {type(e).__name__}: {str(e)[:150]}")

    for s in sessoes:
        try:
            if not s.get("issue"):
                continue
            iid = s["issue"]["id"]
            # issue cancelada não roda nem pela sessão do agente (cancelada pelo dono e rodou mesmo assim)
            if (s["issue"].get("state") or {}).get("type") == "canceled":
                continue
            if iid in vistas or not sessao_quer_resposta(s):
                continue
            vistas.add(iid)
            item = item_da_issue(s["issue"])
            item["sessao"] = s["id"]
            item["comentarios"] += [f"{config.dono(True)} (no painel do agente): {a['content']['body']}" for a in _prompts(s)]
            item["ultima_fala"] = max([x for x in [item.get("ultima_fala")] + [a["createdAt"] for a in _prompts(s)] if x],
                                      default=None)
            item["lido_ate"] = max([item["lido_ate"]] + [a["createdAt"] for a in _prompts(s)], key=_z)
            t = tarefa(item)
            if t.nome in rodando:  # a execução em curso lê com `novidades` (e vê o stop sozinha)
                if rodando[t.nome].get("pid") and _prompts(s) and not parar_pedido(s):
                    disparar_status(item, rodando[t.nome], _prompts(s)[-1]["content"]["body"])
                continue
            if parar_pedido(s):
                atividade(s["id"], "response", "Parado. Me chame de novo quando quiser.")
                continue
            oferecer(t, item, s["issue"])
        except Exception as e:  # noqa: BLE001
            log(f"linear: sessão {s.get('id', '?')[:8]} pulada: {type(e).__name__}: {str(e)[:150]}")
    if ids is None:
        issues = gql(Q_TODO)["issues"]["nodes"] + [i for i in gql(Q_CONVERSA)["issues"]["nodes"] if quer_resposta(i)]
    else:
        try:
            doevento = gql(Q_IDS, ids=ids[:50])["issues"]["nodes"]
        except Exception:
            linear_uso.hook_devolver(ids)
            raise
        linear_uso.hook_devolver(ids[50:])
        issues = [i for i in doevento if i["state"]["type"] == "unstarted" or quer_resposta(i)]
    _licoes_na_hora(issues if ids is None else doevento, log)
    for issue in issues:
        try:
            if issue["id"] in vistas:
                continue
            vistas.add(issue["id"])
            cs = _ordem(issue["comments"]["nodes"])
            if issue["id"] in ultima_sessao and (not cs or ultima_sessao[issue["id"]] >= cs[-1]["createdAt"]):
                continue  # já tem sessão cobrindo esta fala (ex.: comentário com @Claude, ou a issue sem comentário)
            item = item_da_issue(issue)
            t = tarefa(item)
            if t.nome in rodando:
                # em Todo a issue vem do Q_TODO sem passar por quer_resposta: o último comentário pode ser o próprio
                # status, e responder a ele vira loop (~40 comentários de status em 15 min)
                if rodando[t.nome].get("pid") and cs and not _eh_meu(cs[-1]):
                    disparar_status(item, rodando[t.nome], cs[-1]["body"])
                continue
            if com_app:
                try:
                    item["sessao"] = abrir_sessao(item["id"])
                    t = tarefa(item)
                except Exception:  # noqa: BLE001 — Linear recusou abrir sessão: segue por comentário
                    pass
            oferecer(t, item, issue)
        except Exception as e:  # noqa: BLE001
            log(f"linear: {issue.get('identifier', '?')} pulada: {type(e).__name__}: {str(e)[:150]}")
    for u in (gql(Q_UPDATES)["projectUpdates"]["nodes"] if ids is None else []):
        try:
            if update_quer_resposta(u):
                item = item_do_update(u)
                t = tarefa(item)
                if t.nome in rodando:
                    cs = _ordem(u["comments"]["nodes"])
                    if rodando[t.nome].get("pid"):
                        disparar_status(item, rodando[t.nome], cs[-1]["body"] if cs else u.get("body") or "")
                    continue
                oferecer(t, item)
        except Exception as e:  # noqa: BLE001
            log(f"linear: update {u.get('id', '?')[:8]} pulado: {type(e).__name__}: {str(e)[:150]}")
    return novas


def _licoes_na_hora(issues, log=print):
    """fala nova do dono com correção vira incidente P1 no tick em que chega, sem esperar a retro. Nunca derruba o tick."""
    try:
        try:
            import hub.licoes_laco as licoes_laco
        except ImportError as e:  # snapshot público: o laço de lições fica fora
            if not faltou(e, "hub.licoes_laco"):
                raise  # dependência quebrada: vai ao log abaixo, como antes
            return
        licoes_laco.fala_na_hora(issues, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"linear: lições na hora falharam: {type(e).__name__}: {str(e)[:150]}")


MAX_CONFERENCIAS = 1
MAX_PORTOES = 2  # voltas pelo portão de Done antes de deixar a issue em In Progress
SESSAO_AUTO = "This thread is for an agent session"
# A conferência cobrava o pedido ao pé da letra ("build verde") contra o COMO, que proíbe o build local.
REGRAS_CONFERENCIA = (
    "Regras fixas da casa (valem mais que o texto do pedido): build local (`next build`), suíte completa e "
    "`scripts/ci-local.sh` à mão são proibidos: são do CI (local-ci, que só o runner roda) e do deploy; vermelho por "
    "timeout com carga alta se resolve com `/local-ci` comentado no PR (ou commit novo) e nova espera, não rodando o CI local; o Claude roda só a checagem de tipos e os testes dos arquivos mudados, e a prova vale como "
    "CI verde ou deploy no ar conferido (curl 200, versão nova). Exceção: cliente-c e clientee-moraes-site não têm local-ci e "
    "exigem um build local (no asite01, `tsc` + `vitest` + `build`) como prova antes do merge: ali o build local é prova válida. "
    "Merge: no cliente-c com a label `merge` (tsc, vitest e build passando, Codex sem achado sério; PR #219 de 15/10) e no "
    "clienteh (`gh pr merge --squash` com local-ci verde, também com a label `merge`) a sessão faz o merge; fora dessas regras do repo é proposta "
    "`pr_merge`. Nunca inventar URL, link ou dado. Cobrar o que essas regras proíbem (build local onde há local-ci, "
    "suíte inteira, rodar o local-ci à mão, merge direto fora da regra do repo, link que não existe) não é falta: nesse caso a entrega está completa.")


def conferir(item, texto, chamar=None, estrito=False, extra=None):
    """Conferência automática: outro Claude lê o pedido e a entrega e diz se faltou algo que o dono pediu.
    Devolve o que faltou (texto curto) ou "" se a entrega está completa (ou se a conferência falhou)."""
    falas = [c for c in item.get("comentarios", []) if SESSAO_AUTO not in c]
    pedido = "\n".join([item.get("titulo", ""), item.get("descricao", "")] + falas[-6:])
    prompt = (f"Você confere entregas do Claude da VPS para o {config.dono()}, que odeia ter que cobrar o óbvio. Pedido (com a "
              f"conversa):\n«{pedido[-6000:]}»\n\nEntrega final do Claude:\n«{texto[-6000:]}»\n\nFaltou algo que o {config.dono()} "
              "pediu ou obviamente esperaria? Exemplos: disse que gerou arquivo/vídeo/imagem e não há link anexado; "
              "pediu para publicar/postar e só gerou; pediu PR e não há link; respondeu outra coisa; deixou passo para o "
              f"{config.dono()} fazer que o Claude podia fazer. Pergunta simples bem respondida = completo. {REGRAS_CONFERENCIA} "
              "Código, testes e merge não provam execução de coleta, classificação, backfill ou piloto. "
              "Quando o pedido exige dados processados, gabarito ou medições, cobre os resultados reais, "
              "a fonte e os números; comandos para rodar depois e promessa de fase futura são pendências. "
              "Considere mudanças de escopo na conversa e não cobre massa quando só o piloto foi autorizado. "
              "Responda APENAS um JSON: "
              '{"completo": true|false, "faltou": "o que falta, em uma frase", "mecanica": true|false} '
              "(mecanica = o que falta é só VERIFICAR algo que um script confere: o PR foi mergeado, está no ar, a URL "
              "responde 200; false se falta fazer, escrever, anexar ou decidir algo)")
    try:
        r = (chamar or (lambda p: _claude_rapido(p, modelo="sonnet")))(prompt)
        j = json.loads(re.search(r"\{.*\}", r, re.S).group(0))
        if estrito and (type(j.get("completo")) is not bool or
                        (not j["completo"] and not str(j.get("faltou") or "").strip())):
            raise ValueError("conferência sem veredito válido")
        if isinstance(extra, dict):  # o que falta é pergunta de código? (o chamador tenta sem sessão)
            extra["mecanica"] = (not j.get("completo")) and j.get("mecanica") is True
        return "" if j.get("completo") else str(j.get("faltou") or "").strip()[:500]
    except Exception:  # noqa: BLE001
        if estrito:  # recibo: erro de leitura segue pendente, nunca aprova Done
            raise
        return ""


def falas_nao_lidas(item, inicio):
    """Falas do dono feitas DEPOIS que a conversa do item foi lida (`lido_ate`: ao montar o item, renovado na largada
    por `reler`; item antigo sem ele: o `inicio`) e depois da última vez que a execução leu (`novidades`/`perguntar`
    gravam a hora em claude/visto/<id>). Cortar pelo `inicio` perdia a fala entre enfileirar e começar."""
    if item.get("tipo", "issue") != "issue":
        return []
    try:
        visto = (Path(data()) / "claude" / "visto" / item["identifier"].upper()).read_text(encoding="utf-8").strip()
    except OSError:
        visto = ""
    return _falas_depois(item, max(_z(item.get("lido_ate") or inicio), _z(visto)))


def _falas_depois(item, corte):
    return [c for c in _falas(item["identifier"], False, item.get("sessao")) if _z(c["createdAt"]) > _z(corte)]


def reler(t):
    """Na largada: as falas que chegaram depois que o item entrou na fila vão para o prompt (o item guarda a conversa
    do momento em que foi enfileirado). Mexe em t (prompt e .linear). Erro de leitura: segue com a cópia da fila, e o
    fim da execução pega a fala (falas_nao_lidas corta pelo mesmo lido_ate)."""
    item = getattr(t, "linear", None)
    if not item or item.get("tipo", "issue") != "issue" or not item.get("lido_ate"):
        return t
    agora = _agora()
    try:  # sem o `visto`: outra execução da mesma issue pode ter lido a fala, esta não
        novas = _falas_depois(item, item["lido_ate"])
    except Exception:  # noqa: BLE001 — sem o Linear agora: roda com o que tem
        return t
    ja = set(item.get("comentarios") or [])
    linhas = [f"{_quem(c)} (depois de entrar na fila): {c['body']}" for c in novas]
    item = {**item, "lido_ate": agora, "comentarios": (item.get("comentarios") or []) + [x for x in linhas if x not in ja]}
    if novas:
        item["ultima_fala"] = max([x for x in [item.get("ultima_fala"), *(c["createdAt"] for c in novas)] if x])
    nova = tarefa(item)
    t.linear, t.prompt = nova.linear, nova.prompt
    return t


def _z(iso):
    """ISO em UTC comparável como texto (aceita Z e +00:00)."""
    return (iso or "").replace("+00:00", "Z")[:19]


def marcar_visto(ident):
    arq = Path(data()) / "claude" / "visto" / ident.upper()
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(_agora(), encoding="utf-8")


def concluir(t, reg, conferir_fn=None, log=print):
    """Depois da execução: confere a entrega (faltou algo → volta para a fila com o que faltou, até
    MAX_CONFERENCIAS vezes; devolve a tarefa nova para a agenda enfileirar), senão responde no painel (ou
    comentário) e move a issue; se o dono falou durante a execução e ninguém leu, devolve a tarefa com a fala.
    Limite de uso: não mexe."""
    if not reg or reg.get("limite") or reg.get("interrompida"):  # voltou para a fila: ninguém é avisado
        return None
    try:
        novas_falas = falas_nao_lidas(t.linear, reg.get("inicio"))
    except Exception:  # noqa: BLE001
        novas_falas = []
    entregue = _concluir(t, reg, conferir_fn, log)
    if entregue is not None:  # refação da conferência: a fala nova vai junto
        if novas_falas:
            entregue.linear["comentarios"] += [f"{_quem(c)}: {c['body']}" for c in novas_falas]
            entregue.linear["lido_ate"] = max(_z(c["createdAt"]) for c in novas_falas) + "Z"
            entregue = tarefa(entregue.linear)
        return entregue
    if novas_falas and t.linear.get("releituras", 0) < 3:
        log(f"{t.nome}: o {config.dono()} falou durante a execução ({len(novas_falas)} fala(s)); volta para responder")
        item = {**t.linear, "releituras": t.linear.get("releituras", 0) + 1, "conferencias": 0,
                "lido_ate": max(_z(c["createdAt"]) for c in novas_falas) + "Z",
                "comentarios": t.linear.get("comentarios", []) + [f"{_quem(c)} (durante a execução anterior): {c['body']}"
                                                                   for c in novas_falas]}
        return tarefa(item)
    return None


def por_que_volta(item):
    """Motivo curto da volta que `concluir` devolveu (vai para a fila e vira o tipo da execução)."""
    ultimo = str((item.get("comentarios") or [""])[-1])
    if ultimo.startswith("Conferência automática"):
        return "conferência"
    if "(durante a execução anterior)" in ultimo:
        return "fala durante a execução"
    return "promessa sem gancho" if item.get("promessas") else "conferência"


def _concluir(t, reg, conferir_fn=None, log=print):
    item = t.linear
    try:
        texto = Path(reg["saida"]).read_text(encoding="utf-8").strip()
    except (OSError, KeyError, TypeError):
        texto = reg.get("erro") or ""
    conta = f"conta {reg.get('conta', '1')}, {reg.get('duracao_s', 0) / 60:.0f} min"
    esperando = False
    if item.get("tipo", "issue") == "issue":
        with contextlib.suppress(Exception):  # terminou com `voltar-quando`: a entrega é "volto quando o CI sair"
            esperando = bool(reg.get("ok")) and esperas.gravada_desde(item["identifier"], reg.get("inicio"))
    from hub import parada, portao, recibo
    promessa = (reg.get("ok") and not esperando and item.get("tipo", "issue") == "issue" and parada.promessa_sem_gancho(texto)
                and not recibo._proposta_pendente(item["identifier"]) and item.get("promessas", 0) < 1)
    if promessa:
        # promessa com PR citado: o hub grava o `voltar-quando` por código, sem abrir sessão só para isso
        with contextlib.suppress(Exception):
            if retomada.gancho_mecanico(item["identifier"], texto):
                esperas.registrar_sem_ia(item["identifier"], "promessa", "", None, None)
                esperando, promessa = True, False
    if promessa:
        # (14/10): terminou dizendo "o agente está rodando, aguardo"; nada roda depois da sessão e a issue
        # ficou 4 h parada. Promessa sem gancho (sem voltar-quando nem Pedido) não é entrega: volta agora.
        novo = {**item, "promessas": item.get("promessas", 0) + 1,  # contador próprio: a conferência real ainda roda
                "comentarios": item.get("comentarios", []) + [f"Claude: {texto[-3000:]}", f"{config.get('produto.nome')}: {parada.MUDOU}."]}
        return tarefa(novo)
    if reg.get("ok") and not esperando and item.get("tipo", "issue") == "issue":
        # issue de dado só entrega com "Prova de execução"; sem ela volta (2 vezes) e depois fica em In Progress
        with contextlib.suppress(Exception):
            p = portao.avaliar(item, texto, criar_fn=_criar_execucao(item))
            if p["execucao"]:
                texto = f"{texto}\n\nPortão de Done (KRN-973): o código não roda em massa; abri a issue de execução {p['execucao']}."
            if not p["ok"]:
                if item.get("portoes", 0) < MAX_PORTOES:
                    return tarefa({**item, "portoes": item.get("portoes", 0) + 1,
                                   "comentarios": item.get("comentarios", []) + [
                                       f"Claude: {texto[-3000:]}", f"Portão de Done (faça agora, KRN-973): {p['motivo']}"]})
                texto = f"{texto}\n\nPortão de Done (KRN-973): não fecha, {p['motivo']}. Fica em In Progress."
                esperando = True  # a issue fica em In Progress com o motivo no comentário
    if (reg.get("ok") and not esperando and item.get("tipo", "issue") == "issue"
            and item.get("conferencias", 0) < MAX_CONFERENCIAS):
        extra = {}
        faltou = (conferir_fn or (lambda i, t: conferir(i, t, extra=extra)))(item, texto)
        if faltou and extra.get("mecanica"):  # pergunta de código (PR entrou, no ar, URL 200): o recibo responde
            m = retomada.conferir_mecanica(item, texto)
            if m["resultado"] in ("ok", "esperar"):
                with contextlib.suppress(Exception):
                    esperas.registrar_sem_ia(item["identifier"], "conferencia", "", None, None)
                esperando, faltou = m["resultado"] == "esperar", ""
            elif m["resultado"] == "falha":
                faltou = "; ".join(m["faltas"])[:500]  # o motivo já vem pronto do recibo
        if faltou:  # refaz em silêncio (só o painel do agente mostra): notificação, só na entrega final
            if item.get("sessao"):
                with contextlib.suppress(Exception):
                    atividade(item["sessao"], "thought", f"Conferência automática: faltou {faltou} Voltei para fazer.")
            novo = {**item, "conferencias": item.get("conferencias", 0) + 1,
                    "comentarios": item.get("comentarios", []) + [f"Claude: {texto[-3000:]}",
                                                                   f"Conferência automática (faça agora): faltou {faltou}"]}
            return tarefa(novo)
    falha = f"Falhou ({conta}):\n\n```\n{texto[-3000:]}\n```\n\nFale comigo aqui para tentar de novo."
    # todo arquivo anexado nesta execução vai junto na entrega, mesmo que o texto final esqueça de citar
    for link, conteudo in anexos_desde(item["identifier"], reg.get("inicio") or ""):
        if conteudo and conteudo[:200] not in texto and len(texto) + len(conteudo) < 18000:
            texto = f"{texto}\n\n---\n{conteudo}\n\nArquivo: {link}"
        elif link.split("](")[-1] not in texto:
            texto = f"{texto}\n\nArquivo: {link}"
    avisar_zap(item, texto, reg.get("ok"))
    if item.get("sessao") and f"PARADO pelo {config.dono()}" in texto:
        atividade(item["sessao"], "response", f"Parei, como você pediu ({conta}). O que já estava feito ficou no repo.")
    elif item.get("sessao"):
        # UMA notificação: a response do agente vira o comentário da entrega na issue (o Linear posta sozinho)
        atividade(item["sessao"], "response" if reg.get("ok") else "error",
                  f"{texto}\n\n_{conta}._" if reg.get("ok") else falha)
        _mover_no_fim(item, ("In Progress" if esperando else "In Review") if reg.get("ok") else "Backlog")
    elif item.get("tipo") == "update":
        corpo = texto if reg.get("ok") else f"Falhou:\n\n```\n{texto[-3000:]}\n```"
        comentar_update(item["id"], f"{corpo}\n\n_{MARCA} ({conta})._")
    elif reg.get("ok"):
        _mover_no_fim(item, "In Progress" if esperando else "In Review", f"{texto}\n\n_{MARCA} ({conta})._")
    else:
        _mover_no_fim(item, "Backlog", f"{falha.replace('Falhou (', f'Falhou ({MARCA}, ', 1)}")


RESUMO_ZAP_H = 3  # o dono, 09/10/2026: "vc tá me spammando": tarefa do Linear não avisa uma a uma, vai num resumo


def _fila_zap():
    return Path(data()) / "claude" / "avisos-linear.jsonl"


ENTRADA_ZAP = Path(os.environ.get("LINEAR_ENTRADA_ZAP", "/opt/asite01-wpp/data/linear-entrada"))


def entregar_respostas_zap(entrada=None, comentar_fn=None, log=print):
    """Respostas do dono pelo WhatsApp ("avisei", ou respondendo em cima do aviso; o bot grava em
    linear-entrada/*.json): vira comentário dele na issue (chave do dono = fala dele, acorda o Claude). Falha de rede
    deixa o arquivo para o próximo tick. Devolve as issues comentadas."""
    entrada, comentar_fn = Path(entrada or ENTRADA_ZAP), comentar_fn or comentar
    feitas = []
    for arq in sorted(entrada.glob("*.json")) if entrada.exists() else []:
        try:
            r = json.loads(arq.read_text(encoding="utf-8"))
            if not re.fullmatch(r"KRN-\d{1,6}", str(r.get("issue", ""))) or not str(r.get("texto", "")).strip():
                arq.unlink()
                continue
            comentar_fn(r["issue"], str(r["texto"])[:5000])
            arq.unlink()
            feitas.append(r["issue"])
        except ValueError:
            arq.unlink()  # JSON quebrado: descarta
        except Exception as e:  # noqa: BLE001
            log(f"linear: resposta do WhatsApp para {arq.name} não entregue: {type(e).__name__}: {str(e)[:150]}")
    return feitas


def avisar_zap(item, texto, ok, avisar_fn=None):
    """Tarefa do Linear que termina entra na fila do resumo do WhatsApp (enviar_resumo_zap junta tudo numa mensagem
    a cada RESUMO_ZAP_H horas). Nunca derruba a entrega."""
    try:
        linha = {"ident": item["identifier"], "titulo": item.get("titulo", "")[:80], "ok": bool(ok), "url": item.get("url", "")}
        if item.get("plano"):
            with contextlib.suppress(Exception):
                fim = progresso_plano(item["plano"], item["identifier"], ok)
                if fim.startswith("🏁"):
                    linha["plano"] = fim
        arq = _fila_zap()
        arq.parent.mkdir(parents=True, exist_ok=True)
        with arq.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
        enviar_resumo_zap(avisar_fn=avisar_fn)
    except Exception:  # noqa: BLE001 — aviso nunca derruba a entrega
        pass


def enviar_resumo_zap(agora_=None, avisar_fn=None, intervalo_h=RESUMO_ZAP_H):
    """Uma mensagem com as tarefas do Linear que FALHARAM desde o último resumo (uma linha por issue, a última situação
    vale), no máximo a cada `intervalo_h` horas. Tarefa pronta não avisa. Devolve o texto enviado ou None."""
    arq = _fila_zap()
    marca = arq.with_name("avisos-linear.ultimo")
    agora_ = agora_ or time.time()
    try:
        ultimo = float(marca.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ultimo = 0.0
    if agora_ - ultimo < intervalo_h * 3600 or not arq.exists():
        return None
    itens = {}
    for l in arq.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            j = json.loads(l)
            itens[j["ident"]] = j
    if not itens:
        return None
    # o dono, 13/10/2026 ("pq esse louco tá me spammando isso?"): tarefa pronta não vai ao WhatsApp (as com label merge já
    # entraram sozinhas; o resto está no Linear). Só falha avisa; sem falha, a fila esvazia em silêncio.
    falhas = [j for j in itens.values() if not j.get("ok")]
    if not falhas:
        arq.write_text("", encoding="utf-8")
        return None
    linhas = [f"❌ Linear: {len(falhas)} tarefa(s) com falha"]
    linhas += [f"{j['ident']} · {j['titulo']}" for j in falhas]
    texto = "\n".join(linhas)
    if avisar_fn is None:
        from hub import conta
        avisar_fn = conta.avisar
    avisar_fn(texto)  # "tarefa com falha" o porteiro desvia ao orquestrador
    marca.write_text(str(agora_), encoding="utf-8")
    arq.write_text("", encoding="utf-8")
    return texto


def progresso_plano(label, atual=None, ok=False, consulta=None):
    """Linha de progresso do plano (issues com a label plano-<slug>). Pronta = Done, Canceled ou In Review (o Claude
    acabou; falta o olho do dono). A issue `atual` ainda está In Progress na hora do aviso: conta pelo `ok`."""
    nos = (consulta or gql)('query($l: String!) { issues(first: 100, filter: {labels: {name: {eqIgnoreCase: $l}}}) '
                            '{ nodes { identifier state { type name } } } }', l=label)["issues"]["nodes"]
    pronta = [n["state"]["type"] in ("completed", "canceled") or n["state"]["name"] == "In Review"
              or (n["identifier"] == atual and ok) for n in nos]
    rodando = sum(1 for n, p in zip(nos, pronta) if not p and n["state"]["type"] == "started")
    feitas, total = sum(pronta), len(nos)
    if total and feitas == total:
        return f"🏁 Plano {label} concluído ({feitas}/{total})"
    return f"Plano {label}: {feitas}/{total} prontas, {rodando} rodando, {total - feitas - rodando} na fila"


Q_FINAIS = ('{ issues(first: 20, filter: {labels: {name: {eqIgnoreCase: "fase-final"}}, state: {type: {eq: "backlog"}}}) '
            '{ nodes { id identifier labels { nodes { name } } team { states { nodes { id name } } } } } }')


def liberar_fases_finais(consulta=None, mover_fn=None, log=print):
    """Issue com a label `fase-final` espera no Backlog até todas as outras do mesmo plano (plano-<slug>) estarem
    prontas (Done, Canceled ou In Review); aí vai para Todo e a primeira conta livre pega (revisão final, merge,
    polimento). Devolve os identificadores liberados."""
    consulta, mover_fn = consulta or gql, mover_fn or mover
    liberadas = []
    for i in consulta(Q_FINAIS)["issues"]["nodes"]:
        plano = next((lb["name"] for lb in i["labels"]["nodes"] if lb["name"].lower().startswith("plano-")), None)
        if not plano:
            continue
        nos = consulta('query($l: String!) { issues(first: 100, filter: {labels: {name: {eqIgnoreCase: $l}}}) '
                       '{ nodes { identifier state { type name } labels { nodes { name } } } } }', l=plano)["issues"]["nodes"]
        outras = [n for n in nos if not any(lb["name"].lower() == "fase-final" for lb in n["labels"]["nodes"])]
        prontas = all(n["state"]["type"] in ("completed", "canceled") or n["state"]["name"] == "In Review" for n in outras)
        if outras and prontas:
            mover_fn(i["id"], next(s["id"] for s in i["team"]["states"]["nodes"] if s["name"] == "Todo"))
            liberadas.append(i["identifier"])
            log(f"{i['identifier']}: fase final do {plano} liberada (as outras {len(outras)} estão prontas)")
    return liberadas


# ---- frentes de um plano: o hub só solta a seguinte (trava de ordem removida, o dono 17/10: em paralelo) ----

RE_FRENTE = re.compile(r"\bfrente\s*(\d+)\b", re.I)
SEGURA_FRENTE = "Frente em sequência (hub)"
RE_SEGURA = re.compile(rf"^_{re.escape(SEGURA_FRENTE)}\._$", re.M)
FRENTE_VELHA = dt.timedelta(days=7)  # frente anterior fechada há mais que isso: o plano foi abandonado, o hub não solta
Q_FRENTES = ('query($a: String) { issues(first: 100, after: $a, filter: {labels: {name: {startsWithIgnoreCase: "plano-"}}, '
             'state: {type: {in: ["backlog", "unstarted", "started", "completed", "canceled"]}}, '
             'updatedAt: {gt: "-P30D"}}) { nodes { id identifier title completedAt state { type name } '
             'labels { nodes { name } } comments(last: 10) { nodes { body } } '
             'team { states { nodes { id name } } } } pageInfo { hasNextPage endCursor } } }')


def numero_da_frente(titulo):
    m = RE_FRENTE.search(titulo or "")
    return int(m.group(1)) if m else None


def _issues_de_planos(consulta):
    todas, depois = [], None
    while True:
        r = consulta(Q_FRENTES, a=depois)["issues"]
        todas += r["nodes"]
        pg = r.get("pageInfo") or {}
        if not pg.get("hasNextPage") or not pg.get("endCursor"):
            return todas
        depois = pg["endCursor"]


def sequenciar_frentes(consulta=None, mover_fn=None, log=print, agora=None):
    """Frentes rodam em PARALELO (o dono, 17/10: "pode mandar tudo em paralelo"); o hub nunca devolve frente ao Backlog.
    Só solta: frente N no Backlog com a anterior fechada (Done ou Canceled), o hub move para Todo (só a que o próprio hub segurou, ou se a anterior
      fechou há menos de 7 dias; `proposta` fica com o orquestrador).
    Devolve ([devolvidas], [liberadas])."""
    consulta, mover_fn = consulta or gql, mover_fn or mover
    agora = agora or dt.datetime.now(dt.timezone.utc)
    planos = {}
    for i in _issues_de_planos(consulta):
        n = numero_da_frente(i["title"])
        labels = {lb["name"].lower() for lb in i["labels"]["nodes"]}
        for plano in (lb for lb in labels if lb.startswith("plano-")):
            if n is not None:
                planos.setdefault(plano, {}).setdefault(n, []).append({**i, "_labels": labels})
    devolvidas, liberadas = [], []
    for plano, frentes in sorted(planos.items()):
        ordem = sorted(frentes)
        for pos, n in enumerate(ordem[1:], 1):
            anteriores = frentes[ordem[pos - 1]]
            fechada = all(a["state"]["type"] in ("completed", "canceled") for a in anteriores)
            for i in frentes[n]:
                tipo, estados = i["state"]["type"], {s["name"]: s["id"] for s in i["team"]["states"]["nodes"]}
                segurada = any(RE_SEGURA.search(c["body"] or "") for c in i["comments"]["nodes"])
                if fechada and tipo == "backlog" and "proposta" not in i["_labels"] and "Todo" in estados:
                    fim = max((_data_iso(a.get("completedAt")) for a in anteriores if a.get("completedAt")), default=None)
                    if not segurada and (fim is None or agora - fim > FRENTE_VELHA):
                        continue
                    mover_fn(i["id"], estados["Todo"])
                    liberadas.append(i["identifier"])
                    log(f"{i['identifier']}: frente {ordem[pos - 1]} do {plano} fechou; frente {n} liberada para Todo")
    return devolvidas, liberadas


def _data_iso(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def propostas_no_backlog(consulta=None):
    """Identificadores das issues no Backlog com a label `proposta` (o orquestrador decide Todo ou Canceled)."""
    nos = (consulta or gql)('{ issues(first: 50, filter: {labels: {name: {eqIgnoreCase: "proposta"}}, '
                            'state: {type: {eq: "backlog"}}}) { nodes { identifier } } }')["issues"]["nodes"]
    return [n["identifier"] for n in nos]


def _estados_do_plano(plano, consulta):
    """Estados de TODAS as issues do plano (paginado: uma aberta fora da 1ª página não pode sumir)."""
    todas, depois = [], None
    while True:
        r = consulta('query($l: String!, $a: String) { issues(first: 100, after: $a, filter: {labels: {name: '
                     '{eqIgnoreCase: $l}}}) { nodes { state { type name } } pageInfo { hasNextPage endCursor } } }',
                     l=plano, a=depois)["issues"]
        todas += r["nodes"]
        pg = r.get("pageInfo") or {}
        if not pg.get("hasNextPage") or not pg.get("endCursor"):
            return todas
        depois = pg["endCursor"]


def planos_fechados(desde, consulta=None):
    """Pares (label plano-<slug>, nº de issues do plano) com issue fechada (Done/Canceled) depois de `desde` e nenhuma outra ainda aberta
    (backlog/unstarted/started, fora In Review). Abrir a próxima frente exige ler o plano: fica para o orquestrador.
    shortcut: fechamento medido por updatedAt (inclui edição posterior), no pior caso repete aviso em 45 min."""
    consulta = consulta or gql
    nos = consulta('query($d: DateTimeOrDuration!) { issues(first: 50, filter: {updatedAt: {gt: $d}, '
                   'state: {type: {in: ["completed", "canceled"]}}, labels: {name: {startsWithIgnoreCase: "plano-"}}}) '
                   '{ nodes { labels { nodes { name } } } } }', d=desde)["issues"]["nodes"]
    planos = sorted({lb["name"] for n in nos for lb in n["labels"]["nodes"] if lb["name"].lower().startswith("plano-")})
    fechados = []
    for plano in planos:
        todas = _estados_do_plano(plano, consulta)
        if not any(n["state"]["type"] in ("backlog", "unstarted", "started") and n["state"]["name"] != "In Review"
                   for n in todas):
            fechados.append((plano, len(todas)))
    return fechados


def novos(chaves, arquivo):
    """Chaves ainda não avisadas (uma por linha em `arquivo`); grava as novas. Evita acordar o LLM a cada ciclo
    pela mesma proposta/plano."""
    arquivo = Path(arquivo)
    vistas = set(arquivo.read_text(encoding="utf-8").splitlines()) if arquivo.exists() else set()
    novas = [c for c in dict.fromkeys(chaves) if c not in vistas]
    if novas:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        with arquivo.open("a", encoding="utf-8") as f:
            f.write("".join(c + "\n" for c in novas))
    return novas


def _mover_no_fim(item, estado, comentario=None):
    """Move a issue no fim da execução; preserva Done/Canceled, inclusive quando há espera posterior.
    Falha ao MOVER (rede, coluna que não existe) só vai para o log: a entrega já saiu, não pode virar "não chegou"."""
    try:
        _mover_no_fim_(item, estado, comentario)
    except Exception as e:  # noqa: BLE001
        if comentario:
            raise  # sem sessão o comentário É a entrega: aí a falha tem que subir
        print(f"{item.get('identifier')}: entrega ok, mas não consegui mover para {estado}: {type(e).__name__}: {e}")


def _mover_no_fim_(item, estado, comentario=None):
    try:
        issue = gql("query($id: String!) { issue(id: $id) { completedAt state { type name } } }", id=item["id"])["issue"]
        st = issue["state"]
        tipo, nome = st["type"], st["name"]
    except Exception:  # noqa: BLE001
        tipo = nome = None
    # A ordem dos timestamps não prova quem fechou a issue. Preserve Done/Canceled;
    # só o portão de recibo pode desfazer Done com prova de entrega pendente.
    if tipo in ("completed", "canceled") or nome == estado:
        if comentario:
            comentar(item["id"], comentario)
        return
    if estado == "Done" and item.get("tipo", "issue") == "issue":  # portão único; o comentário que acompanha o Done vale como último comentário
        from hub import portao_done
        if portao_done.barrar(item["identifier"], comentario):
            return
    mover(item["id"], item["estados"][estado], comentario)


# ---- ao vivo: eventos do `claude -p --output-format stream-json` no painel do agente ----

def _parametro(nome, entrada):
    entrada = entrada or {}
    for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt"):
        if entrada.get(k):
            return str(entrada[k]).strip().splitlines()[0][:200]
    return json.dumps(entrada, ensure_ascii=False)[:200]


class AoVivo:
    """Recebe os eventos da CLI e manda para o painel: cada ferramenta vira action efêmera (a de agora), o texto do
    Claude vira thought efêmero. No máximo um envio a cada INTERVALO s (guarda o último); `parar()` olha o stop."""
    INTERVALO = 8

    def __init__(self, sessao, enviar=None, relogio=time.monotonic):
        self.sessao, self.relogio, self.ultimo_envio, self.pendente = sessao, relogio, 0.0, None
        self.enviar = enviar or (lambda tipo, **c: atividade(sessao, tipo, efemera=True, **c))

    def __call__(self, evento):
        if evento.get("type") != "assistant":
            return
        for bloco in (evento.get("message") or {}).get("content") or []:
            if bloco.get("type") == "tool_use":
                self.pendente = ("action", {"action": bloco.get("name", "?"),
                                            "parameter": _parametro(bloco.get("name"), bloco.get("input"))})
            elif bloco.get("type") == "text" and bloco.get("text", "").strip():
                self.pendente = ("thought", {"corpo": bloco["text"].strip()[:1500]})
        self.descarregar()

    def descarregar(self, forcar=False):
        if not self.pendente or (not forcar and self.relogio() - self.ultimo_envio < self.INTERVALO):
            return
        tipo, c = self.pendente
        self.pendente, self.ultimo_envio = None, self.relogio()
        try:
            self.enviar(tipo, **c)
        except Exception:  # noqa: BLE001 — o painel ao vivo nunca derruba a execução
            pass

    def parar(self):
        try:
            s = gql("query($id: String!) { agentSession(id: $id) { status activities(first: 10) { nodes { createdAt "
                    "signal content { __typename } } } } }", app=True, id=self.sessao)["agentSession"]
            return parar_pedido(s)
        except Exception:  # noqa: BLE001
            return False


# ---- autoaprimoramento: o próprio Claude abre issue (retro diária, backlog semanal) ----

def projeto_do_repo(repo):
    """Nome do projeto cuja descrição tem `repo: <org>/<repo>` (aceita "x" ou "<org>/x")."""
    alvo = (repo if "/" in repo else config.repo(repo)).lower()
    nodes = gql("{ projects(first: 100) { nodes { name description } } }", cache=CACHE_S)["projects"]["nodes"]
    for p in nodes:
        if alvo in (r.lower() for r in RE_REPO.findall(p.get("description") or "")):
            return p["name"]
    raise SystemExit(f"nenhum projeto do Linear tem repo: {alvo}")


def _id_da_label(nome, existentes, time_id):
    """Id da label do time; se não existe, cria (e guarda em `existentes`)."""
    if nome.lower() not in existentes:
        # `labels` do time vem paginado (50): procurar pelo nome antes de criar, senão dá "duplicate label name".
        achadas = gql("query($n: String!, $t: ID!) { issueLabels(filter: {name: {eqIgnoreCase: $n}, team: {id: {eq: $t}}}) "
                      "{ nodes { id } } }", cache=CACHE_S, n=nome, t=time_id)["issueLabels"]["nodes"]
        if achadas:
            existentes[nome.lower()] = achadas[0]["id"]
            return existentes[nome.lower()]
        existentes[nome.lower()] = gql("mutation($n: String!, $t: String!) { issueLabelCreate(input: {name: $n, teamId: $t}) "
                                       "{ issueLabel { id } } }", usuario=True, n=nome, t=time_id)["issueLabelCreate"]["issueLabel"]["id"]
    return existentes[nome.lower()]


def ligar_skills(ident, nomes):
    """Põe a label `skill:<nome>` na issue (cria a label se faltar). A próxima execução já tem a skill."""
    from hub import skills
    ruins = [n for n in nomes if not skills.nome_valido(n)]
    if ruins:
        raise SystemExit(f"skill inválida: {', '.join(ruins)}")
    time_ = gql("{ teams(first: 1) { nodes { id labels { nodes { id name } } } } }", cache=CACHE_S)["teams"]["nodes"][0]
    existentes = {lb["name"].lower(): lb["id"] for lb in time_["labels"]["nodes"]}
    issue = gql("query($i: String!) { issue(id: $i) { id labels { nodes { name } } } }", cache=CACHE_S, i=ident)["issue"]
    ja = {lb["name"].lower() for lb in issue["labels"]["nodes"]}
    rotulo = lambda n: n if n.startswith("mcp:") else f"skill:{n}"  # noqa: E731 — mcp:<nome> liga MCP
    novas = [n for n in nomes if rotulo(n) not in ja]
    for n in novas:
        gql("mutation($i: String!, $l: String!) { issueAddLabel(id: $i, labelId: $l) { success } }", i=issue["id"],
            l=_id_da_label(rotulo(n), existentes, time_["id"]))
    return novas


def criar_issue(projeto, titulo, descricao, estado="Backlog", labels=(), prioridade=3):
    """Abre issue como o usuário-app Claude (creator=app: no Backlog espera o orquestrador; em Todo roda sozinha).
    Label que não existe é criada. Devolve "<url>"."""
    from hub import esteira
    if motivo := esteira.sem_pedido(titulo, descricao, labels):  # clipe/reel/animação sem o pedido do dono
        raise SystemExit(f"criar: {motivo}")
    time_ = gql("{ teams(first: 1) { nodes { id states { nodes { id name } } labels { nodes { id name } } } } }", cache=CACHE_S)["teams"]["nodes"][0]
    projetos = {p["name"].lower(): p["id"] for p in gql("{ projects(first: 100) { nodes { id name } } }", cache=CACHE_S)["projects"]["nodes"]}
    if projeto.lower() not in projetos:
        raise SystemExit(f"projeto {projeto!r} não existe; use um de: {', '.join(sorted(projetos))}")
    existentes = {lb["name"].lower(): lb["id"] for lb in time_["labels"]["nodes"]}
    ids = [_id_da_label(nome, existentes, time_["id"]) for nome in labels]
    estado_id = next(s["id"] for s in time_["states"]["nodes"] if s["name"].lower() == estado.lower())
    r = gql("mutation($i: IssueCreateInput!) { issueCreate(input: $i) { issue { identifier url } } }", app=bool(token_app()),
            i={"teamId": time_["id"], "projectId": projetos[projeto.lower()], "title": titulo[:250], "description": descricao,
               "stateId": estado_id, "labelIds": ids, "priority": prioridade})["issueCreate"]["issue"]
    return f"{r['identifier']} {r['url']}"


def com_proposta(estado, labels):
    """Dentro da execução de uma issue (TAREFA_NOME/HUB_TAREFA linear-*), tarefa nova no Backlog que não é da esteira
    leva a label `proposta`: quem decide é o orquestrador."""
    nome = (os.environ.get("TAREFA_NOME") or os.environ.get("HUB_TAREFA") or "").lower()
    ls = {lb.lower() for lb in labels}
    if nome.startswith("linear-") and estado.lower() == "backlog" and not ls & {"esteira", "proposta"}:
        return [*labels, "proposta"]
    return list(labels)


def _criar_execucao(item):
    """criar_fn do portão: a issue de execução nasce no mesmo projeto da issue de código."""
    def criar(projeto, titulo, descricao, estado, labels, prioridade):
        return criar_issue(item.get("projeto") or projeto, titulo, descricao, estado, labels, prioridade)
    return criar


def _pr_aberto(issue, gh=None):
    """(lição 7): URL do primeiro PR ligado à issue (anexo do Linear) que ainda está aberto, ou "". Sem ler o GitHub,
    não bloqueia (o recibo de pronto confere depois)."""
    from hub import recibo
    for repo, n in recibo._prs(issue):
        try:
            if (gh or esperas.gh_api)(f"repos/{repo}/pulls/{n}").get("state") == "open":
                return f"https://github.com/{repo}/pull/{n}"
        except Exception:  # noqa: BLE001
            continue
    return ""


def _portao_done(ident, consulta=None, gh=None):
    """motivo para NÃO mover KRN-N para Done ("" = pode). Lê pedido e comentários; usa o texto de todos os comentários.
    PR ligado e ainda aberto também barra (Done com PR aberto, reaberta pela auditoria)."""
    i = (consulta or gql)("query($i: String!) { issue(id: $i) { identifier title description attachments { nodes { url } } "
                          "comments(first: 100) { nodes { body } } } }", i=ident)["issue"]
    if aberto := _pr_aberto(i, gh):
        return (f"o PR {aberto} ainda está aberto: faça o merge (ou feche, explicando por que a issue vale sem ele) e só então "
                "Done; esperando CI ou revisão, `voltar-quando`; esperando o dono, In Review")
    texto = "\n".join(c["body"] for c in i["comments"]["nodes"])
    from hub import portao
    item = {"identifier": i["identifier"], "titulo": i["title"], "descricao": i.get("description") or ""}
    if not portao.eh_de_dado(item, portao.arquivos_do_pr(texto)):
        return ""
    return portao.faltou_prova(texto)


def mover_por_nome(ident, nome, comentario=None, consulta=None, mover_fn=None, portao_fn=None):
    """Move KRN-N para o estado `nome` do time (sem diferença de caixa). Estado que não existe: SystemExit(1)."""
    i = (consulta or gql)("query($i: String!) { issue(id: $i) { id team { states { nodes { id name } } } } }", i=ident)["issue"]
    estado = next((s["id"] for s in i["team"]["states"]["nodes"] if s["name"].lower() == nome.lower()), None)
    if not estado:
        sys.exit(f"estado {nome!r} não existe no time")
    if nome.lower() == "done" and portao_fn is not None:
        motivo = portao_fn(ident)
        if motivo:  # issue de dado sem prova de execução não fecha
            volta = next((s["id"] for s in i["team"]["states"]["nodes"] if s["name"].lower() == "in progress"), estado)
            (mover_fn or mover)(i["id"], volta, f"Portão de Done (KRN-973): não fechei, {motivo}. Voltou para In Progress.")
            sys.exit(f"{ident} não vai para Done: {motivo}")
    if nome.lower() == "done" and not mover_fn:  # portão único; sem recibo a issue volta para In Progress
        from hub import portao_done
        faltas = portao_done.barrar(ident, comentario, consulta=consulta)
        if faltas:
            sys.exit(f"Done barrado: {'; '.join(faltas)}")
    (mover_fn or mover)(i["id"], estado, comentario)


def arquivar_antigas(dias=14):
    """Plano grátis do Linear = 250 issues não arquivadas: arquiva Done/Canceled sem mexer há `dias` dias."""
    q = ('query($d: DateTimeOrDuration!) { issues(first: 100, filter: {state: {type: {in: ["completed", "canceled"]}}, '
         'updatedAt: {lt: $d}}) { nodes { id identifier } } }')
    feitas = []
    for i in gql(q, d=f"-P{int(dias)}D")["issues"]["nodes"]:
        gql("mutation($id: String!) { issueArchive(id: $id) { success } }", id=i["id"])
        feitas.append(i["identifier"])
    return feitas


def postar_update(projeto, corpo, saude="onTrack"):
    """Update de projeto escrito pelo Claude (status semanal). Sai como o usuário-app: a ponte não responde a ele."""
    projetos = {p["name"].lower(): p["id"] for p in gql("{ projects(first: 100) { nodes { id name } } }")["projects"]["nodes"]}
    if projeto.lower() not in projetos:
        raise SystemExit(f"projeto {projeto!r} não existe; use um de: {', '.join(sorted(projetos))}")
    r = gql("mutation($i: ProjectUpdateCreateInput!) { projectUpdateCreate(input: $i) { projectUpdate { url } } }",
            app=bool(token_app()), i={"projectId": projetos[projeto.lower()], "body": corpo, "health": saude})
    return r["projectUpdateCreate"]["projectUpdate"]["url"]


def resumo_dia(horas=24):
    """Texto com as issues mexidas nas últimas horas: estado, projeto, conferências e a conversa (quem disse o quê)."""
    q = ('query($d: DateTimeOrDuration!) { issues(first: 100, filter: {updatedAt: {gt: $d}}) { nodes { identifier title '
         'state { name } project { name } labels { nodes { name } } creator { name app } '
         'comments(first: 100) { nodes { createdAt body user { name app } } } } } }')
    linhas = []
    for i in gql(q, d=f"-PT{int(horas)}H")["issues"]["nodes"]:
        labels = ",".join(lb["name"] for lb in i["labels"]["nodes"])
        quem = "Claude" if (i.get("creator") or {}).get("app") else (i.get("creator") or {}).get("name", "?")
        linhas.append(f"## {i['identifier']} [{i['state']['name']}] {(i.get('project') or {}).get('name', '-')} · "
                      f"{i['title']} (aberta por {quem}{'; ' + labels if labels else ''})")
        for c in _ordem(i["comments"]["nodes"]):
            autor = "Linear" if _eh_bot(c) else "Claude" if _eh_meu(c) else (c.get("user") or {}).get("name", "?")
            linhas.append(f"- {c['createdAt'][:16]} {autor}: {c['body'][:700]}".replace("\n", " "))
    return "\n".join(linhas) or "nada mexido no Linear nesse período"


# ---- CLI da sessão: conversar com o dono durante a execução ----

def _falas(alvo, update=False, sessao=None):
    """Falas do dono: comentários (não do Claude) e, com sessão, prompts do painel do agente."""
    if update:
        cs = gql("query($id: String!) { projectUpdate(id: $id) { comments(first: 100) { nodes { body createdAt user { app } } } } }",
                 id=alvo)["projectUpdate"]["comments"]["nodes"]
    else:
        cs = gql("query($id: String!) { issue(id: $id) { comments(first: 100) { nodes { body createdAt user { app } } } } }",
                 id=alvo)["issue"]["comments"]["nodes"]
    falas = [c for c in cs if _eh_fala(c)]
    if sessao:
        s = gql("query($id: String!) { agentSession(id: $id) { activities(first: 100) { nodes { createdAt signal "
                "content { __typename ... on AgentActivityPromptContent { body } } } } } }", app=True, id=sessao)
        falas += [{"body": a["content"]["body"], "createdAt": a["createdAt"]} for a in _prompts(s["agentSession"])]
    return _ordem(falas)


def _agora():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.linear")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mv = sub.add_parser("mover", help="move a issue de estado pelo nome (o orquestrador aprova proposta: Todo)")
    mv.add_argument("alvo", help="KRN-6")
    mv.add_argument("estado")
    mv.add_argument("--comentario")
    cr = sub.add_parser("criar", help="abre issue como o Claude (descrição pela entrada padrão)")
    qual = cr.add_mutually_exclusive_group(required=True)
    qual.add_argument("--projeto")
    qual.add_argument("--repo", help="<org>/x: acha o projeto pela linha repo: da descrição")
    cr.add_argument("--titulo", required=True)
    cr.add_argument("--estado", default="Backlog", help="Backlog (proposta: o orquestrador decide) ou Todo (roda sozinha)")
    cr.add_argument("--label", action="append", default=[])
    cr.add_argument("--skill", action="append", default=[], help="skill da biblioteca (hub.skills listar); vira label skill:<nome>")
    cr.add_argument("--prioridade", type=int, default=3, help="1 urgente (opus), 2 alta, 3 média, 4 baixa; label opus força o Opus")
    sk = sub.add_parser("skill", help="liga skills da biblioteca a uma issue (label skill:<nome>)")
    sk.add_argument("alvo", help="KRN-6")
    sk.add_argument("nomes", nargs="+")
    di = sub.add_parser("dia", help="o que aconteceu no Linear nas últimas horas (para a retro)")
    di.add_argument("--horas", type=int, default=24)
    sub.add_parser("uso", help="chamadas por hora e por chave (app2, fulano, app) e % do limite (KRN-986)")
    ar = sub.add_parser("arquivar", help="arquiva Done/Canceled antigas (limite de 250 do plano grátis)")
    ar.add_argument("--dias", type=int, default=14)
    up = sub.add_parser("update", help="update de projeto (status semanal; corpo pela entrada padrão)")
    up.add_argument("--projeto", required=True)
    up.add_argument("--saude", default="onTrack", choices=["onTrack", "atRisk", "offTrack"])
    an = sub.add_parser("anexar", help="sobe arquivos e posta no Linear (vídeo, imagem, PDF)")
    an.add_argument("alvo")
    an.add_argument("arquivos", nargs="+")
    an.add_argument("--texto", default="")
    an.add_argument("--update", action="store_true")
    for nome in ("comentar", "perguntar", "novidades", "esperar"):
        sp = sub.add_parser(nome)
        sp.add_argument("alvo", help="KRN-6, ou o id do update com --update")
        sp.add_argument("--update", action="store_true")
        if nome in ("comentar", "perguntar"):
            sp.add_argument("texto", nargs="?", help="sem texto: lê da entrada padrão")
        if nome == "novidades":
            sp.add_argument("--desde", default="1970-01-01T00:00:00Z")
        if nome in ("perguntar", "esperar"):
            sp.add_argument("--minutos", type=float, default=20)
    vq = sub.add_parser("voltar-quando", help="grava a espera do CI/revisão do PR e a sessão termina; volta sozinha")
    vq.add_argument("alvo", help="KRN-6")
    vq.add_argument("--pr", required=True, help="URL do PR no GitHub")
    vq.add_argument("--max-horas", type=float, default=esperas.MAX_HORAS)
    vq.add_argument("--no-ar", nargs="?", const="", default=None, metavar="SHA",
                    help="espera o deploy do merge do PR (ou deste sha) em vez do CI")
    vq.add_argument("--url", default="", help="com --no-ar: página que mostra o commit no ar (ex.: https://site/api/versao)")
    inc = sub.add_parser("incidente", help="registra falha do harness: classe e a peça que muda (a retro lista)")
    isub = inc.add_subparsers(dest="acao", required=True)
    ir = isub.add_parser("registrar")
    ir.add_argument("--falha", required=True)
    ir.add_argument("--classe", required=True, choices=incidentes.CLASSES)
    ir.add_argument("--peca", required=True, help="qual peça do harness muda (arquivo, teste, hook, regra)")
    ir.add_argument("--issue", default="")
    ir.add_argument("--chave", default="", help="a mesma chave não abre dois incidentes pendentes (idempotente)")
    ir.add_argument("--prioridade", type=int, choices=(1, 2), default=2, help="1 = correção do dono")
    ifd = isub.add_parser("feito")
    ifd.add_argument("id")
    ifd.add_argument("--nota", default="")
    il = isub.add_parser("listar")
    il.add_argument("--horas", type=float, default=None)
    il.add_argument("--pendentes", action="store_true")
    sub.add_parser("status", help="uso interno de disparar_status").add_argument("dados")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "status":
        responder_status(json.loads(a.dados))
        return 0
    if a.cmd == "mover":
        mover_por_nome(a.alvo, a.estado, a.comentario, portao_fn=_portao_done)
        print(f"{a.alvo} → {a.estado}")
        return 0
    if a.cmd == "criar":
        from hub import esteira
        if esteira.desligada(a.label):
            sys.exit(f"criar: label {sorted(esteira.DESLIGADAS)} desligada pelo {config.dono()} (KRN-825): sem clipe/reel automático")
        # a faixa (Claude ou Codex) é decidida no despacho da esteira, não na criação
        descricao = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
        from hub import plano_itens  # itens com check:/files:/after:/exec: válidos; sem check: vai para o Sonnet
        itens = plano_itens.parse(descricao)
        if erros := plano_itens.validar(itens):
            sys.exit("criar: itens do plano inválidos: " + "; ".join(erros))
        for aviso in plano_itens.avisos(itens):
            print(f"aviso: {aviso}", file=sys.stderr)
        print(criar_issue(a.projeto or projeto_do_repo(a.repo), a.titulo, descricao,
                          a.estado, com_proposta(a.estado, [*a.label, *(f"skill:{n}" for n in a.skill)]), a.prioridade))
        return 0
    if a.cmd == "skill":
        novas = ligar_skills(a.alvo, a.nomes)
        print(f"{a.alvo}: " + (", ".join(f"skill:{n}" for n in novas) + " ligada(s)" if novas else "já tinha todas"))
        return 0
    if a.cmd == "voltar-quando":
        try:
            e = esperas.gravar(a.alvo, a.pr, a.max_horas, no_ar=a.no_ar, url=a.url)
        except ValueError as erro:
            print(erro, file=sys.stderr)
            return 1
        if a.no_ar is not None:
            print(f"espera gravada: {e['ident']} volta quando o merge de {e['pr']} estiver no ar (no máximo "
                  f"{e['max_horas']:g} h). Termine agora com uma linha: \"Merge feito; volto quando estiver no ar.\"")
            return 0
        print(f"espera gravada: {e['ident']} volta quando o local-ci de {e['pr']} sair e a revisão chegar "
              f"(no máximo {e['max_horas']:g} h). Termine agora com uma linha: \"PR {e['pr']} no CI; volto quando sair.\"")
        return 0
    if a.cmd == "uso":
        print(linear_uso.texto_uso())
        return 0
    if a.cmd == "dia":
        print(resumo_dia(a.horas))
        return 0
    if a.cmd == "incidente":
        try:
            if a.acao == "registrar":
                print(json.dumps(incidentes.registrar(a.falha, a.classe, a.peca, a.issue, chave=a.chave, prioridade=a.prioridade), ensure_ascii=False))
            elif a.acao == "feito":
                print(json.dumps(incidentes.marcar(a.id, "feito", a.nota), ensure_ascii=False))
            elif a.pendentes:
                print("\n".join(f"{i['id']} {i['classe']} {i.get('issue', '')}: {i['falha']} → {i['peca']}"
                                for i in incidentes.listar(so_pendentes=True)) or "nenhum pendente")
            else:
                print(incidentes.texto_retro(a.horas or 24))
        except ValueError as erro:
            print(erro, file=sys.stderr)
            return 1
        return 0
    if a.cmd == "arquivar":
        feitas = arquivar_antigas(a.dias)
        print(f"arquivadas: {', '.join(feitas) or 'nenhuma'}")
        return 0
    if a.cmd == "update":
        print(postar_update(a.projeto, sys.stdin.buffer.read().decode("utf-8", "replace").strip(), a.saude))
        return 0
    # LINEAR_SESSAO só vale na execução da própria issue: de fora (o orquestrador, outra tarefa) a fala é comentário
    eh_dela = not a.update and da_execucao(a.alvo)
    sessao = os.environ.get("LINEAR_SESSAO") if eh_dela else None
    if a.cmd == "anexar":
        partes = [a.texto.strip()] if a.texto.strip() else []
        for f in a.arquivos:
            link = markdown_anexo(f, subir_arquivo(f))
            # texto curto (proposta, relatório em .md/.txt) entra inteiro na entrega final: o dono lê sem baixar
            curto = Path(f).suffix.lower() in (".md", ".txt") and Path(f).stat().st_size <= 15000
            registrar_anexo(a.alvo, link, Path(f).read_text(encoding="utf-8", errors="replace").strip() if curto else None, f)
            partes.append(link)
        # com sessão: só no painel (sem notificar); a entrega final repete tudo. Sem sessão: comentário.
        atividade(sessao, "thought", "\n\n".join(partes)) if sessao else falar(a.alvo, "\n\n".join(partes), a.update)
        print("anexado:", ", ".join(Path(f).name for f in a.arquivos))
        return 0
    if a.cmd in ("comentar", "perguntar"):
        texto = (a.texto or sys.stdin.buffer.read().decode("utf-8", "replace")).strip()  # Windows lê stdin em cp1252
        if a.cmd == "perguntar" or not a.update:  # o que o dono já decidiu não se pergunta nem se espera
            from hub import memorias
            if feita := memorias.resposta_registrada(texto, comentario=a.cmd == "comentar"):
                print(f"{'Pergunta' if a.cmd == 'perguntar' else 'Comentário'} NÃO enviado: o dono já decidiu ({feita[0]}). {feita[1]}")
                return 0
        if not texto:
            print("texto vazio", file=sys.stderr)
            return 1
        if a.cmd == "comentar" and not a.update and (recusa := guarda_deploy(a.alvo, texto)):
            print(recusa[1], file=sys.stderr if recusa[0] else sys.stdout)
            return recusa[0]
        if sessao:  # progresso: painel do agente, sem notificar; pergunta: pedido de resposta (notifica)
            atividade(sessao, "thought" if a.cmd == "comentar" else "elicitation", texto)
        else:  # comentar de fora: repasse (a pergunta de fora não, senão o próprio `perguntar` a leria como resposta)
            falar(a.alvo, texto, a.update, repasse=a.cmd == "comentar" and not a.update and not eh_dela)
        print("enviado")
        if a.cmd == "comentar":
            return 0
    if a.cmd == "novidades":
        falas = [c for c in _falas(a.alvo, a.update, sessao) if c["createdAt"] > a.desde]
    else:
        desde, fim, falas = _agora(), time.monotonic() + a.minutos * 60, []
        while not falas and time.monotonic() < fim:
            time.sleep(20)
            falas = [c for c in _falas(a.alvo, a.update, sessao) if c["createdAt"] > desde]
        if sessao and a.cmd == "perguntar":
            atividade(sessao, "thought", "Recebi, seguindo." if falas else "Sem resposta no prazo; sigo com a recomendação.")
    for c in falas:
        print(f"[{c['createdAt']}] {config.dono(True)}: {c['body']}")
    if eh_dela:  # o `novidades` do orquestrador não marca como lido o que a execução da issue não viu
        with contextlib.suppress(Exception):
            marcar_visto(a.alvo)  # o fim da execução só devolve para a fila o que ninguém leu
    print(f"agora: {_agora()}" + ("" if falas else " (nada novo)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
