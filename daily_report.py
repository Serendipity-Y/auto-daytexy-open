"""Compatibility entrypoint for the report application."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from apps.reports.daily_report import main

    raise SystemExit(main(sys.argv[1:]))

from apps.reports import daily_report as _implementation

sys.modules[__name__] = _implementation
