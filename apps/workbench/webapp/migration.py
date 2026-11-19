from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    legacy_storage_state: Path
    private_storage_state: Path
    legacy_profile: Path
    private_profile: Path
    copy_storage_state: bool
    copy_profile: bool


@dataclass(frozen=True, slots=True)
class MigrationResult:
    copied_paths: tuple[Path, ...]
    planned_paths: tuple[Path, ...]
    dry_run: bool


def plan_login_state_migration(project_root, private_root) -> MigrationPlan:
    project = Path(project_root).expanduser().resolve()
    private = Path(private_root).expanduser().resolve()
    legacy_storage = project / ".fxiaoke_storage_state.json"
    private_storage = private / "fxiaoke_storage_state.json"
    legacy_profile = project / ".fxiaoke_browser_profile"
    private_profile = private / "fxiaoke_browser_profile"
    return MigrationPlan(
        legacy_storage_state=legacy_storage,
        private_storage_state=private_storage,
        legacy_profile=legacy_profile,
        private_profile=private_profile,
        copy_storage_state=legacy_storage.is_file() and not (private_storage.exists() or private_storage.is_symlink()),
        copy_profile=legacy_profile.is_dir() and not (private_profile.exists() or private_profile.is_symlink()),
    )


def execute_login_state_migration(plan: MigrationPlan, *, dry_run=False) -> MigrationResult:
    planned = tuple(
        path
        for enabled, path in (
            (plan.copy_storage_state, plan.private_storage_state),
            (plan.copy_profile, plan.private_profile),
        )
        if enabled
    )
    if dry_run or not planned:
        return MigrationResult((), planned, bool(dry_run))

    private_root = plan.private_storage_state.parent
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod(private_root, 0o700)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".login-state-", dir=private_root)
    )
    copied = []
    try:
        staged_storage = staging_root / plan.private_storage_state.name
        staged_profile = staging_root / plan.private_profile.name
        if plan.copy_storage_state:
            if plan.legacy_storage_state.is_symlink():
                raise ValueError("登录状态迁移来源不允许符号链接。")
            shutil.copy2(plan.legacy_storage_state, staged_storage)
            _validate_storage_json(staged_storage)
            _chmod(staged_storage, 0o600)
        if plan.copy_profile:
            if plan.legacy_profile.is_symlink():
                raise ValueError("浏览器配置迁移来源不允许符号链接。")
            singleton_names = {"SingletonLock", "SingletonCookie", "SingletonSocket"}
            shutil.copytree(
                plan.legacy_profile, staged_profile, symlinks=True,
                ignore=lambda directory, names: [
                    name for name in names
                    if Path(directory) == plan.legacy_profile and name in singleton_names
                ],
            )
            _secure_profile_tree(staged_profile)

        for enabled, staged, destination in (
            (plan.copy_storage_state, staged_storage, plan.private_storage_state),
            (plan.copy_profile, staged_profile, plan.private_profile),
        ):
            if not enabled:
                continue
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(f"迁移目标已存在，未覆盖：{destination}")
            os.replace(staged, destination)
            copied.append(destination)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return MigrationResult(tuple(copied), planned, False)


def _validate_storage_json(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"纷享销客登录状态 JSON 无效：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"纷享销客登录状态 JSON 无效：{path}")
    cookies = payload.get("cookies", [])
    origins = payload.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError(f"纷享销客登录状态 JSON 无效：{path}")


def _secure_profile_tree(root: Path) -> None:
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("浏览器配置中包含不允许迁移的符号链接。")
    _chmod(root, 0o700)
    for path in paths:
        _chmod(path, 0o700 if path.is_dir() else 0o600)


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
