import curses
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


class HandleRefreshTests(unittest.TestCase):
    def test_refresh_inherits_before_projection_and_preserves_filtered_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = app.UserState(root / "state.json")
            target = app.Session(
                "codex",
                "target",
                "ordinary",
                str(root / "target"),
                2,
                1,
                "",
                True,
                "log",
                conversation_id="target-conversation",
            )
            sup = replace(
                target,
                session_id="sup",
                title="Supervisor",
                cwd=str(root / "uuid"),
                conversation_id="supervisor-conversation",
                supervises="record",
            )
            state.supervisors = {
                "record": {"target_conversation": target.conversation_id, "cwd": target.cwd}
            }
            items = [target, sup]
            state.handles.ensure(items)
            state._inherit_supervisor_projects(items)
            browser = app.Browser(
                ScriptedScreen(), items, state, LaunchConfig(path=root / "config.toml")
            )
            browser.origin = "all"
            browser._rows_memo = True
            browser.query = sup.handle
            browser.keep_selection()
            previous = browser.selected_id()
            assert previous
            for project in ("one", "two"):
                # Exact old handles leave the filter; a role search survives
                # the next rename and must retain the same selected row.
                browser.query = sup.handle if project == "one" else "sup"
                browser.keep_selection(previous)
                state.handles.add_project(project, root / project)
                state.handles.assign(replace(target), project)
                with patch.object(app, "load_sessions", side_effect=AssertionError("discovery")):
                    self.assertTrue(browser.refresh_handles())
                self.assertEqual(sup.project_handle, project)
                self.assertEqual(target.project_handle, project)
                if project == "one":
                    self.assertEqual(browser.view_rows(), ())
                else:
                    self.assertEqual(browser.selected_id(), previous)
                    self.assertEqual(
                        browser.view_rows()[browser.selected].representative.handle, sup.handle
                    )
                browser.query = sup.handle
                browser.keep_selection(previous)
                self.assertEqual(browser.selected_id(), previous)
                self.assertFalse(browser.refresh_handles())

    def test_menu_reads_external_handle_changes_without_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            state = app.UserState(Path(root) / "state.json")
            item = app.Session(
                "codex", "fixture", "Worker", root, 1_700_000_002, 1_700_000_001, "", True, "log"
            )
            state.handles.ensure([item])
            browser = app.Browser(
                ScriptedScreen(), [item], state, LaunchConfig(path=Path(root) / "config.toml")
            )
            original = item.handle
            state.handles.set_session(item, "chosen-worker")
            item.handle = original
            with patch.object(app, "load_sessions", side_effect=AssertionError("discovery")):
                self.assertTrue(browser.refresh_handles())
            self.assertEqual(item.handle, "chosen-worker")
            with patch.object(
                state.handles, "metadata_for", side_effect=AssertionError("unchanged read")
            ):
                self.assertFalse(browser.refresh_handles())

    def test_idle_menu_notices_handle_change_after_initial_refresh_finished(self):
        with tempfile.TemporaryDirectory() as root:
            state = app.UserState(Path(root) / "state.json")
            item = app.Session(
                "codex", "fixture", "Worker", root, 1_700_000_002, 1_700_000_001, "", True, "log"
            )
            state.handles.ensure([item])

            class Updating(ScriptedScreen):
                first = True

                def get_wch(self):
                    if self.first:
                        self.first = False
                        old = item.handle
                        state.handles.set_session(item, "live-worker")
                        item.handle = old
                        raise curses.error()
                    return "\x1b"

            browser = app.Browser(
                Updating(), [item], state, LaunchConfig(path=Path(root) / "config.toml")
            )
            browser.run()
            self.assertEqual(item.handle, "live-worker")
