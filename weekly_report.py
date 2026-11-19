"""Compatibility entrypoint for the weekly-report application."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from apps.reports.weekly_report import main

    raise SystemExit(main(sys.argv[1:]))

from apps.reports import weekly_report as _implementation

sys.modules[__name__] = _implementation
