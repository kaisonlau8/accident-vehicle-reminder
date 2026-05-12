#!/bin/bash
# 事故车维修超期提醒 Pipeline 启动脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/../.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "虚拟环境不存在，运行 bootstrap.py..."
    python3 "$SCRIPT_DIR/bootstrap.py"
fi

PYTHON="$VENV_DIR/bin/python"
exec "$PYTHON" "$SCRIPT_DIR/reminder_pipeline.py" "$@"
