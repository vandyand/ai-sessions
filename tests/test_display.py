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

    def test_same_title_same_cwd_gets_short_id_suffixes_in_plain_list(self) -> None:
        rows = [
            self.titled_session("081c1234567890"),
            self.titled_session("6f321234567890"),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output(rows)
        rendered = output.getvalue()
        self.assertIn("market-anoma… [081c1234]", rendered)
        self.assertIn("market-anoma… [6f321234]", rendered)

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
        self.assertIn("[081c1234]", screen.frames[-1])
        self.assertIn("[6f321234]", screen.frames[-1])

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
