# 共享浏览器会话：多爬虫复用同一 DMS 窗口

与 `dfmc-dms-crawler` 使用同一套机制。完整说明见：

[`../dfmc-dms-crawler/docs/shared-browser-session.md`](../../dfmc-dms-crawler/docs/shared-browser-session.md)

（若目录并列：`plugins/dfmc-dms-crawler/docs/shared-browser-session.md`）

## 快速要点

1. 设置同一会话目录：

```bash
export DFMC_DMS_SESSION_HOME="$HOME/dms-shared-session"
```

2. 用任一控制台登录并保活（本插件默认 `http://127.0.0.1:9000`）：

```bash
./run.sh --console
```

3. 错峰跑爬虫；`exporting.lock` 保证同一时刻只有一个爬虫占用标签页。

| 爬虫 | 插件 | 页面 |
|------|------|------|
| 维修工单 | accident-vehicle-reminder | 维修工单查询 |
| 投诉导出 | dfmc-dms-crawler | 投诉工单 |

4. 未设置 `DFMC_DMS_SESSION_HOME` 时，本插件使用自己的 `.browser-profile` / `.runtime`（独立会话）。
