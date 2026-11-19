"""Finite Windows console smoke test; no model calls or live agent messages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def child(result):
    import curses

    from ai_sessions.desk import view
    from ai_sessions.desk.store import Store

    with tempfile.TemporaryDirectory(prefix="desk-console-state-") as root:
        store = Store(root)
        store.create(
            "smoke", [{"tool": "claude", "session_id": "fiction", "storage": "fiction.jsonl"}], root
        )
        store.publish(
            "smoke",
            0,
            {"observed_at": 1},
            "Fictional console smoke test.",
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
        )
        original_wrapper = curses.wrapper
        original_render = view.render
        tabs = []

        def render(snapshot, state, *args, **kwargs):
            tabs.append(state.tab)
            return original_render(snapshot, state, *args, **kwargs)

        def wrapper(function):
            def enter(screen):
                for key in reversed("123q"):
                    curses.unget_wch(key)
                return function(screen)

            return original_wrapper(enter)

        view.render = render
        view.curses.wrapper = wrapper
        view.run(store, "smoke")
        Path(result).write_text(json.dumps({"tabs": tabs, "exited": True}), encoding="utf-8")


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
        print("Native Windows console: Review, Needs you, Next action and exit passed.")


if __name__ == "__main__":
    main()
