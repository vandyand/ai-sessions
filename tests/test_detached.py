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

    def test_a_normal_exit_is_kept_for_reopen_but_not_offered_to_reattach(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab-1", path=self.path)
        self.assertEqual(detached.remembered(path=self.path)["tab-1"]["session_id"], "lost")
        detached.mark_closed(41, path=self.path)
        self.assertEqual(list(detached.closed(path=self.path)), ["tab-1"])
        with patch.object(windows, "is_detached", return_value=False):
            self.assertEqual(detached.interrupted(path=self.path), {})

    def test_closed_sessions_are_listed_newest_first(self) -> None:
        for pid, name in ((41, "older"), (42, "newer")):
            detached.remember(session(name), "claude", pid, 5.0, f"tab-{pid}", path=self.path)
            detached.mark_closed(pid, path=self.path)
            time.sleep(0.01)
        ids = [entry["session_id"] for entry in detached.closed(path=self.path).values()]
        self.assertEqual(ids, ["newer", "older"])

    def test_reopening_in_another_tab_moves_the_memory_there(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab-1", path=self.path)
        detached.mark_closed(41, path=self.path)
        detached.remember(session(), "claude", 42, 5.0, "tab-2", path=self.path)
        self.assertEqual(detached.closed(path=self.path), {})
        self.assertEqual(list(detached.remembered(path=self.path)), ["tab-2"])

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


class RestartTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "terminals.json"

    def test_a_restart_request_is_reported_once_when_the_harness_exits(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab", path=self.path)
        detached.request_restart("tab", path=self.path)
        self.assertEqual(detached.mark_closed(41, path=self.path), "")
        entry = detached.remembered(path=self.path)["tab"]
        self.assertTrue(entry["closed"])
        self.assertNotIn("restart", entry)
        detached.remember(session(), "claude", 42, 5.0, "tab", path=self.path)
        self.assertIsNone(detached.mark_closed(42, path=self.path))

    def test_a_restart_into_another_harness_reports_that_harness_once(self) -> None:
        detached.remember(session(), "claude", 41, 5.0, "tab", path=self.path)
        detached.request_restart("tab", "codex", path=self.path)
        self.assertEqual(detached.mark_closed(41, path=self.path), "codex")
        self.assertNotIn("restart_tool", detached.remembered(path=self.path)["tab"])
        detached.remember(session(), "codex", 42, 5.0, "tab", path=self.path)
        detached.request_restart("tab", path=self.path)
        self.assertEqual(detached.mark_closed(42, path=self.path), "")

    def test_the_session_a_process_runs_inside_is_found_through_its_parents(self) -> None:
        parent = psutil.Process().parent()
        detached.remember(
            session("outer"), "claude", parent.pid, parent.create_time(), "tab", path=self.path
        )
        detached.remember(session("elsewhere"), "claude", 999_999, 5.0, "other", path=self.path)
        key, entry = detached.running_here(path=self.path)
        self.assertEqual((key, entry["session_id"]), ("tab", "outer"))

    def test_a_closed_launch_is_not_running_here(self) -> None:
        parent = psutil.Process().parent()
        detached.remember(
            session(), "claude", parent.pid, parent.create_time(), "tab", path=self.path
        )
        detached.mark_closed(parent.pid, path=self.path)
        self.assertIsNone(detached.running_here(path=self.path))

    def test_restart_stops_the_harness_but_spares_a_shared_daemon(self) -> None:
        # A stand-in harness that starts an ordinary child and a shared app-server.
        sleeper = "import time; time.sleep(60)"
        script = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {sleeper!r}, 'app-server'])\n"
            f"subprocess.Popen([sys.executable, '-c', {sleeper!r}])\n"
            "time.sleep(60)\n"
        )
        harness = subprocess.Popen([sys.executable, "-c", script])
        self.addCleanup(harness.kill)
        root = psutil.Process(harness.pid)
        deadline = time.time() + 15
        while len(root.children()) < 2 and time.time() < deadline:
            time.sleep(0.05)
        children = root.children()
        daemon = next(child for child in children if "app-server" in child.cmdline())
        ordinary = next(child for child in children if child.pid != daemon.pid)
        self.addCleanup(lambda: daemon.kill() if daemon.is_running() else None)
        self.addCleanup(lambda: ordinary.kill() if ordinary.is_running() else None)
        problem = detached.stop_for_restart(harness.pid, root.create_time(), {"no-such-harness"})
        self.assertEqual(problem, "")
        psutil.wait_procs([ordinary], timeout=5)
        self.assertFalse(root.is_running())
        self.assertFalse(ordinary.is_running())
        self.assertTrue(daemon.is_running())

    def test_restart_refuses_a_recycled_pid(self) -> None:
        me = psutil.Process()
        self.assertIn("different process", detached.stop_for_restart(me.pid, 1.0, ()))

    def test_restart_outside_a_session_launched_by_sessions_says_so(self) -> None:
        with (
            patch.object(detached, "running_here", return_value=None),
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(app.restart_here(), 2)
        self.assertIn("opened in Windows Terminal", errors.getvalue())

    def test_a_restart_reopens_with_the_same_launch_mode(self) -> None:
        calls: list[list[str] | None] = []

        def fake_main(argv):
            calls.append(argv)
            app._restart_into = "" if len(calls) == 1 else None
            return 0

        with patch.object(app, "_main", fake_main):
            self.assertEqual(app.main(["--launch-mode", "dangerous"]), 0)
        self.assertEqual(
            calls, [["--launch-mode", "dangerous"], ["--reopen", "--launch-mode", "dangerous"]]
        )

    def test_a_restart_into_another_harness_reopens_there_once(self) -> None:
        calls: list[list[str] | None] = []
        wanted = iter(["codex", "", None])

        def fake_main(argv):
            calls.append(argv)
            app._restart_into = next(wanted)
            return 0

        with patch.object(app, "_main", fake_main):
            app.main([])
        self.assertEqual(calls, [[], ["--reopen", "--launch-tool", "codex"], ["--reopen"]])

    def test_each_harness_has_a_flag_for_restarting_into_it(self) -> None:
        for name in app.REGISTRY.names():
            args = app.build_parser().parse_args(["--restart", f"--{name}"])
            self.assertEqual((args.restart, args.launch_tool), (True, name))

    def restart(self, tool: str, launch_tool: str) -> tuple[int, list, str]:
        requests: list = []
        self.stopped: list = []
        entry = {"tool": tool, "pid": 41, "started": 5.0, "title": "work"}
        with (
            patch.object(detached, "running_here", return_value=("tab", entry)),
            patch.object(detached, "request_restart", lambda *a: requests.append(a)),
            patch.object(detached, "stop_for_restart", lambda *a: self.stopped.append(a) or ""),
            patch("sys.stdout", io.StringIO()) as out,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = app.restart_here(launch_tool)
        return code, requests, out.getvalue() + errors.getvalue()

    def test_restart_into_another_harness_asks_its_launcher_for_that_harness(self) -> None:
        code, requests, output = self.restart("claude", "codex")
        self.assertEqual((code, requests), (0, [("tab", "codex")]))
        self.assertEqual(len(self.stopped), 1)
        self.assertIn("in Codex", output)

    def test_restart_into_the_same_harness_is_a_plain_restart(self) -> None:
        code, requests, _output = self.restart("claude", "claude")
        self.assertEqual((code, requests), (0, [("tab", "")]))

    def test_restart_into_a_harness_that_cannot_take_it_leaves_the_session_running(self) -> None:
        with patch.object(app.REGISTRY, "bridge_targets", return_value=("claude",)):
            code, requests, output = self.restart("claude", "codex")
        self.assertEqual((code, requests, self.stopped), (2, [], []))
        self.assertIn("left running", output)


class ChooseReopenTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "terminals.json"

    def run_in_tab(self, pid: int, name: str, tab: str) -> None:
        detached.remember(session(name), "claude", pid, 5.0, tab, path=self.path)
        detached.mark_closed(pid, path=self.path)

    def choose(self, sessions, *, terminal="tab", answer="1", lost=None):
        closed, remembered = detached.closed, detached.remembered
        with (
            patch.object(detached, "closed", lambda: closed(path=self.path)),
            patch.object(detached, "remembered", lambda: remembered(path=self.path)),
            patch.object(detached, "terminal_id", return_value=terminal),
            patch.object(detached, "interrupted", return_value=lost or {}),
            patch("builtins.input", return_value=answer),
            redirect_stderr(io.StringIO()) as errors,
            patch("sys.stdout", io.StringIO()),
        ):
            self.errors = errors
            return app.choose_reopen(sessions)

    def test_this_tab_reopens_what_it_last_ran(self) -> None:
        mine, other = session("mine"), session("other")
        self.run_in_tab(41, "mine", "tab")
        self.run_in_tab(42, "other", "tab-2")
        self.assertIs(self.choose([mine, other]), mine)

    def test_this_tab_also_reopens_a_session_it_lost(self) -> None:
        mine = session("mine")
        lost = {"tab": {"session_id": "mine", "at": 1}}
        self.assertIs(self.choose([mine], lost=lost), mine)

    def test_a_session_still_running_in_this_tab_is_not_reopened(self) -> None:
        detached.remember(session("mine"), "claude", 41, 5.0, "tab", path=self.path)
        self.assertIsNone(self.choose([session("mine")]))
        self.assertIn("still running", self.errors.getvalue())

    def test_a_fresh_tab_picks_from_recently_closed_sessions(self) -> None:
        older, newer = session("older"), session("newer")
        self.run_in_tab(41, "older", "a")
        time.sleep(0.01)
        self.run_in_tab(42, "newer", "b")
        self.assertIs(self.choose([older, newer], terminal="new-tab"), newer)
        self.assertIs(self.choose([older, newer], terminal="new-tab", answer="2"), older)

    def test_an_open_session_is_not_offered_from_a_fresh_tab(self) -> None:
        self.run_in_tab(41, "reopened", "a")
        self.assertIsNone(self.choose([session("reopened", pid=31)], terminal=""))

    def test_a_supervisor_remembered_by_its_record_is_found(self) -> None:
        supervisor = session("01a19f75-native")
        supervisor.supervises = "record-1"
        self.run_in_tab(41, "supervisor-record-1", "tab")
        self.assertIs(self.choose([supervisor]), supervisor)

    def test_a_supervisor_reopens_through_its_own_launch(self) -> None:
        supervisor = session("supervisor")
        supervisor.supervises = "record-1"
        with (
            patch.object(app, "make_room", return_value=True),
            patch.object(app, "launch_supervisor", return_value=0) as launched,
            patch.object(app, "launch") as plain,
        ):
            app.open_again(supervisor, app.LaunchConfig(), app.UserState(self.path), [])
        launched.assert_called_once()
        self.assertEqual(launched.call_args.args[0], "record-1")
        plain.assert_not_called()


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
