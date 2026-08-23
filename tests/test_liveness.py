import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from ai_sessions import diagnostics, liveness
from ai_sessions.app import Session, detect_open_sessions
from ai_sessions.discovery import HarnessContext
from ai_sessions.liveness import LivenessContext, ProcessInfo, populate_context
from ai_sessions.model import SourceKind
from ai_sessions.registry import REGISTRY


class LivenessTests(unittest.TestCase):
    def context(self, platform: str, *processes: ProcessInfo) -> HarnessContext:
        context = HarnessContext.create()
        context.platform = platform
        context.process_snapshot = processes
        context.liveness_ready = True
        return context

    def test_shared_snapshot_is_populated_only_once_even_when_empty(self) -> None:
        context = HarnessContext.create()
        with (
            patch("ai_sessions.liveness.process_snapshot", return_value=()) as processes,
            patch("ai_sessions.liveness.held_file_locks", return_value={}) as locks,
        ):
            populate_context(context)
            populate_context(context)
        processes.assert_called_once_with(context.platform)
        locks.assert_called_once_with(context.platform)

    def test_windows_process_snapshot_emits_filetime_start_token(self) -> None:
        process = type(
            "FakeProcess",
            (),
            {
                "info": {
                    "pid": 7,
                    "name": "claude.exe",
                    "cmdline": ["claude.exe"],
                    "create_time": 1.25,
                }
            },
        )()
        with patch.object(liveness.psutil, "process_iter", return_value=[process]):
            snapshot = liveness.process_snapshot("win32")
        self.assertEqual(
            snapshot[0].start_token,
            str(liveness.WINDOWS_EPOCH_FILETIME + 12_500_000),
        )

    def test_windows_process_snapshot_marks_missing_create_time_unverifiable(self) -> None:
        process = type(
            "FakeProcess",
            (),
            {
                "info": {
                    "pid": 7,
                    "name": "claude.exe",
                    "cmdline": ["claude.exe"],
                    "create_time": None,
                }
            },
        )()
        with patch.object(liveness.psutil, "process_iter", return_value=[process]):
            snapshot = liveness.process_snapshot("win32")
        self.assertEqual(snapshot[0].started_at, 0)
        self.assertEqual(snapshot[0].start_token, "")

    def test_claude_windows_registry_uses_shared_start_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry = home / "sessions"
            registry.mkdir()
            (registry / "live.json").write_text(
                json.dumps(
                    {
                        "pid": 41,
                        "sessionId": "live",
                        "procStart": "116444736010000000",
                        "kind": "interactive",
                    }
                ),
                encoding="utf-8",
            )
            item = Session("claude", "live", "", "", 0, 0, "", False, "storage")
            context = self.context(
                "win32",
                ProcessInfo(41, "claude.exe", ("claude.exe",), "116444736010000001"),
            )
            with REGISTRY.temporary(replace(REGISTRY.get("claude"), home=home)):
                detect_open_sessions([item], context=context)
        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 41)

    def test_claude_linux_registry_rejects_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry = home / "sessions"
            registry.mkdir()
            (registry / "stale.json").write_text(
                json.dumps(
                    {
                        "pid": 41,
                        "sessionId": "stale",
                        "procStart": "old-token",
                        "kind": "interactive",
                    }
                ),
                encoding="utf-8",
            )
            item = Session("claude", "stale", "", "", 0, 0, "", False, "storage")
            context = self.context("linux", ProcessInfo(41, "claude", ("claude",), "new-token"))
            with REGISTRY.temporary(replace(REGISTRY.get("claude"), home=home)):
                detect_open_sessions([item], context=context)
        self.assertFalse(item.is_open)
        self.assertEqual(item.open_pid, 0)

    def test_claude_windows_missing_token_requires_executable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry = home / "sessions"
            registry.mkdir()
            (registry / "legacy.json").write_text(
                json.dumps({"pid": 41, "sessionId": "legacy", "kind": "interactive"}),
                encoding="utf-8",
            )
            item = Session("claude", "legacy", "", "", 0, 0, "", False, "storage")
            unrelated = self.context(
                "win32",
                ProcessInfo(
                    41,
                    "python.exe",
                    ("python.exe", "claude_bench.py"),
                    "token",
                    started_at=1,
                ),
            )
            adapter = replace(REGISTRY.get("claude"), home=home)
            with REGISTRY.temporary(adapter):
                detect_open_sessions([item], context=unrelated)
                self.assertFalse(item.is_open)
                actual = self.context(
                    "win32",
                    ProcessInfo(
                        41,
                        "node.exe",
                        ("C:/tools/claude.cmd",),
                        "token",
                        started_at=1,
                    ),
                )
                detect_open_sessions([item], context=actual)
        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 41)

    def test_claude_unverifiable_start_warns_only_for_eligible_interactive_row(self) -> None:
        diagnostics.clear_warnings()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry = home / "sessions"
            registry.mkdir()
            foreign = registry / "foreign.json"
            foreign.write_text(
                json.dumps(
                    {
                        "pid": 41,
                        "sessionId": "foreign",
                        "procStart": "expected",
                        "kind": "interactive",
                    }
                ),
                encoding="utf-8",
            )
            item = Session("claude", "eligible", "", "", 0, 0, "", False, "storage")
            context = self.context("win32", ProcessInfo(41, "claude.exe", ("claude.exe",), ""))
            adapter = replace(REGISTRY.get("claude"), home=home)
            with REGISTRY.temporary(adapter):
                detect_open_sessions([item], context=context)
                self.assertEqual(diagnostics.warnings(), [])
                foreign.write_text(
                    json.dumps(
                        {
                            "pid": 41,
                            "sessionId": "eligible",
                            "procStart": "expected",
                            "kind": "interactive",
                        }
                    ),
                    encoding="utf-8",
                )
                detect_open_sessions([item], context=context)
        self.assertTrue(
            any("could not verify Claude process start" in note for note in diagnostics.warnings())
        )

    @unittest.skipIf(os.name == "nt", "Linux lock identity uses POSIX stat device fields")
    def test_codex_linux_writer_lock_uses_shared_lock_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            locks = home / "thread-writer-locks"
            locks.mkdir()
            lock = locks / "live.lock"
            lock.touch()
            stat = lock.stat()
            identity = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
            item = Session("codex", "live", "", "", 0, 0, "", False, "storage")
            context = self.context("linux", ProcessInfo(73, "codex", ("codex",), "token"))
            context.lock_map = {identity: 73}
            with REGISTRY.temporary(replace(REGISTRY.get("codex"), home=home)):
                detect_open_sessions([item], context=context)
        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 73)

    def test_hook_receives_only_immutable_own_rows_and_core_validates_mapping(self) -> None:
        observed: list[str] = []
        observed_homes: list[Path] = []
        mutation_blocked = False
        context_mutation_blocked = False

        def inspect(context: LivenessContext, home: Path, rows: object) -> dict[str, int]:
            nonlocal context_mutation_blocked, mutation_blocked
            owned = list(rows)  # type: ignore[arg-type]
            observed.extend(item.session_id for item in owned)
            observed_homes.append(home)
            try:
                owned[0].session_id = "foreign"
            except FrozenInstanceError:
                mutation_blocked = True
            try:
                context.lock_map[(0, 0, 0)] = 999  # type: ignore[index]
            except TypeError:
                context_mutation_blocked = True
            return {"own": 91, "foreign": 91, "noninteractive": 91, "ghost": 999}

        fake = replace(
            REGISTRY.get("codex"),
            name="fake",
            order=30,
            home=Path("adapter-home"),
            inspect_liveness=inspect,
        )
        own = Session("fake", "own", "", "", 0, 0, "", False, "storage", source="interactive")
        noninteractive = Session(
            "fake",
            "noninteractive",
            "",
            "",
            0,
            0,
            "",
            False,
            "storage",
            source=SourceKind.NON_INTERACTIVE,
        )
        foreign = Session("codex", "foreign", "", "", 0, 0, "", False, "storage")
        context = self.context("linux", ProcessInfo(91, "fake", ("fake",), "token"))
        with REGISTRY.temporary(fake):
            detect_open_sessions([own, noninteractive, foreign], context=context)
        self.assertEqual(observed, ["own"])
        self.assertEqual(observed_homes, [Path("adapter-home")])
        self.assertTrue(mutation_blocked)
        self.assertTrue(context_mutation_blocked)
        self.assertTrue(own.is_open)
        self.assertEqual(own.open_pid, 91)
        self.assertFalse(noninteractive.is_open)
        self.assertFalse(foreign.is_open)

    def test_hook_failures_and_invalid_results_are_isolated(self) -> None:
        diagnostics.clear_warnings()
        context = self.context("linux", ProcessInfo(91, "fake", ("fake",), "token"))
        item = Session("fake", "own", "", "", 0, 0, "", False, "storage")
        item.is_open = True
        item.open_pid = 12

        def raises(*_args: object) -> dict[str, int]:
            raise RuntimeError("broken hook")

        base = REGISTRY.get("codex")
        failing = replace(base, name="fake", order=30, inspect_liveness=raises)
        with REGISTRY.temporary(failing):
            detect_open_sessions([item], context=context)
        self.assertFalse(item.is_open)
        self.assertTrue(any("broken hook" in note for note in diagnostics.warnings()))

        diagnostics.clear_warnings()
        item.is_open = True
        item.open_pid = 12
        invalid = replace(
            base,
            name="fake",
            order=30,
            inspect_liveness=lambda *_args: [91],
        )
        with REGISTRY.temporary(invalid):
            detect_open_sessions([item], context=context)
        self.assertFalse(item.is_open)
        self.assertTrue(any("did not return a mapping" in note for note in diagnostics.warnings()))

    def test_no_interactive_rows_avoids_host_snapshot(self) -> None:
        item = Session(
            "codex",
            "agent",
            "",
            "",
            0,
            0,
            "",
            False,
            "storage",
            source=SourceKind.NON_INTERACTIVE,
        )
        with patch("ai_sessions.app.populate_liveness_context") as populate:
            detect_open_sessions([item])
        populate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
