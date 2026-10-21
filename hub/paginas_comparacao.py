"""Páginas de comparação e citação por IA: lista de termos, esqueleto de rascunho e portão de qualidade (stdlib).

    python3 -m hub.paginas_comparacao termos --gsc consultas.csv [--concorrentes "Concorrente A,Concorrente B"]
        [--paginas paginas.txt] [--lugares "Bairro Alfa"] [--atendemos "Bairro Alfa"] [--min-impressoes 20] [--saida termos.json]
    python3 -m hub.paginas_comparacao rascunhos termos.json --pasta rascunhos/ --marca "Marca Exemplo" [--idioma pt|en]
    python3 -m hub.paginas_comparacao portao rascunho.md [outro.md ...] [--idioma pt|en]

`termos`: lê o CSV de consultas do Search Console (query, clicks, impressions, position, page; aceita cabeçalhos em
português e inglês), separa o que já tem página (reforçar) do que não tem (criar), classifica cada busca por molde
(vs, alternativas, melhor-em-local, dor-servico, pergunta, funciona, nominal) e descarta lugar sem atendimento declarado.
Busca com nome de concorrente vai para a lista do molde nominal e nunca é criada sem fonte pública aberta.
`rascunhos`: um esqueleto em Markdown por termo, com campos de fato, fonte e data a preencher. Não escreve nenhum fato.
`portao`: valida o rascunho e sai com código 1 se houver erro. Regras (cada uma com teste em tests/test_paginas_comparacao.py):
campos obrigatórios, marcador não preenchido, fato sem fonte, promessa de resultado, superlativo ou primeiro lugar da
própria marca, lugar sem atendimento, linguagem que denigre, "não tem" sobre concorrente, concorrente sem "onde ganha",
regras do conselho (CFP, COFFITO, OAB), data da checagem e páginas irmãs parecidas demais (Jaccard, limite do doorway).
Referência jurídica brasileira para o método (não é consultoria): CONAR art. 32, CDC arts. 36 e 37, Lei 9.279/1996 art. 195.
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path

LIMITE_JACCARD = 0.5  # páginas irmãs com texto parecido demais viram doorway
MAX_PALAVRAS_RESPOSTA = 95
VALIDADE_DIAS = 90

MOLDES = ("vs", "nominal", "alternativas", "melhor-em-local", "dor-servico", "pergunta", "funciona")
COM_CONCORRENTE = ("nominal", "alternativas")
CONSELHOS = ("CFP", "COFFITO", "OAB")

PALAVRAS_VAZIAS = set("""a o as os um uma de da do das dos em no na nos nas para pra por com sem e ou que qual quais
como onde quando se me meu minha the of and for in to is are do does""".split())

RE_ALTERNATIVAS = re.compile(r"\balternativ[ao]s?\s+(a|ao|aos|à|para)\b|\bsubstitut[oa]s?\s+(de|do|da)\b|\bsimilares?\s+(a|ao)\b|\balternatives?\s+to\b|\bsimilar\s+to\b")
RE_VS = re.compile(r"\b(vs\.?|versus|x)\b|\s(ou|or)\s")
RE_LOCAL = re.compile(r"\bperto de mim\b|\bnear me\b|\bmelhor(es)?\b.+\b(em|na|no|de)\s+\S+|\bbest\b.+\b(in|near)\s+\S+")
RE_PERGUNTA = re.compile(r"^(como|o que|por que|porque|qual|quais|quanto|quanto custa|quando|onde|quem|posso|devo|é normal|how|what|why|which|when|where|who|can|should|does|is)\b|\?$")
RE_FUNCIONA = re.compile(r"\b(funciona|funcionam|adianta|vale a pena|works?|is it worth)\b")
RE_SINTOMA = re.compile(r"\b(dor|dores|sintomas?|pain|ache|symptoms?|inchaco|formigamento)\b")

# Promessa de resultado, nos dois idiomas. Sempre erro.
RE_PROMESSA = re.compile(
    r"\bgarantid[oa]s?\b|\bgarantimos\b|\b100\s?%|\bresultados?\s+garantid|\bcura(mos|r)?\b|\bsem\s+risco\b|\bmilagr|\bguarantee[sd]?\b|"
    r"\bcures?\b|\brisk[- ]free\b|\bmiracle\b", re.I)
RE_SUPERLATIVO = re.compile(
    r"\bo\s+melhor\b|\ba\s+melhor\b|\blíder\b|\blider\b|\bnúmero\s+1\b|\bnumero\s+1\b|#\s?1\b|\bmais\s+indicad[oa]\b|"
    r"\bno\s+1\b|\bthe\s+best\b|\bleading\b|\bnumber\s+one\b|\bmost\s+recommended\b", re.I)
RE_DENIGRE = re.compile(
    r"\bcharlat[aã]o|\bcharlatanismo\b|\bgolpe\b|\bfraude\b|\bpicaretagem\b|\bpicareta\b|\bpseudoci[eê]ncia\b|\bscam\b|\bfraud\b|\bquack|\brip-?off\b|\bengana\b", re.I)
RE_NAO_TEM = re.compile(r"\b(n[ãa]o\s+(tem|possui|existe|oferece|faz|atende)|(has|have)\s+no|does\s+not\s+(have|offer|exist)|doesn't\s+(have|offer))\b", re.I)
RE_VIVENCIA = re.compile(
    r"\b(no\s+consult[óo]rio\s+(eu|a\s+gente|vejo)|vejo\s+(muito|com\s+frequ[êe]ncia)|minhas?\s+(pacientes?|clientes?)|nossos\s+(pacientes|clientes)\s+costumam|"
    r"i\s+(often\s+)?see\s+in\s+my\s+(clinic|practice)|my\s+(patients|clients))\b", re.I)
RE_ANTES_DEPOIS = re.compile(r"\bantes\s+e\s+depois\b|\bdepoimentos?\b|\btestimonials?\b|\bbefore\s+and\s+after\b", re.I)
RE_PRECO = re.compile(r"R\$\s?\d|\bUS\$\s?\d|\bhonor[áa]rios\b|\bgratuit[oa]\b|\bfree\s+consultation\b", re.I)

MSG = {
    "pt": {
        "campo": "campo obrigatório ausente: {x}",
        "molde": "molde desconhecido: {x} (válidos: {y})",
        "titulo": "falta o título (linha que começa com '# ')",
        "resposta": "falta a seção 'Resposta direta' com texto",
        "resposta_longa": "resposta direta com {x} palavras (máx {y})",
        "marcador": "marcador não preenchido: {x}",
        "fato_sem_fonte": "fato sem fonte (use [fonte: https://...]): {x}",
        "linha_sem_fonte": "linha da tabela sem URL na coluna de fonte: {x}",
        "promessa": "promessa de resultado: '{x}'",
        "superlativo": "superlativo da própria marca ('{x}'): critério declarado, sem 'o melhor'",
        "primeiro": "a própria marca em 1º numa lista ('{x}'): ordem por critério declarado, marca fora do 1º lugar",
        "criterio": "molde {x} exige a linha 'Critério:' declarando como a lista foi ordenada",
        "lugar": "lugar '{x}' sem atendimento declarado (atendimento: sim)",
        "denigre": "linguagem que denigre ou acusa sem prova: '{x}'",
        "nao_tem": "afirmação 'não tem/não possui' sobre o concorrente: use 'não informa' e a fonte consultada ('{x}')",
        "ganha": "falta a seção 'Onde o concorrente ganha' com ao menos um item",
        "vivencia": "experiência clínica ou de caso em primeira pessoa (sem aval): '{x}'",
        "antes_depois": "antes e depois ou depoimento ({c}): '{x}'",
        "preco": "preço, honorários ou gratuidade ({c}): '{x}'",
        "data_invalida": "verificado_em inválido (use AAAA-MM-DD): {x}",
        "data_futura": "verificado_em no futuro: {x}",
        "data_velha": "verificado_em com mais de {y} dias: {x}",
        "irma": "texto parecido demais com {x} (Jaccard {y}, limite {z}): uma pergunta diferente por página",
        "conselho": "conselho desconhecido: {x} (válidos: none, {y})",
        "ok": "ok", "reprovado": "REPROVADO", "erro": "ERRO", "aviso": "aviso",
    },
    "en": {
        "campo": "missing required field: {x}",
        "molde": "unknown template: {x} (valid: {y})",
        "titulo": "missing title (a line starting with '# ')",
        "resposta": "missing 'Direct answer' section with text",
        "resposta_longa": "direct answer has {x} words (max {y})",
        "marcador": "placeholder left unfilled: {x}",
        "fato_sem_fonte": "fact without source (use [source: https://...]): {x}",
        "linha_sem_fonte": "table row without a URL in the source column: {x}",
        "promessa": "promise of results: '{x}'",
        "superlativo": "superlative about your own brand ('{x}'): declare the criterion, no 'the best'",
        "primeiro": "your own brand ranked 1st in a list ('{x}'): order by a declared criterion, brand not first",
        "criterio": "template {x} needs a 'Criterion:' line stating how the list was ordered",
        "lugar": "place '{x}' without declared service there (atendimento: sim)",
        "denigre": "disparaging or unproven accusation: '{x}'",
        "nao_tem": "claim 'has no / does not offer' about the competitor: say 'does not list' and name the source consulted ('{x}')",
        "ganha": "missing 'Where the competitor wins' section with at least one item",
        "vivencia": "first-person clinical or case experience (unapproved): '{x}'",
        "antes_depois": "before and after or testimonial ({c}): '{x}'",
        "preco": "price, fees or free offer ({c}): '{x}'",
        "data_invalida": "invalid verificado_em (use YYYY-MM-DD): {x}",
        "data_futura": "verificado_em in the future: {x}",
        "data_velha": "verificado_em older than {y} days: {x}",
        "irma": "text too similar to {x} (Jaccard {y}, limit {z}): one different question per page",
        "conselho": "unknown professional board: {x} (valid: none, {y})",
        "ok": "ok", "reprovado": "REJECTED", "erro": "ERROR", "aviso": "warning",
    },
}


def normalizar(texto):
    sem = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sem if unicodedata.category(c) != "Mn").lower()


def palavras(texto):
    return [p for p in re.split(r"[^a-z0-9]+", normalizar(texto)) if p]


def termos_da_busca(busca):
    return [p for p in palavras(busca) if p not in PALAVRAS_VAZIAS and (len(p) >= 3 or p.isdigit())]


def _casa(termo, conjunto):
    if termo in conjunto:
        return True
    if len(termo) < 5:
        return False
    raiz = termo[:-2]
    return any(p.startswith(raiz) or (len(p) >= 5 and termo.startswith(p[:-2])) for p in conjunto)


def cobre(busca, *textos):
    """A página (url, título) cobre ao menos metade dos termos da busca."""
    termos = termos_da_busca(busca)
    if not termos:
        return False
    conj = set()
    for t in textos:
        conj.update(palavras(t or ""))
    return sum(_casa(t, conj) for t in termos) * 2 >= len(termos)


# ---------- termos ----------
CABECALHOS = {
    "query": ("query", "consulta", "consultas", "top queries", "principais consultas", "consultas principais", "termo"),
    "clicks": ("clicks", "cliques"),
    "impressions": ("impressions", "impressoes", "impressões"),
    "position": ("position", "posicao", "posição"),
    "page": ("page", "pagina", "página", "url"),
}


def _col(cabecalho, campo):
    alvos = {normalizar(x) for x in CABECALHOS[campo]}
    for i, c in enumerate(cabecalho):
        if normalizar(c).strip() in alvos:
            return i
    return None


def _numero(v):
    v = (v or "").strip().replace("%", "")
    if not v:
        return 0.0
    if "," in v and "." not in v:
        v = v.replace(",", ".")
    elif "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return 0.0


def ler_gsc(caminho):
    with open(caminho, newline="", encoding="utf-8-sig") as f:
        linhas = list(csv.reader(f))
    if not linhas:
        return []
    cab = linhas[0]
    idx = {c: _col(cab, c) for c in CABECALHOS}
    if idx["query"] is None or idx["impressions"] is None:
        raise ValueError("o CSV precisa de colunas de consulta e impressões (query/consulta, impressions/impressões)")
    saida = []
    for ln in linhas[1:]:
        if not ln or idx["query"] >= len(ln):
            continue
        pegar = lambda c: ln[idx[c]] if idx[c] is not None and idx[c] < len(ln) else ""  # noqa: E731
        saida.append({"query": pegar("query").strip(), "clicks": _numero(pegar("clicks")),
                      "impressions": _numero(pegar("impressions")), "position": _numero(pegar("position")),
                      "page": pegar("page").strip()})
    return [r for r in saida if r["query"]]


def _citado(busca_norm, nomes):
    for n in nomes:
        nn = normalizar(n).strip()
        if nn and re.search(r"(?<![a-z0-9])" + re.escape(nn) + r"(?![a-z0-9])", busca_norm):
            return n
    return None


def classificar(consulta, concorrentes=(), lugares=()):
    """Devolve (molde, concorrente, lugar) para uma busca, ou (None, None, None) se não serve a nenhum molde."""
    q = normalizar(consulta)
    conc = _citado(q, concorrentes)
    lugar = _citado(q, lugares)
    if conc and RE_ALTERNATIVAS.search(q):
        return "alternativas", conc, lugar
    if conc:
        return "nominal", conc, lugar
    if RE_ALTERNATIVAS.search(q):
        return "alternativas", None, lugar
    if RE_VS.search(q) and len(termos_da_busca(q)) >= 2:
        return "vs", None, lugar
    if lugar or RE_LOCAL.search(q):
        return "melhor-em-local", None, lugar
    if RE_FUNCIONA.search(q):
        return "funciona", None, lugar
    if RE_SINTOMA.search(q):
        return "dor-servico", None, lugar
    if RE_PERGUNTA.search(q):
        return "pergunta", None, lugar
    return None, None, None


def listar_termos(linhas, concorrentes=(), lugares=(), atendemos=(), paginas=(), min_impressoes=20):
    """paginas: lista de (url, titulo). Agrupa por busca e prioriza por impressões."""
    por_busca = {}
    for r in linhas:
        b = por_busca.setdefault(normalizar(r["query"]), {"termo": r["query"], "cliques": 0, "impressoes": 0, "pos": 0.0, "pages": set()})
        b["cliques"] += r["clicks"]
        b["impressoes"] += r["impressions"]
        b["pos"] += r["position"] * r["impressions"]
        if r["page"]:
            b["pages"].add(r["page"])
    atende = {normalizar(a) for a in atendemos}
    itens = []
    for b in por_busca.values():
        if b["impressoes"] < min_impressoes:
            continue
        molde, conc, lugar = classificar(b["termo"], concorrentes, lugares)
        if not molde:
            continue
        coberta = any(cobre(b["termo"], url, titulo) for url, titulo in paginas) or any(cobre(b["termo"], p) for p in b["pages"])
        acao, motivo = ("reforcar", "já existe página que cobre a busca: reforçar a existente, não criar outra") if coberta else ("criar", "sem página que cubra a busca")
        if (lugar or molde == "melhor-em-local") and not (lugar and normalizar(lugar) in atende):
            acao, motivo = "descartar", "lugar sem atendimento declarado (--atendemos): não criar página de cidade ou bairro"
        if conc and acao == "criar":
            motivo += "; molde com concorrente: só com fonte pública aberta, nasce fora do ar até revisão"
        itens.append({"termo": b["termo"], "molde": molde, "concorrente": conc, "lugar": lugar, "acao": acao, "motivo": motivo,
                      "impressoes": int(b["impressoes"]), "cliques": int(b["cliques"]),
                      "posicao": round(b["pos"] / b["impressoes"], 1) if b["impressoes"] else 0})
    ordem = {"criar": 0, "reforcar": 1, "descartar": 2}
    return sorted(itens, key=lambda i: (ordem[i["acao"]], -i["impressoes"], i["termo"]))


# ---------- rascunhos ----------
def esqueleto(item, marca, idioma="pt"):
    en = idioma == "en"
    conc = item.get("concorrente") or ""
    lugar = item.get("lugar") or ""
    cabeca = [
        "---",
        f"molde: {item['molde']}",
        f"termo: {item['termo']}",
        f"marca: {marca}",
        f"concorrente: {conc}",
        f"lugar: {lugar}",
        f"atendimento: {'[PREENCHER sim|nao]' if lugar else 'n/a'}",
        "conselho: [PREENCHER none|CFP|COFFITO|OAB]",
        "verificado_em: [PREENCHER AAAA-MM-DD]",
        "---",
    ]
    if en:
        corpo = [
            "# [FILL title phrased as the question people type]", "",
            "## Direct answer", "[FILL up to 80 words, self-contained, no claim without a source below]", "",
            "## Criterion", "Criterion: [FILL how the comparison or list is ordered; the brand is not first by default]", "",
            "## Facts", "- [FILL one verifiable fact] [source: https://FILL]", "",
            "## Table", "| Criterion | Brand | Other side | Source |", "| --- | --- | --- | --- |",
            "| [FILL] | [FILL] | [FILL or 'does not list'] | https://FILL |", "",
            "## Where the competitor wins", "- [FILL at least one item, with source]", "",
            "## When each one makes sense", "- [FILL]", "",
            "## FAQ", "- [FILL question people type] / [FILL answer, 2-3 sentences]", "",
            "## Internal links", "- [FILL 3 to 6 pages that exist on the site]", "",
        ]
    else:
        corpo = [
            "# [PREENCHER título em forma de pergunta que as pessoas digitam]", "",
            "## Resposta direta", "[PREENCHER até 80 palavras, auto-suficiente, sem afirmação sem fonte abaixo]", "",
            "## Critério", "Critério: [PREENCHER como a comparação ou a lista foi ordenada; a marca não vem em 1º por padrão]", "",
            "## Fatos", "- [PREENCHER um fato verificável] [fonte: https://PREENCHER]", "",
            "## Tabela", "| Critério | Marca | Outro lado | Fonte |", "| --- | --- | --- | --- |",
            "| [PREENCHER] | [PREENCHER] | [PREENCHER ou 'não informa'] | https://PREENCHER |", "",
            "## Onde o concorrente ganha", "- [PREENCHER ao menos um item, com fonte]", "",
            "## Quando cada um faz mais sentido", "- [PREENCHER]", "",
            "## Perguntas frequentes", "- [PREENCHER pergunta que as pessoas digitam] / [PREENCHER resposta, 2 a 3 frases]", "",
            "## Links internos", "- [PREENCHER 3 a 6 páginas que existem no site]", "",
        ]
    return "\n".join(cabeca + [""] + corpo)


def slug(texto):
    return re.sub(r"[^a-z0-9]+", "-", normalizar(texto)).strip("-")[:80] or "pagina"


# ---------- portão ----------
def ler_rascunho(texto):
    meta, corpo = {}, texto
    m = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", texto, re.S)
    if m:
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                meta[k.strip()] = v.strip()
        corpo = m.group(2)
    return meta, corpo


def secoes(corpo):
    """Mapa título normalizado da seção (## ...) para o texto dela."""
    saida, atual = {}, None
    for ln in corpo.splitlines():
        if ln.startswith("## "):
            atual = normalizar(ln[3:]).strip()
            saida[atual] = []
        elif atual is not None:
            saida[atual].append(ln)
    return {k: "\n".join(v).strip() for k, v in saida.items()}


def _pega(secs, *nomes):
    for n in nomes:
        for k, v in secs.items():
            if k.startswith(normalizar(n)):
                return v
    return None


def _trecho(texto, m, n=40):
    return texto[max(0, m.start() - 10): m.end() + n].replace("\n", " ").strip()


def portao(texto, idioma="pt", hoje=None):
    """Devolve (erros, avisos), listas de mensagens no idioma pedido."""
    t = MSG[idioma]
    erros, avisos = [], []
    hoje = hoje or dt.date.today()
    meta, corpo = ler_rascunho(texto)
    secs = secoes(corpo)

    for c in ("molde", "termo", "marca", "verificado_em"):
        if not meta.get(c):
            erros.append(t["campo"].format(x=c))
    molde = meta.get("molde", "")
    if molde and molde not in MOLDES:
        erros.append(t["molde"].format(x=molde, y=", ".join(MOLDES)))
    if molde in COM_CONCORRENTE and not meta.get("concorrente"):
        erros.append(t["campo"].format(x="concorrente"))
    conselho = meta.get("conselho", "none") or "none"
    if conselho not in ("none",) + CONSELHOS and "PREENCHER" not in conselho:
        erros.append(t["conselho"].format(x=conselho, y=", ".join(CONSELHOS)))

    if not re.search(r"^# \S", corpo, re.M):
        erros.append(t["titulo"])
    resposta = _pega(secs, "resposta direta", "direct answer")
    if not resposta:
        erros.append(t["resposta"])
    elif len(resposta.split()) > MAX_PALAVRAS_RESPOSTA:
        avisos.append(t["resposta_longa"].format(x=len(resposta.split()), y=MAX_PALAVRAS_RESPOSTA))

    for m in re.finditer(r"\[(?:PREENCHER|FILL)[^\]]*\]", texto):
        erros.append(t["marcador"].format(x=m.group(0)[:40]))
        break
    if re.search(r"\bhttps?://(?:PREENCHER|FILL)\b", texto):
        erros.append(t["marcador"].format(x="https://PREENCHER"))

    # fato sem fonte
    fatos = _pega(secs, "fatos", "facts") or ""
    for ln in fatos.splitlines():
        if ln.lstrip().startswith(("-", "*")) and not re.search(r"\[(?:fonte|source):\s*https?://\S+\]", ln, re.I):
            erros.append(t["fato_sem_fonte"].format(x=ln.strip()[:70]))
    tabela = _pega(secs, "tabela", "table") or ""
    for ln in tabela.splitlines():
        if ln.startswith("|") and not re.match(r"^\|\s*:?-{2,}", ln) and not re.match(r"^\|\s*(crit[eé]rio|criterion)\b", ln, re.I):
            if not re.search(r"https?://\S+\s*\|?\s*$", ln):
                erros.append(t["linha_sem_fonte"].format(x=ln.strip()[:70]))

    # linguagem
    m = RE_PROMESSA.search(corpo)
    if m:
        erros.append(t["promessa"].format(x=_trecho(corpo, m)))
    m = RE_DENIGRE.search(corpo)
    if m:
        erros.append(t["denigre"].format(x=m.group(0)))

    marca = meta.get("marca", "")
    marca_n = normalizar(marca).strip()
    for frase in re.split(r"(?<=[.!?])\s+|\n", corpo):
        if marca_n and marca_n in normalizar(frase):
            m = RE_SUPERLATIVO.search(frase)
            if m:
                erros.append(t["superlativo"].format(x=m.group(0)))
                break
    if marca_n:
        for m in re.finditer(r"^\s*(?:1[.)]|#1|1º|1st)\s*[-:]?\s*\**(.+)$", corpo, re.M):
            if marca_n in normalizar(m.group(1)):
                erros.append(t["primeiro"].format(x=m.group(0).strip()[:60]))
                break
    if molde in ("alternativas", "melhor-em-local") and not re.search(r"^\s*(crit[eé]rio|criterion)\s*:\s*\S{3,}", corpo, re.M | re.I):
        erros.append(t["criterio"].format(x=molde))
    if molde == "melhor-em-local" and not meta.get("lugar"):
        erros.append(t["campo"].format(x="lugar"))

    lugar = meta.get("lugar", "")
    if lugar and meta.get("atendimento", "").strip().lower() not in ("sim", "yes"):
        erros.append(t["lugar"].format(x=lugar))

    conc = meta.get("concorrente", "")
    if conc:
        conc_n = normalizar(conc).strip()
        for frase in re.split(r"(?<=[.!?])\s+|\n", corpo):
            if conc_n in normalizar(frase) and RE_NAO_TEM.search(frase):
                erros.append(t["nao_tem"].format(x=frase.strip()[:70]))
                break
        ganha = _pega(secs, "onde o concorrente ganha", "where the competitor wins") or ""
        if not re.search(r"^\s*[-*]\s*\S", ganha, re.M) and molde in COM_CONCORRENTE + ("vs",):
            erros.append(t["ganha"])

    # conselho profissional
    if conselho in CONSELHOS:
        m = RE_ANTES_DEPOIS.search(corpo)
        if m:
            erros.append(t["antes_depois"].format(c=conselho, x=m.group(0)))
        if conselho in ("CFP", "COFFITO"):
            m = RE_VIVENCIA.search(corpo)
            if m:
                erros.append(t["vivencia"].format(x=_trecho(corpo, m)))
        if conselho == "OAB":
            m = RE_PRECO.search(corpo)
            if m:
                erros.append(t["preco"].format(c=conselho, x=m.group(0)))
    elif conselho == "none":
        m = RE_ANTES_DEPOIS.search(corpo)
        if m:
            avisos.append(t["antes_depois"].format(c="aviso", x=m.group(0)))

    # data da checagem
    v = meta.get("verificado_em", "")
    if v and "PREENCHER" not in v and "FILL" not in v:
        try:
            d = dt.date.fromisoformat(v)
            if d > hoje:
                erros.append(t["data_futura"].format(x=v))
            elif (hoje - d).days > VALIDADE_DIAS:
                avisos.append(t["data_velha"].format(x=v, y=VALIDADE_DIAS))
        except ValueError:
            erros.append(t["data_invalida"].format(x=v))
    return erros, avisos


def jaccard(a, b):
    ca, cb = set(palavras(a)), set(palavras(b))
    return len(ca & cb) / len(ca | cb) if ca | cb else 0.0


def irmas(textos, idioma="pt", limite=LIMITE_JACCARD):
    """textos: {nome: texto}. Pares com Jaccard acima do limite, só sobre o corpo (sem o cabeçalho)."""
    corpos = {n: ler_rascunho(x)[1] for n, x in textos.items()}
    nomes = sorted(corpos)
    achados = []
    for i, a in enumerate(nomes):
        for b in nomes[i + 1:]:
            j = jaccard(corpos[a], corpos[b])
            if j >= limite:
                achados.append((a, b, MSG[idioma]["irma"].format(x=b, y=f"{j:.2f}", z=limite)))
    return achados


# ---------- CLI ----------
def _lista(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _paginas(caminho):
    if not caminho:
        return []
    saida = []
    for ln in Path(caminho).read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#"):
            url, _, titulo = ln.partition("\t")
            saida.append((url.strip(), titulo.strip()))
    return saida


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paginas_comparacao", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("termos")
    t.add_argument("--gsc", required=True)
    t.add_argument("--concorrentes", default="")
    t.add_argument("--lugares", default="")
    t.add_argument("--atendemos", default="")
    t.add_argument("--paginas")
    t.add_argument("--min-impressoes", type=int, default=20)
    t.add_argument("--saida")
    r = sub.add_parser("rascunhos")
    r.add_argument("termos")
    r.add_argument("--pasta", required=True)
    r.add_argument("--marca", required=True)
    r.add_argument("--idioma", choices=("pt", "en"), default="pt")
    r.add_argument("--so-criar", action="store_true", help="só os termos com ação 'criar' (padrão)")
    p = sub.add_parser("portao")
    p.add_argument("arquivos", nargs="+")
    p.add_argument("--idioma", choices=("pt", "en"), default="pt")
    a = ap.parse_args(argv)

    if a.cmd == "termos":
        itens = listar_termos(ler_gsc(a.gsc), _lista(a.concorrentes), _lista(a.lugares), _lista(a.atendemos),
                              _paginas(a.paginas), a.min_impressoes)
        print("| ação | molde | termo | impressões | posição | concorrente |\n| --- | --- | --- | ---: | ---: | --- |")
        for i in itens:
            print(f"| {i['acao']} | {i['molde']} | {i['termo']} | {i['impressoes']} | {i['posicao']} | {i['concorrente'] or ''} |")
        if a.saida:
            Path(a.saida).write_text(json.dumps(itens, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if a.cmd == "rascunhos":
        itens = json.loads(Path(a.termos).read_text(encoding="utf-8"))
        pasta = Path(a.pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        n = 0
        for i in itens:
            if i.get("acao") != "criar":
                continue
            (pasta / f"{slug(i['termo'])}.md").write_text(esqueleto(i, a.marca, a.idioma), encoding="utf-8")
            n += 1
        print(f"{n} rascunhos em {pasta}")
        return 0
    falhou = False
    textos = {}
    msg = MSG[a.idioma]
    for arq in a.arquivos:
        texto = Path(arq).read_text(encoding="utf-8")
        textos[arq] = texto
        erros, avisos = portao(texto, a.idioma)
        print(f"{msg['reprovado'] if erros else msg['ok']}  {arq}")
        for e in erros:
            print(f"   {msg['erro']}  {e}")
        for w in avisos:
            print(f"   {msg['aviso']}  {w}")
        falhou = falhou or bool(erros)
    for x, y, m in irmas(textos, a.idioma):
        print(f"{msg['reprovado']}  {x} / {y}\n   {msg['erro']}  {m}")
        falhou = True
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
