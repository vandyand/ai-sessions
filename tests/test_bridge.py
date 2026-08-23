import inspect
import io
import json
import tempfile
import time
import tracemalloc
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_sessions import bridge, conversion
from ai_sessions.app import (
    Session,
    UserState,
    available_launch_tools,
    can_bridge,
    command_for,
    launch,
    list_output,
    load_claude_sessions,
    prepare_launch,
)
from ai_sessions.bridge import (
    CODEX_BUDGET_CONTEXT_TOKENS,
    CODEX_CONTEXT_WINDOW,
    HANDOFF_NOTE_RESERVE_CHARS,
    MIN_BRIDGE_CHARS,
    TRUNCATION_MARKER,
    BridgeError,
    Budget,
    BudgetPolicy,
    SelectionMetric,
    SelectionResult,
    ToolCall,
    Transcript,
    Turn,
    bridged_title,
    fit,
    flatten,
    from_last_compaction,
    handoff_note,
    merge_runs,
    prepare,
    read_turns,
    render_calls,
    resolve_budget,
    select_messages,
    selection_cost,
    write_claude_session,
    write_codex_session,
)
from ai_sessions.capabilities import HarnessAdapter
from ai_sessions.config import LaunchConfig
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.harnesses import codex as codex_harness
from ai_sessions.registry import REGISTRY


def codex_line(role: str, text: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-13T21:56:42.542Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_1",
                "role": role,
                "content": [
                    {"type": "input_text" if role == "user" else "output_text", "text": text}
                ],
            },
        }
    )


def claude_line(role: str, content: object, **extra: object) -> str:
    record = {
        "type": role,
        "uuid": "u-1",
        "timestamp": "2026-08-19T14:02:50.555Z",
        "message": {"role": role, "content": content},
    }
    record.update(extra)
    return json.dumps(record)


class ReadTurnsTests(unittest.TestCase):
    def test_codex_transcript_keeps_conversation_and_drops_scaffolding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp"}}),
                        codex_line(
                            "user", "<environment_context>noise</environment_context>Ship it"
                        ),
                        json.dumps(
                            {"type": "response_item", "payload": {"type": "reasoning", "id": "r"}}
                        ),
                        codex_line("assistant", "Shipped."),
                        codex_line("user", "<environment_context>only noise</environment_context>"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_turns("codex", path),
                [Turn("user", "Ship it"), Turn("assistant", "Shipped.")],
            )

    def claude_with_a_tool_call(self, directory: str) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text(
            "\n".join(
                [
                    claude_line("user", "Find the bug"),
                    claude_line(
                        "assistant",
                        [
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "text", "text": "Looking now."},
                            {
                                "type": "tool_use",
                                "id": "t",
                                "name": "Bash",
                                "input": {"command": "pytest -q", "description": "run tests"},
                            },
                        ],
                    ),
                    claude_line(
                        "user",
                        [{"type": "tool_result", "tool_use_id": "t", "content": "1 failed"}],
                    ),
                    claude_line("user", "<system-reminder>ignore me</system-reminder>"),
                    claude_line("assistant", [{"type": "text", "text": "Found it."}]),
                    json.dumps({"type": "ai-title", "title": "x"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_claude_transcript_keeps_tool_calls_and_drops_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            turns = read_turns("claude", self.claude_with_a_tool_call(directory))
            self.assertEqual([turn.role for turn in turns], ["user", "assistant", "assistant"])
            self.assertEqual(turns[0].text, "Find the bug")
            self.assertEqual(turns[1].text, "Looking now.")
            self.assertNotIn("hidden", turns[1].text)
            self.assertEqual(
                turns[1].calls, (ToolCall(name="Bash", request="pytest -q …", result="1 failed"),)
            )
            self.assertEqual(turns[2].text, "Found it.")

    def test_tool_calls_can_be_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            turns = read_turns("claude", self.claude_with_a_tool_call(directory), tool_calls=False)
            self.assertTrue(all(turn.calls == () for turn in turns))

    def test_prepare_renders_calls_into_the_turn_that_made_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            turns = prepare(read_turns("claude", self.claude_with_a_tool_call(directory)))
            self.assertEqual([turn.role for turn in turns], ["user", "assistant"])
            self.assertEqual(
                turns[1].text,
                "Looking now.\n\n⟦Bash⟧ pytest -q …\n   → 1 failed\n\nFound it.",
            )

    def test_codex_tool_calls_are_paired_with_their_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        codex_line("assistant", "Checking."),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "name": "shell",
                                    "call_id": "call_1",
                                    "arguments": '{"command": "ls -la"}',
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call_1",
                                    "output": [
                                        {
                                            "type": "input_text",
                                            "text": (
                                                "Script completed\nWall time 0.3 seconds\n"
                                                "Output:\ntotal 4\n"
                                            ),
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            turns = read_turns("codex", path)
            self.assertEqual(len(turns), 1)
            # Codex's fixed result preamble carries no information the rest of
            # the transcript does not already give.
            self.assertEqual(
                turns[0].calls,
                (ToolCall(name="shell", request="ls -la", result="total 4"),),
            )

    def test_a_tool_call_without_a_preceding_message_still_lands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": "c",
                            "input": (
                                'const r = await tools.exec_command({"cmd":"grep -rn foo src",'
                                '"workdir":"/home/andrew"}); text(r.output);'
                            ),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            turns = read_turns("codex", path)
            self.assertEqual(turns[0].role, "assistant")
            self.assertEqual(turns[0].text, "")
            self.assertEqual(turns[0].calls[0].name, "exec")
            # The JavaScript wrapper is scaffolding; the command is the point.
            self.assertEqual(turns[0].calls[0].request, "grep -rn foo src")

    def test_subagent_transcript_falls_back_to_sidechain_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-1.jsonl"
            path.write_text(
                "\n".join(
                    [
                        claude_line("user", "Search the repo", isSidechain=True),
                        claude_line(
                            "assistant", [{"type": "text", "text": "Done."}], isSidechain=True
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_turns("claude", path),
                [Turn("user", "Search the repo"), Turn("assistant", "Done.")],
            )

    def test_missing_transcript_is_reported(self) -> None:
        with self.assertRaises(BridgeError):
            read_turns("codex", "")


class ShapingTests(unittest.TestCase):
    def test_consecutive_same_role_turns_merge(self) -> None:
        self.assertEqual(
            merge_runs([Turn("user", "a"), Turn("user", "b"), Turn("assistant", "c")]),
            [Turn("user", "a\n\nb"), Turn("assistant", "c")],
        )

    def test_fit_keeps_opening_request_and_most_recent_turns(self) -> None:
        turns = [Turn("user", "opening"), *(Turn("assistant", "x" * 100) for _ in range(10))]
        kept, dropped = fit(turns, max_chars=250)
        self.assertEqual(kept[0], turns[0])
        self.assertEqual(kept[-1], turns[-1])
        self.assertEqual(dropped, 10 - (len(kept) - 1))
        self.assertLessEqual(sum(len(turn.text) for turn in kept), 250)

    def test_fit_leaves_a_small_conversation_alone(self) -> None:
        turns = [Turn("user", "hi"), Turn("assistant", "hello")]
        self.assertEqual(fit(turns, max_chars=1000), (turns, 0))

    def test_oversized_single_turn_is_truncated_not_dropped(self) -> None:
        kept, dropped = fit([Turn("user", "x" * 50_000)], max_chars=2000)
        self.assertEqual(dropped, 0)
        self.assertIn("message truncated", kept[0].text)

    def test_target_defaults_and_legacy_override_resolve_distinctly(self) -> None:
        claude = resolve_budget("claude")
        codex = resolve_budget("codex")
        self.assertEqual((claude.tokens, claude.chars), (150_000, 300_000))
        self.assertEqual((codex.tokens, codex.chars), (192_000, 384_000))
        self.assertEqual(resolve_budget("claude", max_chars=950_000).chars, 950_000)
        self.assertTrue(resolve_budget("claude", max_chars=950_000).over_policy)
        self.assertEqual(resolve_budget("claude", migrated=True).origin, "target-default-migrated")

    def test_token_budget_applies_ratio_and_precedence(self) -> None:
        budget = resolve_budget("claude", max_tokens=5_000, max_chars=950_000)
        self.assertEqual(
            (budget.tokens, budget.chars, budget.origin),
            (5_000, 10_000, "config.max_tokens"),
        )

    def test_non_integer_policy_ratio_applies_in_both_directions(self) -> None:
        base = REGISTRY.get("claude")
        ratio_harness = HarnessAdapter(
            name="ratio",
            label="Ratio",
            short_label="Ratio",
            order=999,
            home=Path("ratio"),
            default_command=("ratio",),
            dangerous_args=(),
            source_kinds=base.source_kinds,
            id_patterns=base.id_patterns,
            read=base.read,
            write=base.write,
            locate=base.locate,
            change_status=base.change_status,
            budget=BudgetPolicy(10_000, 1.0, 2.5, "test ratio"),
        )
        with REGISTRY.temporary(ratio_harness):
            from_tokens = resolve_budget("ratio", max_tokens=10_000)
            from_chars = resolve_budget("ratio", max_chars=25_001)
        self.assertEqual((from_tokens.tokens, from_tokens.chars), (10_000, 25_000))
        self.assertEqual((from_chars.tokens, from_chars.chars), (10_001, 25_001))

    def test_too_small_legacy_character_budget_fails(self) -> None:
        with self.assertRaisesRegex(BridgeError, "must be at least"):
            resolve_budget("claude", max_chars=100)

    def test_invalid_resolver_values_are_unset_and_huge_values_are_reported(self) -> None:
        expected = resolve_budget("claude")
        for value in (0, -1, True, "5000"):
            self.assertEqual(resolve_budget("claude", max_tokens=value), expected)  # type: ignore[arg-type]
            self.assertEqual(resolve_budget("claude", max_chars=value), expected)  # type: ignore[arg-type]
        with self.assertRaisesRegex(BridgeError, "max_tokens is too large"):
            resolve_budget("claude", max_tokens=10**400)
        with self.assertRaisesRegex(BridgeError, "max_chars is too large"):
            resolve_budget("claude", max_chars=10**400)

    def test_fit_through_prices_same_role_joins(self) -> None:
        turns = [Turn("user", "x" * 10) for _ in range(10)]
        self.assertEqual(selection_cost(turns), 118)
        selected = select_messages(turns, 117)
        self.assertGreater(selected.dropped, 0)
        self.assertLessEqual(selection_cost(selected.turns), 117)

    def test_overflow_keeps_first_and_contiguous_newest_suffix(self) -> None:
        turns = [
            Turn("user", "a" * 10),
            Turn("user", "b" * 10),
            Turn("user", "c" * 100),
            Turn("user", "d" * 10),
            Turn("user", "e" * 10),
        ]
        selected = select_messages(turns, 60)
        self.assertEqual(selected.turns[0].text, turns[0].text)
        self.assertEqual(selected.turns, [turns[0], turns[3], turns[4]])

    def test_same_role_anchors_fit_at_the_exact_bridge_floor(self) -> None:
        self.assertEqual(MIN_BRIDGE_CHARS, 4_158)
        selected = select_messages(
            [Turn("user", "a" * 100), Turn("user", "b" * 100)],
            MIN_BRIDGE_CHARS - 4_096,
        )
        self.assertEqual(selected.dropped, 0)
        self.assertEqual(selected.truncated, 2)
        self.assertEqual(selection_cost(selected.turns), 62)
        self.assertTrue(all(turn.text.endswith(TRUNCATION_MARKER) for turn in selected.turns))

    def test_survivors_do_not_decrease_at_the_anchor_cap_boundary(self) -> None:
        turns = [
            Turn("user", "a" * 1_000),
            Turn("assistant", "mmm"),
            Turn("user", "b" * 1_000),
        ]
        below = select_messages(turns, 2_001)
        above = select_messages(turns, 2_002)
        self.assertEqual((len(below.turns), below.dropped), (2, 1))
        self.assertGreaterEqual(len(above.turns), len(below.turns))
        self.assertLessEqual(above.dropped, below.dropped)

    def test_tail_pricing_uses_the_truncated_head_cost(self) -> None:
        turns = [
            Turn("user", "a" * 1_000),
            Turn("assistant", "m" * 10),
            Turn("user", "b" * 50),
        ]
        selected = select_messages(turns, 202)
        self.assertEqual(selected.turns[1:], turns[1:])
        self.assertEqual((selected.dropped, selected.truncated), (0, 1))

    def test_bridge_floor_and_note_reserve_bound_maximal_metadata(self) -> None:
        with self.assertRaises(BridgeError):
            resolve_budget("claude", max_chars=MIN_BRIDGE_CHARS - 1)
        for maximum in (MIN_BRIDGE_CHARS, MIN_BRIDGE_CHARS + 1):
            budget = resolve_budget("claude", max_chars=maximum)
            selected = select_messages(
                [Turn("user", "a" * 10_000), Turn("user", "b" * 10_000)],
                budget.chars - HANDOFF_NOTE_RESERVE_CHARS,
            )
            self.assertLessEqual(selection_cost(selected.turns), maximum - 4_096)
        hostile = '界\\"\n\t\x00' * 2_000
        note = handoff_note(
            source_tool="codex",
            target_tool="claude",
            session_id=hostile,
            title=hostile,
            cwd=hostile,
            conversation_id=hostile,
            kept=2,
            assembled=1,
            calls=999_999,
            dropped=999_999,
            truncated=2,
            compacted=999_999,
            opaque_compactions=999_999,
            sealed_summary=True,
            resumes_at_last_summary=False,
            budget=resolve_budget("claude", max_tokens=1),
        )
        self.assertLessEqual(len(note) + 2, HANDOFF_NOTE_RESERVE_CHARS)
        provenance = next(
            line for line in note.splitlines() if line.startswith("[ai-sessions-provenance v1]")
        )
        json.loads(provenance.split("] ", 1)[1])

    def test_selection_shape_is_deterministic_monotone_and_contiguous(self) -> None:
        turns = [
            Turn("user" if index % 3 else "assistant", f"{index:02d}:" + "x" * (20 + index))
            for index in range(30)
        ]

        def matches(candidate: Turn, original: Turn) -> bool:
            return candidate == original or (
                candidate.role == original.role
                and candidate.text.endswith(TRUNCATION_MARKER)
                and original.text.startswith(candidate.text[: -len(TRUNCATION_MARKER)])
            )

        survivor_counts: list[int] = []
        for maximum in range(62, 1_600, 31):
            selected = select_messages(turns, maximum)
            repeated = select_messages(turns, maximum)
            self.assertEqual(selected, repeated)
            self.assertLessEqual(selection_cost(selected.turns), maximum)
            self.assertEqual(selected.dropped, len(turns) - len(selected.turns))
            survivor_counts.append(len(selected.turns))
            if selected.dropped:
                self.assertTrue(matches(selected.turns[0], turns[0]))
                suffix = selected.turns[1:]
                originals = turns[-len(suffix) :] if suffix else []
                self.assertTrue(all(matches(got, want) for got, want in zip(suffix, originals)))
        self.assertEqual(survivor_counts, sorted(survivor_counts))
        self.assertEqual(select_messages([], 100).turns, [])
        single = select_messages([Turn("user", "z" * 1_000)], 100)
        self.assertEqual((single.dropped, single.truncated), (0, 1))

    def test_large_same_role_selection_prices_real_assembly(self) -> None:
        turns = [Turn("user", f"{index:04d}" + "x" * 96) for index in range(2_000)]
        conversation_limit = 190_000
        selected = select_messages(turns, conversation_limit)
        oracle = sum(len(turn.text) for turn in selected.turns) + 2 * (len(selected.turns) - 1)
        self.assertEqual(selection_cost(selected.turns), oracle)
        self.assertGreater(len(selected.turns), 1_500)
        self.assertEqual(selected.turns[1:], turns[-(len(selected.turns) - 1) :])
        budget = Budget(
            "claude",
            100_000,
            conversation_limit + HANDOFF_NOTE_RESERVE_CHARS,
            "test",
        )
        note = handoff_note(
            source_tool="codex",
            target_tool="claude",
            session_id="source",
            title="stress",
            cwd="/tmp",
            kept=len(selected.turns),
            assembled=1,
            calls=0,
            dropped=selected.dropped,
            budget=budget,
        )
        payload = merge_runs([Turn("user", note), *selected.turns])
        self.assertLessEqual(sum(len(turn.text) for turn in payload), budget.chars)
        self.assertGreater(
            2 * (len(selected.turns) - 1),
            HANDOFF_NOTE_RESERVE_CHARS - len(note) - 2,
        )

    def test_million_character_selection_is_linear_and_memory_bounded(self) -> None:
        turns = [Turn("user", f"{index:05d}" + "x" * 95) for index in range(10_000)]
        calls = {"item": 0, "join": 0, "truncate": 0}

        def item_cost(turn: Turn) -> int:
            calls["item"] += 1
            return len(turn.text)

        def join_cost(left: Turn, right: Turn) -> int:
            calls["join"] += 1
            return 2 if left.role == right.role else 0

        def truncate(turn: Turn, limit: int) -> Turn:
            calls["truncate"] += 1
            return Turn(turn.role, turn.text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER)

        metric = SelectionMetric(item_cost, join_cost, truncate)
        maximum = 300_000
        tracemalloc.start()
        started = time.perf_counter()
        selected = select_messages(turns, maximum, metric)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(elapsed, 5.0)
        self.assertLessEqual(sum(calls.values()), 6 * len(turns) + 10)
        self.assertLessEqual(peak, maximum + 16 * len(turns) + 262_144)
        self.assertLessEqual(selection_cost(selected.turns), maximum)

    def test_truncation_does_not_report_cut_tool_calls(self) -> None:
        calls = (
            ToolCall("one", "request", "result"),
            ToolCall("two", "request", "result"),
            ToolCall("three", "request", "result"),
            ToolCall("four", "request", "result" * 100),
        )
        turn = flatten([Turn("assistant", "prefix", calls)])[0]
        rendered_start = turn.text.rfind(render_calls(calls))
        first_three = [render_calls((call,)) for call in calls[:3]]
        third_end = rendered_start + sum(map(len, first_three)) + 2
        prefix = third_end - 2
        selected = select_messages([turn], prefix + len(TRUNCATION_MARKER))
        self.assertEqual(selected.turns[0].calls, calls[:2])
        self.assertIn(render_calls(calls[:2]), selected.turns[0].text)
        self.assertNotIn(render_calls(calls[2:3]), selected.turns[0].text)

    def test_oversized_newest_anchor_is_truncated_and_disclosed(self) -> None:
        selected = select_messages([Turn("user", "short"), Turn("assistant", "x" * 50_000)], 2_000)
        self.assertEqual((selected.dropped, selected.truncated), (0, 1))
        self.assertEqual(len(selected.turns[-1].text), 2_000 - 2 - len("short"))
        self.assertIn("message truncated", selected.turns[-1].text)

    def test_budget_policy_rejects_invalid_registration(self) -> None:
        invalid = (
            (0, 0.75, 2.0, "source"),
            (10_000, 0.0, 2.0, "source"),
            (10_000, 1.1, 2.0, "source"),
            (10_000, 0.75, 0.0, "source"),
            (10_000, 0.75, 2.0, ""),
            (2_000, 1.0, 2.0, "too small"),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                BudgetPolicy(*values)
        with self.assertRaises(ValueError):
            Budget("claude", 1, 1, "target-default")

    def test_selection_metric_guard_clauses_reject_invalid_hooks(self) -> None:
        turn = Turn("user", "x" * 100)
        negative_item = SelectionMetric(lambda _: -1, lambda _a, _b: 0, lambda value, _: value)
        with self.assertRaisesRegex(BridgeError, "negative item"):
            selection_cost([turn], negative_item)
        negative_join = SelectionMetric(lambda _: 1, lambda _a, _b: -1, lambda value, _: value)
        with self.assertRaisesRegex(BridgeError, "negative join"):
            selection_cost([turn, turn], negative_join)
        wrong_role = SelectionMetric(
            lambda value: len(value.text),
            lambda _a, _b: 0,
            lambda value, limit: Turn("assistant", value.text[:limit]),
        )
        with self.assertRaisesRegex(BridgeError, "invalid truncated anchor"):
            select_messages([turn], 50, wrong_role)

    def test_bridged_title_names_the_origin(self) -> None:
        self.assertEqual(
            bridged_title("ai-sessions-cli-util", "codex"), "ai-sessions-cli-util (from Codex)"
        )
        self.assertEqual(bridged_title("", "claude"), "untitled session (from Claude)")


class CompactionTests(unittest.TestCase):
    def compacted_claude(self, directory: str) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text(
            "\n".join(
                [
                    claude_line("user", "original request"),
                    claude_line("assistant", [{"type": "text", "text": "early work"}]),
                    claude_line("user", "summary one", isCompactSummary=True),
                    claude_line("assistant", [{"type": "text", "text": "middle work"}]),
                    claude_line("user", "summary two", isCompactSummary=True),
                    claude_line("assistant", [{"type": "text", "text": "recent work"}]),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_compaction_summaries_are_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            turns = read_turns("claude", self.compacted_claude(directory))
            self.assertEqual(
                [turn.compaction for turn in turns], [False, False, True, False, True, False]
            )

    def test_only_the_newest_window_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            turns = read_turns("claude", self.compacted_claude(directory))
            kept, count = from_last_compaction(turns)
            self.assertEqual(count, 2)
            self.assertEqual([turn.text for turn in kept], ["summary two", "recent work"])

    def test_a_conversation_that_never_compacted_is_untouched(self) -> None:
        turns = [Turn("user", "a"), Turn("assistant", "b")]
        self.assertEqual(from_last_compaction(turns), (turns, 0))

    def test_bridging_starts_at_the_summary_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                result = bridge.bridge(
                    source_tool="claude",
                    target_tool="codex",
                    session_id="c-1",
                    storage=str(self.compacted_claude(directory)),
                    cwd="/home/andrew",
                )
            body = result.path.read_text(encoding="utf-8")
            self.assertIn("ran out of context 2 time(s)", body)
            self.assertIn("summary two", body)
            self.assertNotIn("original request", body)

    def test_the_whole_history_can_still_be_carried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                result = bridge.bridge(
                    source_tool="claude",
                    target_tool="codex",
                    session_id="c-1",
                    storage=str(self.compacted_claude(directory)),
                    cwd="/home/andrew",
                    latest_window=False,
                )
            self.assertIn("original request", result.path.read_text(encoding="utf-8"))

    def test_codex_compactions_are_counted_but_never_truncate(self) -> None:
        # Codex stores its summaries encrypted, so there is nothing to resume
        # from and the pre-compaction history has to carry the conversation.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        codex_line("user", "original request"),
                        json.dumps(
                            {
                                "type": "compacted",
                                "payload": {
                                    "window_number": 1,
                                    "replacement_history": [],
                                    "message": "",
                                },
                            }
                        ),
                        codex_line("assistant", "recent work"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            source = bridge.read_transcript("codex", path)
            self.assertEqual(source.opaque_compactions, 1)
            self.assertEqual(from_last_compaction(source.turns)[1], 0)
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                result = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="x-1",
                    storage=str(path),
                    cwd="/home/andrew",
                )
            body = result.path.read_text(encoding="utf-8")
            self.assertIn("original request", body)
            self.assertIn("stores those summaries encrypted", body)


class HarnessRegistryTests(unittest.TestCase):
    def test_every_harness_is_self_consistent(self) -> None:
        for entry in REGISTRY.adapters():
            self.assertGreater(entry.budget.context_tokens, 0)
            self.assertGreater(entry.budget.usable_fraction, 0)
            self.assertLessEqual(entry.budget.usable_fraction, 1)
            self.assertGreater(entry.budget.chars_per_token, 0)
            self.assertTrue(entry.budget.source)
        for entry in REGISTRY.adapters():
            self.assertTrue(entry.label)
            for hook in (entry.read, entry.write, entry.locate, entry.change_status):
                self.assertTrue(callable(hook))

    def test_an_unknown_harness_is_reported_rather_than_guessed(self) -> None:
        with self.assertRaises(BridgeError):
            read_turns("opencode", __file__)
        self.assertFalse(bridge.native_session_exists("opencode", "whatever"))

    def test_codex_budget_floor_is_not_writer_context_metadata(self) -> None:
        self.assertEqual(
            REGISTRY.get("codex").budget.context_tokens,
            CODEX_BUDGET_CONTEXT_TOKENS,
        )
        self.assertNotEqual(CODEX_BUDGET_CONTEXT_TOKENS, CODEX_CONTEXT_WINDOW)
        from ai_sessions.harnesses import claude, codex

        registry_source = inspect.getsource(claude) + inspect.getsource(codex)
        self.assertIn("context_tokens=CODEX_BUDGET_CONTEXT_TOKENS", registry_source)
        self.assertIn("context_tokens=CLAUDE_BUDGET_CONTEXT_TOKENS", registry_source)


class WriteSessionTests(unittest.TestCase):
    def test_claude_session_lands_in_the_project_directory_for_its_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory)):
                session_id, path = write_claude_session(
                    cwd="/home/andrew/ai-sessions",
                    turns=[Turn("user", "hi"), Turn("assistant", "hello")],
                    title="Bridged",
                )
            self.assertEqual(path.parent.name, "-home-andrew-ai-sessions")
            self.assertEqual(path.name, f"{session_id}.jsonl")
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            kinds = [record.get("type") for record in records]
            self.assertEqual(kinds, ["user", "assistant", "custom-title"])
            self.assertEqual(records[0]["message"]["content"], "hi")
            self.assertEqual(records[1]["message"]["content"][0]["text"], "hello")
            self.assertEqual(records[1]["parentUuid"], records[0]["uuid"])
            self.assertEqual(records[2]["customTitle"], "Bridged")
            self.assertTrue(all(r.get("sessionId") == session_id for r in records))

    def test_codex_rollout_opens_with_session_meta_and_names_the_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex_harness, "CODEX_HOME", Path(directory)):
                session_id, path = write_codex_session(
                    cwd="/home/andrew",
                    turns=[Turn("user", "hi"), Turn("assistant", "hello")],
                    title="Bridged",
                )
                index = Path(directory) / "session_index.jsonl"
                named = json.loads(index.read_text(encoding="utf-8").strip())
            self.assertTrue(path.name.startswith("rollout-"))
            self.assertIn(session_id, path.name)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["type"], "session_meta")
            self.assertEqual(records[0]["payload"]["session_id"], session_id)
            self.assertEqual(records[0]["payload"]["cwd"], "/home/andrew")
            self.assertEqual([record["ordinal"] for record in records], list(range(len(records))))
            # Model context and TUI scrollback are recorded separately, and
            # both sit inside a started-and-completed turn or the history
            # projection drops them.
            self.assertEqual(
                [record["payload"].get("type") for record in records[1:]],
                [
                    "task_started",
                    "message",
                    "user_message",
                    "message",
                    "agent_message",
                    "task_complete",
                ],
            )
            self.assertEqual(records[2]["payload"]["content"][0]["type"], "input_text")
            self.assertEqual(records[3]["payload"]["message"], "hi")
            self.assertEqual(records[4]["payload"]["content"][0]["type"], "output_text")
            self.assertEqual(records[5]["payload"]["message"], "hello")
            self.assertEqual(records[6]["payload"]["last_agent_message"], "hello")
            turn_ids = {
                record["payload"]["turn_id"]
                for record in records[1:]
                if "turn_id" in record["payload"]
            }
            self.assertEqual(len(turn_ids), 1, "one exchange must share one turn id")
            self.assertEqual(named["id"], session_id)
            self.assertEqual(named["thread_name"], "Bridged")

    def test_each_exchange_gets_its_own_codex_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex_harness, "CODEX_HOME", Path(directory)):
                _, path = write_codex_session(
                    cwd="/home/andrew",
                    turns=[
                        Turn("user", "one"),
                        Turn("assistant", "two"),
                        Turn("user", "three"),
                        Turn("assistant", "four"),
                        Turn("user", "dangling"),
                    ],
                )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            starts = [r for r in records if r["payload"].get("type") == "task_started"]
            completes = [r for r in records if r["payload"].get("type") == "task_complete"]
            self.assertEqual(len(starts), 3)
            self.assertEqual(len(completes), 3)
            # A trailing user message with no reply still needs a closed turn.
            self.assertEqual(completes[-1]["payload"]["last_agent_message"], "")
            self.assertEqual(
                len({record["payload"]["turn_id"] for record in starts}),
                3,
                "turns must not share ids",
            )


class BridgeTests(unittest.TestCase):
    def source(self, directory: str) -> Path:
        path = Path(directory) / "rollout.jsonl"
        path.write_text(
            "\n".join([codex_line("user", "Ship it"), codex_line("assistant", "Shipped.")]) + "\n",
            encoding="utf-8",
        )
        return path

    def test_codex_conversation_becomes_a_resumable_claude_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                result = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="019f-source",
                    storage=str(source),
                    cwd="/home/andrew",
                    title="ai-sessions-cli-util",
                )
            self.assertEqual(result.tool, "claude")
            self.assertEqual(result.turns, 2)
            self.assertTrue(result.path.is_file())
            records = [
                json.loads(line) for line in result.path.read_text(encoding="utf-8").splitlines()
            ]
            preamble = records[0]["message"]["content"]
            self.assertIn("[ai-sessions]", preamble)
            self.assertIn("019f-source", preamble)
            self.assertIn("Ship it", preamble)
            self.assertEqual(records[1]["message"]["content"][0]["text"], "Shipped.")

    def test_the_source_transcript_is_left_byte_for_byte_unchanged(self) -> None:
        # Sessions at rest are read-only to this tool; a bridge only ever adds
        # a new file beside them.
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            before = source.read_bytes()
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                result = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="019f-source",
                    storage=str(source),
                    cwd="/home/andrew",
                    title="whatever",
                )
            self.assertEqual(source.read_bytes(), before)
            self.assertNotEqual(result.path, source)

    def test_explicit_token_budget_changes_written_payload_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jsonl"
            source.write_text(
                "\n".join(
                    codex_line("user" if index % 2 == 0 else "assistant", str(index) * 2_000)
                    for index in range(20)
                )
                + "\n",
                encoding="utf-8",
            )
            budget = resolve_budget("claude", max_tokens=5_000)
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                result = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="source-session",
                    storage=str(source),
                    cwd=directory,
                    budget=budget,
                )
            written = read_turns("claude", result.path)
        self.assertEqual((result.budget.tokens, result.budget.chars), (5_000, 10_000))
        self.assertGreater(result.dropped, 0)
        self.assertLessEqual(sum(len(turn.text) for turn in written), 10_000)
        self.assertIn("approximately 5,000 tokens / 10,000 projected characters", written[0].text)

    def test_bridge_reports_source_and_assembled_message_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jsonl"
            source.write_text(
                "\n".join(codex_line("user", text) for text in ("one", "two", "three")) + "\n",
                encoding="utf-8",
            )
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                result = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="source-session",
                    storage=str(source),
                    cwd=directory,
                )
            handoff = read_turns("claude", result.path)[0].text
        self.assertEqual((result.turns, result.written_turns), (3, 1))
        self.assertIn("3 source message(s) below were assembled into 1 target message(s)", handoff)

    def test_bridge_refuses_a_handoff_note_that_exceeds_its_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            with (
                patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"),
                patch.object(
                    conversion,
                    "handoff_note",
                    return_value="x" * (HANDOFF_NOTE_RESERVE_CHARS - 1),
                ),
                self.assertRaisesRegex(BridgeError, "handoff note exceeds"),
            ):
                bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="source-session",
                    storage=str(source),
                    cwd=directory,
                )

    def test_bridge_refuses_a_selected_payload_above_the_applied_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            selected = SelectionResult(
                [Turn("user", "x" * MIN_BRIDGE_CHARS)],
                dropped=0,
                truncated=0,
            )
            with (
                patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"),
                patch.object(conversion, "select_messages", return_value=selected),
                self.assertRaisesRegex(BridgeError, "payload exceeds"),
            ):
                bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="source-session",
                    storage=str(source),
                    cwd=directory,
                    budget=resolve_budget("claude", max_chars=MIN_BRIDGE_CHARS),
                )

    def test_unset_target_policies_diverge_on_a_340k_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_source = Path(directory) / "codex-source.jsonl"
            claude_source = Path(directory) / "claude-source.jsonl"
            messages = [f"{index:03d}:" + "x" * 1_996 for index in range(170)]
            codex_source.write_text(
                "\n".join(
                    codex_line("user" if index % 2 == 0 else "assistant", text)
                    for index, text in enumerate(messages)
                )
                + "\n",
                encoding="utf-8",
            )
            claude_source.write_text(
                "\n".join(
                    claude_line("user" if index % 2 == 0 else "assistant", text)
                    for index, text in enumerate(messages)
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude-home"),
                patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex-home"),
            ):
                into_claude = bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="codex-source",
                    storage=str(codex_source),
                    cwd=directory,
                )
                into_codex = bridge.bridge(
                    source_tool="claude",
                    target_tool="codex",
                    session_id="claude-source",
                    storage=str(claude_source),
                    cwd=directory,
                )
        self.assertGreater(into_claude.dropped, 0)
        self.assertEqual(into_codex.dropped, 0)
        self.assertEqual(into_codex.turns, len(messages))

    def test_bridging_into_the_recording_harness_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BridgeError):
                bridge.bridge(
                    source_tool="codex",
                    target_tool="codex",
                    session_id="x",
                    storage=str(self.source(directory)),
                    cwd="/home/andrew",
                )

    def test_empty_transcript_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(BridgeError):
                bridge.bridge(
                    source_tool="codex",
                    target_tool="claude",
                    session_id="x",
                    storage=str(path),
                    cwd="/home/andrew",
                )


def session(directory: str, **overrides: object) -> Session:
    path = Path(directory) / "rollout.jsonl"
    if not path.exists():
        path.write_text(
            "\n".join([codex_line("user", "Ship it"), codex_line("assistant", "Shipped.")]) + "\n",
            encoding="utf-8",
        )
    values: dict[str, object] = dict(
        tool="codex",
        session_id="019f-source",
        title="ai-sessions-cli-util",
        cwd=directory,
        updated=100.0,
        created=0,
        preview="",
        named=True,
        storage=str(path),
    )
    values.update(overrides)
    return Session(**values)


class LaunchIntegrationTests(unittest.TestCase):
    @staticmethod
    def claude_copy(
        source: Session, prepared: Session, storage: str, *, launch_tool: str = "codex"
    ) -> Session:
        return Session(
            tool="claude",
            session_id=prepared.launch_target("claude") or prepared.session_id,
            title=source.title,
            cwd=source.cwd,
            updated=source.updated,
            created=source.created,
            preview="",
            named=True,
            storage=storage,
            origin="cross",
            launch_targets={"claude": prepared.launch_target("claude") or prepared.session_id},
            launch_tool=launch_tool,
        )

    @staticmethod
    def loaded_dry_run(
        directory: str, config_text: str, *, long_transcript: bool = False
    ) -> tuple[Session, UserState, str, str]:
        item = session(directory, launch_tool="claude")
        if long_transcript:
            Path(item.storage).write_text(
                "\n".join(
                    codex_line(
                        "user" if index % 2 == 0 else "assistant",
                        f"message-{index}:" + (str(index) * 2_000),
                    )
                    for index in range(20)
                )
                + "\n",
                encoding="utf-8",
            )
        config_path = Path(directory) / "config.toml"
        config_path.write_text(config_text, encoding="utf-8")
        config = LaunchConfig.load(config_path)
        state = UserState(path=Path(directory) / "state.json")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = launch(item, config, dry_run=True, state=state)
        if result != 0:
            raise AssertionError(stderr.getvalue())
        return item, state, stdout.getvalue(), stderr.getvalue()

    def test_a_session_with_a_transcript_offers_both_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory)
            self.assertEqual(available_launch_tools(item), ("codex", "claude"))
            self.assertTrue(can_bridge(item))

    def test_a_session_without_a_transcript_offers_only_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, storage="")
            self.assertEqual(available_launch_tools(item), ("codex",))

    def test_loaded_token_budget_drives_the_real_dry_run_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, state, stdout, stderr = self.loaded_dry_run(
                directory,
                "version = 2\n[bridge]\nmax_tokens = 5000\n",
                long_transcript=True,
            )
            target_path = Path(state.bridges[item.key]["claude"]["storage"])
            written = read_turns("claude", target_path)
        self.assertIn("claude --resume", stdout)
        self.assertLessEqual(sum(len(turn.text) for turn in written), 10_000)
        self.assertIn("older message(s) dropped to fit", stderr)
        self.assertIn("5,000 tokens / 10,000 projected characters", stderr)
        self.assertIn("5,000 tokens / 10,000 projected characters", written[0].text)

    def test_loaded_missing_budget_keeps_the_same_long_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, state, _, stderr = self.loaded_dry_run(
                directory,
                "version = 2\n[bridge]\n",
                long_transcript=True,
            )
            target_path = Path(state.bridges[item.key]["claude"]["storage"])
            written = read_turns("claude", target_path)
        self.assertNotIn("dropped to fit", stderr)
        self.assertIn("150,000 tokens / 300,000 projected characters", stderr)
        self.assertIn("replaces the previous global 950,000-character ceiling", stderr)
        self.assertIn("replaces the previous global 950,000-character ceiling", written[0].text)
        self.assertIn("message-10:", "\n".join(turn.text for turn in written))

    def test_launch_explains_migrated_explicit_and_clamped_budgets(self) -> None:
        cases = (
            (
                "version = 1\n[bridge]\nmax_chars = 950000\n",
                "target-default-migrated",
                "Migrated the legacy 950,000-character default to target policy",
                "legacy version-1 950,000-character machine default was replaced",
            ),
            (
                "version = 2\n[bridge]\nmax_chars = 950000\n",
                "config.max_chars",
                "legacy max_chars override exceeds target policy",
                "legacy character override exceeds the target policy",
            ),
            (
                "version = 2\n[bridge]\nmax_tokens = 1\n",
                "config.max_tokens",
                "raised to the safe bridge minimum",
                "raised to the safe bridge minimum",
            ),
        )
        for config_text, origin, launch_explanation, handoff_explanation in cases:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as directory:
                item, state, _, stderr = self.loaded_dry_run(directory, config_text)
                target_path = Path(state.bridges[item.key]["claude"]["storage"])
                handoff = read_turns("claude", target_path)[0].text
                self.assertIn(f"({origin})", stderr)
                self.assertIn(launch_explanation, stderr)
                self.assertIn(f"({origin})", handoff)
                self.assertIn(handoff_explanation, handoff)

    def test_launch_reports_truncated_anchor_without_claiming_a_drop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            Path(item.storage).write_text(
                "\n".join(
                    [
                        codex_line("user", "x" * 20_000),
                        codex_line("assistant", "y" * 20_000),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state = UserState(path=Path(directory) / "state.json")
            stderr = io.StringIO()
            with (
                patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                code = launch(
                    item,
                    LaunchConfig(bridge_max_tokens=4_096),
                    dry_run=True,
                    state=state,
                )
            target = Path(state.bridges[item.key]["claude"]["storage"])
            handoff = read_turns("claude", target)[0].text
        self.assertEqual(code, 0)
        self.assertNotIn("dropped to fit", stderr.getvalue())
        self.assertIn("2 anchor message(s) truncated to fit", stderr.getvalue())
        self.assertIn("2 anchor message(s) were truncated", handoff)

    def test_launch_reports_a_short_head_and_truncated_newest_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            Path(item.storage).write_text(
                "\n".join([codex_line("user", "short"), codex_line("assistant", "x" * 20_000)])
                + "\n",
                encoding="utf-8",
            )
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, notice = prepare_launch(
                    item,
                    config=LaunchConfig(bridge_max_tokens=4_096),
                )
            handoff = read_turns("claude", prepared.storage)[0].text
        self.assertNotIn("dropped to fit", notice)
        self.assertIn("1 anchor message(s) truncated to fit", notice)
        self.assertIn("1 anchor message(s) were truncated", handoff)

    def test_command_for_refuses_an_unbridged_cross_harness_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            with self.assertRaises(BridgeError):
                command_for(item, LaunchConfig())

    def test_prepare_launch_bridges_then_the_command_resumes_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, note = prepare_launch(item)
                self.assertIn("Copied 2 source message(s), assembled as 2 target message(s)", note)
            argv = command_for(prepared, LaunchConfig())
            self.assertEqual(argv[:2], ["claude", "--resume"])
            self.assertNotEqual(argv[2], item.session_id)

    def test_native_launch_resumes_the_original_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory)
            before = sorted(p.name for p in Path(directory).iterdir())
            digest = Path(item.storage).read_bytes()
            prepared, note = prepare_launch(item)
            self.assertEqual(note, "")
            self.assertEqual(
                command_for(prepared, LaunchConfig()), ["codex", "resume", "019f-source"]
            )
            # Resuming a session in its own harness must be a pure read.
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), before)
            self.assertEqual(Path(item.storage).read_bytes(), digest)

    def test_a_recorded_counterpart_is_used_when_it_really_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claude_home = Path(directory) / "claude"
            counterpart = claude_home / "projects" / "-tmp"
            counterpart.mkdir(parents=True)
            (counterpart / "real-claude-id.jsonl").write_text("", encoding="utf-8")
            item = session(
                directory,
                launch_targets={"codex": "019f-source", "claude": "real-claude-id"},
                launch_tool="claude",
            )
            with patch.object(claude_harness, "CLAUDE_HOME", claude_home):
                prepared, note = prepare_launch(item)
            self.assertEqual(note, "")
            self.assertEqual(command_for(prepared, LaunchConfig())[2], "real-claude-id")

    def test_a_counterpart_that_does_not_exist_is_replaced_by_a_copy(self) -> None:
        # Cross-provider references are matched out of transcript text, so an
        # unrelated id of the same shape can be recorded as a counterpart.
        with tempfile.TemporaryDirectory() as directory:
            item = session(
                directory,
                launch_targets={"codex": "019f-source", "claude": "never-existed"},
                launch_tool="claude",
            )
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, note = prepare_launch(item)
            self.assertIn("does not exist", note)
            self.assertNotEqual(command_for(prepared, LaunchConfig())[2], "never-existed")

    def test_a_bridged_copy_is_reused_until_the_source_moves_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                first, _ = prepare_launch(item, state=state)
                second, note = prepare_launch(item, state=state)
                self.assertEqual(first.launch_target("claude"), second.launch_target("claude"))
                self.assertIn("Continuing the Claude copy", note)

                with Path(item.storage).open("a", encoding="utf-8") as handle:
                    handle.write(codex_line("user", "The source moved") + "\n")
                moved = session(directory, launch_tool="claude", updated=200.0)
                third, note = prepare_launch(moved, state=state)
            self.assertNotEqual(third.launch_target("claude"), first.launch_target("claude"))
            self.assertIn("Copied", note)

    def test_a_bridged_copy_is_remade_when_its_file_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                first, _ = prepare_launch(item, state=state)
                Path(state.bridges[item.key]["claude"]["storage"]).unlink()
                second, note = prepare_launch(item, state=state)
            self.assertNotEqual(second.launch_target("claude"), first.launch_target("claude"))
            self.assertIn("Copied", note)

    def test_an_append_during_bridge_aborts_before_writing_a_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            source_path = Path(source.storage)
            cursor_before_bridge = source_path.stat().st_size
            arriving = codex_line("user", "Arrived during the bridge") + "\n"
            split = len(arriving) // 2
            with source_path.open("a", encoding="utf-8") as handle:
                handle.write(arriving[:split])
            state = UserState(path=Path(directory) / "state.json")

            def append_while_reading(*args: object, **kwargs: object) -> Transcript:
                with source_path.open("a", encoding="utf-8") as handle:
                    handle.write(arriving[split:])
                return Transcript([Turn("user", "Ship it"), Turn("assistant", "Shipped.")])

            with (
                patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"),
                patch.object(bridge, "read_transcript", side_effect=append_while_reading),
            ):
                with self.assertRaisesRegex(BridgeError, "changed or became incomplete"):
                    prepare_launch(source, state=state)

            self.assertGreater(source_path.stat().st_size, cursor_before_bridge)
            self.assertFalse((Path(directory) / "claude").exists())

    def test_codex_to_claude_work_to_codex_follows_the_newest_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            claude_home = Path(directory) / "claude"
            codex_home = Path(directory) / "codex"
            with patch.object(claude_harness, "CLAUDE_HOME", claude_home):
                claude_prepared, _ = prepare_launch(source, state=state)
            copy = self.claude_copy(
                source, claude_prepared, state.bridges[source.key]["claude"]["storage"]
            )
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(claude_line("user", "Work completed in Claude") + "\n")
                handle.write(claude_line("assistant", "Claude result") + "\n")
            original_row = session(directory)
            state.apply([original_row, copy])
            self.assertTrue(original_row.superseded)
            self.assertFalse(copy.superseded)
            output = io.StringIO()
            with redirect_stdout(output):
                list_output([original_row, copy])
            self.assertIn("HEAD", output.getvalue().splitlines()[0])
            self.assertIn("superseded", output.getvalue().splitlines()[1])

            with patch.object(codex_harness, "CODEX_HOME", codex_home):
                codex_prepared, note = prepare_launch(original_row, state=state)
            self.assertIn("Copied", note)
            self.assertEqual(codex_prepared.tool, "codex")
            self.assertNotEqual(codex_prepared.session_id, source.session_id)
            self.assertIn(
                "Work completed in Claude", Path(codex_prepared.storage).read_text("utf-8")
            )

            # Either historical row now resolves to the same current Codex copy.
            state.apply([original_row, copy, codex_prepared])
            copy.launch_tool = "codex"
            again, _ = prepare_launch(copy, state=state)
            self.assertEqual(again.session_id, codex_prepared.session_id)

    def test_switching_harnesses_without_new_work_reuses_an_equivalent_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                claude_prepared, _ = prepare_launch(source, state=state)
            copy = self.claude_copy(
                source, claude_prepared, state.bridges[source.key]["claude"]["storage"]
            )
            state.apply([source, copy])
            copy.launch_tool = "codex"

            prepared, _ = prepare_launch(copy, state=state)
            self.assertEqual(prepared.tool, "codex")
            self.assertEqual(prepared.session_id, source.session_id)

    def test_two_members_advancing_from_one_frontier_is_reported_as_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                claude_prepared, _ = prepare_launch(source, state=state)
            copy = self.claude_copy(
                source, claude_prepared, state.bridges[source.key]["claude"]["storage"]
            )
            with Path(source.storage).open("a", encoding="utf-8") as handle:
                handle.write(codex_line("user", "Codex branch") + "\n")
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(claude_line("user", "Claude branch") + "\n")
            state.apply([source, copy])
            copy.launch_tool = "codex"

            with self.assertRaisesRegex(BridgeError, "divergent heads"):
                prepare_launch(source, state=state)
            self.assertTrue(source.diverged)
            self.assertTrue(copy.diverged)

    def test_older_generations_remain_superseded_during_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                claude_prepared, _ = prepare_launch(original, state=state)
            copy = self.claude_copy(
                original, claude_prepared, state.bridges[original.key]["claude"]["storage"]
            )
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(claude_line("user", "Advance one generation") + "\n")
            state.apply([original, copy])
            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                newest, _ = prepare_launch(original, state=state)

            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(claude_line("user", "Claude fork") + "\n")
            with Path(newest.storage).open("a", encoding="utf-8") as handle:
                handle.write(codex_line("user", "Codex fork") + "\n")
            state.apply([original, copy, newest])

            self.assertTrue(original.superseded)
            self.assertFalse(original.diverged)
            self.assertTrue(copy.diverged)
            self.assertTrue(newest.diverged)

    def test_missing_newest_generation_never_promotes_an_older_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                claude_prepared, _ = prepare_launch(original, state=state)
            copy = self.claude_copy(
                original, claude_prepared, state.bridges[original.key]["claude"]["storage"]
            )
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(claude_line("user", "Newest work") + "\n")
            state.apply([original, copy])
            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                newest, _ = prepare_launch(original, state=state)

            Path(copy.storage).unlink()
            Path(newest.storage).unlink()
            state.apply([original])
            with self.assertRaisesRegex(BridgeError, "unavailable newest"):
                prepare_launch(original, state=state)

    def test_partial_appended_jsonl_is_conservatively_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory)
            path = Path(source.storage)
            cursor = path.stat().st_size
            with path.open("ab") as handle:
                handle.write(b'{"type":"response_item","payload":')

            self.assertTrue(bridge.conversation_changed_since("codex", path, cursor))
            self.assertEqual(bridge.conversation_change_status("codex", path, cursor), "unstable")

            source.launch_tool = "claude"
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                with self.assertRaisesRegex(BridgeError, "changed or became incomplete"):
                    prepare_launch(source, state=state)

    def test_semantic_append_does_not_hide_a_later_partial_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases = (
                ("codex", codex_line("user", "Complete work")),
                ("claude", claude_line("user", "Complete work")),
            )
            for tool, complete in cases:
                with self.subTest(tool=tool):
                    path = Path(directory) / f"{tool}.jsonl"
                    path.write_text(codex_line("user", "Earlier") + "\n", encoding="utf-8")
                    cursor = path.stat().st_size
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(complete + "\n" + '{"type":"unfinished"')
                    self.assertEqual(
                        bridge.conversation_change_status(tool, path, cursor), "unstable"
                    )

    def test_unchanged_snapshot_does_not_reparse_the_full_valid_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            path.write_text(codex_line("user", "Earlier") + "\n", encoding="utf-8")
            cursor = path.stat().st_size
            with path.open("a", encoding="utf-8") as handle:
                handle.write(codex_line("user", "Changed") + "\n")
                for index in range(100):
                    handle.write(json.dumps({"type": "metadata", "index": index}) + "\n")

            with patch.object(bridge.json, "loads", wraps=json.loads) as loads:
                self.assertEqual(
                    bridge.conversation_change_status("codex", path, cursor), "changed"
                )
                parsed = loads.call_count
                self.assertEqual(
                    bridge.conversation_change_status("codex", path, cursor), "changed"
                )
            self.assertGreater(parsed, 1)
            self.assertEqual(loads.call_count, parsed)

    def test_malformed_newline_terminated_record_after_work_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            path.write_text(codex_line("user", "Earlier") + "\n", encoding="utf-8")
            cursor = path.stat().st_size
            with path.open("a", encoding="utf-8") as handle:
                handle.write(codex_line("user", "Changed") + "\n")
                handle.write('{"type":"malformed"\n')

            self.assertEqual(bridge.conversation_change_status("codex", path, cursor), "unstable")

    def test_transient_unstable_snapshot_is_retried_without_metadata_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            path.write_text(codex_line("user", "Earlier") + "\n", encoding="utf-8")
            outcomes = iter(("unstable", "unchanged"))
            entry = REGISTRY.get("codex")
            retrying = HarnessAdapter(
                name=entry.name,
                label=entry.label,
                short_label=entry.short_label,
                order=entry.order,
                home=entry.home,
                default_command=entry.default_command,
                dangerous_args=entry.dangerous_args,
                source_kinds=entry.source_kinds,
                id_patterns=entry.id_patterns,
                read=entry.read,
                write=entry.write,
                locate=entry.locate,
                change_status=lambda *_: next(outcomes),
                budget=entry.budget,
            )

            with REGISTRY.temporary(retrying):
                self.assertEqual(bridge.conversation_change_status("codex", path, 0), "unstable")
                self.assertEqual(bridge.conversation_change_status("codex", path, 0), "unchanged")

    def test_retry_after_partial_tail_uses_the_last_complete_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            source_path = Path(source.storage)
            complete_cursor = source_path.stat().st_size
            arriving = codex_line("user", "Completed after retry")
            split = len(arriving) // 2
            with source_path.open("a", encoding="utf-8") as handle:
                handle.write(arriving[:split])
            state = UserState(path=Path(directory) / "state.json")

            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                with self.assertRaisesRegex(BridgeError, "changed or became incomplete"):
                    prepare_launch(source, state=state)

                conversation_id = state.conversation_id_for(source)
                member = state.conversations[conversation_id]["members"][source.key]
                self.assertEqual(member["cursor"], complete_cursor)

                with source_path.open("a", encoding="utf-8") as handle:
                    handle.write(arriving[split:] + "\n")
                prepared, note = prepare_launch(source, state=state)

            self.assertEqual(prepared.tool, "claude")
            self.assertIn("Copied", note)

    def test_renaming_a_copy_does_not_advance_the_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                claude_prepared, _ = prepare_launch(source, state=state)
            copy = self.claude_copy(
                source, claude_prepared, state.bridges[source.key]["claude"]["storage"]
            )
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "custom-title", "customTitle": "renamed"}) + "\n")
            state.apply([source, copy])
            copy.launch_tool = "codex"

            prepared, _ = prepare_launch(copy, state=state)
            self.assertEqual(prepared.session_id, source.session_id)

    def test_uuid_shaped_text_is_not_treated_as_a_launch_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claude_home = Path(directory) / "claude"
            project = claude_home / "projects" / "test"
            project.mkdir(parents=True)
            session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            pasted = "019f59af-e300-7bd1-be75-e47599b5b593"
            (project / f"{session_id}.jsonl").write_text(
                claude_line("user", f"ordinary correlation id: {pasted}") + "\n",
                encoding="utf-8",
            )
            refs: dict[str, list[str]] = {}
            with patch("ai_sessions.app.CLAUDE_HOME", claude_home):
                items = load_claude_sessions(use_cache=False, codex_refs=refs)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].launch_targets, {"claude": session_id})
            self.assertEqual(refs[session_id], [pasted])

    def test_state_v6_round_trips_conversation_members_and_cursors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state_path = Path(directory) / "state.json"
            state = UserState(state_path)
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, _ = prepare_launch(source, state=state)
            conversation_id = state.conversation_id_for(source)
            reloaded = UserState(state_path)
            self.assertEqual(reloaded.conversation_id_for(source), conversation_id)
            members = reloaded.conversations[conversation_id]["members"]
            self.assertEqual(set(members), {source.key, prepared.key})
            self.assertTrue(all(isinstance(member["cursor"], int) for member in members.values()))

    def test_v5_bridge_state_migrates_without_resuming_the_stale_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory)
            target_path = Path(directory) / "claude-copy.jsonl"
            target_path.write_text(
                claude_line("user", "Imported history")
                + "\n"
                + claude_line("user", "Later Claude work")
                + "\n",
                encoding="utf-8",
            )
            target = Session(
                tool="claude",
                session_id="claude-copy",
                title=source.title,
                cwd=source.cwd,
                updated=200,
                created=100,
                preview="",
                named=True,
                storage=str(target_path),
                launch_tool="codex",
            )
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 5,
                        "names": {},
                        "original_names": {},
                        "hidden": [],
                        "launch_tools": {},
                        "bridges": {
                            source.key: {
                                "claude": {
                                    "session_id": target.session_id,
                                    "storage": str(target_path),
                                    "source_updated": source.updated,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = UserState(state_path)
            state.apply([source, target])
            self.assertTrue(source.superseded)
            self.assertFalse(target.superseded)

            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                prepared, _ = prepare_launch(source, state=state)
            self.assertNotEqual(prepared.session_id, source.session_id)
            self.assertIn("Later Claude work", Path(prepared.storage).read_text("utf-8"))

    def test_bridge_writes_machine_readable_conversation_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = session(directory, launch_tool="claude")
            state = UserState(Path(directory) / "state.json")
            with patch.object(claude_harness, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, _ = prepare_launch(source, state=state)
            text = Path(prepared.storage).read_text(encoding="utf-8")
            self.assertIn("[ai-sessions-provenance v1]", text)
            self.assertIn(state.conversation_id_for(source), text)

    def test_claude_subagent_bridges_as_a_plain_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-1.jsonl"
            path.write_text(
                claude_line("user", "Search the repo", isSidechain=True) + "\n",
                encoding="utf-8",
            )
            item = session(
                directory,
                tool="claude",
                source="subagent",
                resume_id="parent-id",
                parent_id="parent-id",
                storage=str(path),
                launch_tool="codex",
            )
            with patch.object(codex_harness, "CODEX_HOME", Path(directory) / "codex"):
                prepared, _ = prepare_launch(item)
            argv = command_for(prepared, LaunchConfig())
            # The parent-id and --include-non-interactive rules describe Claude's
            # own storage, so they must not leak into the Codex copy.
            self.assertEqual(argv[:2], ["codex", "resume"])
            self.assertNotIn("--include-non-interactive", argv)
            self.assertNotIn("parent-id", argv)


if __name__ == "__main__":
    unittest.main()
