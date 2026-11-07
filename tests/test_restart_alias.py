import unittest
from unittest.mock import patch


class RestartAliasTests(unittest.TestCase):
    def test_alias_preserves_options_and_return_code(self):
        from ai_sessions.restart import main

        with patch("ai_sessions.app.main", return_value=7) as run:
            self.assertEqual(main(["--launch-tool", "claude"]), 7)
        run.assert_called_once_with(["--restart", "--launch-tool", "claude"])
