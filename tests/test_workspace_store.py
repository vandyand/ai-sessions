import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psutil

from ai_sessions.workspaces.store import WorkspaceStore


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkspaceStore(Path(self.temp.name) / "workspaces.sqlite3")
        self.layout = [
            {"action": "newTab", "slot": "pane-1", "cwd": self.temp.name},
            {"action": "splitPane", "slot": "pane-2", "split": "right", "size": 0.5},
        ]
        self.binding = {"tool": "test", "session_id": "id-1", "home": self.temp.name}
        self.store.save("project", self.layout)

    def test_bindings_survive_reopen_and_clone_clears_them(self):
        self.store.bind("project", "pane-1", self.binding)
        self.store.clone("project", "other")
        reopened = WorkspaceStore(self.store.path)
        self.assertEqual(reopened.get("project")["panes"]["pane-1"]["session_id"], "id-1")
        self.assertEqual(reopened.get("other")["layout"], reopened.get("project")["layout"])
        self.assertEqual(reopened.get("other")["panes"], {"pane-1": None, "pane-2": None})

    def test_duplicate_binding_requires_explicit_move(self):
        self.store.bind("project", "pane-1", self.binding)
        with self.assertRaises(ValueError):
            self.store.bind("project", "pane-2", self.binding)
        self.store.bind("project", "pane-2", self.binding, move=True)
        self.assertIsNone(self.store.get("project")["panes"]["pane-1"])

    def test_live_slot_and_identity_are_exclusive_across_connections(self):
        started = psutil.Process().create_time()
        token = self.store.acquire("project", "pane-1", self.binding, os.getpid(), started)
        other = WorkspaceStore(self.store.path)
        with self.assertRaises(ValueError):
            other.acquire("project", "pane-2", self.binding, os.getpid(), started)
        with self.assertRaises(ValueError):
            other.acquire(
                "project", "pane-1", {**self.binding, "session_id": "id-2"}, os.getpid(), started
            )
        other.release("wrong-token")
        self.store.release(token)
        other.release(other.acquire("project", "pane-2", self.binding, os.getpid(), started))

    def test_reused_pid_does_not_keep_a_stale_lease(self):
        self.store.acquire("project", "pane-1", self.binding, os.getpid(), 1.0)
        token = self.store.acquire(
            "project", "pane-1", self.binding, os.getpid(), psutil.Process().create_time()
        )
        self.store.release(token)

    def test_simultaneous_claim_has_one_winner(self):
        barrier = Barrier(2)
        started = psutil.Process().create_time()

        def claim(slot):
            store = WorkspaceStore(self.store.path)
            barrier.wait(timeout=5)
            try:
                return store.acquire("project", slot, self.binding, os.getpid(), started)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, ["pane-1", "pane-2"]))
        winners = [token for token in tokens if token]
        self.assertEqual(len(winners), 1)
        self.store.release(winners[0])

    def test_invalid_geometry_is_not_saved(self):
        with self.assertRaises(ValueError):
            self.store.save(
                "bad", [*self.layout, {"action": "splitPane", "slot": "pane-3", "size": 2}]
            )

    def test_rejects_duplicate_save_invalid_slot_and_command_binding(self):
        with self.assertRaises(ValueError):
            self.store.save("project", self.layout)
        with self.assertRaises(ValueError):
            self.store.bind("project", "missing", self.binding)
        with self.assertRaises(ValueError):
            self.store.bind("project", "pane-1", {**self.binding, "command": "bad"})
