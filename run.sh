#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/venv/bin/python"

if [[ "${1:-}" == "setup" ]]; then
  shift
  exec bash "$SCRIPT_DIR/setup.sh" "$@"
fi

if [[ ! -x "$VENV_PY" ]] || ! "$VENV_PY" -c "import os, sys, fastapi, playwright; raise SystemExit(0 if os.path.samefile(sys.prefix, 'venv') else 1)" >/dev/null 2>&1; then
  echo "[ERROR] Virtual environment 'venv' is missing or not configured for this device."
  echo "Please run './setup.sh' first to initialize the environment."
  exit 1
fi

case "${1:-}" in
  "")
    echo "=============================================================================="
    echo "                  LEAD EXTRACTION PIPELINE - LAUNCHER"
    echo "=============================================================================="
    echo "  [1] Run Lead Scraper & Auditor (default config.json)"
    echo "  [2] Open Verification Cockpit Dashboard (http://127.0.0.1:8000)"
    echo "  [3] Seed Offline FCC/NANPA Phone Prefixes"
    echo "  [4] Run Test Suite (pytest)"
    echo "  [5] Exit"
    read -r -p "Select option (1-5): " choice
    case "$choice" in
      1) exec "$VENV_PY" main.py ;;
      2) exec "$VENV_PY" dashboard.py --open-browser --no-reload ;;
      3) exec "$VENV_PY" scripts/seed_nanpa.py ;;
      4) exec "$VENV_PY" -m pytest tests/ -v ;;
      *) exit 0 ;;
    esac
    ;;
  dashboard)
    exec "$VENV_PY" dashboard.py --open-browser --no-reload
    ;;
  start|pipeline)
    shift
    exec "$VENV_PY" main.py "$@"
    ;;
  test|tests)
    exec "$VENV_PY" -m pytest tests/ -v
    ;;
  nanpa)
    exec "$VENV_PY" scripts/seed_nanpa.py
    ;;
  inspect)
    exec "$VENV_PY" scripts/inspect_db.py
    ;;
  *)
    exec "$VENV_PY" main.py "$@"
    ;;
esac
