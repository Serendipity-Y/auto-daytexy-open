"""Private, date-scoped recovery for completed quick-input fields."""
import hashlib
import json
import os
import stat
import tempfile
from datetime import date, datetime
from contextlib import contextmanager
from pathlib import Path
from packages.report_domain.history import fcntl


DEFAULT_QUICK_DRAFT_ROOT = Path.home() / ".config/auto-daytexy/quick-drafts"
_active_sessions: dict[tuple[str, str], tuple[Path, str] | None] = {}
_observed_versions: dict[tuple[str, str], tuple[Path, str | None]] = {}


class QuickDraftError(OSError):
    """A draft could not be safely read or changed; its data is retained."""


def _identity(report_path, report_date=None):
    path = str(Path(report_path).expanduser().absolute())
    day = report_date or date.today().isoformat()
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("草稿日期必须是 YYYY-MM-DD")
    return path, day


def quick_draft_path(report_path, draft_root=None, report_date=None):
    path, day = _identity(report_path, report_date)
    root = Path(draft_root) if draft_root is not None else DEFAULT_QUICK_DRAFT_ROOT
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return root / f"{day}-{digest}.json"


def begin_quick_draft_session(report_path, report_date=None):
    """Forget a previous run's cleanup token before recovery/ignore is chosen."""
    _active_sessions[_identity(report_path, report_date)] = None
    _observed_versions.pop(_identity(report_path, report_date), None)


@contextmanager
def _draft_lock(root):
    """Serialize the missing/read/check/replace boundary between independent rb runs."""
    _check_root(root, create=True)
    descriptor = os.open(root / ".draft.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise QuickDraftError("草稿锁不是当前用户的普通文件")
        os.fchmod(descriptor, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _draft_digest(path):
    if not _check_file(path):
        return None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_observed_version(identity, path):
    observed = _observed_versions.get(identity)
    if observed is None:
        return
    current = _draft_digest(path)
    if observed != (path, current):
        _active_sessions[identity] = None
        raise QuickDraftError("草稿已被另一次输入更新，本次停止自动保存并保留其他输入")


def clear_quick_draft_after_save(report_path, report_date=None):
    """Clear only this session's last autosave, preserving ignored/newer drafts."""
    identity = _identity(report_path, report_date)
    active = _active_sessions.pop(identity, None)
    if active is None:
        return False
    path, digest = active
    with _draft_lock(path.parent):
        if not _check_file(path):
            return False
        if _draft_digest(path) != digest:
            raise QuickDraftError("草稿在本次输入后已变化，保留该文件以免删除其他输入")
        path.unlink()
        _observed_versions.pop(identity, None)
    return True


def _check_root(root, create=False):
    if root.is_symlink():
        raise QuickDraftError("草稿目录不能是符号链接")
    if not root.exists():
        if not create:
            return False
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise QuickDraftError("草稿路径不是安全目录")
    if root.stat().st_uid != os.getuid():
        raise QuickDraftError("草稿目录不属于当前用户")
    root.chmod(0o700)
    return True


def _check_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise QuickDraftError("草稿必须是当前用户的普通文件，不能使用符号链接")
    return True


def _load_quick_draft_unlocked(report_path, draft_root=None, report_date=None):
    path = quick_draft_path(report_path, draft_root, report_date)
    if not _check_root(path.parent) or not _check_file(path):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            content = stream.read(1_048_577)
        if len(content) > 1_048_576:
            raise ValueError("草稿过大")
        document = json.loads(content)
        identity, day = _identity(report_path, report_date)
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or document.get("report_path") != identity
            or document.get("report_date") != day
            or not isinstance(document.get("fields"), dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in document["fields"].items())
        ):
            raise ValueError("草稿身份或字段不匹配")
        path.chmod(0o600)
        return document["fields"]
    except (OSError, ValueError, UnicodeError) as exc:
        raise QuickDraftError(f"草稿读取失败，原文件已保留：{exc}") from exc


def load_quick_draft(report_path, draft_root=None, report_date=None):
    path = quick_draft_path(report_path, draft_root, report_date)
    with _draft_lock(path.parent):
        identity = _identity(report_path, report_date)
        if identity in _active_sessions:
            _observed_versions[identity] = (path, _draft_digest(path))
        return _load_quick_draft_unlocked(report_path, draft_root, report_date)


def save_quick_draft(report_path, fields, draft_root=None, report_date=None):
    path = quick_draft_path(report_path, draft_root, report_date)
    with _draft_lock(path.parent):
        return _save_quick_draft_unlocked(report_path, fields, draft_root, report_date)


def _save_quick_draft_unlocked(report_path, fields, draft_root=None, report_date=None):
    path = quick_draft_path(report_path, draft_root, report_date)
    _check_root(path.parent, create=True)
    identity, day = _identity(report_path, report_date)
    _check_observed_version((identity, day), path)
    # Never overwrite corrupt data or a link without the user's explicit deletion.
    if _check_file(path):
        _load_quick_draft_unlocked(report_path, draft_root, report_date)
    if not isinstance(fields, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in fields.items()):
        raise ValueError("草稿字段必须是文本")
    document = {"version": 1, "report_path": identity, "report_date": day,
                "updated_at": datetime.now().isoformat(timespec="seconds"), "fields": fields}
    serialized = json.dumps(document, ensure_ascii=False) + "\n"
    if len(serialized) > 1_048_576:
        raise ValueError("草稿过大，已有文件保留")
    descriptor, temporary = tempfile.mkstemp(prefix=".quick-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        _check_file(path)
        os.replace(temporary, path)
        if (identity, day) in _active_sessions:
            _active_sessions[(identity, day)] = (path, hashlib.sha256(serialized.encode("utf-8")).hexdigest())
            _observed_versions[(identity, day)] = (path, hashlib.sha256(serialized.encode("utf-8")).hexdigest())
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def clear_quick_draft(report_path, draft_root=None, report_date=None):
    """Clear only this report's draft for the specified day after a local save."""
    path = quick_draft_path(report_path, draft_root, report_date)
    identity = _identity(report_path, report_date)
    with _draft_lock(path.parent):
        _check_observed_version(identity, path)
        if not _check_file(path):
            return False
        path.unlink()
        if identity in _active_sessions:
            _active_sessions[identity] = None
            _observed_versions[identity] = (path, None)
    return True
