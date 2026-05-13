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


def recover_browser_state(state_file: Path, plugin_root: Path) -> Optional[int]:
    """Try to recover browser state by scanning for a running CDP-enabled browser.

    Looks for processes matching the project's --user-data-dir and extracts the
    CDP port from their command line. If found and CDP is responsive, rewrites
    the state file and returns the port.
    """
    import subprocess

    browser_profile_dir = plugin_root / ".browser-profile"
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
        import re
        m = re.search(r"--remote-debugging-port=(\d+)", cmd)
        if not m:
            continue
        port = int(m.group(1))

        if not cdp_is_ready(port):
            continue

        # Determine browser executable from command line
        executable = ""
        for name, path in DEFAULT_BROWSER_CANDIDATES.items():
            if path in cmd:
                executable = path
                break

        # Found a live browser — rebuild state file
        payload = {
            "port": port,
            "pid": pid,
            "browserExecutable": executable,
            "browserProfileDir": str(browser_profile_dir),
            "targetUrl": DEFAULT_TARGET_URL,
            "startedAt": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat().replace("+00:00", "Z"),
        }
        write_browser_state(state_file, payload)
        print(f"  [RECOVERED] Browser state rebuilt: pid={pid}, port={port}")
        return port

    return None


def ensure_cdp_browser_running(state_file: Path) -> int:
    """Read browser state and validate the browser process is alive with CDP ready.

    If the state file is missing or stale, attempts to recover by scanning for
    a running CDP browser with the project's user-data-dir.

    Returns the CDP port. Raises if the browser is not running.
    """
    # Try to recover from a running browser even if state file is missing/stale
    plugin_root = state_file.parent.parent  # .runtime/ -> project root

    if not state_file.exists():
        print("  Browser state file not found, attempting recovery...")
        port = recover_browser_state(state_file, plugin_root)
        if port:
            return port
        raise FileNotFoundError(
            f"No browser state found at {state_file} and no running browser detected. "
            "Start the login browser first: scripts/open_browser_for_login.sh"
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