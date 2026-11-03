import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_sessions import app, retention
from ai_sessions.app import UserState
from ai_sessions.compression import CompressionError
from ai_sessions.config import LaunchConfig
from ai_sessions.model import Session

DAY = 86_400.0
NOW = 1_800_000_000.0


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.opens = self.root / "opens.jsonl"
        for name, value in (
            ("OPENS_FILE", self.opens),
            ("LOCK_FILE", self.root / "sweep.lock"),
            ("LAST_RUN_FILE", self.root / "sweep.last"),
            ("LOG_FILE", self.root / "retention.log"),
        ):
            patcher = patch.object(retention, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, name: str, *, age_days: float = 60.0, **kw) -> Session:
        path = self.root / f"{name}.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        moment = NOW - age_days * DAY
        os.utime(path, (moment, moment))
        return Session("codex", name, name, "/w", moment, moment - 1, "p", True, str(path), **kw)


class IdleRuleTests(Fixture):
    def idle(self, *sessions: Session, days: int = 30) -> list[str]:
        return [item.session_id for item in retention.idle_sessions(sessions, days, now=NOW)]

    def test_old_sessions_qualify_and_recent_ones_do_not(self) -> None:
        self.assertEqual(
            self.idle(self.session("old"), self.session("recent", age_days=29.9)), ["old"]
        )

    def test_exactly_the_limit_qualifies(self) -> None:
        self.assertEqual(self.idle(self.session("edge", age_days=30.0)), ["edge"])

    def test_the_transcript_time_counts_when_updated_is_older(self) -> None:
        item = self.session("touched")
        recent = NOW - DAY
        os.utime(item.storage, (recent, recent))
        self.assertEqual(self.idle(item), [])

    def test_a_recent_open_counts(self) -> None:
        item = self.session("opened")
        retention.note_opened(item, now=NOW - DAY)
        self.assertEqual(self.idle(item), [])

    def test_hidden_sessions_qualify(self) -> None:
        item = self.session("hidden", hidden=True)
        self.assertEqual(self.idle(item), ["hidden"])

    def test_every_origin_qualifies(self) -> None:
        item = self.session("agent", origin="agent")
        self.assertEqual(self.idle(item), ["agent"])

    def test_open_compressed_and_unsupported_sessions_are_skipped(self) -> None:
        opened = self.session("open", is_open=True)
        packed = self.session("packed", compressed=True)
        foreign = self.session("foreign")
        foreign.tool = "opencode"
        self.assertEqual(self.idle(opened, packed, foreign), [])

    def test_a_subagent_follows_a_recently_opened_parent(self) -> None:
        parent = self.session("parent")
        child = self.session("child", parent_id="parent")
        retention.note_opened(parent, now=NOW - DAY)
        self.assertEqual(self.idle(parent, child), [])

    def test_a_subagent_follows_an_open_parent(self) -> None:
        parent = self.session("parent", age_days=1, is_open=True)
        child = self.session("child", parent_id="parent")
        self.assertEqual(self.idle(child, parent), [])

    def test_a_recent_subagent_keeps_its_parent(self) -> None:
        parent = self.session("parent")
        child = self.session("child", age_days=1, parent_id="parent")
        self.assertEqual(self.idle(parent, child), [])

    def test_an_open_subagent_keeps_its_parent(self) -> None:
        parent = self.session("parent")
        child = self.session("child", parent_id="parent", is_open=True)
        self.assertEqual(self.idle(parent, child), [])

    def test_nested_subagents_share_the_family(self) -> None:
        top = self.session("top", age_days=1)
        middle = self.session("middle", parent_id="top")
        bottom = self.session("bottom", parent_id="middle")
        self.assertEqual(self.idle(bottom, middle, top), [])

    def test_an_idle_family_qualifies_together(self) -> None:
        parent = self.session("parent")
        child = self.session("child", parent_id="parent")
        self.assertEqual(set(self.idle(parent, child)), {"parent", "child"})

    def test_a_subagent_whose_parent_is_gone_stands_alone(self) -> None:
        self.assertEqual(self.idle(self.session("child", parent_id="gone")), ["child"])

    def test_a_parent_cycle_does_not_hang(self) -> None:
        one = self.session("one", parent_id="two")
        two = self.session("two", parent_id="one")
        self.assertEqual(set(self.idle(one, two)), {"one", "two"})

    def test_another_harness_with_the_same_id_is_not_family(self) -> None:
        other = self.session("parent", age_days=1)
        other.tool = "claude"
        child = self.session("child", parent_id="parent")
        self.assertEqual(self.idle(other, child), ["child"])

    def test_a_damaged_open_log_is_ignored(self) -> None:
        self.opens.write_text('not json\n{"key": 5}\n\n', encoding="utf-8")
        self.assertEqual(self.idle(self.session("old")), ["old"])


class ConfigTests(unittest.TestCase):
    def load(self, text: str) -> LaunchConfig:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return LaunchConfig.load(path)

    def test_default_is_thirty(self) -> None:
        self.assertEqual(self.load("").compress_after_days, 30)

    def test_value_and_off(self) -> None:
        self.assertEqual(self.load("[retention]\ncompress_after_days = 90").compress_after_days, 90)
        self.assertEqual(self.load("[retention]\ncompress_after_days = 0").compress_after_days, 0)

    def test_invalid_values_keep_the_default(self) -> None:
        for value in ("-1", "true", '"7"', "1.5"):
            config = self.load(f"[retention]\ncompress_after_days = {value}")
            self.assertEqual(config.compress_after_days, 30, value)

    def test_saving_the_config_keeps_the_setting(self) -> None:
        config = self.load("[retention]\ncompress_after_days = 90")
        config.set_mode("safe")
        config.save()
        self.assertEqual(LaunchConfig.load(config.path).compress_after_days, 90)


class ActivityTests(Fixture):
    def test_decompressing_records_an_open(self) -> None:
        item = self.session("old")
        retention.compress_session(item)
        retention.decompress_session(item)
        self.assertIn(item.key, retention.last_opened())

    def test_launching_records_an_open(self) -> None:
        item = self.session("launched")
        item.cwd = str(self.root)
        config = LaunchConfig(path=self.root / "config.toml")
        with (
            patch.object(app, "make_room", return_value=True),
            patch.object(app, "run_harness", return_value=0),
            patch.object(app, "record_launch"),
            patch.object(app, "os") as fake_os,
        ):
            fake_os.chdir = lambda path: None
            fake_os.environ = os.environ
            fake_os.name = os.name
            app.launch(item, config, False, None, others=[])
        self.assertIn(item.key, retention.last_opened())

    def test_a_dry_run_records_nothing(self) -> None:
        item = self.session("dry")
        item.cwd = str(self.root)
        config = LaunchConfig(path=self.root / "config.toml")
        with contextlib.redirect_stdout(io.StringIO()):
            app.launch(item, config, True, None, others=[])
        self.assertEqual(retention.last_opened(), {})


class LiveSweepTests(Fixture):
    def test_a_session_found_open_during_the_run_is_skipped(self) -> None:
        item = self.session("old")
        path = Path(item.storage)

        def found_open(sessions) -> bool:
            sessions[0].is_open = True
            return True

        result = retention.run_sweep([item], 30, now=NOW, refresh=found_open)
        self.assertEqual(result.compressed, 0)
        self.assertTrue(path.is_file())

    def test_unknown_liveness_compresses_nothing(self) -> None:
        item = self.session("old")
        result = retention.run_sweep([item], 30, now=NOW, refresh=lambda sessions: False)
        self.assertEqual(result.compressed, 0)
        self.assertTrue(Path(item.storage).is_file())
        self.assertIn("open", result.failures[0])

    def test_a_long_run_checks_liveness_again(self) -> None:
        first = self.session("first")
        second = self.session("second")
        calls = []

        def refresh(sessions) -> bool:
            calls.append(len(sessions))
            if len(calls) > 1:
                second.is_open = True
            return True

        with patch.object(retention, "LIVENESS_SECONDS", -1.0):
            result = retention.run_sweep([first, second], 30, now=NOW, refresh=refresh)
        self.assertEqual(result.compressed, 1)
        self.assertGreater(len(calls), 1)
        self.assertTrue(Path(second.storage).is_file())

    def test_opening_a_family_during_its_compression_stops_it(self) -> None:
        parent = self.session("parent")

        def opened_midway(item, *, still_idle):
            retention.note_opened(item, now=NOW)
            self.assertFalse(still_idle())
            raise CompressionError("active")

        with patch.object(retention, "compress_session", opened_midway):
            result = retention.run_sweep([parent], 30, now=NOW)
        self.assertEqual(result.compressed, 0)

    def test_a_dry_run_does_not_need_liveness(self) -> None:
        item = self.session("old")
        result = retention.run_sweep(
            [item], 30, dry_run=True, now=NOW, refresh=lambda sessions: False
        )
        self.assertEqual(result.compressed, 1)


class UnknownLivenessTests(Fixture):
    def test_compress_command_refuses(self) -> None:
        item = self.session("old")
        parsed = app.build_parser().parse_args(["--compress", "old"])
        self.assertEqual(parsed.compress, "old")
        with (
            patch.object(app, "load_sessions", return_value=[item]),
            patch.object(app, "save_session_catalog"),
            patch.object(app, "sessions_are_known", return_value=False),
            patch.object(app.sys, "stderr", io.StringIO()) as stderr,
            patch.object(app.sys, "stdout", io.StringIO()),
        ):
            code = app.main(["--compress", "old"])
        self.assertEqual(code, 2)
        self.assertIn("open", stderr.getvalue())
        self.assertTrue(Path(item.storage).is_file())

    def test_detection_reports_failure(self) -> None:
        item = self.session("old")
        with patch.object(app, "detect_open_sessions", side_effect=RuntimeError("x")):
            self.assertFalse(app.sessions_are_known([item]))
        with patch.object(app, "detect_open_sessions", return_value=False):
            self.assertFalse(app.sessions_are_known([item]))


class FakeTerminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class EntryPointTests(unittest.TestCase):
    """The compress commands must see live sessions, so they cannot use the browser's catalog."""

    def run_main(self, *flags: str) -> tuple[Mock, Mock, Mock]:
        with (
            patch.object(app.sys, "stdin", FakeTerminal()),
            patch.object(app.sys, "stdout", FakeTerminal()),
            patch.object(app.sys, "stderr", FakeTerminal()),
            patch.object(app, "load_session_catalog", return_value=[]) as catalog,
            patch.object(app, "load_sessions", return_value=[]) as live,
            patch.object(app, "save_session_catalog"),
            patch.object(app, "SessionRefresh"),
            patch.object(app, "compress_idle", return_value=0),
            patch.object(app, "start_background_sweep") as sweep,
            patch.object(app.curses, "wrapper", return_value=None),
        ):
            app.main(list(flags))
        return catalog, live, sweep

    def test_terminal_commands_load_live_sessions(self) -> None:
        for flags in (
            ("--compress-idle",),
            ("--compress-idle", "--dry-run"),
            ("--compress", "x"),
            ("--decompress", "x"),
        ):
            catalog, live, sweep = self.run_main(*flags)
            catalog.assert_not_called()
            live.assert_called()
            sweep.assert_not_called()

    def test_a_dry_run_of_the_browser_starts_no_sweep(self) -> None:
        catalog, _, sweep = self.run_main("--dry-run")
        catalog.assert_called()
        sweep.assert_not_called()

    def test_the_browser_starts_the_sweep(self) -> None:
        with patch.object(LaunchConfig, "load", return_value=LaunchConfig()):
            _, _, sweep = self.run_main()
        sweep.assert_called_once()


class SweepTests(Fixture):
    def test_compresses_idle_sessions_and_reports(self) -> None:
        old = self.session("old")
        recent = self.session("recent", age_days=1)
        result = retention.run_sweep([old, recent], 30, now=NOW)
        self.assertEqual(result.compressed, 1)
        self.assertTrue(old.compressed)
        self.assertFalse(recent.compressed)
        self.assertGreater(result.bytes_before, 0)

    def test_hidden_sessions_stay_hidden(self) -> None:
        item = self.session("hidden", hidden=True)
        retention.run_sweep([item], 30, now=NOW)
        self.assertTrue(item.compressed)
        self.assertTrue(item.hidden)

    def test_dry_run_changes_nothing(self) -> None:
        item = self.session("old")
        result = retention.run_sweep([item], 30, dry_run=True, now=NOW)
        self.assertEqual(result.compressed, 1)
        self.assertFalse(item.compressed)
        self.assertTrue(Path(item.storage).is_file())

    def test_one_failure_does_not_stop_the_run(self) -> None:
        first, second = self.session("first"), self.session("second")
        real = retention.compress_session

        def fail_first(item, **_kw):
            if item is first:
                raise CompressionError("first is stuck")
            real(item)

        with patch.object(retention, "compress_session", fail_first):
            result = retention.run_sweep([first, second], 30, now=NOW)
        self.assertEqual((result.compressed, len(result.failures)), (1, 1))
        self.assertTrue(second.compressed)
        self.assertIn("first is stuck", retention.LOG_FILE.read_text(encoding="utf-8"))

    def test_session_opened_during_the_run_is_skipped(self) -> None:
        first, second = self.session("first"), self.session("second")
        real = retention.compress_session

        def open_the_other(item, **_kw):
            real(item)
            retention.note_opened(second, now=NOW)

        with patch.object(retention, "compress_session", open_the_other):
            result = retention.run_sweep([first, second], 30, now=NOW)
        self.assertEqual(result.compressed, 1)
        self.assertFalse(second.compressed)

    def test_session_written_during_the_run_is_skipped(self) -> None:
        first, second = self.session("first"), self.session("second")
        real = retention.compress_session

        def touch_the_other(item, **_kw):
            real(item)
            os.utime(second.storage, (NOW, NOW))

        with patch.object(retention, "compress_session", touch_the_other):
            retention.run_sweep([first, second], 30, now=NOW)
        self.assertFalse(second.compressed)


class LockTests(Fixture):
    def test_only_one_holder(self) -> None:
        self.assertTrue(retention.acquire_lock())
        self.assertFalse(retention.acquire_lock())
        retention.release_lock()
        self.assertTrue(retention.acquire_lock())
        retention.release_lock()

    def test_concurrent_starts_give_one_winner(self) -> None:
        wins: list[bool] = []
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            wins.append(retention.acquire_lock())

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(wins), 1)
        retention.release_lock()

    def test_lock_of_a_dead_process_is_taken_over(self) -> None:
        child = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
        )
        retention.LOCK_FILE.write_text(child.stdout.strip(), encoding="utf-8")
        self.assertTrue(retention.acquire_lock())
        retention.release_lock()

    def test_old_lock_is_taken_over(self) -> None:
        retention.LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        long_ago = time.time() - 13 * 3600
        os.utime(retention.LOCK_FILE, (long_ago, long_ago))
        self.assertTrue(retention.acquire_lock())
        retention.release_lock()

    def test_garbled_lock_is_taken_over(self) -> None:
        retention.LOCK_FILE.write_text("?", encoding="utf-8")
        self.assertTrue(retention.acquire_lock())
        retention.release_lock()


class BackgroundStartTests(Fixture):
    def test_starts_hidden_once_then_waits_six_hours(self) -> None:
        with patch.object(retention.subprocess, "Popen") as popen:
            self.assertTrue(retention.start_background_sweep(now=NOW))
            self.assertFalse(retention.start_background_sweep(now=NOW + 5 * 3600))
            self.assertTrue(retention.start_background_sweep(now=NOW + 7 * 3600))
        self.assertEqual(popen.call_count, 2)
        arguments, options = popen.call_args
        self.assertEqual(arguments[0][1:], ["-m", "ai_sessions", "--compress-idle"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_a_held_lock_starts_nothing(self) -> None:
        retention.LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        with patch.object(retention.subprocess, "Popen") as popen:
            self.assertFalse(retention.start_background_sweep(now=NOW))
        popen.assert_not_called()

    def test_a_failed_start_is_quiet(self) -> None:
        with patch.object(retention.subprocess, "Popen", side_effect=OSError("no")):
            self.assertFalse(retention.start_background_sweep(now=NOW))


class CommandTests(Fixture):
    def run_main(self, *arguments: str, config: str = "") -> tuple[int, str]:
        path = self.root / "config.toml"
        path.write_text(config, encoding="utf-8")
        output = io.StringIO()
        state = UserState(self.root / "state.json")
        with (
            patch.object(app, "load_sessions", return_value=self.sessions),
            patch.object(app, "save_session_catalog") as self.saved,
            patch.object(app, "UserState", return_value=state),
            patch.object(app, "CONFIG_FILE", path, create=True),
            patch.object(app.LaunchConfig, "load", return_value=LaunchConfig.load(path)),
            contextlib.redirect_stdout(output),
        ):
            code = app.main(["--no-cache", *arguments])
        return code, output.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.old = self.session("old", age_days=4000)
        self.sessions = [self.old]

    def test_compress_idle_compresses_and_saves_the_list(self) -> None:
        code, text = self.run_main("--compress-idle")
        self.assertEqual(code, 0)
        self.assertTrue(self.old.compressed)
        self.assertIn("1 session", text)
        self.saved.assert_called()

    def test_dry_run(self) -> None:
        code, text = self.run_main("--compress-idle", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.old.compressed)
        self.assertIn("would compress 1", text)

    def test_turned_off(self) -> None:
        code, text = self.run_main(
            "--compress-idle", config="[retention]\ncompress_after_days = 0\n"
        )
        self.assertEqual(code, 0)
        self.assertFalse(self.old.compressed)
        self.assertIn("off", text)

    def test_second_run_while_locked_exits_quietly(self) -> None:
        retention.LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        code, text = self.run_main("--compress-idle")
        self.assertEqual(code, 0)
        self.assertFalse(self.old.compressed)
        self.assertIn("already running", text)


if __name__ == "__main__":
    unittest.main()
