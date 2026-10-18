#!/usr/bin/env bash
# Portão de qualidade: testes, neutralidade e segredo na árvore (--historico: e no histórico).
# Uso: scripts/checar.sh [--sem-testes] [--historico]
# Saída: 0 ok; 1 achado ou erro; 2 uso errado; 3 gitleaks indisponível (rede/arquitetura: nada foi varrido).
set -euo pipefail
orig=$PWD
cd "$(dirname "$0")/.."

raiz=$PWD
testes=1
historico=0
so_arvore=""
while [ $# -gt 0 ]; do
    case "$1" in
        --sem-testes) testes=0 ;;
        --historico) historico=1 ;;
        --so-arvore) shift; so_arvore=$(cd "$orig" && realpath -- "${1:?--so-arvore pede a pasta do repo}") ;;
        *) echo "uso: scripts/checar.sh [--sem-testes] [--historico] [--so-arvore <repo>]" >&2; exit 2 ;;
    esac
    shift
done

# O gitleaks dir não respeita .gitignore: varre só o que o git considera do projeto
# (rastreado + novo não ignorado), copiado para uma pasta temporária.
varrer_arvore() {
    local g=$1 repo=$2 tmp lista rc=0
    tmp=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN
    lista="$tmp.lista"
    git -C "$repo" ls-files -z -co --exclude-standard >"$lista" || { rm -f "$lista"; return 1; }
    # só o que existe (arquivo apagado e ainda listado não conta)
    : >"$lista.ok"
    while IFS= read -r -d '' f; do
        if [ -f "$repo/$f" ]; then printf '%s\0' "$f" >>"$lista.ok"; fi
    done <"$lista"
    tar -C "$repo" --null -T "$lista.ok" -cf - | tar -xf - -C "$tmp" || { rm -f "$lista" "$lista.ok"; return 1; }
    rm -f "$lista" "$lista.ok"
    [ -n "$(ls -A "$tmp")" ] || { echo "checar: nada para varrer em $repo" >&2; return 1; }
    if [ -n "$so_arvore" ]; then
        python3 "$raiz/scripts/checar_publico.py" "$tmp" || return 1
    fi
    (cd "$tmp" && "$g" dir . --no-banner --redact --config "$raiz/.gitleaks.toml") || rc=$?
    return $rc
}

etapa=""
falhou() { echo "checar: FALHOU em $etapa"; exit 1; }

if [ -n "$so_arvore" ]; then testes=0; fi
if [ "$testes" = 1 ]; then
    etapa="testes"
    python3 -m unittest discover -s tests -t . -q || falhou
    etapa="neutralidade"
    python3 -m unittest tests.test_neutro -q || falhou
fi

G=$(scripts/gitleaks.sh) || { echo "checar: gitleaks indisponível" >&2; exit 3; }

etapa="gitleaks (árvore)"
varrer_arvore "$G" "${so_arvore:-.}" || falhou
if [ -n "$so_arvore" ]; then echo "checar: ok"; exit 0; fi

if [ "$historico" = 1 ]; then
    etapa="gitleaks (histórico)"
    "$G" git . --no-banner --redact --config .gitleaks.toml --gitleaks-ignore-path .gitleaksignore || falhou
fi

echo "checar: ok"
