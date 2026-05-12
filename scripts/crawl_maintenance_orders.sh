#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$ROOT/.venv/bin/python3"
STATE_FILE="$ROOT/.runtime/browser-state.json"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Virtual environment not found. Run: python3 \"$ROOT/scripts/bootstrap.py\"" >&2
  exit 1
fi

if [[ ! -f "$STATE_FILE" ]]; then
  echo "No browser session found. Start the login browser first:" >&2
  echo "  $ROOT/scripts/open_browser_for_login.sh" >&2
  exit 1
fi

# Resolve relative paths for output-dir to absolute
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      KEY="$1"
      shift
      if [[ -n "$1" ]]; then
        ABS_PATH="$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")" || ABS_PATH="$1"
        ARGS+=("$KEY" "$ABS_PATH")
      fi
      shift
      ;;
    --output-dir=*)
      KEY="${1%%=*}"
      VAL="${1#*=}"
      ABS_PATH="$(cd "$(dirname "$VAL")" 2>/dev/null && pwd)/$(basename "$VAL")" || ABS_PATH="$VAL"
      ARGS+=("$KEY=$ABS_PATH")
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

exec "$PYTHON_BIN" "$ROOT/scripts/crawl_maintenance_orders.py" --state-file "$STATE_FILE" "${ARGS[@]}"