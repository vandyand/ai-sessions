import curses
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.config import LaunchConfig

try:
    import test_display as display
except ImportError:
    from tests import test_display as display


class PositionedScreen(display.ScriptedScreen):
    def erase(self):
        super().erase()
        height, width = self.size
        self.lines = [list(" " * width) for _ in range(height)]

    def addnstr(self, y, x, value, length, style):
        super().addnstr(y, x, value, length, style)
        rendered = value[:length]
        self.lines[y][x : x + len(rendered)] = rendered


class HandleColumnsTests(unittest.TestCase):
    def test_confirmation_retries_timeout_and_restores_menu_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser(directory, 40, [self.item()])
            with patch.object(browser.screen, "get_wch", side_effect=[curses.error(), "\x1b"]):
                self.assertEqual(browser.confirmation_key(), "\x1b")
            self.assertEqual(browser.screen.timeouts[-2:], [-1, 1000])

    def test_nested_rows_render_indented_titles_without_shifting_handles(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as directory:
            browser = self.browser(directory, 80, [self.item()])
            row = browser.view_rows()[0]
            nested = [replace(row, row_id=str(n), depth=n) for n in range(3)]
            with patch.object(browser, "view_rows", return_value=nested):
                browser.draw()
            painted = ["".join(browser.screen.lines[y]) for y in (4, 5, 6)]
            self.assertLess(painted[0].index("elevate"), painted[1].index("elevate"))
            self.assertLess(painted[1].index("elevate"), painted[2].index("elevate"))
            self.assertEqual(len({line.index("elev-mgr") for line in painted}), 1)
            self.assertIn("↳", painted[1])

    def test_decompression_keys_are_visible_before_title_at_skinny_width(self):
        with tempfile.TemporaryDirectory() as directory:
            for width in (40, 60, 80):
                item = self.item(handle="elev-mgr")
                item.title = "A very long conversation name " * 5
                item.compressed = True
                browser = self.browser(directory, width, [item], "n")
                row = browser.view_rows()[0]
                with (
                    patch("ai_sessions.retention.sync_storage"),
                    patch("ai_sessions.retention.needs_restore", return_value=True),
                ):
                    self.assertIsNone(browser.restore_to_open(row, item))
                prompt = "".join(browser.screen.lines[-1])
                self.assertIn("Y", prompt)
                self.assertIn("Decompress", prompt)
                self.assertIn("Esc cancel", prompt)

    def test_messages_key_previews_selected_copy_without_opening(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("ai_sessions.conversation_view.run") as preview,
        ):
            item = self.item()
            browser = self.browser(directory, 60, [item], "m", "q")
            self.assertIsNone(browser.run())
            preview.assert_called_once_with(browser.screen, item)

    def test_footer_uses_handle_without_redundant_slug(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch.object(app.curses, "curs_set"),
        ):
            item = self.item()
            item.slug = "long-legacy-slug"
            browser = self.browser(directory, 120, [item], "x")
            browser.draw()
            self.assertNotIn("Slug ", "\n".join("".join(line) for line in browser.screen.lines))
            browser.details()
            self.assertIn("long-legacy-slug", browser.screen.frames[-1])

    def browser(self, directory, width, items, *keys):
        return app.Browser(
            PositionedScreen(*keys, size=(30, width)),
            items,
            app.UserState(Path(directory) / "state.json"),
            LaunchConfig(path=Path(directory) / "config.toml"),
        )

    def item(self, native="native", handle="elev-mgr"):
        class AnnotatedSession(app.Session):
            pass

        original = display.DisplayTests.titled_session(native, title="elevate-futures-fork")
        item = AnnotatedSession(**asdict(original))
        item.handle = handle
        item.project_handle = "elev"
        item.session_role = "mgr"
        return item

    def test_handle_and_meaningful_title_at_real_terminal_widths(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch.object(app.curses, "curs_set"),
        ):
            for width in (40, 60, 80, 120):
                with self.subTest(width=width):
                    browser = self.browser(directory, width, [self.item()])
                    browser.draw()
                    heading = "".join(browser.screen.lines[3])
                    row = "".join(browser.screen.lines[4])
                    self.assertIn("HANDLE", heading)
                    self.assertIn("TITLE", heading)
                    self.assertIn("elev-mgr", row)
                    self.assertIn("elevate-futures-fork", row)
                    self.assertEqual(len(row), width)
                    self.assertLess(heading.index("HANDLE"), heading.index("TITLE"))

    def test_long_handles_keep_a_meaningful_title_and_missing_handles_show_dash(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch.object(app.curses, "curs_set"),
        ):
            for width in (40, 60, 80, 120):
                for handle in ("elev-supervisor-with-a-long-handle-12", "elev-mgr-12", ""):
                    with self.subTest(width=width, handle=handle):
                        browser = self.browser(directory, width, [self.item(handle=handle)])
                        browser.draw()
                        row = "".join(browser.screen.lines[4])
                        self.assertIn("elevate-futures-fork", row)
                        self.assertIn("elev" if handle else "—", row)
                        if handle == "elev-mgr-12":
                            self.assertIn(handle, row)
                        self.assertEqual(len(row), width)

    def test_expanded_copies_use_logical_handle(self):
        first, second = self.item("old"), self.item("new", "")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch.object(app.curses, "curs_set"),
        ):
            browser = self.browser(directory, 80, [first, second])
            rows = [
                app.ViewRow(
                    row_id=item.key,
                    members=(item,),
                    all_members=(first, second),
                    representative=item,
                    target=item,
                    status="latest copy",
                    title=item.title,
                    project=item.cwd,
                    expanded=True,
                )
                for item in (first, second)
            ]
            for visible in (rows, rows[1:]):
                with patch.object(browser, "view_rows", return_value=visible):
                    browser.draw()
                for y in range(4, 4 + len(visible)):
                    self.assertIn("elev-mgr", "".join(browser.screen.lines[y]))

    def test_list_has_handle_column_on_same_line(self):
        output = io.StringIO()
        with redirect_stdout(output):
            app.list_output([self.item()])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("HANDLE", lines[0])
        self.assertIn("elev-mgr", lines[1])
        self.assertIn("elevate-futures-fork", lines[1])

    def test_details_show_handle_project_and_role(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch.object(app.curses, "curs_set"),
        ):
            browser = self.browser(directory, 80, [self.item()], "x")
            browser.details()
            self.assertIn("Handle     elev-mgr", browser.screen.frames[-1])
            self.assertIn("Project handle  elev", browser.screen.frames[-1])
            self.assertIn("Role       mgr", browser.screen.frames[-1])
