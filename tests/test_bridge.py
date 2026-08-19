import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import bridge
from ai_sessions.app import Session, UserState, command_for, prepare_launch
from ai_sessions.bridge import (
    HARNESSES,
    BridgeError,
    ToolCall,
    Turn,
    bridged_title,
    fit,
    merge_runs,
    prepare,
    read_turns,
    write_claude_session,
    write_codex_session,
)
from ai_sessions.config import LaunchConfig


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
            turns = read_turns(
                "claude", self.claude_with_a_tool_call(directory), tool_calls=False
            )
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

    def test_bridged_title_names_the_origin(self) -> None:
        self.assertEqual(
            bridged_title("ai-sessions-cli-util", "codex"), "ai-sessions-cli-util (from Codex)"
        )
        self.assertEqual(bridged_title("", "claude"), "untitled session (from Claude)")


class HarnessRegistryTests(unittest.TestCase):
    def test_every_harness_is_self_consistent(self) -> None:
        for name, entry in HARNESSES.items():
            self.assertEqual(name, entry.name)
            self.assertTrue(entry.label)
            for hook in (entry.read, entry.write, entry.locate):
                self.assertTrue(callable(hook))

    def test_an_unknown_harness_is_reported_rather_than_guessed(self) -> None:
        with self.assertRaises(BridgeError):
            read_turns("opencode", __file__)
        self.assertFalse(bridge.native_session_exists("opencode", "whatever"))


class WriteSessionTests(unittest.TestCase):
    def test_claude_session_lands_in_the_project_directory_for_its_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bridge, "CLAUDE_HOME", Path(directory)):
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
            with patch.object(bridge, "CODEX_HOME", Path(directory)):
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
            with patch.object(bridge, "CODEX_HOME", Path(directory)):
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
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
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
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
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
    def test_a_session_with_a_transcript_offers_both_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory)
            self.assertEqual(item.available_launch_tools, ("codex", "claude"))
            self.assertTrue(item.can_bridge)

    def test_a_session_without_a_transcript_offers_only_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, storage="")
            self.assertEqual(item.available_launch_tools, ("codex",))

    def test_command_for_refuses_an_unbridged_cross_harness_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            with self.assertRaises(BridgeError):
                command_for(item, LaunchConfig())

    def test_prepare_launch_bridges_then_the_command_resumes_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, note = prepare_launch(item)
            self.assertIn("Copied 2 message(s)", note)
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
            with patch.object(bridge, "CLAUDE_HOME", claude_home):
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
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
                prepared, note = prepare_launch(item)
            self.assertIn("does not exist", note)
            self.assertNotEqual(command_for(prepared, LaunchConfig())[2], "never-existed")

    def test_a_bridged_copy_is_reused_until_the_source_moves_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
                first, _ = prepare_launch(item, state=state)
                second, note = prepare_launch(item, state=state)
                self.assertEqual(first.launch_target("claude"), second.launch_target("claude"))
                self.assertIn("Continuing the Claude copy", note)

                moved = session(directory, launch_tool="claude", updated=200.0)
                third, note = prepare_launch(moved, state=state)
            self.assertNotEqual(third.launch_target("claude"), first.launch_target("claude"))
            self.assertIn("Copied", note)

    def test_a_bridged_copy_is_remade_when_its_file_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = session(directory, launch_tool="claude")
            state = UserState(path=Path(directory) / "state.json")
            with patch.object(bridge, "CLAUDE_HOME", Path(directory) / "claude"):
                first, _ = prepare_launch(item, state=state)
                Path(state.bridges[item.key]["claude"]["storage"]).unlink()
                second, note = prepare_launch(item, state=state)
            self.assertNotEqual(second.launch_target("claude"), first.launch_target("claude"))
            self.assertIn("Copied", note)

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
            with patch.object(bridge, "CODEX_HOME", Path(directory) / "codex"):
                prepared, _ = prepare_launch(item)
            argv = command_for(prepared, LaunchConfig())
            # The parent-id and --include-non-interactive rules describe Claude's
            # own storage, so they must not leak into the Codex copy.
            self.assertEqual(argv[:2], ["codex", "resume"])
            self.assertNotIn("--include-non-interactive", argv)
            self.assertNotIn("parent-id", argv)


if __name__ == "__main__":
    unittest.main()
