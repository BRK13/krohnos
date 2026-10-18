"""Metas do hub: objetivo + como medir + esforço. Uma meta = um JSON em $HUB_DATA/claude/metas/<id>.json.

    python3 -m hub.metas criar --objetivo "..." [--site fsite05] [--url U --metrica lcp_ms | --como "..."] [--alvo N]
    python3 -m hub.metas definir <id> ... | medir <id> [--registrar "mudança"] | registrar <id> <valor> --mudanca "..."
    python3 -m hub.metas listar
"""
import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path


from hub import config, conta, data, linear, psi, sites
from hub.comum.datas import BRT


ESFORCOS = {"baixo": 3, "medio": 5, "alto": 10}
TETO = 50
DIRECOES = ("menor", "maior")
REPO_HUB = config.repo("pereiraos-privado")
URL_KRONOS = "https://nimbus.clientec-com.example/metas"

PROTOCOLO = """**Meta:** {objetivo}
**Como medir:** {medida}. **Alvo:** {alvo}. **Esforço:** {esforco} ({k} rodadas por sessão).
Meta no {produto}: {url}#{id}

Protocolo (não negociável):
1. Branch própria no repo do site, a partir da base dele. Nunca push na base.
2. Linha de base: `python3 -m hub.metas medir {id} --registrar "linha de base"` (pagespeed) ou meça com o seu
   comando e `python3 -m hub.metas registrar {id} <valor> --mudanca "linha de base"`.
3. Uma mudança por vez. Depois de cada uma, meça e registre. O hub responde CONTA (melhorou ≥ 5 % sobre o melhor:
   mantenha e commite) ou REVERTA (desfaça a mudança antes da próxima).
4. Nunca apague funcionalidade nem teste para bater o número.
5. Dúvida: `python3 -m hub.linear perguntar {issue} --minutos 2` e siga em outra mudança; não espere.
6. Até {k} rodadas por sessão; o hub te acorda de novo em 2 h. Em 50 rodadas a meta trava: escreva aqui onde travou
   e o que tentaria depois. BATEU: abra o PR com as mudanças que contaram e encerre."""
PASSO_0 = """
0. Esta meta veio só com o texto: defina antes como medir e o alvo com
   `python3 -m hub.metas definir {id} --url ... --metrica ... --alvo ... --direcao ...` (ou `--como "..."`)."""


def pasta(base_dados=None):
    return Path(base_dados or data()) / "claude" / "metas"


def _iso(t):
    return conta.iso(t).replace("+00:00", "Z")


def _texto(s, campo, minimo=3, maximo=300):
    s = " ".join(str(s or "").split())
    if not minimo <= len(s) <= maximo:
        raise ValueError(f"{campo}: de {minimo} a {maximo} caracteres")
    return s


def _numero(v, campo):
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{campo}: não é número") from None
    if not math.isfinite(n) or n <= 0:
        raise ValueError(f"{campo}: precisa ser um número maior que zero")
    return n


def _medir(medir):
    if medir is None:
        return None
    t = medir.get("tipo")
    if t == "pagespeed":
        url = _texto(medir.get("url"), "url", 8, 300)
        if not url.startswith(("http://", "https://")) or medir.get("metrica") not in psi.METRICAS:
            raise ValueError(f"medir: url http(s) e metrica em {psi.METRICAS}")
        return {"tipo": t, "url": url, "metrica": medir["metrica"]}
    if t == "manual":
        return {"tipo": t, "como": _texto(medir.get("como"), "como")}
    raise ValueError("medir.tipo: pagespeed ou manual")


def _slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return "-".join(re.findall(r"[a-z0-9]+", s)[:3])[:24] or "meta"


def gravar(meta, base_dados=None):
    p = pasta(base_dados)
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / f".{meta['id']}.{os.getpid()}.tmp"  # sem .json: listar não enxerga meio gravado
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p / f"{meta['id']}.json")


@contextlib.contextmanager
def _trava(id, base_dados=None):
    """Uma escrita por vez por meta (painel, agenda e CLI do agente gravam o mesmo arquivo)."""
    try:
        import fcntl
    except ImportError:  # Windows dos testes: sem concorrência
        yield
        return
    p = pasta(base_dados)
    p.mkdir(parents=True, exist_ok=True)
    with open(p / f".{id}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _mudar(id, base_dados, fn):
    """Lê, aplica fn e grava sob a trava; devolve o que fn devolver e a meta."""
    with _trava(id, base_dados):
        meta = ler(id, base_dados)
        r = fn(meta)
        gravar(meta, base_dados)
    return r, meta


def ler(id, base_dados=None):
    if not re.fullmatch(r"[a-z0-9-]+", str(id)):
        raise ValueError(f"id inválido: {id}")
    return json.loads((pasta(base_dados) / f"{id}.json").read_text(encoding="utf-8"))


def listar(base_dados=None):
    out = []
    for f in sorted(pasta(base_dados).glob("m-*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("id"):
            out.append(m)
    return out


def _medida(m):
    d = m.get("medir")
    if not d:
        return "a definir"
    return d["como"] if d["tipo"] == "manual" else f"PageSpeed mobile (mediana de 3), {d['metrica']} de {d['url']}"


def _alvo(m):
    if not m.get("alvo"):
        return "a definir"
    return f"{'abaixo de' if m['direcao'] == 'menor' else 'a partir de'} {m['alvo']:g} {m.get('unidade') or ''}".strip()


def descricao(meta):
    t = PROTOCOLO.format(produto=config.get("produto.nome"), objetivo=meta["objetivo"], medida=_medida(meta), alvo=_alvo(meta), esforco=meta["esforco"],
                         k=ESFORCOS[meta["esforco"]], url=URL_KRONOS, id=meta["id"], issue=meta.get("issue") or "<issue>")
    return t + (PASSO_0.format(id=meta["id"]) if meta["estado"] == "definir" else "")


def abrir_issue(meta):
    repo = next(((s.get("repo") or {}).get("github") for s in sites.carregar() if s["id"] == meta.get("site")), None) or REPO_HUB
    return linear.criar_issue(linear.projeto_do_repo(repo), f"Meta: {meta['objetivo']}"[:120], descricao(meta), "Todo",
                              ["meta"] + (["opus"] if meta["esforco"] == "alto" else []), 3)


def criar(objetivo, *, site=None, medir=None, alvo=None, direcao="menor", unidade="", esforco="medio",
          origem="painel", base_dados=None, abrir=None, agora=None):
    objetivo = _texto(objetivo, "objetivo")
    if site is not None and site not in {s["id"] for s in sites.carregar()}:
        raise ValueError(f"site desconhecido: {site}")
    if esforco not in ESFORCOS:
        raise ValueError(f"esforço: {'|'.join(ESFORCOS)}")
    if direcao not in DIRECOES:
        raise ValueError(f"direção: {'|'.join(DIRECOES)}")
    alvo = None if alvo is None else _numero(alvo, "alvo")
    medir = _medir(medir)
    t = agora or conta.agora()
    base = f"m-{t:%Y%m%d}-{site or 'hub'}-{_slug(objetivo)}"
    id, n = base, 1
    while (pasta(base_dados) / f"{id}.json").exists():
        n += 1
        id = f"{base}-{n}"
    meta = {"id": id, "objetivo": objetivo, "site": site, "medir": medir, "alvo": alvo, "direcao": direcao,
            "unidade": str(unidade or "")[:20], "esforco": esforco, "estado": "ativa" if alvo and medir else "definir",
            "issue": None, "url_issue": None, "origem": str(origem)[:20], "criada_em": _iso(t), "acordada_em": None,
            "rodadas": [], "perguntas": []}
    gravar(meta, base_dados)
    try:
        r = (abrir or abrir_issue)(meta)
        meta["issue"], meta["url_issue"] = r.split(" ", 1) if " " in r else (r, None)
        gravar(meta, base_dados)
    except (Exception, SystemExit):
        pass  # issue fica null: o acordar abre depois
    return meta


def definir(id, *, medir, alvo, direcao="menor", unidade="", base_dados=None):
    if direcao not in DIRECOES:
        raise ValueError(f"direção: {'|'.join(DIRECOES)}")
    campos = dict(medir=_medir(medir), alvo=_numero(alvo, "alvo"), direcao=direcao, unidade=str(unidade or "")[:20])
    if not campos["medir"]:
        raise ValueError("medir obrigatório")

    def fn(meta):
        meta.update(campos)
        if meta["estado"] == "definir":
            meta["estado"] = "ativa"
    return _mudar(id, base_dados, fn)[1]


def melhor(meta):
    v = [r["valor"] for r in meta["rodadas"] if r.get("conta")]
    return (min if meta["direcao"] == "menor" else max)(v) if v else None


def bloco_resumo(base=None, agora=None):
    """Resumo das 7h: metas ativas e as que bateram/travaram nas últimas 24 h."""
    from hub.metas_tela import _v  # import tardio: a tela importa este módulo
    agora = agora or dt.datetime.now(dt.timezone.utc)
    linhas = []
    for m in listar(base):
        try:
            r = m.get("rodadas") or []
            if m["estado"] != "ativa" and not (m["estado"] in ("batida", "travada") and r
                                               and dt.datetime.fromisoformat(r[-1]["em"].replace("Z", "+00:00")) >= agora - dt.timedelta(days=1)):
                continue
            linhas.append(f"- {m['objetivo']}: {_v(m, melhor(m))} × alvo {_v(m, m.get('alvo'))} ({m['estado']}, rodada {len(r)}/{TETO})")
        except (KeyError, ValueError, TypeError):
            continue
    return ["## Metas"] + linhas if linhas else []


def faltam(meta):
    m = melhor(meta)
    return None if m is None or not meta.get("alvo") else max(0.0, (m - meta["alvo"]) * (1 if meta["direcao"] == "menor" else -1))


def registrar(id, valor, mudanca, base_dados=None, agora=None):
    valor = float(valor)
    if not math.isfinite(valor):
        raise ValueError("valor: precisa ser um número")
    mudanca = _texto(mudanca, "mudança", 1, 300)

    def fn(meta):
        if meta["estado"] in ("batida", "travada"):
            raise ValueError(f"meta {meta['estado']}: não aceita mais rodadas")
        menor, n, b = meta["direcao"] == "menor", len(meta["rodadas"]), melhor(meta)
        if b is None:
            conta_, v = True, "base"
        else:
            conta_ = valor <= b * 0.95 if menor else valor >= b * 1.05
            v = "conta" if conta_ else "reverta"
        alvo = meta.get("alvo")
        if alvo and (valor <= alvo if menor else valor >= alvo):  # bater o alvo sempre conta
            conta_, v = True, "bateu"
        if v == "bateu":
            meta["estado"] = "batida"
        elif n >= TETO:
            meta["estado"], v = "travada", "travou"
        meta["acordadas_sem_rodada"] = 0
        meta["rodadas"].append({"n": n, "em": _iso(agora or conta.agora()), "valor": valor, "mudanca": mudanca,
                                "conta": conta_})
        return v
    return _mudar(id, base_dados, fn)


def medir(id, base_dados=None, medidor=None):
    m = ler(id, base_dados)["medir"]
    if not m:
        raise ValueError("meta sem medida: rode definir primeiro")
    if m.get("tipo") != "pagespeed":
        raise ValueError("meta manual: meça com o seu comando e use registrar")
    r = (medidor or (lambda u: psi.mediana(u, chave=psi.chave())))(m["url"])
    if not r or r.get(m["metrica"]) is None:
        raise RuntimeError("PageSpeed não respondeu")
    return float(r[m["metrica"]])


def acordar(ocupadas, base_dados=None, agora=None, reabrir=None, enfileirar=None, em_loop=None, abrir=None):
    """Agenda a cada tick: meta sem issue ganha uma; meta ativa/definir parada há 2 h volta para a fila."""
    from hub import agenda
    agora = agora or conta.agora()
    reabrir, em_loop = reabrir or linear.reabrir, em_loop or agenda.em_loop
    acordadas = []
    for m in listar(base_dados):
        if m.get("estado") not in ("ativa", "definir"):
            continue
        try:
            if not m.get("issue"):
                r = (abrir or abrir_issue)(m)
                m["issue"], m["url_issue"] = r.split(" ", 1) if " " in r else (r, None)
                try:
                    gravar(m, base_dados)
                except OSError:
                    print(f"metas: {m.get('id')}: issue {m['issue']} aberta mas gravar falhou (órfã)", file=sys.stderr)
                    raise
                continue
            nome = f"linear-{m['issue'].lower()}"
            ultima = max([m.get("acordada_em"), m.get("criada_em")] + [r["em"] for r in m["rodadas"]][-1:], key=lambda x: x or "")
            if nome in ocupadas or not ultima or agora - conta.de_iso(ultima) < dt.timedelta(hours=2):
                continue
            if em_loop(nome, base_dados, m.get("acordada_em")):
                continue
            if m.get("acordadas_sem_rodada", 0) >= 6:
                _mudar(m["id"], base_dados, lambda x: x.update(estado="travada", motivo_travada="6 sessões sem registrar rodada"))
                continue
            b = melhor(m)
            fala = (f"Meta {m['id']}: rodada {len(m['rodadas'])}/{TETO}, melhor {'-' if b is None else f'{b:g}'} × alvo "
                    f"{m.get('alvo') or 'a definir'}. Continue o protocolo da descrição.")
            t = reabrir(m["issue"], fala)
            # marca já: reabriu, então não reabre a cada tick mesmo se enfileirar falhar
            _mudar(m["id"], base_dados, lambda x: x.update(acordada_em=_iso(agora)))
            if enfileirar(t, f"meta {m['id']}"):  # só sessão que entrou na fila conta para o "travada"
                _mudar(m["id"], base_dados, lambda x: x.update(acordadas_sem_rodada=x.get("acordadas_sem_rodada", 0) + 1))
                acordadas.append(nome)
        except (Exception, SystemExit) as e:  # noqa: BLE001 — uma meta com defeito não trava as outras
            print(f"metas: {m.get('id')}: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    return acordadas


def coletor_bi(hoje=None, base_dados=None):
    """Melhor valor de cada dia por meta, para o armazém do BI (fonte=meta)."""
    fatos = []
    for m in listar(base_dados):
        dias = {}
        for r in m.get("rodadas") or []:
            if not r.get("conta"):  # mudança revertida não é progresso
                continue
            dias.setdefault(conta.de_iso(r["em"]).astimezone(BRT).date().isoformat(), []).append(r["valor"])
        for dia, v in sorted(dias.items()):
            fatos.append({"dia": dia, "negocio": m.get("site") or "hub", "fonte": "meta", "metrica": m["id"], "dim": "",
                          "valor": (min if m.get("direcao") == "menor" else max)(v)})
    return fatos


def _args(argv=None):
    p = argparse.ArgumentParser(prog="hub.metas")
    s = p.add_subparsers(dest="cmd", required=True)

    def comum(q):
        q.add_argument("--url"); q.add_argument("--metrica"); q.add_argument("--como")  # noqa: E702
        q.add_argument("--alvo"); q.add_argument("--direcao", default="menor"); q.add_argument("--unidade", default="")  # noqa: E702

    c = s.add_parser("criar"); c.add_argument("--objetivo", required=True); c.add_argument("--site")  # noqa: E702
    c.add_argument("--esforco", default="medio"); c.add_argument("--origem", default="painel"); comum(c)  # noqa: E702
    d = s.add_parser("definir"); d.add_argument("id"); comum(d)  # noqa: E702
    m = s.add_parser("medir"); m.add_argument("id"); m.add_argument("--registrar")  # noqa: E702
    r = s.add_parser("registrar"); r.add_argument("id"); r.add_argument("valor"); r.add_argument("--mudanca", required=True)  # noqa: E702
    s.add_parser("listar")
    return p.parse_args(argv)


def _medir_args(a):
    if a.como:
        return {"tipo": "manual", "como": a.como}
    return {"tipo": "pagespeed", "url": a.url, "metrica": a.metrica} if a.url else None


def _resumo(meta):
    b = melhor(meta)
    return f"melhor {b:g} / alvo {meta['alvo']:g}" if b is not None and meta.get("alvo") else f"alvo {meta.get('alvo') or '-'}"


def main(argv=None):
    a = _args(argv)
    try:
        if a.cmd == "criar":
            obj = sys.stdin.read() if a.objetivo == "-" else a.objetivo
            m = criar(obj, site=a.site, medir=_medir_args(a), alvo=a.alvo, direcao=a.direcao, unidade=a.unidade,
                      esforco=a.esforco, origem=a.origem)
            print(f"{m['issue']} {m['url_issue']}" if m["issue"] and m["url_issue"] else f"{m['id']} (issue pendente)")
        elif a.cmd == "definir":
            m = definir(a.id, medir=_medir_args(a), alvo=a.alvo, direcao=a.direcao, unidade=a.unidade)
            print(f"{m['id']} {m['estado']}")
        elif a.cmd == "medir":
            v = medir(a.id)
            print(f"{v:g}")
            if a.registrar:
                ver, m = registrar(a.id, v, a.registrar)
                print(f"{ver.upper()} {_resumo(m)}")
        elif a.cmd == "registrar":
            ver, m = registrar(a.id, a.valor, a.mudanca)
            print(f"{ver.upper()} {_resumo(m)}")
        else:
            for m in listar():
                print(f"{m['id']}\t{m['estado']}\t{m.get('issue') or '-'}\t{_resumo(m)}")
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(f"erro: {e}")


if __name__ == "__main__":
    main()
