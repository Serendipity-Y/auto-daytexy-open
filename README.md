# auto dayTexy

本地运行的日报、周报和工作中台，面向要记录实施、售后和 POC 工作的技术服务人员。平时把客户、问题和处理结果记成日报，每周从同一份记录生成周报。需要追查漏报或补交时，在工作中台按日期查看原文和提交记录。

## 能做什么

| 入口 | 用途 |
| --- | --- |
| 日报 CLI | 逐项速记或输入一段工作摘要，预览后保存为 Markdown。结构化速记的 AI 润色不可用时，会用原始输入生成预览。 |
| 周报 CLI | 读取目标周的日报和周报前瞻，汇总项目交付、POC 和售后服务，结合下周安排生成周报草稿。 |
| 工作中台 | 在本机浏览器里按月查看日报，筛选缺日报、缺主技术服务和待核实的日期，打开当天详情追查原文和结果。 |
| 纷享销客集成 | 支持同步 CRM 日报、外勤技术服务记录和远程服务工单。工作中台可预览补交内容、查看任务结果和发起只读复核。 |

## 在本机体验

下面的命令在 macOS 的 shell 中运行。请从仓库根目录执行，使用 Python 3.14 建立项目环境。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-web.txt
```

纷享销客集成需要主动启用。本机试用可以从日报和中台开始。

### 预览一篇日报

用没有模型凭据的新环境运行这条命令，可以体验结构化输入的本地整理。已有模型凭据时，预览也可能调用模型。

```bash
DEMO_DIR="$(mktemp -d)"
python -m apps.reports.daily_report --preview-only --no-fxiaoke --no-clipboard \
  --path "$DEMO_DIR/日报.md" \
  "客户：示例实验室；销售：示例联系人；是否外勤：否；工作类型：售后；事项：测试终端偶尔掉线；完成：更换测试网线后暂未复现；处理结果：进行中；明日：继续观察连接状态"
```

首次保存自己的日报时，执行下面的命令，按提示输入并确认保存。把 `--path` 指向你希望长期保存日报的位置。

```bash
python -m apps.reports.daily_report --no-fxiaoke --no-clipboard \
  --path "$DEMO_DIR/个人日报.md"
```

### 打开工作中台

把[虚构日报](examples/daily-report.md)复制到刚创建的临时目录，用它启动中台。查看这些样例只需要本地文件。

```bash
cp examples/daily-report.md "$DEMO_DIR/日报.md"
WEB_MANAGER_REPORT_PATH="$DEMO_DIR/日报.md" \
WEB_MANAGER_DATABASE_PATH="$DEMO_DIR/workbench.sqlite3" \
python -m apps.workbench.web_manager
```

打开[示例月份](http://127.0.0.1:8765/?month=2026-11)，选择 11 月 2 日或 3 日查看当天原文。中台只监听本机 `127.0.0.1`。退出时在终端按 `Ctrl+C`。

### 生成周报

退出中台后，可在同一个终端运行周报命令。周报需要可用的模型 API。将 `WEEKLY_REPORT_API_KEY` 设为本机环境变量，`--daily-path` 指向包含目标周数据的日报文件，`--path` 指向周报输出文件。

```bash
python -m apps.reports.weekly_report --preview-only --no-clipboard \
  --daily-path "$DEMO_DIR/个人日报.md" --path "$DEMO_DIR/周报.md" \
  --reflection "继续跟踪间歇性掉线" "复测连接并补充排查记录"
```

周一至周四运行时统计上周，周五至周日运行时统计本周。只想体验时，可以复制虚构日报，把日期改到目标周后用作输入。确认预览后，去掉 `--preview-only` 并按提示保存。

模型处理时，请求可能包含本次输入和用来参考格式的历史报告。使用真实工作内容前，请确认模型服务符合所在组织的数据使用要求。常用配置、纷享销客接入及提交确认流程见[日常使用](docs/workflow.md)。

## 本地检查

```bash
python -m unittest discover -s tests -v
```

测试使用虚构日报和模拟模型响应。

## 许可证

本项目使用 [MIT 许可证](LICENSE)。
