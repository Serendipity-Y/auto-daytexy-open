#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .webapp.actions import execute_job
from .webapp.config import load_web_config
from .webapp.jobs import JobStatus, _has_captured_id, _is_safe_pre_submit_failure, transition_job
from .webapp.store import WebStore


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="执行一个已由本机 Web 管理端授权的任务。")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--database", required=True)
    return parser.parse_args(argv)


def validate_database_path(supplied, trusted) -> Path:
    supplied_path = Path(supplied).expanduser().resolve()
    trusted_path = Path(trusted).expanduser().resolve()
    if supplied_path != trusted_path:
        raise ValueError("database path does not match the trusted Web configuration")
    return supplied_path


def _safe_print(message, *, file=None) -> None:
    try:
        print(message, file=file, flush=True)
    except BrokenPipeError:
        pass


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_web_config()
    database_path = validate_database_path(args.database, config.database_path)
    store = WebStore(database_path)
    store.initialize()

    try:
        job = store.attach_worker_pid(args.job_id, os.getpid())
        if job["status"] != JobStatus.RUNNING or job.get("worker_pid") != os.getpid():
            raise RuntimeError("任务不属于本工作进程或已进入不可执行阶段。")
        execute_job(
            store,
            args.job_id,
            progress_callback=_safe_print,
        )
    except Exception as exc:
        _record_worker_failure(store, args.job_id, exc)
        _safe_print(f"任务执行失败：{exc}", file=sys.stderr)
        return 1

    job = store.get_job(args.job_id)
    if job is not None and job["status"] == JobStatus.RUNNING:
        _record_worker_failure(store, args.job_id, RuntimeError("任务处理器退出时没有写入终结状态。"))
        return 1
    return 0


def _record_worker_failure(store, job_id: str, exc: Exception) -> None:
    job = store.get_job(job_id)
    if job is None or job["status"] != JobStatus.RUNNING:
        return
    if job.get("worker_pid") not in (None, os.getpid()):
        return
    result = job.get("result_payload", {})
    if _has_captured_id(result):
        target = JobStatus.CRM_ID_CAPTURED
    elif _is_safe_pre_submit_failure(result):
        target = JobStatus.PENDING_RETRY
    elif (result.get("submit_clicked") or result.get("browser_started") or result.get("routing_conflict")
          or result.get("status") in {"uncertain", "evidence_error", "crm_id_captured"}):
        target = JobStatus.UNCERTAIN
    else:
        target = JobStatus.PENDING_RETRY
    message = str(exc) or exc.__class__.__name__
    transition_job(
        store,
        job_id,
        target,
        error_summary=message,
        progress_log=[*job.get("progress_log", []), message],
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
