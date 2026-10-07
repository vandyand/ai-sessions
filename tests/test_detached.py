import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import psutil

from ai_sessions import app, detached
from ai_sessions.app import Session
from ai_sessions.harnesses import claude, codex
from ai_sessions.platforms import windows


def session(
    session_id: str = "lost", *, tool: str = "claude", pid: int = 0, storage: str = ""
) -> Session:
    return Session(
        tool,
        session_id,
        f"Title {session_id}",
        "C:/work",
        1_700_000_002,
        1_700_000_001,
        "preview",
        True,
        storage or "storage",
        is_open=bool(pid),
        open_pid=pid,
    )


class TerminalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "terminals.json"

    def test_a_tab_remembers_its_session_until_the_harness_exits_normally(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab-1", path=self.path)
        self.assertEqual(detached.remembered(path=self.path)["tab-1"]["session_id"], "lost")
        detached.forget(41, path=self.path)
        self.assertEqual(detached.remembered(path=self.path), {})

    def test_a_session_lives_in_one_tab_at_a_time(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab-1", path=self.path)
        detached.remember(session(), "claude", 42, 5.0, "tab-2", path=self.path)
        self.assertEqual(list(detached.remembered(path=self.path)), ["tab-2"])

    def test_old_entries_expire(self) -> None:
        stale = {"tab": {"session_id": "x", "pid": 1, "at": time.time() - 8 * 86_400}}
        self.path.write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(detached.remembered(path=self.path), {})

    def test_a_launch_whose_harness_is_gone_is_interrupted(self) -> None:
        detached.remember(session("gone"), "claude", 999_999, 5.0, "tab-1", path=self.path)
        me = psutil.Process(os.getpid())
        detached.remember(
            session("alive"), "claude", me.pid, me.create_time(), "tab-2", path=self.path
        )
        with patch.object(windows, "is_detached", return_value=False):
            lost = detached.interrupted(path=self.path)
        self.assertEqual(list(lost), ["tab-1"])


class BusyTests(unittest.TestCase):
    def write(self, *events: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "rollout.jsonl"
        rows = [{"type": "event_msg", "payload": {"type": event}} for event in events]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def codex_state(self, storage: Path, *command: str) -> str:
        return codex.turn_state(
            pid=1, home=Path("."), storage=str(storage), command=("codex.exe", *command)
        )

    def test_codex_is_working_between_task_started_and_task_complete(self) -> None:
        working = self.write("task_complete", "task_started", "token_count")
        idle = self.write("task_started", "token_count", "task_complete")
        self.assertEqual(self.codex_state(working, "resume", "x"), "working")
        self.assertEqual(self.codex_state(idle), "idle")

    def test_codex_batch_runs_and_the_daemon_are_headless(self) -> None:
        idle = self.write("task_complete")
        self.assertEqual(self.codex_state(idle, "exec", "prompt"), "headless")
        self.assertEqual(self.codex_state(idle, "app-server", "--listen"), "headless")

    def test_claude_reports_its_own_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "sessions").mkdir()
            (home / "sessions" / "7.json").write_text('{"status": "busy"}', encoding="utf-8")
            (home / "sessions" / "8.json").write_text('{"status": "idle"}', encoding="utf-8")
            states = [
                claude.turn_state(pid=pid, home=home, storage="", command=()) for pid in (7, 8, 9)
            ]
        self.assertEqual(states, ["working", "idle", ""])

    def test_an_unknown_state_falls_back_to_recent_writes(self) -> None:
        recent = self.write("anything")
        with patch.object(detached, "_turn_state", return_value=""):
            self.assertTrue(detached.is_busy(session(pid=1, storage=str(recent))))
        with patch.object(detached, "_turn_state", return_value="idle"):
            self.assertFalse(detached.is_busy(session(pid=1, storage=str(recent))))


class StopDetachedTests(unittest.TestCase):
    def test_idle_detached_sessions_stop_and_working_ones_wait(self) -> None:
        idle, working, attached = session("a", pid=11), session("b", pid=12), session("c", pid=13)
        stops: list[int] = []
        with (
            patch.object(windows, "is_detached", side_effect=lambda pid: pid != 13),
            patch.object(detached, "is_busy", side_effect=lambda item: item is working),
            patch.object(windows, "process_started", return_value=5.0),
            patch.object(
                windows, "stop_process_tree", side_effect=lambda pid, _: stops.append(pid) or ""
            ),
        ):
            stopped, waiting, problems = detached.stop_detached([idle, working, attached])
        self.assertEqual(stops, [11])
        self.assertEqual(stopped, [idle])
        self.assertEqual(waiting, [working])
        self.assertEqual(problems, [])
        self.assertFalse(idle.is_open)
        self.assertTrue(attached.is_open)
        self.assertIn("Stopped 1 session", detached.cleanup_note(stopped, waiting, problems))


class ChooseReattachTests(unittest.TestCase):
    def choose(self, sessions, *, here=(), detached_pids=(), lost=None, terminal="tab", answer="1"):
        with (
            patch.object(windows, "console_pids", return_value=tuple(here)),
            patch.object(windows, "is_detached", side_effect=lambda pid: pid in detached_pids),
            patch.object(detached, "interrupted", return_value=lost or {}),
            patch.object(detached, "terminal_id", return_value=terminal),
            patch("builtins.input", return_value=answer),
            redirect_stderr(io.StringIO()),
            patch("sys.stdout", io.StringIO()),
        ):
            return app.choose_reattach(sessions)

    def test_a_harness_detached_in_this_tab_comes_first(self) -> None:
        here, elsewhere = session("here", pid=21), session("elsewhere", pid=22)
        chosen = self.choose([elsewhere, here], here=(21,), detached_pids=(21, 22))
        self.assertIs(chosen, here)

    def test_otherwise_the_session_this_tab_last_ran(self) -> None:
        mine, other = session("mine"), session("other")
        lost = {"tab": {"session_id": "mine", "at": 1}, "tab-2": {"session_id": "other", "at": 2}}
        self.assertIs(self.choose([mine, other], lost=lost), mine)

    def test_a_fresh_tab_picks_from_everything_that_lost_its_terminal(self) -> None:
        older, newer = session("older"), session("newer")
        lost = {"a": {"session_id": "older", "at": 1}, "b": {"session_id": "newer", "at": 2}}
        self.assertIs(self.choose([older, newer], lost=lost, terminal="", answer="2"), older)

    def test_a_session_resumed_elsewhere_since_is_not_offered(self) -> None:
        resumed = session("resumed", pid=31)
        lost = {"tab": {"session_id": "resumed", "at": 1}}
        self.assertIsNone(self.choose([resumed], lost=lost))


class MakeRoomTests(unittest.TestCase):
    def run_make_room(self, chosen, others, *, here=(), detached_pids=(), busy=False, answer="n"):
        stops: list[int] = []
        with (
            patch.object(app, "detect_open_sessions"),
            patch.object(windows, "console_pids", return_value=tuple(here)),
            patch.object(windows, "is_detached", side_effect=lambda pid: pid in detached_pids),
            patch.object(detached, "is_busy", return_value=busy),
            patch.object(windows, "process_started", return_value=5.0),
            patch.object(
                windows, "stop_process_tree", side_effect=lambda pid, _: stops.append(pid) or ""
            ),
            patch("builtins.input", return_value=answer),
            redirect_stderr(io.StringIO()),
        ):
            return app.make_room(chosen, others), stops

    def test_an_idle_harness_detached_in_this_tab_is_stopped_first(self) -> None:
        ok, stops = self.run_make_room(
            session("new"), [session("old", pid=41)], here=(41,), detached_pids=(41,)
        )
        self.assertTrue(ok)
        self.assertEqual(stops, [41])

    def test_a_detached_harness_in_another_tab_is_not_this_launch_s_business(self) -> None:
        ok, stops = self.run_make_room(
            session("new"), [session("old", pid=41)], detached_pids=(41,)
        )
        self.assertTrue(ok)
        self.assertEqual(stops, [])

    def test_a_copy_attached_elsewhere_is_only_stopped_when_confirmed(self) -> None:
        chosen = session("open", pid=51)
        self.assertEqual(self.run_make_room(chosen, [], answer="n"), (False, []))
        self.assertEqual(self.run_make_room(session("open", pid=51), [], answer="y"), (True, [51]))


@unittest.skipUnless(sys.platform == "win32", "Windows process model")
class WindowsProcessTests(unittest.TestCase):
    SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]

    def test_a_process_whose_launcher_exited_is_detached(self) -> None:
        launcher = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        orphan = psutil.Process(int(launcher.stdout))
        self.addCleanup(lambda: orphan.is_running() and orphan.kill())
        self.assertTrue(windows.is_detached(orphan.pid))

    def test_a_process_whose_launcher_runs_is_attached(self) -> None:
        process = subprocess.Popen(self.SLEEP)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        self.assertFalse(windows.is_detached(process.pid))

    def test_the_harness_dies_with_its_launcher(self) -> None:
        process, job = windows.KillOnCloseJob.start(self.SLEEP)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        self.assertIsNotNone(job)
        self.assertIsNone(process.poll())
        # What Windows does when the launcher dies: its handle closes.
        job.kernel32.CloseHandle(job.handle)
        job.handle = 0
        self.assertIsNotNone(process.wait(timeout=5))

    def test_a_normal_exit_leaves_what_the_harness_started_running(self) -> None:
        process, job = windows.KillOnCloseJob.start(self.SLEEP)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        job.release()
        time.sleep(0.5)
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
