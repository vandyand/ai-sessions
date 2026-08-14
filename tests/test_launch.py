import unittest

from ai_sessions.app import Session, command_for
from ai_sessions.config import LaunchConfig


def session(tool: str, source: str = "interactive") -> Session:
    return Session(
        tool=tool,
        session_id="session-id",
        title="Test",
        cwd="/tmp/project with spaces",
        updated=0,
        created=0,
        preview="",
        named=True,
        storage="",
        source=source,
    )


class LaunchCommandTests(unittest.TestCase):
    def test_safe_claude_resume(self) -> None:
        self.assertEqual(
            command_for(session("claude"), LaunchConfig()),
            ["claude", "--resume", "session-id"],
        )

    def test_safe_codex_resume(self) -> None:
        self.assertEqual(
            command_for(session("codex"), LaunchConfig()),
            ["codex", "resume", "session-id"],
        )

    def test_noninteractive_codex_resume(self) -> None:
        self.assertEqual(
            command_for(session("codex", "non-interactive"), LaunchConfig()),
            ["codex", "resume", "--include-non-interactive", "session-id"],
        )

    def test_dangerous_flags_precede_resume(self) -> None:
        config = LaunchConfig(mode="dangerous")
        self.assertEqual(
            command_for(session("codex"), config),
            [
                "codex",
                "--dangerously-bypass-approvals-and-sandbox",
                "resume",
                "session-id",
            ],
        )


if __name__ == "__main__":
    unittest.main()
