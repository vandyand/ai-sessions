import json
from types import SimpleNamespace

import pytest

from ai_sessions import handle_lookup as lookup
from ai_sessions.app import Session, UserState
from ai_sessions.handles_cli import main
from ai_sessions.registry import REGISTRY


@pytest.fixture
def sample(tmp_path, monkeypatch):

    state = UserState(tmp_path / "state.json")
    items = [
        Session(
            "codex",
            "current",
            "Elevate manager",
            str(tmp_path / "elevate"),
            20,
            10,
            "",
            True,
            "fiction",
        ),
        Session(
            "claude",
            "other",
            "Other conversation",
            str(tmp_path / "elevate"),
            10,
            5,
            "",
            True,
            "other",
        ),
    ]
    state.handles.ensure(items)
    monkeypatch.setattr(lookup, "catalog", lambda: (items, state))
    monkeypatch.setattr(lookup, "managed_session", lambda: None)
    monkeypatch.setattr(lookup, "_managed_native_session", lambda: None)
    for adapter in REGISTRY.adapters():
        if adapter.messaging and adapter.messaging.sender_environment:
            monkeypatch.delenv(adapter.messaging.sender_environment, raising=False)
    return items, state


def test_whoami_uses_native_identity_and_shows_handle(sample, monkeypatch, capsys):
    items, _ = sample
    monkeypatch.setenv(REGISTRY.get("codex").messaging.sender_environment, "current")
    assert main(["whoami"]) == 0
    output = capsys.readouterr().out
    assert items[0].handle in output and "Elevate manager" in output


def test_whoami_short_and_json(sample, monkeypatch, capsys):
    items, _ = sample
    monkeypatch.setenv(REGISTRY.get("codex").messaging.sender_environment, "current")
    assert main(["whoami", "--short"]) == 0
    assert capsys.readouterr().out.strip() == items[0].handle
    assert main(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "current"


def test_whoami_managed_session_and_plain_shell_refusal(sample, monkeypatch, capsys):
    items, _ = sample
    monkeypatch.setattr(lookup, "managed_session", lambda: ("claude", "other"))
    assert main(["whoami"]) == 0
    assert items[1].handle in capsys.readouterr().out
    monkeypatch.setattr(lookup, "managed_session", lambda: None)
    assert main(["whoami"]) == 2
    assert "inside" in capsys.readouterr().err


def test_bare_handle_alias(sample, monkeypatch, capsys):
    items, _ = sample
    monkeypatch.setenv(REGISTRY.get("codex").messaging.sender_environment, "current")
    assert main(["handle"]) == 0
    assert items[0].handle in capsys.readouterr().out


def test_ambiguous_native_environments_require_matching_managed_identity(monkeypatch):
    adapters = [
        SimpleNamespace(name=name, messaging=SimpleNamespace(sender_environment=key))
        for name, key in (("first", "TEST_NATIVE_A"), ("second", "TEST_NATIVE_B"))
    ]
    monkeypatch.setattr(lookup.REGISTRY, "adapters", lambda: adapters)
    monkeypatch.setenv("TEST_NATIVE_A", "one")
    monkeypatch.setenv("TEST_NATIVE_B", "two")
    monkeypatch.setattr(lookup, "managed_session", lambda: None)
    with pytest.raises(ValueError, match="inside"):
        lookup.current_identity()
    monkeypatch.setattr(lookup, "managed_session", lambda: ("second", "two"))
    assert lookup.current_identity() == ("second", "two")


def test_current_harness_wins_over_inherited_other_harness_environment(sample, monkeypatch):
    monkeypatch.setenv(REGISTRY.get("codex").messaging.sender_environment, "old")
    monkeypatch.setattr(lookup, "managed_session", lambda: ("claude", "other"))
    assert lookup.current_identity() == ("claude", "other")


def test_current_native_thread_wins_over_old_launch_in_same_harness(sample, monkeypatch):
    monkeypatch.setenv(REGISTRY.get("codex").messaging.sender_environment, "current")
    monkeypatch.setattr(lookup, "managed_session", lambda: ("codex", "old"))
    assert lookup.current_identity() == ("codex", "current")


def test_uncached_identity_never_selects_an_arbitrary_native_copy(sample, monkeypatch, capsys):
    _, state = sample
    monkeypatch.setattr(lookup, "catalog", lambda: ([], state))
    monkeypatch.setattr(lookup, "current_identity", lambda: ("codex", "unindexed"))
    assert main(["whoami"]) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "not indexed yet" in result.err


def test_native_claude_ancestor_takes_precedence_over_remembered_cmd_launcher(monkeypatch):
    from unittest.mock import Mock

    from ai_sessions.capabilities import NativeRef

    adapter = SimpleNamespace(
        name="claude",
        liveness_executables={"claude.exe"},
        current_session=Mock(
            side_effect=lambda pid, started: (
                NativeRef("actual", "actual.jsonl") if pid == 42 else None
            )
        ),
    )
    monkeypatch.setattr(lookup.REGISTRY, "get", lambda name: adapter)
    monkeypatch.setattr(lookup.REGISTRY, "adapters", lambda: [adapter])
    monkeypatch.setattr(
        "ai_sessions.detached.running_here",
        lambda: ("terminal", {"tool": "claude", "pid": 41, "started": 10}),
    )
    native = SimpleNamespace(pid=42, name=lambda: "claude.exe", create_time=lambda: 11)
    monkeypatch.setattr("psutil.Process", lambda: SimpleNamespace(parents=lambda: [native]))
    assert lookup.managed_session() == ("claude", "actual")
    assert lookup._managed_native_session() == ("claude", NativeRef("actual", "actual.jsonl"))


def test_ancestor_inspection_failure_preserves_verified_managed_fallback(monkeypatch):
    from unittest.mock import Mock

    import psutil

    from ai_sessions.capabilities import NativeRef

    adapter = SimpleNamespace(
        name="claude", current_session=lambda pid, started: NativeRef("verified", "verified.jsonl")
    )
    monkeypatch.setattr(lookup.REGISTRY, "get", lambda name: adapter)
    monkeypatch.setattr(
        "ai_sessions.detached.running_here",
        lambda: ("terminal", {"tool": "claude", "pid": 42, "started": 10}),
    )
    monkeypatch.setattr("psutil.Process", Mock(side_effect=psutil.AccessDenied(42)))
    assert lookup.managed_session() == ("claude", "verified")


def test_uncached_verified_session_is_registered_and_saved_for_assignment(
    sample, monkeypatch, tmp_path, capsys
):
    from dataclasses import replace

    from ai_sessions import app, handles_cli
    from ai_sessions.model import NativeRef, NativeSession

    items, state = sample
    native = NativeSession(
        "claude",
        "new-current",
        "top-lego-pwa-implementation-worker-2",
        str(tmp_path),
        30,
        20,
        "",
        True,
        str(tmp_path / "current.jsonl"),
    )
    ref = NativeRef(native.session_id, native.storage)
    monkeypatch.setattr(lookup, "current_identity", lambda: ("claude", "new-current"))
    monkeypatch.setattr(lookup, "_managed_native_session", lambda: ("claude", ref), raising=False)
    original_get = REGISTRY.get

    def rediscover(context, refs):
        assert refs == [ref]
        assert context.use_cache is False
        return [native]

    adapter = replace(original_get("claude"), rediscover=rediscover)
    monkeypatch.setattr(
        REGISTRY, "get", lambda name: adapter if name == "claude" else original_get(name)
    )
    path = tmp_path / "catalog.json"
    save = app.save_session_catalog
    load = app.load_session_catalog
    monkeypatch.setattr(app, "save_session_catalog", lambda rows: save(rows, path))
    monkeypatch.setattr(
        app, "load_session_catalog", lambda state=None, path=path: load(state, path)
    )
    assert main(["whoami", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["session_id"] == "new-current"
    assert row["handle"] and row["project"] != "lego"
    saved = app.load_session_catalog(state, path)
    assert {s.session_id for s in saved} == {"current", "other", "new-current"}
    assert next(s for s in saved if s.session_id == "new-current").handle == row["handle"]
    from ai_sessions.references import resolve_session

    assert resolve_session(row["handle"], saved, store=state.slugs).session_id == "new-current"
    state.handles.add_project("lego", str(tmp_path / "lego"))
    monkeypatch.setattr(
        handles_cli, "catalog", lambda: (app.load_session_catalog(state, path), state)
    )
    assert (
        main(
            [
                "handle",
                "assign",
                "new-current",
                "--project",
                "lego",
                "--role",
                "wrk",
                "--number",
                "2",
            ]
        )
        == 0
    )
    assigned_handle = capsys.readouterr().out.strip()
    assert assigned_handle == "lego-wrk-2"
    reloaded = app.load_session_catalog(state, path)
    assert resolve_session(assigned_handle, reloaded, store=state.slugs).session_id == "new-current"


@pytest.mark.parametrize("mismatch", ["identity", "storage", "duplicate", "catalog-home"])
def test_uncached_verified_lookup_refuses_foreign_or_ambiguous_rows(
    sample, monkeypatch, tmp_path, capsys, mismatch
):
    from dataclasses import replace

    from ai_sessions import app
    from ai_sessions.model import NativeRef, NativeSession

    _, state = sample
    ref = NativeRef("new", str(tmp_path / "current.jsonl"))
    native = NativeSession("claude", "new", "Title", str(tmp_path), 30, 20, "", True, ref.storage)
    rows = {
        "identity": [replace(native, session_id="foreign")],
        "storage": [replace(native, storage=str(tmp_path / "copy.jsonl"))],
        "duplicate": [native, replace(native, storage=str(tmp_path / "copy.jsonl"))],
        "catalog-home": [native],
    }[mismatch]
    monkeypatch.setattr(lookup, "current_identity", lambda: ("claude", "new"))
    monkeypatch.setattr(lookup, "_managed_native_session", lambda: ("claude", ref), raising=False)
    original_get = REGISTRY.get
    adapter = replace(original_get("claude"), rediscover=lambda context, refs: rows)
    monkeypatch.setattr(
        REGISTRY, "get", lambda name: adapter if name == "claude" else original_get(name)
    )
    monkeypatch.setattr(
        app, "save_session_catalog", lambda rows: pytest.fail("must not save ambiguous lookup")
    )
    competing = app.session_from_native(replace(native, storage=str(tmp_path / "copy.jsonl")))
    monkeypatch.setattr(
        app, "load_session_catalog", lambda: [competing] if mismatch == "catalog-home" else []
    )
    assert main(["whoami"]) == 2
    assert not capsys.readouterr().out


def test_registration_does_not_report_success_when_catalog_write_is_lost(
    sample, monkeypatch, tmp_path, capsys
):
    from dataclasses import replace

    from ai_sessions import app
    from ai_sessions.model import NativeRef, NativeSession

    ref = NativeRef("new", str(tmp_path / "current.jsonl"))
    native = NativeSession("claude", "new", "Title", str(tmp_path), 30, 20, "", True, ref.storage)
    monkeypatch.setattr(lookup, "current_identity", lambda: ("claude", "new"))
    monkeypatch.setattr(lookup, "_managed_native_session", lambda: ("claude", ref))
    original_get = REGISTRY.get
    adapter = replace(original_get("claude"), rediscover=lambda context, refs: [native])
    monkeypatch.setattr(
        REGISTRY, "get", lambda name: adapter if name == "claude" else original_get(name)
    )
    monkeypatch.setattr(app, "save_session_catalog", lambda rows: None)
    monkeypatch.setattr(app, "load_session_catalog", lambda: [])
    assert main(["whoami"]) == 2
    result = capsys.readouterr()
    assert not result.out and "persist" in result.err


def test_current_thread_change_during_registration_refuses_old_native_reference(
    sample, monkeypatch, capsys
):
    from ai_sessions.model import NativeRef

    monkeypatch.setattr(lookup, "current_identity", lambda: ("claude", "new"))
    monkeypatch.setattr(
        lookup, "_managed_native_session", lambda: ("claude", NativeRef("old", "old.jsonl"))
    )
    assert main(["whoami"]) == 2
    assert "not indexed yet" in capsys.readouterr().err
