from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


# Weekday portion after the opening paren: 1-3 chars covering single (五),
# short (周五) and full (星期五) forms so user-authored dates all parse.
_WEEKDAY_CHARS = r"[周星期一二三四五六日天]{1,3}"
REPORT_DATE_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}[（(]" + _WEEKDAY_CHARS + r"[）)]$")
_DATE_HEADING_BODY = (
    r"\d{4}(?:[./-]|年)\d{1,2}(?:[./-]|月)\d{1,2}日?"
    r"(?:[（(]\s*" + _WEEKDAY_CHARS + r"\s*[）)]|\s+" + _WEEKDAY_CHARS + r")?"
)
_DATE_HEADING_PATTERN = re.compile(r"^" + _DATE_HEADING_BODY + r"$")
REPORT_SECTION_PATTERN = re.compile(r"(?m)(?=^\s{0,3}(?:#+\s*)?" + _DATE_HEADING_BODY + r"\s*$)")
SECTION_LABELS = [
    "今日总结",
    "服务客户",
    "对应销售",
    "问题描述",
    "处理结果",
    "处理情况",
    "远程服务支持",
    "明日安排",
    "明日工作计划",
    "感想",
]
REPORT_FIELD_ALIASES = {
    "今日总结": "今日总结",
    "服务客户": "服务客户",
    "对应销售": "对应销售",
    "问题描述": "问题描述",
    "处理结果": "处理结果",
    "处理情况": "处理情况",
    "远程服务支持": "远程服务支持",
    "明日安排": "明日安排",
    "明日工作计划": "明日安排",
    "感想": "感想",
}


def join_structured_value(existing, addition):
    existing_text = (existing or "").strip()
    addition_text = (addition or "").strip()
    if not existing_text:
        return addition_text
    if not addition_text:
        return existing_text
    return f"{existing_text}\n{addition_text}"


def parse_report_fields(report_text):
    parsed = {label: "" for label in SECTION_LABELS}
    current_label = None
    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or REPORT_DATE_PATTERN.match(line):
            continue
        match = re.match(r"^([^：:]+)[：:](.*)$", line)
        if match:
            label = REPORT_FIELD_ALIASES.get(match.group(1).strip())
            if label:
                parsed[label] = match.group(2).strip()
                current_label = label
                continue
        if current_label:
            parsed[current_label] = join_structured_value(parsed[current_label], line)
    return parsed


def is_service_work_start_line(line):
    return line == "服务支持工作" or bool(re.match(r"^\d+[：:]服务支持工作$", line))


def parse_report_service_entries(report_text):
    entries = []
    current_entry = None
    current_label = None
    service_labels = {"服务客户", "对应销售", "问题描述", "处理结果", "处理情况"}

    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or REPORT_DATE_PATTERN.match(line):
            continue
        if line in {"今日总结：", "今日总结:"}:
            current_label = None
            continue
        if line.startswith(
            ("远程服务支持：", "远程服务支持:", "明日安排：", "明日安排:", "明日工作计划：", "明日工作计划:")
        ):
            break
        if is_service_work_start_line(line):
            current_entry = {label: "" for label in service_labels}
            entries.append(current_entry)
            current_label = None
            continue

        match = re.match(r"^([^：:]+)[：:](.*)$", line)
        if match:
            label = REPORT_FIELD_ALIASES.get(match.group(1).strip())
            if label in service_labels:
                if current_entry is None:
                    current_entry = {label: "" for label in service_labels}
                    entries.append(current_entry)
                current_entry[label] = match.group(2).strip()
                current_label = label
                continue

        if current_entry is not None and current_label:
            current_entry[current_label] = join_structured_value(
                current_entry[current_label],
                line,
            )

    return [
        entry
        for entry in entries
        if any(value.strip() for value in entry.values())
    ]


def extract_report_customer_names(report_text):
    customers = [
        entry.get("服务客户", "").strip()
        for entry in parse_report_service_entries(report_text)
        if entry.get("服务客户", "").strip()
    ]
    if customers:
        return list(dict.fromkeys(customers))
    parsed = parse_report_fields(report_text)
    customer = parsed.get("服务客户", "").strip()
    return [customer] if customer else []


def build_daily_work_summary_from_report(report_text):
    lines = []
    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or REPORT_DATE_PATTERN.match(line):
            continue
        if line.startswith(
            ("明日安排：", "明日安排:", "明日工作计划：", "明日工作计划:")
        ):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def strip_markdown_noise(text):
    cleaned_lines = []
    for raw_line in text.replace("\r\n", "\n").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```") or re.fullmatch(r"[-*_]{3,}", stripped):
            continue
        line = re.sub(r"^\s{0,3}#+\s*", "", line).strip()
        line = re.sub(r"^[*-]\s*", "", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def canonicalize_report_block(report_text):
    lines = strip_markdown_noise(report_text).splitlines()
    if lines and _DATE_HEADING_PATTERN.fullmatch(lines[0]) and not REPORT_DATE_PATTERN.fullmatch(lines[0]):
        date_parts = re.match(r"^(\d{4})(?:[./-]|年)(\d{1,2})(?:[./-]|月)(\d{1,2})", lines[0])
        if date_parts is None:
            raise ValueError("日报日期标题无法解析。")
        report_date = datetime(int(date_parts.group(1)), int(date_parts.group(2)), int(date_parts.group(3)))
        lines[0] = f"{report_date:%Y.%m.%d}（{'一二三四五六日'[report_date.weekday()]}）"
    while lines and lines[0].strip() == "---":
        lines.pop(0)
    while lines and lines[-1].strip() == "---":
        lines.pop()
    content = "\n".join(line.rstrip() for line in lines).strip()
    return f"{content}\n---" if content else ""


def extract_report_date(report_text):
    lines = [line.strip() for line in report_text.splitlines() if line.strip()]
    return lines[0] if lines and REPORT_DATE_PATTERN.match(lines[0]) else ""


def split_report_sections(content):
    sections = []
    for raw_section in REPORT_SECTION_PATTERN.split(content):
        section = canonicalize_report_block(raw_section)
        if section and extract_report_date(section):
            sections.append(section)
    return sections


def normalize_report_date_to_iso(report_text):
    report_date = extract_report_date(report_text)
    if not report_date:
        raise RuntimeError("无法从日报内容中提取日期，无法联动纷享销客技术服务填写。")
    return datetime.strptime(report_date[:10], "%Y.%m.%d").strftime("%Y-%m-%d")


def normalize_report_date_argument(report_date):
    value = (report_date or "").strip()
    if not value:
        raise ValueError("日报日期不能为空。")
    for fmt in ("%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y.%m.%d")
        except ValueError:
            continue
    raise ValueError("无法识别日报日期，请使用 YYYY-MM-DD 或 YYYY.MM.DD。")


def find_saved_report_section(report_path, report_date=None):
    path = Path(report_path)
    if not path.exists():
        raise RuntimeError(f"未找到日报文件：{path}")
    sections = split_report_sections(path.read_text(encoding="utf-8"))
    if not sections:
        raise RuntimeError(f"日报文件中没有可补交的日报内容：{path}")
    target_date = normalize_report_date_argument(report_date) if report_date else extract_report_date(sections[-1])[:10]
    matching = [section for section in sections if extract_report_date(section).startswith(target_date)]
    if len(matching) > 1:
        raise RuntimeError(f"日报文件中 {target_date} 存在重复记录，无法确定唯一客户和内容，请先核对。")
    if matching:
        return matching[0]
    raise RuntimeError(f"未在日报文件中找到 {target_date} 对应的日报内容。")
