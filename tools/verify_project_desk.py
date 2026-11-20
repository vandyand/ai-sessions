"""Finite Windows console smoke test; no model calls or live agent messages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def child(result):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    console_input = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
    kernel.SetStdHandle(-10, console_input)
    import curses

    from ai_sessions.desk import view
    from ai_sessions.desk.store import Store

    with tempfile.TemporaryDirectory(prefix="desk-console-state-") as root:
        store = Store(root)
        source = Path(root) / "fiction.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        stat = source.stat()
        source_ref = {
            "tool": "claude",
            "session_id": "fiction",
            "storage": str(source),
            "title": "Garden supervisor",
        }
        store.create("smoke", [source_ref], root)
        store.publish(
            "smoke",
            0,
            {
                "observed_at": time.time(),
                "synthesized_at": time.time() - 7200,
                "sources": [
                    dict(
                        source_ref,
                        status="available",
                        fingerprint=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns],
                    )
                ],
            },
            "The review page is ready.\n\nThree plant choices still need your input.\n\nThe team has finished the layout and checked it on desktop and phone. Your previous answers are saved.\n\nNext: choose a delivery day in the Desk conversation.\n\n"
            + "Additional project context.\n" * 18,
            [
                {
                    "key": "internal",
                    "question": "Which example should we use?",
                    "context": "Fictional test.",
                    "recommendation": "Choose the first example.",
                    "status": "pending",
                    "evidence": ["fixture"],
                }
            ],
            overview=dict(
                background="We started with a bare courtyard.",
                current="Planting beds and the review page are ready.",
                direction="Choose plants, then schedule delivery.",
                goal="A shaded garden for summer.",
            ),
        )
        original_wrapper = curses.wrapper
        original_render = view.render
        tabs = []
        captures = []
        keys_seen = []
        original_handle = view.handle_key

        def handle(state, key, *args):
            keys_seen.append([repr(key), type(key).__name__])
            return original_handle(state, key, *args)

        view.handle_key = handle

        def render(snapshot, state, *args, **kwargs):
            tabs.append(state.tab)
            return original_render(snapshot, state, *args, **kwargs)

        for columns, rows in ((100, 25), (60, 12), (40, 18)):

            class Screen:
                def __init__(self, screen):
                    self.screen = screen

                def __getattr__(self, name):
                    return getattr(self.screen, name)

                def refresh(self):
                    self.screen.refresh()
                    captures.append(
                        dict(
                            width=columns,
                            height=rows,
                            lines=[
                                self.screen.instr(row, 0).decode("utf-8", errors="replace").rstrip()
                                for row in range(rows)
                            ],
                        )
                    )

            def wrapper(function):
                def enter(screen):
                    curses.resize_term(rows, columns)
                    import ctypes
                    from ctypes import wintypes

                    class KeyEvent(ctypes.Structure):
                        _fields_ = [
                            ("down", wintypes.BOOL),
                            ("repeat", wintypes.WORD),
                            ("virtual", wintypes.WORD),
                            ("scan", wintypes.WORD),
                            ("char", wintypes.WCHAR),
                            ("control", wintypes.DWORD),
                        ]

                    class Event(ctypes.Union):
                        _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]

                    class Input(ctypes.Structure):
                        _fields_ = [("type", wintypes.WORD), ("event", Event)]

                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.CreateFileW.restype = wintypes.HANDLE
                    kernel.CreateFileW.argtypes = [
                        wintypes.LPCWSTR,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        ctypes.c_void_p,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.HANDLE,
                    ]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.WriteConsoleInputW.argtypes = [
                        wintypes.HANDLE,
                        ctypes.POINTER(Input),
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                    ]
                    keys = [
                        (0x27, "", 0),
                        (0x27, "", 0),
                        (0x27, "", 0),
                        (0x25, "", 0),
                        (9, "\t", 0),
                        (9, "\t", 16),
                        (78, "n", 0),
                        (80, "p", 0),
                        (72, "h", 0),
                        (0x22, "", 0),
                        (27, "\x1b", 0),
                        (50, "2", 0),
                        (0x23, "", 0),
                        (81, "q", 0),
                    ]
                    records = []
                    for virtual, char, control in keys:
                        for down in (True, False):
                            record = Input()
                            record.type = 1
                            record.event.key = KeyEvent(
                                down,
                                1,
                                virtual,
                                0,
                                char or "\x00",
                                control | (256 if virtual in (0x25, 0x27, 0x22, 0x23) else 0),
                            )
                            records.append(record)

                    def feed():
                        import time

                        time.sleep(0.3)
                        console = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
                        try:
                            for record in records:
                                written = wintypes.DWORD()
                                if not kernel.WriteConsoleInputW(
                                    console, ctypes.byref(record), 1, ctypes.byref(written)
                                ):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                time.sleep(0.03)
                        finally:
                            kernel.CloseHandle(console)

                    import threading

                    threading.Thread(target=feed, daemon=True).start()
                    return function(Screen(screen))

                return original_wrapper(enter)

            view.render = render
            view.curses.wrapper = wrapper
            view.run(store, "smoke")
        Path(result).write_text(
            json.dumps({"tabs": tabs, "exited": True, "captures": captures, "keys": keys_seen}),
            encoding="utf-8",
        )


def main():
    if len(sys.argv) > 1:
        child(sys.argv[1])
        return
    if os.name != "nt":
        raise SystemExit("This test requires a native Windows console.")
    with tempfile.TemporaryDirectory(prefix="desk-console-smoke-") as root:
        result = Path(root) / "result.json"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        with (Path(root) / "error.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(result)],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startup,
                stderr=errors,
            )
            try:
                code = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
                raise
        if code or not result.exists():
            raise RuntimeError((Path(root) / "error.log").read_text(encoding="utf-8"))
        data = json.loads(result.read_text(encoding="utf-8"))
        if not data["exited"] or not {1, 2, 3, 4}.issubset(data["tabs"]):
            raise RuntimeError(f"Console view did not visit all tabs: {data}")
        capture_file = os.environ.get("AI_SESSIONS_DESK_SMOKE_CAPTURE")
        if capture_file:
            Path(capture_file).write_text(json.dumps(data), encoding="utf-8")
        assert {frame["width"] for frame in data["captures"]} == {40, 60, 100}
        assert any("Briefing help" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert all("? Help" in frame["lines"][-1] for frame in data["captures"])
        print("Native Windows console: 40/60/100 columns, view keys, help, scroll and exit passed.")


if __name__ == "__main__":
    main()
