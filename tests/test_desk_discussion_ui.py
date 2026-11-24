import curses

from ai_sessions.desk import view
from ai_sessions.desk.discussion_ui import Editor, edit, editor_lines


def snapshot():
    return dict(
        name="fixture",
        decisions=[
            dict(
                key="ask",
                question="Choose a delivery day",
                context="Friday or Monday",
                recommendation="Friday",
                status="pending",
                evidence=["s0"],
            )
        ],
        decision_details={},
        decision_drafts={},
    )


def test_editor_types_navigation_letters_and_multiline_without_leaving():
    editor = Editor("ask", "version")
    for key in "qnp123?\nSecond line":
        editor = edit(editor, key)
    assert editor.text == "qnp123?\nSecond line"
    assert editor.request is None
    editor = edit(edit(editor, "\t"), "\t")
    assert edit(editor, "\n").request == "clarification"


def test_existing_draft_reopens_and_esc_exits_only_editor():
    snap = snapshot()
    snap["decision_drafts"] = {"ask": dict(text="Unsent question", version="old")}
    state = view.handle_key(view.State(tab=3), "e", snap, 40, 24)
    assert state.editor.text == "Unsent question"
    state = view.handle_key(state, "q", snap, 40, 24)
    assert not state.quit and state.editor.text.endswith("q")
    state = view.handle_key(state, "\x1b", snap, 40, 24)
    assert state.editor is None and not state.quit


def test_editor_actions_visible_at_40_60_100_columns():
    for width in (40, 60, 100):
        lines = editor_lines(
            Editor("ask", "v", "Long note " * 200, 2000),
            view.wrap,
            width,
            24,
            "Choose a delivery day",
        )
        assert all(view.cell_width(line) <= width for line in lines)
        assert "Ask for clarification" in "\n".join(lines)


def test_discussion_controls_do_not_break_original_tab_switching():
    state = view.State(tab=3)
    snap = snapshot()
    state = view.handle_key(state, "t", snap, 40, 24)
    assert "ask" in state.discussions
    state = view.handle_key(state, curses.KEY_BTAB, snap, 40, 24)
    assert state.tab == 2


def test_complete_action_is_explicit_and_supports_optional_text():
    editor = Editor("ask", "version", focus=4)
    assert edit(editor, "\n").request == "completion"
    assert "Mark as completed" in "\n".join(editor_lines(editor, view.wrap, 40, 24, "Choose a day"))


def test_visual_roles_survive_wrap_scroll_and_frame_layout():
    from ai_sessions.desk.presentation import line

    wrapped = view.wrap(line("A long heading " * 8, "heading"), 20)
    assert len(wrapped) > 1 and all(row.role == "heading" for row in wrapped)
    frame = view.render(snapshot(), view.State(tab=3), 60, 24)
    assert "question" in frame.roles
    assert "action" in frame.roles
    assert "metadata" in frame.roles


def test_return_from_clarification_keeps_question_reply_and_offline_status_visible():
    snap = snapshot()
    row = dict(
        id="clarify",
        key="ask",
        kind="clarification",
        text="Why not Monday?",
        status="waiting",
        reply="Monday is possible, but availability is unconfirmed.",
        delivery="queued",
        delivery_wait="offline",
        created_at=1,
    )
    snap["agents"] = {"codex": {"id": "bound-but-offline"}}
    snap["decision_discussion"] = {"ask": [row]}
    state = view.normalize(view.State(tab=3, selected_key="ask", feedback=row), snap, 60, 24)
    state = view.handle_key(state, "\n", snap, 60, 24)
    text = "\n".join(view._body(snap, state, 60))
    assert "Why not Monday?" in text
    assert "Monday is possible" in text
    assert "Desk agent is offline" in text
    assert "unchanged" in text.lower()
    assert "n/p" in text


def test_clarification_receipt_explains_waiting_without_claiming_updated_card():
    from ai_sessions.desk.discussion_ui import receipt

    snap = snapshot()
    row = dict(
        kind="clarification",
        text="Explain Friday",
        status="waiting",
        delivery="queued",
        delivery_wait="offline",
        reply="Need the delivery schedule.",
    )
    text = "\n".join(receipt(snap, row, view.wrap, 60))
    assert "Desk agent is offline" in text
    assert "unchanged" in text.lower()
    assert "n/p" in text
