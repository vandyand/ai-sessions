"""Grok harness rows that do not need a model or the owner pipe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("GROK_HOME", str(Path.cwd() / ".grok-test-home"))

from ai_sessions.capabilities import Unsupported
from ai_sessions.harnesses import grok
from ai_sessions.harnesses.desk_support import arguments, create_id, missing_can_start
from ai_sessions.model import ToolCall, Turn


def _fixture(tmp_path: Path, *, thought: str = "Private thought word: QUILL.") -> tuple[str, Path]:
    cwd = "C:\\t"
    session_id = "11111111-1111-4111-8111-111111111111"
    directory = grok.session_dir(cwd, session_id, tmp_path / "home")
    os.environ["GROK_HOME"] = str(tmp_path / "home")
    moment = int(time.time())
    lines = [
        grok._envelope(
            session_id,
            grok._chunk("user_message_chunk", "Remember this codeword: LARKSPUR."),
            moment,
        ),
        grok._envelope(session_id, grok._chunk("agent_thought_chunk", thought), moment),
        grok._envelope(
            session_id,
            grok._chunk("agent_message_chunk", "I will remember the codeword LARKSPUR."),
            moment,
        ),
        grok._envelope(
            session_id,
            {"sessionUpdate": "turn_completed", "prompt_id": "t", "stop_reason": "end_turn"},
            moment,
        ),
    ]
    directory.mkdir(parents=True)
    (directory / "updates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    grok.write_summary(directory, session_id, str(cwd), "Bridge proof LARKSPUR", time.time())
    return session_id, directory


def test_discover_and_read_drops_thought(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "home"))
    session_id, directory = _fixture(tmp_path)
    found = grok.discover(None)
    match = [row for row in found if row.session_id == session_id]
    assert len(match) == 1
    assert match[0].title == "Bridge proof LARKSPUR"
    assert match[0].storage.endswith("updates.jsonl")
    snapshot = grok._read(grok.NativeRef(session_id, match[0].storage))
    text = "\n".join(turn.text for turn in snapshot.transcript.turns)
    assert "LARKSPUR" in text
    assert "QUILL" not in text
    assert grok.resume_args(
        session_id=session_id, source=None, resume_id="", parent_id="", native=True
    ) == [
        "--resume",
        session_id,
    ]


def test_liveness_live_and_dead(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    session_id = "22222222-2222-4222-8222-222222222222"
    (home / "active_sessions.json").write_text(
        json.dumps([{"session_id": session_id, "pid": 99999999, "cwd": "x", "opened_at": "t"}]),
        encoding="utf-8",
    )
    assert grok.live_pid(session_id) is None
    held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (home / "active_sessions.json").write_text(
            json.dumps([{"session_id": session_id, "pid": held.pid, "cwd": "x", "opened_at": "t"}]),
            encoding="utf-8",
        )
        assert grok.live_pid(session_id) == held.pid
        directory = home / "sessions" / session_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "updates.jsonl").write_text("{}\n", encoding="utf-8")
        ref = grok.NativeRef(session_id, str(directory / "updates.jsonl"))
        with pytest.raises(Exception, match="live pid"):
            grok.plan_delete(ref)
        from ai_sessions.deletion import DeletePlan

        with pytest.raises(Exception, match="live pid"):
            grok.execute_delete(
                DeletePlan("grok", ref, 0, (), notes=("session directory",), native_delete=True)
            )
        assert directory.is_dir()
        assert held.poll() is None
    finally:
        held.kill()
        held.wait(timeout=10)


def test_write_omits_cache_and_thought_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "home"))
    written = grok.write(
        cwd="C:\\t",
        turns=[
            Turn("user", "alpha"),
            Turn(
                "assistant",
                "beta THOUGHTTOKEN",
                (ToolCall("read_file", '{"target_file":"notes.txt"}', "hello-from-tool"),),
            ),
        ],
        prepared=None,
        title="t",
    )
    directory = Path(written.native.storage).parent
    stream = (directory / "updates.jsonl").read_text(encoding="utf-8")
    assert "alpha" in stream and "beta" in stream and "hello-from-tool" in stream
    assert "THOUGHTTOKEN" not in stream
    assert not (directory / "chat_history.jsonl").exists()
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["chat_format_version"] == 1
    again = grok.reduce_updates(Path(written.native.storage))
    calls = [call for turn in again for call in turn.calls]
    assert calls and calls[0].name == "read_file" and "hello-from-tool" in calls[0].result


def test_rename_delete_and_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "home"))
    session_id, directory = _fixture(tmp_path)
    ref = grok.resolve(session_id)
    assert ref is not None

    class Session:
        pass

    item = Session()
    item.session_id = session_id
    assert grok.publish_name(item, "Renamed proof") == "Renamed proof"
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["generated_title"] == "Renamed proof" and summary["title_is_manual"] is True
    plan = grok.plan_delete(ref)
    grok.execute_delete(plan)
    assert not directory.exists()
    assert arguments("grok", "abc", fresh=True, prompt="desk-prompt") == [
        "--session-id",
        "abc",
        "desk-prompt",
    ]
    assert arguments("grok", "abc", fresh=False, prompt="desk-prompt") == [
        "--resume",
        "abc",
        "desk-prompt",
    ]
    assert missing_can_start("grok") is True

    class Adapter:
        name = "grok"
        create_session = Unsupported("no")

    assert create_id(Adapter(), tmp_path, "desk")
    assert grok.new_session_args(session_id="abc") == ["-s", "abc"]


def test_grown_transcript_uses_the_frontier_changed_status(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text('{"ok":true}\n', encoding="utf-8")
    ref = grok.NativeRef("8554a9c4-bccc-4b26-9a38-8db40663a800", str(path))
    assert grok.change_status(ref, 0) == "changed"
    assert grok.change_status(ref, path.stat().st_size) == "unchanged"


def test_generation_changes_with_the_process():
    first = grok._generation("sid", 10, 1.5)
    assert first != "1"
    assert first != grok._generation("sid", 11, 1.5)
    assert first == grok._generation("sid", 10, 1.5)


def test_message_send_does_not_invoke_grok_when_the_pipe_is_absent(monkeypatch):
    monkeypatch.setattr(grok, "live_pid", lambda session_id: 69144)
    monkeypatch.setattr(grok, "_pipe_open", lambda session_id: False)
    called = {}

    def fail(*args, **kwargs):
        called["ran"] = True
        raise AssertionError("message_send must not spawn grok")

    monkeypatch.setattr(subprocess, "run", fail)
    result = grok.message_send(
        {"session_id": "22bdef25-0d1f-4eb6-a55d-caa2cc78c04c"},
        "ping",
        "mid",
        {"tool": "claude", "handle": "own-ses-1"},
    )
    assert called == {}
    assert result["status"] == "unavailable"
    assert "not listening" in result["reason"]
    assert "message-file" not in result["reason"]
