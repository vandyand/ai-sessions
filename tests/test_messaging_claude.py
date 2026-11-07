"""Claude adapter contracts; no live sessions or vault writes in unit tests."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.harnesses import messaging_claude as claude


class ClaudeMessagingTests(unittest.TestCase):
    def setUp(self):
        from ai_sessions.messaging import store

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.start_patch(patch.object(store, "ROOT", Path(temporary.name)))
        self.calls = []
        self.owner = Mock(pid=42)
        self.owner.name.return_value = "claude.exe"
        self.owner.cmdline.return_value = ["claude.exe"]
        self.owner.create_time.return_value = 1234.5
        process = Mock()
        process.parents.return_value = [self.owner]
        self.start_patch(patch.object(claude.sys, "platform", "win32"))
        self.start_patch(patch.object(claude.psutil, "Process", return_value=process))
        self.run = self.start_patch(patch.object(claude.subprocess, "run", side_effect=self.helper))
        self.start_patch(
            patch.dict(claude.os.environ, {"CLAUDE_CODE_MESSAGING_TOKEN": "secret-canary"})
        )

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def helper(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"status":"ok"}', stderr="")

    def register(self, **env):
        return claude.register(
            {"session_id": "session-1", "cwd": "C:/work"},
            {
                "CLAUDE_CODE_MESSAGING_TOKEN": "secret-canary",
                "CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\native",
                "USERPROFILE": "C:/Users/test",
                **env,
            },
        )

    def response(self, stdout, code=0):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(
            returncode=code, stdout=stdout, stderr="secret-canary"
        )

    def test_registration_owner_and_stdin(self):
        receiver = self.register()
        self.assertEqual(receiver["pid"], 42)
        self.assertEqual(receiver["started"], 1234.5)
        self.assertEqual(receiver["tool"], "claude")
        self.assertEqual(receiver["session_id"], "session-1")
        self.assertNotIn("secret-canary", repr(receiver))
        args, kwargs = self.calls[0]
        self.assertNotIn("secret-canary", repr(args))
        self.assertEqual(json.loads(kwargs["input"])["token"], "secret-canary")
        self.assertNotIn("CLAUDE_CODE_MESSAGING_TOKEN", kwargs["env"])
        self.assertEqual(self.register()["vault_name"], receiver["vault_name"])
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_generation_changes_for_reused_pid(self):
        first = self.register()
        self.owner.create_time.return_value = 9999
        self.assertNotEqual(self.register()["vault_name"], first["vault_name"])

    def test_no_registration_without_claude_ancestor(self):
        self.owner.name.return_value = "python.exe"
        self.owner.cmdline.return_value = ["python.exe", "claude-hook.py"]
        self.assertIsNone(self.register())
        self.assertEqual(self.calls, [])

    def test_missing_token_does_not_store(self):
        self.assertIsNone(claude.register({"session_id": "s"}, {}))
        self.assertEqual(self.calls, [])

    def test_send_submits_text_as_data(self):
        receiver = self.register()
        sender = {"tool": "codex", "session_id": "source"}
        text = 'hello $(bad) "world"'
        result = claude.send(receiver, text, "m-1", sender)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["message_id"], "m-1")
        self.assertEqual(result["recipient"], {"tool": "claude", "session_id": "session-1"})
        request = json.loads(self.calls[-1][1]["input"])
        self.assertEqual(request["text"], text)
        self.assertEqual(request["operation"], "send")
        self.assertNotIn("secret-canary", repr(request))

    def test_helper_status_preserved(self):
        receiver = self.register()
        for status in ["held", "refused", "unavailable", "uncertain"]:
            with self.subTest(status=status):
                self.response(json.dumps({"status": status}))
                self.assertEqual(claude.send(receiver, "x", "m", {})["status"], status)

    def test_subprocess_errors_redacted(self):
        receiver = self.register()
        self.run.side_effect = OSError("secret-canary")
        self.assertIsNone(self.register())
        result = claude.send(receiver, "x", "m", {})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret-canary", repr(result))

    def test_forget_only_exact_owned_key(self):
        receiver = self.register()
        count = len(self.calls)
        claude.forget({**receiver, "vault_name": "other-secret"})
        claude.forget({**receiver, "session_id": "other"})
        self.assertEqual(len(self.calls), count)
        claude.forget(receiver)
        self.assertEqual(json.loads(self.calls[-1][1]["input"])["operation"], "remove")

    def test_non_windows_unavailable(self):
        with patch.object(claude.sys, "platform", "linux"):
            self.assertIsNone(self.register())
            result = claude.send({}, "x", "m", {})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Windows", result["reason"])
        self.assertEqual(self.calls, [])

    def test_timeout_is_uncertain_without_retry(self):
        receiver = self.register()
        self.run.reset_mock()
        self.run.side_effect = subprocess.TimeoutExpired("secret-canary", 15)
        result = claude.send(receiver, "x", "m", {})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.run.call_count, 1)
        self.assertNotIn("secret-canary", repr(result))

    def test_lost_send_result_is_uncertain(self):
        receiver = self.register()
        for stdout, code in [("not-json secret-canary", 0), ("", 1)]:
            with self.subTest(stdout=stdout, code=code):
                self.response(stdout, code)
                result = claude.send(receiver, "x", "m", {})
                self.assertEqual(result["status"], "uncertain")
                self.assertNotIn("secret-canary", repr(result))

    def test_address_environment_and_node_owner(self):
        self.owner.name.return_value = "node.exe"
        self.owner.cmdline.return_value = [
            "node.exe",
            r"C:\tools\node_modules\@anthropic-ai\claude-code\cli.js",
        ]
        result = self.register(CLAUDE_CONFIG_DIR=r"C:\custom-home")
        self.assertEqual(result["pid"], 42)
        self.assertEqual(result["home"], r"C:\custom-home")

    def test_unavailable_vault_prevents_registration(self):
        self.response('{"status":"unavailable"}')
        self.assertIsNone(self.register())

    def test_uds_prefixed_socket(self):
        result = self.register(CLAUDE_CODE_MESSAGING_SOCKET=r"uds:\\.\pipe\native")
        self.assertIsNotNone(result)
        self.assertEqual(result["address"], r"\\.\pipe\native")


if __name__ == "__main__":
    unittest.main()
