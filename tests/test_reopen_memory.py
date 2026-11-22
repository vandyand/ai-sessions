import json
import threading
import time
from unittest.mock import patch

import pytest

from ai_sessions import app, detached
from ai_sessions.model import Session


@pytest.fixture(autouse=True)
def isolated_workspace_registration():
    with patch("ai_sessions.workspaces.launch.remember_launch"):
        yield


def item(sid):
    return Session("codex", sid, sid, "C:/project", 1, 1, "", False, "rollout")


@pytest.mark.parametrize("operation", ["remember", "close", "restart"])
def test_concurrent_terminal_updates_are_serialized(tmp_path, operation):
    path = tmp_path / "terminals.json"
    detached.remember(item("first"), "codex", 101, 10, "one", path=path)
    first_writing, release_first, second_done = (threading.Event() for _ in range(3))
    write = detached._write
    failures = []

    def paused_write(target, document):
        if threading.current_thread().name == "first":
            first_writing.set()
            assert release_first.wait(3)
        write(target, document)

    def first():
        try:
            if operation == "close":
                detached.mark_closed(101, path=path)
            elif operation == "restart":
                detached.request_restart("one", "claude", path=path)
            else:
                detached.remember(item("other"), "codex", 102, 11, "two", path=path)
        except BaseException as error:
            failures.append(error)

    def second():
        try:
            detached.remember(item("third"), "codex", 103, 12, "three", path=path)
        except BaseException as error:
            failures.append(error)
        finally:
            second_done.set()

    with patch.object(detached, "_write", paused_write):
        a = threading.Thread(target=first, name="first")
        b = threading.Thread(target=second, name="second")
        a.start()
        assert first_writing.wait(3)
        b.start()
        serialized = not second_done.wait(0.1)
        release_first.set()
        a.join(4)
        b.join(4)
    assert not failures
    assert not a.is_alive() and not b.is_alive()
    assert serialized
    rows = detached.remembered(path=path)
    assert rows["three"]["session_id"] == "third"
    if operation == "close":
        assert rows["one"]["closed"]
    elif operation == "restart":
        assert rows["one"]["restart_tool"] == "claude"
    else:
        assert rows["two"]["session_id"] == "other"


def test_exact_terminal_journal_survives_old_writer_overwriting_shared_index(tmp_path):
    path = tmp_path / "terminals.json"
    detached.remember(item("mine"), "codex", 101, 10, "my-terminal", path=path)
    old = path.read_bytes()
    detached.mark_closed(101, path=path)
    history = detached._history_path(path, "my-terminal")
    authoritative = history.read_bytes()
    path.write_bytes(old)  # An older, still-running launcher has no new lock/journal.
    assert detached.closed(path=path)["my-terminal"]["session_id"] == "mine"
    detached.remember(item("other"), "codex", 102, 11, "other-terminal", path=path)
    assert history.read_bytes() == authoritative
    assert detached.closed(path=path)["my-terminal"]["session_id"] == "mine"
    path.write_text("{}", encoding="utf-8")
    assert detached.closed(path=path)["my-terminal"]["session_id"] == "mine"


def test_expired_terminal_history_is_not_recovered_and_is_pruned(tmp_path):
    path = tmp_path / "terminals.json"
    now = time.time()
    with patch.object(detached.time, "time", return_value=now - 8 * 86400):
        detached.remember(item("old"), "codex", 101, 10, "old-terminal", path=path)
        detached.mark_closed(101, path=path)
    assert detached.closed(path=path) == {}
    detached.remember(item("new"), "codex", 102, 11, "new-terminal", path=path)
    assert not detached._history_path(path, "old-terminal").exists()


@pytest.mark.parametrize("terminal", [None, 1, [], "another-terminal"])
def test_malformed_or_misbound_terminal_history_is_ignored(tmp_path, terminal):
    path = tmp_path / "terminals.json"
    detached.remember(item("mine"), "codex", 101, 10, "my-terminal", path=path)
    history = detached._history_path(path, "my-terminal")
    document = json.loads(history.read_text(encoding="utf-8"))
    document["terminal"] = terminal
    history.write_text(json.dumps(document), encoding="utf-8")
    path.write_text("{}", encoding="utf-8")
    assert detached.remembered(path=path) == {}


def test_terminal_memory_rejects_a_different_native_home():
    row = {"tool": "codex", "session_id": "mine", "home": "C:/different-home"}
    assert app.remembered_session(row, [item("mine")]) is None


def test_unknown_terminal_does_not_silently_reopen_another_terminal(capsys):
    other = item("other")
    with (
        patch.object(detached, "terminal_id", return_value="unknown"),
        patch.object(detached, "closed", return_value={"elsewhere": {"session_id": "other"}}),
        patch.object(detached, "interrupted", return_value={}),
        patch.object(detached, "remembered", return_value={}),
        patch("builtins.input", side_effect=EOFError),
    ):
        assert app.choose_reopen([other]) is None
    assert "Recently closed" in capsys.readouterr().out


def test_unknown_terminal_without_history_explains_managed_launch(capsys):
    with (
        patch.object(detached, "terminal_id", return_value="unknown"),
        patch.object(detached, "closed", return_value={}),
        patch.object(detached, "interrupted", return_value={}),
        patch.object(detached, "remembered", return_value={}),
    ):
        assert app.choose_reopen([]) is None
    assert "sessions new codex" in capsys.readouterr().err
