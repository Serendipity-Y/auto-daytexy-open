from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from integrations.fxiaoke import daily_log as fxiaoke_daily_log
from integrations.fxiaoke import remote_workorder as fxiaoke_remote_workorder
from integrations.fxiaoke import service_record as fxiaoke_service_record
from packages.report_domain.report_format import find_saved_report_section
from packages.report_domain.workday import is_onsite_workday
from integrations.fxiaoke.onsite_routing import select_after_sales_workorder
from .previews import build_action_previews, ONSITE_SERVICE_ACTION


VERIFICATION_ACTION = "crm_verification"
VERIFICATION_STEPS = ("daily_log", "service_record", "remote_workorder")
ROUTING_FIELDS = ("work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source")


def build_verification_jobs(report_date: str, *, day=None, steps=VERIFICATION_STEPS, source_jobs=()) -> list[dict]:
    normalized_date = datetime.strptime(str(report_date), "%Y-%m-%d").strftime(
        "%Y-%m-%d"
    )
    day = day or {}
    customer = str(day.get("customer_name", "")).strip()
    submissions = list(day.get("submissions", []) or [])
    descriptors = []
    for step in VERIFICATION_STEPS:
        if step not in steps:
            continue
        candidates = [item for item in submissions if item.get("step") == step]
        selected = (
            _remote_verification_candidates(candidates, customer)
            if step == "remote_workorder"
            else [_prefer_known_id(candidates)]
        )
        for candidate in selected:
            if step == "service_record":
                candidate = _onsite_frozen_candidate(normalized_date, day, candidate, source_jobs)
            descriptors.append(_build_verification_descriptor(normalized_date, step, candidate, customer))
    return descriptors


def _onsite_frozen_candidate(report_date, day, candidate, source_jobs):
    routed = [job for job in source_jobs if job.get("action") == ONSITE_SERVICE_ACTION
              and job.get("request_payload", {}).get("report_date") == report_date
              and (job.get("result_payload", {}).get("routing_policy")
                   or job.get("result_payload", {}).get("routing_target")
                   or job.get("request_payload", {}).get("payload", {}).get("routing_policy"))]
    if not routed:
        return candidate
    customer = str((candidate or {}).get("service_customer") or day.get("customer_name") or "").strip()
    previews = [preview for preview in build_action_previews(day["report_path"], report_date, ONSITE_SERVICE_ACTION)
                if preview.customer == customer]
    if len(previews) != 1:
        raise ValueError("原始外勤客户或日报内容已变化，无法恢复冻结复核依据。")
    preview = previews[0]
    snapshots = []
    record_ids = {str(candidate["service_record_id"])} if candidate and candidate.get("service_record_id") else set()
    for job in routed:
        request = job["request_payload"]
        evidence = job.get("result_payload", {})
        if (request.get("customer") != customer or request.get("content_digest") != preview.content_digest
                or job.get("authorized_digest") != preview.authorization_digest
                or request.get("payload") != dict(preview.payload)):
            raise ValueError("原外勤任务的客户、正文或路由授权与当前日报不一致，停止复核。")
        if evidence.get("routing_conflict"):
            raise ValueError("原任务存在关联工单冲突，需先核对原始证据。")
        routing = {key: deepcopy(evidence.get(key)) for key in ROUTING_FIELDS}
        target = routing["routing_target"]
        source = routing["routing_source"]
        if (routing["work_type"] != "售后" or routing["routing_policy"] != preview.payload.get("routing_policy")
                or not isinstance(target, dict) or not isinstance(source, dict)
                or source.get("workorder_id") != routing["source_checkin_workorder_id"]):
            raise ValueError("原始冻结关联目标或签到依据不完整，禁止回退到其他工单。")
        select_after_sales_workorder(source, [target], report_date)
        for receipt in (candidate or {}, evidence):
            if receipt.get("workorder_id") and receipt["workorder_id"] != target["workorder_id"]:
                raise ValueError("已知回执与原始冻结目标不一致，停止复核。")
            if receipt.get("service_record_id"):
                record_ids.add(str(receipt["service_record_id"]))
        snapshots.append(routing)
    if len({_digest(snapshot) for snapshot in snapshots}) != 1 or len(record_ids) > 1:
        raise ValueError("存在多个冻结工单或服务记录，不能自动选择最新记录。")
    return {
        **(candidate or {}), **snapshots[0], "service_customer": customer,
        "workorder_id": snapshots[0]["routing_target"]["workorder_id"],
        "service_record_id": next(iter(record_ids), None), "content_digest": preview.content_digest,
        "original_authorization_digest": preview.authorization_digest,
    }


def _build_verification_descriptor(normalized_date, step, candidate, customer):
    descriptor: dict[str, Any] = {
        "action": VERIFICATION_ACTION,
        "report_date": normalized_date,
        "step": step,
        "customer": str((candidate or {}).get("service_customer") or customer).strip(),
        "workorder_id": str((candidate or {}).get("workorder_id") or "").strip() or None,
        "service_record_id": str((candidate or {}).get("service_record_id") or "").strip() or None,
        "content_digest": str((candidate or {}).get("content_digest") or "").strip(),
        "expected_service_hours": str((candidate or {}).get("service_hours") or "").strip() if step == "remote_workorder" else "",
        "payload": {},
    }
    if candidate and candidate.get("verification_conflict"):
        descriptor["verification_conflict"] = candidate["verification_conflict"]
    if candidate and candidate.get("routing_target"):
        descriptor.update({key: deepcopy(candidate[key]) for key in (*ROUTING_FIELDS, "original_authorization_digest")})
    descriptor["unique_key"] = _digest(
        {
            key: descriptor[key]
            for key in (
                "action",
                "report_date",
                "step",
                "customer",
                "workorder_id",
                "service_record_id",
                "expected_service_hours",
            )
        }
    )
    descriptor["authorization_digest"] = verification_authorization_digest(
        descriptor
    )
    if descriptor.get("routing_target"):
        descriptor["unique_key"] = _digest({"identity": descriptor["unique_key"],
                                            "routing": {key: descriptor[key] for key in ROUTING_FIELDS},
                                            "original_authorization_digest": descriptor["original_authorization_digest"]})
        descriptor["authorization_digest"] = verification_authorization_digest(descriptor)
    if step == "remote_workorder":
        if isinstance((candidate or {}).get("includes_main_work"), bool):
            descriptor["includes_main_work"] = candidate["includes_main_work"]
        descriptor["unique_key"] = _digest({
            "identity": descriptor["unique_key"],
            "content_digest": descriptor["content_digest"],
            "verification_conflict": descriptor.get("verification_conflict", ""),
            **({"includes_main_work": descriptor["includes_main_work"]} if "includes_main_work" in descriptor else {}),
        })
        descriptor["authorization_digest"] = verification_authorization_digest(descriptor)
    return descriptor

def _remote_verification_candidates(candidates, default_customer=""):
    candidates = [
        {**item, "service_customer": str(item.get("service_customer") or default_customer).strip()}
        for item in candidates
    ]
    known_customers = {
        (str(item.get("service_customer") or "").strip(), item.get("includes_main_work"))
        for item in candidates if str(item.get("workorder_id") or "").strip()
    }
    grouped: dict[tuple[str, str, bool | None], list[dict]] = {}
    for item in candidates:
        customer = str(item.get("service_customer") or "").strip()
        workorder_id = str(item.get("workorder_id") or "").strip()
        role = item.get("includes_main_work")
        if not workorder_id and (customer, role) in known_customers:
            continue
        grouped.setdefault((customer, workorder_id, role), []).append(item)
    selected = []
    for records in grouped.values():
        candidate = dict(records[0])
        hours = set()
        invalid_hours = False
        for record in records:
            value = str(record.get("service_hours") or "").strip()
            try:
                hours.add(fxiaoke_remote_workorder.normalize_positive_service_hours(value) if value else "")
            except ValueError:
                invalid_hours = True
        digests = {str(item.get("content_digest") or "").strip() for item in records}
        if invalid_hours or len(hours) != 1 or len(digests) != 1:
            candidate["verification_conflict"] = "来源记录的服务工时或正文摘要不一致或无效，请先核对后再校验 CRM。"
        elif hours:
            candidate["service_hours"] = next(iter(hours))
        selected.append(candidate)
    return selected or [None]


def verification_authorization_digest(descriptor: dict) -> str:
    controlled = {
        key: descriptor.get(key)
        for key in (
            "action",
            "report_date",
            "step",
            "customer",
            "workorder_id",
            "service_record_id",
            "content_digest",
            "expected_service_hours",
            "unique_key",
        )
    }
    if descriptor.get("verification_conflict"):
        controlled["verification_conflict"] = descriptor["verification_conflict"]
    if "includes_main_work" in descriptor:
        controlled["includes_main_work"] = descriptor["includes_main_work"]
    controlled.update({key: descriptor[key] for key in (*ROUTING_FIELDS, "original_authorization_digest") if key in descriptor})
    return _digest(controlled)


def verify_daily_log(report_path, report_date, **options) -> dict:
    path = Path(report_path)
    section = find_saved_report_section(path, report_date)
    draft = fxiaoke_daily_log.build_daily_log_draft(section)
    expected_customer = str(options.get("customer") or "").strip()
    if expected_customer and draft.customer_name != expected_customer:
        raise RuntimeError("日报客户与指定复核客户不一致。")
    raw = fxiaoke_daily_log.run_daily_log_verification(
        draft,
        storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
        report_path=path,
        show_browser=bool(options.get("show_browser", False)),
        progress_callback=options.get("progress_callback"),
        allow_ocr_login=False,
        profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
    )
    return _normalize_result(
        raw,
        step="daily_log",
        report_date=report_date,
        customer=draft.customer_name,
    )


def verify_onsite_service(
    report_path,
    report_date,
    *,
    customer="",
    workorder_id="",
    service_record_id="",
    **options,
) -> dict:
    path = Path(report_path)
    section = find_saved_report_section(path, report_date)
    drafts = fxiaoke_service_record.build_service_record_drafts(section, main_only=True, report_path=path)
    draft = _select_customer_draft(drafts, customer)
    routing_options = {
        key: options[key]
        for key in ("routing_target", "source_checkin_workorder_id", "routing_source")
        if options.get(key) not in (None, "")
    }
    raw = fxiaoke_service_record.run_service_record_verification(
        draft,
        storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
        report_path=path,
        workorder_id=workorder_id or "",
        service_record_id=service_record_id or "",
        show_browser=bool(options.get("show_browser", False)),
        progress_callback=options.get("progress_callback"),
        allow_ocr_login=False,
        profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
        **routing_options,
    )
    return _normalize_result(
        raw,
        step="service_record",
        report_date=report_date,
        customer=draft.customer_name,
    )


def verify_remote_workorder(
    report_path,
    report_date,
    *,
    customer="",
    workorder_id="",
    **options,
) -> dict:
    path = Path(report_path)
    section = find_saved_report_section(path, report_date)
    onsite = is_onsite_workday(report_text=section, report_path=path)
    raw = fxiaoke_remote_workorder.run_remote_workorder_verification(
        section,
        path,
        workorder_id=workorder_id or "",
        is_onsite=onsite,
        storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
        profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
        show_browser=bool(options.get("show_browser", False)),
        allow_ocr_login=False,
        customer_filter=customer,
        progress_callback=options.get("progress_callback"),
        expected_service_hours=options.get("expected_service_hours") or "",
        **({"includes_main_work": options["includes_main_work"]} if "includes_main_work" in options else {}),
    )
    normalized_raw = dict(raw)
    if raw.get("status") == "verified":
        normalized_raw.update({"query_completed": True, "found": True})
    elif raw.get("status") == "not_found":
        normalized_raw.update({"query_completed": True, "found": False})
    else:
        normalized_raw.update({"query_completed": False, "found": False})
    return _normalize_result(
        normalized_raw,
        step="remote_workorder",
        report_date=report_date,
        customer=customer or raw.get("service_customer", ""),
    )


def _normalize_result(raw, *, step, report_date, customer):
    if raw.get("found"):
        status = "crm_verified"
    elif raw.get("query_completed"):
        status = "not_found"
    else:
        status = "unknown"
    return {
        "status": status,
        "step": step,
        "report_date": report_date,
        "customer": customer,
        "crm_customer": raw.get("crm_customer") or customer,
        "workorder_id": raw.get("workorder_id"),
        "service_record_id": raw.get("service_record_id"),
        "service_hours": raw.get("service_hours"),
        "query_completed": bool(raw.get("query_completed")),
        "query_status": raw.get("query_status") or (
            "found" if raw.get("found") else "not_found" if raw.get("query_completed") else "query_failed"
        ),
        "queried_at": datetime.now().isoformat(timespec="seconds"),
        "message": str(raw.get("message", "")),
        "artifacts": list(raw.get("artifacts", []) or []),
    }


def _prefer_known_id(candidates):
    return next(
        (
            item
            for item in candidates
            if item.get("workorder_id") or item.get("service_record_id")
        ),
        candidates[0] if candidates else None,
    )


def _select_customer_draft(drafts, customer):
    if not drafts:
        raise RuntimeError("日报中没有外勤服务草稿")
    if customer:
        matches = [draft for draft in drafts if draft.customer_name == customer]
        if len(matches) == 1:
            return matches[0]
        raise RuntimeError("日报客户与指定复核客户不一致，无法确定复核对象")
    if len(drafts) == 1:
        return drafts[0]
    raise RuntimeError("日报存在多个外勤客户，无法确定复核对象")


def _digest(payload):
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
