import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import Session, codex_resume_target, command_for, launch
from ai_sessions.config import LaunchConfig
from ai_sessions.registry import REGISTRY


def session(
    tool: str,
    source: str = "interactive",
    cwd: str = "/tmp/project with spaces",
    **overrides: object,
) -> Session:
    values = dict(
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
    values.update(overrides)
    return Session(**values)


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

    def test_selected_launch_harness_changes_command(self) -> None:
        item = session(
            "claude",
            launch_targets={"claude": "c-1", "codex": "x-2"},
            launch_tool="codex",
        )
        self.assertEqual(command_for(item, LaunchConfig()), ["codex", "resume", "x-2"])

    def test_selected_launch_target_is_honored_for_codex(self) -> None:
        item = session(
            "codex",
            launch_targets={"codex": "x-2", "claude": "c-1"},
            launch_tool="claude",
        )
        self.assertEqual(command_for(item, LaunchConfig()), ["claude", "--resume", "c-1"])

    def test_noninteractive_codex_resume(self) -> None:
        self.assertEqual(
            command_for(session("codex", "non-interactive"), LaunchConfig()),
            ["codex", "resume", "--include-non-interactive", "session-id"],
        )

    def test_codex_subagent_resumes_parent(self) -> None:
        item = session(
            "codex",
            "subagent",
            resume_id="parent-id",
            parent_id="parent-id",
        )
        self.assertEqual(command_for(item, LaunchConfig()), ["codex", "resume", "parent-id"])

    def test_orphaned_codex_subagent_resumes_itself_as_noninteractive(self) -> None:
        item = session("codex", "subagent")
        self.assertEqual(
            command_for(item, LaunchConfig()),
            ["codex", "resume", "--include-non-interactive", "session-id"],
        )

    def test_codex_subagent_target_prefers_parent_with_fallback(self) -> None:
        self.assertEqual(codex_resume_target("child", "subagent", "parent"), "parent")
        self.assertEqual(codex_resume_target("child", "subagent", ""), "child")
        self.assertEqual(codex_resume_target("thread", "interactive", "parent"), "thread")

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

    def test_registered_adapter_owns_resume_syntax(self) -> None:
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="other",
            label="Other",
            short_label="Other",
            default_command=("other-cli",),
            resume_args=lambda **values: ["continue-thread", values["session_id"]],
        )
        with REGISTRY.temporary(fake):
            self.assertEqual(
                command_for(session("other"), LaunchConfig()),
                ["other-cli", "continue-thread", "session-id"],
            )

    def test_invalid_source_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "invalid source kind"):
            command_for(session("codex", "invented"), LaunchConfig())


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


class WindowsShimResolutionTests(unittest.TestCase):
    """CreateProcess appends only .exe, so PATHEXT shims must be resolved first."""

    def test_windows_launch_resolves_the_shim_before_calling(self) -> None:
        calls: list[list[str]] = []
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app.os, "chdir", lambda _: None),
            patch.object(app.shutil, "which", lambda name: rf"C:\npm\{name}.CMD"),
            patch.object(app.subprocess, "call", lambda argv: calls.append(argv) or 0),
        ):
            self.assertEqual(launch(session("claude"), LaunchConfig()), 0)
        self.assertEqual(calls, [[r"C:\npm\claude.CMD", "--resume", "session-id"]])

    def test_missing_command_reports_instead_of_raising(self) -> None:
        errors = io.StringIO()
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app.os, "chdir", lambda _: None),
            patch.object(app.shutil, "which", lambda _: None),
            redirect_stderr(errors),
        ):
            self.assertEqual(launch(session("claude"), LaunchConfig()), 127)
        self.assertIn("not on PATH", errors.getvalue())


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

    def test_other_provider_args_do_not_suppress_warning(self) -> None:
        config = LaunchConfig(mode="custom", custom_claude_args=["--verbose"])
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            launch(session("codex"), config, dry_run=True)
        self.assertIn("no arguments configured for Codex", errors.getvalue())
        self.assertIn("launch.custom.codex_args", errors.getvalue())

    def test_safe_mode_is_silent(self) -> None:
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            launch(session("codex"), LaunchConfig(), dry_run=True)
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
