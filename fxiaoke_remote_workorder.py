#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from packages.report_domain.history import (
    read_pending_queue_text,
    resolve_fxiaoke_pending_queue_path,
    resolve_fxiaoke_submission_history_path,
)
from daily_report import (
    build_fxiaoke_submission_intent_blocked_result,
    configure_stdio,
    finish_fxiaoke_direct_submission,
    is_onsite_workday,
    normalize_report_date_to_iso,
    parse_report_fields,
    parse_report_service_entries,
    read_brief_input,
    reserve_fxiaoke_submission_intent,
    resolve_report_path,
    split_remote_support_items,
)
from integrations.fxiaoke.tenant import require_service_owner, tenant_value
import fxiaoke_service_record as fx
from fxiaoke_service_record import (
    JSFW_ID_PATTERN,
    customer_matches_workorder,
    extract_remote_support_customer_name,
    normalize_customer_match_text,
    resolve_customer_primary_name,
    should_skip_remote_support_item,
)


REMOTE_WORKORDER_LABEL = "《远程服务工单》"
REMOTE_WORKORDER_MAIN_HOURS = "8"
REMOTE_WORKORDER_COMPLETION_STATUS = "已解决"
REMOTE_SUPPORT_EDIT_MIN_HOURS = Decimal("0.5")
REMOTE_SUPPORT_EDIT_MAX_HOURS = Decimal("8")
REMOTE_SUPPORT_EDIT_STEP_HOURS = Decimal("0.5")
REMOTE_WORKORDER_TYPE_FIELD_TITLES = (
    "全部业务类型",
    REMOTE_WORKORDER_LABEL,
    "远程服务工单",
)
REMOTE_WORKORDER_CUSTOMER_FIELD = "account_id"
REMOTE_WORKORDER_SUBMIT_TIMEOUT_SECONDS = 45
REMOTE_WORKORDER_POLL_MS = 300
# A freshly created work order is not queryable by customer until the CRM search
# index catches up. Re-query instead of declaring the submission uncertain.
REMOTE_WORKORDER_VERIFY_RETRY_SECONDS = 24
REMOTE_WORKORDER_VERIFY_RETRY_DELAY_MS = 4_000
REMOTE_WORKORDER_VERIFY_ATTEMPTS = 4
FORM_SELECT_SETTLE_MS = 700
# Wide enough for the slow grid re-render already observed on the lookup dialog,
# and shared across the progressive query rounds below.
FULL_LOOKUP_QUERY_TIMEOUT_SECONDS = 15
FULL_LOOKUP_SETTLE_MS = 1_200
CUSTOMER_SEARCH_QUERY_LIMIT = 8
CUSTOMER_MANUAL_SEARCH_LIMIT = 3
REMOTE_WORKORDER_PAGE_TIMEOUT_SECONDS = 8
DEFAULT_SERVICE_CATEGORY_CONFIG_PATH = Path(__file__).with_name(
    "fxiaoke_remote_workorder_categories.json"
)

# Reports can use shorthand while the CRM keeps a customer’s full name.
CUSTOMER_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团")

# Longest first, so "职业技术学院" is not clipped to "职业技术". Only pure
# stage-of-schooling words belong here: stripping "健康职业技术学院" would also
# delete "健康", which is part of the unit's identity.
CUSTOMER_STAGE_SUFFIXES = (
    "职业技术学院",
    "中等专业学校",
    "中等职业学校",
    "高级中学",
    "初级中学",
    "职业学院",
    "技术学院",
    "学院",
    "大学",
    "学校",
    "中学",
    "小学",
    "职高",
    "技校",
    "幼儿园",
)

CUSTOMER_ORDINAL_CHARS = "一二三四五六七八九十"

# Region prefixes too generic to be used as a fallback search term.
CUSTOMER_GENERIC_REGIONS = {"中国"}

# Similarity is a search/ranking hint, never proof of customer identity.
CUSTOMER_QUERY_MATCH_SCORE = 0.75
CUSTOMER_SCHOOL_SHORT_FORMS = {
    "中等": ("中等专业学校", "中等职业学校"),
    "中专": ("中等专业学校", "中等职业学校"),
    "职院": ("职业技术学院", "职业学院"),
    "职校": ("职业学校", "中等职业学校"),
    "职大": ("职业大学",),
    "高技": ("高级技工学校",),
    "技校": ("技工学校",),
}


class RemoteWorkorderNotFound(RuntimeError):
    artifacts: list[str]


class RemoteWorkorderConfirmationUncertain(RuntimeError):
    def __init__(self, message, *, workorder_id="", differences=None):
        super().__init__(message)
        self.status = "uncertain"
        self.workorder_id = workorder_id
        self.retry_action = "verify_crm"
        self.differences = list(differences or [])


class RemoteWorkorderIdentityMismatch(RuntimeError):
    artifacts: list[str]

    def __init__(self, kind, *, workorder_id, expected, observed):
        self.kind = str(kind).strip()
        self.workorder_id = str(workorder_id).strip()
        self.expected = str(expected or "").strip()
        self.observed = str(observed or "").strip()
        labels = {
            "customer": "客户",
            "date": "服务日期",
            "status": "工单状态",
        }
        label = labels.get(self.kind, "工单信息")
        super().__init__(
            f"工单 {self.workorder_id} {label}“{self.observed or '未识别'}”"
            f"与预期“{self.expected or '未识别'}”不一致。"
        )


class RemoteWorkorderHoursMismatch(RuntimeError):
    artifacts: list[str]

    def __init__(self, message, *, observed_hours=""):
        self.observed_hours = str(observed_hours or "").strip()
        super().__init__(message)


@dataclass
class RemoteWorkorderDraft:
    report_date_iso: str
    customer_name: str
    content_items: list[str]
    service_category: str
    service_category_value: str
    service_hours: str = ""
    includes_main_work: bool = False
    completion_status: str = REMOTE_WORKORDER_COMPLETION_STATUS
    source_completion_status: str = ""
    selected_customer_name: str = ""
    completion_status_options: tuple[str, ...] = ()

    @property
    def content(self):
        return "；".join(item.strip().strip("；;。") for item in self.content_items if item.strip()) + "；"


def serialize_remote_workorder_draft(draft):
    return {
        "report_date_iso": draft.report_date_iso,
        "customer_name": draft.customer_name,
        "content_items": list(draft.content_items),
        "service_category": draft.service_category,
        "service_category_value": draft.service_category_value,
        "service_hours": draft.service_hours,
        "includes_main_work": bool(draft.includes_main_work),
        "completion_status": draft.completion_status,
        "source_completion_status": draft.source_completion_status,
        "selected_customer_name": draft.selected_customer_name,
        "completion_status_options": list(draft.completion_status_options),
    }


def deserialize_remote_workorder_draft(payload):
    if not isinstance(payload, dict):
        raise ValueError("远程工单草稿格式无效。")
    content_items = payload.get("content_items")
    if not isinstance(content_items, list) or not all(
        isinstance(item, str) for item in content_items
    ):
        raise ValueError("远程工单草稿缺少有效服务内容。")
    fields = {
        key: str(payload.get(key, "") or "").strip()
        for key in (
            "report_date_iso",
            "customer_name",
            "service_category",
            "service_category_value",
        )
    }
    if not all(fields.values()):
        raise ValueError("远程工单草稿缺少必要字段。")
    status_options = payload.get("completion_status_options", [])
    if not isinstance(status_options, list) or not all(isinstance(item, str) and item.strip() for item in status_options):
        raise ValueError("远程工单完成情况选项格式无效。")
    return RemoteWorkorderDraft(
        **fields,
        content_items=content_items,
        service_hours=str(payload.get("service_hours", "") or "").strip(),
        includes_main_work=bool(payload.get("includes_main_work")),
        completion_status=str(payload.get("completion_status", "") or "").strip(),
        source_completion_status=str(payload.get("source_completion_status", "") or "").strip(),
        selected_customer_name=str(payload.get("selected_customer_name", "") or "").strip(),
        completion_status_options=tuple(dict.fromkeys(item.strip() for item in status_options)),
    )


def load_remote_workorder_drafts_file(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取已授权远程工单草稿：{path}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("已授权远程工单草稿格式无效。")
    return [deserialize_remote_workorder_draft(item) for item in payload]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="根据日报内容创建纷享销客远程服务工单；每张工单都必须人工输入 Y 授权。"
    )
    parser.add_argument("--date", dest="report_date", help="指定日报日期，支持 YYYY-MM-DD 或 YYYY.MM.DD。")
    parser.add_argument("--path", dest="report_path", help="覆盖默认日报路径。")
    parser.add_argument("--customer", help="只处理指定客户，适合待补交重试。")
    parser.add_argument("--preview-only", action="store_true", help="只输出远程工单草稿，不启动浏览器。")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="填写并校验远程工单表单后自动取消，绝不点击提交。",
    )
    parser.add_argument(
        "--verify-workorder",
        help="只读复核指定 JSFW 编号的远程工单身份、内容和工时，不修改 CRM 数据。",
    )
    parser.add_argument("--expected-service-hours", default="", help="只读复核远程支持工单时的预期服务工时。")
    parser.add_argument("--remote-role", choices=("main", "optional"), help="只读复核时区分同客户的主服务与附加支持。")
    parser.add_argument(
        "--expected-completion-status",
        default=None,
        help="只读复核远程工单时的预期完成情况（必须是已核验的 CRM 选项）。",
    )
    parser.add_argument(
        "--record-verified",
        action="store_true",
        help="复核通过后写入本机确认历史，仅清除身份与核验前快照一致的待办；不修改 CRM。",
    )
    parser.add_argument("--state-path", default=str(fx.DEFAULT_STORAGE_STATE_PATH), help="覆盖登录状态缓存文件。")
    parser.add_argument("--profile-dir", default=None, help="使用指定持久化浏览器 Profile。")
    parser.add_argument("--show-browser", action="store_true", help="显示真实浏览器窗口。")
    parser.add_argument("--allow-ocr-login", action="store_true", help="登录验证码允许使用 OCR 兜底。")
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出 JSON 结果。")
    args = parser.parse_args(argv)
    if args.expected_service_hours and not args.verify_workorder:
        parser.error("--expected-service-hours 仅用于 --verify-workorder。")
    if args.remote_role and not args.verify_workorder:
        parser.error("--remote-role 仅用于 --verify-workorder。")
    if args.record_verified and not args.verify_workorder:
        parser.error("--record-verified 仅用于 --verify-workorder。")
    if args.expected_completion_status is not None and not args.verify_workorder:
        parser.error("--expected-completion-status 仅用于 --verify-workorder。")
    return args


def load_service_category_config(config_path=None, *, require_crm_values=False):
    local_config = config_path or os.environ.get("FXIAOKE_SERVICE_CATEGORY_CONFIG_PATH", "").strip()
    if require_crm_values and not local_config:
        raise RuntimeError("请设置 FXIAOKE_SERVICE_CATEGORY_CONFIG_PATH 为当前租户的远程工单类别配置。")
    path = Path(local_config or DEFAULT_SERVICE_CATEGORY_CONFIG_PATH).expanduser()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取远程工单类别配置：{path}。{exc}") from exc

    if not isinstance(config, dict):
        raise RuntimeError("远程工单类别配置必须是 JSON 对象。")
    rules = config.get("rules")
    fallback = config.get("fallback")
    if not isinstance(rules, list) or not rules:
        raise RuntimeError(f"远程工单类别配置缺少有效 rules：{path}")
    if not isinstance(fallback, dict):
        raise RuntimeError(f"远程工单类别配置缺少 fallback：{path}")
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise RuntimeError(f"远程工单类别配置第 {index} 条规则格式无效：{path}")
        if not all(str(rule.get(key, "")).strip() for key in ("label", "value")):
            raise RuntimeError(f"远程工单类别配置第 {index} 条缺少 label/value：{path}")
        keywords = rule.get("keywords")
        if not isinstance(keywords, list) or not any(str(keyword).strip() for keyword in keywords):
            raise RuntimeError(f"远程工单类别配置第 {index} 条缺少关键词：{path}")
    if not all(str(fallback.get(key, "")).strip() for key in ("label", "value")):
        raise RuntimeError(f"远程工单类别配置 fallback 缺少 label/value：{path}")
    if require_crm_values:
        for item in [*rules, fallback]:
            value = str(item["value"]).strip()
            if value.startswith("preview_") or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise RuntimeError("远程工单类别配置必须使用当前租户的有效选项标识。")
    return config


def classify_service_category(text, config_path=None):
    config = load_service_category_config(config_path=config_path)
    normalized = (text or "").lower()
    for rule in config["rules"]:
        for keyword in rule["keywords"]:
            for match in re.finditer(re.escape(str(keyword).lower()), normalized):
                # A negated symptom is not the service's positive classification.
                negated_symptom = rule["label"] in {"疑难问题排查", "硬件故障排查处理"} and re.search(
                    r"(?:无|没有|未发现|未见|并非|不是|排除|排除了|无需|不需要)\s*$",
                    normalized[max(0, match.start() - 6):match.start()],
                )
                if not negated_symptom:
                    return str(rule["label"]).strip(), str(rule["value"]).strip()
    fallback = config["fallback"]
    return str(fallback["label"]).strip(), str(fallback["value"]).strip()


def infer_remote_completion_status(content, source_statuses=()):
    """Prefill one customer's result without turning uncertainty into success."""
    clauses = [item.strip() for item in re.split(r"[；;，,。\n]", str(content or "")) if item.strip()]
    uncertain = ("可能", "疑似", "不确定", "未确认", "未验证", "尚未", "预计", "是否", "不一定", "待")
    resolved = any(
        re.search(r"已(?:解决|修复|恢复)|恢复正常|(?:问题|故障)(?:已)?解决", item)
        and not any(word in item for word in uncertain)
        for item in clauses
    )
    if resolved:
        # Only unqualified past-state clauses refer to the now-resolved item;
        # a named second issue is retained and can still prevent completion.
        clauses = [item for item in clauses if not re.fullmatch(r"(?:此前|之前|原先|原来|昨天)(?:未解决|未恢复)", item)]
    text = "；".join([*clauses, *source_statuses])
    if any(word in text for word in ("未回复", "未答复", "未回应", "等待客户回复", "待客户回复")):
        return "未回复"
    if any(word in text for word in ("未解决", "未恢复", "未修复", "未修好", "未能解决", "未能恢复", "没有解决", "没有恢复", "没有修好", "没有修复", "无法解决", "无法恢复", "修复失败", "修复未成功", "仍有故障", "问题仍存在")) or re.search(r"仍[^；;，,。]{0,6}(?:异常|报错|失效|失败|存在)", text):
        return "未解决"
    if any(word in text for word in ("进行中", "未完成", "待处理", "待验证", "待确认", "待跟进", "待客户确认", "未确认", "未验证", "尚未", "等待", "计划", "明日", "继续排查")):
        return "待跟进"
    if resolved or any(str(status).strip() in {"已完成", "已解决", "处理完成"} for status in source_statuses):
        return "已解决"
    return "待跟进"


def get_other_category_custom_text(config_path=None):
    fallback = load_service_category_config(config_path=config_path)["fallback"]
    return str(fallback.get("custom_text", "远程技术支持") or "远程技术支持").strip()


def build_remote_workorder_drafts(report_section, is_onsite, *, includes_main_work=None):
    report_date_iso = normalize_report_date_to_iso(report_section)
    report_fields = parse_report_fields(report_section)
    service_entries = parse_report_service_entries(report_section)
    default_customer = report_fields.get("服务客户", "").strip()
    if service_entries and service_entries[0].get("服务客户", "").strip():
        default_customer = service_entries[0]["服务客户"].strip()

    grouped: dict[tuple[str, bool], dict[str, Any]] = {}

    def add_content(customer_name, content, includes_main_work=False, source_status=""):
        customer = (customer_name or "").strip()
        content = (content or "").strip().strip("；;")
        if not customer or not content:
            return
        key = (strict_customer_identity(resolve_customer_primary_name(customer)), includes_main_work)
        bucket = grouped.setdefault(
            key,
            {
                "customer_name": customer,
                "content_items": [],
                "includes_main_work": includes_main_work,
                "source_statuses": [],
            },
        )
        if content not in bucket["content_items"]:
            bucket["content_items"].append(content)
        if source_status:
            bucket["source_statuses"].append(source_status)

    if not is_onsite and includes_main_work is not False:
        entries = service_entries or [report_fields]
        for entry in entries:
            add_content(
                entry.get("服务客户", "").strip() or default_customer,
                entry.get("处理情况", ""),
                includes_main_work=True,
                source_status=entry.get("处理结果", ""),
            )

    if includes_main_work is not True:
        for remote_item in split_remote_support_items(report_fields.get("远程服务支持", "")):
            if should_skip_remote_support_item(remote_item):
                continue
            add_content(
                extract_remote_support_customer_name(remote_item, default_customer),
                remote_item,
            )

    drafts = []
    for bucket in grouped.values():
        content = "；".join(bucket["content_items"])
        category, category_value = classify_service_category(content)
        drafts.append(
            RemoteWorkorderDraft(
                report_date_iso=report_date_iso,
                customer_name=bucket["customer_name"],
                content_items=bucket["content_items"],
                service_category=category,
                service_category_value=category_value,
                service_hours=(
                    REMOTE_WORKORDER_MAIN_HOURS
                    if bucket["includes_main_work"]
                    else ""
                ),
                includes_main_work=bucket["includes_main_work"],
                source_completion_status="、".join(dict.fromkeys(bucket["source_statuses"])),
                completion_status=infer_remote_completion_status(content, bucket["source_statuses"]),
            )
        )
    return drafts


def render_draft_preview(draft):
    return (
        "\n[远程服务工单待提交]\n"
        f"客户：{draft.customer_name}\n"
        f"服务日期：{draft.report_date_iso}\n"
        f"服务内容类别：{draft.service_category}\n"
        f"完成情况：{draft.completion_status or '待确认（未自动设为已解决）'}\n"
        f"服务工时：{draft.service_hours or '确认后输入'}\n"
        f"服务内容：{draft.content}\n"
        "操作：Y 提交当前，回车/N 跳过当前，Q 结束选择。\n"
        "确认提交？[Y/N/Q]："
    )


def normalize_positive_service_hours(value):
    raw_value = str(value or "").strip()
    try:
        if len(raw_value) > 256:
            raise ValueError
        hours = Decimal(raw_value)
        if not hours.is_finite() or hours <= 0:
            raise ValueError
        # Bound the rendered value before allocation, without context rounding.
        digits = len(hours.as_tuple().digits)
        exponent = hours.as_tuple().exponent
        if not isinstance(exponent, int) or max(digits + exponent, 1) + max(-exponent, 0) > 128:
            raise ValueError
        rendered = format(hours, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except (DecimalException, ValueError):
        raise ValueError("服务工时必须是大于 0 且长度有效的数字。") from None


def normalize_editable_remote_support_hours(value):
    raw_value = str(value or "").strip()
    try:
        hours = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        raise ValueError("远程工单工时必须是 0.5 到 8 的数字。") from None
    if not hours.is_finite():
        raise ValueError("远程工单工时必须是 0.5 到 8 的数字。")
    if not REMOTE_SUPPORT_EDIT_MIN_HOURS <= hours <= REMOTE_SUPPORT_EDIT_MAX_HOURS:
        raise ValueError("远程工单工时必须在 0.5 到 8 小时之间。")
    if hours % REMOTE_SUPPORT_EDIT_STEP_HOURS:
        raise ValueError("远程工单工时必须是 0.5 小时的倍数。")
    return format(hours.normalize(), "f")


def prompt_remote_support_hours(draft, input_func=input):
    if draft.includes_main_work:
        return replace(draft, service_hours=REMOTE_WORKORDER_MAIN_HOURS)
    if draft.service_hours:
        return replace(
            draft,
            service_hours=normalize_positive_service_hours(draft.service_hours),
        )

    error_hint = ""
    while True:
        prompt = (
            f"\n[远程支持工时]\n客户：{draft.customer_name}\n"
            f"服务内容：{draft.content}\n"
            f"{error_hint}"
            "服务工时（小时）："
        )
        try:
            service_hours = normalize_positive_service_hours(input_func(prompt))
        except ValueError as exc:
            error_hint = f"输入无效：{exc}\n"
            continue
        return replace(draft, service_hours=service_hours)


def confirm_remote_workorder_drafts(drafts, input_func=input):
    authorized = []
    for draft in drafts:
        answer = input_func(render_draft_preview(draft)).strip().lower()
        if answer == "y":
            authorized.append(
                prompt_remote_support_hours(draft, input_func=input_func)
            )
        elif answer == "q":
            break
    return authorized


def get_remote_workorder_review_options(draft=None):
    config = load_service_category_config()
    categories = []
    for item in [*config["rules"], config["fallback"]]:
        option = {"label": str(item["label"]), "value": str(item["value"])}
        if option not in categories:
            categories.append(option)
    statuses = list(draft.completion_status_options) if draft is not None and draft.completion_status_options else config.get("completion_status_options", [REMOTE_WORKORDER_COMPLETION_STATUS])
    if not isinstance(statuses, list) or not statuses or not all(isinstance(item, str) and not is_completion_status_placeholder(item) for item in statuses):
        raise RuntimeError("远程工单完成情况配置无效。")
    return {"categories": categories, "completion_statuses": list(dict.fromkeys(statuses))}


def is_completion_status_placeholder(value):
    label = str(value or "").strip()
    return not label or label == "选择" or label.startswith("请选择")


def apply_remote_workorder_review_options(draft, *, service_category=None, completion_status=None):
    options = get_remote_workorder_review_options(draft)
    category = draft.service_category if service_category is None else str(service_category).strip()
    match = next((item for item in options["categories"] if item["label"] == category), None)
    if match is None:
        raise ValueError("服务内容类别不是已确认的 CRM 选项。")
    status = draft.completion_status if completion_status is None else str(completion_status).strip()
    if status and (is_completion_status_placeholder(status) or status not in options["completion_statuses"]):
        raise ValueError("完成情况不是已确认的 CRM 选项，请先核实 CRM 可选值。")
    return replace(draft, service_category=match["label"], service_category_value=match["value"], completion_status=status)


def review_remote_workorder_options(draft, input_func, options):
    """Let the operator override category and completion status for one draft.

    Only reached on explicit request (``M``) or when the draft has no resolved
    completion status, so the common case no longer asks two questions whose
    answers are already known.
    """
    labels = [item["label"] for item in options["categories"]]
    category_answer = input_func(
        "类别：" + "；".join(f"{i} {label}" for i, label in enumerate(labels, 1))
        + f"\n服务内容类别（回车保留“{draft.service_category}”，或输入序号）："
    ).strip()
    if category_answer:
        if category_answer.isdigit() and 1 <= int(category_answer) <= len(labels):
            draft = apply_remote_workorder_review_options(draft, service_category=labels[int(category_answer) - 1])
        elif category_answer in labels:
            draft = apply_remote_workorder_review_options(draft, service_category=category_answer)
        else:
            raise ValueError("服务内容类别选择无效，未授权提交。")
    statuses = get_remote_workorder_review_options(draft)["completion_statuses"]
    status_answer = input_func(
        f"源报告处理结果：{draft.source_completion_status or '未单独填写'}\n"
        f"完成情况：{draft.completion_status or '待确认'}；"
        + "；".join(f"{i} {status}" for i, status in enumerate(statuses, 1))
        + "\n完成情况（回车保留，输入序号明确更改；未确认则跳过）："
    ).strip()
    if status_answer:
        if status_answer.isdigit() and 1 <= int(status_answer) <= len(statuses):
            status_answer = statuses[int(status_answer) - 1]
        draft = apply_remote_workorder_review_options(draft, completion_status=status_answer)
    return draft


def confirm_remote_workorder_batch(drafts, input_func=input, *, confirm_batch=True, require_main=False):
    """Confirm hours per draft, then authorize the batch with one exact Y."""
    selected: list[RemoteWorkorderDraft] = []
    options = get_remote_workorder_review_options()
    for draft in drafts:
        required = require_main and draft.includes_main_work
        selection_hint = "N 跳过当前项，Q 取消全部远程工单"
        label = "远程工单审核"
        if require_main:
            label = "非外勤主技术服务（必交）" if required else "附加远程支持（可选）"
            selection_hint = "N 暂缓并记入待办" if required else "N 跳过当前项，Q 跳过所有附加远程支持"
        default_hours = draft.service_hours or (REMOTE_WORKORDER_MAIN_HOURS if draft.includes_main_work else "")
        hours_hint = f"回车保留 {default_hours}" if default_hours else "请输入本次工时"
        while True:
            answer = input_func(
                f"\n[{label}]\n客户：{draft.customer_name}\n"
                f"CRM 单位：{draft.selected_customer_name or '提交前预检确认'}\n"
                f"服务内容：{draft.content}\n"
                f"类别/完成情况：{draft.service_category}／{draft.completion_status or '待确认'}\n"
                f"工时（小时，{hours_hint}；M 修改类别与完成情况；{selection_hint}）："
            ).strip()
            if answer.lower() == "q":
                if require_main:
                    if required:
                        continue
                    return [item for item in selected if item.includes_main_work]
                return []
            if answer.lower() == "n":
                break
            if answer.lower() == "m":
                draft = review_remote_workorder_options(draft, input_func, options)
                continue
            try:
                draft = replace(draft, service_hours=normalize_positive_service_hours(answer or default_hours))
            except ValueError:
                continue
            if not draft.completion_status:
                # An unresolved status must be shown, never silently dropped.
                draft = review_remote_workorder_options(draft, input_func, options)
            if draft.completion_status:
                selected.append(draft)
            break
    if not selected:
        return []
    amounts = [Decimal(item.service_hours) for item in selected]
    with localcontext() as context:
        context.prec = 260 + len(str(len(amounts)))
        total = sum(amounts, Decimal("0"))
    summary = "\n".join(
        f"{item.customer_name}：{item.service_hours} 小时；{item.service_category}；{item.completion_status}"
        + (f"；CRM：{item.selected_customer_name}" if item.selected_customer_name else "")
        for item in selected
    )
    if not confirm_batch:
        print(f"\n[远程服务总览]\n{summary}\n合计：{normalize_positive_service_hours(total)} 小时")
        return selected
    answer = input_func(
        f"\n[远程工单整批确认]\n{summary}\n合计：{normalize_positive_service_hours(total)} 小时\n"
        "确认提交以上工单？仅输入 Y 授权，其他输入取消整批："
    ).strip().lower()
    return selected if answer == "y" else []


def find_recent_workorder_defaults(report_path, customer_name):
    """Return the latest successful remote-workorder defaults for one customer."""
    if report_path is None:
        return {}
    try:
        lines = resolve_fxiaoke_submission_history_path(Path(report_path)).read_text(
            encoding="utf-8"
        ).splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return {}

    def same_customer(left, right):
        return (
            strict_customer_identity(left) == strict_customer_identity(right)
            or customer_matches_workorder(left, right)
            or customer_matches_workorder(right, left)
        )

    latest = {}
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(item, dict)
            or item.get("step") != "remote_workorder"
            or item.get("status") not in {"submitted", "already_exists"}
        ):
            continue
        service_customer = str(item.get("service_customer") or "").strip()
        crm_customer = str(item.get("crm_customer") or "").strip()
        if not service_customer or not same_customer(service_customer, customer_name):
            continue
        latest = {
            "crm_customer": crm_customer,
            "service_hours": str(item.get("service_hours") or "").strip(),
            "service_category": str(item.get("service_category") or "").strip(),
            "completion_status": str(item.get("completion_status") or "").strip(),
        }
    return latest


def find_recent_workorder_hours(report_path, customer_name):
    hours = find_recent_workorder_defaults(report_path, customer_name).get("service_hours", "")
    try:
        return normalize_positive_service_hours(hours)
    except ValueError:
        return ""


def fill_compact_workorder_hours(drafts, report_path, input_func=read_brief_input):
    """Fill missing hours from history, asking only when no default exists."""
    updated = []
    for draft in drafts:
        if draft.service_hours:
            updated.append(draft)
            continue
        remembered_hours = find_recent_workorder_hours(report_path, draft.customer_name)
        if remembered_hours:
            updated.append(replace(draft, service_hours=remembered_hours))
            continue
        updated.append(prompt_remote_support_hours(draft, input_func=input_func))
    return updated


def review_compact_workorder_options(draft, input_func, options):
    """Edit hours, category and completion status for the compact table."""
    current_hours = draft.service_hours or "未填写"
    while True:
        answer = input_func(f"工时（小时，回车保留 {current_hours}）：").strip()
        if not answer:
            break
        try:
            draft = replace(draft, service_hours=normalize_positive_service_hours(answer))
            break
        except ValueError:
            continue
    return review_remote_workorder_options(draft, input_func, options)


def confirm_compact_remote_workorders(
    drafts,
    input_func=read_brief_input,
    *,
    submit_service_record=False,
    service_summary="",
    blocked_summary="",
):
    """One compact table; Enter authorizes the whole batch.

    Returns ``(drafts, submit_service_record)``. ``drafts is None`` means the
    operator cancelled everything, so the caller must not write to CRM.
    """
    while True:
        lines = ["\n[提交确认]"]
        if service_summary:
            lines.append(service_summary)
        if blocked_summary:
            lines.append(blocked_summary)
        if drafts:
            lines.append("远程工单：")
            for index, draft in enumerate(drafts, start=1):
                lines.append(
                    f"{index}. {draft.customer_name} → "
                    f"{draft.selected_customer_name or '待确认'} | "
                    f"{draft.service_hours or '待填写'} 小时 | "
                    f"{draft.service_category} | "
                    f"{draft.completion_status or '待确认'}"
                )
        prompt = "\n".join(lines) + "\n回车提交 / m 修改工时或类别 / n 只发日报 / q 取消全部："
        answer = input_func(prompt).strip().lower()
        if answer in {"", "y", "yes"}:
            return drafts, submit_service_record
        if answer in {"n", "no"}:
            return [], False
        if answer in {"q", "quit"}:
            return None, False
        if answer in {"m", "modify"}:
            options = get_remote_workorder_review_options()
            drafts = [
                review_compact_workorder_options(draft, input_func, options)
                for draft in drafts
            ]


def strict_customer_identity(value):
    # Search aliases may omit departments; an explicitly selected CRM unit may not.
    return re.sub(r"\s+", "", str(value or "")).casefold()


def validate_preselected_customer(draft, selected_customer):
    if draft.selected_customer_name and strict_customer_identity(draft.selected_customer_name) != strict_customer_identity(selected_customer):
        raise RuntimeError(
            f"客户单位与预检确认不一致：预检“{draft.selected_customer_name}”，当前“{selected_customer}”；未提交，请重新预检和确认。"
        )


class RemoteCustomerPreflightError(RuntimeError):
    """A known customer-scope problem; isolate this workorder, not the batch."""
    failure_scope = "customer"
    stage = "customer_selection"

    def __init__(self, customer_name, message, *, reason):
        super().__init__(message)
        self.customer_name = customer_name
        self.reason = reason


class RemoteCustomerSelectionError(RemoteCustomerPreflightError):
    """A known, local selection problem; never a generic browser/session error."""


class RemoteCustomerLookupError(RemoteCustomerPreflightError):
    """The lookup UI loaded but the name could not be resolved to a CRM unit.

    This is not proof that the customer is absent. The item stays blocked until
    the operator explicitly confirms whether to continue the remaining batch.
    """


class CustomerSearchRequested(RuntimeError):
    """An explicit read-only search command, never a selected CRM customer."""

    def __init__(self, keyword):
        keyword = str(keyword).strip()
        if not 2 <= len(keyword) <= 80 or any(ord(char) < 32 for char in keyword):
            raise ValueError("重查关键词须为2至80个字符且不能包含换行。")
        super().__init__(keyword)
        self.keyword = keyword


def preflight_remote_workorder_drafts_on_authenticated_page(page, drafts, input_func=read_brief_input, *, blocked=None, compact=False):
    fx.trace_phase(page, "remote_workorder/预检选择客户")
    selected = []
    selected_units = set()
    for draft in drafts:
        # Validate required local fields before touching a CRM form.
        apply_remote_workorder_review_options(draft)
        date.fromisoformat(draft.report_date_iso)
        if draft.service_hours:
            normalize_positive_service_hours(draft.service_hours)
        if not draft.customer_name.strip() or not any(item.strip() for item in draft.content_items):
            raise ValueError("远程工单缺少客户或服务内容。")
        try:
            open_new_remote_workorder_form(page)
            customer = select_customer_for_draft(
                page, draft, input_func=input_func, remember_alias=False, compact=compact,
            )
            validate_preselected_customer(draft, customer)
            unit_key = (draft.report_date_iso, strict_customer_identity(customer))
            if unit_key in selected_units:
                raise RemoteCustomerSelectionError(
                    draft.customer_name,
                    "同日已有另一项对应同一CRM客户，未合并内容或创建第二张工单，请人工核对。",
                    reason="duplicate_crm_customer",
                )
            fill_first_available_contact(page)
            status_options = read_remote_workorder_completion_status_options(page)
            selected.append(replace(
                draft,
                selected_customer_name=customer,
                completion_status_options=status_options,
                completion_status=draft.completion_status if draft.completion_status in status_options else "",
            ))
            selected_units.add(unit_key)
        except RemoteCustomerPreflightError as exc:
            if blocked is None or exc.stage != "customer_selection":
                raise
            blocked.append({
                "ok": False, "step": "remote_workorder", "status": "preflight_failed",
                "service_customer": draft.customer_name, "retry_action": "confirm_customer",
                "includes_main_work": draft.includes_main_work,
                "failure_scope": "customer", "message": str(exc), "artifacts": [],
            })
        finally:
            close_remote_workorder_form(page)
    return selected


def build_skipped_remote_workorder_results(drafts, authorized):
    selected = {(draft.report_date_iso, draft.customer_name, draft.includes_main_work) for draft in authorized}
    return [
        build_skipped_result(draft) for draft in drafts
        if (draft.report_date_iso, draft.customer_name, draft.includes_main_work) not in selected
    ]


def choose_customer_candidate_name(
    query,
    candidate_names,
    remembered_primary="",
    input_func=input,
    on_explicit_choice=None,
    allow_search=False,
    force_confirmation=False,
    compact=False,
):
    candidates = filter_customer_candidate_names(candidate_names)
    if not candidates:
        raise RemoteCustomerSelectionError(query, f"未找到客户“{query}”的可选 CRM 单位。", reason="not_found")

    identity = strict_customer_identity(remembered_primary or query)
    exact = [name for name in candidates if strict_customer_identity(name) == identity]
    if len(exact) == 1 and not force_confirmation:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError("CRM候选存在重复完整名称，无法唯一确认客户身份，已停止选择。")

    # Keep every candidate available; a truncated list can hide the intended unit.
    displayed = rank_customer_candidates(query, candidates)
    options = "\n".join(
        f"{index}：{name}" for index, (name, _score) in enumerate(displayed, start=1)
    )
    warning = "名称相似不代表同一单位，请核对行政区划、学段、校区及部门。\n"
    # Only an exact identity may be confirmed with Enter. A lossy name key
    # (city/stage/department stripped) is not enough to authorize a write.
    strong_single = (
        len(displayed) == 1
        and strict_customer_identity(displayed[0][0]) == identity
    )
    if compact and strong_single:
        prompt = (
            f"\n客户确认：{query} → {displayed[0][0]}\n"
            "回车确认 / 输入关键词重查 / N 取消："
            if allow_search else
            f"\n客户确认：{query} → {displayed[0][0]}\n"
            "回车确认 / N 取消："
        )
    elif len(displayed) == 1:
        prompt = (
            f"\n客户“{query}”只找到 1 个相近的 CRM 单位：\n{options}\n{warning}"
            "输入 1 确认该单位（回车取消当前工单）："
        )
    else:
        prompt = (
            f"\n客户“{query}”匹配到 {len(displayed)} 个 CRM 单位（按名称相近度排序）：\n{options}\n{warning}"
            "请输入正确客户序号（回车取消当前工单）："
        )
    if allow_search and not (compact and strong_single):
        prompt += "也可直接输入新关键词重新检索："
    for _ in range(3):
        answer = input_func(prompt).strip()
        if not answer:
            if compact and strong_single:
                chosen = displayed[0][0]
                if on_explicit_choice is not None:
                    on_explicit_choice(chosen)
                return chosen
            raise RemoteCustomerSelectionError(query, f"已取消选择客户“{query}”。", reason="cancelled")
        if compact and answer.lower() in {"n", "no", "取消"}:
            raise RemoteCustomerSelectionError(query, f"已取消选择客户“{query}”。", reason="cancelled")
        if answer.isdigit():
            if 1 <= int(answer) <= len(displayed):
                chosen = displayed[int(answer) - 1][0]
                if on_explicit_choice is not None:
                    on_explicit_choice(chosen)
                return chosen
            continue
        if allow_search:
            keyword = answer[1:] if answer.startswith("/") else answer
            try:
                request = CustomerSearchRequested(keyword)
            except ValueError:
                continue
            raise request
    raise RemoteCustomerSelectionError(query, f"客户“{query}”连续 3 次输入了无效序号。", reason="invalid_choice")


def filter_customer_candidate_names(candidate_names):
    candidates = []
    for raw_name in candidate_names:
        name = (raw_name or "").strip()
        if not name or JSFW_ID_PATTERN.search(name) or name in candidates:
            continue
        candidates.append(name)
    return candidates


def sanitize_remembered_customer_name(customer_name):
    name = (customer_name or "").strip()
    return "" if JSFW_ID_PATTERN.search(name) else name


def find_duplicate_remote_workorder(rows, draft):
    invalid_status_markers = ("作废", "取消", "已失效", "已删除")
    for row in rows:
        workorder_id = str(row.get("workorder_id", "") or "").strip()
        customer_name = str(row.get("customer_name", "") or "").strip()
        service_date = str(row.get("service_date", "") or "").strip()
        status_text = str(row.get("status_text", "") or "").strip()
        if not JSFW_ID_PATTERN.fullmatch(workorder_id):
            continue
        if any(marker in status_text for marker in invalid_status_markers):
            continue
        if not customer_name:
            raise RuntimeError("远程工单列表客户字段缺失，无法完成查重，请先核对。")
        if draft.selected_customer_name and strict_customer_identity(customer_name) != strict_customer_identity(draft.selected_customer_name):
            continue
        if customer_matches_workorder(draft.customer_name, customer_name) or customer_matches_workorder(
            customer_name,
            draft.customer_name,
        ):
            if not re.match(r"^\d{4}-\d{2}-\d{2}", service_date):
                raise RuntimeError("匹配客户的远程工单缺少有效服务日期，无法确认是否重复。")
            if service_date[:10] == draft.report_date_iso:
                return row
    return None


def remote_field(key, *, required=True):
    return tenant_value("remote_workorder", f"{key}_field", required=required, token=True)


def require_remote_form_fields():
    fields = {key: remote_field(key) for key in (
        "region", "product", "category", "warranty", "service_date", "hours", "summary", "completion_status",
    )}
    if len(set(fields.values())) != len(fields) or set(fields.values()) & {"account_id", "description", "owner"}:
        raise RuntimeError("远程工单配置含重复或冲突的字段标识。")
    return fields


def remote_option(key):
    return (
        tenant_value("remote_workorder", f"{key}_label", required=True),
        tenant_value("remote_workorder", f"{key}_value", required=True, token=True),
    )


def build_remote_workorder_form_values(draft):
    owner = require_service_owner()
    load_service_category_config(require_crm_values=True)
    fields = require_remote_form_fields()
    try:
        service_hours = normalize_positive_service_hours(draft.service_hours)
    except ValueError as exc:
        raise RuntimeError(f"远程工单服务工时尚未正确填写：{exc}") from exc
    reviewed = apply_remote_workorder_review_options(draft)
    if reviewed.service_category_value != draft.service_category_value:
        raise RuntimeError("远程工单类别选项已变化，请重新预览并授权。")
    draft = reviewed
    if not draft.completion_status:
        raise RuntimeError("远程工单完成情况尚未确认；源报告进行中时禁止自动设为已解决。")
    return {
        fields["region"]: remote_option("region")[1],
        fields["product"]: remote_option("product")[1],
        fields["category"]: draft.service_category_value,
        "description": draft.content,
        fields["service_date"]: draft.report_date_iso,
        fields["hours"]: service_hours,
        fields["summary"]: draft.content,
        fields["completion_status"]: draft.completion_status,
        "owner": owner,
    }


def find_remembered_remote_workorder_id(report_path, draft):
    """History is a positive locator only; CRM must still verify the record."""
    if report_path is None:
        return ""
    history_path = resolve_fxiaoke_submission_history_path(report_path)
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
        latest = {}
        damaged = False
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                damaged = True
                continue
            if not isinstance(item, dict) or item.get("step") != "remote_workorder":
                continue
            workorder_id = str(item.get("workorder_id", "") or "").strip()
            if JSFW_ID_PATTERN.fullmatch(workorder_id):
                latest[workorder_id] = item
    except FileNotFoundError as exc:
        if not history_path.is_symlink():
            return ""  # A new installation can have no success history yet.
        raise RemoteWorkorderConfirmationUncertain(
            "成功历史链接已失效，无法读取；请先核对历史文件，禁止直接重建。"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise RemoteWorkorderConfirmationUncertain(
            "成功历史无法读取，不能确认已有编号；请先核对历史文件，禁止直接重建。"
        ) from exc
    ids = [
        workorder_id for workorder_id, item in latest.items()
        if item.get("status") in ("submitted", "already_exists")
        and ("ok" not in item or item["ok"] is True or type(item["ok"]) is int and item["ok"] == 1)
        and item.get("report_date") == draft.report_date_iso
        and normalize_customer_match_text(str(item.get("crm_customer") or item.get("service_customer") or ""))
        == normalize_customer_match_text(draft.customer_name)
    ]
    if ids and (damaged or len(ids) > 1):
        raise RemoteWorkorderConfirmationUncertain(
            "成功历史存在损坏行或同日多个编号，需先核对：" + "、".join(ids) + "；禁止直接重建。",
            workorder_id=ids[0],
        )
    return ids[0] if len(ids) == 1 else ""


def extract_existing_remote_workorder_values(detail_text):
    # Read the basic/detail section, excluding the repeated header summary.
    lines = [line.strip() for line in str(detail_text).splitlines() if line.strip()]
    lines = lines[lines.index("基本信息") + 1:]
    values = {}
    for label, end in (("服务需求详述", "问题或现象图片"), ("完成小结", "系统信息")):
        if lines.count(label) != 1 or lines.count(end) != 1:
            raise ValueError(f"详情字段 {label} 不完整或不唯一。")
        start, stop = lines.index(label) + 1, lines.index(end)
        if label == "完成小结":
            status_markers = [index for index in range(start, stop) if lines[index] == "服务完成情况"]
            if len(status_markers) > 1:
                raise ValueError("详情字段 服务完成情况 不唯一。")
            if status_markers:
                stop = status_markers[0]
        if start >= stop:
            raise ValueError(f"详情字段 {label} 尚未加载。")
        content = "\n".join(
            line for line in lines[start:stop]
            if not all("\ue000" <= char <= "\uf8ff" for char in line)
        )
        if not content or content == "--":
            raise ValueError(f"详情字段 {label} 尚未加载。")
        values[label] = content
    text = "\n".join(lines)
    for label in ("工单编码", "客户单位", "服务日期", "服务工时"):
        if lines.count(label) != 1:
            raise ValueError(f"详情字段 {label} 不唯一或缺失。")
        values[label] = fx.extract_detail_field_value(text, label)
    for label in ("服务完成情况", "完成情况"):
        if label in lines:
            index = lines.index(label)
            if index + 1 < len(lines) and lines[index + 1] not in {"服务日期", "服务工时", "完成小结"}:
                values["服务完成情况"] = lines[index + 1]
                break
    values["服务工时"] = normalize_positive_service_hours(values["服务工时"])
    return values


def verify_existing_remote_workorder(page, row, draft):
    from integrations.fxiaoke.read_only import WebDetailObserver, remote_workorder_values

    workorder_id = row["workorder_id"]
    detail_page = None
    differences = []
    try:
        if not draft.service_hours:
            raise RuntimeError("缺少本次授权服务工时，无法确认草稿已完整同步；请先在预览中确认工时。")
        verify_remote_workorder_row([row], workorder_id, draft)
        url = row.get("detail_url") or get_workorder_list_detail_url(page, workorder_id)
        detail_page = page.context.new_page()
        with WebDetailObserver(detail_page, url, "CasesObj") as observed:
            fx.fast_goto(detail_page, url)
            deadline = time.monotonic() + REMOTE_WORKORDER_PAGE_TIMEOUT_SECONDS
            previous = None
            while time.monotonic() < deadline:
                try:
                    values = remote_workorder_values(observed.value)
                    if values is not None:
                        try:
                            values["服务工时"] = normalize_positive_service_hours(values["服务工时"])
                        except ValueError:
                            values = None
                        else:
                            break
                    values = extract_existing_remote_workorder_values(
                        fx.safe_inner_text(detail_page.locator("body"), timeout_ms=2_000)
                    )
                except ValueError:
                    previous = None
                else:
                    if values == previous:
                        break
                    previous = values
                detail_page.wait_for_timeout(300)
            else:
                raise RuntimeError("已有工单详情字段未完整加载或未稳定。")
        if (
            values["工单编码"] != workorder_id
            or strict_customer_identity(values["客户单位"]) != strict_customer_identity(row["customer_name"])
            or values["服务日期"] != draft.report_date_iso
        ):
            raise RuntimeError("已有工单详情的编号、客户或日期与列表不一致。")
        differences = [
            {"field": label, "expected": draft.content, "observed": values[label]}
            for label in ("服务需求详述", "完成小结")
            if values[label].replace("\r\n", "\n").strip() != draft.content.replace("\r\n", "\n").strip()
        ]
        if not remote_workorder_hours_match(values["服务工时"], draft.service_hours):
            differences.append({"field": "服务工时", "expected": draft.service_hours, "observed": values["服务工时"]})
        observed_status = values.get("服务完成情况", "")
        if observed_status or draft.selected_customer_name:
            if observed_status != draft.completion_status:
                differences.append({"field": "服务完成情况", "expected": draft.completion_status, "observed": observed_status or "未识别"})
        if differences:
            raise RuntimeError(
                "已有同日工单与本次草稿存在差异：\n" + "\n".join(
                    f"{item['field']}｜本次：{item['expected']}｜CRM：{item['observed']}"
                    for item in differences
                )
            )
    except fx.SubmissionEvidenceError:
        raise
    except Exception as exc:
        raise RemoteWorkorderConfirmationUncertain(
            f"工单 {workorder_id} 需要人工核对：{exc} 禁止重复创建或自动覆盖。",
            workorder_id=workorder_id,
            differences=differences,
        ) from exc
    finally:
        if detail_page is not None:
            handling_error = sys.exc_info()[0] is not None
            try:
                detail_page.close()
            except fx.PlaywrightError as exc:
                if not handling_error:
                    raise RemoteWorkorderConfirmationUncertain(
                        "已有工单详情核对后关闭页面失败，请先核对，禁止直接重建。",
                        workorder_id=workorder_id,
                    ) from exc


def extract_new_workorder_id(known_ids, current_ids):
    new_ids = {
        workorder_id
        for workorder_id in set(current_ids) - set(known_ids)
        if JSFW_ID_PATTERN.fullmatch(workorder_id)
    }
    if len(new_ids) > 1:
        raise RuntimeError("同时检测到多个新工单编号，身份不明确，请先只读核对。")
    return next(iter(new_ids)) if new_ids else ""


def extract_workorder_id_from_detail_history_request(url, post_data):
    if "/History/addObjDetailHistory" not in str(url or ""):
        return ""
    try:
        payload = json.loads(post_data or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or payload.get("objApiName") != "CasesObj":
        return ""
    workorder_id = str(payload.get("name", "")).strip()
    return workorder_id if JSFW_ID_PATTERN.fullmatch(workorder_id) else ""


def submitted_workorder_text_matches(body_text, workorder_id, customer_name, service_date):
    if not workorder_id or not customer_name or not service_date:
        return False
    normalized_body = normalize_customer_match_text(body_text)
    normalized_customer = normalize_customer_match_text(customer_name)
    return all(
        (
            bool(re.search(rf"(?<![A-Z0-9-]){re.escape(workorder_id)}(?![A-Z0-9-])", body_text)),
            bool(re.search(rf"(?<!\d){re.escape(service_date)}(?!\d)", body_text)),
            normalized_customer in normalized_body,
        )
    )


def build_remote_workorder_audit_fields(draft, crm_customer):
    return {
        "report_date": draft.report_date_iso,
        "includes_main_work": draft.includes_main_work,
        "crm_customer": str(crm_customer or "").strip(),
        "service_category": draft.service_category,
        "service_hours": draft.service_hours,
        "completion_status": draft.completion_status,
        "selected_customer_name": draft.selected_customer_name,
        "content_digest": hashlib.sha256(draft.content.encode("utf-8")).hexdigest(),
    }


def stamp_remote_workorder_retry_actions(results, *, submit_clicked=False):
    """Record how far each failed attempt got so a batch retry can trust it.

    Only the module CLI used to stamp ``retry_action``. Failures surfaced through
    the daily-report batch therefore entered the pending queue as "unknown
    position" and blocked ``--retry-pending`` behind manual queue surgery, even
    when the attempt provably stopped before any submit click.
    """
    for item in results:
        if item.get("ok") or item.get("retry_action"):
            continue
        captured = bool(str(item.get("workorder_id") or "").strip()) or item.get("status") in {
            "uncertain",
            "crm_id_captured",
        }
        if submit_clicked or captured:
            item["retry_action"] = "verify_crm"
            item.setdefault(
                "submission_phase",
                "crm_id_captured" if str(item.get("workorder_id") or "").strip() else "submit_clicked",
            )
        else:
            item["retry_action"] = "safe_retry"
            item.setdefault("submission_phase", "before_submit")
    return results


def verify_remote_workorder_row(rows, workorder_id, draft):
    if not JSFW_ID_PATTERN.fullmatch(str(workorder_id or "").strip()):
        raise RuntimeError(f"远程工单编号格式无效：{workorder_id}")
    matching_rows = [row for row in rows if str(row.get("workorder_id", "")).strip() == workorder_id]
    if len(matching_rows) > 1:
        raise RuntimeError(f"工单 {workorder_id} 的精确编号结果不唯一，已停止核验。")
    for row in matching_rows:
        row_customer = str(row.get("customer_name", "")).strip()
        validate_preselected_customer(draft, row_customer)
        if not (
            customer_matches_workorder(draft.customer_name, row_customer)
            or customer_matches_workorder(row_customer, draft.customer_name)
        ):
            raise RuntimeError(
                f"工单 {workorder_id} 客户“{row_customer or '未识别'}”与日报客户“{draft.customer_name}”不匹配。"
            )
        row_date = str(row.get("service_date", "")).strip()[:10]
        if row_date != draft.report_date_iso:
            raise RuntimeError(
                f"工单 {workorder_id} 服务日期“{row_date or '未识别'}”与日报日期“{draft.report_date_iso}”不匹配。"
            )
        if any(
            marker in str(row.get("status_text", ""))
            for marker in ("作废", "取消", "已失效", "已删除")
        ):
            raise RuntimeError(f"工单 {workorder_id} 已作废、取消或失效。")
        return row
    raise RuntimeError(
        f"《远程服务工单》列表中未找到 {workorder_id}，无法完成提交闭环复核。"
    )


def open_recent_workorder_detail(page, workorder_id):
    history_button = fx.wait_for_first_visible(
        page.locator("#fx-topnav-tool-history"),
        timeout_ms=8_000,
        description="顶部历史记录入口",
    )
    history_button.click(force=True)
    history_item = fx.wait_for_first_visible(
        page.locator("div.fx-hd-history li.item").filter(has_text=workorder_id),
        timeout_ms=8_000,
        description=f"历史工单 {workorder_id}",
    )
    detail_link = history_item.locator('a[href*="#objdetail/CasesObj/"]').first
    detail_url = fx.build_absolute_fxiaoke_url(
        fx.safe_get_attribute(detail_link, "href") or ""
    )
    if not detail_url:
        raise RuntimeError(f"历史记录中未找到工单 {workorder_id} 的详情链接。")
    fx.fast_goto(page, detail_url)

    return wait_for_remote_workorder_detail(page, workorder_id)


def get_workorder_list_detail_url(page, workorder_id, *, deadline=None):
    if not JSFW_ID_PATTERN.fullmatch(workorder_id):
        raise RuntimeError("远程工单编号无效。")
    name_cell = fx.wait_for_first_visible(
        page.locator(
            f'div.j-td[data-fieldname="name"][title="{workorder_id}"]'
        ),
        timeout_ms=fx.remaining_browser_timeout(deadline, 8_000),
        description=f"远程工单列表行 {workorder_id}",
    )
    detail_link = name_cell.locator('a[href*="#objdetail/CasesObj/"]').first
    detail_url = ""
    if fx.safe_locator_count(detail_link):
        detail_url = fx.build_absolute_fxiaoke_url(fx.safe_get_attribute(detail_link, "href", timeout_ms=fx.remaining_browser_timeout(deadline, 1_000)) or "")
    if not detail_url:
        record_id = fx.safe_get_attribute(name_cell, "data-id", timeout_ms=fx.remaining_browser_timeout(deadline, 1_000)) or ""
        if re.fullmatch(r"[0-9a-fA-F]{24}", record_id):
            detail_url = fx.build_absolute_fxiaoke_url(f"/XV/UI/Home#objdetail/CasesObj/{record_id}")
    if not detail_url:
        raise RuntimeError(f"远程工单列表中未找到 {workorder_id} 的详情链接。")
    return detail_url


def open_workorder_list_detail(page, workorder_id):
    detail_url = get_workorder_list_detail_url(page, workorder_id)
    fx.fast_goto(page, detail_url)
    return wait_for_remote_workorder_detail(page, workorder_id)


def wait_for_remote_workorder_detail(page, workorder_id, *, deadline=None):

    local_deadline = time.time() + 10
    while time.time() < local_deadline:
        body_text = fx.safe_inner_text(page.locator("body"), timeout_ms=fx.remaining_browser_timeout(deadline, 3_000))
        if workorder_id in body_text and any(
            marker in body_text for marker in ("服务日期", "服务工时")
        ):
            return body_text
        fx.wait_with_browser_budget(page, 300, deadline)
    raise RuntimeError(f"历史记录未打开工单 {workorder_id} 的详情。")


def extract_remote_workorder_hours(detail_text):
    field_value = fx.extract_detail_field_value(detail_text, "服务工时")
    if field_value:
        return normalize_positive_service_hours(field_value)
    match = re.search(
        r"(?:服务工时|本次工时)(?:\s*[（(][^）)]*[）)])?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
        str(detail_text or ""),
    )
    if match:
        return normalize_positive_service_hours(match.group(1))
    raise RuntimeError("CRM 工单详情未识别到服务工时。")


def remote_workorder_hours_match(left, right):
    try:
        return Decimal(normalize_positive_service_hours(left)) == Decimal(
            normalize_positive_service_hours(right)
        )
    except ValueError:
        return False


def inspect_remote_workorder_hours_on_authenticated_page(
    page,
    *,
    workorder_id,
    customer_name,
    report_date,
):
    normalized_workorder_id = str(workorder_id or "").strip()
    expected_customer = str(customer_name or "").strip()
    expected_date = str(report_date or "").strip()[:10]
    if not JSFW_ID_PATTERN.fullmatch(normalized_workorder_id):
        raise ValueError(f"远程工单编号格式无效：{workorder_id}")

    rows = search_remote_workorder_rows(page, normalized_workorder_id)
    row = next(
        (
            item
            for item in rows
            if str(item.get("workorder_id", "")).strip() == normalized_workorder_id
        ),
        None,
    )
    if row is None:
        raise RemoteWorkorderNotFound(
            f"《远程服务工单》列表中未找到 {normalized_workorder_id}。"
        )

    observed_customer = str(row.get("customer_name", "") or "").strip()
    if not (
        customer_matches_workorder(expected_customer, observed_customer)
        or customer_matches_workorder(observed_customer, expected_customer)
    ):
        raise RemoteWorkorderIdentityMismatch(
            "customer",
            workorder_id=normalized_workorder_id,
            expected=expected_customer,
            observed=observed_customer,
        )

    observed_date = str(row.get("service_date", "") or "").strip()[:10]
    if observed_date != expected_date:
        raise RemoteWorkorderIdentityMismatch(
            "date",
            workorder_id=normalized_workorder_id,
            expected=expected_date,
            observed=observed_date,
        )

    status_text = str(row.get("status_text", "") or "").strip()
    if any(marker in status_text for marker in ("作废", "取消", "已失效", "已删除")):
        raise RemoteWorkorderIdentityMismatch(
            "status",
            workorder_id=normalized_workorder_id,
            expected="有效的远程服务工单",
            observed=status_text,
        )

    fx.fast_goto(page, row.get("detail_url") or get_workorder_list_detail_url(page, normalized_workorder_id))
    deadline = time.time() + 10
    detail_text = ""
    while time.time() < deadline:
        detail_text = fx.safe_inner_text(page.locator("body"), timeout_ms=2_000)
        if submitted_workorder_text_matches(
            detail_text,
            normalized_workorder_id,
            observed_customer,
            observed_date,
        ):
            try:
                observed_hours = extract_remote_workorder_hours(detail_text)
            except (ValueError, RuntimeError):
                pass  # Identity fields can render before the numeric hours value.
            else:
                break
        page.wait_for_timeout(300)
    else:
        raise RuntimeError(
            f"列表未打开远程工单 {normalized_workorder_id} 的匹配详情及有效工时。"
        )

    return {
        "workorder_id": normalized_workorder_id,
        "crm_customer": observed_customer,
        "service_date": observed_date,
        "crm_observed_hours": observed_hours,
    }


def open_remote_workorder_edit_form(page):
    try:
        edit_button = fx.wait_for_first_visible(
            page.get_by_text("编辑", exact=True),
            timeout_ms=5_000,
            description="远程工单编辑按钮",
        )
        edit_button.click(force=True)
    except Exception:
        more_button = fx.wait_for_first_visible(
            page.locator("button.hi_btn-item"),
            timeout_ms=8_000,
            description="远程工单更多操作按钮",
        )
        more_button.click(force=True)
        menu_edit = fx.wait_for_first_visible(
            page.locator('li[data-autotest="Edit"]'),
            timeout_ms=5_000,
            description="远程工单编辑菜单项",
        )
        menu_edit.click(force=True)
    get_form_field(page, remote_field("hours"))


def submit_remote_workorder_hour_edit(page, milestone_callback=None):
    require_service_owner()
    submit_button = fx.wait_for_first_visible(
        page.locator('span.crm-btn[data-action="submit"]'),
        timeout_ms=8_000,
        description="远程工单编辑提交按钮",
    )
    if milestone_callback:
        milestone_callback("submit_clicked")
    submit_button.click(force=True)
    page.wait_for_timeout(1_000)


def update_remote_workorder_hours_on_authenticated_page(
    page,
    *,
    workorder_id,
    customer_name,
    report_date,
    expected_hours,
    target_hours,
    milestone_callback=None,
):
    if not JSFW_ID_PATTERN.fullmatch(str(workorder_id or "").strip()):
        raise ValueError(f"远程工单编号格式无效：{workorder_id}")
    require_service_owner()
    hours_api_name = remote_field("hours")
    normalized_expected_hours = normalize_positive_service_hours(expected_hours)
    normalized_target_hours = normalize_editable_remote_support_hours(target_hours)
    inspection = inspect_remote_workorder_hours_on_authenticated_page(
        page,
        workorder_id=workorder_id,
        customer_name=customer_name,
        report_date=report_date,
    )
    current_hours = inspection["crm_observed_hours"]
    if not remote_workorder_hours_match(current_hours, normalized_expected_hours):
        raise RemoteWorkorderHoursMismatch(
            f"工单 {workorder_id} CRM 当前工时为 {current_hours}，"
            f"与本机原工时 {normalized_expected_hours} 不一致，已停止更新。",
            observed_hours=current_hours,
        )

    open_remote_workorder_edit_form(page)
    hours_field = get_form_field(page, hours_api_name)
    hours_input = hours_field.locator("input.j-f-ipt").first
    form_current_hours = fx.safe_input_value(hours_input)
    if not remote_workorder_hours_match(form_current_hours, normalized_expected_hours):
        raise RemoteWorkorderHoursMismatch(
            f"工单 {workorder_id} 编辑表单当前工时为 {form_current_hours or '未识别'}，"
            f"与本机原工时 {normalized_expected_hours} 不一致，已停止更新。",
            observed_hours=form_current_hours,
        )
    fill_form_input(page, hours_api_name, normalized_target_hours)
    entered_hours = fx.safe_input_value(hours_input)
    if not remote_workorder_hours_match(entered_hours, normalized_target_hours):
        raise RuntimeError(
            f"工单 {workorder_id} 工时字段未写入目标值 {normalized_target_hours}。"
        )
    submit_remote_workorder_hour_edit(page, milestone_callback=milestone_callback)
    verified = inspect_remote_workorder_hours_on_authenticated_page(
        page,
        workorder_id=workorder_id,
        customer_name=customer_name,
        report_date=report_date,
    )
    observed_hours = verified["crm_observed_hours"]
    if not remote_workorder_hours_match(observed_hours, normalized_target_hours):
        raise RemoteWorkorderHoursMismatch(
            f"工单 {workorder_id} 提交后 CRM 工时仍为 {observed_hours}，"
            f"未确认更新为 {normalized_target_hours}。",
            observed_hours=observed_hours,
        )
    return {
        "workorder_id": str(workorder_id).strip(),
        "crm_customer": verified["crm_customer"],
        "expected_hours": normalized_expected_hours,
        "service_hours": normalized_target_hours,
        "crm_observed_hours": observed_hours,
    }


def read_remote_workorder_hours(
    *,
    workorder_id,
    customer_name,
    report_date,
    report_path,
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    progress_callback=print,
):
    storage_state_path = Path(
        storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH
    ).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    artifact_root = fx.resolve_failure_artifact_root(Path(report_path))
    playwright = browser = context = page = None
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程工单工时复核/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
        )
        result = inspect_remote_workorder_hours_on_authenticated_page(
            page,
            workorder_id=workorder_id,
            customer_name=customer_name,
            report_date=report_date,
        )
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return result
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{str(report_date).replace('-', '')}_remote_hours_read",
        )
        if isinstance(
            exc,
            (RemoteWorkorderIdentityMismatch, RemoteWorkorderNotFound),
        ):
            exc.artifacts = artifacts
            raise
        raise fx.AutomationFailure(str(exc), artifacts=artifacts) from exc
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def update_remote_workorder_hours(
    *,
    workorder_id,
    customer_name,
    report_date,
    expected_hours,
    target_hours,
    report_path,
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    progress_callback=print,
    milestone_callback=None,
):
    normalized_target_hours = normalize_editable_remote_support_hours(target_hours)
    normalized_expected_hours = normalize_positive_service_hours(expected_hours)
    storage_state_path = Path(
        storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH
    ).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    artifact_root = fx.resolve_failure_artifact_root(Path(report_path))
    playwright = browser = context = page = None
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程工单工时同步/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
        )
        if milestone_callback:
            milestone_callback("browser_started")
        result = update_remote_workorder_hours_on_authenticated_page(
            page,
            workorder_id=workorder_id,
            customer_name=customer_name,
            report_date=report_date,
            expected_hours=normalized_expected_hours,
            target_hours=normalized_target_hours,
            milestone_callback=milestone_callback,
        )
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return {
            "ok": True,
            "step": "remote_workorder_hours_sync",
            "status": "submitted",
            **result,
            "message": (
                f"远程工单工时已同步。工单：{result['workorder_id']}，"
                f"工时：{result['service_hours']} 小时。"
            ),
            "artifacts": [],
        }
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{str(report_date).replace('-', '')}_remote_hours_sync",
        )
        if isinstance(
            exc,
            (
                RemoteWorkorderHoursMismatch,
                RemoteWorkorderIdentityMismatch,
                RemoteWorkorderNotFound,
            ),
        ):
            exc.artifacts = artifacts
            raise
        raise fx.AutomationFailure(str(exc), artifacts=artifacts) from exc
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def get_form_field(page, apiname):
    return fx.wait_for_first_visible(
        page.locator(f'.j-comp-wrap[data-apiname="{apiname}"]'),
        timeout_ms=8_000,
        description=f"远程工单字段 {apiname}",
    )


TECHNICAL_SERVICE_LIST_LOADING_SELECTOR = (
    "div.el-loading-mask:visible, div.dt-loading:visible, "
    "div.crm-table-loading:visible"
)


def wait_for_technical_service_list_idle(page, timeout_ms=8_000):
    """Wait until the visible technical-service list is no longer loading.

    A workorder submitted immediately before this one can leave the previous
    list mask mounted; clicking "新建" then lands on the mask and never opens
    the form. Return once the mask is gone, or after the timeout so a
    permanently stuck mask cannot block the flow forever.
    """
    loading = page.locator(TECHNICAL_SERVICE_LIST_LOADING_SELECTOR)
    if not fx.safe_locator_count(loading):
        return
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not fx.safe_locator_count(loading):
            return
        page.wait_for_timeout(200)


def click_technical_service_add(add_button, attempt):
    """Click "新建" with a strategy that matches the retry attempt.

    A transparent mask left by the previous workorder can swallow a force
    click. The DOM click bypasses hit testing and still triggers the CRM's
    delegated handler, so it is used for the second attempt.
    """
    if attempt == 1:
        add_button.evaluate("element => element.click()")
        return
    add_button.click(force=True)


def find_technical_service_add_button(page, timeout_ms=4_000):
    selector = (
        'div.dt-op-box span.crm-btn[data-action="add"], '
        'div.crm-w-table:visible span.crm-btn[data-action="add"]'
    )
    try:
        return fx.wait_for_first_visible(
            page.locator(selector),
            timeout_ms=timeout_ms,
            description="技术服务新建按钮",
        )
    except RuntimeError:
        return fx.wait_for_first_visible(
            page.locator('span.crm-btn[data-action="add"]'),
            timeout_ms=timeout_ms,
            description="技术服务新建按钮",
        )


def reload_technical_service_list(page):
    fx.fast_goto(page, fx.FXIAOKE_TECHNICAL_SERVICE_URL)
    fx.wait_for_first_visible(
        page.locator(fx.TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
        timeout_ms=8_000,
        description="技术服务搜索框",
    )
    wait_for_technical_service_list_idle(page)


def wait_for_remote_workorder_form_state(page, timeout_ms=8_000):
    record_type = tenant_value("remote_workorder", "record_type", required=True, token=True)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        customer_field = page.locator(
            f'.j-comp-wrap[data-apiname="{REMOTE_WORKORDER_CUSTOMER_FIELD}"]'
        ).first
        if fx.safe_locator_count(customer_field) and fx.locator_is_visible(customer_field):
            return customer_field, None
        workorder_type = page.locator(
            "div.crm-comp-steplist:visible "
            f'li[data-value="{record_type}"], '
            "div.crm-c-dialog:visible "
            f'li[data-value="{record_type}"], '
            "div.el-dialog__wrapper:visible "
            f'li[data-value="{record_type}"]'
        ).first
        if fx.safe_locator_count(workorder_type) and fx.locator_is_visible(workorder_type):
            return None, workorder_type
        page.wait_for_timeout(250)
    return None, None


def open_new_remote_workorder_form(page):
    tenant_value("remote_workorder", "record_type", required=True, token=True)
    reset_remote_workorder_dialogs(page)
    fx.open_technical_service_list(page)
    wait_for_technical_service_list_idle(page)
    add_button = find_technical_service_add_button(page)
    for attempt in range(3):
        if attempt == 2:
            # Two clicks were swallowed. Reload the list to get a clean page
            # instead of retrying against the stale mount.
            reload_technical_service_list(page)
            add_button = find_technical_service_add_button(page, timeout_ms=8_000)
        try:
            click_technical_service_add(add_button, attempt)
        except Exception:
            # A detached/re-rendered button cannot be clicked with this handle;
            # let the next attempt re-locate it (attempt 2 is the last one).
            if attempt == 2:
                raise
            continue
        customer_field, workorder_type = wait_for_remote_workorder_form_state(page)
        if customer_field is not None:
            return
        if workorder_type is not None:
            workorder_type.click(force=True)
            if not fx.click_visible_exact_text(page, "确定", timeout_ms=8_000):
                raise RuntimeError("选择《远程服务工单》后未找到“确定”按钮。")
            get_form_field(page, REMOTE_WORKORDER_CUSTOMER_FIELD)
            page.wait_for_timeout(500)
            return
    raise RuntimeError("点击技术服务新建后未显示远程服务工单表单或类型选项。")


def dismiss_remote_workorder_lookup(page):
    dialogs = page.locator("div.crm-c-dialog.crm-comp-pickview:visible")
    for index in reversed(range(fx.safe_locator_count(dialogs))):
        dialog = dialogs.nth(index)
        # The close wrapper can have zero area while its positioned icon is visible.
        close = dialog.locator(
            '.crm-c-dialog-close[data-role="close"] .dialog-close:visible, '
            '.crm-c-dialog-close[data-role="close"]:visible'
        ).first
        if not fx.safe_locator_count(close):
            raise RuntimeError("客户完整查找弹窗无法关闭，已停止处理下一张工单。")
        close.click(timeout=5_000)
        dialog.wait_for(state="hidden", timeout=5_000)


def reset_remote_workorder_dialogs(page):
    # Hash navigation can leave the previous lookup and new-record form mounted.
    dismiss_remote_workorder_lookup(page)
    fields = page.locator(f'.j-comp-wrap[data-apiname="{REMOTE_WORKORDER_CUSTOMER_FIELD}"]:visible')
    if fx.safe_locator_count(fields):
        field = fields.first
        close_remote_workorder_form(page)
        field.wait_for(state="hidden", timeout=5_000)


def close_remote_workorder_form(page):
    dismiss_remote_workorder_lookup(page)
    cancel = page.locator('span.crm-btn:visible').filter(has_text="取消").last
    if fx.safe_locator_count(cancel) and fx.locator_is_visible(cancel):
        cancel.click(force=True)
    elif not fx.click_visible_exact_text(page, "取消", timeout_ms=5_000):
        raise RuntimeError("无法关闭远程服务工单表单。")
    page.wait_for_timeout(600)

    discard = page.locator(
        'div.crm-c-dialog-confirm span[action-type="dialogEnter"]'
    ).filter(has_text="不保存").last
    if fx.safe_locator_count(discard) and fx.locator_is_visible(discard):
        discard.click(force=True)
        page.wait_for_timeout(600)


def _visible_inline_customer_candidates(field):
    items = field.locator("li.j-search-item")
    for index in range(fx.safe_locator_count(items)):
        item = items.nth(index)
        if not fx.locator_is_visible(item):
            continue
        title_node = item.locator("span[data-title]").first
        name = fx.normalize_spacing(
            fx.safe_get_attribute(title_node, "data-title")
            or fx.safe_inner_text(item)
        )
        yield name, item


def collect_inline_customer_candidates(field):
    candidates = []
    for name, _item in _visible_inline_customer_candidates(field):
        if name and name not in candidates:
            candidates.append(name)
    return candidates


def click_inline_customer_candidate(field, selected_name):
    selected_key = strict_customer_identity(selected_name)
    matches = [item for name, item in _visible_inline_customer_candidates(field)
               if strict_customer_identity(name) == selected_key]
    if len(matches) != 1:
        raise RuntimeError(f"无法点击 CRM 客户候选“{selected_name}”：未找到唯一可见目标，已停止以避免同名误选。")
    matches[0].click(force=True)


def click_full_lookup_customer_cell(cell):
    """勾选完整查找表格里的目标行，不能点名称单元格。

    名称单元格内含 ``a.j-show-lookup`` 链接，点它会打开客户详情浮层而不勾选
    复选框；随后点「确定」CRM 会提示「请选择一条数据!」，清理阶段又被该提示
    遮挡导致关闭按钮超时。CRM 的 ``em.checkbox-item`` 是空元素、靠 CSS 渲染，
    可能没有可见尺寸，因此先点可见的 em，否则点有尺寸的复选框单元格；旧版
    无复选框列的 DOM 才退回点击名称单元格。
    """
    checkbox_cell = cell.locator(
        "xpath=ancestor::tr[1]//td[contains(@class,'column-checkbox')]"
    ).first
    if fx.safe_locator_count(checkbox_cell) and fx.locator_is_visible(checkbox_cell):
        checkbox = checkbox_cell.locator("em[class*='checkbox-item']").first
        if fx.safe_locator_count(checkbox) and fx.locator_is_visible(checkbox):
            checkbox.click(force=True)
        else:
            checkbox_cell.click(force=True)
        return
    cell.click(force=True)


def get_full_lookup_table_scope(page):
    tables = page.locator("div.crm-comp-pickview:visible div.dt-main:visible")
    if fx.safe_locator_count(tables) != 1:
        raise RuntimeError("客户完整查找界面未显示唯一客户数据表。")
    return tables.first


def _visible_full_lookup_customer_candidates(scope):
    cells = scope.locator('div.j-td[data-fieldname="name"]')
    for index in range(fx.safe_locator_count(cells)):
        cell = cells.nth(index)
        if not fx.locator_is_visible(cell):
            continue
        name = fx.normalize_spacing(
            fx.safe_get_attribute(cell, "title") or fx.safe_inner_text(cell)
        )
        yield name, cell


def collect_full_lookup_customer_candidates(scope):
    return filter_customer_candidate_names([
        name for name, _cell in _visible_full_lookup_customer_candidates(scope)
    ])


def expand_customer_school_search_names(name):
    """Expand bounded school shorthand for recall only, without changing identity."""
    name = unicodedata.normalize("NFKC", str(name or "")).strip()
    variants: list[str] = []
    for suffix, expansions in CUSTOMER_SCHOOL_SHORT_FORMS.items():
        if name.endswith(suffix) and len(name) > len(suffix):
            variants.extend(name[:-len(suffix)] + expanded for expanded in expansions)
            break
    ordinal = re.search(rf"第?([{CUSTOMER_ORDINAL_CHARS}\d]+)(中(?:学)?|小学)$", name)
    if ordinal:
        number = ordinal.group(1)
        if len(number) <= 2 and number.isascii() and number.isdigit() and 0 < int(number) < 100:
            value = int(number)
            digits = "零一二三四五六七八九"
            number = (digits[value] if value < 10 else
                      (digits[value // 10] if value >= 20 else "") + "十" +
                      (digits[value % 10] if value % 10 else ""))
        stage = "小学" if ordinal.group(2) == "小学" else "中学"
        variants.append(name[:ordinal.start()] + f"第{number}{stage}")
    return list(dict.fromkeys(variants))


def normalize_full_lookup_customer_key(value):
    """Build a lossy search key, never an identity or authorization key.

    School types, regions and departments can distinguish different customers.
    Removing them helps recall shorthand candidates but cannot prove equivalence.
    """
    key = normalize_customer_match_text(unicodedata.normalize("NFKC", str(value or "")))
    expanded = expand_customer_school_search_names(key)
    if expanded:
        key = expanded[0]
    for suffix in CUSTOMER_COMPANY_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 2:
            key = key[: -len(suffix)]
            break
    key = re.sub(r"[省市县区]", "", key)
    key = re.sub(rf"第(?=[{CUSTOMER_ORDINAL_CHARS}\d])", "", key)
    for suffix in CUSTOMER_STAGE_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 1:
            key = key[: -len(suffix)]
            break
    for region in CUSTOMER_GENERIC_REGIONS:
        # Keep at least three distinguishing characters after removing a country prefix.
        if key.startswith(region) and len(key) - len(region) >= 3:
            key = key[len(region):]
            break
    # A shortened school name may leave its ordinal followed by “中”.
    return re.sub(rf"([{CUSTOMER_ORDINAL_CHARS}\d])中$", r"\1", key)


def score_customer_candidate(query, candidate):
    """Rank search candidates; only complete names receive 1.0."""
    if strict_customer_identity(query) and strict_customer_identity(query) == strict_customer_identity(candidate):
        return 1.0
    query_key = normalize_full_lookup_customer_key(query)
    candidate_key = normalize_full_lookup_customer_key(candidate)
    if not query_key or not candidate_key:
        return 0.0
    if query_key == candidate_key:
        return 0.9
    query_text = normalize_customer_match_text(query)
    candidate_text = normalize_customer_match_text(candidate)
    if len(query_text) >= 4 and (
        candidate_text.startswith(query_text) or query_text.startswith(candidate_text)
    ):
        # A department or campus adds words on one side only.
        return 0.85
    shorter, longer = sorted((query_key, candidate_key), key=len)
    if len(shorter) >= 4 and (
        longer.startswith(shorter) or longer.endswith(shorter) or shorter in longer
    ):
        # City prefix added or company form changed, e.g. "绿萌科技" vs "赣州绿萌科技".
        return 0.9
    return 0.0


def rank_customer_candidates(query, candidate_names):
    """Order candidates by name similarity; equal scores keep the CRM order."""
    return sorted(
        ((name, score_customer_candidate(query, name)) for name in candidate_names),
        key=lambda item: -item[1],
    )


def build_customer_search_queries(name):
    """Original name first, followed by bounded substring-search variants."""
    base = (name or "").strip()
    if not base:
        return []
    queries = [base]

    generic = CUSTOMER_GENERIC_REGIONS | fx.GENERIC_CUSTOMER_ALIASES | {"公司", "科技", "职业", "中等"}

    def add(candidate):
        candidate = candidate.strip()
        if candidate and len(candidate) >= 2 and candidate not in generic and candidate not in queries:
            queries.append(candidate)

    cleaned = normalize_customer_match_text(unicodedata.normalize("NFKC", base))
    add(cleaned)
    expanded = expand_customer_school_search_names(cleaned)
    for variant in expanded:
        add(variant)
    # Search the institution separately when the report adds a department/campus.
    suffixes = "|".join(re.escape(item) for item in sorted(
        (item for item in fx.CUSTOMER_INSTITUTION_SUFFIXES
         if item not in CUSTOMER_SCHOOL_SHORT_FORMS and item != "一中"),
        key=len, reverse=True,
    ))
    institution = re.match(rf"^(.+?(?:{suffixes}))", cleaned)
    if institution:
        add(institution.group(1))
    for variant in list(queries):
        without_admin = re.sub(r"[省市县区]", "", variant)
        add(without_admin)
        core = without_admin
        for suffix in CUSTOMER_COMPANY_SUFFIXES:
            if core.endswith(suffix) and len(core) > len(suffix) + 1:
                core = core[:-len(suffix)]
                add(core)
                break
        for prefix in sorted(CUSTOMER_GENERIC_REGIONS, key=len, reverse=True):
            if core.startswith(prefix) and len(core) - len(prefix) >= 3:
                add(core[len(prefix):])
                break
    for variant in expanded:
        ordinal = re.search(rf"第[{CUSTOMER_ORDINAL_CHARS}\d]+(?:中学|小学)$", variant)
        if ordinal:
            add(ordinal.group(0))
    # Keep the written locality as the last, broadest automatic fallback.
    region = re.sub(r"[省市县区]", "", base)[:2]
    if len(region) == 2:
        add(region)
    if len(queries) > CUSTOMER_SEARCH_QUERY_LIMIT:
        return queries[:CUSTOMER_SEARCH_QUERY_LIMIT - 1] + [queries[-1]]
    return queries


def full_lookup_candidates_match_query(query, candidates):
    # Inline suggestions can expand shorthand, but dropping school types would
    # let an old primary-school row interrupt a secondary-school search.
    query_text = normalize_customer_match_text(unicodedata.normalize("NFKC", query))
    variants = [query_text, *expand_customer_school_search_names(query_text)]

    def core(value):
        text = re.sub(r"[省市县区]", "", value).casefold()
        for suffix in CUSTOMER_COMPANY_SUFFIXES:
            if text.endswith(suffix):
                return text[:-len(suffix)]
        return text

    for candidate in candidates:
        if strict_customer_identity(query) and strict_customer_identity(query) == strict_customer_identity(candidate):
            return True
        if score_customer_candidate(query, candidate) < CUSTOMER_QUERY_MATCH_SCORE:
            continue
        observed = core(normalize_customer_match_text(candidate))
        if any(min(len(core(value)), len(observed)) >= 2 and
               (core(value) in observed or observed in core(value)) for value in variants):
            return True
    return False


def customer_candidate_matches_search(query, keyword, candidate):
    """A retrieved candidate may be shown without claiming identity equivalence."""
    generic = CUSTOMER_GENERIC_REGIONS | fx.GENERIC_CUSTOMER_ALIASES | {"公司", "科技", "职业", "中等"}
    key = strict_customer_identity(unicodedata.normalize("NFKC", keyword))
    observed = strict_customer_identity(unicodedata.normalize("NFKC", candidate))
    if key and key == observed and strict_customer_identity(query) == strict_customer_identity(candidate):
        return True
    generic_key = re.sub(r"[省市县区]", "", key)
    if len(key) < 2 or keyword in generic or generic_key in generic or key not in observed:
        return False
    anchor = re.sub(r"[省市县区]", "", normalize_customer_match_text(query))
    for prefix in sorted(CUSTOMER_GENERIC_REGIONS, key=len, reverse=True):
        if anchor.startswith(prefix) and len(anchor) > len(prefix) + 1:
            anchor = anchor[len(prefix):]
            break
    # Broad ordinal/stage searches must still belong to the original institution.
    return len(anchor) < 2 or anchor[:2].casefold() in observed


def trigger_full_lookup_search(search_input):
    search_button = search_input.locator(
        "xpath=ancestor::div[contains(@class,'dt-ipt-wrap')]"
        "/following-sibling::span[contains(@class,'dt-sc-btn')]"
    ).first
    if fx.safe_locator_count(search_button):
        search_button.click(force=True)
    else:
        search_input.press("Enter")


def choose_customer_from_full_lookup(
    page,
    query,
    remembered_primary,
    input_func,
    require_remembered=False,
    on_explicit_choice=None,
    selection_query=None,
    allow_manual_search=False,
    initial_search=None,
    compact=False,
):
    dialog = fx.wait_for_first_visible(
        page.locator("div.crm-comp-pickview"),
        timeout_ms=8_000,
        description="客户完整查找弹窗",
    )
    search_input = fx.wait_for_first_visible(
        dialog.locator('input.el-input__inner.dt-ipt[placeholder="搜索"]'),
        timeout_ms=8_000,
        description="客户完整查找搜索框",
    )
    original_query = selection_query or query
    manual_searches = 0
    if initial_search is not None:
        initial_search = CustomerSearchRequested(initial_search).keyword
        manual_searches = 1
    if require_remembered and initial_search:
        raise RuntimeError("已授权客户不能通过重查改选，请重新预检。")
    queries = ([initial_search] if initial_search else
               [query] if require_remembered else build_customer_search_queries(query))
    manual = bool(initial_search)
    attempted: list[str] = []
    while True:
        deadline = time.monotonic() + FULL_LOOKUP_QUERY_TIMEOUT_SECONDS
        candidates: list[str] = []
        table_scope = None
        saw_table = False
        saw_rows = False
        for index, current_query in enumerate(queries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            search_input.fill(current_query)
            trigger_full_lookup_search(search_input)
            attempted.append(current_query)
            # Every remaining keyword gets a turn; never spend the whole budget
            # on early empty searches or wait beyond the shared deadline.
            round_deadline = min(time.monotonic() + remaining / (len(queries) - index), deadline)
            page.wait_for_timeout(min(FULL_LOOKUP_SETTLE_MS, max(0, (round_deadline - time.monotonic()) * 1000)))
            while True:
                try:
                    table_scope = get_full_lookup_table_scope(page)
                    saw_table = True
                except RuntimeError:
                    table_scope = None
                rows = collect_full_lookup_customer_candidates(table_scope) if table_scope is not None else []
                saw_rows = saw_rows or bool(rows)
                candidates = [name for name in rows if customer_candidate_matches_search(
                    current_query if manual else query, current_query, name,
                )]
                if candidates:
                    break
                wait_ms = min(REMOTE_WORKORDER_POLL_MS, (round_deadline - time.monotonic()) * 1000)
                if wait_ms <= 0:
                    break
                page.wait_for_timeout(wait_ms)
            if candidates:
                break
        if require_remembered and not any(strict_customer_identity(item) == strict_customer_identity(remembered_primary) for item in candidates):
            raise RuntimeError(f"预检确认的客户单位“{remembered_primary}”不在当前候选项中，请重新预检。")
        if not candidates:
            detail = ("界面未显示客户数据表" if not saw_table else
                      "返回候选但均与名称不符" if saw_rows else "搜索无结果或尚未加载")
            failure = (f"CRM 客户完整查找未找到“{original_query}”：{detail}；"
                       f"已尝试关键词：{'、'.join(attempted)}。已停止选择以避免误选默认列表。")
            # An operator may retry the read-only search, but blank/failed grids
            # never become proof that this customer is absent. A missing grid is
            # still a system fault; a loaded grid with no match is a customer
            # lookup problem and may be isolated after an explicit confirmation.
            if not saw_table:
                raise RuntimeError(failure)
            if not (allow_manual_search and manual_searches < CUSTOMER_MANUAL_SEARCH_LIMIT):
                raise RemoteCustomerLookupError(original_query, failure, reason="not_found")
            answer = input_func(f"\n{failure}\n直接输入新关键词重新检索，回车取消当前客户：").strip()
            if not answer:
                raise RemoteCustomerSelectionError(original_query, "已明确取消当前客户检索。", reason="cancelled")
            keyword = answer[1:] if answer.startswith("/") else answer
            try:
                request = CustomerSearchRequested(keyword)
            except ValueError as exc:
                raise RemoteCustomerLookupError(
                    original_query, f"{failure}\n{exc}", reason="invalid_search",
                ) from exc
        else:
            try:
                selected = choose_customer_candidate_name(
                    original_query, candidates,
                    remembered_primary=remembered_primary,
                    input_func=input_func,
                    on_explicit_choice=on_explicit_choice,
                    allow_search=allow_manual_search and not require_remembered,
                    force_confirmation=manual,
                    compact=compact,
                )
            except CustomerSearchRequested as exc:
                request = exc
            else:
                selected_key = strict_customer_identity(selected)
                matches = [cell for name, cell in _visible_full_lookup_customer_candidates(table_scope)
                           if strict_customer_identity(name) == selected_key]
                if len(matches) != 1:
                    raise RuntimeError(f"无法在完整查找界面唯一选择客户“{selected}”，已停止以避免同名误选。")
                click_full_lookup_customer_cell(matches[0])
                if not fx.click_visible_exact_text(dialog, "确定", timeout_ms=8_000):
                    raise RuntimeError("选择完整 CRM 客户后未找到“确定”按钮。")
                page.wait_for_timeout(800)
                return selected
        manual_searches += 1
        if manual_searches > CUSTOMER_MANUAL_SEARCH_LIMIT:
            raise RemoteCustomerLookupError(
                original_query, "客户手动重查已达到3轮上限，请核对名称后重新预检。",
                reason="manual_limit",
            )
        queries = [request.keyword]
        manual = True


def wait_for_inline_customer_candidates(field, page, timeout_seconds, *, query=None):
    """Poll the inline suggestion list; the CRM fills it asynchronously."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        candidates = collect_inline_customer_candidates(field)
        if (candidates and (query is None or full_lookup_candidates_match_query(query, candidates))) or time.monotonic() >= deadline:
            return candidates
        page.wait_for_timeout(250)


def select_customer_for_draft(
    page,
    draft,
    input_func=read_brief_input,
    remember_alias=False,
    compact=False,
):
    field = get_form_field(page, REMOTE_WORKORDER_CUSTOMER_FIELD)
    resolved_primary = sanitize_remembered_customer_name(
        resolve_customer_primary_name(draft.customer_name)
    )
    query = draft.selected_customer_name or resolved_primary or draft.customer_name
    remembered_primary = draft.selected_customer_name
    if not remembered_primary and strict_customer_identity(resolved_primary) != strict_customer_identity(draft.customer_name):
        memory = fx.load_customer_alias_memory(strict=True).get(fx.normalize_customer_alias_key(draft.customer_name), {})
        alias_matches = (
            strict_customer_identity(memory.get("alias")) == strict_customer_identity(draft.customer_name)
            and strict_customer_identity(memory.get("primary_customer")) == strict_customer_identity(resolved_primary)
        )
        if alias_matches and (compact or memory.get("explicitly_confirmed") is True):
            # In compact mode the operator confirms the report-name → CRM-unit
            # mapping in the single submission table, so a maintained alias is
            # enough to preselect it without another per-customer prompt.
            remembered_primary = resolved_primary
    search_input = field.locator('input.j-search-ipt[placeholder="搜索"]').first
    explicit_choices: list[str] = []

    # Quick lookup once; keyword expansion belongs to the full lookup, rather
    # than waiting four seconds per variant in both widgets.
    search_input.fill(query)
    candidates = wait_for_inline_customer_candidates(field, page, 4, query=query)
    initial_search = None
    inline_matches = bool(candidates and full_lookup_candidates_match_query(query, candidates))
    if draft.selected_customer_name:
        # Suggestions may truncate departments. Try the full lookup with the
        # same locked identity before declaring that the confirmed unit is gone.
        inline_matches = any(strict_customer_identity(item) == strict_customer_identity(draft.selected_customer_name) for item in candidates)
    if inline_matches:
        try:
            selected = choose_customer_candidate_name(
                draft.customer_name,
                candidates,
                remembered_primary=remembered_primary,
                input_func=input_func,
                on_explicit_choice=explicit_choices.append,
                allow_search=not bool(draft.selected_customer_name),
                compact=compact,
            )
        except CustomerSearchRequested as exc:
            initial_search = exc.keyword
        else:
            click_inline_customer_candidate(field, selected)
            page.wait_for_timeout(1_000)
    if initial_search is not None or not inline_matches:
        picker = field.locator(".j-pick-self").first
        if not fx.safe_locator_count(picker):
            if draft.selected_customer_name:
                raise RuntimeError(f"预检确认的客户单位“{draft.selected_customer_name}”未出现在快捷候选，且完整查找入口不可用，请重新预检。")
            raise RuntimeError(f"客户“{draft.customer_name}”没有候选项，也没有完整查找入口。")
        picker.click(force=True)
        page.get_by_text("选择数据", exact=True).wait_for(state="visible", timeout=8_000)
        selected = choose_customer_from_full_lookup(
            page,
            query,
            remembered_primary,
            input_func,
            on_explicit_choice=explicit_choices.append,
            selection_query=draft.customer_name,
            allow_manual_search=not bool(draft.selected_customer_name),
            initial_search=initial_search,
            compact=compact,
            **({"require_remembered": True} if draft.selected_customer_name else {}),
        )
    validate_preselected_customer(draft, selected)
    if remember_alias and selected in explicit_choices:
        try:
            fx.remember_customer_alias_mapping(draft.customer_name, selected, explicitly_confirmed=True)
        except Exception as exc:
            print(f"[客户别名] 本次选择有效，但别名未保存（{type(exc).__name__}）；下次仍需确认。")
    return selected


def fill_first_available_contact(page):
    deadline = time.time() + 15
    while time.time() < deadline:
        buttons = page.locator(
            ".wo-widget-casesrelatedinfo__list-item button"
        ).filter(has_text="回填")
        for index in range(fx.safe_locator_count(buttons)):
            button = buttons.nth(index)
            if fx.locator_is_visible(button):
                button.click(force=True)
                contact_field = get_form_field(page, "contact_id")
                contact_input = contact_field.locator("input").first
                selected_deadline = time.time() + 5
                while time.time() < selected_deadline:
                    selected_contact = fx.safe_input_value(contact_input)
                    if selected_contact:
                        return selected_contact
                    page.wait_for_timeout(250)
        page.wait_for_timeout(300)
    raise RuntimeError("当前客户没有可回填联系人，无法创建远程服务工单。")


def read_form_field_value(page, apiname):
    field = get_form_field(page, apiname)
    controls = field.locator("input, textarea")
    values = [
        fx.safe_input_value(controls.nth(index))
        for index in range(fx.safe_locator_count(controls))
    ]
    field_text = fx.safe_inner_text(field)
    return fx.normalize_spacing(
        " ".join([*(value for value in values if value), field_text])
    )


def normalize_remote_workorder_owner_text(value):
    return fx.normalize_spacing(
        " ".join(
            token
            for token in str(value or "").split()
            if token.lower() not in {"false", "true"}
        )
    )


def validate_remote_workorder_form_snapshot(snapshot, draft, selected_customer):
    owner = require_service_owner()
    errors = []
    customer_value = str(snapshot.get("customer", "")).strip()
    if not (
        customer_matches_workorder(selected_customer, customer_value)
        or customer_matches_workorder(customer_value, selected_customer)
    ):
        errors.append("客户单位不匹配")
    if not str(snapshot.get("contact", "")).strip():
        errors.append("联系人未回填")

    expected_contains = (
        ("region", remote_option("region")[0], "所在区域与租户配置不一致"),
        ("product", remote_option("product")[0], "服务产品与租户配置不一致"),
        ("category", draft.service_category, "服务内容类别不匹配"),
        ("warranty", remote_option("warranty")[0], "质保情况与租户配置不一致"),
        ("service_date", draft.report_date_iso, "服务日期不匹配"),
        ("hours", draft.service_hours, f"服务工时不是{draft.service_hours}"),
        ("completion_status", draft.completion_status, "服务完成情况与审核值不一致"),
        ("owner", owner, "工单负责人与当前账号配置不一致"),
        ("description", draft.content, "服务需求详述不匹配"),
        ("summary", draft.content, "完成小结不匹配"),
    )
    for key, expected, message in expected_contains:
        observed = str(snapshot.get(key, ""))
        if key in {"completion_status", "owner"}:
            matches = fx.normalize_spacing(expected) == fx.normalize_spacing(observed)
        elif key == "hours":
            matches = remote_workorder_hours_match(expected, observed)
        else:
            matches = bool(expected) and expected in observed
        if not matches:
            errors.append(message)
    return errors


def capture_remote_workorder_form_snapshot(page):
    fields = require_remote_form_fields()
    return {
        "customer": read_form_field_value(page, "account_id"),
        "contact": read_form_field_value(page, "contact_id"),
        "region": read_form_field_value(page, fields["region"]),
        "product": read_form_field_value(page, fields["product"]),
        "category": read_form_field_value(page, fields["category"]),
        "warranty": read_form_field_value(page, fields["warranty"]),
        "service_date": read_form_field_value(page, fields["service_date"]),
        "hours": fx.safe_input_value(get_form_field(page, fields["hours"]).locator("input.j-f-ipt").first),
        "completion_status": fx.safe_input_value(
            get_form_field(page, fields["completion_status"]).locator("input.j-select-input").first
        ),
        "owner": normalize_remote_workorder_owner_text(fx.safe_inner_text(get_form_field(page, "owner"))),
        "description": read_form_field_value(page, "description"),
        "summary": read_form_field_value(page, fields["summary"]),
    }


def fill_form_input(page, apiname, value):
    field = get_form_field(page, apiname)
    control = field.locator("textarea, input").first
    control.click(force=True)
    control.fill(str(value))
    control.press("Tab")
    page.wait_for_timeout(250)


def fill_other_option_custom_text(option, custom_text):
    expected_text = (custom_text or get_other_category_custom_text()).strip()
    deadline = time.time() + 5
    while time.time() < deadline:
        custom_input = option.locator(
            'input.j-custom-ipt:not([disabled])'
        ).first
        if (
            fx.safe_locator_count(custom_input)
            and fx.locator_is_visible(custom_input)
            and fx.locator_is_enabled(custom_input)
        ):
            custom_input.fill(expected_text)
            custom_input.press("Tab")
            if expected_text in fx.safe_input_value(custom_input):
                return
        time.sleep(0.2)
    raise RuntimeError("选择“其他”后自定义内容输入框未启用或未写入，已停止提交。")


def other_option_custom_input_is_enabled(option):
    custom_input = option.locator('input.j-custom-ipt:not([disabled])').first
    return (
        fx.safe_locator_count(custom_input)
        and fx.locator_is_visible(custom_input)
        and fx.locator_is_enabled(custom_input)
    )


def wait_for_form_option(page, field, option_selector, label_text, timeout_ms=8_000):
    """Wait for one select option, re-opening the panel when the first click is swallowed.

    CRM re-renders the whole form right after the customer is picked. A click that
    lands during that re-render leaves the dropdown closed, so the option never
    shows up and the old single-shot wait failed the whole work order.
    """
    deadline = time.time() + timeout_ms / 1000
    last_click = time.time()
    while True:
        remaining_ms = max(300, int((deadline - time.time()) * 1000))
        try:
            return fx.wait_for_first_visible(
                page.locator(option_selector),
                timeout_ms=min(remaining_ms, 1_500),
                description=f"选项“{label_text}”",
            )
        except RuntimeError:
            if time.time() >= deadline:
                raise
        panels = field.locator("div.crm-w-panel")
        try:
            panel_open = any(
                fx.safe_locator_count(panels.nth(index)) and fx.locator_is_visible(panels.nth(index))
                for index in range(fx.safe_locator_count(panels))
            )
        except fx.PlaywrightError:
            panel_open = False
        if not panel_open and time.time() - last_click >= 1.0:
            field.locator("input.j-select-input").first.click(force=True)
            last_click = time.time()
        time.sleep(0.2)


def select_form_option(page, apiname, data_value, label_text, custom_text=""):
    field = get_form_field(page, apiname)
    field.locator("input.j-select-input").first.click(force=True)
    option_selector = f'li[action-type="itemclick"][data-value="{data_value}"]'
    if data_value == "other":
        # The CRM appends the currently opened dropdown panel after older hidden
        # panels. Only ``other`` is reused by unrelated fields, so use the last
        # menu item here while preserving normal visible-option handling.
        option = page.locator(option_selector).last
        option.wait_for(state="visible", timeout=8_000)
    else:
        option = wait_for_form_option(page, field, option_selector, label_text)
    option.click(force=True)
    page.wait_for_timeout(250)
    if data_value == "other":
        if not other_option_custom_input_is_enabled(option):
            # CRM's custom ``other`` item can close its menu on a forced
            # Playwright click without dispatching the selection handler.
            option.evaluate("node => node.click()")
        if not other_option_custom_input_is_enabled(option):
            raise RuntimeError("选择“其他”后自定义内容输入框未启用，已停止提交。")
        expected_custom_text = (custom_text or get_other_category_custom_text()).strip()
        fill_other_option_custom_text(option, expected_custom_text)
        field_value = read_form_field_value(page, apiname)
        if label_text not in field_value or expected_custom_text not in field_value:
            raise RuntimeError("选择“其他”后字段未生效，已停止提交。")
        page.wait_for_timeout(FORM_SELECT_SETTLE_MS)
        return

    if label_text not in read_form_field_value(page, apiname):
        # CRM's custom ``other`` item can close its menu on a forced Playwright
        # click without dispatching the selection handler. Retry only after
        # confirming that the field itself is still unchanged.
        option.evaluate("node => node.click()")
    deadline = time.time() + 5
    while time.time() < deadline:
        if label_text in read_form_field_value(page, apiname):
            break
        page.wait_for_timeout(250)
    else:
        raise RuntimeError(f"选择“{label_text}”后字段未生效，已停止提交。")
    # CRM keeps the just-selected panel in its closing animation briefly; the
    # next select can otherwise ignore its click and expose no options.
    page.wait_for_timeout(FORM_SELECT_SETTLE_MS)


def ensure_default_select_value(page, apiname, expected_value, option_value=""):
    field = get_form_field(page, apiname)
    current = fx.safe_input_value(field.locator("input.j-select-input").first)
    exact_status = apiname in {remote_field("completion_status"), remote_field("warranty")}

    def matches(actual):
        if exact_status:
            return fx.normalize_spacing(actual) == fx.normalize_spacing(expected_value)
        return expected_value in actual

    if matches(current):
        return
    field.locator("input.j-select-input").first.click(force=True)
    if option_value:
        option = page.locator(f'li[action-type="itemclick"][data-value="{option_value}"]').last
        if fx.safe_locator_count(option) and fx.locator_is_visible(option):
            option.click(force=True)
            actual = fx.safe_input_value(field.locator("input.j-select-input").first) if exact_status else read_form_field_value(page, apiname)
            if matches(actual):
                return
    if not fx.click_visible_exact_text(page, expected_value, timeout_ms=8_000):
        raise RuntimeError(f"无法选择默认值“{expected_value}”。")
    if exact_status:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            actual = fx.safe_input_value(field.locator("input.j-select-input").first)
            if matches(actual):
                return
            page.wait_for_timeout(200)
        raise RuntimeError("服务完成情况未按审核值生效，已停止提交。")


def read_remote_workorder_completion_status_options(page):
    """Read the opened field's actual choices without selecting or submitting."""
    visible_options = page.locator('li[action-type="itemclick"][data-value]:visible')
    if fx.safe_locator_count(visible_options):
        raise RuntimeError("读取完成情况前仍有其他下拉菜单打开，已停止预检。")
    field = get_form_field(page, remote_field("completion_status"))
    control = field.locator("input.j-select-input").first
    control.click(force=True)
    observed = ()
    try:
        deadline = time.monotonic() + 8
        previous = ()
        last_click = time.monotonic()
        while time.monotonic() < deadline:
            labels = []
            for index in range(fx.safe_locator_count(visible_options)):
                option = visible_options.nth(index)
                label = fx.normalize_spacing(fx.safe_inner_text(option))
                data_value = str(fx.safe_get_attribute(option, "data-value") or "").strip()
                if not data_value or is_completion_status_placeholder(label):
                    continue
                labels.append(label)
            current = tuple(dict.fromkeys(labels))
            # All options must belong to a single menu; preexisting visible menus
            # were rejected above, so this menu is bound to the field just opened.
            if current and current == previous:
                parent_count = visible_options.evaluate_all("nodes => new Set(nodes.map(node => node.parentElement)).size")
                if parent_count != 1:
                    raise RuntimeError("完成情况出现多个可见选项菜单，已停止预检。")
                observed = current
                break
            previous = current
            # The customer choice re-renders the whole form and can swallow this
            # first click, leaving the panel closed with no options at all.
            # Re-open it within the same budget, as wait_for_form_option does.
            if not current:
                panels = field.locator("div.crm-w-panel")
                try:
                    panel_open = any(
                        fx.safe_locator_count(panels.nth(index)) and fx.locator_is_visible(panels.nth(index))
                        for index in range(fx.safe_locator_count(panels))
                    )
                except fx.PlaywrightError:
                    panel_open = False
                if not panel_open and time.monotonic() - last_click >= 1.0:
                    control.click(force=True)
                    last_click = time.monotonic()
                    previous = ()
            page.wait_for_timeout(200)
        if not observed:
            raise RuntimeError("无法可靠读取 CRM 服务完成情况选项，已停止预检，未提交。")
    finally:
        control.press("Escape")
        if fx.safe_locator_count(visible_options):
            control.click(force=True)
        if fx.safe_locator_count(visible_options):
            raise RuntimeError("完成情况选项菜单未关闭，已停止操作，未提交。")
    return observed


def fill_remote_workorder_form(
    page,
    draft,
    input_func=read_brief_input,
    remember_alias=False,
):
    values = build_remote_workorder_form_values(draft)
    fields = require_remote_form_fields()
    selected_customer = select_customer_for_draft(
        page,
        draft,
        input_func=input_func,
        remember_alias=remember_alias,
    )
    if draft.completion_status_options:
        current_options = read_remote_workorder_completion_status_options(page)
        if draft.completion_status not in current_options:
            raise RuntimeError("预检确认的完成情况已不在当前 CRM 选项中，请重新预检和确认。")
    fill_first_available_contact(page)
    for key in ("region", "product"):
        apiname = fields[key]
        select_form_option(page, apiname, values[apiname], remote_option(key)[0])
    select_form_option(
        page,
        fields["category"],
        values[fields["category"]],
        draft.service_category,
        custom_text=get_other_category_custom_text(),
    )
    fill_form_input(page, "description", values["description"])
    warranty_label, warranty_value = remote_option("warranty")
    ensure_default_select_value(page, fields["warranty"], warranty_label, option_value=warranty_value)
    for key in ("service_date", "hours", "summary"):
        apiname = fields[key]
        fill_form_input(page, apiname, values[apiname])
    ensure_default_select_value(page, fields["completion_status"], draft.completion_status)
    owner_text = normalize_remote_workorder_owner_text(
        fx.safe_inner_text(get_form_field(page, "owner"))
    )
    if owner_text != fx.normalize_spacing(values["owner"]):
        raise RuntimeError("远程工单负责人和当前账号配置不一致，已停止提交。")
    return selected_customer


def select_remote_workorder_business_type(page, *, deadline=None):
    field = fx.get_visible_technical_service_field(
        page,
        REMOTE_WORKORDER_TYPE_FIELD_TITLES,
        "远程服务工单业务类型筛选",
        **({"deadline": deadline} if deadline is not None else {}),
    )
    fx.choose_technical_service_dropdown_value(
        page,
        field,
        REMOTE_WORKORDER_LABEL,
        "业务类型",
        current_values=(REMOTE_WORKORDER_LABEL, "远程服务工单"),
        **({"deadline": deadline} if deadline is not None else {}),
    )


def clear_remote_workorder_list_filters(page, *, deadline=None):
    filter_button = page.locator(".j-filter-btn.filter-num").first
    if not (
        fx.safe_locator_count(filter_button)
        and fx.locator_is_visible(filter_button)
    ):
        return True
    try:
        filter_button.click(timeout=fx.remaining_browser_timeout(deadline, 5_000))
    except fx.PlaywrightError:
        filter_button.evaluate("node => node.click()")
    filter_dialog = fx.wait_for_first_visible(
        page.locator(".crm-w-table-filter-dialog"),
        timeout_ms=fx.remaining_browser_timeout(deadline, 5_000),
        description="技术服务列表筛选弹窗",
    )
    reset_button = filter_dialog.locator(".j-reset").first
    apply_button = filter_dialog.locator(".j-filter").first
    if not (
        fx.safe_locator_count(reset_button)
        and fx.locator_is_visible(reset_button)
        and fx.safe_locator_count(apply_button)
        and fx.locator_is_visible(apply_button)
    ):
        raise RuntimeError("技术服务列表筛选弹窗缺少清除或应用按钮。")
    try:
        reset_button.click(timeout=fx.remaining_browser_timeout(deadline, 5_000))
    except (AttributeError, fx.PlaywrightError):
        reset_button.evaluate("node => node.click()")
    fx.wait_with_browser_budget(page, 500, deadline)
    apply_button = filter_dialog.locator(".j-filter").first
    try:
        apply_button.click(timeout=fx.remaining_browser_timeout(deadline, 5_000))
    except (AttributeError, fx.PlaywrightError):
        apply_button.evaluate("node => node.click()")
    try:
        filter_dialog.wait_for(state="hidden", timeout=fx.remaining_browser_timeout(deadline, 5_000))
    except Exception:
        close_button = filter_dialog.locator(".j-cancle, .close").first
        if not (
            fx.safe_locator_count(close_button)
            and fx.locator_is_visible(close_button)
        ):
            raise RuntimeError("技术服务列表筛选弹窗未关闭，且未找到关闭按钮。")
        try:
            close_button.evaluate("node => node.click()")
        except (AttributeError, fx.PlaywrightError):
            close_button.click(force=True, **({"timeout": fx.remaining_browser_timeout(deadline, 5_000)} if deadline is not None else {}))
        try:
            filter_dialog.wait_for(state="hidden", timeout=fx.remaining_browser_timeout(deadline, 5_000))
        except Exception as exc:
            raise RuntimeError("技术服务列表筛选弹窗未能关闭。") from exc
        fx.wait_with_browser_budget(page, 500, deadline)
        return False
    fx.wait_with_browser_budget(page, 1_200, deadline)
    return True


def collect_page_workorder_ids(page):
    body_text = fx.safe_inner_text(page.locator("body"), timeout_ms=2_000)
    return set(fx.JSFW_ID_PATTERN.findall(body_text))


def _locate_remote_workorder_table(page):
    pager = page.locator("div.crm-w-pagination:visible")
    pager_count = fx.safe_locator_count(pager)
    hidden_zero_footer = pager_count == 0
    if hidden_zero_footer:
        scope = page.locator("div.crm-w-table.crm-table-nodata:visible")
        table_count = fx.safe_locator_count(scope)
        if table_count == 0:
            return None, pager, hidden_zero_footer
        if table_count != 1:
            raise RuntimeError("远程工单空列表不唯一或尚未加载。")
        pager = scope.locator("div.crm-w-pagination")
    else:
        if pager_count != 1:
            raise RuntimeError("远程工单分页控件不唯一或尚未加载。")
        scope = pager.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' crm-w-table ')][1]")
        if fx.safe_locator_count(scope) != 1:
            raise RuntimeError("远程工单分页对应的列表容器缺失。")
    return scope, pager, hidden_zero_footer


def collect_remote_workorder_rows(page):
    date_field = remote_field("service_date", required=False)
    scope, _pager, _hidden_zero_footer = _locate_remote_workorder_table(page)
    # Isolated grids remain readable; complete scans separately require a pager.
    if scope is None:
        scope = page
    name_cells = scope.locator('div.j-td[data-fieldname="name"][title^="JSFW"]')
    snapshots = name_cells.evaluate_all(
        f"""(nameCells) => {{
            const dateField = {json.dumps(date_field)};
            const cellsByGrid = new Map();
            const allCells = document.querySelectorAll("div.j-td[data-id]");
            for (const cell of allCells) {{
                const grid = cell.closest("div.dt-main") || document;
                if (!cellsByGrid.has(grid)) cellsByGrid.set(grid, new Map());
                const cellsByRecordId = cellsByGrid.get(grid);
                const recordId = (cell.getAttribute("data-id") || "").trim();
                if (!cellsByRecordId.has(recordId)) cellsByRecordId.set(recordId, []);
                cellsByRecordId.get(recordId).push(cell);
            }}
            const readTitleOrText = (cell) => {{
                if (!cell) return "";
                return (cell.getAttribute("title") || cell.innerText || "")
                    .replace(/\\s+/g, " ").trim();
            }};
            return nameCells.map((nameCell) => {{
                const recordId = (nameCell.getAttribute("data-id") || "").trim();
                const rect = nameCell.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0
                    && getComputedStyle(nameCell).visibility !== "hidden";
                const grid = nameCell.closest("div.dt-main") || document;
                const cells = cellsByGrid.get(grid)?.get(recordId) || [];
                const customerCell = cells.find(
                    (cell) => cell.getAttribute("data-fieldname") === "account_id"
                );
                const dateCell = cells.find(
                    (cell) => cell.getAttribute("data-fieldname") === dateField
                );
                return {{
                    visible,
                    workorder_text: readTitleOrText(nameCell),
                    record_id: recordId,
                    customer_name: readTitleOrText(customerCell),
                    service_date: readTitleOrText(dateCell),
                    status_text: cells.map((cell) => cell.innerText || "").join(" ")
                }};
            }});
        }}"""
    )

    rows = []
    for snapshot in snapshots:
        if not snapshot.get("visible"):
            continue
        match = fx.JSFW_ID_PATTERN.search(snapshot.get("workorder_text", ""))
        record_id = (snapshot.get("record_id") or "").strip()
        if not match or not record_id:
            continue
        rows.append(
            {
                "workorder_id": match.group(1),
                "customer_name": fx.normalize_spacing(snapshot.get("customer_name", "")),
                "service_date": fx.normalize_spacing(snapshot.get("service_date", "")),
                "status_text": fx.normalize_spacing(snapshot.get("status_text", "")),
            }
        )
    return rows


def resolve_missing_remote_workorder_dates(page, rows, *, deadline=None):
    fx.remaining_browser_timeout(deadline, 1_000)
    budget = {"deadline": deadline} if deadline is not None else {}
    rows = [dict(row) for row in rows]
    missing = [row for row in rows if not re.match(r"^\d{4}-\d{2}-\d{2}", str(row.get("service_date", "") or ""))]
    if not missing:
        return rows
    targets = [(row, row.get("detail_url") or get_workorder_list_detail_url(page, row["workorder_id"], **budget)) for row in missing]
    detail_page = page.context.new_page()
    try:
        for row, url in targets:
            fx.fast_goto(detail_page, url, **budget)
            text = wait_for_remote_workorder_detail(detail_page, row["workorder_id"], **budget)
            observed_id = fx.extract_detail_field_value(text, "工单编码")
            customer = fx.extract_detail_field_value(text, "客户单位")
            service_date = fx.extract_detail_field_value(text, "服务日期")[:10]
            if observed_id != row["workorder_id"] or not customer or fx.normalize_customer_match_text(customer) != fx.normalize_customer_match_text(row.get("customer_name", "")):
                raise RuntimeError("远程工单详情编号或客户与列表不一致，已停止查重。")
            try:
                date.fromisoformat(service_date)
            except ValueError as exc:
                raise RuntimeError("远程工单详情缺少有效服务日期，无法确认是否重复。") from exc
            row["service_date"] = service_date
    finally:
        detail_page.close()
    return rows


def read_remote_workorder_pagination(page, *, deadline=None):
    table, pager, hidden_zero_footer = _locate_remote_workorder_table(page)
    if table is None:
        raise RuntimeError("远程工单空列表不唯一或尚未加载。")
    if fx.safe_locator_count(pager) != 1:
        raise RuntimeError("远程工单分页控件不唯一或尚未加载。")
    # The table that owns this pager is resolved once, in
    # ``_locate_remote_workorder_table``; re-deriving it here from ``closest``
    # was a second, independently failing ownership proof.
    table_handle = table.element_handle(**({"timeout": fx.remaining_browser_timeout(deadline, 8_000)} if deadline is not None else {}))
    if table_handle is None:
        raise RuntimeError("远程工单分页对应的列表容器缺失。")
    try:
        snapshot = pager.evaluate("""(node, table) => {
            const visible = e => !!e && e.getBoundingClientRect().width > 0
                && e.getBoundingClientRect().height > 0 && getComputedStyle(e).visibility !== 'hidden';
            const disabled = e => e ? e.classList.contains('disable') || e.classList.contains('disabled')
                || e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled') : null;
            if (!table) throw new Error('远程工单分页对应的列表容器缺失');
            return {
                total_text: node.querySelector('.total-num')?.textContent || '',
                page_text: node.querySelector('.page-num')?.textContent || '',
                next_disabled: disabled(node.querySelector('.el-icon-arrow-right')),
                previous_disabled: disabled(node.querySelector('.el-icon-arrow-left')),
                approximate: visible(node.querySelector('.about-num')),
                loading: !!table && (table.getAttribute('aria-busy') === 'true' ||
                    Array.from(table.querySelectorAll('.el-loading-mask, .dt-loading, .loading-mask')).some(visible)),
                empty: !!table?.classList.contains('crm-table-nodata'),
                empty_text: Array.from(table.querySelectorAll('.dt-no-data .no-data-text')).filter(visible)
                    .map(e => e.textContent.trim()).join(''),
                empty_query: table.querySelector('.crm-w-table-search input.dt-ipt')?.value || ''
            };
        }""", table_handle, **({"timeout": fx.remaining_browser_timeout(deadline, 8_000)} if deadline is not None else {}))
    finally:
        table_handle.dispose()
    total = re.fullmatch(r"\s*共\s*(\d+)\s*条\s*", snapshot.pop("total_text"))
    number = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", snapshot.pop("page_text"))
    if not total or not number or any(type(snapshot.get(key)) is not bool for key in ("next_disabled", "previous_disabled", "approximate", "loading", "empty")):
        raise RuntimeError("远程工单分页信息不完整。")
    if hidden_zero_footer and not (
        int(total[1]) == 0 and int(number[1]) in {0, 1} and int(number[2]) in {0, 1}
        and snapshot["empty"] and snapshot["empty_text"] and snapshot["empty_query"]
        and not snapshot["loading"] and not snapshot["approximate"]
    ):
        raise RuntimeError("远程工单隐藏分页缺少完整的空结果证据。")
    if hidden_zero_footer:
        snapshot["hidden_zero_footer"] = True
    return {**snapshot, "total": int(total[1]), "current": int(number[1]), "pages": int(number[2])}


def _wait_remote_workorder_page(page, expected_page, *, previous_ids=None, query="", deadline=None):
    page_deadline = time.monotonic() + REMOTE_WORKORDER_PAGE_TIMEOUT_SECONDS
    if deadline is not None:
        page_deadline = min(page_deadline, deadline)
    last_signature = None
    while True:
        fx.remaining_browser_timeout(deadline, 1_000)
        now = time.monotonic()
        try:
            state = read_remote_workorder_pagination(page, **({"deadline": deadline} if deadline is not None else {}))
            rows = collect_remote_workorder_rows(page)
            ids = frozenset(row.get("workorder_id", "") for row in rows)
            empty = state["total"] == 0 and state["pages"] in {0, 1} and state["current"] in {0, 1} and state["empty"] and not rows
            if state.get("hidden_zero_footer"):
                empty = empty and bool(query) and state.get("empty_query") == query and state.get("empty_text") == f'没有找到"{query}"'
            populated = (
                state["current"] == expected_page and 1 <= state["pages"] <= state["total"]
                and bool(rows) and len(ids) <= state["total"] and not state["empty"]
                and all(row.get("customer_name") and row.get("workorder_id") for row in rows)
                and state["previous_disabled"] == (expected_page == 1)
                and state["next_disabled"] == (expected_page == state["pages"])
                and (previous_ids is None or ids != previous_ids)
            )
            query_matches = not query or not rows or any(
                row["workorder_id"] == query if JSFW_ID_PATTERN.fullmatch(query)
                else fx.normalize_customer_match_text(query) in fx.normalize_customer_match_text(row["customer_name"])
                or fx.normalize_customer_match_text(row["customer_name"]) in fx.normalize_customer_match_text(query)
                for row in rows
            )
            # CRM 全字段搜索是模糊匹配：某客户在 CRM 里没有任何远程工单时，
            # 返回的是别的单位的工单，而不是零结果页。搜索框值等于本次查询词
            # （查询已提交）、单页结果完整、且下方签名连续两轮稳定，足以证明
            # 这就是本次查询的真实结果；下游 find_duplicate_remote_workorder 仍
            # 会逐行严格比对客户身份与服务日期，无匹配行即判定无重复。
            # 查询词未生效时不得放行，避免把旧列表当作查重结果。
            query_applied = bool(query) and state.get("empty_query") == query
            unmatched_single_page = (
                query_applied and not query_matches and not empty
                and state["pages"] == 1 and state["total"] == len(rows)
            )
            ready = (
                (populated or (empty and expected_page == 1))
                and (query_matches or unmatched_single_page)
                and not state["loading"] and not state["approximate"]
            )
            signature = json.dumps([state, rows], sort_keys=True, ensure_ascii=False) if ready else None
            if signature and signature == last_signature and (not empty or now >= page_deadline - REMOTE_WORKORDER_POLL_MS / 1000):
                return state, rows
            last_signature = signature
        except fx.SubmissionEvidenceError:
            raise
        except (RuntimeError, fx.PlaywrightError):
            last_signature = None
        if now >= page_deadline:
            raise RuntimeError(f"远程工单第 {expected_page} 页未完成更新或分页信息不完整，已停止查重。")
        fx.wait_with_browser_budget(page, REMOTE_WORKORDER_POLL_MS, deadline)


def collect_all_remote_workorder_pages(page, query="", *, deadline=None):
    budget = {"deadline": deadline} if deadline is not None else {}
    fx.remaining_browser_timeout(deadline, 1_000)
    state, rows = _wait_remote_workorder_page(page, 1, query=query, **budget)
    expected_total, expected_pages = state["total"], state["pages"]
    if expected_total == 0:
        return []
    results: dict[str, dict[str, Any]] = {}
    page_snapshots = []
    previous_ids: frozenset[str] | None = None
    for number in range(1, expected_pages + 1):
        if number > 1:
            page.locator("div.crm-w-pagination:visible .el-icon-arrow-right").click(timeout=fx.remaining_browser_timeout(deadline, 5_000))
            state, rows = _wait_remote_workorder_page(page, number, previous_ids=previous_ids, **budget)
        if (state["total"], state["pages"]) != (expected_total, expected_pages):
            raise RuntimeError("远程工单分页总数发生变化，已停止查重。")
        page_snapshots.append({row["workorder_id"]: dict(row) for row in rows})
        previous_ids = frozenset(row["workorder_id"] for row in rows)
        current = [{**row, "detail_url": get_workorder_list_detail_url(page, row["workorder_id"], **budget)} for row in rows]
        for row in resolve_missing_remote_workorder_dates(page, current, **budget):
            workorder_id = row["workorder_id"]
            try:
                valid_date = date.fromisoformat(str(row["service_date"])[:10]).isoformat()
            except (ValueError, KeyError) as exc:
                raise RuntimeError("远程工单服务日期无效，已停止查重。") from exc
            row = {**row, "service_date": valid_date}
            if workorder_id in results and results[workorder_id] != row:
                raise RuntimeError("同编号远程工单跨页信息冲突，已停止查重。")
            results[workorder_id] = row
        if len(results) > expected_total:
            raise RuntimeError("远程工单去重数量超过分页总数，已停止查重。")
    if len(results) != expected_total:
        raise RuntimeError("远程工单分页未收集完整，已停止查重。")
    # Recheck every page after detail reads; equal totals do not prove equal records.
    for number in range(expected_pages, 0, -1):
        previous_ids = frozenset(row["workorder_id"] for row in rows)
        if number < expected_pages:
            page.locator("div.crm-w-pagination:visible .el-icon-arrow-left").click(timeout=fx.remaining_browser_timeout(deadline, 5_000))
        state, rows = _wait_remote_workorder_page(page, number, previous_ids=previous_ids if number < expected_pages else None, **budget)
        if (state["total"], state["pages"]) != (expected_total, expected_pages) or {row["workorder_id"]: row for row in rows} != page_snapshots[number - 1]:
            raise RuntimeError("远程工单分页数据发生变化或复核不一致，已停止查重。")
    return list(results.values())


def search_remote_workorder_rows(page, customer_name, *, deadline=None):
    if deadline is not None:
        # A fresh read-only tab avoids spending the shared budget dismissing
        # the submitted form, and leaves its captured result untouched.
        fx.remaining_browser_timeout(deadline, 1_000)
        query_page = page.context.new_page()
        try:
            query_page.goto(fx.FXIAOKE_TECHNICAL_SERVICE_URL, wait_until="commit",
                            timeout=fx.remaining_browser_timeout(deadline, 24_000))
            select_remote_workorder_business_type(query_page, deadline=deadline)
            if clear_remote_workorder_list_filters(query_page, deadline=deadline) is False:
                raise RuntimeError("技术服务列表查询范围不完整，保留待核对状态。")
            fx.select_technical_service_search_field(query_page, "全字段", deadline=deadline)
            search_input = fx.wait_for_first_visible(
                query_page.locator(fx.TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
                timeout_ms=fx.remaining_browser_timeout(deadline, 8_000),
                description="技术服务搜索框", deadline=deadline,
            )
            search_input.fill(customer_name, timeout=fx.remaining_browser_timeout(deadline, 8_000))
            search_input.press("Enter", timeout=fx.remaining_browser_timeout(deadline, 8_000))
            return collect_all_remote_workorder_pages(query_page, query=customer_name, deadline=deadline)
        finally:
            query_page.close()
    reset_remote_workorder_dialogs(page)
    fx.open_technical_service_list(page)
    select_remote_workorder_business_type(page)
    filters_cleared = clear_remote_workorder_list_filters(page)
    fx.select_technical_service_search_field(page, "全字段")
    search_input = fx.wait_for_first_visible(
        page.locator(fx.TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
        timeout_ms=8_000,
        description="技术服务搜索框",
    )
    search_input.fill(customer_name)
    search_input.press("Enter")
    page.wait_for_timeout(1_500)
    if filters_cleared is False:
        raise RuntimeError(
            "技术服务列表仍保留 CRM 固定筛选，查询范围不完整，不能用于判断远程工单不存在。"
        )
    return collect_all_remote_workorder_pages(page, query=customer_name)


def submit_remote_workorder(page, known_ids, milestone_callback=None):
    require_service_owner()
    captured_ids = []

    def capture_detail_history_request(request):
        workorder_id = extract_workorder_id_from_detail_history_request(
            getattr(request, "url", ""),
            getattr(request, "post_data", ""),
        )
        if workorder_id and workorder_id not in known_ids:
            captured_ids.append(workorder_id)

    page.on("request", capture_detail_history_request)
    submit_button = fx.wait_for_first_visible(
        page.locator('span.crm-btn[data-action="submit"]'),
        timeout_ms=8_000,
        description="远程工单提交按钮",
    )
    try:
        if milestone_callback:
            milestone_callback("submit_clicked")
        submit_button.click(force=True)
        deadline = time.time() + REMOTE_WORKORDER_SUBMIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            page.wait_for_timeout(REMOTE_WORKORDER_POLL_MS)
            if captured_ids:
                workorder_id = captured_ids[-1]
                if milestone_callback:
                    milestone_callback(
                        "crm_id_captured",
                        workorder_id=workorder_id,
                    )
                return workorder_id
            body_text = fx.safe_inner_text(page.locator("body"), timeout_ms=1_000)
            current_ids = set(fx.JSFW_ID_PATTERN.findall(body_text))
            new_id = extract_new_workorder_id(known_ids, current_ids)
            if new_id:
                if milestone_callback:
                    milestone_callback(
                        "crm_id_captured",
                        workorder_id=new_id,
                    )
                return new_id
        return ""
    except fx.SubmissionEvidenceError:
        raise
    except Exception as exc:
        raise RemoteWorkorderConfirmationUncertain(
            "远程工单已开始提交但结果未确认；请先只读核对，禁止重复提交。",
            workorder_id=captured_ids[-1] if captured_ids else "",
        ) from exc
    finally:
        try:
            page.remove_listener("request", capture_detail_history_request)
        except (AttributeError, fx.PlaywrightError):
            pass


def submit_remote_workorder_on_authenticated_page(
    page,
    draft,
    input_func=read_brief_input,
    milestone_callback=None,
    report_path=None,
    progress_callback=None,
):
    build_remote_workorder_form_values(draft)

    def stage(label):
        fx.trace_phase(page, f"remote_workorder/{label}")
        if progress_callback:
            progress_callback(f"[远程工单/{draft.customer_name}] {label}")
    stage("选择客户")
    open_new_remote_workorder_form(page)
    try:
        selected_customer = select_customer_for_draft(page, draft, input_func=input_func)
        validate_preselected_customer(draft, selected_customer)
    finally:
        close_remote_workorder_form(page)
    selected_draft = replace(draft, customer_name=selected_customer, selected_customer_name=selected_customer)
    stage("查重")
    remembered_id = find_remembered_remote_workorder_id(report_path, selected_draft)
    if remembered_id:
        try:
            duplicate_rows = search_remote_workorder_rows(page, remembered_id)
            duplicate = verify_remote_workorder_row(duplicate_rows, remembered_id, selected_draft)
        except fx.SubmissionEvidenceError:
            raise
        except Exception as exc:
            raise RemoteWorkorderConfirmationUncertain(
                f"历史编号 {remembered_id} 的实时复核失败：{exc} 请先核对，禁止直接重建。",
                workorder_id=remembered_id,
            ) from exc
    else:
        duplicate_rows = search_remote_workorder_rows(page, selected_customer)
        duplicate = find_duplicate_remote_workorder(duplicate_rows, selected_draft)
    if duplicate:
        if milestone_callback:
            milestone_callback(
                "crm_id_captured",
                workorder_id=duplicate["workorder_id"],
            )
        verify_existing_remote_workorder(page, duplicate, selected_draft)
        return {
            "status": "already_exists",
            "workorder_id": duplicate["workorder_id"],
            "customer_name": selected_customer,
            "message": (
                f"已有同日工单，内容及工时核对一致，无需重复创建。工单：{duplicate['workorder_id']}，"
                f"客户：{selected_customer}，日期：{draft.report_date_iso}。"
            ),
        }

    known_ids = collect_page_workorder_ids(page) | {row["workorder_id"] for row in duplicate_rows}
    stage("填写与校验")
    open_new_remote_workorder_form(page)
    selected_customer = fill_remote_workorder_form(page, replace(draft, selected_customer_name=selected_customer), input_func=input_func)
    validate_preselected_customer(replace(draft, selected_customer_name=selected_draft.customer_name), selected_customer)
    selected_draft = replace(draft, customer_name=selected_customer, selected_customer_name=selected_customer)
    errors = validate_remote_workorder_form_snapshot(
        capture_remote_workorder_form_snapshot(page), selected_draft, selected_customer,
    )
    if errors:
        raise RuntimeError("远程工单提交前字段核验未通过：" + "、".join(errors) + "。")
    stage("提交")
    workorder_id = submit_remote_workorder(
        page,
        known_ids,
        milestone_callback=milestone_callback,
    )
    try:
        stage("回读核验")
        return _verify_created_remote_workorder(page, selected_draft, workorder_id)
    except Exception as exc:
        raise RemoteWorkorderConfirmationUncertain(
            "远程工单已提交，但详情复核未完成；请先只读核对，禁止重复提交。",
            workorder_id=workorder_id,
            differences=getattr(exc, "differences", []),
        ) from exc


def _verify_created_remote_workorder(page, selected_draft, workorder_id):
    draft = selected_draft
    selected_customer = draft.customer_name
    body_text = fx.safe_inner_text(page.locator("body"), timeout_ms=2_000)
    page_confirmed = bool(workorder_id) and submitted_workorder_text_matches(
        body_text,
        workorder_id,
        selected_customer,
        draft.report_date_iso,
    )
    history_error = ""
    verified_row = None
    if not page_confirmed and workorder_id:
        try:
            detail_text = open_recent_workorder_detail(page, workorder_id)
            page_confirmed = submitted_workorder_text_matches(
                detail_text,
                workorder_id,
                selected_customer,
                draft.report_date_iso,
            )
            if not page_confirmed:
                history_error = "最近历史详情未通过编号、客户和服务日期校验"
        except Exception as exc:
            history_error = str(exc)
    if not page_confirmed:
        deadline = time.monotonic() + REMOTE_WORKORDER_VERIFY_RETRY_SECONDS
        attempts = 0
        recovered = None
        while attempts < REMOTE_WORKORDER_VERIFY_ATTEMPTS:
            attempts += 1
            try:
                cold_rows = search_remote_workorder_rows(page, workorder_id or selected_customer, deadline=deadline)
            except fx.SubmissionEvidenceError:
                raise
            except Exception as exc:
                history_error = str(exc)
                recovered = None
            else:
                if workorder_id:
                    exact_rows = [row for row in cold_rows if row.get("workorder_id") == workorder_id]
                    recovered = verify_remote_workorder_row(exact_rows, workorder_id, selected_draft) if exact_rows else None
                else:
                    recovered = find_duplicate_remote_workorder(cold_rows, selected_draft)
                if recovered and (not workorder_id or recovered["workorder_id"] == workorder_id):
                    break
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0 or attempts >= REMOTE_WORKORDER_VERIFY_ATTEMPTS:
                break
            page.wait_for_timeout(min(REMOTE_WORKORDER_VERIFY_RETRY_DELAY_MS, remaining_ms))
            if time.monotonic() >= deadline:
                break
        if not recovered or (workorder_id and recovered["workorder_id"] != workorder_id):
            if workorder_id:
                return {
                    "status": "crm_id_captured",
                    "workorder_id": workorder_id,
                    "customer_name": selected_customer,
                    "message": (
                        f"远程工单已捕获编号 {workorder_id}，但尚未完成详情复核；"
                        f"{history_error or '冷回查未找到匹配工单'}。仅允许继续复核，不允许直接重提。"
                    ),
                }
            raise RuntimeError(
                "远程工单提交后未完成客户、日期和工单类型校验，冷回查也未找到匹配工单。"
            )
        workorder_id = recovered["workorder_id"]
        verified_row = verify_remote_workorder_row([recovered], workorder_id, selected_draft)
    # Every new submission, including Web drafts without preflight, must read
    # persisted business fields before promoting the captured identifier.
    if verified_row is None:
        verified_rows = search_remote_workorder_rows(page, workorder_id)
        verified_row = verify_remote_workorder_row(verified_rows, workorder_id, selected_draft)
    verify_existing_remote_workorder(page, verified_row, selected_draft)
    return {
        "status": "submitted",
        "workorder_id": workorder_id,
        "customer_name": selected_customer,
        "message": (
            f"远程服务工单创建成功。工单：{workorder_id}，客户：{selected_customer}，"
            f"日期：{draft.report_date_iso}。"
        ),
    }


def submit_authorized_remote_workorder_draft(
    draft,
    report_path,
    *,
    authorization_digest,
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    progress_callback=print,
    milestone_callback=None,
):
    if not str(authorization_digest or "").strip():
        raise ValueError("远程工单缺少 Web 授权指纹。")
    resolved_hours = normalize_positive_service_hours(draft.service_hours)
    draft = replace(draft, service_hours=resolved_hours)
    storage_state_path = Path(
        storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH
    ).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    artifact_root = fx.resolve_failure_artifact_root(Path(report_path))
    playwright = browser = context = page = None
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程服务工单/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
        )
        if milestone_callback:
            milestone_callback("browser_started")
        result = submit_authorized_remote_workorder_on_authenticated_page(
            page,
            draft,
            input_func=_reject_worker_prompt,
            milestone_callback=milestone_callback,
            report_path=report_path,
        )
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return result
    except fx.SubmissionEvidenceError as exc:
        fx.retain_submission_failure_trace(page, artifact_root, f"{draft.report_date_iso.replace('-', '')}_remote_workorder_evidence", exc)
        raise
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_remote_workorder_web",
        )
        failure = fx.AutomationFailure(
            str(exc), artifacts=artifacts,
            status=getattr(exc, "status", "error"),
            workorder_id=getattr(exc, "workorder_id", ""),
        )
        setattr(failure, "differences", list(getattr(exc, "differences", [])))
        setattr(failure, "retry_action", getattr(exc, "retry_action", ""))
        raise failure from exc
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def _reject_worker_prompt(prompt):
    raise RuntimeError(
        f"远程工单需要人工选择：{prompt}。请返回 Web 预览处理后重新授权。"
    )


def submit_authorized_remote_workorder_on_authenticated_page(
    page,
    draft,
    *,
    input_func=read_brief_input,
    milestone_callback=None,
    report_path=None,
    progress_callback=None,
):
    draft = replace(
        draft,
        service_hours=normalize_positive_service_hours(draft.service_hours),
    )
    result = submit_remote_workorder_on_authenticated_page(
        page,
        draft,
        input_func=input_func,
        milestone_callback=milestone_callback,
        **({"report_path": report_path} if report_path is not None else {}),
        **({"progress_callback": progress_callback} if progress_callback else {}),
    )
    return {
        "ok": result["status"] in {"submitted", "already_exists"},
        "step": "remote_workorder",
        "status": result["status"],
        "service_customer": draft.customer_name,
        "crm_customer": result["customer_name"],
        "workorder_id": result["workorder_id"],
        **build_remote_workorder_audit_fields(
            draft,
            result["customer_name"],
        ),
        "message": f"纷享销客远程服务工单：{result['message']}",
        "artifacts": [],
    }


def filter_drafts_by_customer(drafts, customer_filter):
    if not (customer_filter or "").strip():
        return drafts
    return [
        draft
        for draft in drafts
        if strict_customer_identity(customer_filter) == strict_customer_identity(draft.customer_name)
    ]


def build_skipped_result(draft):
    return {
        "ok": True,
        "step": "remote_workorder",
        "status": "skipped",
        "service_customer": draft.customer_name,
        "includes_main_work": draft.includes_main_work,
        "message": f"远程服务工单：已跳过客户“{draft.customer_name}”，未提交。",
        "artifacts": [],
    }


def run_remote_workorder_smoke_test(
    report_section,
    report_path,
    is_onsite,
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    customer_filter="",
    input_func=read_brief_input,
    progress_callback=print,
):
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_section, is_onsite=is_onsite),
        customer_filter,
    )
    if not drafts:
        return []
    drafts = [
        prompt_remote_support_hours(draft, input_func=input_func)
        for draft in drafts
    ]

    storage_state_path = Path(storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    artifact_root = fx.resolve_failure_artifact_root(Path(report_path))
    playwright = browser = context = page = None
    results = []
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程工单冒烟检查/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
        )
        for index, draft in enumerate(drafts, start=1):
            form_opened = False
            try:
                if progress_callback:
                    progress_callback(
                        f"[远程工单冒烟检查] 正在验证第 {index}/{len(drafts)} 张：{draft.customer_name}"
                    )
                open_new_remote_workorder_form(page)
                form_opened = True
                selected_customer = fill_remote_workorder_form(
                    page,
                    draft,
                    input_func=input_func,
                    remember_alias=False,
                )
                snapshot = capture_remote_workorder_form_snapshot(page)
                errors = validate_remote_workorder_form_snapshot(
                    snapshot,
                    draft,
                    selected_customer,
                )
                if errors:
                    raise RuntimeError("；".join(errors))
                results.append(
                    {
                        "ok": True,
                        "step": "remote_workorder_smoke_test",
                        "status": "smoke_passed",
                        "service_customer": draft.customer_name,
                        "crm_customer": selected_customer,
                        "snapshot": snapshot,
                        "message": (
                            f"远程服务工单表单冒烟检查通过。客户：{selected_customer}，"
                            f"日期：{draft.report_date_iso}；表单已取消，未提交。"
                        ),
                        "artifacts": [],
                    }
                )
            except Exception as exc:
                artifacts = fx.capture_failure_artifacts(
                    page,
                    artifact_root,
                    f"{draft.report_date_iso.replace('-', '')}_remote_workorder_smoke_{index}",
                )
                results.append(
                    {
                        "ok": False,
                        "step": "remote_workorder_smoke_test",
                        "status": "error",
                        "service_customer": draft.customer_name,
                        "message": f"远程服务工单表单冒烟检查失败：{exc}",
                        "artifacts": artifacts,
                    }
                )
            finally:
                if form_opened:
                    try:
                        close_remote_workorder_form(page)
                    except Exception:
                        fx.fast_goto(page, fx.FXIAOKE_TECHNICAL_SERVICE_URL)
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return results
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{drafts[0].report_date_iso.replace('-', '')}_remote_workorder_smoke_auth",
        )
        return [
            {
                "ok": False,
                "step": "remote_workorder_smoke_test",
                "status": "error",
                "service_customer": draft.customer_name,
                "message": f"远程服务工单表单冒烟检查失败：{exc}",
                "artifacts": artifacts,
            }
            for draft in drafts
        ]
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def apply_expected_completion_status(draft, expected):
    """Override the expected status with an authorized value for read-only recovery.

    A source report marked 进行中 yields no default status, so a verified work
    order the operator already resolved could never pass the strict comparison.
    The override must be one of the confirmed options; anything else keeps
    the record unverifiable instead of trusting whatever CRM currently shows.
    """
    value = str(expected or "").strip()
    if not value:
        raise ValueError("预期完成情况不能为空。")
    statuses = get_remote_workorder_review_options(draft)["completion_statuses"]
    if value not in statuses:
        raise ValueError("预期完成情况不是已确认的 CRM 选项，请先核实 CRM 可选值。")
    return replace(draft, completion_status=value)


def run_remote_workorder_verification(
    report_section,
    report_path,
    is_onsite,
    workorder_id="",
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    customer_filter="",
    progress_callback=print,
    expected_service_hours="",
    expected_completion_status=None,
    includes_main_work=None,
):
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_section, is_onsite=is_onsite),
        customer_filter,
    )
    if isinstance(includes_main_work, bool):
        drafts = [draft for draft in drafts if draft.includes_main_work is includes_main_work]
    if not drafts:
        raise RuntimeError("日报中没有符合条件的远程工单草稿，无法复核。")
    if len(drafts) != 1:
        if len({strict_customer_identity(draft.customer_name) for draft in drafts}) == 1:
            raise RuntimeError("该客户有主服务及附加支持，请用 --remote-role main 或 optional 指定要复核的项目。")
        customers = "、".join(draft.customer_name for draft in drafts)
        raise RuntimeError(
            f"当前日报有多个远程客户：{customers}。请用 --customer 指定要复核的客户。"
        )

    draft = drafts[0]
    if expected_service_hours:
        draft = replace(draft, service_hours=normalize_positive_service_hours(expected_service_hours))
    if expected_completion_status is not None:
        draft = apply_expected_completion_status(draft, expected_completion_status)
    storage_state_path = Path(storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    artifact_root = fx.resolve_failure_artifact_root(Path(report_path))
    playwright = browser = context = page = None
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程工单闭环复核/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
        )
        customer_query = sanitize_remembered_customer_name(
            resolve_customer_primary_name(draft.customer_name)
        ) or draft.customer_name
        search_term = workorder_id or customer_query
        rows = search_remote_workorder_rows(page, search_term)
        if workorder_id:
            matching_row = next(
                (
                    item
                    for item in rows
                    if str(item.get("workorder_id", "")).strip() == workorder_id
                ),
                None,
            )
            try:
                row = verify_remote_workorder_row(rows, workorder_id, draft)
            except RuntimeError as list_error:
                if matching_row is not None and str(
                    matching_row.get("service_date", "") or ""
                ).strip():
                    raise
                try:
                    if matching_row is not None:
                        detail_text = open_workorder_list_detail(page, workorder_id)
                    else:
                        detail_text = open_recent_workorder_detail(page, workorder_id)
                except Exception as history_error:
                    raise RuntimeError(
                        f"{list_error}；历史详情不可用：{history_error}"
                    ) from history_error
                if not submitted_workorder_text_matches(
                    detail_text,
                    workorder_id,
                    customer_query,
                    draft.report_date_iso,
                ):
                    raise RuntimeError(
                        f"{list_error}；最近历史详情也未通过编号、客户和服务日期校验。"
                    ) from list_error
                row = {
                    "workorder_id": workorder_id,
                    "customer_name": str(
                        (matching_row or {}).get("customer_name", "")
                    ).strip()
                    or customer_query,
                    "service_date": draft.report_date_iso,
                    "status_text": "历史详情已核验",
                    "detail_url": getattr(page, "url", ""),
                }
        else:
            row = find_duplicate_remote_workorder(rows, draft)
            if row is None:
                raise RemoteWorkorderNotFound(
                    f"未找到 {draft.report_date_iso}、客户“{draft.customer_name}”的远程服务工单。"
                )
            workorder_id = row["workorder_id"]
        verify_existing_remote_workorder(page, row, draft)
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return {
            "ok": True,
            "step": "remote_workorder_verification",
            "status": "verified",
            "query_completed": True,
            "service_customer": draft.customer_name,
            "crm_customer": row["customer_name"],
            "workorder_id": workorder_id,
            **build_remote_workorder_audit_fields(draft, row["customer_name"]),
            "message": (
                f"远程服务工单身份、内容及工时闭环复核通过。工单：{workorder_id}，"
                f"客户：{row['customer_name']}，日期：{draft.report_date_iso}。"
            ),
            "artifacts": [],
        }
    except RemoteWorkorderNotFound as exc:
        return {
            "ok": True,
            "step": "remote_workorder_verification",
            "status": "not_found",
            "query_completed": True,
            "service_customer": draft.customer_name,
            "workorder_id": workorder_id or None,
            "message": str(exc),
            "artifacts": [],
        }
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_remote_workorder_verify",
        )
        return {
            "ok": False,
            "step": "remote_workorder_verification",
            "status": "unknown",
            "query_completed": False,
            "service_customer": draft.customer_name,
            "workorder_id": workorder_id,
            "retry_action": getattr(exc, "retry_action", ""),
            "differences": getattr(exc, "differences", []),
            "message": f"远程服务工单闭环复核失败：{exc}",
            "artifacts": artifacts,
        }
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def record_remote_workorder_verified_result(report_section, report_path, result, *, pending_snapshot=None):
    """Persist a work order that a read-only verification already confirmed.

    The CRM record is not touched; only the local submission history and the
    pending queue are reconciled, replacing the manual JSON surgery an operator
    otherwise has to perform after ``--verify-workorder`` succeeds.
    """
    workorder_id = str(result.get("workorder_id") or "").strip()
    customer = str(result.get("service_customer") or "").strip()
    crm_customer = str(result.get("crm_customer") or "").strip() or customer
    report_date = str(result.get("report_date") or "").strip()
    if not workorder_id or not customer or not report_date:
        raise RuntimeError("复核结果缺少工单编号、客户或日期，未写入本地确认记录。")
    digest = result.get("content_digest")
    if (
        result.get("ok") is not True or result.get("query_completed") is not True
        or result.get("status") != "verified" or not JSFW_ID_PATTERN.fullmatch(workorder_id)
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or report_date != normalize_report_date_to_iso(report_section)
        or (pending_snapshot is not None and not isinstance(pending_snapshot, str))
    ):
        raise RuntimeError("复核结果或原待办快照不完整，未写入本地确认记录。")
    from daily_report import clear_pending_fxiaoke_tasks, record_fxiaoke_submission_history

    item = {
        **result,
        "ok": True,
        "step": "remote_workorder",
        "status": "submitted",
        "message": (
            f"纷享销客远程服务工单：远程服务工单创建成功。工单：{workorder_id}，"
            f"客户：{crm_customer}，日期：{report_date}。"
        ),
    }
    entry = record_fxiaoke_submission_history(report_section, report_path, item)
    cleared = 0

    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("待办含重复字段，保留歧义证据。")
        return fields

    for raw_line in (pending_snapshot or "").splitlines():
        try:
            pending = json.loads(raw_line, object_pairs_hook=unique_fields)
        except ValueError:
            continue
        if (
            not isinstance(pending, dict) or pending.get("step") != "remote_workorder"
            or pending.get("report_date") != report_date or pending.get("service_customer") != customer
            or pending.get("workorder_id") != workorder_id or pending.get("content_digest") != digest
            or (isinstance(pending.get("includes_main_work"), bool) and pending["includes_main_work"] is not result.get("includes_main_work"))
            or (pending.get("completion_status") and pending["completion_status"] != result.get("completion_status"))
        ):
            continue
        cleared += clear_pending_fxiaoke_tasks(
            Path(report_path), step_key="remote_workorder", report_date=report_date,
            service_customer=customer, expected_line=raw_line, includes_main_work=pending.get("includes_main_work"),
        )
    return entry, cleared


def run_remote_workorder_flow(
    report_section,
    report_path,
    is_onsite,
    storage_state_path=None,
    profile_dir=None,
    show_browser=False,
    allow_ocr_login=False,
    customer_filter="",
    input_func=read_brief_input,
    progress_callback=print,
    milestone_callback=None,
    authorized_drafts=None,
):
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_section, is_onsite=is_onsite),
        customer_filter,
    )
    if not drafts:
        return []

    if authorized_drafts is None:
        authorized = confirm_remote_workorder_drafts(drafts, input_func=input_func)
    else:
        authorized = list(authorized_drafts)
        original = {(item.report_date_iso, item.customer_name, item.includes_main_work): item for item in drafts}
        seen = set()
        for item in authorized:
            key = (item.report_date_iso, item.customer_name, item.includes_main_work)
            source = original.get(key)
            if source is None or source.content != item.content or key in seen:
                raise RuntimeError("预检授权草稿与当前日报内容不一致，请重新预检和确认。")
            build_remote_workorder_form_values(item)
            seen.add(key)
    results = build_skipped_remote_workorder_results(drafts, authorized) if authorized_drafts is None else []
    if not authorized:
        return results

    storage_state_path = Path(storage_state_path or fx.DEFAULT_STORAGE_STATE_PATH).resolve()
    resolved_profile_dir = fx.resolve_profile_dir(profile_dir)
    playwright = browser = context = page = None
    artifact_root = fx.resolve_failure_artifact_root(report_path)
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[远程服务工单/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=resolved_profile_dir,
            **({"cached_login_only": True} if authorized_drafts is not None else {}),
        )
        if milestone_callback:
            milestone_callback("browser_started")
        for index, draft in enumerate(authorized, start=1):
            if authorized_drafts is not None and fx.is_login_page(page):
                raise fx.CachedLoginExpired()
            customer_started = time.monotonic()
            try:
                if progress_callback:
                    progress_callback(
                        f"[远程服务工单] 正在处理第 {index}/{len(authorized)} 张：{draft.customer_name}"
                    )
                result = submit_authorized_remote_workorder_on_authenticated_page(
                    page,
                    draft,
                    input_func=input_func,
                    report_path=report_path,
                    **({"milestone_callback": milestone_callback} if milestone_callback else {}),
                    progress_callback=progress_callback,
                )
                result["elapsed_seconds"] = round(time.monotonic() - customer_started, 3)
                results.append(result)
            except fx.SubmissionEvidenceError:
                raise
            except Exception as exc:
                artifacts = fx.capture_failure_artifacts(
                    page,
                    artifact_root,
                    f"{draft.report_date_iso.replace('-', '')}_remote_workorder_{index}",
                )
                # Only this call site knows whether a submit click could have
                # happened: any exception without CRM evidence here was raised by
                # an earlier stage (customer, lookup, form fill), so the pending
                # queue may retry it instead of demanding manual queue surgery.
                failure_retry_action = getattr(exc, "retry_action", "")
                failure_workorder_id = getattr(exc, "workorder_id", "")
                if failure_retry_action:
                    failure_phase = "crm_id_captured" if failure_workorder_id else "submit_clicked"
                else:
                    failure_retry_action = "safe_retry"
                    failure_phase = "before_submit"
                results.append(
                    {
                        "ok": False,
                        "step": "remote_workorder",
                        "status": getattr(exc, "status", "error"),
                        "workorder_id": failure_workorder_id,
                        "retry_action": failure_retry_action,
                        "submission_phase": failure_phase,
                        "differences": getattr(exc, "differences", []),
                        "elapsed_seconds": round(time.monotonic() - customer_started, 3),
                        "service_customer": draft.customer_name,
                        "includes_main_work": draft.includes_main_work,
                        "message": f"纷享销客远程服务工单：{exc}",
                        "artifacts": artifacts,
                    }
                )
                if authorized_drafts is not None and not isinstance(exc, RemoteCustomerPreflightError):
                    raise
                try:
                    fx.fast_goto(page, fx.FXIAOKE_TECHNICAL_SERVICE_URL)
                except Exception:
                    pass
        fx.try_save_login_state(context, storage_state_path, progress_callback)
        return results
    except fx.SubmissionEvidenceError as exc:
        fx.retain_submission_failure_trace(page, artifact_root, f"{authorized[0].report_date_iso.replace('-', '')}_remote_workorder_evidence", exc)
        raise
    except Exception as exc:
        artifacts = fx.capture_failure_artifacts(
            page,
            artifact_root,
            f"{authorized[0].report_date_iso.replace('-', '')}_remote_workorder_auth",
        )
        processed_customers = {
            (result.get("service_customer"), result.get("includes_main_work")) for result in results
        }
        for draft in authorized:
            if (draft.customer_name, draft.includes_main_work) in processed_customers:
                continue
            results.append(
                {
                    "ok": False,
                    "step": "remote_workorder",
                    "status": "auth_expired" if isinstance(exc, fx.CachedLoginExpired) else "blocked",
                    "retry_action": "safe_retry", "submission_phase": "before_submit",
                    "service_customer": draft.customer_name,
                    "includes_main_work": draft.includes_main_work,
                    "message": f"纷享销客远程服务工单：{exc}",
                    "artifacts": artifacts,
                }
            )
        return results
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def emit_progress(message, json_output=False):
    print(message, file=sys.stderr if json_output else sys.stdout, flush=True)


def main(argv=None):
    configure_stdio()
    args = parse_args(argv)
    report_path = resolve_report_path(args.report_path)
    report_section = fx.find_target_report_section(report_path, args.report_date)
    onsite = is_onsite_workday(report_text=report_section, report_path=report_path)
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_section, is_onsite=onsite),
        args.customer,
    )

    if args.preview_only:
        payload = {
            "ok": True,
            "step": "remote_workorder",
            "status": "preview",
            "drafts": [
                {
                    "report_date": draft.report_date_iso,
                    "customer": draft.customer_name,
                    "includes_main_work": draft.includes_main_work,
                    "service_category": draft.service_category,
                    "service_hours": draft.service_hours or "待输入",
                    "content": draft.content,
                }
                for draft in drafts
            ],
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("\n\n".join(render_draft_preview(draft).rsplit("\n", 1)[0] for draft in drafts) or "没有远程服务工单草稿。")
        return 0

    if args.verify_workorder:
        pending_snapshot = read_pending_queue_text(resolve_fxiaoke_pending_queue_path(report_path)) if args.record_verified else None
        result = run_remote_workorder_verification(
            report_section,
            report_path,
            workorder_id=args.verify_workorder,
            is_onsite=onsite,
            storage_state_path=Path(os.path.expanduser(args.state_path)),
            profile_dir=args.profile_dir,
            show_browser=args.show_browser,
            allow_ocr_login=args.allow_ocr_login,
            customer_filter=args.customer,
            progress_callback=lambda message: emit_progress(message, json_output=args.json_output),
            expected_service_hours=args.expected_service_hours,
            expected_completion_status=args.expected_completion_status,
            **({"includes_main_work": args.remote_role == "main"} if args.remote_role else {}),
        )
        if args.record_verified and result.get("ok") and result.get("status") == "verified":
            entry, cleared = record_remote_workorder_verified_result(
                report_section, report_path, result, pending_snapshot=pending_snapshot,
            )
            result = {**result, "local_history_recorded": True, "pending_cleared": cleared}
            if not args.json_output:
                print(
                    f"已写入本机确认记录（{result.get('workorder_id')}），"
                    f"清除匹配待办 {cleared} 条；未匹配或核验期间变化的待办已保留。CRM 未作任何修改。"
                )
        if args.json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result["message"])
        return 0 if result["ok"] else 1

    if args.smoke_test:
        results = run_remote_workorder_smoke_test(
            report_section,
            report_path,
            is_onsite=onsite,
            storage_state_path=Path(os.path.expanduser(args.state_path)),
            profile_dir=args.profile_dir,
            show_browser=args.show_browser,
            allow_ocr_login=args.allow_ocr_login,
            customer_filter=args.customer,
            input_func=read_brief_input,
            progress_callback=lambda message: emit_progress(message, json_output=args.json_output),
        )
        ok = all(item.get("ok") for item in results)
        payload = {
            "ok": ok,
            "step": "remote_workorder_smoke_test",
            "status": "smoke_passed" if ok else "partial_error",
            "message": "\n".join(item.get("message", "") for item in results),
            "results": results,
            "artifacts": [artifact for item in results for artifact in item.get("artifacts", [])],
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            for item in results:
                print(item["message"])
        return 0 if ok else 1

    intent_path = None
    if drafts:
        existing, intent_path = reserve_fxiaoke_submission_intent(
            report_section, "remote_workorder", fx.resolve_profile_dir(args.profile_dir), report_path=report_path,
        )
        if existing is not None:
            payload = build_fxiaoke_submission_intent_blocked_result("remote_workorder", "远程服务工单", existing)
            print(json.dumps(payload, ensure_ascii=False) if args.json_output else payload["message"])
            return 1
    milestones = {}

    def milestone_callback(event, **fields):
        milestones[event] = True
        milestones.update(fields)

    results = run_remote_workorder_flow(
        report_section,
        report_path,
        is_onsite=onsite,
        storage_state_path=Path(os.path.expanduser(args.state_path)),
        profile_dir=args.profile_dir,
        show_browser=args.show_browser,
        allow_ocr_login=args.allow_ocr_login,
        customer_filter=args.customer,
        input_func=read_brief_input,
        progress_callback=lambda message: emit_progress(message, json_output=args.json_output),
        milestone_callback=milestone_callback,
    )
    stamp_remote_workorder_retry_actions(results, submit_clicked=bool(milestones.get("submit_clicked")))
    has_uncertain_result = any(
        item.get("status") in {"uncertain", "crm_id_captured"}
        for item in results
    )
    payload = {
        "ok": all(item.get("ok") for item in results),
        "step": "remote_workorder",
        "status": (
            "uncertain"
            if has_uncertain_result
            else "submitted"
            if results and all(item.get("ok") for item in results)
            else "partial_error"
        ),
        "results": results,
        "artifacts": [artifact for item in results for artifact in item.get("artifacts", [])],
    }
    finish_fxiaoke_direct_submission(report_section, report_path, "remote_workorder", payload, intent_path)
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in results:
            print(item["message"])
        if payload.get("local_recording_error"):
            print(payload["message"])
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        json_requested = "--json" in sys.argv
        payload = {
            "ok": False,
            "step": "remote_workorder",
            "status": getattr(exc, "status", "error"),
            "retry_action": getattr(exc, "retry_action", ""),
            "message": str(exc),
            "results": [],
            "artifacts": list(getattr(exc, "artifacts", []) or []),
        }
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False))
            raise SystemExit(1)
        raise
