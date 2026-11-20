"""Current-session handle lookup; never launches or stops a native harness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from .capabilities import Unsupported
from .registry import REGISTRY


def catalog():
    from .app import UserState, load_session_catalog

    state = UserState()
    return load_session_catalog(state) or [], state


def managed_session():
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
                        return adapter.name, ref.session_id
                    return adapter.name, ""
    except psutil.Error:
        pass
    if found is not None:
        _, entry = found
        adapter = REGISTRY.get(str(entry.get("tool", "")))
        if not isinstance(adapter.current_session, Unsupported):
            ref = adapter.current_session(int(entry["pid"]), float(entry["started"]))
            if ref is not None:
                return adapter.name, ref.session_id
        # A remembered launch is harness evidence, not proof of the current
        # thread after an in-process session switch.
        return adapter.name, ""
    return None


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
            raise ValueError(
                "This session is not indexed yet; open the sessions menu and let it refresh."
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
