"""Briefs and updates: what the hook injects into a supervisor before a prompt."""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..conversion import BridgeError
from ..model import NativeRef
from ..registry import REGISTRY
from . import store
from .digest import render_digest
from .index import TargetIndex, clean_exchange, tokens
from .store import HookState

if TYPE_CHECKING:
    from ..app import UserState
    from .index import Exchange

INLINE_LIMIT = 9_000
KEEP_FILES = 5
TAIL_TOKENS = 50_000
LEDGER_NAME = "ledger.md"
INDEX_NAME = "index.jsonl"
NO_LEDGER = "(no ledger yet)"

STANDING_INSTRUCTIONS = """\
You are a supervisor of another agent session (the target), not a participant in it.

1. Your role is to assess and advise. Never modify the target, its working files, its
   transcript or its process.
2. Keep your ledger current: {ledger_path}
   Record the objective, your running assessment, open concerns and decisions. Record a message
   to the target as sent only when the user says they sent it.
3. When asked for a message to the target, produce one clean, paste-ready block for the user to
   review.
4. Say plainly when your information is stale or partial.
5. To look further back than this brief shows, run:
   sessions --target-log {record_id} [--since TIME] [--tail N]
   It prints cleaned target activity without moving your checkpoint.
"""


def standing_instructions(ledger_path: Path, record_id: str) -> str:
    return STANDING_INSTRUCTIONS.format(ledger_path=str(ledger_path), record_id=record_id)


def supervisor_dir(record_id: str, root: Path | None = None) -> Path:
    return (store.SUPERVISORS_DIR if root is None else root) / record_id


def format_age(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    if not minutes:
        return "less than a minute"
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def liveness_text(storage: str, *, now: float | None = None) -> str:
    """How long ago the target's transcript last changed."""
    try:
        modified = os.stat(storage).st_mtime
    except OSError:
        return "last target activity unknown"
    age = (time.time() if now is None else now) - modified
    return f"last target activity {format_age(age)} ago"


def _when(stamp: float) -> str:
    if not stamp:
        return "the last update"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _render(exchanges: list[Exchange]) -> list[str]:
    return [f"#{ex.seq}\n{clean_exchange(ex)}" for ex in exchanges]


def _tail_split(exchanges: list[Exchange]) -> tuple[list[Exchange], list[Exchange]]:
    """Split into (older, tail); the tail is the newest exchanges within the token budget."""
    used = 0
    start = len(exchanges)
    for position in range(len(exchanges) - 1, -1, -1):
        cost = tokens(_render([exchanges[position]])[0]) + 1
        if used + cost > TAIL_TOKENS:
            break
        used += cost
        start = position
    if start == len(exchanges) and exchanges:
        start = len(exchanges) - 1
    return exchanges[:start], exchanges[start:]


def _tail_text(tail: list[Exchange]) -> str:
    if not tail:
        return "(no activity yet)"
    text = "\n\n".join(_render(tail))
    limit = int(TAIL_TOKENS * 3.5)
    if len(text) > limit:
        text = "[… start of this exchange omitted …]\n" + text[-limit:]
    return text


def _objective(exchanges: list[Exchange], title: str) -> str:
    first = next((ex for ex in exchanges if ex.seq >= 1 and ex.typed), None)
    lines = [f"Title: {title}" if title else "Title: (untitled)"]
    lines.append("First prompt: " + (first.typed if first else "(none recorded)"))
    return "\n".join(lines)


def _header(
    record: dict[str, Any], target_tool: str, target_title: str, cwd: str, liveness: str
) -> list[str]:
    del record
    label = _tool_label(target_tool)
    return [
        f'Target: {label} session "{target_title}"',
        f"Working directory: {cwd}",
        f"Liveness: {liveness}",
    ]


def _tool_label(tool: str) -> str:
    try:
        return REGISTRY.get(tool).label
    except (KeyError, ValueError):
        return tool


def _read_ledger(directory: Path) -> str:
    try:
        text = (directory / LEDGER_NAME).read_text(encoding="utf-8")
    except OSError:
        return NO_LEDGER
    return text if text.strip() else NO_LEDGER


def build_brief(
    record: dict[str, Any],
    hook: HookState,
    index: TargetIndex,
    *,
    target_title: str,
    target_tool: str,
    cwd: str,
    liveness: str,
    root: Path | None = None,
    note: str = "",
) -> str:
    exchanges = index.exchanges()
    older, tail = _tail_split(exchanges)
    directory = supervisor_dir(hook.record_id, root)
    ledger_path = (directory / LEDGER_NAME).resolve()
    digest = render_digest(older) if older else "(no earlier history)"
    header = "\n".join(_header(record, target_tool, target_title, cwd, liveness))
    parts = ["# Supervisor brief", header]
    if note:
        parts.append(note)
    parts += [
        "## Starting objective\n" + _objective(exchanges, target_title),
        "## Digest of earlier activity\n" + digest,
        "## Recent activity (cleaned)\n" + _tail_text(tail),
        f"## Ledger ({ledger_path})\n" + _read_ledger(directory),
        "## Standing instructions\n" + standing_instructions(ledger_path, hook.record_id),
    ]
    return "\n\n".join(parts)


def build_update(
    record: dict[str, Any],
    hook: HookState,
    index: TargetIndex,
    *,
    target_title: str,
    target_tool: str,
    cwd: str,
    liveness: str,
    note: str = "",
    resent: int | None = None,
) -> str:
    """Cleaned exchanges after ``hook.last_seq_sent``, plus ``resent`` if it grew."""
    stored = index.exchanges()
    fresh = [ex for ex in stored if ex.seq > hook.last_seq_sent]
    again = [ex for ex in stored if resent is not None and ex.seq == resent]
    header = "# Target update\n" + "\n".join(
        _header(record, target_tool, target_title, cwd, liveness)
    )
    parts = [header]
    if note:
        parts.append(note)
    if not fresh and not again:
        parts.append(f"Nothing new: no target activity since {_when(hook.updated)}.")
        return "\n\n".join(parts)
    if again:
        parts.append(
            f"(Exchange #{again[0].seq} was still open at the last update; its full text follows.)"
        )
    parts.extend(_render(again + fresh))
    return "\n\n".join(parts)


def _prune(directory: Path, kind: str) -> None:
    pattern = re.compile(rf"{kind}-(\d+)\.md$")
    found = sorted(
        (int(m.group(1)), p) for p in directory.glob(f"{kind}-*.md") if (m := pattern.match(p.name))
    )
    for _, path in found[:-KEEP_FILES]:
        try:
            path.unlink()
        except OSError:
            pass


def _next_number(directory: Path, kind: str) -> int:
    pattern = re.compile(rf"{kind}-(\d+)\.md$")
    numbers = [
        int(m.group(1)) for p in directory.glob(f"{kind}-*.md") if (m := pattern.match(p.name))
    ]
    return max(numbers, default=0) + 1


def deliver(kind: str, text: str, directory: Path) -> str:
    """The text to inject: whole when short, otherwise a note naming a file that holds it."""
    if len(text) <= INLINE_LIMIT:
        return text
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"{kind}-{_next_number(directory, kind)}.md").resolve()
    path.write_text(text, encoding="utf-8")
    _prune(directory, kind)
    return (
        f"Supervisor {kind}: the full text is too long to inline ({len(text):,} characters). "
        f"It is in {path}. Read the whole file before answering."
    )


def _member_checkpoints(member: dict[str, Any]) -> list[Any]:
    found = [member.get("checkpoint"), member.get("protected_checkpoint")]
    if not any(value is not None for value in found):
        found.append(member.get("cursor"))
    result: list[Any] = []
    for value in found:
        if value is not None and value not in result:
            result.append(value)
    return result


def _pick_head(hook: HookState, heads: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict]:
    for key, member in heads:
        if key == hook.member:
            return key, member
    return max(heads, key=lambda pair: int(pair[1].get("generation", 0)))


def _ref(member: dict[str, Any]) -> tuple[str, NativeRef]:
    return str(member["tool"]), NativeRef(str(member["session_id"]), str(member["storage"]))


def _warn(message: str) -> str:
    return "⚠ supervisor: " + " ".join(message.split())


def prepare_context(
    record_id: str,
    *,
    state: UserState,
    hook: HookState | None = None,
    root: Path | None = None,
) -> tuple[str, HookState]:
    """Decide brief or update; return the text to inject and the new hook state.

    Never raises: failures become a one-line warning and leave the state unchanged.
    """
    if hook is None:
        hook = store.load_hook_state(record_id, root) or HookState(record_id)
    try:
        return _prepare(record_id, state, hook, root)
    except Exception as error:  # the hook must never break a prompt
        return _warn(f"{type(error).__name__}: {error}"), hook


def _prepare(
    record_id: str, state: UserState, hook: HookState, root: Path | None
) -> tuple[str, HookState]:
    record = state.supervisors.get(record_id)
    if record is None:
        return _warn(f"no supervisor record {record_id} in state.json"), hook
    status, heads = state.conversation_head(str(record.get("target_conversation", "")))
    if status == "diverged":
        names = "; ".join(
            f'{_tool_label(str(m.get("tool", "")))} "{m.get("title", "")}" ({key})'
            for key, m in heads
        )
        return (
            "⚠ supervisor: the target diverged. Two members advanced independently: "
            f"{names}. No target activity is injected; tell the user and let them resolve it "
            "in the sessions browser.",
            hook,
        )
    if status == "compressed":
        return (
            f"⚠ supervisor: the target has been compressed since {_when(hook.updated)}, so no new "
            "activity can be shown. The checkpoint is unchanged, and updates resume once the user "
            "decompresses it with Z in the sessions browser.",
            hook,
        )
    if status != "ok" or not heads:
        what = "unavailable" if status == "unavailable" or not heads else "unverifiable"
        return _unavailable(hook, what), hook
    key, member = _pick_head(hook, heads)
    tool, ref = _ref(member)
    directory = supervisor_dir(record_id, root)
    idx = TargetIndex(directory / INDEX_NAME)
    moved = bool(hook.member) and key != hook.member
    note = ""
    try:
        stored = idx.exchanges()
        before = {ex.seq: ex for ex in stored}
        last_open = stored[-1].seq if stored else None
        if moved:
            starts = _member_checkpoints(member)
            note = (
                f'Target now runs in {_tool_label(tool)} as "{member.get("title", "")}"; '
                "history carried over by the bridge is not repeated."
            )
        else:
            starts = [hook.checkpoint] if hook.checkpoint is not None else []
        checkpoint = None
        rebuilt = False
        if starts and stored:
            for start in starts:
                checkpoint = idx.extend(tool, ref, key, start)
                if checkpoint is not None:
                    break
        if checkpoint is None:
            if stored:
                note = (
                    "The target's transcript was rewritten, so the supervisor's checkpoint "
                    "could not be located. This fresh brief replaces the incremental update."
                )
                idx.path.unlink()
                stored, before, last_open = [], {}, None
            checkpoint = idx.build(tool, ref, key)
            rebuilt = True
    except BridgeError:
        return _unavailable(hook, "unavailable"), hook
    exchanges = idx.exchanges()
    last_seq = exchanges[-1].seq if exchanges else 0
    new = [ex for ex in exchanges if ex.seq > hook.last_seq_sent]
    resent = None
    if last_open is not None and last_open in before:
        now_ex = next((ex for ex in exchanges if ex.seq == last_open), None)
        if now_ex is not None and now_ex != before[last_open] and last_open <= hook.last_seq_sent:
            resent = last_open
    liveness = liveness_text(str(member.get("storage", "")))
    options = dict(
        target_title=str(member.get("title") or record.get("title", "")),
        target_tool=tool,
        cwd=str(member.get("cwd", "")),
        liveness=liveness,
    )
    long_gap = sum(tokens(t) for t in _render(new)) > TAIL_TOKENS
    advanced = replace(
        hook, member=key, checkpoint=checkpoint, last_seq_sent=last_seq, updated=time.time()
    )
    if hook.brief_due or rebuilt or long_gap:
        text = build_brief(record, advanced, idx, root=root, note=note, **options)
        done = replace(advanced, brief_due=False, briefs=hook.briefs + 1)
        return deliver("brief", text, directory), done
    text = build_update(record, hook, idx, note=note, resent=resent, **options)
    unchanged = not new and resent is None and not moved
    if unchanged:
        return deliver("update", text, directory), replace(hook, checkpoint=checkpoint)
    return deliver("update", text, directory), advanced


def _unavailable(hook: HookState, what: str) -> str:
    return (
        f"⚠ supervisor: target {what} since {_when(hook.updated)}; no new activity can be "
        "shown. The checkpoint is unchanged. Say so plainly if asked about the target."
    )


__all__ = [
    "INLINE_LIMIT",
    "STANDING_INSTRUCTIONS",
    "build_brief",
    "build_update",
    "deliver",
    "prepare_context",
    "standing_instructions",
]
