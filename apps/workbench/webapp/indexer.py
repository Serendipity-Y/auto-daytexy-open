from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from json import JSONDecodeError
from pathlib import Path

from packages.report_domain.history import (
    read_pending_queue_text,
    resolve_fxiaoke_pending_queue_path,
    resolve_fxiaoke_submission_history_path,
)
from packages.report_domain.paths import resolve_fxiaoke_trace_root
from packages.report_domain.report_format import (
    normalize_report_date_to_iso,
    parse_report_fields,
    parse_report_service_entries,
    split_report_sections,
)
from packages.report_domain.workday import PREVIEW_JSON_FILENAME, is_onsite_workday

from .jobs import TERMINAL_JOB_STATUSES

ALLOWED_STEPS = {"daily_log", "service_record", "remote_workorder"}
SUCCESS_HISTORY_STATUSES = {"submitted", "already_exists"}
ARTIFACT_FILE_TYPES = {
    ".html": "html",
    ".htm": "html",
    ".png": "screenshot",
}
ARTIFACT_REPORT_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
WORKORDER_ID_PATTERN = re.compile(r"\b(JSFW\d{8}-\d+)\b")
SERVICE_RECORD_ID_PATTERN = re.compile(r"\b(FWJL-\d{8}-\d+)\b")


@dataclass(frozen=True)
class IndexSummary:
    report_days: int
    submissions: int
    pending: int
    artifacts: int
    warnings: list[str]
    pending_snapshot_complete: bool = True


class HistoryIndexer:
    def __init__(self, store, report_path):
        self.store = store
        self.report_path = Path(report_path).expanduser()
        self.history_path = resolve_fxiaoke_submission_history_path(self.report_path)
        self.pending_path = resolve_fxiaoke_pending_queue_path(self.report_path)
        self.artifact_root = self.report_path.parent.resolve() / "fxiaoke_failure_artifacts"
        self._trace_root_warning = None
        self.trace_root: Path | None = None
        try:
            self.trace_root = resolve_fxiaoke_trace_root(self.artifact_root)
        except ValueError as exc:
            self.trace_root = None
            self._trace_root_warning = f"private trace indexing disabled: {exc}"
        self._sync_lock = threading.Lock()
        self._last_signature = None
        self._last_summary: IndexSummary | None = None

    def sync(self) -> IndexSummary:
        with self._sync_lock:
            signature, artifact_snapshot = self._input_signature_and_artifacts()
            if signature == self._last_signature and self._last_summary is not None:
                return self._last_summary

            warnings: list[str] = [self._trace_root_warning] if self._trace_root_warning else []
            report_days, report_dates, report_count, report_complete = (
                self._read_report_days(_now_iso(), warnings)
            )
            history_rows, submission_count, discarded_history_sources, history_complete = (
                self._read_submission_history(report_dates, warnings)
            )
            pending_rows, pending_count, protected_paths, pending_complete = (
                self._read_pending_queue(report_dates, warnings)
            )
            protected_paths.update(self._job_artifact_paths())
            artifact_rows, artifact_count = self._read_artifacts(
                protected_paths,
                warnings,
                file_snapshot=artifact_snapshot,
            )

            final_signature = self._input_signature()
            if final_signature != signature:
                raise RuntimeError("索引来源在刷新期间发生变化；本次没有写入索引，请稍后刷新。")

            active_pending_sources = {
                row["source"]
                for row in pending_rows
                if str(row.get("source", "")).startswith("pending_queue:")
            }
            self.store.apply_index_snapshot(
                report_days=report_days,
                submissions=[*history_rows, *pending_rows],
                artifacts=artifact_rows,
                active_pending_sources=active_pending_sources,
                # 历史段落的个别缺字段不应阻断待补交快照的对账；只有日报完全读不到
                # 或整份内容无法解析出任何日期时才保留旧证据。
                reconcile_pending=(report_complete or bool(report_dates)) and pending_complete,
                reconcile_artifact_protection=pending_complete,
                discard_history_sources=(
                    discarded_history_sources if report_complete and history_complete else ()
                ),
            )
            summary = IndexSummary(
                report_days=report_count,
                submissions=submission_count,
                pending=pending_count,
                artifacts=artifact_count,
                warnings=warnings,
                pending_snapshot_complete=pending_complete,
            )
            self._last_signature = (
                final_signature
                if report_complete and pending_complete and history_complete
                else None
            )
            self._last_summary = summary
            return summary

    def _input_signature(self) -> tuple:
        return (
            *self._non_artifact_input_signature(),
            _artifact_tree_signature(self.artifact_root),
            _artifact_tree_signature(self.trace_root) if self.trace_root else None,
        )

    def _input_signature_and_artifacts(self) -> tuple[tuple, tuple]:
        artifact_signature, artifact_snapshot = _artifact_tree_snapshot(
            self.artifact_root
        )
        trace_signature, trace_snapshot = (
            _artifact_tree_snapshot(self.trace_root) if self.trace_root else (None, ())
        )
        return (
            (*self._non_artifact_input_signature(), artifact_signature, trace_signature),
            (*artifact_snapshot, *trace_snapshot),
        )

    def _non_artifact_input_signature(self) -> tuple:
        return (
            _path_signature(self.report_path),
            _path_signature(self.history_path),
            _path_signature(self.pending_path),
            _path_signature(self.report_path.resolve().with_name(PREVIEW_JSON_FILENAME)),
            tuple(sorted(self._job_artifact_paths())),
        )

    def _job_artifact_paths(self) -> set[str]:
        paths = set()
        for job in self.store.list_jobs():
            if job["status"] not in TERMINAL_JOB_STATUSES or job["status"] == "uncertain":
                paths.update(self._collect_pending_artifact_paths(job.get("result_payload", {})))
        return paths

    def _read_report_days(
        self,
        indexed_at: str,
        warnings: list[str],
    ) -> tuple[list[dict], set[str], int, bool]:
        if not self.report_path.exists():
            warnings.append(f"report file not found: {self.report_path}")
            return [], set(), 0, False

        try:
            content = self.report_path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"failed to read report file {self.report_path}: {exc}")
            return [], set(), 0, False

        report_dates: set[str] = set()
        records_by_date: dict[str, dict] = {}
        seen_dates: set[str] = set()
        sections = split_report_sections(content)
        complete = not content.strip() or bool(sections)
        if content.strip() and not sections:
            warnings.append("report file contains no dated sections")
        for index, section in enumerate(sections, start=1):
            try:
                report_date = normalize_report_date_to_iso(section)
            except Exception as exc:
                warnings.append(f"report section {index} has invalid date: {exc}")
                complete = False
                continue

            if report_date in seen_dates:
                records_by_date.pop(report_date, None)
                report_dates.discard(report_date)
                warnings.append(f"duplicate report date {report_date}; unique content cannot be determined")
                complete = False
                continue
            seen_dates.add(report_date)

            fields = parse_report_fields(section)
            customer_name = str(fields.get("服务客户", "")).strip()
            if not customer_name:
                warnings.append(f"report section {report_date} is missing 服务客户")
                complete = False
                continue

            is_onsite = is_onsite_workday(
                report_text=section,
                structured_fields=fields,
                report_path=self.report_path,
            )
            entries = parse_report_service_entries(section) or [fields]
            main_service_customers = list(dict.fromkeys(
                str(entry.get("服务客户") or customer_name).strip()
                for entry in entries if entry.get("处理情况", "").strip()
            ))
            records_by_date[report_date] = {
                "report_date": report_date,
                "report_path": str(self.report_path),
                "customer_name": customer_name,
                "is_onsite": int(bool(is_onsite)),
                "content_digest": _sha256_text(section),
                "indexed_at": indexed_at,
                "main_service_customers": main_service_customers,
            }
            report_dates.add(report_date)

        return list(records_by_date.values()), report_dates, len(records_by_date), complete

    def _read_submission_history(
        self,
        report_dates: set[str],
        warnings: list[str],
    ) -> tuple[list[dict], int, set[str], bool]:
        count = 0
        records = []
        discarded_sources: set[str] = set()
        complete = True
        for item, raw_line, line_number in _iter_jsonl(
            self.history_path,
            "history",
            warnings,
        ):
            report_date = str(item.get("report_date", "")).strip()
            if not report_date:
                warnings.append(
                    f"history line {line_number} is missing report_date in {self.history_path.name}"
                )
                complete = False
                continue
            if report_date not in report_dates:
                warnings.append(
                    f"history line {line_number} references absent report date {report_date}"
                )
                continue

            step = str(item.get("step", "")).strip()
            if step not in ALLOWED_STEPS:
                warnings.append(
                    f"history line {line_number} has unsupported step {step or '<empty>'}"
                )
                complete = False
                continue

            history_status = str(item.get("status", "")).strip()
            if history_status not in SUCCESS_HISTORY_STATUSES:
                continue
            if "ok" in item:
                ok = item["ok"]
                if not (ok is True or (type(ok) is int and ok == 1)):
                    discarded_sources.add(f"submission_history:{_sha256_text(raw_line)}")
                    warnings.append(f"history line {line_number} has an explicit non-success ok flag; ignored")
                    continue

            records.append(
                {
                    "report_date": report_date,
                    "step": step,
                    "service_customer": str(item.get("service_customer", "")).strip(),
                    "content_digest": str(item.get("content_digest", "")).strip(),
                    "status": "submitted",
                    "workorder_id": _history_crm_identifier(
                        item,
                        "workorder_id",
                        WORKORDER_ID_PATTERN,
                    ),
                    "service_record_id": _history_crm_identifier(
                        item,
                        "service_record_id",
                        SERVICE_RECORD_ID_PATTERN,
                    ),
                    "service_hours": item.get("service_hours"),
                    "crm_customer": str(item.get("crm_customer", "")).strip() or None,
                    "service_category": str(item.get("service_category", "")).strip()
                    or None,
                    "local_recorded_at": str(item.get("created_at", "")).strip() or None,
                    "source": f"submission_history:{_sha256_text(raw_line)}",
                    "includes_main_work": item.get("includes_main_work"),
                }
            )
            count += 1
        complete = complete and not any(
            warning.startswith(("failed to read history", "malformed history"))
            for warning in warnings
        )
        return records, count, discarded_sources, complete

    def _read_pending_queue(
        self,
        report_dates: set[str],
        warnings: list[str],
    ) -> tuple[list[dict], int, set[str], bool]:
        count = 0
        records = []
        protected_paths: set[str] = set()
        pending_rows, can_reconcile = self._load_pending_snapshot(warnings)
        for item, raw_line, line_number in pending_rows:
            protected_paths.update(self._collect_pending_artifact_paths(item))
            report_date = str(item.get("report_date", "")).strip()
            if not report_date:
                warnings.append(
                    f"pending line {line_number} is missing report_date in {self.pending_path.name}"
                )
                can_reconcile = False
                continue
            if report_date not in report_dates:
                warnings.append(
                    f"pending line {line_number} references absent report date {report_date}"
                )
                can_reconcile = False
                continue

            step = str(item.get("step", "")).strip()
            if step not in ALLOWED_STEPS:
                warnings.append(
                    f"pending line {line_number} has unsupported step {step or '<empty>'}"
                )
                can_reconcile = False
                continue

            source = f"pending_queue:{_sha256_text(raw_line)}"
            record = {
                "report_date": report_date,
                "step": step,
                "service_customer": str(item.get("service_customer", "")).strip(),
                "content_digest": str(item.get("content_digest", "")).strip(),
                "status": "pending_retry",
                "local_recorded_at": str(item.get("created_at", "")).strip() or None,
                "source": source,
                "includes_main_work": item.get("includes_main_work"),
            }
            records.append(record)
            count += 1
        return records, count, protected_paths, can_reconcile

    def _load_pending_snapshot(
        self,
        warnings: list[str],
    ) -> tuple[list[tuple[dict, str, int]], bool]:
        try:
            lines = read_pending_queue_text(self.pending_path).splitlines()
        except OSError as exc:
            warnings.append(f"failed to read pending file {self.pending_path}: {exc}")
            return [], False

        if not any(line.strip() for line in lines):
            return [], True

        parsed_rows: list[tuple[dict, str, int]] = []
        snapshot_complete = True
        for line_number, raw_line in enumerate(lines, start=1):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line)
            except JSONDecodeError:
                warnings.append(
                    f"malformed pending line {line_number} in {self.pending_path.name}"
                )
                snapshot_complete = False
                continue
            if not isinstance(item, dict):
                warnings.append(
                    f"malformed pending line {line_number} in {self.pending_path.name}"
                )
                snapshot_complete = False
                continue
            if "artifacts" in item and not isinstance(item["artifacts"], list):
                warnings.append(f"malformed pending artifacts at line {line_number} in {self.pending_path.name}")
                snapshot_complete = False
            parsed_rows.append((item, raw_line, line_number))
        return parsed_rows, snapshot_complete

    def _collect_pending_artifact_paths(self, item: dict) -> set[str]:
        protected_paths: set[str] = set()
        artifacts = item.get("artifacts", [])
        if not isinstance(artifacts, list):
            return protected_paths

        for artifact in artifacts:
            text = str(artifact or "").strip()
            if not text:
                continue
            protected_paths.add(str(Path(text).expanduser().resolve(strict=False)))
        return protected_paths

    def _read_artifacts(
        self,
        protected_paths: set[str],
        warnings: list[str],
        file_snapshot: tuple | None = None,
    ) -> tuple[list[dict], int]:
        count = 0
        records = []
        candidates = file_snapshot
        if candidates is None:
            candidates = tuple(
                item for root in (self.artifact_root, self.trace_root)
                if root is not None
                for item in _artifact_tree_snapshot(root)[1]
            )
        for candidate, metadata in candidates:
            root = (
                self.trace_root if self.trace_root is not None
                and _is_path_within_root(candidate, self.trace_root) else self.artifact_root
            )
            root_resolved = root
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    outside = not _is_path_within_root(candidate.resolve(), root_resolved)
                except (OSError, RuntimeError):
                    outside = False
                label = "symbolic artifact link outside artifact root" if outside else "symbolic artifact link"
                warnings.append(f"{label} skipped: {candidate}")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue

            resolved_path = candidate.resolve()
            if not _is_path_within_root(resolved_path, root_resolved):
                warnings.append(
                    f"outside artifact root skipped: {candidate}"
                )
                continue

            step = _infer_artifact_step(candidate.parent.name)
            if step is None:
                warnings.append(
                    f"unknown artifact step for directory {candidate.parent.name}"
                )
                continue

            file_type = (
                "trace" if root == self.trace_root and candidate.name == "trace.zip"
                else ARTIFACT_FILE_TYPES.get(candidate.suffix.lower()) if root == self.artifact_root
                else None
            )
            if file_type is None:
                warnings.append(f"unsupported artifact file type skipped: {candidate.name}")
                continue

            created_at_dt = datetime.fromtimestamp(metadata.st_mtime)
            report_date = _infer_report_date(candidate.parent.name)
            records.append(
                {
                    "job_id": candidate.parent.name,
                    "report_date": report_date,
                    "step": step,
                    "file_path": str(resolved_path),
                    "file_type": file_type,
                    "size_bytes": metadata.st_size,
                    "created_at": created_at_dt.isoformat(timespec="seconds"),
                    "expires_at": (created_at_dt + timedelta(days=30)).isoformat(
                        timespec="seconds"
                    ),
                    "protected_by_pending": str(resolved_path) in protected_paths,
                }
            )
            count += 1
        return records, count


def _iter_jsonl(path: Path, label: str, warnings: list[str]):
    if not path.exists():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        warnings.append(f"failed to read {label} file {path}: {exc}")
        return

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except JSONDecodeError:
            warnings.append(f"malformed {label} line {line_number} in {path.name}")
            continue
        if not isinstance(item, dict):
            warnings.append(f"malformed {label} line {line_number} in {path.name}")
            continue
        yield item, raw_line, line_number


def _path_signature(path: Path) -> tuple:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return str(path), None
    return (
        str(path),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _artifact_tree_signature(root: Path) -> tuple:
    return _artifact_tree_snapshot(root)[0]


def _artifact_tree_snapshot(root: Path) -> tuple[tuple, tuple]:
    snapshot = []
    signatures = []
    # Never descend through links, including a replaced root or private parent.
    for parent in (*reversed(root.parents), root):
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            return ((str(root), None), ()), ()
        if stat.S_ISLNK(metadata.st_mode):
            signature = (str(parent), metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns)
            return (signature, ()), ((parent, metadata),)
    root_signature = _path_signature(root)
    for directory, subdirs, filenames in os.walk(root, followlinks=False):
        for name in sorted([*subdirs, *filenames]):
            candidate = Path(directory) / name
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                continue
            snapshot.append((candidate, metadata))
            signatures.append(
                (
                    candidate.relative_to(root).as_posix(),
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
    return (root_signature, tuple(signatures)), tuple(snapshot)


def _infer_artifact_step(directory_name: str) -> str | None:
    matches: list[str] = []
    if "daily_log" in directory_name:
        matches.append("daily_log")
    if (
        "remote_workorder" in directory_name
        or "_remote_hours_read" in directory_name
        or "_remote_hours_sync" in directory_name
    ):
        matches.append("remote_workorder")
    if "service_record" in directory_name or "_fw_" in directory_name:
        matches.append("service_record")
    if len(matches) != 1:
        return None
    return matches[0]


def _infer_report_date(directory_name: str) -> str | None:
    match = ARTIFACT_REPORT_DATE_PATTERN.search(directory_name)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _is_path_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _history_crm_identifier(item: dict, field_name: str, pattern: re.Pattern) -> str | None:
    explicit = str(item.get(field_name, "") or "").strip()
    if explicit:
        match = pattern.search(explicit)
        return match.group(1) if match else explicit
    message = str(item.get("message", "") or "")
    match = pattern.search(message)
    return match.group(1) if match else None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
