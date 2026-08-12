#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

# shellcheck disable=SC1091
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

SESSION_HOME="${DFMC_DMS_SESSION_HOME:-$ROOT}"
STATE="$SESSION_HOME/.runtime/browser-state.json"
PROFILE_DIR="$SESSION_HOME/.browser-profile"

# 清理旧残留进程
if [[ -f "$STATE" ]]; then
  OLD_PID="$(python3 -c "import json; print(json.load(open('$STATE')).get('pid',''))" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && ! ps -p "$OLD_PID" >/dev/null 2>&1; then
    echo "旧浏览器已退出，清理残留..."
    pkill -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
    sleep 2
    rm -f "$STATE"
  fi
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "虚拟环境不存在，先运行: python3 $ROOT/scripts/bootstrap.py" >&2
  exit 1
fi

echo "========================================"
echo "  事故车提醒系统 — DMS 登录"
echo "========================================"
echo ""
echo "将使用独立 Chromium 打开 DMS（与 VIP 提醒共享会话），不占用本机 Chrome。"
echo "Session: $SESSION_HOME"
echo "登录完成后关闭提示，回到终端输入: ./run.sh"
echo ""

exec "$PYTHON" "$ROOT/scripts/open_browser_for_login.py"