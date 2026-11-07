"""Idempotent startup hook setup; never changes inbound permission policy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import fastjson
from . import store

MARKER = "ai_sessions.messaging.hook"


def settings() -> dict:
    return store._read(store.ROOT / "settings.json")


def homes(tool: str) -> list[Path]:
    from ..registry import REGISTRY

    configured = settings().get("homes", {}).get(tool, [])
    default = str(REGISTRY.get(tool).home.resolve())
    return [Path(p) for p in dict.fromkeys([default, *configured])]


def edit_hooks(path: Path, add: bool, tool: str = "") -> None:
    if not path.exists() and not add:
        return
    data = fastjson.read(path) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"invalid hook configuration at {path}")
    hooks = data.setdefault("hooks", {})
    for event, action in (("SessionStart", "start"), ("SessionEnd", "end")):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"invalid {event} hooks at {path}")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            others = [
                h
                for h in group["hooks"]
                if not isinstance(h, dict) or MARKER not in str(h.get("command", ""))
            ]
            if others:
                kept.append({**group, "hooks": others})
        if add:
            executable = sys.executable.replace("\\", "/")
            # Both native hook runners use PowerShell on Windows.
            program = f'& "{executable}"' if os.name == "nt" else f'"{executable}"'
            # Claude's Bash hook runner does not accept PowerShell's call operator.
            # An executable path without spaces works in both shells on this PC;
            # use a quoted Python -m through a portable shell trampoline if needed.
            if " " not in executable:
                program = executable
            elif os.name == "nt" and path.name == "settings.json":
                program = f'"{executable}"'
            kept.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{program} -m {MARKER} {action} {tool}".rstrip(),
                            "timeout": 20,
                        }
                    ]
                }
            )
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    fastjson.write_atomic(path, data, indent=True, newline=True)


def setup(add: bool = True, home: Path | None = None) -> dict:
    from ..registry import REGISTRY

    adapters = [a for a in REGISTRY.adapters() if a.messaging]
    runtime = {}
    for adapter in adapters:
        hooks = adapter.messaging
        if home and hooks.prepare and home.resolve() != adapter.home.resolve():
            raise ValueError(
                "chosen home must match the active harness home environment before installation"
            )
        if hooks.prepare and add:
            runtime = hooks.prepare(home or adapter.home)
            if runtime.get("status") not in ("running", "started"):
                raise ValueError("Native messaging runtime could not start; setup was not enabled")
    previous = settings()
    configured = previous.get("homes", {})
    for adapter in adapters:
        hooks = adapter.messaging
        root = ((home if hooks.prepare else None) or adapter.home).resolve()
        edit_hooks(root / hooks.hook_file, add, adapter.name)
        entries = set(configured.get(adapter.name, []))
        if add:
            entries.add(str(root))
        else:
            entries.discard(str(root))
        configured[adapter.name] = sorted(entries)
        if not add and hooks.forget:
            for row in store.receivers():
                if row.get("tool") == adapter.name:
                    store.queue_cleanup(row)
                    store.remove_receiver(row)
            store.cleanup_pending(adapter.name, hooks.forget)
    fastjson.write_atomic(store.ROOT / "settings.json", {"enabled": add, "homes": configured})
    return {"status": "enabled" if add else "disabled", "runtime": runtime, "restart_needed": add}
