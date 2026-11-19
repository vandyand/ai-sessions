"""Native-deletion dispatch contracts; all storage and RPC responses fictional."""

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_deletion import IDENT, OTHER, confirm, session, transcript
from test_deletion import isolated as isolated
from test_deletion import native as native
from test_deletion_provider_schemas import codex_store as codex_store

from ai_sessions import deletion
from ai_sessions.harnesses import deletion_codex_native as native_rpc
from ai_sessions.registry import REGISTRY


def test_paginated_plan_uses_native_deletion_without_starting_a_process(codex_store):
    _, adapter = codex_store
    ref = adapter.resolve(IDENT)
    path = Path(ref.storage)
    lines = path.read_text().splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["history_mode"] = "paginated"
    path.write_text(json.dumps(meta) + "\n" + "\n".join(lines[1:]) + "\n")
    plan = adapter.plan_delete(ref)
    assert plan.native_delete
    assert not plan.databases
    assert path.exists()
    assert "thread/delete" in " ".join(plan.notes)
    with pytest.raises(deletion.DeletionError, match="native"):
        deletion.commit_plan(plan)


def test_generic_combiner_cannot_turn_native_guards_into_manual_deletion(tmp_path):
    path = tmp_path / "history"
    path.write_text("fiction")
    plan = deletion.DeletePlan(
        "fixture",
        deletion.NativeRef(IDENT, str(path)),
        None,
        (deletion.FileChange.capture(tmp_path, path),),
        native_delete=True,
    )
    with pytest.raises(deletion.DeletionError, match="native"):
        deletion.combine_plans([plan])
    assert path.exists()


def test_legacy_rollout_in_modern_home_delegates_ancillary_cleanup(codex_store):
    home, adapter = codex_store
    (home / "thread_history_1.sqlite").touch()
    assert adapter.plan_delete(adapter.resolve(IDENT)).native_delete


def test_provider_writer_lock_keeps_running_target_precheck(native, monkeypatch):
    item = session(native[1])
    monkeypatch.setattr(
        deletion,
        "deletion_snapshot",
        lambda: pytest.fail("native writer ownership is checked by the provider"),
    )
    deletion.assert_idle([item], native_locked={item.key})
    item.is_open = True
    with pytest.raises(deletion.DeletionError, match="running"):
        deletion.assert_idle([item], native_locked={item.key})


class Input(io.StringIO):
    def close(self):
        pass


class Process:
    def __init__(self, responses):
        self.stdin = Input()
        self.stdout = io.StringIO("".join(json.dumps(row) + "\n" for row in responses))
        self.stopped = False

    def wait(self, timeout):
        return 0

    def terminate(self):
        self.stopped = True


def test_owned_native_rpc_only_initializes_and_deletes_exact_identity(tmp_path, monkeypatch):
    process = Process([{"id": 1, "result": {}}, {"id": 2, "result": {}}])
    calls = []

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(native_rpc, "native_executable", lambda: "fictional-native.exe")
    monkeypatch.setattr(native_rpc.subprocess, "Popen", start)
    native_rpc._delete(tmp_path, IDENT)
    requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert [row["method"] for row in requests] == ["initialize", "initialized", "thread/delete"]
    assert requests[-1] == {"id": 2, "method": "thread/delete", "params": {"threadId": IDENT}}
    assert calls[0][0] == ["fictional-native.exe", "app-server", "--listen", "stdio://"]
    assert calls[0][1]["env"]["CODEX_HOME"] == str(tmp_path)


@pytest.mark.parametrize(
    "reply", [{"id": 2, "error": {"message": "fixture failure"}}, {"id": 2, "result": None}, None]
)
def test_any_post_submission_failure_is_uncertain_and_never_retried(tmp_path, monkeypatch, reply):
    responses = [{"id": 1, "result": {}}] + ([reply] if reply else [])
    process = Process(responses)
    monkeypatch.setattr(native_rpc, "native_executable", lambda: "fixture")
    monkeypatch.setattr(native_rpc.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(
        deletion.DeletionError, match="PARTIAL/UNCERTAIN.*do not automatically retry"
    ):
        native_rpc._delete(tmp_path, IDENT)
    assert process.stdin.getvalue().count('"thread/delete"') == 1


def test_initialize_failure_never_submits_deletion(tmp_path, monkeypatch):
    process = Process([{"id": 1, "error": {"message": "unsupported fixture"}}])
    monkeypatch.setattr(native_rpc, "native_executable", lambda: "fixture")
    monkeypatch.setattr(native_rpc.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(deletion.DeletionError, match="not submitted"):
        native_rpc._delete(tmp_path, IDENT)
    assert '"thread/delete"' not in process.stdin.getvalue()


@pytest.mark.parametrize("failure", [False, True])
def test_mixed_all_copies_composes_manual_history_and_reports_native_partial(
    native, tmp_path, monkeypatch, capsys, failure
):
    from ai_sessions.app import UserState

    home, first_path, second_path, adapter = native
    third_id = "12345678-1234-1234-8234-123456789fed"
    third_path = transcript(home, third_id)
    items = [session(first_path), session(second_path, OTHER), session(third_path, third_id)]
    state = UserState(tmp_path / "state.json")
    state.handles.ensure(items)
    for item in items[1:]:
        state.handles.link(items[0], item)
    history = home / "history.jsonl"
    history.write_text(
        "".join(
            json.dumps({"sessionId": ident}) + "\n" for ident in (IDENT, OTHER, third_id, "keep")
        )
    )
    original_plan = adapter.plan_delete

    def plan(ref):
        value = original_plan(ref)
        if ref.session_id == third_id:
            return replace(
                value, native_delete=True, files=(deletion.FileChange.capture(home, third_path),)
            )
        return value

    def execute(value):
        assert not first_path.exists() and not second_path.exists()
        if failure:
            raise deletion.DeletionError("PARTIAL/UNCERTAIN native fixture failure")
        third_path.unlink()
        history.write_text(json.dumps({"sessionId": "keep"}) + "\n")

    monkeypatch.setattr(deletion, "catalog", lambda: (items, state))
    confirm(monkeypatch, items[0].handle)
    with REGISTRY.temporary(replace(adapter, plan_delete=plan, execute_delete=execute)):
        assert deletion.main([items[0].handle, "--all-copies"]) == int(failure)
    assert not first_path.exists() and not second_path.exists()
    assert third_path.exists() == failure
    output = capsys.readouterr()
    if failure:
        assert "PARTIAL: deleted" in output.err and "Unconfirmed native outcome:" in output.err
        assert third_id in output.err
    else:
        assert json.loads(history.read_text()) == {"sessionId": "keep"}
        assert "Permanently deleted 3" in output.out
