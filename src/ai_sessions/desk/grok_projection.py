"""Wrapper-owned projection of closed Grok turns. The model does not write this file."""

from __future__ import annotations

import json
from pathlib import Path

THOUGHTS = frozenset({"agent_thought_chunk", "hook_execution"})


def projection_path(source: Path) -> Path:
    return (
        source.with_name(source.name + ".desk.jsonl")
        if source.name == "updates.jsonl"
        else source.with_suffix(".desk.jsonl")
    )


def _state_path(source: Path) -> Path:
    return projection_path(source).with_suffix(".offset.json")


def sync(source: Path, projection: Path | None = None, *, fault: str = "") -> int:
    """Consume complete lines from the update stream. Return turns written this call."""
    projection = projection or projection_path(source)
    state_path = _state_path(source)
    state = {"offset": 0, "user": "", "assistant": "", "pending_line": None}
    if state_path.is_file():
        state.update(json.loads(state_path.read_text(encoding="utf-8")))
    data = source.read_bytes()
    offset = int(state["offset"])
    if offset > len(data):
        raise ValueError(f"truncated: source {len(data)} bytes, sidecar offset {offset}")
    text = data[offset:].decode("utf-8")
    if text and not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut < 0:
            return 0
        consumed, leftover = text[: cut + 1], len(text) - cut - 1
    else:
        consumed, leftover = text, 0
    written = 0
    user = state.get("user") or ""
    assistant = state.get("assistant") or ""
    for line in consumed.splitlines():
        record = json.loads(line)
        update = (record.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        piece = content.get("text") if isinstance(content.get("text"), str) else ""
        if kind == "user_message_chunk":
            user += piece
        elif kind == "agent_message_chunk":
            assistant += piece
        elif kind in THOUGHTS:
            continue
        elif kind == "turn_completed":
            row = {"type": "turn", "user": user, "assistant": assistant, "source_offset": offset}
            if fault == "before-offset":
                with projection.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                raise SystemExit(0)
            existing = projection.read_text(encoding="utf-8") if projection.is_file() else ""
            marker = json.dumps(row, ensure_ascii=False)
            if marker not in existing:
                with projection.open("a", encoding="utf-8") as handle:
                    handle.write(marker + "\n")
                written += 1
            user, assistant = "", ""
    state = {"offset": len(data) - leftover, "user": user, "assistant": assistant}
    if fault != "before-offset":
        state_path.write_text(json.dumps(state), encoding="utf-8")
    return written


def check(source: Path) -> tuple[int, str]:
    state_path = _state_path(source)
    size = source.stat().st_size
    offset = 0
    if state_path.is_file():
        offset = int(json.loads(state_path.read_text(encoding="utf-8")).get("offset") or 0)
    if offset == size:
        return 0, f"in step: {size} bytes"
    if offset < size:
        return 2, f"stale: source {size} bytes, sidecar offset {offset}"
    return 2, f"truncated: source {size} bytes, sidecar offset {offset}"
