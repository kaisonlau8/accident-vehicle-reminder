#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
STATE="$ROOT/.runtime/browser-state.json"

if [[ ! -x "$PYTHON" ]]; then
  echo "虚拟环境不存在，先运行: python3 $ROOT/scripts/bootstrap.py" >&2
  exit 1
fi

# 解析参数
MODE="test"
SKIP_CRAWL=""
TEST_PHONE="${ADMIN_MOBILE:-}"
CONSOLE=""
CONSOLE_PORT="9000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)      MODE="test"; shift ;;
    --prod)      MODE="prod"; shift ;;
    --skip-crawl) SKIP_CRAWL="--skip-crawl"; shift ;;
    --test-phone) TEST_PHONE="$2"; shift 2 ;;
    --morning)   MODE="morning"; shift ;;
    --evening)   MODE="evening"; shift ;;
    --console)   CONSOLE="yes"; shift ;;
    --port)      CONSOLE_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "用法: ./run.sh [选项]"
      echo ""
      echo "  --test          测试模式：所有消息发给测试手机号（默认）"
      echo "  --prod          正式模式：启动保活 + 定时调度常驻运行"
      echo "  --morning       仅执行10:00任务（告警+门店/区域报表）"
      echo "  --evening       仅执行17:00报表（门店/区域/全国报表）"
      echo "  --skip-crawl    跳过爬取，使用最新数据快照"
      echo "  --test-phone N  测试手机号（默认读取 ADMIN_MOBILE 环境变量）"
      echo "  --console       启动 Web 控制台（端口9000）"
      echo "  --port N        Web 控制台端口（默认9000，需配合 --console）"
      echo ""
      echo "示例："
      echo "  ./login.sh                 第一步：打开DMS登录"
      echo "  ./run.sh                   第二步：测试模式（爬取+发送到测试手机号）"
      echo "  ./run.sh --skip-crawl      测试模式，用已有数据不爬取"
      echo "  ./run.sh --prod            正式模式：保活+定时调度常驻运行"
      echo "  ./run.sh --prod --morning  正式模式，仅10:00任务（单次）"
      echo "  ./run.sh --prod --evening  正式模式，仅17:00报表（单次）"
      echo "  ./run.sh --console         仅启动 Web 控制台（可手动触发任务）"
      echo "  ./run.sh --console --skip-crawl  Web 控制台 + 使用已有数据"
      echo "  ./run.sh --prod --console  正式模式 + Web 控制台同时运行"
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# 检查浏览器会话（console 模式不需要，skip-crawl 不需要）
if [[ -z "$SKIP_CRAWL" ]] && [[ "$CONSOLE" != "yes" ]] && [[ ! -f "$STATE" ]]; then
  echo "[ERROR] 无浏览器会话，请先运行: ./login.sh" >&2
  exit 1
fi

echo "========================================"

# --console 模式：启动 Web 控制台（所有任务通过控制台触发）
if [[ "$CONSOLE" == "yes" ]]; then
  echo "  事故车提醒系统 — Web 控制台"
  echo "  端口: $CONSOLE_PORT"
  echo "  通过浏览器控制模式切换和手动触发"
  echo "========================================"
  echo ""
  exec "$PYTHON" "$ROOT/scripts/web_console.py" --port "$CONSOLE_PORT"
fi

case "$MODE" in
  test)
    echo "  事故车提醒系统 — 测试模式"
    echo "  所有消息发送给: $TEST_PHONE"
    echo "========================================"
    echo ""
    exec "$PYTHON" "$ROOT/scripts/scheduler.py" --test --test-phone "$TEST_PHONE" ${SKIP_CRAWL:-}
    ;;
  prod)
    if [[ -z "$SKIP_CRAWL" ]]; then
      # 正式常驻模式：后台启动保活 + 前台启动定时调度
      echo "  事故车提醒系统 — 正式模式"
      echo "  浏览器保活 + 定时调度常驻运行"
      echo "========================================"
      echo ""

      # 启动保活（后台）
      echo "启动浏览器保活..."
      "$PYTHON" "$ROOT/scripts/keepalive_browser.py" &
      KEEPALIVE_PID=$!
      echo "保活进程 PID: $KEEPALIVE_PID"
      echo ""

      # 启动定时调度（前台常驻）
      echo "启动定时调度器..."
      echo "  10:00 — 自动爬取 + 超期告警 + 门店/区域报表"
      echo "  17:00 — 自动爬取 + 门店/区域报表 + 全国报表"
      echo "  按 Ctrl+C 退出（保活进程也会一起停止）"
      echo ""

      # Ctrl+C 时同时杀保活
      trap "echo '停止保活进程...'; kill $KEEPALIVE_PID 2>/dev/null; exit 0" SIGINT SIGTERM

      exec "$PYTHON" "$ROOT/scripts/scheduler.py"
    else
      # 正式单次模式
      echo "  事故车提醒系统 — 正式模式（单次执行）"
      echo "========================================"
      echo ""
      exec "$PYTHON" "$ROOT/scripts/scheduler.py" --now ${SKIP_CRAWL:-}
    fi
    ;;
  morning)
    echo "  事故车提醒系统 — 仅10:00任务（正式）"
    echo "========================================"
    echo ""
    exec "$PYTHON" "$ROOT/scripts/scheduler.py" --morning ${SKIP_CRAWL:-}
    ;;
  evening)
    echo "  事故车提醒系统 — 仅17:00报表（正式）"
    echo "========================================"
    echo ""
    exec "$PYTHON" "$ROOT/scripts/scheduler.py" --evening ${SKIP_CRAWL:-}
    ;;
esac