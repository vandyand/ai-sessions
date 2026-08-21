"""Invariants for reading Codex compaction windows.

Every fixture here is generated. The behavior under test is a property of the
Codex transcript format, not of any particular session, so tying these checks
to a recorded transcript would make them unverifiable on another machine and
untestable in CI. See specs/conversation-log/codex-compaction-window/.
"""

import json
import tempfile
import unittest
from pathlib import Path

from ai_sessions import bridge
from ai_sessions.bridge import (
    count_compactions,
    from_last_compaction,
    handoff_note,
    read_transcript,
)


class PeakRecorder:
    """Record the most turns a read ever holds at once.

    Comparing final output length cannot distinguish replace-on-boundary from
    accumulate-everything-then-slice, so the invariant has to be measured
    where it actually happens.
    """

    def __enter__(self) -> "PeakRecorder":
        self.peak = 0
        self._message = bridge._Conversation.message
        self._carry = bridge._Conversation.carry_window
        recorder = self

        def message(conversation, role, text, compaction=False):
            recorder._message(conversation, role, text, compaction)
            recorder.peak = max(recorder.peak, len(conversation.turns))

        def carry(conversation, turns, sealed):
            recorder._carry(conversation, turns, sealed)
            recorder.peak = max(recorder.peak, len(conversation.turns))

        bridge._Conversation.message = message
        bridge._Conversation.carry_window = carry
        return self

    def __exit__(self, *exc: object) -> None:
        bridge._Conversation.message = self._message
        bridge._Conversation.carry_window = self._carry


def make_codex_rollout(
    path: Path,
    *,
    windows: int,
    per_window: int,
    tail: int,
    replacement_history: bool = True,
    sealed: bool = True,
    developer: bool = True,
    superseded_per_window: int = 3,
) -> Path:
    """Synthesize a Codex rollout with a configurable compaction history."""

    def message(role: str, text: str) -> dict:
        kind = "output_text" if role == "assistant" else "input_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}

    with path.open("w", encoding="utf-8") as handle:

        def record(payload: dict) -> None:
            handle.write(json.dumps(payload) + "\n")

        for window in range(windows):
            for index in range(superseded_per_window):
                record(
                    {
                        "type": "response_item",
                        "payload": message("user", f"superseded w{window} m{index}"),
                    }
                )
            history: list[dict] = []
            if developer:
                history.append(
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "text", "text": "<permissions instructions>"}],
                    }
                )
            history += [message("user", f"w{window} carried {i}") for i in range(per_window)]
            if sealed:
                history.append(
                    {
                        "type": "compaction",
                        "id": f"cmp_{window}",
                        "encrypted_content": "gAAAAAB" + "x" * 40,
                    }
                )
            payload = {"message": "", "window_number": window + 1}
            if replacement_history:
                payload["replacement_history"] = history
            record({"type": "compacted", "payload": payload})

        for index in range(tail):
            role = "assistant" if index % 2 else "user"
            record({"type": "response_item", "payload": message(role, f"live {index}")})

    return path


class CodexCompactionWindowTests(unittest.TestCase):
    """Invariants R1-R7 from the spec."""

    def rollout(self, **kwargs) -> Path:
        return make_codex_rollout(Path(tempfile.mkdtemp()) / "rollout.jsonl", **kwargs)

    def test_window_becomes_turns(self) -> None:
        """R1: the newest window's carried context plus the live tail, exactly."""
        path = self.rollout(windows=3, per_window=5, tail=4)
        turns = read_transcript("codex", path).turns
        expected = [f"w2 carried {i}" for i in range(5)] + [f"live {i}" for i in range(4)]
        self.assertEqual([turn.text for turn in turns], expected)

    def test_window_replaces_earlier_turns(self) -> None:
        path = self.rollout(windows=2, per_window=3, tail=1)
        texts = [turn.text for turn in read_transcript("codex", path).turns]
        self.assertFalse(any("superseded" in text for text in texts))
        self.assertFalse(any(text.startswith("w0 ") for text in texts))

    def test_exactly_one_boundary_marked(self) -> None:
        """R2/R3: one boundary, on the first carried turn, however many windows."""
        turns = read_transcript("codex", self.rollout(windows=7, per_window=4, tail=2)).turns
        self.assertEqual(count_compactions(turns), 1)
        self.assertTrue(turns[0].compaction)

    def test_from_last_compaction_is_a_noop(self) -> None:
        """R4: the reader already starts at the boundary."""
        turns = read_transcript("codex", self.rollout(windows=4, per_window=3, tail=2)).turns
        sliced, boundaries = from_last_compaction(list(turns))
        self.assertEqual(sliced, turns)
        self.assertEqual(boundaries, 1)

    def test_peak_is_independent_of_window_count(self) -> None:
        """R5: the invariant that rules out appending every window."""
        few = read_transcript("codex", self.rollout(windows=2, per_window=6, tail=3)).turns
        many = read_transcript("codex", self.rollout(windows=200, per_window=6, tail=3)).turns
        self.assertEqual(len(few), len(many))
        self.assertEqual(len(many), 6 + 3)

    def test_sealed_and_developer_entries_are_skipped(self) -> None:
        transcript = read_transcript("codex", self.rollout(windows=1, per_window=2, tail=0))
        texts = " ".join(turn.text for turn in transcript.turns)
        self.assertNotIn("permissions instructions", texts)
        self.assertNotIn("gAAAAAB", texts)
        self.assertTrue(transcript.sealed_summary)

    def test_missing_replacement_history_falls_back(self) -> None:
        """R6: an unreadable compaction leaves the surrounding history alone."""
        path = self.rollout(windows=1, per_window=4, tail=2, replacement_history=False)
        transcript = read_transcript("codex", path)
        self.assertEqual(transcript.opaque_compactions, 1)
        self.assertEqual(transcript.carried_windows, 0)
        self.assertTrue(any("superseded" in turn.text for turn in transcript.turns))
        self.assertEqual(count_compactions(transcript.turns), 0)

    def test_reset_clears_pending_tool_calls(self) -> None:
        """A result from a superseded window must not attach to a carried turn."""
        path = Path(tempfile.mkdtemp()) / "rollout.jsonl"
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "orphan",
                    "name": "bash",
                    "arguments": json.dumps({"command": "ls"}),
                },
            },
            {
                "type": "compacted",
                "payload": {
                    "message": "",
                    "window_number": 1,
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "carried"}],
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "orphan",
                    "output": "LEAKED",
                },
            },
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        turns = read_transcript("codex", path).turns
        self.assertEqual([turn.text for turn in turns], ["carried"])
        self.assertEqual(turns[0].calls, ())

    def test_transcript_without_compactions_is_unchanged(self) -> None:
        path = self.rollout(windows=0, per_window=0, tail=3, superseded_per_window=0)
        transcript = read_transcript("codex", path)
        self.assertEqual(count_compactions(transcript.turns), 0)
        self.assertEqual(transcript.carried_windows, 0)
        self.assertEqual(len(transcript.turns), 3)

    def test_read_claude_unchanged(self) -> None:
        """R7: Claude's single readable boundary still behaves as before."""
        path = Path(tempfile.mkdtemp()) / "claude.jsonl"
        records = [
            {"type": "user", "message": {"role": "user", "content": "before"}},
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "SUMMARY"},
            },
            {"type": "user", "message": {"role": "user", "content": "after"}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        turns = read_transcript("claude", path).turns
        self.assertEqual(count_compactions(turns), 1)
        sliced, _ = from_last_compaction(list(turns))
        self.assertEqual([turn.text for turn in sliced], ["SUMMARY", "after"])


class CodexCompactionNoteTests(unittest.TestCase):
    """R8: the copy must describe what actually crossed."""

    def test_note_does_not_claim_history_it_did_not_carry(self) -> None:
        note = handoff_note(
            source_tool="codex",
            target_tool="claude",
            session_id="abc",
            title="t",
            cwd="/tmp",
            kept=5,
            calls=0,
            dropped=0,
            compacted=366,
            sealed_summary=True,
        )
        self.assertNotIn("full pre-compaction history", note)
        self.assertIn("366", note)
        self.assertIn("seals its own summary", note)

    def test_unreadable_compaction_still_explains_the_fallback(self) -> None:
        note = handoff_note(
            source_tool="codex",
            target_tool="claude",
            session_id="abc",
            title="t",
            cwd="/tmp",
            kept=5,
            calls=0,
            dropped=0,
            opaque_compactions=2,
        )
        self.assertIn("full pre-compaction history", note)


class AdversarialReviewFindingsTests(unittest.TestCase):
    """Regressions found reviewing the shipped v3.1.4 change."""

    def rollout(self, **kwargs) -> Path:
        return make_codex_rollout(Path(tempfile.mkdtemp()) / "rollout.jsonl", **kwargs)

    def custom(self, records: list[dict]) -> Path:
        path = Path(tempfile.mkdtemp()) / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def item(role: str, text: str) -> dict:
        kind = "output_text" if role == "assistant" else "input_text"
        return {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
        }

    @staticmethod
    def compaction(entries: list[dict], window: int = 1) -> dict:
        return {
            "type": "compacted",
            "payload": {"message": "", "window_number": window, "replacement_history": entries},
        }

    @staticmethod
    def carried(text: str = "carried", role: str = "user") -> dict:
        kind = "output_text" if role == "assistant" else "input_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}

    def test_latest_window_false_still_replays_the_whole_transcript(self) -> None:
        """The documented opt-out must survive the reader adopting windows."""
        path = self.custom(
            [
                self.item("user", "original"),
                self.compaction([self.carried()]),
                self.item("user", "recent"),
            ]
        )
        whole = read_transcript("codex", path, latest_window=False).turns
        self.assertIn("original", [turn.text for turn in whole])
        newest = read_transcript("codex", path, latest_window=True).turns
        self.assertNotIn("original", [turn.text for turn in newest])

    def test_peak_does_not_grow_with_compaction_count(self) -> None:
        """R5 measured where it happens, not inferred from output length."""
        with PeakRecorder() as few:
            read_transcript(
                "codex", self.rollout(windows=2, per_window=3, tail=2, superseded_per_window=5)
            )
        with PeakRecorder() as many:
            read_transcript(
                "codex", self.rollout(windows=200, per_window=3, tail=2, superseded_per_window=5)
            )
        self.assertEqual(few.peak, many.peak)
        self.assertLess(many.peak, 30)

    def test_peak_is_bounded_by_the_span_between_boundaries(self) -> None:
        """The honest bound: the largest run between boundaries, not the file."""
        with PeakRecorder() as recorder:
            read_transcript(
                "codex", self.rollout(windows=3, per_window=2, tail=1, superseded_per_window=40)
            )
        self.assertGreaterEqual(recorder.peak, 40)
        self.assertLess(recorder.peak, 60)

    def test_note_does_not_claim_to_resume_where_the_source_does(self) -> None:
        """A readable window followed by an unreadable one moves the resume point."""
        path = self.custom(
            [
                self.item("user", "a"),
                self.compaction([self.carried()]),
                self.item("user", "b"),
                {"type": "compacted", "payload": {"message": "", "window_number": 2}},
                self.item("user", "c"),
            ]
        )
        transcript = read_transcript("codex", path)
        self.assertFalse(transcript.resumes_at_last_summary)
        note = handoff_note(
            source_tool="codex",
            target_tool="claude",
            session_id="abc",
            title="t",
            cwd="/tmp",
            kept=3,
            calls=0,
            dropped=0,
            compacted=transcript.carried_windows,
            resumes_at_last_summary=transcript.resumes_at_last_summary,
        )
        self.assertNotIn("which is where the source session itself is picking up", note)
        self.assertIn("could not be read", note)

    def test_unreadable_window_keeps_surrounding_history_deliberately(self) -> None:
        """A window whose entries are all unreadable takes the fallback path.

        Its carried context is real but sealed, so this is the same situation
        as a record with no window at all: a superseded-but-readable history
        beats an empty copy, and the note says so.
        """
        developer = {
            "type": "message",
            "role": "developer",
            "content": [{"type": "text", "text": "<permissions instructions>"}],
        }
        path = self.custom(
            [self.item("user", "stale"), self.compaction([developer]), self.item("user", "after")]
        )
        transcript = read_transcript("codex", path)
        self.assertEqual(transcript.opaque_compactions, 1)
        self.assertEqual(transcript.carried_windows, 0)
        self.assertEqual([turn.text for turn in transcript.turns], ["stale", "after"])

    def test_assistant_entries_inside_a_window_are_carried(self) -> None:
        """The fixtures previously only ever generated user entries."""
        path = self.custom(
            [self.compaction([self.carried("asked"), self.carried("answered", "assistant")])]
        )
        turns = read_transcript("codex", path).turns
        self.assertEqual(
            [(t.role, t.text) for t in turns], [("user", "asked"), ("assistant", "answered")]
        )

    def test_non_message_entries_are_not_conversation(self) -> None:
        """A window entry that is not a message must not become a turn."""
        path = self.custom(
            [
                self.compaction(
                    [
                        {
                            "type": "bookkeeping",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "NOT A MESSAGE"}],
                        },
                        self.carried("real"),
                    ]
                )
            ]
        )
        turns = read_transcript("codex", path).turns
        self.assertEqual([turn.text for turn in turns], ["real"])


if __name__ == "__main__":
    unittest.main()
