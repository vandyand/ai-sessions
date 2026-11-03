import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import zstandard

from ai_sessions import diagnostics
from ai_sessions.compression import compress_file
from ai_sessions.discovery import HarnessContext
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.harnesses import codex as codex_harness
from ai_sessions.model import Availability, NativeRef
from ai_sessions.registry import REGISTRY

CLAUDE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AGENT_ID = "abc123"
CODEX_ID = "019f59af-e300-7bd1-be75-e47599b5b593"


def jsonl(*records: dict) -> bytes:
    return b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)


def claude_records() -> bytes:
    return jsonl(
        {
            "type": "user",
            "cwd": "/work",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "first prompt here"},
        },
        {"type": "assistant", "message": {"content": "an answer"}},
        {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
        {"type": "custom-title", "customTitle": "Named session"},
    )


def codex_records() -> bytes:
    return jsonl(
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "codex prompt"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "codex answer"},
        },
        {"type": "compacted", "payload": {}},
    )


def frame_without_size(data: bytes) -> bytes:
    compressor = zstandard.ZstdCompressor(write_content_size=False, write_checksum=False)
    frame = compressor.compress(data)
    assert zstandard.get_frame_parameters(frame).content_size == zstandard.CONTENTSIZE_UNKNOWN
    return frame


def counts(session) -> tuple:
    return (
        session.title,
        session.cwd,
        session.turn_count,
        session.prompt_count,
        session.compaction_count,
        session.preview,
    )


class CompressedDiscoveryBase(unittest.TestCase):
    def setUp(self) -> None:
        diagnostics.clear_warnings()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.claude_home = self.root / "claude"
        self.codex_home = self.root / "codex"
        self.project = self.claude_home / "projects" / "project"
        self.project.mkdir(parents=True)
        self.codex_day = self.codex_home / "sessions" / "2026" / "01" / "01"
        self.codex_day.mkdir(parents=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(claude_harness, "DISCOVERY_CACHE_FILE", self.root / "claude-cache.json")
        )
        self.stack.enter_context(
            patch.object(codex_harness, "DISCOVERY_CACHE_FILE", self.root / "codex-cache.json")
        )
        self.stack.enter_context(
            REGISTRY.temporary(replace(REGISTRY.get("claude"), home=self.claude_home))
        )
        self.stack.enter_context(
            REGISTRY.temporary(replace(REGISTRY.get("codex"), home=self.codex_home))
        )

    def claude(self, use_cache: bool = False):
        context = HarnessContext.create(use_cache=use_cache)
        return claude_harness.discover(context)

    def claude_by_id(self, use_cache: bool = False) -> dict:
        return {session.session_id: session for session in self.claude(use_cache)}

    def codex(self, use_cache: bool = False):
        context = HarnessContext.create(use_cache=use_cache)
        return codex_harness.discover(context)

    def write_codex_db(self, rollout: Path) -> None:
        database = sqlite3.connect(self.codex_home / "state_5.sqlite")
        database.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT, rollout_path TEXT, source TEXT, "
            "cwd TEXT, title TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER)"
        )
        database.execute("DELETE FROM threads")
        database.execute(
            "INSERT INTO threads VALUES (?, ?, 'cli', '/work', 'Codex work', 1, 2, 0)",
            (CODEX_ID, str(rollout)),
        )
        database.commit()
        database.close()


class UnregisteredRolloutTests(CompressedDiscoveryBase):
    OTHER = "01a02502-5225-7121-a4e8-869df0686982"

    def rollout(self, session_id: str, source: object = "cli") -> Path:
        meta = {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-01-01T00:00:00Z",
                "cwd": "/elsewhere",
                "source": source,
            },
        }
        path = self.codex_day / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
        path.write_bytes(jsonl(meta) + codex_records())
        return path

    def test_a_rollout_missing_from_the_database_is_listed(self) -> None:
        registered = self.rollout(CODEX_ID)
        self.write_codex_db(registered)
        compress_file(self.rollout(self.OTHER, "exec"))
        sessions = {item.session_id: item for item in self.codex()}
        self.assertEqual(set(sessions), {CODEX_ID, self.OTHER})
        found = sessions[self.OTHER]
        self.assertTrue(found.compressed)
        self.assertEqual(found.cwd, "/elsewhere")
        self.assertEqual(found.source.value, "non-interactive")
        self.assertEqual(found.title, "codex prompt")
        self.assertEqual(found.turn_count, sessions[CODEX_ID].turn_count)

    def test_both_forms_of_a_missing_rollout_list_once_as_plain(self) -> None:
        self.write_codex_db(self.rollout(CODEX_ID))
        plain = self.rollout(self.OTHER)
        compressed = compress_file(plain)
        self.rollout(self.OTHER)
        self.assertTrue(compressed.is_file())
        found = [item for item in self.codex() if item.session_id == self.OTHER]
        self.assertEqual([item.compressed for item in found], [False])

    def test_a_file_without_session_meta_is_ignored(self) -> None:
        self.write_codex_db(self.rollout(CODEX_ID))
        stray = self.codex_day / f"rollout-2026-01-01T00-00-00-{self.OTHER}.jsonl"
        stray.write_bytes(b"not json\n")
        self.assertEqual([item.session_id for item in self.codex()], [CODEX_ID])


class ClaudeCompressedDiscoveryTests(CompressedDiscoveryBase):
    def plain(self) -> Path:
        path = self.project / f"{CLAUDE_ID}.jsonl"
        path.write_bytes(claude_records())
        return path

    def test_compressed_only_matches_plain_listing(self) -> None:
        path = self.plain()
        baseline = self.claude_by_id()[CLAUDE_ID]
        self.assertFalse(baseline.compressed)
        target = compress_file(path)
        listed = self.claude()
        self.assertEqual(len(listed), 1)
        session = listed[0]
        self.assertTrue(session.compressed)
        self.assertEqual(session.storage, str(target))
        self.assertEqual(session.session_id, CLAUDE_ID)
        self.assertEqual(counts(session), counts(baseline))
        self.assertEqual(baseline.title, "Named session")
        self.assertEqual(baseline.compaction_count, 1)

    def test_both_forms_list_once_from_the_plain_file(self) -> None:
        path = self.plain()
        shutil.copy(path, path.with_name(path.name + ".zst"))
        (path.with_name(path.name + ".zst")).write_bytes(
            zstandard.ZstdCompressor().compress(b'{"type":"user"}\n')
        )
        listed = self.claude()
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0].compressed)
        self.assertEqual(listed[0].storage, str(path))
        self.assertEqual(listed[0].title, "Named session")

    def test_frame_without_size_or_checksum(self) -> None:
        (self.project / f"{CLAUDE_ID}.jsonl.zst").write_bytes(frame_without_size(claude_records()))
        session = self.claude_by_id()[CLAUDE_ID]
        self.assertTrue(session.compressed)
        self.assertEqual(session.title, "Named session")
        self.assertEqual(session.turn_count, 3)

    def test_subagent_is_listed(self) -> None:
        directory = self.project / CLAUDE_ID / "subagents"
        directory.mkdir(parents=True)
        agent = directory / f"agent-{AGENT_ID}.jsonl"
        agent.write_bytes(
            jsonl(
                {
                    "type": "user",
                    "isSidechain": True,
                    "agentId": AGENT_ID,
                    "sessionId": CLAUDE_ID,
                    "message": {"content": "do the thing"},
                }
            )
        )
        baseline = self.claude_by_id()[AGENT_ID]
        compress_file(agent)
        session = self.claude_by_id()[AGENT_ID]
        self.assertTrue(session.compressed)
        self.assertTrue(session.storage.endswith(".jsonl.zst"))
        self.assertEqual(session.parent_id, CLAUDE_ID)
        self.assertEqual(counts(session), counts(baseline))

    def test_discovery_leaves_files_unchanged(self) -> None:
        path = self.plain()
        target = compress_file(path)
        data, mtime = target.read_bytes(), target.stat().st_mtime_ns
        self.claude(use_cache=True)
        self.claude(use_cache=True)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [target.name])

    def test_second_discovery_does_not_decompress(self) -> None:
        target = compress_file(self.plain())
        first = self.claude(use_cache=True)
        with patch.object(
            claude_harness, "open_for_scan", wraps=claude_harness.open_for_scan
        ) as opener:
            second = self.claude(use_cache=True)
        opener.assert_not_called()
        self.assertEqual([counts(s) for s in first], [counts(s) for s in second])
        self.assertTrue(second[0].storage.endswith(target.name))

    def test_cache_carries_over_from_plain_entry(self) -> None:
        path = self.plain()
        baseline = self.claude(use_cache=True)[0]
        target = compress_file(path)
        with patch.object(
            claude_harness, "open_for_scan", wraps=claude_harness.open_for_scan
        ) as opener:
            carried = self.claude(use_cache=True)[0]
        opener.assert_not_called()
        self.assertTrue(carried.compressed)
        self.assertEqual(counts(carried), counts(baseline))
        saved = json.loads((self.root / "claude-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["entries"][str(target)]["size"], target.stat().st_size)

    def test_corrupt_file_does_not_crash_or_cache(self) -> None:
        target = self.project / f"{CLAUDE_ID}.jsonl.zst"
        target.write_bytes(b"this is not a zstd frame at all")
        listed = self.claude(use_cache=True)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].turn_count, 0)
        cache = self.root / "claude-cache.json"
        if cache.exists():
            self.assertNotIn(str(target), json.loads(cache.read_text(encoding="utf-8"))["entries"])

    def test_availability_resolve_and_exists(self) -> None:
        path = self.plain()
        plain_ref = NativeRef(CLAUDE_ID, str(path))
        self.assertEqual(claude_harness._claude_availability(plain_ref), Availability.AVAILABLE)
        self.assertTrue(claude_harness._claude_exists(CLAUDE_ID))
        target = compress_file(path)
        zst_ref = NativeRef(CLAUDE_ID, str(target))
        self.assertEqual(claude_harness._claude_availability(zst_ref), Availability.COMPRESSED)
        self.assertEqual(claude_harness._claude_availability(plain_ref), Availability.COMPRESSED)
        self.assertTrue(claude_harness._claude_exists(CLAUDE_ID))
        self.assertEqual(claude_harness._claude_resolve(CLAUDE_ID).storage, str(target))
        path.write_bytes(claude_records())
        self.assertEqual(claude_harness._claude_availability(zst_ref), Availability.AVAILABLE)
        self.assertEqual(claude_harness._claude_resolve(CLAUDE_ID).storage, str(path))
        path.unlink()
        target.unlink()
        self.assertEqual(claude_harness._claude_availability(zst_ref), Availability.UNAVAILABLE)
        self.assertFalse(claude_harness._claude_exists(CLAUDE_ID))
        self.assertIsNone(claude_harness._claude_resolve(CLAUDE_ID))


class CodexCompressedDiscoveryTests(CompressedDiscoveryBase):
    def plain(self) -> Path:
        path = self.codex_day / f"rollout-2026-01-01T00-00-00-{CODEX_ID}.jsonl"
        path.write_bytes(codex_records())
        self.write_codex_db(path)
        return path

    def test_compressed_only_matches_plain_listing(self) -> None:
        path = self.plain()
        baseline = self.codex()[0]
        self.assertFalse(baseline.compressed)
        self.assertEqual((baseline.turn_count, baseline.compaction_count), (2, 1))
        target = compress_file(path)
        listed = self.codex()
        self.assertEqual(len(listed), 1)
        session = listed[0]
        self.assertTrue(session.compressed)
        self.assertEqual(session.storage, str(target))
        self.assertEqual(counts(session), counts(baseline))

    def test_both_forms_list_once_from_the_plain_file(self) -> None:
        path = self.plain()
        path.with_name(path.name + ".zst").write_bytes(
            zstandard.ZstdCompressor().compress(b'{"type":"compacted"}\n')
        )
        listed = self.codex()
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0].compressed)
        self.assertEqual(listed[0].storage, str(path))
        self.assertEqual(listed[0].compaction_count, 1)

    def test_frame_without_size_or_checksum(self) -> None:
        path = self.plain()
        baseline = self.codex()[0]
        path.unlink()
        path.with_name(path.name + ".zst").write_bytes(frame_without_size(codex_records()))
        session = self.codex()[0]
        self.assertTrue(session.compressed)
        self.assertEqual(counts(session), counts(baseline))

    def test_discovery_leaves_files_unchanged_and_does_not_redecompress(self) -> None:
        target = compress_file(self.plain())
        data, mtime = target.read_bytes(), target.stat().st_mtime_ns
        first = self.codex(use_cache=True)
        with patch.object(
            codex_harness, "open_for_scan", wraps=codex_harness.open_for_scan
        ) as opener:
            second = self.codex(use_cache=True)
        opener.assert_not_called()
        self.assertEqual([counts(s) for s in first], [counts(s) for s in second])
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_cache_carries_over_from_plain_entry(self) -> None:
        path = self.plain()
        baseline = self.codex(use_cache=True)[0]
        target = compress_file(path)
        with patch.object(
            codex_harness, "open_for_scan", wraps=codex_harness.open_for_scan
        ) as opener:
            carried = self.codex(use_cache=True)[0]
        opener.assert_not_called()
        self.assertTrue(carried.compressed)
        self.assertEqual(counts(carried), counts(baseline))
        saved = json.loads((self.root / "codex-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["entries"][str(target)]["size"], target.stat().st_size)

    def test_corrupt_file_does_not_crash_or_cache(self) -> None:
        path = self.plain()
        path.unlink()
        target = path.with_name(path.name + ".zst")
        target.write_bytes(b"this is not a zstd frame at all")
        listed = self.codex(use_cache=True)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].turn_count, 0)
        cache = self.root / "codex-cache.json"
        if cache.exists():
            self.assertNotIn(str(target), json.loads(cache.read_text(encoding="utf-8"))["entries"])

    def test_availability_resolve_and_exists(self) -> None:
        path = self.plain()
        plain_ref = NativeRef(CODEX_ID, str(path))
        self.assertEqual(codex_harness._codex_availability(plain_ref), Availability.AVAILABLE)
        self.assertTrue(codex_harness._codex_exists(CODEX_ID))
        target = compress_file(path)
        zst_ref = NativeRef(CODEX_ID, str(target))
        codex_harness._rollout_index = None
        self.assertEqual(codex_harness._codex_availability(zst_ref), Availability.COMPRESSED)
        self.assertEqual(codex_harness._codex_availability(plain_ref), Availability.COMPRESSED)
        self.assertTrue(codex_harness._codex_exists(CODEX_ID))
        self.assertEqual(codex_harness._codex_resolve(CODEX_ID).storage, str(target))
        path.write_bytes(codex_records())
        codex_harness._rollout_index = None
        self.assertEqual(codex_harness._codex_availability(zst_ref), Availability.AVAILABLE)
        self.assertEqual(codex_harness._codex_resolve(CODEX_ID).storage, str(path))
        path.unlink()
        target.unlink()
        codex_harness._rollout_index = None
        self.assertEqual(codex_harness._codex_availability(zst_ref), Availability.UNAVAILABLE)
        self.assertFalse(codex_harness._codex_exists(CODEX_ID))
        self.assertIsNone(codex_harness._codex_resolve(CODEX_ID))

    def test_indexed_lookup_finds_compressed_rollout_outside_dated_folder(self) -> None:
        other = "11111111-1111-4111-8111-111111111111"
        odd = self.codex_home / "sessions" / "odd"
        odd.mkdir()
        (odd / f"rollout-x-{other}.jsonl.zst").write_bytes(frame_without_size(b"{}\n"))
        codex_harness._rollout_index = None
        self.assertTrue(codex_harness._codex_exists(other))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    unittest.main()
