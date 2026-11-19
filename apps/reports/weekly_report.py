# -*- coding: utf-8 -*-
import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from openai import OpenAI

from packages.report_domain.paths import get_default_report_path
from packages.report_domain.report_format import (
    SECTION_LABELS,
    canonicalize_report_block,
    split_report_sections,
    strip_markdown_noise,
)
from ._shared import (
    APPLE_TERMINAL_CLIPBOARD_COMMAND,
    BASE_URL as DEFAULT_BASE_URL,
    DEFAULT_MODEL_NAME,
    REQUEST_TIMEOUT_SECONDS,
    request_chat_completion,
    build_chat_completion_options,
    configure_stdio,
    emit_terminal_message,
    copy_to_clipboard,
    extract_message_content,
    get_env_value,
    is_apple_terminal,
    load_api_key_from_credentials,
    read_brief_input,
    should_auto_confirm,
    write_text_atomically,
)
from .weekly_preview import (
    analyze_preview_entries,
    build_preview_entry,
    merge_preview_entry,
    load_preview_entries,
    resolve_preview_json_path,
    resolve_preview_markdown_path,
    save_preview_entries,
    save_preview_markdown,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEKLY_TITLE_PATTERN = re.compile(
    r"^\d{4}\.\d{2}\.\d{2}[（(][一二三四五六日天][）)]【周报】$"
)
WEEKLY_SECTION_PATTERN = re.compile(
    r"(?m)(?=^\d{4}\.\d{2}\.\d{2}[（(][一二三四五六日天][）)]【周报】$)"
)
DATE_PREFIX_PATTERN = re.compile(r"^(\d{4}\.\d{2}\.\d{2})")
DEFAULT_REFLECTION = (
    "本周围绕项目实施与售后支持持续推进工作，在保证响应效率的同时，也进一步沉淀了处理经验。"
    "后续会继续把交付节奏、问题跟进和客户沟通做得更扎实。"
)
MODEL_NAME = get_env_value(
    "WEEKLY_REPORT_MODEL",
    "DAILY_REPORT_MODEL",
    default=DEFAULT_MODEL_NAME,
)
BASE_URL = get_env_value(
    "WEEKLY_REPORT_BASE_URL",
    "DAILY_REPORT_BASE_URL",
    "SILICONFLOW_BASE_URL",
    default=DEFAULT_BASE_URL,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="根据目标工作周的日报自动生成正式周报，并写入周报文件。"
    )
    parser.add_argument("plan_text", nargs="*", help="直接传入下周工作安排。")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过确认，直接写入周报文件。",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成并打印，不写入文件。",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="生成完成后不复制到剪贴板。",
    )
    parser.add_argument("--path", dest="weekly_report_path", help="覆盖周报输出路径。")
    parser.add_argument(
        "--daily-path",
        dest="daily_report_path",
        help="覆盖日报来源路径。",
    )
    parser.add_argument(
        "--reflection",
        help="直接传入本周感想；不传则交互输入或由 AI 自动生成。",
    )
    return parser.parse_args(argv)


def resolve_daily_source_path(path_value=None):
    if path_value and path_value.strip():
        return Path(os.path.expandvars(os.path.expanduser(path_value.strip())))

    env_path = get_env_value(
        "WEEKLY_REPORT_DAILY_PATH",
        "DAILY_REPORT_PATH",
        "DAILY_REPORT_MD_FILE",
    )
    if env_path:
        return Path(os.path.expandvars(os.path.expanduser(env_path)))

    return get_default_report_path()


def resolve_weekly_report_path(path_value=None, daily_path=None):
    if path_value and path_value.strip():
        return Path(os.path.expandvars(os.path.expanduser(path_value.strip())))

    env_path = get_env_value("WEEKLY_REPORT_PATH", "WEEKLY_REPORT_MD_FILE")
    if env_path:
        return Path(os.path.expandvars(os.path.expanduser(env_path)))

    # Keep the weekly report beside the actual daily source, including custom names.
    reference_daily_path = daily_path or get_default_report_path()
    return reference_daily_path.with_name("周报.md")


def get_weekly_api_key():
    return get_env_value("WEEKLY_REPORT_API_KEY", "DAILY_REPORT_API_KEY", "SILICONFLOW_API_KEY") or load_api_key_from_credentials(
        "weekly_report_api_key",
        "daily_report_api_key",
        "siliconflow_api_key",
        "api_key",
    )


def get_last_week_workdays(now=None):
    """
    根据运行时间，决定统计的是本周的工作还是上周的工作。
    通常周五、周六、周日运行是为了统计本周；
    而下周一、周二、周三运行是为了补上周的周报。
    """
    current = now or datetime.now()
    # 如果今天是周五(4)、周六(5)或周日(6)，说明是要统计本周（本周一到本周五）
    # 如果今天是周一(0)到周四(3)，说明是要统计上周（上周一到上周五）
    if current.weekday() >= 4:
        start_of_week = current - timedelta(days=current.weekday())
    else:
        start_of_week = current - timedelta(days=current.weekday() + 7)
    return [start_of_week + timedelta(days=index) for index in range(5)]


def build_weekly_title(now=None):
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    friday = get_last_week_workdays(now)[-1]
    return f"{friday.strftime('%Y.%m.%d')}（{weekdays[friday.weekday()]}）【周报】"


def get_report_week_days(now=None):
    monday = get_last_week_workdays(now)[0]
    return [monday + timedelta(days=index) for index in range(7)]


def extract_date_prefix(text):
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = DATE_PREFIX_PATTERN.match(first_line)
    return match.group(1) if match else ""


def format_workday_range(workdays):
    return f"{workdays[0].strftime('%Y.%m.%d')} 至 {workdays[-1].strftime('%Y.%m.%d')}"


def filter_daily_sections_for_workdays(content, workdays):
    target_dates = {day.strftime("%Y.%m.%d") for day in workdays}
    matched_sections = []
    for section in split_report_sections(content):
        if extract_date_prefix(section) in target_dates:
            matched_sections.append(section)
    return matched_sections


def extract_daily_reports(file_path, workdays):
    if not file_path.exists():
        raise RuntimeError(f"未找到日报文件：{file_path}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"读取日报失败：{exc}") from exc

    sections = filter_daily_sections_for_workdays(content, workdays)
    if not sections:
        raise RuntimeError(f"未找到统计区间（{format_workday_range(workdays)}）的日报数据。")
    return "\n\n".join(sections)


def parse_daily_section(section):
    parsed = {label: "" for label in SECTION_LABELS}
    lines = [line.rstrip() for line in section.splitlines() if line.strip()]
    if lines:
        parsed["日期"] = lines[0].strip()

    current_label = None
    for line in lines[1:]:
        if line.strip() == "---":
            continue

        match = re.match(r"^([^：:]+)[：:](.*)$", line.strip())
        if match:
            label = match.group(1).strip()
            value = match.group(2).strip()
            if label in parsed:
                parsed[label] = value
                current_label = label
                continue

        if current_label:
            parsed[current_label] = "\n".join(
                part for part in (parsed[current_label], line.strip()) if part
            )

    return parsed


def collect_weekly_preview_entries(daily_content, daily_path, *, persist=True):
    preview_json_path = resolve_preview_json_path(daily_path)
    preview_markdown_path = resolve_preview_markdown_path(daily_path)
    existing_entries = load_preview_entries(preview_json_path)
    existing_by_date = {
        entry.get("date", ""): entry
        for entry in existing_entries
        if entry.get("date", "")
    }

    weekly_entries = []
    merged_by_date = dict(existing_by_date)
    changed = False

    for section in split_report_sections(daily_content):
        parsed = parse_daily_section(section)
        date_key = extract_date_prefix(parsed.get("日期", ""))
        if not date_key:
            continue

        fallback_entry = build_preview_entry(date_key, parsed)
        merged_entry = merge_preview_entry(existing_by_date.get(date_key), fallback_entry)
        weekly_entries.append(merged_entry)

        if merged_by_date.get(date_key) != merged_entry:
            merged_by_date[date_key] = merged_entry
            changed = True

    if changed and persist:
        merged_entries = sorted(
            merged_by_date.values(),
            key=lambda entry: (entry.get("date", ""), entry.get("customer", "")),
        )
        save_preview_entries(merged_entries, preview_json_path)
        save_preview_markdown(merged_entries, preview_markdown_path)

    return weekly_entries


def format_preview_entry_lines(entries):
    lines = []
    for entry in entries:
        lines.append(
            "{date} | {customer} | 外勤:{is_onsite} | 类型:{work_type} | 归类:{classification} | 远程:{remote_support_count}次".format(
                date=entry.get("date", "未知日期"),
                customer=entry.get("customer", "未填写客户") or "未填写客户",
                is_onsite=entry.get("is_onsite", "未知"),
                work_type=entry.get("work_type", "未知"),
                classification=entry.get("classification", "未知"),
                remote_support_count=entry.get("remote_support_count", 0),
            )
        )
    return lines


def normalize_plan_items(plan_text):
    items = []
    normalized_text = plan_text.replace("\r\n", "\n")
    for raw_line in normalized_text.splitlines():
        for raw_item in re.split(r"[；;]+", raw_line):
            line = raw_item.strip()
            if not line:
                continue
            line = re.sub(r"^\d+[：:]\s*", "", line)
            line = line.strip("；;")
            if line:
                items.append(line)

    return "\n".join(
        f"{index}：{item}；"
        for index, item in enumerate(items, start=1)
    )


def read_next_week_plan(args):
    if args.plan_text:
        return normalize_plan_items(" ".join(args.plan_text))

    if not sys.stdin.isatty():
        return normalize_plan_items(sys.stdin.read().strip())

    # 不再跳转编辑器，直接在终端交互输入
    print("\n请输入下周工作安排，直接回车结束：")
    if is_apple_terminal():
        print(f"[提示] 长内容请先复制，再输入 {APPLE_TERMINAL_CLIPBOARD_COMMAND} 读取剪贴板。")
    items = []
    line_number = 1
    while True:
        user_input = read_brief_input(f"{line_number}：").strip()
        if not user_input:
            break
        items.append(user_input)
        line_number += 1

    if not items:
        return ""
    return normalize_plan_items("\n".join(items))


def extract_weekly_title(report_text):
    first_line = next((line.strip() for line in report_text.splitlines() if line.strip()), "")
    return first_line if WEEKLY_TITLE_PATTERN.match(first_line) else ""


def split_weekly_sections(content):
    sections = []
    for raw_section in WEEKLY_SECTION_PATTERN.split(content):
        section = canonicalize_report_block(raw_section)
        if section and extract_weekly_title(section):
            sections.append(section)
    return sections


def get_recent_weekly_reports(file_path, limit=3):
    if not file_path.exists():
        return "暂无历史周报参考数据。"

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"读取历史周报失败：{exc}"

    sections = split_weekly_sections(content)
    if not sections:
        return "暂无历史周报参考数据。"
    return "\n\n".join(sections[-limit:])


def extract_reflections_from_reports(history_content):
    reflections = []
    for section in split_weekly_sections(history_content):
        match = re.search(r"感想[：:]\s*(.*)$", section, flags=re.DOTALL)
        if not match:
            continue
        reflection = match.group(1).strip().strip("-").strip()
        if reflection:
            reflections.append(reflection)
    return reflections


def build_stats_strings(stats):
    project_names = "、".join(stats["project_delivered_customers"]) or "无"
    onsite_names = "、".join(stats["onsite_service_customers"]) or "无"
    project_count = len(stats["project_delivered_customers"])
    onsite_count = stats.get("onsite_service_count", 0)
    remote_count = stats["remote_count"]
    total_service = onsite_count + remote_count
    return {
        "project_names": project_names,
        "onsite_names": onsite_names,
        "project_count": project_count,
        "onsite_count": onsite_count,
        "remote_count": remote_count,
        "total_service": total_service,
    }


def generate_reflection(daily_content, history):
    reflections = extract_reflections_from_reports(history)
    prompt = f"""
你是一个专业的周报感想写作者。请根据本周工作内容生成一段自然、真实、像真人写的感想。

### 历史感想参考（学习其语气和表达习惯，不要模仿其具体内容）
{chr(10).join(reflections) if reflections else "暂无"}

### 本周日报汇总（从中提取真实地点、客户和事件）
{daily_content}

### 写作要求（核心：去AI感）
1. **口语化表达**：像在跟同事聊天一样。多用“比较顺利”、“还得再跟进”、“挺及时的”等词，少用“进一步沉淀”、“持续推进”、“响应效率”等大词空词。
2. **细节驱动**：必须包含日报中提到的具体地点、具体客户或具体事件（如磁盘故障、硬件损坏）。
3. **真实情感**：可以提到工作节奏紧凑、某个问题解决得顺利、或者某个硬件问题让人头疼。
4. **长度限制**：1到2句话，不要超过3句。
5. **严禁事项**：严禁出现“本周总结”、“本周感想”等标题，严禁使用“总之”、“通过...提升了...”等AI常用句式。

请只输出感想正文（纯文本）：
    """.strip()

    try:
        client = OpenAI(api_key=get_weekly_api_key(), base_url=BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **build_chat_completion_options(
                temperature=0.6,
                model_name=MODEL_NAME,
            ),
        )
        result = extract_message_content(response.choices[0].message.content)
        if not result or _is_ai_generated_reflection(result):
            return DEFAULT_REFLECTION
        return result
    except Exception:
        return DEFAULT_REFLECTION


_AI_REFLECTION_PATTERNS = (
    "本周总结", "本周感想", "总之", "提升了", "进一步", "持续推进",
    "响应效率", "沉淀经验", "在此基础上", "收获颇丰", "成长了不少",
)


def _is_ai_generated_reflection(text):
    """Return True if the text looks AI-generated (too generic or contains banned phrases)."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) < 6 or len(cleaned) > 200:
        return True
    lower = cleaned.lower()
    return sum(1 for p in _AI_REFLECTION_PATTERNS if p in lower) >= 1


def build_weekly_prompt(daily_content, history, stats, next_week_plan, reflection, title):
    stats_strings = build_stats_strings(stats)
    project_names = stats_strings["project_names"]
    onsite_names = stats_strings["onsite_names"]
    project_count = stats_strings["project_count"]
    onsite_count = stats_strings["onsite_count"]
    remote_count = stats_strings["remote_count"]
    total_service = stats_strings["total_service"]
    work_days = stats["work_days"]
    next_week_section = f"\n{next_week_plan}" if next_week_plan else ""

    return f"""
你是一个严格的文本转换工具。请根据【统计数据】和【本周日报汇总】生成一份【正式周报】。

### 本周真实统计数据（必须准确使用）
- POC测试次数：{stats["poc_count"]}次
- 项目交付数量：{project_count}个
- 项目交付名称：{project_names}
- 售后次数：{total_service}次（{onsite_count}个上门，{remote_count}个远程）
- 售后上门客户：{onsite_names}
- 远程服务支持次数：{remote_count}次
- 服务出勤天数：{work_days}天

### 写作规范
1. **标题**：必须是“{title}”。
2. **严禁事项**：严禁在正文中再次输出“感想”这两个字，严禁输出任何感想内容。
3. **格式**：保持纯文本，不要 Markdown，不要项目符号。
4. **一：服务支持情况**：
1：服务支持：本周poc测试 {stats["poc_count"]}次，项目交付 {project_count}个（{project_names}），售后 {total_service}次，{onsite_count}个上门，{remote_count}个远程，服务出勤{work_days}天；
2：重大项目进展情况＆重大问题处理情况：暂无；
5. **二：周关键工作执行情况**：
关键工作1：项目交付；
执行情况：本周交付项目 {project_count}个（包含{project_names}），项目已交付；
关键工作2：项目售后问题及时处理；
执行情况：本周处理售后问题 {total_service}次，均已当天完成问题处理以及相关工单以及技术服务填写；
6. **其他工作**：暂无。
7. **下周安排**：保留“关键工作计划：项目实施、售后问题处理、客情维护”，并把给定的下周安排写进去。

### 标准模板（仅填充内容，严禁在末尾添加感想）
{title}
本周总结：
一：服务支持情况
1：服务支持：本周poc测试 {stats["poc_count"]}次，项目交付 {project_count}个（{project_names}），售后 {total_service}次，{onsite_count}个上门，{remote_count}个远程，服务出勤{work_days}天；
2：重大项目进展情况＆重大问题处理情况：暂无；

二：周关键工作执行情况
关键工作1：项目交付；
执行情况：本周交付项目 {project_count}个（包含{project_names}），项目已交付；

关键工作2：项目售后问题及时处理；
执行情况：本周处理售后问题 {total_service}次，均已当天完成问题处理以及相关工单以及技术服务填写；

其他工作：
暂无
下周安排：
一：服务支持工作；{next_week_section}

关键工作计划：项目实施、售后问题处理、客情维护
---

### 历史周报样本（学习行文风格和措辞）
{history}

### 本周日报汇总（仅供参考工作细节）
{daily_content}

正式周报内容（严禁包含感想）：
""".strip()


def normalize_weekly_report_text(report_text, title, reflection):
    content = strip_markdown_noise(report_text)
    if not content:
        raise RuntimeError("AI 未返回有效周报内容。")

    lines = [line.rstrip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("AI 未返回有效周报内容。")

    # 1. 强制修正标题
    if WEEKLY_TITLE_PATTERN.match(lines[0]):
        lines[0] = title
    elif extract_date_prefix(lines[0]):
        lines[0] = title
    else:
        lines.insert(0, title)

    # 2. 全局清洗：移除 AI 可能生成的任何包含“感想”的行，统一由脚本拼接
    cleaned_lines = []
    seen_headers = set()
    in_ai_reflection_block = False
    reflection_stop_patterns = (
        r"^---$",
        r"^其他工作[：:]",
        r"^下周安排[：:]",
        r"^关键工作计划[：:]",
        r"^[一二三四五六七八九十][：:].*",
        r"^关键工作[12][：:].*",
        r"^执行情况[：:].*",
    )

    for line in lines:
        clean_line = line.strip()

        # 跳过重复标题
        if clean_line in seen_headers and clean_line in {"本周总结：", "本周总结:", title}:
            continue

        # 识别并跳过 AI 生成的感想部分（因为我们要统一拼接）
        if re.match(r"^感想[：:]", clean_line):
            in_ai_reflection_block = True
            continue

        if in_ai_reflection_block:
            if not any(re.match(pattern, clean_line) for pattern in reflection_stop_patterns):
                continue
            in_ai_reflection_block = False

        if clean_line:
            seen_headers.add(clean_line)
        cleaned_lines.append(line)

    # 3. 拼接正文
    text = "\n".join(cleaned_lines)

    # 4. 统一在末尾追加唯一的感想部分
    # 确保末尾没有多余的分隔符，然后追加
    text = text.rstrip().rstrip("-").rstrip()
    text = f"{text}\n\n感想：\n{reflection}"

    return canonicalize_report_block(text)


def generate_weekly_report(daily_content, history, stats, next_week_plan, reflection, title):
    api_key = get_weekly_api_key()
    if not api_key:
        raise RuntimeError(
            "未配置 API Key。请设置 WEEKLY_REPORT_API_KEY, DAILY_REPORT_API_KEY 或 SILICONFLOW_API_KEY。"
        )

    client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0)
    response = request_chat_completion(client, {
        "messages": [{"role": "user", "content": build_weekly_prompt(
            daily_content, history, stats, next_week_plan, reflection, title,
        )}],
        **build_chat_completion_options(temperature=0.2, model_name=MODEL_NAME),
    }, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
    if not getattr(response, "choices", None):
        raise RuntimeError("AI 服务未返回可用结果。")
    if getattr(response.choices[0], "finish_reason", None) == "length":
        raise RuntimeError("AI 周报内容被截断，未重复请求；请缩短本次输入后再生成。")
    content = extract_message_content(response.choices[0].message.content)
    return normalize_weekly_report_text(content, title, reflection)


def save_weekly_report(report_text, file_path):
    report_block = canonicalize_report_block(report_text)
    if not report_block:
        raise RuntimeError("周报内容为空，无法写入文件。")

    report_title = extract_weekly_title(report_block)
    existing_sections = []
    if file_path.exists():
        existing_sections = split_weekly_sections(file_path.read_text(encoding="utf-8"))

    updated = False
    merged_sections = []
    for section in existing_sections:
        if report_title and extract_weekly_title(section) == report_title and not updated:
            merged_sections.append(report_block)
            updated = True
            continue
        merged_sections.append(section)

    if not updated:
        merged_sections.append(report_block)

    write_text_atomically(file_path, "\n\n".join(merged_sections) + "\n")
    return "更新" if updated else "新增"


def get_user_reflection(args, daily_content, history):
    if args.reflection is not None:
        return args.reflection.strip() or DEFAULT_REFLECTION

    if not sys.stdin.isatty():
        return generate_reflection(daily_content, history)

    custom_reflection = read_brief_input(
        "\n是否有感想需要添加？(直接回车使用 AI 生成): "
    ).strip()
    if custom_reflection:
        return custom_reflection
    print("[AI 正在生成感想...]")
    return generate_reflection(daily_content, history)


def main(argv=None):
    configure_stdio()
    args = parse_args(argv)
    daily_path = resolve_daily_source_path(args.daily_report_path)
    weekly_path = resolve_weekly_report_path(args.weekly_report_path, daily_path=daily_path)
    workdays = get_report_week_days()
    title = build_weekly_title()

    print("--- 周报 AI 助手 ---")
    print(f"日报来源路径：{daily_path}")
    print(f"周报输出路径：{weekly_path}")
    print(f"统计区间：{format_workday_range(workdays)}")
    print(f"前瞻表路径：{resolve_preview_markdown_path(daily_path)}")

    try:
        daily_content = extract_daily_reports(daily_path, workdays)
        preview_entries = collect_weekly_preview_entries(daily_content, daily_path, persist=not args.preview_only)
        stats = analyze_preview_entries(preview_entries)
        stats_strings = build_stats_strings(stats)

        print("\n=== 本周前瞻表 ===")
        for line in format_preview_entry_lines(preview_entries):
            print(line)

        print("\n=== 本周统计 ===")
        print(f"POC测试：{stats['poc_count']}次")
        print(
            f"项目交付：{stats_strings['project_count']}个 - {stats_strings['project_names']}"
        )
        print(
            f"售后次数：{stats_strings['total_service']}次"
            f"（{stats_strings['onsite_count']}个上门，{stats_strings['remote_count']}个远程）"
        )
        print(f"服务出勤：{stats['work_days']}天")

        next_week_plan = read_next_week_plan(args)
        history = get_recent_weekly_reports(weekly_path)
        reflection = get_user_reflection(args, daily_content, history)

        print("\n[AI 正在分析历史周报风格并生成周报...]")
        result = generate_weekly_report(
            daily_content,
            history,
            stats,
            next_week_plan,
            reflection,
            title,
        )

        print("\n" + "=" * 50)
        print(result)
        print("=" * 50)

        if args.preview_only:
            print("\n[预览模式] 未写入文件。")
            return 0

        auto_confirm = should_auto_confirm(args)
        if not auto_confirm:
            confirm = read_brief_input("\n是否写入周报文件？(y/n): ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("\n已取消保存。")
                return 0
        elif not args.yes:
            print("\n[提示] 检测到非交互输入，已自动写入；如仅需预览可使用 --preview-only。")

        save_action = save_weekly_report(result, weekly_path)
        clipboard_copied = False
        if not args.no_clipboard:
            clipboard_copied = copy_to_clipboard(result)

        if args.no_clipboard:
            print(f"\n[成功] 周报已{save_action}到文件。")
        elif clipboard_copied:
            print(f"\n[成功] 周报已{save_action}到文件，并复制到剪贴板。")
        else:
            print(f"\n[成功] 周报已{save_action}到文件，但复制剪贴板失败。")
        return 0
    except EOFError:
        emit_terminal_message("\n输入已结束，已安全退出。")
        return 0
    except KeyboardInterrupt:
        print("\n已安全退出。")
        return 130
    except Exception as exc:
        print(f"\n程序运行出错：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
