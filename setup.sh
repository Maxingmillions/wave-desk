#!/bin/bash
# Set up the wave desk on this machine.
#
#   bash setup.sh
#
# Finds a Python 3.10+ (the IBKR library needs it), creates ./.venv, installs
# the requirements, creates .env from the example if missing, and runs the
# offline tests. Safe to re-run.

set -uo pipefail
cd "$(dirname "$0")"

echo
echo "================================================================"
echo " Wave desk setup"
echo "================================================================"
echo

# --- find a modern python --------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$v" -ge 310 ]; then PY=$(command -v "$c"); break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed and none was found."
  echo "On a Mac:   brew install python@3.12     (then re-run this script)"
  echo "Or install from https://www.python.org/downloads/"
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

# --- venv + deps -----------------------------------------------------------
if [ ! -x ./.venv/bin/python ]; then
  "$PY" -m venv .venv || { echo "could not create .venv"; exit 1; }
fi
./.venv/bin/python -m pip install --upgrade pip -q
./.venv/bin/pip install -q -r requirements.txt || { echo "pip install failed"; exit 1; }
echo "Dependencies installed into ./.venv"

# --- .env ------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example - edit it for IBKR_PORT, Telegram, WAVE_MODE."
else
  echo ".env already exists; left as is."
fi
[ -f portfolio.json ] || cp portfolio.example.json portfolio.json

# --- smoke test ------------------------------------------------------------
echo
echo "Running offline tests..."
./.venv/bin/python test_desk.py >/tmp/desk-test.log 2>&1 && echo "  desk tests: ok" || { echo "  desk tests FAILED (see /tmp/desk-test.log)"; }
./.venv/bin/python test_rotation.py >/tmp/rotation-test.log 2>&1 && echo "  planner tests: ok" || { echo "  planner tests FAILED (see /tmp/rotation-test.log)"; }

echo
echo "Done. Next:"
echo "  ./.venv/bin/python terminal.py --sim      rehearse with simulated fills"
echo "  ./.venv/bin/python terminal.py            connect to TWS / IB Gateway (port in .env)"
echo "  ./.venv/bin/python plan.py                tomorrow's rotation plan"
echo
