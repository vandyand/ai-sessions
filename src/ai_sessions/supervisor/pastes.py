"""Detect pasted content in user prompts and replace it with a compact marker."""

from __future__ import annotations

import re
from collections.abc import Iterable

FENCE = "```"
HEAD_CHARS = 300
TAIL_CHARS = 500
WHOLE_LIMIT = 800
_LANG_FENCE = re.compile(r"[A-Za-z0-9_+-]+\n")


def fenced_spans(text: str) -> list[tuple[int, int]]:
    """Return spans of pasted content, excluding the fence characters.

    A fence followed by a language word and a newline, while a span is open, starts an
    inner block. A bare fence closes one level. End of text closes any span still open.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    depth = 0
    i = 0
    while True:
        i = text.find(FENCE, i)
        if i < 0:
            break
        after = i + len(FENCE)
        if start is None:
            start = after
            depth = 0
        elif _LANG_FENCE.match(text, after):
            depth += 1
        elif depth > 0:
            depth -= 1
        else:
            spans.append((start, i))
            start = None
        i = after
    if start is not None:
        spans.append((start, len(text)))
    return spans


def content_spans(text: str, pastes: Iterable[str]) -> list[tuple[int, int]]:
    """Return exact-substring spans for each known paste content found in the text."""
    spans: list[tuple[int, int]] = []
    for paste in pastes:
        if not paste:
            continue
        pos = text.find(paste)
        while pos >= 0:
            spans.append((pos, pos + len(paste)))
            pos = text.find(paste, pos + len(paste))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] < merged[-1][1]:
            continue
        merged.append(span)
    return merged


def _marker(content: str) -> str:
    count = f"{len(content):,}"
    if len(content) <= WHOLE_LIMIT:
        return f"[pasted {count} chars: {content}]"
    return f"[pasted {count} chars: {content[:HEAD_CHARS]} … {content[-TAIL_CHARS:]}]"


def strip_pastes(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace each span with a paste marker and drop the fences around it."""
    out: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        before = start - len(FENCE) if text.startswith(FENCE, start - len(FENCE)) else start
        before = max(before, cursor)
        after = end + len(FENCE) if text.startswith(FENCE, end) else end
        out.append(text[cursor:before])
        out.append(_marker(text[start:end]))
        cursor = after
    out.append(text[cursor:])
    return "".join(out)


def typed_text(text: str, *, pastes: list[str]) -> str:
    """Return the prompt with pastes replaced by markers.

    Paste contents the harness recorded decide when any match; otherwise the fence rule
    decides. A harness that records no pastes passes an empty list.
    """
    spans = content_spans(text, pastes)
    if not spans:
        spans = fenced_spans(text)
    return strip_pastes(text, spans)
