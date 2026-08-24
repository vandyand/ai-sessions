import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_opencode_writer import FAKE_CLI, SCHEMA

from ai_sessions.app import (
    Browser,
    Session,
    UserState,
    command_for,
    filtered_sessions,
    list_output,
    prepare_launch,
)
from ai_sessions.capabilities import Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.conversion import BridgeError, bridge, bridged_title, read_snapshot
from ai_sessions.discovery import HarnessContext
from ai_sessions.harnesses import opencode
from ai_sessions.harnesses.opencode_write import create_native_id
from ai_sessions.model import NativeRef, PreparedTarget, SourceKind, Turn
from ai_sessions.registry import REGISTRY


class OpenCodeMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.database = self.root / "opencode.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(
            REGISTRY.temporary(replace(REGISTRY.get("claude"), home=self.root / "claude"))
        )
        self.stack.enter_context(
            REGISTRY.temporary(replace(REGISTRY.get("codex"), home=self.root / "codex"))
        )
        self.command = (sys.executable, str(FAKE_CLI), str(self.database), "standard")
        self.config = LaunchConfig(
            path=self.root / "config.toml",
            providers={"opencode": ProviderProfile(command=list(self.command))},
        )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    @staticmethod
    def portable_turns() -> list[Turn]:
        return [
            Turn("user", "matrix portable request"),
            Turn("assistant", "matrix portable response\n\n⟦shell⟧ ls\n   → total 4"),
            Turn("user", "matrix portable tail"),
        ]

    def prepared_opencode(self) -> PreparedTarget:
        return opencode._prepare_opencode_target(self.command, str(self.cwd), {})

    def write_native(self, tool: str) -> NativeRef:
        adapter = REGISTRY.get(tool)
        writer = adapter.write
        assert not isinstance(writer, Unsupported)
        prepared = (
            self.prepared_opencode()
            if tool == "opencode"
            else PreparedTarget(adapter.default_command, adapter.budget)
        )
        written = writer(
            cwd=str(self.cwd),
            turns=self.portable_turns(),
            title=f"{tool} matrix source",
            prepared=prepared,
            created=1_787_530_000.0,
        )
        return written.native

    def row(self, tool: str, ref: NativeRef, *, launch_tool: str = "") -> Session:
        return Session(
            tool,
            ref.session_id,
            f"{tool} matrix source",
            str(self.cwd),
            1_787_530_000.0,
            1_787_530_000.0,
            "matrix portable tail",
            True,
            ref.storage,
            launch_tool=launch_tool,
        )

    def bridge_pair(self, source: str, target: str):
        source_ref = self.write_native(source)
        prepared = self.prepared_opencode() if target == "opencode" else None
        return bridge(
            source_tool=source,
            target_tool=target,
            session_id=source_ref.session_id,
            storage=source_ref.storage,
            cwd=str(self.cwd),
            title="Matrix portable",
            prepared_target=prepared,
        )

    @staticmethod
    def assert_portable_payload(test: unittest.TestCase, turns: list[Turn]) -> None:
        test.assertEqual([turn.role for turn in turns], ["user", "assistant", "user"])
        test.assertTrue(turns[0].text.endswith("matrix portable request"))
        test.assertEqual(
            turns[1].text,
            "matrix portable response\n\n⟦shell⟧ ls\n   → total 4",
        )
        test.assertEqual(turns[2].text, "matrix portable tail")

    def native_member_count(self, tool: str) -> int:
        if tool == "opencode":
            return self.database_session_count()
        return len(list(REGISTRY.get(tool).home.rglob("*.jsonl")))

    def test_all_six_ordered_bridge_directions_reread_native_semantics(self) -> None:
        for source in ("claude", "codex", "opencode"):
            for target in ("claude", "codex", "opencode"):
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    result = self.bridge_pair(source, target)
                    snapshot = read_snapshot(target, result.native, latest_window=False)
                    relevant = snapshot.transcript.turns
                    self.assert_portable_payload(self, relevant)
                    self.assertEqual(result.tool, target)
                    expected_title = bridged_title("Matrix portable", source)
                    if target == "opencode":
                        connection = sqlite3.connect(self.database)
                        try:
                            title = connection.execute(
                                "SELECT title FROM session WHERE id=?", (result.session_id,)
                            ).fetchone()[0]
                        finally:
                            connection.close()
                        self.assertEqual(title, expected_title)
                        self.assertIn("OpenCode target model provider/large", relevant[0].text)
                    elif target == "codex":
                        index = (self.root / "codex" / "session_index.jsonl").read_text(
                            encoding="utf-8"
                        )
                        self.assertIn(expected_title, index)
                    else:
                        self.assertIn(
                            expected_title, Path(result.storage).read_text(encoding="utf-8")
                        )

    def test_all_six_ordered_prepare_launch_directions_reread_native_semantics(self) -> None:
        for source in ("claude", "codex", "opencode"):
            for target in ("claude", "codex", "opencode"):
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    source_ref = self.write_native(source)
                    item = self.row(source, source_ref, launch_tool=target)
                    state = UserState(self.root / f"state-{source}-{target}.json")
                    prepared, note = prepare_launch(item, state=state, config=self.config)
                    self.assertEqual(prepared.tool, target)
                    self.assertIn("Copied", note)
                    snapshot = read_snapshot(
                        target,
                        NativeRef(prepared.session_id, prepared.storage),
                        latest_window=False,
                    )
                    self.assert_portable_payload(self, snapshot.transcript.turns)

    def test_same_harness_launches_resume_exact_native_identity_without_copy(self) -> None:
        for tool in ("claude", "codex", "opencode"):
            with self.subTest(tool=tool):
                ref = self.write_native(tool)
                item = self.row(tool, ref, launch_tool=tool)
                before = self.native_member_count(tool)
                prepared, note = prepare_launch(item, config=self.config)
                self.assertEqual(
                    (prepared.session_id, prepared.storage), (ref.session_id, ref.storage)
                )
                self.assertEqual(note, "")
                self.assertEqual(self.native_member_count(tool), before)
                if tool == "opencode":
                    safe = command_for(prepared, self.config)
                    self.assertEqual(safe[-2:], ["--session", ref.session_id])
                    dangerous = replace(self.config, mode="dangerous")
                    self.assertEqual(
                        command_for(prepared, dangerous)[-3:],
                        ["--auto", "--session", ref.session_id],
                    )
                    custom = replace(
                        self.config,
                        mode="custom",
                        providers={
                            "opencode": ProviderProfile(
                                command=list(self.command), custom_args=["--theme", "system"]
                            )
                        },
                    )
                    self.assertEqual(
                        command_for(prepared, custom)[-4:],
                        ["--theme", "system", "--session", ref.session_id],
                    )

    def database_session_count(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return int(connection.execute("SELECT count(*) FROM session").fetchone()[0])
        finally:
            connection.close()

    def append_opencode_work(self, ref: NativeRef, text: str) -> None:
        connection = sqlite3.connect(ref.storage)
        try:
            created = int(
                connection.execute(
                    "SELECT coalesce(max(time_created),0)+1 FROM message WHERE session_id=?",
                    (ref.session_id,),
                ).fetchone()[0]
            )
            message_id = create_native_id("msg", "ascending", created)
            part_id = create_native_id("prt", "ascending", created)
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (
                    message_id,
                    ref.session_id,
                    created,
                    json.dumps(
                        {
                            "role": "user",
                            "time": {"created": created},
                            "agent": "build",
                            "model": {"providerID": "provider", "modelID": "large"},
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (
                    part_id,
                    message_id,
                    ref.session_id,
                    created,
                    json.dumps({"type": "text", "text": text}),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def add_opencode_message(
        self,
        session_id: str,
        message_id: str,
        role: str,
        created: int,
        part: dict[str, object],
        **info: object,
    ) -> None:
        connection = sqlite3.connect(self.database)
        try:
            payload = {"role": role, **info}
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (message_id, session_id, created, json.dumps(payload)),
            )
            connection.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (
                    "prt_" + message_id,
                    message_id,
                    session_id,
                    created,
                    json.dumps(part),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_opencode_source_bridge_honors_latest_and_full_compaction_windows(self) -> None:
        session_id = "ses_" + "W" * 26
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO session "
                "(id,directory,title,time_created,time_updated,revert) VALUES (?,?,?,?,?,?)",
                (session_id, str(self.cwd), "compacted", 1, 5, None),
            )
            connection.commit()
        finally:
            connection.close()
        self.add_opencode_message(
            session_id, "msg_000000000001old", "user", 1, {"type": "text", "text": "old"}
        )
        self.add_opencode_message(
            session_id,
            "msg_000000000002compact",
            "user",
            2,
            {"type": "compaction", "auto": True},
        )
        self.add_opencode_message(
            session_id,
            "msg_000000000003summary",
            "assistant",
            3,
            {"type": "text", "text": "governing summary"},
            parentID="msg_000000000002compact",
            summary=True,
            finish="stop",
        )
        self.add_opencode_message(
            session_id,
            "msg_000000000004tail",
            "user",
            4,
            {"type": "text", "text": "current tail"},
        )
        ref = NativeRef(session_id, str(self.database))
        latest = bridge(
            source_tool="opencode",
            target_tool="claude",
            session_id=session_id,
            storage=ref.storage,
            cwd=str(self.cwd),
            latest_window=True,
        )
        full = bridge(
            source_tool="opencode",
            target_tool="codex",
            session_id=session_id,
            storage=ref.storage,
            cwd=str(self.cwd),
            latest_window=False,
        )
        latest_turns = read_snapshot("claude", latest.native, latest_window=False).transcript.turns
        full_turns = read_snapshot("codex", full.native, latest_window=False).transcript.turns
        latest_text = "\n".join(turn.text for turn in latest_turns)
        full_text = "\n".join(turn.text for turn in full_turns)
        self.assertNotIn("\nold\n", f"\n{latest_text}\n")
        self.assertIn("governing summary", latest_text)
        self.assertIn("current tail", latest_text)
        self.assertIn("\nold\n", f"\n{full_text}\n")

    def test_opencode_target_honors_latest_and_full_compaction_windows(self) -> None:
        source_path = self.root / "compacted-claude.jsonl"

        def claude_record(role: str, text: str, **extra: object) -> str:
            return json.dumps(
                {
                    "type": role,
                    "message": {"role": role, "content": text},
                    **extra,
                }
            )

        source_path.write_text(
            "\n".join(
                (
                    claude_record("user", "old target-side source message"),
                    claude_record("assistant", "governing Claude summary", isCompactSummary=True),
                    claude_record("user", "current Claude tail"),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        source = NativeRef("compacted-claude", str(source_path))
        latest = bridge(
            source_tool="claude",
            target_tool="opencode",
            session_id=source.session_id,
            storage=source.storage,
            cwd=str(self.cwd),
            latest_window=True,
            prepared_target=self.prepared_opencode(),
        )
        full = bridge(
            source_tool="claude",
            target_tool="opencode",
            session_id=source.session_id,
            storage=source.storage,
            cwd=str(self.cwd),
            latest_window=False,
            prepared_target=self.prepared_opencode(),
        )
        latest_text = "\n".join(
            turn.text
            for turn in read_snapshot(
                "opencode", latest.native, latest_window=False
            ).transcript.turns
        )
        full_text = "\n".join(
            turn.text
            for turn in read_snapshot("opencode", full.native, latest_window=False).transcript.turns
        )
        self.assertNotIn("old target-side source message", latest_text)
        self.assertIn("governing Claude summary", latest_text)
        self.assertIn("current Claude tail", latest_text)
        self.assertIn("old target-side source message", full_text)

    def test_archived_root_and_child_resume_by_their_exact_native_ids(self) -> None:
        parent = self.write_native("opencode")
        child = self.write_native("opencode")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE session SET time_archived=? WHERE id=?",
                (1_787_530_500_000, parent.session_id),
            )
            connection.execute(
                "UPDATE session SET parent_id=? WHERE id=?", (parent.session_id, child.session_id)
            )
            connection.commit()
        finally:
            connection.close()
        context = HarnessContext.create(
            use_cache=False, provider_commands={"opencode": self.command}
        )
        rows = {row.session_id: row for row in opencode.discover(context, use_cache=False)}
        self.assertTrue(rows[parent.session_id].archived)
        self.assertEqual(rows[child.session_id].parent_id, parent.session_id)
        for ref in (parent, child):
            item = self.row("opencode", ref, launch_tool="opencode")
            prepared, _ = prepare_launch(item, config=self.config)
            self.assertEqual(prepared.session_id, ref.session_id)
            self.assertEqual(command_for(prepared, self.config)[-2:], ["--session", ref.session_id])

    def test_opencode_advance_returns_through_codex_and_reuses_the_new_head(self) -> None:
        source_ref = self.write_native("codex")
        source = self.row("codex", source_ref, launch_tool="opencode")
        state = UserState(self.root / "state.json")
        opencode_copy, note = prepare_launch(source, state=state, config=self.config)
        self.assertIn("Copied", note)
        copy_ref = NativeRef(opencode_copy.session_id, opencode_copy.storage)
        self.append_opencode_work(copy_ref, "work completed in OpenCode")
        copy_row = self.row("opencode", copy_ref, launch_tool="codex")
        source_row = self.row("codex", source_ref, launch_tool="codex")
        state.apply([source_row, copy_row])
        self.assertTrue(source_row.superseded)
        returned, note = prepare_launch(source_row, state=state, config=self.config)
        self.assertIn("Copied", note)
        self.assertEqual(returned.tool, "codex")
        returned_text = "\n".join(
            turn.text
            for turn in read_snapshot(
                "codex", NativeRef(returned.session_id, returned.storage), latest_window=False
            ).transcript.turns
        )
        self.assertIn("work completed in OpenCode", returned_text)
        state.apply([source_row, copy_row, returned])
        copy_row.launch_tool = "codex"
        reused, reuse_note = prepare_launch(copy_row, state=state, config=self.config)
        self.assertEqual(reused.session_id, returned.session_id)
        self.assertIn("Continuing", reuse_note)

    def test_opencode_title_and_unrelated_database_writes_do_not_advance_checkpoint(self) -> None:
        primary = self.write_native("opencode")
        checkpoint = opencode._opencode_checkpoint(primary)
        opencode.publish_name(self.row("opencode", primary), "renamed only")
        other = self.write_native("opencode")
        self.append_opencode_work(other, "unrelated work")
        self.assertEqual(opencode._opencode_change_status(primary, checkpoint), "unchanged")

    def test_opencode_rows_participate_in_hidden_search_list_and_json_surfaces(self) -> None:
        ref = self.write_native("opencode")
        context = HarnessContext.create(
            use_cache=False, provider_commands={"opencode": self.command}
        )
        native = next(
            row
            for row in opencode.discover(context, use_cache=False)
            if row.session_id == ref.session_id
        )
        item = self.row("opencode", NativeRef(native.session_id, native.storage))
        state = UserState(self.root / "ui-state.json")
        state.set_hidden(item, True)
        state.apply([item])
        self.assertTrue(item.hidden)
        self.assertEqual(filtered_sessions([item], query="tool:opencode matrix"), [])
        visible = filtered_sessions(
            [item], query="tool:opencode matrix", visibility="all", origin="all"
        )
        self.assertEqual(visible, [item])
        with contextlib.redirect_stdout(io.StringIO()) as text_output:
            list_output(visible)
        self.assertIn("OpenCode", text_output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as json_output:
            list_output(visible, as_json=True)
        payload = json.loads(json_output.getvalue())[0]
        self.assertEqual((payload["tool"], payload["session_id"]), ("opencode", ref.session_id))

    def test_opencode_child_detail_states_exact_child_resume_and_true_parent(self) -> None:
        class CaptureScreen:
            def __init__(self) -> None:
                self.values: list[str] = []

            def keypad(self, _enabled: bool) -> None:
                pass

            def erase(self) -> None:
                self.values.clear()

            def getmaxyx(self) -> tuple[int, int]:
                return 30, 180

            def addnstr(self, _y: int, _x: int, value: str, length: int, _style: int) -> None:
                self.values.append(value[:length])

            def refresh(self) -> None:
                pass

        ref = self.write_native("opencode")
        parent_id = "ses_" + "P" * 26
        child = replace(
            self.row("opencode", ref),
            source=SourceKind.SUBAGENT,
            auxiliary=True,
            origin="agent",
            parent_id=parent_id,
        )
        screen = CaptureScreen()
        with (
            patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
            patch("ai_sessions.app.detect_open_sessions"),
        ):
            browser = Browser(
                screen, [child], UserState(self.root / "detail-state.json"), self.config
            )
            browser.origin = "agent"
            browser.draw()
        rendered = "\n".join(screen.values)
        self.assertIn("Parent     " + parent_id + " · resumes this child directly", rendered)
        self.assertNotIn("Opens      parent session " + ref.session_id, rendered)

    def test_two_advanced_opencode_and_codex_members_are_divergent(self) -> None:
        source_ref = self.write_native("codex")
        source = self.row("codex", source_ref, launch_tool="opencode")
        state = UserState(self.root / "divergent-state.json")
        copied, _ = prepare_launch(source, state=state, config=self.config)
        copied_ref = NativeRef(copied.session_id, copied.storage)
        self.append_opencode_work(copied_ref, "OpenCode fork")
        with Path(source_ref.storage).open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Codex fork"}],
                        },
                    }
                )
                + "\n"
            )
        source_row = self.row("codex", source_ref, launch_tool="opencode")
        copy_row = self.row("opencode", copied_ref, launch_tool="codex")
        state.apply([source_row, copy_row])
        with self.assertRaisesRegex(BridgeError, "divergent heads"):
            prepare_launch(source_row, state=state, config=self.config)

    def test_missing_newest_opencode_generation_never_promotes_an_older_session(self) -> None:
        source_ref = self.write_native("codex")
        source = self.row("codex", source_ref, launch_tool="opencode")
        state = UserState(self.root / "missing-state.json")
        copied, _ = prepare_launch(source, state=state, config=self.config)
        copied_ref = NativeRef(copied.session_id, copied.storage)
        self.append_opencode_work(copied_ref, "newest OpenCode work")
        source_row = self.row("codex", source_ref, launch_tool="claude")
        copy_row = self.row("opencode", copied_ref, launch_tool="claude")
        state.apply([source_row, copy_row])
        source_row.launch_tool = "claude"
        newest, _ = prepare_launch(source_row, state=state, config=self.config)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM part WHERE session_id=?", (copied.session_id,))
            connection.execute("DELETE FROM message WHERE session_id=?", (copied.session_id,))
            connection.execute("DELETE FROM session WHERE id=?", (copied.session_id,))
            connection.commit()
        finally:
            connection.close()
        Path(newest.storage).unlink()
        state.apply([source_row])
        with self.assertRaisesRegex(BridgeError, "unavailable newest"):
            prepare_launch(source_row, state=state, config=self.config)

    def test_concurrent_opencode_source_update_aborts_before_target_write(self) -> None:
        source_ref = self.write_native("opencode")
        source_adapter = REGISTRY.get("opencode")
        original_read = source_adapter.read
        assert not isinstance(original_read, Unsupported)

        def read_then_advance(ref: NativeRef, *, latest_window: bool = True):
            snapshot = original_read(ref, latest_window=latest_window)
            self.append_opencode_work(ref, "arrived during bridge read")
            return snapshot

        claude_home = self.root / "claude"
        before = set(claude_home.rglob("*.jsonl")) if claude_home.exists() else set()
        with REGISTRY.temporary(replace(source_adapter, read=read_then_advance)):
            with self.assertRaisesRegex(BridgeError, "changed or became incomplete"):
                bridge(
                    source_tool="opencode",
                    target_tool="claude",
                    session_id=source_ref.session_id,
                    storage=source_ref.storage,
                    cwd=str(self.cwd),
                )
        after = set(claude_home.rglob("*.jsonl")) if claude_home.exists() else set()
        self.assertEqual(after, before)

    def test_advanced_opencode_source_rematerializes_target_instead_of_reusing_copy(self) -> None:
        source_ref = self.write_native("opencode")
        source = self.row("opencode", source_ref, launch_tool="claude")
        state = UserState(self.root / "source-advance-state.json")
        first, _ = prepare_launch(source, state=state, config=self.config)
        self.append_opencode_work(source_ref, "source moved after first copy")
        moved = self.row("opencode", source_ref, launch_tool="claude")
        second, note = prepare_launch(moved, state=state, config=self.config)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertIn("Copied", note)

    def test_missing_opencode_copy_row_is_rematerialized(self) -> None:
        source_ref = self.write_native("codex")
        source = self.row("codex", source_ref, launch_tool="opencode")
        state = UserState(self.root / "missing-row-state.json")
        first, _ = prepare_launch(source, state=state, config=self.config)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM part WHERE session_id=?", (first.session_id,))
            connection.execute("DELETE FROM message WHERE session_id=?", (first.session_id,))
            connection.execute("DELETE FROM session WHERE id=?", (first.session_id,))
            connection.commit()
        finally:
            connection.close()
        second, note = prepare_launch(source, state=state, config=self.config)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertIn("Copied", note)

    def test_older_codex_generation_stays_superseded_during_opencode_claude_divergence(
        self,
    ) -> None:
        original_ref = self.write_native("codex")
        original = self.row("codex", original_ref, launch_tool="opencode")
        state = UserState(self.root / "old-generation-state.json")
        middle, _ = prepare_launch(original, state=state, config=self.config)
        middle_ref = NativeRef(middle.session_id, middle.storage)
        self.append_opencode_work(middle_ref, "advance the OpenCode generation")
        original_row = self.row("codex", original_ref, launch_tool="claude")
        middle_row = self.row("opencode", middle_ref, launch_tool="claude")
        state.apply([original_row, middle_row])
        original_row.launch_tool = "claude"
        newest, _ = prepare_launch(original_row, state=state, config=self.config)

        self.append_opencode_work(middle_ref, "OpenCode divergent fork")
        with Path(newest.storage).open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Claude divergent fork"},
                    }
                )
                + "\n"
            )
        state.apply([original_row, middle_row, newest])
        self.assertTrue(original_row.superseded)
        self.assertFalse(original_row.diverged)
        self.assertTrue(middle_row.diverged)
        self.assertTrue(newest.diverged)

    def test_unverified_opencode_member_stays_unstable_through_schema_six_round_trip(self) -> None:
        source_ref = self.write_native("codex")
        prepared = self.prepared_opencode()
        verification_error = BridgeError("persisted verification unavailable")
        with patch.object(
            opencode,
            "_verify_import_with_backoff",
            return_value=(None, verification_error),
        ):
            target_written = opencode._write_opencode(
                cwd=str(self.cwd),
                turns=self.portable_turns(),
                prepared=prepared,
                title="unverified OpenCode import",
            )
        self.assertIsNone(target_written.checkpoint)
        self.assertTrue(any("remains unstable" in note for note in target_written.notices))
        target_ref = target_written.native
        source = self.row("codex", source_ref, launch_tool="opencode")
        target = self.row("opencode", target_ref, launch_tool="codex")
        source_checkpoint = REGISTRY.get("codex").checkpoint
        assert not isinstance(source_checkpoint, Unsupported)
        state_path = self.root / "unverified-state.json"
        state = UserState(state_path)
        state.set_bridge(
            source,
            "opencode",
            target_ref.session_id,
            target_ref.storage,
            source_checkpoint=source_checkpoint(source_ref),
            target_checkpoint=target_written.checkpoint,
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 6)

        restored = UserState(state_path)
        conversation_id = restored.conversation_id_for(source)
        member = restored.conversations[conversation_id]["members"][target.key]
        self.assertNotIn("checkpoint", member)
        self.assertNotIn("cursor", member)
        self.assertEqual(restored._member_change_status(member), "unstable")
        status = restored._conversation_status(conversation_id)
        self.assertEqual([key for key, _ in status["unstable"]], [target.key])
        self.assertEqual(status["advanced"], [])

        restored.apply([source, target])
        for selected, launch_tool in ((source, "opencode"), (target, "codex")):
            with self.subTest(selected=selected.tool):
                selected.launch_tool = launch_tool
                with self.assertRaisesRegex(BridgeError, "incomplete or unstable"):
                    prepare_launch(selected, state=restored, config=self.config)


if __name__ == "__main__":
    unittest.main()
