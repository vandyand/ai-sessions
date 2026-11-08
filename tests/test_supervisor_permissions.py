"""Scoped supervisor permissions on the native shared daemon."""

import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from ai_sessions.conversion import BridgeError
from ai_sessions.harnesses import codex
from ai_sessions.harnesses.messaging_rpc import RPCError


class ThreadRPC:
    def __init__(self, cwd, policy, *, ignore_update=False, active=False):
        self.cwd = cwd
        self.policy = copy.deepcopy(policy)
        self.ignore_update = ignore_update
        self.active = active
        self.approval = "on-request"
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def drain_notifications(self):
        self.draining = True

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/resume":
            assert params == {"threadId": "supervisor", "excludeTurns": True}
            return {
                "approvalPolicy": self.approval,
                "cwd": self.cwd,
                "sandbox": copy.deepcopy(self.policy),
                "thread": {
                    "id": "supervisor",
                    "status": {"type": "active" if self.active else "idle"},
                },
            }
        if method == "thread/settings/update":
            assert set(params) <= {"threadId", "sandboxPolicy", "approvalPolicy"}
            assert params["threadId"] == "supervisor"
            if not self.ignore_update:
                self.policy = copy.deepcopy(params.get("sandboxPolicy", self.policy))
                self.approval = params.get("approvalPolicy", self.approval)
            return {}
        raise AssertionError(method)


class SupervisorPermissionsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = str(Path(tmp.name).resolve() / "project")
        self.directory = str(Path(tmp.name).resolve() / "supervisor")
        self.existing = str(Path(tmp.name).resolve() / "existing")
        self.resources = self.enterContext(ExitStack())
        self.command = ["codex", "--remote", "unix://", "resume", "supervisor"]
        self.policy = dict(
            type="workspaceWrite",
            writableRoots=[self.existing],
            networkAccess=False,
            excludeTmpdirEnvVar=True,
            excludeSlashTmp=True,
        )

    def prepare(self, command=None, *, dry_run=False):
        return codex.supervisor_launch(
            self.command if command is None else command,
            session_id="supervisor",
            cwd=self.cwd,
            directory=self.directory,
            dry_run=dry_run,
            resources=self.resources,
        )

    def connect(self, fake):
        self.enterContext(
            patch(
                "ai_sessions.harnesses.messaging_codex.ensure_runtime",
                return_value={"status": "running"},
            )
        )
        return self.enterContext(
            patch("ai_sessions.harnesses.messaging_rpc.CodexRPC", return_value=fake)
        )

    @unittest.skipUnless(os.name == "nt", "Windows short path aliases")
    def test_short_project_path_matches_canonical_server_directory(self):
        import ctypes

        Path(self.cwd).mkdir(parents=True)
        short = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(self.cwd, short, len(short))
        if not length or short.value.casefold() == self.cwd.casefold():
            self.skipTest("filesystem has no distinct short path alias")
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        self.cwd = short.value
        self.assertEqual(self.prepare(), self.command)
        self.assertIn(self.directory, fake.policy["writableRoots"])

    def test_adds_only_supervisor_root_and_preserves_every_policy_setting(self):
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        self.assertEqual(self.prepare(), self.command)
        self.assertEqual(
            fake.policy, {**self.policy, "writableRoots": [self.existing, self.directory]}
        )
        self.assertEqual(
            [m for m, _ in fake.calls], ["thread/resume", "thread/settings/update", "thread/resume"]
        )

    def test_reopen_does_not_repeat_permission_mutation(self):
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        self.prepare()
        self.prepare()
        self.assertEqual([m for m, _ in fake.calls].count("thread/settings/update"), 1)

    def test_inherited_full_access_is_not_changed(self):
        fake = ThreadRPC(self.cwd, {"type": "dangerFullAccess"})
        self.connect(fake)
        self.assertEqual(self.prepare(), self.command)
        self.assertEqual([m for m, _ in fake.calls], ["thread/resume"])

    def test_read_only_is_not_silently_bypassed(self):
        fake = ThreadRPC(self.cwd, {"type": "readOnly", "networkAccess": False})
        self.connect(fake)
        with self.assertRaisesRegex(BridgeError, "workspace-write"):
            self.prepare()
        self.assertEqual([m for m, _ in fake.calls], ["thread/resume"])

    def test_rejected_or_ignored_update_prevents_launch_and_keeps_error_private(self):
        for rejected in (True, False):
            with self.subTest(rejected=rejected):
                fake = ThreadRPC(self.cwd, self.policy, ignore_update=True)
                original = fake.call

                def call(method, params):
                    if rejected and method == "thread/settings/update":
                        raise RPCError(-32600, "private upstream diagnostic")
                    return original(method, params)

                fake.call = call
                with (
                    patch("ai_sessions.harnesses.messaging_rpc.CodexRPC", return_value=fake),
                    patch(
                        "ai_sessions.harnesses.messaging_codex.ensure_runtime",
                        return_value={"status": "running"},
                    ),
                ):
                    with self.assertRaises(BridgeError) as caught:
                        self.prepare()
                self.assertNotIn("private upstream diagnostic", str(caught.exception))

    def test_different_project_or_active_thread_is_not_mutated(self):
        for cwd, active in ((self.existing, False), (self.cwd, True)):
            fake = ThreadRPC(cwd, self.policy, active=active)
            with (
                patch("ai_sessions.harnesses.messaging_rpc.CodexRPC", return_value=fake),
                patch(
                    "ai_sessions.harnesses.messaging_codex.ensure_runtime",
                    return_value={"status": "running"},
                ),
            ):
                with self.assertRaises(BridgeError):
                    self.prepare()
            self.assertEqual([m for m, _ in fake.calls], ["thread/resume"])

    def test_local_launch_keeps_add_dir_and_dry_run_never_connects(self):
        with patch("ai_sessions.harnesses.messaging_rpc.CodexRPC") as connect:
            self.assertEqual(
                self.prepare(["codex", "resume", "supervisor"]),
                ["codex", "resume", "supervisor", "--add-dir=" + self.directory],
            )
            self.assertEqual(self.prepare(dry_run=True), self.command)
        connect.assert_not_called()

    def test_custom_remote_is_not_configured_on_the_local_daemon(self):
        for remote in (["--remote", "ws://localhost:1234"], ["--remote=ws://localhost:1234"]):
            with (
                self.subTest(remote=remote),
                patch("ai_sessions.harnesses.messaging_rpc.CodexRPC") as connect,
            ):
                with self.assertRaisesRegex(BridgeError, "local daemon"):
                    self.prepare(["codex", *remote, "resume", "supervisor"])
                connect.assert_not_called()

    def test_joined_local_remote_is_supported(self):
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        command = ["codex", "--remote=unix://", "resume", "supervisor"]
        self.assertEqual(self.prepare(command), command)
        self.assertIn(self.directory, fake.policy["writableRoots"])

    def test_explicit_read_only_and_conflicting_mode_flags_never_widen_permissions(self):
        cases = [
            (["--sandbox", "read-only"], "workspaceWrite"),
            (["-sread-only"], "workspaceWrite"),
            (["--sandbox=read-only"], "dangerFullAccess"),
        ]
        for flags, policy_type in cases:
            with self.subTest(flags=flags, policy_type=policy_type):
                policy = self.policy if policy_type == "workspaceWrite" else {"type": policy_type}
                fake = ThreadRPC(self.cwd, policy)
                with (
                    patch("ai_sessions.harnesses.messaging_rpc.CodexRPC", return_value=fake),
                    patch(
                        "ai_sessions.harnesses.messaging_codex.ensure_runtime",
                        return_value={"status": "running"},
                    ),
                ):
                    with self.assertRaisesRegex(BridgeError, "permission|read-only"):
                        self.prepare(
                            ["codex", *flags, "--remote", "unix://", "resume", "supervisor"]
                        )
                self.assertNotIn("thread/settings/update", [m for m, _ in fake.calls])

    def test_matching_explicit_workspace_profile_retains_folder_access(self):
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        command = [
            "codex",
            "--sandbox=workspace-write",
            "--remote",
            "unix://",
            "resume",
            "supervisor",
        ]
        self.assertEqual(self.prepare(command), command)
        self.assertIn(self.directory, fake.policy["writableRoots"])

    def test_explicit_dangerous_mode_applies_only_to_selected_thread(self):
        fake = ThreadRPC(self.cwd, self.policy)
        self.connect(fake)
        command = [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--remote",
            "unix://",
            "resume",
            "supervisor",
        ]
        self.assertEqual(self.prepare(command), command)
        self.assertEqual(fake.policy, {"type": "dangerFullAccess"})
        self.assertEqual(fake.approval, "never")

    def test_explicit_workspace_mode_narrows_full_access_and_adds_folder(self):
        fake = ThreadRPC(self.cwd, {"type": "dangerFullAccess"})
        self.connect(fake)
        self.prepare(
            ["codex", "-sworkspace-write", "-anever", "--remote", "unix://", "resume", "supervisor"]
        )
        self.assertEqual(fake.policy["type"], "workspaceWrite")
        self.assertEqual(fake.policy["writableRoots"], [self.directory])
        self.assertFalse(fake.policy["networkAccess"])
        self.assertEqual(fake.approval, "never")

    def test_explicit_approval_change_applies_even_if_folder_already_available(self):
        fake = ThreadRPC(self.cwd, {**self.policy, "writableRoots": [self.directory]})
        self.connect(fake)
        self.prepare(
            ["codex", "--ask-for-approval=never", "--remote", "unix://", "resume", "supervisor"]
        )
        self.assertEqual(fake.approval, "never")

    def test_preparation_attachment_lives_until_terminal_resources_close(self):
        fake = ThreadRPC(self.cwd, self.policy)
        fake.closed = False

        def close(client, *args):
            client.closed = True

        with patch.object(ThreadRPC, "__exit__", close):
            self.connect(fake)
            self.prepare()
            self.assertFalse(fake.closed)
            self.resources.close()
            self.assertTrue(fake.closed)

    def test_all_named_profile_spellings_refuse_unresolved_permission_selection(self):
        for flags in (
            ["-p", "restricted"],
            ["-prestricted"],
            ["-p=restricted"],
            ["--profile", "restricted"],
            ["--profile=restricted"],
        ):
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(BridgeError, "permission profiles"):
                    self.prepare(["codex", *flags, "--remote", "unix://", "resume", "supervisor"])
