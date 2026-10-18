"""Portão de Done: issue de dado (coleta, carga, classificação, leitura por IA) só fecha com prova de que rodou.

O dono (16/10): foi Done só com o código (0 teses e 0 óbices lidos); o STJ ficou 8 dias parado com o job dizendo
"0 erros". A entrega de uma issue de dado precisa do bloco "Prova de execução" no texto final:

    ## Prova de execução
    - Antes: 0 teses. Depois: 1.482 (consulta ou log).
    - Agendamento: cron instalado em deploy/nimbus.cron (diário 03:00).

Sem o bloco a issue volta para In Progress com o motivo (`hub.linear._concluir` e `hub.linear mover ... Done`). Se a
entrega diz "não roda em massa", o hub abre sozinho a issue de execução (uma por issue) e ela entra no resumo.
Eventos em $HUB_DATA/portao.jsonl; `parado_ou_so_codigo` alimenta o resumo diário (hub.resumo).
"""
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from hub import data, gh, github_api

UTC = dt.timezone.utc
TEXTO_DADO = re.compile(
    r"\b(colet(?:a|ar|ador)|importa(?:r|ção)|classifica(?:r|ção)|backfill|moedura|moer|carga\s+(?:de|dos|das|em)|"
    r"leitura\s+por\s+ia|(?:processar|rodar)\s+(?:a|o|os|as)?\s*(?:coleta|carga|classifica\w+|importa\w+|ingest\w+))\b", re.I)
ARQUIVO_DADO = re.compile(r"(^|/)coleta/|importar|classificar|\.cron$|(^|/)cron/", re.I)
SEM_MASSA = re.compile(r"n[ãa]o\s+(?:roda|rodei|rodou|rodamos|rodar)\s+(?:em\s+massa|a\s+carga)|sem\s+rodar\s+em\s+massa", re.I)
BLOCO = re.compile(r"^#{0,6}\s*\**\s*Prova de execu[çc][ãa]o\b.*?$(.*?)(?=^#{1,6}\s|\Z)", re.I | re.S | re.M)
ANTES_DEPOIS = re.compile(r"antes.*depois|depois.*antes", re.I | re.S)
AGENDA = re.compile(r"\bcron\b|agendad|agendamento|systemd|timer", re.I)


def eh_de_dado(item, arquivos):
    texto = f"{item.get('titulo', '')}\n{item.get('descricao', '')}"
    return bool(TEXTO_DADO.search(texto) or any(ARQUIVO_DADO.search(a) for a in arquivos))


def faltou_prova(texto):
    """"" se o texto tem o bloco com contagem antes/depois e agendamento; senão o que falta, em uma frase."""
    m = BLOCO.search(texto or "")
    if not m:
        return 'falta o bloco "Prova de execução" (contagem antes/depois e agendamento) no comentário final'
    corpo = m.group(1)
    falta = []
    if not (ANTES_DEPOIS.search(corpo) and len(re.findall(r"\d", corpo)) >= 2):
        falta.append("a contagem antes/depois (consulta ou log, com números)")
    if not AGENDA.search(corpo):
        falta.append("o agendamento (cron instalado)")
    return ("no bloco \"Prova de execução\" falta " + " e ".join(falta)) if falta else ""


def _arq(base=None):
    return Path(base or data()) / "portao.jsonl"


def _linhas(base=None):
    try:
        textos = _arq(base).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    saida = []
    for t in textos:
        try:
            saida.append(json.loads(t))
        except ValueError:
            continue
    return saida


def _registrar(item, evento, detalhe="", base=None, agora=None):
    reg = {"em": (agora or dt.datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"), "issue": item.get("identifier", "?"),
           "titulo": (item.get("titulo") or "")[:120], "evento": evento, "detalhe": detalhe[:300]}
    arq = _arq(base)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(reg, ensure_ascii=False) + "\n")


def arquivos_do_pr(texto):
    """Arquivos dos PRs citados no texto (gh pr diff --name-only). Sem gh ou sem PR: []."""
    arquivos = []
    for url in dict.fromkeys(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", texto or "")):
        try:
            r = gh.executar(["pr", "diff", url, "--name-only"], "portao", capture_output=True, text=True, timeout=60)
            arquivos += r.stdout.split() if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, github_api.CotaBaixa):
            continue
    return arquivos


def avaliar(item, texto, arquivos_fn=arquivos_do_pr, criar_fn=None, base=None):
    """Portão da entrega. Devolve {"ok", "motivo", "execucao"}: `ok` False = volta para In Progress com `motivo`;
    `execucao` = "KRN-N url" da issue de execução aberta agora porque a entrega diz "não roda em massa"."""
    if not (eh_de_dado(item, []) or eh_de_dado(item, arquivos_fn(texto) if arquivos_fn else [])):  # gh só se o texto não bastou
        return {"ok": True, "motivo": "", "execucao": ""}
    motivo = faltou_prova(texto)
    if not motivo:
        _registrar(item, "provado", base=base)
        return {"ok": True, "motivo": "", "execucao": ""}
    execucao = ""
    texto_todo = f"{item.get('descricao', '')}\n{texto}"
    if SEM_MASSA.search(texto_todo):
        ja = any(r["issue"] == item.get("identifier") and r["evento"] == "execucao_aberta" for r in _linhas(base))
        if not ja:
            criar = criar_fn
            if criar is None:
                from hub import linear
                criar = linear.criar_issue
            execucao = criar("ClienteJ", f"Executar a carga: {item.get('titulo', '')}"[:250],
                             f"O código de {item.get('identifier')} está pronto e não roda em massa. Rode, prove com contagem "
                             "antes/depois e instale o cron; só então fecha (bloco \"Prova de execução\", KRN-973).",
                             "Todo", ("dados",), 2)
            _registrar(item, "execucao_aberta", execucao, base=base)
        return {"ok": True, "motivo": "", "execucao": execucao}
    _registrar(item, "voltou", motivo, base=base)
    return {"ok": False, "motivo": motivo, "execucao": ""}


def parado_ou_so_codigo(base=None, agora=None, dias=14):
    """Linhas do resumo diário: issue de dado que voltou ou ficou só em código e ainda não provou que rodou."""
    corte = (agora or dt.datetime.now(UTC)) - dt.timedelta(days=dias)
    ultimo = {}
    for r in _linhas(base):
        try:
            if dt.datetime.strptime(r["em"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) >= corte:
                ultimo[r["issue"]] = r
        except (KeyError, ValueError):
            continue
    rotulo = {"voltou": "só código, sem prova de execução (voltou para In Progress)",
              "execucao_aberta": "código pronto, execução em issue própria"}
    return [f"- {i} {r['titulo']}: {rotulo[r['evento']]}" + (f" ({r['detalhe']})" if r["evento"] == "execucao_aberta" else "")
            for i, r in ultimo.items() if r["evento"] in rotulo]
