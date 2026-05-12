"""初始化脚本 — 创建虚拟环境并安装依赖。"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = BASE_DIR / ".venv"
REQUIREMENTS = BASE_DIR / "requirements.txt"


def main():
    print(f"项目目录: {BASE_DIR}")
    print(f"虚拟环境: {VENV_DIR}")

    if VENV_DIR.exists():
        print("虚拟环境已存在，跳过创建")
    else:
        print("创建虚拟环境...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        print("✅ 虚拟环境创建完成")

    pip = str(VENV_DIR / "bin" / "pip")
    python_bin = str(VENV_DIR / "bin" / "python3")
    print("安装依赖...")
    subprocess.check_call([pip, "install", "-r", str(REQUIREMENTS)])
    print("✅ 依赖安装完成")

    print("安装 Playwright 浏览器...")
    subprocess.check_call([python_bin, "-m", "playwright", "install", "chromium"])
    print("✅ Playwright Chromium 安装完成")

    print("\n使用方式:")
    print(f"  {VENV_DIR}/bin/python {BASE_DIR}/scripts/reminder_pipeline.py --import-xlsx <path>")
    print(f"  {VENV_DIR}/bin/python {BASE_DIR}/scripts/reminder_pipeline.py --import-xlsx <path> --dry-run")
    print(f"  {VENV_DIR}/bin/python {BASE_DIR}/scripts/scheduler.py --morning")
    print(f"  {VENV_DIR}/bin/python {BASE_DIR}/scripts/scheduler.py --now")
    print(f"\nDMS爬取方式:")
    print(f"  {BASE_DIR}/scripts/open_browser_for_login.sh   # 先登录")
    print(f"  {BASE_DIR}/scripts/crawl_maintenance_orders.sh  # 爬取维修工单")
    print(f"  {BASE_DIR}/scripts/keepalive_browser.sh          # 保活(5分钟刷新)")


if __name__ == "__main__":
    main()
