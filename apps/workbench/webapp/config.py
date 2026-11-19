from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from packages.report_domain.paths import resolve_report_path


CRM_DISABLED_MESSAGE = "纷享销客集成未启用。设置 FXIAOKE_ENABLED=1 并重启工作中台。"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PRIVATE_APP_DIR = Path("~/.config/auto-daytexy").expanduser()
DEFAULT_DATABASE_PATH = PRIVATE_APP_DIR / "web_manager.sqlite3"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class WebConfig:
    project_root: Path
    report_path: Path
    database_path: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    testing: bool = False
    knowledge_base_quarterly_target: int | None = None


def validate_bind_host(host: str) -> str:
    normalized_host = str(host or "").strip()
    if normalized_host != DEFAULT_HOST:
        raise ValueError("Web 管理端只允许绑定 127.0.0.1")
    return normalized_host


def load_web_config(project_root=None, environ=None) -> WebConfig:
    env = os.environ if environ is None else environ
    root = Path(project_root or PROJECT_ROOT).expanduser()
    report_path_value = str(env.get("WEB_MANAGER_REPORT_PATH", "")).strip()
    database_path_value = str(env.get("WEB_MANAGER_DATABASE_PATH", "")).strip()
    port_value = str(env.get("WEB_MANAGER_PORT", DEFAULT_PORT)).strip()
    testing_value = str(env.get("WEB_MANAGER_TESTING", "")).strip().lower()
    quarterly_target = str(env.get("WEB_MANAGER_KNOWLEDGE_BASE_QUARTERLY_TARGET", "")).strip()
    try:
        knowledge_base_quarterly_target = int(quarterly_target) if quarterly_target else None
    except ValueError as exc:
        raise ValueError("知识库季度目标必须为正整数。") from exc
    if knowledge_base_quarterly_target is not None and knowledge_base_quarterly_target <= 0:
        raise ValueError("知识库季度目标必须为正整数。")

    report_path = (
        Path(os.path.expandvars(os.path.expanduser(report_path_value)))
        if report_path_value
        else resolve_report_path()
    )
    database_path = (
        Path(os.path.expandvars(os.path.expanduser(database_path_value)))
        if database_path_value
        else DEFAULT_DATABASE_PATH
    )
    host = validate_bind_host(env.get("WEB_MANAGER_HOST", DEFAULT_HOST))
    port = int(port_value)
    testing = testing_value in {"1", "true", "yes", "on"}

    return WebConfig(
        project_root=root,
        report_path=report_path,
        database_path=database_path,
        host=host,
        port=port,
        testing=testing,
        knowledge_base_quarterly_target=knowledge_base_quarterly_target,
    )
