"""Extração da receita: salvo (jsonl do dia) -> receita curta em markdown, com guarda de prompt injection.

O conteúdo do post é DADO: vai dentro de um bloco com delimitador aleatório por chamada, escapado para não conter o delimitador,
com as instruções fixas antes e depois (prompt.md). A resposta do LLM só é aceita depois de `validar_receita` (frontmatter,
tamanho, sem URL fora dos links do post, sem comando perigoso, sem delimitador). O LLM roda sem ferramentas: `executar_llm(prompt)`
é injetável e o padrão é `claude -p` só texto (sem pular permissões). Nunca usa rede por conta própria.
"""
import json
import re
import secrets
import subprocess
import sys
import unicodedata
from pathlib import Path

from . import guardrails as G

PROMPT = Path(__file__).with_name("prompt.md")
MAX_RECEITA = 4000   # caracteres da resposta do LLM
MAX_TEXTO = 12000    # caracteres do post enviados ao LLM
MAX_TITULO = 120
TIMEOUT_LLM = 300

RE_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'`]+", re.I)
RE_PERIGO = re.compile(
    r"\brm\s+-[a-z]*[rf]|\bsudo\b|\bchmod\b|\bchown\b|\bmkfs|\bdd\s+if=|\bwget\b|\bcurl\b|\beval\b|\bbase64\s+-d|:\(\)\s*\{|"
    r"\bnc\s+-|\bkill\s+-9|\bshutdown\b|\breboot\b|>\s*/dev/sd|\bpip3?\s+install\b|\bnpm\s+(i|install)\b|\bpowershell\b|"
    r"\|\s*(ba|z)?sh\b|\b(ba|z)?sh\s+-c\b|\bpython3?\s+-c\b|\bssh\s|\btee\s|\bPWNED\b|\bgit\s+push\b|\bDROP\s+TABLE\b", re.I)


# domínio nu (sem esquema): host.tld[/caminho]; extensões de arquivo comuns não contam, e sem "/" só vale TLD conhecido
RE_NU = re.compile(r"(?<![\w./-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,24}))(?![\w-])(/[^\s<>()\[\]\"'`]*)?", re.I)
EXT_CODIGO = {"py", "js", "ts", "jsx", "tsx", "md", "json", "txt", "sh", "yml", "yaml", "toml", "html", "css", "csv", "log", "ini",
              "cfg", "conf", "rb", "go", "rs", "java", "php", "sql", "lock", "env", "pdf", "png", "jpg", "jpeg", "gif", "svg", "zip",
              "gz", "tar", "xml", "mjs", "cjs", "vue", "c", "h", "cpp", "swift", "kt", "pl", "lua", "bat", "exe", "bin", "pyc", "db"}
TLDS = {"com", "org", "net", "io", "ly", "co", "br", "dev", "app", "ai", "me", "info", "biz", "xyz", "gl", "to", "sh", "us", "uk",
        "de", "fr", "es", "pt", "ru", "cn", "jp", "in", "tv", "cc", "link", "site", "online", "top", "club", "page", "gg", "so",
        "example", "test", "tk", "ml", "ga", "cf", "gq", "su", "ws", "is", "it", "nl", "eu", "ch", "id", "gov", "edu"}
ID_VALIDO = re.compile(r"\d+")


class ReceitaRejeitada(ValueError):
    pass


# ---------- prompt ----------
def neutralizar(texto, nonce):
    """Tira do texto do post qualquer coisa que forje ou feche o bloco (delimitadores e o próprio nonce)."""
    t = str(texto).replace("<<<", "‹‹‹").replace(">>>", "›››")
    return t.replace(nonce, "[removido]")


def bloco_post(item, nonce):
    campos = {
        "autor": item.get("autor"), "url": item.get("url"), "data": item.get("data"), "nome": item.get("nome"),
        "texto": item.get("texto"), "fio": item.get("fio"), "citados": item.get("citados"), "artigo": item.get("artigo"),
        "links": item.get("links"),
    }
    corpo = neutralizar(json.dumps({k: v for k, v in campos.items() if v}, ensure_ascii=False, indent=1)[:MAX_TEXTO], nonce)
    return f"<<<POST id={item.get('id')} fim={nonce}>>>\n{corpo}\n<<<FIM {nonce}>>>"


def montar_prompt(item, nonce=None):
    nonce = nonce or secrets.token_hex(12)
    modelo = PROMPT.read_text(encoding="utf-8")
    fixo = modelo.replace("{nonce}", nonce).replace("{maximo}", str(MAX_RECEITA))
    return fixo.replace("{bloco}", bloco_post(item, nonce)), nonce  # o bloco entra por último: {..} no post não é expandido


# ---------- validação ----------
def _url_norm(u):
    return u.rstrip(".,;:!?)\"'").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def links_do_post(item):
    ls = [item.get("url")] + list(item.get("links") or []) + list(item.get("citados") or [])
    out = set()
    for x in ls:
        if isinstance(x, dict):
            x = x.get("url")
        if isinstance(x, str) and x:
            out.add(_url_norm(x))
    return out


def validar_receita(resp, item, nonce=""):
    """Devolve (titulo, corpo) ou levanta ReceitaRejeitada."""
    if not isinstance(resp, str) or not resp.strip():
        raise ReceitaRejeitada("resposta vazia")
    resp = resp.strip()
    if len(resp) > MAX_RECEITA:
        raise ReceitaRejeitada(f"resposta maior que {MAX_RECEITA} caracteres")
    if "<<<" in resp or ">>>" in resp or (nonce and nonce in resp):
        raise ReceitaRejeitada("resposta contém delimitador")
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", resp, re.S)
    if not m:
        raise ReceitaRejeitada("sem frontmatter")
    titulo = next((l.split(":", 1)[1].strip() for l in m.group(1).splitlines() if l.startswith("titulo:")), "")
    if not titulo or len(titulo) > MAX_TITULO:
        raise ReceitaRejeitada("título ausente ou longo demais")
    corpo = m.group(2).strip()
    if not corpo:
        raise ReceitaRejeitada("corpo vazio")
    if RE_PERIGO.search(resp):
        raise ReceitaRejeitada("comando perigoso na resposta")
    permitidos = links_do_post(item)
    hosts = {p.split("/")[0] for p in permitidos}
    for u in RE_URL.findall(resp):
        if _url_norm(u) not in permitidos:
            raise ReceitaRejeitada("URL fora dos links do post")
    for m in RE_NU.finditer(RE_URL.sub(" ", resp)):
        host, tld, caminho = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if tld in EXT_CODIGO and tld not in TLDS:
            continue
        if not caminho and tld not in TLDS:
            continue
        if _url_norm(host + (caminho or "")) not in permitidos and not (not caminho and host in hosts):
            raise ReceitaRejeitada("URL sem esquema fora dos links do post")
    return titulo, corpo


# ---------- LLM ----------
def executar_claude(prompt):
    """Padrão: `claude -p` só texto, prompt pela entrada padrão: sem ferramentas (--tools ""), sem servidores MCP
    (--strict-mcp-config sem --mcp-config), sem configurações de usuário/projeto (--setting-sources "": nem hooks nem MCP delas) e sem
    pular permissões."""
    r = subprocess.run(["claude", "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", ""], input=prompt, capture_output=True, text=True, timeout=TIMEOUT_LLM)
    if r.returncode != 0:
        raise RuntimeError(f"claude saiu com código {r.returncode}")
    return r.stdout


# ---------- destino ----------
def slugify(s, limite=60):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:limite].strip("-") or "sem-titulo"


def escolher_tema(item, temas):
    """(tema, fora_do_tema): primeiro tema de `temas` que aparece no post; sem casar, `geral` + fora-do-tema (se há temas)."""
    palco = " ".join(str(item.get(k) or "") for k in ("texto", "nome", "fio", "artigo", "citados")).lower()
    for t in temas or []:
        if t.strip() and t.strip().lower() in palco:
            return slugify(t), False
    return "geral", bool(temas)


def _yaml(s):
    return json.dumps(str(s), ensure_ascii=False)


def montar_arquivo(item, titulo, corpo, tema, fora, destino):
    if destino == "issue":
        return f"# {titulo}\n\nFonte: {item.get('url', '')}\n\n{corpo}\n"
    etiquetas = ["fora-do-tema"] if fora else []
    fm = ["---", f"titulo: {_yaml(titulo)}", f"tema: {tema}", "status: curta", f"etiquetas: {json.dumps(etiquetas)}",
          f"fonte: {_yaml(item.get('url', ''))}", f"autor: {_yaml(item.get('autor', ''))}", f"data: {_yaml(item.get('data', ''))}", "---"]
    return "\n".join(fm) + f"\n\n{corpo}\n"


def caminho_destino(cfg, item, titulo, tema):
    nome = f"{slugify(item.get('id', ''), 30)}-{slugify(titulo, 50)}.md"
    base = Path(cfg.saida)
    return base / "issues" / nome if cfg.destino == "issue" else base / "receitas" / tema / nome


# ---------- orquestração ----------
def ler_jsonl(arq):
    out = []
    for linha in Path(arq).read_text(encoding="utf-8").splitlines():
        if linha.strip():
            try:
                d = json.loads(linha)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if not ID_VALIDO.fullmatch(str(d.get("id", ""))):
                G.log("linha ignorada: id inválido")
                continue
            d["id"] = str(d["id"])
            out.append(d)
    return out


def ultimo_jsonl(saida):
    arqs = sorted(Path(saida).glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl"))
    return arqs[-1] if arqs else None


def ja_extraido(cfg, item):
    """Já existe {id}-*.md (em qualquer tema, ou em issues): não chama o LLM de novo (o título dele muda a cada chamada)."""
    base, pid = Path(cfg.saida), item["id"]
    pasta = [base / "issues"] if cfg.destino == "issue" else list((base / "receitas").glob("*"))
    return any(next(p.glob(f"{pid}-*.md"), None) for p in pasta if p.is_dir())


def extrair_item(cfg, item, executar_llm):
    """Grava a receita do salvo. Devolve o caminho, ou None se já existia. Levanta ReceitaRejeitada se a resposta não passa."""
    if ja_extraido(cfg, item):
        return None
    prompt, nonce = montar_prompt(item)
    titulo, corpo = validar_receita(executar_llm(prompt), item, nonce)
    tema, fora = escolher_tema(item, cfg.temas)
    dest = caminho_destino(cfg, item, titulo, tema)
    if dest.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(montar_arquivo(item, titulo, corpo, tema, fora, cfg.destino), encoding="utf-8")
    return dest


def extrair(cfg, arq=None, executar_llm=None):
    """Extrai todos os salvos do jsonl. Devolve (gravadas, rejeitadas)."""
    executar_llm = executar_llm or executar_claude
    arq = arq or ultimo_jsonl(cfg.saida)
    if not arq:
        G.log("nenhum .jsonl para extrair")
        return 0, 0
    ok = rej = 0
    for item in ler_jsonl(arq):
        try:
            if extrair_item(cfg, item, executar_llm):
                ok += 1
        except ReceitaRejeitada as e:
            rej += 1
            G.log(f"receita rejeitada ({item.get('id')}): {e}")
        except Exception as e:
            rej += 1
            G.log_erro(e, f"extrair {item.get('id')}: ")
    G.log(f"extração: {ok} receita(s) gravada(s), {rej} rejeitada(s)")
    return ok, rej


def main(cfg, executar_llm=None):
    ok, rej = extrair(cfg, executar_llm=executar_llm)
    return 0 if ok or not rej else 1


if __name__ == "__main__":
    sys.exit("use: python3 -m hub.x_salvos extrair")
