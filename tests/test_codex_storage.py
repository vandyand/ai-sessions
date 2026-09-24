import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.harnesses import codex


def rollout(root: Path, name: str = "rollout.jsonl", **meta: object) -> Path:
    path = root / name
    payload = {"id": "019f6162-c0c1-7520-b4d4-0c7a0b46d359", "cwd": str(root)}
    payload.update(meta)
    path.write_text(
        json.dumps(
            {"timestamp": "2026-07-14T16:08:54.358Z", "type": "session_meta", "payload": payload}
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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
            path.write_text("not json\n", encoding="utf-8")
            self.assertIsNone(codex._history_mode(str(path)))


class UpgradeStorageTests(unittest.TestCase):
    """Codex refuses to rewind a legacy rollout, so resuming one migrates it first."""

    def setUp(self) -> None:
        self.reported: list[str] = []

    def upgrade(self, storage: str, run: object) -> tuple[str, ...]:
        with (
            patch.object(codex.shutil, "which", lambda name: f"/opt/bin/{name}"),
            patch.object(codex.subprocess, "run", run),
        ):
            return codex.upgrade_storage(
                session_id="019f6162-c0c1-7520-b4d4-0c7a0b46d359",
                storage=storage,
                command=("codex",),
                report=self.reported.append,
            )

    def test_a_current_rollout_runs_nothing(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a paginated rollout must not be migrated again")

        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="paginated")
            self.assertEqual(self.upgrade(str(path), run), ())
        self.assertEqual(self.reported, [])

    def test_a_legacy_rollout_is_migrated_and_the_result_is_verified_on_disk(self) -> None:
        calls: list[list[str]] = []

        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(argv)
                rollout(Path(root), history_mode="paginated")
                return subprocess.CompletedProcess(argv, 0, "1 migrated", "")

            notices = self.upgrade(str(path), run)

        self.assertEqual(
            calls[0],
            [
                "/opt/bin/codex",
                "migrate-rollouts",
                "--apply",
                "--thread",
                "019f6162-c0c1-7520-b4d4-0c7a0b46d359",
            ],
        )
        self.assertEqual(len(notices), 1)
        self.assertIn("can be rewound now", notices[0])
        self.assertTrue(self.reported)

    def test_a_rollout_predating_the_field_is_also_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root))

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rollout(Path(root), history_mode="paginated")
                return subprocess.CompletedProcess(argv, 0, "1 migrated", "")

            notices = self.upgrade(str(path), run)
        self.assertIn("can be rewound now", notices[0])

    def test_a_session_open_elsewhere_is_reported_not_forced(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    argv, 0, "skipped busy\tthread already has an active writer", ""
                )

            notices = self.upgrade(str(path), run)
            self.assertEqual(codex._history_mode(str(path)), "legacy")
        self.assertIn("open in another Codex process", notices[0])

    def test_a_refused_migration_says_rewind_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(argv, 1, "", "0 migrated, 1 failed")

            notices = self.upgrade(str(path), run)
        self.assertIn("stays unavailable", notices[0])

    def test_a_crashing_or_timing_out_migration_is_contained(self) -> None:
        for error in (
            OSError("cannot spawn"),
            subprocess.TimeoutExpired(cmd="codex", timeout=1.0),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as root:
                path = rollout(Path(root), history_mode="legacy")

                def run(*_args: object, _error: BaseException = error, **_kwargs: object) -> object:
                    raise _error

                notices = self.upgrade(str(path), run)
            self.assertEqual(len(notices), 1)
            self.assertIn("could not update Codex storage", notices[0])

    def test_a_harness_that_is_not_installed_changes_nothing(self) -> None:
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("nothing may run when the command is absent")

        with tempfile.TemporaryDirectory() as root:
            path = rollout(Path(root), history_mode="legacy")
            with (
                patch.object(codex.shutil, "which", lambda _name: None),
                patch.object(codex.subprocess, "run", run),
            ):
                notices = codex.upgrade_storage(
                    session_id="019f6162-c0c1-7520-b4d4-0c7a0b46d359",
                    storage=str(path),
                    command=("codex",),
                    report=self.reported.append,
                )
        self.assertEqual(notices, ())


if __name__ == "__main__":
    unittest.main()
