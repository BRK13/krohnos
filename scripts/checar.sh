#!/usr/bin/env bash
# Roda todos os testes do repo: portão de neutralidade (tests/) e testes do site (site/tests/).
# O teste de navegador (Playwright) é à parte: python3 site/tests/browser_test.py
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== neutralidade (tests/)"
python3 -m unittest discover -s tests -v
echo "== site (site/tests/)"
(cd site && python3 -m unittest discover -s tests -v)
echo "ok"
