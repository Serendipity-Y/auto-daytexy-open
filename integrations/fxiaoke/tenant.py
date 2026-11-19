"""Explicit local schema and owner settings for a CRM tenant."""

import json
import os
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_tenant_config(path, required):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if not required:
            return {}
        raise RuntimeError("无法读取 FXIAOKE_CRM_CONFIG_PATH 指定的租户配置。") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("纷享销客租户配置不是可读取的 UTF-8 JSON 文件。") from exc
    if not isinstance(value, dict):
        raise RuntimeError("纷享销客租户配置必须是 JSON 对象。")
    return value


def tenant_value(section, key, *, required=False, token=False):
    configured = os.environ.get("FXIAOKE_CRM_CONFIG_PATH", "").strip()
    default_path = Path.home() / ".config" / "auto-daytexy" / "fxiaoke_tenant.json"
    data = _load_tenant_config(Path(configured).expanduser() if configured else default_path, bool(configured))
    settings = data.get(section, {})
    if not isinstance(settings, dict):
        raise RuntimeError(f"纷享销客租户配置项 {section} 必须是 JSON 对象。")
    value = settings.get(key, "")
    if not isinstance(value, str):
        raise RuntimeError(f"纷享销客租户配置项 {section}.{key} 必须是文本。")
    value = value.strip()
    if value and token and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise RuntimeError(f"纷享销客租户配置项 {section}.{key} 含无效标识符。")
    if required and not value:
        raise RuntimeError(f"请在纷享销客租户配置中设置 {section}.{key}（FXIAOKE_CRM_CONFIG_PATH）。")
    return value


def require_service_owner():
    owner = os.environ.get("FXIAOKE_SERVICE_OWNER", "").strip()
    if not owner:
        raise RuntimeError("请设置 FXIAOKE_SERVICE_OWNER 为当前账号在纷享销客显示的完整姓名。")
    return owner
