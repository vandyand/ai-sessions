"""Current-session handle lookup; never launches or stops a native harness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .capabilities import Unsupported
from .registry import REGISTRY


def catalog():
    from .app import UserState, load_session_catalog

    state = UserState()
    return load_session_catalog(state) or [], state


def _managed_native_session():
    from .detached import running_here

    found = running_here()
    # The remembered launcher can be cmd.exe rather than the native harness.
    # Prefer the nearest actual harness's generation-checked identity, including
    # native sessions started outside a managed launcher.
    import psutil

    try:
        for process in psutil.Process().parents():
            name = process.name().casefold()
            for adapter in REGISTRY.adapters():
                if name in adapter.liveness_executables and not isinstance(
                    adapter.current_session, Unsupported
                ):
                    ref = adapter.current_session(process.pid, process.create_time())
                    if ref is not None:
                        return adapter.name, ref
                    return adapter.name, None
    except psutil.Error:
        pass
    if found is not None:
        _, entry = found
        adapter = REGISTRY.get(str(entry.get("tool", "")))
        if not isinstance(adapter.current_session, Unsupported):
            ref = adapter.current_session(int(entry["pid"]), float(entry["started"]))
            if ref is not None:
                return adapter.name, ref
        # A remembered launch is harness evidence, not proof of the current
        # thread after an in-process session switch.
        return adapter.name, None
    return None


def managed_session():
    found = _managed_native_session()
    return (found[0], found[1].session_id if found[1] else "") if found else None


def register_current(tool, ident, items, state):
    """Reread one process-verified native file; never pick a copy by title or age."""
    from .app import load_session_catalog, save_session_catalog, session_from_native
    from .discovery import HarnessContext

    found = _managed_native_session()
    if not found or found[0] != tool or found[1] is None or found[1].session_id != ident:
        return []
    ref = found[1]
    adapter = REGISTRY.get(tool)
    if isinstance(adapter.rediscover, Unsupported):
        return []
    context = HarnessContext.create(use_cache=False)
    rows = [
        row
        for row in adapter.rediscover(context, [ref])
        if row.tool == tool and row.session_id == ident
    ]
    if len(rows) > 1:
        raise ValueError("More than one native home matches this identity; registration refused.")
    if not rows or Path(rows[0].storage).resolve() != Path(ref.storage).resolve():
        return []
    item = session_from_native(rows[0])
    # Recheck the catalog before merging; never discard other cached harnesses.
    latest = load_session_catalog()
    merged = {(row.tool, row.session_id, row.storage): row for row in [*items, *latest]}
    conflicts = [row for row in merged.values() if row.tool == tool and row.session_id == ident]
    if any(Path(row.storage).resolve() != Path(ref.storage).resolve() for row in conflicts):
        raise ValueError("More than one native home matches this identity; registration refused.")
    state.apply([item], check_copies=False)
    merged[(item.tool, item.session_id, item.storage)] = item
    save_session_catalog(merged.values())
    # The menu's cache writer is best-effort; whoami must not claim registration
    # succeeded if the next assign/open command still cannot find this session.
    persisted = [
        row for row in load_session_catalog() if row.tool == tool and row.session_id == ident
    ]
    if len(persisted) != 1 or Path(persisted[0].storage).resolve() != Path(ref.storage).resolve():
        raise RuntimeError("Could not persist this session's catalog registration; retry whoami.")
    return [item]


def terminal_session():
    """The session this Windows Terminal window last recorded, if any."""
    from .detached import remembered, terminal_id

    terminal = terminal_id()
    if not terminal:
        return None
    return terminal, remembered().get(terminal)


def current_identity():
    candidates = set()
    for adapter in REGISTRY.adapters():
        key = adapter.messaging.sender_environment if adapter.messaging else ""
        ident = os.environ.get(key, "") if key else ""
        if ident:
            candidates.add((adapter.name, ident))
    managed = managed_session()
    if managed is not None:
        # Switching harnesses can leave the previous harness's environment in
        # the process tree. A verified current harness takes precedence. Its
        # native per-turn ID can in turn supersede its remembered launch ID.
        same_harness = [entry for entry in candidates if entry[0] == managed[0]]
        if len(same_harness) == 1:
            return same_harness[0]
        if managed[1]:
            return managed
    elif len(candidates) == 1:
        return next(iter(candidates))
    raise ValueError(
        'Run ! ses whoami inside an agent session; from a separate shell, use ses -q "session title".'
    )


def _print_terminal(terminal: str, entry: dict, *, json_output: bool) -> None:
    row = {
        "terminal": terminal,
        "state": "closed" if entry.get("closed") else "running",
        "tool": entry.get("tool") or "",
        "session_id": entry.get("session_id") or "",
        "title": entry.get("title") or "",
        "cwd": entry.get("cwd") or "",
        "pid": entry.get("pid") or 0,
    }
    if json_output:
        print(json.dumps(row, ensure_ascii=False))
        return
    state = (
        "closed; sessions --reopen opens it here" if entry.get("closed") else "recorded as running"
    )
    print(
        f"Terminal: {terminal}\n"
        f"State: {state}\n"
        f"Title: {row['title']}\n"
        f"Harness: {row['tool']}\n"
        f"Session: {row['session_id']}\n"
        f"Directory: {row['cwd']}"
    )


def identify(argv=None) -> int:
    """Show the session this terminal last recorded. Does not scan the catalog."""
    parser = argparse.ArgumentParser(
        prog="sessions identify",
        description="Show the session recorded for this terminal, if any.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    recorded = terminal_session()
    if recorded is None:
        print(
            "sessions: this shell is not in Windows Terminal, so it has no tab record.",
            file=sys.stderr,
        )
        return 2
    terminal, entry = recorded
    if entry is None:
        print(f"sessions: no session is recorded for this terminal ({terminal}).", file=sys.stderr)
        return 1
    _print_terminal(terminal, entry, json_output=args.json)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ses whoami", description="Show this agent session's handle."
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--short", action="store_true", help="Print only the handle")
    output.add_argument("--json", action="store_true", help="Print structured identity")
    args = parser.parse_args(argv)
    try:
        tool, ident = current_identity()
        items, state = catalog()
        matches = [item for item in items if item.tool == tool and item.session_id == ident]
        if not matches:
            matches = register_current(tool, ident, items, state)
        if not matches:
            raise ValueError(
                "This session is not indexed yet and its transcript could not be verified; retry after its first message."
            )
        elif len(matches) == 1:
            item = matches[0]
        else:
            raise ValueError(
                "More than one native home matches this identity; use the sessions menu."
            )
        if not item.handle:
            state.handles.ensure([item])
        row = {
            "handle": item.handle,
            "title": item.title,
            "project": item.project_handle,
            "tool": item.tool,
            "session_id": item.session_id,
        }
        from .desk.view import sanitize

        if args.short:
            print(item.handle)
        elif args.json:
            print(json.dumps(row, ensure_ascii=False))
        else:
            from .app import ellipsize

            title = ellipsize(
                sanitize(item.title), max(12, shutil.get_terminal_size((100, 24)).columns - 7)
            )
            print(
                f"Handle: {item.handle}\nTitle: {title}\nProject: {item.project_handle}\nHarness: {REGISTRY.get(tool).label}"
            )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"ses: {error}", file=sys.stderr)
        return 2
