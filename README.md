# 事故车维修超期提醒系统

猛士科技服务运营 — 事故车维修进度监控与飞书自动提醒。

## 背景

123 家服务网点中 109 家共用钣喷车间，事故车维修缺乏有效管控。本系统自动监控维修进度，对超期车辆通过飞书发送告警和报表。

## 提醒规则

| 规则 | 触发条件 | 时间 | 收件人 | 消息类型 |
|------|---------|------|--------|---------|
| 1 | ≥7天未完工每日分级告警（⚠️7天/🚨10天/🔴14天） | 10:00 每日 | 门店提醒人1~4 | 卡片告警 |
| 2 | 已作废工单排除（不纳入告警和统计） | 始终生效 | — | 过滤规则 |
| 3 | 门店未完工汇总 | 10:00/17:00 每日 | 门店提醒人1 | 卡片+Excel |
| 4 | 区域未完工汇总 | 10:00/17:00 每日 | 该区域督导+庄帅+杨永昌 | 卡片+Excel |
| 5 | 全国汇总+KPI | 17:00 每日 | 全部督导+庄帅+杨永昌 | 卡片+Excel |

告警分级：7天（黄色⚠️）→ 10天（橙色🚨）→ 14天（红色🔴），所有≥7天的事故车每天持续跟踪。

统计分档（不累加）：7~10天未完工 / 10~14天未完工 / ≥14天未完工，每辆车只归入最高档，不重复计算。

重复提醒追踪：已提醒过的车辆，卡片会标注"这是第X次提醒，首次提醒时间：YYYY-MM-DD"。首次提醒记录保存在 `data/followup/alert_history.json`。

排除规则：当前节点为"已作废"的工单不纳入告警、统计和报表。

KPI 目标：7 天完工率 ≥55%，10 天完工率 ≥80%。

## 快速开始

### 1. 初始化

```bash
python3 scripts/bootstrap.py
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入飞书应用凭证。

联系人信息维护 `list.xlsx`（门店编码→区域→督导→提醒人1~4），更新此表即可。

### 3. 登录 DMS

```bash
./login.sh
```

Chrome 弹窗打开 DMS 系统，手动登录后回到终端。

### 4. 爬取 + 分析 + 发送

```bash
# 测试模式（默认）：爬取最新数据，所有消息发给测试手机号（通过 --test-phone 或 ADMIN_MOBILE 环境变量指定），跑完就退出
./run.sh

# 测试模式，用已有数据不爬取
./run.sh --skip-crawl

# 正式模式：启动浏览器保活 + 定时调度常驻运行（Ctrl+C退出）
./run.sh --prod

# 正式模式，仅执行10:00任务（单次）
./run.sh --prod --morning

# 正式模式，仅执行17:00报表（单次）
./run.sh --prod --evening
```

`./run.sh --prod` 会自动：
- 后台启动浏览器保活（每5分钟刷新防止会话过期）
- 前台启动定时调度器（10:00 自动爬取+告警+报表，17:00 自动爬取+报表）
- Ctrl+C 同时停止保活和调度

### 5. Web 控制台（推荐）

```bash
./run.sh --console
```

浏览器打开 `http://localhost:9000`，可通过页面操作所有功能：

| 操作 | 说明 |
|------|------|
| 定时等候模式 | 启动后等到 10:00/17:00 自动触发，不立即发送，常驻运行 |
| 测试模式 | 立即执行，所有消息发给测试手机号 |
| 正式 10:00 任务 | 立即执行告警+门店/区域报表 |
| 正式 17:00 报表 | 立即执行全报表 |
| 手动触发 | 爬取数据、导入 Excel、单次触发指定任务 |
| 规则管理 | 查看/编辑 5 条规则参数 |
| 数据查看 | 超期车辆列表、告警历史 |
| 报表下载 | 下载生成的 Excel 报表 |
| 门店联系人 | 查看/上传 list.xlsx |

其他参数：
```bash
./run.sh --console --port 8000       # 自定义端口
./run.sh --console --skip-crawl      # 控制台 + 默认跳过爬取
```

## 数据流

```
DMS 自动爬取 → output/maintenance_orders.xlsx → import_excel.py → repair_orders JSON
                    ↑                                                               ↓
          open_browser_for_login.py (手动登录)                      rule_engine.py → 5 条规则评估
          keepalive_browser.py (5分钟保活)                                 ↓
                                                              alert_history.json → 提醒次数追踪
                                                                       ↓
                                                              report_generator.py → Excel 报表
                                                                       ↓
                                                             message_dispatcher.py → 飞书消息
```

## 配置文件

| 文件 | 说明 |
|------|------|
| `.env` | 飞书 APP_ID/SECRET |
| `list.xlsx` | 门店联系人主表（门店编码→区域→督导→提醒人1~4，直接更新此表即可） |
| `config/rules.json` | 5 条提醒规则配置（阈值、收件人角色） |
| `config/stores.json` | 仅存庄帅/杨永昌手机号（其余联系人从 list.xlsx 读取） |
| `config/recipients.json` | 手机号→open_id 缓存（自动生成） |
| `data/followup/alert_history.json` | VIN告警历史（首次提醒日期+累计次数，自动生成） |
| `.runtime/browser-state.json` | 浏览器CDP端口+PID（自动生成） |

## DMS Excel 格式

系统期望的 Excel 包含 4 个 Sheet：
- **维修工单**（主表）：门店编码、门店名称、大区、派工单号、维修状态、到店时间、VIN码、车系名称 等 24 列
- 工单工时、工单备件、工单其他项目（辅助 Sheet，用于判断事故车类型）

## 报表输出

| 报表 | 文件路径 | 内容 |
|------|---------|------|
| 全国汇总 | `data/reports/national_report_YYYY-MM-DD.xlsx` | 全国汇总+区域汇总+超期车辆明细+各门店汇总 |
| 区域汇总 | `data/reports/region_{区域名}_YYYY-MM-DD.xlsx` | 区域门店汇总+超期车辆明细（含告警级别） |
| 门店汇总 | `data/reports/store_{门店编码}_YYYY-MM-DD.xlsx` | 门店汇总指标+超期车辆明细（含告警级别） |

## 目录结构

```
plugins/accident-vehicle-reminder/
├── .env / .env.example
├── requirements.txt
├── README.md
├── config/          # 规则 + 门店 + 收件人配置
├── scripts/         # 核心脚本
│   ├── crawl_maintenance_orders.py/sh  # DMS维修工单爬取
│   ├── open_browser_for_login.py/sh    # 浏览器登录弹窗
│   ├── keepalive_browser.py/sh         # 5分钟保活
│   ├── dfmc_browser_utils.py           # 浏览器共享工具
│   ├── import_excel.py                 # Excel→JSON转换
│   ├── rule_engine.py                  # 规则评估+告警历史追踪
│   ├── feishu_client.py                # 飞书API+卡片模板
│   ├── message_dispatcher.py           # 消息路由+发送
│   ├── report_generator.py             # 全国/区域Excel报表生成
│   ├── scheduler.py                    # 定时调度(含爬取)
│   └── web_console.py                  # Web控制台(端口9000)
├── templates/        # HTML页面模板（仪表盘/模式/触发/规则/数据/报表/联系人）
├── docs/             # 格式说明和飞书消息模板文档
├── data/             # 运行时数据（快照、报表、告警历史、日志）
└── output/           # 爬取导出的Excel文件
```