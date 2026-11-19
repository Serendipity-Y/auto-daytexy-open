from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import datetime
from typing import Any

from integrations.fxiaoke import crm_enabled

from .knowledge_base import KNOWLEDGE_BASE_SYNC_ACTION


MAX_PROGRESS_CHARS = 4_000
LOGGER = logging.getLogger(__name__)


class JobStatus:
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    RUNNING = "running"
    CRM_ID_CAPTURED = "crm_id_captured"
    CRM_VERIFIED = "crm_verified"
    PENDING_RETRY = "pending_retry"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"
    SKIPPED = "skipped"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    COMPLETED = "completed"


TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.CRM_VERIFIED,
        JobStatus.PENDING_RETRY,
        JobStatus.UNCERTAIN,
        JobStatus.CONFLICT,
        JobStatus.SKIPPED,
        JobStatus.NOT_FOUND,
        JobStatus.UNKNOWN,
        JobStatus.COMPLETED,
    }
)
ACTIVE_WORKER_STATUSES = (JobStatus.RUNNING, JobStatus.CRM_ID_CAPTURED)

ALLOWED_TRANSITIONS = {
    JobStatus.AWAITING_CONFIRMATION: {JobStatus.QUEUED, JobStatus.SKIPPED},
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.SKIPPED},
    JobStatus.RUNNING: {
        JobStatus.QUEUED,
        JobStatus.CRM_ID_CAPTURED,
        JobStatus.CRM_VERIFIED,
        JobStatus.PENDING_RETRY,
        JobStatus.UNCERTAIN,
        JobStatus.CONFLICT,
        JobStatus.NOT_FOUND,
        JobStatus.UNKNOWN,
        JobStatus.COMPLETED,
    },
    JobStatus.CRM_ID_CAPTURED: {
        JobStatus.CRM_VERIFIED,
        JobStatus.UNCERTAIN,
    },
    JobStatus.CRM_VERIFIED: set(),
    JobStatus.PENDING_RETRY: set(),
    JobStatus.UNCERTAIN: {JobStatus.CRM_VERIFIED},
    JobStatus.CONFLICT: set(),
    JobStatus.SKIPPED: set(),
    JobStatus.NOT_FOUND: set(),
    JobStatus.UNKNOWN: set(),
}

_UNSET = object()


class JobTransitionError(ValueError):
    pass


def validate_job_transition(current: str, target: str) -> None:
    current_status = str(current or "").strip()
    target_status = str(target or "").strip()
    if current_status == target_status:
        return
    if target_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
        raise JobTransitionError(
            f"unsupported job transition: {current_status} -> {target_status}"
        )


def transition_job(
    store,
    job_id: str,
    target_status: str,
    *,
    worker_pid=_UNSET,
    result_payload=_UNSET,
    progress_log=_UNSET,
    error_summary=_UNSET,
):
    current = store.get_job(job_id)
    if current is None:
        raise KeyError(f"unknown job: {job_id}")
    current_status = current["status"]
    validate_job_transition(current_status, target_status)

    changes: dict[str, Any] = {"status": target_status}
    if result_payload is not _UNSET:
        changes["result_payload"] = result_payload
    if progress_log is not _UNSET:
        changes["progress_log"] = progress_log
    if error_summary is not _UNSET:
        changes["error_summary"] = error_summary
    if worker_pid is not _UNSET:
        changes["worker_pid"] = worker_pid

    now = _now_iso()
    if target_status == JobStatus.RUNNING and not current.get("started_at"):
        changes["started_at"] = now
    if target_status == JobStatus.QUEUED:
        changes.update(
            {
                "started_at": None,
                "finished_at": None,
                "worker_pid": None,
                "error_summary": None,
            }
        )
    elif target_status in TERMINAL_JOB_STATUSES:
        changes["finished_at"] = now
        if worker_pid is _UNSET:
            changes["worker_pid"] = None

    return store.update_job(job_id, current_status, **changes)


def append_job_progress(store, job_id: str, message: str) -> dict:
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    cleaned = str(message or "").strip()
    if not cleaned:
        return job
    progress = [str(item) for item in job.get("progress_log", []) if str(item).strip()]
    progress.append(cleaned)
    progress = _cap_progress(progress)
    return store.update_job(
        job_id,
        job["status"],
        status=job["status"],
        progress_log=progress,
    )


def record_job_milestone(store, job_id: str, milestone: str, **fields) -> dict:
    allowed_milestones = {
        "browser_started",
        "submit_clicked",
        "crm_id_captured",
        "crm_verified",
    }
    normalized = str(milestone or "").strip()
    if normalized not in allowed_milestones:
        raise ValueError(f"unsupported job milestone: {normalized}")
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    result = dict(job.get("result_payload", {}))
    result[normalized] = True
    result[f"{normalized}_at"] = _now_iso()
    for key in ("workorder_id", "service_record_id", "feed_id", "crm_customer"):
        if fields.get(key):
            result[key] = str(fields[key]).strip()

    target_status = job["status"]
    if normalized == "crm_id_captured":
        target_status = JobStatus.CRM_ID_CAPTURED
    elif normalized == "crm_verified":
        target_status = JobStatus.CRM_VERIFIED
    return transition_job(
        store,
        job_id,
        target_status,
        result_payload=result,
    )


def recover_interrupted_jobs(store, running_jobs=None) -> int:
    recovered = 0
    jobs = (
        store.list_jobs_by_status(ACTIVE_WORKER_STATUSES)
        if running_jobs is None
        else running_jobs
    )
    for job in jobs:
        if job.get("status") not in ACTIVE_WORKER_STATUSES:
            continue
        worker_pid = job.get("worker_pid")
        if worker_pid and _pid_exists(int(worker_pid)):
            continue
        if job["status"] == JobStatus.CRM_ID_CAPTURED and not worker_pid:
            continue
        result = job.get("result_payload", {})
        if _has_captured_id(result):
            target = JobStatus.CRM_ID_CAPTURED
            message = "进程中断，但已保存 CRM 编号；仅允许继续复核。"
        elif _is_safe_pre_submit_failure(result):
            target = JobStatus.PENDING_RETRY
            message = "进程在明确提交前失败，需重新预览后重试。"
        elif (result.get("submit_clicked") or result.get("browser_started") or result.get("routing_conflict")
              or result.get("status") in {"uncertain", "evidence_error", "crm_id_captured"}):
            target = JobStatus.UNCERTAIN
            message = "进程在浏览器处理期间中断，提交阶段缺少完整证据，需要 CRM 复核。"
        else:
            target = JobStatus.QUEUED
            message = "进程在提交前中断，任务已安全地重新排队。"
        progress = _cap_progress([*job.get("progress_log", []), message])
        transition_job(
            store,
            job["job_id"],
            target,
            worker_pid=None,
            progress_log=progress,
            error_summary=message if target != JobStatus.QUEUED else None,
        )
        recovered += 1
    return recovered


class JobCoordinator:
    def __init__(self, store, worker_command, poll_seconds=0.5):
        command = [str(part) for part in worker_command]
        if not command or any(not part for part in command):
            raise ValueError("worker_command must be a non-empty argument list")
        self.store = store
        self.worker_command = tuple(command)
        self.poll_seconds = max(float(poll_seconds), 0.01)
        self._process = None
        self._active_job_id = None
        self._stop_event = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        recover_interrupted_jobs(self.store)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="auto-daytexy-job-coordinator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 4))
        self._thread = None

    def run_once(self):
        if self._process is not None:
            active_job_id = self._active_job_id
            if self._process.poll() is None:
                return active_job_id
            self._handle_worker_exit(active_job_id, self._process.returncode, worker_pid=self._process.pid)
            self._process = None
            self._active_job_id = None
            return active_job_id

        active_jobs = self.store.list_jobs_by_status(
            [JobStatus.QUEUED, *ACTIVE_WORKER_STATUSES]
        )
        running_jobs = [
            job for job in active_jobs if job.get("status") in ACTIVE_WORKER_STATUSES
        ]
        live_external_job = _find_live_running_job(self.store, running_jobs)
        if live_external_job is not None:
            return live_external_job["job_id"]
        recover_interrupted_jobs(self.store, running_jobs)
        if not active_jobs:
            return None

        job = (
            self.store.claim_next_queued_job()
            if crm_enabled()
            else self.store.claim_next_queued_job(action=KNOWLEDGE_BASE_SYNC_ACTION)
        )
        if job is None:
            return None
        job_id = job["job_id"]
        command = [
            *self.worker_command,
            "--job-id",
            job_id,
            "--database",
            str(self.store.database_path),
        ]
        try:
            process = subprocess.Popen(command, shell=False)
        except Exception as exc:
            message = f"无法启动任务进程：{exc}"
            transition_job(
                self.store,
                job_id,
                JobStatus.PENDING_RETRY,
                error_summary=message,
                progress_log=_cap_progress([*job.get("progress_log", []), message]),
            )
            return job_id

        self._process = process
        self._active_job_id = job_id
        try:
            self.store.attach_worker_pid(job_id, process.pid)
            append_job_progress(self.store, job_id, f"工作进程已启动：PID {process.pid}")
        except RuntimeError:
            current = self.store.get_job(job_id)
            if current is None or (current["status"] in ACTIVE_WORKER_STATUSES
                                   and current.get("worker_pid") in (None, process.pid)):
                raise
        return job_id

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("工作任务协调器本轮处理失败，将在下一轮继续")

    def _handle_worker_exit(self, job_id, returncode, *, worker_pid=None) -> None:
        if not job_id:
            return
        job = self.store.get_job(job_id)
        if job is None or job["status"] not in ACTIVE_WORKER_STATUSES:
            return
        if worker_pid is not None and job.get("worker_pid") not in (None, worker_pid):
            return
        result = job.get("result_payload", {})
        if _has_captured_id(result):
            target = JobStatus.CRM_ID_CAPTURED
            message = f"工作进程退出（{returncode}），CRM 编号已保存，等待复核。"
        elif _is_safe_pre_submit_failure(result):
            target = JobStatus.PENDING_RETRY
            message = f"工作进程退出（{returncode}），明确在提交前失败，可重新预览后重试。"
        elif (result.get("submit_clicked") or result.get("browser_started") or result.get("routing_conflict")
              or result.get("status") in {"uncertain", "evidence_error", "crm_id_captured"}):
            target = JobStatus.UNCERTAIN
            message = f"工作进程退出（{returncode}），提交结果不确定，必须先复核。"
        else:
            target = JobStatus.PENDING_RETRY
            message = f"工作进程退出（{returncode}），尚未点击提交，可重新预览后重试。"
        transition_job(
            self.store,
            job_id,
            target,
            worker_pid=None,
            error_summary=message,
            progress_log=_cap_progress([*job.get("progress_log", []), message]),
        )


def _has_captured_id(result: dict) -> bool:
    target = result.get("routing_target")
    existing_target_id = isinstance(target, dict) and target.get("workorder_id") == result.get("workorder_id")
    return bool(
        result.get("crm_id_captured")
        or (result.get("workorder_id") and not existing_target_id)
        or result.get("service_record_id")
        or result.get("feed_id")
    )


def _is_safe_pre_submit_failure(result: dict) -> bool:
    return bool(
        result.get("submission_phase") == "before_submit"
        and result.get("retry_action") == "safe_retry"
        and not result.get("submit_clicked") and not result.get("routing_conflict")
        and result.get("status") not in {"uncertain", "crm_id_captured", "evidence_error"}
        and not _has_captured_id(result)
    )


def _find_live_running_job(store, running_jobs=None):
    jobs = (
        store.list_jobs_by_status(ACTIVE_WORKER_STATUSES)
        if running_jobs is None
        else running_jobs
    )
    for job in jobs:
        if job.get("status") not in ACTIVE_WORKER_STATUSES:
            continue
        worker_pid = job.get("worker_pid")
        if worker_pid and _pid_exists(int(worker_pid)):
            return job
    return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cap_progress(messages) -> list[str]:
    kept: list[str] = []
    used = 0
    for message in reversed([str(item) for item in messages if str(item).strip()]):
        cost = len(message) + (1 if kept else 0)
        if kept and used + cost > MAX_PROGRESS_CHARS:
            break
        if not kept and cost > MAX_PROGRESS_CHARS:
            message = message[-MAX_PROGRESS_CHARS:]
            cost = len(message)
        kept.append(message)
        used += cost
    kept.reverse()
    return kept


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
