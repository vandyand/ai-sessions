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

        class FixtureStore(Store):
            def __init__(self):
                self.root, self.path, self.readonly = store.root, store.path, False

            def _get(self, db, name):
                snapshot = Store._get(db, name)
                # Rendering-only fixture details; production validates retrieved sources.
                snapshot["decision_details"] = record["decision_details"]
                return snapshot

            def get(self, name):
                with self._connect() as db:
                    snapshot = self._get(db, name)
                return dict(
                    record,
                    decisions=snapshot["decisions"],
                    decision_meta=snapshot["decision_meta"],
                    decision_discussion=snapshot["decision_discussion"],
                    decision_drafts=snapshot["decision_drafts"],
                )

        from ai_sessions.desk import discussion, runtime

        original_submit = discussion.submit

        def submit_fixture(*args, **kwargs):
            entry = original_submit(*args, **kwargs)
            if entry["kind"] == "decision":
                entry = discussion.update(
                    args[0],
                    args[1],
                    entry["id"],
                    delivery_wait="offline",
                    recipient=dict(tool="claude", session_id="fixture-desk", home=root),
                )
            return entry

        discussion.submit = submit_fixture
        runtime.ensure_observer = lambda *args, **kwargs: None

        original_wrapper = curses.wrapper
        original_render = view.render
        tabs = []
        states = []
        captures = []
        keys_seen = []
        input_times = []
        mouse_events = []
        original_getmouse = curses.getmouse

        def getmouse():
            event = original_getmouse()
            mouse_events.append(event)
            return event

        curses.getmouse = getmouse
        original_handle = view.handle_key

        def handle(state, key, *args):
            keys_seen.append([repr(key), type(key).__name__])
            input_times.append([repr(key), time.monotonic()])
            return original_handle(state, key, *args)

        view.handle_key = handle

        def render(snapshot, state, *args, **kwargs):
            tabs.append(state.tab)
            states.append(
                dict(
                    tab=state.tab,
                    expanded=bool(state.expanded),
                    scroll=state.scroll,
                    editing=bool(state.editor),
                )
            )
            return original_render(snapshot, state, *args, **kwargs)

        for columns, rows in ((100, 25), (60, 12), (40, 18)):
            with store._connect() as db:
                db.execute("DELETE FROM desk_discussion WHERE desk=?", ("smoke",))
                db.execute("DELETE FROM desk_decision_drafts WHERE desk=?", ("smoke",))

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
                            cells=[
                                [self.screen.inch(y, x) for x in range(columns)]
                                for y in range(rows)
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

                    class Coord(ctypes.Structure):
                        _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

                    class MouseEvent(ctypes.Structure):
                        _fields_ = [
                            ("position", Coord),
                            ("buttons", wintypes.DWORD),
                            ("control", wintypes.DWORD),
                            ("flags", wintypes.DWORD),
                        ]

                    class Event(ctypes.Union):
                        _fields_ = [
                            ("key", KeyEvent),
                            ("mouse", MouseEvent),
                            ("padding", ctypes.c_byte * 16),
                        ]

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
                    keys = (
                        [(0x28, "", 0)] * 20
                        + [(0x27, "", 0), (0x25, "", 0)]
                        + [
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
                            (69, "e", 0),
                            (81, "q", 0),
                            (13, "\r", 0),
                            (88, "x", 0),
                            (9, "\t", 0),
                            (13, "\r", 0),
                            (84, "t", 0),
                            (84, "t", 0),
                            (13, "\r", 0),
                            (69, "e", 0),
                            (70, "F", 0),
                            (82, "r", 0),
                            (73, "i", 0),
                            (68, "d", 0),
                            (65, "a", 0),
                            (89, "y", 0),
                            (190, ".", 0),
                            (9, "\t", 0),
                            (9, "\t", 0),
                            (9, "\t", 0),
                            (13, "\r", 0),
                            (84, "t", 0),
                            (86, "v", 0),
                            (0x23, "", 0),
                            (84, "t", 0),
                            (13, "\r", 0),
                            (65, "a", 0),
                            (66, "b", 0),
                            (67, "c", 0),
                            (9, "\t", 0),
                            (70, "F", 0),
                            (73, "i", 0),
                            (88, "x", 0),
                            (69, "e", 0),
                            (68, "d", 0),
                            (9, "\t", 16),
                            (13, "\r", 0),
                            (85, "u", 0),
                            (84, "t", 0),
                            (84, "t", 0),
                            (72, "h", 0),
                            (0x22, "", 0),
                            (27, "\x1b", 0),
                            (50, "2", 0),
                            (0x23, "", 0),
                            (81, "q", 0),
                        ]
                    )
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

                    for delta in (120, -120):
                        wheel = Input()
                        wheel.type = 2
                        wheel.event.mouse = MouseEvent(Coord(3, 5), (delta & 0xFFFF) << 16, 0, 4)
                        records.insert(44, wheel)

                    def feed():
                        import time

                        time.sleep(0.3)
                        console = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
                        try:
                            for index, record in enumerate(records):
                                written = wintypes.DWORD()
                                if not kernel.WriteConsoleInputW(
                                    console, ctypes.byref(record), 1, ctypes.byref(written)
                                ):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                time.sleep(0.001 if index < 44 else 0.03)
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
                    "input_times": input_times,
                    "mouse_events": mouse_events,
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
            environment = dict(os.environ)
            if not environment.get("AI_SESSIONS_DESK_SMOKE_MONO"):
                environment.pop("NO_COLOR", None)
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(result)],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startup,
                stderr=errors,
                env=environment,
            )
            try:
                code = process.wait(timeout=30)
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
        assert any("Background needed" in "\n".join(frame["lines"]) for frame in data["captures"])
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
        assert all(
            "? Help" in frame["lines"][-1] or "Tab Actions" in frame["lines"][-1]
            for frame in data["captures"]
        )
        for start in range(len(data["input_times"]) - 20):
            window = data["input_times"][start : start + 21]
            if all(item[0] == str(curses.KEY_DOWN) for item in window[:20]):
                assert window[-1][1] - window[0][1] < 1.5, window
        assert any(key == "'wheel_down'" for key, _ in data["keys"]), (
            "Wheel down did not reach the app"
        )
        assert any(key == "'wheel_up'" for key, _ in data["keys"]), "Wheel up did not reach the app"
        assert any(
            "Your answer is saved" in "\n".join(frame["lines"]) for frame in data["captures"]
        )
        assert any("v Earlier versions" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert any(row["editing"] for row in data["states"])
        assert any("Note saved" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert any("Marked as completed" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert any("Completion undone" in "\n".join(frame["lines"]) for frame in data["captures"])
        assert any(
            cell & curses.A_REVERSE
            for frame in data["captures"]
            for row in frame["cells"]
            for cell in row
        )
        colored = any(
            cell & curses.A_COLOR
            for frame in data["captures"]
            for row in frame["cells"]
            for cell in row
        )
        assert not colored if os.environ.get("AI_SESSIONS_DESK_SMOKE_MONO") else colored
        print(
            "Native Windows console: 40/60/100 columns, view keys, multiline notes, discussion, help, scroll and exit passed."
        )


if __name__ == "__main__":
    main()
