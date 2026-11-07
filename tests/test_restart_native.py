import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from test_detached import session

from ai_sessions import app, detached
from ai_sessions.conversion import BridgeError


class RestartNativeTests(unittest.TestCase):
    def test_shared_daemon_tool_finds_only_its_own_verified_terminal_launch(self):
        import os

        daemon = Mock(pid=10)
        daemon.name.return_value = "codex.exe"
        daemon.cmdline.return_value = ["codex", "app-server"]
        caller = Mock()
        caller.parents.return_value = [daemon]
        terminal = Mock(pid=20)
        terminal.create_time.return_value = 123.5
        terminal.is_running.return_value = True
        terminal.cmdline.return_value = ["codex", "--remote", "unix://"]
        entry = dict(tool="codex", session_id="mine", pid=20, started=123.5)
        with (
            patch.object(detached, "_read", return_value={"tab": entry}),
            patch.object(
                detached.psutil,
                "Process",
                side_effect=lambda pid=None: caller if pid is None else terminal,
            ),
            patch.dict(os.environ, {"CODEX_THREAD_ID": "mine"}),
        ):
            self.assertEqual(detached.running_here(), ("tab", entry))
            terminal.create_time.return_value = 456
            self.assertIsNone(detached.running_here())
            terminal.create_time.return_value = 123.5
            terminal.cmdline.return_value = ["codex", "app-server"]
            self.assertIsNone(detached.running_here())
            terminal.cmdline.return_value = ["codex", "--remote", "unix://"]
            with patch.dict(os.environ, {"CODEX_THREAD_ID": "other"}):
                self.assertIsNone(detached.running_here())
            with patch.object(detached, "_read", return_value={"tab": entry, "other": dict(entry)}):
                self.assertIsNone(detached.running_here())
            terminal.cmdline.side_effect = detached.psutil.AccessDenied(20)
            self.assertIsNone(detached.running_here())
            terminal.cmdline.side_effect = None
            daemon.name.return_value = "unrelated.exe"
            self.assertIsNone(detached.running_here())

    def test_normal_open_again_still_refuses_divergent_heads(self):
        chosen = session("fork", tool="codex")
        state = Mock()
        state.resolve_launch.side_effect = BridgeError("divergent heads")
        with (
            patch("ai_sessions.retention.sync_storage"),
            patch("ai_sessions.retention.needs_restore", return_value=False),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(app.open_again(chosen, app.LaunchConfig(), state, [], dry_run=True), 2)
        self.assertIn("divergent heads", errors.getvalue())
        state.resolve_launch.assert_called_once()

    def test_reopen_ignores_saved_cross_harness_preference_unless_requested(self):
        chosen = session("fork", tool="codex")
        chosen.launch_tool = "claude"
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app, "UserState"),
            patch.object(app.LaunchConfig, "load", return_value=app.LaunchConfig()),
            patch.object(app, "reopen_from_catalog", return_value=(chosen, [chosen])),
            patch.object(app, "open_again", return_value=0) as opened,
        ):
            self.assertEqual(app._main(["--reopen"]), 0)
            self.assertEqual(opened.call_args.args[0].launch_tool, "codex")
            self.assertEqual(app._main(["--reopen", "--launch-tool", "claude"]), 0)
            self.assertEqual(opened.call_args.args[0].launch_tool, "claude")

    def test_exact_native_mode_does_not_allow_cross_harness_selection(self):
        chosen = session("fork", tool="codex")
        chosen.launch_tool = "claude"
        with (
            patch("ai_sessions.retention.sync_storage"),
            patch("ai_sessions.retention.needs_restore", return_value=False),
        ):
            with self.assertRaisesRegex(BridgeError, "cannot switch harnesses"):
                app.prepare_launch(chosen, exact_native=True)
            chosen.launch_tool = ""
            chosen.compressed = True
            with self.assertRaisesRegex(BridgeError, "compressed"):
                app.prepare_launch(chosen, exact_native=True)

    def test_exact_reopen_keeps_fork_even_when_conversation_has_divergent_heads(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.UserState(Path(directory) / "state.json")
            chosen = session("fork", tool="codex")
            chosen.conversation_id = "divergent"
            chosen.launch_targets = {"codex": "wrong-legacy-hint"}
            state.resolve_launch = Mock(side_effect=BridgeError("divergent heads"))
            with (
                patch("ai_sessions.retention.sync_storage"),
                patch("ai_sessions.retention.needs_restore", return_value=False),
            ):
                exact, note = app.prepare_launch(chosen, state=state, exact_native=True)
                self.assertEqual((exact.tool, exact.session_id), ("codex", "fork"))
                self.assertEqual(exact.launch_target("codex"), "fork")
                self.assertEqual(note, "")
                state.resolve_launch.assert_not_called()
                with self.assertRaisesRegex(BridgeError, "divergent heads"):
                    app.prepare_launch(chosen, state=state)

    def test_open_again_uses_exact_native_only_for_same_harness(self):
        chosen = session("fork", tool="codex")
        with patch.object(app, "launch", return_value=0) as launch:
            app.open_again(chosen, app.LaunchConfig(), Mock(), [], exact_native=True)
            self.assertTrue(launch.call_args.kwargs["exact_native"])
            chosen.launch_tool = "claude"
            app.open_again(chosen, app.LaunchConfig(), Mock(), [])
            self.assertFalse(launch.call_args.kwargs["exact_native"])

    def test_terminal_memory_selects_harness_and_id_not_just_shared_uuid(self):
        first = session("same", tool="claude")
        second = session("same", tool="codex")
        self.assertIs(
            app.remembered_session({"tool": "claude", "session_id": "same"}, [first, second]), first
        )
        self.assertIs(
            app.remembered_session({"tool": "codex", "session_id": "same"}, [first, second]), second
        )
        self.assertIsNone(app.remembered_session({"session_id": "same"}, [first, second]))
