import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.conversion import BridgeError, bridge, resolve_budget
from ai_sessions.harnesses import opencode
from ai_sessions.harnesses.opencode_semantics import StoredMessage, semantic_checkpoint
from ai_sessions.harnesses.opencode_write import (
    build_export,
    choose_model,
    create_native_id,
    parse_model_ids,
    parse_verbose_models,
)
from ai_sessions.model import SourceKind, ToolCall, Turn

FIXTURE = Path(__file__).parent / "fixtures" / "opencode-1.18.21-cli.json"
IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "opencode-1.18.21-import.json"
FAKE_CLI = Path(__file__).parent / "fixtures" / "fake_opencode_cli.py"

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    directory TEXT NOT NULL,
    path TEXT,
    project_id TEXT,
    title TEXT NOT NULL,
    agent TEXT,
    metadata TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    time_archived INTEGER,
    revert TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX message_session_time ON message(session_id, time_created, id);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX part_message_id ON part(message_id, id);
CREATE INDEX part_session_id ON part(session_id);
"""


class OpenCodeWriterAlgorithmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_real_cli_capture_parses_installed_and_verbose_models(self) -> None:
        behavior = self.capture["command_behavior"]
        installed = parse_model_ids(behavior["models"]["stdout"])
        verbose = parse_verbose_models(behavior["models_verbose"]["stdout"])
        self.assertEqual(installed[0], "opencode/big-pickle")
        self.assertEqual(tuple(model.full_id for model in verbose), installed)
        selected = choose_model(
            installed,
            verbose,
            minimum_input_tokens=opencode.OPENCODE_CONTEXT_FLOOR_TOKENS,
        )
        self.assertEqual(selected.full_id, "opencode/big-pickle")
        self.assertEqual(selected.effective_input_tokens, 160_000)
        self.assertEqual(self.capture["opencode_version"], "1.18.21")

    def test_real_import_capture_preserves_ids_nonce_and_semantic_checkpoint(self) -> None:
        capture = json.loads(IMPORT_FIXTURE.read_text(encoding="utf-8"))
        submitted = capture["submitted"]
        persisted = capture["persisted"]
        self.assertEqual(capture["opencode_version"], "1.18.21")
        self.assertEqual(capture["import"]["exit_code"], 0)
        self.assertTrue(all(capture["observed"].values()))
        self.assertEqual(persisted["session"]["id"], submitted["info"]["id"])
        self.assertEqual(
            json.loads(persisted["session"]["metadata"]), submitted["info"]["metadata"]
        )
        submitted_messages = [
            StoredMessage(
                item["info"]["id"],
                item["info"]["time"]["created"],
                item["info"],
                tuple(item["parts"]),
            )
            for item in submitted["messages"]
        ]
        parts_by_message: dict[str, list[dict[str, object]]] = {}
        for row in persisted["parts"]:
            part = {
                **row["data"],
                "id": row["id"],
                "messageID": row["message_id"],
                "sessionID": row["session_id"],
            }
            parts_by_message.setdefault(row["message_id"], []).append(part)
        persisted_messages = [
            StoredMessage(
                row["id"],
                row["time_created"],
                {**row["data"], "id": row["id"], "sessionID": row["session_id"]},
                tuple(parts_by_message[row["id"]]),
            )
            for row in persisted["messages"]
        ]
        self.assertEqual(
            semantic_checkpoint(submitted_messages, None),
            semantic_checkpoint(persisted_messages, None),
        )

    def test_model_parsers_fail_closed_on_drift(self) -> None:
        with self.assertRaisesRegex(BridgeError, "duplicates"):
            parse_model_ids("provider/model\nprovider/model\n")
        with self.assertRaisesRegex(BridgeError, "provider/model identifier"):
            parse_model_ids("not-a-model\n")
        captured = self.capture["command_behavior"]["models_verbose"]["stdout"]
        with self.assertRaisesRegex(BridgeError, "disagrees"):
            parse_verbose_models(captured.replace('"id": "big-pickle"', '"id": "other"', 1))
        with self.assertRaisesRegex(BridgeError, "unknown status"):
            parse_verbose_models(captured.replace('"status": "active"', '"status": "future"', 1))
        with self.assertRaisesRegex(BridgeError, "malformed JSON"):
            parse_verbose_models("provider/model\n{")

    def test_adapter_resume_and_launch_policy_are_exactly_opencode_native(self) -> None:
        adapter = opencode.ADAPTER
        self.assertEqual(adapter.dangerous_args, ("--auto",))
        self.assertEqual(adapter.scratch_patterns, ())
        for source in (SourceKind.INTERACTIVE, SourceKind.SUBAGENT):
            self.assertEqual(
                opencode.resume_args(
                    session_id="ses_" + "A" * 26,
                    source=source,
                    resume_id="ignored",
                    parent_id="ignored",
                    native=True,
                ),
                ["--session", "ses_" + "A" * 26],
            )

    def test_explicit_model_wins_and_missing_model_fails(self) -> None:
        behavior = self.capture["command_behavior"]
        installed = parse_model_ids(behavior["models"]["stdout"])
        verbose = parse_verbose_models(behavior["models_verbose"]["stdout"])
        selected = choose_model(
            installed,
            verbose,
            explicit="opencode/x-preview-f-free",
            minimum_input_tokens=128_000,
        )
        self.assertEqual(selected.full_id, "opencode/x-preview-f-free")
        with self.assertRaisesRegex(BridgeError, "is not installed"):
            choose_model(
                installed,
                verbose,
                explicit="provider/missing",
                minimum_input_tokens=128_000,
            )

    def test_native_ids_have_exact_shape_and_monotone_time_prefixes(self) -> None:
        timestamp = 1_787_489_550_507
        ascending = [create_native_id("msg", "ascending", timestamp) for _ in range(5_000)]
        descending = [create_native_id("ses", "descending", timestamp) for _ in range(5_000)]
        self.assertEqual(len(set(ascending)), 5_000)
        self.assertEqual(len(set(descending)), 5_000)
        self.assertTrue(all(len(value) == 30 and value.startswith("msg_") for value in ascending))
        self.assertTrue(all(len(value) == 30 and value.startswith("ses_") for value in descending))
        self.assertEqual(
            [value[4:16] for value in ascending], sorted(value[4:16] for value in ascending)
        )
        self.assertEqual(
            [value[4:16] for value in descending],
            sorted((value[4:16] for value in descending), reverse=True),
        )

    def test_export_is_alternating_schema_shape_with_expected_checkpoint(self) -> None:
        generated = build_export(
            cwd="/work/project",
            root="/work/project",
            turns=[Turn("user", "one"), Turn("assistant", "two"), Turn("user", "three")],
            title="Imported",
            provider_id="provider",
            model_id="large",
            nonce="0123456789abcdef",
            created_ms=1_000,
        )
        self.assertRegex(generated.session_id, r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
        self.assertEqual(len(generated.message_ids), 3)
        self.assertEqual(len(generated.part_ids), 3)
        self.assertEqual(
            [item["info"]["role"] for item in generated.payload["messages"]],
            ["user", "assistant", "user"],
        )
        self.assertEqual(
            generated.payload["messages"][1]["info"]["parentID"], generated.message_ids[0]
        )
        self.assertEqual(
            generated.payload["info"]["metadata"]["ai_sessions_import_nonce"],
            generated.nonce,
        )
        self.assertRegex(generated.expected_checkpoint, r"^sqlite-semantic-v1:[0-9a-f]{64}$")
        json.dumps(generated.payload, allow_nan=False)

    def test_export_rejects_non_alternating_turns_and_preserves_flattened_call_text(self) -> None:
        arguments = {
            "cwd": "/work",
            "root": "/",
            "title": "title",
            "provider_id": "provider",
            "model_id": "model",
            "nonce": "nonce",
            "created_ms": 1,
        }
        with self.assertRaisesRegex(BridgeError, "must start with a user"):
            build_export(turns=[Turn("assistant", "answer")], **arguments)
        with self.assertRaisesRegex(BridgeError, "must alternate"):
            build_export(turns=[Turn("user", "one"), Turn("user", "two")], **arguments)
        generated = build_export(
            turns=[
                Turn("user", "one"),
                Turn(
                    "assistant",
                    "done\n\n⟦shell⟧ ls\n   → ok",
                    (ToolCall("shell", "ls", "ok"),),
                ),
            ],
            **arguments,
        )
        self.assertIn("⟦shell⟧", generated.payload["messages"][1]["parts"][0]["text"])


class OpenCodeWriterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "opencode.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, mode: str = "standard") -> tuple[str, ...]:
        return (sys.executable, str(FAKE_CLI), str(self.database), mode)

    def prepared(self, mode: str = "standard", **options: object):
        return opencode._prepare_opencode_target(self.command(mode), str(self.root), options)

    @staticmethod
    def turns() -> list[Turn]:
        return [Turn("user", "one"), Turn("assistant", "two"), Turn("user", "three")]

    def test_prepare_selects_cli_order_adequate_model_and_dynamic_hard_budget(self) -> None:
        prepared = self.prepared()
        self.assertEqual(prepared.option("model"), "provider/large")
        self.assertEqual(prepared.option("database"), str(self.database))
        self.assertEqual(prepared.budget_policy.context_tokens, 160_000)
        self.assertTrue(prepared.budget_policy.hard_limit)
        self.assertIn("provider/large", prepared.handoff_context[0])
        default = resolve_budget("opencode", policy=prepared.budget_policy)
        self.assertEqual(default.tokens, 120_000)
        with self.assertRaisesRegex(BridgeError, "exceeds the prepared opencode target limit"):
            resolve_budget("opencode", max_tokens=120_001, policy=prepared.budget_policy)

    def test_prepare_honors_explicit_model_and_rejects_missing_or_invalid_option(self) -> None:
        prepared = self.prepared(bridge_model="provider/small")
        self.assertEqual(prepared.option("model"), "provider/small")
        self.assertEqual(prepared.budget_policy.context_tokens, 56_000)
        with self.assertRaisesRegex(BridgeError, "is not installed"):
            self.prepared(bridge_model="provider/missing")
        with self.assertRaisesRegex(BridgeError, "must be a string"):
            self.prepared(bridge_model=42)
        with self.assertRaisesRegex(BridgeError, "too little usable input capacity"):
            self.prepared(bridge_model="provider/tiny")

    def test_malformed_verbose_output_uses_warned_static_fallback(self) -> None:
        prepared = self.prepared("bad-verbose")
        self.assertEqual(prepared.option("model"), "provider/small")
        self.assertEqual(
            prepared.budget_policy.context_tokens, opencode.OPENCODE_CONTEXT_FLOOR_TOKENS
        )
        self.assertTrue(prepared.notices)
        self.assertIn("could not be used", prepared.notices[0])

    def test_write_imports_and_rereads_exact_semantic_content(self) -> None:
        prepared = self.prepared()
        written = opencode._write_opencode(
            cwd=str(self.root),
            turns=self.turns(),
            prepared=prepared,
            title="Imported title",
            created=1_787_489_550.507,
        )
        self.assertIsNotNone(written.checkpoint)
        snapshot = opencode._read_opencode_snapshot(written.native, latest_window=False)
        self.assertEqual(snapshot.checkpoint, written.checkpoint)
        self.assertEqual([turn.text for turn in snapshot.transcript.turns], ["one", "two", "three"])
        connection = sqlite3.connect(self.database)
        try:
            title, metadata = connection.execute(
                "SELECT title, metadata FROM session WHERE id=?", (written.native.session_id,)
            ).fetchone()
            user = json.loads(
                connection.execute(
                    "SELECT data FROM message WHERE session_id=? ORDER BY time_created LIMIT 1",
                    (written.native.session_id,),
                ).fetchone()[0]
            )
            message_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM message WHERE session_id=?", (written.native.session_id,)
                )
            ]
            part_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM part WHERE session_id=?", (written.native.session_id,)
                )
            ]
        finally:
            connection.close()
        self.assertEqual(title, "Imported title")
        self.assertIn("ai_sessions_import_nonce", json.loads(metadata))
        self.assertEqual(user["model"], {"providerID": "provider", "modelID": "large"})
        self.assertRegex(written.native.session_id, r"^ses_[0-9A-Za-z]{26}$")
        for native_id in (*message_ids, *part_ids):
            self.assertRegex(native_id, r"^(?:msg|prt)_[0-9A-Za-z]{26}$")
        import_path = Path(Path(str(self.database) + ".import-path").read_text(encoding="utf-8"))
        self.assertFalse(import_path.exists())

    def test_temporary_export_creation_failure_closes_descriptor_and_removes_file(self) -> None:
        descriptor, name = tempfile.mkstemp(dir=self.root)
        path = Path(name)
        with (
            patch.object(opencode.tempfile, "mkstemp", return_value=(descriptor, name)),
            patch.object(opencode.os, "fdopen", side_effect=OSError("fdopen failed")),
            self.assertRaisesRegex(OSError, "fdopen failed"),
        ):
            opencode._write_temporary_export({"session": "not written"})

        self.assertFalse(path.exists())
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_temporary_export_preserves_creation_error_if_cleanup_also_fails(self) -> None:
        descriptor, name = tempfile.mkstemp(dir=self.root)
        path = Path(name)
        creation_error = OSError("fdopen failed")
        with (
            patch.object(opencode.tempfile, "mkstemp", return_value=(descriptor, name)),
            patch.object(opencode.os, "fdopen", side_effect=creation_error),
            patch.object(Path, "unlink", side_effect=OSError("unlink denied")),
            self.assertRaises(OSError) as raised,
        ):
            opencode._write_temporary_export({"session": "not written"})

        self.assertIs(raised.exception, creation_error)
        self.assertTrue(path.exists())
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        path.unlink()

    def test_import_failure_reports_a_secondary_cleanup_failure(self) -> None:
        temporary = self.root / "failed-import.json"
        temporary.write_text("sensitive transcript", encoding="utf-8")
        with (
            patch.object(opencode, "run_maintenance", side_effect=BridgeError("import failed")),
            patch.object(Path, "unlink", side_effect=OSError("unlink denied")),
            self.assertRaisesRegex(
                BridgeError,
                "import failed; the temporary OpenCode import file also could not be removed: "
                "unlink denied",
            ),
        ):
            opencode._run_import_and_cleanup(self.command(), str(self.root), temporary)

        self.assertTrue(temporary.exists())

    def test_import_failure_propagates_after_successful_cleanup(self) -> None:
        temporary = self.root / "ordinary-failed-import.json"
        temporary.write_text("sensitive transcript", encoding="utf-8")
        failure = BridgeError("ordinary import failure")
        with (
            patch.object(opencode, "run_maintenance", side_effect=failure),
            self.assertRaises(BridgeError) as raised,
        ):
            opencode._run_import_and_cleanup(self.command(), str(self.root), temporary)

        self.assertIs(raised.exception, failure)
        self.assertFalse(temporary.exists())

    def test_import_accepts_a_temporary_file_already_removed_by_provider(self) -> None:
        temporary = self.root / "provider-removed-import.json"
        result = opencode.MaintenanceResult(self.command(), 0, "Imported session", "")
        with patch.object(opencode, "run_maintenance", return_value=result):
            actual = opencode._run_import_and_cleanup(self.command(), str(self.root), temporary)

        self.assertIs(actual, result)

    def test_successful_import_treats_cleanup_failure_as_an_error(self) -> None:
        temporary = self.root / "successful-import.json"
        temporary.write_text("sensitive transcript", encoding="utf-8")
        result = opencode.MaintenanceResult(self.command(), 0, "Imported session", "")
        with (
            patch.object(opencode, "run_maintenance", return_value=result),
            patch.object(Path, "unlink", side_effect=OSError("unlink denied")),
            self.assertRaisesRegex(
                BridgeError,
                "could not remove the temporary OpenCode import file: unlink denied",
            ),
        ):
            opencode._run_import_and_cleanup(self.command(), str(self.root), temporary)

        self.assertTrue(temporary.exists())

    def test_windows_final_wait_rekills_a_process_that_survives_initial_cleanup(self) -> None:
        process = Mock()
        process.stdout = Mock()
        process.stderr = Mock()
        process.returncode = None
        process.poll.return_value = None
        process.wait.side_effect = (
            opencode.subprocess.TimeoutExpired("fixture", 0.01),
            opencode.subprocess.TimeoutExpired("fixture", 0.1),
            9,
        )
        readers = (Mock(), Mock())
        for reader in readers:
            reader.is_alive.return_value = False

        with (
            patch.object(opencode, "IS_WINDOWS", True),
            patch.object(opencode._WindowsJob, "assign", return_value=None),
            patch.object(opencode.subprocess, "Popen", return_value=process),
            patch.object(opencode.threading, "Thread", side_effect=readers),
        ):
            result = opencode.run_maintenance(
                (sys.executable,),
                ("--fixture",),
                cwd=str(self.root),
                timeout=0.01,
                return_timeout=True,
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(process.kill.call_count, 2)
        self.assertEqual(process.wait.call_count, 3)

    def test_reminted_localized_and_marker_recovery_bind_verified_persisted_rows(self) -> None:
        reminted = opencode._write_opencode(
            cwd=str(self.root),
            turns=self.turns(),
            prepared=self.prepared("remint"),
        )
        self.assertEqual(reminted.native.session_id, "ses_000000000000RRRRRRRRRRRRRR")
        self.assertTrue(reminted.notices)

        second_database = self.root / "localized.db"
        self.database = second_database
        connection = sqlite3.connect(second_database)
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()
        localized = opencode._write_opencode(
            cwd=str(self.root),
            turns=self.turns(),
            prepared=self.prepared("localized"),
        )
        self.assertIsNotNone(localized.checkpoint)
        self.assertTrue(any("did not name" in notice for notice in localized.notices))
        marker_database = self.root / "marker.db"
        self.database = marker_database
        connection = sqlite3.connect(marker_database)
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()
        marker = opencode._write_opencode(
            cwd=str(self.root),
            turns=self.turns(),
            prepared=self.prepared("remint-localized"),
        )
        self.assertEqual(marker.native.session_id, "ses_000000000000RRRRRRRRRRRRRR")
        self.assertTrue(any("did not name" in notice for notice in marker.notices))

    def test_persisted_content_mismatch_is_a_hard_error_naming_only_proven_id(self) -> None:
        prepared = self.prepared("corrupt")
        with self.assertRaisesRegex(
            BridgeError, r"imported session ses_[0-9A-Za-z]+ does not match"
        ):
            opencode._write_opencode(cwd=str(self.root), turns=self.turns(), prepared=prepared)

    def test_transient_persisted_mismatch_recovers_on_a_later_verification(self) -> None:
        original = opencode._verify_import
        attempts = 0

        def verify_after_settle(path, generated, reported_id):
            nonlocal attempts
            attempts += 1
            try:
                return original(path, generated, reported_id)
            except opencode._PersistedImportMismatch:
                if attempts == 1:
                    connection = sqlite3.connect(path)
                    try:
                        row = connection.execute(
                            "SELECT id,data FROM part WHERE session_id=? ORDER BY message_id,id "
                            "LIMIT 1",
                            (generated.session_id,),
                        ).fetchone()
                        payload = json.loads(row[1])
                        payload["text"] = "one"
                        connection.execute(
                            "UPDATE part SET data=? WHERE id=?", (json.dumps(payload), row[0])
                        )
                        connection.commit()
                    finally:
                        connection.close()
                raise

        with patch.object(opencode, "_verify_import", side_effect=verify_after_settle):
            written = opencode._write_opencode(
                cwd=str(self.root), turns=self.turns(), prepared=self.prepared("corrupt")
            )
        self.assertGreaterEqual(attempts, 2)
        self.assertIsNotNone(written.checkpoint)

    def test_success_without_visible_row_is_an_actionable_error(self) -> None:
        prepared = self.prepared("no-persist")
        with self.assertRaisesRegex(BridgeError, "no persisted session row became visible"):
            opencode._write_opencode(cwd=str(self.root), turns=self.turns(), prepared=prepared)

    def test_success_without_verified_content_or_recoverable_id_is_an_error(self) -> None:
        verification_error = BridgeError("persisted verification unavailable")
        with (
            patch.object(
                opencode,
                "_verify_import_with_backoff",
                return_value=(None, verification_error),
            ),
            self.assertRaisesRegex(
                BridgeError,
                "no persisted session or recoverable ID could be verified",
            ),
        ):
            opencode._write_opencode(
                cwd=str(self.root),
                turns=self.turns(),
                prepared=self.prepared("localized"),
            )

    def test_timed_out_import_recovers_a_committed_verified_session(self) -> None:
        with patch.object(opencode, "IMPORT_TIMEOUT_SECONDS", 1.0):
            written = opencode._write_opencode(
                cwd=str(self.root),
                turns=self.turns(),
                prepared=self.prepared("persist-timeout"),
            )
        self.assertIsNotNone(written.checkpoint)
        self.assertTrue(any("timed out after persisting" in note for note in written.notices))

    def test_post_import_authority_failure_records_only_verified_unstable_member(self) -> None:
        written = opencode._write_opencode(
            cwd=str(self.root),
            turns=self.turns(),
            prepared=self.prepared("db-fail-after-import"),
        )
        self.assertIsNone(written.checkpoint)
        self.assertTrue(
            any("previously authoritative database" in note for note in written.notices)
        )
        self.assertEqual(opencode._availability(written.native), opencode.Availability.AVAILABLE)

    def test_rejected_and_timed_out_imports_fail_and_remove_temporary_file(self) -> None:
        with self.assertRaisesRegex(BridgeError, "fixture import rejected"):
            opencode._write_opencode(
                cwd=str(self.root), turns=self.turns(), prepared=self.prepared("reject")
            )
        with (
            patch.object(opencode, "IMPORT_TIMEOUT_SECONDS", 0.05),
            self.assertRaisesRegex(BridgeError, "timed out"),
        ):
            opencode._write_opencode(
                cwd=str(self.root), turns=self.turns(), prepared=self.prepared("timeout")
            )
        recorded = Path(str(self.database) + ".import-path")
        temporary = Path(recorded.read_text(encoding="utf-8"))
        self.assertFalse(temporary.exists())

    def test_preflight_session_conflict_prevents_writer_from_spawning_import(self) -> None:
        prepared = self.prepared()
        generated = build_export(
            cwd=str(self.root),
            root=str(self.root),
            turns=self.turns(),
            title="title",
            provider_id="provider",
            model_id="large",
            nonce="nonce",
            created_ms=1,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO session "
                "(id,directory,title,time_created,time_updated) VALUES (?,?,?,?,?)",
                (generated.session_id, str(self.root), "conflict", 1, 1),
            )
            connection.commit()
        finally:
            connection.close()
        with (
            patch.object(opencode, "build_export", return_value=generated),
            self.assertRaisesRegex(BridgeError, "session identifier"),
        ):
            opencode._write_opencode(cwd=str(self.root), turns=self.turns(), prepared=prepared)
        self.assertFalse(Path(str(self.database) + ".import-path").exists())

    def test_production_bridge_accepts_flattened_tool_calls(self) -> None:
        source = self.root / "codex-source.jsonl"
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "checking"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "call-1",
                    "arguments": '{"command":"ls"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "ok",
                },
            },
        ]
        source.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        prepared = self.prepared()
        result = bridge(
            source_tool="codex",
            target_tool="opencode",
            session_id="019f0000-0000-7000-8000-000000000001",
            storage=str(source),
            cwd=str(self.root),
            prepared_target=prepared,
        )
        snapshot = opencode._read_opencode_snapshot(result.native, latest_window=False)
        self.assertIn("⟦shell⟧", snapshot.transcript.turns[-1].text)

    def test_title_publication_is_exact_bounded_and_checkpoint_neutral(self) -> None:
        written = opencode._write_opencode(
            cwd=str(self.root), turns=self.turns(), prepared=self.prepared()
        )
        before = opencode._opencode_checkpoint(written.native)
        note = opencode.publish_name(
            SimpleNamespace(
                session_id=written.native.session_id,
                storage=written.native.storage,
            ),
            "Renamed",
        )
        self.assertIn("running OpenCode TUI", note)
        connection = sqlite3.connect(self.database)
        try:
            title = connection.execute(
                "SELECT title FROM session WHERE id=?", (written.native.session_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(title, "Renamed")
        self.assertEqual(opencode._opencode_checkpoint(written.native), before)

    def test_missing_title_database_is_not_created(self) -> None:
        missing = self.root / "missing.db"
        with self.assertRaises(BridgeError):
            opencode.publish_name(
                SimpleNamespace(session_id="ses_missing", storage=str(missing)), "name"
            )
        self.assertFalse(missing.exists())

    def test_title_publication_rolls_back_when_database_is_locked(self) -> None:
        written = opencode._write_opencode(
            cwd=str(self.root), turns=self.turns(), prepared=self.prepared()
        )
        lock = sqlite3.connect(self.database)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            with (
                patch.object(opencode, "SEMANTIC_BUSY_TIMEOUT_SECONDS", 0.05),
                self.assertRaisesRegex(BridgeError, "locked"),
            ):
                opencode.publish_name(
                    SimpleNamespace(
                        session_id=written.native.session_id,
                        storage=written.native.storage,
                    ),
                    "must not commit",
                )
        finally:
            lock.rollback()
            lock.close()
        connection = sqlite3.connect(self.database)
        try:
            title = connection.execute(
                "SELECT title FROM session WHERE id=?", (written.native.session_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(title, "must not commit")

    def test_title_publication_missing_row_does_not_create_or_change_rows(self) -> None:
        before = self.database.stat().st_size
        with self.assertRaisesRegex(BridgeError, "is not present"):
            opencode.publish_name(
                SimpleNamespace(session_id="ses_" + "x" * 26, storage=str(self.database)),
                "name",
            )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM session").fetchone()[0], 0)
        finally:
            connection.close()
        self.assertEqual(self.database.stat().st_size, before)

    def test_write_rejects_missing_prepared_database(self) -> None:
        prepared = self.prepared()
        without_database = SimpleNamespace(
            command=prepared.command,
            option=lambda name: "" if name == "database" else prepared.option(name),
        )
        with self.assertRaisesRegex(BridgeError, "did not resolve an import database"):
            opencode._write_opencode(
                cwd=str(self.root), turns=self.turns(), prepared=without_database
            )

    @unittest.skipUnless(os.name == "nt", "Windows command-shim import fidelity")
    def test_windows_cmd_fake_preserves_metacharacter_paths_with_shell_false(self) -> None:
        command_root = self.root / "cmd & caret^ bang! parens()"
        command_root.mkdir()
        shim = command_root / "open-code.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{FAKE_CLI}" "{self.database}" standard %*\r\n',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {
                "PATH": str(command_root) + os.pathsep + os.environ.get("PATH", ""),
                "PATHEXT": ".CMD;.EXE;.BAT;.COM",
            },
        ):
            prepared = opencode._prepare_opencode_target(("open-code",), str(self.root), {})
            with patch.object(tempfile, "tempdir", str(command_root)):
                written = opencode._write_opencode(
                    cwd=str(self.root), turns=self.turns(), prepared=prepared
                )
        self.assertIsNotNone(written.checkpoint)
        import_path = Path(Path(str(self.database) + ".import-path").read_text(encoding="utf-8"))
        self.assertEqual(import_path.parent, command_root)
        self.assertFalse(import_path.exists())
        self.assertRegex(written.native.session_id, r"^ses_[0-9A-Za-z]{26}$")
        connection = sqlite3.connect(self.database)
        try:
            message_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM message WHERE session_id=?", (written.native.session_id,)
                )
            ]
            part_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM part WHERE session_id=?", (written.native.session_id,)
                )
            ]
        finally:
            connection.close()
        self.assertTrue(message_ids)
        self.assertTrue(part_ids)
        for native_id in (*message_ids, *part_ids):
            self.assertRegex(native_id, r"^(?:msg|prt)_[0-9A-Za-z]{26}$")


if __name__ == "__main__":
    unittest.main()
