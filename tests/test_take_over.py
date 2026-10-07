import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_display import ScriptedScreen

from ai_sessions import app
from ai_sessions import detached as detached_module
from ai_sessions.app import Browser, Session, UserState, detect_open_sessions
from ai_sessions.config import LaunchConfig
from ai_sessions.discovery import HarnessContext
from ai_sessions.liveness import ProcessInfo
from ai_sessions.platforms import windows
from ai_sessions.registry import REGISTRY


def open_session(cwd: str) -> Session:
    return Session(
        "claude",
        "running",
        "running",
        cwd,
        1_700_000_002,
        1_700_000_001,
        "preview",
        True,
        "storage",
        is_open=True,
        open_pid=4242,
    )


class TakeOverTests(unittest.TestCase):
    def run_browser(
        self, *keys: object, problem: str = "", detached: bool = False, busy: bool = False
    ) -> tuple[object, Browser, list]:
        stops: list[tuple[int, float]] = []

        def stop(pid: int, started: float) -> str:
            stops.append((pid, started))
            return problem

        with tempfile.TemporaryDirectory() as directory:
            browser = Browser(
                ScriptedScreen(*keys),
                [open_session(directory)],
                UserState(Path(directory) / "state.json"),
                LaunchConfig(path=Path(directory) / "config.toml"),
            )
            with (
                patch.object(app, "IS_WINDOWS", True),
                patch.object(app, "detect_open_sessions"),
                patch.object(windows, "process_started", return_value=1_700_000_100.0),
                patch.object(windows, "stop_process_tree", side_effect=stop),
                patch.object(windows, "is_detached", return_value=detached),
                patch.object(detached_module, "is_busy", return_value=busy),
            ):
                chosen = browser.run()
        return chosen, browser, stops

    def test_confirmed_take_over_stops_the_running_copy_then_resumes_here(self) -> None:
        chosen, _browser, stops = self.run_browser("\n", "y")
        self.assertEqual(stops, [(4242, 1_700_000_100.0)])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.session_id, "running")
        self.assertFalse(chosen.is_open)

    def test_declined_take_over_leaves_the_running_copy_alone(self) -> None:
        chosen, browser, stops = self.run_browser("\n", "n", "q")
        self.assertIsNone(chosen)
        self.assertEqual(stops, [])

    def test_a_copy_that_will_not_stop_is_not_resumed_twice(self) -> None:
        chosen, browser, stops = self.run_browser(
            "\n", "y", "q", problem="PID 4242 did not exit within 5 seconds."
        )
        self.assertIsNone(chosen)
        self.assertEqual(len(stops), 1)

    def test_an_idle_detached_copy_is_replaced_without_asking(self) -> None:
        chosen, _browser, stops = self.run_browser("\n", detached=True)
        self.assertEqual(stops, [(4242, 1_700_000_100.0)])
        self.assertIsNotNone(chosen)

    def test_a_detached_copy_still_working_needs_confirmation(self) -> None:
        chosen, browser, stops = self.run_browser("\n", "n", "q", detached=True, busy=True)
        self.assertIsNone(chosen)
        self.assertEqual(stops, [])


class StopProcessTreeTests(unittest.TestCase):
    def start_sleeper(self) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        return process

    def test_stops_the_verified_process(self) -> None:
        process = self.start_sleeper()
        started = windows.process_started(process.pid)
        self.assertTrue(started)
        self.assertEqual(windows.stop_process_tree(process.pid, started), "")
        self.assertIsNotNone(process.wait(timeout=5))

    def test_refuses_a_recycled_pid(self) -> None:
        process = self.start_sleeper()
        started = windows.process_started(process.pid)
        problem = windows.stop_process_tree(process.pid, started - 3600)
        self.assertIn("different process", problem)
        self.assertIsNone(process.poll())

    def test_a_process_already_gone_counts_as_stopped(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        started = windows.process_started(process.pid)
        process.wait(timeout=5)
        self.assertEqual(windows.stop_process_tree(process.pid, started), "")


class CodexAppServerTests(unittest.TestCase):
    def test_shared_app_server_is_not_a_terminal_holding_its_last_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = sqlite3.connect(root / "logs_2.sqlite")
            logs.execute(
                "CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, process_uuid TEXT)"
            )
            logs.execute("INSERT INTO logs VALUES (10, 0, 'thread', 'pid:42:daemon')")
            logs.execute("INSERT INTO logs VALUES (10, 0, 'other', 'pid:43:tui')")
            logs.commit()
            logs.close()

            daemon = Session("codex", "thread", "", "", 0, 0, "", False, "storage")
            terminal = Session("codex", "other", "", "", 0, 0, "", False, "storage")
            context = HarnessContext.create()
            context.platform = "win32"
            context.process_snapshot = (
                ProcessInfo(
                    42,
                    "codex.exe",
                    ("codex.exe", "app-server", "--listen", "unix://", "--managed-daemon"),
                    "",
                    started_at=5.0,
                ),
                ProcessInfo(43, "codex.exe", ("codex.exe", "resume", "other"), "", started_at=5.0),
            )
            context.liveness_ready = True
            adapter = replace(REGISTRY.get("codex"), home=root)
            with REGISTRY.temporary(adapter):
                detect_open_sessions([daemon, terminal], context=context)

        self.assertFalse(daemon.is_open)
        self.assertTrue(terminal.is_open)
        self.assertEqual(terminal.open_pid, 43)


if __name__ == "__main__":
    unittest.main()
