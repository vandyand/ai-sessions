import json
import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_sessions import diagnostics
from ai_sessions.app import (
    Session,
    collect_scratch_origin_hints,
    dedupe_sessions,
    load_sessions,
    reconcile_evidence,
    session_from_native,
)
from ai_sessions.discovery import (
    MAX_EVIDENCE_IDS,
    MAX_EXISTENCE_PROBES_PER_PASS,
    EvidenceAccumulator,
    HarnessContext,
)
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

    def test_stale_cache_payload_version_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": "fresh prompt"},
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stat = transcript.stat()
            cache_path = root / "claude-discovery-v5.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "entries": {
                            str(transcript): {
                                "custom_title": "stale title",
                                "inode": stat.st_ino,
                                "size": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(claude_harness, "DISCOVERY_CACHE_FILE", cache_path):
                meta, _ = claude_harness.DiscoveryCache(HarnessContext.create()).scan(transcript)
        self.assertEqual(meta["custom_title"], "")
        self.assertEqual(meta["first_prompt"], "fresh prompt")

    def test_truncation_flag_survives_cache_round_trip(self) -> None:
        base = REGISTRY.get("codex")
        fake = replace(base, name="patterned", id_patterns=(re.compile(rb"x[0-9]{4}"),))
        with tempfile.TemporaryDirectory() as directory, REGISTRY.temporary(fake):
            root = Path(directory)
            transcript = root / "transcript.jsonl"
            values = " ".join(f"x{index:04d}" for index in range(MAX_EVIDENCE_IDS + 1))
            transcript.write_text(
                json.dumps({"type": "user", "message": {"content": values}}) + "\n",
                encoding="utf-8",
            )
            cache_path = root / "claude-discovery-v5.json"
            with patch.object(claude_harness, "DISCOVERY_CACHE_FILE", cache_path):
                context = HarnessContext.create()
                cache = claude_harness.DiscoveryCache(context)
                _, scanned = cache.scan(transcript)
                cache.save()
                restored_context = HarnessContext.create()
                _, restored = claude_harness.DiscoveryCache(restored_context).scan(transcript)
        self.assertTrue(scanned.truncated)
        self.assertTrue(restored.truncated)
        self.assertEqual(len(restored.tokens), MAX_EVIDENCE_IDS)

    def test_incomplete_final_record_is_replayed_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "transcript.jsonl"
            first = json.dumps({"type": "user", "message": {"content": "first"}}) + "\n"
            second = json.dumps({"type": "user", "message": {"content": "completed"}})
            transcript.write_bytes(first.encode() + second[:20].encode())
            cache_path = root / "claude-discovery-v5.json"
            with patch.object(claude_harness, "DISCOVERY_CACHE_FILE", cache_path):
                context = HarnessContext.create()
                cache = claude_harness.DiscoveryCache(context)
                before, incomplete = cache.scan(transcript)
                cache.save()
                with transcript.open("ab") as handle:
                    handle.write(second[20:].encode() + b"\n")
                after, complete = claude_harness.DiscoveryCache(context).scan(transcript)
        self.assertEqual(before["last_prompt"], "first")
        self.assertEqual(after["last_prompt"], "completed")
        self.assertTrue(incomplete.truncated)
        self.assertFalse(complete.truncated)

    def test_cache_size_never_advances_beyond_consumed_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"type": "user", "message": {"content": "prompt"}}) + "\n",
                encoding="utf-8",
            )
            actual = transcript.stat()
            with patch.object(
                claude_harness.os,
                "fstat",
                return_value=type(
                    "Stat",
                    (),
                    {
                        "st_ino": actual.st_ino,
                        "st_size": actual.st_size + 100,
                        "st_mtime_ns": actual.st_mtime_ns + 1,
                    },
                )(),
            ):
                meta, _ = claude_harness.DiscoveryCache(
                    HarnessContext.create(use_cache=False)
                ).scan(transcript)
        self.assertEqual(meta["size"], actual.st_size)
        self.assertEqual(meta["offset"], actual.st_size)

    def test_replaced_file_is_rescanned_from_zero_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"type": "user", "message": {"content": "replacement prefix"}}) + "\n",
                encoding="utf-8",
            )
            actual = transcript.stat()
            cached_offset = min(10, actual.st_size - 1)
            old_stat = SimpleNamespace(
                st_ino=actual.st_ino + 1,
                st_size=actual.st_size,
                st_mtime_ns=actual.st_mtime_ns,
            )
            context = HarnessContext.create(use_cache=False)
            cache = claude_harness.DiscoveryCache(context)
            cache.entries[str(transcript)] = {
                **cache.blank(),
                "inode": old_stat.st_ino,
                "size": cached_offset,
                "mtime_ns": old_stat.st_mtime_ns,
                "offset": cached_offset,
                "pattern_signature": context.pattern_signature,
                "candidate_ids": [],
                "evidence_truncated": False,
            }
            with patch.object(Path, "stat", return_value=old_stat):
                meta, _ = cache.scan(transcript)
        self.assertEqual(meta["first_prompt"], "replacement prefix")
        self.assertEqual(meta["inode"], actual.st_ino)


class GenericDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        diagnostics.clear_warnings()

    def tearDown(self) -> None:
        diagnostics.clear_warnings()

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
        target = Session(
            "codex",
            target_id,
            "",
            "",
            0,
            0,
            "",
            False,
            "target",
            source=SourceKind.NON_INTERACTIVE,
        )
        context.evidence[("claude", "publisher")] = ([target_id], True)
        reconcile_evidence([publisher, target], context)
        self.assertEqual(target.origin, "cross")
        self.assertIn("truncated", diagnostics.warnings()[0])

    def test_interactive_target_keeps_human_origin_when_real_id_is_mentioned(self) -> None:
        context = HarnessContext.create()
        publisher = Session("claude", "publisher", "", "", 0, 0, "", False, "source")
        target_id = "019f59af-e300-7bd1-be75-e47599b5b593"
        target = Session("codex", target_id, "", "", 0, 0, "", False, "target")
        context.evidence[("claude", "publisher")] = ([target_id], False)
        reconcile_evidence([publisher, target], context)
        self.assertEqual(target.origin, "human")
        self.assertEqual(publisher.launch_targets["codex"], target_id)

    def test_unresolved_existence_probes_are_bounded_and_disclosed(self) -> None:
        diagnostics.clear_warnings()
        calls: list[str] = []
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="native",
            id_patterns=(re.compile(rb"native-[0-9]+"),),
            locate=lambda token: calls.append(token) or False,
        )
        publisher = Session("claude", "publisher", "", "", 0, 0, "", False, "source")
        context = HarnessContext.create()
        context.evidence[("claude", "publisher")] = (
            [f"native-{index}" for index in range(100)],
            False,
        )
        with REGISTRY.temporary(fake):
            reconcile_evidence([publisher], context)
        self.assertEqual(len(calls), MAX_EXISTENCE_PROBES_PER_PASS)
        warnings = [note for note in diagnostics.warnings() if "probes were capped" in note]
        self.assertEqual(len(warnings), 1)

    def test_existence_probe_budget_is_global_and_cache_is_shared(self) -> None:
        diagnostics.clear_warnings()
        calls: list[str] = []
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="native",
            id_patterns=(re.compile(rb"native-[0-9]+"),),
            locate=lambda token: calls.append(token) or token == "native-0",
        )
        first = Session("claude", "first", "", "", 0, 0, "", False, "first")
        second = Session("claude", "second", "", "", 0, 0, "", False, "second")
        context = HarnessContext.create()
        context.evidence[("claude", "first")] = (
            [f"native-{index}" for index in range(40)],
            False,
        )
        context.evidence[("claude", "second")] = (
            ["native-0", *[f"native-{index}" for index in range(40, 80)]],
            False,
        )
        with REGISTRY.temporary(fake):
            reconcile_evidence([first, second], context)
        self.assertEqual(len(calls), MAX_EXISTENCE_PROBES_PER_PASS)
        self.assertEqual(calls.count("native-0"), 1)
        self.assertEqual(first.launch_targets["native"], "native-0")
        self.assertEqual(second.launch_targets["native"], "native-0")

    def test_existence_probe_budget_prioritizes_recent_publishers(self) -> None:
        calls: list[str] = []
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="native",
            id_patterns=(re.compile(rb"native-[0-9]+"),),
            locate=lambda token: calls.append(token) or False,
        )
        older = Session("claude", "aaa", "", "", 1, 0, "", False, "older")
        newer = Session("claude", "zzz", "", "", 2, 0, "", False, "newer")
        context = HarnessContext.create()
        context.evidence[("claude", "aaa")] = (
            [f"native-{index}" for index in range(MAX_EXISTENCE_PROBES_PER_PASS)],
            False,
        )
        context.evidence[("claude", "zzz")] = (["native-999"], False)
        with REGISTRY.temporary(fake):
            reconcile_evidence([older, newer], context)
        self.assertEqual(calls[0], "native-999")

    def test_overlapping_publisher_pattern_does_not_suppress_native_probe(self) -> None:
        token = "019f59af-e300-7bd1-be75-e47599b5b593"
        calls: list[str] = []
        codex = replace(REGISTRY.get("codex"), locate=lambda value: calls.append(value) or True)
        publisher = Session("claude", "publisher", "", "", 0, 0, "", False, "source")
        context = HarnessContext.create()
        context.evidence[("claude", "publisher")] = ([token], False)
        with REGISTRY.temporary(codex):
            reconcile_evidence([publisher], context)
        self.assertIn(token, calls)
        self.assertEqual(publisher.launch_targets["codex"], token)

    def test_scratch_origin_is_declared_by_publisher_not_target(self) -> None:
        base = REGISTRY.get("codex")
        publisher_adapter = replace(
            base,
            name="publisher",
            scratch_patterns=(re.compile(r"/publisher-scratch/"),),
        )
        target = Session(
            "codex",
            "target",
            "",
            "/publisher-scratch/work",
            0,
            0,
            "",
            False,
            "target",
            source=SourceKind.NON_INTERACTIVE,
        )
        context = HarnessContext.create()
        with REGISTRY.temporary(publisher_adapter):
            collect_scratch_origin_hints([target], context)
            reconcile_evidence([target], context)
        self.assertEqual(target.origin, "cross")
        self.assertNotIn("publisher", target.launch_targets)

    def test_builtin_claude_scratch_pattern_matches_realistic_codex_cwd(self) -> None:
        target = Session(
            "codex",
            "target",
            "",
            r"C:\Users\vandy\AppData\Local\Temp\claude-worktree",
            0,
            0,
            "",
            False,
            "target",
            source=SourceKind.NON_INTERACTIVE,
        )
        context = HarnessContext.create()
        collect_scratch_origin_hints([target], context)
        reconcile_evidence([target], context)
        self.assertEqual(target.origin, "cross")
        self.assertNotIn("claude", target.launch_targets)

    def test_scratch_hint_never_reclassifies_interactive_session(self) -> None:
        target = Session(
            "codex",
            "target",
            "",
            "/tmp/claude-worktree",
            0,
            0,
            "",
            False,
            "target",
            source=SourceKind.INTERACTIVE,
        )
        context = HarnessContext.create()
        collect_scratch_origin_hints([target], context)
        reconcile_evidence([target], context)
        self.assertEqual(target.origin, "human")
        self.assertNotIn("claude", target.launch_targets)

    def test_nonmatching_scratch_path_preserves_origin(self) -> None:
        target = Session(
            "codex",
            "target",
            "",
            "/tmp/claudette-worktree",
            0,
            0,
            "",
            False,
            "target",
            source=SourceKind.NON_INTERACTIVE,
        )
        context = HarnessContext.create()
        collect_scratch_origin_hints([target], context)
        reconcile_evidence([target], context)
        self.assertEqual(target.origin, "human")

    def test_unavailable_evidence_publisher_is_ignored(self) -> None:
        diagnostics.clear_warnings()
        target_id = "019f59af-e300-7bd1-be75-e47599b5b593"
        target = Session("codex", target_id, "", "", 0, 0, "", False, "target")
        context = HarnessContext.create()
        context.evidence[("unavailable", "publisher")] = ([target_id], False)
        reconcile_evidence([target], context)
        self.assertEqual(target.origin, "human")
        self.assertNotIn("unavailable", target.launch_targets)
        self.assertTrue(any("unavailable harness" in note for note in diagnostics.warnings()))

    def test_publisher_own_id_never_creates_a_cross_link(self) -> None:
        publisher_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        publisher = Session(
            "claude",
            publisher_id,
            "",
            "",
            0,
            0,
            "",
            False,
            "source",
            source=SourceKind.NON_INTERACTIVE,
            origin="agent",
        )
        context = HarnessContext.create()
        context.evidence[("claude", publisher_id)] = ([publisher_id], False)
        reconcile_evidence([publisher], context)
        self.assertEqual(publisher.launch_targets, {"claude": publisher_id})
        self.assertEqual(publisher.origin, "agent")

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

    def test_dedupe_identity_includes_harness_name(self) -> None:
        first = Session("first", "shared", "", "", 1, 0, "", False, "one")
        second = Session("second", "shared", "", "", 2, 0, "", False, "two")
        duplicate = Session("first", "shared", "newer", "", 3, 0, "", False, "three")
        rows = dedupe_sessions([first, second, duplicate])
        self.assertEqual({row.key for row in rows}, {"first:shared", "second:shared"})
        self.assertEqual(next(row for row in rows if row.tool == "first").title, "newer")

    def test_equal_time_dedupe_is_independent_of_scan_order(self) -> None:
        lower = Session("first", "shared", "a", "", 1, 0, "", False, "a")
        higher = Session("first", "shared", "z", "", 1, 0, "", False, "z")
        self.assertEqual(dedupe_sessions([lower, higher])[0].storage, "z")
        self.assertEqual(dedupe_sessions([higher, lower])[0].storage, "z")

    def test_dedupe_precedes_evidence_reconciliation(self) -> None:
        target_id = "019f59af-e300-7bd1-be75-e47599b5b593"
        codex = replace(
            REGISTRY.get("codex"),
            discover=lambda *_args, **_kwargs: [
                NativeSession(
                    "codex",
                    target_id,
                    "new",
                    "",
                    2,
                    0,
                    "",
                    False,
                    "new",
                    source=SourceKind.NON_INTERACTIVE,
                ),
                NativeSession("codex", target_id, "old", "", 1, 0, "", False, "old"),
            ],
        )

        def discover_claude(context: HarnessContext, **_kwargs: object) -> list[NativeSession]:
            context.evidence[("claude", "publisher")] = ([target_id], False)
            return [NativeSession("claude", "publisher", "", "", 0, 0, "", False, "source")]

        claude = replace(REGISTRY.get("claude"), discover=discover_claude)
        with (
            REGISTRY.temporary(codex),
            REGISTRY.temporary(claude),
            patch("ai_sessions.app.detect_open_sessions"),
        ):
            rows = load_sessions(use_cache=False)
        survivor = next(row for row in rows if row.tool == "codex")
        self.assertEqual(survivor.storage, "new")
        self.assertEqual(survivor.origin, "cross")
        self.assertEqual(survivor.launch_targets["claude"], "publisher")


if __name__ == "__main__":
    unittest.main()
