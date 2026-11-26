#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NON_INTERACTIVE=0
SEED_NANPA=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive|--ci|-y) NON_INTERACTIVE=1 ;;
    --seed-nanpa) SEED_NANPA=1 ;;
  esac
done

echo "=============================================================================="
echo "             LEAD EXTRACTION PIPELINE - POSIX SETUP (macOS / Linux)"
echo "=============================================================================="
echo "[i] Project directory: $SCRIPT_DIR"

if [[ ! -f "requirements.txt" || ! -f "config.json" ]]; then
  echo "[ERROR] Missing requirements.txt or config.json in $SCRIPT_DIR"
  exit 1
fi

find_python() {
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PY_EXE="$(find_python || true)"
if [[ -z "$PY_EXE" ]]; then
  echo "[ERROR] Python 3.10+ is required but was not found on PATH."
  exit 1
fi
PY_VER="$("$PY_EXE" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")"
echo "[+] Found Python $PY_VER at: $PY_EXE"

VENV_DIR="$SCRIPT_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"

if [[ -x "$VENV_PY" ]]; then
  if ! "$VENV_PY" -c "import os, sys; raise SystemExit(0 if os.path.samefile(sys.prefix, 'venv') else 1)" >/dev/null 2>&1 \
     || ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    echo "[!] Existing 'venv' is invalid or from another device/path. Recreating..."
    rm -rf "$VENV_DIR"
  else
    echo "[+] Valid local virtual environment already exists."
  fi
elif [[ -d "$VENV_DIR" ]]; then
  echo "[!] Incomplete or non-POSIX 'venv' directory detected. Recreating..."
  rm -rf "$VENV_DIR"
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "[*] Creating virtual environment..."
  "$PY_EXE" -m venv "$VENV_DIR"
fi

echo "[*] Installing Python dependencies..."
"$VENV_PY" -m pip install --upgrade pip --quiet --disable-pip-version-check || true
"$VENV_PY" -m pip install -r requirements.txt --disable-pip-version-check

echo "[*] Installing Playwright Chromium..."
"$VENV_PY" -m playwright install chromium

echo "[*] Initializing SQLite database..."
"$VENV_PY" -c "import json; from core import database; cfg=json.load(open('config.json', encoding='utf-8')); db=cfg.get('app', {}).get('db_path', 'data/lead_pipeline.db'); database.init_db(db); print(f'[+] SQLite database ready: {db}')"

if [[ "$SEED_NANPA" -eq 1 ]]; then
  echo "[*] Seeding offline FCC/NANPA phone prefix table..."
  "$VENV_PY" scripts/seed_nanpa.py
fi

echo ""
echo "Setup complete! Run './run.sh' to launch the pipeline, dashboard, or tests."
