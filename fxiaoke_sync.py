#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from fxiaoke_service_record import CachedLoginExpired, is_login_page, retain_submission_failure_trace
from typing import Any
from packages.report_domain.workday import resolve_report_work_type

from daily_report import (
    SubmissionEvidenceError,
    checkpoint_fxiaoke_submission,
    build_fxiaoke_submission_intent_blocked_result,
    clear_fxiaoke_submission_intent,
    configure_stdio,
    finish_fxiaoke_direct_submission,
    is_onsite_workday,
    read_brief_input,
    reserve_fxiaoke_submission_intent,
    resolve_report_path,
    should_auto_confirm,
)
from fxiaoke_daily_log import (
    PublishConfirmationUncertain,
    build_daily_log_draft,
    capture_failure_artifacts as capture_daily_log_failure_artifacts,
    emit_progress,
    publish_daily_log,
)
from fxiaoke_service_record import (
    FXIAOKE_HOME_URL,
    DEFAULT_STORAGE_STATE_PATH,
    ServiceRecordConfirmationUncertain,
    ROUTING_AUDIT_FIELDS,
    build_service_record_drafts,
    capture_failure_artifacts as capture_service_record_failure_artifacts,
    collect_environment_diagnostics,
    close_browser_session,
    fast_goto,
    find_target_report_section,
    open_authenticated_page,
    reset_login_state,
    resolve_profile_dir,
    save_login_state,
    submit_service_record_on_authenticated_page,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="共用一次浏览器会话，自动提交纷享销客日报与技术服务记录。"
    )
    parser.add_argument("--date", dest="report_date", help="指定日报日期，支持 YYYY-MM-DD 或 YYYY.MM.DD。")
    parser.add_argument("--submission-intent-tokens", default="{}", help=argparse.SUPPRESS)
    parser.add_argument("--onsite-mode", choices=("yes", "no"), default=None, help=argparse.SUPPRESS)
    parser.add_argument("--work-type", choices=("实施", "售后", "POC"), default="", help="明确工作类型，用于外勤工单归属核对。")
    parser.add_argument("--path", dest="report_path", help="覆盖默认日报路径。")
    parser.add_argument("--skip-service-record", action="store_true", help="跳过技术服务记录自动填写。")
    parser.add_argument(
        "--remote-drafts-file",
        help="读取主进程已授权的远程工单草稿，并在当前浏览器会话中执行。",
    )
    parser.add_argument("--preview-only", action="store_true", help="只生成联动草稿，不启动浏览器自动化。")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认，直接执行浏览器自动化。")
    parser.add_argument("--doctor", action="store_true", help="检查本地浏览器自动化环境是否已准备好。")
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STORAGE_STATE_PATH),
        help="覆盖默认的纷享销客登录状态缓存文件。",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="使用指定 Playwright 持久化浏览器 Profile 目录保存完整登录状态。",
    )
    parser.add_argument(
        "--reset-login-state",
        action="store_true",
        help="清理登录态缓存与指定 Profile 后退出，用于登录状态损坏时重建。",
    )
    parser.add_argument(
        "--login-check",
        action="store_true",
        help="只检查/刷新纷享销客登录态，不提交日报或服务记录。",
    )
    parser.add_argument("--show-browser", action="store_true", help="显示真实浏览器窗口，默认无头执行。")
    parser.add_argument(
        "--allow-ocr-login",
        action="store_true",
        help="登录态失效且出现图形验证码时，允许使用 OCR 自动识别；默认改为人工/扫码恢复登录。",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出 JSON 结果。")
    return parser.parse_args(argv)


def build_success_result(step, status, message, **extra):
    payload = {
        "ok": True,
        "step": step,
        "status": status,
        "message": message,
        "artifacts": [],
    }
    payload.update(extra)
    return payload


def build_failure_result(step, message, artifacts=None, status="error", **extra):
    payload = {
        "ok": False,
        "step": step,
        "status": status,
        "message": message,
        "artifacts": list(artifacts or []),
    }
    payload.update(extra)
    return payload


def _validate_remote_drafts(drafts, report_date):
    from fxiaoke_remote_workorder import normalize_positive_service_hours

    validated = []
    for draft in drafts:
        if draft.report_date_iso != report_date:
            raise ValueError("远程工单草稿日期与本次日报日期不一致。")
        validated.append(replace(draft, service_hours=normalize_positive_service_hours(draft.service_hours)))
    return validated


def _stage_callback(step, state, callback, child_index=0):
    def record(event, **fields):
        state[event] = True
        for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"):
            if fields.get(key):
                state[key] = fields[key]
        if step == "service_record" and event == "target_selected":
            for key in ROUTING_AUDIT_FIELDS:
                if key in fields:
                    state[key] = deepcopy(fields[key])
        if callback:
            callback(event, step=step, child_index=child_index, **fields)

    return record


def _capture_artifacts(capture, page, root, prefix):
    try:
        return capture(page, root, prefix)
    except Exception:
        return []


def _stage_failure(step, exc, state, artifacts, **extra):
    if isinstance(exc, SubmissionEvidenceError):
        raise exc
    details = deepcopy(state)
    if getattr(exc, "differences", None):
        details["differences"] = exc.differences
    for key in ("workorder_id", "service_record_id"):
        if getattr(exc, key, ""):
            details[key] = getattr(exc, key)
    uncertain = (
        details.get("submit_clicked") or details.get("crm_id_captured")
        or isinstance(exc, (PublishConfirmationUncertain, ServiceRecordConfirmationUncertain))
        or getattr(exc, "status", "") in {"uncertain", "crm_id_captured"}
    )
    if uncertain or (step == "service_record" and details.get("routing_target") is not None):
        details["retry_action"] = "verify_crm"
    return build_failure_result(
        step, str(exc), artifacts=artifacts,
        status="uncertain" if uncertain else "error", **{**details, **extra},
    )


def run_shared_sync(
    report_section,
    report_path,
    storage_state_path,
    skip_service_record=False,
    remote_drafts=None,
    remote_input_func=read_brief_input,
    show_browser=False,
    progress_callback=None,
    allow_ocr_login=False,
    profile_dir=None,
    milestone_callback=None,
    result_callback=None,
    cached_login_only=False,
    onsite_mode=None,
    work_type="",
):
    daily_draft = build_daily_log_draft(report_section)
    remote_drafts = _validate_remote_drafts(list(remote_drafts or []), daily_draft.report_date_iso)
    skip_message = (
        "纷享销客技术服务记录：本次已指定跳过自动填写。"
        if skip_service_record else "纷享销客技术服务记录：当天已标记为非外勤，已跳过自动填写。"
    )
    service_drafts = []
    if not skip_service_record and (onsite_mode if onsite_mode is not None else is_onsite_workday(
        report_text=report_section, report_path=report_path,
    )):
        selected_type = resolve_report_work_type(
            report_section, report_path, {"work_type": work_type} if work_type else None,
        )
        service_drafts = build_service_record_drafts(
            report_section, main_only=True,
            **({"report_path": report_path, "work_type": selected_type} if selected_type else {}),
        )

    playwright = browser = context = page = None
    results = []
    service_states: dict[str, dict[str, Any]] = {}

    def record_result(result):
        if result_callback:
            result_callback(result)
        results.append(result)
    def stop_remaining(message, status="blocked", retry_action="safe_retry"):
        completed = {(row.get("step"), row.get("service_customer", ""), row.get("includes_main_work")) for row in results}
        remaining = [("daily_log", "", None), *[("service_record", draft.customer_name, None) for draft in service_drafts],
                     *[("remote_workorder", draft.customer_name, draft.includes_main_work) for draft in remote_drafts]]
        for step, customer, role in remaining:
            if (step, customer, role) not in completed:
                state = service_states.get(customer, {}) if step == "service_record" else {}
                audit = {key: deepcopy(state[key]) for key in ROUTING_AUDIT_FIELDS if key in state}
                identities = {key: state[key] for key in ("workorder_id", "service_record_id") if state.get(key)}
                record_result(build_failure_result(
                    step, message, status=status, service_customer=customer,
                    retry_action="verify_crm" if audit.get("routing_target") is not None else retry_action,
                    submission_phase="submit_clicked" if state.get("submit_clicked") else "before_submit",
                    failure_scope="session", **audit, **identities,
                    **({"includes_main_work": role} if role is not None else {}),
                ))
        return results

    def is_local_customer_error(exc):
        from fxiaoke_remote_workorder import RemoteCustomerPreflightError
        from fxiaoke_service_record import NoMatchingCheckinCandidate
        return isinstance(exc, (RemoteCustomerPreflightError, NoMatchingCheckinCandidate))

    auth_started_at = time.monotonic()

    try:
        if progress_callback:
            progress_callback("[纷享销客同步] 正在启动共用浏览器会话...")
        playwright, browser, context, page = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[纷享销客同步/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
            **({"cached_login_only": True} if cached_login_only else {}),
        )
    except Exception as exc:
        auth_elapsed_seconds = round(time.monotonic() - auth_started_at, 3)
        artifacts = _capture_artifacts(capture_service_record_failure_artifacts, page, report_path.parent / "fxiaoke_failure_artifacts", f"{daily_draft.report_date_iso.replace('-', '')}_sync_auth")
        message = str(exc)
        record_result(
            build_failure_result(
                "daily_log",
                f"纷享销客日报：{message}",
                artifacts=artifacts,
                auth_elapsed_seconds=auth_elapsed_seconds,
            )
        )
        if not service_drafts:
            record_result(
                build_success_result(
                    "service_record",
                    "skipped",
                    skip_message,
                    auth_elapsed_seconds=auth_elapsed_seconds,
                )
            )
        else:
            for service_draft in service_drafts:
                record_result(
                    build_failure_result(
                        "service_record",
                        f"纷享销客技术服务记录：{message}",
                        artifacts=artifacts,
                        main_only=True,
                        service_customer=service_draft.customer_name,
                        auth_elapsed_seconds=auth_elapsed_seconds,
                    )
                )
        for remote_draft in remote_drafts:
            record_result(
                build_failure_result(
                    "remote_workorder",
                    f"纷享销客远程服务工单：{message}",
                    artifacts=artifacts,
                    service_customer=remote_draft.customer_name,
                    includes_main_work=remote_draft.includes_main_work,
                    auth_elapsed_seconds=auth_elapsed_seconds,
                )
            )
        return results

    auth_elapsed_seconds = round(time.monotonic() - auth_started_at, 3)
    active_step = "daily_log"
    try:
        daily_state: dict[str, Any] = {}
        try:
            daily_started_at = time.monotonic()
            if progress_callback:
                progress_callback("[纷享销客日报] 1/4 正在打开纷享销客并检查登录状态...")
            if cached_login_only and is_login_page(page):
                raise CachedLoginExpired()
            publish_status = publish_daily_log(
                page,
                daily_draft,
                progress_callback=progress_callback,
                pre_submit_history_check=True,
                milestone_callback=_stage_callback("daily_log", daily_state, milestone_callback),
            )
            message = (
                f"纷享销客日报：已检测到 {daily_draft.report_date_iso} 当天日报已发布，无需重复提交。"
                if publish_status == "already_exists"
                else f"纷享销客日报：发布成功。客户：{daily_draft.customer_name}，日期：{daily_draft.report_date_iso}。"
            )
            record_result(
                build_success_result(
                    "daily_log",
                    publish_status,
                    message,
                    auth_elapsed_seconds=auth_elapsed_seconds,
                    phase_elapsed_seconds=round(time.monotonic() - daily_started_at, 3),
                    **({"feed_id": daily_state["feed_id"]} if daily_state.get("feed_id") else {}),
                )
            )
        except (SubmissionEvidenceError, CachedLoginExpired):
            raise
        except Exception as exc:
            daily_elapsed_seconds = round(time.monotonic() - daily_started_at, 3)
            artifacts = _capture_artifacts(
                capture_daily_log_failure_artifacts,
                page,
                report_path.parent / "fxiaoke_failure_artifacts",
                f"{daily_draft.report_date_iso.replace('-', '')}_daily_log",
            )
            record_result(
                _stage_failure(
                    "daily_log",
                    exc,
                    daily_state,
                    artifacts,
                    auth_elapsed_seconds=auth_elapsed_seconds,
                    phase_elapsed_seconds=daily_elapsed_seconds,
                )
            )

        if cached_login_only and results and not results[-1].get("ok"):
            return stop_remaining("日报阶段未能确认安全完成，后续项目尚未开始；请处理该项后重新确认。")

        active_step = "service_record"
        if not service_drafts:
            record_result(build_success_result("service_record", "skipped", skip_message))
        else:
            for index, service_draft in enumerate(service_drafts, start=1):
                service_state: dict[str, Any] = {}
                if getattr(service_draft, "work_type", ""):
                    service_state["work_type"] = service_draft.work_type
                service_states[service_draft.customer_name] = service_state
                try:
                    service_started_at = time.monotonic()
                    if progress_callback:
                        progress_callback(f"[技术服务记录] 正在处理第 {index}/{len(service_drafts)} 个服务客户：{service_draft.customer_name}")
                        progress_callback("[技术服务记录] 正在返回工作台并刷新当天签到流...")
                    fast_goto(page, FXIAOKE_HOME_URL)
                    if cached_login_only and is_login_page(page):
                        raise CachedLoginExpired()
                    service_result = submit_service_record_on_authenticated_page(
                        page,
                        context,
                        service_draft,
                        progress_callback=progress_callback,
                        milestone_callback=_stage_callback("service_record", service_state, milestone_callback, index - 1),
                    )
                    routing_audit = {
                        key: deepcopy(value)
                        for key in ROUTING_AUDIT_FIELDS
                        if (value := getattr(service_result, key, None) or service_state.get(key))
                    }
                    record_result(
                        build_success_result(
                            "service_record",
                            service_result.status,
                            f"纷享销客技术服务记录：{service_result.message}",
                            workorder_id=service_result.workorder_id,
                            service_record_id=service_result.service_record_id,
                            crm_customer=getattr(service_result, "crm_customer", ""),
                            **routing_audit,
                            main_only=True,
                            service_customer=service_draft.customer_name,
                            phase_elapsed_seconds=round(
                                time.monotonic() - service_started_at,
                                3,
                            ),
                        )
                    )
                except (SubmissionEvidenceError, CachedLoginExpired):
                    raise
                except Exception as exc:
                    service_elapsed_seconds = round(
                        time.monotonic() - service_started_at,
                        3,
                    )
                    artifacts = _capture_artifacts(
                        capture_service_record_failure_artifacts,
                        page,
                        report_path.parent / "fxiaoke_failure_artifacts",
                        f"{service_draft.report_date_compact}_fw_{index}",
                    )
                    record_result(
                        _stage_failure(
                            "service_record",
                            exc,
                            service_state,
                            artifacts,
                            main_only=True,
                            service_customer=service_draft.customer_name,
                            phase_elapsed_seconds=service_elapsed_seconds,
                        )
                    )
                    if cached_login_only and not is_local_customer_error(exc):
                        return stop_remaining("服务阶段发生未分类或会话错误，后续项目尚未开始，请先核对。")

        if remote_drafts:
            active_step = "remote_workorder"
            from fxiaoke_remote_workorder import (
                submit_authorized_remote_workorder_on_authenticated_page,
            )

            for index, remote_draft in enumerate(remote_drafts, start=1):
                remote_state: dict[str, Any] = {}
                try:
                    remote_started_at = time.monotonic()
                    if progress_callback:
                        progress_callback(
                            f"[远程服务工单] 正在复用当前会话处理第 {index}/{len(remote_drafts)} 张："
                            f"{remote_draft.customer_name}"
                        )
                    if cached_login_only and is_login_page(page):
                        raise CachedLoginExpired()
                    remote_result = submit_authorized_remote_workorder_on_authenticated_page(
                        page,
                        remote_draft,
                        input_func=remote_input_func,
                        report_path=report_path,
                        progress_callback=progress_callback,
                        milestone_callback=_stage_callback("remote_workorder", remote_state, milestone_callback, index - 1),
                    )
                    remote_result["phase_elapsed_seconds"] = round(
                        time.monotonic() - remote_started_at,
                        3,
                    )
                    record_result(remote_result)
                except (SubmissionEvidenceError, CachedLoginExpired):
                    raise
                except Exception as exc:
                    artifacts = _capture_artifacts(
                        capture_service_record_failure_artifacts,
                        page,
                        report_path.parent / "fxiaoke_failure_artifacts",
                        f"{remote_draft.report_date_iso.replace('-', '')}_remote_workorder_shared_{index}",
                    )
                    record_result(
                        _stage_failure(
                            "remote_workorder",
                            exc,
                            remote_state,
                            artifacts,
                            service_customer=remote_draft.customer_name,
                            includes_main_work=remote_draft.includes_main_work,
                            phase_elapsed_seconds=round(
                                time.monotonic() - remote_started_at,
                                3,
                            ),
                        )
                    )
                    if cached_login_only and not is_local_customer_error(exc):
                        return stop_remaining("远程工单阶段发生未分类或会话错误，后续项目尚未开始，请先核对。")

        try:
            save_login_state(context, storage_state_path)
        except Exception as exc:
            if progress_callback:
                progress_callback(f"[纷享销客同步] 各阶段结果已保存，登录缓存保存失败：{exc}")
        return results
    except CachedLoginExpired as exc:
        return stop_remaining(str(exc), status="auth_expired")
    except SubmissionEvidenceError as exc:
        retain_submission_failure_trace(page, report_path.parent / "fxiaoke_failure_artifacts", f"{daily_draft.report_date_iso.replace('-', '')}_{active_step}_evidence", exc)
        raise
    finally:
        try:
            close_browser_session(browser, context)
        except Exception:
            pass
        finally:
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass


def run_login_check(storage_state_path, show_browser=False, progress_callback=None, allow_ocr_login=False, profile_dir=None):
    playwright = browser = context = None
    try:
        playwright, browser, context, _ = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[纷享销客登录检查]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
        )
        save_login_state(context, storage_state_path)
        return {
            "ok": True,
            "step": "sync",
            "status": "login_checked",
            "message": "纷享销客登录态可用。",
            "artifacts": [],
        }
    finally:
        close_browser_session(browser, context)
        if playwright is not None:
            playwright.stop()


def read_remote_input_from_stderr(prompt):
    emit_progress(prompt, json_output=True)
    return sys.stdin.readline().strip()


def main(argv=None):
    configure_stdio()
    args = parse_args(argv)
    storage_state_path = Path(os.path.expanduser(args.state_path)).resolve()
    profile_dir = resolve_profile_dir(args.profile_dir)

    if args.reset_login_state:
        removed = reset_login_state(storage_state_path, profile_dir)
        payload = {
            "ok": True,
            "step": "sync",
            "status": "reset_login_state",
            "message": "已清理登录态：" + "、".join(removed) if removed else "没有发现需要清理的登录态。",
            "removed": removed,
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["message"])
        return 0

    if args.doctor:
        diagnostics = collect_environment_diagnostics(storage_state_path, profile_dir=profile_dir)
        payload = {
            "ok": all(item[1] for item in diagnostics),
            "step": "sync",
            "status": "doctor",
            "diagnostics": [
                {"label": label, "ok": ok, "detail": detail, "hint": hint}
                for label, ok, detail, hint in diagnostics
            ],
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            for item in payload["diagnostics"]:
                status = "OK" if item["ok"] else "FAIL"
                print(f"[{status}] {item['label']}: {item['detail']}")
        return 0

    if args.login_check:
        progress_callback = lambda message: emit_progress(message, json_output=args.json_output)
        payload = run_login_check(
            storage_state_path,
            show_browser=args.show_browser,
            progress_callback=progress_callback,
            allow_ocr_login=args.allow_ocr_login,
            profile_dir=profile_dir,
        )
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["message"])
        return 0

    report_path = resolve_report_path(args.report_path)
    report_section = find_target_report_section(report_path, args.report_date)
    remote_drafts = []
    if args.remote_drafts_file:
        from fxiaoke_remote_workorder import load_remote_workorder_drafts_file

        remote_drafts = load_remote_workorder_drafts_file(args.remote_drafts_file)
    daily_draft = build_daily_log_draft(report_section)
    remote_drafts = _validate_remote_drafts(remote_drafts, daily_draft.report_date_iso)
    service_drafts = []
    if not args.skip_service_record and (args.onsite_mode == "yes" if args.onsite_mode is not None else is_onsite_workday(
        report_text=report_section, report_path=report_path,
    )):
        selected_type = resolve_report_work_type(
            report_section, report_path,
            {"work_type": args.work_type} if getattr(args, "work_type", "") else None,
        )
        service_drafts = build_service_record_drafts(
            report_section, main_only=True,
            **({"report_path": report_path, "work_type": selected_type} if selected_type else {}),
        )

    if args.preview_only:
        payload = {
            "ok": True,
            "step": "sync",
            "status": "preview",
            "results": [
                build_success_result("daily_log", "preview", daily_draft.work_summary),
                build_success_result(
                    "service_record",
                    "skipped" if not service_drafts else "preview",
                    "纷享销客技术服务记录：当天已标记为非外勤，已跳过自动填写。"
                    if not service_drafts
                    else "\n\n".join(draft.service_content for draft in service_drafts),
                ),
            ],
        }
        payload["results"].extend(
            build_success_result("remote_workorder", "preview", draft.content)
            for draft in remote_drafts
        )
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            for item in payload["results"]:
                print(item["message"])
        return 0

    if not should_auto_confirm(args, stdin_isatty=sys.stdin.isatty()):
        confirm = read_brief_input("\n是否继续自动提交纷享销客日报与技术服务记录？(y/n): ").strip().lower()
        if confirm not in {"y", "yes"}:
            payload = {
                "ok": True,
                "step": "sync",
                "status": "cancelled",
                "results": [
                    build_success_result("daily_log", "cancelled", "已取消。"),
                    build_success_result("service_record", "cancelled", "已取消。"),
                ],
            }
            payload["results"].extend(
                build_success_result("remote_workorder", "cancelled", "已取消。")
                for _draft in remote_drafts
            )
            if args.json_output:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print("已取消。")
            return 0

    tokens = json.loads(args.submission_intent_tokens)
    if not isinstance(tokens, dict):
        raise RuntimeError("共用会话父进程提交凭据无效。")
    steps = ["daily_log", *(["service_record"] if service_drafts else []), *(["remote_workorder"] if remote_drafts else [])]
    intent_paths: dict[str, Path | None] = {}
    evidence_contexts = {}
    customers = {
        "daily_log": [daily_draft.customer_name],
        "service_record": [draft.customer_name for draft in service_drafts],
        "remote_workorder": [draft.customer_name for draft in remote_drafts],
    }
    for step in steps:
        evidence_context: dict[str, Any] = {}
        existing, intent_path = reserve_fxiaoke_submission_intent(
            report_section, step, profile_dir, report_path=report_path, inherited_token=tokens.get(step, ""),
            evidence_context=evidence_context,
        )
        if existing is not None:
            for owned_path in intent_paths.values():
                clear_fxiaoke_submission_intent(owned_path)
            payload = build_fxiaoke_submission_intent_blocked_result(step, "共用会话", existing)
            print(json.dumps(payload, ensure_ascii=False) if args.json_output else payload["message"])
            return 1
        intent_paths[step] = intent_path
        evidence_contexts[step] = evidence_context
        checkpoint_fxiaoke_submission(evidence_context, expected_customers=customers[step])
    milestones: dict[tuple[str, int], dict[str, bool]] = {}

    def milestone_callback(event, **fields):
        if fields.get("step"):
            milestones.setdefault((fields["step"], fields.get("child_index", 0)), {})[event] = True
            checkpoint_fxiaoke_submission(evidence_contexts.get(fields["step"]),
                child_index=fields.get("child_index", 0), event=event, fields=fields)

    result_counts: dict[str, int] = {}

    def result_callback(item):
        step = item.get("step")
        if step not in evidence_contexts:
            return
        index = result_counts.get(step, 0)
        if not item.get("ok") and not item.get("retry_action"):
            state = milestones.get((step, index), {})
            item["retry_action"] = "verify_crm" if state.get("submit_clicked") or state.get("crm_id_captured") or item.get("status") in {"uncertain", "crm_id_captured", "timeout"} else "safe_retry"
        checkpoint_fxiaoke_submission(evidence_contexts[step], child_index=index, result=item)
        result_counts[step] = index + 1

    progress_callback = lambda message: emit_progress(message, json_output=args.json_output)
    results = run_shared_sync(
        report_section,
        report_path,
        storage_state_path=storage_state_path,
        skip_service_record=args.skip_service_record,
        remote_drafts=remote_drafts,
        remote_input_func=read_remote_input_from_stderr,
        show_browser=args.show_browser,
        progress_callback=progress_callback,
        allow_ocr_login=args.allow_ocr_login,
        profile_dir=profile_dir,
        milestone_callback=milestone_callback,
        result_callback=result_callback,
        **({"cached_login_only": True} if tokens else {}),
        **({"onsite_mode": args.onsite_mode == "yes"} if args.onsite_mode is not None else {}),
        **({"work_type": args.work_type} if getattr(args, "work_type", "") else {}),
    )
    for step, intent_path in intent_paths.items():
        items = [item for item in results if item.get("step") == step]
        for item in items[result_counts.get(step, 0):]:
            result_callback(item)
        if items:
            recorded = finish_fxiaoke_direct_submission(report_section, report_path, step, {"results": items}, intent_path)
            if recorded.get("local_recording_error"):
                for item in items:
                    item["local_recording_error"] = recorded["local_recording_error"]
                    item["message"] += recorded["message"]
            if recorded.get("status") == "uncertain":
                results.append({
                    "ok": False, "step": step, "status": "uncertain", "retry_action": "verify_crm",
                    "message": recorded["message"], "artifacts": [],
                })
    ok = all(item["ok"] for item in results)
    payload = {
        "ok": ok,
        "step": "sync",
        "status": "submitted" if ok else "partial_error",
        "message": "\n".join(item["message"] for item in results),
        "results": results,
        "artifacts": [artifact for item in results for artifact in item.get("artifacts", [])],
    }
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in results:
            print(item["message"])
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        json_requested = "--json" in sys.argv
        artifacts = list(getattr(exc, "artifacts", []) or [])
        payload = {
            "ok": False,
            "step": "sync",
            "status": getattr(exc, "status", "error"),
            "retry_action": getattr(exc, "retry_action", ""),
            "message": str(exc),
            "results": [],
            "artifacts": artifacts,
        }
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False))
            raise SystemExit(1)
        raise
