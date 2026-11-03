"""Deterministic, bounded digest of old target exchanges."""

from __future__ import annotations

import math
from collections import Counter

from ..model import ToolCall
from .index import CHARS_PER_TOKEN, COMPACTION_MARKER, Exchange, call_line, clip, tokens

PROMPT_CHARS = 6000
FINAL_CHARS = 2000
SHORT_PROMPT_CHARS = 500
SHORT_FINAL_CHARS = 300


def digest_entry(
    ex: Exchange, *, prompt_chars: int = PROMPT_CHARS, final_chars: int = FINAL_CHARS
) -> str:
    lines = [f"#{ex.seq} {clip(ex.typed, prompt_chars)}".rstrip()]
    if ex.assistant:
        lines.append("→ " + clip(ex.assistant[-1], final_chars))
    if ex.calls:
        counts = Counter(call.name for call in ex.calls)
        lines.append("tools: " + ", ".join(f"{name} ×{n}" for name, n in counts.items()))
    failed: dict[tuple[str, str, str], int] = {}
    for call in ex.calls:
        if call.failed:
            key = (call.name, call.request, call.result)
            failed[key] = failed.get(key, 0) + 1
    for (name, request, result), count in failed.items():
        suffix = f" same command ×{count}" if count > 1 else ""
        line = call_line(ToolCall(name, request, result, True), limit=400 - len(suffix))
        lines.append(line + suffix)
    if ex.compaction:
        lines.append(COMPACTION_MARKER)
    return "\n".join(lines)


def _omitted(count: int) -> str:
    return f"[… {count} exchanges omitted …]"


def _join(front: list[str], back: list[str], omitted: int) -> str:
    middle = [_omitted(omitted)] if omitted else []
    return "\n\n".join(front + middle + list(reversed(back)))


def render_digest(exchanges: list[Exchange], *, cap_tokens: int = 50_000) -> str:
    # Lengths are tracked arithmetically; joining a long history after every step is
    # quadratic and too slow for a hook that runs before each prompt.
    sep = len("\n\n")
    entries = [digest_entry(ex) for ex in exchanges]
    total = sum(len(entry) for entry in entries) + sep * max(len(entries) - 1, 0)
    if _fits(total, cap_tokens):
        return "\n\n".join(entries)
    # Stage 2: shorten the oldest first; exchange 1's typed prompt stays whole.
    for i, ex in enumerate(exchanges):
        shorter = digest_entry(
            ex,
            prompt_chars=PROMPT_CHARS if i == 0 else SHORT_PROMPT_CHARS,
            final_chars=SHORT_FINAL_CHARS,
        )
        total += len(shorter) - len(entries[i])
        entries[i] = shorter
        if _fits(total, cap_tokens):
            return "\n\n".join(entries)
    # Stage 3: cut the middle, keeping entries alternately from both ends.
    n = len(entries)
    if n < 3:
        return _guard("\n\n".join(entries), cap_tokens)
    front, back = [entries[0]], [entries[-1]]
    kept = len(entries[0]) + len(entries[-1])
    lo, hi = 1, n - 2
    take_front = True
    stuck = 0
    while lo <= hi and stuck < 2:
        entry = entries[lo] if take_front else entries[hi]
        omitted = hi - lo
        count = len(front) + len(back) + 1 + (1 if omitted else 0)
        note = len(_omitted(omitted)) if omitted else 0
        if _fits(kept + len(entry) + note + sep * (count - 1), cap_tokens):
            kept += len(entry)
            if take_front:
                front.append(entry)
                lo += 1
            else:
                back.append(entry)
                hi -= 1
            stuck = 0
        else:
            stuck += 1
        take_front = not take_front
    return _guard(_join(front, back, max(hi - lo + 1, 0)), cap_tokens)


def _fits(chars: int, cap_tokens: int) -> bool:
    return math.ceil(chars / CHARS_PER_TOKEN) <= cap_tokens


def _guard(text: str, cap_tokens: int) -> str:
    """Last resort so the result never exceeds the cap."""
    limit = int(cap_tokens * 3.5)
    while tokens(text) > cap_tokens:
        text = text[:limit]
        limit -= 10
    return text
