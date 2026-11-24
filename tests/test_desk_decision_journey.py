from ai_sessions.desk import view
from ai_sessions.desk.decisions import core_fingerprint


def fixture(kind="decision", delivery="queued"):
    core = dict(
        key="day",
        question="Choose a delivery day",
        context="Long project background. " * 80,
        recommendation="Friday",
        status="pending",
        evidence=["s0"],
    )
    entry = dict(
        id="entry",
        key="day",
        kind=kind,
        text="Friday, after the site is ready.",
        core_hash=core_fingerprint(core),
        card=dict(decision=core, details=None),
        status="queued",
        delivery=delivery,
        created_at=1795540000,
        reply=None,
        recipient=dict(tool="claude", session_id="desk", home="home"),
    )
    return dict(
        name="Garden",
        decisions=[core, dict(core, key="next", question="Choose the plant mix")],
        decision_details={},
        decision_discussion={"day": [entry]},
        decision_drafts={},
        agents={"claude": {"session_id": "desk", "home": "home"}},
    )


def test_saved_answer_is_visible_immediately_without_scrolling():
    snap = fixture()
    text = "\n".join(view.render(snap, view.State(tab=3, selected_key="day"), 40, 18).lines)
    assert "Your answer is saved" in text
    assert "Friday, after the site is ready." in text
    assert "Next unanswered question" in text
    assert "Long project background" not in text


def test_next_skips_answered_without_resolving_core():
    snap = fixture()
    assert [d["key"] for d in view._pending(snap)] == ["next"]
    state = view.handle_key(view.State(tab=3, selected_key="day"), "\n", snap, 40, 18)
    assert state.selected_key == "next"
    assert snap["decisions"][0]["status"] == "pending"
    state = view.handle_key(state, "a", snap, 40, 18)
    assert state.selected_key == "day"


def test_clarification_remains_unanswered():
    snap = fixture(kind="clarification")
    assert [d["key"] for d in view._pending(snap)] == ["day", "next"]


def test_offline_answer_has_truthful_recovery_state():
    snap = fixture()
    snap["decision_discussion"]["day"][0]["delivery_wait"] = "offline"
    text = "\n".join(view.render(snap, view.State(tab=3, selected_key="day"), 60, 25).lines)
    assert "Waiting for the Desk agent to connect" in text
    assert "ses desk open Garden --tool claude" in text
    assert "Answer sent" not in text


def test_discussion_does_not_dump_card_copies_by_default():
    snap = fixture()
    state = view.handle_key(view.State(tab=3, selected_key="day"), "t", snap, 60, 30)
    text = "\n".join(view.render(snap, state, 60, 30).lines)
    assert "Your answer" in text
    assert "Long project background" not in text
    assert "v Earlier versions" in text


def test_old_answer_does_not_hide_a_changed_question():
    snap = fixture()
    snap["decisions"][0] = dict(snap["decisions"][0], question="Choose a delivery week")
    assert [d["key"] for d in view._pending(snap)] == ["day", "next"]


def test_corrected_canonical_ask_is_not_hidden_by_answer_to_legacy_premise():
    snap = fixture()
    snap["decision_details"]["day"] = dict(
        schema_version=1, summary="Did you already approve Friday delivery?"
    )
    assert [d["key"] for d in view._pending(snap)] == ["day", "next"]


def test_reopened_question_requires_a_fresh_answer():
    snap = fixture()
    snap["decision_meta"] = {"day": {"last_changed": 1795540100}}
    assert [d["key"] for d in view._pending(snap)] == ["day", "next"]


def test_empty_saved_answers_does_not_claim_all_questions_are_done():
    snap = fixture(kind="clarification")
    state = view.handle_key(view.State(tab=3), "a", snap, 60, 24)
    text = "\n".join(view.render(snap, state, 60, 24).lines)
    assert "no answers to the current questions" in text
    assert "a Return to unanswered questions" in text


def test_legacy_answer_survives_choice_background_hydration():
    snap = fixture()
    snap["decision_details"]["day"] = dict(
        kind="choice", summary="Choose Friday delivery or provide a different delivery day."
    )
    assert [d["key"] for d in view._pending(snap)] == ["next"]


def test_pending_order_is_newest_created_not_latest_reworded():
    snap = fixture(kind="clarification")
    snap["decision_meta"] = {
        "day": dict(first_seen=10, last_changed=99, sequence=1),
        "next": dict(first_seen=20, last_changed=20, sequence=2),
    }
    assert [d["key"] for d in view._pending(snap)] == ["next", "day"]


def test_imported_ties_use_stable_creation_sequence():
    snap = fixture(kind="clarification")
    snap["decision_meta"] = {
        "day": dict(first_seen=10, sequence=1),
        "next": dict(first_seen=10, sequence=2),
    }
    assert [d["key"] for d in view._pending(snap)] == ["next", "day"]


def test_cli_audit_and_view_have_the_same_numbered_unanswered_projection():
    from ai_sessions.desk.backlog import audit

    snap = fixture()
    assert (
        [row["key"] for row in audit(snap)] == [d["key"] for d in view._pending(snap)] == ["next"]
    )
    assert audit(snap)[0]["number"] == 1


def test_completed_question_review_has_undo_instead_of_dead_response_controls():
    snap = fixture(kind="completion")
    snap["decisions"][0]["status"] = "resolved"
    state = view.State(tab=3, selected_key="day", question_open=frozenset({"day"}))
    text = "\n".join(view.render(snap, state, 60, 24).lines)
    assert "[u Undo completion]" in text
    assert "[e Respond]" not in text and "[c Mark completed]" not in text


def test_background_rewording_does_not_revoke_answer_to_unchanged_choice():
    snap = fixture()
    snap["decision_discussion"]["day"][0]["card"]["details"] = dict(
        kind="choice", summary="Who should choose the delivery day?"
    )
    snap["decision_details"]["day"] = dict(
        kind="choice", summary="Who should set the delivery day and review the wording?"
    )
    assert [d["key"] for d in view._pending(snap)] == ["next"]
    snap["decision_details"]["day"]["kind"] = "information"
    assert [d["key"] for d in view._pending(snap)] == ["day", "next"]


def test_next_answer_continues_after_current_instead_of_restarting_at_first():
    snap = fixture()
    core = snap["decisions"][0]
    snap["decisions"] = [dict(core, key="earlier"), core, dict(core, key="later")]
    state = view.State(tab=3, selected_key="day")
    state = view.handle_key(state, "\n", snap, 60, 24)
    assert state.selected_key == "later"
