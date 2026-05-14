#!/usr/bin/env python3
"""Browser keepalive: periodically refresh the DMS page to prevent session timeout.

Checks for an export lock file before each refresh — skips refresh while
the crawler is exporting data so the download is not interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

from dfmc_browser_utils import (
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    get_default_state_file,
    get_runtime_dir,
)


EXPORT_LOCK_NAME = "exporting.lock"


def _write_status(status_file: Path, payload: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep the DMS browser alive by periodically refreshing.")
    parser.add_argument("--state-file", default="", help="Path to browser-state.json")
    parser.add_argument("--status-file", default="", help="Path to keepalive-state.json")
    parser.add_argument("--interval", type=int, default=300, help="Refresh interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--once", action="store_true", help="Refresh once and exit (for testing)")
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else get_default_state_file(plugin_root)
    runtime_dir = get_runtime_dir(plugin_root)
    lock_file = runtime_dir / EXPORT_LOCK_NAME
    status_file = Path(args.status_file).expanduser().resolve() if args.status_file else runtime_dir / "keepalive-state.json"
    started_at = int(time.time())

    cdp_port = ensure_cdp_browser_running(state_file)
    print(f"Browser alive on CDP port {cdp_port}. Interval: {args.interval}s")
    print(f"Lock file: {lock_file} (refresh will be skipped while lock exists)")
    _write_status(status_file, {
        "pid": os.getpid(),
        "interval": args.interval,
        "startedAt": started_at,
        "lastResult": "starting",
        "lastActionAt": 0,
        "nextRefreshAt": started_at + args.interval,
    })

    should_stop = False

    def request_stop(signum: int, _: object) -> None:
        nonlocal should_stop
        print(f"Received signal {signum}, stopping keepalive...")
        should_stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with sync_playwright() as pw:
        browser = connect_browser_over_cdp(pw, cdp_port)
        context = browser.contexts[0]

        cycle = 0
        while not should_stop:
            cycle += 1
            try:
                if not browser.is_connected():
                    print("Browser disconnected, exiting.")
                    break

                # Check export lock — skip refresh while crawler is downloading
                last_result = "not_found"
                if lock_file.exists():
                    print(f"[{cycle}] Export in progress (lock detected), skipping refresh")
                    last_result = "skipped_locked"
                else:
                    # Find a DMS page to refresh
                    refreshed = False
                    for page in context.pages:
                        try:
                            url = page.url
                            if "m-dms.dfmc.com.cn" in url and url != "about:blank":
                                page.reload(wait_until="domcontentloaded", timeout=10_000)
                                print(f"[{cycle}] Refreshed: {url[:80]}")
                                refreshed = True
                                last_result = "refreshed"
                                break
                        except Error as exc:
                            print(f"[{cycle}] Refresh error on tab: {exc}")
                            last_result = f"error: {exc}"

                    if not refreshed:
                        print(f"[{cycle}] No DMS page found among {len(context.pages)} tabs")

                next_refresh_at = int(time.time()) + args.interval
                _write_status(status_file, {
                    "pid": os.getpid(),
                    "interval": args.interval,
                    "startedAt": started_at,
                    "lastResult": last_result,
                    "lastActionAt": int(time.time()),
                    "nextRefreshAt": next_refresh_at,
                    "cycle": cycle,
                })

                if args.once:
                    break

                # Sleep in short intervals so SIGINT is caught promptly
                sleep_end = time.monotonic() + args.interval
                while time.monotonic() < sleep_end and not should_stop:
                    time.sleep(1)

            except Error:
                print("Browser error, exiting.")
                break

    print("Keepalive stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
