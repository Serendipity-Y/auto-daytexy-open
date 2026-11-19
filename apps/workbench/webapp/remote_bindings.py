from __future__ import annotations

import hashlib
import json

from integrations.fxiaoke import service_record as fxiaoke_service_record

from .remote_hours import build_remote_hour_candidates


REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION = (
    "remote_workorder_binding_verification"
)


def find_remote_workorder_binding_candidate(
    day: dict,
    report_section: str,
    content_digest: str,
):
    normalized_digest = str(content_digest or "").strip()
    matches = [
        candidate
        for candidate in build_remote_hour_candidates(day, report_section)
        if candidate.content_digest == normalized_digest
    ]
    if len(matches) != 1:
        raise ValueError("未找到唯一的远程工单记录。")
    return matches[0]


def build_remote_workorder_binding_descriptor(
    day: dict,
    candidate,
    replacement_workorder_id: str,
) -> dict:
    replacement_id = str(replacement_workorder_id or "").strip()
    if not fxiaoke_service_record.JSFW_ID_PATTERN.fullmatch(replacement_id):
        raise ValueError("替换工单必须是有效的 JSFW 编号。")
    original_id = str(candidate.workorder_id or "").strip()
    if not fxiaoke_service_record.JSFW_ID_PATTERN.fullmatch(original_id):
        raise ValueError("当前远程工单缺少有效 JSFW 编号。")
    if replacement_id == original_id:
        raise ValueError("替换工单编号与当前工单相同。")

    report_date = str(day.get("report_date") or "").strip()
    if report_date != str(candidate.report_date or "").strip():
        raise ValueError("日报日期与远程工单记录不一致。")
    payload = {
        "action": REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION,
        "report_date": report_date,
        "content_digest": candidate.content_digest,
        "original_workorder_id": original_id,
        "replacement_workorder_id": replacement_id,
        "report_customer": candidate.report_customer_name,
        "customer": candidate.crm_customer_name,
        "expected_service_date": report_date,
        "show_browser": False,
    }
    unique_key = "remote-workorder-binding:" + _digest_json(payload)
    descriptor = {**payload, "unique_key": unique_key}
    return {
        **descriptor,
        "authorization_digest": _digest_json(descriptor),
    }


def _digest_json(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
