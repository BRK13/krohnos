"""Janelas de medição: quanto esperar depois de mexer numa página antes de julgar o efeito (e de mexer de novo).

Prazos do Google (Gary Illyes, Search Central Live, out/2026):
- título/snippet: muda no resultado em 1 a 2 dias; julgar após 14 dias;
- página nova: indexada em horas a dias; julgar após 28 dias;
- canonical: o Google acata em 1 a 3 semanas; julgar após 42 dias;
- core update: o efeito se acomoda em 3 a 6 meses; julgar após 180 dias.
`conteudo` (reescrever o corpo de uma página que já existe) segue o prazo de página nova: depende de novo
rastreamento e reindexação. Escolha do hub, não do Google.

Cada PR de SEO registra a mudança em $HUB_DATA/mudancas.jsonl:

    python -m hub.janelas registrar --site fsite06 --url /quiropraxia --tipo titulo --pr https://github.com/...
    python -m hub.janelas abertas [--site fsite06]

A análise semanal e a tarefa do Claude leem as janelas abertas e não propõem mexer de novo na mesma página
antes da data de julgar.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from hub import codigo, data

PRAZOS = {
    "titulo": {"efeito": "1 a 2 dias", "julgar_apos": 14},
    "conteudo": {"efeito": "dias, após novo rastreamento", "julgar_apos": 28},
    "pagina_nova": {"efeito": "horas a dias para indexar", "julgar_apos": 28},
    "canonical": {"efeito": "1 a 3 semanas", "julgar_apos": 42},
    "core_update": {"efeito": "3 a 6 meses", "julgar_apos": 180},
}


def arquivo(base_dados=None):
    return Path(base_dados or data()) / "mudancas.jsonl"


def normalizar(url, dominio=None):
    """Mesma página, mesma chave: host minúsculo sem www, sem query/fragmento, sem barra final (exceto a raiz)."""
    if url.startswith("/"):
        if not dominio:
            raise ValueError("caminho relativo precisa do domínio do site")
        url = f"https://{dominio}{url}"
    p = urlsplit(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower().removeprefix("www.")
    caminho = p.path or "/"
    if caminho != "/":
        caminho = caminho.rstrip("/")
    return f"{host}{caminho}"


def registrar(site_id, url, tipo, data_mudanca=None, pr=None, nota=None, base_dados=None, pasta_sites=None):
    from hub import sites
    if tipo not in PRAZOS:
        raise ValueError(f"tipo tem que ser um de: {', '.join(PRAZOS)}")
    s = sites.site(site_id, pasta_sites)
    quando = dt.date.fromisoformat(data_mudanca) if data_mudanca else dt.date.today()
    registro = {"data": quando.isoformat(), "site": s["id"], "pagina": normalizar(url, s["dominio"]), "tipo": tipo,
                "julgar_em": (quando + dt.timedelta(days=PRAZOS[tipo]["julgar_apos"])).isoformat(), "pr": pr, "nota": nota}
    alvo = arquivo(base_dados)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    with open(alvo, "a", encoding="utf-8") as f:  # uma linha por write: append atômico para linhas curtas
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    return registro


def ler(base_dados=None):
    alvo = arquivo(base_dados)
    if not alvo.exists():
        return []
    saida = []
    for n, linha in enumerate(alvo.read_text(encoding="utf-8").splitlines(), 1):
        if linha.strip():
            try:
                saida.append(json.loads(linha))
            except ValueError:
                print(f"aviso: {alvo}:{n} não é JSON; linha ignorada", file=sys.stderr)
    return saida


def abertas(hoje=None, site_id=None, base_dados=None):
    """Mudanças cuja janela ainda não fechou (não julgar, não mexer de novo). Mais recente primeiro."""
    hoje = hoje or dt.date.today()
    saida = [m for m in ler(base_dados) if dt.date.fromisoformat(m["julgar_em"]) > hoje and (not site_id or m["site"] == site_id)]
    return sorted(saida, key=lambda m: m["data"], reverse=True)


def pode_mexer(pagina, site_dominio, hoje=None, base_dados=None):
    """None se a página está livre; senão, a mudança que segura a janela."""
    chave = normalizar(pagina, site_dominio)
    return next((m for m in abertas(hoje, base_dados=base_dados) if m["pagina"] == chave), None)


def texto_abertas(lista):
    linhas = ["## Janelas de medição abertas (não mexer de novo nestas páginas antes da data)", ""]
    if not lista:
        return linhas + ["- nenhuma mudança registrada em janela aberta", ""]
    linhas += [f"- {m['site']} · {m['pagina']} · {m['tipo']} em {m['data']} · julgar a partir de {m['julgar_em']}"
               + (f" · {m['pr']}" if m.get("pr") else "") for m in lista]
    return linhas + ["", "Para registrar uma mudança nova (todo PR de SEO): "
                         "`python3 -m hub.janelas registrar --site <id> --url <página> --tipo <" + "|".join(PRAZOS) + "> --pr <url>` "
                         "rodado em " + str(codigo()) + ".", ""]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.janelas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("registrar")
    r.add_argument("--site", required=True)
    r.add_argument("--url", required=True, help="URL completa ou caminho (/pagina)")
    r.add_argument("--tipo", required=True, choices=sorted(PRAZOS))
    r.add_argument("--data", help="AAAA-MM-DD; padrão hoje")
    r.add_argument("--pr")
    r.add_argument("--nota")
    a = sub.add_parser("abertas")
    a.add_argument("--site")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # console do Windows não é UTF-8
    if args.cmd == "registrar":
        print(json.dumps(registrar(args.site, args.url, args.tipo, args.data, args.pr, args.nota), ensure_ascii=False))
    else:
        sys.stdout.write("\n".join(texto_abertas(abertas(site_id=args.site))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
