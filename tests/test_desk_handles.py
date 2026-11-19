import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from ai_sessions.desk import cli
from ai_sessions.model import Session


class DeskHandleTests(unittest.TestCase):
    def test_defaults_to_target_harness_and_reuses_existing_desk(self):
        item = Session(
            "codex", "ops", "Operational supervisor", ".", 0, 0, "", True, "fiction.jsonl"
        )
        item.handle = "elev-sup"
        with tempfile.TemporaryDirectory() as root:
            from ai_sessions.desk.store import Store

            store = Store(root)
            store.create(
                "existing",
                [
                    {
                        "tool": "codex",
                        "session_id": "ops",
                        "storage": "fiction.jsonl",
                        "role": "supervisor",
                    }
                ],
                root,
            )
            with (
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                patch("ai_sessions.desk.cli.Store", return_value=store),
                patch("ai_sessions.desk.launch.open_agent", return_value=0) as opened,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["elev-sup"]), 0)
            self.assertEqual(opened.call_args.args[1:3], ("existing", "codex"))

    def test_explicit_override_changes_human_conversation_not_target(self):
        item = Session("codex", "ops", "Supervisor", ".", 0, 0, "", True, "fiction.jsonl")
        item.handle = "elev-sup"
        with tempfile.TemporaryDirectory() as root:
            from ai_sessions.desk.store import Store

            store = Store(root)
            store.create(
                "desk",
                [
                    {
                        "tool": "codex",
                        "session_id": "ops",
                        "storage": "fiction.jsonl",
                        "role": "supervisor",
                    }
                ],
                root,
            )
            with (
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                patch("ai_sessions.desk.cli.Store", return_value=store),
                patch("ai_sessions.desk.launch.open_agent", return_value=0) as opened,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["elev-sup", "--claude"]), 0)
            self.assertEqual(opened.call_args.args[1:3], ("desk", "claude"))
            self.assertEqual(store.get("desk")["sources"][0]["tool"], "codex")


class DeskMembershipTests(unittest.TestCase):
    def test_linked_member_reuses_desk_and_preserves_conversation(self):
        from types import SimpleNamespace

        from ai_sessions.desk.store import Store
        from ai_sessions.handles import HandleStore
        from ai_sessions.references import native_key

        for target_tool, dry_run in (
            ("claude", False),
            ("claude", True),
            ("codex", False),
            ("codex", True),
        ):
            with (
                self.subTest(target_tool=target_tool, dry_run=dry_run),
                tempfile.TemporaryDirectory() as root,
            ):
                old = Session("codex", "old", "Supervisor", ".", 0, 0, "", True, "old.jsonl")
                new = Session(target_tool, "new", "Supervisor", ".", 1, 1, "", True, "new.jsonl")
                handles = HandleStore(root + "/handles.sqlite3")
                handles.link(old, new)
                team = {
                    "tool": "codex",
                    "session_id": "team",
                    "storage": "team.jsonl",
                    "role": "team",
                }
                store = Store(root + "/desk")
                store.create(
                    "original",
                    [
                        {
                            "tool": old.tool,
                            "session_id": old.session_id,
                            "storage": old.storage,
                            "home": native_key(old.tool, old.session_id).home,
                            "role": "supervisor",
                        },
                        team,
                    ],
                    root,
                )
                before = store.bind_agent("original", "claude", "human", "home")
                with (
                    patch(
                        "ai_sessions.handles_cli.catalog",
                        return_value=([new], SimpleNamespace(handles=handles)),
                    ),
                    patch("ai_sessions.references.resolve_session", return_value=new),
                    patch("ai_sessions.desk.cli.Store", return_value=store),
                    patch("ai_sessions.desk.launch.open_agent", return_value=0) as opened,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli.main([new.handle, *(["--dry-run"] if dry_run else [])]), 0)
                self.assertEqual(opened.call_args.args[1:3], ("original", new.tool))
                after = store.get("original")
                self.assertEqual(after["agents"], before["agents"])
                self.assertEqual(after["sources"][1:], [team])
                self.assertEqual(after["sources"][0]["session_id"], "old" if dry_run else "new")
                self.assertEqual(after["revision"], before["revision"] + (not dry_run))

    def test_primary_update_refuses_stale_revision(self):
        from ai_sessions.desk.store import ConflictError, Store

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            source = {"tool": "codex", "session_id": "old", "storage": "old.jsonl"}
            store.create("desk", [source], root)
            before = store.bind_agent("desk", "claude", "human", "home")
            with self.assertRaises(ConflictError):
                store.update_primary_source("desk", 0, dict(source, session_id="new"))
            self.assertEqual(store.get("desk"), before)

    def test_dry_run_new_desk_creates_no_files(self):
        from pathlib import Path

        item = Session("codex", "ops", "Supervisor", ".", 0, 0, "", True, "log.jsonl")
        item.handle = "elev-sup"
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "absent"
            with (
                patch.dict("os.environ", {"AI_SESSIONS_DESK_DIR": str(destination)}),
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main([item.handle, "--dry-run"]), 0)
            self.assertFalse(destination.exists())

    def test_same_title_does_not_reuse_unlinked_desk(self):
        from ai_sessions.desk.store import Store

        item = Session("codex", "new", "Same title", ".", 0, 0, "", True, "new.jsonl")
        item.handle = "new-handle"
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            before = store.create(
                "unrelated",
                [
                    {
                        "tool": "codex",
                        "session_id": "old",
                        "storage": "old.jsonl",
                        "title": item.title,
                    }
                ],
                root,
            )
            with (
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                patch("ai_sessions.desk.cli.Store", return_value=store),
                patch("ai_sessions.desk.launch.open_agent", return_value=0) as opened,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main([item.handle]), 0)
            self.assertEqual(opened.call_args.args[1], item.handle)
            self.assertEqual(store.get("unrelated"), before)

    def test_readonly_existing_store_leaves_database_unchanged(self):
        from pathlib import Path

        from ai_sessions.desk.store import Store

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.create("desk", [{"tool": "codex", "session_id": "old", "storage": "log"}], root)
            before = Path(store.path).read_bytes()
            readonly = Store(root, readonly=True)
            self.assertEqual(readonly.list(), store.list())
            self.assertEqual(Path(store.path).read_bytes(), before)


class DeskHomeBoundaryTests(unittest.TestCase):
    def test_home_switch_same_native_id_never_reuses_other_transcript(self):
        from pathlib import Path

        from ai_sessions.desk.store import Store
        from ai_sessions.references import NativeKey

        for explicit_home in (False, True):
            with self.subTest(explicit_home=explicit_home), tempfile.TemporaryDirectory() as root:
                home_a, home_b = Path(root) / "home-a", Path(root) / "home-b"
                item = Session(
                    "codex", "same-id", "Supervisor", ".", 0, 0, "", True, str(home_b / "log.jsonl")
                )
                item.handle = "new-home"
                source = {
                    "tool": item.tool,
                    "session_id": item.session_id,
                    "storage": str(home_a / "log.jsonl"),
                }
                if explicit_home:
                    source["home"] = str(home_a)
                store = Store(Path(root) / "desks")
                before = store.create("old-home", [source], root)

                def key(tool, ident, home=None):
                    return NativeKey(tool, ident, str(home or home_b))

                with (
                    patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                    patch("ai_sessions.references.resolve_session", return_value=item),
                    patch("ai_sessions.references.native_key", side_effect=key),
                    patch("ai_sessions.desk.cli.Store", return_value=store),
                    patch("ai_sessions.desk.launch.open_agent", return_value=0) as opened,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli.main([item.handle]), 0)
                self.assertEqual(opened.call_args.args[1], item.handle)
                self.assertEqual(store.get("old-home"), before)
                self.assertEqual(
                    store.get(item.handle)["sources"][0]["home"],
                    key(item.tool, item.session_id).home,
                )

    def test_store_preserves_source_home_on_create_and_update(self):
        from ai_sessions.desk.store import Store

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            source = {"tool": "codex", "session_id": "old", "storage": "log", "home": root}
            self.assertEqual(store.create("desk", [source], root)["sources"][0]["home"], root)
            updated = store.update_primary_source("desk", 0, dict(source, session_id="new"))
            self.assertEqual(updated["sources"][0]["home"], root)

    def test_legacy_link_requires_transcript_under_registered_member_home(self):
        from pathlib import Path

        from ai_sessions.references import NativeKey

        with tempfile.TemporaryDirectory() as root:
            member = NativeKey("codex", "old", str(Path(root) / "old-home"))
            target = Session("claude", "new", "Supervisor", ".", 0, 0, "", True, "new.jsonl")
            source = {
                "tool": member.tool,
                "session_id": member.session_id,
                "storage": str(Path(member.home) / "sessions" / "old.jsonl"),
            }
            self.assertTrue(cli.source_matches_target(source, target, {member}))
            source["storage"] = str(Path(root) / "unrelated" / "old.jsonl")
            self.assertFalse(cli.source_matches_target(source, target, {member}))
