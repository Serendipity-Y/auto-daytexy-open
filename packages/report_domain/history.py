from __future__ import annotations

import json
import hashlib
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import ModuleType

try:
    import fcntl as _fcntl
    fcntl: ModuleType | None = _fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

from .report_format import normalize_report_date_to_iso, parse_report_fields


FXIAOKE_PENDING_QUEUE_NAME = "fxiaoke_pending_queue.jsonl"
FXIAOKE_SUBMISSION_HISTORY_NAME = "fxiaoke_submission_history.jsonl"
FXIAOKE_HISTORY_RECORDABLE_STATUSES = {
    "submitted", "already_exists", "voided", "invalidated", "deleted",
    "cancelled", "作废", "已作废",
}
PENDING_TASK_DETAIL_FIELDS = (
    "status",
    "submission_phase",
    "retry_action",
    "workorder_id",
    "service_record_id",
    "feed_id",
    "crm_customer",
    "content_digest",
    "completion_status",
    "work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source",
)
# A batch's CRM evidence belongs to its individual children, never the aggregate.
FXIAOKE_CHILD_EVIDENCE_FIELDS = (frozenset(PENDING_TASK_DETAIL_FIELDS) - {"status"}) | {"includes_main_work"}
PENDING_QUEUE_LOCK_ROOT = Path("~/.config/auto-daytexy/report_locks").expanduser()


def resolve_fxiaoke_pending_queue_path(report_path) -> Path:
    return Path(report_path).parent / FXIAOKE_PENDING_QUEUE_NAME


def resolve_fxiaoke_submission_history_path(report_path) -> Path:
    return Path(report_path).parent / FXIAOKE_SUBMISSION_HISTORY_NAME


def _write_text_atomically(path, text):
    path = Path(path)
    path = path.resolve(strict=path.is_symlink())
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, existing_mode)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def read_pending_queue_text(queue_path):
    queue_path = Path(queue_path)
    try:
        return queue_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if queue_path.is_symlink():
            raise OSError(f"待补交文件链接失效，已停止处理并保留原链接：{queue_path}") from exc
        return ""
    except UnicodeError as exc:
        raise OSError(f"待补交文件无法按 UTF-8 读取，已停止处理：{queue_path}") from exc


def _read_pending_queue_lines(queue_path):
    return [line for line in read_pending_queue_text(queue_path).splitlines() if line.strip()]


@contextmanager
def _pending_queue_lock(queue_path):
    lock_key = hashlib.sha256(str(Path(queue_path).resolve()).encode("utf-8")).hexdigest()
    lock_path = PENDING_QUEUE_LOCK_ROOT / f"{lock_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _filter_pending_queue_lines(
    lines, step_key=None, report_date=None, main_only=None, service_customer=None,
    *, expected_line=None, includes_main_work=None, preserve_review=False,
):
    remaining_lines = []
    removed_count = 0
    target_date = (report_date or "").strip()
    for raw_line in lines:
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            remaining_lines.append(raw_line)
            continue
        if not isinstance(item, dict):
            remaining_lines.append(raw_line)
            continue
        matches_step = not step_key or item.get("step") == step_key
        matches_date = not target_date or item.get("report_date") == target_date
        matches_main_only = main_only is None or bool(item.get("main_only")) == bool(main_only)
        matches_customer = service_customer is None or str(item.get("service_customer", "")).strip() == str(service_customer).strip()
        matches_version = expected_line is None or raw_line == expected_line
        matches_role = item.get("step") != "remote_workorder" or item.get("includes_main_work") is includes_main_work
        if (matches_step and matches_date and matches_main_only and matches_customer
                and matches_version and matches_role and not (preserve_review and _needs_crm_recheck(item))):
            removed_count += 1
            continue
        remaining_lines.append(raw_line if expected_line is not None else json.dumps(item, ensure_ascii=False))
    return remaining_lines, removed_count


def _write_pending_queue_lines(queue_path, lines):
    if lines or queue_path.is_symlink():
        _write_text_atomically(queue_path, "\n".join(lines) + "\n" if lines else "")
    else:
        queue_path.unlink(missing_ok=True)


def record_fxiaoke_submission_history(report_text, report_path, result):
    children = result.get("results")
    if isinstance(children, list) and children:
        if any(not isinstance(child, dict) for child in children):
            raise RuntimeError("同步结果列表存在非对象记录，已停止写入历史。")
        base = {key: value for key, value in result.items() if key not in {"results", "ok"} and key not in FXIAOKE_CHILD_EVIDENCE_FIELDS}
        last_entry = None
        for child in children:
            if child.get("ok") is False:
                continue
            item = {**base, **child}
            item["service_customer"] = child.get("service_customer") or child.get("customer_name") or base.get("service_customer", "")
            entry = record_fxiaoke_submission_history(report_text, report_path, item)
            last_entry = entry or last_entry
        return last_entry
    step = str(result.get("step", "")).strip()
    status = str(result.get("status", "")).strip()
    if step not in {"daily_log", "service_record", "remote_workorder"} or status not in FXIAOKE_HISTORY_RECORDABLE_STATUSES:
        return None
    if status in {"submitted", "already_exists"} and "ok" in result:
        ok_value = result["ok"]
        if not (ok_value is True or (type(ok_value) is int and ok_value == 1)):
            return None

    parsed = parse_report_fields(report_text)
    entry = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "report_date": normalize_report_date_to_iso(report_text),
        "step": step,
        "status": status,
        "service_customer": str(result.get("service_customer") or parsed.get("服务客户", "")).strip(),
        "message": str(result.get("message", "")),
    }
    for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer", "service_category", "service_hours", "content_digest", "completion_status", "work_type", "routing_policy", "routing_target", "source_checkin_workorder_id", "routing_source"):
        if result.get(key):
            entry[key] = result[key]
    role = result.get("includes_main_work")
    if step == "remote_workorder" and isinstance(role, bool):
        entry["includes_main_work"] = role

    history_path = resolve_fxiaoke_submission_history_path(report_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with _pending_queue_lock(history_path):
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return entry


def extract_fxiaoke_service_mode_flags(payload):
    command = payload.get("command", []) if isinstance(payload, dict) else []
    command = command if isinstance(command, list) else []
    return {"main_only": bool(payload.get("main_only")) or "--main-only" in command}


def clear_pending_fxiaoke_tasks(
    report_path, step_key=None, report_date=None, main_only=None, service_customer=None,
    *, expected_line=None, includes_main_work: bool | None = None,
):
    """Clear matching tasks; a missing remote role matches only legacy unknown roles."""
    if expected_line is not None and not isinstance(expected_line, str):
        raise ValueError("待办核验前快照必须是原始记录文本，已保留待办。")
    if includes_main_work is not None and not isinstance(includes_main_work, bool):
        raise ValueError("远程工单待办角色必须是布尔值或未知，已保留待办。")
    queue_path = resolve_fxiaoke_pending_queue_path(report_path)
    with _pending_queue_lock(queue_path):
        if not queue_path.exists() and not queue_path.is_symlink():
            return 0

        remaining_lines, removed_count = _filter_pending_queue_lines(
            _read_pending_queue_lines(queue_path),
            step_key=step_key,
            report_date=report_date,
            main_only=main_only,
            service_customer=service_customer,
            expected_line=expected_line,
            includes_main_work=includes_main_work,
        )
        _write_pending_queue_lines(queue_path, remaining_lines)
        return removed_count


def _service_record_recovery_evidence(item):
    fields = ("report_date", "workorder_id", "service_record_id", "content_digest")
    values = [item.get(field) for field in fields]
    if any(value is not None and not isinstance(value, str) for value in values):
        return None
    report_date, workorder_id, record_id, digest = [(value or "").strip() for value in values]
    if not report_date or not workorder_id or not (record_id or digest):
        return None
    return report_date, workorder_id, record_id, digest


def _needs_crm_recheck(item):
    return (
        str(item.get("retry_action") or "").strip() == "verify_crm"
        or item.get("status") in ("uncertain", "crm_id_captured", "timeout", "evidence_error")
        or item.get("submission_phase") in ("submit_clicked", "crm_id_captured")
        or bool(item.get("routing_target"))
    )


def reconcile_pending_service_records_from_history(report_path):
    history_path = resolve_fxiaoke_submission_history_path(report_path)
    queue_path = resolve_fxiaoke_pending_queue_path(report_path)
    if not history_path.exists() or (not queue_path.exists() and not queue_path.is_symlink()):
        return []

    confirmed_records: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for raw_line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if (
            item.get("step") != "service_record"
            or item.get("status") not in ("submitted", "already_exists")
            or ("ok" in item and not (item["ok"] is True or type(item["ok"]) is int and item["ok"] == 1))
        ):
            continue
        evidence = _service_record_recovery_evidence(item)
        if evidence is not None:
            report_date, workorder_id, record_id, digest = evidence
            if record_id:
                confirmed_records.setdefault((report_date, workorder_id), []).append((record_id, digest))

    if not confirmed_records:
        return []

    reconciled_workorders = []
    with _pending_queue_lock(queue_path):
        remaining_lines = []
        for raw_line in _read_pending_queue_lines(queue_path):
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                remaining_lines.append(raw_line)
                continue
            if (
                not isinstance(item, dict)
                or item.get("step") != "service_record"
                or _needs_crm_recheck(item)
            ):
                remaining_lines.append(raw_line)
                continue
            evidence = _service_record_recovery_evidence(item)
            if evidence is None:
                remaining_lines.append(raw_line)
                continue
            report_date, workorder_id, pending_record_id, pending_digest = evidence
            if any(
                (not pending_record_id or pending_record_id == record_id)
                and (not pending_digest or pending_digest == content_digest)
                for record_id, content_digest in confirmed_records.get(
                    (report_date, workorder_id), []
                )
            ):
                reconciled_workorders.append(workorder_id)
                continue
            remaining_lines.append(raw_line)
        if reconciled_workorders:
            _write_pending_queue_lines(queue_path, remaining_lines)
    return reconciled_workorders


def record_pending_fxiaoke_task(report_text, report_path, step_key, step_label, payload):
    children = payload.get("results")
    if isinstance(children, list) and children:
        if any(not isinstance(child, dict) for child in children):
            raise RuntimeError("同步结果列表存在非对象记录，已停止更新待补交。")
        base = {key: value for key, value in payload.items() if key != "results" and key not in FXIAOKE_CHILD_EVIDENCE_FIELDS}
        last_entry = None
        queue_path = resolve_fxiaoke_pending_queue_path(report_path)
        for child in children:
            if child.get("status") in {"submitted", "already_exists", "skipped", "preview", "cancelled"}:
                continue
            item = {**base, **child}
            item["service_customer"] = child.get("service_customer") or child.get("customer_name") or base.get("service_customer", "")
            last_entry, queue_path = record_pending_fxiaoke_task(report_text, report_path, step_key, step_label, item)
        return last_entry, queue_path
    queue_path = resolve_fxiaoke_pending_queue_path(report_path)
    parsed = parse_report_fields(report_text)
    report_date = normalize_report_date_to_iso(report_text)
    mode_flags = extract_fxiaoke_service_mode_flags(payload or {})
    service_customer = str((payload or {}).get("service_customer") or parsed.get("服务客户", "")).strip()
    role = payload.get("includes_main_work")
    role = role if isinstance(role, bool) else None
    clear_kwargs = {}
    if step_key == "service_record":
        clear_kwargs = {**mode_flags, "service_customer": service_customer}
    elif step_key == "remote_workorder":
        clear_kwargs = {"service_customer": service_customer, "includes_main_work": role}
    entry = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "report_date": report_date,
        "service_customer": service_customer,
        "step": step_key,
        "step_label": step_label,
        "message": payload.get("message", ""),
        "artifacts": payload.get("artifacts", []),
        "report_path": str(report_path),
    }
    if mode_flags["main_only"]:
        entry["main_only"] = True
    if step_key == "remote_workorder" and role is not None:
        entry["includes_main_work"] = role
    for key in PENDING_TASK_DETAIL_FIELDS:
        value = (payload or {}).get(key)
        if value not in (None, "", [], {}):
            entry[key] = value
    with _pending_queue_lock(queue_path):
        remaining_lines, _ = _filter_pending_queue_lines(
            _read_pending_queue_lines(queue_path),
            step_key=step_key,
            report_date=report_date,
            preserve_review=step_key == "remote_workorder" and role is not None and not _needs_crm_recheck(entry),
            **clear_kwargs,
        )
        _write_pending_queue_lines(queue_path, [*remaining_lines, json.dumps(entry, ensure_ascii=False)])
    return entry, queue_path
