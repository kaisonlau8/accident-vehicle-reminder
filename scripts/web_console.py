"""Web 控制台 — 事故车提醒系统管理界面（端口 9000）。

提供：仪表盘、模式切换、规则管理、手动触发、数据/报表/联系人查看。
"""

import json
import os
import subprocess
import sys
import threading
import time
import io
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_from_directory, send_file,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_excel import import_excel
from rule_engine import evaluate_rules, load_stores_config, load_rules_config
from report_generator import (
    generate_national_report, generate_all_region_reports, generate_all_store_reports,
)
from message_dispatcher import (
    dispatch_alerts, dispatch_store_reports,
    dispatch_region_reports, dispatch_national_report,
)
from dfmc_browser_utils import (
    DEFAULT_BROWSER_CANDIDATES, detect_browser, find_free_port,
    write_browser_state, read_browser_state, process_is_running,
)
from time_utils import beijing_now, beijing_strftime

_DEFAULT_TEST_PHONE = os.getenv("ADMIN_MOBILE", "").replace("+86-", "").replace("+86", "")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PLUGIN_ROOT / "config"
DATA_DIR = PLUGIN_ROOT / "data"
FOLLOWUP_DIR = DATA_DIR / "followup"
REPAIR_ORDERS_DIR = DATA_DIR / "repair_orders"
REPORTS_DIR = DATA_DIR / "reports"
OUTPUT_DIR = PLUGIN_ROOT / "output"
RUNTIME_DIR = PLUGIN_ROOT / ".runtime"
LIST_XLSX = PLUGIN_ROOT / "list.xlsx"
ALERT_HISTORY_PATH = FOLLOWUP_DIR / "alert_history.json"

app = Flask(
    __name__,
    template_folder=str(PLUGIN_ROOT / "templates"),
    static_folder=str(PLUGIN_ROOT / "static"),
)

# ── 全局状态 ──────────────────────────────────────────────
task_status = {
    "running": False,
    "task": "",
    "started_at": "",
    "log_lines": [],
    "result": None,
}

scheduler_state = {
    "mode": "idle",       # idle / test / prod / scheduled
    "started_at": None,
    "scheduler_thread": None,
    "scheduler_stop": False,  # 用于停止定时调度线程
    "next_fire": "",          # 下一次触发时间描述
}

keepalive_state = {
    "thread": None,
    "stop": False,
}


class _LogCapture:
    """将 print 输出捕获到 task_status['log_lines']。"""
    def __init__(self, original):
        self.original = original
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        task_status["log_lines"] = self.lines[-200:]  # 保留最近200行
        self.original.write(text)

    def flush(self):
        self.original.flush()


def _run_in_thread(target, name, args=(), kwargs=None):
    """在后台线程中运行任务，更新 task_status。"""
    if task_status["running"]:
        return False

    kwargs = kwargs or {}
    task_status["running"] = True
    task_status["task"] = name
    task_status["started_at"] = beijing_strftime("%Y-%m-%d %H:%M:%S")
    task_status["log_lines"] = []
    task_status["result"] = None

    def wrapper():
        old_stdout = sys.stdout
        sys.stdout = _LogCapture(old_stdout)
        try:
            result = target(*args, **kwargs)
            task_status["result"] = result
        except Exception as exc:
            task_status["result"] = {"error": str(exc)}
            print(f"[ERROR] {exc}")
        finally:
            sys.stdout = old_stdout
            task_status["running"] = False

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return True


# ── 数据读取工具 ──────────────────────────────────────────
def _load_browser_state() -> dict | None:
    """加载浏览器状态，当 PID 死亡时自动从系统中找回。"""
    path = RUNTIME_DIR / "browser-state.json"
    if not path.exists():
        # 状态文件不存在，尝试从系统中找回
        return _find_existing_browser()

    with open(path) as f:
        state = json.load(f)

    pid = state.get("pid", 0)
    port = state.get("port", 0)

    # PID 和 CDP 端口都正常 → 直接返回
    if pid and _pid_exists(pid) and port and _cdp_port_alive(port):
        return state

    # 进程已死或端口不通 → 尝试从系统中找回
    recovered = _find_existing_browser()
    if recovered:
        write_browser_state(path, recovered)
        return recovered

    # 没找到存活进程 → 返回原状态（会显示离线）
    return state


def _cdp_port_alive(port: int) -> bool:
    """检查 CDP 端口是否响应。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _dms_page_alive(port: int) -> bool:
    """检查浏览器中是否还有 DMS 页面打开。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
            return any(t.get("type") == "page" and "m-dms.dfmc.com.cn" in (t.get("url") or "") for t in targets)
    except Exception:
        return False


def _find_existing_browser() -> dict | None:
    """扫描系统中使用本项目 profile 目录的浏览器进程，返回状态信息。"""
    import subprocess
    profile_dir = str(PLUGIN_ROOT / ".browser-profile")
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if profile_dir not in line:
                continue
            if "Helper" in line:
                continue
            if "Google Chrome" not in line and "Microsoft Edge" not in line:
                continue

            parts = line.split(None, 1)
            new_pid = int(parts[0])
            cmd = parts[1] if len(parts) > 1 else ""

            new_port = 0
            for arg in cmd.split():
                if arg.startswith("--remote-debugging-port="):
                    new_port = int(arg.split("=")[1])
                    break

            if new_port and _cdp_port_alive(new_port):
                executable = DEFAULT_BROWSER_CANDIDATES["chrome"] if "Google Chrome" in cmd else DEFAULT_BROWSER_CANDIDATES.get("edge", "")
                return {
                    "pid": new_pid,
                    "port": new_port,
                    "browserExecutable": executable,
                    "browserProfileDir": profile_dir,
                    "targetUrl": "https://m-dms.dfmc.com.cn",
                    "startedAt": "",
                }
    except Exception:
        pass
    return None


def _find_latest_snapshot() -> dict | None:
    if not REPAIR_ORDERS_DIR.exists():
        return None
    files = sorted(REPAIR_ORDERS_DIR.glob("repair_orders_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0], "r", encoding="utf-8") as f:
        data = json.load(f)
    data["_file"] = files[0].name
    return data


def _load_alert_history() -> dict:
    path = FOLLOWUP_DIR / "alert_history.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _load_recent_logs(n=20) -> list[dict]:
    path = FOLLOWUP_DIR / "followup_log.jsonl"
    if not path.exists():
        return []
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            lines.append(line.strip())
    # 取最近n条
    recent = lines[-n:] if len(lines) >= n else lines
    return [json.loads(l) for l in recent if l]


def _list_reports() -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    reports = []
    for f in REPORTS_DIR.iterdir():
        if f.suffix == ".xlsx":
            reports.append({
                "name": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    reports.sort(key=lambda x: x["modified"], reverse=True)
    return reports


def _load_stores_json() -> dict:
    path = CONFIG_DIR / "stores.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {"version": "template", "stores": {}, "regions": {}, "national_recipients": {}}


def _save_stores_json(data: dict) -> None:
    path = CONFIG_DIR / "stores.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_keepalive_runtime() -> dict:
    path = RUNTIME_DIR / "keepalive-state.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


def _save_keepalive_runtime(data: dict) -> None:
    path = RUNTIME_DIR / "keepalive-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _keepalive_process_running() -> bool:
    state = _load_keepalive_runtime()
    pid = int(state.get("pid") or 0)
    if pid <= 0 or not process_is_running(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return False

    output = result.stdout.strip()
    if not output:
        return False
    stat = output.split(None, 1)[0]
    if "Z" in stat:
        return False
    return "keepalive_browser.py" in output


def _coerce_epoch(value) -> int:
    if value in (None, "", 0):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(datetime.fromisoformat(value).timestamp())
            except ValueError:
                return 0
    return 0


def _get_keepalive_info() -> dict:
    state = _load_keepalive_runtime()
    running = _keepalive_process_running()
    now_ts = int(time.time())
    next_refresh_at = _coerce_epoch(state.get("nextRefreshAt"))
    interval = int(state.get("interval") or 300)
    started_at = _coerce_epoch(state.get("startedAt"))
    if running and not next_refresh_at and started_at:
        next_refresh_at = started_at + interval
    return {
        "running": running,
        "pid": int(state.get("pid") or 0),
        "interval": interval,
        "started_at_epoch": started_at,
        "last_action_at_epoch": _coerce_epoch(state.get("lastActionAt")),
        "next_refresh_at_epoch": next_refresh_at,
        "seconds_left": max(next_refresh_at - now_ts, 0) if running and next_refresh_at else 0,
        "last_result": state.get("lastResult", ""),
        "cycle": int(state.get("cycle") or 0),
    }


def _stop_keepalive_process() -> None:
    state = _load_keepalive_runtime()
    pid = int(state.get("pid") or 0)
    if pid > 0 and _keepalive_process_running():
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    path = RUNTIME_DIR / "keepalive-state.json"
    if path.exists():
        path.unlink()


def _ensure_keepalive_process() -> bool:
    browser = _load_browser_state()
    if not browser:
        _stop_keepalive_process()
        return False

    port = int(browser.get("port") or 0)
    if not port or not _cdp_port_alive(port) or not _dms_page_alive(port):
        _stop_keepalive_process()
        return False

    if _keepalive_process_running():
        return True

    keepalive_script = PLUGIN_ROOT / "scripts" / "keepalive_browser.py"
    python_bin = PLUGIN_ROOT / ".venv" / "bin" / "python"
    try:
        now_ts = int(time.time())
        interval = 300
        proc = subprocess.Popen(
            [
                str(python_bin),
                str(keepalive_script),
                "--state-file",
                str(RUNTIME_DIR / "browser-state.json"),
                "--status-file",
                str(RUNTIME_DIR / "keepalive-state.json"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False

    _save_keepalive_runtime({
        "pid": proc.pid,
        "interval": interval,
        "startedAt": now_ts,
        "lastResult": "starting",
        "lastActionAt": 0,
        "nextRefreshAt": now_ts + interval,
    })
    return True


def _keepalive_watchdog_loop() -> None:
    while not keepalive_state["stop"]:
        try:
            _ensure_keepalive_process()
        except Exception:
            pass
        time.sleep(30)


def _start_keepalive_watchdog() -> None:
    thread = keepalive_state.get("thread")
    if thread and thread.is_alive():
        return
    keepalive_state["stop"] = False
    thread = threading.Thread(target=_keepalive_watchdog_loop, daemon=True)
    keepalive_state["thread"] = thread
    thread.start()


def _format_rule_recipients(rule: dict, stores_config: dict) -> str:
    """将规则原始 recipients 转成控制台友好的展示文本。"""
    role_labels = {
        "remind_all": "门店提醒人（1-4）",
        "store_service_manager": "门店服务经理（提醒人1）",
        "supervisor": "区域督导",
    }
    national = stores_config.get("national_recipients", {})
    national_names = [str(name).strip() for name in national.keys() if str(name).strip()] if isinstance(national, dict) else []

    display = []
    include_national = False
    for role in rule.get("recipients", []):
        if role in role_labels:
            display.append(role_labels[role])
        else:
            include_national = True

    if include_national and national_names:
        display.append(f"全国收件人（{'、'.join(national_names)}）")

    return "，".join(display) if display else "—"


def _detect_available_browsers() -> list[dict]:
    """扫描本地可用浏览器，返回列表。"""
    browsers = []
    for name, path in DEFAULT_BROWSER_CANDIDATES.items():
        p = Path(path)
        browsers.append({
            "name": name,
            "path": path,
            "available": p.exists(),
            "label": "Chrome" if name == "chrome" else "Edge" if name == "edge" else name,
        })
    # 也检查环境变量指定的浏览器
    env_path = os.environ.get("DFMC_DMS_BROWSER_EXECUTABLE")
    if env_path and Path(env_path).exists():
        browsers.append({
            "name": "custom",
            "path": env_path,
            "available": True,
            "label": f"自定义 ({Path(env_path).name})",
        })
    return browsers


# ── 页面路由 ──────────────────────────────────────────────

@app.route("/")
def dashboard():
    """仪表盘首页。"""
    browser = _load_browser_state()
    snapshot = _find_latest_snapshot()
    alert_history = _load_alert_history()
    recent_logs = _load_recent_logs(10)

    # 浏览器会话状态
    browser_ok = False
    if browser:
        port = browser.get("port", 0)
        browser_ok = _cdp_port_alive(port) and _dms_page_alive(port)
        if browser_ok:
            _ensure_keepalive_process()
    keepalive_info = _get_keepalive_info()

    # 快照统计
    snap_info = {}
    if snapshot:
        records = snapshot.get("records", [])
        accident = [r for r in records if not r.get("is_qc_completed")]
        snap_info = {
            "date": snapshot.get("snapshot_date", ""),
            "file": snapshot.get("_file", ""),
            "total_records": snapshot.get("total_records", len(records)),
            "accident_records": snapshot.get("accident_records", len(accident)),
            "regions": len(snapshot.get("regions", set(r.get("region", "") for r in records))),
            "stores": len(snapshot.get("stores", set(r.get("store_code", "") for r in records))),
        }

    # 告警历史统计
    history_info = {
        "vin_count": len(alert_history),
        "total_alerts": sum(v.get("alert_count", 0) for v in alert_history.values()),
    }

    # 当前运行模式
    mode = scheduler_state["mode"]

    return render_template("dashboard.html",
        browser=browser, browser_ok=browser_ok,
        keepalive_info=keepalive_info,
        available_browsers=_detect_available_browsers(),
        Path=Path,
        snap_info=snap_info, history_info=history_info,
        recent_logs=recent_logs, mode=mode,
        task_status=task_status,
    )


@app.route("/mode", methods=["GET", "POST"])
def mode_page():
    """模式切换页面。"""
    if request.method == "POST":
        action = request.form.get("action")

        if action == "test":
            test_phone = request.form.get("test_phone", _DEFAULT_TEST_PHONE)
            skip_crawl = request.form.get("skip_crawl") == "on"
            success = _run_in_thread(
                _run_test_mode, "测试模式",
                kwargs={"test_phone": test_phone, "skip_crawl": skip_crawl},
            )
            if success:
                scheduler_state["mode"] = "test"
            return redirect(url_for("dashboard"))

        elif action == "morning":
            skip_crawl = request.form.get("skip_crawl") == "on"
            success = _run_in_thread(
                _run_morning, "10:00任务（正式）",
                kwargs={"skip_crawl": skip_crawl},
            )
            return redirect(url_for("dashboard"))

        elif action == "evening":
            skip_crawl = request.form.get("skip_crawl") == "on"
            success = _run_in_thread(
                _run_evening, "17:00报表（正式）",
                kwargs={"skip_crawl": skip_crawl},
            )
            return redirect(url_for("dashboard"))

        elif action == "scheduled":
            skip_crawl = request.form.get("skip_crawl") == "on"
            # 启动后台调度线程
            t = threading.Thread(
                target=_run_scheduler_loop,
                kwargs={"skip_crawl": skip_crawl},
                daemon=True,
            )
            scheduler_state["mode"] = "scheduled"
            scheduler_state["scheduler_thread"] = t
            t.start()
            return redirect(url_for("dashboard"))

        elif action == "stop":
            if scheduler_state["mode"] == "scheduled":
                _stop_scheduler()
            else:
                scheduler_state["mode"] = "idle"
            return redirect(url_for("dashboard"))

    return render_template("mode.html", mode=scheduler_state["mode"])


@app.route("/rules")
def rules_page():
    """规则列表页面。"""
    rules = load_rules_config()
    stores_config = load_stores_config()
    display_rules = []
    for rule in rules:
        display_rule = dict(rule)
        display_rule["recipient_display"] = _format_rule_recipients(rule, stores_config)
        display_rules.append(display_rule)
    return render_template("rules.html", rules=display_rules)


@app.route("/rules/edit/<int:rule_id>", methods=["GET", "POST"])
def rules_edit(rule_id):
    """编辑单条规则。"""
    rules_path = CONFIG_DIR / "rules.json"
    with open(rules_path, "r", encoding="utf-8") as f:
        all_data = json.load(f)

    rules = all_data["rules"]
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return redirect(url_for("rules_page"))

    if request.method == "POST":
        # 更新规则字段
        rule["name"] = request.form.get("name", rule["name"])
        rule["description"] = request.form.get("description", rule["description"])
        rule["enabled"] = request.form.get("enabled", "true") == "true"

        # 更新 thresholds（针对 Rule 1）
        if rule_id == 1:
            for i, lv in enumerate(rule.get("levels", [])):
                lv["threshold"] = int(request.form.get(f"level_{i}_threshold", lv["threshold"]))

        # 更新 trigger_times（针对 Rule 3/4）
        if "trigger_times" in rule:
            times_str = request.form.get("trigger_times", "")
            rule["trigger_times"] = [t.strip() for t in times_str.split(",") if t.strip()]
        elif "trigger_time" in rule:
            rule["trigger_time"] = request.form.get("trigger_time", rule["trigger_time"])

        # 更新 recipients
        recipients_str = request.form.get("recipients", "")
        rule["recipients"] = [r.strip() for r in recipients_str.split(",") if r.strip()]

        # 保存
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=2)

        return redirect(url_for("rules_page"))

    return render_template("rules_edit.html", rule=rule, rule_id=rule_id)


@app.route("/trigger")
def trigger_page():
    """手动触发任务页面。"""
    # 列出 output/ 中已有的 Excel 文件
    xlsx_files = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("maintenance_orders_*.xlsx"), reverse=True):
            xlsx_files.append({
                "name": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

    return render_template("trigger.html", task_status=task_status, xlsx_files=xlsx_files)


@app.route("/data")
def data_page():
    """数据查看页面。"""
    snapshot = _find_latest_snapshot()
    if not snapshot:
        return render_template("data.html", snap_info=None, overdue=[], alert_history={})

    records = snapshot.get("records", [])
    active = [r for r in records if not r.get("is_qc_completed")]
    overdue = [r for r in active if r.get("days_in_shop", 0) >= 7]
    overdue.sort(key=lambda r: r.get("days_in_shop", 0), reverse=True)

    # 分级标记
    levels = load_rules_config()
    alert_levels = next((r.get("levels", []) for r in levels if r["id"] == 1), [])
    for v in overdue:
        days = v.get("days_in_shop", 0)
        for lv in reversed(alert_levels):
            if days >= lv["threshold"]:
                v["level_icon"] = lv["icon"]
                v["level_label"] = lv["label"]
                break

    snap_info = {
        "date": snapshot.get("snapshot_date", ""),
        "total": len(records),
        "active": len(active),
        "overdue": len(overdue),
        "regions": len(set(r.get("region", "") for r in active)),
        "stores": len(set(r.get("store_code", "") for r in active)),
    }

    alert_history = _load_alert_history()

    return render_template("data.html", snap_info=snap_info, overdue=overdue, alert_history=alert_history)


@app.route("/reports")
def reports_page():
    """报表列表页面。"""
    reports = _list_reports()
    return render_template("reports.html", reports=reports)


@app.route("/reports/download/<filename>")
def reports_download(filename):
    """下载报表文件。"""
    return send_from_directory(str(REPORTS_DIR), filename)


@app.route("/contacts")
def contacts_page():
    """门店联系人页面。"""
    stores_config = load_stores_config()
    stores = stores_config.get("stores", {})
    regions = stores_config.get("regions", {})
    national = stores_config.get("national_recipients", {})

    # 按区域分组门店
    store_list = []
    for code, info in stores.items():
        store_list.append({
            "code": code,
            "name": info.get("name", ""),
            "region": info.get("region", ""),
            "supervisor": info.get("supervisor", {}).get("name", ""),
            "sup_phone": info.get("supervisor", {}).get("phone", ""),
            "remind_phones": info.get("remind_phones", []),
        })
    store_list.sort(key=lambda s: (s["region"], s["code"]))

    national_list = [
        {"key": key, "phone": phone}
        for key, phone in sorted(national.items())
        if str(phone or "").strip()
    ]

    return render_template(
        "contacts.html",
        store_list=store_list,
        regions=regions,
        national=national,
        national_list=national_list,
    )


@app.route("/clear-cache")
def clear_cache_page():
    """清除缓存和历史数据页面。"""
    return render_template("clear_cache.html")


@app.route("/contacts/upload", methods=["POST"])
def contacts_upload():
    """上传新的 list.xlsx。"""
    if "file" not in request.files:
        return redirect(url_for("contacts_page"))
    f = request.files["file"]
    if f.filename == "" or not f.filename.endswith(".xlsx"):
        return redirect(url_for("contacts_page"))

    # 备份旧的 list.xlsx
    backup = LIST_XLSX.parent / f"list_backup_{beijing_strftime('%Y%m%d%H%M%S')}.xlsx"
    if LIST_XLSX.exists():
        import shutil
        shutil.copy2(str(LIST_XLSX), str(backup))

    f.save(str(LIST_XLSX))
    return redirect(url_for("contacts_page"))


@app.route("/contacts/national/add", methods=["POST"])
def contacts_national_add():
    """新增或更新全国收件人。"""
    key = str(request.form.get("key", "") or "").strip()
    phone = str(request.form.get("phone", "") or "").strip()
    phone = phone.replace("+86-", "").replace("+86", "").replace(" ", "")
    if not key or not phone:
        return redirect(url_for("contacts_page"))

    stores_data = _load_stores_json()
    national = stores_data.get("national_recipients", {})
    if not isinstance(national, dict):
        national = {}
    national[key] = phone
    stores_data["national_recipients"] = national
    _save_stores_json(stores_data)
    return redirect(url_for("contacts_page"))


@app.route("/contacts/national/delete", methods=["POST"])
def contacts_national_delete():
    """删除全国收件人。"""
    key = str(request.form.get("key", "") or "").strip()
    if not key:
        return redirect(url_for("contacts_page"))

    stores_data = _load_stores_json()
    national = stores_data.get("national_recipients", {})
    if isinstance(national, dict) and key in national:
        national.pop(key, None)
        stores_data["national_recipients"] = national
        _save_stores_json(stores_data)
    return redirect(url_for("contacts_page"))


# ── API 路由 ──────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """系统状态 JSON。"""
    browser = _load_browser_state()
    snapshot = _find_latest_snapshot()
    alert_history = _load_alert_history()

    # 浏览器详细信息
    browser_info = {}
    browser_ok = False
    if browser:
        pid = browser.get("pid", 0)
        port = browser.get("port", 0)
        browser_ok = _cdp_port_alive(port) and _dms_page_alive(port)
        if browser_ok:
            _ensure_keepalive_process()
        browser_info = {
            "browser": Path(browser.get("browserExecutable", "")).stem,
            "pid": pid,
            "port": port,
            "started_at": browser.get("startedAt", ""),
            "alive": browser_ok,
            "cdp_alive": _cdp_port_alive(port),
            "dms_page": _dms_page_alive(port),
            "keepalive_running": _keepalive_process_running(),
        }
    keepalive_info = _get_keepalive_info()

    return jsonify({
        "mode": scheduler_state["mode"],
        "browser_ok": browser_ok,
        "browser_info": browser_info,
        "available_browsers": _detect_available_browsers(),
        "snapshot_date": snapshot.get("snapshot_date", "") if snapshot else "",
        "task_running": task_status["running"],
        "task_name": task_status["task"],
        "task_started": task_status["started_at"],
        "task_log_tail": task_status["log_lines"][-10:] if task_status["log_lines"] else [],
        "alert_vin_count": len(alert_history),
        "next_fire": scheduler_state.get("next_fire", ""),
        "scheduler_started": scheduler_state.get("started_at", ""),
        "keepalive_running": keepalive_info["running"],
        "keepalive_info": keepalive_info,
    })


@app.route("/api/run-test", methods=["POST"])
def api_run_test():
    """启动测试模式。"""
    test_phone = request.json.get("test_phone", _DEFAULT_TEST_PHONE) if request.json else _DEFAULT_TEST_PHONE
    skip_crawl = request.json.get("skip_crawl", False) if request.json else False

    success = _run_in_thread(
        _run_test_mode, "测试模式",
        kwargs={"test_phone": test_phone, "skip_crawl": skip_crawl},
    )
    if success:
        scheduler_state["mode"] = "test"
    return jsonify({"started": success})


@app.route("/api/run-morning", methods=["POST"])
def api_run_morning():
    """执行 10:00 任务。"""
    skip_crawl = request.json.get("skip_crawl", False) if request.json else False
    success = _run_in_thread(
        _run_morning, "10:00任务",
        kwargs={"skip_crawl": skip_crawl},
    )
    return jsonify({"started": success})


@app.route("/api/run-evening", methods=["POST"])
def api_run_evening():
    """执行 17:00 报表。"""
    skip_crawl = request.json.get("skip_crawl", False) if request.json else False
    success = _run_in_thread(
        _run_evening, "17:00报表",
        kwargs={"skip_crawl": skip_crawl},
    )
    return jsonify({"started": success})


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    """触发爬取。"""
    success = _run_in_thread(_crawl_only, "爬取维修工单")
    return jsonify({"started": success})


@app.route("/api/browser/launch", methods=["POST"])
def api_browser_launch():
    """启动指定浏览器用于 DMS 登录。如果已有浏览器在运行，直接使用。"""
    browser_name = request.json.get("browser", "chrome") if request.json else "chrome"

    # 先检查是否已有浏览器在运行（自动恢复逻辑会更新状态）
    existing_state = _load_browser_state()
    if existing_state and _cdp_port_alive(existing_state.get("port", 0)):
        port = existing_state.get("port", 0)
        # If DMS page is closed, open a new one via CDP
        if not _dms_page_alive(port):
            import urllib.request
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new?https%3A%2F%2Fm-dms.dfmc.com.cn",
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
        _ensure_keepalive_process()
        return jsonify({
            "launched": True,
            "reused": True,
            "browser": Path(existing_state["browserExecutable"]).stem if existing_state.get("browserExecutable") else "未知",
            "pid": existing_state["pid"],
            "port": port,
            "message": "浏览器已在运行，直接使用当前会话",
        })

    try:
        browser_executable = detect_browser(browser_name, None)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400

    port = find_free_port()
    browser_profile_dir = PLUGIN_ROOT / ".browser-profile"
    target_url = "https://m-dms.dfmc.com.cn"

    cmd = [
        str(browser_executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={browser_profile_dir}",
        "--no-first-run",
        "--disable-default-apps",
        target_url,
    ]

    import subprocess
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return jsonify({"error": f"启动浏览器失败: {exc}"}), 500

    # 等待一下让浏览器启动
    time.sleep(3)

    # 验证浏览器是否存活，如果死了说明有旧进程占用 profile
    if not _pid_exists(proc.pid):
        # 扫描系统中已存在的浏览器进程，复用它
        recovered = _find_existing_browser()
        if recovered and _cdp_port_alive(recovered.get("port", 0)):
            write_browser_state(RUNTIME_DIR / "browser-state.json", recovered)
            return jsonify({
                "launched": True,
                "reused": True,
                "browser": Path(recovered["browserExecutable"]).stem if recovered.get("browserExecutable") else "未知",
                "pid": recovered["pid"],
                "port": recovered["port"],
                "message": "新浏览器因 profile 冲突退出，已自动连接到现有浏览器会话",
            })
        return jsonify({"error": "浏览器启动后立即退出（可能已有同 profile 浏览器在运行）"}), 500

    # 写入浏览器状态
    state_file = RUNTIME_DIR / "browser-state.json"
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    write_browser_state(state_file, {
        "port": port,
        "pid": proc.pid,
        "browserExecutable": str(browser_executable),
        "browserProfileDir": str(browser_profile_dir),
        "targetUrl": target_url,
        "startedAt": beijing_now().strftime("%Y-%m-%dT%H:%M:%S"),
    })

    # macOS: 用 AppleScript 激活窗口
    app_name = browser_executable.stem
    try:
        subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    _ensure_keepalive_process()

    return jsonify({
        "launched": True,
        "browser": app_name,
        "pid": proc.pid,
        "port": port,
        "message": f"{app_name} 已打开 DMS 系统，请在浏览器中手动登录",
    })


@app.route("/api/browser/disconnect", methods=["POST"])
def api_browser_disconnect():
    """断开浏览器连接（清理状态文件，不杀浏览器进程）。"""
    _stop_keepalive_process()
    state_file = RUNTIME_DIR / "browser-state.json"
    if state_file.exists():
        state_file.unlink()
    return jsonify({"disconnected": True})


@app.route("/api/start-scheduler", methods=["POST"])
def api_start_scheduler():
    """启动定时等候模式。"""
    if scheduler_state["mode"] == "scheduled":
        return jsonify({"error": "调度器已在运行"})
    skip_crawl = request.json.get("skip_crawl", False) if request.json else False
    t = threading.Thread(target=_run_scheduler_loop, kwargs={"skip_crawl": skip_crawl}, daemon=True)
    scheduler_state["mode"] = "scheduled"
    scheduler_state["scheduler_thread"] = t
    t.start()
    return jsonify({"started": True})


@app.route("/api/stop-scheduler", methods=["POST"])
def api_stop_scheduler():
    """停止定时等候模式。"""
    _stop_scheduler()
    return jsonify({"stopped": True})


@app.route("/api/import-xlsx", methods=["POST"])
def api_import_xlsx():
    """导入指定 Excel。"""
    filename = request.json.get("filename", "") if request.json else ""
    if not filename:
        return jsonify({"error": "缺少 filename"}), 400
    xlsx_path = OUTPUT_DIR / filename
    if not xlsx_path.exists():
        return jsonify({"error": "文件不存在"}), 404
    success = _run_in_thread(_import_only, "导入Excel", args=(str(xlsx_path),))
    return jsonify({"started": success})


@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    """清除缓存和历史数据。"""
    targets = request.json.get("targets", []) if request.json else []
    if not targets:
        return jsonify({"error": "未指定清除项"}), 400

    cleared = []
    for t in targets:
        if t == "alert_history":
            if ALERT_HISTORY_PATH.exists():
                ALERT_HISTORY_PATH.unlink()
                cleared.append("告警历史 (alert_history.json)")
        elif t == "followup_log":
            path = FOLLOWUP_DIR / "followup_log.jsonl"
            if path.exists():
                path.unlink()
                cleared.append("发送日志 (followup_log.jsonl)")
        elif t == "repair_orders":
            for f in REPAIR_ORDERS_DIR.glob("repair_orders_*.json"):
                f.unlink()
                cleared.append(f.name)
        elif t == "reports":
            for f in REPORTS_DIR.glob("*.xlsx"):
                f.unlink()
                cleared.append(f.name)
        elif t == "output":
            for f in OUTPUT_DIR.glob("maintenance_orders_*.xlsx"):
                f.unlink()
                cleared.append(f.name)
            crawl_manifest = OUTPUT_DIR / "crawl_manifest.json"
            if crawl_manifest.exists():
                crawl_manifest.unlink()
                cleared.append("crawl_manifest.json")
        elif t == "recipients_cache":
            path = CONFIG_DIR / "recipients.json"
            if path.exists():
                path.unlink()
                cleared.append("收件人缓存 (recipients.json)")
        elif t == "browser_state":
            _stop_keepalive_process()
            if RUNTIME_DIR.exists():
                for f in RUNTIME_DIR.glob("*.json"):
                    f.unlink()
                    cleared.append(f.name)
        elif t == "run_manifest":
            path = DATA_DIR / "run_manifest.json"
            if path.exists():
                path.unlink()
                cleared.append("运行记录 (run_manifest.json)")

    return jsonify({"cleared": cleared})


# ── 任务执行函数 ──────────────────────────────────────────

def _pid_exists(pid) -> bool:
    """检查 PID 是否存活（macOS 兼容）。"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _run_test_mode(test_phone=_DEFAULT_TEST_PHONE, skip_crawl=False):
    """测试模式：10:00+17:00 全规则，所有消息发给测试手机号。"""
    from scheduler import run_morning_alerts, run_evening_reports

    print(f"[测试模式] 所有消息发送给 {test_phone}")
    if skip_crawl:
        print("[测试模式] 跳过爬取，使用最新快照")
        run_morning_alerts(skip_crawl=True, test_phone=test_phone)
        run_evening_reports(skip_crawl=True, test_phone=test_phone)
    else:
        run_morning_alerts(test_phone=test_phone)
        run_evening_reports(test_phone=test_phone)


def _run_morning(skip_crawl=False):
    """正式模式：10:00 任务。"""
    from scheduler import run_morning_alerts
    run_morning_alerts(skip_crawl=skip_crawl)


def _run_evening(skip_crawl=False):
    """正式模式：17:00 报表。"""
    from scheduler import run_evening_reports
    run_evening_reports(skip_crawl=skip_crawl)


def _run_scheduler_loop(skip_crawl=False):
    """定时等候模式：后台常驻，10:00/17:00 自动触发任务。"""
    scheduler_state["scheduler_stop"] = False
    scheduler_state["started_at"] = beijing_strftime("%Y-%m-%d %H:%M:%S")

    print("[定时等候模式] 调度器已启动（北京时间 UTC+8）")
    print("  10:00 — 自动爬取 + 超期告警 + 门店/区域报表")
    print("  17:00 — 自动爬取 + 门店/区域报表 + 全国报表")
    print("  通过 Web 控制台停止")

    from scheduler import run_morning_alerts, run_evening_reports

    fired = {"10:00": False, "17:00": False}
    while not scheduler_state["scheduler_stop"]:
        now = beijing_now()
        now_str = now.strftime("%H:%M")

        # 计算下一次触发时间描述
        if now_str < "10:00":
            scheduler_state["next_fire"] = f"今天 10:00（北京时间 {now_str}）"
        elif now_str < "17:00":
            scheduler_state["next_fire"] = f"今天 17:00（北京时间 {now_str}）"
        else:
            scheduler_state["next_fire"] = f"明天 10:00（北京时间 {now_str}）"

        if now_str == "10:00" and not fired["10:00"]:
            print(f"\n{'=' * 50}")
            print(f"[10:00] 定时触发 — {now.strftime('%Y-%m-%d %H:%M')}")
            print(f"{'=' * 50}")
            try:
                run_morning_alerts(skip_crawl=skip_crawl)
            except Exception as exc:
                print(f"[ERROR] 10:00 任务异常: {exc}")
            fired["10:00"] = True

        elif now_str == "17:00" and not fired["17:00"]:
            print(f"\n{'=' * 50}")
            print(f"[17:00] 定时触发 — {now.strftime('%Y-%m-%d %H:%M')}")
            print(f"{'=' * 50}")
            try:
                run_evening_reports(skip_crawl=skip_crawl)
            except Exception as exc:
                print(f"[ERROR] 17:00 任务异常: {exc}")
            fired["17:00"] = True

        elif now_str not in ("10:00", "17:00"):
            # 过了触发点后重置，允许次日再触发
            fired = {"10:00": False, "17:00": False}

        time.sleep(30)

    scheduler_state["next_fire"] = ""
    print("[定时等候模式] 调度器已停止")


def _stop_scheduler():
    """停止定时调度线程。"""
    scheduler_state["scheduler_stop"] = True
    scheduler_state["mode"] = "idle"
    scheduler_state["next_fire"] = ""


def _crawl_only():
    """仅爬取。"""
    from scheduler import _crawl_and_import
    data = _crawl_and_import()
    if data:
        print(f"爬取完成: {data.get('total_records', 0)} 条记录")
    else:
        print("[ERROR] 爬取失败")


def _import_only(xlsx_path: str):
    """仅导入 Excel。"""
    data = import_excel(xlsx_path)
    print(f"导入完成: {data.get('total_records', 0)} 条记录, {data.get('accident_records', '?')} 条事故车")


# ── 启动 ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="事故车提醒系统 Web 控制台")
    parser.add_argument("--port", type=int, default=9000, help="端口（默认 9000）")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    args = parser.parse_args()

    _start_keepalive_watchdog()
    print(f"事故车提醒系统 Web 控制台启动: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
