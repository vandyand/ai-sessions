"""Format-neutral discovery context, evidence collection, and text shaping."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .registry import REGISTRY

MAX_EVIDENCE_IDS = 4_096


def normalize_space(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def clean_prompt(value: Any) -> str:
    """Turn a first prompt or generated title into a useful one-line label."""
    text = normalize_space(value)
    if not text:
        return ""
    text = re.sub(r"<environment_context>.*?</environment_context>", " ", text, flags=re.I | re.S)
    text = re.sub(
        r"<permissions instructions>.*?</permissions instructions>", " ", text, flags=re.I | re.S
    )
    text = normalize_space(text)
    for prefix in ("<user>", "Human:", "User:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
    return text


def prompt_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                value = block.get("text", "")
                if isinstance(value, str):
                    parts.append(value)
        return " ".join(parts)
    return ""


def timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        result = float(value)
        return result / 1000 if result > 100_000_000_000 else result
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


@dataclass(slots=True)
class EvidenceAccumulator:
    patterns: tuple[re.Pattern[bytes], ...]
    tokens: list[str] = field(default_factory=list)
    truncated: bool = False
    _seen: set[str] = field(default_factory=set)

    @classmethod
    def restored(
        cls,
        patterns: tuple[re.Pattern[bytes], ...],
        tokens: list[str],
        truncated: bool,
    ) -> "EvidenceAccumulator":
        valid = [value for value in tokens if isinstance(value, str) and value]
        return cls(patterns, valid[:MAX_EVIDENCE_IDS], truncated, set(valid[:MAX_EVIDENCE_IDS]))

    def scan(self, line: bytes) -> None:
        matches: list[tuple[int, int, bytes]] = []
        for pattern_index, pattern in enumerate(self.patterns):
            matches.extend(
                (match.start(), pattern_index, match.group(0)) for match in pattern.finditer(line)
            )
        for _, _, raw in sorted(matches, key=lambda item: (item[0], item[1])):
            try:
                token = raw.decode("ascii")
            except UnicodeDecodeError:
                continue
            if token in self._seen:
                continue
            if len(self.tokens) >= MAX_EVIDENCE_IDS:
                self.truncated = True
                continue
            self._seen.add(token)
            self.tokens.append(token)


@dataclass(slots=True)
class HarnessContext:
    use_cache: bool
    patterns: tuple[re.Pattern[bytes], ...]
    pattern_signature: str
    evidence: dict[tuple[str, str], tuple[list[str], bool]] = field(default_factory=dict)
    origin_hints: dict[tuple[str, str], str] = field(default_factory=dict)
    process_snapshot: tuple[Any, ...] = ()
    lock_map: dict[Any, int] = field(default_factory=dict)

    @classmethod
    def create(cls, *, use_cache: bool = True) -> "HarnessContext":
        patterns: list[re.Pattern[bytes]] = []
        digest = hashlib.sha256()
        for adapter in REGISTRY.adapters():
            for pattern in adapter.id_patterns:
                patterns.append(pattern)
                digest.update(adapter.name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(pattern.pattern)
                digest.update(str(pattern.flags).encode("ascii"))
                digest.update(b"\0")
        return cls(use_cache, tuple(patterns), digest.hexdigest())

    def accumulator(
        self, *, tokens: list[str] | None = None, truncated: bool = False
    ) -> EvidenceAccumulator:
        return EvidenceAccumulator.restored(self.patterns, tokens or [], truncated)

    def publish(self, tool: str, session_id: str, evidence: EvidenceAccumulator) -> None:
        self.evidence[(tool, session_id)] = (list(evidence.tokens), evidence.truncated)

    def mark_cross_origin(self, tool: str, session_id: str, source_tool: str) -> None:
        self.origin_hints[(tool, session_id)] = source_tool
