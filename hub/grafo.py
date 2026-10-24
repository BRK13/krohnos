"""Segundo cérebro do orquestrador: grafo de entidades e relações + índice híbrido (BM25 + embeddings por RRF,
o mesmo modelo da biblioteca) de tudo que temos: repos, memória, Linear, VPS, Dokploy, domínios, crons, segredos
(só o NOME), decisões do dono, receitas. SQLite + numpy em $HUB_DATA/grafo/grafo.db (sem WAL: o serviço do MCP lê em modo só leitura).

Como cresce: cada coletor (hub/grafo_coleta.py) devolve "unidades" {origem, hash, entidades, relacoes, pedacos}. A unidade com
o mesmo hash não é tocada; o grafo e o índice são remontados a partir das unidades (milhares de linhas, é rápido) e só o texto
novo ganha embedding (cache por hash do texto). Nenhum valor de segredo entra: `limpar` tira o que parece chave de todo texto
e os coletores guardam só nome de variável. Dado de cliente (bancos, leitura/) nunca é lido: só metadados de onde está.

    python3 -m hub.grafo atualizar [grupo|fonte ...]   rapido (padrão, cron de 10 min) | diario | fontes soltas
    python3 -m hub.grafo buscar <pergunta>             resposta curta + fontes
    python3 -m hub.grafo vizinhos <entidade>           relações de uma entidade
    python3 -m hub.grafo gabarito                      acerto@3 contra tests/grafo_gabarito.json
    python3 -m hub.grafo instrucoes                    bloco "cerebro" do deploy/orquestrador/CLAUDE.md nas contas
"""
import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

from hub import data

MODELO = "openai/text-embedding-3-small"
DIM = 768
LOTE = 64
PEDACO = 1500
RRF_K = 60
BM25_K1, BM25_B = 1.5, 0.75
STOP = set("""a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sob sobre e ou que se ao aos à às é
ser foi são como mais menos mas já não nao sim qual quais onde quem quando the of and or to in for on with is are be an at by from""".split())
RE_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*[a-z0-9]|[a-z0-9]")
TIPOS = ("projeto", "repo", "dominio", "app", "vps", "banco", "cron", "servico", "segredo", "pessoa", "decisao", "issue", "pr",
         "receita", "referencia", "documento", "memoria", "relatorio", "tarefa", "pagina")


# a página da wiki compilada (estado atual, com fonte e data) é a resposta principal: peso máximo; os trechos soltos complementam.
# O orquestrador pergunta mais por infraestrutura do que por texto de documento: a ficha de repo, domínio, app, cron... vale mais que
# um pedaço de doc com as mesmas palavras. E a palavra da pergunta que nomeia o tipo ("repo", "cron", "chave"...) puxa aquele tipo.
PESO_TIPO = {"pagina": 2.5, "projeto": 1.3, "repo": 1.3, "dominio": 1.3, "app": 1.3, "banco": 1.3, "vps": 1.3, "cron": 1.3, "servico": 1.3, "tarefa": 1.2,
             "pessoa": 1.2, "decisao": 1.15, "documento": 0.85, "issue": 0.9, "pr": 0.8}
INTENCAO = [(re.compile(r"\b(repo|repositorio|codigo)\b"), ("repo",)), (re.compile(r"\bdominio|\burl\b|\bsite\b"), ("dominio",)),
            (re.compile(r"\bapp\b|\bdokploy\b|\bcontainer|\bdeploy"), ("app",)), (re.compile(r"\bcron\b|\bagendad|\bcada \d+ min|\brotina"), ("cron",)),
            (re.compile(r"\bservico\b|\bsystemd\b|\bporta\b"), ("servico",)), (re.compile(r"\bbanco\b|\bpostgres"), ("banco",)),
            (re.compile(r"\bvps\b|\bservidor"), ("vps",)), (re.compile(r"credencial|\bchave|\btoken|\bsegredo|\bvariave|\bsenha|\benv\b"), ("segredo",)),
            (re.compile(r"decidiu|decisao|\bregra\b|\bvetou|combinado"), ("decisao", "memoria")), (re.compile(r"\bissue\b|\bkrn-?\d+"), ("issue",)),
            (re.compile(r"\breceita|biblioteca"), ("receita",)), (re.compile(r"\breferencia|\bfonte conferida|\bcitacao"), ("referencia",)), (re.compile(r"\btarefa"), ("tarefa", "cron")), (re.compile(r"instruc|claude\.md|diretriz"), ("documento",)), (re.compile(r"\bquem\b|\bdono\b"), ("pessoa",))]
BOOST_INTENCAO = 1.6
BOOST_INTENCAO_2 = 1.25
SEM_MARCA = 0.7
NOME_NA_PERGUNTA = 2.5  # a pergunta traz o nome inteiro (2+ palavras) da entidade: "reel" + "fsite06" pede o cron reels-fsite06
BONUS_VIZINHO = 0.4  # fração da nota de cada um dos 3 primeiros que passa ao vizinho (relação forte) do tipo pedido


def intencao(pergunta):
    """{tipo: reforço}. O tipo nomeado primeiro leva o reforço cheio ("qual REPO serve o DOMÍNIO x" quer o repo); os demais, o menor."""
    q = sem_acento(pergunta)
    achados = sorted((m.start(), tipos) for rx, tipos in INTENCAO for m in [rx.search(q)] if m)
    out = {}
    for pos, tipos in achados:
        for t in tipos:
            out.setdefault(t, BOOST_INTENCAO if not out else BOOST_INTENCAO_2)
    return out


def caminho_db():
    return Path(os.environ.get("GRAFO_DB") or data() / "grafo" / "grafo.db")


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


SINONIMO = {"wpp": "whatsapp", "zap": "whatsapp", "whats": "whatsapp"}


def _singular(t):
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def tokens(texto):
    out = []
    for t in RE_TOKEN.findall(sem_acento(texto)):
        if t in STOP:
            continue
        t = SINONIMO.get(_singular(t), _singular(t))
        out.append(t)
        if re.search(r"[._-]", t):  # "cliente-c", "SUPABASE_URL": também as partes, para "supabase" achar o nome ("reels-fsite06" acha "reel")
            out += [_singular(p) for p in re.split(r"[._-]+", t) if len(p) > 2 and p not in STOP]
    return out


# ---- segurança: nenhum valor de segredo, nenhum dado pessoal ----

_NOME_SEGREDO = r"[A-Za-z0-9_.-]*(?:key|token|secret|senha|password|passwd|pwd|credential|apikey)[A-Za-z0-9_.-]*"
PADROES_SEGREDO = [
    re.compile(r"(?i)\b(?:sk|pk|rk|lin_api|lin_oauth|re)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]{3,}(?=@)"),                    # postgres://user:senha@host
    re.compile(r"\b[0-9a-f]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}(?=\s|$)"),
    re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),                           # CPF
]


RE_NOME_VALOR = re.compile(r"(?i)(\b" + _NOME_SEGREDO + r"['\"]?\s*[=:]\s*)(['\"]?)([^\s'\"`$<{(\[]{8,})")


def limpar(texto):
    """Troca por [oculto] o que parece chave, token, senha em URL, CPF; e o valor que vem depois de NOME_DA_CHAVE= / NOME: ."""
    texto = RE_NOME_VALOR.sub(lambda m: m.group(1) + m.group(2) + "[oculto]", texto or "")
    for p in PADROES_SEGREDO:
        texto = p.sub("[oculto]", texto)
    return texto


# ---- banco ----

def _ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _rw(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=60)
    c.row_factory = sqlite3.Row
    c.executescript("""
        create table if not exists unidade(origem text primary key, hash text not null, payload text not null);
        create table if not exists meta(chave text primary key, valor text);
        create table if not exists entidade(id text primary key, tipo text, nome text, descricao text, caminho text, link text,
            data text, alias text, extra text);
        create table if not exists relacao(de text, rel text, para text, primary key(de, rel, para));
        create index if not exists relacao_para on relacao(para);
        create table if not exists pedaco(id integer primary key, ent text, secao text, texto text, h text);
        create index if not exists pedaco_ent on pedaco(ent);
        create table if not exists vec(h text primary key, vec blob);""")
    return c


def _h(*partes):
    return hashlib.sha1("\0".join(partes).encode()).hexdigest()


def quebrar(corpo, n=PEDACO):
    """Parte um texto longo em pedaços por parágrafo."""
    corpo = (corpo or "").strip()
    if len(corpo) <= n:
        return [corpo] if corpo else []
    out, atual = [], ""
    for par in re.split(r"\n\s*\n", corpo):
        if atual and len(atual) + len(par) > n:
            out.append(atual)
            atual = ""
        while len(par) > n:
            out.append(par[:n])
            par = par[n:]
        atual = f"{atual}\n\n{par}" if atual else par
    return out + ([atual] if atual else [])


def secoes(texto):
    """[(titulo, corpo)] de um Markdown pelos cabeçalhos # a ###, ignorando '#' dentro de bloco de código."""
    partes, atual, corpo, cerca = [], "", [], False
    for linha in (texto or "").splitlines():
        if linha.lstrip().startswith("```"):
            cerca = not cerca
        m = None if cerca else re.match(r"^#{1,3}\s+(.*\S)\s*$", linha)
        if m:
            partes.append((atual, "\n".join(corpo).strip()))
            atual, corpo = m.group(1), []
        else:
            corpo.append(linha)
    partes.append((atual, "\n".join(corpo).strip()))
    return [(t, c) for t, c in partes if c]


def unidade(origem, entidades=(), relacoes=(), pedacos=(), versao=None):
    """Monta uma unidade. entidades: dicts {tipo, chave, nome, desc, caminho, link, data, alias, extra}; relacoes: (de, rel, para)
    com ids "tipo:chave"; pedacos: (id da entidade, seção, texto). O hash cobre o conteúdo (ou `versao`, ex.: o commit)."""
    ents = []
    for e in entidades:
        ents.append({"id": e.get("id") or f"{e['tipo']}:{e['chave']}", "tipo": e["tipo"], "nome": limpar(e["nome"]),
                     "desc": limpar(e.get("desc") or "")[:600], "caminho": e.get("caminho") or "", "link": e.get("link") or "",
                     "data": e.get("data") or "", "alias": [limpar(a) for a in e.get("alias") or []], "extra": e.get("extra") or {}})
    peds = [[i, s, limpar(t)] for i, s, t in pedacos if (t or "").strip()]
    rels = [list(r) for r in relacoes]
    payload = {"e": ents, "r": rels, "p": peds}
    corpo = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return {"origem": origem, "hash": _h(versao or corpo, str(len(corpo))) if versao else _h(corpo), "payload": payload}


# ---- aplicar unidades ----

def aplicar(prefixo, unidades, db=None, apagar=True):
    """Troca as unidades cuja origem começa por `prefixo` pelas novas (o que não veio some, salvo apagar=False). True se algo mudou."""
    c = _rw(Path(db or caminho_db()))
    novas = {u["origem"]: u for u in unidades}
    antes = {r["origem"]: r["hash"] for r in c.execute("select origem, hash from unidade where origem like ? escape '\\'",
                                                       (prefixo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))}
    mudou = False
    with c:
        for o, u in novas.items():
            if antes.get(o) != u["hash"]:
                c.execute("insert or replace into unidade values(?,?,?)", (o, u["hash"], json.dumps(u["payload"], ensure_ascii=False)))
                mudou = True
        for o in antes if apagar else ():
            if o not in novas:
                c.execute("delete from unidade where origem=?", (o,))
                mudou = True
    c.close()
    return mudou


def meta(chave, valor=None, db=None):
    c = _rw(Path(db or caminho_db()))
    try:
        if valor is None:
            r = c.execute("select valor from meta where chave=?", (chave,)).fetchone()
            return r["valor"] if r else None
        with c:
            c.execute("insert or replace into meta values(?,?)", (chave, valor))
    finally:
        c.close()


# ---- remontagem ----

def _chaves_mencao(ents):
    """{texto minúsculo: id} do que vale ligar por menção: repo, domínio, projeto, app, issue, segredo."""
    candidatos = {}
    for ent in ents.values():
        tipo = ent['tipo']
        if tipo not in {'repo', 'dominio', 'projeto', 'app', 'pessoa', 'issue', 'segredo'}:
            continue
        nomes = [ent['nome']] + (ent.get('alias', []) if tipo not in {'issue', 'pessoa'} else [])
        if tipo in {'projeto', 'repo'}:
            nomes += [ent['id'].split(':', 1)[1].split('/')[-1]]
        for nome in nomes:
            nome = sem_acento(nome).strip()
            if len(nome) < 4 or (tipo == 'segredo' and '_' not in nome):
                continue
            if tipo == 'repo' and len(nome.split('/')[-1]) < 7 and not re.search(r'[-0-9]', nome):
                continue
            if tipo == 'app' and len(nome) < 6 or tipo == 'segredo' and len(nome) < 8:
                continue
            if tipo == 'issue' and not re.fullmatch(r'[a-z]+-\d+', nome):
                continue
            if tipo == 'pessoa' and len(nome.split()) < 2:
                continue
            candidatos.setdefault(nome, set()).add(ent['id'])
    return {k: next(iter(ids)) for k, ids in candidatos.items() if len(ids) == 1}


ORIGENS_MENCAO = ("memoria", "decisao", "documento", "issue", "receita")
RELACOES_FRACAS = {"documenta", "decidido_em", "implementado_em", "pertence", "pertence_inferido"}
TIPOS_TEXTO = {"pr", "issue", "documento", "decisao", "memoria", "tarefa", "receita"}


def remontar(db=None, log=print):
    """Refaz entidade, relacao e pedaco a partir das unidades (entidade repetida junta alias e completa os campos vazios)."""
    c = _rw(Path(db or caminho_db()))
    ents, rels, peds = {}, set(), []
    for r in c.execute("select payload from unidade order by origem"):
        p = json.loads(r["payload"])
        for e in p["e"]:
            a = ents.get(e["id"])
            if not a:
                ents[e["id"]] = e
                continue
            for k in ("desc", "caminho", "link", "data"):
                a[k] = a[k] or e[k]
            a["alias"] = sorted(set(a["alias"]) | set(e["alias"]))
            a["extra"] = {**e["extra"], **a["extra"]}
        rels.update(tuple(x) for x in p["r"])
        peds += [(i, s, t) for i, s, t in p["p"]]
    # entidades citadas só por relação ainda precisam existir (ex.: domínio visto só no Traefik)
    for de, _, para in list(rels):
        if de not in ents or para not in ents:
            rels.discard((de, _, para))
    # Origem física já coletada: arquivo de configuração é entidade de proveniência,
    # apenas metadados. Não abrir o arquivo nem indexar seu conteúdo.
    arquivos = {e['caminho']: e['id'] for e in ents.values() if e['tipo'] == 'documento' and e['caminho']}
    vps_por = {}
    for de, rel, para in rels:
        if rel == 'roda_em' and ents[para]['tipo'] == 'vps':
            vps_por.setdefault(de, []).append(para)
    for ent in list(ents.values()):
        if ent['tipo'] != 'segredo' or not ent['caminho']:
            continue
        caminho = ent['caminho']
        aid = arquivos.get(caminho)
        if not aid:
            aid = 'documento:config-' + _h(caminho)[:20]
            arquivos[caminho] = aid
            ents[aid] = {'id': aid, 'tipo': 'documento', 'nome': 'Configuração ' + caminho,
                         'desc': 'Origem de configuração: somente metadados; conteúdo nunca indexado.',
                         'caminho': caminho, 'link': '', 'data': '', 'alias': [], 'extra': {}}
        rels.add((ent['id'], 'guardado_em', aid))
        for para in vps_por.get(ent['id'], []):
            rels.add((aid, 'roda_em', para))
    # menções
    chaves = _chaves_mencao(ents)
    if chaves:
        rx = re.compile(r"(?<![a-z0-9_-])(" + "|".join(re.escape(k) for k in sorted(chaves, key=len, reverse=True)) + r")(?![a-z0-9_-])")
        texto_por = {i: [sem_acento(e["nome"] + " " + e["desc"])] for i, e in ents.items() if e["tipo"] in ORIGENS_MENCAO}
        for i, _, t in peds:
            if i in ents and ents[i]["tipo"] in ORIGENS_MENCAO:
                texto_por.setdefault(i, []).append(sem_acento(t))
        for i, ts in texto_por.items():
            vistos = Counter(chaves[m] for m in rx.findall("\n".join(ts)))
            for alvo in vistos:
                if alvo != i:
                    rels.add((i, "menciona", alvo))
    from types import SimpleNamespace
    from hub.grafo_saude import projetos
    # A path to a checked-out repository is stronger evidence than project-name
    # inference. Use complete directory components, never a shared VPS or a
    # substring (clienteh-import is not clienteh).
    repos = {eid.split(':', 1)[1]: eid for eid, e in ents.items() if e['tipo'] == 'repo'}
    for eid, ent in ents.items():
        if ent['tipo'] not in {'cron', 'servico'}:
            continue
        texto = (ent.get('caminho') or '') + ' ' + (ent.get('desc') or '')
        caminhos = re.findall(r'/(?:opt|srv|home/[\w.-]+/repos)/([\w.-]+)(?=/|[\s;\"\']|$)', texto)
        for nome in set(caminhos):
            if nome in repos:
                rels.add((eid, 'depende_de', repos[nome]))
    sai, entra = {}, {}
    for de, rel, para in rels:
        sai.setdefault(de, []).append((rel, para))
        entra.setdefault(para, []).append((rel, de))
    donos = projetos(SimpleNamespace(ent=ents, sai=sai, entra=entra))
    for eid, projeto in donos.items():
        if ents[eid]['tipo'] in {'memoria', 'decisao', 'documento', 'cron', 'servico'}:
            rels.add((eid, 'pertence_inferido' if ents[eid]['tipo'] in {'cron', 'servico'} else 'pertence', projeto))
    with c:
        for t in ("entidade", "relacao", "pedaco"):
            c.execute(f"delete from {t}")
        c.executemany("insert into entidade values(?,?,?,?,?,?,?,?,?)",
                      [(e["id"], e["tipo"], e["nome"], e["desc"], e["caminho"], e["link"], e["data"],
                        json.dumps(e["alias"], ensure_ascii=False), json.dumps(e["extra"], ensure_ascii=False)) for e in ents.values()])
        c.executemany("insert into relacao values(?,?,?)", sorted(rels))
        linhas = []
        vizinho = {}
        for de, rel, para in sorted(rels):
            if rel != "menciona":
                # 300 documentos que "documentam" e PRs que "dependem" não podem esconder o "serve"/"roda_em" do repo
                fraca = rel in RELACOES_FRACAS
                vizinho.setdefault(de, []).append((2 if fraca else 0, f"{rel} {ents[para]['nome']}"))
                vizinho.setdefault(para, []).append((2 if fraca or ents[de]["tipo"] in TIPOS_TEXTO else 1, f"{ents[de]['nome']} {rel}"))
        vizinho = {k: [t for _, t in sorted(v, key=lambda x: x[0])] for k, v in vizinho.items()}
        for e in ents.values():  # a ficha: nome, tipo, apelidos, descrição e as relações fortes (o BM25 acha "quem serve tal domínio")
            ficha = (f"{e['nome']} ({e['tipo']})\n" + (f"também: {', '.join(e['alias'])}\n" if e["alias"] else "") + e["desc"]
                     + (f"\nrelações: {'; '.join(vizinho[e['id']][:10])}" if e["id"] in vizinho and e["tipo"] != "segredo" else ""))
            linhas.append((e["id"], "ficha", ficha, _h(ficha)))
        for i, s, t in peds:
            if i in ents:
                linhas.append((i, s, t, _h(t)))
        c.executemany("insert into pedaco(ent, secao, texto, h) values(?,?,?,?)", linhas)
        c.execute("delete from vec where h not in (select h from pedaco)")
    c.close()
    _carregar.cache_clear()
    return {"entidades": len(ents), "relacoes": len(rels), "pedacos": len(linhas)}


# ---- embeddings ----

def embeddar(textos, timeout=60):
    """Desligado: a OpenRouter fica só no WhatsApp; quem chama já cai em BM25."""
    raise RuntimeError("embeddings desligados: OpenRouter só serve o WhatsApp (KRN-1445); a busca segue em BM25")


def _blob(v):
    a = np.asarray(v, dtype=np.float32)
    return (a / (np.linalg.norm(a) or 1.0)).tobytes()


def completar_vetores(db=None, embed=embeddar, log=print):
    """Embedding só do texto que ainda não tem (cache por hash). Sem chave ou fora do ar a busca segue em BM25."""
    c = _rw(Path(db or caminho_db()))
    nomes = {r["id"]: r["nome"] for r in c.execute("select id, nome from entidade")}
    faltam = c.execute("select h, min(ent) ent, min(secao) secao, min(texto) texto from pedaco where h not in (select h from vec) group by h").fetchall()
    feitos = 0
    try:
        for i in range(0, len(faltam), LOTE):
            lote = faltam[i:i + LOTE]
            vs = embed([f"{nomes.get(f['ent'], '')} — {f['secao']}\n{f['texto']}" for f in lote])
            with c:
                c.executemany("insert or replace into vec values(?,?)", [(f["h"], _blob(v)) for f, v in zip(lote, vs)])
            feitos += len(lote)
    except Exception as ex:  # noqa: BLE001
        log(f"aviso: embeddings parados ({type(ex).__name__}: {str(ex)[:120]})")
    c.close()
    _carregar.cache_clear()
    return {"vetores": feitos, "sem_vetor": len(faltam) - feitos}


# ---- busca ----

def _nome_na_pergunta(nome, tipo, consulta):
    partes = {_singular(p) for p in re.split(r"[\s._:@/-]+", sem_acento(nome)) if len(p) > 2 and p not in STOP and p != tipo}
    return len(partes) >= 2 and partes <= set(tokens(consulta))


class _Indice:
    def __init__(self, c):
        self.ent = {r["id"]: dict(r) for r in c.execute("select * from entidade")}
        for e in self.ent.values():
            e["desc"] = e.pop("descricao")
            e["alias"] = json.loads(e["alias"] or "[]")
            e["extra"] = json.loads(e["extra"] or "{}")
        rows = c.execute("select p.id, p.ent, p.secao, p.texto, v.vec from pedaco p left join vec v on v.h = p.h order by p.id").fetchall()
        self.ped = [dict(ent=r["ent"], secao=r["secao"], texto=r["texto"]) for r in rows if r["ent"] in self.ent]
        rows = [r for r in rows if r["ent"] in self.ent]
        self.tf = [Counter(tokens(self.ent[p["ent"]]["nome"] + "\n" + p["texto"])) for p in self.ped]
        self.len = np.array([sum(t.values()) for t in self.tf] or [1], dtype=np.float32)
        self.avg = float(self.len.mean()) or 1.0
        df = Counter(t for tf in self.tf for t in tf)
        n = max(len(self.tf), 1)
        self.idf = {t: float(np.log(1 + (n - d + 0.5) / (d + 0.5))) for t, d in df.items()}
        self.vec = np.zeros((len(self.ped), DIM), dtype=np.float32)
        self.tem_vec = np.zeros(len(self.ped), dtype=bool)
        for i, r in enumerate(rows):
            if r["vec"]:
                self.vec[i] = np.frombuffer(r["vec"], dtype=np.float32)
                self.tem_vec[i] = True
        self.media = self.vec[self.tem_vec].mean(0) if self.tem_vec.any() else np.zeros(DIM, dtype=np.float32)
        if self.tem_vec.any():
            self.vec[self.tem_vec] -= self.media
            self.vec[self.tem_vec] /= np.linalg.norm(self.vec[self.tem_vec], axis=1, keepdims=True) + 1e-9
        # marcas: as palavras dos projetos ("clientei", "asite01"...), para a pergunta que cita uma marca preferir quem a tem
        self.marcas = {p for e in self.ent.values() if e["tipo"] == "projeto" for p in tokens(e["id"].split(":", 1)[1]) if len(p) > 3}
        self.sai, self.entra = {}, {}
        for r in c.execute("select de, rel, para from relacao"):
            if r["de"] in self.ent and r["para"] in self.ent:
                self.sai.setdefault(r["de"], []).append((r["rel"], r["para"]))
                self.entra.setdefault(r["para"], []).append((r["rel"], r["de"]))

    def bm25(self, consulta):
        nota = np.zeros(len(self.ped), dtype=np.float32)
        for t in set(tokens(consulta)):
            idf = self.idf.get(t)
            if not idf:
                continue
            f = np.array([tf.get(t, 0) for tf in self.tf], dtype=np.float32)
            nota += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * self.len / self.avg))
        return nota


@functools.lru_cache(maxsize=2)
def _carregar(p, mtime):
    c = _ro(p)
    try:
        return _Indice(c)
    finally:
        c.close()


def _indice(db=None):
    p = Path(db or caminho_db())
    return _carregar(str(p), p.stat().st_mtime_ns) if p.is_file() else None


_indice.cache_clear = _carregar.cache_clear


def disponivel(db=None):
    try:
        i = _indice(db)
    except sqlite3.Error:
        return False
    return bool(i and i.ent)


def _ranque(nota, mascara, minimo, n=400):
    idx = np.where(mascara & (nota > minimo))[0]
    return list(idx[np.argsort(-nota[idx], kind="stable")][:n])


def _bm(x, n):
    return x["bm25"] * np.log(1 + 7500) / np.log(1 + max(n, 2))  # o BM25 cresce com o log do tamanho do índice (idf): normaliza para o do gabarito


def _passa(x, com_vetor, n=7500):
    """Descarta o que não tem nenhum sinal (sem palavra em comum e longe no sentido). É frouxo de propósito: no gabarito, acertos
    legítimos têm cos 0,21 com bm25 7,3 e perguntas sem relação nenhuma têm cos 0,17 com bm25 7,4; os dois lados se sobrepõem."""
    return _bm(x, n) >= 2 or (com_vetor and x["cos"] >= 0.25)


def confianca(x, n=7500):
    """'alta' se o melhor resultado passa na régua do gabarito (cos >= 0,35, ou bm25 >= 8, ou os dois a meio caminho), senão 'baixa'."""
    bm = _bm(x, n)
    return "alta" if x["cos"] >= 0.35 or bm >= 8 or (x["cos"] >= 0.28 and bm >= 6) else "baixa"


def buscar(consulta, tipo=None, limite=5, db=None, embed=embeddar, vetor=None):
    """Entidades mais relevantes (BM25 + embeddings por RRF), cada uma com o melhor pedaço:
    [{id, tipo, nome, desc, caminho, link, data, rrf, cos, bm25, secao, trecho}]."""
    ix = _indice(db)
    if not ix or not ix.ped or not (consulta or "").strip():
        return []
    mascara = np.array([not tipo or ix.ent[p["ent"]]["tipo"] == tipo for p in ix.ped], dtype=bool)
    if not mascara.any():
        return []
    bm = ix.bm25(consulta)
    cos = np.zeros(len(ix.ped), dtype=np.float32)
    if vetor is None and ix.tem_vec.any():
        try:
            vetor = embed([consulta[:6000]])[0]
        except Exception:  # noqa: BLE001
            vetor = None
    if vetor is not None and ix.tem_vec.any():
        q = np.asarray(vetor, dtype=np.float32) - ix.media
        cos = ix.vec @ (q / (np.linalg.norm(q) or 1.0))
    rrf = np.zeros(len(ix.ped), dtype=np.float32)
    for lista in (_ranque(bm, mascara, 0), _ranque(cos, mascara & ix.tem_vec, 0.1)):
        for pos, i in enumerate(lista):
            rrf[i] += 1.0 / (RRF_K + pos + 1)
    por = {}
    for i in np.where(mascara & (rrf > 0))[0]:
        por.setdefault(ix.ped[i]["ent"], []).append(i)
    saida = []
    for eid, idx in por.items():
        idx = sorted(idx, key=lambda i: -rrf[i])
        e = ix.ent[eid]
        saida.append({"id": eid, "tipo": e["tipo"], "nome": limpar(e["nome"]), "desc": e["desc"], "caminho": e["caminho"], "link": e["link"],
                      "data": e["data"], "rrf": float(rrf[idx[0]] + 0.5 * sum(rrf[i] for i in idx[1:2])),
                      "cos": float(max(cos[i] for i in idx)), "bm25": float(max(bm[i] for i in idx)),
                      "secao": ix.ped[idx[0]]["secao"], "trecho": ix.ped[idx[0]]["texto"]})
    saida = [x for x in saida if _passa(x, vetor is not None and ix.tem_vec.any(), len(ix.ped))]
    alvo = intencao(consulta)
    marcas = ix.marcas & set(tokens(consulta))
    for x in saida:
        if marcas and not marcas & set(tokens(x["id"] + " " + x["nome"] + " " + ix.ped[por[x["id"]][0]]["texto"])):
            x["rrf"] *= SEM_MARCA  # a pergunta cita "ClienteI" e esta entidade não tem a palavra
    for x in saida:
        x["rrf"] *= PESO_TIPO.get(x["tipo"], 1.0) * alvo.get(x["tipo"], 1.0) * (NOME_NA_PERGUNTA if _nome_na_pergunta(x["nome"], x["tipo"], consulta) else 1.0)
    saida.sort(key=lambda s: -s["rrf"])
    if alvo:  # o grafo ajuda: "qual REPO serve x" acha o domínio x; o repo ligado a ele (e a cada outro do topo) por relação forte sobe
        por_id = {x["id"]: x for x in saida}
        bonus = Counter()
        for topo in saida[:3]:
            for o in {o for r, o in ix.sai.get(topo["id"], []) + ix.entra.get(topo["id"], []) if r != "menciona" and r not in RELACOES_FRACAS}:
                if o in por_id and o != topo["id"] and por_id[o]["tipo"] in alvo:
                    bonus[o] += BONUS_VIZINHO * topo["rrf"]
        for o, b in bonus.items():
            por_id[o]["rrf"] += b
        saida.sort(key=lambda s: -s["rrf"])
    return saida[:limite]


def _achar(ref, db=None):
    """Entidade por id exato ("repo:cliente-c"), por nome exato ou apelido; senão a melhor da busca pelo nome."""
    ix = _indice(db)
    if not ix:
        return None
    ref = (ref or "").strip()
    if ref in ix.ent:
        return ref
    r = sem_acento(ref)
    for e in ix.ent.values():
        if sem_acento(e["nome"]) == r or e["id"].split(":", 1)[1].lower() == r or r in (sem_acento(a) for a in e["alias"]):
            return e["id"]
    achados = buscar(ref, limite=1, db=db)
    return achados[0]["id"] if achados else None


def _breve(e):
    return {"id": e["id"], "tipo": e["tipo"], "nome": limpar(e["nome"]), "desc": e["desc"][:200], "caminho": e["caminho"], "link": e["link"]}


def vizinhos(ref, limite=40, db=None):
    """{entidade, relacoes: [{rel, sentido, entidade}]} (1 salto). None se a entidade não existe."""
    ix = _indice(db)
    eid = _achar(ref, db) if ix else None
    if not eid:
        return None
    rels = [{"rel": r, "sentido": "sai", "entidade": _breve(ix.ent[o])} for r, o in ix.sai.get(eid, [])]
    rels += [{"rel": r, "sentido": "entra", "entidade": _breve(ix.ent[o])} for r, o in ix.entra.get(eid, [])]
    rels.sort(key=lambda x: (x["rel"] == "menciona", x["rel"], x["entidade"]["nome"]))
    return {"entidade": _breve(ix.ent[eid]), "total": len(rels), "relacoes": rels[:limite]}


def _vide(ix, eid, n=6):
    """Linha curta com as relações fortes (sem menção) de uma entidade: 'serve: x; roda_em: y'."""
    por = {}
    for r, o in ix.sai.get(eid, []):
        if r != "menciona":
            por.setdefault(r, []).append(ix.ent[o]["nome"])
    for r, o in ix.entra.get(eid, []):
        if r != "menciona":
            por.setdefault(f"← {r}", []).append(ix.ent[o]["nome"])
    return "; ".join(f"{r}: {', '.join(v[:3])}" for r, v in list(por.items())[:n])


def responder(pergunta, tipo=None, limite=5, db=None, embed=embeddar, vetor=None):
    """O que o MCP devolve: resposta curta (montada das fichas, sem LLM), fontes com caminho/link e as relações da 1ª entidade."""
    achados = buscar(pergunta, tipo, limite=max(limite, 3), db=db, embed=embed, vetor=vetor)
    if not achados:
        return {"ok": True, "resposta": "Nada no grafo casou com essa pergunta.", "fontes": [], "achados": 0, "confianca": "nenhuma"}
    ix = _indice(db)
    paginas = [a for a in achados if a["tipo"] == "pagina"]
    if paginas and achados[0]["tipo"] != "pagina" and paginas[0]["rrf"] >= 0.4 * achados[0]["rrf"]:  # a página do assunto vai à frente
        achados = [paginas[0]] + [a for a in achados if a is not paginas[0]]
    topo = achados[0]
    conf = confianca(topo, len(ix.ped)) if ix.tem_vec.any() else "baixa"
    util = [a for a in achados if a["rrf"] >= 0.6 * topo["rrf"] or a is topo][:3]
    linhas = []
    for a in util:
        e = ix.ent[a["id"]]
        if a["tipo"] == "pagina":  # a página inteira (estado atual com fonte e data), não o trecho
            trecho = " ".join(a["trecho"].split())
            abertura = " ".join(e["desc"].split())
            if trecho and trecho[:80] not in abertura:  # o fato achado pode estar depois da abertura (a desc guarda só o começo)
                abertura = f"{abertura[:400]} … {trecho}"
            linhas.append(f"PÁGINA {e['caminho']} (estado em {e['data'] or '—'}): {abertura[:900]}")
            continue
        corpo = " ".join((a["trecho"] if a["secao"] != "ficha" else e["desc"]).split())
        corpo = re.sub(r"^" + re.escape(e["nome"]) + r"\s*\(\w+\)\s*", "", corpo)
        linhas.append(f"{e['nome']} ({e['tipo']}): {corpo[:260]}".rstrip(": "))
    vide = _vide(ix, topo["id"])
    resposta = "\n".join(linhas) + (f"\nRelações de {topo['nome']}: {vide}" if vide else "")
    fontes = [{"entidade": a["id"], "tipo": a["tipo"], "nome": a["nome"], "caminho": a["caminho"], "link": a["link"], "data": a["data"],
               "trecho": " ".join(a["trecho"].split())[:240], "nota": round(a["rrf"], 4)} for a in achados[:limite]]
    if conf == "baixa":
        resposta = "Confiança baixa (nada casou forte; confira as fontes):\n" + resposta
    return {"ok": True, "resposta": resposta, "fontes": fontes, "achados": len(achados), "confianca": conf}


def estado(db=None):
    ix = _indice(db)
    if not ix:
        return {"entidades": 0}
    return {"entidades": len(ix.ent), "relacoes": sum(len(v) for v in ix.sai.values()), "pedacos": len(ix.ped),
            "com_vetor": int(ix.tem_vec.sum()), "tipos": dict(Counter(e["tipo"] for e in ix.ent.values()))}


# ---- gabarito ----

def gabarito(arquivo=None, db=None, embed=embeddar, log=print):
    """acerto@3: a pergunta acerta se algum dos 3 primeiros resultados tem id (ou nome) que contém algum dos `esperado`."""
    arq = Path(arquivo or Path(__file__).resolve().parent.parent / "tests" / "grafo_gabarito.json")
    casos = json.loads(arq.read_text(encoding="utf-8"))
    certos = 0
    for c in casos:
        top = buscar(c["pergunta"], limite=3, db=db, embed=embed)
        esp = [sem_acento(x) for x in c["esperado"]]
        ok = any(x in sem_acento(a["id"] + " " + a["nome"]) for a in top for x in esp)
        certos += ok
        if not ok:
            log(f"ERROU: {c['pergunta']!r} esperado {c['esperado']} veio {[a['id'] for a in top]}")
    taxa = certos / max(len(casos), 1)
    log(f"acerto@3 = {taxa:.2f} ({certos}/{len(casos)})")
    return taxa


# ---- instruções nas contas ----

BLOCO = ("<!-- cerebro:inicio -->", "<!-- cerebro:fim -->")


def instrucoes(repo_md=None, contas=None, dono=None):
    from hub import biblioteca as B, faltou, home_agentes
    try:
        import hub.vigia_clientej as V
    except ImportError as e:  # snapshot público: sem o plugin, as contas ficam como estão
        if not faltou(e, "hub.vigia_clientej"):
            raise
        print("aviso: sem hub.vigia_clientej; instruções do cérebro não aplicadas")
        return 0
    repo_md = repo_md or Path(__file__).resolve().parent.parent / "deploy" / "orquestrador" / "CLAUDE.md"
    ini, fim = BLOCO
    bloco = V.secao(repo_md.read_text(encoding="utf-8"), ini, fim)
    if not bloco:
        print("aviso: CLAUDE.md do repo sem a seção cerebro")
        return 0
    for conta in contas if contas is not None else sorted(home_agentes().glob(".claude*/")):
        c = Path(conta) / "CLAUDE.md"
        if Path(conta).is_symlink() or c.is_symlink() or not Path(conta).is_dir():
            continue
        try:
            antes = c.read_text(encoding="utf-8") if c.exists() else ""
            depois = V.aplicar_secao(antes, bloco, ini, fim)
            if depois != antes:
                V.gravar(c, depois, 0o664, dono or B._dono_claude)
                print(f"instruções do cérebro em {c}")
        except OSError as ex:
            print(f"aviso: {c}: {ex}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, resto = (argv[0] if argv else ""), argv[1:]
    if cmd == "atualizar":
        from hub import grafo_coleta as C
        print(json.dumps(C.atualizar(resto or ["rapido"]), ensure_ascii=False))
        return 0
    if cmd == "buscar":
        r = responder(" ".join(resto))
        print(r["resposta"])
        for f in r["fontes"]:
            print(f"  [{f['nota']}] {f['entidade']}  {f['caminho'] or f['link']}")
        return 0
    if cmd == "vizinhos":
        r = vizinhos(" ".join(resto))
        if not r:
            print("não achei")
            return 1
        print(f"{r['entidade']['id']}: {r['entidade']['nome']}")
        for x in r["relacoes"]:
            print(f"  {'→' if x['sentido'] == 'sai' else '←'} {x['rel']} {x['entidade']['id']}")
        return 0
    if cmd == "gabarito":
        return 0 if gabarito(resto[0] if resto else None) >= 0.85 else 1
    if cmd == "estado":
        print(json.dumps(estado(), ensure_ascii=False))
        return 0
    if cmd == "instrucoes":
        return instrucoes()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
