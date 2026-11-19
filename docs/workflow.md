# 日常使用

## 从速记到周报

1. 记录当天的客户、事项、已做的处理、当前结果和明日安排。速记时填写是否外勤及工作类型，供服务流程和周报统计使用。
2. 核对日报预览中的客户、日期、完成状态和下一步。结构化速记保留客户、处理结果等关键字段。问题描述和处理情况的润色未通过校验时，沿用原文。自由描述也会生成预览，保存前需要核对事实。
3. 保存到同一个 Markdown 文件。同日重新保存会更新当天的内容。周报读取对应日期的日报，并结合周报前瞻汇总工作量。

## 查询漏报和补交

中台按日期索引日报、提交历史和待补交记录。月历可以筛选问题日期，打开当天详情核对原文、任务状态和已有回执。SQLite 用于中台索引，日常归档应保留 Markdown 和提交记录源文件。

### 接入纷享销客

日常生成日报和浏览中台默认使用本地数据。需要提交或复核 CRM 时，在启动日报 CLI 或中台的同一个终端设置 `FXIAOKE_ENABLED=1`。各租户的表单字段、负责人和工单分类可能不同，请用获准的账号在测试环境核对实际权限和表单。

安装浏览器后，可用登录维护命令打开可见浏览器完成登录或扫码。中台重启后会读取启动环境中的配置。

```bash
python -m playwright install chromium
export FXIAOKE_ENABLED=1
python -m apps.reports.daily_report --fxiaoke-login-check
```

负责人通过 `FXIAOKE_SERVICE_OWNER` 指定。把租户配置示例复制到本机私有目录，填写管理员确认的表单字段、记录类型和选项标识。将 `FXIAOKE_CRM_CONFIG_PATH` 指向该文件，或保存在默认位置 `~/.config/auto-daytexy/fxiaoke_tenant.json`。示例保留空值。外勤记录的列表查重需要实际开始时间字段，创建远程工单需要对应记录类型、字段、选项名称和标识。修改配置后重新运行 CLI 或重启中台。[租户配置示例](../examples/fxiaoke-crm.example.json)

创建远程工单还需要本机工单类别文件。可复制公开的预览分类清单，按照实际租户修改分类名称与选项标识，然后将 `FXIAOKE_SERVICE_CATEGORY_CONFIG_PATH` 指向本机文件。账户、登录态与实际表单配置均保存在仓库外。[预览分类清单](../fxiaoke_remote_workorder_categories.json)

### 提交与复核

日常交互流程为

1. 填写日报和是否外勤，核对预览后保存本地 Markdown。
2. 外勤以签到关联的技术服务记录作为主服务。当前规则下，非外勤以远程工单作为主服务。存在附加远程支持时，按提示决定是否提交。
3. 提交前核对远程客户、工时、类别和处理结果。日报与选定的服务记录、工单分别显示提交结果。
4. 在中台补交时，先打开日期详情并查看预览，核对正文和每一项服务后再确认。若提交结果显示未知，先用只读复核查找原记录和回执，再决定能否补交。

只写本地日报时，使用 `--no-fxiaoke`。只预览时，再加 `--preview-only`。用 `--help` 查看各脚本当前支持的选项。

## 配置

| 配置项 | 作用 |
| --- | --- |
| `DAILY_REPORT_PATH` | 默认日报文件为 `~/Documents/auto-daytexy/日报.md`。也可用日报命令的 `--path` 指定。 |
| `WEEKLY_REPORT_PATH` | 默认周报文件。未配置时，周报写到日报同目录的 `周报.md`。 |
| `WEB_MANAGER_REPORT_PATH` | 中台读取的日报源文件。日常使用时设为和 CLI 相同的文件。 |
| `WEB_MANAGER_DATABASE_PATH` | 中台 SQLite 索引的位置。 |
| `FXIAOKE_ENABLED` | 设为 `1` 启用日报和工作中台的 CRM 操作。 |
| `FXIAOKE_SERVICE_OWNER` | CRM 服务单的默认负责人，填写该租户实际可选的账号名称。 |
| `FXIAOKE_CRM_CONFIG_PATH` | CRM 表单和路由映射的本机 JSON 路径。 |
| `FXIAOKE_SERVICE_CATEGORY_CONFIG_PATH` | 创建远程工单使用的本机分类映射。 |
| `DAILY_REPORT_API_KEY`、`WEEKLY_REPORT_API_KEY` | 日报润色和周报生成的模型凭据。周报也会读取日报凭据。 |
| `DAILY_REPORT_BASE_URL`、`WEEKLY_REPORT_BASE_URL` | 模型接口地址。默认接入硅基流动。 |
| `DAILY_REPORT_MODEL`、`WEEKLY_REPORT_MODEL` | 覆盖日报和周报使用的模型。 |

凭据保存在环境变量或本机私有配置中。分享问题时请使用虚构记录，去掉客户名称、工单编号、账号、密钥和浏览器登录态。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `apps/reports/` | 日报、周报、速记草稿和周报前瞻 |
| `apps/workbench/` | 本机 Web 中台、索引、任务和复核 |
| `packages/report_domain/` | 日报解析、路径、日期和历史记录的共享逻辑 |
| `integrations/fxiaoke/` | 纷享销客适配入口，部分实现来自根目录的 `fxiaoke_*.py` |
