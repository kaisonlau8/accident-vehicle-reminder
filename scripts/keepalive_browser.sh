#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$ROOT/.venv/bin/python3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Virtual environment not found. Run: python3 \"$ROOT/scripts/bootstrap.py\"" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$ROOT/scripts/keepalive_browser.py" "$@"