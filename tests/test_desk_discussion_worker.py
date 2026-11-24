import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_sessions.desk import discussion
from ai_sessions.desk import discussion_worker as worker
from ai_sessions.desk.read_context import Reader
from ai_sessions.desk.store import Store


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path / "desk")
    source = tmp_path / "source.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    store.create(
        "fixture", [dict(tool="claude", session_id="source", storage=str(source))], str(tmp_path)
    )
    core = dict(
        key="ask",
        question="Which option?",
        context="Background",
        recommendation="",
        status="pending",
        evidence=["s0"],
    )
    store.publish("fixture", 0, {}, "Review", [core])
    return store


def submit(store, kind="clarification"):
    return discussion.submit(
        store,
        "fixture",
        "ask",
        kind,
        "Please explain the difference",
        discussion.card_version(store.get("fixture"), "ask"),
    )


def test_project_tools_confine_reads_and_exclude_private_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("feature_flag = True\n", encoding="utf-8")
    (root / ".env").write_text("private-value", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    reader = Reader(root)
    assert reader.search("feature_flag")["matches"] == [dict(path="main.py", line=1)]
    assert reader.read("main.py")["id"] == "code1"
    for path in (".env", "../outside.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(ValueError):
            reader.read(path)
    try:
        (root / "escape").symlink_to(tmp_path / "outside.txt")
    except OSError:
        return
    with pytest.raises(ValueError):
        reader.read("escape")


def test_one_off_reply_is_saved_without_changing_status(desk):
    submit(desk)

    def runner(prompt, schema, config, audit):
        assert "Which option?" in prompt
        assert config["mcpServers"]["desk_context"]["args"][-4] == "--root"
        return json.dumps(
            dict(reply="A is faster; B is cheaper.", updated_details=None, question_for_agent=None)
        )

    worker.process_once(desk, "fixture", runner=runner)
    snap = desk.get("fixture")
    assert snap["decision_discussion"]["ask"][-1]["status"] == "complete"
    assert snap["decisions"][0]["status"] == "pending"


def test_offline_then_uncertain_delivery_is_never_retried(desk, monkeypatch):
    desk.bind_agent("fixture", "claude", "desk-agent", "exact-home")
    submit(desk, "decision")
    target = dict(tool="claude", session_id="desk-agent", home="exact-home")
    monkeypatch.setattr(worker, "_targets", lambda _: [target])
    receivers = []
    monkeypatch.setattr(worker.messaging, "reachable", lambda **_: receivers)
    calls = []

    def deliver(*args):
        calls.append(args)
        raise OSError("ambiguous transport failure")

    monkeypatch.setattr(worker.messaging, "deliver", deliver)
    worker.deliver_pending(desk, "fixture")
    assert desk.get("fixture")["decision_discussion"]["ask"][-1]["delivery"] == "queued"
    receivers.append(dict(target, generation="live"))
    worker.deliver_pending(desk, "fixture")
    worker.deliver_pending(desk, "fixture")
    assert len(calls) == 1
    assert "Exact human answer:" in calls[0][1]
    assert "discussion-" in calls[0][1]
    assert desk.get("fixture")["decisions"][0]["status"] == "pending"


def test_missing_context_waits_and_exact_desk_reply_resumes(desk, monkeypatch):
    desk.bind_agent("fixture", "claude", "desk-agent", "exact-home")
    row = submit(desk)

    def runner(*args):
        return json.dumps(
            dict(
                reply="The code does not record the prior agreement.",
                updated_details=None,
                question_for_agent="What did the human previously agree?",
            )
        )

    target = dict(tool="claude", session_id="desk-agent", home="exact-home")
    monkeypatch.setattr(worker, "_targets", lambda _: [target])
    monkeypatch.setattr(
        worker.messaging, "reachable", lambda **_: [dict(target, generation="live")]
    )
    monkeypatch.setattr(worker.messaging, "deliver", lambda *args: {"status": "submitted"})
    worker.process_once(desk, "fixture", runner=runner)
    with pytest.raises(ValueError):
        discussion.agent_reply(
            desk, "fixture", row["id"], "Some answer", dict(target, home="foreign")
        )
    discussion.agent_reply(desk, "fixture", row["id"], "The agreement was staging only.", target)
    worker.process_once(
        desk,
        "fixture",
        runner=lambda *args: json.dumps(
            dict(
                reply="The agreement covers staging only.",
                updated_details=None,
                question_for_agent=None,
            )
        ),
    )
    saved = desk.get("fixture")["decision_discussion"]["ask"][-1]
    assert saved["status"] == "complete"
    assert saved["agent_reply"] == "The agreement was staging only."
    assert len(saved["responses"]) == 2
    assert saved["responses"][0]["reply"] == "The code does not record the prior agreement."


def test_invalid_model_output_preserves_question_for_retry(desk):
    submit(desk)
    worker.process_once(desk, "fixture", runner=lambda *args: '{"reply":"bad shape"}')
    saved = desk.get("fixture")["decision_discussion"]["ask"][-1]
    assert saved["status"] == "failed"
    assert saved["text"] == "Please explain the difference"
    discussion.retry(desk, "fixture", saved["id"])
    assert discussion.claim(desk, "fixture")["id"] == saved["id"]


def test_stdio_read_server_roundtrip_is_windowless_and_read_only(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "example.py").write_text("VALUE = 42\n", encoding="utf-8")
    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("pythonw.exe")
    requests = [
        dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2024-11-05")),
        dict(
            jsonrpc="2.0",
            id=2,
            method="tools/call",
            params=dict(name="read_project_file", arguments=dict(path="example.py")),
        ),
    ]
    env = dict(
        os.environ, PYTHONPATH=str(Path(worker.__file__).resolve().parents[2]), PYTHONUTF8="1"
    )
    result = subprocess.run(
        [
            str(executable),
            "-m",
            "ai_sessions.desk.read_context",
            "--root",
            str(root),
            "--audit",
            str(tmp_path / "audit.json"),
        ],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    entry = json.loads(replies[1]["result"]["content"][0]["text"])
    assert entry["text"] == "VALUE = 42"
    assert entry["id"] == "code1"


def test_reply_cli_ensures_worker_after_saving(desk, monkeypatch):
    from ai_sessions import handle_lookup
    from ai_sessions.desk import cli
    from ai_sessions.references import native_key

    home = native_key("claude", "desk-agent").home
    desk.bind_agent("fixture", "claude", "desk-agent", home)
    row = submit(desk)
    discussion.update(desk, "fixture", row["id"], status="waiting")
    monkeypatch.setattr(handle_lookup, "current_identity", lambda: ("claude", "desk-agent"))
    monkeypatch.setattr(cli, "Store", lambda: desk)
    monkeypatch.setattr(cli, "resolve_desk_name", lambda *args, **kwargs: "fixture")
    started = []
    monkeypatch.setattr(cli, "ensure_observer", lambda store, name: started.append(name))
    assert cli.main(["reply", "fixture", row["id"], "--message", "Staging only"]) == 0
    assert started == ["fixture"]
    assert desk.get("fixture")["decision_discussion"]["ask"][-1]["agent_reply"] == "Staging only"
