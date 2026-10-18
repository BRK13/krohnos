#!/bin/bash
# Roda uma rotina do hub com trava, ambiente da VPS e aviso de falha no WhatsApp.
#   deploy/rotina.sh <nome> <comando> [argumentos...]      (no cron, como root)
# Desligada na tela Rotinas: não roda (FORCAR=1 ignora). Falha sempre avisa (com o fim da saída); sucesso não avisa: quem tem algo para o Fulano ler avisa sozinho.
set -uo pipefail
nome="${1:?uso: rotina.sh <nome> <comando...>}"
shift
hub="$(cd "$(dirname "$0")/.." && pwd)"
# KRONOS_LOCK_DIR: no contêiner (uid 1000) /var/lock não é gravável; na VPS o padrão fica /var/lock
lock="${KRONOS_LOCK_DIR:-/var/lock}"
mkdir -p "$lock" 2>/dev/null || true
exec 9>"$lock/nimbus-rotina-$nome.lock"
# Já rodando: exit 0 (o cron e o consertador não tratam como falha). Com FORCAR=1 ("rodar agora" do painel) sai 79 (código reservado; 75 pode ser de uma rotina de verdade), para o
# painel mostrar "pulada" em vez de ok.
flock -n 9 || { echo "$(date -u +%FT%TZ) $nome: já está rodando"; [ "${FORCAR:-}" = "1" ] && exit 79; exit 0; }
umask 027
export HUB_DATA="${HUB_DATA:-/var/lib/nimbus/reports}"
export NIMBUS_HOME="${NIMBUS_HOME:-/etc/nimbus/so}"
cd "$hub"
# Desligada na tela Rotinas (rotinas.desligadas na sobreposição do painel): sai sem rodar. FORCAR=1 ("rodar agora" do painel)
# ignora. Sem python, sem a chave ou erro na consulta = roda como sempre (a VPS não para por causa da consulta).
if [ "${FORCAR:-}" != "1" ] && command -v python3 >/dev/null 2>&1 && python3 -m hub.rotinas desligada "$nome" >/dev/null 2>&1; then
  echo "$(date -u +%FT%TZ) $nome: desligada no painel"
  exit 0
fi
unset FORCAR  # a rotina em si não precisa saber (e não passa para os filhos dela)
saida="$(mktemp)"
trap 'rm -f "$saida"' EXIT
echo "$(date -u +%FT%TZ) $nome: início ($*)"
"$@" 2>&1 | tee "$saida"
codigo=${PIPESTATUS[0]}
echo "$(date -u +%FT%TZ) $nome: fim, código $codigo"
if [ "$codigo" -ne 0 ]; then
  # Sem WhatsApp (regra do Fulano: sem spam): a falha entra no resumo das 8h (hub/resumo.py rotinas_falhas).
  mkdir -p "$HUB_DATA/rotinas"
  python3 -c 'import json,sys,datetime as d; print(json.dumps({"em": d.datetime.now(d.timezone.utc).isoformat(), "nome": sys.argv[1], "codigo": int(sys.argv[2]), "fim": open(sys.argv[3], encoding="utf-8", errors="replace").read()[-600:]}, ensure_ascii=False))'     "$nome" "$codigo" "$saida" >> "$HUB_DATA/rotinas/falhas.jsonl" || true
fi
exit "$codigo"
