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
