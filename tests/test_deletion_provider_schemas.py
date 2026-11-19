"""Read-only native schema snapshots (no user rows), reviewed 2026-11-19."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_deletion import IDENT, OTHER
from test_deletion import isolated as isolated

from ai_sessions import deletion
from ai_sessions.registry import REGISTRY

OPENCODE_NATIVE_SCHEMA = {
    "workspace": "CREATE TABLE `workspace` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `type` text NOT NULL,\n"
    "          `name` text DEFAULT '' NOT NULL,\n"
    "          `branch` text,\n"
    "          `directory` text,\n"
    "          `extra` text,\n"
    "          `project_id` text NOT NULL,\n"
    "          `time_used` integer NOT NULL,\n"
    "          CONSTRAINT `fk_workspace_project_id_project_id_fk` FOREIGN KEY "
    "(`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE\n"
    "        )",
    "data_migration": "CREATE TABLE `data_migration` (\n"
    "          `name` text PRIMARY KEY,\n"
    "          `time_completed` integer NOT NULL\n"
    "        )",
    "account_state": "CREATE TABLE `account_state` (\n"
    "          `id` integer PRIMARY KEY,\n"
    "          `active_account_id` text,\n"
    "          `active_org_id` text,\n"
    "          CONSTRAINT `fk_account_state_active_account_id_account_id_fk` FOREIGN "
    "KEY (`active_account_id`) REFERENCES `account`(`id`) ON DELETE SET NULL\n"
    "        )",
    "account": "CREATE TABLE `account` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `email` text NOT NULL,\n"
    "          `url` text NOT NULL,\n"
    "          `access_token` text NOT NULL,\n"
    "          `refresh_token` text NOT NULL,\n"
    "          `token_expiry` integer,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL\n"
    "        )",
    "control_account": "CREATE TABLE `control_account` (\n"
    "          `email` text NOT NULL,\n"
    "          `url` text NOT NULL,\n"
    "          `access_token` text NOT NULL,\n"
    "          `refresh_token` text NOT NULL,\n"
    "          `token_expiry` integer,\n"
    "          `active` integer NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          CONSTRAINT `control_account_pk` PRIMARY KEY(`email`, `url`)\n"
    "        )",
    "credential": "CREATE TABLE `credential` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `integration_id` text,\n"
    "          `label` text NOT NULL,\n"
    "          `value` text NOT NULL,\n"
    "          `connector_id` text,\n"
    "          `method_id` text,\n"
    "          `active` integer,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL\n"
    "        )",
    "event_sequence": "CREATE TABLE `event_sequence` (\n"
    "          `aggregate_id` text PRIMARY KEY,\n"
    "          `seq` integer NOT NULL,\n"
    "          `owner_id` text\n"
    "        )",
    "event": "CREATE TABLE `event` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `aggregate_id` text NOT NULL,\n"
    "          `seq` integer NOT NULL,\n"
    "          `type` text NOT NULL,\n"
    "          `data` text NOT NULL,\n"
    "          CONSTRAINT `fk_event_aggregate_id_event_sequence_aggregate_id_fk` FOREIGN KEY "
    "(`aggregate_id`) REFERENCES `event_sequence`(`aggregate_id`) ON DELETE CASCADE\n"
    "        )",
    "permission": "CREATE TABLE `permission` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `project_id` text NOT NULL,\n"
    "          `action` text NOT NULL,\n"
    "          `resource` text NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          CONSTRAINT `fk_permission_project_id_project_id_fk` FOREIGN KEY "
    "(`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE\n"
    "        )",
    "project_directory": "CREATE TABLE `project_directory` (\n"
    "          `project_id` text NOT NULL,\n"
    "          `directory` text NOT NULL,\n"
    "          `type` text,\n"
    "          `strategy` text,\n"
    "          `time_created` integer NOT NULL,\n"
    "          CONSTRAINT `project_directory_pk` PRIMARY KEY(`project_id`, "
    "`directory`),\n"
    "          CONSTRAINT `fk_project_directory_project_id_project_id_fk` "
    "FOREIGN KEY (`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE\n"
    "        )",
    "project": "CREATE TABLE `project` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `worktree` text NOT NULL,\n"
    "          `vcs` text,\n"
    "          `name` text,\n"
    "          `icon_url` text,\n"
    "          `icon_url_override` text,\n"
    "          `icon_color` text,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          `time_initialized` integer,\n"
    "          `sandboxes` text NOT NULL,\n"
    "          `commands` text\n"
    "        )",
    "message": "CREATE TABLE `message` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `session_id` text NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          `data` text NOT NULL,\n"
    "          CONSTRAINT `fk_message_session_id_session_id_fk` FOREIGN KEY (`session_id`) "
    "REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "part": "CREATE TABLE `part` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `message_id` text NOT NULL,\n"
    "          `session_id` text NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          `data` text NOT NULL,\n"
    "          CONSTRAINT `fk_part_message_id_message_id_fk` FOREIGN KEY (`message_id`) "
    "REFERENCES `message`(`id`) ON DELETE CASCADE\n"
    "        )",
    "session_context_epoch": "CREATE TABLE `session_context_epoch` (\n"
    "          `session_id` text PRIMARY KEY,\n"
    "          `baseline` text NOT NULL,\n"
    "          `snapshot` text NOT NULL,\n"
    "          `baseline_seq` integer NOT NULL,\n"
    "          CONSTRAINT "
    "`fk_session_context_epoch_session_id_session_id_fk` FOREIGN KEY "
    "(`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "session_input": "CREATE TABLE `session_input` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `session_id` text NOT NULL,\n"
    "          `prompt` text NOT NULL,\n"
    "          `delivery` text NOT NULL,\n"
    "          `admitted_seq` integer NOT NULL,\n"
    "          `promoted_seq` integer,\n"
    "          `time_created` integer NOT NULL,\n"
    "          CONSTRAINT `fk_session_input_session_id_session_id_fk` FOREIGN KEY "
    "(`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "session_message": "CREATE TABLE `session_message` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `session_id` text NOT NULL,\n"
    "          `type` text NOT NULL,\n"
    "          `seq` integer NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          `data` text NOT NULL,\n"
    "          CONSTRAINT `fk_session_message_session_id_session_id_fk` FOREIGN "
    "KEY (`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "session": "CREATE TABLE `session` (\n"
    "          `id` text PRIMARY KEY,\n"
    "          `project_id` text NOT NULL,\n"
    "          `workspace_id` text,\n"
    "          `parent_id` text,\n"
    "          `slug` text NOT NULL,\n"
    "          `directory` text NOT NULL,\n"
    "          `path` text,\n"
    "          `title` text NOT NULL,\n"
    "          `version` text NOT NULL,\n"
    "          `share_url` text,\n"
    "          `summary_additions` integer,\n"
    "          `summary_deletions` integer,\n"
    "          `summary_files` integer,\n"
    "          `summary_diffs` text,\n"
    "          `metadata` text,\n"
    "          `cost` real DEFAULT 0 NOT NULL,\n"
    "          `tokens_input` integer DEFAULT 0 NOT NULL,\n"
    "          `tokens_output` integer DEFAULT 0 NOT NULL,\n"
    "          `tokens_reasoning` integer DEFAULT 0 NOT NULL,\n"
    "          `tokens_cache_read` integer DEFAULT 0 NOT NULL,\n"
    "          `tokens_cache_write` integer DEFAULT 0 NOT NULL,\n"
    "          `revert` text,\n"
    "          `permission` text,\n"
    "          `agent` text,\n"
    "          `model` text,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          `time_compacting` integer,\n"
    "          `time_archived` integer,\n"
    "          CONSTRAINT `fk_session_project_id_project_id_fk` FOREIGN KEY (`project_id`) "
    "REFERENCES `project`(`id`) ON DELETE CASCADE\n"
    "        )",
    "todo": "CREATE TABLE `todo` (\n"
    "          `session_id` text NOT NULL,\n"
    "          `content` text NOT NULL,\n"
    "          `status` text NOT NULL,\n"
    "          `priority` text NOT NULL,\n"
    "          `position` integer NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          CONSTRAINT `todo_pk` PRIMARY KEY(`session_id`, `position`),\n"
    "          CONSTRAINT `fk_todo_session_id_session_id_fk` FOREIGN KEY (`session_id`) "
    "REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "session_share": "CREATE TABLE `session_share` (\n"
    "          `session_id` text PRIMARY KEY,\n"
    "          `id` text NOT NULL,\n"
    "          `secret` text NOT NULL,\n"
    "          `url` text NOT NULL,\n"
    "          `time_created` integer NOT NULL,\n"
    "          `time_updated` integer NOT NULL,\n"
    "          CONSTRAINT `fk_session_share_session_id_session_id_fk` FOREIGN KEY "
    "(`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE\n"
    "        )",
    "migration": 'CREATE TABLE "migration" (id TEXT PRIMARY KEY, time_completed INTEGER NOT NULL)',
}

CODEX_NATIVE_SCHEMA = {
    "_sqlx_migrations": "CREATE TABLE _sqlx_migrations (\n"
    "    version BIGINT PRIMARY KEY,\n"
    "    description TEXT NOT NULL,\n"
    "    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
    "    success BOOLEAN NOT NULL,\n"
    "    checksum BLOB NOT NULL,\n"
    "    execution_time BIGINT NOT NULL\n"
    ")",
    "threads": "CREATE TABLE threads (\r\n"
    "    id TEXT PRIMARY KEY,\r\n"
    "    rollout_path TEXT NOT NULL,\r\n"
    "    created_at INTEGER NOT NULL,\r\n"
    "    updated_at INTEGER NOT NULL,\r\n"
    "    source TEXT NOT NULL,\r\n"
    "    model_provider TEXT NOT NULL,\r\n"
    "    cwd TEXT NOT NULL,\r\n"
    "    title TEXT NOT NULL,\r\n"
    "    sandbox_policy TEXT NOT NULL,\r\n"
    "    approval_mode TEXT NOT NULL,\r\n"
    "    tokens_used INTEGER NOT NULL DEFAULT 0,\r\n"
    "    has_user_event INTEGER NOT NULL DEFAULT 0,\r\n"
    "    archived INTEGER NOT NULL DEFAULT 0,\r\n"
    "    archived_at INTEGER,\r\n"
    "    git_sha TEXT,\r\n"
    "    git_branch TEXT,\r\n"
    "    git_origin_url TEXT\r\n"
    ", cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '', "
    "agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled', "
    "model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER, "
    "updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '', "
    "recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0, "
    "history_mode TEXT NOT NULL DEFAULT 'legacy', name TEXT, is_pinned INTEGER NOT NULL "
    "DEFAULT 0, thread_section_id TEXT\r\n"
    "    REFERENCES thread_sections(id) ON DELETE SET NULL, section_position INTEGER, "
    "section_entered_at_ms INTEGER, project_id TEXT\r\n"
    "    REFERENCES projects(id) ON DELETE SET NULL, originator TEXT, daybreak_enabled "
    "BOOLEAN, creator_user_id TEXT, creator_account_id TEXT, attached_thread_parent_id "
    "TEXT)",
    "thread_dynamic_tools": "CREATE TABLE thread_dynamic_tools (\r\n"
    "    thread_id TEXT NOT NULL,\r\n"
    "    position INTEGER NOT NULL,\r\n"
    "    name TEXT NOT NULL,\r\n"
    "    description TEXT NOT NULL,\r\n"
    "    input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT "
    "0, namespace TEXT,\r\n"
    "    PRIMARY KEY(thread_id, position),\r\n"
    "    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE\r\n"
    ")",
    "backfill_state": "CREATE TABLE backfill_state (\r\n"
    "    id INTEGER PRIMARY KEY CHECK (id = 1),\r\n"
    "    status TEXT NOT NULL,\r\n"
    "    last_watermark TEXT,\r\n"
    "    last_success_at INTEGER,\r\n"
    "    updated_at INTEGER NOT NULL\r\n"
    ")",
    "thread_spawn_edges": "CREATE TABLE thread_spawn_edges (\r\n"
    "    parent_thread_id TEXT NOT NULL,\r\n"
    "    child_thread_id TEXT NOT NULL PRIMARY KEY,\r\n"
    "    status TEXT NOT NULL\r\n"
    ")",
    "remote_control_enrollments": "CREATE TABLE remote_control_enrollments (\r\n"
    "    websocket_url TEXT NOT NULL,\r\n"
    "    account_id TEXT NOT NULL,\r\n"
    "    app_server_client_name TEXT NOT NULL,\r\n"
    "    server_id TEXT NOT NULL,\r\n"
    "    environment_id TEXT NOT NULL,\r\n"
    "    server_name TEXT NOT NULL,\r\n"
    "    updated_at INTEGER NOT NULL, remote_control_enabled "
    "INTEGER,\r\n"
    "    PRIMARY KEY (websocket_url, account_id, "
    "app_server_client_name)\r\n"
    ")",
    "external_agent_config_imports": "CREATE TABLE external_agent_config_imports (\r\n"
    "    import_id TEXT PRIMARY KEY,\r\n"
    "    completed_at_ms INTEGER NOT NULL,\r\n"
    "    successes TEXT NOT NULL,\r\n"
    "    failures TEXT NOT NULL\r\n"
    ", provider_id TEXT)",
    "thread_sections": "CREATE TABLE thread_sections (\r\n"
    "    id TEXT PRIMARY KEY,\r\n"
    "    name TEXT NOT NULL\r\n"
    ", appearance TEXT)",
    "rollout_migration_state": "CREATE TABLE rollout_migration_state (\r\n"
    "    migration_id TEXT PRIMARY KEY,\r\n"
    "    last_checked_thread_created_at INTEGER,\r\n"
    "    last_checked_thread_id TEXT,\r\n"
    "    updated_at INTEGER NOT NULL\r\n"
    ")",
    "rollout_migration_skipped_rollouts": "CREATE TABLE rollout_migration_skipped_rollouts (\r\n"
    "    migration_id TEXT NOT NULL,\r\n"
    "    rollout_path TEXT NOT NULL,\r\n"
    "    rollout_size_bytes INTEGER NOT NULL,\r\n"
    "    rollout_modified_at_ns INTEGER NOT NULL,\r\n"
    "    skip_reason TEXT NOT NULL,\r\n"
    "    skipped_at INTEGER NOT NULL,\r\n"
    "    PRIMARY KEY (migration_id, rollout_path)\r\n"
    ")",
    "projects": "CREATE TABLE projects (\r\n"
    "    id TEXT PRIMARY KEY,\r\n"
    "    name TEXT NOT NULL,\r\n"
    "    metadata TEXT NOT NULL DEFAULT '{}',\r\n"
    "    position INTEGER NOT NULL,\r\n"
    "    created_at_ms INTEGER NOT NULL,\r\n"
    "    updated_at_ms INTEGER NOT NULL\r\n"
    ")",
    "project_roots": "CREATE TABLE project_roots (\r\n"
    "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,\r\n"
    "    position INTEGER NOT NULL,\r\n"
    "    path TEXT NOT NULL,\r\n"
    "    PRIMARY KEY (project_id, position)\r\n"
    ")",
    "project_idempotency_keys": "CREATE TABLE project_idempotency_keys (\r\n"
    "    key TEXT PRIMARY KEY,\r\n"
    "    project_id TEXT NOT NULL,\r\n"
    "    created_at_ms INTEGER NOT NULL\r\n"
    ")",
    "thread_attachments": 'CREATE TABLE "thread_attachments" (\r\n'
    "    id TEXT PRIMARY KEY,\r\n"
    "    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,\r\n"
    "    attachment_type TEXT NOT NULL,\r\n"
    "    identity_key TEXT NOT NULL,\r\n"
    "    payload TEXT NOT NULL,\r\n"
    "    created_at INTEGER NOT NULL,\r\n"
    "    UNIQUE (thread_id, attachment_type, identity_key)\r\n"
    ")",
    "guardian_review_feedback": "CREATE TABLE guardian_review_feedback (\r\n"
    "    id TEXT PRIMARY KEY NOT NULL,\r\n"
    "    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE "
    "CASCADE,\r\n"
    "    record BLOB NOT NULL\r\n"
    ")",
    "thread_read_receipts": "CREATE TABLE thread_read_receipts (\r\n"
    "    thread_id TEXT PRIMARY KEY NOT NULL,\r\n"
    "    first_unread_turn TEXT,\r\n"
    "    revision TEXT NOT NULL,\r\n"
    "    last_published_turn TEXT\r\n"
    ")",
}


CODEX_LOG_SCHEMA = {
    "_sqlx_migrations": "CREATE TABLE _sqlx_migrations (\n"
    "    version BIGINT PRIMARY KEY,\n"
    "    description TEXT NOT NULL,\n"
    "    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
    "    success BOOLEAN NOT NULL,\n"
    "    checksum BLOB NOT NULL,\n"
    "    execution_time BIGINT NOT NULL\n"
    ")",
    "logs": "CREATE TABLE logs (\r\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\r\n"
    "    ts INTEGER NOT NULL,\r\n"
    "    ts_nanos INTEGER NOT NULL,\r\n"
    "    level TEXT NOT NULL,\r\n"
    "    target TEXT NOT NULL,\r\n"
    "    feedback_log_body TEXT,\r\n"
    "    module_path TEXT,\r\n"
    "    file TEXT,\r\n"
    "    line INTEGER,\r\n"
    "    thread_id TEXT,\r\n"
    "    process_uuid TEXT,\r\n"
    "    estimated_bytes INTEGER NOT NULL DEFAULT 0\r\n"
    ")",
}


def populate(connection, table, **values):
    for _, column, kind, required, default, _ in connection.execute(
        f'PRAGMA table_info("{table}")'
    ):
        if required and default is None and column not in values:
            values[column] = 1 if "INT" in kind.upper() else ""
    names = ",".join('"' + key + '"' for key in values)
    marks = ",".join("?" for _ in values)
    connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(values.values()))


@pytest.fixture
def open_store(tmp_path, monkeypatch):
    from ai_sessions.harnesses import opencode

    home = tmp_path / "native"
    home.mkdir()
    database = home / "opencode.db"
    # Actual production column and FK definitions; older message/part storage
    # without the newer event-sourcing extensions is the supported subset.
    names = ("project", "session", "message", "part", "todo", "session_share")
    with sqlite3.connect(database) as db:
        for table in names:
            db.execute(OPENCODE_NATIVE_SCHEMA[table])
        db.execute("PRAGMA foreign_keys=ON")
        populate(db, "project", id="project")
        for ident in (IDENT, OTHER):
            populate(db, "session", id=ident, project_id="project", title="fiction", revert=None)
            populate(
                db,
                "message",
                id="msg-" + ident,
                session_id=ident,
                data=json.dumps({"role": "user"}),
            )
            populate(
                db,
                "part",
                id="part-" + ident,
                message_id="msg-" + ident,
                session_id=ident,
                data=json.dumps({"type": "text", "text": "fiction"}),
            )
            populate(db, "todo", session_id=ident, position=0)
            populate(db, "session_share", session_id=ident, id="share-" + ident)
    binding = opencode.DatabaseBinding(database, ("opencode",), True, str(home))
    monkeypatch.setattr(opencode, "_LAST_BINDING", binding)
    with REGISTRY.temporary(replace(opencode.ADAPTER, home=home)) as adapter:
        yield home, database, adapter, binding


def test_opencode_reviewed_native_cascades_preserve_other_session(open_store):
    from ai_sessions.discovery import HarnessContext
    from ai_sessions.harnesses import opencode

    home, database, adapter, binding = open_store
    context = HarnessContext.create(use_cache=False)
    context.adapter_state["opencode.database"] = binding
    assert {row.session_id for row in opencode.discover(context, use_cache=False)} == {IDENT, OTHER}
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    assert plan.databases and not plan.files
    deletion.commit_plan(plan)
    with sqlite3.connect(database) as db:
        for table in ("session", "message", "part", "todo", "session_share"):
            column = "id" if table == "session" else "session_id"
            assert db.execute(f'SELECT "{column}" FROM "{table}"').fetchall() == [(OTHER,)]
        assert db.execute("SELECT id FROM project").fetchall() == [("project",)]
    context = HarnessContext.create(use_cache=False)
    context.adapter_state["opencode.database"] = binding
    assert {row.session_id for row in opencode.discover(context, use_cache=False)} == {OTHER}


def test_opencode_child_session_refused(open_store):
    _, database, adapter, _ = open_store
    with sqlite3.connect(database) as db:
        db.execute("UPDATE session SET parent_id=? WHERE id=?", (IDENT, OTHER))
    with pytest.raises(deletion.DeletionError, match="child/fork"):
        adapter.plan_delete(adapter.resolve(IDENT))


def test_opencode_cross_session_cascade_refused(open_store):
    _, database, adapter, _ = open_store
    with sqlite3.connect(database) as db:
        db.execute("UPDATE part SET message_id=? WHERE session_id=?", ("msg-" + IDENT, OTHER))
    with pytest.raises(deletion.DeletionError, match="unselected session"):
        adapter.plan_delete(adapter.resolve(IDENT))


def test_opencode_db_mutation_race_refused(open_store):
    _, database, adapter, _ = open_store
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    with sqlite3.connect(database) as db:
        db.execute("UPDATE session SET title='raced' WHERE id=?", (IDENT,))
    with pytest.raises(deletion.DeletionError, match="database changed"):
        deletion.commit_plan(plan)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM session").fetchone()[0] == 2


def test_opencode_actual_extended_native_schema_supported(open_store):
    _, database, adapter, _ = open_store
    with sqlite3.connect(database) as db:
        for name, ddl in OPENCODE_NATIVE_SCHEMA.items():
            if name not in ("project", "session", "message", "part", "todo", "session_share"):
                db.execute(ddl)
    assert adapter.plan_delete(adapter.resolve(IDENT)).databases


def test_opencode_unproven_binding_refused(open_store, monkeypatch):
    from ai_sessions.harnesses import opencode

    _, _, adapter, binding = open_store
    monkeypatch.setattr(opencode, "_LAST_BINDING", replace(binding, authoritative=False))
    with pytest.raises(deletion.DeletionError, match="binding"):
        adapter.plan_delete(adapter.resolve(IDENT))


@pytest.fixture
def codex_store(tmp_path):
    from ai_sessions.harnesses import codex

    home = tmp_path / "native"
    folder = home / "sessions" / "2026" / "11" / "19"
    folder.mkdir(parents=True)
    for ident in (IDENT, OTHER):
        path = folder / ("rollout-2026-11-19T00-00-00-" + ident + ".jsonl")
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": ident, "cwd": str(home)}}) + "\n"
        )
    with REGISTRY.temporary(replace(codex.ADAPTER, home=home)) as adapter:
        yield home, adapter


def test_codex_legacy_rollout_compressed_index_roundtrip(codex_store):
    import zstandard

    from ai_sessions.discovery import HarnessContext
    from ai_sessions.harnesses import codex

    home, adapter = codex_store
    ref = adapter.resolve(IDENT)
    path = Path(ref.storage)
    path.with_name(path.name + ".zst").write_bytes(
        zstandard.ZstdCompressor().compress(path.read_bytes())
    )
    (home / "history.jsonl").write_text(
        json.dumps({"session_id": IDENT}) + "\n" + json.dumps({"session_id": OTHER}) + "\n"
    )
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": IDENT}) + "\n" + json.dumps({"id": OTHER}) + "\n"
    )
    plan = adapter.plan_delete(ref)
    deletion.commit_plan(plan)
    # Clear only the fixture listing to exercise a genuinely fresh discovery.
    codex._rollout_index = None
    assert IDENT not in {
        row.session_id
        for row in codex.discover(HarnessContext.create(use_cache=False), use_cache=False)
    }
    assert adapter.resolve(OTHER) is not None
    assert IDENT not in (home / "history.jsonl").read_text()
    assert IDENT not in (home / "session_index.jsonl").read_text()


def test_codex_unknown_future_native_schema_refused(codex_store):
    home, adapter = codex_store
    with sqlite3.connect(home / "state_5.sqlite") as db:
        for ddl in CODEX_NATIVE_SCHEMA.values():
            db.execute(ddl)
        populate(db, "threads", id=IDENT, rollout_path=adapter.resolve(IDENT).storage)
        db.execute("CREATE TABLE unknown_future_session_history (thread_id TEXT)")
    with pytest.raises(deletion.DeletionError, match="unreviewed native database tables"):
        adapter.plan_delete(adapter.resolve(IDENT))


def test_codex_unknown_diagnostic_database_schema_refused(codex_store):
    home, adapter = codex_store
    with sqlite3.connect(home / "logs_2.sqlite") as db:
        db.execute("CREATE TABLE logs (thread_id TEXT, feedback_log_body TEXT)")
        db.execute("CREATE TABLE unknown_log_extension (thread_id TEXT)")
    with pytest.raises(deletion.DeletionError, match="unreviewed native database tables"):
        adapter.plan_delete(adapter.resolve(IDENT))
