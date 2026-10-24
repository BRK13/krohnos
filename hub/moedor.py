"""Moedor público: referências conferidas de fonte aberta, a custo ZERO.

Regra do dono (18/10): gasto ZERO. NENHUM modelo roda aqui: as consultas saem de template sobre os nossos temas, o
trecho sai por código (frase da página que cita o tema) e a verificação é determinística (o trecho existe na página lida).

Backend padrão, sem custo e sem prazo: OpenAlex (busca, com autor, ano e DOI vindos da própria base) + leitura direta
da página (robots.txt respeitado, ≤ 2 req/s por domínio, User-Agent identificado). Continua depois de 31/10.

Backend DeepSeek (Search/Fetch de `/v1/tools/*`): DESLIGADO, só com MOEDOR_DEEPSEEK=1. Em 18/10 o saldo da chave caiu
de US$ 1,95 para 1,91 durante os testes, inclusive uma queda sem nenhuma chamada nossa (outro consumidor na chave);
não se provou que o Search/Fetch cobra, e a regra é gasto zero: só liga se o orquestrador liberar. Com ele ligado, o
saldo é lido de GET /user/balance antes de cada lote e a cada 100 chamadas; caiu 1 centavo desde a linha de base, ou
ficou abaixo de US$ 0,50, ou passou de 31/10 12h BRT, a faixa para (FaixaParada). Nunca recarrega.

    python3 -m hub.moedor lote <sementes.json> --repo <pasta do repo> [--max N]   # busca, lê, confere, grava
    python3 -m hub.moedor estado                                                   # contadores, saldo e se está ligada
    python3 -m hub.moedor juntar <estado.json>...                                  # soma ao estado oficial o de um lote que gravou fora

Só assunto público: a semente traz tema e consulta, nunca dado de cliente, paciente ou usuário. Saída por tema em
`<repo>/docs/referencias/<tema>.jsonl` (+ `.md`). Sem trecho conferido a referência não entra; autor e ano só quando
aparecem na própria página (senão `null`); dedupe por URL e DOI. Chave: DEEPSEEK_API_KEY (~/.config/deepseek.env).
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hub import config, data
# Cliente DeepSeek compartilhado: os nomes continuam acessíveis como `moedor.<nome>`.
from hub.comum.deepseek import API, ENV_CHAVE, QPS, FaixaParada, Limitador, _http, chave, gasto_registrado, saldo_usd  # noqa: F401
from hub.comum.deepseek import Cliente as _ClienteBase

BRT = dt.timezone(dt.timedelta(hours=-3))
DESLIGA_EM = dt.datetime(2026, 10, 31, 12, 0, tzinfo=BRT)
SALDO_MINIMO_USD = 0.50
CONFERIR_SALDO_A_CADA = 100
TRECHO_MIN, TRECHO_MAX = 60, 600
UA = f"nimbus-moedor/1 (pesquisa publica; {config.email('pesquisa')})"

# Fonte aberta de doutrina/evidência. O que não casa vira `fonte_tipo: "outra"` e só entra se o lote deixar (`aceita_outras`).
FONTES = re.compile(
    r"(scielo|\.edu\.br|\.edu(/|$)|repositorio|repositorios|periodicos\.capes|capes\.gov\.br|revista|revistas|"
    r"\.jus\.br|\.gov\.br|\.leg\.br|pubmed|ncbi\.nlm\.nih|cochrane|pedro\.org|europepmc|openalex|crossref|arxiv|"
    r"semanticscholar|doi\.org|\.ac\.[a-z]{2}(/|$)|\.uff\.br|\.usp\.br|ufrgs|unicamp)", re.I)
_TRAVA = threading.RLock()  # estado.json é lido-modificado-gravado: uma thread por vez (o flock cobre os outros processos)
NAO_DOUTRINA = re.compile(r"jurisprudencia|informativo|acordao|diario-?(oficial|da-justica)|/dje|/pauta|/noticia|loja\.|/loja/|/produto", re.I)
NAO_DOUTRINA_HOST = re.compile(r"(^|[.-])(jurisprudencia|acordaos?|jurisprudence|esaj|pje)[.-]", re.I)  # base de ementas de tribunal (jurisprudencia.tjto.jus-br.example)
NAO_DOUTRINA_CONSULTA = re.compile(r"consulta|pesquisa|resultado|ementa", re.I)
DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)]", re.I)


# ---- estado (contadores, linha de base do saldo) ----

def pasta(base_dados=None):
    return Path(base_dados or data()) / "moedor"


def _json(p, padrao):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f"{p.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")  # único por processo e thread
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _trava(base_dados=None):
    """Exclusão para ler-modificar-gravar o estado: RLock entre threads e flock entre processos."""
    with _TRAVA:
        arq = pasta(base_dados) / "estado.lock"
        arq.parent.mkdir(parents=True, exist_ok=True)
        with open(arq, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def estado(base_dados=None):
    return _json(pasta(base_dados) / "estado.json", {"base_usd": None, "chamadas": 0, "projetos": {}, "parada": None})


def hoje(quando=None):
    return (quando or dt.datetime.now(BRT)).strftime("%Y-%m-%d")


def _projeto(e, projeto):
    return e["projetos"].setdefault(projeto, {"buscas": 0, "leituras": 0, "aceitas": 0, "recusadas": 0, "motivos": {}})


def _no_dia(p, campo, n, dia):
    """Série diária: o painel mostra as aceitas por dia; os outros campos vão junto para a taxa do dia."""
    d = p.setdefault("por_dia", {}).setdefault(dia, {})
    d[campo] = d.get(campo, 0) + n


def contar(projeto, campo, n=1, base_dados=None):
    with _trava(base_dados):
        _contar(projeto, campo, n, base_dados)


def _contar(projeto, campo, n, base_dados):
    e = estado(base_dados)
    p = _projeto(e, projeto)
    p[campo] = p.get(campo, 0) + n
    if n:
        _no_dia(p, campo, n, hoje())
    _gravar(pasta(base_dados) / "estado.json", e)


def recusar(projeto, motivo, base_dados=None):
    with _trava(base_dados):
        _recusar(projeto, motivo, base_dados)


def _recusar(projeto, motivo, base_dados):
    e = estado(base_dados)
    p = _projeto(e, projeto)
    p["recusadas"] += 1
    p["motivos"][motivo] = p["motivos"].get(motivo, 0) + 1
    _no_dia(p, "recusadas", 1, hoje())
    _gravar(pasta(base_dados) / "estado.json", e)


def juntar(arquivos, base_dados=None):
    """Soma ao estado oficial os contadores de lotes que gravaram em outra pasta (HUB_DATA própria, antes de existir
    `<dados>/moedor` com dono claude). Cada arquivo entra uma vez (caminho resolvido em `juntados`); sem `por_dia`, o
    dia é o da última gravação do arquivo. Devolve {arquivo: "juntado" | "já juntado" | "ilegível"}."""
    saida = {}
    with _trava(base_dados):
        e = estado(base_dados)
        feitos = set(e.setdefault("juntados", []))
        for arq in arquivos:
            chave_arq = str(Path(arq).resolve())
            if chave_arq == str((pasta(base_dados) / "estado.json").resolve()):
                saida[arq] = "é o próprio estado"
                continue
            if chave_arq in feitos:
                saida[arq] = "já juntado"
                continue
            outro = _json(arq, None)
            if not isinstance(outro, dict) or not isinstance(outro.get("projetos"), dict):
                saida[arq] = "ilegível"
                continue
            dia = dt.datetime.fromtimestamp(Path(arq).stat().st_mtime, BRT).strftime("%Y-%m-%d")
            for nome, q in outro["projetos"].items():
                p = _projeto(e, nome)
                for campo in ("buscas", "leituras", "aceitas", "recusadas"):
                    p[campo] = p.get(campo, 0) + int(q.get(campo) or 0)
                for m, n in (q.get("motivos") or {}).items():
                    p["motivos"][m] = p["motivos"].get(m, 0) + int(n)
                dias = {d: dict(c) for d, c in (q.get("por_dia") or {}).items()}
                for campo in ("buscas", "leituras", "aceitas", "recusadas"):  # o que o por_dia não cobre (lote antigo) vai para o dia do arquivo
                    resto = int(q.get(campo) or 0) - sum(int(c.get(campo) or 0) for c in dias.values())
                    if resto > 0:
                        dias.setdefault(dia, {})[campo] = dias.get(dia, {}).get(campo, 0) + resto
                for d, cont in dias.items():
                    for campo, n in cont.items():
                        if n:
                            _no_dia(p, campo, int(n), d)
            e["chamadas"] = e.get("chamadas", 0) + int(outro.get("chamadas") or 0)
            if outro.get("gasto_usd"):  # gasto de qualquer lote aparece no oficial: a regra é 0
                e["gasto_juntado_usd"] = round((e.get("gasto_juntado_usd") or 0) + float(outro["gasto_usd"]), 4)
            e["juntados"].append(chave_arq)
            feitos.add(chave_arq)
            saida[arq] = "juntado"
        _gravar(pasta(base_dados) / "estado.json", e)
    return saida


# ---- desligamento, chave, limitador, saldo ----

def ligada(quando=None):
    return (quando or dt.datetime.now(BRT)) < DESLIGA_EM


def guarda(base_dados=None, quando=None, http=_http):
    """Levanta FaixaParada se a faixa não pode chamar mais nada: desligada (31/10 12h BRT), saldo < US$ 0,50 ou caiu
    qualquer centavo abaixo da linha de base (o primeiro saldo lido) que o gasto registrado dos backlinks não explique. Grava o motivo em estado.json."""
    e = estado(base_dados)
    motivo = None
    if not ligada(quando):
        motivo = "desligada em 31/10 12h BRT (fim do Search/Fetch grátis)"
    else:
        s = saldo_usd(http)
        if e.get("base_usd") is None:
            e["base_usd"] = s
            e["gasto_externo_base"] = gasto_registrado(base_dados)
        externo = max(0.0, gasto_registrado(base_dados) - e.setdefault("gasto_externo_base", gasto_registrado(base_dados)))
        e["saldo_usd"], e["saldo_em"] = s, (quando or dt.datetime.now(BRT)).isoformat()
        if s < SALDO_MINIMO_USD:
            motivo = f"saldo US$ {s:.2f} abaixo de US$ {SALDO_MINIMO_USD:.2f}"
        elif e["base_usd"] - externo - s > 0.0001:  # só a queda NÃO explicada pelo gasto registrado (backlinks) para o moedor
            motivo = f"saldo caiu de US$ {e['base_usd']:.4f} para US$ {s:.4f}: algo está gastando (regra do dono: gasto 0)"
    e["parada"] = motivo
    e["gasto_usd"] = round((e.get("base_usd") or 0) - (e.get("saldo_usd") or 0), 4) if e.get("saldo_usd") is not None else None
    _gravar(pasta(base_dados) / "estado.json", e)
    if motivo:
        raise FaixaParada(motivo)


class Cliente(_ClienteBase):
    """Search e Fetch do comum (`hub.comum.deepseek`) com o guarda do moedor: desligamento em 31/10, saldo lido antes de cada
    lote e a cada 100 chamadas, contadores em estado.json."""

    def __init__(self, base_dados=None, http=_http, limitador=None, quando=None):
        super().__init__(http, limitador or Limitador(pasta(base_dados) / "limitador.txt"))
        self.base, self.quando = base_dados, quando

    def _antes_da_chamada(self):
        with _trava(self.base):
            e = estado(self.base)
            if e["chamadas"] % CONFERIR_SALDO_A_CADA == 0:
                guarda(self.base, self.quando, self.http)
            elif not ligada(self.quando):
                raise FaixaParada("desligada em 31/10 12h BRT")
            e = estado(self.base)
            e["chamadas"] += 1
            _gravar(pasta(self.base) / "estado.json", e)


class _Texto(HTMLParser):
    """HTML → texto com quebra de linha por bloco; sem script, style, nav e rodapé."""
    BLOCOS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "td", "dd", "dt"}
    PULA = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self.pula = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.PULA:
            self.pula += 1
        if tag in self.BLOCOS:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in self.PULA and self.pula:
            self.pula -= 1
        if tag in self.BLOCOS:
            self.partes.append("\n")

    def handle_data(self, d):
        if not self.pula:
            self.partes.append(d)


def html_para_texto(html):
    t = _Texto()
    try:
        t.feed(html)
    except Exception:
        pass
    return re.sub(r"\n\s*\n+", "\n", "".join(t.partes))


def _resumo(inv):
    """OpenAlex guarda o resumo como índice invertido {palavra: [posições]}."""
    if not inv:
        return ""
    pos = {i: w for w, idx in inv.items() for i in idx}
    return " ".join(pos[i] for i in sorted(pos))


def _arxiv_abs(url):
    """PDF (ou DOI) do arXiv vira a página do resumo (HTML, com o abstract): PDF fica de fora da leitura."""
    m = (re.match(r"https?://(?:export\.)?arxiv\.org/pdf/([^?#]+?)(?:\.pdf)?$", url or "")
         or re.match(r"https?://(?:dx\.)?doi\.org/10\.48550/arxiv\.([^?#]+)$", url or "", re.I))  # o DOI do arXiv redireciona sem o Crawl-delay
    return f"https://arxiv-org.example/abs/{m.group(1)}" if m else url


class ClienteAberto:
    """OpenAlex + leitura direta, sem custo. A interface é a do Cliente da DeepSeek (buscar, ler)."""
    OPENALEX = "https://api.openalex-org.example/works"
    ATRASO_DOMINIO = 0.5  # ≤ 2 req/s por domínio
    MAX_BYTES = 3_000_000

    def __init__(self, base_dados=None, abrir=None, limitador=None, relogio=time.time, dormir=time.sleep):
        self.base, self.abrir_fn = base_dados, abrir or self._abrir
        self.limitador = limitador or Limitador(pasta(base_dados) / "limitador-openalex.txt")
        self.relogio, self.dormir = relogio, dormir
        self._robots, self._ultimo, self._trava = {}, {}, threading.Lock()

    def consultas(self, semente):
        c = semente["consulta"]
        return [c, f"{c} jurisprudência doutrina"]

    def _abrir(self, url, timeout=25):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tipo = r.headers.get("Content-Type", "")
            return tipo, r.read(self.MAX_BYTES).decode(r.headers.get_content_charset() or "utf-8", "replace")

    CROSSREF = "https://api.crossref-org.example/works"

    def buscar(self, consulta, count=10):
        """OpenAlex; com a cota grátis do dia esgotada (429) ou fora do ar, cai na Crossref (também aberta e gratuita)."""
        if not getattr(self, "_sem_openalex", False):
            try:
                return self._buscar_openalex(consulta, count)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                self._sem_openalex = True  # retry-after do OpenAlex é de horas: não insiste no resto do lote
            except urllib.error.URLError:
                self._sem_openalex = True
        achadas = []
        for fonte in (self._buscar_zenodo, self._buscar_crossref):
            try:
                achadas += fonte(consulta, count)
            except (urllib.error.URLError, ValueError):
                continue  # uma fonte fora do ar não derruba a outra
        return achadas

    ZENODO = "https://zenodo-org.example/api/records"

    def _buscar_zenodo(self, consulta, count=10):
        """Zenodo: acesso aberto, página HTML legível e resumo no próprio registro."""
        self.limitador.esperar()
        q = urllib.parse.urlencode({"q": consulta, "size": max(1, min(25, count)), "type": "publication"})
        _, corpo = self.abrir_fn(f"{self.ZENODO}?{q}")
        saida = []
        for h in json.loads(corpo).get("hits", {}).get("hits", []):
            m = h.get("metadata") or {}
            url = (h.get("links") or {}).get("self_html")
            if not m.get("title") or not url:
                continue
            ano = (m.get("publication_date") or "")[:4]
            doi = (h.get("doi") or m.get("doi") or "").lower() or None
            saida.append({"title": m["title"], "url": url, "ano": int(ano) if ano.isdigit() else None,
                          "autores": [c["name"] for c in m.get("creators", []) if c.get("name")], "doi": doi,
                          "resumo": re.sub(r"<[^>]+>", " ", m.get("description") or "")})
        return saida

    def _buscar_crossref(self, consulta, count=10):
        self.limitador.esperar()
        q = urllib.parse.urlencode({
            "query.bibliographic": consulta, "rows": max(1, min(25, count)), "mailto": "rafael@clientec-com.example",
            "select": "title,DOI,issued,author,URL,abstract,resource"})
        _, corpo = self.abrir_fn(f"{self.CROSSREF}?{q}")
        saida = []
        for w in json.loads(corpo).get("message", {}).get("items", []):
            titulo = (w.get("title") or [""])[0]
            doi = (w.get("DOI") or "").lower()
            url = ((w.get("resource") or {}).get("primary") or {}).get("URL") or w.get("URL")
            if not titulo or not url:
                continue
            ano = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            autores = [" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in w.get("author", []) if a.get("family") or a.get("name")]
            resumo = re.sub(r"<[^>]+>", " ", w.get("abstract") or "")
            saida.append({"title": titulo, "url": url, "ano": ano, "autores": autores, "doi": doi or None, "resumo": resumo})
        return saida

    def _buscar_openalex(self, consulta, count=10):
        self.limitador.esperar()
        q = urllib.parse.urlencode({
            "search": consulta, "per-page": max(1, min(25, count)), "mailto": config.email("pesquisa"),
            "select": "title,doi,publication_year,authorships,primary_location,best_oa_location,abstract_inverted_index"})
        _, corpo = self.abrir_fn(f"{self.OPENALEX}?{q}")
        saida = []
        for w in json.loads(corpo).get("results", []):
            loc = w.get("best_oa_location") or w.get("primary_location") or {}
            url = _arxiv_abs(loc.get("landing_page_url") or w.get("doi"))
            if not url or not w.get("title"):
                continue
            autores = [a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author", {}).get("display_name")]
            saida.append({"title": w["title"], "url": url, "ano": w.get("publication_year"), "autores": autores,
                          "doi": (w.get("doi") or "").replace("https://doi-org.example/", "").lower() or None,
                          "resumo": _resumo(w.get("abstract_inverted_index"))})
        return saida

    def _pode(self, url):
        dom = urllib.parse.urlsplit(url).netloc
        with self._trava:
            rp = self._robots.get(dom)
            if rp is None:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    _, txt = self.abrir_fn(f"{urllib.parse.urlsplit(url).scheme}://{dom}/robots.txt", 10)
                    rp.parse(txt.splitlines())
                except urllib.error.HTTPError as e:
                    if e.code >= 500 or e.code == 429:  # RFC 9309: robots indisponível = tudo bloqueado; não guarda, tenta de novo depois
                        return False
                    rp.parse([])  # 4xx: sem robots.txt, tudo liberado (a convenção)
                except Exception:
                    return False  # timeout ou rede: sem saber a regra (nem o Crawl-delay), não lê
                self._robots[dom] = rp
        return rp.can_fetch(UA, url)

    def ler(self, url):
        if not self._pode(url):
            raise RuntimeError("robots.txt")
        dom = urllib.parse.urlsplit(url).netloc
        atraso = max(self.ATRASO_DOMINIO, self._robots[dom].crawl_delay(UA) or 0)  # o Crawl-delay do site manda (arXiv: 15 s)
        with self._trava:  # reserva o horário do domínio e espera fora da trava
            livre = max(self.relogio(), self._ultimo.get(dom, 0) + atraso)
            self._ultimo[dom] = livre
        if livre > self.relogio():
            self.dormir(livre - self.relogio())
        tipo, corpo = self.abrir_fn(url)
        if "pdf" in tipo.lower() or corpo.startswith("%PDF"):
            raise RuntimeError("pdf")
        return html_para_texto(corpo) if "html" in tipo.lower() or "<html" in corpo[:2000].lower() else corpo


class ClienteArxiv(ClienteAberto):
    """arXiv (API aberta, sem chave) para os temas de computação (F5): título, autores, ano e DOI vêm do
    próprio registro; a leitura é a página /abs (HTML com o resumo), no Crawl-delay do robots.txt. A consulta da semente
    vai crua para `search_query` (sintaxe do arXiv: `abs:"prompt injection" AND abs:agent`). Sementes com `"fonte": "arxiv"`."""
    API = "https://export.arxiv-org.example/api/query"
    ancora_no_resumo = True  # o resumo do registro é o mesmo da página /abs: sem a âncora nele, a leitura não teria trecho
    ATOM = "{http://www.w3.org/2005/Atom}"

    def __init__(self, base_dados=None, abrir=None, limitador=None, **kw):
        super().__init__(base_dados, abrir, limitador or Limitador(pasta(base_dados) / "limitador-arxiv.txt", qps=1 / 3), **kw)  # API: 1 a cada 3 s

    def consultas(self, semente):
        return [semente["consulta"]]

    def buscar(self, consulta, count=10, inicio=0):
        import xml.etree.ElementTree as ET
        self.limitador.esperar()
        q = urllib.parse.urlencode({"search_query": consulta, "start": inicio, "max_results": max(1, min(25, count)), "sortBy": "relevance"})
        _, corpo = self.abrir_fn(f"{self.API}?{q}")
        a, saida = self.ATOM, []
        for en in ET.fromstring(corpo).iter(f"{a}entry"):
            url = _arxiv_abs((en.findtext(f"{a}id") or "").strip().replace("http://", "https://", 1))
            url = re.sub(r"v\d+$", "", url)  # sem a versão: a página leva à última, e a dedupe não conta v1 e v2 como dois
            titulo = re.sub(r"\s+", " ", en.findtext(f"{a}title") or "").strip()
            if "/abs/" not in url or not titulo:
                continue
            doi = en.findtext("{http://arxiv-org.example/schemas/atom}doi")
            ano = (en.findtext(f"{a}published") or "")[:4]
            saida.append({"title": titulo, "url": url, "ano": int(ano) if ano.isdigit() else None,
                          "autores": [n.text.strip() for n in en.iter(f"{a}name") if n.text],
                          "doi": doi.strip().lower() if doi else None,
                          "resumo": re.sub(r"\s+", " ", en.findtext(f"{a}summary") or "").strip()})
        return saida


# ---- extração e verificação (determinísticas, sem modelo) ----

def _desescapar(s):
    """O Fetch devolve markdown com a pontuação escapada (`\\.`, `\\:`, `\\/`): tira a barra."""
    return re.sub(r"\\([^\w\s])", r"\1", s or "")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = _desescapar(s)
    return re.sub(r"\s+", " ", s).strip()


def url_chave(url):
    u = urllib.parse.urlsplit(url.strip())
    return f"{u.netloc.lower().removeprefix('www.')}{u.path.rstrip('/')}" + (f"?{u.query}" if u.query else "")


def acha_doi(texto):
    m = DOI.search(texto or "")
    return m.group(0).lower() if m else None


def ancora(numero, tribunal):
    """Regex do tema na página: `Tema 1.059`, `Tema nº 1059`, `Temas 566 a 571`. O tribunal tem que aparecer na página."""
    n = str(numero)
    com_ponto = f"{n[:-3]}\\.?{n[-3:]}" if len(n) > 3 else n
    return re.compile(rf"\btemas?\s*(?:n[º°o.]*\s*)?(?:\d+\s*(?:,|e|a)\s*)?{com_ponto}\b", re.I)


def ancora_livre(semente):
    """Âncora de semente sem número de tema: `ancora` (um termo) ou `ancoras` (qualquer um), literal, sem caixa e no
    começo de palavra (`agent` casa `agents`, não `reagent`; radical como `lombalg` segue valendo)."""
    termos = semente.get("ancoras") or [semente["ancora"]]
    return re.compile("|".join(rf"(?<!\w){re.escape(t)}" for t in termos), re.I)


TRIBUNAL = {"STJ": re.compile(r"\bstj\b|superior tribunal de justi", re.I), "STF": re.compile(r"\bstf\b|supremo tribunal federal|supremo\b", re.I)}


ABREV_FIM = re.compile(r"\b(n|art|arts|inc|min|des|dr|dra|fls|v|p|pp|ex|rel|ac)\.$", re.I)


def _fim_de_frase(linha, de, ate=None, ultimo=False):
    """Posição do ponto que fecha frase (". " depois de palavra que não é abreviatura: art., n., min., v., fls.)."""
    ate = len(linha) if ate is None else ate
    achados = [m.start() for m in re.finditer(r"\. ", linha[:ate]) if m.start() >= de and not ABREV_FIM.search(linha[:m.start() + 1])]
    return (achados[-1] if achados else -1) if ultimo else (achados[0] if achados else -1)


_CITA_TEMA = re.compile(
    r"\btemas?\s+(?:repetitivo\s+|de\s+repercuss[aã]o\s+geral\s+)?(?P<pre>stj|stf)?\s*(?:n[º°o.]*\s*)?"
    r"(?P<num>\d{1,4}(?:\.\d{3})?)\b(?P<pos>\s*[/–—,-]?\s*(?:d[oa]s?\s+)?(?:stj|stf|superior tribunal de justi|supremo tribunal federal)?)", re.I)


def temas_citados(texto):
    """[(numero, tribunal ou None)] dos `Tema N` que o texto cita (`Tema 1.237`, `TEMA STJ N. 1.237`, `Tema 483/STJ`,
    `Tema 1.313 do Superior Tribunal de Justiça`). O tribunal vem logo antes ou logo depois do número."""
    saida = []
    for m in _CITA_TEMA.finditer(texto or ""):
        n = int(m["num"].replace(".", ""))
        if n < 1 or n > 3000:  # ano ou outro número solto
            continue
        marca = _norm(f"{m['pre'] or ''} {m['pos'] or ''}")
        trib = "STJ" if re.search(r"\bstj\b|superior tribunal", marca) else "STF" if re.search(r"\bstf\b|supremo", marca) else None
        saida.append((n, trib))
    return saida


def _veredito_tema(citados, n, tribunal, texto):
    if (n, tribunal) in citados:
        return None
    if (n, None) in citados and not any(c[0] == n and c[1] for c in citados):
        return None if TRIBUNAL[tribunal].search(texto) else "tema_sem_tribunal"
    return "tema_de_outro"


def tema_confere(numero, tribunal, titulo, trecho):
    """Motivo da recusa quando o título ou o trecho trata de OUTRO tema: o conferidor prova que o trecho está na
    página, não que a página é do tema da semente. None = serve. Primeiro o título (é o assunto do artigo: título que nomeia
    outro tema reprova mesmo que o trecho cite o nosso de passagem), depois título + trecho. Em cada um, se há `Tema N`:
      - nenhum é o da semente (número e tribunal), ou o número certo vem só com o outro tribunal: `tema_de_outro`;
      - o número certo vem sem tribunal e o tribunal da semente não aparece no texto: `tema_sem_tribunal`.
    Sem nenhuma citação de tema (doutrina sobre o assunto, sem número), a ligação segue pelos termos do tema."""
    tribunal = (tribunal or "").upper()
    if not numero or tribunal not in TRIBUNAL:
        return None
    n, texto = int(numero), f"{titulo or ''} {trecho or ''}"
    for parte in (titulo or "", texto):
        citados = temas_citados(parte)
        motivo = _veredito_tema(citados, n, tribunal, texto) if citados else None
        if motivo:
            return motivo
    return None


def extrair_trecho(texto, ancora_re, titulo=None):
    """A frase (ou par de frases) da página que cita o tema, com TRECHO_MIN..TRECHO_MAX caracteres; None se não houver.
    O trecho sai literal do texto lido (barras de escape e cabeçalhos markdown saem; o espaço em branco é colapsado).
    Procura linha a linha: título, DOI e palavras-chave não viram trecho; frase que traz o `titulo` inteiro é metadado
    ("View a PDF of the paper titled …" do arXiv) e a busca segue para a próxima ocorrência."""
    nt = _norm(titulo) if titulo and len(titulo) >= 20 else None
    for linha in _desescapar(texto).splitlines():
        linha = re.sub(r"^[#>*\-\s]+", "", linha)
        linha = re.sub(r"\s+", " ", linha).strip()
        linha = re.sub(r"(?i)^(?:abstract|resumo)\s*:\s*", "", linha)  # o rótulo do bloco não é parte da frase
        for m in ancora_re.finditer(linha):
            ant = _fim_de_frase(linha, 0, m.start(), ultimo=True)
            ini = ant + 2 if ant >= 0 else 0
            fim = _fim_de_frase(linha, m.end())
            fim = len(linha) if fim < 0 else fim + 1
            trecho = linha[ini:fim].strip()
            if len(trecho) > TRECHO_MAX:
                a, b = max(m.start() - 150, ini), min(m.end() + 350, fim)
                if a > ini:
                    a = linha.find(" ", a) + 1 or a  # começa numa palavra inteira
                if b < fim:
                    b = linha.rfind(" ", m.end(), b) if linha.rfind(" ", m.end(), b) > 0 else b
                trecho = linha[a:b].strip()
            if (TRECHO_MIN <= len(trecho) <= TRECHO_MAX and len(re.findall(r"[a-zà-ú]{3,}", trecho, re.I)) >= 8
                    and ancora_re.search(trecho) and not (nt and nt in _norm(trecho)) and "|" not in trecho and not re.search(r"\.(pdf|docx?|odt|zip)\s*$", trecho, re.I) and not ABREV_FIM.search(trecho) and not re.match(r"(?i)t[ií]tulo\b", trecho)):  # linha de tabela ou metadado não é afirmação
                return trecho
    return None


def trecho_confere(trecho, pagina):
    return bool(trecho) and _norm(trecho) in _norm(pagina)


def autor_ano(pagina, resultado):
    """Autor e ano só se aparecem na própria página (ou no resultado da busca, a data); senão None. Nunca inventa."""
    if resultado and (resultado.get("autores") or resultado.get("ano")):  # vindos da base (OpenAlex), nunca deduzidos
        return ("; ".join(resultado["autores"]) if resultado.get("autores") else None), resultado.get("ano")
    autor = ano = None
    m = re.search(r"(?im)^\s*(?:autor(?:es)?|author(?:s)?)\s*[:\-]\s*([^\n]{3,120})$", pagina or "")
    if m:
        autor = re.sub(r"\\", "", m.group(1)).strip()
    d = (resultado or {}).get("date") or ""
    if re.match(r"(19|20)\d{2}", d):
        ano = int(d[:4])
    else:
        m = re.search(r"\b((?:19|20)\d{2})\b", (resultado or {}).get("title", ""))
        ano = int(m.group(1)) if m else None
    return autor, ano


def _aberta(resultado):
    """Fonte aberta: registro da base acadêmica (tem DOI) ou domínio da lista FONTES."""
    u = urllib.parse.urlsplit(resultado.get("url", ""))
    return bool(resultado.get("doi")) or bool(FONTES.search(u.netloc + u.path))


def montar_ref(semente, resultado, pagina, aceita_outras=False):
    """(referência, None) ou (None, motivo da recusa). Sem trecho conferido não entra."""
    url = resultado.get("url", "")
    if not url.startswith(("http://", "https://")):
        return None, "url_invalida"
    partes = urllib.parse.urlsplit(url)
    if NAO_DOUTRINA.search(partes.path) or NAO_DOUTRINA_HOST.search(partes.netloc) and NAO_DOUTRINA_CONSULTA.search(partes.path + "?" + partes.query):
        return None, "jurisprudencia_nao_e_doutrina"
    fonte = "aberta" if _aberta(resultado) else "outra"
    if fonte == "outra" and not aceita_outras:
        return None, "fonte_fora_da_lista"
    if not pagina or len(pagina) < 400:
        return None, "pagina_nao_abre"
    tribunal = semente.get("tribunal")
    trecho = extrair_trecho(pagina, ancora(semente["numero"], tribunal) if semente.get("numero") else ancora_livre(semente),
                            resultado.get("title"))
    if not trecho:
        return None, "sem_trecho"
    if not trecho_confere(trecho, pagina):
        return None, "trecho_nao_confere"
    if tribunal in TRIBUNAL and not TRIBUNAL[tribunal].search(trecho):  # o tribunal tem que estar na própria afirmação, não solto na página
        return None, "tribunal_ausente"
    motivo = tema_confere(semente.get("numero"), tribunal, resultado.get("title"), trecho)
    if motivo:
        return None, motivo
    autor, ano = autor_ano(pagina, resultado)
    titulo = re.sub(r"\s+", " ", resultado.get("title") or "").strip()
    if not titulo:
        return None, "sem_titulo"
    nt, nr = _norm(titulo), _norm(trecho)
    if nr in nt or (nt in nr and len(nr) - len(nt) < 40):  # o trecho é o próprio título: não sustenta afirmação
        return None, "trecho_e_o_titulo"
    return {"titulo": titulo, "autor": autor, "ano": ano, "url": url.split("#")[0], "doi": resultado.get("doi") or acha_doi(url) or acha_doi(pagina[:4000]),
            "trecho": trecho, "tema": semente["tema"], "fonte_tipo": fonte,
            "conferido_em": dt.datetime.now(BRT).strftime("%Y-%m-%d")}, None


# ---- gravação ----

def _vistos(arq):
    urls, dois, titulos = set(), set(), set()
    for linha in Path(arq).read_text(encoding="utf-8").splitlines() if Path(arq).exists() else []:
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        urls.add(url_chave(r["url"]))
        titulos.add(_norm(r["titulo"]))
        if r.get("doi"):
            dois.add(r["doi"])
    return urls, dois, titulos


def gravar(repo, tema_slug, refs, titulo_tema):
    """Acrescenta ao .jsonl do tema só o que não repete URL nem DOI e refaz o resumo .md. Devolve quantas entraram."""
    pasta_ref = Path(repo) / "docs" / "referencias"
    pasta_ref.mkdir(parents=True, exist_ok=True)
    arq = pasta_ref / f"{tema_slug}.jsonl"
    urls, dois, titulos = _vistos(arq)
    novas = []
    for r in refs:
        k, t = url_chave(r["url"]), _norm(r["titulo"])
        if k in urls or t in titulos or (r.get("doi") and r["doi"] in dois):  # o mesmo artigo em dois sítios conta uma vez
            continue
        urls.add(k)
        titulos.add(t)
        if r.get("doi"):
            dois.add(r["doi"])
        novas.append(r)
    if novas:
        with open(arq, "a", encoding="utf-8") as f:
            for r in novas:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    todas = [json.loads(x) for x in arq.read_text(encoding="utf-8").splitlines() if x.strip()] if arq.exists() else []
    if todas:
        md = [f"# Referências: {titulo_tema}", "",
              "Geradas pelo moedor público (KRN-1135). Cada uma tem o trecho literal conferido na página lida; autor e ano só quando a página traz.", ""]
        for r in todas:
            quem = ", ".join(x for x in (r.get("autor"), str(r["ano"]) if r.get("ano") else None) if x) or "autor e ano não informados na página"
            md += [f"- [{r['titulo']}]({r['url']}) ({quem})" + (f", DOI {r['doi']}" if r.get("doi") else ""), f"  > {r['trecho']}"]
        arq.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return len(novas)


# ---- lote ----

def consultas(semente):
    """Consultas por template (sem modelo): o tema + o assunto, com os sítios de doutrina aberta."""
    base = f"{semente['consulta']}"
    return [f"{base} doutrina artigo scielo", f"{base} repositório universidade artigo", f"{base} revista jurídica"]


def _ler_seguro(cliente, url):
    try:
        return cliente.ler(url), None
    except FaixaParada as e:
        return None, e
    except Exception:  # 404 do Fetch, timeout: a página não abre, a referência não entra
        return None, RuntimeError("fetch")


def processar(semente, repo, cliente, projeto="clientej", por_consulta=8, max_refs=5, aceita_outras=False, base_dados=None, paralelo=4):
    """Busca, lê (até `paralelo` páginas juntas; o limitador global segura os 4 QPS) e confere para uma semente.
    Devolve {aceitas, recusadas}. FaixaParada sobe para o chamador."""
    aceitas, recusadas, tentadas = [], 0, set()
    for q in semente.get("consultas") or getattr(cliente, "consultas", consultas)(semente):  # semente fora do direito traz as suas
        contar(projeto, "buscas", base_dados=base_dados)
        candidatas = []
        for r in cliente.buscar(q, por_consulta):
            k = url_chave(r.get("url", ""))
            if k in tentadas:
                continue
            tentadas.add(k)
            if not _aberta(r) and not aceita_outras:
                recusar(projeto, "fonte_fora_da_lista", base_dados)
                recusadas += 1
            elif getattr(cliente, "ancora_no_resumo", False) and not semente.get("numero") and not ancora_livre(semente).search(
                    f"{r.get('title', '')} {r.get('resumo', '')}"):
                recusar(projeto, "ancora_fora_do_resumo", base_dados)
                recusadas += 1
            elif semente.get("exige") and r.get("resumo") and not ancora_livre({"ancoras": semente["exige"]}).search(
                    f"{r.get('title', '')} {r['resumo']}"):  # homônimo (MetaGPT de fusão de modelos, OpenHands de língua de sinais)
                recusar(projeto, "homonimo_fora_do_assunto", base_dados)
                recusadas += 1
            else:
                candidatas.append(r)
        with ThreadPoolExecutor(paralelo) as ex:
            lidas = list(ex.map(lambda r: _ler_seguro(cliente, r["url"].split("#")[0]), candidatas))
        for r, (pagina, erro) in zip(candidatas, lidas):
            if isinstance(erro, FaixaParada):
                raise erro
            if erro:
                recusar(projeto, "fetch_falhou", base_dados)
                recusadas += 1
                continue
            contar(projeto, "leituras", base_dados=base_dados)
            ref, motivo = montar_ref(semente, r, pagina, aceita_outras)
            if ref and len(aceitas) < max_refs:
                aceitas.append(ref)
            else:
                recusar(projeto, motivo or "limite_do_tema", base_dados)
                recusadas += 1
        if len(aceitas) >= max_refs:
            break
    novas = gravar(repo, semente["slug"], aceitas, semente["tema"])
    contar(projeto, "aceitas", novas, base_dados)
    return {"aceitas": novas, "recusadas": recusadas}


def lote(arq_sementes, repo, maximo=None, cliente=None, base_dados=None, registro=None):
    """`registro`: jsonl com uma linha por tema concluído; temas já nele são pulados (retoma de onde parou)."""
    feitos = set()
    if registro and Path(registro).exists():
        for ln in Path(registro).read_text(encoding="utf-8").splitlines():
            try:
                feitos.add(json.loads(ln)["slug"])
            except (ValueError, KeyError):
                pass
    sem = json.loads(Path(arq_sementes).read_text(encoding="utf-8"))
    projeto = sem.get("projeto", "clientej")
    if cliente is None:
        cliente = (Cliente(base_dados) if os.environ.get("MOEDOR_DEEPSEEK") == "1" else
                   ClienteArxiv(base_dados) if sem.get("fonte") == "arxiv" else ClienteAberto(base_dados))
    if isinstance(cliente, Cliente):
        guarda(base_dados, None, cliente.http)
    resumo = []
    for s in sem["sementes"][:maximo]:
        if s["slug"] in feitos:
            continue
        try:
            r = processar(s, repo, cliente, projeto, base_dados=base_dados, aceita_outras=sem.get("aceita_outras", False),
                          max_refs=sem.get("max_refs", 5))
        except FaixaParada as e:
            resumo.append({"slug": s["slug"], "parada": str(e)})
            break
        except (OSError, ValueError, SyntaxError) as e:  # 429/5xx, timeout (TimeoutError não é URLError) ou resposta ilegível: perde a semente, não o lote
            recusar(projeto, "busca_falhou", base_dados)
            resumo.append({"slug": s["slug"], "erro": f"{type(e).__name__}: {e}"[:200]})
            _registrar(registro, resumo[-1])
            continue
        resumo.append({"slug": s["slug"], **r})
        _registrar(registro, resumo[-1])
    return resumo


def _registrar(registro, linha):
    if registro:
        with open(registro, "a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.moedor", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lo = sub.add_parser("lote", help="busca, lê, confere e grava as referências de um arquivo de sementes")
    lo.add_argument("sementes")
    lo.add_argument("--repo", required=True)
    lo.add_argument("--max", type=int)
    lo.add_argument("--registro", help="jsonl por tema concluído; ao relançar, pula os que já estão nele")
    sub.add_parser("estado", help="contadores por projeto, saldo e se a faixa está ligada")
    ju = sub.add_parser("juntar", help="soma ao estado oficial o estado.json de um lote que gravou em outra pasta (uma vez por arquivo)")
    ju.add_argument("arquivos", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "juntar":
        print(json.dumps(juntar(a.arquivos), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "estado":
        e = estado()
        print(json.dumps({**e, "ligada": ligada()}, ensure_ascii=False, indent=2))
        return 0
    try:
        res = lote(a.sementes, a.repo, a.max, registro=a.registro)
    except FaixaParada as e:
        print(f"faixa parada: {e}", file=sys.stderr)
        return 3
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 3 if any("parada" in r for r in res) else 0


if __name__ == "__main__":
    raise SystemExit(main())
