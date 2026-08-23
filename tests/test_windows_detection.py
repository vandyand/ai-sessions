import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions.app import Session, detect_open_sessions
from ai_sessions.discovery import HarnessContext
from ai_sessions.liveness import ProcessInfo
from ai_sessions.registry import REGISTRY


class WindowsDetectionTests(unittest.TestCase):
    def test_codex_log_pid_maps_subagent_to_parent(self) -> None:
        # Real Codex logs use Unix epoch seconds in ts and the fractional
        # nanoseconds in ts_nanos (verified against logs_*.sqlite on 2026-08-23).
        process_start = 1_787_482_875.5
        with TemporaryDirectory() as directory:
            root = Path(directory)
            logs = sqlite3.connect(root / "logs_2.sqlite")
            logs.execute(
                "CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, process_uuid TEXT)"
            )
            logs.execute(
                "INSERT INTO logs VALUES (?, 0, 'child', 'pid:42:process-uuid')",
                (1_787_482_876,),
            )
            logs.commit()
            logs.close()

            state = sqlite3.connect(root / "state_5.sqlite")
            state.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)"
            )
            state.execute("INSERT INTO thread_spawn_edges VALUES ('parent', 'child')")
            state.commit()
            state.close()

            item = Session("codex", "parent", "", "", 0, 0, "", False, "storage")
            context = HarnessContext.create()
            context.platform = "win32"
            context.process_snapshot = (
                ProcessInfo(42, "codex.exe", ("codex.exe",), "", started_at=process_start),
            )
            context.liveness_ready = True
            adapter = replace(REGISTRY.get("codex"), home=root)
            with REGISTRY.temporary(adapter):
                detect_open_sessions([item], context=context)

        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 42)

    def test_codex_log_row_before_current_process_start_in_same_second_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            logs = sqlite3.connect(root / "logs_2.sqlite")
            logs.execute(
                "CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, process_uuid TEXT)"
            )
            logs.execute("INSERT INTO logs VALUES (3, 100, 'thread', 'pid:42:old-instance')")
            logs.commit()
            logs.close()

            item = Session("codex", "thread", "", "", 0, 0, "", False, "storage")
            context = HarnessContext.create()
            context.platform = "win32"
            context.process_snapshot = (
                ProcessInfo(42, "codex.exe", ("codex.exe",), "", started_at=3.5),
            )
            context.liveness_ready = True
            adapter = replace(REGISTRY.get("codex"), home=root)
            with REGISTRY.temporary(adapter):
                detect_open_sessions([item], context=context)

        self.assertFalse(item.is_open)
        self.assertEqual(item.open_pid, 0)


if __name__ == "__main__":
    unittest.main()
