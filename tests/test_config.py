import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions.config import LaunchConfig


class LaunchConfigTests(unittest.TestCase):
    def test_default_is_safe(self) -> None:
        with TemporaryDirectory() as directory:
            config = LaunchConfig.load(Path(directory) / "missing.toml")
        self.assertEqual(config.mode, "safe")
        self.assertEqual(config.provider_prefix("claude"), ["claude"])
        self.assertEqual(config.provider_prefix("codex"), ["codex"])

    def test_dangerous_provider_flags(self) -> None:
        config = LaunchConfig(mode="dangerous")
        self.assertEqual(
            config.provider_prefix("claude"),
            ["claude", "--dangerously-skip-permissions"],
        )
        self.assertEqual(
            config.provider_prefix("codex"),
            ["codex", "--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_round_trip_custom_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = LaunchConfig(
                path=path,
                mode="custom",
                claude_command=["C:/Tools/claude.exe"],
                codex_command=["codex"],
                custom_claude_args=["--permission-mode", "acceptEdits"],
                custom_codex_args=["--sandbox", "workspace-write"],
            )
            original.save()
            loaded = LaunchConfig.load(path)
        self.assertEqual(loaded.mode, "custom")
        self.assertEqual(loaded.claude_command, ["C:/Tools/claude.exe"])
        self.assertEqual(
            loaded.provider_prefix("claude"),
            ["C:/Tools/claude.exe", "--permission-mode", "acceptEdits"],
        )
        self.assertEqual(
            loaded.provider_prefix("codex"),
            ["codex", "--sandbox", "workspace-write"],
        )


if __name__ == "__main__":
    unittest.main()
