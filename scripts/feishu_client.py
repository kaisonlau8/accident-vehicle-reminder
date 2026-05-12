"""飞书 API 客户端 — 封装认证、消息发送、文件上传。

移植自 lark-sendmessage-bot/code/main.js + send_feishu_file.py，
增加 token 缓存、card 消息、rate limit 处理。
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://open.feishu.cn/open-apis"

# ── Token 管理 ──────────────────────────────────────────────

_token_cache: dict = {"token": None, "expires_at": 0}


def _get_app_credentials() -> tuple[str, str]:
    app_id = os.getenv("APP_ID", "")
    app_secret = os.getenv("APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("缺少 APP_ID 或 APP_SECRET，请在 .env 中配置")
    return app_id, app_secret


def get_tenant_token() -> str:
    """获取 tenant_access_token，带 2 小时缓存。"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    app_id, app_secret = _get_app_credentials()
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {data}")

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200) - 300
    return _token_cache["token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_tenant_token()}"}


# ── 用户查询 ────────────────────────────────────────────────

_recipients_cache_path = Path(__file__).resolve().parent.parent / "config" / "recipients.json"
_phone_to_open_id: dict = {}


def _load_recipients_cache():
    global _phone_to_open_id
    if _recipients_cache_path.exists():
        with open(_recipients_cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            _phone_to_open_id = {
                e["phone"]: e["open_id"] for e in data.get("entries", [])
            }


def _save_recipients_cache():
    entries = [
        {"phone": k, "open_id": v, "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")}
        for k, v in _phone_to_open_id.items()
    ]
    _recipients_cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_recipients_cache_path, "w", encoding="utf-8") as f:
        json.dump({"last_updated": time.strftime("%Y-%m-%d"), "entries": entries}, f, ensure_ascii=False, indent=2)


def resolve_phone_to_open_id(phone: str) -> str | None:
    """手机号 → open_id，优先查缓存，未命中则调 API。"""
    phone = phone.lstrip("+").replace("-", "").replace(" ", "")
    if not _phone_to_open_id:
        _load_recipients_cache()
    if phone in _phone_to_open_id:
        return _phone_to_open_id[phone]

    resp = requests.post(
        f"{BASE_URL}/contact/v3/users/batch_get_id?user_id_type=open_id",
        headers=_auth_headers(),
        json={"mobiles": [phone], "emails": [], "include_resigned": False},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] 解析手机号 {phone} 失败: {data.get('msg')}")
        return None

    user_list = data.get("data", {}).get("user_list", [])
    if not user_list:
        print(f"[WARN] 手机号 {phone} 未找到飞书用户")
        return None

    open_id = user_list[0].get("user_id") or user_list[0].get("open_id")
    if open_id:
        _phone_to_open_id[phone] = open_id
        _save_recipients_cache()
    return open_id


def resolve_user_name(open_id: str) -> str:
    """open_id → 用户姓名。"""
    resp = requests.get(
        f"{BASE_URL}/contact/v3/users/{open_id}?user_id_type=open_id",
        headers=_auth_headers(),
        timeout=10,
    )
    if resp.ok:
        data = resp.json()
        return data.get("data", {}).get("user", {}).get("name", open_id)
    return open_id


# ── 消息发送 ────────────────────────────────────────────────

def send_text_message(open_id: str, text: str) -> dict | None:
    """发送文本消息给个人。"""
    return _send_message(open_id, msg_type="text", content=json.dumps({"text": text}))


def send_card_message(open_id: str, card: dict) -> dict | None:
    """发送交互式卡片消息给个人。card 为飞书 Card JSON 结构。"""
    return _send_message(open_id, msg_type="interactive", content=json.dumps(card))


def send_file_message(open_id: str, file_path: str) -> dict | None:
    """上传文件并发送文件消息给个人。"""
    file_key = _upload_file(file_path)
    if not file_key:
        return None

    ext = Path(file_path).suffix.lstrip(".")
    msg_content = {"file_key": file_key}
    return _send_message(open_id, msg_type="file", content=json.dumps(msg_content))


def _send_message(open_id: str, msg_type: str, content: str) -> dict | None:
    """底层消息发送。"""
    try:
        resp = requests.post(
            f"{BASE_URL}/im/v1/messages?receive_id_type=open_id",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"receive_id": open_id, "msg_type": msg_type, "content": content},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            print(f"[ERROR] 发送消息失败: {data.get('msg')}")
            return None
        return data.get("data")
    except requests.RequestException as e:
        print(f"[ERROR] 发送消息异常: {e}")
        return None


def _upload_file(file_path: str) -> str | None:
    """上传文件到飞书，返回 file_key。"""
    ext_map = {
        "xlsx": "xlsx", "xls": "xlsx", "pdf": "pdf", "docx": "docx",
        "doc": "docx", "pptx": "pptx", "png": "png", "jpg": "png",
        "jpeg": "png", "zip": "stream", "mp4": "stream", "csv": "csv",
    }
    ext = Path(file_path).suffix.lstrip(".").lower()
    file_type = ext_map.get(ext, "stream")

    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{BASE_URL}/im/v1/files",
                headers=_auth_headers(),
                files={"file": (Path(file_path).name, f)},
                data={"file_type": file_type, "file_name": Path(file_path).name},
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            print(f"[ERROR] 上传文件失败: {data.get('msg')}")
            return None
        return data.get("data", {}).get("file_key")
    except requests.RequestException as e:
        print(f"[ERROR] 上传文件异常: {e}")
        return None


# ── 批量发送（带限速） ──────────────────────────────────────

def send_batch_text(open_ids: list[str], text: str, interval: float = 0.3) -> list[dict]:
    """批量发送文本消息，自动限速。"""
    results = []
    for oid in open_ids:
        result = send_text_message(oid, text)
        results.append({"open_id": oid, "result": result, "success": result is not None})
        time.sleep(interval)
    return results


def send_batch_card(open_ids: list[str], card: dict, interval: float = 0.3) -> list[dict]:
    """批量发送卡片消息，自动限速。"""
    results = []
    for oid in open_ids:
        result = send_card_message(oid, card)
        results.append({"open_id": oid, "result": result, "success": result is not None})
        time.sleep(interval)
    return results


# ── Card 模板 ───────────────────────────────────────────────

def build_store_report_card(store_name: str, data: dict) -> dict:
    """构建门店每日报表卡片。"""

    # ── 前置：告警卡片构建函数 ──────────────────────────────

LEVEL_HEADER_TEMPLATE = {
    "yellow": "turquoise",
    "orange": "orange",
    "red": "red",
}

def build_alert_card(alert: dict) -> dict:
    """构建单条超期告警卡片，根据级别设置不同颜色。"""
    level = alert.get("level", {})
    icon = level.get("icon", "⚠️")
    label = level.get("label", "超期")
    header_color = LEVEL_HEADER_TEMPLATE.get(level.get("color", "yellow"), "turquoise")

    alert_count = alert.get("alert_count", 1)
    first_alert_date = alert.get("first_alert_date", "")

    # 非首次提醒时追加提醒次数信息
    reminder_note = ""
    if alert_count > 1 and first_alert_date:
        reminder_note = f"\n这是第{alert_count}次提醒，首次提醒时间：{first_alert_date}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{icon} {label} — 事故车超期提醒"},
            "template": header_color,
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**门店**: {alert.get('store_name', '')}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**车型**: {alert.get('vehicle_model', '')}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**进店时间**: {alert.get('entry_date', '')}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**已进店**: {alert.get('days_in_shop', 0)} 天"}},
                ]
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**VIN**: {alert.get('vin', '')}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**当前节点**: {alert.get('current_stage', '')}"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"请尽快推进维修！{reminder_note}"}
            },
        ],
    }


def build_store_report_card(store_name: str, data: dict) -> dict:
    """构建门店每日报表卡片。"""
    overdue_rows = ""
    for v in data.get("overdue_vehicles", []):
        level = v.get("level")
        icon = level.get("icon", "⚠️") if level else "⚠️"
        overdue_rows += f"\n{icon} VIN: {v['vin'][-8:]} | {v['days_in_shop']}天 | 节点: {v['current_stage']}"

    date_range = data.get("date_range", "")
    title_suffix = f"（{date_range}）" if date_range else ""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📋 {store_name} — 事故维修未完工日报{title_suffix}"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**7~10天未完工**: {data.get('count_7d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**10~14天未完工**: {data.get('count_10d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**≥14天未完工**: {data.get('count_14d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**在修总数**: {data.get('total', 0)} 台"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**超期车辆明细**：{overdue_rows if overdue_rows else '无'}"}
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"数据截止: {data.get('snapshot_date', '')} | 详细报表见附件 Excel"}]
            }
        ]
    }


def build_region_report_card(region_name: str, data: dict) -> dict:
    """构建区域每日报表卡片。"""
    store_rows = ""
    for s in data.get("stores_summary", []):
        store_rows += f"\n- {s['store_name']}: 7~10天 {s['count_7d']}台 / 10~14天 {s['count_10d']}台 / ≥14天 {s['count_14d']}台"

    date_range = data.get("date_range", "")
    title_suffix = f"（{date_range}）" if date_range else ""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 {region_name} — 事故维修超期区域日报{title_suffix}"},
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**门店总数**: {data.get('store_count', 0)}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**在修车辆**: {data.get('total_vehicles', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**7天未完工**: {data.get('count_7d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**10天未完工**: {data.get('count_10d', 0)} 台"}},
                ]
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**14天未完工**: {data.get('count_14d', 0)} 台"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**各门店情况**：{store_rows if store_rows else '无超期门店'}"}
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"数据截止: {data.get('snapshot_date', '')} | 详细报表见附件 Excel"}]
            }
        ]
    }


def build_national_summary_card(data: dict) -> dict:
    """构建全国每日报表摘要卡片（配合 Excel 文件发送）。"""
    region_rows = ""
    for r in data.get("regions", []):
        region_rows += f"\n- {r['region']}: 7~10天 {r['count_7d']}台 / 10~14天 {r['count_10d']}台 / ≥14天 {r['count_14d']}台"

    date_range = data.get("date_range", "")
    title_suffix = f"（{date_range}）" if date_range else ""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🏢 全国事故维修超期日报{title_suffix}"},
            "template": "red"
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**全国在修**: {data.get('total_vehicles', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**7~10天未完工**: {data.get('count_7d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**10~14天未完工**: {data.get('count_10d', 0)} 台"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**≥14天未完工**: {data.get('count_14d', 0)} 台"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**各区域汇总**：{region_rows}"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**7天完工率**: {data.get('kpi_7d_rate', '-')}\n**10天完工率**: {data.get('kpi_10d_rate', '-')}"}
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"数据截止: {data.get('snapshot_date', '')} | 详细报表见附件 Excel"}]
            }
        ]
    }


if __name__ == "__main__":
    # 快速验证：向管理员发送测试消息
    admin_mobile = os.getenv("ADMIN_MOBILE", "")
    if admin_mobile:
        oid = resolve_phone_to_open_id(admin_mobile)
        if oid:
            print(f"管理员 open_id: {oid}")
            result = send_text_message(oid, "✅ 事故车提醒系统飞书客户端测试成功")
            print(f"发送结果: {result}")
        else:
            print(f"无法解析管理员手机号: {admin_mobile}")
