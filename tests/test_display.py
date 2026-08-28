import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import Browser, Session, UserState, filtered_sessions
from ai_sessions.config import LaunchConfig


class ScriptedScreen:
    def __init__(self, *keys: object, size: tuple[int, int] = (30, 120)) -> None:
        self.keys = list(keys)
        self.size = size
        self.frames: list[str] = []
        self.current: list[str] = []
        self.timeouts: list[int] = []

    def keypad(self, _enabled: bool) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def erase(self) -> None:
        self.current = []

    def addnstr(self, _y: int, _x: int, value: str, length: int, _style: int) -> None:
        self.current.append(value[:length])

    def refresh(self) -> None:
        self.frames.append("\n".join(self.current))

    def get_wch(self) -> object:
        return self.keys.pop(0)

    def timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)


def session(tool: str) -> Session:
    return Session(
        tool,
        f"{tool}-id",
        tool,
        "/work",
        1_700_000_002,
        1_700_000_001,
        "preview",
        True,
        "storage",
    )


class DisplayTests(unittest.TestCase):
    @staticmethod
    def titled_session(
        session_id: str,
        *,
        title: str = "market-anomaly-analysis",
        cwd: str = "/work",
        tool: str = "claude",
        renamed: bool = False,
    ) -> Session:
        return Session(
            tool=tool,
            session_id=session_id,
            title=title,
            cwd=cwd,
            updated=1_700_000_002,
            created=1_700_000_001,
            preview="preview",
            named=True,
            storage="storage",
            renamed=renamed,
        )

    def test_unique_title_is_byte_for_byte_unchanged(self) -> None:
        item = self.titled_session("081c1234567890")
        self.assertEqual(app.title_disambiguators([item]), {})
        self.assertEqual(app.display_list_title(item), "market-anomaly-analysis")

    def test_same_title_same_cwd_keeps_native_ids_out_of_plain_list(self) -> None:
        rows = [
            self.titled_session("081c1234567890"),
            self.titled_session("6f321234567890"),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output(rows)
        rendered = output.getvalue()
        self.assertEqual(rendered.count("market-anomaly-analysis"), 2)
        self.assertNotIn("081c1234", rendered)
        self.assertNotIn("6f321234", rendered)

    def test_json_titles_do_not_receive_rendering_suffixes(self) -> None:
        rows = [
            self.titled_session("081c1234567890"),
            self.titled_session("6f321234567890"),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output(rows, as_json=True)
        payload = json.loads(output.getvalue())
        self.assertEqual([item["title"] for item in payload], ["market-anomaly-analysis"] * 2)

    def test_normal_agent_tag_uses_nickname_without_parent_id(self) -> None:
        item = self.titled_session("child-id")
        item.source = "subagent"
        item.agent_nickname = "Bohr"
        item.parent_id = "parent-native-id"
        self.assertNotIn("parent-native-id", app.display_list_title(item))

        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output([item])
        self.assertIn("[Bohr]", output.getvalue())
        self.assertNotIn("parent-native-id", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output([item], as_json=True)
        self.assertEqual(json.loads(output.getvalue())[0]["parent_id"], "parent-native-id")

    def test_same_title_different_cwd_still_gets_suffixes(self) -> None:
        rows = [
            self.titled_session("081c1234567890", cwd="/one"),
            self.titled_session("6f321234567890", title="MARKET-ANOMALY-ANALYSIS", cwd="/two"),
        ]
        self.assertEqual(
            app.title_disambiguators(rows),
            {
                "claude:081c1234567890": "[081c1234]",
                "claude:6f321234567890": "[6f321234]",
            },
        )

    def test_renamed_title_collision_is_rendering_only(self) -> None:
        rows = [
            self.titled_session("081c1234567890", title="shared name", renamed=True),
            self.titled_session("6f321234567890", title="shared name", renamed=True),
        ]
        self.assertEqual(rows[0].title, "shared name")
        self.assertEqual(app.title_disambiguators(rows)[rows[0].key], "[081c1234]")
        self.assertEqual(rows[0].title, "shared name")

    def test_shared_id_prefix_extends_only_until_unique(self) -> None:
        rows = [
            self.titled_session("12345678a-rest"),
            self.titled_session("12345678b-rest"),
        ]
        labels = app.title_disambiguators(rows)
        self.assertEqual(labels[rows[0].key], "[12345678a]")
        self.assertEqual(labels[rows[1].key], "[12345678b]")

    def test_identical_ids_across_tools_fall_back_to_tool_qualified_ids(self) -> None:
        rows = [
            self.titled_session("shared-id", tool="claude"),
            self.titled_session("shared-id", tool="codex"),
        ]
        labels = app.title_disambiguators(rows)
        self.assertEqual(labels[rows[0].key], "[claude:shared-id]")
        self.assertEqual(labels[rows[1].key], "[codex:shared-id]")

    def test_suffix_remains_visible_with_bounded_title_rendering(self) -> None:
        rendered = app.ellipsize_with_suffix("market-anomaly-analysis", "[081c1234]", 13)
        self.assertEqual(len(rendered), 13)
        self.assertTrue(rendered.endswith("[081c1234]"))

    def test_tui_rows_disambiguate_current_title_collisions(self) -> None:
        rows = [
            self.titled_session("081c1234567890"),
            self.titled_session("6f321234567890"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen(size=(30, 65))
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                Browser(
                    screen,
                    rows,
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                ).draw()
        # The conversation view groups same-project/same-title independent
        # threads and never leaks native IDs into ordinary rows.
        self.assertIn("independent", screen.frames[-1])
        self.assertNotIn("081c1234567890", screen.frames[-1])
        self.assertNotIn("6f321234567890", screen.frames[-1])

    def test_windows_extended_path_prefix_is_hidden(self) -> None:
        # short_path also abbreviates the home directory, so asserting on the
        # whole string made this pass or fail depending on who ran it.
        rendered = app.short_path(r"\\?\C:\Users\vandy\project")
        self.assertNotIn("\\\\?\\", rendered)
        self.assertTrue(rendered.endswith("project"))

    def test_multiple_tools_can_be_filtered_as_one_set(self) -> None:
        rows = [session("codex"), session("claude"), session("opencode")]
        visible = filtered_sessions(rows, tools={"codex", "opencode"}, origin="all")
        self.assertEqual([item.tool for item in visible], ["codex", "opencode"])

    def test_tool_picker_applies_a_visible_multi_select_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen(app.curses.KEY_DOWN, " ", "\n")
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser = Browser(
                    screen,
                    [session("codex"), session("claude"), session("opencode")],
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                )
                browser.tool_picker()
        self.assertEqual(browser.tools, frozenset(("codex", "opencode")))
        self.assertEqual([item.tool for item in browser.current()], ["codex", "opencode"])
        self.assertTrue(any("[ ] Claude Code" in frame for frame in screen.frames))
        self.assertEqual(browser.message, "Showing Codex + OpenCode.")

    def test_tool_picker_prevents_an_empty_filter_and_offers_recovery(self) -> None:
        keys = (
            " ",
            app.curses.KEY_DOWN,
            " ",
            app.curses.KEY_DOWN,
            " ",
            "\n",
            "a",
            "\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen(*keys)
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser = Browser(
                    screen,
                    [session("codex"), session("claude"), session("opencode")],
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                )
                browser.tool_picker()
        self.assertEqual(browser.tools, frozenset(("codex", "claude", "opencode")))
        self.assertTrue(any("Choose at least one tool" in frame for frame in screen.frames))

    def test_draw_is_side_effect_free_and_advertises_tool_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen()
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
                patch("ai_sessions.app.detect_open_sessions") as detect,
            ):
                browser = Browser(
                    screen,
                    [session("codex")],
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                )
                browser.draw()
        detect.assert_not_called()
        self.assertIn("t tools", screen.frames[-1])

    def test_minimum_supported_size_keeps_tool_filter_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen(size=(12, 65))
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser = Browser(
                    screen,
                    [session("codex")],
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                )
                browser.draw()
        self.assertIn("AI SESSIONS", screen.frames[-1])
        self.assertIn("t tools", screen.frames[-1])

    def test_empty_first_paint_distinguishes_loading_from_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen()
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser = Browser(
                    screen,
                    [],
                    UserState(Path(directory) / "state.json"),
                    LaunchConfig(path=Path(directory) / "config.toml"),
                    refresh=object(),
                )
                browser.draw()
        self.assertIn("Loading sessions from installed tools", screen.frames[-1])
        self.assertIn("refreshing", screen.frames[-1])


class ConversationViewTests(unittest.TestCase):
    @staticmethod
    def item(
        session_id: str,
        *,
        title: str = "Topic",
        cwd: str = "/repo",
        updated: float = 1_700_000_002,
        conversation_id: str = "",
        superseded: bool = False,
        diverged: bool = False,
        hidden: bool = False,
    ) -> Session:
        return Session(
            "claude",
            session_id,
            title,
            cwd,
            updated,
            1_700_000_001,
            "latest prompt",
            True,
            "storage/" + session_id,
            conversation_id=conversation_id,
            superseded=superseded,
            diverged=diverged,
            hidden=hidden,
            message_count=4,
        )

    def browser(self, sessions: list[Session], directory: str) -> Browser:
        return Browser(
            ScriptedScreen(),
            sessions,
            UserState(Path(directory) / "state.json"),
            LaunchConfig(path=Path(directory) / "config.toml"),
        )

    def test_tracked_chain_has_one_parent_and_exact_expanded_children(self) -> None:
        old = self.item("old", conversation_id="conv", superseded=True, updated=2)
        head = self.item("head", conversation_id="conv", updated=3)
        rows = app.build_view_rows([old, head])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "lineage head")
        self.assertIs(rows[0].target, head)
        expanded = app.build_view_rows([old, head], expanded={rows[0].row_id})
        self.assertEqual([row.target for row in expanded], [head, old, head])
        self.assertEqual(
            [row.status for row in expanded],
            ["lineage head", "superseded copy", "lineage head"],
        )

    def test_filtered_head_does_not_promote_an_older_visible_child(self) -> None:
        old = self.item("old", conversation_id="conv", superseded=True, updated=2)
        head = self.item("head", conversation_id="conv", updated=3)
        rows = app.build_view_rows([old, head], visible_keys={old.key})
        self.assertEqual(len(rows), 1)
        self.assertEqual([item.key for item in rows[0].members], [old.key])
        self.assertIs(rows[0].target, head)
        self.assertEqual(rows[0].all_members, (old, head))

    def test_equivalent_current_heads_choose_a_deterministic_actionable_target(self) -> None:
        first = self.item("first", conversation_id="conv", updated=2)
        second = self.item("second", conversation_id="conv", updated=3)
        rows = app.build_view_rows([first, second])
        self.assertIs(rows[0].target, second)
        self.assertTrue(rows[0].actionable)

        first.diverged = True
        second.diverged = True
        rows = app.build_view_rows([first, second])
        self.assertIsNone(rows[0].target)
        self.assertFalse(rows[0].actionable)

    def test_independent_group_is_presentation_only_and_projects_do_not_merge(self) -> None:
        first = self.item("one", title=" Topic ", cwd="/one")
        second = self.item("two", title="topic", cwd="/one", updated=3)
        other = self.item("three", title="TOPIC", cwd="/two")
        rows = app.build_view_rows([first, second, other])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].status, "independent thread")
        self.assertIsNone(rows[0].target)
        self.assertEqual([item.key for item in rows[0].members], [first.key, second.key])
        self.assertEqual(rows[1].status, "untracked")
        self.assertIs(rows[1].target, other)

    def test_unknown_cwd_independent_threads_never_group(self) -> None:
        first = self.item("first", cwd="")
        second = self.item("second", cwd=" ")
        rows = app.build_view_rows([first, second])
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.status for row in rows], ["untracked", "untracked"])

    def test_related_tracked_and_independent_threads_share_a_presentation_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "nautilus-ema-backtest"
            harness = root / "harness"
            harness.mkdir(parents=True)
            tracked = self.item("tracked", title="Nautilus", cwd=str(root), conversation_id="conv")
            first = self.item("first", title="Nautilus", cwd=str(harness))
            second = self.item("second", title="Nautilus", cwd=str(harness))

            collapsed = app.build_view_rows([tracked, first, second])
            self.assertEqual(len(collapsed), 1)
            self.assertTrue(collapsed[0].row_id.startswith("project:"))
            self.assertIsNone(collapsed[0].target)
            self.assertEqual(collapsed[0].project, str(root))

            expanded = app.build_view_rows([tracked, first, second], expanded={collapsed[0].row_id})
            self.assertEqual(len(expanded), 4)
            self.assertIsNone(expanded[0].target)
            self.assertEqual([row.target for row in expanded[1:]], [tracked, first, second])
            self.assertEqual(
                [row.row_id for row in expanded[1:]],
                [
                    "conversation:conv",
                    "session:claude:first",
                    "session:claude:second",
                ],
            )

    def test_unknown_project_container_cannot_be_created_by_same_title(self) -> None:
        first = self.item("first", title="Nautilus", cwd="")
        second = self.item("second", title="Nautilus", cwd="")
        rows = app.build_view_rows([first, second])
        self.assertFalse(any(row.row_id.startswith("project:") for row in rows))

    def test_normal_rows_have_human_collision_labels_but_no_native_ids(self) -> None:
        first = self.item("native-one", cwd="/one")
        second = self.item("native-two", cwd="/two")
        rows = app.build_view_rows([first, second])
        self.assertTrue(all(row.collision_label.startswith("[") for row in rows))
        self.assertNotIn("native-one", rows[0].collision_label)
        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser([first, second], directory)
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser.draw()
            frame = browser.screen.frames[-1]
        self.assertNotIn("native-one", frame)
        self.assertNotIn("native-two", frame)

    def test_status_and_compatibility_activity_label_are_exact(self) -> None:
        head = self.item("head", conversation_id="conv")
        old = self.item("old", conversation_id="conv", superseded=True)
        branch = self.item("branch", conversation_id="fork", diverged=True)
        self.assertEqual(app.conversation_status(head), "lineage head")
        self.assertEqual(app.conversation_status(old), "superseded copy")
        self.assertEqual(app.conversation_status(branch), "diverged branch")
        self.assertEqual(app.conversation_status(self.item("free")), "untracked")
        self.assertEqual(app.activity_label(self.item("free")), "0t 0c 4p")

    def test_details_show_full_native_identity_and_filtered_members(self) -> None:
        old = self.item("old-native", conversation_id="conv", superseded=True)
        head = self.item("head-native", conversation_id="conv")
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen("x")
            browser = Browser(
                screen,
                [old, head],
                UserState(Path(directory) / "state.json"),
                LaunchConfig(path=Path(directory) / "config.toml"),
            )
            browser.query = "head-native"
            with (
                patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
                patch.object(app.curses, "curs_set"),
            ):
                browser.details()
        details = screen.frames[-1]
        self.assertIn("CONVERSATION DETAILS", details)
        self.assertIn("Native ID head-native", details)
        self.assertIn("Storage    storage/head-native", details)
        self.assertIn("filtered/hidden", details)

    def test_enter_on_independent_group_only_expands(self) -> None:
        first = self.item("one")
        second = self.item("two", updated=3)
        with tempfile.TemporaryDirectory() as directory:
            screen = ScriptedScreen("\n", "q")
            browser = Browser(
                screen,
                [first, second],
                UserState(Path(directory) / "state.json"),
                LaunchConfig(path=Path(directory) / "config.toml"),
            )
            self.assertIsNone(browser.run())
        self.assertEqual(len(browser.view_rows()), 3)
        self.assertIsNone(browser.view_rows()[0].target)

    def test_project_focus_toggles_back_to_all_projects(self) -> None:
        first = self.item("one", cwd="/one")
        second = self.item("two", cwd="/two")
        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser([first, second], directory)
            browser.focus_selected_project()
            self.assertEqual(browser.project_focus, "/one")
            self.assertEqual([item.key for item in browser.current()], [first.key])
            browser.focus_selected_project()
            self.assertEqual(browser.project_focus, "")
            self.assertEqual(len(browser.current()), 2)

    def test_f_focuses_a_synthetic_project_group_at_its_common_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            child = root / "harness"
            child.mkdir(parents=True)
            tracked = self.item("tracked", title="Topic", cwd=str(root), conversation_id="conv")
            independent = self.item("independent", title="Topic", cwd=str(child))
            browser = self.browser([tracked, independent], directory)

            self.assertIsNone(browser.view_rows()[0].target)
            browser.focus_selected_project()
            self.assertEqual(browser.project_focus, str(root))
            self.assertEqual(
                {item.key for item in browser.current()}, {tracked.key, independent.key}
            )

    def test_disappearing_expanded_child_restores_parent_selection(self) -> None:
        old = self.item("old", conversation_id="conv", superseded=True)
        head = self.item("head", conversation_id="conv", updated=3)
        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser([old, head], directory)
            parent_id = browser.view_rows()[0].row_id
            browser.expanded.add(parent_id)
            browser.selected = 1

            class Refresh:
                def take(self, *, wait: bool = False):
                    return [head], (), ""

            browser.refresh = Refresh()
            self.assertTrue(browser.apply_refresh())
            self.assertEqual(browser.selected_id(), parent_id)
            self.assertIs(browser.selected_target(), head)

    def test_filtering_an_expanded_child_restores_its_parent_not_a_sibling(self) -> None:
        old = self.item("old", conversation_id="conv", superseded=True)
        head = self.item("head", conversation_id="conv", updated=3)
        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser([old, head], directory)
            parent_id = browser.view_rows()[0].row_id
            browser.expanded.add(parent_id)
            child_id = browser.view_rows()[1].row_id
            browser.selected = 1
            browser.query = "head"
            browser.keep_selection(child_id)
            self.assertEqual(browser.selected_id(), parent_id)

    def test_same_title_component_build_does_not_compare_every_row_pair(self) -> None:
        rows = [
            self.item(str(index), title="Same title", cwd=f"/unrelated/project-{index}")
            for index in range(1_000)
        ]
        app.project_identity.cache_clear()
        with patch.object(app, "_projects_related", wraps=app._projects_related) as related:
            built = app.build_view_rows(rows)
        self.assertEqual(len(built), len(rows))
        self.assertLessEqual(related.call_count, len(rows))


class StripExtendedPrefixTests(unittest.TestCase):
    def test_drive_prefix_removed(self) -> None:
        self.assertEqual(
            app.strip_extended_prefix(r"\\?\C:\Users\vandy\project"),
            r"C:\Users\vandy\project",
        )

    def test_unc_prefix_restores_share_form(self) -> None:
        self.assertEqual(
            app.strip_extended_prefix(r"\\?\UNC\server\share"),
            r"\\server\share",
        )

    def test_ordinary_paths_unchanged(self) -> None:
        for value in (r"C:\Users\vandy", "/home/vandy/project", "", r"\\server\share"):
            self.assertEqual(app.strip_extended_prefix(value), value)


if __name__ == "__main__":
    unittest.main()
