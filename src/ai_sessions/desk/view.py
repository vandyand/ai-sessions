"""Project Desk terminal view with durable decision input and pure rendering.

Rendering is deterministic with explicit ``now``; normalization and keys are pure. ``run`` owns polling and curses; ``read_snapshot`` handles store failures. Decision keys remain in state, never in the frame.
"""

from __future__ import annotations

import curses
import re
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .discussion_ui import Editor
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


@dataclass(frozen=True)
class Frame:
    lines: tuple[str, ...]


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
        for ch in paragraph:
            if cell_width(ch) > width:
                ch = "?"
            if cell_width(line + ch) > width:
                split = line.rfind(" ")
                if split > 0:
                    result.append(line[:split])
                    line = line[split + 1 :]
                else:
                    result.append(line)
                    line = ""
                # The remainder plus a wide character can still overflow.
                if cell_width(line + ch) > width:
                    result.append(line)
                    line = ""
            line += ch
        result.append(line)
    return result


def _pending(snapshot: dict | None) -> list[dict]:
    return [
        item
        for item in (snapshot or {}).get("decisions", [])
        if isinstance(item, dict) and item.get("status") == "pending"
    ]


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
            "Left / Right or Tab / Shift+Tab: switch views\n"
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
            "In Decisions, e opens your text area; Tab selects Save note, Ask for clarification, or Submit decision. "
            "Enter inserts a new line while typing or activates the selected action. Esc keeps your draft. "
            "t opens Discussion with earlier cards and comments; r retries a failed clarification. "
            "Ask for clarification searches project files read-only and consults the bound Desk conversation only when needed. "
            "Submit decision sends your exact answer and a discussion reference to that conversation. "
            "Submitted answers do not automatically close a decision or mean the work was performed."
            + "\n\nWatched sessions\n"
            + (
                "\n".join(
                    f"- {source.get('title') or ('Supervisor' if source.get('role', 'supervisor') == 'supervisor' else 'Team session')}"
                    for source in (snapshot or {}).get("sources", [])
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
            + (
                "\n\nUpdate problem: " + _human_text(snapshot["error"])
                if snapshot and snapshot.get("error")
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
        return ""
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
    if state.retained and state.selected_key == state.retained["decision"].get("key"):
        return state.retained["decision"], state.retained.get("details"), True
    pending = _pending(snapshot)
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
    if selected is None:
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
    if missing:
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
    parts = [
        position + (" · " + background if active else ""),
        "e Write · t Discussion" if active else "t Discussion",
        "",
    ]
    if len(pending) > 1 or index is None and pending:
        parts[0] += " · n/p"
    if state.notice and state.notice[0] == key:
        parts.extend([state.notice[1], ""])
    discussion_rows = snapshot.get("decision_discussion", {}).get(key, [])
    if discussion_rows:
        from .discussion_ui import status as discussion_status

        last_question = next(
            (r for r in reversed(discussion_rows) if r["kind"] == "clarification"), None
        )
        status_row = (
            last_question
            if last_question
            and last_question["status"] in ("queued", "working", "waiting", "failed")
            else discussion_rows[-1]
        )
        parts.extend([discussion_status(status_row, bool(snapshot.get("agents"))), ""])

    def section(title, value):
        if value:
            parts.extend(["", title, _human_text(value)])

    if failed:
        parts.extend(
            [
                "Background update unavailable. Showing saved context."
                if details
                else "Background lookup failed. Saved request shown below.",
                "Retry the background lookup; ? Help has the command.",
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
        parts.append(_human_text(details.get("summary")) or _human_text(selected.get("question")))
        section("Context", details.get("background") or "Background is still being prepared.")
        section("Why now", details.get("why_now"))
        options = details.get("options") or []
        if options:
            parts.extend(["", "Options and tradeoffs"])
            for number, option in enumerate(options, 1):
                parts.append(f"{number}. " + _human_text(option.get("label")))
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
        missing_facts = details.get("missing_information") or []
        if missing_facts:
            section(
                "Missing information", "\n".join("- " + _human_text(fact) for fact in missing_facts)
            )
        elif not ready:
            section("Missing information", "The background or options are not complete yet.")
        section("If deferred", details.get("if_deferred"))
        section("Deadline", _deadline_text(details.get("deadline")))
    else:
        parts.append(_human_text(selected.get("question")) or "A decision needs your attention.")
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
    parts.extend(
        [
            "",
            "Submit decision sends your answer to the Desk conversation."
            if active
            else "Saved context for the previously viewed decision.",
        ]
    )
    from .discussion_ui import lines as discussion_lines

    parts.extend(discussion_lines(snapshot, key, key in state.discussions))
    return wrap("\n".join(parts), width)


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
    keys = [item.get("key") for item in _pending(snapshot)]
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
    if key in ("e", "E") and state.tab == 3 and state.selected_key and not state.help:
        from .discussion import card_version

        selected, _, missing = _selected(snapshot, state)
        if selected and not missing and selected.get("status") == "pending":
            draft = (snapshot or {}).get("decision_drafts", {}).get(state.selected_key, {})
            text = draft.get("text", "")
            return replace(
                state,
                editor=Editor(
                    state.selected_key, card_version(snapshot, state.selected_key), text, len(text)
                ),
                scroll=0,
                notice=None,
            )
    if key in ("t", "T") and state.tab == 3 and state.selected_key and not state.help:
        return normalize(
            replace(state, discussions=state.discussions ^ {state.selected_key}),
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
        keys = [item.get("key") for item in _pending(snapshot)]
        if keys:
            if state.selected_key in keys:
                index = (keys.index(state.selected_key) + (1 if key == "n" else -1)) % len(keys)
            else:
                index = 0 if key == "n" else len(keys) - 1
            state = replace(state, selected_key=keys[index], scroll=0)
    elif key in ("d", "D") and state.tab == 3 and not state.help and state.selected_key:
        expanded = state.expanded ^ {state.selected_key}
        state = replace(state, expanded=frozenset(expanded))
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
            if len(snapshot.get("sources", [])) == 1
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
    for ch in text:
        if cell_width(result + ch) > width - 1:
            break
        result += ch
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
    controls = "Tab Select · Esc Back" if state.editor else "? Help · q Exit"
    extra = []
    if not state.help and not state.editor and state.tab == 3 and state.selected_key:
        extra.append("d Sources" if state.selected_key not in state.expanded else "d Hide")
        selected, _, missing = _selected(snapshot, state)
        if selected and not missing and selected.get("status") == "pending":
            extra.append("e Write")
        extra.append("t Discussion")
    available = width - (len(position) + 2 if position else 0)
    if position:
        extra.append("↑↓ Scroll")
    if not state.help and not state.editor and _pending(snapshot):
        if state.tab in (1, 2):
            extra.append("n Decisions")
        elif len(_pending(snapshot)) > 1:
            extra.append("n/p Decisions")
    if not state.help and not state.editor:
        extra.append("←→ Views")
    for hint in extra:
        if cell_width(controls + " · " + hint) <= available:
            controls += " · " + hint
    footer = _header(controls, position, width) if position else _clip(controls, width)
    return Frame(tuple([header, tabs, ""] + visible + [footer]))


def _source_freshness(snapshot: dict) -> str:
    """Check at most 32 file stats; no transcript reads, model calls or writes."""
    sources = snapshot.get("sources", [])
    if not sources or len(sources) > 32:
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

    def loop(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(100)
        state, snapshot, read_error = State(), None, None
        next_poll = 0.0
        previous = None
        previous_input = None
        while not state.quit:
            tick = time.monotonic()
            if tick >= next_poll:
                snapshot, read_error = read_snapshot(store, name, snapshot)
                if snapshot and state.discussions:
                    from .discussion import versions

                    try:
                        snapshot["_discussion_versions"] = {
                            key: versions(store, name, key) for key in state.discussions
                        }
                    except (OSError, RuntimeError, ValueError):
                        read_error = (
                            read_error or "Earlier versions could not be loaded; retry shortly."
                        )
                if activity and activity.get("error"):
                    read_error = read_error or activity.get("error")
                next_poll = tick + 1.0
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
                bool(activity and activity.get("busy")),
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
                    updating=bool(activity and activity.get("busy")),
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
                                curses.A_BOLD
                                if row < 2
                                else curses.A_DIM
                                if row == len(frame.lines) - 1
                                else 0,
                            )
                        except curses.error:
                            pass  # A terminal resize may race this paint.
                    screen.refresh()
                    previous = signature
                previous_input = current_input
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            previous_editor = state.editor
            if key == "r" and state.tab == 3 and not state.editor and not state.help:
                from .discussion import retry

                rows = (snapshot or {}).get("decision_discussion", {}).get(state.selected_key, [])
                failed = next(
                    (row for row in reversed(rows) if row["kind"] == "clarification"), None
                )
                if failed and failed["status"] == "failed":
                    try:
                        retry(store, name, failed["id"])
                        next_poll = 0
                    except (OSError, RuntimeError, ValueError):
                        state = replace(
                            state,
                            notice=(
                                state.selected_key,
                                "Retry unavailable; your question is saved.",
                            ),
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
                        }[entry["kind"]]
                        state = replace(
                            state,
                            editor=None,
                            notice=(editor.key, message + " · n Next decision"),
                            scroll=0,
                        )
                        from .runtime import ensure_observer

                        if entry["kind"] != "note":
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
                    next_poll = 0
                except (OSError, RuntimeError, ValueError) as error:
                    message = (
                        str(error)
                        if isinstance(error, (ValueError, RuntimeError))
                        else "Could not save. Keep this view open and try again."
                    )
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
