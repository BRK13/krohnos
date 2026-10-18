"""Índice híbrido da biblioteca, a mesma metodologia do ClienteJ: pedaços por seção de cada receita com
metadados (tema, projetos, status, licença, data), BM25 (palavra exata: lib, comando, técnica) + embeddings (sentido) juntados
por RRF, filtro por projeto e tema. SQLite + numpy em $HUB_DATA/biblioteca-indice.db (é pequeno; o clone da biblioteca é
apagado a cada volta do cron, por isso o índice mora fora dele). Reindexa só a receita cujo hash mudou.

Embeddings: openai/text-embedding-3-small, 768 dimensões, pelo OpenRouter (chave OPENROUTER_API_KEY ou
~/.config/openrouter.key). Sem chave ou fora do ar a busca cai para BM25 e o próximo `atualizar` completa os vetores.

    python3 -m hub.biblioteca_indice atualizar [<clone>]   reindexa o que mudou (o deploy/biblioteca.sh chama a cada 10 min)
    python3 -m hub.biblioteca_indice buscar <texto>        top 3 com as notas (para ver o que o despacho injetaria)
    python3 -m hub.biblioteca_indice gabarito              precisão@3 e recall@3 contra tests/biblioteca_gabarito.json
"""
import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np

from hub.comum.segredos import chave_openrouter
from hub import biblioteca as B, config, data

MODELO = "openai/text-embedding-3-small"
DIM = 768
URL = "https://openrouter.ai/api/v1/embeddings"
LOTE = 64
PEDACO = 1800          # caracteres por pedaço (parte um corpo longo em parágrafos)
MAX_CONTEUDO = 10      # pedaços de "conteúdo original" por receita (o resto é a cópia literal de artigo/prompt)
RRF_K = 60
BM25_K1, BM25_B = 1.5, 0.75
# Limiar do despacho, calibrado no gabarito (tests/biblioteca_gabarito.json): melhor nada do que receita errada.
LIMIAR_COS = 0.40      # cosseno (vetores centrados) do melhor pedaço com a consulta
LIMITE_DESPACHO = 3
STOP = set("""a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sob sobre e ou que se ao aos à às é
ser foi são como mais menos mas já não nao sim the of and or to in for on with is are be an at by from it this that as""".split())
RE_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#_-]*[a-z0-9]|[a-z0-9]")
RE_TITULO = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
RE_LICENCA = re.compile(r"licen[cç]a[^\n:]*:\s*([^\n]{1,80})", re.I)
RE_REPRO = re.compile(r"reprodu|nossa|nosso|reconstru|snippet|prompt de|comando|instal", re.I)


def caminho_db():
    return Path(os.environ.get("BIBLIOTECA_INDICE") or data() / "biblioteca-indice.db")


def _ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _rw(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
        create table if not exists receita(caminho text primary key, hash text, titulo text, tema text, projetos text,
            status text, licenca text, data text);
        create table if not exists pedaco(id integer primary key, caminho text, secao text, ordem int, texto text, vec blob);
        create index if not exists pedaco_caminho on pedaco(caminho);""")
    return c


# ---- pedaços ----

def tokens(texto):
    """Palavras sem acento e sem caixa; plural simples removido; stopwords fora."""
    out = []
    for t in RE_TOKEN.findall(B._sem_acento(texto)):
        if t in STOP:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _secoes(texto):
    """[(titulo da seção, corpo)] pelos cabeçalhos # a ###, ignorando "#" dentro de bloco de código."""
    partes, atual, corpo, cerca = [], "", [], False
    for linha in texto.splitlines():
        if linha.lstrip().startswith("```"):
            cerca = not cerca
        m = None if cerca else RE_TITULO.match(linha)
        if m and len(m.group(1)) <= 2:
            partes.append((atual, "\n".join(corpo).strip()))
            atual, corpo = m.group(2), []
        else:
            corpo.append(linha)
    partes.append((atual, "\n".join(corpo).strip()))
    return [(t, c) for t, c in partes if c]


def _quebrar(corpo):
    if len(corpo) <= PEDACO:
        return [corpo]
    out, atual = [], ""
    for par in re.split(r"\n\s*\n", corpo):
        if atual and len(atual) + len(par) > PEDACO:
            out.append(atual)
            atual = ""
        while len(par) > PEDACO:  # bloco de código gigante: corta
            out.append(par[:PEDACO])
            par = par[PEDACO:]
        atual = f"{atual}\n\n{par}" if atual else par
    return out + ([atual] if atual else [])


def pedacos(texto):
    """[(secao, texto)] de uma receita. secao: o_que_e (abertura), onde_aplicar, reproducao ou conteudo."""
    out, conteudo = [], 0
    for i, (titulo, corpo) in enumerate(_secoes(texto)):
        t = B._sem_acento(titulo)
        if i == 0:
            secao = "o_que_e"
        elif "onde aplicar" in t:
            secao = "onde_aplicar"
        elif RE_REPRO.search(t):
            secao = "reproducao"
        else:
            secao = "conteudo"
        for p in _quebrar(corpo):
            if secao == "conteudo":
                conteudo += 1
                if conteudo > MAX_CONTEUDO:
                    continue
            out.append((secao, (f"{titulo}\n" if titulo else "") + p))
    return out


def _hash(x):
    return hashlib.sha1("\0".join([x["texto"], x["titulo"], x["status"], ",".join(x["projetos"])]).encode()).hexdigest()


def _licenca(texto):
    m = RE_LICENCA.search(texto[:6000])
    return m.group(1).strip(" *`_.") if m else ""


# ---- embeddings ----

def _chave():
    return chave_openrouter()


def embeddar(textos, timeout=60):
    """Lista de vetores (list[float]) na ordem de textos; levanta se a chamada falhar."""
    k = _chave()
    if not k:
        raise RuntimeError("sem OPENROUTER_API_KEY")
    corpo = json.dumps({"model": MODELO, "input": [t[:6000] or " " for t in textos], "dimensions": DIM}).encode()
    req = urllib.request.Request(URL, data=corpo, method="POST", headers={
        "Authorization": f"Bearer {k}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    itens = sorted(d["data"], key=lambda i: i["index"])
    if len(itens) != len(textos) or any(len(i["embedding"]) != DIM for i in itens):
        raise RuntimeError("resposta de embeddings fora do formato")
    return [i["embedding"] for i in itens]


def _blob(v):
    a = np.asarray(v, dtype=np.float32)
    return (a / (np.linalg.norm(a) or 1.0)).tobytes()


# ---- atualização incremental ----

def atualizar(r=None, db=None, embed=embeddar, log=print):
    """Reindexa só a receita que mudou (hash), tira as que sumiram e completa os vetores que faltam.
    Devolve {"receitas", "reindexadas", "removidas", "vetores", "sem_vetor"}."""
    itens = B.receitas(r)
    p = Path(db or caminho_db())
    if not itens:
        return {"receitas": 0, "reindexadas": 0, "removidas": 0, "vetores": 0, "sem_vetor": 0}
    c = _rw(p)
    antes = {x["caminho"]: x["hash"] for x in c.execute("select caminho, hash from receita")}
    vivos, mudou = set(), 0
    with c:
        for x in itens:
            vivos.add(x["caminho"])
            meta = (x["titulo"], x["tema"], json.dumps(x["projetos"]), x["status"], _licenca(x["texto"]), x["data"])
            h = _hash(x)
            if antes.get(x["caminho"]) == h:  # texto igual: só a data/commit pode ter mudado
                c.execute("update receita set titulo=?, tema=?, projetos=?, status=?, licenca=?, data=? where caminho=?",
                          (*meta, x["caminho"]))
                continue
            mudou += 1
            c.execute("delete from pedaco where caminho=?", (x["caminho"],))
            c.execute("insert or replace into receita values(?,?,?,?,?,?,?,?)", (x["caminho"], h, *meta))
            c.executemany("insert into pedaco(caminho, secao, ordem, texto) values(?,?,?,?)",
                          [(x["caminho"], s, i, t) for i, (s, t) in enumerate(pedacos(x["texto"]))])
        sumiram = [k for k in antes if k not in vivos]
        for k in sumiram:
            c.execute("delete from pedaco where caminho=?", (k,))
            c.execute("delete from receita where caminho=?", (k,))
    titulos = {x["caminho"]: x["titulo"] for x in itens}
    faltam = c.execute("select id, caminho, secao, texto from pedaco where vec is null").fetchall()
    feitos = 0
    try:
        for i in range(0, len(faltam), LOTE):
            lote = faltam[i:i + LOTE]
            vs = embed([f"{titulos.get(f['caminho'], '')} — {f['secao']}\n{f['texto']}" for f in lote])
            with c:
                c.executemany("update pedaco set vec=? where id=?", [(_blob(v), f["id"]) for f, v in zip(lote, vs)])
            feitos += len(lote)
    except Exception as ex:  # sem chave ou OpenRouter fora: a busca segue em BM25 e a próxima volta completa
        log(f"aviso: embeddings parados ({type(ex).__name__}: {str(ex)[:120]})")
    c.close()
    _indice.cache_clear()
    return {"receitas": len(itens), "reindexadas": mudou, "removidas": len(sumiram), "vetores": feitos,
            "sem_vetor": len(faltam) - feitos}


# ---- busca ----

class _Indice:
    """Índice em memória. `rec`: {chave: {"titulo": ..., ...}}; `rows`: pedaços {id, caminho (a chave), secao, texto, vec}.
    Sem argumentos lê as tabelas receita/pedaco de `c`; o hub.busca passa as suas fontes."""

    def __init__(self, c=None, rec=None, rows=None):
        if rec is None:
            rec = {r["caminho"]: dict(r) for r in c.execute("select * from receita")}
            for r in rec.values():
                r["projetos"] = json.loads(r["projetos"] or "[]")
            rows = c.execute("select id, caminho, secao, ordem, texto, vec from pedaco order by id").fetchall()
        self.rec = rec
        self.ped = [dict(id=x["id"], caminho=x["caminho"], secao=x["secao"], texto=x["texto"])
                    for x in rows if x["caminho"] in self.rec]
        self.tf = [Counter(tokens(self.rec[p["caminho"]]["titulo"] + "\n" + p["texto"])) for p in self.ped]
        self.len = np.array([sum(t.values()) for t in self.tf] or [1], dtype=np.float32)
        self.avg = float(self.len.mean()) or 1.0
        df = Counter(t for tf in self.tf for t in tf)
        n = max(len(self.tf), 1)
        self.idf = {t: float(np.log(1 + (n - d + 0.5) / (d + 0.5))) for t, d in df.items()}
        self.vec = np.zeros((len(self.ped), DIM), dtype=np.float32)
        self.tem_vec = np.zeros(len(self.ped), dtype=bool)
        ok = {x["id"]: x["vec"] for x in rows if x["vec"]}
        for i, p in enumerate(self.ped):
            if p["id"] in ok:
                self.vec[i] = np.frombuffer(ok[p["id"]], dtype=np.float32)
                self.tem_vec[i] = True
        # Centrar: tirar a média dos vetores (e da consulta) separa muito mais o relevante do vizinho de assunto; sem isso
        # o cosseno de qualquer receita de vídeo com qualquer pedido de vídeo fica em 0,6 a 0,8 (medido no gabarito).
        self.media = self.vec[self.tem_vec].mean(0) if self.tem_vec.any() else np.zeros(DIM, dtype=np.float32)
        if self.tem_vec.any():
            self.vec[self.tem_vec] -= self.media
            self.vec[self.tem_vec] /= np.linalg.norm(self.vec[self.tem_vec], axis=1, keepdims=True) + 1e-9

    def bm25(self, consulta):
        q = set(tokens(consulta))
        nota = np.zeros(len(self.ped), dtype=np.float32)
        for t in q:
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
    return bool(i and i.ped)


def _ranque(nota, mascara, minimo, n=50):
    idx = np.where(mascara & (nota > minimo))[0]
    return list(idx[np.argsort(-nota[idx], kind="stable")][:n])


def notas(ix, consulta, mascara, embed=embeddar, vetor=None):
    """(bm25, cosseno, rrf) de cada pedaço do índice para a consulta, dentro da máscara. Compartilhado com o hub.busca."""
    bm = ix.bm25(consulta)
    cos = np.zeros(len(ix.ped), dtype=np.float32)
    if vetor is None and ix.tem_vec.any():
        try:
            vetor = embed([consulta[:6000]])[0]
        except Exception:
            vetor = None
    if vetor is not None and ix.tem_vec.any():
        q = np.asarray(vetor, dtype=np.float32) - ix.media
        cos = ix.vec @ (q / (np.linalg.norm(q) or 1.0))
    rrf = np.zeros(len(ix.ped), dtype=np.float32)
    for lista in (_ranque(bm, mascara, 0), _ranque(cos, mascara & ix.tem_vec, 0.1)):
        for pos, i in enumerate(lista):
            rrf[i] += 1.0 / (RRF_K + pos + 1)
    return bm, cos, rrf


def buscar(consulta, projeto=None, tema=None, limite=3, db=None, embed=embeddar, vetor=None, genericas=True):
    """Receitas mais relevantes (BM25 + embeddings por RRF), cada uma com as notas do melhor pedaço:
    [{caminho, titulo, tema, projetos, status, licenca, data, rrf, cos, bm25, secao, trecho, onde_aplicar}].
    projeto filtra receitas que citam o projeto em "Onde aplicar" (e, com `genericas`, as que não nomeiam nenhum); tema filtra a pasta.
    vetor: embedding da consulta já pronto (testes); senão chama `embed` e, se falhar, só BM25."""
    ix = _indice(db)
    if not ix or not ix.ped or not (consulta or "").strip():
        return []
    mascara = np.array([(not tema or ix.rec[p["caminho"]]["tema"] == tema)
                        and (not projeto or (genericas and not ix.rec[p["caminho"]]["projetos"]) or projeto in ix.rec[p["caminho"]]["projetos"])
                        for p in ix.ped], dtype=bool)
    if not mascara.any():
        return []
    bm, cos, rrf = notas(ix, consulta, mascara, embed, vetor)
    por = {}
    for i in np.where(mascara & (rrf > 0))[0]:
        por.setdefault(ix.ped[i]["caminho"], []).append(i)
    saida = []
    for cam, idx in por.items():
        idx = sorted(idx, key=lambda i: -rrf[i])
        melhor = idx[0]
        r = ix.rec[cam]
        onde = next((ix.ped[i]["texto"] for i in range(len(ix.ped)) if ix.ped[i]["caminho"] == cam
                     and ix.ped[i]["secao"] == "onde_aplicar"), "")
        saida.append({"caminho": cam, "titulo": r["titulo"], "tema": r["tema"], "projetos": r["projetos"],
                      "status": r["status"], "licenca": r["licenca"], "data": r["data"],
                      "rrf": float(rrf[melhor] + 0.5 * sum(rrf[i] for i in idx[1:2])),
                      "cos": float(max(cos[i] for i in idx)), "bm25": float(max(bm[i] for i in idx)),
                      "secao": ix.ped[melhor]["secao"], "trecho": ix.ped[melhor]["texto"], "onde_aplicar": onde,
                      "reproducao": next((ix.ped[i]["texto"] for i in sorted(idx, key=lambda i: -rrf[i])
                                          if ix.ped[i]["secao"] == "reproducao"), "")})
    saida.sort(key=lambda s: -s["rrf"])
    return saida[:limite]


def relevantes(consulta, limite=LIMITE_DESPACHO, db=None, embed=embeddar, vetor=None):
    """As até `limite` receitas cujo melhor pedaço passa do limiar de cosseno; [] se nenhuma (melhor nada do que receita
    errada). Sem o vetor da consulta (chave ausente, OpenRouter fora) também devolve []: a nota BM25 sozinha não separa o
    relevante do vizinho de assunto (precisão@3 de 0,24 a 0,62 no gabarito), então não vale injetar.
    Sem filtro de projeto: no gabarito ele tirou recall (0,52 → 0,42) sem ganhar precisão; o filtro existe em `buscar` para o MCP."""
    achados = buscar(consulta, limite=max(limite * 3, 10), db=db, embed=embed, vetor=vetor)
    return [s for s in achados if s["cos"] >= LIMIAR_COS][:limite]


def buscar_tela(termo, tema=None, projeto=None, limite=20, r=None, db=None):
    """O que o MCP e a tela da Biblioteca usam: com termo e índice, a busca híbrida (sem o limiar do despacho: só descarta o
    que não casou em nada); sem termo ou sem índice, a busca por texto de biblioteca.buscar. Mesmo formato dos dois."""
    if not (termo or "").strip() or not disponivel(db):
        return B.buscar(termo, tema, projeto, limite, r)
    achados = [s for s in buscar(termo, projeto, tema, limite=limite, db=db, genericas=False)
               if s["bm25"] > 0 or s["cos"] >= LIMIAR_COS - 0.1]
    por = {x["caminho"]: x for x in B.receitas(r)}
    return [{**{k: v for k, v in por[s["caminho"]].items() if k not in ("texto", "_busca")},
             "trecho": " ".join(s["trecho"].split())[:240]} for s in achados if s["caminho"] in por]


# ---- bloco do prompt ----

BLOCO = "## Receitas da biblioteca que se aplicam"
RE_URL = re.compile(r"https?://\S+")
RE_PREAMBULO = re.compile(r"(execute|use|leia)\b.*\b(skill|skills|antes)\b", re.I)
CORTE_DESCRICAO = 400  # só a abertura do pedido: o resto são passos de execução que diluem o sentido (medido no gabarito)


RUIDO_LABELS = {"merge", "codex", "visual", "proposta", "esteira", "melhoria", "fase-final", "claude"}


def consulta_da_issue(titulo, descricao, labels=()):
    """Título, abertura da descrição (sem URLs nem a linha de preâmbulo de skill) e as labels."""
    desc = RE_URL.sub(" ", descricao or "")
    desc = "\n".join(l for l in desc.splitlines() if not RE_PREAMBULO.match(l.strip()))  # "Execute com a skill…", "Leia antes…"
    return f"{titulo}\n{desc[:CORTE_DESCRICAO]}\n{' '.join(l for l in labels if l.lower() not in RUIDO_LABELS and not l.lower().startswith(('plano-', 'skill:')))}".strip()


def _recorte(texto, n, sem_titulo=False):
    t = texto.strip()
    if sem_titulo and "\n" in t:  # o pedaço começa pela linha do cabeçalho da seção
        t = t.split("\n", 1)[1].strip()
    return t if len(t) <= n else t[:n].rsplit("\n", 1)[0].rstrip() + "\n…"


def bloco_prompt(achados):
    """O bloco colado no fim do prompt: título, "onde aplicar" e o trecho de reprodução de cada receita."""
    if not achados:
        return ""
    linhas = [BLOCO, "",
              "O hub achou estas receitas (BM25 + embeddings sobre o seu pedido). Use o que serve; o que não serve, ignore. "
              f"Leia inteira com `ler_receita` (MCP) ou em {data()}/reports/biblioteca/<caminho>. "
              f"**Se usar, cite no corpo do PR uma linha por receita: `Receita: receitas/<tema>/<arquivo>.md`** (o hub conta e mostra no {config.get('produto.nome')}).", ""]
    for s in achados:
        linhas += [f"### {s['titulo']}", f"`{s['caminho']}` · {s['status'] or '—'} · nota {s['cos']:.2f}", ""]
        if s["onde_aplicar"]:
            linhas += ["Onde aplicar:", _recorte(s["onde_aplicar"], 500, True), ""]
        corpo = s["reproducao"] or s["trecho"]
        linhas += ["Reprodução:" if s["reproducao"] else "Trecho:", _recorte(corpo, 1200, bool(s["reproducao"])), ""]
        if "curta" in (s["status"] or "").lower():
            linhas += ["Receita curta: ao usar pela 1ª vez, complete a extração (original, reprodução, onde aplicar) e grave a "
                       f"versão completa na main de {config.repo('biblioteca')} (status `completo`).", ""]
    return "\n".join(linhas).rstrip()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["atualizar"]:
        print(json.dumps(atualizar(argv[1] if len(argv) > 1 else None)))
        return 0
    if argv[:1] == ["buscar"]:
        for s in buscar(" ".join(argv[1:])):
            print(f"{s['rrf']:.4f} cos={s['cos']:.2f} bm25={s['bm25']:.1f} {'ok' if s['cos'] >= LIMIAR_COS else '--'} {s['caminho']}")
        return 0
    if argv[:1] == ["gabarito"]:
        from hub import biblioteca_gabarito as G
        return G.main(argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
