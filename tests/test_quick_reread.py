"""A search's rows are reread first, and come out exactly as a full refresh lists them."""

import curses
import dataclasses
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import Browser, Reread, Session, UserState
from ai_sessions.config import LaunchConfig
from ai_sessions.discovery import HarnessContext
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.harnesses import codex as codex_harness
from ai_sessions.model import NativeRef

try:
    from test_compressed_discovery import (
        AGENT_ID,
        CLAUDE_ID,
        CODEX_ID,
        CompressedDiscoveryBase,
        claude_records,
        codex_records,
        jsonl,
    )
    from test_display import ScriptedScreen
except ImportError:  # run as tests.test_quick_reread
    from tests.test_compressed_discovery import (
        AGENT_ID,
        CLAUDE_ID,
        CODEX_ID,
        CompressedDiscoveryBase,
        claude_records,
        codex_records,
        jsonl,
    )
    from tests.test_display import ScriptedScreen

UNREGISTERED = "01a02502-5225-7121-a4e8-869df0686982"


def as_dicts(sessions) -> dict[str, dict]:
    return {session.session_id: dataclasses.asdict(session) for session in sessions}


class RediscoverMatchesDiscoverTests(CompressedDiscoveryBase):
    def setUp(self) -> None:
        super().setUp()
        (self.project / f"{CLAUDE_ID}.jsonl").write_bytes(claude_records())
        agents = self.project / CLAUDE_ID / "subagents"
        agents.mkdir(parents=True)
        (agents / f"agent-{AGENT_ID}.jsonl").write_bytes(
            jsonl(
                {
                    "type": "user",
                    "isSidechain": True,
                    "agentId": AGENT_ID,
                    "sessionId": CLAUDE_ID,
                    "message": {"content": "do the thing"},
                }
            )
        )
        rollout = self.codex_day / f"rollout-2026-01-01T00-00-00-{CODEX_ID}.jsonl"
        rollout.write_bytes(codex_records())
        self.write_codex_db(rollout)
        meta = {
            "type": "session_meta",
            "payload": {"id": UNREGISTERED, "timestamp": "2026-01-01T00:00:00Z", "cwd": "/x"},
        }
        unregistered = self.codex_day / f"rollout-2026-01-01T00-00-00-{UNREGISTERED}.jsonl"
        unregistered.write_bytes(jsonl(meta) + codex_records())

    def check(self, module) -> None:
        full = module.discover(HarnessContext.create(use_cache=False))
        self.assertGreaterEqual(len(full), 2)
        refs = [NativeRef(item.session_id, item.storage) for item in full]
        with patch.object(module.DiscoveryCache, "save") as saved:
            again = module.rediscover(HarnessContext.create(use_cache=False), refs)
        self.assertEqual(as_dicts(again), as_dicts(full))
        saved.assert_not_called()
        one = module.rediscover(HarnessContext.create(use_cache=False), refs[:1])
        self.assertEqual(list(as_dicts(one).values()), [as_dicts(full)[refs[0].session_id]])

    def test_claude_sessions_and_subagents(self) -> None:
        self.check(claude_harness)

    def test_codex_threads_and_a_rollout_missing_from_the_database(self) -> None:
        self.check(codex_harness)

    def test_a_session_whose_file_is_gone_is_left_out(self) -> None:
        refs = [NativeRef("gone", str(self.project / "gone.jsonl"))]
        self.assertEqual(claude_harness.rediscover(HarnessContext.create(), refs), [])
        missing = (
            self.codex_day
            / "rollout-2026-01-01T00-00-00-01a00000-0000-7000-8000-000000000000.jsonl"
        )
        codex_refs = [NativeRef("01a00000-0000-7000-8000-000000000000", str(missing))]
        self.assertEqual(codex_harness.rediscover(HarnessContext.create(), codex_refs), [])

    def test_a_reread_does_not_declare_the_index_complete(self) -> None:
        context = HarnessContext.create(use_cache=False)
        refs = [NativeRef(CLAUDE_ID, str(self.project / f"{CLAUDE_ID}.jsonl"))]
        claude_harness.rediscover(context, refs)
        self.assertNotIn("claude", context.complete_native_indexes)


def item(session_id: str, **fields) -> Session:
    return Session(
        "claude",
        session_id,
        fields.pop("title", "match " + session_id),
        "",
        fields.pop("updated", 1_700_000_002),
        1_700_000_001,
        "latest prompt",
        True,
        "storage/" + session_id,
        **fields,
    )


class Pending:
    def take(self, *, wait: bool = False):
        return None, (), ""


class Waiting(ScriptedScreen):
    """Times out like curses does while a refresh runs, until told to stop."""

    def get_wch(self):
        key = super().get_wch()
        if key == "wait":
            time.sleep(0.02)
            raise curses.error()
        return key


class BrowserQuickRereadTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = UserState(self.root / "state.json")

    def browser(self, sessions, *keys) -> Browser:
        browser = Browser(
            Waiting(*keys),
            sessions,
            self.state,
            LaunchConfig(path=self.root / "config.toml"),
            refresh=Pending(),
        )
        browser.query = "match"
        return browser

    def fresh(self, key: str, title: str) -> Reread:
        values = {name: getattr(item("x"), name) for name in app._REREAD_FIELDS}
        values.update(title=title, original_title=title, provider_title=title)
        return Reread(fields={key: values}, open={key: (True, 77)}, statuses={})

    def test_the_searched_rows_are_brought_up_to_date_before_the_refresh(self) -> None:
        stale = item("a", title="match stale")
        other = item("b", title="unrelated")
        browser = self.browser([stale, other], *(["wait"] * 20), "\x1b", "\x1b")
        with patch.object(
            app, "reread_sessions", return_value=self.fresh(stale.key, "match fresh")
        ) as reread:
            browser.run()
        targets = reread.call_args.args[0]
        self.assertEqual([entry.key for entry in targets], [stale.key])
        self.assertEqual(stale.title, "match fresh")
        self.assertEqual((stale.is_open, stale.open_pid), (True, 77))
        # The fresh title is in the rows the table draws.
        titles = {row.title for row in browser.view_rows()}
        self.assertIn("match fresh", titles)
        self.assertNotIn("match stale", titles)

    def test_a_name_given_in_sessions_survives_the_reread(self) -> None:
        stale = item("a", title="match stale")
        self.state.names[self.state.stable_key(stale.key)] = "match my name"
        self.state.local_names.add(self.state.stable_key(stale.key))
        browser = self.browser([stale], *(["wait"] * 20), "\x1b", "\x1b")
        with patch.object(app, "reread_sessions", return_value=self.fresh(stale.key, "match new")):
            browser.run()
        self.assertEqual(stale.title, "match my name")

    def test_a_reread_that_lost_the_race_to_the_refresh_is_dropped(self) -> None:
        stale = item("a", title="match stale")
        browser = self.browser([stale])
        with patch.object(app, "reread_sessions", return_value=self.fresh(stale.key, "match new")):
            browser.start_quick_reread()
            while not browser.quick.ready():
                time.sleep(0.01)
        browser.sessions = [item("a", title="match from the refresh")]
        self.assertFalse(browser.apply_quick_reread())
        self.assertEqual(browser.sessions[0].title, "match from the refresh")

    def test_without_a_search_nothing_is_reread(self) -> None:
        browser = self.browser([item("a")])
        browser.query = ""
        with patch.object(app, "reread_sessions") as reread:
            browser.start_quick_reread()
        self.assertIsNone(browser.quick)
        reread.assert_not_called()

    def test_a_search_matching_too_much_is_left_to_the_refresh(self) -> None:
        many = [item(f"s{index}") for index in range(app.QUICK_REREAD_LIMIT + 5)]
        browser = self.browser(many)
        browser.origin = "all"
        with patch.object(app, "reread_sessions") as reread:
            browser.start_quick_reread()
        self.assertIsNone(browser.quick)
        reread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
