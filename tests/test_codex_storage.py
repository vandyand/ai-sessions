import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.capabilities import StorageUpgrade
from ai_sessions.harnesses import codex
from ai_sessions.model import NativeRef

SESSION_ID = "019f6162-c0c1-7520-b4d4-0c7a0b46d359"


def meta_record(**meta: object) -> dict:
    payload = {"id": SESSION_ID, "cwd": "/project"}
    payload.update(meta)
    return {"timestamp": "2026-07-14T16:08:54.358Z", "type": "session_meta", "payload": payload}


def message(index: int, role: str = "user") -> dict:
    return {
        "timestamp": f"2026-07-14T16:09:{index:02d}.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": f"turn {index}"}],
        },
    }


def ui_event(index: int) -> dict:
    return {
        "timestamp": f"2026-07-14T16:10:{index:02d}.000Z",
        "type": "event_msg",
        "payload": {"type": "token_count"},
    }


def write_records(path: Path, records: list[dict], *, newline: bytes = b"\n") -> list[int]:
    """Write exact bytes and return the offset after each record."""
    offsets: list[int] = []
    data = b""
    for record in records:
        data += json.dumps(record).encode("utf-8") + newline
        offsets.append(len(data))
    path.write_bytes(data)
    return offsets


def rollout(root: Path, name: str = "rollout.jsonl", **meta: object) -> Path:
    path = root / name
    write_records(path, [meta_record(**meta)])
    return path


def migrate_in_place(path: Path, *, drop: int | None = None) -> None:
    """Imitate Codex migration: a new header, normalized line endings, extra fields."""
    lines = [line for line in path.read_bytes().replace(b"\r\n", b"\n").split(b"\n") if line]
    records = [json.loads(line) for line in lines]
    records[0]["payload"]["history_mode"] = "paginated"
    rewritten = []
    for index, record in enumerate(records):
        if drop is not None and index == drop:
            continue
        if index:
            record["ordinal"] = index
        rewritten.append(record)
    write_records(path, rewritten)


class HistoryModeTests(unittest.TestCase):
    def test_an_unreadable_rollout_is_not_mistaken_for_an_old_one(self) -> None:
        self.assertIsNone(codex._history_mode(""))
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(codex._history_mode(str(Path(root) / "absent.jsonl")))

    def test_a_rollout_predating_the_field_reads_as_empty_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(codex._history_mode(str(rollout(Path(root)))), "")

    def test_recorded_modes_are_reported_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            legacy = rollout(base, "legacy.jsonl", history_mode="legacy")
            current = rollout(base, "current.jsonl", history_mode="paginated")
            self.assertEqual(codex._history_mode(str(legacy)), "legacy")
            self.assertEqual(codex._history_mode(str(current)), "paginated")

    def test_a_damaged_first_line_is_not_treated_as_an_old_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "broken.jsonl"
            path.write_bytes(b"not json\n")
            self.assertIsNone(codex._history_mode(str(path)))


class UpgradeStorageTests(unittest.TestCase):
    """Codex refuses to rewind a legacy rollout, so resuming one migrates it first."""

    def setUp(self) -> None:
        self.reported: list[str] = []

    def upgrade(self, storage: str, run: object, checkpoints: dict | None = None) -> StorageUpgrade:
        with (
            patch.object(codex.shutil, "which", lambda name: f"/opt/bin/{name}"),
            patch.object(codex.subprocess, "run", run),
        ):
            return codex.upgrade_storage(
                session_id=SESSION_ID,
                storage=storage,
                command=("codex",),
                report=self.reported.append,
                checkpoints=checkpoints or {},
            )

    @staticmethod
    def migrating(path: Path, *, drop: int | None = None):
        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            migrate_in_place(path, drop=drop)
            return subprocess.CompletedProcess(argv, 0, "1 migrated", "")

        return run

    def test_a_current_rollout_runs_nothing(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a paginated rollout must not be migrated again")

        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="paginated")
            self.assertEqual(self.upgrade(str(path), run), StorageUpgrade())
        self.assertEqual(self.reported, [])

    def test_a_legacy_rollout_is_migrated_and_verified_on_disk(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")
            migrate = self.migrating(path)

            def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(argv)
                return migrate(argv, **kwargs)

            result = self.upgrade(str(path), run)
            self.assertEqual(codex._history_mode(str(path)), "paginated")
        self.assertEqual(
            calls[0], ["/opt/bin/codex", "migrate-rollouts", "--apply", "--thread", SESSION_ID]
        )
        self.assertIn("can be rewound now", result.notices[0])
        self.assertTrue(self.reported)

    def test_a_rollout_predating_the_field_is_also_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root))
            result = self.upgrade(str(path), self.migrating(path))
        self.assertIn("can be rewound now", result.notices[0])

    def test_a_session_open_elsewhere_is_reported_not_forced(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    argv, 0, "skipped busy\tthread already has an active writer", ""
                )

            result = self.upgrade(str(path), run)
            self.assertEqual(codex._history_mode(str(path)), "legacy")
        self.assertIn("open in another Codex process", result.notices[0])

    def test_a_refused_migration_says_rewind_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(argv, 1, "", "0 migrated, 1 failed")

            result = self.upgrade(str(path), run)
        self.assertIn("stays unavailable", result.notices[0])

    def test_a_crashing_or_timing_out_migration_is_contained(self) -> None:
        for error in (
            OSError("cannot spawn"),
            subprocess.TimeoutExpired(cmd="codex", timeout=1.0),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as root:
                path = rollout(Path(root), history_mode="legacy")

                def run(*_args: object, _error: BaseException = error, **_kwargs: object) -> object:
                    raise _error

                result = self.upgrade(str(path), run)
            self.assertEqual(len(result.notices), 1)
            self.assertIn("could not update Codex storage", result.notices[0])

    def test_a_harness_that_is_not_installed_changes_nothing(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("nothing may run when the command is absent")

        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")
            with (
                patch.object(codex.shutil, "which", lambda _name: None),
                patch.object(codex.subprocess, "run", run),
            ):
                result = codex.upgrade_storage(
                    session_id=SESSION_ID,
                    storage=str(path),
                    command=("codex",),
                    report=self.reported.append,
                    checkpoints={},
                )
        self.assertEqual(result, StorageUpgrade())


class TrackedCheckpointTests(unittest.TestCase):
    """Migration rewrites every byte; a tracked conversation must keep its footing."""

    def upgrade(self, path: Path, checkpoints: dict, run: object) -> StorageUpgrade:
        with (
            patch.object(codex.shutil, "which", lambda name: f"/opt/bin/{name}"),
            patch.object(codex.subprocess, "run", run),
        ):
            return codex.upgrade_storage(
                session_id=SESSION_ID,
                storage=str(path),
                command=("codex",),
                report=lambda _message: None,
                checkpoints=checkpoints,
            )

    def build(self, root: Path) -> tuple[Path, list[int]]:
        path = root / "rollout.jsonl"
        offsets = write_records(
            path,
            [meta_record(), message(1), message(2, "assistant"), message(3), ui_event(4)],
            newline=b"\r\n",
        )
        return path, offsets

    def status(self, path: Path, checkpoint: int) -> str:
        return codex._codex_change_status(NativeRef(SESSION_ID, str(path)), checkpoint)

    def test_a_conversation_that_moved_on_is_relocated_after_the_same_record(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path, offsets = self.build(Path(root))
            frontier = offsets[2]
            self.assertEqual(self.status(path, frontier), "changed")
            result = self.upgrade(
                path, {"conversation": frontier}, UpgradeStorageTests.migrating(path)
            )
            moved = result.checkpoints["conversation"]
            self.assertNotEqual(moved, frontier)
            self.assertEqual(self.status(path, moved), "changed")
            # Exactly the records after the frontier remain after the new position.
            tail = path.read_bytes()[moved:].split(b"\n")
            self.assertEqual(json.loads(tail[0])["payload"]["content"][0]["text"], "turn 3")
            self.assertEqual(self.status(path, frontier), "unstable")

    def test_a_settled_conversation_stays_settled(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path, offsets = self.build(Path(root))
            settled = offsets[3]
            self.assertEqual(self.status(path, settled), "unchanged")
            result = self.upgrade(
                path, {"conversation": settled}, UpgradeStorageTests.migrating(path)
            )
            self.assertEqual(self.status(path, result.checkpoints["conversation"]), "unchanged")

    def test_every_tracking_conversation_is_translated_independently(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path, offsets = self.build(Path(root))
            result = self.upgrade(
                path,
                {"older": offsets[1], "newer": offsets[3]},
                UpgradeStorageTests.migrating(path),
            )
            self.assertEqual(self.status(path, result.checkpoints["older"]), "changed")
            self.assertEqual(self.status(path, result.checkpoints["newer"]), "unchanged")

    def test_a_record_dropped_by_migration_falls_back_to_the_same_classification(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path, offsets = self.build(Path(root))
            result = self.upgrade(
                path,
                {"conversation": offsets[1]},
                UpgradeStorageTests.migrating(path, drop=1),
            )
            self.assertEqual(self.status(path, result.checkpoints["conversation"]), "changed")

    def test_an_unsettled_conversation_blocks_the_migration(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("an unsettled conversation must not be migrated")

        with tempfile.TemporaryDirectory() as root:
            path, offsets = self.build(Path(root))
            mid_record = offsets[1] + 5
            result = self.upgrade(path, {"conversation": mid_record}, run)
            self.assertEqual(codex._history_mode(str(path)), "")
        self.assertEqual(result.checkpoints, {})
        self.assertIn("not in a settled state", result.notices[0])

    def test_a_position_that_is_not_a_byte_offset_blocks_the_migration(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("an untranslatable position must not be migrated")

        with tempfile.TemporaryDirectory() as root:
            path, _offsets = self.build(Path(root))
            result = self.upgrade(path, {"conversation": None}, run)
        self.assertIn("not a byte offset", result.notices[0])


if __name__ == "__main__":
    unittest.main()
