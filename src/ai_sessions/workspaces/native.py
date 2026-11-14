"""Native named Terminal windows with durable, verified managed pane launchers."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from pathlib import Path

from . import terminal


@contextmanager
def _db(store):
    try:
        with closing(sqlite3.connect(store, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_terminal (id INTEGER PRIMARY KEY, "
                "exe TEXT NOT NULL, state_file TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_windows (name TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
            )
            yield db
    except sqlite3.Error as error:
        raise ValueError(f"Native workspace settings unavailable: {error}") from error


def terminal_version(exe):
    if os.name != "nt":
        raise ValueError("Native Terminal workspaces require Windows")
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    size_fn = version.GetFileVersionInfoSizeW
    size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size_fn.restype = wintypes.DWORD
    size = size_fn(str(exe), None)
    data = ctypes.create_string_buffer(size)
    get_fn = version.GetFileVersionInfoW
    get_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    get_fn.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    query.restype = wintypes.BOOL
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (
        not size
        or not get_fn(str(exe), 0, size, data)
        or not query(data, "\\", ctypes.byref(pointer), ctypes.byref(length))
        or length.value < 16
    ):
        raise ValueError("Cannot read Terminal version; select Windows Terminal 1.26 or newer")
    fields = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    return fields[2] >> 16, fields[2] & 0xFFFF


def configure(store, exe, state_file=None):
    exe = Path(exe).expanduser().resolve(strict=True)
    if not exe.is_file() or terminal_version(exe) < (1, 26):
        raise ValueError("Named workspaces require Windows Terminal 1.26 or newer")
    if state_file is None:
        if (exe.parent / ".portable").is_file():
            state_file = exe.parent / "settings" / "state.json"
        else:
            raise ValueError("Supply --state-file with this Terminal installation's state.json")
    state_file = Path(state_file).expanduser().resolve()
    with _db(store) as db:
        previous = db.execute("SELECT exe,state_file FROM native_terminal WHERE id=1").fetchone()
        selected = (str(exe), str(state_file))
        if previous and previous != selected:
            # The new installation has its own saved windows.
            db.execute("DELETE FROM native_windows")
        db.execute("INSERT OR REPLACE INTO native_terminal VALUES (1,?,?)", selected)
    return exe


def split_commandline(command):
    """Use Windows' own argument parser, including paths with quotes/backslashes."""
    if os.name != "nt":
        raise ValueError("Native Terminal workspaces require Windows")
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    parse = shell.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    result = parse(command, ctypes.byref(count))
    if not result:
        raise ValueError("Cannot read saved pane launcher")
    try:
        return [result[i] for i in range(count.value)]
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(result)


def window_name(name, store, generation=0):
    terminal._identifier(name, "workspace name")
    identity = os.path.normcase(str(Path(store).resolve()))
    digest = hashlib.sha256(json.dumps([identity, name]).encode()).hexdigest()[:12]
    suffix = f"-r{generation}" if generation else ""
    return "sessions-" + name[: 58 - len(suffix)] + "-" + digest + suffix


def _command(db, workspace, store, state, recover=False):
    row = db.execute("SELECT exe,state_file FROM native_terminal WHERE id=1").fetchone()
    if row is None:
        raise ValueError(
            "Choose Preview first: sessions workspace terminal PATH_TO_WindowsTerminal.exe"
        )
    exe, state_file = row
    if not Path(exe).is_file():
        raise ValueError(
            "Configured Terminal is missing; run sessions workspace terminal PATH again"
        )
    base = window_name(workspace["name"], store)
    pending = db.execute("SELECT generation FROM native_windows WHERE name=?", (base,)).fetchone()
    generation = pending[0] if pending else 0
    name = window_name(workspace["name"], store, generation)
    create = terminal.restore_argv(workspace, window=name, store_path=store, state_path=state)
    create[0] = exe
    try:
        saved = json.loads(Path(state_file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict) or not isinstance(saved.get("persistedWorkspaces", {}), dict):
        raise ValueError("Invalid Terminal workspace state; leave it unchanged and check Terminal")
    layout = saved.get("persistedWorkspaces", {}).get(name)
    if layout is None:
        if pending:
            if not recover:
                raise ValueError(
                    "Native window is not saved yet; use and close the existing window first. "
                    f"If launch failed, run sessions workspace open {workspace['name']} --recover "
                    "to create a separate window."
                )
            generation += 1
            name = window_name(workspace["name"], store, generation)
            create[2] = name
        return create, True, generation
    if not isinstance(layout, dict) or not isinstance(layout.get("tabLayout"), list):
        raise ValueError("Invalid saved native workspace layout")
    # Compare only managed launch commands: geometry may have been changed in Terminal.
    expected = [
        terminal.pane_argv(workspace["name"], action["slot"], store, state)
        for action in terminal.validate_layout(workspace["layout"])
        if "slot" in action
    ]
    actual = []
    for action in layout["tabLayout"]:
        if not isinstance(action, dict):
            raise ValueError("Invalid saved native workspace action")
        if action.get("action") in ("newTab", "splitPane"):
            command = action.get("commandline")
            if not isinstance(command, str) or not command:
                raise ValueError("Saved pane launcher is missing; use a new workspace name")
            argv = split_commandline(command)
            if argv not in expected or argv in actual:
                raise ValueError(
                    "Saved pane launcher differs from this workspace; use a new workspace name"
                )
            actual.append(argv)
    if len(actual) != len(expected):
        raise ValueError("Saved native panes differ from this workspace; use a new workspace name")
    return [exe, "-w", name, "focus-tab", "-t", "0"], False, generation


def open_argv(workspace, store, state, *, recover=False):
    with _db(store) as db:
        return _command(db, workspace, store, state, recover)[0]


def mark_created(store, name):
    with _db(store) as db:
        db.execute("INSERT OR IGNORE INTO native_windows VALUES (?,0)", (window_name(name, store),))


def launch(workspace, store, state, *, recover=False):
    # Serialize first creation so concurrent commands cannot duplicate the layout.
    with _db(store) as db:
        command, created, generation = _command(db, workspace, store, state, recover)
        subprocess.Popen(command)
        if created:
            db.execute(
                "INSERT OR REPLACE INTO native_windows VALUES (?,?)",
                (window_name(workspace["name"], store), generation),
            )
    return command[2]
