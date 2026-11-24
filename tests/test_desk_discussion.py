import json

import pytest

from ai_sessions.desk import discussion
from ai_sessions.desk.store import ConflictError, Store


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path / "desk")
    store.create(
        "fixture", [dict(tool="claude", session_id="source", storage="source.jsonl")], str(tmp_path)
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


def test_saved_comment_keeps_exact_card_and_clears_only_its_draft(desk):
    snap = desk.get("fixture")
    version = discussion.card_version(snap, "ask")
    discussion.save_draft(desk, "fixture", "ask", "Why option A?", version)
    entry = discussion.submit(desk, "fixture", "ask", "clarification", "Why option A?", version)
    snap = desk.get("fixture")
    assert snap["decision_drafts"].get("ask") is None
    assert snap["decision_discussion"]["ask"][-1]["text"] == "Why option A?"
    assert entry["card"]["decision"]["question"] == "Which option?"
    assert entry["status"] == "queued"
    assert snap["decisions"][0]["status"] == "pending"


def test_changed_card_refuses_send_and_keeps_draft(desk):
    snap = desk.get("fixture")
    version = discussion.card_version(snap, "ask")
    discussion.save_draft(desk, "fixture", "ask", "My answer", version)
    core = dict(snap["decisions"][0], context="Changed proposal")
    desk.publish("fixture", snap["revision"], {}, "New review", [core])
    with pytest.raises(ConflictError):
        discussion.submit(desk, "fixture", "ask", "decision", "My answer", version)
    assert desk.get("fixture")["decision_drafts"]["ask"]["text"] == "My answer"


def test_notes_are_not_jobs_or_consent(desk):
    version = discussion.card_version(desk.get("fixture"), "ask")
    row = discussion.submit(desk, "fixture", "ask", "note", "Not decided yet", version)
    assert row["status"] == "saved"
    assert discussion.claim(desk, "fixture") is None
    assert desk.get("fixture")["decisions"][0]["status"] == "pending"


def test_durable_single_claim_and_rejected_stale_result(desk):
    version = discussion.card_version(desk.get("fixture"), "ask")
    row = discussion.submit(desk, "fixture", "ask", "clarification", "Explain", version)
    claimed = discussion.claim(desk, "fixture")
    assert claimed["id"] == row["id"]
    assert discussion.claim(desk, "fixture") is None
    snap = desk.get("fixture")
    desk.publish(
        "fixture", snap["revision"], {}, "Review", [dict(snap["decisions"][0], context="Updated")]
    )
    discussion.finish(desk, "fixture", claimed, reply="Explanation", details=None)
    entry = desk.get("fixture")["decision_discussion"]["ask"][-1]
    assert entry["status"] == "outdated"
    assert entry["reply"] == "Explanation"


def test_discussion_export_is_stable_and_contains_original(desk):
    v = discussion.card_version(desk.get("fixture"), "ask")
    row = discussion.submit(desk, "fixture", "ask", "decision", "Use B, only for staging.", v)
    path = discussion.export(desk, "fixture", row["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"][0]["text"] == "Use B, only for staging."
    assert data["entries"][0]["card"]["decision"]["question"] == "Which option?"


def test_every_changed_card_is_retained_even_without_comments(desk):
    snap = desk.get("fixture")
    desk.publish(
        "fixture",
        snap["revision"],
        {},
        "Updated",
        [dict(snap["decisions"][0], context="New background")],
    )
    versions = discussion.versions(desk, "fixture", "ask")
    assert [v["card"]["decision"]["context"] for v in versions] == ["Background", "New background"]


def test_projection_failure_after_commit_does_not_repeat_submission(desk, monkeypatch):
    def unavailable(*args):
        raise PermissionError("fixture")

    monkeypatch.setattr(discussion, "export", unavailable)
    v = discussion.card_version(desk.get("fixture"), "ask")
    row = discussion.submit(desk, "fixture", "ask", "note", "Saved once", v)
    assert row["status"] == "saved"
    assert len(desk.get("fixture")["decision_discussion"]["ask"]) == 1


def test_old_claim_failure_cannot_overwrite_a_new_attempt(desk):
    v = discussion.card_version(desk.get("fixture"), "ask")
    discussion.submit(desk, "fixture", "ask", "clarification", "Explain", v)
    old = discussion.claim(desk, "fixture")
    discussion.fail_claim(desk, "fixture", old)
    discussion.retry(desk, "fixture", old["id"])
    current = discussion.claim(desk, "fixture")
    discussion.fail_claim(desk, "fixture", old)
    assert desk.get("fixture")["decision_discussion"]["ask"][-1]["token"] == current["token"]
    assert desk.get("fixture")["decision_discussion"]["ask"][-1]["status"] == "working"


def test_retry_cannot_overlap_a_new_question(desk):
    v = discussion.card_version(desk.get("fixture"), "ask")
    discussion.submit(desk, "fixture", "ask", "clarification", "First", v)
    first = discussion.claim(desk, "fixture")
    discussion.fail_claim(desk, "fixture", first)
    discussion.submit(desk, "fixture", "ask", "clarification", "Second", v)
    with pytest.raises(ConflictError, match="already in progress"):
        discussion.retry(desk, "fixture", first["id"])


def test_no_recipient_is_frozen_by_the_first_explicit_desk_binding(desk):
    v = discussion.card_version(desk.get("fixture"), "ask")
    row = discussion.submit(desk, "fixture", "ask", "decision", "Use option B", v)
    assert row.get("recipient") is None
    desk.bind_agent("fixture", "claude", "desk-conversation", "exact-home")
    row = desk.get("fixture")["decision_discussion"]["ask"][-1]
    assert row["recipient"] == dict(
        tool="claude", session_id="desk-conversation", home="exact-home"
    )
    desk.bind_agent("fixture", "codex", "other-conversation", "other-home")
    assert desk.get("fixture")["decision_discussion"]["ask"][-1]["recipient"] == row["recipient"]


def test_dismissed_request_cannot_be_reopened_by_model_publication(desk):
    snap = desk.get("fixture")
    desk.dismiss_decision("fixture", "ask", snap["revision"], "This is team coordination")
    current = desk.get("fixture")
    desk.publish("fixture", current["revision"], {}, "Review", snap["decisions"])
    assert desk.get("fixture")["decisions"][0]["status"] == "superseded"
