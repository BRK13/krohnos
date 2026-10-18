#!/usr/bin/env bash
# Imprime o caminho de um gitleaks 8.30.1 pronto (baixa, confere sha256 e guarda em cache).
set -euo pipefail

VERSAO="8.30.1"
ARQUIVO="gitleaks_${VERSAO}_linux_x64.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSAO}/${ARQUIVO}"
SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/krohnos/gitleaks-${VERSAO}/gitleaks"

if [ -n "${GITLEAKS:-}" ] && [ -x "$GITLEAKS" ]; then
    echo "$GITLEAKS"; exit 0
fi
if command -v gitleaks >/dev/null 2>&1 && [ "$(gitleaks version 2>/dev/null)" = "$VERSAO" ]; then
    command -v gitleaks; exit 0
fi
if [ -x "$CACHE" ]; then
    echo "$CACHE"; exit 0
fi

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "gitleaks: só baixo para linux x86_64; instale o gitleaks $VERSAO e passe GITLEAKS=<caminho>" >&2
    exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$URL" -o "$tmp/$ARQUIVO"
soma=$(sha256sum "$tmp/$ARQUIVO" | cut -d' ' -f1)
if [ "$soma" != "$SHA256" ]; then
    rm -f "$tmp/$ARQUIVO"
    echo "gitleaks: sha256 do download não confere; abortei" >&2
    exit 1
fi
tar -xzf "$tmp/$ARQUIVO" -C "$tmp" gitleaks
mkdir -p "$(dirname "$CACHE")"
mv "$tmp/gitleaks" "$CACHE"
chmod 0755 "$CACHE"
echo "$CACHE"
