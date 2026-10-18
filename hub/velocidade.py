"""Rotina semanal de velocidade: PageSpeed mobile da home e das 3 páginas mais vistas de cada site ativo.

    python3 -m hub.velocidade coletar

Fatos no armazém (fonte velocidade, dim = caminho; a home também com dim ""), $HUB_DATA/velocidade/ultima.json
e issue de regressão (no máximo 1 por site a cada 28 dias).
"""
import datetime as dt
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hub import config, armazem, data, google, linear, psi, sites
from hub.comum.datas import BRT

FONTE = "velocidade"
LIM_LCP = 1.2      # regressão: LCP 20 % acima da base
LIM_NOTA = 10      # ou nota 10 pontos abaixo
DIAS_ISSUE = 28
_TRAVA = threading.Lock()  # issues.json é lido, alterado e gravado por até 6 threads

AUDITORIA = """## Auditoria em 4 frentes (KRN-723)

**1. Imagens e fontes**
- WebP/AVIF no tamanho exibido.
- `next/image` com `sizes` certo; lazy abaixo da dobra e prioridade na imagem do LCP.
- `next/font`, no máximo 2 famílias.
- Terceiros que não servem saem; analytics e chat carregam depois da página.

**2. Código do front**
- Bundle analyzer; importar só o que usa.
- Componentes pesados com `dynamic`.
- Dados da home no servidor.
- Streaming de IA; skeleton.

**3. Banco e APIs**
- Índice nas colunas de filtro e ordenação, inclusive as de RLS como `user_id`.
- Em RLS, `(select auth.uid())`.
- N+1 vira uma consulta; paginar listas longas.
- Cache só do que é igual para todos.
- Trabalho pesado em job de fundo.

**4. Servidor e rede**
- Função na mesma região do banco.
- Página que muda pouco vira estática/ISR.
- Chamadas em série viram paralelas.

**Regras**
- Medir antes e depois: PageSpeed mobile, 3 rodadas, mediana, mesmas URLs.
- PRs pequenos, um assunto por PR, regras do repo.
- Não remover funcionalidade.
- Não remover dependência, mudar região nem esquema do banco sem perguntar ao {dono}.
- Índice novo é migração revisada em PR.

**Conferir a página**
- Antes e depois: `seomator audit <url> --format llm`, sem `--no-cwv` (o LCP precisa ser medido); "depois" = depois do deploy no ar (categoria perf: LCP, CLS, peso da página). Os `critical` da categoria perf não podem aumentar; nota antes → depois no PR.
"""


def _hoje():
    return dt.datetime.now(BRT).date().isoformat()


def _dir(base_dados=None):
    return Path(base_dados or data()) / FONTE


def _ler_json(arq, padrao):
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar_json(arq, obj):
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def _top_ga4(site):
    r = google.relatorio_ga4(site["ids"]["ga4"], {"channel": "all", "limit": 6}, "get_google_analytics_page_performance")
    return [l.get("pagePath") or "" for l in r.get("rows") or []
            if l.get("hostName") in (site["dominio"], "www." + site["dominio"])]


def paginas(site, top=None):
    """["/"] + os 3 caminhos mais vistos (sem repetir "/" nem query)."""
    saida = ["/"]
    try:
        achados = (top or _top_ga4)(site)
    except (Exception, SystemExit):  # noqa: BLE001  # sem GA4 (falha ou sem id) mede só a home
        achados = []
    for p in achados:
        p = p.split("?", 1)[0].split("#", 1)[0]
        if p.startswith("/") and p not in saida:
            saida.append(p)
    return saida[:4]


def fatos(site_id, dia, medidas):
    saida = []
    for path, m in medidas.items():
        for met in psi.METRICAS:
            if m.get(met) is None:
                continue
            f = {"dia": dia, "negocio": site_id, "fonte": FONTE, "metrica": met, "dim": path, "valor": m[met]}
            saida.append(f)
            if path == "/":
                saida.append({**f, "dim": ""})
    return saida


def _base(site_id, path, metrica, dia, base_dados):
    de = (dt.date.fromisoformat(dia) - dt.timedelta(days=35)).isoformat()
    ate = (dt.date.fromisoformat(dia) - dt.timedelta(days=1)).isoformat()
    v = [x for _, x in armazem.serie(site_id, FONTE, metrica, de, ate, path, base_dados) if x is not None]
    return statistics.median(v[-4:]) if len(v) >= 4 else None


def regressoes(site_id, dia, medidas, base_dados=None):
    saida = []
    for path, m in medidas.items():
        lcp, nota = m.get("lcp_ms"), m.get("nota")
        bl, bn = _base(site_id, path, "lcp_ms", dia, base_dados), _base(site_id, path, "nota", dia, base_dados)
        if (lcp is not None and bl is not None and lcp >= bl * LIM_LCP) or \
                (nota is not None and bn is not None and nota <= bn - LIM_NOTA):
            saida.append({"pagina": path, "lcp_ms": lcp, "base_lcp": bl, "nota": nota, "base_nota": bn})
    return saida


def saude(site_id, base_dados=None):
    u = armazem.ultimo(site_id, FONTE, "lcp_ms", base_dados)
    if not u:
        return None, None
    pior = max((v for _, v in armazem.dims(site_id, FONTE, "lcp_ms", u[0], u[0], base_dados)), default=None)
    if pior is None:
        return None, None
    ultima = _ler_json(_dir(base_dados) / "ultima.json", {})
    reg = ((ultima.get("sites") or {}).get(site_id) or {}).get("regressoes")
    return ("ruim" if reg or pior > 4000 else "alerta" if pior > 2500 else "ok"), pior


def _fmt(v, nd=0):
    return "?" if v is None else f"{v:.{nd}f}".replace(".", ",")


def abrir_issue(site, regs, base_dados=None, criar=None):
    with _TRAVA:
        return _abrir_issue(site, regs, base_dados, criar)


def _abrir_issue(site, regs, base_dados, criar):
    arq = _dir(base_dados) / "issues.json"
    reg = _ler_json(arq, {})
    ant = reg.get(site["id"]) or {}
    try:
        if dt.date.fromisoformat(_hoje()) - dt.date.fromisoformat(ant["dia"]) < dt.timedelta(days=DIAS_ISSUE):
            return None
    except (KeyError, ValueError, TypeError):
        pass
    linhas = ["| Página | LCP (ms) | Base LCP | Nota | Base nota |", "|---|---|---|---|---|"]
    linhas += [f"| {r['pagina']} | {_fmt(r['lcp_ms'])} | {_fmt(r['base_lcp'])} | {_fmt(r['nota'])} | {_fmt(r['base_nota'])} |"
               for r in regs]
    corpo = (f"Regressão no PageSpeed mobile de https://{site['dominio']} contra a mediana das 4 coletas anteriores "
             f"(LCP +20 % ou nota -10 pontos).\n\n" + "\n".join(linhas) + "\n\n" + AUDITORIA.replace("{dono}", config.dono()))
    titulo = f"Velocidade de {site['nome']}: regressão no PageSpeed mobile ({', '.join(r['pagina'] for r in regs)})"
    r = (criar or linear.criar_issue)(linear.projeto_do_repo((site.get("repo") or {}).get("github")), titulo, corpo, "Todo", ["velocidade"])
    ident = r.split()[0]
    reg[site["id"]] = {"issue": ident, "dia": _hoje()}
    _gravar_json(arq, reg)
    return ident


def _site(s, dia, base_dados, medir, top, abrir):
    medidas = {}
    for p in paginas(s, top):
        m = medir("https://" + s["dominio"] + p)
        if m:
            medidas[p] = {k: m.get(k) for k in psi.METRICAS}
    if not medidas:
        return None
    regs = regressoes(s["id"], dia, medidas, base_dados)  # antes de gravar o dia
    armazem.gravar(fatos(s["id"], dia, medidas), base_dados)
    issue = None
    if regs:
        try:
            issue = (abrir or abrir_issue)(s, regs, base_dados)
        except (Exception, SystemExit) as e:  # noqa: BLE001  # Linear fora/sem projeto: a medida fica, a issue não
            print(f"{s['id']}: issue não aberta: {e}", file=sys.stderr)
    return {"paginas": medidas, "regressoes": regs, "issue": issue}


def coletar(hoje=None, base_dados=None, medir=None, top=None, abrir=None):
    dia, ch = hoje or _hoje(), psi.chave()
    medir = medir or (lambda url: psi.mediana(url, chave=ch))
    ativos = sites.ativos()

    def um(s):
        try:
            return _site(s, dia, base_dados, medir, top, abrir)
        except Exception as e:  # noqa: BLE001  # um site com erro não derruba os outros
            print(f"{s['id']}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(um, ativos))
    out = {"dia": dia, "sites": {s["id"]: r for s, r in zip(ativos, res) if r}}
    _gravar_json(_dir(base_dados) / "ultima.json", out)
    return out


def bloco_resumo(base=None):
    ultima = _ler_json(_dir(base) / "ultima.json", {})
    try:
        if (dt.datetime.now(BRT).date() - dt.date.fromisoformat(ultima["dia"])).days > 7:
            return []
    except (KeyError, ValueError, TypeError):
        return []
    nomes = {s["id"]: s["nome"] for s in sites.ativos()}
    linhas = []
    for sid, r in (ultima.get("sites") or {}).items():
        h = (r.get("paginas") or {}).get("/") or {}
        l = f"- {nomes.get(sid, sid)}: LCP home {_fmt(h['lcp_ms'] / 1000 if h.get('lcp_ms') is not None else None, 1)} s, nota {_fmt(h.get('nota'))}"
        if r.get("regressoes"):
            l += f" · regressão em {', '.join(x['pagina'] for x in r['regressoes'])} ({r.get('issue') or 'issue já aberta'})"
        linhas.append(l)
    return ["## Velocidade (semanal)"] + linhas if linhas else []


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] != ["coletar"]:
        print(__doc__)
        return 2
    r = coletar()
    for l in bloco_resumo():
        print(l)
    return 0 if r["sites"] else 1


if __name__ == "__main__":
    sys.exit(main())
