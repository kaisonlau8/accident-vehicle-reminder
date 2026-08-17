# 共享浏览器会话

本项目与 **VIP 保养提醒**、**区域报表自动化** 共用同一 Playwright Chromium profile 与 CDP 会话。

权威说明（时刻表 / 登记 / 强刷保护）：

- 工具集文档：[m-hero/docs/SHARED_DMS_BROWSER.md](https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md)

## 配置

```bash
export DFMC_DMS_SESSION_HOME=/Users/i/dms-shared-session
export DFMC_DMS_BROWSER_EXECUTABLE="…/ms-playwright/chromium-…/Google Chrome for Testing"
```

```text
$DFMC_DMS_SESSION_HOME/
  .browser-profile/
  .runtime/
    browser-state.json
    keepalive-state.json
    keepalive.log
    exporting.lock
    crawl_schedule.json      # 时刻表
    crawl_registry.json      # 爬取登记
```

## 爬取时刻表（与本项目相关）

共享文件：`$DFMC_DMS_SESSION_HOME/.runtime/crawl_schedule.json`

| id | 计划时间 | 本仓任务 | owner |
|----|----------|----------|--------|
| `accident-morning` | **10:00** | 上午爬取 + 告警 | `crawl_maintenance_orders` |
| `accident-evening` | **17:00** | 下午爬取 + 报表 | `crawl_maintenance_orders` |

同文件内还有：区域报表 `08:30`、VIP `09:00`（错峰，勿并行）。

保护规则（三仓共用）：

- **提前 3 分钟**（`pre_minutes`）进入禁刷，直到爬取 `unregister` 完成
- 若到点未开跑，最多再等 **45 分钟**（`await_start_minutes`）后恢复强刷
- 拿锁 / 放锁时自动写 `crawl_registry.json`

## 约定

- 同一时刻只允许一个导出（`exporting.lock`）
- 登录一次即可：本控制台或 VIP 控制台「启动登录」后，其余附着同一会话
- 冷启动带 `--disable-extensions` 与 `--use-mock-keychain`，避免每次输 macOS 密码；换参数后可能要重登一次 DMS
- 改 10:00 / 17:00 调度时，同步改 `crawl_schedule.json` 里对应条目的 `time`
