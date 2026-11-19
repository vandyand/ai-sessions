import contextlib
import io
import unittest
from unittest.mock import patch

from ai_sessions import handles_cli
from ai_sessions.model import Session


class HandleCliTests(unittest.TestCase):
    def test_short_open_forwards_exact_resolver_reference(self):
        with patch("ai_sessions.app.main", return_value=0) as run:
            self.assertEqual(handles_cli.main(["open", "elev-wrk-11"]), 0)
        run.assert_called_once_with(["--resume", "elev-wrk-11"])

    def test_ses_uses_same_entry_point(self):
        import tomllib
        from pathlib import Path

        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"]["ses"], project["project"]["scripts"]["sessions"]
        )

    def test_list_has_real_stats_and_does_not_launch(self):
        row = Session("codex", "fiction", "Worker eleven", ".", 0, 0, "", True, "fiction.jsonl")
        row.handle = "elev-wrk-11"
        row.project_handle = "elev"
        row.session_role = "wrk"
        with (
            patch("ai_sessions.handles_cli.catalog", return_value=([row], object())),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(handles_cli.main(["list", "elev"]), 0)
        self.assertIn("elev-wrk-11", output.getvalue())
        self.assertIn("Worker eleven", output.getvalue())
        self.assertNotIn("%", output.getvalue())
