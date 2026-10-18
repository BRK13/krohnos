"""Retomada sem IA de verdade: tipo obrigatório, teto de execuções por tarefa e a meta medida.

Medido em 16-17/10 (uso.jsonl, `linear-krn-*`): espera_ci 313 sessões, conferência 187, retomada sem tipo 120; uma tarefa
rodava 4 a 7 vezes (e 828, 35). A espera de deploy já era sem IA; o resto abria uma
sessão Claude só para ler um resultado que o código lê. Aqui:

- `exigir_tipo`: toda execução grava um tipo de `tokens.TIPOS`; vazio ou desconhecido vira "outro" E incidente.
- `teto`: 6 execuções desde a última fala → aviso na issue; 10 → a tarefa pausa, o orquestrador é chamado com o resumo e os
  números, e só uma fala nova (a decisão dele, ou do dono) libera. A contagem recomeça nessa fala.
- `gancho_mecanico`: tarefa parada ou que "promete voltar" com um PR citado ganha a espera por código (`voltar-quando`) em
  vez de uma sessão; `conferir_mecanica`: a pergunta de conferência que o código responde (PR entrou, está no ar, URL 200)
  é respondida pelo recibo (`portao_done.avaliar`): passou, fecha; falhou, abre a sessão com o motivo pronto.
- `medir`: execuções por tarefa (meta ≤ 2 nas pequenas, ≤ 4 nas grandes), % de retomadas sem IA (meta ≥ 70%) e a série
  diária por tipo, para a aba Retomadas de /uso.

    python3 -m hub.retomada medir [--dias 2]
"""
import argparse
import collections
import contextlib
import datetime as dt
import json
import sys

from hub import conta, esperas, tokens

AVISO_EXECUCOES = 6
TETO_EXECUCOES = 10
META_PEQUENA, META_GRANDE = 2, 4  # execuções por tarefa
META_SEM_IA = 70.0  # % das retomadas resolvidas por código
GRANDE_MIN, GRANDE_USD = 90, 20  # tarefa grande: somou 90 min de execução ou US$ 20 (equivalente de API) na janela
HISTORICO = dt.timedelta(days=30)
SUFIXO = "execuções desde a última fala"
PR_RECENTE = dt.timedelta(days=7)  # PR fundido há mais que isso, citado no texto, é referência e não o PR da tarefa


def _arq(base_dados=None):
    return conta.pasta(base_dados) / "teto-execucoes.json"


def _estado(base_dados=None):
    try:
        return json.loads(_arq(base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar(est, base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(est), encoding="utf-8")
    tmp.replace(arq)


def exigir_tipo(tipo, nome="", base_dados=None, agora=None, registrar=None):
    """Tipo da execução, sempre de `tokens.TIPOS`. Vazio ou fora do conjunto: incidente e "outro" (nunca "?")."""
    if tipo in tokens.TIPOS and tipo != "outro":  # "outro" é o que `tipo_execucao` devolve para motivo que ninguém classificou
        return tipo
    with contextlib.suppress(Exception):  # o incidente nunca derruba o registro da execução
        if registrar is None:
            from hub import incidentes
            registrar = incidentes.registrar
        registrar(f"execução de {nome or '?'} sem tipo conhecido ({tipo!r})", "outra", "hub/retomada.py (exigir_tipo)",
                  nome.removeprefix("linear-").upper() if nome.startswith("linear-") else "",
                  chave=f"sem-tipo:{nome}:{(agora or conta.agora()):%Y%m%d%H}", base_dados=base_dados, agora=agora)
    return "outro"


def _janela(nome, ultima_fala, base_dados=None):
    """(corte, marcas da issue). A janela começa na última fala; aviso e pausa gravados antes dela já não valem."""
    corte = conta.de_iso(ultima_fala) if ultima_fala else conta.agora() - HISTORICO
    marcas = {k: v for k, v in (_estado(base_dados).get(nome) or {}).items() if conta.de_iso(v) > corte}
    return corte, marcas


def execucoes(nome, ultima_fala=None, base_dados=None):
    """Execuções da issue na janela atual (limite de uso e sinal não contam: não são trabalho da issue)."""
    corte, _ = _janela(nome, ultima_fala, base_dados)
    return [r for r in conta.ler_uso(corte, base_dados) if r.get("nome") == nome and conta.de_iso(r["inicio"]) > corte
            and not (r.get("limite") or r.get("interrompida"))]


def teto(nome, ultima_fala=None, base_dados=None):
    """Motivo (texto) se a tarefa já rodou TETO_EXECUCOES vezes desde a última fala; senão None."""
    n = len(execucoes(nome, ultima_fala, base_dados))
    return f"rodou {n} {SUFIXO} (teto {TETO_EXECUCOES})" if n >= TETO_EXECUCOES else None


def resumo_numeros(regs):
    """Os números que vão ao orquestrador e à issue: execuções por tipo, custo e duração."""
    por_tipo = collections.Counter(r.get("tipo") or "sem_tipo" for r in regs)
    custo = sum(float(r.get("custo_usd") or 0) for r in regs)
    minutos = sum(float(r.get("duracao_s") or 0) for r in regs) / 60
    tipos = ", ".join(f"{k} {v}" for k, v in por_tipo.most_common())
    return f"{len(regs)} execuções ({tipos}); US$ {custo:.2f} equivalente de API; {minutos:.0f} min de sessão"


def pausar(item, motivo, base_dados=None, agora=None, chamar_orq=None, comentar=None):
    """A tarefa chegou ao teto: grava a pausa (uma por janela), comenta na issue e chama o orquestrador com o resumo.
    Quem chama NÃO enfileira. `chamar_orq(ident, motivo, origem)` repete a cada ciclo; o dedupe é de quem o passa."""
    nome = f"linear-{item['identifier'].lower()}"
    agora = agora or conta.agora()
    atual = _janela(nome, item.get("ultima_fala"), base_dados)[1]
    ja = bool(atual.get("teto"))
    numeros = resumo_numeros(execucoes(nome, item.get("ultima_fala"), base_dados))
    if chamar_orq:
        chamar_orq(item["identifier"], f"teto de {TETO_EXECUCOES} execuções: {motivo}. {numeros}", "teto de execuções")
    if ja:
        return False
    todo = _estado(base_dados)
    todo[nome] = {**atual, "teto": conta.iso(agora)}
    _gravar(todo, base_dados)
    texto = (f"Teto: {motivo}. {numeros}. Pausei a tarefa e chamei o orquestrador; a próxima fala dele (o rumo) libera "
             "e a contagem recomeça nela.")
    if comentar:
        with contextlib.suppress(Exception):
            comentar(item, texto)
    return True


def avisar_seis(item, base_dados=None, agora=None, comentar=None):
    """Aos AVISO_EXECUCOES, um comentário na issue (uma vez por janela). Devolve True se avisou."""
    nome = f"linear-{item['identifier'].lower()}"
    regs = execucoes(nome, item.get("ultima_fala"), base_dados)
    if len(regs) < AVISO_EXECUCOES:
        return False
    corte, est = _janela(nome, item.get("ultima_fala"), base_dados)
    if est.get("aviso"):
        return False
    todo = _estado(base_dados)
    todo[nome] = {**est, "aviso": conta.iso(agora or conta.agora())}
    _gravar(todo, base_dados)
    if comentar:
        with contextlib.suppress(Exception):
            comentar(item, f"Aviso: {len(regs)} execuções desta tarefa desde a última fala ({resumo_numeros(regs)}). "
                           f"Aos {TETO_EXECUCOES} eu pauso e chamo o orquestrador. Feche num PR, ou diga o que trava.")
    return True


# ---- o que o código responde sem abrir sessão ----

def gancho_mecanico(ident, texto, gravar=None, gh=None, base_dados=None, agora=None):
    """O último texto cita um PR aberto (ou fundido há menos de 7 dias)? Grava a espera por código (`voltar-quando`:
    CI e revisão, ou o deploy se já fundido) e devolve a espera; senão None. Evita a sessão que só serviria para chamar
    `voltar-quando` (tarefa parada e promessa sem gancho)."""
    gravar, gh = gravar or esperas.gravar, gh or esperas.gh_api
    achados = esperas.RE_PR.findall(texto or "")
    if not achados:
        return None
    repo, n = achados[-1]
    pr = gh(f"repos/{repo}/pulls/{n}")
    agora = agora or conta.agora()
    if pr.get("merged") or pr.get("merged_at"):
        if agora - conta.de_iso(pr.get("merged_at") or conta.iso(agora)) > PR_RECENTE:
            return None
        return gravar(ident, f"https://github.com/{repo}/pull/{n}", chamar=gh, base_dados=base_dados, agora=agora, no_ar="")
    if pr.get("state") == "closed":
        return None
    return gravar(ident, f"https://github.com/{repo}/pull/{n}", chamar=gh, base_dados=base_dados, agora=agora)


def conferir_mecanica(item, texto, issue=None, avaliar=None, gancho=None):
    """A conferência apontou falta que é pergunta de código ("está no ar?", "a URL responde?", "o PR entrou?"):
    o recibo (portao_done.avaliar) responde. {"resultado": "ok"|"esperar"|"falha"|"indisponivel", "faltas": [...]}.
    ok = tudo confere, nada a refazer; esperar = o deploy ainda sobe, a espera por código foi gravada; falha = a sessão
    abre com `faltas` (o motivo pronto); indisponivel = não deu para ler: caminho de sempre."""
    try:
        if issue is None:
            from hub import linear, portao_done
            issue = linear.gql(portao_done.Q_ISSUE, i=item["identifier"])["issue"]
        if avaliar is None:
            from hub import portao_done
            avaliar = portao_done.avaliar
        r = avaliar(issue, comentario=texto)
        if r.get("ok"):
            return {"resultado": "ok", "faltas": []}
        if r.get("esperar") and not r.get("faltas"):
            espera = (gancho or gancho_mecanico)(item["identifier"], texto)
            return {"resultado": "esperar" if espera else "indisponivel", "faltas": []}
        faltas = list(r.get("faltas") or [])
        return {"resultado": "falha" if faltas else "indisponivel", "faltas": faltas}
    except Exception:  # noqa: BLE001 — sem ler: volta ao caminho de hoje (sessão), nunca fecha por engano
        return {"resultado": "indisponivel", "faltas": []}


# ---- a meta medida ----

def _grande(regs):
    return (sum(float(r.get("duracao_s") or 0) for r in regs) / 60 >= GRANDE_MIN
            or sum(float(r.get("custo_usd") or 0) for r in regs) >= GRANDE_USD)


def ler_sem_ia(base_dados=None, desde=None):
    """Linhas de sem-ia.jsonl (volta resolvida por código; escritas por esperas.registrar_sem_ia)."""
    arq = conta.pasta(base_dados) / "sem-ia.jsonl"
    saida = []
    try:
        for linha in arq.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError, KeyError):
                r = json.loads(linha)
                if desde is None or conta.de_iso(r["quando"]) >= desde:
                    saida.append(r)
    except OSError:
        pass
    return saida


def medir(usos, sem_ia, agora=None, dias=2):
    """{por_tarefa, pequenas, grandes, retomadas, sem_ia, pct_sem_ia, diario, sem_tipo, metas}. Só execuções de issue."""
    agora = agora or conta.agora()
    desde = agora - dt.timedelta(days=dias)
    regs = [r for r in usos if str(r.get("nome") or "").startswith("linear-") and tokens._quando(r)
            and desde <= tokens._quando(r) <= agora and not (r.get("limite") or r.get("interrompida"))]
    por = collections.defaultdict(list)
    for r in regs:
        por[tokens.issue_de(r)].append(r)
    tarefas = [{"issue": k, "execucoes": len(v), "grande": _grande(v), "custo_usd": round(
        sum(float(r.get("custo_usd") or 0) for r in v), 2)} for k, v in por.items()]
    tarefas.sort(key=lambda t: -t["execucoes"])
    pequenas, grandes = [t for t in tarefas if not t["grande"]], [t for t in tarefas if t["grande"]]

    def media(ts):
        return round(sum(t["execucoes"] for t in ts) / len(ts), 2) if ts else 0.0
    com_ia = sum(1 for r in regs if (r.get("tipo") or "sem_tipo") in tokens.TIPOS_RETOMADA or not r.get("tipo"))
    sem = sum(1 for x in sem_ia if conta.de_iso(x["quando"]) >= desde)
    pct = round(100 * sem / (sem + com_ia), 1) if sem + com_ia else 0.0
    diario = collections.defaultdict(collections.Counter)
    for r in regs:
        diario[tokens._quando(r).strftime("%Y-%m-%d")][r.get("tipo") or "sem_tipo"] += 1
    for x in sem_ia:
        if conta.de_iso(x["quando"]) >= desde:
            diario[conta.de_iso(x["quando"]).strftime("%Y-%m-%d")]["sem_ia"] += 1
    return {"dias": dias, "por_tarefa": tarefas, "media_pequenas": media(pequenas), "media_grandes": media(grandes),
            "acima_da_meta": [t for t in tarefas if t["execucoes"] > (META_GRANDE if t["grande"] else META_PEQUENA)],
            "retomadas_com_ia": com_ia, "retomadas_sem_ia": sem, "pct_sem_ia": pct,
            "sem_tipo": sum(1 for r in regs if not r.get("tipo")),
            "diario": {k: dict(v) for k, v in sorted(diario.items())},
            "metas": {"pequenas": media(pequenas) <= META_PEQUENA, "grandes": media(grandes) <= META_GRANDE,
                      "sem_ia": pct >= META_SEM_IA}}


def main(argv=None):
    p = argparse.ArgumentParser(prog="hub.retomada")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("medir")
    m.add_argument("--dias", type=float, default=2)
    args = p.parse_args(argv)
    r = medir(conta.ler_uso(), ler_sem_ia(), dias=args.dias)
    print(f"Janela {args.dias:g} d: média de execuções por tarefa pequena {r['media_pequenas']} (meta ≤ {META_PEQUENA}), "
          f"grande {r['media_grandes']} (meta ≤ {META_GRANDE}); retomadas sem IA {r['pct_sem_ia']}% (meta ≥ {META_SEM_IA:g}%: "
          f"{r['retomadas_sem_ia']} sem × {r['retomadas_com_ia']} com); sem tipo {r['sem_tipo']}")
    for t in r["por_tarefa"][:10]:
        print(f"  {t['issue']:<10} {t['execucoes']:>3} exec  US$ {t['custo_usd']:>7.2f}{'  (grande)' if t['grande'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
