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
