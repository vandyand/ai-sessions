"""Current native schemas and review fault injections, fictional content only."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import psutil
import pytest
from test_deletion import IDENT, OTHER
from test_deletion import isolated as isolated
from test_deletion_provider_schemas import (
    CODEX_LOG_SCHEMA,
    CODEX_NATIVE_SCHEMA,
    OPENCODE_NATIVE_SCHEMA,
    populate,
)
from test_deletion_provider_schemas import (
    codex_store as codex_store,
)
from test_deletion_provider_schemas import (
    open_store as open_store,
)

from ai_sessions import deletion
from ai_sessions.liveness import LivenessContext, ProcessInfo
from ai_sessions.model import NativeRef, Session
from ai_sessions.registry import REGISTRY


@pytest.fixture
def current_codex(codex_store):
    home, adapter = codex_store
    state, logs = home / "state_5.sqlite", home / "logs_2.sqlite"
    with sqlite3.connect(state) as connection:
        for ddl in CODEX_NATIVE_SCHEMA.values():
            connection.execute(ddl)
        for ident in (IDENT, OTHER):
            populate(connection, "threads", id=ident, rollout_path=adapter.resolve(ident).storage)
            populate(connection, "thread_dynamic_tools", thread_id=ident, position=0)
            populate(
                connection,
                "thread_attachments",
                id="attachment-" + ident,
                thread_id=ident,
                identity_key=ident,
            )
            populate(
                connection,
                "guardian_review_feedback",
                id="feedback-" + ident,
                thread_id=ident,
                record=b"fiction",
            )
            populate(connection, "thread_read_receipts", thread_id=ident)
    with sqlite3.connect(logs) as connection:
        for ddl in CODEX_LOG_SCHEMA.values():
            connection.execute(ddl)
        for ident in (IDENT, OTHER, None):
            populate(
                connection,
                "logs",
                thread_id=ident,
                feedback_log_body="fiction",
                process_uuid="shared-process",
            )
    return home, adapter, state, logs


def test_current_codex_state_logs_exact_roundtrip(current_codex):
    home, adapter, state, logs = current_codex
    selected = Path(adapter.resolve(IDENT).storage)
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    assert {db.path for db in plan.databases} == {state, logs}
    deletion.commit_plan(plan)
    assert not selected.exists()
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT id FROM threads").fetchall() == [(OTHER,)]
        for table in (
            "thread_dynamic_tools",
            "thread_attachments",
            "guardian_review_feedback",
            "thread_read_receipts",
        ):
            assert connection.execute(f'SELECT thread_id FROM "{table}"').fetchall() == [(OTHER,)]
    with sqlite3.connect(logs) as connection:
        assert connection.execute("SELECT thread_id FROM logs ORDER BY id").fetchall() == [
            (OTHER,),
            (None,),
        ]
    assert Path(adapter.resolve(OTHER).storage).exists()
    assert not list(home.rglob(".delete-*"))


def test_current_codex_unrelated_log_append_allowed(current_codex):
    _, adapter, _, logs = current_codex
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    with sqlite3.connect(logs) as connection:
        populate(connection, "logs", thread_id=OTHER, feedback_log_body="new unrelated activity")
    deletion.commit_plan(plan)
    with sqlite3.connect(logs) as connection:
        assert connection.execute(
            "SELECT count(*) FROM logs WHERE thread_id=?", (OTHER,)
        ).fetchone() == (2,)


def test_current_codex_selected_log_race_refused(current_codex):
    _, adapter, _, logs = current_codex
    ref = adapter.resolve(IDENT)
    plan = adapter.plan_delete(ref)
    with sqlite3.connect(logs) as connection:
        populate(connection, "logs", thread_id=IDENT, feedback_log_body="selected mutation")
    with pytest.raises(deletion.DeletionError, match="database changed"):
        deletion.commit_plan(plan)
    assert Path(ref.storage).exists()


def test_current_codex_second_database_failure_rolls_everything_back(current_codex):
    _, adapter, state, logs = current_codex
    ref = adapter.resolve(IDENT)
    plan = adapter.plan_delete(ref)
    bad = replace(
        plan.databases[1],
        statements=(("DELETE FROM {schema}.fiction_missing WHERE thread_id=?", (IDENT,)),),
    )
    with pytest.raises(sqlite3.Error):
        deletion.commit_plan(replace(plan, databases=(plan.databases[0], bad)))
    assert Path(ref.storage).exists()
    for path, query in (
        (state, "SELECT count(*) FROM threads"),
        (logs, "SELECT count(*) FROM logs"),
    ):
        with sqlite3.connect(path) as connection:
            assert connection.execute(query).fetchone()[0] == (2 if path == state else 3)


@pytest.mark.parametrize(
    "kind", ["spawn", "attached", "paginated", "threadless", "empty-thread", "wrong-path"]
)
def test_current_codex_concrete_refusals(current_codex, kind):
    _, adapter, state, logs = current_codex
    with sqlite3.connect(logs if kind in ("threadless", "empty-thread") else state) as connection:
        if kind == "spawn":
            populate(
                connection, "thread_spawn_edges", parent_thread_id=IDENT, child_thread_id=OTHER
            )
        elif kind == "attached":
            connection.execute(
                "UPDATE threads SET attached_thread_parent_id=? WHERE id=?", (IDENT, OTHER)
            )
        elif kind == "paginated":
            connection.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (IDENT,))
        elif kind == "wrong-path":
            connection.execute(
                "UPDATE threads SET rollout_path='outside-unverified-history' WHERE id=?", (IDENT,)
            )
        else:
            connection.execute(
                "UPDATE logs SET thread_id=?, feedback_log_body=? WHERE thread_id IS NULL",
                ("" if kind == "empty-thread" else None, IDENT),
            )
    with pytest.raises(deletion.DeletionError):
        adapter.plan_delete(adapter.resolve(IDENT))


@pytest.fixture
def current_open(open_store):
    _, path, _, _ = open_store
    with sqlite3.connect(path) as connection:
        for name, ddl in OPENCODE_NATIVE_SCHEMA.items():
            if name not in ("project", "session", "message", "part", "todo", "session_share"):
                connection.execute(ddl)
        for ident in (IDENT, OTHER):
            populate(connection, "event_sequence", aggregate_id=ident, seq=1)
            populate(
                connection,
                "event",
                id="event-" + ident,
                aggregate_id=ident,
                seq=1,
                type="message.updated.1",
                data=json.dumps({"sessionID": ident, "info": {"sessionID": ident}}),
            )
            populate(connection, "session_context_epoch", session_id=ident)
            populate(connection, "session_input", id="input-" + ident, session_id=ident)
            populate(connection, "session_message", id="message-" + ident, session_id=ident)
        populate(
            connection,
            "account",
            id="unrelated-account",
            access_token="fictional-not-a-secret",
            refresh_token="fictional-not-a-secret",
        )
    return open_store


def test_current_opencode_events_inputs_history_exact_roundtrip(current_open):
    _, path, adapter, _ = current_open
    deletion.commit_plan(adapter.plan_delete(adapter.resolve(IDENT)))
    with sqlite3.connect(path) as connection:
        for table in ("event", "event_sequence"):
            assert connection.execute(f'SELECT aggregate_id FROM "{table}"').fetchall() == [
                (OTHER,)
            ]
        for table in (
            "session_context_epoch",
            "session_input",
            "session_message",
            "message",
            "part",
        ):
            assert connection.execute(f'SELECT session_id FROM "{table}"').fetchall() == [(OTHER,)]
        assert connection.execute("SELECT id FROM account").fetchall() == [("unrelated-account",)]


def test_current_opencode_conflicting_event_owner_refused(current_open):
    _, path, adapter, _ = current_open
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE event SET data=? WHERE aggregate_id=?",
            (json.dumps({"sessionID": IDENT}), OTHER),
        )
    with pytest.raises(deletion.DeletionError, match="ownership"):
        adapter.plan_delete(adapter.resolve(IDENT))


def test_explicit_opencode_db_resolves_without_native_process(current_open, monkeypatch):
    from ai_sessions.harnesses import opencode

    _, path, adapter, _ = current_open
    monkeypatch.setattr(opencode, "_LAST_BINDING", None)
    monkeypatch.setenv("OPENCODE_DB", str(path))
    ref = adapter.resolve_delete(IDENT)
    assert ref == NativeRef(IDENT, str(path))
    assert adapter.plan_delete(ref).databases


def test_continue_refuses_even_when_current_hook_reports_historical_other(tmp_path, monkeypatch):
    from ai_sessions.harnesses import codex

    home = tmp_path / "native"
    home.mkdir()
    current = NativeRef(OTHER, str(home / "other.jsonl"))
    adapter = replace(
        codex.ADAPTER,
        home=home,
        current_session=lambda pid, started: current,
        resolve=lambda ident: current if ident == OTHER else None,
    )
    item = Session(adapter.name, IDENT, "fiction", "", 0, 0, "", False, str(home / "selected"))
    snapshot = LivenessContext(
        "win32", (ProcessInfo(991, "codex.exe", ("codex.exe", "--continue"), "start", 1),), {}
    )
    monkeypatch.setattr(deletion, "deletion_snapshot", lambda: snapshot)
    with REGISTRY.temporary(adapter):
        with pytest.raises(deletion.DeletionError, match="unknown"):
            deletion.assert_idle([item])


def test_deletion_snapshot_retains_unreadable_native_not_system_cmdline(monkeypatch):
    # Call the real implementation despite the autouse fictional snapshot stub.
    real_snapshot = _REAL_SNAPSHOT

    class Process:
        def __init__(self, pid, name):
            self.info = {"pid": pid, "name": name}

        def cmdline(self):
            raise psutil.AccessDenied(self.info["pid"])

        def create_time(self):
            return 1

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda attributes: iter((Process(991, "opencode.exe"), Process(992, "lsass.exe"))),
    )
    snapshot = real_snapshot()
    assert {row.pid for row in snapshot.process_snapshot} == {991, 992}
    assert snapshot.process_snapshot[0].command == ()


_REAL_SNAPSHOT = deletion.deletion_snapshot


def test_recreated_original_never_reports_completion(tmp_path, monkeypatch):
    path = tmp_path / "history"
    path.write_text("original")
    change = deletion.FileChange.capture(tmp_path, path)
    plan = deletion.DeletePlan("fixture", NativeRef("fiction", str(path)), None, (change,))
    identity = deletion.file_identity
    mutated = False

    def recreate(candidate):
        nonlocal mutated
        if candidate.name.startswith(".delete-") and not mutated:
            mutated = True
            path.write_text("concurrent history")
        return identity(candidate)

    monkeypatch.setattr(deletion, "file_identity", recreate)
    with pytest.raises(deletion.DeletionError, match="PARTIAL"):
        deletion.commit_plan(plan)
    assert path.read_text() == "concurrent history"
    assert list(tmp_path.glob(".delete-*"))


def test_rollback_failure_still_restores_files_and_reports_uncertainty(tmp_path, monkeypatch):
    path = tmp_path / "history"
    path.write_text("original")
    db_path = tmp_path / "native.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT)")
    with sqlite3.connect(db_path) as connection:
        fingerprint = deletion.database_fingerprint(connection)
    db = deletion.DatabaseChange(
        tmp_path, db_path, fingerprint, (("DELETE FROM missing WHERE id=?", (IDENT,)),)
    )
    plan = deletion.DeletePlan(
        "fixture",
        NativeRef(IDENT, str(path)),
        None,
        (deletion.FileChange.capture(tmp_path, path),),
        (db,),
    )
    connect = sqlite3.connect

    class BrokenRollback:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def rollback(self):
            raise sqlite3.OperationalError("fictional rollback I/O failure")

    monkeypatch.setattr(
        deletion.sqlite3,
        "connect",
        lambda *args, **kwargs: BrokenRollback(connect(*args, **kwargs)),
    )
    with pytest.raises(deletion.DeletionError, match="PARTIAL.*state uncertain"):
        deletion.commit_plan(plan)
    assert path.read_text() == "original"
    assert not list(tmp_path.glob(".delete-*"))
