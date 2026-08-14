"""Native Windows process/session integration.

Windows Terminal does not expose a stable session-ID-to-tab API, so this
adapter detects open sessions but intentionally does not attempt exact tab
focus.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import psutil

WINDOWS_EPOCH_FILETIME = 116_444_736_000_000_000


def process_exists(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except (psutil.Error, ValueError):
        return False


def process_start_matches(pid: int, expected: str) -> bool:
    """Compare Claude's Windows FILETIME process token with process creation."""
    try:
        expected_value = int(expected)
        actual = int(psutil.Process(pid).create_time() * 10_000_000) + WINDOWS_EPOCH_FILETIME
        return abs(actual - expected_value) < 10_000_000
    except (psutil.Error, ValueError, OverflowError):
        return False


def detect_open_sessions(sessions: Iterable[Any], claude_home: Path, codex_home: Path) -> None:
    by_identity = {(item.tool, item.session_id): item for item in sessions}
    _detect_claude(by_identity, claude_home)
    _detect_codex(by_identity, codex_home)


def _detect_claude(by_identity: dict[tuple[str, str], Any], claude_home: Path) -> None:
    for path in (claude_home / "sessions").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pid = int(record.get("pid", 0))
            sid = str(record.get("sessionId", ""))
            expected_start = str(record.get("procStart", ""))
            kind = str(record.get("kind", ""))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            continue
        if not pid or not sid or (kind and kind != "interactive"):
            continue
        if expected_start and not process_start_matches(pid, expected_start):
            continue
        if not expected_start and not process_exists(pid):
            continue
        item = by_identity.get(("claude", sid))
        if item and item.source == "interactive":
            item.is_open = True
            item.open_pid = pid


def _detect_codex(by_identity: dict[tuple[str, str], Any], codex_home: Path) -> None:
    live_pids: list[int] = []
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        try:
            name = (process.info.get("name") or "").casefold()
            command = " ".join(process.info.get("cmdline") or []).casefold()
            if name in ("codex.exe", "codex") or "codex.exe" in command:
                live_pids.append(int(process.info["pid"]))
        except (psutil.Error, TypeError, ValueError):
            continue
    if not live_pids:
        return

    log_databases = list(codex_home.glob("logs_*.sqlite"))
    if not log_databases:
        return
    logs_db = max(log_databases, key=_numbered_database_key)
    parent_by_child = _spawn_parents(codex_home)
    try:
        connection = sqlite3.connect(f"file:{logs_db}?mode=ro", uri=True, timeout=2)
        for pid in live_pids:
            row = connection.execute(
                """
                SELECT thread_id FROM logs
                WHERE process_uuid LIKE ? AND thread_id IS NOT NULL AND thread_id != ''
                ORDER BY ts DESC, ts_nanos DESC LIMIT 1
                """,
                (f"pid:{pid}:%",),
            ).fetchone()
            if not row:
                continue
            sid = str(row[0])
            seen: set[str] = set()
            while sid in parent_by_child and sid not in seen:
                seen.add(sid)
                sid = parent_by_child[sid]
            item = by_identity.get(("codex", sid))
            if item and item.source == "interactive":
                item.is_open = True
                item.open_pid = pid
        connection.close()
    except (sqlite3.Error, OSError):
        return


def _spawn_parents(codex_home: Path) -> dict[str, str]:
    databases = list(codex_home.glob("state_*.sqlite"))
    if not databases:
        return {}
    database = max(databases, key=_numbered_database_key)
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        rows = connection.execute(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
        )
        result = {str(child): str(parent) for parent, child in rows}
        connection.close()
        return result
    except (sqlite3.Error, OSError):
        return {}


def _numbered_database_key(path: Path) -> tuple[int, float]:
    stem = path.stem.rsplit("_", 1)
    try:
        number = int(stem[-1])
    except ValueError:
        number = 0
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return number, modified
