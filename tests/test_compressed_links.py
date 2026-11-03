import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app, conversion
from ai_sessions.app import Session, UserState, can_bridge, prepare_launch
from ai_sessions.compression import compress_file, decompress_file, is_compressed_path
from ai_sessions.conversion import BridgeError
from ai_sessions.harnesses import claude as claude_harness
from ai_sessions.model import Availability
from ai_sessions.registry import REGISTRY
from ai_sessions.supervisor import context, store
from ai_sessions.supervisor.create import SupervisorError, create_supervisor
from ai_sessions.supervisor.store import HookState


def probe(ref):
    from ai_sessions.compression import resolve_storage

    found = resolve_storage(Path(ref.storage))
    if found is None:
        return Availability.UNAVAILABLE
    return Availability.COMPRESSED if is_compressed_path(found) else Availability.AVAILABLE


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("codex", "claude"):
            scope = REGISTRY.temporary(replace(REGISTRY.get(name), availability=probe))
            scope.__enter__()
            self.addCleanup(scope.__exit__, None, None, None)
        self.state = UserState(self.root / "state.json")
        self.codex_file = self.file("codex.jsonl")
        self.claude_file = self.file("claude.jsonl")
        self.codex = self.session("codex", "c1", self.codex_file)
        self.claude = self.session("claude", "c2", self.claude_file)

    def file(self, name: str) -> Path:
        path = self.root / name
        path.write_text('{"a":1}\n', encoding="utf-8")
        return path

    def session(self, tool: str, session_id: str, path: Path) -> Session:
        return Session(tool, session_id, "T", "/w", 2.0, 1.0, "p", True, str(path))

    def link(self, *, codex_generation: int = 0, claude_generation: int = 0, same=True) -> str:
        members = {}
        for item, generation, frontier in (
            (self.codex, codex_generation, "f1"),
            (self.claude, claude_generation, "f1" if same else "f2"),
        ):
            members[item.key] = {
                "tool": item.tool,
                "session_id": item.session_id,
                "storage": item.storage,
                "generation": generation,
                "frontier": frontier,
                "checkpoint": 8,
            }
        self.state.conversations["conv"] = {"members": members}
        self.state.session_conversations.update(dict.fromkeys(members, "conv"))
        return "conv"

    def compress(self, path: Path, item: Session) -> None:
        item.storage = str(compress_file(path))
        item.compressed = True


class ConversationTests(Fixture):
    def status(self) -> dict:
        return self.state._conversation_status("conv")

    def changed(self, member, availability=None):
        return "unchanged"

    def test_compressed_newest_member_is_compressed_not_missing(self) -> None:
        self.link(claude_generation=1, same=False)
        self.compress(self.claude_file, self.claude)
        with patch.object(UserState, "_member_change_status", self.changed):
            status = self.status()
            self.assertEqual([key for key, _ in status["compressed"]], [self.claude.key])
            self.assertEqual(status["unavailable"], [])
            self.assertEqual(self.state.conversation_head("conv")[0], "compressed")

    def test_blocker_marks_the_compressed_head_as_the_head(self) -> None:
        self.link(claude_generation=1, same=False)
        self.compress(self.claude_file, self.claude)
        with patch.object(UserState, "_member_change_status", self.changed):
            self.state._apply_conversation_status([self.codex, self.claude])
        self.assertEqual(self.claude.conversation_blocker, "compressed")
        self.assertFalse(self.claude.superseded)
        self.assertTrue(self.codex.superseded)
        self.assertIs(app._tracked_target((self.codex, self.claude)), self.claude)
        self.assertEqual(app._tracked_status((self.codex, self.claude), self.claude), "compressed")

    def test_older_compressed_member_does_not_block(self) -> None:
        self.link(claude_generation=1, same=False)
        self.compress(self.codex_file, self.codex)
        with patch.object(UserState, "_member_change_status", self.changed):
            status = self.status()
            self.assertEqual(status["compressed"] if "compressed" in status else [], [])
            self.assertEqual(self.state.conversation_head("conv")[0], "ok")

    def test_equivalent_compressed_copy_does_not_block(self) -> None:
        self.link()
        self.compress(self.codex_file, self.codex)
        with patch.object(UserState, "_member_change_status", self.changed):
            self.assertEqual(self.state.conversation_head("conv")[0], "ok")

    def test_resolve_refuses_a_compressed_head(self) -> None:
        self.link(claude_generation=1, same=False)
        self.compress(self.claude_file, self.claude)
        with (
            patch.object(UserState, "_member_change_status", self.changed),
            self.assertRaisesRegex(BridgeError, "compressed"),
        ):
            self.state.resolve_launch(self.claude, "claude")

    def test_round_trip_restores_the_same_status_and_bytes(self) -> None:
        self.link(claude_generation=1, same=False)
        with patch.object(UserState, "_member_change_status", self.changed):
            before = self.state.conversation_head("conv")[0]
            size = self.claude_file.stat().st_size
            mtime = self.claude_file.stat().st_mtime_ns
            self.compress(self.claude_file, self.claude)
            decompress_file(Path(self.claude.storage))
            self.assertEqual(self.state.conversation_head("conv")[0], before)
        self.assertEqual(self.claude_file.stat().st_size, size)
        self.assertEqual(self.claude_file.stat().st_mtime_ns, mtime)


class TranspileTests(Fixture):
    def test_no_bridge_is_offered_for_a_compressed_session(self) -> None:
        self.assertTrue(can_bridge(self.codex))
        self.compress(self.codex_file, self.codex)
        self.assertFalse(can_bridge(self.codex))
        self.assertEqual(app.available_launch_tools(self.codex), ("codex",))

    def test_bridge_is_refused_and_nothing_is_written(self) -> None:
        self.compress(self.codex_file, self.codex)
        self.codex.launch_tool = "claude"
        with (
            patch.object(conversion, "bridge") as bridge,
            self.assertRaisesRegex(BridgeError, "compressed"),
        ):
            prepare_launch(self.codex)
        bridge.assert_not_called()

    def test_native_launch_is_refused_too(self) -> None:
        self.compress(self.codex_file, self.codex)
        with self.assertRaisesRegex(BridgeError, "decompress"):
            prepare_launch(self.codex, state=self.state)

    def test_read_is_refused_at_the_shared_gate(self) -> None:
        self.compress(self.codex_file, self.codex)
        ref = app.NativeRef(self.codex.session_id, str(self.codex_file))
        with self.assertRaisesRegex(BridgeError, "compressed"):
            conversion.read_snapshot("codex", ref)

    def test_a_compressed_recorded_copy_blocks_a_duplicate(self) -> None:
        self.compress(self.claude_file, self.claude)
        self.state.bridges[self.state.stable_key(self.codex.key)] = {
            "claude": {
                "session_id": self.claude.session_id,
                "storage": str(self.claude_file),
                "source_updated": 2.0,
            }
        }
        with self.assertRaisesRegex(BridgeError, "compressed"):
            self.state.bridge_for(self.codex, "claude")


class WriteTests(Fixture):
    def test_rename_never_touches_a_compressed_transcript(self) -> None:
        self.compress(self.claude_file, self.claude)
        path = Path(self.claude.storage)
        before = path.read_bytes()
        note = claude_harness.publish_name(self.claude, "New name")
        self.assertIn("local", note)
        self.assertEqual(path.read_bytes(), before)


class SupervisorTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(store, "SUPERVISORS_DIR", self.root / "supervisors")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creating_a_supervisor_is_refused(self) -> None:
        self.compress(self.codex_file, self.codex)
        with self.assertRaisesRegex(SupervisorError, "compressed"):
            create_supervisor(self.state, self.codex, "claude", save=False)
        self.assertEqual(self.state.supervisors, {})

    def test_compressed_target_is_reported_and_the_checkpoint_kept(self) -> None:
        self.link(claude_generation=1, same=False)
        self.state.supervisors["r1"] = {"target_conversation": "conv"}
        self.compress(self.claude_file, self.claude)
        hook = HookState("r1", checkpoint=5, last_seq_sent=3, brief_due=False)
        with patch.object(UserState, "_member_change_status", lambda *a, **k: "unchanged"):
            text, after = context._prepare("r1", self.state, hook, None)
        self.assertIn("compressed", text)
        self.assertIn("decompress", text)
        self.assertEqual(after.checkpoint, 5)
        self.assertEqual(after.last_seq_sent, 3)


if __name__ == "__main__":
    unittest.main()
