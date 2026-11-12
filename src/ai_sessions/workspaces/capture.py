"""Capture live pane geometry using bounded console markers, never terminal text."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


def terminal_key(value):
    return value.strip().strip("{}").casefold()


def layout_from_panes(panes, *, current=""):
    if not panes or len(panes) > 64:
        raise ValueError("Expected between 1 and 64 visible terminal panes.")
    clean = []
    for pane in panes:
        item = {k: pane[k] for k in ("x", "y", "width", "height")}
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in item.values()):
            raise ValueError("Invalid pane bounds.")
        if item["width"] <= 0 or item["height"] <= 0:
            raise ValueError("Cannot capture a minimized or hidden pane.")
        clean.append({**item, "terminal": pane.get("terminal", "")})

    def tree(group):
        if len(group) == 1:
            return group[0]
        for axis, length, direction in (("x", "width", "right"), ("y", "height", "down")):
            for start in sorted({p[axis] for p in group})[1:]:
                first = [p for p in group if p[axis] < start]
                second = [p for p in group if p[axis] >= start]
                edge = max(p[axis] + p[length] for p in first)
                if edge > start + 1:
                    continue
                low = min(p[axis] for p in first)
                high = max(p[axis] + p[length] for p in second)
                cut = (edge + start) / 2
                return direction, (high - cut) / (high - low), tree(first), tree(second)
        raise ValueError("Pane geometry overlaps or cannot be reconstructed as terminal splits.")

    root = tree(clean)
    actions, slots = [], {}

    def first_leaf(node):
        return node if isinstance(node, dict) else first_leaf(node[2])

    def create(pane, kind, **kwargs):
        index = len(slots)
        slot = f"pane-{index}"
        slots[slot] = pane
        actions.append(dict(action=kind, slot=slot, cwd=str(Path.home()), **kwargs))
        return index

    def walk(node, index):
        if isinstance(node, dict):
            return
        direction, ratio, first, second = node
        actions.append(dict(action="focusPane", id=index))
        other = create(first_leaf(second), "splitPane", split=direction, size=ratio)
        walk(first, index)
        walk(second, other)

    initial = create(first_leaf(root), "newTab")
    walk(root, initial)
    if current:
        matches = [i for i, p in enumerate(slots.values()) if p["terminal"] == current]
        if len(matches) != 1:
            raise ValueError("Could not identify this pane uniquely; retry from the desired tab.")
        actions.append(dict(action="focusPane", id=matches[0]))
    return {"layout": actions, "panes": slots}


def _marker_main():
    """Isolated helper: attach only to verified clients and restore titles on EOF."""
    import ctypes

    import psutil

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleTitleW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
    request = json.loads(sys.stdin.readline())
    changed = []
    try:
        for item in request:
            try:
                if abs(psutil.Process(item["pid"]).create_time() - item["started"]) >= 0.01:
                    continue
                kernel.FreeConsole()
                if not kernel.AttachConsole(item["pid"]):
                    continue
                old = ctypes.create_unicode_buffer(32768)
                kernel.GetConsoleTitleW(old, len(old))
                if kernel.SetConsoleTitleW(item["marker"]):
                    changed.append((item, old.value))
            except psutil.Error:
                continue
        kernel.FreeConsole()
        print(json.dumps([i[0]["marker"] for i in changed]), flush=True)
        # Parent closes the pipe in finally, including on UIA timeout/failure.
        sys.stdin.readline()
    finally:
        for item, old in reversed(changed):
            try:
                if abs(psutil.Process(item["pid"]).create_time() - item["started"]) >= 0.01:
                    continue
                kernel.FreeConsole()
                if kernel.AttachConsole(item["pid"]):
                    title = ctypes.create_unicode_buffer(32768)
                    kernel.GetConsoleTitleW(title, len(title))
                    if title.value == item["marker"]:
                        kernel.SetConsoleTitleW(old)
            except psutil.Error:
                continue
        kernel.FreeConsole()


@contextmanager
def _marked(targets):
    process = subprocess.Popen(
        [sys.executable, "-m", __name__, "mark"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.stdin.write(json.dumps(targets) + "\n")
        process.stdin.flush()
        ready = queue.Queue()

        def read_ready():
            # A blocked buffered readline holds its stream lock, so closing
            # that stream during a timeout could itself block indefinitely.
            data = b""
            try:
                while b"\n" not in data:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            ready.put(data.decode("utf-8", errors="replace"))

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        try:
            line = ready.get(timeout=5)
            if not isinstance(json.loads(line), list):
                raise ValueError
        except (queue.Empty, ValueError) as exc:
            raise ValueError(
                "Could not identify live console panes; no workspace was saved."
            ) from exc
        yield
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            # Leave the helper alive to finish its restoration finally block.
            # TerminateProcess would bypass it and strand temporary titles.
            raise ValueError(
                "Pane titles are still being restored; retry saving after cleanup finishes."
            ) from exc
        finally:
            process.stdout.close()
            process.stderr.close()


def _read_controls(prefix, caller):
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(Path(__file__).with_name("capture.ps1")),
        "-Prefix",
        prefix,
        "-Caller",
        caller,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=12
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Live Terminal capture timed out; no workspace was saved.") from exc
    if result.returncode:
        raise ValueError(
            "Could not identify this live Terminal tab; run save inside the desired tab."
        )
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("Terminal returned no usable live pane geometry.") from exc


def current_tab():
    if os.name != "nt" or not os.environ.get("WT_SESSION"):
        raise ValueError(
            "Run workspace save in a pane of the Windows Terminal tab you want to save."
        )
    import psutil

    from ..detached import TERMINALS_FILE, _read

    caller = terminal_key(os.environ["WT_SESSION"])
    prefix = f"ai-sessions-{uuid.uuid4().hex}"
    by_terminal = {}
    for terminal, entry in _read(TERMINALS_FILE).items():
        try:
            uuid.UUID(terminal_key(terminal))
        except ValueError:
            continue
        if not entry.get("closed") and entry.get("pid") and entry.get("started"):
            by_terminal[terminal_key(terminal)] = dict(pid=entry["pid"], started=entry["started"])
    by_terminal[caller] = dict(pid=os.getpid(), started=psutil.Process().create_time())
    targets = [
        dict(**value, marker=f"{prefix}-{i}") for i, value in enumerate(by_terminal.values())
    ]
    markers = {target["marker"]: key for key, target in zip(by_terminal, targets)}
    own_marker = targets[list(by_terminal).index(caller)]["marker"]
    with _marked(targets):
        controls = _read_controls(prefix, own_marker)
    panes = [{**p, "terminal": markers.get(p.get("marker"), "")} for p in controls]
    return layout_from_panes(panes, current=caller)


if __name__ == "__main__":
    _marker_main()
