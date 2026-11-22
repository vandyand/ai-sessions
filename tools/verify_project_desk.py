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
                    "question": "When should we schedule delivery for the courtyard garden?",
                    "context": "Residents need a shaded garden before the weekend. The nursery can deliver Friday or Monday.",
                    "recommendation": "Friday uses the covered bay to keep unloading dry.",
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
        record = store.get("smoke")
        record["decisions"].append(
            dict(
                key="deposit",
                question="What does the nursery deposit cover?",
                context="The invoice names a deposit but does not define its terms.",
                recommendation="Get the refund terms before choosing a booking.",
                status="pending",
                evidence=["fixture"],
            )
        )
        details = dict(
            schema_version=1,
            kind="choice",
            readiness="ready",
            summary="Choose Friday or Monday delivery for the courtyard garden.",
            background="The garden gives residents a shaded courtyard. The plants are ready at the nursery. Friday needs a covered unloading bay; Monday would delay planting until next week.",
            why_now="The nursery needs a booking to reserve its next delivery slot.",
            options=[
                dict(
                    id="friday",
                    label="Friday delivery",
                    description="Reserve the covered unloading bay.",
                    benefits=["Plant before the weekend"],
                    risks=["Rain may delay unloading"],
                    consequences="A volunteer must open the bay.",
                    evidence=["delivery"],
                ),
                dict(
                    id="monday",
                    label="Monday delivery",
                    description="Wait for the next scheduled delivery.",
                    benefits=["More time to arrange volunteers"],
                    risks=["Residents wait through the weekend"],
                    consequences="Planting starts next week.",
                    evidence=["delivery"],
                ),
            ],
            recommendation=dict(
                option_id="friday",
                reason="The covered bay protects the plants and lets residents use the garden sooner.",
            ),
            if_deferred="The nursery may offer Friday's slot to another customer.",
            deadline=None,
            missing_information=[],
            citations={"background": ["delivery"]},
            evidence=[
                dict(
                    id="delivery",
                    title="Garden delivery planning",
                    tool="claude",
                    session_id="fiction",
                    home=root,
                    storage=str(source),
                    identity=[1, 2],
                    start=10,
                    end=20,
                    digest="fixture",
                    text="The covered bay is available Friday. A volunteer can collect the key from the caretaker.",
                    context_only=True,
                )
            ],
        )
        record["decision_details"] = {
            "internal": details,
            "deposit": dict(
                details,
                kind="information",
                readiness="needs_context",
                summary="Define the nursery deposit before booking delivery.",
                background="The nursery invoice lists a deposit without explaining what it reserves or whether it is refundable.",
                options=[],
                recommendation=dict(
                    option_id=None, reason="The refund terms have not been supplied."
                ),
                missing_information=[
                    "What does the deposit reserve?",
                    "What is the refundable amount?",
                ],
            ),
        }

        class FixtureStore:
            def get(self, name):
                return record

        original_wrapper = curses.wrapper
        original_render = view.render
        tabs = []
        states = []
        captures = []
        keys_seen = []
        original_handle = view.handle_key

        def handle(state, key, *args):
            keys_seen.append([repr(key), type(key).__name__])
            return original_handle(state, key, *args)

        view.handle_key = handle

        def render(snapshot, state, *args, **kwargs):
            tabs.append(state.tab)
            states.append(dict(tab=state.tab, expanded=bool(state.expanded), scroll=state.scroll))
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
                        (68, "d", 0),
                        (0x23, "", 0),
                        (0x26, "", 0),
                        (0x28, "", 0),
                        (0x21, "", 0),
                        (0x22, "", 0),
                        (0x24, "", 0),
                        (68, "d", 0),
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
                                control
                                | (
                                    256
                                    if virtual in (0x25, 0x27, 0x21, 0x22, 0x23, 0x24, 0x26, 0x28)
                                    else 0
                                ),
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
            view.run(FixtureStore(), "smoke")
        Path(result).write_text(
            json.dumps(
                {
                    "tabs": tabs,
                    "exited": True,
                    "captures": captures,
                    "keys": keys_seen,
                    "states": states,
                }
            ),
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
        if not data["exited"] or not {1, 2, 3}.issubset(data["tabs"]):
            raise RuntimeError(f"Console view did not visit all tabs: {data}")
        assert any(row["expanded"] for row in data["states"])
        assert any(
            "Garden delivery planning" in "\n".join(frame["lines"]) for frame in data["captures"]
        )
        assert any("Needs context" in "\n".join(frame["lines"]) for frame in data["captures"])
        import curses

        seen = {value for value, kind in data["keys"] if kind == "int"}
        assert {
            str(key)
            for key in (
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                curses.KEY_BTAB,
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_HOME,
                curses.KEY_END,
                curses.KEY_PPAGE,
                curses.KEY_NPAGE,
            )
        }.issubset(seen), seen
        capture_file = os.environ.get("AI_SESSIONS_DESK_SMOKE_CAPTURE")
        if capture_file:
            Path(capture_file).write_text(json.dumps(data), encoding="utf-8")
        assert {frame["width"] for frame in data["captures"]} == {40, 60, 100}
        assert any("Briefing help" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert all("? Help" in frame["lines"][-1] for frame in data["captures"])
        print("Native Windows console: 40/60/100 columns, view keys, help, scroll and exit passed.")


if __name__ == "__main__":
    main()
