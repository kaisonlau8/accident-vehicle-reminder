# 运维手册

本文档用于日常启动、验证、排查事故车提醒系统。

## 日常启动流程

1. 启动 DMS 登录浏览器。

```bash
./login.sh
```

2. 在浏览器中手动登录 DMS。

3. 启动 Web 控制台。

```bash
./run.sh --console
```

4. 打开控制台。

```text
http://127.0.0.1:9000
```

5. 在仪表盘确认：

- 浏览器会话：在线
- DMS 保活：已开启
- 保活倒计时：显示“下次刷新 xx 秒”

## 定时任务

所有触发判断使用北京时间 UTC+8。

| 时间 | 任务 |
|------|------|
| 10:00 | 自动爬取 DMS，发送事故车超期告警、门店报表、区域报表 |
| 17:00 | 自动爬取 DMS，发送门店报表、区域报表、全国报表和 KPI |

Web 控制台的“模式切换”页面可以启动“定时等候模式”。该模式不会立即发送消息，会常驻等待北京时间 10:00 和 17:00。

## 手动任务

| 操作 | 位置 |
|------|------|
| 单独爬取 DMS 数据 | 控制台 -> 手动触发 -> 爬取数据 |
| 导入已有 Excel | 控制台 -> 手动触发 -> 导入已有 Excel |
| 立即跑测试 | 控制台 -> 模式切换 -> 测试模式 |
| 立即跑正式 10:00 任务 | 控制台 -> 模式切换 -> 正式模式 10:00 |
| 立即跑正式 17:00 报表 | 控制台 -> 模式切换 -> 正式模式 17:00 |

## DMS 保活

控制台会自动管理 DMS 保活：

- 浏览器会话在线且 DMS 页面存在时，自动启动保活进程。
- 保活进程每 300 秒刷新一次 DMS 页面。
- 导出期间存在 `.runtime/exporting.lock`，保活会跳过刷新，避免影响下载。
- 断开浏览器连接或 DMS 页面离线时，控制台会停止保活状态。

保活状态文件：

```text
.runtime/keepalive-state.json
```

常用检查：

```bash
cat .runtime/keepalive-state.json
ps -eo pid=,stat=,command= | rg keepalive_browser.py
```

## 数据检查

爬取完成后，原始 Excel 位于：

```text
output/maintenance_orders_YYYY-MM-DD.xlsx
```

导入后的标准快照位于：

```text
data/repair_orders/repair_orders_YYYY-MM-DD.json
```

报表位于：

```text
data/reports/
```

## 常见问题

### 仪表盘显示 DMS 保活“初始化中”

通常表示保活进程启动后没有写出下一次刷新时间。可按顺序检查：

```bash
cat .runtime/keepalive-state.json
ps -eo pid=,stat=,command= | rg keepalive_browser.py
./.venv/bin/python -m py_compile scripts/web_console.py scripts/keepalive_browser.py
```

如果 `keepalive-state.json` 里只有 `starting` 且没有有效 `nextRefreshAt`，重启 Web 控制台即可让新代码重新接管保活。

### 控制台显示浏览器在线，但 DMS 页面离线

点击仪表盘的“启动登录”。如果浏览器已在运行，控制台会复用当前会话并重新打开 DMS 页面。

### Chrome 打印 GCM 网络错误

类似以下日志来自 Chrome 后台服务，不是业务错误：

```text
google_apis/gcm/engine/connection_factory_impl.cc ... net error: -2
```

当前 Web 控制台启动浏览器时会将浏览器标准输出和错误输出重定向到空设备，正常不应再刷屏。

### Excel 导入后数量异常

优先检查 DMS 导出条件：

- 到店日期是否为北京时间今天向前 30 天至今天。
- 业务类型是否显示为事故维修。
- 导出的 Excel 是否来自“维修工单”页面。

导入逻辑不会二次判断事故车，默认信任 DMS 已筛选“事故维修”。

### KPI 与预期不一致

当前 KPI 口径：

```text
30天内事故工单7天完工率 = 7天内完工事故车辆数 / 全部事故工单数 * 100%
30天内事故工单10天完工率 = 10天内完工事故车辆数 / 全部事故工单数 * 100%
```

工单里不包含作废。维修中状态仅包含：待派工、接车、维修中。其他状态均视为维修完成。

## 发布前检查

建议在推送前运行：

```bash
./.venv/bin/python -m py_compile scripts/*.py
git status --short
```

