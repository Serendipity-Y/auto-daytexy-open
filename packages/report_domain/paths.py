from __future__ import annotations

import hashlib
import os
from pathlib import Path


DEFAULT_MACOS_REPORT_PATH = Path.home() / "Documents" / "auto-daytexy" / "日报.md"
DEFAULT_WINDOWS_REPORT_PATH = DEFAULT_MACOS_REPORT_PATH


def get_default_report_path(*, environ=None, platform_name=None) -> Path:
    environment = os.environ if environ is None else environ
    for name in ("DAILY_REPORT_PATH", "DAILY_REPORT_MD_FILE"):
        value = str(environment.get(name, "") or "").strip()
        if value:
            return Path(os.path.expandvars(os.path.expanduser(value)))
    if (os.name if platform_name is None else platform_name) == "nt":
        return DEFAULT_WINDOWS_REPORT_PATH
    return DEFAULT_MACOS_REPORT_PATH


def resolve_report_path(path_value=None, *, environ=None, platform_name=None) -> Path:
    value = str(path_value or "").strip()
    if value:
        return Path(os.path.expandvars(os.path.expanduser(value)))
    return get_default_report_path(environ=environ, platform_name=platform_name)


def resolve_fxiaoke_trace_root(artifact_root, private_root=None) -> Path:
    """Map report artifacts to local-only diagnostics without exposing report paths."""
    root = Path(private_root).expanduser() if private_root is not None else Path("~/.config/auto-daytexy").expanduser()
    root = root.absolute()
    if any("nutstore" in part.casefold() or "坚果云" in part for part in root.parts + root.resolve().parts):
        raise ValueError("原始 CRM 轨迹禁止保存在同步目录。")
    digest = hashlib.sha256(str(Path(artifact_root).expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return root / "fxiaoke_failure_traces" / digest
