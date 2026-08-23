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
from typing import Any, Iterable, Iterator

from .capabilities import HarnessAdapter, Unsupported
from .model import (
    DEFAULT_MAX_CHARS,
    HANDOFF_NOTE_RESERVE_CHARS,
    MIN_BRIDGE_CHARS,
    MIN_BUDGET_TOKENS,
    TRUNCATION_MARKER,
    BridgeResult,
    Budget,
    SelectionMetric,
    SelectionResult,
    ToolCall,
    Transcript,
    Turn,
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
CLAUDE_BUDGET_CONTEXT_TOKENS = 200_000
CODEX_BUDGET_CONTEXT_TOKENS = 256_000
DEFAULT_USABLE_FRACTION = 0.75
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


def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as error:
        raise BridgeError(f"could not read {path}: {error}") from error
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
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
    tool: str, storage: str | Path, *, tool_calls: bool = True, latest_window: bool = True
) -> Transcript:
    """Extract the conversation from a transcript, oldest first.

    Turns come back structured, with tool calls still attached, so callers
    can count or inspect them before ``prepare`` renders them to text.
    """
    path = Path(storage)
    if not storage or not path.is_file():
        raise BridgeError("the source transcript is missing, so there is nothing to carry over")
    reader = harness(tool).read
    if isinstance(reader, Unsupported):
        raise BridgeError(f"{harness(tool).label} cannot read native transcripts: {reader.reason}")
    result = reader(path, latest_window=latest_window)
    turns = result.turns
    if not tool_calls:
        turns = [replace(turn, calls=()) for turn in turns]
    return Transcript(
        [turn for turn in turns if turn.text or turn.calls],
        result.opaque_compactions,
        result.carried_windows,
        result.sealed_summary,
        result.resumes_at_last_summary,
    )


def read_turns(tool: str, storage: str | Path, *, tool_calls: bool = True) -> list[Turn]:
    """The conversation alone, for callers that do not need the rest."""
    return read_transcript(tool, storage, tool_calls=tool_calls).turns


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


def _records_after(path: Path, offset: int) -> Iterator[dict[str, Any]]:
    """Yield complete JSONL records appended after a recorded byte frontier."""
    try:
        size = path.stat().st_size
        if offset < 0 or size < offset:
            # Provider transcripts are append-only. Shrinking means the native
            # history was replaced, which is necessarily a new revision.
            yield {"type": "ai_sessions_replaced"}
            return
        with path.open("rb") as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    yield {"type": "ai_sessions_incomplete"}
                    continue
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    yield {"type": "ai_sessions_incomplete"}
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        yield {"type": "ai_sessions_missing"}


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
) -> Budget:
    """Resolve config into the one target budget used by the bridge."""
    policy = harness(target).budget
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


def native_session_exists(tool: str, session_id: str) -> bool:
    """Whether ``tool`` still has a session with this id on disk.

    Cross-provider references are recovered by matching id-shaped strings out
    of transcripts, so they also pick up unrelated identifiers that share the
    shape -- a correlation id pasted into a conversation looks exactly like a
    Codex session id.  Sessions also get deleted.  Either way a reference has
    to be checked before a CLI is asked to resume it.
    """
    if not session_id or tool not in REGISTRY:
        return False
    try:
        locator = REGISTRY.get(tool).locate
        if isinstance(locator, Unsupported):
            return False
        return locator(session_id)
    except OSError:
        return False


@functools.lru_cache(maxsize=2048)
def _snapshot_change_status(
    tool: str,
    generation: int,
    storage: str,
    offset: int,
    device: int,
    inode: int,
    size: int,
    mtime_ns: int,
) -> str:
    """Classify one immutable file snapshot once per process."""
    del generation, device, inode, size, mtime_ns
    classifier = harness(tool).change_status
    if isinstance(classifier, Unsupported):
        return "unsupported"
    status = classifier(Path(storage), offset)
    if status == "unstable":
        # lru_cache does not retain exceptions. A sharing violation or other
        # transient read failure must be retried even if file metadata did not
        # change while access was unavailable.
        raise _UnstableSnapshot
    return status


class _UnstableSnapshot(RuntimeError):
    """Internal signal that a snapshot must not enter the status cache."""


def conversation_change_status(tool: str, storage: str | Path, offset: int) -> str:
    """Classify semantic activity, instability, or unavailable harness support."""
    if tool not in REGISTRY:
        return "unknown"
    path = Path(storage)
    if not storage or not path.is_file():
        return "unstable"
    try:
        stat = path.stat()
    except OSError:
        return "unstable"
    try:
        return _snapshot_change_status(
            tool,
            REGISTRY.generation,
            str(path),
            offset,
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
    except _UnstableSnapshot:
        return "unstable"


def conversation_changed_since(tool: str, storage: str | Path, offset: int) -> bool:
    """Compatibility predicate for callers that do not need instability detail."""
    return conversation_change_status(tool, storage, offset) != "unchanged"


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
    tool_calls: bool = True,
    latest_window: bool = True,
    conversation_id: str = "",
) -> BridgeResult:
    """Materialise a conversation as a new native session in ``target_tool``."""
    target = harness(target_tool)
    if target_tool == source_tool:
        raise BridgeError("that session already belongs to this harness")
    harness(source_tool)
    applied_budget = budget or resolve_budget(target_tool)
    if applied_budget.target != target_tool:
        raise BridgeError("the resolved bridge budget belongs to a different target")
    # This is deliberately captured before reading and aligned to a complete
    # JSONL record. If the live source is appended while the bridge is
    # materialising it, an earlier frontier may cause one conservative
    # re-copy; a later or partial EOF could mark unread work as already carried.
    source_cursor = complete_jsonl_cursor(Path(storage))
    source = read_transcript(
        source_tool, storage, tool_calls=tool_calls, latest_window=latest_window
    )
    change_status = conversation_change_status(source_tool, storage, source_cursor)
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
    )
    if len(note) + 2 > HANDOFF_NOTE_RESERVE_CHARS:
        raise BridgeError("the required handoff note exceeds its reserved bridge budget")
    payload = merge_runs([Turn("user", note), *kept])
    if sum(len(turn.text) for turn in payload) > applied_budget.chars:
        raise BridgeError("the assembled bridge payload exceeds its applied budget")
    writer = target.write
    if isinstance(writer, Unsupported):
        raise BridgeError(f"{target.label} cannot write native transcripts: {writer.reason}")
    new_id, path = writer(cwd=cwd, turns=payload, title=bridged_title(title, source_tool))
    return BridgeResult(
        tool=target_tool,
        session_id=new_id,
        path=path,
        turns=len(kept),
        written_turns=assembled_count,
        calls=summarised,
        dropped=selected.dropped,
        truncated=selected.truncated,
        source_cursor=source_cursor,
        budget=applied_budget,
    )
