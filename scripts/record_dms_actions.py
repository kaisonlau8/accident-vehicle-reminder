#!/usr/bin/env python3
"""录制 DMS 页面上手动操作，输出可复现的步骤 JSON。

用法：
  1. 先 ./login.sh 并登录，打开维修工单页（可选）
  2. python3 scripts/record_dms_actions.py
  3. 在已打开的 Chrome 里亲手操作完整流程
  4. 终端按 Enter 结束录制
     或：python3 scripts/record_dms_actions.py --stop-file .runtime/stop-recording
        创建该文件即可结束
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dfmc_browser_utils import (
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
    get_default_state_file,
)
from time_utils import beijing_strftime

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = PLUGIN_ROOT / "recordings"

INJECT_JS = r"""
(() => {
  if (window.__DMS_RECORDER_INSTALLED__) return "already";
  window.__DMS_RECORDER_INSTALLED__ = true;
  window.__DMS_RECORDED_STEPS__ = window.__DMS_RECORDED_STEPS__ || [];

  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift(`#${CSS.escape(cur.id)}`);
        break;
      }
      const cls = Array.from(cur.classList || [])
        .filter(c => c && !c.startsWith("is-") && !c.startsWith("hover") && c !== "focusing")
        .slice(0, 3);
      if (cls.length) part += "." + cls.map(c => CSS.escape(c)).join(".");
      const parent = cur.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(n => n.tagName === cur.tagName);
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
        }
      }
      parts.unshift(part);
      cur = parent;
      if (cur && (cur.id === "app" || cur.tagName === "BODY")) break;
    }
    return parts.join(" > ");
  };

  const textOf = (el) => {
    if (!el) return "";
    const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.slice(0, 80);
  };

  const attrsOf = (el) => {
    if (!el || !el.getAttribute) return {};
    const keys = ["comp-key", "placeholder", "type", "role", "aria-label", "title", "name", "value"];
    const out = {};
    for (const k of keys) {
      const v = el.getAttribute(k);
      if (v) out[k] = String(v).slice(0, 120);
    }
    return out;
  };

  const describe = (el) => {
    if (!el || el.nodeType !== 1) return null;
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      classes: Array.from(el.classList || []).slice(0, 8),
      text: textOf(el),
      attrs: attrsOf(el),
      selector: cssPath(el),
    };
  };

  const push = (step) => {
    step.ts = Date.now();
    step.url = location.href;
    step.hash = location.hash || "";
    window.__DMS_RECORDED_STEPS__.push(step);
    console.log("[DMS_RECORDER]", JSON.stringify(step));
  };

  const interestingClickTarget = (el) => {
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      if (!cur.tagName) break;
      const tag = cur.tagName.toLowerCase();
      if (["button", "a", "input", "textarea", "select", "label", "td", "li", "span"].includes(tag)) return cur;
      if (cur.getAttribute && (cur.getAttribute("comp-key") || cur.getAttribute("role") === "button")) return cur;
      if (cur.classList && (
        cur.classList.contains("el-button") ||
        cur.classList.contains("el-select") ||
        cur.classList.contains("el-input") ||
        cur.classList.contains("el-date-editor") ||
        cur.classList.contains("el-select-dropdown__item") ||
        cur.classList.contains("el-picker-panel") ||
        cur.classList.contains("available") ||
        cur.classList.contains("moreParam")
      )) return cur;
      cur = cur.parentElement;
    }
    return el;
  };

  document.addEventListener("click", (ev) => {
    const raw = ev.target;
    const target = interestingClickTarget(raw);
    push({
      type: "click",
      target: describe(target),
      raw: describe(raw),
      x: ev.clientX,
      y: ev.clientY,
    });
  }, true);

  const onField = (ev, type) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (!["input", "textarea", "select"].includes(tag)) return;
    push({
      type,
      target: describe(el),
      value: String(el.value || "").slice(0, 200),
    });
  };

  document.addEventListener("change", (ev) => onField(ev, "change"), true);
  document.addEventListener("input", (ev) => onField(ev, "input"), true);

  // hash / SPA route changes
  const notifyNav = () => push({ type: "navigate", href: location.href, hash: location.hash || "" });
  window.addEventListener("hashchange", notifyNav);
  const _pushState = history.pushState;
  history.pushState = function() {
    const ret = _pushState.apply(this, arguments);
    setTimeout(notifyNav, 0);
    return ret;
  };
  const _replaceState = history.replaceState;
  history.replaceState = function() {
    const ret = _replaceState.apply(this, arguments);
    setTimeout(notifyNav, 0);
    return ret;
  };

  push({ type: "recorder_started", href: location.href, title: document.title || "" });
  return "installed";
})()
"""


def drain_steps(page) -> list[dict[str, Any]]:
    try:
        steps = page.evaluate(
            """() => {
              const steps = window.__DMS_RECORDED_STEPS__ || [];
              window.__DMS_RECORDED_STEPS__ = [];
              return steps;
            }"""
        )
        return steps or []
    except Exception:
        return []


def ensure_injector(page) -> None:
    try:
        result = page.evaluate(INJECT_JS)
        if result == "installed":
            print("[recorder] 注入成功:", page.url[:100])
        elif result == "already":
            pass
        else:
            print("[recorder] 注入返回:", result)
    except Exception as exc:
        print(f"[recorder] 注入失败: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="录制 DMS 手动操作")
    parser.add_argument(
        "--stop-file",
        type=str,
        default="",
        help="若指定，创建该文件即结束录制（适合后台运行）",
    )
    args = parser.parse_args()
    stop_file = Path(args.stop_file) if args.stop_file else None
    if stop_file and stop_file.exists():
        stop_file.unlink()

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    state_file = get_default_state_file(PLUGIN_ROOT)
    port = ensure_cdp_browser_running(state_file)

    collected: list[dict[str, Any]] = []
    stop = threading.Event()

    print("=" * 60)
    print("DMS 操作录制已就绪")
    print(f"CDP 端口: {port}")
    print("请在已打开的 Chrome 里亲手完成：")
    print("  1) 进入维修工单查询页（若已在可跳过）")
    print("  2) 设置到店日期（近 30 天）")
    print("  3) 如需则展开筛选")
    print("  4) 业务类型 = 事故维修")
    print("  5) 点查询")
    print("  6) 点导出，等文件下载完成")
    if stop_file:
        print(f"完成后告诉我，或手动创建停止文件: {stop_file}")
    else:
        print("完成后回到此终端，按 Enter 结束录制。")
    print("=" * 60)

    def wait_stop() -> None:
        if stop_file:
            while not stop.is_set():
                if stop_file.exists():
                    break
                time.sleep(0.5)
        else:
            try:
                input()
            except EOFError:
                while not stop.is_set():
                    time.sleep(1)
        stop.set()

    waiter = threading.Thread(target=wait_stop, daemon=True)
    waiter.start()

    with sync_playwright() as pw:
        browser = connect_browser_over_cdp(pw, port)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            print("[ERROR] 没有可用 browser context")
            return 1

        page = find_dms_page(context)
        if page is None:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://m-dms.dfmc.com.cn", wait_until="domcontentloaded")

        def on_download(download) -> None:
            step = {
                "type": "download",
                "suggested_filename": download.suggested_filename,
                "url": download.url,
                "ts": int(time.time() * 1000),
                "page_url": page.url,
            }
            collected.append(step)
            print(f"[download] {download.suggested_filename}")

        for p in context.pages:
            try:
                p.on("download", on_download)
            except Exception:
                pass
        context.on("page", lambda p: p.on("download", on_download))

        ensure_injector(page)
        last_url = page.url
        print(f"[recorder] 当前页: {page.url}")
        print("[recorder] 开始监听操作…")

        while not stop.is_set():
            current = find_dms_page(context) or page
            if current is not page:
                page = current
                ensure_injector(page)
                try:
                    page.on("download", on_download)
                except Exception:
                    pass

            if page.url != last_url:
                collected.append({
                    "type": "url_changed",
                    "from": last_url,
                    "to": page.url,
                    "ts": int(time.time() * 1000),
                })
                print(f"[nav] {page.url[:120]}")
                last_url = page.url
                ensure_injector(page)

            ensure_injector(page)
            for step in drain_steps(page):
                collected.append(step)
                kind = step.get("type")
                target = step.get("target") or {}
                label = target.get("text") or target.get("attrs", {}).get("placeholder") or target.get("selector") or ""
                print(f"[{kind}] {label[:60]}")
            time.sleep(0.35)

        for step in drain_steps(page):
            collected.append(step)

    if stop_file and stop_file.exists():
        stop_file.unlink(missing_ok=True)

    stamp = beijing_strftime("%Y%m%d%H%M%S")
    out = RECORDINGS_DIR / f"dms_actions_{stamp}.json"
    payload = {
        "recorded_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "step_count": len(collected),
        "steps": collected,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=" * 60)
    print(f"录制结束，共 {len(collected)} 步")
    print(f"已保存: {out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
