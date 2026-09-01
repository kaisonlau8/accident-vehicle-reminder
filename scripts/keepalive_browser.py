#!/usr/bin/env python3
"""Browser keepalive: briefly attach, open https://m-dms.dfmc.com.cn/, then disconnect.

Same action as typing the origin URL in the address bar (full document jump + SSO).
Hash routes like #/dashboard are not used — they do not reload the document.

Skips refresh when:
- exporting.lock is held,
- a crawl is registered on crawl_registry.json,
- or the shared crawl_schedule.json blackout window is active
  (from schedule_time - pre_minutes until crawl finishes / await timeout).

Does not hold a Playwright CDP connection between cycles.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

from playwright.sync_api import Error

from dfmc_browser_utils import (
    ensure_cdp_browser_running,
    ensure_default_crawl_schedule,
    get_crawl_schedule_path,
    get_default_state_file,
    get_runtime_dir,
    get_session_home,
    hard_refresh_dms,
    load_crawl_schedule,
    observe_dms_session,
    refresh_block_reason,
)


def _write_status(status_file: Path, payload: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_dms(plugin_root: Path, cdp_port: int) -> tuple[str, str]:
    """Return (last_result, block_reason). Never holds CDP after return."""
    block_reason = refresh_block_reason(plugin_root)
    if block_reason:
        print(f"Skip refresh ({block_reason})")
        return f"skipped:{block_reason}", block_reason

    result = hard_refresh_dms(plugin_root, port=cdp_port, label="keepalive")
    hint = result.split(":", 1)[-1] if ":" in result else result
    if result.startswith("skipped") or result.startswith("error"):
        return result, ""
    if hint in {"login", "sso"}:
        print(f"Need login after origin refresh (hint={hint})")
        return "need_login", ""
    if hint == "ok":
        return "refreshed", ""
    return result, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep the DMS browser alive by periodically opening the origin URL.")
    parser.add_argument("--state-file", default="", help="Path to browser-state.json")
    parser.add_argument("--status-file", default="", help="Path to keepalive-state.json")
    parser.add_argument("--interval", type=int, default=300, help="Refresh interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--once", action="store_true", help="Refresh once and exit (for testing)")
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else get_default_state_file(plugin_root)
    runtime_dir = get_runtime_dir(plugin_root)
    status_file = Path(args.status_file).expanduser().resolve() if args.status_file else runtime_dir / "keepalive-state.json"
    started_at = int(time.time())
    schedule_path = ensure_default_crawl_schedule(plugin_root)
    schedule = load_crawl_schedule(plugin_root)

    print(f"Session home: {get_session_home(plugin_root)}")
    print(f"Interval: {args.interval}s")
    print("Hard refresh: https://m-dms.dfmc.com.cn/")
    print(
        f"Crawl schedule: {schedule_path} "
        f"(pre={schedule.get('pre_minutes')}m, await={schedule.get('await_start_minutes')}m, "
        f"entries={len(schedule.get('entries') or [])})"
    )
    _write_status(status_file, {
        "pid": os.getpid(),
        "interval": args.interval,
        "startedAt": started_at,
        "lastResult": "starting",
        "lastActionAt": 0,
        "nextRefreshAt": started_at + args.interval,
        "scheduleFile": str(get_crawl_schedule_path(plugin_root)),
    })

    should_stop = False

    def request_stop(signum: int, _: object) -> None:
        nonlocal should_stop
        print(f"Received signal {signum}, stopping keepalive...")
        should_stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    cycle = 0
    while not should_stop:
        cycle += 1
        last_result = "error"
        block_reason = ""
        try:
            cdp_port = ensure_cdp_browser_running(state_file, plugin_root=plugin_root)
            last_result, block_reason = _refresh_dms(plugin_root, cdp_port)
        except Error as exc:
            print(f"[{cycle}] Playwright error: {exc}")
            last_result = f"error: {exc}"
        except Exception as exc:
            print(f"[{cycle}] Keepalive error: {exc}")
            last_result = f"error: {exc}"

        next_refresh_at = int(time.time()) + args.interval
        _write_status(status_file, {
            "pid": os.getpid(),
            "interval": args.interval,
            "startedAt": started_at,
            "lastResult": last_result,
            "lastActionAt": int(time.time()),
            "nextRefreshAt": next_refresh_at,
            "cycle": cycle,
            "blockReason": block_reason or "",
        })
        print(f"[{cycle}] result={last_result}")

        if args.once:
            break

        sleep_end = time.monotonic() + args.interval
        next_observe = time.monotonic() + 30
        while time.monotonic() < sleep_end and not should_stop:
            time.sleep(1)
            if time.monotonic() >= next_observe:
                next_observe = time.monotonic() + 30
                try:
                    observe_dms_session(plugin_root, source="keepalive-poll")
                except Exception as exc:
                    print(f"  [WARN] session monitor: {exc}")

    print("Keepalive stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
