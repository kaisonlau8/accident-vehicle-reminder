"""一键编排器 — 一条命令完成数据导入 → 规则评估 → 报表生成 → 消息发送。

用法:
  python reminder_pipeline.py --import-xlsx <path>       # 导入 Excel 并执行全部
  python reminder_pipeline.py --import-xlsx <path> --dry-run  # 仅评估不发送
  python reminder_pipeline.py --skip-crawl               # 使用最新快照
  python reminder_pipeline.py --rules 1,2                 # 仅执行指定规则
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_excel import import_excel
from rule_engine import evaluate_rules, load_stores_config
from report_generator import generate_national_report
from message_dispatcher import (
    dispatch_alerts,
    dispatch_store_reports,
    dispatch_region_reports,
    dispatch_national_report,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "repair_orders"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "run_manifest.json"


def _find_latest_snapshot() -> dict | None:
    if not DATA_DIR.exists():
        return None
    json_files = sorted(DATA_DIR.glob("repair_orders_*.json"), reverse=True)
    if not json_files:
        return None
    with open(json_files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(stats: dict):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def run_pipeline(
    xlsx_path: str | None = None,
    skip_crawl: bool = False,
    dry_run: bool = False,
    rules_filter: list[int] | None = None,
):
    start_time = time.time()
    print("=" * 60)
    print(f"🚀 事故车维修超期提醒 Pipeline — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── Step 1: 获取数据 ─────────────────────────────────────
    print("\n[1/4] 数据获取...")
    if xlsx_path:
        data = import_excel(xlsx_path)
        print(f"  导入完成: {data['total_records']} 条记录")
    elif skip_crawl:
        data = _find_latest_snapshot()
        if not data:
            print("  [ERROR] 无可用快照，请先 --import-xlsx")
            return
        print(f"  使用快照: {data.get('snapshot_date')} | {data['total_records']} 条记录")
    else:
        print("  [ERROR] 请指定 --import-xlsx 或 --skip-crawl")
        return

    # ── Step 2: 规则评估 ─────────────────────────────────────
    print("\n[2/4] 规则评估...")
    stores_config = load_stores_config()
    results = evaluate_rules(data, stores_config)

    alert_count = len(results["alerts"])
    store_count = len(results["store_reports"])
    region_count = len(results["region_reports"])
    national = results["national_report"]

    print(f"  告警: {alert_count} 条")
    print(f"  门店报表: {store_count} 个门店")
    print(f"  区域报表: {region_count} 个区域")
    print(f"  全国: 在修 {national['total_vehicles']} 台 | 7天 {national['count_7d']} | 10天 {national['count_10d']} | 11天 {national['count_11d']}")

    if dry_run:
        print("\n[DRY RUN] 仅评估，不发送消息")
        _save_manifest({
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "dry_run": True,
            "alert_count": alert_count,
            "store_count": store_count,
            "region_count": region_count,
            "national_summary": national,
            "elapsed_seconds": round(time.time() - start_time, 2),
        })
        return

    # ── Step 3: 报表生成 ─────────────────────────────────────
    print("\n[3/4] 报表生成...")
    excel_path = generate_national_report(results)

    # ── Step 4: 消息发送 ─────────────────────────────────────
    print("\n[4/4] 消息发送...")
    dispatch_stats = {"total_sent": 0, "total_failed": 0}

    if rules_filter is None or 1 in rules_filter or 2 in rules_filter:
        alerts = [a for a in results["alerts"]
                  if rules_filter is None or a["rule_id"] in rules_filter]
        r = dispatch_alerts(alerts)
        dispatch_stats["total_sent"] += r["total_sent"]
        dispatch_stats["total_failed"] += r["total_failed"]

    if rules_filter is None or 3 in rules_filter:
        r = dispatch_store_reports(results["store_reports"])
        dispatch_stats["total_sent"] += r["total_sent"]
        dispatch_stats["total_failed"] += r["total_failed"]

    if rules_filter is None or 4 in rules_filter:
        r = dispatch_region_reports(results["region_reports"])
        dispatch_stats["total_sent"] += r["total_sent"]
        dispatch_stats["total_failed"] += r["total_failed"]

    if rules_filter is None or 5 in rules_filter:
        r = dispatch_national_report(national, excel_path)
        dispatch_stats["total_sent"] += r["total_sent"]
        dispatch_stats["total_failed"] += r["total_failed"]

    elapsed = round(time.time() - start_time, 2)
    print(f"\n{'=' * 60}")
    print(f"✅ Pipeline 完成 | 耗时 {elapsed}s | 发送 {dispatch_stats['total_sent']} | 失败 {dispatch_stats['total_failed']}")
    print(f"{'=' * 60}")

    _save_manifest({
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "dry_run": False,
        "alert_count": alert_count,
        "store_count": store_count,
        "region_count": region_count,
        "national_summary": national,
        "dispatch_stats": dispatch_stats,
        "elapsed_seconds": elapsed,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="事故车维修超期提醒 Pipeline")
    parser.add_argument("--import-xlsx", type=str, help="DMS 导出的维修工单 Excel 路径")
    parser.add_argument("--skip-crawl", action="store_true", help="使用最新数据快照")
    parser.add_argument("--dry-run", action="store_true", help="仅评估，不发送消息")
    parser.add_argument("--rules", type=str, help="仅执行指定规则，逗号分隔（如 1,2）")
    args = parser.parse_args()

    rules_filter = None
    if args.rules:
        rules_filter = [int(r.strip()) for r in args.rules.split(",")]

    run_pipeline(
        xlsx_path=args.import_xlsx,
        skip_crawl=args.skip_crawl,
        dry_run=args.dry_run,
        rules_filter=rules_filter,
    )
