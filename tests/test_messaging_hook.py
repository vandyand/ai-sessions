"""Late self-registration only; no live transports or credential helpers."""

import io
import json
from dataclasses import replace

import pytest

from ai_sessions.messaging import hook, install, store
from ai_sessions.registry import REGISTRY


def test_installer_adds_and_removes_only_owned_late_claude_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "other"}]}]}}))
    install.edit_hooks(path, True, "claude")
    first = path.read_bytes()
    config = json.loads(first)["hooks"]
    for event in ("UserPromptSubmit", "PreToolUse"):
        commands = [h["command"] for group in config[event] for h in group["hooks"]]
        assert sum("ai_sessions.messaging.hook ensure claude" in c for c in commands) == 1
    install.edit_hooks(path, True, "claude")
    assert path.read_bytes() == first
    install.edit_hooks(path, False, "claude")
    assert json.loads(path.read_bytes())["hooks"] == {
        "PreToolUse": [{"hooks": [{"command": "other"}]}]
    }


@pytest.fixture
def registration(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(install, "settings", lambda: {"enabled": True})
    calls = []
    outcomes = [
        None,
        dict(
            tool="claude", session_id="own", pid=42, started=1, home="home", generation="generation"
        ),
    ]

    def register(payload, env):
        calls.append(dict(payload))
        return outcomes.pop(0) if outcomes else store._read(store._path("receivers", "claude:own"))

    adapter = REGISTRY.get("claude")
    hooks = replace(
        adapter.messaging, register=register, forget=lambda row: pytest.fail("must not forget")
    )
    selected = replace(adapter, messaging=hooks)
    get = REGISTRY.get
    monkeypatch.setattr(REGISTRY, "get", lambda tool: selected if tool == "claude" else get(tool))
    return calls


def test_late_hook_registers_without_guidance_or_cleanup(registration, monkeypatch, capsys):
    def invoke(event):
        monkeypatch.setattr(hook.sys, "argv", ["hook", event, "claude"])
        monkeypatch.setattr(hook.sys, "stdin", io.StringIO('{"session_id":"own"}'))
        assert hook.main() == 0

    invoke("start")
    assert "Session messaging is available" not in capsys.readouterr().out
    invoke("ensure")
    assert store._read(store._path("receivers", "claude:own"))["pid"] == 42
    saved = store._path("receivers", "claude:own").read_bytes()
    invoke("ensure")
    assert store._path("receivers", "claude:own").read_bytes() == saved
    assert not capsys.readouterr().out


def test_explicit_self_registration_needs_no_stdin_and_uses_verified_own_identity(
    registration, monkeypatch
):
    from ai_sessions import handle_lookup
    from ai_sessions.model import NativeRef

    monkeypatch.setattr(
        handle_lookup, "_managed_native_session", lambda: ("claude", NativeRef("own", "own.jsonl"))
    )

    class NoInput:
        def read(self):
            pytest.fail("explicit registration must not wait for stdin")

    monkeypatch.setattr(hook.sys, "stdin", NoInput())
    assert hook.register_current("claude")["status"] == "unavailable"
    assert hook.register_current("claude")["status"] == "registered"
    assert registration[-1]["session_id"] == "own"
    monkeypatch.setattr(
        handle_lookup,
        "_managed_native_session",
        lambda: ("codex", NativeRef("foreign", "foreign.jsonl")),
    )
    assert hook.register_current("claude")["status"] == "unavailable"


def test_explicit_registration_refuses_without_native_identity(registration, monkeypatch):
    from ai_sessions import handle_lookup

    monkeypatch.setattr(handle_lookup, "_managed_native_session", lambda: None)
    assert hook.register_current("claude")["status"] == "unavailable"
    assert not registration


def test_explicit_module_command_does_not_read_stdin(monkeypatch, capsys):
    monkeypatch.setattr(hook.sys, "argv", ["hook", "register", "claude"])
    monkeypatch.setattr(
        hook,
        "register_current",
        lambda tool: {"status": "registered", "tool": tool, "session_id": "own"},
    )

    class NoInput:
        def read(self):
            pytest.fail("explicit command must not read stdin")

    monkeypatch.setattr(hook.sys, "stdin", NoInput())
    assert hook.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "registered",
        "tool": "claude",
        "session_id": "own",
    }


def test_late_hook_errors_never_expose_prompt_or_credentials(registration, monkeypatch, capsys):
    monkeypatch.setattr(hook.sys, "argv", ["hook", "ensure", "claude"])
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO("not-json SECRET-CANARY"))
    assert hook.main() == 0
    output = capsys.readouterr()
    assert not output.out and "SECRET-CANARY" not in output.err
