#!/bin/bash
# Entrada do contêiner Krohnos: um serviço por contêiner (painel | agenda | webhook | mcp).
#   deploy/entrada.sh <serviço>
set -euo pipefail
cd "$(dirname "$0")/.."
porta() { python3 -c "from hub import config; print(config.get('$1'))"; }  # falha alto: use `v=$(porta x)` e depois export
case "${1:-}" in
  painel)
    if [ -z "${PAINEL_PORTA:-}" ]; then PAINEL_PORTA=$(porta painel.porta); fi
    # login por usuário e senha (hub/usuarios.py); uma senha antiga em painel.env vira o 1º dono na subida do painel
    export PAINEL_HOST="${PAINEL_HOST:-0.0.0.0}" PAINEL_PORTA
    exec python3 -m hub.painel ;;
  agenda)
    # Um laço por minuto: tick da fila + rotinas devidas (hub.rotinas). Falha de um passo não derruba o laço.
    while true; do
      inicio=$(date -u +%s)
      minuto=$(date -u -d "@$inicio" +%Y-%m-%dT%H:%M)  # capturado antes do tick (que pode demorar): não pula o minuto das rotinas
      python3 deploy/agenda.py tick || echo "$(date -u +%FT%TZ) agenda: tick falhou" >&2
      python3 -m hub.rotinas --agora "$minuto" || echo "$(date -u +%FT%TZ) agenda: rotinas falhou" >&2
      resto=$((inicio + 60 - $(date -u +%s)))  # dorme até início+60s, não 60s depois do fim
      if [ "$resto" -gt 0 ]; then sleep "$resto"; else echo "$(date -u +%FT%TZ) agenda: ciclo de $minuto passou de 60s; minutos pulados não rodam" >&2; fi
    done ;;
  webhook)
    export WEBHOOK_HOST="${WEBHOOK_HOST:-0.0.0.0}"
    exec python3 deploy/linear-webhook.py ;;
  mcp)
    exec python3 -m hub.mcp_proxy --host 0.0.0.0 --porta "${MCP_PROXY_PORTA:-4181}" ;;
  *)
    echo "uso: entrada.sh painel|agenda|webhook|mcp" >&2
    exit 2 ;;
esac
