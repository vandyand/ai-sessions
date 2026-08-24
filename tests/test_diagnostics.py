import gc
import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions import app, diagnostics
from ai_sessions.capabilities import Unsupported
from ai_sessions.registry import REGISTRY


@contextmanager
def harness_home(name: str, home: Path):
    with REGISTRY.temporary(replace(REGISTRY.get(name), home=home)):
        yield


def make_state_database(directory: Path) -> Path:
    """A minimal stand-in for Codex's own state database."""
    path = directory / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT, rollout_path TEXT, source TEXT, cwd TEXT, "
        "title TEXT, created_at INTEGER, updated_at INTEGER)"
    )
    connection.execute("INSERT INTO threads VALUES ('019f-a', '', 'cli', '/tmp/p', 'One', 1, 2)")
    connection.commit()
    connection.close()
    return path


class WarningRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        diagnostics.clear_warnings()

    def test_distinct_messages_are_kept_once(self) -> None:
        diagnostics.record_warning("a")
        diagnostics.record_warning("a")
        diagnostics.record_warning("b")
        self.assertEqual(diagnostics.warnings(), ["a", "b"])

    def test_clearing_starts_a_fresh_load(self) -> None:
        diagnostics.record_warning("a")
        diagnostics.clear_warnings()
        self.assertEqual(diagnostics.warnings(), [])


class UnreadableCodexStateTests(unittest.TestCase):
    """An unreadable store must not be reported as an empty one."""

    def setUp(self) -> None:
        diagnostics.clear_warnings()

    def test_readable_database_reports_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            make_state_database(root)
            with harness_home("codex", root):
                app.load_codex_sessions()
            self.assertEqual(diagnostics.warnings(), [])

    def test_locked_database_is_reported_not_silently_empty(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = make_state_database(root)
            holder = sqlite3.connect(path, timeout=30, isolation_level=None)
            holder.execute("PRAGMA journal_mode=DELETE")
            holder.execute("BEGIN EXCLUSIVE")
            try:
                with harness_home("codex", root):
                    result = app.load_codex_sessions()
            finally:
                holder.execute("ROLLBACK")
                holder.close()
                # The failed load's traceback keeps its connection alive, and
                # Windows will not delete a file that still has a handle.
                gc.collect()
            self.assertEqual(result, [])
            notes = diagnostics.warnings()
            self.assertEqual(len(notes), 1)
            self.assertIn("could not read Codex sessions", notes[0])
            self.assertIn("state_5.sqlite", notes[0])

    def test_missing_state_database_is_reported_when_codex_home_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with harness_home("codex", root):
                self.assertEqual(app.load_codex_sessions(), [])
            self.assertEqual(len(diagnostics.warnings()), 1)
            self.assertIn("no Codex state database", diagnostics.warnings()[0])

    def test_absent_codex_home_is_silent(self) -> None:
        """Codex simply not being installed is not a fault to report."""
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "no-codex-here"
            with harness_home("codex", missing):
                self.assertEqual(app.load_codex_sessions(), [])
            self.assertEqual(diagnostics.warnings(), [])

    def test_a_load_clears_warnings_from_the_previous_one(self) -> None:
        diagnostics.record_warning("stale note from an earlier load")
        with TemporaryDirectory() as directory:
            with (
                REGISTRY.temporary(
                    replace(REGISTRY.get("claude"), home=Path(directory) / "claude")
                ),
                REGISTRY.temporary(replace(REGISTRY.get("codex"), home=Path(directory) / "absent")),
                REGISTRY.temporary(
                    replace(
                        REGISTRY.get("opencode"),
                        discover=Unsupported("isolated diagnostics test"),
                    )
                ),
            ):
                app.load_sessions(use_cache=False)
        self.assertNotIn("stale note from an earlier load", diagnostics.warnings())


if __name__ == "__main__":
    unittest.main()
