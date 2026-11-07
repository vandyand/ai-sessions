import unittest
from unittest.mock import patch


class MessagingLaunchTests(unittest.TestCase):
    def test_managed_native_launch_and_explicit_remote_are_preserved(self):
        from ai_sessions.messaging.launch import arguments

        with patch("ai_sessions.messaging.install.settings", return_value={"enabled": True}):
            self.assertEqual(
                arguments(["codex", "resume", "a"], "codex"),
                ["codex", "--remote", "unix://", "resume", "a"],
            )
            command = ["codex", "--remote", "ws://localhost:1234", "resume", "a"]
            self.assertEqual(arguments(command, "codex"), command)
            with self.assertRaisesRegex(ValueError, "no-daemon"):
                arguments(["codex", "--no-daemon"], "codex")

    def test_remote_flags_follow_the_entire_provider_command_prefix(self):
        from ai_sessions.messaging.launch import arguments

        with patch("ai_sessions.messaging.install.settings", return_value={"enabled": True}):
            self.assertEqual(
                arguments(["node", "codex.js", "resume", "a"], "codex", prefix_length=2),
                ["node", "codex.js", "--remote", "unix://", "resume", "a"],
            )
