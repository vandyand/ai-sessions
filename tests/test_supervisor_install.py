import json
import tempfile
import unittest
from pathlib import Path

from ai_sessions.supervisor.install import hook_command, install, uninstall

KILL = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "python -I block_mass_kill.py", "timeout": 10}],
}


def ours(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        h["command"]
        for groups in data.get("hooks", {}).values()
        for g in groups
        for h in g["hooks"]
        if "ai_sessions_hook" in h["command"]
    ]


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.claude = self.dir / "settings.json"
        self.codex = self.dir / "hooks.json"
        for path in (self.claude, self.codex):
            path.write_text(
                json.dumps({"other": 1, "hooks": {"PreToolUse": [KILL]}}), encoding="utf-8"
            )

    def test_install_adds_entries_and_preserves_existing_hooks(self) -> None:
        install(self.claude, self.codex)
        for path in (self.claude, self.codex):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["PreToolUse"], [KILL])
            self.assertEqual(data["other"], 1)
            self.assertEqual(len(ours(path)), 2)
            handler = data["hooks"]["SessionStart"][0]["hooks"][0]
            self.assertEqual(handler["timeout"], 60)
            self.assertEqual(handler["command"], hook_command("session-start"))
        self.assertEqual(len(list(self.dir.glob("*.bak-*"))), 2)

    def test_install_is_idempotent(self) -> None:
        install(self.claude, self.codex)
        install(self.claude, self.codex)
        self.assertEqual(len(ours(self.claude)), 2)
        self.assertEqual(len(ours(self.codex)), 2)

    def test_uninstall_removes_only_ours(self) -> None:
        install(self.claude, self.codex)
        uninstall(self.claude, self.codex)
        for path in (self.claude, self.codex):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"], {"PreToolUse": [KILL]})

    def test_codex_install_notice_mentions_retrust(self) -> None:
        notices = install(self.claude, self.codex)
        self.assertTrue(any("re-trust" in n for n in notices))

    def test_malformed_settings_file_is_refused_not_overwritten(self) -> None:
        self.claude.write_text("{not json", encoding="utf-8")
        notices = install(self.claude, self.codex)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), "{not json")
        self.assertTrue(any("Refused" in n for n in notices))
        self.assertEqual(len(ours(self.codex)), 2)

    def test_missing_file_is_created(self) -> None:
        new = self.dir / "sub" / "new.json"
        install(new, self.codex)
        self.assertEqual(len(ours(new)), 2)


if __name__ == "__main__":
    unittest.main()


class HookCommandTests(unittest.TestCase):
    def test_command_uses_unquoted_forward_slash_path(self) -> None:
        command = hook_command("session-start", r"C:\Python312\python.exe")
        self.assertEqual(command, "C:/Python312/python.exe -m ai_sessions_hook session-start")

    def test_command_quotes_a_path_with_spaces(self) -> None:
        command = hook_command("user-prompt-submit", r"C:\Program Files\Python\python.exe")
        self.assertTrue(command.startswith('"C:/Program Files/Python/python.exe" -m '))


class UpgradeTests(unittest.TestCase):
    def test_reinstall_replaces_an_earlier_builds_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            old = {"type": "command", "command": "py -I -m ai_sessions.supervisor.hook x"}
            path.write_text(
                json.dumps({"hooks": {"SessionStart": [{"hooks": [old]}]}}), encoding="utf-8"
            )
            install(path, Path(tmp) / "hooks.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            commands = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
            self.assertEqual(commands, [hook_command("session-start")])

    def test_non_ascii_text_is_kept_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"note": "café ✓"}, ensure_ascii=False), encoding="utf-8")
            install(path, Path(tmp) / "hooks.json")
            self.assertIn("café ✓", path.read_text(encoding="utf-8"))
