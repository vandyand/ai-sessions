import json
import time

import pytest

from ai_sessions.desk import observer
from ai_sessions.desk.store import ConflictError, Store


def item(number, **changes):
    return dict(
        key=f"q{number}",
        question=f"Choose option {number}?",
        context="Context " * 100,
        recommendation="Discuss the options.",
        status="pending",
        evidence=["original"],
        **changes,
    )


def test_large_backlog_is_preserved_and_prompt_is_a_rotating_working_set(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk",
        [{"tool": "claude", "session_id": "fixture", "storage": "fixture.jsonl"}],
        str(tmp_path),
    )
    records = [item(n) for n in range(200)]
    current = store.publish("desk", 0, {}, "Current progress", records)
    assert current["decisions"] == records
    coverage = [
        dict(
            id="s0",
            tool="claude",
            session_id="fixture",
            storage="fixture.jsonl",
            status="available",
            changed=True,
        )
    ]
    evidence = [dict(citation="s0", text="Fresh source activity")]
    first = json.loads(observer._build_prompt(current, coverage, evidence).split("\n", 1)[1])
    assert 0 < len(first["previous_decisions"]) <= 20
    assert len(json.dumps(first["previous_decisions"], ensure_ascii=False)) < 26000
    current["observation"]["decision_window_offset"] = 20
    second = json.loads(observer._build_prompt(current, coverage, evidence).split("\n", 1)[1])
    assert {d["key"] for d in first["previous_decisions"]}.isdisjoint(
        d["key"] for d in second["previous_decisions"]
    )


def test_closed_history_survives_projection_pruning(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    records = [dict(item(n), status="resolved") for n in range(40)]
    result = store.publish("desk", 0, {}, "Progress", records)
    assert len(result["decisions"]) == 20
    history = store.decision_history("desk", limit=100)
    assert len(history) == 40
    assert {h["decision"]["key"] for h in history} == {d["key"] for d in records}


def test_dismiss_requires_current_revision_and_preserves_original(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    current = store.publish("desk", 0, {}, "Progress", [item(1)])
    with pytest.raises(ConflictError):
        store.dismiss_decision("desk", "q1", 0, "No longer relevant")
    after = store.dismiss_decision("desk", "q1", current["revision"], "No longer relevant")
    assert after["decisions"][0]["status"] == "superseded"
    history = store.decision_history("desk", limit=100)
    assert any(h["decision"] == item(1) for h in history)
    assert any(h.get("note") == "No longer relevant" for h in history)


def test_audit_flags_duplicates_old_requests_and_team_questions_without_closing(tmp_path):
    from ai_sessions.desk.backlog import audit

    record = dict(
        decisions=[
            item(1),
            dict(item(2), question="Choose option 1?"),
            dict(item(3), question="Worker 2: can you inspect the file?"),
        ],
        decision_meta={"q1": dict(last_changed=time.time() - 8 * 86400, imported=False)},
    )
    report = audit(record)
    assert "Duplicate wording" in report[1]["flags"]
    assert "Unchanged for at least 7 days" in report[0]["flags"]
    assert "Possible team coordination" in report[2]["flags"]
    assert all(d["status"] == "pending" for d in record["decisions"])


def test_publication_omission_cannot_delete_an_unanswered_request(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    first = store.publish("desk", 0, {}, "First", [item(1)])
    second = store.publish("desk", first["revision"], {}, "Next", [item(2)])
    assert second["decisions"] == [item(1), item(2)]


def test_large_individual_context_is_excerpted_only_for_the_model():
    from ai_sessions.desk.backlog import WINDOW_CHARS, working_set

    original = dict(item(1), question="q" * 8000, context="x" * 8000, recommendation="r" * 8000)
    record = dict(decisions=[original], observation={})
    selected = working_set(record)
    assert len(json.dumps(selected, ensure_ascii=False)) <= WINDOW_CHARS
    assert "full request retained" in selected[0]["context"]
    assert record["decisions"][0]["context"] == "x" * 8000


def test_store_reopen_does_not_reset_age_or_multiply_history(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    first = store.publish("desk", 0, {}, "First", [item(1)])
    before = first["decision_meta"]["q1"]
    history = store.decision_history("desk")
    reopened = Store(tmp_path)
    assert reopened.get("desk")["decision_meta"]["q1"] == before
    assert reopened.decision_history("desk") == history


def test_cli_listing_and_dismiss_are_explicit_and_revision_checked(tmp_path, monkeypatch, capsys):
    from ai_sessions.desk import cli

    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    current = store.publish("desk", 0, {}, "First", [item(1)])
    monkeypatch.setattr(cli, "Store", lambda: store)
    monkeypatch.setattr(cli, "resolve_desk_name", lambda store, name, **kw: name)
    assert cli.main(["decisions", "desk", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["revision"] == current["revision"]
    assert cli.main(["dismiss", "desk", "1", "--revision", "0", "--reason", "Duplicate"]) == 2
    assert store.get("desk")["decisions"][0]["status"] == "pending"
    assert (
        cli.main(
            [
                "dismiss",
                "desk",
                "1",
                "--revision",
                str(current["revision"]),
                "--reason",
                "Duplicate",
            ]
        )
        == 0
    )
    assert store.get("desk")["decisions"][0]["status"] == "superseded"


def test_review_audit_prefers_canonical_asks_over_false_legacy_premises():
    from ai_sessions.desk.backlog import audit

    record = dict(
        decisions=[item(1), dict(item(2), question=item(1)["question"])],
        decision_details={
            "q1": {"summary": "Did you approve the test that already ran?"},
            "q2": {"summary": "Should we book another test?"},
        },
    )
    assert not any("Duplicate wording" in row["flags"] for row in audit(record))


def test_index_exposes_old_keys_outside_detailed_window(tmp_path):
    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    current = store.publish("desk", 0, {}, "First", [item(n) for n in range(62)])
    coverage = [dict(id="s0", status="available", changed=True)]
    payload = json.loads(
        observer._build_prompt(
            current, coverage, [dict(citation="s0", text="Answer to an older question")]
        ).split("\n", 1)[1]
    )
    assert len(payload["previous_decisions"]) <= 20
    assert len(payload["pending_question_index"]) == 62
    assert not payload["pending_question_index_partial"]


def test_newest_creation_order_survives_rewording_and_store_reopen(tmp_path):
    from ai_sessions.desk.backlog import ordered_decisions

    store = Store(tmp_path)
    store.create(
        "desk", [{"tool": "claude", "session_id": "fixture", "storage": "log"}], str(tmp_path)
    )
    first = store.publish("desk", 0, {}, "First", [item(1), item(2)])
    second = store.publish(
        "desk",
        first["revision"],
        {},
        "Next",
        [dict(item(1), context="Updated background"), item(3)],
    )
    assert [d["key"] for d in ordered_decisions(second)] == ["q3", "q2", "q1"]
    assert [d["key"] for d in ordered_decisions(Store(tmp_path).get("desk"))] == ["q3", "q2", "q1"]


def test_same_batch_duplicate_wording_reuses_one_key():
    first = item(1)
    duplicate = dict(item(2), question=first["question"])
    assert [d["key"] for d in observer._reconcile([], [first, duplicate], [])] == ["q1"]


def test_known_key_wins_over_another_questions_lossy_wording():
    first, second = item(1), item(2)
    changed = dict(second, question=first["question"])
    result = observer._reconcile([first, second], [changed], [])
    assert {d["key"] for d in result} == {"q1", "q2"}
    assert next(d for d in result if d["key"] == "q1") == first
