"""Estado do orquestrador e do revisor Codex para o painel. Roda como orch, no fim da varredura e de cada
revisão (o painel roda como claude e não lê /home/usuario); grava só resumo, sem segredo:
    python3 -m hub.orq_estado
"""
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

from hub import data
from hub.cockpit import _sem_segredo

HOME = Path(os.environ.get("ORQ_HOME", "/home/usuario"))
CAB = re.compile(r"^=== (\S+) motivo: (.*)$")
FIM = re.compile(r"^=== fim (\S+) código (\d+)")
CHECAGEM = re.compile(r"^(\d{4}-\d\d-\d\dT\S+) (.+)$")


def varredura(textos):
    """Logs da varredura (mais antigo primeiro) → rodando agora, última varredura com Claude e última checagem."""
    ultima, aberta, checada = None, None, None
    for linha in "\n".join(textos).splitlines():
        if m := CAB.match(linha):
            aberta = {"inicio": m[1], "motivo": _sem_segredo(m[2]), "linhas": []}
        elif (m := FIM.match(linha)) and aberta:
            ultima = {**aberta, "fim": m[1], "codigo": int(m[2])}
            checada, aberta = m[1], None
        elif aberta:
            aberta["linhas"].append(linha)
        elif m := CHECAGEM.match(linha):  # "nada pendente", "pulada: ...", "anterior ainda rodando"
            checada = m[1]
    if ultima:
        ultima["resumo"] = "\n".join(_sem_segredo(x) for x in ultima.pop("linhas")).strip()[-3000:]
    return {"rodando": {"desde": aberta["inicio"], "motivo": aberta["motivo"]} if aberta else None,
            "ultima": ultima, "checada_em": checada}


def codex(linhas, hoje, login_ok, config):
    """revisoes.jsonl (uma linha por comentário publicado) → contagem de hoje e a última."""
    rs = []
    for x in linhas:
        try:
            rs.append(json.loads(x))
        except ValueError:
            continue
    de_hoje = [r for r in rs if str(r.get("em", "")).startswith(hoje)]
    sem = sum(1 for r in de_hoje if not r.get("achados"))
    return {"login_ok": login_ok, "modelo": config.get("model"), "raciocinio": config.get("model_reasoning_effort"),
            "hoje": {"revisados": len(de_hoje), "com_achado": len(de_hoje) - sem, "sem_achados": sem},
            "ultimo": rs[-1] if rs else None}


def limite_codex(sessoes):
    """Limite semanal do Codex do último evento rate_limits nas sessões dele (~/.codex/sessions/**.jsonl):
    {"pct": 8.0, "reset": ISO} ou None."""
    for arq in sorted(sessoes, key=lambda f: f.stat().st_mtime, reverse=True)[:5]:
        for linha in reversed(_ler(arq).splitlines()):
            if '"rate_limits"' not in linha:
                continue
            try:
                rl = json.loads(linha).get("payload", {}).get("rate_limits") or json.loads(linha).get("rate_limits")
            except ValueError:
                continue
            janelas = [(rl or {}).get(k) or {} for k in ("primary", "secondary")]
            p = next((j for j in janelas if j.get("window_minutes") == 10080), None)
            # Eventos antigos só tinham primary (a janela semanal).
            if p is None:
                p = next((j for j in janelas if j and j.get("window_minutes") is None), {})
            if p.get("used_percent") is not None:
                reset = p.get("resets_at")
                return {"pct": float(p["used_percent"]),
                        "reset": dt.datetime.fromtimestamp(reset, dt.timezone.utc).isoformat() if reset else None}
    return None


def _ler(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(agora=None):
    agora = agora or dt.datetime.now(dt.timezone.utc)
    logs = sorted((HOME / "varredura").glob("????-??-??.log"))[-2:]  # ontem também: logo depois da meia-noite
    try:
        config = tomllib.loads(_ler(HOME / ".codex" / "config.toml"))
    except tomllib.TOMLDecodeError:
        config = {}
    try:
        login = subprocess.run(["codex", "login", "status"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        login = False
    estado = {"em": agora.isoformat(timespec="seconds"), "varredura": varredura([_ler(p) for p in logs]),
              "codex": {**codex(_ler(HOME / "codex" / "revisoes.jsonl").splitlines(), agora.date().isoformat(), login, config),
                        "semana": limite_codex(list((HOME / ".codex" / "sessions").rglob("*.jsonl")))}}
    destino = Path(data()) / "orch" / "estado.json"
    # tmp próprio: varredura e revisor gravam com travas diferentes, podem coincidir
    with tempfile.NamedTemporaryFile("w", dir=destino.parent, suffix=".tmp", delete=False, encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)
    os.chmod(f.name, 0o644)
    os.replace(f.name, destino)


if __name__ == "__main__":
    main()
