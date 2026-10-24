"""Coletores do segundo cérebro. Cada um devolve unidades (hub.grafo.unidade) de um prefixo de origem.

Grupos de `python3 -m hub.grafo atualizar`:
    rapido  (cron de 10 min)  sites, pessoas, infra, repos (por hash do commit), memoria, orch, receitas, segredos, relatorios, linear
    diario  (cron diário)     docker (apps do Dokploy, bancos, domínios do Traefik, nomes de variável dos contêineres) e infra
O que só o root lê (memória do PC, ~/pc/segredos, /root/*.env, as outras VPS por SSH) chega já reduzido a nomes em
$HUB_DATA/grafo-raiz/ (hub/grafo_raiz.py, que o deploy/grafo.sh roda como root antes de virar claude).
Segurança: só NOME de variável de segredo (nunca o valor: os parsers cortam na linha antes do "="); o que parece chave é
trocado por [oculto] em todo texto (hub.grafo.limpar); bancos e leitura/ dos apps não são lidos, só a pasta é citada.
"""
import datetime as dt
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from hub import RAIZ, config, data, grafo as G, home_agentes, sites as S

MAX_DOCS = 70           # documentos por repo
MAX_PEDACOS_DOC = 8
MAX_CHARS_DOC = 14_000
RE_ISSUE = re.compile(r"\bKRN-\d+\b")
RE_PR = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
RE_VAR = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
RE_SEGREDOISH = re.compile(r"(?i)key|token|secret|pass|senha|credential|database|_url$|dsn|auth|client_id|api")
RE_DECISAO = re.compile(r"(?i)decidiu|decidido|decisão do fulano|o fulano (?:aprovou|vetou|definiu|mandou)|\bdecis[aã]o\b")


def _ler(p, limite=None):
    try:
        t = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return t[:limite] if limite else t


def _git(r, *args, timeout=60):
    try:
        p = subprocess.run(["git", "-C", str(r), *args], capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _data_iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


# ---- Traefik (domínio → app) ----

RE_HOST = re.compile(r"rule:\s*Host\(`([^`]+)`\)")
RE_URL_SERVICO = re.compile(r"url:\s*http://([\w.-]+):(\d+)")


def traefik(pasta="/etc/dokploy/traefik/dynamic"):
    """{domínio: [(app, porta)]} dos .yml do Dokploy. Não lê acme.json nem certificados."""
    out = {}
    for arq in sorted(Path(pasta).glob("*.yml")):
        texto = _ler(arq)
        hosts = set(RE_HOST.findall(texto))
        alvos = set(RE_URL_SERVICO.findall(texto)) or {(arq.stem, "")}
        for h in hosts:
            out.setdefault(h.lower(), set()).update(alvos)
    return {h: sorted(a) for h, a in out.items()}


# ---- sites ----

def sites(pasta=None, traefik_dir="/etc/dokploy/traefik/dynamic"):
    """Projeto, domínios, repo e app de cada site do hub (sites/*.json)."""
    entidades, relacoes, pedacos = [], [], []
    tr = traefik(traefik_dir)
    for s in S.carregar(pasta):
        pid = f"projeto:{s['id']}"
        ctx = " ".join((s.get("contexto") or "").split())
        entidades.append({"tipo": "projeto", "chave": s["id"], "nome": s["nome"], "alias": s.get("ids_antigos", []) + (s.get("marca", {}).get("aliases") or []),
                          "desc": f"Projeto/negócio {s['nome']}, site {s['dominio']}, repo {s['repo']['github']} (base {s['repo']['base']}, "
                                  f"deploy {s['repo'].get('deploy')}). {'Ativo.' if s.get('ativo') else 'Inativo.'} {ctx}",
                          "caminho": f"sites/{s['id']}.json", "extra": {"ativo": s.get("ativo")}})
        pedacos.append((pid, "contexto", ctx))
        ids = s.get("ids") or {}
        pedacos.append((pid, "ids", "IDs de painel de " + s["nome"] + ": " + "; ".join(f"{k} {v}" for k, v in ids.items() if v)))
        dom = s["dominio"].lower()
        hosts = [dom] + [h.lower() for h in (s.get("hosts_alternativos") or []) + (s.get("hosts_citacao") or [])]
        rid = f"repo:{s['repo']['github'].split('/')[-1]}"
        entidades.append({"tipo": "repo", "chave": s["repo"]["github"].split("/")[-1], "nome": s["repo"]["github"], "alias": [s["nome"], f"código do {s['nome']}"],
                          "desc": f"Repositório {s['repo']['github']} do projeto {s['nome']}; branch base {s['repo']['base']}; "
                                  f"pacotes {s['repo'].get('pacotes')}; deploy {s['repo'].get('deploy')}.",
                          "link": f"https://github.com/{s['repo']['github']}"})
        relacoes.append((pid, "dono", rid))
        relacoes.append(("pessoa:fulano", "dono", pid))
        vid = f"vps:{_cerebro().get('site_vps', {}).get(s['id']) or servidor_hub()}"
        relacoes.append((rid, "roda_em", vid))
        for h in hosts:
            did = f"dominio:{h}"
            entidades.append({"tipo": "dominio", "chave": h, "nome": h, "desc": f"Domínio {h} do projeto {s['nome']}" +
                              (" (principal)." if h == dom else ".")})
            relacoes.append((rid, "serve", did))
            relacoes.append((did, "pertence", pid))
            relacoes.append((did, "roda_em", vid))
            for app, porta in tr.get(h, []):
                relacoes.append((f"app:{app}", "serve", did))
                relacoes.append((rid, "roda_em", f"app:{app}"))
                entidades.append({"tipo": "app", "chave": app, "nome": app, "desc": f"App do Dokploy {app} (porta {porta}), serve {h}."})
        dk = ids.get("dokploy_app")
        if dk:  # applicationId do Dokploy: o `deploy-app` usa este id, o contêiner usa o appName
            apps = {a for h in hosts for a, _ in tr.get(h, [])}
            for app in apps:
                entidades.append({"tipo": "app", "chave": app, "nome": app, "alias": [dk],
                                  "desc": f"App do Dokploy {app}; applicationId {dk} (use este id no deploy-app)."})
    return [G.unidade("sites:todos", entidades, relacoes, pedacos)]


def _cerebro():
    """Quem é quem e a infra que não muda toda semana: vem de <caminhos.privado>/cerebro.json (nomes e hosts reais são do
    repo privado); sem ele, do cerebro.json da raiz do repo; sem nenhum, vazio. Não fica no hub.json (cópia privada na VPS)."""
    privado = config.get("caminhos.privado")
    for p in ([Path(privado).expanduser() / "cerebro.json"] if privado else []) + [RAIZ / "cerebro.json"]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}

def servidor_hub():
    return _vps_local().split(":", 1)[1]


def pessoas():
    ents, rels = [], []
    for p in _cerebro().get("pessoas", []):
        ents.append({"tipo": "pessoa", "chave": p["chave"], "nome": p["nome"], "alias": p.get("alias", []), "desc": p["desc"]})
        dono_de = set(p.get("dono_de", []))
        rels += [(f"pessoa:{p['chave']}", "dono" if alvo in dono_de else "pertence", alvo) for alvo in p.get("donos", [])]
    return [G.unidade("pessoas:todas", ents, rels)]

def raiz_dir():
    return Path(data()) / "grafo-raiz"


def _json(p, padrao):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def infra(raiz=None, systemd="/etc/systemd/system"):
    """VPS, bancos e apps fixos + unidades systemd desta VPS + retrato das outras VPS (contêineres, systemd, crons) do grafo_raiz."""
    raiz = Path(raiz or raiz_dir())
    hub = servidor_hub()
    cer = _cerebro()
    vh = cer.get("servidor_hub", {})
    ents = [{"tipo": "vps", "chave": hub, "nome": f"VPS do hub ({hub})", "alias": vh.get("alias", ["servidor_hub", "hub"]),
             "desc": vh.get("desc", "VPS do hub.")}]
    rels = []
    for projeto in cer.get("projetos_infra", []):
        ents.append({"tipo": "projeto", "chave": projeto["chave"], "nome": projeto["nome"],
                      "alias": projeto.get("alias", []), "desc": projeto.get("desc", "")})
        rels.extend((f"projeto:{projeto['chave']}", "dono", f"repo:{repo}") for repo in projeto.get("repos", []))
    for v in cer.get("vps", []):
        ents.append({"tipo": "vps", "chave": v["chave"], "nome": v["nome"], "alias": v.get("alias", []), "desc": v["desc"]})
    for bd in cer.get("bancos", []):
        ents.append({"tipo": "banco", "chave": bd["chave"], "nome": bd["chave"], "desc": bd["desc"]})
        vps = hub if bd["vps"] in ("hub", "clientec") else bd["vps"]
        rels += [(f"banco:{bd['chave']}", "roda_em", f"vps:{vps}"), (bd["repo"], "usa", f"banco:{bd['chave']}")]
    for ap in cer.get("apps", []):
        app, vps, dom, repo = (ap.get(k) for k in ("chave", "vps", "dominio", "repo"))
        if not app:
            continue
        ents.append({"tipo": "app", "chave": app, "nome": app, "desc": f"App {app} do inventário de infraestrutura."})
        if vps:
            rels.append((f"app:{app}", "roda_em", f"vps:{vps}"))
        if dom:
            rels.append((f"app:{app}", "serve", f"dominio:{dom}"))
        if repo:
            rels.append((repo, "roda_em", f"app:{app}"))
    # systemd desta VPS (o claude lê /etc/systemd/system; link para /usr/lib é unidade do sistema, fica de fora)
    for f in sorted(Path(systemd).glob("*.service")):
        if f.is_symlink():
            continue
        e, r = servico(_ler(f, 8000), f.stem, None, f"vps:{hub}", str(f))
        ents += e
        rels += r
    for arq in sorted(raiz.glob("remoto-*.json")):
        d = _json(arq, {})
        vps = d.get("vps") or arq.stem.removeprefix("remoto-")
        vid, quando = f"vps:{vps}", (d.get("quando") or "")[:10]
        ativos = set(d.get("ativos") or [])
        for nome, desc, exe in d.get("systemd") or []:
            if nome:
                e, r = servico(f"Description={desc}\nExecStart={exe}\n", nome, None, vid, f"{vps}:/etc/systemd/system/{nome}.service")
                e[0]["desc"] = e[0]["desc"].replace("Unidade systemd", f"Unidade systemd da {vps}") + (" Ativa." if f"{nome}.service" in ativos else "")
                ents += e
                rels += r
        vistos = set()
        for linha in (d.get("swarm") or []) + (d.get("docker") or []):
            nome, imagem = linha[0], linha[1] if len(linha) > 1 else ""
            if not nome or RE_EFEMERO.match(nome) or re.search(r"\.\d+\.\w{20,}$", nome) or nome in vistos:
                continue
            vistos.add(nome)
            tipo = "banco" if re.search(r"postgres|paradedb|mysql|mariadb|redis", imagem) else "app"
            ents.append({"tipo": tipo, "chave": nome, "nome": nome, "data": quando,
                         "desc": f"{'Banco' if tipo == 'banco' else 'App'} {nome} na {vps} (contêiner, imagem {imagem}; visto em {quando})."})
            rels.append((f"{tipo}:{nome}", "roda_em", vid))
            # The static inventory names the service; runtime adds a Dokploy
            # suffix. Reuse its verified source and hosts on this same VPS.
            for ap in cer.get('apps', []):
                if ap.get('vps') == vps and ap.get('chave') and re.fullmatch(re.escape(ap['chave']) + r'-[a-z0-9]{6}', nome):
                    if ap.get('repo'):
                        rels.append((ap['repo'], 'roda_em', f'{tipo}:{nome}'))
                    if ap.get('dominio'):
                        rels.append((f'{tipo}:{nome}', 'serve', f"dominio:{ap['dominio']}"))
        for item in d.get("cron") or []:
            if len(item) == 2:
                e, r = crons(item[1], None, vid, f"{vps}:/etc/cron.d/{item[0]}")
                ents += e
                rels += r
    rels.append(("repo:clienteh", "roda_em", "vps:vps-servidor_b"))  # a gêmea: banco clienteh-dados e coleta
    return [G.unidade("infra:todas", ents, [x for x in rels if x[2] and x[0]])]


# ---- repos ----

def _base(r):
    ref = (_git(r, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    if ref:
        return ref
    for b in ("origin/main", "origin/master", "origin/develop"):
        if (_git(r, "rev-parse", "--verify", "-q", b) or "").strip():
            return b
    return ""


def _arquivos_doc(arquivos):
    """README, CLAUDE, AGENTS na raiz e docs/**/*.md (os mais curtos de caminho primeiro, no máximo MAX_DOCS)."""
    raiz = [a for a in arquivos if a.lower() in ("readme.md", "claude.md", "agents.md", "sistema.md", "raiz.md")
            or (a.lower().endswith(("/claude.md", "/agents.md")) and a.count("/") <= 3 and "node_modules" not in a)]
    docs = sorted((a for a in arquivos if a.lower().endswith(".md") and a.lower().startswith(("docs/", "plans/", "planos/"))
                   and "node_modules" not in a), key=lambda a: (a.count("/"), a))
    return (raiz + docs)[:MAX_DOCS]


RE_CRON = re.compile(r"^((?:\S+\s+){4}\S+)\s+(?:(\w+)\s+)?(\S.*)$")


def crons(texto, repo_id, vps_id, arquivo):
    """Linhas de crontab → entidades cron (comentário logo acima vira a descrição)."""
    ents, rels, comentario = [], [], []
    for linha in texto.splitlines():
        l = linha.strip()
        if l.startswith("#"):
            comentario.append(l.lstrip("# ").strip())
            continue
        m = RE_CRON.match(l)
        if not m or not re.match(r"^[\d*/,-]+$", m.group(1).split()[0]):
            comentario = [] if not l else comentario
            continue
        quando, cmd = m.group(1), re.sub(r"\s*>>.*$", "", m.group(3))
        m2 = re.search(r"rotina\.sh\s+(\S+)", cmd)
        if m2:
            nome = m2.group(1)
        else:  # sem rotina.sh: o nome do script (ou do módulo python) do comando
            alvo = next((w for w in reversed(cmd.split()) if not w.startswith(("-", ">", "2>"))), "cron")
            nome = re.sub(r"[^\w.-]+", "-", Path(alvo).stem.strip("-_ ") or "cron")[:40]
        cid = f"cron:{nome}"
        ents.append({"tipo": "cron", "chave": nome, "nome": f"cron {nome}", "alias": [nome],
                     "desc": f"Cron {nome} ({quando}, UTC; BRT = UTC-3) em {arquivo}: {cmd[:200]}. {' '.join(comentario)[:420]}",
                     "caminho": arquivo})
        rels += [(cid, "roda_em", vps_id)] + ([(cid, "depende_de", repo_id)] if repo_id else [])
        comentario = []
    return ents, rels


def _so_caminhos(cmd):
    """ExecStart sem argumentos: --password=... não entra no índice. A regra é a do grafo_raiz (aspas, opção antes)."""
    from hub.grafo_raiz import so_caminhos
    return so_caminhos(cmd)


def servico(texto, nome, repo_id, vps_id, arquivo):
    desc = re.search(r"(?m)^Description=(.*)$", texto)
    exe = re.search(r"(?m)^ExecStart=(.*)$", texto)
    return ([{"tipo": "servico", "chave": nome, "nome": f"serviço {nome}", "alias": [nome],
              "desc": f"Unidade systemd {nome}: {desc.group(1) if desc else ''}. Roda: {_so_caminhos(exe.group(1)) if exe else '?'}.",
              "caminho": arquivo}],
            [(f"servico:{nome}", "roda_em", vps_id)] + ([(f"servico:{nome}", "depende_de", repo_id)] if repo_id else []))


def _vps_local():
    return f"vps:{socket.gethostname().lower()}"


def _repo(r, nome, base, sha, orgrepo):
    rid = f"repo:{nome}"
    arquivos = (_git(r, "ls-tree", "-r", "--name-only", sha) or "").splitlines()
    ents = [{"tipo": "repo", "chave": nome, "nome": orgrepo or nome, "link": f"https://github.com/{orgrepo}" if orgrepo else "",
             "desc": f"Repositório {orgrepo or nome}, branch base {base.split('/')[-1]}, {len(arquivos)} arquivos."}]
    rels, peds = [], []
    sobre = ""
    for caminho in _arquivos_doc(arquivos):
        texto = _git(r, "show", f"{sha}:{caminho}")[:MAX_CHARS_DOC]
        if not texto.strip():
            continue
        did = f"documento:{nome}/{caminho}"
        titulo = next((m for m in re.findall(r"(?m)^#\s+(.+)$", texto)[:1]), caminho)
        primeiro = next((l.strip() for l in texto.splitlines() if l.strip() and not l.startswith(("#", "---", "|", "```", "<"))), "")
        if caminho.lower() == "readme.md":
            sobre = primeiro
        ents.append({"tipo": "documento", "chave": f"{nome}/{caminho}", "nome": f"{nome}/{caminho}", "alias": [titulo],
                     "desc": f"Documento {caminho} do repo {nome}: {titulo}. {primeiro[:300]}", "caminho": f"{nome}/{caminho}",
                     "link": f"https://github.com/{orgrepo}/blob/{base.split('/')[-1]}/{caminho}" if orgrepo else ""})
        rels.append((did, "documenta", rid))
        n = 0
        for sec, corpo in G.secoes(texto):
            for p in G.quebrar(corpo):
                if n >= MAX_PEDACOS_DOC:
                    break
                peds.append((did, sec or "abertura", f"{sec}\n{p}" if sec else p))
                n += 1
    if sobre:
        ents[0]["desc"] += f" {sobre[:300]}"
    # nomes de variável de .env.example (nunca valor)
    for a in arquivos:
        if re.search(r"(?:^|/)\.env\.(?:example|sample)$|(?:^|/)\.env\.local\.example$", a):
            for linha in (_git(r, "show", f"{sha}:{a}") or "").splitlines():
                m = RE_VAR.match(linha)
                if m and RE_SEGREDOISH.search(m.group(1)):
                    sid = f"segredo:{m.group(1)}@{nome}/{a}"
                    ents.append({"tipo": "segredo", "chave": f"{m.group(1)}@{nome}/{a}", "nome": m.group(1),
                                 "desc": f"Variável de ambiente (chave/credencial) {m.group(1)}, declarada em {nome}/{a}. Só o nome; o valor fica no ambiente do app.",
                                 "caminho": f"{nome}/{a}"})
                    rels.append((rid, "usa", sid))
    if nome in ("repo-privado", "nimbus-hub"):
        vps = _vps_local()
        for a in arquivos:
            if a.startswith("deploy/") and a.endswith(".cron"):
                e, r2 = crons(_git(r, "show", f"{sha}:{a}"), rid, vps, f"{nome}/{a}")
                ents += e
                rels += r2
            elif a.startswith("deploy/") and a.endswith(".service"):
                e, r2 = servico(_git(r, "show", f"{sha}:{a}"), Path(a).stem, rid, vps, f"{nome}/{a}")
                ents += e
                rels += r2
            elif a.startswith("tarefas/") and a.endswith(".md") and not a.endswith("README.md"):
                t = _git(r, "show", f"{sha}:{a}")
                fm = dict(re.findall(r"(?m)^(\w+):\s*(.*)$", t.split("---")[1])) if t.startswith("---") and t.count("---") >= 2 else {}
                tid = f"tarefa:{Path(a).stem}"
                ents.append({"tipo": "tarefa", "chave": Path(a).stem, "nome": f"tarefa {Path(a).stem}", "alias": [Path(a).stem],
                             "desc": f"Tarefa do Claude da VPS {Path(a).stem}: {fm.get('descricao', '')} (quando: {fm.get('quando', '?')}, modelo {fm.get('modelo', '?')}).",
                             "caminho": f"{nome}/{a}"})
                rels.append((tid, "depende_de", rid))
                for rp in re.split(r"[,\[\]\s]+", fm.get("repos", "")):
                    if "/" in rp:
                        rels.append((tid, "depende_de", f"repo:{rp.split('/')[-1]}"))
                peds.append((tid, "prompt", t.split("---", 2)[-1][:2000]))
    return G.unidade(f"repo:{nome}", ents, rels, peds, versao=sha)


def repos(pasta=None, buscar=True):
    """Uma unidade por clone em ~/repos, com o hash do commit da base: sem commit novo, nada é relido."""
    pasta = Path(pasta or os.path.expanduser(config.get("caminhos.repos")))
    out = []
    for r in sorted(pasta.iterdir()) if pasta.is_dir() else []:
        if not (r / ".git").exists():
            continue
        if buscar:
            _git(r, "fetch", "-q", "origin", timeout=90)
        base = _base(r)
        sha = (_git(r, "rev-parse", base) or "").strip() if base else ""
        if not sha:
            continue
        url = (_git(r, "remote", "get-url", "origin") or "").strip()
        m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
        out.append(_repo(r, r.name, base, sha, m.group(1) if m else ""))
    return out


# ---- memória ----

RE_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def _frontmatter(texto):
    m = RE_FM.match(texto)
    if not m:
        return {}, texto
    fm = {}
    for l in m.group(1).splitlines():
        if ":" in l and not l.startswith(" "):
            k, v = l.split(":", 1)
            fm[k.strip()] = v.strip().strip("\"'")
        elif ":" in l:
            k, v = l.strip().split(":", 1)
            fm.setdefault(k.strip(), v.strip().strip("\"'"))
    return fm, m.group(2)


# O claude não lista /home/usuario (só --x), mas lê /home/usuario/.claude: o glob tem de partir dali, não de /home/usuario/.claude*.
ORQ_CLAUDE = Path("/home/usuario/.claude")


def memoria(raizes=None):
    """Memórias das contas claude e do orch (~/.claude*/projects/*/memory/*.md, inclusive as cópias do PC por projeto):
    uma entidade por memória; feedback e project viram decisão do dono."""
    if raizes is None:
        raizes = [*home_agentes().glob(".claude*/projects/*/memory"), *ORQ_CLAUDE.glob("projects/*/memory")]
    melhor, copias = {}, {}
    for pasta in raizes:
        try:
            arqs = sorted(Path(pasta).glob("*.md"))
        except OSError:
            continue
        for a in arqs:
            if a.name == "MEMORY.md":
                continue
            try:
                m = a.stat().st_mtime
            except OSError:
                continue
            texto = _ler(a, 20_000)
            copias.setdefault(a.stem, []).append({"origem": str(a), "hash": G._h(G.limpar(texto))})
            if a.stem not in melhor or m > melhor[a.stem][1]:
                melhor[a.stem] = (a, m)
    ents, rels, peds = [], [], []
    for nome, (a, m) in sorted(melhor.items()):
        fm, corpo = _frontmatter(_ler(a, 20_000))
        tipo = fm.get("type") or "memoria"
        desc = fm.get("description") or " ".join(corpo.split())[:200]
        dia = (fm.get("modified") or "")[:10] or _data_iso(m)
        mid = f"memoria:{nome}"
        projeto = a.parent.parent.name  # -home-orch-repos-clienteh, -home-claude...
        dono_mem = "do orquestrador (orch)" if a.is_relative_to(ORQ_CLAUDE) else "das contas claude"
        repo = re.sub(r"^-home-\w+-repos-", "", projeto) if "-repos-" in projeto else ""
        ents.append({"tipo": "memoria", "chave": nome, "nome": fm.get("name") or nome, "alias": [nome],
                     "desc": f"Memória {dono_mem} ({tipo}{', projeto ' + repo if repo else ''}): {desc}", "caminho": str(a), "data": dia, "extra": {"copias": copias[nome]}})
        peds.append((mid, "memoria", f"{desc}\n{corpo}"))
        if repo:
            rels.append((mid, "pertence", f"repo:{repo}"))
        if tipo in ("feedback", "project"):
            did = f"decisao:{nome}"
            quem = config.dono(True) if tipo == "feedback" else "orquestrador (estado do projeto)"
            ents.append({"tipo": "decisao", "chave": nome, "nome": f"decisão: {fm.get('name') or nome}", "alias": [nome], "data": dia,
                         "desc": f"{'Decisão do ' + config.dono() if tipo == 'feedback' else 'Estado/decisão de projeto'} em {dia} ({quem}): {desc}",
                         "caminho": str(a)})
            rels.append((did, "decidido_em", mid))
            for b in set(RE_ISSUE.findall(corpo + desc)):
                rels.append((did, "decidido_em", f"issue:{b}"))
            for velha in re.findall(r"(?i)substitui\w*\s+(?:a\s+|o\s+)?(KRN-\d+)", corpo):
                rels.append((did, "substitui", f"issue:{velha}"))
    return [G.unidade("memoria:todas", ents, rels, peds)]


def orch(raiz=None, claude_md=None):
    """Instruções do orquestrador (/home/usuario/.claude/CLAUDE.md) e os LEIA-ME do PC (cópia do root em grafo-raiz/docs) como documentos."""
    raiz = Path(raiz or raiz_dir())
    fontes = [(Path(claude_md or ORQ_CLAUDE / "CLAUDE.md"), "orch/CLAUDE.md", "Instruções do orquestrador")]
    for f in sorted((raiz / "docs").glob("*")):
        fontes.append((f, None, f"LEIA-ME do PC do {config.dono()}"))
    ents, rels, peds = [], [], []
    for arq, chave, rotulo in fontes:
        texto = _ler(arq, MAX_CHARS_DOC * 3)
        if not texto.strip():
            continue
        m = re.match(r"<!-- origem: (\S+) -->\n", texto)
        origem = m.group(1) if m else str(arq)
        texto = texto[m.end():] if m else texto
        chave = chave or "pc/" + origem.removeprefix("/home/usuario/pc/")
        did = f"documento:{chave}"
        titulo = next(iter(re.findall(r"(?m)^#\s+(.+)$", texto)[:1]), chave)
        ents.append({"tipo": "documento", "chave": chave, "nome": chave, "alias": [titulo], "caminho": origem,
                     "desc": f"{rotulo} ({origem}): {titulo}."})
        rels.append((did, "roda_em", f"vps:{servidor_hub()}"))
        n = 0
        for sec, corpo in G.secoes(texto):
            for p in G.quebrar(corpo):
                if n >= MAX_PEDACOS_DOC * 4:
                    break
                peds.append((did, sec or "abertura", f"{sec}\n{p}" if sec else p))
                n += 1
    return [G.unidade("orch:docs", ents, rels, peds)]


# ---- receitas ----

def receitas():
    from hub import biblioteca as B
    ents, rels, peds = [], [], []
    for x in B.receitas():
        rid = f"receita:{x['caminho']}"
        ents.append({"tipo": "receita", "chave": x["caminho"], "nome": x["titulo"], "alias": [x["caminho"]], "data": x.get("data") or "",
                     "desc": f"Receita da biblioteca ({x['tema']}, {x.get('status') or '—'}): {x.get('resumo') or ''}",
                     "caminho": x["caminho"], "link": f"https://github.com/{B.REPO}/blob/main/{x['caminho']}"})
        peds.append((rid, "resumo", f"{x['titulo']}\n{x.get('resumo') or ''}"))
        m = B.RE_ONDE.search(x["texto"])
        if m:
            peds.append((rid, "onde_aplicar", "Onde aplicar: " + m.group(1)))
        rels += [(rid, "aplica_em", f"projeto:{p}") for p in x["projetos"]]
    return [G.unidade("receitas:todas", ents, rels, peds)]


# ---- referências conferidas do moedor: docs/referencias/<tema>.jsonl de cada repo, tipo `referencia` ----
MAX_REFS_ENTIDADE = 3000


def referencias(repos=None):
    """Uma entidade por (repo, tema); cada referência conferida (título, autor, ano, trecho literal, link) é um pedaço buscável."""
    try:
        from hub import busca as K
    except ImportError as _e:  # a busca é do repo privado: fora do snapshot público
        from hub import faltou
        if not faltou(_e, "hub.busca"):
            raise
        return []
    ents, rels, peds = [], [], []
    por = {}
    for d in K.referencias(repos):
        por.setdefault(d["caminho"], []).append(d)
    for caminho, docs in sorted(por.items()):
        tema = Path(caminho).stem
        rid = f"referencia:{caminho}"
        ents.append({"tipo": "referencia", "chave": caminho, "nome": f"Referências conferidas: {tema} ({caminho.split('/')[0]})", "alias": [tema, caminho],
                     "data": max(d["data"] for d in docs), "caminho": caminho,
                     "desc": f"{len(docs)} referências com trecho literal conferido na fonte (moedor, KRN-1135), tema {tema}."})
        for d in docs[:MAX_REFS_ENTIDADE]:
            peds.append((rid, "trecho", f"{d['pedacos'][0][1]}\nFonte: {d['url']}"))
        for p in filter(None, docs[0]["projeto"].split(",")):
            rels.append((rid, "aplica_em", f"projeto:{p}"))
    return [G.unidade("referencias:todas", ents, rels, peds)]


# ---- wiki compilada: páginas de estado atual, tipo `pagina` ----

def wiki():
    try:
        import hub.wiki as W
    except ImportError as _e:  # a wiki é do repo privado: fora do snapshot público
        from hub import faltou
        if not faltou(_e, "hub.wiki"):
            raise
        return []
    return W.coletor()


# ---- relatórios do hub (só metadados) ----

LEGENDA = {"leitura": "leitura diária dos bancos dos apps (dado de cliente/paciente: NÃO indexado, só a pasta)", "propostas": "propostas do hub (faz N)",
           "rotinas": "falhas das rotinas do cron (falhas.jsonl)", "claude": "uso das contas Claude, filas e anexos", "dataforseo": "custos do DataForSEO",
           "gsc": "oportunidades do Search Console", "seomator": "auditorias SEOmator", "ranking": "ranking de palavras", "aio": "AI Overview",
           "biblioteca": "clone do repo biblioteca (receitas)", "mcp": "estado das ferramentas MCP", "orch": "estado do orquestrador",
           "bi": "BI dos negócios (dado de negócio: NÃO indexado)", "asite01-prospeccao-whatsapp": "prospecção da asite01 no WhatsApp (dado de cliente: NÃO indexado)",
           "alertas": "alertas dos apps", "concorrentes": "monitor de concorrentes", "instagram": "dados do Instagram", "snapshots": "retratos do sistema"}


def relatorios(pasta=None):
    pasta = Path(pasta or data())
    ents, rels = [], []
    try:
        pastas = sorted(p for p in pasta.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    for p in pastas:
        try:
            arqs = [f for f in p.iterdir()]
            novo = max((f.stat().st_mtime for f in arqs), default=p.stat().st_mtime)
        except OSError:
            continue
        ents.append({"tipo": "relatorio", "chave": p.name, "nome": f"relatórios/{p.name}", "alias": [p.name],
                     "desc": f"Pasta {p.name} em {pasta} ({len(arqs)} itens, mais recente {_data_iso(novo)}): {LEGENDA.get(p.name, 'relatórios e estado de rotina do hub')}.",
                     "caminho": str(p)})
        rels.append((f"relatorio:{p.name}", "roda_em", _vps_local()))
    return [G.unidade("relatorios:todos", ents, rels)]


# ---- segredos: só nomes ----

def _nomes_do_arquivo(p):
    """Nomes de variável de um .env/.key: só o texto antes do "=". Arquivo sem '=' (chave solta) não tem nome para listar."""
    out = []
    for linha in _ler(p).splitlines():
        m = RE_VAR.match(linha)
        if m:
            out.append(m.group(1))
    return out


# pedaço do caminho → projeto (para "a credencial do Supabase da fsite06" achar /root/banco-fsite06/segredos.env)
PROJETO_DO_CAMINHO = [(r"fsite06", "fsite06", "Cliente A"), (r"asite01", "asite01", "Cliente C"), (r"clienteh|clientej", "clientej", "ClienteJ"),
                      (r"clientei", "clientei", "ClienteI Pessoa Teixeira"), (r"psite15", "psite15", "Cliente G"), (r"clientee", "clientee", "ClienteE & Moraes")]


def _segredo(nome, caminho, vps, onde):
    """Entidade de segredo (só o nome) + relações com a VPS e o projeto deduzido do caminho."""
    sid = f"segredo:{nome}@{caminho}"
    proj = [(pid, rot) for rx, pid, rot in PROJETO_DO_CAMINHO if re.search(rx, caminho, re.I)]
    palavras = " ".join(nome.lower().replace("-", "_").split("_"))
    desc = (f"Segredo (credencial/chave) {nome} ({palavras}), guardado no arquivo {caminho} {onde}"
            + (f"; projeto {', '.join(r for _, r in proj)}" if proj else "") + ". Só o nome; o valor não está no grafo.")
    e = {"tipo": "segredo", "chave": f"{nome}@{caminho}", "nome": nome, "caminho": caminho, "desc": desc}
    return e, [(sid, "roda_em", f"vps:{vps}")] + [(sid, "pertence", f"projeto:{pid}") for pid, _ in proj]


def segredos(pastas=None, home=None, raiz=None):
    """Nomes de segredo: arquivos que o claude lê (pasta de segredos do hub, ~/.config) + o que o root reduziu a nomes (grafo-raiz:
    ~/pc/segredos, /root/*/*.env, /opt/*/.env, .env dos repos e das outras VPS)."""
    ents, rels = [], []
    home = Path(home or home_agentes())
    raiz = Path(raiz or raiz_dir())
    hub = servidor_hub()
    pastas = pastas if pastas is not None else [Path(config.get("caminhos.segredos")), Path(config.get("caminhos.segredos")) / "apps", home / ".config"]
    for pasta in pastas:
        try:
            arqs = sorted(f for f in Path(pasta).iterdir() if f.is_file() and not re.search(r"\.(bak|antes)|acme", f.name))
        except OSError:
            continue
        for f in arqs:
            nomes = _nomes_do_arquivo(f) if f.suffix in (".env", "") or ".env" in f.name else []
            if not nomes:  # chave solta num arquivo (ex.: openrouter.key): o próprio arquivo é o segredo
                nomes = [f.name] if f.suffix in (".key", ".txt") or "key" in f.name.lower() else []
            for n in nomes:
                e, r = _segredo(n, str(f), hub, "na VPS do hub")
                e["extra"] = {}
                ents.append(e)
                rels += r
    listas = [(x, hub) for x in _json(raiz / "segredos.json", [])]
    for arq in sorted(raiz.glob("remoto-*.json")):
        d = _json(arq, {})
        listas += [(x, d.get("vps") or arq.stem.removeprefix("remoto-")) for x in d.get("env") or []]
    for x, vps in listas:
        caminho = str(x.get("caminho") or "")
        for n in x.get("nomes") or []:
            if caminho and re.fullmatch(r"[\w.@+-]{1,120}", n):
                e, r = _segredo(n, caminho if vps == hub else f"{vps}:{caminho}", vps, "na VPS do hub" if vps == hub else f"na {vps}")
                h = (x.get("hmacs") or {}).get(n)
                e["extra"] = {"valor_hash": h, "lote_hmac": x.get("lote_hmac")} if x.get("lote_hmac") and isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h) else {}
                ents.append(e)
                rels += r
    return [G.unidade("segredos:arquivos", ents, rels)]


# ---- docker / Dokploy (diário) ----

# contêineres de teste de sessão: ens-<nome>-<6 dígitos> (postgres descartável) e o paradedb local de nome <hash 32>_..._<6 hex>
RE_EFEMERO = re.compile(r"^(krn\d+-|clienteh-ci-|.*-ci-|ens-.+-\d{6}$|[0-9a-f]{32}_)")


def dokploy_fontes():
    """Read only source coordinates, never application env or credentials.

    The local database is queried with a read-only transaction. A failed read
    preserves the last successful independent collection unit.
    """
    containers = _docker('ps', '--filter', 'name=dokploy-postgres.', '--format', '{{.Names}}')
    nomes = (containers or '').splitlines()
    if len(nomes) != 1:
        raise RuntimeError('Inventário Dokploy indisponível: esperado um banco local.')
    sql = '''BEGIN READ ONLY;
SELECT json_build_object('app', "appName", 'repo', repository, 'owner', owner,
                         'fonte', "sourceType") FROM application;
SELECT json_build_object('app', "appName", 'repo', repository, 'owner', owner,
                         'fonte', "sourceType", 'composto', true) FROM compose;
COMMIT;'''
    saida = _docker('exec', nomes[0], 'psql', '-U', 'dokploy', '-d', 'dokploy', '-Atc', sql)
    if saida is None:
        raise RuntimeError('Leitura das coordenadas de origem do Dokploy falhou.')
    ents, rels = [], []
    # Only reuse legacy short IDs for a verified owner/repository.
    conhecidos = {s['repo']['github']: s['repo']['github'].split('/')[-1] for s in S.carregar()}
    for pasta in sorted((Path.home() / 'repos').glob('*')):
        if pasta.is_dir():
            remoto = (_git(pasta, 'remote', 'get-url', 'origin') or '').strip()
            m = re.fullmatch(r'(?:https://github.com/|git@github.com:)([\w.-]+/[\w.-]+?)(?:\.git)?', remoto)
            if m:
                conhecidos[m[1]] = pasta.name
    for linha in saida.splitlines():
        if not linha.startswith('{'):
            continue
        a = json.loads(linha)
        app, repo, owner = a.get('app'), a.get('repo'), a.get('owner')
        if not app or not re.fullmatch(r'[\w.-]+', app):
            continue
        alvos = [('app', app)]
        if a.get('composto'):
            # compose: o appName é o projeto; os contêineres (app-1, db-1...) levam o rótulo com o nome dele
            alvos = []
            for c in (_docker('ps', '--filter', f'label=com.docker.compose.project={app}', '--format', '{{.Names}}|{{.Image}}') or '').splitlines():
                nome, _, imagem = c.partition('|')
                if nome and not RE_EFEMERO.match(nome):
                    alvos.append(('banco' if re.search(r'postgres|paradedb|mysql|mariadb|redis', imagem) else 'app', nome))
        for tipo, nome in alvos:
            ents.append({'tipo': tipo, 'chave': nome, 'nome': nome,
                         'extra': {'fonte_deploy': a.get('fonte')},
                         'desc': 'Aplicação cadastrada no Dokploy; fonte verificada no inventário de deploy'
                                 + (f" (código em {owner}/{repo}, fonte {a.get('fonte')})." if repo and owner else '.')})
        if a.get('fonte') == 'github' and all(re.fullmatch(r'[\w.-]+', x or '') for x in (repo, owner)):
            chave = conhecidos.get(f'{owner}/{repo}', f'{owner}/{repo}')
            ents.append({'tipo': 'repo', 'chave': chave, 'nome': f'{owner}/{repo}',
                         'link': f'https://github.com/{owner}/{repo}'})
            rels.extend((f'repo:{chave}', 'roda_em', f'{tipo}:{nome}') for tipo, nome in alvos)
        else:
            # sem fonte git (imagem pronta): o dono vem só do que o cerebro.json declara, com a evidência escrita lá
            for d in _cerebro().get('dokploy_declarado', []):
                if d.get('repo') and re.fullmatch(re.escape(d['app']) + r'(?:-[a-z0-9]+)*', app):
                    rels.extend((d['repo'], 'roda_em', f'{tipo}:{nome}') for tipo, nome in alvos)
    return [G.unidade('dokploy:fontes', ents, rels)]


def _docker(*args):
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def docker(traefik_dir="/etc/dokploy/traefik/dynamic"):
    ents, rels = [], []
    vps = _vps_local()
    # as VPS (ficha e apelidos) vêm de infra(); aqui só as relações roda_em com a VPS do hub
    nomes_seg = {}
    servicos = _docker("service", "ls", "--format", "{{.Name}}|{{.Image}}|{{.Replicas}}|{{.Ports}}")
    if servicos is None:
        return []  # Docker falhou: sem unidade nova, a coleta anterior (docker:todos) continua valendo
    for linha in servicos.splitlines():
        nome, imagem, repl, portas = (linha.split("|") + ["", "", ""])[:4]
        if RE_EFEMERO.match(nome):
            continue
        tipo = "banco" if re.search(r"postgres|paradedb|mysql|mariadb|redis", imagem) else "app"
        ents.append({"tipo": tipo, "chave": nome, "nome": nome, "desc": f"{'Banco' if tipo == 'banco' else 'App'} do Dokploy/Swarm {nome} "
                     f"(imagem {imagem}, réplicas {repl}{', portas ' + portas if portas else ''}).", "extra": {"imagem": imagem}})
        rels.append((f"{tipo}:{nome}", "roda_em", vps))
        nomes_seg[nome] = tipo
    for linha in (_docker("ps", "--format", "{{.Names}}|{{.Image}}|{{.Ports}}") or "").splitlines():
        nome, imagem, portas = (linha.split("|") + ["", ""])[:3]
        if RE_EFEMERO.match(nome) or re.search(r"\.\d+\.\w{20,}$", nome) or nome in nomes_seg:
            continue
        tipo = "banco" if re.search(r"postgres|paradedb|mysql|redis", imagem) else "app"
        ents.append({"tipo": tipo, "chave": nome, "nome": nome, "desc": f"Contêiner {nome} fora do Swarm (imagem {imagem}{', portas ' + portas if portas else ''})."})
        rels.append((f"{tipo}:{nome}", "roda_em", vps))
    for dom, alvos in traefik(traefik_dir).items():
        ents.append({"tipo": "dominio", "chave": dom, "nome": dom, "desc": f"Domínio {dom} roteado pelo Traefik do Dokploy."})
        for app, porta in alvos:
            rels.append((f"app:{app}", "serve", f"dominio:{dom}"))
    # nomes das variáveis de ambiente de cada serviço (nunca os valores: o docker inspect é lido em memória e só a chave fica)
    for nome in nomes_seg:
        saida = _docker("service", "inspect", "--format", "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}", nome)
        for linha in (saida or "").splitlines():
            var = linha.split("=", 1)[0].strip()
            if var and RE_SEGREDOISH.search(var) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
                sid = f"segredo:{var}@{nome}"
                ents.append({"tipo": "segredo", "chave": f"{var}@{nome}", "nome": var,
                             "desc": f"Variável de ambiente (credencial/chave) {var} configurada no app {nome} do Dokploy. Só o nome; o valor está no ambiente do serviço."})
                rels.append((f"{nomes_seg[nome]}:{nome}", "usa", sid))
    return [G.unidade("docker:todos", ents, rels)]


# ---- Linear ----

Q_ISSUES = """query($depois: String, $desde: DateTimeOrDuration) { issues(first: 40, after: $depois, orderBy: updatedAt,
  filter: {updatedAt: {gt: $desde}}) { nodes { id identifier title description url createdAt updatedAt state { name type } labels { nodes { name } }
  project { name } comments(first: 50) { nodes { id body createdAt user { name app } } pageInfo { hasNextPage endCursor } } } pageInfo { hasNextPage endCursor } } }"""

Q_COMENTARIOS = """query($id: String!, $depois: String) { issue(id: $id) { comments(first: 100, after: $depois) {
  nodes { id body createdAt user { name app } } pageInfo { hasNextPage endCursor } } } }"""


def _mais_comentarios(i, consulta):
    """Completa os comentários da issue além da 1ª página (as decisões costumam estar nos últimos)."""
    pg = i["comments"].get("pageInfo") or {}
    while pg.get("hasNextPage") and i.get("id"):
        d = consulta(Q_COMENTARIOS, id=i["id"], depois=pg["endCursor"])["issue"]["comments"]
        i["comments"]["nodes"] += d["nodes"]
        pg = d["pageInfo"]


def linear_issue(i, projetos):
    ident = i["identifier"]
    iid = f"issue:{ident}"
    desc = i.get("description") or ""
    ents = [{"tipo": "issue", "chave": ident, "nome": ident, "alias": [i["title"]], "data": (i.get("createdAt") or "")[:10], "link": i["url"], "extra": {"estado_tipo": (i.get("state") or {}).get("type")},
             "desc": f"Issue {ident}: {i['title']} (estado {(i.get('state') or {}).get('name')}; labels {', '.join(l['name'] for l in i['labels']['nodes'])}; "
                     f"projeto {(i.get('project') or {}).get('name')})."}]
    rels, peds = [], [(iid, "descricao", f"{ident} {i['title']}\n{desc[:3000]}")]
    pn = (i.get("project") or {}).get("name") or ""
    if pn in projetos:
        rels.append((iid, "pertence", projetos[pn]))
    textos = [desc]
    for c in i["comments"]["nodes"]:
        corpo = c.get("body") or ""
        textos.append(corpo)
        quem = (c.get("user") or {}).get("name") or ""
        humano = bool(quem) and not (c.get("user") or {}).get("app")
        if RE_DECISAO.search(corpo) and ("Orquestrador" in corpo or config.dono(True) in corpo or humano):
            m = RE_DECISAO.search(corpo)
            trecho = " ".join(corpo[max(0, m.start() - 200):m.end() + 400].split())
            did = f"decisao:linear-{c['id'][:8]}"
            ents.append({"tipo": "decisao", "chave": f"linear-{c['id'][:8]}", "nome": f"decisão em {ident}: {i['title'][:70]}", "data": (c.get("createdAt") or "")[:10],
                         "link": i["url"], "desc": f"Decisão em {(c.get('createdAt') or '')[:10]} ({quem or 'orquestrador'}) na issue {ident}: {trecho}"})
            rels.append((did, "decidido_em", iid))
            if pn in projetos:
                rels.append((did, "pertence", projetos[pn]))
        elif humano and len(corpo) > 40:
            peds.append((iid, "comentario", f"{quem}: {corpo[:1200]}"))
    tudo = "\n".join(textos)
    for outra in set(RE_ISSUE.findall(tudo)) - {ident}:
        if re.search(r"(?i)substitui\w*[^.\n]{0,30}" + outra, tudo):
            rels.append((iid, "substitui", f"issue:{outra}"))
    for org, rp, n in set(RE_PR.findall(tudo)):
        pid = f"pr:{rp}#{n}"
        ents.append({"tipo": "pr", "chave": f"{rp}#{n}", "nome": f"PR {rp}#{n}", "link": f"https://github.com/{org}/{rp}/pull/{n}",
                     "desc": f"Pull request {n} do repo {rp} ({org})."})
        rels += [(iid, "implementado_em", pid), (pid, "depende_de", f"repo:{rp}")]
    return G.unidade(f"linear:{ident}", ents, rels, peds)


def linear(consulta=None, completo=False, db=None):
    """Issues alteradas desde o último corte (meta linear_corte); a 1ª vez traz todas. Devolve (unidades, corte novo)."""
    from hub import linear as L
    consulta = consulta or (lambda q, **v: L.gql(q, **v))
    desde = None if completo or G.meta("linear_schema", db=db) != "estado-v1" else G.meta("linear_corte", db=db)
    projetos = {s["nome"]: f"projeto:{s['id']}" for s in S.carregar()}
    out, depois, corte = [], None, desde or ""
    while True:
        d = consulta(Q_ISSUES, depois=depois, desde=desde or "1970-01-01T00:00:00Z")["issues"]
        for i in d["nodes"]:
            _mais_comentarios(i, consulta)
            out.append(linear_issue(i, projetos))
            corte = max(corte, i["updatedAt"])
        if not d["pageInfo"]["hasNextPage"]:
            break
        depois = d["pageInfo"]["endCursor"]
    return out, corte


# ---- orquestra ----

FONTES = {"sites": sites, "pessoas": pessoas, "infra": infra, "repos": repos, "memoria": memoria, "orch": orch, "receitas": receitas,
          "referencias": referencias, "wiki": wiki, "segredos": segredos, "relatorios": relatorios, "docker": docker, "dokploy_fontes": dokploy_fontes}
PREFIXO = {"sites": "sites:", "pessoas": "pessoas:", "infra": "infra:", "repos": "repo:", "memoria": "memoria:", "orch": "orch:", "receitas": "receitas:",
           "referencias": "referencias:", "wiki": "pagina:", "segredos": "segredos:", "relatorios": "relatorios:", "docker": "docker:", "dokploy_fontes": "dokploy:"}
GRUPOS = {"rapido": ["sites", "pessoas", "infra", "repos", "memoria", "orch", "receitas", "referencias", "wiki", "segredos", "relatorios", "linear"],
          "diario": ["dokploy_fontes", "docker", "infra", "segredos"],
          "tudo": ["sites", "pessoas", "infra", "repos", "memoria", "orch", "receitas", "referencias", "wiki", "segredos", "relatorios", "dokploy_fontes", "docker", "linear"]}


def atualizar(alvos, db=None, embed=G.embeddar, log=print):
    """Roda os grupos/fontes pedidos, remonta o grafo se algo mudou e completa os vetores. Uma rodada por vez (flock)."""
    fontes = []
    for a in alvos:
        fontes += GRUPOS.get(a, [a])
    pasta = Path(db or G.caminho_db()).parent
    pasta.mkdir(parents=True, exist_ok=True)
    trava = open(pasta / "grafo.lock", "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"ok": True, "pulou": "outra rodada em andamento"}
    mudou, falhas = [], {}
    for f in dict.fromkeys(fontes):
        try:
            if f == "linear":
                us, corte = linear(db=db, completo="linear-completo" in alvos)
                if us and G.aplicar("linear:", us, db=db, apagar=False):
                    mudou.append(f)
                if corte:
                    G.meta("linear_corte", corte, db=db)
                    G.meta("linear_schema", "estado-v1", db=db)
            elif f in FONTES:
                if G.aplicar(PREFIXO[f], FONTES[f](), db=db):
                    mudou.append(f)
            else:
                falhas[f] = "fonte desconhecida"
        except Exception as ex:  # noqa: BLE001  uma fonte fora do ar não derruba as outras
            falhas[f] = f"{type(ex).__name__}: {str(ex)[:160]}"
            log(f"aviso: {f}: {falhas[f]}")
    r = {"ok": not falhas, "mudou": mudou, "falhas": falhas}
    c = G._rw(Path(db or G.caminho_db()))
    vazio = c.execute("select count(*) from entidade").fetchone()[0] == 0
    c.close()
    if mudou or vazio:
        r["grafo"] = G.remontar(db=db)
    r["vetores"] = G.completar_vetores(db=db, embed=embed, log=log)
    return r
