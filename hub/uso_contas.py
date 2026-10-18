"""Uso das contas Claude Max da VPS (conta.CONTAS, só as com login): percentual da cota (endpoint de uso da CLI) e
resumo do uso.jsonl.

    python3 -m hub.uso_contas     # grava $HUB_DATA/claude/uso-contas.json (cron de 10 em 10 min, como root)

O token OAuth só é lido para a requisição: nunca é gravado, logado nem entra em exceção. Token vencido não é renovado
(renovar giraria a credencial da CLI e podia deslogar a conta): cai em `estimativa`, sem percentual.
"""
import collections
import http.client
import json
import os
import tempfile
import urllib.request
from pathlib import Path

from hub import conta, home_agentes

URL = "https://api.anthropic.com/api/oauth/usage"
JANELAS = (("5h", 5), ("24h", 24), ("7d", 168))
VAZIA = {"cinco_horas": {"pct": None, "reset": None}, "semana": {"pct": None, "reset": None}, "fonte": "estimativa"}


def _dir(n, home):
    return Path(home) / (".claude" if n == "1" else f".claude-{n}")


def _token(caminho, agora):
    try:
        o = json.loads(Path(caminho).read_text(encoding="utf-8"))["claudeAiOauth"]
        return o["accessToken"] if o["expiresAt"] / 1000 > agora.timestamp() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _janela(j):
    j = j if isinstance(j, dict) else {}
    pct = j.get("utilization", j.get("percent"))
    return {"pct": float(pct) if isinstance(pct, (int, float)) else None, "reset": j.get("resets_at")}


def _limite(j, kind):
    """Linha de limits[] da CLI (kind session | weekly_all), no formato de janela; {} se não houver."""
    return next((x for x in j.get("limits") or [] if isinstance(x, dict) and x.get("kind") == kind), {})


def _consultar(token):
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                                               "User-Agent": "claude-code/2.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _cota(caminho, agora):
    """Cota de uma credencial: oauth com percentuais, ou VAZIA (estimativa) se faltar/vencer o token ou a consulta falhar."""
    token = _token(caminho, agora)
    if not token:
        return json.loads(json.dumps(VAZIA))
    try:
        j = _consultar(token)
    except (OSError, ValueError, http.client.HTTPException):  # HTTPError e URLError são OSError; a mensagem pode ecoar o pedido, então não vai adiante
        return json.loads(json.dumps(VAZIA))
    if isinstance(j, dict):
        # seven_day/five_hour vêm null em algumas contas; aí vale limits[] (weekly_all = semana inteira da conta)
        cinco = _janela(j.get("five_hour") or _limite(j, "session"))
        sem = _janela(j.get("seven_day") or _limite(j, "weekly_all"))
        if cinco["pct"] is not None or sem["pct"] is not None:
            return {"cinco_horas": cinco, "semana": sem, "fonte": "oauth"}
    return json.loads(json.dumps(VAZIA))


def coletar(home=None, agora=None, orch=None):
    # root no cron tem HOME=/root: as credenciais são do usuário claude
    home, agora = home or os.environ.get("USO_CONTAS_HOME") or home_agentes(), agora or conta.agora()
    orch = orch or os.environ.get("USO_ORQ_CREDENCIAIS", "/home/usuario/.claude/.credentials.json")
    # o orquestrador fica fora de `contas`: esteira/agenda despacham trabalho a partir dela
    return {"em": conta.iso(agora),
            "contas": {n: _cota(_dir(n, home) / ".credentials.json", agora) for n in conta.com_login(home)},
            "orquestrador": _cota(orch, agora)}


def _modelo(m):
    m = str(m or "").lower()
    return "opus" if "opus" in m else "sonnet" if "sonnet" in m else m or "—"


def resumo(usos, agora):
    saida = {n: {"janelas": {k: {"execucoes": 0, "minutos": 0.0, "custo": 0.0, "tokens": 0} for k, _ in JANELAS},
                 "tarefas": [], "modelo": "—", "falhas": 0, "limites": 0} for n in conta.contas_config()}
    tarefas = {n: collections.defaultdict(lambda: {"execucoes": 0, "minutos": 0.0, "custo": 0.0}) for n in conta.contas_config()}
    modelos = {n: collections.Counter() for n in conta.contas_config()}
    for u in usos:
        n = str(u.get("conta") or "1")
        try:
            idade = (agora - conta.de_iso(u["inicio"])).total_seconds() / 3600
        except (KeyError, ValueError, TypeError):
            continue
        if n not in saida or idade < 0 or idade > 168:
            continue
        s, t = saida[n], u.get("tokens") or {}
        for k, h in JANELAS:
            if idade <= h:
                j = s["janelas"][k]
                j["execucoes"] += 1
                j["minutos"] += (u.get("duracao_s") or 0) / 60
                j["custo"] += u.get("custo_usd") or 0
                j["tokens"] += sum(v for v in t.values() if isinstance(v, (int, float)))
        x = tarefas[n][str(u.get("nome") or "—")]
        x["execucoes"] += 1
        x["minutos"] += (u.get("duracao_s") or 0) / 60
        x["custo"] += u.get("custo_usd") or 0
        modelos[n][_modelo(u.get("modelo"))] += 1
        s["falhas"] += 0 if u.get("ok") else 1
        s["limites"] += 1 if u.get("limite") else 0
    for n in conta.contas_config():
        saida[n]["tarefas"] = sorted(({"nome": k, **v} for k, v in tarefas[n].items()), key=lambda x: -x["custo"])[:8]
        saida[n]["modelo"] = modelos[n].most_common(1)[0][0] if modelos[n] else "—"
    return saida


def horas(usos, agora, n=48):
    """Minutos de execução por hora cheia nas últimas n horas (a última barra é a hora corrente), por conta."""
    fim = agora.replace(minute=0, second=0, microsecond=0)
    saida = {c: [0.0] * n for c in conta.contas_config()}
    for u in usos:
        try:
            i = n - 1 - int((fim - conta.de_iso(u["inicio"]).replace(minute=0, second=0, microsecond=0)).total_seconds() // 3600)
        except (KeyError, ValueError, TypeError):
            continue
        c = str(u.get("conta") or "1")
        if c in saida and 0 <= i < n:
            saida[c][i] += (u.get("duracao_s") or 0) / 60
    return saida


def main():
    r = coletar()
    d = conta.pasta()
    d.mkdir(parents=True, exist_ok=True)
    # roda como root numa pasta do usuário claude: nome aleatório com O_EXCL e chmod pelo fd, nunca seguir symlink
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".uso-contas.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)  # rotina.sh roda com umask 027; o painel (usuário claude) precisa ler
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        os.replace(tmp, d / "uso-contas.json")
    except BaseException:
        os.unlink(tmp)
        raise
    return r


if __name__ == "__main__":
    r = main()
    for n, c in {**r["contas"], "orch": r["orquestrador"]}.items():
        print(n, c["fonte"], c["cinco_horas"]["pct"], c["semana"]["pct"])
