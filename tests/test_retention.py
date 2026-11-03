import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_sessions import retention
from ai_sessions.app import BridgeError, UserState, prepare_launch
from ai_sessions.compression import CompressionError, compress_file, resolve_storage
from ai_sessions.conversion import append_jsonl, conversation_change_status
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.harnesses import codex as codex_harness
from ai_sessions.model import NativeRef, Session
from ai_sessions.registry import REGISTRY


def session(tool: str, storage: Path, **kw) -> Session:
    return Session(tool, "id", "title", "/w", 2.0, 1.0, "p", True, str(storage), **kw)


class TranscriptFiles(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.main = self.root / "abc.jsonl"
        self.main.write_text("main\n", encoding="utf-8")

    def child(self, name: str = "agent-1.jsonl") -> Path:
        folder = self.root / "abc" / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("child\n", encoding="utf-8")
        return path

    def test_claude_lists_subagents_first_and_main_last(self) -> None:
        child = self.child()
        (child.parent / "agent-1.meta.json").write_text("{}", encoding="utf-8")
        files = claude_harness.transcript_files(NativeRef("abc", str(self.main)))
        self.assertEqual(files, (child, self.main))

    def test_claude_finds_compressed_subagents_by_their_plain_names(self) -> None:
        child = self.child()
        retention.compress_session(session("claude", self.main))
        files = claude_harness.transcript_files(NativeRef("abc", str(self.main) + ".zst"))
        self.assertEqual(files, (child, self.main))

    def test_codex_is_the_rollout_alone(self) -> None:
        self.assertEqual(
            codex_harness.transcript_files(NativeRef("abc", str(self.main) + ".zst")),
            (self.main,),
        )

    def test_opencode_is_unsupported(self) -> None:
        self.assertFalse(retention.can_compress(session("opencode", self.main)))
        with self.assertRaisesRegex(CompressionError, "cannot be compressed"):
            retention.compress_session(session("opencode", self.main))


class Operations(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.main = self.root / "abc.jsonl"
        self.main.write_text("main\n", encoding="utf-8")
        folder = self.root / "abc" / "subagents"
        folder.mkdir(parents=True)
        self.child = folder / "agent-1.jsonl"
        self.child.write_text("child\n", encoding="utf-8")
        self.item = session("claude", self.main)

    def test_compress_then_decompress_updates_the_session(self) -> None:
        retention.compress_session(self.item)
        self.assertTrue(self.item.compressed)
        self.assertEqual(self.item.storage, str(self.main) + ".zst")
        self.assertFalse(self.main.exists() or self.child.exists())
        retention.decompress_session(self.item)
        self.assertFalse(self.item.compressed)
        self.assertEqual(self.item.storage, str(self.main))
        self.assertEqual(self.main.read_text(encoding="utf-8"), "main\n")
        self.assertEqual(self.child.read_text(encoding="utf-8"), "child\n")

    def test_open_session_is_refused(self) -> None:
        self.item.is_open = True
        with self.assertRaisesRegex(CompressionError, "open"):
            retention.compress_session(self.item)
        self.assertTrue(self.main.is_file())

    def test_compress_stops_before_the_main_file_on_failure(self) -> None:
        failing = replace(
            REGISTRY.get("claude"),
            transcript_files=lambda ref: (self.root / "missing.jsonl", self.main),
        )
        self.main_mtime = self.main.stat().st_mtime_ns
        real = retention.compress_file

        def boom(path):
            raise CompressionError("nope")

        retention.compress_file = boom
        self.addCleanup(setattr, retention, "compress_file", real)
        self.child.unlink()
        with REGISTRY.temporary(failing):
            with self.assertRaisesRegex(CompressionError, "nope"):
                retention.compress_session(self.item)
        self.assertFalse(self.item.compressed)
        self.assertEqual(self.item.storage, str(self.main))

    def test_a_failed_main_file_restores_the_subagent_logs(self) -> None:
        real = retention.compress_file

        def refuse_main(path):
            if path == self.main:
                raise CompressionError("in use")
            return real(path)

        with patch.object(retention, "compress_file", refuse_main):
            with self.assertRaisesRegex(CompressionError, "in use"):
                retention.compress_session(self.item)
        self.assertFalse(self.item.compressed)
        self.assertEqual(self.child.read_text(encoding="utf-8"), "child\n")
        self.assertFalse(retention.needs_restore(self.item))

    def test_a_session_with_no_transcript_is_refused(self) -> None:
        self.main.unlink()
        with self.assertRaises(CompressionError):
            retention.compress_session(self.item)
        self.assertFalse(self.item.compressed)

    def test_a_stranded_subagent_log_needs_restoring(self) -> None:
        compress_file(self.child)
        self.assertFalse(self.item.compressed)
        self.assertTrue(retention.needs_restore(self.item))
        retention.decompress_session(self.item)
        self.assertEqual(self.child.read_text(encoding="utf-8"), "child\n")
        self.assertFalse(retention.needs_restore(self.item))

    def test_nested_subagent_logs_travel_with_the_session(self) -> None:
        nested = self.child.parent / "workflows" / "wf_1"
        nested.mkdir(parents=True)
        log = nested / "agent-2.jsonl"
        log.write_text("nested\n", encoding="utf-8")
        retention.compress_session(self.item)
        self.assertFalse(log.exists())
        retention.decompress_session(self.item)
        self.assertEqual(log.read_text(encoding="utf-8"), "nested\n")

    def test_sync_storage_notices_another_window(self) -> None:
        other = session("claude", self.main)
        retention.compress_session(other)
        self.assertFalse(self.item.compressed)
        retention.sync_storage(self.item)
        self.assertTrue(self.item.compressed)
        self.assertEqual(self.item.storage, other.storage)
        retention.decompress_session(other)
        retention.sync_storage(self.item)
        self.assertFalse(self.item.compressed)
        self.assertEqual(self.item.storage, str(self.main))

    def test_naming_never_recreates_a_transcript_that_was_just_compressed(self) -> None:
        compress_file(self.main)
        note = claude_harness.publish_name(self.item, "name")
        self.assertIn("missing", note)
        self.assertFalse(self.main.exists())

    def test_appending_without_create_refuses_a_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            append_jsonl(self.root / "gone.jsonl", {"a": 1}, create=False)
        self.assertFalse((self.root / "gone.jsonl").exists())
        append_jsonl(self.main, {"a": 1}, create=False)
        self.assertEqual(self.main.read_bytes().count(b"\n"), 2)

    def test_a_recorded_compressed_path_follows_the_restored_file(self) -> None:
        recorded = str(self.main) + ".zst"
        seen: list[str] = []

        def classify(native, checkpoint) -> str:
            seen.append(native.storage)
            return "unchanged"

        with REGISTRY.temporary(replace(REGISTRY.get("claude"), change_status=classify)):
            status = conversation_change_status(
                "claude", NativeRef("id", recorded), 5, availability="available"
            )
        self.assertEqual(status, "unchanged")
        self.assertEqual(seen, [str(self.main)])

    def test_legacy_bridges_wait_until_their_source_is_restored(self) -> None:
        state = UserState(self.root / "state.json")
        state.bridges = {"claude:id": {"codex": {"session_id": "other"}}}
        retention.compress_session(self.item)
        state._migrate_legacy_bridges([self.item])
        self.assertEqual(state.conversations, {})

    def test_a_plain_tail_hiding_history_stops_the_open_and_says_why(self) -> None:
        compress_file(self.main)
        self.main.write_text("tail\n", encoding="utf-8")
        retention.sync_storage(self.item)
        self.assertFalse(self.item.compressed)
        self.assertTrue(retention.needs_restore(self.item))
        with self.assertRaisesRegex(CompressionError, "differ"):
            retention.decompress_session(self.item)
        self.assertEqual(self.main.read_text(encoding="utf-8"), "tail\n")
        self.assertTrue(Path(str(self.main) + ".zst").is_file())

    def test_a_session_that_wakes_up_part_way_is_put_back(self) -> None:
        calls = []

        def active() -> bool:
            calls.append(1)
            return len(calls) < 2

        with self.assertRaisesRegex(CompressionError, "active"):
            retention.compress_session(self.item, still_idle=active)
        self.assertEqual(self.main.read_text(encoding="utf-8"), "main\n")
        self.assertEqual(self.child.read_text(encoding="utf-8"), "child\n")
        self.assertFalse(self.item.compressed)

    def test_prepare_launch_restores_a_stranded_log(self) -> None:
        compress_file(self.child)
        prepare_launch(self.item)
        self.assertEqual(self.child.read_text(encoding="utf-8"), "child\n")

    def test_prepare_launch_refuses_hidden_history(self) -> None:
        compress_file(self.main)
        self.main.write_text("tail\n", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "differ"):
            prepare_launch(self.item)

    def test_prepare_launch_mentions_a_log_it_could_not_restore(self) -> None:
        compress_file(self.child)
        (self.child.parent / "agent-1.jsonl.zst").write_bytes(b"not zstd")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            prepare_launch(self.item)
        self.assertIn("agent-1", stderr.getvalue())

    def test_operations_repeat_safely(self) -> None:
        retention.compress_session(self.item)
        retention.compress_session(self.item)
        self.assertTrue(self.item.compressed)
        retention.decompress_session(self.item)
        retention.decompress_session(self.item)
        self.assertFalse(self.item.compressed)
        self.assertEqual(resolve_storage(self.main), self.main)

    def test_a_subagent_failure_does_not_stop_the_session_opening(self) -> None:
        retention.compress_session(self.item)
        (self.child.parent / "agent-1.jsonl.zst").write_bytes(b"not zstd")
        note = retention.decompress_session(self.item)
        self.assertFalse(self.item.compressed)
        self.assertIn("agent-1", note)
        self.assertTrue(self.main.is_file())


if __name__ == "__main__":
    unittest.main()
