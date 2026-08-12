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
    exporting.lock
    crawl_schedule.json      # 时刻表
    crawl_registry.json      # 爬取登记
```

## 约定

| 爬虫 | 默认时间 | 页面 |
|------|----------|------|
| 区域报表 | 08:30 | 多报表导出 |
| VIP 保养提醒 | 09:00 | 保养提醒任务 |
| 本项目（上午） | 10:00 | 维修工单查询 |
| 本项目（下午） | 17:00 | 维修工单查询 |

- 强刷前 3 分钟起禁刷，直至爬取登记完成（见共享时刻表）
- 同一时刻只允许一个导出（`exporting.lock`）
- 登录一次即可：本控制台或 VIP 控制台「启动登录」后，其余附着同一会话
