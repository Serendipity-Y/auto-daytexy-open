from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from packages.report_domain.paths import resolve_fxiaoke_trace_root

from .jobs import TERMINAL_JOB_STATUSES


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    artifact_id: int
    file_path: Path
    size_bytes: int
    modified_ns: int
    device: int
    inode: int
    changed_ns: int
    root_path: Path
    root_device: int
    root_inode: int


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    candidates: tuple[CleanupCandidate, ...]
    total_bytes: int
    digest: str


@dataclass(frozen=True, slots=True)
class CleanupResult:
    deleted_count: int
    deleted_bytes: int
    deleted_at: str


def build_cleanup_preview(store, artifact_root, now=None, *, additional_roots=()) -> CleanupPreview:
    current = now or datetime.now()
    roots = _cleanup_roots(store, artifact_root, additional_roots)
    job_paths = _protected_job_paths(store)
    candidates = []
    for artifact in store.list_artifacts():
        if artifact.get("deleted_at"):
            continue
        source_path = Path(artifact["file_path"]).expanduser().absolute()
        original_root = Path(artifact_root).expanduser().absolute()
        if _is_within(source_path, original_root):
            source_path = roots[0] / source_path.relative_to(original_root)
        if not any(_is_within(source_path, root) for root in roots):
            raise ValueError(f"失败留痕路径不在留痕根目录内：{source_path}")
        if _has_symbolic_component(source_path):
            raise ValueError(f"失败留痕包含不允许清理的符号链接：{source_path}")
        resolved = source_path.resolve(strict=False)
        root = next((root for root in roots if _is_within(resolved, root)), None)
        if root is None:
            raise ValueError(f"失败留痕路径不在留痕根目录内：{source_path}")
        if artifact.get("protected_by_pending") or str(resolved) in job_paths:
            continue
        if _has_non_terminal_job(store, artifact.get("job_id")):
            continue
        if not _is_expired(artifact.get("expires_at"), current):
            continue
        if not resolved.is_file():
            continue
        stat = resolved.stat()
        root_stat = root.stat()
        candidates.append(
            CleanupCandidate(
                artifact_id=int(artifact["artifact_id"]),
                file_path=resolved,
                size_bytes=int(stat.st_size),
                modified_ns=int(stat.st_mtime_ns),
                device=int(stat.st_dev),
                inode=int(stat.st_ino),
                changed_ns=int(stat.st_ctime_ns),
                root_path=root,
                root_device=int(root_stat.st_dev),
                root_inode=int(root_stat.st_ino),
            )
        )
    candidates.sort(key=lambda item: (str(item.file_path), item.artifact_id))
    payload = [
        {
            "artifact_id": item.artifact_id,
            "file_path": str(item.file_path),
            "size_bytes": item.size_bytes,
            "modified_ns": item.modified_ns,
            "device": item.device,
            "inode": item.inode,
            "changed_ns": item.changed_ns,
            "root_path": str(item.root_path),
            "root_device": item.root_device,
            "root_inode": item.root_inode,
            "protected": False,
        }
        for item in candidates
    ]
    return CleanupPreview(
        candidates=tuple(candidates),
        total_bytes=sum(item.size_bytes for item in candidates),
        digest=_digest(payload),
    )


def execute_cleanup(store, artifact_root, preview_digest, now=None, *, additional_roots=()) -> CleanupResult:
    current = now or datetime.now()
    preview = build_cleanup_preview(store, artifact_root, now=current, additional_roots=additional_roots)
    submitted_digest = str(preview_digest or "").strip()
    if not secrets.compare_digest(preview.digest.encode("utf-8"), submitted_digest.encode("utf-8")):
        raise ValueError("清理预览已变化，请重新预览后再确认。")

    for candidate in preview.candidates:
        _validate_candidate(candidate)

    deleted_ids = []
    deleted_bytes = 0
    deleted_at = current.isoformat(timespec="seconds")
    try:
        for candidate in preview.candidates:
            _validate_candidate(candidate)
            candidate.file_path.unlink()
            deleted_ids.append(candidate.artifact_id)
            deleted_bytes += candidate.size_bytes
            _remove_empty_parents(candidate.file_path.parent, candidate.root_path)
    finally:
        if deleted_ids:
            store.mark_artifacts_deleted(deleted_ids, deleted_at)
    return CleanupResult(
        deleted_count=len(deleted_ids),
        deleted_bytes=deleted_bytes,
        deleted_at=deleted_at,
    )


def _validate_candidate(candidate: CleanupCandidate) -> None:
    path = candidate.file_path
    if (
        _has_symbolic_component(path) or path.resolve(strict=False) != path
        or not _is_within(path, candidate.root_path) or not path.is_file()
    ):
        raise ValueError("清理预览已变化，请重新预览后再确认。")
    root_stat = candidate.root_path.stat()
    if root_stat.st_dev != candidate.root_device or root_stat.st_ino != candidate.root_inode:
        raise ValueError("清理预览已变化，请重新预览后再确认。")
    stat = path.stat()
    if (
        stat.st_size != candidate.size_bytes
        or stat.st_mtime_ns != candidate.modified_ns
        or stat.st_dev != candidate.device
        or stat.st_ino != candidate.inode
        or stat.st_ctime_ns != candidate.changed_ns
    ):
        raise ValueError("清理预览已变化，请重新预览后再确认。")


def _has_non_terminal_job(store, job_id) -> bool:
    normalized = str(job_id or "").strip()
    if not normalized:
        return False
    job = store.get_job(normalized)
    return job is not None and (
        job["status"] not in TERMINAL_JOB_STATUSES or job["status"] == "uncertain"
    )


def _protected_job_paths(store) -> set[str]:
    paths: set[str] = set()
    for job in store.list_jobs():
        if job["status"] in TERMINAL_JOB_STATUSES and job["status"] != "uncertain":
            continue
        artifacts = job.get("result_payload", {}).get("artifacts", [])
        if isinstance(artifacts, list):
            paths.update(str(Path(value).expanduser().resolve(strict=False)) for value in artifacts if isinstance(value, str) and value.strip())
    return paths


def _cleanup_roots(store, artifact_root, additional_roots) -> tuple[Path, ...]:
    original = Path(artifact_root).expanduser().absolute()
    root = original.parent.resolve() / original.name
    roots = [root]
    for value in additional_roots:
        expected_trace_root = resolve_fxiaoke_trace_root(root)
        candidate = Path(value).expanduser().absolute()
        if candidate != expected_trace_root:
            raise ValueError("失败留痕路径不在留痕根目录内：额外根目录未授权")
        roots.append(candidate)
    for candidate in roots:
        if _has_symbolic_component(candidate):
            raise ValueError(f"失败留痕包含不允许清理的符号链接：{candidate}")
    return tuple(roots)


def _has_symbolic_component(path: Path) -> bool:
    return any(component.is_symlink() for component in (path, *path.parents))


def _is_expired(value, now: datetime) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"失败留痕过期时间无效：{value}") from exc
    return expires_at.timestamp() <= now.timestamp()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_empty_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root and _is_within(current, root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _digest(payload) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
