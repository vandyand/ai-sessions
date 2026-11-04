import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
from ai_sessions.conversion import BridgeError
from ai_sessions.harnesses import codex as codex_harness
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


def last_title(transcript: Path) -> dict:
    """The last title record a rename appended to a Claude transcript."""
    records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    return [record for record in records if record.get("type") == "custom-title"][-1]


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

    def test_provider_bridge_error_still_commits_authoritative_local_name(self) -> None:
        with TemporaryDirectory() as directory:
            state = UserState(Path(directory) / "state.json")
            item = session(tool="codex", session_id="019f-thread", title="Before")

            def fail(_item: object, _name: str) -> str:
                raise BridgeError("provider database is busy")

            adapter = replace(REGISTRY.get("codex"), publish_name=fail)
            with REGISTRY.temporary(adapter):
                note = rename_session(state, item, "After")
            reloaded = session(tool="codex", session_id="019f-thread", title="Before")
            state.apply([reloaded])
        self.assertIn("provider database is busy", note)
        self.assertEqual(reloaded.title, "After")
        self.assertTrue(reloaded.renamed)

    def test_rename_reaches_every_copy_of_the_conversation(self) -> None:
        published: list[tuple[str, str, str]] = []

        def recorder(item: Session, name: str) -> str:
            published.append((item.tool, item.session_id, name))
            return ""

        with TemporaryDirectory() as directory:
            state = UserState(Path(directory) / "state.json")
            original = session(tool="claude", session_id="claude-copy", title="work")
            copy = session(tool="codex", session_id="codex-copy", title="work (from Claude)")
            other = session(tool="codex", session_id="unrelated", title="other")
            state.conversations["conv"] = {"members": {original.key: {}, copy.key: {}}}
            state.session_conversations = {
                state.stable_key(original.key): "conv",
                state.stable_key(copy.key): "conv",
            }
            state._sessions_by_key = {item.key: item for item in (original, copy, other)}
            with (
                REGISTRY.temporary(replace(REGISTRY.get("claude"), publish_name=recorder)),
                REGISTRY.temporary(replace(REGISTRY.get("codex"), publish_name=recorder)),
            ):
                self.assertEqual(rename_session(state, copy, "minecraft"), "")
            self.assertEqual(
                published,
                [("codex", "codex-copy", "minecraft"), ("claude", "claude-copy", "minecraft")],
            )
            self.assertEqual(state.names[state.stable_key(original.key)], "minecraft")
            self.assertNotIn(state.stable_key(other.key), state.names)

    def tidy(self, item: Session) -> list[str]:
        published: list[str] = []

        def record(target: Session, name: str) -> str:
            published.append(name)
            target.provider_title = name
            return ""

        with patch.object(app, "publish_name", record):
            app.tidy_launched_title(item)
        return published

    def test_launch_drops_old_bridge_suffixes(self) -> None:
        stacked = "TOP-Assistant (from Codex) (from Claude) (from Codex)"
        item = session(tool="codex", title=stacked, provider_title=stacked)
        self.assertEqual(self.tidy(item), ["TOP-Assistant"])
        self.assertEqual(item.title, "TOP-Assistant")
        single = "open-phone (from Claude)"
        self.assertEqual(
            self.tidy(session(tool="codex", title=single, provider_title=single)), ["open-phone"]
        )
        plain = session(tool="codex", title="plain", provider_title="plain")
        self.assertEqual(self.tidy(plain), [])

    def test_launch_never_pushes_a_name_over_the_harness(self) -> None:
        # A name chosen in the harness stands, whatever this list last showed.
        renamed = session(tool="codex", title="old list name", provider_title="named in Codex")
        renamed.renamed = True
        self.assertEqual(self.tidy(renamed), [])
        supervisor = session(tool="codex", title="Supervisor: x", provider_title="my own name")
        supervisor.supervises = "record"
        self.assertEqual(self.tidy(supervisor), [])
        unknown = session(title="a (from Codex) (from Claude)")
        self.assertEqual(self.tidy(unknown), [])

    def test_a_rename_made_in_the_harness_wins_over_an_earlier_one_made_here(self) -> None:
        with TemporaryDirectory() as directory:
            state = UserState(Path(directory) / "state.json")
            item = session(tool="codex", title="generated", provider_title="generated")
            with REGISTRY.temporary(
                replace(REGISTRY.get("codex"), publish_name=lambda _item, _name: "")
            ):
                rename_session(state, item, "named here")
            state.apply([item])
            self.assertEqual(item.title, "named here")
            # Later renamed with /rename inside Codex; discovery reports the new name.
            reloaded = session(
                tool="codex", title="named in Codex", provider_title="named in Codex"
            )
            UserState(Path(directory) / "state.json").apply([reloaded])
            self.assertEqual(reloaded.title, "named in Codex")
            self.assertFalse(reloaded.renamed)

    def test_a_name_the_harness_could_not_record_is_kept_here(self) -> None:
        with TemporaryDirectory() as directory:
            state = UserState(Path(directory) / "state.json")
            item = session(tool="codex", title="generated", provider_title="generated")
            with REGISTRY.temporary(
                replace(REGISTRY.get("codex"), publish_name=lambda _item, _name: "compressed")
            ):
                rename_session(state, item, "kept locally")
            reloaded = session(tool="codex", title="generated", provider_title="generated")
            UserState(Path(directory) / "state.json").apply([reloaded])
            self.assertEqual(reloaded.title, "kept locally")

    def test_claude_rename_appends_custom_title(self) -> None:
        with TemporaryDirectory() as directory:
            transcript = Path(directory) / "abc.jsonl"
            transcript.write_text('{"type":"ai-title","aiTitle":"Auto"}\n', encoding="utf-8")
            item = session(session_id="abc", storage=str(transcript))
            self.assertEqual(publish_name(item, "my-name"), "")
            lines = transcript.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [json.loads(line) for line in lines[-2:]],
                [
                    {"type": "custom-title", "customTitle": "my-name", "sessionId": "abc"},
                    {"type": "agent-name", "agentName": "my-name", "sessionId": "abc"},
                ],
            )

    def test_claude_rename_reports_missing_transcript(self) -> None:
        item = session(storage="/nonexistent/path.jsonl")
        self.assertIn("missing", publish_name(item, "my-name"))

    def test_codex_rename_goes_through_codex(self) -> None:
        # A paginated thread's name lives only in Codex's database, so the
        # rename must be Codex's own thread/name/set, not an index append.
        calls: list[tuple[Path, str, str]] = []
        with TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                REGISTRY.temporary(replace(REGISTRY.get("codex"), home=home)),
                patch.object(
                    codex_harness,
                    "_thread_set_name",
                    lambda *args: calls.append(args) or "",
                ),
            ):
                item = session(tool="codex", session_id="019f-thread")
                self.assertEqual(publish_name(item, "my-name"), "")
            self.assertEqual(calls, [(home, "019f-thread", "my-name")])
            self.assertFalse((home / "session_index.jsonl").exists())

    def test_codex_rename_falls_back_to_the_index_and_says_so(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                REGISTRY.temporary(replace(REGISTRY.get("codex"), home=home)),
                patch.object(
                    codex_harness, "_thread_set_name", lambda *_: "'codex' is not on PATH"
                ),
            ):
                item = session(tool="codex", session_id="019f-thread")
                note = publish_name(item, "my-name")
            self.assertIn("not on PATH", note)
            self.assertIn("legacy format", note)
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
            last = last_title(transcript)
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
            last = last_title(transcript)
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

            published = last_title(transcript)
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
            last = last_title(transcript)
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
