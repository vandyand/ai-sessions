import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.config import LaunchConfig

try:
    from test_display import ScriptedScreen
except ImportError:
    from tests.test_display import ScriptedScreen


class LatestSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.state = app.UserState(Path(self.temp) / "state.json")
        self.old = app.Session(
            "claude",
            "old",
            "Elevate",
            self.temp,
            1700000000,
            1699990000,
            "",
            True,
            "old-storage",
            conversation_id="conv",
            diverged=True,
        )
        self.new = replace(
            self.old,
            tool="codex",
            session_id="new",
            updated=1700400000,
            storage="new-storage",
            launch_targets={"codex": "new"},
        )

    def test_collapsed_divergent_conversation_targets_latest_copy(self):
        row = app.build_view_rows([self.old, self.new])[0]
        self.assertIs(row.target, self.new)
        self.assertIs(row.representative, self.new)
        self.assertTrue(row.actionable)

    def test_default_launch_uses_fresh_metadata_not_old_checkpoint_timestamps(self):
        records = [
            (
                self.old.key,
                dict(tool=self.old.tool, session_id=self.old.session_id, updated=1800000000),
            ),
            (
                self.new.key,
                dict(tool=self.new.tool, session_id=self.new.session_id, updated=1600000000),
            ),
        ]
        self.state.session_conversations = {self.old.key: "conv", self.new.key: "conv"}
        self.state.conversations = {"conv": {"members": dict(records)}}
        self.state._sessions_by_key = {item.key: item for item in (self.old, self.new)}
        status = dict(
            conflict=True,
            heads=records,
            advanced=records,
            unknown=[],
            unstable=[],
            unavailable=[],
            compressed=[],
        )
        with patch.object(self.state, "_conversation_status", return_value=status):
            source, target, cid = self.state.resolve_launch(self.old, "codex")
        self.assertIs(source, self.new)
        self.assertIs(target, self.new)
        self.assertEqual(cid, "conv")
        self.assertEqual(len(self.state.conversations["conv"]["members"]), 2)

    def test_expanded_copy_selection_retains_exact_native_intent(self):
        screen = ScriptedScreen("\n", size=(30, 100))
        browser = app.Browser(
            screen, [self.old, self.new], self.state, LaunchConfig(path=Path(self.temp) / "config")
        )
        group = browser.view_rows()[0]
        browser.expanded.add(group.row_id)
        browser.invalidate_rows()
        browser.keep_selection(group.row_id + "/member:" + self.old.key)
        with patch.object(app.curses, "curs_set"):
            chosen = browser.run()
        self.assertIs(chosen, self.old)
        self.assertTrue(browser.exact_native_selection)

    def test_early_enter_refreshes_divergent_copy_timestamps_before_default_selection(self):
        class Pending:
            waited = False

            def take(self, *, wait=False):
                self.waited = self.waited or wait
                return None, (), ""

        refresh = Pending()
        browser = app.Browser(
            ScriptedScreen("\n"),
            [self.old, self.new],
            self.state,
            LaunchConfig(path=Path(self.temp) / "config"),
            refresh=refresh,
        )
        fresh = app.Reread(
            fields={
                self.old.key: {"updated": self.new.updated + 100},
                self.new.key: {"updated": self.new.updated},
            },
            open={},
            statuses={},
        )
        with (
            patch.object(app, "check_before_open", return_value=True),
            patch.object(app, "reread_sessions", return_value=fresh),
            patch.object(app.curses, "curs_set"),
        ):
            chosen = browser.run()
        self.assertIs(chosen, self.old)
        self.assertFalse(refresh.waited)
        self.assertFalse(browser.exact_native_selection)

    def test_incomplete_recency_reread_requests_full_refresh(self):
        browser = app.Browser(
            ScriptedScreen(),
            [self.old, self.new],
            self.state,
            LaunchConfig(path=Path(self.temp) / "config"),
        )
        fresh = app.Reread(
            fields={self.old.key: {"updated": self.old.updated}}, open={}, statuses={}
        )
        with (
            patch.object(app, "check_before_open", return_value=True),
            patch.object(app, "reread_sessions", return_value=fresh),
        ):
            self.assertFalse(browser.check_before_open(browser.view_rows()[0]))

    def test_browser_main_passes_explicit_native_selection_through_launch(self):
        chosen = self.new
        state = self.state

        class SelectedBrowser:
            def __init__(self, *args, **kwargs):
                self.sessions = [chosen]
                self.refresh = None
                self.exact_native_selection = True

            def run(self):
                return chosen

        with (
            patch.object(app, "UserState", return_value=state),
            patch.object(
                app.LaunchConfig, "load", return_value=LaunchConfig(path=Path(self.temp) / "config")
            ),
            patch.object(app, "load_session_catalog", return_value=[chosen]),
            patch.object(app, "SessionRefresh"),
            patch.object(app, "Browser", SelectedBrowser),
            patch.object(app.curses, "wrapper", side_effect=lambda f: f(object())),
            patch.object(app.sys.stdin, "isatty", return_value=True),
            patch.object(app.sys.stdout, "isatty", return_value=True),
            patch.object(app, "open_again", return_value=0) as opened,
        ):
            self.assertEqual(app._main([]), 0)
        self.assertTrue(opened.call_args.kwargs.get("exact_native"))
