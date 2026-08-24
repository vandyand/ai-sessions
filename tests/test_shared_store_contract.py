import ast
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared_store_harness import SharedStoreFixture

from ai_sessions.app import Session, UserState, command_for, prepare_launch
from ai_sessions.capabilities import Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.conversion import (
    BridgeError,
    bridge,
    conversation_change_status,
    native_checkpoint,
    native_session_availability,
    native_session_exists,
    read_snapshot,
    read_transcript,
    resolve_native_session,
)
from ai_sessions.model import (
    Availability,
    Budget,
    BudgetPolicy,
    NativeRef,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    Transcript,
    Turn,
)
from ai_sessions.registry import REGISTRY


class SharedStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = SharedStoreFixture(self.root)
        self.fixture.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_two_ids_in_one_store_are_never_interchangeable(self) -> None:
        first = self.fixture.put("shr-a", Turn("user", "alpha"))
        second = self.fixture.put("shr-b", Turn("user", "beta"))
        with REGISTRY.temporary(self.fixture.adapter()):
            first_snapshot = read_snapshot("shared", first)
            second_snapshot = read_snapshot("shared", second)
        self.assertEqual(first.storage, second.storage)
        self.assertNotEqual(first, second)
        self.assertEqual([turn.text for turn in first_snapshot.transcript.turns], ["alpha"])
        self.assertEqual([turn.text for turn in second_snapshot.transcript.turns], ["beta"])
        self.assertNotEqual(first_snapshot.checkpoint, second_snapshot.checkpoint)

    def test_deleted_row_is_unavailable_even_when_store_and_same_id_elsewhere_exist(self) -> None:
        original = self.fixture.put("shr-a", Turn("user", "original"))
        other = SharedStoreFixture(self.root / "other")
        other.put("shr-a", Turn("user", "other database"))
        self.fixture.delete("shr-a")
        with REGISTRY.temporary(self.fixture.adapter()):
            self.assertEqual(
                native_session_availability("shared", original), Availability.UNAVAILABLE
            )
            self.assertIsNone(REGISTRY.get("shared").resolve("shr-a"))
            self.assertEqual(
                native_session_availability("shared", NativeRef("shr-a", str(other.path))),
                Availability.AVAILABLE,
            )

    def test_resolver_cannot_return_a_different_native_identity(self) -> None:
        self.fixture.put("shr-a", Turn("user", "alpha"))
        mismatched = replace(
            self.fixture.adapter(),
            resolve=lambda _session_id: NativeRef("shr-b", str(self.fixture.path)),
        )
        with REGISTRY.temporary(mismatched):
            self.assertIsNone(resolve_native_session("shared", "shr-a"))

    def test_semantic_checkpoints_are_session_local_and_detect_edits(self) -> None:
        first = self.fixture.put("shr-a", Turn("user", "alpha"))
        second = self.fixture.put("shr-b", Turn("user", "beta"))
        with REGISTRY.temporary(self.fixture.adapter()):
            before = native_checkpoint("shared", first)
            self.fixture.append("shr-b", Turn("assistant", "unrelated"))
            self.assertEqual(native_checkpoint("shared", first), before)
            self.assertEqual(conversation_change_status("shared", first, before), "unchanged")
            self.fixture.edit("shr-a", 0, "alpha edited")
            self.assertNotEqual(native_checkpoint("shared", first), before)
            self.assertEqual(conversation_change_status("shared", first, before), "changed")
            self.assertEqual(native_session_availability("shared", second), Availability.AVAILABLE)

    def test_snapshot_content_and_checkpoint_share_one_transaction(self) -> None:
        ref = self.fixture.put("shr-a", Turn("user", "before"))
        before = self.fixture.checkpoint(ref)
        self.fixture.during_snapshot = lambda: self.fixture.append(
            "shr-a", Turn("assistant", "committed concurrently")
        )
        with REGISTRY.temporary(self.fixture.adapter()):
            snapshot = read_snapshot("shared", ref)
        self.assertEqual([turn.text for turn in snapshot.transcript.turns], ["before"])
        self.assertEqual(snapshot.checkpoint, before)
        with REGISTRY.temporary(self.fixture.adapter()):
            self.assertEqual(
                conversation_change_status("shared", ref, snapshot.checkpoint), "changed"
            )

    def test_shared_members_route_heads_by_id_not_storage(self) -> None:
        first = self.fixture.put("shr-a", Turn("user", "alpha"))
        second = self.fixture.put("shr-b", Turn("user", "beta"))
        first_checkpoint = self.fixture.checkpoint(first)
        second_checkpoint = self.fixture.checkpoint(second)
        state = UserState(self.root / "state.json")
        state.conversations = {
            "conversation": {
                "members": {
                    "shared:shr-a": {
                        "tool": "shared",
                        "session_id": "shr-a",
                        "storage": str(self.fixture.path),
                        "generation": 0,
                        "frontier": "same",
                        "checkpoint": first_checkpoint,
                    },
                    "shared:shr-b": {
                        "tool": "shared",
                        "session_id": "shr-b",
                        "storage": str(self.fixture.path),
                        "generation": 0,
                        "frontier": "same",
                        "checkpoint": second_checkpoint,
                    },
                }
            }
        }
        self.fixture.append("shr-a", Turn("assistant", "alpha advanced"))
        with REGISTRY.temporary(self.fixture.adapter()):
            status = state._conversation_status("conversation")
        self.assertEqual([key for key, _ in status["advanced"]], ["shared:shr-a"])
        self.assertEqual([key for key, _ in status["heads"]], ["shared:shr-a"])

    def test_conversation_status_reuses_one_availability_probe_per_member(self) -> None:
        ref = self.fixture.put("shr-a", Turn("user", "alpha"))
        checkpoint = self.fixture.checkpoint(ref)
        calls = 0
        original = self.fixture.availability

        def counted(candidate: NativeRef) -> Availability:
            nonlocal calls
            calls += 1
            return original(candidate)

        adapter = replace(self.fixture.adapter(), availability=counted)
        state = UserState(self.root / "state.json")
        state.conversations = {
            "conversation": {
                "members": {
                    "shared:shr-a": {
                        "tool": "shared",
                        "session_id": "shr-a",
                        "storage": ref.storage,
                        "generation": 0,
                        "frontier": "head",
                        "checkpoint": checkpoint,
                    }
                }
            }
        }
        with REGISTRY.temporary(adapter):
            status = state._conversation_status("conversation")
        self.assertEqual(status["heads"][0][0], "shared:shr-a")
        self.assertEqual(calls, 1)

    def test_bridge_results_with_shared_storage_retain_distinct_native_ids(self) -> None:
        sources = []
        for index in range(2):
            path = self.root / f"source-{index}.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"request {index}"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            sources.append(path)
        with REGISTRY.temporary(self.fixture.adapter()):
            results = [
                bridge(
                    source_tool="codex",
                    target_tool="shared",
                    session_id=f"source-{index}",
                    storage=str(path),
                    cwd=str(self.root),
                )
                for index, path in enumerate(sources)
            ]
        self.assertEqual(results[0].storage, results[1].storage)
        self.assertNotEqual(results[0].session_id, results[1].session_id)

    def test_preparation_uses_maintenance_command_without_launch_mode_flags(self) -> None:
        source_path = self.root / "source.jsonl"
        source_path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "carry me"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source = Session(
            "codex",
            "source",
            "Source",
            str(self.root),
            1,
            1,
            "carry me",
            True,
            str(source_path),
            launch_tool="shared",
        )
        config = LaunchConfig(
            mode="dangerous",
            providers={
                "shared": ProviderProfile(
                    ["configured-shared"], ["--custom-only"], {"bridge_model": "fixture/model"}
                )
            },
        )
        with REGISTRY.temporary(self.fixture.adapter()):
            prepared, _ = prepare_launch(source, config=config)
            command = command_for(prepared, config)
        self.assertEqual(self.fixture.prepared_commands, [("configured-shared",)])
        self.assertEqual(self.fixture.prepared_options, [{"bridge_model": "fixture/model"}])
        self.assertEqual(command[:2], ["configured-shared", "--unsafe"])

    def test_maintenance_paths_structurally_never_use_launch_prefix(self) -> None:
        import ai_sessions.app as app_module
        import ai_sessions.conversion as conversion_module

        maintenance_source = inspect.getsource(app_module.prepare_launch) + inspect.getsource(
            conversion_module.bridge
        )
        self.assertNotIn("provider_prefix", maintenance_source)
        self.assertIn("provider_command", inspect.getsource(app_module.prepare_launch))

    def test_direct_bridge_prepares_the_adapter_default_command_once(self) -> None:
        source = self.root / "source.jsonl"
        source.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "source"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with REGISTRY.temporary(self.fixture.adapter()):
            bridge(
                source_tool="codex",
                target_tool="shared",
                session_id="source",
                storage=str(source),
                cwd=str(self.root),
            )
        self.assertEqual(self.fixture.prepared_commands, [("shared-cli",)])
        self.assertEqual(self.fixture.prepared_options, [{}])

    def test_direct_budget_cannot_bypass_a_prepared_target_hard_limit(self) -> None:
        source = self.root / "hard-limit-source.jsonl"
        source.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "source"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        prepared = PreparedTarget(
            ("shared-cli",),
            BudgetPolicy(5_000, 1.0, 2.0, "fixture hard limit", hard_limit=True),
        )
        with (
            REGISTRY.temporary(self.fixture.adapter()),
            self.assertRaisesRegex(
                BridgeError, "exceeds the prepared Shared Store Harness target limit"
            ),
        ):
            bridge(
                source_tool="codex",
                target_tool="shared",
                session_id="source",
                storage=str(source),
                cwd=str(self.root),
                prepared_target=prepared,
                budget=Budget("shared", 6_000, 12_000, "test"),
            )

    def test_schema_six_producer_and_persistence_preserve_checkpoint_shapes(self) -> None:
        path = self.root / "state.json"
        source_path = self.root / "zero.jsonl"
        source_path.write_bytes(b"")
        target = self.fixture.put("shr-opaque", Turn("user", "opaque"))
        checkpoint = self.fixture.checkpoint(target)
        state = UserState(path)
        source = Session("codex", "zero", "", str(self.root), 0, 0, "", False, str(source_path))
        with REGISTRY.temporary(self.fixture.adapter()):
            state.set_bridge(
                source,
                "shared",
                target.session_id,
                target.storage,
                source_checkpoint=0,
                target_checkpoint=checkpoint,
            )
        state.save()
        saved = json.loads(path.read_text(encoding="utf-8"))
        conversation_id = saved["session_conversations"]["codex:zero"]
        members = saved["conversations"][conversation_id]["members"]
        self.assertEqual(members["codex:zero"]["checkpoint"], 0)
        self.assertEqual(members["codex:zero"]["cursor"], 0)
        self.assertEqual(members["shared:shr-opaque"]["checkpoint"], checkpoint)
        self.assertNotIn("cursor", members["shared:shr-opaque"])

    def test_missing_and_corrupt_storage_are_distinct_without_creation(self) -> None:
        missing = self.root / "missing.sqlite"
        corrupt = self.root / "corrupt.sqlite"
        corrupt.write_bytes(b"not sqlite")
        with REGISTRY.temporary(self.fixture.adapter()):
            self.assertEqual(
                native_session_availability("shared", NativeRef("shr-a", str(missing))),
                Availability.UNAVAILABLE,
            )
            self.assertFalse(missing.exists())
            self.assertEqual(
                native_session_availability("shared", NativeRef("shr-a", str(corrupt))),
                Availability.UNKNOWN,
            )

    def test_builtin_stat_failures_are_unknown_not_unavailable(self) -> None:
        from ai_sessions.harnesses import claude, codex

        ref = NativeRef("session", str(self.root / "locked.jsonl"))
        with patch.object(Path, "stat", side_effect=PermissionError("locked")):
            self.assertEqual(claude._claude_availability(ref), Availability.UNKNOWN)
            self.assertEqual(codex._codex_availability(ref), Availability.UNKNOWN)

    def test_bare_storage_cannot_be_mistaken_for_native_identity(self) -> None:
        self.fixture.put("shr-a", Turn("user", "alpha"))
        with (
            REGISTRY.temporary(self.fixture.adapter()),
            self.assertRaisesRegex(TypeError, "explicit NativeRef"),
        ):
            read_transcript("shared", str(self.fixture.path))  # type: ignore[arg-type]

    def test_unsupported_availability_fails_with_capability_reason(self) -> None:
        ref = self.fixture.put("shr-a", Turn("user", "alpha"))
        unsupported = replace(
            self.fixture.adapter(),
            availability=Unsupported("shared adapter has no exact row probe"),
        )
        with (
            REGISTRY.temporary(unsupported),
            self.assertRaisesRegex(BridgeError, "shared adapter has no exact row probe"),
        ):
            read_snapshot("shared", ref)

    def test_invalid_availability_fails_closed_and_blocks_head_promotion(self) -> None:
        ref = self.fixture.put("shr-a", Turn("user", "alpha"))
        checkpoint = self.fixture.checkpoint(ref)
        state = UserState(self.root / "state.json")
        state.conversations = {
            "conversation": {
                "members": {
                    "shared:shr-a": {
                        "tool": "shared",
                        "session_id": "shr-a",
                        "storage": ref.storage,
                        "generation": 1,
                        "frontier": "head",
                        "checkpoint": checkpoint,
                    }
                }
            }
        }
        malformed = replace(self.fixture.adapter(), availability=lambda _ref: True)
        with REGISTRY.temporary(malformed):
            status = state._conversation_status("conversation")
        self.assertEqual([key for key, _ in status["unknown"]], ["shared:shr-a"])
        self.assertEqual(status["heads"], [])

    def test_invalid_read_and_write_checkpoints_fail_at_adapter_boundary(self) -> None:
        ref = self.fixture.put("shr-a", Turn("user", "alpha"))

        def read_with_invalid_checkpoint(
            _ref: NativeRef, *, latest_window: bool = True
        ) -> ReadSnapshot:
            del latest_window
            return ReadSnapshot(Transcript([Turn("user", "alpha")]), 1.5)  # type: ignore[arg-type]

        invalid_reader = replace(
            self.fixture.adapter(),
            read=read_with_invalid_checkpoint,
        )
        with (
            REGISTRY.temporary(invalid_reader),
            self.assertRaisesRegex(BridgeError, "invalid native read checkpoint"),
        ):
            read_snapshot("shared", ref)

        original_writer = self.fixture.write

        def invalid_writer(**values: object) -> NativeWrite:
            written = original_writer(**values)  # type: ignore[arg-type]
            return NativeWrite(written.native, 1.5)  # type: ignore[arg-type]

        invalid_target = replace(self.fixture.adapter(), write=invalid_writer)
        source = self.root / "source-invalid-writer.jsonl"
        source.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "source"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with (
            REGISTRY.temporary(invalid_target),
            self.assertRaisesRegex(BridgeError, "invalid native write result"),
        ):
            bridge(
                source_tool="codex",
                target_tool="shared",
                session_id="source",
                storage=str(source),
                cwd=str(self.root),
            )

    def test_missing_database_and_deleted_row_fail_as_bridge_errors(self) -> None:
        missing = NativeRef("shr-missing", str(self.root / "missing.sqlite"))
        deleted = self.fixture.put("shr-deleted", Turn("user", "gone"))
        self.fixture.delete(deleted.session_id)
        with REGISTRY.temporary(self.fixture.adapter()):
            for ref in (missing, deleted):
                with self.subTest(ref=ref), self.assertRaises(BridgeError):
                    read_snapshot("shared", ref)
            with self.assertRaises(BridgeError):
                bridge(
                    source_tool="shared",
                    target_tool="codex",
                    session_id=deleted.session_id,
                    storage=deleted.storage,
                    cwd=str(self.root),
                )

    def test_unknown_saved_copy_is_not_treated_as_missing_or_rebridged(self) -> None:
        source_path = self.root / "source.jsonl"
        source_path.write_bytes(b"")
        source = Session("codex", "source", "", str(self.root), 0, 0, "", False, str(source_path))
        state = UserState(self.root / "state.json")
        state.bridges = {
            source.key: {
                "shared": {
                    "session_id": "shr-a",
                    "storage": str(self.fixture.path),
                    "source_updated": 0,
                }
            }
        }
        unknown = replace(self.fixture.adapter(), availability=lambda _ref: Availability.UNKNOWN)
        with (
            REGISTRY.temporary(unknown),
            self.assertRaisesRegex(BridgeError, "refusing to create a duplicate"),
        ):
            state.bridge_for(source, "shared")
        self.assertFalse(native_session_exists("shared", "", str(self.fixture.path)))

    def test_core_never_orders_or_performs_arithmetic_on_checkpoints(self) -> None:
        import ai_sessions.app as app_module
        import ai_sessions.conversion as conversion_module

        for module in (app_module, conversion_module):
            tree = ast.parse(inspect.getsource(module))

            def is_checkpoint_expression(expression: ast.AST) -> bool:
                if isinstance(expression, ast.Name):
                    return expression.id.endswith(("checkpoint", "cursor"))
                if isinstance(expression, ast.Attribute):
                    return expression.attr.endswith(("checkpoint", "cursor"))
                if isinstance(expression, ast.Subscript):
                    key = expression.slice
                    return isinstance(key, ast.Constant) and key.value in ("checkpoint", "cursor")
                if (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Attribute)
                    and expression.func.attr == "get"
                    and expression.args
                ):
                    key = expression.args[0]
                    return isinstance(key, ast.Constant) and key.value in (
                        "checkpoint",
                        "cursor",
                    )
                return False

            for node in ast.walk(tree):
                if isinstance(node, (ast.BinOp, ast.AugAssign)):
                    self.assertFalse(
                        any(is_checkpoint_expression(child) for child in ast.walk(node))
                    )
                if isinstance(node, ast.Compare):
                    operands = (node.left, *node.comparators)
                    if any(is_checkpoint_expression(operand) for operand in operands):
                        self.assertFalse(
                            any(
                                isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                                for operator in node.ops
                            )
                        )
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                    self.assertFalse(is_checkpoint_expression(node.operand))
                if isinstance(node, ast.BoolOp):
                    self.assertFalse(any(is_checkpoint_expression(value) for value in node.values))
                if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                    self.assertFalse(is_checkpoint_expression(node.test))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "bool"
                ):
                    self.assertFalse(
                        any(is_checkpoint_expression(argument) for argument in node.args)
                    )


if __name__ == "__main__":
    unittest.main()
