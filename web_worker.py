"""Compatibility entrypoint for the workbench worker."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from apps.workbench.web_worker import main

    raise SystemExit(main(sys.argv[1:]))

from apps.workbench import web_worker as _implementation

sys.modules[__name__] = _implementation
