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
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, Error, Playwright


DEFAULT_TARGET_URL = "https://m-dms.dfmc.com.cn"
DEFAULT_STATE_FILE_NAME = "browser-state.json"
EXPORT_LOCK_NAME = "exporting.lock"
SESSION_HOME_ENV = "DFMC_DMS_SESSION_HOME"
BROWSER_EXECUTABLE_ENV = "DFMC_DMS_BROWSER_EXECUTABLE"

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
    """Check whether a DMS page tab is open via CDP /json/list."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
            return any(
                t.get("type") == "page" and "m-dms.dfmc.com.cn" in (t.get("url") or "")
                for t in targets
            )
    except Exception:
        return False


def connect_browser_over_cdp(playwright: Playwright, port: int, timeout_seconds: float = 15.0) -> Browser:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as exc:
            last_error = exc
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
) -> Path:
    """Acquire the shared session busy lock.

    Purpose:
    - Tell keepalive to skip page refresh while a crawler is exporting.
    - Prevent two crawlers from driving the same DMS tab at once.

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
        return lock_file


def release_export_lock(lock_file: Path, *, owner: str = "") -> None:
    """Release the session busy lock if we still own it (or owner check skipped)."""
    if not lock_file.exists():
        return
    if owner:
        payload = _read_lock_payload(lock_file)
        current_owner = str(payload.get("owner") or "")
        current_pid = int(payload.get("pid") or 0)
        if current_owner and current_owner != owner and current_pid and process_is_running(current_pid):
            return
    lock_file.unlink(missing_ok=True)


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


def find_dms_page(context: Any) -> Optional[Any]:
    """Find a page whose URL contains the DMS domain among existing browser tabs."""
    for page in context.pages:
        try:
            if "m-dms.dfmc.com.cn" in (page.url or ""):
                return page
        except Error:
            continue
    return None
