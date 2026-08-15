import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_sessions.app import Session, command_for, launch
from ai_sessions.config import LaunchConfig


def session(
    tool: str, source: str = "interactive", cwd: str = "/tmp/project with spaces"
) -> Session:
    return Session(
        tool=tool,
        session_id="session-id",
        title="Test",
        cwd=cwd,
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


class LaunchDirectoryTests(unittest.TestCase):
    def test_dry_run_strips_extended_length_prefix(self) -> None:
        item = session("codex", cwd=r"\\?\C:\Users\vandy\project")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(launch(item, LaunchConfig(), dry_run=True), 0)
        rendered = buffer.getvalue()
        self.assertIn(r"C:\Users\vandy\project", rendered)
        self.assertNotIn("\\\\?\\", rendered)

    def test_dry_run_keeps_unc_share(self) -> None:
        item = session("codex", cwd=r"\\?\UNC\server\share\project")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            launch(item, LaunchConfig(), dry_run=True)
        rendered = buffer.getvalue()
        self.assertIn(r"\\server\share\project", rendered)
        self.assertNotIn("\\\\?\\", rendered)

    def test_dry_run_leaves_ordinary_paths_alone(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            launch(session("codex"), LaunchConfig(), dry_run=True)
        self.assertIn("/tmp/project with spaces", buffer.getvalue())


class CustomModeNoticeTests(unittest.TestCase):
    def test_empty_custom_mode_warns(self) -> None:
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            launch(session("codex"), LaunchConfig(mode="custom"), dry_run=True)
        self.assertIn("custom launch mode has no arguments configured", errors.getvalue())

    def test_configured_custom_mode_is_silent(self) -> None:
        config = LaunchConfig(mode="custom", custom_codex_args=["--search"])
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            launch(session("codex"), config, dry_run=True)
        self.assertEqual(errors.getvalue(), "")

    def test_safe_mode_is_silent(self) -> None:
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            launch(session("codex"), LaunchConfig(), dry_run=True)
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
