import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ai_sessions.platforms import windows


class FakeProcess:
    def __init__(self, pid: int, name: str = "codex.exe") -> None:
        self.info = {"pid": pid, "name": name, "cmdline": [name]}


class WindowsDetectionTests(unittest.TestCase):
    def test_codex_log_pid_maps_subagent_to_parent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            logs = sqlite3.connect(root / "logs_2.sqlite")
            logs.execute(
                "CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, process_uuid TEXT)"
            )
            logs.execute("INSERT INTO logs VALUES (2, 0, 'child', 'pid:42:process-uuid')")
            logs.commit()
            logs.close()

            state = sqlite3.connect(root / "state_5.sqlite")
            state.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)"
            )
            state.execute("INSERT INTO thread_spawn_edges VALUES ('parent', 'child')")
            state.commit()
            state.close()

            item = SimpleNamespace(
                tool="codex",
                session_id="parent",
                source="interactive",
                is_open=False,
                open_pid=0,
            )
            with patch.object(windows.psutil, "process_iter", return_value=[FakeProcess(42)]):
                windows.detect_open_sessions([item], root / "claude", root)

        self.assertTrue(item.is_open)
        self.assertEqual(item.open_pid, 42)


if __name__ == "__main__":
    unittest.main()
