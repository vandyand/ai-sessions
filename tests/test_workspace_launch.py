import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_sessions.workspaces.launch import launch_slot, remember_launch
from ai_sessions.workspaces.store import WorkspaceStore


class WorkspaceLaunchTests(unittest.TestCase):
    def test_cross_harness_binding_uses_target_storage(self):
        from ai_sessions.model import NativeRef
        from ai_sessions.workspaces.launch import _binding

        item = SimpleNamespace(
            tool="source",
            session_id="source-id",
            title="Worker",
            cwd="",
            storage="source.jsonl",
            launch_target=lambda tool: "target-id",
        )
        adapter = SimpleNamespace(
            home=Path.home(), resolve=lambda value: NativeRef(value, "target.jsonl")
        )
        with patch("ai_sessions.workspaces.launch.REGISTRY") as registry:
            registry.get.return_value = adapter
            binding = _binding(item, "target")
        self.assertEqual(binding["session_id"], "target-id")
        self.assertEqual(binding["storage"], "target.jsonl")

    def test_real_launcher_records_success_and_releases_after_child_exit(self):
        from ai_sessions import app, detached
        from ai_sessions.model import Session

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workspaces.sqlite3"
            store = WorkspaceStore(path)
            store.save("test", [{"action": "newTab", "slot": "pane-0"}])
            item = Session("test", "native", "Worker", temp, 0, 0, "", False, "native.jsonl")
            env = {
                "AI_SESSIONS_WORKSPACE": "test",
                "AI_SESSIONS_WORKSPACE_PANE": "pane-0",
                "AI_SESSIONS_WORKSPACE_FILE": str(path),
            }
            remember = detached.remember
            with (
                patch.dict(os.environ, env),
                patch.object(app, "active_launch_tool", return_value="test"),
                patch.object(
                    app.REGISTRY,
                    "get",
                    return_value=SimpleNamespace(home=Path(temp), contain_launch=False),
                ),
                patch("ai_sessions.messaging.launch.prepare"),
                patch.object(
                    detached,
                    "remember",
                    side_effect=lambda *args: remember(*args, path=Path(temp) / "terminals.json"),
                ),
                patch.object(detached, "mark_closed", return_value=None),
                patch.object(detached, "terminal_id", return_value=""),
            ):
                self.assertEqual(app.run_harness(item, [sys.executable, "-c", "pass"]), 0)
                self.assertEqual(store.get("test")["panes"]["pane-0"]["session_id"], "native")
                with launch_slot(item, "test"):
                    pass

    def test_managed_make_room_never_stops_live_session(self):
        from ai_sessions import app
        from ai_sessions.model import Session

        item = Session("test", "id", "Test", "", 0, 0, "", False, "native")

        def opened(items):
            item.is_open = True
            item.open_pid = 123
            return True

        with (
            patch.dict(os.environ, {"AI_SESSIONS_WORKSPACE": "test"}),
            patch.object(app, "detect_open_sessions", side_effect=opened),
            patch("ai_sessions.platforms.windows.console_pids", return_value=[]),
            patch("ai_sessions.detached.detached_sessions", return_value=[]),
            patch("ai_sessions.platforms.windows.stop_process_tree") as stop,
            patch.object(app, "ask") as ask,
        ):
            self.assertFalse(app.make_room(item, [item]))
            stop.assert_not_called()
            ask.assert_not_called()

    def test_only_successful_launch_updates_binding_and_lease_is_released(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workspaces.sqlite3"
            store = WorkspaceStore(path)
            store.save("test", [{"action": "newTab", "slot": "pane-1"}])
            session = SimpleNamespace(
                tool="test",
                session_id="native",
                title="Title",
                cwd=temp,
                storage="",
                launch_target=lambda tool: "native",
            )
            env = {
                "AI_SESSIONS_WORKSPACE": "test",
                "AI_SESSIONS_WORKSPACE_PANE": "pane-1",
                "AI_SESSIONS_WORKSPACE_FILE": str(path),
            }
            with (
                patch.dict(os.environ, env),
                patch("ai_sessions.workspaces.launch.REGISTRY") as registry,
            ):
                registry.get.return_value.home = Path(temp)
                with launch_slot(session, "test"):
                    self.assertIsNone(store.get("test")["panes"]["pane-1"])
                    remember_launch(session, "test")
                self.assertEqual(store.get("test")["panes"]["pane-1"]["session_id"], "native")
                with launch_slot(session, "test"):
                    pass

    def test_unmanaged_launch_does_not_open_store(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ai_sessions.workspaces.launch.WorkspaceStore") as store,
        ):
            with launch_slot(None, "test"):
                remember_launch(None, "test")
            store.assert_not_called()
