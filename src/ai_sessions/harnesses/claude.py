"""Claude Code harness adapter."""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from ..capabilities import HarnessAdapter
from ..conversion import (
    CLAUDE_BUDGET_CONTEXT_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_USABLE_FRACTION,
    BridgeError,
    _block_text,
    _Conversation,
    _digest_arguments,
    _iso,
    _records,
    _records_after,
    append_jsonl,
    scrub,
)
from ..model import BudgetPolicy, SourceKind, Transcript, Turn
from ..paths import CLAUDE_HOME
from ..registry import REGISTRY

CLAUDE_VERSION = "2.1.227"


def claude_project_dir(cwd: str) -> Path:
    """Resolve Claude's cwd-flattened project directory from the live adapter."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd or str(Path.home()))
    return REGISTRY.get("claude").home / "projects" / slug


def read_claude(path: Path, *, latest_window: bool = True) -> Transcript:
    """Read mainline Claude messages, using sidechain only for an agent-only file."""
    main = _Conversation()
    sidechain = _Conversation()
    for record in _records(path):
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isMeta"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
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
    del latest_window
    return Transcript(main.finish() or sidechain.finish(), main.opaque_compactions)


def write_claude_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write a native appendable Claude transcript with linked message UUIDs."""
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


def _claude_exists(session_id: str) -> bool:
    return any((REGISTRY.get("claude").home / "projects").rglob(f"{session_id}.jsonl"))


def _claude_change_status(path: Path, offset: int) -> str:
    """Ignore title/metadata appends and classify only semantic messages."""
    changed = False
    for record in _records_after(path, offset):
        if record.get("type") in (
            "ai_sessions_replaced",
            "ai_sessions_missing",
            "ai_sessions_incomplete",
        ):
            return "unstable"
        if record.get("type") not in ("user", "assistant") or record.get("isMeta"):
            continue
        message = record.get("message")
        if isinstance(message, dict) and message.get("content") not in (None, "", []):
            changed = True
    return "changed" if changed else "unchanged"


def publish_name(session: Any, name: str) -> str:
    transcript = Path(session.storage)
    if not transcript.is_file():
        return f"transcript for {session.session_id} is missing; name kept local only"
    append_jsonl(
        transcript,
        {"type": "custom-title", "customTitle": name, "sessionId": session.session_id},
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
    del source, resume_id, parent_id, native
    return ["--resume", session_id]


ADAPTER = HarnessAdapter(
    name="claude",
    label="Claude Code",
    short_label="Claude",
    order=20,
    home=CLAUDE_HOME,
    default_command=("claude",),
    dangerous_args=("--dangerously-skip-permissions",),
    source_kinds=frozenset((SourceKind.INTERACTIVE, SourceKind.SDK, SourceKind.SUBAGENT)),
    id_patterns=(
        re.compile(
            rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    read=read_claude,
    write=write_claude_session,
    locate=_claude_exists,
    change_status=_claude_change_status,
    resume_args=resume_args,
    publish_name=publish_name,
    budget=BudgetPolicy(
        context_tokens=CLAUDE_BUDGET_CONTEXT_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=(
            "Claude 200k unknown-model floor from official context-window docs, "
            "2026-08-23; Opus 5 context is larger"
        ),
    ),
)
