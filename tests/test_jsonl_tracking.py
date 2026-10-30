import json
import os
import tempfile
import unittest
from pathlib import Path

from ai_sessions.jsonl_tracking import locate_checkpoint, protect_checkpoint
from ai_sessions.model import NativeRef


def identity(record):
    return record.get("payload") if record.get("type") == "message" else None


class TrackingTests(unittest.TestCase):
    def test_rewrite_metadata_and_formatting_preserves_bookmark_but_edits_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            records = [
                {"type": "meta", "instructions": "old"},
                {"type": "message", "payload": {"role": "user", "text": "hello"}},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            ref = NativeRef("session", str(path))
            checkpoint = protect_checkpoint(ref, path.stat().st_size, identity)
            records[0]["instructions"] = "new longer developer instructions"
            prefix = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
            path.write_text(
                prefix
                + json.dumps({"type": "message", "payload": {"role": "assistant", "text": "reply"}})
                + "\n",
                encoding="utf-8",
            )
            offset = locate_checkpoint(ref, checkpoint, identity)
            self.assertEqual(
                offset,
                len(prefix.replace("\n", "\r\n").encode())
                if os.name == "nt"
                else len(prefix.encode()),
            )
            records[1]["payload"]["text"] = "edited"
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            self.assertIsNone(locate_checkpoint(ref, checkpoint, identity))

    def test_partial_capture_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_bytes(b'{"type":"message"')
            self.assertIsNone(
                protect_checkpoint(NativeRef("session", str(path)), path.stat().st_size, identity)
            )


class AdapterTrackingTests(unittest.TestCase):
    def test_state_round_trip_keeps_protected_history_after_metadata_rewrite(self):
        from ai_sessions.app import UserState
        from ai_sessions.model import Session

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout.jsonl"
            user = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "question"}],
                },
            }
            path.write_text(json.dumps(user) + "\n", encoding="utf-8")
            item = Session("codex", "session", "title", directory, 1, 1, "", True, str(path))
            state = UserState(root / "state.json")
            cid = state.conversation_id_for(item, create=True)
            state.save()
            member = UserState(root / "state.json").conversations[cid]["members"][item.key]
            self.assertIn("protected_checkpoint", member)
            path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": "new instructions",
                        },
                    }
                )
                + "\n"
                + json.dumps(user)
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(state._member_change_status(member), "unchanged")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": "answer",
                            },
                        }
                    )
                    + "\n"
                )
            self.assertEqual(state._member_change_status(member), "changed")
            user["payload"]["content"][0]["text"] = "edited question"
            path.write_text(json.dumps(user) + "\n", encoding="utf-8")
            self.assertEqual(state._member_change_status(member), "unstable")


class RecoveryTests(unittest.TestCase):
    def test_recovery_persists_selected_copy_and_preserves_other_history(self):
        from ai_sessions.app import UserState
        from ai_sessions.model import Session

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": "hello"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            item = Session("codex", "new", "title", directory, 1, 1, "", True, str(path))
            state = UserState(root / "state.json")
            old = state.conversation_id_for(item, create=True)
            previous = dict(state.conversations[old]["members"][item.key])
            state.conversations[old]["members"]["codex:other"] = dict(previous, session_id="other")
            state.session_conversations["codex:other"] = old
            state.save()
            recovered = state.recover_session(item)
            loaded = UserState(state.path)
            self.assertNotEqual(recovered.conversation_id, old)
            self.assertEqual(loaded.conversation_id_for(item), recovered.conversation_id)
            self.assertIn("codex:other", loaded.conversations[old]["members"])
            self.assertNotIn(item.key, loaded.conversations[old]["members"])
            self.assertEqual(
                loaded.conversations[recovered.conversation_id]["recovery"]["previous_member"],
                previous,
            )
            self.assertTrue(list(root.glob("state.before-recovery-*.json")))
            loaded.apply([recovered])
            self.assertFalse(recovered.conversation_blocker)

    def test_recovery_rejects_malformed_history(self):
        from ai_sessions.app import UserState
        from ai_sessions.conversion import BridgeError
        from ai_sessions.model import Session

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "broken.jsonl"
            path.write_bytes(b"not json\n")
            item = Session("codex", "bad", "title", directory, 1, 1, "", True, str(path))
            state = UserState(root / "state.json")
            with self.assertRaises(BridgeError):
                state.recover_session(item)


class SemanticCodexTrackingTests(unittest.TestCase):
    def test_instruction_and_compaction_bookkeeping_rewrites_do_not_block(self):
        from ai_sessions.harnesses.codex import _codex_change_status, _protect_codex_checkpoint

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            instruction = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "# AGENTS.md instructions\n\n<INSTRUCTIONS>old</INSTRUCTIONS>",
                },
            }
            question = {"type": "message", "role": "user", "content": "real question"}
            summary = {
                "type": "compacted",
                "payload": {
                    "message": "real summary",
                    "window_id": "old",
                    "replacement_history": [instruction["payload"], question],
                },
            }
            records = [instruction, {"type": "response_item", "payload": question}, summary]

            def write():
                path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

            write()
            ref = NativeRef("session", str(path))
            checkpoint = _protect_codex_checkpoint(ref, path.stat().st_size)
            instruction["payload"]["content"] = (
                "# AGENTS.md instructions\n\n<INSTRUCTIONS>new rules</INSTRUCTIONS>"
            )
            summary["payload"]["window_id"] = "new"
            summary["payload"]["latest_token_usage_record"] = {"tokens": 100}
            write()
            self.assertEqual(_codex_change_status(ref, checkpoint), "unchanged")
            summary["payload"]["message"] = "edited summary"
            write()
            self.assertEqual(_codex_change_status(ref, checkpoint), "unstable")


class LocationCacheTests(unittest.TestCase):
    """A bookmark is verified once per file change, not once per launch."""

    def setUp(self):
        from unittest.mock import patch

        from ai_sessions import jsonl_tracking

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in (
            patch.object(jsonl_tracking, "LOCATIONS_FILE", self.root / "locations.json"),
            patch.object(jsonl_tracking, "_locations", None),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.tracking = jsonl_tracking
        jsonl_tracking._locate.cache_clear()
        self.path = self.root / "history.jsonl"
        self.records = [
            {"type": "meta", "instructions": "old"},
            {"type": "message", "payload": {"role": "user", "text": "hello"}},
        ]
        self.write(self.records)
        self.ref = NativeRef("session", str(self.path))
        self.checkpoint = protect_checkpoint(self.ref, self.path.stat().st_size, identity)

    def write(self, records):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def fresh_launch(self):
        self.tracking._locations = None
        self.tracking._locate.cache_clear()

    def test_an_appended_file_reuses_the_saved_location(self):
        self.write([*self.records, {"type": "message", "payload": {"text": "reply"}}])
        first = locate_checkpoint(self.ref, self.checkpoint, identity)
        self.fresh_launch()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "message", "payload": {"text": "more"}}) + "\n")
        from unittest.mock import patch

        with patch.object(self.tracking, "_locate", side_effect=AssertionError("rescanned")):
            self.assertEqual(locate_checkpoint(self.ref, self.checkpoint, identity), first)

    def test_a_rewrite_is_checked_again(self):
        self.assertIsNotNone(locate_checkpoint(self.ref, self.checkpoint, identity))
        self.fresh_launch()
        self.records[1]["payload"]["text"] = "edited"
        self.write(self.records)
        self.assertIsNone(locate_checkpoint(self.ref, self.checkpoint, identity))

    def test_a_failure_is_not_repeated_until_the_file_changes(self):
        self.records[1]["payload"]["text"] = "edited"
        self.write(self.records)
        self.assertIsNone(locate_checkpoint(self.ref, self.checkpoint, identity))
        self.fresh_launch()
        from unittest.mock import patch

        with patch.object(self.tracking, "_locate", side_effect=AssertionError("rescanned")):
            self.assertIsNone(locate_checkpoint(self.ref, self.checkpoint, identity))
        self.records[1]["payload"]["text"] = "hello"
        self.write([*self.records, {"type": "meta", "instructions": "padding to change size"}])
        self.assertIsNotNone(locate_checkpoint(self.ref, self.checkpoint, identity))


class CodexContextRewriteTests(unittest.TestCase):
    """Old byte-for-byte Codex bookmarks survive Codex 0.165's context rewrite."""

    def build(self, agents, environment_block, compact_window, extra=()):
        from ai_sessions.harnesses.codex import _codex_record_identity

        content = [
            {"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\n" + agents}
        ]
        if environment_block:
            content.append(
                {"type": "input_text", "text": "<environment_context>x</environment_context>"}
            )
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": content},
            },
            {"type": "compacted", "payload": {"message": "summary", "window": compact_window}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
            *extra,
        ]
        return records, _codex_record_identity

    def test_rewritten_envelopes_are_tolerated_but_new_work_is_not(self):
        from ai_sessions.harnesses.codex import (
            _codex_context_rewrite,
            _codex_record_identity,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            ref = NativeRef("session", str(path))

            def write(records):
                path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

            original, _ = self.build("old", False, 1)
            write(original)
            checkpoint = protect_checkpoint(ref, path.stat().st_size, _codex_record_identity)
            rewritten, _ = self.build("old", True, 2)
            write(rewritten)
            self.assertIsNone(locate_checkpoint(ref, checkpoint, _codex_record_identity))
            self.assertIsNotNone(
                locate_checkpoint(ref, checkpoint, _codex_record_identity, _codex_context_rewrite)
            )
            edited = [dict(r) for r in rewritten]
            edited[3] = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "changed"}],
                },
            }
            write(edited)
            self.assertIsNone(
                locate_checkpoint(ref, checkpoint, _codex_record_identity, _codex_context_rewrite)
            )
            inserted = [*rewritten[:1], rewritten[3], *rewritten[1:]]
            write(inserted)
            self.assertIsNone(
                locate_checkpoint(ref, checkpoint, _codex_record_identity, _codex_context_rewrite)
            )


class CodexRolloutLookupTests(unittest.TestCase):
    def test_lookup_uses_the_id_date_and_falls_back_to_one_shared_walk(self):
        import datetime as dt
        from dataclasses import replace
        from unittest.mock import patch

        from ai_sessions.harnesses import codex

        session_id = "019a0000-0000-7000-8000-000000000001"
        created = dt.datetime.fromtimestamp(0x019A00000000 / 1000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions"
            dated = root / f"{created:%Y}" / f"{created:%m}" / f"{created:%d}"
            dated.mkdir(parents=True)
            (dated / f"rollout-x-{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
            misfiled_id = "019a0000-0000-7000-8000-000000000002"
            (root / "elsewhere").mkdir()
            (root / "elsewhere" / f"rollout-x-{misfiled_id}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            with (
                codex.REGISTRY.temporary(
                    replace(codex.REGISTRY.get("codex"), home=Path(directory))
                ),
                patch.object(codex, "_rollout_index", None),
            ):
                with patch.object(codex, "_indexed_rollouts", side_effect=AssertionError("walked")):
                    self.assertTrue(codex._codex_exists(session_id))
                self.assertTrue(codex._codex_exists(misfiled_id))
                self.assertFalse(codex._codex_exists("019a0000-0000-7000-8000-000000000003"))
