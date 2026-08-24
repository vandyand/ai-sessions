"""Pure OpenCode conversation semantics shared by SQLite reads and status checks.

Compaction selection is ported from OpenCode 1.18.21's ``MessageV2.filterCompacted`` behavior:
https://github.com/anomalyco/opencode/blob/3a31c4ea801915c0b050df4b3842997ea62b6e93/packages/opencode/src/session/message-v2.ts
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Iterable

from ..conversion import (
    RESULT_CHARS,
    BridgeError,
    _clip,
    _digest_arguments,
    scrub,
)
from ..model import ToolCall, Transcript, Turn

CHECKPOINT_SCHEME = "sqlite-semantic-v1:"
COMPACTION_PROMPT = "What did we do so far?"
INTERRUPTED_RESULT = "[Tool execution was interrupted]"
PLACEHOLDER_CHARS = 500

PART_KINDS = frozenset(
    {
        "text",
        "subtask",
        "reasoning",
        "file",
        "tool",
        "step-start",
        "step-finish",
        "snapshot",
        "patch",
        "agent",
        "retry",
        "compaction",
    }
)
MESSAGE_ROLES = frozenset(("user", "assistant"))
FILE_SOURCE_TYPES = frozenset(("file", "symbol", "resource"))
TOOL_STATE_STATUSES = frozenset(("pending", "running", "completed", "error"))
BOOKKEEPING_PART_KINDS = frozenset(
    {"reasoning", "step-start", "step-finish", "snapshot", "patch", "retry"}
)


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """One hydrated OpenCode message with provider row ordering preserved."""

    message_id: str
    time_created: int
    info: dict[str, Any]
    parts: tuple[dict[str, Any], ...]


def _fail(label: str) -> BridgeError:
    return BridgeError(f"OpenCode semantic data is invalid: {label}")


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise _fail(f"{label} must be a {'string' if empty else 'non-empty string'}")
    return value


def _required_string(value: dict[str, Any], key: str, label: str, *, empty: bool = False) -> str:
    return _string(value.get(key), f"{label}.{key}", empty=empty)


def _optional_string(value: dict[str, Any], key: str, label: str) -> str | None:
    if key not in value or value[key] is None:
        return None
    return _required_string(value, key, label, empty=True)


def _required_bool(value: dict[str, Any], key: str, label: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise _fail(f"{label}.{key} must be a boolean")
    return result


def _optional_bool(value: dict[str, Any], key: str, label: str) -> bool | None:
    if key not in value or value[key] is None:
        return None
    return _required_bool(value, key, label)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(f"{label} must be an array")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{label} must be a non-negative integer")
    return value


def _canonical(value: Any, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise _fail(f"{label} is not canonical JSON ({error})") from error


def _validate_source(value: Any, label: str) -> dict[str, Any]:
    source = _object(value, label)
    kind = _required_string(source, "type", label)
    if kind not in FILE_SOURCE_TYPES:
        raise _fail(f"{label}.type has unknown value {kind!r}")
    if kind == "file":
        _required_string(source, "path", label, empty=True)
    elif kind == "symbol":
        _required_string(source, "path", label, empty=True)
        _required_string(source, "name", label, empty=True)
        _object(source.get("range"), f"{label}.range")
        _nonnegative_integer(source.get("kind"), f"{label}.kind")
    elif kind == "resource":
        _required_string(source, "clientName", label, empty=True)
        _required_string(source, "uri", label, empty=True)
    text = _object(source.get("text"), f"{label}.text")
    _required_string(text, "value", f"{label}.text", empty=True)
    for key in ("start", "end"):
        coordinate = text.get(key)
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise _fail(f"{label}.text.{key} must be finite")
        if not math.isfinite(coordinate):
            raise _fail(f"{label}.text.{key} must be finite")
    return source


def _validate_file(part: dict[str, Any], label: str) -> dict[str, Any]:
    _required_string(part, "mime", label, empty=True)
    _required_string(part, "url", label, empty=True)
    _optional_string(part, "filename", label)
    if part.get("source") is not None:
        _validate_source(part["source"], f"{label}.source")
    return part


def _validate_time(value: Any, label: str, *keys: str) -> dict[str, Any]:
    result = _object(value, label)
    for key in keys:
        _nonnegative_integer(result.get(key), f"{label}.{key}")
    if "compacted" in result:
        _nonnegative_integer(result["compacted"], f"{label}.compacted")
    return result


def _tool_semantics(part: dict[str, Any], label: str) -> dict[str, Any]:
    call_id = _required_string(part, "callID", label)
    name = _required_string(part, "tool", label)
    state = _object(part.get("state"), f"{label}.state")
    status = _required_string(state, "status", f"{label}.state")
    if status not in TOOL_STATE_STATUSES:
        raise _fail(f"{label}.state.status has unknown value {status!r}")
    arguments = _object(state.get("input"), f"{label}.state.input")
    semantic: dict[str, Any] = {
        "id": _required_string(part, "id", label),
        "type": "tool",
        "callID": call_id,
        "tool": name,
        "state": {"status": status, "input": arguments},
    }
    if status == "pending":
        semantic["state"]["raw"] = _required_string(state, "raw", f"{label}.state", empty=True)
    elif status == "running":
        _optional_string(state, "title", f"{label}.state")
        if state.get("metadata") is not None:
            _object(state["metadata"], f"{label}.state.metadata")
        _validate_time(state.get("time"), f"{label}.state.time", "start")
    elif status == "completed":
        semantic["state"]["output"] = _required_string(
            state, "output", f"{label}.state", empty=True
        )
        _required_string(state, "title", f"{label}.state", empty=True)
        _object(state.get("metadata"), f"{label}.state.metadata")
        completed_time = _validate_time(state.get("time"), f"{label}.state.time", "start", "end")
        if "compacted" in completed_time:
            semantic["state"]["compacted"] = completed_time["compacted"]
        if "attachments" in state:
            attachments: list[dict[str, Any]] = []
            for index, attachment in enumerate(
                _array(state["attachments"], f"{label}.state.attachments")
            ):
                attachment_label = f"{label}.state.attachments[{index}]"
                semantic_attachment = _semantic_part(
                    _object(attachment, attachment_label), attachment_label
                )
                if semantic_attachment is None or semantic_attachment["type"] != "file":
                    raise _fail(f"{attachment_label} must be a file part")
                attachments.append(semantic_attachment)
            semantic["state"]["attachments"] = attachments
    elif status == "error":
        semantic["state"]["error"] = _required_string(state, "error", f"{label}.state", empty=True)
        if state.get("metadata") is not None:
            metadata = _object(state["metadata"], f"{label}.state.metadata")
            if metadata.get("interrupted") is True and isinstance(metadata.get("output"), str):
                semantic["state"]["interrupted_output"] = metadata["output"]
        _validate_time(state.get("time"), f"{label}.state.time", "start", "end")
    return semantic


def _semantic_part(part: dict[str, Any], label: str) -> dict[str, Any] | None:
    kind = _required_string(part, "type", label)
    if kind not in PART_KINDS:
        raise _fail(f"{label}.type has unknown value {kind!r}")
    part_id = _required_string(part, "id", label)
    if kind in BOOKKEEPING_PART_KINDS:
        return None
    if kind == "text":
        text = _required_string(part, "text", label, empty=True)
        ignored = _optional_bool(part, "ignored", label) or False
        return {"id": part_id, "type": kind, "text": text, "ignored": ignored}
    if kind == "file":
        _validate_file(part, label)
        result = {
            "id": part_id,
            "type": kind,
            "mime": part["mime"],
            "url": part["url"],
        }
        if part.get("filename") is not None:
            result["filename"] = part["filename"]
        if part.get("source") is not None:
            result["source"] = part["source"]
        return result
    if kind == "agent":
        result = {"id": part_id, "type": kind, "name": _required_string(part, "name", label)}
        if part.get("source") is not None:
            result["source"] = _object(part["source"], f"{label}.source")
        return result
    if kind == "subtask":
        result = {
            "id": part_id,
            "type": kind,
            "prompt": _required_string(part, "prompt", label, empty=True),
            "description": _required_string(part, "description", label, empty=True),
            "agent": _required_string(part, "agent", label),
        }
        if part.get("model") is not None:
            model = _object(part["model"], f"{label}.model")
            _required_string(model, "providerID", f"{label}.model")
            _required_string(model, "modelID", f"{label}.model")
            result["model"] = model
        if part.get("command") is not None:
            result["command"] = _required_string(part, "command", label, empty=True)
        return result
    if kind == "tool":
        return _tool_semantics(part, label)
    if kind == "compaction":
        result = {
            "id": part_id,
            "type": kind,
            "auto": _required_bool(part, "auto", label),
        }
        overflow = _optional_bool(part, "overflow", label)
        if overflow is not None:
            result["overflow"] = overflow
        tail = _optional_string(part, "tail_start_id", label)
        # Upstream treats a falsy tail boundary as no retained tail.
        if tail:
            result["tail_start_id"] = tail
        return result
    raise AssertionError(kind)


def _message_semantics(message: StoredMessage) -> dict[str, Any]:
    message_id = _string(message.message_id, "message.id")
    created = _nonnegative_integer(message.time_created, f"message {message_id}.time_created")
    info = _object(message.info, f"message {message_id}.info")
    role = _required_string(info, "role", f"message {message_id}.info")
    if role not in MESSAGE_ROLES:
        raise _fail(f"message {message_id}.info.role has unknown value {role!r}")
    result: dict[str, Any] = {"id": message_id, "time_created": created, "role": role}
    if role == "assistant":
        result["parentID"] = _required_string(info, "parentID", f"message {message_id}.info")
        summary = _optional_bool(info, "summary", f"message {message_id}.info")
        result["summary"] = bool(summary)
        result["finish"] = _optional_string(info, "finish", f"message {message_id}.info")
        if info.get("error") is not None:
            result["error"] = _object(info["error"], f"message {message_id}.info.error")
        else:
            result["error"] = None
    parts: list[dict[str, Any]] = []
    for index, value in enumerate(message.parts):
        part = _object(value, f"message {message_id}.parts[{index}]")
        semantic = _semantic_part(part, f"message {message_id}.parts[{index}]")
        if semantic is not None:
            parts.append(semantic)
    result["parts"] = parts
    return result


def normalize_revert(revert: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate and retain only the staged boundaries OpenCode cleanup consumes."""
    if revert is None:
        return None
    value = _object(revert, "session.revert")
    result = {"messageID": _required_string(value, "messageID", "session.revert")}
    part_id = _optional_string(value, "partID", "session.revert")
    if part_id is not None:
        if not part_id:
            raise _fail("session.revert.partID must be a non-empty string")
        result["partID"] = part_id
    for key in ("snapshot", "diff"):
        _optional_string(value, key, "session.revert")
    return result


def semantic_checkpoint(messages: Iterable[StoredMessage], revert: dict[str, Any] | None) -> str:
    """Hash complete session-local state that can alter projection or filtering."""
    digest = hashlib.sha256()
    digest.update(b"opencode-sqlite-semantic-v1\n")
    digest.update(_canonical(normalize_revert(revert), "session.revert").encode("utf-8"))
    digest.update(b"\n")
    for message in messages:
        digest.update(_canonical(_message_semantics(message), "message").encode("utf-8"))
        digest.update(b"\n")
    return CHECKPOINT_SCHEME + digest.hexdigest()


def apply_revert(
    messages: list[StoredMessage], revert: dict[str, Any] | None
) -> list[StoredMessage]:
    """Project the staged view that OpenCode cleanup would make persistent."""
    boundary = normalize_revert(revert)
    if boundary is None:
        return list(messages)
    index = next(
        (
            position
            for position, item in enumerate(messages)
            if item.message_id == boundary["messageID"]
        ),
        -1,
    )
    if index < 0:
        return list(messages)
    part_id = boundary.get("partID")
    if part_id is None:
        return list(messages[:index])
    target = messages[index]
    part_index = next(
        (position for position, item in enumerate(target.parts) if item.get("id") == part_id), -1
    )
    # Match SessionRevert.cleanup exactly: a missing part boundary leaves the
    # target message intact but still removes every later message.
    retained = target.parts if part_index < 0 else target.parts[:part_index]
    return [*messages[:index], replace(target, parts=tuple(retained))]


def _compaction_part(message: StoredMessage) -> dict[str, Any] | None:
    return next((part for part in message.parts if part.get("type") == "compaction"), None)


def _completed_summary(message: StoredMessage) -> bool:
    info = message.info
    return (
        info.get("role") == "assistant"
        and info.get("summary") is True
        and isinstance(info.get("finish"), str)
        and bool(info["finish"])
        and info.get("error") is None
    )


@dataclass(frozen=True, slots=True)
class WindowSelection:
    messages: list[StoredMessage]
    governing_compaction_id: str = ""
    summary_id: str = ""
    tail_start_id: str = ""
    unresolved_tail: bool = False


def select_compacted_newest(messages: Iterable[StoredMessage]) -> WindowSelection:
    """Select a neutral window only when a completed OpenCode compaction governs.

    Pinned OpenCode 1.18.21 reorders around any summary attempt. After a failed
    attempt and successful retry, that puts the completed boundary after the
    retained tail; if every attempt fails, it can truncate earlier history even
    though no completed boundary replaces it. We deliberately reorder/truncate
    only around the newest completed summary. Otherwise full history survives.
    """
    result: list[StoredMessage] = []
    completed: dict[str, str] = {}
    retain: str | None = None
    governing_compaction_id = ""
    summary_id = ""
    tail_start_id = ""
    for message in messages:
        result.append(message)
        if retain is not None:
            if message.message_id == retain:
                break
            continue
        if message.info.get("role") == "user" and message.message_id in completed:
            part = _compaction_part(message)
            if part is None:
                continue
            governing_compaction_id = message.message_id
            summary_id = completed[message.message_id]
            if not part.get("tail_start_id"):
                break
            retain = _string(part["tail_start_id"], "compaction.tail_start_id")
            tail_start_id = retain
            if message.message_id == retain:
                break
            continue
        if _completed_summary(message):
            parent_id = _required_string(message.info, "parentID", f"message {message.message_id}")
            completed.setdefault(parent_id, message.message_id)
    result.reverse()
    compaction_index = next(
        (
            index
            for index, message in enumerate(result)
            if message.message_id == governing_compaction_id
        ),
        -1,
    )
    summary_index = next(
        (index for index, message in enumerate(result) if message.message_id == summary_id), -1
    )
    tail_index = next(
        (index for index, message in enumerate(result) if message.message_id == tail_start_id), -1
    )
    if tail_index >= 0 and tail_index < compaction_index and summary_index > compaction_index:
        result = [
            *result[compaction_index : summary_index + 1],
            *result[tail_index:compaction_index],
            *result[summary_index + 1 :],
        ]
    return WindowSelection(
        result,
        governing_compaction_id,
        summary_id,
        tail_start_id,
        bool(tail_start_id) and not (0 <= tail_index <= compaction_index),
    )


def select_compacted(messages: list[StoredMessage]) -> WindowSelection:
    """Select from a chronological materialized conversation."""
    return select_compacted_newest(reversed(messages))


def _summary_ids(messages: list[StoredMessage]) -> set[str]:
    compactions = {
        message.message_id
        for message in messages
        if message.info.get("role") == "user" and _compaction_part(message) is not None
    }
    newest_by_parent: dict[str, str] = {}
    for message in messages:
        parent_id = message.info.get("parentID")
        if _completed_summary(message) and parent_id in compactions:
            newest_by_parent[parent_id] = message.message_id
    return set(newest_by_parent.values())


def _file_placeholder(part: dict[str, Any]) -> str:
    name = part.get("filename")
    if not isinstance(name, str) or not name.strip():
        source = part.get("source")
        if isinstance(source, dict):
            name = source.get("path") or source.get("uri") or source.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "file"
    return _clip(f"[Attached {part.get('mime') or 'file'}: {name}]", PLACEHOLDER_CHARS)


def _subtask_placeholder(part: dict[str, Any]) -> str:
    description = str(part.get("description") or "subtask")
    agent = str(part.get("agent") or "agent")
    prompt = str(part.get("prompt") or "")
    return _clip(
        f"[OpenCode subtask for {agent}: {description}]" + (f"\n{prompt}" if prompt else ""),
        PLACEHOLDER_CHARS,
    )


def _tool_call(part: dict[str, Any]) -> ToolCall:
    state = _object(part["state"], "tool.state")
    status = _required_string(state, "status", "tool.state")
    request = _digest_arguments(state["input"])
    if status == "completed":
        result = (
            "[Old tool result content cleared]"
            if "compacted" in state
            else _clip(state["output"], RESULT_CHARS)
        )
    elif status == "error":
        result = (
            _clip(state["interrupted_output"], RESULT_CHARS)
            if "interrupted_output" in state
            else _clip(f"[Tool error] {state['error']}", RESULT_CHARS)
        )
    else:
        result = INTERRUPTED_RESULT
    return ToolCall(_required_string(part, "tool", "tool"), request, result)


def _project_message(message: StoredMessage, summary_ids: set[str]) -> Turn | None:
    role = _required_string(message.info, "role", f"message {message.message_id}.info")
    text: list[str] = []
    calls: list[ToolCall] = []
    for index, part in enumerate(message.parts):
        semantic = _semantic_part(part, f"message {message.message_id}.parts[{index}]")
        if semantic is None:
            continue
        kind = semantic["type"]
        if kind == "text":
            if role == "user" and semantic["ignored"]:
                continue
            value = scrub(semantic["text"])
            if value:
                text.append(value)
        elif kind == "file":
            text.append(_file_placeholder(semantic))
        elif kind == "subtask":
            text.append(_subtask_placeholder(semantic))
        elif kind == "agent":
            text.append(_clip(f"[OpenCode agent selected: {semantic['name']}]", PLACEHOLDER_CHARS))
        elif kind == "compaction":
            text.append(COMPACTION_PROMPT)
        elif kind == "tool":
            calls.append(_tool_call(semantic))
            state = semantic["state"]
            if "compacted" not in state:
                for attachment in state.get("attachments", []):
                    text.append(_file_placeholder(attachment))
    rendered = "\n\n".join(value for value in text if value)
    if not rendered and not calls:
        return None
    return Turn(
        role,
        rendered,
        tuple(calls),
        compaction=message.message_id in summary_ids and bool(rendered or calls),
    )


def project_transcript(
    messages: list[StoredMessage],
    revert: dict[str, Any] | None,
    *,
    latest_window: bool,
) -> Transcript:
    """Apply staged state, native compaction order, then neutral projection."""
    # Validate the full state even if the staged/latest view would hide a malformed
    # record. Checkpoints and reads must fail over exactly the same semantic domain.
    for message in messages:
        _message_semantics(message)
    staged = apply_revert(messages, revert)
    if latest_window:
        selection = select_compacted(staged)
        return project_view(
            selection.messages,
            resumes_at_last_summary=True,
            summary_ids=frozenset({selection.summary_id}) if selection.summary_id else frozenset(),
        )
    return project_view(staged, resumes_at_last_summary=True)


def project_view(
    messages: list[StoredMessage],
    *,
    resumes_at_last_summary: bool,
    summary_ids: frozenset[str] | None = None,
) -> Transcript:
    """Project an already staged/windowed native view."""
    summaries = _summary_ids(messages) if summary_ids is None else set(summary_ids)
    turns = [
        turn for message in messages if (turn := _project_message(message, summaries)) is not None
    ]
    carried_windows = sum(turn.compaction for turn in turns)
    return Transcript(
        turns,
        carried_windows=carried_windows,
        resumes_at_last_summary=bool(carried_windows) and resumes_at_last_summary,
    )
