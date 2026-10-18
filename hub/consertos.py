"""O que quebrou e o que foi consertado: insumo e diário da tarefa `consertador` (e do resumo das 8h).

    python3 -m hub.consertos novidades [--desde ISO] [--marcar] [--sem-gh]   # o que quebrou desde a última rodada
    python3 -m hub.consertos registrar --quebrou "..." --causa "..." --conserto "..." \\
        --estado consertado|precisa_fulano|tentando [--prova "..."] [--pedido N|id] [--por tarefa]
    python3 -m hub.consertos ver [--horas 24]

Fontes de `novidades` (todas já no disco, menos o CI, que vem do gh): tarefas com ok=false em claude/uso.jsonl,
rotinas/falhas.jsonl, notas-tecnicas.jsonl do bot (`falha` = a consertar; `nota` com pendência = resumo), checks da coleta (latest.json), verificador que piorou, foto da VPS (vigia dos bots, contêiner
caído, deploy do Dokploy com erro), consertos do root que falharam (vps/consertos.jsonl) e CI vermelho na branch
padrão dos repos <org> ativos. `--marcar` grava a hora desta rodada em claude/consertador.json.

Diário em $HUB_DATA/consertos.jsonl, uma linha por conserto.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hub import config, conta, data, gh as gh_mod, home_agentes, saude, sites

UTC = dt.timezone.utc
ESTADOS = ("consertado", "precisa_fulano", "tentando")
REPOS_ATIVOS = ("pereiraos-privado", "triagem-wpp", "seo-kit", "cliente-a-v2", "clienteipsi-v2", "psite13",
                "cliente-c", "clienteh", "emissor-NF-api", "infra", "cliente-oestrutura")

NOTAS_TECNICAS = "/opt/asite01-wpp/data/agente/notas-tecnicas.jsonl"  # triagem-wpp PR #135: nota/falha do grupo da equipe
_PENDENTE = re.compile(
    r"pend[eê]ncia|pendente|depende (?:do|de) (?:fulano|voc[eê])|(?:fulano|voc[eê]) (?:precisa|decide|tem que|deve|vai ter que)"
    r"|preciso (?:que|do|da) |aguard(?:o|ando) (?:o |a |resposta|decis|aprova|libera)|(?:decis[aã]o|aprova[cç][aã]o) (?:do|de) fulano"
    r"|precisa (?:de )?(?:decis|aprova|libera)|escolh(?:a|er) entre|posso (?:mergear|seguir|publicar|aplicar)|[?]\s*$",
    re.IGNORECASE)


def notas_bot(desde, agora=None, arq=None):
    """Lê notas-tecnicas.jsonl do bot desde `desde`: (falhas, pendencias). `falha` é item a consertar; `nota` só
    entra em pendencias se pede algo do lado do dono (pendência ou decisão). Nunca vai ao WhatsApp."""
    agora = agora or dt.datetime.now(UTC)
    falhas, pendencias = [], []
    for r in _linhas(arq or os.environ.get("NOTAS_TECNICAS_ARQ") or NOTAS_TECNICAS):
        if not isinstance(r, dict) or not isinstance(r.get("texto"), str) or not (_em(r, "em") or agora) > desde:
            continue
        item = {k: r.get(k) for k in ("em", "tipo", "onde", "texto")}
        if r.get("tipo") == "falha":
            falhas.append(item)
        elif r.get("tipo") == "nota" and _PENDENTE.search(r["texto"]):
            pendencias.append(item)
    return falhas, pendencias


def _linhas(arq):
    try:
        texto = Path(arq).read_text(encoding="utf-8")
    except OSError:
        return []
    saida = []
    for l in texto.splitlines():
        try:
            saida.append(json.loads(l))
        except ValueError:
            continue
    return saida


def _em(r, *campos):
    for c in campos:
        if r.get(c):
            try:
                return conta.de_iso(str(r[c]))
            except ValueError:
                return None
    return None


def estado_arq(base):
    return Path(base) / "claude" / "consertador.json"


def ci_vermelho(repos=REPOS_ATIVOS, gh=None):
    """[{repo, branch, workflow, url, em}] do último run da branch padrão que falhou. Sem gh: None."""
    def chamar(args):
        r = gh_mod.executar(args, "consertos", cache=args[0] in ("api", "run"), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:200])
        return json.loads(r.stdout or "null")
    gh = gh or chamar
    saida = []
    for repo in repos:
        try:
            base = gh(["api", f"repos/{config.repo(repo)}", "--jq", "{b: .default_branch}"])["b"]
            runs = gh(["run", "list", "-R", config.repo(repo), "--branch", base, "--limit", "8",
                       "--json", "conclusion,status,workflowName,url,createdAt"]) or []
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            continue
        ultimo = {}
        for r in runs:  # o mais recente de cada workflow
            ultimo.setdefault(r.get("workflowName"), r)
        for r in ultimo.values():
            if r.get("conclusion") in ("failure", "startup_failure", "timed_out"):
                saida.append({"repo": repo, "branch": base, "workflow": r.get("workflowName"), "url": r.get("url"),
                              "em": r.get("createdAt")})
    return saida


def problemas_ponte(base, agora, home=None, servico_ativo=None):
    """Saúde da ponte Linear → Claude, só pelo disco: tick parado, item preso na fila, rodada órfã ou estourada,
    erro repetido do Linear, disjuntor e entrega que não chegou no Linear na última hora, receptor do webhook fora."""
    import os
    claude = Path(base) / "claude"
    home = Path(home or home_agentes())
    p = []
    try:
        ultimo = conta.de_iso(json.loads((claude / "agenda.json").read_text(encoding="utf-8"))["ultimo_tick"])
        if agora - ultimo > dt.timedelta(minutes=5):
            p.append(f"tick da agenda parado desde {conta.iso(ultimo)} (cron do usuário claude?)")
    except (OSError, ValueError, KeyError):
        p.append("agenda.json sem ultimo_tick")
    for arq in sorted((claude / "fila").glob("*.json")):
        r = _em(json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}, "enfileirada_em")
        if r and agora - r > dt.timedelta(hours=2):
            p.append(f"{arq.stem} parado na fila desde {conta.iso(r)}")
    from hub.agenda import _registro_vivo
    for arq in sorted(claude.glob("rodando*.json")):
        try:
            r = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or "pid" not in r:
            continue
        if not _registro_vivo(r):  # morto ou pid reaproveitado depois de reboot
            p.append(f"{arq.name}: {r.get('nome')} com o tick morto (sobra; o próximo despacho apaga)")
            continue
        ini = _em(r, "inicio")
        if ini and agora - ini > dt.timedelta(hours=6, minutes=15):
            p.append(f"{r.get('nome')} rodando há mais de 6 h (conta {r.get('conta', '1')}): travou?")
    hora = conta.iso(agora - dt.timedelta(hours=1))[:13]
    erros = {"linear": 0, "disjuntor": 0, "entrega": 0}
    for log in home.glob("saida/agenda*.log"):
        try:
            linhas = log.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]
        except OSError:
            continue
        for l in linhas:
            if l[:13] < hora:
                continue
            erros["linear"] += " linear: " in l
            erros["disjuntor"] += "disjuntor" in l
            erros["entrega"] += "não foi para o Linear" in l
    if erros["linear"] >= 5:
        p.append(f"Linear respondeu erro {erros['linear']}× na última hora (agenda*.log: 'linear:')")
    if erros["disjuntor"]:
        p.append(f"disjuntor acionado {erros['disjuntor']}× na última hora: issue em loop (agenda*.log)")
    if erros["entrega"]:
        p.append(f"{erros['entrega']} entrega(s) não chegaram no Linear na última hora (agenda*.log)")
    ativo = servico_ativo
    if ativo is None:
        try:
            ativo = subprocess.run(["systemctl", "is-active", "nimbus-linear-webhook"], capture_output=True,
                                   text=True, timeout=10).stdout.strip() == "active"
        except (OSError, subprocess.SubprocessError):
            ativo = True  # fora da VPS (testes, PC): não acusa
    if not ativo:
        p.append("receptor do webhook do Linear fora do ar (systemctl status nimbus-linear-webhook)")
    return p


def novidades(desde=None, base_dados=None, com_gh=True, agora=None, gh=None):
    base = Path(base_dados or data())
    agora = agora or dt.datetime.now(UTC)
    if desde is None:
        try:
            desde = conta.de_iso(json.loads(estado_arq(base).read_text(encoding="utf-8"))["ultima"])
        except (OSError, ValueError, KeyError):
            desde = agora - dt.timedelta(hours=24)
    n = {"desde": conta.iso(desde), "agora": conta.iso(agora)}
    novo = lambda r, *c: (_em(r, *c) or agora) > desde  # noqa: E731
    n["tarefas_falharam"] = [{k: r.get(k) for k in ("nome", "origem", "inicio", "erro", "saida", "limite")}
                             for r in _linhas(base / "claude" / "uso.jsonl") if not r.get("ok") and novo(r, "fim", "inicio")]
    n["rotinas_falharam"] = [r for r in _linhas(base / "rotinas" / "falhas.jsonl") if novo(r, "em")]
    n["root_falhou"] = [r for r in _linhas(base / "vps" / "consertos.jsonl") if r.get("codigo") not in (0, None) and novo(r, "em")]
    coleta = []
    try:
        lat = json.loads((base / "latest.json").read_text(encoding="utf-8"))
        ativos = {s["id"] for s in sites.ativos()}
        for p in lat.get("projects") or []:
            if p.get("id") in ativos:
                for k, v in (p.get("checks") or {}).items():
                    if k != "status" and isinstance(v, dict) and not v.get("ok"):
                        coleta.append({"site": p["id"], "check": k, "erro": v.get("error"), "coleta_em": lat.get("at")})
    except (OSError, ValueError):
        coleta.append({"erro": "latest.json ilegível ou ausente"})
    n["coleta_checks_falhando"] = coleta
    pioras = []
    for arq in sorted((base / "verificador").glob("*/latest.json")):
        try:
            if dt.datetime.fromtimestamp(arq.stat().st_mtime, UTC) > desde:
                j = json.loads(arq.read_text(encoding="utf-8"))
                p = j.get("pioras") or j.get("piorou") or []
                if p:
                    pioras.append({"site": arq.parent.name, "pioras": p[:10]})
        except (OSError, ValueError):
            continue
    n["verificador_piorou"] = pioras
    f = saude.ler(base) or {}
    n["vps_problemas"] = f.get("problemas", ["sem foto da saúde"])
    n["deploys_com_erro"] = [d for d in f.get("deploys") or [] if d.get("status") == "error"]
    n["ponte_linear"] = problemas_ponte(base, agora)
    n["bot_falhas"], n["bot_notas_pendentes"] = notas_bot(desde, agora)
    n["ci_vermelho"] = (ci_vermelho(gh=gh) if com_gh else None)
    n["ja_registrado_24h"] = [r for r in ver(24, base, agora)]
    return n


def marcar(base_dados=None, agora=None):
    arq = estado_arq(base_dados or data())
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps({"ultima": conta.iso(agora or dt.datetime.now(UTC))}), encoding="utf-8")


def registrar(quebrou, causa, conserto, estado, prova="", pedido="", por="consertador", base_dados=None, agora=None):
    if estado not in ESTADOS:
        raise ValueError(f"estado tem que ser um de {', '.join(ESTADOS)}")
    if not quebrou.strip():
        raise ValueError("falta o que quebrou")
    reg = {"em": conta.iso(agora or dt.datetime.now(UTC)), "por": por, "quebrou": quebrou.strip()[:300],
           "causa": causa.strip()[:500], "conserto": conserto.strip()[:500], "estado": estado,
           "prova": prova.strip()[:300], "pedido": str(pedido or "")[:80]}
    arq = Path(base_dados or data()) / "consertos.jsonl"
    with open(arq, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(reg, ensure_ascii=False) + "\n")
    return reg


def ver(horas=24, base_dados=None, agora=None):
    agora = agora or dt.datetime.now(UTC)
    corte = agora - dt.timedelta(hours=horas)
    return [r for r in _linhas(Path(base_dados or data()) / "consertos.jsonl") if (_em(r, "em") or agora) >= corte]


def blocos_resumo(base_dados=None, agora=None):
    """Para o resumo das 8h: o que quebrou e já foi consertado, e o que precisa do dono (último estado de cada)."""
    regs = ver(24, base_dados, agora)
    ultimo = {}
    for r in regs:
        ultimo[r["quebrou"]] = r
    feitos = [r for r in ultimo.values() if r["estado"] == "consertado"]
    precisa = [r for r in ultimo.values() if r["estado"] != "consertado"]
    a = ["## O que quebrou e já foi consertado (24 h)"] + (
        [f"- {r['quebrou']} → {r['conserto']}" + (f" ({r['prova']})" if r.get("prova") else "") for r in feitos] or ["- nada"])
    b = [f"## O que precisa do {config.dono()}"] + (
        [f"- {r['quebrou']}: {r['conserto'] or r['causa']}" + (f" (Pedido/proposta {r['pedido']})" if r.get("pedido") else "")
         for r in precisa] or ["- nada além das propostas abaixo"])
    notas = notas_bot((agora or dt.datetime.now(UTC)) - dt.timedelta(hours=24), agora)[1]
    b += [f"- nota do bot ({r['onde']}): {' '.join(r['texto'].split())[:300]}" for r in notas]
    return a, b


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.consertos", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("novidades")
    n.add_argument("--desde")
    n.add_argument("--marcar", action="store_true")
    n.add_argument("--sem-gh", action="store_true")
    r = sub.add_parser("registrar")
    r.add_argument("--quebrou", required=True)
    r.add_argument("--causa", default="")
    r.add_argument("--conserto", default="")
    r.add_argument("--estado", required=True, choices=ESTADOS)
    r.add_argument("--prova", default="")
    r.add_argument("--pedido", default="")
    r.add_argument("--por", default="consertador")
    v = sub.add_parser("ver")
    v.add_argument("--horas", type=int, default=24)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.cmd == "novidades":
            desde = conta.de_iso(args.desde) if args.desde else None
            print(json.dumps(novidades(desde, com_gh=not args.sem_gh), ensure_ascii=False, indent=2))
            if args.marcar:
                marcar()
        elif args.cmd == "registrar":
            reg = registrar(args.quebrou, args.causa, args.conserto, args.estado, args.prova, args.pedido, args.por)
            print(f"registrado: {reg['estado']} · {reg['quebrou']}")
        else:
            for reg in ver(args.horas):
                print(json.dumps(reg, ensure_ascii=False))
    except (ValueError, OSError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
