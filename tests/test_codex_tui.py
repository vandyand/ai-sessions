"""Interactive renderer installation must never replace the RPC executable."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from ai_sessions.config import LaunchConfig
from ai_sessions.harnesses import codex_tui as tui


@pytest.fixture
def renderer(tmp_path, monkeypatch):
    manifest = tmp_path / "installed" / "active.json"
    monkeypatch.setenv("AI_SESSIONS_CODEX_TUI_MANIFEST", str(manifest))
    source = tmp_path / "artifact.exe"
    source.write_bytes(b"MZ-renderer-fixture")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = []

    def version(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.170.0\n", stderr="")

    monkeypatch.setattr(tui.subprocess, "run", version)
    return source, digest, manifest, calls


def test_absent_default_manifest_preserves_stock_command(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_SESSIONS_CODEX_TUI_MANIFEST", raising=False)
    monkeypatch.setattr(tui, "default_manifest", lambda: tmp_path / "absent.json")
    assert LaunchConfig().provider_prefix("codex") == ["codex"]


def test_explicit_missing_override_fails_clearly(renderer):
    with pytest.raises((ValueError, OSError), match="manifest|renderer"):
        LaunchConfig().provider_prefix("codex")


def test_install_routes_tui_only_and_preserves_mode_and_prefix_arguments(renderer):
    source, digest, manifest, calls = renderer
    installed = tui.install(source, digest, "0.170.0")
    config = LaunchConfig(mode="dangerous", codex_command=["codex", "--profile", "mine"])
    assert config.provider_prefix("codex") == [
        str(installed),
        "--profile",
        "mine",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert config.provider_command("codex") == ["codex", "--profile", "mine"]
    assert config.provider_prefix("claude") == ["claude", "--dangerously-skip-permissions"]
    assert json.loads(manifest.read_text())["sha256"] == digest
    assert source.read_bytes() == installed.read_bytes()
    assert all(command[1:] == ["--version"] for command in calls)


@pytest.mark.parametrize("damage", ["digest", "missing", "version"])
def test_active_renderer_validation_fails_closed(renderer, monkeypatch, damage):
    source, digest, _, _ = renderer
    installed = tui.install(source, digest, "0.170.0")
    if damage == "digest":
        installed.write_bytes(b"changed")
    elif damage == "missing":
        installed.unlink()
    else:
        monkeypatch.setattr(
            tui.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="codex-cli 9.0.0", stderr=""),
        )
    with pytest.raises((ValueError, OSError), match="renderer|digest|version"):
        LaunchConfig().provider_prefix("codex")


def test_invalid_artifact_does_not_replace_active_install(renderer, monkeypatch):
    source, digest, manifest, _ = renderer
    installed = tui.install(source, digest, "0.170.0")
    previous = manifest.read_bytes()
    source.write_bytes(b"different")
    with pytest.raises(ValueError, match="digest"):
        tui.install(source, digest, "0.170.0")
    assert manifest.read_bytes() == previous
    assert installed.is_file()


def test_disable_restores_stock_even_with_explicit_manifest(renderer):
    source, digest, _, _ = renderer
    installed = tui.install(source, digest, "0.170.0")
    tui.disable()
    assert LaunchConfig().provider_prefix("codex") == ["codex"]
    assert installed.is_file()


def test_resume_uses_renderer_but_rpc_resolver_stays_stock(renderer, monkeypatch, tmp_path):
    from ai_sessions import app
    from ai_sessions.harnesses import messaging_rpc

    source, digest, _, _ = renderer
    installed = tui.install(source, digest, "0.170.0")
    monkeypatch.setattr("ai_sessions.messaging.install.settings", lambda: {"enabled": False})
    session = app.Session("codex", "own", "Title", str(tmp_path), 2, 1, "", True, "fixture.jsonl")
    assert app.command_for(session, LaunchConfig()) == [str(installed), "resume", "own"]
    monkeypatch.setattr(messaging_rpc.sys, "platform", "linux")
    monkeypatch.setattr(messaging_rpc.shutil, "which", lambda name: "/stock/codex")
    assert messaging_rpc.native_executable() == "/stock/codex"


def test_cdba_managed_new_session_and_restart_use_tui_override(renderer, monkeypatch, tmp_path):
    from contextlib import nullcontext
    from dataclasses import replace

    from ai_sessions import app, new_session
    from ai_sessions.registry import REGISTRY

    source, digest, _, _ = renderer
    installed = tui.install(source, digest, "0.170.0")
    adapter = replace(REGISTRY.get("codex"), native_executable=lambda: "stock-codex.exe")
    original_get = REGISTRY.get
    monkeypatch.setattr(
        REGISTRY, "get", lambda name: adapter if name == "codex" else original_get(name)
    )
    current = app.Session("codex", "own", "Title", str(tmp_path), 2, 1, "", True, "fixture.jsonl")
    flag = "--dangerously-bypass-approvals-and-sandbox"
    monkeypatch.setattr(new_session, "_specs", lambda *a: {flag: (False, False, False)})
    monkeypatch.setattr(
        new_session, "_prepare_initial", lambda adapter, exe, args, *a: ([exe, *args], current)
    )
    monkeypatch.setattr(new_session, "_workspace_context", lambda *a: nullcontext())
    monkeypatch.setattr(new_session, "_remember", lambda *a: None)
    monkeypatch.setattr(new_session, "_started", lambda pid: 1)
    outcomes = iter(["", None])
    monkeypatch.setattr(new_session, "_closed", lambda pid: next(outcomes))
    monkeypatch.setattr(new_session, "_latest_registered", lambda *a: None)
    launched = []

    def launch(command, **kwargs):
        launched.append(list(command))
        return SimpleNamespace(pid=len(launched), wait=lambda **kw: 7)

    monkeypatch.setattr(new_session.subprocess, "Popen", launch)
    assert new_session.run("codex", [flag]) == 7
    assert launched == [[str(installed), flag], [str(installed), flag, "resume", "own"]]
    assert adapter.native_executable() == "stock-codex.exe"


def test_invalid_renderer_prevents_restart_before_stopping_current_client(
    renderer, monkeypatch, capsys
):
    from ai_sessions import app, detached

    monkeypatch.setattr(
        detached, "running_here", lambda: ("tab", dict(tool="codex", pid=42, started=1))
    )
    monkeypatch.setattr(
        detached, "request_restart", lambda *a: pytest.fail("must not request restart")
    )
    monkeypatch.setattr(
        detached, "stop_for_restart", lambda *a: pytest.fail("must not stop client")
    )
    assert app.restart_here() == 2
    assert "Nothing was stopped" in capsys.readouterr().err


def test_install_cli_explains_fresh_launcher_activation_and_status(renderer, capsys):
    source, digest, _, _ = renderer
    assert tui.main(["install", str(source), "--sha256", digest, "--version", "0.170.0"]) == 0
    output = capsys.readouterr().out
    assert "ses --reopen" in output and "fresh" in output and "sesres" in output
    assert tui.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["renderer"] is True
    assert tui.main(["disable"]) == 0
    capsys.readouterr()
    assert tui.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["renderer"] is False
