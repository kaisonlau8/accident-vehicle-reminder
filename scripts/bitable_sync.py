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
    "督导open_id",
    "售后负责人",
    "售后电话",
    "售后open_id",
    "门店省",
    "门店市",
    "门店区",
    "售后负责人2",
    "售后电话2",
    "售后open_id2",
    "售后负责人3",
    "售后电话3",
    "售后open_id3",
    "售后负责人4",
    "售后电话4",
    "售后open_id4",
]

MANAGER_FIELD_PREFIX = "门店售后业务负责人"
MANAGER_PHONE_FIELD_PREFIX = "售后业务负责人手机号"
MANAGER_SLOT_COUNT = 4


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


def _parse_user(value: Any) -> tuple[str, str]:
    """飞书联系人字段 → (姓名, open_id)。"""
    items = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("en_name") or "").strip()
        open_id = str(item.get("id") or item.get("open_id") or "").strip()
        if name or open_id:
            return name, open_id
    return "", ""


def _normalize_phone(value: Any) -> str:
    phone = _field_text(value)
    phone = phone.replace("+86-", "").replace("+86", "").replace(" ", "").replace("-", "")
    if phone.endswith(".0") and phone[:-2].isdigit():
        phone = phone[:-2]
    return phone


def _normalized_fields(fields: dict) -> dict:
    """字段名去掉所有空白，兼容「售后负责人 1」和「售后负责人1」；同名别名取第一个非空值。"""
    out: dict = {}
    for key, value in fields.items():
        nk = "".join(str(key).split())
        if nk not in out or (not out[nk] and value):
            out[nk] = value
    return out


def _manager_slots(fields: dict) -> list[dict | None]:
    """按 1..4 槽位读取门店售后业务负责人，旧的无编号字段视为槽位1。

    槽位1：编号字段（人或手机号任一非空）整体优先，全空时才整体使用旧字段，
    不把两个人的姓名/open_id/手机号拼在一起。
    """
    normalized = _normalized_fields(fields)
    slots: list[dict | None] = []
    for slot in range(1, MANAGER_SLOT_COUNT + 1):
        name, open_id = _parse_user(normalized.get(f"{MANAGER_FIELD_PREFIX}{slot}"))
        phone = _normalize_phone(normalized.get(f"{MANAGER_PHONE_FIELD_PREFIX}{slot}"))
        if slot == 1 and not name and not open_id and not phone:
            name, open_id = _parse_user(normalized.get(MANAGER_FIELD_PREFIX))
            phone = _normalize_phone(normalized.get(MANAGER_PHONE_FIELD_PREFIX) or normalized.get("电话1"))
        if name or open_id or phone:
            slots.append({"name": name, "phone": phone, "open_id": open_id})
        else:
            slots.append(None)
    return slots


def _dedupe_manager_slots(slots: list[dict | None]) -> list[dict | None]:
    """按 open_id（否则姓名）去重，保留首次出现的槽位，并把重复项里的手机号/open_id 补到首次那条。"""
    index: dict[str, dict] = {}
    deduped: list[dict | None] = []
    for person in slots:
        if not person:
            deduped.append(None)
            continue
        key = person.get("open_id") or person.get("name") or ""
        if key and key in index:
            kept = index[key]
            kept["phone"] = kept.get("phone") or person.get("phone", "")
            kept["open_id"] = kept.get("open_id") or person.get("open_id", "")
            deduped.append(None)
            continue
        person = dict(person)
        if key:
            index[key] = person
        deduped.append(person)
    return deduped


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


def records_to_rows(records: list[dict]) -> tuple[list[list[Any]], list[str]]:
    """将「参与考核特约店清单」记录转为 list.xlsx 行。

    售后业务负责人支持 1~4 号槽位，主列写首个非空负责人，其余槽位追加在末尾。
    返回 (rows, warnings)。所有槽位都没有负责人时记入 warnings。
    """
    rows: list[list[Any]] = []
    warnings: list[str] = []
    for idx, fields in enumerate(records, start=1):
        store_code = _field_text(fields.get("店代码") or fields.get("门店编码"))
        if not store_code:
            continue

        sup_name, sup_open_id = _parse_user(fields.get("督导"))
        if not sup_name:
            sup_name = _field_text(fields.get("督导姓名"))

        slots = _dedupe_manager_slots(_manager_slots(fields))
        managers = [person for person in slots if person]
        primary = managers[0] if managers else {}
        if not managers:
            warnings.append(f"{store_code} {_field_text(fields.get('店名称') or fields.get('门店名称'))}".strip())

        row = [
            _field_text(fields.get("序号")) or str(idx),
            store_code,
            _field_text(fields.get("区域")),
            _field_text(fields.get("店名称") or fields.get("门店名称")),
            sup_name,
            _normalize_phone(fields.get("督导手机号") or fields.get("督导飞书电话号")),
            sup_open_id,
            primary.get("name", ""),
            primary.get("phone", ""),
            primary.get("open_id", ""),
            _field_text(fields.get("门店省")),
            _field_text(fields.get("门店市")),
            _field_text(fields.get("门店区")),
        ]
        for person in slots[1:]:
            person = person or {}
            row.extend([
                person.get("name", ""),
                person.get("phone", ""),
                person.get("open_id", ""),
            ])
        rows.append(row)
    return rows, warnings


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
    rows, missing_managers = records_to_rows(records)
    if not rows:
        raise RuntimeError("多维表格未返回有效门店记录（店代码为空）")

    backup_path = write_list_xlsx(rows, backup=backup)
    for label in missing_managers:
        print(f"[bitable-sync] 无售后负责人，门店消息将跳过: {label}")
    result = {
        "ok": True,
        "synced_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "record_count": len(records),
        "store_count": len(rows),
        "missing_manager_count": len(missing_managers),
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
