#!/usr/bin/env bash
# Runs everything automatically. Any other command works too, e.g.  ./start.sh status   ./start.sh spy
cd "$(dirname "$0")"
exec .venv/bin/python -m signals "${@:-start}"
