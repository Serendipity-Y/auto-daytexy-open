#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from packages.report_domain.paths import resolve_fxiaoke_trace_root
from integrations.fxiaoke.tenant import require_service_owner, tenant_value
from packages.report_domain.history import _pending_queue_lock
from packages.report_domain.workday import resolve_report_work_type

from daily_report import (
    SubmissionEvidenceError,
    checkpoint_fxiaoke_submission,
    emit_terminal_message,
    build_fxiaoke_submission_intent_blocked_result,
    configure_stdio,
    finish_fxiaoke_direct_submission,
    is_onsite_workday,
    parse_report_fields,
    parse_report_service_entries,
    read_brief_input,
    reserve_fxiaoke_submission_intent,
    resolve_report_path,
    should_auto_confirm,
    split_report_sections,
    write_text_atomically,
)


PROJECT_ROOT = Path(__file__).resolve().parent
PRIVATE_LOGIN_STATE_ROOT = Path("~/.config/auto-daytexy").expanduser()
LEGACY_STORAGE_STATE_PATH = PROJECT_ROOT / ".fxiaoke_storage_state.json"
PRIVATE_STORAGE_STATE_PATH = PRIVATE_LOGIN_STATE_ROOT / "fxiaoke_storage_state.json"
LEGACY_PROFILE_DIR = PROJECT_ROOT / ".fxiaoke_browser_profile"
PRIVATE_PROFILE_DIR = PRIVATE_LOGIN_STATE_ROOT / "fxiaoke_browser_profile"
DEFAULT_CUSTOMER_ALIAS_PATH = PROJECT_ROOT / "daily_report_customer_aliases.json"
FXIAOKE_BASE_URL = "https://www.fxiaoke.com"
FXIAOKE_HOME_URL = "https://www.fxiaoke.com/XV/UI/Home#stream"
FXIAOKE_FIELDSIGN_URL = "https://www.fxiaoke.com/XV/UI/Home#stream/fieldsign"
FXIAOKE_CRM_URL = "https://www.fxiaoke.com/XV/UI/Home#crm/index"
FXIAOKE_TECHNICAL_SERVICE_URL = "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/CasesObj"
FXIAOKE_LOGIN_URL = "https://www.fxiaoke.com/proj/page/login?returnUrl=https%3A%2F%2Fwww.fxiaoke.com%2FXV%2FUI%2FHome#stream"
ONSITE_RECORD_KIND = "onsite"
CHROME_EXECUTABLE = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_STORAGE_STATE_PATH = (
    PRIVATE_STORAGE_STATE_PATH
    if PRIVATE_STORAGE_STATE_PATH.exists()
    else LEGACY_STORAGE_STATE_PATH
)
DEFAULT_PROFILE_DIR = PRIVATE_PROFILE_DIR if PRIVATE_PROFILE_DIR.exists() else LEGACY_PROFILE_DIR
DEFAULT_TIMEOUT_MS = 120_000
CHECKIN_CARD_TIMEOUT_MS = 30_000
CHECKIN_CARD_FALLBACK_TIMEOUT_MS = 10_000
CHECKIN_CARD_REFRESH_RETRIES = 1
CHECKIN_CARD_REFRESH_SETTLE_MS = 1_000
CHECKIN_WORKORDER_CLICK_TIMEOUT_MS = 5_000
CHECKIN_WORKORDER_DETAIL_TIMEOUT_MS = 8_000
CHECKIN_WORKORDER_RELOCATE_TIMEOUT_MS = CHECKIN_CARD_TIMEOUT_MS
CHECKIN_WORKORDER_RELOCATE_POLL_MS = 250
SERVICE_RECORD_TAB_TIMEOUT_MS = 10_000
FAILURE_ARTIFACT_DIR_NAME = "fxiaoke_failure_artifacts"
DATE_LINE_PATTERN = re.compile(r"^(\d{4}\.\d{2}\.\d{2})(?:[（(].*[）)])?$")
# 2026-08: fxiaoke redesigned work-circle; check-in cards now live under
# #stream/fieldsign with a feed-list layout instead of the old right sidebar.
#
# Keep this scoped to one card row.  A broad ``[class*="xxvui"]`` selector can
# match list containers and ancestors that contain several JSFW links.  Parsing
# one of those containers and later clicking by its index selects different DOM
# elements, which is how a completed daily report could leave its service record
# stuck in the pending queue.
CHECKIN_CARD_SELECTOR = "div.xxvui-fl-feed-list > div.xxvui-fl-layout"
# The fallback accepts a parent-layout variation, but still returns individual
# card rows that own a technical-service work-order link.
CHECKIN_CARD_FALLBACK_SELECTOR = (
    'div.xxvui-fl-layout:has(a.crm-object-atag[data-type="myobject"])'
)
CHECKIN_WORKORDER_SELECTOR = 'a.crm-object-atag[data-type="myobject"]'
WORKORDER_DETAIL_DIALOG_SELECTOR = "div.detail-dialog-v3"
WORKORDER_DETAIL_CLOSE_SELECTOR = (
    "button.el-dialog__headerbtn, i.el-dialog__close, "
    "button[aria-label='Close'], button[aria-label='关闭']"
)
SERVICE_RECORD_TAB_SELECTOR = (
    "li.el-menu-item[data-autotest*='related_list']:visible, "
    'div.el-tabs__item[role="tab"][title="服务记录"]:visible, '
    '[role="tab"][id*="related_list"][title="服务记录"]:visible'
)
SERVICE_RECORD_NAME_CELL_SELECTOR = "div.j-td[data-fieldname=\"name\"][title^=\"FWJL-\"]"
WORKORDER_NAME_CELL_SELECTOR = "div.j-td[data-fieldname=\"name\"][title^=\"JSFW\"]"
SERVICE_RECORD_ADD_CONTAINER_SELECTOR = "div.btn-wrap-right"
SERVICE_RECORD_ADD_SELECTOR = "i.fx-icon-add-2.icon--active"
SERVICE_RECORD_SUBMIT_SELECTOR = "span.crm-btn[data-action='submit']"
SERVICE_START_TIME_SELECTOR = 'input[placeholder="点击选择日期时间"]'
SERVICE_CONTENT_TEXTAREA_SELECTOR = "textarea.el-textarea__inner.xx-ipt"
WORK_HOUR_INPUT_SELECTOR = "input.j-f-ipt.xx-ipt"
CRM_MODULE_ICON_SELECTOR = "i.fx-icon-f-obj-info"
CRM_MENU_TEXT_SELECTOR = "span.text"
TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR = "input.dt-ipt[placeholder='搜索']"
TECHNICAL_SERVICE_FULL_FIELD_SELECTOR = "input.j-select-input.crm-ui-title[data-title='全字段']"
TECHNICAL_SERVICE_FIELD_SELECTOR = "input.j-select-input.crm-ui-title"
TECHNICAL_SERVICE_NORMAL_SEARCH_MODE_LABEL = "普通"
LOGIN_TAB_TEXT = "账号登录"
LOGIN_PHONE_SELECTOR = "input.fssdk-phone-mobile-value"
LOGIN_PASSWORD_SELECTOR = "input.fssdk-phone-password-value"
LOGIN_CAPTCHA_INPUT_SELECTOR = "input.fssdk-phone-imgcode-value"
LOGIN_CAPTCHA_IMAGE_SELECTOR = "img.fssdk-phone-captcha"
LOGIN_BUTTON_SELECTOR = "div.fssdk-phone-login"
LOGIN_AGREE_BUTTON_TEXT = "同意并登录"
FXIAOKE_CREDENTIALS_PATH_ENV = "FXIAOKE_CREDENTIALS_PATH"
FXIAOKE_PHONE_ENV_NAMES = ("FXIAOKE_PHONE", "FXIAOKE_USERNAME", "FXIAOKE_ACCOUNT")
FXIAOKE_PASSWORD_ENV = "FXIAOKE_PASSWORD"
DEFAULT_CREDENTIALS_PATH = Path("~/.config/auto-daytexy/fxiaoke_credentials.json").expanduser()
FAST_GOTO_SETTLE_MS = 350
FAST_LOADSTATE_TIMEOUT_MS = 8_000
POST_LOGIN_SETTLE_TIMEOUT_MS = 8_000
MANUAL_LOGIN_TIMEOUT_MS = 180_000
FAST_LOCATOR_READ_TIMEOUT_MS = 1_000
SERVICE_RECORD_FORM_TIMEOUT_SECONDS = 5
SERVICE_RECORD_SUBMIT_TIMEOUT_SECONDS = 45
SERVICE_RECORD_SUBMIT_POLL_MS = 300
SERVICE_RECORD_VERIFY_LIST_TIMEOUT_MS = 30_000
CUSTOMER_UNIT_PATTERN = re.compile(
    r"客户单位\s*[:：]?\s*(.*?)\s*"
    r"(?:创建人|工单负责人|服务方式|关联项目|联系人名称|联系人电话|服务需求|$)",
    re.S,
)
CUSTOMER_UNIT_FIELD_VALUE_XPATH = (
    "xpath=.//*[contains(@class, 'faci-field-display')]["
    ".//*[contains(@class, 'faci-field-display_label') and normalize-space()='客户单位']"
    "]//*[contains(@class, 'faci-field-display_value')]"
)
FWJL_ID_PATTERN = re.compile(r"(FWJL-\d{8}-\d+)")
JSFW_ID_PATTERN = re.compile(r"(JSFW\d{8}-\d+)")
WQJL_ID_PATTERN = re.compile(r"(WQJL(\d{8})\d*)")
SIGN_TIME_VALUE_PATTERN = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}|\d{1,2}月\d{1,2}日?\s*\d{1,2}:\d{2})"
SIGN_IN_TIME_PATTERN = re.compile(rf"签到\s*{SIGN_TIME_VALUE_PATTERN}")
SIGN_OUT_TIME_PATTERN = re.compile(rf"签退\s*{SIGN_TIME_VALUE_PATTERN}")
SIGN_IN_BLOCK_PATTERN = re.compile(
    rf"签到\s*{SIGN_TIME_VALUE_PATTERN}\s*(.*?)\s*(?:查看设备|签退)",
    re.S,
)
SIGN_OUT_BLOCK_PATTERN = re.compile(
    rf"签退\s*{SIGN_TIME_VALUE_PATTERN}\s*(.*?)\s*(?:查看更多|CRM\(\d+\)|回复|转发|$)",
    re.S,
)
FEED_RELATIVE_TIME_PATTERN = re.compile(r"(今天|昨天)\s*(\d{1,2}:\d{2})")
PROFILE_LOCKS: dict[int, "BrowserProfileLock"] = {}


def emit_progress(message, json_output=False):
    stream = sys.stderr if json_output else sys.stdout
    emit_terminal_message(message, file=stream)


@dataclass
class ServiceRecordDraft:
    report_date_display: str
    report_date_iso: str
    report_date_compact: str
    customer_name: str
    process_text: str
    next_plan: str
    search_query: str
    duplicate_prefix: str
    record_kind: str = ONSITE_RECORD_KIND
    service_method: str = ""
    service_day_value: str = ""
    workhour_value: str = ""
    service_start_time: str = ""
    service_end_time: str = ""
    work_type: str = ""
    routing_target: Optional[dict] = None
    source_checkin_workorder_id: str = ""
    routing_source: Optional[dict] = None

    @property
    def service_content(self):
        return "\n".join(
            [
                "1）服务结果/进度说明:",
                f"2）服务过程中遇到的问题及解决情况:{self.process_text}",
                f"3）下一步行动计划:{self.next_plan}",
                "4）其他有价值的信息反馈:",
            ]
        )


@dataclass
class CheckinCandidate:
    card_index: int
    jsfw: str
    sign_in_time: str
    sign_out_time: str
    sign_in_address: str
    sign_out_address: str
    card_text: str


@dataclass
class AutomationResult:
    workorder_id: str
    service_record_id: Optional[str]
    status: str
    message: str
    artifacts: list[str] = field(default_factory=list)
    crm_customer: str = ""
    work_type: str = ""
    routing_policy: str = ""
    routing_target: Optional[dict] = None
    source_checkin_workorder_id: str = ""
    routing_source: Optional[dict] = None


@dataclass
class ExistingServiceRecordSummary:
    record_id: str
    record_data_id: str
    detail_url: str
    service_start_time: str


@dataclass
class ExistingServiceRecordDetail:
    record_id: str
    owner_name: str
    service_start_time: str
    customer_name: str = ""
    service_method: str = ""


@dataclass
class FxiaokeCredentials:
    phone_number: str
    password: str
    source: str

    @property
    def is_complete(self):
        return bool(self.phone_number and self.password)


class AutomationFailure(RuntimeError):
    def __init__(
        self,
        message,
        artifacts=None,
        status="error",
        workorder_id="",
        service_record_id=None,
    ):
        super().__init__(message)
        self.artifacts = list(artifacts or [])
        self.status = status
        self.workorder_id = workorder_id
        self.service_record_id = service_record_id


class CachedLoginExpired(AutomationFailure):
    def __init__(self):
        super().__init__("CRM 登录已失效，已停止本次提交；请先通过登录维护入口恢复登录。", status="auth_expired")


class AuthenticationUncertain(AutomationFailure):
    def __init__(self):
        super().__init__("CRM认证页面尚未就绪或无法确认当前登录状态，已停止；请先检查登录与网络。", status="auth_unknown")
        self.failure_scope = "session"
        self.retry_action = "restore_login"


class ServiceRecordConfirmationUncertain(RuntimeError):
    def __init__(self, message, workorder_id="", service_record_id=""):
        super().__init__(message)
        self.status = "uncertain"
        self.workorder_id = str(workorder_id or "").strip()
        self.service_record_id = str(service_record_id or "").strip()


class ManualLoginRequired(RuntimeError):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="根据固定日报内容自动填写纷享销客服务记录。"
    )
    parser.add_argument(
        "--date",
        dest="report_date",
        help="指定日报日期，支持 YYYY-MM-DD 或 YYYY.MM.DD。",
    )
    parser.add_argument("--submission-intent-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--customer", default=None, help="仅处理与此名称精确匹配的一个日报服务客户。")
    parser.add_argument(
        "--path",
        dest="report_path",
        help="覆盖默认日报路径。",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成服务记录草稿，不启动浏览器自动化。",
    )
    parser.add_argument(
        "--main-only",
        action="store_true",
        help="兼容参数；当前服务记录功能只处理外勤签到关联的主服务。",
    )
    parser.add_argument(
        "--verify-workorder",
        help="只读核验指定 JSFW 工单的同日服务记录，不提交 CRM 数据。",
    )
    parser.add_argument(
        "--verify-service-record",
        help="配合 --verify-workorder 核验指定 FWJL 服务记录编号。",
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
        help="使用指定 Playwright 持久化浏览器 Profile 目录保存完整登录状态。建议路径位于项目目录内。",
    )
    parser.add_argument(
        "--reset-login-state",
        action="store_true",
        help="清理登录态缓存与指定 Profile 后退出，用于登录状态损坏时重建。",
    )
    parser.add_argument("--work-type", choices=("实施", "售后", "POC"), default="", help="明确日报工作类型；仅售后可启用实施签到转售后工单规则。")
    parser.add_argument("--routing-pending-file", default="", help=argparse.SUPPRESS)
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
    args = parser.parse_args(argv)
    if args.verify_service_record and not args.verify_workorder:
        parser.error("--verify-service-record 需要与 --verify-workorder 一起使用。")
    return args


def normalize_report_date_value(value):
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("日报日期不能为空。")

    for fmt in ("%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    match = DATE_LINE_PATTERN.match(cleaned)
    if match:
        return datetime.strptime(match.group(1), "%Y.%m.%d")

    raise ValueError("无法识别日报日期，请使用 YYYY-MM-DD 或 YYYY.MM.DD。")


def extract_section_date(section):
    first_line = section.splitlines()[0].strip()
    match = DATE_LINE_PATTERN.match(first_line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y.%m.%d")


def load_report_sections(report_path):
    if not report_path.exists():
        raise RuntimeError(f"未找到日报文件：{report_path}")

    content = report_path.read_text(encoding="utf-8")
    sections = split_report_sections(content)
    if not sections:
        raise RuntimeError("日报文件中没有可用的日报内容。")
    return sections


def find_target_report_section(report_path, report_date=None):
    sections = load_report_sections(report_path)
    if report_date:
        target_date = normalize_report_date_value(report_date).date()
        for section in sections:
            section_date = extract_section_date(section)
            if section_date and section_date.date() == target_date:
                return section
        raise RuntimeError(f"未找到 {target_date.isoformat()} 对应的日报内容。")
    return sections[-1]


CUSTOMER_DEPARTMENT_SUFFIXES = (
    "现代教育技术中心",
    "教育技术中心",
    "信息化中心",
    "信息中心",
    "现教中心",
    "实训中心",
    "资产处",
    "教务处",
    "信息化部",
    "信息化办",
    "信息化办公室",
    "网络中心",
    "科研处",
)

CUSTOMER_NAME_REPLACEMENTS = (
    ("高等专科学校", "专科学校"),
    ("中等专业职业学校", "中等专业学校"),
    ("中等专科学校", "中等专业学校"),
    ("中等专业技术学校", "中等专业学校"),
    ("中等职业技术学校", "中等职业学校"),
    ("职业中等专业学校", "中等职业学校"),
    ("中专", "中等专业学校"),
)

SECONDARY_VOCATIONAL_SCHOOL_MARKERS = (
    "中等职业学校",
    "中等专业学校",
)

CUSTOMER_INSTITUTION_SUFFIXES = (
    "健康职业技术学院",
    "职业技术学院",
    "科学技术学院",
    "中等专业学校",
    "中等职业学校",
    "技术学院",
    "职业学院",
    "职业学校",
    "职院",
    "师范学院",
    "警察学院",
    "科技学院",
    "学院",
    "大学",
    "学校",
    "监狱",
    "一中",
    "中专",
    "中学",
    "小学",
)

CUSTOMER_ORG_BEFORE_CAMPUS_PATTERN = re.compile(
    r"^(.+(?:职业技术学院|健康职业技术学院|科学技术学院|科技学院|技术学院|"
    r"警察学院|师范学院|职业学院|职业学校|中等专业学校|中等职业学校|职院|"
    r"学院|大学|学校|监狱|一中|中专|中学|小学))"
    r"[\u4e00-\u9fffA-Za-z0-9]{2,}校区$"
)

GENERIC_CUSTOMER_ALIASES = {
    "健康职业技术学院",
    "职业技术学院",
    "科学技术学院",
    "中等专业学校",
    "中等职业学校",
    "技术学院",
    "健康职业学院",
    "职业学院",
    "职业学校",
    "职院",
    "师范学院",
    "警察学院",
    "科技学院",
    "学院",
    "大学",
    "学校",
    "中专",
    "一中",
    "中学",
    "小学",
}

CUSTOMER_NAME_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,}"
    r"(?:职业技术学院|健康职业技术学院|健康职业学院|技术学院|警察学院|"
    r"师范学院|职业学院|职业学校|科学技术学院|科技学院|"
    r"中等专业技术学校|中等职业技术学校|职业中等专业学校|"
    r"中等专业学校|中等职业学校|职院|学院|大学|学校|监狱|一中|中专|中学|小学)"
)

REMOTE_SUPPORT_ACTION_PREFIX_PATTERN = re.compile(
    # 动词可连续出现（“远程沟通指导示例学院…”），必须一次剥净，否则动词会被
    # 并入客户名，导致 CRM 用“指导示例学院”检索不到目标单位。
    # 动词组仍保持“零个也可”，否则“远程配合集成商…更改某学院”只会剥掉
    # 动词而留下“远程”，协调前缀正则就再也匹配不到“配合集成商”。
    r"^(?:远程|线上|电话)?(?:指导|解决|处理|排查|协助|沟通|支持|确认|跟进|帮助)*"
)
REMOTE_SUPPORT_LEADING_FILLER_PATTERN = re.compile(
    # 口语常把时间/完成态写在客户名前面（“今日已完成示例中专维保沟通”）。
    # 若不先剥掉，贪婪的客户名正则会把它并成“今日已完成示例中专”，CRM
    # 用这个名字检索必然 0 条。这里只放不会独立构成单位名的前缀词。
    r"^(?:(?:今日|今天|本日|昨日|昨天|今早|今晨|上午|下午|晚上|目前|现已|"
    r"已经|已|完成|处理完成|关于|针对|通过|经过|根据|同时|另外))+"
)
REMOTE_SUPPORT_COORDINATION_PREFIX_PATTERN = re.compile(
    r"^(?:配合|协助)?(?:集成商|厂家|厂商)"
    r"(?:进行|协助|配合|完成|更改|修改|配置|处理|解决|排查|测试|调整)*"
)
REMOTE_COORDINATION_ONLY_PATTERN = re.compile(r"(?:沟通|确认|协调|对接|跟进|联系)")
REMOTE_TECHNICAL_ACTION_PATTERN = re.compile(
    r"(?:指导|解决|处理|排查|协助|支持|帮助|修复|恢复|优化|配置|部署|安装|调试|测试|切换|新增|讲解|培训)"
)


def normalize_customer_match_text(value):
    cleaned = normalize_inline_text(value)
    cleaned = re.sub(r"[\s:：,，;；.。()（）\-_—·]+", "", cleaned)
    for source, target in CUSTOMER_NAME_REPLACEMENTS:
        cleaned = cleaned.replace(source, target)
    for suffix in CUSTOMER_DEPARTMENT_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix) + 2:
            cleaned = cleaned[: -len(suffix)]
            break
    campus_match = CUSTOMER_ORG_BEFORE_CAMPUS_PATTERN.match(cleaned)
    if campus_match:
        cleaned = campus_match.group(1)
    return cleaned


def normalize_customer_core_text(value):
    cleaned = normalize_customer_match_text(value)
    cleaned = re.sub(r"[省市县区]", "", cleaned)
    for suffix in CUSTOMER_INSTITUTION_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix) + 2:
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned


def secondary_vocational_location_core(value):
    cleaned = normalize_customer_match_text(value)
    marker_index = -1
    for marker in SECONDARY_VOCATIONAL_SCHOOL_MARKERS:
        marker_index = cleaned.find(marker)
        if marker_index >= 0:
            break
    if marker_index < 0:
        return ""

    location_core = re.sub(r"[省市县区]", "", cleaned[:marker_index])
    location_core = location_core.removeprefix("中国")
    return location_core if len(location_core) >= 2 else ""


def secondary_vocational_school_matches(customer_name, workorder_customer):
    customer_location = secondary_vocational_location_core(customer_name)
    workorder_location = secondary_vocational_location_core(workorder_customer)
    if not customer_location or not workorder_location:
        return False
    return customer_location in workorder_location or workorder_location in customer_location


def extract_university_subcollege_parts(value):
    cleaned = normalize_customer_match_text(value)
    match = re.match(r"^(.+大学)(.+学院)$", cleaned)
    if not match:
        return "", ""
    university, college = match.groups()
    return university, college


def university_subcollege_conflicts(customer_name, workorder_customer):
    customer_university, customer_college = extract_university_subcollege_parts(customer_name)
    workorder_university, workorder_college = extract_university_subcollege_parts(workorder_customer)
    if not all((customer_university, customer_college, workorder_university, workorder_college)):
        return False
    if customer_university != workorder_university:
        return True
    return not (customer_college in workorder_college or workorder_college in customer_college)


def build_customer_search_keyword(customer_name):
    primary_name = resolve_customer_primary_name(customer_name)
    cleaned = normalize_customer_match_text(primary_name)
    core = normalize_customer_core_text(primary_name)
    if len(core) >= 5:
        return core
    return cleaned or normalize_inline_text(customer_name)


def longest_common_substring_length(left, right):
    if not left or not right:
        return 0

    previous = [0] * (len(right) + 1)
    best = 0
    for left_char in left:
        current = [0] * (len(right) + 1)
        for index, right_char in enumerate(right, start=1):
            if left_char == right_char:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def customer_core_matches(customer_name, workorder_customer):
    if university_subcollege_conflicts(customer_name, workorder_customer):
        return False

    if secondary_vocational_school_matches(customer_name, workorder_customer):
        return True

    customer_core = normalize_customer_core_text(customer_name)
    workorder_core = normalize_customer_core_text(workorder_customer)
    if len(customer_core) < 6 or len(workorder_core) < 6:
        return False
    if customer_core in workorder_core or workorder_core in customer_core:
        return True

    common_length = longest_common_substring_length(customer_core, workorder_core)
    shorter_length = min(len(customer_core), len(workorder_core))
    return common_length >= 6 and common_length / shorter_length >= 0.8


def build_customer_core_aliases(customer_name):
    cleaned = normalize_customer_match_text(customer_name)
    aliases = set()
    match = re.match(r"^(.+大学)(.+学院)$", cleaned)
    if match:
        university, college = match.groups()
        for keyword in ("制造", "信息", "软件", "管理", "教育", "技术", "工程", "建筑", "艺术", "医学"):
            if keyword in college:
                aliases.add(f"{university}{college[college.index(keyword):]}")
    return aliases


def build_customer_aliases(customer_name):
    raw_cleaned = normalize_inline_text(customer_name)
    cleaned = normalize_customer_match_text(customer_name)
    aliases = {raw_cleaned, cleaned, *build_customer_core_aliases(customer_name)}
    if cleaned.startswith("中国") and len(cleaned) > len("中国") + 3:
        aliases.add(cleaned.removeprefix("中国"))

    for suffix in (
        "职业技术学院",
        "技术学院",
        "健康职业学院",
        "警察学院",
        "师范学院",
        "职业学院",
        "职业学校",
        "职院",
        "学院",
        "大学",
        "学校",
        "监狱",
        "一中",
        "中专",
        "中学",
        "小学",
    ):
        index = cleaned.find(suffix)
        if index >= 0:
            aliases.add(cleaned[index:])
    return sorted(
        alias
        for alias in aliases
        if len(alias) >= 4 and alias not in GENERIC_CUSTOMER_ALIASES
        and not alias.startswith(("大学", "学院", "学校"))
    )


def normalize_inline_text(value):
    return re.sub(r"\s+", "", value.strip())


def normalize_customer_alias_key(customer_name):
    return normalize_customer_core_text(customer_name) or normalize_customer_match_text(customer_name)


def unique_customer_alias_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("客户别名文件包含重复的JSON字段")
        result[key] = value
    return result


def load_customer_alias_memory(alias_path=None, *, strict=False):
    path = alias_path or DEFAULT_CUSTOMER_ALIAS_PATH
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_customer_alias_object)
        if not isinstance(data, dict):
            raise ValueError("期望 JSON 对象")
        mappings = data.get("customer_aliases", data)
        if not isinstance(mappings, dict):
            raise ValueError("customer_aliases 必须是对象")
    except Exception as exc:
        if strict:
            raise RuntimeError("客户别名文件异常，已停止更新并保留原文件。") from exc
        return {}

    normalized = {}
    for key, value in mappings.items():
        if isinstance(value, dict):
            alias = str(value.get("alias", key) or "").strip()
            primary_customer = str(value.get("primary_customer", "") or "").strip()
            explicitly_confirmed = value.get("explicitly_confirmed") is True
        else:
            alias = str(key or "").strip()
            primary_customer = str(value or "").strip()
            explicitly_confirmed = False
        if not alias or not primary_customer:
            continue
        if (
            university_subcollege_conflicts(alias, primary_customer)
            and not explicitly_confirmed
        ):
            continue
        item = {
            "alias": alias,
            "primary_customer": primary_customer,
            "explicitly_confirmed": explicitly_confirmed,
        }
        normalized_key = normalize_customer_alias_key(alias)
        if normalized_key in normalized:
            if strict:
                raise RuntimeError("客户别名名称归一后发生冲突，已停止更新并保留原文件。")
            return {}
        normalized[normalized_key] = item
    return normalized


def save_customer_alias_memory(mappings, alias_path=None):
    path = alias_path or DEFAULT_CUSTOMER_ALIAS_PATH
    payload = {
        "customer_aliases": {
            key: {
                "alias": value["alias"],
                "primary_customer": value["primary_customer"],
                **(
                    {"explicitly_confirmed": True}
                    if value.get("explicitly_confirmed")
                    else {}
                ),
            }
            for key, value in sorted(mappings.items())
        }
    }
    write_text_atomically(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def remember_customer_alias_mapping(
    alias,
    primary_customer,
    alias_path=None,
    *,
    explicitly_confirmed=False,
):
    alias = (alias or "").strip()
    primary_customer = (primary_customer or "").strip()
    if not alias or not primary_customer or alias == primary_customer:
        return
    if (
        university_subcollege_conflicts(alias, primary_customer)
        and not explicitly_confirmed
    ):
        return
    with _pending_queue_lock(alias_path or DEFAULT_CUSTOMER_ALIAS_PATH):
        mappings = load_customer_alias_memory(alias_path=alias_path, strict=True)
        mappings[normalize_customer_alias_key(alias)] = {
            "alias": alias,
            "primary_customer": primary_customer,
            "explicitly_confirmed": bool(explicitly_confirmed),
        }
        save_customer_alias_memory(mappings, alias_path=alias_path)


def resolve_customer_primary_name(customer_name, alias_path=None):
    key = normalize_customer_alias_key(customer_name)
    remembered = load_customer_alias_memory(alias_path=alias_path).get(key)
    if remembered and normalize_inline_text(customer_name).casefold() == normalize_inline_text(remembered["alias"]).casefold():
        return remembered["primary_customer"]
    return customer_name


def extract_plan_customer_names(plan_text):
    if not plan_text:
        return []

    return list(dict.fromkeys(match.group(0) for match in CUSTOMER_NAME_PATTERN.finditer(plan_text)))


def is_same_customer_plan(customer_name, tomorrow_plan):
    normalized_customer = normalize_inline_text(customer_name)
    normalized_plan = normalize_inline_text(tomorrow_plan)
    if not normalized_customer or not normalized_plan:
        return False
    if normalized_customer not in normalized_plan:
        return False

    remaining_plan = tomorrow_plan
    while customer_name in remaining_plan:
        remaining_plan = remaining_plan.replace(customer_name, " ", 1)

    return not extract_plan_customer_names(remaining_plan)


def build_next_plan(customer_name, tomorrow_plan):
    cleaned_plan = tomorrow_plan.strip()
    if cleaned_plan and is_same_customer_plan(customer_name, cleaned_plan):
        return cleaned_plan
    return "客情维护"


def ensure_terminal_punctuation(value):
    cleaned = value.strip()
    if not cleaned:
        return cleaned
    if cleaned.endswith(("；", ";", "。")):
        return cleaned
    return f"{cleaned}；"


def format_report_date_for_display(report_date):
    return report_date.strftime("%Y.%m.%d")


def build_search_query(report_date_iso, customer_name):
    return f"{report_date_iso} 签到 {build_customer_search_keyword(customer_name)}"


def strip_remote_support_action_prefix(value):
    cleaned = normalize_spacing(value).strip("；;。")
    # 时间/完成态与动作词可以交替嵌套（“今日已完成协助…”），单轮替换会漏掉
    # 后面的动词，所以按“填充词→动作词→协调词”循环剥到不再变化为止。
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = REMOTE_SUPPORT_LEADING_FILLER_PATTERN.sub("", cleaned, count=1)
        cleaned = REMOTE_SUPPORT_ACTION_PREFIX_PATTERN.sub("", cleaned, count=1)
        cleaned = REMOTE_SUPPORT_COORDINATION_PREFIX_PATTERN.sub("", cleaned, count=1)
    return cleaned.strip()


def should_skip_remote_support_item(remote_item):
    cleaned = normalize_spacing(remote_item).strip("；;。")
    if not cleaned:
        return True
    return bool(
        REMOTE_COORDINATION_ONLY_PATTERN.search(cleaned)
        and not REMOTE_TECHNICAL_ACTION_PATTERN.search(cleaned)
    )


def extract_remote_support_customer_name(remote_item, default_customer):
    cleaned = strip_remote_support_action_prefix(remote_item)
    match = CUSTOMER_NAME_PATTERN.search(cleaned)
    if match:
        return match.group(0).strip()
    return default_customer.strip()


def build_service_record_draft_from_fields(report_date, fields):
    customer_name = fields.get("服务客户", "").strip()
    process_text = fields.get("处理情况", "").strip()
    tomorrow_plan = fields.get("明日安排", "").strip()

    if not customer_name:
        raise RuntimeError("日报缺少“服务客户”，无法生成服务记录。")
    if not process_text:
        raise RuntimeError("日报缺少“处理情况”，无法生成服务记录。")

    process_text = ensure_terminal_punctuation(process_text)
    next_plan = build_next_plan(customer_name, tomorrow_plan)
    report_date_iso = report_date.strftime("%Y-%m-%d")
    report_date_compact = report_date.strftime("%Y%m%d")
    return ServiceRecordDraft(
        report_date_display=format_report_date_for_display(report_date),
        report_date_iso=report_date_iso,
        report_date_compact=report_date_compact,
        customer_name=customer_name,
        process_text=process_text,
        next_plan=next_plan,
        search_query=build_search_query(report_date_iso, customer_name),
        duplicate_prefix=f"FWJL-{report_date_compact}-",
        record_kind=ONSITE_RECORD_KIND,
        service_end_time="",
    )


def build_service_record_drafts(report_section, main_only=False, *, work_type="", report_path=None):
    report_date = extract_section_date(report_section)
    if not report_date:
        raise RuntimeError("目标日报缺少可识别的日期标题。")

    report_fields = parse_report_fields(report_section)
    service_entries = parse_report_service_entries(report_section)
    if service_entries:
        tomorrow_plan = report_fields.get("明日安排", "").strip()
        for entry in service_entries:
            entry.setdefault("明日安排", tomorrow_plan)
        service_drafts = [
            build_service_record_draft_from_fields(
                report_date,
                entry,
            )
            for entry in service_entries
            if entry.get("服务客户", "").strip() or entry.get("处理情况", "").strip()
        ]
        return [replace(draft, work_type=resolve_report_work_type(
            report_section, report_path, {"work_type": work_type} if work_type else None,
            customer_name=draft.customer_name,
        )) for draft in service_drafts]

    primary_draft = build_service_record_draft_from_fields(
        report_date,
        report_fields,
    )
    return [replace(primary_draft, work_type=resolve_report_work_type(
        report_section, report_path, {"work_type": work_type} if work_type else None,
        customer_name=primary_draft.customer_name,
    ))]


def build_service_record_draft(report_section, *, work_type="", report_path=None):
    return build_service_record_drafts(report_section, work_type=work_type, report_path=report_path)[0]


def serialize_draft(draft):
    payload = {
        "report_date": draft.report_date_iso,
        "customer_name": draft.customer_name,
        "record_kind": draft.record_kind,
        "service_method": draft.service_method,
        "service_day_value": draft.service_day_value,
        "workhour_value": draft.workhour_value,
        "service_start_time": draft.service_start_time,
        "service_end_time": draft.service_end_time,
        "search_query": draft.search_query,
        "process_text": draft.process_text,
    }
    for key in ("work_type", "routing_target", "source_checkin_workorder_id", "routing_source"):
        value = getattr(draft, key, None)
        if value:
            payload[key] = deepcopy(value)
    return payload


def serialize_result(draft, result):
    payload = {
        **serialize_draft(draft),
        "ok": result.status in {"submitted", "already_exists"},
        "step": "service_record",
        "service_customer": draft.customer_name,
        "crm_customer": result.crm_customer,
        "content_digest": hashlib.sha256(draft.service_content.strip().encode("utf-8")).hexdigest(),
        "workorder_id": result.workorder_id,
        "service_record_id": result.service_record_id,
        "status": result.status,
        "message": result.message,
        "artifacts": result.artifacts,
    }
    for key in ("work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source"):
        value = getattr(result, key, None)
        if value:
            payload[key] = deepcopy(value)
    return payload


def parse_compact_date(value):
    if not value or not re.fullmatch(r"\d{8}", value):
        return ""
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def normalize_sign_time(value, report_date_iso=None):
    value = normalize_spacing(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}", value):
        date_value, time_value = value.split()
        hour, minute = time_value.split(":")
        return f"{date_value} {int(hour):02d}:{minute}"

    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日?\s*(\d{1,2}:\d{2})", value)
    if match and report_date_iso:
        hour, minute = match.group(3).split(":")
        return f"{report_date_iso[:4]}-{int(match.group(1)):02d}-{int(match.group(2)):02d} {int(hour):02d}:{minute}"

    return value


def derive_checkin_time_from_card(card_text, report_date_iso=None):
    sign_in_match = SIGN_IN_TIME_PATTERN.search(card_text)
    if sign_in_match:
        return normalize_sign_time(sign_in_match.group(1), report_date_iso)

    relative_match = FEED_RELATIVE_TIME_PATTERN.search(card_text)
    relative_time = ""
    if relative_match:
        hour, minute = relative_match.group(2).split(":")
        relative_time = f"{int(hour):02d}:{minute}"

    wqjl_match = WQJL_ID_PATTERN.search(card_text)
    if wqjl_match:
        checkin_date = parse_compact_date(wqjl_match.group(2))
        return f"{checkin_date} {relative_time or '09:00'}"

    jsfw_match = JSFW_ID_PATTERN.search(card_text)
    if jsfw_match and report_date_iso:
        workorder_date = parse_compact_date(jsfw_match.group(1)[4:12])
        if workorder_date == report_date_iso:
            return f"{workorder_date} {relative_time or '09:00'}"

    if relative_time and report_date_iso:
        return f"{report_date_iso} {relative_time}"

    return ""


def parse_checkin_candidate_from_text(card_text, card_index, report_date_iso=None):
    if "技术外勤" not in card_text or ("签到" not in card_text and "外勤记录" not in card_text):
        return None

    jsfw_match = JSFW_ID_PATTERN.search(card_text)
    sign_in_time = derive_checkin_time_from_card(card_text, report_date_iso)
    if not jsfw_match or not sign_in_time:
        return None

    sign_out_match = SIGN_OUT_TIME_PATTERN.search(card_text)
    sign_in_block = SIGN_IN_BLOCK_PATTERN.search(card_text)
    sign_out_block = SIGN_OUT_BLOCK_PATTERN.search(card_text)
    sign_in_address = normalize_spacing(sign_in_block.group(2)) if sign_in_block else ""
    sign_out_address = normalize_spacing(sign_out_block.group(2)) if sign_out_block else ""

    return CheckinCandidate(
        card_index=card_index,
        jsfw=jsfw_match.group(1),
        sign_in_time=sign_in_time,
        sign_out_time=normalize_sign_time(sign_out_match.group(1), report_date_iso) if sign_out_match else "",
        sign_in_address=sign_in_address,
        sign_out_address=sign_out_address,
        card_text=card_text,
    )


def normalize_spacing(value):
    return re.sub(r"\s+", " ", value or "").strip()


def candidate_mentions_customer(candidate, customer_name):
    haystack = normalize_customer_match_text(
        f"{candidate.card_text}{candidate.sign_in_address}{candidate.sign_out_address}"
    )
    candidate_names = [customer_name]
    primary_name = resolve_customer_primary_name(customer_name)
    if primary_name and primary_name not in candidate_names:
        candidate_names.append(primary_name)
    return any(
        alias in haystack
        for candidate_name in candidate_names
        for alias in build_customer_aliases(candidate_name)
    )


def candidate_workorder_date_matches(candidate, report_date_compact):
    match = re.match(r"JSFW(\d{8})-", candidate.jsfw or "")
    return bool(match and match.group(1) == report_date_compact)


def order_checkin_candidates_for_draft(candidates, draft):
    preferred = [c for c in candidates if candidate_mentions_customer(c, draft.customer_name)]
    same_day_workorders = [
        c
        for c in candidates
        if c not in preferred and candidate_workorder_date_matches(c, draft.report_date_compact)
    ]
    remaining = [c for c in candidates if c not in preferred and c not in same_day_workorders]
    return preferred + same_day_workorders + remaining


def calculate_service_duration(sign_in_time, sign_out_time):
    # 业务规则：只要是外勤 FW 服务记录，不按签到/签退时长折算半天。
    return "1", "8.0"


def resolve_credentials_path(credentials_path=None):
    if credentials_path:
        return Path(credentials_path).expanduser()
    configured_path = os.getenv(FXIAOKE_CREDENTIALS_PATH_ENV, "").strip()
    return Path(configured_path or str(DEFAULT_CREDENTIALS_PATH)).expanduser()


def load_fxiaoke_credentials_from_env():
    phone_number = ""
    for env_name in FXIAOKE_PHONE_ENV_NAMES:
        phone_number = os.getenv(env_name, "").strip()
        if phone_number:
            break
    password = os.getenv(FXIAOKE_PASSWORD_ENV, "").strip()
    if phone_number or password:
        return FxiaokeCredentials(phone_number=phone_number, password=password, source="环境变量")
    return None


def load_fxiaoke_credentials_from_file(credentials_path=None):
    path = resolve_credentials_path(credentials_path)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"纷享销客凭据文件格式错误：{path}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"纷享销客凭据文件格式错误：{path}")

    phone_number = str(
        data.get("phone_number")
        or data.get("phone")
        or data.get("mobile")
        or data.get("account")
        or data.get("username")
        or ""
    ).strip()
    password = str(data.get("password") or "").strip()
    if phone_number or password:
        return FxiaokeCredentials(phone_number=phone_number, password=password, source=str(path))
    return None


def resolve_fxiaoke_credentials(credentials_path=None, raise_on_missing=True):
    env_credentials = load_fxiaoke_credentials_from_env()
    if env_credentials and env_credentials.is_complete:
        return env_credentials

    file_credentials = load_fxiaoke_credentials_from_file(credentials_path)
    sources = []
    if env_credentials:
        sources.append(env_credentials.source)
    if file_credentials:
        sources.append(file_credentials.source)
    if env_credentials or file_credentials:
        credentials = FxiaokeCredentials(
            phone_number=(
                (env_credentials.phone_number if env_credentials else "")
                or (file_credentials.phone_number if file_credentials else "")
            ),
            password=(
                (env_credentials.password if env_credentials else "")
                or (file_credentials.password if file_credentials else "")
            ),
            source=" + ".join(sources),
        )
        if credentials.is_complete:
            return credentials
    else:
        credentials = None

    if not raise_on_missing:
        return credentials

    path = resolve_credentials_path(credentials_path)
    raise RuntimeError(
        "未找到完整的纷享销客登录凭据。请设置环境变量 FXIAOKE_PHONE/FXIAOKE_PASSWORD，"
        f"或创建本机私有凭据文件：{path}"
    )


def save_fxiaoke_credentials(phone_number, password, credentials_path=None):
    path = resolve_credentials_path(credentials_path)
    payload = {
        "phone_number": str(phone_number).strip(),
        "password": str(password).strip(),
    }
    write_text_atomically(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def build_service_method(candidate):
    local_city = tenant_value("service_record", "local_city", required=True)
    combined = f"{candidate.sign_in_address} {candidate.sign_out_address}"
    if local_city in combined:
        return "市内上门"
    return "出差上门"


def resolve_failure_artifact_root(report_path):
    return report_path.parent / FAILURE_ARTIFACT_DIR_NAME


# Context identity is retained until close, avoiding id reuse across shared sessions.
_TRACE_SESSIONS: dict[int, dict[str, Any]] = {}
MAX_TRACE_BYTES = 64 * 1024 * 1024


def start_context_tracing(context):
    """Start only after authentication; diagnostics must never change business outcomes."""
    state = _TRACE_SESSIONS.setdefault(id(context), {"context": context})
    if state.get("active"):
        return
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        state["active"] = True
    except Exception:
        pass


def trace_phase(page, label):
    try:
        context = page.context
        state = _TRACE_SESSIONS.get(id(context), {})
        if not state.get("active"):
            return
        temporary = state.get("temporary")
        if temporary is not None:
            total = 0
            for path in Path(temporary.name).rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
                if total > MAX_TRACE_BYTES:
                    state["active"] = False
                    context.tracing.stop()
                    return
        if state.pop("group", False):
            context.tracing.group_end()
        context.tracing.group(label)
        state["group"] = True
    except Exception:
        pass


def _export_failure_trace(page, artifact_root, prefix):
    trace_dir = None
    try:
        context = page.context
        state = _TRACE_SESSIONS.get(id(context), {})
        if not state.get("active"):
            return []
        root = resolve_fxiaoke_trace_root(artifact_root)
        # Refuse symlinks at every existing component before creating private data.
        for component in (*reversed(root.parents), root):
            if component.is_symlink():
                raise ValueError("轨迹目录不允许使用符号链接。")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.parent.chmod(0o700)
        root.chmod(0o700)
        safe_prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", str(prefix))[:100] or "failure"
        trace_dir = Path(tempfile.mkdtemp(prefix=f"{safe_prefix}_", dir=root))
        trace_path = trace_dir / "trace.zip"
        descriptor = os.open(trace_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if state.pop("group", False):
            try:
                context.tracing.group_end()
            except Exception:
                pass
        try:
            context.tracing.stop_chunk(path=str(trace_path))
        finally:
            # Shared sessions can continue to collect later failures independently.
            try:
                context.tracing.start_chunk()
            except Exception:
                state["active"] = False
        trace_path.chmod(0o600)
        if trace_path.stat().st_size > MAX_TRACE_BYTES:
            raise ValueError("轨迹超过本次留存上限。")
        return [str(trace_path)]
    except Exception:
        if trace_dir is not None:
            shutil.rmtree(trace_dir, ignore_errors=True)
        return []


def retain_submission_failure_trace(page, artifact_root, prefix, error):
    """Keep evidence diagnostics while preserving the original exception identity."""
    try:
        traces = _export_failure_trace(page, artifact_root, prefix)
        if traces:
            error.artifacts = list(getattr(error, "artifacts", []) or []) + traces
    except Exception:
        pass


def capture_failure_artifacts(page, artifact_root, prefix):
    if page is None:
        return []

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact_dir = artifact_root / f"{prefix}_{timestamp}"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _export_failure_trace(page, artifact_root, prefix)
    artifacts = []

    screenshot_path = artifact_dir / "screenshot.png"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        artifacts.append(str(screenshot_path.resolve()))
    except Exception:
        pass

    html_path = artifact_dir / "page.html"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        artifacts.append(str(html_path.resolve()))
    except Exception:
        pass

    artifacts.extend(_export_failure_trace(page, artifact_root, prefix))
    return artifacts


def is_login_page(page):
    try:
        current_url = page.url or ""
    except PlaywrightError:
        current_url = ""
    parsed = urlsplit(current_url)
    host = (parsed.hostname or "").casefold()
    return bool(parsed.scheme == "https" and (host == "fxiaoke.com" or host.endswith(".fxiaoke.com"))
                and re.fullmatch(r"/proj/page/login(?:v2)?/?", parsed.path))


def wait_for_authenticated_workspace(page, timeout_ms=6_000):
    """A visible trusted workspace proves readiness; a blank page proves nothing."""
    try:
        page.wait_for_function("""() => {
            const u = new URL(location.href);
            if (u.protocol !== 'https:' || !(u.hostname === 'fxiaoke.com' || u.hostname.endsWith('.fxiaoke.com'))
                || !/^\\/XV\\/UI\\/Home\\/?$/.test(u.pathname)) return false;
            return [...document.querySelectorAll('a[data-link="#plan"], .publish-wrapper')].some(el => {
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            });
        }""", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        if is_login_page(page):
            return False
        raise AuthenticationUncertain()


def collect_environment_diagnostics(storage_state_path, profile_dir=None):
    diagnostics = []

    diagnostics.append(
        (
            "Chrome 可执行文件",
            CHROME_EXECUTABLE.exists(),
            str(CHROME_EXECUTABLE) if CHROME_EXECUTABLE.exists() else f"未找到 {CHROME_EXECUTABLE}",
            "请确认本机已安装 Google Chrome。",
        )
    )

    try:
        import playwright  # noqa: F401

        playwright_ok = True
        playwright_detail = "playwright 已安装。"
    except Exception as exc:  # pragma: no cover
        playwright_ok = False
        playwright_detail = str(exc)
    diagnostics.append(
        (
            "Playwright",
            playwright_ok,
            playwright_detail,
            "请先执行 python3 -m pip install --user playwright。",
        )
    )

    try:
        import ddddocr as _ddddocr  # noqa: F401

        ocr_ok = True
        ocr_detail = "ddddocr 已安装。"
    except Exception as exc:  # pragma: no cover
        ocr_ok = False
        ocr_detail = str(exc)
    diagnostics.append(
        (
            "ddddocr（可选兜底）",
            True,
            ocr_detail if ocr_ok else f"未安装，可跳过；仅 --allow-ocr-login 需要。{ocr_detail}",
            "如需启用 OCR 兜底，请先执行 python3 -m pip install --user ddddocr。",
        )
    )

    diagnostics.append(
        (
            "登录状态缓存",
            storage_state_path.exists(),
            f"已找到 {storage_state_path}" if storage_state_path.exists() else f"尚未生成 {storage_state_path}",
            "首次登录成功后会自动生成缓存文件。",
        )
    )

    resolved_profile_dir = resolve_profile_dir(profile_dir) or DEFAULT_PROFILE_DIR
    diagnostics.append(
        (
            "持久化浏览器 Profile",
            True,
            f"已找到 {resolved_profile_dir}" if resolved_profile_dir.exists() else f"尚未启用 {resolved_profile_dir}",
            "如需减少登录/验证码触发，可使用 --profile-dir 指定持久化 Profile。",
        )
    )

    credentials_error = ""
    try:
        credentials = resolve_fxiaoke_credentials(raise_on_missing=False)
    except RuntimeError as exc:
        credentials = None
        credentials_error = str(exc)
    credentials_path = resolve_credentials_path()
    diagnostics.append(
        (
            "登录凭据兜底",
            bool(credentials and credentials.is_complete),
            f"已配置自动登录凭据（来源：{credentials.source}）"
            if credentials and credentials.is_complete
            else credentials_error or "尚未配置完整自动登录凭据。",
            "请设置 FXIAOKE_PHONE/FXIAOKE_PASSWORD，或创建本机私有凭据文件："
            f"{credentials_path}",
        )
    )
    return diagnostics


def print_environment_diagnostics(storage_state_path, profile_dir=None):
    print("--- fw 环境自检 ---")
    for label, ok, detail, hint in collect_environment_diagnostics(storage_state_path, profile_dir=profile_dir):
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {label}: {detail}")
        if not ok:
            print(f"      建议：{hint}")


def print_draft(draft, report_path):
    if draft.work_type == "售后":
        print("[关联规则] 仅实施签到转同客户/部门及商机的售后工单，按不晚于服务日的创建时间选唯一最近单；缺字段、并列或归属不清停止；签到本身不修改。")
    print("--- 纷享销客服务记录助手 ---")
    print(f"日报来源：{report_path}")
    print(f"日报日期：{draft.report_date_display}")
    print(f"服务客户：{draft.customer_name}")
    print("记录类型：外勤服务")
    print(f"检索关键词：{draft.search_query}")
    print(f"去重前缀：{draft.duplicate_prefix}")
    print("\n服务内容预览：")
    print("=" * 50)
    print(draft.service_content)
    print("=" * 50)


def solve_login_captcha(page, ocr_engine):
    captcha_image = page.locator(LOGIN_CAPTCHA_IMAGE_SELECTOR).first
    captcha_input = page.locator(LOGIN_CAPTCHA_INPUT_SELECTOR).first
    captcha_image.wait_for(state="visible", timeout=FAST_LOADSTATE_TIMEOUT_MS)
    captcha_input.wait_for(state="visible", timeout=FAST_LOADSTATE_TIMEOUT_MS)
    image_bytes = captcha_image.screenshot(type="png")
    code = (ocr_engine.classification(image_bytes) or "").strip().upper()
    if len(code) != 4:
        raise RuntimeError(f"图形验证码识别失败：{code!r}")
    captcha_input.fill(code)
    return code


def maybe_click_agreement(page):
    button = page.get_by_text(LOGIN_AGREE_BUTTON_TEXT, exact=True)
    if button.count() and button.first.is_visible():
        button.first.click()
        return True
    return False


def extract_login_error(page):
    body_text = page.locator("body").inner_text()
    for marker in ("图形验证码错误", "验证码错误", "手机号或密码错误", "账号或密码错误"):
        if marker in body_text:
            return marker
    return ""


def emit_auth_progress(progress_callback, progress_prefix, message):
    if progress_callback:
        progress_callback(f"{progress_prefix} {message}")


def install_context_speedups(context):
    blocked_domains = (
        "hm.baidu.com",
        "googletagmanager.com",
        "google-analytics.com",
        "doubleclick.net",
        "googlesyndication.com",
    )

    def route_handler(route, request):
        url = request.url.lower()
        if request.resource_type in {"font", "media"} or any(domain in url for domain in blocked_domains):
            route.abort()
            return
        route.continue_()

    context.route("**/*", route_handler)


def create_browser_context(browser, context_kwargs, progress_callback=None, progress_prefix="[认证]"):
    try:
        return browser.new_context(**context_kwargs)
    except PlaywrightError:
        if "storage_state" not in context_kwargs:
            raise
        emit_auth_progress(progress_callback, progress_prefix, "缓存登录态无法读取，正在改用空白会话重新登录...")
        fallback_kwargs = dict(context_kwargs)
        fallback_kwargs.pop("storage_state", None)
        return browser.new_context(**fallback_kwargs)


def save_login_state(context, storage_state_path):
    storage_state_path = Path(storage_state_path)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{storage_state_path.name}.", dir=storage_state_path.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        context.storage_state(path=str(temp_path))
        temp_path.chmod(0o600)
        snapshot_bytes = temp_path.read_bytes()
        os.replace(temp_path, storage_state_path)
        _save_profile_cache_binding(context, storage_state_path, snapshot_bytes)
    finally:
        temp_path.unlink(missing_ok=True)


def _crm_cookie_key(cookie):
    domain = str(cookie.get("domain", "")).lstrip(".").casefold()
    if domain != "fxiaoke.com" and not domain.endswith(".fxiaoke.com"):
        return None
    if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
        return None
    return (cookie["name"], cookie.get("domain", ""), cookie.get("path", "/"),
            json.dumps(cookie.get("partitionKey"), sort_keys=True))


def _cookie_digest(cookie):
    return hashlib.sha256(json.dumps(cookie, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _save_profile_cache_binding(context, storage_state_path, snapshot_bytes):
    session = _TRACE_SESSIONS.get(id(context), {})
    if session.get("context") is not context or not session.get("profile_dir"):
        return
    path = Path(storage_state_path)
    # Another Profile can save the shared snapshot concurrently. Only label
    # bytes exported by this context; mismatched state/metadata pairs fail closed.
    if path.read_bytes() != snapshot_bytes:
        return
    snapshot = json.loads(snapshot_bytes)
    cookies = snapshot.get("cookies", [])
    anchors = [cookie for cookie in cookies if _crm_cookie_key(cookie) is not None
               and type(cookie.get("expires")) in (int, float) and cookie["expires"] > time.time()]
    if not anchors:
        return
    binding = {"schema_version": 1, "profile_dir": str(Path(session["profile_dir"]).expanduser().resolve()),
               "snapshot_digest": hashlib.sha256(snapshot_bytes).hexdigest(),
               "persistent_anchors": [_cookie_digest(cookie) for cookie in anchors]}
    target = path.with_name(path.name + ".profile-binding.json")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(binding, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _restore_profile_session_cookies(context, storage_state_path, profile_dir, progress_callback=None, progress_prefix="[认证]"):
    """Restore only missing session cookies from a private, same-profile snapshot."""
    path = Path(storage_state_path)
    binding_path = path.with_name(path.name + ".profile-binding.json")
    if not path.exists() or not binding_path.exists():
        return 0
    try:
        for item in (path, binding_path):
            stat = item.stat()
            if item.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                return 0
        raw = path.read_bytes()
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if (binding.get("schema_version") != 1
                or binding.get("profile_dir") != str(Path(profile_dir).expanduser().resolve())
                or binding.get("snapshot_digest") != hashlib.sha256(raw).hexdigest()):
            return 0
        snapshot = json.loads(raw)
        cookies = snapshot["cookies"]
        if not isinstance(cookies, list) or any(not isinstance(item, dict) for item in cookies):
            return 0
        expected = {_crm_cookie_key(item): item for item in cookies if _crm_cookie_key(item) is not None}
        if len(expected) != sum(_crm_cookie_key(item) is not None for item in cookies):
            return 0
        current = {_crm_cookie_key(item): item for item in context.cookies() if _crm_cookie_key(item) is not None}
        # Never merge a stale identity or a partly renewed session into another.
        if any(key in expected and current[key]["value"] != expected[key]["value"] for key in current):
            return 0
        anchors = [item for item in expected.values() if type(item.get("expires")) in (int, float)
                   and item["expires"] > time.time()]
        if not anchors or sorted(_cookie_digest(item) for item in anchors) != sorted(binding.get("persistent_anchors", [])):
            return 0
        if any(_crm_cookie_key(item) not in current or current[_crm_cookie_key(item)]["value"] != item["value"] for item in anchors):
            return 0
        missing = [item for key, item in expected.items() if key not in current and item.get("expires") == -1]
        if missing:
            context.add_cookies(missing)
            emit_auth_progress(progress_callback, progress_prefix, "已复用同一登录环境保存的会话，正在向CRM验证有效性...")
        return len(missing)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PlaywrightError):
        emit_auth_progress(progress_callback, progress_prefix, "保存的会话依据无法核实，未合并缓存；仍以CRM实际登录检查为准。")
        return 0


def try_save_login_state(context, storage_state_path, progress_callback=None):
    try:
        save_login_state(context, storage_state_path)
    except (OSError, PlaywrightError) as exc:
        if progress_callback:
            progress_callback(f"[登录缓存] CRM 结果已确认，缓存保存失败：{exc}")


def stop_browser_driver(playwright):
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


def resolve_profile_dir(profile_dir):
    if not profile_dir:
        return None
    return Path(os.path.expanduser(profile_dir)).resolve()


def reset_login_state(storage_state_path, profile_dir=None):
    resolved_profile_dir = resolve_profile_dir(profile_dir)
    if resolved_profile_dir:
        home = Path.home().resolve()
        protected = {
            home, PROJECT_ROOT, PRIVATE_LOGIN_STATE_ROOT,
            *home.parents, *PROJECT_ROOT.parents, *PRIVATE_LOGIN_STATE_ROOT.parents,
        }
        if resolved_profile_dir in protected:
            raise ValueError("Profile 不能指向用户目录、项目目录或配置根目录。")
        lock = BrowserProfileLock(resolved_profile_dir)
        if lock.is_process_alive(lock.read_lock_pid()):
            raise RuntimeError("浏览器 Profile 正在使用，已停止清理登录缓存。")
    removed = []
    if storage_state_path.exists():
        storage_state_path.unlink()
        removed.append(str(storage_state_path))
    binding_path = Path(storage_state_path).with_name(Path(storage_state_path).name + ".profile-binding.json")
    if binding_path.exists():
        binding_path.unlink()
        removed.append(str(binding_path))

    if resolved_profile_dir and resolved_profile_dir.exists():
        shutil.rmtree(resolved_profile_dir)
        removed.append(str(resolved_profile_dir))
    return removed


class BrowserProfileLock:
    def __init__(self, profile_dir):
        self.profile_dir = profile_dir
        self.lock_path = profile_dir / ".auto_daytexy.lock"
        self.fd = None

    def acquire(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            if self.release_stale_lock():
                self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, self.build_lock_text().encode("utf-8"))
                return self
            try:
                detail = self.lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                detail = ""
            suffix = f"（{detail}）" if detail else ""
            raise RuntimeError(f"持久化浏览器 Profile 正在被其他任务使用：{self.profile_dir}{suffix}") from exc
        os.write(self.fd, self.build_lock_text().encode("utf-8"))
        return self

    def build_lock_text(self):
        return (
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now().isoformat(timespec='seconds')}\n"
        )

    def read_lock_pid(self):
        try:
            text = self.lock_path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.search(r"^pid=(\d+)\s*$", text, re.M)
        return int(match.group(1)) if match else None

    @staticmethod
    def is_process_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def release_stale_lock(self):
        pid = self.read_lock_pid()
        if pid is None or self.is_process_alive(pid):
            return False
        try:
            self.lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def release(self):
        if self.fd is None:
            return
        try:
            owned = os.fstat(self.fd)
            current = self.lock_path.lstat()
            if (owned.st_dev, owned.st_ino) == (current.st_dev, current.st_ino):
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None


def remember_profile_lock(context, profile_lock):
    if profile_lock is not None:
        PROFILE_LOCKS[id(context)] = profile_lock


def release_profile_lock(context):
    if context is None:
        return
    profile_lock = PROFILE_LOCKS.pop(id(context), None)
    if profile_lock is not None:
        profile_lock.release()


def remaining_browser_timeout(deadline, limit_ms):
    """Clamp an operation to the shared monotonic read-only query budget."""
    if deadline is None:
        return limit_ms
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("只读回查等待预算已用完，保留当前结果供后续核对。")
    return min(limit_ms, remaining)


def wait_with_browser_budget(page, milliseconds, deadline=None):
    page.wait_for_timeout(remaining_browser_timeout(deadline, milliseconds))


def fast_goto(page, url, timeout_ms=DEFAULT_TIMEOUT_MS, *, deadline=None):
    page.goto(url, wait_until="commit", timeout=remaining_browser_timeout(deadline, timeout_ms))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=remaining_browser_timeout(deadline, FAST_LOADSTATE_TIMEOUT_MS))
    except PlaywrightTimeoutError:
        pass
    wait_with_browser_budget(page, FAST_GOTO_SETTLE_MS, deadline)


def wait_for_login_transition(page):
    deadline = time.time() + (POST_LOGIN_SETTLE_TIMEOUT_MS / 1000)
    while time.time() < deadline:
        if not is_login_page(page):
            return True, ""
        error_text = extract_login_error(page)
        if error_text:
            return False, error_text
        page.wait_for_timeout(200)
    return (not is_login_page(page)), extract_login_error(page)


def ensure_account_login_form(page):
    phone_field = page.locator(LOGIN_PHONE_SELECTOR).first
    try:
        if phone_field.count() and phone_field.is_visible():
            return
    except PlaywrightError:
        pass

    login_tab = page.get_by_text(LOGIN_TAB_TEXT, exact=True)
    login_tab.wait_for(state="visible", timeout=FAST_LOADSTATE_TIMEOUT_MS)

    last_error = None
    for _ in range(3):
        login_tab.click(force=True)
        page.wait_for_timeout(150)
        try:
            page.wait_for_selector(LOGIN_PHONE_SELECTOR, timeout=1_500, state="visible")
            page.wait_for_selector(LOGIN_PASSWORD_SELECTOR, timeout=1_500, state="visible")
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc

    raise last_error or RuntimeError("无法展开纷享销客账号登录表单。")


def wait_for_manual_login(page, context, storage_state_path, progress_callback=None, progress_prefix="[认证]"):
    emit_auth_progress(
        progress_callback,
        progress_prefix,
        "请在弹出的浏览器窗口手动完成登录或扫码；检测到进入工作台后会自动继续。",
    )
    deadline = time.time() + (MANUAL_LOGIN_TIMEOUT_MS / 1000)
    while time.time() < deadline:
        if not is_login_page(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=FAST_LOADSTATE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            save_login_state(context, storage_state_path)
            emit_auth_progress(progress_callback, progress_prefix, "已保存新的登录态。")
            return
        page.wait_for_timeout(1_000)
    raise RuntimeError("等待手动登录超时。请重新运行命令，并在浏览器窗口中完成登录或扫码。")


def login_to_fxiaoke(
    page,
    context,
    storage_state_path,
    phone_number,
    password,
    progress_callback=None,
    progress_prefix="[认证]",
    allow_ocr_login=False,
    max_attempts=6,
):
    if not phone_number or not password:
        raise RuntimeError("纷享销客账号或密码为空，无法自动登录。")

    emit_auth_progress(progress_callback, progress_prefix, "正在打开登录页...")
    fast_goto(page, FXIAOKE_LOGIN_URL)
    emit_auth_progress(progress_callback, progress_prefix, "正在等待登录表单可用...")
    ensure_account_login_form(page)

    for attempt in range(1, max_attempts + 1):
        if not is_login_page(page):
            save_login_state(context, storage_state_path)
            return

        emit_auth_progress(progress_callback, progress_prefix, f"正在执行自动登录（第 {attempt}/{max_attempts} 次）...")
        ensure_account_login_form(page)
        page.locator(LOGIN_PHONE_SELECTOR).fill(phone_number)
        page.locator(LOGIN_PASSWORD_SELECTOR).fill(password)
        page.locator(LOGIN_BUTTON_SELECTOR).click()
        login_ok, error_text = wait_for_login_transition(page)
        if not login_ok and not error_text:
            maybe_click_agreement(page)
            login_ok, error_text = wait_for_login_transition(page)
        if login_ok:
            save_login_state(context, storage_state_path)
            return

        captcha_input = page.locator(LOGIN_CAPTCHA_INPUT_SELECTOR).first
        captcha_visible = False
        try:
            captcha_visible = captcha_input.count() and captcha_input.is_visible()
        except PlaywrightError:
            captcha_visible = False
        if captcha_visible or error_text in {"图形验证码错误", "验证码错误"}:
            if not allow_ocr_login:
                raise ManualLoginRequired("检测到图形验证码，已切换到人工/扫码登录恢复。")
            emit_auth_progress(progress_callback, progress_prefix, "检测到验证码校验，正在自动识别...")
            import ddddocr

            ocr_engine = ddddocr.DdddOcr(show_ad=False)
            try:
                solve_login_captcha(page, ocr_engine)
            except Exception:
                image = page.locator(LOGIN_CAPTCHA_IMAGE_SELECTOR).first
                if image.count():
                    image.click()
                    page.wait_for_timeout(600)
                solve_login_captcha(page, ocr_engine)
            page.locator(LOGIN_BUTTON_SELECTOR).click()
            maybe_click_agreement(page)
            login_ok, error_text = wait_for_login_transition(page)
            if login_ok:
                save_login_state(context, storage_state_path)
                return

        if attempt >= max_attempts:
            raise RuntimeError(f"纷享销客登录失败：{error_text or '超过最大尝试次数。'}")
        if page.locator(LOGIN_CAPTCHA_IMAGE_SELECTOR).count():
            page.locator(LOGIN_CAPTCHA_IMAGE_SELECTOR).first.click()
            page.wait_for_timeout(600)

    raise RuntimeError("纷享销客登录失败。")


def launch_browser_session(playwright, storage_state_path, show_browser, profile_dir=None, progress_callback=None, progress_prefix="[认证]"):
    context_kwargs: dict[str, Any] = {
        "viewport": {"width": 1456, "height": 900},
    }
    resolved_profile_dir = resolve_profile_dir(profile_dir)
    profile_lock = None
    if resolved_profile_dir is not None:
        if not resolved_profile_dir.exists():
            emit_auth_progress(
                progress_callback,
                progress_prefix,
                "首次使用持久化登录环境，可能需要手动登录/扫码一次。",
            )
        emit_auth_progress(progress_callback, progress_prefix, f"正在使用持久化浏览器 Profile：{resolved_profile_dir}")
        profile_lock = BrowserProfileLock(resolved_profile_dir).acquire()
        context = None
        trace_temp = None
        try:
            trace_temp = tempfile.TemporaryDirectory(prefix="auto-daytexy-crm-trace-")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(resolved_profile_dir),
                traces_dir=trace_temp.name,
                executable_path=str(CHROME_EXECUTABLE),
                headless=not show_browser,
                args=["--disable-blink-features=AutomationControlled"],
                **context_kwargs,
            )
            _TRACE_SESSIONS[id(context)] = {"context": context, "temporary": trace_temp,
                                             "profile_dir": resolved_profile_dir,
                                             "storage_state_path": Path(storage_state_path).resolve()}
            remember_profile_lock(context, profile_lock)
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            _restore_profile_session_cookies(context, storage_state_path, resolved_profile_dir,
                                             progress_callback, progress_prefix)
            install_context_speedups(context)
            return context.browser, context, context.new_page()
        except Exception:
            if context is not None:
                close_browser_session(context.browser, context)
            else:
                profile_lock.release()
                if trace_temp is not None:
                    trace_temp.cleanup()
            raise

    trace_temp = tempfile.TemporaryDirectory(prefix="auto-daytexy-crm-trace-")
    try:
        browser = playwright.chromium.launch(
            executable_path=str(CHROME_EXECUTABLE),
            headless=not show_browser,
            args=["--disable-blink-features=AutomationControlled"],
            traces_dir=trace_temp.name,
        )
    except Exception:
        trace_temp.cleanup()
        raise
    if storage_state_path.exists():
        context_kwargs["storage_state"] = str(storage_state_path)
    context = None
    try:
        context = create_browser_context(
            browser,
            context_kwargs,
            progress_callback=progress_callback,
            progress_prefix=progress_prefix,
        )
        _TRACE_SESSIONS[id(context)] = {"context": context, "temporary": trace_temp}
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        install_context_speedups(context)
        return browser, context, context.new_page()
    except Exception:
        close_browser_session(browser, context)
        trace_temp.cleanup()
        raise


def close_browser_session(browser, context):
    state = _TRACE_SESSIONS.pop(id(context), {})
    if state.get("active"):
        try:
            if state.get("group"):
                context.tracing.group_end()
            context.tracing.stop()
        except Exception:
            pass
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
        finally:
            try:
                release_profile_lock(context)
            except OSError:
                pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if state.get("temporary") is not None:
        try:
            state["temporary"].cleanup()
        except Exception:
            pass


def recover_with_manual_login(
    playwright,
    browser,
    context,
    page,
    storage_state_path,
    show_browser,
    profile_dir=None,
    progress_callback=None,
    progress_prefix="[认证]",
):
    if not show_browser:
        emit_auth_progress(progress_callback, progress_prefix, "登录态失效，正在切换到可见浏览器以便手动/扫码登录...")
        close_browser_session(browser, context)
        browser, context, page = launch_browser_session(
            playwright,
            storage_state_path,
            show_browser=True,
            profile_dir=profile_dir,
            progress_callback=progress_callback,
            progress_prefix=progress_prefix,
        )
        fast_goto(page, FXIAOKE_HOME_URL)

    wait_for_manual_login(
        page,
        context,
        storage_state_path,
        progress_callback=progress_callback,
        progress_prefix=progress_prefix,
    )
    fast_goto(page, FXIAOKE_HOME_URL)
    return browser, context, page


def open_authenticated_page(
    storage_state_path,
    show_browser,
    progress_callback=None,
    progress_prefix="[认证]",
    allow_ocr_login=False,
    profile_dir=None,
    cached_login_only=False,
    recover_cached_login=False,
):
    emit_auth_progress(progress_callback, progress_prefix, "正在启动浏览器...")
    playwright = sync_playwright().start()
    browser = context = None
    try:
        browser, context, page = launch_browser_session(
            playwright,
            storage_state_path,
            show_browser=show_browser,
            profile_dir=profile_dir,
            progress_callback=progress_callback,
            progress_prefix=progress_prefix,
        )
        emit_auth_progress(progress_callback, progress_prefix, "正在载入缓存登录态...")
        emit_auth_progress(progress_callback, progress_prefix, "正在打开纷享销客首页...")
        fast_goto(page, FXIAOKE_HOME_URL)

        authenticated = wait_for_authenticated_workspace(page)
        if not authenticated:
            if cached_login_only and not recover_cached_login:
                raise CachedLoginExpired()
            credentials = resolve_fxiaoke_credentials(raise_on_missing=False)
            if credentials and credentials.is_complete:
                emit_auth_progress(
                    progress_callback,
                    progress_prefix,
                    f"检测到登录态已失效，正在使用{credentials.source}尝试账号登录...",
                )
                try:
                    login_to_fxiaoke(
                        page,
                        context,
                        storage_state_path=storage_state_path,
                        phone_number=credentials.phone_number,
                        password=credentials.password,
                        progress_callback=progress_callback,
                        progress_prefix=progress_prefix,
                        allow_ocr_login=allow_ocr_login,
                        **({"max_attempts": 1} if recover_cached_login and not allow_ocr_login else {}),
                    )
                except ManualLoginRequired as exc:
                    emit_auth_progress(progress_callback, progress_prefix, str(exc))
                    browser, context, page = recover_with_manual_login(
                        playwright,
                        browser,
                        context,
                        page,
                        storage_state_path,
                        show_browser=show_browser,
                        profile_dir=profile_dir,
                        progress_callback=progress_callback,
                        progress_prefix=progress_prefix,
                    )
                except RuntimeError as exc:
                    emit_auth_progress(progress_callback, progress_prefix, f"账号自动登录未完成：{exc} 改用人工/扫码登录恢复。")
                    browser, context, page = recover_with_manual_login(
                        playwright,
                        browser,
                        context,
                        page,
                        storage_state_path,
                        show_browser=show_browser,
                        profile_dir=profile_dir,
                        progress_callback=progress_callback,
                        progress_prefix=progress_prefix,
                    )
            else:
                emit_auth_progress(progress_callback, progress_prefix, "登录态已失效，且未配置完整账号密码，改用人工/扫码登录恢复。")
                browser, context, page = recover_with_manual_login(
                    playwright,
                    browser,
                    context,
                    page,
                    storage_state_path,
                    show_browser=show_browser,
                    profile_dir=profile_dir,
                    progress_callback=progress_callback,
                    progress_prefix=progress_prefix,
                )
            emit_auth_progress(progress_callback, progress_prefix, "登录完成，正在返回工作台...")
            fast_goto(page, FXIAOKE_HOME_URL)

        if not authenticated and not wait_for_authenticated_workspace(page):
            raise RuntimeError("纷享销客登录后仍未进入工作台页面。")
        # Login renewal paths persist immediately; every caller persists again
        # after its own operation completes, so avoid serializing the same state
        # twice on each successful browser session.
        emit_auth_progress(progress_callback, progress_prefix, "认证完成。")
        start_context_tracing(context)
        trace_phase(page, "authenticated/ready")
        return playwright, browser, context, page
    except Exception:
        close_browser_session(browser, context)
        playwright.stop()
        raise


ROUTING_POLICY = "implementation_to_latest_after_sales_v1"
ROUTING_AUDIT_FIELDS = ("work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source")


def resolve_onsite_routing(page, draft, candidate, workorder_customer, milestone_callback=None):
    """Resolve only the explicitly authorized implementation -> after-sales case."""
    from integrations.fxiaoke import onsite_routing_browser as routing_browser
    from integrations.fxiaoke.onsite_routing import (
        AFTER_SALES_TYPE, IMPLEMENTATION_TYPE, RoutingError,
        assert_target_matches, onsite_routing_enabled, select_after_sales_workorder,
    )
    if draft.work_type != "售后":
        if draft.routing_target is not None:
            raise RoutingError("非售后工作不能使用售后关联工单授权。")
        return draft, {}
    if not onsite_routing_enabled():
        if draft.routing_target is not None:
            raise RoutingError("当前未启用工单路由，无法使用已冻结的售后工单授权。")
        return draft, {}
    source = routing_browser.read_workorder_snapshot(page, candidate.jsfw)
    if re.sub(r"\s+", "", source["customer_name"]).casefold() != re.sub(r"\s+", "", workorder_customer).casefold():
        raise RoutingError("签到详情客户与工单列表客户不一致，未提交。")
    if source["record_type"] == AFTER_SALES_TYPE and draft.routing_target is None:
        return draft, {}
    if source["record_type"] != IMPLEMENTATION_TYPE:
        raise RoutingError("仅实施签到配合明确售后日报可自动调整关联工单。")
    if draft.routing_target is not None:
        if draft.source_checkin_workorder_id != candidate.jsfw or draft.routing_source is None:
            raise RoutingError("已冻结目标缺少原签到依据或来源已变化，未提交。")
        assert_target_matches(draft.routing_source, source)
        target = deepcopy(draft.routing_target)
        assert_target_matches(target, routing_browser.read_workorder_snapshot(page, target["workorder_id"]))
        select_after_sales_workorder(source, [target], draft.report_date_iso)
    else:
        target = routing_browser.find_latest_after_sales_workorder(page, source, draft.report_date_iso)
    if milestone_callback is None:
        raise RoutingError("缺少持久提交意图回调，无法冻结选单结果，未提交。")
    audit = dict(work_type="售后", routing_policy=ROUTING_POLICY,
                 routing_target=deepcopy(target), source_checkin_workorder_id=candidate.jsfw,
                 routing_source=deepcopy(source))
    milestone_callback("target_selected", workorder_id=target["workorder_id"], **deepcopy(audit))
    routed = replace(draft, routing_target=deepcopy(target),
                     source_checkin_workorder_id=candidate.jsfw, routing_source=deepcopy(source))
    routing_browser.open_target_workorder(page, target)
    return routed, audit


def revalidate_onsite_routing(page, draft):
    if draft.routing_target is None:
        return
    from integrations.fxiaoke.onsite_routing import assert_target_matches, select_after_sales_workorder, RoutingError
    from integrations.fxiaoke.onsite_routing_browser import read_workorder_snapshot
    if draft.work_type != "售后" or not draft.source_checkin_workorder_id or draft.routing_source is None:
        raise RoutingError("售后关联授权依据不完整，未提交。")
    source = read_workorder_snapshot(page, draft.source_checkin_workorder_id)
    target = read_workorder_snapshot(page, draft.routing_target["workorder_id"])
    assert_target_matches(draft.routing_source, source)
    assert_target_matches(draft.routing_target, target)
    select_after_sales_workorder(source, [target], draft.report_date_iso)


def submit_service_record_on_authenticated_page(
    page,
    context,
    draft,
    progress_callback=None,
    milestone_callback=None,
):
    if draft.record_kind != ONSITE_RECORD_KIND:
        raise RuntimeError(
            "远程 FWJL 服务记录功能已停用；请使用 fxiaoke_remote_workorder.py 创建《远程服务工单》。"
        )

    owner = require_service_owner()
    tenant_value("service_record", "start_time_field", required=True, token=True)
    if progress_callback:
        progress_callback("[技术服务记录] 2/5 正在定位当天签到关联工单...")
    trace_phase(page, "service_record/定位签到工单")
    candidate, workorder_customer = select_checkin_candidate(
        page,
        draft,
        progress_callback=progress_callback,
    )
    draft, routing_audit = resolve_onsite_routing(
        page, draft, candidate, workorder_customer, milestone_callback,
    ) if draft.work_type or draft.routing_target is not None else (draft, {})
    workorder_id = draft.routing_target["workorder_id"] if draft.routing_target else candidate.jsfw
    if draft.routing_target:
        workorder_customer = draft.routing_target["customer_name"]
    if progress_callback:
        progress_callback(f"[技术服务记录] 已定位工单 {workorder_id}，正在打开服务记录页签...")
    open_service_record_tab(page)

    existing_summaries = collect_existing_service_record_summaries(page)
    if progress_callback:
        progress_callback("[技术服务记录] 3/5 正在检查同日重复提交记录...")
    trace_phase(page, "service_record/查重")
    duplicate_detail = find_duplicate_service_record(
        context,
        existing_summaries,
        draft.report_date_iso,
        customer_name="",
        service_method=draft.service_method,
    )
    if duplicate_detail:
        if milestone_callback:
            milestone_callback(
                "crm_id_captured",
                workorder_id=workorder_id,
                service_record_id=duplicate_detail.record_id,
            )
        return AutomationResult(
            workorder_id=workorder_id,
            service_record_id=duplicate_detail.record_id,
            status="already_exists",
            crm_customer=workorder_customer,
            message=(
                f"已检测到 {draft.report_date_iso} 当天由{owner}提交的服务记录，无需重复提交。"
                f"工单：{workorder_id}，服务记录：{duplicate_detail.record_id}。"
            ),
            **routing_audit,
        )

    known_record_ids = {summary.record_id for summary in existing_summaries}
    if progress_callback:
        progress_callback("[技术服务记录] 4/5 正在填写服务记录表单...")
    revalidate_onsite_routing(page, draft)
    trace_phase(page, "service_record/填写与校验")
    open_new_service_record_form(page)
    start_time = draft.service_start_time or candidate.sign_in_time
    service_method = draft.service_method or build_service_method(candidate)
    fill_service_start_time(page, start_time)
    fill_service_end_time_if_available(page, draft.service_end_time)
    fill_service_content(page, draft.service_content)
    select_exact_radio_label(page, service_method)
    if draft.service_day_value and draft.workhour_value:
        day_value, workhour_value = draft.service_day_value, draft.workhour_value
    else:
        day_value, workhour_value = calculate_service_duration(
            candidate.sign_in_time,
            candidate.sign_out_time,
        )
    select_exact_radio_label(page, day_value)
    page.wait_for_timeout(500)
    ensure_workhour_value(page, workhour_value)
    wait_for_service_record_form(
        page, content=draft.service_content, start_time=start_time,
        end_time=draft.service_end_time, service_method=service_method,
        service_day=day_value, workhour=workhour_value,
    )
    revalidate_onsite_routing(page, draft)
    if progress_callback:
        progress_callback("[技术服务记录] 5/5 正在提交表单并等待服务记录身份复核...")
    trace_phase(page, "service_record/提交")
    submit_error = None
    try:
        service_record_id = submit_service_record(
            page,
            known_record_ids,
            milestone_callback=milestone_callback,
            workorder_id=workorder_id,
        )
    except ServiceRecordConfirmationUncertain as exc:
        submit_error = exc
        service_record_id = exc.service_record_id
    if progress_callback:
        progress_callback("[技术服务记录/复核] 正在只读核对工单、负责人和服务日期...")
    try:
        trace_phase(page, "service_record/回读核验")
        verification = verify_submitted_service_record_on_authenticated_page(
            page, context, draft, workorder_id, service_record_id=service_record_id,
        )
    except Exception as verification_error:
        raise ServiceRecordConfirmationUncertain(
            "服务记录已开始提交，但只读身份复核失败；请不要重复提交。",
            workorder_id=workorder_id,
            service_record_id=service_record_id,
        ) from verification_error
    if not verification["found"] or (
        service_record_id and verification["service_record_id"] != service_record_id
    ):
        raise ServiceRecordConfirmationUncertain(
            "服务记录已开始提交，但未找到身份一致的同日记录；请先在 CRM 中核对，不要重复提交。",
            workorder_id=workorder_id,
            service_record_id=service_record_id,
        ) from submit_error
    service_record_id = verification["service_record_id"]
    if milestone_callback:
        milestone_callback(
            "crm_id_captured", workorder_id=workorder_id, service_record_id=service_record_id,
        )
    return AutomationResult(
        workorder_id=workorder_id,
        service_record_id=service_record_id,
        status="submitted",
        **routing_audit,
        crm_customer=verification["crm_customer"],
        message=(
            f"服务记录提交成功并已复核。工单：{workorder_id}，客户：{verification['crm_customer']}，"
            f"服务记录：{service_record_id}。"
        ),
    )


def collect_current_checkin_candidates(page, report_date_iso, progress_callback=None):
    selected_selector = CHECKIN_CARD_SELECTOR
    try:
        # The field-sign feed can be DOM-attached while its list container is
        # temporarily judged non-visible by Playwright.  We only read text
        # here, and work-order clicks below are explicitly forced, so waiting
        # for attachment avoids a false timeout without weakening form-submit
        # confirmation.
        page.wait_for_selector(
            CHECKIN_CARD_SELECTOR,
            state="attached",
            timeout=CHECKIN_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        if progress_callback:
            progress_callback(
                "[技术服务记录] 主选择器未匹配，尝试备选选择器..."
            )
        page.wait_for_selector(
            CHECKIN_CARD_FALLBACK_SELECTOR,
            state="attached",
            timeout=CHECKIN_CARD_FALLBACK_TIMEOUT_MS,
        )
        selected_selector = CHECKIN_CARD_FALLBACK_SELECTOR
    cards = page.locator(selected_selector)
    candidates = []
    for index in range(cards.count()):
        card = cards.nth(index)
        try:
            card_text = card.inner_text(timeout=3_000)
        except PlaywrightTimeoutError:
            continue
        candidate = parse_checkin_candidate_from_text(card_text, index, report_date_iso)
        if candidate and candidate.sign_in_time.startswith(report_date_iso):
            candidates.append(candidate)
    return candidates


def extract_checkin_candidates(
    page, report_date_iso, progress_callback=None, *, refresh_retries=CHECKIN_CARD_REFRESH_RETRIES,
):
    if progress_callback:
        progress_callback(
            "[技术服务记录] 正在打开外勤签到页面（#stream/fieldsign）..."
        )
    fast_goto(page, FXIAOKE_FIELDSIGN_URL, timeout_ms=CHECKIN_CARD_TIMEOUT_MS)

    if progress_callback:
        progress_callback(
            f"[技术服务记录] 正在读取 {report_date_iso} 签到卡片（最多等待 "
            f"{CHECKIN_CARD_TIMEOUT_MS // 1000} 秒）..."
        )
    for attempt in range(refresh_retries + 1):
        candidates = collect_current_checkin_candidates(
            page,
            report_date_iso,
            progress_callback=progress_callback,
        )
        if candidates or attempt == refresh_retries:
            if progress_callback:
                progress_callback(
                    f"[技术服务记录] 已读取签到卡片，找到 {len(candidates)} 条当日候选工单。"
                )
            return candidates
        if progress_callback:
            progress_callback(
                "[技术服务记录] 当前卡片尚无当天记录，正在刷新签到流后再次读取..."
            )
        page.wait_for_timeout(CHECKIN_CARD_REFRESH_SETTLE_MS)
        fast_goto(page, FXIAOKE_FIELDSIGN_URL, timeout_ms=CHECKIN_CARD_TIMEOUT_MS)


def extract_customer_unit(page_text):
    match = CUSTOMER_UNIT_PATTERN.search(page_text)
    if not match:
        return ""
    return normalize_spacing(match.group(1))


def customer_matches_workorder(customer_name, workorder_customer):
    haystack = normalize_customer_match_text(workorder_customer)
    if not haystack:
        return False
    candidate_names = [customer_name]
    primary_name = resolve_customer_primary_name(customer_name)
    if primary_name and primary_name not in candidate_names:
        candidate_names.append(primary_name)

    for candidate_name in candidate_names:
        if university_subcollege_conflicts(candidate_name, workorder_customer):
            continue
        if any(alias in haystack for alias in build_customer_aliases(candidate_name)):
            return True
        if customer_core_matches(candidate_name, workorder_customer):
            return True
    return False


def describe_customer_match_basis(customer_name, workorder_customer):
    customer_core = normalize_customer_core_text(customer_name) or normalize_customer_match_text(customer_name)
    workorder_core = normalize_customer_core_text(workorder_customer) or normalize_customer_match_text(workorder_customer)
    return f"日报主客户核心“{customer_core or '未识别'}”，工单客户核心“{workorder_core or '未识别'}”"


def format_candidate_inspections(inspected, customer_name):
    parts = []
    for jsfw, customer in inspected:
        customer_text = customer or "未识别"
        parts.append(f"{jsfw}:{customer_text}（{describe_customer_match_basis(customer_name, customer_text)}）")
    return "；".join(parts)


def get_open_workorder_dialog(page):
    visible_dialogs = page.locator(f"{WORKORDER_DETAIL_DIALOG_SELECTOR}:visible")
    try:
        if visible_dialogs.count():
            return visible_dialogs.last
    except PlaywrightError:
        pass
    return page.locator(WORKORDER_DETAIL_DIALOG_SELECTOR).last


def get_open_workorder_detail_scope(page):
    visible_dialogs = page.locator(f"{WORKORDER_DETAIL_DIALOG_SELECTOR}:visible")
    try:
        if visible_dialogs.count():
            return visible_dialogs.last
    except PlaywrightError:
        pass
    return page.locator("body")


def extract_customer_unit_from_open_workorder(page):
    scope = get_open_workorder_detail_scope(page)

    try:
        customer_fields = scope.locator(CUSTOMER_UNIT_FIELD_VALUE_XPATH)
        if customer_fields.count():
            customer_text = normalize_spacing(customer_fields.first.inner_text(timeout=3_000))
            if customer_text:
                return customer_text
    except PlaywrightError:
        pass

    try:
        return extract_customer_unit(scope.inner_text(timeout=3_000))
    except PlaywrightError:
        return ""


def dismiss_active_workorder_dialog(page):
    dialog = page.locator(WORKORDER_DETAIL_DIALOG_SELECTOR).last
    if dialog.count() == 0:
        return

    try:
        if not dialog.is_visible():
            return
    except PlaywrightError:
        return

    close_button = dialog.locator(WORKORDER_DETAIL_CLOSE_SELECTOR).first
    closed = False
    try:
        if close_button.count() and close_button.is_visible():
            close_button.click(force=True)
            dialog.wait_for(state="hidden", timeout=5_000)
            closed = True
    except PlaywrightError:
        closed = False

    if closed:
        page.wait_for_timeout(500)
        return

    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden", timeout=5_000)
    page.wait_for_timeout(500)


def wait_for_workorder_detail_ready(page, timeout_ms=DEFAULT_TIMEOUT_MS):
    try:
        page.locator(WORKORDER_DETAIL_DIALOG_SELECTOR).last.wait_for(
            state="visible",
            timeout=min(timeout_ms, 8_000),
        )
    except PlaywrightError:
        if timeout_ms < DEFAULT_TIMEOUT_MS:
            raise
    page.wait_for_selector(SERVICE_RECORD_TAB_SELECTOR, timeout=timeout_ms)
    page.wait_for_timeout(600)


def is_workorder_detail_open(page):
    tab = page.locator(SERVICE_RECORD_TAB_SELECTOR).filter(has_text="服务记录").first
    try:
        return safe_locator_count(tab) > 0 and tab.is_visible(timeout=500)
    except PlaywrightError:
        return False


def click_workorder_link(link, use_dom_click=False):
    try:
        link.scroll_into_view_if_needed(timeout=3_000)
    except (AttributeError, PlaywrightError):
        pass

    if use_dom_click:
        try:
            link.evaluate("(element) => element.click()")
            return
        except (AttributeError, PlaywrightError):
            pass
        try:
            link.dispatch_event("click")
            return
        except (AttributeError, PlaywrightError):
            pass

    link.click(
        force=True,
        timeout=CHECKIN_WORKORDER_CLICK_TIMEOUT_MS,
        no_wait_after=True,
    )


def click_crm_menu_text(page, title):
    locators = page.locator(f'{CRM_MENU_TEXT_SELECTOR}[title="{title}"]')
    try:
        if not locators.count():
            locators = page.locator(CRM_MENU_TEXT_SELECTOR).filter(has_text=title)
    except PlaywrightError:
        locators = page.locator(CRM_MENU_TEXT_SELECTOR).filter(has_text=title)
    locator = wait_for_first_visible(
        locators,
        timeout_ms=8_000,
        description=f'CRM 菜单“{title}”',
    )
    try:
        locator.click(force=True)
    except PlaywrightError:
        locator.evaluate("(element) => element.click()")
    page.wait_for_timeout(500)


def open_crm_module(page):
    technical_service_menu = page.locator(f'{CRM_MENU_TEXT_SELECTOR}[title="技术服务管理"]').first
    try:
        if technical_service_menu.count() and technical_service_menu.is_visible():
            return
    except PlaywrightError:
        pass

    fast_goto(page, FXIAOKE_CRM_URL)
    page.wait_for_timeout(800)


def open_technical_service_list(page):
    open_crm_module(page)
    try:
        click_crm_menu_text(page, "技术服务管理")
        click_crm_menu_text(page, "技术服务")
        wait_for_first_visible(
            page.locator(TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
            timeout_ms=DEFAULT_TIMEOUT_MS,
            description="技术服务搜索框",
        )
    except (PlaywrightError, RuntimeError):
        # CRM occasionally leaves this group collapsed even after its title is clicked.
        fast_goto(page, FXIAOKE_TECHNICAL_SERVICE_URL)
        wait_for_first_visible(
            page.locator(TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
            timeout_ms=DEFAULT_TIMEOUT_MS,
            description="技术服务搜索框",
        )
    page.wait_for_timeout(800)


def first_visible(locator):
    for index in range(safe_locator_count(locator)):
        item = locator.nth(index)
        try:
            if item.is_visible(timeout=1_000):
                return item
        except PlaywrightError:
            continue
    return locator.first


def wait_for_first_visible(locator, timeout_ms=DEFAULT_TIMEOUT_MS, description="元素", *, deadline=None):
    local_deadline = time.time() + timeout_ms / 1000
    while time.time() < local_deadline:
        remaining_browser_timeout(deadline, timeout_ms)
        for index in range(safe_locator_count(locator)):
            item = locator.nth(index)
            try:
                if item.is_visible(timeout=remaining_browser_timeout(deadline, 300)):
                    return item
            except PlaywrightError:
                continue
        time.sleep(remaining_browser_timeout(deadline, 200) / 1000)
    raise RuntimeError(f"等待可见{description}超时。")


def click_visible_exact_text(page, text, timeout_ms=8_000, *, deadline=None):
    candidate_locators = [
        page.locator(
            "li.el-select-dropdown__item, "
            "div.el-select-dropdown__item, "
            "li.crm-ui-select-option, "
            "div.crm-ui-select-option, "
            "span"
        ).filter(has_text=text),
        page.get_by_text(text, exact=True),
    ]
    local_deadline = time.time() + timeout_ms / 1000
    while time.time() < local_deadline:
        remaining_browser_timeout(deadline, timeout_ms)
        for locator in candidate_locators:
            for index in range(safe_locator_count(locator)):
                item = locator.nth(index)
                item_text = normalize_spacing(safe_inner_text(item, timeout_ms=remaining_browser_timeout(deadline, 300)))
                try:
                    if item.is_visible(timeout=remaining_browser_timeout(deadline, 300)) and item_text == text:
                        item.click(force=True, **({"timeout": remaining_browser_timeout(deadline, 8_000)} if deadline is not None else {}))
                        return True
                except PlaywrightError:
                    continue
        time.sleep(remaining_browser_timeout(deadline, 200) / 1000)
    return False


def get_visible_technical_service_field(page, titles, description, *, deadline=None):
    selectors = ", ".join(
        f'{TECHNICAL_SERVICE_FIELD_SELECTOR}[data-title="{title}"]' for title in titles
    )
    return wait_for_first_visible(
        page.locator(selectors),
        timeout_ms=8_000,
        description=description,
        **({"deadline": deadline} if deadline is not None else {}),
    )


def choose_technical_service_dropdown_value(page, field, target_value, description, current_values=None, *, deadline=None):
    accepted_values = set(current_values or [target_value])
    current_value = normalize_spacing(safe_get_attribute(field, "data-title", timeout_ms=remaining_browser_timeout(deadline, FAST_LOCATOR_READ_TIMEOUT_MS)) or "")
    if current_value in accepted_values:
        return

    field.click(force=True, **({"timeout": remaining_browser_timeout(deadline, 8_000)} if deadline is not None else {}))
    wait_with_browser_budget(page, 300, deadline)
    if click_visible_exact_text(page, target_value, timeout_ms=8_000, **({"deadline": deadline} if deadline is not None else {})):
        wait_with_browser_budget(page, 800, deadline)
        return

    raise RuntimeError(f"无法在技术服务列表中选择{description}“{target_value}”。")


def select_technical_service_search_field(page, target_value="全字段", *, deadline=None):
    field = get_visible_technical_service_field(
        page,
        ("工单编码", "全字段"),
        "技术服务搜索字段",
        **({"deadline": deadline} if deadline is not None else {}),
    )
    choose_technical_service_dropdown_value(page, field, target_value, "搜索字段", **({"deadline": deadline} if deadline is not None else {}))


def open_known_workorder_for_service_record_verification(
    page,
    workorder_id,
    *,
    expected_service_record_id="",
):
    """Open a known technical-service workorder without relying on check-in cards."""
    normalized_workorder_id = str(workorder_id or "").strip()
    if not JSFW_ID_PATTERN.fullmatch(normalized_workorder_id):
        raise ValueError(f"技术服务工单编号格式无效：{workorder_id}")

    dismiss_active_workorder_dialog(page)
    open_technical_service_list(page)
    select_technical_service_search_field(page, "全字段")
    search_input = wait_for_first_visible(
        page.locator(TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR),
        timeout_ms=8_000,
        description="技术服务搜索框",
    )
    search_input.fill(normalized_workorder_id)
    search_input.press("Enter")
    page.wait_for_timeout(1_200)

    workorder_cell = wait_for_first_visible(
        page.locator(
            f'{WORKORDER_NAME_CELL_SELECTOR}[title="{normalized_workorder_id}"]'
        ),
        timeout_ms=15_000,
        description=f"技术服务工单 {normalized_workorder_id}",
    )
    last_error = None
    for attempt in range(3):
        try:
            click_workorder_link(workorder_cell, use_dom_click=attempt > 0)
            get_open_workorder_dialog(page).wait_for(state="visible", timeout=10_000)
            break
        except PlaywrightError as exc:
            last_error = exc
            page.wait_for_timeout(500)
    else:
        raise RuntimeError(
            f"无法打开技术服务工单 {normalized_workorder_id}。"
        ) from last_error

    deadline = time.time() + SERVICE_RECORD_VERIFY_LIST_TIMEOUT_MS / 1000
    workorder_customer = ""
    while time.time() < deadline:
        workorder_customer = extract_customer_unit_from_open_workorder(page)
        if workorder_customer:
            if expected_service_record_id:
                summary = wait_for_service_record_summary(
                    page,
                    expected_service_record_id,
                    "",
                    timeout_ms=2_000,
                )
                if summary is not None:
                    return workorder_customer
            else:
                return workorder_customer
        page.wait_for_timeout(300)
    if not workorder_customer:
        raise RuntimeError(
            f"已打开技术服务工单 {normalized_workorder_id}，但无法识别客户单位。"
        )
    return workorder_customer


def wait_for_service_record_summary(
    page,
    service_record_id,
    expected_report_date_iso,
    *,
    timeout_ms=SERVICE_RECORD_VERIFY_LIST_TIMEOUT_MS,
):
    normalized_record_id = str(service_record_id or "").strip()
    if not FWJL_ID_PATTERN.fullmatch(normalized_record_id):
        raise ValueError(f"服务记录编号格式无效：{service_record_id}")

    expected_date = str(expected_report_date_iso or "").strip()
    deadline = time.time() + timeout_ms / 1000
    last_summary = None
    while time.time() < deadline:
        summaries = collect_existing_service_record_summaries(page)
        summary = next(
            (item for item in summaries if item.record_id == normalized_record_id),
            None,
        )
        if summary is not None:
            last_summary = summary
            if not expected_date or summary.service_start_time:
                return summary
        page.wait_for_timeout(300)
    return last_summary


def collect_visible_workorder_links_in_card(card, workorder_id):
    expected_id = str(workorder_id or "").strip()
    exact_id_pattern = re.compile(
        rf"(?<![A-Z0-9-]){re.escape(expected_id)}(?![A-Z0-9-])"
    )
    links = card.locator(CHECKIN_WORKORDER_SELECTOR)
    matched_links = []
    for index in range(safe_locator_count(links)):
        link = links.nth(index)
        link_text = normalize_spacing(safe_inner_text(link))
        if (
            expected_id
            and exact_id_pattern.search(link_text)
        ):
            matched_links.append(link)
    return matched_links


def deduplicate_workorder_links_by_crm_object(matched_links):
    unique_links = []
    seen_crm_object_ids = set()
    for link in matched_links:
        try:
            crm_object_id = (safe_get_attribute(link, "data-id") or "").strip()
        except AttributeError:
            crm_object_id = ""
        if crm_object_id:
            if crm_object_id in seen_crm_object_ids:
                continue
            seen_crm_object_ids.add(crm_object_id)
        unique_links.append(link)
    return unique_links


class WorkorderLinkLookupError(RuntimeError):
    def __init__(self, workorder_id, matched_count, scope_label="当前签到卡片"):
        self.workorder_id = str(workorder_id or "").strip()
        self.matched_count = int(matched_count)
        super().__init__(
            f"{scope_label}中目标工单 {self.workorder_id or '未识别'} "
            f"匹配到 {self.matched_count} 个可见链接，已停止提交。"
        )


def find_visible_workorder_link_in_card(card, workorder_id):
    expected_id = str(workorder_id or "").strip()
    matched_links = deduplicate_workorder_links_by_crm_object(
        collect_visible_workorder_links_in_card(card, expected_id)
    )
    if len(matched_links) != 1:
        raise WorkorderLinkLookupError(
            expected_id,
            len(matched_links),
            scope_label="签到卡片",
        )
    return matched_links[0]


def find_visible_workorder_link_on_checkin_cards(page, workorder_id):
    cards = page.locator(CHECKIN_CARD_SELECTOR)
    if not safe_locator_count(cards):
        cards = page.locator(CHECKIN_CARD_FALLBACK_SELECTOR)

    matched_links = []
    for index in range(safe_locator_count(cards)):
        matched_links.extend(
            collect_visible_workorder_links_in_card(cards.nth(index), workorder_id)
        )
    matched_links = deduplicate_workorder_links_by_crm_object(matched_links)
    if len(matched_links) != 1:
        raise WorkorderLinkLookupError(
            workorder_id,
            len(matched_links),
            scope_label="当前签到卡片",
        )
    return matched_links[0]


def wait_for_visible_workorder_link_on_checkin_cards(
    page,
    workorder_id,
    timeout_ms=CHECKIN_WORKORDER_RELOCATE_TIMEOUT_MS,
    poll_ms=CHECKIN_WORKORDER_RELOCATE_POLL_MS,
):
    deadline = time.monotonic() + timeout_ms / 1000
    last_error = None
    while time.monotonic() < deadline:
        try:
            return find_visible_workorder_link_on_checkin_cards(page, workorder_id)
        except WorkorderLinkLookupError as exc:
            if exc.matched_count != 0:
                raise
            last_error = exc
        page.wait_for_timeout(poll_ms)
    if last_error is not None:
        raise last_error
    raise RuntimeError("签到卡片中的目标工单重新定位超时。")


def open_workorder_from_candidate(page, candidate):
    dismiss_active_workorder_dialog(page)
    link = wait_for_visible_workorder_link_on_checkin_cards(page, candidate.jsfw)
    last_error = None
    for attempt in range(3):
        try:
            click_workorder_link(link, use_dom_click=attempt > 0)
            wait_for_workorder_detail_ready(
                page,
                timeout_ms=CHECKIN_WORKORDER_DETAIL_TIMEOUT_MS,
            )
            return
        except PlaywrightError as exc:
            last_error = exc
            page.wait_for_timeout(500)
    if last_error:
        raise last_error


class NoMatchingCheckinCandidate(RuntimeError):
    """The visible feed has no candidate with a confirmed matching customer."""


def choose_candidate(page, draft, candidates, progress_callback=None):
    if not candidates:
        raise NoMatchingCheckinCandidate(
            f"当前可见签到流未找到 {draft.report_date_iso} 当天的技术外勤签到记录。"
            "已刷新签到流后再次读取，且未发生 CRM 提交；如果这一天实际为远程/非外勤，"
            "请在 rb 中将“是否外勤”填写为“否”。"
        )

    ordered_candidates = order_checkin_candidates_for_draft(candidates, draft)
    inspected = []

    candidate_count = len(ordered_candidates)
    for index, candidate in enumerate(ordered_candidates, start=1):
        if progress_callback:
            progress_callback(
                f"[技术服务记录] 正在核对签到工单 {index}/{candidate_count}："
                f"{candidate.jsfw}..."
            )
        open_workorder_from_candidate(page, candidate)
        workorder_customer = extract_customer_unit_from_open_workorder(page)
        inspected.append((candidate.jsfw, workorder_customer))
        if progress_callback:
            progress_callback(
                f"[技术服务记录] 工单 {candidate.jsfw} 客户："
                f"{workorder_customer or '未识别'}。"
            )
        if customer_matches_workorder(draft.customer_name, workorder_customer):
            return candidate, workorder_customer

    # Unreadable customer details are an unknown result, not a feed mismatch.
    mismatch_error = NoMatchingCheckinCandidate if all(customer for _, customer in inspected) else RuntimeError
    if len(candidates) == 1:
        candidate = candidates[0]
        inspected_customer = inspected[0][1] if inspected else ""
        raise mismatch_error(
            f"{draft.report_date_iso} 当天只有 1 条签到记录，但工单 {candidate.jsfw} 的客户单位"
            f"“{inspected_customer or '未识别'}”与日报客户“{draft.customer_name}”不匹配，已停止提交。"
            f"{describe_customer_match_basis(draft.customer_name, inspected_customer)}。"
        )

    inspected_summary = format_candidate_inspections(inspected, draft.customer_name)

    raise mismatch_error(
        f"{draft.report_date_iso} 当天存在多条签到记录，但没有任何工单客户单位与“{draft.customer_name}”匹配。"
        f"已检查：{inspected_summary or '无可用工单信息'}。"
    )


def select_checkin_candidate(page, draft, progress_callback=None):
    candidates = extract_checkin_candidates(
        page, draft.report_date_iso, progress_callback=progress_callback, refresh_retries=0,
    )
    try:
        return choose_candidate(page, draft, candidates, progress_callback=progress_callback)
    except NoMatchingCheckinCandidate:
        if progress_callback:
            progress_callback("[技术服务记录] 当前签到流尚无匹配工单，正在刷新后只读复核一次...")
        dismiss_active_workorder_dialog(page)
        page.wait_for_timeout(CHECKIN_CARD_REFRESH_SETTLE_MS)
        candidates = extract_checkin_candidates(
            page, draft.report_date_iso, progress_callback=progress_callback, refresh_retries=0,
        )
        return choose_candidate(page, draft, candidates, progress_callback=progress_callback)


def open_service_record_tab(page):
    tab = page.locator(SERVICE_RECORD_TAB_SELECTOR).filter(has_text="服务记录").first
    tab.click()
    page.locator(f"{SERVICE_RECORD_ADD_CONTAINER_SELECTOR} {SERVICE_RECORD_ADD_SELECTOR}").first.wait_for(
        state="visible",
        timeout=SERVICE_RECORD_TAB_TIMEOUT_MS,
    )
    page.wait_for_timeout(500)


def build_absolute_fxiaoke_url(path_or_url):
    if not path_or_url:
        return ""
    return urljoin(FXIAOKE_BASE_URL, path_or_url)


def extract_detail_field_value(page_text, label):
    lines = [normalize_spacing(line) for line in page_text.splitlines() if normalize_spacing(line)]
    for index, line in enumerate(lines[:-1]):
        if line == label:
            return lines[index + 1]
    return ""


def extract_detail_owner_name(page_text):
    lines = [normalize_spacing(line) for line in page_text.splitlines() if normalize_spacing(line)]
    for index in range(len(lines) - 2):
        if lines[index] == "负责人" and lines[index + 2] == "负责人主属部门":
            return lines[index + 1]
    return ""


def parse_service_record_detail(page_text, record_id=""):
    return ExistingServiceRecordDetail(
        record_id=record_id,
        owner_name=extract_detail_owner_name(page_text),
        service_start_time=extract_detail_field_value(page_text, "服务开始时间"),
        customer_name=extract_detail_field_value(page_text, "客户单位"),
        service_method=extract_detail_field_value(page_text, "服务方式"),
    )


def is_same_day_service_record(service_start_time, report_date_iso):
    return bool(service_start_time) and service_start_time.startswith(report_date_iso)


def safe_locator_count(locator):
    try:
        return locator.count()
    except PlaywrightError:
        return 0


def safe_get_attribute(locator, name, timeout_ms=FAST_LOCATOR_READ_TIMEOUT_MS):
    try:
        return locator.get_attribute(name, timeout=timeout_ms)
    except PlaywrightError:
        return ""


def safe_inner_text(locator, timeout_ms=FAST_LOCATOR_READ_TIMEOUT_MS):
    try:
        return locator.inner_text(timeout=timeout_ms)
    except PlaywrightError:
        return ""


def safe_input_value(locator):
    try:
        return (locator.input_value() or "").strip()
    except PlaywrightError:
        return ""


def locator_is_visible(locator, timeout_ms=FAST_LOCATOR_READ_TIMEOUT_MS):
    try:
        return locator.is_visible(timeout=timeout_ms)
    except TypeError:
        try:
            return locator.is_visible()
        except PlaywrightError:
            return False
    except PlaywrightError:
        return False


def locator_is_enabled(locator, timeout_ms=FAST_LOCATOR_READ_TIMEOUT_MS):
    try:
        if locator.get_attribute("disabled", timeout=timeout_ms) is not None:
            return False
    except PlaywrightError:
        pass
    if str(safe_get_attribute(locator, "aria-disabled", timeout_ms=timeout_ms) or "").lower() == "true":
        return False
    class_text = safe_get_attribute(locator, "class", timeout_ms=timeout_ms) or ""
    if "disabled" in class_text or "ipt-disabled" in class_text:
        return False
    try:
        return locator.is_enabled(timeout=timeout_ms)
    except TypeError:
        try:
            return locator.is_enabled()
        except PlaywrightError:
            return True
    except PlaywrightError:
        return True


def form_value_equals(actual, expected):
    actual_text = str(actual or "").strip()
    expected_text = str(expected or "").strip()
    if actual_text == expected_text:
        return True
    try:
        return float(actual_text) == float(expected_text)
    except ValueError:
        return False


def extract_fwjl_id(value):
    match = FWJL_ID_PATTERN.search(value or "")
    return match.group(1) if match else ""


def collect_visible_service_record_ids(page):
    name_cells = page.locator(SERVICE_RECORD_NAME_CELL_SELECTOR)
    record_ids = set()
    for index in range(safe_locator_count(name_cells)):
        cell = name_cells.nth(index)
        if not locator_is_visible(cell):
            continue
        record_text = normalize_spacing(
            safe_get_attribute(cell, "title") or safe_inner_text(cell)
        )
        record_id = extract_fwjl_id(record_text)
        if record_id:
            record_ids.add(record_id)
    return record_ids


def collect_new_service_record_ids_from_response(response, known_record_ids):
    try:
        request = response.request
        if getattr(request, "resource_type", "") not in {"xhr", "fetch"}:
            return set()
        if str(getattr(request, "method", "")).upper() != "POST":
            return set()
        response_text = response.text()
    except Exception:
        return set()
    return {
        record_id
        for record_id in FWJL_ID_PATTERN.findall(response_text or "")
        if record_id not in known_record_ids
    }


def collect_existing_service_record_summaries(page):
    start_field = tenant_value("service_record", "start_time_field", required=True, token=True)
    name_cells = page.locator(SERVICE_RECORD_NAME_CELL_SELECTOR)
    summaries = []
    for index in range(safe_locator_count(name_cells)):
        cell = name_cells.nth(index)
        if not locator_is_visible(cell):
            continue
        record_text = normalize_spacing(
            safe_get_attribute(cell, "title") or safe_inner_text(cell)
        )
        record_id = extract_fwjl_id(record_text)
        record_data_id = (safe_get_attribute(cell, "data-id") or "").strip()
        link = cell.locator("a.fx-icon-newtab").first
        detail_url = ""
        if safe_locator_count(link):
            detail_url = build_absolute_fxiaoke_url(safe_get_attribute(link, "href") or "")
        if not (record_id and record_data_id and detail_url):
            continue
        service_start_time = ""
        if record_data_id:
            grid = cell.locator(
                "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' dt-main ')][1]"
            )
            if not safe_locator_count(grid):
                grid = cell.locator("xpath=..")
            start_cells = grid.locator(
                f'div.j-td[data-id="{record_data_id}"][data-fieldname="{start_field}"]'
            )
            if safe_locator_count(start_cells):
                service_start_time = normalize_spacing(safe_inner_text(start_cells.first))
        if record_id and record_data_id and detail_url:
            summaries.append(
                ExistingServiceRecordSummary(
                    record_id=record_id,
                    record_data_id=record_data_id,
                    detail_url=detail_url,
                    service_start_time=service_start_time,
                )
            )
    return summaries


def load_service_record_detail(context, summary):
    from integrations.fxiaoke.read_only import WebDetailObserver, service_record_values

    service_record_object = tenant_value("service_record", "object", token=True)
    detail_page = context.new_page()
    try:
        for attempt in range(3):
            with WebDetailObserver(
                detail_page, summary.detail_url, service_record_object, summary.record_data_id,
            ) as observed:
                detail_page.goto(summary.detail_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                # The response is atomic; successful reads need no global network-idle wait.
                if observed.enabled:
                    for _ in range(6):
                        values = service_record_values(observed.value, summary.record_id)
                        if values is not None:
                            return ExistingServiceRecordDetail(**values)
                        detail_page.wait_for_timeout(100)
                detail_ready = "() => document.body && (document.body.innerText.includes('服务记录编号') || document.body.innerText.includes('服务开始时间') || document.body.innerText.includes('负责人'))"
                if observed.enabled:
                    deadline = time.monotonic() + 15
                    while True:
                        values = service_record_values(observed.value, summary.record_id)
                        if values is not None:
                            return ExistingServiceRecordDetail(**values)
                        try:
                            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                            detail_page.wait_for_function(detail_ready, timeout=min(300, remaining_ms))
                            break
                        except PlaywrightTimeoutError:
                            if time.monotonic() >= deadline:
                                values = service_record_values(observed.value, summary.record_id)
                                if values is not None:
                                    return ExistingServiceRecordDetail(**values)
                                raise
                else:
                    detail_page.wait_for_function(detail_ready, timeout=15_000)

                for _ in range(8):
                    values = service_record_values(observed.value, summary.record_id)
                    if values is not None:
                        return ExistingServiceRecordDetail(**values)
                    page_text = detail_page.locator("body").inner_text()
                    lines = [normalize_spacing(line) for line in page_text.splitlines()]
                    if (
                        lines.count("服务记录编号") == 1
                        and extract_detail_field_value(page_text, "服务记录编号") == summary.record_id
                    ):
                        detail = parse_service_record_detail(page_text, record_id=summary.record_id)
                        fields = (detail.owner_name, detail.service_start_time, detail.customer_name, detail.service_method)
                        labels = {"服务记录编号", "负责人", "负责人主属部门", "服务开始时间", "客户单位", "服务方式"}
                        if (
                            all(value and value != "--" and value not in labels for value in fields)
                            and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}(?::\d{2})?", detail.service_start_time)
                        ):
                            try:
                                datetime.fromisoformat(detail.service_start_time)
                            except ValueError:
                                pass
                            else:
                                return detail
                    detail_page.wait_for_timeout(500)

            if attempt < 2:
                detail_page.wait_for_timeout(800)
        raise RuntimeError(f"服务记录 {summary.record_id} 详情编号或必要字段未完整核实，已停止核对。")
    finally:
        detail_page.close()


def find_duplicate_service_record(
    context,
    existing_summaries,
    report_date_iso,
    customer_name="",
    service_method="",
):
    owner = require_service_owner()
    same_day_summaries = [
        summary for summary in existing_summaries
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:\s|T|$)", summary.service_start_time or "")
        or is_same_day_service_record(summary.service_start_time, report_date_iso)
    ]
    if not same_day_summaries:
        return None

    for summary in same_day_summaries:
        detail = load_service_record_detail(context, summary)
        if not detail.owner_name:
            raise RuntimeError(
                f"已找到同日服务记录 {summary.record_id}，但无法识别负责人，已停止提交。"
            )
        if detail.owner_name == owner and is_same_day_service_record(
            detail.service_start_time,
            report_date_iso,
        ):
            if service_method:
                if not detail.service_method:
                    raise RuntimeError(
                        f"已找到同日服务记录 {summary.record_id}，但无法识别服务方式，已停止提交。"
                    )
                if detail.service_method != service_method:
                    continue
            if customer_name and detail.customer_name and not customer_matches_workorder(
                customer_name,
                detail.customer_name,
            ):
                continue
            if customer_name and not detail.customer_name:
                raise RuntimeError(
                    f"已找到同日服务记录 {summary.record_id}，但无法识别客户单位，已停止提交。"
                )
            return detail
    return None


def open_new_service_record_form(page):
    selector = f"{SERVICE_RECORD_ADD_CONTAINER_SELECTOR} {SERVICE_RECORD_ADD_SELECTOR}"
    add_button = page.locator(selector).first
    add_button.click()
    page.wait_for_selector(SERVICE_START_TIME_SELECTOR, timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(300)


def select_exact_radio_label(page, label_text):
    xpath = (
        "//label[contains(@class,'item radio')]"
        f"[.//span[contains(@class,'el-label') and normalize-space()='{label_text}']]"
    )
    locator = page.locator(f"xpath={xpath}").filter(visible=True)
    if safe_locator_count(locator) != 1:
        raise RuntimeError(f"服务记录的“{label_text}”选项不是唯一可见项，已停止提交。")
    locator.click()


def fill_service_start_time(page, sign_in_time):
    field = page.locator(SERVICE_START_TIME_SELECTOR + ":visible").first
    field.click()
    field.fill(sign_in_time)
    field.press("Tab")
    page.wait_for_timeout(500)


def fill_service_end_time_if_available(page, service_end_time):
    if not service_end_time:
        return
    fields = page.locator(SERVICE_START_TIME_SELECTOR + ":visible")
    if safe_locator_count(fields) < 2:
        raise RuntimeError("无法读取服务记录结束时间，已停止提交。")
    field = fields.nth(1)
    try:
        field.click()
        field.fill(service_end_time)
        field.press("Tab")
        page.wait_for_timeout(300)
    except PlaywrightError as exc:
        raise RuntimeError("无法填写服务记录结束时间，已停止提交。") from exc


def fill_service_content(page, content):
    field = page.locator(SERVICE_CONTENT_TEXTAREA_SELECTOR + ":visible").first
    field.click()
    field.fill(content)
    page.wait_for_timeout(300)


def ensure_workhour_value(page, expected_value):
    fields = page.locator(WORK_HOUR_INPUT_SELECTOR)
    editable_field = None
    for index in range(safe_locator_count(fields)):
        field = fields.nth(index)
        if not locator_is_visible(field):
            continue
        current_value = safe_input_value(field)
        if form_value_equals(current_value, expected_value):
            return
        if (editable_field is None and locator_is_enabled(field)
                and safe_get_attribute(field, "readonly") is None):
            editable_field = field

    if editable_field is None:
        return

    editable_field.click()
    editable_field.fill(expected_value)
    editable_field.press("Tab")
    page.wait_for_timeout(300)



def read_service_record_form_snapshot(page):
    """Read one visible form together, excluding old hidden dialogs and labels."""
    return page.evaluate(
        """({contentSelector, timeSelector, hourSelector, submitSelector}) => {
            const visible = el => el.isConnected && el.getClientRects().length > 0
                && getComputedStyle(el).visibility !== 'hidden';
            const visibleAll = (root, selector) => [...root.querySelectorAll(selector)].filter(visible);
            const contents = visibleAll(document, contentSelector);
            if (contents.length !== 1) return {invalid: '服务内容不是唯一可见字段'};
            let form = contents[0].parentElement;
            while (form && !visibleAll(form, submitSelector).length) form = form.parentElement;
            if (!form || visibleAll(form, submitSelector).length !== 1)
                return {invalid: '无法确定唯一服务记录表单'};
            const hours = visibleAll(form, hourSelector);
            if (hours.length !== 1) return {invalid: '工时不是唯一可见字段'};
            const times = visibleAll(form, timeSelector);
            const labels = visibleAll(form, 'label.item.radio');
            const checked = labels.filter(label => {
                const control = label.querySelector('input[type="radio"]');
                if (control) return control.checked;
                // The live CRM marks a chosen tile with the bare "checked" class
                // (<label class="item radio checked">); older builds used
                // "is-checked", so both must be accepted.
                return label.getAttribute('aria-checked') === 'true'
                    || label.classList.contains('is-checked')
                    || label.classList.contains('checked')
                    || !!label.querySelector('.is-checked, .checked, [aria-checked="true"]');
            }).map(label => (label.querySelector('.el-label')?.textContent || '').trim());
            return {content: contents[0].value, times: times.map(el => el.value),
                    hours: hours[0].value, checked};
        }""",
        {"contentSelector": SERVICE_CONTENT_TEXTAREA_SELECTOR,
         "timeSelector": SERVICE_START_TIME_SELECTOR,
         "hourSelector": WORK_HOUR_INPUT_SELECTOR,
         "submitSelector": SERVICE_RECORD_SUBMIT_SELECTOR},
    )


def wait_for_service_record_form(page, *, content, start_time, end_time="",
                                 service_method, service_day, workhour):
    """Allow dependent controls to settle; never click Submit with unverified values."""
    deadline = time.monotonic() + SERVICE_RECORD_FORM_TIMEOUT_SECONDS
    while True:
        snapshot = read_service_record_form_snapshot(page)
        errors = []
        if not isinstance(snapshot, dict) or snapshot.get("invalid"):
            errors.append(snapshot.get("invalid", "表单无法读取") if isinstance(snapshot, dict) else "表单无法读取")
        else:
            if str(snapshot.get("content", "")).strip() != str(content).strip():
                errors.append("服务内容")
            times = snapshot.get("times", [])
            for index, expected, label in ((0, start_time, "开始时间"), (1, end_time, "结束时间")):
                if not expected:
                    continue
                try:
                    matches = datetime.fromisoformat(times[index]) == datetime.fromisoformat(expected)
                except (IndexError, TypeError, ValueError):
                    matches = False
                if not matches:
                    errors.append(label)
            if not form_value_equals(snapshot.get("hours", ""), workhour):
                errors.append("工时")
            checked = snapshot.get("checked", [])
            for expected, label in ((service_method, "服务方式"), (service_day, "服务天数")):
                if not expected or checked.count(expected) != 1:
                    errors.append(label)
        if not errors:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("服务记录提交前字段核验未通过：" + "、".join(errors) + "；未提交。")
        page.wait_for_timeout(min(100, remaining * 1000))


def submit_service_record(
    page,
    known_record_ids,
    milestone_callback=None,
    workorder_id="",
):
    require_service_owner()
    known_record_ids = set(known_record_ids)
    response_record_ids = set()
    service_record_id = ""

    def capture_service_record_response(response):
        response_record_ids.update(
            collect_new_service_record_ids_from_response(response, known_record_ids)
        )

    listener_registered = False
    try:
        page.on("response", capture_service_record_response)
        listener_registered = True
    except (AttributeError, PlaywrightError):
        pass
    submit_button = page.locator(SERVICE_RECORD_SUBMIT_SELECTOR).first
    try:
        if milestone_callback:
            milestone_callback("submit_clicked")
        submit_button.click()
        deadline = time.time() + SERVICE_RECORD_SUBMIT_TIMEOUT_SECONDS
        last_seen_ids: set[str] = set()
        while time.time() < deadline:
            if response_record_ids:
                if len(response_record_ids) != 1:
                    raise ServiceRecordConfirmationUncertain(
                        "提交响应含多个新服务记录编号，无法确认本次记录；请先只读核对。",
                        workorder_id=workorder_id,
                    )
                service_record_id = next(iter(response_record_ids))
                if milestone_callback:
                    milestone_callback(
                        "crm_id_captured",
                        workorder_id=workorder_id,
                        service_record_id=service_record_id,
                    )
                return service_record_id
            page.wait_for_timeout(SERVICE_RECORD_SUBMIT_POLL_MS)
            current_ids = collect_visible_service_record_ids(page)
            last_seen_ids = current_ids or last_seen_ids
            new_ids = current_ids - known_record_ids
            if new_ids:
                if len(new_ids) != 1:
                    raise ServiceRecordConfirmationUncertain(
                        "列表出现多个新服务记录编号，无法确认本次记录；请先只读核对。",
                        workorder_id=workorder_id,
                    )
                service_record_id = next(iter(new_ids))
                if milestone_callback:
                    milestone_callback(
                        "crm_id_captured",
                        workorder_id=workorder_id,
                        service_record_id=service_record_id,
                    )
                return service_record_id
        seen_text = f"当前已识别：{', '.join(sorted(last_seen_ids))}" if last_seen_ids else "当前未识别到服务记录编号"
        raise ServiceRecordConfirmationUncertain(
            f"服务记录已点击提交，但未检测到新的服务记录编号。{seen_text}。",
            workorder_id=workorder_id,
        )
    except SubmissionEvidenceError:
        raise
    except ServiceRecordConfirmationUncertain:
        raise
    except Exception as exc:
        raise ServiceRecordConfirmationUncertain(
            "服务记录已开始提交但结果未确认；请先只读核对，禁止重复提交。",
            workorder_id=workorder_id,
            service_record_id=service_record_id,
        ) from exc
    finally:
        if listener_registered:
            try:
                page.remove_listener("response", capture_service_record_response)
            except (AttributeError, PlaywrightError):
                pass


def service_record_detail_matches(detail, draft, service_record_id=""):
    owner = require_service_owner()
    return bool(
        detail and (not service_record_id or detail.record_id == service_record_id)
        and detail.owner_name == owner
        and is_same_day_service_record(detail.service_start_time, draft.report_date_iso)
        and (not detail.customer_name or customer_matches_workorder(draft.customer_name, detail.customer_name))
        and (not draft.service_method or detail.service_method == draft.service_method)
    )


def verify_submitted_service_record_on_authenticated_page(
    page, context, draft, workorder_id, *, service_record_id="",
):
    require_service_owner()
    workorder_customer = open_known_workorder_for_service_record_verification(
        page,
        workorder_id,
    )
    if draft.routing_target:
        if workorder_id != draft.routing_target["workorder_id"] or re.sub(r"\s+", "", workorder_customer).casefold() != re.sub(r"\s+", "", draft.routing_target["customer_name"]).casefold():
            raise RuntimeError("服务记录回读工单或完整客户与已冻结目标不一致。")
    if not customer_matches_workorder(draft.customer_name, workorder_customer):
        return {
            "found": False,
            "workorder_id": workorder_id,
            "service_record_id": None,
            "crm_customer": workorder_customer,
        }
    open_service_record_tab(page)
    if service_record_id:
        summary = wait_for_service_record_summary(page, service_record_id, draft.report_date_iso)
        detail = load_service_record_detail(context, summary) if summary else None
        if not service_record_detail_matches(detail, draft, service_record_id):
            detail = None
    else:
        detail = find_duplicate_service_record(
            context,
            collect_existing_service_record_summaries(page),
            draft.report_date_iso,
            customer_name=draft.customer_name,
            service_method=draft.service_method,
        )
    return {
        "found": detail is not None,
        "workorder_id": workorder_id,
        "service_record_id": detail.record_id if detail else None,
        "crm_customer": workorder_customer,
    }


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
    if not CHROME_EXECUTABLE.exists():
        raise RuntimeError(f"未找到 Chrome 可执行文件：{CHROME_EXECUTABLE}")

    playwright = browser = context = page = None
    artifact_root = resolve_failure_artifact_root(report_path)
    try:
        if progress_callback:
            progress_callback("[技术服务记录] 1/5 正在打开纷享销客并检查登录状态...")
        playwright, browser, context, page = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[技术服务记录/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
            **({"cached_login_only": True} if cached_login_only or draft.work_type == "售后" else {}),
        )
        if milestone_callback:
            milestone_callback("browser_started")
        result = submit_service_record_on_authenticated_page(
            page,
            context,
            draft,
            progress_callback=progress_callback,
            milestone_callback=milestone_callback,
        )
        if result_callback:
            result_callback(serialize_result(draft, result))
        try_save_login_state(context, storage_state_path, progress_callback)
        return result
    except CachedLoginExpired:
        raise
    except SubmissionEvidenceError as exc:
        retain_submission_failure_trace(page, artifact_root, f"{draft.report_date_compact}_fw_evidence", exc)
        raise
    except ServiceRecordConfirmationUncertain as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_compact}_fw",
        )
        raise AutomationFailure(
            str(exc),
            artifacts=artifacts,
            status="uncertain",
            workorder_id=exc.workorder_id,
            service_record_id=exc.service_record_id,
        ) from exc
    except PlaywrightTimeoutError as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_compact}_fw",
        )
        raise AutomationFailure(f"浏览器自动化等待超时：{exc}", artifacts=artifacts) from exc
    except PlaywrightError as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_compact}_fw",
        )
        raise AutomationFailure(f"浏览器自动化执行失败：{exc}", artifacts=artifacts) from exc
    except Exception as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_compact}_fw",
        )
        raise AutomationFailure(str(exc), artifacts=artifacts) from exc
    finally:
        try:
            close_browser_session(browser, context)
        except Exception:
            pass
        finally:
            stop_browser_driver(playwright)


def run_service_record_verification(
    draft,
    storage_state_path,
    report_path,
    *,
    workorder_id="",
    service_record_id="",
    show_browser=False,
    progress_callback=None,
    allow_ocr_login=False,
    profile_dir=None,
    routing_target=None,
    source_checkin_workorder_id="",
    routing_source=None,
):
    if not CHROME_EXECUTABLE.exists():
        raise RuntimeError(f"未找到 Chrome 可执行文件：{CHROME_EXECUTABLE}")
    playwright = browser = context = page = None
    artifact_root = resolve_failure_artifact_root(report_path)
    try:
        frozen_target = getattr(draft, "routing_target", None)
        if routing_target is not None and frozen_target is not None and routing_target != frozen_target:
            raise RuntimeError("显式复核目标与草稿冻结工单不一致，已停止核验。")
        if routing_target is None:
            routing_target = frozen_target
        if routing_target is not None:
            frozen_source = getattr(draft, "routing_source", None)
            frozen_source_id = getattr(draft, "source_checkin_workorder_id", "")
            if routing_source is not None and frozen_source is not None and routing_source != frozen_source:
                raise RuntimeError("显式签到快照与冻结来源不一致，已停止核验。")
            if source_checkin_workorder_id and frozen_source_id and source_checkin_workorder_id != frozen_source_id:
                raise RuntimeError("显式签到编号与冻结来源不一致，已停止核验。")
            routing_source = routing_source if routing_source is not None else frozen_source
            source_checkin_workorder_id = source_checkin_workorder_id or frozen_source_id
            if workorder_id and workorder_id != routing_target.get("workorder_id"):
                raise RuntimeError("复核编号与已冻结关联目标不一致。")
            draft = replace(draft, work_type="售后", routing_target=deepcopy(routing_target),
                            source_checkin_workorder_id=source_checkin_workorder_id,
                            routing_source=deepcopy(routing_source))
            workorder_id = routing_target["workorder_id"]
        if progress_callback:
            if workorder_id:
                progress_callback("[技术服务记录复核] 正在按已知工单编号查询服务记录...")
            else:
                progress_callback("[技术服务记录复核] 正在查询签到工单和同日服务记录...")
        playwright, browser, context, page = open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=storage_state_path,
            show_browser=show_browser,
            progress_callback=progress_callback,
            progress_prefix="[技术服务记录复核/认证]",
            allow_ocr_login=allow_ocr_login,
            profile_dir=profile_dir,
            **({"cached_login_only": True} if draft.routing_target is not None else {}),
        )
        if draft.routing_target is not None:
            revalidate_onsite_routing(page, draft)
        if workorder_id:
            workorder_customer = open_known_workorder_for_service_record_verification(
                page,
                workorder_id,
                expected_service_record_id=service_record_id,
            )
            if draft.routing_target and re.sub(r"\s+", "", workorder_customer).casefold() != re.sub(r"\s+", "", draft.routing_target["customer_name"]).casefold():
                raise RuntimeError("复核客户与已冻结目标完整部门不一致。")
            if not customer_matches_workorder(draft.customer_name, workorder_customer):
                try_save_login_state(context, storage_state_path, progress_callback)
                return {
                    "query_completed": True,
                    "found": False,
                    "workorder_id": workorder_id,
                    "service_record_id": service_record_id or None,
                    "crm_customer": workorder_customer,
                    "message": (
                        f"工单 {workorder_id} 的客户单位“{workorder_customer}”与日报客户“{draft.customer_name}”不匹配。"
                    ),
                    "artifacts": [],
                }
            if service_record_id:
                summary = wait_for_service_record_summary(
                    page,
                    service_record_id,
                    draft.report_date_iso,
                )
                detail = load_service_record_detail(context, summary) if summary else None
                found = service_record_detail_matches(detail, draft, service_record_id)
                try_save_login_state(context, storage_state_path, progress_callback)
                return {
                    "query_completed": True,
                    "found": found,
                    "workorder_id": workorder_id,
                    "service_record_id": service_record_id,
                    "crm_customer": workorder_customer,
                    "message": "" if found else (
                        f"工单 {workorder_id} 中未找到 {service_record_id} 的同日服务记录。"
                    ),
                    "artifacts": [],
                }
            open_service_record_tab(page)
            summaries = collect_existing_service_record_summaries(page)
            detail = find_duplicate_service_record(
                context,
                summaries,
                draft.report_date_iso,
                customer_name=draft.customer_name,
                service_method=draft.service_method,
            )
            candidate_workorder_id = workorder_id
        else:
            candidate, workorder_customer = select_checkin_candidate(
                page, draft, progress_callback=progress_callback,
            )
            open_service_record_tab(page)
            summaries = collect_existing_service_record_summaries(page)
            if service_record_id:
                summary = next(
                    (item for item in summaries if item.record_id == service_record_id),
                    None,
                )
                detail = load_service_record_detail(context, summary) if summary else None
                if not service_record_detail_matches(detail, draft, service_record_id):
                    detail = None
            else:
                detail = find_duplicate_service_record(
                    context,
                    summaries,
                    draft.report_date_iso,
                    customer_name=draft.customer_name,
                    service_method=draft.service_method,
                )
            candidate_workorder_id = candidate.jsfw
        try_save_login_state(context, storage_state_path, progress_callback)
        return {
            "query_completed": True,
            "found": detail is not None,
            "workorder_id": candidate_workorder_id,
            "service_record_id": detail.record_id if detail else service_record_id or None,
            "crm_customer": workorder_customer or draft.customer_name,
            "artifacts": [],
        }
    except Exception as exc:
        artifacts = capture_failure_artifacts(
            page,
            artifact_root,
            f"{draft.report_date_compact}_fw_verify",
        )
        return {
            "query_completed": False,
            "found": False,
            "message": str(exc),
            "artifacts": artifacts,
        }
    finally:
        close_browser_session(browser, context)
        stop_browser_driver(playwright)


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
                        "step": "service_record",
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
        if args.json_output:
            diagnostics = collect_environment_diagnostics(storage_state_path, profile_dir=profile_dir)
            print(
                json.dumps(
                    {
                        "ok": all(item[1] for item in diagnostics),
                        "step": "service_record",
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
            print_environment_diagnostics(storage_state_path, profile_dir=profile_dir)
        return 0

    report_path = resolve_report_path(args.report_path)
    if args.routing_pending_file and not args.verify_workorder:
        raise RuntimeError("路由待办参数仅用于按已有工单编号只读核验。")
    target_section = find_target_report_section(report_path, args.report_date)
    drafts = []
    if is_onsite_workday(report_text=target_section, report_path=report_path):
        drafts = build_service_record_drafts(
            target_section,
            main_only=True,
            work_type=args.work_type,
            report_path=report_path,
        )
    if args.customer is not None:
        customer = args.customer.strip()
        drafts = [draft for draft in drafts if draft.customer_name == customer] if customer else []
        if len(drafts) != 1:
            raise RuntimeError("指定客户未唯一匹配日报服务草稿，已停止提交，请先核对客户。")
    if not args.json_output:
        for draft in drafts:
            print_draft(draft, report_path)

    if not drafts:
        message = "目标日报没有需要提交的服务记录。"
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "service_record",
                        "status": "empty",
                        "message": message,
                        "workorder_id": None,
                        "service_record_id": None,
                        "artifacts": [],
                        "drafts": [],
                        "results": [],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(message)
        return 0

    if args.verify_workorder:
        progress_callback = lambda message: emit_progress(message, json_output=args.json_output)
        verifications = []
        for draft in drafts:
            routing_options = {}
            if args.routing_pending_file:
                from packages.report_domain.history import resolve_fxiaoke_pending_queue_path
                queue_path = Path(args.routing_pending_file).expanduser().resolve()
                if queue_path != resolve_fxiaoke_pending_queue_path(report_path).resolve():
                    raise RuntimeError("路由复核只允许读取本日报关联的待办文件。")
                entries = [json.loads(line, object_pairs_hook=unique_customer_alias_object)
                           for line in queue_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                matches = [entry for entry in entries if isinstance(entry, dict)
                           and entry.get("step") == "service_record"
                           and entry.get("report_date") == draft.report_date_iso
                           and normalize_inline_text(entry.get("service_customer", "")).casefold() == normalize_inline_text(draft.customer_name).casefold()
                           and isinstance(entry.get("routing_target"), dict)
                           and entry["routing_target"].get("workorder_id") == args.verify_workorder]
                if len(matches) != 1 or matches[0].get("work_type") != "售后" or matches[0].get("routing_policy") != ROUTING_POLICY:
                    raise RuntimeError("待办中的路由依据缺失或不唯一，请人工核对；原文件未修改。")
                routing_options = {key: matches[0].get(key) for key in ("routing_target", "source_checkin_workorder_id", "routing_source")}
            verification = run_service_record_verification(
                draft,
                storage_state_path,
                report_path,
                workorder_id=args.verify_workorder,
                service_record_id=args.verify_service_record,
                show_browser=args.show_browser,
                progress_callback=progress_callback,
                allow_ocr_login=args.allow_ocr_login,
                profile_dir=profile_dir,
                **routing_options,
            )
            verifications.append((draft, verification))

        verification_values = [verification for _, verification in verifications]
        query_completed = all(item.get("query_completed") for item in verification_values)
        found = query_completed and all(item.get("found") for item in verification_values)
        status = "verified" if found else "not_found" if query_completed else "error"
        message_parts = []
        for draft, verification in verifications:
            if verification.get("message"):
                message_parts.append(str(verification["message"]))
            elif verification.get("found"):
                message_parts.append(
                    f"已确认 {draft.report_date_iso} 服务记录："
                    f"工单 {verification.get('workorder_id') or args.verify_workorder}，"
                    f"服务记录 {verification.get('service_record_id') or '已找到'}。"
                )
            else:
                message_parts.append(
                    f"工单 {verification.get('workorder_id') or args.verify_workorder} "
                    f"未找到 {draft.report_date_iso} 的匹配服务记录。"
                )
        payload = {
            "ok": query_completed,
            "step": "service_record",
            "status": status,
            "found": found,
            "message": "\n".join(message_parts),
            "workorder_id": args.verify_workorder,
            "service_record_id": args.verify_service_record or next(
                (item.get("service_record_id") for item in verification_values if item.get("service_record_id")),
                None,
            ),
            "artifacts": [
                artifact
                for item in verification_values
                for artifact in item.get("artifacts", [])
            ],
            "drafts": [serialize_draft(draft) for draft, _ in verifications],
            "results": verification_values,
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("\n[技术服务记录只读核验结果]")
            print(payload["message"])
        return 0 if query_completed else 1

    if args.preview_only:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "service_record",
                        "status": "preview",
                        "message": "\n\n".join(draft.service_content for draft in drafts),
                        "workorder_id": None,
                        "service_record_id": None,
                        "artifacts": [],
                        "drafts": [serialize_draft(draft) for draft in drafts],
                        "results": [],
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    if not should_auto_confirm(args, stdin_isatty=sys.stdin.isatty()):
        confirm = read_brief_input("\n是否继续自动填写并提交纷享销客服务记录？(y/n): ").strip().lower()
        if confirm not in {"y", "yes"}:
            if args.json_output:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "step": "service_record",
                            "status": "cancelled",
                            "message": "已取消。",
                            "workorder_id": None,
                            "service_record_id": None,
                            "artifacts": [],
                            "drafts": [serialize_draft(draft) for draft in drafts],
                            "results": [],
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print("\n已取消。")
            return 0

    evidence_context: dict[str, Any] = {}
    existing_intent, intent_path = reserve_fxiaoke_submission_intent(
        target_section, "service_record", profile_dir, report_path=report_path,
        inherited_token=args.submission_intent_token,
        evidence_context=evidence_context,
    )
    if existing_intent is not None:
        payload = build_fxiaoke_submission_intent_blocked_result("service_record", "技术服务记录", existing_intent)
        print(json.dumps(payload, ensure_ascii=False) if args.json_output else payload["message"])
        return 1
    checkpoint_fxiaoke_submission(evidence_context, expected_customers=[draft.customer_name for draft in drafts])
    progress_callback = lambda message: emit_progress(message, json_output=args.json_output)
    if not args.json_output:
        print("\n[浏览器自动化正在执行，请稍候...]")
    results = []
    result_values: list[dict[str, Any]] = []
    for index, draft in enumerate(drafts, start=1):
        milestones: dict[str, Any] = {}

        def milestone_callback(event, **fields):
            milestones[event] = True
            milestones.update(fields)
            checkpoint_fxiaoke_submission(evidence_context, child_index=index - 1, event=event, fields=fields)

        def result_callback(item):
            checkpoint_fxiaoke_submission(evidence_context, child_index=index - 1, result=item)
        if len(drafts) > 1:
            progress_callback(f"[技术服务记录] 正在处理第 {index}/{len(drafts)} 个服务客户：{draft.customer_name}")
        try:
            if any(item.get("status") == "auth_expired" for item in result_values):
                raise CachedLoginExpired()
            result = run_browser_automation(
                draft,
                storage_state_path=storage_state_path,
                report_path=report_path,
                show_browser=args.show_browser,
                progress_callback=progress_callback,
                allow_ocr_login=args.allow_ocr_login,
                profile_dir=profile_dir,
                milestone_callback=milestone_callback,
                result_callback=result_callback,
                **({"cached_login_only": True} if args.submission_intent_token else {}),
            )
        except SubmissionEvidenceError:
            raise
        except Exception as exc:
            result = AutomationResult(
                workorder_id=str(getattr(exc, "workorder_id", "") or milestones.get("workorder_id", "")),
                service_record_id=getattr(exc, "service_record_id", None) or milestones.get("service_record_id"),
                status=str(getattr(exc, "status", "error") or "error"),
                message=str(exc),
                artifacts=list(getattr(exc, "artifacts", []) or []),
            )
        results.append((draft, result))
        item = serialize_result(draft, result)
        if milestones.get("routing_target"):
            item.update({key: deepcopy(milestones[key]) for key in ROUTING_AUDIT_FIELDS if key in milestones})
        if result.status not in {"submitted", "already_exists"}:
            item["retry_action"] = "verify_crm" if milestones.get("submit_clicked") or result.status == "uncertain" else "safe_retry"
            item["submission_phase"] = "submit_clicked" if item["retry_action"] == "verify_crm" else "before_submit"
        result_values.append(item)
        result_callback(item)
    ok = all(result.status in {"submitted", "already_exists"} for _, result in results)
    payload = {
        "ok": ok,
        "step": "service_record",
        "status": (
            "uncertain"
            if any(item.get("retry_action") == "verify_crm" for item in result_values)
            else "error"
            if not ok
            else "submitted"
            if any(item["status"] == "submitted" for item in result_values)
            else result_values[-1]["status"]
        ),
        "message": "\n".join(result.message for _, result in results),
        "workorder_id": result_values[-1]["workorder_id"],
        "service_record_id": result_values[-1]["service_record_id"],
        "artifacts": [artifact for _, result in results for artifact in result.artifacts],
        "drafts": [serialize_draft(draft) for draft, _ in results],
        "results": result_values,
    }
    finish_fxiaoke_direct_submission(target_section, report_path, "service_record", payload, intent_path)
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("\n[浏览器自动化结果]")
        for _, result in results:
            print(result.message)
        if payload.get("local_recording_error"):
            print(payload["message"])
    return 0 if payload.get("ok") else 1


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
                        "step": "service_record",
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
