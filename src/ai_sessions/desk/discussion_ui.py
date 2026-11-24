"""Pure keyboard editor and human-facing discussion copy for the terminal view."""

from __future__ import annotations

import curses
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .discussion import MAX_TEXT
from .time_format import eastern_time

ACTIONS = ("Save note", "Ask for clarification", "Send answer", "Mark as completed")
ACTION_HELP = (
    "Keep this note with the question.",
    "Ask the reviewer to explain or find missing context.",
    "Send your answer to the Desk agent.",
    "Close this question. Your comment is optional. You can undo completion.",
)


def answer(snapshot, key, *, core=None):
    from .decisions import core_fingerprint
    from .discussion import completed

    completion = completed(snapshot).get(key)
    if completion:
        return completion

    core = core or next((d for d in snapshot.get("decisions", []) if d.get("key") == key), None)
    if core is None:
        return None
    fingerprint = core_fingerprint(core)
    result = next(
        (
            row
            for row in reversed(snapshot.get("decision_discussion", {}).get(key, []))
            if row["kind"] == "decision" and row.get("core_hash") == fingerprint
        ),
        None,
    )
    changed = snapshot.get("decision_meta", {}).get(key, {}).get("last_changed", 0)
    if result and core.get("status") == "pending" and changed > result["created_at"]:
        return None
    if result:
        old = (result.get("card", {}).get("details") or {}).get("summary") or result.get(
            "card", {}
        ).get("decision", {}).get("question", "")
        current = (snapshot.get("decision_details", {}).get(key) or {}).get("summary") or core.get(
            "question", ""
        )
        # First-time choice hydration expands a legacy card; it does not revoke
        # the answer attached to its unchanged core. A reconstructed historical
        # approval question (information), or a changed existing canonical ask,
        # still requires human review.
        initial_choice = (
            not result.get("card", {}).get("details")
            and (snapshot.get("decision_details", {}).get(key) or {}).get("kind") == "choice"
        )
        previous_kind = (result.get("card", {}).get("details") or {}).get("kind")
        current_kind = (snapshot.get("decision_details", {}).get(key) or {}).get("kind")
        same_choice = previous_kind in ("choice", "action") and previous_kind == current_kind
        # Background copy may be regenerated while the authoritative request is
        # unchanged. That must not silently revoke the human's saved answer.
        if not (initial_choice or same_choice) and " ".join(old.casefold().split()) != " ".join(
            current.casefold().split()
        ):
            return None
    return result


def answered_keys(snapshot):
    if not snapshot:
        return set()
    discussion = snapshot.get("decision_discussion", {})
    return {
        core["key"]
        for core in snapshot.get("decisions", [])
        if core["key"] in discussion and answer(snapshot, core["key"], core=core)
    }


def receipt(snapshot, row, wrap, width, *, reviewing=False):
    from .presentation import line

    kind = row["kind"]
    delivery = row.get("delivery")
    if kind == "completion":
        title, detail, label, primary = (
            "Marked as completed",
            "[u Undo completion]",
            "Your comment",
            "Enter: Next unanswered question",
        )
    elif kind == "decision":
        title = (
            "Answer sent to the Desk agent"
            if delivery in ("submitted", "delivered")
            else "Your answer is saved"
        )
        detail = "Waiting to send to the Desk agent."
        if delivery in ("submitted", "delivered"):
            detail = "You can move on to the next question."
        elif delivery == "uncertain":
            detail = "Delivery is unconfirmed. Check the Desk conversation before sending again."
        elif delivery == "binding_changed":
            detail = "The Desk recipient changed. Your answer is saved; delivery is paused."
        elif row.get("delivery_wait") == "offline":
            detail = "Waiting for the Desk agent to connect."
        elif not row.get("recipient"):
            detail = "Open the Desk conversation to send your saved answer."
        elif row.get("delivery_wait") == "ambiguous":
            detail = "The Desk connection is ambiguous. Your answer is saved; delivery is paused."
        if delivery == "uncertain":
            title = "Answer saved; delivery unconfirmed"
        elif delivery == "binding_changed":
            title = "Answer saved; delivery paused"
        label = "Your answer"
        primary = "Enter: Unanswered questions" if reviewing else "Enter: Next unanswered question"
    elif kind == "note":
        title, detail, label, primary = (
            "Note saved",
            "",
            "Your note",
            "Enter: Return to the question",
        )
    else:
        title, detail, label, primary = (
            status(row, bool(snapshot.get("agents"))),
            "",
            "Your question",
            "Enter: View the question",
        )
        if row["status"] == "complete":
            primary = "Enter: View the updated question"
        detail = "n/p: Move to another question. Your clarification stays in Discussion."
        if not row.get("updated_details"):
            detail = "The decision details are unchanged so far. " + detail
    card = row.get("card") or {}
    question = (card.get("details") or {}).get("summary") or card.get("decision", {}).get(
        "question", ""
    )
    parts = [line(title, "success"), line("[ " + primary + " ]", "focus"), ""]
    if row["text"]:
        parts += [
            line(label, "heading"),
            line("│ " + row["text"].replace("\n", "\n│ "), "user"),
            "",
        ]
    if detail:
        parts += [detail, ""]
    if row.get("reply"):
        parts += ["", "Clarification", row["reply"]]
    if row["status"] == "failed":
        parts += ["", "r Retry clarification"]
    if (
        kind == "decision"
        and delivery == "queued"
        and (not row.get("recipient") or row.get("delivery_wait") == "offline")
    ):
        command = "ses desk open " + snapshot["name"]
        tool = (row.get("recipient") or {}).get("tool")
        if tool:
            command += " --tool " + tool
        parts += ["", "To connect, run in another terminal:", command]
    parts += ["", line("Question", "heading"), question]
    return [row for part in parts for row in wrap(part, width)]


@dataclass(frozen=True)
class Editor:
    key: str
    version: str
    text: str = ""
    cursor: int = 0
    focus: int = 0
    request: str | None = None


def edit(editor, key):
    if key == "\x1b":
        return None
    if key in ("\t", curses.KEY_BTAB):
        return replace(
            editor, focus=(editor.focus + (1 if key == "\t" else -1)) % (len(ACTIONS) + 1)
        )
    if editor.focus:
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            return replace(
                editor,
                focus=1
                + (editor.focus - 1 + (1 if key == curses.KEY_RIGHT else -1)) % len(ACTIONS),
            )
        if key in ("\n", "\r", curses.KEY_ENTER):
            return replace(
                editor,
                request=("note", "clarification", "decision", "completion")[editor.focus - 1],
            )
        return editor
    cursor, text = editor.cursor, editor.text
    if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
        return replace(
            editor, text=text[: max(0, cursor - 1)] + text[cursor:], cursor=max(0, cursor - 1)
        )
    if key == curses.KEY_DC:
        return replace(editor, text=text[:cursor] + text[cursor + 1 :])
    if key == curses.KEY_LEFT:
        return replace(editor, cursor=max(0, cursor - 1))
    if key == curses.KEY_RIGHT:
        return replace(editor, cursor=min(len(text), cursor + 1))
    if key == curses.KEY_HOME:
        return replace(editor, cursor=text.rfind("\n", 0, cursor) + 1)
    if key == curses.KEY_END:
        end = text.find("\n", cursor)
        return replace(editor, cursor=len(text) if end < 0 else end)
    if key in (curses.KEY_UP, curses.KEY_DOWN):
        start = text.rfind("\n", 0, cursor) + 1
        column = cursor - start
        if key == curses.KEY_UP and start:
            previous = text.rfind("\n", 0, start - 1) + 1
            cursor = min(previous + column, start - 1)
        elif key == curses.KEY_DOWN:
            end = text.find("\n", cursor)
            if end >= 0:
                following = text.find("\n", end + 1)
                cursor = min(end + 1 + column, len(text) if following < 0 else following)
        return replace(editor, cursor=cursor)
    if key in ("\r", curses.KEY_ENTER):
        key = "\n"
    if (
        isinstance(key, str)
        and len(key) == 1
        and (key == "\n" or key.isprintable())
        and len(text) < MAX_TEXT
    ):
        return replace(editor, text=text[:cursor] + key + text[cursor:], cursor=cursor + 1)
    return editor


def editor_layout(editor, wrap, width, height, title):
    from .presentation import line
    from .view import cell_width

    width = max(1, width)
    # The view reserves header, tabs, action strip and footer.
    capacity = max(1, height - 4)
    actions = []
    for i, action in enumerate(ACTIONS, 1):
        actions += wrap(
            line(
                ("› [ " + action + " ]") if editor.focus == i else "  [ " + action + " ]",
                "focus" if editor.focus == i else "action",
            ),
            width,
        )
    if height < 14 and width >= 36:
        # Two compact action rows leave a real multiline field in short panes.
        labels = ("Save note", "Ask clarification", "Send answer", "Mark completed")
        actions = []
        for first in (1, 3):
            text = "  ".join(
                ("›" if editor.focus == i else " ") + "[" + labels[i - 1] + "]"
                for i in (first, first + 1)
            )
            actions.append(line(text, "focus" if editor.focus in (first, first + 1) else "action"))
    title_rows = wrap(line(title, "question"), width)[: 1 if height < 18 else 2]
    prefix = title_rows + [
        line("Your response" + (" · typing" if not editor.focus else ""), "heading")
    ]
    help_rows = (
        []
        if height < 18
        else wrap(
            ACTION_HELP[editor.focus - 1]
            if editor.focus
            else "Type your response. Tab selects an action.",
            width,
        )[:2]
    )
    field_height = max(1, capacity - len(prefix) - len(actions) - len(help_rows) - 1)
    rows, current, column = [], "", 0
    caret = (0, 0)
    for index in range(len(editor.text) + 1):
        char = editor.text[index] if index < len(editor.text) else None
        size = cell_width(char) if char and char != "\n" else 0
        if column >= width or char not in (None, "\n") and column + size > width:
            rows.append(current)
            current, column = "", 0
        if index == editor.cursor:
            caret = (len(rows), column)
        if char is None:
            break
        if char == "\n":
            rows.append(current)
            current, column = "", 0
        else:
            current += char
            column += size
    rows.append(current)
    start = max(0, caret[0] - field_height + 1)
    visible = rows[start : start + field_height]
    visible += [""] * (field_height - len(visible))
    result = prefix + [line(row, "user") for row in visible] + [""] + actions + help_rows
    cursor = (len(prefix) + caret[0] - start, caret[1]) if not editor.focus else None
    return result, cursor


def editor_lines(editor, wrap, width, height, title):
    return editor_layout(editor, wrap, width, height, title)[0]


def status(row, bound=True):
    if row["kind"] == "completion":
        return "Completion undone" if row.get("undone_at") else "Marked as completed"
    state = row["status"]
    if state == "failed":
        return "Clarification unavailable · r Retry"
    if state == "outdated":
        return "The card changed; this reply belongs to the earlier version."
    if state == "working":
        return "Preparing clarification"
    if state == "waiting":
        if row.get("delivery_wait") == "offline":
            return "Waiting for project context · Desk agent is offline"
        if row.get("delivery") == "uncertain":
            return "Waiting for project context · delivery unconfirmed"
        return "Waiting for project context" + (
            " · open the Desk conversation" if not bound else ""
        )
    if row["kind"] == "decision":
        delivery = row.get("delivery")
        if delivery in ("submitted", "delivered"):
            return "Answer submitted to the Desk conversation"
        if delivery == "uncertain":
            return "Answer saved · delivery unconfirmed; check the Desk conversation"
        if delivery == "binding_changed":
            return "Answer saved · Desk recipient changed; delivery paused"
        if row.get("delivery_wait") == "offline":
            return "Answer saved · waiting for the Desk agent"
        return "Answer saved · waiting to send" + (
            " · open the Desk conversation" if not bound else ""
        )
    return {
        "queued": "Clarification queued",
        "saved": "Note saved",
        "complete": "Clarification ready",
    }.get(state, "Saved")


def _copy(card):
    details = card.get("details") or {}
    core = card.get("decision") or {}
    lines = [
        details.get("summary") or core.get("question", ""),
        details.get("background") or core.get("context", ""),
    ]
    for field, label in (
        ("why_now", "Why now"),
        ("if_deferred", "If deferred"),
        ("deadline", "Deadline"),
    ):
        if details.get(field):
            lines += [label + ": " + details[field]]
    for option in details.get("options", []):
        lines += [
            option["label"] + ": " + option["description"],
            "Benefits: " + "; ".join(option["benefits"]),
            "Risks: " + "; ".join(option["risks"]),
            "Consequence: " + option["consequences"],
        ]
    recommendation = details.get("recommendation") or {}
    reason = recommendation.get("reason") or core.get("recommendation")
    if reason:
        lines += ["Recommendation: " + reason]
    if details.get("missing_information"):
        lines += ["Missing information: " + "; ".join(details["missing_information"])]
    for source in details.get("evidence", []):
        lines += [source["title"], source["text"]]
    return lines


def lines(snapshot, key, expanded=False, show_versions=False):
    from .presentation import line

    rows = snapshot.get("decision_discussion", {}).get(key, [])
    result = ["", line("Discussion" + (f" ({len(rows)})" if rows else ""), "heading")]
    if expanded:
        result = [line("Discussion", "heading"), line("t Back · v Earlier versions", "action"), ""]
    if not rows and not expanded:
        return []
    if not rows:
        if not show_versions:
            return result + ["No comments yet.", "e Write a note or question"]
    clarification = next((r for r in reversed(rows) if r["kind"] == "clarification"), None)
    selected = rows if expanded else [r for r in rows if r is clarification or r is rows[-1]]
    for row in selected:
        stamp = eastern_time(datetime.fromtimestamp(row["created_at"], timezone.utc))
        label = {
            "note": "Your note",
            "clarification": "Your question",
            "decision": "Your answer",
            "completion": "Your completion",
        }[row["kind"]]
        result += [
            "",
            line(label + " · " + stamp, "heading"),
            line("│ " + row["text"].replace("\n", "\n│ "), "user"),
            line(status(row, bool(snapshot.get("agents"))), "metadata"),
        ]
        if row.get("reply"):
            result += ["", "Clarification", row["reply"]]
        if row.get("agent_reply"):
            result += ["", "Project context from the Desk conversation", row["agent_reply"]]
        if expanded and show_versions:
            for response in row.get("responses", [])[:-1]:
                result += ["", "Earlier clarification", response["reply"]]
                if response.get("details"):
                    result += [
                        "Earlier updated decision",
                        *_copy(dict(details=response["details"])),
                    ]
            result += ["", "Decision when you wrote this", *_copy(row["card"])]
            if row.get("updated_details"):
                result += ["", "Updated decision", *_copy(dict(details=row["updated_details"]))]
    if expanded and show_versions:
        for version in snapshot.get("_discussion_versions", {}).get(key, []):
            stamp = eastern_time(datetime.fromtimestamp(version["created_at"], timezone.utc))
            result += ["", "Saved decision version · " + stamp, *_copy(version["card"])]
    return result


def latest_clarification(snapshot, key):
    """Keep the latest saved exchange visible when the receipt is dismissed."""
    from .presentation import line

    row = next(
        (
            r
            for r in reversed(snapshot.get("decision_discussion", {}).get(key, []))
            if r["kind"] == "clarification"
        ),
        None,
    )
    if not row:
        return []
    parts = [
        line("Your latest clarification", "heading"),
        line("│ " + row["text"].replace("\n", "\n│ "), "user"),
        line(status(row, bool(snapshot.get("agents"))), "metadata"),
    ]
    if row.get("reply"):
        parts += [line("Clarification response", "heading"), row["reply"]]
    if not row.get("updated_details"):
        parts += ["The decision details are unchanged so far."]
    parts += [line("n/p Other questions · t Full discussion", "action"), ""]
    return parts
