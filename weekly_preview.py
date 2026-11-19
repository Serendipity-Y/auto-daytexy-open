"""Compatibility import for the weekly-preview report module."""

from __future__ import annotations

import sys

from apps.reports import weekly_preview as _implementation

sys.modules[__name__] = _implementation
