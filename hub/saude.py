"""Saúde da VPS: foto da máquina para o resumo diário e para a tarefa vps-saude, e os consertos seguros.

Roda como root (cron a cada 10 min, deploy/nimbus.cron):
    python3 -m hub.saude coletar     # grava $HUB_DATA/vps/saude.json
    python3 -m hub.saude consertar   # rede de segurança do executor root (hub/raiz.py processar)
    python3 -m hub.saude ver         # mostra a foto atual

O Claude (usuário claude, sem sudo) pede conserto com `python3 -m hub.raiz pedir <acao> [alvo]` (lista fechada em
hub/raiz.py ACOES); o que sai dela vira proposta tipo vps (o "faz N" do dono faz o root rodar).
A foto inclui o último deploy de cada app do Dokploy (de 30 em 30 min) para o consertador ver deploy que não subiu.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

from hub import data

UTC = dt.timezone.utc
# Backups conferidos: nome, padrão de arquivo, idade máxima em horas.
BACKUPS = [
    ("relatórios do hub", "/var/backups/nimbus/reports-*.tar.gz", 48),
    ("banco do CRM da fsite05", "/var/backups/banco-fsite05/fsite05-*.dump", 36),
]
VIVOS = {"asite01-wpp": "/opt/asite01-wpp/data/vivo.json", "bot_whatsapp": "/opt/bot_whatsapp/data/vivo.json"}
TRAEFIK = "/etc/dokploy/traefik/dynamic/*.yml"
# Disco: o cache de build fica abaixo de 75% (deploy/docker-cache.sh, de hora em hora); de 90% em diante o
# limpar-docker vai sozinho, no máximo 1x/hora, sem esperar o consertador.
LIMITES = {"disco_pct": 90, "memoria_pct": 90, "cert_dias": 14, "vivo_min": 15, "zumbis": 20}


def pasta(base_dados=None):
    return Path(base_dados or data()) / "vps"


def _rodar(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def disco():
    u = shutil.disk_usage("/")
    return {"pct": round(100 * u.used / u.total), "livre_gb": round(u.free / 1e9)}


def memoria(meminfo="/proc/meminfo"):
    m = {}
    for linha in Path(meminfo).read_text().splitlines():
        k, v = linha.split(":", 1)
        m[k] = int(v.split()[0])
    total, livre = m["MemTotal"], m.get("MemAvailable", m.get("MemFree", 0))
    swap = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
    return {"pct": round(100 * (total - livre) / total), "swap_mb": swap // 1024}


def zumbis(proc="/proc"):
    """Processos zumbis agrupados pelo pai (quem não colhe os filhos)."""
    pais = {}
    for d in glob.glob(f"{proc}/[0-9]*/stat"):
        try:
            s = Path(d).read_text()
        except OSError:
            continue
        fim = s.rfind(")")
        campos = s[fim + 2:].split()
        if campos and campos[0] == "Z":
            ppid = campos[1]
            try:
                nome = Path(f"{proc}/{ppid}/comm").read_text().strip()
            except OSError:
                nome = "?"
            pais.setdefault(f"{nome} (pid {ppid})", 0)
            pais[f"{nome} (pid {ppid})"] += 1
    return {"total": sum(pais.values()), "por_pai": dict(sorted(pais.items(), key=lambda x: -x[1])[:5])}


def reboot():
    pend = Path("/var/run/reboot-required").exists()
    pacotes = []
    if pend:
        try:
            pacotes = Path("/var/run/reboot-required.pkgs").read_text().split()[:10]
        except OSError:
            pass
    return {"pendente": pend, "pacotes": pacotes}


def atualizacoes():
    c, _, err = _rodar(["/usr/lib/update-notifier/apt-check"], 120)
    m = re.match(r"^(\d+);(\d+)", err.strip())
    return {"total": int(m.group(1)), "seguranca": int(m.group(2))} if c == 0 and m else None


def containers():
    """Serviços do Swarm com réplicas faltando e contêineres soltos (fora do Swarm) parados ou unhealthy."""
    caidos = []
    c, out, _ = _rodar(["docker", "service", "ls", "--format", "{{.Name}} {{.Replicas}}"])
    if c != 0:
        return {"erro": "docker indisponível", "caidos": []}
    for linha in out.splitlines():
        nome, _, rep = linha.partition(" ")
        m = re.match(r"(\d+)/(\d+)", rep)
        if m and int(m.group(1)) < int(m.group(2)):
            caidos.append(f"{nome} ({rep})")
    c, out, _ = _rodar(["docker", "ps", "-a", "--format", '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "com.docker.swarm.service.name"}}'])
    for linha in out.splitlines():
        partes = linha.split("\t")
        if len(partes) < 4 or partes[3]:
            continue  # tarefa do Swarm: o serviço já foi conferido acima
        nome, estado, status = partes[:3]
        if nome.startswith("buildx_buildkit"):
            continue
        if estado != "running" or "unhealthy" in status:
            caidos.append(f"{nome} ({status})")
    return {"caidos": caidos}


def vigia(agora=None, vivos=None):
    agora = agora or dt.datetime.now(UTC)
    saida = {}
    for bot, arq in (vivos or VIVOS).items():
        try:
            v = json.loads(Path(arq).read_text())
            idade = (agora - dt.datetime.fromisoformat(v["em"].replace("Z", "+00:00"))).total_seconds() / 60
            saida[bot] = {"conectado": bool(v.get("conectado")), "min_sem_sinal": round(idade)}
        except (OSError, ValueError, KeyError):
            saida[bot] = {"conectado": False, "min_sem_sinal": None}
    return saida


def backups(agora=None, lista=None):
    agora = agora or dt.datetime.now(UTC).timestamp()
    saida = []
    for nome, padrao, max_h in lista or BACKUPS:
        arqs = glob.glob(padrao)
        if not arqs:
            saida.append({"nome": nome, "ok": False, "idade_h": None, "padrao": padrao})
            continue
        ultimo = max(arqs, key=os.path.getmtime)
        idade = (agora - os.path.getmtime(ultimo)) / 3600
        saida.append({"nome": nome, "ok": idade <= max_h, "idade_h": round(idade), "arquivo": ultimo})
    return saida


def hosts_publicados(padrao=TRAEFIK):
    hosts = set()
    for arq in glob.glob(padrao):
        try:
            hosts.update(re.findall(r"Host\(`([^`]+)`\)", Path(arq).read_text()))
        except OSError:
            continue
    return sorted(h for h in hosts if "." in h and not h.endswith((".localhost", ".traefik.me", ".sslip.io")))


def certificado(host, timeout=6):
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            fim = dt.datetime.strptime(t.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
            return {"host": host, "dias": (fim - dt.datetime.now(UTC)).days}
    except (OSError, ssl.SSLError, KeyError, ValueError) as e:
        return {"host": host, "dias": None, "erro": str(e)[:120]}


def deploys(chave=None):
    """Último deploy de cada app do Dokploy: [{app, projeto, status, em, titulo, erro}]. Sem chave: None."""
    from hub import raiz
    chave = chave or raiz.ler_env(raiz.ENV_AGENTE).get("DOKPLOY_API_KEY")
    if not chave:
        return None
    saida = []
    for a in raiz.apps_dokploy(chave):
        try:
            ds = raiz._dokploy(f"deployment.all?applicationId={a['applicationId']}", chave=chave) or []
        except (OSError, ValueError):
            continue
        if ds:
            d = max(ds, key=lambda x: x.get("createdAt") or "")
            saida.append({"app": a["name"], "appName": a["appName"], "projeto": a["projeto"], "status": d.get("status"),
                          "em": d.get("createdAt"), "titulo": str(d.get("title") or "")[:100],
                          "erro": str(d.get("errorMessage") or "")[:300] or None})
    return saida


def problemas(f):
    """Lista em português simples do que está fora do normal na foto."""
    p = []
    if f["disco"]["pct"] >= LIMITES["disco_pct"]:
        p.append(f"disco em {f['disco']['pct']}%")
    if f["memoria"]["pct"] >= LIMITES["memoria_pct"]:
        p.append(f"memória em {f['memoria']['pct']}%")
    if f["zumbis"]["total"] >= LIMITES["zumbis"]:
        pai = next(iter(f["zumbis"]["por_pai"]), "?")
        p.append(f"{f['zumbis']['total']} processos zumbis (maioria de {pai})")
    if f["reboot"]["pendente"]:
        p.append("reinício pendente (atualização do sistema)")
    if (f.get("atualizacoes") or {}).get("seguranca"):
        p.append(f"{f['atualizacoes']['seguranca']} atualizações de segurança do sistema esperando")
    for c in f["containers"].get("caidos", []):
        p.append(f"fora do ar: {c}")
    if f["containers"].get("erro"):
        p.append(f["containers"]["erro"])
    for bot, v in f["vigia"].items():
        if not v["conectado"] or v["min_sem_sinal"] is None or v["min_sem_sinal"] > LIMITES["vivo_min"]:
            p.append(f"bot {bot} sem sinal" + (f" há {v['min_sem_sinal']} min" if v["min_sem_sinal"] is not None else ""))
    for b in f["backups"]:
        if not b["ok"]:
            p.append(f"backup {b['nome']}: " + ("nunca rodou" if b["idade_h"] is None else f"último há {b['idade_h']} h"))
    for d in f.get("deploys") or []:
        if d.get("status") == "error":
            p.append(f"deploy do {d['app']} ({d['projeto']}) falhou em {str(d.get('em'))[:16]}")
    p.extend(f.get("linear_git") or [])
    for c in f["certificados"]:
        if c["dias"] is None:
            p.append(f"certificado de {c['host']} não confere")
        elif c["dias"] < LIMITES["cert_dias"]:
            p.append(f"certificado de {c['host']} vence em {c['dias']} dias")
    return p


def limpeza_automatica(f, ultima, agora, base_dados=None):
    """Disco no limite: pede o limpar-docker ao root (no máximo 1x/hora). Devolve quando foi o último pedido."""
    if f["disco"]["pct"] < LIMITES["disco_pct"] or (
            ultima and agora - dt.datetime.fromisoformat(ultima) < dt.timedelta(hours=1)):
        return ultima
    from hub import raiz
    raiz.pedir("limpar-docker", motivo=f"disco em {f['disco']['pct']}% (automático, hub.saude)", por="saude",
               base_dados=base_dados)
    return f["em"]


def coletar(base_dados=None, certs=True):
    agora = dt.datetime.now(UTC)
    f = {"em": agora.isoformat(timespec="seconds"), "disco": disco(), "memoria": memoria(), "zumbis": zumbis(),
         "reboot": reboot(), "atualizacoes": atualizacoes(), "containers": containers(), "vigia": vigia(agora),
         "backups": backups()}
    anterior = ler(base_dados) or {}
    # certificado muda devagar: confere de 6 em 6 horas
    if certs and (not anterior.get("certificados_em") or
                  agora - dt.datetime.fromisoformat(anterior["certificados_em"]) > dt.timedelta(hours=6)):
        f["certificados"], f["certificados_em"] = [certificado(h) for h in hosts_publicados()], f["em"]
    else:
        f["certificados"], f["certificados_em"] = anterior.get("certificados", []), anterior.get("certificados_em")
    if not anterior.get("deploys_em") or agora - dt.datetime.fromisoformat(anterior["deploys_em"]) > dt.timedelta(minutes=30):
        try:
            f["deploys"], f["deploys_em"] = deploys(), f["em"]
        except (OSError, ValueError):
            f["deploys"], f["deploys_em"] = anterior.get("deploys"), anterior.get("deploys_em")
    else:
        f["deploys"], f["deploys_em"] = anterior.get("deploys"), anterior.get("deploys_em")
    # regra de merge → Done do Linear: de hora em hora; falha de rede não vira alerta
    if not anterior.get("linear_git_em") or agora - dt.datetime.fromisoformat(anterior["linear_git_em"]) > dt.timedelta(hours=1):
        try:
            from hub import linear
            f["linear_git"], f["linear_git_em"] = linear.conferir_automacao_git(), f["em"]
        except Exception:
            f["linear_git"], f["linear_git_em"] = anterior.get("linear_git"), anterior.get("linear_git_em")
    else:
        f["linear_git"], f["linear_git_em"] = anterior.get("linear_git"), anterior.get("linear_git_em")
    f["problemas"] = problemas(f)
    f["limpeza_em"] = limpeza_automatica(f, anterior.get("limpeza_em"), agora, base_dados)
    d = pasta(base_dados)
    d.mkdir(parents=True, exist_ok=True)
    (d / "pedidos").mkdir(exist_ok=True)
    tmp = d / ".saude.json.tmp"
    tmp.write_text(json.dumps(f, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "saude.json")
    return f


def ler(base_dados=None):
    try:
        return json.loads((pasta(base_dados) / "saude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def consertar(base_dados=None):
    """Rede de segurança do executor root: o systemd (nimbus-raiz.path) já roda na hora."""
    from hub import raiz
    return raiz.processar(base_dados)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.saude", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("coletar", "consertar", "ver"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "coletar":
        f = coletar()
        print(f"{f['em']}: " + ("; ".join(f["problemas"]) or "tudo normal"))
    elif args.cmd == "consertar":
        for r in consertar():
            print(f"{r['em']} {r.get('acao')}: código {r['codigo']}")
    else:
        print(json.dumps(ler(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
