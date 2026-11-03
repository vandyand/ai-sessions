import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import conversion, jsonl_tracking
from ai_sessions.model import NativeRef


def claude_message(role, text):
    return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def codex_message(role, text):
    kind = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text}],
        },
    }


class ReadSinceCase(unittest.TestCase):
    tool = ""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in (
            patch.object(jsonl_tracking, "LOCATIONS_FILE", self.root / "locations.json"),
            patch.object(jsonl_tracking, "_locations", None),
        ):
            target.start()
            self.addCleanup(target.stop)
        jsonl_tracking._locate.cache_clear()
        self.path = self.root / "session.jsonl"
        self.ref = NativeRef("session", str(self.path))

    def write(self, records):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def append(self, records, *, newline=True):
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            for record in records:
                handle.write(json.dumps(record) + ("\n" if newline else ""))

    def texts(self, snapshot):
        return [(turn.role, turn.text) for turn in snapshot.transcript.turns]


class ClaudeReadSinceTests(ReadSinceCase):
    tool = "claude"
    message = staticmethod(claude_message)

    def test_claude_read_since_returns_only_appended_turns(self):
        self.write(
            [
                self.message("user", "one"),
                self.message("assistant", "a1"),
                self.message("user", "two"),
                self.message("assistant", "a2"),
            ]
        )
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        self.append([self.message("user", "three"), self.message("assistant", "a3")])
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        self.assertEqual(self.texts(snapshot), [("user", "three"), ("assistant", "a3")])
        self.assertEqual(snapshot.checkpoint, conversion.complete_jsonl_cursor(self.path))

    def test_read_since_partial_tail_stops_before_incomplete_record(self):
        self.write([self.message("user", "one")])
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        self.append([self.message("assistant", "done")])
        complete = conversion.complete_jsonl_cursor(self.path)
        self.append([self.message("assistant", "half")], newline=False)
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        self.assertEqual(self.texts(snapshot), [("assistant", "done")])
        self.assertEqual(snapshot.checkpoint, complete)

    def test_read_since_unlocatable_checkpoint_returns_none(self):
        self.write([self.message("user", "one"), self.message("assistant", "a1")])
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        self.write([self.message("user", "one")])
        self.assertIsNone(conversion.read_since(self.tool, self.ref, checkpoint))

    def test_claude_is_error_sets_failed(self):
        self.write([self.message("user", "run it")])
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        call = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}},
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "y"}},
                ],
            },
        }
        result = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "boom",
                        "is_error": True,
                    },
                    {"type": "tool_result", "tool_use_id": "t2", "content": "fine"},
                ],
            },
        }
        self.append([call, result])
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        calls = snapshot.transcript.turns[0].calls
        self.assertEqual([c.failed for c in calls], [True, False])
        self.assertEqual(calls[0].result, "boom")


class CodexReadSinceTests(ReadSinceCase):
    tool = "codex"
    message = staticmethod(codex_message)

    def test_codex_read_since_returns_only_appended_turns(self):
        self.write([self.message("user", "one"), self.message("assistant", "a1")])
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        self.append([self.message("user", "two"), self.message("assistant", "a2")])
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        self.assertEqual(self.texts(snapshot), [("user", "two"), ("assistant", "a2")])
        self.assertEqual(snapshot.checkpoint, conversion.complete_jsonl_cursor(self.path))

    def test_read_since_protected_checkpoint_survives_codex_context_rewrite(self):
        from ai_sessions.harnesses.codex import _protect_codex_checkpoint

        def developer(text):
            return {
                "type": "response_item",
                "payload": {"type": "message", "role": "developer", "content": text},
            }

        records = [developer("old"), self.message("user", "one"), self.message("assistant", "a1")]
        self.write(records)
        checkpoint = _protect_codex_checkpoint(self.ref, self.path.stat().st_size)
        self.assertIsInstance(checkpoint, str)
        self.write([developer("new longer instructions"), *records[1:]])
        self.append([self.message("user", "two")])
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        self.assertEqual(self.texts(snapshot), [("user", "two")])

    def test_codex_script_failed_and_timeout_set_failed(self):
        self.write([self.message("user", "run")])
        checkpoint = conversion.native_checkpoint(self.tool, self.ref)
        outputs = {
            "c1": "Script failed\nWall time: 1s\nOutput:\nnope",
            "c2": "command timed out after 10s\nOutput:\n",
            "c3": "Script completed\nWall time: 1s\nOutput:\nok",
        }
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "shell",
                    "arguments": json.dumps({"command": call_id}),
                },
            }
            for call_id in outputs
        ]
        records += [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
            for call_id, output in outputs.items()
        ]
        self.append(records)
        snapshot = conversion.read_since(self.tool, self.ref, checkpoint)
        calls = snapshot.transcript.turns[0].calls
        self.assertEqual([c.failed for c in calls], [True, True, False])


if __name__ == "__main__":
    unittest.main()
