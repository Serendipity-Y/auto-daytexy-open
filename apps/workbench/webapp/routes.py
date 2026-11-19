from __future__ import annotations

import calendar
import json
import re
from datetime import date as local_date
from datetime import datetime, time as local_time, timedelta
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from packages.report_domain.report_format import (
    find_saved_report_section,
    normalize_report_date_to_iso,
    parse_report_fields,
    split_report_sections,
)
from packages.report_domain.workday import REMOTE_MAIN_SERVICE_START_DATE
from integrations.fxiaoke import crm_enabled
from integrations.fxiaoke import remote_workorder as fxiaoke_remote_workorder
from integrations.fxiaoke import service_record as fxiaoke_service_record
from .artifacts import build_cleanup_preview, execute_cleanup
from .china_holidays import (
    ADJUSTED_REST_DAYS,
    REST_STATUSES,
    automatic_day_statuses,
    is_china_workday,
)
from .config import CRM_DISABLED_MESSAGE
from .knowledge_base import (
    KNOWLEDGE_BASE_SYNC_ACTION,
    build_knowledge_base_sync_descriptor,
    parse_metadata_timestamp,
)
from .previews import (
    SUPPORTED_ACTIONS,
    authorize_preview,
    build_action_previews,
    build_source_sync_previews,
    remote_options_from_payload,
    review_source_sync_preview,
)
from .store import (
    CALENDAR_DAY_STATUS_LABELS,
    COMPANY_OFFICE_CALENDAR_DAY_STATUSES,
    CRM_VERIFICATION_ACTION,
    DELETABLE_JOB_RECORD_STATUSES,
    DELETABLE_CRM_VERIFICATION_STATUSES,
    MAIN_SERVICE_REVIEW_STATUSES,
    MANUAL_OVERRIDE_SOURCE_PREFIX,
    MANUAL_OVERRIDE_STEPS,
    SUBMITTED_BUSINESS_STATUSES,
)
from .remote_hours import (
    build_remote_hour_candidates,
    build_remote_hour_sync_descriptor,
    find_remote_hour_candidate,
)
from .remote_bindings import (
    build_remote_workorder_binding_descriptor,
    find_remote_workorder_binding_candidate,
)
from .verification import build_verification_jobs
from .customer_aliases import AliasConflict, customer_alias_snapshot, update_customer_alias


MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KNOWLEDGE_BASE_QUARTER_PATTERN = re.compile(r"^(\d{4})-Q([1-4])$")

STATUS_PRESENTATIONS = {
    "unknown": ("未提交", "未执行或没有成功回执"),
    "not_found": ("未提交", "CRM 只读校验未找到"),
    "local_recorded": ("已提交", "历史成功回执"),
    "submitted": ("已提交", "提交成功回执"),
    "awaiting_confirmation": ("未提交", "等待确认"),
    "queued": ("未提交", "执行已排队"),
    "running": ("未提交", "正在执行"),
    "crm_id_captured": ("未提交", "已获取编号，结果待确认"),
    "crm_verified": ("已提交", "CRM 只读校验通过"),
    "pending_retry": ("未提交", "执行失败或待补交"),
    "manual_confirmed": ("已提交", "人工确认"),
    "manual_conflict": ("已提交", "人工确认，CRM 未找到"),
    "uncertain": ("未提交", "结果待核实"),
    "conflict": ("未提交", "CRM 校验冲突"),
    "legacy_ambiguous": ("待核实", "现有证据无法确认主服务归属"),
    "not_required": ("无需提交", "日报没有要求主服务"),
    "skipped": ("未提交", "本次未执行"),
    "completed": ("已完成", "本机只读同步完成"),
}
STATUS_LABELS = {
    status: presentation[0] for status, presentation in STATUS_PRESENTATIONS.items()
}

STEP_LABELS = {
    "daily_log": "日报",
    "service_record": "技术服务记录",
    "remote_workorder": "远程工单",
}
DAILY_SUBMISSION_OPEN_TIME = local_time(hour=18, minute=30)
REPORT_SYNC_WINDOW_DAYS = 7
LIST_PAGE_SIZE = 50
SERVICE_MODE_FILTER_VALUES = frozenset({"all", "onsite", "remote"})
SERVICE_MODE_FILTER_LABELS = {
    "all": "全部方式",
    "onsite": "外勤",
    "remote": "远程",
}
SERVICE_MODE_PERIOD_LABELS = {
    "week": "所在周",
    "month": "所在月",
    "year": "所在年",
    "q1": "Q1",
    "q2": "Q2",
    "q3": "Q3",
    "q4": "Q4",
    "custom": "自定义日期",
}

ACTION_LABELS = {
    "daily_log": "补交日报",
    "onsite_service": "补交外勤服务",
    "remote_workorder": "新建远程工单",
    "remote_workorder_hours_sync": "同步远程工单工时",
    "remote_workorder_binding_verification": "复核替换远程工单",
    "crm_verification": "CRM 只读复核",
    "artifact_cleanup": "清理失败留痕",
    "knowledge_base_sync": "同步知识库索引",
}

RETRYABLE_JOB_STATUSES = frozenset({"pending_retry"})
POLLING_JOB_STATUSES = frozenset({"awaiting_confirmation", "queued", "running"})
WORKBENCH_COMPLETED_STATUSES = frozenset(
    {
        "local_recorded",
        "submitted",
        "crm_verified",
        "manual_confirmed",
        "manual_conflict",
    }
)
AUDIT_VERIFICATION_STATUSES = frozenset(
    {"crm_id_captured", "uncertain", "conflict", "not_found"}
)
AUDIT_GROUP_LABELS = {
    "missing_daily": "缺日报",
    "missing_service": "缺主技术服务",
    "verification": "待核实",
}
ARTIFACT_SORT_OPTIONS = {
    "created_desc": "最新优先",
    "size_asc": "大小：小到大",
    "size_desc": "大小：大到小",
}
KNOWLEDGE_BASE_SYNC_PRESENTATIONS = {
    "never": ("尚未导入", "首次导入只读取知识库页面元数据。"),
    "running": ("正在同步", "正在读取知识库页面元数据。"),
    "success": ("同步完成", "本机索引已更新。"),
    "failed": ("同步失败", "上次索引保留，可重新发起同步。"),
}


def create_web_blueprint(store, artifact_root=None, indexer=None) -> Blueprint:
    blueprint = Blueprint("web", __name__)
    cleanup_root = Path(
        artifact_root or (store.database_path.parent / "fxiaoke_failure_artifacts")
    )
    cleanup_additional_roots = (indexer.trace_root,) if getattr(indexer, "trace_root", None) else ()

    @blueprint.before_request
    def require_crm_for_external_actions():
        crm_endpoints = {
            "web.authorize_action", "web.verify_day", "web.update_remote_workorder_hours",
            "web.verify_replacement_remote_workorder", "web.retry_job",
        }
        requires_crm = request.endpoint in crm_endpoints or (
            request.endpoint == "web.source_sync" and request.path.endswith("/authorize")
        )
        if request.method == "POST" and requires_crm and not crm_enabled():
            return _json_error(CRM_DISABLED_MESSAGE, 403)
        return None

    @blueprint.route("/customer-aliases", methods=["GET", "POST"])
    def customer_aliases_page():
        config = current_app.config["WEB_CONFIG"]
        alias_path = current_app.config.get("CUSTOMER_ALIAS_PATH")
        if alias_path is None:
            alias_path = (
                config.project_root / "daily_report_customer_aliases.json"
                if config.testing
                else fxiaoke_service_record.DEFAULT_CUSTOMER_ALIAS_PATH
            )
        error = ""
        status = 200
        if request.method == "POST":
            try:
                update_customer_alias(alias_path, request.form)
            except AliasConflict as exc:
                error, status = str(exc), 409
            except (ValueError, OSError, RuntimeError) as exc:
                error, status = str(exc), 400
            else:
                return redirect(url_for("web.customer_aliases_page", saved="1"), code=303)
        try:
            snapshot = customer_alias_snapshot(alias_path)
        except (ValueError, OSError, RuntimeError) as exc:
            snapshot = {"rows": [], "revision": ""}
            error, status = str(exc), 400
        return render_template(
            "customer_aliases.html", active_page="customer_aliases", **snapshot,
            error=error, saved=request.args.get("saved") == "1",
        ), status

    @blueprint.get("/")
    def calendar_page():
        _refresh_index(indexer)
        now = datetime.now().astimezone()
        selected_month = _parse_month(request.args.get("month"))
        issue_type = _parse_issue_type(request.args.get("issue_type"))
        issues_only = (
            _parse_issues_only(request.args.get("issues"))
            or issue_type is not None
        )
        today = now.date()
        service_mode_filter = _parse_service_mode_filter(
            request.args,
            today=today,
        )
        if service_mode_filter is not None:
            issues_only = False
            issue_type = None
        days = store.list_month(selected_month.year, selected_month.month)
        days_by_date = {item["report_date"]: _decorate_day(item) for item in days}
        _, month_last_day = calendar.monthrange(selected_month.year, selected_month.month)
        month_start_date = selected_month.isoformat()
        month_end_date = selected_month.replace(day=month_last_day).isoformat()
        month_automatic_statuses = automatic_day_statuses(
            month_start_date,
            month_end_date,
        )
        month_manual_statuses_by_date = {
            item["report_date"]: item
            for item in store.list_calendar_day_statuses(
                month_start_date,
                month_end_date,
            )
        }
        month_non_workday_dates = {
            report_date
            for report_date, status in month_automatic_statuses.items()
            if status["status"] in REST_STATUSES
        }
        month_work_summary = _build_month_work_summary(
            days,
            month_manual_statuses_by_date.values(),
            official_statuses=month_automatic_statuses.values(),
        )
        all_month_issues = _build_month_issue_list(
            selected_month,
            days_by_date,
            calendar_status_dates=(
                set(month_manual_statuses_by_date) | month_non_workday_dates
            ),
            today=today,
            now=now,
        )
        month_issues = [
            item
            for item in all_month_issues
            if issue_type is None or item["group_key"] == issue_type
        ]
        weeks = []
        if not issues_only:
            calendar_dates = _calendar_month_dates(selected_month)
            visible_day_rows = store.list_report_days(
                calendar_dates[0][0].isoformat(),
                calendar_dates[-1][-1].isoformat(),
            )
            visible_manual_statuses_by_date = {
                item["report_date"]: item
                for item in store.list_calendar_day_statuses(
                    calendar_dates[0][0].isoformat(),
                    calendar_dates[-1][-1].isoformat(),
                )
            }
            visible_automatic_statuses = automatic_day_statuses(
                calendar_dates[0][0].isoformat(),
                calendar_dates[-1][-1].isoformat(),
            )
            visible_statuses_by_date = {
                **visible_automatic_statuses,
                **visible_manual_statuses_by_date,
            }
            weeks = _build_calendar_weeks(
                selected_month,
                days_by_date,
                occupied_dates={
                    *(item["report_date"] for item in visible_day_rows),
                    *visible_manual_statuses_by_date,
                    *(
                        report_date
                        for report_date, status in visible_automatic_statuses.items()
                        if status["status"]
                        in REST_STATUSES
                    ),
                },
                calendar_statuses_by_date=visible_statuses_by_date,
            )
        previous_month = _shift_month(selected_month, -1).strftime("%Y-%m")
        next_month = _shift_month(selected_month, 1).strftime("%Y-%m")
        service_mode_results = (
            _build_service_mode_results(store, service_mode_filter)
            if service_mode_filter is not None
            else None
        )
        return render_template(
            "calendar.html",
            active_page="calendar",
            month=selected_month.strftime("%Y-%m"),
            month_title=f"{selected_month.year} 年 {selected_month.month} 月",
            previous_month=previous_month,
            next_month=next_month,
            weeks=weeks,
            indexed_count=sum(
                1 for item in days if not item.get("is_date_attribution")
            ),
            month_work_summary=month_work_summary,
            calendar_day_status_options=[
                {"value": value, "label": label}
                for value, label in CALENDAR_DAY_STATUS_LABELS.items()
            ],
            issues_only=issues_only,
            full_calendar_url=_calendar_page_url(selected_month.strftime("%Y-%m")),
            issues_only_url=_calendar_page_url(
                selected_month.strftime("%Y-%m"),
                issues_only=True,
            ),
            previous_month_url=_calendar_page_url(
                previous_month,
                issues_only=issues_only,
                issue_type=issue_type,
                service_mode_filter=service_mode_filter,
            ),
            next_month_url=_calendar_page_url(
                next_month,
                issues_only=issues_only,
                issue_type=issue_type,
                service_mode_filter=service_mode_filter,
            ),
            issue_type=issue_type,
            issue_type_filters=_build_issue_type_filters(
                selected_month.strftime("%Y-%m"),
                all_month_issues,
                active_type=issue_type,
            ),
            month_issue_total=len(all_month_issues),
            month_issues=month_issues,
            recent_audit=_build_recent_audit(
                store,
                today=today,
                now=now,
            ),
            adjusted_rest_schedule_available=selected_month.year in ADJUSTED_REST_DAYS,
            service_mode_form=_build_service_mode_filter_form(
                service_mode_filter,
                selected_month=selected_month,
            ),
            service_mode_periods=SERVICE_MODE_PERIOD_LABELS,
            service_mode_results=service_mode_results,
            service_mode_clear_url=_calendar_page_url(
                selected_month.strftime("%Y-%m")
            ),
        )

    @blueprint.get("/report-sync/candidates")
    def report_sync_candidates():
        try:
            candidates = _build_report_sync_candidates(
                _resolve_report_path(indexer),
                today=datetime.now().astimezone().date(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        return jsonify({"ok": True, "candidates": candidates})

    @blueprint.get("/report-sync/open")
    def open_report_sync_candidate():
        try:
            report_date = _parse_date_for_json(request.args.get("report_date"))
            candidates = _build_report_sync_candidates(
                _resolve_report_path(indexer),
                today=datetime.now().astimezone().date(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        if report_date not in {item["report_date"] for item in candidates}:
            return _json_error("只能选择日报.md 扫描出的最近 7 日日报。", 400)
        _refresh_index(indexer)
        if store.get_day(report_date) is None:
            return _json_error("已读取日报源文件，但尚不能生成该日期的补交预览。", 409)
        return jsonify(
            {
                "ok": True,
                "day_url": url_for(
                    "web.day_detail",
                    date=report_date,
                    source_sync="1",
                ),
            }
        )

    @blueprint.get("/days/<date>")
    def day_detail(date: str):
        _refresh_index(indexer)
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        if day.get("attribution_target_date"):
            return redirect(
                url_for("web.day_detail", date=day["attribution_target_date"])
            )

        report_text = ""
        report_error = ""
        if not day.get("is_date_attribution"):
            try:
                report_text = find_saved_report_section(
                    Path(day["report_path"]), report_date
                )
            except (OSError, RuntimeError, ValueError) as exc:
                report_error = str(exc)

        remote_candidates_by_identity = {}
        if report_text:
            try:
                remote_candidates_by_identity = {
                    candidate.identity: candidate
                    for candidate in build_remote_hour_candidates(day, report_text)
                }
            except (RuntimeError, ValueError):
                remote_candidates_by_identity = {}

        decorated = _decorate_day(day)
        related_job_ids = {
            str(item.get(field) or "")
            for item in day.get("submissions", [])
            for field in ("remote_hour_sync_job_id", "remote_workorder_binding_job_id")
        }
        related_job_ids.update(
            str(item.get("source", "")).removeprefix("crm_verification:")
            for item in day.get("submissions", [])
            if str(item.get("source", "")).startswith("crm_verification:")
        )
        jobs = store.list_jobs(report_date=report_date, related_job_ids=related_job_ids)
        jobs_by_id = {job["job_id"]: job for job in jobs}
        binding_jobs_by_digest: dict[str, dict] = {}
        for job in jobs:
            payload = job.get("request_payload", {})
            if (
                job.get("action") == "remote_workorder_binding_verification"
                and payload.get("report_date") == report_date
            ):
                digest = str(payload.get("content_digest", "") or "").strip()
                if digest:
                    binding_jobs_by_digest.setdefault(digest, job)
        manual_override_steps = {
            item["step"]
            for item in day.get("submissions", [])
            if str(item.get("source", "")).startswith(MANUAL_OVERRIDE_SOURCE_PREFIX)
        }
        manual_action_steps = {"daily_log"}
        if day["is_onsite"]:
            manual_action_steps.add("service_record")
        displayed_manual_steps = set()
        remote_as_technical_service = _uses_remote_as_technical_service(day)
        decorated_submissions = []
        for item in day.get("submissions", []):
            binding_job = binding_jobs_by_digest.get(
                str(item.get("content_digest") or "").strip()
            )
            hour_job = jobs_by_id.get(
                str(item.get("remote_hour_sync_job_id") or "").strip()
            )
            remote_job = max(
                (job for job in (binding_job, hour_job) if job is not None),
                key=lambda job: (job.get("created_at") or "", job["job_id"]),
                default=None,
            )
            candidate = remote_candidates_by_identity.get(
                (
                    str(item.get("content_digest") or "").strip(),
                    str(item.get("workorder_id") or "").strip(),
                )
            )
            decorated_item = _decorate_submission(
                item,
                remote_candidate=candidate,
                remote_job=remote_job,
                remote_as_technical_service=remote_as_technical_service,
            )
            verification_job_id = _failed_crm_verification_job_id(
                decorated_item,
                jobs_by_id,
                report_date,
            )
            decorated_item["crm_verification_failure_deletable"] = bool(
                verification_job_id
            )
            decorated_item["crm_verification_job_id"] = verification_job_id
            step = decorated_item["step"]
            if (
                not day.get("is_date_attribution")
                and step in manual_action_steps
                and step not in displayed_manual_steps
            ):
                decorated_item["manual_status_editable"] = True
                decorated_item["manual_override"] = step in manual_override_steps
                displayed_manual_steps.add(step)
            decorated_submissions.append(decorated_item)
        decorated["submissions"] = decorated_submissions
        decorated["artifacts"] = [
            _decorate_artifact(item) for item in day.get("artifacts", [])
        ]
        verification_error = ""
        remote_verification_targets = []
        try:
            remote_verification_targets = [
                item for item in build_verification_jobs(report_date, day=day, steps=("remote_workorder",))
                if item["step"] == "remote_workorder"
            ]
        except (OSError, RuntimeError, ValueError) as exc:
            verification_error = f"无法生成远程工单复核对象：{exc}"
        return render_template(
            "day_detail.html",
            active_page="calendar",
            day=decorated,
            report_text=report_text,
            report_error=report_error,
            auto_source_sync=request.args.get("source_sync") == "1",
            remote_verification_targets=remote_verification_targets,
            verification_error=verification_error,
        )

    @blueprint.post("/date-attributions")
    def create_date_attribution():
        try:
            source_date = _parse_date_for_json(request.form.get("source_date"))
            target_date = _parse_date_for_json(request.form.get("target_date"))
            attribution = store.create_date_attribution(source_date, target_date)
        except KeyError as exc:
            return _json_error(str(exc), 404)
        except ValueError as exc:
            return _json_error(str(exc), 409)
        return jsonify({"ok": True, "attribution": attribution})

    @blueprint.post("/date-attributions/<date>/revoke")
    def revoke_date_attribution(date: str):
        try:
            attribution = store.clear_date_attribution(_parse_date(date))
        except KeyError as exc:
            return _json_error(str(exc), 404)
        return jsonify({"ok": True, "attribution": attribution})

    @blueprint.post("/calendar-day-statuses")
    def set_calendar_day_status():
        try:
            status = store.set_calendar_day_status(
                _parse_date_for_json(request.form.get("report_date")),
                request.form.get("status", ""),
            )
        except ValueError as exc:
            return _json_error(str(exc), 409)
        return jsonify({"ok": True, "calendar_day_status": status})

    @blueprint.post("/calendar-day-statuses/<date>/clear")
    def clear_calendar_day_status(date: str):
        try:
            status = store.clear_calendar_day_status(_parse_date(date))
        except KeyError as exc:
            return _json_error(str(exc), 404)
        return jsonify({"ok": True, "calendar_day_status": status})

    @blueprint.post("/days/<date>/preview")
    def preview_action(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        action = str(request.form.get("action", "")).strip()
        if action not in SUPPORTED_ACTIONS:
            return _json_error("不支持的操作类型。", 400)
        customer = str(request.form.get("customer", "")).strip()
        hours = str(request.form.get("hours", "")).strip()
        if hours and not customer:
            return _json_error("填写工时时必须指定对应客户。", 400)
        if action == "remote_workorder" and hours:
            try:
                hours = fxiaoke_remote_workorder.normalize_editable_remote_support_hours(
                    hours
                )
            except ValueError as exc:
                return _json_error(str(exc), 400)
        hours_by_customer = {customer: hours} if customer and hours else None
        try:
            previews = build_action_previews(
                day["report_path"],
                report_date,
                action,
                hours_by_customer=hours_by_customer,
                remote_options_by_customer=_remote_options_from_form(customer, action),
                **_remote_role_from_form(customer, action),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        return jsonify(
            {
                "ok": True,
                "previews": [_serialize_preview(item) for item in previews],
            }
        )

    @blueprint.post("/actions/authorize")
    def authorize_action():
        try:
            report_date = _parse_date_for_json(request.form.get("report_date"))
        except ValueError as exc:
            return _json_error(str(exc), 400)
        day = store.get_day(report_date)
        if day is None:
            return _json_error("本地索引中不存在该日期。", 404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        action = str(request.form.get("action", "")).strip()
        customer = str(request.form.get("customer", "")).strip()
        digest = str(request.form.get("authorization_digest", "")).strip()
        hours = str(request.form.get("hours", "")).strip()
        if action not in SUPPORTED_ACTIONS or not customer or not digest:
            return _json_error("授权参数不完整。", 400)
        if action == "remote_workorder" and hours:
            try:
                hours = fxiaoke_remote_workorder.normalize_editable_remote_support_hours(
                    hours
                )
            except ValueError as exc:
                return _json_error(str(exc), 400)
        hours_by_customer = {customer: hours} if hours else None
        try:
            previews = build_action_previews(
                day["report_path"],
                report_date,
                action,
                hours_by_customer=hours_by_customer,
                remote_options_by_customer=_remote_options_from_form(customer, action),
                **_remote_role_from_form(customer, action),
            )
            preview = _match_preview(previews, customer, digest)
            job_id = authorize_preview(store, preview, digest)
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        return _job_created_response(job_id)

    @blueprint.post("/days/<date>/source-sync/preview")
    @blueprint.post("/days/<date>/source-sync/authorize")
    def source_sync(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            return _json_error("本地索引中不存在该日期。", 404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        confirming = request.path.endswith("/authorize")
        try:
            required, optional, warning = build_source_sync_previews(day["report_path"], report_date)
            required_customers = {
                _source_sync_remote_customer(item) for item in required if item.action == "remote_workorder"
            }
            if not confirming:
                previews = [dict(_serialize_preview(item), required=True) for item in required]
                try:
                    previews.extend([
                        dict(
                            _serialize_preview(item), required=False,
                            selection_key=_source_sync_remote_customer(item),
                            selection_blocked=_source_sync_remote_conflict(item, required_customers),
                        )
                        for item in optional
                    ])
                except (OSError, RuntimeError, ValueError) as exc:
                    warning = f"可选远程工单预览失败，已跳过；不影响日报与主服务：{exc}"
                return jsonify({"ok": True, "previews": previews, "warning": warning})

            selections = _source_sync_selections()
            current = {item.authorization_digest: item for item in (*required, *optional)}
            if selections.keys() - current.keys():
                raise ValueError("日报源文件已变化或已选预览不可用，请重新从源文件同步。")
            if any(item.authorization_digest not in selections for item in required):
                raise ValueError("缺少必选的日报或主服务，或者源文件已变化，请重新从源文件同步。")
            selected_customers = set(required_customers)
            for item in optional:
                if item.authorization_digest not in selections:
                    continue
                conflict = _source_sync_remote_conflict(item, selected_customers)
                if conflict:
                    raise ValueError(conflict)
                selected_customers.add(_source_sync_remote_customer(item))
            reviewed = [
                review_source_sync_preview(item, selections[digest])
                for digest, item in current.items() if digest in selections
            ]
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409 if confirming else 400)

        job_ids = []
        for preview in reviewed:
            try:
                job_ids.append(authorize_preview(store, preview, preview.authorization_digest))
            except (OSError, RuntimeError, ValueError) as exc:
                message = str(exc) if not job_ids else f"前 {len(job_ids)} 项已排队，请先在任务页核对。后续排队停止：{exc}"
                return _json_error(message, 409)
        return jsonify({"ok": True, "job_ids": job_ids, "job_url": url_for("web.jobs_page")}), 202

    @blueprint.post("/days/<date>/verify")
    def verify_day(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        step = str(request.form.get("step", "")).strip()
        try:
            descriptors = build_verification_jobs(
                report_date, day=day, steps=(step,), source_jobs=store.list_jobs(report_date=report_date),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(f"无法生成复核对象：{exc}", 409)
        matching = [item for item in descriptors if item["step"] == step]
        if not matching:
            return _json_error("不支持的复核类型。", 400)
        unique_key = str(request.form.get("unique_key", "")).strip()
        if unique_key:
            matching = [item for item in matching if item["unique_key"] == unique_key]
            if len(matching) != 1:
                return _json_error("复核对象已变化，请刷新页面后重新选择。", 409)
        elif len(matching) != 1:
            return _json_error("存在多个远程工单复核对象，请先选择客户和工单编号。", 409)
        descriptor = matching[0]
        if descriptor.get("verification_conflict"):
            return _json_error(descriptor["verification_conflict"], 409)
        job_id = store.create_or_get_queued_job(
            {
                "job_key": descriptor["unique_key"],
                "action": descriptor["action"],
                "authorized_digest": descriptor["authorization_digest"],
                "request_payload": descriptor,
            }
        )
        cleanup = {"deleted_verification_count": 0}
        if step != "remote_workorder":
            cleanup = store.delete_failed_crm_verifications(
                report_date=report_date,
                step=step,
            )
        return _job_created_response(
            job_id,
            deleted_failed_verifications=cleanup["deleted_verification_count"],
        )

    @blueprint.post("/days/<date>/manual-override")
    def apply_manual_override(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        step = str(request.form.get("step", "")).strip()
        if step not in MANUAL_OVERRIDE_STEPS:
            return _json_error("不支持人工更正的记录类型。", 400)
        if step == "service_record" and not day["is_onsite"]:
            return _json_error("非外勤日报不能更正技术服务记录。", 409)
        store.set_manual_submission_override(
            report_date,
            step,
            day["customer_name"],
        )
        return jsonify({"ok": True})

    @blueprint.post("/days/<date>/manual-override/revoke")
    def revoke_manual_override(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        step = str(request.form.get("step", "")).strip()
        if step not in MANUAL_OVERRIDE_STEPS:
            return _json_error("不支持人工更正的记录类型。", 400)
        if step == "service_record" and not day["is_onsite"]:
            return _json_error("非外勤日报不能更正技术服务记录。", 409)
        store.clear_manual_submission_override(report_date, step)
        return jsonify({"ok": True})

    @blueprint.post("/days/<date>/crm-verifications/<job_id>/delete")
    def delete_failed_crm_verification(date: str, job_id: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        try:
            result = store.delete_failed_crm_verification(
                report_date=report_date,
                job_id=job_id,
            )
        except KeyError:
            abort(404)
        except ValueError as exc:
            return _json_error(str(exc), 409)
        return jsonify({"ok": True, **result})

    @blueprint.post("/days/<date>/remote-hours")
    def update_remote_workorder_hours(date: str):
        report_date = _parse_date(date)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        content_digest = str(request.form.get("content_digest", "")).strip()
        workorder_id = str(request.form.get("workorder_id", "")).strip()
        if not fxiaoke_service_record.JSFW_ID_PATTERN.fullmatch(workorder_id):
            return _json_error("缺少有效 JSFW 编号，不能同步 CRM。", 400)
        try:
            target_hours = fxiaoke_remote_workorder.normalize_editable_remote_support_hours(
                request.form.get("service_hours", "")
            )
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
            if not candidate.editable or not candidate.can_sync:
                return _json_error(
                    candidate.sync_reason
                    or candidate.lock_reason
                    or "该远程工单不可同步 CRM。",
                    409,
                )
            descriptor = build_remote_hour_sync_descriptor(candidate, target_hours)
            job_id, _created = store.queue_remote_workorder_hour_sync(
                descriptor,
                base_service_hours=candidate.base_service_hours,
                target_hours=target_hours,
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            return _json_error(str(exc), 409)
        return (
            jsonify(
                {
                    "ok": True,
                    "job_id": job_id,
                    "poll_url": url_for("web.job_api", job_id=job_id),
                    "workorder_id": candidate.workorder_id,
                    "confirmed_hours": candidate.confirmed_hours,
                    "target_hours": float(target_hours),
                }
            ),
            202,
        )

    @blueprint.post("/days/<date>/remote-workorder-binding")
    def verify_replacement_remote_workorder(date: str):
        report_date = _parse_date(date)
        replacement_id = str(
            request.form.get("replacement_workorder_id", "")
        ).strip()
        if not fxiaoke_service_record.JSFW_ID_PATTERN.fullmatch(replacement_id):
            return _json_error("替换工单必须是有效的 JSFW 编号。", 400)
        day = store.get_day(report_date)
        if day is None:
            abort(404)
        blocked = _date_attribution_action_error(day)
        if blocked is not None:
            return blocked
        content_digest = str(request.form.get("content_digest", "")).strip()
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
            job_id = store.create_or_get_queued_job(
                {
                    "job_key": descriptor["unique_key"],
                    "action": descriptor["action"],
                    "authorized_digest": descriptor["authorization_digest"],
                    "request_payload": descriptor,
                }
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            return _json_error(str(exc), 409)
        return _job_created_response(job_id)

    @blueprint.get("/knowledge-base")
    def knowledge_base_page():
        selected_quarter = _parse_knowledge_base_quarter(
            request.args.get("quarter")
        )
        today = local_date.today()
        current_quarter = (today.year, (today.month - 1) // 3 + 1)
        quarter_rows = store.list_knowledge_base_quarters()
        if selected_quarter is None:
            selected_quarter = current_quarter
        selected_year, selected_quarter_number = selected_quarter
        pages = store.list_knowledge_base_pages(
            year=selected_year,
            quarter=selected_quarter_number,
        )
        for page in pages:
            for source, label in (("created_at", "created_date"), ("modified_at", "modified_date")):
                try:
                    timestamp = parse_metadata_timestamp(page.get(source))
                except OverflowError:
                    timestamp = None
                page[label] = timestamp.date().isoformat() if timestamp else "—"
        quarter_filters = _build_knowledge_base_quarter_filters(
            quarter_rows,
            selected_quarter,
            current_quarter=current_quarter,
        )
        sync_state = _decorate_knowledge_base_sync_state(
            store.get_knowledge_base_sync_state(),
            store.list_jobs_by_status({"queued", "running"}),
        )
        kpi_target = current_app.config["WEB_CONFIG"].knowledge_base_quarterly_target
        return render_template(
            "knowledge_base.html",
            active_page="knowledge_base",
            pages=pages,
            selected_quarter_value=_knowledge_base_quarter_value(
                selected_year,
                selected_quarter_number,
            ),
            selected_quarter_label=(
                f"{selected_year} 年第 {selected_quarter_number} 季度"
            ),
            summary_quarter_label="本季度" if selected_quarter == current_quarter else "所选季度",
            quarter_filters=quarter_filters,
            kpi_target=kpi_target,
            kpi_count=len(pages),
            kpi_percent=min(100, round(len(pages) / kpi_target * 100)) if kpi_target else None,
            total_indexed_count=store.count_knowledge_base_pages(),
            sync_state=sync_state,
            latest_backup=_decorate_knowledge_base_backup(
                (store.list_knowledge_base_backups(limit=1) or [None])[0]
            ),
        )

    @blueprint.post("/knowledge-base/sync")
    def sync_knowledge_base_index():
        mode = str(request.form.get("mode") or "incremental").strip().lower()
        try:
            descriptor = build_knowledge_base_sync_descriptor(mode=mode)
        except ValueError as exc:
            return _json_error(str(exc), 400)
        active_job = max(
            (
                job
                for job in store.list_jobs_by_status({"queued", "running"})
                if job.get("action") == KNOWLEDGE_BASE_SYNC_ACTION
            ),
            key=lambda job: (job.get("created_at", ""), job.get("job_id", "")),
            default=None,
        )
        if active_job is not None:
            return _job_created_response(active_job["job_id"])
        job_id = store.create_or_get_queued_job(
            {
                "job_key": descriptor["unique_key"],
                "action": descriptor["action"],
                "authorized_digest": descriptor["authorization_digest"],
                "request_payload": descriptor,
            }
        )
        return _job_created_response(job_id)

    @blueprint.get("/jobs")
    def jobs_page():
        focus_job_id = str(request.args.get("focus", "")).strip()
        page = _parse_page_number(request.args.get("page"))
        if focus_job_id and request.args.get("page") is None:
            page = store.job_page_number(focus_job_id, LIST_PAGE_SIZE)
        pagination = _list_pagination(
            "web.jobs_page", store.count_jobs(), page, focus=focus_job_id
        )
        jobs = [
            _decorate_job(item)
            for item in store.list_jobs(
                limit=LIST_PAGE_SIZE,
                offset=(pagination["page"] - 1) * LIST_PAGE_SIZE,
            )
        ]
        return render_template(
            "jobs.html",
            active_page="jobs",
            jobs=jobs,
            focus_job_id=focus_job_id,
            pagination=pagination,
        )

    @blueprint.get("/api/jobs/<job_id>")
    def job_api(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            abort(404)
        return jsonify(_serialize_job(job))

    @blueprint.post("/jobs/<job_id>/delete")
    def delete_job_record(job_id: str):
        try:
            result = store.delete_job_record(job_id)
        except KeyError:
            abort(404)
        except ValueError as exc:
            return _json_error(str(exc), 409)
        except RuntimeError as exc:
            return _json_error(str(exc), 409)
        return jsonify(
            {
                "ok": True,
                **result,
                "message": "本机任务记录已删除；待补交队列和失败留痕已保留。",
            }
        )

    @blueprint.post("/jobs/<job_id>/confirm-customer-alias")
    def confirm_remote_workorder_customer_alias(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            abort(404)
        request_payload = job.get("request_payload", {})
        result_payload = job.get("result_payload", {})
        if not (
            job.get("action") == "remote_workorder_hours_sync"
            and job.get("status") == "conflict"
            and result_payload.get("conflict_kind") == "customer"
        ):
            return _json_error("只能确认远程工单的客户校验冲突。", 409)
        report_customer = str(
            request_payload.get("report_customer", "") or ""
        ).strip()
        expected_customer = str(request_payload.get("customer", "") or "").strip()
        conflict_expected = str(
            result_payload.get("conflict_expected", "") or ""
        ).strip()
        observed_customer = str(
            result_payload.get("conflict_observed", "") or ""
        ).strip()
        if (
            not report_customer
            or not observed_customer
            or conflict_expected != expected_customer
        ):
            return _json_error("客户冲突审计信息不完整，不能写入别名。", 409)
        fxiaoke_service_record.remember_customer_alias_mapping(
            report_customer,
            observed_customer,
            explicitly_confirmed=True,
        )
        return jsonify(
            {
                "ok": True,
                "alias": report_customer,
                "primary_customer": observed_customer,
            }
        )

    @blueprint.post("/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            abort(404)
        if job["status"] not in RETRYABLE_JOB_STATUSES:
            return _json_error("该任务不允许直接重试，只能发起 CRM 复核。", 409)
        request_payload = job.get("request_payload", {})
        action = str(job.get("action", "")).strip()
        if action not in SUPPORTED_ACTIONS:
            return _json_error("该历史任务不是可重试的提交操作。", 409)
        report_date = str(request_payload.get("report_date", "")).strip()
        day = store.get_day(report_date)
        if day is None:
            return _json_error("本地索引中不存在该任务日期。", 409)
        customer = str(request_payload.get("customer", "")).strip()
        hours = request_payload.get("hours")
        hours_by_customer = {customer: hours} if customer and hours is not None else None
        browser_mode = str(request.form.get("browser_mode", "background")).strip()
        if browser_mode not in {"background", "visible"}:
            return _json_error("浏览器模式无效。", 400)
        digest = str(request.form.get("authorization_digest", "")).strip()
        try:
            previews = build_action_previews(
                day["report_path"],
                report_date,
                action,
                hours_by_customer=hours_by_customer,
                remote_options_by_customer=(
                    remote_options_from_payload(customer, request_payload.get("payload"))
                    if action == "remote_workorder" else None
                ),
            )
            preview = _match_preview(previews, customer, digest)
            if preview.content_digest != request_payload.get("content_digest"):
                raise ValueError("日报内容已变化，请从日期详情重新生成预览。")
            retry_job_id = authorize_preview(
                store,
                preview,
                digest,
                retry_of_job_id=job_id,
                show_browser=browser_mode == "visible",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        return _job_created_response(retry_job_id)

    @blueprint.get("/artifacts")
    def artifacts_page():
        totals = store.active_artifact_totals()
        selected_type = str(request.args.get("type", "")).strip()
        selected_sort = str(request.args.get("sort", "created_desc")).strip() or "created_desc"
        artifact_types = [item["step"] for item in totals]
        if selected_type and selected_type not in artifact_types:
            abort(400)
        if selected_sort not in ARTIFACT_SORT_OPTIONS:
            abort(400)

        selected_count = sum(
            item["count"] for item in totals if not selected_type or item["step"] == selected_type
        )
        pagination = _list_pagination(
            "web.artifacts_page", selected_count,
            _parse_page_number(request.args.get("page")),
            type=selected_type, sort=selected_sort,
        )
        artifacts = [
            _decorate_artifact(item)
            for item in store.list_artifacts(
                active_only=True, step=selected_type, sort=selected_sort,
                limit=LIST_PAGE_SIZE, offset=(pagination["page"] - 1) * LIST_PAGE_SIZE,
            )
        ]
        total_bytes = sum(item["total_bytes"] for item in totals)
        protected_count = sum(item["protected_count"] for item in totals)
        return render_template(
            "artifacts.html",
            active_page="artifacts",
            artifacts=artifacts,
            artifact_types=[
                {"value": step, "label": STEP_LABELS.get(step, step)}
                for step in artifact_types
            ],
            selected_artifact_type=selected_type,
            selected_artifact_sort=selected_sort,
            artifact_sort_options=ARTIFACT_SORT_OPTIONS,
            active_artifact_count=sum(item["count"] for item in totals),
            total_size=_format_size(total_bytes),
            protected_count=protected_count,
            pagination=pagination,
        )

    @blueprint.post("/artifacts/cleanup/preview")
    def preview_artifact_cleanup():
        try:
            _refresh_index(indexer, require_pending_snapshot=True)
            preview = build_cleanup_preview(store, cleanup_root, additional_roots=cleanup_additional_roots)
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        return jsonify(
            {
                "ok": True,
                "candidate_count": len(preview.candidates),
                "total_bytes": preview.total_bytes,
                "total_size": _format_size(preview.total_bytes),
                "preview_digest": preview.digest,
                "candidates": [
                    {
                        "artifact_id": item.artifact_id,
                        "file_name": item.file_path.name,
                        "size": _format_size(item.size_bytes),
                    }
                    for item in preview.candidates
                ],
            }
        )

    @blueprint.post("/artifacts/cleanup/confirm")
    def confirm_artifact_cleanup():
        digest = str(request.form.get("preview_digest", "")).strip()
        if not digest:
            return _json_error("缺少清理预览摘要。", 400)
        try:
            _refresh_index(indexer, require_pending_snapshot=True)
            result = execute_cleanup(store, cleanup_root, digest, additional_roots=cleanup_additional_roots)
        except (OSError, RuntimeError, ValueError) as exc:
            return _json_error(str(exc), 409)
        return jsonify(
            {
                "ok": True,
                "deleted_count": result.deleted_count,
                "deleted_bytes": result.deleted_bytes,
                "deleted_size": _format_size(result.deleted_bytes),
                "deleted_at": result.deleted_at,
            }
        )

    return blueprint


def _parse_page_number(value: str | None) -> int:
    if value is None:
        return 1
    if not re.fullmatch(r"[1-9]\d{0,6}", value):
        abort(400)
    return int(value)


def _list_pagination(endpoint: str, total_count: int, page: int, **query) -> dict:
    page_count = max(1, (total_count + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = min(page, page_count)
    return {
        "page": page, "page_count": page_count, "total_count": total_count,
        "previous_url": url_for(endpoint, page=page - 1, **query) if page > 1 else None,
        "next_url": url_for(endpoint, page=page + 1, **query) if page < page_count else None,
    }


def _parse_month(value: str | None) -> local_date:
    if value is None:
        today = local_date.today()
        return today.replace(day=1)
    if not MONTH_PATTERN.fullmatch(value):
        abort(400)
    try:
        parsed = datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError:
        abort(400)
    if (parsed.year, parsed.month) in {(1, 1), (9999, 12)}:
        abort(400)
    return parsed


def _parse_knowledge_base_quarter(value: str | None) -> tuple[int, int] | None:
    if value in (None, ""):
        return None
    matched = KNOWLEDGE_BASE_QUARTER_PATTERN.fullmatch(value)
    if matched is None:
        abort(400)
    year, quarter = int(matched.group(1)), int(matched.group(2))
    if year < 1:
        abort(400)
    return year, quarter


def _knowledge_base_quarter_value(year: int, quarter: int) -> str:
    return f"{int(year):04d}-Q{int(quarter)}"


def _build_knowledge_base_quarter_filters(
    quarter_rows: list[dict],
    selected_quarter: tuple[int, int],
    *,
    current_quarter: tuple[int, int] | None = None,
) -> list[dict]:
    counts = {
        (int(row["year"]), int(row["quarter"])): int(row["count"])
        for row in quarter_rows
    }
    counts.setdefault(selected_quarter, 0)
    if current_quarter is not None:
        counts.setdefault(current_quarter, 0)
    filters = []
    for (year, quarter), count in sorted(counts.items(), reverse=True):
        value = _knowledge_base_quarter_value(year, quarter)
        filters.append(
            {
                "value": value,
                "label": f"{year} 年 Q{quarter}",
                "count": count,
                "active": (year, quarter) == selected_quarter,
                "url": url_for("web.knowledge_base_page", quarter=value),
            }
        )
    return filters


def _decorate_knowledge_base_sync_state(state: dict, jobs: list[dict]) -> dict:
    decorated = dict(state)
    active_job = max(
        (
            item
            for item in jobs
            if item.get("action") == KNOWLEDGE_BASE_SYNC_ACTION
            and item.get("status") in {"queued", "running"}
        ),
        key=lambda item: (item.get("created_at", ""), item.get("job_id", "")),
        default=None,
    )
    if active_job is not None:
        status = str(active_job["status"])
        decorated.update(
            {
                "status": status,
                "label": "同步任务已排队" if status == "queued" else "正在同步",
                "detail": "正在等待本机只读同步任务完成。",
                "job_id": active_job["job_id"],
                "poll_url": url_for("web.job_api", job_id=active_job["job_id"]),
            }
        )
        return decorated
    status = str(decorated.get("status") or "never")
    label, detail = KNOWLEDGE_BASE_SYNC_PRESENTATIONS.get(
        status,
        ("同步状态未知", "请重新发起知识库同步。"),
    )
    decorated.update(
        {
            "status": status,
            "label": label,
            "detail": detail,
            "job_id": "",
            "poll_url": "",
        }
    )
    return decorated


def _decorate_knowledge_base_backup(backup: dict | None) -> dict | None:
    if backup is None:
        return None
    decorated = dict(backup)
    decorated["size_label"] = _format_size(int(decorated.get("size_bytes") or 0))
    return decorated


def _parse_issues_only(value: str | None) -> bool:
    if value is None:
        return False
    if value == "1":
        return True
    abort(400)


def _parse_issue_type(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if value in AUDIT_GROUP_LABELS:
        return value
    abort(400)


def _parse_service_mode_filter(arguments, *, today: local_date) -> dict | None:
    period = str(arguments.get("service_period") or "").strip().lower()
    if not period:
        return None
    if period not in SERVICE_MODE_PERIOD_LABELS:
        abort(400)

    mode = str(arguments.get("service_mode") or "all").strip().lower()
    if mode not in SERVICE_MODE_FILTER_VALUES:
        abort(400)

    if period == "custom":
        start = _parse_query_date(arguments.get("service_start"))
        end = _parse_query_date(arguments.get("service_end"))
        anchor = start
    else:
        anchor = _parse_query_date(arguments.get("service_anchor") or today.isoformat())
        try:
            start, end = _resolve_service_mode_date_range(period, anchor=anchor)
        except (ValueError, OverflowError):
            abort(400)

    if start > end:
        abort(400)
    return {
        "mode": mode,
        "mode_label": SERVICE_MODE_FILTER_LABELS[mode],
        "period": period,
        "period_label": SERVICE_MODE_PERIOD_LABELS[period],
        "anchor": anchor,
        "start": start,
        "end": end,
    }


def _parse_query_date(value: str | None) -> local_date:
    normalized = str(value or "").strip()
    if not DATE_PATTERN.fullmatch(normalized):
        abort(400)
    try:
        parsed = datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        abort(400)
    if parsed.isoformat() != normalized:
        abort(400)
    return parsed


def _resolve_service_mode_date_range(
    period: str,
    *,
    anchor: local_date,
) -> tuple[local_date, local_date]:
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = anchor.replace(day=1)
        end = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
        return start, end
    if period == "year":
        return local_date(anchor.year, 1, 1), local_date(anchor.year, 12, 31)
    if period in {"q1", "q2", "q3", "q4"}:
        quarter = int(period[-1])
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        return (
            local_date(anchor.year, start_month, 1),
            local_date(
                anchor.year,
                end_month,
                calendar.monthrange(anchor.year, end_month)[1],
            ),
        )
    raise ValueError(f"不支持的服务方式筛选周期：{period}")


def _build_service_mode_filter_form(
    service_filter: dict | None,
    *,
    selected_month: local_date,
) -> dict:
    default_start = selected_month.replace(day=1)
    default_end = selected_month.replace(
        day=calendar.monthrange(selected_month.year, selected_month.month)[1]
    )
    if service_filter is None:
        return {
            "mode": "all",
            "period": "month",
            "anchor": selected_month.isoformat(),
            "start": default_start.isoformat(),
            "end": default_end.isoformat(),
        }
    return {
        "mode": service_filter["mode"],
        "period": service_filter["period"],
        "anchor": service_filter["anchor"].isoformat(),
        "start": service_filter["start"].isoformat(),
        "end": service_filter["end"].isoformat(),
    }


def _build_service_mode_results(store, service_filter: dict) -> dict:
    onsite_filter = {
        "all": None,
        "onsite": True,
        "remote": False,
    }[service_filter["mode"]]
    rows = [
        row
        for row in store.list_report_days(
            service_filter["start"].isoformat(),
            service_filter["end"].isoformat(),
            is_onsite=onsite_filter,
        )
        if not row.get("attribution_target_date")
    ]
    onsite_count = sum(1 for row in rows if bool(row["is_onsite"]))
    remote_count = len(rows) - onsite_count
    return {
        "mode_label": service_filter["mode_label"],
        "period_label": service_filter["period_label"],
        "range_label": (
            f"{service_filter['start'].isoformat()} 至 "
            f"{service_filter['end'].isoformat()}"
        ),
        "total_count": len(rows),
        "onsite_count": onsite_count,
        "remote_count": remote_count,
        "records": [
            {
                "report_date": row["report_date"],
                "customer_name": row["customer_name"],
                "mode": "onsite" if row["is_onsite"] else "remote",
                "mode_label": "外勤" if row["is_onsite"] else "远程",
            }
            for row in reversed(rows)
        ],
    }


def _service_mode_filter_url_values(service_filter: dict | None) -> dict:
    if service_filter is None:
        return {}
    values = {
        "service_mode": service_filter["mode"],
        "service_period": service_filter["period"],
    }
    if service_filter["period"] == "custom":
        values.update(
            {
                "service_start": service_filter["start"].isoformat(),
                "service_end": service_filter["end"].isoformat(),
            }
        )
    else:
        values["service_anchor"] = service_filter["anchor"].isoformat()
    return values


def _calendar_page_url(
    month: str,
    *,
    issues_only: bool = False,
    issue_type: str | None = None,
    service_mode_filter: dict | None = None,
) -> str:
    values: dict[str, Any] = {"month": month}
    if issues_only:
        values["issues"] = 1
    if issue_type is not None:
        values["issue_type"] = issue_type
    values.update(_service_mode_filter_url_values(service_mode_filter))
    return url_for("web.calendar_page", **values)


def _build_issue_type_filters(
    month: str,
    issues: list[dict],
    *,
    active_type: str | None,
) -> list[dict]:
    filters = []
    for group_key, group_label in ((None, "全部"), *AUDIT_GROUP_LABELS.items()):
        filters.append(
            {
                "key": group_key or "all",
                "label": group_label,
                "count": sum(
                    1
                    for item in issues
                    if group_key is None or item["group_key"] == group_key
                ),
                "active": group_key == active_type,
                "url": _calendar_page_url(
                    month,
                    issues_only=True,
                    issue_type=group_key,
                ),
            }
        )
    return filters


def _parse_date(value: str) -> str:
    if not DATE_PATTERN.fullmatch(value):
        abort(404)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        abort(404)
    if parsed.isoformat() != value:
        abort(404)
    return parsed.isoformat()


def _calendar_month_dates(month: local_date) -> list[list[local_date]]:
    return calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)


def _build_calendar_weeks(
    month: local_date,
    days_by_date: dict[str, dict],
    *,
    occupied_dates: set[str],
    calendar_statuses_by_date: dict[str, dict],
) -> list[list[dict]]:
    weeks = []
    for week in _calendar_month_dates(month):
        weeks.append(
            [
                {
                    "date": day.isoformat(),
                    "day_number": day.day,
                    "in_month": day.month == month.month,
                    "is_before_month": day < month,
                    "is_today": day == local_date.today(),
                    "is_workday": is_china_workday(day.isoformat()),
                    "is_occupied": day.isoformat() in occupied_dates,
                    "calendar_status": calendar_statuses_by_date.get(day.isoformat()),
                    "can_manage_calendar_status": (
                        day.month == month.month
                        and day.isoformat() not in days_by_date
                    ),
                    "record": days_by_date.get(day.isoformat()),
                }
                for day in week
            ]
        )
    return weeks


def _build_month_work_summary(
    days: list[dict],
    calendar_statuses,
    *,
    official_statuses=(),
) -> dict:
    calendar_statuses = list(calendar_statuses)
    official_statuses = list(official_statuses)
    counted_days = [
        item for item in days if not item.get("attribution_target_date")
    ]
    company_office_dates = {
        item["report_date"] for item in counted_days if not item["is_onsite"]
    }
    company_office_dates.update(
        item["report_date"] for item in calendar_statuses
        if item.get("status") in COMPANY_OFFICE_CALENDAR_DAY_STATUSES
    )
    status_counts = {
        status: sum(
            1
            for item in calendar_statuses
            if item.get("status") == status
        )
        for status in CALENDAR_DAY_STATUS_LABELS
    }
    return {
        "onsite_days": sum(1 for item in counted_days if item["is_onsite"]),
        "company_office_days": len(company_office_dates),
        "leave_days": status_counts["leave"],
        "adjusted_rest_days": sum(
            1 for item in official_statuses if item.get("status") == "adjusted_rest"
        ),
        "weekend_rest_days": sum(
            1 for item in official_statuses if item.get("status") == "weekend_rest"
        ),
        "statutory_holiday_days": sum(
            1
            for item in official_statuses
            if item.get("status") == "statutory_holiday"
        ),
        "adjusted_workday_days": sum(
            1
            for item in official_statuses
            if item.get("status") == "adjusted_workday"
        ),
        "company_meeting_days": status_counts["company_meeting"],
        "company_activity_days": status_counts["company_activity"],
    }


def _shift_month(month: local_date, offset: int) -> local_date:
    absolute_month = month.year * 12 + month.month - 1 + offset
    year, zero_based_month = divmod(absolute_month, 12)
    return local_date(year, zero_based_month + 1, 1)


def _decorate_day(day: dict) -> dict:
    decorated = dict(day)
    decorated["onsite_label"] = "外勤" if day["is_onsite"] else "非外勤"
    manual_override_steps = {
        item["step"]
        for item in day.get("submissions", [])
        if str(item.get("source", "")).startswith(MANUAL_OVERRIDE_SOURCE_PREFIX)
    }
    status_rows = [
        _status_row(
            "daily_log",
            day.get("daily_log_status", "unknown"),
            int(day.get("daily_log_count", 0)),
            manual_override="daily_log" in manual_override_steps,
        ),
    ]
    if _uses_remote_as_technical_service(day):
        status_rows.append(_main_service_status_row(day, "remote_workorder"))
        if day.get("optional_remote_workorder_count"):
            status_rows.append(_status_row(
                "remote_workorder",
                day["optional_remote_workorder_status"],
                day["optional_remote_workorder_count"],
                step_label="远程工单（可选支持）",
            ))
    else:
        status_rows.extend(
            [
                _main_service_status_row(day, "service_record", manual_override="service_record" in manual_override_steps)
                if day["is_onsite"] else _status_row(
                    "service_record",
                    day.get("onsite_service_status", "unknown"),
                    int(day.get("onsite_service_count", 0)),
                    manual_override="service_record" in manual_override_steps,
                ),
                _status_row(
                    "remote_workorder",
                    day.get("remote_workorder_status", "unknown"),
                    int(day.get("remote_workorder_count", 0)),
                ),
            ]
        )
    decorated["status_rows"] = status_rows
    decorated["manual_controls"] = [
        row
        for row in decorated["status_rows"]
        if row["step"] == "daily_log"
        or (day["is_onsite"] and row["step"] == "service_record")
    ]
    return decorated


def _build_recent_audit(
    store,
    *,
    today: local_date,
    now: datetime | None = None,
    workday_count: int = 7,
) -> list[dict]:
    now = now or datetime.now().astimezone()
    groups: dict[str, dict[str, Any]] = {
        "missing_daily": {
            "key": "missing_daily",
            "title": "缺日报",
            "description": "未保存源文件或没有成功回执",
            "items": [],
        },
        "missing_service": {
            "key": "missing_service",
            "title": "缺主技术服务",
            "description": "日报已完成，但当天主服务没有成功依据",
            "items": [],
        },
        "verification": {
            "key": "verification",
            "title": "待核实",
            "description": "CRM 结果待确认，不会自动发起复核",
            "items": [],
        },
    }

    include_today = now.time() >= DAILY_SUBMISSION_OPEN_TIME
    report_dates = _recent_workday_dates(
        today,
        workday_count,
        include_today=include_today,
    )
    if not report_dates:
        return list(groups.values())
    end_date = today if include_today else today - timedelta(days=1)
    existing_rows = store.list_report_days(min(report_dates), end_date.isoformat())
    report_dates = sorted(
        set(report_dates)
        | {
            item["report_date"]
            for item in existing_rows
            if not item.get("attribution_target_date")
        },
        reverse=True,
    )
    calendar_status_dates = {
        item["report_date"]
        for item in store.list_calendar_day_statuses(
            min(report_dates),
            max(report_dates),
        )
    }
    days_by_date = {
        item["report_date"]: item for item in store.list_days(report_dates)
    } if report_dates else {}
    for report_date in report_dates:
        if report_date in calendar_status_dates and report_date not in days_by_date:
            continue
        if days_by_date.get(report_date, {}).get("attribution_target_date"):
            continue
        classified = _classify_audit_day(
            days_by_date.get(report_date),
            report_date,
        )
        if classified is None:
            continue
        group_key, item = classified
        groups[group_key]["items"].append(item)

    return list(groups.values())


def _build_month_issue_list(
    month: local_date,
    days_by_date: dict[str, dict],
    *,
    calendar_status_dates: set[str],
    today: local_date,
    now: datetime | None = None,
) -> list[dict]:
    if month > today.replace(day=1):
        return []
    now = now or datetime.now().astimezone()

    _, last_day = calendar.monthrange(month.year, month.month)
    end_date = min(today, month.replace(day=last_day))
    candidate = month
    issues = []
    while candidate <= end_date:
        report_date = candidate.isoformat()
        record = days_by_date.get(report_date)
        if record and record.get("attribution_target_date"):
            candidate += timedelta(days=1)
            continue
        if is_china_workday(report_date) or record is not None:
            if candidate == today and now.time() < DAILY_SUBMISSION_OPEN_TIME:
                candidate += timedelta(days=1)
                continue
            if record is not None or report_date not in calendar_status_dates:
                classified = _classify_audit_day(
                    record,
                    report_date,
                )
                if classified is not None:
                    group_key, item = classified
                    issues.append(
                        {
                            **item,
                            "group_key": group_key,
                            "group_label": AUDIT_GROUP_LABELS[group_key],
                        }
                    )
        candidate += timedelta(days=1)
    return issues


def _classify_audit_day(day: dict | None, report_date: str) -> tuple[str, dict] | None:
    if day is None:
        return (
            "missing_daily",
            {
                "report_date": report_date,
                "customer_name": "未保存日报",
                "status": "unknown",
                "detail": "本机日报索引中未发现该工作日。",
                "has_record": False,
                "action_label": "先保存日报",
            },
        )

    daily_row = _decorate_day(day)["status_rows"][0]
    if daily_row["status"] in AUDIT_VERIFICATION_STATUSES:
        return "verification", _build_audit_item(day, daily_row)
    if daily_row["status"] not in WORKBENCH_COMPLETED_STATUSES:
        return "missing_daily", _build_audit_item(day, daily_row)

    main_service_steps = _required_main_steps(day)[1:]
    if not main_service_steps:
        return None
    service_row = _main_service_status_row(day, main_service_steps[0])
    if service_row["status"] in MAIN_SERVICE_REVIEW_STATUSES:
        return "verification", _build_audit_item(day, service_row)
    if service_row["status"] not in WORKBENCH_COMPLETED_STATUSES | {"not_required"}:
        return "missing_service", _build_audit_item(day, service_row)
    return None


def _build_audit_item(day: dict, row: dict) -> dict:
    return {
        "report_date": day["report_date"],
        "customer_name": day["customer_name"],
        "status": row["status"],
        "detail": row["display_text"],
        "step_label": row["step_label"],
        "has_record": True,
        "action_label": "查看详情",
    }


def _recent_workday_dates(
    today: local_date,
    workday_count: int,
    *,
    include_today: bool = True,
) -> list[str]:
    dates: list[str] = []
    candidate = today if include_today else today - timedelta(days=1)
    while len(dates) < workday_count:
        if is_china_workday(candidate.isoformat()):
            dates.append(candidate.isoformat())
        candidate -= timedelta(days=1)
    return dates


def _resolve_report_path(indexer) -> Path:
    if indexer is not None:
        return Path(indexer.report_path).expanduser()
    web_config = current_app.config.get("WEB_CONFIG")
    if web_config is None:
        raise RuntimeError("无法读取日报源文件配置。")
    return Path(web_config.report_path).expanduser()


def _build_report_sync_candidates(
    report_path: Path,
    *,
    today: local_date,
) -> list[dict]:
    if not report_path.exists():
        raise RuntimeError("未找到日报.md，无法扫描补交候选。")
    earliest_date = today - timedelta(days=REPORT_SYNC_WINDOW_DAYS - 1)
    candidates_by_date: dict[str, dict] = {}
    for section in split_report_sections(
        report_path.read_text(encoding="utf-8")
    ):
        try:
            report_date = normalize_report_date_to_iso(section)
            parsed_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except (RuntimeError, ValueError):
            continue
        if parsed_date < earliest_date or parsed_date > today:
            continue
        fields = parse_report_fields(section)
        customer_name = str(fields.get("服务客户") or "").strip()
        if not customer_name:
            continue
        candidates_by_date[report_date] = {
            "report_date": report_date,
            "customer_name": customer_name,
        }
    return [
        candidates_by_date[report_date]
        for report_date in sorted(candidates_by_date, reverse=True)
    ]


def _required_main_steps(day: dict) -> tuple[str, ...]:
    if day["is_onsite"]:
        return ("daily_log", "service_record")
    if _uses_remote_as_technical_service(day):
        return ("daily_log", "remote_workorder")
    return ("daily_log",)


def _uses_remote_as_technical_service(day: dict) -> bool:
    return (
        not bool(day.get("is_onsite"))
        and str(day.get("report_date", ""))
        >= REMOTE_MAIN_SERVICE_START_DATE
    )


def _main_service_status_row(day: dict, step: str, *, manual_override: bool = False) -> dict:
    incomplete = [
        f"{item['customer_name']}（{_status_presentation(item['status'])[1]}）"
        for item in day.get("main_service_customers") or []
        if item["status"] not in SUBMITTED_BUSINESS_STATUSES
    ]
    return _status_row(
        step,
        day.get("main_service_status", "unknown"),
        int(day.get("main_service_count", 0)),
        manual_override=manual_override,
        step_label=STEP_LABELS["service_record"],
        detail="；".join(incomplete),
    )


def _status_row(
    step: str,
    status: str,
    count: int,
    *,
    manual_override: bool = False,
    step_label: str | None = None,
    detail: str = "",
) -> dict:
    submission_label, verification_label = _status_presentation(status)
    if detail:
        verification_label = detail
    resolved_step_label = step_label or STEP_LABELS[step]
    count_suffix = f"（{count} 条）" if count > 1 else ""
    return {
        "step": step,
        "step_label": resolved_step_label,
        "status": status,
        "status_label": submission_label,
        "submission_label": submission_label,
        "verification_label": verification_label,
        "display_text": (
            f"{resolved_step_label}：{submission_label}；{verification_label}"
            f"{count_suffix}"
        ),
        "count": count,
        "manual_override": manual_override,
    }


def _decorate_submission(
    item: dict,
    *,
    remote_candidate=None,
    remote_job=None,
    remote_as_technical_service: bool = False,
) -> dict:
    decorated = dict(item)
    submission_label, verification_label = _status_presentation(item["status"])
    if remote_as_technical_service and item["step"] == "remote_workorder":
        role = item.get("includes_main_work")
        decorated["step_label"] = (
            STEP_LABELS["service_record"] if role is True
            else "远程工单（可选支持）" if role is False
            else "远程工单（主服务归属待核实）"
        )
    else:
        decorated["step_label"] = STEP_LABELS.get(item["step"], item["step"])
    decorated["status_label"] = submission_label
    decorated["submission_label"] = submission_label
    decorated["verification_label"] = verification_label
    decorated["evidence_label"] = _evidence_label(item.get("source"))
    decorated["manual_status_editable"] = False
    decorated["manual_override"] = False
    decorated["remote_hours_editable"] = False
    decorated["remote_hours_lock_reason"] = ""
    decorated["remote_hours_can_sync"] = False
    decorated["remote_hours_sync_reason"] = ""
    decorated["remote_hour_sync_label"] = _remote_hour_sync_label(
        item.get("remote_hour_sync_status")
    )
    decorated["remote_hour_job_id"] = ""
    decorated["remote_hour_job_api"] = ""
    decorated["remote_hour_job_status"] = ""
    decorated["remote_hour_job_status_label"] = ""
    decorated["remote_hour_conflict_kind"] = ""
    decorated["remote_hour_conflict_message"] = ""
    decorated["remote_hours_alias_confirmable"] = False
    decorated["remote_hours_binding_repairable"] = False
    if remote_candidate is not None:
        decorated["remote_hours_editable"] = remote_candidate.editable
        decorated["remote_hours_lock_reason"] = remote_candidate.lock_reason
        decorated["remote_hours_can_sync"] = remote_candidate.can_sync
        decorated["remote_hours_sync_reason"] = remote_candidate.sync_reason
        decorated["remote_hours_workorder_id"] = remote_candidate.workorder_id
        decorated["remote_hours_base_service_hours"] = remote_candidate.base_service_hours
    if remote_job is not None:
        job_payload = remote_job.get("request_payload", {})
        result_payload = remote_job.get("result_payload", {})
        job_status = str(remote_job.get("status", "") or "").strip()
        conflict_kind = str(
            result_payload.get("conflict_kind", "") or ""
        ).strip()
        conflict_observed = str(
            result_payload.get("conflict_observed", "") or ""
        ).strip()
        current_crm_customer = (
            str(remote_candidate.crm_customer_name or "").strip()
            if remote_candidate is not None
            else ""
        )
        customer_conflict_resolved = bool(
            conflict_kind == "customer"
            and conflict_observed
            and conflict_observed == current_crm_customer
        )
        unresolved_conflict = bool(
            job_status == "conflict"
            and conflict_kind in {"customer", "date", "status"}
            and not customer_conflict_resolved
        )
        decorated["remote_hour_job_id"] = remote_job["job_id"]
        decorated["remote_hour_job_api"] = url_for(
            "web.job_api",
            job_id=remote_job["job_id"],
        )
        decorated["remote_hour_job_status"] = job_status
        decorated["remote_hour_job_status_label"] = STATUS_LABELS.get(
            job_status,
            job_status,
        )
        decorated["remote_hour_job_action"] = remote_job.get("action", "")
        decorated["remote_hour_target_hours"] = job_payload.get(
            "target_hours",
            item.get("remote_hour_target_hours"),
        )
        decorated["remote_hour_conflict_kind"] = conflict_kind
        decorated["remote_hour_conflict_message"] = str(
            result_payload.get("message") or remote_job.get("error_summary") or ""
        ).strip()
        decorated["remote_hours_alias_confirmable"] = bool(
            job_status == "conflict"
            and conflict_kind == "customer"
            and not customer_conflict_resolved
        )
        decorated["remote_hours_binding_repairable"] = bool(
            (
                job_status == "conflict"
                and conflict_kind in {"customer", "date", "status"}
                and not customer_conflict_resolved
            )
            or job_status == "not_found"
        )
        if job_status in {"queued", "running", "uncertain"} or unresolved_conflict:
            decorated["remote_hours_editable"] = False
            decorated["remote_hours_lock_reason"] = (
                "CRM 工时结果待核实"
                if job_status == "uncertain"
                else "CRM 同步任务正在处理"
                if job_status in {"queued", "running"}
                else "请先修复 CRM 工单校验冲突"
            )
    return decorated


def _failed_crm_verification_job_id(item: dict, jobs_by_id: dict, report_date: str) -> str:
    source = str(item.get("source", "") or "")
    if not source.startswith("crm_verification:"):
        return ""
    job_id = source.removeprefix("crm_verification:").strip()
    job = jobs_by_id.get(job_id)
    if not job:
        return ""
    if (
        job.get("action") != CRM_VERIFICATION_ACTION
        or job.get("status") not in DELETABLE_CRM_VERIFICATION_STATUSES
        or item.get("status") not in DELETABLE_CRM_VERIFICATION_STATUSES
        or str(job.get("request_payload", {}).get("report_date", "")).strip() != report_date
    ):
        return ""
    return job_id


def _decorate_artifact(item: dict) -> dict:
    decorated = dict(item)
    decorated["step_label"] = STEP_LABELS.get(item["step"], item["step"])
    decorated["file_name"] = Path(item["file_path"]).name
    decorated["size_label"] = _format_size(int(item["size_bytes"]))
    return decorated


def _decorate_job(item: dict) -> dict:
    decorated = dict(item)
    decorated["action_label"] = ACTION_LABELS.get(item["action"], item["action"])
    decorated["report_date"] = item.get("request_payload", {}).get("report_date", "")
    decorated["customer"] = item.get("request_payload", {}).get(
        "service_customer",
        item.get("request_payload", {}).get("customer", ""),
    )
    decorated["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    decorated["can_retry"] = item["status"] in RETRYABLE_JOB_STATUSES
    decorated["can_delete"] = item["status"] in DELETABLE_JOB_RECORD_STATUSES
    decorated["verification_only"] = item["status"] in {
        "crm_id_captured",
        "uncertain",
    }
    decorated["is_polling"] = _job_is_polling(item)
    return decorated


def _serialize_preview(preview) -> dict:
    return {
        "action": preview.action,
        "action_label": ACTION_LABELS.get(preview.action, preview.action),
        "report_date": preview.report_date,
        "customer": preview.customer,
        "hours": preview.hours,
        "content": preview.content,
        "content_digest": preview.content_digest,
        "authorization_digest": preview.authorization_digest,
        "requires_hours": preview.action == "remote_workorder" and preview.hours is None,
        "service_category": preview.payload.get("service_category", ""),
        "completion_status": preview.payload.get("completion_status", ""),
        "source_completion_status": preview.payload.get("source_completion_status", ""),
        "includes_main_work": bool(preview.payload.get("includes_main_work", False)),
        "work_type": preview.payload.get("work_type", ""),
        "routing_policy": preview.payload.get("routing_policy", ""),
        "routing_rule": (
            "仅在签到关联实施工单且日报明确为售后时，核对同一客户及部门、关联商机和可服务状态后查找售后工单；"
            "按创建时间选择不晚于日报日期的唯一最近工单。无匹配、并列或字段不完整时停止，不修改签到记录。"
            if preview.action == "onsite_service"
            and preview.payload.get("routing_policy") == "implementation_to_latest_after_sales_v1"
            else ""
        ),
        "routing_target": (
            dict(preview.payload["routing_target"])
            if preview.payload.get("routing_target") is not None else None
        ),
        "source_checkin_workorder_id": preview.payload.get("source_checkin_workorder_id", ""),
        "review_options": (
            fxiaoke_remote_workorder.get_remote_workorder_review_options()
            if preview.action == "remote_workorder" else None
        ),
    }


def _remote_options_from_form(customer: str, action: str):
    if action != "remote_workorder":
        return None
    options = {
        key: str(request.form[key]).strip()
        for key in ("service_category", "completion_status") if key in request.form
    }
    if options and not customer:
        raise ValueError("修改服务类别和处理状态时必须指定客户")
    return {customer: options} if options else None


def _remote_role_from_form(customer: str, action: str) -> dict[str, bool]:
    if action != "remote_workorder" or "includes_main_work" not in request.form:
        return {}
    role = request.form["includes_main_work"]
    if not customer or role not in {"true", "false"}:
        raise ValueError("请指定远程工单的客户和草稿角色。")
    return {"includes_main_work": role == "true"}


def _source_sync_selections() -> dict[str, dict]:
    selected = json.loads(request.form.get("selections", "[]"))
    if not isinstance(selected, list) or not selected:
        raise ValueError("请重新核对本次同步的预览。")
    selections = {}
    for item in selected:
        if not isinstance(item, dict) or set(item) - {
            "authorization_digest", "hours", "service_category", "completion_status",
        } or any(not isinstance(value, str) for value in item.values()):
            raise ValueError("同步选择格式不正确，请重新生成预览。")
        digest = item.get("authorization_digest")
        if not digest or digest in selections:
            raise ValueError("存在重复或无效的同步预览，请重新生成预览。")
        selections[digest] = item
    return selections


def _source_sync_remote_customer(preview) -> str:
    return fxiaoke_remote_workorder.strict_customer_identity(
        preview.payload.get("selected_customer_name")
        or fxiaoke_remote_workorder.resolve_customer_primary_name(preview.customer)
        or preview.customer
    )


def _source_sync_remote_conflict(preview, selected_customers: set[str]) -> str:
    if _source_sync_remote_customer(preview) in selected_customers:
        return f"“{preview.customer}”与同日已选远程工单属于同一客户；不能同时创建，主服务优先，不会合并内容。"
    return ""


def _match_preview(previews, customer: str, digest: str):
    matching = [
        item
        for item in previews
        if item.customer == customer and item.authorization_digest == digest
    ]
    if len(matching) != 1:
        raise ValueError("预览已变化或授权指纹无效，请重新生成预览。")
    return matching[0]


def _job_is_polling(job: dict) -> bool:
    return job["status"] in POLLING_JOB_STATUSES or (
        job["status"] == "crm_id_captured" and bool(job.get("worker_pid"))
    )


def _serialize_job(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "action": job["action"],
        "action_label": ACTION_LABELS.get(job["action"], job["action"]),
        "status": job["status"],
        "status_label": STATUS_LABELS.get(job["status"], job["status"]),
        "request_payload": job.get("request_payload", {}),
        "result_payload": job.get("result_payload", {}),
        "progress_log": job.get("progress_log", []),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error_summary": job.get("error_summary"),
        "retry_of_job_id": job.get("retry_of_job_id"),
        "can_retry": job["status"] in RETRYABLE_JOB_STATUSES,
        "can_delete": job["status"] in DELETABLE_JOB_RECORD_STATUSES,
        "verification_only": job["status"] in {"crm_id_captured", "uncertain"},
        "poll": _job_is_polling(job),
    }


def _job_created_response(job_id: str, **extra):
    return (
        jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "job_url": url_for("web.jobs_page", focus=job_id),
                "poll_url": url_for("web.job_api", job_id=job_id),
                **extra,
            }
        ),
        202,
    )


def _json_error(message: str, status: int):
    return jsonify({"ok": False, "error": str(message)}), status


def _date_attribution_action_error(day: dict):
    if not day.get("is_date_attribution"):
        return None
    return _json_error(
        "补交归属仅用于本机日期整理，不能从归属日期发起 CRM 操作。",
        409,
    )


def _parse_date_for_json(value) -> str:
    normalized = str(value or "").strip()
    if not DATE_PATTERN.fullmatch(normalized):
        raise ValueError("日期格式无效。")
    try:
        parsed = datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期格式无效。") from exc
    if parsed.isoformat() != normalized:
        raise ValueError("日期格式无效。")
    return normalized


def _refresh_index(indexer, *, require_pending_snapshot=False) -> None:
    if indexer is None:
        return
    try:
        summary = indexer.sync()
        if require_pending_snapshot and not getattr(summary, "pending_snapshot_complete", True):
            raise RuntimeError("待补交索引不完整，暂不允许清理失败留痕。")
    except Exception as exc:
        if require_pending_snapshot:
            raise RuntimeError("无法完整刷新待补交索引，请修复来源后重新预览清理。") from exc
        current_app.logger.exception("刷新日报提交索引失败，继续显示上次成功索引。")


def _status_presentation(status: str) -> tuple[str, str]:
    return STATUS_PRESENTATIONS.get(status, (str(status), "状态待核对"))


def _evidence_label(source) -> str:
    normalized = str(source or "")
    if normalized.startswith(MANUAL_OVERRIDE_SOURCE_PREFIX):
        return "人工更正（仅 Web）"
    if normalized.startswith("crm_verification:"):
        return "CRM 只读复核"
    if normalized.startswith("web_job:"):
        return "Web 执行回写"
    if normalized.startswith("submission_history:"):
        return "本地提交历史"
    if normalized.startswith("pending_queue:"):
        return "待补交队列"
    return "本地索引"


def _remote_hour_sync_label(status) -> str:
    labels = {
        "local_only": "仅本机，未同步 CRM",
        "sync_queued": "CRM 工时同步已排队",
        "crm_verified": "CRM 工时已同步",
        "crm_mismatch": "CRM 当前工时已变化，未写入",
        "pending_retry": "CRM 工时同步失败",
        "uncertain": "CRM 工时同步结果待核对",
        "not_found": "CRM 未找到对应远程工单",
    }
    return labels.get(str(status or ""), "")


def _format_size(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
