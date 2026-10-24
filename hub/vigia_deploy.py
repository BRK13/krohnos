"""Vigia de deploy do Dokploy: deploy travado ou com erro não segura a fila. Root, cron de 5 min.

    python3 -m hub.vigia_deploy             uma rodada
    python3 -m hub.vigia_deploy --simular   mostra o que faria, sem agir

Por app, olhando só o deploy mais recente:
- "running" há mais que o limite (max(20 min, 3x a mediana dos deploys bons do app)): cancela e dispara de novo uma
  vez. Se o log do build mudou nos últimos 5 min o build está lento, não travado: não cancela e só anota,
  até o teto de 3x o limite. Com a máquina em load > 2x os núcleos, o limite dobra. Se o refeito travar também:
  cancela, não refaz e registra precisa_fulano no diário (o orquestrador vê).
- "error": dispara de novo uma vez (o deploy pega o último commit da branch). Se o refeito falhar: abre issue no repo
  com o fim do log.
O cancelamento da API (`application.cancelDeployment`) só existe no Dokploy cloud e o `killBuild` mata todos os
builds da máquina; aqui o sinal vai só para os processos de build daquele app, dentro do contêiner do Dokploy.
Estado em $HUB_DATA/vps/vigia-deploy.json; cada ação em vigia-deploy.jsonl e no diário de consertos.
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from hub import consertos, data, github_api, google, raiz, repos, sites

UTC = dt.timezone.utc
LIMITE_MIN, FATOR = 20, 3
LIMITES = {"vigia-teste": 3}  # prefixo do appName -> minutos (o Dokploy põe sufixo); o app de teste do
LOG_VIVO = dt.timedelta(minutes=5)  # log escrito há menos que isso: o build anda
TETO = 3  # x o limite: nem log andando segura o deploy além disso
JANELA_REFEITO = dt.timedelta(hours=3)  # deploy depois disso não conta mais como o "refeito" da nossa ação


def _t(iso):
    return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def _sha(d):
    m = re.search(r"\b([0-9a-f]{40})\b", d.get("description") or "")
    return m.group(1) if m else None


def _titulo(d):
    return ((d.get("title") or "").splitlines() or [""])[0][:80]


def limite(app_name, deploys):
    """Minutos que um deploy deste app pode ficar em running."""
    for prefixo, minutos in LIMITES.items():
        if app_name.startswith(prefixo):
            return minutos
    bons = [(_t(d["finishedAt"]) - _t(d["startedAt"])).total_seconds() / 60
            for d in deploys if d.get("status") == "done" and d.get("finishedAt") and d.get("startedAt")]
    return max(LIMITE_MIN, round(FATOR * statistics.median(bons))) if bons else LIMITE_MIN


def com_carga(lim, carga, nucleos):
    """Limite dobrado com load1 acima de 2x os núcleos: build lento por máquina cheia não é build travado."""
    return lim * 2 if carga is not None and nucleos and carga > 2 * nucleos else lim


def decidir(deploys, estado, agora, lim, log_em=None):
    """Puro. deploys do mais novo para o mais velho; log_em = última escrita no log do mais novo (ou None).
    Devolve (acao|None, novo_estado). Ações: lento (só anota), cancelar_refazer, cancelar_desistir, refazer, abrir_issue."""
    if not deploys:
        return None, estado
    u = deploys[0]
    criado, t = _t(u["createdAt"]), estado.get("tentativa")
    if t and _t(t["em"]) >= criado:  # este deploy já foi tratado (ou é anterior à nossa ação)
        return None, estado
    # Depois da nossa ação, mesmo commit (ou deploy manual, sem hash) e dentro da janela: é o nosso refeito.
    refeito = bool(t and not t.get("fim") and criado - _t(t["em"]) < JANELA_REFEITO
                   and (_sha(u) is None or _sha(u) == t["chave"]))
    chave, em = _sha(u) or u["deploymentId"], agora.isoformat()
    rodando = agora - _t(u.get("startedAt") or u["createdAt"])
    if u.get("status") == "running" and rodando > dt.timedelta(minutes=lim):
        if log_em and agora - log_em < LOG_VIVO and rodando <= dt.timedelta(minutes=TETO * lim):
            return "lento", estado
        if refeito:
            return "cancelar_desistir", {"tentativa": {**t, "em": em, "fim": "desistiu"}}
        return "cancelar_refazer", {"tentativa": {"chave": chave, "em": em, "por": "travou"}}
    if u.get("status") == "error":
        if refeito:
            return "abrir_issue", {"tentativa": {**t, "em": em, "fim": "issue"}}
        return "refazer", {"tentativa": {"chave": chave, "em": em, "por": "erro"}}
    return None, estado


def sitemap_pendente(deploys, ultimo):
    """Puro. deploys do mais novo para o mais velho. deploymentId do mais novo se terminou bem e ainda não foi enviado."""
    u = deploys[0] if deploys else None
    return u["deploymentId"] if u and u.get("status") == "done" and u["deploymentId"] != ultimo else None


def limpar_log(linhas):
    """Fim do log sem segredo: tira linhas de build-arg/env e mascara token comprido."""
    fora = re.compile(r"--build-arg|--env |SecretsUsedInArgOrEnv|^\s*(ARG|ENV) ", re.I)
    mascaras = [(r"(?i)\b(authorization|bearer|basic)\b.*$", r"\1 ***"), (r"://[^/\s:@]*:[^@\s]+@", "://***@"), (r"\b([A-Z][A-Z0-9_]{2,})=\S+", r"\1=***"),
                (r"(?i)(\w*(?:pass|senha|secret|token|key|auth)\w*[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1***"),
                (r"[A-Za-z0-9_\-+/=.]{32,}", "***")]
    saida = []
    for l in linhas:
        if fora.search(l):
            continue
        for padrao, troca in mascaras:
            l = re.sub(padrao, troca, l)
        saida.append(l)
    return saida


# ---- efeitos ----

def _container_dokploy():
    r = subprocess.run(["docker", "ps", "-qf", "name=^dokploy\\."], capture_output=True, text=True, timeout=30)
    return (r.stdout.split() or [None])[0]


def log_mexeu_em(d):
    """Última escrita no log do deploy: no host (a pasta de logs do Dokploy é montada) ou, sem ela, no contêiner."""
    caminho = d.get("logPath")
    if not caminho:
        return None
    try:
        return dt.datetime.fromtimestamp(os.stat(caminho).st_mtime, UTC)
    except OSError:
        pass
    try:
        ctr = _container_dokploy()
        r = subprocess.run(["docker", "exec", ctr, "stat", "-c", "%Y", caminho], capture_output=True, text=True,
                           timeout=30) if ctr else None
        return dt.datetime.fromtimestamp(int(r.stdout.strip()), UTC) if r and r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def cancelar(app_name, deployment_id, application_id):
    """SIGINT nos processos de build só deste app (clone, nixpacks, docker build); KILL se ainda estiver rodando."""
    ctr = _container_dokploy()
    if not ctr:
        raise RuntimeError("contêiner do Dokploy não encontrado")
    padrao = f"-t {re.escape(app_name)}( |$)|/applications/{re.escape(app_name)}/code"
    for sinal, espera in (("-INT", 20), ("-KILL", 10)):
        subprocess.run(["docker", "exec", ctr, "pkill", sinal, "-f", "--", padrao], capture_output=True, timeout=30)
        time.sleep(espera)
        if not any(d["deploymentId"] == deployment_id and d.get("status") == "running"
                   for d in _dk(raiz._dokploy, f"deployment.all?applicationId={application_id}", chave=_chave())):
            return
    raise RuntimeError(f"{app_name}: deploy {deployment_id} continua em running depois do KILL")


def _chave():
    return raiz.ler_env(raiz.ENV_AGENTE).get("DOKPLOY_API_KEY")


class ErroHttp(RuntimeError):
    """HTTP que falhou, com quem (Dokploy ou GitHub) e a URL (o 401 de 22/10 não dizia de qual dos dois era)."""


def _http(origem, e):
    url = getattr(e, "url", None) or getattr(e, "filename", None) or "?"
    return ErroHttp(f"{origem} {url}: HTTP {e.code} {e.reason}"
                    + (" (DOKPLOY_API_KEY de /etc/agente-fsite06.env não vale para este Dokploy)"
                       if origem == "Dokploy" and e.code == 401 else ""))


def _dk(fn, *args, **kw):
    """Chamada ao Dokploy (raiz._dokploy, raiz.apps_dokploy): HTTPError vira ErroHttp com a URL."""
    try:
        return fn(*args, **kw)
    except urllib.error.HTTPError as e:
        raise _http("Dokploy", e) from None


def refazer(application_id):
    _dk(raiz._dokploy, "application.deploy", {"applicationId": application_id}, _chave())


def abrir_issue(app, d):
    owner, repo = app.get("owner"), app.get("repository")
    if not (owner and repo):
        return None
    try:
        log = limpar_log(Path(d["logPath"]).read_text(encoding="utf-8", errors="replace").splitlines()[-60:])
    except (OSError, KeyError):
        log = ["(log ilegível)"]
    corpo = (f"O deploy do app `{app['appName']}` no Dokploy falhou duas vezes seguidas "
             f"(o vigia de deploy já tinha disparado de novo uma vez).\n\n"
             f"- branch: `{app.get('branch')}`\n- deploy: `{d['deploymentId']}` ({_titulo(d)})\n"
             f"- log completo na VPS: `{d.get('logPath')}`\n\nFim do log:\n\n```\n" + "\n".join(log) + "\n```\n\n"
             "Aberta por `hub/vigia_deploy.py` (KRN-105).")
    token = repos.token_github()
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}/issues", method="POST",
                                 data=json.dumps({"title": f"Deploy do {app['name']} ({app['appName']}) falhou duas vezes",
                                                  "body": corpo}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"})
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status = r.status
            return json.loads(r.read())["html_url"]
    except urllib.error.HTTPError as e:
        status = e.code
        raise _http("GitHub", e) from None
    finally:
        github_api.registrar_http("vigia_deploy", token, status)


def rodar(simular=False, base_dados=None, agora=None):
    agora = agora or dt.datetime.now(UTC)
    pasta = Path(base_dados or data()) / "vps"
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / "vigia-deploy.json"
    estado = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}
    chave = _chave()
    if not chave:
        print("sem DOKPLOY_API_KEY", file=sys.stderr)
        return 1
    feitos, ultimos = [], {}
    carga, nucleos = os.getloadavg()[0], os.cpu_count()
    try:
        por_app = {s["ids"]["dokploy_app"]: s for s in sites.ativos() if s["ids"].get("dokploy_app") and s["ids"].get("gsc")}
    except Exception as e:  # cadastro quebrado não pode parar o resto do vigia: segue sem enviar sitemap
        por_app = {}
        feitos.append({"em": agora.isoformat(), "acao": "sitemap", "erro": f"sites: {str(e)[:300]}"})
    enviados = estado.setdefault("_sitemap", {})  # separado: estado[nome] é sobrescrito pelo decidir
    try:
        apps = _dk(raiz.apps_dokploy, chave)
    except ErroHttp as e:  # sem a lista de apps o vigia não vê nada: falha com a URL na última linha (rotina_falhas)
        print(f"vigia-deploy: {e}", file=sys.stderr)
        return 1
    for a in apps:
        try:
            deploys = sorted(_dk(raiz._dokploy, f"deployment.all?applicationId={a['applicationId']}", chave=chave) or [],
                             key=lambda d: d["createdAt"], reverse=True)
            nome = a.get("appName") or a["name"]
            lim = com_carga(limite(nome, deploys), carga, nucleos)
            if (bom := next((d for d in deploys if d.get("status") == "done"), None)) and _sha(bom):  # o recibo confere o "no ar"
                ultimos[a["applicationId"]] = {"sha": _sha(bom), "em": bom.get("finishedAt") or bom.get("createdAt")}
            if (s := por_app.get(a["applicationId"])) and (pend := sitemap_pendente(deploys, enviados.get(nome))):
                ev = {"em": agora.isoformat(), "app": nome, "acao": "sitemap", "deploy": pend, "site": s["id"]}
                if simular:
                    print(json.dumps(ev, ensure_ascii=False))
                else:
                    try:
                        google.enviar_sitemap(s["ids"]["gsc"], f"https://{s['dominio']}/sitemap.xml")
                        enviados[nome] = pend
                    except Exception as e:  # tenta de novo na próxima rodada
                        ev["erro"] = str(e)[:300]
                    feitos.append(ev)
            log_em = log_mexeu_em(deploys[0]) if deploys and deploys[0].get("status") == "running" else None
            acao, novo = decidir(deploys, estado.get(nome, {}), agora, lim, log_em)
            if not acao:
                continue
            d = deploys[0]
            ev = {"em": agora.isoformat(), "app": nome, "acao": acao, "deploy": d["deploymentId"],
                  "titulo": _titulo(d), "limite_min": lim, "load1": round(carga, 1)}
            if log_em:
                ev["log_em"] = log_em.isoformat()
            if simular:
                print(json.dumps(ev, ensure_ascii=False))
                continue
            if acao == "lento":  # só anota no jsonl; não é conserto, não vai ao diário
                feitos.append(ev)
                continue
            app = {**a, **(_dk(raiz._dokploy, f"application.one?applicationId={a['applicationId']}", chave=chave) or {})}
            if acao.startswith("cancelar"):
                cancelar(nome, d["deploymentId"], a["applicationId"])
            if acao in ("cancelar_refazer", "refazer"):
                refazer(a["applicationId"])
            if acao == "abrir_issue":
                ev["issue"] = abrir_issue(app, d)
            estado[nome] = novo
            feitos.append(ev)
            textos = {
                "cancelar_refazer": (f"deploy do {nome} travado há mais de {lim} min", "build sem progresso",
                                     "vigia cancelou e disparou de novo uma vez", "tentando"),
                "cancelar_desistir": (f"deploy do {nome} travou de novo depois de refeito", "build trava sempre",
                                      "vigia cancelou e parou de refazer: precisa olhar o build", "precisa_fulano"),
                "refazer": (f"deploy do {nome} falhou", "erro no build ou no deploy",
                            "vigia disparou de novo uma vez", "tentando"),
                "abrir_issue": (f"deploy do {nome} falhou de novo depois de refeito", "erro repetido no build",
                                f"vigia abriu issue com o log: {ev.get('issue')}", "precisa_fulano"),
            }[acao]
            consertos.registrar(*textos, prova=f"deploy {d['deploymentId']}", por="vigia-deploy", base_dados=base_dados)
        except Exception as e:  # um app com problema não para o vigia dos outros
            feitos.append({"em": agora.isoformat(), "app": a.get("appName"), "erro": str(e)[:300]})
    if not simular:
        arq.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp = pasta / "deploys.json.tmp"
        tmp.write_text(json.dumps(ultimos), encoding="utf-8")
        os.chmod(tmp, 0o644)  # o recibo roda como claude, o vigia como root
        os.replace(tmp, pasta / "deploys.json")
        with open(pasta / "vigia-deploy.jsonl", "a", encoding="utf-8") as fh:
            for ev in feitos:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    for ev in feitos:
        print(json.dumps(ev, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simular", action="store_true")
    return rodar(simular=ap.parse_args(argv).simular)


if __name__ == "__main__":
    sys.exit(main())
