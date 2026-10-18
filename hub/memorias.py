"""Conferência da memória única (F1 da faxina).

    python3 -m hub.memorias checar [pasta]     # padrão: ~/memoria
    python3 -m hub.memorias checar --diff [arquivo|-] [--repo NOME] [--base origin/main] # o diff respeita as decisões?

Reprova (código 1, uma linha por problema): ponteiro de MEMORY.md para arquivo que não existe, arquivo sem ponteiro,
índice maior que o limite, `indice-<projeto>.md` sem ponteiro no MEMORY.md, arquivo mesclado sem `fontes:` e decisão do dono (memorias_decisoes.json) que sumiu da pasta.
O MEMORY.md (máx. 80 linhas) pode apontar índices por projeto (`indice-<projeto>.md`, um nível): os ponteiros deles valem como os do MEMORY.md.
`arquivo/` e as demais subpastas ficam de fora: a memória é plana. Plano: docs/plans/feitos/2026-10-16-faxina-f1-memoria-unica.md.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hub import gh, github_api

PADRAO = Path.home() / "memoria"
MAX_LINHAS, MAX_BYTES = 80, 25_000  # MEMORY.md: o harness o lê inteiro em toda sessão (enxuto, 80 linhas)
MAX_LINHAS_INDICE = 200  # cada indice-<projeto>.md (lido sob demanda)
# Gabarito (memorias_decisoes.json) do anexo conhecimento §1.2 (mesma ordem). Uma decisão vale se alguma LINHA da pasta tiver, juntas, a âncora,
# a data e a issue (quando há; data e issue como palavra inteira: não casa com). Sem uma delas, a mescla perdeu a decisão.
_GABARITO = Path(__file__).with_name("memorias_decisoes.json")
# Sem o gabarito (snapshot público, as decisões são do dono e ficam fora) não há decisão a conferir.
REGRAS = json.loads(_GABARITO.read_text(encoding="utf-8")) if _GABARITO.is_file() else []
DECISOES = [d for d in REGRAS if d.get("ancora")]  # as 24 do anexo; regra só de diff (sem âncora) não precisa estar na memória
_LINK = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)\s]*)?\)")
_E_INDICE = re.compile(r"indice-[\w-]+\.md\Z")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _arquivos(raiz):
    """Memórias (os índices MEMORY.md e indice-*.md não contam)."""
    return sorted(p for p in raiz.glob("*.md") if p.is_file() and p.name != "MEMORY.md" and not _E_INDICE.match(p.name))


def _indice_de(arq):
    texto = arq.read_text(encoding="utf-8")
    return texto, [m.group(1) for m in _LINK.finditer(texto)]


def _indice(raiz):
    return _indice_de(raiz / "MEMORY.md")


def problemas(raiz):
    raiz = Path(raiz)
    if not (raiz / "MEMORY.md").is_file():
        return [f"{raiz}: sem MEMORY.md"]
    achados = []
    indice, alvos = _indice(raiz)
    if indice.count("\n") + 1 > MAX_LINHAS or len(indice.encode()) > MAX_BYTES:
        achados.append(f"MEMORY.md grande demais (limite {MAX_LINHAS} linhas / {MAX_BYTES} bytes)")
    for alvo in alvos:
        if not (raiz / alvo).is_file():
            achados.append(f"ponteiro quebrado: {alvo}")
    apontados = {os.path.normpath(a) for a in alvos}
    for ind in sorted(raiz.glob("indice-*.md")):
        if not _E_INDICE.match(ind.name):
            continue
        if ind.name not in apontados:
            achados.append(f"índice sem ponteiro no MEMORY.md: {ind.name}")
            continue
        texto, filhos = _indice_de(ind)
        if texto.count("\n") + 1 > MAX_LINHAS_INDICE or len(texto.encode()) > MAX_BYTES:
            achados.append(f"{ind.name} grande demais (limite {MAX_LINHAS_INDICE} linhas / {MAX_BYTES} bytes)")
        for alvo in filhos:
            if not (raiz / alvo).is_file():
                achados.append(f"ponteiro quebrado: {alvo} (em {ind.name})")
        apontados |= {os.path.normpath(a) for a in filhos}
    linhas_texto = []
    for p in _arquivos(raiz):
        if p.name not in apontados:
            achados.append(f"sem ponteiro no MEMORY.md: {p.name}")
        corpo = p.read_text(encoding="utf-8")
        linhas_texto += corpo.lower().splitlines()
        front = _FRONT.match(corpo)
        if front and re.search(r"^mesclada:\s*(sim|true)", front.group(1), re.M | re.I) \
                and not re.search(r"^fontes:[ \t]*\S", front.group(1), re.M):
            achados.append(f"mesclada sem fontes: {p.name}")
    for d in DECISOES:
        exatos = [re.compile(r"(?<![\w/-])" + re.escape(t.lower()) + r"(?![\w/-])") for t in (d["data"], d["issue"]) if t]
        ancora = d["ancora"].lower()
        if not any(ancora in l and all(r.search(l) for r in exatos) for l in linhas_texto):
            achados.append(f"decisão do dono ausente: {d['id']} ({d['data']}, {d['issue'] or 'sem issue'})")
    return achados


# ---- portão do diff (lição 2): decisão do dono que tem `proibido` reprova o diff que a contraria ----
# Campos opcionais de cada decisão: `proibido` (regex), `alvo` ("linha", padrão: linha adicionada; "arquivo": caminho do
# arquivo tocado), `onde` (globs do caminho; vazio = todo arquivo), `repos` (vazio = todo repo) e `mensagem`.
# Escape de linha com motivo: `memoria-ok` no fim dela. Testes, docs e o próprio gabarito nunca são varridos.
IGNORAR = ("tests/*", "test/*", "docs/*", "arquivo/*", "hub/memorias_decisoes.json", "*.test.*", "*.spec.*")
ESCAPE = "memoria-ok"


def _casa(globs, caminho):
    return any(fnmatch.fnmatch(caminho, g) for g in globs)


def violacoes_do_diff(diff, repo=None, decisoes=None):
    """Uma linha por violação: `arquivo:linha: [id] mensagem`. `diff` é o unified diff (git diff / gh pr diff)."""
    regras = [d for d in (REGRAS if decisoes is None else decisoes) if d.get("proibido")
              and (not repo or not d.get("repos") or repo in d["repos"])]
    achados, caminho, n, vistos = [], None, 0, set()

    def _arquivo(alvo):  # regra de alvo "arquivo": o caminho novo, venha de `+++` ou de rename/copy puro
        if _casa(IGNORAR, alvo):
            return None
        for d in (r for r in regras if r.get("alvo") == "arquivo"):
            if re.search(d["proibido"], alvo) and (alvo, d["id"]) not in vistos:
                vistos.add((alvo, d["id"]))
                achados.append(f"{alvo}:1: [{d['id']}] {d['mensagem']}")
        return alvo

    for linha in diff.splitlines():
        if linha.startswith("diff --git "):
            caminho = None
            continue
        if linha.startswith(("rename to ", "copy to ")):  # renomeação pura não tem `+++`
            _arquivo(linha.split(" to ", 1)[1].strip())
            continue
        if linha.startswith("+++ "):
            alvo = linha[4:].strip()
            caminho = None if alvo == "/dev/null" else _arquivo(alvo.removeprefix("b/"))
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", linha)
        if m:
            n = int(m.group(1)) - 1
            continue
        if not caminho or not linha.startswith(("+", " ")):
            continue
        n += 1
        if not linha.startswith("+") or ESCAPE in linha:
            continue
        for d in regras:
            if d.get("alvo", "linha") == "linha" and (not d.get("onde") or _casa(d["onde"], caminho)) \
                    and re.search(d["proibido"], linha[1:]):
                achados.append(f"{caminho}:{n}: [{d['id']}] {d['mensagem']}")
    return achados


NEGACAO_ANTES = re.compile(r"\b(n[ãa]o|nem|nunca|sem)\s+(\w+\s+){0,2}$", re.I)


def resposta_registrada(texto, comentario=False):
    """(lição 10): pergunta (ou, com `comentario`, fala de espera) cuja resposta o dono já deu. Devolve a decisão
    (id, resposta) ou None. `perguntar` usa o regex `pergunta`; `comentar`, só o `comentario` (mais estreito)."""
    campo = "comentario" if comentario else "pergunta"
    texto = texto or ""
    for d in REGRAS:
        if d.get(campo) and d.get("resposta"):
            for m in re.finditer(d[campo], texto, re.I | re.S):
                if not NEGACAO_ANTES.search(texto[:m.start()]):  # "Não preciso da aprovação..." nega a espera, não é espera
                    return d["id"], d["resposta"]
    return None


def agents_com_regra_revogada(raizes):
    """Arquivos AGENTS.md/CLAUDE.md ativos que ainda trazem a regra de espera já revogada ("avisar os sócios antes")."""
    achados = []
    for raiz in raizes:
        for nome in ("AGENTS.md", "CLAUDE.md"):
            arq = Path(raiz) / nome
            if not arq.is_file():
                continue
            corpo = arq.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?i)#\s*regra dos s[óo]cios|avisar antes", corpo) and not re.search(r"(?i)revogad|n[ãa]o existe .esperar", corpo):
                achados.append(f"{arq}: regra de espera já revogada (decisão socios-nao-seguram)")
    return achados


def diff_do_pr(repo, numero):
    """Diff de um PR do GitHub (`gh pr diff`); None se não deu para ler (quem chama adia, nunca libera às cegas)."""
    try:
        p = gh.executar(["pr", "diff", str(numero), "-R", repo], "memorias", capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError, github_api.CotaBaixa):
        return None
    return p.stdout if p.returncode == 0 and p.stdout.strip() else None


def _checar_diff(argv):
    ent, repo, base = None, None, "origin/main"
    it = iter(argv)
    for a in it:
        if a == "--repo":
            repo = next(it, None)
        elif a == "--base":
            base = next(it, base)
        else:
            ent = a
    if ent == "-":
        diff = sys.stdin.read()
    elif ent:
        diff = Path(ent).read_text(encoding="utf-8")
    else:
        diff = subprocess.run(["git", "diff", f"{base}...HEAD"], capture_output=True, text=True, check=True).stdout
    achados = violacoes_do_diff(diff, repo)
    print("\n".join(achados) if achados else "ok: o diff respeita as decisões do dono")
    return 1 if achados else 0


def main(argv):
    if not argv or argv[0] != "checar":
        print(__doc__, file=sys.stderr)
        return 2
    if "--diff" in argv:
        return _checar_diff([a for a in argv[1:] if a != "--diff"])
    raiz = Path(argv[1]) if len(argv) > 1 else PADRAO
    achados = problemas(raiz)
    if achados:
        print("\n".join(achados))
        return 1
    print(f"ok: {len(DECISOES)}/{len(DECISOES)} decisões, 0 ponteiro quebrado, 0 arquivo sem ponteiro ({len(_arquivos(raiz))} memórias)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
