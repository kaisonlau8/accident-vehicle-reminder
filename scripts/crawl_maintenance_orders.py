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
from time_utils import beijing_strftime, beijing_today

MAINTENANCE_ORDER_ROUTE = "/aftermarketMange/maintenanceManagement/maintenanceOrderSearch"
DMS_HOST = "m-dms.dfmc.com.cn"
EXPORT_LOCK_NAME = "exporting.lock"


def date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ── Lock file for keepalive coordination ─────────────────────────────


def acquire_export_lock(plugin_root: Path) -> Path:
    lock_file = get_runtime_dir(plugin_root) / EXPORT_LOCK_NAME
    lock_file.write_text(f"locked at {beijing_strftime('%Y-%m-%dT%H:%M:%S%z')}\n", encoding="utf-8")
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
    """Click the '展开/open more' button only when the filter area is collapsed.

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

    state = btn.evaluate("""el => ({
        className: el.className || '',
        text: (el.textContent || '').trim().toLowerCase(),
    })""")
    classes = state.get("className", "")
    text = state.get("text", "")

    is_collapsed = (
        "open more" in text
        or "展开" in text
        or "circle-arrow-down" in classes
    )
    is_expanded = (
        "put away" in text
        or "收起" in text
        or "circle-arrow-up" in classes
    )

    if is_collapsed:
        btn.click()
        try:
            page.wait_for_selector("div#datePicker", timeout=5_000)
        except Error:
            page.wait_for_selector(".el-date-editor--daterange", timeout=5_000)
        page.wait_for_timeout(500)
        print("  Filter fields expanded")
    elif is_expanded:
        print("  moreParam already expanded, skipping click")
    else:
        print(f"  moreParam state unclear (text={text!r}, class={classes!r}), skipping click")


def select_accident_business_type(page: Page) -> None:
    """Select '事故维修' in the 业务类型 (business type) multi-select dropdown.

    This filters the DMS results to only accident-related maintenance orders,
    significantly reducing export size and time.
    Value for '事故维修' is "7" in the DMS system, but relying on that raw value
    alone is not enough: the DMS page can render a stale "7" without the actual
    selected label. We therefore require the visible tag text to become "事故维修".
    """
    verify_js = """() => {
        const selects = document.querySelectorAll('.el-select.el-select--small:not(.header-search-select)');
        for (const sel of selects) {
            let el = sel;
            for (let i = 0; i < 8; i++) {
                el = el.parentElement;
                if (!el) break;
                if (el.textContent.includes('业务类型') && !el.textContent.includes('维修状态')) {
                    const v = sel.__vue__;
                    return {
                        vueValue: JSON.stringify(v?.value ?? null),
                        inputText: sel.querySelector('input')?.value || '',
                        tags: Array.from(sel.querySelectorAll('.el-tag')).map(t => t.textContent.trim()),
                    };
                }
            }
        }
        return {};
    }"""

    verify = page.evaluate(verify_js)
    tag_text = verify.get("tags", [])
    if "事故维修" in tag_text:
        print(f"  业务类型 already set to 事故维修 (tags: {tag_text})")
        return

    # Clear any stale injected raw value such as "7" before doing a real click selection.
    page.evaluate("""() => {
        const selects = document.querySelectorAll('.el-select.el-select--small:not(.header-search-select)');
        for (const sel of selects) {
            let el = sel;
            for (let i = 0; i < 8; i++) {
                el = el.parentElement;
                if (!el) break;
                if (el.textContent.includes('业务类型') && !el.textContent.includes('维修状态')) {
                    const comps = [];
                    if (sel.__vue__) comps.push(sel.__vue__);
                    for (const child of sel.querySelectorAll('*')) {
                        if (child.__vue__) comps.push(child.__vue__);
                    }
                    const seen = new Set();
                    for (const comp of comps) {
                        let cur = comp;
                        let depth = 0;
                        while (cur && depth < 6 && !seen.has(cur)) {
                            seen.add(cur);
                            if (cur.value !== undefined) cur.value = [];
                            if (cur.selected !== undefined) cur.selected = [];
                            if (cur.query !== undefined) cur.query = '';
                            if (cur.selectedLabel !== undefined) cur.selectedLabel = '';
                            if (typeof cur.$emit === 'function') {
                                cur.$emit('input', []);
                                cur.$emit('change', []);
                            }
                            cur = cur.$parent;
                            depth += 1;
                        }
                    }
                    const input = sel.querySelector('input');
                    if (input) input.value = '';
                    return true;
                }
            }
        }
        return false;
    }""")
    page.wait_for_timeout(300)

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
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            sel.locator(".el-input").click(force=True)
            page.wait_for_timeout(500)
            # Click the "事故维修" option
            options = page.locator(".el-select-dropdown:visible .el-select-dropdown__item")
            for j in range(options.count()):
                try:
                    text = options.nth(j).text_content()
                    if text and text.strip() == "事故维修":
                        options.nth(j).click(force=True)
                        page.wait_for_timeout(300)
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                        verify = page.evaluate(verify_js)
                        tag_text = verify.get("tags", [])
                        if "事故维修" in tag_text:
                            print(f"  业务类型 set to 事故维修 (tags: {tag_text})")
                            return
                        raise RuntimeError(
                            "业务类型 click completed but visible tag is not 事故维修: "
                            f"{verify}"
                        )
                except Error:
                    continue
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            break

    raise RuntimeError("Failed to select business type 事故维修")


def _verify_accident_business_type(page: Page) -> bool:
    """Verify the business type visibly remains set to 事故维修."""
    try:
        result = page.evaluate("""() => {
            const selects = document.querySelectorAll('.el-select.el-select--small:not(.header-search-select)');
            for (const sel of selects) {
                let el = sel;
                for (let i = 0; i < 8; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (el.textContent.includes('业务类型') && !el.textContent.includes('维修状态')) {
                        const v = sel.__vue__;
                        return {
                            vueValue: JSON.stringify(v?.value ?? null),
                            tags: Array.from(sel.querySelectorAll('.el-tag')).map(t => t.textContent.trim()),
                            visibleText: (el.textContent || '').trim(),
                        };
                    }
                }
            }
            return {};
        }""")
        tags = result.get("tags", [])
        if "事故维修" in tags:
            return True
        print(
            "  Business type verification:"
            f" vue={result.get('vueValue')},"
            f" tags={tags},"
            f" text={result.get('visibleText')}"
        )
        return False
    except Error:
        return False


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
        _sync_date_range_state(page, start_str, end_str)
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
        _sync_date_range_state(page, start_str, end_str)
        page.wait_for_timeout(1_000)  # Wait for Vue reactivity to propagate
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
            const expectedRangeJson = JSON.stringify([expectedStart, expectedEnd]);

            function collectRangeState(comp) {
                const states = [];
                const seen = new Set();
                const rangeKeys = [
                    'repairTimeStart,repairTimeEnd',
                    'arrivalTimeStart,arrivalTimeEnd',
                    'entryDateStart,entryDateEnd',
                    'startDate,endDate',
                ];
                const singleKeyPairs = [
                    ['repairTimeStart', 'repairTimeEnd'],
                    ['arrivalTimeStart', 'arrivalTimeEnd'],
                    ['entryDateStart', 'entryDateEnd'],
                    ['startDate', 'endDate'],
                ];

                function inspectObject(owner, ownerName) {
                    if (!owner || typeof owner !== 'object') return;

                    for (const key of rangeKeys) {
                        if (key in owner) {
                            states.push({
                                owner: ownerName,
                                key,
                                value: JSON.stringify(owner[key]),
                            });
                        }
                    }

                    for (const [startKey, endKey] of singleKeyPairs) {
                        if (startKey in owner && endKey in owner) {
                            states.push({
                                owner: ownerName,
                                key: `${startKey}|${endKey}`,
                                value: JSON.stringify([owner[startKey], owner[endKey]]),
                            });
                        }
                    }
                }

                let current = comp;
                let depth = 0;
                while (current && depth < 8 && !seen.has(current)) {
                    seen.add(current);
                    inspectObject(current, current.$options?.name || `comp-${depth}`);
                    inspectObject(current.formField, `${current.$options?.name || `comp-${depth}`}.formField`);
                    inspectObject(current.queryForm, `${current.$options?.name || `comp-${depth}`}.queryForm`);
                    inspectObject(current.ruleForm, `${current.$options?.name || `comp-${depth}`}.ruleForm`);
                    inspectObject(current.model, `${current.$options?.name || `comp-${depth}`}.model`);
                    inspectObject(current.listQuery, `${current.$options?.name || `comp-${depth}`}.listQuery`);
                    current = current.$parent;
                    depth += 1;
                }

                return states;
            }

            const startVal = startInput ? startInput.value : '';
            const endVal = endInput ? endInput.value : '';
            const vueVal = rangeEditor && rangeEditor.__vue__
                ? JSON.stringify(rangeEditor.__vue__.value) : '';
            const rangeStates = rangeEditor && rangeEditor.__vue__
                ? collectRangeState(rangeEditor.__vue__)
                : [];
            const hiddenRangeOk = rangeStates.length === 0
                || rangeStates.some(item => item.value === expectedRangeJson);

            return {
                startInput: startVal,
                endInput: endVal,
                vueValue: vueVal,
                rangeStates,
                match: startVal === expectedStart
                    && endVal === expectedEnd
                    && hiddenRangeOk
            };
        }""", [expected_start, expected_end])
        if result.get("match"):
            return True
        print(
            "  Verification:"
            f" start={result.get('startInput')},"
            f" end={result.get('endInput')},"
            f" vue={result.get('vueValue')},"
            f" hidden={result.get('rangeStates')}"
        )
        return False
    except Error:
        return False


def _sync_date_range_state(page: Page, start_str: str, end_str: str) -> bool:
    """Synchronize the visible picker and hidden query state used by the DMS page.

    The page keeps both separate start/end fields and a combined range array
    (for example ``repairTimeStart,repairTimeEnd``). If the combined field is
    left unchanged, clicking 查询 resets the visible picker back to today's date.
    """
    js_code = """
    ([startStr, endStr]) => {
        const datePicker = document.querySelector('#datePicker')
            || document.querySelector('.el-date-editor--daterange')
            || document.querySelector('.el-form-item .el-date-editor');

        if (!datePicker) return false;

        const range = [startStr, endStr];
        const rangeKeys = [
            'repairTimeStart,repairTimeEnd',
            'arrivalTimeStart,arrivalTimeEnd',
            'entryDateStart,entryDateEnd',
            'startDate,endDate',
        ];
        const singleKeyPairs = [
            ['repairTimeStart', 'repairTimeEnd'],
            ['arrivalTimeStart', 'arrivalTimeEnd'],
            ['entryDateStart', 'entryDateEnd'],
            ['startDate', 'endDate'],
        ];
        let changed = false;

        function syncObject(obj) {
            if (!obj || typeof obj !== 'object') return;

            for (const [startKey, endKey] of singleKeyPairs) {
                if (startKey in obj && endKey in obj) {
                    obj[startKey] = startStr;
                    obj[endKey] = endStr;
                    changed = true;
                }
            }

            for (const key of rangeKeys) {
                if (key in obj) {
                    obj[key] = range.slice();
                    changed = true;
                }
            }
        }

        function syncComponent(comp) {
            if (!comp) return;

            if (comp.value !== undefined) {
                comp.value = range.slice();
                changed = true;
            }
            if (comp.displayValue !== undefined) comp.displayValue = range.slice();
            if (comp.userInput !== undefined) comp.userInput = null;
            if (comp.valueOnOpen !== undefined) comp.valueOnOpen = range.slice();
            if (comp.modelCode !== undefined) comp.modelCode = range.slice();
            if (typeof comp.$emit === 'function') comp.$emit('input', range.slice());

            syncObject(comp);
            syncObject(comp.formField);
            syncObject(comp.queryForm);
            syncObject(comp.ruleForm);
            syncObject(comp.model);
            syncObject(comp.listQuery);
        }

        const startInput = datePicker.querySelector('input.el-range-input[placeholder="开始日期"]')
            || datePicker.querySelector('input.el-range-input:first-of-type');
        const endInput = datePicker.querySelector('input.el-range-input[placeholder="结束日期"]')
            || datePicker.querySelector('input.el-range-input:last-of-type');

        if (startInput && endInput) {
            startInput.value = startStr;
            endInput.value = endStr;
        }

        const seen = new Set();
        const vueRoots = [];
        if (datePicker.__vue__) vueRoots.push(datePicker.__vue__);
        for (const el of datePicker.querySelectorAll('*')) {
            if (el.__vue__) vueRoots.push(el.__vue__);
        }

        for (const root of vueRoots) {
            let current = root;
            let depth = 0;
            while (current && depth < 8 && !seen.has(current)) {
                seen.add(current);
                syncComponent(current);
                current = current.$parent;
                depth += 1;
            }
        }

        return changed;
    }
    """
    try:
        return bool(page.evaluate(js_code, [start_str, end_str]))
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
                // Do NOT emit 'change' — it triggers DMS event listeners that
                // can cause page navigation or auto-query, resetting the date.
                return true;
            }
            for (const child of vueComp.$children || []) {
                if (child.value !== undefined || child.pickerVisible !== undefined) {
                    child.value = [startStr, endStr];
                    child.$emit('input', [startStr, endStr]);
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
            return true;
        }

        // Strategy C: find __vue__ anywhere within datePicker
        const allEls = datePicker.querySelectorAll('*');
        for (const el of allEls) {
            if (el.__vue__ && el.__vue__.value !== undefined) {
                const comp = el.__vue__;
                comp.value = [startStr, endStr];
                comp.$emit('input', [startStr, endStr]);
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
    """Set date range by clicking calendar cells in the Element UI date picker popup.

    Mimics real user: click input → picker opens → navigate BOTH panels to correct
    months → click start day → click end day → picker auto-closes.

    CRITICAL: The DMS picker has independently-scrolling left/right panels.
    All month navigation must happen BEFORE any date selection, because any click
    between the two date selections cancels the first selection.
    """
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

    # Step 1: Navigate BOTH panels to correct months BEFORE selecting any date.
    # The two panels scroll independently in the DMS picker.
    _navigate_picker_month(page, picker, start_date, panel="left")
    _navigate_picker_month(page, picker, end_date, panel="right")
    print(f"  Calendar: panels navigated, clicking start day {start_date.day}")

    # Step 2: Click start day in left panel (first click = start date)
    _click_date_cell(page, picker, start_date, panel="left")
    page.wait_for_timeout(300)

    # Step 3: Click end day in right panel (second click = end date, picker auto-closes)
    print(f"  Calendar: clicking end day {end_date.day} in right panel")
    _click_date_cell(page, picker, end_date, panel="right")
    page.wait_for_timeout(500)

    # Picker should auto-close after selecting end date
    try:
        picker.wait_for(state="hidden", timeout=3_000)
        print("  Calendar: picker closed after end date selection")
    except Error:
        # If picker is still open, look for a confirm button
        confirm_btn = picker.locator(".el-picker-panel__footer button:last-child")
        if confirm_btn.count() > 0:
            confirm_btn.click()
            print("  Calendar: clicked confirm button")
            page.wait_for_timeout(300)
        else:
            print("  Calendar: picker still open, no confirm button found")


def _navigate_picker_month(page: Page, picker: Any, target_date: date, panel: str) -> None:
    """Navigate the picker popup to show the month of target_date in the specified panel.

    The DMS uses a custom Element UI daterange picker where the header label is a
    single <div> containing both year and month (e.g. "2026  May"), not two separate
    .el-date-picker__header-label elements.
    """
    content_class = "is-left" if panel == "left" else "is-right"
    header = picker.locator(f".el-date-range-picker__content.{content_class} .el-date-range-picker__header")
    if header.count() == 0:
        print(f"  Navigate({panel}): header not found")
        return

    target_year, target_month = target_date.year, target_date.month

    for attempt in range(24):
        # Get the header div text — format is like "2026  May" or "2026  April"
        try:
            # The year-month text is in a plain <div> inside the header
            header_divs = header.locator("div")
            year_month_text = ""
            for i in range(header_divs.count()):
                txt = header_divs.nth(i).text_content().strip()
                if txt and any(c.isdigit() for c in txt):
                    year_month_text = txt
                    break
        except Error:
            break

        if not year_month_text:
            break

        # Parse "2026  May" → year=2026, month=5
        parts = year_month_text.split()
        try:
            current_year = int(parts[0])
            current_month = _parse_month_text(" ".join(parts[1:]))
        except (ValueError, IndexError):
            print(f"  Navigate({panel}): cannot parse header '{year_month_text}'")
            break

        if current_year == target_year and current_month == target_month:
            print(f"  Navigate({panel}): reached {current_year}-{current_month}")
            break

        if (current_year, current_month) > (target_year, target_month):
            prev_btn = header.locator("button.el-icon-arrow-left").first
            if prev_btn.count() > 0:
                prev_btn.click()
            else:
                prev_btn = header.locator("button.el-icon-d-arrow-left").first
                prev_btn.click()
        else:
            next_btn = header.locator("button.el-icon-arrow-right").first
            if next_btn.count() > 0:
                next_btn.click()
            else:
                next_btn = header.locator("button.el-icon-d-arrow-right").first
                next_btn.click()

        page.wait_for_timeout(200)
    else:
        print(f"  Navigate({panel}): FAILED to reach {target_year}-{target_month} after 24 attempts")


def _parse_month_text(text: str) -> int:
    """Parse month number from Element UI picker header label.

    The DMS may display months in Chinese (一月), English (April), or numeric (4) format.
    """
    import re
    # Numeric: "4", "04", "4月"
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))

    # Chinese months
    chinese_months = {"一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
                      "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12}
    for cn, num in chinese_months.items():
        if cn in text:
            return num

    # English months
    english_months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                      "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
    lower = text.strip().lower()
    for en, num in english_months.items():
        if en in lower:
            return num

    return 1


def _click_date_cell(page: Page, picker: Any, target_date: date, panel: str) -> None:
    """Click the date cell for target_date.day in the picker panel.

    Uses the panel's content selector (.is-left / .is-right) matching the DMS layout,
    and only clicks cells that are NOT prev-month or next-month to avoid wrong dates.
    """
    day = target_date.day
    content_class = "is-left" if panel == "left" else "is-right"
    panel_content = picker.locator(f".el-date-range-picker__content.{content_class}")
    date_table = panel_content.locator(".el-date-table")

    # Only click available/current-month cells, skip prev-month and next-month
    cells = date_table.locator("td.available span, td.current-month span")
    cell_count = cells.count()
    for i in range(cell_count):
        try:
            text = cells.nth(i).text_content()
            if text and text.strip() == str(day):
                cells.nth(i).click()
                print(f"  Clicked day {day} in {panel} panel (cell {i}/{cell_count})")
                return
        except Error:
            continue

    # Fallback: try all cells including prev/next month
    all_cells = date_table.locator("td span")
    all_count = all_cells.count()
    print(f"  Fallback: searching {all_count} cells for day {day} in {panel} panel")
    for i in range(all_count):
        try:
            text = all_cells.nth(i).text_content()
            if text and text.strip() == str(day):
                all_cells.nth(i).click()
                print(f"  Clicked day {day} (fallback cell {i}/{all_count})")
                return
        except Error:
            continue

    print(f"  WARNING: could not find day {day} in {panel} panel ({cell_count}+{all_count} cells checked)")


def click_query(page: Page) -> None:
    """Click the 查询 (query) button and wait for results to load.

    Uses JavaScript .click() instead of Playwright's .click() because
    Playwright's click triggers mouseover/focus events that cause the DMS
    frontend to reset the date range to today.
    """
    clicked = page.evaluate("""(function() {
        var btns = document.querySelectorAll('section.mixButton button');
        for (var i = 0; i < btns.length; i++) {
            var span = btns[i].querySelector('span');
            if (span && span.textContent.indexOf('查询') !== -1) {
                btns[i].click();
                return true;
            }
        }
        // Fallback selectors
        var selectors = [
            'section.mixButton button[comp-key=\"btnKey2\"]',
            'section.mixButton button[comp-key=\"btnKey1\"]',
        ];
        for (var s = 0; s < selectors.length; s++) {
            var btn = document.querySelector(selectors[s]);
            if (btn) { btn.click(); return true; }
        }
        return false;
    })()""")

    if not clicked:
        # Last resort: Playwright click
        query_btns = page.locator("section.mixButton button")
        for i in range(query_btns.count()):
            try:
                text = query_btns.nth(i).locator("span").text_content()
                if text and "查询" in text:
                    query_btns.nth(i).click()
                    break
            except Error:
                continue

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
    """Click the 重置 (reset) button to clear previous date filter.

    Uses JavaScript .click() for consistency — Playwright clicks can
    trigger DMS frontend events that interfere with date state.
    """
    page.evaluate("""(function() {
        var btns = document.querySelectorAll('section.mixButton button');
        for (var i = 0; i < btns.length; i++) {
            var span = btns[i].querySelector('span');
            if (span && span.textContent.indexOf('重置') !== -1) {
                btns[i].click(); return;
            }
        }
    })()""")
    page.wait_for_timeout(500)


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

    today = beijing_today()
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

            # Create CDP session for download handling
            cdp_session = context.new_cdp_session(page)

            # Acquire export lock so keepalive won't refresh during download
            lock_file = acquire_export_lock(plugin_root)

            try:
                # Reset filters first to clear any residual state from previous queries.
                reset_filters(page)
                page.wait_for_timeout(300)

                # Follow the real DMS interaction order:
                # 1. 到店日期
                # 2. 展开
                # 3. 业务类型
                # 4. 查询
                # 5. 导出
                print(f"Setting date range: {date_str(start_date)} ~ {date_str(end_date)}")
                set_arrival_date_range(page, start_date, end_date)
                expand_filter_fields(page)
                select_accident_business_type(page)

                if args.dry_run:
                    print("[DRY RUN] Would export maintenance orders for this date range")
                    status = "dry_run"
                else:
                    # Click query
                    click_query(page)

                    # Post-query verification: check if we're still on the right page
                    # and the date range is preserved. The DMS page sometimes
                    # resets 到店日期 back to "today ~ today" after 查询, so we
                    # immediately re-apply the intended range before export.
                    try:
                        if "/login" in page.url.lower():
                            raise RuntimeError("Redirected to login page after query")
                        date_ok = _verify_date_range(page, date_str(start_date), date_str(end_date))
                        business_ok = _verify_accident_business_type(page)
                        if not date_ok or not business_ok:
                            print("  [WARN] Filters reset after query — reapplying before export")
                            set_arrival_date_range(page, start_date, end_date)
                            expand_filter_fields(page)
                            select_accident_business_type(page)
                            date_ok = _verify_date_range(page, date_str(start_date), date_str(end_date))
                            business_ok = _verify_accident_business_type(page)
                            if not date_ok or not business_ok:
                                print("  [WARN] Filters still mismatching after reapply")
                    except RuntimeError:
                        raise
                    except Exception as exc:
                        print(f"  [WARN] Could not verify date range after query: {exc}")

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
                            set_arrival_date_range(page, start_date, end_date)
                            expand_filter_fields(page)
                            select_accident_business_type(page)
                            click_query(page)
                            date_ok = _verify_date_range(page, date_str(start_date), date_str(end_date))
                            business_ok = _verify_accident_business_type(page)
                            if not date_ok or not business_ok:
                                print("  [WARN] Retry query reset filters — reapplying before export")
                                set_arrival_date_range(page, start_date, end_date)
                                expand_filter_fields(page)
                                select_accident_business_type(page)
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
