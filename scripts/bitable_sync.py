"""从飞书多维表格同步门店/督导联系人到 list.xlsx。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import openpyxl
import requests
from dotenv import load_dotenv

from feishu_client import BASE_URL, _auth_headers
from time_utils import beijing_strftime

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PLUGIN_ROOT / ".env")

LIST_XLSX_PATH = PLUGIN_ROOT / "list.xlsx"
SYNC_STATE_PATH = PLUGIN_ROOT / ".runtime" / "bitable-sync-state.json"

HEADERS = [
    "序号",
    "门店编码",
    "区域",
    "门店名称",
    "督导姓名",
    "督导飞书电话号",
    "提醒人1",
    "电话1",
    "提醒人2",
    "电话2",
    "提醒人3",
    "电话3",
    "提醒人4",
    "电话4",
]


def _bitable_config() -> tuple[str, str, str]:
    app_token = os.getenv("BITABLE_APP_TOKEN", "").strip()
    table_id = os.getenv("BITABLE_TABLE_ID", "").strip()
    view_id = os.getenv("BITABLE_VIEW_ID", "").strip()
    if not app_token or not table_id:
        raise RuntimeError("缺少 BITABLE_APP_TOKEN 或 BITABLE_TABLE_ID，请在 .env 中配置")
    return app_token, table_id, view_id


def _field_text(value: Any) -> str:
    """把多维表格字段值规范成纯文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text is None and "name" in item:
                    text = item.get("name")
                if text is not None:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        if "text" in value:
            return str(value.get("text") or "").strip()
        if "name" in value:
            return str(value.get("name") or "").strip()
    return str(value).strip()


def _normalize_phone(value: Any) -> str:
    phone = _field_text(value)
    phone = phone.replace("+86-", "").replace("+86", "").replace(" ", "").replace("-", "")
    if phone.endswith(".0") and phone[:-2].isdigit():
        phone = phone[:-2]
    return phone


def fetch_bitable_records() -> list[dict]:
    """分页拉取多维表格全部记录。"""
    app_token, table_id, view_id = _bitable_config()
    records: list[dict] = []
    page_token = ""

    while True:
        params: dict[str, Any] = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=_auth_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"拉取多维表格失败: {payload.get('msg') or payload}")

        data = payload.get("data") or {}
        for item in data.get("items") or []:
            records.append(item.get("fields") or {})

        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break

    return records


def records_to_rows(records: list[dict]) -> list[list[Any]]:
    """将多维表格记录转为 list.xlsx 行。"""
    rows: list[list[Any]] = []
    for idx, fields in enumerate(records, start=1):
        store_code = _field_text(fields.get("门店编码"))
        if not store_code:
            continue

        seq = _field_text(fields.get("序号")) or str(idx)
        rows.append([
            seq,
            store_code,
            _field_text(fields.get("区域")),
            _field_text(fields.get("门店名称")),
            _field_text(fields.get("督导姓名")),
            _normalize_phone(fields.get("督导飞书电话号")),
            _field_text(fields.get("提醒人1")),
            _normalize_phone(fields.get("电话1")),
            _field_text(fields.get("提醒人2")),
            _normalize_phone(fields.get("电话2")),
            _field_text(fields.get("提醒人3")),
            _normalize_phone(fields.get("电话3")),
            _field_text(fields.get("提醒人4")),
            _normalize_phone(fields.get("电话4")),
        ])
    return rows


def write_list_xlsx(rows: list[list[Any]], backup: bool = True) -> Path | None:
    """写入 list.xlsx，可选备份旧文件。返回备份路径。"""
    backup_path = None
    if backup and LIST_XLSX_PATH.exists():
        backup_path = LIST_XLSX_PATH.parent / f"list_backup_{beijing_strftime('%Y%m%d%H%M%S')}.xlsx"
        shutil.copy2(str(LIST_XLSX_PATH), str(backup_path))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "门店联系人"
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(str(LIST_XLSX_PATH))
    return backup_path


def _save_sync_state(state: dict) -> None:
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SYNC_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_sync_state() -> dict:
    if not SYNC_STATE_PATH.exists():
        return {}
    try:
        with open(SYNC_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sync_contacts_from_bitable(*, backup: bool = True) -> dict:
    """从飞书多维表格同步联系人到 list.xlsx。"""
    records = fetch_bitable_records()
    rows = records_to_rows(records)
    if not rows:
        raise RuntimeError("多维表格未返回有效门店记录（门店编码为空）")

    backup_path = write_list_xlsx(rows, backup=backup)
    result = {
        "ok": True,
        "synced_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "record_count": len(records),
        "store_count": len(rows),
        "backup": str(backup_path.name) if backup_path else "",
        "source": "bitable",
    }
    _save_sync_state(result)
    print(
        f"[bitable-sync] 同步完成: 拉取 {result['record_count']} 条，"
        f"写入门店 {result['store_count']} 家"
        + (f"，备份 {result['backup']}" if result["backup"] else "")
    )
    return result


if __name__ == "__main__":
    print(json.dumps(sync_contacts_from_bitable(), ensure_ascii=False, indent=2))
