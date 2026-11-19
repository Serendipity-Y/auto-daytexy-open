from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from collections.abc import Mapping
from datetime import date as local_date, datetime
from pathlib import Path
from typing import Any, Final, Literal, NotRequired, TypedDict

from .china_holidays import automatic_day_status, is_china_workday
from .knowledge_base import KNOWLEDGE_BASE_SYNC_ACTION, parse_metadata_timestamp


SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 30_000
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
_UNSET = object()
SUBMISSION_STEPS = ("daily_log", "service_record", "remote_workorder")
MANUAL_OVERRIDE_STEPS = frozenset({"daily_log", "service_record"})
MANUAL_OVERRIDE_STATUS: Final = "manual_confirmed"
MANUAL_OVERRIDE_SOURCE_PREFIX = "manual_override:"
CALENDAR_DAY_STATUS_LABELS = {
    "leave": "请假",
    "company_office": "公司办公",
    "company_meeting": "公司会议",
    "company_activity": "公司活动",
}
COMPANY_OFFICE_CALENDAR_DAY_STATUSES = frozenset(
    {"company_office", "company_meeting", "company_activity"}
)
CRM_VERIFICATION_ACTION = "crm_verification"
DELETABLE_CRM_VERIFICATION_STATUSES = frozenset(
    {"not_found", "unknown", "pending_retry", "uncertain"}
)
REMOTE_HOUR_SYNC_STATUSES = frozenset(
    {
        "local_only",
        "sync_queued",
        "crm_verified",
        "crm_mismatch",
        "pending_retry",
        "uncertain",
        "not_found",
    }
)
SUBMITTED_BUSINESS_STATUSES = frozenset(
    {"local_recorded", "submitted", "crm_verified", "manual_confirmed"}
)
MAIN_SERVICE_REVIEW_STATUSES = frozenset(
    {"crm_id_captured", "uncertain", "conflict", "not_found", "manual_conflict", "legacy_ambiguous"}
)
# 本地尝试失败状态可以被同客户的成功回执覆盖；CRM 只读结论（如 not_found）
# 和路由冲突仍保留，避免掩盖真实的 CRM 差异。
SUPERSEDABLE_FAILURE_STATUSES = frozenset({"pending_retry", "uncertain", "unknown", "skipped"})
STATUS_PRIORITY = {
    "unknown": 0,
    "skipped": 10,
    "not_found": 20,
    "pending_retry": 30,
    "awaiting_confirmation": 40,
    "queued": 50,
    "running": 60,
    "crm_id_captured": 70,
    "uncertain": 80,
    "conflict": 80,
    "local_recorded": 100,
    "submitted": 110,
    "manual_confirmed": 115,
    "crm_verified": 120,
    "completed": 120,
}
STEP_STATUS_KEYS = {
    "daily_log": "daily_log_status",
    "service_record": "onsite_service_status",
    "remote_workorder": "remote_workorder_status",
}
STEP_COUNT_KEYS = {
    "daily_log": "daily_log_count",
    "service_record": "onsite_service_count",
    "remote_workorder": "remote_workorder_count",
}
STEP_ORDER = {
    "daily_log": 0,
    "service_record": 1,
    "remote_workorder": 2,
}
JOB_DEDUPLICATION_STATUSES = (
    "awaiting_confirmation",
    "queued",
    "running",
    "crm_id_captured",
    "uncertain",
)
DELETABLE_JOB_RECORD_STATUSES = frozenset(
    {
        "queued",
        "pending_retry",
        "crm_verified",
        "skipped",
        "not_found",
        "unknown",
        "completed",
    }
)
ACTIVE_REMOTE_HOUR_JOB_STATUSES = ("queued", "running", "uncertain")

BusinessStatus = Literal[
    "unknown",
    "not_found",
    "local_recorded",
    "submitted",
    "awaiting_confirmation",
    "queued",
    "running",
    "crm_id_captured",
    "crm_verified",
    "pending_retry",
    "manual_confirmed",
    "uncertain",
    "conflict",
    "skipped",
]


BUSINESS_STATUSES: set[BusinessStatus] = {
    "unknown",
    "not_found",
    "local_recorded",
    "submitted",
    "awaiting_confirmation",
    "queued",
    "running",
    "crm_id_captured",
    "crm_verified",
    "pending_retry",
    "manual_confirmed",
    "uncertain",
    "conflict",
    "skipped",
}


class ReportDayRecord(TypedDict):
    report_date: str
    report_path: str
    customer_name: str
    is_onsite: int | bool
    content_digest: str
    indexed_at: str
    main_service_customers: NotRequired[list[str]]


class SubmissionRecord(TypedDict, total=False):
    report_date: str
    step: str
    service_customer: str
    content_digest: str
    status: BusinessStatus
    workorder_id: str
    service_record_id: str
    service_hours: float
    crm_customer: str
    service_category: str
    local_recorded_at: str
    crm_verified_at: str
    source: str
    includes_main_work: bool | None


class ArtifactRecord(TypedDict, total=False):
    job_id: str
    report_date: str
    step: str
    file_path: str
    file_type: str
    size_bytes: int
    created_at: str
    expires_at: str
    protected_by_pending: int | bool
    deleted_at: NotRequired[str]


class KnowledgeBasePageRecord(TypedDict):
    page_id: str
    title: str
    space_key: str
    space_name: str
    creator_user_key: str
    creator_display_name: str
    created_at: str
    modified_at: str
    page_url: str
    indexed_at: str


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    meta_id INTEGER PRIMARY KEY CHECK (meta_id = 1),
    schema_version INTEGER NOT NULL,
    initialized_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_days (
    report_date TEXT PRIMARY KEY,
    report_path TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    is_onsite INTEGER NOT NULL CHECK (is_onsite IN (0, 1)),
    content_digest TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    main_service_customers TEXT
);

CREATE TABLE IF NOT EXISTS submission_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    step TEXT NOT NULL CHECK (step IN ('daily_log', 'service_record', 'remote_workorder')),
    service_customer TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    workorder_id TEXT,
    service_record_id TEXT,
    service_hours REAL,
    crm_customer TEXT,
    service_category TEXT,
    local_recorded_at TEXT,
    crm_verified_at TEXT,
    source TEXT NOT NULL,
    includes_main_work INTEGER CHECK (includes_main_work IN (0, 1)),
    UNIQUE (report_date, step, service_customer, content_digest, source),
    FOREIGN KEY (report_date) REFERENCES report_days(report_date) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS remote_workorder_hour_overrides (
    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    workorder_id TEXT NOT NULL DEFAULT '',
    base_service_hours REAL,
    service_hours REAL NOT NULL,
    crm_sync_status TEXT NOT NULL,
    crm_sync_job_id TEXT,
    crm_observed_hours REAL,
    crm_updated_at TEXT,
    crm_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (report_date, content_digest, workorder_id),
    FOREIGN KEY (report_date) REFERENCES report_days(report_date) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    authorized_digest TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    result_payload TEXT NOT NULL,
    progress_log TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    worker_pid INTEGER,
    error_summary TEXT,
    retry_of_job_id TEXT,
    FOREIGN KEY (retry_of_job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS remote_workorder_bindings (
    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    original_workorder_id TEXT NOT NULL,
    workorder_id TEXT NOT NULL,
    crm_customer TEXT NOT NULL,
    service_date TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (report_date, content_digest),
    FOREIGN KEY (report_date) REFERENCES report_days(report_date) ON DELETE CASCADE,
    FOREIGN KEY (source_job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS date_attributions (
    source_date TEXT NOT NULL UNIQUE,
    target_date TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    CHECK (source_date <> target_date),
    FOREIGN KEY (source_date) REFERENCES report_days(report_date) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS calendar_day_statuses (
    report_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('leave', 'company_office', 'company_meeting', 'company_activity')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    report_date TEXT,
    step TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    file_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    protected_by_pending INTEGER NOT NULL DEFAULT 0 CHECK (protected_by_pending IN (0, 1)),
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_base_pages (
    page_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    space_key TEXT NOT NULL DEFAULT '',
    space_name TEXT NOT NULL DEFAULT '',
    creator_user_key TEXT NOT NULL,
    creator_display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL DEFAULT '',
    page_url TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base_sync_state (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    status TEXT NOT NULL,
    identity_user_key TEXT NOT NULL DEFAULT '',
    identity_display_name TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_base_backups (
    backup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    backup_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_time ON jobs (status, created_at, job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_time ON jobs (created_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_report_date ON jobs (
    CASE WHEN json_valid(request_payload) THEN json_extract(request_payload, '$.report_date') END,
    created_at DESC, job_id DESC
);
CREATE INDEX IF NOT EXISTS idx_artifacts_report_date ON artifacts (report_date, created_at, file_path);
CREATE INDEX IF NOT EXISTS idx_artifacts_active_time ON artifacts (deleted_at, created_at DESC, artifact_id DESC);
"""


class WebStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path).expanduser()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod_if_supported(self.database_path.parent, DIRECTORY_MODE)

        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(SCHEMA_SQL)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT schema_version FROM schema_meta WHERE meta_id = 1"
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO schema_meta (meta_id, schema_version, initialized_at)
                        VALUES (1, ?, ?)
                        """,
                        (SCHEMA_VERSION, _now_iso()),
                    )
                elif row["schema_version"] in (1, 2):
                    if row["schema_version"] == 1:
                        self._migrate_v1_to_v2(connection)
                    self._migrate_v2_to_v3(connection)
                elif row["schema_version"] != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Unsupported schema version: {row['schema_version']}"
                    )

                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise RuntimeError("Web 本机数据库完整性检查失败，请从备份恢复后重试。")

        self._chmod_if_supported(self.database_path, FILE_MODE)

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE calendar_day_statuses_v2 (
                report_date TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('leave', 'company_office', 'company_meeting', 'company_activity')),
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO calendar_day_statuses_v2 (report_date, status, updated_at)
            SELECT report_date, 'leave', updated_at
            FROM calendar_day_statuses
            """
        )
        connection.execute("DROP TABLE calendar_day_statuses")
        connection.execute(
            "ALTER TABLE calendar_day_statuses_v2 RENAME TO calendar_day_statuses"
        )
        connection.execute(
            "UPDATE schema_meta SET schema_version = 2 WHERE meta_id = 1"
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        if not any(row["name"] == "main_service_customers" for row in connection.execute("PRAGMA table_info(report_days)")):
            connection.execute("ALTER TABLE report_days ADD COLUMN main_service_customers TEXT")
        if not any(row["name"] == "includes_main_work" for row in connection.execute("PRAGMA table_info(submission_records)")):
            connection.execute(
                "ALTER TABLE submission_records ADD COLUMN includes_main_work INTEGER CHECK (includes_main_work IN (0, 1))"
            )
        connection.execute("UPDATE schema_meta SET schema_version = 3 WHERE meta_id = 1")

    def upsert_report_day(self, record: ReportDayRecord) -> None:
        self.apply_index_snapshot(report_days=[record])

    def upsert_submission(self, record: SubmissionRecord) -> None:
        self.apply_index_snapshot(submissions=[record])

    def apply_index_snapshot(
        self,
        *,
        report_days=(),
        submissions=(),
        artifacts=(),
        active_pending_sources=(),
        discard_history_sources=(),
        reconcile_pending: bool = False,
        reconcile_artifact_protection: bool = True,
    ) -> None:
        report_payloads = [
            (
                _require_text(record, "report_date"),
                _require_text(record, "report_path"),
                _require_text(record, "customer_name"),
                _normalize_bool(record.get("is_onsite", False)),
                _require_text(record, "content_digest"),
                _require_text(record, "indexed_at"),
                json.dumps(record["main_service_customers"], ensure_ascii=False)
                if "main_service_customers" in record else None,
            )
            for record in report_days
        ]
        submission_payloads = [
            (
                _require_text(record, "report_date"),
                _require_step(record),
                _optional_text(record, "service_customer"),
                _optional_text(record, "content_digest"),
                _require_status(record),
                _optional_text_or_none(record, "workorder_id"),
                _optional_text_or_none(record, "service_record_id"),
                _optional_float(record, "service_hours"),
                _optional_text_or_none(record, "crm_customer"),
                _optional_text_or_none(record, "service_category"),
                _optional_text_or_none(record, "local_recorded_at"),
                _optional_text_or_none(record, "crm_verified_at"),
                _require_text(record, "source"),
                _main_work_flag(record),
            )
            for record in submissions
        ]
        artifact_payloads = [
            (
                _optional_text_or_none(record, "job_id"),
                _optional_text_or_none(record, "report_date"),
                _require_text(record, "step"),
                _require_text(record, "file_path"),
                _require_text(record, "file_type"),
                int(record["size_bytes"]),
                _require_text(record, "created_at"),
                _require_text(record, "expires_at"),
                _normalize_bool(record.get("protected_by_pending", False)),
                _optional_text_or_none(record, "deleted_at"),
            )
            for record in artifacts
        ]
        pending_sources = sorted(
            {
                str(source).strip()
                for source in active_pending_sources
                if str(source).strip().startswith("pending_queue:")
            }
        )
        history_sources = set(discard_history_sources)
        if any(not isinstance(source, str) or not source.startswith("submission_history:")
               or not source.removeprefix("submission_history:") for source in history_sources):
            raise ValueError("only exact submission_history sources may be discarded")
        if not (report_payloads or submission_payloads or artifact_payloads or reconcile_pending or history_sources):
            return

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM submission_records WHERE source = ?",
                [(source,) for source in history_sources],
            )
            if report_payloads:
                connection.executemany(
                    """
                    INSERT INTO report_days (
                        report_date, report_path, customer_name, is_onsite,
                        content_digest, indexed_at, main_service_customers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(report_date) DO UPDATE SET
                        report_path = excluded.report_path,
                        customer_name = excluded.customer_name,
                        is_onsite = excluded.is_onsite,
                        content_digest = excluded.content_digest,
                        indexed_at = excluded.indexed_at,
                        main_service_customers = excluded.main_service_customers
                    """,
                    report_payloads,
                )
                connection.executemany(
                    "DELETE FROM calendar_day_statuses WHERE report_date = ?",
                    [(payload[0],) for payload in report_payloads],
                )
                connection.executemany(
                    "DELETE FROM date_attributions WHERE target_date = ?",
                    [(payload[0],) for payload in report_payloads],
                )
            if submission_payloads:
                connection.executemany(
                    """
                    INSERT INTO submission_records (
                        report_date, step, service_customer, content_digest, status,
                        workorder_id, service_record_id, service_hours, crm_customer,
                        service_category, local_recorded_at, crm_verified_at, source,
                        includes_main_work
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(report_date, step, service_customer, content_digest, source)
                    DO UPDATE SET
                        status = excluded.status,
                        workorder_id = excluded.workorder_id,
                        service_record_id = excluded.service_record_id,
                        service_hours = excluded.service_hours,
                        crm_customer = excluded.crm_customer,
                        service_category = excluded.service_category,
                        local_recorded_at = excluded.local_recorded_at,
                        crm_verified_at = excluded.crm_verified_at,
                        includes_main_work = excluded.includes_main_work
                    """,
                    submission_payloads,
                )
            if artifact_payloads:
                protection_update = (
                    "excluded.protected_by_pending" if reconcile_artifact_protection
                    else "MAX(artifacts.protected_by_pending, excluded.protected_by_pending)"
                )
                connection.executemany(
                    f"""
                    INSERT INTO artifacts (
                        job_id, report_date, step, file_path, file_type, size_bytes,
                        created_at, expires_at, protected_by_pending, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        job_id = excluded.job_id,
                        report_date = excluded.report_date,
                        step = excluded.step,
                        file_type = excluded.file_type,
                        size_bytes = excluded.size_bytes,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        protected_by_pending = {protection_update},
                        deleted_at = excluded.deleted_at
                    """,
                    artifact_payloads,
                )
            if reconcile_pending:
                if pending_sources:
                    placeholders = ", ".join("?" for _ in pending_sources)
                    connection.execute(
                        f"""
                        DELETE FROM submission_records
                        WHERE source LIKE ? AND source NOT IN ({placeholders})
                        """,
                        ("pending_queue:%", *pending_sources),
                    )
                else:
                    connection.execute(
                        "DELETE FROM submission_records WHERE source LIKE ?",
                        ("pending_queue:%",),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_days(self, report_dates: list[str]) -> list[dict]:
        requested_dates = sorted({str(value or "").strip() for value in report_dates if str(value or "").strip()})
        if not requested_dates:
            return []
        requested_set = set(requested_dates)
        with closing(self._connect()) as connection:
            day_rows = [
                row
                for row in self._load_display_day_rows_between(
                    connection,
                    requested_dates[0],
                    requested_dates[-1],
                )
                if row["report_date"] in requested_set
            ]
            if not day_rows:
                return []
            source_dates = sorted({str(row["source_date"]) for row in day_rows})
            submissions = self._load_submissions(connection, source_dates)
            remote_hour_overrides = self._load_remote_hour_overrides(
                connection,
                source_dates,
            )
            remote_workorder_bindings = self._load_remote_workorder_bindings(
                connection,
                source_dates,
            )
            artifacts = self._load_artifacts(connection, source_dates)

        submissions = _apply_remote_workorder_bindings(
            submissions,
            remote_workorder_bindings,
        )
        submissions = _apply_remote_hour_overrides(
            submissions,
            remote_hour_overrides,
        )
        return [
            self._build_day_payload(
                row=dict(row),
                submissions=submissions.get(row["source_date"], []),
                artifacts=artifacts.get(row["source_date"], []),
                include_details=True,
            )
            for row in day_rows
        ]

    def set_manual_submission_override(
        self,
        report_date: str,
        step: str,
        service_customer: str,
    ) -> None:
        normalized_step = _require_manual_override_step(step)
        self.upsert_submission(
            {
                "report_date": str(report_date).strip(),
                "step": normalized_step,
                "service_customer": str(service_customer).strip(),
                "content_digest": "",
                "status": MANUAL_OVERRIDE_STATUS,
                "local_recorded_at": _now_iso(),
                "source": _manual_override_source(normalized_step),
            }
        )

    def clear_manual_submission_override(self, report_date: str, step: str) -> None:
        normalized_step = _require_manual_override_step(step)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    DELETE FROM submission_records
                    WHERE report_date = ? AND step = ? AND source = ?
                    """,
                    (
                        str(report_date).strip(),
                        normalized_step,
                        _manual_override_source(normalized_step),
                    ),
                )

    def create_date_attribution(self, source_date: str, target_date: str) -> dict:
        source = _normalize_iso_date(source_date, "来源日期")
        target = _normalize_iso_date(target_date, "归属日期")
        if target >= source:
            raise ValueError("补交归属日期必须早于来源日期。")
        if not is_china_workday(target):
            day_status = automatic_day_status(target)
            if day_status:
                raise ValueError(
                    f"归属日期是国务院日历中的{day_status['label']}，不能作为工作日。"
                )
            raise ValueError("补交归属日期必须是工作日。")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                "SELECT report_date FROM report_days WHERE report_date = ?",
                (source,),
            ).fetchone()
            if source_row is None:
                raise KeyError("来源日期不存在本机日报记录。")
            if connection.execute(
                "SELECT 1 FROM report_days WHERE report_date = ?", (target,)
            ).fetchone() is not None:
                raise ValueError("归属日期已有日报记录，不能覆盖。")
            if connection.execute(
                "SELECT 1 FROM date_attributions WHERE target_date = ?", (target,)
            ).fetchone() is not None:
                raise ValueError("归属日期已有补交整理记录。")
            calendar_status = connection.execute(
                "SELECT status FROM calendar_day_statuses WHERE report_date = ?",
                (target,),
            ).fetchone()
            if calendar_status is not None:
                raise ValueError(
                    f"归属日期已标记为{CALENDAR_DAY_STATUS_LABELS[calendar_status['status']]}，请先清除本机日历状态。"
                )
            if connection.execute(
                "SELECT 1 FROM date_attributions WHERE source_date = ?", (source,)
            ).fetchone() is not None:
                raise ValueError("该来源日期已经归属到其他工作日。")

            created_at = _now_iso()
            connection.execute(
                """
                INSERT INTO date_attributions (source_date, target_date, created_at)
                VALUES (?, ?, ?)
                """,
                (source, target, created_at),
            )
            connection.commit()
            return {
                "source_date": source,
                "target_date": target,
                "created_at": created_at,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clear_date_attribution(self, target_date: str) -> dict:
        target = _normalize_iso_date(target_date, "归属日期")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT source_date, target_date, created_at
                FROM date_attributions
                WHERE target_date = ?
                """,
                (target,),
            ).fetchone()
            if row is None:
                raise KeyError("归属日期没有本机整理记录。")
            connection.execute(
                "DELETE FROM date_attributions WHERE target_date = ?", (target,)
            )
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_calendar_day_status(self, report_date: str, status: str) -> dict:
        normalized_date = _normalize_iso_date(report_date, "日期")
        normalized_status = _require_calendar_day_status(status)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM report_days WHERE report_date = ?", (normalized_date,)
            ).fetchone() is not None:
                raise ValueError("当天已有日报或工作记录，不能标记本机日历状态。")
            if connection.execute(
                "SELECT 1 FROM date_attributions WHERE target_date = ?",
                (normalized_date,),
            ).fetchone() is not None:
                raise ValueError("当天已有补交归属记录，不能标记本机日历状态。")

            updated_at = _now_iso()
            connection.execute(
                """
                INSERT INTO calendar_day_statuses (report_date, status, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (normalized_date, normalized_status, updated_at),
            )
            connection.commit()
            return {
                "report_date": normalized_date,
                "status": normalized_status,
                "label": CALENDAR_DAY_STATUS_LABELS[normalized_status],
                "updated_at": updated_at,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clear_calendar_day_status(self, report_date: str) -> dict:
        normalized_date = _normalize_iso_date(report_date, "日期")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT report_date, status, updated_at
                FROM calendar_day_statuses
                WHERE report_date = ?
                """,
                (normalized_date,),
            ).fetchone()
            if row is None:
                raise KeyError("当天没有本机日历状态。")
            connection.execute(
                "DELETE FROM calendar_day_statuses WHERE report_date = ?",
                (normalized_date,),
            )
            connection.commit()
            result = dict(row)
            result["label"] = CALENDAR_DAY_STATUS_LABELS[result["status"]]
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_calendar_day_statuses(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        start = _normalize_iso_date(start_date, "开始日期")
        end = _normalize_iso_date(end_date, "结束日期")
        if start > end:
            raise ValueError("日期范围无效")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT report_date, status, updated_at
                FROM calendar_day_statuses
                WHERE report_date >= ? AND report_date <= ?
                ORDER BY report_date ASC
                """,
                (start, end),
            ).fetchall()
        return [
            {
                **dict(row),
                "label": CALENDAR_DAY_STATUS_LABELS[row["status"]],
            }
            for row in rows
        ]

    def delete_failed_crm_verification(self, *, report_date: str, job_id: str) -> dict:
        normalized_report_date = _require_nonempty_text(report_date, "report_date")
        normalized_job_id = _require_nonempty_text(job_id, "job_id")
        source = f"crm_verification:{normalized_job_id}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).fetchone()
            if job_row is None:
                raise KeyError(f"unknown job: {normalized_job_id}")
            job = _deserialize_job_row(job_row)
            if (
                job["action"] != CRM_VERIFICATION_ACTION
                or job["status"] not in DELETABLE_CRM_VERIFICATION_STATUSES
            ):
                raise ValueError("只能删除已失败的 CRM 只读校验记录。")
            if str(job.get("request_payload", {}).get("report_date", "")).strip() != normalized_report_date:
                raise ValueError("CRM 校验记录不属于当前日报日期。")
            child = connection.execute(
                "SELECT job_id FROM jobs WHERE retry_of_job_id = ? LIMIT 1",
                (normalized_job_id,),
            ).fetchone()
            if child is not None:
                raise ValueError("该校验记录存在后续重试任务，不能直接删除。")
            record_rows = connection.execute(
                """
                SELECT status
                FROM submission_records
                WHERE report_date = ? AND source = ?
                """,
                (normalized_report_date, source),
            ).fetchall()
            if any(
                row["status"] not in DELETABLE_CRM_VERIFICATION_STATUSES
                for row in record_rows
            ):
                raise ValueError("校验记录包含非失败结果，不能删除。")
            deleted_submissions = connection.execute(
                """
                DELETE FROM submission_records
                WHERE report_date = ? AND source = ?
                """,
                (normalized_report_date, source),
            ).rowcount
            deleted_jobs = connection.execute(
                "DELETE FROM jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).rowcount
            connection.commit()
            return {
                "job_id": normalized_job_id,
                "deleted_submission_count": int(deleted_submissions),
                "deleted_job_count": int(deleted_jobs),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_failed_crm_verifications(
        self,
        *,
        report_date: str,
        step: str,
    ) -> dict:
        normalized_report_date = _require_nonempty_text(report_date, "report_date")
        normalized_step = _require_step({"step": step})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            status_placeholders = ", ".join(
                "?" for _ in DELETABLE_CRM_VERIFICATION_STATUSES
            )
            deleted_submissions = connection.execute(
                f"""
                DELETE FROM submission_records
                WHERE report_date = ?
                  AND step = ?
                  AND source LIKE 'crm_verification:%'
                  AND status IN ({status_placeholders})
                """,
                (
                    normalized_report_date,
                    normalized_step,
                    *sorted(DELETABLE_CRM_VERIFICATION_STATUSES),
                ),
            ).rowcount

            candidate_ids = set()
            for row in connection.execute(
                "SELECT * FROM jobs WHERE action = ?",
                (CRM_VERIFICATION_ACTION,),
            ).fetchall():
                job = _deserialize_job_row(row)
                payload = job.get("request_payload", {})
                if (
                    job["status"] in DELETABLE_CRM_VERIFICATION_STATUSES
                    and str(payload.get("report_date", "")).strip()
                    == normalized_report_date
                    and str(payload.get("step", "")).strip() == normalized_step
                ):
                    candidate_ids.add(job["job_id"])

            deleted_jobs = 0
            pending_ids = set(candidate_ids)
            while pending_ids:
                leaves = []
                for job_id in pending_ids:
                    child = connection.execute(
                        "SELECT job_id FROM jobs WHERE retry_of_job_id = ? LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    if child is None:
                        leaves.append(job_id)
                if not leaves:
                    break
                for job_id in leaves:
                    deleted_jobs += int(
                        connection.execute(
                            "DELETE FROM jobs WHERE job_id = ?",
                            (job_id,),
                        ).rowcount
                    )
                    pending_ids.remove(job_id)

            connection.commit()
            return {
                "deleted_submission_count": int(deleted_submissions),
                "deleted_job_count": deleted_jobs,
                "deleted_verification_count": deleted_jobs,
                "skipped_job_count": len(pending_ids),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_remote_workorder_hour_override(
        self,
        *,
        report_date: str,
        content_digest: str,
        workorder_id: str,
        base_service_hours,
        service_hours,
    ) -> dict:
        normalized_report_date = _require_nonempty_text(report_date, "report_date")
        normalized_digest = _require_nonempty_text(content_digest, "content_digest")
        normalized_workorder_id = str(workorder_id or "").strip()
        normalized_base_hours = _optional_float_value(base_service_hours)
        normalized_service_hours = _normalize_remote_hour_override_hours(service_hours)
        now = _now_iso()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO remote_workorder_hour_overrides (
                        report_date,
                        content_digest,
                        workorder_id,
                        base_service_hours,
                        service_hours,
                        crm_sync_status,
                        crm_sync_job_id,
                        crm_observed_hours,
                        crm_updated_at,
                        crm_error,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'local_only', NULL, NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(report_date, content_digest, workorder_id) DO UPDATE SET
                        base_service_hours = COALESCE(
                            remote_workorder_hour_overrides.base_service_hours,
                            excluded.base_service_hours
                        ),
                        service_hours = excluded.service_hours,
                        crm_sync_status = 'local_only',
                        crm_sync_job_id = NULL,
                        crm_observed_hours = NULL,
                        crm_updated_at = NULL,
                        crm_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_report_date,
                        normalized_digest,
                        normalized_workorder_id,
                        normalized_base_hours,
                        normalized_service_hours,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT
                        report_date,
                        content_digest,
                        workorder_id,
                        base_service_hours,
                        service_hours,
                        crm_sync_status,
                        crm_sync_job_id,
                        crm_observed_hours,
                        crm_updated_at,
                        crm_error,
                        created_at,
                        updated_at
                    FROM remote_workorder_hour_overrides
                    WHERE report_date = ? AND content_digest = ? AND workorder_id = ?
                    """,
                    (
                        normalized_report_date,
                        normalized_digest,
                        normalized_workorder_id,
                    ),
                ).fetchone()
        return _deserialize_remote_hour_override(row)

    def get_remote_workorder_hour_override(
        self,
        *,
        report_date: str,
        content_digest: str,
        workorder_id: str,
    ) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    report_date,
                    content_digest,
                    workorder_id,
                    base_service_hours,
                    service_hours,
                    crm_sync_status,
                    crm_sync_job_id,
                    crm_observed_hours,
                    crm_updated_at,
                    crm_error,
                    created_at,
                    updated_at
                FROM remote_workorder_hour_overrides
                WHERE report_date = ? AND content_digest = ? AND workorder_id = ?
                """,
                (
                    str(report_date or "").strip(),
                    str(content_digest or "").strip(),
                    str(workorder_id or "").strip(),
                ),
            ).fetchone()
        return _deserialize_remote_hour_override(row) if row is not None else None

    def mark_remote_workorder_hour_sync_queued(
        self,
        *,
        report_date: str,
        content_digest: str,
        workorder_id: str,
        job_id: str,
    ) -> dict:
        queued = self._update_remote_workorder_hour_override_sync(
            report_date=report_date,
            content_digest=content_digest,
            workorder_id=workorder_id,
            crm_sync_status="sync_queued",
            crm_sync_job_id=job_id,
            crm_observed_hours=None,
            crm_updated_at=None,
            crm_error=None,
        )
        if queued is None:
            raise RuntimeError("远程工时排队状态未保存。")
        return queued

    def record_remote_workorder_hour_sync_result(
        self,
        *,
        report_date: str,
        content_digest: str,
        workorder_id: str,
        expected_job_id: str | None = None,
        crm_sync_status: str,
        crm_observed_hours=None,
        crm_error=None,
    ) -> dict | None:
        normalized_status = str(crm_sync_status or "").strip()
        if normalized_status not in REMOTE_HOUR_SYNC_STATUSES:
            raise ValueError(f"unsupported remote hour sync status: {normalized_status}")
        observed_hours = _optional_float_value(crm_observed_hours)
        return self._update_remote_workorder_hour_override_sync(
            report_date=report_date,
            content_digest=content_digest,
            workorder_id=workorder_id,
            crm_sync_status=normalized_status,
            crm_sync_job_id=None,
            crm_observed_hours=observed_hours,
            crm_updated_at=_now_iso() if normalized_status == "crm_verified" else None,
            crm_error=str(crm_error or "").strip() or None,
            expected_job_id=expected_job_id,
            adopt_observed_hours=(
                normalized_status in {"crm_verified", "crm_mismatch"}
                and observed_hours is not None
            ),
        )

    def _update_remote_workorder_hour_override_sync(
        self,
        *,
        report_date: str,
        content_digest: str,
        workorder_id: str,
        crm_sync_status: str,
        crm_sync_job_id,
        crm_observed_hours,
        crm_updated_at,
        crm_error,
        expected_job_id: str | None = None,
        adopt_observed_hours: bool = False,
    ) -> dict | None:
        normalized_report_date = _require_nonempty_text(report_date, "report_date")
        normalized_digest = _require_nonempty_text(content_digest, "content_digest")
        normalized_workorder_id = str(workorder_id or "").strip()
        expected = str(expected_job_id or "").strip()
        where_clause = "WHERE report_date = ? AND content_digest = ? AND workorder_id = ?"
        where_parameters: tuple[object, ...] = (
            normalized_report_date,
            normalized_digest,
            normalized_workorder_id,
        )
        if expected:
            where_clause += " AND crm_sync_job_id = ? AND crm_sync_status = 'sync_queued'"
            where_parameters += (expected,)

        with closing(self._connect()) as connection:
            with connection:
                updated = connection.execute(
                    f"""
                    UPDATE remote_workorder_hour_overrides
                    SET base_service_hours = CASE WHEN ? THEN ? ELSE base_service_hours END,
                        service_hours = CASE WHEN ? THEN ? ELSE service_hours END,
                        crm_sync_status = ?,
                        crm_sync_job_id = COALESCE(?, crm_sync_job_id),
                        crm_observed_hours = ?,
                        crm_updated_at = ?,
                        crm_error = ?,
                        updated_at = ?
                    {where_clause}
                    """,
                    (
                        int(bool(adopt_observed_hours)),
                        crm_observed_hours,
                        int(bool(adopt_observed_hours)),
                        crm_observed_hours,
                        crm_sync_status,
                        str(crm_sync_job_id or "").strip() or None,
                        crm_observed_hours,
                        crm_updated_at,
                        crm_error,
                        _now_iso(),
                        *where_parameters,
                    ),
                )
                if updated.rowcount != 1:
                    if expected:
                        return None
                    raise KeyError("未找到远程工单本机工时覆盖记录。")
                row = connection.execute(
                    """
                    SELECT
                        report_date,
                        content_digest,
                        workorder_id,
                        base_service_hours,
                        service_hours,
                        crm_sync_status,
                        crm_sync_job_id,
                        crm_observed_hours,
                        crm_updated_at,
                        crm_error,
                        created_at,
                        updated_at
                    FROM remote_workorder_hour_overrides
                    WHERE report_date = ? AND content_digest = ? AND workorder_id = ?
                    """,
                    (
                        normalized_report_date,
                        normalized_digest,
                        normalized_workorder_id,
                    ),
                ).fetchone()
        return _deserialize_remote_hour_override(row)

    def queue_remote_workorder_hour_sync(
        self,
        descriptor: dict,
        *,
        base_service_hours,
        target_hours,
    ) -> tuple[str, bool]:
        if not isinstance(descriptor, dict):
            raise ValueError("descriptor must be an object")
        action = _require_text(descriptor, "action")
        if action != "remote_workorder_hours_sync":
            raise ValueError("unsupported remote hour action")
        report_date = _require_text(descriptor, "report_date")
        content_digest = _require_text(descriptor, "content_digest")
        workorder_id = _require_text(descriptor, "workorder_id")
        unique_key = _require_text(descriptor, "unique_key")
        authorized_digest = _require_text(descriptor, "authorization_digest")
        normalized_base_hours = _optional_float_value(base_service_hours)
        if normalized_base_hours is None:
            raise ValueError("base_service_hours is required")
        normalized_target_hours = _normalize_remote_hour_override_hours(target_hours)
        descriptor_hours = _normalize_remote_hour_override_hours(descriptor.get("target_hours"))
        if abs(descriptor_hours - normalized_target_hours) > 1e-9:
            raise ValueError("descriptor target_hours mismatch")

        candidate_job_id = uuid.uuid4().hex
        now = _now_iso()
        request_json = json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ", ".join("?" for _ in ACTIVE_REMOTE_HOUR_JOB_STATUSES)
            rows = connection.execute(
                f"""
                SELECT job_id, request_payload
                FROM jobs
                WHERE action = ? AND status IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC
                """,
                (action, *ACTIVE_REMOTE_HOUR_JOB_STATUSES),
            ).fetchall()
            for row in rows:
                payload = _decode_json(row["request_payload"], {})
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("workorder_id") or "").strip() != workorder_id:
                    continue
                if payload.get("unique_key") == unique_key:
                    connection.commit()
                    return row["job_id"], False
                raise ValueError("该远程工单已有工时任务正在处理。")

            connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    action,
                    status,
                    authorized_digest,
                    request_payload,
                    result_payload,
                    progress_log,
                    created_at,
                    started_at,
                    finished_at,
                    worker_pid,
                    error_summary,
                    retry_of_job_id
                ) VALUES (?, ?, 'queued', ?, ?, '{}', '[]', ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    candidate_job_id,
                    action,
                    authorized_digest,
                    request_json,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO remote_workorder_hour_overrides (
                    report_date,
                    content_digest,
                    workorder_id,
                    base_service_hours,
                    service_hours,
                    crm_sync_status,
                    crm_sync_job_id,
                    crm_observed_hours,
                    crm_updated_at,
                    crm_error,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, 'sync_queued', ?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(report_date, content_digest, workorder_id) DO UPDATE SET
                    base_service_hours = excluded.base_service_hours,
                    service_hours = excluded.service_hours,
                    crm_sync_status = 'sync_queued',
                    crm_sync_job_id = excluded.crm_sync_job_id,
                    crm_observed_hours = NULL,
                    crm_updated_at = NULL,
                    crm_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    report_date,
                    content_digest,
                    workorder_id,
                    normalized_base_hours,
                    normalized_target_hours,
                    candidate_job_id,
                    now,
                    now,
                ),
            )
            connection.commit()
            return candidate_job_id, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_remote_workorder_binding(
        self,
        *,
        report_date: str,
        content_digest: str,
        original_workorder_id: str,
        workorder_id: str,
        crm_customer: str,
        service_date: str,
        source_job_id: str,
        confirmed_hours=_UNSET,
    ) -> dict:
        normalized_report_date = _require_nonempty_text(report_date, "report_date")
        normalized_digest = _require_nonempty_text(content_digest, "content_digest")
        normalized_original_id = _require_nonempty_text(
            original_workorder_id,
            "original_workorder_id",
        )
        normalized_workorder_id = _require_nonempty_text(workorder_id, "workorder_id")
        normalized_customer = _require_nonempty_text(crm_customer, "crm_customer")
        normalized_service_date = _require_nonempty_text(service_date, "service_date")
        normalized_source_job_id = _require_nonempty_text(source_job_id, "source_job_id")
        observed_hours = (
            None if confirmed_hours is _UNSET
            else _normalize_remote_hour_override_hours(confirmed_hours)
        )
        now = _now_iso()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO remote_workorder_bindings (
                        report_date,
                        content_digest,
                        original_workorder_id,
                        workorder_id,
                        crm_customer,
                        service_date,
                        source_job_id,
                        verified_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(report_date, content_digest) DO UPDATE SET
                        original_workorder_id = remote_workorder_bindings.original_workorder_id,
                        workorder_id = excluded.workorder_id,
                        crm_customer = excluded.crm_customer,
                        service_date = excluded.service_date,
                        source_job_id = excluded.source_job_id,
                        verified_at = excluded.verified_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_report_date,
                        normalized_digest,
                        normalized_original_id,
                        normalized_workorder_id,
                        normalized_customer,
                        normalized_service_date,
                        normalized_source_job_id,
                        now,
                        now,
                        now,
                    ),
                )
                if observed_hours is not None:
                    connection.execute(
                        """
                        INSERT INTO remote_workorder_hour_overrides (
                            report_date, content_digest, workorder_id, base_service_hours,
                            service_hours, crm_sync_status, crm_sync_job_id, crm_observed_hours,
                            crm_updated_at, crm_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'crm_verified', NULL, ?, ?, NULL, ?, ?)
                        ON CONFLICT(report_date, content_digest, workorder_id) DO UPDATE SET
                            base_service_hours = excluded.base_service_hours,
                            service_hours = excluded.service_hours,
                            crm_sync_status = 'crm_verified', crm_sync_job_id = NULL,
                            crm_observed_hours = excluded.crm_observed_hours,
                            crm_updated_at = excluded.crm_updated_at, crm_error = NULL,
                            updated_at = excluded.updated_at
                        """,
                        (normalized_report_date, normalized_digest, normalized_workorder_id,
                         observed_hours, observed_hours, observed_hours, now, now, now),
                    )
                row = connection.execute(
                    """
                    SELECT report_date, content_digest, original_workorder_id,
                           workorder_id, crm_customer, service_date, source_job_id,
                           verified_at, created_at, updated_at
                    FROM remote_workorder_bindings
                    WHERE report_date = ? AND content_digest = ?
                    """,
                    (normalized_report_date, normalized_digest),
                ).fetchone()
        return dict(row)

    def get_remote_workorder_binding(
        self,
        report_date: str,
        content_digest: str,
    ) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT report_date, content_digest, original_workorder_id,
                       workorder_id, crm_customer, service_date, source_job_id,
                       verified_at, created_at, updated_at
                FROM remote_workorder_bindings
                WHERE report_date = ? AND content_digest = ?
                """,
                (str(report_date or "").strip(), str(content_digest or "").strip()),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_artifact(self, record: ArtifactRecord) -> None:
        self.apply_index_snapshot(artifacts=[record])

    def reconcile_pending_submission_sources(self, active_sources: set[str]) -> None:
        normalized_sources = {
            str(source).strip()
            for source in active_sources
            if str(source).strip().startswith("pending_queue:")
        }
        with closing(self._connect()) as connection:
            with connection:
                if not normalized_sources:
                    connection.execute(
                        """
                        DELETE FROM submission_records
                        WHERE source LIKE ?
                        """,
                        ("pending_queue:%",),
                    )
                    return

                placeholders = ", ".join("?" for _ in normalized_sources)
                connection.execute(
                    f"""
                    DELETE FROM submission_records
                    WHERE source LIKE ?
                      AND source NOT IN ({placeholders})
                    """,
                    ("pending_queue:%", *sorted(normalized_sources)),
                )

    def list_month(self, year: int, month: int) -> list[dict]:
        prefix = f"{int(year):04d}-{int(month):02d}-"
        with closing(self._connect()) as connection:
            day_rows = self._load_month_display_day_rows(connection, prefix)
            if not day_rows:
                return []

            source_dates = sorted({row["source_date"] for row in day_rows})
            submissions = self._load_submissions(connection, source_dates)
            remote_hour_overrides = self._load_remote_hour_overrides(
                connection,
                source_dates,
            )
            remote_workorder_bindings = self._load_remote_workorder_bindings(
                connection,
                source_dates,
            )
            artifacts = self._load_artifacts(connection, source_dates)

        submissions = _apply_remote_workorder_bindings(
            submissions,
            remote_workorder_bindings,
        )
        submissions = _apply_remote_hour_overrides(
            submissions,
            remote_hour_overrides,
        )

        return [
            self._build_day_payload(
                row=dict(row),
                submissions=submissions.get(row["source_date"], []),
                artifacts=artifacts.get(row["source_date"], []),
                include_details=False,
            )
            for row in day_rows
        ]

    def list_report_days(
        self,
        start_date: str,
        end_date: str,
        *,
        is_onsite: bool | None = None,
    ) -> list[dict]:
        normalized_start = str(start_date or "").strip()
        normalized_end = str(end_date or "").strip()
        if not normalized_start or not normalized_end:
            raise ValueError("日期范围不能为空")
        if normalized_start > normalized_end:
            raise ValueError("日期范围无效")

        with closing(self._connect()) as connection:
            rows = self._load_display_day_rows_between(
                connection,
                normalized_start,
                normalized_end,
                is_onsite=is_onsite,
            )
        return [dict(row) for row in rows]

    def get_day(self, report_date: str) -> dict | None:
        target_date = str(report_date or "").strip()
        if not target_date:
            return None

        with closing(self._connect()) as connection:
            day_row = self._load_display_day_row(connection, target_date)
            if day_row is None:
                return None

            source_date = str(day_row["source_date"])
            submissions = self._load_submissions(connection, [source_date]).get(
                source_date, []
            )
            remote_hour_overrides = self._load_remote_hour_overrides(
                connection,
                [source_date],
            )
            remote_workorder_bindings = self._load_remote_workorder_bindings(
                connection,
                [source_date],
            )
            artifacts = self._load_artifacts(connection, [source_date]).get(
                source_date, []
            )

        submissions = _apply_remote_workorder_bindings(
            {source_date: submissions},
            remote_workorder_bindings,
        ).get(source_date, [])
        submissions = _apply_remote_hour_overrides(
            {source_date: submissions},
            remote_hour_overrides,
        ).get(source_date, [])

        return self._build_day_payload(
            row=dict(day_row),
            submissions=submissions,
            artifacts=artifacts,
            include_details=True,
        )

    def upsert_knowledge_base_pages(
        self,
        records: list[KnowledgeBasePageRecord],
    ) -> int:
        self.apply_knowledge_base_snapshot(records)
        return len(records)

    def apply_knowledge_base_snapshot(
        self,
        records: list[KnowledgeBasePageRecord],
        *,
        creator_user_key: str | None = None,
        visible_page_ids: set[str] | None = None,
    ) -> int:
        if not records and creator_user_key is None:
            return 0
        creator = None
        if creator_user_key is not None:
            creator = _require_nonempty_text(creator_user_key, "creator_user_key")
            if visible_page_ids is None:
                raise ValueError("visible_page_ids is required for a full snapshot")
            if any(record.get("creator_user_key") != creator for record in records):
                raise ValueError("snapshot contains another creator's pages")
        visible = {str(page_id).strip() for page_id in (visible_page_ids or ())}
        payloads = [
            (
                _require_text(record, "page_id"),
                _require_text(record, "title"),
                _optional_text(record, "space_key"),
                _optional_text(record, "space_name"),
                _require_text(record, "creator_user_key"),
                _optional_text(record, "creator_display_name"),
                _require_text(record, "created_at"),
                _optional_text(record, "modified_at"),
                _require_text(record, "page_url"),
                _require_text(record, "indexed_at"),
            )
            for record in records
        ]
        pruned_count = 0
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    """
                    INSERT INTO knowledge_base_pages (
                        page_id,
                        title,
                        space_key,
                        space_name,
                        creator_user_key,
                        creator_display_name,
                        created_at,
                        modified_at,
                        page_url,
                        indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(page_id) DO UPDATE SET
                        title = excluded.title,
                        space_key = excluded.space_key,
                        space_name = excluded.space_name,
                        creator_user_key = excluded.creator_user_key,
                        creator_display_name = excluded.creator_display_name,
                        created_at = excluded.created_at,
                        modified_at = excluded.modified_at,
                        page_url = excluded.page_url,
                        indexed_at = excluded.indexed_at
                    """,
                    payloads,
                )
                if creator is not None:
                    rows = connection.execute(
                        "SELECT page_id FROM knowledge_base_pages WHERE creator_user_key = ?",
                        (creator,),
                    ).fetchall()
                    stale_ids = [(row["page_id"],) for row in rows if row["page_id"] not in visible]
                    connection.executemany(
                        "DELETE FROM knowledge_base_pages WHERE page_id = ?", stale_ids
                    )
                    pruned_count = len(stale_ids)
        return pruned_count

    def list_knowledge_base_pages(
        self,
        *,
        year: int | None = None,
        quarter: int | None = None,
    ) -> list[dict]:
        if (year is None) != (quarter is None):
            raise ValueError("year and quarter must be supplied together")
        if year is not None and quarter is not None:
            normalized_year = int(year)
            normalized_quarter = int(quarter)
            if not 1 <= normalized_year <= 9999 or normalized_quarter not in {1, 2, 3, 4}:
                raise ValueError("quarter must be between 1 and 4")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    page_id,
                    title,
                    space_key,
                    space_name,
                    creator_user_key,
                    creator_display_name,
                    created_at,
                    modified_at,
                    page_url,
                    indexed_at
                FROM knowledge_base_pages
                """,
            ).fetchall()
        pages = [dict(row) for row in rows]
        created_dates = {row["page_id"]: parse_metadata_timestamp(row["created_at"]) for row in pages}
        pages.sort(key=lambda row: (
            created_dates[row["page_id"]] is not None,
            created_dates[row["page_id"]] or datetime.min,
            row["page_id"],
        ), reverse=True)
        if year is not None:
            pages = [row for row in pages if (
                (created := created_dates[row["page_id"]]) is not None
                and created.year == normalized_year
                and (created.month - 1) // 3 + 1 == normalized_quarter
            )]
        return pages

    def list_knowledge_base_quarters(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT created_at
                FROM knowledge_base_pages
                WHERE length(created_at) >= 7
                ORDER BY created_at DESC, page_id DESC
                """
            ).fetchall()
        grouped: dict[tuple[int, int], int] = {}
        for row in rows:
            created = parse_metadata_timestamp(row["created_at"])
            if created is None:
                continue
            key = (created.year, (created.month - 1) // 3 + 1)
            grouped[key] = grouped.get(key, 0) + 1
        return [
            {"year": year, "quarter": quarter, "count": count}
            for (year, quarter), count in sorted(grouped.items(), reverse=True)
        ]

    def get_latest_knowledge_base_created_at(self) -> str | None:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT created_at
                FROM knowledge_base_pages
                """
            ).fetchall()
        latest = max((
            created for row in rows
            if (created := parse_metadata_timestamp(row["created_at"])) is not None
        ), default=None)
        return latest.isoformat() if latest is not None else None

    def count_knowledge_base_pages(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM knowledge_base_pages"
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def create_knowledge_base_backup(self, *, created_at: str) -> dict:
        normalized_created_at = _require_nonempty_text(created_at, "created_at")
        backup_directory = self.database_path.parent / "knowledge_base_backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        self._chmod_if_supported(backup_directory, DIRECTORY_MODE)
        backup_path = backup_directory / (
            "knowledge-base-before-full-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.sqlite3"
        )

        source = self._connect()
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
            destination.commit()
        except Exception:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            destination.close()
            source.close()

        self._chmod_if_supported(backup_path, FILE_MODE)
        payload = {
            "created_at": normalized_created_at,
            "backup_path": str(backup_path),
            "size_bytes": int(backup_path.stat().st_size),
        }
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO knowledge_base_backups (created_at, backup_path, size_bytes)
                    VALUES (?, ?, ?)
                    """,
                    (
                        payload["created_at"],
                        payload["backup_path"],
                        payload["size_bytes"],
                    ),
                )
        return payload

    def list_knowledge_base_backups(self, *, limit: int = 1) -> list[dict]:
        normalized_limit = max(1, min(int(limit), 20))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT backup_id, created_at, backup_path, size_bytes
                FROM knowledge_base_backups
                ORDER BY backup_id DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_unseen_knowledge_base_pages(
        self,
        *,
        creator_user_key: str,
        visible_page_ids: set[str],
    ) -> int:
        return self.apply_knowledge_base_snapshot(
            [], creator_user_key=creator_user_key, visible_page_ids=visible_page_ids,
        )

    def get_knowledge_base_sync_state(self) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    status,
                    identity_user_key,
                    identity_display_name,
                    started_at,
                    completed_at,
                    scanned_count,
                    matched_count,
                    error_summary
                FROM knowledge_base_sync_state
                WHERE state_id = 1
                """
            ).fetchone()
        if row is None:
            return {
                "status": "never",
                "identity_user_key": "",
                "identity_display_name": "",
                "started_at": None,
                "completed_at": None,
                "scanned_count": 0,
                "matched_count": 0,
                "error_summary": "",
            }
        return dict(row)

    def start_knowledge_base_sync(
        self,
        *,
        identity_user_key: str,
        identity_display_name: str,
        started_at: str,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO knowledge_base_sync_state (
                        state_id,
                        status,
                        identity_user_key,
                        identity_display_name,
                        started_at,
                        completed_at,
                        scanned_count,
                        matched_count,
                        error_summary
                    ) VALUES (1, 'running', ?, ?, ?, NULL, 0, 0, NULL)
                    ON CONFLICT(state_id) DO UPDATE SET
                        status = 'running',
                        identity_user_key = excluded.identity_user_key,
                        identity_display_name = excluded.identity_display_name,
                        started_at = excluded.started_at,
                        completed_at = NULL,
                        scanned_count = 0,
                        matched_count = 0,
                        error_summary = NULL
                    """,
                    (
                        _require_nonempty_text(identity_user_key, "identity_user_key"),
                        _optional_text({"value": identity_display_name}, "value"),
                        _require_nonempty_text(started_at, "started_at"),
                    ),
                )

    def update_knowledge_base_sync_progress(
        self,
        *,
        scanned_count: int,
        matched_count: int,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE knowledge_base_sync_state
                    SET scanned_count = ?, matched_count = ?
                    WHERE state_id = 1 AND status = 'running'
                    """,
                    (max(int(scanned_count), 0), max(int(matched_count), 0)),
                )

    def finish_knowledge_base_sync(
        self,
        *,
        status: str,
        completed_at: str,
        scanned_count: int,
        matched_count: int,
        error_summary: str | None = None,
    ) -> None:
        if status not in {"success", "failed"}:
            raise ValueError("knowledge base sync status must be success or failed")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE knowledge_base_sync_state
                    SET
                        status = ?,
                        completed_at = ?,
                        scanned_count = ?,
                        matched_count = ?,
                        error_summary = ?
                    WHERE state_id = 1
                    """,
                    (
                        status,
                        _require_nonempty_text(completed_at, "completed_at"),
                        max(int(scanned_count), 0),
                        max(int(matched_count), 0),
                        _optional_text_or_none({"value": error_summary}, "value"),
                    ),
                )

    def fail_knowledge_base_sync(
        self,
        *,
        completed_at: str,
        scanned_count: int,
        matched_count: int,
        error_summary: str,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO knowledge_base_sync_state (
                        state_id,
                        status,
                        completed_at,
                        scanned_count,
                        matched_count,
                        error_summary
                    ) VALUES (1, 'failed', ?, ?, ?, ?)
                    ON CONFLICT(state_id) DO UPDATE SET
                        status = 'failed',
                        completed_at = excluded.completed_at,
                        scanned_count = excluded.scanned_count,
                        matched_count = excluded.matched_count,
                        error_summary = excluded.error_summary
                    """,
                    (
                        _require_nonempty_text(completed_at, "completed_at"),
                        max(int(scanned_count), 0),
                        max(int(matched_count), 0),
                        _require_nonempty_text(error_summary, "error_summary"),
                    ),
                )

    def list_jobs(
        self, *, limit=None, offset=0, report_date=None, related_job_ids=()
    ) -> list[dict]:
        parameters: list[object] = []
        where = ""
        if report_date is not None:
            where = "WHERE (CASE WHEN json_valid(request_payload) THEN json_extract(request_payload, '$.report_date') END) = ?"
            parameters.append(str(report_date))
            related_ids = sorted({str(value) for value in related_job_ids if value})
            if related_ids:
                where += " OR job_id IN (" + ", ".join("?" for _ in related_ids) + ")"
                parameters.extend(related_ids)
        page_clause = ""
        if limit is not None:
            if int(limit) < 1 or int(offset) < 0:
                raise ValueError("分页范围无效")
            page_clause = "LIMIT ? OFFSET ?"
            parameters.extend((int(limit), int(offset)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    job_id,
                    action,
                    status,
                    authorized_digest,
                    request_payload,
                    result_payload,
                    progress_log,
                    created_at,
                    started_at,
                    finished_at,
                    worker_pid,
                    error_summary,
                    retry_of_job_id
                FROM jobs
                {where}
                ORDER BY created_at DESC, job_id DESC
                {page_clause}
                """,
                parameters,
            ).fetchall()

        return [_deserialize_job_row(row) for row in rows]

    def count_jobs(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def job_page_number(self, job_id: str, page_size: int) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT created_at FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return 1
            newer_count = connection.execute(
                """SELECT COUNT(*) FROM jobs
                   WHERE created_at > ? OR (created_at = ? AND job_id > ?)""",
                (row["created_at"], row["created_at"], job_id),
            ).fetchone()[0]
        return int(newer_count) // page_size + 1

    def get_job(self, job_id: str) -> dict | None:
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    job_id,
                    action,
                    status,
                    authorized_digest,
                    request_payload,
                    result_payload,
                    progress_log,
                    created_at,
                    started_at,
                    finished_at,
                    worker_pid,
                    error_summary,
                    retry_of_job_id
                FROM jobs
                WHERE job_id = ?
                """,
                (normalized_job_id,),
            ).fetchone()
        return _deserialize_job_row(row) if row is not None else None

    def delete_job_record(self, job_id: str) -> dict:
        normalized_job_id = _require_nonempty_text(job_id, "job_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {normalized_job_id}")
            if row["status"] not in DELETABLE_JOB_RECORD_STATUSES:
                raise ValueError("只能删除未开始、已完成或明确失败的任务。")
            child = connection.execute(
                "SELECT job_id FROM jobs WHERE retry_of_job_id = ? LIMIT 1",
                (normalized_job_id,),
            ).fetchone()
            if child is not None:
                raise ValueError("该任务存在后续重试记录，不能直接删除。")

            binding = connection.execute(
                "SELECT binding_id FROM remote_workorder_bindings WHERE source_job_id = ? LIMIT 1",
                (normalized_job_id,),
            ).fetchone()
            if binding is not None:
                raise ValueError("该任务仍被远程工单绑定引用，不能直接删除。")

            released_remote_hours = 0
            if row["status"] in {"queued", "pending_retry"}:
                released_remote_hours = connection.execute(
                    """
                    UPDATE remote_workorder_hour_overrides
                    SET crm_sync_status = 'local_only',
                        crm_sync_job_id = NULL,
                        crm_observed_hours = NULL,
                        crm_updated_at = NULL,
                        crm_error = NULL,
                        updated_at = ?
                    WHERE crm_sync_job_id = ?
                    """,
                    (_now_iso(), normalized_job_id),
                ).rowcount
            deleted = connection.execute(
                "DELETE FROM jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).rowcount
            if deleted != 1:
                raise RuntimeError("任务状态已变化，请刷新后重试。")
            connection.commit()
            return {
                "job_id": normalized_job_id,
                "deleted_job_count": int(deleted),
                "released_remote_hour_count": int(released_remote_hours),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_jobs_by_status(self, statuses) -> list[dict]:
        normalized_statuses = tuple(
            sorted({str(status or "").strip() for status in statuses if str(status or "").strip()})
        )
        if not normalized_statuses:
            return []
        placeholders = ", ".join("?" for _ in normalized_statuses)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    job_id,
                    action,
                    status,
                    authorized_digest,
                    request_payload,
                    result_payload,
                    progress_log,
                    created_at,
                    started_at,
                    finished_at,
                    worker_pid,
                    error_summary,
                    retry_of_job_id
                FROM jobs
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC
                """,
                normalized_statuses,
            ).fetchall()
        return [_deserialize_job_row(row) for row in rows]

    def claim_next_queued_job(self, *, action: str | None = None) -> dict | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE status = 'queued' AND (? IS NULL OR action = ?)
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (action, action),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            started_at = _now_iso()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    started_at = ?,
                    finished_at = NULL,
                    worker_pid = NULL,
                    error_summary = NULL
                WHERE job_id = ? AND status = 'queued'
                """,
                (started_at, row["job_id"]),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            job_row = connection.execute(
                """
                SELECT * FROM jobs WHERE job_id = ?
                """,
                (row["job_id"],),
            ).fetchone()
            connection.commit()
            return _deserialize_job_row(job_row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def attach_worker_pid(self, job_id: str, worker_pid: int) -> dict:
        """Bind a live worker without reverting a concurrently advanced job state."""
        if type(worker_pid) is not int or worker_pid <= 0:
            raise ValueError("worker_pid must be a positive integer")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            if row["status"] in ("running", "crm_id_captured"):
                if row["worker_pid"] not in (None, worker_pid):
                    raise RuntimeError("job is already owned by another worker")
                connection.execute("UPDATE jobs SET worker_pid = ? WHERE job_id = ?", (worker_pid, job_id))
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.commit()
            return _deserialize_job_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_job(
        self,
        job_id: str,
        expected_status: str,
        *,
        status=_UNSET,
        result_payload=_UNSET,
        progress_log=_UNSET,
        started_at=_UNSET,
        finished_at=_UNSET,
        worker_pid=_UNSET,
        error_summary=_UNSET,
    ) -> dict:
        updates = {}
        if status is not _UNSET:
            updates["status"] = str(status)
        if result_payload is not _UNSET:
            if not isinstance(result_payload, dict):
                raise ValueError("result_payload must be an object")
            updates["result_payload"] = json.dumps(
                result_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if progress_log is not _UNSET:
            if not isinstance(progress_log, list):
                raise ValueError("progress_log must be a list")
            updates["progress_log"] = json.dumps(
                progress_log,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        for key, value in (
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("worker_pid", worker_pid),
            ("error_summary", error_summary),
        ):
            if value is not _UNSET:
                updates[key] = value
        if not updates:
            current = self.get_job(job_id)
            if current is None:
                raise KeyError(f"unknown job: {job_id}")
            return current

        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = list(updates.values())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                f"""
                UPDATE jobs
                SET {assignments}
                WHERE job_id = ? AND status = ?
                """,
                (*values, str(job_id), str(expected_status)),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise RuntimeError(
                    f"job state changed before update: {job_id} ({expected_status})"
                )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            connection.commit()
            return _deserialize_job_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_artifacts(
        self, *, limit=None, offset=0, active_only=False, step=None, sort="created_desc"
    ) -> list[dict]:
        orders = {
            "created_desc": "created_at DESC, artifact_id DESC",
            "size_asc": "size_bytes ASC, created_at ASC, artifact_id ASC",
            "size_desc": "size_bytes DESC, created_at DESC, artifact_id DESC",
        }
        if sort not in orders:
            raise ValueError("排序方式无效")
        conditions = []
        parameters = []
        if active_only:
            conditions.append("deleted_at IS NULL")
        if step:
            conditions.append("step = ?")
            parameters.append(step)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        page_clause = ""
        if limit is not None:
            if int(limit) < 1 or int(offset) < 0:
                raise ValueError("分页范围无效")
            page_clause = "LIMIT ? OFFSET ?"
            parameters.extend((int(limit), int(offset)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    artifact_id,
                    job_id,
                    report_date,
                    step,
                    file_path,
                    file_type,
                    size_bytes,
                    created_at,
                    expires_at,
                    protected_by_pending,
                    deleted_at
                FROM artifacts
                {where}
                ORDER BY {orders[sort]}
                {page_clause}
                """,
                parameters,
            ).fetchall()

        artifacts = []
        for row in rows:
            item = dict(row)
            item["protected_by_pending"] = bool(item["protected_by_pending"])
            artifacts.append(item)
        return artifacts

    def active_artifact_totals(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT step, COUNT(*) AS count, SUM(size_bytes) AS total_bytes,
                          SUM(protected_by_pending) AS protected_count
                   FROM artifacts WHERE deleted_at IS NULL GROUP BY step ORDER BY step"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_artifacts_deleted(self, artifact_ids, deleted_at: str) -> int:
        normalized_ids = tuple(
            sorted({int(artifact_id) for artifact_id in artifact_ids})
        )
        if not normalized_ids:
            return 0
        normalized_deleted_at = str(deleted_at or "").strip()
        if not normalized_deleted_at:
            raise ValueError("deleted_at is required")
        placeholders = ", ".join("?" for _ in normalized_ids)
        with closing(self._connect()) as connection:
            with connection:
                updated = connection.execute(
                    f"""
                    UPDATE artifacts
                    SET deleted_at = ?
                    WHERE artifact_id IN ({placeholders}) AND deleted_at IS NULL
                    """,
                    (normalized_deleted_at, *normalized_ids),
                )
        return int(updated.rowcount)

    def _load_month_display_day_rows(
        self,
        connection: sqlite3.Connection,
        prefix: str,
    ) -> list[sqlite3.Row]:
        return self._load_display_day_rows_between(
            connection,
            f"{prefix}01",
            f"{prefix}31",
        )

    def _load_display_day_rows_between(
        self,
        connection: sqlite3.Connection,
        start_date: str,
        end_date: str,
        *,
        is_onsite: bool | None = None,
    ) -> list[sqlite3.Row]:
        mode_clause = ""
        mode_parameters: tuple[object, ...] = ()
        if is_onsite is not None:
            mode_clause = " AND d.is_onsite = ?"
            mode_parameters = (int(bool(is_onsite)),)

        actual_rows = connection.execute(
            f"""
            SELECT
                d.report_date,
                d.report_path,
                d.customer_name,
                d.is_onsite,
                d.content_digest,
                d.indexed_at,
                d.main_service_customers,
                d.report_date AS source_date,
                NULL AS attribution_source_date,
                a.target_date AS attribution_target_date,
                a.created_at AS attribution_created_at
            FROM report_days AS d
            LEFT JOIN date_attributions AS a ON a.source_date = d.report_date
            WHERE d.report_date >= ? AND d.report_date <= ?{mode_clause}
            """,
            (start_date, end_date, *mode_parameters),
        ).fetchall()
        attributed_rows = connection.execute(
            f"""
            SELECT
                a.target_date AS report_date,
                d.report_path,
                d.customer_name,
                d.is_onsite,
                d.content_digest,
                d.indexed_at,
                d.main_service_customers,
                a.source_date,
                a.source_date AS attribution_source_date,
                NULL AS attribution_target_date,
                a.created_at AS attribution_created_at
            FROM date_attributions AS a
            JOIN report_days AS d ON d.report_date = a.source_date
            WHERE a.target_date >= ? AND a.target_date <= ?{mode_clause}
            """,
            (start_date, end_date, *mode_parameters),
        ).fetchall()
        return sorted(
            [*actual_rows, *attributed_rows],
            key=lambda row: str(row["report_date"]),
        )

    def _load_display_day_row(
        self,
        connection: sqlite3.Connection,
        report_date: str,
    ) -> sqlite3.Row | None:
        actual_row = connection.execute(
            """
            SELECT
                d.report_date,
                d.report_path,
                d.customer_name,
                d.is_onsite,
                d.content_digest,
                d.indexed_at,
                d.main_service_customers,
                d.report_date AS source_date,
                NULL AS attribution_source_date,
                a.target_date AS attribution_target_date,
                a.created_at AS attribution_created_at
            FROM report_days AS d
            LEFT JOIN date_attributions AS a ON a.source_date = d.report_date
            WHERE d.report_date = ?
            """,
            (report_date,),
        ).fetchone()
        if actual_row is not None:
            return actual_row
        return connection.execute(
            """
            SELECT
                a.target_date AS report_date,
                d.report_path,
                d.customer_name,
                d.is_onsite,
                d.content_digest,
                d.indexed_at,
                d.main_service_customers,
                a.source_date,
                a.source_date AS attribution_source_date,
                NULL AS attribution_target_date,
                a.created_at AS attribution_created_at
            FROM date_attributions AS a
            JOIN report_days AS d ON d.report_date = a.source_date
            WHERE a.target_date = ?
            """,
            (report_date,),
        ).fetchone()

    def create_or_get_queued_job(self, record: dict) -> str:
        job_key = _require_text(record, "job_key")
        action = _require_text(record, "action")
        authorized_digest = _require_text(record, "authorized_digest")
        request_payload = record.get("request_payload")
        if not isinstance(request_payload, dict):
            raise ValueError("request_payload must be an object")
        if str(request_payload.get("unique_key", "")).strip() != job_key:
            raise ValueError("request_payload unique_key mismatch")

        candidate_job_id = str(record.get("job_id", "")).strip() or uuid.uuid4().hex
        created_at = str(record.get("created_at", "")).strip() or _now_iso()
        retry_of_job_id = _optional_text_or_none(record, "retry_of_job_id")
        request_json = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ", ".join("?" for _ in JOB_DEDUPLICATION_STATUSES)
            if action == KNOWLEDGE_BASE_SYNC_ACTION:
                active_sync = connection.execute(
                    "SELECT job_id FROM jobs WHERE action = ? AND status IN ('queued', 'running') ORDER BY created_at, job_id LIMIT 1",
                    (action,),
                ).fetchone()
                if active_sync is not None:
                    connection.commit()
                    return active_sync["job_id"]
            rows = connection.execute(
                f"""
                SELECT job_id, request_payload, action, authorized_digest
                FROM jobs
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC
                """,
                JOB_DEDUPLICATION_STATUSES,
            ).fetchall()
            for row in rows:
                payload = _decode_json(row["request_payload"], {})
                if isinstance(payload, dict) and payload.get("unique_key") == job_key:
                    if row["action"] != action or row["authorized_digest"] != authorized_digest:
                        raise ValueError("同一记录已有不同工时或内容的授权，请先核对原任务。")
                    connection.commit()
                    return row["job_id"]

            connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    action,
                    status,
                    authorized_digest,
                    request_payload,
                    result_payload,
                    progress_log,
                    created_at,
                    started_at,
                    finished_at,
                    worker_pid,
                    error_summary,
                    retry_of_job_id
                ) VALUES (?, ?, 'queued', ?, ?, '{}', '[]', ?, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    candidate_job_id,
                    action,
                    authorized_digest,
                    request_json,
                    created_at,
                    retry_of_job_id,
                ),
            )
            connection.commit()
            return candidate_job_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    def _build_day_payload(
        self,
        row: dict,
        submissions: list[dict],
        artifacts: list[dict],
        include_details: bool,
    ) -> dict:
        source_report_date = str(row.get("source_date") or row["report_date"])
        attribution_source_date = str(row.get("attribution_source_date") or "")
        attribution_target_date = str(row.get("attribution_target_date") or "")
        payload = {
            "report_date": row["report_date"],
            "source_report_date": source_report_date,
            "report_path": row["report_path"],
            "customer_name": row["customer_name"],
            "is_onsite": bool(row["is_onsite"]),
            "content_digest": row["content_digest"],
            "indexed_at": row["indexed_at"],
            "is_date_attribution": bool(attribution_source_date),
            "attribution_source_date": attribution_source_date,
            "attribution_target_date": attribution_target_date,
            "attribution_created_at": str(row.get("attribution_created_at") or ""),
            "artifact_count": len(artifacts),
        }

        submissions_by_step: dict[str, list[dict]] = {step: [] for step in SUBMISSION_STEPS}
        for item in submissions:
            submissions_by_step.setdefault(item["step"], []).append(item)

        for step in SUBMISSION_STEPS:
            records = submissions_by_step.get(step, [])
            status_records = _success_priority_records(records)
            payload[STEP_STATUS_KEYS[step]] = _resolve_status(step, status_records)
            payload[STEP_COUNT_KEYS[step]] = _count_business_records(
                step, status_records
            )

        main_step = "service_record" if payload["is_onsite"] else "remote_workorder"
        remote_records = submissions_by_step["remote_workorder"]
        optional_records = _success_priority_records([
            item for item in remote_records if item["includes_main_work"] is False
        ])
        payload["optional_remote_workorder_status"] = _resolve_status("remote_workorder", optional_records)
        payload["optional_remote_workorder_count"] = _count_business_records("remote_workorder", optional_records)
        raw_customers = row.get("main_service_customers")
        customers = json.loads(raw_customers) if raw_customers is not None else None
        main_records = [
            item for item in submissions_by_step[main_step]
            if main_step != "remote_workorder" or item["includes_main_work"] is not False
        ]
        customer_statuses = _main_service_customer_statuses(main_step, customers, main_records)
        payload["main_service_customers"] = customer_statuses
        if customer_statuses is None:
            payload["main_service_status"] = "legacy_ambiguous" if main_records else "unknown"
            payload["main_service_count"] = 0
        elif not customer_statuses:
            payload["main_service_status"] = "not_required"
            payload["main_service_count"] = 0
        else:
            statuses = [item["status"] for item in customer_statuses]
            unresolved = [status for status in statuses if status not in SUBMITTED_BUSINESS_STATUSES]
            payload["main_service_status"] = max(
                unresolved or statuses,
                key=lambda status: (status in MAIN_SERVICE_REVIEW_STATUSES, STATUS_PRIORITY.get(status, 0)),
            )
            payload["main_service_count"] = sum(status in SUBMITTED_BUSINESS_STATUSES for status in statuses)

        if include_details:
            payload["submissions"] = submissions
            payload["artifacts"] = artifacts

        return payload

    def _load_submissions(
        self,
        connection: sqlite3.Connection,
        report_dates: list[str],
    ) -> dict[str, list[dict]]:
        placeholders = ", ".join("?" for _ in report_dates)
        rows = connection.execute(
            f"""
            SELECT
                submission_records.report_date,
                step,
                service_customer,
                content_digest,
                submission_records.status,
                workorder_id,
                service_record_id,
                service_hours,
                crm_customer,
                service_category,
                local_recorded_at,
                crm_verified_at,
                source,
                includes_main_work,
                jobs.request_payload AS frozen_remote_request
            FROM submission_records
            LEFT JOIN jobs ON step = 'remote_workorder' AND source LIKE 'web_job:%'
                AND jobs.job_id = substr(source, 9) AND jobs.action = 'remote_workorder'
            WHERE submission_records.report_date IN ({placeholders})
            ORDER BY submission_records.report_date ASC, step ASC, service_customer ASC, source ASC
            """,
            report_dates,
        ).fetchall()
        grouped: dict[str, list[dict]] = {report_date: [] for report_date in report_dates}
        for row in rows:
            item = dict(row)
            frozen_request = item.pop("frozen_remote_request")
            item["includes_main_work"] = (
                bool(item["includes_main_work"]) if item["includes_main_work"] is not None
                else _frozen_remote_main_flag(item, frozen_request)
            )
            item["service_hours"] = (
                float(item["service_hours"])
                if item["service_hours"] is not None
                else None
            )
            grouped.setdefault(item["report_date"], []).append(item)
        for items in grouped.values():
            _resolve_remote_evidence_roles(items)
        return grouped

    def _load_artifacts(
        self,
        connection: sqlite3.Connection,
        report_dates: list[str],
    ) -> dict[str, list[dict]]:
        placeholders = ", ".join("?" for _ in report_dates)
        rows = connection.execute(
            f"""
            SELECT
                job_id,
                report_date,
                step,
                file_path,
                file_type,
                size_bytes,
                created_at,
                expires_at,
                protected_by_pending,
                deleted_at
            FROM artifacts
            WHERE report_date IN ({placeholders})
            ORDER BY report_date ASC, created_at ASC, file_path ASC
            """,
            report_dates,
        ).fetchall()
        grouped: dict[str, list[dict]] = {report_date: [] for report_date in report_dates}
        for row in rows:
            item = dict(row)
            item["protected_by_pending"] = bool(item["protected_by_pending"])
            grouped.setdefault(item["report_date"], []).append(item)
        return grouped

    def _load_remote_hour_overrides(
        self,
        connection: sqlite3.Connection,
        report_dates: list[str],
    ) -> dict[tuple[str, str, str], dict]:
        if not report_dates:
            return {}
        placeholders = ", ".join("?" for _ in report_dates)
        rows = connection.execute(
            f"""
            SELECT
                report_date,
                content_digest,
                workorder_id,
                base_service_hours,
                service_hours,
                crm_sync_status,
                crm_sync_job_id,
                crm_observed_hours,
                crm_updated_at,
                crm_error,
                created_at,
                updated_at
            FROM remote_workorder_hour_overrides
            WHERE report_date IN ({placeholders})
            """,
            report_dates,
        ).fetchall()
        return {
            (
                row["report_date"],
                row["content_digest"],
                row["workorder_id"],
            ): _deserialize_remote_hour_override(row)
            for row in rows
        }

    def _load_remote_workorder_bindings(
        self,
        connection: sqlite3.Connection,
        report_dates: list[str],
    ) -> dict[tuple[str, str], dict]:
        if not report_dates:
            return {}
        placeholders = ", ".join("?" for _ in report_dates)
        rows = connection.execute(
            f"""
            SELECT report_date, content_digest, original_workorder_id,
                   workorder_id, crm_customer, service_date, source_job_id,
                   verified_at, created_at, updated_at
            FROM remote_workorder_bindings
            WHERE report_date IN ({placeholders})
            """,
            report_dates,
        ).fetchall()
        return {
            (row["report_date"], row["content_digest"]): dict(row)
            for row in rows
        }

    @staticmethod
    def _chmod_if_supported(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except FileNotFoundError:
            return
        except OSError:
            return


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _require_text(record: Mapping[str, Any], key: str) -> str:
    value = str(record.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _require_nonempty_text(value, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_iso_date(value, field_name: str) -> str:
    normalized = _require_nonempty_text(value, field_name)
    try:
        return local_date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name}必须是 YYYY-MM-DD 日期。") from exc


def _optional_text(record: Mapping[str, Any], key: str) -> str:
    return str(record.get(key, "") or "").strip()


def _optional_text_or_none(record: Mapping[str, Any], key: str) -> str | None:
    value = _optional_text(record, key)
    return value or None


def _optional_float(record: dict, key: str) -> float | None:
    value = record.get(key)
    if value in (None, ""):
        return None
    return float(value)


def _optional_float_value(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _normalize_remote_hour_override_hours(value) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise ValueError("远程工单工时必须是 0.5 到 8 的数字。") from None
    if not 0.5 <= hours <= 8:
        raise ValueError("远程工单工时必须在 0.5 到 8 小时之间。")
    if abs(hours * 2 - round(hours * 2)) > 1e-9:
        raise ValueError("远程工单工时必须是 0.5 小时的倍数。")
    return hours


def _normalize_bool(value) -> int:
    return 1 if bool(value) else 0


def _require_status(record: dict) -> BusinessStatus:
    status = _optional_text(record, "status")
    if status not in BUSINESS_STATUSES:
        raise ValueError(f"unsupported status: {status}")
    return status  # type: ignore[return-value]


def _require_step(record: dict) -> str:
    step = _optional_text(record, "step")
    if step not in SUBMISSION_STEPS:
        raise ValueError(f"unsupported step: {step}")
    return step


def _require_manual_override_step(step) -> str:
    normalized_step = str(step or "").strip()
    if normalized_step not in MANUAL_OVERRIDE_STEPS:
        raise ValueError(f"unsupported manual override step: {normalized_step}")
    return normalized_step


def _require_calendar_day_status(status) -> str:
    normalized_status = str(status or "").strip()
    if normalized_status not in CALENDAR_DAY_STATUS_LABELS:
        raise ValueError("不支持的本机日历状态。")
    return normalized_status


def _manual_override_source(step: str) -> str:
    return f"{MANUAL_OVERRIDE_SOURCE_PREFIX}{step}"


def _main_work_flag(record: Mapping[str, Any]) -> bool | None:
    value = record.get("includes_main_work")
    return value if type(value) is bool else None


def _service_customer_key(customer) -> str:
    return "".join(str(customer or "").split()).casefold()


def _frozen_remote_main_flag(record: dict, raw_request: str | None) -> bool | None:
    if raw_request is None:
        return None
    request = _decode_json(raw_request, {})
    if not isinstance(request, dict) or (
        request.get("report_date") != record["report_date"]
        or _service_customer_key(request.get("customer")) != _service_customer_key(record["service_customer"])
        or not record["content_digest"]
        or request.get("content_digest") != record["content_digest"]
    ):
        return None
    payload = request.get("payload")
    return _main_work_flag(payload) if isinstance(payload, dict) else None


def _remote_receipt_key(record: dict) -> tuple[str, str] | None:
    customer = _service_customer_key(record["service_customer"])
    workorder_id = record["workorder_id"]
    return (customer, workorder_id) if customer and workorder_id else None


def _resolve_remote_evidence_roles(records: list[dict]) -> None:
    by_receipt: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        if record["step"] == "remote_workorder" and (key := _remote_receipt_key(record)):
            by_receipt.setdefault(key, []).append(record)
    for matching_records in by_receipt.values():
        roles = {item["includes_main_work"] for item in matching_records if item["includes_main_work"] is not None}
        if roles:
            role = next(iter(roles)) if len(roles) == 1 else None
            for item in matching_records:
                item["includes_main_work"] = role


def _main_service_customer_statuses(
    step: str, customers: list[str] | None, records: list[dict]
) -> list[dict] | None:
    if customers is None:
        return None
    names: dict[str, str] = {}
    for customer in customers:
        names.setdefault(_service_customer_key(customer), customer)
    by_customer: dict[str, list[dict]] = {}
    for record in records:
        by_customer.setdefault(_service_customer_key(record["service_customer"]), []).append(record)
    result = []
    for customer_key, customer in names.items():
        matching = by_customer.get(customer_key, [])
        scoped = matching if step == "service_record" else [
            item for item in matching if item["includes_main_work"] is True
        ]
        status = _resolve_status(step, _success_priority_records(scoped))
        if not scoped and (
            any(item["includes_main_work"] is None for item in matching)
            or any(item["includes_main_work"] is not False for item in by_customer.get("", []))
        ):
            status = "legacy_ambiguous"
        result.append({"customer_name": customer, "status": status})
    return result


def _success_priority_records(records: list[dict]) -> list[dict]:
    if any(record["status"] == MANUAL_OVERRIDE_STATUS for record in records):
        return records
    successful_customers = {
        str(record.get("service_customer") or "").strip()
        for record in records
        if record["status"] in SUBMITTED_BUSINESS_STATUSES
    }
    if not successful_customers:
        return records
    return [
        record
        for record in records
        if record["status"] not in SUPERSEDABLE_FAILURE_STATUSES
        or str(record.get("service_customer") or "").strip()
        not in successful_customers
    ]


def _resolve_status(step: str, records: list[dict]) -> str:
    if not records:
        return "unknown"
    statuses = {record["status"] for record in records}
    if MANUAL_OVERRIDE_STATUS in statuses:
        if "crm_verified" in statuses:
            return "crm_verified"
        if "not_found" in statuses:
            return "manual_conflict"
        return MANUAL_OVERRIDE_STATUS

    grouped_records: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        key = ("daily_log", "") if step == "daily_log" else _business_record_key(record)
        grouped_records.setdefault(key, []).append(record)
    resolved = [
        max(
            {record["status"] for record in group},
            key=lambda status: STATUS_PRIORITY.get(status, -1),
        )
        for group in grouped_records.values()
    ]
    unresolved = [
        status for status in resolved if status not in SUBMITTED_BUSINESS_STATUSES
    ]
    candidates = unresolved or resolved
    return max(candidates, key=lambda status: STATUS_PRIORITY.get(status, -1))


def _count_business_records(step: str, records: list[dict]) -> int:
    countable_records = [
        record for record in records if record["status"] != MANUAL_OVERRIDE_STATUS
    ] or records
    if step == "daily_log":
        return 1 if countable_records else 0
    return len({_business_record_key(record) for record in countable_records})


def _business_record_key(record: dict) -> tuple[str, str]:
    customer = str(record.get("service_customer") or "").strip()
    content_digest = str(record.get("content_digest") or "").strip()
    if content_digest:
        return customer, content_digest
    identifier = str(
        record.get("workorder_id") or record.get("service_record_id") or record.get("source") or ""
    ).strip()
    return customer, identifier


def _decode_json(value: str, fallback):
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return decoded


def _deserialize_job_row(row) -> dict:
    item = dict(row)
    request_payload = _decode_json(item["request_payload"], {})
    result_payload = _decode_json(item["result_payload"], {})
    progress_log = _decode_json(item["progress_log"], [])
    item["request_payload"] = request_payload if isinstance(request_payload, dict) else {}
    item["result_payload"] = result_payload if isinstance(result_payload, dict) else {}
    item["progress_log"] = progress_log if isinstance(progress_log, list) else []
    return item


def _deserialize_remote_hour_override(row) -> dict:
    item = dict(row)
    for key in ("base_service_hours", "service_hours", "crm_observed_hours"):
        if item.get(key) is not None:
            item[key] = float(item[key])
    return item


def _apply_remote_hour_overrides(
    submissions_by_date: dict[str, list[dict]],
    overrides: dict[tuple[str, str, str], dict],
) -> dict[str, list[dict]]:
    decorated: dict[str, list[dict]] = {}
    for report_date, records in submissions_by_date.items():
        decorated_records = []
        for record in records:
            item = dict(record)
            if item.get("step") == "remote_workorder":
                key = (
                    str(report_date),
                    str(item.get("content_digest") or "").strip(),
                    str(item.get("workorder_id") or "").strip(),
                )
                override = overrides.get(key)
                if override is not None:
                    confirmed_hours = item.get("service_hours")
                    observed_hours = override.get("crm_observed_hours")
                    if (
                        override.get("crm_sync_status")
                        in {"crm_verified", "crm_mismatch"}
                        and observed_hours is not None
                    ):
                        confirmed_hours = observed_hours
                    item["service_hours"] = confirmed_hours
                    item["base_service_hours"] = (
                        confirmed_hours
                        if confirmed_hours is not None
                        else override.get("base_service_hours")
                    )
                    item["remote_hour_target_hours"] = override["service_hours"]
                    item["has_remote_hour_override"] = True
                    item["remote_hour_sync_status"] = override["crm_sync_status"]
                    item["remote_hour_override_updated_at"] = override["updated_at"]
                    item["remote_hour_sync_job_id"] = override["crm_sync_job_id"]
                    item["remote_hour_sync_error"] = override["crm_error"]
                else:
                    item["base_service_hours"] = item.get("service_hours")
                    item["remote_hour_target_hours"] = None
                    item["has_remote_hour_override"] = False
            decorated_records.append(item)
        decorated[report_date] = decorated_records
    return decorated


def _apply_remote_workorder_bindings(
    submissions_by_date: dict[str, list[dict]],
    bindings: dict[tuple[str, str], dict],
) -> dict[str, list[dict]]:
    decorated: dict[str, list[dict]] = {}
    for report_date, records in submissions_by_date.items():
        decorated_records = []
        for record in records:
            item = dict(record)
            if item.get("step") == "remote_workorder":
                item["has_remote_workorder_binding"] = False
                binding = bindings.get(
                    (
                        str(report_date),
                        str(item.get("content_digest") or "").strip(),
                    )
                )
                if binding is not None:
                    item["original_workorder_id"] = binding["original_workorder_id"]
                    item["workorder_id"] = binding["workorder_id"]
                    item["crm_customer"] = binding["crm_customer"]
                    item["has_remote_workorder_binding"] = True
                    item["remote_workorder_binding_job_id"] = binding["source_job_id"]
                    item["remote_workorder_binding_verified_at"] = binding["verified_at"]
            decorated_records.append(item)
        decorated[report_date] = decorated_records
    return decorated
