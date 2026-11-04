"""Removing what the utility left behind in its own folders.

Only files this utility writes are touched: temporary files from saves that did not
finish, and caches that an older version wrote and no current version reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# ``name.tmp-<pid>`` and ``name.tmp-<pid>-<thread>``, as every save here names them.
_TEMPORARY = re.compile(r"\.tmp-(\d+)(?:-\d+)?$")

# Caches written by earlier versions under names no version now reads.
OBSOLETE_CACHES = (
    "claude-discovery-v5.json",
    "claude-discovery-v6.json",
    "claude-metadata-v4.json",
    "codex-discovery-v5.json",
    "codex-discovery-v6.json",
    "codex-discovery-v7.json",
    "codex-message-counts-v1.json",
)


def _alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.pid_exists(pid)
    except Exception:
        return True  # Unsure, so it may still be writing.


def sweep_temporary_files(folders: Iterable[Path]) -> list[Path]:
    """Delete temporary files whose writing process has exited; return what went.

    A live process may still be about to rename its file into place, so its files
    stay.  A reused process id only means a stray file stays a little longer.
    """
    removed: list[Path] = []
    for folder in folders:
        try:
            candidates = list(folder.iterdir())
        except OSError:
            continue
        for path in candidates:
            match = _TEMPORARY.search(path.name)
            if match is None or _alive(int(match.group(1))):
                continue
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(path)
            except OSError:
                continue
    return removed


def remove_obsolete_caches(folder: Path) -> list[Path]:
    """Delete caches from earlier versions.

    The previous catalog stays: an older ``sessions`` still running uses it.
    """
    removed: list[Path] = []
    for name in OBSOLETE_CACHES:
        path = folder / name
        try:
            if path.is_file():
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed
