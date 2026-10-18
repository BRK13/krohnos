"""Planos com itens atômicos e aceite executável: o jeito do jev-plano, sem API de terceiro.

Item de plano (ou de issue criada por `hub.linear criar`), em Markdown:

    - [ ] **I1** Título curto
      - check: `python3 -m unittest tests.test_x`     (obrigatório para ir ao Haiku; pode repetir)
      - files: hub/x.py, tests/test_x.py              (o que o item pode tocar)
      - after: I0                                     (dependência; vários separados por vírgula)
      - exec: haiku|sonnet|opus                       (opcional: sem ele, a regra escolhe)
      - tipo: teste|docs|lint|codigo|...              (opcional: agrupa a taxa de retrabalho)

Fluxo: `ondas` agrupa o que não divide arquivo; o Haiku faz o item mecânico e grava o relatório curto; um Sonnet em
esforço alto revisa o lote (APROVADO, AJUSTADO até ~20 linhas, ou REFAZER, que volta para o Sonnet); o portão de Done
exige a saída de cada `check:` da issue; `registrar` alimenta a taxa de retrabalho que o Pereiraos mostra.

    python3 -m hub.plano_itens validar <plano.md>
    python3 -m hub.plano_itens ondas <plano.md>               # ondas + o modelo de cada item
    python3 -m hub.plano_itens executor <plano.md> <id>       # prompt do executor, restrito aos `files:`
    python3 -m hub.plano_itens revisor <plano.md> <id>...     # prompt do revisor do lote
    python3 -m hub.plano_itens conferir <plano.md|-> [--cwd D] # roda os `check:` e imprime o bloco de prova do Done
    python3 -m hub.plano_itens registrar <plano> <id> --executor haiku --veredito APROVADO [--custo 0.02]
    python3 -m hub.plano_itens taxa [--plano P]               # retrabalho e custo por tipo e executor
"""
import argparse
import contextlib
import dataclasses
import fcntl
import json
import re
import subprocess
import sys

from hub import conta

MODELOS = ("haiku", "sonnet", "opus")
VEREDITOS = ("APROVADO", "AJUSTADO", "REFAZER")
MAX_ARQUIVOS_HAIKU = 3
MAX_LINHAS_AJUSTE = 20
LIMITE_RETRABALHO = 0.30  # acima disso: fragmentar mais ou subir o executor daquele tipo de item
AMOSTRA_MIN = 5  # menos itens que isso num grupo: a taxa ainda não decide nada
TIMEOUT_CHECK = 300
SAIDA_MAX = 600

# Item que nunca vai para o Haiku: segurança, migração, arquitetura, segredo, permissão.
RE_SENSIVEL = re.compile(r"seguran|segredo|secret|senha|token|credencial|permiss|\bauth|login|cripto|migra[cç]|migration|"
                         r"schema|arquitetur|\.env\b|dokploy|traefik|deploy|produ[cç][aã]o|\bpagamento|cobran[cç]a", re.I)
# `check:` que a VPS não roda (CPU dividida por até 12 sessões; destrutivo; sai para fora).
RE_PROIBIDO = re.compile(r"\bnext\s+build\b|\bci-local\b|\bsudo\b|\brm\b[^;&|\n]*\s(?:-[a-z]*r|--recursive)|\bgit\s+push\b|"
                         r"\bmkfs\b|\bshutdown\b|\breboot\b|\bdd\s+if=|\|\s*(?:ba)?sh\b|\byarn\s+build\b|\bnpm\s+run\s+build\b", re.I)

RE_CABECA = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?\*\*(?P<id>[A-Za-z][\w.-]*)\*\*[:.\s-]*(?P<titulo>.*)$")
RE_CAMPO = re.compile(r"^\s*(?:[-*]\s+)?(?P<k>check|files|after|exec|tipo):\s*(?P<v>.+?)\s*$", re.I)
RE_CHECK_SOLTO = re.compile(r"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?)?check:\s*(?P<v>.+?)\s*$", re.I)


@dataclasses.dataclass
class Item:
    id: str
    titulo: str = ""
    checks: list = dataclasses.field(default_factory=list)
    files: list = dataclasses.field(default_factory=list)
    after: list = dataclasses.field(default_factory=list)
    exec: str = ""
    tipo: str = "geral"

    def para_dict(self):
        return dataclasses.asdict(self)


def _cmd(v):
    """`comando` entre crases, ou a linha inteira."""
    m = re.fullmatch(r"`+(.+?)`+\s*", v.strip())
    return (m.group(1) if m else v).strip()


def _lista(v):
    return [x.strip().strip("`") for x in re.split(r"[,;]", v) if x.strip().strip("`")]


def parse(texto):
    """Itens do texto, na ordem. Linhas de campo antes de qualquer cabeça de item não contam."""
    itens, atual, em_codigo = [], None, False
    for linha in (texto or "").splitlines():
        if linha.strip().startswith("```"):
            em_codigo = not em_codigo
            continue
        if em_codigo:
            continue
        if m := RE_CABECA.match(linha):
            atual = Item(m["id"], m["titulo"].strip())
            itens.append(atual)
        elif (m := RE_CAMPO.match(linha)) and atual:
            k, v = m["k"].lower(), m["v"]
            if k == "check":
                atual.checks.append(_cmd(v))
            elif k == "files":
                atual.files += _lista(v)
            elif k == "after":
                atual.after += _lista(v)
            elif k == "exec":
                atual.exec = v.strip().strip("`").lower()
            else:
                atual.tipo = v.strip().strip("`").lower() or "geral"
        elif linha.strip() and not linha.startswith((" ", "\t")):
            atual = None  # texto solto fecha o item
    return itens


def checks_da_issue(descricao):
    """Todo `check: <comando>` da descrição (itens e lista "Pronto quando"), sem repetir, na ordem. Linhas de código
    cercado e `check:` entre crases no meio da prosa não contam."""
    vistos, out, em_codigo = set(), [], False
    for linha in (descricao or "").splitlines():
        if linha.strip().startswith("```"):
            em_codigo = not em_codigo
        elif not em_codigo and (m := RE_CHECK_SOLTO.match(linha)):
            c = _cmd(m["v"])
            if c and c not in vistos:
                vistos.add(c)
                out.append(c)
    return out


# ---- validação, roteamento e ondas ----

def validar(itens):
    """Lista de erros (vazia = plano bom). `exec: haiku` sem `check:` é erro; os demais avisos saem em `avisos`."""
    erros, ids = [], {}
    for i in itens:
        if i.id in ids:
            erros.append(f"{i.id}: id repetido")
        ids[i.id] = i
        if i.exec and i.exec not in MODELOS:
            erros.append(f"{i.id}: exec {i.exec!r} não é haiku, sonnet nem opus")
        if i.exec == "haiku" and not i.checks:
            erros.append(f"{i.id}: sem check: não vai para o Haiku")
        for c in i.checks:
            if RE_PROIBIDO.search(c):
                erros.append(f"{i.id}: check proibido na VPS ({c[:60]})")
    for i in itens:
        for d in i.after:
            if d not in ids:
                erros.append(f"{i.id}: after {d} não existe")
            elif d == i.id:
                erros.append(f"{i.id}: depende dele mesmo")
    if not erros and _ciclo(itens):
        erros.append(f"ciclo em after: {', '.join(_ciclo(itens))}")
    return erros


def avisos(itens):
    """Itens que vão para o Sonnet por falta de check (o plano continua válido)."""
    return [f"{i.id}: sem check:, vai para o Sonnet" for i in itens if not i.checks]


def _ciclo(itens):
    pend = {i.id: set(i.after) for i in itens}
    while True:
        livres = [k for k, v in pend.items() if not v]
        if not livres:
            return sorted(pend)
        for k in livres:
            pend.pop(k)
        for v in pend.values():
            v.difference_update(livres)
        if not pend:
            return []


def rotear(item):
    """(modelo, motivo). Haiku só com check:, 1 a 3 arquivos declarados e nada de segurança, migração ou arquitetura.
    O `exec:` do plano manda para cima (sonnet/opus); `haiku` pedido sem cumprir a regra sobe para sonnet."""
    sensivel = RE_SENSIVEL.search(f"{item.titulo} {' '.join(item.files)} {item.tipo}")
    if item.exec in ("sonnet", "opus"):
        return item.exec, "exec do plano"
    motivo_nao = ""
    if not item.checks:
        motivo_nao = "sem check:"
    elif not item.files:
        motivo_nao = "sem files:"
    elif len(item.files) > MAX_ARQUIVOS_HAIKU:
        motivo_nao = f"mais de {MAX_ARQUIVOS_HAIKU} arquivos"
    elif sensivel:
        motivo_nao = f"sensível ({sensivel.group(0).lower()})"
    if motivo_nao:
        return "sonnet", motivo_nao
    return "haiku", "exec do plano" if item.exec == "haiku" else "mecânico com check"


def ondas(itens):
    """[[Item]]: cada onda só tem itens com dependências já feitas e sem arquivo em comum. Item sem `files:` roda sozinho
    (não dá para provar que não conflita)."""
    feitos, restante, saida = set(), list(itens), []
    while restante:
        onda, usados = [], set()
        for i in restante:
            arqs = set(i.files)
            if not set(i.after) <= feitos:
                continue
            if not arqs:
                if not onda:
                    onda.append(i)
                    break
                continue
            if arqs & usados:
                continue
            onda.append(i)
            usados |= arqs
        if not onda:
            raise ValueError(f"itens sem saída (ciclo ou dependência faltando): {[i.id for i in restante]}")
        saida.append(onda)
        feitos |= {i.id for i in onda}
        restante = [i for i in restante if i.id not in feitos]
    return saida


# ---- prompts ----

def prompt_executor(item, relatorio="relatorio.md"):
    arqs = ", ".join(item.files) or "(nenhum declarado)"
    checks = "\n".join(f"  - `{c}`" for c in item.checks)
    return (f"Você executa UM item mecânico de um plano. Item {item.id}: {item.titulo}\n\n"
            f"Pode tocar SOMENTE nestes arquivos: {arqs}. Se precisar de outro, pare e diga no relatório.\n"
            f"Não decida arquitetura, não refatore fora do item, não mexa em segredo, migração nem deploy.\n\n"
            f"Aceite (rode ao terminar e copie a saída real):\n{checks}\n\n"
            f"Grave `{relatorio}` curto, neste formato:\n"
            f"- Arquivos: <os que mudou>\n- O que fez: <2 a 4 linhas>\n"
            f"- check: `<comando>` → PASSOU|FALHOU\n  ```\n  <saída real, últimas linhas>\n  ```\n"
            f"- O que NÃO fez: <ou \"nada\">\n\nNunca escreva PASSOU sem ter rodado o comando.")


def prompt_revisor(itens):
    lista = "\n".join(f"- {i.id}: {i.titulo} (files: {', '.join(i.files) or '-'}; check: {'; '.join(i.checks) or '-'})" for i in itens)
    return (f"Você revisa o lote de itens que o Haiku executou, em esforço alto, numa sessão só para isso.\n{lista}\n\n"
            f"Para cada item: leia o relatório e o diff, rode o `check:` você mesmo e confira que só os `files:` mudaram. "
            f"Veredito por item, uma linha `<id>: APROVADO|AJUSTADO|REFAZER — motivo`:\n"
            f"- APROVADO: certo e o check passa.\n"
            f"- AJUSTADO: corrija você mesmo (até ~{MAX_LINHAS_AJUSTE} linhas, sem refazer do zero) e rode o check de novo.\n"
            f"- REFAZER: errado ou grande demais; o item volta para o Sonnet fazer (não é o Haiku de novo).\n"
            f"Depois de cada veredito: `python3 -m hub.plano_itens registrar <plano> <id> --executor haiku --veredito <V> --tipo <tipo>`.")


# ---- relatório do executor ----

RE_CHECK_REL = re.compile(r"check:\s*`(?P<cmd>[^`]+)`\s*[→>-]+\s*(?P<res>PASSOU|FALHOU)(?P<resto>[^\n]*\n\s*```[^\n]*\n(?P<saida>.*?)```)?", re.I | re.S)
RE_VEREDITO = re.compile(r"^\s*[-*]?\s*(?P<id>[\w.-]+):\s*\**(?P<v>APROVADO|AJUSTADO|REFAZER)\b", re.I | re.M)


def ler_relatorio(texto):
    """{cmd: 'PASSOU'|'FALHOU'|'SEM_SAIDA'} do relatório do executor (última ocorrência de cada comando vale).
    PASSOU sem o bloco de saída logo abaixo vira SEM_SAIDA: aceite sem prova não vale."""
    out = {}
    for m in RE_CHECK_REL.finditer(texto or ""):
        res = m["res"].upper()
        if res == "PASSOU" and not (m["saida"] or "").strip():
            res = "SEM_SAIDA"
        out[m["cmd"].strip()] = res
    return out


def ler_vereditos(texto):
    return {m["id"]: m["v"].upper() for m in RE_VEREDITO.finditer(texto or "")}


# ---- rodar os checks ----

def rodar_check(cmd, cwd=None, timeout=TIMEOUT_CHECK, run=None):
    """{cmd, res, saida}: roda por `bash -c` (cwd = worktree). Proibido na VPS ou estourou o tempo = FALHOU com o motivo."""
    if RE_PROIBIDO.search(cmd):
        return {"cmd": cmd, "res": "FALHOU", "saida": "comando proibido na VPS (build/suíte/sudo/rm -rf/push): é do CI"}
    try:
        p = (run or subprocess.run)(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "res": "FALHOU", "saida": f"estourou {timeout}s"}
    except OSError as e:
        return {"cmd": cmd, "res": "FALHOU", "saida": f"não rodou: {e}"}
    saida = ((p.stdout or "") + (p.stderr or "")).strip()
    return {"cmd": cmd, "res": "PASSOU" if p.returncode == 0 else "FALHOU", "saida": saida[-SAIDA_MAX:]}


def bloco_prova(resultados):
    """O texto que o comentário final leva (e que o portão de Done procura)."""
    out = []
    for r in resultados:
        out.append(f"- check: `{r['cmd']}` → {r['res']}\n  ```\n  " + (r["saida"] or "(sem saída)").replace("\n", "\n  ") + "\n  ```")
    return "\n".join(out)


# ---- portão de Done ----

def _norm(s):
    return " ".join((s or "").split())


def faltas_checks(descricao, textos):
    """Faltas do portão: cada `check:` da descrição precisa constar PASSOU num dos `textos` (o comentário final
    e/ou os comentários do Claude). FALHOU como última palavra, ou nenhuma saída, é falta. Sem `check:` na issue: nada."""
    checks = checks_da_issue(descricao)
    if not checks:
        return []
    provas = {}
    for t in textos:
        for cmd, res in ler_relatorio(t).items():
            provas[_norm(cmd)] = res  # a ordem dos textos é a do tempo: o último vale
    faltas = []
    for c in checks:
        res = provas.get(_norm(c))
        if res is None:
            faltas.append(f"o check `{c[:80]}` não tem saída no comentário final (rode `python3 -m hub.plano_itens conferir` e cole o bloco)")
        elif res == "SEM_SAIDA":
            faltas.append(f"o check `{c[:80]}` diz PASSOU sem a saída real embaixo (bloco ``` do `conferir`)")
        elif res != "PASSOU":
            faltas.append(f"o check `{c[:80]}` está FALHOU")
    return faltas


# ---- ledger e taxa de retrabalho (Pereiraos) ----

def _arq(base_dados=None):
    return conta.pasta(base_dados) / "plano-itens.jsonl"


def registrar(plano, item_id, executor, veredito, tipo="geral", custo_usd=None, base_dados=None, agora=None):
    """Uma linha por item revisado. Veredito != APROVADO conta como retrabalho."""
    executor, veredito = executor.lower(), veredito.upper()
    if executor not in MODELOS or veredito not in VEREDITOS:
        raise ValueError(f"executor {MODELOS} e veredito {VEREDITOS}")
    reg = {"em": conta.iso(agora or conta.agora()), "plano": plano, "item": item_id, "executor": executor, "veredito": veredito,
           "tipo": tipo or "geral", "custo_usd": custo_usd}
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    return reg


def registros(base_dados=None, plano=None):
    out = []
    with contextlib.suppress(OSError), open(_arq(base_dados), encoding="utf-8") as f:
        for linha in f:
            with contextlib.suppress(ValueError):
                r = json.loads(linha)
                if plano is None or r.get("plano") == plano:
                    out.append(r)
    return out


def taxa(regs):
    """{(tipo, executor): {n, retrabalho, refazer, taxa, custo_medio, alarme}}; retrabalho = AJUSTADO ou REFAZER."""
    grupos = {}
    for r in regs:
        g = grupos.setdefault((r["tipo"], r["executor"]), {"n": 0, "retrabalho": 0, "refazer": 0, "custos": []})
        g["n"] += 1
        g["retrabalho"] += r["veredito"] != "APROVADO"
        g["refazer"] += r["veredito"] == "REFAZER"
        if r.get("custo_usd") is not None:
            g["custos"].append(float(r["custo_usd"]))
    for g in grupos.values():
        g["taxa"] = g["retrabalho"] / g["n"]
        g["custo_medio"] = sum(g["custos"]) / len(g["custos"]) if g["custos"] else None
        g["alarme"] = g["n"] >= AMOSTRA_MIN and g["taxa"] > LIMITE_RETRABALHO
        del g["custos"]
    return grupos


def comparar(regs):
    """Haiku contra Sonnet nos tipos que têm os dois: {tipo: {economia_pct, taxa_haiku, taxa_sonnet, veredito}}.
    Veredito: `padrão` (economiza e o retrabalho do Haiku fica no limite de 30%),
    `fragmentar ou subir` (retrabalho acima do limite), `sem custo`/`amostra pequena` quando ainda não decide."""
    t, out = taxa(regs), {}
    for tipo in {k[0] for k in t}:
        h, s = t.get((tipo, "haiku")), t.get((tipo, "sonnet"))
        if not h or not s:
            continue
        ch, cs = h["custo_medio"], s["custo_medio"]
        eco = (1 - ch / cs) * 100 if ch is not None and cs else None
        if min(h["n"], s["n"]) < AMOSTRA_MIN:
            v = "amostra pequena"
        elif h["taxa"] > LIMITE_RETRABALHO:
            v = "fragmentar ou subir"
        elif eco is None:
            v = "sem custo"
        elif eco > 0:
            v = "padrão"
        else:
            v = "não compensa"
        out[tipo] = {"economia_pct": eco, "taxa_haiku": h["taxa"], "taxa_sonnet": s["taxa"], "veredito": v}
    return out


def card_html(base_dados=None):
    """Cartão do Pereiraos (config/agentes): retrabalho e custo por tipo de item e executor."""
    from html import escape as e

    from hub import ui
    regs = registros(base_dados)
    if not regs:
        return ui.card("Planos com check (KRN-1070)", '<p class="mu">Nenhum item revisado ainda. O piloto são os 3 próximos planos com 4 ou mais itens.</p>')
    linhas = []
    for (tipo, ex), g in sorted(taxa(regs).items()):
        custo = f"US$ {g['custo_medio']:.3f}" if g["custo_medio"] is not None else "-"
        selo = ui.selo("acima de 30%", "ruim") if g["alarme"] else ui.selo("ok", "ok")
        linhas.append([e(tipo), e(ex), str(g["n"]), f"{g['taxa']:.0%}", str(g["refazer"]), custo, selo])
    return ui.card("Planos com check (KRN-1070)", ui.tabela(["Tipo", "Executor", "Itens", "Retrabalho", "Refazer", "Custo médio", ""], linhas))


# ---- CLI ----

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for nome in ("validar", "ondas"):
        sub.add_parser(nome).add_argument("plano")
    e = sub.add_parser("executor")
    e.add_argument("plano")
    e.add_argument("id")
    r = sub.add_parser("revisor")
    r.add_argument("plano")
    r.add_argument("ids", nargs="+")
    c = sub.add_parser("conferir")
    c.add_argument("plano")
    c.add_argument("--cwd")
    g = sub.add_parser("registrar")
    g.add_argument("plano")
    g.add_argument("item")
    g.add_argument("--executor", required=True)
    g.add_argument("--veredito", required=True)
    g.add_argument("--tipo", default="geral")
    g.add_argument("--custo", type=float)
    t = sub.add_parser("taxa")
    t.add_argument("--plano")
    a = p.parse_args(argv)

    if a.cmd == "taxa":
        regs = registros(plano=a.plano)
        for (tipo, ex), x in sorted(taxa(regs).items()):
            custo = f"US$ {x['custo_medio']:.3f}" if x["custo_medio"] is not None else "-"
            print(f"{tipo:12} {ex:7} n={x['n']:3} retrabalho={x['taxa']:.0%} refazer={x['refazer']} custo={custo}" + ("  ALARME >30%" if x["alarme"] else ""))
        for tipo, x in sorted(comparar(regs).items()):
            eco = f"{x['economia_pct']:.0f}%" if x["economia_pct"] is not None else "-"
            print(f"haiku x sonnet em {tipo}: economia {eco}, retrabalho {x['taxa_haiku']:.0%} x {x['taxa_sonnet']:.0%} → {x['veredito']}")
        return 0
    if a.cmd == "registrar":
        print(json.dumps(registrar(a.plano, a.item, a.executor, a.veredito, a.tipo, a.custo), ensure_ascii=False))
        return 0
    texto = sys.stdin.read() if a.plano == "-" else open(a.plano, encoding="utf-8").read()
    itens = parse(texto)
    if not itens and a.cmd != "conferir":
        sys.exit("nenhum item no formato `- [ ] **I1** título` com campos check:/files:/after:/exec:")
    if a.cmd == "conferir":
        resultados = [rodar_check(cmd, a.cwd) for cmd in checks_da_issue(texto)]
        print(bloco_prova(resultados))
        return int(any(x["res"] != "PASSOU" for x in resultados))
    erros = validar(itens)
    for er in erros:
        print(f"ERRO {er}", file=sys.stderr)
    if erros:
        return 1
    if a.cmd == "validar":
        for av in avisos(itens):
            print(f"aviso: {av}")
        print(f"{len(itens)} item(ns) ok")
    elif a.cmd == "ondas":
        for n, onda in enumerate(ondas(itens), 1):
            print(f"onda {n}: " + ", ".join(f"{i.id}[{rotear(i)[0]}: {rotear(i)[1]}]" for i in onda))
    elif a.cmd == "executor":
        item = next((i for i in itens if i.id == a.id), None) or sys.exit(f"item {a.id} não existe")
        if rotear(item)[0] != "haiku":
            sys.exit(f"{a.id} não vai para o Haiku ({rotear(item)[1]})")
        print(prompt_executor(item))
    else:
        lote = [i for i in itens if i.id in a.ids]
        print(prompt_revisor(lote))
    return 0


if __name__ == "__main__":
    sys.exit(main())
