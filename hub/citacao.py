"""Menção e citação de uma marca numa resposta de IA ou num AI Overview. Uma regra só para o hub inteiro.

- Citação: a URL é do site quando o host é exatamente um dos hosts declarados no cadastro (domínio,
  hosts_alternativos, hosts_citacao). Subdomínio qualquer não conta: blog.exemplo.com só vale se declarado.
- Menção: o nome aparece como palavra inteira (sem diferença de maiúscula ou acento). Aliases só os
  declarados. Marca de nome genérico (ex.: "ClienteJ") não conta pelo nome, só pelos aliases.
- Nomes que se sobrepõem ("fsite05" e "Cliente A") contam uma vez por ocorrência.
"""
import re
import unicodedata
from urllib.parse import urlsplit

# Frases de quem não reconhece a marca: "reconhecer" exige que a resposta não diga que não achou nada.
DESCONHECE = ("nao encontrei", "nao ha informac", "nao tenho informac", "nao consegui encontrar", "nao localizei",
              "sem informacoes", "nao foi possivel encontrar", "nao aparece nos resultados")


def normalizar(texto):
    """Minúsculo e sem acento, preservando o resto (posições comparáveis dentro do mesmo texto)."""
    sem = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sem if not unicodedata.combining(c)).casefold()


def trechos(texto, nomes):
    """[(início, fim)] de cada ocorrência de qualquer um dos nomes como palavra inteira."""
    nomes = sorted({normalizar(n).strip() for n in nomes if n and n.strip()}, key=len, reverse=True)
    if not nomes:
        return []
    # Uma alternância só: aliases sobrepostos na mesma ocorrência não contam duas vezes.
    return [m.span() for m in re.finditer(r"(?<!\w)(?:" + "|".join(map(re.escape, nomes)) + r")(?!\w)", normalizar(texto))]


def mencoes(texto, nomes):
    """(quantas vezes, posição da primeira) de qualquer um dos nomes como palavra inteira."""
    t = trechos(texto, nomes)
    return len(t), t[0][0] if t else None


def host(url):
    try:
        h = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""
    return h.lower().rstrip(".")


def alvo(site):
    """O que identifica o site numa resposta, a partir do cadastro (sites/<id>.json)."""
    marca = site["marca"]
    nomes = list(marca["aliases"]) + ([] if marca["nome_generico"] else [marca["nome"]])
    return {"nome": marca["nome"], "nomes": nomes, "identificador": marca.get("identificador"),
            "hosts": {site["dominio"], *site["hosts_alternativos"], *site["hosts_citacao"]}}


def urls_do_site(urls, hosts):
    """As URLs cujo host é exatamente um dos hosts do site."""
    hosts = {h.lower() for h in hosts}
    return [u for u in urls if host(u) in hosts]


def citado(urls, hosts):
    return bool(urls_do_site(urls, hosts))


def urls_no_texto(texto):
    return [u.rstrip(".,;:!?") for u in re.findall(r"https?://[^\s<>)\]\"']+", texto or "")]


def medir_resposta(texto, urls, a, concorrentes, prompt=""):
    """Métricas de uma resposta do painel nas IAs.

    Com o nome no prompt, mencionar é trivial: vale reconhecer (site citado, domínio escrito, identificador)
    e quem é recomendado. Sem o nome, vale aparecer e em que ordem."""
    t = normalizar(texto)
    com_marca = mencoes(prompt, a["nomes"])[0] > 0 or mencoes(prompt, a["hosts"])[0] > 0
    posicoes = {a["nome"]: mencoes(texto, a["nomes"])[1]}
    posicoes.update({c: mencoes(texto, [c])[1] for c in concorrentes})
    ordem = [n for n, p in sorted(((n, p) for n, p in posicoes.items() if p is not None), key=lambda x: x[1])]
    dominio = citado(list(urls) + urls_no_texto(texto), a["hosts"])
    reconhece = dominio or mencoes(texto, a["hosts"])[0] > 0 or bool(a.get("identificador") and re.search(a["identificador"], texto))
    return {
        "com_marca": com_marca,
        "reconhece": reconhece and not any(x in t for x in DESCONHECE),
        "marca_mencionada": posicoes[a["nome"]] is not None,
        "dominio_citado": dominio,
        "concorrentes": [c for c in ordem if c != a["nome"]],
        "primeiro": ordem[0] if ordem else None,
        "ordem": ordem,
    }
