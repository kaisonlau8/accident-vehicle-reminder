"""调度器 — 定时触发提醒任务。

- 10:00 → 爬取维修工单 → 导入 → 规则 1 告警 + 规则 3/4 门店/区域报表
- 17:00 → 爬取维修工单 → 导入 → 规则 3/4 门店/区域报表 + 规则 5 全国报表

也支持 --now 立即执行和 --test 测试模式（所有消息发给指定手机号）。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_excel import import_excel
from rule_engine import evaluate_rules, load_stores_config
from report_generator import generate_national_report, generate_all_region_reports, generate_all_store_reports
from message_dispatcher import (
    dispatch_alerts,
    dispatch_store_reports,
    dispatch_region_reports,
    dispatch_national_report,
)
from time_utils import beijing_now, beijing_strftime

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PLUGIN_ROOT / "data" / "repair_orders"
OUTPUT_DIR = PLUGIN_ROOT / "output"
VENV_DIR = PLUGIN_ROOT / ".venv"
PYTHON_BIN = VENV_DIR / "bin" / "python3"
CRAWL_SCRIPT = Path(__file__).resolve().parent / "crawl_maintenance_orders.py"

DEFAULT_TEST_PHONE = os.getenv("ADMIN_MOBILE", "").replace("+86-", "").replace("+86", "")


def _override_recipients(results: dict, test_phone: str) -> dict:
    """测试模式：将所有收件人替换为指定手机号，一条都不跳过。"""
    # 告警：每条都发
    for alert in results["alerts"]:
        alert["recipient_phones"] = [test_phone]

    # 门店报表：每条都发（去掉 count 过滤）
    for store_code, report in results["store_reports"].items():
        report["recipient_phones"] = [test_phone]

    # 区域报表：每条都发
    for region_name, report in results["region_reports"].items():
        report["recipient_phones"] = [test_phone]

    # 全国报表
    results["national_report"]["recipient_phones"] = [test_phone]

    return results


def _find_latest_snapshot() -> dict | None:
    """找到最新的 repair_orders JSON 快照。"""
    if not DATA_DIR.exists():
        return None
    json_files = sorted(DATA_DIR.glob("repair_orders_*.json"), reverse=True)
    if not json_files:
        return None
    with open(json_files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def _crawl_and_import() -> dict | None:
    """运行维修工单爬取，然后导入Excel生成JSON。返回导入后的数据dict。"""
    from dfmc_browser_utils import ensure_cdp_browser_running, get_default_state_file

    state_file = get_default_state_file(PLUGIN_ROOT)

    # Validate browser (with auto-recovery if state file is missing/stale)
    try:
        ensure_cdp_browser_running(state_file)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[WARN] 无浏览器会话: {exc}。使用最新快照。")
        return _find_latest_snapshot()

    # 运行爬取
    print("开始爬取维修工单...")
    result = subprocess.run(
        [str(PYTHON_BIN), str(CRAWL_SCRIPT),
         "--state-file", str(state_file),
         "--output-dir", str(OUTPUT_DIR)],
        capture_output=True, text=True, timeout=600,
    )

    if result.returncode != 0:
        err_msg = result.stderr.strip() or result.stdout.strip() or "未知错误"
        print(f"[ERROR] 爬取失败: {err_msg}")
        return _find_latest_snapshot()

    # 找最新的爬取Excel
    xlsx_files = sorted(OUTPUT_DIR.glob("maintenance_orders_*.xlsx"), reverse=True)
    if not xlsx_files:
        print("[ERROR] 爬取后未找到Excel文件")
        return _find_latest_snapshot()

    xlsx_path = str(xlsx_files[0])
    print(f"导入Excel: {xlsx_path}")

    # 导入Excel生成JSON
    try:
        data = import_excel(xlsx_path)
        print(f"导入完成: {data.get('total_records', 0)} 条记录, {data.get('accident_records', '?')} 条事故车")
        return data
    except Exception as exc:
        print(f"[ERROR] 导入失败: {exc}")
        return _find_latest_snapshot()


def run_morning_alerts(xlsx_path: str | None = None, skip_crawl: bool = False, test_phone: str | None = None):
    """执行 10:00 任务（规则 1 告警 + 规则 3/4 门店/区域报表）。"""
    print("=" * 50)
    mode_tag = f" [测试→{test_phone}]" if test_phone else ""
    print(f"[10:00] 事故车超期告警 + 门店/区域报表{mode_tag} — {beijing_strftime('%Y-%m-%d %H:%M')} (北京时间)")
    print("=" * 50)

    # 获取数据
    if xlsx_path:
        data = import_excel(xlsx_path)
    elif skip_crawl:
        data = _find_latest_snapshot()
        if not data:
            print("[ERROR] 无可用数据快照，请先导入 Excel")
            return
    else:
        data = _crawl_and_import()
        if not data:
            print("[ERROR] 无可用数据")
            return

    stores_config = load_stores_config()
    results = evaluate_rules(data, stores_config)

    if test_phone:
        _override_recipients(results, test_phone)

    # 发送所有 ≥7天告警（已按分级标记）
    alerts = results["alerts"]
    if alerts:
        print(f"发现 {len(alerts)} 条超期告警:")
        for a in alerts:
            print(f"  Rule {a['rule_id']}: {a['store_name']} | {a['vin'][-8:]} | {a['days_in_shop']}天")
        dispatch_results = dispatch_alerts(alerts)
        print(f"告警发送: 成功 {dispatch_results['total_sent']} | 失败 {dispatch_results['total_failed']}")
    else:
        print("当前无超期告警")

    # 发送门店报表（card + Excel）
    store_excel_paths = generate_all_store_reports(results)
    r3 = dispatch_store_reports(results["store_reports"], store_excel_paths, skip_filter=test_phone is not None)
    print(f"门店报表发送: 成功 {r3['total_sent']} | 失败 {r3['total_failed']}")

    # 发送区域报表（card + Excel）
    region_excel_paths = generate_all_region_reports(results)
    r4 = dispatch_region_reports(results["region_reports"], region_excel_paths, skip_filter=test_phone is not None)
    print(f"区域报表发送: 成功 {r4['total_sent']} | 失败 {r4['total_failed']}")


def run_evening_reports(xlsx_path: str | None = None, skip_crawl: bool = False, test_phone: str | None = None):
    """执行 17:00 报表任务（规则 3 + 4 + 5）。"""
    print("=" * 50)
    mode_tag = f" [测试→{test_phone}]" if test_phone else ""
    print(f"[17:00] 事故车超期日报{mode_tag} — {beijing_strftime('%Y-%m-%d %H:%M')} (北京时间)")
    print("=" * 50)

    # 获取数据
    if xlsx_path:
        data = import_excel(xlsx_path)
    elif skip_crawl:
        data = _find_latest_snapshot()
        if not data:
            print("[ERROR] 无可用数据快照，请先导入 Excel")
            return
    else:
        data = _crawl_and_import()
        if not data:
            print("[ERROR] 无可用数据")
            return

    stores_config = load_stores_config()
    results = evaluate_rules(data, stores_config)

    if test_phone:
        _override_recipients(results, test_phone)

    # Rule 5: 全国报表（先生成 Excel）
    national = results["national_report"]
    excel_path = generate_national_report(results)

    print(f"全国汇总: 在修 {national['total_vehicles']} 台 | 7天 {national['count_7d']} | 10天 {national['count_10d']} | 14天 {national['count_14d']}")
    print(
        "KPI:"
        f" 30天内事故工单7天完工率 {national['kpi_7d_rate']}"
        f" | 30天内事故工单10天完工率 {national['kpi_10d_rate']}"
    )

    # 发送门店报表（card + Excel）
    store_excel_paths = generate_all_store_reports(results)
    r3 = dispatch_store_reports(results["store_reports"], store_excel_paths, skip_filter=test_phone is not None)
    print(f"门店报表发送: 成功 {r3['total_sent']} | 失败 {r3['total_failed']}")

    # 发送区域报表（card + Excel）
    region_excel_paths = generate_all_region_reports(results)
    r4 = dispatch_region_reports(results["region_reports"], region_excel_paths, skip_filter=test_phone is not None)
    print(f"区域报表发送: 成功 {r4['total_sent']} | 失败 {r4['total_failed']}")

    # 发送全国报表
    r5 = dispatch_national_report(national, excel_path)
    print(f"全国报表发送: 成功 {r5['total_sent']} | 失败 {r5['total_failed']}")


def run_test_mode(xlsx_path: str | None = None, skip_crawl: bool = False, test_phone: str = DEFAULT_TEST_PHONE):
    """测试模式：爬取+导入+全部规则，所有消息发给测试手机号。"""
    print("*" * 50)
    print(f"[测试模式] 所有消息发送给 {test_phone}")
    print("*" * 50)

    run_morning_alerts(xlsx_path, skip_crawl=skip_crawl, test_phone=test_phone)
    run_evening_reports(xlsx_path, skip_crawl=skip_crawl, test_phone=test_phone)


def run_all(xlsx_path: str | None = None, skip_crawl: bool = False):
    """运行全部规则（正式模式）。"""
    run_morning_alerts(xlsx_path, skip_crawl=skip_crawl)
    run_evening_reports(xlsx_path, skip_crawl=skip_crawl)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="事故车维修超期提醒调度器")
    parser.add_argument("--now", action="store_true", help="立即执行全部规则（正式模式）")
    parser.add_argument("--test", action="store_true", help="测试模式：所有消息发给测试手机号，跑完10:00+17:00全部规则")
    parser.add_argument("--test-phone", type=str, default=DEFAULT_TEST_PHONE, help=f"测试手机号（默认{DEFAULT_TEST_PHONE}）")
    parser.add_argument("--morning", action="store_true", help="仅执行 10:00 任务（规则1告警 + 规则3/4报表）")
    parser.add_argument("--evening", action="store_true", help="仅执行 17:00 报表（规则 3+4+5）")
    parser.add_argument("--import-xlsx", type=str, help="导入指定的 DMS Excel 文件后执行")
    parser.add_argument("--skip-crawl", action="store_true", help="跳过自动爬取，使用最新数据快照")
    args = parser.parse_args()

    xlsx = args.import_xlsx if args.import_xlsx else None
    skip_crawl = args.skip_crawl

    if args.test:
        run_test_mode(xlsx, skip_crawl=skip_crawl, test_phone=args.test_phone)
    elif args.now:
        run_all(xlsx, skip_crawl=skip_crawl)
    elif args.morning:
        run_morning_alerts(xlsx, skip_crawl=skip_crawl)
    elif args.evening:
        run_evening_reports(xlsx, skip_crawl=skip_crawl)
    else:
        # 默认：持续运行定时任务（10:00/17:00自动爬取+告警+报表）
        print("事故车提醒调度器已启动")
        print("  10:00 — 自动爬取 + 超期告警 + 门店/区域报表")
        print("  17:00 — 自动爬取 + 门店/区域报表 + 全国报表")
        print("  按 Ctrl+C 退出")
        print()

        fired = {"10:00": False, "17:00": False}
        while True:
            now = beijing_now().strftime("%H:%M")
            if now == "10:00" and not fired["10:00"]:
                run_morning_alerts(skip_crawl=skip_crawl)
                fired["10:00"] = True
            elif now == "17:00" and not fired["17:00"]:
                run_evening_reports(skip_crawl=skip_crawl)
                fired["17:00"] = True
            elif now not in ("10:00", "17:00"):
                # 过了触发点后重置，允许次日再触发
                fired = {"10:00": False, "17:00": False}
            time.sleep(30)
