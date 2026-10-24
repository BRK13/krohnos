"""Executor root da VPS: o que o Claude (usuário `claude`, sem sudo) pode mandar o root fazer, e nada além.

Duas entradas, uma saída por pedido. Quem dispara é o systemd (`deploy/nimbus-raiz.path` vigia as duas
pastas e roda `python3 -m hub.raiz processar` na hora); o cron de 10 min da saúde roda o mesmo como rede de segurança.

1. Consertos seguros, sem o dono ($HUB_DATA/vps/pedidos/, gravável pelo claude):
       python3 -m hub.raiz pedir <acao> [alvo] --motivo "..."      # devolve o id do pedido
       python3 -m hub.raiz resultado <id> [--esperar 600]          # código e fim da saída
   Só as ações de ACOES, com o alvo conferido contra a máquina (serviço que existe, rotina do cron do repositório).
   Banco, Dokploy e proxy ficam de fora (derrubam tudo): isso é Pedido N tipo vps.

2. Pedido N tipo `vps` aprovado pelo dono ($ROOT_FILA, padrão /opt/asite01-wpp/data/root-fila, só root escreve: é o
   bot do WhatsApp, que roda como root no contêiner, quem grava o pedido no "faz N"). O arquivo traz os comandos que o
   o dono viu e o sha256 deles; o executor confere arquivo do root, hash dos comandos e o mesmo hash no pedido
   guardado pelo bot (pedidos.json, que o claude não escreve), roda `bash -e -o pipefail` com timeout e devolve
   ✅/❌ + 3 linhas ao bot (feitos do agente), que manda no privado do dono. Saída completa só para o root em
   /var/log/nimbus/root-fila/.

Registro de tudo em $HUB_DATA/vps/consertos.jsonl (o claude lê).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from hub import config, RAIZ, data, home, home_agentes
from hub.destino_execucao import ALIAS as ALIAS_HOST, HOST_PADRAO
from hub.comum.datas import BRT
from hub.comum import env as comum_env

UTC = dt.timezone.utc
DEPLOY_RODANDO_MIN = 180  # deploy-app espera deploy running mais novo que isso; o travado o vigia de deploy cancela antes
ADIADO = 75  # _deploy_app: tem deploy rodando, o pedido volta na fila de adiados
ROOT_FILA = Path(os.environ.get("ROOT_FILA", "/opt/asite01-wpp/data/root-fila"))
PEDIDOS_BOT = Path(os.environ.get("PEDIDOS_BOT", "/opt/asite01-wpp/data/pedidos.json"))
FEITOS_AGENTE = Path(os.environ.get("FEITOS_AGENTE", "/opt/asite01-wpp/data/agente/feitos"))
FILA_AGENTE = Path(os.environ.get("FILA_AGENTE", "/opt/asite01-wpp/data/agente/fila"))
LOG_ROOT = Path(os.environ.get("ROOT_FILA_LOG", "/var/log/nimbus/root-fila"))
ENV_AGENTE = Path("/etc/agente-fsite06.env")
TOKEN_CLAUDE = home_agentes() / ".claude-token.env"
RUNNER = Path("/opt/bot/bot_whatsapp")
DOKPLOY = os.environ.get("DOKPLOY_URL") or config.legado("dokploy.url") or ""
RE_ALVO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
RE_PID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,40}-[0-9a-f]{4}$")
RE_HASH = re.compile(r"^[0-9a-f]{64}$")
# Derrubar estes derruba tudo ou perde dado em voo: só com "faz N" (tipo vps).
PROTEGIDOS = re.compile(r"dokploy|traefik|postgres|banco|redis|mysql|mariadb|mongo", re.I)
ATRASO_INCIDENTE_S = 2 * 3600  # hub instalado atrás da main por mais que isso abre o incidente hub_quebrado_em_producao
RE_SHA_ATUALIZAR = re.compile(r"^ATUALIZAR_SHA=([0-9a-f]{40})\s*$", re.M)  # impresso por deploy/atualizar.sh logo depois do pull
RE_TESTES_REPROVADOS = re.compile(r"^ATUALIZAR_TESTES_REPROVADOS\s*$", re.M)
RE_TESTE_FALHO = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)", re.M)
RE_FORCAR = re.compile(r"\[for[cç]ar\]", re.I)
ENV_PADRAO = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}

# acao: (precisa de alvo, timeout em segundos, descrição para o prompt)
ACOES = {
    "limpar-journal": (False, 300, "logs do sistema acima de 500 MB"),
    "limpar-docker": (False, 900, "imagens soltas e cache de build do Docker"),
    "limpar-apt": (False, 300, "cache de pacotes do apt"),
    "reiniciar-servico": (True, 600, "serviço do Swarm (docker service update --force); menos banco, Dokploy e proxy"),
    "reiniciar-container": (True, 300, "contêiner fora do Swarm (docker restart); menos banco, Dokploy e proxy"),
    "rerodar-rotina": (True, 3600, "uma rotina do deploy/nimbus.cron pelo nome (coleta, geo, leitura...)"),
    "rodar-rotina-agora": (True, 3600, "como rerodar-rotina, com FORCAR=1 (ignora o desligada na tela Rotinas); só o painel pede; codigo 79 = pulada, já rodava"),
    "atualizar-hub": (False, 2400, "deploy/atualizar.sh: puxa main do krohnos, testa e instala"),
    "atualizar-runner": (False, 600, "git pull do runner do agente (bot_whatsapp) e restart, se não houver job rodando"),
    "deploy-app": (True, 120, "redeploy de um app do Dokploy pelo nome (o que está na branch dele)"),
}


def pasta(base_dados=None):
    return Path(base_dados or data()) / "vps"


def agora_iso():
    return dt.datetime.now(UTC).isoformat(timespec="seconds")


def _gravar(arq, obj):
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, arq)


def _registrar(reg, base_dados=None):
    arq = pasta(base_dados) / "consertos.jsonl"
    arq.parent.mkdir(parents=True, exist_ok=True)
    with open(arq, "a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


def _rodar(cmd, timeout, env=None, cwd="/"):
    """Roda e devolve (código, saída+erro). Timeout mata o grupo inteiro (124)."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", env=env or dict(ENV_PADRAO), cwd=cwd, start_new_session=True)
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return 124, f"{out or ''}\nTEMPO ESGOTADO ({timeout // 60} min)"


ler_env = comum_env.ler_env


# ---- 1. consertos seguros ----

def pedir(acao, alvo=None, motivo="", por="claude", base_dados=None):
    """Grava o pedido na pasta do claude. Valida só a forma; o root confere o alvo contra a máquina."""
    if acao not in ACOES:
        raise ValueError(f"ação desconhecida {acao!r}; existem: {', '.join(ACOES)}")
    precisa = ACOES[acao][0]
    if precisa and not (alvo and RE_ALVO.match(alvo)):
        raise ValueError(f"{acao} precisa de um alvo (letras, números, . _ -)")
    if not precisa and alvo:
        raise ValueError(f"{acao} não leva alvo")
    pid = f"{dt.datetime.now(UTC):%Y%m%d-%H%M%S}-{acao}-{secrets.token_hex(2)}"
    _gravar(pasta(base_dados) / "pedidos" / f"{pid}.json",
            {"id": pid, "acao": acao, "alvo": alvo, "motivo": motivo[:500], "por": por[:80], "em": agora_iso()})
    return pid


def _servicos():
    c, out = _rodar(["docker", "service", "ls", "--format", "{{.Name}}"], 60)
    return set(out.split()) if c == 0 else set()


def _containers_soltos():
    c, out = _rodar(["docker", "ps", "-a", "--format", '{{.Names}}\t{{.Label "com.docker.swarm.service.name"}}'], 60)
    soltos = set()
    for linha in out.splitlines() if c == 0 else []:
        nome, _, servico = linha.partition("\t")
        if nome and not servico.strip():
            soltos.add(nome)
    return soltos


def rotinas(cron=None):
    """{nome: linha de comando} das rotinas do deploy/nimbus.cron (o arquivo do repositório é o da VPS)."""
    cron_padrao = cron is None
    cron = Path(cron or RAIZ / "deploy" / "nimbus.cron")
    saida = {}
    # sem o crontab (instalação pública não traz deploy/nimbus.cron) não há rotina de sistema, sem erro
    for linha in (cron.read_text(encoding="utf-8").splitlines() if cron.is_file() else []):
        m = re.match(r"^\s*[^#\s]\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+root\s+(\S*/rotina\.sh\s+(\S+)\s+.*)$", linha)
        if m:
            saida.setdefault(m.group(2), m.group(1))
    if cron_padrao:  # rotinas dos plugins ligados (hub/plugins.py); a linha do cron, se existe, vale
        # Roda como ROOT e importa código dos plugins: nada que o painel (ou o usuário claude) grava pode decidir quais. Só o
        # krohnos.toml e o ambiente valem (plugins.caminho, plugins.ligados, caminhos.*); a sobreposição krohnos.painel.toml é ignorada.
        from hub import plugins
        with config.sem_sobreposicao():
            plugins.recarregar()  # a carga em cache pode ter vindo com a sobreposição; refaz sem ela e não deixa a nossa para trás
            try:
                rot = plugins.rotinas()
            finally:
                plugins.recarregar()
        for nome, linha in rot.items():
            saida.setdefault(nome, linha)
    return saida


def _dokploy(caminho, corpo=None, chave=None):
    import urllib.request
    req = urllib.request.Request(f"{DOKPLOY}/api/{caminho}", method="POST" if corpo is not None else "GET",
                                 data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"x-api-key": chave, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode("utf-8", "replace")
    return json.loads(txt) if txt.strip() else {}


def apps_dokploy(chave):
    """[{applicationId, name, appName, projeto}] de todos os projetos/ambientes."""
    saida = []
    for p in _dokploy("project.all", chave=chave) or []:
        for e in p.get("environments") or [{"applications": p.get("applications") or []}]:
            for a in e.get("applications") or []:
                app_name = a.get("appName")
                if not app_name and a.get("applicationId"):  # Dokploy novo tirou appName do project.all
                    try:
                        app_name = (_dokploy(f"application.one?applicationId={a['applicationId']}", chave=chave) or {}).get("appName")
                    except (OSError, ValueError):
                        app_name = None
                saida.append({"applicationId": a.get("applicationId"), "name": a.get("name"),
                              "appName": app_name, "projeto": p.get("name")})
    return saida


def comando_da_acao(acao, alvo, runner_ocupado=None):
    """Lista do comando a rodar, ou ValueError com o motivo da recusa. Confere o alvo contra a máquina."""
    if acao == "limpar-journal":
        return ["journalctl", "--vacuum-size=500M"]
    if acao == "limpar-docker":
        return ["sh", "-c", f"docker image prune -f && bash {Path(__file__).parent.parent / 'deploy' / 'docker-cache.sh'}"]
    if acao == "limpar-apt":
        return ["apt-get", "clean"]
    if acao in ("reiniciar-servico", "reiniciar-container") and PROTEGIDOS.search(alvo):
        raise ValueError(f"{alvo} é protegido (banco, Dokploy ou proxy): isso é Pedido N tipo vps")
    if acao == "reiniciar-servico":
        if alvo not in _servicos():
            raise ValueError(f"serviço {alvo} não existe no Swarm")
        return ["docker", "service", "update", "--force", "--detach=false", "--quiet", alvo]
    if acao == "reiniciar-container":
        if alvo not in _containers_soltos():
            raise ValueError(f"contêiner {alvo} não existe fora do Swarm (serviço do Swarm: reiniciar-servico)")
        return ["docker", "restart", alvo]
    if acao in ("rerodar-rotina", "rodar-rotina-agora"):
        r = rotinas()
        if alvo not in r:
            raise ValueError(f"rotina {alvo} não está no deploy/nimbus.cron (existem: {', '.join(sorted(r))})")
        return ["bash", "-c", f"cd {RAIZ} && {'FORCAR=1 ' if acao == 'rodar-rotina-agora' else ''}{r[alvo]}"]
    if acao == "atualizar-hub":
        return ["bash", str(RAIZ / "deploy" / "atualizar.sh")]
    if acao == "atualizar-runner":
        ocupado = runner_ocupado() if runner_ocupado else _runner_ocupado()
        if ocupado:
            raise ValueError(f"runner ocupado ({ocupado}); peça de novo daqui a pouco")
        return ["bash", "-c", SCRIPT_RUNNER]
    if acao == "deploy-app":
        return ["dokploy-deploy", alvo]
    raise ValueError(f"ação desconhecida {acao!r}")


# Pull como claude, com o GH_TOKEN dele só no ambiente do git; o serviço novo entra se mudou.
SCRIPT_RUNNER = f"""set -euo pipefail
tok="$(grep '^GH_TOKEN=' {TOKEN_CLAUDE} | head -1 | cut -d= -f2- | tr -d "\\"'")"
antes=$(runuser -u claude -- git -C {RUNNER} rev-parse --short HEAD)
runuser -u claude -- env GH_TOKEN="$tok" git -C {RUNNER} -c credential.helper= -c "credential.helper=!gh auth git-credential" pull --ff-only -q
depois=$(runuser -u claude -- git -C {RUNNER} rev-parse --short HEAD)
for s in {RUNNER}/vps/agente/*.service; do
  [ -f "$s" ] || continue
  d=/etc/systemd/system/$(basename "$s")
  cmp -s "$s" "$d" || {{ install -m 644 "$s" "$d"; systemctl daemon-reload; systemctl enable -q "$(basename "$s")"; }}
done
systemctl restart agente-fsite06
sleep 3
systemctl is-active agente-fsite06
echo "runner $antes -> $depois"
"""


def _runner_ocupado():
    try:
        rodando = [f for f in os.listdir(FILA_AGENTE) if f.endswith(".rodando")]
    except OSError:
        return None
    return ", ".join(rodando) if rodando else None


def _apps_do_alvo(apps, alvo):
    """Nome, appName ou id exato ganha; só sem exato vale o nome do serviço no Swarm (casa todo app "site")."""
    apps = list({a["applicationId"]: a for a in apps}.values())  # o mesmo app pode vir de mais de um ambiente
    exatos = [a for a in apps if alvo in (a["name"], a["appName"], a["applicationId"])]
    return exatos or [a for a in apps if re.fullmatch(rf"(?:[a-z0-9-]+-)?{re.escape(a['name'] or '?')}-[a-z0-9]{{6}}", alvo)]


def _site_do_alvo(alvo):
    from hub import sites
    for s in sites.carregar():
        repo = (s.get("repo") or {}).get("github", "").split("/")[-1]
        nomes = {n + suf for n in [s["id"], *s["ids_antigos"], repo] if n for suf in ("", "-site")}
        if alvo in nomes:
            return s
    return None


def _alvo_do_site(alvo):
    """'fsite06', 'cliente-a', 'cliente-a-site' ou o nome do repo (cliente-a-v2) viram o applicationId de sites/*.json."""
    s = _site_do_alvo(alvo)
    return (s["ids"].get("dokploy_app") if s else None) or alvo


def _deploy_app(alvo):
    chave = ler_env(ENV_AGENTE).get("DOKPLOY_API_KEY")
    if not chave:
        return 1, "sem DOKPLOY_API_KEY em /etc/agente-fsite06.env"
    apps = apps_dokploy(chave)
    achados = _apps_do_alvo(apps, alvo) or _apps_do_alvo(apps, _alvo_do_site(alvo))
    apps = achados
    if not apps and (s := _site_do_alvo(alvo)) and not s["ids"].get("dokploy_app"):
        return 1, f"{s['id']} não está neste Dokploy: o deploy sobe sozinho no merge; confira o commit em /api/versao"
    if len(apps) != 1:
        quais = "; ".join(f"{a['projeto']}/{a['name']} ({a['appName']}, {a['applicationId']})" for a in apps)
        return 1, f"app {alvo!r}: {'não achei' if not apps else 'mais de um com esse nome: ' + quais} no Dokploy"
    a = apps[0]
    if PROTEGIDOS.search(f"{a['name']} {a['appName']}"):
        return 1, f"{a['name']} é protegido: Pedido N tipo vps"
    if desde := _deploy_rodando(a["applicationId"], chave):
        return ADIADO, (f"já tem deploy rodando desde {desde.astimezone(BRT):%H:%M} (Brasília) em {a['projeto']}/{a['name']}: "
                        "não disparei outro agora (cancelaria o que está rodando); disparo quando ele terminar")
    _dokploy("application.deploy", {"applicationId": a["applicationId"]}, chave)
    return 0, f"deploy disparado: {a['projeto']}/{a['name']} ({a['appName']})"


def _deploy_rodando(application_id, chave, agora=None):
    """Início do deploy em running do app há menos de DEPLOY_RODANDO_MIN (um novo cancela o anterior).
    Mais velho que isso é registro órfão do Dokploy; API fora do ar não segura o pedido."""
    agora = agora or dt.datetime.now(UTC)
    try:
        deploys = _dokploy(f"deployment.all?applicationId={application_id}", chave=chave) or []
    except (OSError, ValueError):
        return None
    inicios = [dt.datetime.fromisoformat(str(d.get("startedAt") or d["createdAt"]).replace("Z", "+00:00"))
               for d in deploys if d.get("status") == "running"]
    recentes = [t for t in inicios if agora - t < dt.timedelta(minutes=DEPLOY_RODANDO_MIN)]
    return max(recentes) if recentes else None


def _sha_main():
    """SHA da main no GitHub (git ls-remote, como o atualizar.sh usa o remoto do root); None se não deu: a trava não recusa no escuro."""
    codigo, saida = _rodar(["git", "-C", str(RAIZ), "ls-remote", "origin", "refs/heads/main"], 30)
    m = re.match(r"([0-9a-f]{40})\s", saida or "")
    return m.group(1) if codigo == 0 and m else None


def _arq_estado_atualizar(base_dados=None):
    return pasta(base_dados) / "atualizar-hub-estado.json"


def estado_atualizar(base_dados=None):
    """Último resultado do atualizar-hub: {instalado_sha, instalado_em, reprovada: {sha, em, testes}, falha_desde, incidente}."""
    try:
        e = json.loads(_arq_estado_atualizar(base_dados).read_text(encoding="utf-8"))
        return e if isinstance(e, dict) else {}
    except (OSError, ValueError):
        return {}


def _recusar_mesma_head(base_dados, motivo, sha):
    """a main que acabou de reprovar nos testes não roda a suíte (~11 min de CPU) de novo. `[forcar]` no motivo libera
    (teste instável); `sha` None (sem rede) também."""
    rep = estado_atualizar(base_dados).get("reprovada") or {}
    if not (sha and rep.get("sha") == sha) or RE_FORCAR.search(motivo or ""):
        return
    # a frase vem primeiro: o resultado guarda só os últimos 1500 caracteres, e a lista longa cortaria o começo
    lista = rep.get("testes") or []
    testes = ", ".join(lista[:5]) + (f" (+{len(lista) - 5})" if len(lista) > 5 else "")
    raise ValueError(f"mesma HEAD já reprovada: conserte os testes antes ({sha[:7]}"
                     f"{'; ' + testes if testes else ''}). Com teste instável, `[forcar]` no motivo roda de novo")


def _vigiar_atraso(base_dados=None, agora=None):
    """hub instalado mais de 2 h atrás da main = um incidente hub_quebrado_em_producao, com a lista dos testes."""
    est = estado_atualizar(base_dados)
    desde = est.get("falha_desde")
    agora = agora or dt.datetime.now(UTC)
    try:
        atraso = (agora - dt.datetime.fromisoformat(desde)).total_seconds() if desde else 0
    except ValueError:
        return None
    if atraso < ATRASO_INCIDENTE_S or est.get("incidente"):
        return None
    from hub import incidentes
    rep = est.get("reprovada") or {}
    testes = ", ".join(rep.get("testes") or []) or "sem teste identificado (ver o resultado do pedido)"
    inc = incidentes.registrar(f"hub instalado {atraso / 3600:.1f} h atrás da main: atualizar-hub falha desde {desde}"
                               f" (HEAD {str(rep.get('sha') or '?')[:7]}); testes: {testes}",
                               "hub_quebrado_em_producao", "deploy/atualizar.sh", chave="hub-atrasado-main", prioridade=1,
                               base_dados=base_dados, agora=agora)
    est["incidente"] = inc["id"]
    _gravar(_arq_estado_atualizar(base_dados), est)
    return inc


def _guardar_resultado_atualizar(base_dados, codigo, saida, sha, agora=None):
    """Grava o que o atualizar-hub deu na HEAD `sha`: sucesso zera o atraso e fecha o incidente; falha dos testes grava a HEAD reprovada."""
    agora = agora or dt.datetime.now(UTC)
    est = estado_atualizar(base_dados)
    if codigo == 0:
        inc = est.get("incidente")
        est = {"instalado_sha": sha, "instalado_em": agora.isoformat()}
        if inc:
            try:
                from hub import incidentes
                incidentes.marcar(inc, "feito", "sem mudança de código: o atualizar-hub voltou rc=0 e o hub alcançou a main",
                                  base_dados=base_dados, agora=agora)
            except Exception:  # noqa: BLE001 - o estado da instalação não pode depender do incidente
                pass
    elif codigo != ADIADO:
        est.setdefault("falha_desde", agora.isoformat())
        if sha and RE_TESTES_REPROVADOS.search(saida or ""):
            est["reprovada"] = {"sha": sha, "em": agora.isoformat(), "testes": [n if "." in n else f"{c}.{n}" for n, c in RE_TESTE_FALHO.findall(saida)][:20]}
    else:
        return
    _gravar(_arq_estado_atualizar(base_dados), est)


def executar_pedido(arq, base_dados=None, rodar=None, sha_main=None):
    """Um arquivo da pasta de pedidos → resultado gravado e registrado. Apaga o pedido sempre."""
    rodar = rodar or _rodar
    sha_main = sha_main or _sha_main
    nome = arq.name
    try:
        if arq.is_symlink() or not arq.is_file():
            raise ValueError("não é arquivo comum")
        if nome.endswith(".json"):
            p = json.loads(arq.read_text(encoding="utf-8") or "{}")
            pid, acao, alvo = p.get("id") or arq.stem, p.get("acao"), p.get("alvo")
            motivo, por = str(p.get("motivo") or "")[:500], str(p.get("por") or "claude")[:80]
        else:  # formato antigo: arquivo vazio com o nome da ação
            pid, acao, alvo, motivo, por = f"{dt.datetime.now(UTC):%Y%m%d-%H%M%S}-{nome}-0000", nome, None, "", "claude"
    except (OSError, ValueError) as e:
        arq.unlink(missing_ok=True)
        return {"em": agora_iso(), "acao": "?", "arquivo": nome, "codigo": 2, "saida": f"pedido ilegível: {e}"}
    arq.unlink(missing_ok=True)
    if not RE_PID.match(str(pid)):
        pid = f"{dt.datetime.now(UTC):%Y%m%d-%H%M%S}-pedido-{secrets.token_hex(2)}"
    reg = {"id": pid, "em": agora_iso(), "acao": acao, "alvo": alvo, "motivo": motivo, "por": por}
    inicio = time.monotonic()
    try:
        if acao not in ACOES:
            raise ValueError(f"ação desconhecida {acao!r}")
        if ACOES[acao][0] and not (isinstance(alvo, str) and RE_ALVO.match(alvo)):
            raise ValueError(f"{acao} precisa de alvo válido")
        cmd = comando_da_acao(acao, alvo)
        if acao == "atualizar-hub":
            atual = sha_main()
            if atual:
                reg["sha"] = atual  # o resultado sempre mostra a HEAD; a que o script imprime depois do pull vale mais
            _recusar_mesma_head(base_dados, motivo, atual)
        if cmd[0] == "dokploy-deploy":
            codigo, saida = _deploy_app(alvo)
            if codigo == ADIADO:  # o deploy rodando pode ter clonado antes do commit pedido: dispara de novo depois
                _gravar(pasta(base_dados) / "adiados" / f"deploy-app-{alvo}.json", {**p, "id": pid})
                codigo = 0
        else:
            codigo, saida = rodar(cmd, ACOES[acao][1])
            if acao == "atualizar-hub" and codigo == ADIADO and por == "cron":  # o cron tenta de novo em 5 min
                reg.update(codigo=ADIADO, ok=False, adiado=True, saida=(saida or "")[-1500:])
                return reg
            if acao == "atualizar-hub" and codigo == ADIADO:  # outra atualização tem o trava: sem resultado, roda depois (adiados/)
                _gravar(pasta(base_dados) / "adiados" / f"{pid}.json", {"id": pid, "acao": acao, "alvo": alvo, "motivo": motivo, "por": por})
                reg.update(codigo=ADIADO, ok=False, adiado=True, saida=(saida or "")[-1500:])
                _registrar(reg, base_dados)
                return reg
    except ValueError as e:
        codigo, saida = 2, f"recusado: {e}"
    except Exception as e:  # noqa: BLE001 - o resultado tem que sair sempre
        codigo, saida = 1, f"{type(e).__name__}: {e}"
    if acao == "atualizar-hub":  # o SHA da HEAD testada vai no resultado; o estado guarda a reprovada e o atraso
        m = RE_SHA_ATUALIZAR.search(saida or "")
        if m:
            reg["sha"] = m.group(1)
        try:  # o estado e o incidente não podem impedir o resultado do pedido
            if not str(saida or "").startswith("recusado:"):
                _guardar_resultado_atualizar(base_dados, codigo, saida, reg.get("sha"))
            _vigiar_atraso(base_dados)
        except Exception as e:  # noqa: BLE001
            print(f"aviso: estado do atualizar-hub não gravado: {type(e).__name__}: {e}", file=sys.stderr)
    reg.update(codigo=codigo, ok=codigo == 0, duracao_s=round(time.monotonic() - inicio, 1), saida=(saida or "")[-1500:])
    _registrar(reg, base_dados)
    _gravar(pasta(base_dados) / "resultados" / f"{pid}.json", reg)
    return reg


def atualizar_cron(base_dados=None, rodar=None, sha_main=None):
    """o cron de 5 min passa pelo mesmo caminho do pedido (a mesma HEAD reprovada não roda a suíte de novo a cada 5 min;
    o estado e o incidente de atraso valem para as duas portas). Devolve (código, texto)."""
    try:
        _recusar_mesma_head(base_dados, "", (sha_main or _sha_main)())
    except ValueError as e:
        return 0, f"{agora_iso()} recusado: {e}"  # quieto: 0 não vira alarme de cron
    pid = pedir("atualizar-hub", motivo="cron: main andou", por="cron", base_dados=base_dados)
    reg = executar_pedido(pasta(base_dados) / "pedidos" / f"{pid}.json", base_dados, rodar=rodar, sha_main=sha_main)
    return (0 if reg.get("ok") or reg.get("adiado") else 1), f"{reg['em']} sha={reg.get('sha', '?')} código {reg['codigo']}\n{reg.get('saida', '')}"


def resultado(pid, base_dados=None, esperar=0):
    if not RE_PID.match(pid):
        raise ValueError(f"id inválido: {pid!r}")
    arq = pasta(base_dados) / "resultados" / f"{pid}.json"
    limite = time.monotonic() + esperar
    while True:
        if arq.exists():
            return json.loads(arq.read_text(encoding="utf-8"))
        if time.monotonic() >= limite:
            return None
        time.sleep(5)


# ---- 2. Pedido N tipo vps aprovado pelo dono ----

def hash_comandos(comandos):
    return hashlib.sha256(comandos.encode("utf-8")).hexdigest()


HOSTS_PERMITIDOS = (HOST_PADRAO, "vps-servidor_b")  # vps-servidor_a (o dedicado, desde 22/10) é apelido de HOST_PADRAO


def host_do_pedido(plano):
    """`dados.host` do plano guardado pelo bot (o claude não escreve lá) → servidor_hub (local; vps-servidor_a é apelido) ou vps-servidor_b;
    outro valor, ValueError."""
    dados = (plano or {}).get("dados")
    bruto = dados.get("host") if isinstance(dados, dict) else None
    if bruto is None or (isinstance(bruto, str) and not bruto.strip()):
        return HOST_PADRAO
    host = ALIAS_HOST.get(str(bruto).strip(), str(bruto).strip()) if isinstance(bruto, str) else None
    if host not in HOSTS_PERMITIDOS:
        raise ValueError(f"host {bruto!r} não permitido (só {', '.join(HOSTS_PERMITIDOS)})")
    return host


def comando_execucao(job):
    """O que o root roda: bash local na servidor_hub, ou o mesmo bash por ssh na máquina do Pedido."""
    base = ["bash", "-e", "-o", "pipefail", "-c", job["comandos"]]
    if job.get("host", HOST_PADRAO) == HOST_PADRAO:
        return base
    # sem PTY o bash remoto sobrevive ao ssh morto: o timeout também roda lá (grupo próprio, um pouco antes do local)
    remoto = max(job["timeout_min"] * 60 - 15, 30)
    return ["ssh", "-o", "BatchMode=yes", job["host"], shlex.join(["timeout", "-k", "5", str(remoto)] + base)]


def conferir_aprovado(arq, pedidos_bot=None, exigir_root=True):
    """Arquivo da fila root → job conferido, ou ValueError. Confere dono, hash e o pedido guardado pelo bot."""
    st = os.lstat(arq)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("não é arquivo comum")
    if exigir_root and (st.st_uid != 0 or st.st_mode & 0o022):
        raise ValueError("arquivo não é do root ou é gravável por outros")
    job = json.loads(Path(arq).read_text(encoding="utf-8"))
    n, comandos, h = job.get("n"), job.get("comandos"), str(job.get("hash") or "")
    if not isinstance(n, int) or n < 1:
        raise ValueError("sem número do pedido")
    if not isinstance(comandos, str) or not comandos.strip():
        raise ValueError("sem comandos")
    if not RE_HASH.match(h) or hash_comandos(comandos) != h:
        raise ValueError(f"hash dos comandos não confere com o que o {config.dono()} viu")
    t = job.get("timeout_min", 10)
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 30:
        raise ValueError("timeout_min fora de 1 a 30")
    pedidos = json.loads(Path(pedidos_bot or PEDIDOS_BOT).read_text(encoding="utf-8"))
    p = (pedidos.get("itens") or {}).get(str(n)) if isinstance(pedidos, dict) else None  # src/pedidos.js
    if not p or (p.get("plano") or {}).get("hash") != h:
        raise ValueError(f"pedido {n} do bot não tem esse hash (não foi o que o {config.dono()} aprovou)")
    if p.get("estado") not in ("executando", "aguardando"):
        raise ValueError(f"pedido {n} está {p.get('estado')}, não aprovado")
    host = host_do_pedido(p.get("plano"))
    return {"n": n, "comandos": comandos, "hash": h, "timeout_min": t, "proposta": job.get("proposta"), "host": host}


def tres_linhas(saida):
    linhas = [l.rstrip()[:160] for l in (saida or "").strip().splitlines() if l.strip()]
    return "\n".join(linhas[-3:]) or "(sem saída)"


def executar_aprovado(arq, base_dados=None, rodar=None, feitos=None, pedidos_bot=None, exigir_root=True,
                      marcar=None, log_dir=None):
    rodar = rodar or _rodar
    feitos = Path(feitos or FEITOS_AGENTE)
    nome = Path(arq).name
    try:
        job = conferir_aprovado(arq, pedidos_bot, exigir_root)
    except (OSError, ValueError) as e:
        Path(arq).unlink(missing_ok=True)
        reg = {"em": agora_iso(), "acao": "pedido-vps", "arquivo": nome, "codigo": 2, "ok": False, "saida": f"recusado: {e}"}
        _registrar(reg, base_dados)
        m = re.match(r"^(\d+)-", nome)
        if m:
            _gravar(feitos / f"{int(m.group(1)):05d}-executar.json",
                    {"n": int(m.group(1)), "fase": "executar", "ok": False, "erro": f"o root recusou: {e}"})
        return reg
    Path(arq).unlink(missing_ok=True)
    inicio = time.monotonic()
    codigo, saida = rodar(comando_execucao(job), job["timeout_min"] * 60,
                          dict(ENV_PADRAO, HUB_DATA=str(base_dados or data()), NIMBUS_HOME=str(home())), "/root" if Path("/root").is_dir() else "/")
    dur = round(time.monotonic() - inicio, 1)
    log_dir = Path(log_dir or LOG_ROOT)
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (log_dir / f"{job['n']:05d}-{job['hash'][:12]}.log").write_text(
            f"# pedido {job['n']} · {agora_iso()} · código {codigo} · {dur}s · {job['host']}\n# comandos:\n{job['comandos']}\n# saída:\n{saida}",
            encoding="utf-8")
    except OSError:
        pass
    ok = codigo == 0
    corpo = f"código {codigo} em {dur:.0f}s\n{tres_linhas(saida)}"  # o bot abre com "✅ Pedido N feito." / "❌ ... falhou:"
    _gravar(feitos / f"{job['n']:05d}-executar.json",
            {"n": job["n"], "fase": "executar", "ok": ok, **({"resumo": corpo} if ok else {"erro": corpo})})
    reg = {"em": agora_iso(), "acao": "pedido-vps", "pedido": job["n"], "hash": job["hash"][:12], "proposta": job["proposta"],
           "codigo": codigo, "ok": ok, "duracao_s": dur, "saida": tres_linhas(saida)}
    _registrar(reg, base_dados)
    if job.get("proposta") and RE_PID.match(str(job["proposta"])):
        (marcar or _marcar_proposta)(job["proposta"], "feita" if ok else "falhou", f"root: código {codigo} (faz {job['n']})")
    return reg


def _marcar_proposta(pid, estado, nota):
    from hub import propostas
    try:
        propostas.marcar(pid, estado, nota)
    except (OSError, ValueError):
        pass


def _limpar_temporarios(d, idade_s=600):
    """Arquivo .tmp esquecido deixaria a pasta "não vazia" e o systemd.path dispararia sem parar."""
    for arq in d.glob(".*"):
        try:
            if time.time() - arq.lstat().st_mtime > idade_s:
                arq.unlink()
        except OSError:
            pass


ESPERA_TRAVA_S = 30  # trava ocupada (saude/atualizar segurando): espera antes de sair, para o .path não girar em laço
UNIDADE_PATH = "nimbus-raiz.path"


def _esperar_trava(fd, fcntl, espera_s):
    """True se pegou o trava dentro de espera_s; tenta de 1 em 1 s."""
    fim = time.monotonic() + espera_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= fim:
                return False
            time.sleep(1)


def religar_path(rodar=None):
    """O .path nunca fica em failed: se não está active, limpa o start-limit e liga de novo. Devolve True se religou."""
    if rodar is None:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:  # só o root liga unidade do systemd
            return False
        rodar = lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        if rodar(["systemctl", "is-active", UNIDADE_PATH]).stdout.strip() == "active":
            return False
        rodar(["systemctl", "reset-failed", UNIDADE_PATH])
        rodar(["systemctl", "start", UNIDADE_PATH])
        return True
    except (OSError, subprocess.SubprocessError):  # sem systemctl (testes, Windows): nada a religar
        return False


def processar(base_dados=None, fila_root=None, trava=None, espera_s=None):
    """Uma execução por vez (o systemd.path e o cron de 10 min podem chegar juntos).

    Trava ocupada: espera até espera_s e sai sem processar (a pasta continua cheia e o path dispara de novo, já
    espaçado pela espera; o start-limit das unidades está desligado). No fim, religa o .path se tiver caído."""
    trava = trava or os.environ.get("RAIZ_TRAVA", "/var/lock/nimbus-raiz.lock")
    espera_s = ESPERA_TRAVA_S if espera_s is None else espera_s
    try:
        import fcntl
        fd = os.open(trava, os.O_RDWR | os.O_CREAT, 0o600)
        if not _esperar_trava(fd, fcntl, espera_s):
            os.close(fd)
            religar_path()
            return []
    except (ImportError, OSError):  # Windows (testes) ou sem /var/lock: segue sem trava
        fd = None
    try:
        return _processar(base_dados, fila_root)
    finally:
        if fd is not None:
            os.close(fd)
        religar_path()


COLAPSAVEIS = ("atualizar-hub", "atualizar-runner")  # repetidos: uma execução depois do pedido cobre os anteriores


def _id_acao_do_pedido(arq):
    """(id, ação) de um arquivo da pasta de pedidos, sem apagar nada; (None, None) se ilegível."""
    try:
        if arq.is_symlink() or not arq.is_file():
            return None, None
        if not arq.name.endswith(".json"):
            return None, arq.name
        p = json.loads(arq.read_text(encoding="utf-8") or "{}")
        return p.get("id") or arq.stem, p.get("acao")
    except (OSError, ValueError, AttributeError):
        return None, None


def _cobrir(arq, base_dados, reg):
    """Pedido repetido: não roda, ganha o resultado da execução que o cobre (raiz resultado <id> responde)."""
    pid, acao = _id_acao_do_pedido(arq)
    if not (pid and RE_PID.match(str(pid))):  # sem id válido não há resultado para dar: fica na fila e roda como pedido próprio
        return None
    arq.unlink(missing_ok=True)
    cob ={**reg, "id": pid, "acao": acao, "coberto_por": reg.get("id"), "motivo": f"coberto por {reg.get('id')}: {reg.get('motivo', '')}"[:500]}
    _registrar(cob, base_dados)
    _gravar(pasta(base_dados) / "resultados" / f"{pid}.json", cob)
    return cob


def _processar_aprovados(base_dados, fila_root):
    feitos = []
    fr = Path(fila_root or ROOT_FILA)
    if fr.is_dir():
        for arq in sorted(fr.iterdir()):
            if arq.name.endswith(".json") and not arq.name.startswith("."):
                feitos.append(executar_aprovado(arq, base_dados))
    return feitos


def _executar_com_colapso(arq, resto, base_dados=None):
    """Roda `arq`. Se deu certo, os repetidos colapsáveis de `resto` (mesma ação, depois dele) ganham o mesmo resultado
    e saem da fila. Se ficou adiado (outra atualização segura o trava), não cobre ninguém: eles rodam depois."""
    acao = _id_acao_do_pedido(arq)[1]
    cobertos = [a for a in resto if acao in COLAPSAVEIS and _id_acao_do_pedido(a)[1] == acao]
    reg = executar_pedido(arq, base_dados)
    feitos = [reg]
    if reg.get("ok"):
        for c in cobertos:
            cob = _cobrir(c, base_dados, reg)
            if cob:
                feitos.append(cob)
    return feitos


def _processar(base_dados=None, fila_root=None):
    feitos = []
    for d in (pasta(base_dados) / "pedidos", Path(fila_root or ROOT_FILA)):
        if d.is_dir():
            _limpar_temporarios(d)
    d = pasta(base_dados) / "pedidos"
    vistos = set()
    while d.is_dir():
        feitos += _processar_aprovados(base_dados, fila_root)  # aprovadas do dono não esperam a pasta inteira de pedidos
        fila = [a for a in sorted(d.iterdir()) if not a.name.startswith(".") and a.name not in vistos]
        if not fila:
            break
        arq = fila[0]
        vistos.add(arq.name)
        feitos += _executar_com_colapso(arq, fila[1:], base_dados)
    d = pasta(base_dados) / "adiados"  # fora de pedidos/: o systemd.path não dispara em laço; o cron de 10 min passa aqui
    vistos = set()  # um adiado que volta a adiar é regravado com o mesmo nome: não pode girar o laço
    while d.is_dir():
        fila = [a for a in sorted(d.glob("*.json")) if a.name not in vistos]
        if not fila:
            break
        vistos.add(fila[0].name)
        feitos += _executar_com_colapso(fila[0], fila[1:], base_dados)
    feitos += _processar_aprovados(base_dados, fila_root)
    return feitos


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.raiz", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("pedir", help="pede um conserto seguro (usuário claude)")
    pe.add_argument("acao", choices=sorted(ACOES))
    pe.add_argument("alvo", nargs="?")
    pe.add_argument("--motivo", default="")
    pe.add_argument("--por", default=os.environ.get("TAREFA_NOME", "claude"))
    r = sub.add_parser("resultado")
    r.add_argument("id")
    r.add_argument("--esperar", type=int, default=0, help="segundos esperando o root")
    sub.add_parser("processar", help="(root) executa as duas filas")
    sub.add_parser("atualizar-cron", help="(root, cron de 5 min) atualizar-hub com a trava da mesma HEAD reprovada")
    sub.add_parser("acoes", help="lista as ações e as rotinas")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.cmd == "pedir":
            print(pedir(args.acao, args.alvo, args.motivo, args.por))
        elif args.cmd == "resultado":
            reg = resultado(args.id, esperar=args.esperar)
            if reg is None:
                print("ainda sem resultado (o root processa na hora; o cron de 10 min é a rede)")
                return 3
            print(json.dumps(reg, ensure_ascii=False, indent=2))
            return 0 if reg.get("ok") else 1
        elif args.cmd == "atualizar-cron":
            codigo, texto = atualizar_cron()
            print(texto)
            return codigo
        elif args.cmd == "processar":
            for reg in processar():
                print(f"{reg['em']} {reg.get('acao')} {reg.get('alvo') or reg.get('pedido') or ''}: código {reg['codigo']}")
        else:
            for a, (alvo, t, desc) in ACOES.items():
                print(f"{a}{' <alvo>' if alvo else ''}: {desc} (até {t // 60} min)")
            print("rotinas:", ", ".join(sorted(rotinas())))
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
