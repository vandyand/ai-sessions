"""Read-only Project Desk terminal view; no observer or messaging dependencies.

Rendering is deterministic with explicit ``now``; normalization and keys are pure. ``run`` owns polling and curses; ``read_snapshot`` handles store failures. Decision keys remain in state, never in the frame.
"""

from __future__ import annotations

import curses
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

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


def _detail(snapshot: dict | None, state: State, width: int) -> list[str]:
    if state.help:
        return wrap(
            "Briefing help\n\n"
            "Left / Right or Tab / Shift+Tab: switch views\n"
            "1 Review: progress and current outcome\n"
            "2 Needs you: questions and their context\n"
            "3 Next action: suggested response\n"
            "n / p: next / previous question; opens Needs you from Review\n"
            "Up / Down or PgUp / PgDn: scroll\n"
            "Home / End: top / bottom\n"
            "h / ? / Esc: return to briefing\nq: exit\n\n"
            "Up to date: the briefing includes all content from the latest checked session files. "
            "Its age is time since the summary last changed, not time since the last check. "
            "An unchanged session needs no new summary.\n\n"
            "Supervisor has new content: a new briefing is needed.\n"
            "Updating briefing: the reviewer is checking new content.\n"
            "Saved: current source activity has not been verified.\n\n"
            "Discuss questions and give answers in your Desk conversation."
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
            ),
            width,
        )
    if snapshot is None:
        return wrap("Waiting for the briefing. It will appear here automatically.", width)
    if state.tab == 1:
        return wrap(
            _human_text(snapshot.get("review")).removeprefix("Partial source coverage. ")
            or "Preparing the first briefing…",
            width,
        )
    pending = _pending(snapshot)
    if not pending:
        if not snapshot.get("observed_at"):
            qualification = "The first briefing is still being prepared."
        elif snapshot.get("error"):
            qualification = "The update failed; new questions may be missing."
        elif _partial(snapshot):
            qualification = "Earlier conversation may contain other questions."
        else:
            qualification = "Discuss anything else in your Desk conversation."
        return wrap("No open questions. " + qualification, width)
    selected = next((d for d in pending if d.get("key") == state.selected_key), pending[0])
    index = pending.index(selected) + 1
    parts = [
        f"Question {index} of {len(pending)}" + (" · n/p to browse" if len(pending) > 1 else ""),
        "",
        selected.get("question") or "A decision needs your attention.",
    ]
    if state.tab == 2 and selected.get("context"):
        parts.extend(["", "Why it matters", str(selected["context"])])
    parts.extend(
        [
            "",
            "Suggested response\n"
            + str(
                selected.get("recommendation") or "Discuss this question in your Desk conversation."
            ),
        ]
    )
    if state.tab == 3:
        parts.extend(["", "2 Needs you: read the context."])
    # Evidence is deliberately not dumped: it can contain internal source IDs.
    return wrap(_human_text("\n".join(parts)), width)


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
    """Retain stable selection through reorder; fall back when it disappears."""
    keys = [item.get("key") for item in _pending(snapshot)]
    selected = state.selected_key if state.selected_key in keys else (keys[0] if keys else None)
    state = replace(state, selected_key=selected)
    limit = max(0, len(_body(snapshot, state, max(1, width))) - _body_height(height))
    return replace(state, scroll=min(max(0, state.scroll), limit))


def handle_key(
    state: State, key: str | int, snapshot: dict | None, width: int, height: int
) -> State:
    state = normalize(state, snapshot, width, height)
    if key in ("q", "Q"):
        return replace(state, quit=True)
    if key in ("?", "h", "H") or key == "\x1b" and state.help:
        return normalize(replace(state, help=not state.help, scroll=0), snapshot, width, height)
    if key in ("1", "2", "3"):
        return normalize(
            replace(state, tab=int(key), help=False, scroll=0), snapshot, width, height
        )
    if key in (curses.KEY_RIGHT, "\t", curses.KEY_LEFT, curses.KEY_BTAB):
        step = 1 if key in (curses.KEY_RIGHT, "\t") else -1
        return normalize(
            replace(state, tab=(state.tab - 1 + step) % 3 + 1, help=False, scroll=0),
            snapshot,
            width,
            height,
        )
    if key in ("n", "p") and state.tab == 1 and not state.help:
        questions = _pending(snapshot)
        if questions:
            item = questions[0] if key == "n" else questions[-1]
            return normalize(
                replace(state, tab=2, selected_key=item.get("key"), scroll=0),
                snapshot,
                width,
                height,
            )
    if key in ("n", "p") and state.tab != 1:
        keys = [item.get("key") for item in _pending(snapshot)]
        if keys:
            index = keys.index(state.selected_key)
            index = (index + (1 if key == "n" else -1)) % len(keys)
            state = replace(state, selected_key=keys[index], scroll=0)
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
    labels = ("Review", "Needs you", "Next action")

    def line(compact=False):
        return "  ".join(
            (f"[{n} {label}]" if state.tab == n else str(n) if compact else f"{n} {label}")
            for n, label in enumerate(labels, 1)
        )

    text = line()
    if cell_width(text) > width:
        text = line(True)
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
        status = "Reconnecting" + (" · " + _age(snapshot, now or _now()) if snapshot else "")
    elif updating and (snapshot or {}).get("_freshness") == "changed":
        status = "Updating briefing" + (" · " + _age(snapshot, now or _now()) if snapshot else "")
    title = str((snapshot or {}).get("name") or "Briefing")
    header = _header(title, status, width)
    tabs = _clip("Help · ? or Esc to return", width) if state.help else _tabs(state, width)
    if height < 6:
        return Frame(tuple([header, tabs, _clip("? Help · q Exit", width)][:height]))
    body = _body(snapshot, state, width)
    capacity = _body_height(height)
    offset = min(state.scroll, max(0, len(body) - capacity))
    visible = body[offset : offset + capacity]
    visible += [""] * (capacity - len(visible))
    position = (
        f"{offset + 1}-{min(len(body), offset + capacity)}/{len(body)}"
        if len(body) > capacity
        else ""
    )
    controls = "? Help · q Exit"
    extra = []
    available = width - (len(position) + 2 if position else 0)
    if position:
        extra.append("↑↓ Scroll" if width >= 55 else "↑↓")
    if not state.help and _pending(snapshot):
        if state.tab == 1:
            extra.append("n Questions")
        elif len(_pending(snapshot)) > 1:
            extra.append("n/p Questions")
    if not state.help:
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
            state = handle_key(state, key, snapshot, width, height)

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
