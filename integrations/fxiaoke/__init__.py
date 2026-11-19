"""Fxiaoke adapters used by application layers."""

import os


def crm_enabled() -> bool:
    """Return whether the report and workbench entry points may access CRM."""
    return os.getenv("FXIAOKE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
