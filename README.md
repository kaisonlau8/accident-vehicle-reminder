# 事故车维修超期提醒系统

猛士科技服务运营的事故维修进度监控系统。系统从 DMS 爬取近 30 天事故维修工单，按维修状态识别在修车辆，生成门店、区域、全国报表，并通过飞书发送告警和报表。

## 当前口径

- 所有定时触发和爬虫日期范围均按北京时间 UTC+8 计算。
- DMS 爬取时固定筛选“业务类型 = 事故维修”，导入阶段信任 DMS 筛选结果，不再做事故车二次判断。
- 爬取日期范围为“北京时间今天向前 30 天 ~ 北京时间今天”。
- 状态为“已作废”的工单在 Excel 导入阶段直接移除，不参与告警、统计、报表和 KPI。
- 维修中状态仅包含：待派工、接车、维修中。
- 其他所有状态均视为维修完成。

## 提醒规则

| 规则 | 内容 | 时间 | 收件人 | 消息 |
|------|------|------|--------|------|
| 1 | 事故车超期告警，7 天/10 天/14 天分级 | 10:00 | 门店提醒人 1~4 | 卡片 |
| 2 | 已作废工单排除 | 导入时生效 | 无 | 过滤规则 |
| 3 | 门店事故维修未完工汇总 | 10:00、17:00 | 门店服务经理（提醒人 1） | 卡片 |
| 4 | 区域事故维修超期汇总 | 10:00、17:00 | 区域督导 + 控制台维护的全国收件人 | 卡片 + Excel |
| 5 | 全国事故维修超期汇总及 KPI | 17:00 | 全部区域督导 + 控制台维护的全国收件人 | 卡片 + Excel |

超期分级按在修车辆的到店天数判断：

| 分级 | 条件 |
|------|------|
| 7 天超期 | days_in_shop >= 7 |
| 10 天超期 | days_in_shop >= 10 |
| 14 天超期 | days_in_shop >= 14 |

## KPI 口径

全国报表中的 KPI 使用 30 天事故工单作为分母，工单里不包含作废。

```text
30天内事故工单7天完工率 = 7天内完工事故车辆数 / 全部事故工单数 * 100%
30天内事故工单10天完工率 = 10天内完工事故车辆数 / 全部事故工单数 * 100%
```

其中：

- 全部事故工单数：DMS 已按事故维修筛选后、导入时排除已作废后的全部工单数。
- 7 天内完工事故车辆数：已视为维修完成且 `days_in_shop <= 7` 的工单数。
- 10 天内完工事故车辆数：已视为维修完成且 `days_in_shop <= 10` 的工单数。

## 快速开始

### 1. 初始化环境

```bash
python3 scripts/bootstrap.py
```

### 2. 配置飞书

复制 `.env.example` 为 `.env`，填写飞书应用凭证和测试手机号。

```bash
cp .env.example .env
```

`.env` 示例：

```env
APP_ID=cli_xxxxx
APP_SECRET=xxxxx
ADMIN_MOBILE=13800138000
```

### 3. 维护联系人

门店、区域、督导、提醒人信息维护在根目录 `list.xlsx`。格式见 [docs/list_xlsx_format.md](docs/list_xlsx_format.md)。

全国收件人在 Web 控制台的“门店联系人”页面维护，数据写入 `config/stores.json` 的 `national_recipients`。

### 4. 登录 DMS

```bash
./login.sh
```

浏览器打开 DMS 后，手动完成登录。登录后的浏览器会话会写入 `.runtime/browser-state.json`。

### 5. 启动 Web 控制台

```bash
./run.sh --console
```

默认地址：

```text
http://127.0.0.1:9000
```

控制台支持：

| 页面 | 用途 |
|------|------|
| 仪表盘 | 查看浏览器会话、DMS 保活、最新数据和任务状态 |
| 模式切换 | 启动定时等候、立即执行测试、立即执行正式任务 |
| 手动触发 | 单独爬取、导入 Excel、执行 10:00 或 17:00 任务 |
| 规则管理 | 查看和编辑规则参数 |
| 数据查看 | 查看超期车辆列表 |
| 报表下载 | 下载生成的 Excel 报表 |
| 门店联系人 | 上传 `list.xlsx`，新增或删除全国收件人 |

## 常用命令

```bash
# 打开 DMS 登录浏览器
./login.sh

# 启动 Web 控制台
./run.sh --console

# 使用 8000 端口启动控制台
./run.sh --console --port 8000

# 测试模式，所有消息发给 ADMIN_MOBILE 或 --test-phone
./run.sh --test --test-phone 13800138000

# 跳过爬取，用最新快照跑测试
./run.sh --test --skip-crawl --test-phone 13800138000

# 正式单次执行 10:00 任务
./run.sh --morning

# 正式单次执行 17:00 报表
./run.sh --evening
```

## DMS 爬取与保活

Web 控制台启动后会监控浏览器会话。只要 DMS 页面在线，控制台会自动启动 `keepalive_browser.py`，每 300 秒刷新一次 DMS 页面，防止会话过期。

仪表盘会显示 DMS 保活状态和“距离下一次刷新还有 xx 秒”。导出期间会写入 `.runtime/exporting.lock`，保活进程检测到锁文件后会跳过刷新，避免打断下载。

DMS 爬取步骤按页面真实交互顺序执行：

1. 填写到店日期。
2. 判断筛选区状态：显示 `open more/展开` 时点击展开；显示 `Put away/收起` 时不点击。
3. 选择业务类型：事故维修。
4. 点击查询。
5. 校验日期和业务类型仍正确。
6. 点击导出。

## 数据流

```text
DMS 页面
  -> crawl_maintenance_orders.py
  -> output/maintenance_orders_YYYY-MM-DD.xlsx
  -> import_excel.py
  -> data/repair_orders/repair_orders_YYYY-MM-DD.json
  -> rule_engine.py
  -> report_generator.py
  -> message_dispatcher.py
  -> 飞书
```

## 主要文件

| 文件 | 说明 |
|------|------|
| `.env` | 飞书应用凭证和测试手机号，本地私密文件，不提交 |
| `.env.example` | 环境变量示例 |
| `list.xlsx` | 门店联系人主表，包含真实手机号，不提交 |
| `config/rules.json` | 规则配置 |
| `config/stores.json` | 全国收件人配置 |
| `config/recipients.json` | 手机号到飞书 open_id 的缓存，自动生成 |
| `.runtime/browser-state.json` | DMS 浏览器会话状态，自动生成 |
| `.runtime/keepalive-state.json` | DMS 保活状态，自动生成 |
| `output/maintenance_orders_YYYY-MM-DD.xlsx` | DMS 导出的原始 Excel |
| `data/repair_orders/repair_orders_YYYY-MM-DD.json` | 标准化后的数据快照 |
| `data/reports/*.xlsx` | 生成的门店、区域、全国报表 |

## 运行文档

- 日常运维和故障排查：[docs/operations.md](docs/operations.md)
- `list.xlsx` 格式：[docs/list_xlsx_format.md](docs/list_xlsx_format.md)
- 飞书消息模板：[docs/飞书消息模板.md](docs/飞书消息模板.md)
