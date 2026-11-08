import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ai_sessions.references import SlugStore
from ai_sessions.registry import REGISTRY
from ai_sessions.supervisor.create import SupervisorError, _resolve_record
from ai_sessions.supervisor.store import HookState, save_hook_state


class TargetLogReferenceTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.tool = REGISTRY.adapters()[0].name
        self.enterContext(
            REGISTRY.temporary(replace(REGISTRY.get(self.tool), home=self.root / "home"))
        )
        self.state = SimpleNamespace(
            supervisors={"record": {"target_conversation": "conversation", "tool": self.tool}},
            session_conversations={self.tool + ":target": "conversation"},
            conversations={},
            slugs=SlugStore(self.root / "slugs.sqlite3"),
        )
        save_hook_state(HookState(record_id="record", native_session_id="supervisor"), self.root)

    def test_supervisor_native_id_and_qualified_id(self):
        for ref in (
            "supervisor",
            self.tool + ":supervisor",
            "target",
            self.tool + ":target",
            "record",
            "conversation",
        ):
            with self.subTest(ref=ref):
                self.assertEqual(_resolve_record(self.state, ref, root=self.root), "record")

    def test_slug_lookup_and_home_guard(self):
        from ai_sessions.references import native_key

        key = native_key(self.tool, "target")
        self.state.slugs.ensure([(key, "target-slug")])
        self.assertEqual(_resolve_record(self.state, "target-slug", root=self.root), "record")
        self.assertEqual(
            _resolve_record(self.state, self.tool + ":target-slug", root=self.root), "record"
        )
        self.state.slugs = SlugStore(self.root / "other-slugs.sqlite3")
        self.state.slugs.ensure(
            [(native_key(self.tool, "target", self.root / "other"), "target-slug")]
        )
        with self.assertRaises(SupervisorError):
            _resolve_record(self.state, "target-slug", root=self.root)

    def test_duplicate_supervisors_fail_closed(self):
        self.state.supervisors["other"] = dict(self.state.supervisors["record"])
        with self.assertRaises(SupervisorError):
            _resolve_record(self.state, "target", root=self.root)

    def test_supervisor_slug(self):
        from ai_sessions.references import native_key

        self.state.slugs.ensure([(native_key(self.tool, "supervisor"), "supervisor-slug")])
        self.assertEqual(_resolve_record(self.state, "supervisor-slug", root=self.root), "record")

    def test_duplicate_native_ids_across_tools_fail_closed(self):
        self.state.session_conversations["fixture-other:target"] = "another"
        with self.assertRaises(SupervisorError):
            _resolve_record(self.state, "target", root=self.root)
        self.assertEqual(
            _resolve_record(self.state, self.tool + ":target", root=self.root), "record"
        )

    def test_duplicate_native_ids_even_qualified_fail_closed(self):
        self.state.supervisors["other"] = dict(self.state.supervisors["record"])
        save_hook_state(HookState(record_id="other", native_session_id="supervisor"), self.root)
        with self.assertRaises(SupervisorError):
            _resolve_record(self.state, self.tool + ":supervisor", root=self.root)

    def test_legacy_record_without_tool_still_resolves(self):
        self.state.supervisors["record"].pop("tool")
        self.assertEqual(_resolve_record(self.state, "record", root=self.root), "record")
        self.assertEqual(_resolve_record(self.state, "target", root=self.root), "record")
        with self.assertRaises(SupervisorError):
            _resolve_record(self.state, "supervisor", root=self.root)

    def test_native_id_cannot_mask_another_sessions_registered_slug(self):
        from ai_sessions.references import SlugStore, native_key

        self.state.slugs = SlugStore(self.root / "slugs.sqlite3")
        self.state.slugs.ensure([(native_key(self.tool, "target"), "collision")])
        self.state.session_conversations[self.tool + ":collision"] = "other-conversation"
        self.state.supervisors["other"] = {
            "tool": self.tool,
            "target_conversation": "other-conversation",
        }
        for reference in ("collision", self.tool + ":collision"):
            with self.subTest(reference=reference), self.assertRaises(SupervisorError):
                _resolve_record(self.state, reference, root=self.root)
