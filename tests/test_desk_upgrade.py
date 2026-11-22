import json
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

from ai_sessions import __version__
from ai_sessions.desk import runtime
from ai_sessions.desk.store import Store


def make_store(path):
    store = Store(path)
    store.create("desk", [{"tool": "claude", "session_id": "ops", "storage": "log"}], str(path))
    return store


def test_open_replaces_verified_observer_from_an_older_package(tmp_path):
    store = make_store(tmp_path)
    old = dict(state="running", pid=123, generation="old")
    new = dict(state="running", pid=456, generation="new", version=__version__)
    with (
        patch.object(runtime, "status", side_effect=[old, new]),
        patch.object(
            runtime, "_stop_current", return_value={"state": "stopped"}, create=True
        ) as stop,
        patch.object(runtime.subprocess, "Popen", return_value=Mock()) as spawn,
    ):
        assert runtime.ensure_observer(store, "desk") == new
        stop.assert_called_once_with(store, "desk", old)
        spawn.assert_called_once()


def test_upgrade_does_not_start_duplicate_when_old_worker_is_still_stopping(tmp_path):
    store = make_store(tmp_path)
    old = dict(state="running", pid=123, generation="old", version="old-version")
    with (
        patch.object(runtime, "status", return_value=old),
        patch.object(runtime, "_stop_current", return_value={"state": "stopping"}, create=True),
        patch.object(runtime.subprocess, "Popen") as spawn,
    ):
        with pytest.raises(RuntimeError, match="stopping"):
            runtime.ensure_observer(store, "desk")
        spawn.assert_not_called()


def test_transient_status_sharing_failure_does_not_claim_live_observer_stopped(tmp_path):
    store = make_store(tmp_path)
    row = dict(
        root=str(tmp_path.resolve()),
        name="desk",
        state="running",
        busy=False,
        pid=123,
        started=456,
        generation="owned",
        version=__version__,
    )
    process = Mock()
    process.create_time.return_value = 456
    process.is_running.return_value = True
    with (
        patch("pathlib.Path.read_text", side_effect=[PermissionError("sharing"), json.dumps(row)]),
        patch.object(runtime.psutil, "Process", return_value=process),
        patch.object(runtime, "command_matches", return_value=True),
        patch.object(runtime, "claim", return_value=nullcontext(False)),
    ):
        assert runtime.status(store, "desk")["state"] == "running"
