import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import (
    Session,
    UserState,
    agent_tag,
    append_jsonl,
    publish_name,
    rename_session,
)


def session(**overrides) -> Session:
    base = dict(
        tool="claude",
        session_id="session-id",
        title="Test",
        cwd="/tmp/project",
        updated=0,
        created=0,
        preview="",
        named=True,
        storage="",
    )
    base.update(overrides)
    return Session(**base)


class AppendJsonlTests(unittest.TestCase):
    def test_creates_file_and_writes_compact_line(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "index.jsonl"
            append_jsonl(path, {"a": 1})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"a":1}\n')

    def test_repairs_missing_final_newline(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "index.jsonl"
            path.write_text('{"existing":true}', encoding="utf-8")
            append_jsonl(path, {"added": True})
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"existing": True})
            self.assertEqual(json.loads(lines[1]), {"added": True})

    def test_does_not_rewrite_existing_content(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "index.jsonl"
            path.write_text('{"first":1}\n', encoding="utf-8")
            append_jsonl(path, {"second": 2})
            self.assertTrue(path.read_text(encoding="utf-8").startswith('{"first":1}\n'))


class PublishNameTests(unittest.TestCase):
    def test_claude_rename_appends_custom_title(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text('{"type":"ai-title","aiTitle":"Auto"}\n', encoding="utf-8")
            item = session(session_id="abc", storage=str(transcript))
            self.assertEqual(publish_name(item, "my-name"), "")
            last = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(
                last, {"type": "custom-title", "customTitle": "my-name", "sessionId": "abc"}
            )

    def test_claude_rename_reports_missing_transcript(self) -> None:
        item = session(storage="/nonexistent/path.jsonl")
        self.assertIn("missing", publish_name(item, "my-name"))

    def test_codex_rename_appends_thread_name(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(app, "CODEX_HOME", home):
                item = session(tool="codex", session_id="019f-thread")
                self.assertEqual(publish_name(item, "my-name"), "")
            written = json.loads((home / "session_index.jsonl").read_text().splitlines()[-1])
            self.assertEqual(written["id"], "019f-thread")
            self.assertEqual(written["thread_name"], "my-name")
            self.assertTrue(written["updated_at"].endswith("Z"))

    def test_reset_restores_a_provider_name(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text("", encoding="utf-8")
            item = session(
                session_id="abc",
                storage=str(transcript),
                original_named=True,
                original_title="original",
            )
            self.assertEqual(publish_name(item, ""), "")
            last = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(last["customTitle"], "original")

    def test_reset_without_a_provider_name_is_reported_not_invented(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text("", encoding="utf-8")
            item = session(storage=str(transcript), original_named=False, original_title="auto")
            self.assertIn("cannot be cleared", publish_name(item, ""))
            self.assertEqual(transcript.read_text(encoding="utf-8"), "")

    def test_original_name_survives_provider_reload_before_reset(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "abc.jsonl"
            transcript.write_text('{"type":"ai-title","aiTitle":"Original"}\n', encoding="utf-8")
            state_path = root / "state.json"
            state = UserState(state_path)
            first = session(
                session_id="abc",
                storage=str(transcript),
                original_named=True,
                original_title="Original",
            )

            self.assertEqual(rename_session(state, first, "Renamed"), "")

            # A fresh provider scan now sees its own newly appended title. The
            # v3 state must still know what existed before that first rename.
            reloaded = session(
                session_id="abc",
                title="Renamed",
                storage=str(transcript),
                original_named=True,
                original_title="Renamed",
            )
            state = UserState(state_path)
            state.apply([reloaded])
            self.assertEqual(reloaded.original_title, "Original")
            self.assertEqual(reloaded.title, "Renamed")

            self.assertEqual(rename_session(state, reloaded, ""), "")
            last = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(last["customTitle"], "Original")

            after_reset = session(
                session_id="abc",
                title="Original",
                storage=str(transcript),
                original_named=True,
                original_title="Original",
            )
            state = UserState(state_path)
            state.apply([after_reset])
            self.assertEqual(after_reset.title, "Original")
            self.assertFalse(after_reset.renamed)


class UserStateLaunchToolTests(unittest.TestCase):
    def test_launch_tool_preference_is_applied_to_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "names": {},
                        "original_names": {},
                        "hidden": [],
                        "launch_tools": {
                            "claude:ab": "codex",
                            "codex:cd": "claude",
                            "codex:invalid": "all",
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = UserState(state_path)
            items = [
                session(
                    tool="claude",
                    session_id="ab",
                    launch_targets={"claude": "ab", "codex": "x1"},
                ),
                session(
                    tool="codex",
                    session_id="cd",
                    launch_targets={"codex": "cd", "claude": "c1"},
                ),
                session(
                    tool="codex",
                    session_id="invalid",
                    launch_targets={"codex": "cd"},
                ),
            ]
            state.apply(items)
            self.assertEqual(items[0].active_launch_tool, "codex")
            self.assertEqual(items[1].active_launch_tool, "claude")
            self.assertEqual(items[2].active_launch_tool, "codex")

    def test_launch_tool_preference_round_trips(self) -> None:
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = UserState(state_path)
            item = session(
                tool="claude",
                session_id="round-trip",
                launch_targets={"claude": "ab", "codex": "cd"},
            )
            state.set_launch_tool(item, "codex")
            state = UserState(state_path)
            reloaded = session(
                tool="claude",
                session_id="round-trip",
                launch_targets={"claude": "ab", "codex": "cd"},
            )
            state.apply([reloaded])
            self.assertEqual(reloaded.launch_tool, "codex")

            state.set_launch_tool(reloaded, "claude")
            state = UserState(state_path)
            fallback = session(
                tool="claude",
                session_id="round-trip",
                launch_targets={"claude": "ab", "codex": "cd"},
            )
            state.apply([fallback])
            self.assertEqual(fallback.launch_tool, "")
            self.assertEqual(fallback.active_launch_tool, "claude")


class AgentTagTests(unittest.TestCase):
    def test_subagent_shows_nickname_and_parent(self) -> None:
        item = session(source="subagent", agent_nickname="Bohr", parent_id="019f59af-e300-7bd1")
        self.assertEqual(agent_tag(item), "[Bohr<019f59af] ")

    def test_siblings_render_distinctly(self) -> None:
        parent = "019f59af-e300-7bd1"
        first = session(source="subagent", agent_nickname="Bohr", parent_id=parent, named=False)
        second = session(source="subagent", agent_nickname="Hypatia", parent_id=parent, named=False)
        self.assertNotEqual(app.display_list_title(first), app.display_list_title(second))

    def test_subagent_without_nickname_falls_back(self) -> None:
        item = session(source="subagent", parent_id="019f59af-e300")
        self.assertEqual(agent_tag(item), "[sub<019f59af] ")

    def test_non_interactive_is_marked_exec(self) -> None:
        self.assertEqual(agent_tag(session(source="non-interactive")), "[exec] ")

    def test_interactive_sessions_are_untagged(self) -> None:
        self.assertEqual(agent_tag(session()), "")
        self.assertEqual(app.display_list_title(session(title="Plain")), "Plain")


if __name__ == "__main__":
    unittest.main()
