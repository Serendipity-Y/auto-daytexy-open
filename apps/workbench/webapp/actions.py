from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
import time
from copy import deepcopy

from integrations.fxiaoke import crm_enabled
from integrations.fxiaoke import daily_log as fxiaoke_daily_log
from integrations.fxiaoke import remote_workorder as fxiaoke_remote_workorder
from integrations.fxiaoke import service_record as fxiaoke_service_record
from packages.report_domain.history import (
    clear_pending_fxiaoke_tasks,
    record_fxiaoke_submission_history,
    record_pending_fxiaoke_task,
)
from packages.report_domain.report_format import find_saved_report_section
from packages.report_domain.workday import is_onsite_workday

from .config import CRM_DISABLED_MESSAGE
from .jobs import JobStatus, _has_captured_id, append_job_progress, record_job_milestone, transition_job
from .store import WebStore
from .knowledge_base import (
    KNOWLEDGE_BASE_SYNC_ACTION,
    build_knowledge_base_sync_descriptor,
    sync_knowledge_base,
)
from .previews import (
    DAILY_LOG_ACTION,
    ONSITE_SERVICE_ACTION,
    REMOTE_WORKORDER_ACTION,
    build_action_previews,
    daily_log_preview_payload,
    onsite_service_preview_payload,
    remote_options_from_payload,
)
from .remote_hours import (
    REMOTE_WORKORDER_HOURS_SYNC_ACTION,
    build_remote_hour_sync_descriptor,
    find_remote_hour_candidate,
)
from .remote_bindings import (
    REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION,
    build_remote_workorder_binding_descriptor,
    find_remote_workorder_binding_candidate,
)
from .verification import (
    build_verification_jobs,
    verification_authorization_digest,
    verify_daily_log,
    verify_onsite_service,
    verify_remote_workorder,
)


CRM_VERIFICATION_ACTION = "crm_verification"
ONSITE_ROUTING_AUDIT_FIELDS = (
    "work_type", "routing_policy", "routing_target", "source_checkin_workorder_id",
    "routing_source",
)


class AuthorizationMismatch(RuntimeError):
    pass


@dataclass
class ActionContext:
    store: WebStore
    job_id: str
    progress_callback: Callable[[str], None] | None = None
    report_path: Path | None = None
    show_browser: bool = False
    intent_evidence: dict | None = None

    def checkpoint_submission(self, *, event=None, fields=None, result=None) -> None:
        if self.intent_evidence:
            fxiaoke_daily_log.checkpoint_fxiaoke_submission(
                self.intent_evidence, child_index=0, event=event, fields=fields, result=result,
            )

    def progress(self, message: str) -> None:
        cleaned = str(message or "").strip()
        if not cleaned:
            return
        append_job_progress(self.store, self.job_id, cleaned)
        if self.progress_callback is not None:
            self.progress_callback(cleaned)

    def mark_browser_started(self) -> None:
        self.checkpoint_submission(event="browser_started")
        record_job_milestone(self.store, self.job_id, "browser_started")

    def mark_submit_clicked(self) -> None:
        self.checkpoint_submission(event="submit_clicked")
        record_job_milestone(self.store, self.job_id, "submit_clicked")

    def mark_crm_id_captured(
        self,
        *,
        workorder_id: str | None = None,
        service_record_id: str | None = None,
        feed_id: str | None = None,
    ) -> None:
        if not workorder_id and not service_record_id and not feed_id:
            raise ValueError("captured CRM milestone requires an identifier")
        job = self.store.get_job(self.job_id) or {}
        target = job.get("result_payload", {}).get("routing_target")
        if target and workorder_id and target.get("workorder_id") != workorder_id:
            self.merge_result({
                "status": "uncertain", "retry_action": "verify_crm",
                "routing_conflict": {
                    "expected_workorder_id": target.get("workorder_id"),
                    "observed_workorder_id": workorder_id,
                    "observed_service_record_id": service_record_id,
                },
            })
            raise AuthorizationMismatch("CRM回执工单与已冻结的关联目标不一致，停止并保留核验依据。")
        self.checkpoint_submission(event="crm_id_captured", fields={
            "workorder_id": workorder_id, "service_record_id": service_record_id, "feed_id": feed_id,
        })
        record_job_milestone(
            self.store,
            self.job_id,
            "crm_id_captured",
            workorder_id=workorder_id,
            service_record_id=service_record_id,
            feed_id=feed_id,
        )

    def mark_crm_verified(self, result: dict | None = None) -> None:
        if result:
            self.checkpoint_submission(result=result)
            self.merge_result(result)
        record_job_milestone(
            self.store,
            self.job_id,
            "crm_verified",
            crm_customer=(result or {}).get("crm_customer"),
            workorder_id=(result or {}).get("workorder_id"),
            service_record_id=(result or {}).get("service_record_id"),
            feed_id=(result or {}).get("feed_id"),
        )

    def merge_result(self, result: dict) -> None:
        if not isinstance(result, dict):
            raise ValueError("action result must be an object")
        job = self.store.get_job(self.job_id)
        if job is None:
            raise KeyError(f"unknown job: {self.job_id}")
        merged = dict(job.get("result_payload", {}))
        for key in ONSITE_ROUTING_AUDIT_FIELDS:
            if key in merged and key in result and merged[key] != result[key]:
                raise AuthorizationMismatch("已冻结的外勤关联工单或路由授权发生变化，禁止替换。")
        merged.update(result)
        self.store.update_job(
            self.job_id,
            job["status"],
            status=job["status"],
            result_payload=merged,
        )

    def load_report_section(self, report_date: str) -> str:
        if self.report_path is None:
            raise RuntimeError("任务上下文缺少日报路径")
        return find_saved_report_section(self.report_path, report_date)


def execute_job(store, job_id: str, progress_callback=None) -> dict:
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    action = str(job.get("action", "")).strip()
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"unsupported action: {action}")
    if job["status"] != "running":
        raise RuntimeError(f"job is not running: {job_id} ({job['status']})")

    if action != KNOWLEDGE_BASE_SYNC_ACTION and not crm_enabled():
        raise RuntimeError(CRM_DISABLED_MESSAGE)

    if action == CRM_VERIFICATION_ACTION:
        request_value, report_path = _rebuild_authorized_verification(store, job)
        context = ActionContext(
            store,
            job_id,
            progress_callback=progress_callback,
            report_path=report_path,
        )
        result = handler(request_value, context)
        if not isinstance(result, dict):
            raise RuntimeError(f"action handler returned invalid result: {action}")
        return result

    if action == REMOTE_WORKORDER_HOURS_SYNC_ACTION:
        request_value, report_path = _rebuild_authorized_remote_hour_sync(store, job)
        context = ActionContext(
            store,
            job_id,
            progress_callback=progress_callback,
            report_path=report_path,
            show_browser=bool(request_value.get("show_browser", False)),
        )
        result = handler(request_value, context)
        if not isinstance(result, dict):
            raise RuntimeError(f"action handler returned invalid result: {action}")
        return result

    if action == REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION:
        request_value, report_path = _rebuild_authorized_remote_workorder_binding(
            store,
            job,
        )
        context = ActionContext(
            store,
            job_id,
            progress_callback=progress_callback,
            report_path=report_path,
            show_browser=bool(request_value.get("show_browser", False)),
        )
        result = handler(request_value, context)
        if not isinstance(result, dict):
            raise RuntimeError(f"action handler returned invalid result: {action}")
        return result

    if action == KNOWLEDGE_BASE_SYNC_ACTION:
        descriptor = _rebuild_authorized_knowledge_base_sync(job)
        context = ActionContext(store, job_id, progress_callback=progress_callback)
        result = handler(descriptor, context)
        if not isinstance(result, dict):
            raise RuntimeError(f"action handler returned invalid result: {action}")
        return result

    preview, report_path = _rebuild_authorized_preview(store, job)
    context = ActionContext(
        store,
        job_id,
        progress_callback=progress_callback,
        report_path=report_path,
        show_browser=bool(job.get("request_payload", {}).get("show_browser", False)),
    )
    step = {
        DAILY_LOG_ACTION: "daily_log", ONSITE_SERVICE_ACTION: "service_record",
        REMOTE_WORKORDER_ACTION: "remote_workorder",
    }.get(action)
    intent_path = None
    intent_evidence: dict = {}
    if step:
        section = context.load_report_section(preview.report_date)
        existing, intent_path = fxiaoke_daily_log.reserve_fxiaoke_submission_intent(
            section, step, report_path=report_path, evidence_context=intent_evidence,
        )
        if existing is not None:
            message = "存在未闭环的本机提交意图，请先只读复核，禁止直接重提。"
            transition_job(store, job_id, JobStatus.UNCERTAIN, error_summary=message)
            raise AuthorizationMismatch(message)
        context.intent_evidence = intent_evidence
    try:
        if step:
            fxiaoke_daily_log.checkpoint_fxiaoke_submission(
                intent_evidence, expected_customers=[preview.customer],
            )
        result = handler(preview, context)
        if not isinstance(result, dict):
            raise RuntimeError(f"action handler returned invalid result: {action}")
        return result
    except fxiaoke_daily_log.SubmissionEvidenceError:
        context.merge_result({
            "status": "uncertain", "submission_phase": "evidence_error", "retry_action": "verify_crm",
        })
        raise
    finally:
        current = store.get_job(job_id) or {}
        evidence = current.get("result_payload", {})
        before_submit = (
            not evidence.get("submit_clicked") and not evidence.get("crm_id_captured")
            and not _has_captured_id(evidence)
            and evidence.get("status") not in {"uncertain", "crm_id_captured"}
            and (not evidence.get("browser_started") or evidence.get("submission_phase") == "before_submit")
        )
        if current.get("status") == JobStatus.CRM_VERIFIED or before_submit:
            fxiaoke_daily_log.clear_fxiaoke_submission_intent(intent_path)


def _rebuild_authorized_preview(store, job):
    request_payload = job.get("request_payload", {})
    action = str(job.get("action", "")).strip()
    if request_payload.get("action") != action:
        raise AuthorizationMismatch("任务动作与授权 payload 不一致")
    report_date = str(request_payload.get("report_date", "")).strip()
    customer = str(request_payload.get("customer", "")).strip()
    content_digest = str(request_payload.get("content_digest", "")).strip()
    if not report_date or not customer or not content_digest:
        raise AuthorizationMismatch("任务授权 payload 缺少日期、客户或内容摘要")

    day = store.get_day(report_date)
    if day is None:
        raise AuthorizationMismatch(f"本地索引中不存在 {report_date} 的日报")
    hours_by_customer = None
    hours = request_payload.get("hours")
    if action == REMOTE_WORKORDER_ACTION and hours is not None:
        hours_by_customer = {customer: hours}
    frozen_payload = request_payload.get("payload")
    remote_role = frozen_payload.get("includes_main_work") if isinstance(frozen_payload, dict) else None
    try:
        previews = build_action_previews(
            day["report_path"],
            report_date,
            action,
            hours_by_customer=hours_by_customer,
            remote_options_by_customer=(
                remote_options_from_payload(customer, frozen_payload)
                if action == REMOTE_WORKORDER_ACTION else None
            ),
            **({"includes_main_work": remote_role} if action == REMOTE_WORKORDER_ACTION and isinstance(remote_role, bool) else {}),
        )
    except Exception as exc:
        raise AuthorizationMismatch(f"无法重建已授权预览：{exc}") from exc

    matching = [
        preview
        for preview in previews
        if preview.customer == customer and preview.content_digest == content_digest
    ]
    if len(matching) != 1:
        raise AuthorizationMismatch("日报内容或客户已变化，请重新生成预览")
    preview = matching[0]
    if preview.unique_key != request_payload.get("unique_key"):
        raise AuthorizationMismatch("任务唯一键已变化，请重新生成预览")
    if preview.authorization_digest != job.get("authorized_digest"):
        raise AuthorizationMismatch("日报内容、工时或授权指纹已变化")
    return preview, Path(day["report_path"])


def _rebuild_authorized_verification(store, job):
    request_payload = dict(job.get("request_payload", {}))
    if request_payload.get("action") != CRM_VERIFICATION_ACTION:
        raise AuthorizationMismatch("复核任务动作不一致")
    if verification_authorization_digest(request_payload) != job.get("authorized_digest"):
        raise AuthorizationMismatch("复核任务原始授权已变化")
    report_date = str(request_payload.get("report_date", "")).strip()
    day = store.get_day(report_date)
    if day is None:
        raise AuthorizationMismatch(f"本地索引中不存在 {report_date} 的日报")
    try:
        candidates = build_verification_jobs(
            report_date, day=day, steps=(request_payload.get("step"),),
            source_jobs=store.list_jobs(report_date=report_date),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorizationMismatch(f"无法恢复原始复核依据：{exc}") from exc
    matching = [
        item
        for item in candidates
        if item["step"] == request_payload.get("step")
        and item.get("customer") == request_payload.get("customer")
        and item.get("workorder_id") == request_payload.get("workorder_id")
        and item.get("service_record_id") == request_payload.get("service_record_id")
        and item.get("includes_main_work") is request_payload.get("includes_main_work")
    ]
    if len(matching) != 1:
        raise AuthorizationMismatch("复核对象或已知 CRM 编号已变化")
    descriptor = matching[0]
    if descriptor.get("verification_conflict"):
        raise AuthorizationMismatch(descriptor["verification_conflict"])
    if verification_authorization_digest(descriptor) != job.get("authorized_digest"):
        raise AuthorizationMismatch("复核授权指纹已变化")
    if descriptor["step"] == "remote_workorder" and descriptor.get("content_digest"):
        try:
            options = {"includes_main_work": descriptor["includes_main_work"]} if "includes_main_work" in descriptor else {}
            previews = build_action_previews(day["report_path"], report_date, REMOTE_WORKORDER_ACTION, **options)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AuthorizationMismatch(f"无法重新核对远程工单正文：{exc}") from exc
        if len([
            preview for preview in previews
            if preview.customer == descriptor["customer"] and preview.content_digest == descriptor["content_digest"]
        ]) != 1:
            raise AuthorizationMismatch("远程工单日报内容或客户已变化，请重新生成预览后核对。")
    return descriptor, Path(day["report_path"])


def _rebuild_authorized_remote_hour_sync(store, job):
    request_payload = dict(job.get("request_payload", {}))
    if request_payload.get("action") != REMOTE_WORKORDER_HOURS_SYNC_ACTION:
        raise AuthorizationMismatch("远程工单工时同步任务动作不一致")
    report_date = str(request_payload.get("report_date", "")).strip()
    content_digest = str(request_payload.get("content_digest", "")).strip()
    workorder_id = str(request_payload.get("workorder_id", "")).strip()
    if not report_date or not content_digest or not workorder_id:
        raise AuthorizationMismatch("远程工单工时同步任务缺少身份信息")
    day = store.get_day(report_date)
    if day is None:
        raise AuthorizationMismatch(f"本地索引中不存在 {report_date} 的日报")
    try:
        report_section = find_saved_report_section(
            Path(day["report_path"]),
            report_date,
        )
        candidate = find_remote_hour_candidate(
            day,
            report_section,
            content_digest,
            workorder_id,
        )
        override = store.get_remote_workorder_hour_override(
            report_date=report_date,
            content_digest=candidate.content_digest,
            workorder_id=candidate.workorder_id,
        )
        if override is None:
            raise ValueError("远程工单工时同步状态不存在")
        if str(override.get("crm_sync_job_id") or "").strip() != str(
            job.get("job_id") or ""
        ).strip():
            raise ValueError("远程工单工时同步任务已被其他任务取代")
        descriptor = build_remote_hour_sync_descriptor(
            candidate,
            request_payload.get("target_hours"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorizationMismatch(f"无法重建远程工单工时同步授权：{exc}") from exc
    if descriptor["unique_key"] != request_payload.get("unique_key"):
        raise AuthorizationMismatch("远程工单工时或同步目标已变化")
    if descriptor["authorization_digest"] != job.get("authorized_digest"):
        raise AuthorizationMismatch("远程工单工时同步授权指纹已变化")
    return descriptor, Path(day["report_path"])


def _rebuild_authorized_remote_workorder_binding(store, job):
    request_payload = dict(job.get("request_payload", {}))
    if (
        request_payload.get("action")
        != REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION
    ):
        raise AuthorizationMismatch("远程工单关联复核任务动作不一致")
    report_date = str(request_payload.get("report_date", "")).strip()
    content_digest = str(request_payload.get("content_digest", "")).strip()
    replacement_id = str(
        request_payload.get("replacement_workorder_id", "")
    ).strip()
    day = store.get_day(report_date)
    if day is None:
        raise AuthorizationMismatch(f"本地索引中不存在 {report_date} 的日报")
    try:
        report_section = find_saved_report_section(
            Path(day["report_path"]),
            report_date,
        )
        candidate = find_remote_workorder_binding_candidate(
            day,
            report_section,
            content_digest,
        )
        descriptor = build_remote_workorder_binding_descriptor(
            day,
            candidate,
            replacement_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorizationMismatch(
            f"无法重建远程工单关联复核授权：{exc}"
        ) from exc
    if descriptor["unique_key"] != request_payload.get("unique_key"):
        raise AuthorizationMismatch("远程工单关联复核对象已变化")
    if descriptor["authorization_digest"] != job.get("authorized_digest"):
        raise AuthorizationMismatch("远程工单关联复核授权指纹已变化")
    return descriptor, Path(day["report_path"])


def _rebuild_authorized_knowledge_base_sync(job):
    request_payload = dict(job.get("request_payload", {}))
    descriptor = build_knowledge_base_sync_descriptor(
        mode=str(request_payload.get("mode") or "")
    )
    for key in ("action", "scope", "metadata_only", "mode", "unique_key"):
        if request_payload.get(key) != descriptor.get(key):
            raise AuthorizationMismatch("知识库同步任务授权信息不一致")
    if job.get("authorized_digest") != descriptor["authorization_digest"]:
        raise AuthorizationMismatch("知识库同步授权指纹无效")
    return descriptor


def run_daily_log_action(preview, context):
    report_section = context.load_report_section(preview.report_date)
    draft = fxiaoke_daily_log.build_daily_log_draft(report_section)
    if daily_log_preview_payload(draft) != dict(preview.payload):
        raise AuthorizationMismatch("日报在授权校验后发生变化，请重新生成预览。")
    try:
        result = fxiaoke_daily_log.run_browser_automation(
            draft,
            storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
            report_path=context.report_path,
            show_browser=context.show_browser,
            progress_callback=context.progress,
            allow_ocr_login=False,
            profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
            milestone_callback=_milestone_callback(context),
        )
    except Exception as exc:
        _record_action_failure(
            context,
            report_section,
            preview,
            step="daily_log",
            step_label="纷享销客日报",
            exc=exc,
        )
        raise

    normalized = {
        **dict(result),
        "step": "daily_log",
        "service_customer": preview.customer,
        "crm_customer": preview.customer,
        "content_digest": preview.content_digest,
    }
    if normalized.get("feed_id"):
        _ensure_crm_id_captured(context, normalized)
    _record_action_success(context, report_section, preview, normalized)
    return normalized


def run_onsite_service_action(preview, context):
    report_section = context.load_report_section(preview.report_date)
    if not is_onsite_workday(report_text=report_section, report_path=context.report_path):
        raise AuthorizationMismatch("外勤状态在授权校验后发生变化，请重新生成预览。")
    drafts = fxiaoke_service_record.build_service_record_drafts(
        report_section,
        main_only=True,
        report_path=context.report_path,
    )
    matching = [
        draft
        for draft in drafts
        if draft.customer_name == preview.customer
        and draft.service_content.strip() == preview.content.strip()
        and onsite_service_preview_payload(draft) == dict(preview.payload)
    ]
    if len(matching) != 1:
        raise AuthorizationMismatch("无法从已保存日报重建唯一外勤服务草稿")
    draft = matching[0]
    frozen = _onsite_retry_routing(context, preview)
    if frozen:
        draft = replace(
            draft,
            routing_target=deepcopy(frozen["routing_target"]),
            source_checkin_workorder_id=frozen.get("source_checkin_workorder_id", ""),
            routing_source=deepcopy(frozen.get("routing_source")),
        )
        context.merge_result(frozen)
    try:
        result = fxiaoke_service_record.run_browser_automation(
            draft,
            storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
            report_path=context.report_path,
            show_browser=context.show_browser,
            progress_callback=context.progress,
            allow_ocr_login=False,
            profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
            milestone_callback=_milestone_callback(context),
        )
    except Exception as exc:
        _record_action_failure(
            context,
            report_section,
            preview,
            step="service_record",
            step_label="纷享销客技术服务记录",
            exc=exc,
            extra={"main_only": True},
        )
        raise

    normalized = {
        "ok": result.status in {"submitted", "already_exists"},
        "step": "service_record",
        "status": result.status,
        "message": result.message,
        "artifacts": list(result.artifacts),
        "service_customer": preview.customer,
        "crm_customer": result.crm_customer or preview.customer,
        "workorder_id": result.workorder_id,
        "service_record_id": result.service_record_id,
        "content_digest": preview.content_digest,
        "main_only": True,
    }
    for key in ONSITE_ROUTING_AUDIT_FIELDS:
        value = getattr(result, key, None)
        if value not in (None, ""):
            normalized[key] = deepcopy(value)
    if result.workorder_id or result.service_record_id:
        _ensure_crm_id_captured(context, normalized)
    _record_action_success(context, report_section, preview, normalized)
    return normalized


def _onsite_retry_routing(context, preview):
    """Carry a server-recorded target through safe retries without choosing anew."""
    job = context.store.get_job(context.job_id) or {}
    seen = set()
    while job:
        identity = job.get("job_id")
        if identity in seen:
            raise AuthorizationMismatch("外勤重试记录存在循环，已停止。")
        seen.add(identity)
        if (
            job.get("action") != ONSITE_SERVICE_ACTION
            or job.get("authorized_digest") != preview.authorization_digest
        ):
            raise AuthorizationMismatch("外勤重试授权与原始任务不一致。")
        request = job.get("request_payload", {})
        if (
            request.get("report_date") != preview.report_date
            or request.get("customer") != preview.customer
            or request.get("content_digest") != preview.content_digest
        ):
            raise AuthorizationMismatch("外勤重试的日期、客户或正文已变化。")
        evidence = job.get("result_payload", {})
        if evidence.get("routing_target"):
            if (
                preview.payload.get("work_type") != "售后"
                or evidence.get("work_type") != "售后"
                or evidence.get("routing_policy") != preview.payload.get("routing_policy")
            ):
                raise AuthorizationMismatch("已选售后工单不能用于其他工作类型或路由规则。")
            target = evidence["routing_target"]
            if preview.payload.get("routing_target") is not None and preview.payload["routing_target"] != target:
                raise AuthorizationMismatch("重试关联目标与明确授权的工单不一致。")
            return {key: deepcopy(evidence[key]) for key in ONSITE_ROUTING_AUDIT_FIELDS if key in evidence}
        parent_id = job.get("retry_of_job_id")
        if not parent_id:
            return {}
        job = context.store.get_job(parent_id)
        if job is None:
            raise AuthorizationMismatch("外勤重试缺少原始任务，无法恢复关联工单。")
    return {}


def run_remote_workorder_action(preview, context):
    report_section = context.load_report_section(preview.report_date)
    payload = preview.payload
    current = fxiaoke_remote_workorder.build_remote_workorder_drafts(
        report_section,
        is_onsite=is_onsite_workday(report_text=report_section, report_path=context.report_path),
        includes_main_work=payload["includes_main_work"],
    )
    matching = [
        draft for draft in current
        if draft.report_date_iso == preview.report_date
        and draft.customer_name == preview.customer
        and draft.content_items == list(payload["content_items"])
        and draft.includes_main_work == bool(payload["includes_main_work"])
        and draft.source_completion_status == str(payload["source_completion_status"])
        and draft.selected_customer_name == str(payload["selected_customer_name"])
    ]
    if len(matching) != 1:
        raise AuthorizationMismatch("远程草稿在授权校验后发生变化，请重新生成预览。")
    draft = matching[0]
    draft = fxiaoke_remote_workorder.apply_remote_workorder_review_options(
        draft,
        service_category=str(payload["service_category"]),
        completion_status=str(payload["completion_status"]),
    )
    if draft.service_category_value != str(payload["service_category_value"]):
        raise AuthorizationMismatch("远程服务类别配置已变化，请重新生成预览。")
    draft.service_hours = str(preview.hours or "")
    try:
        result = fxiaoke_remote_workorder.submit_authorized_remote_workorder_draft(
            draft,
            context.report_path,
            authorization_digest=preview.authorization_digest,
            storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
            profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
            show_browser=context.show_browser,
            allow_ocr_login=False,
            progress_callback=context.progress,
            milestone_callback=_milestone_callback(context),
        )
    except Exception as exc:
        _record_action_failure(
            context, report_section, preview,
            step="remote_workorder", step_label="纷享销客远程服务工单", exc=exc,
            extra={"includes_main_work": draft.includes_main_work},
        )
        raise

    result = {**result, "includes_main_work": draft.includes_main_work}
    if result.get("workorder_id"):
        _ensure_crm_id_captured(context, result)
    if result.get("status") == "crm_id_captured":
        result = {
            **result, "step": "remote_workorder", "service_customer": preview.customer,
            "content_digest": preview.content_digest, "submission_phase": "crm_id_captured",
            "retry_action": "verify_crm",
        }
        context.merge_result(result)
        context.checkpoint_submission(result=result)
        record_pending_fxiaoke_task(
            report_section, context.report_path,
            "remote_workorder", "纷享销客远程服务工单", result,
        )
        return result
    _record_action_success(context, report_section, preview, result)
    return result


def run_crm_verification_action(request_value, context):
    common = {
        "progress_callback": context.progress,
        "show_browser": False,
    }
    step = request_value["step"]
    if step == "daily_log":
        result = verify_daily_log(
            context.report_path,
            request_value["report_date"],
            customer=request_value.get("customer", ""),
            **common,
        )
    elif step == "service_record":
        routing_options = {
            key: deepcopy(request_value[key])
            for key in ("routing_target", "source_checkin_workorder_id", "routing_source")
            if request_value.get(key) not in (None, "")
        }
        result = verify_onsite_service(
            context.report_path,
            request_value["report_date"],
            customer=request_value.get("customer", ""),
            workorder_id=request_value.get("workorder_id") or "",
            service_record_id=request_value.get("service_record_id") or "",
            **common,
            **routing_options,
        )
    elif step == "remote_workorder":
        result = verify_remote_workorder(
            context.report_path,
            request_value["report_date"],
            customer=request_value.get("customer", ""),
            workorder_id=request_value.get("workorder_id") or "",
            expected_service_hours=request_value.get("expected_service_hours") or "",
            **({"includes_main_work": request_value["includes_main_work"]} if "includes_main_work" in request_value else {}),
            **common,
        )
    else:
        raise ValueError(f"unsupported verification step: {step}")

    if step == "service_record" and request_value.get("routing_target"):
        conflict = any(request_value.get(key) and result.get(key) != request_value[key]
                       and (result["status"] == "crm_verified" or result.get(key))
                       for key in ("workorder_id", "service_record_id"))
        if conflict:
            context.merge_result({
                **{key: deepcopy(request_value[key]) for key in ONSITE_ROUTING_AUDIT_FIELDS if key in request_value},
                "status": "uncertain", "retry_action": "verify_crm",
                "routing_conflict": {"expected_workorder_id": request_value.get("workorder_id"),
                                     "observed_workorder_id": result.get("workorder_id"),
                                     "observed_service_record_id": result.get("service_record_id")},
            })
            raise AuthorizationMismatch("复核回执与冻结目标或原服务记录不一致，未写入成功证据。")

    if result["status"] == "crm_verified":
        context.store.upsert_submission(
            {
                "report_date": result["report_date"],
                "step": result["step"],
                "service_customer": result.get("customer", ""),
                "content_digest": request_value.get("content_digest", ""),
                "status": result["status"],
                "workorder_id": result.get("workorder_id"),
                "service_record_id": result.get("service_record_id"),
                "crm_customer": result.get("crm_customer"),
                "service_hours": result.get("service_hours"),
                **({"includes_main_work": request_value["includes_main_work"]} if "includes_main_work" in request_value else {}),
                "crm_verified_at": result.get("queried_at"),
                "source": f"crm_verification:{context.job_id}",
            }
        )
        context.mark_crm_verified(result)
    else:
        context.merge_result(result)
        job = context.store.get_job(context.job_id)
        diagnostic_message = str(result.get("message", "")).strip()
        transition_job(
            context.store,
            context.job_id,
            result["status"],
            error_summary=diagnostic_message or None,
            progress_log=[
                *job.get("progress_log", []),
                "CRM 复核完成：" + result["status"],
            ],
        )
    return result


def run_remote_workorder_hours_sync_action(request_value, context):
    common = {
        "workorder_id": request_value["workorder_id"],
        "customer_name": request_value["customer"],
        "report_date": request_value["report_date"],
        "expected_hours": request_value["expected_hours"],
        "target_hours": request_value["target_hours"],
        "report_path": context.report_path,
        "storage_state_path": fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
        "profile_dir": fxiaoke_service_record.resolve_profile_dir(None),
        "show_browser": context.show_browser,
        "allow_ocr_login": False,
        "progress_callback": context.progress,
        "milestone_callback": _milestone_callback(context),
    }
    for attempt in range(2):
        try:
            result = fxiaoke_remote_workorder.update_remote_workorder_hours(**common)
            break
        except fxiaoke_remote_workorder.RemoteWorkorderIdentityMismatch as exc:
            return _finish_remote_hour_identity_conflict(request_value, context, exc)
        except fxiaoke_remote_workorder.RemoteWorkorderHoursMismatch as exc:
            if _remote_hour_submit_was_clicked(context):
                return _resolve_submitted_hour_update_with_read_only_checks(
                    request_value,
                    context,
                    initial_error=exc,
                )
            return _finish_remote_hour_hours_conflict(request_value, context, exc)
        except fxiaoke_remote_workorder.RemoteWorkorderNotFound as exc:
            if _remote_hour_submit_was_clicked(context):
                return _resolve_submitted_hour_update_with_read_only_checks(
                    request_value,
                    context,
                    initial_error=exc,
                )
            return _finish_remote_hour_not_found(request_value, context, exc)
        except Exception as exc:
            if _remote_hour_submit_was_clicked(context):
                return _resolve_submitted_hour_update_with_read_only_checks(
                    request_value,
                    context,
                    initial_error=exc,
                )
            if attempt == 0:
                context.progress("CRM 提交前失败，正在进行唯一一次自动重试。")
                continue
            context.store.record_remote_workorder_hour_sync_result(
                report_date=request_value["report_date"],
                content_digest=request_value["content_digest"],
                workorder_id=request_value["workorder_id"],
                expected_job_id=context.job_id,
                crm_sync_status="pending_retry",
                crm_error=str(exc),
            )
            raise

    if result.get("status") != "submitted":
        raise RuntimeError(result.get("message") or "CRM 工时同步没有返回成功状态")
    context.store.record_remote_workorder_hour_sync_result(
        report_date=request_value["report_date"],
        content_digest=request_value["content_digest"],
        workorder_id=request_value["workorder_id"],
        expected_job_id=context.job_id,
        crm_sync_status="crm_verified",
        crm_observed_hours=result.get("crm_observed_hours"),
    )
    context.mark_crm_verified(result)
    return result


def _remote_hour_submit_was_clicked(context):
    job = context.store.get_job(context.job_id) or {}
    return bool(job.get("result_payload", {}).get("submit_clicked"))


def _finish_remote_hour_identity_conflict(request_value, context, exc):
    payload = {
        "ok": False,
        "step": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
        "status": "conflict",
        "conflict_kind": exc.kind,
        "conflict_expected": exc.expected,
        "conflict_observed": exc.observed,
        "workorder_id": exc.workorder_id,
        "report_customer": request_value.get("report_customer", ""),
        "crm_customer": request_value["customer"],
        "expected_hours": request_value["expected_hours"],
        "service_hours": request_value["target_hours"],
        "message": str(exc),
        "artifacts": list(getattr(exc, "artifacts", []) or []),
    }
    return _finish_remote_hour_conflict(
        request_value,
        context,
        payload,
        observed_hours=None,
    )


def _finish_remote_hour_hours_conflict(request_value, context, exc):
    observed_hours = str(getattr(exc, "observed_hours", "") or "").strip()
    payload = {
        "ok": False,
        "step": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
        "status": "conflict",
        "conflict_kind": "hours",
        "conflict_expected": request_value["expected_hours"],
        "conflict_observed": observed_hours,
        "workorder_id": request_value["workorder_id"],
        "report_customer": request_value.get("report_customer", ""),
        "crm_customer": request_value["customer"],
        "expected_hours": request_value["expected_hours"],
        "service_hours": request_value["target_hours"],
        "crm_observed_hours": observed_hours,
        "message": str(exc),
        "artifacts": list(getattr(exc, "artifacts", []) or []),
    }
    return _finish_remote_hour_conflict(
        request_value,
        context,
        payload,
        observed_hours=observed_hours or None,
    )


def _finish_remote_hour_conflict(
    request_value,
    context,
    payload,
    *,
    observed_hours,
):
    context.store.record_remote_workorder_hour_sync_result(
        report_date=request_value["report_date"],
        content_digest=request_value["content_digest"],
        workorder_id=request_value["workorder_id"],
        expected_job_id=context.job_id,
        crm_sync_status="crm_mismatch",
        crm_observed_hours=observed_hours,
        crm_error=payload["message"],
    )
    context.merge_result(payload)
    job = context.store.get_job(context.job_id)
    transition_job(
        context.store,
        context.job_id,
        "conflict",
        progress_log=[*job.get("progress_log", []), "CRM 工单校验冲突，未继续写入。"],
        error_summary=payload["message"],
    )
    return payload


def _finish_remote_hour_not_found(request_value, context, exc):
    payload = {
        "ok": False,
        "step": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
        "status": "not_found",
        "workorder_id": request_value["workorder_id"],
        "report_customer": request_value.get("report_customer", ""),
        "crm_customer": request_value["customer"],
        "expected_hours": request_value["expected_hours"],
        "service_hours": request_value["target_hours"],
        "message": str(exc),
        "artifacts": list(getattr(exc, "artifacts", []) or []),
    }
    context.store.record_remote_workorder_hour_sync_result(
        report_date=request_value["report_date"],
        content_digest=request_value["content_digest"],
        workorder_id=request_value["workorder_id"],
        expected_job_id=context.job_id,
        crm_sync_status="not_found",
        crm_error=str(exc),
    )
    context.merge_result(payload)
    job = context.store.get_job(context.job_id)
    transition_job(
        context.store,
        context.job_id,
        "not_found",
        progress_log=[*job.get("progress_log", []), "CRM 未找到远程工单，未写入。"],
        error_summary=str(exc),
    )
    return payload


def _resolve_submitted_hour_update_with_read_only_checks(
    request_value,
    context,
    *,
    initial_error,
):
    last_observed_hours = ""
    recovery_messages = [str(initial_error)]
    read_common = {
        "workorder_id": request_value["workorder_id"],
        "customer_name": request_value["customer"],
        "report_date": request_value["report_date"],
        "report_path": context.report_path,
        "storage_state_path": fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
        "profile_dir": fxiaoke_service_record.resolve_profile_dir(None),
        "show_browser": context.show_browser,
        "allow_ocr_login": False,
        "progress_callback": context.progress,
    }
    for check_index in range(2):
        if check_index:
            time.sleep(5)
        try:
            inspection = fxiaoke_remote_workorder.read_remote_workorder_hours(
                **read_common
            )
        except fxiaoke_remote_workorder.RemoteWorkorderIdentityMismatch as exc:
            return _finish_remote_hour_identity_conflict(request_value, context, exc)
        except Exception as exc:
            recovery_messages.append(str(exc))
            continue

        last_observed_hours = str(
            inspection.get("crm_observed_hours", "") or ""
        ).strip()
        if fxiaoke_remote_workorder.remote_workorder_hours_match(
            last_observed_hours,
            request_value["target_hours"],
        ):
            result = {
                "ok": True,
                "step": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
                "status": "submitted",
                "workorder_id": inspection["workorder_id"],
                "crm_customer": inspection["crm_customer"],
                "expected_hours": request_value["expected_hours"],
                "service_hours": request_value["target_hours"],
                "crm_observed_hours": last_observed_hours,
                "message": "CRM 提交后页面异常，已通过只读回查确认目标工时。",
                "artifacts": [],
                "recovered_by_read_only_check": True,
            }
            context.store.record_remote_workorder_hour_sync_result(
                report_date=request_value["report_date"],
                content_digest=request_value["content_digest"],
                workorder_id=request_value["workorder_id"],
                expected_job_id=context.job_id,
                crm_sync_status="crm_verified",
                crm_observed_hours=last_observed_hours,
            )
            context.mark_crm_verified(result)
            return result
        recovery_messages.append(
            f"CRM 回查工时为 {last_observed_hours or '未识别'}"
        )

    message = "；".join(item for item in recovery_messages if item)
    payload = {
        "ok": False,
        "step": REMOTE_WORKORDER_HOURS_SYNC_ACTION,
        "status": "uncertain",
        "workorder_id": request_value["workorder_id"],
        "report_customer": request_value.get("report_customer", ""),
        "crm_customer": request_value["customer"],
        "expected_hours": request_value["expected_hours"],
        "service_hours": request_value["target_hours"],
        "crm_observed_hours": last_observed_hours,
        "message": message or "CRM 提交后无法确认工时结果。",
        "artifacts": list(getattr(initial_error, "artifacts", []) or []),
    }
    context.store.record_remote_workorder_hour_sync_result(
        report_date=request_value["report_date"],
        content_digest=request_value["content_digest"],
        workorder_id=request_value["workorder_id"],
        expected_job_id=context.job_id,
        crm_sync_status="uncertain",
        crm_error=payload["message"],
    )
    context.merge_result(payload)
    job = context.store.get_job(context.job_id)
    transition_job(
        context.store,
        context.job_id,
        "uncertain",
        progress_log=[*job.get("progress_log", []), "提交后只读回查未能确认目标工时。"],
        error_summary=payload["message"],
    )
    return payload


def run_remote_workorder_binding_verification_action(request_value, context):
    try:
        inspection = fxiaoke_remote_workorder.read_remote_workorder_hours(
            workorder_id=request_value["replacement_workorder_id"],
            customer_name=request_value["customer"],
            report_date=request_value["expected_service_date"],
            report_path=context.report_path,
            storage_state_path=fxiaoke_service_record.DEFAULT_STORAGE_STATE_PATH,
            profile_dir=fxiaoke_service_record.resolve_profile_dir(None),
            show_browser=context.show_browser,
            allow_ocr_login=False,
            progress_callback=context.progress,
        )
    except fxiaoke_remote_workorder.RemoteWorkorderIdentityMismatch as exc:
        payload = {
            "ok": False,
            "step": REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION,
            "status": "conflict",
            "conflict_kind": exc.kind,
            "conflict_expected": exc.expected,
            "conflict_observed": exc.observed,
            "workorder_id": exc.workorder_id,
            "message": str(exc),
            "artifacts": list(getattr(exc, "artifacts", []) or []),
        }
        context.merge_result(payload)
        job = context.store.get_job(context.job_id)
        transition_job(
            context.store,
            context.job_id,
            "conflict",
            progress_log=[*job.get("progress_log", []), "替换工单只读校验冲突。"],
            error_summary=str(exc),
        )
        return payload
    except fxiaoke_remote_workorder.RemoteWorkorderNotFound as exc:
        payload = {
            "ok": False,
            "step": REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION,
            "status": "not_found",
            "workorder_id": request_value["replacement_workorder_id"],
            "message": str(exc),
            "artifacts": list(getattr(exc, "artifacts", []) or []),
        }
        context.merge_result(payload)
        job = context.store.get_job(context.job_id)
        transition_job(
            context.store,
            context.job_id,
            "not_found",
            progress_log=[*job.get("progress_log", []), "CRM 未找到替换远程工单。"],
            error_summary=str(exc),
        )
        return payload

    observed_hours = inspection["crm_observed_hours"]
    context.store.set_remote_workorder_binding(
        report_date=request_value["report_date"],
        content_digest=request_value["content_digest"],
        original_workorder_id=request_value["original_workorder_id"],
        workorder_id=inspection["workorder_id"],
        crm_customer=inspection["crm_customer"],
        service_date=inspection["service_date"],
        source_job_id=context.job_id,
        confirmed_hours=observed_hours,
    )
    result = {
        "ok": True,
        "step": REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION,
        "status": "crm_verified",
        "workorder_id": inspection["workorder_id"],
        "original_workorder_id": request_value["original_workorder_id"],
        "crm_customer": inspection["crm_customer"],
        "service_date": inspection["service_date"],
        "crm_observed_hours": observed_hours,
        "message": "替换远程工单已通过只读校验并更新本地关联。",
        "artifacts": [],
    }
    context.mark_crm_verified(result)
    return result


def run_knowledge_base_sync_action(descriptor, context):
    result = sync_knowledge_base(
        context.store,
        mode=descriptor["mode"],
        progress_callback=context.progress,
    )
    transition_job(
        context.store,
        context.job_id,
        JobStatus.COMPLETED,
        result_payload=result,
    )
    return result


ACTION_HANDLERS = {
    DAILY_LOG_ACTION: run_daily_log_action,
    ONSITE_SERVICE_ACTION: run_onsite_service_action,
    REMOTE_WORKORDER_ACTION: run_remote_workorder_action,
    REMOTE_WORKORDER_HOURS_SYNC_ACTION: run_remote_workorder_hours_sync_action,
    REMOTE_WORKORDER_BINDING_VERIFICATION_ACTION: (
        run_remote_workorder_binding_verification_action
    ),
    CRM_VERIFICATION_ACTION: run_crm_verification_action,
    KNOWLEDGE_BASE_SYNC_ACTION: run_knowledge_base_sync_action,
}


def _milestone_callback(context):
    def callback(event, **fields):
        if event == "browser_started":
            context.mark_browser_started()
        elif event == "submit_clicked":
            context.mark_submit_clicked()
        elif event == "crm_id_captured":
            context.mark_crm_id_captured(
                workorder_id=fields.get("workorder_id"),
                service_record_id=fields.get("service_record_id"),
                feed_id=fields.get("feed_id"),
            )
        elif event == "target_selected":
            target = fields.get("routing_target")
            if (
                not isinstance(target, dict)
                or not target.get("workorder_id")
                or fields.get("workorder_id", target["workorder_id"]) != target["workorder_id"]
                or fields.get("work_type") != "售后"
                or fields.get("routing_policy") != "implementation_to_latest_after_sales_v1"
                or not fields.get("source_checkin_workorder_id")
            ):
                raise AuthorizationMismatch("外勤关联工单的冻结依据不完整或与授权不一致。")
            job = context.store.get_job(context.job_id) or {}
            authorized = job.get("request_payload", {}).get("payload", {})
            if (
                job.get("action") != ONSITE_SERVICE_ACTION
                or authorized.get("work_type") != "售后"
                or authorized.get("routing_policy") != "implementation_to_latest_after_sales_v1"
            ):
                raise AuthorizationMismatch("本次Web任务未授权实施转售后的关联规则。")
            authorized_target = authorized.get("routing_target")
            if authorized_target is not None and authorized_target != target:
                raise AuthorizationMismatch("外勤关联工单与预览明确授权的目标不一致。")
            authorized_source = authorized.get("routing_source")
            if authorized_source is not None and authorized_source != fields.get("routing_source"):
                raise AuthorizationMismatch("签到来源与预览明确授权的工单不一致。")
            context.merge_result({
                **{key: deepcopy(fields[key]) for key in ONSITE_ROUTING_AUDIT_FIELDS if key in fields},
                "target_selected": True,
                "routing_target_workorder_id": target["workorder_id"],
            })
            context.checkpoint_submission(event="target_selected", fields=fields)
            context.progress(
                f"[外勤关联工单] 签到 {fields['source_checkin_workorder_id']} → "
                f"售后 {target['workorder_id']}；目标已冻结，尚未提交服务记录。"
            )
        else:
            raise ValueError(f"unsupported action milestone: {event}")

    return callback


def _ensure_crm_id_captured(context, result):
    job = context.store.get_job(context.job_id)
    if job is None:
        raise KeyError(f"unknown job: {context.job_id}")
    captured = job.get("result_payload", {})
    if (
        captured.get("workorder_id") == result.get("workorder_id")
        and captured.get("service_record_id") == result.get("service_record_id")
        and captured.get("feed_id") == result.get("feed_id")
    ):
        return
    context.mark_crm_id_captured(
        workorder_id=result.get("workorder_id"),
        service_record_id=result.get("service_record_id"),
        feed_id=result.get("feed_id"),
    )


def _record_action_success(context, report_section, preview, result):
    ok = result.get("ok")
    if (result.get("status") not in {"submitted", "already_exists"}
            or ("ok" in result and not (ok is True or (type(ok) is int and ok == 1)))):
        raise RuntimeError(result.get("message") or "CRM 返回了非成功状态")
    clear_kwargs = {}
    if result["step"] == "service_record":
        clear_kwargs = {"main_only": True, "service_customer": preview.customer}
    elif result["step"] == "remote_workorder":
        clear_kwargs = {"service_customer": preview.customer, "includes_main_work": result.get("includes_main_work")}
    try:
        record_fxiaoke_submission_history(report_section, context.report_path, result)
        clear_pending_fxiaoke_tasks(
            context.report_path,
            step_key=result["step"],
            report_date=preview.report_date,
            **clear_kwargs,
        )
    except Exception as exc:
        result["local_recording_error"] = str(exc)
        result["message"] = str(result.get("message", "")) + f"；CRM 结果已确认，但本机留痕未完成：{exc}"
    now = datetime.now().isoformat(timespec="seconds")
    context.store.upsert_submission(
        {
            "report_date": preview.report_date,
            "step": result["step"],
            "service_customer": preview.customer,
            "content_digest": preview.content_digest,
            "status": "crm_verified",
            "workorder_id": result.get("workorder_id"),
            "service_record_id": result.get("service_record_id"),
            "crm_customer": result.get("crm_customer") or preview.customer,
            "service_hours": result.get("service_hours"),
            "service_category": result.get("service_category"),
            **({"includes_main_work": result.get("includes_main_work")} if result["step"] == "remote_workorder" else {}),
            "local_recorded_at": now,
            "crm_verified_at": now,
            "source": f"web_job:{context.job_id}",
        }
    )
    context.mark_crm_verified(result)


def _record_action_failure(
    context,
    report_section,
    preview,
    *,
    step,
    step_label,
    exc,
    extra=None,
):
    job = context.store.get_job(context.job_id) or {}
    milestones = job.get("result_payload", {})
    uncertain = (
        milestones.get("submit_clicked") or milestones.get("crm_id_captured")
        or milestones.get("routing_conflict")
        or getattr(exc, "status", "") in {"uncertain", "crm_id_captured", "evidence_error"}
        or (milestones.get("browser_started")
            and getattr(exc, "submission_phase", "") != "before_submit"
            and getattr(exc, "status", "") != "auth_expired")
    )
    payload = {
        "ok": False,
        "step": step,
        "status": "error",
        "message": str(exc),
        "artifacts": list(getattr(exc, "artifacts", []) or []),
        "service_customer": preview.customer,
        "content_digest": preview.content_digest,
        **(extra or {}),
    }
    payload.update({
        "status": "uncertain" if uncertain else "error",
        "submission_phase": "crm_id_captured" if milestones.get("crm_id_captured") else "submit_clicked" if milestones.get("submit_clicked") else "unknown" if uncertain else "before_submit",
        "retry_action": "verify_crm" if uncertain else "safe_retry",
    })
    for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"):
        value = milestones.get(key) or getattr(exc, key, "")
        if value:
            payload[key] = value
    for key in ONSITE_ROUTING_AUDIT_FIELDS:
        if key in milestones:
            payload[key] = deepcopy(milestones[key])
    target = milestones.get("routing_target")
    if target:
        target_id = target.get("workorder_id")
        if not target_id:
            raise AuthorizationMismatch("冻结关联目标缺少工单编号，已保留原证据。")
        if payload.get("workorder_id") and payload["workorder_id"] != target_id:
            context.merge_result({
                "status": "uncertain", "retry_action": "verify_crm",
                "routing_conflict": {
                    "expected_workorder_id": target_id,
                    "observed_workorder_id": payload["workorder_id"],
                    "observed_service_record_id": payload.get("service_record_id"),
                },
            })
            raise AuthorizationMismatch("失败回执工单与冻结关联目标不一致，已保留冲突证据，禁止直接重试。")
        # B already existed before this service operation. Retain its identity
        # for read-only recovery without pretending that a new record was made.
        payload["workorder_id"] = target_id
    if _has_captured_id(payload):
        payload.update(status="uncertain", submission_phase="crm_id_captured", retry_action="verify_crm")
    differences = getattr(exc, "differences", None)
    if differences:
        payload["differences"] = differences
    context.merge_result(payload)
    context.checkpoint_submission(result=payload)
    record_pending_fxiaoke_task(
        report_section,
        context.report_path,
        step,
        step_label,
        payload,
    )
