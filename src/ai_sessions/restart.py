"""The ``sesres`` shorthand for ``sessions --restart``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .app import main as sessions_main

    return sessions_main(["--restart", *(sys.argv[1:] if argv is None else argv)])
