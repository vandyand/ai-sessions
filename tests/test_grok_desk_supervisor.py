"""Desk projection, staleness, launcher CLI, and supervisor hook file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_sessions.desk.grok_projection import check, sync
from ai_sessions.harnesses.desk_observer import read_since
from ai_sessions.model import NativeRef


def _stream(path: Path, text: str, *, done: bool) -> None:
    line = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
            }
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
        if done:
            handle.write(
                json.dumps(
                    {
                        "method": "session/update",
                        "params": {"update": {"sessionUpdate": "turn_completed"}},
                    }
                )
                + "\n"
            )


def test_projection_and_staleness(tmp_path: Path):
    source = tmp_path / "updates.jsonl"
    _stream(source, "Remember this codeword: LARKSPUR.", done=True)
    thought = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "QUILL"},
            }
        },
    }
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(thought) + "\n")
    # thought before turn_completed would be inside the turn; rewrite cleanly
    source.write_text("", encoding="utf-8")
    user = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "Remember this codeword: LARKSPUR."},
            }
        },
    }
    assistant = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "I will remember the codeword LARKSPUR."},
            }
        },
    }
    thought = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "QUILL"},
            }
        },
    }
    done = {"method": "session/update", "params": {"update": {"sessionUpdate": "turn_completed"}}}
    source.write_text(
        "\n".join(json.dumps(row) for row in (user, thought, assistant, done)) + "\n",
        encoding="utf-8",
    )
    assert sync(source) == 1
    body = (tmp_path / "updates.jsonl.desk.jsonl").read_text(encoding="utf-8")
    assert "LARKSPUR" in body and "QUILL" not in body
    code, text = check(source)
    assert code == 0 and text.startswith("in step:")
    code, text = check(source)
    assert code == 0 and text.startswith("in step:")
    extra = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "second"},
            }
        },
    }
    before = source.stat().st_size
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra) + "\n")
    code, text = check(source)
    assert code == 2 and "stale:" in text and str(before) in text
    sync(source)
    code, text = check(source)
    assert code == 0
    assert (
        body.count('"type": "turn"') == 1
        or (tmp_path / "updates.jsonl.desk.jsonl").read_text(encoding="utf-8").count("\n") == 1
    )
    snapshot = read_since(
        "grok", NativeRef("s", str(tmp_path / "updates.jsonl.desk.jsonl")), 0, end=10_000
    )
    joined = " ".join(turn.text for turn in snapshot.transcript.turns)
    assert "LARKSPUR" in joined
    with pytest.raises(ValueError):
        read_since("grok", NativeRef("s", str(source)), 0, end=10)


def test_truncated(tmp_path: Path):
    source = tmp_path / "updates.jsonl"
    source.write_text("x\n", encoding="utf-8")
    state = tmp_path / "updates.jsonl.desk.offset.json"
    state.write_text(json.dumps({"offset": 50}), encoding="utf-8")
    code, text = check(source)
    assert code == 2 and text.startswith("truncated:")


def test_supervisor_and_desk_dry_run(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    env["CODEX_HOME"] = str(tmp_path / "codex")
    env["GROK_HOME"] = str(tmp_path / "grok")
    env["AI_SESSIONS_CACHE_DIR"] = str(tmp_path / "cache")
    (tmp_path / "claude").mkdir()
    (tmp_path / "codex").mkdir()
    (tmp_path / "claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "codex" / "hooks.json").write_text("{}\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "ai_sessions", "--install-hooks"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    hook = tmp_path / "grok" / "hooks" / "sessions-supervisor.json"
    assert completed.returncode == 0, completed.stderr
    data = json.loads(hook.read_text(encoding="utf-8"))
    events = set(data["hooks"])
    assert events == {"SessionStart", "UserPromptSubmit"}
    commands = [
        item["command"]
        for group in data["hooks"].values()
        for entry in group
        for item in entry["hooks"]
    ]
    assert any(command.endswith("session-start") for command in commands)
    assert any(command.endswith("user-prompt-submit") for command in commands)
    assert all(" grok" not in command.split("-m ai_sessions_hook", 1)[-1] for command in commands)
    removed = subprocess.run(
        [sys.executable, "-m", "ai_sessions", "--uninstall-hooks"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert removed.returncode == 0
    assert not hook.exists()
    claude_left = json.loads((tmp_path / "claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Stop" not in json.dumps(claude_left)
