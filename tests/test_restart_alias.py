import unittest
from unittest.mock import patch


class RestartAliasTests(unittest.TestCase):
    def test_reset_and_restart_spellings_share_dispatch_and_harness_flags(self):
        from ai_sessions import app

        for spelling in ("--restart", "--reset", "restart", "reset"):
            with (
                self.subTest(spelling=spelling),
                patch.object(app, "IS_WINDOWS", True),
                patch.object(app, "restart_here", return_value=7) as restart,
            ):
                self.assertEqual(app._main([spelling, "--grok"]), 7)
                restart.assert_called_once_with("grok")

    def test_identity_and_identify_share_arguments_and_return_code(self):
        from ai_sessions import app

        for spelling in ("identify", "identity"):
            with (
                self.subTest(spelling=spelling),
                patch(
                    "ai_sessions.handle_lookup.identify", create=True, return_value=1
                ) as identify,
            ):
                self.assertEqual(app._main([spelling, "--json"]), 1)
                identify.assert_called_once_with(["--json"])

    def test_native_reset_argument_is_not_rewritten(self):
        from ai_sessions import app

        with patch("ai_sessions.new_session.main", return_value=0) as launch:
            self.assertEqual(app._main(["new", "grok", "--", "--reset"]), 0)
        launch.assert_called_once_with(["grok", "--", "--reset"])

    def test_alias_preserves_options_and_return_code(self):
        from ai_sessions.restart import main

        with patch("ai_sessions.app.main", return_value=7) as run:
            self.assertEqual(main(["--launch-tool", "claude"]), 7)
        run.assert_called_once_with(["--restart", "--launch-tool", "claude"])
