"""规则引擎 — 评估提醒规则，产出通知列表。

规则 1: 事故车超期分级告警（≥7天黄色/≥10天橙色/≥14天红色，每日持续跟踪）
规则 2: 已作废工单不计入告警和统计
规则 3: 门店每日报表
规则 4: 区域每日报表
规则 5: 全国每日报表+KPI
"""

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import openpyxl

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FOLLOWUP_DIR = Path(__file__).resolve().parent.parent / "data" / "followup"
ALERT_HISTORY_PATH = FOLLOWUP_DIR / "alert_history.json"
LIST_XLSX_PATH = Path(__file__).resolve().parent.parent / "list.xlsx"


def _load_alert_history() -> dict:
    """加载告警历史（VIN → {first_alert_date, alert_count}）。"""
    if ALERT_HISTORY_PATH.exists():
        with open(ALERT_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_alert_history(history: dict):
    """保存告警历史。"""
    FOLLOWUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALERT_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _cell(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _normalize_region(value) -> str:
    """区域名归一化：去空白，长度大于1时去掉一个末尾「区」。"""
    text = "".join(str(value or "").split())
    if not text:
        return ""
    if len(text) > 1 and text.endswith("区"):
        text = text[:-1]
    return text


def _person(name: str, phone: str, open_id: str = "") -> dict:
    return {"name": name.strip(), "phone": phone.strip(), "open_id": open_id.strip()}


def _person_reachable(person: dict | None) -> bool:
    if not person:
        return False
    return bool(str(person.get("open_id") or "").strip() or str(person.get("phone") or "").strip())


def load_stores_config() -> dict:
    """从 list.xlsx 读取门店→区域→督导/售后负责人。

    列：门店编码、区域、门店名称、督导姓名、督导飞书电话号、督导open_id、
    售后负责人、售后电话、售后open_id 及 2~4 号售后负责人列。省市区只缓存。
    全国收件人仍从 config/stores.json 读取。
    """
    if not LIST_XLSX_PATH.exists():
        return {"stores": {}, "regions": {}, "national_recipients": _load_national_recipients()}

    wb = openpyxl.load_workbook(str(LIST_XLSX_PATH), data_only=True)
    ws = wb[wb.sheetnames[0]]
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None) or ()
    headers = [_cell(cell) for cell in header_row]
    index = {name: i for i, name in enumerate(headers) if name}

    def col(row: tuple, *names: str) -> str:
        for name in names:
            pos = index.get(name)
            if pos is None or pos >= len(row):
                continue
            text = _cell(row[pos])
            if text:
                return text
        return ""

    stores = {}
    regions = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        store_code = col(row, "门店编码", "店代码")
        if not store_code:
            continue

        region_name = col(row, "区域")
        store_name = col(row, "门店名称", "店名称")
        supervisor = _person(
            col(row, "督导姓名", "督导"),
            col(row, "督导飞书电话号", "督导手机号"),
            col(row, "督导open_id"),
        )
        # 旧表没有售后负责人列时，退回提醒人1，避免未同步前完全发不出去。
        service_managers = []
        for slot in range(1, 5):
            if slot == 1:
                manager = _person(
                    col(row, "售后负责人", "提醒人1"),
                    col(row, "售后电话", "电话1"),
                    col(row, "售后open_id"),
                )
            else:
                manager = _person(
                    col(row, f"售后负责人{slot}", f"提醒人{slot}"),
                    col(row, f"售后电话{slot}", f"电话{slot}"),
                    col(row, f"售后open_id{slot}"),
                )
            if manager["name"] or manager["phone"] or manager["open_id"]:
                service_managers.append(manager)

        # 同一人可能因主列 + 编号列重复出现，按 open_id（否则姓名）去重。
        unique_managers = []
        seen_managers: dict = {}
        for manager in service_managers:
            key = manager["open_id"] or manager["name"]
            if key and key in seen_managers:
                kept = seen_managers[key]
                kept["phone"] = kept["phone"] or manager["phone"]
                kept["open_id"] = kept["open_id"] or manager["open_id"]
                continue
            if key:
                seen_managers[key] = manager
            unique_managers.append(manager)
        service_managers = unique_managers
        service_manager = service_managers[0] if service_managers else _person("", "")

        stores[store_code] = {
            "name": store_name,
            "region": region_name,
            "province": col(row, "门店省"),
            "city": col(row, "门店市"),
            "district": col(row, "门店区"),
            "service_manager": service_manager,
            "service_managers": service_managers,
            "supervisor": supervisor,
        }

        region_key = _normalize_region(region_name)
        if region_key and _person_reachable(supervisor):
            regions[region_key] = {"name": region_name, "supervisor": supervisor}

    return {
        "stores": stores,
        "regions": regions,
        "national_recipients": _load_national_recipients(),
    }


def _load_national_recipients() -> dict:
    stores_json_path = CONFIG_DIR / "stores.json"
    if not stores_json_path.exists():
        return {}
    with open(stores_json_path, "r", encoding="utf-8") as f:
        legacy = json.load(f)
    nr = legacy.get("national_recipients", {})
    return nr if isinstance(nr, dict) else {}


def load_rules_config() -> list[dict]:
    path = CONFIG_DIR / "rules.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["rules"]


def _as_target(person: dict | None) -> dict | None:
    if not isinstance(person, dict):
        return None
    name = str(person.get("name") or "").strip()
    phone = str(person.get("phone") or "").strip()
    open_id = str(person.get("open_id") or "").strip()
    if not phone and not open_id:
        return None
    return {"name": name, "phone": phone, "open_id": open_id}


def _dedupe_targets(people: list) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in people:
        if isinstance(item, str):
            phone = item.strip()
            target = {"name": "", "phone": phone, "open_id": ""} if phone else None
        else:
            target = _as_target(item) if isinstance(item, dict) else None
        if not target:
            continue
        key = target["open_id"] or target["phone"] or target["name"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(target)
    return out


def _national_targets(store_config: dict) -> list[dict]:
    """控制台配置的全国收件人。值为手机号字符串，或 {phone, open_id}。"""
    nr = store_config.get("national_recipients", {})
    people: list[dict] = []
    if isinstance(nr, dict):
        for name, value in nr.items():
            if isinstance(value, dict):
                people.append({
                    "name": str(name),
                    "phone": str(value.get("phone") or "").strip(),
                    "open_id": str(value.get("open_id") or "").strip(),
                })
            else:
                phone = str(value or "").strip()
                if phone:
                    people.append({"name": str(name), "phone": phone, "open_id": ""})
    return _dedupe_targets(people)


_missing_manager_logged: set[str] = set()


def _store_manager_targets(store_config: dict, store_code: str) -> list[dict]:
    """门店告警和门店报表发给全部可达的售后业务负责人。"""
    store_info = store_config.get("stores", {}).get(store_code, {})
    managers = store_info.get("service_managers")
    people = list(managers) if isinstance(managers, list) else []
    # 兼容只有单个 service_manager 的旧配置/旧表。
    people.append(store_info.get("service_manager"))
    targets = _dedupe_targets([person for person in people if _person_reachable(person)])
    if not targets:
        if store_code not in _missing_manager_logged:
            _missing_manager_logged.add(store_code)
            print(f"[rule] 门店 {store_code} 无售后负责人，跳过门店消息")
        return []
    return targets


def _find_region_info(region_name: str, store_config: dict) -> dict:
    """按归一化区域名查找区域督导，兼容旧配置里的原始区域名。"""
    regions = store_config.get("regions", {})
    if not isinstance(regions, dict):
        return {}
    key = _normalize_region(region_name)
    region_info = regions.get(key) if key else None
    if region_info is None:
        region_info = regions.get(str(region_name or "").strip())
    if region_info is None and key:
        for candidate, info in regions.items():
            if _normalize_region(candidate) == key:
                region_info = info
                break
    return region_info if isinstance(region_info, dict) else {}


def _get_region_recipient_phones(region_name: str, store_config: dict) -> list[dict]:
    """区域报表：该区域督导 + 本系统全国收件人。"""
    region_info = _find_region_info(region_name, store_config)
    people = []
    sup = _as_target(region_info.get("supervisor"))
    if sup:
        people.append(sup)
    people.extend(_national_targets(store_config))
    return _dedupe_targets(people)


def _get_national_recipient_phones(store_config: dict) -> list[dict]:
    """全国报表：全部区域督导 + 本系统全国收件人。"""
    people = []
    regions = store_config.get("regions", {})
    if not isinstance(regions, dict):
        regions = {}
    for region_info in regions.values():
        if not isinstance(region_info, dict):
            continue
        sup = _as_target(region_info.get("supervisor"))
        if sup:
            people.append(sup)
    people.extend(_national_targets(store_config))
    return _dedupe_targets(people)


def _directory_region(rec: dict, stores_config: dict) -> str:
    """区域只取 DMS 大区并归一化，飞书区域不覆盖 DMS 归属。"""
    return _normalize_region(rec.get("region")) or "无区域"


def _determine_alert_level(days: int, levels: list[dict]) -> dict | None:
    """根据进店天数确定告警级别，取满足的最高阈值。"""
    for lv in reversed(levels):  # 从14→10→7检查
        if days >= lv["threshold"]:
            return lv
    return None


def _format_level_message(level: dict, rec: dict) -> str:
    """格式化分级告警文本消息。"""
    return (
        f"{level['icon']} {level['label']}\n"
        f"门店：{rec.get('store_name', '')}\n"
        f"VIN：{rec.get('vin', '')}\n"
        f"车型：{rec.get('vehicle_model', '')}\n"
        f"进店时间：{rec.get('entry_date', '')}\n"
        f"已进店 {rec.get('days_in_shop', 0)} 天未完工\n"
        f"当前节点：{rec.get('current_stage', '')}"
    )


def _filter_assessed_records(records: list[dict], stores_config: dict) -> tuple[list[dict], dict]:
    """只保留飞书同步清单内门店的工单；清单为空时不过滤。"""
    configured_stores = stores_config.get("stores") or {}
    known_codes = {_cell(code) for code in configured_stores if _cell(code)}
    ignored = {"records": 0, "stores": []}
    if not known_codes:
        print("[rule] 警告: 门店清单为空或不可用，跳过参与考核范围过滤")
        return records, ignored

    kept: list[dict] = []
    ignored_codes: set[str] = set()
    for rec in records:
        store_code = _cell(rec.get("store_code"))
        if store_code in known_codes:
            kept.append(rec)
            continue
        ignored["records"] += 1
        if store_code and store_code not in ignored_codes:
            ignored_codes.add(store_code)
            ignored["stores"].append(store_code)
    return kept, ignored


def evaluate_rules(repair_orders: dict, stores_config: dict | None = None) -> dict:
    """评估所有规则，返回通知任务。"""
    if stores_config is None:
        stores_config = load_stores_config()

    rules = load_rules_config()
    records, ignored_not_in_list = _filter_assessed_records(repair_orders.get("records", []), stores_config)
    snapshot_date = repair_orders.get("snapshot_date", date.today().strftime("%Y-%m-%d"))
    snapshot_start = repair_orders.get("snapshot_start", (date.today() - timedelta(days=30)).strftime("%Y-%m-%d"))
    date_range = f"{snapshot_start} ~ {snapshot_date}"

    results = {
        "snapshot_date": snapshot_date,
        "date_range": date_range,
        "alerts": [],
        "store_reports": {},
        "region_reports": {},
        "national_report": {},
        "ignored_not_in_list": ignored_not_in_list,
    }

    # ── Rule 1: 分级告警（≥7天未完工，每日跟踪）───────────────
    alert_rule = None
    for rule in rules:
        if rule["id"] == 1:
            alert_rule = rule
            break

    alert_history = _load_alert_history()
    levels: list[dict] = []

    if alert_rule:
        levels = alert_rule.get("levels", [])

        for rec in records:
            days = rec.get("days_in_shop", -1)
            if days < 7 or rec.get("is_qc_completed") or rec.get("current_stage") == "已作废":
                continue

            level = _determine_alert_level(days, levels)
            if level is None:
                continue

            vin = rec.get("vin", "")
            store_code = rec.get("store_code", "")
            phones = _store_manager_targets(stores_config, store_code)
            directory_region = _directory_region(rec, stores_config)

            # 告警历史追踪
            hist_entry = alert_history.get(vin)
            if hist_entry:
                alert_count = hist_entry["alert_count"] + 1
                first_alert_date = hist_entry["first_alert_date"]
            else:
                alert_count = 1
                first_alert_date = snapshot_date
            alert_history[vin] = {"first_alert_date": first_alert_date, "alert_count": alert_count}

            msg = _format_level_message(level, rec)

            results["alerts"].append({
                "rule_id": 1,
                "rule_name": alert_rule["name"],
                "level": level,
                "store_code": store_code,
                "store_name": rec.get("store_name", ""),
                "region": directory_region,
                "vin": vin,
                "vehicle_model": rec.get("vehicle_model", ""),
                "days_in_shop": days,
                "entry_date": rec.get("entry_date", ""),
                "current_stage": rec.get("current_stage", ""),
                "recipient_phones": phones,
                "msg_type": alert_rule["msg_type"],
                "message": msg,
                "alert_count": alert_count,
                "first_alert_date": first_alert_date,
            })

    _save_alert_history(alert_history)

    # ── Rule 3, 4, 5: 汇总报表 ────────────────────────────
    # records 已在入口按飞书参与考核门店清单过滤。
    active_records = [r for r in records if not r.get("is_qc_completed") and r.get("current_stage") != "已作废"]

    # 按门店分组
    store_groups = defaultdict(list)
    for rec in active_records:
        store_groups[rec.get("store_code", "UNKNOWN")].append(rec)

    # 门店区域：该店全部（已过滤）工单里最常见的归一化 DMS 大区（含已完工记录）
    store_region_votes: dict = defaultdict(Counter)
    for rec in records:
        store_region_votes[rec.get("store_code", "UNKNOWN")][_directory_region(rec, stores_config)] += 1

    # 按 DMS 大区（归一化）分组，飞书区域只用于找该区域督导
    region_groups = defaultdict(list)
    for rec in active_records:
        region_groups[_directory_region(rec, stores_config)].append(rec)

    # Rule 3: 门店报表
    for store_code, vehicles in store_groups.items():
        store_info = stores_config.get("stores", {}).get(store_code, {})
        count_7d = sum(1 for v in vehicles if 7 <= v.get("days_in_shop", 0) < 10)
        count_10d = sum(1 for v in vehicles if 10 <= v.get("days_in_shop", 0) < 14)
        count_14d = sum(1 for v in vehicles if v.get("days_in_shop", 0) >= 14)
        votes = store_region_votes.get(store_code) or Counter(_directory_region(v, stores_config) for v in vehicles)
        store_region = votes.most_common(1)[0][0]

        overdue_vehicles = [
            {
                "vin": v.get("vin", ""),
                "days_in_shop": v.get("days_in_shop", 0),
                "current_stage": v.get("current_stage", ""),
                "entry_date": v.get("entry_date", ""),
                "vehicle_model": v.get("vehicle_model", ""),
                "level": _determine_alert_level(v.get("days_in_shop", 0), levels) if levels else None,
            }
            for v in vehicles if v.get("days_in_shop", 0) >= 7
        ]

        results["store_reports"][store_code] = {
            "store_name": store_info.get("name", vehicles[0].get("store_name", "")),
            "region": store_region,
            "total": len(vehicles),
            "count_7d": count_7d,
            "count_10d": count_10d,
            "count_14d": count_14d,
            "overdue_vehicles": overdue_vehicles,
            "snapshot_date": snapshot_date,
            "date_range": date_range,
            "recipient_phones": _store_manager_targets(stores_config, store_code),
        }

    # Rule 4: 区域报表
    for region_name, vehicles in region_groups.items():
        stores_summary = []
        by_store: dict = defaultdict(list)
        for v in vehicles:  # 门店行只统计本区域内的记录（一家店的工单可能分属多个 DMS 大区）
            by_store[v.get("store_code", "")].append(v)
        seen_stores = set(by_store)
        for sc, sv in by_store.items():
            sr = results["store_reports"].get(sc, {})
            stores_summary.append({
                "store_name": sr.get("store_name", sv[0].get("store_name", "")),
                "total": len(sv),
                "count_7d": sum(1 for v in sv if 7 <= v.get("days_in_shop", 0) < 10),
                "count_10d": sum(1 for v in sv if 10 <= v.get("days_in_shop", 0) < 14),
                "count_14d": sum(1 for v in sv if v.get("days_in_shop", 0) >= 14),
            })

        results["region_reports"][region_name] = {
            "store_count": len(seen_stores),
            "total_vehicles": len(vehicles),
            "count_7d": sum(1 for v in vehicles if 7 <= v.get("days_in_shop", 0) < 10),
            "count_10d": sum(1 for v in vehicles if 10 <= v.get("days_in_shop", 0) < 14),
            "count_14d": sum(1 for v in vehicles if v.get("days_in_shop", 0) >= 14),
            "stores_summary": stores_summary,
            "snapshot_date": snapshot_date,
            "date_range": date_range,
            "recipient_phones": _get_region_recipient_phones(region_name, stores_config),
        }

    # Rule 5: 全国报表
    count_7d = sum(1 for v in active_records if 7 <= v.get("days_in_shop", 0) < 10)
    count_10d = sum(1 for v in active_records if 10 <= v.get("days_in_shop", 0) < 14)
    count_14d = sum(1 for v in active_records if v.get("days_in_shop", 0) >= 14)
    total = len(active_records)

    total_accident_orders_30d = len(records)
    completed = [r for r in records if r.get("is_qc_completed")]
    completed_7d = sum(1 for r in completed if r.get("days_in_shop", 0) <= 7)
    completed_10d = sum(1 for r in completed if r.get("days_in_shop", 0) <= 10)

    # KPI口径：
    # 30天内事故工单7天完工率 = 7天内完工事故车辆数 / 全部事故工单数 * 100%
    # 30天内事故工单10天完工率 = 10天内完工事故车辆数 / 全部事故工单数 * 100%
    denominator = max(total_accident_orders_30d, 1)
    kpi_7d_rate = f"{completed_7d / denominator * 100:.1f}%"
    kpi_10d_rate = f"{completed_10d / denominator * 100:.1f}%"

    regions_summary = []
    for rn, rv in results["region_reports"].items():
        regions_summary.append({
            "region": rn,
            "count_7d": rv["count_7d"],
            "count_10d": rv["count_10d"],
            "count_14d": rv["count_14d"],
        })

    results["national_report"] = {
        "total_vehicles": total,
        "count_7d": count_7d,
        "count_10d": count_10d,
        "count_14d": count_14d,
        "kpi_7d_rate": kpi_7d_rate,
        "kpi_10d_rate": kpi_10d_rate,
        "total_accident_orders_30d": total_accident_orders_30d,
        "completed_7d": completed_7d,
        "completed_10d": completed_10d,
        "regions": regions_summary,
        "snapshot_date": snapshot_date,
        "date_range": date_range,
        "recipient_phones": _get_national_recipient_phones(stores_config),
    }

    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python rule_engine.py <repair_orders.json>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)

    result = evaluate_rules(data)
    print(json.dumps({
        "alert_count": len(result["alerts"]),
        "store_report_count": len(result["store_reports"]),
        "region_report_count": len(result["region_reports"]),
        "national_summary": {
            "total": result["national_report"]["total_vehicles"],
            "7d": result["national_report"]["count_7d"],
            "10d": result["national_report"]["count_10d"],
            "14d": result["national_report"]["count_14d"],
            "kpi_7d": result["national_report"]["kpi_7d_rate"],
            "kpi_10d": result["national_report"]["kpi_10d_rate"],
            "date_range": result["national_report"]["date_range"],
        }
    }, ensure_ascii=False, indent=2))
