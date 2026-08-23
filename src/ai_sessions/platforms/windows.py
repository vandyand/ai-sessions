"""Provider-neutral Windows process helpers used by terminal focus."""

from __future__ import annotations

import psutil


def process_exists(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except (psutil.Error, ValueError):
        return False
