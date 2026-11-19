"""Shared workday classification without importing either application entrypoint."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .report_format import normalize_report_date_to_iso, parse_report_fields


PREVIEW_JSON_FILENAME = "周报前瞻表.json"
REMOTE_MAIN_SERVICE_START_DATE = "2026-07-13"
ONSITE_TRUE_VALUES = {"", "是", "y", "yes", "true", "1", "外勤"}
ONSITE_FALSE_VALUES = {"否", "n", "no", "false", "0", "非外勤", "不外勤", "公司", "公司办公", "在公司"}
REMOTE_MAIN_HINTS = ("远程指导", "远程处理", "远程解决", "远程排查", "远程沟通", "远程支持", "电话处理", "线上处理")
ONSITE_HINTS = ("现场", "机房", "上门", "驻场", "巡检", "到校", "到场", "入校", "驻校", "集成商", "网线", "交换机", "机器排序", "ip规整", "终端bios", "bios修改", "底层软件安装", "系统制作", "多媒体教室", "拆机", "硬盘清洗", "硬件拆机", "教室")
NON_ONSITE_HINTS = ("适配", "不适配", "兼容", "兼容性", "研发确认", "寄往总部", "寄回总部", "总部处理", "返厂", "公司测试", "本地测试")


def normalize_onsite_value(value, default="是"):
    if isinstance(value, bool):
        return "是" if value else "否"
    normalized = re.sub(r"\s+", "", str("" if value is None else value).strip()).lower()
    if not normalized:
        return default
    if normalized in ONSITE_FALSE_VALUES or any(marker in normalized for marker in ("否", "不外勤", "非外勤", "公司办公")):
        return "否"
    if normalized in ONSITE_TRUE_VALUES:
        return "是"
    return default


def infer_onsite_value(problem, handling, remote_support):
    combined_main = " ".join(part for part in (problem, handling) if part).strip()
    if not combined_main:
        return "否" if remote_support else "是"
    if any(hint in combined_main for hint in REMOTE_MAIN_HINTS + NON_ONSITE_HINTS):
        return "否"
    if any(hint in combined_main.lower() for hint in ONSITE_HINTS):
        return "是"
    return "是"


def resolve_preview_onsite_value(report_text, report_path):
    if not report_text or not report_path:
        return ""
    try:
        path = Path(report_path).expanduser().resolve().with_name(PREVIEW_JSON_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        report_date = normalize_report_date_to_iso(report_text).replace("-", ".")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        raise RuntimeError("周报前瞻表无法读取，已停止判断外勤状态，请先核对。") from exc
    entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise RuntimeError("周报前瞻表格式异常，已停止判断外勤状态。")
    if any(not isinstance(entry, dict) for entry in entries):
        raise RuntimeError("周报前瞻表含有非对象记录，已停止判断外勤状态。")
    matches = {
        normalize_onsite_value(entry["is_onsite"])
        for entry in entries
        if entry.get("date") == report_date and entry.get("is_onsite") not in (None, "")
    }
    if len(matches) > 1:
        raise RuntimeError("同日外勤状态存在冲突，已停止判断，请先核对前瞻表。")
    return matches.pop() if matches else ""


def is_onsite_workday(report_text=None, structured_fields=None, report_path=None):
    structured_value = (structured_fields or {}).get("is_onsite", "")
    if structured_value not in (None, ""):
        return normalize_onsite_value(structured_value) == "是"
    preview_value = resolve_preview_onsite_value(report_text, report_path)
    if preview_value:
        return normalize_onsite_value(preview_value) == "是"
    fields = parse_report_fields(report_text or "")
    return infer_onsite_value(fields.get("问题描述", ""), fields.get("处理情况", ""), fields.get("远程服务支持", "")) == "是"


def _explicit_work_type(value):
    if not isinstance(value, str):
        raise RuntimeError("明确工作类型必须为实施、售后或POC，已停止自动选单。")
    normalized = value.strip()
    if normalized.casefold() == "poc":
        return "POC"
    if normalized in {"实施", "售后"}:
        return normalized
    raise RuntimeError("明确工作类型必须为实施、售后或POC，已停止自动选单。")


def _unique_work_type_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("周报前瞻表包含重复JSON字段")
        result[key] = value
    return result


def resolve_report_work_type(report_text, report_path=None, structured_fields=None, customer_name=""):
    """Return only an explicitly chosen type belonging to this report customer.

    Inferred/default classifications are useful for summaries, but cannot
    authorize moving a service record to a different CRM workorder.
    """
    structured_fields = structured_fields or {}
    primary = str(structured_fields.get("service_customer") or parse_report_fields(report_text or "").get("服务客户", "")).strip()
    customer = str(customer_name or primary).strip()
    identity = lambda value: re.sub(r"\s+", "", str(value or "")).casefold()
    explicit = structured_fields.get("work_type", "")
    if isinstance(explicit, str):
        explicit = explicit.strip()
    if explicit not in (None, ""):
        value = _explicit_work_type(explicit)
        if not customer_name or (primary and identity(customer) == identity(primary)):
            return value
    if not report_text or not report_path or not customer:
        return ""
    path = Path(report_path).expanduser().resolve().with_name(PREVIEW_JSON_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_work_type_object)
        report_date = normalize_report_date_to_iso(report_text).replace("-", ".")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        raise RuntimeError("周报前瞻表无法读取，已停止判断工作类型，请先核对。") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise RuntimeError("周报前瞻表格式异常，已停止判断工作类型。")
    matches = set()
    for entry in entries:
        if (str(entry.get("date", "")).replace("-", ".") == report_date
                and identity(entry.get("customer", "")) == identity(customer)
                and entry.get("work_type_source") == "explicit"):
            matches.add(_explicit_work_type(entry.get("work_type")))
    if len(matches) > 1:
        raise RuntimeError("同日同客户明确工作类型存在冲突，已停止自动选单。")
    return matches.pop() if matches else ""
