#!/usr/bin/env bash
# Jarvis setup for macOS / Linux. Run from the repo root:  ./setup.sh
set -euo pipefail
echo "== Python backend =="
cd backend
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium
cd ../app
echo "== Desktop app =="
npm install
echo "Done. Start Jarvis with:  cd app && npm run dev"
