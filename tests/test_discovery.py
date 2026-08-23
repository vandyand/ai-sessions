import json
import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_sessions import diagnostics
from ai_sessions.app import Session, load_sessions, reconcile_evidence, session_from_native
from ai_sessions.discovery import MAX_EVIDENCE_IDS, EvidenceAccumulator, HarnessContext
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.model import NativeSession, SourceKind
from ai_sessions.registry import REGISTRY


class EvidenceAccumulatorTests(unittest.TestCase):
    def test_overlapping_patterns_preserve_first_byte_scan_order(self) -> None:
        evidence = EvidenceAccumulator((re.compile(rb"id-[0-9]+"), re.compile(rb"id-2")))
        evidence.scan(b"before id-2 then id-1 and id-2 again")
        self.assertEqual(evidence.tokens, ["id-2", "id-1"])

    def test_distinct_evidence_is_capped_and_disclosed(self) -> None:
        evidence = EvidenceAccumulator((re.compile(rb"x[0-9]{4}"),))
        line = " ".join(f"x{index:04d}" for index in range(MAX_EVIDENCE_IDS + 1)).encode()
        evidence.scan(line)
        self.assertEqual(len(evidence.tokens), MAX_EVIDENCE_IDS)
        self.assertEqual(evidence.tokens[-1], "x4095")
        self.assertTrue(evidence.truncated)

    def test_pattern_signature_changes_with_registry_generation(self) -> None:
        before = HarnessContext.create().pattern_signature
        base = REGISTRY.get("codex")
        fake = replace(base, name="patterned", id_patterns=(re.compile(rb"new-[0-9]+"),))
        with REGISTRY.temporary(fake):
            during = HarnessContext.create().pattern_signature
        after = HarnessContext.create().pattern_signature
        self.assertNotEqual(during, before)
        self.assertEqual(after, before)

    def test_cache_signature_change_rescans_historical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": "historical new-123"},
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cache_path = root / "claude-discovery-v5.json"
            with patch.object(claude_harness, "DISCOVERY_CACHE_FILE", cache_path):
                before_context = HarnessContext.create()
                _, before = claude_harness.DiscoveryCache(before_context).scan(transcript)
                self.assertNotIn("new-123", before.tokens)
                first_cache = claude_harness.DiscoveryCache(before_context)
                first_cache.scan(transcript)
                first_cache.save()
                base = REGISTRY.get("codex")
                fake = replace(
                    base,
                    name="patterned",
                    id_patterns=(re.compile(rb"new-[0-9]+"),),
                )
                with REGISTRY.temporary(fake):
                    changed_context = HarnessContext.create()
                    changed_cache = claude_harness.DiscoveryCache(changed_context)
                    _, after = changed_cache.scan(transcript)
                    changed_cache.save()
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertIn("new-123", after.tokens)
        self.assertEqual(payload["version"], claude_harness.DISCOVERY_CACHE_VERSION)
        entry = payload["entries"][str(transcript)]
        self.assertEqual(entry["pattern_signature"], changed_context.pattern_signature)


class GenericDiscoveryTests(unittest.TestCase):
    def test_builtin_discovery_and_cross_evidence_use_registered_homes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_home = root / "claude"
            codex_home = root / "codex"
            project = claude_home / "projects" / "project"
            project.mkdir(parents=True)
            codex_home.mkdir(parents=True)
            claude_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            codex_id = "019f59af-e300-7bd1-be75-e47599b5b593"
            rollout = codex_home / f"rollout-{codex_id}.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "from codex"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (project / f"{claude_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": claude_id,
                        "cwd": "/project",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"content": f"spawned codex thread {codex_id}"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            database = sqlite3.connect(codex_home / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, source TEXT, cwd TEXT, "
                "title TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, 'exec', '/work', 'Codex work', 1, 2, 0)",
                (codex_id, str(rollout)),
            )
            database.commit()
            database.close()
            with (
                REGISTRY.temporary(replace(REGISTRY.get("claude"), home=claude_home)),
                REGISTRY.temporary(replace(REGISTRY.get("codex"), home=codex_home)),
            ):
                rows = load_sessions(use_cache=False)
        by_tool = {row.tool: row for row in rows}
        self.assertEqual(set(by_tool), {"claude", "codex"})
        self.assertEqual(by_tool["codex"].origin, "cross")
        self.assertEqual(by_tool["claude"].launch_targets["codex"], codex_id)
        self.assertEqual(by_tool["codex"].launch_targets["claude"], claude_id)

    def test_truncated_evidence_accepts_positive_match_and_warns(self) -> None:
        diagnostics.clear_warnings()
        context = HarnessContext.create()
        publisher = Session("claude", "publisher", "", "", 0, 0, "", False, "source")
        target_id = "019f59af-e300-7bd1-be75-e47599b5b593"
        target = Session("codex", target_id, "", "", 0, 0, "", False, "target")
        context.evidence[("claude", "publisher")] = ([target_id], True)
        reconcile_evidence([publisher, target], context)
        self.assertEqual(target.origin, "cross")
        self.assertIn("truncated", diagnostics.warnings()[0])

    def test_overlapping_claims_resolve_by_discovered_identity_not_first_pattern(self) -> None:
        base = REGISTRY.get("codex")
        first = replace(base, name="first", order=30)
        second = replace(base, name="second", order=40)
        shared = "019f59af-e300-7bd1-be75-e47599b5b593"
        publisher = Session("claude", "publisher", "", "", 0, 0, "", False, "source")
        one = Session("first", shared, "", "", 0, 0, "", False, "one")
        two = Session("second", shared, "", "", 0, 0, "", False, "two")
        context = HarnessContext.create()
        context.evidence[("claude", "publisher")] = ([shared], False)
        with REGISTRY.temporary(first), REGISTRY.temporary(second):
            reconcile_evidence([publisher, one, two], context)
        self.assertEqual(one.origin, "human")
        self.assertEqual(two.origin, "human")
        self.assertNotIn("first", publisher.launch_targets)
        self.assertNotIn("second", publisher.launch_targets)

    def test_native_archived_evidence_remains_auxiliary(self) -> None:
        native = NativeSession(
            "codex",
            "id",
            "title",
            "/cwd",
            0,
            0,
            "",
            False,
            "storage",
            source=SourceKind.INTERACTIVE,
            archived=True,
        )
        row = session_from_native(native)
        self.assertTrue(row.auxiliary)
        self.assertEqual(row.origin, "human")


if __name__ == "__main__":
    unittest.main()
