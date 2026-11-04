"""A bookmark's record list lives beside the state, not inside it."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import jsonl_tracking
from ai_sessions.app import UserState
from ai_sessions.jsonl_tracking import PREFIX, locate_checkpoint, protect_checkpoint
from ai_sessions.model import NativeRef

COUNT = 200


def identity(record):
    return record.get("payload") if record.get("type") == "message" else None


def messages(count: int) -> list[dict]:
    return [{"type": "message", "payload": {"text": f"message {index}"}} for index in range(count)]


def write(path: Path, records: list[dict], **dump) -> None:
    path.write_bytes(b"".join(json.dumps(r, **dump).encode() + b"\n" for r in records))


def inline(checkpoint: str) -> str:
    """The same bookmark as versions before 3.10 stored it."""
    saved = json.loads(checkpoint[len(PREFIX) :])
    records = jsonl_tracking._load_records(saved.pop("records_ref"))
    saved.pop("records_count")
    saved["records"] = records
    return PREFIX + json.dumps(saved, separators=(",", ":"))


class RecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, value in (
            ("RECORDS_DIR", self.root / "records"),
            ("LOCATIONS_FILE", self.root / "locations.json"),
            ("_locations", None),
        ):
            patcher = patch.object(jsonl_tracking, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = self.root / "history.jsonl"
        self.records = messages(COUNT)
        write(self.path, self.records)
        self.ref = NativeRef("session", str(self.path))

    def protect(self) -> str:
        checkpoint = protect_checkpoint(self.ref, self.path.stat().st_size, identity)
        self.assertIsNotNone(checkpoint)
        return checkpoint

    def rewrite(self) -> int:
        """Reformat the history so only the record list can find the bookmark."""
        write(self.path, self.records, separators=(",", ":"))
        end = self.path.stat().st_size
        with self.path.open("ab") as handle:
            handle.write(json.dumps({"type": "message", "payload": {"text": "new"}}).encode())
            handle.write(b"\n")
        return end

    def test_a_long_list_is_stored_beside_the_state_and_still_relocates(self) -> None:
        checkpoint = self.protect()
        saved = json.loads(checkpoint[len(PREFIX) :])
        self.assertEqual(saved["records"], [])
        self.assertEqual(saved["records_count"], COUNT)
        self.assertLess(len(checkpoint), 400)
        self.assertTrue((jsonl_tracking.RECORDS_DIR / f"{saved['records_ref']}.txt").is_file())
        end = self.rewrite()
        self.assertEqual(locate_checkpoint(self.ref, checkpoint, identity), end)

    def test_an_unchanged_history_needs_no_records(self) -> None:
        checkpoint = self.protect()
        size = self.path.stat().st_size
        for path in jsonl_tracking.RECORDS_DIR.iterdir():
            path.unlink()
        self.assertEqual(locate_checkpoint(self.ref, checkpoint, identity), size)

    def test_missing_or_damaged_records_cannot_relocate(self) -> None:
        checkpoint = self.protect()
        (stored,) = jsonl_tracking.RECORDS_DIR.iterdir()
        self.rewrite()
        stored.write_bytes(b"0" * 64)
        self.assertIsNone(locate_checkpoint(self.ref, checkpoint, identity))
        stored.unlink()
        self.assertIsNone(locate_checkpoint(self.ref, checkpoint, identity))

    def test_an_older_inline_bookmark_still_relocates(self) -> None:
        old = inline(self.protect())
        end = self.rewrite()
        self.assertEqual(locate_checkpoint(self.ref, old, identity), end)

    def test_a_short_list_stays_inline(self) -> None:
        write(self.path, messages(3))
        saved = json.loads(self.protect()[len(PREFIX) :])
        self.assertEqual(len(saved["records"]), 3)
        self.assertNotIn("records_ref", saved)

    def state_with(self, checkpoint: str) -> Path:
        state_file = self.root / "state.json"
        member = {"tool": "codex", "session_id": "session", "protected_checkpoint": checkpoint}
        state_file.write_text(
            json.dumps(
                {"version": 7, "conversations": {"c1": {"members": {"codex:session": member}}}}
            ),
            encoding="utf-8",
        )
        return state_file

    def test_saving_the_state_moves_inline_records_out(self) -> None:
        checkpoint = self.protect()
        state_file = self.state_with(inline(checkpoint))
        before = state_file.stat().st_size
        state = UserState(state_file)
        member = state.conversations["c1"]["members"]["codex:session"]
        state.save()
        self.assertLess(state_file.stat().st_size, before / 10)
        self.assertEqual(member["protected_checkpoint"], checkpoint)
        again = UserState(state_file).conversations["c1"]["members"]["codex:session"]
        end = self.rewrite()
        self.assertEqual(locate_checkpoint(self.ref, again["protected_checkpoint"], identity), end)

    def test_housekeeping_moves_them_once_unless_another_process_wrote(self) -> None:
        checkpoint = inline(self.protect())
        state_file = self.state_with(checkpoint)
        state = UserState(state_file)
        later = state_file.stat().st_mtime_ns + 5_000_000_000
        os.utime(state_file, ns=(later, later))  # Written again by someone else.
        state.tidy_checkpoints()
        self.assertNotIn("records_ref", state_file.read_text(encoding="utf-8"))
        state = UserState(state_file)
        state.tidy_checkpoints()
        self.assertIn("records_ref", state_file.read_text(encoding="utf-8"))
        with patch.object(UserState, "_save") as saved:
            UserState(state_file).tidy_checkpoints()
        saved.assert_not_called()

    def test_the_sweep_keeps_referenced_and_recent_records(self) -> None:
        state = UserState(self.state_with(self.protect()))
        (kept,) = jsonl_tracking.RECORDS_DIR.iterdir()
        write(self.path, messages(COUNT + 1))
        self.protect()
        (unused,) = set(jsonl_tracking.RECORDS_DIR.iterdir()) - {kept}
        old = time.time() - 30 * 86400
        for path in (kept, unused):
            os.utime(path, (old, old))
        recent = jsonl_tracking.RECORDS_DIR / ("f" * 64 + ".txt")
        recent.write_text("x")
        state.tidy_checkpoints()
        self.assertEqual(set(jsonl_tracking.RECORDS_DIR.iterdir()), {kept, recent})

    def test_storing_known_records_again_marks_them_in_use(self) -> None:
        self.protect()
        (stored,) = jsonl_tracking.RECORDS_DIR.iterdir()
        old = time.time() - 30 * 86400
        os.utime(stored, (old, old))
        self.protect()
        self.assertGreater(stored.stat().st_mtime, old + 86400)


if __name__ == "__main__":
    unittest.main()
