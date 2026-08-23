import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions.bridge import resolve_budget
from ai_sessions.config import LaunchConfig


class LaunchConfigTests(unittest.TestCase):
    def test_default_is_safe(self) -> None:
        with TemporaryDirectory() as directory:
            config = LaunchConfig.load(Path(directory) / "missing.toml")
        self.assertEqual(config.mode, "safe")
        self.assertEqual(config.provider_prefix("claude"), ["claude"])
        self.assertEqual(config.provider_prefix("codex"), ["codex"])

    def test_custom_args_missing_detects_empty_custom_mode(self) -> None:
        self.assertTrue(LaunchConfig(mode="custom").custom_args_missing())
        self.assertFalse(
            LaunchConfig(mode="custom", custom_claude_args=["--verbose"]).custom_args_missing()
        )
        self.assertFalse(
            LaunchConfig(mode="custom", custom_codex_args=["--search"]).custom_args_missing()
        )

    def test_custom_args_missing_is_provider_specific(self) -> None:
        config = LaunchConfig(mode="custom", custom_claude_args=["--verbose"])
        self.assertFalse(config.custom_args_missing("claude"))
        self.assertTrue(config.custom_args_missing("codex"))

    def test_custom_args_missing_is_false_for_other_modes(self) -> None:
        self.assertFalse(LaunchConfig().custom_args_missing())
        self.assertFalse(LaunchConfig(mode="dangerous").custom_args_missing())

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

    def test_missing_budget_stays_unset_when_saved(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            config = LaunchConfig(path=path)
            config.save()
            text = path.read_text(encoding="utf-8")
            loaded = LaunchConfig.load(path)
        self.assertIn("version = 2", text)
        self.assertNotIn("max_chars", text)
        self.assertNotIn("max_tokens", text)
        self.assertIsNone(loaded.bridge_max_chars)
        self.assertIsNone(loaded.bridge_max_tokens)

    def test_schema_one_machine_default_migrates_but_schema_two_is_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("version = 1\n[bridge]\nmax_chars = 950000\n", encoding="utf-8")
            migrated = LaunchConfig.load(path)
            path.write_text("version = 2\n[bridge]\nmax_chars = 950000\n", encoding="utf-8")
            explicit = LaunchConfig.load(path)
        self.assertTrue(migrated.bridge_max_chars_migrated)
        self.assertIsNone(migrated.bridge_max_chars)
        self.assertFalse(explicit.bridge_max_chars_migrated)
        self.assertEqual(explicit.bridge_max_chars, 950_000)

    def test_token_budget_survives_save_without_resurrecting_characters(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = LaunchConfig(path=path, bridge_max_tokens=12_345)
            original.save()
            loaded = LaunchConfig.load(path)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(loaded.bridge_max_tokens, 12_345)
        self.assertIsNone(loaded.bridge_max_chars)
        self.assertIn("max_tokens = 12345", text)
        self.assertNotIn("max_chars", text)

    def test_loaded_token_budget_wins_when_both_legacy_keys_exist(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "version = 2\n[bridge]\nmax_tokens = 5000\nmax_chars = 950000\n",
                encoding="utf-8",
            )
            loaded = LaunchConfig.load(path)
            budget = resolve_budget(
                "claude",
                max_tokens=loaded.bridge_max_tokens,
                max_chars=loaded.bridge_max_chars,
            )
            loaded.save()
            saved = path.read_text(encoding="utf-8")
        self.assertEqual((budget.tokens, budget.chars), (5_000, 10_000))
        self.assertIn("max_tokens = 5000", saved)
        self.assertNotIn("max_chars", saved)

    def test_migrated_default_save_preserves_effective_target_budget(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[bridge]\nmax_chars = 950000\n", encoding="utf-8")
            loaded = LaunchConfig.load(path)
            before = resolve_budget("claude", migrated=loaded.bridge_max_chars_migrated)
            loaded.save()
            reloaded = LaunchConfig.load(path)
            after = resolve_budget(
                "claude",
                max_tokens=reloaded.bridge_max_tokens,
                max_chars=reloaded.bridge_max_chars,
                migrated=reloaded.bridge_max_chars_migrated,
            )
            saved = path.read_text(encoding="utf-8")
        self.assertEqual((before.tokens, before.chars), (after.tokens, after.chars))
        self.assertNotIn("max_chars", saved)

    def test_invalid_budget_values_load_as_unset(self) -> None:
        cases = ("0", "-1", "true", '"5000"')
        for key in ("max_tokens", "max_chars"):
            for value in cases:
                with self.subTest(key=key, value=value), TemporaryDirectory() as directory:
                    path = Path(directory) / "config.toml"
                    path.write_text(f"version = 2\n[bridge]\n{key} = {value}\n", encoding="utf-8")
                    loaded = LaunchConfig.load(path)
                self.assertIsNone(loaded.bridge_max_tokens)
                self.assertIsNone(loaded.bridge_max_chars)


if __name__ == "__main__":
    unittest.main()
