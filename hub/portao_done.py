"""Portão único de Done. Em 16/10 a auditoria achou 873 issues Done: 9 com PR aberto, 7 com PRs só fechados e
187 cujo último comentário ainda dizia "Falta", "volto quando", "no CI; volto". Done só passa se, para toda mudança
de estado (conta, Codex, recibo, mover_por_nome, webhook, UI):
- todo PR citado está MERGED, ou CLOSED com um comentário "fechado porque…";
- o merge está no ar (recibo.no_ar) quando o repo tem deploy;
- o último comentário não deixa pendência aberta ("Falta", "volto quando", "aguardando", "ainda não"), a não ser que
  liste o que ficou para outra issue (KRN-N) ou para o dono (a pendência vai para a lista "Pendências do dono");
- PDF/WhatsApp prometido: o aviso consta em avisos/enviados.
Faltou algo: a issue volta para In Progress com o motivo e a conta retoma. As regras de PR, deploy e aviso moram em
recibo.conferir; aqui ficam as de texto, o portão de escrita (`barrar`), o webhook (`conferir_uma`) e a ronda diária.

    python3 -m hub.portao_done conferir [--desfazer] # o webhook chama isso quando uma issue vira Done
    python3 -m hub.portao_done ronda [--dias 3]                 # auditoria das 3 classes; o nº vai para o painel Erros
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys

from hub import config, conta, esperas

_DONO = re.escape(config.dono())
# "Falta"/"Faltam" no começo de frase, linha ou item (maiúsculo: "falta de dados" é substantivo); "volto quando", "no CI; volto"
RE_PENDENCIA = re.compile(
    r"(?<![\w])(?-i:Falta|Faltam)\b(?!\s+de\b)|\bvolto\s+(?:quando|a\s+conferir)|\bno\s+CI;?\s*volto|"
    r"\bainda\s+não\s+(?:fiz|foi|fez|est[áa]|subiu|saiu|mergeei|houve|tem|mergead)|\bmerge\s+ainda\s+não|"
    r"\bnão\s+(?:rodou|foi\s+(?:testad[oa]|validad[oa]|executad[oa]))\s+de\s+ponta\s+a\s+ponta\b|"
    r"\baguardando\b", re.I)
# INC-239: entrega explicitamente ausente não se resolve com uma referência a outra issue.
RE_ENTREGA_AUSENTE = re.compile(
    r"\bnão\s+(?:foi|foram)\s+entregue[s]?\b|"
    r"\bainda\s+não\s+tem\s+(?:a|o|as|os)\s+(?:lista|decisões|provas?|recibos?)\b|"
    r"\bsem\s+prova\s+ainda\b", re.I)
RE_BLOQUEIO = re.compile(f"(?:{RE_ENTREGA_AUSENTE.pattern})|(?:{RE_PENDENCIA.pattern})", re.I)
RE_BRU = re.compile(r"\bKRN-\d+\b")
RE_PARA_DONO = re.compile(rf"depende de voc[êe]|depende do {_DONO}|para o {_DONO}|pro {_DONO}|do {_DONO}\b|"
                          rf"{_DONO} (?:decide|precisa|tem que|deve)|precisa do {_DONO}|\bsó o {_DONO} tem\b", re.I)
RE_FECHADO_PORQUE = re.compile(r"\bfechad[oa]s?\s+porque|\bfechei\s+porque|\bfechad[oa]s?\s*:|\bfechad[oa]s?\s+por\b", re.I)
RE_SEM_AVISO = re.compile(r"^[ \t>*_-]*aviso[ \t]*:[ \t*_]*nenhum[ \t*_.]*$", re.I | re.M)
RE_PROMETE = re.compile(r"\b(?:mand\w+|envi\w+)\b[^.\n]{0,60}\b(?:pdf|whatsapp|zap)\b|\b(?:pdf|whatsapp)\b[^.\n]{0,40}\b(?:mand\w+|envi\w+)\b", re.I)
JANELA = 400  # a pendência tem destino se o KRN-N ou o dono aparece até aqui depois dela


class Delegada(str):
    """Linha de pendência que o texto passa adiante; `dono`: o destino é o dono (vai para a lista Pendências do dono)."""
    dono = False


# frase de espera de CI/revisão/merge/deploy ("no CI; volto quando sair"); some quando os PRs citados já foram fundidos
RE_ESPERA = re.compile(r"\bvolto\s+(?:quando|a\s+conferir)|\bno\s+CI;?\s*volto|"
                       r"\bainda\s+não\s+(?:fiz|mergeei|mergead|saiu|subiu)|\bmerge\s+ainda\s+não", re.I)
RE_ESPERA_DE = re.compile(r"\bCI\b|local-ci|revis[ãa]o|merge|deploy|no\s+ar|\bsa(?:ir|iu)\b|\bsubi(?:r|u)\b", re.I)


def pendencias(texto, ident="", historico=(), espera_resolvida=False):
    """(abertas, delegadas): trechos de pendência no texto. Delegada = o texto manda a pendência para outra issue
    (KRN-N diferente da própria) ou para o dono, na mesma passagem; aberta = ninguém assumiu.
    `espera_resolvida`: os PRs do comentário já estão fundidos, então a frase de espera não conta."""
    # (INC-205/221/231/240): trecho entre aspas que aparece literalmente num comentário anterior do portão
    # ("Done barrado…"/"Voltou de Done…") é citação do portão, não pendência nova, qualquer que seja a redação em volta.
    # Pendência em prosa, fora de aspas, ou entre aspas com frase que o portão nunca citou, continua contando.
    # INC-335: o relato pode citar sem o negrito do diagnóstico ("Falta:**").
    # Normalize só a comparação; o bloqueio de prosa e citações sem histórico permanece.
    def sem_negrito(t):
        return t.lower().replace("**", "")
    citados = [sem_negrito(h) for h in historico if RE_DO_PORTAO.match(h)]
    def _citacao_do_portao(m):
        # INC-261: o relato pode abreviar a recusa com reticências.
        # Só retire o sufixo de abreviação; o trecho restante exige prova no portão.
        cru = sem_negrito(m.group(1)).strip()
        trecho = re.sub(r"(?:…|\.\.\.)$", "", cru).strip()
        if trecho != cru and len(trecho) < 12:  # "Falta..." abreviado casaria com quase toda recusa
            return m.group(0)
        return "" if trecho and any(trecho in h for h in citados) else m.group(0)
    texto = re.sub(r'"([^"\n]+)"', _citacao_do_portao, texto or "")
    texto = re.sub(r"(?m)^[ \t>*_-]*Falta(?:m)?[ \t*]*:[ \t*]*(?:nada|nenhuma?)[ \t*_.!]*$", "", texto)
    # INC-293: uma sugestão explicitamente fora do escopo não é entrega pendente.
    # Só vale na própria linha marcada; ausência de entrega continua bloqueando.
    sugestoes = [m.span() for m in re.finditer(
        r"(?im)^[ \t]*(?:[-*] +)?(?:\*\*)?Sugestão, não tarefa aberta(?:\*\*)?:[^\n]*", texto)]
    # INC-297: o destino explícito no cabeçalho vale para os itens da lista
    # imediatamente abaixo, até a próxima linha em branco ou prosa sem marcador.
    listas_delegadas = []
    for cab in re.finditer(
            r"(?im)^[ \t]*Ficaram[^\n]*pendências fora do escopo desta issue[^\n]*:\n"
            r"(?:[ \t]*[-*] [^\n]*(?:\n|$))+", texto):
        destino = cab.group().split("\n", 1)[0]
        if any(b != ident for b in RE_BRU.findall(destino)):
            listas_delegadas.append(cab.span())
    abertas, delegadas = [], []
    for m in RE_BLOQUEIO.finditer(texto or ""):
        if (not RE_ENTREGA_AUSENTE.match(m.group())
                and any(inicio <= m.start() < fim for inicio, fim in sugestoes)):
            continue
        trecho = texto[m.start():m.start() + JANELA].split("\n\n")[0]  # destino só vale na mesma passagem (parágrafo ou lista)
        if espera_resolvida and RE_ESPERA.match(m.group()) and RE_ESPERA_DE.search(trecho.split("\n")[0]):
            continue
        linha = " ".join(trecho.split())[:160]
        outras = [b for b in RE_BRU.findall(trecho) if b != ident]
        if not RE_ENTREGA_AUSENTE.match(m.group()) and (outras or RE_PARA_DONO.search(trecho)
                or any(inicio <= m.start() < fim for inicio, fim in listas_delegadas)):
            d = Delegada(linha)
            d.dono = bool(RE_PARA_DONO.search(trecho))
            delegadas.append(d)
        else:
            abertas.append(linha)
    return abertas, delegadas


def ultimo_comentario(issue):
    """(texto, humano) do último comentário da issue. Humano: o dono falou por último; aí não há pendência nossa."""
    nodes = sorted((issue.get("comments") or {}).get("nodes") or [], key=lambda c: c.get("createdAt") or "")
    # O diagnóstico do portão repete faltas antigas; avalie a entrega anterior.
    nodes = [c for c in nodes if (c.get("body") or "").strip() !=
             "This thread is for an agent session with claude."
             and not ((c.get("user") or {}).get("app")
                                     and RE_DO_PORTAO.match(c.get("body") or ""))
             and not RE_MERGE_DO_HUB.match(c.get("body") or "")]
    if not nodes:
        return "", False
    c = nodes[-1]
    return c.get("body") or "", not (c.get("user") or {}).get("app")


RE_DO_PORTAO = re.compile(r"\s*(?:Done barrado|Voltou de Done)\b", re.I)
# INC-122: o aviso mecânico de merge do hub não é entrega; sem pular ele, a pendência do comentário anterior some
RE_MERGE_DO_HUB = re.compile(r"\s*Merge feito pelo hub\b", re.I)
RE_DEBUGGER = re.compile(r"\s*Debugger\b|\bINC-\d+\b", re.I)
RE_ISSUE_DONA = re.compile(r"issue\s+dona\s*:?\s*\**\s*(KRN-\d+)", re.I)


def _entrega_de_outra(corpo, ident):
    """comentário que não fala da entrega desta issue. Os do próprio portão ("Done barrado", "Voltou de Done")
    só repetem PR já citado (e travavam o Done em laço); os do debugger/incidente que apontam outra issue dona (ou
    citam outra KRN-N e não esta) falam do PR de outra issue (presa por causa do #553, que era da)."""
    if RE_DO_PORTAO.match(corpo):
        return True
    if not RE_DEBUGGER.search(corpo):
        return False
    ident = ident.upper()
    donas = {d.upper() for d in RE_ISSUE_DONA.findall(corpo)}
    if donas:
        return ident not in donas
    # INC-278: o relatório automático do debugger publica na issue afetada
    # o PR do harness. Sem uma atribuição explícita à entrega, esse PR não
    # é requisito do artigo/site que a issue pediu.
    if re.match(r"\s*Debugger\s*·\s*tentativa\b", corpo, re.I) and re.search(r"\bINC-\d+\b", corpo):
        return True
    citadas = {b.upper() for b in re.findall(r"\bKRN-\d+\b", corpo, re.I)}
    return bool(citadas - {ident}) and ident not in citadas


def _prs_de_incidente_separado(issue):
    """INC-280: reconciliação humana pode separar o PR do incidente da entrega.

    Exige referência exata, INC e a frase explícita "não é pendência desta entrega". Só vale o PR citado
    ANTES dela na mesma frase (é o sujeito da frase): o "ver #600" depois dela continua cobrado. "Não implementa
    o artigo" fica de fora, porque é como um revisor reprova entrega incompleta (INC-277). Relatos do agente
    não dispensam PRs.
    """
    separados = set()
    for c in (issue.get("comments") or {}).get("nodes") or []:
        corpo = c.get("body") or ""
        if (c.get("user") or {}).get("app"):
            continue
        for paragrafo in corpo.split("\n\n"):
            if not re.search(r"\bINC-\d+\b", paragrafo, re.I):
                continue
            frase = re.search(r"não é pendência desta entrega", paragrafo, re.I)
            if not frase:
                continue
            # só a frase da dispensa: corta no último fim de frase (ponto, !, ? + espaço, ou quebra de linha)
            antes = re.split(r"(?<=[.!?])\s+|\n", paragrafo[:frase.start()])[-1]
            refs = esperas.RE_PR.findall(antes)
            refs += re.findall(r"\b([\w.-]+/[\w.-]+)#(\d+)\b", antes)
            separados.update((repo.lower(), int(n)) for repo, n in refs)
    return separados


def prs_citados(issue, comentario=None):
    """PRs anexados à issue mais os que o Claude citou nos próprios comentários e no que vai junto do Done (sem repetir).
    Comentário do portão, ou do debugger/incidente sobre outra issue dona, não conta (`_entrega_de_outra`)."""
    ident = issue.get("identifier") or ""
    textos = [a.get("url") or "" for a in (issue.get("attachments") or {}).get("nodes") or []] + [comentario or ""]
    textos += [c.get("body") or "" for c in (issue.get("comments") or {}).get("nodes") or []
               if (c.get("user") or {}).get("app") and not _entrega_de_outra(c.get("body") or "", ident)]
    separados = _prs_de_incidente_separado(issue)
    vistos, out = set(), []
    for t in textos:
        for repo, n in esperas.RE_PR.findall(t):
            if (repo.lower(), int(n)) in separados:
                continue
            if (repo, n) not in vistos:
                vistos.add((repo, n))
                out.append((repo, int(n)))
    return out


def fechado_com_motivo(issue, repo, n, comentario=None):
    """Um comentário "fechado porque…" que cite este PR (#n ou /pull/n); sem citação, só vale se a issue tem um único PR."""
    cita = re.compile(rf"(?:#|/pull/){n}\b")
    unico = len(prs_citados(issue)) == 1
    return any(RE_FECHADO_PORQUE.search(b) and (unico or cita.search(b))
               for b in [*((c.get("body") or "") for c in (issue.get("comments") or {}).get("nodes") or []), comentario or ""])


def sem_aviso(issue):
    """Dispensa explícita: a linha `aviso: nenhum` no título, na descrição ou em comentário humano (orquestrador/dono) desliga a regra."""
    textos = [issue.get("title") or "", issue.get("description") or ""]
    textos += [c.get("body") or "" for c in (issue.get("comments") or {}).get("nodes") or [] if not (c.get("user") or {}).get("app")]
    return any(RE_SEM_AVISO.search(t) for t in textos)


def promete_aviso(issue):
    if sem_aviso(issue):
        return False
    return bool(RE_PROMETE.search(f"{issue.get('title') or ''}\n{issue.get('description') or ''}"))


# repasse que só aceita a entrega e manda fechar: não é pedido novo, não pode barrar o Done
RE_REPASSE_FECHA = re.compile(r"entrega\s+aceita|(?<!não\s)(?<!nao\s)\baceit[oa]\b|mova\s+para\s+Done|\bfech(?:o|a|ando)\b|\bfeche\b|pode\s+fechar", re.I)
# ...mas "feche" junto de um pedido ainda por fazer ("rode X e feche") continua pedido
RE_REPASSE_PEDE = re.compile(r"\b(?:rode|rodar|responda|corrija|conserte|falta|faltam|antes\s+de|prove|comprove|adicione|adicionar|ajuste|inclua|refa[çc]a|fa[çc]a)\b", re.I)


def repasse_sem_resposta(issue):
    """INC-258: pedido repassado pelo orquestrador depois da última entrega da execução ainda não foi feito."""
    from hub.entrega_visual import RE_DISPENSA
    from hub.linear import REPASSE
    nodes = sorted((issue.get("comments") or {}).get("nodes") or [], key=lambda c: c.get("createdAt") or "")
    entrega = repasse = ""
    for c in nodes:
        corpo = c.get("body") or ""
        if REPASSE in corpo:
            # dispensa do gate visual e aceite/fechamento não pedem nada, e também não são entrega
            fecha = RE_REPASSE_FECHA.search(corpo) and not RE_REPASSE_PEDE.search(corpo)
            if not RE_DISPENSA.search(corpo) and not fecha:
                repasse = c.get("createdAt") or ""
        elif ((c.get("user") or {}).get("app") and not RE_DO_PORTAO.match(corpo) and not RE_MERGE_DO_HUB.match(corpo)
              and corpo.strip() != "This thread is for an agent session with claude."):
            entrega = c.get("createdAt") or ""
        elif not (c.get("user") or {}).get("app") and corpo.strip():
            # a resposta do `mover --comentario` sai pelo token do dono (humano, sem app): também responde ao repasse
            entrega = c.get("createdAt") or ""
    return bool(repasse) and repasse > entrega


RE_VOLTAR_EM = re.compile(r"voltar-em:\s*\d{4}-\d{2}-\d{2}", re.I)


def pendencia_sem_destino(texto, ident=""):
    """a entrega que vai para In Review com "Falta o item 1" e sem destino (outra issue KRN-N,
    o dono, ou `voltar-em: AAAA-MM-DD`) fica parada sem ninguém para retomar. Devolve as pendências abertas ([] = pode)."""
    # `voltar-em` só libera a pendência da mesma passagem (parágrafo); a de outro parágrafo continua sem destino
    texto = "\n\n".join(p for p in re.split(r"\n\s*\n", texto or "") if not RE_VOLTAR_EM.search(p))
    # espera de CI/revisão/merge/deploy ("no CI; volto quando sair") tem gancho próprio (esperas, conferência): não é desta trava
    return [a for a in pendencias(texto, ident)[0] if not RE_ESPERA.match(a)][:3]


def _prs_fundidos(texto, gh):
    """True se o texto cita PR e todos estão merged. Sem `gh`, sem PR ou falha de leitura: False (continua barrando)."""
    prs = {(r.lower(), int(n)) for r, n in esperas.RE_PR.findall(texto or "")}
    if not gh or not prs:
        return False
    try:
        return all(gh(f"repos/{r}/pulls/{n}").get("merged") for r, n in prs)
    except Exception:  # noqa: BLE001 — falha de leitura não prova merge
        return False


def faltas_do_texto(issue, comentario=None, gh=None):
    """(faltas, delegadas) pelo último comentário (`comentario`: o que está para ser postado junto do Done, vale no lugar dele)."""
    ident = issue["identifier"]
    if comentario is None and repasse_sem_resposta(issue):
        return [("há pedido do orquestrador depois da última entrega, ainda sem resposta (INC-258): "
                 f"responda ao repasse em `mover {ident} Done --comentario \"<resposta>\"`")], []
    texto, humano = (comentario, False) if comentario is not None else ultimo_comentario(issue)
    if humano:
        return [], []
    anteriores = [c.get("body") or "" for c in (issue.get("comments") or {}).get("nodes") or []
                  if (c.get("body") or "") != texto]
    abertas, delegadas = pendencias(texto, ident, anteriores, _prs_fundidos(texto, gh))
    return [f"o último comentário deixa pendência aberta: \"{a}\"" for a in abertas[:3]], delegadas


# ---- Pendências do dono: lista única (sem WhatsApp) ----

def _arq_pendencias(base_dados=None):
    return conta.pasta(base_dados) / "pendencias-fulano.json"


def pendencias_fulano(base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_arq_pendencias(base_dados).read_text(encoding="utf-8"))
    return {}


@contextlib.contextmanager
def _alterar_pendencias(base_dados=None):
    """Webhooks rodam em processos concorrentes: ler-alterar-gravar sob flock, gravando por rename (leitor nunca vê arquivo pela metade)."""
    arq = _arq_pendencias(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq.with_suffix(".lock"), "w") as trava:
        fcntl.flock(trava, fcntl.LOCK_EX)
        lista = pendencias_fulano(base_dados)
        yield lista
        tmp = arq.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, arq)


def registrar_pendencia_fulano(ident, titulo, texto, base_dados=None, agora=None):
    """Grava (ou atualiza) as pendências da issue na lista única (`texto`: uma ou várias linhas). Idempotente por issue."""
    with _alterar_pendencias(base_dados) as lista:
        lista[ident] = {"titulo": titulo[:200], "pendencia": texto[:1500], "desde": (lista.get(ident) or {}).get("desde") or conta.iso(agora or conta.agora())}
    return lista


def resolver_pendencia_fulano(ident, base_dados=None):
    with _alterar_pendencias(base_dados) as lista:
        lista.pop(ident, None)


# ---- portão de escrita e webhook ----

Q_ISSUE = ("query($i: String!) { issue(id: $i) { id identifier title description createdAt completedAt state { name type } "
           "attachments { nodes { url title } } comments(first: 100) { nodes { createdAt body user { app name } } } "
           "team { id states { nodes { id name } } } } }")


def avaliar(issue, comentario=None, gh=None, get=None, base_dados=None, agora=None):
    """Todas as regras para Done: o dict de recibo.conferir (ok, esperar, faltas, depende_fulano, delegadas)."""
    from hub import recibo
    return recibo.conferir(issue, gh, get, base_dados, agora, comentario=comentario)


def _ip(issue):
    return {s["name"]: s["id"] for s in issue["team"]["states"]["nodes"]}["In Progress"]


# falta que é só "ainda espero" (PR aberto, deploy subindo, "volto quando"): a execução da própria issue
# que chama `mover Done` assim não move nem comenta, só recebe a recusa
# (a pendência aberta é o que a própria sessão escreveu no último comentário; o merge fora do ar é o deploy que ela espera)
ESPERA_FALTA = re.compile(r"ainda está aberto|deploy subindo|não está no ar|volto quando|no CI; volto|deixa pendência aberta", re.I)


def so_espera(faltas):
    return bool(faltas) and all(ESPERA_FALTA.search(f) for f in faltas)


REPETE_BARRADO = 3600  # o mesmo "Done barrado" não se posta de novo antes de 1 h


def _barrado_repetido(issue, texto, agora):
    """O último comentário do portão na issue é idêntico a `texto` e tem menos de 60 min."""
    nodes = [c for c in (issue.get("comments") or {}).get("nodes") or []
             if RE_DO_PORTAO.match(c.get("body") or "")]  # sem exigir app: o `mover` com token do dono também posta
    if not nodes:
        return False
    ultimo = max(nodes, key=lambda c: c.get("createdAt") or "")
    try:
        idade = (agora - conta.de_iso(ultimo["createdAt"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return False
    return (ultimo.get("body") or "").strip() == texto.strip() and 0 <= idade < REPETE_BARRADO


def barrar(ident, comentario=None, consulta=None, mover_fn=None, comentar_fn=None, avaliar_fn=None, base_dados=None, agora=None,
           silencioso=False):
    """Chamado ANTES de mover para Done. Devolve [] se pode passar; senão volta a issue para In Progress, comenta o
    motivo e devolve as faltas. Falha de leitura também barra: sem conferência não há recibo.
    `silencioso` (execução da própria issue): se as faltas são só espera, devolve-as sem mover nem comentar."""
    from hub import linear
    consulta = consulta or linear.gql
    issue = consulta(Q_ISSUE, i=ident)["issue"]
    try:
        r = (avaliar_fn or avaliar)(issue, comentario=comentario, base_dados=base_dados)
    except Exception as e:  # noqa: BLE001
        print(f"{ident}: portão de Done não leu o estado ({type(e).__name__}); Done bloqueado", file=sys.stderr)
        faltas, delegadas = faltas_do_texto(issue, comentario)  # sem gh: com a leitura quebrada, não presume PR fundido
        r = {"ok": False, "faltas": faltas + ["recibo não conferido: falha de leitura; tentar novamente"],
             "delegadas": delegadas}
    if r.get("esperar"):  # deploy ainda subindo: não é Done ainda, mas também não é falta; barra sem reabrir a fila
        r = {**r, "faltas": r["faltas"] or r.get("notas") or ["o merge ainda não está no ar (deploy subindo)"]}
    elif r["ok"]:
        _delegar(issue, r, base_dados)
        return []
    if silencioso and so_espera(r["faltas"]):
        return r["faltas"]
    faltas = "; ".join(r["faltas"])
    (mover_fn or linear.mover)(issue["id"], _ip(issue))
    falar = comentar_fn or linear.comentar
    ultimo = max(issue["comments"]["nodes"], key=lambda c: c["createdAt"])["body"] if issue["comments"]["nodes"] else None
    if comentario and (ultimo or "").strip() != comentario.strip():  # o texto que ia junto do Done não se perde, nem se repete
        falar(issue["id"], comentario)
    aviso = f"Done barrado pelo portão (KRN-979): {faltas}. A issue volta para In Progress.\n\n_{config.get('produto.nome')}._"
    if not _barrado_repetido(issue, aviso, agora or conta.agora()):  # a sessão e o fim da execução chamam o portão em par
        falar(issue["id"], aviso)
    return r["faltas"]


def _delegar(issue, r, base_dados=None):
    """Pendência que o texto passa para o dono entra na lista única; para outra issue, nada a fazer aqui."""
    linhas = [l for l in r.get("delegadas") or [] if getattr(l, "dono", False)]
    if linhas:
        registrar_pendencia_fulano(issue["identifier"], issue.get("title") or "", "\n".join(linhas), base_dados)


def conferir_uma(ident, desfazer=False, consulta=None, base_dados=None, avaliar_fn=None, mover_fn=None, comentar_fn=None):
    """Webhook: a issue virou Done (conta, UI, qualquer um). Sem recibo, desfaz: In Progress + motivo (a conta retoma)."""
    from hub import linear
    consulta = consulta or linear.gql
    issue = consulta(Q_ISSUE, i=ident)["issue"]
    if issue["state"]["type"] != "completed":
        return []
    r = (avaliar_fn or avaliar)(issue, base_dados=base_dados)
    if r["ok"] or r.get("esperar"):
        _delegar(issue, r, base_dados)
        return []
    if desfazer:
        faltas = "; ".join(r["faltas"])
        (mover_fn or linear.mover)(issue["id"], _ip(issue))
        (comentar_fn or linear.comentar)(issue["id"], f"Voltou de Done (portão, KRN-979): {faltas}. Volto para fazer.\n\n_{config.get('produto.nome')}._")
        # INC-390: o comentário do portão não é fala do dono; sozinho não
        # acorda a ponte. Use a fila normal, com seus tetos e disjuntor.
        from hub import agenda, esperas
        nome = f"linear-{ident.lower()}"
        ocupadas = {t["nome"] for t in agenda.fila(base_dados)} | {
            t["nome"] for t in agenda.rodando_todas(base_dados)}
        if nome not in ocupadas and not esperas.ler(ident, base_dados) and not r.get("depende_fulano"):
            agenda.enfileirar(linear.reabrir(ident, f"Portão de Done: faça o que falta e prove: {faltas}"),
                              "portão de Done", base_dados)
    return r["faltas"]


# ---- ronda diária ----

Q_RONDA = ("query($d: DateTimeOrDuration!, $a: String) { issues(first: 50, after: $a, filter: {state: {type: {eq: \"completed\"}}, "
           "completedAt: {gt: $d}}) { pageInfo { hasNextPage endCursor } nodes { id identifier title description createdAt completedAt "
           "state { name type } attachments { nodes { url title } } comments(first: 100) { nodes { createdAt body user { app name } } } "
           "team { id states { nodes { id name } } } } } }")


def ronda(dias=3, consulta=None, base_dados=None, avaliar_fn=None, agora=None, log=print):
    """Repete a auditoria nas issues concluídas nos últimos `dias` dias (as 3 classes: PR aberto, PR só fechado, pendência
    no texto) sem reabrir issues antigas. A reabertura é do orquestrador (`reabrir --orquestrador-conferiu`). O nº de achados vai para claude/auditoria-done.json (painel Erros,; meta 0)
    e os achados ficam acumulados em claude/achados-ronda.json até a issue passar na auditoria: a fila que o orquestrador lê na triagem."""
    from hub import linear
    consulta, agora = consulta or linear.gql, agora or conta.agora()
    desde, cursor, achados, limpas = f"-P{int(dias)}D", None, [], []
    while True:
        pag = consulta(Q_RONDA, d=desde, a=cursor)["issues"]
        for i in pag["nodes"]:
            try:
                r = (avaliar_fn or avaliar)(i, base_dados=base_dados, agora=agora)
            except Exception as e:  # noqa: BLE001
                log(f"{i['identifier']}: ronda não leu ({type(e).__name__}: {str(e)[:100]})")
                continue
            if r["ok"] or r.get("esperar"):
                _delegar(i, r, base_dados)
                if r["ok"]:
                    limpas.append(i["identifier"])
                continue
            if excluida(i.get("title")):  # projeto pessoal ou superado: a ronda nunca reabre (dono, 17/10)
                log(f"{i['identifier']}: ronda ignora (projeto superado)")
                continue
            achados.append({"issue": i["identifier"], "faltas": r["faltas"]})
        if not pag["pageInfo"]["hasNextPage"]:
            break
        cursor = pag["pageInfo"]["endCursor"]
    acumular_achados(achados, limpas, base_dados, agora)
    arq = conta.pasta(base_dados) / "auditoria-done.json"
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps({"em": conta.iso(agora), "achados": len(achados), "issues": achados}, ensure_ascii=False), encoding="utf-8")
    log(f"ronda de Done: {len(achados)} achado(s); aguardam triagem do orquestrador")
    return achados


def acumular_achados(achados, limpas, base_dados=None, agora=None):
    """Mantém claude/achados-ronda.json: achado que escapou fica até a issue passar na auditoria (o auditoria-done.json é
    sobrescrito a cada ronda e sozinho perderia o que a ronda de ontem achou)."""
    arq = conta.pasta(base_dados) / "achados-ronda.json"
    ant = {}
    with contextlib.suppress(OSError, ValueError):
        ant = json.loads(arq.read_text(encoding="utf-8"))
    for ident in limpas:
        ant.pop(ident, None)
    for a in achados:
        ant[a["issue"]] = {"faltas": a["faltas"], "desde": ant.get(a["issue"], {}).get("desde") or conta.iso(agora or conta.agora())}
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(ant, ensure_ascii=False, indent=1), encoding="utf-8")
    return ant


# ---- silêncio e exclusões (dono, 17/10: a triagem reabriu o apartamento e mandou render velho ao WhatsApp) ----

EXCLUIDAS = ("apartamento", "evento_exemplo", "fábrica 3d", "fabrica_exemplo", "lobo b3", "reel", "clipe", "render")


def excluida(titulo):
    """Projeto pessoal ou já superado: a triagem nunca reabre."""
    t = (titulo or "").lower()
    return any(x in t for x in EXCLUIDAS)


def _arq_silenciadas(base_dados=None):
    return conta.pasta(base_dados) / "silenciadas-auditoria.json"


def _so_aviso(faltas):
    """Reaberta porque o aviso prometido não saiu: silenciar travaria o portão para sempre, então o aviso pode sair."""
    return bool(faltas) and all("aviso" in f.lower() and "não saiu" in f.lower() for f in faltas)


def silenciada(ident, base_dados=None):
    """Issue reaberta pela auditoria termina em silêncio: o envio ao WhatsApp (entrega_zap) não manda nada dela."""
    with contextlib.suppress(OSError, ValueError):
        return ident in json.loads(_arq_silenciadas(base_dados).read_text(encoding="utf-8"))
    return False


def silenciar(ident, base_dados=None):
    arq = _arq_silenciadas(base_dados)
    try:
        lista = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lista = []
    if ident not in lista:
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps(lista + [ident]), encoding="utf-8")


# ---- reabertura em lotes (triagem das 187 + 7): só o orquestrador chama, depois de olhar a lista ----

LOTE = 10
Q_ESTADO = "query($i: String!) { issue(id: $i) { id identifier title state { type } team { states { nodes { id name } } } } }"


def _arq_fila(base_dados=None):
    return conta.pasta(base_dados) / "reabrir-done.json"


def _ler_fila(base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_arq_fila(base_dados).read_text(encoding="utf-8"))
    return {"fila": [], "reabertas": []}


def _gravar_fila(est, base_dados=None):
    arq = _arq_fila(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(est, ensure_ascii=False, indent=1), encoding="utf-8")


def enfileirar_reabertura(itens, base_dados=None):
    """itens: [{ident, pendencia}] da classe (b). A fila é consumida por `reabrir_lote`, no máximo LOTE reabertas em aberto por vez."""
    est = _ler_fila(base_dados)
    ja = {i["ident"] for i in est["fila"]} | set(est["reabertas"])
    est["fila"] += [i for i in itens if i["ident"] not in ja and not excluida(i.get("titulo"))]
    _gravar_fila(est, base_dados)
    return len(est["fila"])


def reabrir_lote(base_dados=None, confirmado=False, consulta=None, mover_fn=None, comentar_fn=None, log=print):
    """Reabre para Todo as da fila até haver LOTE reabertas ainda abertas (a cota é a das contas: 10 por vez; as frentes de
    plano fora da vez voltam sozinhas ao Backlog pela regra do hub). Pausa de conta ativa: não reabre. O Linear não deixa criar
    label por aqui, então quem está aberta vem da lista `reabertas` (sai dela ao virar Done/Canceled)."""
    from hub import linear
    consulta = consulta or linear.gql
    est = _ler_fila(base_dados)
    if not est["fila"] or conta.pausa_ativa(base_dados):
        return []
    if not confirmado:  # a triagem nunca reabre sozinha (dono, 17/10)
        return []
    est["reabertas"] = [i for i in est["reabertas"] if consulta(Q_ESTADO, i=i)["issue"]["state"]["type"] not in ("completed", "canceled")]
    livres, feitas, resto = LOTE - len(est["reabertas"]), [], []
    for item in est["fila"]:
        if livres <= 0:
            resto.append(item)
            continue
        i = consulta(Q_ESTADO, i=item["ident"])["issue"]
        if i["state"]["type"] != "completed" or excluida(i.get("title")):  # alguém já mexeu, ou projeto superado: sai da fila
            continue
        if not _so_aviso([item["pendencia"]]):
            silenciar(item["ident"], base_dados)
        estados = {s["name"]: s["id"] for s in i["team"]["states"]["nodes"]}
        (mover_fn or linear.mover)(i["id"], estados["Todo"], f"Reaberta pela triagem de Done (KRN-979): {item['pendencia']}\n\n_{config.get('produto.nome')}._")
        feitas.append(item["ident"])
        livres -= 1
    est["fila"], est["reabertas"] = resto, est["reabertas"] + feitas
    _gravar_fila(est, base_dados)
    log(f"reabertura em lote: {len(feitas)} reaberta(s), {len(resto)} na fila")
    return feitas


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("conferir")
    c.add_argument("ident")
    c.add_argument("--desfazer", action="store_true")
    sub.add_parser("reabrir").add_argument("--orquestrador-conferiu", action="store_true")
    r = sub.add_parser("ronda")
    r.add_argument("--dias", type=int, default=3)
    a = p.parse_args(argv)
    if a.cmd == "conferir":
        faltas = conferir_uma(a.ident, desfazer=a.desfazer)
        print("; ".join(faltas) or "ok")
    elif a.cmd == "reabrir":
        reabrir_lote(confirmado=a.orquestrador_conferiu)
    else:
        ronda(a.dias)


if __name__ == "__main__":
    main()
