import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
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
        with self.assertRaisesRegex(ValueError, "invented"):
            session("codex", "invented")


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
            tempfile.TemporaryDirectory() as root,
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app, "LAUNCH_LOG_FILE", Path(root) / "launch-log.jsonl"),
            patch.object(app.os, "chdir", lambda _: None),
            patch.object(app.shutil, "which", lambda name: rf"C:\npm\{name}.CMD"),
            patch.object(app.subprocess, "call", lambda argv: calls.append(argv) or 0),
        ):
            self.assertEqual(launch(session("claude"), LaunchConfig()), 0)
        self.assertEqual(calls, [[r"C:\npm\claude.CMD", "--resume", "session-id"]])

    def test_missing_command_reports_instead_of_raising(self) -> None:
        errors = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(app, "LAUNCH_LOG_FILE", Path(root) / "launch-log.jsonl"),
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

    def test_third_harness_notice_names_the_keyed_profile(self) -> None:
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="other",
            label="Other",
            short_label="Other",
            default_command=("other-cli",),
            resume_args=lambda **values: ["continue-thread", values["session_id"]],
        )
        errors = io.StringIO()
        with (
            REGISTRY.temporary(fake),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            launch(session("other"), LaunchConfig(mode="custom"), dry_run=True)
        self.assertIn("[launch.providers.other] custom_args", errors.getvalue())


class LaunchLogTests(unittest.TestCase):
    """A resume that opens the wrong conversation needs a record of what it asked for."""

    def test_launch_records_the_command_and_requested_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "launch-log.jsonl"
            with (
                patch.object(app, "IS_WINDOWS", True),
                patch.object(app, "LAUNCH_LOG_FILE", log),
                patch.object(app.os, "chdir", lambda _: None),
                patch.object(app.shutil, "which", lambda name: f"/opt/bin/{name}"),
                patch.object(app.subprocess, "call", lambda argv: 0),
            ):
                launch(session("codex", title="Reports"), LaunchConfig())
            entries = app.read_launch_log(path=log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "codex")
        self.assertEqual(entries[0]["selected_id"], "session-id")
        self.assertEqual(entries[0]["argv"], ["codex", "resume", "session-id"])
        self.assertEqual(entries[0]["executable"], "/opt/bin/codex")

    def test_subagent_redirect_is_recorded_so_the_parent_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "launch-log.jsonl"
            item = session("codex", "subagent", resume_id="parent-id", parent_id="parent-id")
            app.record_launch(item, command_for(item, LaunchConfig()), path=log)
            entries = app.read_launch_log(path=log)
        self.assertEqual(entries[0]["selected_id"], "session-id")
        self.assertEqual(entries[0]["resume_target"], "parent-id")
        self.assertEqual(entries[0]["argv"][-1], "parent-id")

    def test_a_dry_run_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "launch-log.jsonl"
            with patch.object(app, "LAUNCH_LOG_FILE", log), redirect_stdout(io.StringIO()):
                launch(session("codex"), LaunchConfig(), dry_run=True)
            self.assertFalse(log.exists())
            self.assertEqual(app.read_launch_log(path=log), [])

    def test_history_is_bounded_and_keeps_the_newest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "launch-log.jsonl"
            with patch.object(app, "LAUNCH_LOG_ENTRIES", 3):
                for index in range(5):
                    app.record_launch(
                        session("codex", session_id=f"id-{index}"), ["codex"], path=log
                    )
            entries = app.read_launch_log(path=log)
        self.assertEqual([entry["selected_id"] for entry in entries], ["id-2", "id-3", "id-4"])

    def test_damaged_lines_are_skipped_rather_than_losing_the_history(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "launch-log.jsonl"
            good = json.dumps({"selected_id": "kept"})
            log.write_text("not json\n" + good + "\n", encoding="utf-8")
            self.assertEqual(app.read_launch_log(path=log), [{"selected_id": "kept"}])

    def test_an_unwritable_log_never_fails_the_launch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            blocked = Path(root) / "missing-dir"
            blocked.write_text("not a directory", encoding="utf-8")
            with (
                patch.object(app, "IS_WINDOWS", True),
                patch.object(app, "LAUNCH_LOG_FILE", blocked / "launch-log.jsonl"),
                patch.object(app.os, "chdir", lambda _: None),
                patch.object(app.shutil, "which", lambda name: name),
                patch.object(app.subprocess, "call", lambda argv: 0),
            ):
                self.assertEqual(launch(session("codex"), LaunchConfig()), 0)

    def test_output_names_the_redirect_and_the_command(self) -> None:
        buffer = io.StringIO()
        entries = [
            {
                "at": "2026-09-23T17:00:00Z",
                "tool": "codex",
                "selected_id": "child",
                "resume_target": "parent",
                "title": "Worker",
                "cwd": "/project",
                "argv": ["codex", "resume", "parent"],
            }
        ]
        with redirect_stdout(buffer):
            app.launch_log_output(entries)
        printed = buffer.getvalue()
        self.assertIn("child → parent", printed)
        self.assertIn("codex resume parent", printed)

    def test_empty_history_explains_where_it_would_be(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            app.launch_log_output([])
        self.assertIn("no launches recorded", buffer.getvalue())


class StorageUpgradeTests(unittest.TestCase):
    """A harness that changed how it stores sessions gets to update its own."""

    def adapter_with_hook(self, hook: object) -> object:
        return replace(REGISTRY.get("codex"), upgrade_storage=hook)

    def test_native_resume_asks_the_recording_harness_first(self) -> None:
        seen: list[dict] = []

        def hook(**values: object) -> tuple[str, ...]:
            seen.append(values)
            return ("updated the stored session",)

        item = session("codex", storage="/sessions/one.jsonl")
        with REGISTRY.temporary(self.adapter_with_hook(hook)):
            notices = app.upgrade_native_storage(item, LaunchConfig())
        self.assertEqual(notices, ("updated the stored session",))
        self.assertEqual(seen[0]["session_id"], "session-id")
        self.assertEqual(seen[0]["storage"], "/sessions/one.jsonl")
        self.assertEqual(seen[0]["command"], ("codex",))

    def test_subagent_upgrades_the_session_that_is_actually_resumed(self) -> None:
        seen: list[str] = []

        def hook(*, session_id: str, **_: object) -> tuple[str, ...]:
            seen.append(session_id)
            return ()

        item = session("codex", "subagent", resume_id="parent-id", parent_id="parent-id")
        with REGISTRY.temporary(self.adapter_with_hook(hook)):
            app.upgrade_native_storage(item, LaunchConfig())
        self.assertEqual(seen, ["parent-id"])

    def test_a_copy_opened_elsewhere_is_left_to_that_harness(self) -> None:
        def hook(**_: object) -> tuple[str, ...]:
            raise AssertionError("a cross-harness launch must not upgrade the source")

        item = session(
            "codex",
            launch_targets={"codex": "x-2", "claude": "c-1"},
            launch_tool="claude",
        )
        with REGISTRY.temporary(self.adapter_with_hook(hook)):
            self.assertEqual(app.upgrade_native_storage(item, LaunchConfig()), ())

    def test_a_harness_without_the_capability_is_skipped(self) -> None:
        self.assertEqual(app.upgrade_native_storage(session("claude"), LaunchConfig()), ())

    def test_a_failing_upgrade_reports_but_never_blocks_the_resume(self) -> None:
        def hook(**_: object) -> tuple[str, ...]:
            raise OSError("storage is read-only")

        with REGISTRY.temporary(self.adapter_with_hook(hook)):
            notices = app.upgrade_native_storage(session("codex"), LaunchConfig())
        self.assertEqual(len(notices), 1)
        self.assertIn("storage is read-only", notices[0])

    def test_launch_reports_the_upgrade_before_running_the_harness(self) -> None:
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            hook = lambda **_: ("Updated session-id to paginated history.",)  # noqa: E731
            with (
                REGISTRY.temporary(self.adapter_with_hook(hook)),
                patch.object(app, "IS_WINDOWS", True),
                patch.object(app, "LAUNCH_LOG_FILE", Path(root) / "launch-log.jsonl"),
                patch.object(app.os, "chdir", lambda _: None),
                patch.object(app.shutil, "which", lambda name: f"/opt/bin/{name}"),
                patch.object(app.subprocess, "call", lambda argv: 0),
                redirect_stderr(errors),
            ):
                self.assertEqual(launch(session("codex"), LaunchConfig()), 0)
        self.assertIn("Updated session-id to paginated history.", errors.getvalue())

    def test_a_dry_run_changes_no_stored_session(self) -> None:
        def hook(**_: object) -> tuple[str, ...]:
            raise AssertionError("a dry run must not touch stored sessions")

        with (
            REGISTRY.temporary(self.adapter_with_hook(hook)),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(launch(session("codex"), LaunchConfig(), dry_run=True), 0)


if __name__ == "__main__":
    unittest.main()
