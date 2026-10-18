#!/usr/bin/env python3
"""Receptor do webhook do Linear (app Claude): https://painel.clientec-com.example/hooks/linear → Traefik → aqui.

Só acorda a fila: responde 200 na hora; evento de sessão de agente assinado (Linear-Signature, HMAC-SHA256 com
LINEAR_WEBHOOK_SECRET ou LINEAR_APP2_WEBHOOK_SECRET, os dois apps) ganha um "Recebi" no painel dentro dos 10 s que o Linear
exige; evento assinado também anota a issue em hub/linear_uso.py (o tick só consulta essas issues enquanto o webhook estiver
vivo); qualquer evento dispara um `agenda.py tick` (as travas da agenda seguram concorrência). O trabalho continua todo no
tick (hub/linear.py), então um evento perdido só atrasa até a varredura completa de 15 min. Roda como o usuário claude (deploy/nimbus-linear-webhook.service).
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB))
from hub import linear, linear_uso  # noqa: E402

_TRAVA, _ULTIMO_TICK, _ULTIMO_GRAFO = threading.Lock(), 0.0, 0.0
ENDERECO = (os.environ.get("WEBHOOK_HOST", "172.19.0.1"), int(os.environ.get("WEBHOOK_PORTA", "8790")))
MAX = 2_000_000


SEGREDOS = ("LINEAR_WEBHOOK_SECRET", "LINEAR_APP2_WEBHOOK_SECRET")  # app Claude e app claude2


def assinatura_ok(corpo, assinatura):
    """Vale a assinatura de qualquer um dos dois apps (HMAC-SHA256 do corpo com o segredo do webhook do app)."""
    if not assinatura:
        return False
    for nome in SEGREDOS:
        segredo = linear._segredo(nome)
        if segredo and hmac.compare_digest(hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest(), assinatura):
            return True
    return False


def atualizar_grafo(evento, assinado):
    """Issue ou comentário mexido: o segundo cérebro (hub/grafo.py) relê as issues alteradas. No máximo 1 por minuto;
    o que cair na janela entra na rodada seguinte (o corte é a data de alteração, nada se perde) ou no cron de 10 min."""
    global _ULTIMO_GRAFO
    if not assinado or evento.get("type") not in ("Issue", "Comment"):
        return
    with _TRAVA:
        if time.monotonic() - _ULTIMO_GRAFO < 60:
            return
        _ULTIMO_GRAFO = time.monotonic()
    subprocess.Popen([sys.executable, "-m", "hub.grafo", "atualizar", "linear"], cwd=str(HUB), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def portao_done(evento, assinado):
    """issue que acabou de virar Done (conta, UI, qualquer um) passa pelo portão; sem recibo, volta para In Progress.
    Fora do tick de propósito: o webhook é a única hora em que a mudança é conhecida na hora."""
    d = evento.get("data") or {}
    if not assinado or evento.get("type") != "Issue" or evento.get("action") != "update" or not d.get("identifier"):
        return
    if (d.get("state") or {}).get("type") != "completed" or "stateId" not in (evento.get("updatedFrom") or {}):
        return
    subprocess.Popen([sys.executable, "-m", "hub.portao_done", "conferir", d["identifier"], "--desfazer"], cwd=str(HUB),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def tratar(evento, assinado):
    if assinado:  # o tick só consulta as issues dos eventos enquanto o webhook estiver vivo
        try:
            linear_uso.hook_registrar(evento)
        except Exception as e:  # noqa: BLE001
            print("registro do evento falhou:", e, flush=True)
    atualizar_grafo(evento, assinado)
    portao_done(evento, assinado)
    if assinado and evento.get("type") == "AgentSessionEvent" and evento.get("action") in ("created", "prompted"):
        sessao = (evento.get("agentSession") or {}).get("id")
        if sessao:
            try:
                linear.atividade(sessao, "thought", "Recebi. Já vou começar.", efemera=True)
            except Exception as e:  # noqa: BLE001
                print("ack falhou:", e, flush=True)
    global _ULTIMO_TICK
    with _TRAVA:  # avalanche de eventos (o Claude comentando, várias issues) = no máximo um tick a cada 10 s
        if time.monotonic() - _ULTIMO_TICK < 10:
            return
        _ULTIMO_TICK = time.monotonic()
    subprocess.Popen([sys.executable, str(HUB / "deploy" / "agenda.py"), "tick"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


class Receptor(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if not self.path.startswith("/hooks/linear") or n > MAX:
            self.send_response(404)
            self.end_headers()
            return
        corpo = self.rfile.read(n)
        self.send_response(200)
        self.end_headers()
        try:
            evento = json.loads(corpo)
        except ValueError:
            return
        assinado = assinatura_ok(corpo, self.headers.get("Linear-Signature"))
        print(evento.get("type"), evento.get("action"), "assinado" if assinado else "SEM assinatura válida", flush=True)
        threading.Thread(target=tratar, args=(evento, assinado), daemon=True).start()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.environ.setdefault("HUB_DATA", "/var/lib/nimbus/reports")
    ThreadingHTTPServer(ENDERECO, Receptor).serve_forever()
