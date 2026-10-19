"""规则引擎门店清单、区域与考核范围测试（离线）。"""

import json

import openpyxl
import pytest

import rule_engine
from bitable_sync import HEADERS


FAKE_RULES = [{
    "id": 1,
    "name": "事故车超期告警",
    "msg_type": "text",
    "levels": [
        {"threshold": 7, "label": "7天", "icon": "🟡"},
        {"threshold": 10, "label": "10天", "icon": "🟠"},
        {"threshold": 14, "label": "14天", "icon": "🔴"},
    ],
}]


@pytest.fixture
def engine_env(tmp_path, monkeypatch):
    """隔离 list.xlsx、config 和告警历史路径，避免读写仓库真实文件。"""
    list_path = tmp_path / "list.xlsx"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "stores.json").write_text(
        json.dumps({"national_recipients": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    followup_dir = tmp_path / "followup"
    monkeypatch.setattr(rule_engine, "LIST_XLSX_PATH", list_path)
    monkeypatch.setattr(rule_engine, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(rule_engine, "FOLLOWUP_DIR", followup_dir)
    monkeypatch.setattr(rule_engine, "ALERT_HISTORY_PATH", followup_dir / "alert_history.json")
    monkeypatch.setattr(rule_engine, "_missing_manager_logged", set())
    return tmp_path


@pytest.fixture
def fake_rules(monkeypatch):
    monkeypatch.setattr(rule_engine, "load_rules_config", lambda: FAKE_RULES)


def _write_list(path, rows, headers=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "门店联系人"
    ws.append(headers or HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(str(path))


def _list_row(store_code="D001", region="华中 1区", manager="负责人甲", manager_open_id="ou_m1"):
    return [
        "1", store_code, region, "武汉测试店",
        "督导甲", "13800000000", "ou_sup",
        manager, "13800000001", manager_open_id,
        "湖北省", "武汉市", "洪山区",
    ]


def _record(store_code, vin, days, region, *, completed=False):
    return {
        "store_code": store_code,
        "store_name": f"{store_code}门店",
        "vin": vin,
        "vehicle_model": "猛士917",
        "entry_date": "2026-10-01",
        "days_in_shop": days,
        "region": region,
        "current_stage": "维修完成" if completed else "维修中",
        "is_qc_completed": completed,
    }


def test_load_stores_config_reads_all_manager_slots(engine_env):
    _write_list(engine_env / "list.xlsx", [[
        "1", "D001", "华中 1区", "武汉测试店",
        "督导甲", "13800000000", "ou_sup",
        "负责人甲", "13800000001", "ou_m1",
        "湖北省", "武汉市", "洪山区",
        "负责人乙", "13800000002", "ou_m2",
        "", "", "",
        "负责人丁", "13800000004", "ou_m4",
    ]])

    config = rule_engine.load_stores_config()
    store = config["stores"]["D001"]

    assert store["service_manager"]["name"] == "负责人甲"
    assert [m["name"] for m in store["service_managers"]] == ["负责人甲", "负责人乙", "负责人丁"]
    # 飞书区域只用于联系人表，键按同一规则归一化。
    assert "华中1" in config["regions"]
    assert config["regions"]["华中1"]["supervisor"]["open_id"] == "ou_sup"


def test_store_manager_targets_returns_all_reachable_deduped_managers():
    config = {
        "stores": {
            "D001": {
                "service_manager": {"name": "甲", "phone": "1", "open_id": "ou_1"},
                "service_managers": [
                    {"name": "甲", "phone": "1", "open_id": "ou_1"},
                    {"name": "乙", "phone": "2", "open_id": ""},
                    {"name": "丙", "phone": "3", "open_id": "ou_1"},
                    {"name": "丁", "phone": "", "open_id": ""},
                ],
            },
        },
        "regions": {},
        "national_recipients": {},
    }

    targets = rule_engine._store_manager_targets(config, "D001")

    assert [t["name"] for t in targets] == ["甲", "乙"]
    assert [t["open_id"] for t in targets] == ["ou_1", ""]


def test_directory_region_uses_normalized_dms_region_only():
    config = {"stores": {"D001": {"region": "华南区"}}, "regions": {}}

    assert rule_engine._directory_region({"store_code": "D001", "region": " 华中1区 "}, config) == "华中1"
    assert rule_engine._directory_region({"store_code": "D001", "region": "华北区"}, config) == "华北"
    assert rule_engine._directory_region({"store_code": "D001", "region": ""}, config) == "无区域"
    assert rule_engine._normalize_region(" 西北 区 ") == "西北"


def test_region_recipient_lookup_normalizes_dms_key():
    supervisor = {"name": "督导甲", "phone": "13800000000", "open_id": "ou_sup"}
    national = {"name": "全国甲", "phone": "13900000000", "open_id": "ou_nat"}
    config = {
        "stores": {},
        "regions": {"华中1": {"name": "华中 1区", "supervisor": supervisor}},
        "national_recipients": {"全国甲": "13900000000"},
    }

    recipients = rule_engine._get_region_recipient_phones("华中1区", config)
    assert [p["name"] for p in recipients] == ["督导甲", "全国甲"]

    national_recipients = rule_engine._get_national_recipient_phones(config)
    assert [p["name"] for p in national_recipients] == ["督导甲", "全国甲"]


def test_out_of_list_records_are_ignored_everywhere(engine_env, fake_rules):
    _write_list(engine_env / "list.xlsx", [
        _list_row(region="华中 1区"),
    ])
    records = [
        _record("D001", "VIN001", 8, "华中1区"),
        _record("D001", "VIN002", 8, "华中1区"),
        _record("D001", "VIN003", 5, "华北区", completed=True),
        _record("D999", "VIN999", 20, "华东区"),
    ]

    results = rule_engine.evaluate_rules({
        "snapshot_date": "2026-10-19",
        "snapshot_start": "2026-09-19",
        "records": records,
    })

    assert results["ignored_not_in_list"] == {"records": 1, "stores": ["D999"]}
    assert all(alert["store_code"] == "D001" for alert in results["alerts"])
    assert [alert["vin"] for alert in results["alerts"]] == ["VIN001", "VIN002"]
    assert set(results["store_reports"]) == {"D001"}
    assert results["store_reports"]["D001"]["region"] == "华中1"
    assert set(results["region_reports"]) == {"华中1"}
    assert results["region_reports"]["华中1"]["total_vehicles"] == 2
    assert results["region_reports"]["华中1"]["recipient_phones"][0]["open_id"] == "ou_sup"
    assert results["national_report"]["total_vehicles"] == 2
    assert results["national_report"]["total_accident_orders_30d"] == 3
    assert results["national_report"]["completed_7d"] == 1
    assert results["national_report"]["kpi_7d_rate"] == "33.3%"
    assert [r["region"] for r in results["national_report"]["regions"]] == ["华中1"]


def test_store_report_region_uses_most_frequent_dms_region(engine_env, fake_rules):
    _write_list(engine_env / "list.xlsx", [
        _list_row(region="华南区"),
    ])
    records = [
        _record("D001", "VIN001", 8, "华中1区"),
        _record("D001", "VIN002", 9, "华中1区"),
        _record("D001", "VIN003", 5, "华北区", completed=True),
    ]

    results = rule_engine.evaluate_rules({"records": records})

    assert results["store_reports"]["D001"]["region"] == "华中1"
    assert set(results["region_reports"]) == {"华中1"}
    assert results["region_reports"]["华中1"]["total_vehicles"] == 2


@pytest.mark.parametrize("create_empty_file", [False, True])
def test_empty_or_missing_store_list_does_not_filter(engine_env, fake_rules, capsys, create_empty_file):
    if create_empty_file:
        _write_list(engine_env / "list.xlsx", [])
    records = [
        _record("D001", "VIN001", 8, "华中1区"),
        _record("D002", "VIN002", 12, "华北区"),
    ]

    results = rule_engine.evaluate_rules({"records": records})

    assert results["ignored_not_in_list"] == {"records": 0, "stores": []}
    assert set(results["store_reports"]) == {"D001", "D002"}
    assert {alert["store_code"] for alert in results["alerts"]} == {"D001", "D002"}
    assert results["national_report"]["total_vehicles"] == 2
    assert "跳过参与考核范围过滤" in capsys.readouterr().out


def test_load_stores_config_dedupes_repeated_primary_and_numbers(engine_env):
    _write_list(engine_env / "list.xlsx", [[
        "1", "D001", "华中1区", "武汉测试店",
        "督导甲", "13800000000", "ou_sup",
        "负责人甲", "13800000001", "ou_m1",
        "湖北省", "武汉市", "洪山区",
        "负责人甲", "13800000001", "ou_m1",
        "负责人乙", "13800000002", "ou_m2",
        "", "", "",
    ]])

    config = rule_engine.load_stores_config()
    store = config["stores"]["D001"]

    assert store["service_manager"]["name"] == "负责人甲"
    assert [m["name"] for m in store["service_managers"]] == ["负责人甲", "负责人乙"]


def test_mixed_region_store_rows_and_store_region_from_all_records(tmp_path, monkeypatch):
    import rule_engine as re_
    cfg = {"stores": {"S1": {"name": "店1", "region": "华南区", "service_managers": [], "service_manager": {}, "supervisor": {}}},
           "regions": {}, "national_recipients": {}}
    monkeypatch.setattr(re_, "_save_alert_history", lambda h: None)
    monkeypatch.setattr(re_, "_load_alert_history", lambda: {}, raising=False)
    recs = [
        {"store_code": "S1", "vin": "A", "region": "华中1区", "days_in_shop": 8},
        {"store_code": "S1", "vin": "B", "region": "华中1区", "days_in_shop": 3},
        {"store_code": "S1", "vin": "C", "region": "华北区", "days_in_shop": 12},
        {"store_code": "S1", "vin": "D", "region": "华北区", "days_in_shop": 1, "is_qc_completed": True},
        {"store_code": "S1", "vin": "E", "region": "华北区", "days_in_shop": 1, "is_qc_completed": True},
    ]
    res = re_.evaluate_rules({"records": recs, "snapshot_date": "2026-09-12", "snapshot_start": "2026-08-13"}, cfg)
    assert res["store_reports"]["S1"]["region"] == "华北"
    assert res["region_reports"]["华中1"]["stores_summary"][0]["total"] == 2
    assert res["region_reports"]["华北"]["stores_summary"][0]["total"] == 1
