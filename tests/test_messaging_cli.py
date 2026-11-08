import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


class MessagingCliTests(unittest.TestCase):
    def setUp(self):
        from ai_sessions.messaging import cli
        from ai_sessions.references import SlugStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(
            patch.object(
                cli, "_slug_store", return_value=SlugStore(Path(temporary.name) / "slugs.sqlite3")
            )
        )

    def test_receipt_disk_failure_after_submission_is_uncertain_not_retryable(self):
        from ai_sessions.messaging import cli, store

        row = dict(tool="codex", session_id="a")
        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            original = store.save_receipt

            def save(data):
                if data["status"] != "submitting":
                    raise OSError("disk full")
                original(data)

            with (
                patch.object(cli, "reachable", return_value=[row]),
                patch.object(cli, "deliver", return_value={"status": "accepted"}) as deliver,
                patch.object(store, "save_receipt", side_effect=save),
            ):
                code, result = cli.send_message("codex:a", "hi", "codex:a")
            self.assertEqual(code, 3)
            self.assertEqual(result["status"], "uncertain")
            deliver.assert_called_once()

    def test_early_messaging_dispatch_keeps_windows_unicode_setup(self):
        from unittest.mock import Mock

        import ai_sessions.app as app

        output = Mock()
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app.sys, "stdout", output),
            patch("ai_sessions.messaging.cli.main", return_value=0),
        ):
            self.assertEqual(app._main(["messaging", "status", "--json"]), 0)
        output.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_send_literal_text_and_reply_route(self):
        from ai_sessions.messaging import cli, store

        a = dict(tool="codex", session_id="a", name="A")
        b = dict(tool="claude", session_id="b", name="B")
        calls = []

        def deliver(row, text, mid, sender):
            calls.append((row, text, sender))
            return dict(status="submitted", message_id=mid)

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            with (
                patch.object(cli, "reachable", return_value=[a, b]),
                patch.object(cli, "deliver", side_effect=deliver),
                redirect_stdout(io.StringIO()),
            ):
                code, first = cli.send_message("B", "hello $(danger); '中文'", "codex:a")
                self.assertEqual(code, 0)
                self.assertIn("hello $(danger); '中文'", calls[0][1])
                code, reply = cli.reply_message(first["message_id"], "received", "claude:b")
                self.assertEqual(code, 0)
                self.assertEqual(calls[1][0], a)
                self.assertEqual(store.receipt(first["message_id"])["status"], "acknowledged")
                self.assertEqual(reply["recipient"]["session_id"], "a")

    def test_uncertain_is_not_success_or_retried(self):
        from ai_sessions.messaging import cli, store

        row = dict(tool="codex", session_id="a", name="A")
        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            with (
                patch.object(cli, "reachable", return_value=[row]),
                patch.object(cli, "deliver", return_value={"status": "uncertain"}) as deliver,
            ):
                code, _ = cli.send_message("A", "hi", "codex:a")
                self.assertEqual(code, 3)
                deliver.assert_called_once()

    def test_installer_preserves_other_hooks_and_is_idempotent(self):
        from ai_sessions.messaging.install import edit_hooks

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text('{"hooks":{"SessionStart":[{"hooks":[{"command":"other"}]}]}}')
            edit_hooks(path, True)
            first = path.read_bytes()
            edit_hooks(path, True)
            self.assertEqual(path.read_bytes(), first)
            edit_hooks(path, False)
            self.assertIn('"other"', path.read_text())
            self.assertNotIn("ai_sessions.messaging.hook", path.read_text())

    def test_reply_cannot_reach_replacement_generation(self):
        from ai_sessions.messaging import cli, store

        a = dict(tool="codex", session_id="a", home="C:/one", generation="original")
        b = dict(tool="claude", session_id="b", generation="b")
        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            with (
                patch.object(cli, "reachable", return_value=[a, b]),
                patch.object(cli, "deliver", return_value={"status": "accepted"}),
            ):
                _, first = cli.send_message("claude:b", "hi", "codex:a")
            with patch.object(
                cli, "reachable", return_value=[{**a, "generation": "replacement"}, b]
            ):
                with self.assertRaisesRegex(ValueError, "original.*unavailable"):
                    cli.reply_message(first["message_id"], "received", "claude:b")
