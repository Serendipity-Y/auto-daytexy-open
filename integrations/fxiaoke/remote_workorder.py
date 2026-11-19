"""Compatibility adapter for the remote-workorder CRM integration."""

from __future__ import annotations

import fxiaoke_remote_workorder as _legacy


def __getattr__(name: str):
    """Resolve attributes late so existing patches keep working during migration."""
    return getattr(_legacy, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_legacy)))
