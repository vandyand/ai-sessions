"""Cross-harness bridging: continue any session in any supported CLI.

Every harness stores transcripts in its own format and resumes only its own
session ids, so a conversation recorded by one cannot be handed to another by
reference.  Bridging closes that gap by reading the source transcript into a
harness-neutral conversation, then writing that conversation back out in the
target's own on-disk format as a *new* native session.  The original file is
never modified, and the copy is an ordinary session that the target CLI
resumes -- and then keeps appending to -- natively.

Conversions go through the neutral form rather than pairwise, so adding a
harness costs one reader and one writer instead of a converter per existing
harness. Harness capabilities are resolved dynamically through the registry.

Tool calls have no portable representation -- a replayed ``tool_use`` block
would name tools the target does not have, and would need a matching result
to stay valid -- so they cross over summarised as text rather than as live
calls.  That keeps what was run and what it returned, which is usually the
part worth having, without inventing structure the target cannot honour.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import math
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .capabilities import HarnessAdapter, Unsupported
from .model import (
    DEFAULT_MAX_CHARS,
    HANDOFF_NOTE_RESERVE_CHARS,
    MIN_BRIDGE_CHARS,
    MIN_BUDGET_TOKENS,
    TARGET_CONTEXT_RESERVE_CHARS,
    TRUNCATION_MARKER,
    Availability,
    BridgeResult,
    Budget,
    Checkpoint,
    NativeRef,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    SelectionMetric,
    SelectionResult,
    ToolCall,
    Transcript,
    Turn,
    is_checkpoint,
)
from .model import (
    LEGACY_DEFAULT_MAX_CHARS as LEGACY_DEFAULT_MAX_CHARS,
)
from .model import (
    BudgetPolicy as BudgetPolicy,
)
from .registry import REGISTRY

# A bridged transcript is replayed into the target's context window in full,
# so the ceiling is a context budget rather than a storage one.
CLAUDE_BUDGET_CONTEXT_TOKENS = 1_000_000
CODEX_BUDGET_CONTEXT_TOKENS = 1_000_000
DEFAULT_USABLE_FRACTION = 0.75
ONE_M_CONTEXT_USABLE_FRACTION = 0.95
DEFAULT_CHARS_PER_TOKEN = 2.0
REQUEST_CHARS = 300
RESULT_CHARS = 500
CODEX_CONTEXT_WINDOW = 258_400

# Machine-injected scaffolding that reads as noise once it is out of its
# original harness.  Each provider re-injects its own equivalent on resume.
_SCRUB_BLOCKS = (
    "environment_context",
    "user_instructions",
    "system-reminder",
    "permissions instructions",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
)
_SCRUB_PATTERNS = tuple(re.compile(rf"<{tag}>.*?</{tag}>", re.I | re.S) for tag in _SCRUB_BLOCKS)

# Codex wraps every tool result in a fixed preamble that says nothing the
# surrounding transcript does not already say.
# Codex's ``exec`` tool takes a snippet of JavaScript that wraps the real
# shell command, which is the only part worth carrying into a summary.
_CODEX_EXEC_COMMAND = re.compile(r'"cmd"\s*:\s*("(?:[^"\\]|\\.)*")')

# Argument keys worth showing first when a tool call is summarised, in the
# order a reader would want them.
_SALIENT_KEYS = (
    "command",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "old_string",
    "prompt",
    "description",
)


class BridgeError(RuntimeError):
    """A session could not be carried into another harness."""


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one compact JSON line, repairing a missing final newline first.

    Both providers append to these files while they run, so the line is
    written in one call and never rewrites what is already on disk.
    """
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = b""
    try:
        with path.open("rb") as handle:
            if handle.seek(0, os.SEEK_END):
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    prefix = b"\n"
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(prefix + line + b"\n")


def _iso(value: float) -> str:
    moment = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def scrub(value: Any) -> str:
    """Drop machine-injected blocks and leave the human-readable remainder."""
    if not isinstance(value, str):
        return ""
    text = value
    for pattern in _SCRUB_PATTERNS:
        text = pattern.sub(" ", text)
    return text.strip()


def _clip(value: str, limit: int) -> str:
    text = value.strip()
    suffix = " …"
    return text if len(text) <= limit else text[: limit - len(suffix)].rstrip() + suffix


def _bounded_json_value(value: str, limit: int) -> str:
    """Clip a string by its rendered JSON size while keeping the JSON valid."""
    text = value.strip()
    if len(json.dumps(text, ensure_ascii=False)) <= limit:
        return text
    low = 0
    high = len(text)
    best = "…"
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + " …"
        if len(json.dumps(candidate, ensure_ascii=False)) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _block_text(
    content: Any, kinds: tuple[str, ...] = ("text", "input_text", "output_text")
) -> str:
    """Concatenate the plain-text blocks of a provider message body."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in kinds:
            continue
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n\n".join(parts)


def _digest_arguments(value: Any) -> str:
    """Render tool arguments as the one detail that identifies the call."""
    payload = value
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _clip(payload, REQUEST_CHARS)
    if not isinstance(payload, dict):
        return _clip(str(payload), REQUEST_CHARS)
    for key in _SALIENT_KEYS:
        if isinstance(payload.get(key), str) and payload[key].strip():
            extra = " …" if len(payload) > 1 else ""
            return _clip(payload[key], REQUEST_CHARS) + extra
    if not payload:
        return ""
    return _clip(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), REQUEST_CHARS)


def _digest_codex_input(value: Any) -> str:
    """Prefer the shell command a Codex ``exec`` snippet actually runs."""
    if isinstance(value, str):
        match = _CODEX_EXEC_COMMAND.search(value)
        if match:
            try:
                return _clip(json.loads(match.group(1)), REQUEST_CHARS)
            except json.JSONDecodeError:
                pass
    return _digest_arguments(value)


def render_calls(calls: Iterable[ToolCall]) -> str:
    """Summarise tool activity as compact, greppable text."""
    lines: list[str] = []
    for call in calls:
        head = f"⟦{call.name}⟧"
        if call.request:
            head += f" {call.request}"
        lines.append(head)
        if call.result:
            body = call.result.splitlines() or [""]
            lines.append("   → " + body[0])
            lines.extend("     " + line for line in body[1:])
    return "\n".join(lines)


def flatten(turns: Iterable[Turn]) -> list[Turn]:
    """Fold tool summaries into the text of the turn that made them."""
    result: list[Turn] = []
    for turn in turns:
        if not turn.calls:
            result.append(turn)
            continue
        rendered = render_calls(turn.calls)
        text = f"{turn.text}\n\n{rendered}" if turn.text else rendered
        # The calls stay attached after rendering purely so later trimming can
        # still report how many survived.  Writers only ever read ``text``.
        result.append(Turn(turn.role, text.strip(), turn.calls))
    return result


def _records(path: Path, *, end: int | None = None) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open("rb")
    except OSError as error:
        raise BridgeError(f"could not read {path}: {error}") from error
    with handle:
        while end is None or handle.tell() < end:
            line = handle.readline() if end is None else handle.readline(end - handle.tell())
            if not line:
                break
            if end is not None and not line.endswith(b"\n"):
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


class _Conversation:
    """Assembles turns while tool calls and their results arrive apart.

    Both providers record a call and its output as separate entries, often
    with other traffic in between, so calls are attached to the assistant
    turn that made them and filled in by id once the output shows up.
    """

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.opaque_compactions = 0
        self.carried_windows = 0
        self.sealed_summary = False
        self.resumes_at_last_summary = False
        self._pending: dict[str, tuple[int, int]] = {}

    def reset(self) -> None:
        """Drop everything accumulated so far, including open tool calls.

        A pending call whose result never arrived must not survive into the
        next window, or that result attaches to an unrelated turn.
        """
        self.turns.clear()
        self._pending.clear()

    def append_window(self, turns: list[Turn], sealed: bool) -> None:
        """Keep a window's context without discarding what preceded it.

        Only for ``latest_window = false``, where the caller has explicitly
        asked to replay the whole transcript.  Peak memory then grows with the
        file, which is the cost of that request.
        """
        self.turns.extend(turns)
        self.carried_windows += 1
        self.sealed_summary = sealed
        self.resumes_at_last_summary = True

    def carry_window(self, turns: list[Turn], sealed: bool) -> None:
        """Adopt a compaction's carried context in place of everything before it.

        ``replacement_history`` is named for what it does: the window replaces
        the conversation that preceded it.  Appending it instead would keep
        every superseded window alive at once.
        """
        self.reset()
        self.turns.extend(turns)
        self.carried_windows += 1
        self.sealed_summary = sealed
        self.resumes_at_last_summary = True

    def opaque_compaction(self) -> None:
        """Note a compaction whose carried context cannot be read back."""
        # An unreadable compaction leaves the copy standing before the point
        # the source resumes from, whatever happened in earlier windows.
        self.resumes_at_last_summary = False
        self.opaque_compactions += 1

    def message(self, role: str, text: str, compaction: bool = False) -> None:
        if text:
            self.turns.append(Turn(role, text, compaction=compaction))

    def call(self, call_id: str, name: str, request: str) -> None:
        if not self.turns or self.turns[-1].role != "assistant":
            self.turns.append(Turn("assistant", ""))
        turn = self.turns[-1]
        calls = (*turn.calls, ToolCall(name=name or "tool", request=request))
        self.turns[-1] = replace(turn, calls=calls)
        if call_id:
            self._pending[call_id] = (len(self.turns) - 1, len(calls) - 1)

    def result(self, call_id: str, text: str) -> None:
        location = self._pending.pop(call_id, None)
        if location is None or not text:
            return
        index, position = location
        turn = self.turns[index]
        calls = list(turn.calls)
        calls[position] = replace(calls[position], result=_clip(text, RESULT_CHARS))
        self.turns[index] = replace(turn, calls=tuple(calls))

    def finish(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.text or turn.calls]


def read_transcript(
    tool: str,
    native: NativeRef,
    *,
    tool_calls: bool = True,
    latest_window: bool = True,
) -> Transcript:
    """Extract the conversation from a transcript, oldest first.

    Turns come back structured, with tool calls still attached, so callers
    can count or inspect them before ``prepare`` renders them to text.
    """
    return read_snapshot(
        tool,
        native,
        tool_calls=tool_calls,
        latest_window=latest_window,
    ).transcript


def read_snapshot(
    tool: str,
    native: NativeRef,
    *,
    tool_calls: bool = True,
    latest_window: bool = True,
) -> ReadSnapshot:
    """Read one adapter-owned native snapshot and its opaque checkpoint."""
    adapter = harness(tool)
    if not isinstance(native, NativeRef):
        raise TypeError("native session reads require an explicit NativeRef")
    if isinstance(adapter.availability, Unsupported):
        raise BridgeError(
            f"{adapter.label} cannot verify exact native availability: "
            f"{adapter.availability.reason}"
        )
    availability = native_session_availability(tool, native)
    if availability == "unavailable":
        raise BridgeError(
            f"the {adapter.label} source session is unavailable, so there is nothing to carry over"
        )
    if availability != "available":
        raise BridgeError(
            f"the {adapter.label} source session could not be verified safely; "
            "restore storage access or retry"
        )
    reader = adapter.read
    if isinstance(reader, Unsupported):
        raise BridgeError(f"{adapter.label} cannot read native transcripts: {reader.reason}")
    try:
        result = reader(native, latest_window=latest_window)
    except OSError as error:
        raise BridgeError(f"could not read the {adapter.label} source session: {error}") from error
    if (
        not isinstance(result, ReadSnapshot)
        or not isinstance(result.transcript, Transcript)
        or not is_checkpoint(result.checkpoint)
    ):
        raise BridgeError(f"{adapter.label} returned an invalid native read checkpoint")
    transcript = result.transcript
    turns = transcript.turns
    if not tool_calls:
        turns = [replace(turn, calls=()) for turn in turns]
    return ReadSnapshot(
        Transcript(
            [turn for turn in turns if turn.text or turn.calls],
            transcript.opaque_compactions,
            transcript.carried_windows,
            transcript.sealed_summary,
            transcript.resumes_at_last_summary,
        ),
        result.checkpoint,
    )


def read_turns(tool: str, native: NativeRef, *, tool_calls: bool = True) -> list[Turn]:
    """The conversation alone, for callers that do not need the rest."""
    return read_transcript(tool, native, tool_calls=tool_calls).turns


def prepare(turns: Iterable[Turn]) -> list[Turn]:
    """Compatibility shaping helper; production selects before calling ``merge_runs``."""
    return merge_runs(flatten(turns))


def count_compactions(turns: Iterable[Turn]) -> int:
    return sum(1 for turn in turns if turn.compaction)


def from_last_compaction(turns: list[Turn]) -> tuple[list[Turn], int]:
    """Drop everything superseded by the newest compaction summary.

    A long session is not one conversation but a chain of windows, each
    opening with a summary of everything before it.  Replaying the whole
    file carries superseded history *and* every summary of it, then leaves
    the character budget to cut the middle out at an arbitrary point.
    Starting at the last summary is both smaller and truer: it is the
    conversation the source session itself is still holding.
    """
    boundaries = [index for index, turn in enumerate(turns) if turn.compaction]
    if not boundaries:
        return turns, 0
    return turns[boundaries[-1] :], len(boundaries)


def merge_runs(turns: Iterable[Turn]) -> list[Turn]:
    """Fold consecutive same-role turns together.

    Summarising tool traffic leaves runs of same-role messages behind, and
    the providers expect a conversation that alternates.
    """
    merged: list[Turn] = []
    for turn in turns:
        if merged and merged[-1].role == turn.role:
            previous = merged[-1]
            merged[-1] = Turn(
                turn.role,
                f"{previous.text}\n\n{turn.text}".strip(),
                (*previous.calls, *turn.calls),
            )
        else:
            merged.append(turn)
    return merged


def _item_cost(turn: Turn) -> int:
    return len(turn.text)


def _join_cost(left: Turn, right: Turn) -> int:
    return 2 if left.role == right.role else 0


def _truncate_turn(turn: Turn, limit: int) -> Turn:
    if len(turn.text) <= limit:
        return turn
    if limit <= len(TRUNCATION_MARKER):
        raise BridgeError("the bridge budget cannot hold a truncated message anchor")
    prefix = limit - len(TRUNCATION_MARKER)
    text = turn.text[:prefix] + TRUNCATION_MARKER
    retained_calls: tuple[ToolCall, ...] = ()
    if turn.calls:
        rendered = render_calls(turn.calls)
        start = turn.text.rfind(rendered)
        if start >= 0:
            count = 0
            offset = start
            for call in turn.calls:
                block = render_calls((call,))
                if offset + len(block) > prefix:
                    break
                count += 1
                offset += len(block) + 1
            retained_calls = turn.calls[:count]
    return Turn(turn.role, text, retained_calls, turn.compaction)


TEXT_SELECTION_METRIC = SelectionMetric(_item_cost, _join_cost, _truncate_turn)


def selection_cost(turns: Iterable[Turn], metric: SelectionMetric = TEXT_SELECTION_METRIC) -> int:
    """Return a conservative linear assembly cost for one ordered candidate."""
    total = 0
    previous: Turn | None = None
    for turn in turns:
        item = metric.item_cost(turn)
        if item < 0:
            raise BridgeError("selection metric returned a negative item cost")
        total += item
        if previous is not None:
            joined = metric.join_cost(previous, turn)
            if joined < 0:
                raise BridgeError("selection metric returned a negative join cost")
            total += joined
        previous = turn
    return total


def _capped(turn: Turn, limit: int, metric: SelectionMetric) -> tuple[Turn, bool]:
    if metric.item_cost(turn) <= limit:
        return turn, False
    result = metric.truncate(turn, limit)
    if result.role != turn.role or metric.item_cost(result) > limit:
        raise BridgeError("selection metric produced an invalid truncated anchor")
    return result, True


def select_messages(
    turns: list[Turn], max_chars: int, metric: SelectionMetric = TEXT_SELECTION_METRIC
) -> SelectionResult:
    """Keep the first selected-context message and one contiguous newest suffix."""
    if max_chars <= 0:
        raise BridgeError("the bridge selection budget must be positive")
    if not turns:
        return SelectionResult([], 0, 0)
    if selection_cost(turns, metric) <= max_chars:
        return SelectionResult(list(turns), 0, 0)
    if len(turns) == 1:
        only, truncated = _capped(turns[0], max_chars, metric)
        return SelectionResult([only], 0, int(truncated))

    anchor_share = (max_chars - 2) // 2
    head, head_truncated = _capped(turns[0], anchor_share, metric)
    # Give the newest anchor everything left after the capped head and the
    # worst-case two-character anchor join. If both independent caps grew at once, their
    # combined cost could grow by two when the budget grew by one, making
    # selection non-monotone at the truncated/full boundary.
    head_cost = metric.item_cost(head)
    newest_share = max_chars - 2 - head_cost
    newest, newest_truncated = _capped(turns[-1], newest_share, metric)
    suffix_reversed = [newest]
    suffix_first = newest
    suffix_cost = metric.item_cost(newest)
    for index in range(len(turns) - 2, 0, -1):
        turn = turns[index]
        item = metric.item_cost(turn)
        joined = metric.join_cost(turn, suffix_first)
        if item < 0 or joined < 0:
            raise BridgeError("selection metric returned a negative cost")
        candidate_suffix_cost = item + joined + suffix_cost
        total = head_cost + metric.join_cost(head, turn) + candidate_suffix_cost
        if total > max_chars:
            break
        suffix_reversed.append(turn)
        suffix_first = turn
        suffix_cost = candidate_suffix_cost
    kept = [head, *reversed(suffix_reversed)]
    if selection_cost(kept, metric) > max_chars:
        raise BridgeError("selected conversation exceeds its applied budget")
    return SelectionResult(
        kept,
        len(turns) - len(kept),
        int(head_truncated) + int(newest_truncated),
    )


def fit(turns: list[Turn], max_chars: int = DEFAULT_MAX_CHARS) -> tuple[list[Turn], int]:
    """Compatibility wrapper around whole-message selection."""
    selected = select_messages(turns, max_chars)
    return selected.turns, selected.dropped


def handoff_note(
    *,
    source_tool: str,
    target_tool: str,
    session_id: str,
    title: str,
    cwd: str,
    kept: int,
    assembled: int | None = None,
    calls: int,
    dropped: int,
    truncated: int = 0,
    compacted: int = 0,
    opaque_compactions: int = 0,
    sealed_summary: bool = False,
    resumes_at_last_summary: bool = True,
    conversation_id: str = "",
    budget: Budget | None = None,
    target_context: tuple[str, ...] = (),
) -> str:
    """The opening message that tells the target where this came from."""
    source = harness(source_tool).label
    target = harness(target_tool).label
    title = _bounded_json_value(_clip(title, 500), 600)
    cwd = _bounded_json_value(_clip(cwd, 500), 600)
    session_id = _bounded_json_value(_clip(session_id, 200), 250)
    conversation_id = _bounded_json_value(_clip(conversation_id, 100), 150)
    label = f" titled {json.dumps(title, ensure_ascii=False)}" if title else ""
    display_session_id = json.dumps(session_id, ensure_ascii=False)
    display_cwd = json.dumps(cwd, ensure_ascii=False) if cwd else "unknown"
    lines = [
        f"[ai-sessions] This conversation started in {source} and is being continued in {target}.",
    ]
    if conversation_id:
        lines.append(
            "[ai-sessions-provenance v1] "
            + json.dumps(
                {
                    "conversation_id": conversation_id,
                    "source": {"harness": source_tool, "session_id": session_id},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    lines += [
        "",
        f"Source session{label}: {display_session_id} ({source})",
        f"Working directory: {display_cwd}",
        "",
        (
            f"The {kept} source message(s) below were assembled into "
            f"{assembled if assembled is not None else kept} target message(s) as plain text."
        ),
    ]
    remaining_context = TARGET_CONTEXT_RESERVE_CHARS
    for detail in target_context[:4]:
        prefix = "Target preparation: "
        available = remaining_context - len(prefix) - 1
        if available <= 0:
            break
        rendered = f"{prefix}{_clip(detail, min(300, available))}"
        lines.append(rendered)
        remaining_context -= len(rendered) + 1
    if budget is not None:
        lines += [
            "",
            f"Applied bridge budget: approximately {budget.tokens:,} tokens / "
            f"{budget.chars:,} projected characters ({budget.origin}).",
        ]
        if budget.clamped:
            lines.append("The configured token budget was raised to the safe bridge minimum.")
        if budget.origin == "target-default":
            lines.append(
                "This target policy replaces the previous global 950,000-character ceiling; "
                "set bridge.max_tokens to choose a different ceiling."
            )
        elif budget.origin == "target-default-migrated":
            lines.append(
                "The legacy version-1 950,000-character machine default was replaced by this "
                "target policy; set bridge.max_tokens to choose a different ceiling."
            )
        elif budget.over_policy:
            lines.append(
                "This legacy character override exceeds the target policy; delete max_chars or "
                "set max_tokens to adopt an explicit token budget."
            )
    if calls:
        lines.append(
            f"{calls} tool call(s) are summarised inline as ⟦tool⟧ request → result, with "
            "long arguments and output clipped. They are a record of what was already run, "
            "not calls you made: treat the state of the filesystem as unverified and "
            "re-check anything that matters rather than trusting a summarised result."
        )
    else:
        lines.append(
            "Tool calls and command output were not carried across, so treat the state of "
            "the filesystem as unverified and re-check anything that matters."
        )
    if compacted:
        if resumes_at_last_summary:
            lines += [
                "",
                f"The source ran out of context {compacted} time(s) and summarised itself. What "
                "follows starts at the most recent of those summaries, which is where the source "
                "session itself is picking up; earlier windows are in its transcript but not here.",
            ]
        else:
            lines += [
                "",
                f"The source ran out of context {compacted} time(s) and summarised itself, but "
                "its most recent summary could not be read. What follows therefore stands before "
                "the point the source is picking up from, and some of it may be superseded.",
            ]
        if sealed_summary:
            lines.append(
                f"{source} seals its own summary of that window, so the summary itself could "
                "not cross. The messages it was written to stand in for did: what follows is "
                "the conversation the source kept, not a paraphrase of it."
            )
    if opaque_compactions:
        lines += [
            "",
            f"The source also compacted {opaque_compactions} time(s), but {source} stores those "
            "summaries encrypted, so the full pre-compaction history is carried instead of the "
            "summary. Expect some of it to have been superseded later in the conversation.",
        ]
    if dropped:
        lines += [
            "",
            f"{dropped} message(s) from the middle of the conversation were dropped to fit "
            "the context budget. The first message of the selected source context and a "
            "contiguous suffix of its most recent exchanges were kept.",
        ]
    if truncated:
        lines += [
            "",
            f"{truncated} anchor message(s) were truncated with an explicit marker to keep "
            "both the first selected-context message and the newest message within budget.",
        ]
    lines += ["", "Pick up from here as though the conversation had always been yours."]
    return "\n".join(lines)


def _records_after(path: Path, offset: int, *, end: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield complete JSONL records appended after a recorded byte frontier."""
    try:
        size = path.stat().st_size
        captured = size if end is None else end
        if offset < 0 or captured < offset or size < captured:
            # Provider transcripts are append-only. Shrinking means the native
            # history was replaced, which is necessarily a new revision.
            yield {"type": "ai_sessions_replaced"}
            return
        with path.open("rb") as handle:
            handle.seek(offset)
            while handle.tell() < captured:
                raw = handle.readline(captured - handle.tell())
                if not raw.endswith(b"\n"):
                    yield {"type": "ai_sessions_incomplete"}
                    break
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    yield {"type": "ai_sessions_incomplete"}
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        yield {"type": "ai_sessions_missing"}


class _UnstableJsonlSnapshot(RuntimeError):
    """Prevent a transient or racing JSONL classification from entering the cache."""


@functools.lru_cache(maxsize=2048)
def _cached_jsonl_change_status(
    classifier: Callable[[NativeRef, Checkpoint, int], str],
    registry_generation: int,
    ref: NativeRef,
    checkpoint: Checkpoint,
    device: int,
    inode: int,
    size: int,
    mtime_ns: int,
) -> str:
    """Classify one immutable file snapshot once for an adapter hook generation."""
    del registry_generation
    status = classifier(ref, checkpoint, size)
    if status == "unstable":
        raise _UnstableJsonlSnapshot
    try:
        after = Path(ref.storage).stat()
    except OSError as error:
        raise _UnstableJsonlSnapshot from error
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        device,
        inode,
        size,
        mtime_ns,
    ):
        raise _UnstableJsonlSnapshot
    return status


def _jsonl_change_status(
    ref: NativeRef,
    checkpoint: Checkpoint,
    classifier: Callable[[NativeRef, Checkpoint, int], str],
    registry_generation: int,
) -> str:
    """Use adapter-owned JSONL semantics with exact-ref, race-safe snapshot caching."""
    try:
        stat = Path(ref.storage).stat()
        return _cached_jsonl_change_status(
            classifier,
            registry_generation,
            ref,
            checkpoint,
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
    except (OSError, _UnstableJsonlSnapshot):
        return "unstable"


def harness(name: str) -> HarnessAdapter:
    try:
        return REGISTRY.get(name)
    except KeyError:
        raise BridgeError(f"unknown harness: {name}") from None


def bridge_tools() -> tuple[str, ...]:
    """Return bridge-capable harness names from the current registry generation."""
    return REGISTRY.bridge_targets()


def resolve_budget(
    target: str,
    *,
    max_tokens: int | None = None,
    max_chars: int | None = None,
    migrated: bool = False,
    policy: BudgetPolicy | None = None,
) -> Budget:
    """Resolve config into the one target budget used by the bridge."""
    policy = policy or harness(target).budget
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = None
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        max_chars = None
    default_tokens = math.floor(policy.context_tokens * policy.usable_fraction)
    default_chars = math.floor(default_tokens * policy.chars_per_token)
    if max_tokens is not None:
        minimum = max(
            MIN_BUDGET_TOKENS,
            math.ceil(MIN_BRIDGE_CHARS / policy.chars_per_token),
        )
        tokens = max(max_tokens, minimum)
        if policy.hard_limit and tokens > default_tokens:
            raise BridgeError(
                f"bridge.max_tokens={max_tokens} exceeds the prepared {target} target limit "
                f"of {default_tokens}; choose a larger target model or lower the budget "
                f"({policy.source})"
            )
        try:
            chars = math.floor(tokens * policy.chars_per_token)
        except (OverflowError, ValueError) as error:
            raise BridgeError("bridge.max_tokens is too large to resolve") from error
        return Budget(
            target=target,
            tokens=tokens,
            chars=chars,
            origin="config.max_tokens",
            clamped=tokens != max_tokens,
        )
    if migrated:
        return Budget(target, default_tokens, default_chars, "target-default-migrated")
    if max_chars is not None:
        if max_chars < MIN_BRIDGE_CHARS:
            raise BridgeError(
                f"bridge.max_chars must be at least {MIN_BRIDGE_CHARS} characters "
                "to hold required handoff metadata"
            )
        if policy.hard_limit and max_chars > default_chars:
            raise BridgeError(
                f"bridge.max_chars={max_chars} exceeds the prepared {target} target limit "
                f"of {default_chars}; choose a larger target model or lower the budget "
                f"({policy.source})"
            )
        try:
            tokens = math.ceil(max_chars / policy.chars_per_token)
        except (OverflowError, ValueError) as error:
            raise BridgeError("bridge.max_chars is too large to resolve") from error
        return Budget(
            target=target,
            tokens=tokens,
            chars=max_chars,
            origin="config.max_chars",
            over_policy=max_chars > default_chars,
        )
    return Budget(target, default_tokens, default_chars, "target-default")


def prepare_target(
    target: str,
    *,
    command: tuple[str, ...] | None = None,
    cwd: str = "",
    options: Mapping[str, Any] | None = None,
) -> PreparedTarget:
    """Resolve one immutable target plan before budgeting and materialization."""
    adapter = harness(target)
    resolved_command = command or adapter.default_command
    hook = adapter.prepare_target
    if isinstance(hook, Unsupported):
        return PreparedTarget(tuple(resolved_command), adapter.budget)
    try:
        prepared = hook(tuple(resolved_command), cwd, dict(options or {}))
    except OSError as error:
        raise BridgeError(f"could not prepare the {adapter.label} target: {error}") from error
    if not isinstance(prepared, PreparedTarget) or not isinstance(
        prepared.budget_policy, BudgetPolicy
    ):
        raise BridgeError(f"{adapter.label} returned an invalid target preparation")
    if prepared.command != tuple(resolved_command):
        raise BridgeError("target preparation changed the configured command")
    return prepared


def resolve_native_session(tool: str, session_id: str) -> NativeRef | None:
    """Resolve an ID through its adapter without borrowing another store."""
    if not session_id or tool not in REGISTRY:
        return None
    resolver = REGISTRY.get(tool).resolve
    if isinstance(resolver, Unsupported):
        return None
    try:
        resolved = resolver(session_id)
    except Exception:
        return None
    if not isinstance(resolved, NativeRef) or resolved.session_id != session_id:
        return None
    return resolved


def native_session_exists(tool: str, session_id: str, storage: str = "") -> bool:
    """Whether ``tool`` still has this exact native session.

    Cross-provider references are recovered by matching id-shaped strings out
    of transcripts, so they also pick up unrelated identifiers that share the
    shape -- a correlation id pasted into a conversation looks exactly like a
    Codex session id.  Sessions also get deleted.  Either way a reference has
    to be checked before a CLI is asked to resume it.
    """
    if not session_id:
        return False
    ref = NativeRef(session_id, storage) if storage else resolve_native_session(tool, session_id)
    if ref is None:
        return False
    return native_session_availability(tool, ref) == "available"


def native_session_availability(tool: str, ref: NativeRef) -> str:
    """Return available, unavailable, or unknown for one exact native ref."""
    if tool not in REGISTRY:
        return "unknown"
    probe = REGISTRY.get(tool).availability
    if isinstance(probe, Unsupported):
        return "unknown"
    try:
        value = probe(ref)
    except (BridgeError, OSError):
        return "unknown"
    return native_session_availability_value(value)


def native_checkpoint(tool: str, ref: NativeRef) -> Checkpoint | None:
    """Capture a current adapter-owned checkpoint, or None when it is not reliable."""
    if tool not in REGISTRY:
        return None
    capture = REGISTRY.get(tool).checkpoint
    if isinstance(capture, Unsupported):
        return None
    try:
        value = capture(ref)
    except Exception:
        return None
    if not is_checkpoint(value):
        return None
    return value


def conversation_change_status(
    tool: str,
    native: NativeRef,
    checkpoint: Checkpoint | None,
    *,
    availability: Availability | str | None = None,
) -> str:
    """Classify semantic activity, instability, or unavailable harness support."""
    if tool not in REGISTRY:
        return "unknown"
    if not is_checkpoint(checkpoint):
        return "unstable"
    resolved_availability = (
        native_session_availability(tool, native)
        if availability is None
        else native_session_availability_value(availability)
    )
    if resolved_availability != "available":
        return "unstable" if resolved_availability == "unavailable" else "unknown"
    classifier = REGISTRY.get(tool).change_status
    if isinstance(classifier, Unsupported):
        return "unsupported"
    try:
        status = classifier(native, checkpoint)
    except Exception:
        return "unstable"
    if status not in ("unchanged", "changed", "unstable", "unknown"):
        return "unstable"
    return status


def native_session_availability_value(value: object) -> str:
    """Normalize an availability value, failing closed for malformed hooks."""
    try:
        return Availability(value).value
    except (TypeError, ValueError):
        return "unknown"


def conversation_changed_since(
    tool: str,
    native: NativeRef,
    checkpoint: Checkpoint | None,
) -> bool:
    """Compatibility predicate for callers that do not need instability detail."""
    return conversation_change_status(tool, native, checkpoint) != "unchanged"


def bridged_title(title: str, source_tool: str) -> str:
    """Name the copy so the list never shows two identical rows."""
    source = harness(source_tool).short_label
    base = (title or "untitled session").strip()
    suffix = f" (from {source})"
    return base[: 200 - len(suffix)] + suffix


def complete_jsonl_cursor(path: Path) -> int:
    """Return the byte offset after the last complete record currently on disk."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if not size:
                return 0
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size
            position = size
            while position:
                start = max(0, position - 64 * 1024)
                handle.seek(start)
                block = handle.read(position - start)
                newline = block.rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                position = start
            return 0
    except OSError as error:
        raise BridgeError(f"could not inspect {path}: {error}") from error


def bridge(
    *,
    source_tool: str,
    target_tool: str,
    session_id: str,
    storage: str,
    cwd: str,
    title: str = "",
    budget: Budget | None = None,
    prepared_target: PreparedTarget | None = None,
    target_command: tuple[str, ...] | None = None,
    tool_calls: bool = True,
    latest_window: bool = True,
    conversation_id: str = "",
    allow_lossy: bool = False,
) -> BridgeResult:
    """Materialise a conversation as a new native session in ``target_tool``."""
    target = harness(target_tool)
    if target_tool == source_tool:
        raise BridgeError("that session already belongs to this harness")
    harness(source_tool)
    prepared = prepared_target or prepare_target(target_tool, command=target_command, cwd=cwd)
    if prepared_target is not None and target_command is not None:
        if prepared.command != target_command:
            raise BridgeError("prepared target does not match the configured command")
    applied_budget = budget or resolve_budget(target_tool, policy=prepared.budget_policy)
    if applied_budget.target != target_tool:
        raise BridgeError("the resolved bridge budget belongs to a different target")
    if prepared.budget_policy.hard_limit:
        token_limit = math.floor(
            prepared.budget_policy.context_tokens * prepared.budget_policy.usable_fraction
        )
        character_limit = math.floor(token_limit * prepared.budget_policy.chars_per_token)
        if applied_budget.tokens > token_limit or applied_budget.chars > character_limit:
            raise BridgeError(
                f"the applied bridge budget exceeds the prepared {target.label} target limit; "
                "choose a larger target model or lower the budget"
            )
    source_ref = NativeRef(session_id, storage)
    captured = read_snapshot(
        source_tool,
        source_ref,
        tool_calls=tool_calls,
        latest_window=latest_window,
    )
    source_checkpoint = captured.checkpoint
    source = captured.transcript
    change_status = conversation_change_status(source_tool, source_ref, source_checkpoint)
    if change_status != "unchanged":
        raise BridgeError(
            "the source transcript changed or became incomplete while it was being read; "
            "wait for the current write to finish and retry"
        )
    turns = source.turns
    if not turns:
        raise BridgeError("the source transcript holds no conversation to carry over")
    compacted = 0
    if latest_window:
        turns, compacted = from_last_compaction(turns)
    flattened = flatten(turns)
    conversation_limit = applied_budget.chars - HANDOFF_NOTE_RESERVE_CHARS
    selected = select_messages(flattened, conversation_limit)
    if not allow_lossy and (selected.dropped or selected.truncated):
        projected_context_chars = selection_cost(flattened)
        required_chars = projected_context_chars + HANDOFF_NOTE_RESERVE_CHARS
        required_tokens = (
            required_chars * applied_budget.tokens + applied_budget.chars - 1
        ) // applied_budget.chars
        raise BridgeError(
            f"refusing lossy bridge from {harness(source_tool).label} to {target.label}: "
            f"source message count {len(flattened):,}; projected context cost "
            f"{projected_context_chars:,} characters ({required_chars:,} including the "
            f"{HANDOFF_NOTE_RESERVE_CHARS:,}-character handoff reserve). The applied budget is "
            f"{applied_budget.tokens:,} tokens / {applied_budget.chars:,} characters; "
            f"selection would drop {selected.dropped:,} message(s) and truncate "
            f"{selected.truncated:,} anchor message(s). Increase the bridge budget to at "
            f"least {required_tokens:,} max_tokens (or {required_chars:,} max_chars), "
            "or set bridge.allow_lossy = true to explicitly permit loss."
        )
    kept = selected.turns
    assembled_count = sum(
        1 for index, turn in enumerate(kept) if index == 0 or turn.role != kept[index - 1].role
    )
    summarised = sum(len(turn.calls) for turn in kept)
    note = handoff_note(
        source_tool=source_tool,
        target_tool=target_tool,
        session_id=session_id,
        title=title,
        cwd=cwd,
        kept=len(kept),
        assembled=assembled_count,
        calls=summarised,
        dropped=selected.dropped,
        truncated=selected.truncated,
        compacted=max(compacted, source.carried_windows),
        opaque_compactions=source.opaque_compactions,
        sealed_summary=source.sealed_summary,
        resumes_at_last_summary=source.resumes_at_last_summary or not source.carried_windows,
        conversation_id=conversation_id,
        budget=applied_budget,
        target_context=prepared.handoff_context,
    )
    if len(note) + 2 > HANDOFF_NOTE_RESERVE_CHARS:
        raise BridgeError("the required handoff note exceeds its reserved bridge budget")
    payload = merge_runs([Turn("user", note), *kept])
    if sum(len(turn.text) for turn in payload) > applied_budget.chars:
        raise BridgeError("the assembled bridge payload exceeds its applied budget")
    writer = target.write
    if isinstance(writer, Unsupported):
        raise BridgeError(f"{target.label} cannot write native transcripts: {writer.reason}")
    try:
        written = writer(
            cwd=cwd,
            turns=payload,
            title=bridged_title(title, source_tool),
            prepared=prepared,
        )
    except (OSError, ValueError) as error:
        raise BridgeError(f"could not write the {target.label} target session: {error}") from error
    if (
        not isinstance(written, NativeWrite)
        or not isinstance(written.native, NativeRef)
        or (written.checkpoint is not None and not is_checkpoint(written.checkpoint))
        or not isinstance(written.notices, tuple)
        or not all(isinstance(notice, str) for notice in written.notices)
    ):
        raise BridgeError(f"{target.label} returned an invalid native write result")
    return BridgeResult(
        tool=target_tool,
        native=written.native,
        turns=len(kept),
        written_turns=assembled_count,
        calls=summarised,
        dropped=selected.dropped,
        truncated=selected.truncated,
        source_checkpoint=source_checkpoint,
        target_checkpoint=written.checkpoint,
        budget=applied_budget,
        notices=(*prepared.notices, *written.notices),
    )
