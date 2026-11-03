#!/usr/bin/env bash
# ── J.A.R.V.I.S. one-time setup for macOS / Linux ──
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
chmod +x jarvis.sh
echo
echo " Setup complete."
echo " 1) Edit .env: add GEMINI_API_KEY and JARVIS_ACCESS_CODE"
echo " 2) Save your Google OAuth client file as data/credentials.json (see README)"
echo " 3) Start with: ./jarvis.sh"
