#!/usr/bin/env python3
"""Crawl maintenance order (维修工单) data from the DFMC DMS system.

回放路径基于人工录制（recordings/dms_actions_*.json）：
  1. 打开 #datePicker → 日历选近 N 天
  2. Open more / 展开
  3. 业务类型 = 事故维修
  4. Query / 查询（JS click，避免 Playwright 点击重置日期）
  5. 导出 → 捕获下载文件

Usage:
  python3 crawl_maintenance_orders.py --days 30
  python3 crawl_maintenance_orders.py --days 30 --keepalive
  python3 crawl_maintenance_orders.py --days 30 --dry-run
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Error, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dfmc_browser_utils import (
    DEFAULT_TARGET_URL,
    acquire_export_lock,
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
    goto_dms_route,
    get_default_state_file,
    get_export_lock_path,
    get_session_home,
    release_export_lock,
)
from time_utils import beijing_strftime, beijing_today

MAINTENANCE_ORDER_ROUTE = "/aftermarketMange/maintenanceManagement/maintenanceOrderSearch"
DMS_HOST = "m-dms.dfmc.com.cn"
CRAWLER_OWNER = "crawl_maintenance_orders"


def date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def validate_logged_in(page: Page) -> None:
    url = page.url
    if DMS_HOST not in url:
        raise RuntimeError(f"Browser not on DMS site. Current URL: {url}")
    if "/login" in url.lower():
        raise RuntimeError("Browser is on the login page. Log in first via ./login.sh")
    if page.locator("input[type='password']").count() > 0:
        raise RuntimeError("Login page detected (password input found). Log in first.")


def navigate_to_maintenance_order_page(page: Page) -> None:
    goto_dms_route(page, MAINTENANCE_ORDER_ROUTE)


def reset_filters(page: Page) -> None:
    page.evaluate(
        """() => {
          const btns = document.querySelectorAll('section.mixButton button');
          for (const btn of btns) {
            const text = (btn.innerText || '').replace(/\\s+/g, '');
            if (text.includes('重置') || text.toLowerCase().includes('reset')) {
              btn.click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_timeout(400)


def _parse_month_text(text: str) -> int:
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    chinese_months = {
        "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
        "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
    }
    for cn, num in chinese_months.items():
        if cn in text:
            return num
    english_months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    lower = text.strip().lower()
    for en, num in english_months.items():
        if en in lower:
            return num
    return 1


def _navigate_picker_month(page: Page, picker: Any, target_date: date, panel: str) -> None:
    content_class = "is-left" if panel == "left" else "is-right"
    header = picker.locator(f".el-date-range-picker__content.{content_class} .el-date-range-picker__header")
    if header.count() == 0:
        print(f"  Navigate({panel}): header not found")
        return

    target_year, target_month = target_date.year, target_date.month
    for _ in range(24):
        header_divs = header.locator("div")
        year_month_text = ""
        for i in range(header_divs.count()):
            txt = (header_divs.nth(i).text_content() or "").strip()
            if txt and any(c.isdigit() for c in txt):
                year_month_text = txt
                break
        if not year_month_text:
            break

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
            btn = header.locator("button.el-icon-arrow-left").first
            if btn.count() == 0:
                btn = header.locator("button.el-icon-d-arrow-left").first
            btn.click()
        else:
            btn = header.locator("button.el-icon-arrow-right").first
            if btn.count() == 0:
                btn = header.locator("button.el-icon-d-arrow-right").first
            btn.click()
        page.wait_for_timeout(200)
    else:
        print(f"  Navigate({panel}): FAILED to reach {target_year}-{target_month}")


def _click_date_cell(page: Page, picker: Any, target_date: date, panel: str) -> None:
    day = target_date.day
    content_class = "is-left" if panel == "left" else "is-right"
    date_table = picker.locator(f".el-date-range-picker__content.{content_class} .el-date-table")
    cells = date_table.locator("td.available span, td.current-month span")
    for i in range(cells.count()):
        text = (cells.nth(i).text_content() or "").strip()
        if text == str(day):
            cells.nth(i).click()
            print(f"  Clicked day {day} in {panel} panel")
            return
    raise RuntimeError(f"Calendar: day {day} not found in {panel} panel")


def set_arrival_date_range(page: Page, start_date: date, end_date: date) -> None:
    """按录制路径：点 #datePicker → 左右面板导航到目标月 → 点起止日。"""
    editor = page.locator("#datePicker .el-date-editor--daterange").first
    if editor.count() == 0:
        editor = page.locator("#datePicker").first
    if editor.count() == 0:
        editor = page.locator(".el-date-editor--daterange").first
    editor.click()
    page.wait_for_timeout(400)

    picker = page.locator(".el-date-range-picker:visible").first
    try:
        picker.wait_for(state="visible", timeout=5_000)
    except Error:
        picker = page.locator(".el-picker-panel:visible").first
        picker.wait_for(state="visible", timeout=5_000)

    # 先导航左右面板，再点日期（避免中间点击取消首日）
    _navigate_picker_month(page, picker, start_date, panel="left")
    _navigate_picker_month(page, picker, end_date, panel="right")
    _click_date_cell(page, picker, start_date, panel="left")
    page.wait_for_timeout(250)
    _click_date_cell(page, picker, end_date, panel="right")
    page.wait_for_timeout(400)

    try:
        picker.wait_for(state="hidden", timeout=3_000)
    except Error:
        pass
    print(f"  Date range set: {date_str(start_date)} ~ {date_str(end_date)}")


def expand_filter_fields(page: Page) -> None:
    """按录制：仅在显示 Open more / 展开 时点击 button.moreParam。"""
    try:
        page.wait_for_selector("button.moreParam", timeout=5_000)
    except Error:
        print("  moreParam not found, assume already expanded")
        return

    state = page.evaluate(
        """() => {
          const btn = document.querySelector('button.moreParam');
          if (!btn) return {exists: false};
          const text = (btn.innerText || '').trim().toLowerCase();
          const cls = btn.className || '';
          const collapsed = text.includes('open more') || text.includes('展开')
            || cls.includes('circle-arrow-down');
          const expanded = text.includes('put away') || text.includes('收起')
            || cls.includes('circle-arrow-up');
          return {exists: true, collapsed, expanded, text};
        }"""
    )
    if not state.get("exists"):
        return
    if state.get("expanded") or not state.get("collapsed"):
        print(f"  Filters already expanded ({state.get('text')!r})")
        return

    page.locator("button.moreParam").click()
    page.wait_for_timeout(500)
    print("  Clicked Open more / 展开")


def select_accident_business_type(page: Page) -> None:
    """按录制：打开业务类型下拉 → 点「事故维修」。"""
    opened = page.evaluate(
        """() => {
          const cols = document.querySelectorAll('div.el-col');
          for (const col of cols) {
            const label = (col.innerText || '').slice(0, 40);
            if (!label.includes('业务类型') || label.includes('维修状态')) continue;
            const input = col.querySelector('.el-select input.el-input__inner, .el-select .el-input');
            const root = col.querySelector('.el-select');
            const clickable = input || root;
            if (!clickable) continue;
            clickable.click();
            return true;
          }
          // 录制时的兜底：第 8 个 el-col-6 上的 Please Choose
          const fallback = document.querySelector(
            'div.el-col.el-col-6:nth-of-type(8) .el-select input.el-input__inner'
          );
          if (fallback) { fallback.click(); return true; }
          return false;
        }"""
    )
    if not opened:
        raise RuntimeError("业务类型下拉未找到")
    page.wait_for_timeout(350)

    item = page.locator(".el-select-dropdown:visible .el-select-dropdown__item", has_text="事故维修").first
    try:
        item.wait_for(state="visible", timeout=5_000)
    except Error:
        item = page.locator("li.el-select-dropdown__item", has_text="事故维修").first
        item.wait_for(state="visible", timeout=5_000)
    item.click()
    page.wait_for_timeout(300)

    ok = page.evaluate(
        """() => {
          const tags = Array.from(document.querySelectorAll('.el-select .el-tag, .el-select .el-select__tags-text'));
          return tags.some(t => (t.innerText || '').includes('事故维修'));
        }"""
    )
    if not ok:
        raise RuntimeError("业务类型未成功选中「事故维修」")
    print("  Business type = 事故维修")


def click_query(page: Page) -> None:
    """按录制点 Query；用 JS click，避免 Playwright 点击把日期重置成今天。"""
    clicked = page.evaluate(
        """() => {
          const btns = document.querySelectorAll('section.mixButton button');
          for (const btn of btns) {
            const text = (btn.innerText || '').replace(/\\s+/g, '');
            if (text.includes('查询') || text.toLowerCase() === 'query' || text.includes('Query')) {
              // 排除 Reset
              if (text.toLowerCase().includes('reset') || text.includes('重置')) continue;
              btn.click();
              return text;
            }
          }
          const primary = document.querySelector(
            'section.mixButton .right-btn button.el-button--primary'
          );
          if (primary) { primary.click(); return primary.innerText || 'primary'; }
          return '';
        }"""
    )
    if not clicked:
        raise RuntimeError("查询按钮未找到")
    print(f"  Clicked query ({clicked!r})")
    try:
        page.wait_for_selector(".el-table__body-wrapper tbody tr", timeout=15_000)
    except Error:
        print("  [WARN] table rows not visible within 15s")
    page.wait_for_timeout(800)


def click_export_and_capture(page: Page, cdp_session: Any, output_dir: Path, today: date) -> Optional[Path]:
    save_path = output_dir / f"maintenance_orders_{date_str(today)}.xlsx"
    before_files = {f.name: f.stat().st_mtime for f in output_dir.iterdir() if f.is_file()}

    try:
        cdp_session.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(output_dir), "eventsEnabled": True},
        )
    except Exception as exc:
        print(f"  [WARN] setDownloadBehavior failed: {exc}")

    clicked = page.evaluate(
        """() => {
          const btns = document.querySelectorAll('section.mixButton button, .u-btn-left button');
          for (const btn of btns) {
            const text = (btn.innerText || '').trim();
            if (text.includes('导出') || text.toLowerCase().includes('export')) {
              btn.click();
              return text;
            }
          }
          return '';
        }"""
    )
    if not clicked:
        raise RuntimeError("导出按钮未找到")
    print(f"  Clicked export ({clicked!r}), waiting for download...")

    deadline = time.monotonic() + 180
    new_file: Optional[Path] = None
    stable_size: Optional[int] = None
    stable_since = 0.0

    while time.monotonic() < deadline:
        time.sleep(0.5)
        for f in output_dir.iterdir():
            if not f.is_file() or f.name.startswith("crawl_manifest"):
                continue
            if f.name in before_files:
                try:
                    if f.stat().st_mtime <= before_files[f.name]:
                        continue
                except OSError:
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

    try:
        if new_file != save_path:
            new_file.rename(save_path)
        print(f"  Saved: {save_path}")
        return save_path
    except OSError as exc:
        print(f"  Rename failed ({exc}), using: {new_file}")
        return new_file


def run_export_flow(page: Page, cdp_session: Any, output_dir: Path, start_date: date, end_date: date, today: date, dry_run: bool) -> tuple[Optional[Path], str]:
    reset_filters(page)
    print(f"Setting date range: {date_str(start_date)} ~ {date_str(end_date)}")
    set_arrival_date_range(page, start_date, end_date)
    expand_filter_fields(page)
    select_accident_business_type(page)

    if dry_run:
        print("[DRY RUN] Filters ready, skip query/export")
        return None, "dry_run"

    click_query(page)
    filepath = click_export_and_capture(page, cdp_session, output_dir, today)
    if filepath:
        return filepath, "ok"

    print("  Retrying once...")
    page.wait_for_timeout(2_000)
    reset_filters(page)
    set_arrival_date_range(page, start_date, end_date)
    expand_filter_fields(page)
    select_accident_business_type(page)
    click_query(page)
    filepath = click_export_and_capture(page, cdp_session, output_dir, today)
    return filepath, ("retried_ok" if filepath else "retry_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl maintenance order data from DFMC DMS.")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back (default: 30)")
    parser.add_argument("--output-dir", default="", help="Directory to save Excel file")
    parser.add_argument("--state-file", default="", help="Path to browser-state.json")
    parser.add_argument("--keepalive", action="store_true", help="Start browser keepalive in background")
    parser.add_argument("--dry-run", action="store_true", help="Set filters but don't export")
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else get_default_state_file(plugin_root)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else plugin_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    today = beijing_today()
    start_date = today - timedelta(days=args.days)
    end_date = today
    print(f"Date range: {date_str(start_date)} to {date_str(end_date)} ({args.days} days)")

    print(f"Session home: {get_session_home(plugin_root)}")
    cdp_port = ensure_cdp_browser_running(state_file, plugin_root=plugin_root)
    print(f"Browser CDP port: {cdp_port}")

    keepalive_proc: Optional[subprocess.Popen[Any]] = None
    if args.keepalive:
        python_bin = str(plugin_root / ".venv" / "bin" / "python")
        keepalive_script = str((plugin_root / "scripts" / "keepalive_browser.py").resolve())
        keepalive_proc = subprocess.Popen(
            [python_bin, keepalive_script, "--state-file", str(state_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"Keepalive started (pid={keepalive_proc.pid})")
        atexit.register(
            lambda: keepalive_proc.terminate() if keepalive_proc and keepalive_proc.poll() is None else None
        )

    filepath: Optional[Path] = None
    status = "unknown"

    lock_file = None
    try:
        with sync_playwright() as pw:
            browser = connect_browser_over_cdp(pw, cdp_port)
            context = browser.contexts[0]
            context.set_default_timeout(10_000)
            lock_file = acquire_export_lock(plugin_root, CRAWLER_OWNER)
            try:
                page = find_dms_page(context)
                if page is None:
                    page = context.new_page()
                    page.goto(DEFAULT_TARGET_URL, wait_until="domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2_000)

                validate_logged_in(page)
                print("Navigating to maintenance order page...")
                navigate_to_maintenance_order_page(page)
                print("Maintenance order page loaded.")

                cdp_session = context.new_cdp_session(page)
                filepath, status = run_export_flow(
                    page, cdp_session, output_dir, start_date, end_date, today, args.dry_run
                )
            finally:
                release_export_lock(lock_file, owner=CRAWLER_OWNER, plugin_root=plugin_root)

    except Exception as exc:
        print(f"Fatal error: {exc}")
        status = "fatal_error"
        try:
            release_export_lock(
                get_export_lock_path(plugin_root),
                owner=CRAWLER_OWNER,
                plugin_root=plugin_root,
            )
        except Exception:
            pass

    manifest_path = output_dir / "crawl_manifest.json"
    manifest = {
        "crawledAt": date_str(today),
        "dateRange": f"{date_str(start_date)} ~ {date_str(end_date)}",
        "days": args.days,
        "status": status,
        "file": str(filepath) if filepath else "",
        "replaySource": "recordings/dms_actions_manual",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_path}")
    print(f"Status: {status}")
    return 0 if status in {"ok", "retried_ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
