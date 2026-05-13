"""消息分发器 — 将规则引擎输出转化为飞书消息并发送。

负责：消息路由、限速、去重、日志记录。
"""

import json
import time
from datetime import date
from pathlib import Path

# 同目录导入
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from feishu_client import (
    resolve_phone_to_open_id,
    send_card_message,
    send_file_message,
    build_alert_card,
    build_store_report_card,
    build_region_report_card,
    build_national_summary_card,
)

FOLLOWUP_DIR = Path(__file__).resolve().parent.parent / "data" / "followup"


def _log_followup(entry: dict):
    """追加写入审计日志。"""
    FOLLOWUP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = FOLLOWUP_DIR / "followup_log.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
def dispatch_alerts(alerts: list[dict]) -> dict:
    """发送 Rule 1 的分级告警消息（卡片格式，按级别颜色区分）。"""
    total_sent = 0
    total_failed = 0

    for alert in alerts:
        phones = alert.get("recipient_phones", [])
        card = build_alert_card(alert)

        for phone in phones:
            open_id = resolve_phone_to_open_id(phone)
            if not open_id:
                _log_followup({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "rule": 1, "vin": alert.get("vin", ""), "phone": phone,
                    "status": "failed", "error": "open_id_not_found",
                })
                total_failed += 1
                continue

            result = send_card_message(open_id, card)
            success = result is not None
            _log_followup({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "rule": 1, "vin": alert.get("vin", ""), "phone": phone, "open_id": open_id,
                "level": alert.get("level", {}).get("label", ""),
                "status": "sent" if success else "failed",
            })
            total_sent += 1 if success else 0
            total_failed += 1 if not success else 0
            time.sleep(0.3)

    return {"total_sent": total_sent, "total_failed": total_failed}


def dispatch_store_reports(store_reports: dict, excel_paths: dict[str, str] | None = None, skip_filter: bool = False) -> dict:
    """发送 Rule 3 门店每日报表（card 摘要 + file Excel）。"""
    total_sent = 0
    total_failed = 0

    for store_code, report in store_reports.items():
        phones = report.get("recipient_phones", [])
        if not skip_filter and not report.get("count_7d", 0) and not report.get("count_10d", 0) and not report.get("count_14d", 0):
            continue

        card = build_store_report_card(report["store_name"], report)
        excel_path = (excel_paths or {}).get(store_code)

        for phone in phones:
            open_id = resolve_phone_to_open_id(phone)
            if not open_id:
                total_failed += 1
                continue

            # 先发 card 摘要
            result1 = send_card_message(open_id, card)
            # 再发 Excel 文件
            result2 = send_file_message(open_id, excel_path) if excel_path else None

            sent = result1 is not None
            total_sent += 1 if sent else 0
            total_failed += 1 if not sent else 0

            _log_followup({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "rule": 3, "store_code": store_code, "phone": phone, "open_id": open_id,
                "status": "sent" if sent else "failed",
                "excel_sent": result2 is not None,
            })
            time.sleep(0.5)

    return {"total_sent": total_sent, "total_failed": total_failed}


def dispatch_region_reports(region_reports: dict, excel_paths: dict[str, str] | None = None, skip_filter: bool = False) -> dict:
    """发送 Rule 4 区域每日报表（card 摘要 + file Excel）。"""
    total_sent = 0
    total_failed = 0

    for region_name, report in region_reports.items():
        phones = report.get("recipient_phones", [])
        if not skip_filter and not report.get("count_7d", 0) and not report.get("count_10d", 0) and not report.get("count_14d", 0):
            continue

        card = build_region_report_card(region_name, report)
        excel_path = (excel_paths or {}).get(region_name)

        for phone in phones:
            open_id = resolve_phone_to_open_id(phone)
            if not open_id:
                total_failed += 1
                continue

            # 先发 card 摘要
            result1 = send_card_message(open_id, card)
            # 再发 Excel 文件
            result2 = send_file_message(open_id, excel_path) if excel_path else None

            sent = result1 is not None
            total_sent += 1 if sent else 0
            total_failed += 1 if not sent else 0

            _log_followup({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "rule": 4, "region": region_name, "phone": phone, "open_id": open_id,
                "status": "sent" if sent else "failed",
                "excel_sent": result2 is not None,
            })
            time.sleep(0.5)

    return {"total_sent": total_sent, "total_failed": total_failed}


def dispatch_national_report(national_data: dict, excel_path: str) -> dict:
    """发送 Rule 5 全国每日报表（card 摘要 + file Excel）。"""
    phones = national_data.get("recipient_phones", [])
    total_sent = 0
    total_failed = 0

    card = build_national_summary_card(national_data)

    for phone in phones:
        open_id = resolve_phone_to_open_id(phone)
        if not open_id:
            total_failed += 1
            continue

        # 先发 card 摘要
        result1 = send_card_message(open_id, card)
        # 再发 Excel 文件
        result2 = send_file_message(open_id, excel_path) if excel_path else None

        sent = result1 is not None
        total_sent += 1 if sent else 0
        total_failed += 1 if not sent else 0

        _log_followup({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "rule": 5, "phone": phone, "open_id": open_id,
            "status": "sent" if sent else "failed",
            "excel_sent": result2 is not None,
        })
        time.sleep(0.5)

    return {"total_sent": total_sent, "total_failed": total_failed}


if __name__ == "__main__":
    print("message_dispatcher 模块 — 请通过 reminder_pipeline.py 调用")
