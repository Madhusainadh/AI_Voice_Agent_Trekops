#!/usr/bin/env bash
# Dev runner. Production should use a process manager (systemd / pm2) and put
# the service on the same box or region as your Meta webhook endpoint.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "→ creating venv"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

[ -f .env ] || { echo "✗ .env missing — copy .env.example and fill it in"; exit 1; }

exec ./.venv/bin/python -m app.server
