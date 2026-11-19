"""Deletion tests operate exclusively on fictional homes; no native processes."""

import io
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_sessions import deletion
from ai_sessions.capabilities import Unsupported
from ai_sessions.model import NativeRef, Session
from ai_sessions.registry import REGISTRY

IDENT = "12345678-1234-1234-8234-123456789abc"
OTHER = "12345678-1234-1234-8234-123456789def"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Also enforced by the test-run command before imports. Per-test overrides
    # protect future additions which consult the environment dynamically.
    for name, relative in (
        ("AI_SESSIONS_CONFIG_FILE", "config.toml"),
        ("AI_SESSIONS_MESSAGING_DIR", "messaging"),
        ("AI_SESSIONS_STATE_FILE", "state.json"),
        ("AI_SESSIONS_CACHE_DIR", "cache"),
    ):
        monkeypatch.setenv(name, str(tmp_path / relative))
    (tmp_path / "config.toml").write_text("")
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    from ai_sessions import app

    monkeypatch.setattr(app, "CATALOG_CACHE_FILE", tmp_path / "cache" / "catalog.json")
    monkeypatch.setattr(app, "LEGACY_CATALOG_FILE", tmp_path / "cache" / "legacy.json")
    monkeypatch.setattr(app, "sessions_are_known", lambda items: True)
    from ai_sessions.liveness import LivenessContext, ProcessInfo

    monkeypatch.setattr(
        deletion,
        "deletion_snapshot",
        lambda: LivenessContext(
            "win32", (ProcessInfo(1, "fictional-test-host", (), "start", 1),), {}
        ),
    )
    # Fail immediately if a future deletion path starts any native process.
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("deletion tests must never launch native processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def transcript(home, ident=IDENT, text="hello"):
    path = home / "projects" / "fiction" / f"{ident}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": ident,
                "uuid": ident,
                "cwd": str(home),
                "message": {"role": "user", "content": text},
            }
        )
        + "\n"
    )
    return path


@pytest.fixture
def native(tmp_path):
    from ai_sessions.harnesses import claude

    home = tmp_path / "native"
    path = transcript(home)
    other = transcript(home, OTHER)
    adapter = replace(claude.ADAPTER, home=home, inspect_liveness=lambda *args: {})
    with REGISTRY.temporary(adapter):
        yield home, path, other, adapter


def session(path, ident=IDENT):
    return Session(
        "claude",
        ident,
        "Fiction title",
        str(path.parent),
        1,
        1,
        "",
        True,
        str(path),
        handle="fiction-ses-1",
    )


def command_setup(native, tmp_path, monkeypatch):
    from ai_sessions.app import UserState

    home, path, other, _ = native
    item = session(path)
    state = UserState(tmp_path / "state.json")
    state.slugs.ensure([])
    # Native-ID resolution remains the shared resolver; stub only linked-copy
    # lookup, which separate tests exercise against a real durable registry.
    state.handles = SimpleNamespace(lookup_members=lambda handle: None)
    monkeypatch.setattr(deletion, "catalog", lambda: ([item], state))
    monkeypatch.setattr(deletion.sys, "stdin", io.StringIO())
    return item, state


def test_dry_run_noninteractive_no_mutation(native, tmp_path, monkeypatch, capsys):
    item, _ = command_setup(native, tmp_path, monkeypatch)
    before = native[1].read_bytes()
    assert deletion.main([IDENT, "--dry-run"]) == 0
    assert native[1].read_bytes() == before
    output = capsys.readouterr().out
    assert str(native[1]) in output and IDENT in output and "Fiction title" in output
    assert "Linked native copies: 1" in output


def test_noninteractive_refuses(native, tmp_path, monkeypatch, capsys):
    command_setup(native, tmp_path, monkeypatch)
    assert deletion.main([IDENT]) == 1
    assert native[1].exists()
    assert "interactive confirmation" in capsys.readouterr().err


def confirm(monkeypatch, text):
    monkeypatch.setattr(deletion.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda: text)


def test_wrong_confirmation_preserves(native, tmp_path, monkeypatch):
    command_setup(native, tmp_path, monkeypatch)
    confirm(monkeypatch, "FICTION-SES-1")
    assert deletion.main([IDENT]) == 1
    assert native[1].exists()


def test_exact_confirmation_deletes_only_selected(native, tmp_path, monkeypatch):
    command_setup(native, tmp_path, monkeypatch)
    confirm(monkeypatch, "fiction-ses-1")
    assert deletion.main([IDENT]) == 0
    assert not native[1].exists()
    assert native[2].exists()
    assert not list(native[0].rglob("*.delete-*"))


def test_unknown_and_running_refuse(native, tmp_path, monkeypatch):
    from ai_sessions import app

    item, _ = command_setup(native, tmp_path, monkeypatch)
    monkeypatch.setattr(app, "sessions_are_known", lambda items: False)
    assert deletion.main([IDENT, "--dry-run"]) == 0
    assert deletion.main([IDENT]) == 1
    monkeypatch.setattr(app, "sessions_are_known", lambda items: True)
    item.is_open = True
    assert deletion.main([IDENT, "--dry-run"]) == 0
    assert deletion.main([IDENT]) == 1
    assert native[1].exists()


def test_unknown_capability_refuses(native, tmp_path, monkeypatch):
    command_setup(native, tmp_path, monkeypatch)
    with REGISTRY.temporary(replace(native[3], inspect_liveness=Unsupported("unknown"))):
        assert deletion.main([IDENT, "--dry-run"]) == 0
        assert deletion.main([IDENT]) == 1


def test_mutation_while_confirming_refuses(native, tmp_path, monkeypatch):
    command_setup(native, tmp_path, monkeypatch)
    confirm(monkeypatch, "unused")

    def mutate():
        with native[1].open("a") as output:
            output.write(
                json.dumps({"sessionId": IDENT, "type": "custom-title", "customTitle": "changed"})
                + "\n"
            )
        return "fiction-ses-1"

    monkeypatch.setattr("builtins.input", mutate)
    assert deletion.main([IDENT]) == 1
    assert native[1].exists()


def test_running_after_confirmation_refuses(native, tmp_path, monkeypatch):
    item, _ = command_setup(native, tmp_path, monkeypatch)
    confirm(monkeypatch, "unused")

    def run():
        item.is_open = True
        return "fiction-ses-1"

    monkeypatch.setattr("builtins.input", run)
    assert deletion.main([IDENT]) == 1
    assert native[1].exists()


def test_no_yes_bypass(native, tmp_path, monkeypatch):
    command_setup(native, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as error:
        deletion.main([IDENT, "--yes"])
    assert error.value.code == 2


def test_qualified_native_id_confirmation_without_handle(native, tmp_path, monkeypatch):
    item, _ = command_setup(native, tmp_path, monkeypatch)
    item.handle = ""
    confirm(monkeypatch, f"claude:{IDENT}")
    assert deletion.main([f"claude:{IDENT}"]) == 0


def test_provider_roundtrip_and_metadata(native):
    from ai_sessions.discovery import HarnessContext
    from ai_sessions.harnesses import claude

    home, path, other, adapter = native
    history = home / "history.jsonl"
    history.write_text(
        json.dumps({"sessionId": IDENT, "display": "private"})
        + "\n"
        + json.dumps({"sessionId": OTHER, "display": "keep"})
        + "\n"
    )
    index = path.parent / "sessions-index.json"
    index.write_text(
        json.dumps({"version": 1, "entries": [{"sessionId": IDENT}, {"sessionId": OTHER}]})
    )
    context = HarnessContext.create(use_cache=False)
    assert IDENT in {row.session_id for row in claude.discover(context, use_cache=False)}
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    deletion.commit_plan(plan)
    assert IDENT not in {
        row.session_id
        for row in claude.discover(HarnessContext.create(use_cache=False), use_cache=False)
    }
    assert OTHER in history.read_text() and IDENT not in history.read_text()
    assert json.loads(index.read_text())["entries"] == [{"sessionId": OTHER}]
    assert other.exists()


def test_compressed_transcript_included(native):
    import zstandard

    home, path, _, adapter = native
    compressed = path.with_name(path.name + ".zst")
    compressed.write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
    plan = adapter.plan_delete(adapter.resolve(IDENT))
    assert {path, compressed} <= {change.path for change in plan.files}
    deletion.commit_plan(plan)
    assert not path.exists() and not compressed.exists()


def test_parent_children_refused(native):
    _, path, _, adapter = native
    child = path.with_suffix("") / "subagents" / "agent-fiction.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("keep")
    with pytest.raises(deletion.DeletionError, match="child resources"):
        adapter.plan_delete(adapter.resolve(IDENT))
    assert child.exists()


def test_mismatching_transcript_identity_refused(native):
    native[1].write_text(json.dumps({"sessionId": OTHER}) + "\n")
    with pytest.raises(deletion.DeletionError, match="mismatch"):
        native[3].plan_delete(native[3].resolve(IDENT))


def test_path_escape_and_hardlink_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("keep")
    with pytest.raises(deletion.DeletionError, match="escapes"):
        deletion.FileChange.capture(home, outside)
    target = home / "hardlink"
    os.link(outside, target)
    with pytest.raises(deletion.DeletionError, match="exclusively"):
        deletion.FileChange.capture(home, target)


def test_symlink_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("keep")
    try:
        (home / "link").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    with pytest.raises(deletion.DeletionError, match="linked"):
        deletion.FileChange.capture(home, home / "link")


def test_mutation_between_plan_and_commit_refused(native):
    plan = native[3].plan_delete(native[3].resolve(IDENT))
    native[1].write_text("changed")
    with pytest.raises(deletion.DeletionError, match="changed"):
        deletion.commit_plan(plan)
    assert native[1].read_text() == "changed"


def test_db_failure_rolls_back_quarantine(tmp_path):
    home = tmp_path
    path = home / "history"
    path.write_text("selected")
    database = home / "native.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO session VALUES ('selected')")
    with sqlite3.connect(database) as connection:
        fingerprint = deletion.database_fingerprint(connection)
    db = deletion.DatabaseChange(
        home, database, fingerprint, (("DELETE FROM missing WHERE id=?", ("selected",)),)
    )
    plan = deletion.DeletePlan(
        "fixture",
        NativeRef("selected", str(path)),
        None,
        (deletion.FileChange.capture(home, path),),
        (db,),
    )
    with pytest.raises(sqlite3.Error):
        deletion.commit_plan(plan)
    assert path.read_text() == "selected"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM session").fetchall() == [("selected",)]


def test_purge_failure_reported_partial(native, monkeypatch):
    plan = native[3].plan_delete(native[3].resolve(IDENT))
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if ".delete-" in path.name:
            raise OSError("fixture purge failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(deletion.DeletionError, match="PARTIAL.*purge"):
        deletion.commit_plan(plan)
    assert not native[1].exists()


def test_all_copies_composes_shared_metadata(native, tmp_path, monkeypatch):
    from ai_sessions.app import UserState
    from ai_sessions.references import native_key

    home, path, other, _ = native
    first, second = session(path), session(other, OTHER)
    state = UserState(tmp_path / "state.json")
    state.handles.ensure([first, second])
    state.handles.link(first, second)
    members = state.handles.lookup_members(first.handle)
    assert {native_key(row.tool, row.session_id) for row in (first, second)} == set(members)
    history = home / "history.jsonl"
    history.write_text(
        json.dumps({"sessionId": IDENT})
        + "\n"
        + json.dumps({"sessionId": OTHER})
        + "\n"
        + json.dumps({"sessionId": "unrelated"})
        + "\n"
    )
    monkeypatch.setattr(deletion, "catalog", lambda: ([first, second], state))
    confirm(monkeypatch, first.handle)
    assert deletion.main([first.handle, "--all-copies"]) == 0
    assert not path.exists() and not other.exists()
    assert json.loads(history.read_text()) == {"sessionId": "unrelated"}
    assert state.handles.lookup_members(first.handle) == members  # Durable claims stay reserved.


def test_local_cleanup_preserves_unselected_and_claims(native, tmp_path):
    from ai_sessions import app
    from ai_sessions.app import UserState

    home, path, other, _ = native
    item = session(path)
    state = UserState(tmp_path / "state.json")
    selected, keep = item.key, f"claude:{OTHER}"
    state.path.write_text(
        json.dumps(
            {
                "version": 7,
                "names": {selected: "remove", keep: "keep"},
                "hidden": [selected, keep],
                "bridges": {keep: {"claude": {"session_id": IDENT}}},
                "conversations": {
                    "logical": {
                        "members": {
                            selected: {"tool": "claude", "session_id": IDENT},
                            keep: {"tool": "claude", "session_id": OTHER},
                        }
                    }
                },
            }
        )
    )
    app.CATALOG_CACHE_FILE.parent.mkdir(parents=True)
    app.CATALOG_CACHE_FILE.write_text(
        json.dumps(
            {
                "sessions": [
                    {"tool": "claude", "session_id": IDENT},
                    {"tool": "claude", "session_id": OTHER},
                ]
            }
        )
    )
    cleanup = deletion.local_cleanup([item], state)
    deletion.commit_plan(
        deletion.DeletePlan("local", NativeRef("local", str(state.path)), None, cleanup)
    )
    document = json.loads(state.path.read_text())
    assert document["names"] == {keep: "keep"}
    assert document["hidden"] == [keep]
    assert document["bridges"] == {keep: {}}
    assert list(document["conversations"]["logical"]["members"]) == [keep]
    assert json.loads(app.CATALOG_CACHE_FILE.read_text())["sessions"] == [
        {"tool": "claude", "session_id": OTHER}
    ]
    assert path.exists() and other.exists()  # This test exercises only local cleanup.
