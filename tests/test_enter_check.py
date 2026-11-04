"""Enter opens a session after checking only what it depends on, not every session."""

import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import Browser, Session, SessionRefresh, UserState
from ai_sessions.config import LaunchConfig

try:
    from test_display import ScriptedScreen
except ImportError:  # run as tests.test_enter_check
    from tests.test_display import ScriptedScreen


def item(session_id: str, **fields) -> Session:
    return Session(
        "claude",
        session_id,
        fields.pop("title", "Topic " + session_id),
        "",
        fields.pop("updated", 1_700_000_002),
        1_700_000_001,
        "latest prompt",
        True,
        "storage/" + session_id,
        **fields,
    )


class Unfinished:
    """A refresh that is still running unless something waits for it."""

    def __init__(self) -> None:
        self.waited = False
        self.result: list[Session] = []

    def take(self, *, wait: bool = False):
        if not wait:
            return None, (), ""
        self.waited = True
        return self.result, (), ""


def status(heads: list[str]) -> dict:
    return {
        "heads": [(key, {}) for key in heads],
        "conflict": False,
        "unknown": [],
        "unstable": [],
        "unavailable": [],
        "compressed": [],
    }


class EnterTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = UserState(self.root / "state.json")
        self.detected: list[list[str]] = []

    def detect(self, items, **_kwargs):
        self.detected.append(sorted(entry.session_id for entry in items))
        return True

    def enter(self, sessions: list[Session], *keys: object) -> tuple[object, Unfinished]:
        refresh = Unfinished()
        refresh.result = sessions
        browser = Browser(
            ScriptedScreen("\n", *keys),
            sessions,
            self.state,
            LaunchConfig(path=self.root / "config.toml"),
            refresh=refresh,
        )
        browser.origin = "all"
        with patch.object(app, "detect_open_sessions", side_effect=self.detect):
            selected = browser.run()
        return selected, refresh

    def conversation(self, *sessions: Session, recorded: dict | None = None) -> None:
        members = {entry.key: {"storage": entry.storage} for entry in sessions}
        members.update(recorded or {})
        self.state.conversations["conv"] = {"members": members, "history": []}
        for entry in sessions:
            entry.conversation_id = "conv"

    def test_a_plain_session_opens_without_waiting(self) -> None:
        chosen = item("plain")
        selected, refresh = self.enter([chosen])
        self.assertIs(selected, chosen)
        self.assertFalse(refresh.waited)
        self.assertIn(["plain"], self.detected)

    def test_the_copy_that_became_current_since_the_list_was_saved_is_opened(self) -> None:
        older = item("older", updated=1_700_000_100)
        newer = item("newer", updated=1_700_000_200)
        self.conversation(older, newer)
        # The saved list still thinks the older copy is current.
        newer.superseded = True
        with patch.object(UserState, "_conversation_status", return_value=status([newer.key])):
            selected, refresh = self.enter([older, newer])
        self.assertIs(selected, newer)
        self.assertFalse(refresh.waited)
        self.assertIn(["newer", "older"], self.detected)

    def test_a_copy_the_list_has_never_seen_waits_for_the_refresh(self) -> None:
        known = item("known")
        unseen = self.root / "unseen.jsonl"
        unseen.write_text("{}\n", encoding="utf-8")
        self.conversation(known, recorded={"claude:unseen": {"storage": str(unseen)}})
        with patch.object(UserState, "_conversation_status", return_value=status([known.key])):
            _selected, refresh = self.enter([known])
        self.assertTrue(refresh.waited)

    def test_a_copy_whose_file_is_gone_is_no_reason_to_wait(self) -> None:
        known = item("known")
        gone = str(self.root / "gone.jsonl")
        self.conversation(known, recorded={"claude:gone": {"storage": gone}})
        with patch.object(UserState, "_conversation_status", return_value=status([known.key])):
            selected, refresh = self.enter([known])
        self.assertIs(selected, known)
        self.assertFalse(refresh.waited)

    def test_a_supervisors_target_is_checked_with_it(self) -> None:
        supervisor = item("sup", supervises="record")
        target = item("target", supervised_by="record")
        with patch.object(Browser, "view_rows", autospec=True) as rows:
            rows.side_effect = lambda browser: (
                SimpleNamespace(
                    row_id="r",
                    all_members=(supervisor,),
                    target=supervisor,
                    expandable=False,
                    representative=supervisor,
                    members=(supervisor,),
                ),
            )
            browser = Browser(
                ScriptedScreen(),
                [supervisor, target],
                self.state,
                LaunchConfig(path=self.root / "config.toml"),
            )
            with patch.object(app, "detect_open_sessions", side_effect=self.detect):
                self.assertTrue(browser.check_before_open(browser.view_rows()[0]))
        self.assertEqual(self.detected, [["sup", "target"]])

    def test_a_session_found_open_elsewhere_is_not_opened_blind(self) -> None:
        chosen = item("busy")

        def detect(items, **_kwargs):
            for entry in items:
                entry.is_open, entry.open_pid = True, 4242
            return True

        browser = Browser(
            ScriptedScreen("\n", "\x1b", "\x1b"),
            [chosen],
            self.state,
            LaunchConfig(path=self.root / "config.toml"),
            refresh=Unfinished(),
        )
        with (
            patch.object(app, "detect_open_sessions", side_effect=detect),
            patch.object(app, "IS_WINDOWS", True),
            patch.object(Browser, "confirm_take_over", return_value=None) as asked,
        ):
            self.assertIsNone(browser.run())
        asked.assert_called_once_with(chosen)

    def test_a_failing_check_falls_back_to_the_refresh(self) -> None:
        chosen = item("plain")
        with patch.object(Browser, "check_before_open", side_effect=RuntimeError("boom")):
            selected, refresh = self.enter([chosen])
        self.assertIs(selected, chosen)
        self.assertTrue(refresh.waited)


class OutlivingRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_refresh_left_behind_a_launch_stops_nothing(self) -> None:
        gate = threading.Event()

        def slow_load(**_kwargs):
            gate.wait(5)
            return [item("a")]

        with (
            patch.object(app, "load_sessions", side_effect=slow_load),
            patch.object(app, "save_session_catalog") as saved,
            patch("ai_sessions.detached.stop_detached") as stopped,
        ):
            refresh = SessionRefresh(
                use_cache=True,
                state=UserState(self.root / "state.json"),
                config=LaunchConfig(path=self.root / "config.toml"),
                catalog_path=self.root / "catalog.json",
                stop_detached=True,
            )
            refresh.outlive_browser()
            gate.set()
            refresh.wait()
        stopped.assert_not_called()
        saved.assert_called_once()

    def test_main_launches_holding_the_state_and_waits_for_the_refresh(self) -> None:
        chosen = item("plain")
        state = UserState(self.root / "state.json")
        seen: dict[str, object] = {}

        class Refresh(Unfinished):
            def __init__(self, *_args, **_kwargs) -> None:
                super().__init__()
                self.result = [chosen]
                self.cleanup = True
                seen["refresh"] = self

            def outlive_browser(self) -> None:
                self.cleanup = False

            def wait(self) -> None:
                seen["waited_after"] = "launched" in seen

        def fake_open_again(selected, _config, launched_state, _others, _dry_run):
            probe = threading.Thread(
                target=lambda: seen.update(free=launched_state.lock.acquire(timeout=0.1))
            )
            probe.start()
            probe.join()
            seen["launched"] = selected is chosen
            return 0

        with (
            patch.object(app, "UserState", return_value=state),
            patch.object(app.LaunchConfig, "load", return_value=LaunchConfig(path=self.root / "c")),
            patch.object(app, "load_session_catalog", return_value=[chosen]),
            patch.object(app, "SessionRefresh", Refresh),
            patch.object(app, "detect_open_sessions", return_value=True),
            patch.object(app, "open_again", side_effect=fake_open_again),
            patch.object(app, "start_background_sweep"),
            patch.object(app.curses, "wrapper", side_effect=lambda f: f(ScriptedScreen("\n"))),
            patch.object(app.sys.stdin, "isatty", return_value=True),
            patch.object(app.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(app._main([]), 0)
        self.assertEqual(seen["launched"], True)
        self.assertEqual(seen["free"], False)
        self.assertTrue(seen["waited_after"])
        self.assertFalse(seen["refresh"].cleanup)


class ReopenTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = UserState(self.root / "state.json")

    def reopen(self, catalog, closed, scanned_sessions=None, code=0):
        opened: list = []
        scanned: list = []

        def fake_open_again(chosen, _config, _state, others, _dry_run):
            opened.append((chosen.session_id, len(list(others))))
            return 0

        def fake_scan(**_kwargs):
            scanned.append(True)
            return catalog if scanned_sessions is None else scanned_sessions

        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app, "UserState", return_value=self.state),
            patch.object(app.LaunchConfig, "load", return_value=LaunchConfig(path=self.root / "c")),
            patch.object(app, "load_session_catalog", return_value=catalog),
            patch.object(app, "load_sessions", side_effect=fake_scan),
            patch.object(app, "save_session_catalog"),
            patch.object(app, "detect_open_sessions", return_value=True),
            patch.object(app, "open_again", side_effect=fake_open_again),
            patch("ai_sessions.detached.terminal_id", return_value="tab"),
            patch("ai_sessions.detached.closed", return_value=closed),
            patch("ai_sessions.detached.interrupted", return_value={}),
            patch("ai_sessions.detached.remembered", return_value={}),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(app._main(["--reopen"]), code)
        return opened, scanned

    def test_a_session_in_the_saved_list_reopens_without_a_scan(self) -> None:
        opened, scanned = self.reopen(
            [item("mine"), item("other")], {"tab": {"session_id": "mine"}}
        )
        self.assertEqual(opened, [("mine", 2)])
        self.assertEqual(scanned, [])

    def test_a_session_started_since_the_list_was_saved_is_found_by_a_scan(self) -> None:
        opened, scanned = self.reopen(
            [item("other")],
            {"tab": {"session_id": "new"}},
            scanned_sessions=[item("other"), item("new")],
        )
        self.assertEqual(scanned, [True])
        self.assertEqual(opened, [("new", 2)])

    def test_a_tab_that_ran_nothing_falls_back_to_the_scan(self) -> None:
        opened, scanned = self.reopen([item("mine")], {}, code=1)
        self.assertEqual((opened, scanned), ([], [True]))


if __name__ == "__main__":
    unittest.main()
