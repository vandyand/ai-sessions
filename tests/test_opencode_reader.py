import gc
import json
import sqlite3
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import diagnostics
from ai_sessions.conversion import (
    BridgeError,
    conversation_change_status,
    from_last_compaction,
    read_snapshot,
)
from ai_sessions.harnesses import opencode
from ai_sessions.harnesses.opencode_semantics import (
    BOOKKEEPING_PART_KINDS,
    COMPACTION_PROMPT,
    FILE_SOURCE_TYPES,
    INTERRUPTED_RESULT,
    MESSAGE_ROLES,
    PART_KINDS,
    TOOL_STATE_STATUSES,
    StoredMessage,
    apply_revert,
    project_transcript,
    select_compacted,
    semantic_checkpoint,
)
from ai_sessions.model import NativeRef

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    directory TEXT NOT NULL,
    title TEXT NOT NULL,
    agent TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    time_archived INTEGER,
    revert TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX message_session_time ON message(session_id, time_created, id);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX part_session_message ON part(session_id, message_id, id);
"""
SEMANTICS_FIXTURE = Path(__file__).parent / "fixtures" / "opencode-1.18.21-semantics.json"


def text(value: str, *, part_id: str = "") -> dict[str, object]:
    result: dict[str, object] = {"type": "text", "text": value}
    if part_id:
        result["_id"] = part_id
    return result


def tool_part(status: str, *, part_id: str = "prt_tool") -> dict[str, object]:
    state: dict[str, object] = {"status": status, "input": {"command": "echo hi"}}
    if status == "pending":
        state["raw"] = '{"command":"echo hi"}'
    elif status == "running":
        state.update({"title": "shell", "metadata": {}, "time": {"start": 1}})
    elif status == "completed":
        state.update(
            {
                "output": "hi",
                "title": "shell",
                "metadata": {},
                "time": {"start": 1, "end": 2},
            }
        )
    elif status == "error":
        state.update({"error": "boom", "metadata": {}, "time": {"start": 1, "end": 2}})
    else:
        raise ValueError(status)
    return {
        "_id": part_id,
        "type": "tool",
        "callID": f"call_{status}",
        "tool": "shell",
        "state": state,
    }


def stored(
    message_id: str,
    role: str,
    *parts: dict[str, object],
    created: int,
    parent_id: str = "msg_parent",
    summary: bool = False,
    finish: str | None = None,
    error: dict[str, object] | None = None,
) -> StoredMessage:
    info: dict[str, object] = {"role": role, "id": message_id, "sessionID": "ses_test"}
    if role == "assistant":
        info["parentID"] = parent_id
        if summary:
            info["summary"] = True
        if finish is not None:
            info["finish"] = finish
        if error is not None:
            info["error"] = error
    hydrated = []
    for index, source in enumerate(parts):
        part = dict(source)
        part_id = str(part.pop("_id", f"prt_{message_id}_{index}"))
        hydrated.append({**part, "id": part_id, "messageID": message_id, "sessionID": "ses_test"})
    return StoredMessage(message_id, created, info, tuple(hydrated))


class OpenCodeSemanticAlgorithmTests(unittest.TestCase):
    # Behavioral fixtures pin OpenCode 1.18.21 MessageV2.filterCompacted:
    # https://github.com/anomalyco/opencode/blob/3a31c4ea801915c0b050df4b3842997ea62b6e93/packages/opencode/src/session/message-v2.ts
    def test_audited_part_kind_list_is_exact_and_unknown_kinds_fail(self) -> None:
        captured = json.loads(SEMANTICS_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(captured["opencode_version"], "1.18.21")
        self.assertEqual(captured["commit"], "3a31c4ea801915c0b050df4b3842997ea62b6e93")
        self.assertEqual(PART_KINDS, set(captured["part_kinds"]))
        self.assertEqual(
            BOOKKEEPING_PART_KINDS,
            set(captured["adapter_bookkeeping_part_kinds"]),
        )
        self.assertEqual(COMPACTION_PROMPT, captured["compaction_prompt"])
        self.assertEqual(captured["message_order"], ["time_created", "id"])
        self.assertEqual(captured["message_stream_direction"], "newest-first")
        self.assertEqual(captured["part_order"], ["id"])
        self.assertEqual(MESSAGE_ROLES, set(captured["message_roles"]))
        self.assertEqual(TOOL_STATE_STATUSES, set(captured["tool_state_statuses"]))
        self.assertEqual(FILE_SOURCE_TYPES, set(captured["file_source_types"]))
        self.assertIn("optional_nulls", captured["adapter_policy"])
        self.assertIn("empty_tail_start_id", captured["adapter_policy"])
        self.assertIn(
            "only when the newest completed summary", captured["adapter_policy"]["reorder_anchor"]
        )
        message = stored("msg_1", "user", {"type": "future-kind"}, created=1)
        with self.assertRaisesRegex(BridgeError, "unknown value 'future-kind'"):
            semantic_checkpoint([message], None)
        with self.assertRaisesRegex(BridgeError, "unknown value 'future-kind'"):
            project_transcript([message], None, latest_window=True)

    def test_completed_compaction_reorders_summary_retained_tail_and_continuation(self) -> None:
        messages = [
            stored("msg_u1", "user", text("old"), created=1),
            stored("msg_a1", "assistant", text("old reply"), created=2, parent_id="msg_u1"),
            stored("msg_u2", "user", text("retained"), created=3),
            stored("msg_a2", "assistant", text("retained reply"), created=4, parent_id="msg_u2"),
            stored(
                "msg_compact",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u2"},
                created=5,
            ),
            stored(
                "msg_summary",
                "assistant",
                text("summary"),
                created=6,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
            stored("msg_u3", "user", text("continue"), created=7),
        ]
        self.assertEqual(
            [message.message_id for message in select_compacted(messages).messages],
            ["msg_compact", "msg_summary", "msg_u2", "msg_a2", "msg_u3"],
        )
        latest = project_transcript(messages, None, latest_window=True)
        self.assertEqual(
            [turn.text for turn in latest.turns],
            [COMPACTION_PROMPT, "summary", "retained", "retained reply", "continue"],
        )
        self.assertEqual(
            [turn.compaction for turn in latest.turns], [False, True, False, False, False]
        )
        self.assertEqual(latest.carried_windows, 1)
        self.assertTrue(latest.resumes_at_last_summary)
        full = project_transcript(messages, None, latest_window=False)
        self.assertEqual(
            [turn.text for turn in full.turns],
            [
                "old",
                "old reply",
                "retained",
                "retained reply",
                COMPACTION_PROMPT,
                "summary",
                "continue",
            ],
        )
        self.assertEqual(full.carried_windows, 1)
        self.assertTrue(full.resumes_at_last_summary)

    def test_adapter_keeps_completed_anchor_when_newer_compaction_is_incomplete(self) -> None:
        messages = [
            stored("msg_u1", "user", text("old"), created=1),
            stored("msg_u2", "user", text("retained"), created=2),
            stored("msg_a2", "assistant", text("reply"), created=3, parent_id="msg_u2"),
            stored(
                "msg_c1",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u2"},
                created=4,
            ),
            stored(
                "msg_s1",
                "assistant",
                text("summary"),
                created=5,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
            stored("msg_u3", "user", text("continue"), created=6),
            stored(
                "msg_c2",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u3"},
                created=7,
            ),
        ]
        selection = select_compacted(messages)
        self.assertEqual(selection.governing_compaction_id, "msg_c1")
        self.assertEqual(selection.summary_id, "msg_s1")
        self.assertEqual(
            [message.message_id for message in selection.messages],
            ["msg_c1", "msg_s1", "msg_u2", "msg_a2", "msg_u3", "msg_c2"],
        )

    def test_adapter_anchors_retry_order_on_successful_summary(self) -> None:
        messages = [
            stored("msg_u1", "user", text("old"), created=1),
            stored("msg_u2", "user", text("retained"), created=2),
            stored(
                "msg_c1",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u2"},
                created=3,
            ),
            stored(
                "msg_bad",
                "assistant",
                text("failed"),
                created=4,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
                error={"name": "failure"},
            ),
            stored(
                "msg_good",
                "assistant",
                text("summary"),
                created=5,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
            stored("msg_u3", "user", text("continue"), created=6),
        ]
        selection = select_compacted(messages)
        self.assertEqual(selection.summary_id, "msg_good")
        self.assertEqual(
            [message.message_id for message in selection.messages],
            ["msg_c1", "msg_bad", "msg_good", "msg_u2", "msg_u3"],
        )

    def test_abort_then_retry_keeps_retained_tail_after_governing_summary(self) -> None:
        messages = [
            stored("msg_old", "user", text("old"), created=1),
            stored("msg_tail", "user", text("retained question"), created=2),
            stored(
                "msg_tail_reply",
                "assistant",
                text("retained answer"),
                created=3,
                parent_id="msg_tail",
            ),
            stored(
                "msg_compact",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_tail"},
                created=4,
            ),
            stored(
                "msg_aborted",
                "assistant",
                text("partial summary before abort"),
                created=5,
                parent_id="msg_compact",
                summary=True,
                error={"name": "AbortError"},
            ),
            stored(
                "msg_summary",
                "assistant",
                text("governing summary"),
                created=6,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
            stored("msg_new", "user", text("newest"), created=7),
        ]
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual(
            [turn.text for turn in transcript.turns],
            [
                COMPACTION_PROMPT,
                "partial summary before abort",
                "governing summary",
                "retained question",
                "retained answer",
                "newest",
            ],
        )
        bridged, count = from_last_compaction(transcript.turns)
        self.assertEqual(count, 1)
        self.assertEqual(
            [turn.text for turn in bridged],
            ["governing summary", "retained question", "retained answer", "newest"],
        )

    def test_failed_only_compaction_retains_full_history_and_governs_no_window(self) -> None:
        messages = [
            stored("msg_old", "user", text("early context"), created=1),
            stored(
                "msg_old_reply",
                "assistant",
                text("early reply"),
                created=2,
                parent_id="msg_old",
            ),
            stored("msg_tail", "user", text("retained question"), created=3),
            stored(
                "msg_tail_reply",
                "assistant",
                text("retained answer"),
                created=4,
                parent_id="msg_tail",
            ),
            stored(
                "msg_compact",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_tail"},
                created=5,
            ),
            stored(
                "msg_failed",
                "assistant",
                text("partial summary"),
                created=6,
                parent_id="msg_compact",
                summary=True,
                error={"name": "ProviderError"},
            ),
            stored("msg_new", "user", text("user keeps chatting"), created=7),
        ]
        selection = select_compacted(messages)
        self.assertEqual(selection.governing_compaction_id, "")
        self.assertEqual(selection.messages, messages)
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual(transcript.carried_windows, 0)
        self.assertFalse(transcript.resumes_at_last_summary)
        self.assertEqual(
            [turn.text for turn in transcript.turns],
            [
                "early context",
                "early reply",
                "retained question",
                "retained answer",
                COMPACTION_PROMPT,
                "partial summary",
                "user keeps chatting",
            ],
        )
        bridged, count = from_last_compaction(transcript.turns)
        self.assertEqual(count, 0)
        self.assertEqual(bridged, transcript.turns)

    def test_adapter_marks_only_newest_successful_retry_as_boundary(self) -> None:
        messages = [
            stored("msg_u2", "user", text("retained"), created=1),
            stored(
                "msg_c1",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u2"},
                created=2,
            ),
            stored(
                "msg_s1",
                "assistant",
                text("summary one"),
                created=3,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
            stored(
                "msg_s2",
                "assistant",
                text("summary two"),
                created=4,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
        ]
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual(transcript.carried_windows, 1)
        self.assertEqual(
            [(turn.text, turn.compaction) for turn in transcript.turns],
            [
                (COMPACTION_PROMPT, False),
                ("summary one", False),
                ("summary two", True),
                ("retained", False),
            ],
        )

    def test_overlapping_retained_tail_marks_only_governing_summary(self) -> None:
        messages = [
            stored("msg_u0", "user", text("oldest retained"), created=1),
            stored("msg_c0", "user", {"type": "compaction", "auto": True}, created=2),
            stored(
                "msg_s0",
                "assistant",
                text("stale summary"),
                created=3,
                parent_id="msg_c0",
                summary=True,
                finish="stop",
            ),
            stored("msg_mid", "user", text("mid"), created=4),
            stored(
                "msg_c1",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_u0"},
                created=5,
            ),
            stored(
                "msg_s1",
                "assistant",
                text("governing summary"),
                created=6,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
            stored("msg_new", "user", text("newest"), created=7),
        ]
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual(
            [turn.text for turn in transcript.turns],
            [
                COMPACTION_PROMPT,
                "governing summary",
                "oldest retained",
                COMPACTION_PROMPT,
                "stale summary",
                "mid",
                "newest",
            ],
        )
        self.assertEqual(sum(turn.compaction for turn in transcript.turns), 1)
        bridged, count = from_last_compaction(transcript.turns)
        self.assertEqual(count, 1)
        self.assertEqual(
            [turn.text for turn in bridged],
            [
                "governing summary",
                "oldest retained",
                COMPACTION_PROMPT,
                "stale summary",
                "mid",
                "newest",
            ],
        )

    def test_latest_window_with_no_governing_compaction_marks_no_stale_summary(self) -> None:
        messages = [
            stored("msg_old", "user", text("important early context"), created=1),
            stored(
                "msg_reply",
                "assistant",
                text("early reply"),
                created=2,
                parent_id="msg_old",
            ),
            # This completed summary sorts before its named compaction parent.
            # The native newest-first scan therefore finds no governing window.
            stored(
                "msg_summary",
                "assistant",
                text("stale summary"),
                created=3,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
            stored(
                "msg_compact",
                "user",
                {"type": "compaction", "auto": True},
                created=4,
            ),
            stored("msg_new", "user", text("newest"), created=5),
        ]
        selection = select_compacted(messages)
        self.assertEqual(selection.governing_compaction_id, "")
        self.assertEqual(selection.summary_id, "")
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual(transcript.carried_windows, 0)
        self.assertFalse(transcript.resumes_at_last_summary)
        self.assertFalse(any(turn.compaction for turn in transcript.turns))
        bridged, count = from_last_compaction(transcript.turns)
        self.assertEqual(count, 0)
        self.assertEqual(bridged, transcript.turns)

    def test_dangling_tail_is_reported_while_preserving_native_full_history_fallback(self) -> None:
        messages = [
            stored("msg_old", "user", text("old"), created=1),
            stored(
                "msg_c1",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": "msg_missing"},
                created=2,
            ),
            stored(
                "msg_s1",
                "assistant",
                text("summary"),
                created=3,
                parent_id="msg_c1",
                summary=True,
                finish="stop",
            ),
        ]
        selection = select_compacted(messages)
        self.assertTrue(selection.unresolved_tail)
        self.assertEqual(
            [message.message_id for message in selection.messages],
            ["msg_old", "msg_c1", "msg_s1"],
        )

    def test_tail_at_governing_compaction_is_a_resolved_empty_tail(self) -> None:
        messages = [
            stored("msg_old", "user", text("old"), created=1),
            stored(
                "msg_compact",
                "user",
                {
                    "type": "compaction",
                    "auto": True,
                    "tail_start_id": "msg_compact",
                },
                created=2,
            ),
            stored(
                "msg_summary",
                "assistant",
                text("summary"),
                created=3,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
        ]
        selection = select_compacted(messages)
        self.assertFalse(selection.unresolved_tail)
        self.assertEqual(
            [message.message_id for message in selection.messages],
            ["msg_compact", "msg_summary"],
        )

    def test_empty_tail_start_matches_absent_tail_in_checkpoint_and_read(self) -> None:
        absent = [
            stored("msg_compact", "user", {"type": "compaction", "auto": True}, created=1),
            stored(
                "msg_summary",
                "assistant",
                text("summary"),
                created=2,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
        ]
        empty = [
            stored(
                "msg_compact",
                "user",
                {"type": "compaction", "auto": True, "tail_start_id": ""},
                created=1,
            ),
            absent[1],
        ]
        self.assertEqual(semantic_checkpoint(absent, None), semantic_checkpoint(empty, None))
        self.assertEqual(
            project_transcript(absent, None, latest_window=True),
            project_transcript(empty, None, latest_window=True),
        )

    def test_completed_compaction_without_retained_tail_starts_at_request(self) -> None:
        messages = [
            stored("msg_old", "user", text("old"), created=1),
            stored("msg_compact", "user", {"type": "compaction", "auto": False}, created=2),
            stored(
                "msg_summary",
                "assistant",
                text("summary"),
                created=3,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
            stored("msg_after", "user", text("after"), created=4),
        ]
        self.assertEqual(
            [turn.text for turn in project_transcript(messages, None, latest_window=True).turns],
            [COMPACTION_PROMPT, "summary", "after"],
        )

    def test_incomplete_or_failed_summary_does_not_compact(self) -> None:
        variants = (
            {"summary": True, "finish": None, "error": None},
            {"summary": False, "finish": "stop", "error": None},
            {"summary": True, "finish": "stop", "error": {"name": "failure"}},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                messages = [
                    stored("msg_old", "user", text("old"), created=1),
                    stored("msg_compact", "user", {"type": "compaction", "auto": True}, created=2),
                    stored(
                        "msg_summary",
                        "assistant",
                        text("summary"),
                        created=3,
                        parent_id="msg_compact",
                        **variant,
                    ),
                    stored("msg_after", "user", text("after"), created=4),
                ]
                transcript = project_transcript(messages, None, latest_window=True)
                self.assertEqual(
                    [turn.text for turn in transcript.turns],
                    ["old", COMPACTION_PROMPT, "summary", "after"],
                )
                self.assertEqual(transcript.carried_windows, 0)

    def test_staged_revert_matches_cleanup_boundaries(self) -> None:
        messages = [
            stored("msg_u1", "user", text("one"), created=1),
            stored(
                "msg_a1",
                "assistant",
                text("first", part_id="prt_first"),
                text("second", part_id="prt_second"),
                created=2,
                parent_id="msg_u1",
            ),
            stored("msg_u2", "user", text("later"), created=3),
        ]
        self.assertEqual(
            [item.message_id for item in apply_revert(messages, {"messageID": "msg_a1"})],
            ["msg_u1"],
        )
        partial = apply_revert(messages, {"messageID": "msg_a1", "partID": "prt_second"})
        self.assertEqual([item.message_id for item in partial], ["msg_u1", "msg_a1"])
        self.assertEqual([part["id"] for part in partial[-1].parts], ["prt_first"])
        missing_part = apply_revert(messages, {"messageID": "msg_a1", "partID": "prt_missing"})
        self.assertEqual([item.message_id for item in missing_part], ["msg_u1", "msg_a1"])
        self.assertEqual(len(missing_part[-1].parts), 2)
        self.assertEqual(
            apply_revert(messages, {"messageID": "msg_missing"}),
            messages,
        )

    def test_all_tool_states_project_without_dangling_calls(self) -> None:
        message = stored(
            "msg_tools",
            "assistant",
            *(
                tool_part(status, part_id=f"prt_{status}")
                for status in (
                    "pending",
                    "running",
                    "completed",
                    "error",
                )
            ),
            created=1,
        )
        transcript = project_transcript([message], None, latest_window=True)
        self.assertEqual(len(transcript.turns), 1)
        calls = transcript.turns[0].calls
        self.assertEqual([call.name for call in calls], ["shell"] * 4)
        self.assertEqual([call.request for call in calls], ["echo hi"] * 4)
        self.assertEqual(
            [call.result for call in calls],
            [INTERRUPTED_RESULT, INTERRUPTED_RESULT, "hi", "[Tool error] boom"],
        )

    def test_semantic_placeholders_are_bounded_and_bookkeeping_is_ignored(self) -> None:
        message = stored(
            "msg_parts",
            "user",
            {
                "type": "file",
                "mime": "image/png",
                "filename": "screen.png",
                "url": "data:image/png;base64," + "x" * 10_000,
            },
            {"type": "agent", "name": "explore"},
            {
                "type": "subtask",
                "prompt": "p" * 2_000,
                "description": "inspect",
                "agent": "explore",
            },
            {"type": "reasoning", "text": "private"},
            {"type": "snapshot", "snapshot": "opaque"},
            created=1,
        )
        turn = project_transcript([message], None, latest_window=True).turns[0]
        self.assertIn("screen.png", turn.text)
        self.assertIn("agent selected: explore", turn.text)
        self.assertIn("subtask for explore", turn.text)
        self.assertNotIn("private", turn.text)
        self.assertNotIn("base64", turn.text)
        self.assertLessEqual(max(map(len, turn.text.split("\n\n"))), 500)

    def test_resource_source_without_filename_uses_uri_placeholder(self) -> None:
        message = stored(
            "msg_resource",
            "user",
            {
                "type": "file",
                "mime": "text/plain",
                "url": "https://example.invalid/content",
                "source": {
                    "type": "resource",
                    "clientName": "docs",
                    "uri": "resource://docs/readme",
                    "text": {"value": "@readme", "start": 0, "end": 7},
                },
            },
            created=1,
        )
        turn = project_transcript([message], None, latest_window=True).turns[0]
        self.assertEqual(turn.text, "[Attached text/plain: resource://docs/readme]")

    def test_null_filename_matches_absent_filename_semantically(self) -> None:
        absent = stored(
            "msg_file",
            "user",
            {"type": "file", "mime": "text/plain", "url": "file:///note.txt"},
            created=1,
        )
        explicit_null = stored(
            "msg_file",
            "user",
            {
                "type": "file",
                "mime": "text/plain",
                "url": "file:///note.txt",
                "filename": None,
            },
            created=1,
        )
        self.assertEqual(
            semantic_checkpoint([absent], None),
            semantic_checkpoint([explicit_null], None),
        )
        self.assertEqual(
            project_transcript([absent], None, latest_window=True),
            project_transcript([explicit_null], None, latest_window=True),
        )

    def test_optional_null_fields_match_absent_fields_semantically(self) -> None:
        file_part = {"type": "file", "mime": "text/plain", "url": "file:///note.txt"}
        agent_part = {"type": "agent", "name": "build"}
        subtask_part = {
            "type": "subtask",
            "prompt": "inspect",
            "description": "task",
            "agent": "build",
        }
        running = tool_part("running")
        running["state"].pop("metadata")
        failed = tool_part("error")
        failed["state"].pop("metadata")
        cases = (
            ("file source", "user", file_part, "source"),
            ("agent source", "user", agent_part, "source"),
            ("subtask model", "user", subtask_part, "model"),
            ("subtask command", "user", subtask_part, "command"),
            ("running metadata", "assistant", running, "metadata"),
            ("error metadata", "assistant", failed, "metadata"),
        )
        for name, role, base_part, field in cases:
            with self.subTest(name=name):
                absent_part = json.loads(json.dumps(base_part))
                explicit_null_part = json.loads(json.dumps(base_part))
                if base_part["type"] == "tool":
                    explicit_null_part["state"][field] = None
                else:
                    explicit_null_part[field] = None
                absent = stored("msg_optional", role, absent_part, created=1)
                explicit_null = stored("msg_optional", role, explicit_null_part, created=1)
                self.assertEqual(
                    semantic_checkpoint([absent], None),
                    semantic_checkpoint([explicit_null], None),
                )
                self.assertEqual(
                    project_transcript([absent], None, latest_window=True),
                    project_transcript([explicit_null], None, latest_window=True),
                )

    def test_empty_governing_summary_does_not_claim_a_projected_window(self) -> None:
        messages = [
            stored("msg_old", "user", text("old"), created=1),
            stored("msg_compact", "user", {"type": "compaction", "auto": True}, created=2),
            stored(
                "msg_summary",
                "assistant",
                text(""),
                created=3,
                parent_id="msg_compact",
                summary=True,
                finish="stop",
            ),
        ]
        transcript = project_transcript(messages, None, latest_window=True)
        self.assertEqual([turn.text for turn in transcript.turns], [COMPACTION_PROMPT])
        self.assertEqual(transcript.carried_windows, 0)
        self.assertFalse(transcript.resumes_at_last_summary)

    def test_unknown_role_tool_status_and_file_source_type_fail_closed(self) -> None:
        cases = (
            (
                stored("msg_role", "future", text("value"), created=1),
                "role has unknown value 'future'",
            ),
            (
                stored(
                    "msg_tool",
                    "assistant",
                    {
                        "type": "tool",
                        "callID": "call_future",
                        "tool": "shell",
                        "state": {"status": "future", "input": {}},
                    },
                    created=1,
                ),
                "status has unknown value 'future'",
            ),
            (
                stored(
                    "msg_file",
                    "user",
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "url": "file:///note.txt",
                        "source": {
                            "type": "future",
                            "text": {"value": "@note", "start": 0, "end": 5},
                        },
                    },
                    created=1,
                ),
                "source.type has unknown value 'future'",
            ),
        )
        for message, failure in cases:
            with self.subTest(failure=failure):
                with self.assertRaisesRegex(BridgeError, failure):
                    semantic_checkpoint([message], None)
                with self.assertRaisesRegex(BridgeError, failure):
                    project_transcript([message], None, latest_window=True)

    def test_null_assistant_error_matches_absent_error_in_filter_and_digest(self) -> None:
        absent = stored(
            "msg_summary",
            "assistant",
            text("summary"),
            created=2,
            parent_id="msg_compact",
            summary=True,
            finish="stop",
        )
        info = dict(absent.info)
        info["error"] = None
        explicit_null = StoredMessage(absent.message_id, absent.time_created, info, absent.parts)
        compact = stored("msg_compact", "user", {"type": "compaction", "auto": True}, created=1)
        self.assertEqual(
            [message.message_id for message in select_compacted([compact, absent]).messages],
            ["msg_compact", "msg_summary"],
        )
        self.assertEqual(
            [message.message_id for message in select_compacted([compact, explicit_null]).messages],
            ["msg_compact", "msg_summary"],
        )
        self.assertEqual(
            semantic_checkpoint([compact, absent], None),
            semantic_checkpoint([compact, explicit_null], None),
        )

    def test_tool_raw_and_attachments_are_semantic(self) -> None:
        pending = tool_part("pending")
        base = stored("msg_tool", "assistant", pending, created=1)
        changed_raw = json.loads(json.dumps(pending))
        changed_raw["state"]["raw"] = "different"
        self.assertNotEqual(
            semantic_checkpoint([base], None),
            semantic_checkpoint([stored("msg_tool", "assistant", changed_raw, created=1)], None),
        )
        completed = tool_part("completed")
        completed["state"]["attachments"] = [
            {
                "id": "prt_attachment",
                "messageID": "msg_tool",
                "sessionID": "ses_test",
                "type": "file",
                "mime": "image/png",
                "filename": "result.png",
                "url": "data:image/png;base64,opaque",
            }
        ]
        with_attachment = stored("msg_tool", "assistant", completed, created=1)
        transcript = project_transcript([with_attachment], None, latest_window=True)
        self.assertIn("result.png", transcript.turns[0].text)
        without_attachment = tool_part("completed")
        self.assertNotEqual(
            semantic_checkpoint([with_attachment], None),
            semantic_checkpoint(
                [stored("msg_tool", "assistant", without_attachment, created=1)], None
            ),
        )

    def test_compacted_and_interrupted_tool_results_follow_native_replay(self) -> None:
        compacted = tool_part("completed")
        compacted["state"]["time"]["compacted"] = 3
        compacted["state"]["attachments"] = [
            {
                "id": "prt_attachment",
                "messageID": "msg_tool",
                "sessionID": "ses_test",
                "type": "file",
                "mime": "image/png",
                "filename": "cleared-result.png",
                "url": "data:image/png;base64,opaque",
            }
        ]
        compacted_message = stored("msg_tool", "assistant", compacted, created=1)
        compacted_turn = project_transcript([compacted_message], None, latest_window=True).turns[0]
        compacted_call = compacted_turn.calls[0]
        self.assertEqual(compacted_call.result, "[Old tool result content cleared]")
        self.assertNotIn("cleared-result.png", compacted_turn.text)
        ordinary = stored("msg_tool", "assistant", tool_part("completed"), created=1)
        self.assertNotEqual(
            semantic_checkpoint([compacted_message], None),
            semantic_checkpoint([ordinary], None),
        )

        interrupted = tool_part("error")
        interrupted["state"]["metadata"] = {
            "interrupted": True,
            "output": "partial useful output",
        }
        interrupted_message = stored("msg_tool", "assistant", interrupted, created=1)
        interrupted_call = (
            project_transcript([interrupted_message], None, latest_window=True).turns[0].calls[0]
        )
        self.assertEqual(interrupted_call.result, "partial useful output")
        ordinary_error = stored("msg_tool", "assistant", tool_part("error"), created=1)
        self.assertNotEqual(
            semantic_checkpoint([interrupted_message], None),
            semantic_checkpoint([ordinary_error], None),
        )

    def test_digest_closure_covers_every_projected_part_field(self) -> None:
        cases = (
            ("text", text("one"), text("two")),
            (
                "text ignored",
                {"type": "text", "text": "one", "ignored": False},
                {"type": "text", "text": "one", "ignored": True},
            ),
            (
                "file",
                {"type": "file", "mime": "text/plain", "filename": "a", "url": "file:///a"},
                {"type": "file", "mime": "text/plain", "filename": "b", "url": "file:///a"},
            ),
            (
                "agent",
                {"type": "agent", "name": "build", "source": {"value": "@build"}},
                {"type": "agent", "name": "explore", "source": {"value": "@build"}},
            ),
            (
                "subtask",
                {
                    "type": "subtask",
                    "prompt": "inspect",
                    "description": "task",
                    "agent": "build",
                    "model": {"providerID": "one", "modelID": "model"},
                    "command": "run",
                },
                {
                    "type": "subtask",
                    "prompt": "inspect changed",
                    "description": "task",
                    "agent": "build",
                    "model": {"providerID": "one", "modelID": "model"},
                    "command": "run",
                },
            ),
            (
                "compaction",
                {"type": "compaction", "auto": True, "overflow": False},
                {"type": "compaction", "auto": True, "overflow": True},
            ),
        )
        for name, before, after in cases:
            with self.subTest(name=name):
                left = stored("msg_part", "user", before, created=1)
                right = stored("msg_part", "user", after, created=1)
                self.assertNotEqual(
                    semantic_checkpoint([left], None), semantic_checkpoint([right], None)
                )
        left_id = stored("msg_part", "user", text("one", part_id="prt_one"), created=1)
        right_id = stored("msg_part", "user", text("one", part_id="prt_two"), created=1)
        self.assertNotEqual(
            semantic_checkpoint([left_id], None), semantic_checkpoint([right_id], None)
        )

    def test_digest_closure_covers_attachment_task_agent_and_compaction_details(self) -> None:
        file_base = {
            "type": "file",
            "mime": "text/plain",
            "filename": "a.txt",
            "url": "file:///a.txt",
            "source": {
                "type": "file",
                "path": "/a.txt",
                "text": {"value": "@a", "start": 0, "end": 2},
            },
        }
        subtask_base = {
            "type": "subtask",
            "prompt": "inspect",
            "description": "task",
            "agent": "build",
            "model": {"providerID": "one", "modelID": "model"},
            "command": "run",
        }
        agent_base = {"type": "agent", "name": "build", "source": {"value": "@build"}}
        compaction_base = {
            "type": "compaction",
            "auto": True,
            "overflow": False,
            "tail_start_id": "msg_tail",
        }

        def mutation(source: dict[str, object], *path_and_value: object) -> dict[str, object]:
            result = json.loads(json.dumps(source))
            *path, value = path_and_value
            target = result
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return result

        pairs = (
            (file_base, mutation(file_base, "mime", "image/png")),
            (file_base, mutation(file_base, "url", "file:///b.txt")),
            (file_base, mutation(file_base, "source", "path", "/b.txt")),
            (subtask_base, mutation(subtask_base, "description", "other task")),
            (subtask_base, mutation(subtask_base, "agent", "explore")),
            (subtask_base, mutation(subtask_base, "model", "modelID", "other")),
            (subtask_base, mutation(subtask_base, "command", "other command")),
            (agent_base, mutation(agent_base, "source", "value", "@other")),
            (compaction_base, mutation(compaction_base, "auto", False)),
            (compaction_base, mutation(compaction_base, "tail_start_id", "msg_other")),
        )
        for before, after in pairs:
            self.assertNotEqual(
                semantic_checkpoint([stored("msg_part", "user", before, created=1)], None),
                semantic_checkpoint([stored("msg_part", "user", after, created=1)], None),
            )

    def test_revert_digest_covers_only_semantic_message_and_part_boundaries(self) -> None:
        message = stored("msg_one", "user", text("one"), created=1)
        baseline = semantic_checkpoint(
            [message],
            {"messageID": "msg_one", "partID": "prt_one", "snapshot": "one", "diff": "a"},
        )
        self.assertEqual(
            baseline,
            semantic_checkpoint(
                [message],
                {
                    "messageID": "msg_one",
                    "partID": "prt_one",
                    "snapshot": "two",
                    "diff": "b",
                },
            ),
        )
        self.assertNotEqual(
            baseline,
            semantic_checkpoint([message], {"messageID": "msg_other", "partID": "prt_one"}),
        )
        self.assertNotEqual(
            baseline,
            semantic_checkpoint([message], {"messageID": "msg_one", "partID": "prt_other"}),
        )

    def test_digest_closure_covers_message_and_assistant_filter_fields(self) -> None:
        base = stored("msg_one", "user", text("one"), created=1)
        variants = (
            stored("msg_two", "user", text("one"), created=1),
            stored("msg_one", "user", text("one"), created=2),
            stored("msg_one", "assistant", text("one"), created=1, parent_id="msg_parent"),
        )
        baseline = semantic_checkpoint([base], None)
        for variant in variants:
            self.assertNotEqual(semantic_checkpoint([variant], None), baseline)
        assistant = stored(
            "msg_assistant", "assistant", text("answer"), created=2, parent_id="msg_user"
        )
        assistant_variants = (
            stored("msg_assistant", "assistant", text("answer"), created=2, parent_id="msg_other"),
            stored(
                "msg_assistant",
                "assistant",
                text("answer"),
                created=2,
                parent_id="msg_user",
                summary=True,
            ),
            stored(
                "msg_assistant",
                "assistant",
                text("answer"),
                created=2,
                parent_id="msg_user",
                finish="stop",
            ),
            stored(
                "msg_assistant",
                "assistant",
                text("answer"),
                created=2,
                parent_id="msg_user",
                error={"name": "failure"},
            ),
        )
        assistant_checkpoint = semantic_checkpoint([assistant], None)
        for variant in assistant_variants:
            self.assertNotEqual(semantic_checkpoint([variant], None), assistant_checkpoint)

    def test_digest_excludes_all_audited_bookkeeping_and_display_metadata(self) -> None:
        base = stored("msg_one", "assistant", text("answer"), created=1)
        base_info = {**base.info, "agent": "build", "modelID": "one", "cost": 1}
        changed_info = {**base.info, "agent": "explore", "modelID": "two", "cost": 999}
        self.assertEqual(
            semantic_checkpoint(
                [StoredMessage(base.message_id, base.time_created, base_info, base.parts)], None
            ),
            semantic_checkpoint(
                [StoredMessage(base.message_id, base.time_created, changed_info, base.parts)], None
            ),
        )
        baseline = semantic_checkpoint([base], None)
        for kind in BOOKKEEPING_PART_KINDS:
            with self.subTest(kind=kind):
                noisy = stored(
                    "msg_one",
                    "assistant",
                    text("answer"),
                    {"type": kind, "payload": "one"},
                    created=1,
                )
                changed = stored(
                    "msg_one",
                    "assistant",
                    text("answer"),
                    {"type": kind, "payload": "two"},
                    created=1,
                )
                self.assertEqual(semantic_checkpoint([noisy], None), baseline)
                self.assertEqual(semantic_checkpoint([changed], None), baseline)

    def test_digest_covers_tool_identity_input_terminal_content_and_status(self) -> None:
        completed = tool_part("completed")
        baseline = semantic_checkpoint(
            [stored("msg_tool", "assistant", completed, created=1)], None
        )
        mutations = []
        for key, value in (("callID", "other_call"), ("tool", "other_tool")):
            changed = json.loads(json.dumps(completed))
            changed[key] = value
            mutations.append(changed)
        changed_input = json.loads(json.dumps(completed))
        changed_input["state"]["input"] = {"command": "other"}
        mutations.append(changed_input)
        changed_output = json.loads(json.dumps(completed))
        changed_output["state"]["output"] = "other output"
        mutations.append(changed_output)
        error = tool_part("error")
        changed_error = json.loads(json.dumps(error))
        changed_error["state"]["error"] = "different error"
        self.assertNotEqual(
            semantic_checkpoint([stored("msg_tool", "assistant", error, created=1)], None),
            semantic_checkpoint([stored("msg_tool", "assistant", changed_error, created=1)], None),
        )
        mutations.append(tool_part("pending"))
        mutations.append(tool_part("running"))
        mutations.append(tool_part("error"))
        for mutation in mutations:
            self.assertNotEqual(
                semantic_checkpoint([stored("msg_tool", "assistant", mutation, created=1)], None),
                baseline,
            )


class OpenCodeReaderIntegrationTests(unittest.TestCase):
    SESSION_ID = "ses_000000000001AAAAAAAAAAAAAA"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "opencode.db"
        self.connection = sqlite3.connect(self.database)
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT INTO session "
            "(id,parent_id,directory,title,agent,time_created,time_updated,time_archived,revert) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.SESSION_ID, None, "/work", "title", "build", 1, 2, None, None),
        )
        self.connection.commit()
        self.ref = NativeRef(self.SESSION_ID, str(self.database))
        diagnostics.clear_warnings()

    def tearDown(self) -> None:
        self.connection.close()
        opencode._cached_opencode_change_status.cache_clear()
        diagnostics.clear_warnings()
        self.temporary.cleanup()

    def add_message(
        self,
        message_id: str,
        role: str,
        *parts: dict[str, object],
        created: int,
        session_id: str | None = None,
        **info: object,
    ) -> None:
        owner = session_id or self.SESSION_ID
        payload: dict[str, object] = {"role": role, **info}
        if role == "assistant" and "parentID" not in payload:
            payload["parentID"] = "msg_parent"
        self.connection.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (message_id, owner, created, json.dumps(payload, ensure_ascii=False)),
        )
        for index, source in enumerate(parts):
            part = dict(source)
            part_id = str(part.pop("_id", f"prt_{message_id}_{index}"))
            self.connection.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (part_id, message_id, owner, created + index, json.dumps(part, ensure_ascii=False)),
            )

    def test_reader_orders_equal_time_messages_and_parts_by_native_id(self) -> None:
        self.add_message("msg_b", "user", text("second", part_id="prt_b"), created=1)
        self.add_message(
            "msg_a",
            "user",
            text("later part", part_id="prt_b2"),
            text("earlier part", part_id="prt_a2"),
            created=1,
        )
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in snapshot.transcript.turns],
            ["earlier part\n\nlater part", "second"],
        )
        self.assertRegex(snapshot.checkpoint, r"^sqlite-semantic-v1:[0-9a-f]{64}$")
        public = read_snapshot("opencode", self.ref)
        self.assertEqual(public, snapshot)

    def test_reader_uses_captured_time_then_id_message_order(self) -> None:
        self.add_message("msg_z", "user", text("first by time"), created=1)
        self.add_message("msg_a", "user", text("second by time"), created=2)
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in snapshot.transcript.turns],
            ["first by time", "second by time"],
        )

    def test_latest_and_full_reads_share_the_complete_semantic_checkpoint(self) -> None:
        self.add_message("msg_old", "user", text("old"), created=1)
        self.add_message("msg_compact", "user", {"type": "compaction", "auto": True}, created=2)
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.connection.commit()
        latest = opencode._read_opencode_snapshot(self.ref, latest_window=True)
        full = opencode._read_opencode_snapshot(self.ref, latest_window=False)
        self.assertEqual(latest.checkpoint, full.checkpoint)
        self.assertEqual(
            [turn.text for turn in latest.transcript.turns], [COMPACTION_PROMPT, "summary"]
        )
        self.assertEqual(
            [turn.text for turn in full.transcript.turns],
            ["old", COMPACTION_PROMPT, "summary"],
        )

    def test_revert_and_unrevert_change_view_and_checkpoint_without_row_cleanup(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.add_message("msg_u2", "user", text("two"), created=2)
        self.connection.commit()
        original = opencode._read_opencode_snapshot(self.ref)
        self.connection.execute(
            "UPDATE session SET revert=? WHERE id=?",
            (json.dumps({"messageID": "msg_u2"}), self.SESSION_ID),
        )
        self.connection.commit()
        reverted = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual([turn.text for turn in reverted.transcript.turns], ["one"])
        self.assertNotEqual(reverted.checkpoint, original.checkpoint)
        self.connection.execute("UPDATE session SET revert=NULL WHERE id=?", (self.SESSION_ID,))
        self.connection.commit()
        restored = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(restored, original)

    def test_equal_time_revert_sql_matches_native_message_and_part_boundaries(self) -> None:
        self.add_message("msg_a", "user", text("before"), created=1)
        self.add_message(
            "msg_b",
            "user",
            text("first", part_id="prt_a"),
            text("second", part_id="prt_b"),
            created=1,
        )
        self.add_message("msg_c", "user", text("after"), created=1)
        self.connection.commit()

        chronological = [
            stored("msg_a", "user", text("before"), created=1),
            stored(
                "msg_b",
                "user",
                text("first", part_id="prt_a"),
                text("second", part_id="prt_b"),
                created=1,
            ),
            stored("msg_c", "user", text("after"), created=1),
        ]
        boundaries = (
            {"messageID": "msg_b"},
            {"messageID": "msg_b", "partID": "prt_b"},
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                self.connection.execute(
                    "UPDATE session SET revert=? WHERE id=?",
                    (json.dumps(boundary), self.SESSION_ID),
                )
                self.connection.commit()
                actual = opencode._read_opencode_snapshot(self.ref, latest_window=False).transcript
                expected = project_transcript(chronological, boundary, latest_window=False)
                self.assertEqual(actual, expected)

    def test_revert_before_and_after_compaction_changes_the_native_view(self) -> None:
        self.add_message("msg_old", "user", text("old"), created=1)
        self.add_message("msg_compact", "user", {"type": "compaction", "auto": True}, created=2)
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.add_message("msg_after", "user", text("after"), created=4)
        self.connection.commit()
        original = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in original.transcript.turns],
            [COMPACTION_PROMPT, "summary", "after"],
        )

        self.connection.execute(
            "UPDATE session SET revert=? WHERE id=?",
            (json.dumps({"messageID": "msg_after"}), self.SESSION_ID),
        )
        self.connection.commit()
        after_compaction = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in after_compaction.transcript.turns],
            [COMPACTION_PROMPT, "summary"],
        )
        self.assertNotEqual(after_compaction.checkpoint, original.checkpoint)

        self.connection.execute(
            "UPDATE session SET revert=? WHERE id=?",
            (json.dumps({"messageID": "msg_compact"}), self.SESSION_ID),
        )
        self.connection.commit()
        before_compaction = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual([turn.text for turn in before_compaction.transcript.turns], ["old"])
        self.assertNotEqual(before_compaction.checkpoint, after_compaction.checkpoint)

    def test_checkpoint_ignores_title_unrelated_session_and_bookkeeping_payloads(self) -> None:
        self.add_message("msg_u1", "user", text("semantic"), created=1)
        self.add_message(
            "msg_a1",
            "assistant",
            {"type": "reasoning", "text": "first"},
            {"type": "snapshot", "snapshot": "one"},
            created=2,
            parentID="msg_u1",
        )
        other = "ses_000000000002BBBBBBBBBBBBBB"
        self.connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
            (other, None, "/other", "other", "build", 1, 2, None, None),
        )
        self.add_message("msg_other", "user", text("other"), created=1, session_id=other)
        self.connection.commit()
        baseline = opencode._opencode_checkpoint(self.ref)
        self.connection.execute("UPDATE session SET title='renamed' WHERE id=?", (self.SESSION_ID,))
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_a1_0'",
            (json.dumps({"type": "reasoning", "text": "changed"}),),
        )
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_a1_1'",
            (json.dumps({"type": "snapshot", "snapshot": "changed"}),),
        )
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_other_0'",
            (json.dumps({"type": "text", "text": "other changed"}),),
        )
        self.connection.commit()
        self.assertEqual(opencode._opencode_checkpoint(self.ref), baseline)
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
            (json.dumps({"type": "text", "text": "semantic changed"}),),
        )
        self.connection.commit()
        self.assertNotEqual(opencode._opencode_checkpoint(self.ref), baseline)

    def test_tool_state_transitions_each_change_checkpoint(self) -> None:
        self.add_message("msg_tool", "assistant", tool_part("pending"), created=1)
        self.connection.commit()
        checkpoints = [opencode._opencode_checkpoint(self.ref)]
        for status in ("running", "completed", "error"):
            payload = tool_part(status)
            payload.pop("_id")
            self.connection.execute(
                "UPDATE part SET data=? WHERE id='prt_tool'", (json.dumps(payload),)
            )
            self.connection.commit()
            checkpoints.append(opencode._opencode_checkpoint(self.ref))
        self.assertEqual(len(set(checkpoints)), 4)

    def test_change_status_parses_scheme_and_classifies_semantic_edits(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unchanged")
        self.assertEqual(
            opencode._opencode_change_status(self.ref, "sqlite-semantic-v0:abc"), "unknown"
        )
        self.assertEqual(opencode._opencode_change_status(self.ref, 0), "unknown")
        self.assertTrue(diagnostics.warnings())
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
            (json.dumps({"type": "text", "text": "two"}),),
        )
        self.connection.commit()
        self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "changed")
        self.assertEqual(conversation_change_status("opencode", self.ref, checkpoint), "changed")

    def test_change_status_cache_reuses_stable_stamp_and_invalidates_on_edit(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        opencode._cached_opencode_change_status.cache_clear()
        capture = opencode._opencode_checkpoint
        with patch.object(opencode, "_opencode_checkpoint", wraps=capture) as wrapped:
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unchanged")
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unchanged")
            self.assertEqual(wrapped.call_count, 1)
            self.connection.execute(
                "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
                (json.dumps({"type": "text", "text": "two"}),),
            )
            self.connection.commit()
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "changed")
            self.assertEqual(wrapped.call_count, 2)

    def test_change_status_falls_back_to_uncached_comparison_without_store_stamp(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        capture = opencode._opencode_checkpoint
        with (
            patch.object(opencode, "_store_stamp", return_value=None),
            patch.object(opencode, "_opencode_checkpoint", wraps=capture) as wrapped,
        ):
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unchanged")
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unchanged")
            self.assertEqual(wrapped.call_count, 2)
            self.connection.execute(
                "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
                (json.dumps({"type": "text", "text": "two"}),),
            )
            self.connection.commit()
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "changed")
            self.assertEqual(wrapped.call_count, 3)

    def test_racing_status_snapshot_is_unstable_and_never_cached(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        opencode._cached_opencode_change_status.cache_clear()
        capture = opencode._opencode_checkpoint

        def capture_then_edit(ref: NativeRef) -> str:
            current = capture(ref)
            self.connection.execute(
                "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
                (json.dumps({"type": "text", "text": "two"}),),
            )
            self.connection.commit()
            return current

        with patch.object(opencode, "_opencode_checkpoint", side_effect=capture_then_edit):
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unstable")
        with patch.object(opencode, "_opencode_checkpoint", wraps=capture) as wrapped:
            self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "changed")
            self.assertEqual(wrapped.call_count, 1)

    def test_append_and_schema_replacement_are_changed_then_unstable(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        self.add_message("msg_u2", "user", text("two"), created=2)
        self.connection.commit()
        self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "changed")
        current = opencode._opencode_checkpoint(self.ref)
        self.connection.execute("DROP TABLE part")
        self.connection.commit()
        self.assertEqual(opencode._opencode_change_status(self.ref, current), "unstable")

    def test_row_deletion_and_wal_visible_edit_change_checkpoint(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        baseline = opencode._opencode_checkpoint(self.ref)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
            (json.dumps({"type": "text", "text": "wal edit"}),),
        )
        self.connection.commit()
        edited = opencode._opencode_checkpoint(self.ref)
        self.assertNotEqual(edited, baseline)
        self.connection.execute("DELETE FROM part WHERE id='prt_msg_u1_0'")
        self.connection.commit()
        self.assertNotEqual(opencode._opencode_checkpoint(self.ref), edited)

    def test_edit_hidden_before_latest_compaction_changes_complete_checkpoint_only(self) -> None:
        self.add_message("msg_old", "user", text("old"), created=1)
        self.add_message("msg_compact", "user", {"type": "compaction", "auto": True}, created=2)
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.connection.commit()
        before = opencode._read_opencode_snapshot(self.ref)
        self.connection.execute(
            "UPDATE part SET data=? WHERE id='prt_msg_old_0'",
            (json.dumps({"type": "text", "text": "hidden edit"}),),
        )
        self.connection.commit()
        after = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(after.transcript, before.transcript)
        self.assertNotEqual(after.checkpoint, before.checkpoint)

    def test_snapshot_is_pinned_across_commit_between_digest_and_projection(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.add_message("msg_u1", "user", text("before"), created=1)
        self.connection.commit()
        before = opencode._opencode_checkpoint(self.ref)
        select = opencode.select_compacted_newest

        def commit_then_select(messages):
            writer = sqlite3.connect(self.database)
            try:
                writer.execute(
                    "UPDATE part SET data=? WHERE id='prt_msg_u1_0'",
                    (json.dumps({"type": "text", "text": "after"}),),
                )
                writer.commit()
            finally:
                writer.close()
            return select(messages)

        with patch.object(opencode, "select_compacted_newest", side_effect=commit_then_select):
            captured = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(captured.checkpoint, before)
        self.assertEqual([turn.text for turn in captured.transcript.turns], ["before"])
        self.assertNotEqual(opencode._opencode_checkpoint(self.ref), captured.checkpoint)

    def test_locked_semantic_store_degrades_status_to_unstable_within_bound(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        self.connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with patch.object(opencode, "SEMANTIC_BUSY_TIMEOUT_SECONDS", 0.05):
                self.assertEqual(opencode._opencode_change_status(self.ref, checkpoint), "unstable")
        finally:
            self.connection.rollback()
        self.assertLess(time.monotonic() - started, 1)

    def test_empty_revert_is_tolerated_as_no_staged_boundary(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.execute("UPDATE session SET revert='' WHERE id=?", (self.SESSION_ID,))
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual([turn.text for turn in snapshot.transcript.turns], ["one"])

    def test_empty_compaction_tail_is_checkpointable_and_readable(self) -> None:
        self.add_message(
            "msg_compact",
            "user",
            {"type": "compaction", "auto": True, "tail_start_id": ""},
            created=1,
        )
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=2,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.connection.commit()
        checkpoint = opencode._opencode_checkpoint(self.ref)
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(snapshot.checkpoint, checkpoint)
        self.assertEqual(
            [turn.text for turn in snapshot.transcript.turns],
            [COMPACTION_PROMPT, "summary"],
        )

    def test_dangling_tail_warns_and_preserves_native_full_history_fallback(self) -> None:
        self.add_message("msg_old", "user", text("old"), created=1)
        self.add_message(
            "msg_compact",
            "user",
            {"type": "compaction", "auto": True, "tail_start_id": "msg_missing"},
            created=2,
        )
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in snapshot.transcript.turns],
            ["old", COMPACTION_PROMPT, "summary"],
        )
        self.assertTrue(
            any("retained-tail boundary is missing" in note for note in diagnostics.warnings())
        )

    def test_forward_tail_warns_and_preserves_native_full_history_fallback(self) -> None:
        self.add_message("msg_old", "user", text("old"), created=1)
        self.add_message(
            "msg_compact",
            "user",
            {"type": "compaction", "auto": True, "tail_start_id": "msg_after"},
            created=2,
        )
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.add_message("msg_after", "user", text("after"), created=4)
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        self.assertEqual(
            [turn.text for turn in snapshot.transcript.turns],
            ["old", COMPACTION_PROMPT, "summary", "after"],
        )
        self.assertTrue(
            any("retained-tail boundary is missing" in note for note in diagnostics.warnings())
        )

    def test_overlapping_compaction_tail_survives_generic_window_cut(self) -> None:
        self.add_message("msg_u0", "user", text("oldest retained"), created=1)
        self.add_message("msg_c0", "user", {"type": "compaction", "auto": True}, created=2)
        self.add_message(
            "msg_s0",
            "assistant",
            text("stale summary"),
            created=3,
            parentID="msg_c0",
            summary=True,
            finish="stop",
        )
        self.add_message("msg_mid", "user", text("mid"), created=4)
        self.add_message(
            "msg_c1",
            "user",
            {"type": "compaction", "auto": True, "tail_start_id": "msg_u0"},
            created=5,
        )
        self.add_message(
            "msg_s1",
            "assistant",
            text("governing summary"),
            created=6,
            parentID="msg_c1",
            summary=True,
            finish="stop",
        )
        self.add_message("msg_new", "user", text("newest"), created=7)
        self.connection.commit()
        snapshot = opencode._read_opencode_snapshot(self.ref)
        bridged, count = from_last_compaction(snapshot.transcript.turns)
        self.assertEqual(count, 1)
        self.assertEqual(
            [turn.text for turn in bridged],
            [
                "governing summary",
                "oldest retained",
                COMPACTION_PROMPT,
                "stale summary",
                "mid",
                "newest",
            ],
        )

    def test_sqlite_latest_window_with_no_governing_compaction_marks_none(self) -> None:
        self.add_message("msg_old", "user", text("important early context"), created=1)
        self.add_message(
            "msg_reply",
            "assistant",
            text("early reply"),
            created=2,
            parentID="msg_old",
        )
        self.add_message(
            "msg_summary",
            "assistant",
            text("stale summary"),
            created=3,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.add_message("msg_compact", "user", {"type": "compaction", "auto": True}, created=4)
        self.add_message("msg_new", "user", text("newest"), created=5)
        self.connection.commit()
        transcript = opencode._read_opencode_snapshot(self.ref).transcript
        self.assertEqual(transcript.carried_windows, 0)
        self.assertFalse(any(turn.compaction for turn in transcript.turns))
        bridged, count = from_last_compaction(transcript.turns)
        self.assertEqual(count, 0)
        self.assertEqual(bridged, transcript.turns)

    def test_latest_reader_peak_excludes_obsolete_and_unrelated_payloads(self) -> None:
        payload = "x" * 50_000
        for index in range(40):
            self.add_message(f"msg_old_{index:03}", "user", text(payload), created=index + 1)
        self.add_message("msg_compact", "user", {"type": "compaction", "auto": True}, created=100)
        self.add_message(
            "msg_summary",
            "assistant",
            text("summary"),
            created=101,
            parentID="msg_compact",
            summary=True,
            finish="stop",
        )
        self.connection.commit()

        def measured(*, latest_window: bool) -> tuple[object, int]:
            gc.collect()
            tracemalloc.start()
            try:
                result = opencode._read_opencode_snapshot(self.ref, latest_window=latest_window)
                _, peak = tracemalloc.get_traced_memory()
                return result, peak
            finally:
                tracemalloc.stop()

        latest, latest_peak = measured(latest_window=True)
        self.assertEqual(
            [turn.text for turn in latest.transcript.turns], [COMPACTION_PROMPT, "summary"]
        )

        other = "ses_000000000002BBBBBBBBBBBBBB"
        self.connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
            (other, None, "/other", "other", "build", 1, 2, None, None),
        )
        for index in range(40):
            self.add_message(
                f"msg_other_{index:03}", "user", text(payload), created=index + 1, session_id=other
            )
        self.connection.commit()
        del payload
        _, unrelated_peak = measured(latest_window=True)
        full, full_peak = measured(latest_window=False)
        self.assertEqual(len(full.transcript.turns), 42)
        self.assertLessEqual(unrelated_peak, latest_peak + 1_000_000)
        self.assertGreater(full_peak, unrelated_peak * 2)

    def test_malformed_semantic_json_unknown_kind_and_missing_revert_schema_fail(self) -> None:
        self.add_message("msg_bad", "user", {"type": "future"}, created=1)
        self.connection.commit()
        diagnostics.clear_warnings()
        with self.assertRaisesRegex(BridgeError, "unknown value 'future'"):
            opencode._read_opencode_snapshot(self.ref)
        self.assertTrue(
            any("unknown semantic part kind" in warning for warning in diagnostics.warnings())
        )
        self.connection.execute("UPDATE part SET data='not-json' WHERE id='prt_msg_bad_0'")
        self.connection.commit()
        with self.assertRaisesRegex(BridgeError, "malformed JSON"):
            opencode._read_opencode_snapshot(self.ref)

        incompatible = self.root / "without-revert.db"
        other = sqlite3.connect(incompatible)
        try:
            other.executescript(SCHEMA.replace(",\n    revert TEXT", ""))
            other.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                (self.SESSION_ID, None, "/work", "title", "build", 1, 2, None),
            )
            other.commit()
        finally:
            other.close()
        with self.assertRaisesRegex(BridgeError, "session lacks revert"):
            opencode._read_opencode_snapshot(NativeRef(self.SESSION_ID, str(incompatible)))

    def test_orphan_part_fails_closed_before_semantic_projection(self) -> None:
        self.connection.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (
                "prt_orphan",
                "msg_missing",
                self.SESSION_ID,
                1,
                json.dumps({"type": "text", "text": "orphan"}),
            ),
        )
        self.connection.commit()
        with self.assertRaisesRegex(BridgeError, "refers to a missing message"):
            opencode._opencode_checkpoint(self.ref)
        with self.assertRaisesRegex(BridgeError, "refers to a missing message"):
            opencode._read_opencode_snapshot(self.ref)

    def test_cross_session_part_fails_closed_before_semantic_projection(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (
                "prt_foreign",
                "msg_u1",
                "ses_000000000002BBBBBBBBBBBBBB",
                2,
                json.dumps({"type": "text", "text": "foreign"}),
            ),
        )
        self.connection.commit()
        failure = "belongs to a different session than its message"
        with self.assertRaisesRegex(BridgeError, failure):
            opencode._opencode_checkpoint(self.ref)
        with self.assertRaisesRegex(BridgeError, failure):
            opencode._read_opencode_snapshot(self.ref)

    def test_empty_revert_part_id_fails_checkpoint_and_read_consistently(self) -> None:
        self.add_message("msg_u1", "user", text("one"), created=1)
        self.connection.execute(
            "UPDATE session SET revert=? WHERE id=?",
            (json.dumps({"messageID": "msg_u1", "partID": ""}), self.SESSION_ID),
        )
        self.connection.commit()
        with self.assertRaisesRegex(BridgeError, "partID must be a non-empty string"):
            opencode._opencode_checkpoint(self.ref)
        with self.assertRaisesRegex(BridgeError, "partID must be a non-empty string"):
            opencode._read_opencode_snapshot(self.ref)


if __name__ == "__main__":
    unittest.main()
