from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from integrations.fxiaoke import remote_workorder as fxiaoke_remote_workorder
from integrations.fxiaoke import service_record as fxiaoke_service_record


REMOTE_WORKORDER_HOURS_SYNC_ACTION = "remote_workorder_hours_sync"


@dataclass(frozen=True, slots=True)
class RemoteHourCandidate:
    report_date: str
    content_digest: str
    report_customer_name: str
    crm_customer_name: str
    workorder_id: str
    confirmed_hours: float | None
    base_service_hours: float | None
    editable: bool
    lock_reason: str
    can_sync: bool
    sync_reason: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.content_digest, self.workorder_id

    @property
    def customer_name(self) -> str:
        return self.crm_customer_name

    @property
    def service_hours(self) -> float | None:
        return self.confirmed_hours


def build_remote_hour_candidates(day: dict, report_section: str) -> list[RemoteHourCandidate]:
    drafts_by_digest: dict[str, list] = {}
    for draft in fxiaoke_remote_workorder.build_remote_workorder_drafts(
        report_section,
        is_onsite=bool(day.get("is_onsite")),
    ):
        digest = hashlib.sha256(draft.content.encode("utf-8")).hexdigest()
        drafts_by_digest.setdefault(digest, []).append(draft)

    candidates: dict[tuple[str, str], RemoteHourCandidate] = {}
    for item in day.get("submissions", []):
        if item.get("step") != "remote_workorder":
            continue
        content_digest = str(item.get("content_digest") or "").strip()
        workorder_id = _normalize_workorder_id(item.get("workorder_id"))
        key = (content_digest, workorder_id)
        draft_matches = drafts_by_digest.get(content_digest, [])
        service_hours = _optional_float(item.get("service_hours"))
        base_service_hours = _optional_float(
            item.get("base_service_hours", item.get("service_hours"))
        )
        candidate = _build_candidate(
            report_date=str(day.get("report_date") or "").strip(),
            content_digest=content_digest,
            workorder_id=workorder_id,
            submission_customer_name=str(
                item.get("crm_customer") or item.get("service_customer") or ""
            ).strip(),
            service_hours=service_hours,
            base_service_hours=base_service_hours,
            draft_matches=draft_matches,
        )
        current = candidates.get(key)
        if current is None or (
            current.base_service_hours is None
            and candidate.base_service_hours is not None
        ):
            candidates[key] = candidate
    return list(candidates.values())


def find_remote_hour_candidate(
    day: dict,
    report_section: str,
    content_digest: str,
    workorder_id: str,
) -> RemoteHourCandidate:
    normalized_digest = str(content_digest or "").strip()
    normalized_workorder_id = _normalize_workorder_id(workorder_id)
    matches = [
        candidate
        for candidate in build_remote_hour_candidates(day, report_section)
        if candidate.identity == (normalized_digest, normalized_workorder_id)
    ]
    if len(matches) != 1:
        raise ValueError("未找到唯一的远程工单记录。")
    return matches[0]


def build_remote_hour_sync_descriptor(
    candidate: RemoteHourCandidate,
    target_hours,
) -> dict:
    if not candidate.editable or not candidate.can_sync:
        raise ValueError(candidate.sync_reason or candidate.lock_reason or "该远程工单不可同步 CRM")
    if not candidate.workorder_id:
        raise ValueError("缺少有效 JSFW 编号，不能同步 CRM")
    if candidate.confirmed_hours is None:
        raise ValueError("缺少原工时，不能安全同步 CRM")
    normalized_target_hours = (
        fxiaoke_remote_workorder.normalize_editable_remote_support_hours(target_hours)
    )
    expected_hours = fxiaoke_remote_workorder.normalize_positive_service_hours(
        candidate.confirmed_hours
    )
    if normalized_target_hours == expected_hours:
        raise ValueError("目标工时与 CRM 已确认工时相同，没有变化。")
    payload = {
        "action": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
        "report_date": candidate.report_date,
        "customer": candidate.crm_customer_name,
        "report_customer": candidate.report_customer_name,
        "content_digest": candidate.content_digest,
        "workorder_id": candidate.workorder_id,
        "expected_hours": expected_hours,
        "target_hours": normalized_target_hours,
        "show_browser": False,
    }
    unique_key = "remote-hours-sync:" + _digest_json(payload)
    descriptor = {**payload, "unique_key": unique_key}
    return {
        **descriptor,
        "authorization_digest": _digest_json(descriptor),
    }


def _build_candidate(
    *,
    report_date: str,
    content_digest: str,
    workorder_id: str,
    submission_customer_name: str,
    service_hours: float | None,
    base_service_hours: float | None,
    draft_matches: list,
) -> RemoteHourCandidate:
    if not content_digest or len(draft_matches) != 1:
        return RemoteHourCandidate(
            report_date=report_date,
            content_digest=content_digest,
            report_customer_name=submission_customer_name,
            crm_customer_name=submission_customer_name,
            workorder_id=workorder_id,
            confirmed_hours=service_hours,
            base_service_hours=base_service_hours,
            editable=False,
            lock_reason="无法确认属于远程支持，已锁定",
            can_sync=False,
            sync_reason="无法确认工单来源",
        )

    draft = draft_matches[0]
    report_customer_name = str(draft.customer_name or "").strip()
    remembered_customer = str(
        fxiaoke_service_record.resolve_customer_primary_name(report_customer_name) or ""
    ).strip()
    crm_customer_name = (
        remembered_customer
        if remembered_customer and remembered_customer != report_customer_name
        else submission_customer_name or report_customer_name
    )
    if not workorder_id:
        return RemoteHourCandidate(
            report_date=report_date,
            content_digest=content_digest,
            report_customer_name=report_customer_name,
            crm_customer_name=crm_customer_name,
            workorder_id="",
            confirmed_hours=service_hours,
            base_service_hours=base_service_hours,
            editable=False,
            lock_reason="缺少有效 JSFW 编号，已锁定",
            can_sync=False,
            sync_reason="缺少有效 JSFW 编号，不能同步 CRM",
        )

    if service_hours is None:
        return RemoteHourCandidate(
            report_date=report_date,
            content_digest=content_digest,
            report_customer_name=report_customer_name,
            crm_customer_name=crm_customer_name,
            workorder_id=workorder_id,
            confirmed_hours=None,
            base_service_hours=None,
            editable=False,
            lock_reason="缺少 CRM 已确认原工时，已锁定",
            can_sync=False,
            sync_reason="缺少原工时，不能安全同步 CRM",
        )

    return RemoteHourCandidate(
        report_date=report_date,
        content_digest=content_digest,
        report_customer_name=report_customer_name,
        crm_customer_name=crm_customer_name,
        workorder_id=workorder_id,
        confirmed_hours=service_hours,
        base_service_hours=service_hours,
        editable=True,
        lock_reason="",
        can_sync=True,
        sync_reason="",
    )


def _normalize_workorder_id(value) -> str:
    workorder_id = str(value or "").strip()
    return (
        workorder_id
        if fxiaoke_service_record.JSFW_ID_PATTERN.fullmatch(workorder_id)
        else ""
    )


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _digest_json(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
