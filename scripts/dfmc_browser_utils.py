#!/usr/bin/env python3
"""Shared browser utilities for accident-vehicle-reminder scripts.

Session sharing
---------------
Multiple crawlers (e.g. VIP reminder + maintenance orders) can reuse ONE
Chromium window and ONE keepalive process by pointing at the same session home:

  export DFMC_DMS_SESSION_HOME=/path/to/shared-dms-session

Layout under the session home (defaults to the plugin root when unset):

  .browser-profile/          Chromium --user-data-dir
  .runtime/
    browser-state.json       CDP port / pid
    keepalive-state.json     keepalive status
    exporting.lock           busy lock (keepalive skips refresh; crawlers mutex)
    crawl_schedule.json      时刻表（定时爬取窗口）
    crawl_registry.json      爬取登记（进行中 / 今日已完成）
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, Error, Playwright


DEFAULT_TARGET_URL = "https://m-dms.dfmc.com.cn"
DMS_HOST = "m-dms.dfmc.com.cn"
DMS_CLEAN_URL = "https://m-dms.dfmc.com.cn/#/dashboard"
DEFAULT_STATE_FILE_NAME = "browser-state.json"
EXPORT_LOCK_NAME = "exporting.lock"
CRAWL_SCHEDULE_NAME = "crawl_schedule.json"
CRAWL_REGISTRY_NAME = "crawl_registry.json"
KEEPALIVE_LOG_NAME = "keepalive.log"
KEEPALIVE_LOG_MAX_BYTES = 2 * 1024 * 1024
SESSION_HOME_ENV = "DFMC_DMS_SESSION_HOME"
BROWSER_EXECUTABLE_ENV = "DFMC_DMS_BROWSER_EXECUTABLE"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# Shared timetable: keepalive skips refresh from (time - pre_minutes) until
# the matching crawl unregisters (or await_start_minutes elapses with no start).
DEFAULT_CRAWL_SCHEDULE: dict[str, Any] = {
    "pre_minutes": 3,
    "await_start_minutes": 45,
    "entries": [
        {
            "id": "district-form",
            "name": "区域报表",
            "time": "08:30",
            "owners": ["mhero_district_form"],
            "enabled": True,
        },
        {
            "id": "vip-alert",
            "name": "VIP保养提醒",
            "time": "09:00",
            "owners": ["vip_maintenance_reminder"],
            "enabled": True,
        },
        {
            "id": "accident-morning",
            "name": "事故车上午任务",
            "time": "10:00",
            "owners": ["crawl_maintenance_orders"],
            "enabled": True,
        },
        {
            "id": "accident-evening",
            "name": "事故车下午报表",
            "time": "17:00",
            "owners": ["crawl_maintenance_orders"],
            "enabled": True,
        },
    ],
}

BROWSER_PROCESS_MARKERS = (
    "Google Chrome for Testing",
    "Chromium",
    "Google Chrome",
    "Microsoft Edge",
)

BROWSER_LABELS = {
    "chromium": "Chromium (Playwright)",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
}


def resolve_playwright_chromium() -> Optional[str]:
    """Locate Playwright's bundled Chromium / Chrome for Testing."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
        if path and Path(path).exists():
            return path
    except Exception:
        pass

    roots = [
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ]
    patterns = [
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-win/chrome.exe",
    ]
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            matches.extend(root.glob(pattern))
    if not matches:
        return None
    matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return str(matches[0])


def build_browser_candidates() -> dict[str, str]:
    """Prefer Playwright Chromium so DMS login does not occupy system Chrome."""
    candidates: dict[str, str] = {}
    chromium = resolve_playwright_chromium()
    if chromium:
        candidates["chromium"] = chromium
    candidates["chrome"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    candidates["edge"] = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    return candidates


DEFAULT_BROWSER_CANDIDATES = build_browser_candidates()
DEFAULT_BROWSER_NAME = "chromium" if "chromium" in DEFAULT_BROWSER_CANDIDATES else "chrome"


def browser_label(name: str) -> str:
    return BROWSER_LABELS.get(name, name)


def is_cdp_browser_command(cmd: str) -> bool:
    if "Helper" in cmd:
        return False
    return any(marker in cmd for marker in BROWSER_PROCESS_MARKERS)


def resolve_executable_from_command(cmd: str) -> str:
    candidates = build_browser_candidates()
    for path in candidates.values():
        if path and path in cmd:
            return path
    if "Google Chrome for Testing" in cmd or "/Chromium" in cmd or "Chromium.app" in cmd:
        return candidates.get("chromium", "")
    if "Google Chrome" in cmd:
        return candidates.get("chrome", "")
    if "Microsoft Edge" in cmd:
        return candidates.get("edge", "")
    return cmd.split(None, 1)[0] if cmd else ""


def detect_browser(preferred: str, explicit_path: Optional[str]) -> Path:
    candidates_map = build_browser_candidates()
    preferred = (preferred or DEFAULT_BROWSER_NAME).strip().lower()
    if preferred == "custom":
        preferred = DEFAULT_BROWSER_NAME

    candidates: list[tuple[str, Optional[str]]] = []
    if explicit_path:
        candidates.append(("explicit", explicit_path))
    env_browser = os.environ.get(BROWSER_EXECUTABLE_ENV)
    if env_browser:
        candidates.append(("env", env_browser))
    if preferred in candidates_map:
        candidates.append((preferred, candidates_map[preferred]))
    for name, path in candidates_map.items():
        if name != preferred:
            candidates.append((name, path))

    for _, path in candidates:
        if path and Path(path).exists():
            return Path(path)

    options = "\n".join(f"- {path}" for path in candidates_map.values())
    raise FileNotFoundError(
        "No supported browser executable was found.\n"
        "Pass --browser-executable or set DFMC_DMS_BROWSER_EXECUTABLE.\n"
        f"Tried:\n{options}"
    )


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def get_session_home(plugin_root: Path) -> Path:
    """Resolve the shared DMS session home.

    If DFMC_DMS_SESSION_HOME is set, all plugins/crawlers using that path share
    one browser profile, state file, keepalive, and export lock.
    Otherwise fall back to the calling plugin root (isolated session).
    """
    env = (os.environ.get(SESSION_HOME_ENV) or "").strip()
    if env:
        home = Path(env).expanduser().resolve()
    else:
        home = plugin_root.resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_runtime_dir(plugin_root: Path) -> Path:
    runtime_dir = get_session_home(plugin_root) / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_browser_profile_dir(plugin_root: Path) -> Path:
    profile_dir = get_session_home(plugin_root) / ".browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def get_default_state_file(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / DEFAULT_STATE_FILE_NAME


def get_export_lock_path(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / EXPORT_LOCK_NAME


def get_crawl_schedule_path(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / CRAWL_SCHEDULE_NAME


def get_crawl_registry_path(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / CRAWL_REGISTRY_NAME


def _beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM: {value!r}")
    return int(parts[0]), int(parts[1])


def ensure_default_crawl_schedule(plugin_root: Path) -> Path:
    """Create crawl_schedule.json with defaults if missing."""
    path = get_crawl_schedule_path(plugin_root)
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CRAWL_SCHEDULE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return path


def load_crawl_schedule(plugin_root: Path) -> dict[str, Any]:
    path = ensure_default_crawl_schedule(plugin_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Fill missing keys from defaults without wiping custom entries.
            merged = dict(DEFAULT_CRAWL_SCHEDULE)
            merged.update({k: v for k, v in data.items() if k != "entries"})
            if isinstance(data.get("entries"), list) and data["entries"]:
                merged["entries"] = data["entries"]
            return merged
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return dict(DEFAULT_CRAWL_SCHEDULE)


def _empty_registry() -> dict[str, Any]:
    return {"active": None, "completedToday": {"date": "", "ids": []}}


def load_crawl_registry(plugin_root: Path) -> dict[str, Any]:
    path = get_crawl_registry_path(plugin_root)
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_registry()
        active = data.get("active")
        if active is not None and not isinstance(active, dict):
            active = None
        completed = data.get("completedToday") or {}
        if not isinstance(completed, dict):
            completed = {"date": "", "ids": []}
        ids = completed.get("ids") or []
        if not isinstance(ids, list):
            ids = []
        return {
            "active": active,
            "completedToday": {
                "date": str(completed.get("date") or ""),
                "ids": [str(x) for x in ids],
            },
        }
    except (OSError, json.JSONDecodeError, TypeError):
        return _empty_registry()


def save_crawl_registry(plugin_root: Path, registry: dict[str, Any]) -> None:
    path = get_crawl_registry_path(plugin_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_completed(registry: dict[str, Any], today: str) -> list[str]:
    completed = registry.get("completedToday") or {}
    if str(completed.get("date") or "") != today:
        registry["completedToday"] = {"date": today, "ids": []}
        return []
    ids = completed.get("ids") or []
    return [str(x) for x in ids] if isinstance(ids, list) else []


def _infer_schedule_id(plugin_root: Path, owner: str, *, now: Optional[datetime] = None) -> str:
    """Pick the best timetable entry for this owner around now."""
    now = now or _beijing_now()
    today = now.strftime("%Y-%m-%d")
    schedule = load_crawl_schedule(plugin_root)
    registry = load_crawl_registry(plugin_root)
    done = set(_normalize_completed(registry, today))
    pre = int(schedule.get("pre_minutes") or 3)
    await_start = int(schedule.get("await_start_minutes") or 45)
    best_id = ""
    best_delta: Optional[timedelta] = None
    for entry in schedule.get("entries") or []:
        if not entry or not entry.get("enabled", True):
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in done:
            continue
        owners = [str(x) for x in (entry.get("owners") or [])]
        if owner and owners and owner not in owners:
            continue
        try:
            hour, minute = _parse_hhmm(str(entry.get("time") or ""))
        except ValueError:
            continue
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_start = start - timedelta(minutes=pre)
        window_end = start + timedelta(minutes=await_start)
        if not (window_start <= now <= window_end):
            continue
        delta = abs(now - start)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_id = entry_id
    return best_id


def register_crawl(
    plugin_root: Path,
    owner: str,
    *,
    schedule_id: str = "",
) -> dict[str, Any]:
    """Mark a crawl as in-progress so keepalive skips refresh until unregister."""
    now = _beijing_now()
    today = now.strftime("%Y-%m-%d")
    registry = load_crawl_registry(plugin_root)
    _normalize_completed(registry, today)
    sid = (schedule_id or "").strip() or _infer_schedule_id(plugin_root, owner, now=now)
    active = {
        "owner": owner,
        "pid": os.getpid(),
        "scheduleId": sid,
        "startedAt": now.isoformat(),
    }
    registry["active"] = active
    save_crawl_registry(plugin_root, registry)
    return active


def unregister_crawl(plugin_root: Path, owner: str = "") -> None:
    """Clear active crawl registration and mark timetable entry done for today."""
    now = _beijing_now()
    today = now.strftime("%Y-%m-%d")
    registry = load_crawl_registry(plugin_root)
    done = _normalize_completed(registry, today)
    active = registry.get("active")
    if isinstance(active, dict):
        active_owner = str(active.get("owner") or "")
        active_pid = int(active.get("pid") or 0)
        if owner and active_owner and active_owner != owner and active_pid and process_is_running(active_pid):
            return
        sid = str(active.get("scheduleId") or "")
        if sid and sid not in done:
            done.append(sid)
        registry["completedToday"] = {"date": today, "ids": done}
    registry["active"] = None
    save_crawl_registry(plugin_root, registry)


def refresh_block_reason(plugin_root: Path) -> Optional[str]:
    """Return a human reason if keepalive must skip page.reload; else None."""
    # 1) Legacy / concurrent export lock
    lock_file = get_export_lock_path(plugin_root)
    if lock_file.exists():
        payload = _read_lock_payload(lock_file)
        if _lock_is_active(payload):
            holder = payload.get("owner") or "unknown"
            return f"export_lock:{holder}"

    now = _beijing_now()
    today = now.strftime("%Y-%m-%d")
    registry = load_crawl_registry(plugin_root)

    # 2) Active crawl registration
    active = registry.get("active")
    if isinstance(active, dict) and active:
        pid = int(active.get("pid") or 0)
        if pid <= 0 or process_is_running(pid):
            owner = active.get("owner") or "unknown"
            sid = active.get("scheduleId") or ""
            return f"registered:{owner}" + (f"/{sid}" if sid else "")
        # Stale registration — clear
        registry["active"] = None
        save_crawl_registry(plugin_root, registry)

    # 3) Timetable pre-window / await-start window
    schedule = load_crawl_schedule(plugin_root)
    done = set(_normalize_completed(registry, today))
    pre = int(schedule.get("pre_minutes") or 3)
    await_start = int(schedule.get("await_start_minutes") or 45)
    for entry in schedule.get("entries") or []:
        if not entry or not entry.get("enabled", True):
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in done:
            continue
        try:
            hour, minute = _parse_hhmm(str(entry.get("time") or ""))
        except ValueError:
            continue
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_start = start - timedelta(minutes=pre)
        window_end = start + timedelta(minutes=await_start)
        if window_start <= now <= window_end:
            name = entry.get("name") or entry_id
            return f"schedule:{entry_id}({name} {entry.get('time')}, pre={pre}m)"

    return None


def write_browser_state(state_file: Path, payload: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_browser_state(state_file: Path) -> dict[str, Any]:
    return json.loads(state_file.read_text(encoding="utf-8"))


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cdp_is_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def dms_page_alive(port: int) -> bool:
    """True when a DMS / SSO / login tab is already open."""
    return bool(collect_page_hints(port).get("has_session"))


def sanitize_dms_url(url: str = "") -> str:
    """Drop OAuth query (?code=) and keep a DMS hash route."""
    raw = (url or "").strip()
    fragment = "/dashboard"
    if "#" in raw:
        piece = raw.split("#", 1)[1].strip()
        if piece:
            fragment = piece if piece.startswith("/") else f"/{piece}"
    return f"{DEFAULT_TARGET_URL}/#/{fragment.lstrip('/')}"


def dms_hash_path(url_or_route: str = "") -> str:
    text = (url_or_route or "").strip()
    if "#" in text:
        text = text.split("#", 1)[1]
    text = text.split("?", 1)[0].strip()
    return "/" + text.lstrip("/")


def dms_route_url(route: str = "/dashboard") -> str:
    return f"{DEFAULT_TARGET_URL}/#{dms_hash_path(route)}"


def hash_route_matches(url: str, route: str) -> bool:
    current = dms_hash_path(url)
    expected = dms_hash_path(route)
    if not expected or expected == "/":
        return False
    return current == expected or current.startswith(expected + "/")


def goto_dms_route(page: Any, route: str, *, timeout_ms: int = 20_000) -> str:
    """Open a clean DMS hash route and wait until the tab actually lands there.

    Assigning window.location.hash often leaves the previous Vue page mounted,
    so crawlers would click Query/Export on the leftover screen.
    """
    target = dms_route_url(route)
    expected = dms_hash_path(route)
    page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
    deadline = time.monotonic() + max(timeout_ms / 1000.0, 1.0)
    last = page.url or ""
    while time.monotonic() < deadline:
        last = page.url or ""
        hint = dms_session_hint(last)
        if hint in {"login", "sso"}:
            raise RuntimeError(f"Need login while opening {expected}: {last[:120]}")
        if hash_route_matches(last, expected):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError(f"DMS route did not change to {expected}: {last[:160]}")
    try:
        page.wait_for_selector(
            "section.mixButton, .el-table, #datePicker",
            timeout=min(int(timeout_ms), 15_000),
        )
    except Error:
        page.wait_for_timeout(800)
    page.wait_for_timeout(400)
    return last


def dms_session_hint(url: str = "") -> str:
    """Classify a tab URL: ok / sso / login / other."""
    text = (url or "").lower()
    if not text:
        return "other"
    if "iam-admin.m-hero.com" in text or "sso." in text or "/cas/" in text:
        return "sso"
    if "/login" in text:
        return "login"
    if DMS_HOST in text:
        return "ok"
    return "other"


def list_cdp_pages(port: int) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        if isinstance(targets, list):
            return [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    except Exception:
        return []
    return []


def collect_page_hints(port: int) -> dict[str, Any]:
    pages = list_cdp_pages(port)
    urls = [str(p.get("url") or "") for p in pages]
    hints = [dms_session_hint(u) for u in urls]
    dms_url = next((u for u in urls if DMS_HOST in u), "")
    hint = "other"
    if "ok" in hints:
        hint = "ok"
    elif "sso" in hints:
        hint = "sso"
    elif "login" in hints:
        hint = "login"
    has_session = hint in {"ok", "sso", "login"}
    return {
        "pages": pages,
        "urls": urls,
        "hint": hint,
        "dms_url": dms_url,
        "has_dms": has_session,
        "has_session": has_session,
    }


def _is_blank_tab_url(url: str = "") -> bool:
    text = (url or "").strip().lower()
    return text in {"", "about:blank"} or text.startswith("chrome://newtab") or text.startswith("chrome://new-tab-page")


def close_cdp_page(port: int, target_id: str) -> bool:
    page_id = (target_id or "").strip()
    if not page_id:
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{quote(page_id, safe='')}", timeout=3).read()
        return True
    except Exception:
        return False


def open_dms_tab(port: int, url: str = "") -> bool:
    """Open a clean DMS tab via Chrome HTTP CDP."""
    target = sanitize_dms_url(url) if url else DMS_CLEAN_URL
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{quote(target, safe='')}",
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=3).read()
        return True
    except Exception:
        return False


def ensure_dms_tab(port: int) -> bool:
    """Keep exactly one DMS/SSO/login tab. Do not open another if one exists."""
    hints = collect_page_hints(port)
    pages = [p for p in (hints.get("pages") or []) if isinstance(p, dict)]
    session_pages = []
    blank_pages = []
    for page in pages:
        url = str(page.get("url") or "")
        hint = dms_session_hint(url)
        if hint in {"ok", "sso", "login"}:
            session_pages.append(page)
        elif _is_blank_tab_url(url):
            blank_pages.append(page)
    if session_pages:
        for page in session_pages[1:]:
            close_cdp_page(port, str(page.get("id") or ""))
        for page in blank_pages:
            close_cdp_page(port, str(page.get("id") or ""))
        return True
    return open_dms_tab(port)


def wait_for_dms_session(port: int, timeout_seconds: float = 45.0) -> dict[str, Any]:
    """Wait briefly for SSO to land on DMS, or return login/sso hint."""
    deadline = time.monotonic() + max(timeout_seconds, 0)
    last = collect_page_hints(port)
    while time.monotonic() < deadline:
        last = collect_page_hints(port)
        if last.get("hint") in {"ok", "login", "sso"}:
            return last
        time.sleep(1)
    return last


def get_keepalive_log_path(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / KEEPALIVE_LOG_NAME


def rotate_keepalive_log(log_path: Path, max_bytes: int = KEEPALIVE_LOG_MAX_BYTES) -> None:
    if log_path.exists() and log_path.stat().st_size >= max_bytes:
        backup = log_path.with_suffix(".log.1")
        backup.unlink(missing_ok=True)
        log_path.replace(backup)


def connect_browser_over_cdp(playwright: Playwright, port: int, timeout_seconds: float = 15.0) -> Browser:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as exc:
            last_error = exc
            if "Browser context management is not supported" in str(exc):
                time.sleep(1.5)
            else:
                time.sleep(0.25)
    raise RuntimeError(f"Failed to connect to Chrome over CDP on port {port}: {last_error}")


def _read_lock_payload(lock_file: Path) -> dict[str, Any]:
    if not lock_file.exists():
        return {}
    raw = lock_file.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Legacy plain-text lock
    return {"owner": "unknown", "pid": 0, "raw": raw}


def _lock_is_active(payload: dict[str, Any]) -> bool:
    pid = int(payload.get("pid") or 0)
    if pid > 0:
        return process_is_running(pid)
    # Legacy lock without pid — treat as active while file exists
    return bool(payload)


def acquire_export_lock(
    plugin_root: Path,
    owner: str,
    *,
    timeout_seconds: float = 0,
    poll_interval: float = 2.0,
    schedule_id: str = "",
) -> Path:
    """Acquire the shared session busy lock.

    Purpose:
    - Tell keepalive to skip page refresh while a crawler is exporting.
    - Prevent two crawlers from driving the same DMS tab at once.
    - Register the crawl on the shared timetable registry.

    timeout_seconds=0: fail immediately if another live owner holds the lock.
    timeout_seconds>0: wait up to that many seconds for the lock.
    """
    lock_file = get_export_lock_path(plugin_root)
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        payload = _read_lock_payload(lock_file) if lock_file.exists() else {}
        if lock_file.exists() and _lock_is_active(payload):
            holder = payload.get("owner") or "unknown"
            holder_pid = int(payload.get("pid") or 0)
            if timeout_seconds <= 0 or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"DMS session is busy: lock held by '{holder}' (pid={holder_pid}). "
                    "Wait for the other crawler to finish, or remove stale lock: "
                    f"{lock_file}"
                )
            print(f"  Session busy ({holder}), waiting for lock...")
            time.sleep(poll_interval)
            continue

        # Stale or missing — take ownership
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(
            json.dumps(
                {
                    "owner": owner,
                    "pid": os.getpid(),
                    "acquiredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            active = register_crawl(plugin_root, owner, schedule_id=schedule_id)
            sid = active.get("scheduleId") or ""
            print(f"  Crawl registered: owner={owner}" + (f" schedule={sid}" if sid else ""))
        except Exception as exc:
            print(f"  [WARN] crawl register failed: {exc}")
        return lock_file


def release_export_lock(lock_file: Path, *, owner: str = "", plugin_root: Optional[Path] = None) -> None:
    """Release the session busy lock if we still own it (or owner check skipped)."""
    root = plugin_root
    if root is None and lock_file is not None:
        # exporting.lock lives in <session>/.runtime/
        try:
            root = lock_file.resolve().parent.parent
        except Exception:
            root = None

    if lock_file.exists():
        if owner:
            payload = _read_lock_payload(lock_file)
            current_owner = str(payload.get("owner") or "")
            current_pid = int(payload.get("pid") or 0)
            if current_owner and current_owner != owner and current_pid and process_is_running(current_pid):
                return
        lock_file.unlink(missing_ok=True)

    if root is not None:
        try:
            unregister_crawl(root, owner=owner)
        except Exception as exc:
            print(f"  [WARN] crawl unregister failed: {exc}")


def recover_browser_state(state_file: Path, plugin_root: Path) -> Optional[int]:
    """Try to recover browser state by scanning for a running CDP-enabled browser."""
    browser_profile_dir = get_browser_profile_dir(plugin_root)
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        if "--remote-debugging-port=" not in line:
            continue
        if str(browser_profile_dir) not in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue

        cmd = parts[1]
        m = re.search(r"--remote-debugging-port=(\d+)", cmd)
        if not m:
            continue
        port = int(m.group(1))

        if not cdp_is_ready(port):
            continue

        executable = resolve_executable_from_command(cmd)

        payload = {
            "port": port,
            "pid": pid,
            "browserExecutable": executable,
            "browserProfileDir": str(browser_profile_dir),
            "targetUrl": DEFAULT_TARGET_URL,
            "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sessionHome": str(get_session_home(plugin_root)),
        }
        write_browser_state(state_file, payload)
        print(f"  [RECOVERED] Browser state rebuilt: pid={pid}, port={port}")
        return port

    return None


def ensure_cdp_browser_running(state_file: Path, plugin_root: Optional[Path] = None) -> int:
    """Validate browser CDP is ready; recover from profile process if needed.

    Returns the CDP port. Raises if the browser is not running.
    """
    if plugin_root is None:
        # Prefer session home = parent of .runtime/
        plugin_root = state_file.parent.parent

    if not state_file.exists():
        print("  Browser state file not found, attempting recovery...")
        port = recover_browser_state(state_file, plugin_root)
        if port:
            return port
        raise FileNotFoundError(
            f"No browser state found at {state_file} and no running browser detected. "
            "Start the login browser first (Web console or open_browser_for_login.sh)."
        )

    payload = read_browser_state(state_file)
    pid = int(payload.get("pid") or 0)
    port = int(payload.get("port") or 0)
    if pid <= 0 or port <= 0:
        print("  Invalid browser state, attempting recovery...")
        port = recover_browser_state(state_file, plugin_root)
        if port:
            return port
        raise RuntimeError(f"Invalid browser state: pid={pid}, port={port}")

    if not process_is_running(pid) or not cdp_is_ready(port):
        print("  Browser process/port not responding, attempting recovery...")
        port = recover_browser_state(state_file, plugin_root)
        if port:
            return port
        if not process_is_running(pid):
            raise RuntimeError(f"Browser process (pid={pid}) is not running. Restart the login browser.")
        raise RuntimeError(f"CDP port {port} is not responding. Browser may be hung.")

    return port


def chromium_automation_args() -> list[str]:
    """Flags that stop macOS from asking for the login password on every launch.

    Playwright Chromium otherwise unlocks "Chrome Safe Storage" via Keychain.
    The shared profile also inherited desktop extensions (1Password, etc.).
    """
    return [
        "--no-first-run",
        "--disable-default-apps",
        "--disable-extensions",
        "--use-mock-keychain",
        "--password-store=basic",
    ]


def build_shared_chromium_command(
    executable: str | Path,
    port: int,
    profile: Path,
    target_url: str = "",
    extra_args: list[str] | None = None,
) -> list[str]:
    command = [
        str(executable),
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile}",
        *chromium_automation_args(),
        *(extra_args or []),
    ]
    if target_url:
        command.append(target_url)
    return command


def launch_shared_cdp_browser(plugin_root: Path, browser_name: str = "") -> dict[str, Any]:
    """Recover a running shared Chromium, or launch one with the shared profile."""
    state_file = get_default_state_file(plugin_root)
    port = recover_browser_state(state_file, plugin_root)
    if port:
        return read_browser_state(state_file)

    executable = detect_browser(browser_name or DEFAULT_BROWSER_NAME, None)
    port = find_free_port()
    profile = get_browser_profile_dir(plugin_root)
    proc = subprocess.Popen(
        build_shared_chromium_command(executable, port, profile),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(3)
    if cdp_is_ready(port):
        ensure_dms_tab(port)
    if not process_is_running(proc.pid):
        recovered = recover_browser_state(state_file, plugin_root)
        if recovered:
            return read_browser_state(state_file)
        raise RuntimeError("Browser exited immediately after launch (profile may be in use).")

    payload = {
        "port": port,
        "pid": proc.pid,
        "browserExecutable": str(executable),
        "browserProfileDir": str(profile),
        "targetUrl": DMS_CLEAN_URL,
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sessionHome": str(get_session_home(plugin_root)),
    }
    write_browser_state(state_file, payload)
    return payload


def find_dms_page(context: Any) -> Optional[Any]:
    """Find a page whose URL contains the DMS domain among existing browser tabs."""
    for page in context.pages:
        try:
            if "m-dms.dfmc.com.cn" in (page.url or ""):
                return page
        except Error:
            continue
    return None
