"""Read-only Terminal layout import and managed workspace command construction."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

from ai_sessions.paths import STATE_FILE

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_DIRECTIONS = ("left", "right", "up", "down", "previousInOrder", "nextInOrder", "first")


def _text(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {label}")
    return value


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def validate_layout(layout):
    """Return only managed layout fields, rejecting ambiguous structural actions."""
    if not isinstance(layout, list) or not layout:
        raise ValueError("Layout must contain a newTab")
    result = []
    count = 0
    slots = set()
    for raw in layout:
        if not isinstance(raw, dict):
            raise ValueError("Layout action must be an object")
        action = raw.get("action")
        item = {"action": action}
        if action in ("newTab", "splitPane"):
            if (action == "newTab") != (count == 0):
                raise ValueError("Layout must start with exactly one newTab")
            slot = raw.get("slot", f"pane-{count}")
            if not isinstance(slot, str) or not re.fullmatch(r"pane-[0-9]+", slot) or slot in slots:
                raise ValueError("Pane slots must be unique pane-N identifiers")
            slots.add(slot)
            item.update(
                slot=slot,
                cwd=_text(raw.get("cwd", raw.get("startingDirectory", str(Path.home()))), "cwd"),
            )
            for key, source in (("profile", "profile"), ("title", "tabTitle")):
                value = raw.get(key, raw.get(source))
                if value is not None:
                    item[key] = _text(value, key)
            if action == "splitPane":
                size = raw.get("size", 0.5)
                if raw.get("splitMode", "manual") != "manual":
                    raise ValueError("Unsupported splitMode")
                if raw.get("split") not in ("right", "down"):
                    raise ValueError("Unsupported split direction")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, (int, float))
                    or not math.isfinite(size)
                    or not 0 < size < 1
                ):
                    raise ValueError("Split size must be between zero and one")
                item.update(split=raw["split"], size=size)
            count += 1
        elif action == "moveFocus":
            if not count or raw.get("direction") not in _DIRECTIONS:
                raise ValueError("Invalid moveFocus")
            item["direction"] = raw["direction"]
        elif action == "focusPane":
            target = raw.get("id")
            if type(target) is not int or not 0 <= target < count:
                raise ValueError("focusPane must reference an existing tab-local pane")
            item["id"] = target
        else:
            raise ValueError(f"Unsupported layout action: {action!r}")
        result.append(item)
    return result


def saved_tabs(path=None):
    """Import each persisted tab without copying commands or modifying Terminal state."""
    if path is None:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        path = local / "Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/state.json"
    state = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict) or not isinstance(state.get("persistedWindowLayouts"), list):
        raise ValueError("Terminal state has no persistedWindowLayouts")
    tabs = []
    for window, saved in enumerate(state["persistedWindowLayouts"]):
        if not isinstance(saved, dict) or not isinstance(saved.get("tabLayout"), list):
            raise ValueError("Invalid persisted window layout")
        groups = []
        for raw in saved["tabLayout"]:
            if not isinstance(raw, dict):
                raise ValueError("Invalid Terminal action")
            if raw.get("action") == "switchToTab":
                index = raw.get("index")
                if type(index) is not int or not 0 <= index < len(groups):
                    raise ValueError("Invalid switchToTab")
                continue
            if raw.get("action") == "newTab":
                groups.append([])
            if not groups:
                raise ValueError("Pane action before newTab")
            groups[-1].append(raw)
        for tab, group in enumerate(groups):
            layout = validate_layout(group)
            tabs.append(
                {
                    "window": window,
                    "tab": tab,
                    "title": layout[0].get("title", ""),
                    "pane_count": sum(a["action"] in {"newTab", "splitPane"} for a in layout),
                    "layout": layout,
                }
            )
    return tabs


def _wt_arg(value):
    # Terminal splits every argument on unescaped semicolons, even inside quotes.
    return _text(str(value), "argument").replace(";", "\\;")


def restore_argv(workspace, *, window="0", store_path=None, state_path=None):
    """Build argv only; never launch Terminal or execute saved commandlines."""
    if not isinstance(workspace, dict):
        raise ValueError("Workspace must be an object")
    name = _identifier(workspace.get("name"), "workspace name")
    window = str(window)
    if window != "-1":
        window = _identifier(window, "window")
    layout = validate_layout(workspace.get("layout"))
    state_path = (
        Path(state_path or os.environ.get("AI_SESSIONS_STATE_FILE") or STATE_FILE)
        .expanduser()
        .absolute()
    )
    store_path = (
        Path(
            store_path
            or os.environ.get("AI_SESSIONS_WORKSPACE_FILE")
            or state_path.with_suffix(".workspaces.sqlite3")
        )
        .expanduser()
        .absolute()
    )
    args = ["wt", "-w", window]
    for index, action in enumerate(layout):
        if index:
            args.append(";")
        kind = action["action"]
        if kind == "moveFocus":
            args.extend(["move-focus", action["direction"]])
        elif kind == "focusPane":
            args.extend(["focus-pane", "-t", str(action["id"])])
        else:
            args.append("new-tab" if kind == "newTab" else "split-pane")
            if kind == "splitPane":
                args.extend(
                    ["-V" if action["split"] == "right" else "-H", "--size", str(action["size"])]
                )
            args.extend(["--startingDirectory", _wt_arg(action["cwd"])])
            for key, flag in (("profile", "--profile"), ("title", "--title")):
                if key in action:
                    args.extend([flag, _wt_arg(action[key])])
            args.extend(
                _wt_arg(value) for value in pane_argv(name, action["slot"], store_path, state_path)
            )
    return args


def pane_argv(name, slot, store_path, state_path):
    """The durable command Terminal must retain for one managed pane."""
    _identifier(name, "workspace name")
    if not re.fullmatch(r"pane-[0-9]+", slot):
        raise ValueError("Invalid pane slot")
    return [
        "pwsh",
        "-NoExit",
        "-File",
        str(Path(__file__).with_name("launch.ps1").absolute()),
        "-Workspace",
        name,
        "-Pane",
        slot,
        "-WorkspaceFile",
        str(Path(store_path).expanduser().absolute()),
        "-StateFile",
        str(Path(state_path).expanduser().absolute()),
        "-Python",
        sys.executable,
    ]
