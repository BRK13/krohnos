#!/bin/bash
# Teste de instalação cronometrado do Pereiraos: do repositório limpo ao painel no ar com 1 projeto cadastrado e verificado.
#
#   scripts/teste-instalacao.sh --local [--sem-cache] [--limite 900]   usa o docker desta máquina (projeto compose kronos-teste-<pid>,
#                                                         portas altas livres, imagem própria; limpa só o que criou)
#   scripts/teste-instalacao.sh --dind  [--limite 900]   sobe `docker:dind` --privileged como "VPS" limpa e roda tudo dentro dela
#   (sem argumento = --dind, o padrão do plano; --local é o que cabe numa máquina com outros contêineres)
#
# Etapas cronometradas: build da imagem, kronos init --sim, docker compose up, espera do /saude (+ senha do painel),
# cadastro do projeto (POST /comecar, a mesma rota do assistente), rotina na agenda, "verificar" o projeto.
# Imprime o tempo de cada etapa e o total; sai com 1 se alguma etapa falha ou se o total passa do limite (900 s).
#
# Sobre o "verificar": o projeto cadastrado usa o domínio fictício alvo.kronos-teste.test (sem DNS nem TLS), e o
# PageSpeed (que só enxerga URL pública) não cabe aqui. Por isso sobe-se um contêiner "alvo" (uma página HTML estática na
# mesma rede do compose) e o verificador (verificar_site.py, o mesmo do painel) roda DENTRO do contêiner do painel com a
# busca HTTP redirecionada desse domínio para o alvo. O que se prova: cadastro lido pelo hub, verificador executa, relatório
# gravado com os 10 itens. Itens de GSC/Bing ficam "não verificado" (sem credencial), como numa instalação nova.
#
# Nunca toca contêiner, volume ou rede que não sejam do projeto kronos-teste-<pid>.
set -uo pipefail

LIMITE=900
MODO=dind
ROTEIRO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --local) MODO=local ;;
    --dind) MODO=dind ;;
    --limite) shift; LIMITE="${1:?--limite precisa de segundos}" ;;
    --sem-cache) export KT_SEM_CACHE=1 ;;  # build frio (sem camadas em cache): o tempo que uma VPS nova leva de verdade
    --roteiro) ROTEIRO=1 ;;  # interno: o roteiro que roda dentro da "VPS" (a própria máquina em --local)
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "argumento desconhecido: $1 (uso: $0 --local|--dind [--limite N])" >&2; exit 2 ;;
  esac
  shift
done

agora_ms() { local t=${EPOCHREALTIME/./}; echo $((t / 1000)); }
fmt_ms() { printf '%d.%d s' $(($1 / 1000)) $(($1 % 1000 / 100)); }

# ============================================================================================
# Roteiro: o que uma pessoa faria numa VPS limpa, dentro da pasta do repositório.
# ============================================================================================
if [ "$ROTEIRO" = 1 ]; then
  : "${KT_PROJ:?}" "${KT_IMAGEM:?}" "${KRONOS_PAINEL_PORTA:?}" "${KRONOS_WEBHOOK_PORTA:?}" "${KRONOS_MCP_PORTA:?}"
  export KT_IMAGEM KRONOS_IMAGEM="$KT_IMAGEM" KRONOS_PAINEL_PORTA KRONOS_WEBHOOK_PORTA KRONOS_MCP_PORTA
  URL="http://127.0.0.1:$KRONOS_PAINEL_PORTA"
  DOMINIO_ALVO=alvo.kronos-teste.test
  dc() { docker compose -p "$KT_PROJ" "$@"; }
  senha=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
  export KRONOS_SENHA_INICIAL="$senha"
  EMAIL_DONO=dono@example.com
  JAR=$(mktemp)  # o painel entra por formulário (/entrar) e guarda a sessão em cookie

  etapa() {  # etapa "nome" funcao: cronometra, imprime e interrompe o roteiro se falhar
    local nome=$1 fn=$2 t0 t1
    echo "--- $nome"
    t0=$(agora_ms)
    if ! "$fn"; then
      t1=$(agora_ms)
      echo "[etapa] FALHOU: $nome ($(fmt_ms $((t1 - t0))))"
      dc ps -a 2>&1 | tail -n 8
      dc logs --tail 15 2>&1 | tail -n 40
      exit 1
    fi
    t1=$(agora_ms)
    echo "[etapa] $nome: $(fmt_ms $((t1 - t0)))"
  }

  e_build() { if [ -n "${KT_SEM_CACHE:-}" ]; then dc build --no-cache painel; else dc build painel; fi; }

  e_init() {
    # o toml mora no volume `config` (gravável: o /comecar o reescreve); o init grava direto nele (passo 3 do docs/INSTALAR.md)
    dc run --rm -T --no-deps -e KRONOS_SENHA_INICIAL --entrypoint kronos painel \
      init --sim --dominio kronos-teste.example.com --dono-nome "Dono Teste" --dono-email dono@example.com \
      --dono-senha-env KRONOS_SENHA_INICIAL --org exemplo-org --exemplo || return 1
    dc run --rm -T --no-deps --entrypoint grep painel -q 'dono@example.com' /etc/kronos/kronos.toml || { echo "kronos.toml sem o dono"; return 1; }
  }

  e_up() { dc up -d; }

  sessao() {  # cookies da sessão do dono (jar do /entrar) como cabeçalho Cookie; #HttpOnly_ é cookie válido
    awk -F'\t' '($1 ~ /^#HttpOnly_/ || $1 !~ /^#/) && NF >= 7 {printf "%s=%s; ", $6, $7}' "$JAR"
  }

  e_saude() {
    local i
    for i in $(seq 1 120); do
      [ "$(curl -fsS -m 3 "$URL/saude" 2>/dev/null)" = ok ] && break
      sleep 1
    done
    [ "$(curl -fsS -m 3 "$URL/saude" 2>/dev/null)" = ok ] || { echo "/saude não respondeu ok em 120 s"; return 1; }
    local sem login com
    sem=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -m 5 "$URL/") || true
    login=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -c "$JAR" -H "Origin: $URL" \
      --data-urlencode "email=$EMAIL_DONO" --data-urlencode "senha=$senha" "$URL/entrar") || true
    com=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -m 10 -b "$JAR" "$URL/") || true
    echo "GET / sem sessão = $sem (esperado 303 para /entrar); login do dono = $login (esperado 303); GET / com sessão = $com (esperado 200, ou 302 que não volta ao login)"
    [[ "$sem" == "303 "*"/entrar"* ]] && [ "$login" = 303 ] || return 1
    [[ "$com" == "200 "* || "$com" == "302 "* ]] && [[ "$com" != *"/entrar"* ]] || return 1
    local n
    n=$(dc ps --status running -q | wc -l)
    echo "serviços rodando: $n de 4"
    [ "$n" -eq 4 ]
  }

  e_cadastro() {
    local pag csrf cod
    pag=$(curl -fsS -m 10 -b "$JAR" "$URL/comecar?passo=projeto") || return 1
    csrf=$(printf '%s' "$pag" | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$csrf" ] || { echo "sem token csrf na página do assistente"; return 1; }
    cod=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Cookie: $(sessao)kronos_comecar=$csrf" -H "Origin: $URL" \
      --data-urlencode "csrf=$csrf" -d passo=projeto -d acao=salvar --data-urlencode "dominio=$DOMINIO_ALVO" \
      -d id=alvo-teste --data-urlencode "nome=Alvo do Teste" "$URL/comecar") || true
    echo "POST /comecar (passo projeto) = $cod (esperado 303)"
    [ "$cod" = 303 ] || return 1
    dc exec -T painel python3 -c "from hub import sites; s = sites.site('alvo-teste'); print('cadastrado:', s['id'], s['dominio'])"
  }

  e_rotina() {
    local saida
    saida=$(dc exec -T agenda deploy/rotina.sh teste-instalacao echo rotina-ok 2>&1) || { echo "$saida"; return 1; }
    echo "$saida"
    printf '%s\n' "$saida" | grep -q 'fim, código 0'
  }

  e_verificar() {
    local alvo="${KT_PROJ}-alvo"
    docker rm -f "$alvo" >/dev/null 2>&1 || true
    # alvo local: uma página de SEO mínima, só para o verificador ter o que ler (ver o cabeçalho deste arquivo)
    docker run -d --name "$alvo" --network "${KT_PROJ}_default" --network-alias alvo --entrypoint python3 "$KT_IMAGEM" -c '
import http.server
H = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Alvo do teste de instalação do Pereiraos</title>
<meta name="description" content="Página estática que o teste de instalação do Pereiraos usa como alvo do verificador de SEO, servida na mesma rede do compose.">
<link rel="canonical" href="https://alvo.kronos-teste.test/"><meta property="og:locale" content="pt_BR">
<meta property="og:title" content="Alvo do teste"></head><body><h1>Alvo do teste</h1></body></html>"""
CORPOS = {"/": ("text/html", H), "/robots.txt": ("text/plain", "User-agent: *\nAllow: /\nSitemap: https://alvo.kronos-teste.test/sitemap.xml\n")}
class A(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        t, c = CORPOS.get(self.path.split("?")[0], ("text/plain", "não achei"))
        self.send_response(200 if self.path.split("?")[0] in CORPOS else 404)
        self.send_header("Content-Type", t + "; charset=utf-8"); self.end_headers(); self.wfile.write(c.encode())
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), A).serve_forever()
' >/dev/null || return 1
    sleep 2
    dc exec -T painel python3 - <<'PY'
import json, sys, urllib.parse
import verificar_site as v
from hub import data
original = v.buscar
def local(url, limite=3_000_000):
    p = urllib.parse.urlsplit(url)
    if p.hostname != "alvo.kronos-teste.test":
        return original(url, limite)
    r = original("http://alvo:8000" + p.path + ("?" + p.query if p.query else ""), limite)
    r.url = url
    return r
v.Cache.__init__.__defaults__ = (local,)   # o verificador cria Cache() sem argumento: troca a busca padrão
codigo = v.main(["alvo-teste"])
rel = json.loads((data() / "verificador" / "alvo-teste" / "latest.json").read_text(encoding="utf-8"))
itens = rel["itens"]
print("relatório:", len(itens), "itens;", sum(i["ok"] is True for i in itens), "ok")
if codigo or len(itens) != 10 or not any(i["ok"] is True for i in itens):
    sys.exit(1)
PY
  }

  etapa "build da imagem" e_build
  etapa "kronos init --sim" e_init
  etapa "docker compose up" e_up
  etapa "espera do /saude e senha do painel" e_saude
  etapa "cadastro do projeto (POST /comecar)" e_cadastro
  etapa "rotina na agenda (rotina.sh)" e_rotina
  etapa "verificar o projeto" e_verificar
  exit 0
fi

# ============================================================================================
# Fora: prepara a "VPS" (dind ou esta máquina), roda o roteiro, cronometra o total e limpa.
# ============================================================================================
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PID=$$
PROJ="kronos-teste-$PID"
TMP="$(mktemp -d)"
INICIO=$(agora_ms)
LOG="$TMP/saida.log"
CODIGO=1

limpar() {
  local c=$?
  trap - EXIT INT TERM
  echo "--- limpeza (só o que é de $PROJ)"
  if [ "$MODO" = dind ]; then
    docker rm -f -v "$PROJ-dind" >/dev/null 2>&1 || true
  else
    docker rm -f "$PROJ-alvo" >/dev/null 2>&1 || true  # antes do down: ele ocupa a rede do projeto
    if [ -f "$TMP/repo/compose.yml" ]; then
      (cd "$TMP/repo" && KRONOS_IMAGEM="kronos-hub:teste-$PID" KRONOS_PAINEL_PORTA="${KRONOS_PAINEL_PORTA:-8787}" \
        docker compose -p "$PROJ" down -v --remove-orphans --timeout 5 >/dev/null 2>&1) || true
    fi
    docker rm -f "$PROJ-alvo" >/dev/null 2>&1 || true
    docker rmi "kronos-hub:teste-$PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
  exit "${CODIGO:-$c}"
}
trap limpar EXIT INT TERM

etapa_fora() {  # etapa_fora "nome" comando...
  local nome=$1 t0 t1
  shift
  echo "--- $nome"
  t0=$(agora_ms)
  "$@" || { echo "[etapa] FALHOU: $nome"; return 1; }
  t1=$(agora_ms)
  echo "[etapa] $nome: $(fmt_ms $((t1 - t0)))" | tee -a "$LOG"
}

copiar_repo() {  # cópia do repositório sem .git, dados e segredos (o que o `git clone` + checkout deixaria)
  tar -C "$RAIZ" --exclude=./.git --exclude=./.superpowers --exclude=./data --exclude=./secrets --exclude=./kronos.toml \
    --exclude='__pycache__' --exclude='*.pyc' -cf - .
}

copiar_local() { mkdir -p "$TMP/repo" && copiar_repo | tar -C "$TMP/repo" -xf -; }

subir_dind() {
  docker run -d --privileged --name "$PROJ-dind" -e DOCKER_TLS_CERTDIR= docker:dind >/dev/null || return 1
  local i
  for i in $(seq 1 90); do docker exec "$PROJ-dind" docker info >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

copiar_dind() { docker exec "$PROJ-dind" mkdir -p /repo && copiar_repo | docker exec -i "$PROJ-dind" tar -C /repo -xf -; }

porta_livre() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

echo "Teste de instalação do Pereiraos, modo $MODO, projeto $PROJ, limite ${LIMITE} s"

if [ "$MODO" = local ]; then
  etapa_fora "copiar o repositório" copiar_local || exit 1
  export KT_PROJ="$PROJ" KT_IMAGEM="kronos-hub:teste-$PID"
  export KRONOS_PAINEL_PORTA KRONOS_WEBHOOK_PORTA KRONOS_MCP_PORTA
  KRONOS_PAINEL_PORTA=$(porta_livre); KRONOS_WEBHOOK_PORTA=$(porta_livre); KRONOS_MCP_PORTA=$(porta_livre)
  echo "portas: painel $KRONOS_PAINEL_PORTA, webhook $KRONOS_WEBHOOK_PORTA, mcp $KRONOS_MCP_PORTA (todas em 127.0.0.1)"
  ( cd "$TMP/repo" && nice -n 19 bash scripts/teste-instalacao.sh --roteiro ) 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
else
  export DOCKER_CLI_HINTS=false
  etapa_fora "subir o docker:dind (a VPS limpa)" subir_dind || exit 1
  etapa_fora "preparar a VPS (bash, curl)" docker exec "$PROJ-dind" apk add --no-cache bash curl >/dev/null || exit 1
  etapa_fora "copiar o repositório" copiar_dind || exit 1
  docker exec -w /repo -e KT_PROJ="$PROJ" -e KT_IMAGEM=kronos-hub:latest -e KRONOS_PAINEL_PORTA=8787 \
    -e KRONOS_WEBHOOK_PORTA=8790 -e KRONOS_MCP_PORTA=4181 "$PROJ-dind" bash scripts/teste-instalacao.sh --roteiro 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
fi

FIM=$(agora_ms)
TOTAL_MS=$((FIM - INICIO))
echo
echo "===== Tempo por etapa ====="
grep '^\[etapa\]' "$LOG" | sed 's/^\[etapa\] //'
echo "TOTAL: $(fmt_ms "$TOTAL_MS") (limite ${LIMITE} s)"
CODIGO=0
if [ "$rc" -ne 0 ]; then
  echo "RESULTADO: FALHA (alguma etapa falhou)"
  CODIGO=1
elif [ "$TOTAL_MS" -gt $((LIMITE * 1000)) ]; then
  echo "RESULTADO: FALHA (passou de ${LIMITE} s)"
  CODIGO=1
else
  echo "RESULTADO: OK"
fi
exit "$CODIGO"
