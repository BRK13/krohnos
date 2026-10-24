"""In Review parada nunca fica esperando alguém olhar (o dono, 21/10: "que porra de demora é essa").

Em 21/10 havia 248 issues em In Review, 230 sem mexida há mais de 6 h, a fila vazia e as contas ociosas. Causa: a sessão
que termina bem vai para In Review (linear.py, `concluir`), e nenhum vigia olhava esse estado. `travadas.varrer` só
consulta In Progress; `esperas` só age em quem gravou `voltar-quando` (2 arquivos para 211 issues); o portão de Done
devolve para In Progress, a sessão responde "segue In Progress" e o hub a move de novo para In Review.

`varrer` (tick da conta 1, no máximo a cada INTERVALO), para cada In Review sem mexida há mais de PARADA_H:
- fora: com espera viva, em fila ou rodando, com proposta pendente, ou com `voltar-em: AAAA-MM-DD` no próprio item
  (data futura; o executor declara a data em comentário ou na descrição);
- PR citado já fundido, no ar e recibo completo (e sem `seo-fix`, INC-117): Done sem IA ("Fechado pelo hub, sem IA");
- o resto volta para a fila, na prioridade da issue, SÓ se há conta ociosa (uma issue por conta ociosa que a fila já
  não cubra), com o que falta no recibo na fala. Cada issue volta no máximo MAX_VOLTAS vezes (a cada COOLDOWN); depois
  disso o orquestrador é chamado (`travadas.chamar`), porque a volta sozinha não resolveu.

Painel e alarme: `medir` grava `in-review.json` (paradas, contas ociosas); mais de P1_LIMITE paradas por P1_MINUTOS
com vaga ociosa não coberta pela fila abre incidente P1 para o orquestrador.

    python3 -m hub.in_review varrer | medir
"""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from hub import conta, esperas, github_api

PARADA_H = 2
INTERVALO = dt.timedelta(minutes=10)  # o Linear tem teto de chamadas por hora; 4 páginas a cada 10 min é pouco
COOLDOWN = dt.timedelta(hours=6)  # a mesma issue não volta em laço: a sessão precisa de tempo e o recibo muda devagar
MAX_VOLTAS = 3
MAX_FECHAR = 15  # por rodada: cada fechamento lê o recibo (GitHub)
ORCAMENTO_S = 90  # por rodada: o tick do hub tem prazo curto; sem teto a varredura de 100+ itens nunca chegava a gravar varrida_em
ORCAMENTO_FOLGADO_S = 300  # com a máquina folgada (carga por núcleo < CARGA_FOLGADA) a rodada inteira cabe
CARGA_FOLGADA = 0.5
PARALELO = 12  # conferências (só leitura: GitHub, Linear) ao mesmo tempo; em série eram ~1 s cada e o orçamento acabava
P1_LIMITE = 10
P1_MINUTOS = 30
RE_DATA = re.compile(r"voltar-em:\s*(\d{4}-\d{2}-\d{2})", re.I)
Q_PARADAS = ("query($d: DateTimeOrDuration!, $a: String) { issues(first: 50, after: $a, filter: {state: {name: {eq: \"In Review\"}}, "
             "updatedAt: {lt: $d}}) { pageInfo { hasNextPage endCursor } nodes { id identifier title updatedAt priority description "
             "labels { nodes { name } } comments(first: 10) { nodes { body createdAt } } } } }")


def _arq(base_dados=None):
    return conta.pasta(base_dados) / "in-review.json"


def estado(base_dados=None):
    try:
        return json.loads(_arq(base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar(est, base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    fd, nome = tempfile.mkstemp(dir=arq.parent, prefix=f".{arq.name}.", suffix=".tmp")
    tmp = Path(nome)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(est, ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o644)  # o painel lê como outro usuário
        tmp.replace(arq)
    finally:
        tmp.unlink(missing_ok=True)


def paradas_na_linear(consulta, agora):
    """Todas as In Review sem mexida há mais de PARADA_H (paginado)."""
    corte, depois, saida = conta.iso(agora - dt.timedelta(hours=PARADA_H)), None, []
    while True:
        r = consulta(Q_PARADAS, d=corte, a=depois)["issues"]
        saida += r["nodes"]
        if not r["pageInfo"]["hasNextPage"]:
            return saida
        depois = r["pageInfo"]["endCursor"]


def _textos(i):
    return [i.get("description") or ""] + [c.get("body") or "" for c in sorted((i.get("comments") or {}).get("nodes") or [], key=lambda c: c.get("createdAt") or "")]


def data_declarada(i):
    """`voltar-em: AAAA-MM-DD` mais recente (comentário ou descrição), ou None."""
    achadas = [m.group(1) for t in _textos(i) for m in RE_DATA.finditer(t)]
    try:
        return dt.date.fromisoformat(achadas[-1]) if achadas else None
    except ValueError:
        return None


def pr_citado(i):
    achados = [m.group(0) for t in _textos(i) for m in esperas.RE_PR.finditer(t)]
    return f"https://{achados[-1]}" if achados else ""


def _propostas_pendentes(listar=None):
    if listar is None:
        from hub import propostas
        listar = lambda: propostas.listar("pendente")  # noqa: E731
    return {p.get("tarefa", "").removeprefix("linear-").upper() for p in listar()}


def fora(i, agora, ocupadas, pendentes, base_dados=None):
    """Motivo para a issue ficar em In Review (ou "" = está parada de verdade)."""
    ident = i["identifier"]
    if f"linear-{ident.lower()}" in ocupadas:
        return "na fila ou rodando"
    e = esperas.ler(ident, base_dados)
    if e and agora - conta.de_iso(e["criada_em"]) < dt.timedelta(hours=float(e.get("max_horas") or esperas.MAX_HORAS)):
        return "espera viva"
    if ident in pendentes:
        return "proposta pendente"
    # INC-388: PR mergeado não resolve uma pausa explícita por decisão do dono.
    # Só a última fala vale: uma mensagem posterior libera a nova avaliação.
    from hub import recibo
    falas = [t for t in _textos(i)[1:] if t.strip()
             and not t.startswith("This thread is for an agent session")]
    ultima = falas[-1] if falas else ""
    if (re.search(r"nenhuma conta deve (?:retomar|retomá-la)\b", ultima, re.I)
            and recibo.RE_DONO.search(ultima)):
        return "retomada suspensa por decisão do dono"
    data = data_declarada(i)
    if data and data > agora.date():
        return f"voltar-em {data}"
    return ""


def _fundido(pr, gh):
    repo, numero = esperas._pr(pr)
    d = gh(f"repos/{repo}/pulls/{numero}")
    return bool(d.get("merged") or d.get("merged_at"))


def orcamento_s(carga=None):
    """Segundos da rodada: ORCAMENTO_FOLGADO_S com a carga por núcleo abaixo de CARGA_FOLGADA; ORCAMENTO_S com a máquina
    cheia ou sem medida. `carga` None mede agora (agenda.carga_por_nucleo)."""
    if carga is None:
        from hub import agenda
        carga = agenda.carga_por_nucleo()
    return ORCAMENTO_FOLGADO_S if carga is not None and carga < CARGA_FOLGADA else ORCAMENTO_S


def _precisa_rede(i):
    """`fechavel` só lê o GitHub e o Linear com PR citado e sem seo-fix; o resto devolve (False, "") na hora."""
    return "seo-fix" not in {lb["name"].lower() for lb in i["labels"]["nodes"]} and bool(pr_citado(i))


def _conferir(paradas, gh, avaliar, consulta):
    """{ident: Future de fechavel} das que leem a rede, em ordem de prioridade, num pool de PARALELO threads.
    Devolve (pool, futuros); quem chama encerra o pool."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=PARALELO, thread_name_prefix="in_review")
    return pool, {i["identifier"]: pool.submit(fechavel, i, gh, avaliar, consulta) for i in paradas if _precisa_rede(i)}


def fechavel(i, gh, avaliar, consulta):
    """(ok, texto): PR citado fundido, recibo completo e no ar, e a issue não é seo-fix. Qualquer dúvida = não fecha."""
    if "seo-fix" in {lb["name"].lower() for lb in i["labels"]["nodes"]}:
        return False, ""  # INC-117: SHA publicado não prova que a regra da auditoria sumiu; a sessão repete a auditoria
    pr = pr_citado(i)
    if not pr or not _fundido(pr, gh):
        return False, ""
    from hub import portao_done
    r = avaliar(consulta(portao_done.Q_ISSUE, i=i["identifier"])["issue"])
    if r.get("ok") and not r.get("esperar"):
        return True, f"PR {pr} fundido, no ar e recibo completo. Fechado pelo hub, sem IA."
    return False, "; ".join(r.get("faltas") or r.get("notas") or [])


def _fala(i, horas, faltas):
    pr = pr_citado(i)
    return (f"In Review há {horas:.0f} h sem mexida e com conta ociosa (KRN-1600): o hub reabriu a issue. "
            f"{('PR: ' + pr + '. ') if pr else ''}"
            f"{('O que falta: ' + faltas + '. ') if faltas else ''}"
            "Termine o que falta e feche com `mover Done` (o portão confere o recibo). Se depende de uma data marcada, "
            "comente `voltar-em: AAAA-MM-DD` e fique em In Review; se depende do dono, diga em uma linha o quê.")


def _devolver(ident, devolver, log):
    from hub import linear
    try:
        (devolver or (lambda x: linear.mover_por_nome(x, "In Review")))(ident)
    except BaseException as e:  # noqa: BLE001 — mover_por_nome sai com SystemExit; a próxima rodada tenta de novo
        log(f"{ident}: não voltou ao In Review ({type(e).__name__}: {str(e)[:120]})")


def varrer(consulta=None, gh=None, avaliar=None, agora=None, base_dados=None, log=print, ociosas=None, ocupadas=None,
           pendentes=None, fechar=None, reabrir=None, enfileirar=None, chamar_orq=None, forcar=False, esperando=None,
           devolver=None, orcamento=None, relogio=time.monotonic):
    """Fecha o que está no ar e reenfileira o parado quando há conta ociosa. Devolve {ident: "fechada"|"reenfileirada"|
    "orquestrador"}. Os parâmetros trocam as pontas (Linear, GitHub, fila) nos testes."""
    from hub import agenda, linear, portao_done, travadas
    agora = agora or conta.agora()
    orcamento = orcamento_s() if orcamento is None else orcamento
    est = estado(base_dados)
    if not forcar and est.get("varrida_em") and agora - conta.de_iso(est["varrida_em"]) < INTERVALO:
        return {}
    consulta = consulta or linear.gql
    gh = gh or (lambda caminho: github_api.get(caminho, consumidor="in_review"))
    avaliar = avaliar or (lambda issue: portao_done.avaliar(issue, gh=gh, base_dados=base_dados, agora=agora))
    if ocupadas is None:
        aguardando = {x["nome"] for x in agenda.fila(base_dados)}
        ocupadas = aguardando | {r["nome"] for r in agenda.rodando_todas(base_dados).values()}
        esperando = aguardando if esperando is None else esperando
    esperando = ocupadas if esperando is None else esperando  # só a fila disputa as contas ociosas; quem roda já tem a sua
    pendentes = _propostas_pendentes() if pendentes is None else pendentes
    livres = ociosas if ociosas is not None else sum(1 for n in conta.contas_config() if agenda.conta_ociosa(n, base_dados, agora))
    na_fila = sum(1 for n in esperando if n.startswith("linear-"))
    vagas = max(0, livres - na_fila)  # a fila que já existe vai ocupar as contas ociosas antes
    voltas = est.setdefault("voltas", {})
    checadas = est.get("checadas") or {}
    paradas = sorted((i for i in paradas_na_linear(consulta, agora) if not fora(i, agora, ocupadas, pendentes, base_dados)),
                     key=lambda i: (checadas.get(i["identifier"], ""), i.get("priority") or 5, i["updatedAt"]))
    # quem ficou sem ser olhado na rodada anterior (orçamento) vai primeiro; dentro disso, Linear: 1 urgente … 4 baixa, 0 sem prioridade, depois a mais velha
    feitos, fechadas = {}, 0
    inicio = relogio()
    novas = {}
    # a conferência (só leitura) de todas as candidatas corre em paralelo antes; em série, ~1 s por item
    # estourava o orçamento em toda rodada. As gravações (fechar, reabrir, enfileirar, voltas) seguem em série, na
    # ordem de prioridade, consumindo o resultado pronto.
    pool, futuros = _conferir(paradas, gh, avaliar, consulta)
    try:
        for i in paradas:
            ident = i["identifier"]
            decorrido = relogio() - inicio
            if decorrido > orcamento:
                log(f"in_review: orçamento de {orcamento} s esgotado; o resto fica para a próxima rodada")
                break
            novas[ident] = conta.iso(agora)
            horas = (agora - conta.de_iso(i["updatedAt"])).total_seconds() / 3600
            faltas = ""
            if fechadas < MAX_FECHAR:
                f = futuros.get(ident)
                if f and not concurrent.futures.wait([f], timeout=max(0.0, orcamento - decorrido)).done:
                    novas.pop(ident)  # a leitura não voltou dentro do orçamento: fica para a próxima rodada
                    log(f"in_review: orçamento de {orcamento} s esgotado; o resto fica para a próxima rodada")
                    break
                try:
                    ok, texto = f.result() if f else fechavel(i, gh, avaliar, consulta)
                except github_api.CotaBaixa:
                    log(f"{ident}: consulta GitHub pausada até o reset da cota")
                    ok, texto = False, ""
                except Exception as e:  # noqa: BLE001 — sem a leitura não fecha; cai no reenfileirar
                    log(f"{ident}: recibo não conferido ({type(e).__name__}: {str(e)[:120]})")
                    ok, texto = False, ""
                if ok:
                    item = linear.item_por_ident(ident)
                    (fechar or linear.encerrar_sem_ia)(item, "Done", texto)
                    esperas.registrar_sem_ia(ident, "in_review_no_ar", pr_citado(i), base_dados, agora)
                    feitos[ident] = "fechada"
                    fechadas += 1
                    log(f"{ident}: In Review há {horas:.0f} h, {texto}")
                    continue
                faltas = texto
            if vagas <= 0:
                continue
            v = voltas.get(ident) or {}
            if v.get("em") and agora - conta.de_iso(v["em"]) < COOLDOWN:
                continue
            if v.get("n", 0) >= MAX_VOLTAS:  # voltou 3 vezes e voltou a parar: a volta sozinha não resolve, o orquestrador decide
                if (chamar_orq or travadas.chamar)(ident, f"In Review parada: voltou {MAX_VOLTAS} vezes para a fila e parou de novo", "vigia do In Review", base_dados, agora):
                    feitos[ident] = "orquestrador"
                    log(f"{ident}: {MAX_VOLTAS} voltas sem resolver; orquestrador chamado")
                continue
            try:
                t = (reabrir or linear.reabrir)(ident, _fala(i, horas, faltas))
                ok = (enfileirar or agenda.enfileirar)(t, f"In Review parada há {horas:.0f} h com conta ociosa (KRN-1600)", base_dados)
            except Exception as e:  # noqa: BLE001 — Linear fora: tenta no próximo ciclo
                log(f"{ident}: não reenfileirou ({type(e).__name__}: {str(e)[:150]})")
                _devolver(ident, devolver, log)
                continue
            if not ok:  # freio, teto ou já na fila: a sessão aberta por reabrir não pode deixar a issue em In Progress sem tarefa
                log(f"{ident}: fila recusou; devolvida ao In Review")
                _devolver(ident, devolver, log)
                continue
            if ok:
                voltas[ident] = {"n": v.get("n", 0) + 1, "em": conta.iso(agora)}
                feitos[ident] = "reenfileirada"
                vagas -= 1
                log(f"{ident}: In Review há {horas:.0f} h com conta ociosa; reenfileirada")
    finally:  # o que ficou para a próxima rodada não segura a coordenação: o pool não espera as leituras pendentes
        pool.shutdown(wait=False, cancel_futures=True)
    est["varrida_em"] = conta.iso(agora)
    est["checadas"] = {k: v for k, v in {**checadas, **novas}.items() if k in {i["identifier"] for i in paradas}}
    _gravar(est, base_dados)
    reenfileiradas = {f"linear-{ident.lower()}" for ident, feito in feitos.items() if feito == "reenfileirada"}
    medir(consulta, agora, base_dados, ocupadas | reenfileiradas, pendentes, livres, log,
          esperando=set(esperando) | reenfileiradas)
    return feitos


def medir(consulta=None, agora=None, base_dados=None, ocupadas=None, pendentes=None, ociosas=None, log=print, incidente=None, esperando=None):
    """Grava para o painel quantas In Review estão paradas (fora as que declaram data, espera, fila ou proposta) e quantas
    contas estão ociosas. Mais de P1_LIMITE paradas por P1_MINUTOS com vaga não coberta pela fila: incidente P1."""
    from hub import agenda, linear
    agora = agora or conta.agora()
    consulta = consulta or linear.gql
    if esperando is None:
        esperando = {x["nome"] for x in agenda.fila(base_dados)}
    if ocupadas is None:
        ocupadas = esperando | {r["nome"] for r in agenda.rodando_todas(base_dados).values()}
    pendentes = _propostas_pendentes() if pendentes is None else pendentes
    livres = ociosas if ociosas is not None else sum(1 for n in conta.contas_config() if agenda.conta_ociosa(n, base_dados, agora))
    paradas = [i for i in paradas_na_linear(consulta, agora) if not fora(i, agora, ocupadas, pendentes, base_dados)]
    n = len(paradas)
    est = estado(base_dados)
    # INC-399: a varredura aguarda seis horas entre voltas. Essa espera deliberada
    # continua visível no painel, mas não representa omissão com conta livre.
    voltas = est.get("voltas") or {}
    elegiveis = sum(1 for i in paradas if not (
        (v := voltas.get(i["identifier"]) or {}).get("em")
        and agora - conta.de_iso(v["em"]) < COOLDOWN))
    est.update(paradas=n, elegiveis=elegiveis, ociosas=livres, medida_em=conta.iso(agora))
    # Como na varredura, a fila já cobre as contas livres. Só há omissão se sobra vaga.
    vagas = max(0, livres - sum(1 for nome in esperando if nome.startswith("linear-")))
    if elegiveis > P1_LIMITE and vagas > 0:
        est.setdefault("acima_desde", conta.iso(agora))
    else:
        est.pop("acima_desde", None)
    if est.get("acima_desde") and agora - conta.de_iso(est["acima_desde"]) >= dt.timedelta(minutes=P1_MINUTOS):
        with contextlib.suppress(Exception):
            from hub import incidentes
            (incidente or incidentes.registrar)(
                f"{elegiveis} issues em In Review paradas há mais de {PARADA_H} h por mais de {P1_MINUTOS} min "
                f"({livres} conta(s) ociosa(s))", "in_review_parada", "hub/in_review.py (varrer: fecha o que está no ar, reenfileira o resto)",
                chave="in-review-paradas", base_dados=base_dados, agora=agora, prioridade=1)
    _gravar(est, base_dados)
    return est


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.in_review")
    ap.add_argument("cmd", choices=("varrer", "medir"))
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(varrer(forcar=True) if a.cmd == "varrer" else medir(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
