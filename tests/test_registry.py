import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from ai_sessions import bridge
from ai_sessions.app import (
    Browser,
    Session,
    UserState,
    available_launch_tools,
    build_parser,
    filtered_sessions,
    list_output,
)
from ai_sessions.capabilities import Unsupported
from ai_sessions.harnesses import install
from ai_sessions.model import NativeRef, SourceKind
from ai_sessions.registry import REGISTRY, Registry


class RegistryTests(unittest.TestCase):
    def test_builtins_are_ordered_and_install_is_idempotent(self) -> None:
        before = REGISTRY.generation
        install()
        install()
        self.assertEqual(REGISTRY.names(), ("codex", "claude"))
        self.assertEqual(REGISTRY.generation, before)

    def test_equal_time_sort_is_independent_of_discovery_order(self) -> None:
        def row(tool: str, session_id: str) -> Session:
            return Session(
                tool=tool,
                session_id=session_id,
                title="same",
                cwd="/same",
                updated=10,
                created=10,
                preview="",
                named=True,
                storage="",
            )

        rows = [row("claude", "b"), row("codex", "a"), row("claude", "a")]
        forward = [item.key for item in filtered_sessions(rows, sort_mode="recent")]
        backward = [item.key for item in filtered_sessions(reversed(rows), sort_mode="recent")]
        self.assertEqual(forward, backward)

    def test_duplicate_and_ambiguous_names_are_rejected(self) -> None:
        registry = Registry()
        base = REGISTRY.get("codex")
        registry.register(base)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(base)
        for name in ("all", "Claude", "has space", "has.dot", "has:colon", ""):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "invalid harness name"):
                    registry.register(replace(base, name=name))

    def test_incomplete_adapter_metadata_is_rejected(self) -> None:
        base = REGISTRY.get("codex")
        mutations = (
            {"label": ""},
            {"short_label": "x" * 17},
            {"default_command": ()},
            {"source_kinds": frozenset()},
            {"id_patterns": ()},
            {"id_patterns": ("not compiled",)},
            {"scratch_patterns": [re.compile("scratch")]},
            {"scratch_patterns": (re.compile(b"scratch"),)},
            {"read": "not callable"},
            {"change_status": Unsupported("")},
            {"budget": None},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                replace(base, **mutation)
        with self.assertRaisesRegex(ValueError, "budget must be a BudgetPolicy"):
            replace(base, budget=Unsupported("budget policy is unavailable"))

    def test_every_mutation_advances_generation_monotonically(self) -> None:
        registry = Registry()
        base = REGISTRY.get("codex")
        start = registry.generation
        registry.register(base)
        self.assertEqual(registry.generation, start + 1)
        registry.register(replace(base, label="Replacement"), replace=True)
        self.assertEqual(registry.generation, start + 2)
        registry.unregister(base.name)
        self.assertEqual(registry.generation, start + 3)
        with registry.temporary(base):
            self.assertEqual(registry.generation, start + 4)
        self.assertEqual(registry.generation, start + 5)

    def test_late_registration_is_visible_after_app_import(self) -> None:
        base = REGISTRY.get("codex")
        fake = replace(base, name="late", label="Late Harness", short_label="Late", order=30)
        row = Session(
            tool="codex",
            session_id="session-1",
            title="title",
            cwd="/tmp",
            updated=0,
            created=0,
            preview="",
            named=False,
            storage="transcript.jsonl",
        )
        self.assertNotIn("late", available_launch_tools(row))
        with REGISTRY.temporary(fake):
            self.assertIn("late", available_launch_tools(row))
        self.assertNotIn("late", available_launch_tools(row))

    def test_incomplete_adapter_is_not_offered_as_a_bridge_target(self) -> None:
        base = REGISTRY.get("codex")
        limited = replace(
            base,
            name="limited",
            label="Limited Harness",
            availability=Unsupported("exact availability is unavailable"),
        )
        row = Session(
            tool="codex",
            session_id="session-1",
            title="title",
            cwd="/tmp",
            updated=0,
            created=0,
            preview="",
            named=False,
            storage="transcript.jsonl",
        )
        with REGISTRY.temporary(limited):
            self.assertNotIn("limited", available_launch_tools(row))

    def test_late_registration_is_visible_in_rendering_and_browser_styles(self) -> None:
        base = REGISTRY.get("codex")
        fake = replace(
            base,
            name="late",
            label="Late Harness",
            short_label="LateHarness12",
            order=30,
        )
        row = Session(
            tool="late",
            session_id="late-1",
            title="late title",
            cwd="/tmp",
            updated=0,
            created=0,
            preview="",
            named=True,
            storage="",
        )
        with REGISTRY.temporary(fake), redirect_stdout(io.StringIO()) as output:
            list_output([row])
            self.assertIn("LateHarness12", output.getvalue())
            self.assertIn("late", Browser._pair_layout())
            self.assertIn("late", build_parser()._option_string_actions["--tool"].choices)
        self.assertNotIn("late", Browser._pair_layout())

    def test_registry_replacement_changes_semantic_status_immediately(self) -> None:
        base = REGISTRY.get("codex")
        replacement = replace(base, change_status=lambda *_: "changed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            ref = NativeRef("snapshot", str(path))
            self.assertEqual(bridge.conversation_change_status("codex", ref, 0), "unchanged")
            with REGISTRY.temporary(replacement):
                self.assertEqual(bridge.conversation_change_status("codex", ref, 0), "changed")
            self.assertEqual(bridge.conversation_change_status("codex", ref, 0), "unchanged")

    def test_unknown_and_unsupported_change_status_are_explicit(self) -> None:
        base = REGISTRY.get("codex")
        unsupported = replace(base, name="limited", change_status=Unsupported("no tail reader"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            ref = NativeRef("snapshot", str(path))
            self.assertEqual(bridge.conversation_change_status("future", ref, 0), "unknown")
            with REGISTRY.temporary(unsupported):
                self.assertEqual(
                    bridge.conversation_change_status("limited", ref, 0), "unsupported"
                )

    def test_unknown_state_survives_and_blocks_head_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "future.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            state_path = root / "state.json"
            payload = {
                "version": 6,
                "names": {},
                "original_names": {},
                "hidden": [],
                "launch_tools": {"codex:known": "future"},
                "bridges": {
                    "codex:known": {
                        "future": {"session_id": "future-1", "storage": str(transcript)}
                    }
                },
                "conversations": {
                    "conversation": {
                        "members": {
                            "future:future-1": {
                                "tool": "future",
                                "session_id": "future-1",
                                "storage": str(transcript),
                                "generation": 1,
                                "frontier": "future-frontier",
                                "cursor": 0,
                            }
                        }
                    }
                },
                "session_conversations": {"future:future-1": "conversation"},
            }
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            state = UserState(state_path)
            self.assertEqual(
                state._conversation_status("conversation")["unknown"][0][0], "future:future-1"
            )
            state.save()
            reloaded = UserState(state_path)
            self.assertIn("future", reloaded.bridges["codex:known"])
            self.assertEqual(reloaded.launch_tools["codex:known"], "future")
            self.assertIn("future:future-1", reloaded.conversations["conversation"]["members"])

    def test_older_unknown_member_does_not_veto_newer_known_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown_path = root / "future.jsonl"
            known_path = root / "codex.jsonl"
            unknown_path.write_text("{}\n", encoding="utf-8")
            known_path.write_text("{}\n", encoding="utf-8")
            state = UserState(root / "missing-state.json")
            state.conversations = {
                "conversation": {
                    "members": {
                        "future:old": {
                            "tool": "future",
                            "session_id": "old",
                            "storage": str(unknown_path),
                            "generation": 1,
                            "frontier": "old",
                            "cursor": 0,
                        },
                        "codex:new": {
                            "tool": "codex",
                            "session_id": "new",
                            "storage": str(known_path),
                            "generation": 5,
                            "frontier": "new",
                            "cursor": 0,
                        },
                    }
                }
            }
            status = state._conversation_status("conversation")
        self.assertEqual(status["unknown"], [])
        self.assertEqual(status["heads"][0][0], "codex:new")

    def test_source_kind_is_string_json_serializable(self) -> None:
        self.assertEqual(SourceKind.NON_INTERACTIVE, "non-interactive")
        self.assertEqual(
            json.loads(json.dumps({"source": SourceKind.SDK})),
            {"source": "sdk"},
        )


if __name__ == "__main__":
    unittest.main()
