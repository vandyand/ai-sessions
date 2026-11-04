"""What keeps the browser and the refresh fast without making either less exact."""

import curses
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app, fastjson, housekeeping, jsonl_tracking
from ai_sessions.app import Browser, Session, SessionRefresh, UserState
from ai_sessions.config import LaunchConfig
from ai_sessions.discovery import prune_entries, same_inode, stat_key, walk_files
from ai_sessions.harnesses import codex
from ai_sessions.liveness import LivenessContext, ProcessInfo

try:
    from test_display import ScriptedScreen
except ImportError:  # run as tests.test_browser_speed
    from tests.test_display import ScriptedScreen


def item(session_id: str, title: str, **fields) -> Session:
    return Session(
        "claude",
        session_id,
        title,
        fields.pop("cwd", "/work/" + session_id),
        fields.pop("updated", 1_700_000_002),
        1_700_000_001,
        "latest prompt",
        True,
        "storage/" + session_id,
        **fields,
    )


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def browser(self, sessions, *keys, refresh=None) -> Browser:
        return Browser(
            ScriptedScreen(*keys),
            sessions,
            UserState(self.root / "state.json"),
            LaunchConfig(path=self.root / "config.toml"),
            refresh=refresh,
        )


class RowCacheTests(Folder):
    def sessions(self) -> list[Session]:
        return [item(f"s{index}", f"topic {index} alpha") for index in range(40)]

    def test_moving_the_cursor_does_not_rebuild_the_rows(self) -> None:
        browser = self.browser(self.sessions(), *([curses.KEY_DOWN] * 10), "\x1b")
        with patch.object(app, "build_view_rows", wraps=app.build_view_rows) as built:
            browser.run()
        # Once for the first frame; never again for ten moves.
        self.assertEqual(built.call_count, 1)
        self.assertEqual(browser.selected, 10)

    def test_hiding_a_row_shows_at_once(self) -> None:
        sessions = self.sessions()
        browser = self.browser(sessions, "h", "y", "\x1b", "\x1b")
        first = browser.view_rows()[0].target
        browser.run()
        self.assertTrue(first.hidden)
        self.assertNotIn(first.key, {row.target.key for row in browser.view_rows() if row.target})
        self.assertNotIn(first.title, browser.screen.frames[-1])

    def test_a_narrowed_search_matches_a_fresh_one(self) -> None:
        sessions = self.sessions() + [item("open", "alpha open", is_open=True, open_pid=1)]
        for query in ("al", "alp", "alpha", "alpha 3", "alpha is:", "alpha is:open"):
            with self.subTest(query=query):
                browser = self.browser(sessions)
                browser.origin = "all"
                browser._rows_memo = True
                for end in range(1, len(query) + 1):
                    browser.query = query[:end]
                    narrowed = browser.current()
                expected = app.filtered_sessions(
                    sessions,
                    tools=browser.tools,
                    query=query,
                    origin="all",
                    visibility=browser.visibility,
                    sort_mode=browser.sort_mode,
                    compression=browser.compression,
                )
                self.assertEqual([entry.key for entry in narrowed], [e.key for e in expected])

    def test_search_text_follows_a_rename(self) -> None:
        session = item("r", "before")
        self.assertIn("before", app.session_searchable(session))
        session.title = "after"
        self.assertIn("after", app.session_searchable(session))
        self.assertNotIn("before", app.session_searchable(session))

    def test_waiting_for_a_refresh_does_not_redraw_an_unchanged_screen(self) -> None:
        class Pending:
            def take(self, *, wait: bool = False):
                return None, (), ""

        class TimingOut(ScriptedScreen):
            def get_wch(self):
                key = super().get_wch()
                if key == "timeout":
                    raise curses.error()
                return key

        browser = Browser(
            TimingOut(*(["timeout"] * 30), "\x1b", "\x1b"),
            self.sessions(),
            UserState(self.root / "state.json"),
            LaunchConfig(path=self.root / "config.toml"),
            refresh=Pending(),
        )
        browser.run()
        self.assertLessEqual(len(browser.screen.frames), 3)


class RefreshSaveTests(Folder):
    def test_the_result_is_ready_before_the_catalog_is_written(self) -> None:
        release = threading.Event()
        writing = threading.Event()

        def slow_save(*_args, **_kwargs):
            writing.set()
            release.wait(5)

        with (
            patch.object(app, "load_sessions", return_value=[item("a", "a")]),
            patch.object(app, "save_session_catalog", side_effect=slow_save),
        ):
            refresh = SessionRefresh(
                use_cache=True,
                state=UserState(self.root / "state.json"),
                config=LaunchConfig(path=self.root / "config.toml"),
                catalog_path=self.root / "catalog.json",
            )
            sessions, _notes, error = refresh.take(wait=True)
            self.assertEqual((len(sessions), error), (1, ""))
            self.assertTrue(writing.wait(5))
            self.assertFalse(refresh.wait_saved(0))
            waiter = threading.Thread(target=app.finish_catalog_saves)
            waiter.start()
            waiter.join(0.2)
            self.assertTrue(waiter.is_alive())
            release.set()
            waiter.join(5)
        self.assertTrue(refresh.wait_saved(0))

    def test_a_refresh_only_tidies_when_asked(self) -> None:
        with (
            patch.object(app, "load_sessions", return_value=[]),
            patch.object(app, "save_session_catalog"),
            patch("ai_sessions.housekeeping.sweep_temporary_files") as swept,
        ):
            SessionRefresh(
                use_cache=True,
                state=UserState(self.root / "state.json"),
                config=LaunchConfig(path=self.root / "config.toml"),
            ).wait_saved(5)
        swept.assert_not_called()


class StateLockTests(Folder):
    def test_applying_waits_for_whoever_holds_the_state(self) -> None:
        state = UserState(self.root / "state.json")
        applied = threading.Event()
        with patch.object(UserState, "_apply", side_effect=lambda *_a, **_k: applied.set()):
            with state.lock:
                worker = threading.Thread(target=state.apply, args=([],))
                worker.start()
                self.assertFalse(applied.wait(0.2))
            worker.join(5)
        self.assertTrue(applied.is_set())


class ListingTests(Folder):
    def test_a_listing_stat_answers_like_a_real_one(self) -> None:
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "one.jsonl").write_text("{}\n", encoding="utf-8")
        (self.root / "other.txt").write_text("x", encoding="utf-8")
        found, stats = walk_files(self.root, lambda name: name.endswith(".jsonl"))
        self.assertEqual(found, [nested / "one.jsonl"])
        listed, real = stats[stat_key(found[0])], os.stat(found[0])
        self.assertEqual(
            (listed.st_size, listed.st_mtime_ns, listed.st_ctime_ns),
            (real.st_size, real.st_mtime_ns, real.st_ctime_ns),
        )

    def test_a_missing_inode_is_unknown_not_different(self) -> None:
        class Stat:
            def __init__(self, inode: int) -> None:
                self.st_ino = inode

        self.assertTrue(same_inode({"inode": 5}, Stat(0)))
        self.assertTrue(same_inode({"inode": 0}, Stat(5)))
        self.assertTrue(same_inode({"inode": 5}, Stat(5)))
        self.assertFalse(same_inode({"inode": 5}, Stat(6)))

    def test_only_files_gone_from_the_walked_tree_are_pruned(self) -> None:
        kept, gone = self.root / "kept.jsonl", self.root / "gone.jsonl"
        outside = self.root.parent / "elsewhere.jsonl"
        entries = {str(kept): {}, str(gone): {}, str(outside): {}}
        self.assertTrue(prune_entries(entries, self.root, [kept]))
        self.assertEqual(set(entries), {str(kept), str(outside)})
        self.assertFalse(prune_entries(entries, self.root, [kept]))


class FileTests(Folder):
    def test_files_read_the_same_without_orjson(self) -> None:
        value = {"name": "café ☕", "items": [1, 2.5, None, True], "nested": {"a": "b"}}
        path = self.root / "value.json"
        fastjson.write_atomic(path, value, indent=True, newline=True)
        with patch.object(fastjson, "_orjson", None):
            self.assertEqual(fastjson.read(path), value)
            fastjson.write_atomic(path, value)
        self.assertEqual(fastjson.read(path), value)

    def test_a_failed_write_leaves_no_temporary_file(self) -> None:
        path = self.root / "value.json"
        with (
            patch.object(Path, "replace", side_effect=PermissionError("busy")),
            patch.object(fastjson, "_REPLACE_DELAY", 0),
        ):
            with self.assertRaises(PermissionError):
                fastjson.write_atomic(path, {"a": 1})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_temporary_files_of_running_processes_are_kept(self) -> None:
        mine = self.root / f"cache.json.tmp-{os.getpid()}-1"
        dead = self.root / "cache.tmp-4194303"
        other = self.root / "cache.json"
        for path in (mine, dead, other):
            path.write_text("x", encoding="utf-8")
        with patch.object(housekeeping, "_alive", side_effect=lambda pid: pid == os.getpid()):
            removed = housekeeping.sweep_temporary_files([self.root])
        self.assertEqual(removed, [dead])
        self.assertTrue(mine.exists() and other.exists())

    def test_an_unchanged_location_is_not_written_again(self) -> None:
        with (
            patch.object(jsonl_tracking, "_locations", {}),
            patch.object(jsonl_tracking, "LOCATIONS_FILE", self.root / "locations.json"),
            patch.object(fastjson, "write_atomic") as written,
        ):
            jsonl_tracking._save_location("k", {"offset": 1})
            jsonl_tracking._save_location("k", {"offset": 1})
            jsonl_tracking._save_location("k", {"offset": 2})
        self.assertEqual(written.call_count, 2)


class CodexStartTimeWarningTests(unittest.TestCase):
    def test_a_shell_mentioning_codex_is_not_an_unverified_codex(self) -> None:
        shell = ProcessInfo(5, "bash.exe", ("bash.exe", "-c", "codex.exe resume"), "", 0.0)
        context = LivenessContext("win32", (shell,), {})
        with (
            tempfile.TemporaryDirectory() as home,
            patch.object(codex, "record_warning") as warned,
        ):
            codex._windows_liveness(context, Path(home), set())
        warned.assert_not_called()

    def test_a_codex_process_without_a_start_time_still_warns(self) -> None:
        process = ProcessInfo(6, "codex.exe", ("codex.exe",), "", 0.0)
        context = LivenessContext("win32", (process,), {})
        with (
            tempfile.TemporaryDirectory() as home,
            patch.object(codex, "record_warning") as warned,
        ):
            codex._windows_liveness(context, Path(home), set())
        warned.assert_called_once()


class CodexMetaCacheTests(Folder):
    def test_an_unregistered_rollout_is_opened_once(self) -> None:
        path = self.root / "rollout-2026-11-04T00-00-00-0000.jsonl"
        path.write_text('{"type":"session_meta","payload":{"id":"x","cwd":"/w"}}\n', "utf-8")
        cache = codex.DiscoveryCache(codex.HarnessContext.create(use_cache=False))
        cache.entries[str(path)] = {}
        real = path.stat()
        meta = cache.rollout_meta(path, real)
        cache.remember_rollout_meta(path, real, meta)
        with patch.object(codex, "_rollout_meta", side_effect=AssertionError("read")):
            self.assertEqual(cache.rollout_meta(path, real), {"id": "x", "cwd": "/w"})


if __name__ == "__main__":
    unittest.main()
