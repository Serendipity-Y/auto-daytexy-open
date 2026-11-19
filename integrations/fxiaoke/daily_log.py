"""Daily-log CRM boundary with browser automation compatibility fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module

from packages.report_domain.report_format import (
    build_daily_work_summary_from_report,
    extract_report_customer_names,
    parse_report_fields,
)


DATE_LINE_PATTERN = re.compile(r"^(\d{4}\.\d{2}\.\d{2})(?:[（(].*[）)])?$")


@dataclass
class DailyLogDraft:
    report_date_iso: str
    report_date_cn: str
    customer_name: str
    work_summary: str
    tomorrow_plan: str
    reflection: str
    verification_snippet: str


def extract_report_date(section):
    first_line = section.splitlines()[0].strip()
    match = DATE_LINE_PATTERN.match(first_line)
    if not match:
        raise RuntimeError("目标日报缺少可识别的日期标题。")
    return datetime.strptime(match.group(1), "%Y.%m.%d")


def build_work_summary(fields):
    lines = [
        "今日总结：",
        (fields.get("今日总结") or "服务支持工作").strip(),
        f"服务客户：{fields.get('服务客户', '').strip()}",
        f"对应销售：{fields.get('对应销售', '').strip()}",
        f"问题描述：{fields.get('问题描述', '').strip()}",
        f"处理结果：{fields.get('处理结果', '').strip()}",
        f"处理情况：{fields.get('处理情况', '').strip()}",
        "",
        "远程服务支持：",
    ]
    remote_support = fields.get("远程服务支持", "").strip()
    if remote_support:
        lines.append(remote_support)
    return "\n".join(lines).strip()


def build_tomorrow_plan(fields):
    return f"明日安排：{fields.get('明日安排', '').strip()}".strip()


def build_daily_log_draft(report_section):
    report_date = extract_report_date(report_section)
    fields = parse_report_fields(report_section)
    customer_names = extract_report_customer_names(report_section)
    customer_name = "、".join(customer_names) if customer_names else fields.get("服务客户", "").strip()
    tomorrow_plan = build_tomorrow_plan(fields)
    if not customer_name:
        raise RuntimeError("日报缺少“服务客户”，无法发布纷享销客日报。")
    if tomorrow_plan == "明日安排：":
        raise RuntimeError("日报缺少“明日安排”，无法发布纷享销客日报。")

    process_text = fields.get("处理情况", "").strip()
    verification_source = tomorrow_plan if tomorrow_plan != "明日安排：" else process_text or customer_name
    return DailyLogDraft(
        report_date_iso=report_date.strftime("%Y-%m-%d"),
        report_date_cn=report_date.strftime("%Y年%m月%d日"),
        customer_name=customer_name,
        work_summary=build_daily_work_summary_from_report(report_section) or build_work_summary(fields),
        tomorrow_plan=tomorrow_plan,
        reflection="",
        verification_snippet=verification_source[:32],
    )


def __getattr__(name: str):
    """Resolve attributes late so existing patches keep working during migration."""
    return getattr(import_module("fxiaoke_daily_log"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(import_module("fxiaoke_daily_log"))))
