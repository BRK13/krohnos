"""Monitor da VPS: uma leitura por minuto de CPU, memória, disco, rede, processos e containers, para a tela /monitor do painel.

Roda como root pelo cron a cada 1 min (deploy/nimbus.cron); em caso de sucesso não imprime nada:
    python3 -m hub.monitor coletar   # grava $HUB_DATA/monitor/AAAA-MM-DD.jsonl, atual.json e estado.json
    python3 -m hub.monitor ver       # mostra o atual.json

Formato dos arquivos: notas do (um ponto por linha no jsonl, 14 dias de retenção). Taxas e porcentagens saem do
delta entre esta leitura e a anterior (estado.json); a primeira leitura, contador que voltou ou relógio parado dão null.
Memória em MiB/GiB (como o docker stats e o /proc); disco, docker e taxas em unidades decimais (GB, MB/s = 1e6).
Só stdlib.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

from hub import data

UTC = dt.timezone.utc
RETENCAO_DIAS = 14
JANELAS = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080}
MAX_PONTOS = 360
DF_INTERVALO = 600  # segundos entre dois `docker system df`
TOP = 5
SETOR = 512
RE_SWARM = re.compile(r"\.\d+\.[a-z0-9]{20,}$")
RE_DISCO = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
DISCO_FORA = ("loop", "ram", "zram", "dm-", "sr", "fd", "md")
REDE_FORA = ("lo", "veth", "docker", "br-", "virbr", "vx", "cali", "flannel", "tun", "tap", "cni", "kube")
CAMPOS_1_CASA = ("cpu", "nucleos", "mem_pct", "disco_pct")


def pasta(base_dados=None):
    return Path(base_dados or data()) / "monitor"


def _rodar(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def _gravar(caminho, conteudo):
    tmp = caminho.with_name("." + caminho.name + ".tmp")
    tmp.write_text(conteudo, encoding="utf-8")
    os.replace(tmp, caminho)


def _ler(caminho):
    try:
        return Path(caminho).read_text()
    except OSError:
        return None


# ---------- parse de /proc (puro) ----------

def parse_stat(texto):
    """/proc/stat -> {"cpu": (ocioso, total), "cpu0": (...), ...}. ocioso = idle+iowait; guest já está em user."""
    saida = {}
    for linha in texto.splitlines():
        p = linha.split()
        if not p or not re.fullmatch(r"cpu\d*", p[0]) or len(p) < 5:
            continue
        v = [int(x) for x in p[1:9]]
        v += [0] * (8 - len(v))
        user, nice, system, idle, iowait, irq, softirq, steal = v
        saida[p[0]] = (idle + iowait, user + nice + system + idle + iowait + irq + softirq + steal)
    return saida


def parse_meminfo(texto):
    """/proc/meminfo -> {chave: kB}."""
    m = {}
    for linha in texto.splitlines():
        k, _, v = linha.partition(":")
        if v.split():
            m[k] = int(v.split()[0])
    return m


def disco_inteiro(nome, discos=None):
    """True se o nome é um disco inteiro (não loop/ram/dm/partição). Com `discos` (de /sys/block) vale o conjunto."""
    if nome.startswith(DISCO_FORA):
        return False
    if discos is not None:
        return nome in discos
    return bool(RE_DISCO.match(nome))


def parse_diskstats(texto, discos=None):
    """/proc/diskstats -> (setores lidos, setores gravados) somados nos discos inteiros."""
    ler = gravar = 0
    for linha in texto.splitlines():
        p = linha.split()
        if len(p) < 10 or not disco_inteiro(p[2], discos):
            continue
        ler += int(p[5])
        gravar += int(p[9])
    return ler, gravar


def interface_fisica(nome):
    return not nome.startswith(REDE_FORA)


def parse_netdev(texto):
    """/proc/net/dev -> (bytes recebidos, bytes enviados) sem lo, veth, docker, pontes e túneis."""
    rx = tx = 0
    for linha in texto.splitlines():
        nome, sep, resto = linha.partition(":")
        nome = nome.strip()
        c = resto.split()
        if not sep or len(c) < 9 or not interface_fisica(nome):
            continue
        rx += int(c[0])
        tx += int(c[8])
    return rx, tx


def parse_loadavg(texto):
    return [float(x) for x in texto.split()[:3]]


# ---------- leitura crua ----------

def leitura(proc="/proc", raiz="/", sys_block="/sys/block", agora=None):
    """Leitura crua (contadores) da máquina. Parte que falhar fica None."""
    r = {"ts": (agora or dt.datetime.now(UTC)).timestamp()}

    def tentar(chave, fn):
        try:
            r[chave] = fn()
        except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
            r[chave] = None

    def discos():
        try:
            d = set(os.listdir(sys_block))
        except OSError:
            return None
        return d or None

    def disco():
        s = os.statvfs(raiz)
        total, usado = s.f_blocks * s.f_frsize, (s.f_blocks - s.f_bfree) * s.f_frsize
        return {"total": total, "usado": usado, "pct": 100 * usado / (usado + s.f_bavail * s.f_frsize)}

    tentar("cpu", lambda: parse_stat(Path(proc, "stat").read_text()))
    tentar("mem", lambda: parse_meminfo(Path(proc, "meminfo").read_text()))
    tentar("io", lambda: parse_diskstats(Path(proc, "diskstats").read_text(), discos()))
    tentar("rede", lambda: parse_netdev(Path(proc, "net/dev").read_text()))
    tentar("carga", lambda: parse_loadavg(Path(proc, "loadavg").read_text()))
    tentar("disco", disco)
    return r


def _pct_cpu(a, b):
    """% de uso entre duas amostras (ocioso, total); None se o contador voltou ou não andou."""
    if not a or not b:
        return None
    d_idle, d_total = b[0] - a[0], b[1] - a[1]
    if d_idle < 0 or d_total <= 0 or d_idle > d_total:
        return None
    return round(100 * (1 - d_idle / d_total), 1)


def _taxa(a, b, d_t, fator=1):
    if a is None or b is None or d_t is None or d_t <= 0 or b < a:
        return None
    return round((b - a) * fator / d_t / 1e6, 2)


def _gb(kb, binario=True):
    return round(kb * 1024 / (2 ** 30 if binario else 1e9), 2)


def ponto(anterior, atual, docker_gb=None):
    """Ponto do contrato a partir de duas leituras cruas (anterior pode ser None)."""
    ant = anterior or {}
    d_t = atual["ts"] - ant["ts"] if "ts" in ant else None
    p = {"t": dt.datetime.fromtimestamp(atual["ts"], UTC).isoformat(timespec="seconds")}
    cpu, cpu_a = atual.get("cpu") or {}, ant.get("cpu") or {}
    ok = d_t is not None and d_t > 0
    p["cpu"] = _pct_cpu(cpu_a.get("cpu"), cpu.get("cpu")) if ok else None
    nomes = sorted((k for k in cpu if k != "cpu"), key=lambda k: int(k[3:]))
    p["nucleos"] = [_pct_cpu(cpu_a.get(k), cpu[k]) if ok else None for k in nomes] if nomes else None
    p["carga"] = atual.get("carga")
    p["n_cpu"] = len(nomes) or None
    m = atual.get("mem")
    if m and m.get("MemTotal"):
        total, disp = m["MemTotal"], m.get("MemAvailable", m.get("MemFree", 0))
        p["mem_pct"] = round(100 * (total - disp) / total, 1)
        p["mem_usado_gb"], p["mem_total_gb"] = _gb(total - disp), _gb(total)
        p["swap_usado_gb"] = _gb(m.get("SwapTotal", 0) - m.get("SwapFree", 0))
        p["swap_total_gb"] = _gb(m.get("SwapTotal", 0))
    else:
        for k in ("mem_pct", "mem_usado_gb", "mem_total_gb", "swap_usado_gb", "swap_total_gb"):
            p[k] = None
    d = atual.get("disco")
    p["disco_pct"] = round(d["pct"], 1) if d else None
    p["disco_usado_gb"] = round(d["usado"] / 1e9, 1) if d else None
    p["disco_total_gb"] = round(d["total"] / 1e9, 1) if d else None
    io, io_a = atual.get("io"), ant.get("io")
    p["io_ler_mbs"] = _taxa(io_a and io_a[0], io and io[0], d_t, SETOR) if io and io_a else None
    p["io_gravar_mbs"] = _taxa(io_a and io_a[1], io and io[1], d_t, SETOR) if io and io_a else None
    rd, rd_a = atual.get("rede"), ant.get("rede")
    p["rede_rx_mbs"] = _taxa(rd_a and rd_a[0], rd and rd[0], d_t) if rd and rd_a else None
    p["rede_tx_mbs"] = _taxa(rd_a and rd_a[1], rd and rd[1], d_t) if rd and rd_a else None
    p["docker_gb"] = docker_gb
    return p


# ---------- processos ----------

def processos_lidos(proc="/proc"):
    """{"pid:inicio": {"pid", "ticks", "rss"}} de todos os processos; quem sumiu no meio da leitura é ignorado."""
    pagina = os.sysconf("SC_PAGE_SIZE")
    saida = {}
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return saida
    for pid in pids:
        try:
            s = Path(proc, pid, "stat").read_text()
            c = s[s.rfind(")") + 2:].split()
            ticks, inicio = int(c[11]) + int(c[12]), c[19]
            rss = int(Path(proc, pid, "statm").read_text().split()[1]) * pagina
        except (OSError, ValueError, IndexError):
            continue
        saida[f"{pid}:{inicio}"] = {"pid": int(pid), "ticks": ticks, "rss": rss}
    return saida


def _usuario(proc, pid):
    try:
        for linha in Path(proc, str(pid), "status").read_text().splitlines():
            if linha.startswith("Uid:"):
                uid = int(linha.split()[1])
                try:
                    return pwd.getpwuid(uid).pw_name
                except KeyError:
                    return str(uid)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _item(proc, p, cpu):
    """nome: o programa da linha de comando (o comm é o nome da thread: "MainThread"). Só o programa, nunca os
    argumentos: linha de comando pode levar token."""
    try:
        nome = Path(proc, str(p["pid"]), "comm").read_text().strip()
    except OSError:
        nome = "?"
    try:
        args = [a for a in Path(proc, str(p["pid"]), "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
    except OSError:
        args = []
    if args and args[0].split():
        prog = args[0].split()[0]
        nome = "claude" if "/claude/versions/" in prog else os.path.basename(prog) or nome
    return {"pid": p["pid"], "nome": nome, "usuario": _usuario(proc, p["pid"]), "cpu": cpu,
            "mem_mb": round(p["rss"] / 2 ** 20, 1)}


def processos_top(atuais, anteriores, d_t, proc="/proc", clk_tck=None, n=TOP):
    """Top n por CPU (% de um núcleo, delta de ticks desde `anteriores`) e por RAM (RSS)."""
    clk_tck = clk_tck or os.sysconf("SC_CLK_TCK")
    cpus = {}
    if anteriores and d_t and d_t > 0:
        for k, p in atuais.items():
            a = anteriores.get(k)
            if a is not None and p["ticks"] >= a:
                cpus[k] = round((p["ticks"] - a) / clk_tck / d_t * 100, 1)
    por_cpu = sorted(cpus, key=lambda k: -cpus[k])[:n]
    por_mem = sorted(atuais, key=lambda k: -atuais[k]["rss"])[:n]
    return {"cpu": [_item(proc, atuais[k], cpus[k]) for k in por_cpu],
            "mem": [_item(proc, atuais[k], cpus.get(k)) for k in por_mem]}


# ---------- docker ----------

_UNIDADES = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
             "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30, "tib": 2 ** 40}


def tamanho_bytes(texto):
    """"131.8MiB" / "12.3GB" / "0B" -> bytes (None se não entender)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", str(texto))
    if not m or m.group(2).lower() not in _UNIDADES:
        return None
    try:
        return float(m.group(1)) * _UNIDADES[m.group(2).lower()]
    except ValueError:
        return None


def _perc(texto):
    try:
        return float(str(texto).strip().rstrip("%"))
    except ValueError:
        return None


def parse_docker_stats(saida):
    """Linhas JSON do `docker stats --format '{{json .}}'` -> [{nome, cpu, mem_mb, mem_pct}] por cpu desc."""
    itens = []
    for linha in saida.splitlines():
        try:
            j = json.loads(linha)
            usado = tamanho_bytes(str(j["MemUsage"]).split("/")[0])
            itens.append({"nome": RE_SWARM.sub("", j["Name"]), "cpu": _perc(j["CPUPerc"]),
                          "mem_mb": round(usado / 2 ** 20, 1) if usado is not None else None,
                          "mem_pct": _perc(j["MemPerc"])})
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(itens, key=lambda x: -(x["cpu"] or 0))


def docker_stats():
    c, out, _ = _rodar(["docker", "stats", "--no-stream", "--format", "{{json .}}"], 15)
    return parse_docker_stats(out) if c == 0 else None


def parse_docker_df(saida, agora=None):
    """Linhas JSON do `docker system df --format '{{json .}}'` -> dict do contrato (None se não veio nada)."""
    chaves = {"Images": "imagens_gb", "Containers": "containers_gb", "Local Volumes": "volumes_gb", "Build Cache": "cache_gb"}
    r = {}
    for linha in saida.splitlines():
        try:
            j = json.loads(linha)
            b = tamanho_bytes(j["Size"])
            if j["Type"] in chaves and b is not None:
                r[chaves[j["Type"]]] = round(b / 1e9, 2)
        except (ValueError, KeyError, TypeError):
            continue
    if not r:
        return None
    for k in chaves.values():
        r.setdefault(k, 0.0)
    r["total_gb"] = round(sum(r[k] for k in chaves.values()), 2)
    r["aprox"] = False
    return {"em": _iso(agora), **r}


def parse_imagens(saida, agora=None):
    """Plano B: `docker image ls --format '{{.ID}} {{.Size}}'`, uma vez por ID, somando em imagens_gb e total_gb."""
    vistos = {}
    for linha in saida.splitlines():
        p = linha.split()
        if len(p) == 2 and tamanho_bytes(p[1]) is not None:
            vistos[p[0]] = tamanho_bytes(p[1])
    if not vistos:
        return None
    gb = round(sum(vistos.values()) / 1e9, 2)
    return {"em": _iso(agora), "imagens_gb": gb, "containers_gb": None, "volumes_gb": None, "cache_gb": None,
            "total_gb": gb, "aprox": True}


def docker_df(agora=None):
    c, out, _ = _rodar(["docker", "system", "df", "--format", "{{json .}}"], 30)
    r = parse_docker_df(out, agora) if c == 0 else None
    if r:
        return r
    c, out, _ = _rodar(["docker", "image", "ls", "--format", "{{.ID}} {{.Size}}"], 30)
    return parse_imagens(out, agora) if c == 0 else None


def _iso(agora=None):
    return (agora or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


# ---------- coleta ----------

def _ler_json(caminho):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def limpar(d, agora):
    """Apaga os jsonl com mais de RETENCAO_DIAS (pelo nome do arquivo)."""
    corte = agora.astimezone(UTC).date() - dt.timedelta(days=RETENCAO_DIAS)
    for f in d.glob("????-??-??.jsonl"):
        try:
            if dt.date.fromisoformat(f.stem) < corte:
                f.unlink()
        except (ValueError, OSError):
            continue


def coletar(base_dados=None, agora=None, proc="/proc", raiz="/"):
    """Uma coleta: lê o estado anterior, mede, grava o ponto do dia, atual.json e estado.json; devolve o atual."""
    agora = agora or dt.datetime.now(UTC)
    d = pasta(base_dados)
    d.mkdir(parents=True, exist_ok=True)
    estado = _ler_json(d / "estado.json") or {}
    df_em = estado.get("docker_df_em")
    df_velho = df_em is None or not (0 <= agora.timestamp() - df_em < DF_INTERVALO)
    # docker é lento (~2,5 s): roda em paralelo com a leitura do /proc
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_stats = ex.submit(docker_stats)
        f_df = ex.submit(docker_df, agora) if df_velho else None
        atual_cru = leitura(proc, raiz, agora=agora)
        procs = processos_lidos(proc)
        containers = f_stats.result()
        if f_df:
            # falhou agora: mantém o último bom, mas só tenta de novo daqui a 10 min (o df falho demora)
            estado["docker_df"] = f_df.result() or estado.get("docker_df")
            estado["docker_df_em"] = agora.timestamp()
    df = estado.get("docker_df")
    anterior = estado.get("leitura")
    p = ponto(anterior, atual_cru, df["total_gb"] if df else None)
    d_t = atual_cru["ts"] - anterior["ts"] if anterior and "ts" in anterior else None
    res = {"ponto": p,
           "processos": processos_top(procs, estado.get("procs") or {}, d_t, proc),
           "containers": containers, "docker": df}
    dia = dt.datetime.fromtimestamp(atual_cru["ts"], UTC).strftime("%Y-%m-%d")
    with open(d / f"{dia}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")
    _gravar(d / "atual.json", json.dumps(res, ensure_ascii=False, indent=2))
    estado["leitura"], estado["procs"] = atual_cru, {k: v["ticks"] for k, v in procs.items()}
    _gravar(d / "estado.json", json.dumps(estado, ensure_ascii=False))
    limpar(d, agora)
    return res


def atual(base_dados=None):
    return _ler_json(pasta(base_dados) / "atual.json")


def _media(valores, casas):
    v = [x for x in valores if isinstance(x, (int, float))]
    return round(sum(v) / len(v), casas) if v else None


def _reduzir_balde(balde):
    r = {"t": balde[-1]["t"]}
    for k in {k for p in balde for k in p if k != "t"}:
        casas = 1 if k in CAMPOS_1_CASA else 2
        vals = [p.get(k) for p in balde]
        if k == "n_cpu":
            r[k] = next((x for x in reversed(vals) if x is not None), None)
        elif any(isinstance(x, list) for x in vals):
            listas = [x for x in vals if isinstance(x, list)]
            r[k] = [_media([l[i] for l in listas if i < len(l)], casas) for i in range(max(len(l) for l in listas))]
        else:
            r[k] = _media(vals, casas)
    return r


def reduzir(pontos, maximo=MAX_PONTOS):
    if len(pontos) <= maximo:
        return pontos
    tam = math.ceil(len(pontos) / maximo)
    return [_reduzir_balde(pontos[i:i + tam]) for i in range(0, len(pontos), tam)]


def serie(janela, base_dados=None, agora=None):
    """Pontos da janela ("1h", "6h", "24h", "7d") em ordem, reduzidos a no máximo 360 por média em baldes."""
    minutos = JANELAS[janela]
    agora = (agora or dt.datetime.now(UTC)).astimezone(UTC)
    ini = agora - dt.timedelta(minutes=minutos)
    d = pasta(base_dados)
    pontos = []
    dia = ini.date()
    while dia <= agora.date():
        try:
            linhas = (d / f"{dia.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            linhas = []
        for linha in linhas:
            try:
                p = json.loads(linha)
                t = dt.datetime.fromisoformat(p["t"])
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(p, dict) and ini < t <= agora:
                pontos.append(p)
        dia += dt.timedelta(days=1)
    pontos.sort(key=lambda p: p["t"])
    return reduzir(pontos)


# ---------- VPS do ClienteJ (por SSH do root da HUB) ----------

REMOTOS = {"servidor_a": "192.0.2.1", "servidor_b": "192.0.2.2"}
_SEP = "@@@"


def ponto_remoto(texto, anterior=None):
    """Saída de `cat /proc/stat; echo @@@; cat /proc/meminfo; echo @@@; cat /proc/loadavg` -> (ponto, stat cru).
    CPU pela diferença com a leitura anterior (sem anterior: None)."""
    stat_txt, mem_txt, load_txt, df_txt = (texto.split(_SEP) + ["", "", ""])[:4]
    stat = parse_stat(stat_txt).get("cpu")
    mem = parse_meminfo(mem_txt)
    cpu = _pct_cpu(tuple(anterior), stat) if anterior and stat else None
    total, livre = mem.get("MemTotal"), mem.get("MemAvailable")
    disco = re.findall(r"(\d+)%", df_txt)
    return ({"cpu": cpu, "disco_pct": float(disco[-1]) if disco else None, "carga": parse_loadavg(load_txt) if load_txt.strip() else None,
             "n_cpu": sum(1 for l in stat_txt.splitlines() if re.match(r"cpu\d", l)),
             "mem_pct": round(100 * (1 - livre / total), 1) if total and livre is not None else None}, stat)


# PC do dono pelo Tailscale (Windows, OpenSSH): o root usa a chave do orch, a que o PC já aceita.
PC = {"host": "fulano@192.0.2.3", "chave": "/home/usuario/.ssh/clientek"}
_PS_PC = ('"cpu:" + (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average; '
          '$o = Get-CimInstance Win32_OperatingSystem; "mem:" + [math]::Round(100 * (1 - $o.FreePhysicalMemory / $o.TotalVisibleMemorySize), 1); '
          '"gpu:" + (nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits)')


def ponto_pc(texto):
    """Saída do _PS_PC -> {"cpu", "mem_pct", "gpu", "gpu_mem_pct"} (o que não veio fica None)."""
    v = dict(l.strip().split(":", 1) for l in texto.splitlines() if re.match(r"^(cpu|mem|gpu):", l.strip()))
    num = lambda x: float(x.replace(",", ".")) if re.fullmatch(r"[\d.,]+", (x or "").strip()) else None
    g = [num(x) for x in (v.get("gpu") or "").split(",")] + [None] * 3
    return {"cpu": num(v.get("cpu")), "mem_pct": num(v.get("mem")), "gpu": g[0],
            "gpu_mem_pct": round(100 * g[1] / g[2], 1) if g[1] is not None and g[2] else None}


def coletar_remotos(base_dados=None, agora=None, rodar=None):
    """Um giro: lê CPU, memória e carga das VPS do ClienteJ e grava monitor/remotos.json para o Cérebro."""
    rodar, agora = rodar or _rodar, agora or dt.datetime.now(dt.timezone.utc)
    arq = pasta(base_dados) / "remotos.json"
    velho = _ler_json(arq) or {}
    novo = {}
    for nome, ip in REMOTOS.items():
        cod, out, _ = rodar(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{ip}",
                             f"cat /proc/stat; echo {_SEP}; cat /proc/meminfo; echo {_SEP}; cat /proc/loadavg; echo {_SEP}; df -P /"], timeout=20)
        if cod != 0 or not out:
            novo[nome] = {**(velho.get(nome) or {}), "erro": "sem resposta"}
            continue
        p, stat = ponto_remoto(out, (velho.get(nome) or {}).get("stat"))
        anterior = velho.get(nome) or {}
        if p["cpu"] is None and anterior.get("cpu") is not None:
            # Reinício do contador: guarda o último ponto completo e inicia novo delta.
            novo[nome] = {**anterior, "stat": stat}
            continue
        novo[nome] = {**p, "t": agora.isoformat(timespec="seconds"), "stat": stat}
        if nome == "servidor_a":  # o site sobe pelo coleta/vps-deploy-local.sh, não pelo Dokploy que o hub lê
            cod, log, _ = rodar(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{ip}",
                                 "grep 'no ar: ' /opt/clienteh/deploy-local.log | tail -40"], timeout=20)
            deploys = [{"em": l.split()[0], "sha": l.split()[-1]} for l in log.splitlines() if cod == 0 and "no ar: " in l]
            novo[nome]["deploys"] = deploys or (velho.get(nome) or {}).get("deploys") or []
    import base64
    enc = base64.b64encode(_PS_PC.encode("utf-16-le")).decode()
    cod, out, _ = rodar(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
                         "-i", PC["chave"], PC["host"],
                         f"powershell -NoProfile -EncodedCommand {enc}"], timeout=30)
    p = ponto_pc(out) if cod == 0 else {}
    novo["pc"] = ({**p, "t": agora.isoformat(timespec="seconds")} if p.get("cpu") is not None
                  else {**(velho.get("pc") or {}), "erro": "desligado ou fora do Tailscale"})
    arq.parent.mkdir(parents=True, exist_ok=True)
    _gravar(arq, json.dumps(novo))
    return novo


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.monitor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("coletar", "remotos", "ver"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "coletar":
        coletar()  # sucesso não imprime nada: roda a cada minuto e o log encheria o disco
    elif args.cmd == "remotos":
        coletar_remotos()
    else:
        print(json.dumps(atual(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
