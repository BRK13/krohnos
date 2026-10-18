"""Segundo cérebro: o que só o root lê, reduzido a metadados que o claude pode indexar.

O cron do grafo (deploy/grafo.sh) começa como root; antes de virar claude roda este script, que grava em <saida> (0750 root:claude):
    segredos.json         [{caminho, nomes, vps}]: NOMES das chaves de ~/pc/segredos, /root/*/*.env, /opt/*/.env, .env dos repos
    docs/<nome>.md        cópia (com o que parece chave trocado por [oculto]) do ~/pc/LEIAME.md e dos LEIA-ME do PC
    remoto-<vps>.json     (só com `diario`/`tudo`) contêineres, serviços systemd, crons e nomes das chaves dos .env da vps-servidor_a/servidor_b,
                          lidos por SSH; o nome da chave é cortado LÁ (sed antes do "="): o valor não atravessa a rede.
Nenhum valor de segredo sai deste processo. Só stdlib, roda com `python3 -I` (não importa nada do hub nem do diretório atual).

    python3 -I hub/grafo_raiz.py <saida> [rapido|diario|tudo ...]
"""
import datetime as dt
import glob
import hashlib
import hmac
import grp
import json
import os
import re
import subprocess
import sys
from pathlib import Path

RE_VAR = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
LIMITE = 1_000_000  # arquivo maior que isso não é .env (perfil de navegador, dump): fica só o nome do arquivo
FORA = re.compile(r"\.(bak|antes|old|orig|example|sample)$|\.(bak|antes)\.|acme|\.pem$|\.crt$")

# (padrão glob, profundidade): onde ficam as credenciais fora do Dokploy
SEGREDOS = ["/home/usuario/pc/segredos/*", "/home/usuario/pc/segredos/*/*", "/root/*/*.env", "/root/*/segredos*", "/root/.env",
            "/opt/*/.env", "/opt/*/*.env", "/opt/*/.env.*", "/home/usuario/repos/*/.env", "/home/usuario/repos/*/.env.*",
            "/home/usuario/repos/*/*/.env", os.path.expanduser("~claude") + "/repos/*/.env", os.path.expanduser("~claude") + "/repos/*/.env.*", "/home/usuario/.config/*.env",
            "/home/usuario/.config/*.key"]
DOCS = ["/home/usuario/pc/LEIAME.md", "/home/usuario/pc/*/LEIAME.md", "/home/usuario/pc/*/*/LEIA-ME*.md", "/home/usuario/pc/*/*/*/LEIA-ME*.md",
        "/home/usuario/pc/*/*/LEIAME.md", "/home/usuario/pc/*/*/*/LEIAME.md"]
MAX_DOCS = 40

# o que parece valor de segredo num texto (o grafo limpa de novo ao indexar; aqui é para a cópia no disco)
CHAVE = r"\b[\w.-]*(?:key|token|secret|senha|password|passwd|pwd|pass|auth|credential)[\w.-]*"
SEP = r"[*_`'\"]*[ \t]*[=:][ \t]*[*_`]*[ \t]*"  # NOME=, NOME: e o Markdown **Senha**: / **Senha:** / `token`:
RE_OCULTAR = [re.compile(r"(?i)(" + CHAVE + SEP + r")(?:\"[^\"\n]*\"|'[^'\n]*')"),  # frase entre aspas, qualquer tamanho
              re.compile(r"(?i)(" + CHAVE + SEP + r")(?![\"'\[])[^\n|]*[^\s|]"),  # valor solto até o fim da linha (frase com espaço, "Bearer x")
              re.compile(r"(?i)(\b(?:Bearer|Basic|Token)\s+)[\w.~+/=-]{6,}"),  # cabeçalho Authorization sem o nome da chave antes
              re.compile(r"(?i)(--?[\w-]*(?:key|token|secret|senha|password|passwd|pwd|pass|auth|credential)[\w-]*[=\s])\S+"),
              re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp]|AKIA|re_|prc_|sk-or-v1)[-_A-Za-z0-9]{12,}"),
              re.compile(r"(\w+://[^:/\s]+:)[^@\s]+(@)"),
              re.compile(r"(?<![\w/.+=-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{40,}")]

VPS = {"vps-servidor_a": "192.0.2.1", "vps-servidor_b": "192.0.2.2"}
CHAVES_SSH = ["/root/.ssh/clientek", "/home/usuario/.ssh/clientek"]

# roda na VPS remota (só leitura). Nomes de .env: o sed corta antes do "="; cron: só horário, usuário e caminhos;
# ExecStart: só os caminhos (programa e script). Nunca argumentos nem KEY=valor: PGPASSWORD='frase com espaço' ou
# --password=... ficariam na rede e no disco. O hub corta de novo (so_caminhos, cron_sem_args) com a mesma regra
SCRIPT_REMOTO = r"""
# trecho entre aspas vira Q antes de quebrar por espaço (TOKEN='abc /senha def' não solta /senha); aspa sem par corta o resto
sem_aspas() { sed -E "s/\\\\ /_/g; s/'[^']*'/Q/g; s/\"[^\"]*\"/Q/g; s/['\"].*$//"; }
echo '## docker'; docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null
echo '## swarm'; docker service ls --format '{{.Name}}|{{.Image}}|{{.Replicas}}|{{.Ports}}' 2>/dev/null
echo '## systemd'
for f in /etc/systemd/system/*.service; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  printf '%s|%s|%s\n' "$(basename "$f" .service)" "$(sed -n 's/^Description=//p' "$f" | head -1)" "$(sed -n 's/^ExecStart=//p' "$f" | head -1 | sem_aspas | awk '{o=""; for(i=1;i<=NF;i++) if ($i ~ /^[-@+!:]*\// && $i !~ /[=\047"]/ && (i==1 || $(i-1) !~ /^-+[A-Za-z]/)) o=o" "$i; print substr(o,2)}' | cut -c1-160)"
done
echo '## ativos'; systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}'
echo '## cron'
for f in /etc/cron.d/*; do
  [ -f "$f" ] || continue
  grep -E '^[0-9*@]' "$f" | sem_aspas | awk '{n=($1 ~ /^@/)?2:6; o=""; for(i=1;i<=NF;i++) if (i<=n || ($i ~ /^[-@+!:]*\// && $i !~ /[=\047"]/ && $(i-1) !~ /^-+[A-Za-z]/)) o=o" "$i; print substr(o,2)}' | cut -c1-260 | sed "s|^|$(basename "$f")\t|"
done
echo '## env'
for f in /opt/*/.env /opt/*/*.env /opt/*/.env.* /root/*/*.env /root/.env /home/*/.env /home/*/*/.env; do
  [ -f "$f" ] || continue
  case "$f" in *.bak|*.example|*.sample|*.antes*) continue;; esac
  printf '%s|%s\n' "$f" "$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' "$f" | tr '\n' ' ')"
done
"""


def _caminho(t, antes):
    """Caminho absoluto que não é valor: sem "=", aspas, nem logo depois de uma opção (--password /abc123; o prefixo "-/usr/bin" do systemd não é opção)."""
    return bool(re.match(r"^[-@+!:]*/", t)) and not re.search(r"[='\"]", t) and not re.match(r"-+[A-Za-z]", antes)


def _sem_aspas(cmd):
    """O mesmo sem_aspas do SCRIPT_REMOTO: trecho entre aspas vira Q, "\\ " some, aspa sem par corta o resto da linha."""
    cmd = re.sub(r"'[^']*'|\"[^\"]*\"", "Q", cmd.replace("\\ ", "_"))
    return re.sub(r"['\"].*$", "", cmd)


def so_caminhos(cmd):
    """ExecStart sem argumentos: só os caminhos absolutos (o programa e o script)."""
    t = _sem_aspas(cmd).split()
    return " ".join(x for i, x in enumerate(t) if _caminho(x, t[i - 1] if i else ""))[:160]


def cron_sem_args(linha):
    """Linha de cron: horário e usuário (5+1 campos, ou @reboot+1) e os caminhos do comando; o resto (KEY=valor, argumentos) sai."""
    t = _sem_aspas(linha).split()
    n = 2 if t and t[0].startswith("@") else 6
    return " ".join(x for i, x in enumerate(t) if i < n or _caminho(x, t[i - 1]))[:260]


# palavra que anuncia segredo (inteira ou dentro de NOME_DA_CHAVE; "passo" e "author" não contam)
RE_PALAVRA = re.compile(r"(?i)(?<![^\W_])(?:senhas?|passwords?|passwd|pwd|pass|tokens?|secrets?|segredos?|keys?|apikey|chaves?"
                        r"|credenciais|credencial|credentials?|auth|bearer)(?![^\W_])")


def _so_caminhos(resto):
    """Do que vem depois da palavra de segredo, sobra só o caminho que existe no disco (não é senha); o resto vira [oculto]."""
    caminhos = [t.strip("`*'\"(),;|") for t in resto.split()]
    caminhos = [t for t in caminhos if t[:1] in "/~" and os.path.exists(os.path.expanduser(t.replace("~/", "/home/usuario/", 1)))]
    return " [oculto]" + "".join(" " + c for c in caminhos)


def _linhas_ocultas(texto):
    """Linha que cita senha/token/chave: depois da palavra só ficam caminhos que existem; valor na linha seguinte ou bloco de
    código logo depois também some. Tabela Markdown: a coluna cujo cabeçalho cita segredo some em todas as linhas."""
    out, colunas, esconder_prox, cerca = [], None, False, False
    for linha in texto.split("\n"):
        crua = linha.strip()
        if cerca:  # dentro do bloco de código que veio logo depois de "senha:"
            if crua.startswith("```"):
                cerca = False
                out.append(linha)
            else:
                out.append("[oculto]")
            continue
        if esconder_prox and crua:
            esconder_prox = False
            if crua.startswith("```"):
                cerca = True
                out.append(linha)
            else:
                out.append("[oculto]")
            continue
        if crua.startswith("|"):
            celulas = linha.split("|")
            if colunas is None:  # cabeçalho
                colunas = {i for i, c in enumerate(celulas) if RE_PALAVRA.search(c)}
                out.append(linha)
                continue
            if set(crua) <= set("|:- "):  # linha de separação
                out.append(linha)
                continue
            novo, depois = [], False
            for i, c in enumerate(celulas):
                if (i in colunas or depois) and c.strip():
                    c = _so_caminhos(c) + " "
                elif RE_PALAVRA.search(c):
                    m = RE_PALAVRA.search(c)
                    c = c[:m.end()] + (_so_caminhos(c[m.end():]) + " " if c[m.end():].strip(" :=*_`'\"") else c[m.end():])
                    depois = True
                novo.append(c)
            out.append("|".join(novo))
            continue
        colunas = None
        m = RE_PALAVRA.search(linha)
        if not m:
            out.append(linha)
            continue
        resto = linha[m.end():]
        if resto.strip(" :=*_`'\"-"):
            out.append(linha[:m.end()] + _so_caminhos(resto))
        else:
            out.append(linha)
            esconder_prox = True  # "Senha:" ou "Senha" sozinho: o valor pode vir na próxima linha
    return "\n".join(out)


def ocultar(texto):
    texto = _linhas_ocultas(texto)
    for rx in RE_OCULTAR:
        texto = rx.sub(lambda m: (m.group(1) if m.re.groups >= 1 and m.group(1) else "") + "[oculto]" + (m.group(2) if m.re.groups >= 2 else ""), texto)
    return texto


def nomes(p):
    """Nomes de variável de um arquivo de credencial; sem "=" (chave solta, JSON de sessão), o próprio arquivo é o segredo."""
    p = Path(p)
    try:
        if p.stat().st_size > LIMITE:
            return [p.name]
        texto = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = list(dict.fromkeys(m.group(1) for linha in texto.splitlines() for m in [RE_VAR.match(linha)] if m))
    return out or [p.name]


def hashes(p, chave=None):
    """HMAC efêmero de atribuições literais; chave nunca exportada.

    Interpolação e multilinha ficam sem hash, para não comparar expressões como valores.
    """
    try:
        p = Path(p)
        if p.stat().st_size > LIMITE:
            return {}
        texto = p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    chave = chave or os.urandom(32)
    out = {}
    for linha in texto.splitlines():
        m = RE_VAR.match(linha)
        if not m:
            continue
        valor = linha[m.end():].strip()
        if not valor or '$' in valor or '\\' in valor:
            continue
        if valor[0] in "\"'":
            if len(valor) < 2 or valor[-1] != valor[0]:
                continue
            valor = valor[1:-1]
        else:
            valor = re.split(r'\s+#', valor, maxsplit=1)[0].rstrip()
        if valor:
            out[m.group(1)] = hmac.new(chave, valor.encode(), hashlib.sha256).hexdigest()
    return out


def segredos(padroes=None):
    out, vistos = [], set()
    chave = os.urandom(32)  # só nesta coleta root; não persiste nem cruza a fronteira
    lote = os.urandom(16).hex()
    for padrao in padroes or SEGREDOS:
        for f in sorted(glob.glob(padrao)):
            real = os.path.realpath(f)
            if real in vistos or not os.path.isfile(f) or FORA.search(f):
                continue
            vistos.add(real)
            n = nomes(f)
            if n:
                out.append({"caminho": f, "nomes": n[:200], "vps": "hub", "hmacs": hashes(f, chave), "lote_hmac": lote})
    return out


def docs(padroes=None):
    out = {}
    for padrao in padroes or DOCS:
        for f in sorted(glob.glob(padrao)):
            if "/segredos/" in f or len(out) >= MAX_DOCS or not os.path.isfile(f):
                continue
            try:
                out[f] = ocultar(Path(f).read_text(encoding="utf-8", errors="replace")[:60_000])
            except OSError:
                continue
    return out


def _secoes(texto):
    sec, out = None, {}
    for linha in texto.splitlines():
        m = re.match(r"^## (\w+)$", linha)
        if m:
            sec = m.group(1)
            out.setdefault(sec, [])
        elif sec and linha.strip():
            out[sec].append(linha)
    return out


def remoto(vps, ip, rodar=None):
    """Retrato só leitura de uma VPS por SSH. None se nenhuma chave entrou (o retrato anterior continua valendo)."""
    rodar = rodar or (lambda cmd: subprocess.run(cmd, input=SCRIPT_REMOTO, capture_output=True, text=True, timeout=120))
    erro = ""
    for chave in [c for c in CHAVES_SSH if os.path.exists(c)] or [None]:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]
        cmd += ["-i", chave] if chave else []
        try:
            p = rodar(cmd + [f"root@{ip}", "bash -s"])
        except (OSError, subprocess.TimeoutExpired) as ex:
            erro = str(ex)[:200]
            continue
        if p.returncode != 0 and "## docker" not in p.stdout:
            erro = (p.stderr or "").strip()[:200]
            continue
        s = _secoes(p.stdout)
        corta = lambda linhas, n: [(l.split("|") + [""] * n)[:n] for l in linhas]  # noqa: E731
        env = []
        for caminho, ns in corta(s.get("env", []), 2):
            ns = [n for n in ns.split() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]
            if ns:
                env.append({"caminho": caminho, "nomes": ns[:200], "vps": vps})
        return {"vps": vps, "ip": ip, "quando": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "docker": corta(s.get("docker", []), 3), "swarm": corta(s.get("swarm", []), 4), "systemd": [[n, d, so_caminhos(e)] for n, d, e in corta(s.get("systemd", []), 3)],
                "ativos": [l.strip() for l in s.get("ativos", []) if l.strip()],
                "cron": [[a, cron_sem_args(b)] for a, b in (l.split("\t", 1) for l in s.get("cron", []) if "\t" in l)], "env": env}
    print(f"aviso: {vps}: ssh falhou ({erro})", file=sys.stderr)
    return None


def _gravar(p, conteudo, gid):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(conteudo, encoding="utf-8")
    os.chmod(tmp, 0o640)
    if gid is not None:
        os.chown(tmp, 0, gid)
    os.replace(tmp, p)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    saida, grupos = Path(argv[0]), argv[1:]
    try:
        gid = grp.getgrnam("claude").gr_gid if os.geteuid() == 0 else None
    except KeyError:
        gid = None
    (saida / "docs").mkdir(parents=True, exist_ok=True)
    for d in (saida, saida / "docs"):
        os.chmod(d, 0o750)
        if gid is not None:
            os.chown(d, 0, gid)
    _gravar(saida / "segredos.json", json.dumps(segredos(), ensure_ascii=False, indent=1), gid)
    lidos = docs()
    for f, texto in lidos.items():
        nome = re.sub(r"[^\w.-]+", "-", f.strip("/"))
        _gravar(saida / "docs" / nome, f"<!-- origem: {f} -->\n{texto}", gid)
    for velho in (saida / "docs").glob("*"):
        if velho.name not in {re.sub(r"[^\w.-]+", "-", f.strip("/")) for f in lidos}:
            velho.unlink()
    if any(g in ("diario", "tudo", "infra") for g in grupos):
        for vps, ip in VPS.items():
            r = remoto(vps, ip)
            if r:
                _gravar(saida / f"remoto-{vps}.json", json.dumps(r, ensure_ascii=False, indent=1), gid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
