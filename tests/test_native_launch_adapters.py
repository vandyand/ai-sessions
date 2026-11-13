"""Native launch identity uses owned process evidence, never catalog recency."""

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import psutil

from ai_sessions.capabilities import Unsupported
from ai_sessions.liveness import _windows_start_token
from ai_sessions.model import NativeRef
from ai_sessions.registry import REGISTRY


class NativeLaunchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.started = 1787482875.5
        self.pid = 42
        self.process = Mock()
        self.process.create_time.return_value = self.started
        self.process.name.return_value = "codex.exe"
        self.process.cmdline.return_value = ["codex.exe", "--remote", "unix://shared"]
        self.enterContext(patch("sys.platform", "win32"))
        self.enterContext(patch("psutil.Process", return_value=self.process))
        self.enterContext(patch("psutil.process_iter", side_effect=AssertionError("global scan")))

    def adapter(self, provider):
        adapter = replace(REGISTRY.get(provider), home=self.root)
        self.enterContext(REGISTRY.temporary(adapter))
        self.assertTrue(
            callable(getattr(adapter, "current_session", None)), "native identity hook missing"
        )
        return adapter

    def codex_logs(self, rows, edges=()):
        with closing(sqlite3.connect(self.root / "logs_2.sqlite")) as db, db:
            db.execute(
                "CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, process_uuid TEXT)"
            )
            db.executemany("INSERT INTO logs VALUES (?, ?, ?, ?)", rows)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)"
            )
            db.executemany("INSERT INTO thread_spawn_edges VALUES (?, ?)", edges)

    def codex_ref(self, sid):
        path = self.root / "sessions" / f"rollout-{sid}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("", encoding="utf-8")
        return NativeRef(sid, str(path))

    def claude_claim(self, name, sid, updated, **overrides):
        record = dict(
            pid=self.pid,
            sessionId=sid,
            procStart=_windows_start_token(self.started),
            kind="interactive",
            updatedAt=updated,
        )
        record.update(overrides)
        path = self.root / "sessions" / f"{name}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    def claude_ref(self, sid):
        path = self.root / "projects" / "fixture" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return NativeRef(sid, str(path))

    def test_optional_hooks_validate_and_default_to_unsupported(self):
        base = REGISTRY.get("opencode")
        self.assertIsInstance(getattr(base, "native_executable", None), Unsupported)
        self.assertIsInstance(base.current_session, Unsupported)
        self.assertEqual(base.restart_discard_options, ())
        for field in ("native_executable", "current_session"):
            for invalid in (None, 42, Unsupported("")):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    replace(base, **{field: invalid})
        for invalid in (["--resume"], ("",), (42,), ("resume",)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(base, restart_discard_options=invalid)

    def test_codex_exact_pid_latest_activity_follows_spawn_parent(self):
        adapter = self.adapter("codex")
        expected = self.codex_ref("019a7c00-0000-7000-8000-000000000001")
        self.codex_logs(
            [
                (1787482875, 600000000, "old", "pid:42:ui"),
                (1787482876, 0, "child", "pid:42:ui"),
                (1787483000, 0, "unrelated", "pid:99:daemon"),
            ],
            [(expected.session_id, "child")],
        )
        self.assertEqual(adapter.current_session(self.pid, self.started), expected)

    def test_codex_rejects_same_second_stale_log_and_unresolved_id(self):
        adapter = self.adapter("codex")
        self.codex_ref("old")
        self.codex_logs([(1787482875, 499999999, "old", "pid:42:old")])
        self.assertIsNone(adapter.current_session(self.pid, self.started))
        with closing(sqlite3.connect(self.root / "logs_2.sqlite")) as db, db:
            db.execute("INSERT INTO logs VALUES (1787482876, 0, 'missing', 'pid:42:new')")
        self.assertIsNone(adapter.current_session(self.pid, self.started))

    def test_codex_refuses_shared_app_server_and_shell_mentions(self):
        adapter = self.adapter("codex")
        self.codex_ref("thread")
        self.codex_logs([(1787482876, 0, "thread", "pid:42:daemon")])
        for name, command in [
            ("codex.exe", ["codex.exe", "app-server", "daemon"]),
            ("pwsh.exe", ["pwsh.exe", "-Command", "codex.exe"]),
        ]:
            with self.subTest(name=name):
                self.process.name.return_value = name
                self.process.cmdline.return_value = command
                self.assertIsNone(adapter.current_session(self.pid, self.started))

    def test_both_providers_refuse_reused_or_unverifiable_pid(self):
        for provider in ("codex", "claude"):
            adapter = self.adapter(provider)
            self.process.name.return_value = f"{provider}.exe"
            self.process.cmdline.return_value = [f"{provider}.exe"]
            if provider == "codex":
                self.codex_ref("current")
                self.codex_logs([(1787482876, 0, "current", "pid:42:ui")])
            else:
                self.claude_ref("current")
                self.claude_claim("current", "current", 20)
            for birth in (self.started + 0.001, 0, float("nan")):
                self.process.create_time.return_value = birth
                with self.subTest(provider=provider, birth=birth):
                    self.assertIsNone(adapter.current_session(self.pid, self.started))
            self.process.create_time.side_effect = psutil.NoSuchProcess(self.pid)
            self.assertIsNone(adapter.current_session(self.pid, self.started))
            self.process.create_time.side_effect = None

    def test_claude_selects_latest_verified_claim_for_owned_pid(self):
        adapter = self.adapter("claude")
        self.process.name.return_value = "claude.exe"
        self.process.cmdline.return_value = ["claude.exe"]
        self.claude_ref("old")
        expected = self.claude_ref("current")
        self.claude_claim("old", "old", 10)
        self.claude_claim("current", "current", 20)
        self.claude_claim("foreign", "foreign", 100, pid=99)
        self.claude_claim(
            "stale", "stale", 200, procStart=str(int(_windows_start_token(self.started)) - 10000)
        )
        self.claude_claim("missing-token", "missing-token", 300, procStart="")
        self.claude_claim("sdk", "sdk", 400, kind="sdk")
        self.assertEqual(adapter.current_session(self.pid, self.started), expected)

    def test_claude_does_not_fall_back_when_latest_claim_is_unresolved(self):
        adapter = self.adapter("claude")
        self.process.name.return_value = "claude.exe"
        self.process.cmdline.return_value = ["claude.exe"]
        self.claude_ref("old")
        self.claude_claim("old", "old", 10)
        self.claude_claim("current", "missing", 20)
        self.assertIsNone(adapter.current_session(self.pid, self.started))

    def test_claude_accepts_filetime_precision_lost_in_psutil_float(self):
        adapter = self.adapter("claude")
        self.process.name.return_value = "claude.exe"
        self.process.cmdline.return_value = ["claude.exe"]
        expected = self.claude_ref("current")
        self.claude_claim(
            "current", "current", 20, procStart=str(int(_windows_start_token(self.started)) + 1)
        )
        self.assertEqual(adapter.current_session(self.pid, self.started), expected)

    def test_claude_latest_claim_without_session_id_does_not_reuse_old_identity(self):
        adapter = self.adapter("claude")
        self.process.name.return_value = "claude.exe"
        self.process.cmdline.return_value = ["claude.exe"]
        self.claude_ref("old")
        self.claude_claim("old", "old", 10)
        self.claude_claim("current", "", 20)
        self.assertIsNone(adapter.current_session(self.pid, self.started))

    def test_claude_equally_recent_conflicting_claims_are_not_guessed(self):
        adapter = self.adapter("claude")
        self.process.name.return_value = "claude.exe"
        self.process.cmdline.return_value = ["claude.exe"]
        for sid in ("one", "two"):
            self.claude_ref(sid)
            self.claude_claim(sid, sid, 20)
        self.assertIsNone(adapter.current_session(self.pid, self.started))

    def test_both_providers_reject_generation_change_during_lookup(self):
        for provider in ("codex", "claude"):
            adapter = self.adapter(provider)
            self.process.name.return_value = f"{provider}.exe"
            self.process.cmdline.return_value = [f"{provider}.exe"]
            self.process.create_time.side_effect = [self.started, self.started + 0.001]
            if provider == "codex":
                self.codex_ref("current")
                self.codex_logs([(1787482876, 0, "current", "pid:42:ui")])
            else:
                self.claude_ref("current")
                self.claude_claim("current", "current", 20)
            with self.subTest(provider=provider):
                self.assertIsNone(adapter.current_session(self.pid, self.started))
            self.process.create_time.side_effect = None

    def test_claude_native_executable_prefers_path_exe_then_npm_neighbor(self):
        adapter = self.adapter("claude")
        native = self.root / "claude.exe"
        native.touch()
        shim = self.root / "claude.cmd"
        shim.touch()
        bundled = self.root / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        bundled.parent.mkdir(parents=True)
        bundled.touch()
        with patch(
            "shutil.which", side_effect=lambda name: str(native if name == "claude.exe" else shim)
        ):
            self.assertEqual(adapter.native_executable(), str(native))
        with patch(
            "shutil.which", side_effect=lambda name: str(shim) if name == "claude" else None
        ):
            self.assertEqual(adapter.native_executable(), str(bundled))
        with patch("shutil.which", return_value=None), self.assertRaises(OSError):
            adapter.native_executable()


if __name__ == "__main__":
    unittest.main()
