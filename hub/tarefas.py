"""Contrato das tarefas do Claude da VPS: tarefas/<nome>.md, frontmatter simples + prompt.

    ---
    quando: 0 8 * * *          # cron de 5 campos em horário de Brasília (BRT), ou "manual"
    prioridade: 1              # 1 alta, 2 normal, 3 baixa
    modelo: haiku              # haiku | sonnet | opus | fable
    timeout: 15m               # 90s, 15m, 2h (número sozinho = minutos)
    repos: <org>/a, <org>/b    # opcional; também aceita [a, b]
    externo: false             # true = produz algo que sai para terceiros (só grava propostas)
    descricao: uma linha
    pauta: fsite05 fsite05-gmb     # opcional: <app> <tarefa>; lote de um app do Dokploy (docs/migracao-openrouter.md)
    avisar: true               # opcional (padrão false): manda a saída no WhatsApp. Só o resumo diário usa;
                               # o resto aparece no resumo das 8h (regra do dono: WhatsApp sem spam)
    skills: frontend-design, x  # opcional: skills da biblioteca ligadas nesta tarefa (hub/skills.py)
    conta: api               # opcional: roda só na reserva de API (hub/conta.py), nunca nas contas OAuth
    ---
    Corpo = prompt.

Parse sem dependência (não é YAML de verdade: uma chave por linha, `#` comenta). Chave desconhecida é erro.

    python -m hub.tarefas validar | listar
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from hub import config
from hub.comum.datas import BRT

PASTA = config.tarefas()
MODELOS = ("haiku", "sonnet", "opus", "fable")
CHAVES = ("quando", "prioridade", "modelo", "timeout", "repos", "externo", "descricao", "pauta", "avisar", "conta", "skills")
OBRIGATORIAS = ("quando", "prioridade", "modelo", "timeout", "externo", "descricao")
RE_NOME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")  # \Z: "a\n" não passa
RE_SKILL = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")
RE_TIMEOUT = re.compile(r"^(\d+)\s*(s|m|h)?$")
PESADA_MIN = 30  # tarefa com timeout acima disso é pesada: de dia, prioridade 2-3 espera a noite
DIA = (8, 22)    # 8h-22h BRT: pedidos do WhatsApp na frente das tarefas de prioridade 2-3


class TarefaInvalida(ValueError):
    pass


class Tarefa:
    def __init__(self, nome, quando, prioridade, modelo, timeout_s, repos, externo, descricao, prompt, arquivo=None,
                 pauta=None, avisar=False, conta=None, skills=None):
        self.skills = list(skills or [])  # nomes da biblioteca de skills
        self.conta = conta  # "api" = só a reserva de API pega (agenda.quer_api)
        self.avisar = avisar  # True só para quem o dono quer ler no WhatsApp (resumo diário)
        self.nome, self.quando, self.prioridade, self.modelo = nome, quando, prioridade, modelo
        self.timeout_s, self.repos, self.externo, self.descricao = timeout_s, repos, externo, descricao
        self.prompt, self.arquivo = prompt, arquivo
        self.pauta = pauta  # (app, tarefa) ou None: lote de um app (contrato pauta/entrega)

    @property
    def pesada(self):
        return self.timeout_s > PESADA_MIN * 60

    def __repr__(self):
        return f"Tarefa({self.nome!r}, {self.quando!r}, p{self.prioridade}, {self.modelo})"


def _valor(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def timeout_segundos(v):
    m = RE_TIMEOUT.match(str(v).strip().lower())
    if not m:
        raise TarefaInvalida(f"timeout inválido: {v!r} (use 90s, 15m ou 2h)")
    n = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, None: 60}[m.group(2)]
    if not 60 <= n <= 6 * 3600:
        raise TarefaInvalida(f"timeout fora de 1 min a 6 h: {v!r}")
    return n


def parse(texto, nome="tarefa"):
    """Texto de tarefas/<nome>.md → Tarefa. Erro diz o campo e o arquivo."""
    origem = f"{nome}.md"
    if not RE_NOME.match(nome):
        raise TarefaInvalida(f"{origem}: nome do arquivo só com minúsculas, números e hífen")
    linhas = texto.replace("\r\n", "\n").split("\n")
    if not linhas or linhas[0].strip() != "---":
        raise TarefaInvalida(f"{origem}: falta o frontmatter (primeira linha '---')")
    try:
        fim = next(i for i in range(1, len(linhas)) if linhas[i].strip() == "---")
    except StopIteration:
        raise TarefaInvalida(f"{origem}: frontmatter sem o '---' de fechamento") from None
    campos, erros = {}, []
    for n, linha in enumerate(linhas[1:fim], 2):
        if not linha.strip() or linha.lstrip().startswith("#"):
            continue
        if ":" not in linha:
            erros.append(f"{origem}:{n}: linha sem 'chave: valor'")
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip()
        if chave not in CHAVES:
            erros.append(f"{origem}:{n}: chave desconhecida {chave!r} (válidas: {', '.join(CHAVES)})")
        elif chave in campos:
            erros.append(f"{origem}:{n}: chave {chave!r} repetida")
        else:
            # comentário no fim da linha, menos na descrição (pode ter #)
            campos[chave] = _valor(valor if chave == "descricao" else re.sub(r"\s+#.*$", "", valor))
    for chave in OBRIGATORIAS:
        if chave not in campos:
            erros.append(f"{origem}: falta '{chave}'")
    if erros:
        raise TarefaInvalida("\n".join(erros))
    quando = campos["quando"]
    if quando != "manual":
        try:
            Cron(quando)
        except ValueError as e:
            erros.append(f"{origem}: 'quando' inválido ({e})")
    try:
        prioridade = int(campos["prioridade"])
        if prioridade not in (1, 2, 3):
            raise ValueError
    except ValueError:
        erros.append(f"{origem}: 'prioridade' tem que ser 1, 2 ou 3")
        prioridade = None
    if campos["modelo"] not in MODELOS:
        erros.append(f"{origem}: 'modelo' tem que ser um de {', '.join(MODELOS)}")
    try:
        timeout_s = timeout_segundos(campos["timeout"])
    except TarefaInvalida as e:
        erros.append(f"{origem}: {e}")
        timeout_s = None
    if campos["externo"] not in ("true", "false"):
        erros.append(f"{origem}: 'externo' tem que ser true ou false")
    repos_txt = campos.get("repos", "").strip().strip("[]")
    repos = [_valor(r) for r in repos_txt.split(",") if r.strip()]
    for r in repos:
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", r):
            erros.append(f"{origem}: repo {r!r} não é dono/nome")
    if not campos["descricao"]:
        erros.append(f"{origem}: 'descricao' vazia")
    pauta = None
    if campos.get("pauta"):
        partes = campos["pauta"].split()
        if len(partes) != 2 or not all(RE_NOME.match(x) for x in partes):
            erros.append(f"{origem}: 'pauta' tem que ser '<app> <tarefa>' (minúsculas, números e hífen)")
        else:
            pauta = tuple(partes)
    if campos.get("conta") not in (None, "api", "codex"):
        erros.append(f"{origem}: 'conta' só aceita api ou codex")
    skills = [_valor(x) for x in campos.get("skills", "").strip().strip("[]").split(",") if x.strip()]
    for x in skills:
        if not RE_SKILL.match(x):
            erros.append(f"{origem}: skill {x!r} inválida (minúsculas, números, hífen, _ e :)")
    prompt = "\n".join(linhas[fim + 1:]).strip()
    if not prompt:
        erros.append(f"{origem}: corpo (prompt) vazio")
    if erros:
        raise TarefaInvalida("\n".join(erros))
    return Tarefa(nome, quando, prioridade, campos["modelo"], timeout_s, repos, campos["externo"] == "true",
                  campos["descricao"], prompt, pauta=pauta, avisar=campos.get("avisar") == "true",
                  conta=campos.get("conta"), skills=skills)


def validar(texto, nome="tarefa"):
    """Valida o texto de uma tarefa (o mesmo contrato do arquivo) e devolve a Tarefa; TarefaInvalida com uma linha de
    erro por problema, em português. Usada pela tela Rotinas antes de gravar."""
    return parse(texto, nome)


def _ler_pasta(pasta):
    """{nome: (Tarefa|None, erro|None, arquivo)} dos *.md da pasta (README.md fora). Nome fora de RE_NOME entra com erro."""
    saida = {}
    if not pasta.is_dir():
        return saida
    for arq in sorted(pasta.glob("*.md")):
        if arq.name.lower() == "readme.md":
            continue
        try:
            t = parse(arq.read_text(encoding="utf-8"), arq.stem)
            t.arquivo = arq
            saida[arq.stem] = (t, None, arq)
        except (TarefaInvalida, OSError, UnicodeDecodeError) as e:
            saida[arq.stem] = (None, str(e), arq)
    return saida


def listar(pasta=None, extra=None):
    """[{nome, origem, tarefa, erro, arquivo}] das tarefas, em ordem de nome. A pasta `extra` (camada do painel,
    `config.tarefas_extra()`) vale por cima da base: origem "repo" (só na base), "painel" (só na extra) ou "sobrescrita"
    (nos dois; vale a do painel). `tarefa` é a que a agenda usa; `erro` é o do arquivo que deveria valer (se o do painel
    está inválido e o da base não, `tarefa` é a da base e `erro` o do painel)."""
    base = _ler_pasta(Path(pasta or PASTA))
    camada = _ler_pasta(Path(extra)) if extra and Path(extra).resolve() != Path(pasta or PASTA).resolve() else {}
    saida = []
    for nome in sorted(set(base) | set(camada)):
        b, c = base.get(nome), camada.get(nome)
        origem = "sobrescrita" if b and c else ("painel" if c else "repo")
        t, erro, arq = c if c else b
        if c and t is None and b:
            t = b[0]
        saida.append({"nome": nome, "origem": origem, "tarefa": t, "erro": erro, "arquivo": arq})
    return saida


def carregar(pasta=None, extra=None):
    """{nome: Tarefa} de tarefas/*.md (README.md fica de fora). Sem argumentos lê a pasta de tarefas e, por cima, a do
    painel (`config.tarefas_extra()`; sem ela, como antes). Erro em arquivo da base derruba a carga; arquivo inválido da
    camada do painel é ignorado com aviso no stderr (a agenda segue com a versão da base) e a tela Rotinas o mostra."""
    if pasta is None and extra is None:
        extra = config.tarefas_extra()
    tarefas, erros = {}, []
    base = _ler_pasta(Path(pasta or PASTA))
    for nome, (t, erro, _) in base.items():
        if t is None:
            erros.append(erro)
        else:
            tarefas[nome] = t
    if erros:
        raise TarefaInvalida("tarefas com erro:\n- " + "\n- ".join(erros))
    if extra and Path(extra).resolve() != Path(pasta or PASTA).resolve():
        for nome, (t, erro, _) in _ler_pasta(Path(extra)).items():
            if t is None:
                print(f"tarefas: cópia do painel ignorada ({erro.splitlines()[0]})", file=sys.stderr)
            else:
                tarefas[nome] = t
    return tarefas


# ---- cron em BRT ----

_LIMITES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _campo(txt, lo, hi):
    vals = set()
    for parte in txt.split(","):
        passo = 1
        if "/" in parte:
            parte, p = parte.split("/", 1)
            if not p.isdigit() or int(p) < 1:
                raise ValueError(f"passo inválido em {txt!r}")
            passo = int(p)
        if parte == "*":
            a, b = lo, hi
        elif "-" in parte:
            a, b = parte.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"faixa inválida em {txt!r}")
            a, b = int(a), int(b)
        elif parte.isdigit():
            a = b = int(parte)
            if passo > 1:
                b = hi
        else:
            raise ValueError(f"valor inválido {parte!r}")
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{txt!r} fora de {lo}-{hi}")
        vals.update(range(a, b + 1, passo))
    return vals


class Cron:
    """Cron de 5 campos (min hora dia mês semana), avaliado em BRT. Dia e semana restritos: vale um OU outro."""

    def __init__(self, expr):
        partes = expr.split()
        if len(partes) != 5:
            raise ValueError("cron precisa de 5 campos: min hora dia mês dia-da-semana")
        self.min, self.hora, self.dia, self.mes, sem = (_campo(p, *lim) for p, lim in zip(partes, _LIMITES))
        self.sem = {d % 7 for d in sem}  # 0 e 7 = domingo
        self.dia_livre, self.sem_livre = partes[2] == "*", partes[4] == "*"

    def bate(self, quando):
        t = quando.astimezone(BRT)
        if t.minute not in self.min or t.hour not in self.hora or t.month not in self.mes:
            return False
        dia_ok, sem_ok = t.day in self.dia, (t.isoweekday() % 7) in self.sem
        if self.dia_livre or self.sem_livre:
            return dia_ok and sem_ok
        return dia_ok or sem_ok

    def entre(self, desde, ate):
        """Primeiro minuto em (desde, ate] em que o cron bate, ou None."""
        t = desde.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        while t <= ate:
            if self.bate(t):
                return t
            t += dt.timedelta(minutes=1)
        return None


def de_dia(agora):
    return DIA[0] <= agora.astimezone(BRT).hour < DIA[1]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.tarefas", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("validar", "listar"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        tarefas = carregar()
    except TarefaInvalida as e:
        print(e, file=sys.stderr)
        return 1
    if args.cmd == "validar":
        print(f"ok: {len(tarefas)} tarefas")
    else:
        for t in tarefas.values():
            print(f"{t.nome:<22} {t.quando:<16} p{t.prioridade} {t.modelo:<6} {t.timeout_s // 60:>4}m"
                  f"{' externo' if t.externo else ''}  {t.descricao}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
