"""Pure policy for an implementation check-in whose report explicitly says after-sales.

Search and browser authorization live outside this module. A lossy customer name,
workorder number date, or last-modified timestamp is never routing evidence.
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .read_only import _date, _field, _option, _parts, _text
from .tenant import tenant_value


IMPLEMENTATION_TYPE = "implementation"
AFTER_SALES_TYPE = "after_sales"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ID = re.compile(r"[a-fA-F0-9]{24}")
# 客户单位（account_id）与关联商机在 CRM 里可能是 32 位无连字符 UUID，
# 与 CasesObj 工单自身的 24 位 _id 不同长。两者都是不透明对象 ID，
# 仍按整串十六进制校验，只放宽长度。
_REF_ID = re.compile(r"[a-fA-F0-9]{24}|[a-fA-F0-9]{32}")
_NUMBER = re.compile(r"JSFW\d{8}-\d+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_TYPE_LABELS = {IMPLEMENTATION_TYPE: "《项目实施工单》", AFTER_SALES_TYPE: "《售后服务工单》"}
_LIFE_LABELS = {"ineffective": "未生效", "under_review": "审核中", "normal": "正常", "in_change": "变更中", "invalid": "作废"}
_WORKFLOW_LABELS = {"in_progress": "进行中", "cancel": "已取消", "error": "异常", "pass": "已完成"}
_EXECUTING_LABELS = {"1": "待预约", "2": "已预约", "4": "服务中", "8": "已服务", "16": "已取消"}
_SNAPSHOT_FIELDS = (
    "workorder_id", "data_id", "customer_id", "customer_name", "record_type",
    "created_at", "life_status", "workflow_status", "executing_state", "project_id", "is_deleted",
)


class RoutingError(RuntimeError):
    """Insufficient or conflicting routing proof; do not choose or submit."""


def onsite_routing_enabled():
    enabled = tenant_value("onsite_routing", "enabled")
    if enabled not in {"", "true", "false"}:
        raise RoutingError("onsite_routing.enabled 需要使用文本 true 或 false。")
    if enabled != "true":
        return False
    routing_schema()
    return True


def routing_schema():
    section = "onsite_routing"
    schema = {key: tenant_value(section, key, required=True, token=True) for key in (
        "implementation_type", "after_sales_type", "project_field",
        "workflow_status_field", "executing_state_field",
    )}
    if len(set(schema.values())) != len(schema):
        raise RoutingError("现场工单路由配置中存在重复标识。")
    return schema


def _enum_value(data, field, labels):
    if not field:
        return None
    value = data.get(field["api_name"])
    if not isinstance(value, str) or value not in labels or _option(data, field) != labels[value]:
        return None
    options = [item for item in field["options"] if isinstance(item, dict) and item.get("value") == value]
    if (len(options) != 1 or options[0].get("not_usable") is True
            or options[0].get("is_active") is False):
        return None
    return value


def _creation_time(raw, field):
    # Reuse the verified field/date contract, then preserve precision lost by
    # its display-oriented second formatting. Integer arithmetic avoids rounding.
    if field.get("date_format") != "yyyy-MM-dd HH:mm:ss" or _date(raw, field) is None:
        return None
    text = str(raw) if type(raw) in (int, str) else ""
    try:
        if re.fullmatch(r"\d{10}|\d{13}", text):
            seconds, milliseconds = divmod(int(text), 1000) if len(text) == 13 else (int(text), 0)
            parsed = datetime.fromtimestamp(seconds, _SHANGHAI) + timedelta(milliseconds=milliseconds)
        else:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_SHANGHAI)
            parsed = parsed.astimezone(_SHANGHAI)
        return parsed.isoformat(timespec="milliseconds" if parsed.microsecond % 1000 == 0 else "microseconds")
    except (ValueError, OverflowError, OSError):
        return None


def routing_values(value):
    """Read a validated CasesObj WebDetail value, or reject the unknown contract.

    This does not send requests. Callers must use WebDetailObserver for response
    provenance and may not treat None as a missing or ineligible workorder.
    """
    data, fields = _parts(value)
    describe = value.get("objectDescribeExt", {}) if isinstance(value, dict) else {}
    if not isinstance(describe, dict):
        return None
    if (data.get("object_describe_api_name") != "CasesObj" or describe.get("api_name") != "CasesObj"
            or data.get("is_deleted") is not False or not _ID.fullmatch(str(data.get("_id", "")))):
        return None
    schema = routing_schema()
    project_field = schema["project_field"]
    workflow_field = schema["workflow_status_field"]
    execution_field = schema["executing_state_field"]
    type_roles = {
        schema["implementation_type"]: IMPLEMENTATION_TYPE,
        schema["after_sales_type"]: AFTER_SALES_TYPE,
    }
    type_labels = {code: _TYPE_LABELS[role] for code, role in type_roles.items()}
    required = (
        ("name", "工单编码", "auto_number"), ("account_id", "客户单位", "object_reference"),
        ("record_type", "业务类型", "record_type"), ("create_time", "创建时间", "date_time"),
        ("life_status", "生命状态", "select_one"), (workflow_field, "工单流程状态", "select_one"),
        (execution_field, "工程师服务状态", "select_one"),
        (project_field, "关联商机", "object_reference"),
    )
    # The verified CRM response omits an empty optional associated opportunity;
    # its schema must still exist, while every identity/status value is required.
    if any(not _field(fields, *item) or (item[0] != project_field and item[0] not in data) for item in required):
        return None
    customer = _text(data.get("account_id__r"))
    customer_id = data.get("account_id")
    project_id = data.get(project_field)
    number = _text(data.get("name"))
    if (not customer or not isinstance(customer_id, str) or not _REF_ID.fullmatch(customer_id)
            or not number or not _NUMBER.fullmatch(number)):
        return None
    if project_id not in (None, "") and (not isinstance(project_id, str) or not _REF_ID.fullmatch(project_id)):
        return None
    observed = {
        "workorder_id": number, "data_id": data["_id"],
        "customer_id": customer_id, "customer_name": customer,
        "record_type": type_roles.get(_enum_value(data, fields["record_type"], type_labels)),
        "created_at": _creation_time(data.get("create_time"), fields["create_time"]),
        "life_status": _enum_value(data, fields["life_status"], _LIFE_LABELS),
        "workflow_status": _enum_value(data, fields[workflow_field], _WORKFLOW_LABELS),
        "executing_state": _enum_value(data, fields[execution_field], _EXECUTING_LABELS),
        "project_id": project_id or "", "is_deleted": False,
    }
    return observed if all(observed[key] is not None for key in _SNAPSHOT_FIELDS) else None


def _instant(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise RoutingError("工单创建时间缺失或不含明确时区，已停止自动选单。")
    try:
        return datetime.fromisoformat(value).astimezone(_SHANGHAI)
    except ValueError as exc:
        raise RoutingError("工单创建时间无效，已停止自动选单。") from exc


def _snapshot(value):
    if not isinstance(value, dict) or any(key not in value for key in _SNAPSHOT_FIELDS):
        raise RoutingError("工单路由信息不完整，已停止自动选单。")
    values = {key: value[key] for key in _SNAPSHOT_FIELDS}
    if (not isinstance(values["workorder_id"], str) or not _NUMBER.fullmatch(values["workorder_id"])
            or not isinstance(values["data_id"], str) or not _ID.fullmatch(values["data_id"])
            or not isinstance(values["customer_id"], str) or not _REF_ID.fullmatch(values["customer_id"])
            or not _text(values["customer_name"]) or type(values["is_deleted"]) is not bool):
        raise RoutingError("工单身份信息无效，已停止自动选单。")
    if values["project_id"] != "" and (not isinstance(values["project_id"], str) or not _REF_ID.fullmatch(values["project_id"])):
        raise RoutingError("工单关联商机无效，已停止自动选单。")
    for field, labels in (("record_type", _TYPE_LABELS), ("life_status", _LIFE_LABELS),
                          ("workflow_status", _WORKFLOW_LABELS), ("executing_state", _EXECUTING_LABELS)):
        if not isinstance(values[field], str) or values[field] not in labels:
            raise RoutingError("工单类型或状态未经确认，已停止自动选单。")
    _instant(values["created_at"])
    return values


def _identity(name):
    # Whitespace/case only: departments, regions and school types remain identity.
    return re.sub(r"\s+", "", name).casefold()


def select_after_sales_workorder(source, candidates, report_date):
    """Return the unique latest eligible after-sales snapshot without mutations."""
    source = _snapshot(source)
    if source["record_type"] != IMPLEMENTATION_TYPE or source["is_deleted"]:
        raise RoutingError("仅签到为有效实施工单时可触发售后自动选单。")
    if not isinstance(report_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise RoutingError("日报服务日期无效。")
    try:
        service_date = date.fromisoformat(report_date)
    except ValueError as exc:
        raise RoutingError("日报服务日期无效。") from exc
    if not isinstance(candidates, (list, tuple)):
        raise RoutingError("工单候选列表不完整。")
    values = [_snapshot(candidate) for candidate in candidates]
    if len({candidate["data_id"] for candidate in values}) != len(values):
        raise RoutingError("工单候选存在重复对象，已停止自动选单。")
    eligible = [candidate for candidate in values if (
        candidate["record_type"] == AFTER_SALES_TYPE
        and candidate["customer_id"] == source["customer_id"]
        and _identity(candidate["customer_name"]) == _identity(source["customer_name"])
        and candidate["is_deleted"] is False
        and candidate["life_status"] == "normal"
        and candidate["workflow_status"] in {"in_progress", "pass"}
        and candidate["executing_state"] in {"1", "2", "4", "8"}
        and _instant(candidate["created_at"]).date() <= service_date
        and (not source["project_id"] or candidate["project_id"] == source["project_id"])
    )]
    if not eligible:
        raise RoutingError("未找到同客户、部门及关联商机的有效售后工单，未提交。")
    if not source["project_id"] and len({candidate["project_id"] for candidate in eligible}) != 1:
        raise RoutingError("候选售后工单涉及多个关联商机，无法自动确定项目，未提交。")
    newest = max(_instant(candidate["created_at"]) for candidate in eligible)
    latest = [candidate for candidate in eligible if _instant(candidate["created_at"]) == newest]
    if len(latest) != 1:
        raise RoutingError("最近创建的售后工单存在并列，无法唯一选单，未提交。")
    return dict(latest[0])


def snapshot_digest(snapshot):
    """Bind every verified identity/time/status/project field, independent of key order."""
    value = _snapshot(snapshot)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assert_target_matches(expected, observed):
    if snapshot_digest(expected) != snapshot_digest(observed):
        raise RoutingError("选定工单的身份、类型、创建时间、状态或关联商机已变化，请重新预检；未提交。")
