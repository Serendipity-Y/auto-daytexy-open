from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from integrations.fxiaoke import daily_log as fxiaoke_daily_log
from integrations.fxiaoke import remote_workorder as fxiaoke_remote_workorder
from integrations.fxiaoke import service_record as fxiaoke_service_record
from packages.report_domain.report_format import (
    find_saved_report_section,
    normalize_report_date_to_iso,
    parse_report_fields,
    parse_report_service_entries,
)
from packages.report_domain.workday import (
    REMOTE_MAIN_SERVICE_START_DATE,
    is_onsite_workday,
)

DAILY_LOG_ACTION = "daily_log"
ONSITE_SERVICE_ACTION = "onsite_service"
REMOTE_WORKORDER_ACTION = "remote_workorder"
SUPPORTED_ACTIONS = frozenset(
    {DAILY_LOG_ACTION, ONSITE_SERVICE_ACTION, REMOTE_WORKORDER_ACTION}
)


@dataclass(frozen=True, slots=True)
class ActionPreview:
    action: str
    report_date: str
    customer: str
    hours: str | None
    content: str
    content_digest: str
    authorization_digest: str
    payload: Mapping[str, Any]

    @property
    def unique_key(self) -> str:
        return _digest_json(
            {
                "action": self.action,
                "report_date": self.report_date,
                "customer": self.customer,
                "content_digest": self.content_digest,
            }
        )


def build_action_previews(
    report_path,
    report_date,
    action,
    *,
    hours_by_customer: Mapping[str, object] | None = None,
    remote_options_by_customer: Mapping[str, Mapping[str, object]] | None = None,
    includes_main_work: bool | None = None,
) -> list[ActionPreview]:
    normalized_action = str(action or "").strip()
    if normalized_action not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported action: {normalized_action}")

    source_path, report_section = _load_report_section(report_path, report_date)

    if normalized_action == DAILY_LOG_ACTION:
        return [_build_daily_log_preview(report_section)]

    onsite = is_onsite_workday(
        report_text=report_section,
        report_path=source_path,
    )
    if normalized_action == ONSITE_SERVICE_ACTION:
        if not onsite:
            raise ValueError("非外勤日报不能生成外勤技术服务预览")
        return _build_onsite_service_previews(report_section, report_path=source_path)

    return _build_remote_workorder_previews(
        report_section,
        onsite=onsite,
        hours_by_customer=hours_by_customer,
        remote_options_by_customer=remote_options_by_customer,
        includes_main_work=includes_main_work,
    )


def build_source_sync_previews(report_path, report_date) -> tuple[list[ActionPreview], list[ActionPreview], str]:
    source_path, report_section = _load_report_section(report_path, report_date)
    onsite = is_onsite_workday(report_text=report_section, report_path=source_path)
    requires_remote_main = not onsite and normalize_report_date_to_iso(report_section) >= REMOTE_MAIN_SERVICE_START_DATE
    required = [_build_daily_log_preview(report_section)]
    if onsite or requires_remote_main:
        required.extend(_build_required_main_previews(report_section, source_path, onsite=onsite))
    try:
        optional = _build_remote_workorder_previews(
            report_section, onsite=onsite, includes_main_work=False if onsite or requires_remote_main else None,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return required, [], f"可选远程工单预览失败，已跳过；不影响日报与主服务：{exc}"
    return required, optional, ""


def _build_required_main_previews(report_section: str, source_path: Path, *, onsite: bool) -> list[ActionPreview]:
    entries = parse_report_service_entries(report_section) or [parse_report_fields(report_section)]
    customers = []
    for entry in entries:
        customer = entry.get("服务客户", "").strip()
        if not customer:
            continue
        if not entry.get("处理情况", "").strip(" \t\r\n；;。."):
            raise ValueError(f"“{customer}”缺少主服务的处理情况，请补全日报后再同步。")
        customers.append(customer)

    if onsite:
        previews = _build_onsite_service_previews(report_section, report_path=source_path)
    else:
        previews = _build_remote_workorder_previews(report_section, onsite=False, includes_main_work=True)

    def customer_key(customer: str) -> str:
        primary = customer if onsite else fxiaoke_remote_workorder.resolve_customer_primary_name(customer)
        return fxiaoke_remote_workorder.strict_customer_identity(primary)

    present = {customer_key(preview.customer) for preview in previews}
    missing = list(dict.fromkeys(customer for customer in customers if customer_key(customer) not in present))
    if missing or not previews:
        label = "、".join(missing) or "该日报"
        raise ValueError(f"“{label}”缺少主服务预览，请核对源文件后再同步。")
    return previews


def _load_report_section(report_path, report_date) -> tuple[Path, str]:
    source_path = Path(report_path).expanduser()
    report_section = find_saved_report_section(source_path, report_date)
    if normalize_report_date_to_iso(report_section) != str(report_date or "").strip().replace(".", "-"):
        raise ValueError("日报日期与预览日期不一致")
    return source_path, report_section


def authorize_preview(
    store,
    preview: ActionPreview,
    token: str,
    *,
    retry_of_job_id: str | None = None,
    show_browser: bool = False,
) -> str:
    submitted_digest = str(token or "").strip()
    if not secrets.compare_digest(preview.authorization_digest.encode("utf-8"), submitted_digest.encode("utf-8")):
        raise ValueError("预览授权指纹不匹配，请重新生成预览")
    _validate_authorizable_preview(preview)

    request_payload = {
        "unique_key": preview.unique_key,
        "action": preview.action,
        "report_date": preview.report_date,
        "customer": preview.customer,
        "hours": preview.hours,
        "content_digest": preview.content_digest,
        "payload": _thaw(preview.payload),
        "show_browser": bool(show_browser),
    }
    return store.create_or_get_queued_job(
        {
            "job_key": preview.unique_key,
            "action": preview.action,
            "authorized_digest": preview.authorization_digest,
            "request_payload": request_payload,
            "retry_of_job_id": retry_of_job_id,
        }
    )


def _validate_authorizable_preview(preview: ActionPreview) -> None:
    if preview.action == REMOTE_WORKORDER_ACTION:
        if preview.hours is None:
            raise ValueError("纯远程支持必须填写服务工时后才能授权")
        fxiaoke_remote_workorder.normalize_positive_service_hours(preview.hours)
        if not preview.payload.get("completion_status"):
            raise ValueError("请先核对处理状态；进行中的工作不能自动标记为已解决")


def _build_daily_log_preview(report_section: str) -> ActionPreview:
    draft = fxiaoke_daily_log.build_daily_log_draft(report_section)
    content = f"{draft.work_summary}\n\n{draft.tomorrow_plan}".strip()
    payload = daily_log_preview_payload(draft)
    return _make_preview(
        action=DAILY_LOG_ACTION,
        report_date=draft.report_date_iso,
        customer=draft.customer_name,
        hours=None,
        content=content,
        payload=payload,
    )


def daily_log_preview_payload(draft) -> dict:
    return {
        "report_date": draft.report_date_iso,
        "customer": draft.customer_name,
        "work_summary": draft.work_summary,
        "tomorrow_plan": draft.tomorrow_plan,
        "reflection": draft.reflection,
    }


def _build_onsite_service_previews(report_section: str, *, report_path=None) -> list[ActionPreview]:
    previews = []
    for draft in fxiaoke_service_record.build_service_record_drafts(
        report_section,
        main_only=True,
        report_path=report_path,
    ):
        payload = onsite_service_preview_payload(draft)
        previews.append(
            _make_preview(
                action=ONSITE_SERVICE_ACTION,
                report_date=draft.report_date_iso,
                customer=draft.customer_name,
                hours=None,
                content=draft.service_content,
                payload=payload,
            )
        )
    return previews


def onsite_service_preview_payload(draft) -> dict:
    payload = {
        "report_date": draft.report_date_iso,
        "customer": draft.customer_name,
        "process_text": draft.process_text,
        "next_plan": draft.next_plan,
        "record_kind": draft.record_kind,
    }
    work_type = str(getattr(draft, "work_type", "") or "").strip()
    if work_type:
        payload["work_type"] = work_type
        if work_type == "售后":
            payload["routing_policy"] = "implementation_to_latest_after_sales_v1"
    routing_target = getattr(draft, "routing_target", None)
    if routing_target is not None:
        if not isinstance(routing_target, Mapping):
            raise ValueError("外勤关联工单快照必须是对象")
        payload["routing_target"] = _thaw(routing_target)
    source = str(getattr(draft, "source_checkin_workorder_id", "") or "").strip()
    if source:
        payload["source_checkin_workorder_id"] = source
    routing_source = getattr(draft, "routing_source", None)
    if routing_source is not None:
        if not isinstance(routing_source, Mapping):
            raise ValueError("签到工单快照必须是对象")
        payload["routing_source"] = _thaw(routing_source)
    return payload


def _build_remote_workorder_previews(
    report_section: str,
    *,
    onsite: bool,
    hours_by_customer: Mapping[str, object] | None = None,
    remote_options_by_customer: Mapping[str, Mapping[str, object]] | None = None,
    includes_main_work: bool | None = None,
) -> list[ActionPreview]:
    normalized_hours = _normalize_hours_mapping(hours_by_customer)
    options_by_customer = _normalize_remote_options(remote_options_by_customer)
    previews = []
    for draft in fxiaoke_remote_workorder.build_remote_workorder_drafts(
        report_section,
        is_onsite=onsite,
        **({"includes_main_work": includes_main_work} if includes_main_work is not None else {}),
    ):
        customer_key = fxiaoke_remote_workorder.strict_customer_identity(draft.customer_name)
        options = options_by_customer.get(customer_key, {})
        if options:
            draft = fxiaoke_remote_workorder.apply_remote_workorder_review_options(
                draft, **options,
            )
        hours = draft.service_hours or None
        if customer_key in normalized_hours:
            hours = fxiaoke_remote_workorder.normalize_positive_service_hours(
                normalized_hours[customer_key]
            )

        payload = {
            "report_date": draft.report_date_iso,
            "customer": draft.customer_name,
            "content_items": list(draft.content_items),
            "service_category": draft.service_category,
            "service_category_value": draft.service_category_value,
            "service_hours": hours,
            "includes_main_work": bool(draft.includes_main_work),
            "completion_status": draft.completion_status,
            "source_completion_status": draft.source_completion_status,
            "selected_customer_name": draft.selected_customer_name,
        }
        previews.append(
            _make_preview(
                action=REMOTE_WORKORDER_ACTION,
                report_date=draft.report_date_iso,
                customer=draft.customer_name,
                hours=hours,
                content=draft.content,
                payload=payload,
            )
        )
    return previews


def review_source_sync_preview(preview: ActionPreview, selection: dict) -> ActionPreview:
    if preview.action != REMOTE_WORKORDER_ACTION:
        return preview
    hours = fxiaoke_remote_workorder.normalize_editable_remote_support_hours(selection.get("hours"))
    payload = _thaw(preview.payload)
    draft = fxiaoke_remote_workorder.deserialize_remote_workorder_draft({
        **payload, "report_date_iso": preview.report_date, "customer_name": preview.customer,
    })
    draft = fxiaoke_remote_workorder.apply_remote_workorder_review_options(
        draft,
        service_category=selection.get("service_category"),
        completion_status=selection.get("completion_status"),
    )
    payload.update(
        service_hours=hours,
        service_category=draft.service_category,
        service_category_value=draft.service_category_value,
        completion_status=draft.completion_status,
    )
    reviewed = _make_preview(
        action=preview.action, report_date=preview.report_date, customer=preview.customer,
        hours=hours, content=preview.content, payload=payload,
    )
    _validate_authorizable_preview(reviewed)
    return reviewed


def _normalize_remote_options(options_by_customer):
    if options_by_customer is None:
        return {}
    if not isinstance(options_by_customer, Mapping):
        raise ValueError("remote_options_by_customer must be an object")
    normalized = {}
    for customer, options in options_by_customer.items():
        if not isinstance(options, Mapping) or set(options) - {"service_category", "completion_status"}:
            raise ValueError("仅允许修改服务类别和处理状态")
        key = fxiaoke_remote_workorder.strict_customer_identity(str(customer))
        if not key:
            raise ValueError("修改服务类别和处理状态时必须指定客户")
        normalized[key] = {str(name): str(value or "").strip() for name, value in options.items()}
    return normalized


def remote_options_from_payload(customer: str, payload) -> dict | None:
    if not isinstance(payload, Mapping):
        return None
    options = {
        key: payload[key] for key in ("service_category", "completion_status") if key in payload
    }
    return {customer: options} if options else None


def _make_preview(
    *,
    action: str,
    report_date: str,
    customer: str,
    hours: str | None,
    content: str,
    payload: dict[str, Any],
) -> ActionPreview:
    normalized_content = str(content or "").strip()
    if not normalized_content:
        raise ValueError("预览内容不能为空")
    content_digest = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    authorization_payload = {
        "action": action,
        "report_date": report_date,
        "customer": customer,
        "hours": hours,
        "content_digest": content_digest,
        "payload": payload,
    }
    return ActionPreview(
        action=action,
        report_date=report_date,
        customer=customer,
        hours=hours,
        content=normalized_content,
        content_digest=content_digest,
        authorization_digest=_digest_json(authorization_payload),
        payload=_freeze(payload),
    )


def _normalize_hours_mapping(
    hours_by_customer: Mapping[str, object] | None,
) -> dict[str, object]:
    if hours_by_customer is None:
        return {}
    if not isinstance(hours_by_customer, Mapping):
        raise ValueError("hours_by_customer must be an object")
    return {
        fxiaoke_remote_workorder.strict_customer_identity(str(customer)): value
        for customer, value in hours_by_customer.items()
        if str(customer).strip()
    }


def _digest_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        _thaw(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value
