#!/usr/bin/env python3
"""Browser keepalive: briefly attach, navigate to a clean DMS URL, then disconnect.

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

from playwright.sync_api import Error, sync_playwright

from dfmc_browser_utils import (
    DMS_CLEAN_URL,
    collect_page_hints,
    connect_browser_over_cdp,
    dms_session_hint,
    ensure_cdp_browser_running,
    ensure_default_crawl_schedule,
    find_dms_page,
    get_crawl_schedule_path,
    get_default_state_file,
    get_runtime_dir,
    get_session_home,
    load_crawl_schedule,
    ensure_dms_tab,
    refresh_block_reason,
    wait_for_dms_session,
)


def _write_status(status_file: Path, payload: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_dms(plugin_root: Path, cdp_port: int, *, wait_sso: bool) -> tuple[str, str]:
    """Return (last_result, block_reason). Never holds CDP after return."""
    block_reason = refresh_block_reason(plugin_root)
    if block_reason:
        print(f"Skip refresh ({block_reason})")
        return f"skipped:{block_reason}", block_reason

    hints = wait_for_dms_session(cdp_port, 45.0) if wait_sso else collect_page_hints(cdp_port)
    if hints.get("hint") in {"login", "sso"}:
        print(f"Need login (hint={hints.get('hint')})")
        return "need_login", ""

    if not hints.get("has_dms"):
        print("No DMS tab, opening clean URL")
        if not ensure_dms_tab(cdp_port):
            print("Failed to open DMS tab")
            return "not_found", ""
        time.sleep(2)
        hints = collect_page_hints(cdp_port)
        if hints.get("hint") in {"login", "sso"}:
            print(f"Need login after open (hint={hints.get('hint')})")
            return "need_login", ""
        if not hints.get("has_dms"):
            print("DMS tab still missing after open")
            return "not_found", ""

    with sync_playwright() as pw:
        browser = connect_browser_over_cdp(pw, cdp_port)
        try:
            if not browser.contexts:
                print("No browser context")
                return "not_found", ""
            context = browser.contexts[0]
            page = find_dms_page(context)
            if page is None:
                print("Playwright did not see DMS page, opening tab")
                ensure_dms_tab(cdp_port)
                time.sleep(1)
                page = find_dms_page(context)
            if page is None:
                print("No DMS page found among Playwright tabs")
                return "not_found", ""

            url = page.url or ""
            hint = dms_session_hint(url)
            if hint in {"login", "sso"}:
                print(f"Need login on tab: {url[:80]}")
                return "need_login", ""

            target = DMS_CLEAN_URL
            page.goto(target, wait_until="domcontentloaded", timeout=10_000)
            print(f"Navigated: {target[:80]}")
            return "refreshed", ""
        finally:
            # Do not browser.close() — that can shut down the shared Chromium.
            pass

    return "not_found", ""


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
    status_file = Path(args.status_file).expanduser().resolve() if args.status_file else runtime_dir / "keepalive-state.json"
    started_at = int(time.time())
    schedule_path = ensure_default_crawl_schedule(plugin_root)
    schedule = load_crawl_schedule(plugin_root)

    print(f"Session home: {get_session_home(plugin_root)}")
    print(f"Interval: {args.interval}s")
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
            last_result, block_reason = _refresh_dms(plugin_root, cdp_port, wait_sso=(cycle == 1))
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
        while time.monotonic() < sleep_end and not should_stop:
            time.sleep(1)

    print("Keepalive stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
