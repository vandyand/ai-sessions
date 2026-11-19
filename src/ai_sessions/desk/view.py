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
from typing import Protocol


class SnapshotStore(Protocol):
    def get(self, name: str) -> dict: ...


@dataclass(frozen=True)
class State:
    tab: int = 1
    selected_key: str | None = None
    scroll: int = 0
    quit: bool = False


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
        r"\[(claude|codex|opencode):[^\]\n]*\bbytes [^\]\n]*\]",
        lambda match: f"[{match[1].capitalize()} source observation]",
        str(value or ""),
        flags=re.I,
    )


def _detail(snapshot: dict | None, state: State, width: int) -> list[str]:
    if snapshot is None:
        return wrap("Desk unavailable. Waiting for a saved briefing.", width)
    if state.tab == 1:
        return wrap(
            _human_text(snapshot.get("review")).removeprefix("Partial source coverage. ")
            or "No review yet. Waiting for the first observation.",
            width,
        )
    pending = _pending(snapshot)
    if not pending:
        if not snapshot.get("observed_at"):
            qualification = "Waiting for the first observation."
        elif snapshot.get("error"):
            qualification = "The last observation failed; requests may be missing."
        elif _partial(snapshot):
            qualification = "Coverage is partial; other requests may be missing."
        else:
            qualification = "Check the observation status above for freshness."
        return wrap("No pending requests recorded. " + qualification, width)
    selected = next((d for d in pending if d.get("key") == state.selected_key), pending[0])
    index = pending.index(selected) + 1
    parts = [
        f"Request {index} of {len(pending)}",
        "",
        selected.get("question") or "A decision needs your attention.",
    ]
    if state.tab == 2 and selected.get("context"):
        parts.extend(["", "Context: " + str(selected["context"])])
    parts.extend(
        [
            "",
            "Suggested next action: "
            + str(
                selected.get("recommendation")
                or "Discuss this question in your project conversation."
            ),
        ]
    )
    if state.tab == 3:
        parts.extend(["", "Press 2 for context in Needs you."])
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
    if snapshot.get("error"):
        return wrap("Observation error: " + _human_text(snapshot["error"]) + "\n", width) + details
    return details


def _body_height(height: int) -> int:
    return max(0, height - 5)


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
    if key in ("1", "2", "3"):
        return normalize(replace(state, tab=int(key), scroll=0), snapshot, width, height)
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


def _observation_status(snapshot: dict | None, now: datetime | None) -> str:
    observed = (snapshot or {}).get("observed_at")
    if not observed:
        return "Last observation: not observed yet"
    try:
        stamp = _timestamp(observed)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc)
        if now is None:
            return f"Last observation: {stamp:%Y-%m-%d %H:%M} UTC (age unavailable)"
        age = max(0, int((now - stamp).total_seconds()))
        elapsed = "less than 1 min" if age < 60 else f"{age // 60} min"
        freshness = "Stale" if age >= 300 or snapshot.get("error") else "Observed"
        return f"{freshness} - last observation {stamp:%Y-%m-%d %H:%M} UTC ({elapsed} ago)"
    except (ValueError, TypeError, OverflowError, OSError):
        return "Last observation: time unavailable"


def render(
    snapshot: dict | None,
    state: State,
    width: int,
    height: int,
    *,
    now: datetime | None = None,
    read_error: str | None = None,
) -> Frame:
    """Create a bounded frame. Status rows stay visible while the body scrolls."""
    if width <= 0 or height <= 0:
        return Frame(())
    state = normalize(state, snapshot, width, height)
    title = "Project Desk" + (" - " + str(snapshot.get("name", "")) if snapshot else "")
    tabs = "  ".join(
        f"{'[' if state.tab == n else ''}{n} {label}{']' if state.tab == n else ''}"
        for n, label in enumerate(("Review", "Needs you", "Next action"), 1)
    )
    status = _observation_status(snapshot, now if now is not None else _now())
    error = read_error or (snapshot or {}).get("error")
    notice = "Observation failed - details below." if error else ""
    if read_error:
        status = "Stale - " + status
        notice = "Desk unavailable; showing last saved briefing. Retrying."
    if _partial(snapshot):
        notice = "Partial coverage - see Review." + (" " + notice if notice else "")
    # Fixed rows reserve body space even when there is no error.
    headers = [title, tabs, status, notice]
    if height < 6:
        compact = [title, "1 Review / 2 Needs you / 3 Next action", notice or status, "q Exit"]
        return Frame(tuple(line for item in compact for line in wrap(item, width))[:height])
    # Keep detailed errors scrollable without repeating header metadata.
    header_lines = [wrap(item, width)[0] for item in headers]
    body = _body(snapshot, state, width)
    limit = max(0, len(body) - _body_height(height))
    offset = min(state.scroll, limit)
    visible = body[offset : offset + _body_height(height)]
    visible += [""] * (_body_height(height) - len(visible))
    footer = "1/2/3 Tabs | n/p Request | Arrows/PgUp/PgDn Scroll | q Exit"
    return Frame(tuple(header_lines + visible + [wrap(footer, width)[0]]))


def read_snapshot(
    store: SnapshotStore, name: str, previous: dict | None
) -> tuple[dict | None, str | None]:
    """A temporary store failure never discards the last good briefing."""
    try:
        snapshot = store.get(name)
        if not isinstance(snapshot, dict):
            return previous, "Desk unavailable"
        return snapshot, None
    except Exception:
        return previous, "Desk unavailable"


def run(store: SnapshotStore, name: str) -> None:
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
            )
            if current_input != previous_input:
                state = normalize(state, snapshot, width, height)
                current_input = current_input[:2] + (state,) + current_input[3:]
                frame = render(snapshot, state, width, height, now=now, read_error=read_error)
                signature = (height, width, frame)
                if signature != previous:
                    screen.erase()
                    for row, line in enumerate(frame.lines):
                        try:
                            screen.addstr(row, 0, line)
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
