import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class MessagingStoreTests(unittest.TestCase):
    def test_failing_cleanup_entries_do_not_starve_a_fifth_registration(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            for i in range(5):
                store.save_receiver(
                    dict(tool="claude", session_id=str(i), generation="old", pid=42, started=1)
                )
            attempts = []

            def forget(row):
                attempts.append(row["session_id"])
                return {"status": "unavailable"}

            with patch.object(store, "is_live", return_value=False):
                store.cleanup_pending("claude", forget)
                store.cleanup_pending("claude", forget)
            self.assertEqual(len(set(attempts)), 5)

    def test_resolution_refuses_alias_collisions(self):
        from ai_sessions.messaging.store import resolve

        rows = [dict(tool=t, session_id="same", name="worker") for t in ("codex", "claude")]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve("worker", rows)
        self.assertEqual(resolve("codex:same", rows)["tool"], "codex")

    def test_stale_registration_cannot_delete_new_generation(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            old = dict(tool="claude", session_id="a", generation="old", pid=42, started=1)
            new = {**old, "generation": "new"}
            store.save_receiver(old)
            store.save_receiver(new)
            store.remove_receiver(old)
            with patch.object(store, "is_live", return_value=True):
                self.assertEqual(store.receivers()[0]["generation"], "new")
            with patch.object(store, "is_live", return_value=False):
                self.assertEqual(store.receivers(), [])

    def test_receipts_do_not_store_bodies_or_credentials(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            store.save_receipt(
                dict(message_id="id", status="submitted", text="body", token="secret")
            )
            saved = store.receipt("id")
            self.assertEqual(saved["status"], "submitted")
            self.assertNotIn("text", saved)
            self.assertNotIn("token", saved)

    def test_late_transport_result_never_erases_acknowledgment(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            sender = {"tool": "claude", "session_id": "b", "generation": "one"}
            store.save_receipt(dict(message_id="id", status="submitting", recipient=sender))
            late = dict(message_id="id", status="accepted", recipient=sender)
            store.acknowledge("id", sender, "reply")
            store.save_receipt(late)
            self.assertEqual(store.receipt("id")["status"], "acknowledged")

    def test_failed_vault_cleanup_is_retried_without_discarding_its_reference(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            row = dict(tool="claude", session_id="a", generation="old", pid=42, started=1)
            store.save_receiver(row)
            with patch.object(store, "is_live", return_value=False):
                store.cleanup_pending("claude", lambda _: {"status": "unavailable"})
                self.assertEqual(len(list((Path(d) / "cleanup").glob("*.json"))), 1)
                store.cleanup_pending("claude", lambda _: {"status": "ok"})
                self.assertEqual(len(list((Path(d) / "cleanup").glob("*.json"))), 0)

    def test_cleanup_cannot_delete_an_inflight_secure_registration(self):
        from ai_sessions.messaging import store

        with tempfile.TemporaryDirectory() as d, patch.object(store, "ROOT", Path(d)):
            row = dict(tool="claude", session_id="a", generation="new", pid=42, started=1)
            entered, finish = threading.Event(), threading.Event()
            deleted = []

            def put():
                entered.set()
                self.assertTrue(finish.wait(3))
                return {"status": "ok"}

            with patch.object(store, "is_live", return_value=True):
                writer = threading.Thread(target=lambda: store.secure_register(row, put))
                writer.start()
                self.assertTrue(entered.wait(3))
                cleaner = threading.Thread(
                    target=lambda: store.cleanup_pending(
                        "claude", lambda r: deleted.append(r) or {"status": "ok"}
                    )
                )
                cleaner.start()
                time.sleep(0.05)
                finish.set()
                writer.join(4)
                cleaner.join(4)
                self.assertFalse(writer.is_alive() or cleaner.is_alive())
                self.assertEqual(deleted, [])
                self.assertEqual(store.receivers()[0]["generation"], "new")
