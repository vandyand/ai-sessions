"""Harnesses whose `sessions` launcher is gone, and which session each tab ran.

On Windows `sessions` stays alive while the harness runs (there is no exec),
so killing the launcher used to leave the harness reading a terminal whose
shell had its prompt back: both garble each other's input and neither is
usable.  Such a harness is "detached".  It cannot be moved to another
terminal, but its conversation is already on disk, so stopping it and
resuming the session loses only a turn still in flight -- which is why
nothing here stops a harness that is still working without asking.

Each tab also remembers the session `sessions` last launched in it (keyed by
Windows Terminal's per-tab ``WT_SESSION``), so ``sessions --reattach`` can
bring back whatever a tab lost without searching for it, and ``sessions
--reopen`` can resume what it ran after a normal exit.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psutil

from .capabilities import Unsupported
from .model import Session
from .paths import APP_CACHE_DIR
from .registry import REGISTRY

TERMINALS_FILE = APP_CACHE_DIR / "terminals.json"
TERMINAL_MEMORY_SECONDS = 7 * 86_400
# A harness that wrote nothing for this long is waiting, whatever its last
# record says, unless the harness itself reports that it is busy.
QUIET_AFTER_SECONDS = 15 * 60


def terminal_id() -> str:
    """This tab's identity, or "" outside Windows Terminal."""
    return os.environ.get("WT_SESSION", "").strip()


def _read(path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    cutoff = time.time() - TERMINAL_MEMORY_SECONDS
    return {
        str(key): value
        for key, value in document.items()
        if isinstance(value, dict) and float(value.get("at", 0) or 0) >= cutoff
    }


def _write(path: Path, document: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=1), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def remember(
    session: Session,
    tool: str,
    pid: int,
    started: float,
    terminal: str | None = None,
    *,
    path: Path = TERMINALS_FILE,
    restart_error: str = "",
    new_launcher: bool = False,
) -> None:
    """Note that this tab now runs ``session``; a session lives in one tab at a time."""
    from .workspaces.launch import remember_launch

    remember_launch(session, tool)
    terminal = terminal_id() if terminal is None else terminal
    native_id = session.launch_target(tool) or session.session_id
    home = str(REGISTRY.get(tool).home) if tool in REGISTRY else ""
    prior = _read(path)
    pending = {}
    for entry in prior.values():
        if (
            entry.get("pid") == pid
            and entry.get("started") == started
            and entry.get("tool") == tool
            and entry.get("session_id") == native_id
        ):
            pending = {key: entry[key] for key in ("restart", "restart_tool") if key in entry}
            break
    document = {
        key: value
        for key, value in prior.items()
        if value.get("session_id") != native_id
        or value.get("tool") != tool
        or (new_launcher and value.get("pid") != pid and _live_remembered(value))
    }
    document[terminal or f"pid:{pid}"] = {
        "session_id": native_id,
        "home": home,
        "tool": tool,
        "title": session.title,
        "cwd": session.cwd,
        "pid": pid,
        "started": started,
        "at": time.time(),
        **pending,
        **({"new_launcher": True} if new_launcher else {}),
        **({"restart_error": restart_error} if restart_error else {}),
    }
    _write(path, document)


def _live_remembered(entry: dict) -> bool:
    if entry.get("closed"):
        return False
    try:
        process = psutil.Process(int(entry["pid"]))
        return process.is_running() and process.create_time() == float(entry["started"])
    except (KeyError, ValueError, TypeError, psutil.Error):
        return False


def mark_closed(pid: int, *, path: Path = TERMINALS_FILE) -> str | None:
    """The harness ``pid`` exited normally: nothing was lost, but it can be reopened.

    The entry is kept so ``sessions --reopen`` knows what the tab last ran,
    and marked so ``--reattach`` does not mistake a deliberate exit for a loss.
    Returns None unless ``sessions --restart`` asked for it to be reopened;
    then the harness to reopen it in, or "" for the one it ran in.
    """
    document = _read(path)
    restart: str | None = None
    for entry in document.values():
        if entry.get("pid") == pid and not entry.get("closed"):
            if entry.pop("restart", False):
                restart = str(entry.get("restart_tool", "") or "")
            entry.pop("restart_tool", None)
            entry["closed"] = True
            entry["at"] = time.time()
    if any(entry.get("pid") == pid for entry in document.values()):
        _write(path, document)
    return restart


def running_here(*, path: Path = TERMINALS_FILE) -> tuple[str, dict[str, Any]] | None:
    """The remembered launch this process runs inside, found through its parents."""
    try:
        me = psutil.Process()
        ancestors = {process.pid: process for process in me.parents()}
    except psutil.Error:
        return None
    document = _read(path)
    for key, entry in document.items():
        pid = int(entry.get("pid", 0) or 0)
        if entry.get("closed") or pid not in ancestors:
            continue
        try:
            if abs(ancestors[pid].create_time() - float(entry.get("started", 0) or 0)) <= 1:
                return key, entry
        except psutil.Error:
            continue
    # Shared-server tools are children of the daemon, not of their terminal UI.
    # A native per-thread identity can identify the remembered client, but only
    # from a verified harness daemon and with a matching, live PID generation.
    from .registry import REGISTRY

    matches = []
    for adapter in REGISTRY.adapters():
        hooks = adapter.messaging
        key = hooks.sender_environment if hooks else ""
        session_id = os.environ.get(key) if key else ""
        if not session_id:
            continue
        try:
            verified_daemon = any(
                process.name().casefold() in adapter.liveness_executables
                and _shared_daemon(process)
                for process in ancestors.values()
            )
        except psutil.Error:
            continue
        if not verified_daemon:
            continue
        for terminal, entry in document.items():
            if (
                entry.get("closed")
                or entry.get("tool") != adapter.name
                or entry.get("session_id") != session_id
            ):
                continue
            try:
                process = psutil.Process(int(entry.get("pid", 0) or 0))
                # Inspection failure must refuse rather than classify a daemon as a client.
                command = process.cmdline()
                if (
                    process.is_running()
                    and command
                    and not any(arg.casefold() == "app-server" for arg in command[1:])
                    and abs(process.create_time() - float(entry.get("started", 0) or 0)) < 0.01
                ):
                    matches.append((terminal, entry))
            except (psutil.Error, TypeError, ValueError):
                continue
    return matches[0] if len(matches) == 1 else None


def request_restart(key: str, tool: str = "", *, path: Path = TERMINALS_FILE) -> None:
    """Ask the launcher of the tab ``key`` to reopen its session once the harness exits.

    ``tool`` names another harness to carry the conversation into; "" keeps it
    in the one it ran in.
    """
    document = _read(path)
    if key in document:
        document[key]["restart"] = True
        if tool:
            document[key]["restart_tool"] = tool
        else:
            document[key].pop("restart_tool", None)
        _write(path, document)


def _shared_daemon(process: psutil.Process) -> bool:
    # A Codex window can start the app-server other windows depend on.
    try:
        return any(argument.casefold() == "app-server" for argument in process.cmdline()[1:])
    except psutil.Error:
        return False


def stop_for_restart(pid: int, started: float, harness_names: Iterable[str]) -> str:
    """Stop the harness this process runs inside, sparing itself and any shared daemon.

    Everything ``pid`` started goes, except this process, the shell between it
    and the harness, and a shared daemon with what it started.  Returns an
    empty string on success, otherwise why the harness is still running.
    """
    names = {name.casefold() for name in harness_names}
    try:
        root = psutil.Process(pid)
        if root.create_time() != started:
            return f"PID {pid} is now a different process; nothing was stopped."
        me = psutil.Process()
        keep = {me.pid}
        for parent in me.parents():
            if parent.pid == pid or parent.name().casefold() in names:
                break
            keep.add(parent.pid)
        spared = set(keep)
        for process in root.children(recursive=True):
            if _shared_daemon(process):
                spared |= {process.pid, *(child.pid for child in process.children(recursive=True))}
        tree = [
            process
            for process in [*root.children(recursive=True), root]
            if process.pid not in spared
        ]
    except psutil.NoSuchProcess:
        return ""
    except (psutil.Error, ValueError) as error:
        return f"Could not inspect PID {pid}: {error}"
    for process in tree:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as error:
            return f"Could not stop PID {process.pid}: {error}"
    _, alive = psutil.wait_procs(tree, timeout=5.0)
    if any(process.pid == pid for process in alive):
        return f"PID {pid} did not exit within 5 seconds."
    return ""


def remembered(*, path: Path = TERMINALS_FILE) -> dict[str, dict[str, Any]]:
    return _read(path)


def closed(*, path: Path = TERMINALS_FILE) -> dict[str, dict[str, Any]]:
    """Remembered launches whose harness exited normally, newest first."""
    entries = [(key, entry) for key, entry in _read(path).items() if entry.get("closed")]
    entries.sort(key=lambda item: -float(item[1].get("at", 0) or 0))
    return dict(entries)


def interrupted(*, path: Path = TERMINALS_FILE) -> dict[str, dict[str, Any]]:
    """Remembered launches whose harness is gone or detached, but did not exit normally."""
    from .platforms.windows import is_detached

    result = {}
    for key, entry in _read(path).items():
        if entry.get("closed"):
            continue
        pid = int(entry.get("pid", 0) or 0)
        if not _same_process(pid, float(entry.get("started", 0) or 0)) or is_detached(pid):
            result[key] = entry
    return result


def _same_process(pid: int, started: float) -> bool:
    try:
        return pid > 0 and abs(psutil.Process(pid).create_time() - started) <= 1
    except (psutil.Error, ValueError):
        return False


def _turn_state(item: Session) -> str:
    """What the harness running ``item`` says it is doing, or "" when unknown."""
    try:
        adapter = REGISTRY.get(item.tool)
        command = tuple(psutil.Process(item.open_pid).cmdline())
    except (KeyError, psutil.Error, ValueError):
        return ""
    hook = adapter.turn_state
    if isinstance(hook, Unsupported):
        return ""
    try:
        return hook(pid=item.open_pid, home=adapter.home, storage=item.storage, command=command)
    except Exception:
        return ""


def is_busy(item: Session) -> bool:
    """Whether the harness running ``item`` is in the middle of a turn."""
    state = _turn_state(item)
    if state in ("working", "idle"):
        return state == "working"
    try:
        return time.time() - Path(item.storage).stat().st_mtime < QUIET_AFTER_SECONDS
    except (OSError, ValueError):
        return False


def detached_sessions(sessions: Iterable[Session]) -> list[Session]:
    """Open interactive sessions whose harness lost its launcher."""
    from .platforms.windows import is_detached

    return [
        item
        for item in sessions
        if item.is_open
        and item.open_pid
        and is_detached(item.open_pid)
        and _turn_state(item) != "headless"
    ]


def stop_detached(sessions: Iterable[Session]) -> tuple[list[Session], list[Session], list[str]]:
    """Stop every detached harness that is between turns.

    Returns what was stopped, what is still working (left alone until it
    finishes), and any process that would not stop.
    """
    from .platforms.windows import process_started, stop_process_tree

    stopped: list[Session] = []
    working: list[Session] = []
    problems: list[str] = []
    for item in detached_sessions(sessions):
        if is_busy(item):
            working.append(item)
            continue
        started = process_started(item.open_pid)
        problem = stop_process_tree(item.open_pid, started) if started else ""
        if problem:
            problems.append(problem)
            continue
        stopped.append(item)
        item.is_open = False
        item.open_pid = 0
    return stopped, working, problems


def cleanup_note(stopped: list[Session], working: list[Session], problems: list[str]) -> str:
    parts = []
    if stopped:
        names = ", ".join(item.title or item.session_id[:8] for item in stopped)
        parts.append(
            f"Stopped {len(stopped)} session{'s' if len(stopped) != 1 else ''} that had lost "
            f"their terminal ({names}); resume them from here."
        )
    if working:
        parts.append(
            f"{len(working)} detached session{'s are' if len(working) != 1 else ' is'} still "
            "working and will be stopped once idle."
        )
    parts.extend(problems)
    return " ".join(parts)
