#!/usr/bin/env bash
# One-time setup for macOS / Linux:  ./install.sh     then start it any day with:  ./start.sh
set -e
cd "$(dirname "$0")"
PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is needed: https://www.python.org/downloads/  (then run ./install.sh again)"; exit 1
fi
echo "Installing (about a minute)..."
"$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m signals setup
echo
echo "Done. Start it with:  ./start.sh   (leave the window open; it runs every weekday at 9:25 and 15:50 ET)"
