import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from ai_sessions import app
from ai_sessions.config import LaunchConfig
from ai_sessions.model import Session
from ai_sessions.registry import REGISTRY
from ai_sessions.supervisor import create
from ai_sessions.supervisor.create import (
    LEDGER_TEMPLATE,
    SupervisorError,
    create_supervisor,
    default_supervisor_tool,
    target_log,
)
from ai_sessions.supervisor.index import Exchange, TargetIndex
from ai_sessions.supervisor.store import load_hook_state, save_hook_state


def target(tool: str = "codex", **overrides: object) -> Session:
    values = dict(
        tool=tool,
        session_id="target-id",
        title="Fix the build",
        cwd=str(Path.cwd()),
        updated=0,
        created=0,
        preview="",
        named=True,
        storage="storage-path",
    )
    values.update(overrides)
    return Session(**values)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "supervisors"
        self.enterContext(
            patch.dict(os.environ, {"AI_SESSIONS_MESSAGING_DIR": str(self.dir / "messaging")})
        )
        self.state = app.UserState(path=self.dir / "state.json")
        # A launch may store the supervisor's session, so no harness may see a real home.
        for name in ("claude", "codex"):
            home = self.dir / name
            self.enterContext(REGISTRY.temporary(replace(REGISTRY.get(name), home=home)))
        for patcher in (
            patch.object(TargetIndex, "build", return_value=7),
            patch.object(TargetIndex, "extend", return_value=None),
            patch.object(app.UserState, "_checkpoint", staticmethod(lambda item: None)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, tool_of_target: str = "codex", supervisor: str = "claude") -> str:
        return create_supervisor(
            self.state, target(tool_of_target), supervisor, root=self.root, save=False
        )

    def command(self, record_id: str, config: LaunchConfig | None = None) -> str:
        """The dry-run command without its trailing supervisor-folder argument."""
        output = self.dry_run(record_id, config).strip()
        suffix = f" --add-dir={self.root / record_id}"
        self.assertTrue(output.endswith(suffix), output)
        return output.removesuffix(suffix)

    def dry_run(self, record_id: str, config: LaunchConfig | None = None) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            code = app.launch_supervisor(
                record_id, config or LaunchConfig(), self.state, dry_run=True, root=self.root
            )
        self.assertEqual(code, 0)
        return out.getvalue()


class CreateTests(Fixture):
    def test_create_assigns_conversation_and_writes_folder(self) -> None:
        record_id = self.make()
        record = self.state.supervisors[record_id]
        conversation_id = self.state.conversation_id_for(target())
        self.assertTrue(conversation_id)
        self.assertEqual(record["target_conversation"], conversation_id)
        self.assertEqual(record["tool"], "claude")
        self.assertEqual(record["title"], "Fix the build · supervisor")
        self.assertFalse(record["archived"])
        folder = self.root / record_id
        ledger = (folder / "ledger.md").read_text(encoding="utf-8")
        self.assertEqual(ledger, LEDGER_TEMPLATE)
        for heading in ("Objective", "Assessment", "Open concerns", "Decisions", "Messages sent"):
            self.assertIn(f"## {heading}", ledger)
        hook_state = load_hook_state(record_id, self.root)
        self.assertEqual(hook_state.member, "codex:target-id")
        self.assertEqual(hook_state.checkpoint, 7)
        self.assertTrue(hook_state.brief_due)
        uuid.UUID(hook_state.native_session_id)

    def test_codex_supervisor_has_no_native_id_up_front(self) -> None:
        record_id = self.make("claude", "codex")
        self.assertEqual(load_hook_state(record_id, self.root).native_session_id, "")

    def test_default_tool_is_opposite_harness_unless_configured(self) -> None:
        config = LaunchConfig(path=self.dir / "config.toml")
        self.assertEqual(default_supervisor_tool("claude", config), "codex")
        self.assertEqual(default_supervisor_tool("codex", config), "claude")
        config.path.write_text('[supervisor]\ndefault_tool = "codex"\n', encoding="utf-8")
        self.assertEqual(default_supervisor_tool("codex", config), "codex")
        self.assertEqual(default_supervisor_tool("claude", config), "codex")

    def test_create_refuses_opencode_target_with_clear_message(self) -> None:
        with self.assertRaises(SupervisorError) as caught:
            create_supervisor(self.state, target("opencode"), "claude", root=self.root)
        self.assertIn("cannot be supervised", str(caught.exception))
        self.assertEqual(self.state.supervisors, {})
        self.assertFalse(self.root.exists())


class LaunchTests(Fixture):
    def test_new_supervisor_argv_claude_includes_session_id_name_and_no_prompt(self) -> None:
        record_id = self.make("codex", "claude")
        native_id = load_hook_state(record_id, self.root).native_session_id
        output = self.command(record_id)
        self.assertIn(f"claude --session-id {native_id} --name", output)
        # The name is the last argument: there is no opening prompt after it.
        self.assertTrue(output.rstrip("'\"").endswith("Supervisor: Fix the build"), output)

    def test_new_supervisor_argv_codex_has_no_prompt(self) -> None:
        record_id = self.make("claude", "codex")
        output = self.command(record_id)
        self.assertTrue(output.endswith("codex"), output)

    def test_existing_supervisor_resumes_by_native_id(self) -> None:
        record_id = self.make("codex", "codex")
        hook_state = load_hook_state(record_id, self.root)
        hook_state.native_session_id = "019abcde-0000-0000-0000-000000000000"
        hook_state.briefs = 1
        save_hook_state(hook_state, self.root)
        with patch.object(app, "_native_session_stored", return_value=True):
            output = self.command(record_id)
        self.assertTrue(
            output.endswith("codex resume 019abcde-0000-0000-0000-000000000000"), output
        )

    def test_unstored_native_session_starts_fresh_under_the_same_id(self) -> None:
        record_id = self.make("codex", "claude")
        native = load_hook_state(record_id, self.root).native_session_id
        with patch.object(app, "_native_session_stored", return_value=False):
            output = self.dry_run(record_id)
        self.assertIn(f"--session-id {native}", output)
        self.assertNotIn("--resume", output)

    def test_supervisor_inherits_launch_mode(self) -> None:
        record_id = self.make("codex", "claude")
        config = LaunchConfig(mode="dangerous")
        self.assertIn("--dangerously-skip-permissions", self.dry_run(record_id, config))

    def test_launch_sets_supervisor_env_for_the_child(self) -> None:
        record_id = self.make("codex", "claude")
        seen: dict[str, str] = {}

        def fake_run(session: Session, command: list[str]) -> int:
            seen["env"] = os.environ.get(create.ENV_VAR, "")
            return 0

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app, "run_harness", fake_run),
            patch.object(app, "record_launch"),
            patch.object(app.shutil, "which", return_value="claude.exe"),
            patch.object(app.os, "chdir"),
        ):
            code = app.launch_supervisor(record_id, LaunchConfig(), self.state, root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(seen["env"], record_id)

    def launch(self, record_id: str) -> list[list[str]]:
        commands: list[list[str]] = []

        def fake_run(session: Session, command: list[str]) -> int:
            commands.append(command)
            return 0

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app, "run_harness", fake_run),
            patch.object(app, "record_launch"),
            patch.object(app.shutil, "which", side_effect=lambda name: f"{name}.exe"),
            patch.object(app.os, "chdir"),
        ):
            app.launch_supervisor(record_id, LaunchConfig(), self.state, root=self.root)
        return commands

    def test_new_codex_supervisor_is_stored_named_and_resumed(self) -> None:
        # Codex cannot name a session it starts and titles an unnamed one
        # itself, so the supervisor is stored with its name and resumed.
        record_id = self.make("claude", "codex")
        commands = self.launch(record_id)
        native_id = load_hook_state(record_id, self.root).native_session_id
        self.assertTrue(native_id)
        self.assertEqual(commands[0][1:3], ["resume", native_id])
        index = (self.dir / "codex" / "session_index.jsonl").read_text(encoding="utf-8")
        self.assertEqual(
            json.loads(index.splitlines()[-1]),
            {"id": native_id, "thread_name": "Supervisor: Fix the build", "updated_at": ANY},
        )
        self.assertEqual(
            self.state.supervisors[record_id]["published_title"], "Supervisor: Fix the build"
        )

    def test_remote_supervisor_uses_provider_folder_preparation_before_launch(self) -> None:
        record_id = self.make("claude", "codex")
        prepared = []

        def prepare(command, **kwargs):
            self.assertIn("--remote", command)
            self.assertFalse(any(arg.startswith("--add-dir") for arg in command))
            resources = kwargs.pop("resources")
            resources.callback(lambda: prepared.append("closed"))
            prepared.append(kwargs)
            return command

        adapter = replace(REGISTRY.get("codex"), supervisor_launch=prepare)
        with (
            REGISTRY.temporary(adapter),
            patch("ai_sessions.messaging.install.settings", return_value={"enabled": True}),
        ):
            commands = self.launch(record_id)
        self.assertEqual(len(commands), 1)
        self.assertFalse(any(arg.startswith("--add-dir") for arg in commands[0]))
        self.assertEqual(prepared[-1], "closed")
        self.assertEqual(
            prepared[:-1],
            [
                dict(
                    session_id=load_hook_state(record_id, self.root).native_session_id,
                    cwd=str(Path.cwd()),
                    directory=str(self.root / record_id),
                    dry_run=False,
                )
            ],
        )

    def test_folder_preparation_failure_prevents_terminal_launch(self) -> None:
        record_id = self.make("claude", "codex")
        from ai_sessions.conversion import BridgeError

        def refuse(command, **kwargs):
            raise BridgeError("supervisor folder unavailable")

        adapter = replace(REGISTRY.get("codex"), supervisor_launch=refuse)
        with REGISTRY.temporary(adapter), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(self.launch(record_id), [])

    def test_older_supervisor_gets_its_name_published_once(self) -> None:
        record_id = self.make("codex", "claude")
        published: list[tuple[str, str]] = []
        with (
            patch.object(app, "_native_session_stored", return_value=True),
            patch.object(
                app, "publish_name", lambda item, name: published.append((item.tool, name)) or ""
            ),
        ):
            self.launch(record_id)
            self.launch(record_id)
        self.assertEqual(published, [("claude", "Supervisor: Fix the build")])

    def test_unpublished_name_is_retried_on_the_next_launch(self) -> None:
        record_id = self.make("codex", "claude")
        with (
            patch.object(app, "_native_session_stored", return_value=True),
            patch.object(app, "publish_name", return_value="transcript is missing"),
        ):
            self.launch(record_id)
        self.assertNotIn("published_title", self.state.supervisors[record_id])

    def test_supervisor_title_drops_bridge_suffixes(self) -> None:
        record = {"title": "minecraft-mods (from Claude) (from Codex) · supervisor"}
        self.assertEqual(create.supervisor_title(record), "Supervisor: minecraft-mods")


class TargetLogTests(Fixture):
    def test_target_log_does_not_move_checkpoint(self) -> None:
        record_id = self.make()
        index = TargetIndex(self.root / record_id / "index.jsonl")
        index.append(
            [
                Exchange(1, "m", "first question", "first question", ("first answer",)),
                Exchange(2, "m", "second question", "second question", ("second answer",)),
            ]
        )
        state_file = self.root / record_id / "record.json"
        before = (state_file.read_bytes(), index.path.read_bytes())
        output = target_log(self.state, record_id, tail=1, root=self.root)
        self.assertIn("second question", output)
        self.assertNotIn("first question", output)
        self.assertEqual((state_file.read_bytes(), index.path.read_bytes()), before)
        self.assertEqual(load_hook_state(record_id, self.root).checkpoint, 7)

    def test_target_log_accepts_target_session_id_and_rejects_bad_since(self) -> None:
        record_id = self.make()
        TargetIndex(self.root / record_id / "index.jsonl").append(
            [Exchange(1, "m", "hello", "hello")]
        )
        self.assertIn("hello", target_log(self.state, "target-id", root=self.root))
        with self.assertRaises(SupervisorError):
            target_log(self.state, record_id, since="yesterday-ish", root=self.root)
        self.assertIn(
            "best-effort",
            target_log(self.state, record_id, since="2026-10-31T00:00:00", root=self.root),
        )


class CliTests(unittest.TestCase):
    def test_parser_lists_new_options(self) -> None:
        text = app.build_parser().format_help()
        for option in ("--supervise", "--target-log", "--install-hooks", "--uninstall-hooks"):
            self.assertIn(option, text)

    def test_hook_files_are_named_for_each_harness(self) -> None:
        claude_file, codex_file = app.supervisor_hook_files()
        self.assertEqual(claude_file.name, "settings.json")
        self.assertEqual(codex_file.name, "hooks.json")


class SupervisorPosixTests(Fixture):
    def test_ctrl_c_is_left_to_supervisor_child_while_resources_are_retained(self):
        record_id = self.make("claude", "codex")
        process = MagicMock()
        process.__enter__.return_value = process
        process.wait.side_effect = [KeyboardInterrupt, 7]
        with (
            patch.object(app, "IS_WINDOWS", False),
            patch.object(app.subprocess, "Popen", return_value=process),
            patch.object(app, "record_launch"),
            patch.object(app, "note_opened"),
            patch.object(app.shutil, "which", return_value="codex"),
            patch.object(app.os, "chdir"),
        ):
            code = app.launch_supervisor(record_id, LaunchConfig(), self.state, root=self.root)
        self.assertEqual(code, 7)
        self.assertEqual(process.wait.call_count, 2)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
