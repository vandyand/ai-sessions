"""Desk startup tests use temporary stores and never invoke a model."""

from unittest.mock import patch

import pytest

from ai_sessions.desk import cli, runtime
from ai_sessions.desk.store import Store
from ai_sessions.model import Session


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("entry", ["conversation", "view"])
def test_either_entry_initializes_and_starts_observer(tmp_path, tool, entry):
    store = Store(tmp_path)
    item = Session(tool, "ops", "Supervisor", str(tmp_path), 0, 0, "", True, "ops.jsonl")
    item.handle = "ops-handle"
    with (
        patch("ai_sessions.desk.cli.Store", return_value=store),
        patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
        patch("ai_sessions.references.resolve_session", return_value=item),
        patch("ai_sessions.desk.cli.ensure_observer", create=True) as ensure,
        patch("ai_sessions.desk.launch.open_agent", return_value=0),
        patch("ai_sessions.desk.view.run"),
    ):
        assert cli.main([item.handle] if entry == "conversation" else ["view", item.handle]) == 0
        assert store.get(item.handle)["sources"][0]["tool"] == tool
        ensure.assert_called_once()


@pytest.mark.parametrize("changed", ["--name", "--generation", "-m", "--root"])
def test_command_identity_normalizes_only_root(tmp_path, changed):
    row = dict(root=str(tmp_path), name="Exact Desk", generation="generation")
    command = [
        "python",
        "-m",
        "ai_sessions.desk.runtime",
        "--root",
        str(tmp_path).replace("\\", "\\\\"),
        "--name",
        row["name"],
        "--generation",
        row["generation"],
        "--interval",
        "60",
    ]
    assert runtime.command_matches(command, row)
    command[command.index(changed) + 1] = "different"
    assert not runtime.command_matches(command, row)


def test_os_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "worker.lock"
    with runtime.claim(lock) as first:
        assert first
        with runtime.claim(lock) as second:
            assert not second
    with runtime.claim(lock) as third:
        assert third


def test_clean_environment_keeps_homes_but_drops_parent_identity(monkeypatch):
    for key in (
        "CODEX_THREAD_ID",
        "CLAUDE_SESSION_ID",
        "AI_SESSIONS_MESSAGE_FROM",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    ):
        monkeypatch.setenv(key, "fiction")
    monkeypatch.setenv("CODEX_HOME", "codex-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "claude-home")
    env = runtime.clean_environment()
    assert env["CODEX_HOME"] == "codex-home"
    assert env["CLAUDE_CONFIG_DIR"] == "claude-home"
    assert "CODEX_THREAD_ID" not in env
    assert "CLAUDE_SESSION_ID" not in env
    assert "AI_SESSIONS_MESSAGE_FROM" not in env
    assert "CLAUDE_CODE_SESSION_ACCESS_TOKEN" not in env


def test_existing_worker_attaches_without_spawning(tmp_path):
    store = Store(tmp_path)
    store.create("desk", [{"tool": "codex", "session_id": "ops", "storage": "log"}], str(tmp_path))
    with (
        patch.object(runtime, "status", return_value={"state": "running", "pid": 123}),
        patch.object(runtime.subprocess, "Popen") as spawn,
    ):
        assert runtime.ensure_observer(store, "desk")["pid"] == 123
        spawn.assert_not_called()


def test_unwatched_view_never_creates_record_or_starts_worker(tmp_path):
    store = Store(tmp_path)
    item = Session("codex", "ops", "Supervisor", str(tmp_path), 0, 0, "", True, "log")
    with (
        patch("ai_sessions.desk.cli.Store", return_value=store),
        patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
        patch("ai_sessions.references.resolve_session", return_value=item),
        patch.object(cli, "ensure_observer") as ensure,
    ):
        assert cli.main(["view", "ops", "--no-watch"]) == 2
        assert store.list() == []
        ensure.assert_not_called()


def test_stop_is_scoped_to_verified_generation(tmp_path):
    store = Store(tmp_path)
    store.create("desk", [{"tool": "claude", "session_id": "ops", "storage": "log"}], str(tmp_path))
    with patch.object(
        runtime,
        "status",
        side_effect=[{"state": "running", "generation": "owned-generation"}, {"state": "stopped"}],
    ):
        assert runtime.stop_observer(store, "desk")["state"] == "stopped"
    import json

    assert json.loads((runtime.folder(store, "desk") / "stop.json").read_text()) == {
        "generation": "owned-generation"
    }


def test_worker_stop_reaches_owned_watch_and_busy_state(tmp_path):
    store = Store(tmp_path)
    store.create("desk", [{"tool": "codex", "session_id": "ops", "storage": "log"}], str(tmp_path))
    location = runtime.folder(store, "desk")

    def watcher(store, name, interval, stop, activity):
        import json

        activity["busy"] = True
        assert json.loads((location / "status.json").read_text())["busy"]
        runtime.write(location / "stop.json", {"generation": "old"})
        assert not stop.is_set()
        runtime.write(location / "stop.json", {"generation": "owned"})
        assert stop.wait(10)
        activity["busy"] = False

    with patch.object(cli, "watch", side_effect=watcher) as watch:
        runtime.serve(store, "desk", "owned", 60)
        watch.assert_called_once()
    with runtime.claim(location / "worker.lock") as released:
        assert released


def test_competing_process_starts_share_one_worker(tmp_path):
    """Real processes, isolated empty source: observation must never need a model."""
    import json
    import subprocess
    import sys

    import psutil

    store = Store(tmp_path)
    source = tmp_path / "empty.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": str(source)}], str(tmp_path)
    )
    code = (
        "import sys,json; from ai_sessions.desk.store import Store; "
        "from ai_sessions.desk.runtime import ensure_observer; "
        "print(json.dumps(ensure_observer(Store(sys.argv[1]), 'desk', 5)))"
    )
    children = []
    worker = None
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(tmp_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=runtime.clean_environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
        rows = []
        for child in children:
            output, error = child.communicate(timeout=25)
            assert child.returncode == 0, error.decode()
            rows.append(json.loads(output))
        assert rows[0]["pid"] == rows[1]["pid"]
        assert rows[0]["generation"] == rows[1]["generation"]
        worker = psutil.Process(rows[0]["pid"])
        assert store.get("desk")["observation"].get("synthesized_at") is None
        assert runtime.status(store, "desk")["state"] == "running"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        saved = runtime.status(store, "desk")
        if worker is None and saved.get("pid"):
            worker = psutil.Process(saved["pid"])
        runtime.stop_observer(store, "desk")
        if worker:
            try:
                worker.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("width", [20, 40, 60, 100])
def test_detached_status_fits_narrow_headers(width):
    from ai_sessions.desk import view

    data = {"name": "Desk", "observed_at": None, "decisions": [], "_freshness": "changed"}
    frame = view.render(data, view.State(), width, 20, updating=True)
    assert "Updating briefing" in frame.lines[0]
    assert all(view.cell_width(line) <= width for line in frame.lines)
    frame = view.render(data, view.State(), width, 20, read_error="Observer stopped; reopen")
    assert "Observer stopped" in frame.lines[0]
    assert all(view.cell_width(line) <= width for line in frame.lines)
