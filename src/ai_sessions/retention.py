"""Compress and restore whole sessions, whatever files their harness keeps them in.

Idle sessions are compressed by a hidden background run.  A session counts as idle when its
transcript has not changed, and ``sessions`` has not opened it, for the configured days.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .capabilities import Unsupported
from .compression import (
    CompressionError,
    compress_file,
    compressed_path,
    decompress_file,
    hides_history,
    is_compressed_path,
    plain_path,
    resolve_storage,
)
from .model import NativeRef, Session
from .paths import APP_CACHE_DIR
from .registry import REGISTRY

DAY_SECONDS = 86_400.0
SWEEP_INTERVAL_SECONDS = 6 * 3_600.0
LOCK_STALE_SECONDS = 12 * 3_600.0
LIVENESS_SECONDS = 15.0
OPENS_FILE = APP_CACHE_DIR / "session-opens.jsonl"
LOCK_FILE = APP_CACHE_DIR / "retention.lock"
LAST_RUN_FILE = APP_CACHE_DIR / "retention.last"
LOG_FILE = APP_CACHE_DIR / "retention.log"


def can_compress(session: Session) -> bool:
    return (
        session.tool in REGISTRY
        and bool(session.storage)
        and not isinstance(REGISTRY.get(session.tool).transcript_files, Unsupported)
    )


def _files(session: Session) -> tuple[Path, ...]:
    """The session's transcript files by their plain names, main file last."""
    if not can_compress(session):
        raise CompressionError(f"{session.tool} sessions cannot be compressed")
    return REGISTRY.get(session.tool).transcript_files(
        NativeRef(session.session_id, session.storage)
    )


def sync_storage(session: Session) -> None:
    """Bring a row up to date with the disk, which another window may have changed."""
    found = resolve_storage(Path(session.storage)) if can_compress(session) else None
    if found is not None:
        session.storage = str(found)
        session.compressed = is_compressed_path(found)


def needs_restore(session: Session) -> bool:
    """Whether any of the session's files is compressed, including a log left behind earlier."""
    return can_compress(session) and any(
        is_compressed_path(resolve_storage(path) or "") or hides_history(path)
        for path in _files(session)
    )


def compress_session(session: Session, *, still_idle: Callable[[], bool] | None = None) -> None:
    """Compress a closed session whole: a failure restores the logs already compressed.

    ``still_idle`` is asked before each file, so that a run stops as soon as it is reopened.
    """
    if session.is_open:
        raise CompressionError(f"{session.title or session.session_id} is open")
    files = _files(session)
    if resolve_storage(files[-1]) is None:
        raise CompressionError(f"the transcript of {session.session_id} is missing")
    done: list[Path] = []
    try:
        for path in files:
            if still_idle is not None and not still_idle():
                raise CompressionError(f"{session.session_id} became active")
            found = resolve_storage(path)
            if found is not None and not is_compressed_path(found):
                compress_file(found)
                done.append(path)
    except CompressionError:
        for path in done:
            try:
                decompress_file(path)
            except CompressionError:
                pass
        raise
    session.storage = str(compressed_path(files[-1]))
    session.compressed = True


def decompress_session(session: Session) -> str:
    """Restore a session, main file first; return a note about any subagent log left behind."""
    files = _files(session)
    main = files[-1]
    if is_compressed_path(resolve_storage(main) or "") or hides_history(main):
        decompress_file(main)
    session.storage = str(plain_path(main))
    session.compressed = False
    note_opened(session)
    kept: list[str] = []
    for path in files[:-1]:
        if is_compressed_path(resolve_storage(path) or "") or hides_history(path):
            try:
                decompress_file(path)
            except CompressionError:
                kept.append(path.name)
    return f"could not decompress {', '.join(kept)}" if kept else ""


def note_opened(session: Session, *, now: float | None = None) -> None:
    """Record that a session was opened; best effort, like the launch log."""
    entry = {"key": session.key, "at": time.time() if now is None else now}
    try:
        OPENS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OPENS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def last_opened() -> dict[str, float]:
    """The latest recorded open of each session."""
    latest: dict[str, float] = {}
    try:
        lines = OPENS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            entry = json.loads(line)
            key, moment = entry["key"], float(entry["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(key, str):
            latest[key] = max(moment, latest.get(key, 0.0))
    return latest


def last_activity(session: Session, opens: dict[str, float]) -> float:
    latest = max(session.updated, opens.get(session.key, 0.0))
    found = resolve_storage(Path(session.storage)) if session.storage else None
    if found is not None:
        try:
            latest = max(latest, found.stat().st_mtime)
        except OSError:
            pass
    return latest


def _families(sessions: Iterable[Session]) -> list[list[Session]]:
    """Group sessions with their subagents, which are idle or active together."""
    items = list(sessions)
    by_id = {(item.tool, item.session_id): item for item in items}
    families: dict[str, list[Session]] = {}
    for item in items:
        top = item
        seen = {item.key}
        while True:
            parent = by_id.get((top.tool, top.parent_id))
            if parent is None or parent.key in seen:
                break
            seen.add(parent.key)
            top = parent
        families.setdefault(top.key, []).append(item)
    return list(families.values())


def _family_is_idle(family: list[Session], cutoff: float, opens: dict[str, float]) -> bool:
    return all(not item.is_open and last_activity(item, opens) <= cutoff for item in family)


def _to_compress(family: list[Session]) -> list[Session]:
    return [item for item in family if can_compress(item) and not item.compressed]


def idle_sessions(
    sessions: Iterable[Session], days: int, *, now: float | None = None
) -> list[Session]:
    """Sessions that qualify, hidden ones and every origin included."""
    cutoff = (time.time() if now is None else now) - days * DAY_SECONDS
    opens = last_opened()
    return [
        item
        for family in _families(sessions)
        if _family_is_idle(family, cutoff, opens)
        for item in _to_compress(family)
    ]


@dataclass
class SweepResult:
    compressed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    failures: list[str] = field(default_factory=list)


def _size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def _log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def run_sweep(
    sessions: Iterable[Session],
    days: int,
    *,
    dry_run: bool = False,
    now: float | None = None,
    refresh: Callable[[list[Session]], bool] | None = None,
) -> SweepResult:
    """Compress idle sessions, checking each again just before touching it.

    ``refresh`` updates which sessions are open and returns False when that cannot be told;
    the run then stops, because an unknown session must count as open.
    """
    moment = time.time() if now is None else now
    cutoff = moment - days * DAY_SECONDS
    result = SweepResult()
    items = list(sessions)
    checked = float("-inf")

    def still_known() -> bool:
        nonlocal checked
        if refresh is None or dry_run or time.monotonic() - checked < LIVENESS_SECONDS:
            return True
        checked = time.monotonic()
        if refresh(items):
            return True
        message = "could not tell which sessions are open, so the run stopped"
        result.failures.append(message)
        _log(message)
        return False

    if not still_known():
        return result
    opens = last_opened()
    for family in _families(items):
        if not _family_is_idle(family, cutoff, opens):
            continue
        if not still_known():
            return result
        for item in _to_compress(family):
            before = _size(resolve_storage(Path(item.storage)))
            if dry_run:
                result.compressed += 1
                result.bytes_before += before
                continue
            if not _family_is_idle(family, cutoff, last_opened()):
                break
            try:
                compress_session(
                    item,
                    still_idle=lambda family=family: (
                        still_known() and _family_is_idle(family, cutoff, last_opened())
                    ),
                )
            except CompressionError as error:
                result.failures.append(f"{item.session_id}: {error}")
                _log(f"skipped {item.tool} {item.session_id}: {error}")
                continue
            result.compressed += 1
            result.bytes_before += before
            result.bytes_after += _size(Path(item.storage))
            _log(f"compressed {item.tool} {item.session_id}")
    return result


def _lock_is_stale() -> bool:
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
        owner = int(LOCK_FILE.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    return age > LOCK_STALE_SECONDS or not psutil.pid_exists(owner)


def _create_lock() -> bool:
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return True


def acquire_lock() -> bool:
    """Become the only sweep; a lock left by a dead process does not count."""
    if _create_lock():
        return True
    if _lock_is_stale():
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            return False
        return _create_lock()
    return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _sweep_due(now: float) -> bool:
    if LOCK_FILE.exists() and not _lock_is_stale():
        return False
    try:
        return now - float(LAST_RUN_FILE.read_text(encoding="utf-8")) >= SWEEP_INTERVAL_SECONDS
    except (OSError, ValueError):
        return True


def start_background_sweep(*, now: float | None = None) -> bool:
    """Start a hidden sweep when one is due; never let that disturb the caller."""
    moment = time.time() if now is None else now
    if not _sweep_due(moment):
        return False
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(str(moment), encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                [sys.executable, "-m", "ai_sessions", "--compress-idle"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **options,
            )
    except OSError:
        return False
    return True
