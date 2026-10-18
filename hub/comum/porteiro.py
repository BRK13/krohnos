"""Porteiro do WhatsApp do dono. Na noite de 15/10 chegaram 148 mensagens: reenvio de 48 h em rajada, a mesma entrega
por três remetentes, aviso técnico de sistema. Toda mensagem ao dono passa por aqui (hub/comum/caixa.py chama antes de
gravar na caixa, então vale para o recibo, as contas e a CLI `hub.avisar`):

1. técnico (teto de API, tarefa com falha, CI, deploy, auditoria) NUNCA vai ao dono: vai ao diário do orquestrador;
2. dedupe entre remetentes: registro único por (issue, hash do conteúdo do arquivo); arquivo já mandado não repete;
3. só a versão final: arquivo novo de issue já enviada nas últimas 24 h vai como "versão nova de X"; uma vez por dia por
   issue, salvo pedido do dono (`pedido=True`); o resto fica na fila e a versão mais nova substitui a da fila;
4. madrugada (22h-7h): nada sai; às 8h vai UM resumo "N entregas prontas" (até 4 arquivos direto e o resto em link; zip nunca);
5. teto de 10 por hora e 40 por dia: passou, o que sobra junta no mesmo resumo (a fila é uma só);
6. relatório: mensagens por dia e por origem (`python3 -m hub.comum.porteiro relatorio`), meta menos de 20 por dia.
Mensagem para outro número (`para` diferente do dono) não passa por aqui.
Estado: <data>/claude/zap-dono.json (registro, fila, envios), com trava de arquivo."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

from hub.comum import datas

TETO_HORA = 10
TETO_DIA = 40
MADRUGADA_DE, MADRUGADA_ATE, RESUMO_AS = 22, 7, 8  # horas BRT: nada sai de 22h a 7h; a fila sai a partir das 8h
JANELA_VERSAO = dt.timedelta(hours=24)
RESUMO_SOLTOS = 4  # arquivos diretos no resumo; o resto fica no link da issue (zip nunca: o dono não abre no celular)
META_DIA = 20
LINEAR = "https://linear.app/fulanopereira/issue/"
RE_ISSUE = re.compile(r"\bKRN-\d+\b", re.I)
RE_TECNICO = re.compile(
    r"teto di[áa]rio|teto (?:total )?da (?:api|reserva)|Reserva API|tarefas?(?:\(s\))? com falha|Linear: \d* ?tarefa|FALHOU|\bFAIL\b|\bWARN\b|"
    r"local-ci|\bCI\b|deploy|auditoria|cliente esperando|agenda parada|login do orch|Orquestrador:|traceback|"
    r"serviço (?:est[áa] )?parado|Pedido vps|disjuntor|\bHTTP [45]\d\d\b", re.I)
# (lição 14): nota técnica (PR, commit/SHA, teste, traceback, merge) é do orquestrador, não do dono: com 2 marcas ou mais,
# sem arquivo anexo, desvia como o aviso técnico (INC-56: a nota do grupo da fsite05 chegava no zap do Fulano).
MARCAS_NOTA = (r"\bPRs?\s*#?\d+|/pull/\d+", r"\bcommit\b|`[0-9a-f]{7,40}`|\b[0-9a-f]{40}\b",
               r"\b(?:testes?|tsc|vitest|unittest|eslint|lint|local-ci)\b[^.\n]{0,40}(?:passa|verde|limp|falh|vermelh)",
               r"traceback|\bError:|stack trace", r"\bmerge(?:i|ei|ado|ar)?\b")
PRIORIDADE = {".pdf": 0, ".docx": 0, ".xlsx": 0, ".mp4": 1, ".mov": 1, ".webm": 1, ".mp3": 3, ".m4a": 3, ".ogg": 3, ".wav": 3}
IMAGENS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


# ---- estado ----

def _arq(base_dados=None):
    from hub import data
    return Path(base_dados or data()) / "claude" / "zap-dono.json"


@contextlib.contextmanager
def _trava(base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq.with_suffix(".lock"), "w") as f:
        if os.name == "posix":
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _ler(base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        est = json.loads(_arq(base_dados).read_text(encoding="utf-8"))
        if isinstance(est, dict):
            return {"registro": est.get("registro") or {}, "fila": est.get("fila") or [], "envios": est.get("envios") or []}
    return {"registro": {}, "fila": [], "envios": []}


def _gravar(est, base_dados=None, agora=None):
    corte = ((agora or datas.agora()) - dt.timedelta(days=60)).astimezone(datas.UTC).isoformat()
    est["envios"] = [e for e in est["envios"] if e["em"] >= corte]  # 60 dias bastam para o relatório
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def _iso(t):
    return t.astimezone(datas.UTC).isoformat(timespec="seconds")


# ---- identificação ----

def _ler_admin_do_bot():
    """TRIAGEM_ADMIN do contêiner do bot, lido pelo docker (nunca impresso). "" se não achar."""
    import subprocess
    with contextlib.suppress(Exception):
        ids = subprocess.run(["docker", "ps", "-q", "--filter", "name=infra-asite01-wpp"],  # Swarm põe sufixo no nome
                             capture_output=True, text=True, timeout=20).stdout.split()
        r = subprocess.run(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", ids[0]],
                           capture_output=True, text=True, timeout=20)
        m = re.search(r"^TRIAGEM_ADMIN=(\S+)", r.stdout, re.M)
        return re.sub(r"\D", "", m.group(1)) if m else ""
    return ""


_CACHE_BOT = {}


def _numero_do_bot():
    """Só guarda o acerto: falha transitória do Docker não pode ficar presa até reiniciar o processo."""
    if "n" in _CACHE_BOT:
        return _CACHE_BOT["n"]
    n = _ler_admin_do_bot()
    if re.fullmatch(r"\d{10,15}", n or ""):
        _CACHE_BOT["n"] = n
        return n
    return ""


_numero_do_bot.cache_clear = _CACHE_BOT.clear


def numero_dono():
    """Dígitos do WhatsApp do dono, ou "" (config dono.whatsapp, TRIAGEM_ADMIN, depois o env do contêiner do bot).
    O hub não tem TRIAGEM_ADMIN no ambiente: sem o último, o `para` do recibo nunca batia com o dono (17/10)."""
    from hub import config
    n = re.sub(r"\D", "", config.get("dono.whatsapp") or os.environ.get("TRIAGEM_ADMIN") or "")
    return n or _numero_do_bot()


def eh_dono(para):
    """Sem `para` o bot manda ao dono; com `para`, conta se for o número dele. Falha fechada: se o número do dono não
    se deixa descobrir, todo `para` vale como dono (o teto e a madrugada seguram), em vez de abrir a porta."""
    if not para:
        return True
    n = re.sub(r"\D", "", str(para))
    if not n:
        return False
    dono = numero_dono()
    return not dono or n == dono


def tecnico(texto, tipo=None, tem_arquivo=False):
    """Erro de sistema, não decisão nem entrega. `tipo` explícito manda; entrega com arquivo nunca é técnica."""
    if tipo:
        return tipo == "tecnico"
    return not tem_arquivo and bool(RE_TECNICO.search(texto or "") or nota_tecnica(texto))


def nota_tecnica(texto):
    return sum(bool(re.search(m, texto or "", re.I)) for m in MARCAS_NOTA) >= 2


def issue_de(texto, tarefa=None):
    m = re.fullmatch(r"linear-(krn-\d+)", (tarefa or "").lower())
    if m:
        return m.group(1).upper()
    m = RE_ISSUE.search(texto or "")
    return m.group(0).upper() if m else None


def issues_prontas(texto):
    """Só o cabeçalho de entrega afirma conclusão; citar uma issue num pedido não afirma pronto."""
    m = re.match(r"^(?:(KRN-\d+)\s+pronta\b|vers[ãa]o nova de\s+(KRN-\d+)\s*:)",
                 (texto or "").strip(), re.I)
    return [(m.group(1) or m.group(2)).upper()] if m else []


def sha_conteudo(caminho):
    """Hash do conteúdo. Zip: nomes e CRCs dos membros (o mesmo zip refeito tem outros bytes por causa das datas)."""
    caminho = Path(caminho)
    h = hashlib.sha256()
    if caminho.suffix.lower() == ".zip":
        with contextlib.suppress(OSError, zipfile.BadZipFile):
            with zipfile.ZipFile(caminho) as z:
                for i in sorted(z.infolist(), key=lambda i: i.filename):
                    h.update(f"{i.filename}:{i.CRC}:{i.file_size};".encode())
            return "zip:" + h.hexdigest()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _shas(arquivos, base):
    """[(relativo, sha)]; arquivo que sumiu fica sem sha e passa direto (o bot reclama dele, não o porteiro)."""
    saida = []
    for rel in arquivos or []:
        try:
            saida.append((rel, sha_conteudo(Path(base) / rel)))
        except OSError:
            saida.append((rel, None))
    return saida


# ---- contas ----

def _contar(est, agora):
    hora = _iso(agora - dt.timedelta(hours=1))
    dia = agora.astimezone(datas.BRT).date()
    enviados = [e for e in est["envios"] if e.get("tipo") != "desviado" and e.get("destino") != "fila"]
    return (sum(e["em"] >= hora for e in enviados),
            sum(dt.datetime.fromisoformat(e["em"]).astimezone(datas.BRT).date() == dia for e in enviados))


def madrugada(agora):
    h = agora.astimezone(datas.BRT).hour
    return h >= MADRUGADA_DE or h < MADRUGADA_ATE


def _ultimo_da_issue(est, issue):
    ultimos = [dt.datetime.fromisoformat(r["em"]) for r in est["registro"].get(issue, [])]
    return max(ultimos) if ultimos else None


def _registrar(est, issue, shas, agora):
    reg = est["registro"].setdefault(issue or "_", [])
    for _, sha in shas:
        if sha:
            reg.append({"sha": sha, "em": _iso(agora)})
    corte = _iso(agora - dt.timedelta(days=30))
    for k in list(est["registro"]):
        est["registro"][k] = [r for r in est["registro"][k] if r["em"] >= corte]
        if not est["registro"][k]:
            del est["registro"][k]


# ---- decisão ----

def passar(texto, agora=False, caixa=None, arquivos=None, para=None, origem=None, tipo=None, pedido=False,
           base_dados=None, quando=None, gravar=None, desviar=None):
    """Decide o destino de uma mensagem ao dono. Devolve o caminho gravado pelo `gravar`, ou None (desviada, repetida
    ou na fila). `gravar(texto, arquivos)`: o escritor cru da caixa; `desviar(texto)`: para onde vai o técnico."""
    quando = quando or datas.agora()
    caixa = Path(caixa) if caixa else None
    base = caixa.parent if caixa else Path(".")
    tarefa = os.environ.get("TAREFA_NOME")
    origem = origem or tarefa or "hub"
    issue = issue_de(texto, tarefa)
    zips = [a for a in arquivos or [] if str(a).lower().endswith(".zip")]
    if zips:  # o dono não abre zip no celular: sai o texto, sem o zip
        arquivos = [a for a in arquivos if a not in zips]
        texto += "\n(Há um zip que não vai ao WhatsApp; está na issue.)"
    if tecnico(texto, tipo, bool(arquivos)):
        with _trava(base_dados):
            est = _ler(base_dados)
            est["envios"].append({"em": _iso(quando), "origem": origem, "issue": issue, "tipo": "desviado"})
            _gravar(est, base_dados, quando)
        if desviar:
            with contextlib.suppress(Exception):  # o desvio nunca derruba quem avisou
                desviar(texto)
        return None
    with _trava(base_dados):
        est = _ler(base_dados)
        shas = _shas(arquivos, base)
        if arquivos:
            ja = {r["sha"] for r in est["registro"].get(issue or "_", [])}
            novos = [(rel, sha) for rel, sha in shas if not sha or sha not in ja]
            if not novos:  # tudo isto já foi ao dono (por qualquer remetente)
                est["envios"].append({"em": _iso(quando), "origem": origem, "issue": issue, "tipo": "repetido"})
                _gravar(est, base_dados, quando)
                return None
            shas, arquivos = novos, [rel for rel, _ in novos]
        ultimo = _ultimo_da_issue(est, issue) if issue and arquivos else None
        if ultimo and quando - ultimo < JANELA_VERSAO and not re.match(r"vers[ãa]o nova de", texto, re.I):
            texto = f"Versão nova de {issue}: " + re.sub(rf"^{issue}\s+(pronta:?\s*)?", "", texto, flags=re.I)
        mesmo_dia = bool(ultimo) and ultimo.astimezone(datas.BRT).date() == quando.astimezone(datas.BRT).date()
        hora, dia = _contar(est, quando)
        motivo = ("madrugada" if madrugada(quando) else "dia" if dia >= TETO_DIA else "hora" if hora >= TETO_HORA
                  else "versão" if mesmo_dia and not pedido else None)
        if motivo:
            item = {"em": _iso(quando), "origem": origem, "issue": issue, "texto": texto, "arquivos": arquivos or [],
                    "motivo": motivo, "shas": [s for _, s in shas]}
            for ant in est["fila"]:  # a versão nova substitui o arquivo de mesmo nome; os outros anexos da issue ficam
                if (issue and ant.get("issue") == issue) or ant["texto"] == texto:
                    for rel, sha in zip(ant.get("arquivos") or [], ant.get("shas") or []):
                        if rel not in item["arquivos"]:
                            item["arquivos"].append(rel)
                            item["shas"].append(sha)
            est["fila"] = [f for f in est["fila"] if not (issue and f.get("issue") == issue) and f["texto"] != texto]
            est["fila"].append(item)
            est["envios"].append({"em": _iso(quando), "origem": origem, "issue": issue, "tipo": "fila", "destino": "fila"})
            _gravar(est, base_dados, quando)
            return None
        caminho = gravar(texto, arquivos)
        est["fila"] = [f for f in est["fila"] if not (issue and f.get("issue") == issue)]  # o que saiu agora vale mais que a fila
        _registrar(est, issue, shas, quando)
        est["envios"].append({"em": _iso(quando), "origem": origem, "issue": issue, "tipo": "enviado",
                              "issues_prontas": issues_prontas(texto)})
        _gravar(est, base_dados, quando)
        return caminho


# ---- resumo da fila ----

def _linha(item):
    primeira = (item["texto"].strip().splitlines() or [""])[0]
    primeira = re.sub(r"^(vers[ãa]o nova de )?KRN-\d+\s*(pronta)?:?\s*", "", primeira, flags=re.I)[:90]
    ident = item.get("issue")
    return f"• {ident}: {primeira} {LINEAR}{ident}" if ident else f"• {primeira}"


def _ordem(rel_base):
    rel, base = rel_base
    p = Path(rel)
    tam = 0
    with contextlib.suppress(OSError):
        tam = (Path(base) / rel).stat().st_size
    return (PRIORIDADE.get(p.suffix.lower(), 2 if p.suffix.lower() in IMAGENS else 9), -tam, p.name)


def montar_resumo(fila, base, quando):
    """(texto, [arquivos relativos ao DATA_DIR do bot]). Até 4 arquivos diretos (no máximo uma imagem), o resto nos links."""
    com_issue = [f for f in fila if issues_prontas(f.get("texto"))]
    cab = (f"{len(com_issue)} entregas prontas" if len(com_issue) == len(fila) else f"{len(fila)} avisos juntados")
    cab = cab.replace("1 entregas", "1 entrega")
    linhas = [_linha(f) for f in fila[:15]] + ([f"… e mais {len(fila) - 15}"] if len(fila) > 15 else [])
    todos, vistos = [], set()
    for f in fila:
        for rel in f.get("arquivos") or []:
            if rel not in vistos and (Path(base) / rel).is_file():
                vistos.add(rel)
                todos.append((rel, base))
    todos.sort(key=_ordem)
    soltos, resto, imagens = [], [], 0
    for rel, _ in todos:
        img = Path(rel).suffix.lower() in IMAGENS
        if len(soltos) < RESUMO_SOLTOS and not (img and imagens):
            soltos.append(rel)
            imagens += img
        else:
            resto.append(rel)
    arquivos = list(soltos)
    if resto:
        sem_link = {id(f) for f in fila if not f.get("issue")}
        nomes = [Path(r).name for r in resto if any(r in (f.get("arquivos") or []) for f in fila if id(f) in sem_link)]
        linhas.append(f"Os outros {len(resto)} arquivos estão nas issues dos links acima."
                      + (f" Sem issue, não enviados: {', '.join(nomes)}." if nomes else ""))
    return cab + ":\n" + "\n".join(linhas), arquivos


def descarregar(caixa=None, base_dados=None, quando=None, gravar=None):
    """Chamado a cada tick da agenda: com a fila cheia, fora da madrugada (a partir das 8h) e com cota, manda UM resumo.
    Devolve o texto enviado ou None."""
    quando = quando or datas.agora()
    caixa = Path(caixa) if caixa else None
    base = caixa.parent if caixa else Path(".")
    if quando.astimezone(datas.BRT).hour < RESUMO_AS or madrugada(quando):
        return None
    with _trava(base_dados):
        est = _ler(base_dados)
        if not est["fila"]:
            return None
        hora, dia = _contar(est, quando)
        if hora >= TETO_HORA or dia >= TETO_DIA:
            return None
        # A fila também obedece uma versão por issue/dia: o tick não pode
        # contornar a retenção feita em passar(), inclusive após o primeiro resumo.
        prontas, retidas = [], []
        for item in est["fila"]:
            ultimo = _ultimo_da_issue(est, item.get("issue")) if item.get("issue") else None
            mesmo_dia = ultimo and ultimo.astimezone(datas.BRT).date() == quando.astimezone(datas.BRT).date()
            (retidas if mesmo_dia else prontas).append(item)
        if not prontas:
            return None
        texto, arquivos = montar_resumo(prontas, base, quando)
        gravar(texto, arquivos)
        for f in prontas:
            _registrar(est, f.get("issue"), [("", s) for s in f.get("shas") or []], quando)
        est["fila"] = retidas
        est["envios"].append({"em": _iso(quando), "origem": "resumo", "issue": None, "tipo": "resumo",
                              "issues_prontas": [i for f in prontas for i in issues_prontas(f.get("texto"))]})
        _gravar(est, base_dados, quando)
        return texto


# ---- relatório ----

def relatorio(dias=7, base_dados=None, agora=None):
    """{dia BRT: {"total": n, "origens": {origem: n}, "desviados": n, "repetidos": n, "fila": n}}: só o que de fato saiu conta
    no total (a meta é menos de META_DIA por dia)."""
    agora = agora or datas.agora()
    corte = agora.astimezone(datas.BRT).date() - dt.timedelta(days=dias - 1)
    saida = {}
    for e in _ler(base_dados)["envios"]:
        dia = dt.datetime.fromisoformat(e["em"]).astimezone(datas.BRT).date()
        if dia < corte:
            continue
        d = saida.setdefault(dia.isoformat(), {"total": 0, "origens": {}, "desviados": 0, "repetidos": 0, "fila": 0})
        if e["tipo"] in ("enviado", "resumo"):
            d["total"] += 1
            d["origens"][e["origem"]] = d["origens"].get(e["origem"], 0) + 1
        elif e["tipo"] == "desviado":
            d["desviados"] += 1
        elif e["tipo"] == "repetido":
            d["repetidos"] += 1
        elif e["tipo"] == "fila":
            d["fila"] += 1
    return dict(sorted(saida.items()))


def texto_relatorio(dados):
    if not dados:
        return "WhatsApp do dono: nenhuma mensagem no período."
    linhas = ["WhatsApp do dono, mensagens por dia (meta: menos de %d):" % META_DIA]
    for dia, d in dados.items():
        origens = ", ".join(f"{o} {n}" for o, n in sorted(d["origens"].items(), key=lambda x: -x[1])) or "-"
        linhas.append(f"{dia}: {d['total']}{' ACIMA DA META' if d['total'] >= META_DIA else ''} ({origens}); "
                      f"desviadas ao orquestrador {d['desviados']}, repetidas barradas {d['repetidos']}, na fila {d['fila']}")
    return "\n".join(linhas)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.comum.porteiro", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["relatorio"])
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    dados = relatorio(a.dias)
    print(json.dumps(dados, ensure_ascii=False, indent=1) if a.json else texto_relatorio(dados))
    return 0


if __name__ == "__main__":
    sys.exit(main())
