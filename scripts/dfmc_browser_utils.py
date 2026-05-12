#!/usr/bin/env python3
"""Shared browser utilities for DMS crawler scripts."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, Error, Playwright, sync_playwright


DEFAULT_TARGET_URL = "https://m-dms.dfmc.com.cn"
DEFAULT_BROWSER_CANDIDATES = {
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
}
DEFAULT_STATE_FILE_NAME = "browser-state.json"


def detect_browser(preferred: str, explicit_path: Optional[str]) -> Path:
    candidates: list[tuple[str, Optional[str]]] = []
    if explicit_path:
        candidates.append(("explicit", explicit_path))
    env_browser = os.environ.get("DFMC_DMS_BROWSER_EXECUTABLE")
    if env_browser:
        candidates.append(("env", env_browser))
    if preferred in DEFAULT_BROWSER_CANDIDATES:
        candidates.append((preferred, DEFAULT_BROWSER_CANDIDATES[preferred]))
    for name, path in DEFAULT_BROWSER_CANDIDATES.items():
        if name != preferred:
            candidates.append((name, path))

    for _, path in candidates:
        if path and Path(path).exists():
            return Path(path)

    options = "\n".join(f"- {path}" for path in DEFAULT_BROWSER_CANDIDATES.values())
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


def get_runtime_dir(plugin_root: Path) -> Path:
    runtime_dir = plugin_root / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_default_state_file(plugin_root: Path) -> Path:
    return get_runtime_dir(plugin_root) / DEFAULT_STATE_FILE_NAME


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


def ensure_cdp_browser_running(state_file: Path) -> int:
    """Read browser state and validate the browser process is alive with CDP ready.

    Returns the CDP port. Raises if the browser is not running.
    """
    if not state_file.exists():
        raise FileNotFoundError(
            f"No browser state found at {state_file}. "
            "Start the login browser first: scripts/open_browser_for_login.sh"
        )
    payload = read_browser_state(state_file)
    pid = int(payload.get("pid") or 0)
    port = int(payload.get("port") or 0)
    if pid <= 0 or port <= 0:
        raise RuntimeError(f"Invalid browser state: pid={pid}, port={port}")
    if not process_is_running(pid):
        raise RuntimeError(f"Browser process (pid={pid}) is not running. Restart the login browser.")
    if not cdp_is_ready(port):
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