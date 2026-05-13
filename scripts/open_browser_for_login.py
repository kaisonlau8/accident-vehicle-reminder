#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_TARGET_URL = "https://m-dms.dfmc.com.cn"
DEFAULT_BROWSER_CANDIDATES = {
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
}
DEFAULT_STATE_FILE_NAME = "browser-state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

    raise FileNotFoundError("No supported browser executable was found.")


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


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def wait_for_cdp(port: int, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"CDP port {port} did not become ready: {last_error}")


def cdp_is_ready(port: int) -> bool:
    try:
        wait_for_cdp(port, timeout_seconds=1.0)
        return True
    except RuntimeError:
        return False


def fetch_cdp_json(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def ensure_browser_page(port: int, target_url: str) -> None:
    try:
        targets = fetch_cdp_json(port, "/json/list")
        if any(target.get("type") == "page" for target in targets):
            return
    except Exception:
        return

    try:
        encoded_url = urllib.parse.quote(target_url, safe="")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded_url}",
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def activate_browser_window(browser_executable: Path) -> None:
    app_name = browser_executable.stem
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def state_matches_browser(
    payload: dict[str, Any],
    browser_executable: Path,
    browser_profile_dir: Path,
) -> bool:
    pid = int(payload.get("pid") or 0)
    port = int(payload.get("port") or 0)
    if pid <= 0 or port <= 0:
        return False
    if not process_is_running(pid):
        return False
    if not cdp_is_ready(port):
        return False

    command = get_process_command(pid)
    if not command:
        return False

    required_parts = [
        str(browser_executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={browser_profile_dir}",
    ]
    if not all(part in command for part in required_parts):
        return False

    try:
        version_payload = fetch_cdp_json(port, "/json/version")
    except Exception:
        return False
    if "webSocketDebuggerUrl" not in version_payload:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open a reusable browser for manual login before crawling maintenance orders."
    )
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL)
    parser.add_argument("--browser-profile-dir", default="")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--browser", choices=sorted(DEFAULT_BROWSER_CANDIDATES.keys()), default="chrome")
    parser.add_argument("--browser-executable", default="")
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else get_default_state_file(plugin_root)
    )
    browser_profile_dir = (
        Path(args.browser_profile_dir).expanduser().resolve()
        if args.browser_profile_dir
        else plugin_root / ".browser-profile"
    )
    browser_executable = detect_browser(args.browser, args.browser_executable or None)

    if state_file.exists():
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
            if state_matches_browser(payload, browser_executable, browser_profile_dir):
                port = int(payload["port"])
                ensure_browser_page(port, args.target_url)
                activate_browser_window(browser_executable)
                print(f"Login browser already running. State file: {state_file}")
                print(f"Crawl with: scripts/crawl_maintenance_orders.sh")
                return 0
            state_file.unlink(missing_ok=True)
        except Exception:
            state_file.unlink(missing_ok=True)

    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    port = find_free_port()
    command = [
        str(browser_executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={browser_profile_dir}",
        "--new-window",
        "--no-first-run",
        "--disable-popup-blocking",
        "--window-size=1440,960",
        args.target_url,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for_cdp(port)
    except Exception:
        if process.poll() is None:
            process.terminate()
        raise

    payload = {
        "port": port,
        "pid": process.pid,
        "browserExecutable": str(browser_executable),
        "browserProfileDir": str(browser_profile_dir),
        "targetUrl": args.target_url,
        "startedAt": now_iso(),
    }
    write_browser_state(state_file, payload)

    time.sleep(1)
    activate_browser_window(browser_executable)
    print(f"Opened login browser: {browser_executable}")
    print(f"State file: {state_file}")
    print(f"Profile dir: {browser_profile_dir}")
    print(f"CDP port: {port}")
    print("Log in manually, then run:")
    print(f"  scripts/crawl_maintenance_orders.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())