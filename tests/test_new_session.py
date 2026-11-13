import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.model import NativeRef
from ai_sessions.new_session import _prepare_initial, option_specs, restart_options, run

HELP = """Options:
  -m, --model <MODEL>  Model
  -c, --config <KEY>  Config override
      --dangerous    Existing permission flag
      --add-dir <DIR>...  Directories
      --last         Selection
      --resume [ID]  Selection
      --worktree [NAME]  One-time worktree
      --no-alt-screen  Native terminal behavior
"""


class NewSessionTests(unittest.TestCase):
    def test_resume_last_uses_native_cd_for_selection_before_launch(self):
        from pathlib import Path

        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            create_session=Mock(),
            resolve=Mock(return_value=None),
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        specs = {**option_specs(HELP), "--cd": (True, False, False)}
        with (
            patch(
                "ai_sessions.new_session.subprocess.run",
                return_value=SimpleNamespace(stdout="Commands:\n  resume  Resume existing"),
            ),
            patch(
                "ai_sessions.new_session._choose_resume", return_value=NativeRef("id", "native")
            ) as choose,
        ):
            _prepare_initial(
                adapter,
                "native.exe",
                ["resume", "--last", "--cd", "project"],
                specs,
                str(Path.cwd()),
                ["resume", "placeholder"],
            )
        self.assertEqual(choose.call_args.args[2], str((Path.cwd() / "project").resolve()))

    def test_external_remote_is_not_given_a_locally_created_identity(self):
        adapter = SimpleNamespace(create_session=Mock())
        with self.assertRaises(ValueError):
            _prepare_initial(
                adapter,
                "native.exe",
                ["--remote", "wss://example.invalid"],
                {"--remote": (True, False, False)},
                "/work",
                ["resume", "id"],
            )
        adapter.create_session.assert_not_called()

    def test_exceptional_setup_cleans_up_only_the_owned_client(self):
        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            home="home",
            native_executable=lambda: "native.exe",
            current_session=Mock(side_effect=RuntimeError("test failure")),
            restart_discard_options=(),
            contain_launch=False,
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        child = Mock(pid=123)
        with (
            patch("ai_sessions.new_session.REGISTRY") as registry,
            patch("ai_sessions.new_session._specs", return_value=option_specs(HELP)),
            patch("ai_sessions.new_session.subprocess.Popen", return_value=child),
            patch("ai_sessions.new_session._started", return_value=10),
            patch("ai_sessions.new_session._abort_owned", create=True) as abort,
        ):
            registry.get.return_value = adapter
            with self.assertRaises(RuntimeError):
                run("sample", [])
        abort.assert_called_once_with(child, None, "sample", 10)

    def test_native_resume_picker_is_selected_before_starting_shared_client(self):
        ref = NativeRef("selected", "native.jsonl")
        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            create_session=Mock(),
            resolve=Mock(return_value=None),
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        with (
            patch(
                "ai_sessions.new_session.subprocess.run",
                return_value=SimpleNamespace(stdout="Commands:\n  resume  Resume existing"),
            ),
            patch(
                "ai_sessions.new_session._choose_resume", return_value=ref, create=True
            ) as choose,
        ):
            argv, item = _prepare_initial(
                adapter,
                "native.exe",
                ["resume"],
                option_specs(HELP),
                "/work",
                ["resume", "placeholder"],
            )
        self.assertEqual(item.session_id, "selected")
        self.assertEqual(argv, ["native.exe", "resume", "selected"])
        choose.assert_called_once()

    def test_new_conversation_identity_is_assigned_before_cli_launch(self):
        ref = NativeRef("assigned", "native.jsonl")
        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            create_session=Mock(return_value=ref),
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        with patch(
            "ai_sessions.new_session.subprocess.run",
            return_value=SimpleNamespace(stdout="Commands:\n  resume  Resume existing"),
        ):
            argv, item = _prepare_initial(
                adapter,
                "native.exe",
                ["--dangerous", "initial task"],
                option_specs(HELP),
                "/work",
                ["resume", "identity"],
            )
        self.assertEqual(
            argv, ["native.exe", "resume", "assigned", "--dangerous", "--", "initial task"]
        )
        self.assertEqual(item.session_id, "assigned")
        adapter.create_session.assert_called_once_with(cwd="/work", name="")

    def test_native_cd_is_applied_to_initial_conversation_metadata(self):
        from pathlib import Path

        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            create_session=Mock(return_value=NativeRef("id", "native")),
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        specs = {**option_specs(HELP), "--cd": (True, False, False)}
        with patch(
            "ai_sessions.new_session.subprocess.run",
            return_value=SimpleNamespace(stdout="Commands:\n  resume  Resume existing"),
        ):
            argv, _ = _prepare_initial(
                adapter,
                "native.exe",
                ["--cd", "project", "--", "-literal prompt"],
                specs,
                str(Path.cwd()),
                ["resume", "id"],
            )
        self.assertEqual(
            adapter.create_session.call_args.kwargs["cwd"], str((Path.cwd() / "project").resolve())
        )
        self.assertEqual(argv[-2:], ["--", "-literal prompt"])

    def test_identity_preparation_does_not_create_data_for_help_or_version(self):
        adapter = SimpleNamespace(create_session=Mock())
        for flag in ["--help", "--version"]:
            self.assertEqual(
                _prepare_initial(adapter, "native.exe", [flag], {}, "/work", ["resume", "id"]),
                (["native.exe", flag], None),
            )
        adapter.create_session.assert_not_called()

    def test_literal_help_after_separator_is_a_prompt_not_a_help_request(self):
        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            create_session=Mock(return_value=NativeRef("id", "native")),
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        with patch(
            "ai_sessions.new_session.subprocess.run",
            return_value=SimpleNamespace(stdout="Commands:\n  resume  Resume existing"),
        ):
            argv, item = _prepare_initial(
                adapter,
                "native.exe",
                ["--", "--help"],
                option_specs(HELP),
                "/work",
                ["resume", "placeholder"],
            )
        self.assertEqual(item.session_id, "id")
        self.assertEqual(argv[-2:], ["--", "--help"])

    def test_restart_refreshes_native_identity_before_stopping_client(self):
        from ai_sessions import app
        from ai_sessions.model import Session

        old = dict(tool="sample", session_id="old", pid=123, started=10, cwd=".", new_launcher=True)
        fresh = Session("sample", "current", "Current", ".", 0, 0, "", False, "native")
        adapter = SimpleNamespace(
            liveness_executables={"sample.exe"}, current_session=lambda *args: None
        )
        with (
            patch("ai_sessions.detached.running_here", return_value=("pane", old)),
            patch("ai_sessions.platforms.windows.process_started", return_value=10),
            patch("ai_sessions.new_session._identify", return_value=fresh),
            patch("ai_sessions.detached.remember") as remember,
            patch("ai_sessions.detached.request_restart") as request,
            patch("ai_sessions.detached.stop_for_restart", return_value="") as stop,
            patch.object(app.REGISTRY, "get", return_value=adapter),
        ):
            self.assertEqual(app.restart_here(), 0)
            remember.assert_called_once()
            self.assertEqual(remember.call_args.args[0].session_id, "current")
            request.assert_called_once_with("pane", "")
            stop.assert_called_once()

    def test_resume_preserves_options_without_replaying_prompt_or_picker(self):
        specs = option_specs(HELP)
        args = [
            "--dangerous",
            "--model",
            "model-a",
            "initial task",
            "--no-alt-screen",
            "--config",
            "answer=1",
            "resume",
            "--last",
            "old-id",
        ]
        self.assertEqual(
            restart_options(args, specs, {"--last", "--resume", "--worktree"}),
            ["--dangerous", "--model", "model-a", "--no-alt-screen", "--config", "answer=1"],
        )

    def test_variadic_equals_optional_and_attached_short_options(self):
        args = [
            "--model=model-a",
            "-canswer=1",
            "--add-dir",
            "one",
            "two",
            "--worktree",
            "once",
            "--dangerous",
        ]
        self.assertEqual(
            restart_options(args, option_specs(HELP), {"--worktree"}),
            ["--model=model-a", "-canswer=1", "--add-dir", "one", "two", "--dangerous"],
        )

    def test_unknown_option_is_refused_instead_of_dropping_its_value(self):
        with self.assertRaises(ValueError):
            restart_options(["--new-native-flag", "value"], option_specs(HELP), set())

    def test_native_child_inherits_console_and_restart_pins_current_identity(self):
        adapter = SimpleNamespace(
            name="sample",
            label="Sample",
            home="home",
            native_executable=lambda: "native.exe",
            current_session=Mock(
                side_effect=[
                    NativeRef("first", "a"),
                    NativeRef("second", "b"),
                    None,
                    NativeRef("second", "b"),
                    None,
                ]
            ),
            restart_discard_options=("--last",),
            contain_launch=False,
            rediscover=None,
            resume_args=lambda **kwargs: ["resume", kwargs["session_id"]],
        )
        first = Mock(pid=10)
        first.wait.side_effect = [
            subprocess.TimeoutExpired("native", 0.25),
            subprocess.TimeoutExpired("native", 0.25),
            0,
        ]
        second = Mock(pid=20)
        second.wait.return_value = 7
        with (
            patch("ai_sessions.new_session.REGISTRY") as registry,
            patch("ai_sessions.new_session.subprocess.Popen", side_effect=[first, second]) as popen,
            patch("ai_sessions.new_session._specs", return_value=option_specs(HELP)),
            patch("ai_sessions.new_session._started", return_value=1),
            patch("ai_sessions.new_session._remember") as remember,
            patch("ai_sessions.new_session._closed", side_effect=["", None]),
            patch("ai_sessions.new_session._workspace_context") as workspace,
        ):
            registry.get.return_value = adapter
            workspace.return_value.__enter__.return_value = None
            self.assertEqual(run("sample", ["--dangerous", "initial task"]), 7)
        self.assertEqual(
            popen.call_args_list[0].args[0], ["native.exe", "--dangerous", "initial task"]
        )
        self.assertEqual(
            popen.call_args_list[1].args[0], ["native.exe", "--dangerous", "resume", "second"]
        )
        self.assertNotIn("stdin", popen.call_args_list[0].kwargs)
        self.assertNotIn("stdout", popen.call_args_list[0].kwargs)
        self.assertEqual(
            [c.args[0].session_id for c in remember.call_args_list], ["first", "second", "second"]
        )
