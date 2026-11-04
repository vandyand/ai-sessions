import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import (
    SUPERVISED_GLYPH,
    SUPERVISOR_GLYPH,
    Browser,
    Session,
    UserState,
    filtered_sessions,
)
from ai_sessions.config import LaunchConfig
from ai_sessions.supervisor import store
from ai_sessions.supervisor.create import create_supervisor, default_supervisor_tool
from ai_sessions.supervisor.index import TargetIndex
from ai_sessions.supervisor.store import load_hook_state

try:
    from test_display import ScriptedScreen
except ImportError:  # run as tests.test_supervisor_browser
    from tests.test_display import ScriptedScreen


def healthy_status(self: UserState, conversation_id: str) -> dict:
    """Treat every conversation as healthy, with all members as heads (no files needed)."""
    members = self.conversations[conversation_id]["members"]
    return {
        "conflict": False,
        "heads": list(members.items()),
        "advanced": [],
        "unavailable": [],
        "unstable": [],
        "unknown": [],
    }


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "supervisors"
        for patcher in (
            patch.object(store, "SUPERVISORS_DIR", self.root),
            patch.object(TargetIndex, "build", return_value=7),
            patch.object(TargetIndex, "extend", return_value=None),
            patch.object(app, "detect_open_sessions"),
            patch.object(UserState, "_conversation_status", healthy_status),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = UserState(self.dir / "state.json")
        self.config = LaunchConfig(path=self.dir / "config.toml")
        self.target = Session(
            "codex",
            "target-id",
            "Fix the build",
            str(self.dir),
            1_700_000_020,
            1_700_000_010,
            "p",
            True,
            "storage",
        )
        self.sessions = [self.target]
        self.state.apply(self.sessions)

    def supervise(self) -> tuple[str, Session]:
        """Create a supervisor for the target and return its record id and session."""
        record_id = create_supervisor(self.state, self.target, "claude", save=False)
        native = load_hook_state(record_id).native_session_id
        supervisor = Session(
            "claude",
            native,
            "generated",
            str(self.dir),
            1_700_000_030,
            1_700_000_011,
            "q",
            False,
            "storage2",
        )
        self.sessions.append(supervisor)
        self.state.apply(self.sessions)
        return record_id, supervisor

    def browser(self, *keys: object, origin: str = "human", size=(60, 220)) -> Browser:
        browser = Browser(ScriptedScreen(*keys, size=size), self.sessions, self.state, self.config)
        browser.origin = origin
        return browser

    def select(self, browser: Browser, session: Session) -> None:
        for index, row in enumerate(browser.view_rows()):
            if row.representative is session:
                browser.selected = index
                return
        self.fail("row not visible")


class SupervisorBrowserTests(Fixture):
    def test_supervisor_sessions_get_origin_supervisor_and_hidden_from_human_view(self) -> None:
        _record, supervisor = self.supervise()
        self.assertEqual(supervisor.origin, "supervisor")
        self.assertEqual(app.ORIGIN_ORDER, ("human", "cross", "agent", "supervisor", "all"))
        self.assertIn("supervisor", app.ORIGIN_LABELS)
        human = filtered_sessions(self.sessions)
        self.assertEqual([item.key for item in human], [self.target.key])
        shown = filtered_sessions(self.sessions, origin="supervisor")
        self.assertEqual([item.key for item in shown], [supervisor.key])

    def test_supervisor_title_has_suffix_and_does_not_follow_target_rename(self) -> None:
        _record, supervisor = self.supervise()
        self.assertEqual(supervisor.title, "Supervisor: Fix the build")
        self.assertNotIn(supervisor.key, self.state.names)
        self.state.names[self.target.key] = "Renamed target"
        self.state.apply(self.sessions)
        self.assertEqual(self.target.title, "Renamed target")
        self.assertEqual(supervisor.title, "Supervisor: Fix the build")
        self.state.names[supervisor.key] = "My watcher"
        self.state.apply(self.sessions)
        self.assertEqual(supervisor.title, "My watcher")

    def test_once_its_harness_has_the_name_the_harness_name_is_shown(self) -> None:
        # After publication a rename made inside the harness must not be overridden.
        record_id, supervisor = self.supervise()
        self.state.supervisors[record_id]["published_title"] = "Supervisor: Fix the build"
        supervisor.title = supervisor.original_title = "Renamed in Codex"
        supervisor.provider_title = "Renamed in Codex"
        self.state.apply(self.sessions)
        self.assertEqual(supervisor.title, "Renamed in Codex")
        self.assertEqual(supervisor.origin, "supervisor")

    def test_rows_show_supervised_supervisor_and_unsupervised_states(self) -> None:
        self.supervise()
        plain = Session(
            "codex", "plain", "Plain task", str(self.dir), 1_700_000_015, 1, "p", True, "storage5"
        )
        self.sessions.append(plain)
        self.state.apply(self.sessions)
        browser = self.browser("i", "x", "q")
        browser.draw()
        frame = browser.screen.frames[-1]
        self.assertNotEqual(SUPERVISED_GLYPH, SUPERVISOR_GLYPH)
        self.assertIn(SUPERVISED_GLYPH + " Fix the build", frame)
        self.assertIn(SUPERVISOR_GLYPH + " Supervisor: Fix the build", frame)
        self.assertNotIn(SUPERVISED_GLYPH + " Plain task", frame)
        self.assertNotIn(SUPERVISOR_GLYPH + " Plain task", frame)
        self.assertIn("Plain task", frame)
        browser.run()
        self.assertIn("supervised by", "\n".join(browser.screen.frames))
        other = self.browser("i", "x", "q", origin="supervisor")
        other.run()
        self.assertIn("supervises 'Fix the build'", "\n".join(other.screen.frames))

    def test_human_view_lists_supervisor_directly_under_its_target(self) -> None:
        _record, supervisor = self.supervise()
        other = Session(
            "codex", "other", "Other", str(self.dir), 1_700_000_015, 1, "p", True, "storage3"
        )
        self.sessions.append(other)
        self.state.apply(self.sessions)
        rows = self.browser().view_rows()
        self.assertEqual([row.representative for row in rows], [self.target, supervisor, other])

    def test_supervisor_row_follows_the_target_when_the_supervisor_sorts_first(self) -> None:
        _record, supervisor = self.supervise()
        supervisor.updated = self.target.updated + 1000
        rows = self.browser(origin="all").view_rows()
        self.assertEqual([row.representative for row in rows], [self.target, supervisor])

    def test_supervisor_row_comes_after_the_expanded_copies_of_the_target(self) -> None:
        record_id, supervisor = self.supervise()
        copy = Session(
            "claude", "copy", "Fix the build", str(self.dir), 1_700_000_001, 1, "p", True, "s4"
        )
        copy.conversation_id = "conversation"
        self.target.conversation_id = "conversation"
        self.sessions.append(copy)
        self.state.supervisors[record_id]["target_conversation"] = "conversation"
        self.state.apply(self.sessions)
        for item in (copy, self.target):
            item.conversation_id = "conversation"
        self.state._apply_supervisors(self.sessions)
        browser = self.browser()
        top = browser.view_rows()[0]
        self.assertTrue(top.expandable)
        browser.expanded.add(top.row_id)
        rows = browser.view_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1].representative, supervisor)

    def test_superseded_copy_of_a_supervised_session_has_no_circle(self) -> None:
        self.supervise()
        self.target.superseded = True
        browser = self.browser()
        browser.draw()
        self.assertNotIn(SUPERVISED_GLYPH + " Fix the build", browser.screen.frames[-1])

    def test_human_search_keeps_the_pair_together(self) -> None:
        _record, supervisor = self.supervise()
        browser = self.browser()
        browser.query = "Fix"
        self.assertEqual(
            [row.representative for row in browser.view_rows()], [self.target, supervisor]
        )

    def test_reset_supervisor_is_not_listed_under_its_target(self) -> None:
        record_id, supervisor = self.supervise()
        self.state.supervisors[record_id]["archived"] = True
        self.state.apply(self.sessions)
        self.assertTrue(supervisor.hidden)
        rows = self.browser().view_rows()
        self.assertEqual([row.representative for row in rows], [self.target])

    def test_super_view_still_lists_only_supervisors(self) -> None:
        _record, supervisor = self.supervise()
        rows = self.browser(origin="supervisor").view_rows()
        self.assertEqual([row.representative for row in rows], [supervisor])

    def test_details_pane_names_the_role_of_the_selected_row(self) -> None:
        _record, supervisor = self.supervise()
        browser = self.browser()
        browser.draw()
        self.assertIn("MAIN SESSION", browser.screen.frames[-1])
        self.select(browser, supervisor)
        browser.draw()
        frame = browser.screen.frames[-1]
        self.assertIn("SUPERVISOR of 'Fix the build'", frame)

    def test_S_on_target_with_supervisor_returns_supervise_action(self) -> None:
        self.supervise()
        result = self.browser("S").run()
        self.assertEqual(result, ("supervise", self.target))

    def test_S_on_target_without_supervisor_opens_harness_picker_defaulting_to_opposite(
        self,
    ) -> None:
        expected = default_supervisor_tool(self.target.tool, self.config)
        self.assertNotEqual(expected, self.target.tool)
        browser = self.browser("S", "\n")
        with patch(
            "ai_sessions.supervisor.create.create_supervisor", return_value="rid"
        ) as created:
            result = browser.run()
        self.assertEqual(result, ("supervise", self.target))
        self.assertIn("CHOOSE A HARNESS", "\n".join(browser.screen.frames))
        self.assertEqual(created.call_args.args[2], expected)

    def test_S_on_supervisor_selects_target_and_never_offers_take_over_when_open(self) -> None:
        self.supervise()
        self.target.is_open = True
        self.target.open_pid = 4242
        browser = self.browser("S", "q", origin="supervisor")
        self.state.apply(self.sessions)
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(Browser, "confirm_take_over") as take_over,
        ):
            self.select(browser, self.sessions[1])
            result = browser.run()
        self.assertIsNone(result)
        take_over.assert_not_called()
        self.assertIn("Ctrl+Tab", browser.screen.frames[-1])
        self.assertEqual(browser.selected_row().representative, self.target)

    def test_S_on_supervisor_with_closed_target_launches_target(self) -> None:
        self.supervise()
        browser = self.browser("S", origin="supervisor")
        self.select(browser, self.sessions[1])
        self.assertEqual(browser.run(), ("launch", self.target))

    def test_reset_archives_record_and_hides_old_supervisor(self) -> None:
        record_id, supervisor = self.supervise()
        browser = self.browser("i", "R", "y", "S", "\n")
        result = browser.run()
        self.assertEqual(result, ("supervise", self.target))
        self.assertTrue(self.state.supervisors[record_id]["archived"])
        self.assertTrue(load_hook_state(record_id).reset)
        self.assertTrue(supervisor.hidden)
        fresh = self.state.supervisor_for_conversation(self.target.conversation_id)
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh, record_id)

    def test_reset_can_be_cancelled(self) -> None:
        record_id, _supervisor = self.supervise()
        self.browser("i", "R", "n", "q").run()
        self.assertFalse(self.state.supervisors[record_id]["archived"])

    def test_help_lists_S(self) -> None:
        browser = self.browser("?", "x", "q")
        browser.run()
        help_frame = next(f for f in browser.screen.frames if "AI SESSIONS — HELP" in f)
        self.assertIn("supervisor", help_frame)
        self.assertTrue(any(line.startswith("S ") for line in help_frame.splitlines()))


if __name__ == "__main__":
    unittest.main()
