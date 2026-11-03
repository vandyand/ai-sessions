import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_bridge as tb  # noqa: E402

from ai_sessions.app import UserState, prepare_launch
from ai_sessions.supervisor import store
from ai_sessions.supervisor.store import HookState


def _linked_state(directory: str) -> tuple[UserState, object, object, str]:
    source = tb.session(directory, launch_tool="claude")
    state = UserState(path=Path(directory) / "state.json")
    with tb.harness_home("claude", Path(directory) / "claude"):
        prepared, _ = prepare_launch(source, state=state)
    copy = tb.LaunchIntegrationTests.claude_copy(
        source, prepared, state.bridges[source.key]["claude"]["storage"]
    )
    return state, source, copy, state.conversation_id_for(source)


class StateTests(unittest.TestCase):
    def test_state_v6_loads_and_saves_as_v7_with_empty_supervisors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"version": 6, "names": {"codex:a": "Alpha"}}), encoding="utf-8"
            )
            state = UserState(path=path)
            self.assertEqual(state.names, {"codex:a": "Alpha"})
            self.assertEqual(state.supervisors, {})
            state.save()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 7)
            self.assertEqual(saved["supervisors"], {})
            self.assertEqual(UserState(path=path).names, {"codex:a": "Alpha"})

    def test_supervisor_record_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = UserState(path=path)
            record = {
                "target_conversation": "conv-1",
                "tool": "claude",
                "created": 12.5,
                "title": "T · supervisor",
                "archived": False,
            }
            state.supervisors["rec-1"] = record
            state.save()
            loaded = UserState(path=path)
            self.assertEqual(loaded.supervisors, {"rec-1": record})
            self.assertEqual(loaded.supervisor_for_conversation("conv-1"), "rec-1")
            self.assertIsNone(loaded.supervisor_for_conversation("other"))

    def test_conversation_head_ok_single_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, source, copy, conversation_id = _linked_state(directory)
            state.apply([source, copy])
            before = json.dumps(state.conversations, sort_keys=True)
            status, heads = state.conversation_head(conversation_id)
            self.assertEqual(status, "ok")
            self.assertTrue(heads)
            self.assertEqual(json.dumps(state.conversations, sort_keys=True), before)
            self.assertEqual(state.conversation_head("missing"), ("unknown", []))

    def test_conversation_head_diverged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, source, copy, conversation_id = _linked_state(directory)
            with Path(source.storage).open("a", encoding="utf-8") as handle:
                handle.write(tb.codex_line("user", "Codex branch") + "\n")
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(tb.claude_line("user", "Claude branch") + "\n")
            state.apply([source, copy])
            status, heads = state.conversation_head(conversation_id)
            self.assertEqual(status, "diverged")
            self.assertEqual(len(heads), 2)


class HookStateTests(unittest.TestCase):
    def test_hook_state_atomic_save_and_find_by_native_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(store.load_hook_state("r1", root))
            first = HookState("r1", native_session_id="native-1", checkpoint=42, member="m")
            second = HookState("r2", native_session_id="native-2", checkpoint="abc")
            store.save_hook_state(first, root)
            store.save_hook_state(second, root)
            self.assertEqual(store.load_hook_state("r1", root), first)
            self.assertEqual(store.load_hook_state("r2", root), second)
            self.assertEqual(store.find_by_native_id("native-2", root), second)
            self.assertIsNone(store.find_by_native_id("nope", root))
            self.assertEqual(sorted(p.name for p in (root / "r1").iterdir()), ["record.json"])

    def test_concurrent_record_writes_never_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store.save_hook_state(HookState("r1"), root)
            errors: list[str] = []

            def writer(tag: int) -> None:
                for index in range(200):
                    store.save_hook_state(HookState("r1", briefs=tag * 1000 + index), root)
                    if store.load_hook_state("r1", root) is None:
                        errors.append("unparseable")

            threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIsNotNone(store.load_hook_state("r1", root))


if __name__ == "__main__":
    unittest.main()
