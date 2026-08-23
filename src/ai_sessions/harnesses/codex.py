"""Codex CLI harness adapter."""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities import HarnessAdapter
from ..conversion import (
    CODEX_BUDGET_CONTEXT_TOKENS,
    CODEX_CONTEXT_WINDOW,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_USABLE_FRACTION,
    BridgeError,
    _block_text,
    _Conversation,
    _digest_arguments,
    _digest_codex_input,
    _iso,
    _records,
    _records_after,
    append_jsonl,
    scrub,
)
from ..model import BudgetPolicy, SourceKind, Transcript, Turn
from ..paths import CODEX_HOME
from ..registry import REGISTRY

CODEX_CLI_VERSION = "0.147.0"
_RESULT_NOISE = re.compile(r"^Script completed\s*(Wall time[^\n]*)?\s*Output:\s*", re.I)


def uuid7() -> str:
    """Create the time-ordered UUID shape Codex assigns to native sessions."""
    milliseconds = int(time.time() * 1000)
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = milliseconds.to_bytes(6, "big")
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def _codex_window_turns(payload: dict[str, Any]) -> tuple[list[Turn], bool] | None:
    """Read the plaintext replacement context while disclosing its sealed summary."""
    history = payload.get("replacement_history")
    if not isinstance(history, list) or not history:
        return None
    carried: list[Turn] = []
    sealed = False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("type") not in ("message", "compaction"):
            continue
        if entry.get("type") == "compaction":
            sealed = True
            continue
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue
        text = scrub(_block_text(entry.get("content")))
        if text:
            carried.append(Turn(role, text))
    if not carried:
        return None
    return [replace(carried[0], compaction=True), *carried[1:]], sealed


def read_codex(path: Path, *, latest_window: bool = True) -> Transcript:
    """Read Codex response items and honor replacement-history window semantics.

    With ``latest_window`` the newest carried context replaces superseded history;
    without it each window is appended as a marked boundary for whole-log replay.
    """
    conversation = _Conversation()
    for record in _records(path):
        if record.get("type") == "compacted":
            payload = record.get("payload")
            window = _codex_window_turns(payload) if isinstance(payload, dict) else None
            if window is None:
                conversation.opaque_compaction()
            elif latest_window:
                conversation.carry_window(*window)
            else:
                conversation.append_window(*window)
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
            request = (
                _digest_arguments(payload.get("arguments"))
                if kind == "function_call"
                else _digest_codex_input(payload.get("input"))
            )
            conversation.call(
                str(payload.get("call_id", "")), str(payload.get("name", "")), request
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            text = output if isinstance(output, str) else _block_text(output)
            conversation.result(
                str(payload.get("call_id", "")), _RESULT_NOISE.sub("", text.strip())
            )
    return Transcript(
        conversation.finish(),
        conversation.opaque_compactions,
        conversation.carried_windows,
        conversation.sealed_summary,
        conversation.resumes_at_last_summary,
    )


def _exchanges(turns: list[Turn]) -> list[list[Turn]]:
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


def write_codex_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write context, TUI events, and task wrappers required for native resume.

    ``response_item`` alone loads model context but leaves Codex scrollback blank,
    so each exchange also receives user/agent events and a task wrapper.
    """
    session_id = uuid7()
    moment = time.time() if created is None else created
    local = time.localtime(moment)
    home = REGISTRY.get("codex").home
    directory = home / "sessions" / time.strftime("%Y/%m/%d", local)
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
            stamped = started + offset / 10
            add(
                "response_item",
                {
                    "type": "message",
                    "id": "msg_" + str(uuid.uuid4()),
                    "role": turn.role,
                    "content": [
                        {"type": "input_text" if user else "output_text", "text": turn.text}
                    ],
                },
                stamped,
            )
            event: dict[str, Any]
            if user:
                event = {"type": "user_message", "message": turn.text}
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
            home / "session_index.jsonl",
            {"id": session_id, "thread_name": title, "updated_at": _iso(moment)},
        )
    return session_id, path


def _codex_exists(session_id: str) -> bool:
    return any((REGISTRY.get("codex").home / "sessions").rglob(f"rollout-*-{session_id}.jsonl"))


def _codex_change_status(path: Path, offset: int) -> str:
    """Ignore UI metadata while treating messages and tool traffic as work."""
    changed = False
    for record in _records_after(path, offset):
        if record.get("type") in (
            "ai_sessions_replaced",
            "ai_sessions_missing",
            "ai_sessions_incomplete",
        ):
            return "unstable"
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "message" and payload.get("role") in ("user", "assistant"):
            changed = True
        if kind in (
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
        ):
            changed = True
    return "changed" if changed else "unchanged"


def publish_name(session: Any, name: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    append_jsonl(
        REGISTRY.get("codex").home / "session_index.jsonl",
        {"id": session.session_id, "thread_name": name, "updated_at": stamp},
    )
    return ""


def resume_args(
    *,
    session_id: str,
    source: SourceKind,
    resume_id: str,
    parent_id: str,
    native: bool,
) -> list[str]:
    del parent_id
    target = resume_id if native and source is SourceKind.SUBAGENT else session_id
    result = ["resume"]
    if native and (
        source is SourceKind.NON_INTERACTIVE
        or (source is SourceKind.SUBAGENT and resume_id == session_id)
    ):
        result.append("--include-non-interactive")
    result.append(target)
    return result


ADAPTER = HarnessAdapter(
    name="codex",
    label="Codex",
    short_label="Codex",
    order=10,
    home=CODEX_HOME,
    default_command=("codex",),
    dangerous_args=("--dangerously-bypass-approvals-and-sandbox",),
    source_kinds=frozenset(
        (SourceKind.INTERACTIVE, SourceKind.NON_INTERACTIVE, SourceKind.SUBAGENT)
    ),
    id_patterns=(
        re.compile(
            rb"019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    read=read_codex,
    write=write_codex_session,
    locate=_codex_exists,
    change_status=_codex_change_status,
    resume_args=resume_args,
    publish_name=publish_name,
    budget=BudgetPolicy(
        context_tokens=CODEX_BUDGET_CONTEXT_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=(
            "declared Codex unknown-model compatibility floor, 2026-08-23; "
            "current flagship model context is larger"
        ),
    ),
)
