import json
import unittest
from dataclasses import replace

from ai_sessions.app import Session
from ai_sessions.harnesses import install
from ai_sessions.model import SourceKind
from ai_sessions.registry import REGISTRY, Registry


class RegistryTests(unittest.TestCase):
    def test_builtins_are_ordered_and_install_is_idempotent(self) -> None:
        before = REGISTRY.generation
        install()
        install()
        self.assertEqual(REGISTRY.names(), ("codex", "claude"))
        self.assertEqual(REGISTRY.generation, before)

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
        self.assertNotIn("late", row.available_launch_tools)
        with REGISTRY.temporary(fake):
            self.assertIn("late", row.available_launch_tools)
        self.assertNotIn("late", row.available_launch_tools)

    def test_source_kind_is_string_json_serializable(self) -> None:
        self.assertEqual(SourceKind.NON_INTERACTIVE, "non-interactive")
        self.assertEqual(
            json.loads(json.dumps({"source": SourceKind.SDK})),
            {"source": "sdk"},
        )


if __name__ == "__main__":
    unittest.main()
