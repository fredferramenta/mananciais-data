#!/bin/bash
# Roda o scraper a partir de uma máquina no Brasil e publica o resultado.
# Motivo: o site da COPASA recusa conexões de fora do Brasil (timeout no GitHub Actions),
# então a RM BH só atualiza quando o scraper roda daqui.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --rebase --quiet
/usr/bin/python3 scraper/scrape.py
if ! git diff --quiet data/reservatorios.json; then
  git add data/reservatorios.json
  git commit --quiet -m "Atualização local dos reservatórios (COPASA) — $(date '+%Y-%m-%d')"
  git push --quiet
  echo "Publicado."
else
  echo "Sem alterações."
fi
