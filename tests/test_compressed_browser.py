import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app, retention
from ai_sessions.app import Browser, Session, UserState, filtered_sessions, list_output
from ai_sessions.compression import CompressionError
from ai_sessions.config import LaunchConfig

try:
    from test_display import ScriptedScreen
except ImportError:  # run as tests.test_compressed_browser
    from tests.test_display import ScriptedScreen


def session(name: str, *, compressed: bool = False, hidden: bool = False) -> Session:
    item = Session(
        "codex", name, name, "/work", 1_700_000_000, 1_699_000_000, "p", True, f"{name}.jsonl"
    )
    item.compressed = compressed
    item.hidden = hidden
    return item


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        patcher = patch.object(app, "detect_open_sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plain = session("plain task")
        self.packed = session("packed task", compressed=True)
        self.sessions = [self.plain, self.packed]
        self.state = UserState(self.dir / "state.json")
        self.config = LaunchConfig(path=self.dir / "config.toml")

    def browser(self, *keys: object) -> Browser:
        return Browser(
            ScriptedScreen(*keys, size=(40, 200)), self.sessions, self.state, self.config
        )

    def frame(self, browser: Browser) -> str:
        browser.draw()
        return browser.screen.frames[-1]


class FilterTests(Fixture):
    def names(self, **kwargs) -> list[str]:
        return [item.session_id for item in filtered_sessions(self.sessions, **kwargs)]

    def test_default_hides_compressed_sessions(self) -> None:
        self.assertEqual(self.names(), ["plain task"])

    def test_each_value(self) -> None:
        self.assertEqual(self.names(compression="compressed"), ["packed task"])
        self.assertCountEqual(self.names(compression="both"), ["plain task", "packed task"])

    def test_combines_with_visibility(self) -> None:
        hidden = session("hidden packed", compressed=True, hidden=True)
        self.sessions.append(hidden)
        self.assertEqual(
            self.names(compression="compressed", visibility="hidden"), ["hidden packed"]
        )
        self.assertEqual(self.names(compression="compressed"), ["packed task"])


class BrowserTests(Fixture):
    def test_z_cycles_and_the_header_says_so(self) -> None:
        browser = self.browser()
        self.assertEqual(browser.compression, "uncompressed")
        self.assertIn("Uncompressed", self.frame(browser))
        expected = {
            "compressed": "Compressed",
            "both": "Uncompressed + compressed",
            "uncompressed": "Uncompressed",
        }
        for value in app.COMPRESSION_ORDER[1:] + app.COMPRESSION_ORDER[:1]:
            browser.cycle_compression()
            self.assertEqual(browser.compression, value)
            self.assertIn(expected[value], self.frame(browser))

    def test_z_key_is_a_command_not_a_search(self) -> None:
        browser = self.browser("z", "q")
        browser.run()
        self.assertEqual(browser.compression, "compressed")
        self.assertEqual(browser.query, "")

    def test_compressed_row_is_marked(self) -> None:
        browser = self.browser()
        browser.compression = "both"
        browser.selected = 1
        lines = self.frame(browser)
        self.assertIn("compressed", lines)

    def test_header_counts_only_what_the_filter_excludes(self) -> None:
        browser = self.browser()
        self.assertIn("1 compressed", self.frame(browser))
        browser.query = "packed"
        self.assertIn("1 compressed", self.frame(browser))
        browser.query = "plain"
        self.assertNotIn("1 compressed", self.frame(browser))
        browser.query = "packed"
        browser.compression = "both"
        self.assertNotIn("1 compressed", self.frame(browser))

    def test_empty_search_points_at_the_filter(self) -> None:
        browser = self.browser()
        browser.query = "packed"
        self.assertIn("z shows", self.frame(browser))

    def test_show_everything_keys_include_compressed_sessions(self) -> None:
        for key in ("A", "H"):
            browser = self.browser(key, "q")
            browser.run()
            self.assertEqual(browser.compression, "both")

    def test_help_lists_z(self) -> None:
        browser = self.browser("?", "x", "q")
        browser.run()
        help_frame = next(f for f in browser.screen.frames if "AI SESSIONS — HELP" in f)
        self.assertTrue(any(line.startswith("z ") for line in help_frame.splitlines()))


class ActionFixture(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "real.jsonl"
        self.path.write_text('{"a":1}\n', encoding="utf-8")
        self.real = session("real")
        self.real.storage = str(self.path)
        self.real.cwd = str(self.dir)
        self.sessions[:] = [self.real]
        patcher = patch.object(Browser, "begin_refresh")
        patcher.start()
        self.addCleanup(patcher.stop)


class ActionTests(ActionFixture):
    def test_Z_compresses_and_says_where_it_went(self) -> None:
        browser = self.browser("Z", "q")
        browser.run()
        self.assertTrue(self.real.compressed)
        self.assertFalse(self.path.exists())
        self.assertIn("z changes which are shown", "\n".join(browser.screen.frames))

    def test_Z_on_a_compressed_session_restores_it(self) -> None:
        retention.compress_session(self.real)
        browser = self.browser("Z", "q")
        browser.compression = "both"
        browser.run()
        self.assertFalse(self.real.compressed)
        self.assertTrue(self.path.is_file())

    def test_Z_refuses_an_open_session(self) -> None:
        self.real.is_open = True
        browser = self.browser("Z", "q")
        browser.run()
        self.assertFalse(self.real.compressed)
        self.assertTrue(self.path.is_file())
        self.assertIn("is open", "\n".join(browser.screen.frames))

    def test_a_failure_is_shown_and_keeps_the_session(self) -> None:
        with patch.object(retention, "compress_file", side_effect=CompressionError("no room")):
            browser = self.browser("Z", "q")
            browser.run()
        self.assertFalse(self.real.compressed)
        self.assertTrue(self.path.is_file())
        self.assertIn("no room", "\n".join(browser.screen.frames))

    def test_Z_compresses_every_closed_member_of_a_group(self) -> None:
        other_path = self.dir / "other.jsonl"
        other_path.write_text("{}\n", encoding="utf-8")
        other = session("other")
        other.storage = str(other_path)
        other.conversation_id = self.real.conversation_id = "conv"
        other.is_open = True
        self.sessions.append(other)
        with patch.object(UserState, "apply"):
            browser = self.browser("Z", "q")
            browser.run()
        self.assertTrue(self.real.compressed)
        self.assertFalse(other.compressed)

    def test_harness_that_cannot_compress_is_reported(self) -> None:
        self.real.tool = "opencode"
        browser = self.browser("Z", "q")
        browser.run()
        self.assertIn("cannot be compressed", "\n".join(browser.screen.frames))


class OpeningTests(ActionFixture):
    def setUp(self) -> None:
        super().setUp()
        retention.compress_session(self.real)

    def browser_on_it(self, *keys: object) -> Browser:
        browser = self.browser(*keys)
        browser.compression = "both"
        return browser

    def test_enter_asks_decompresses_and_opens(self) -> None:
        browser = self.browser_on_it("\n", "y")
        result = browser.run()
        self.assertIs(result, self.real)
        self.assertFalse(self.real.compressed)
        self.assertTrue(self.path.is_file())
        self.assertIn("Decompress", "\n".join(browser.screen.frames))

    def test_declining_changes_nothing(self) -> None:
        browser = self.browser_on_it("\n", "n", "q")
        self.assertIsNone(browser.run())
        self.assertTrue(self.real.compressed)
        self.assertFalse(self.path.exists())

    def test_failure_does_not_open(self) -> None:
        with patch(
            "ai_sessions.retention.decompress_session",
            side_effect=CompressionError("not enough disk space"),
        ):
            browser = self.browser_on_it("\n", "y", "q")
            self.assertIsNone(browser.run())
        self.assertIn("not enough disk space", "\n".join(browser.screen.frames))

    def test_bridge_choices_vanish_while_compressed(self) -> None:
        self.assertEqual(app.available_launch_tools(self.real), ("codex",))


class CommandLineTests(Fixture):
    def run_list(self, *arguments: str) -> str:
        output = io.StringIO()
        with (
            patch.object(app, "load_sessions", return_value=self.sessions),
            patch.object(app, "save_session_catalog"),
            patch.object(app, "UserState", return_value=self.state),
            contextlib.redirect_stdout(output),
        ):
            code = app.main(["--no-cache", *arguments])
        self.assertIn(code, (0, None))
        return output.getvalue()

    def test_list_defaults_to_uncompressed(self) -> None:
        text = self.run_list("--list")
        self.assertIn("plain task", text)
        self.assertNotIn("packed task", text)

    def test_list_filters_and_marks(self) -> None:
        text = self.run_list("--list", "--compression", "compressed")
        self.assertIn("packed task", text)
        self.assertIn("compressed", text)
        self.assertNotIn("plain task", text)

    def test_json_reports_the_field(self) -> None:
        rows = json.loads(self.run_list("--json", "--compression", "both"))
        self.assertEqual(
            {row["session_id"]: row["compressed"] for row in rows},
            {
                "plain task": False,
                "packed task": True,
            },
        )

    def real_session(self) -> Path:
        path = self.dir / "cli.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        self.plain.storage = str(path)
        return path

    def test_compress_and_decompress_commands(self) -> None:
        path = self.real_session()
        self.assertIn("compressed", self.run_list("--compress", "plain task"))
        self.assertFalse(path.exists())
        self.assertIn("decompressed", self.run_list("--decompress", "plain task"))
        self.assertTrue(path.is_file())

    def test_resume_of_a_compressed_session_refuses_with_instructions(self) -> None:
        path = self.real_session()
        retention.compress_session(self.plain)
        errors = io.StringIO()
        with (
            patch.object(app, "load_sessions", return_value=self.sessions),
            patch.object(app, "save_session_catalog"),
            patch.object(app, "UserState", return_value=self.state),
            contextlib.redirect_stderr(errors),
        ):
            code = app.main(["--no-cache", "--resume", "plain task", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("--decompress", errors.getvalue())
        self.assertFalse(path.exists())

    def test_launch_tool_on_a_compressed_session_says_why(self) -> None:
        self.real_session()
        retention.compress_session(self.plain)
        errors = io.StringIO()
        with (
            patch.object(app, "load_sessions", return_value=self.sessions),
            patch.object(app, "save_session_catalog"),
            patch.object(app, "UserState", return_value=self.state),
            contextlib.redirect_stderr(errors),
        ):
            code = app.main(
                ["--no-cache", "--resume", "plain task", "--launch-tool", "claude", "--dry-run"]
            )
        self.assertEqual(code, 2)
        self.assertIn("compressed", errors.getvalue())

    def test_list_output_marks_a_compressed_row(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            list_output([self.packed])
        self.assertIn("compressed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
