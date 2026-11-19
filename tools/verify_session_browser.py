"""Finite native Windows smoke for narrow Help and Messages; no native agents."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def child(result):
    import curses
    from unittest.mock import patch

    from ai_sessions import app, conversation_view
    from ai_sessions.config import LaunchConfig
    from ai_sessions.model import Session, Turn

    with tempfile.TemporaryDirectory(prefix="browser-console-state-") as directory:
        root = Path(directory)
        item = Session(
            "fixture", "test", "Fictional conversation", directory, 1, 1, "", True, "fiction"
        )
        state = app.UserState(root / "state.json")

        def run(screen):
            curses.resize_term(18, 40)
            browser = app.Browser(screen, [item], state, LaunchConfig(path=root / "config.toml"))
            for key in reversed([curses.KEY_NPAGE, curses.KEY_END, "q"]):
                curses.unget_wch(key)
            browser.help()
            with patch.object(
                conversation_view,
                "load_messages",
                return_value=(
                    [
                        Turn("user", "Fictional question"),
                        Turn("assistant", "Complete answer\n" * 40),
                    ],
                    "",
                ),
            ):
                for key in reversed([curses.KEY_NPAGE, "u", "a", "q"]):
                    curses.unget_wch(key)
                browser.preview_selected()

        curses.wrapper(run)
        Path(result).write_text(
            json.dumps({"help": True, "messages": True, "width": 40}), encoding="utf-8"
        )


def main():
    if len(sys.argv) > 1:
        child(sys.argv[1])
        return
    if os.name != "nt":
        raise SystemExit("This test requires a native Windows console.")
    with tempfile.TemporaryDirectory(prefix="browser-console-smoke-") as directory:
        root = Path(directory)
        result = root / "result.json"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        env = dict(os.environ)
        for variable, name in (
            ("AI_SESSIONS_CONFIG_FILE", "config.toml"),
            ("AI_SESSIONS_STATE_FILE", "state.json"),
            ("AI_SESSIONS_MESSAGING_DIR", "messaging"),
            ("AI_SESSIONS_CACHE_DIR", "cache"),
        ):
            env[variable] = str(root / name)
        with (root / "error.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(result)],
                env=env,
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
            raise RuntimeError((root / "error.log").read_text(encoding="utf-8"))
        assert json.loads(result.read_text(encoding="utf-8")) == {
            "help": True,
            "messages": True,
            "width": 40,
        }
        print("Native Windows console: 40-column Help, Messages navigation and return passed.")


if __name__ == "__main__":
    main()
