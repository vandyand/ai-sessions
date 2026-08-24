import ast
import contextlib
import gc
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psutil

from ai_sessions import diagnostics
from ai_sessions.app import Session, available_launch_tools, can_bridge, load_sessions
from ai_sessions.capabilities import Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.conversion import BridgeError
from ai_sessions.discovery import MAX_EVIDENCE_IDS, HarnessContext
from ai_sessions.harnesses import opencode
from ai_sessions.model import Availability, NativeRef, NativeSession, SourceKind
from ai_sessions.registry import REGISTRY

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    directory TEXT NOT NULL,
    title TEXT NOT NULL,
    agent TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    time_archived INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


def create_database(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def add_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    title: str = "New session - 2026-08-23T12:00:00.000Z",
    parent_id: str | None = None,
    archived: bool = False,
    directory: str = "/work",
    agent: str = "build",
    created: int = 1_700_000_000_000,
    updated: int = 1_700_000_001_000,
) -> None:
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            parent_id,
            directory,
            title,
            agent,
            created,
            updated,
            updated if archived else None,
        ),
    )


def add_message(
    connection: sqlite3.Connection,
    session_id: str,
    message_id: str,
    role: str,
    *parts: dict[str, object] | str,
    created: int,
) -> None:
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (message_id, session_id, created, json.dumps({"role": role})),
    )
    for index, part in enumerate(parts):
        payload = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (f"prt-{message_id}-{index}", message_id, session_id, created + index, payload),
        )


def db_path_command(path: Path, *, returncode: int = 0, extra_output: str = "") -> tuple[str, ...]:
    code = (
        "import sys; "
        f"sys.stdout.write({str(path)!r} + '\\n' + {extra_output!r}); "
        f"sys.exit({returncode})"
    )
    return (sys.executable, "-c", code)


@contextlib.contextmanager
def opencode_home(path: Path):
    global_binding = opencode._LAST_BINDING
    opencode._LAST_BINDING = None
    authoritative_bindings = dict(opencode._AUTHORITATIVE_BINDINGS)
    opencode._AUTHORITATIVE_BINDINGS.clear()
    with opencode._DISCOVERY_CACHE_LOCK:
        cached_discovery = dict(opencode._DISCOVERY_CACHE)
        opencode._DISCOVERY_CACHE.clear()
    with REGISTRY.temporary(replace(REGISTRY.get("opencode"), home=path)):
        try:
            yield
        finally:
            opencode._LAST_BINDING = global_binding
            opencode._AUTHORITATIVE_BINDINGS.clear()
            opencode._AUTHORITATIVE_BINDINGS.update(authoritative_bindings)
            with opencode._DISCOVERY_CACHE_LOCK:
                opencode._DISCOVERY_CACHE.clear()
                opencode._DISCOVERY_CACHE.update(cached_discovery)


class OpenCodeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        diagnostics.clear_warnings()

    def tearDown(self) -> None:
        diagnostics.clear_warnings()
        self.temporary.cleanup()

    def populated_database(self, path: Path) -> None:
        connection = create_database(path)
        try:
            add_session(connection, "ses_000000000001AAAAAAAAAAAAAA")
            add_message(
                connection,
                "ses_000000000001AAAAAAAAAAAAAA",
                "msg-root-1",
                "user",
                {"type": "text", "text": "first root prompt"},
                created=1_700_000_000_100,
            )
            add_message(
                connection,
                "ses_000000000001AAAAAAAAAAAAAA",
                "msg-root-2",
                "user",
                {"type": "text", "text": "latest"},
                {"type": "text", "text": "root prompt"},
                created=1_700_000_000_200,
            )
            add_session(
                connection,
                "ses_000000000002BBBBBBBBBBBBBB",
                parent_id="ses_000000000001AAAAAAAAAAAAAA",
                title="Useful child",
                archived=True,
                directory="/child",
                agent="explore",
            )
            add_message(
                connection,
                "ses_000000000002BBBBBBBBBBBBBB",
                "msg-child",
                "user",
                {"type": "text", "text": "child prompt"},
                created=1_700_000_000_300,
            )
            connection.execute(
                "UPDATE session SET time_archived=0 WHERE id=?",
                ("ses_000000000002BBBBBBBBBBBBBB",),
            )
            connection.commit()
        finally:
            connection.close()

    def context(self, database: Path) -> HarnessContext:
        return HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": db_path_command(database)},
        )

    def test_authoritative_database_discovers_roots_children_and_archives(self) -> None:
        database = self.root / "active" / "opencode.db"
        self.populated_database(database)
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(
            [row.session_id for row in rows],
            ["ses_000000000002BBBBBBBBBBBBBB", "ses_000000000001AAAAAAAAAAAAAA"],
        )
        by_id = {row.session_id: row for row in rows}
        root = by_id["ses_000000000001AAAAAAAAAAAAAA"]
        child = by_id["ses_000000000002BBBBBBBBBBBBBB"]
        self.assertEqual(root.title, "first root prompt")
        self.assertFalse(root.named)
        self.assertEqual(root.preview, "latest root prompt")
        self.assertEqual(root.message_count, 2)
        self.assertEqual(root.source, SourceKind.INTERACTIVE)
        self.assertEqual(root.storage, str(database.resolve()))
        self.assertEqual(child.title, "Useful child")
        self.assertTrue(child.named)
        self.assertTrue(child.archived)
        self.assertEqual(child.source, SourceKind.SUBAGENT)
        self.assertEqual(child.parent_id, root.session_id)
        self.assertEqual(child.agent_nickname, "explore")

    def test_configured_command_beats_adjacent_and_default_databases(self) -> None:
        authoritative = self.root / "channel" / "active.db"
        adjacent = self.root / "channel" / "opencode-beta.db"
        fallback_home = self.root / "data" / "opencode"
        for path, session_id in (
            (authoritative, "ses_000000000001AAAAAAAAAAAAAA"),
            (adjacent, "ses_000000000002BBBBBBBBBBBBBB"),
            (fallback_home / "opencode.db", "ses_000000000003CCCCCCCCCCCCCC"),
        ):
            connection = create_database(path)
            add_session(connection, session_id, title=session_id)
            connection.commit()
            connection.close()
        with opencode_home(fallback_home):
            rows = opencode.discover(self.context(authoritative), use_cache=False)
        self.assertEqual([row.session_id for row in rows], ["ses_000000000001AAAAAAAAAAAAAA"])

    def test_db_path_is_invoked_once_per_discovery_context(self) -> None:
        database = self.root / "active.db"
        self.populated_database(database)
        context = self.context(database)
        with (
            opencode_home(self.root / "fallback"),
            patch.object(opencode, "run_maintenance", wraps=opencode.run_maintenance) as invoked,
        ):
            opencode.discover(context, use_cache=False)
            opencode._binding(context)
        self.assertEqual(invoked.call_count, 1)
        self.assertEqual(invoked.call_args.args[1], ("db", "path"))

    def test_unchanged_store_uses_cache_and_wal_change_invalidates_it(self) -> None:
        database = self.root / "cached.db"
        self.populated_database(database)
        writer = sqlite3.connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        try:
            with (
                opencode_home(self.root / "fallback"),
                patch.object(opencode, "_read_data", wraps=opencode._read_data) as reads,
            ):
                first = opencode.discover(context(), use_cache=True)
                first_reads = reads.call_count
                self.assertEqual(len(opencode._DISCOVERY_CACHE), 0)
                second = opencode.discover(context(), use_cache=True)
                populated_reads = reads.call_count
                self.assertEqual(len(opencode._DISCOVERY_CACHE), 1)
                third = opencode.discover(context(), use_cache=True)
                self.assertEqual(reads.call_count, populated_reads)
                add_session(writer, "ses_000000000003CCCCCCCCCCCCCC", title="New row")
                writer.commit()
                fourth = opencode.discover(context(), use_cache=True)
        finally:
            writer.close()
        self.assertGreater(first_reads, 0)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(fourth), len(first) + 1)
        self.assertGreater(populated_reads, first_reads)
        self.assertGreater(reads.call_count, populated_reads)

    def test_commit_between_snapshot_pin_and_stamp_is_never_cached(self) -> None:
        database = self.root / "snapshot-race.db"
        self.populated_database(database)
        writer = sqlite3.connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        schema_columns = opencode._schema_columns
        committed = False

        def pin_then_commit(connection: sqlite3.Connection):
            nonlocal committed
            result = schema_columns(connection)
            if not committed:
                committed = True
                add_session(writer, "ses_000000000003CCCCCCCCCCCCCC", title="Committed row")
                writer.commit()
            return result

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        try:
            with (
                opencode_home(self.root / "fallback"),
                patch.object(opencode, "_schema_columns", side_effect=pin_then_commit),
            ):
                pinned = opencode.discover(context(), use_cache=True)
                self.assertEqual(len(opencode._DISCOVERY_CACHE), 0)
            with opencode_home(self.root / "fallback"):
                current = opencode.discover(context(), use_cache=True)
        finally:
            writer.close()
        self.assertEqual(len(pinned), 2)
        self.assertEqual(len(current), 3)

    def test_commit_after_snapshot_pin_prevents_post_scan_cache_population(self) -> None:
        database = self.root / "post-scan-race.db"
        self.populated_database(database)
        writer = sqlite3.connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        original_read = opencode._read_data
        committed = False

        def commit_during_scan(*args, **kwargs):
            nonlocal committed
            if not committed:
                committed = True
                add_session(writer, "ses_000000000003CCCCCCCCCCCCCC", title="Later row")
                writer.commit()
            return original_read(*args, **kwargs)

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        try:
            with opencode_home(self.root / "fallback"):
                # Establish stable WAL sidecars without populating the cache.
                opencode.discover(context(), use_cache=False)
                with patch.object(opencode, "_read_data", side_effect=commit_during_scan):
                    pinned = opencode.discover(context(), use_cache=True)
                self.assertEqual(len(opencode._DISCOVERY_CACHE), 0)
                current = opencode.discover(context(), use_cache=True)
        finally:
            writer.close()
        self.assertEqual(len(pinned), 2)
        self.assertEqual(len(current), 3)

    def test_cached_pass_replays_evidence_and_warning_categories(self) -> None:
        database = self.root / "cache-replay.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        foreign_id = "019abcde-1234-5678-9abc-0123456789ab"
        add_session(connection, session_id)
        add_message(
            connection,
            session_id,
            "msg-valid",
            "user",
            {"type": "text", "text": f"remember {foreign_id}"},
            created=1,
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg-malformed", session_id, 2, "{bad"),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "prt-oversized",
                "msg-malformed",
                session_id,
                2,
                json.dumps({"type": "output", "text": "x" * opencode.DISCOVERY_JSON_BYTES}),
            ),
        )
        connection.commit()
        connection.close()

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        with opencode_home(self.root / "fallback"):
            first_context = context()
            first = opencode.discover(first_context, use_cache=True)
            first_warnings = diagnostics.warnings()
            self.assertEqual(len(opencode._DISCOVERY_CACHE), 1)
            diagnostics.clear_warnings()
            second_context = context()
            with patch.object(opencode, "_read_data", wraps=opencode._read_data) as reads:
                second = opencode.discover(second_context, use_cache=True)
            second_warnings = diagnostics.warnings()
        self.assertEqual(first, second)
        self.assertEqual(first_context.evidence, second_context.evidence)
        self.assertEqual(first_warnings, second_warnings)
        self.assertEqual(reads.call_count, 0)

    def test_registry_pattern_change_misses_discovery_cache(self) -> None:
        database = self.root / "pattern-cache.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id)
        add_message(
            connection,
            session_id,
            "msg-pattern",
            "assistant",
            {"type": "text", "text": "historical new-123"},
            created=1,
        )
        connection.commit()
        connection.close()

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        base = REGISTRY.get("codex")
        patterned = replace(
            base,
            name="patterned",
            order=999,
            id_patterns=(re.compile(rb"new-[0-9]+"),),
        )
        with opencode_home(self.root / "fallback"):
            original_context = context()
            opencode.discover(original_context, use_cache=True)
            self.assertNotIn("new-123", original_context.evidence[("opencode", session_id)][0])
            with (
                REGISTRY.temporary(patterned),
                patch.object(opencode, "_read_data", wraps=opencode._read_data) as reads,
            ):
                changed_context = context()
                opencode.discover(changed_context, use_cache=True)
        self.assertGreater(reads.call_count, 0)
        self.assertIn("new-123", changed_context.evidence[("opencode", session_id)][0])

    def test_main_header_change_invalidates_cache_when_size_and_mtime_match(self) -> None:
        database = self.root / "same-stat.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id, title="Alpha")
        connection.commit()
        connection.close()

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        with opencode_home(self.root / "fallback"):
            first = opencode.discover(context(), use_cache=True)
            metadata = database.stat()
            connection = sqlite3.connect(database)
            connection.execute("UPDATE session SET title='Bravo' WHERE id=?", (session_id,))
            connection.commit()
            connection.close()
            self.assertEqual(database.stat().st_size, metadata.st_size)
            os.utime(database, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            second = opencode.discover(context(), use_cache=True)
        self.assertEqual(first[0].title, "Alpha")
        self.assertEqual(second[0].title, "Bravo")

    def test_wal_index_frame_change_invalidates_same_size_same_mtime_cache(self) -> None:
        database = self.root / "same-wal-stat.db"
        connection = create_database(database, wal=True)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id, title="Alpha")
        connection.commit()
        connection.execute("PRAGMA wal_autocheckpoint=0")
        for index in range(120):
            connection.execute(
                "UPDATE session SET title=? WHERE id=?",
                ("Alpha" if index % 2 else "Delta", session_id),
            )
            connection.commit()
        connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
        connection.execute("UPDATE session SET title='Beta' WHERE id=?", (session_id,))
        connection.commit()

        def context() -> HarnessContext:
            return HarnessContext.create(
                use_cache=True,
                provider_commands={"opencode": db_path_command(database)},
            )

        try:
            with opencode_home(self.root / "fallback"):
                first = opencode.discover(context(), use_cache=True)
                before = opencode._store_stamp(database)
                self.assertIsNotNone(before)
                members = (database, Path(f"{database}-wal"))
                metadata = {path: path.stat() for path in members}
                connection.execute("UPDATE session SET title='Gamma' WHERE id=?", (session_id,))
                connection.commit()
                for path, item in metadata.items():
                    os.utime(path, ns=(item.st_atime_ns, item.st_mtime_ns))
                after = opencode._store_stamp(database)
                self.assertIsNotNone(after)
                assert before is not None and after is not None
                self.assertEqual(before[0], after[0])
                self.assertEqual(before[1], after[1])
                self.assertNotEqual(before[2], after[2])
                second = opencode.discover(context(), use_cache=True)
        finally:
            connection.close()
        self.assertEqual(first[0].title, "Beta")
        self.assertEqual(second[0].title, "Gamma")

    def test_command_failure_falls_back_only_to_explicit_override(self) -> None:
        home = self.root / "data" / "opencode"
        override = home / "chosen.db"
        adjacent = home / "opencode-beta.db"
        for path, session_id in (
            (override, "ses_000000000001AAAAAAAAAAAAAA"),
            (adjacent, "ses_000000000002BBBBBBBBBBBBBB"),
        ):
            connection = create_database(path)
            add_session(connection, session_id, title=session_id)
            connection.commit()
            connection.close()
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": ("definitely-missing-opencode",)},
        )
        with (
            opencode_home(home),
            patch.dict(os.environ, {"OPENCODE_DB": "chosen.db"}),
        ):
            rows = opencode.discover(context, use_cache=False)
        self.assertEqual([row.session_id for row in rows], ["ses_000000000001AAAAAAAAAAAAAA"])
        self.assertIn("binding is inferred", diagnostics.warnings()[0])

    def test_empty_override_uses_stable_default(self) -> None:
        home = self.root / "data" / "opencode"
        database = home / "opencode.db"
        self.populated_database(database)
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": ("definitely-missing-opencode",)},
        )
        with opencode_home(home), patch.dict(os.environ, {"OPENCODE_DB": ""}):
            rows = opencode.discover(context, use_cache=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.storage for row in rows}, {str(database.resolve())})

    def test_nonzero_ambiguous_and_memory_db_path_outputs_fall_back(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        commands = (
            db_path_command(self.root / "ignored.db", returncode=7),
            db_path_command(self.root / "ignored.db", extra_output="second\n"),
            db_path_command(Path(":memory:")),
        )
        for command in commands:
            with self.subTest(command=command):
                context = HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": command}
                )
                diagnostics.clear_warnings()
                with opencode_home(home), patch.dict(os.environ, {"OPENCODE_DB": ""}):
                    rows = opencode.discover(context, use_cache=False)
                self.assertEqual(len(rows), 2)
                self.assertEqual(len(diagnostics.warnings()), 1)

    def test_memory_and_uri_environment_overrides_are_ignored(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        for override in (":memory:", "file:other.db", "FILE:/absolute.db"):
            with self.subTest(override=override):
                context = HarnessContext.create(
                    use_cache=False,
                    provider_commands={"opencode": ("definitely-missing-opencode",)},
                )
                diagnostics.clear_warnings()
                with opencode_home(home), patch.dict(os.environ, {"OPENCODE_DB": override}):
                    rows = opencode.discover(context, use_cache=False)
                self.assertEqual(len(rows), 2)
                self.assertEqual({row.storage for row in rows}, {str(database.resolve())})

    def test_unexpandable_user_paths_fall_back_without_aborting_other_harnesses(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        unknown = "~ai_sessions_user_that_does_not_exist_98765"
        cases = (
            (
                {"OPENCODE_DB": f"{unknown}/store.db"},
                ("definitely-missing-opencode",),
                0,
            ),
            ({"OPENCODE_DB": ""}, (f"{unknown}/bin/opencode",), 2),
        )
        for environment, command, expected in cases:
            with self.subTest(command=command):
                diagnostics.clear_warnings()
                context = HarnessContext.create(
                    use_cache=False,
                    provider_commands={"opencode": command},
                )
                with opencode_home(home), patch.dict(os.environ, environment):
                    rows = opencode.discover(context, use_cache=False)
                self.assertEqual(len(rows), expected)
                self.assertTrue(
                    any("binding is inferred" in note for note in diagnostics.warnings())
                )

        def claude_discover(context: HarnessContext, *, use_cache: bool = True):
            del context, use_cache
            return [
                NativeSession(
                    tool="claude",
                    session_id="019abcde-1234-5678-9abc-0123456789ab",
                    title="Claude survives",
                    cwd="/work",
                    updated=2,
                    created=1,
                    preview="survives",
                    named=False,
                    storage=str(self.root / "claude.jsonl"),
                )
            ]

        config = LaunchConfig(
            providers={"opencode": ProviderProfile(command=[f"{unknown}/bin/opencode"])}
        )
        with (
            opencode_home(self.root / "absent-opencode"),
            REGISTRY.temporary(
                replace(REGISTRY.get("codex"), discover=Unsupported("isolated test"))
            ),
            REGISTRY.temporary(replace(REGISTRY.get("claude"), discover=claude_discover)),
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            loaded = load_sessions(use_cache=False, config=config)
        self.assertEqual(
            [(item.tool, item.title) for item in loaded], [("claude", "Claude survives")]
        )

    def test_missing_database_does_not_create_main_or_sidecars(self) -> None:
        database = self.root / "missing" / "opencode.db"
        database.parent.mkdir()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
            availability = opencode._availability(
                NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(database))
            )
        self.assertEqual(rows, [])
        self.assertEqual(availability, Availability.UNAVAILABLE)
        self.assertFalse(database.exists())
        self.assertFalse(database.with_name(database.name + "-wal").exists())
        self.assertFalse(database.with_name(database.name + "-shm").exists())

    def test_existing_clean_wal_store_only_gains_derived_sidecars(self) -> None:
        database = self.root / "clean-wal.db"
        connection = create_database(database, wal=True)
        try:
            add_session(connection, "ses_000000000001AAAAAAAAAAAAAA")
            connection.commit()
        finally:
            connection.close()
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        before = database.read_bytes()

        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)

        self.assertEqual(len(rows), 1)
        self.assertEqual(database.read_bytes(), before)
        self.assertTrue(wal.is_file())
        self.assertEqual(wal.stat().st_size, 0)
        self.assertTrue(shm.is_file())
        self.assertGreater(shm.stat().st_size, 0)

    def test_non_file_database_path_is_unknown_not_proven_absent(self) -> None:
        database = self.root / "directory.db"
        database.mkdir()
        ref = NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(database))
        self.assertEqual(opencode._availability(ref), Availability.UNKNOWN)
        with opencode_home(self.root / "fallback"):
            self.assertEqual(opencode.discover(self.context(database), use_cache=False), [])
        self.assertTrue(diagnostics.warnings())

    def test_incompatible_and_corrupt_databases_are_reported_not_empty(self) -> None:
        incompatible = self.root / "incompatible.db"
        sqlite3.connect(incompatible).close()
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not sqlite")
        for database in (incompatible, corrupt):
            with self.subTest(database=database):
                diagnostics.clear_warnings()
                with opencode_home(self.root / "fallback"):
                    rows = opencode.discover(self.context(database), use_cache=False)
                    availability = opencode._availability(
                        NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(database))
                    )
                self.assertEqual(rows, [])
                self.assertEqual(availability, Availability.UNKNOWN)
                self.assertTrue(diagnostics.warnings())

    def test_without_rowid_payload_table_is_reported_as_incompatible(self) -> None:
        database = self.root / "without-rowid.db"
        schema = SCHEMA.replace(
            "    data TEXT NOT NULL\n);\nCREATE TABLE part",
            "    data TEXT NOT NULL\n) WITHOUT ROWID;\nCREATE TABLE part",
            1,
        )
        self.assertNotEqual(schema, SCHEMA)
        connection = sqlite3.connect(database)
        connection.executescript(schema)
        connection.close()
        with opencode_home(self.root / "fallback"):
            self.assertEqual(opencode.discover(self.context(database), use_cache=False), [])
        self.assertIn("rowid-addressable", diagnostics.warnings()[0])

    def test_malformed_json_isolated_while_valid_rows_survive(self) -> None:
        database = self.root / "malformed.db"
        connection = create_database(database)
        add_session(connection, "ses_000000000001AAAAAAAAAAAAAA")
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "msg-bad",
                "ses_000000000001AAAAAAAAAAAAAA",
                1,
                "{bad",
            ),
        )
        add_message(
            connection,
            "ses_000000000001AAAAAAAAAAAAAA",
            "msg-good",
            "user",
            "{bad part",
            {"type": "text", "text": "survives"},
            created=2,
        )
        connection.execute(
            "UPDATE session SET title=CAST(X'41FF42' AS TEXT) "
            "WHERE id='ses_000000000001AAAAAAAAAAAAAA'"
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, CAST(? AS TEXT))",
            (
                "prt-invalid-utf8",
                "msg-bad",
                "ses_000000000001AAAAAAAAAAAAAA",
                1,
                sqlite3.Binary(b'{"type":"text","text":"\xff"}'),
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "msg-too-deep",
                "ses_000000000001AAAAAAAAAAAAAA",
                3,
                "[" * 20_000,
            ),
        )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(rows[0].preview, "survives")
        self.assertEqual(rows[0].message_count, 1)
        self.assertIn("malformed", diagnostics.warnings()[0])

    def test_orphan_part_is_reported_as_malformed(self) -> None:
        database = self.root / "orphan.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id)
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "prt-orphan",
                "msg-missing",
                session_id,
                1,
                json.dumps({"type": "text", "text": "orphan"}),
            ),
        )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("malformed" in warning for warning in diagnostics.warnings()))

    def test_orphan_message_is_reported_as_malformed(self) -> None:
        database = self.root / "orphan-message.db"
        connection = create_database(database)
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg-orphan", "ses_missing", 1, json.dumps({"role": "user"})),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "prt-orphan",
                "msg-orphan",
                "ses_missing",
                1,
                json.dumps({"type": "text", "text": "orphan"}),
            ),
        )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(rows, [])
        self.assertTrue(any("malformed" in warning for warning in diagnostics.warnings()))

    def test_invalid_utf8_session_ids_are_not_collapsed(self) -> None:
        database = self.root / "invalid-identifiers.db"
        connection = create_database(database)
        for suffix, title in ((b"\xff", "First"), (b"\xfe", "Second")):
            native_id = sqlite3.Binary(b"ses_000000000001AAAAAAAAAAAAA" + suffix)
            connection.execute(
                "INSERT INTO session VALUES (CAST(? AS TEXT), ?, ?, ?, ?, ?, ?, ?)",
                (native_id, None, "/work", title, "build", 1, 2, None),
            )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(rows, [])
        self.assertTrue(any("malformed" in warning for warning in diagnostics.warnings()))

    def test_wal_commits_are_visible_without_checkpointing(self) -> None:
        database = self.root / "wal.db"
        writer = create_database(database, wal=True)
        try:
            add_session(writer, "ses_000000000001AAAAAAAAAAAAAA")
            writer.commit()
            self.assertTrue(database.with_name(database.name + "-wal").exists())
            with opencode_home(self.root / "fallback"):
                rows = opencode.discover(self.context(database), use_cache=False)
        finally:
            writer.close()
        self.assertEqual([row.session_id for row in rows], ["ses_000000000001AAAAAAAAAAAAAA"])

    def test_readonly_connection_does_not_checkpoint_or_remove_abandoned_wal(self) -> None:
        database = self.root / "abandoned-wal.db"
        code = (
            "import os,sqlite3,sys; "
            "c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); "
            "c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute('CREATE TABLE item(value TEXT)'); "
            "c.execute(\"INSERT INTO item VALUES ('visible')\"); c.commit(); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", code, str(database)], check=True, timeout=5)
        paths = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        self.assertTrue(paths[1].exists())
        before = {path: path.read_bytes() for path in paths if path.exists()}
        connection = opencode._connect_existing(database)
        try:
            self.assertEqual(connection.execute("SELECT value FROM item").fetchone(), ("visible",))
        finally:
            connection.close()
        after = {path: path.read_bytes() for path in paths if path.exists()}
        self.assertEqual(after[database], before[database])
        self.assertEqual(after[paths[1]], before[paths[1]])
        self.assertEqual(set(after), set(before))
        self.assertEqual(len(after[paths[2]]), len(before[paths[2]]))

    def test_exact_availability_and_resolution_never_rebind(self) -> None:
        primary = self.root / "primary.db"
        other = self.root / "other.db"
        for database in (primary, other):
            connection = create_database(database)
            add_session(connection, "ses_000000000001AAAAAAAAAAAAAA")
            connection.commit()
            connection.close()
        context = self.context(primary)
        with opencode_home(self.root / "fallback"):
            opencode.discover(context, use_cache=False)
            primary_ref = NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(primary))
            other_ref = NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(other))
            self.assertEqual(opencode._availability(primary_ref), Availability.AVAILABLE)
            self.assertEqual(opencode._availability(other_ref), Availability.AVAILABLE)
            connection = sqlite3.connect(primary)
            connection.execute(
                "DELETE FROM session WHERE id=?", ("ses_000000000001AAAAAAAAAAAAAA",)
            )
            connection.commit()
            connection.close()
            self.assertEqual(opencode._availability(primary_ref), Availability.UNAVAILABLE)
            self.assertIsNone(opencode._resolve(primary_ref.session_id))
            self.assertEqual(opencode._availability(other_ref), Availability.AVAILABLE)

    def test_resolve_without_a_discovery_binding_does_not_guess(self) -> None:
        with patch.object(opencode, "_LAST_BINDING", None):
            self.assertIsNone(opencode._resolve("ses_000000000001AAAAAAAAAAAAAA"))

    def test_cross_harness_evidence_is_bounded_and_reports_truncation(self) -> None:
        database = self.root / "evidence.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id)
        candidates = [f"ses_{index:012x}{'A' * 14}" for index in range(MAX_EVIDENCE_IDS + 1)]
        parts = [
            {"type": "text", "text": " ".join(candidates[offset : offset + 1_000])}
            for offset in range(0, len(candidates), 1_000)
        ]
        add_message(
            connection,
            session_id,
            "msg-evidence",
            "user",
            *parts,
            created=1,
        )
        connection.commit()
        connection.close()
        context = self.context(database)
        with opencode_home(self.root / "fallback"):
            opencode.discover(context, use_cache=False)
        tokens, truncated = context.evidence[("opencode", session_id)]
        self.assertEqual(len(tokens), MAX_EVIDENCE_IDS)
        self.assertTrue(truncated)

    def test_cross_harness_evidence_deduplicates_overlapping_patterns_and_rows(self) -> None:
        database = self.root / "overlap.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        foreign_id = "019abcde-1234-5678-9abc-0123456789ab"
        add_session(connection, session_id)
        add_message(
            connection,
            session_id,
            "msg-overlap",
            "user",
            {"type": "text", "text": f"twice {foreign_id} {foreign_id}"},
            created=1,
        )
        connection.commit()
        connection.close()
        context = self.context(database)
        with opencode_home(self.root / "fallback"):
            opencode.discover(context, use_cache=False)
        tokens, truncated = context.evidence[("opencode", session_id)]
        self.assertEqual(tokens, [foreign_id])
        self.assertFalse(truncated)

    def test_cross_harness_id_spanning_blob_chunks_is_not_lost(self) -> None:
        database = self.root / "chunk-boundary.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        foreign_id = "019abcde-1234-5678-9abc-0123456789ab"
        add_session(connection, session_id)
        template = json.dumps({"type": "text", "text": foreign_id})
        token_offset = template.index(foreign_id)
        padding = 65_536 - token_offset - 8
        payload = json.dumps({"type": "text", "text": "x" * padding + foreign_id})
        self.assertLess(payload.index(foreign_id), 65_536)
        self.assertGreater(payload.index(foreign_id) + len(foreign_id), 65_536)
        add_message(
            connection,
            session_id,
            "msg-boundary",
            "assistant",
            payload,
            created=1,
        )
        connection.commit()
        connection.close()
        context = self.context(database)
        with opencode_home(self.root / "fallback"):
            opencode.discover(context, use_cache=False)
        tokens, truncated = context.evidence[("opencode", session_id)]
        self.assertIn(foreign_id, tokens)
        self.assertFalse(truncated)

    def test_cross_harness_id_cannot_span_independent_rows(self) -> None:
        database = self.root / "cross-row.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        candidate = "ses_" + "A" * 26
        add_session(connection, session_id)
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg-cross-row", session_id, 1, json.dumps({"role": "assistant"})),
        )
        split = 15
        for index, payload in enumerate(("prefix " + candidate[:split], candidate[split:] + " x")):
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                (f"prt-{index}", "msg-cross-row", session_id, index, payload),
            )
        connection.commit()
        connection.close()
        context = self.context(database)
        with opencode_home(self.root / "fallback"):
            opencode.discover(context, use_cache=False)
        tokens, _ = context.evidence[("opencode", session_id)]
        self.assertNotIn(candidate, tokens)

    def test_large_blob_bounds_preview_and_evidence_bytes(self) -> None:
        database = self.root / "large-blob.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        foreign_id = "019abcde-1234-5678-9abc-0123456789ab"
        add_session(connection, session_id)
        add_message(
            connection,
            session_id,
            "msg-large",
            "user",
            {
                "type": "text",
                "text": "x" * (opencode.DISCOVERY_JSON_BYTES + 100) + foreign_id,
            },
            created=1,
        )
        connection.commit()
        connection.close()
        context = self.context(database)
        with (
            opencode_home(self.root / "fallback"),
            patch.object(opencode, "OPENCODE_EVIDENCE_BYTES", 65_536),
        ):
            rows = opencode.discover(context, use_cache=False)
        tokens, truncated = context.evidence[("opencode", session_id)]
        self.assertNotIn(foreign_id, tokens)
        self.assertTrue(truncated)
        self.assertLessEqual(len(rows[0].title), opencode.DISCOVERY_PREVIEW_CHARS)
        self.assertLessEqual(len(rows[0].preview), opencode.DISCOVERY_PREVIEW_CHARS)
        self.assertTrue(any("oversized" in note for note in diagnostics.warnings()))
        self.assertFalse(any("malformed" in note for note in diagnostics.warnings()))
        evidence_limit = 1_048_576
        accumulator = context.accumulator(max_bytes=evidence_limit)
        accumulator.scan(b"x" * (evidence_limit + 1))
        self.assertEqual(accumulator.bytes_scanned, evidence_limit)
        self.assertTrue(accumulator.truncated)

    def test_streaming_discovery_peak_does_not_scale_with_message_count(self) -> None:
        def peak(messages: int) -> int:
            database = self.root / f"large-{messages}.db"
            connection = create_database(database)
            session_id = "ses_000000000001AAAAAAAAAAAAAA"
            add_session(connection, session_id)
            for index in range(messages):
                add_message(
                    connection,
                    session_id,
                    f"msg-{index:06d}",
                    "user",
                    {"type": "text", "text": "x" * 32},
                    created=index,
                )
            connection.commit()
            connection.close()
            context = self.context(database)
            tracemalloc.start()
            try:
                with opencode_home(self.root / "fallback"):
                    opencode.discover(context, use_cache=False)
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

        small = peak(50)
        large = peak(2_000)
        self.assertLess(large, small * 5 + 1_000_000)

    def test_discovery_streams_message_and_part_tables_without_a_payload_join(self) -> None:
        database = self.root / "query-shape.db"
        self.populated_database(database)
        statements: list[str] = []
        connect = opencode._connect_existing

        def traced(path: Path) -> sqlite3.Connection:
            connection = connect(path)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            opencode_home(self.root / "fallback"),
            patch.object(opencode, "_connect_existing", side_effect=traced),
        ):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(len(rows), 2)
        selects = [statement for statement in statements if statement.startswith("SELECT")]
        self.assertTrue(any("FROM message" in statement for statement in selects))
        self.assertTrue(any("FROM part" in statement for statement in selects))
        self.assertFalse(any("JOIN part" in statement for statement in selects))

    def test_large_single_row_has_a_bounded_resident_memory_delta(self) -> None:
        database = self.root / "large-row.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id)
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg-large-rss", session_id, 1, json.dumps({"role": "assistant"})),
        )
        payload = json.dumps({"type": "text", "text": "x" * (64 * 1024 * 1024)})
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt-large-rss", "msg-large-rss", session_id, 1, payload),
        )
        connection.commit()
        connection.close()
        del payload
        gc.collect()
        process = psutil.Process()
        samples = [process.memory_info().rss]
        stopped = threading.Event()

        def sample() -> None:
            while not stopped.wait(0.002):
                samples.append(process.memory_info().rss)

        sampler = threading.Thread(target=sample)
        sampler.start()
        started = time.monotonic()
        try:
            with opencode_home(self.root / "fallback"):
                rows = opencode.discover(self.context(database), use_cache=False)
        finally:
            stopped.set()
            sampler.join()
        samples.append(process.memory_info().rss)
        self.assertEqual(len(rows), 1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertLess(max(samples) - samples[0], 64 * 1024 * 1024)

    def test_maintenance_runner_is_noninteractive_and_bounds_output(self) -> None:
        command = (
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('x'*100); "
            "sys.stderr.write('stdin=' + repr(data))",
        )
        with patch.object(opencode, "MAINTENANCE_OUTPUT_BYTES", 32):
            result = opencode.run_maintenance(command, (), cwd=str(self.root), timeout=2)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout.encode()), 32)
        self.assertIn("stdin=''", result.stderr)

    def test_maintenance_runner_drains_pending_output_after_child_exit(self) -> None:
        size = 200_000
        result = opencode.run_maintenance(
            (sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x'*{size})"),
            (),
            cwd=str(self.root),
            timeout=2,
        )
        self.assertEqual(len(result.stdout), size)
        self.assertFalse(result.truncated)

    def test_maintenance_runner_times_out_a_hanging_command(self) -> None:
        command = (sys.executable, "-c", "import time; time.sleep(30)")
        started = time.monotonic()
        with self.assertRaisesRegex(BridgeError, "timed out"):
            opencode.run_maintenance(command, (), cwd=str(self.root), timeout=0.1)
        self.assertLess(time.monotonic() - started, 1)

    def test_maintenance_runner_cleans_up_when_reader_start_fails(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch.object(opencode.subprocess, "Popen", side_effect=capture),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("reader failed")),
            self.assertRaisesRegex(RuntimeError, "reader failed"),
        ):
            opencode.run_maintenance(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                (),
                cwd=str(self.root),
                timeout=2,
            )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_maintenance_deadline_includes_descendants_holding_capture_pipes(self) -> None:
        child = "import time;\nwhile True:\n print('x', flush=True); time.sleep(0.01)"
        spawn = (
            "import subprocess,sys,tempfile,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1]], "
            "cwd=tempfile.gettempdir(), "
            "start_new_session=(sys.platform != 'win32')); time.sleep(0.1)"
        )
        command = (
            sys.executable,
            "-c",
            spawn,
        )
        started = time.monotonic()
        result = opencode.run_maintenance(command, (child,), cwd=str(self.root), timeout=1)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 2)

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_maintenance_runner_executes_cmd_shim_without_a_console(self) -> None:
        shim = self.root / "open&code^(test)! shim.cmd"
        shim.write_text(
            '@echo off\r\npython -c "import sys; print(repr(sys.argv[1:]))" %*\r\n',
            encoding="utf-8",
        )
        arguments = (
            "a&b",
            "a^b",
            "a!b",
            "a(b)",
            "a|b",
            "a<b",
            "a>b",
            "space x",
            "C:\\trailing\\",
        )
        result = opencode.run_maintenance((str(shim),), arguments, cwd=str(self.root), timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(ast.literal_eval(result.stdout.strip()), list(arguments))

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_maintenance_runner_rejects_unescapable_cmd_values(self) -> None:
        shim = self.root / "opencode.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        for argument in ("a%PATH%b", 'a"b', "a\nb", "a\rb"):
            with (
                self.subTest(argument=argument),
                self.assertRaisesRegex(BridgeError, "cannot safely receive"),
            ):
                opencode.run_maintenance((str(shim),), (argument,), cwd=str(self.root), timeout=2)

    def test_cmd_runner_explicitly_disables_delayed_expansion(self) -> None:
        self.assertIn(" /v:off /s /c ", inspect.getsource(opencode.run_maintenance))

    def test_missing_home_is_proven_unavailable_without_unknown_warning(self) -> None:
        home = self.root / "absent" / "opencode"
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": ("definitely-missing-opencode",)},
        )
        with opencode_home(home), patch.dict(os.environ, {"OPENCODE_DB": ""}):
            self.assertEqual(opencode.discover(context, use_cache=False), [])
            ref = NativeRef("ses_000000000001AAAAAAAAAAAAAA", str(home / "opencode.db"))
            self.assertEqual(opencode._availability(ref), Availability.UNAVAILABLE)
        self.assertEqual(diagnostics.warnings(), [])

    def test_existing_home_reports_broken_command_even_when_store_is_absent(self) -> None:
        home = self.root / "installed" / "opencode"
        home.mkdir(parents=True)
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": ("definitely-missing-opencode",)},
        )
        with opencode_home(home), patch.dict(os.environ, {"OPENCODE_DB": ""}):
            self.assertEqual(opencode.discover(context, use_cache=False), [])
        self.assertEqual(len(diagnostics.warnings()), 1)
        self.assertIn("binding is inferred", diagnostics.warnings()[0])

    def test_truncated_db_path_output_uses_inferred_binding(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        truncated = opencode.MaintenanceResult(
            ("opencode", "db", "path"),
            0,
            str(self.root / "must-not-open.db") + "\n",
            "",
            True,
        )
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": ("opencode",)},
        )
        with (
            opencode_home(home),
            patch.object(opencode, "run_maintenance", return_value=truncated),
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            rows = opencode.discover(context, use_cache=False)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("binding is inferred" in note for note in diagnostics.warnings()))

    def test_hanging_db_path_uses_inferred_binding_within_timeout(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        context = HarnessContext.create(
            use_cache=False,
            provider_commands={"opencode": (sys.executable, "-c", "import time; time.sleep(30)")},
        )
        started = time.monotonic()
        with (
            opencode_home(home),
            patch.object(opencode, "DB_PATH_TIMEOUT_SECONDS", 0.1),
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            rows = opencode.discover(context, use_cache=False)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("binding is inferred" in note for note in diagnostics.warnings()))

    def test_hanging_db_path_does_not_block_other_harnesses_in_shared_refresh(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)

        def native_discover(tool: str):
            def discover(context: HarnessContext, *, use_cache: bool = True):
                del context, use_cache
                return [
                    NativeSession(
                        tool=tool,
                        session_id=f"{tool}-survives",
                        title=f"{tool.title()} survives",
                        cwd="/work",
                        updated=2,
                        created=1,
                        preview="survives",
                        named=True,
                        storage=str(self.root / f"{tool}.jsonl"),
                    )
                ]

            return discover

        config = LaunchConfig(
            providers={
                "opencode": ProviderProfile(
                    command=[sys.executable, "-c", "import time; time.sleep(30)"]
                )
            }
        )
        started = time.monotonic()
        with (
            opencode_home(home),
            REGISTRY.temporary(
                replace(
                    REGISTRY.get("claude"),
                    discover=native_discover("claude"),
                    inspect_liveness=Unsupported("isolated test"),
                )
            ),
            REGISTRY.temporary(
                replace(
                    REGISTRY.get("codex"),
                    discover=native_discover("codex"),
                    inspect_liveness=Unsupported("isolated test"),
                )
            ),
            REGISTRY.temporary(
                replace(REGISTRY.get("opencode"), inspect_liveness=Unsupported("isolated test"))
            ),
            patch.object(opencode, "DB_PATH_TIMEOUT_SECONDS", 0.1),
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            loaded = load_sessions(use_cache=False, config=config)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual({item.tool for item in loaded}, {"claude", "codex", "opencode"})
        binding_notices = [note for note in diagnostics.warnings() if "binding is inferred" in note]
        self.assertEqual(len(binding_notices), 1)

    def test_failed_binding_probe_is_memoized_across_refresh_contexts(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        failure = BridgeError("probe failed")
        with (
            opencode_home(home),
            patch.object(opencode, "run_maintenance", side_effect=failure) as run,
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            first = opencode.discover(
                HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": ("opencode",)}
                ),
                use_cache=False,
            )
            second = opencode.discover(
                HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": ("opencode",)}
                ),
                use_cache=False,
            )
        self.assertEqual(len(first), 2)
        self.assertEqual(second, first)
        run.assert_called_once()

    def test_transient_probe_failure_retains_authority_then_retries(self) -> None:
        home = self.root / "home"
        authoritative = self.root / "channel" / "opencode-dev.db"
        self.populated_database(authoritative)
        command = ("opencode",)
        healthy = opencode.MaintenanceResult(command, 0, f"{authoritative}\n", "")
        with (
            opencode_home(home),
            patch.object(
                opencode,
                "run_maintenance",
                side_effect=(healthy, BridgeError("transient timeout"), healthy),
            ) as run,
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            rows = []
            for _ in range(3):
                rows.append(
                    opencode.discover(
                        HarnessContext.create(
                            use_cache=False, provider_commands={"opencode": command}
                        ),
                        use_cache=False,
                    )
                )
            final_binding = opencode._LAST_BINDING
        self.assertEqual([len(value) for value in rows], [2, 2, 2])
        self.assertEqual(run.call_count, 3)
        self.assertIsNotNone(final_binding)
        self.assertTrue(final_binding.authoritative)
        self.assertEqual(final_binding.path, authoritative.resolve())
        self.assertTrue(any("previously proven" in note for note in diagnostics.warnings()))

    def test_proven_authority_is_not_reused_across_command_or_cwd(self) -> None:
        authoritative = self.root / "channel" / "opencode-dev.db"
        self.populated_database(authoritative)
        fallback = self.root / "home" / "opencode.db"
        healthy = opencode.MaintenanceResult(("opencode",), 0, f"{authoritative}\n", "")
        cases = ("command", "cwd")
        for changed in cases:
            with self.subTest(changed=changed):
                diagnostics.clear_warnings()
                home = fallback.parent
                second_command = ("opencode-dev",) if changed == "command" else ("opencode",)
                different_cwd = self.root / "different-cwd"
                different_cwd.mkdir(exist_ok=True)
                with (
                    opencode_home(home),
                    patch.object(
                        opencode,
                        "run_maintenance",
                        side_effect=(healthy, BridgeError("probe failed")),
                    ) as run,
                    patch.dict(os.environ, {"OPENCODE_DB": ""}),
                ):
                    first = opencode.discover(
                        HarnessContext.create(
                            use_cache=False,
                            provider_commands={"opencode": ("opencode",)},
                        ),
                        use_cache=False,
                    )
                    cwd_context = (
                        contextlib.chdir(different_cwd)
                        if changed == "cwd"
                        else contextlib.nullcontext()
                    )
                    with cwd_context:
                        second = opencode.discover(
                            HarnessContext.create(
                                use_cache=False,
                                provider_commands={"opencode": second_command},
                            ),
                            use_cache=False,
                        )
                    rebound = opencode._LAST_BINDING
                self.assertEqual(len(first), 2)
                self.assertEqual(second, [])
                self.assertEqual(run.call_count, 2)
                self.assertIsNotNone(rebound)
                self.assertFalse(rebound.authoritative)
                self.assertEqual(rebound.path, fallback.resolve())

    def test_cold_start_memo_reprobes_when_binding_inputs_change(self) -> None:
        home = self.root / "home"
        other_database = self.root / "other" / "opencode.db"
        different_cwd = self.root / "different-cwd"
        different_cwd.mkdir()
        with (
            opencode_home(home),
            patch.object(
                opencode,
                "run_maintenance",
                side_effect=BridgeError("probe failed"),
            ) as run,
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):

            def discover_with(command: tuple[str, ...]) -> None:
                self.assertEqual(
                    opencode.discover(
                        HarnessContext.create(
                            use_cache=False, provider_commands={"opencode": command}
                        ),
                        use_cache=False,
                    ),
                    [],
                )

            discover_with(("opencode",))
            discover_with(("opencode",))
            self.assertEqual(run.call_count, 1)
            os.environ["OPENCODE_DB"] = str(other_database)
            discover_with(("opencode",))
            self.assertEqual(run.call_count, 2)
            discover_with(("opencode-dev",))
            self.assertEqual(run.call_count, 3)
            with contextlib.chdir(different_cwd):
                discover_with(("opencode-dev",))
            self.assertEqual(run.call_count, 4)

    def test_thread_start_failure_uses_inferred_binding(self) -> None:
        home = self.root / "home"
        database = home / "opencode.db"
        self.populated_database(database)
        with (
            opencode_home(home),
            patch.object(opencode, "run_maintenance", side_effect=RuntimeError("no thread")),
            patch.dict(os.environ, {"OPENCODE_DB": ""}),
        ):
            rows = opencode.discover(
                HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": ("opencode",)}
                ),
                use_cache=False,
            )
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("no thread" in note for note in diagnostics.warnings()))

    def test_nul_command_and_db_path_output_cannot_crash_discovery(self) -> None:
        commands = (
            ("bad\0command",),
            (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bad\\0path\\n')"),
        )
        for command in commands:
            with self.subTest(command=command):
                diagnostics.clear_warnings()
                context = HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": command}
                )
                with (
                    opencode_home(self.root / "absent"),
                    patch.dict(os.environ, {"OPENCODE_DB": ""}),
                ):
                    self.assertEqual(opencode.discover(context, use_cache=False), [])
                self.assertEqual(diagnostics.warnings(), [])

    def test_empty_parent_id_is_a_root_without_malformed_warning(self) -> None:
        database = self.root / "empty-parent.db"
        connection = create_database(database)
        try:
            add_session(connection, "ses_000000000001AAAAAAAAAAAAAA", parent_id="")
            connection.commit()
        finally:
            connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].parent_id, "")
        self.assertFalse(any("malformed" in note for note in diagnostics.warnings()))

    def test_agent_column_is_optional_display_metadata(self) -> None:
        database = self.root / "without-agent.db"
        connection = sqlite3.connect(database)
        connection.executescript(SCHEMA.replace("    agent TEXT,\n", ""))
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "ses_000000000001AAAAAAAAAAAAAA",
                None,
                "/work",
                "Useful title",
                1,
                2,
                None,
            ),
        )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_nickname, "")

    def test_generated_parent_and_child_titles_are_native_defaults(self) -> None:
        for prefix in ("New session - ", "Child session - "):
            self.assertIsNotNone(
                opencode._DEFAULT_TITLE.fullmatch(prefix + "2026-08-23T12:00:00.000Z")
            )
        self.assertIsNone(opencode._DEFAULT_TITLE.fullmatch("2026-08-23T12:00:00.000Z"))
        self.assertIsNone(
            opencode._DEFAULT_TITLE.fullmatch("Other session - 2026-08-23T12:00:00.000Z")
        )

    def test_preview_cap_applies_across_many_individually_small_parts(self) -> None:
        database = self.root / "preview-cap.db"
        connection = create_database(database)
        session_id = "ses_000000000001AAAAAAAAAAAAAA"
        add_session(connection, session_id)
        add_message(
            connection,
            session_id,
            "msg-preview-cap",
            "user",
            *({"type": "text", "text": "x" * 1_000} for _ in range(6)),
            created=1,
        )
        connection.commit()
        connection.close()
        with opencode_home(self.root / "fallback"):
            rows = opencode.discover(self.context(database), use_cache=False)
        self.assertEqual(len(rows[0].title), opencode.DISCOVERY_PREVIEW_CHARS)
        self.assertEqual(len(rows[0].preview), opencode.DISCOVERY_PREVIEW_CHARS)

    def test_installed_reader_offers_opencode_source_bridge_actions(self) -> None:
        item = Session(
            "opencode",
            "ses_000000000001AAAAAAAAAAAAAA",
            "title",
            "/work",
            2,
            1,
            "",
            True,
            str(self.root / "opencode.db"),
        )
        self.assertTrue(can_bridge(item))
        self.assertEqual(available_launch_tools(item), ("opencode", "codex", "claude"))

    def test_home_resolution_treats_empty_environment_as_unset(self) -> None:
        fake_home = self.root / "user"
        with patch.object(opencode, "HOME", fake_home):
            with (
                patch.object(opencode, "IS_WINDOWS", False),
                patch.dict(os.environ, {"XDG_DATA_HOME": ""}),
            ):
                self.assertEqual(opencode._default_home(), fake_home / ".local/share/opencode")
            with (
                patch.object(opencode, "IS_WINDOWS", True),
                patch.dict(os.environ, {"LOCALAPPDATA": ""}),
            ):
                self.assertEqual(opencode._default_home(), fake_home / "AppData/Local/opencode")

    def test_home_resolution_is_native_and_stable_in_a_fresh_interpreter(self) -> None:
        variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_DATA_HOME"
        environment = os.environ.copy()
        environment[variable] = ""
        source = str(Path(__file__).parents[1] / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source, environment.get("PYTHONPATH", "")) if value
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from ai_sessions.harnesses.opencode import OPENCODE_HOME; print(OPENCODE_HOME)",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
            timeout=5,
        )
        suffix = (
            Path("AppData/Local/opencode") if os.name == "nt" else Path(".local/share/opencode")
        )
        self.assertEqual(Path(completed.stdout.strip()), Path.home() / suffix)

    def test_loaded_config_command_reaches_discovery_context(self) -> None:
        captured: list[tuple[str, ...]] = []

        def discover(context: HarnessContext, *, use_cache: bool = True):
            del use_cache
            captured.append(context.provider_command("opencode"))
            return []

        configured = [sys.executable, "-c", "pass"]
        config = LaunchConfig(providers={"opencode": ProviderProfile(command=configured)})
        with contextlib.ExitStack() as stack:
            for name in ("codex", "claude"):
                stack.enter_context(
                    REGISTRY.temporary(
                        replace(REGISTRY.get(name), discover=Unsupported("isolated test"))
                    )
                )
            stack.enter_context(
                REGISTRY.temporary(replace(REGISTRY.get("opencode"), discover=discover))
            )
            load_sessions(use_cache=False, config=config)
        self.assertEqual(captured, [tuple(configured)])


if __name__ == "__main__":
    unittest.main()
