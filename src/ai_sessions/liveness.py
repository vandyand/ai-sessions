"""Provider-neutral process and file-lock snapshots for one discovery pass."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import psutil

WINDOWS_EPOCH_FILETIME = 116_444_736_000_000_000


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    name: str
    command: tuple[str, ...]
    start_token: str
    started_at: float = 0.0


@dataclass(frozen=True, slots=True)
class LivenessContext:
    """Read-only adapter view of one pass's point-in-time host evidence."""

    platform: str
    process_snapshot: tuple[ProcessInfo, ...]
    lock_map: Mapping[tuple[int, int, int], int]


def process_start_token(pid: int) -> str:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = value[value.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return ""


def _windows_start_token(value: Any) -> str:
    try:
        return str(int(float(value) * 10_000_000) + WINDOWS_EPOCH_FILETIME)
    except (TypeError, ValueError, OverflowError):
        return ""


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _needs_start_time(
    name: str,
    command: tuple[str, ...],
    executables: frozenset[str] | None,
) -> bool:
    if executables is None:
        return True
    if _basename(name) in executables:
        return True
    return any(_basename(value.strip(" \t\"'")) in executables for value in command)


def process_snapshot(
    platform: str | None = None,
    *,
    start_time_executables: frozenset[str] | None = None,
) -> tuple[ProcessInfo, ...]:
    """Enumerate live processes once without retaining provider-specific state."""
    current_platform = platform or sys.platform
    result: list[ProcessInfo] = []
    attributes = ["pid", "name", "cmdline"]
    if start_time_executables is None:
        attributes.append("create_time")
    try:
        processes = iter(psutil.process_iter(attributes))
    except Exception:
        return ()
    iterator_failures = 0
    while True:
        try:
            process = next(processes)
        except StopIteration:
            break
        except Exception:
            # Some psutil backends resolve explicit attributes inside the
            # iterator and can fail before a Process object reaches the body.
            # Retain the point-in-time evidence already collected.
            iterator_failures += 1
            if iterator_failures >= 16:
                break
            continue
        iterator_failures = 0
        try:
            pid = int(process.info["pid"])
            name = str(process.info.get("name") or "")
            command = tuple(str(value) for value in (process.info.get("cmdline") or ()))
            started_at = 0.0
            start = ""
            if _needs_start_time(name, command, start_time_executables):
                raw_started = process.info.get("create_time")
                if raw_started is None and start_time_executables is not None:
                    raw_started = process.create_time()
                started_at = float(raw_started or 0)
                if current_platform == "win32":
                    start = _windows_start_token(started_at) if started_at > 0 else ""
                else:
                    start = process_start_token(pid)
            result.append(ProcessInfo(pid, name, command, start, started_at))
        except (AttributeError, psutil.Error, KeyError, TypeError, ValueError, OverflowError):
            continue
    return tuple(result)


def held_file_locks(platform: str | None = None) -> dict[tuple[int, int, int], int]:
    """Map Linux locked-file device/inode triples to their owning PID."""
    if (platform or sys.platform) == "win32":
        return {}
    result: dict[tuple[int, int, int], int] = {}
    try:
        lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major_text, minor_text, inode_text = fields[5].split(":", 2)
            result[(int(major_text, 16), int(minor_text, 16), int(inode_text))] = int(fields[4])
        except (ValueError, IndexError):
            continue
    return result


def populate_context(context: Any) -> None:
    """Populate a HarnessContext exactly once, including when both snapshots are empty."""
    if context.liveness_ready:
        return
    context.process_snapshot = process_snapshot(
        context.platform,
        start_time_executables=context.liveness_executables,
    )
    context.lock_map = held_file_locks(context.platform)
    context.liveness_ready = True


def immutable_context(context: Any) -> LivenessContext:
    """Freeze a populated per-pass context before it crosses adapter boundaries."""
    return LivenessContext(
        context.platform,
        tuple(context.process_snapshot),
        MappingProxyType(dict(context.lock_map)),
    )
