"""Skills por tarefa: a conta carrega só o kit base; o resto vem de uma biblioteca e liga por execução.

As 166 skills de ~/.claude-N/skills pesavam ~7 mil tokens em TODA sessão. Agora elas moram numa biblioteca
(hub.json → skills.biblioteca, uma pasta por skill com SKILL.md) e cada execução monta, num diretório temporário, um
plugin "tarefa" com symlinks só para as skills do base + as pedidas pela issue (label `skill:<nome>`) ou pela tarefa
(chave `skills` do frontmatter). A CLI recebe `--plugin-dir <pasta>/plugin --settings <json>`; o json desliga os
plugins instalados fora de plugins_base e põe subagentPromptCacheTtl em 5m. Tudo por execução: várias rodam ao mesmo
tempo na mesma conta, então nada vai para o diretório da conta.

    python3 -m hub.skills listar                      # nome — descrição de uma linha, de toda a biblioteca
    python3 -m hub.skills montar <pasta> [nomes...]   # debug: monta o plugin e imprime o resultado em JSON
    python3 -m hub.skills migrar [--aplicar]          # tira as skills das contas construtoras para a biblioteca
                                                      # (sem --aplicar só mostra o que faria)

hub.json: "skills": {"biblioteca": "<home_agentes>/skills-biblioteca", "plugins_base": ["superpowers@...", ...], "base": []}.
HUB_SKILLS_BIBLIOTECA troca a biblioteca (testes).
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from hub import config as config_, home_agentes

PLUGIN = "tarefa"
TTL_SUBAGENTE = "5m"
PADRAO = {"biblioteca": "",  # vazio = <home_agentes>/skills-biblioteca (ver biblioteca())
          "plugins_base": ["superpowers@claude-plugins-official", "code-review@claude-plugins-official"], "base": []}
RE_NOME = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")


def config():
    """hub.json → skills, com o padrão para o que faltar."""
    try:
        j = config_.ler_hub_json().get("skills") or {}
    except (OSError, ValueError):
        j = {}
    return {**PADRAO, **{k: v for k, v in j.items() if k in PADRAO}}


def biblioteca(biblioteca=None):
    return Path(biblioteca or os.environ.get("HUB_SKILLS_BIBLIOTECA") or config()["biblioteca"] or home_agentes() / "skills-biblioteca")


def plugins_base():
    return list(config()["plugins_base"])


def base():
    return list(config()["base"])


def _descricao(skill_md):
    """Descrição de uma linha do frontmatter (aceita `>`/`|` de várias linhas): primeira frase/linha, até 160."""
    try:
        linhas = Path(skill_md).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    if not linhas or linhas[0].strip() != "---":
        return ""
    corpo = []
    for l in linhas[1:]:
        if l.strip() == "---":
            break
        corpo.append(l)
    for i, l in enumerate(corpo):
        m = re.match(r"^description:\s*(.*)$", l)
        if not m:
            continue
        v = m.group(1).strip()
        if v[:1] in (">", "|"):
            v = ""
            for seguinte in corpo[i + 1:]:
                if seguinte.strip():
                    if not seguinte.startswith((" ", "\t")):
                        break
                    v = seguinte.strip()
                    break
        v = v.strip("\"'")
        primeira = re.split(r"(?<=[.!?])\s", v, maxsplit=1)[0]
        return primeira[:160]
    return ""


def listar(biblioteca_=None):
    """[(nome, descrição de uma linha)] da biblioteca, em ordem."""
    pasta = biblioteca(biblioteca_)
    if not pasta.is_dir():
        return []
    return [(p.name, _descricao(p / "SKILL.md")) for p in sorted(pasta.iterdir()) if (p / "SKILL.md").is_file()]


def nome_valido(nome):
    return bool(RE_NOME.match(nome or "")) and ".." not in nome and "/" not in nome


# revisão e exploração dentro da sessão rodam em Haiku; o subagente padrão (CLAUDE_CODE_SUBAGENT_MODEL,
# sonnet) fica para implementar. Vão no plugin da execução como `tarefa:revisor` e `tarefa:explorador`.
AGENTES = {
    "revisor": ("Revisa um diff ou PR atrás de bugs reais (correção, regressão, teste faltando) e devolve achados curtos "
                "com arquivo:linha. Use para a revisão por subagente do fluxo (requesting-code-review, code-review).",
                "Você revisa código. Leia o diff e o código em volta; aponte só o que quebra ou engana (bug, caso de borda, "
                "teste que não cobre o que diz), com arquivo:linha e o cenário que falha. Sem elogio, sem estilo. Nada "
                "achado: diga \"Sem achados sérios\". Responda em português, em até 15 linhas."),
    "explorador": ("Acha arquivos, funções e usos no repo e devolve só a conclusão (caminhos e linhas). Use para busca e "
                   "exploração do código antes de mexer.",
                   "Você explora um repositório para quem vai mexer nele. Use grep, glob e leitura de trechos; não edite "
                   "nada. Devolva a resposta direta: caminhos com linha e uma frase do que cada um faz. Português, curto."),
}


def _agentes(plugin):
    pasta = Path(plugin) / "agents"
    pasta.mkdir(exist_ok=True)
    for nome, (descricao, corpo) in AGENTES.items():
        (pasta / f"{nome}.md").write_text(f"---\nname: {nome}\ndescription: {descricao}\nmodel: haiku\n---\n\n{corpo}\n",
                                          encoding="utf-8")


def montar(pasta, nomes, biblioteca_=None):
    """Cria <pasta>/plugin com as skills do base + `nomes` (symlinks para a biblioteca). Nome desconhecido ou
    inválido vai em `faltando`. Cria o plugin mesmo sem nenhuma skill."""
    lib = biblioteca(biblioteca_)
    plugin = Path(pasta) / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / "skills").mkdir(exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN, "description": "Skills ligadas para esta execução", "version": "1.0.0"}), encoding="utf-8")
    _agentes(plugin)
    ligadas, faltando = [], []
    for nome in dict.fromkeys([*base(), *(nomes or [])]):
        if nome_valido(nome) and (lib / nome / "SKILL.md").is_file():
            alvo = plugin / "skills" / nome
            if not alvo.is_symlink():
                alvo.symlink_to((lib / nome).resolve(), target_is_directory=True)
            ligadas.append(nome)
        else:
            faltando.append(nome)
    return {"plugin": str(plugin), "ligadas": ligadas, "faltando": faltando}


def settings_json(config_dir):
    """Settings extra da execução: TTL do cache dos subagentes e plugins da conta (fora do base) desligados."""
    try:
        habilitados = json.loads((Path(config_dir) / "settings.json").read_text(encoding="utf-8")).get("enabledPlugins") or {}
    except (OSError, ValueError):
        habilitados = {}
    base_ = set(plugins_base())
    extra = {"subagentPromptCacheTtl": TTL_SUBAGENTE,
             "enabledPlugins": {p: False for p in sorted(habilitados) if p not in base_}}
    if excluir := claude_md_duplicado(config_dir):
        extra["claudeMdExcludes"] = excluir
    return json.dumps(extra)


def claude_md_duplicado(config_dir):
    """Contas 2+ rodam com cwd no home: a CLI lê o CLAUDE.md da conta (usuário) e o ~/.claude/CLAUDE.md como projeto,
    com o mesmo texto (~mil tokens por turno). Exclui a cópia de projeto só na execução do hub; o bot, que
    roda fora do home, segue lendo o da conta."""
    conta = Path(config_dir)
    primeira = conta.parent / ".claude"
    if conta.name == ".claude":
        return []
    return [str(primeira / n) for n in ("CLAUDE.md", "RTK.md") if _mesmo_texto(primeira / n, conta / n)]


def _mesmo_texto(a, b):
    """Só exclui a cópia de projeto quando a da conta tem o mesmo conteúdo: texto diferente é instrução própria."""
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# ---- MCP por tarefa ----
# As contas declaram rea (~200 ferramentas), earshot e openseo, e a CLI ainda traz ~20 conectores do claude.ai: ~3 mil
# tokens relidos em cada turno de toda sessão (medido em 16/10: 34,2 mil → 31,2 mil na sessão vazia). Agora cada
# execução recebe `--strict-mcp-config --mcp-config <json>` só com o base + o que a tarefa pede: label `mcp:<nome>`
# na issue ou palavra do pedido que casa com o padrão do servidor.
# openseo (o MCP do hub, ~85 ferramentas: SEO, GSC/GA4, SERP, biblioteca) também é sob demanda: só a conta 1 o declarava.
MCP_PADRAO = {"base": [], "sob_demanda": {
    "openseo": r"\bseo\b|serp|ranking|palavras?.?chave|keyword|search console|\bgsc\b|analytics|\bga4\b|clarity|"
               r"biblioteca|receita|concorrent|backlink|\bgmb\b|perfil (?:da empresa|do google)|avalia[çc][õo]es|"
               r"visibilidade|\bsites?\b|p[áa]ginas?\b|artigos?\b|landing|blog|design",
    "rea": r"engenharia reversa|reverse.?engineer|\brea\b|decompil|bin[áa]rio|\bapk\b|\bipa\b|ghidra|"
           r"bundle (?:js|javascript)|minificad|\belectron\b|\badb\b",
    "earshot": r"\bsom\b|\bsons\b|[áa]udio|trilha|m[úu]sica|\bsfx\b|narra[çc][ãa]o|\bvoz\b|\breels?\b|"
               r"v[íi]deo|\bfilmes?\b|earshot|mixagem"}}


def config_mcp():
    try:
        j = config_.ler_hub_json()
    except (OSError, ValueError):
        j = {}
    c = j.get("mcp_tarefa") or {}
    return {"base": list(c.get("base", MCP_PADRAO["base"])),
            "sob_demanda": {**MCP_PADRAO["sob_demanda"], **(c.get("sob_demanda") or {})}, "url_hub": j.get("mcp")}


def mcp_da_tarefa(texto, pedidos=()):
    """Servidores MCP desta execução: base + label `mcp:<nome>` (pedidos) + os sob demanda cujo padrão casa no texto."""
    c = config_mcp()
    casam = [n for n, padrao in c["sob_demanda"].items() if re.search(padrao, texto or "", re.I)]
    return list(dict.fromkeys([*c["base"], *pedidos, *casam]))


def _servidores(home):
    """Definições mcpServers das contas (a do processo primeiro: CLAUDE_CONFIG_DIR ou ~/.claude.json)."""
    home = Path(home)
    arqs = [Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"] if os.environ.get("CLAUDE_CONFIG_DIR") else []
    arqs += [home / ".claude.json", *sorted(home.glob(".claude*/.claude.json"))]
    defs = {}
    for a in arqs:
        try:
            for nome, d in (json.loads(a.read_text(encoding="utf-8")).get("mcpServers") or {}).items():
                defs.setdefault(nome, d)
        except (OSError, ValueError, AttributeError):
            continue
    return defs


def mcp_json(pasta, nomes, home=None):
    """Grava <pasta>/mcp.json com os servidores pedidos que existem; devolve (caminho, ligados)."""
    defs = _servidores(home or home_agentes())
    url = config_mcp()["url_hub"]
    if url and "openseo" not in defs:  # o MCP do hub (biblioteca, SERP): nem toda conta o declara
        defs["openseo"] = {"type": "http", "url": url}
    escolhidos = {n: defs[n] for n in nomes if n in defs}
    arq = Path(pasta) / "mcp.json"
    arq.write_text(json.dumps({"mcpServers": escolhidos}), encoding="utf-8")
    return str(arq), list(escolhidos)


# ---- migração das skills das contas para a biblioteca ----

def _assinatura(p):
    """Conteúdo de uma skill (arquivos e bytes, symlinks resolvidos) para comparar cópias."""
    p = Path(p)
    if p.is_file():
        return {"": p.read_bytes()}
    return {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file()}


def _config_conta(n, home):
    return Path(home) / (".claude" if str(n) == "1" else f".claude-{n}")


def migrar(contas=None, aplicar=False, home=None, biblioteca_=None):
    """Plano (e, com aplicar=True, a execução) de mover as skills das contas construtoras para a biblioteca.
    Devolve [{"acao", "conta", "nome", "origem"}]: mover | remover-dup | conflito | quebrado | copiar-plugin.
    Nome já na biblioteca: igual → sai da conta; diferente → a da biblioteca vale e a da conta fica (conflito)."""
    from hub import conta as C
    home = Path(home or os.environ.get("CLAUDE_HOME") or home_agentes())
    lib = biblioteca(biblioteca_)
    contas = [c for c in C.CONTAS if C.faixa(c) == "construtor"] if contas is None else [str(c) for c in contas]
    plano, previsto = [], {}  # previsto: nome → caminho de onde vai sair o conteúdo (dry-run enxerga as outras contas)

    def destino(nome):
        return lib / nome if (lib / nome).exists() else previsto.get(nome)

    for n in contas:
        pasta = _config_conta(n, home) / "skills"
        if not pasta.is_dir():
            continue
        for p in sorted(pasta.iterdir()):
            nome, real = p.name, p.resolve()
            reg = {"conta": n, "nome": nome, "origem": str(p)}
            if not real.exists() or not nome_valido(nome):
                plano.append({"acao": "quebrado", **reg})
                if aplicar and p.is_symlink():
                    p.unlink()
                continue
            if not real.is_dir():
                continue  # arquivo solto em skills/: não é skill, fica
            ja = destino(nome)
            if ja is not None:
                if _assinatura(ja) == _assinatura(real):
                    plano.append({"acao": "remover-dup", **reg})
                    if aplicar:
                        _apagar(p)
                else:
                    plano.append({"acao": "conflito", **reg})
                continue
            plano.append({"acao": "mover", **reg})
            previsto[nome] = real
            if aplicar:
                lib.mkdir(parents=True, exist_ok=True)
                shutil.copytree(real, lib / nome, symlinks=False)
                _apagar(p)
                previsto.pop(nome)
    base_ = set(plugins_base())
    for n in contas:
        try:
            instalados = json.loads((_config_conta(n, home) / "plugins" / "installed_plugins.json")
                                    .read_text(encoding="utf-8")).get("plugins") or {}
        except (OSError, ValueError):
            continue
        for pid, regs in sorted(instalados.items()):
            if pid in base_:
                continue
            for r in regs or []:
                for s in sorted((Path(r.get("installPath") or "/nonexistent") / "skills").glob("*/SKILL.md")):
                    nome = s.parent.name
                    if not nome_valido(nome) or destino(nome) is not None:
                        continue
                    plano.append({"acao": "copiar-plugin", "conta": n, "nome": nome, "origem": str(s.parent)})
                    previsto[nome] = s.parent
                    if aplicar:
                        lib.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(s.parent, lib / nome, symlinks=False)
                        previsto.pop(nome)
    return plano


def _apagar(p):
    p = Path(p)
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.skills")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar", help="skills da biblioteca: nome — descrição")
    mo = sub.add_parser("montar", help="debug: monta o plugin tarefa numa pasta")
    mo.add_argument("pasta")
    mo.add_argument("nomes", nargs="*")
    mi = sub.add_parser("migrar", help="move as skills das contas construtoras para a biblioteca")
    mi.add_argument("--aplicar", action="store_true", help="sem isso só mostra o plano")
    a = ap.parse_args(argv)
    if a.cmd == "listar":
        for nome, desc in listar():
            print(f"{nome} — {desc}")
    elif a.cmd == "montar":
        print(json.dumps(montar(a.pasta, a.nomes), ensure_ascii=False, indent=2))
    else:
        plano = migrar(aplicar=a.aplicar)
        for r in plano:
            print(f"{r['acao']:14} conta {r['conta']}  {r['nome']}  ({r['origem']})")
        resumo = {}
        for r in plano:
            resumo[r["acao"]] = resumo.get(r["acao"], 0) + 1
        print(("aplicado: " if a.aplicar else "simulação (use --aplicar): ") + json.dumps(resumo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
