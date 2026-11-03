import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.model import NativeRef, ReadSnapshot, ToolCall, Transcript, Turn
from ai_sessions.supervisor import index
from ai_sessions.supervisor.index import (
    Exchange,
    TargetIndex,
    clean_exchange,
    exchanges_from_turns,
)


def group(turns, **kw):
    options = {"member": "m", "start_seq": 1, "tool": "t", "pastes": []}
    options.update(kw)
    return exchanges_from_turns(turns, **options)


class ExchangeTests(unittest.TestCase):
    def test_exchanges_group_by_user_prompt(self):
        call = ToolCall("Bash", "ls", "ok")
        turns = [
            Turn("user", "first"),
            Turn("assistant", "working", (call,)),
            Turn("assistant", "done"),
            Turn("user", "second"),
            Turn("assistant", "ok"),
        ]
        result = group(turns)
        self.assertEqual([e.seq for e in result], [1, 2])
        self.assertEqual(result[0].assistant, ("working", "done"))
        self.assertEqual(result[0].calls, (call,))
        self.assertEqual(result[1].prompt, "second")
        self.assertEqual(result[1].assistant, ("ok",))

    def test_injected_context_does_not_start_an_exchange(self):
        turns = [
            Turn("user", "# AGENTS.md instructions for C:/x\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>"),
            Turn("user", "fix the retry loop"),
            Turn("assistant", "done"),
        ]
        result = group(turns, injected=lambda text: text.startswith("# AGENTS.md"))
        self.assertEqual([(e.seq, e.prompt) for e in result], [(1, "fix the retry loop")])

    def test_codex_adapter_marks_agents_md_as_injected(self):
        from ai_sessions.registry import REGISTRY

        injected = REGISTRY.get("codex").injected_prompt
        self.assertTrue(injected("# AGENTS.md instructions for C:/x\n\n<INSTRUCTIONS>\nhi"))
        self.assertTrue(injected("<environment_context>\n</environment_context>"))
        self.assertFalse(injected("please read AGENTS.md"))

    def test_preamble_gets_seq_zero(self):
        result = group([Turn("assistant", "early"), Turn("user", "hi")])
        self.assertEqual([e.seq for e in result], [0, 1])

    def test_clean_exchange_clips_tool_lines_to_400(self):
        ex = group([Turn("user", "p"), Turn("assistant", "", (ToolCall("Bash", "x" * 900, "y"),))])[
            0
        ]
        lines = clean_exchange(ex).split("\n\n")
        self.assertEqual(lines[0], "p")
        self.assertEqual(len(lines[1]), 400)
        self.assertTrue(lines[1].startswith("⟦Bash⟧ xxx"))

    def test_failed_call_marked_in_cleaned_text(self):
        calls = (ToolCall("Bash", "bad", "exit 1", failed=True), ToolCall("Bash", "good", "ok"))
        ex = group([Turn("user", "p"), Turn("assistant", "", calls)])[0]
        text = clean_exchange(ex)
        self.assertIn("✗ ⟦Bash⟧ bad → exit 1", text)
        self.assertIn("\n\n⟦Bash⟧ good → ok", text)

    def test_compaction_rendered_as_marker_without_summary_text(self):
        turns = [
            Turn("user", "p"),
            Turn("assistant", "a"),
            Turn("user", "SECRET SUMMARY", compaction=True),
        ]
        ex = group(turns)[0]
        self.assertTrue(ex.compaction)
        text = clean_exchange(ex)
        self.assertIn("[target compacted here]", text)
        self.assertNotIn("SECRET SUMMARY", text)

    def test_json_round_trip(self):
        ex = Exchange(3, "m", "p", "t", ("a",), (ToolCall("B", "r", "s", True),), True)
        self.assertEqual(Exchange.from_json(ex.to_json()), ex)

    def test_pastes_are_replaced_in_typed_text(self):
        ex = group([Turn("user", "see ```" + "z" * 900 + "```")])[0]
        self.assertIn("[pasted 900 chars:", ex.typed)
        self.assertTrue(ex.prompt.startswith("see ```"))


class TargetIndexTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "index.jsonl"
        self.ref = NativeRef("s", "p")

    @staticmethod
    def snapshot(turns, checkpoint):
        return ReadSnapshot(Transcript(turns), checkpoint)

    def test_index_extend_continues_open_exchange(self):
        store = TargetIndex(self.path)
        first = self.snapshot([Turn("user", "go"), Turn("assistant", "start")], 10)
        with patch.object(index.conversion, "read_snapshot", return_value=first):
            checkpoint = store.build("claude", self.ref, "m")
        self.assertEqual(checkpoint, 10)
        self.assertEqual(len(store.exchanges()), 1)
        rest = self.snapshot(
            [
                Turn("assistant", "finish", (ToolCall("Bash", "ls", "ok"),)),
                Turn("user", "next"),
            ],
            20,
        )
        with patch.object(index.conversion, "read_since", return_value=rest):
            self.assertEqual(store.extend("claude", self.ref, "m", 10), 20)
        stored = store.exchanges()
        self.assertEqual([e.seq for e in stored], [1, 2])
        self.assertEqual(stored[0].assistant, ("start", "finish"))
        self.assertEqual(len(stored[0].calls), 1)
        self.assertEqual(stored[1].prompt, "next")
        self.assertEqual(store.last_seq(), 2)

    def test_extend_returns_none_when_checkpoint_unlocatable(self):
        store = TargetIndex(self.path)
        with patch.object(index.conversion, "read_since", return_value=None):
            self.assertIsNone(store.extend("claude", self.ref, "m", 5))

    def test_append_replaces_trailing_exchange_with_same_seq(self):
        store = TargetIndex(self.path)
        store.append([Exchange(1, "m", "a", "a")])
        store.append([Exchange(1, "m", "a", "a", ("x",)), Exchange(2, "m", "b", "b")])
        self.assertEqual([e.seq for e in store.exchanges()], [1, 2])
        self.assertEqual(store.exchanges()[0].assistant, ("x",))

    def test_index_append_is_atomic_on_failure(self):
        store = TargetIndex(self.path)
        store.append([Exchange(1, "m", "a", "a")])
        before = self.path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                store.append([Exchange(2, "m", "b", "b")])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])


if __name__ == "__main__":
    unittest.main()
