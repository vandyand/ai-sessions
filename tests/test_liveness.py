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
        processes.assert_called_once_with(
            context.platform,
            start_time_executables=context.liveness_executables,
        )
        locks.assert_called_once_with(context.platform)

    def test_selective_snapshot_queries_start_only_for_declared_executables(self) -> None:
        class FakeProcess:
            def __init__(self, pid: int, name: str, command: list[str]) -> None:
                self.info = {"pid": pid, "name": name, "cmdline": command}
                self.start_queries = 0

            def create_time(self) -> float:
                self.start_queries += 1
                return 1.25

        unrelated = FakeProcess(7, "python.exe", ["python.exe", "worker.py"])
        wrapped = FakeProcess(8, "node.exe", ["node.exe", "C:/tools/opencode.cmd"])
        with patch.object(liveness.psutil, "process_iter", return_value=[unrelated, wrapped]):
            snapshot = liveness.process_snapshot(
                "win32",
                start_time_executables=frozenset(("opencode.cmd",)),
            )
        self.assertEqual(unrelated.start_queries, 0)
        self.assertEqual(snapshot[0].start_token, "")
        self.assertEqual(wrapped.start_queries, 1)
        self.assertNotEqual(snapshot[1].start_token, "")

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

    def test_linux_process_snapshot_includes_name_command_and_start_evidence(self) -> None:
        process = type(
            "FakeProcess",
            (),
            {
                "info": {
                    "pid": 7,
                    "name": "opencode",
                    "cmdline": ["/usr/bin/opencode", "--session", "ses_" + "A" * 26],
                    "create_time": 1.25,
                }
            },
        )()
        with (
            patch.object(liveness.psutil, "process_iter", return_value=[process]),
            patch.object(liveness, "process_start_token", return_value="start-token"),
        ):
            snapshot = liveness.process_snapshot("linux")
        self.assertEqual(snapshot[0].name, "opencode")
        self.assertEqual(snapshot[0].command[0], "/usr/bin/opencode")
        self.assertEqual(snapshot[0].start_token, "start-token")

    def test_process_snapshot_retains_partial_results_when_iterator_raises(self) -> None:
        first = type(
            "FakeProcess",
            (),
            {"info": {"pid": 1, "name": None, "cmdline": None, "create_time": None}},
        )()
        second = type(
            "FakeProcess",
            (),
            {"info": {"pid": 2, "name": "opencode", "cmdline": [], "create_time": 1}},
        )()

        class BrokenIterator:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.index += 1
                if self.index == 1:
                    return first
                if self.index == 2:
                    raise NotImplementedError("one inaccessible process")
                if self.index == 3:
                    return second
                raise StopIteration

        with (
            patch.object(liveness.psutil, "process_iter", return_value=BrokenIterator()),
            patch.object(liveness, "process_start_token", side_effect=("one", "two")),
        ):
            snapshot = liveness.process_snapshot("linux")
        self.assertEqual([item.pid for item in snapshot], [1, 2])
        self.assertEqual((snapshot[0].name, snapshot[0].command), ("", ()))

    def test_opencode_liveness_requires_exact_identity_session_and_start(self) -> None:
        root = "ses_" + "A" * 26
        child = "ses_" + "b" * 26
        ambiguous = "ses_" + "C" * 26
        duplicate = "ses_" + "D" * 26
        inline = "ses_" + "E" * 26
        rows = [
            Session("opencode", root, "", "", 0, 0, "", False, "storage"),
            Session(
                "opencode",
                child,
                "",
                "",
                0,
                0,
                "",
                False,
                "storage",
                source=SourceKind.SUBAGENT,
            ),
            Session("opencode", ambiguous, "", "", 0, 0, "", False, "storage"),
            Session("opencode", duplicate, "", "", 0, 0, "", False, "storage"),
            Session("opencode", inline, "", "", 0, 0, "", False, "storage"),
        ]
        context = self.context(
            "linux",
            ProcessInfo(10, "opencode", ("opencode", "--session", root), "one"),
            ProcessInfo(
                11,
                "node",
                ("node", "/opt/opencode", f"-s={child}"),
                "two",
            ),
            ProcessInfo(
                12,
                "opencode",
                ("opencode", "--session", ambiguous, "-s", root),
                "three",
            ),
            ProcessInfo(13, "python", ("python", "app.py", "--session", ambiguous), "four"),
            ProcessInfo(14, "opencode", ("opencode", "--session", ambiguous), ""),
            ProcessInfo(15, "opencode", ("opencode",), "bare"),
            ProcessInfo(16, "vim", ("vim", "opencode", "--session", ambiguous), "spoof"),
            ProcessInfo(17, "sudo", ("sudo", "opencode", "--session", ambiguous), "spoof"),
            ProcessInfo(
                18,
                "cmd.exe",
                ("cmd.exe", "/c", "opencode.cmd", "--session", duplicate),
                "wrapper",
            ),
            ProcessInfo(19, "opencode", ("opencode", "-s" + duplicate), "native"),
            ProcessInfo(20, "opencode", ("opencode", "--session=" + inline), "inline"),
        )
        detect_open_sessions(rows, context=context)
        self.assertEqual((rows[0].is_open, rows[0].open_pid), (True, 10))
        self.assertEqual((rows[1].is_open, rows[1].open_pid), (True, 11))
        self.assertEqual((rows[2].is_open, rows[2].open_pid), (False, 0))
        self.assertEqual((rows[3].is_open, rows[3].open_pid), (True, 19))
        self.assertEqual((rows[4].is_open, rows[4].open_pid), (True, 20))

    def test_opencode_liveness_handles_windows_command_wrappers(self) -> None:
        session_id = "ses_" + "z" * 26
        item = Session("opencode", session_id, "", "", 0, 0, "", False, "storage")
        context = self.context(
            "win32",
            ProcessInfo(
                44,
                "cmd.exe",
                (
                    "C:\\Windows\\System32\\cmd.exe",
                    "/c",
                    "C:\\Users\\vandy\\AppData\\Roaming\\npm\\opencode.cmd",
                    "--session",
                    session_id,
                ),
                "verified-start",
                started_at=1,
            ),
        )
        detect_open_sessions([item], context=context)
        self.assertEqual((item.is_open, item.open_pid), (True, 44))

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

    def claimed(self, directory: str, *claims: dict[str, object]) -> Session:
        """Run detection over a registry holding the given entries."""
        home = Path(directory)
        registry = home / "sessions"
        registry.mkdir()
        for claim in claims:
            pid = int(str(claim["pid"]))
            record = {
                "sessionId": "shared",
                "kind": "interactive",
                "procStart": f"start-{pid}",
                **claim,
            }
            (registry / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")
        item = Session("claude", "shared", "", "", 0, 0, "", False, "storage")
        processes = tuple(
            ProcessInfo(
                int(str(claim["pid"])), "claude", ("claude",), f"start-{int(str(claim['pid']))}"
            )
            for claim in claims
        )
        context = self.context("linux", *processes)
        with REGISTRY.temporary(replace(REGISTRY.get("claude"), home=home)):
            detect_open_sessions([item], context=context)
        return item

    def test_the_still_refreshed_claim_wins_when_two_processes_claim_one_session(self) -> None:
        # A process that moves to another session without rewriting its entry
        # keeps claiming the old one, and picking the wrong claimant focuses
        # an unrelated terminal.
        diagnostics.clear_warnings()
        with tempfile.TemporaryDirectory() as directory:
            item = self.claimed(
                directory,
                {"pid": 1135153, "updatedAt": 1787282562349},
                {"pid": 2974016, "updatedAt": 1787605966685},
            )
        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 2974016)
        collision = [w for w in diagnostics.warnings() if "claim session shared" in w]
        self.assertEqual(len(collision), 1)
        self.assertIn("PID 2974016, not 1135153", collision[0])

    def test_the_abandoned_claim_loses_regardless_of_file_ordering(self) -> None:
        # Entries are named after the PID, so sort order is unrelated to which
        # claim is current; the freshest must win from either arrangement.
        for stale, live in ((11, 22), (22, 11)):
            with self.subTest(stale=stale, live=live), tempfile.TemporaryDirectory() as directory:
                item = self.claimed(
                    directory,
                    {"pid": stale, "updatedAt": 1_000},
                    {"pid": live, "updatedAt": 9_000},
                )
                self.assertEqual(item.open_pid, live)

    def test_equally_fresh_claims_resolve_the_same_way_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self.claimed(
                directory, {"pid": 42, "updatedAt": 5_000}, {"pid": 7, "updatedAt": 5_000}
            )
        with tempfile.TemporaryDirectory() as directory:
            second = self.claimed(
                directory, {"pid": 7, "updatedAt": 5_000}, {"pid": 42, "updatedAt": 5_000}
            )
        self.assertEqual(first.open_pid, second.open_pid)

    def test_a_single_claim_is_used_without_complaint(self) -> None:
        diagnostics.clear_warnings()
        with tempfile.TemporaryDirectory() as directory:
            item = self.claimed(directory, {"pid": 99, "updatedAt": 5_000})
        self.assertEqual(item.open_pid, 99)
        self.assertEqual([w for w in diagnostics.warnings() if "claim session" in w], [])

    def test_a_claim_without_timestamps_falls_back_to_its_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "sessions"
            registry.mkdir()
            path = registry / "5.json"
            path.write_text(json.dumps({"pid": 5, "sessionId": "s"}), encoding="utf-8")
            os.utime(path, (1_000, 1_000))
            from ai_sessions.harnesses import claude as claude_harness

            self.assertEqual(claude_harness._claim_refreshed_at({"pid": 5}, path), 1_000_000.0)
            self.assertEqual(claude_harness._claim_refreshed_at({"startedAt": 42}, path), 42.0)
            # A boolean is not a timestamp, and must not be read as one.
            self.assertEqual(
                claude_harness._claim_refreshed_at({"updatedAt": True}, path), 1_000_000.0
            )

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
        claude_subagent = Session(
            "claude",
            "agent",
            "",
            "",
            0,
            0,
            "",
            False,
            "storage",
            source=SourceKind.SUBAGENT,
        )
        with patch("ai_sessions.app.populate_liveness_context") as populate:
            detect_open_sessions([item, claude_subagent])
        populate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
