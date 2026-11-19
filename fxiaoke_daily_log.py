#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys
import time
from html import unescape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from daily_report import (
    SubmissionEvidenceError,
    checkpoint_fxiaoke_submission,
    emit_terminal_message,
    configure_stdio,
    read_brief_input,
    resolve_report_path,
    should_auto_confirm,
    reserve_fxiaoke_submission_intent,
    clear_fxiaoke_submission_intent as clear_fxiaoke_submission_intent,
    build_fxiaoke_submission_intent_blocked_result,
    finish_fxiaoke_direct_submission,
)
from integrations.fxiaoke.tenant import require_service_owner
from integrations.fxiaoke.daily_log import (
    DailyLogDraft as DailyLogDraft,
    build_daily_log_draft,
    build_tomorrow_plan as build_tomorrow_plan,
    build_work_summary as build_work_summary,
    extract_report_date as extract_report_date,
)
from fxiaoke_service_record import (
    FXIAOKE_HOME_URL,
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_TIMEOUT_MS,
    CachedLoginExpired,
    FAILURE_ARTIFACT_DIR_NAME,
    collect_environment_diagnostics,
    close_browser_session,
    capture_failure_artifacts as capture_failure_artifacts,
    trace_phase,
    retain_submission_failure_trace,
    fast_goto,
    find_target_report_section,
    open_authenticated_page,
    reset_login_state,
    resolve_profile_dir,
    save_login_state,
)


TEXTAREA_SELECTOR = "textarea[name='content']"
STREAM_PUBLISH_WRAPPER_SELECTOR = ".publish-wrapper"
WORK_CIRCLE_LINK_SELECTOR = 'a[data-link="#stream"], a[href="#stream"]'
WORK_LOG_LINK_SELECTOR = 'a[data-link="#plan"], a[href="#plan"]'
JOURNAL_TAB_SELECTOR = "li.fs-publish-nav.j-tab-paas_JournalObj"
JOURNAL_TAB_LINK_SELECTOR = f"{JOURNAL_TAB_SELECTOR} a"
VISIBLE_JOURNAL_PANEL_SELECTOR = ".fs-publish-paas_JournalObj"
VISIBLE_JOURNAL_PUBLISH_SELECTOR = (
    f"{VISIBLE_JOURNAL_PANEL_SELECTOR} .xv-asynccomp-publish-open:visible"
)
VISIBLE_JOURNAL_TEXTAREA_SELECTOR = (
    f"{VISIBLE_JOURNAL_PUBLISH_SELECTOR} textarea[name='content']:visible"
)
PUBLISH_BUTTON_SELECTOR = (
    f"{VISIBLE_JOURNAL_PUBLISH_SELECTOR} span.crm-btn.crm-btn-primary.j-ok[action-type='dialogEnter']"
)
REPORT_DATE_FIELD_SELECTOR = (
    f"{VISIBLE_JOURNAL_PUBLISH_SELECTOR} .f-item-inner[data-apiname='journal_time'] input"
)
TEXTAREA_APINAMES = {
    "工作总结": "work_summary",
    "明日工作计划": "work_plan",
    "心得体会": "work_experience",
}
STREAM_READY_TIMEOUT_MS = 30_000
STREAM_READY_SHORT_TIMEOUT_MS = 2_000
STREAM_READY_POLL_MS = 250
STREAM_RECOVERY_TIMEOUT_MS = 12_000
STREAM_RECOVERY_SETTLE_MS = 800
FXIAOKE_WORK_LOG_URL = FXIAOKE_HOME_URL.rsplit("#", 1)[0] + "#plan"
WORK_LOG_LINK_SELECTOR = 'a[data-link="#plan"]'
WORK_LOG_FILTER_SELECTOR = ".filter-header"
WORK_LOG_SEARCH_PATH = "/FHH/EM1HFEED/Feeds/searchFeedList/plan"
WORK_LOG_ALLOWED_HOSTS = ("fxiaoke.com", "www.fxiaoke.com")
WORK_LOG_ALLOWED_HOST_SUFFIX = ".fxiaoke.com"
WORK_LOG_READY_TIMEOUT_MS = 30_000
WORK_LOG_HISTORY_LIMIT = 20
WORK_LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")
JOURNAL_TAB_READY_TIMEOUT_MS = 20_000
EDITOR_OPEN_DEADLINE_SECONDS = 30
EDITOR_SETTLE_SECONDS = 0.9
EDITOR_READY_SETTLE_MS = 150
EDITOR_POLL_MS = 100
DATE_FIELD_SETTLE_MS = 150
PUBLISH_CLICK_SETTLE_MS = 150
PUBLISH_SUCCESS_TIMEOUT_SECONDS = 35
PUBLISH_SUCCESS_POLL_MS = 250
PUBLISH_SURFACE_CONFIRM_TIMEOUT_SECONDS = 3
PUBLISH_BODY_TEXT_TIMEOUT_MS = 1_000
POST_SUBMIT_HISTORY_READY_TIMEOUT_MS = 5_000
WORK_LOG_HISTORY_QUERY_TIMEOUT_MS = 8_000
PUBLISH_CONFIRMATION_BUDGET_SECONDS = 45
PUBLISH_CONFIRMATION_READ_ATTEMPTS = 3
REPORT_DATE_FIELD_XPATHS = [
    "(//*[normalize-space()='时间']/ancestor::div[.//input[contains(@placeholder,'日期')]][1]//input[contains(@placeholder,'日期')][1])",
    "(//*[normalize-space()='时间']/following::input[contains(@placeholder,'日期')][1])",
    "(//*[normalize-space()='时间']/ancestor::div[.//input][1]//input[1])",
]


def emit_progress(message, json_output=False):
    stream = sys.stderr if json_output else sys.stdout
    emit_terminal_message(message, file=stream)


class AutomationFailure(RuntimeError):
    def __init__(self, message, artifacts=None, status="error"):
        super().__init__(message)
        self.artifacts = list(artifacts or [])
        self.status = status


class PublishConfirmationUncertain(RuntimeError):
    pass


class HistoryQueryError(RuntimeError):
    """Keep failed/incomplete reads distinct from a confirmed empty history."""

    def __init__(self, query_status, message):
        super().__init__(message)
        self.query_status = query_status


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="根据固定日报内容自动发布纷享销客工作圈日报。"
    )
    parser.add_argument("--submission-intent-token", default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--date",
        dest="report_date",
        help="指定日报日期，支持 YYYY-MM-DD 或 YYYY.MM.DD。",
    )
    parser.add_argument(
        "--path",
        dest="report_path",
        help="覆盖默认日报路径。",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成日报发布草稿，不启动浏览器自动化。",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过确认，直接执行浏览器自动化。",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="检查本地浏览器自动化环境是否已准备好。",
    )
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
        "--show-browser",
        action="store_true",
        help="显示真实浏览器窗口，默认无头执行。",
    )
    parser.add_argument(
        "--allow-ocr-login",
        action="store_true",
        help="登录态失效且出现图形验证码时，允许使用 OCR 自动识别；默认改为人工/扫码恢复登录。",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出 JSON 结果，供其他脚本联动使用。",
    )
    return parser.parse_args(argv)


def print_draft(draft, report_path):
    print("--- 纷享销客日报发布助手 ---")
    print(f"日报来源：{report_path}")
    print(f"日报日期：{draft.report_date_iso}")
    print(f"服务客户：{draft.customer_name}")
    print("\n工作总结预览：")
    print("=" * 50)
    print(draft.work_summary)
    print("=" * 50)
    print("\n明日工作计划预览：")
    print("=" * 50)
    print(draft.tomorrow_plan)
    print("=" * 50)


def resolve_failure_artifact_root(report_path):
    return report_path.parent / FAILURE_ARTIFACT_DIR_NAME



def click_first_visible(locator):
    try:
        count = min(locator.count(), 8)
    except PlaywrightError:
        return False
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                item.click()
                return True
        except PlaywrightError:
            continue
    return False


def click_visible_selector(page, selector):
    try:
        return click_first_visible(page.locator(selector))
    except (AttributeError, PlaywrightError):
        return False


def locate_visible_field(page, xpath_candidates):
    for xpath in xpath_candidates:
        locator = page.locator(f"xpath={xpath}")
        if not locator.count():
            continue
        candidate = locator.first
        try:
            if candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


def wait_for_visible_selector(page, selector, timeout_ms, poll_ms=STREAM_READY_POLL_MS):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                return True
        except PlaywrightError:
            pass
        page.wait_for_timeout(poll_ms)
    return False


def is_visible_selector(page, selector):
    try:
        locator = page.locator(selector)
        return bool(locator.count() and locator.first.is_visible())
    except (AttributeError, PlaywrightError):
        return False


def is_daily_log_editor_ready(page):
    try:
        return (
            page.locator(VISIBLE_JOURNAL_TEXTAREA_SELECTOR).count() >= 3
            and page.locator(REPORT_DATE_FIELD_SELECTOR).count() >= 1
        )
    except (AttributeError, PlaywrightError):
        return False


def wait_for_publish_surface_ready(page, timeout_ms):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if is_visible_selector(page, STREAM_PUBLISH_WRAPPER_SELECTOR):
            return True
        if is_daily_log_editor_ready(page):
            return True
        page.wait_for_timeout(STREAM_READY_POLL_MS)
    return False


def reload_current_page(page):
    try:
        page.reload(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        page.wait_for_timeout(STREAM_RECOVERY_SETTLE_MS)
        return True
    except PlaywrightError:
        return False


def try_recover_stream_publish_area(page):
    recovery_steps = (
        lambda: click_visible_selector(page, WORK_CIRCLE_LINK_SELECTOR),
        lambda: reload_current_page(page),
        lambda: click_visible_selector(page, WORK_LOG_LINK_SELECTOR),
        lambda: fast_goto(page, FXIAOKE_WORK_LOG_URL) or True,
        lambda: fast_goto(page, FXIAOKE_HOME_URL) or True,
    )

    for recover in recovery_steps:
        try:
            recovery_started = recover()
        except PlaywrightError:
            continue
        if not recovery_started:
            continue
        if wait_for_publish_surface_ready(page, STREAM_RECOVERY_TIMEOUT_MS):
            return True
    return False


def ensure_stream_publish_ready(page):
    if wait_for_visible_selector(
        page,
        STREAM_PUBLISH_WRAPPER_SELECTOR,
        STREAM_READY_SHORT_TIMEOUT_MS,
    ):
        return

    fast_goto(page, FXIAOKE_HOME_URL)
    if wait_for_visible_selector(
        page,
        STREAM_PUBLISH_WRAPPER_SELECTOR,
        STREAM_READY_TIMEOUT_MS,
    ):
        return

    if try_recover_stream_publish_area(page):
        return

    raise RuntimeError("纷享销客工作圈发布器加载超时，请稍后重试。")


def try_open_daily_log_editor_once(page, progress_callback=None):
    if is_daily_log_editor_ready(page):
        return True

    ensure_stream_publish_ready(page)
    if is_daily_log_editor_ready(page):
        page.wait_for_timeout(EDITOR_READY_SETTLE_MS)
        return True

    if progress_callback:
        progress_callback("[纷享销客日报/打开] 正在切换日志发布页签...")
    page.locator(JOURNAL_TAB_SELECTOR).first.wait_for(
        state="visible",
        timeout=JOURNAL_TAB_READY_TIMEOUT_MS,
    )

    deadline = time.time() + EDITOR_OPEN_DEADLINE_SECONDS
    attempts = (
        lambda: page.locator(JOURNAL_TAB_SELECTOR).first.click(timeout=3_000),
        lambda: page.locator(JOURNAL_TAB_LINK_SELECTOR).first.click(timeout=3_000),
        lambda: page.locator(JOURNAL_TAB_SELECTOR).first.click(force=True, timeout=3_000),
        lambda: page.locator(JOURNAL_TAB_LINK_SELECTOR).first.click(force=True, timeout=3_000),
        lambda: page.locator(JOURNAL_TAB_SELECTOR).first.evaluate("(el) => el.click()"),
        lambda: page.locator(JOURNAL_TAB_LINK_SELECTOR).first.evaluate("(el) => el.click()"),
    )

    attempt_index = 0
    while time.time() < deadline:
        if is_daily_log_editor_ready(page):
            page.wait_for_timeout(EDITOR_READY_SETTLE_MS)
            return True

        if not page.locator(STREAM_PUBLISH_WRAPPER_SELECTOR).count():
            ensure_stream_publish_ready(page)

        action = attempts[attempt_index % len(attempts)]
        attempt_index += 1
        if progress_callback and attempt_index > 1:
            progress_callback(f"[纷享销客日报/打开] 正在重试切换日志页签（第 {attempt_index} 次）...")
        try:
            action()
        except PlaywrightError:
            pass

        settle_deadline = time.time() + EDITOR_SETTLE_SECONDS
        while time.time() < settle_deadline:
            if is_daily_log_editor_ready(page):
                page.wait_for_timeout(EDITOR_READY_SETTLE_MS)
                return True
            page.wait_for_timeout(EDITOR_POLL_MS)

    return False


def open_daily_log_editor(page, progress_callback=None):
    if is_daily_log_editor_ready(page):
        return

    if try_open_daily_log_editor_once(page, progress_callback=progress_callback):
        return

    if progress_callback:
        progress_callback("[纷享销客日报/打开] 日志组件长时间未就绪，正在重新进入工作圈后重试...")
    fast_goto(page, FXIAOKE_HOME_URL)
    if try_open_daily_log_editor_once(page, progress_callback=progress_callback):
        return

    raise RuntimeError("无法打开纷享销客日报编辑器，请确认工作圈页面中的“日志”发布面板可用。")


def locate_labeled_textarea(page, label_text, fallback_index):
    apiname = TEXTAREA_APINAMES.get(label_text)
    if apiname:
        direct = page.locator(
            f"{VISIBLE_JOURNAL_PUBLISH_SELECTOR} .f-item-inner[data-apiname='{apiname}'] {TEXTAREA_SELECTOR}"
        )
        if direct.count():
            return direct.first
    labeled = locate_visible_field(
        page,
        [
            f"(//*[contains(@class,'fs-publish-paas_JournalObj') and not(contains(@style,'display: none'))]//*[normalize-space()='{label_text}']/ancestor::div[.//textarea[@name='content']][1]//textarea[@name='content'][1])",
            f"(//*[contains(@class,'fs-publish-paas_JournalObj') and not(contains(@style,'display: none'))]//*[normalize-space()='{label_text}']/following::textarea[@name='content'][1])",
        ],
    )
    if labeled is not None:
        return labeled
    return page.locator(VISIBLE_JOURNAL_TEXTAREA_SELECTOR).nth(fallback_index)


def ensure_report_date(page, report_date_iso):
    field = page.locator(REPORT_DATE_FIELD_SELECTOR).first
    if not field.count():
        field = locate_visible_field(page, REPORT_DATE_FIELD_XPATHS)
    if field is None:
        raise RuntimeError("无法定位纷享销客日报页面中的日期字段。")

    try:
        current_value = (field.input_value() or "").strip()
    except PlaywrightError as exc:
        raise RuntimeError("无法读取纷享销客日报页面中的日期字段。") from exc

    if current_value == report_date_iso:
        return

    try:
        field.click()
        field.press("Meta+a")
        field.fill(report_date_iso)
        field.press("Tab")
        page.wait_for_timeout(DATE_FIELD_SETTLE_MS)
        if (field.input_value() or "").strip() == report_date_iso:
            return
    except PlaywrightError as exc:
        raise RuntimeError("无法写入纷享销客日报页面中的日期字段。") from exc

    raise RuntimeError("纷享销客日报页面中的日期字段校验失败。")


def read_textarea_value(field):
    try:
        return (field.input_value() or "").strip()
    except PlaywrightError:
        try:
            return (field.evaluate("(el) => el.value || ''") or "").strip()
        except PlaywrightError:
            raise


def force_set_textarea_value(field, value):
    field.evaluate(
        """(el, value) => {
            el.value = value || '';
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        }""",
        value,
    )


def fill_textarea(field, value, label_text="文本"):
    field.click()
    field.fill("")
    if value:
        field.fill(value)
    try:
        field.press("Tab")
    except PlaywrightError:
        pass
    if read_textarea_value(field) == (value or "").strip():
        return
    force_set_textarea_value(field, value)
    if read_textarea_value(field) != (value or "").strip():
        raise RuntimeError(f"纷享销客日报页面中的“{label_text}”字段写入失败。")


def daily_log_editor_values_cleared(page, *, deadline=None):
    if not is_daily_log_editor_ready(page):
        return False
    fields = [
        locate_labeled_textarea(page, "工作总结", 0),
        locate_labeled_textarea(page, "明日工作计划", 1),
        locate_labeled_textarea(page, "心得体会", 2),
    ]
    if deadline is not None:
        return all(
            (field.input_value(timeout=_remaining_timeout_ms(deadline, PUBLISH_BODY_TEXT_TIMEOUT_MS)) or "").strip() == ""
            for field in fields
        )
    return all(read_textarea_value(field) == "" for field in fields)


def build_daily_log_marker(draft):
    return draft.report_date_cn


def daily_log_customer_in_text(draft, text):
    customer_name = (draft.customer_name or "").strip()
    if not customer_name:
        return False
    if customer_name in text:
        return True
    customer_parts = [part.strip() for part in re.split(r"[、,，]+", customer_name) if part.strip()]
    return bool(customer_parts) and all(part in text for part in customer_parts)


def daily_log_signature_in_text(draft, text):
    marker = build_daily_log_marker(draft)
    return (
        marker in text
        and daily_log_customer_in_text(draft, text)
        and bool(draft.verification_snippet)
        and draft.verification_snippet in text
    )


def has_published_daily_log(page, draft, body_text=None):
    text = body_text if body_text is not None else read_page_body_text(page)
    return daily_log_signature_in_text(draft, text)


def read_page_body_text(page, timeout_ms=PUBLISH_BODY_TEXT_TIMEOUT_MS):
    try:
        return page.locator("body").inner_text(timeout=timeout_ms)
    except PlaywrightError:
        return ""


def build_work_log_history_query(report_date_iso):
    report_date = datetime.strptime(str(report_date_iso), "%Y-%m-%d")
    day_start = report_date.replace(tzinfo=WORK_LOG_TIMEZONE)
    day_end = day_start + timedelta(days=1)
    return {
        "filterType": 1000,
        "searchArg": {
            "type": 1,
            "subType": 0,
            "keyword": "",
            "startTime": int(day_start.timestamp()),
            "endTime": int(day_end.timestamp()),
            "senderId": "",
            "fireType": 10,
            "limit": WORK_LOG_HISTORY_LIMIT,
            "senderIds": [],
            "departmentIds": [],
        },
    }


def work_log_query_contract_violation(url):
    """Return why a captured URL is not the confirmed read-only log search endpoint."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "地址无法解析"
    if parts.scheme != "https":
        return "必须使用 https"
    host = (parts.hostname or "").lower()
    if host not in WORK_LOG_ALLOWED_HOSTS and not host.endswith(WORK_LOG_ALLOWED_HOST_SUFFIX):
        return f"主机“{host or '空'}”不在纷享销客域名内"
    if parts.path.rstrip("/") != WORK_LOG_SEARCH_PATH.rstrip("/"):
        return f"路径“{parts.path or '空'}”不是日志查询接口"
    if parts.fragment:
        return "地址不应包含片段"
    return ""


def _remaining_timeout_ms(deadline, limit_ms):
    if deadline is None:
        return limit_ms
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise HistoryQueryError("timeout", "日报发布后的只读确认已达到总时限。")
    return min(limit_ms, remaining_ms)


def open_work_log_history(page, timeout_ms=WORK_LOG_READY_TIMEOUT_MS, *, deadline=None):
    log_link = page.locator(WORK_LOG_LINK_SELECTOR).first
    log_link.wait_for(state="visible", timeout=_remaining_timeout_ms(deadline, timeout_ms))
    with page.expect_request(
        lambda request: work_log_query_contract_violation(request.url) == "",
        timeout=_remaining_timeout_ms(deadline, timeout_ms),
    ) as request_info:
        if deadline is None:
            log_link.click()
        else:
            log_link.click(timeout=_remaining_timeout_ms(deadline, timeout_ms))
    page.locator(WORK_LOG_FILTER_SELECTOR).first.wait_for(
        state="visible",
        timeout=_remaining_timeout_ms(deadline, timeout_ms),
    )
    query_url = request_info.value.url
    violation = work_log_query_contract_violation(query_url)
    if violation:
        raise HistoryQueryError(
            "contract_violation",
            f"日志历史查询地址不符合只读契约：{violation}；未发起任何查询。",
        )
    return query_url


def query_work_log_history(page, query_url, report_date_iso, *, timeout_ms=WORK_LOG_HISTORY_QUERY_TIMEOUT_MS):
    violation = work_log_query_contract_violation(query_url)
    if violation:
        raise HistoryQueryError("contract_violation", f"日志历史查询地址不符合只读契约：{violation}；未发起任何查询。")
    if timeout_ms <= 0:
        raise HistoryQueryError("timeout", "日报发布后的只读确认已达到总时限。")
    payload = build_work_log_history_query(report_date_iso)
    response = page.evaluate(
        """async ({url, payload, timeoutMs}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json;charset=UTF-8'},
                    body: JSON.stringify(payload),
                    signal: controller.signal,
                });
                // Retain HTTP/auth evidence even when the body is an HTML error.
                const text = await response.text();
                let body = null;
                try { body = JSON.parse(text); } catch (_) {}
                return {
                    status: response.status,
                    url: response.url,
                    body,
                    contentType: response.headers.get('content-type') || '',
                    textSample: text.slice(0, 400),
                };
            } catch (error) {
                return {
                    status: 0,
                    error: error && error.name === 'AbortError' ? 'timeout' : String(error),
                };
            } finally {
                clearTimeout(timer);
            }
        }""",
        {
            "url": query_url,
            "payload": payload,
            "timeoutMs": min(timeout_ms, WORK_LOG_HISTORY_QUERY_TIMEOUT_MS),
        },
    )
    if isinstance(response, dict) and response.get("error") == "timeout":
        raise HistoryQueryError("timeout", "纷享销客日志历史查询超时。")
    if isinstance(response, dict) and response.get("status") == 0 and response.get("error"):
        raise HistoryQueryError("network_error", "纷享销客日志历史查询暂时无法连接。")
    if isinstance(response, dict):
        try:
            login_redirect = urlsplit(str(response.get("url", ""))).path.startswith("/proj/page/login")
        except ValueError:
            login_redirect = False
        if response.get("status") == 401 or login_redirect:
            raise HistoryQueryError("auth_expired", "纷享销客日志历史查询认证失效，请先核对登录状态。")
        if response.get("status") == 403:
            raise HistoryQueryError("permission_denied", "纷享销客日志历史查询权限不足。")
        response_url = response.get("url")
        if response_url and work_log_query_contract_violation(response_url):
            raise HistoryQueryError("contract_violation", "日志历史查询重定向至未确认的地址，已停止确认。")
        if response.get("status") == 200 and response.get("body") is None:
            # An expired session often answers 200 with the HTML login page
            # instead of 401; treating it as a format error hides the real cause.
            content_type = str(response.get("contentType") or "").lower()
            sample = str(response.get("textSample") or "").lower()
            if "text/html" in content_type and any(
                marker in sample for marker in ("登录", "login", "扫码")
            ):
                raise HistoryQueryError(
                    "auth_expired",
                    "纷享销客日志历史查询返回登录页，认证已失效，请先核对登录状态。",
                )
    if not isinstance(response, dict) or response.get("status") != 200:
        raise HistoryQueryError("query_failed", "纷享销客日志历史查询未返回成功响应。")
    history = response.get("body")
    if not isinstance(history, dict):
        raise HistoryQueryError("invalid_response", "纷享销客日志历史查询返回格式异常。")
    result = history.get("Result", {})
    value = history.get("Value")
    if not isinstance(result, dict) or not isinstance(value, dict) or not isinstance(value.get("feedList"), list):
        raise HistoryQueryError("invalid_response", "纷享销客日志历史查询返回格式异常。")
    if result.get("FailureCode") not in (None, 0) or result.get("StatusCode") not in (
        None,
        0,
    ):
        raise HistoryQueryError("query_failed", "纷享销客日志历史查询被拒绝。")
    return history


def find_daily_log_in_history(history, draft):
    owner = require_service_owner()
    value = history.get("Value") if isinstance(history, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("feedList"), list):
        raise HistoryQueryError("invalid_response", "纷享销客日志历史查询返回格式异常。")
    feed_list = value["feedList"]
    for feed in feed_list:
        if not isinstance(feed, dict):
            raise HistoryQueryError("invalid_response", "纷享销客日志历史查询返回格式异常。")
        if "日报" not in _history_text(feed.get("feedStatus", {})):
            continue
        fields = {
            _history_content(item.get("label", {})): _history_content(item.get("content", {}))
            for item in feed.get("bizDataArea", [])
            if isinstance(item, dict)
        }
        profile = feed.get("feedProfile") or {}
        creator = _history_content(profile.get("name", {})) or fields.get("创建人", "")
        if creator != owner:
            continue
        if draft.report_date_cn not in fields.get("时间", ""):
            continue
        summary = fields.get("工作总结", "")
        if (
            not daily_log_customer_in_text(draft, summary)
            or _normalize_history_field(summary) != _normalize_history_field(draft.work_summary)
            or _normalize_history_field(fields.get("明日工作计划", "")) != _normalize_history_field(draft.tomorrow_plan)
        ):
            raise HistoryQueryError("conflict", "同日已存在本人的日报，但客户或内容不一致，请先核对，禁止重复发布。")
        feed_id = feed.get("feedId")
        if not ((type(feed_id) is int and feed_id > 0)
                or (isinstance(feed_id, str) and re.fullmatch(r"[1-9][0-9]*", feed_id))):
            raise HistoryQueryError("invalid_response", "匹配日报缺少有效日志编号，无法完成确认。")
        return {
            "feed_id": feed_id,
            "creator": creator,
            "text": "\n".join(fields.values()),
        }
    if len(feed_list) >= WORK_LOG_HISTORY_LIMIT:
        raise HistoryQueryError("incomplete", "日志历史查询达到分页上限，不能据此确认当天日报不存在。")
    return None


def _normalize_history_field(value):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _history_text(value):
    values = []

    def collect(item):
        if isinstance(item, str):
            values.append(item)
            return
        if isinstance(item, list):
            for child in item:
                collect(child)
            return
        if isinstance(item, dict):
            for child in item.values():
                collect(child)

    collect(value)
    return "\n".join(values)


def _history_content(value):
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return value["content"].strip()
    return _history_text(value).strip()


def wait_for_publish_success(
    page,
    draft,
    baseline_body_text="",
    accept_editor_reset=False,
    timeout_seconds=PUBLISH_SUCCESS_TIMEOUT_SECONDS,
    deadline=None,
):
    baseline_signature_exists = daily_log_signature_in_text(draft, baseline_body_text)
    surface_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        surface_deadline = min(surface_deadline, deadline)
    while time.monotonic() < surface_deadline:
        body_text = read_page_body_text(page, timeout_ms=_remaining_timeout_ms(surface_deadline, PUBLISH_BODY_TEXT_TIMEOUT_MS))
        if daily_log_signature_in_text(draft, body_text) and not baseline_signature_exists:
            return
        cleared = (daily_log_editor_values_cleared(page) if deadline is None
                   else daily_log_editor_values_cleared(page, deadline=surface_deadline)) if body_text and accept_editor_reset else False
        if cleared:
            return
        page.wait_for_timeout(_remaining_timeout_ms(surface_deadline, PUBLISH_SUCCESS_POLL_MS))
    raise RuntimeError("日报发布后未在纷享销客工作圈中检测到新内容。")


def find_submitted_daily_log_in_history(page, draft, *, deadline=None, query_state=None):
    require_service_owner()
    state = query_state if query_state is not None else {}
    if state.get("page") is not page:
        state.clear()
    query_url = state.get("url")
    if not query_url:
        timeout_ms = _remaining_timeout_ms(deadline, POST_SUBMIT_HISTORY_READY_TIMEOUT_MS)
        options = {"deadline": deadline} if deadline is not None else {}
        query_url = open_work_log_history(page, timeout_ms=timeout_ms, **options)
        state.update(page=page, url=query_url)
    options = {"timeout_ms": _remaining_timeout_ms(deadline, WORK_LOG_HISTORY_QUERY_TIMEOUT_MS)} if deadline is not None else {}
    history = query_work_log_history(page, query_url, draft.report_date_iso, **options)
    return find_daily_log_in_history(history, draft)


def emit_daily_log_phase_timing(progress_callback, label, started_at):
    if progress_callback:
        progress_callback(
            f"[纷享销客日报/耗时] {label} {time.monotonic() - started_at:.3f} 秒"
        )


def publish_daily_log(
    page,
    draft,
    progress_callback=None,
    milestone_callback=None,
    pre_submit_history_check=False,
):
    require_service_owner()
    trace_phase(page, "daily_log/打开工作圈")
    ensure_stream_publish_ready(page)
    baseline_body_text = read_page_body_text(page)
    if not pre_submit_history_check and has_published_daily_log(page, draft, body_text=baseline_body_text):
        return "already_exists"

    if pre_submit_history_check:
        if progress_callback:
            progress_callback("[纷享销客日报/预检] 正在只读核对“我发出的日志”，避免重复提交...")
        trace_phase(page, "daily_log/查重")
        matched_feed = None
        precheck_started_at = time.monotonic()
        try:
            matched_feed = find_submitted_daily_log_in_history(page, draft)
        except Exception as history_error:
            raise RuntimeError(f"日报历史预检未完成，已停止发布，请先核对：{history_error}") from history_error
        finally:
            try:
                fast_goto(page, FXIAOKE_HOME_URL)
            except Exception:
                pass
            emit_daily_log_phase_timing(progress_callback, "历史预检", precheck_started_at)
        if matched_feed is not None:
            if milestone_callback and matched_feed.get("feed_id"):
                milestone_callback("crm_id_captured", feed_id=matched_feed["feed_id"])
            if progress_callback:
                progress_callback("[纷享销客日报/预检] 已在“我发出的日志”中找到匹配日报，跳过重复提交。")
            return "already_exists"
        baseline_body_text = read_page_body_text(page)

    if progress_callback:
        progress_callback("[纷享销客日报] 2/4 正在打开日报编辑器...")
    trace_phase(page, "daily_log/打开编辑器")
    editor_started_at = time.monotonic()
    open_daily_log_editor(page, progress_callback=progress_callback)
    emit_daily_log_phase_timing(progress_callback, "打开编辑器", editor_started_at)
    if progress_callback:
        progress_callback("[纷享销客日报] 3/4 正在填写日报内容...")
    trace_phase(page, "daily_log/填写与校验")
    form_fill_started_at = time.monotonic()
    ensure_report_date(page, draft.report_date_iso)
    work_summary_field = locate_labeled_textarea(page, "工作总结", 0)
    tomorrow_plan_field = locate_labeled_textarea(page, "明日工作计划", 1)
    reflection_field = locate_labeled_textarea(page, "心得体会", 2)
    fill_textarea(work_summary_field, draft.work_summary, "工作总结")
    fill_textarea(tomorrow_plan_field, draft.tomorrow_plan, "明日工作计划")
    fill_textarea(reflection_field, draft.reflection, "心得体会")
    emit_daily_log_phase_timing(progress_callback, "填写表单", form_fill_started_at)

    submit_button = page.locator(PUBLISH_BUTTON_SELECTOR).filter(has_text="发布").first
    trace_phase(page, "daily_log/提交")
    confirmation_started_at = time.monotonic()
    confirmation_deadline = confirmation_started_at + PUBLISH_CONFIRMATION_BUDGET_SECONDS
    if milestone_callback:
        milestone_callback("submit_clicked")
    if progress_callback:
        progress_callback("[纷享销客日报] 4/4 正在点击发布，随后核对提交结果...")
    surface_error = None
    try:
        submit_button.click(timeout=_remaining_timeout_ms(confirmation_deadline, DEFAULT_TIMEOUT_MS))
        page.wait_for_timeout(_remaining_timeout_ms(confirmation_deadline, PUBLISH_CLICK_SETTLE_MS))
        wait_for_publish_success(
            page,
            draft,
            baseline_body_text=baseline_body_text,
            accept_editor_reset=True,
            timeout_seconds=min(PUBLISH_SURFACE_CONFIRM_TIMEOUT_SECONDS, _remaining_timeout_ms(confirmation_deadline, 3000) / 1000),
            deadline=confirmation_deadline,
        )
    except Exception as exc:
        surface_error = exc
    try:
        if surface_error is None and not pre_submit_history_check:
            return "submitted"
        if progress_callback:
            surface_message = "页面未回显" if surface_error is not None else "页面已有发布信号"
            progress_callback(
                f"[纷享销客日报/复核] {surface_message}，正在查询“我发出的日志”"
                f"（入口最多 {POST_SUBMIT_HISTORY_READY_TIMEOUT_MS // 1000} 秒，"
                f"查询最多 {WORK_LOG_HISTORY_QUERY_TIMEOUT_MS // 1000} 秒；仅只读回查）..."
            )
        matched_feed = None
        verification_error = surface_error
        query_state: dict[str, Any] = {}
        for attempt in range(PUBLISH_CONFIRMATION_READ_ATTEMPTS):
            try:
                _remaining_timeout_ms(confirmation_deadline, WORK_LOG_HISTORY_QUERY_TIMEOUT_MS)
                trace_phase(page, "daily_log/回读核验")
                matched_feed = find_submitted_daily_log_in_history(
                    page, draft, deadline=confirmation_deadline, query_state=query_state,
                )
                if matched_feed is not None:
                    break
            except Exception as exc:
                verification_error = exc
                if not isinstance(exc, HistoryQueryError) or exc.query_status not in {"timeout", "network_error"}:
                    break
            if attempt + 1 < PUBLISH_CONFIRMATION_READ_ATTEMPTS:
                try:
                    page.wait_for_timeout(_remaining_timeout_ms(confirmation_deadline, (attempt + 1) * 1000))
                except (HistoryQueryError, PlaywrightError) as exc:
                    verification_error = exc
                    break
        if matched_feed is None:
            raise PublishConfirmationUncertain(
                "日报已开始发布，但“我发出的日志”只读回查仍未确认匹配日报；"
                "请不要重复发布，先在 CRM 中核对当天日报。"
            ) from verification_error
        if milestone_callback and matched_feed.get("feed_id"):
            milestone_callback("crm_id_captured", feed_id=matched_feed["feed_id"])
        if progress_callback:
            progress_callback("[纷享销客日报/复核] 已在“我发出的日志”中确认当天日报。")
    finally:
        emit_daily_log_phase_timing(progress_callback, "发布确认", confirmation_started_at)
    return "submitted"


def run_browser_automation(
    draft,
    storage_state_path,
    report_path,
    show_browser=False,
    progress_callback=None,
    allow_ocr_login=False,
    profile_dir=None,
    milestone_callback=None,
    result_callback=None,
    cached_login_only=False,
):
    require_service_owner()
    playwright = browser = context = page = None
    artifact_root = resolve_failure_artifact_root(report_path)
    publication_identity = {}

    def record_milestone(event, **fields):
        if fields.get("feed_id"):
            publication_identity["feed_id"] = fields["feed_id"]
        if milestone_callback:
            milestone_callback(event, **fields)

    try:
        if progress_callback:
            progress_callback("[纷享销客日报] 1/4 正在打开纷享销客并检查登录状态...")
        playwright, browser, context, page = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[纷享销客日报/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
            **({"cached_login_only": True} if cached_login_only else {}),
        )
        if milestone_callback:
            milestone_callback("browser_started")
        publish_status = publish_daily_log(
            page,
            draft,
            progress_callback=progress_callback,
            milestone_callback=record_milestone,
            pre_submit_history_check=True,
        )
        message = (
            f"已检测到 {draft.report_date_iso} 当天日报已发布，无需重复提交。客户：{draft.customer_name}。"
            if publish_status == "already_exists"
            else f"纷享销客日报发布成功。客户：{draft.customer_name}，日期：{draft.report_date_iso}。"
        )
        result = {
            "ok": True,
            "step": "daily_log",
            "status": publish_status,
            "message": message,
            "artifacts": [],
            **publication_identity,
        }
        if result_callback:
            result_callback(result)
        try:
            save_login_state(context, storage_state_path)
        except Exception as exc:
            if progress_callback:
                progress_callback(f"[纷享销客日报] CRM 结果已确认，登录缓存保存失败：{exc}")
        return result
    except CachedLoginExpired:
        raise
    except SubmissionEvidenceError as exc:
        retain_submission_failure_trace(page, artifact_root, f"{draft.report_date_iso.replace('-', '')}_daily_log_evidence", exc)
        raise
    except PublishConfirmationUncertain as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_daily_log",
        )
        raise AutomationFailure(
            str(exc),
            artifacts=artifacts,
            status="uncertain",
        ) from exc
    except PlaywrightTimeoutError as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_daily_log",
        )
        raise AutomationFailure(f"浏览器自动化等待超时：{exc}", artifacts=artifacts) from exc
    except PlaywrightError as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_daily_log",
        )
        raise AutomationFailure(f"浏览器自动化执行失败：{exc}", artifacts=artifacts) from exc
    except Exception as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_daily_log",
        )
        raise AutomationFailure(str(exc), artifacts=artifacts) from exc
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


def run_daily_log_verification(
    draft,
    storage_state_path,
    report_path,
    show_browser=False,
    progress_callback=None,
    allow_ocr_login=False,
    profile_dir=None,
    cached_login_only=False,
):
    playwright = browser = context = page = None
    artifact_root = resolve_failure_artifact_root(report_path)
    try:
        if progress_callback:
            progress_callback("[纷享销客日报复核] 正在打开工作圈并查询当日日报...")
        playwright, browser, context, page = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[纷享销客日报复核/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
            **({"cached_login_only": True} if cached_login_only else {}),
        )
        query_url = open_work_log_history(page)
        history = query_work_log_history(page, query_url, draft.report_date_iso)
        matched_feed = find_daily_log_in_history(history, draft)
        try:
            save_login_state(context, storage_state_path)
        except Exception as exc:
            if progress_callback:
                progress_callback(f"[纷享销客日报复核] 查询已完成，登录缓存保存失败：{exc}")
        return {
            "query_completed": True,
            "found": matched_feed is not None,
            "query_status": "found" if matched_feed else "not_found",
            "feed_id": matched_feed.get("feed_id") if matched_feed else None,
            "creator": matched_feed.get("creator") if matched_feed else "",
            "message": (
                "已在“我发出的日志”历史中确认对应日报。"
                if matched_feed
                else "已按日报日期查询“我发出的日志”，未找到匹配日报。"
            ),
            "artifacts": [],
        }
    except Exception as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_iso.replace('-', '')}_daily_log_verify",
        )
        return {
            "query_completed": False,
            "found": False,
            "query_status": getattr(exc, "query_status", "query_failed"),
            "message": str(exc),
            "artifacts": artifacts,
        }
    finally:
        close_browser_session(browser, context)
        if playwright is not None:
            playwright.stop()


def main(argv=None):
    configure_stdio()
    args = parse_args(argv)
    storage_state_path = Path(os.path.expanduser(args.state_path)).resolve()
    profile_dir = resolve_profile_dir(args.profile_dir)

    if args.reset_login_state:
        removed = reset_login_state(storage_state_path, profile_dir)
        message = "已清理登录态：" + "、".join(removed) if removed else "没有发现需要清理的登录态。"
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "daily_log",
                        "status": "reset_login_state",
                        "message": message,
                        "removed": removed,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(message)
        return 0

    if args.doctor:
        diagnostics = collect_environment_diagnostics(storage_state_path, profile_dir=profile_dir)
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": all(item[1] for item in diagnostics),
                        "step": "daily_log",
                        "status": "doctor",
                        "diagnostics": [
                            {
                                "label": label,
                                "ok": ok,
                                "detail": detail,
                                "hint": hint,
                            }
                            for label, ok, detail, hint in diagnostics
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print("--- 纷享销客日报环境自检 ---")
            for label, ok, detail, hint in diagnostics:
                status = "OK" if ok else "FAIL"
                print(f"[{status}] {label}: {detail}")
                if not ok:
                    print(f"      建议：{hint}")
        return 0

    report_path = resolve_report_path(args.report_path)
    target_section = find_target_report_section(report_path, args.report_date)
    draft = build_daily_log_draft(target_section)

    if not args.json_output:
        print_draft(draft, report_path)

    if args.preview_only:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "daily_log",
                        "status": "preview",
                        "message": draft.work_summary,
                        "artifacts": [],
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    if not should_auto_confirm(args, stdin_isatty=sys.stdin.isatty()):
        confirm = read_brief_input("\n是否继续自动发布纷享销客日报？(y/n): ").strip().lower()
        if confirm not in {"y", "yes"}:
            if args.json_output:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "step": "daily_log",
                            "status": "cancelled",
                            "message": "已取消。",
                            "artifacts": [],
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print("\n已取消。")
            return 0

    progress_callback = lambda message: emit_progress(message, json_output=args.json_output)
    if not args.json_output:
        print("\n[浏览器自动化正在执行，请稍候...]")
    evidence_context: dict[str, Any] = {}
    existing, intent_path = reserve_fxiaoke_submission_intent(
        target_section, "daily_log", profile_dir, report_path=report_path,
        inherited_token=args.submission_intent_token,
        evidence_context=evidence_context,
    )
    if existing is not None:
        result = build_fxiaoke_submission_intent_blocked_result("daily_log", "纷享销客日报", existing)
        print(json.dumps(result, ensure_ascii=False) if args.json_output else result["message"])
        return 1
    checkpoint_fxiaoke_submission(evidence_context, expected_customers=[draft.customer_name])
    milestones = {}

    def milestone_callback(event, **fields):
        milestones[event] = True
        milestones.update(fields)
        checkpoint_fxiaoke_submission(evidence_context, child_index=0, event=event, fields=fields)

    def result_callback(result):
        checkpoint_fxiaoke_submission(evidence_context, child_index=0, result=result)

    try:
        result = run_browser_automation(
            draft, storage_state_path=storage_state_path, report_path=report_path,
            show_browser=args.show_browser, progress_callback=progress_callback,
            allow_ocr_login=args.allow_ocr_login, profile_dir=profile_dir,
            milestone_callback=milestone_callback,
            result_callback=result_callback,
            **({"cached_login_only": True} if args.submission_intent_token else {}),
        )
    except SubmissionEvidenceError:
        raise
    except Exception as exc:
        uncertain = (
            milestones.get("submit_clicked") or milestones.get("crm_id_captured")
            or getattr(exc, "status", "") == "uncertain"
        )
        phase = (
            "crm_id_captured" if milestones.get("crm_id_captured")
            else "submit_clicked" if milestones.get("submit_clicked")
            else "unknown" if uncertain else "before_submit"
        )
        artifacts = list(getattr(exc, "artifacts", []) or [])
        failure = {
            "ok": False, "step": "daily_log", "status": "uncertain" if uncertain else "auth_expired" if isinstance(exc, CachedLoginExpired) else "error",
            "retry_action": "verify_crm" if uncertain else "safe_retry",
            "submission_phase": phase,
            "message": str(exc), "artifacts": artifacts,
            **({"feed_id": milestones["feed_id"]} if milestones.get("feed_id") else {}),
        }
        result_callback(failure)
        finish_fxiaoke_direct_submission(target_section, report_path, "daily_log", failure, intent_path)
        if uncertain:
            if args.json_output:
                print(json.dumps(failure, ensure_ascii=False))
            else:
                print(f"\n程序运行出错：{failure['message']}")
                if artifacts:
                    print("失败留痕：")
                    for artifact in artifacts:
                        print(artifact)
            return 1
        raise
    result_callback(result)
    finish_fxiaoke_direct_submission(target_section, report_path, "daily_log", result, intent_path)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("\n[浏览器自动化结果]")
        print(result["message"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        json_requested = "--json" in sys.argv
        artifacts = list(getattr(exc, "artifacts", []) or [])
        if json_requested:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "step": "daily_log",
                        "status": getattr(exc, "status", "error"),
                        "retry_action": getattr(exc, "retry_action", ""),
                        "message": str(exc),
                        "artifacts": artifacts,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"\n程序运行出错：{exc}")
            if artifacts:
                print("失败留痕：")
                for artifact in artifacts:
                    print(artifact)
        raise SystemExit(1)
