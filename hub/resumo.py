"""Dados do resumo diário (tarefa resumo-diario): tudo já contado, para o Claude só escrever curto.

    python3 -m hub.resumo dados [--sem-gh]

Lê, sem gastar nada: snapshots da coleta das 4h (GSC por dia, GA4 por dia, Clarity 3 dias), uso.jsonl e saídas das
tarefas das últimas 24 h, o que os parceiros (João) pediram ao agente do WhatsApp, propostas pendentes, PRs abertos do <org> (gh), a foto da VPS (hub/saude.py) e a cota.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from hub import config, conta, consertos, data, gh, github_api, propostas, saude, sites
from hub.comum.datas import BRT

UTC = dt.timezone.utc


def _json(arq):
    try:
        return json.loads(Path(arq).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ultimo_snapshot(base):
    pastas = sorted(p for p in (base / "snapshots").glob("20*") if p.is_dir())
    return pastas[-1] if pastas else None


def gsc(rows):
    """Último dia com dado e a média dos dias antes (o GSC atrasa 2-3 dias)."""
    rows = sorted(rows or [], key=lambda r: r["keys"][0])
    if not rows:
        return None
    u, antes = rows[-1], rows[:-1]
    media = sum(r["clicks"] for r in antes) / len(antes) if antes else None
    return {"dia": u["keys"][0], "cliques": u["clicks"], "impressoes": u["impressions"], "posicao": round(u["position"], 1),
            "media_cliques_antes": round(media, 1) if media is not None else None}


def ga4_dia(rel, dia):
    """Sessões e conversões (key events) de um dia, do relatório GA4 com dimensão date."""
    alvo = dia.strftime("%Y%m%d")
    for r in (rel or {}).get("rows") or []:
        if r["dimensionValues"][0]["value"] == alvo:
            v = [x["value"] for x in r["metricValues"]]
            return {"sessoes": int(float(v[0])), "conversoes": round(float(v[1]))}
    return None


def ga4_7d(rel):
    tot = {"sessoes": 0, "conversoes": 0}
    for r in (rel or {}).get("rows") or []:
        v = r["metricValues"]
        tot["sessoes"] += int(float(v[0]["value"]))
        tot["conversoes"] += round(float(v[2]["value"])) if len(v) > 2 else 0
    return tot if (rel or {}).get("rows") else None


def clarity(rel):
    m = {x.get("metricName"): (x.get("information") or [{}])[0] for x in rel or [] if isinstance(x, dict)}
    if not m:
        return None
    sess = (m.get("Traffic") or {}).get("totalSessionCount") or (m.get("DeadClickCount") or {}).get("sessionsCount")
    return {"sessoes_3d": int(sess) if sess not in (None, "") else None,
            "cliques_mortos_pct": (m.get("DeadClickCount") or {}).get("sessionsWithMetricPercentage"),
            "raiva_pct": (m.get("RageClickCount") or {}).get("sessionsWithMetricPercentage")}


def numeros_sites(base, ontem):
    snap = ultimo_snapshot(base)
    linhas = [f"## Sites (coleta de {snap.name if snap else 'nenhuma'})"]
    if not snap:
        return linhas + ["sem snapshot da coleta"]
    for s in sites.ativos():
        i = s["id"]
        g = gsc((_json(snap / f"{i}-gsc.json") or {}).get("rows"))
        a = ga4_dia(_json(snap / f"{i}-ga4-dia.json"), ontem)
        a7 = ga4_7d(_json(snap / f"{i}-ga4.json"))
        c = clarity(_json(snap / f"{i}-clarity.json"))
        partes = []
        if g:
            partes.append(f"Google {g['dia'][8:10]}/{g['dia'][5:7]}: {g['cliques']} cliques (média antes {g['media_cliques_antes']}), "
                          f"{g['impressoes']} impressões, posição {g['posicao']}")
        if a:
            partes.append(f"GA4 ontem: {a['sessoes']} sessões, {a['conversoes']} conversões")
        elif a7:
            partes.append(f"GA4 7 dias: {a7['sessoes']} sessões, {a7['conversoes']} conversões")
        if c and c["sessoes_3d"] is not None:
            partes.append(f"Clarity 3 dias: {c['sessoes_3d']} sessões, cliques mortos {c['cliques_mortos_pct']}%, raiva {c['raiva_pct']}%")
        linhas.append(f"- {s['nome']}: " + ("; ".join(partes) or "sem dado"))
    return linhas


def tarefas_24h(base, agora):
    regs = [r for r in conta.ler_uso(agora - dt.timedelta(hours=24), base) if r.get("origem") != "agente"]
    linhas = ["## Tarefas das últimas 24 h"]
    for r in regs:
        trecho = ""
        try:
            trecho = Path(r["saida"]).read_text(encoding="utf-8")[:700].replace("\n", " ")
        except (OSError, KeyError, TypeError):
            pass
        linhas.append(f"- {r['nome']} ({conta.de_iso(r['inicio']).astimezone(BRT):%d/%m %H:%M}): "
                      f"{'ok' if r.get('ok') else 'FALHOU'}{' (limite de uso)' if r.get('limite') else ''}. {trecho}")
    agente = [r for r in conta.ler_uso(agora - dt.timedelta(hours=24), base) if r.get("origem") == "agente"]
    if agente:
        linhas.append(f"- agente do WhatsApp: {len(agente)} chamadas ao Claude")
    return linhas if len(linhas) > 1 else linhas + ["nenhuma tarefa rodou"]


def linha_esteira(est, agora):
    """Uma linha para o resumo das 8h: o que a esteira despachou ontem (BRT) e o estoque. Vazio sem esteira."""
    if not est or not est.get("estoque"):
        return ""
    ontem = (agora.astimezone(BRT) - dt.timedelta(days=1)).date().isoformat()
    feitas = [x for x in est.get("despachadas", []) if
              dt.datetime.fromisoformat(x["em"]).astimezone(BRT).date().isoformat() == ontem]
    cats = {}
    for x in feitas:
        cats[x["categoria"]] = cats.get(x["categoria"], 0) + 1
    detalhe = ", ".join(f"{n} {c}" for c, n in cats.items())
    return (f"Esteira ontem: {len(feitas)} entrega{'s' if len(feitas) != 1 else ''}"
            f"{f' ({detalhe})' if detalhe else ''}; estoque {est['estoque'].get('total', 0)}.")


def esteira_do_arquivo(base, agora):
    try:
        est = json.loads((conta.pasta(base) / "esteira.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    try:
        return linha_esteira(est, agora)
    except (KeyError, TypeError, ValueError, AttributeError):  # json válido mas com formato inesperado
        return ""


def propostas_pendentes(base):
    ps = propostas.listar("pendente", base) + propostas.listar("enviada", base)
    return [f"## Propostas esperando o {config.dono()}"] + ([f"- {propostas.texto_curto(p)}" for p in ps] or ["nenhuma"])


def prs_abertos():
    try:
        r = gh.executar(["search", "prs", "--owner", config.get("github.org"), "--state", "open", "--limit", "60",
                         "--json", "repository,title,url,createdAt,isDraft"], "resumo", cache=True,
                    capture_output=True, text=True, timeout=60)
        prs = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError, github_api.CotaBaixa):
        prs = None
    if prs is None:
        return ["## PRs abertos", "gh indisponível"]
    por_repo = {}
    for p in prs:
        por_repo.setdefault(p["repository"]["name"], []).append(p)
    linhas = [f"## PRs abertos ({len(prs)})"]
    for repo, ps in sorted(por_repo.items()):
        linhas.append(f"- {repo}: " + "; ".join(f"{p['title'][:70]} ({p['createdAt'][:10]})" for p in ps[:6]))
    return linhas


def vps(base):
    f = saude.ler(base)
    if not f:
        return ["## VPS", "sem foto da saúde (hub.saude coletar não rodou)"]
    return ["## VPS", f"foto de {f['em']}: disco {f['disco']['pct']}%, memória {f['memoria']['pct']}%",
            "problemas: " + ("; ".join(f["problemas"]) or "nenhum")]


def rotinas_falhas(base, agora):
    """Rotinas do hub (coleta, GEO, auditoria...) que falharam nas últimas 24 h; o rotina.sh grava, não avisa."""
    arq = base / "rotinas" / "falhas.jsonl"
    linhas, por_nome = ["## Rotinas do hub que falharam (24 h)"], {}
    if arq.exists():
        corte = agora - dt.timedelta(days=1)
        for l in arq.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l)
                if dt.datetime.fromisoformat(r["em"].replace("Z", "+00:00")) >= corte:
                    n, _ = por_nome.get(r["nome"], (0, None))
                    por_nome[r["nome"]] = (n + 1, r)  # a coleta do dinheiro roda de hora em hora: uma linha por rotina
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    linhas += [f"- {nome}: falhou {n}× · última: código {r.get('codigo', '?')} · {str(r.get('fim', ''))[-200:]}"
               for nome, (n, r) in por_nome.items()]
    return linhas if len(linhas) > 1 else linhas + ["- nenhuma"]


# Diário do agente do WhatsApp com os parceiros (triagem-wpp, vps/agente/runner.mjs: conversaParceiro).
PARCEIROS = os.environ.get("PARCEIROS_LOG", "/opt/asite01-wpp/data/agente/parceiros.jsonl")


def parceiros_24h(agora, arq=None):
    """O que cada parceiro (ex.: João no ClienteJ) pediu ao agente nas últimas 24 h e o que foi feito."""
    linhas = ["## Parceiros: o que o João pediu e o que foi feito (24 h)"]
    corte = agora - dt.timedelta(days=1)
    try:
        regs = Path(arq or PARCEIROS).read_text(encoding="utf-8").splitlines()
    except OSError:
        regs = []
    for l in regs:
        try:
            r = json.loads(l)
            em = dt.datetime.fromisoformat(r["em"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if em < corte:
            continue
        feito = (r.get("resposta") or "").replace(chr(10), " ")[:300] if r.get("ok") else f"FALHOU: {(r.get('erro') or '?')[:200]}"
        prs = f" PRs: {', '.join(r['prs'])}" if r.get("prs") else ""
        linhas.append(f"- {em.astimezone(BRT):%d/%m %H:%M} {r.get('nome', '?')}: pediu \"{(r.get('pedido') or '')[:200]}\" → {feito}{prs}")
    return linhas if len(linhas) > 1 else linhas + ["- nada"]


# Pedidos do bot (triagem-wpp, src/pedidos.js): operação (vps, pr_merge) é decidida pelo orquestrador.
PEDIDOS_BOT = os.environ.get("PEDIDOS_BOT", "/opt/asite01-wpp/data/pedidos.json")


def decisoes_orq(agora, arq=None):
    """Uma linha, sem detalhe: quantos pedidos o orquestrador aprovou e recusou nas últimas 24 h."""
    corte, soma = agora - dt.timedelta(days=1), {"faz": 0, "nao": 0}
    for p in ((_json(arq or PEDIDOS_BOT) or {}).get("itens") or {}).values():
        d = (p or {}).get("orch") or {}
        try:
            if d.get("decisao") in soma and dt.datetime.fromisoformat(d["em"].replace("Z", "+00:00")) >= corte:
                soma[d["decisao"]] += 1
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return f"Orquestrador aprovou {soma['faz']} pedidos, recusou {soma['nao']}."


def whatsapp_dono(base, agora, dias=2):
    """mensagens ao WhatsApp do dono por dia e origem (meta: menos de 20 por dia)."""
    from hub.comum import porteiro
    return ["## WhatsApp do dono", porteiro.texto_relatorio(porteiro.relatorio(dias, base, agora))]


def parado_ou_so_codigo(base, agora):
    """o que o dono precisa saber que NÃO está andando: issue de dado só com código (portão de Done) e rotina que
    roda ok sem trazer nada novo (vigia_novidade)."""
    from hub import portao, vigia_novidade
    linhas = portao.parado_ou_so_codigo(base, agora) + vigia_novidade.parados(base)
    return ["## Parado ou só código (avise o dono)"] + (linhas or ["- nada"])


def dados(agora=None, base_dados=None, com_gh=True):
    agora = agora or dt.datetime.now(UTC)
    base = Path(base_dados or data())
    ontem = (agora.astimezone(BRT) - dt.timedelta(days=1)).date()
    blocos = [[f"# Dados do resumo de {agora.astimezone(BRT):%d/%m/%Y} (ontem = {ontem:%d/%m})"],
              numeros_sites(base, ontem), *consertos.blocos_resumo(base, agora), parado_ou_so_codigo(base, agora), [decisoes_orq(agora)], tarefas_24h(base, agora),
              *([[linha]] if (linha := esteira_do_arquivo(base, agora)) else []), whatsapp_dono(base, agora), parceiros_24h(agora), rotinas_falhas(base, agora), propostas_pendentes(base)]
    if com_gh:
        blocos.append(prs_abertos())
    blocos += [vps(base), ["## Cota do Claude", conta.relatorio_uso(1, base, agora)]]
    return "\n\n".join("\n".join(b) for b in blocos) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.resumo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("dados",))
    ap.add_argument("--sem-gh", action="store_true")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(dados(com_gh=not args.sem_gh))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
