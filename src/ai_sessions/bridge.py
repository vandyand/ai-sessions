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
harness.  ``HARNESSES`` is where a new one is registered; note that this
covers bridging only, and that listing and open-session detection remain
provider-specific in ``app.py``.

Tool calls have no portable representation -- a replayed ``tool_use`` block
would name tools the target does not have, and would need a matching result
to stay valid -- so they cross over summarised as text rather than as live
calls.  That keeps what was run and what it returned, which is usually the
part worth having, without inventing structure the target cannot honour.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .paths import CLAUDE_HOME, CODEX_HOME

# A bridged transcript is replayed into the target's context window in full,
# so the ceiling is a context budget rather than a storage one.
DEFAULT_MAX_CHARS = 950_000
REQUEST_CHARS = 300
RESULT_CHARS = 500
CLAUDE_VERSION = "2.1.227"
CODEX_CLI_VERSION = "0.147.0"
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
_CODEX_RESULT_NOISE = re.compile(r"^Script completed\s*(Wall time[^\n]*)?\s*Output:\s*", re.I)

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


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    request: str
    result: str = ""


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    text: str
    calls: tuple[ToolCall, ...] = ()
    # A turn that supersedes everything before it: the harness ran out of
    # context, summarised the conversation so far, and carried on from the
    # summary.  Resuming from here is what the source session itself does.
    compaction: bool = False


@dataclass(frozen=True, slots=True)
class BridgeResult:
    tool: str
    session_id: str
    path: Path
    turns: int
    calls: int
    dropped: int


@dataclass(frozen=True, slots=True)
class Transcript:
    """A conversation read out of one harness, ready to be written to another."""

    turns: list[Turn]
    # Compactions whose summary the source harness stores unreadably *and*
    # which carried no readable context either, so the copy has to fall back
    # to the pre-compaction history.  Reported so the copy's size is explainable.
    opaque_compactions: int = 0
    # Compactions that did carry readable context.  Only the newest window
    # survives in ``turns``; this is how many the source actually ran.
    carried_windows: int = 0
    # Whether the carried window's own summary is sealed by the source, so it
    # could not cross even though the messages around it did.
    sealed_summary: bool = False


@dataclass(frozen=True)
class Harness:
    """Everything bridging needs to know about one CLI."""

    name: str
    label: str
    read: Callable[[Path], Transcript]
    write: Callable[..., tuple[str, Path]]
    locate: Callable[[str], bool]


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


def uuid7() -> str:
    """A time-ordered UUID of the shape Codex assigns to its own sessions."""
    milliseconds = int(time.time() * 1000)
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = milliseconds.to_bytes(6, "big")
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def claude_project_dir(cwd: str) -> Path:
    """Claude stores transcripts per working directory under a flattened name."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd or str(Path.home()))
    return CLAUDE_HOME / "projects" / slug


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
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


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
        self._pending: dict[str, tuple[int, int]] = {}

    def reset(self) -> None:
        """Drop everything accumulated so far, including open tool calls.

        A pending call whose result never arrived must not survive into the
        next window, or that result attaches to an unrelated turn.
        """
        self.turns.clear()
        self._pending.clear()

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

    def opaque_compaction(self) -> None:
        """Note a compaction whose summary cannot be read back."""
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


def _codex_window_turns(payload: dict[str, Any]) -> tuple[list[Turn], bool] | None:
    """The plaintext context a Codex compaction carries forward.

    Codex seals its own summary in ``encrypted_content``, but records the
    messages it keeps beside it under a field named for what it does:
    ``replacement_history`` replaces the context before it.  Returns None when
    that field is absent, which is how older rollouts read, and otherwise the
    carried turns plus whether a sealed summary sat among them.
    """
    history = payload.get("replacement_history")
    if not isinstance(history, list) or not history:
        return None
    carried: list[Turn] = []
    sealed = False
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "compaction":
            # The summary itself.  Encrypted by the provider, so not even the
            # source harness can read it back; only its existence is reportable.
            sealed = True
            continue
        role = entry.get("role")
        # `developer` entries are sandbox and permission preamble: harness
        # configuration, and actively misleading once carried somewhere else.
        if role not in ("user", "assistant"):
            continue
        text = scrub(_block_text(entry.get("content")))
        if text:
            carried.append(Turn(role, text))
    if not carried:
        return None
    # Marking the first turn makes from_last_compaction a no-op on this
    # result: it keeps the boundary turn and everything after it.
    return [replace(carried[0], compaction=True), *carried[1:]], sealed


def read_codex(path: Path) -> Transcript:
    conversation = _Conversation()
    for record in _records(path):
        # Codex marks a compaction with a top-level ``compacted`` record.  Its
        # summary is encrypted, but the record also carries the plaintext
        # context the source resumes from, so the window replaces everything
        # before it.  Only when that context is missing is there nothing to
        # resume from, and the surrounding history has to carry the conversation.
        if record.get("type") == "compacted":
            payload = record.get("payload")
            window = _codex_window_turns(payload) if isinstance(payload, dict) else None
            if window is None:
                conversation.opaque_compaction()
            else:
                conversation.carry_window(*window)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                conversation.message(role, scrub(_block_text(payload.get("content"))))
        elif kind in ("function_call", "custom_tool_call"):
            if kind == "function_call":
                request = _digest_arguments(payload.get("arguments"))
            else:
                request = _digest_codex_input(payload.get("input"))
            conversation.call(
                str(payload.get("call_id", "")), str(payload.get("name", "")), request
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            text = output if isinstance(output, str) else _block_text(output)
            conversation.result(
                str(payload.get("call_id", "")), _CODEX_RESULT_NOISE.sub("", text.strip())
            )
    return Transcript(
        conversation.finish(),
        conversation.opaque_compactions,
        conversation.carried_windows,
        conversation.sealed_summary,
    )


def read_claude(path: Path) -> Transcript:
    main = _Conversation()
    sidechain = _Conversation()
    for record in _records(path):
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isMeta"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        # Subagent transcripts are entirely sidechain, so they are collected
        # apart and only used when the file holds nothing else.
        conversation = sidechain if record.get("isSidechain") else main
        content = message.get("content")
        conversation.message(
            role, scrub(_block_text(content)), compaction=bool(record.get("isCompactSummary"))
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                conversation.call(
                    str(block.get("id", "")),
                    str(block.get("name", "")),
                    _digest_arguments(block.get("input")),
                )
            elif block.get("type") == "tool_result":
                body = block.get("content")
                conversation.result(
                    str(block.get("tool_use_id", "")),
                    body if isinstance(body, str) else _block_text(body),
                )
    return Transcript(main.finish() or sidechain.finish(), main.opaque_compactions)


def read_transcript(tool: str, storage: str | Path, *, tool_calls: bool = True) -> Transcript:
    """Extract the conversation from a transcript, oldest first.

    Turns come back structured, with tool calls still attached, so callers
    can count or inspect them before ``prepare`` renders them to text.
    """
    path = Path(storage)
    if not storage or not path.is_file():
        raise BridgeError("the source transcript is missing, so there is nothing to carry over")
    result = harness(tool).read(path)
    turns = result.turns
    if not tool_calls:
        turns = [replace(turn, calls=()) for turn in turns]
    return Transcript(
        [turn for turn in turns if turn.text or turn.calls],
        result.opaque_compactions,
        result.carried_windows,
        result.sealed_summary,
    )


def read_turns(tool: str, storage: str | Path, *, tool_calls: bool = True) -> list[Turn]:
    """The conversation alone, for callers that do not need the rest."""
    return read_transcript(tool, storage, tool_calls=tool_calls).turns


def prepare(turns: Iterable[Turn]) -> list[Turn]:
    """Render tool summaries into text and restore alternating roles."""
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


def fit(turns: list[Turn], max_chars: int = DEFAULT_MAX_CHARS) -> tuple[list[Turn], int]:
    """Trim to a character budget, keeping the opening request and the tail.

    The first turn is what the conversation was *for*, and the tail is where
    it currently stands, so both survive while the middle gives way.
    """
    if max_chars <= 0 or not turns:
        return list(turns), 0
    per_turn = max(1000, max_chars // 2)
    capped = [
        turn
        if len(turn.text) <= per_turn
        else Turn(turn.role, turn.text[:per_turn] + "\n\n[... message truncated ...]", turn.calls)
        for turn in turns
    ]
    if sum(len(turn.text) for turn in capped) <= max_chars:
        return capped, 0
    head, rest = capped[:1], capped[1:]
    budget = max_chars - len(head[0].text)
    tail: list[Turn] = []
    for turn in reversed(rest):
        if len(turn.text) > budget:
            break
        tail.append(turn)
        budget -= len(turn.text)
    tail.reverse()
    return head + tail, len(rest) - len(tail)


def handoff_note(
    *,
    source_tool: str,
    target_tool: str,
    session_id: str,
    title: str,
    cwd: str,
    kept: int,
    calls: int,
    dropped: int,
    compacted: int = 0,
    opaque_compactions: int = 0,
    sealed_summary: bool = False,
) -> str:
    """The opening message that tells the target where this came from."""
    source = harness(source_tool).label
    target = harness(target_tool).label
    label = f" titled {title!r}" if title else ""
    lines = [
        f"[ai-sessions] This conversation started in {source} and is being continued in {target}.",
        "",
        f"Source session{label}: {session_id} ({source})",
        f"Working directory: {cwd or 'unknown'}",
        "",
        f"The {kept} message(s) below are the real conversation, replayed as plain text.",
    ]
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
        lines += [
            "",
            f"The source ran out of context {compacted} time(s) and summarised itself. What "
            "follows starts at the most recent of those summaries, which is where the source "
            "session itself is picking up; earlier windows are in its transcript but not here.",
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
            "the context budget. The opening request and the most recent exchanges were kept.",
        ]
    lines += ["", "Pick up from here as though the conversation had always been yours."]
    return "\n".join(lines)


def write_claude_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write a resumable Claude Code transcript and return its id and path."""
    session_id = str(uuid.uuid4())
    moment = time.time() if created is None else created
    directory = claude_project_dir(cwd)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BridgeError(f"could not create {directory}: {error}") from error
    path = directory / f"{session_id}.jsonl"
    lines: list[str] = []
    parent: str | None = None
    for index, turn in enumerate(turns):
        line_id = str(uuid.uuid4())
        record: dict[str, Any] = {
            "parentUuid": parent,
            "isSidechain": False,
            "type": turn.role,
            "uuid": line_id,
            "timestamp": _iso(moment + index),
            "cwd": cwd,
            "sessionId": session_id,
            "version": CLAUDE_VERSION,
            "userType": "external",
        }
        if turn.role == "user":
            record["message"] = {"role": "user", "content": turn.text}
            record["origin"] = {"kind": "human"}
        else:
            record["message"] = {
                "id": "msg_" + secrets.token_hex(12),
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": turn.text}],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        parent = line_id
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as error:
        raise BridgeError(f"could not write {path}: {error}") from error
    if title:
        append_jsonl(path, {"type": "custom-title", "customTitle": title, "sessionId": session_id})
    return session_id, path


def write_codex_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write a resumable Codex rollout and return its id and path."""
    session_id = uuid7()
    moment = time.time() if created is None else created
    local = time.localtime(moment)
    directory = CODEX_HOME / "sessions" / time.strftime("%Y/%m/%d", local)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BridgeError(f"could not create {directory}: {error}") from error
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", local)
    path = directory / f"rollout-{stamp}-{session_id}.jsonl"

    records: list[dict[str, Any]] = [
        {
            "timestamp": _iso(moment),
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": _iso(moment),
                "cwd": cwd,
                "originator": "codex-tui",
                "cli_version": CODEX_CLI_VERSION,
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
            },
        }
    ]

    def add(kind: str, payload: dict[str, Any], at: float) -> None:
        records.append(
            {"timestamp": _iso(at), "ordinal": len(records), "type": kind, "payload": payload}
        )

    # Codex keeps three things apart: what the model sees (``response_item``),
    # what the TUI redraws (``user_message``/``agent_message`` events), and
    # the turn both belong to.  A copy written with only the first kind
    # resumes with the context loaded but the scrollback blank, so all three
    # are emitted.  The events are also what Codex counts user turns from.
    for index, group in enumerate(_exchanges(turns)):
        turn_id = uuid7()
        started = moment + index
        add(
            "event_msg",
            {
                "type": "task_started",
                "turn_id": turn_id,
                "started_at": int(started),
                "model_context_window": CODEX_CONTEXT_WINDOW,
                "collaboration_mode_kind": "default",
            },
            started,
        )
        for offset, turn in enumerate(group, start=1):
            user = turn.role == "user"
            message_id = "msg_" + str(uuid.uuid4())
            stamped = started + offset / 10
            add(
                "response_item",
                {
                    "type": "message",
                    "id": message_id,
                    "role": turn.role,
                    "content": [
                        {
                            "type": "input_text" if user else "output_text",
                            "text": turn.text,
                        }
                    ],
                },
                stamped,
            )
            if user:
                event: dict[str, Any] = {"type": "user_message", "message": turn.text}
            else:
                event = {
                    "type": "agent_message",
                    "message": turn.text,
                    "phase": "commentary",
                    "memory_citation": None,
                }
            add("event_msg", event, stamped)
        replies = [turn.text for turn in group if turn.role == "assistant"]
        add(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": replies[-1] if replies else "",
            },
            started + 1,
        )
    body = "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records
    )
    try:
        path.write_text(body + "\n", encoding="utf-8")
    except OSError as error:
        raise BridgeError(f"could not write {path}: {error}") from error
    if title:
        append_jsonl(
            CODEX_HOME / "session_index.jsonl",
            {"id": session_id, "thread_name": title, "updated_at": _iso(moment)},
        )
    return session_id, path


def _exchanges(turns: list[Turn]) -> list[list[Turn]]:
    """Group an alternating conversation into Codex turns.

    A Codex turn is one user request and the reply it produced, which is the
    unit its history projection stores and its TUI redraws.
    """
    groups: list[list[Turn]] = []
    index = 0
    while index < len(turns):
        group = [turns[index]]
        following = index + 1
        if (
            turns[index].role == "user"
            and following < len(turns)
            and turns[following].role == "assistant"
        ):
            group.append(turns[following])
            index = following
        index += 1
        groups.append(group)
    return groups


def _claude_exists(session_id: str) -> bool:
    return any((CLAUDE_HOME / "projects").rglob(f"{session_id}.jsonl"))


def _codex_exists(session_id: str) -> bool:
    return any((CODEX_HOME / "sessions").rglob(f"rollout-*-{session_id}.jsonl"))


HARNESSES: dict[str, Harness] = {
    "codex": Harness(
        name="codex",
        label="Codex",
        read=read_codex,
        write=write_codex_session,
        locate=_codex_exists,
    ),
    "claude": Harness(
        name="claude",
        label="Claude Code",
        read=read_claude,
        write=write_claude_session,
        locate=_claude_exists,
    ),
}
BRIDGE_TOOLS = tuple(HARNESSES)
TOOL_NAMES = {name: entry.label for name, entry in HARNESSES.items()}


def harness(name: str) -> Harness:
    try:
        return HARNESSES[name]
    except KeyError:
        raise BridgeError(f"unknown harness: {name}") from None


def native_session_exists(tool: str, session_id: str) -> bool:
    """Whether ``tool`` still has a session with this id on disk.

    Cross-provider references are recovered by matching id-shaped strings out
    of transcripts, so they also pick up unrelated identifiers that share the
    shape -- a correlation id pasted into a conversation looks exactly like a
    Codex session id.  Sessions also get deleted.  Either way a reference has
    to be checked before a CLI is asked to resume it.
    """
    if not session_id or tool not in HARNESSES:
        return False
    try:
        return HARNESSES[tool].locate(session_id)
    except OSError:
        return False


def bridged_title(title: str, source_tool: str) -> str:
    """Name the copy so the list never shows two identical rows."""
    source = harness(source_tool).label.split()[0]
    base = (title or "untitled session").strip()
    suffix = f" (from {source})"
    return base[: 200 - len(suffix)] + suffix


def bridge(
    *,
    source_tool: str,
    target_tool: str,
    session_id: str,
    storage: str,
    cwd: str,
    title: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    tool_calls: bool = True,
    latest_window: bool = True,
) -> BridgeResult:
    """Materialise a conversation as a new native session in ``target_tool``."""
    target = harness(target_tool)
    if target_tool == source_tool:
        raise BridgeError("that session already belongs to this harness")
    harness(source_tool)
    source = read_transcript(source_tool, storage, tool_calls=tool_calls)
    turns = source.turns
    if not turns:
        raise BridgeError("the source transcript holds no conversation to carry over")
    compacted = 0
    if latest_window:
        turns, compacted = from_last_compaction(turns)
    kept, dropped = fit(prepare(turns), max_chars)
    summarised = sum(len(turn.calls) for turn in kept)
    note = handoff_note(
        source_tool=source_tool,
        target_tool=target_tool,
        session_id=session_id,
        title=title,
        cwd=cwd,
        kept=len(kept),
        calls=summarised,
        dropped=dropped,
        compacted=max(compacted, source.carried_windows),
        opaque_compactions=source.opaque_compactions,
        sealed_summary=source.sealed_summary,
    )
    payload = merge_runs([Turn("user", note), *kept])
    new_id, path = target.write(cwd=cwd, turns=payload, title=bridged_title(title, source_tool))
    return BridgeResult(
        tool=target_tool,
        session_id=new_id,
        path=path,
        turns=len(kept),
        calls=summarised,
        dropped=dropped,
    )
