import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions import app
from ai_sessions.app import (
    Session,
    UserState,
    active_launch_tool,
    agent_tag,
    append_jsonl,
    publish_name,
    rename_session,
)
from ai_sessions.capabilities import Unsupported
from ai_sessions.registry import REGISTRY


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
    def test_registered_adapter_owns_title_publication(self) -> None:
        calls: list[tuple[str, str]] = []
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="other",
            label="Other",
            short_label="Other",
            publish_name=lambda item, name: (
                calls.append((item.session_id, name)) or "provider-note"
            ),
        )
        with REGISTRY.temporary(fake):
            self.assertEqual(publish_name(session(tool="other"), "new"), "provider-note")
        self.assertEqual(calls, [("session-id", "new")])

    def test_unsupported_title_publication_stays_local(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            base = REGISTRY.get("codex")
            fake = replace(
                base,
                name="limited",
                label="Limited",
                short_label="Limited",
                home=home,
                publish_name=Unsupported("native titles are unavailable"),
            )
            with REGISTRY.temporary(fake):
                note = publish_name(session(tool="limited"), "new")
            self.assertIn("cannot publish titles", note)
            self.assertIn("kept local", note)
            self.assertEqual(list(home.iterdir()), [])

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
            with REGISTRY.temporary(replace(REGISTRY.get("codex"), home=home)):
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

    def test_reset_republishes_a_generated_title(self) -> None:
        """A published title cannot be withdrawn, so reset must supersede it."""
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text("", encoding="utf-8")
            item = session(
                session_id="abc",
                storage=str(transcript),
                original_named=False,
                original_title="Auto Generated",
            )
            note = publish_name(item, "")
            self.assertIn("explicit title", note)
            last = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(last["customTitle"], "Auto Generated")

    def test_reset_does_not_mask_provider_publication_failure(self) -> None:
        item = session(
            session_id="abc",
            storage="/nonexistent/transcript.jsonl",
            original_named=False,
            original_title="Auto Generated",
        )
        note = publish_name(item, "")
        self.assertIn("missing", note)
        self.assertNotIn("now records it", note)

    def test_reset_without_any_earlier_title_still_refuses(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text("", encoding="utf-8")
            item = session(storage=str(transcript), original_named=False, original_title="")
            self.assertIn("no earlier title", publish_name(item, ""))
            self.assertEqual(transcript.read_text(encoding="utf-8"), "")

    def test_reset_of_a_provider_named_session_is_silent(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text("", encoding="utf-8")
            item = session(
                session_id="abc",
                storage=str(transcript),
                original_named=True,
                original_title="Chosen",
            )
            self.assertEqual(publish_name(item, ""), "")

    def test_reset_leaves_provider_and_utility_agreeing(self) -> None:
        """The end-to-end case that previously diverged: auto-titled session."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "abc.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Explore Seeking Alpha",
                        "sessionId": "abc",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            state = UserState(root / "state.json")

            def make(title: str, named: bool, orig_title: str, orig_named: bool) -> Session:
                return session(
                    session_id="abc",
                    title=title,
                    named=named,
                    storage=str(transcript),
                    original_title=orig_title,
                    original_named=orig_named,
                )

            first = make("Explore Seeking Alpha", False, "Explore Seeking Alpha", False)
            self.assertEqual(rename_session(state, first, "my-name"), "")

            # The provider now reports its own published title on rescan.
            reloaded = make("my-name", True, "my-name", True)
            state = UserState(root / "state.json")
            state.apply([reloaded])
            self.assertIn("explicit title", rename_session(state, reloaded, ""))

            published = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(published["customTitle"], "Explore Seeking Alpha")

            after = make("Explore Seeking Alpha", True, "Explore Seeking Alpha", True)
            state = UserState(root / "state.json")
            state.apply([after])
            # Utility and provider now show the same title again.
            self.assertEqual(after.title, "Explore Seeking Alpha")
            self.assertEqual(published["customTitle"], after.title)
            self.assertFalse(after.renamed)

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
            self.assertEqual(active_launch_tool(items[0]), "codex")
            self.assertEqual(active_launch_tool(items[1]), "claude")
            self.assertEqual(active_launch_tool(items[2]), "codex")

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
            self.assertEqual(active_launch_tool(fallback), "claude")


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
