# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import re
import signal
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from openai import OpenAI

from integrations.fxiaoke import crm_enabled
from packages.report_domain.history import (
    fcntl,
    FXIAOKE_PENDING_QUEUE_NAME as FXIAOKE_PENDING_QUEUE_NAME,
    FXIAOKE_SUBMISSION_HISTORY_NAME as FXIAOKE_SUBMISSION_HISTORY_NAME,
    clear_pending_fxiaoke_tasks,
    extract_fxiaoke_service_mode_flags as extract_fxiaoke_service_mode_flags,
    reconcile_pending_service_records_from_history,
    record_fxiaoke_submission_history,
    record_pending_fxiaoke_task,
    read_pending_queue_text,
    resolve_fxiaoke_pending_queue_path,
    resolve_fxiaoke_submission_history_path as resolve_fxiaoke_submission_history_path,
)
from packages.report_domain.paths import (
    DEFAULT_MACOS_REPORT_PATH as DEFAULT_MACOS_REPORT_PATH,
    DEFAULT_WINDOWS_REPORT_PATH as DEFAULT_WINDOWS_REPORT_PATH,
    get_default_report_path as get_default_report_path,
    resolve_report_path,
)
from packages.report_domain.report_format import (
    REPORT_DATE_PATTERN,
    REPORT_FIELD_ALIASES,
    REPORT_SECTION_PATTERN as REPORT_SECTION_PATTERN,
    SECTION_LABELS,
    build_daily_work_summary_from_report as build_daily_work_summary_from_report,
    canonicalize_report_block,
    extract_report_customer_names,
    extract_report_date,
    find_saved_report_section,
    normalize_report_date_argument as normalize_report_date_argument,
    normalize_report_date_to_iso,
    parse_report_fields,
    parse_report_service_entries,
    split_report_sections,
    strip_markdown_noise,
)
from packages.report_domain.workday import (
    REMOTE_MAIN_SERVICE_START_DATE,
    is_onsite_workday,
    normalize_onsite_value,
    resolve_report_work_type,
    resolve_preview_onsite_value as resolve_preview_onsite_value,
)
from ._shared import (
    APPLE_TERMINAL_CLIPBOARD_COMMAND,
    BASE_URL,
    DEFAULT_API_CREDENTIALS_PATH as DEFAULT_API_CREDENTIALS_PATH,
    DEFAULT_MODEL_NAME as DEFAULT_MODEL_NAME,
    MODEL_NAME as MODEL_NAME,
    REQUEST_TIMEOUT_SECONDS,
    build_chat_completion_options,
    request_chat_completion,
    configure_stdio,
    emit_terminal_message,
    copy_to_clipboard,
    extract_message_content,
    get_env_value,
    is_apple_terminal,
    load_api_key_from_credentials,
    read_clipboard_text,
    read_brief_input,
    should_auto_confirm,
    write_text_atomically,
)
# Re-exported for test compatibility (originally module-level in daily_report)
from ._shared import (
    configure_readline as configure_readline,
    is_libedit_readline as is_libedit_readline,
    prompt_toolkit_prompt as prompt_toolkit_prompt,
    readline as readline,
    should_use_prompt_toolkit as should_use_prompt_toolkit,
)
from .weekly_preview import (
    PreviewDataError,
    build_preview_entry,
    load_preview_entries as load_preview_entries,
    normalize_work_type,
    resolve_preview_json_path,
    resolve_preview_markdown_path,
    upsert_preview_entry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_HISTORY_LIMIT = 3
DAILY_REPORT_MAX_TOKENS = 768
OUTPUT_SECTION_ORDER = [
    "服务客户",
    "对应销售",
    "问题描述",
    "处理结果",
    "处理情况",
    "远程服务支持",
    "明日安排",
]
STRUCTURED_FIELD_LABELS = {
    "service_customer": "服务客户",
    "is_onsite": "是否外勤",
    "work_type": "工作类型",
    "sales_contact": "对应销售",
    "onsite_location": "现场地点",
    "today_task": "今日事项",
    "completed_actions": "完成内容",
    "result_status": "处理结果",
    "service_customer_2": "第二服务客户",
    "sales_contact_2": "第二对应销售",
    "today_task_2": "第二今日事项",
    "completed_actions_2": "第二完成内容",
    "result_status_2": "第二处理结果",
    "remote_support": "远程服务支持",
    "tomorrow_plan": "明日安排",
    "reflection": "感想",
}
STRUCTURED_FIELD_ORDER = [
    "service_customer",
    "is_onsite",
    "work_type",
    "sales_contact",
    "onsite_location",
    "today_task",
    "completed_actions",
    "result_status",
    "service_customer_2",
    "sales_contact_2",
    "today_task_2",
    "completed_actions_2",
    "result_status_2",
    "remote_support",
    "tomorrow_plan",
    "reflection",
]
STRUCTURED_FIELD_ALIASES = {
    "服务客户": "service_customer",
    "客户": "service_customer",
    "学校": "service_customer",
    "单位": "service_customer",
    "客户学校": "service_customer",
    "是否外勤": "is_onsite",
    "外勤": "is_onsite",
    "外勤签到": "is_onsite",
    "工作类型": "work_type",
    "类型": "work_type",
    "工作类别": "work_type",
    "服务类型": "work_type",
    "对应销售": "sales_contact",
    "销售": "sales_contact",
    "现场地点": "onsite_location",
    "地点": "onsite_location",
    "现场": "onsite_location",
    "位置": "onsite_location",
    "今日事项": "today_task",
    "事项": "today_task",
    "工作": "today_task",
    "任务": "today_task",
    "问题描述": "today_task",
    "问题": "today_task",
    "完成内容": "completed_actions",
    "完成": "completed_actions",
    "已完成": "completed_actions",
    "处理情况": "completed_actions",
    "处理结果": "result_status",
    "结果": "result_status",
    "状态": "result_status",
    "第二服务客户": "service_customer_2",
    "第二客户": "service_customer_2",
    "第二学校": "service_customer_2",
    "服务客户2": "service_customer_2",
    "客户2": "service_customer_2",
    "学校2": "service_customer_2",
    "2服务客户": "service_customer_2",
    "2客户": "service_customer_2",
    "第二对应销售": "sales_contact_2",
    "第二销售": "sales_contact_2",
    "对应销售2": "sales_contact_2",
    "销售2": "sales_contact_2",
    "2销售": "sales_contact_2",
    "第二今日事项": "today_task_2",
    "第二事项": "today_task_2",
    "今日事项2": "today_task_2",
    "事项2": "today_task_2",
    "2事项": "today_task_2",
    "第二问题描述": "today_task_2",
    "问题描述2": "today_task_2",
    "第二完成内容": "completed_actions_2",
    "第二完成": "completed_actions_2",
    "完成内容2": "completed_actions_2",
    "完成2": "completed_actions_2",
    "2完成": "completed_actions_2",
    "第二处理情况": "completed_actions_2",
    "处理情况2": "completed_actions_2",
    "第二处理结果": "result_status_2",
    "处理结果2": "result_status_2",
    "结果2": "result_status_2",
    "远程服务支持": "remote_support",
    "远程支持": "remote_support",
    "远程": "remote_support",
    "明日工作计划": "tomorrow_plan",
    "明日安排": "tomorrow_plan",
    "明日计划": "tomorrow_plan",
    "明日": "tomorrow_plan",
    "明天": "tomorrow_plan",
    "计划": "tomorrow_plan",
    "感想": "reflection",
    "心得": "reflection",
}
DEFAULT_SALES_MEMORY_PATH = Path("~/.config/auto-daytexy/daily_report_sales_memory.json").expanduser()
FXIAOKE_HISTORY_RECORDABLE_STATUSES = {
    "submitted",
    "already_exists",
    "voided",
    "invalidated",
    "deleted",
    "cancelled",
    "作废",
    "已作废",
}
PRIVATE_FXIAOKE_PROFILE_DIR = Path("~/.config/auto-daytexy/fxiaoke_browser_profile").expanduser()
DEFAULT_FXIAOKE_PROFILE_DIR = PRIVATE_FXIAOKE_PROFILE_DIR
DEFAULT_FXIAOKE_SUBPROCESS_TIMEOUT_SECONDS = 900
SERVICE_RECORD_SUBPROCESS_TIMEOUT_SECONDS = 240
# The child's own post-submit budget is ~16s nominal (click settle + 3s surface
# confirm + 5s history readiness + 8s history query) plus browser teardown, so a
# 30s backstop can interrupt verification and leave a published daily log
# marked "uncertain". Keep roughly 2x headroom.
DAILY_LOG_POST_SUBMIT_WATCHDOG_SECONDS = 60
DAILY_LOG_POST_SUBMIT_MARKER = "[纷享销客日报] 4/4"
DAILY_LOG_CONFIRMATION_DONE_MARKER = "[纷享销客日报/耗时] 发布确认"
FXIAOKE_SUBMISSION_INTENT_DIRNAME = "fxiaoke_submission_intents"
FXIAOKE_SUBPROCESS_TERMINATE_GRACE_SECONDS = 3
PENDING_SAFE_RETRY_MAX_AGE_DAYS = 14


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="将工作碎碎念转换为正式日报，并写入日报文件。",
        epilog="如需接入纷享销客，请设置 FXIAOKE_ENABLED=1。"
    )
    mode_group = parser.add_mutually_exclusive_group()
    parser.add_argument("input_text", nargs="*", help="直接传入今天的工作摘要。")
    parser.add_argument("--refresh-local-metadata", action="store_true", help="仅从已保存日报补办销售/周报附加更新，不生成或提交CRM。")
    mode_group.add_argument(
        "--quick",
        action="store_true",
        help="使用速记模式逐项输入；交互模式下默认启用。",
    )
    mode_group.add_argument(
        "--freeform",
        action="store_true",
        help="使用自由描述输入。",
    )
    mode_group.add_argument(
        "--editor-input",
        action="store_true",
        help="使用编辑器输入完整摘要。",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过确认，直接写入日报文件。",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成并打印，不写入文件。",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="生成完成后不复制到剪贴板。",
    )
    parser.add_argument(
        "--path",
        dest="report_path",
        help="覆盖默认日报路径。",
    )
    parser.add_argument(
        "--no-fxiaoke",
        action="store_true",
        help="生成日报后跳过已启用的纷享销客联动。",
    )
    parser.add_argument(
        "--shared-session",
        action="store_true",
        help="交互提交时，日报与外勤主服务记录复用一次浏览器会话（macOS 默认开启）。",
    )
    parser.add_argument(
        "--shared-remote-session",
        action="store_true",
        help="显式让已授权远程工单复用日报/外勤浏览器会话；默认关闭。",
    )
    parser.add_argument(
        "--fxiaoke-profile-dir",
        help="指定纷享销客浏览器 Profile，默认使用本机配置目录。",
    )
    parser.add_argument(
        "--reset-fxiaoke-login",
        action="store_true",
        help="清理纷享销客登录态缓存和默认 Profile 后退出。",
    )
    parser.add_argument(
        "--fxiaoke-login-check",
        action="store_true",
        help="只检查/刷新纷享销客登录态，不生成日报、不提交数据。",
    )
    parser.add_argument(
        "--fxiaoke-doctor",
        action="store_true",
        help="检查纷享销客浏览器自动化环境，不生成日报、不提交数据。",
    )
    parser.add_argument(
        "-b",
        "--retry-daily-log",
        action="store_true",
        help="直接从已保存的日报文件中补交纷享销客日报，不重新生成日报。",
    )
    parser.add_argument(
        "--sync-saved-report",
        action="store_true",
        help="扫描已保存日报后，按交互授权补交日报、外勤服务和远程工单。",
    )
    parser.add_argument(
        "--retry-pending",
        action="store_true",
        help="一键重试待补交队列中的纷享销客日报/服务记录，成功后自动清理。",
    )
    parser.add_argument(
        "--review-pending",
        action="store_true",
        help="只查看待补交队列，不提交、不清理任何记录。",
    )
    parser.add_argument("--customer", default="", help="重试队列时只处理此客户的失败项（完整名称）。")
    parser.add_argument("--details", action="store_true", help="展开结果、差异和失败留痕。")
    parser.add_argument(
        "--date",
        dest="report_date",
        help="配合 -b 或 --sync-saved-report 读取指定日期的已保存日报，支持 YYYY-MM-DD 或 YYYY.MM.DD。",
    )
    return parser.parse_args(argv)


def get_api_key():
    return get_env_value("DAILY_REPORT_API_KEY", "SILICONFLOW_API_KEY") or load_api_key_from_credentials(
        "daily_report_api_key",
        "siliconflow_api_key",
        "api_key",
    )


def resolve_sales_memory_path():
    env_path = get_env_value("DAILY_REPORT_SALES_MEMORY_PATH")
    if env_path:
        return Path(os.path.expandvars(os.path.expanduser(env_path)))
    return DEFAULT_SALES_MEMORY_PATH


def get_editor_command():
    for env_name in ("VISUAL", "EDITOR"):
        editor_value = os.environ.get(env_name, "").strip()
        if not editor_value:
            continue

        editor_cmd = shlex.split(editor_value)
        if not editor_cmd:
            continue

        editor_path = shutil.which(editor_cmd[0])
        if editor_path:
            editor_cmd[0] = editor_path
            return editor_cmd

    if os.name == "nt":
        return ["notepad.exe"]

    for editor_name in ("nano", "vim", "vi"):
        editor_path = shutil.which(editor_name)
        if editor_path:
            return [editor_path]

    return None


def normalize_structured_label(label):
    return re.sub(r"\s+", "", label).lower()


NORMALIZED_STRUCTURED_ALIASES = {
    normalize_structured_label(alias): field
    for alias, field in STRUCTURED_FIELD_ALIASES.items()
}


def join_structured_value(existing, addition):
    existing = existing.strip()
    addition = addition.strip()
    if not addition:
        return existing
    if not existing:
        return addition
    separator = "\n" if "\n" in existing or "\n" in addition else "；"
    return f"{existing}{separator}{addition}"


def parse_structured_brief_input(text):
    normalized_fields = {field: "" for field in STRUCTURED_FIELD_ORDER}
    matched_labels = 0
    current_field = None

    segments = []
    for raw_line in text.replace("\r\n", "\n").splitlines():
        if not raw_line.strip():
            segments.append("")
            continue
        segments.extend(re.split(r"[；;]+", raw_line))

    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            current_field = None
            continue

        match = re.match(r"^([^：:]+)[：:](.*)$", segment)
        if match:
            field = NORMALIZED_STRUCTURED_ALIASES.get(
                normalize_structured_label(match.group(1))
            )
            if field:
                normalized_fields[field] = join_structured_value(
                    normalized_fields[field],
                    match.group(2).strip(),
                )
                current_field = field
                matched_labels += 1
                continue

        if current_field:
            normalized_fields[current_field] = join_structured_value(
                normalized_fields[current_field],
                segment,
            )

    parsed_fields = {
        field: value.strip()
        for field, value in normalized_fields.items()
        if value.strip()
    }
    if "is_onsite" in parsed_fields:
        parsed_fields["is_onsite"] = normalize_onsite_value(parsed_fields["is_onsite"])
    if "work_type" in parsed_fields:
        parsed_fields["work_type"] = normalize_work_type(parsed_fields["work_type"])
    if matched_labels < 2:
        return None
    return parsed_fields


def build_structured_brief_text(fields):
    return "\n".join(
        f"{STRUCTURED_FIELD_LABELS[field]}：{fields[field].strip()}"
        for field in STRUCTURED_FIELD_ORDER
        if fields.get(field, "").strip()
    )


def normalize_customer_key(customer_name):
    return re.sub(r"\s+", "", customer_name.strip())


CUSTOMER_SEARCH_SUFFIXES = (
    "健康职业技术学院",
    "职业技术学院",
    "科学技术学院",
    "中等专业学校",
    "中等职业学校",
    "技术学院",
    "职业学院",
    "职业学校",
    "师范学院",
    "警察学院",
    "科技学院",
    "学院",
    "大学",
    "学校",
    "监狱",
    "一中",
    "中学",
    "小学",
)


CUSTOMER_SEARCH_TAILS = (
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
)


def build_preflight_customer_keyword(customer_name):
    cleaned = normalize_customer_key(customer_name)
    cleaned = re.sub(r"[\s:：,，;；.。()（）\-_—·]+", "", cleaned)
    for tail in CUSTOMER_SEARCH_TAILS:
        if cleaned.endswith(tail) and len(cleaned) > len(tail) + 2:
            cleaned = cleaned[: -len(tail)]
            break
    core = re.sub(r"[省市县区]", "", cleaned)
    for suffix in CUSTOMER_SEARCH_SUFFIXES:
        if core.endswith(suffix) and len(core) > len(suffix) + 2:
            core = core[: -len(suffix)]
            break
    return core if len(core) >= 5 else cleaned


def load_sales_memory(memory_path=None, *, strict=False):
    path = memory_path or resolve_sales_memory_path()
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("期望 JSON 对象")
        mappings = data.get("sales_mappings", data)
        if not isinstance(mappings, dict):
            raise ValueError("sales_mappings 必须是对象")
    except Exception as exc:
        if strict:
            raise RuntimeError(f"销售记忆文件无法读取，已保留原文件：{path}") from exc
        sys.stderr.write(f"[警告] 销售记忆文件读取失败 ({path})：{exc}\n")
        return {}

    normalized = {}
    for key, value in mappings.items():
        if isinstance(value, dict):
            customer = value.get("customer", key)
            sales_contact = value.get("sales_contact", "")
        else:
            customer = key
            sales_contact = value

        customer = str(customer or "").strip()
        sales_contact = str(sales_contact or "").strip()
        if customer and sales_contact:
            normalized[normalize_customer_key(customer)] = {
                "customer": customer,
                "sales_contact": sales_contact,
            }
    return normalized


def save_sales_memory(mappings, memory_path=None):
    path = memory_path or resolve_sales_memory_path()
    payload = {
        "sales_mappings": {
            key: {
                "customer": value["customer"],
                "sales_contact": value["sales_contact"],
            }
            for key, value in sorted(mappings.items())
        }
    }
    write_text_atomically(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def remember_sales_mapping(customer_name, sales_contact, memory_path=None):
    customer = customer_name.strip()
    sales = sales_contact.strip()
    if not customer or not sales:
        return

    mappings = load_sales_memory(memory_path, strict=True)
    mappings[normalize_customer_key(customer)] = {
        "customer": customer,
        "sales_contact": sales,
    }
    save_sales_memory(mappings, memory_path)


def build_sales_history_candidates(file_path):
    if not file_path.exists():
        return {}

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    candidates: dict[str, dict[str, Any]] = {}
    for section in split_report_sections(content):
        for parsed in parse_report_service_entries(section) or [parse_report_fields(section)]:
            customer = parsed.get("服务客户", "").strip()
            sales_contact = parsed.get("对应销售", "").strip()
            if not customer or not sales_contact:
                continue

            customer_key = normalize_customer_key(customer)
            bucket = candidates.setdefault(
                customer_key,
                {"customer": customer, "sales_contacts": []},
            )
            if sales_contact not in bucket["sales_contacts"]:
                bucket["sales_contacts"].append(sales_contact)
    return candidates


def resolve_sales_contact(customer_name, report_path, memory_path=None):
    customer = customer_name.strip()
    if not customer:
        return "", ""

    customer_key = normalize_customer_key(customer)
    memory_mappings = load_sales_memory(memory_path)
    remembered = memory_mappings.get(customer_key)
    if remembered:
        return remembered["sales_contact"], "本地记忆"

    history_candidates = build_sales_history_candidates(report_path)
    history_match = history_candidates.get(customer_key)
    if history_match and len(history_match["sales_contacts"]) == 1:
        return history_match["sales_contacts"][0], "历史日报"

    return "", ""


def enrich_structured_fields(fields, report_path, memory_path=None):
    enriched = dict(fields)
    if enriched.get("is_onsite", "").strip():
        enriched["is_onsite"] = normalize_onsite_value(enriched["is_onsite"])
    if enriched.get("work_type", "").strip():
        enriched["work_type"] = normalize_work_type(enriched["work_type"])
    customer = enriched.get("service_customer", "").strip()
    if customer and not enriched.get("sales_contact", "").strip():
        sales_contact, source = resolve_sales_contact(customer, report_path, memory_path)
        if sales_contact:
            enriched["sales_contact"] = sales_contact
            enriched["_sales_source"] = source
    second_customer = enriched.get("service_customer_2", "").strip()
    if second_customer and not enriched.get("sales_contact_2", "").strip():
        sales_contact, source = resolve_sales_contact(second_customer, report_path, memory_path)
        if sales_contact:
            enriched["sales_contact_2"] = sales_contact
            enriched["_sales_source_2"] = source
    return enriched


def build_structured_service_entries(structured_fields):
    if not structured_fields:
        return []

    field_groups = [
        {
            "服务客户": structured_fields.get("service_customer", ""),
            "对应销售": structured_fields.get("sales_contact", ""),
            "问题描述": structured_fields.get("today_task", ""),
            "处理结果": structured_fields.get("result_status", ""),
            "处理情况": structured_fields.get("completed_actions", ""),
        },
        {
            "服务客户": structured_fields.get("service_customer_2", ""),
            "对应销售": structured_fields.get("sales_contact_2", ""),
            "问题描述": structured_fields.get("today_task_2", ""),
            "处理结果": structured_fields.get("result_status_2", ""),
            "处理情况": structured_fields.get("completed_actions_2", ""),
        },
    ]
    entries = []
    for fields in field_groups:
        if any(str(value or "").strip() for value in fields.values()):
            entries.append({label: str(value or "").strip() for label, value in fields.items()})
    return entries


EDITOR_INPUT_PLACEHOLDER = "请在这里写今天的工作内容，保存后关闭编辑器。"


def _extract_editor_input_content(content):
    content = str(content or "").strip()
    if content.startswith(EDITOR_INPUT_PLACEHOLDER):
        content = content.replace(EDITOR_INPUT_PLACEHOLDER, "", 1)
    return content.strip()


def _read_report_input_from_editor(launch_editor):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="daily_report_",
            delete=False,
        ) as temp_file:
            temp_file.write(f"{EDITOR_INPUT_PLACEHOLDER}\n")
            temp_path = temp_file.name

        launch_editor(temp_path)

        with open(temp_path, "r", encoding="utf-8") as temp_file:
            return _extract_editor_input_content(temp_file.read())
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def read_editor_report_input():
    editor_cmd = get_editor_command()
    if not editor_cmd:
        return read_brief_input("\n请输入今天的工作摘要（回车确认）：\n> ")

    print("\n将打开编辑器填写今天的工作摘要。")
    print("保存并关闭编辑器后，脚本会自动继续。")

    editor_name = os.path.basename(editor_cmd[0]).lower()
    if editor_name == "notepad.exe":
        print("记事本使用说明：输入完成后直接保存并关闭窗口即可。")
    elif editor_name == "nano":
        print("nano 使用说明：Ctrl+O 保存，回车确认，Ctrl+X 退出。")
    elif editor_name in {"vim", "vi"}:
        print("vim 使用说明：按 i 输入，完成后按 Esc，再输入 :wq 并回车。")

    return _read_report_input_from_editor(
        lambda temp_path: subprocess.run(editor_cmd + [temp_path], check=False)
    )


def read_macos_clipboard_report_input():
    print("\n请先复制完整工作摘要，再回到此处按回车读取剪贴板。")
    print("不需要将长文本粘贴进 Terminal；输入 q 可取消。")
    while True:
        try:
            action = read_brief_input("复制完成后按回车：").strip().lower()
        except EOFError:
            return ""
        if action in {"q", "quit", "取消"}:
            return ""
        content = read_clipboard_text()
        if content:
            return content
        print("[提示] 剪贴板没有可用文本，请重新复制后再试。")


def read_required_brief_input(prompt_text, empty_message):
    while True:
        value = read_brief_input(prompt_text).strip()
        if value:
            return value
        print(empty_message)


def read_quick_report_input(report_path, structured_fields=None, draft_root=None):
    from .quick_drafts import QuickDraftError, begin_quick_draft_session, clear_quick_draft, load_quick_draft, save_quick_draft

    print("\n进入速记模式，直接输入关键词即可，留空可跳过。")
    print("示例客户为示例实验室，今日事项可填机房部署，完成内容可填部署进展。")
    if is_apple_terminal():
        print(f"[输入] 直接在命令行填写中文，回车确认；也可输入 {APPLE_TERMINAL_CLIPBOARD_COMMAND} 读取已复制文本。")

    # Direct noninteractive callers keep their existing prompt contract. The CLI
    # enters this function only on a terminal; tests can supply a private root.
    persist_draft = draft_root is not None or sys.stdin.isatty()
    draft_day = date.today().isoformat()
    begin_quick_draft_session(report_path, draft_day)
    draft_fields = {}
    recovered = {}
    if persist_draft:
        damaged = False
        try:
            saved = load_quick_draft(report_path, draft_root, draft_day)
        except (QuickDraftError, OSError) as exc:
            print(f"[草稿] {exc}")
            saved, damaged = {}, True
        if saved is not None:
            print("[草稿] 发现本日报今天的未完成输入。")
            while True:
                prompt = "草稿操作（D 删除；N 本次忽略并保留原文件）：" if damaged else "草稿操作（R 恢复；N 本次忽略；D 删除）："
                choice = read_brief_input(prompt).strip().lower()
                if choice in {"r", "恢复"} and not damaged:
                    draft_fields = dict(saved)
                    recovered = dict(saved)
                    print("[草稿] 各项回车保留原值，直接输入可修改，输入 /clear 可清空。")
                    break
                if choice in {"n", "忽略"}:
                    persist_draft = False
                    print("[草稿] 本次不会覆盖原草稿；本次新输入不自动保存。")
                    break
                if choice in {"d", "删除"}:
                    clear_quick_draft(report_path, draft_root, draft_day)
                    print("[草稿] 已删除，开始重新填写。")
                    break
                print("[提示] 请选择恢复、忽略或删除。" if not damaged else "[提示] 损坏草稿需选择忽略或明确删除。")

    def autosave():
        nonlocal persist_draft
        if persist_draft:
            try:
                save_quick_draft(report_path, draft_fields, draft_root, draft_day)
            except (QuickDraftError, OSError, ValueError) as exc:
                persist_draft = False
                print(f"[草稿] 自动保存失败：{exc}。本次继续输入，已有草稿保留。")

    def read_field(field, prompt_text, default="", normalize=None, required=False):
        previous = recovered.pop(field, None)
        if previous is not None:
            print(f"[草稿原值] {previous or '（空）'}")
            prompt_text = prompt_text.rstrip("：") + "（回车保留；/clear 清空）："
        while True:
            raw = read_brief_input(prompt_text).strip()
            value = "" if raw == "/clear" else (raw or (previous if previous is not None else default))
            if required and not value:
                previous = ""
                draft_fields[field] = ""
                autosave()
                print("[提示] 明日安排不能为空，请填写后继续。")
                continue
            value = normalize(value) if normalize else value
            draft_fields[field] = value
            autosave()
            return value

    answers = {}
    old_customer = draft_fields.get("service_customer")
    customer_name = read_field("service_customer", "客户/学校：")
    if customer_name:
        answers["service_customer"] = customer_name
    if old_customer is not None and old_customer != customer_name:
        recovered.pop("sales_contact", None)
        draft_fields.pop("sales_contact", None)
        autosave()

    answers["is_onsite"] = read_field(
        "is_onsite", "是否外勤（默认是，填否则按远程服务记录提交）：", normalize=normalize_onsite_value
    )
    answers["work_type"] = read_field(
        "work_type", "工作类型（实施/售后/POC，默认售后）：", normalize=normalize_work_type
    )

    auto_sales, source = resolve_sales_contact(customer_name, report_path)
    if auto_sales:
        print(f"[提示] 已根据{source}匹配对应销售：{auto_sales}")
        sales_prompt = f"对应销售（回车使用：{auto_sales}）："
    else:
        sales_prompt = "对应销售（未知可留空）："
    sales_contact = read_field("sales_contact", sales_prompt, default=auto_sales)
    if sales_contact:
        answers["sales_contact"] = sales_contact

    prompt_definitions = [
        ("today_task", "今日事项："),
        ("completed_actions", "完成内容："),
        ("result_status", "处理结果（默认已完成）："),
    ]
    for field, prompt_text in prompt_definitions:
        value = read_field(field, prompt_text)
        if value:
            answers[field] = value

    second_customer_choice = read_field(
        "_second_customer_choice",
        "是否有第二个服务客户？（y 添加；n 继续填写远程支持；回车跳过第二客户）：",
    ).lower()
    should_continue_common_fields = second_customer_choice in {"n", "no", "否"}
    if second_customer_choice in {"y", "yes", "是"}:
        old_second_customer = draft_fields.get("service_customer_2")
        second_customer = read_field("service_customer_2", "第二客户/学校：")
        if second_customer:
            answers["service_customer_2"] = second_customer
        if old_second_customer is not None and old_second_customer != second_customer:
            recovered.pop("sales_contact_2", None)
            draft_fields.pop("sales_contact_2", None)
            autosave()

        auto_sales, source = resolve_sales_contact(second_customer, report_path)
        if auto_sales:
            print(f"[提示] 已根据{source}匹配第二对应销售：{auto_sales}")
            sales_prompt = f"第二对应销售（回车使用：{auto_sales}）："
        else:
            sales_prompt = "第二对应销售（未知可留空）："
        second_sales = read_field("sales_contact_2", sales_prompt, default=auto_sales)
        if second_sales:
            answers["sales_contact_2"] = second_sales
        for field, prompt_text in [
            ("today_task_2", "第二今日事项："),
            ("completed_actions_2", "第二完成内容："),
            ("result_status_2", "第二处理结果（默认已完成）："),
        ]:
            value = read_field(field, prompt_text)
            if value:
                answers[field] = value
        should_continue_common_fields = True
    else:
        for field in ("service_customer_2", "sales_contact_2", "today_task_2", "completed_actions_2", "result_status_2"):
            draft_fields.pop(field, None)
            recovered.pop(field, None)
        autosave()

    if should_continue_common_fields:
        value = read_field("remote_support", "远程支持：")
        if value:
            answers["remote_support"] = value
    else:
        draft_fields.pop("remote_support", None)
        autosave()

    answers["tomorrow_plan"] = read_field("tomorrow_plan", "明日计划（必填）：", required=True)
    if "result_status" not in answers:
        answers["result_status"] = "已完成"
    if answers.get("service_customer_2") and "result_status_2" not in answers:
        answers["result_status_2"] = "已完成"
    if structured_fields is not None:
        structured_fields.update(answers)
    return build_structured_brief_text(answers)


def read_report_input(args, structured_fields=None):
    if args.input_text:
        return " ".join(args.input_text).strip()

    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    if args.editor_input:
        if is_apple_terminal():
            return read_brief_input("\n请输入今天的工作摘要（回车确认）：\n> ")
        return read_editor_report_input()
    if args.freeform:
        if is_apple_terminal():
            return read_brief_input("\n请输入今天的工作摘要（回车确认）：\n> ")
        return read_brief_input("\n请输入今天的工作摘要（回车确认）：\n> ")
    return read_quick_report_input(
        resolve_report_path(args.report_path), structured_fields=structured_fields
    )


def build_today_string(now=None):
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    current = now or datetime.now()
    return f"{current.strftime('%Y.%m.%d')}（{weekdays[current.weekday()]}）"


def get_recent_context(file_path, limit=DEFAULT_REPORT_HISTORY_LIMIT, exclude_date=None):
    if not file_path.exists():
        return "暂无历史参考数据。"

    try:
        content = file_path.read_text(encoding="utf-8")
        sections = split_report_sections(content)
        if exclude_date:
            # Re-running the same day must not feed the saved version back to
            # the model; otherwise it copies the current report and polish
            # becomes a no-op.
            target_date = str(exclude_date)[:10]
            sections = [
                section for section in sections
                if not extract_report_date(section).startswith(target_date)
            ]
        if not sections:
            return "暂无历史参考数据。"
        return "\n\n".join(sections[-limit:])
    except Exception as exc:
        return f"读取历史失败：{exc}"


def normalize_problem_description(value):
    cleaned = value.strip(" ：:")
    if re.match(r"^\d+[：:]", cleaned):
        return cleaned
    if cleaned and not cleaned.startswith("关于"):
        return f"关于{cleaned}"
    return cleaned


def normalize_result_status(value):
    cleaned = value.strip()
    if not cleaned:
        return ""

    normalized = cleaned.replace(" ", "").strip("；;。！! ")
    done_markers = {
        "已完成", "完成", "已处理", "已解决", "解决完成", "处理完成", "已关闭",
    }
    # Completion must be an explicit status, not a substring of a negative or
    # qualified sentence. The CLI's accepted default is already explicit.
    return "已完成" if normalized in done_markers else "进行中"


def ensure_trailing_semicolon(value):
    # Report parsers treat labels at line starts as structure. Keep a field's
    # continuation text on its own value line when rendering the formal report.
    cleaned = "；".join(
        line.strip().rstrip("；;") for line in value.splitlines() if line.strip()
    ).rstrip("；;")
    if not cleaned:
        return ""
    # A sentence that already ends with terminal punctuation must not become
    # "。；"; the extra separator is only needed for unpuncutated field text.
    if cleaned.endswith(("。", "！", "？", ".", "!", "?")):
        return cleaned
    return f"{cleaned}；"


def split_remote_support_items(value):
    cleaned = value.strip()
    if not cleaned:
        return []
    cleaned = re.sub(r"(?<!^)(?<!\n)(\d+[：:])", r"\n\1", cleaned)

    items = []
    current_item = ""
    for raw_line in cleaned.replace("\r\n", "\n").splitlines():
        line = raw_line.strip().strip("；;")
        if not line:
            continue

        numbered_match = re.match(r"^\d+[：:]\s*(.*)$", line)
        if numbered_match:
            if current_item:
                items.append(current_item.strip())
            current_item = numbered_match.group(1).strip()
            continue

        if current_item:
            current_item = join_structured_value(current_item, line)
            continue

        for part in re.split(r"[；;]+", line):
            text = part.strip()
            if text:
                items.append(text)

    if current_item:
        items.append(current_item.strip())

    items = [item for item in items if item]
    # Deduplicate while preserving order: skip items whose normalized form
    # (lowercased, whitespace-collapsed) already appeared.
    seen = set()
    deduped = []
    for item in items:
        key = re.sub(r"\s+", "", item.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def remember_sales_mapping_from_report(report_text, memory_path=None):
    service_entries = parse_report_service_entries(report_text)
    if service_entries:
        for entry in service_entries:
            remember_sales_mapping(
                entry.get("服务客户", ""),
                entry.get("对应销售", ""),
                memory_path=memory_path,
            )
        return

    parsed = parse_report_fields(report_text)
    remember_sales_mapping(parsed.get("服务客户", ""), parsed.get("对应销售", ""), memory_path=memory_path)


def normalize_service_entry_fields(entry):
    normalized = dict(entry)
    normalized["问题描述"] = ensure_trailing_semicolon(
        normalize_problem_description(normalized.get("问题描述", ""))
    )
    normalized["处理结果"] = normalize_result_status(normalized.get("处理结果", ""))
    normalized["处理情况"] = ensure_trailing_semicolon(normalized.get("处理情况", ""))
    return normalized


def render_service_entry(entry, index):
    normalized = normalize_service_entry_fields(entry)
    lines = []
    if index == 1:
        lines.append("服务支持工作")
    else:
        lines.append(f"{index}：服务支持工作")
    lines.extend(
        [
            f"服务客户：{' '.join(normalized.get('服务客户', '').splitlines()).strip()}",
            f"对应销售：{' '.join(normalized.get('对应销售', '').splitlines()).strip()}",
            f"问题描述：{normalized.get('问题描述', '')}",
            f"处理结果：{normalized.get('处理结果', '')}",
            f"处理情况：{normalized.get('处理情况', '')}",
        ]
    )
    return lines


def render_multi_service_report(date_line, structured_fields):
    service_entries = build_structured_service_entries(structured_fields) or [{}]
    lines = [date_line, "今日总结："]
    for index, entry in enumerate(service_entries, start=1):
        lines.extend(render_service_entry(entry, index))

    lines.extend(["", "远程服务支持："])
    remote_items = split_remote_support_items(structured_fields.get("remote_support", ""))
    if remote_items:
        lines.extend(
            f"{index}：{ensure_trailing_semicolon(item)}"
            for index, item in enumerate(remote_items, start=1)
        )
    lines.extend(["", f"明日安排：{ensure_trailing_semicolon(structured_fields.get('tomorrow_plan', ''))}"])
    return "\n".join(lines).strip()


# Characters that may appear in a light rewrite without introducing a fact.
# Function words, tense/plan words, pronouns (their written-form swaps such as
# "我们" -> "我方" must not count as lost content) and the formal fillers the
# prompt allows.
_POLISH_FUNCTION_CHARS = frozenset(
    "的了在是将于尚仍把被为和与及或但而且并同时已就都还也很中上下内外部"
    "前后来去到对从向给让使由等待"
    "我你他她它们"
)
_POLISH_REMOVABLE_CHARS = frozenset("相关处状态正之后会")


def _normalize_polish_certainty(value):
    # "等…之后" and "待…后" are the same future/plan wording; normalise them
    # before comparing high-risk markers so safe rewrites are not rejected.
    value = re.sub(r"已确认", "确认", value)
    value = re.sub(r"故障", "问题", value)
    value = re.sub(r"明天", "明日", value)
    value = re.sub(r"无法|并非|非", "〈不〉", value)
    value = re.sub(r"等待|待|等|再", "〈待〉", value)
    value = re.sub(r"之后|以后", "〈后〉", value)
    return value


def _polish_content_coverage(source, proposed):
    """Share of source content characters retained by the rewrite."""
    from collections import Counter

    def content_counter(value):
        value = re.sub(r"[\s，,；;。！!：:]", "", value)
        return Counter(
            char for char in value
            if char not in _POLISH_FUNCTION_CHARS
            and char not in _POLISH_REMOVABLE_CHARS
            and "\u4e00" <= char <= "\u9fff"
        )

    source_counter = content_counter(source)
    total = sum(source_counter.values())
    if not total:
        return 1.0
    proposed_counter = content_counter(proposed)
    retained = sum(
        min(count, proposed_counter.get(char, 0))
        for char, count in source_counter.items()
    )
    return retained / total


# Certainty/conclusion wording: adding or dropping one of these between the
# source and the rewrite changes the factual claim, so both directions fail.
# "确认" is checked separately: it may surface a judgement the source already
# states ("不是软件问题" -> "确认非软件问题"), but never invent one. "故障"
# is normalised to "问题" instead of being listed.
_POLISH_CERTAINTY_MARKERS = (
    "可能", "疑似", "尚未", "未确认", "不确定", "待", "未", "不", "没有",
    "计划", "预计", "明日", "确定", "根因",
    "解决", "修复", "恢复", "验证", "通过", "验收", "硬件",
)
# Reference-switch wording: introducing "其他/重新/再次" points the sentence at
# a different object or an extra occurrence, which is a new fact.
_POLISH_REFERENCE_MARKERS = ("其他", "另外", "其余", "别的", "重新", "再次")
# Completion wording for the "completed actions" field: quick notes there are
# terse verb phrases whose completed tense is implicit, so a formal rewrite may
# surface it ("更换" -> "完成更换"). Dropping it would still hide a fact, so
# only the addition direction is tolerated there.
_POLISH_COMPLETION_MARKERS = (
    "完成", "成功", "升级", "安装", "部署", "替换", "更换", "排除",
)


def _polish_reject_reason(original, candidate, *, field_is_completed_actions=False):
    """Why the rewrite is refused, or None when it is safe to accept.

    Accept formal rewording while rejecting detectable factual changes.
    Fact safety lives in the identifier and certainty-marker checks: numbers,
    English identifiers, and hedging/confirmation wording must survive intact.
    The remaining gates only guard against a wholesale replacement with an
    unrelated text, so legitimate written-style rewrites can pass.
    """
    from difflib import SequenceMatcher

    source = str(original or "").strip().strip("；;。 ")
    proposed = str(candidate or "").strip().strip("；;。 ")
    if not source or not proposed:
        return "改写为空"
    source = re.sub(r"^关于", "", source)
    proposed = re.sub(r"^关于", "", proposed)
    identifiers = lambda value: re.findall(r"[A-Za-z]+[A-Za-z0-9._/-]*|\d+(?:\.\d+)?", value.casefold())
    if sorted(identifiers(source)) != sorted(identifiers(proposed)):
        return "改写改变了数字或英文标识"
    # "今日" is a harmless present-day introduction, unlike a changed plan.
    source_certainty = _normalize_polish_certainty(source)
    proposed_certainty = _normalize_polish_certainty(proposed)
    if any(
        (word in source_certainty) != (word in proposed_certainty)
        for word in _POLISH_CERTAINTY_MARKERS + _POLISH_REFERENCE_MARKERS
    ):
        return "改写改变了确定程度或结论表述"
    if "确认" in proposed_certainty and "确认" not in source_certainty:
        # Surfacing "确认" is only safe when the source already states its
        # judgement (a negation or the word itself); otherwise it invents one.
        if "〈不〉" not in source_certainty:
            return "改写新增了原文没有的确认性结论"
    if field_is_completed_actions:
        if any(
            word in source_certainty and word not in proposed_certainty
            for word in _POLISH_COMPLETION_MARKERS
        ):
            return "改写弱化或丢失了完成状态表述"
    elif any(
        (word in source_certainty) != (word in proposed_certainty)
        for word in _POLISH_COMPLETION_MARKERS
    ):
        return "改写改变了完成状态表述"
    source_key = re.sub(r"[\s，,；;。！!：:]", "", source)
    proposed_key = re.sub(r"[\s，,；;。！!：:]", "", proposed)
    # A formal rewrite adds connectives and fills abbreviations, so the fields
    # may legitimately grow; only an unrelated replacement with a completely
    # different shape is rejected here.
    short_source = len(source_key) <= 8
    min_ratio = 0.3 if short_source else 0.4
    if SequenceMatcher(None, source_key, proposed_key).ratio() < min_ratio:
        return "改写与原文差异过大"
    length_ratio = len(proposed_key) / len(source_key)
    max_length_ratio = 3.5 if short_source else 3.0
    if length_ratio < 0.85:
        return "改写丢失了原文内容"
    if length_ratio > max_length_ratio:
        return "改写篇幅超出原文过多"
    # Nearly every content character of the source must survive: swapping a
    # product name ("多媒体" -> "杀毒") or dropping an identity word ("职大")
    # both show up as lost content characters here. A rewrite that grows the
    # text may additionally trade terse verbs for written synonyms ("看了" ->
    # "排查"), so its bar is slightly lower; a shrinking rewrite gets the
    # strict one because shrinking is how dropped facts usually appear.
    min_coverage = 0.75 if length_ratio >= 1.0 else 0.85
    if _polish_content_coverage(source, proposed) < min_coverage:
        return "改写丢失了原文内容"
    return None


def _accept_structured_polish(original, candidate, *, field_is_completed_actions=False):
    """Accept the rewrite only when no factual drift is detectable."""
    return _polish_reject_reason(
        original, candidate, field_is_completed_actions=field_is_completed_actions
    ) is None


def _polish_wording_key(value):
    cleaned = re.sub(r"^关于\s*", "", str(value or "").strip())
    return re.sub(r"[\s，,。；;：:！!？?、]+", "", cleaned)


def _join_polish_lines(value):
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if any(re.split(r"[：:]", line, maxsplit=1)[0].strip() in REPORT_FIELD_ALIASES for line in lines[1:]):
        return "；".join(line.rstrip("；;") for line in lines)
    return " ".join(lines)


def _polish_structured_locally(value):
    text = _join_polish_lines(value)
    rules = (
        (r"协同(集成商|校方|客户|厂家|厂商)完成", r"与\1共同完成"),
        (r"已完成(部分|[0-9]+台)\s*IP\s*冲突机器修改\s*IP(?=[，。；,;！？]|$)",
         r"已为\1存在 IP 冲突的设备调整 IP 地址"),
        (r"经过测试[，,]?无任何问题(?=[，。；,;！？]|$)", "经测试，未发现问题"),
        (r"(^|[，。；,;！？]\s*)现残留(?=[^，。；,;！？]+问题)", r"\1目前仍存在"),
        (r"已沟通相关(厂家|厂商)配合排查", r"已联系相关\1协助排查"),
        (r"后续协同排查处理(?=[，。；,;！？]|$)", "后续将协同排查并处理"),
    )
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def _render_structured_polish(report_text, today_str, structured_fields):
    """Use model wording only for fields bound to the original customer."""
    original_entries = build_structured_service_entries(structured_fields) or [{}]
    candidates = parse_report_service_entries(report_text) if report_text else []
    identity = lambda value: re.sub(r"\s+", "", str(value or "")).casefold()
    by_customer: dict[str, list[dict[str, str]]] = {}
    for entry in candidates:
        by_customer.setdefault(identity(entry.get("服务客户")), []).append(entry)
    customer_keys = [identity(entry.get("服务客户")) for entry in original_entries]
    customer_labels = re.findall(r"(?m)^服务客户[：:]", str(report_text or ""))
    reliable = (len(candidates) == len(original_entries) == len(customer_labels)
                and len(set(customer_keys)) == len(customer_keys))
    updated = dict(structured_fields)
    notices = []
    for index, entry in enumerate(original_entries):
        matches = by_customer.get(customer_keys[index], []) if reliable else []
        candidate = matches[0] if len(matches) == 1 else {}
        suffix = "_2" if index else ""
        for label, key in (("问题描述", "today_task"), ("处理情况", "completed_actions")):
            source = updated.get(key + suffix, "")
            proposed = candidate.get(label, "")
            reason = _polish_reject_reason(
                source, proposed, field_is_completed_actions=key == "completed_actions"
            )
            if reason is None and _polish_wording_key(source) != _polish_wording_key(proposed):
                wording = _join_polish_lines(proposed)
                updated[key + suffix] = re.sub(r"^关于", "", wording.strip()) if key == "today_task" else wording
                continue
            local = _polish_structured_locally(source)
            updated[key + suffix] = local
            if source and report_text:
                field_name = f"第{index + 1}位客户{label}" if index else label
                if _polish_wording_key(source) != _polish_wording_key(local):
                    notices.append(f"{field_name}：{reason or '模型未改动措辞'}，已改用本地润色")
                elif reason:
                    notices.append(f"{field_name}：{reason}，已沿用原文")
    for notice in notices:
        emit_terminal_message(f"[润色] {notice}。")
    return render_multi_service_report(today_str, updated)


def render_polish_diff_preview(report_text, structured_fields):
    """Show fields whose wording changed, whether polished by AI or locally."""
    if not structured_fields:
        return
    original_entries = build_structured_service_entries(structured_fields)
    final_entries = parse_report_service_entries(report_text) if report_text else []
    if len(final_entries) != len(original_entries):
        # Structure cannot be aligned reliably; the full report is printed
        # right after anyway, so skip the side-by-side view.
        return
    normalize = lambda value: re.sub(r"^关于", "", str(value or "").strip().strip("；;。 "))
    changed = []
    for index, original in enumerate(original_entries):
        final = final_entries[index]
        suffix = f"（第{index + 1}位客户）" if index else ""
        for label in ("问题描述", "处理情况"):
            source = normalize(original.get(label, ""))
            polished = normalize(final.get(label, ""))
            if _polish_wording_key(source) != _polish_wording_key(polished):
                changed.append((label + suffix, source, polished))
    if not changed:
        print("[润色对照] 本次未改动任何字段内容（仅排版不计入）。")
        return
    print(f"[润色对照] {len(changed)} 个字段已润色：")
    for label, source, polished in changed:
        print(f"{label}：")
        print(f"  原文：{source}")
        print(f"  改写：{polished}")


def normalize_report_text(report_text, today_str, structured_fields=None):
    if structured_fields:
        return _render_structured_polish(report_text, today_str, structured_fields)

    content = strip_markdown_noise(canonicalize_report_block(report_text))
    if not content:
        raise RuntimeError("AI 未返回有效日报内容。")

    normalized_sections = {label: "" for label in SECTION_LABELS}
    current_label = None
    date_line = today_str
    freeform_lines = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line == "---":
            continue

        if REPORT_DATE_PATTERN.match(line):
            current_label = None
            continue

        match = re.match(r"^([^：:]+)[：:](.*)$", line)
        if match:
            label = REPORT_FIELD_ALIASES.get(match.group(1).strip())
            value = match.group(2).strip()
            if label in normalized_sections:
                normalized_sections[label] = value
                current_label = label
                continue

        if current_label:
            previous = normalized_sections[current_label]
            normalized_sections[current_label] = "\n".join(
                part for part in (previous, line) if part
            )
        else:
            freeform_lines.append(line)

    if freeform_lines and not normalized_sections["处理情况"]:
        normalized_sections["处理情况"] = "；".join(freeform_lines).strip("；")

    normalized_sections["今日总结"] = "服务支持工作"
    normalized_sections["问题描述"] = normalize_problem_description(
        normalized_sections["问题描述"]
    )
    normalized_sections["处理结果"] = normalize_result_status(
        normalized_sections["处理结果"]
    )
    normalized_sections["问题描述"] = ensure_trailing_semicolon(
        normalized_sections["问题描述"]
    )
    normalized_sections["处理情况"] = ensure_trailing_semicolon(
        normalized_sections["处理情况"]
    )
    normalized_sections["明日安排"] = ensure_trailing_semicolon(
        normalized_sections["明日安排"]
    )

    lines = [
        date_line,
        "今日总结：",
        normalized_sections["今日总结"],
        f"服务客户：{normalized_sections['服务客户'].strip()}",
        f"对应销售：{normalized_sections['对应销售'].strip()}",
        f"问题描述：{normalized_sections['问题描述']}",
        f"处理结果：{normalized_sections['处理结果']}",
        f"处理情况：{normalized_sections['处理情况']}",
        "",
        "远程服务支持：",
    ]

    remote_items = split_remote_support_items(normalized_sections["远程服务支持"])
    if remote_items:
        lines.extend(
            f"{index}：{ensure_trailing_semicolon(item)}"
            for index, item in enumerate(remote_items, start=1)
        )
    lines.extend(
        [
            "",
            f"明日安排：{normalized_sections['明日安排']}",
        ]
    )
    return "\n".join(lines).strip()


def build_prompt(
    user_input,
    file_path,
    structured_fields=None,
    structured_fields_prepared=False,
):
    today_str = build_today_string()
    history = get_recent_context(file_path, exclude_date=today_str)
    if structured_fields is None:
        structured_fields = parse_structured_brief_input(user_input)
    if structured_fields and not structured_fields_prepared:
        structured_fields = enrich_structured_fields(structured_fields, file_path)
    if structured_fields:
        input_guidance = """
### 输入说明
用户这次提供的是【结构化速记】，按原始事实整理模板。
事实准确优先于篇幅：
1. 原始字段是唯一事实来源，改写只重组已提供的信息，不得新增或改变事实。
2. “今日事项”对应“问题描述”，“完成内容”对应“处理情况”。
3. 保留原文的确定程度、否定、计划与完成时态，不补写“已完成”或“按计划推进”。
4. 不要编造未提到的客户、地点、结果或完成内容。
5. 如果某项确实没有提供，对应字段可以留空。
6. “是否外勤”只是流程控制字段，不要输出到最终日报。
7. “工作类型”也只是统计和周报前瞻表字段，不要输出到最终日报。
8. 在保持事实、客户名、数字、时间、完成状态、否定和计划完全不变的前提下，对“问题描述”和“处理情况”做正式书面化改写：不要逐字复制原始口语，应当调整语序、合并重复表达、补充连接词，把口语速记整理成通顺完整的书面句子；不得新增事实、结论或未提供的内容，也不得省略原文已有的事实信息（如主语、对象、归属）。
9. 逐字段保留每次出现的数字、英文缩写及标识（如 IP、VDI），即使同一个缩写出现两次也须保留两次。分别保留已处理部分、验证结果、未解决部分及后续安排。每个字段用一行输出，不能把自动换行当作断句。
改写示例（供体会改写幅度，不要照抄内容）：
原速记：远程看了，不是我们软件问题，是他们机器硬件问题。集成商叫我们上门一起查，我提了收费，等他们回复
书面化：经远程排查，该问题并非我方软件问题，而是对方机器硬件问题所致。集成商提出希望我方上门协助排查，我方已提出相关收费事宜，等待对方回复
""".strip()
        raw_input_section = build_structured_brief_text(structured_fields)
    else:
        input_guidance = """
### 输入说明
用户这次提供的是自由描述。请在不编造事实的前提下，整理为正式日报。
""".strip()
        raw_input_section = user_input

    return f"""
你是一个严格的文本转换工具。请将【原始碎碎念】转换为【正式日报】。

### 核心写作规范（事实准确优先于篇幅）
1. 标题日期必须是 {today_str}。
2. “问题描述”必须以“关于”开头。
3. “处理结果”统一写“已完成”或“进行中”。
4. “处理情况”直接描述动作、过程和结果，不要写“通过排查”“沟通后确认”等空话。
5. 历史参考仅用于格式，不提供本次事实；不得借用历史客户、结论或语气。
6. “远程服务支持”必须单独成段，格式为：
远程服务支持：
1：[原文远程支持内容，不额外添加“已解决”]；
7. “今日总结”必须严格输出为两行：
今日总结：
服务支持工作
8. 最后一项必须是“明日安排：”，不要输出“明日工作计划：”。
9. 不得强化或弱化原文的确定程度：“可能/疑似/尚未确认”不能改为“已确认/确定根因”，已证实的结论也不得改成猜测。
10. 保留否定和时间边界：“未恢复/待验证/计划执行”不能写成“已恢复/已验证/已完成”；同一段中不同问题的结论分别保留。

{input_guidance}

### 格式禁止
- 不要加粗
- 不要使用项目符号
- 不要使用 Markdown 标题
- 不要输出解释，只输出最终日报内容
- 不要输出 `---`
- 不要增加“感想”等额外字段

### 强制标准模板
{today_str}
今日总结：
服务支持工作
服务客户：
对应销售：
问题描述：关于...；
处理结果：
处理情况：...；

远程服务支持：
1：...；

明日安排：...；

### 历史参考样本
{history}

### 原始碎碎念
{raw_input_section}

### 本次输出要求
在保持事实、客户名、数字、时间、完成状态、否定和计划完全不变的前提下，对“问题描述”和“处理情况”做正式书面化改写：参考历史参考样本的表达风格，把口语速记整理成通顺完整的书面句子，不要逐字复制原始口语；不得新增事实、结论或未提供的内容，也不得省略原文已有的事实信息（如主语、对象、归属）。

正式日报内容（只输出填充后的模板，不要解释）：
""".strip()


def generate_report(
    user_input,
    file_path,
    structured_fields=None,
    structured_fields_prepared=False,
):
    if structured_fields is None:
        structured_fields = parse_structured_brief_input(user_input)
    if structured_fields and not structured_fields_prepared:
        structured_fields = enrich_structured_fields(structured_fields, file_path)

    requested_date = build_today_string()
    try:
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("未配置 API Key。请设置环境变量 DAILY_REPORT_API_KEY。")
        client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0)
        try:
            response = request_chat_completion(client, {
                "messages": [{"role": "user", "content": build_prompt(
                    user_input, file_path, structured_fields=structured_fields,
                    structured_fields_prepared=True,
                )}],
                **build_chat_completion_options(temperature=0.2, max_tokens=DAILY_REPORT_MAX_TOKENS),
            }, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        finally:
            client.close()
        if not getattr(response, "choices", None):
            raise RuntimeError("AI 服务未返回可用结果。")
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise RuntimeError("AI 日报内容被截断，未重复请求；请减少本次输入长度。")
        content = extract_message_content(response.choices[0].message.content)
        return normalize_report_text(content, requested_date, structured_fields=structured_fields)
    except Exception:
        if not structured_fields:
            raise
        emit_terminal_message("[润色] AI 润色未完成，已改用本地整理预览。")
        return _render_structured_polish("", requested_date, structured_fields)


def update_weekly_preview(report_text, report_path, structured_fields=None):
    parsed = parse_report_fields(report_text)
    service_entries = parse_report_service_entries(report_text)
    if service_entries:
        parsed["服务客户"] = "、".join(
            entry.get("服务客户", "").strip()
            for entry in service_entries
            if entry.get("服务客户", "").strip()
        )
        parsed["对应销售"] = "、".join(
            entry.get("对应销售", "").strip()
            for entry in service_entries
            if entry.get("对应销售", "").strip()
        )
        parsed["问题描述"] = "；".join(
            entry.get("问题描述", "").strip().strip("；;")
            for entry in service_entries
            if entry.get("问题描述", "").strip()
        )
        parsed["处理情况"] = "；".join(
            entry.get("处理情况", "").strip().strip("；;")
            for entry in service_entries
            if entry.get("处理情况", "").strip()
        )
    report_date = extract_report_date(report_text)
    entry = build_preview_entry(report_date, parsed, structured_fields=structured_fields)
    json_path = resolve_preview_json_path(report_path)
    markdown_path = resolve_preview_markdown_path(report_path)
    upsert_preview_entry(entry, json_path, markdown_path)


def update_report_auxiliaries(report_text, report_path, memory_path=None, structured_fields=None):
    """Update rebuildable metadata while keeping classification conflicts fatal."""
    warnings = []
    try:
        remember_sales_mapping_from_report(report_text, memory_path=memory_path)
    except (OSError, RuntimeError, ValueError) as exc:
        warnings.append(f"销售记忆更新失败：{exc}")
    try:
        update_weekly_preview(report_text, report_path, structured_fields=structured_fields)
    except OSError as exc:
        # The preview writer has already validated the merge. Still establish
        # that a later child can determine onsite status from the saved source.
        try:
            is_onsite_workday(report_text=report_text, report_path=report_path)
        except RuntimeError as classification_error:
            raise RuntimeError(f"日报正文已保存，但外勤归属无法确认：{classification_error}；CRM尚未开始。") from classification_error
        warnings.append(f"周报前瞻更新失败：{exc}")
    except PreviewDataError as exc:
        if (structured_fields or {}).get("is_onsite") not in {"是", "否"}:
            raise RuntimeError(f"日报正文已保存，但附加数据无法读取且没有明确外勤选择：{exc}；CRM尚未开始。") from exc
        warnings.append(f"周报前瞻数据未更新，原文件已保留，本次按明确外勤选择继续：{exc}")
    except RuntimeError as exc:
        raise RuntimeError(f"日报正文已保存，但周报前瞻存在读取或业务分类问题：{exc}；请先核对，CRM尚未开始。") from exc
    for warning in warnings:
        emit_terminal_message(f"[附加更新] 日报正文已保存；{warning}。修正文件问题后可运行 rb --refresh-local-metadata --date {normalize_report_date_to_iso(report_text)} 补办。")
    return warnings


def save_report(report_text, file_path, memory_path=None, structured_fields=None):
    report_block = canonicalize_report_block(report_text)
    if not report_block:
        raise RuntimeError("日报内容为空，无法写入文件。")

    report_date = extract_report_date(report_block)
    if not report_date:
        raise RuntimeError("日报缺少有效日期，无法写入文件。")
    date_key = report_date[:10]
    existing_sections = []
    existing_content = ""
    if file_path.exists():
        existing_content = file_path.read_text(encoding="utf-8")
        existing_sections = split_report_sections(existing_content)

    if sum(extract_report_date(section)[:10] == date_key for section in existing_sections) > 1:
        raise RuntimeError(f"日报文件中 {date_key} 存在重复记录，请先核对；原文件未修改。")

    updated = False
    merged_sections = []
    for section in existing_sections:
        if extract_report_date(section)[:10] == date_key and not updated:
            merged_sections.append(report_block)
            updated = True
            continue
        merged_sections.append(section)

    if not updated:
        merged_sections.append(report_block)

    merged_content = "\n\n".join(merged_sections) + "\n"
    if merged_content != existing_content:
        write_text_atomically(file_path, merged_content)
    update_report_auxiliaries(
        report_block, file_path, memory_path=memory_path, structured_fields=structured_fields,
    )
    return "更新" if updated else "新增"


def resolve_fxiaoke_profile_dir(profile_dir=None):
    value = str(profile_dir or "").strip() or get_env_value("FXIAOKE_PROFILE_DIR")
    if value:
        return value.strip()
    return str(DEFAULT_FXIAOKE_PROFILE_DIR)


def resolve_fxiaoke_submission_intent_dir(profile_dir=None):
    profile_dir = profile_dir or resolve_fxiaoke_profile_dir()

    profile_path = Path(profile_dir).expanduser()
    try:
        profile_path.resolve().relative_to(PROJECT_ROOT.resolve())
        runtime_dir = PRIVATE_FXIAOKE_PROFILE_DIR.parent
    except ValueError:
        runtime_dir = profile_path.parent
    return runtime_dir / FXIAOKE_SUBMISSION_INTENT_DIRNAME


def resolve_fxiaoke_submission_intent_path(report_text, step_key, profile_dir=None):
    intent_dir = resolve_fxiaoke_submission_intent_dir(profile_dir)
    if intent_dir is None:
        return None
    report_date = normalize_report_date_to_iso(report_text)
    safe_step = re.sub(r"[^a-z0-9_-]+", "_", str(step_key).lower()).strip("_")
    return intent_dir / f"{safe_step}_{report_date}.json"


class SubmissionEvidenceError(RuntimeError):
    """Stop further submissions if the original intent cannot retain evidence."""

    status = "evidence_error"
    retry_action = "verify_crm"


@contextmanager
def _fxiaoke_intent_lock(intent_path):
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    with intent_path.with_suffix(".lock").open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reserve_fxiaoke_submission_intent(report_text, step_key, profile_dir=None, *, report_path=None, inherited_token="", evidence_context=None):
    path = resolve_fxiaoke_submission_intent_path(report_text, step_key, profile_dir)
    if evidence_context is not None:
        evidence_context.clear()
    if path is None:
        return None, None
    with _fxiaoke_intent_lock(path):
        existing, owned_path = _reserve_fxiaoke_submission_intent(
            report_text, step_key, profile_dir, report_path=report_path, inherited_token=inherited_token,
        )
        if existing is not None and "evidence" not in existing and path.exists():
            try:
                retained = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                retained = {}
            if isinstance(retained, dict) and retained.get("evidence"):
                existing = {**existing, "evidence": retained["evidence"]}
        if existing is None and evidence_context is not None:
            intent = json.loads(path.read_text(encoding="utf-8"))
            evidence_context.update({key: intent[key] for key in (
                "step", "report_date", "report_digest", "owner_pid", "owner_token",
            )})
            evidence_context["path"] = str(path)
        return existing, owned_path


def _reserve_fxiaoke_submission_intent(report_text, step_key, profile_dir=None, *, report_path=None, inherited_token=""):
    if report_path is not None:
        report_date = normalize_report_date_to_iso(report_text)
        for task in load_pending_fxiaoke_tasks(report_path):
            if (
                build_fxiaoke_pending_task_key(task)[0] == step_key
                and str(task.get("report_date", "")).replace(".", "-") == report_date
                and classify_fxiaoke_pending_task(task)[0] != "safe_retry"
            ):
                return task, None
    intent_path = resolve_fxiaoke_submission_intent_path(
        report_text,
        step_key,
        profile_dir=profile_dir,
    )
    if intent_path is None:
        return None, None

    intent = {
        "step": step_key,
        "report_date": normalize_report_date_to_iso(report_text),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "owner_pid": os.getpid(),
        "owner_token": secrets.token_hex(16),
        "report_digest": hashlib.sha256(canonicalize_report_block(report_text).encode("utf-8")).hexdigest(),
    }
    if inherited_token and not intent_path.exists():
        raise RuntimeError("父进程提交意图已失效，已阻止纷享销客提交。")
    try:
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        with intent_path.open("x", encoding="utf-8") as handle:
            json.dump(intent, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            existing = json.loads(intent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if (
            inherited_token and isinstance(existing, dict)
            and existing.get("owner_pid") == os.getppid()
            and secrets.compare_digest(str(existing.get("owner_token", "")).encode(), str(inherited_token).encode())
            and existing.get("step") == step_key
            and existing.get("report_date") == intent["report_date"]
            and existing.get("report_digest") == intent["report_digest"]
        ):
            return None, None
        return existing if isinstance(existing, dict) else {}, intent_path
    except OSError as exc:
        raise RuntimeError("无法创建本机提交意图，已阻止纷享销客提交。") from exc
    return None, intent_path


def checkpoint_fxiaoke_submission(context, *, expected_customers=None, child_index=None, event=None, fields=None, result=None, verified_recovery=False):
    if not context:
        return
    try:
        path = Path(context["path"])
        with _fxiaoke_intent_lock(path):
            intent = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(intent, dict) or any(intent.get(key) != context[key] for key in (
                "owner_pid", "owner_token", "step", "report_date", "report_digest",
            )):
                raise ValueError("original intent identity changed")
            evidence = intent.setdefault("evidence", {})
            if "expected_customers" in context and evidence.get("expected_customers") != context["expected_customers"]:
                raise ValueError("authorized child coverage changed")
            if expected_customers is not None:
                customers = list(expected_customers)
                if not customers or any(not isinstance(value, str) for value in customers):
                    raise ValueError("invalid child coverage")
                if evidence and evidence.get("expected_customers") != customers:
                    raise ValueError("child coverage changed")
                evidence.setdefault("expected_customers", customers)
                evidence.setdefault("children", {})
            customers = evidence["expected_customers"]
            children = evidence["children"]
            if child_index is not None:
                if type(child_index) is not int or not 0 <= child_index < len(customers):
                    raise ValueError("invalid child index")
                child = children.setdefault(str(child_index), {"service_customer": customers[child_index], "milestones": {}})
                if child.get("service_customer") != customers[child_index] or not isinstance(child.get("milestones"), dict):
                    raise ValueError("child evidence changed")
                routing = fields if event == "target_selected" else result
                if routing and routing.get("routing_target") is not None:
                    from integrations.fxiaoke.onsite_routing import (
                        IMPLEMENTATION_TYPE, select_after_sales_workorder, assert_target_matches,
                    )
                    keys = ("work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source")
                    frozen = {key: routing.get(key) for key in keys}
                    if (context["step"] != "service_record" or frozen["work_type"] != "售后"
                            or frozen["routing_policy"] != "implementation_to_latest_after_sales_v1"
                            or not isinstance(frozen["routing_source"], dict)
                            or frozen["routing_source"].get("record_type") != IMPLEMENTATION_TYPE
                            or frozen["routing_source"].get("workorder_id") != frozen["source_checkin_workorder_id"]):
                        raise ValueError("invalid onsite routing authorization")
                    select_after_sales_workorder(frozen["routing_source"], [frozen["routing_target"]], context["report_date"])
                    if child.get("routing"):
                        if any(child["routing"].get(key) != frozen[key] for key in keys):
                            raise ValueError("frozen routing target/source changed")
                        assert_target_matches(child["routing"]["routing_target"], frozen["routing_target"])
                    child["routing"] = json.loads(json.dumps(frozen))
                if event == "target_selected" and not child.get("routing"):
                    raise ValueError("target selection requires complete durable routing evidence")
                if child.get("routing"):
                    target_id = child["routing"]["routing_target"]["workorder_id"]
                    if any(item.get("workorder_id") not in (None, "", target_id) for item in (fields or {}, result or {})):
                        raise ValueError("receipt does not belong to frozen routing target")
                    target_customer = re.sub(r"\s+", "", child["routing"]["routing_target"]["customer_name"]).casefold()
                    if any(item.get("crm_customer") and re.sub(r"\s+", "", str(item["crm_customer"])).casefold() != target_customer
                           for item in (fields or {}, result or {})):
                        raise ValueError("receipt customer does not match frozen routing department")
                for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"):
                    identities = [source.get(key) for source in (
                        child, child.get("result", {}), result or {}, fields or {},
                    ) if source.get(key)]
                    if identities and any(value != identities[0] for value in identities[1:]):
                        raise ValueError("captured CRM identity changed")
                if result is not None:
                    if result.get("step", context["step"]) != context["step"]:
                        raise ValueError("result stage changed")
                    if result.get("service_customer", customers[child_index]) != customers[child_index]:
                        raise ValueError("result customer changed")
                    if "result" in child and child["result"] != result:
                        previous = child["result"]
                        can_recover = (
                            verified_recovery and context["step"] == "daily_log"
                            and event == "crm_id_captured"
                            and previous.get("ok") is False
                            and previous.get("status") in {"uncertain", "timeout", "crm_id_captured"}
                            and result.get("ok") is True and result.get("status") == "submitted"
                            and result.get("feed_id")
                            and any(child["milestones"].get(mark) is True
                                    for mark in ("submit_clicked", "crm_id_captured"))
                        )
                        if not can_recover:
                            raise ValueError("confirmed result cannot be replaced")
                    if "result" in child and not event:
                        return
                    child["result"] = dict(result)
                if event:
                    child["milestones"][event] = True
                    for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"):
                        if (fields or {}).get(key) and event != "target_selected":
                            child[key] = fields[key]
            evidence["complete"] = all("result" in children.get(str(index), {}) for index in range(len(customers)))
            write_text_atomically(path, json.dumps(intent, ensure_ascii=False))
            # Persist the rename as well as the file contents before unrelated work.
            if os.name == "posix":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            context.setdefault("expected_customers", list(customers))
    except Exception as exc:
        raise SubmissionEvidenceError(f"本机提交证据保存失败，意图已保留，禁止自动重试：{exc}") from exc


def clear_fxiaoke_submission_intent(intent_path, *, report_text=None):
    if intent_path is None:
        return False
    path = Path(intent_path)
    try:
        with _fxiaoke_intent_lock(path):
            intent = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(intent, dict) or intent.get("owner_pid") != os.getpid()
                or not isinstance(intent.get("owner_token"), str) or not intent["owner_token"]
                or path.name != f"{intent.get('step')}_{intent.get('report_date')}.json"
            ):
                return False
            if report_text is not None and not fxiaoke_submission_evidence_complete(path, report_text):
                return False
            path.unlink()
            return True
    except (OSError, ValueError, TypeError):
        return False


def finish_fxiaoke_direct_submission(report_text, report_path, step_key, payload, intent_path):
    if intent_path is None:
        return payload
    children = payload.get("results") or [payload]
    try:
        record_fxiaoke_submission_history(report_text, report_path, {**payload, "step": step_key})
        for item in children:
            if item.get("status") in {"submitted", "already_exists"}:
                kwargs = {"service_customer": item.get("service_customer")} if step_key != "daily_log" else {}
                if step_key == "remote_workorder":
                    kwargs["includes_main_work"] = item.get("includes_main_work")
                clear_pending_fxiaoke_tasks(report_path, step_key=step_key, report_date=normalize_report_date_to_iso(report_text), **kwargs)
        if any(item.get("status") not in {"submitted", "already_exists", "skipped", "cancelled"} for item in children):
            record_pending_fxiaoke_task(report_text, report_path, step_key, step_key, payload)
        if all(
            item.get("status") in {"submitted", "already_exists", "skipped", "cancelled"}
            or item.get("retry_action") == "safe_retry"
            for item in children
        ):
            if not clear_fxiaoke_submission_intent(intent_path, report_text=report_text):
                raise SubmissionEvidenceError("本机提交证据未完整闭环，请先只读核对。")
    except Exception as exc:
        payload["local_recording_error"] = str(exc)
        payload["message"] = str(payload.get("message", "")) + f"；本机留痕未完成，提交意图已保留：{exc}"
        if isinstance(exc, SubmissionEvidenceError):
            payload.update(ok=False, status="uncertain", retry_action="verify_crm")
    return payload


def fxiaoke_submission_evidence_complete(intent_path, report_text):
    try:
        intent = json.loads(Path(intent_path).read_text(encoding="utf-8"))
        if (
            not isinstance(intent, dict)
            or intent.get("owner_pid") != os.getpid()
            or not isinstance(intent.get("owner_token"), str) or not intent["owner_token"]
            or Path(intent_path).name != f"{intent.get('step')}_{intent.get('report_date')}.json"
            or intent.get("report_date") != normalize_report_date_to_iso(report_text)
            or intent.get("report_digest") != hashlib.sha256(canonicalize_report_block(report_text).encode("utf-8")).hexdigest()
        ):
            return False
        if "evidence" not in intent:
            return True
        evidence = intent["evidence"]
        customers = evidence["expected_customers"]
        children = evidence["children"]
        if not isinstance(customers, list) or not customers or not isinstance(children, dict):
            return False
        for index, customer in enumerate(customers):
            child = children.get(str(index), {})
            result = child.get("result", {})
            if (
                not isinstance(customer, str) or child.get("service_customer") != customer
                or type(result.get("ok")) is not bool
                or result.get("step", intent["step"]) != intent["step"]
                or result.get("service_customer", customer) != customer
                or any(child.get(key) and result.get(key) and child[key] != result[key]
                       for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"))
            ):
                return False
            if result["ok"] and result.get("status") in {"submitted", "already_exists"}:
                continue
            if (child.get("milestones", {}).get("submit_clicked")
                or child.get("milestones", {}).get("crm_id_captured")
                or result.get("submission_phase") in {"submit_clicked", "crm_id_captured"}
                or result.get("service_record_id") or result.get("feed_id")
                or (intent["step"] == "remote_workorder" and result.get("workorder_id"))):
                return False
            if result["ok"] and result.get("status") in {"skipped", "cancelled"}:
                continue
            if result["ok"] or result.get("status") not in {"error", "blocked", "auth_expired"} or classify_fxiaoke_pending_task(result)[0] != "safe_retry":
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def list_fxiaoke_submission_intents(profile_dir=None):
    intent_dir = resolve_fxiaoke_submission_intent_dir(profile_dir)
    if intent_dir is None or not intent_dir.exists():
        return []

    intents = []
    for intent_path in sorted(intent_dir.glob("*.json")):
        try:
            payload = json.loads(intent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["path"] = str(intent_path)
        intents.append(payload)
    return intents


def build_fxiaoke_submission_intent_blocked_result(step_key, step_label, intent):
    created_at = str(intent.get("created_at", "未知时间"))
    evidence = intent.get("evidence")
    confirmed: list[str] = []
    unresolved: list[str] = []
    if isinstance(evidence, dict) and isinstance(evidence.get("expected_customers"), list) and isinstance(evidence.get("children"), dict):
        for index, customer in enumerate(evidence["expected_customers"]):
            child = evidence["children"].get(str(index), {})
            if not isinstance(child, dict):
                child = {}
            result = child.get("result", {})
            if not isinstance(result, dict):
                result = {}
            ids = "/".join(str(result.get(key) or child.get(key) or "") for key in ("workorder_id", "service_record_id", "feed_id")).strip("/") or "ID 未取得"
            line = f"{customer or step_label}：{result.get('status', '未完成/未知')}，{ids}"
            (confirmed if result.get("ok") is True and result.get("status") in {"submitted", "already_exists"} else unresolved).append(line)
    details = ("；持久化已确认结果：" + "；".join(confirmed) if confirmed else "") + ("；待只读复核：" + "；".join(unresolved) if unresolved else "")
    return {
        "ok": False,
        "step": step_key,
        "status": "uncertain",
        "message": (
            f"{step_label}：检测到 {created_at} 创建的本机提交意图或待复核记录；"
            "上次执行可能已在 CRM 提交，请先只读核对，禁止自动重试。" + details
        ),
        "artifacts": [],
        "retry_action": "verify_crm",
        "confirmed_evidence": confirmed,
        "unresolved_evidence": unresolved,
    }


def reserve_fxiaoke_submission_intents(report_text, step_keys, profile_dir=None, *, report_path=None):
    intent_paths: dict[str, Path] = {}
    for step_key in step_keys:
        existing_intent, intent_path = reserve_fxiaoke_submission_intent(
            report_text,
            step_key,
            profile_dir=profile_dir,
            report_path=report_path,
        )
        if existing_intent is not None:
            for reserved_path in intent_paths.values():
                clear_fxiaoke_submission_intent(reserved_path)
            return step_key, existing_intent, {}
        if intent_path is not None:
            intent_paths[step_key] = intent_path
    return None, None, intent_paths


def write_authorized_remote_drafts_file(remote_drafts, profile_dir=None):
    from fxiaoke_remote_workorder import serialize_remote_workorder_draft

    intent_dir = resolve_fxiaoke_submission_intent_dir(profile_dir)
    runtime_dir = intent_dir.parent
    drafts_dir = runtime_dir / "fxiaoke_remote_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="authorized_remote_",
        dir=str(drafts_dir),
        delete=False,
    ) as handle:
        json.dump(
            [serialize_remote_workorder_draft(draft) for draft in remote_drafts],
            handle,
            ensure_ascii=False,
        )
        handle.flush()
        os.fsync(handle.fileno())
        draft_path = Path(handle.name)
    try:
        draft_path.chmod(0o600)
    except OSError:
        pass
    return draft_path


def build_fxiaoke_sync_maintenance_command(action, profile_dir=None):
    command = [
        sys.executable,
        str(PROJECT_ROOT / "fxiaoke_sync.py"),
        action,
        "--json",
    ]
    resolved_profile_dir = resolve_fxiaoke_profile_dir(profile_dir)
    if resolved_profile_dir:
        command.extend(["--profile-dir", resolved_profile_dir])
    return command


def build_fxiaoke_command(
    script_name,
    report_text,
    report_path,
    profile_dir=None,
    main_only=False,
    service_customer="",
    work_type="",
):
    report_date_iso = normalize_report_date_to_iso(report_text)
    command = [
        sys.executable,
        str(PROJECT_ROOT / script_name),
        "--date",
        report_date_iso,
        "--path",
        str(report_path),
        "-y",
        "--json",
    ]
    resolved_profile_dir = resolve_fxiaoke_profile_dir(profile_dir)
    if resolved_profile_dir:
        command.extend(["--profile-dir", resolved_profile_dir])
    if main_only and script_name == "fxiaoke_service_record.py":
        command.append("--main-only")
    if service_customer and script_name == "fxiaoke_service_record.py":
        command.extend(["--customer", service_customer])
    if script_name == "fxiaoke_service_record.py":
        selected_type = resolve_report_work_type(
            report_text, report_path,
            {"work_type": work_type} if work_type else None,
            customer_name=service_customer,
        )
        if selected_type:
            command.extend(["--work-type", selected_type])
    intent_step = {"fxiaoke_service_record.py": "service_record", "fxiaoke_daily_log.py": "daily_log"}.get(script_name)
    if intent_step:
        intent_path = resolve_fxiaoke_submission_intent_path(report_text, intent_step, resolved_profile_dir)
        if intent_path is not None and intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if isinstance(intent, dict) and intent.get("owner_pid") == os.getpid() and intent.get("owner_token"):
                command.extend(["--submission-intent-token", intent["owner_token"]])
    return command


def build_fxiaoke_sync_command(
    report_text,
    report_path,
    structured_fields=None,
    profile_dir=None,
    skip_service_record=False,
    remote_drafts_file=None,
):
    report_date_iso = normalize_report_date_to_iso(report_text)
    command = [
        sys.executable,
        str(PROJECT_ROOT / "fxiaoke_sync.py"),
        "--date",
        report_date_iso,
        "--path",
        str(report_path),
        "-y",
        "--json",
    ]
    submit_service = not skip_service_record and is_onsite_workday(
        report_text=report_text, structured_fields=structured_fields, report_path=report_path,
    )
    command.extend(["--onsite-mode", "yes" if submit_service else "no"])
    if submit_service:
        work_type = resolve_report_work_type(report_text, report_path, structured_fields)
        if work_type:
            command.extend(["--work-type", work_type])
    if not submit_service:
        command.append("--skip-service-record")
    resolved_profile_dir = resolve_fxiaoke_profile_dir(profile_dir)
    if resolved_profile_dir:
        command.extend(["--profile-dir", resolved_profile_dir])
    if remote_drafts_file:
        command.extend(["--remote-drafts-file", str(remote_drafts_file)])
    tokens = {}
    for step in ("daily_log", "service_record", "remote_workorder"):
        intent_path = resolve_fxiaoke_submission_intent_path(report_text, step, resolved_profile_dir)
        if intent_path is not None and intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if isinstance(intent, dict) and intent.get("owner_pid") == os.getpid() and intent.get("owner_token"):
                tokens[step] = intent["owner_token"]
    if tokens:
        command.extend(["--submission-intent-tokens", json.dumps(tokens)])
    return command


def build_fxiaoke_preflight_report(report_text, structured_fields=None, report_path=None):
    from fxiaoke_remote_workorder import build_remote_workorder_drafts

    report_date = normalize_report_date_to_iso(report_text)
    parsed = parse_report_fields(report_text)
    service_entries = parse_report_service_entries(report_text)
    customers = extract_report_customer_names(report_text)
    tomorrow_plan = parsed.get("明日安排", "").strip()
    # 已保存日报没有速记字段，外勤归属只能来自周报前瞻表；不传 report_path
    # 会退回正文推断，导致预检显示的外勤/远程条数与实际提交路径不一致。
    is_onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    onsite_count = (len(service_entries) or 1) if is_onsite else 0
    remote_workorder_count = len(
        build_remote_workorder_drafts(report_text, is_onsite=is_onsite)
    )
    lines = [
        f"日期：{report_date}",
        f"客户：{'、'.join(customers) if customers else '未识别'}",
        f"日报：{'可提交' if customers and tomorrow_plan else '需补全'}",
        f"外勤技术服务记录：预计提交 {onsite_count} 条",
        f"远程服务工单：识别到 {remote_workorder_count} 张候选（交互批量总览确认后才提交）",
    ]

    missing = []
    if not customers:
        missing.append("服务客户")
    if not tomorrow_plan:
        missing.append("明日安排")
    entries_to_check = service_entries or [parsed]
    if is_onsite:
        for index, entry in enumerate(entries_to_check, start=1):
            if not entry.get("服务客户", "").strip():
                missing.append(f"第 {index} 个服务客户")
            if not entry.get("处理情况", "").strip():
                missing.append(f"第 {index} 个处理情况")
    if missing:
        lines.append(f"缺失项：{'、'.join(dict.fromkeys(missing))}")
    else:
        lines.append("缺失项：无")

    if customers:
        keyword_lines = [
            f"{customer} -> {build_preflight_customer_keyword(customer)}"
            for customer in customers
        ]
        lines.append(f"CRM 查询关键词：{'；'.join(keyword_lines)}")
    return "\n".join(lines)


def get_fxiaoke_subprocess_timeout_seconds():
    raw_value = os.getenv("FXIAOKE_SUBPROCESS_TIMEOUT_SECONDS", "").strip()
    if not raw_value:
        return DEFAULT_FXIAOKE_SUBPROCESS_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(raw_value)
    except ValueError:
        return DEFAULT_FXIAOKE_SUBPROCESS_TIMEOUT_SECONDS
    return max(60, timeout_seconds)


def parse_fxiaoke_json_payload(stdout):
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def terminate_fxiaoke_process_tree(process):
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            process.kill()
        except (AttributeError, OSError, ProcessLookupError):
            return

    try:
        process.wait(timeout=FXIAOKE_SUBPROCESS_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            process.kill()
        except (AttributeError, OSError, ProcessLookupError):
            pass


def run_fxiaoke_subprocess(command, timeout_seconds=None):
    started_at = time.monotonic()
    resolved_timeout_seconds = (
        get_fxiaoke_subprocess_timeout_seconds()
        if timeout_seconds is None
        else max(60, int(timeout_seconds))
    )
    stdout_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=stdout_file,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=os.name != "nt",
    )
    stderr_chunks = []
    daily_log_post_submit_started_at = None

    def pump_stderr():
        nonlocal daily_log_post_submit_started_at
        if process.stderr is None:
            return
        for raw_line in process.stderr:
            stderr_chunks.append(raw_line)
            line = raw_line.rstrip()
            if DAILY_LOG_POST_SUBMIT_MARKER in line:
                daily_log_post_submit_started_at = time.monotonic()
            elif DAILY_LOG_CONFIRMATION_DONE_MARKER in line:
                daily_log_post_submit_started_at = None
            if line:
                emit_terminal_message(line)

    stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
    stderr_thread.start()

    timed_out = False
    daily_log_confirmation_timed_out = False
    deadline = time.monotonic() + resolved_timeout_seconds
    try:
        while process.poll() is None:
            now = time.monotonic()
            if (
                daily_log_post_submit_started_at is not None
                and now - daily_log_post_submit_started_at
                >= DAILY_LOG_POST_SUBMIT_WATCHDOG_SECONDS
            ):
                daily_log_confirmation_timed_out = True
                terminate_fxiaoke_process_tree(process)
                break
            if now >= deadline:
                timed_out = True
                terminate_fxiaoke_process_tree(process)
                break
            time.sleep(0.2)
        result_code = process.wait()
    except BaseException:
        terminate_fxiaoke_process_tree(process)
        raise
    finally:
        stderr_thread.join(timeout=1)
        if process.stderr is not None and not stderr_thread.is_alive():
            process.stderr.close()
        stdout_file.flush()
        stdout_file.seek(0)
        stdout = stdout_file.read().strip()
        stdout_file.close()

    stderr = "".join(stderr_chunks).strip()
    combined_output = "\n".join(
        chunk for chunk in (stdout, stderr) if chunk
    ).strip()

    if daily_log_confirmation_timed_out:
        return {
            "ok": False,
            "status": "uncertain",
            "message": (
                "纷享销客日报已点击发布，但发布确认超过 "
                f"{DAILY_LOG_POST_SUBMIT_WATCHDOG_SECONDS} 秒未结束；"
                "已终止浏览器子进程。请先只读核对当天日报，禁止直接重提。"
            ),
            "artifacts": [],
            "output": combined_output,
            "command": command,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }

    if timed_out:
        return {
            "ok": False,
            "status": "timeout",
            "message": f"纷享销客自动化超过 {resolved_timeout_seconds} 秒未结束，已终止并记录待补交。",
            "artifacts": [],
            "output": combined_output,
            "command": command,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }

    payload = parse_fxiaoke_json_payload(stdout)

    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        payload = {
            "ok": False,
            "status": "uncertain",
            "message": combined_output or "子进程未返回有效的结构化结果，请先只读核对 CRM。",
            "artifacts": [],
        }

    if payload.get("status") == SubmissionEvidenceError.status:
        payload["ok"] = False
        payload["retry_action"] = SubmissionEvidenceError.retry_action
    if result_code != 0 and payload.get("ok"):
        payload["ok"] = False
        payload["status"] = "uncertain"
    checked = normalize_fxiaoke_result_item(payload)
    payload["ok"] = checked["ok"]
    payload["status"] = checked["status"]
    if checked.get("retry_action"):
        payload["retry_action"] = checked["retry_action"]
    payload["status"] = str(payload.get("status", "submitted" if payload["ok"] else "error"))
    payload["message"] = str(
        payload.get("message")
        or combined_output
        or ("执行成功。" if payload["ok"] else "未返回更多错误信息。")
    )
    artifacts = payload.get("artifacts", [])
    payload["artifacts"] = artifacts if isinstance(artifacts, list) else []
    payload["output"] = combined_output
    payload["command"] = command
    payload["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    return payload


DAILY_LOG_WATCHDOG_UNCERTAIN_MARKER = "发布确认超过"


def reconcile_daily_log_watchdog_uncertain(payload, report_text, report_path, profile_dir=None, *, confirmation_started=False):
    """Finish the read-only check the watchdog interrupted.

    The parent kills the child while the child is still confirming the publish,
    so a published daily log used to stay "uncertain" and forced a manual check
    before any retry was allowed. A fresh read-only history query settles it.
    """
    if payload.get("ok") or payload.get("status") != "uncertain":
        return payload
    if not confirmation_started and DAILY_LOG_WATCHDOG_UNCERTAIN_MARKER not in str(payload.get("message") or ""):
        return payload
    import fxiaoke_daily_log as daily_log

    from fxiaoke_service_record import DEFAULT_STORAGE_STATE_PATH

    try:
        draft = daily_log.build_daily_log_draft(report_text)
        verification = daily_log.run_daily_log_verification(
            draft,
            DEFAULT_STORAGE_STATE_PATH,
            Path(report_path) if report_path else resolve_report_path(None),
            profile_dir=profile_dir,
            cached_login_only=True,
        )
    except Exception as exc:
        payload["message"] = f"{payload.get('message', '')}；自动只读复核未完成：{exc}"
        return payload
    feed_id = verification.get("feed_id")
    valid_feed_id = (
        (type(feed_id) is int and feed_id > 0)
        or (isinstance(feed_id, str) and re.fullmatch(r"[1-9][0-9]*", feed_id) is not None)
    )
    if verification.get("query_completed") is True and verification.get("found") is True and valid_feed_id:
        return {
            **payload,
            "ok": True,
            "status": "submitted",
            "feed_id": feed_id,
            "service_customer": draft.customer_name,
            "_watchdog_verified_customer": draft.customer_name,
            "message": (
                "纷享销客日报：发布确认超时，但自动只读复核已在“我发出的日志”中确认当天日报，"
                f"判定为已提交（feed {verification.get('feed_id')}）。"
            ),
        }
    payload["message"] = (
        f"{payload.get('message', '')}；自动只读复核未确认"
        f"（{verification.get('query_status') or 'unknown'}）：{verification.get('message', '')}"
    )
    return payload


def checkpoint_daily_log_watchdog_reconciliation(context, payload, customer):
    """Bind the read-only receipt to the parent's originally reserved intent."""
    try:
        if not context or context.get("step") != "daily_log" or not isinstance(customer, str) or not customer.strip():
            raise ValueError("original daily-log intent is unavailable")
        intent = json.loads(Path(context["path"]).read_text(encoding="utf-8"))
        evidence = intent.get("evidence", {})
        if evidence.get("expected_customers") != [customer]:
            raise ValueError("authorized daily-log target changed")
        child = evidence["children"]["0"]
        milestones = child.get("milestones", {})
        if not any(milestones.get(event) is True for event in ("submit_clicked", "crm_id_captured")):
            raise ValueError("original daily-log submission was not started")
        receipt = {
            "ok": True, "step": "daily_log", "status": "submitted",
            "feed_id": payload["feed_id"], "service_customer": customer,
        }
        checkpoint_fxiaoke_submission(
            {**context, "expected_customers": [customer]}, child_index=0,
            event="crm_id_captured", fields={"feed_id": payload["feed_id"]}, result=receipt,
            verified_recovery=True,
        )
    except SubmissionEvidenceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SubmissionEvidenceError(f"自动只读复核结果未能绑定原提交证据，保护已保留：{exc}") from exc


def trigger_fxiaoke_daily_log(report_text, report_path, return_payload=False, profile_dir=None):
    payload = run_fxiaoke_subprocess(
        build_fxiaoke_command("fxiaoke_daily_log.py", report_text, report_path, profile_dir=profile_dir)
    )
    payload = reconcile_daily_log_watchdog_uncertain(payload, report_text, report_path, profile_dir=profile_dir)
    if return_payload:
        return payload
    if not payload["ok"]:
        raise RuntimeError(
            "纷享销客日报自动发布失败。\n"
            f"{payload['message']}"
        )
    return payload["message"]


def trigger_fxiaoke_service_record(
    report_text,
    report_path,
    return_payload=False,
    profile_dir=None,
    main_only=False,
    service_customer="",
    work_type="",
):
    payload = run_fxiaoke_subprocess(
        build_fxiaoke_command(
            "fxiaoke_service_record.py",
            report_text,
            report_path,
            profile_dir=profile_dir,
            main_only=main_only,
            service_customer=service_customer,
            **({"work_type": work_type} if work_type else {}),
        ),
        timeout_seconds=SERVICE_RECORD_SUBPROCESS_TIMEOUT_SECONDS,
    )
    if return_payload:
        return payload
    if not payload["ok"]:
        raise RuntimeError(
            "纷享销客技术服务自动填写失败。\n"
            f"{payload['message']}"
        )
    return payload["message"]


def build_fxiaoke_pending_task_key(task):
    step = task.get("step")
    report_date = task.get("report_date")
    if step == "service_record" and (
        task.get("remote_only") or task.get("remote_support_only")
    ):
        return (
            "remote_workorder",
            report_date,
            str(task.get("service_customer", "")).strip(),
            None,
        )
    if step == "remote_workorder":
        role = task.get("includes_main_work")
        return (step, report_date, str(task.get("service_customer", "")).strip(), role if isinstance(role, bool) else None)
    if step != "service_record":
        return (step, report_date)
    return (
        step,
        report_date,
        bool(task.get("main_only")),
        str(task.get("service_customer") or "").strip(),
    )


def build_fxiaoke_pending_service_clear_filters(task):
    return {
        "main_only": bool(task.get("main_only")),
    }


def load_pending_fxiaoke_tasks(report_path):
    queue_path = resolve_fxiaoke_pending_queue_path(report_path)
    tasks = []
    for line_number, raw_line in enumerate(read_pending_queue_text(queue_path).splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"待补交文件第 {line_number} 行格式异常，已停止处理并保留原文件。") from exc
        if not isinstance(item, dict):
            raise RuntimeError(f"待补交文件第 {line_number} 行不是对象，已停止处理并保留原文件。")
        if item.get("step") in {"daily_log", "service_record", "remote_workorder"} and item.get("report_date"):
            tasks.append(item)
    return tasks


def describe_fxiaoke_pending_task_mode(task):
    step = task.get("step")
    if step == "daily_log":
        return "工作圈日报"
    if step == "remote_workorder":
        return "远程服务工单"
    if step != "service_record":
        return "未知"
    if task.get("main_only"):
        return "主服务"
    return "外勤服务记录"


def compact_fxiaoke_pending_message(message, limit=90):
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def pending_task_age_days(task, today=None):
    created_at = str(task.get("created_at", "")).strip()
    if not created_at:
        return None
    try:
        created_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return max(0, ((today or date.today()) - created_date).days)


def classify_fxiaoke_pending_task(task, today=None):
    message = str(task.get("message", "")).strip()
    retry_action = str(task.get("retry_action", "")).strip()
    status = task.get("status")
    phase = task.get("submission_phase")
    if (build_fxiaoke_pending_task_key(task)[0] == "service_record"
            and (task.get("routing_target") is not None or task.get("routing_policy")
                 or task.get("source_checkin_workorder_id"))):
        return "verify_crm", "先按已冻结的售后目标工单只读核对，禁止自动重试或重新选单。"
    if (
        retry_action == "verify_crm"
        or (status is not None and not isinstance(status, str))
        or (phase is not None and not isinstance(phase, str))
        or status in {SubmissionEvidenceError.status, "uncertain", "crm_id_captured", "timeout"}
        or phase in {"submit_clicked", "crm_id_captured"}
        or task.get("service_record_id") or task.get("feed_id")
        or (build_fxiaoke_pending_task_key(task)[0] == "remote_workorder" and task.get("workorder_id"))
    ):
        return "verify_crm", "先在 CRM 中只读核对，禁止自动重试。"
    if retry_action == "confirm_customer":
        return "confirm_customer", "先确认客户归属或重新选择客户，禁止自动重试。"
    if any(
        marker in message
        for marker in (
            "已点击发布",
            "正在等待系统确认",
            "日报发布后",
            "请不要重复发布",
            "结果待确认",
            "只读核对",
            "仅允许继续复核",
            "不允许直接重提",
            "已保留本机提交意图",
        )
    ):
        return "verify_crm", "先在 CRM 中只读核对，禁止自动重试。"
    if "客户" in message and any(
        marker in message
        for marker in ("不匹配", "客户归属", "选择客户", "取消选择")
    ):
        return "confirm_customer", "先确认客户归属或重新选择客户，禁止自动重试。"
    if retry_action == "safe_retry" or "已停止提交" in message:
        age_days = pending_task_age_days(task, today=today)
        if age_days is not None and age_days >= PENDING_SAFE_RETRY_MAX_AGE_DAYS:
            return (
                "review_stale",
                f"待补交已积压 {age_days} 天，超过 {PENDING_SAFE_RETRY_MAX_AGE_DAYS} 天自动重试期限；请人工复核。",
            )
        return "safe_retry", "已确认上次在提交前停止，可自动重试。"
    return "review_failure", "失败位置不明确，请先查看留痕并人工处理，禁止自动重试。"


def build_fxiaoke_pending_alert(report_path, today=None, profile_dir=None):
    tasks = load_pending_fxiaoke_tasks(report_path)
    intents = list_fxiaoke_submission_intents(profile_dir)
    if not tasks and not intents:
        return ""

    mode_counts: dict[str, int] = {}
    crm_review_count = 0
    stale_review_count = 0
    for task in tasks:
        mode = describe_fxiaoke_pending_task_mode(task)
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        action, _ = classify_fxiaoke_pending_task(task, today=today)
        if action in {"verify_crm", "confirm_customer"}:
            crm_review_count += 1
        elif action == "review_stale":
            stale_review_count += 1

    report_dates = []
    for task in tasks:
        try:
            report_dates.append(
                datetime.strptime(str(task.get("report_date", "")), "%Y-%m-%d").date()
            )
        except ValueError:
            continue

    oldest_text = ""
    if report_dates:
        oldest = min(report_dates)
        today_value = today or date.today()
        age_days = max(0, (today_value - oldest).days)
        oldest_text = f"；最早 {oldest.isoformat()}，已积压 {age_days} 天"

    mode_text = "；分类：" + "、".join(
        f"{mode} {count} 条" for mode, count in mode_counts.items()
    )
    crm_text = f"；需 CRM 核对 {crm_review_count} 条" if crm_review_count else ""
    stale_text = f"；超期人工复核 {stale_review_count} 条" if stale_review_count else ""
    intent_labels = {
        "daily_log": "工作圈日报",
        "service_record": "外勤服务记录",
        "remote_workorder": "远程服务工单",
    }
    intent_modes = "、".join(
        intent_labels.get(str(intent.get("step", "")).strip(), "未知")
        for intent in intents
    )
    intent_text = (
        f"；本机提交意图 {len(intents)} 条（{intent_modes}），"
        "先在 CRM 中只读核对，禁止自动重试"
        if intents
        else ""
    )
    return (
        f"[待补交提醒] 当前有 {len(tasks)} 条待补交任务{oldest_text}"
        f"{mode_text if tasks else ''}{crm_text}{stale_text}{intent_text}。"
        "使用 python3 daily_report.py --review-pending 查看。"
    )


def build_fxiaoke_pending_review(report_path, profile_dir=None, details=False):
    queue_path = resolve_fxiaoke_pending_queue_path(report_path)
    tasks = load_pending_fxiaoke_tasks(report_path)
    intents = list_fxiaoke_submission_intents(profile_dir)
    lines = [
        f"待补交文件：{queue_path}",
        f"待补交任务：{len(tasks)} 条",
    ]
    if not tasks:
        lines.append("当前没有待补交任务。")
    else:
        for index, task in enumerate(tasks, 1):
            step_label = str(task.get("step_label", "")).strip()
            if not step_label:
                step_label = "纷享销客日报" if task.get("step") == "daily_log" else "纷享销客技术服务记录"
            customer = str(task.get("service_customer", "")).strip() or "-"
            created_at = str(task.get("created_at", "")).strip() or "-"
            artifacts = task.get("artifacts", [])
            artifacts_count = len(artifacts) if isinstance(artifacts, list) else 0
            lines.append(
                f"{index}. {task.get('report_date')} | {step_label} | "
                f"模式：{describe_fxiaoke_pending_task_mode(task)} | 客户：{customer} | 创建：{created_at}"
            )
            message = compact_fxiaoke_pending_message(task.get("message", ""))
            if message:
                lines.append(f"   摘要：{message}")
            detail_parts = []
            if task.get("status"):
                detail_parts.append(f"状态：{task['status']}")
            if task.get("submission_phase"):
                detail_parts.append(f"阶段：{task['submission_phase']}")
            if task.get("workorder_id"):
                detail_parts.append(f"工单：{task['workorder_id']}")
            if task.get("service_record_id"):
                detail_parts.append(f"服务记录：{task['service_record_id']}")
            if task.get("crm_customer"):
                detail_parts.append(f"CRM客户：{task['crm_customer']}")
            if detail_parts:
                lines.append("   详情：" + " | ".join(detail_parts))
            _, advice = classify_fxiaoke_pending_task(task)
            lines.append(f"   处理：{advice}")
            routing_command = build_fxiaoke_routing_verification_command(task, report_path, profile_dir)
            if routing_command:
                lines.append("   只读核验：" + shlex.join(routing_command))
            if artifacts_count:
                lines.append(f"   留痕：{artifacts_count} 个")
            if details:
                lines.extend(format_fxiaoke_result_details(task))

    if intents:
        lines.append(f"本机提交意图：{len(intents)} 条")
        for intent in intents:
            step = str(intent.get("step", "未知")).strip() or "未知"
            step_label = {
                "daily_log": "工作圈日报",
                "service_record": "外勤服务记录",
                "remote_workorder": "远程服务工单",
            }.get(step, step)
            report_date = str(intent.get("report_date", "未知日期")).strip() or "未知日期"
            created_at = str(intent.get("created_at", "未知时间")).strip() or "未知时间"
            lines.append(
                f"提交意图 | {report_date} | {step_label} | 创建：{created_at}"
                " | 请先在 CRM 中只读核对，禁止自动重试。"
            )
            detail = build_fxiaoke_submission_intent_blocked_result(step, step_label, intent)
            for label, key in (("持久化已确认", "confirmed_evidence"), ("待只读复核", "unresolved_evidence")):
                lines.extend(f"   {label}：{item}" for item in detail[key])
            if details:
                evidence = intent.get("evidence")
                children = evidence.get("children", {}) if isinstance(evidence, dict) else {}
                for child in (children.values() if isinstance(children, dict) else []):
                    if isinstance(child, dict) and isinstance(child.get("result"), dict):
                        lines.extend(format_fxiaoke_result_details(child["result"]))
    return "\n".join(lines)


def build_fxiaoke_routing_verification_command(task, report_path, profile_dir=None):
    """Review an existing frozen target; never create or rewrite queue metadata."""
    target = task.get("routing_target")
    if task.get("step") != "service_record" or not isinstance(target, dict):
        return []
    workorder_id = str(target.get("workorder_id", "")).strip()
    customer = str(task.get("service_customer", "")).strip()
    report_date = str(task.get("report_date", "")).strip()
    if not re.fullmatch(r"JSFW\d{8}-\d+", workorder_id) or not customer:
        return []
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return []
    source = Path(report_path).expanduser().resolve()
    command = [
        sys.executable, str(PROJECT_ROOT / "fxiaoke_service_record.py"),
        "--date", report_date, "--path", str(source), "--customer", customer,
        "--verify-workorder", workorder_id,
        "--routing-pending-file", str(resolve_fxiaoke_pending_queue_path(source)),
        "--json",
    ]
    record_id = str(task.get("service_record_id", "")).strip()
    if record_id:
        if not re.fullmatch(r"FWJL-\d{8}-\d+", record_id):
            return []
        command.extend(["--verify-service-record", record_id])
    if profile_dir:
        command.extend(["--profile-dir", str(profile_dir)])
    return command


def execute_fxiaoke_step(
    report_text,
    report_path,
    step_key,
    step_label,
    trigger_func,
    profile_dir=None,
    trigger_kwargs=None,
    pending_clear_filters=None,
):
    evidence_context: dict[str, Any] = {}
    existing_intent, intent_path = reserve_fxiaoke_submission_intent(
        report_text,
        step_key,
        profile_dir=profile_dir,
        report_path=report_path,
        evidence_context=evidence_context,
    )
    if existing_intent is not None:
        return build_fxiaoke_submission_intent_blocked_result(
            step_key,
            step_label,
            existing_intent,
        )
    last_payload = None
    retried = False
    trigger_kwargs = trigger_kwargs or {}
    for attempt in range(1, 3):
        payload = trigger_func(
            report_text,
            report_path,
            return_payload=True,
            profile_dir=profile_dir,
            **trigger_kwargs,
        )
        if "_watchdog_verified_customer" in payload:
            customer = payload.pop("_watchdog_verified_customer")
            try:
                if step_key != "daily_log" or payload.get("ok") is not True or payload.get("status") != "submitted":
                    raise SubmissionEvidenceError("自动只读复核回执与当前步骤不一致，保护已保留。")
                checkpoint_daily_log_watchdog_reconciliation(evidence_context, payload, customer)
            except SubmissionEvidenceError as exc:
                payload = {**payload, "ok": False, "status": exc.status, "retry_action": exc.retry_action,
                           "message": f"{payload.get('message', '')}；{exc}"}
        has_batch = isinstance(payload.get("results"), list) and bool(payload["results"])
        batch_has_confirmed = False
        batch_terminal = False
        if has_batch and payload.get("status") != SubmissionEvidenceError.status:
            if any(not isinstance(item, dict) for item in payload["results"]):
                raise RuntimeError("同步结果列表存在非对象记录，已停止写入历史。")
            children = [normalize_fxiaoke_result_item(item, default_step=step_key) for item in payload["results"]]
            payload = {**payload, "results": children}
            batch_has_confirmed = any(item["ok"] and item["status"] in {"submitted", "already_exists"} for item in children)
            batch_terminal = all(
                (item["ok"] and item["status"] in {"submitted", "already_exists", "skipped", "preview", "cancelled"})
                or (item["status"] in {"error", "blocked", "auth_expired"} and classify_fxiaoke_pending_task(item)[0] == "safe_retry")
                for item in children
            )
            if any(item["status"] == SubmissionEvidenceError.status for item in children):
                payload = {**payload, "ok": False, "status": SubmissionEvidenceError.status}
            elif any(item["status"] in {"uncertain", "crm_id_captured", "timeout"} for item in children):
                payload = {**payload, "ok": False, "status": "uncertain", "retry_action": "verify_crm"}
            elif any(item["status"] == "auth_expired" for item in children):
                payload = {**payload, "ok": False, "status": "auth_expired"}
            elif not all(item["ok"] for item in children) and payload.get("status") not in {"uncertain", "crm_id_captured", "timeout"}:
                payload = {**payload, "ok": False, "status": "error"}
        if payload.get("status") == SubmissionEvidenceError.status:
            return {
                **payload, "ok": False, "step": step_key,
                "retry_action": SubmissionEvidenceError.retry_action,
                "message": f"{step_label}：{payload.get('message', '')}",
                "artifacts": payload.get("artifacts", []),
            }
        if has_batch:
            record_fxiaoke_submission_history(report_text, report_path, {**payload, "step": step_key})
        if payload.get("ok") is True and payload.get("status", "submitted") in {"submitted", "already_exists"}:
            result = {
                **payload,
                "ok": True,
                "step": step_key,
                "status": payload.get("status", "submitted"),
                "message": f"{step_label}：{payload.get('message', '')}",
                "artifacts": payload.get("artifacts", []),
            }
            if isinstance(payload.get("elapsed_seconds"), (int, float)):
                result["elapsed_seconds"] = float(payload["elapsed_seconds"])
            if not has_batch:
                record_fxiaoke_submission_history(report_text, report_path, result)
            if pending_clear_filters is not None:
                clear_pending_fxiaoke_tasks(
                    report_path, step_key=step_key,
                    report_date=normalize_report_date_to_iso(report_text),
                    **pending_clear_filters,
                )
            if not clear_fxiaoke_submission_intent(intent_path, report_text=report_text):
                return {
                    **result, "ok": False, "status": "uncertain", "retry_action": "verify_crm",
                    "message": result["message"] + "\n本机提交证据未完整闭环，已保留保护，请先只读核对。",
                }
            return result
        last_payload = payload
        if payload.get("status") in {"uncertain", "crm_id_captured", "timeout"}:
            return {
                **payload,
                "ok": False,
                "step": step_key,
                "status": "uncertain",
                "retry_action": "verify_crm",
                "message": f"{step_label}：{payload.get('message', '')}",
                "artifacts": payload.get("artifacts", []),
            }
        retry_action, _ = classify_fxiaoke_pending_task(payload)
        if payload.get("status") == "auth_expired":
            break
        if retry_action != "safe_retry" or (has_batch and (batch_has_confirmed or not batch_terminal)):
            break
        if attempt == 1:
            if not clear_fxiaoke_submission_intent(intent_path, report_text=report_text):
                try:
                    retained = json.loads(Path(intent_path).read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    retained = {}
                if not isinstance(retained, dict):
                    retained = {}
                return build_fxiaoke_submission_intent_blocked_result(step_key, step_label, retained)
            existing_intent, intent_path = reserve_fxiaoke_submission_intent(
                report_text, step_key, profile_dir=profile_dir, report_path=report_path,
                evidence_context=evidence_context,
            )
            if existing_intent is not None:
                return build_fxiaoke_submission_intent_blocked_result(step_key, step_label, existing_intent)
            retried = True
            continue

    entry, queue_path = record_pending_fxiaoke_task(
        report_text,
        report_path,
        step_key,
        step_label,
        last_payload or {},
    )
    retry_action, _ = classify_fxiaoke_pending_task(last_payload or {})
    cleanup_failed = False
    if retry_action == "safe_retry" and (not has_batch or batch_terminal):
        cleanup_failed = not clear_fxiaoke_submission_intent(intent_path, report_text=report_text)
    retry_text = "已重试 1 次" if retried else "未自动重试"
    message_lines = [
        f"{step_label}：自动同步失败，{retry_text}并记录待补交。",
        (last_payload or {}).get("message", "未返回更多错误信息。"),
        f"待补交文件：{queue_path}",
    ]
    if cleanup_failed:
        message_lines.append("本机提交证据未完整闭环或保护无法清除，已保留保护，请先只读核对。")
    artifacts = (last_payload or {}).get("artifacts", [])
    if artifacts:
        message_lines.append("失败留痕：")
        message_lines.extend(artifacts)
    return {
        **(last_payload or {}),
        "ok": False,
        "step": step_key,
        "status": "uncertain" if cleanup_failed else "error",
        "message": "\n".join(message_lines),
        "artifacts": artifacts,
        "pending_entry": entry,
        **({"retry_action": "verify_crm"} if cleanup_failed or batch_has_confirmed else {}),
    }


def sync_remote_workorders_after_save(
    report_text,
    report_path,
    structured_fields=None,
    profile_dir=None,
    customer_filter="",
    input_func=read_brief_input,
    authorized_drafts=None,
):
    from fxiaoke_remote_workorder import (
        build_remote_workorder_drafts,
        filter_drafts_by_customer,
        run_remote_workorder_flow,
    )

    onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_text, is_onsite=onsite),
        customer_filter,
    )
    if not drafts or (authorized_drafts is not None and not authorized_drafts):
        return []
    existing_intent, intent_path = reserve_fxiaoke_submission_intent(
        report_text,
        "remote_workorder",
        profile_dir=profile_dir,
        report_path=report_path,
    )
    if existing_intent is not None:
        return [
            build_fxiaoke_submission_intent_blocked_result(
                "remote_workorder",
                "纷享销客远程服务工单",
                existing_intent,
            )
        ]
    results = run_remote_workorder_flow(
        report_text,
        report_path,
        is_onsite=onsite,
        profile_dir=profile_dir,
        customer_filter=customer_filter,
        input_func=input_func,
        **({"authorized_drafts": authorized_drafts} if authorized_drafts is not None else {}),
    )
    final_results = []
    report_date = normalize_report_date_to_iso(report_text)
    may_clear_intent = True
    for result in results:
        normalized = normalize_fxiaoke_result_item(
            result,
            default_step="remote_workorder",
        )
        normalize_captured_remote_workorder_result(normalized)
        if normalized.get("ok"):
            record_fxiaoke_submission_history(report_text, report_path, normalized)
            if normalized.get("status") in {"submitted", "already_exists"}:
                clear_pending_fxiaoke_tasks(
                    report_path,
                    step_key="remote_workorder",
                    report_date=report_date,
                    service_customer=normalized.get("service_customer", ""),
                    includes_main_work=normalized.get("includes_main_work"),
                )
        else:
            entry, queue_path = record_pending_fxiaoke_task(
                report_text,
                report_path,
                "remote_workorder",
                "纷享销客远程服务工单",
                normalized,
            )
            message_lines = [
                normalized.get("message", "远程服务工单创建失败。"),
                f"待补交文件：{queue_path}",
            ]
            if normalized.get("artifacts"):
                message_lines.append("失败留痕：")
                message_lines.extend(normalized["artifacts"])
            normalized["message"] = "\n".join(message_lines)
            normalized["pending_entry"] = entry
        retry_action, _ = classify_fxiaoke_pending_task(normalized)
        if not normalized.get("ok") and retry_action != "safe_retry":
            may_clear_intent = False
        final_results.append(normalized)
    if may_clear_intent:
        clear_fxiaoke_submission_intent(intent_path)
    return final_results


def sync_fxiaoke_after_save(report_text, report_path, structured_fields=None, profile_dir=None):
    onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    results = [
        execute_fxiaoke_step(
            report_text,
            report_path,
            "daily_log",
            "纷享销客日报",
            trigger_fxiaoke_daily_log,
            profile_dir=profile_dir,
        )
    ]

    if has_fxiaoke_evidence_failure(results):
        return results

    if onsite:
        work_type = resolve_report_work_type(report_text, report_path, structured_fields)
        results.append(
            execute_fxiaoke_step(
                report_text,
                report_path,
                "service_record",
                "纷享销客外勤技术服务记录",
                trigger_fxiaoke_service_record,
                profile_dir=profile_dir,
                trigger_kwargs={"main_only": True, **({"work_type": work_type} if work_type else {})},
            )
        )
    else:
        results.append(
            build_skipped_fxiaoke_result(
                "service_record",
                "纷享销客外勤技术服务记录：当天非外勤，已跳过。",
            )
        )
    results.append(
        build_skipped_fxiaoke_result(
            "remote_workorder",
            "纷享销客远程服务工单：未获得逐单 Y 授权，未提交。",
        )
    )
    return results


def confirm_default_yes(prompt, input_func=None):
    input_func = input_func or read_brief_input
    while True:
        answer = input_func(prompt).strip().lower()
        if answer in {"", "y", "yes", "是", "确认", "确定", "提交"}:
            return True
        if answer in {"n", "no", "否", "不", "不要", "取消", "q", "quit", "退出", "跳过"}:
            return False
        print("未识别该输入：回车或Y继续；N、否、取消或Q跳过。")


def confirm_explicit_yes(prompt, input_func=None):
    input_func = input_func or read_brief_input
    return input_func(prompt).strip().lower() in {"y", "yes"}


def build_skipped_fxiaoke_result(step, message):
    return {
        "ok": True,
        "step": step,
        "status": "skipped",
        "message": message,
        "artifacts": [],
    }


def fxiaoke_results_exit_code(results):
    return 0 if all(normalize_fxiaoke_result_item(item)["ok"] for item in results) else 1


def has_fxiaoke_evidence_failure(results):
    return any(item.get("status") == SubmissionEvidenceError.status for item in results)


def build_fxiaoke_timing_summary(results):
    auth_elapsed = next(
        (
            item.get("auth_elapsed_seconds")
            for item in results
            if isinstance(item.get("auth_elapsed_seconds"), (int, float))
        ),
        None,
    )
    labels = {
        "daily_log": "日报",
        "service_record": "外勤",
        "remote_workorder": "远程工单",
    }
    parts = []
    if auth_elapsed is not None:
        parts.append(f"认证 {auth_elapsed:.3f} 秒")
    for item in results:
        elapsed = item.get("phase_elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            parts.append(f"{labels.get(item.get('step'), '同步')} {elapsed:.3f} 秒")
    return f"[纷享销客阶段耗时] {'、'.join(parts)}" if parts else ""


def print_compact_fxiaoke_results(results):
    """Show required services separately from optional remote outcomes."""
    print("\n[提交结果]")
    failures = []
    printed = 0
    daily_submitted = False
    required_pending = False
    optional_skipped = False
    for raw in results:
        row = normalize_fxiaoke_result_item(raw)
        remote_role = row.get("includes_main_work") if row["step"] == "remote_workorder" else None
        daily_submitted |= row["step"] == "daily_log" and row["ok"] and row["status"] in {"submitted", "already_exists"}
        required_pending |= not row["ok"] and (row["step"] == "service_record" or remote_role is True)
        if row.get("status") in {"skipped", "cancelled", "empty"}:
            optional_skipped |= remote_role is False and row["status"] == "skipped"
            if "已取消" in row["message"]:
                print(f"- {row['message'].splitlines()[0]}")
                printed += 1
            continue
        step = {
            "daily_log": "日报",
            "service_record": "外勤技术服务",
            "remote_workorder": "主技术服务" if remote_role is True else "附加远程支持" if remote_role is False else "远程工单",
        }.get(row["step"], "同步")
        customer = str(row.get("service_customer") or row.get("customer_name") or "").strip()
        identifier = str(
            row.get("workorder_id") or row.get("service_record_id") or row.get("feed_id") or ""
        ).strip()
        target = step + (f" · {customer}" if customer else "")
        if identifier:
            target += f" {identifier}"
        first_line = row["message"].splitlines()[0] if row["message"] else ""
        status = row["status"]
        if status in {"uncertain", "crm_id_captured", "evidence_error", "verify_crm", "timeout"} or (
            not row["ok"] and row.get("retry_action") == "verify_crm"
        ):
            print(f"? {target}：待核对 — {first_line[:160]}")
            failures.append((row, "先只读核对，禁止直接重试。"))
        elif row["ok"]:
            print(f"✓ {target}")
        elif status in {"auth_expired", "auth_unknown"}:
            print(f"✗ {target}：登录待恢复 — {first_line[:160]}")
            failures.append((row, "先完成登录维护，再从已保存日报恢复。"))
        else:
            print(f"✗ {target}：失败 — {first_line[:160]}")
            failures.append((
                row,
                "修正预检问题后重新提交。" if status == "preflight_failed"
                else "查看待办，仅重试失败客户；结果未知时先核对。",
            ))
        printed += 1
    if daily_submitted and required_pending:
        print("日报已发，技术服务待处理。")
    if optional_skipped:
        print("附加远程支持：有项目未选择提交，日报正文仍保留。")
        printed += 1
    if printed == 0:
        print("没有需要展示的结果。")
    for row, advice in failures:
        if row.get("login_command"):
            print(f"  登录维护：{row['login_command']}")
        if row.get("resume_command"):
            print(f"  恢复本日报：{row['resume_command']}")
        print(f"  下一步：{advice}")


def print_fxiaoke_results(results, details=False, compact=False):
    if compact and not details:
        print_compact_fxiaoke_results(results)
        return
    print("\n[纷享销客结果]")
    total = next((row.get("batch_elapsed_seconds") for row in results
                  if isinstance(row.get("batch_elapsed_seconds"), (int, float))), None)
    if total is not None:
        print(f"[纷享销客整批耗时（含预检、确认及远程工单）] {total:.3f} 秒")
    timing = build_fxiaoke_timing_summary(results)
    if timing:
        print(timing)
    for raw in results:
        row = normalize_fxiaoke_result_item(raw)
        step = {"daily_log": "日报", "service_record": "外勤服务", "remote_workorder": "远程工单"}.get(row["step"], "同步")
        status = row["status"]
        customer = str(row.get("service_customer") or row.get("customer_name") or "").strip()
        auth_failure = status in {"auth_expired", "auth_unknown"}
        if status in {"uncertain", "crm_id_captured", "evidence_error", "verify_crm", "timeout"} or (
            not row["ok"] and row.get("retry_action") == "verify_crm"
        ):
            label, advice = "待核对", "先只读核对，禁止直接重试。"
        elif status in {"skipped", "cancelled", "empty"}:
            label, advice = "已跳过", ""
        elif row["ok"]:
            label, advice = "成功", ""
        elif auth_failure:
            label, advice = "登录待恢复", "先完成下方登录维护，再从已保存日报恢复；仍需重新确认提交。"
        else:
            label = "失败"
            advice = ("修正预检问题后重新发起提交。" if status == "preflight_failed" else
                      "查看待办，仅重试失败客户；结果未知时先核对。")
        first_line = row["message"].splitlines()[0] if row["message"] else ""
        print(f"{customer + ' · ' if customer else ''}{step}：{label}。{first_line[:180]}")
        identifier = str(row.get("workorder_id") or row.get("service_record_id") or row.get("feed_id") or "")
        if identifier and identifier not in first_line[:180]:
            print(f"  编号：{identifier}")
        if row.get("local_recording_error"):
            print("  本地留存失败：请按编号核对CRM及本地记录，禁止直接重提。")
        if advice:
            print(f"  下一步：{advice}")
        if auth_failure and row.get("login_command"):
            print(f"  登录维护：{row['login_command']}")
        if row.get("resume_command"):
            print(f"  恢复本日报：{row['resume_command']}")
        if details:
            print("\n".join(format_fxiaoke_result_details(row)))
    if not details and any(not row.get("ok") and row.get("status") not in {"auth_expired", "auth_unknown"} for row in results):
        print("详情：rb --review-pending --details（只读）；本次运行可加 --details 展开留痕。")


def format_fxiaoke_result_details(row):
    lines = [str(row.get("message", ""))]
    differences = row.get("differences", [])
    if not isinstance(differences, list):
        lines.append("差异详情格式异常，原记录已保留。")
        differences = []
    for difference in differences:
        if isinstance(difference, dict):
            lines.append(f"  {difference.get('field', '')}：已有：{difference.get('observed', '')}；本次：{difference.get('expected', '')}")
    artifacts = row.get("artifacts", [])
    if not isinstance(artifacts, list):
        lines.append("留痕格式异常，原记录已保留。")
        artifacts = []
    for artifact in artifacts:
        if str(artifact) not in lines[0]:
            lines.append(f"  留痕：{artifact}")
    if row.get("local_recording_error"):
        lines.append(f"本地留存失败：{row['local_recording_error']}；请只读核对，禁止直接重提。")
    return lines


def validate_report_submission_fields(report_text, structured_fields=None):
    report_date = normalize_report_date_to_iso(report_text)
    parsed = parse_report_fields(report_text)
    missing = [label for label in ("服务客户", "处理情况", "明日安排") if not parsed.get(label, "").strip()]
    if report_date >= REMOTE_MAIN_SERVICE_START_DATE or is_onsite_workday(report_text=report_text, structured_fields=structured_fields):
        for index, entry in enumerate(parse_report_service_entries(report_text), 1):
            missing.extend(f"客户{index}的{label}" for label in ("服务客户", "处理情况") if not entry.get(label, "").strip())
    if missing:
        raise ValueError("提交前缺少必填项：" + "、".join(missing))


class BackgroundPreflightInteractionRequired(Exception):
    """A background preflight hit a prompt that needs the user present."""


def preflight_remote_customers(drafts, profile_dir=None, input_func=read_brief_input, report_path=None, *, blocked=None, compact=False):
    """Recover authentication before read-only lookup; this phase never submits."""
    if not drafts:
        return []
    import fxiaoke_service_record as fx
    from fxiaoke_remote_workorder import preflight_remote_workorder_drafts_on_authenticated_page

    playwright = browser = context = None
    try:
        playwright, browser, context, page = fx.open_authenticated_page(
            recover_cached_login=True,
            storage_state_path=fx.DEFAULT_STORAGE_STATE_PATH, show_browser=False,
            profile_dir=resolve_fxiaoke_profile_dir(profile_dir), cached_login_only=True,
        )
        return preflight_remote_workorder_drafts_on_authenticated_page(
            page, drafts, input_func=input_func, compact=compact,
            **({"blocked": blocked} if blocked is not None else {}),
        )
    except EOFError:
        raise
    except BackgroundPreflightInteractionRequired:
        raise
    except (fx.CachedLoginExpired, fx.AuthenticationUncertain):
        raise
    except Exception as exc:
        artifacts = []
        if report_path is not None and context is not None:
            try:
                artifacts = fx.capture_failure_artifacts(
                    page, fx.resolve_failure_artifact_root(report_path),
                    f"{drafts[0].report_date_iso.replace('-', '')}_remote_preflight",
                )
            except Exception:
                pass
        raise fx.AutomationFailure(str(exc), artifacts=artifacts) from exc
    finally:
        fx.close_browser_session(browser, context)
        fx.stop_browser_driver(playwright)


def _remote_draft_key(draft):
    from fxiaoke_remote_workorder import strict_customer_identity
    return (draft.report_date_iso, strict_customer_identity(draft.customer_name), draft.includes_main_work)


class PrefetchedCustomerPreflight:
    """Reusable outcome of a background read-only customer lookup."""

    def __init__(self, draft_keys, checked, blocked):
        self.draft_keys = frozenset(draft_keys)
        self.checked = list(checked)
        self.blocked = list(blocked)


class BackgroundCustomerPreflight:
    """Run the read-only customer lookup in parallel with the AI polish.

    Drafts are built from the unpolished quick-note fields; polishing only
    rewords "问题描述/处理情况", so customer identities match the final report.
    Only the CRM selections are reused later - never the draft content.
    """

    def __init__(self):
        self.checked = []
        self.blocked = []
        self.error = None
        self.draft_keys: frozenset[tuple[str, str, bool]] = frozenset()
        self.done = threading.Event()
        self.thread = None

    @staticmethod
    def _abort_interactive_input(prompt):
        raise BackgroundPreflightInteractionRequired(
            "后台核对遇到需要人工确认的客户提示。"
        )

    def start(self, structured_fields, report_path, profile_dir=None):
        from fxiaoke_remote_workorder import build_remote_workorder_drafts
        try:
            onsite = is_onsite_workday(
                structured_fields=structured_fields, report_path=report_path,
            )
            preview_text = render_multi_service_report(
                build_today_string(), structured_fields,
            )
            drafts = build_remote_workorder_drafts(
                preview_text, is_onsite=onsite,
            )
        except Exception as exc:
            self.error = exc
            self.done.set()
            return self
        self.draft_keys = frozenset(_remote_draft_key(draft) for draft in drafts)
        if not drafts:
            self.done.set()
            return self

        def run():
            try:
                self.checked = preflight_remote_customers(
                    drafts,
                    profile_dir=profile_dir,
                    input_func=self._abort_interactive_input,
                    report_path=report_path,
                    blocked=self.blocked,
                    compact=True,
                )
            except Exception as exc:
                self.checked = []
                self.blocked = []
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(
            target=run, name="background-customer-preflight", daemon=False,
        )
        self.thread.start()
        return self

    def collect(self, wait=True, timeout=None):
        """Finished reusable result, or None when the inline path must run."""
        if self.thread is not None and wait:
            if not self.done.wait(timeout):
                return None
        if not self.done.is_set() or self.error is not None:
            return None
        if not self.checked and not self.blocked:
            return None
        return PrefetchedCustomerPreflight(
            self.draft_keys, self.checked, self.blocked,
        )

    def settle(self, timeout=60.0):
        """Wait for a cancelled run to finish its read-only browser cleanup."""
        if self.thread is None or self.done.is_set():
            return
        emit_terminal_message("[提交前预检] 正在等待后台核对收尾（只读，不会写入 CRM）...")
        self.done.wait(timeout)


def apply_prefetched_customer_selections(drafts, prefetched_checked):
    """Move CRM selections from the background lookup onto the final drafts.

    Mirrors the per-draft validation of the inline preflight (review options,
    date, hours, required fields, duplicate identity) so skipping the inline
    run removes no safety check; only the unpolished draft content is dropped.
    """
    from dataclasses import replace
    from fxiaoke_remote_workorder import (
        apply_remote_workorder_review_options,
        normalize_positive_service_hours,
        strict_customer_identity,
    )
    by_key = {_remote_draft_key(draft): draft for draft in prefetched_checked}
    checked = []
    selected_units = set()
    for draft in drafts:
        draft = apply_remote_workorder_review_options(draft)
        date.fromisoformat(draft.report_date_iso)
        if draft.service_hours:
            normalize_positive_service_hours(draft.service_hours)
        if not draft.customer_name.strip() or not any(item.strip() for item in draft.content_items):
            raise ValueError("远程工单缺少客户或服务内容。")
        match = by_key.get(_remote_draft_key(draft))
        if match is None:
            continue
        unit_key = (draft.report_date_iso, strict_customer_identity(match.selected_customer_name))
        if unit_key in selected_units:
            raise RuntimeError("多张草稿对应同一CRM客户，请先合并工作内容和实际工时，再重新确认；未提交。")
        checked.append(replace(
            draft,
            selected_customer_name=match.selected_customer_name,
            completion_status_options=match.completion_status_options,
            completion_status=draft.completion_status if draft.completion_status in match.completion_status_options else "",
        ))
        selected_units.add(unit_key)
    return checked


def sync_fxiaoke_preflighted_batch(report_text, report_path, structured_fields=None,
                                  profile_dir=None, input_func=None, shared_session=False,
                                  shared_remote_session=False, prompt_submission_choices=False,
                                  compact_confirmation=False, prefetched_preflight=None):
    from fxiaoke_remote_workorder import (
        build_remote_workorder_drafts, build_skipped_remote_workorder_results,
        confirm_compact_remote_workorders, confirm_remote_workorder_batch,
        fill_compact_workorder_hours, resolve_customer_primary_name, strict_customer_identity,
    )
    blocked: list[dict[str, Any]] = []
    started_at = time.monotonic()
    input_func = input_func or read_brief_input
    compact_confirmation = compact_confirmation and not prompt_submission_choices
    try:
        validate_report_submission_fields(report_text, structured_fields)
        onsite = is_onsite_workday(report_text=report_text, structured_fields=structured_fields, report_path=report_path)
        drafts = build_remote_workorder_drafts(report_text, is_onsite=onsite)
        if prompt_submission_choices and normalize_report_date_to_iso(report_text) < REMOTE_MAIN_SERVICE_START_DATE:
            drafts = [draft for draft in drafts if not draft.includes_main_work]
        if drafts and not compact_confirmation:
            print("[远程工单客户识别] " + "、".join(draft.customer_name for draft in drafts))
        service_choice = onsite if compact_confirmation else None
        preflight_drafts = drafts
        if prompt_submission_choices:
            service_choice = onsite
            required_drafts = [draft for draft in drafts if draft.includes_main_work]
            if onsite:
                print("[必交技术服务] 外勤技术服务记录将随日报提交。")
            elif required_drafts:
                print("[必交技术服务] 非外勤主服务：" + "、".join(draft.customer_name for draft in required_drafts))
            optional_drafts = [draft for draft in drafts if not draft.includes_main_work]
            select_optional = optional_drafts and confirm_default_yes(
                "是否提交附加远程支持？(回车继续/N跳过附加支持): ", input_func=input_func,
            )
            preflight_drafts = required_drafts.copy()
            if select_optional:
                required_customers = {
                    strict_customer_identity(resolve_customer_primary_name(draft.customer_name))
                    for draft in required_drafts
                }
                for draft in optional_drafts:
                    if strict_customer_identity(resolve_customer_primary_name(draft.customer_name)) in required_customers:
                        print(f"[附加远程支持] {draft.customer_name} 与主服务同日同客户，未合并或另建工单。")
                    else:
                        preflight_drafts.append(draft)
        if preflight_drafts:
            reuse_keys = (
                prefetched_preflight.draft_keys
                if prefetched_preflight is not None else None
            )
            if reuse_keys is not None and reuse_keys == {
                _remote_draft_key(draft) for draft in preflight_drafts
            }:
                checked = apply_prefetched_customer_selections(
                    preflight_drafts, prefetched_preflight.checked,
                )
                blocked.extend(prefetched_preflight.blocked)
                print("[提交前预检] 已复用后台客户核对结果；本阶段未重复访问 CRM。")
            else:
                print("[提交前预检] 正在只读核对远程客户；此阶段不会发布日报或工单。")
                checked = preflight_remote_customers(
                    preflight_drafts, profile_dir=profile_dir, input_func=input_func,
                    report_path=report_path, blocked=blocked,
                    compact=compact_confirmation,
                )
            if compact_confirmation:
                checked = fill_compact_workorder_hours(
                    checked, report_path, input_func=input_func,
                )
                main_customer = parse_report_fields(report_text).get("服务客户", "").strip()
                service_summary = (
                    f"外勤技术服务：{main_customer or '主服务客户'}（默认市内上门 / 1 天 / 8 小时）"
                    if onsite else
                    "外勤技术服务：跳过（非外勤）"
                )
                blocked_summary = ""
                if blocked:
                    blocked_summary = "未包含：" + "、".join(
                        f"{item['service_customer']}（{item['message']}）"
                        for item in blocked
                    )
                authorized, service_choice = confirm_compact_remote_workorders(
                    checked, input_func=input_func,
                    submit_service_record=onsite,
                    service_summary=service_summary,
                    blocked_summary=blocked_summary,
                )
                if authorized is None:
                    return [
                        *blocked,
                        build_skipped_fxiaoke_result("sync", "已取消：没有开始任何 CRM 写入。"),
                    ]
            else:
                authorized = confirm_remote_workorder_batch(
                    checked, input_func=input_func,
                    **({"confirm_batch": False, "require_main": True} if prompt_submission_choices else {}),
                )
        elif compact_confirmation and onsite:
            main_customer = parse_report_fields(report_text).get("服务客户", "").strip()
            authorized, service_choice = confirm_compact_remote_workorders(
                [], input_func=input_func,
                submit_service_record=True,
                service_summary=(
                    f"外勤技术服务：{main_customer or '主服务客户'}"
                    "（默认市内上门 / 1 天 / 8 小时）"
                ),
            )
            if authorized is None:
                return [build_skipped_fxiaoke_result("sync", "已取消：没有开始任何 CRM 写入。")]
        else:
            authorized = []
        blocked_customers = {(item["service_customer"], item.get("includes_main_work", False)) for item in blocked}
        skipped = [item for item in build_skipped_remote_workorder_results(drafts, authorized)
                   if (item.get("service_customer"), item.get("includes_main_work")) not in blocked_customers]
        if prompt_submission_choices:
            deferred = [item for item in skipped if item.get("includes_main_work") is True]
            for item in deferred:
                item.update(build_deferred_main_workorder(item["service_customer"]))
            blocked.extend(deferred)
            skipped = [item for item in skipped if item.get("includes_main_work") is not True]
        if blocked and not compact_confirmation and not prompt_submission_choices:
            print("[部分预检未通过] 以下工单本次不会提交：")
            for item in blocked:
                print(f"{item['service_customer']}：{item['message']}")
            print("日报内容仍以已预览正文为准；其他工单仅执行刚才已确认的项目，外勤记录仍单独确认。")
            if not confirm_explicit_yes("是否继续发布日报并执行这些已授权项目？仅输入Y继续：", input_func=input_func):
                return [*blocked, build_skipped_fxiaoke_result("sync", "已取消剩余提交；没有开始任何CRM写入。")]

    except EOFError:
        raise
    except Exception as exc:
        auth_status = getattr(exc, "status", "")
        if auth_status in {"auth_expired", "auth_unknown"}:
            profile = resolve_fxiaoke_profile_dir(profile_dir)
            return [{"ok": False, "step": "sync", "status": auth_status,
                     "retry_action": "login_required", "artifacts": [],
                     "message": f"发布前认证检查未通过：{exc} 日报已保存在本地，日报和工单均未开始提交。",
                     "login_command": shlex.join(["rb", "--fxiaoke-login-check", "--fxiaoke-profile-dir", profile]),
                     "resume_command": shlex.join(["rb", "--sync-saved-report", "--date", normalize_report_date_to_iso(report_text),
                                                    "--path", str(report_path), "--fxiaoke-profile-dir", profile]),
                     "batch_elapsed_seconds": round(time.monotonic() - started_at, 3)}]
        failure_row = {
            "ok": False, "step": "sync", "status": "preflight_failed",
            "artifacts": getattr(exc, "artifacts", []),
            "message": f"发布前预检未通过：{exc} 日报和工单均未开始提交。",
            "batch_elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        if report_path is not None:
            failure_row["resume_command"] = shlex.join([
                "rb", "--sync-saved-report", "--date", normalize_report_date_to_iso(report_text),
                "--path", str(report_path), "--fxiaoke-profile-dir", resolve_fxiaoke_profile_dir(profile_dir),
            ])
        return [failure_row]
    if prompt_submission_choices:
        for item in blocked:
            entry, _ = record_pending_fxiaoke_task(
                report_text, report_path, "remote_workorder", "纷享销客远程服务工单", item,
            )
            item["pending_entry"] = entry
    results = sync_fxiaoke_after_save_interactive(
        report_text, report_path, structured_fields=structured_fields, profile_dir=profile_dir,
        input_func=input_func, shared_session=shared_session, shared_remote_session=shared_remote_session,
        prepared_remote_drafts=(authorized, skipped),
        **({"submit_service_record": service_choice}
           if prompt_submission_choices or compact_confirmation else {}),
    )
    elapsed = round(time.monotonic() - started_at, 3)
    results.extend(blocked)
    for row in results:
        row["batch_elapsed_seconds"] = elapsed
    return results


def build_deferred_main_workorder(customer):
    return {
        "ok": False, "step": "remote_workorder", "status": "required_pending",
        "service_customer": customer, "includes_main_work": True,
        "retry_action": "safe_retry", "submission_phase": "before_submit", "artifacts": [],
        "message": f"主技术服务：{customer} 未完成授权，待补交。",
    }


def normalize_fxiaoke_result_item(item, default_step="sync", elapsed_seconds=None):
    normalized = dict(item) if isinstance(item, dict) else {}
    ok_value = normalized.get("ok")
    normalized["ok"] = ok_value is True or (type(ok_value) is int and ok_value == 1)
    normalized["step"] = str(normalized.get("step", default_step))
    status = normalized.get("status", "submitted" if normalized["ok"] else "error")
    normalized["status"] = status if isinstance(status, str) else "uncertain"
    if normalized["status"] in {"submitted", "already_exists"} and not normalized["ok"]:
        normalized["status"] = "uncertain"
    if normalized["status"] in {"error", "partial_error", "timeout", "uncertain", "crm_id_captured", SubmissionEvidenceError.status}:
        normalized["ok"] = False
    if normalized["status"] in {"timeout", "uncertain", "crm_id_captured", SubmissionEvidenceError.status}:
        normalized["retry_action"] = SubmissionEvidenceError.retry_action
    normalized["message"] = str(normalized.get("message", ""))
    artifacts = normalized.get("artifacts", [])
    normalized["artifacts"] = artifacts if isinstance(artifacts, list) else []
    if isinstance(elapsed_seconds, (int, float)):
        normalized["elapsed_seconds"] = float(elapsed_seconds)
    elif isinstance(normalized.get("elapsed_seconds"), (int, float)):
        normalized["elapsed_seconds"] = float(normalized["elapsed_seconds"])
    return normalized


def normalize_captured_remote_workorder_result(normalized):
    if (
        normalized.get("step") == "remote_workorder"
        and normalized.get("status") == "crm_id_captured"
    ):
        workorder_id = str(normalized.get("workorder_id", "")).strip()
        normalized["ok"] = False
        normalized["status"] = "uncertain"
        normalized["submission_phase"] = "crm_id_captured"
        normalized["retry_action"] = "verify_crm"
        normalized["message"] = (
            normalized["message"]
            + (
                f"\n已捕获工单编号 {workorder_id}，但尚未完成 CRM 身份复核；"
                if workorder_id
                else "\n已捕获工单编号，但尚未完成 CRM 身份复核；"
            )
            + "已保留本机提交意图，禁止自动重试。"
        )
    return normalized


def should_use_shared_session(args=None):
    if args is not None and getattr(args, "shared_session", False):
        return True
    configured = get_env_value("DAILY_REPORT_SHARED_SESSION").lower()
    if configured in {"0", "false", "no", "n", "off"}:
        return False
    if configured in {"1", "true", "yes", "y", "on"}:
        return True
    return sys.platform == "darwin"


def should_use_shared_remote_session(args=None):
    return bool(args is not None and getattr(args, "shared_remote_session", False))


def prepare_authorized_remote_workorders(
    report_text,
    report_path,
    structured_fields=None,
    input_func=read_brief_input,
):
    from fxiaoke_remote_workorder import (
        build_remote_workorder_drafts,
        build_skipped_remote_workorder_results,
        confirm_remote_workorder_drafts,
        filter_drafts_by_customer,
    )

    onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    drafts = filter_drafts_by_customer(
        build_remote_workorder_drafts(report_text, is_onsite=onsite),
        "",
    )
    authorized = confirm_remote_workorder_drafts(drafts, input_func=input_func)
    skipped = build_skipped_remote_workorder_results(drafts, authorized)
    return authorized, skipped


def sync_fxiaoke_after_save_interactive(
    report_text,
    report_path,
    structured_fields=None,
    profile_dir=None,
    input_func=None,
    shared_session=False,
    shared_remote_session=False,
    prepared_remote_drafts=None,
    submit_service_record=None,
):
    input_func = input_func or read_brief_input
    onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    if onsite and resolve_report_work_type(report_text, report_path, structured_fields) == "售后":
        print("[关联规则] 日报为售后时，仅在签到是实施工单的情况下改查同客户/部门及商机的售后工单；按不晚于服务日的创建时间选唯一最近单。缺字段、并列或归属不清停止；签到本身不修改。")

    if shared_session:
        shared_remote_session = shared_remote_session or bool(prepared_remote_drafts and prepared_remote_drafts[0])
        submit_service_record = onsite and (submit_service_record if submit_service_record is not None else confirm_default_yes(
            "\n是否自动填写当天外勤主服务客户技术服务记录？(回车提交/N跳过): ",
            input_func=input_func,
        ))
        if shared_remote_session:
            if prepared_remote_drafts is not None:
                authorized_remote_drafts, skipped_remote_results = prepared_remote_drafts
            else:
                authorized_remote_drafts, skipped_remote_results = prepare_authorized_remote_workorders(
                    report_text, report_path, structured_fields=structured_fields, input_func=input_func,
                )
            if authorized_remote_drafts:
                existing_remote_intent, remote_intent_path = reserve_fxiaoke_submission_intent(
                    report_text,
                    "remote_workorder",
                    profile_dir=profile_dir,
                    report_path=report_path,
                )
                if existing_remote_intent is not None:
                    final_results = run_fxiaoke_shared_sync(
                        report_text,
                        report_path,
                        structured_fields=structured_fields,
                        profile_dir=profile_dir,
                        skip_service_record=not submit_service_record,
                    )
                    final_results.append(
                        build_fxiaoke_submission_intent_blocked_result(
                            "remote_workorder",
                            "纷享销客远程服务工单",
                            existing_remote_intent,
                        )
                    )
                else:
                    final_results = run_fxiaoke_shared_sync(
                        report_text,
                        report_path,
                        structured_fields=structured_fields,
                        profile_dir=profile_dir,
                        skip_service_record=not submit_service_record,
                        remote_drafts=authorized_remote_drafts,
                        remote_intent_path=remote_intent_path,
                    )
                    final_results.extend(skipped_remote_results)
            else:
                final_results = run_fxiaoke_shared_sync(
                    report_text,
                    report_path,
                    structured_fields=structured_fields,
                    profile_dir=profile_dir,
                    skip_service_record=not submit_service_record,
                )
                final_results.extend(skipped_remote_results)
        else:
            final_results = run_fxiaoke_shared_sync(
                report_text,
                report_path,
                structured_fields=structured_fields,
                profile_dir=profile_dir,
                skip_service_record=not submit_service_record,
            )
        if onsite and not submit_service_record:
            for item in final_results:
                if item.get("step") == "service_record" and item.get("status") == "skipped":
                    item["message"] = "纷享销客外勤主服务记录：已跳过。"
        if has_fxiaoke_evidence_failure(final_results):
            return final_results
        if not shared_remote_session:
            final_results.extend(
                sync_remote_workorders_after_save(
                    report_text,
                    report_path,
                    structured_fields=structured_fields,
                    profile_dir=profile_dir,
                    input_func=input_func,
                    **({"authorized_drafts": prepared_remote_drafts[0]} if prepared_remote_drafts is not None else {}),
                )
            )
            if prepared_remote_drafts is not None:
                final_results.extend(prepared_remote_drafts[1])
        return final_results

    results = [
        execute_fxiaoke_step(
            report_text,
            report_path,
            "daily_log",
            "纷享销客日报",
            trigger_fxiaoke_daily_log,
            profile_dir=profile_dir,
        )
    ]

    if has_fxiaoke_evidence_failure(results):
        return results

    if onsite and (submit_service_record if submit_service_record is not None else confirm_default_yes(
        "\n是否自动填写当天外勤主服务客户技术服务记录？(回车提交/N跳过): ",
        input_func=input_func,
    )):
        work_type = resolve_report_work_type(report_text, report_path, structured_fields)
        results.append(
            execute_fxiaoke_step(
                report_text,
                report_path,
                "service_record",
                    "纷享销客外勤主服务记录",
                trigger_fxiaoke_service_record,
                profile_dir=profile_dir,
                    trigger_kwargs={"main_only": True, **({"work_type": work_type} if work_type else {})},
            )
        )
    elif onsite:
        results.append(
            build_skipped_fxiaoke_result(
                "service_record",
                "纷享销客外勤主服务记录：已跳过。",
            )
        )
    else:
        results.append(
            build_skipped_fxiaoke_result(
                "service_record",
                "纷享销客外勤主服务记录：当天非外勤，已跳过。",
            )
        )
    if has_fxiaoke_evidence_failure(results):
        return results
    results.extend(
        sync_remote_workorders_after_save(
            report_text,
            report_path,
            structured_fields=structured_fields,
            profile_dir=profile_dir,
            input_func=input_func,
            **({"authorized_drafts": prepared_remote_drafts[0]} if prepared_remote_drafts is not None else {}),
        )
    )
    if prepared_remote_drafts is not None:
        results.extend(prepared_remote_drafts[1])
    return results


def _shared_confirmation_contexts(report_text, intent_paths, remote_drafts):
    """Freeze the parent's identity before the child can produce receipts."""
    contexts = {}
    for step, path in intent_paths.items():
        if path is None:
            continue
        try:
            intent = json.loads(Path(path).read_text(encoding="utf-8"))
            context = {key: intent[key] for key in (
                "step", "report_date", "report_digest", "owner_pid", "owner_token",
            )}
            if context["owner_pid"] != os.getpid() or context["step"] != step:
                continue
            if step == "daily_log":
                customers = ["、".join(extract_report_customer_names(report_text))]
            elif step == "service_record":
                entries = parse_report_service_entries(report_text) or [parse_report_fields(report_text)]
                customers = [entry.get("服务客户", "").strip() for entry in entries
                             if entry.get("服务客户", "").strip()]
            else:
                customers = [draft.customer_name for draft in remote_drafts]
            context.update(path=str(path), expected_customers=customers)
            contexts[step] = context
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Missing evidence can never authorize recovery or a new write.
            continue
    return contexts


def _recover_shared_confirmation(payload, report_text, report_path, contexts, profile_dir):
    """Recover only owned receipts or read-only confirmation; never restart writes."""
    if (payload.get("ok") or payload.get("status") == SubmissionEvidenceError.status or not contexts
            or (isinstance(payload.get("results"), list) and payload["results"])):
        return payload
    recovered = []
    covered = set()
    for step, context in contexts.items():
        covered.add(step)
        try:
            with _fxiaoke_intent_lock(Path(context["path"])):
                intent = json.loads(Path(context["path"]).read_text(encoding="utf-8"))
            if any(intent.get(key) != context[key] for key in (
                "step", "report_date", "report_digest", "owner_pid", "owner_token",
            )):
                raise ValueError("original shared submission identity changed")
            evidence = intent.get("evidence", {})
            if not evidence:
                covered.discard(step)
                continue
            if evidence.get("expected_customers") != context["expected_customers"]:
                raise ValueError("authorized shared targets are unconfirmed")
            children = evidence.get("children", {})
            for index, customer in enumerate(context["expected_customers"]):
                child = children.get(str(index), {})
                if child and child.get("service_customer") != customer:
                    raise ValueError("shared child identity changed")
                row = dict(child.get("result") or {
                    "ok": False, "status": "uncertain", "retry_action": "verify_crm",
                    "message": "子进程结果未完整返回，保留原保护，请先只读核对。",
                })
                if row.get("step", step) != step or row.get("service_customer", customer) != customer:
                    raise ValueError("shared receipt target changed")
                row.update(step=step, service_customer=customer)
                for key in ("feed_id", "workorder_id", "service_record_id", "crm_customer"):
                    if child.get(key) and row.get(key) and child[key] != row[key]:
                        raise ValueError("shared receipt identifier changed")
                    if child.get(key):
                        row[key] = child[key]
                started = any(child.get("milestones", {}).get(mark) is True
                              for mark in ("submit_clicked", "crm_id_captured"))
                if step == "daily_log" and row.get("ok") is not True and started:
                    candidate = reconcile_daily_log_watchdog_uncertain(
                        {**row, "status": "uncertain"}, report_text, report_path,
                        profile_dir=profile_dir, confirmation_started=True,
                    )
                    if candidate.get("ok") is True:
                        verified_customer = candidate.pop("_watchdog_verified_customer", None)
                        checkpoint_daily_log_watchdog_reconciliation(context, candidate, verified_customer)
                        row = candidate
                recovered.append(row)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SubmissionEvidenceError) as exc:
            recovered = [row for row in recovered if row.get("step") != step]
            recovered.append({
                "ok": False, "step": step, "status": SubmissionEvidenceError.status,
                "retry_action": "verify_crm", "artifacts": [],
                "message": f"共用会话原提交证据未能安全恢复：{exc}；保护已保留。",
            })
    recovered.extend(row for row in (payload.get("results") or [])
                     if isinstance(row, dict) and row.get("step") not in covered)
    return {**payload, "results": recovered} if recovered else payload


def run_fxiaoke_shared_sync(
    report_text,
    report_path,
    structured_fields=None,
    profile_dir=None,
    skip_service_record=False,
    remote_drafts=None,
    remote_intent_path=None,
):
    remote_drafts = list(remote_drafts or [])
    intent_steps = ["daily_log"]
    if not skip_service_record:
        intent_steps.append("service_record")
    blocked_step, existing_intent, intent_paths = reserve_fxiaoke_submission_intents(
        report_text,
        intent_steps,
        profile_dir=profile_dir,
        report_path=report_path,
    )
    if blocked_step is not None:
        clear_fxiaoke_submission_intent(remote_intent_path)
        step_label = "纷享销客日报" if blocked_step == "daily_log" else "纷享销客技术服务记录"
        return [
            build_fxiaoke_submission_intent_blocked_result(
                blocked_step,
                step_label,
                existing_intent,
            )
        ]
    if remote_intent_path is not None:
        intent_paths["remote_workorder"] = remote_intent_path
    confirmation_contexts = _shared_confirmation_contexts(report_text, intent_paths, remote_drafts)
    if remote_drafts:
        remote_drafts_file = write_authorized_remote_drafts_file(
            remote_drafts,
            profile_dir=profile_dir,
        )
        try:
            payload = run_fxiaoke_subprocess(
                build_fxiaoke_sync_command(
                    report_text,
                    report_path,
                    structured_fields=structured_fields,
                    profile_dir=profile_dir,
                    skip_service_record=skip_service_record,
                    remote_drafts_file=remote_drafts_file,
                )
            )
        finally:
            remote_drafts_file.unlink(missing_ok=True)
    else:
        payload = run_fxiaoke_subprocess(
            build_fxiaoke_sync_command(
                report_text,
                report_path,
                structured_fields=structured_fields,
                profile_dir=profile_dir,
                skip_service_record=skip_service_record,
            )
        )
    payload = _recover_shared_confirmation(
        payload, report_text, report_path, confirmation_contexts, profile_dir,
    )
    results = payload.get("results")
    if payload.get("status") == SubmissionEvidenceError.status:
        partial_results = [normalize_fxiaoke_result_item(item) for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        if not has_fxiaoke_evidence_failure(partial_results):
            partial_results.append({
                "ok": False, "step": "sync", "status": SubmissionEvidenceError.status,
                "retry_action": SubmissionEvidenceError.retry_action,
                "message": payload.get("message", "本机提交证据无法保存，已停止后续处理，请先只读核对。"),
                "artifacts": payload.get("artifacts", []),
            })
        return partial_results
    if not isinstance(results, list) or not results:
        payload_status = str(
            payload.get("status", "submitted" if payload.get("ok") else "error")
        )
        if intent_steps and not payload.get("ok"):
            fallback_status = "uncertain" if payload_status in {"timeout", "uncertain"} else payload_status
            results = [
                {
                    "ok": False,
                    "step": step,
                    "status": fallback_status,
                    "message": payload.get("message", "未返回更多错误信息。"),
                    "artifacts": payload.get("artifacts", []),
                }
                for step in intent_steps
            ]
        else:
            results = [
                {
                    "ok": bool(payload.get("ok")),
                    "step": "sync",
                    "status": payload_status,
                    "message": payload.get("message", "未返回更多错误信息。"),
                    "artifacts": payload.get("artifacts", []),
                }
            ]

    final_results = []
    report_date = normalize_report_date_to_iso(report_text)
    for item in results:
        normalized = normalize_fxiaoke_result_item(
            item,
            elapsed_seconds=payload.get("elapsed_seconds"),
        )
        for key in ("workorder_id", "service_record_id"):
            if item.get(key):
                normalized[key] = item.get(key)
        normalize_captured_remote_workorder_result(normalized)
        if (
            not normalized["ok"]
            and normalized["status"] not in {"uncertain", SubmissionEvidenceError.status}
            and normalized["step"] in {"daily_log", "service_record", "remote_workorder"}
        ):
            step_label = {
                "daily_log": "纷享销客日报",
                "service_record": "纷享销客技术服务记录",
                "remote_workorder": "纷享销客远程服务工单",
            }.get(normalized["step"], "纷享销客同步")
            entry, queue_path = record_pending_fxiaoke_task(
                report_text,
                report_path,
                normalized["step"],
                step_label,
                normalized,
            )
            message_lines = [
                normalized["message"] or f"{step_label}：自动同步失败。",
                f"待补交文件：{queue_path}",
            ]
            if normalized["artifacts"]:
                message_lines.append("失败留痕：")
                message_lines.extend(normalized["artifacts"])
            normalized["message"] = "\n".join(message_lines)
            normalized["pending_entry"] = entry
        elif not normalized["ok"] and normalized["status"] in {"uncertain", SubmissionEvidenceError.status}:
            normalized["message"] = (
                (normalized["message"] or "纷享销客结果待确认。")
                + "\n已停止自动重试和待补交入队，请先在 CRM 中只读核对。"
            )
        elif normalized["ok"]:
            record_fxiaoke_submission_history(report_text, report_path, normalized)
            if normalized["status"] in {"submitted", "already_exists"}:
                if normalized["step"] == "daily_log":
                    clear_pending_fxiaoke_tasks(
                        report_path,
                        step_key="daily_log",
                        report_date=report_date,
                    )
                elif normalized["step"] == "service_record" and str(normalized.get("service_customer") or "").strip():
                    clear_pending_fxiaoke_tasks(
                        report_path,
                        step_key="service_record",
                        report_date=report_date,
                        service_customer=normalized.get("service_customer", ""),
                        **build_fxiaoke_pending_service_clear_filters(normalized),
                    )
                elif normalized["step"] == "remote_workorder":
                    clear_pending_fxiaoke_tasks(
                        report_path,
                        step_key="remote_workorder",
                        report_date=report_date,
                        service_customer=normalized.get("service_customer", ""),
                        includes_main_work=normalized.get("includes_main_work"),
                    )
        final_results.append(normalized)
    for step, intent_path in intent_paths.items():
        step_results = [item for item in final_results if item["step"] == step]
        if step_results and all(
            item["ok"] or classify_fxiaoke_pending_task(item)[0] == "safe_retry"
            for item in step_results
        ):
            if not clear_fxiaoke_submission_intent(intent_path, report_text=report_text):
                final_results.append({
                    "ok": False, "step": step, "status": "uncertain", "retry_action": "verify_crm",
                    "message": "提交阶段还有未确认的结果，已保留保护，请先只读核对。", "artifacts": [],
                })
    return final_results


def sync_fxiaoke_after_save_fast(report_text, report_path, structured_fields=None, profile_dir=None):
    from fxiaoke_remote_workorder import build_remote_workorder_drafts

    try:
        validate_report_submission_fields(report_text, structured_fields)
    except ValueError as exc:
        return [{"ok": False, "step": "sync", "status": "preflight_failed", "message": str(exc), "artifacts": []}]
    onsite = is_onsite_workday(
        report_text=report_text,
        structured_fields=structured_fields,
        report_path=report_path,
    )
    deferred = [] if onsite or normalize_report_date_to_iso(report_text) < REMOTE_MAIN_SERVICE_START_DATE else [
        build_deferred_main_workorder(draft.customer_name)
        for draft in build_remote_workorder_drafts(report_text, is_onsite=False, includes_main_work=True)
    ]
    for item in deferred:
        entry, _ = record_pending_fxiaoke_task(
            report_text, report_path, "remote_workorder", "纷享销客主技术服务", item,
        )
        item["pending_entry"] = entry
    final_results = run_fxiaoke_shared_sync(
        report_text,
        report_path,
        structured_fields=structured_fields,
        profile_dir=profile_dir,
        skip_service_record=not onsite,
    )
    final_results.extend(deferred)
    final_results.append({
        **build_skipped_fxiaoke_result("remote_workorder", "附加远程支持：非交互执行未授权，未提交。"),
        "includes_main_work": False,
    })
    return final_results


def retry_saved_daily_log(report_path, report_date=None, profile_dir=None):
    report_text = find_saved_report_section(report_path, report_date=report_date)
    result = execute_fxiaoke_step(
        report_text,
        report_path,
        "daily_log",
        "纷享销客日报",
        trigger_fxiaoke_daily_log,
        profile_dir=profile_dir,
        pending_clear_filters={},
    )
    return report_text, result


def sync_saved_report(
    report_path,
    report_date=None,
    profile_dir=None,
    input_func=None,
    report_text=None,
    shared_session=False,
    shared_remote_session=False,
    prompt_submission_choices=False,
    compact_confirmation=False,
):
    saved_report = report_text or find_saved_report_section(report_path, report_date=report_date)
    input_func = input_func or read_brief_input
    sync_kwargs = {
        "profile_dir": profile_dir,
        "input_func": input_func,
    }
    if shared_session:
        sync_kwargs["shared_session"] = True
    if shared_remote_session:
        sync_kwargs["shared_remote_session"] = True
    if prompt_submission_choices:
        sync_kwargs["prompt_submission_choices"] = True
    if compact_confirmation:
        sync_kwargs["compact_confirmation"] = True
    results = sync_fxiaoke_preflighted_batch(saved_report, report_path, **sync_kwargs)
    return saved_report, results


def retry_saved_service_record(
    report_path,
    report_date=None,
    profile_dir=None,
    main_only=False,
    clear_mode_filters=None,
    service_customer="",
):
    report_text = find_saved_report_section(report_path, report_date=report_date)
    service_customer = str(service_customer or "").strip()
    if not service_customer:
        return report_text, {
            "ok": False, "step": "service_record", "status": "confirm_customer",
            "retry_action": "confirm_customer", "artifacts": [],
            "message": "技术服务待办缺少客户身份，已停止自动重试，请先确认客户。",
        }
    result = execute_fxiaoke_step(
        report_text,
        report_path,
        "service_record",
        "纷享销客技术服务记录",
        trigger_fxiaoke_service_record,
        profile_dir=profile_dir,
        trigger_kwargs={"main_only": main_only, "service_customer": service_customer},
        pending_clear_filters={**(clear_mode_filters or {}), "service_customer": service_customer},
    )
    return report_text, result


def retry_saved_remote_workorder(
    report_path,
    report_date=None,
    service_customer="",
    profile_dir=None,
    input_func=read_brief_input,
    *,
    includes_main_work=None,
):
    from fxiaoke_remote_workorder import (
        build_remote_workorder_drafts, build_skipped_remote_workorder_results,
        confirm_remote_workorder_batch, filter_drafts_by_customer,
    )
    report_text = find_saved_report_section(report_path, report_date=report_date)
    service_customer = str(service_customer or "").strip()
    if not service_customer:
        return report_text, [{
            "ok": False, "step": "remote_workorder", "status": "confirm_customer",
            "retry_action": "confirm_customer", "artifacts": [],
            "message": "远程待办缺少客户身份，已停止重试，请先确认客户。",
        }]
    onsite = is_onsite_workday(report_text=report_text, report_path=report_path)
    drafts = filter_drafts_by_customer(build_remote_workorder_drafts(report_text, is_onsite=onsite), service_customer)
    if isinstance(includes_main_work, bool):
        drafts = [draft for draft in drafts if draft.includes_main_work is includes_main_work]
    elif len(drafts) > 1:
        return report_text, [{
            "ok": False, "step": "remote_workorder", "status": "verify_crm",
            "service_customer": service_customer, "retry_action": "verify_crm", "artifacts": [],
            "message": "旧待办未区分同客户的主技术服务和附加支持，请先核对，未开始补交。",
        }]
    if drafts:
        started = time.monotonic()
        try:
            checked = preflight_remote_customers(drafts, profile_dir=profile_dir, input_func=input_func, report_path=report_path)
            authorized = confirm_remote_workorder_batch(checked, input_func=input_func)
        except EOFError:
            raise
        except Exception as exc:
            return report_text, [{
                "ok": False, "step": "remote_workorder", "status": "preflight_failed",
                "service_customer": service_customer, "artifacts": getattr(exc, "artifacts", []),
                "message": f"重试前预检未通过：{exc}，本次未提交。",
            }]
        results = sync_remote_workorders_after_save(
            report_text, report_path, profile_dir=profile_dir, customer_filter=service_customer,
            input_func=input_func, authorized_drafts=authorized,
        ) if authorized else []
        results.extend(build_skipped_remote_workorder_results(drafts, authorized))
        elapsed = round(time.monotonic() - started, 3)
        for row in results:
            row["batch_elapsed_seconds"] = elapsed
        return report_text, results
    return report_text, [
        {
            "ok": False,
            "step": "remote_workorder",
            "status": "error",
            "service_customer": service_customer,
            "message": f"日报中未找到客户“{service_customer}”对应的远程服务工单草稿。",
            "artifacts": [],
        }
    ]


def retry_pending_fxiaoke_tasks(report_path, profile_dir=None, today=None, customer_filter="", report_date_filter=None):
    # A filtered retry must not reconcile or remove another customer's work.
    reconciled_workorders = [] if customer_filter or report_date_filter else reconcile_pending_service_records_from_history(report_path)
    pending_tasks = load_pending_fxiaoke_tasks(report_path)
    if not pending_tasks:
        results = [
            {
                "ok": True,
                "step": "service_record",
                "status": "reconciled",
                "workorder_id": workorder_id,
                "message": (
                    "纷享销客技术服务记录：已依据本地确认历史清除待补交，"
                    f"无需重试。工单：{workorder_id}。"
                ),
                "artifacts": [],
            }
            for workorder_id in reconciled_workorders
        ]
        return results or [
            {
                "ok": True,
                "step": "pending",
                "status": "empty",
                "message": "待补交队列为空，无需处理。",
                "artifacts": [],
            }
        ]

    results = []
    seen = set()
    blocked_targets = {}
    for task in pending_tasks:
        action, _ = classify_fxiaoke_pending_task(task, today=today)
        key = build_fxiaoke_pending_task_key(task)
        if action != "safe_retry" and (key not in blocked_targets or action == "verify_crm"):
            blocked_targets[key] = task
    for task in pending_tasks:
        if customer_filter and str(task.get("service_customer") or "").strip() != customer_filter.strip():
            continue
        if report_date_filter and str(task.get("report_date") or "") != normalize_report_date_argument(report_date_filter).replace(".", "-"):
            continue
        key = build_fxiaoke_pending_task_key(task)
        if key in seen:
            continue
        seen.add(key)
        legacy_key = (*key[:3], None) if key[0] == "remote_workorder" else key
        legacy_block = blocked_targets.get(legacy_key)
        task = blocked_targets.get(key, legacy_block if legacy_block is not None else task)
        step = task.get("step")
        report_date = task.get("report_date")
        retry_action, retry_advice = classify_fxiaoke_pending_task(task, today=today)
        if retry_action != "safe_retry":
            step_label = (
                str(task.get("step_label", "")).strip()
                or describe_fxiaoke_pending_task_mode(task)
            )
            results.append(
                {
                    "ok": False,
                    "step": step,
                    "status": retry_action,
                    "service_customer": task.get("service_customer", ""),
                    "report_date": report_date,
                    "retry_action": retry_action,
                    "message": f"{step_label}：{retry_advice}",
                    "artifacts": task.get("artifacts", []),
                }
            )
            continue
        if step == "daily_log":
            _, result = retry_saved_daily_log(report_path, report_date=report_date, profile_dir=profile_dir)
            results.append(result)
        elif step == "remote_workorder" or (
            step == "service_record"
            and (task.get("remote_only") or task.get("remote_support_only"))
        ):
            _, remote_results = retry_saved_remote_workorder(
                report_path,
                report_date=report_date,
                service_customer=str(task.get("service_customer", "")).strip(),
                profile_dir=profile_dir,
                **({"includes_main_work": task["includes_main_work"]} if isinstance(task.get("includes_main_work"), bool) else {}),
            )
            results.extend(remote_results)
        elif step == "service_record":
            _, result = retry_saved_service_record(
                report_path,
                report_date=report_date,
                profile_dir=profile_dir,
                main_only=bool(task.get("main_only")),
                clear_mode_filters=build_fxiaoke_pending_service_clear_filters(task),
                service_customer=str(task.get("service_customer") or "").strip(),
            )
            results.append(result)
        else:
            continue
        if has_fxiaoke_evidence_failure(results):
            break
    return results or [{"ok": True, "step": "pending", "status": "empty", "message": "没有符合筛选条件的待补交项。", "artifacts": []}]


def main(argv=None):
    configure_stdio()
    args = parse_args(argv)
    report_path = resolve_report_path(args.report_path)
    fxiaoke_profile_dir = resolve_fxiaoke_profile_dir(args.fxiaoke_profile_dir)
    fxiaoke_enabled = crm_enabled()
    auto_sync_enabled = fxiaoke_enabled and not args.no_fxiaoke

    print("--- 个人日报 AI 助手 ---")
    print(f"日报输出路径：{report_path}")

    try:
        if getattr(args, "refresh_local_metadata", False):
            section = find_saved_report_section(report_path, report_date=args.report_date)
            warnings = update_report_auxiliaries(section, report_path)
            print("本地附加信息已更新；未生成日报或执行CRM。" if not warnings else "附加信息仍有未完成项，请按提示处理。")
            return 1 if warnings else 0
        if args.review_pending:
            print("\n[待补交队列]")
            print(
                build_fxiaoke_pending_review(
                    report_path,
                    profile_dir=fxiaoke_profile_dir,
                    **({"details": True} if args.details else {}),
                )
            )
            return 0

        if not fxiaoke_enabled and any((
            args.retry_pending,
            args.reset_fxiaoke_login,
            args.fxiaoke_login_check,
            args.fxiaoke_doctor,
            args.retry_daily_log,
            args.sync_saved_report,
        )):
            print("\n纷享销客未启用。请先完成本地配置并设置 FXIAOKE_ENABLED=1。")
            return 1

        if auto_sync_enabled:
            pending_alert = build_fxiaoke_pending_alert(
                report_path,
                profile_dir=fxiaoke_profile_dir,
            )
            if pending_alert:
                print(f"\n{pending_alert}")

        if args.retry_pending:
            print("\n[待补交重试] 正在处理待补交队列...")
            retry_results = retry_pending_fxiaoke_tasks(
                report_path, profile_dir=fxiaoke_profile_dir,
                **({"customer_filter": args.customer, "report_date_filter": args.report_date}
                   if args.customer or args.report_date else {}),
            )
            print_fxiaoke_results(retry_results, details=args.details)
            return fxiaoke_results_exit_code(retry_results)

        if args.reset_fxiaoke_login:
            print("\n[纷享销客登录态] 正在清理登录态缓存...")
            payload = run_fxiaoke_subprocess(
                build_fxiaoke_sync_maintenance_command(
                    "--reset-login-state",
                    profile_dir=fxiaoke_profile_dir,
                )
            )
            print(payload["message"])
            return 0 if payload["ok"] else 1

        if args.fxiaoke_login_check:
            print("\n[纷享销客登录态] 正在检查/刷新登录态...")
            payload = run_fxiaoke_subprocess(
                build_fxiaoke_sync_maintenance_command(
                    "--login-check",
                    profile_dir=fxiaoke_profile_dir,
                )
            )
            print(payload["message"])
            return 0 if payload["ok"] else 1

        if args.fxiaoke_doctor:
            print("\n[纷享销客环境自检] 正在检查浏览器自动化环境...")
            payload = run_fxiaoke_subprocess(
                build_fxiaoke_sync_maintenance_command(
                    "--doctor",
                    profile_dir=fxiaoke_profile_dir,
                )
            )
            if payload.get("diagnostics"):
                for item in payload["diagnostics"]:
                    status = "OK" if item.get("ok") else "FAIL"
                    print(f"[{status}] {item.get('label')}: {item.get('detail')}")
            else:
                print(payload["message"])
            return 0 if payload["ok"] else 1

        if args.retry_daily_log:
            report_text, retry_result = retry_saved_daily_log(
                report_path,
                report_date=args.report_date,
                profile_dir=fxiaoke_profile_dir,
            )
            report_date_iso = normalize_report_date_to_iso(report_text)
            customer_name = parse_report_fields(report_text).get("服务客户", "").strip()
            print(f"\n[补交模式] 已定位日报：{report_date_iso} | 客户：{customer_name}")
            print_fxiaoke_results([retry_result], details=args.details)
            return 0 if retry_result["ok"] else 1

        if args.sync_saved_report:
            report_text = find_saved_report_section(report_path, report_date=args.report_date)
            report_date_iso = normalize_report_date_to_iso(report_text)
            customers = extract_report_customer_names(report_text)
            print(
                f"\n[已保存日报] {report_date_iso} | "
                f"客户：{'、'.join(customers) if customers else '未识别'}"
            )

            _, fxiaoke_results = sync_saved_report(
                report_path,
                report_date=args.report_date,
                profile_dir=fxiaoke_profile_dir,
                report_text=report_text,
                prompt_submission_choices=True,
                shared_session=should_use_shared_session(args),
                shared_remote_session=should_use_shared_remote_session(args),
            )
            print_fxiaoke_results(
                fxiaoke_results, details=args.details, compact=not args.details,
            )
            return fxiaoke_results_exit_code(fxiaoke_results)

        quick_fields: dict[str, str] = {}
        raw_input_text = read_report_input(args, structured_fields=quick_fields)
        if not raw_input_text:
            print("\n输入内容为空。")
            return 0

        # Quick answers already have field boundaries; reparsing their text can
        # drop blank-line continuations or mistake body text for another label.
        structured_fields = quick_fields or parse_structured_brief_input(raw_input_text)
        if structured_fields:
            structured_fields = enrich_structured_fields(structured_fields, report_path)

        if structured_fields:
            print("\n[AI 正在润色日报，保留原始事实与确定程度...]")
        else:
            print("\n[AI 正在按近 3 篇历史日报生成日报，完成后将显示耗时...]")
        generation_started_at = time.monotonic()
        result = generate_report(
            raw_input_text,
            report_path,
            structured_fields=structured_fields,
            structured_fields_prepared=bool(structured_fields),
        )
        generation_elapsed = time.monotonic() - generation_started_at
        generation_label = "日报润色与整理完成" if structured_fields else "AI 日报生成完成"
        print(f"[{generation_label}，用时 {generation_elapsed:.1f} 秒。]")
        render_polish_diff_preview(result, structured_fields)

        print("\n" + "=" * 50)
        print(result)
        print("=" * 50)

        if args.preview_only:
            print("\n[预览模式] 未写入文件。")
            return 0

        auto_confirm = should_auto_confirm(args)
        if not auto_confirm and not auto_sync_enabled:
            confirm = read_brief_input("\n是否写入日报文件？(y/n): ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("\n已取消保存。")
                return 0
        elif not auto_confirm:
            answer = read_brief_input("\n回车保存并选择提交项目 / Q 取消：").strip().lower()
            if answer in {"q", "quit", "取消"}:
                print("\n已取消保存。")
                return 0
        elif not args.yes:
            print("\n[提示] 检测到非交互输入，已自动写入；如仅需预览可使用 --preview-only。")

        save_action = save_report(
            result,
            report_path,
            structured_fields=structured_fields,
        )
        from .quick_drafts import clear_quick_draft_after_save, QuickDraftError
        try:
            clear_quick_draft_after_save(report_path)
        except (QuickDraftError, OSError) as exc:
            print(f"[草稿] 日报已保存，草稿保留：{exc}")
        clipboard_copied = False
        if not args.no_clipboard:
            clipboard_copied = copy_to_clipboard(result)

        if args.no_clipboard:
            print(f"\n[成功] 日报已{save_action}到文件。")
        elif clipboard_copied:
            print(f"\n[成功] 日报已{save_action}到文件，并复制到剪贴板。")
        else:
            print(f"\n[成功] 日报已{save_action}到文件，但复制剪贴板失败。")

        fxiaoke_results = []
        if auto_sync_enabled:
            if auto_confirm:
                print("\n[正在同步纷享销客日报与技术服务记录...]")
                fxiaoke_results = sync_fxiaoke_after_save_fast(
                    result,
                    report_path,
                    structured_fields=structured_fields,
                    profile_dir=fxiaoke_profile_dir,
                )
            else:
                fxiaoke_results = sync_fxiaoke_preflighted_batch(
                    result,
                    report_path,
                    prompt_submission_choices=True,
                    structured_fields=structured_fields,
                    profile_dir=fxiaoke_profile_dir,
                    shared_session=should_use_shared_session(args),
                    shared_remote_session=should_use_shared_remote_session(args),
                )

        if fxiaoke_results:
            print_fxiaoke_results(
                fxiaoke_results, details=args.details, compact=not args.details,
            )
        return fxiaoke_results_exit_code(fxiaoke_results)
    except EOFError:
        emit_terminal_message("\n输入已结束，已安全退出。")
        return 0
    except KeyboardInterrupt:
        print("\n已安全退出。")
        return 130
    except Exception as exc:
        print(f"\n程序运行出错：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
