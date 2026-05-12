#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
STATE="$ROOT/.runtime/browser-state.json"

# 清理旧残留进程
if [[ -f "$STATE" ]]; then
  OLD_PORT="$(python3 -c "import json; print(json.load(open('$STATE')).get('port',''))" 2>/dev/null || true)"
  OLD_PID="$(python3 -c "import json; print(json.load(open('$STATE')).get('pid',''))" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && ! ps -p "$OLD_PID" >/dev/null 2>&1; then
    echo "旧浏览器已退出，清理残留..."
    pkill -f "user-data-dir=$ROOT/.browser-profile" 2>/dev/null || true
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
echo "Chrome 将打开 DMS 系统，请手动登录。"
echo "登录完成后关闭提示，回到终端输入: ./run.sh"
echo ""

exec "$PYTHON" "$ROOT/scripts/open_browser_for_login.py"