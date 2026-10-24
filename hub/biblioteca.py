"""Biblioteca de referências: o clone do repo privado <org>/biblioteca em $HUB_DATA/biblioteca, atualizado
a cada 10 min por deploy/biblioteca.sh (cron do hub). Lê as receitas (receitas/<tema>/<slug>.md), busca por texto, tema
e projeto ("onde aplicar"), gera receitas/INDEX.md (só a lista de temas, um INDEX-<tema>.md por tema) e instala nas contas a regra de consultar
a biblioteca. Quem usa: a tela /biblioteca (hub/biblioteca_tela.py) e o MCP (hub/mcp_grupos/biblioteca.py).

    python3 -m hub.biblioteca indice [<clone>]   regrava receitas/INDEX.md (sai 0; imprime "mudou" se mudou)
    python3 -m hub.biblioteca instrucoes         bloco "biblioteca" do deploy/orquestrador/CLAUDE.md nas contas do claude
    python3 -m hub.biblioteca checar [<clone>]   link quebrado nos INDEX de tema e receita fora de índice (sai 1 se achar)
    python3 -m hub.biblioteca arquivar [<clone>] [--aplicar]   receita sem uso em 60 dias e sem projeto vai para arquivo/ (não apaga)
"""
import datetime as dt
import functools
import os
import pwd
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from hub import config, data, faltou, home_agentes

REPO = config.repo("biblioteca")
GERADO = "receitas/INDEX.md"
# "Onde aplicar" → projeto. Ordem = ordem do filtro na tela.
PROJETOS = {
    "asite01": ("asite01", r"\basite01\b"),
    "clientej": ("ClienteJ", r"clientej|clienteh"),
    "fsite06": ("fsite06", r"\bfsite06"),
    "clientei": ("ClienteI", r"clientei"),
    "psite15": ("Cliente G", r"psi ?por ?a[ií]"),
    "clientee": ("ClienteE", r"clientee"),
    "hub": ("Hub", r"nimbus|krohnos|\bhub\b"),
    "reels": ("Reels/Lobo", r"\breels?\b|\blobo\b"),
    "apartamento": ("Apartamento", r"apartamento|\bapto\b"),
    "evento_exemplo": ("Evento_exemplo", r"evento_exemplo"),
}
RE_ONDE = re.compile(r"^#+\s*(?:\d+[.)]\s*)?\**onde aplicar[^\n]*\n(.*?)(?=^#+\s|\Z)", re.I | re.M | re.S)
RE_LINHA_INDEX = re.compile(r"^\|(.+)\|\s*$")
RE_ARQ = re.compile(r"\]\(([^)]+\.md)\)|`?([\w./-]+\.md)`?")


def raiz():
    return Path(os.environ.get("BIBLIOTECA_DIR") or data() / "biblioteca")


def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def _git(r, *args):
    try:
        p = subprocess.run(["git", "-C", str(r), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def _indices(r):
    """{caminho relativo a receitas/: (titulo, status)} das tabelas dos INDEX-<tema>.md."""
    out = {}
    for idx in sorted((r / "receitas").glob("INDEX-*.md")):
        for linha in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            m = RE_LINHA_INDEX.match(linha.strip())
            if not m:
                continue
            cel = [c.strip() for c in m.group(1).split("|")]
            arq = next((m.group(1) or m.group(2) for c in cel[2:3] + cel for m in [RE_ARQ.search(c)] if m), None)
            if arq and len(cel) >= 4:  # | tema | título | arquivo | status |
                out.setdefault(arq.removeprefix("./").removeprefix("receitas/"), (cel[1], cel[3]))
    return out


RE_LINK_INDEX = re.compile(r"(?<![\w./-])((?:\./)?(?:[\w-]+/)+[\w.-]+\.md)")  # caminho tema/slug.md em link, crase ou célula


def checar(r=None):
    """Integridade da biblioteca: {"quebrados": [(INDEX, link)], "orfas": [caminho], "receitas": n}.
    Quebrado = link de um INDEX-<tema>.md para arquivo que não existe; órfã = receita viva que nenhum INDEX de tema linka.
    O INDEX.md geral (gerado) e arquivo/ não contam."""
    r = Path(r or raiz())
    base = r / "receitas"
    ligadas, quebrados = set(), []
    for idx in sorted(base.glob("INDEX-*.md")):
        for achado in dict.fromkeys(RE_LINK_INDEX.findall(idx.read_text(encoding="utf-8", errors="replace"))):
            rel = os.path.normpath(achado.removeprefix("receitas/"))
            ligadas.add(rel)
            if not (base / rel).is_file():
                quebrados.append((idx.name, achado))
    vivas = [p.relative_to(base).as_posix() for p in sorted(base.glob("*/**/*.md"))]
    return {"quebrados": quebrados, "orfas": [f"receitas/{v}" for v in vivas if v not in ligadas], "receitas": len(vivas)}


RE_URL = re.compile(r"https?://[^\s)>\]\"'`]+")
RE_BLOCO = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def mescla_perde_algo(origem, destino):
    """Reconciliação de uma mescla: URLs e blocos de código do texto `origem` que NÃO estão no `destino`
    (vazio = mescla íntegra). URL compara sem barra final e sem âncora; bloco compara sem espaço nas pontas."""
    norm = lambda u: u.rstrip(".,;:/").split("#")[0]  # noqa: E731
    urls = {norm(u) for u in RE_URL.findall(destino)}
    perdeu = sorted({norm(u) for u in RE_URL.findall(origem)} - urls)
    blocos = {b.strip() for b in RE_BLOCO.findall(destino)}
    perdeu += [b.strip().splitlines()[0][:80] + " …" for b in RE_BLOCO.findall(origem) if b.strip() and b.strip() not in blocos]
    return perdeu


def _datas(r):
    """{caminho: AAAA-MM-DD} do commit que criou cada arquivo (o mais antigo vence)."""
    out = {}
    for bloco in _git(r, "log", "--diff-filter=A", "--name-only", "--format=%x00%cs", "--", "receitas").split("\x00"):
        linhas = bloco.strip().splitlines()
        for c in linhas[1:]:
            out[c.strip()] = linhas[0]  # o log vem do mais novo ao mais velho: sobrescrever deixa a criação
    return out


def projetos_de(texto):
    m = RE_ONDE.search(texto)
    alvo = _sem_acento(m.group(1) if m else "")
    return [k for k, (_, rx) in PROJETOS.items() if re.search(rx, alvo)]


def _resumo(texto):
    """1ª linha de conteúdo que não é título nem link: o "o que é" da receita."""
    for linha in texto.splitlines():
        t = linha.strip().lstrip("-* ").strip()
        if not t or t.startswith(("#", "|", "```", ">")) or re.match(r"(?i)\**(links?|post|autor)\**\s*:", t):
            continue
        t = re.sub(r"(?i)^\**o que (é|e)\**\s*:\s*", "", t).replace("**", "")
        return t if len(t) <= 240 else t[:239].rstrip() + "…"
    return ""


def _versao(r):
    v = _git(r, "rev-parse", "HEAD").strip()
    if v:
        return v
    try:  # sem git (testes): a mtime mais nova das receitas
        return str(max((p.stat().st_mtime_ns for p in (r / "receitas").rglob("*.md")), default=0))
    except OSError:
        return "0"


@functools.lru_cache(maxsize=4)
def _carregar(r, versao):
    r = Path(r)
    base = r / "receitas"
    if not base.is_dir():
        return ()
    idx, datas = _indices(r), _datas(r)
    out = []
    for p in sorted(base.glob("*/**/*.md")):
        rel = p.relative_to(base).as_posix()
        try:
            texto = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h1 = next((l.lstrip("# ").strip() for l in texto.splitlines() if l.startswith("# ")), p.stem)
        titulo, status = idx.get(rel, (h1, ""))
        out.append({"caminho": f"receitas/{rel}", "tema": rel.split("/")[0], "titulo": titulo or h1, "status": status,
                    "projetos": projetos_de(texto), "data": datas.get(f"receitas/{rel}", ""), "resumo": _resumo(texto),
                    "texto": texto, "_busca": _sem_acento(titulo + "\n" + texto)})
    out.sort(key=lambda x: x["caminho"])
    out.sort(key=lambda x: x["data"], reverse=True)  # as mais novas no topo; empate pelo caminho
    return tuple(out)


def receitas(r=None):
    r = Path(r or raiz())
    return list(_carregar(str(r), _versao(r)))


def temas(itens):
    return sorted({x["tema"] for x in itens})


def _trecho(texto, termos, n=240):
    alvo = "".join((_sem_acento(c) or " ")[:1] for c in texto)  # 1 caractere por caractere: o índice vale no original
    i = min((alvo.find(t) for t in termos if alvo.find(t) >= 0), default=0)
    ini = max(0, i - n // 3)
    t = " ".join(texto[ini:ini + n].split())
    return ("…" if ini else "") + t + ("…" if ini + n < len(texto) else "")


def buscar(termo="", tema=None, projeto=None, limite=20, r=None):
    """Receitas que têm TODAS as palavras do termo (sem acento, sem caixa), filtradas por tema e projeto. Com termo:
    título que casa primeiro, depois quantas vezes casa; sem termo: as mais novas primeiro. Devolve dicts sem o texto."""
    termos = [t for t in _sem_acento(termo or "").split() if t]
    achados = []
    for x in receitas(r):
        if (tema and x["tema"] != tema) or (projeto and projeto not in x["projetos"]):
            continue
        if termos and not all(t in x["_busca"] for t in termos):
            continue
        tit = _sem_acento(x["titulo"])
        nota = sum(3 * (t in tit) + x["_busca"].count(t) for t in termos)
        achados.append((nota, x))
    if termos:
        achados.sort(key=lambda a: -a[0])
    return [{**{k: v for k, v in x.items() if k not in ("texto", "_busca")},
             "trecho": _trecho(x["texto"], termos) if termos else x["resumo"]} for _, x in achados[:limite]]


def ler(caminho, r=None):
    """Texto da receita (receitas/<tema>/<slug>.md ou <tema>/<slug>.md); None se não existe ou sai da pasta."""
    base = (Path(r or raiz()) / "receitas").resolve()
    c = str(caminho or "").strip().lstrip("/")
    c = c[len("receitas/"):] if c.startswith("receitas/") else c
    if not c.endswith(".md") or ".." in c.split("/"):
        return None
    p = (base / c).resolve()
    if base not in p.parents or not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


# ---- receitas/INDEX.md ----

def _celula(s):
    return str(s).replace("|", "/").replace("\n", " ").strip()


def indice(r=None):
    """Texto do receitas/INDEX.md: só os temas, com a contagem e o link do INDEX-<tema>.md. A lista por receita
    (título, projetos, status, data) mora no INDEX do tema e na busca; o geral não a repete."""
    itens = receitas(r)
    linhas = ["# Índice geral das receitas", "",
              "Gerado pelo nimbus-hub (`python3 -m hub.biblioteca indice`, cron de 10 min). Não edite à mão: cada tema tem o "
              "seu `INDEX-<tema>.md` com uma linha por receita; para achar por assunto, projeto ou data, use a busca da biblioteca "
              "(MCP `buscar_biblioteca`, tela /biblioteca).", "",
              f"{len(itens)} receitas em {len(temas(itens))} temas.", "",
              "| Tema | Receitas | Índice |", "|---|---|---|"]
    for tema in temas(itens):
        n = sum(1 for x in itens if x["tema"] == tema)
        linhas.append(f"| {tema} | {n} | [INDEX-{tema}.md](INDEX-{tema}.md) |")
    return "\n".join(linhas) + "\n"


def gravar_indice(r=None):
    p = Path(r or raiz()) / GERADO
    novo = indice(r)
    if p.is_file() and p.read_text(encoding="utf-8") == novo:
        return False
    p.write_text(novo, encoding="utf-8")
    return True


# ---- limpeza: arquivo/ ----

ARQUIVO = "arquivo"
DIAS_SEM_USO = 60


def a_arquivar(r=None, dias=DIAS_SEM_USO, agora=None, uso=None):
    """Receitas sem projeto em "Onde aplicar", criadas há mais de `dias` e sem PR que as cite nesse prazo (uso = relatório de
    hub.biblioteca_uso.relatorio). Vazio enquanto a medição de uso tem menos de `dias` dias: sem prova de "nunca usada" não se
    arquiva nada. Receita de método (nome com _) fica."""
    from hub import biblioteca_uso as U
    agora = agora or dt.datetime.now(dt.timezone.utc)
    desde = U.medindo_desde()
    if not desde or desde > (agora - dt.timedelta(days=dias)).strftime("%Y-%m-%d"):
        return []
    corte = (agora - dt.timedelta(days=dias)).strftime("%Y-%m-%d")
    usadas = {i["caminho"]: i["ultimo_uso"] for i in (uso or U.relatorio(dias, agora, r=r))["receitas"]}
    return [x for x in receitas(r) if not x["projetos"] and x["data"] and x["data"] < corte
            and not usadas.get(x["caminho"], "") >= corte and not Path(x["caminho"]).name.startswith("_")]


def arquivar(r=None, dias=DIAS_SEM_USO, agora=None, uso=None, aplicar=False):
    """Move as receitas de `a_arquivar` para arquivo/receitas/<tema>/ (git mv: a história fica), tira a linha do INDEX do tema e
    anota em arquivo/INDEX.md. Sem `aplicar` só lista. Devolve os caminhos movidos."""
    r = Path(r or raiz())
    achadas = a_arquivar(r, dias, agora, uso)
    if not aplicar:
        return [x["caminho"] for x in achadas]
    hoje = (agora or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    linhas = []
    for x in achadas:
        rel = x["caminho"]
        destino = r / ARQUIVO / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while destino.exists():  # já arquivada antes e recriada: não sobrescreve a versão arquivada
            n += 1
            destino = destino.with_name(f"{Path(rel).stem}-{n}{Path(rel).suffix}")
        if not _git_ok(r, "mv", rel, str(destino.relative_to(r))):
            (r / rel).rename(destino)
        rel_destino = str(destino.relative_to(r))
        idx = r / "receitas" / f"INDEX-{x['tema']}.md"
        if idx.is_file():
            base = rel.removeprefix("receitas/")
            ls = idx.read_text(encoding="utf-8").splitlines()
            novo = [l for l in ls if base not in l]
            if novo != ls:
                idx.write_text("\n".join(novo) + "\n", encoding="utf-8")
        linhas.append(f"| {hoje} | {x['titulo'].replace('|', '/')} | [{rel_destino}]({rel_destino}) | sem uso em {dias} dias e sem projeto |")
    if linhas:
        arq = r / ARQUIVO / "INDEX.md"
        base = arq.read_text(encoding="utf-8").rstrip("\n") if arq.is_file() else (
            "# Arquivo de receitas\n\nReceita sem PR que a cite em 60 dias e sem projeto em \"Onde aplicar\" (hub.biblioteca "
            "arquivar). Não foi apagada: volte com `git mv arquivo/receitas/<tema>/<arquivo>.md receitas/<tema>/`.\n\n"
            "| Data | Título | Arquivo | Motivo |\n|---|---|---|---|")
        arq.write_text(base + "\n" + "\n".join(linhas) + "\n", encoding="utf-8")
    return [x["caminho"] for x in achadas]


def _git_ok(r, *args):
    try:
        return subprocess.run(["git", "-C", str(r), *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---- instruções das contas ----

BLOCO = ("biblioteca", "<!-- biblioteca:inicio -->", "<!-- biblioteca:fim -->")


def _dono_claude(fd, modo):
    os.fchmod(fd, modo)
    if os.geteuid() == 0:
        u = pwd.getpwnam("claude")
        os.fchown(fd, u.pw_uid, u.pw_gid)


def instrucoes(repo_md=None, contas=None, dono=_dono_claude):
    """Troca só o bloco "biblioteca" no CLAUDE.md de cada conta do claude (~claude/.claude*/). Nunca falha o atualizar.sh."""
    try:
        import hub.vigia_clientej as V
    except ImportError as e:  # snapshot público: sem o plugin, as contas ficam como estão
        if not faltou(e, "hub.vigia_clientej"):
            raise
        print("aviso: sem hub.vigia_clientej; instruções da biblioteca não aplicadas")
        return 0
    repo_md = repo_md or Path(__file__).resolve().parent.parent / "deploy" / "orquestrador" / "CLAUDE.md"
    _, ini, fim = BLOCO
    bloco = V.secao(repo_md.read_text(encoding="utf-8"), ini, fim)
    if not bloco:
        print("aviso: CLAUDE.md do repo sem a seção biblioteca")
        return 0
    for conta in contas if contas is not None else sorted(home_agentes().glob(".claude*/")):
        c = Path(conta) / "CLAUDE.md"
        if Path(conta).is_symlink() or c.is_symlink() or not Path(conta).is_dir():
            continue
        try:
            antes = c.read_text(encoding="utf-8") if c.exists() else ""
            depois = V.aplicar_secao(antes, bloco, ini, fim)
            if depois != antes:
                V.gravar(c, depois, 0o664, dono)
                print(f"instruções da biblioteca em {c}")
        except OSError as ex:
            print(f"aviso: {c}: {ex}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["indice"]:
        if gravar_indice(argv[1] if len(argv) > 1 else None):
            print("mudou")
        return 0
    if argv[:1] == ["instrucoes"]:
        return instrucoes()
    if argv[:1] == ["arquivar"]:
        resto = [a for a in argv[1:] if a != "--aplicar"]
        movidas = arquivar(resto[0] if resto else None, aplicar="--aplicar" in argv)
        for m in movidas:
            print(("arquivada " if "--aplicar" in argv else "arquivaria ") + m)
        if movidas and "--aplicar" in argv:
            print("mudou")
        return 0
    if argv[:1] == ["checar"]:
        res = checar(argv[1] if len(argv) > 1 else None)
        print(f"receitas: {res['receitas']}, quebrados: {len(res['quebrados'])}, orfas: {len(res['orfas'])}")
        for i, l in res["quebrados"]:
            print(f"quebrado {i} -> {l}")
        for o in res["orfas"]:
            print(f"orfa {o}")
        return 1 if res["quebrados"] or res["orfas"] else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
