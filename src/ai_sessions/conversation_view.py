"""On-demand native conversation reader on the browser's existing curses screen.

``run(screen, session)`` returns to its caller without starting any agent. The
parent menu binds ``m messages``. Readers currently materialize a full transcript;
the preview retains at most MAX_MESSAGES complete text messages afterwards.
Compressed input is streamed to a size-limited private temporary file, never
restored alongside the original. No retention/opened metadata is updated.
"""

from __future__ import annotations

import curses
import tempfile
import unicodedata
from collections import deque
from pathlib import Path

from . import conversion
from .capabilities import Unsupported
from .compression import is_compressed_path, open_for_scan, plain_path, resolve_storage
from .model import NativeRef, ReadSnapshot, Session, Turn

MAX_MESSAGES = 200
MAX_TEMP_BYTES = 128 * 1024 * 1024
_FOOTER = (
    "p older",
    "n newer",
    "u user",
    "a assistant",
    "Up/Down scroll",
    "PgUp/PgDn page",
    "Home/End",
    "q/Esc menu",
)


def load_messages(session: Session) -> tuple[list[Turn], str]:
    """Read adapter text without bridge budgeting, flattening, or storage writes.

    There is no generic bounded reader capability. Peak adapter memory can grow
    with the full transcript, even though only 200 messages are retained. The
    latest text from each conversational role survives the retention cap.
    """
    native = NativeRef(session.session_id, session.storage)
    found = resolve_storage(Path(native.storage))
    if found is not None and is_compressed_path(found):
        adapter = conversion.harness(session.tool)
        if isinstance(adapter.read, Unsupported):
            raise conversion.BridgeError("Native message reading is unavailable")
        # Retention's decompress_session changes storage and opened metadata.
        # Use its underlying read-only storage stream instead. Keep the suffix
        # for adapters that infer their format from the native file's name.
        with tempfile.TemporaryDirectory(prefix="session-preview-") as directory:
            temporary = Path(directory) / plain_path(found).name
            with open_for_scan(found) as source, temporary.open("xb") as target:
                size = 0
                while chunk := source.read(min(1024 * 1024, MAX_TEMP_BYTES - size + 1)):
                    size += len(chunk)
                    if size > MAX_TEMP_BYTES:
                        raise conversion.BridgeError(
                            "Compressed preview exceeds the 128 MiB temporary storage limit"
                        )
                    target.write(chunk)
            snapshot = adapter.read(
                NativeRef(native.session_id, str(temporary)), latest_window=False
            )
            if not isinstance(snapshot, ReadSnapshot):
                raise conversion.BridgeError("Invalid native conversation read")
            transcript = snapshot.transcript
    else:
        transcript = conversion.read_transcript(
            session.tool, native, tool_calls=False, latest_window=False
        )
    recent: deque[tuple[int, Turn]] = deque(maxlen=MAX_MESSAGES)
    latest: dict[str, tuple[int, Turn]] = {}
    count = 0
    for turn in transcript.turns:
        if turn.role not in ("user", "assistant") or not turn.text.strip():
            continue
        # Do not retain tool payloads on the compressed direct-reader path.
        entry = (count, Turn(turn.role, turn.text))
        recent.append(entry)
        latest[turn.role] = entry
        count += 1
    chosen = dict(recent)
    chosen.update(dict(latest.values()))
    protected = {entry[0] for entry in latest.values()}
    for index in sorted(chosen):
        if len(chosen) <= MAX_MESSAGES:
            break
        if index not in protected:
            del chosen[index]
    notice = (
        f"Showing {len(chosen)} of {count} messages; latest user/assistant kept."
        if count > len(chosen)
        else ""
    )
    return [chosen[index] for index in sorted(chosen)], notice


def _safe(text: str) -> str:
    parts = []
    for char in text:
        if char == "\n":
            parts.append(char)
        elif char == "\t":
            parts.append("    ")
        elif unicodedata.category(char).startswith("C"):
            value = ord(char)
            parts.append(f"\\x{value:02x}" if value < 256 else f"\\u{value:04x}")
        else:
            parts.append(char)
    return "".join(parts)


def cell_width(text: str) -> int:
    """Conservative terminal cell count for sanitized text."""
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        for c in text
    )


def wrap(text: str, width: int) -> list[str]:
    """Wrap at spaces when possible without losing whitespace or message text."""
    width = max(1, width)
    result = []
    for paragraph in _safe(text).split("\n"):
        line, used = "", 0
        for char in paragraph:
            cells = cell_width(char)
            if used + cells > width and line:
                boundary = line.rfind(" ") + 1
                if boundary:
                    result.append(line[:boundary])
                    line = line[boundary:]
                    used = cell_width(line)
                else:
                    result.append(line)
                    line, used = "", 0
                if used + cells > width and line:
                    result.append(line)
                    line, used = "", 0
            # A double-width glyph cannot fit in a one-cell terminal.
            if cells > width:
                char, cells = "?", 1
            line += char
            used += cells
        result.append(line)
    return result


def _footer(width: int) -> list[str]:
    lines = []
    line = ""
    for item in _FOOTER:
        joined = f"{line} | {item}" if line else item
        if cell_width(joined) > width and line:
            lines.append(line)
            line = item
        else:
            line = joined
    return lines + [line]


def _paint(screen, lines: list[str]) -> None:
    screen.erase()
    for row, line in enumerate(lines):
        try:
            screen.addstr(row, 0, line)
        except curses.error:
            pass  # Resizing can race a paint; the next frame uses the new size.
    screen.refresh()


def run(screen, session: Session) -> None:
    """Display a static native snapshot; restore the caller's input timeout.

    p/n: older/newer; u/a: latest user/assistant; arrows and page keys: scroll;
    Home/End: current message bounds; q/Esc: return. No nested curses wrapper.
    """
    getdelay = getattr(screen, "getdelay", None)
    previous_delay = getdelay() if getdelay is not None else -1
    try:
        screen.timeout(-1)
        height, columns = screen.getmaxyx()
        _paint(screen, wrap("m messages - Loading...", max(1, columns - 1))[:height])
        try:
            messages, notice = load_messages(session)
            error = ""
        except conversion.BridgeError as failure:
            messages, notice = [], ""
            error = f"Messages unavailable: {failure}. Return to the menu and retry."
        except Exception:
            messages, notice = [], ""
            error = "Messages unavailable. Return to the menu and retry."
        index, offset = max(0, len(messages) - 1), 0
        cached = None
        body = []
        while True:
            height, columns = screen.getmaxyx()
            width = max(1, columns - 1)
            footer = _footer(width)
            if width < 18 or height < len(footer) + 4:
                _paint(screen, wrap("q/Esc menu; enlarge terminal", width)[:height])
                page = 1
            else:
                selected = messages[index] if messages else None
                signature = (index, width)
                if cached != signature:
                    body = wrap(
                        selected.text
                        if selected
                        else error or "No conversational messages available.",
                        width,
                    )
                    cached = signature
                page = height - len(footer) - 3
                offset = min(max(0, offset), max(0, len(body) - page))
                status = (
                    f"{index + 1}/{len(messages)} {selected.role} | "
                    f"lines {offset + 1}-{min(len(body), offset + page)}/{len(body)}"
                    if selected
                    else ""
                )
                title = wrap("m messages - " + session.title, width)[0]
                headers = [title, wrap(status, width)[0], wrap(notice, width)[0]]
                visible = body[offset : offset + page]
                visible += [""] * (page - len(visible))
                _paint(screen, headers + visible + footer)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ("q", "Q", "\x1b", 27):
                return
            if key in ("p", "n") and messages:
                index = min(len(messages) - 1, max(0, index + (1 if key == "n" else -1)))
                offset = 0
            elif key in ("u", "a") and messages:
                role = "user" if key == "u" else "assistant"
                index = next(
                    (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == role), index
                )
                offset = 0
            elif key == curses.KEY_DOWN:
                offset += 1
            elif key == curses.KEY_UP:
                offset -= 1
            elif key == curses.KEY_NPAGE:
                offset += max(1, page - 1)
            elif key == curses.KEY_PPAGE:
                offset -= max(1, page - 1)
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = max(0, len(body) - page)
    finally:
        screen.timeout(previous_delay)
