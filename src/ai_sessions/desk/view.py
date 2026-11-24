"""Project Desk terminal view with durable decision input and pure rendering.

Rendering is deterministic with explicit ``now``; normalization and keys are pure. ``run`` owns polling and curses; ``read_snapshot`` handles store failures. Decision keys remain in state, never in the frame.
"""

from __future__ import annotations

import curses
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .discussion_ui import Editor
from .presentation import line as styled
from .time_format import eastern_time


class SnapshotStore(Protocol):
    def get(self, name: str) -> dict: ...


@dataclass(frozen=True)
class State:
    tab: int = 1
    selected_key: str | None = None
    scroll: int = 0
    quit: bool = False
    help: bool = False
    expanded: frozenset[str] = frozenset()
    retained: dict | None = None
    editor: Editor | None = None
    discussions: frozenset[str] = frozenset()
    notice: tuple[str, str] | None = None
    feedback: dict | None = None
    answered: bool = False
    question_open: frozenset[str] = frozenset()
    versions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Frame:
    lines: tuple[str, ...]
    roles: tuple[str, ...] = ()


# Remove complete OSC/DCS strings and CSI sequences before filtering controls.
_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[P^_]|\x90).*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[@-_]",
    re.DOTALL,
)


def sanitize(value: object) -> str:
    text = _ESCAPE.sub("", str(value or ""))
    return "".join(
        ch if ch == "\n" or not unicodedata.category(ch).startswith("C") else " " for ch in text
    )


def cell_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(ch)
        else 2
        if unicodedata.east_asian_width(ch) in ("W", "F")
        else 1
        for ch in text
    )


def wrap(value: object, width: int) -> list[str]:
    """Wrap at terminal cells, preferring spaces; never split a wide glyph."""
    width = max(1, width)
    result = []
    for paragraph in sanitize(value).split("\n"):
        line = ""
        used = 0
        for ch in paragraph:
            size = cell_width(ch)
            if size > width:
                ch, size = "?", 1
            if used + size > width:
                split = line.rfind(" ")
                if split > 0:
                    result.append(line[:split])
                    line = line[split + 1 :]
                else:
                    result.append(line)
                    line = ""
                # The remainder plus a wide character can still overflow.
                used = cell_width(line)
                if used + size > width:
                    result.append(line)
                    line = ""
                    used = 0
            line += ch
            used += size
        result.append(line)
    return [styled(row, getattr(value, "role", "body")) for row in result]


def _pending(snapshot: dict | None) -> list[dict]:
    from .backlog import ordered_decisions
    from .discussion_ui import answered_keys

    answered = answered_keys(snapshot)
    return [
        item
        for item in ordered_decisions(snapshot)
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("key") not in answered
    ]


def _answered(snapshot):
    from .backlog import ordered_decisions
    from .discussion_ui import answered_keys

    keys = answered_keys(snapshot)
    rows = [row for row in ordered_decisions(snapshot) if row.get("key") in keys]
    from .discussion import completed

    known = {row["key"] for row in rows}
    rows.extend(
        dict(entry["card"]["decision"], status="resolved")
        for key, entry in completed(snapshot or {}).items()
        if key not in known
    )
    return rows


def _browse(snapshot, state):
    return _answered(snapshot) if state.answered else _pending(snapshot)


def _feedback(snapshot, state):
    if state.selected_key in state.question_open:
        return None
    rows = (snapshot or {}).get("decision_discussion", {}).get(state.selected_key, [])
    if state.feedback and state.feedback["key"] == state.selected_key:
        current = next(
            (
                row
                for row in (snapshot or {}).get("decisions", [])
                if row.get("key") == state.selected_key
            ),
            None,
        )
        from .decisions import core_fingerprint

        if current and state.feedback.get("core_hash") == core_fingerprint(current):
            if state.feedback["kind"] == "decision":
                from .discussion_ui import answer

                return answer(snapshot, state.selected_key)
            return next((row for row in rows if row["id"] == state.feedback["id"]), state.feedback)
    from .discussion_ui import answer

    return answer(snapshot or {}, state.selected_key)


def _with_saved_feedback(snapshot, feedback):
    if not snapshot or not feedback:
        return snapshot
    rows = snapshot.get("decision_discussion", {}).get(feedback["key"], [])
    if any(row["id"] == feedback["id"] for row in rows):
        return snapshot
    return dict(
        snapshot,
        decision_discussion=dict(
            snapshot.get("decision_discussion", {}), **{feedback["key"]: [*rows, feedback]}
        ),
    )


def _human_text(value: object) -> str:
    return re.sub(
        r"[ \t]*\[(claude|codex|opencode):[^\]\n]*\bbytes [^\]\n]*\]",
        "",
        str(value or ""),
        flags=re.I,
    )


def _review_text(value: object) -> str:
    text = _human_text(value)
    for _ in range(2):
        for prefix in ("Partial source coverage. ", "Some selected sources unavailable. "):
            text = text.removeprefix(prefix)
    return text


def _detail(snapshot: dict | None, state: State, width: int) -> list[str]:
    if state.help:
        return wrap(
            "Briefing help\n\n"
            + (
                ("Update problem: " + _human_text(snapshot["error"]) + "\n\n")
                if snapshot and snapshot.get("error")
                else ""
            )
            + "Left / Right or Tab / Shift+Tab: switch views\n"
            "1 Overview: background, position, direction and goal\n"
            "2 Review: recent progress and current outcome\n"
            "3 Decisions: ask, context, options and recommendation\n"
            "n / p: next / previous decision; opens Decisions from Overview or Review\n"
            "d: show / hide deeper background and source excerpts inline\n"
            "Background ready: the supplied context is available. Background needed: facts are missing.\n"
            "Preparing background: the saved request is waiting for richer context.\n"
            "Background unavailable: the last background lookup failed. Retry with:\n"
            "ses desk refresh HANDLE --retry-context\n"
            "Replace HANDLE with this Desk's handle or saved name.\n"
            "A resolved or removed decision stays visible until you browse away.\n"
            "Up / Down or PgUp / PgDn: scroll\n"
            "Home / End: top / bottom\n"
            "h / ? / Esc: return to briefing\nq: exit\n\n"
            "Up to date: no new session content was found since the last successful briefing check. "
            "Its age is time since the summary last changed, not time since the last check. "
            "An unchanged session needs no new summary.\n\n"
            "Supervisor has new content: a new briefing is needed.\n"
            "Updating briefing: the reviewer is checking new content.\n"
            "Saved: current source activity has not been verified.\n\n"
            "Source unavailable: a watched session file cannot be read.\n"
            "Update failed: showing the saved briefing while retrying.\n"
            "Reconnecting: the saved briefing cannot be reloaded right now.\n\n"
            "Observer stopped: reopen a live view or Desk conversation to restart it. "
            "Use ses desk status HANDLE or ses desk stop HANDLE to manage observation.\n\n"
            "In Decisions, e opens your text area; f prepares a clarification for the listed missing facts. c selects Mark as completed with an optional comment. u undoes completion. "
            "Tab selects Save note, Ask for clarification, Send answer, or Mark as completed. "
            "Enter inserts a new line while typing or activates the selected action. Esc keeps your draft. "
            "t opens Discussion with earlier cards and comments; r retries the failed background or clarification shown. "
            "Ask for clarification searches project files read-only and consults the bound Desk conversation only when needed. "
            "Send answer sends your exact answer and a discussion reference to that conversation. "
            "Enter on a saved answer moves to the next unanswered question. "
            "a switches between unanswered questions and your saved answers; b reviews the question. "
            "Discussion has earlier card versions behind v. "
            "Saved answers remain pending operationally until the project records an outcome."
            + "\n\nWatched sessions\n"
            + (
                "\n".join(
                    f"- {source.get('title') or ('Supervisor' if source.get('role', 'supervisor') == 'supervisor' else 'Team session')}"
                    for source in _watched_sources(snapshot)
                )
                or "No source details available."
            )
            + "\nOnly the listed sessions are read. Managers and workers are not included automatically."
            + (
                "\n\nSummary changed: " + _summary_time(snapshot)
                if snapshot and snapshot.get("observed_at")
                else ""
            )
            + (
                "\n\nRecent history only: earlier conversation may be missing."
                if _partial(snapshot)
                else ""
            )
            + "\n\nReview old or repeated requests:\n"
            "Review suggested flags old or repeated requests; it never closes them.\n"
            "ses desk decisions HANDLE lists review candidates and the revision.\n"
            "Dismiss explicitly with: ses desk dismiss HANDLE NUMBER --revision N --reason REASON\n"
            "ses desk history HANDLE shows preserved request history.\n",
            width,
        )
    if snapshot is None:
        return wrap("Waiting for the briefing. It will appear here automatically.", width)
    if state.tab == 1:
        overview = snapshot.get("overview") or {}
        if not overview:
            return wrap(
                "Preparing the project overview. Recent progress is available in 2 Review."
                + _decision_teasers(snapshot, state),
                width,
            )
        parts = []
        for key, title in (
            ("background", "Where we started"),
            ("current", "Where we are"),
            ("direction", "Where we're going"),
            ("goal", "What we're aiming for"),
        ):
            if parts:
                parts.append("")
            parts.extend(
                [
                    title,
                    _human_text(overview.get(key))
                    or "Not yet described in the watched conversations.",
                ]
            )
        return wrap("\n".join(parts) + _decision_teasers(snapshot, state), width)
    if state.tab == 2:
        return wrap(
            (_review_text(snapshot.get("review")) or "Preparing the first briefing…")
            + _decision_teasers(snapshot, state),
            width,
        )
    return _decision_body(snapshot, state, width)


def _decision_details(snapshot, key):
    details = (snapshot or {}).get("decision_details") or {}
    row = details.get(key) if isinstance(details, dict) else None
    return row if isinstance(row, dict) and row.get("schema_version") == 1 else None


def _decision_teasers(snapshot, state):
    pending = _pending(snapshot)
    if not pending:
        saved = len(_answered(snapshot))
        return (
            f"\n\nYour decisions · {saved} answers saved\n3 Decisions · a Review your answers"
            if saved
            else ""
        )
    index = next((i for i, row in enumerate(pending) if row.get("key") == state.selected_key), 0)
    shown = pending[index : index + 2]
    lines = ["", "", f"Your decisions · {len(pending)} open", "n/p: open Decisions"]
    for item in shown:
        details = _decision_details(snapshot, item.get("key"))
        summary = (details or {}).get("summary") or item.get("question")
        lines.append("- " + _clip(_human_text(summary), 180))
    return "\n".join(lines)


def _selected(snapshot, state):
    rows = (snapshot or {}).get("decisions", [])
    item = next((row for row in rows if row.get("key") == state.selected_key), None)
    if item is not None:
        return item, _decision_details(snapshot, state.selected_key), False
    if state.answered:
        item = next((row for row in _answered(snapshot) if row["key"] == state.selected_key), None)
        if item is not None:
            return item, _decision_details(snapshot, state.selected_key), False
    if state.retained and state.selected_key == state.retained["decision"].get("key"):
        return state.retained["decision"], state.retained.get("details"), True
    pending = _browse(snapshot, state)
    item = pending[0] if pending else None
    return item, _decision_details(snapshot, item.get("key")) if item else None, False


def _source_excerpt(entry):
    title = _human_text(entry.get("title")) or "Watched conversation"
    label = "Earlier context" if entry.get("context_only") else "Source conversation"
    stamp = entry.get("timestamp") or entry.get("observed_at")
    if stamp is not None:
        try:
            label += " · " + eastern_time(_timestamp(stamp))
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return [title, label, _human_text(entry.get("text")) or "Excerpt unavailable."]


def _deadline_text(value):
    text = _human_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})", text):
        try:
            return eastern_time(_timestamp(text))
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return text


def _decision_body(snapshot, state, width):
    pending = _pending(snapshot)
    selected, details, missing = _selected(snapshot, state)
    if state.editor:
        from .discussion_ui import editor_lines

        title = (details or {}).get("summary") or (selected or {}).get("question", "Decision")
        lines = editor_lines(state.editor, wrap, width, 24, title)
        if state.notice and state.notice[0] == state.editor.key:
            lines = wrap(state.notice[1], width) + [""] + lines
        return lines
    if selected and selected.get("key") in state.discussions:
        from .discussion_ui import lines as discussion_lines

        return [
            row
            for part in discussion_lines(
                snapshot,
                selected["key"],
                expanded=True,
                show_versions=selected["key"] in state.versions,
            )
            for row in wrap(part, width)
        ]
    feedback = _feedback(snapshot, state)
    if feedback:
        from .discussion_ui import receipt

        result = receipt(snapshot, feedback, wrap, width, reviewing=state.answered)
        if state.notice and state.notice[0] == state.selected_key:
            result = wrap(state.notice[1], width) + [""] + result
        return result
    if selected is None:
        if state.answered:
            return wrap(
                "Your saved answers\n\nThere are no answers to the current questions yet. Earlier comments remain in each question's Discussion.\n\na Return to unanswered questions",
                width,
            )
        if _answered(snapshot):
            return wrap(
                "No questions are waiting for your answer.\n\nYour answers are saved.\n\na Review your answers",
                width,
            )
        if not snapshot.get("observed_at"):
            qualification = "The first briefing is still being prepared."
        elif snapshot.get("error"):
            qualification = "The update failed; new decisions may be missing."
        elif _partial(snapshot):
            qualification = "Earlier conversation may contain other decisions."
        else:
            qualification = "Discuss anything else in your Desk conversation."
        return wrap("No open decisions. " + qualification, width)
    key = selected.get("key")
    index = next((i + 1 for i, row in enumerate(pending) if row.get("key") == key), None)
    from .discussion_ui import answer

    saved_answer = answer(snapshot, key)
    if saved_answer and not missing:
        position = (
            "Completed · Question"
            if saved_answer["kind"] == "completion"
            else "Your answer is saved · Question"
        )
    elif missing:
        position = "No longer listed · saved decision"
    elif selected.get("status") != "pending":
        position = "Resolved" if selected.get("status") == "resolved" else "Superseded"
    else:
        position = f"Decision {index}/{len(pending)}"
    ready = bool(details and details.get("readiness") == "ready")
    enrichment = (snapshot or {}).get("decision_enrichment") or {}
    attempt = enrichment.get(key) if isinstance(enrichment, dict) else None
    failed = isinstance(attempt, dict) and attempt.get("status") == "failed"
    background = (
        "Background ready"
        if ready
        else "Background needed"
        if details
        else "Background unavailable"
        if failed
        else "Preparing background"
    )
    active = not missing and selected.get("status") == "pending"
    parts = [styled(position + (" · " + background if active else ""), "metadata"), ""]
    if state.notice and state.notice[0] == key:
        parts[:0] = [styled(state.notice[1], "success"), ""]
    if len(_browse(snapshot, state)) > 1 or index is None and pending:
        parts[0] = styled(parts[0] + " · n/p", "metadata")

    def section(title, value):
        if value:
            parts.extend(["", styled(title, "heading"), _human_text(value)])

    from .discussion_ui import latest_clarification

    parts.extend(latest_clarification(snapshot, key))
    if failed:
        parts.extend(
            [
                "Background update unavailable. Showing saved context."
                if details
                else "Background lookup failed. Saved request shown below.",
                styled("r Retry background lookup", "action"),
                "",
            ]
        )
    flags = [
        flag
        for flag in snapshot.get("decision_review", {}).get(key, [])
        if flag != "Imported request; age unknown"
    ]
    if active and flags:
        parts.extend(["Review suggested: " + "; ".join(flags) + ". ? Help", ""])
    if details:
        parts.append(
            styled(
                _human_text(details.get("summary")) or _human_text(selected.get("question")),
                "question",
            )
        )
        missing_facts = details.get("missing_information") or []
        if missing_facts:
            section(
                "Missing information", "\n".join("- " + _human_text(fact) for fact in missing_facts)
            )
        elif not ready:
            section("Missing information", "The background or options are not complete yet.")
        section("Context", details.get("background") or "Background is still being prepared.")
        options = details.get("options") or []
        recommendation = details.get("recommendation") or {}
        recommended = next(
            (o for o in options if o.get("id") == recommendation.get("option_id")), None
        )
        reason = _human_text(recommendation.get("reason"))
        section(
            "Recommendation",
            ((_human_text(recommended.get("label")) + ". ") if recommended else "")
            + (reason or "No supported recommendation yet."),
        )
        if key in state.expanded:
            section("Why now", details.get("why_now"))
        if options:
            parts.extend(["", styled("Options and tradeoffs", "heading")])
            for number, option in enumerate(options, 1):
                parts.append(styled(f"{number}. " + _human_text(option.get("label")), "option"))
                if option.get("description"):
                    parts.append(_human_text(option["description"]))
                for values, label in (("benefits", "Benefits"), ("risks", "Risks")):
                    if option.get(values):
                        parts.append(
                            label + ": " + "; ".join(_human_text(v) for v in option[values])
                        )
                if option.get("consequences"):
                    parts.append("Consequence: " + _human_text(option["consequences"]))
                parts.append("")
        if key in state.expanded:
            section("If deferred", details.get("if_deferred"))
        section("Deadline", _deadline_text(details.get("deadline")))
    else:
        parts.append(
            styled(
                _human_text(selected.get("question")) or "A decision needs your attention.",
                "question",
            )
        )
        section("Saved context", selected.get("context"))
        section(
            "Background and options",
            "Background is unavailable. Options and missing facts could not be established."
            if failed
            else "Preparing the background, options and tradeoffs. Missing facts have not been identified yet.",
        )
        section("Earlier suggestion", selected.get("recommendation"))

    if key in state.expanded:
        parts.extend(["", "Background sources"])
        evidence = (details or {}).get("evidence") or []
        if evidence:
            for entry in evidence:
                parts.extend(["", *_source_excerpt(entry)])
        else:
            parts.append(
                "Source excerpts are unavailable. Retry the background lookup; ? Help has the command."
                if failed
                else "Source excerpts are still being prepared. The saved context above is available now."
            )
        if active:
            parts.extend(
                ["", styled("Send answer sends your answer to the Desk conversation.", "metadata")]
            )
    return [row for part in parts for row in wrap(part, width)]


def _partial(snapshot: dict | None) -> bool:
    if (snapshot or {}).get("observation", {}).get("partial"):
        return True
    # Also support snapshots carrying coverage qualifications in review text.
    review = str((snapshot or {}).get("review") or "")
    return bool(
        re.search(r"\bpartial (?:source )?(?:coverage|observation)|\btruncated\b", review, re.I)
    )


def _body(snapshot: dict | None, state: State, width: int) -> list[str]:
    details = _detail(snapshot, state, width)
    if not snapshot:
        return details
    if snapshot.get("error") and not state.help:
        return (
            wrap("Update failed. Showing the saved briefing; retrying. ? Details\n", width)
            + details
        )
    return details


def _body_height(height: int) -> int:
    return max(0, height - 4)


def normalize(state: State, snapshot: dict | None, width: int, height: int) -> State:
    """Preserve the viewed decision, including a resolved/removed selection."""
    tab = min(3, max(1, state.tab))
    keys = [item.get("key") for item in _browse(snapshot, state)]
    selected = state.selected_key
    if selected not in keys and not (
        tab == 3
        and (
            selected is not None
            and state.retained
            and state.retained["decision"].get("key") == selected
            or any(row.get("key") == selected for row in (snapshot or {}).get("decisions", []))
        )
    ):
        selected = keys[0] if keys else None
    state = replace(state, tab=tab, selected_key=selected)
    item, details, missing = _selected(snapshot, state)
    if item is not None and not missing:
        state = replace(state, retained=deepcopy(dict(decision=item, details=details)))
    limit = max(0, len(_body(snapshot, state, max(1, width))) - _body_height(height))
    return replace(state, scroll=min(max(0, state.scroll), limit))


def handle_key(
    state: State, key: str | int, snapshot: dict | None, width: int, height: int
) -> State:
    state = normalize(state, snapshot, width, height)
    if state.editor:
        from .discussion_ui import edit

        return replace(state, editor=edit(state.editor, key), scroll=0)
    if state.tab == 3 and not state.help:
        if key == "a":
            reviewing = not state.answered
            rows = _answered(snapshot) if reviewing else _pending(snapshot)
            return normalize(
                replace(
                    state,
                    answered=reviewing,
                    selected_key=rows[0]["key"] if rows else None,
                    feedback=None,
                    scroll=0,
                ),
                snapshot,
                width,
                height,
            )
        if key == "b" and state.selected_key:
            return normalize(
                replace(
                    state,
                    question_open=state.question_open | {state.selected_key},
                    discussions=state.discussions - {state.selected_key},
                    feedback=None,
                    scroll=0,
                ),
                snapshot,
                width,
                height,
            )
        if key == "v" and state.selected_key in state.discussions:
            return normalize(
                replace(state, versions=state.versions ^ {state.selected_key}, scroll=0),
                snapshot,
                width,
                height,
            )
        if key in ("\n", "\r", curses.KEY_ENTER) and _feedback(snapshot, state):
            row = _feedback(snapshot, state)
            if row["kind"] not in ("decision", "completion"):
                return normalize(
                    replace(
                        state,
                        question_open=state.question_open | {state.selected_key},
                        feedback=None,
                        scroll=0,
                    ),
                    snapshot,
                    width,
                    height,
                )
            rows = _pending(snapshot)
            return normalize(
                replace(
                    state,
                    answered=False,
                    selected_key=rows[0]["key"] if rows else None,
                    feedback=None,
                    scroll=0,
                ),
                snapshot,
                width,
                height,
            )
    if (
        key in ("e", "E", "c", "C", "f", "F")
        and state.tab == 3
        and state.selected_key
        and not state.help
    ):
        from .discussion import card_version

        selected, selected_details, missing = _selected(snapshot, state)
        if selected and not missing and selected.get("status") == "pending":
            draft = (snapshot or {}).get("decision_drafts", {}).get(state.selected_key, {})
            text = draft.get("text", "")
            if key in ("f", "F"):
                facts = (selected_details or {}).get("missing_information") or []
                if not facts:
                    return state
                text = text or "Please find and explain the missing context:\n" + "\n".join(
                    "- " + fact for fact in facts
                )
            return replace(
                state,
                editor=Editor(
                    state.selected_key,
                    card_version(snapshot, state.selected_key),
                    text,
                    len(text),
                    focus=4 if key in ("c", "C") else 2 if key in ("f", "F") else 0,
                ),
                scroll=0,
                notice=None,
                feedback=None,
            )
    if key in ("t", "T") and state.tab == 3 and state.selected_key and not state.help:
        return normalize(
            replace(state, discussions=state.discussions ^ {state.selected_key}, scroll=0),
            snapshot,
            width,
            height,
        )
    if key in ("q", "Q"):
        return replace(state, quit=True)
    if key in ("?", "h", "H") or key == "\x1b" and state.help:
        return normalize(replace(state, help=not state.help, scroll=0), snapshot, width, height)
    if key in ("1", "2", "3", "4"):
        return normalize(
            replace(state, tab=min(3, int(key)), help=False, scroll=0), snapshot, width, height
        )
    if key in (curses.KEY_RIGHT, "\t", curses.KEY_LEFT, curses.KEY_BTAB):
        step = 1 if key in (curses.KEY_RIGHT, "\t") else -1
        return normalize(
            replace(state, tab=(state.tab - 1 + step) % 3 + 1, help=False, scroll=0),
            snapshot,
            width,
            height,
        )
    if key in ("n", "p") and state.tab in (1, 2) and not state.help:
        questions = _pending(snapshot)
        if questions:
            item = questions[0] if key == "n" else questions[-1]
            return normalize(
                replace(state, tab=3, selected_key=item.get("key"), scroll=0),
                snapshot,
                width,
                height,
            )
    if key in ("n", "p") and state.tab == 3 and not state.help:
        keys = [item.get("key") for item in _browse(snapshot, state)]
        if keys:
            if state.selected_key in keys:
                index = (keys.index(state.selected_key) + (1 if key == "n" else -1)) % len(keys)
            else:
                index = 0 if key == "n" else len(keys) - 1
            state = replace(state, selected_key=keys[index], scroll=0, feedback=None)
    elif key in ("d", "D") and state.tab == 3 and not state.help and state.selected_key:
        expanded = state.expanded ^ {state.selected_key}
        state = replace(state, expanded=frozenset(expanded))
    elif key in ("wheel_down", "wheel_up"):
        state = replace(state, scroll=state.scroll + (3 if key == "wheel_down" else -3))
    elif key in (curses.KEY_DOWN, "j"):
        state = replace(state, scroll=state.scroll + 1)
    elif key in (curses.KEY_UP, "k"):
        state = replace(state, scroll=state.scroll - 1)
    elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
        delta = max(1, _body_height(height) - 1)
        state = replace(state, scroll=state.scroll + (delta if key == curses.KEY_NPAGE else -delta))
    elif key == curses.KEY_HOME:
        state = replace(state, scroll=0)
    elif key == curses.KEY_END:
        state = replace(state, scroll=len(_body(snapshot, state, width)))
    return normalize(state, snapshot, width, height)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _age(snapshot: dict | None, now: datetime) -> str:
    observation = (snapshot or {}).get("observation", {})
    stamp = observation.get("synthesized_at") or (snapshot or {}).get("observed_at")
    if stamp is None:
        return ""
    try:
        age = max(0, int((now - _timestamp(stamp)).total_seconds()))
    except (ValueError, TypeError, OverflowError, OSError):
        return ""
    return (
        "now"
        if age < 60
        else f"{age // 60}m"
        if age < 3600
        else f"{age // 3600}h"
        if age < 86400
        else f"{age // 86400}d"
    )


def _summary_time(snapshot: dict) -> str:
    try:
        return eastern_time(
            _timestamp(
                snapshot.get("observation", {}).get("synthesized_at") or snapshot["observed_at"]
            )
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return "unavailable"


def _observation_status(snapshot: dict | None, now: datetime | None) -> str:
    if not snapshot:
        return "Preparing"
    if snapshot.get("error"):
        status = "Update failed"
    elif not snapshot.get("observed_at"):
        status = "Preparing"
    else:
        status = {
            "current": "Up to date",
            "changed": "Supervisor has new content"
            if len(_watched_sources(snapshot)) == 1
            else "Watched sessions have new content",
            "unavailable": "Source unavailable",
        }.get(snapshot.get("_freshness"), "Saved")
    age = _age(snapshot, now if now is not None else _now())
    return status + (" · " + age if age else "")


def _clip(value: object, width: int) -> str:
    text = sanitize(value).replace("\n", " ")
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    result = ""
    used = 0
    for ch in text:
        size = cell_width(ch)
        if used + size > width - 1:
            break
        result += ch
        used += size
    return result + "…"


def _header(title: str, status: str, width: int) -> str:
    status = _clip(status, width)
    space = width - cell_width(status) - 2
    if space < 3:
        return status.rjust(max(0, width - cell_width(status) + len(status)))
    title = _clip(title, space)
    return title + " " * (width - cell_width(title) - cell_width(status)) + status


def _tabs(state: State, width: int) -> str:
    labels = ("Overview", "Review", "Decisions")

    def line(labels, compact=False, gap="  "):
        return gap.join(
            (f"[{n} {label}]" if state.tab == n else str(n) if compact else f"{n} {label}")
            for n, label in enumerate(labels, 1)
        )

    text = line(labels)
    if cell_width(text) > width:
        text = line(("Overview", "Review", "Decisions"), gap=" ")
    if cell_width(text) > width:
        text = line(labels, True)
    return _clip(text, width)


def render(
    snapshot: dict | None,
    state: State,
    width: int,
    height: int,
    *,
    now: datetime | None = None,
    read_error: str | None = None,
    updating: bool = False,
) -> Frame:
    """Pure bounded layout; source checking happens outside rendering."""
    if width <= 0 or height <= 0:
        return Frame(())
    state = normalize(state, snapshot, width, height)
    status = _observation_status(snapshot, now if now is not None else _now())
    if read_error:
        label = "Observer stopped" if read_error.startswith("Observer stopped;") else "Reconnecting"
        status = label + (" · " + _age(snapshot, now or _now()) if snapshot else "")
    elif updating and (snapshot or {}).get("_freshness") == "changed":
        status = "Updating briefing" + (" · " + _age(snapshot, now or _now()) if snapshot else "")
    title = str((snapshot or {}).get("name") or "Briefing")
    header = _header(title, status, width)
    tabs = _clip("Help · h / ? / Esc to return", width) if state.help else _tabs(state, width)
    if height < 6:
        return Frame(tuple([header, tabs, _clip("? Help · q Exit", width)][:height]))
    body = _body(snapshot, state, width)
    if state.editor:
        from .discussion_ui import editor_lines

        selected, details, _ = _selected(snapshot, state)
        title = (details or {}).get("summary") or (selected or {}).get("question", "Decision")
        body = editor_lines(state.editor, wrap, width, height, title)
        if state.notice and state.notice[0] == state.editor.key:
            body = wrap(state.notice[1], width)[:2] + body
    capacity = _body_height(height)
    offset = min(state.scroll, max(0, len(body) - capacity))
    visible = body[offset : offset + capacity]
    visible += [""] * (capacity - len(visible))
    position = (
        f"{offset + 1}-{min(len(body), offset + capacity)}/{len(body)}"
        if len(body) > capacity
        else ""
    )
    controls = "Tab Actions · Esc Keep draft" if state.editor else "? Help · q Exit"
    extra = []
    if not state.help and not state.editor and state.tab == 3 and state.selected_key:
        if state.selected_key in state.discussions:
            extra.extend(["t Back", "v Versions"])
        elif _feedback(snapshot, state):
            extra.extend(["Enter Next", "b Question"])
        else:
            extra.append("d Sources" if state.selected_key not in state.expanded else "d Hide")
        selected, _, missing = _selected(snapshot, state)
        if selected and not missing and selected.get("status") == "pending":
            extra.append("e Write")
        if state.selected_key not in state.discussions:
            extra.append("t Discussion")
    available = width - (len(position) + 2 if position else 0)
    if position:
        extra.append("↑↓ Scroll")
    if (
        not state.help
        and not state.editor
        and state.tab == 3
        and (_answered(snapshot) or state.answered)
    ):
        extra.append("a Questions" if state.answered else "a Your answers")
    if not state.help and not state.editor and _browse(snapshot, state):
        if state.tab in (1, 2):
            extra.append("n Decisions")
        elif len(_browse(snapshot, state)) > 1:
            extra.append("n/p Decisions")
    if not state.help and not state.editor:
        extra.append("←→ Views")
    for hint in extra:
        if cell_width(controls + " · " + hint) <= available:
            controls += " · " + hint
    footer = _header(controls, position, width) if position else _clip(controls, width)
    action = ""
    if state.tab == 3 and not state.help and not state.editor:
        feedback = _feedback(snapshot, state)
        action_selected, action_details, action_missing = _selected(snapshot, state)
        action = (
            "[e Respond]   [c Mark completed]"
            if action_selected
            and action_selected.get("status") == "pending"
            and not action_missing
            and not feedback
            and state.selected_key not in state.discussions
            else ""
        )
        if action and (action_details or {}).get("missing_information"):
            action = "[f Find context]   [e Respond]"
        if not action and not feedback and state.selected_key not in state.discussions:
            from .discussion import completed

            if state.selected_key in completed(snapshot or {}):
                action = "[u Undo completion]  [t Discussion]"
    rows = (
        [styled(header, "header"), styled(tabs, "tabs"), styled(_clip(action, width), "action")]
        + visible
        + [styled(footer, "hint")]
    )
    return Frame(tuple(rows), tuple(getattr(row, "role", "body") for row in rows))


def _watched_sources(snapshot):
    snapshot = snapshot or {}
    sources = list(snapshot.get("sources", []))
    known = {(s.get("tool"), s.get("session_id")) for s in sources}
    for source in snapshot.get("observation", {}).get("sources", []):
        binding = snapshot.get("agents", {}).get(source.get("tool"), {})
        if (
            source.get("role") == "desk_conversation"
            and source.get("session_id") == binding.get("session_id")
            and source.get("home") == binding.get("home")
            and (source.get("tool"), source.get("session_id")) not in known
        ):
            sources.append(dict(source, title="Desk conversation"))
            known.add((source.get("tool"), source.get("session_id")))
    return sources


def _source_freshness(snapshot: dict) -> str:
    """Check at most 36 file stats; no transcript reads, model calls or writes."""
    sources = _watched_sources(snapshot)
    if not sources or len(sources) > 36:
        return "unknown"
    previous = {
        (s.get("tool"), s.get("session_id"), s.get("storage")): s
        for s in snapshot.get("observation", {}).get("sources", [])
    }
    changed = False
    for source in sources:
        try:
            stat = Path(source["storage"]).stat()
        except (OSError, ValueError, KeyError, TypeError):
            return "unavailable"
        saved = previous.get(
            (source.get("tool"), source.get("session_id"), source.get("storage")), {}
        )
        fingerprint = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        if saved.get("status") != "available" or saved.get("fingerprint") != fingerprint:
            changed = True
    return "changed" if changed else "current"


def read_snapshot(
    store: SnapshotStore, name: str, previous: dict | None
) -> tuple[dict | None, str | None]:
    """A temporary store failure never discards the last good briefing."""
    try:
        snapshot = store.get(name)
        if not isinstance(snapshot, dict):
            return previous, "Desk unavailable"
        return dict(snapshot, _freshness=_source_freshness(snapshot)), None
    except Exception:
        return previous, "Desk unavailable"


def run(store: SnapshotStore, name: str, *, activity: dict | None = None) -> None:
    """Poll saved state at most once per second; draw only changed frames."""

    from .view_polling import Poller

    poller = Poller(store, name, read_snapshot, activity).start()

    def loop(screen):
        from .presentation import attributes, palette

        colors = palette()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.mousemask(getattr(curses, "ALL_MOUSE_EVENTS", 0))
            curses.mouseinterval(0)
        except (curses.error, AttributeError):
            pass
        screen.timeout(20)
        state, snapshot, read_error = State(), None, None
        previous = None
        previous_input = None
        while not state.quit:
            snapshot, read_error, busy = poller.latest()
            snapshot = _with_saved_feedback(snapshot, state.feedback)
            poller.refresh(state.discussions & state.versions)
            height, width = screen.getmaxyx()
            # Leave the final column free to avoid lower-right curses errors.
            width = max(1, width - 1)
            now = _now()
            current_input = (
                height,
                width,
                state,
                snapshot,
                read_error,
                _observation_status(snapshot, now),
                busy,
            )
            if current_input != previous_input:
                state = normalize(state, snapshot, width, height)
                current_input = current_input[:2] + (state,) + current_input[3:]
                frame = render(
                    snapshot,
                    state,
                    width,
                    height,
                    now=now,
                    read_error=read_error,
                    updating=busy,
                )
                signature = (height, width, frame)
                if signature != previous:
                    screen.erase()
                    for row, line in enumerate(frame.lines):
                        try:
                            screen.addstr(
                                row,
                                0,
                                line,
                                attributes(frame.roles[row] if frame.roles else "body", colors),
                            )
                        except curses.error:
                            pass  # A terminal resize may race this paint.
                    screen.refresh()
                    previous = signature
                previous_input = current_input
            try:
                key = screen.get_wch()
                if key == curses.KEY_MOUSE:
                    _, _, _, _, buttons = curses.getmouse()
                    if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
                        key = "wheel_up"
                    elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                        key = "wheel_down"
            except curses.error:
                continue
            previous_editor = state.editor
            if key == "u" and state.tab == 3 and not state.editor and not state.help:
                from .discussion import completed

                entry = _feedback(snapshot, state) or completed(snapshot or {}).get(
                    state.selected_key
                )
                if entry and entry["kind"] == "completion":
                    from .discussion import undo_completion

                    try:
                        undo_completion(store, name, entry["id"])
                        state = replace(
                            state,
                            feedback=None,
                            notice=(state.selected_key, "Completion undone"),
                            answered=False,
                            question_open=state.question_open | {state.selected_key},
                            scroll=0,
                        )
                        poller.refresh()
                    except (OSError, RuntimeError, ValueError):
                        state = replace(
                            state,
                            notice=(state.selected_key, "Could not undo completion. Try again."),
                        )
                continue
            if key == "r" and state.tab == 3 and not state.editor and not state.help:
                from .discussion import retry

                rows = (snapshot or {}).get("decision_discussion", {}).get(state.selected_key, [])
                failed = next(
                    (row for row in reversed(rows) if row["kind"] == "clarification"), None
                )
                if (
                    failed
                    and failed["status"] == "failed"
                    and (
                        state.selected_key in state.discussions
                        or (_feedback(snapshot, state) or {}).get("kind") == "clarification"
                    )
                ):
                    try:
                        retry(store, name, failed["id"])
                        poller.refresh()
                    except (OSError, RuntimeError, ValueError):
                        state = replace(
                            state,
                            notice=(
                                state.selected_key,
                                "Retry unavailable; your question is saved.",
                            ),
                        )
                    continue
                attempt = (
                    (snapshot or {}).get("decision_enrichment", {}).get(state.selected_key, {})
                )
                if attempt.get("status") == "failed":
                    from .decisions import core_fingerprint

                    selected, _, _ = _selected(snapshot, state)
                    try:
                        store.retry_decision_background(
                            name, state.selected_key, core_fingerprint(selected)
                        )
                        poller.refresh()
                        state = replace(
                            state, notice=(state.selected_key, "Background lookup queued.")
                        )
                    except (OSError, RuntimeError, ValueError):
                        state = replace(
                            state, notice=(state.selected_key, "Could not retry. Try again.")
                        )
                    continue
            state = handle_key(state, key, snapshot, width, height)
            editor = state.editor or previous_editor
            if editor:
                from .discussion import save_draft, submit

                try:
                    save_draft(store, name, editor.key, editor.text, editor.version)
                    if state.editor and state.editor.request:
                        entry = submit(
                            store, name, editor.key, editor.request, editor.text, editor.version
                        )
                        message = {
                            "note": "Note saved",
                            "clarification": "Question saved",
                            "decision": "Answer saved",
                            "completion": "Marked as completed",
                        }[entry["kind"]]
                        state = replace(
                            state,
                            editor=None,
                            notice=None,
                            feedback=entry,
                            question_open=state.question_open - {editor.key},
                            discussions=state.discussions - {editor.key},
                            scroll=0,
                        )
                        from .runtime import ensure_observer

                        if entry["kind"] not in ("note", "completion"):
                            try:
                                ensure_observer(store, name)
                            except (OSError, RuntimeError, ValueError):
                                state = replace(
                                    state,
                                    notice=(
                                        editor.key,
                                        message
                                        + "; reopen the view to start background processing",
                                    ),
                                )
                    poller.refresh()
                except (OSError, RuntimeError, ValueError) as error:
                    message = (
                        str(error)
                        if isinstance(error, (ValueError, RuntimeError))
                        else "Could not save. Keep this view open and try again."
                    )
                    if message.startswith("Decision changed"):
                        message = "Question changed. Esc to review. Your draft is saved."
                    elif "clarification is already in progress" in message:
                        message = "A reply is already in progress. Save a note or wait."
                    state = replace(
                        state,
                        editor=replace(editor, request=None),
                        notice=(editor.key, message),
                        scroll=0,
                    )

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    finally:
        poller.close()
