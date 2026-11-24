"""Pure keyboard editor and human-facing discussion copy for the terminal view."""

from __future__ import annotations

import curses
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .discussion import MAX_TEXT
from .time_format import eastern_time

ACTIONS = ("Save note", "Ask for clarification", "Send answer")
ACTION_HELP = (
    "Keep this note with the question.",
    "Ask the reviewer to explain or find missing context.",
    "Send your answer to the Desk agent.",
)


def answer(snapshot, key, *, core=None):
    from .decisions import core_fingerprint

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
        if " ".join(old.casefold().split()) != " ".join(current.casefold().split()):
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
    kind = row["kind"]
    delivery = row.get("delivery")
    if kind == "decision":
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
    card = row.get("card") or {}
    question = (card.get("details") or {}).get("summary") or card.get("decision", {}).get(
        "question", ""
    )
    parts = [title, primary, "b Question · e Comment · t Discussion", ""]
    parts += [label, row["text"], ""]
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
    parts += ["", "Question", question]
    return wrap("\n".join(parts), width)


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
        return replace(editor, focus=(editor.focus + (1 if key == "\t" else -1)) % 4)
    if editor.focus:
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            return replace(
                editor, focus=1 + (editor.focus - 1 + (1 if key == curses.KEY_RIGHT else -1)) % 3
            )
        if key in ("\n", "\r", curses.KEY_ENTER):
            return replace(editor, request=("note", "clarification", "decision")[editor.focus - 1])
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


def editor_lines(editor, wrap, width, height, title):
    marked = editor.text[: editor.cursor] + "▏" + editor.text[editor.cursor :]
    lines = wrap(marked, width)
    before = wrap(editor.text[: editor.cursor] + "▏", width)
    capacity = max(1, height - 18)
    start = max(0, len(before) - capacity)
    visible = lines[start : start + capacity]
    result = wrap("Question: " + title, width)[:2] + [
        "",
        "Your response" + (" [typing]" if editor.focus == 0 else ""),
        *visible,
        "",
    ]
    if height < 15:
        result = wrap("Your comment", width)[:1] + visible[-1:]
    for i, action in enumerate(ACTIONS, 1):
        result += wrap(("[ " + action + " ]") if editor.focus == i else action, width)
    if height < 15:
        return result + wrap("Tab selects · Enter activates", width)[:1]
    result += [
        "",
        *wrap(
            ACTION_HELP[editor.focus - 1]
            if editor.focus
            else "Type your response. Tab chooses what to do with it.",
            width,
        ),
        *wrap("Enter: new line / selected action · Esc: keep draft", width),
    ]
    return result


def status(row, bound=True):
    state = row["status"]
    if state == "failed":
        return "Clarification unavailable · r Retry"
    if state == "outdated":
        return "The card changed; this reply belongs to the earlier version."
    if state == "working":
        return "Preparing clarification"
    if state == "waiting":
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
    rows = snapshot.get("decision_discussion", {}).get(key, [])
    result = ["", "Discussion" + (f" ({len(rows)})" if rows else "")]
    if expanded:
        result = ["Discussion", "t Back · v Earlier versions", ""]
    if not rows and not expanded:
        return []
    if not rows:
        if not show_versions:
            return result + ["No comments yet.", "e Write a note or question"]
    clarification = next((r for r in reversed(rows) if r["kind"] == "clarification"), None)
    selected = rows if expanded else [r for r in rows if r is clarification or r is rows[-1]]
    for row in selected:
        stamp = eastern_time(datetime.fromtimestamp(row["created_at"], timezone.utc))
        label = {"note": "Your note", "clarification": "Your question", "decision": "Your answer"}[
            row["kind"]
        ]
        result += [
            "",
            label + " · " + stamp,
            row["text"],
            status(row, bool(snapshot.get("agents"))),
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
