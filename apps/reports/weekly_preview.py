# -*- coding: utf-8 -*-
import json
import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from packages.report_domain.workday import (
    NON_ONSITE_HINTS,
    ONSITE_FALSE_VALUES,
    ONSITE_HINTS,
    ONSITE_TRUE_VALUES,
    REMOTE_MAIN_HINTS,
)
from ._shared import write_text_atomically


PREVIEW_JSON_FILENAME = "周报前瞻表.json"
PREVIEW_MARKDOWN_FILENAME = "周报前瞻表.md"

WORK_TYPE_IMPLEMENTATION = "实施"
WORK_TYPE_AFTER_SALES = "售后"
WORK_TYPE_POC = "POC"

WORK_TYPE_ALIASES = {
    "实施": WORK_TYPE_IMPLEMENTATION,
    "项目实施": WORK_TYPE_IMPLEMENTATION,
    "交付": WORK_TYPE_IMPLEMENTATION,
    "项目交付": WORK_TYPE_IMPLEMENTATION,
    "部署": WORK_TYPE_IMPLEMENTATION,
    "实施交付": WORK_TYPE_IMPLEMENTATION,
    "售后": WORK_TYPE_AFTER_SALES,
    "售后支持": WORK_TYPE_AFTER_SALES,
    "维护": WORK_TYPE_AFTER_SALES,
    "运维": WORK_TYPE_AFTER_SALES,
    "故障处理": WORK_TYPE_AFTER_SALES,
    "服务": WORK_TYPE_AFTER_SALES,
    "poc": WORK_TYPE_POC,
    "POC": WORK_TYPE_POC,
    "测试": WORK_TYPE_POC,
    "poc测试": WORK_TYPE_POC,
    "验证": WORK_TYPE_POC,
}

POC_KEYWORDS = ("poc", "测试环境", "演示环境", "验证环境")
IMPLEMENTATION_START_HINTS = (
    "项目部署",
    "新增",
    "新建",
    "初次",
    "机房部署",
    "VOI项目部署",
    "VPC部署",
    "底层部署",
    "系统制作",
)
IMPLEMENTATION_KEYWORDS = ("部署", "实施", "上线", "交付", "安装")
AFTER_SALES_HINTS = (
    "售后",
    "巡检",
    "收尾",
    "适配",
    "测试",
    "清洗",
    "维修",
    "保修",
    "排查",
    "异常",
    "故障",
    "失效",
    "掉线",
    "问题处理",
    "替换",
    "卡死",
    "清灰",
)
DATE_KEY_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}")


def normalize_date_key(value):
    text = str(value or "").strip()
    match = DATE_KEY_PATTERN.match(text)
    return match.group(0) if match else ""


def normalize_onsite_value(value, default="是"):
    if isinstance(value, bool):
        return "是" if value else "否"
    normalized = re.sub(r"\s+", "", str("" if value is None else value).strip()).lower()
    if not normalized:
        return default
    if normalized in ONSITE_FALSE_VALUES or any(
        marker in normalized for marker in ("否", "不外勤", "非外勤", "公司办公")
    ):
        return "否"
    if normalized in ONSITE_TRUE_VALUES:
        return "是"
    return default


def normalize_work_type(value, default=WORK_TYPE_AFTER_SALES):
    cleaned = str(value or "").strip()
    if not cleaned:
        return default

    lowered = re.sub(r"\s+", "", cleaned).lower()
    if lowered in {"poc", "poc测试"} or "poc" in lowered:
        return WORK_TYPE_POC

    for alias, canonical in WORK_TYPE_ALIASES.items():
        if lowered == re.sub(r"\s+", "", alias).lower():
            return canonical

    if any(keyword in cleaned for keyword in ("实施", "交付", "部署")):
        return WORK_TYPE_IMPLEMENTATION
    if any(keyword in cleaned for keyword in ("售后", "维护", "故障", "巡检")):
        return WORK_TYPE_AFTER_SALES
    if any(keyword in cleaned.lower() for keyword in POC_KEYWORDS):
        return WORK_TYPE_POC
    return default


def count_remote_support_items(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return 0

    numbered_items = re.findall(
        r"(?:^|[；;\n])\s*\d+[：:](.+?)(?=(?:[；;\n]\s*\d+[：:])|$)",
        cleaned,
    )
    if numbered_items:
        return len([item for item in numbered_items if item.strip()])

    return len([part for part in re.split(r"[；;\n]+", cleaned) if part.strip()])


def infer_onsite_value(problem, handling, remote_support):
    combined_main = " ".join(part for part in (problem, handling) if part).strip()
    combined_lower = combined_main.lower()
    if not combined_main:
        return "否" if remote_support else "是"

    if any(hint in combined_main for hint in REMOTE_MAIN_HINTS):
        return "否"
    if any(hint in combined_main for hint in NON_ONSITE_HINTS):
        return "否"
    if any(hint in combined_lower for hint in ONSITE_HINTS):
        return "是"
    if not combined_main and remote_support:
        return "否"
    return "是"


def infer_work_type(problem, handling, remote_support="", is_onsite="是"):
    combined = " ".join(part for part in (problem, handling, remote_support) if part).lower()
    if any(keyword in combined for keyword in POC_KEYWORDS):
        return WORK_TYPE_POC

    if normalize_onsite_value(is_onsite, default="否") != "是":
        return WORK_TYPE_AFTER_SALES

    if (
        any(hint.lower() in combined for hint in IMPLEMENTATION_START_HINTS)
        and "收尾" not in combined
        and "测试" not in combined
        and "适配" not in combined
    ):
        return WORK_TYPE_IMPLEMENTATION

    if any(hint.lower() in combined for hint in AFTER_SALES_HINTS):
        return WORK_TYPE_AFTER_SALES

    if any(keyword.lower() in combined for keyword in IMPLEMENTATION_KEYWORDS):
        return WORK_TYPE_IMPLEMENTATION

    return WORK_TYPE_AFTER_SALES


def build_preview_entry(date_value, parsed_fields, structured_fields=None):
    structured_fields = structured_fields or {}
    date_key = normalize_date_key(date_value or parsed_fields.get("日期", ""))
    problem = str(parsed_fields.get("问题描述", "") or "").strip()
    handling = str(parsed_fields.get("处理情况", "") or "").strip()
    remote_support = str(parsed_fields.get("远程服务支持", "") or "").strip()

    explicit_onsite = structured_fields.get("is_onsite", "")
    has_explicit_onsite = explicit_onsite is not None and bool(str(explicit_onsite).strip())
    is_onsite = (
        normalize_onsite_value(explicit_onsite)
        if has_explicit_onsite
        else infer_onsite_value(problem, handling, remote_support)
    )

    explicit_work_type = structured_fields.get("work_type", "")
    work_type = (
        normalize_work_type(explicit_work_type)
        if explicit_work_type
        else infer_work_type(problem, handling, remote_support, is_onsite=is_onsite)
    )

    classification = _preview_classification(is_onsite, work_type)

    return {
        "date": date_key,
        "customer": str(parsed_fields.get("服务客户", "") or "").strip(),
        "sales_contact": str(parsed_fields.get("对应销售", "") or "").strip(),
        "is_onsite": is_onsite,
        "is_onsite_source": "explicit" if has_explicit_onsite else "inferred",
        "work_type": work_type,
        "work_type_source": "explicit" if explicit_work_type else "inferred",
        "classification": classification,
        "problem": problem,
        "result_status": str(parsed_fields.get("处理结果", "") or "").strip(),
        "handling": handling,
        "remote_support_text": remote_support,
        "remote_support_count": count_remote_support_items(remote_support),
    }


def _preview_classification(is_onsite, work_type):
    if work_type == WORK_TYPE_POC:
        classification = "POC"
    elif is_onsite == "是" and work_type == WORK_TYPE_IMPLEMENTATION:
        classification = "外勤实施"
    elif is_onsite == "是" and work_type == WORK_TYPE_AFTER_SALES:
        classification = "外勤售后"
    elif work_type == WORK_TYPE_AFTER_SALES:
        classification = "远程/非外勤售后"
    else:
        classification = work_type

    return classification


def merge_preview_entry(existing_entry, fallback_entry):
    if not existing_entry:
        return dict(fallback_entry)

    merged = {**existing_entry, **fallback_entry}
    for field in ("work_type", "is_onsite"):
        source_key = f"{field}_source"
        old_source = existing_entry.get(source_key, existing_entry.get("work_type_source", ""))
        if fallback_entry.get(source_key) != "explicit" and old_source != "inferred":
            if existing_entry.get(field) not in (None, ""):
                merged[field] = existing_entry[field]
                if source_key in existing_entry:
                    merged[source_key] = existing_entry[source_key]
    if (
        "is_onsite_source" not in existing_entry
        and normalize_onsite_value(existing_entry.get("is_onsite")) == "否"
        and normalize_onsite_value(fallback_entry.get("is_onsite")) == "是"
        and fallback_entry.get("is_onsite_source") != "explicit"
    ):
        merged["is_onsite"] = "否"
        merged["is_onsite_source"] = "legacy"
    if not merged.get("sales_contact") and existing_entry.get("customer") == merged.get("customer"):
        merged["sales_contact"] = existing_entry.get("sales_contact", "")

    merged["date"] = normalize_date_key(merged.get("date", "")) or fallback_entry.get("date", "")
    merged["is_onsite"] = normalize_onsite_value(merged.get("is_onsite", "是"))
    merged["work_type"] = normalize_work_type(merged.get("work_type", ""))
    merged["remote_support_count"] = int(merged.get("remote_support_count", 0) or 0)
    merged["classification"] = _preview_classification(merged["is_onsite"], merged["work_type"])
    return merged


def sort_preview_entries(entries):
    return sorted(
        entries,
        key=lambda entry: (
            normalize_date_key(entry.get("date", "")),
            str(entry.get("customer", "")),
        ),
    )


def resolve_preview_json_path(daily_report_path):
    return Path(daily_report_path).expanduser().resolve().with_name(PREVIEW_JSON_FILENAME)


def resolve_preview_markdown_path(daily_report_path):
    return Path(daily_report_path).expanduser().resolve().with_name(PREVIEW_MARKDOWN_FILENAME)


class PreviewDataError(RuntimeError):
    """Auxiliary source is unreadable; never overwrite it during reconstruction."""


def load_preview_entries(json_path):
    path = Path(json_path)
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreviewDataError(f"周报前瞻表读取失败，已停止覆盖：{path}") from exc

    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise PreviewDataError(f"周报前瞻表格式异常，已停止覆盖：{path}")

    normalized_entries = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise PreviewDataError(f"周报前瞻表含有非对象记录，已停止覆盖：{path}")
        date_key = normalize_date_key(raw_entry.get("date", ""))
        if not date_key:
            raise PreviewDataError(f"周报前瞻表缺少有效日期，已停止覆盖：{path}")
        try:
            datetime.strptime(date_key, "%Y.%m.%d")
            support_count = int(raw_entry.get("remote_support_count", 0) or 0)
            if support_count < 0:
                raise ValueError("negative support count")
        except (ValueError, TypeError) as exc:
            raise PreviewDataError(f"周报前瞻表日期或次数异常，已停止覆盖：{path}") from exc
        normalized_entries.append(
            {
                **raw_entry,
                "date": date_key,
                "customer": str(raw_entry.get("customer", "") or "").strip(),
                "sales_contact": str(raw_entry.get("sales_contact", "") or "").strip(),
                "is_onsite": normalize_onsite_value(raw_entry.get("is_onsite", "是")),
                "work_type": normalize_work_type(raw_entry.get("work_type", "")),
                "work_type_source": str(raw_entry.get("work_type_source", "") or "").strip(),
                "classification": str(raw_entry.get("classification", "") or "").strip(),
                "problem": str(raw_entry.get("problem", "") or "").strip(),
                "result_status": str(raw_entry.get("result_status", "") or "").strip(),
                "handling": str(raw_entry.get("handling", "") or "").strip(),
                "remote_support_text": str(raw_entry.get("remote_support_text", "") or "").strip(),
                "remote_support_count": support_count,
            }
        )
    onsite_by_day: dict[str, str] = {}
    for entry in normalized_entries:
        day, mode = entry["date"], entry["is_onsite"]
        if day in onsite_by_day and onsite_by_day[day] != mode:
            raise RuntimeError("同日外勤状态存在冲突，已停止覆盖周报前瞻表。")
        onsite_by_day[day] = mode
    return sort_preview_entries(normalized_entries)


def save_preview_entries(entries, json_path):
    path = Path(json_path)
    payload = {"entries": sort_preview_entries(entries)}
    write_text_atomically(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def build_preview_markdown(entries):
    lines = [
        "# 周报前瞻表",
        "",
        "> 由 `rb` 自动维护，`zb` 优先读取；工作类型以日报录入为准，缺失时脚本会保守推断。",
        "",
        "| 日期 | 客户 | 是否外勤 | 工作类型 | 周报归类 | 远程支持次数 | 对应销售 | 问题描述 |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]

    for entry in sort_preview_entries(entries):
        problem = str(entry.get("problem", "") or "").replace("|", "\\|").strip()
        if len(problem) > 40:
            problem = f"{problem[:40]}..."
        lines.append(
            "| {date} | {customer} | {is_onsite} | {work_type} | {classification} | {remote_support_count} | {sales_contact} | {problem} |".format(
                date=entry.get("date", ""),
                customer=str(entry.get("customer", "") or "").replace("|", "\\|"),
                is_onsite=entry.get("is_onsite", ""),
                work_type=entry.get("work_type", ""),
                classification=entry.get("classification", ""),
                remote_support_count=entry.get("remote_support_count", 0),
                sales_contact=str(entry.get("sales_contact", "") or "").replace("|", "\\|"),
                problem=problem,
            )
        )

    return "\n".join(lines).rstrip() + "\n"


def save_preview_markdown(entries, markdown_path):
    path = Path(markdown_path)
    write_text_atomically(path, build_preview_markdown(entries))


def upsert_preview_entry(entry, json_path, markdown_path):
    entries = load_preview_entries(json_path)
    updated = False
    merged_entries = []
    for current in entries:
        if current.get("date", "") == entry.get("date", "") and not updated:
            merged_entries.append(merge_preview_entry(current, entry))
            updated = True
            continue
        merged_entries.append(current)

    if not updated:
        merged_entries.append(dict(entry))

    sorted_entries = sort_preview_entries(merged_entries)
    if sorted_entries != entries:
        save_preview_entries(sorted_entries, json_path)

    markdown_path = Path(markdown_path)
    expected_markdown = build_preview_markdown(sorted_entries)
    try:
        current_markdown = markdown_path.read_text(encoding="utf-8")
    except OSError:
        current_markdown = ""
    if current_markdown != expected_markdown:
        write_text_atomically(markdown_path, expected_markdown)


class PreviewStatistics(TypedDict):
    poc_count: int
    project_delivered_customers: list[str]
    onsite_service_customers: list[str]
    onsite_service_count: int
    remote_count: int
    work_days: int


def analyze_preview_entries(entries) -> PreviewStatistics:
    stats: PreviewStatistics = {
        "poc_count": 0,
        "project_delivered_customers": [],
        "onsite_service_customers": [],
        "onsite_service_count": 0,
        "remote_count": 0,
        "work_days": 0,
    }

    seen_onsite_days = set()
    for entry in entries:
        date_key = normalize_date_key(entry.get("date", ""))
        customer = str(entry.get("customer", "") or "").strip()
        work_type = normalize_work_type(entry.get("work_type", ""))
        is_onsite = normalize_onsite_value(entry.get("is_onsite", "是"))
        remote_count = int(entry.get("remote_support_count", 0) or 0)
        stats["remote_count"] += remote_count

        if date_key and is_onsite == "是":
            seen_onsite_days.add(date_key)

        if work_type == WORK_TYPE_POC:
            stats["poc_count"] += 1
            continue

        if work_type == WORK_TYPE_IMPLEMENTATION:
            if customer and customer not in stats["project_delivered_customers"]:
                stats["project_delivered_customers"].append(customer)
            continue

        if work_type == WORK_TYPE_AFTER_SALES and is_onsite == "是":
            stats["onsite_service_count"] += 1
            if customer and customer not in stats["onsite_service_customers"]:
                stats["onsite_service_customers"].append(customer)

    stats["work_days"] = len(seen_onsite_days)
    return stats
