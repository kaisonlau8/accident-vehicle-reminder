#!/usr/bin/env python3
"""Crawl maintenance order (维修工单) data from the DFMC DMS system.

Unlike the complaint crawler which works day-by-day, this crawler does a
single export with a 30-day arrival-date range, producing one Excel file.

Usage:
  python3 crawl_maintenance_orders.py --days 30
  python3 crawl_maintenance_orders.py --days 30 --keepalive
  python3 crawl_maintenance_orders.py --days 30 --dry-run
"""

from __future__ import annotations

import argparse
import atexit
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Error, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dfmc_browser_utils import (
    DEFAULT_TARGET_URL,
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
    get_default_state_file,
    get_runtime_dir,
)

MAINTENANCE_ORDER_ROUTE = "/aftermarketMange/maintenanceManagement/maintenanceOrderSearch"
DMS_HOST = "m-dms.dfmc.com.cn"
EXPORT_LOCK_NAME = "exporting.lock"


def date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ── Lock file for keepalive coordination ─────────────────────────────


def acquire_export_lock(plugin_root: Path) -> Path:
    lock_file = get_runtime_dir(plugin_root) / EXPORT_LOCK_NAME
    lock_file.write_text(f"locked at {time.strftime('%Y-%m-%dT%H:%M:%S')}\n", encoding="utf-8")
    return lock_file


def release_export_lock(lock_file: Path) -> None:
    lock_file.unlink(missing_ok=True)


# ── Page interaction helpers ─────────────────────────────────────────


def navigate_to_maintenance_order_page(page: Page) -> None:
    """Navigate to the maintenance order search page via hash routing."""
    try:
        page.evaluate(f"window.location.hash = '{MAINTENANCE_ORDER_ROUTE}'")
    except Error:
        current_url = page.url
        if "?code=" in current_url:
            code = current_url.split("?code=")[1].split("#")[0].split("&")[0]
            target = f"https://{DMS_HOST}/?code={code}#{MAINTENANCE_ORDER_ROUTE}"
            page.goto(target, wait_until="domcontentloaded", timeout=15_000)
        else:
            page.goto(f"https://{DMS_HOST}#{MAINTENANCE_ORDER_ROUTE}", wait_until="domcontentloaded", timeout=15_000)

    try:
        page.wait_for_selector("section.mixButton", timeout=15_000)
    except Error:
        page.wait_for_selector(".el-table", timeout=15_000)
    page.wait_for_timeout(1_000)


def expand_filter_fields(page: Page) -> None:
    """Click the '展开/更多' button to reveal hidden filter fields (date range, business type, etc.).

    Waits for the moreParam button to appear (it may load asynchronously
    after the SPA route change).
    """
    # Wait for moreParam button to appear
    try:
        page.wait_for_selector("button.moreParam", timeout=5_000)
    except Error:
        print("  moreParam button not found within 5s, filters may already be expanded")
        return

    btn = page.locator("button.moreParam")
    if btn.count() == 0:
        print("  moreParam button not found, filters may already be expanded")
        return

    classes = btn.evaluate("el => el.className")
    if "circle-arrow-down" in classes:
        btn.click()
        try:
            page.wait_for_selector("div#datePicker", timeout=5_000)
        except Error:
            page.wait_for_selector(".el-date-editor--daterange", timeout=5_000)
        page.wait_for_timeout(500)
        print("  Filter fields expanded")
    else:
        print("  moreParam already expanded, skipping click")


def select_accident_business_type(page: Page) -> None:
    """Select '事故维修' in the 业务类型 (business type) multi-select dropdown.

    This filters the DMS results to only accident-related maintenance orders,
    significantly reducing export size and time.
    Value for '事故维修' is "7" in the DMS system.
    """
    # Find the 业务类型 select by checking each select's parent context
    result = page.evaluate("""() => {
        const selects = document.querySelectorAll('.el-select.el-select--small:not(.header-search-select)');
        for (const sel of selects) {
            let el = sel;
            for (let i = 0; i < 8; i++) {
                el = el.parentElement;
                if (!el) break;
                const text = el.textContent;
                if (text.includes('业务类型') && !text.includes('维修状态')) {
                    const v = sel.__vue__;
                    if (v && v.multiple) {
                        // Set value to ["7"] (事故维修)
                        v.value = ["7"];
                        v.$emit('input', ["7"]);
                        v.$emit('change', ["7"]);
                        // Also set the tag display
                        if (v.$children && v.$children[0]) {
                            v.$children[0].value = ["7"];
                            v.$children[0].$emit('input', ["7"]);
                            v.$children[0].$emit('change', ["7"]);
                        }
                        return 'set_via_vue';
                    }
                    return 'not_multiple';
                }
            }
        }
        return 'not_found';
    }""")

    if result == "set_via_vue":
        page.wait_for_timeout(500)
        # Verify the selection was applied
        verify = page.evaluate("""() => {
            const selects = document.querySelectorAll('.el-select.el-select--small:not(.header-search-select)');
            for (const sel of selects) {
                let el = sel;
                for (let i = 0; i < 8; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (el.textContent.includes('业务类型') && !el.textContent.includes('维修状态')) {
                        const v = sel.__vue__;
                        return {
                            vueValue: JSON.stringify(v.value),
                            inputText: sel.querySelector('input')?.value || '',
                            tags: Array.from(sel.querySelectorAll('.el-tag')).map(t => t.textContent.trim()),
                        };
                    }
                }
            }
            return {};
        }""")
        tag_text = verify.get("tags", [])
        vue_val = verify.get("vueValue", "")
        if vue_val == '["7"]' or "事故维修" in str(tag_text):
            print(f"  业务类型 set to 事故维修 (tags: {tag_text})")
            return
        # Vue set didn't fully propagate, try click-based approach
        print(f"  Vue set incomplete (vue={vue_val}, tags={tag_text}), trying click approach...")
    else:
        print(f"  Vue injection result: {result}, trying click approach...")

    # Click-based approach: find the select, click to open dropdown, click "事故维修" option
    selects = page.locator(".el-select.el-select--small:not(.header-search-select)")
    for i in range(selects.count()):
        sel = selects.nth(i)
        context_text = sel.evaluate("""el => {
            let parent = el;
            for (let j = 0; j < 8; j++) {
                parent = parent.parentElement;
                if (!parent) break;
                if (parent.textContent.includes('业务类型') && !parent.textContent.includes('维修状态')) return true;
            }
            return false;
        }""")
        if context_text:
            sel.locator("input").click()
            page.wait_for_timeout(500)
            # Click the "事故维修" option
            options = page.locator(".el-select-dropdown:visible .el-select-dropdown__item")
            for j in range(options.count()):
                try:
                    text = options.nth(j).text_content()
                    if text and "事故维修" in text:
                        options.nth(j).click()
                        page.wait_for_timeout(300)
                        print("  业务类型 set to 事故维修 via click")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                        return
                except Error:
                    continue
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            break


def set_arrival_date_range(page: Page, start_date: date, end_date: date) -> None:
    """Set the arrival date (到店日期) range filter.

    Strategy: calendar click first (simulates real user interaction, most reliable),
    Vue model injection as fallback. After setting, verifies the displayed values
    match the intended range.
    """
    start_str = date_str(start_date)
    end_str = date_str(end_date)

    # Strategy 1: calendar click (most reliable — simulates real user)
    try:
        _set_date_range_via_calendar(page, start_date, end_date)
        # Verify the date values are correct
        if _verify_date_range(page, start_str, end_str):
            print(f"  Date range set via calendar click: {start_str} ~ {end_str}")
            return
        print("  Calendar click succeeded but values don't match, trying Vue injection...")
    except Exception as exc:
        print(f"  Calendar click failed ({exc}), trying Vue injection...")

    # Strategy 2: Vue model injection (fallback)
    success = _set_date_range_via_vue(page, start_str, end_str)
    if success:
        page.wait_for_timeout(1_000)  # Wait for Vue reactivity to propagate
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        if _verify_date_range(page, start_str, end_str):
            print(f"  Date range set via Vue injection: {start_str} ~ {end_str}")
            return
        print("  Vue injection succeeded but values don't match")

    raise RuntimeError(f"Failed to set date range to {start_str} ~ {end_str}")


def _verify_date_range(page: Page, expected_start: str, expected_end: str) -> bool:
    """Verify the date picker currently shows the expected range values."""
    try:
        result = page.evaluate("""([expectedStart, expectedEnd]) => {
            const startInput = document.querySelector('input.el-range-input[placeholder="开始日期"]');
            const endInput = document.querySelector('input.el-range-input[placeholder="结束日期"]');
            const rangeEditor = document.querySelector('.el-date-editor--daterange');

            const startVal = startInput ? startInput.value : '';
            const endVal = endInput ? endInput.value : '';
            const vueVal = rangeEditor && rangeEditor.__vue__
                ? JSON.stringify(rangeEditor.__vue__.value) : '';

            return {
                startInput: startVal,
                endInput: endVal,
                vueValue: vueVal,
                match: startVal === expectedStart && endVal === expectedEnd
            };
        }""", [expected_start, expected_end])
        if result.get("match"):
            return True
        print(f"  Verification: start={result.get('startInput')}, end={result.get('endInput')}, vue={result.get('vueValue')}")
        return False
    except Error:
        return False


def _set_date_range_via_vue(page: Page, start_str: str, end_str: str) -> bool:
    """Try to set date range by injecting into the Vue component's data model.

    Uses multiple strategies (A-D) to find and manipulate the date picker,
    with generic Element UI selectors as fallbacks.
    """
    js_code = """
    ([startStr, endStr]) => {
        // Try #datePicker first (specific), then generic Element UI daterange selector
        const datePicker = document.querySelector('#datePicker')
            || document.querySelector('.el-date-editor--daterange')
            || document.querySelector('.el-form-item .el-date-editor');

        if (!datePicker) return false;

        // Strategy A: __vue__ on the datePicker wrapper
        const vueComp = datePicker.__vue__;
        if (vueComp) {
            if (vueComp.value !== undefined) {
                vueComp.value = [startStr, endStr];
                vueComp.$emit('input', [startStr, endStr]);
                vueComp.$emit('change', [startStr, endStr]);
                return true;
            }
            for (const child of vueComp.$children || []) {
                if (child.value !== undefined || child.pickerVisible !== undefined) {
                    child.value = [startStr, endStr];
                    child.$emit('input', [startStr, endStr]);
                    child.$emit('change', [startStr, endStr]);
                    return true;
                }
            }
        }

        // Strategy B: __vue__ on the range editor element
        const rangeEditor = datePicker.querySelector('.el-date-editor--daterange')
            || datePicker.querySelector('.el-range-editor');
        if (rangeEditor && rangeEditor.__vue__) {
            const comp = rangeEditor.__vue__;
            comp.value = [startStr, endStr];
            comp.$emit('input', [startStr, endStr]);
            comp.$emit('change', [startStr, endStr]);
            return true;
        }

        // Strategy C: find __vue__ anywhere within datePicker
        const allEls = datePicker.querySelectorAll('*');
        for (const el of allEls) {
            if (el.__vue__ && el.__vue__.value !== undefined) {
                const comp = el.__vue__;
                comp.value = [startStr, endStr];
                comp.$emit('input', [startStr, endStr]);
                comp.$emit('change', [startStr, endStr]);
                return true;
            }
        }

        // Strategy D: directly set input values and dispatch events
        const startInput = datePicker.querySelector('input.el-range-input[placeholder="开始日期"]')
            || datePicker.querySelector('input.el-range-input:first-of-type');
        const endInput = datePicker.querySelector('input.el-range-input[placeholder="结束日期"]')
            || datePicker.querySelector('input.el-range-input:last-of-type');
        if (startInput && endInput) {
            const inputs = [startInput, endInput];
            const values = [startStr, endStr];
            for (let i = 0; i < inputs.length; i++) {
                const input = inputs[i];
                input.removeAttribute('readonly');
                input.value = values[i];
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.setAttribute('readonly', 'readonly');
            }
            return true;
        }

        return false;
    }
    """
    try:
        result = page.evaluate(js_code, [start_str, end_str])
        return bool(result)
    except Error:
        return False


def _set_date_range_via_calendar(page: Page, start_date: date, end_date: date) -> None:
    """Set date range by clicking calendar cells in the Element UI date picker popup."""
    start_input = page.locator("input.el-range-input[placeholder='开始日期']")
    if start_input.count() == 0:
        start_input = page.locator("#datePicker .el-range-input").first
    if start_input.count() == 0:
        start_input = page.locator(".el-date-editor--daterange .el-range-input").first

    start_input.click()
    page.wait_for_timeout(500)

    picker = page.locator(".el-date-range-picker")
    try:
        picker.wait_for(state="visible", timeout=5_000)
    except Error:
        picker = page.locator(".el-picker-panel")
        picker.wait_for(state="visible", timeout=5_000)

    _navigate_picker_month(page, picker, start_date, panel="left")
    _click_date_cell(page, picker, start_date, panel="left")
    page.wait_for_timeout(300)

    _navigate_picker_month(page, picker, end_date, panel="right")
    _click_date_cell(page, picker, end_date, panel="right")
    page.wait_for_timeout(300)

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


def _navigate_picker_month(page: Page, picker: Any, target_date: date, panel: str) -> None:
    """Navigate the picker popup to show the month of target_date in the specified panel."""
    header = picker.locator(f".el-picker-panel__body:nth-child({1 if panel == 'left' else 2}) .el-date-picker__header")
    if header.count() == 0:
        return

    target_year, target_month = target_date.year, target_date.month

    for _ in range(24):
        try:
            year_text = header.locator(".el-date-picker__header-label").first.text_content()
            month_text = header.locator(".el-date-picker__header-label").nth(1).text_content()
            current_year = int(year_text.strip())
            current_month = _parse_chinese_month(month_text.strip())
        except Error:
            break

        if current_year == target_year and current_month == target_month:
            break

        if (current_year, current_month) > (target_year, target_month):
            prev_btn = header.locator(".el-picker-panel__icon-btn.el-icon-arrow-left").first
            if prev_btn.count() > 0:
                prev_btn.click()
            else:
                prev_btn = header.locator(".el-picker-panel__icon-btn.el-icon-double-arrow-left").first
                prev_btn.click()
        else:
            next_btn = header.locator(".el-picker-panel__icon-btn.el-icon-arrow-right").first
            if next_btn.count() > 0:
                next_btn.click()
            else:
                next_btn = header.locator(".el-picker-panel__icon-btn.el-icon-double-arrow-right").first
                next_btn.click()

        page.wait_for_timeout(200)


def _parse_chinese_month(text: str) -> int:
    """Parse month text from Element UI picker header."""
    import re
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    chinese_months = {"一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
                      "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12}
    for cn, num in chinese_months.items():
        if cn in text:
            return num
    return 1


def _click_date_cell(page: Page, picker: Any, target_date: date, panel: str) -> None:
    """Click the date cell for target_date.day in the picker panel."""
    import re
    day = target_date.day
    panel_idx = 1 if panel == "left" else 2

    panel_body = picker.locator(f".el-picker-panel__body:nth-child({panel_idx})")
    date_table = panel_body.locator(".el-date-table")

    cells = date_table.locator("td.current-month span, td span")
    for i in range(cells.count()):
        try:
            text = cells.nth(i).text_content()
            if text and text.strip() == str(day):
                cells.nth(i).click()
                return
        except Error:
            continue

    all_cells = picker.locator(f".el-picker-panel__body:nth-child({panel_idx}) .el-date-table td span")
    for i in range(all_cells.count()):
        try:
            text = all_cells.nth(i).text_content()
            if text and text.strip() == str(day):
                all_cells.nth(i).click()
                return
        except Error:
            continue


def click_query(page: Page) -> None:
    """Click the 查询 (query) button and wait for results to load."""
    clicked = False

    query_btns = page.locator("section.mixButton button")
    for i in range(query_btns.count()):
        try:
            text = query_btns.nth(i).locator("span").text_content()
            if text and "查询" in text:
                query_btns.nth(i).click()
                clicked = True
                break
        except Error:
            continue

    if not clicked:
        for selector in [
            "section.mixButton button[comp-key='btnKey2']",
            "section.mixButton button[comp-key='btnKey1']",
        ]:
            btn = page.locator(selector)
            if btn.count() == 1:
                btn.click()
                clicked = True
                break

    try:
        page.wait_for_selector(".el-table__body-wrapper tbody tr", timeout=15_000)
    except Error:
        pass
    page.wait_for_timeout(1_000)


def click_export_and_capture(page: Page, cdp_session: Any, output_dir: Path, today: date) -> Optional[Path]:
    """Click the export button and capture the downloaded Excel via filesystem polling.

    Unlike the complaint crawler which exports day-by-day, this does a single
    export for the full date range. Timeout is generous (180s) since 30 days
    of data may be a large file.
    """
    filename = f"maintenance_orders_{date_str(today)}.xlsx"
    save_path = output_dir / filename

    # Snapshot existing files before export
    before_files = {f.name: f.stat().st_mtime for f in output_dir.iterdir() if f.is_file()}

    # Set download directory via CDP
    cdp_session.send("Browser.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": str(output_dir),
    })

    export_btn = None

    # Strategy 1: find by text "导出"
    all_btns = page.locator("section.mixButton button")
    for i in range(all_btns.count()):
        try:
            text = all_btns.nth(i).locator("span").text_content()
            if text and "导出" in text:
                export_btn = all_btns.nth(i)
                break
        except Error:
            continue

    # Strategy 2: comp-key selectors
    if export_btn is None:
        for selector in [
            "section.mixButton button[comp-key='export']",
            "section.mixButton button[comp-key='btnKey3']",
            "section.mixButton button[comp-key='btnKey1']",
            "section.mixButton div.u-btn-left button.el-button--small",
        ]:
            btn = page.locator(selector)
            if btn.count() == 1:
                export_btn = btn.first
                break

    if export_btn is None:
        print("  Export button not found")
        return None

    export_btn.click()
    print("  Clicked export, waiting for file download...")

    # Poll filesystem for a new file — 180s timeout for large 30-day export
    deadline = time.monotonic() + 180
    new_file: Optional[Path] = None
    stable_size: Optional[int] = None
    stable_since: float = 0.0

    while time.monotonic() < deadline:
        time.sleep(0.5)

        current_files = list(output_dir.iterdir())
        for f in current_files:
            if not f.is_file():
                continue
            if f.name in before_files:
                try:
                    if f.stat().st_mtime <= before_files[f.name]:
                        continue
                except OSError:
                    continue
            # Skip manifest files
            if f.name.startswith("crawl_manifest"):
                continue

            try:
                size = f.stat().st_size
            except OSError:
                continue

            if size == 0:
                continue

            if new_file is None or f != new_file:
                new_file = f
                stable_size = size
                stable_since = time.monotonic()
                continue

            if size == stable_size and time.monotonic() - stable_since >= 2.0:
                break
        else:
            continue
        break

    if new_file is None:
        print("  No new file detected within 180s")
        return None

    # Rename to canonical name
    try:
        if new_file != save_path:
            new_file.rename(save_path)
        print(f"  Saved: {save_path}")
        return save_path
    except OSError as exc:
        print(f"  Rename failed ({exc}), using original name: {new_file}")
        return new_file


def reset_filters(page: Page) -> None:
    """Click the 重置 (reset) button to clear previous date filter."""
    reset_btns = page.locator("section.mixButton button")
    for i in range(reset_btns.count()):
        try:
            text = reset_btns.nth(i).locator("span").text_content()
            if text and "重置" in text:
                reset_btns.nth(i).click()
                page.wait_for_timeout(500)
                return
        except Error:
            continue


def validate_logged_in(page: Page) -> None:
    """Check that the browser is on a logged-in DMS page, not on the login screen."""
    url = page.url
    if DMS_HOST not in url:
        raise RuntimeError(f"Browser not on DMS site. Current URL: {url}")
    if "/login" in url.lower():
        raise RuntimeError("Browser is on the login page. Log in first via open_browser_for_login.sh")
    pw_inputs = page.locator("input[type='password']")
    if pw_inputs.count() > 0:
        raise RuntimeError("Login page detected (password input found). Log in first.")


# ── Main crawler ────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl maintenance order data from DFMC DMS.")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back (default: 30)")
    parser.add_argument("--output-dir", default="", help="Directory to save Excel file (default: <plugin_root>/output)")
    parser.add_argument("--state-file", default="", help="Path to browser-state.json")
    parser.add_argument("--keepalive", action="store_true", help="Start browser keepalive in background before crawling")
    parser.add_argument("--dry-run", action="store_true", help="Navigate and set filters but don't export")
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else get_default_state_file(plugin_root)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else plugin_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    today = date.today()
    start_date = today - timedelta(days=args.days)
    end_date = today
    print(f"Date range: {date_str(start_date)} to {date_str(end_date)} ({args.days} days)")

    # Validate browser
    cdp_port = ensure_cdp_browser_running(state_file)
    print(f"Browser CDP port: {cdp_port}")

    # Optionally start keepalive
    keepalive_proc: Optional[subprocess.Popen[Any]] = None
    if args.keepalive:
        python_bin = str((plugin_root / ".venv" / "bin" / "python3").resolve())
        keepalive_script = str((plugin_root / "scripts" / "keepalive_browser.py").resolve())
        keepalive_proc = subprocess.Popen(
            [python_bin, keepalive_script, "--state-file", str(state_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"Keepalive started (pid={keepalive_proc.pid})")
        atexit.register(lambda: keepalive_proc.terminate() if keepalive_proc and keepalive_proc.poll() is None else None)

    filepath: Optional[Path] = None
    status = "unknown"

    try:
        with sync_playwright() as pw:
            browser = connect_browser_over_cdp(pw, cdp_port)
            context = browser.contexts[0]
            context.set_default_timeout(10_000)

            # Find or create DMS page
            page = find_dms_page(context)
            if page is None:
                page = context.new_page()
                page.goto(DEFAULT_TARGET_URL, wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2_000)

            validate_logged_in(page)

            # Navigate to maintenance order page
            print("Navigating to maintenance order page...")
            navigate_to_maintenance_order_page(page)
            print("Maintenance order page loaded.")

            # Expand filter fields
            expand_filter_fields(page)

            # Select accident business type filter
            select_accident_business_type(page)

            # Create CDP session for download handling
            cdp_session = context.new_cdp_session(page)

            # Acquire export lock so keepalive won't refresh during download
            lock_file = acquire_export_lock(plugin_root)

            try:
                # Set arrival date range
                print(f"Setting date range: {date_str(start_date)} ~ {date_str(end_date)}")
                set_arrival_date_range(page, start_date, end_date)

                if args.dry_run:
                    print("[DRY RUN] Would export maintenance orders for this date range")
                    status = "dry_run"
                else:
                    # Click query
                    click_query(page)

                    # Post-query verification: confirm date range still applied
                    if not _verify_date_range(page, date_str(start_date), date_str(end_date)):
                        print("  [WARN] Date range reset after query! Re-setting...")
                        set_arrival_date_range(page, start_date, end_date)
                        click_query(page)

                    # Click export and capture download
                    filepath = click_export_and_capture(page, cdp_session, output_dir, today)
                    if filepath:
                        status = "ok"
                    else:
                        status = "export_failed"

                        # Retry once
                        try:
                            print("  Retrying...")
                            page.wait_for_timeout(3_000)
                            reset_filters(page)
                            page.wait_for_timeout(300)
                            expand_filter_fields(page)
                            select_accident_business_type(page)
                            set_arrival_date_range(page, start_date, end_date)
                            click_query(page)
                            filepath = click_export_and_capture(page, cdp_session, output_dir, today)
                            if filepath:
                                status = "retried_ok"
                            else:
                                status = "retry_failed"
                        except Exception as retry_exc:
                            print(f"  Retry also failed: {retry_exc}")
                            status = "retry_error"

            finally:
                release_export_lock(lock_file)

    except Exception as exc:
        print(f"Fatal error: {exc}")
        status = "fatal_error"
        try:
            release_export_lock(get_runtime_dir(plugin_root) / EXPORT_LOCK_NAME)
        except Exception:
            pass

    # Write manifest
    manifest_path = output_dir / "crawl_manifest.json"
    manifest = {
        "crawledAt": date_str(today),
        "dateRange": f"{date_str(start_date)} ~ {date_str(end_date)}",
        "days": args.days,
        "status": status,
        "file": str(filepath) if filepath else "",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\nCrawl complete: {status}")
    if filepath:
        print(f"Output: {filepath}")
    print(f"Manifest: {manifest_path}")

    return 0 if status in ("ok", "retried_ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())