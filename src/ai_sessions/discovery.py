"""Format-neutral discovery context, evidence collection, and text shaping."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping

from .liveness import ProcessInfo
from .registry import REGISTRY

MAX_EVIDENCE_IDS = 4_096
MAX_EVIDENCE_OVERLAP = 255
MAX_EXISTENCE_PROBES_PER_PASS = 64


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
    bytes_scanned: int = 0
    max_bytes: int | None = None
    _contiguous_tail: bytes = b""

    def remaining_bytes(self) -> int | None:
        if self.max_bytes is None:
            return None
        return max(0, self.max_bytes - self.bytes_scanned)

    @classmethod
    def restored(
        cls,
        patterns: tuple[re.Pattern[bytes], ...],
        tokens: list[str],
        truncated: bool,
    ) -> "EvidenceAccumulator":
        valid = [value for value in tokens if isinstance(value, str) and value]
        retained = valid[:MAX_EVIDENCE_IDS]
        return cls(
            patterns=patterns,
            tokens=retained,
            truncated=truncated or len(valid) > len(retained),
            _seen=set(retained),
        )

    def _bounded(self, line: bytes) -> bytes:
        if self.max_bytes is None:
            remaining = len(line)
        else:
            remaining = self.max_bytes - self.bytes_scanned
        if remaining <= 0:
            if line:
                self.truncated = True
            return b""
        if self.max_bytes is not None and len(line) > remaining:
            line = line[:remaining]
            self.truncated = True
        self.bytes_scanned += len(line)
        return line

    def _retain_matches(self, line: bytes, *, end_limit: int | None = None) -> None:
        matches: list[tuple[int, int, bytes]] = []
        for pattern_index, pattern in enumerate(self.patterns):
            matches.extend(
                (match.start(), pattern_index, match.group(0))
                for match in pattern.finditer(line)
                if end_limit is None or match.end() <= end_limit
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

    def scan(self, line: bytes) -> None:
        self._contiguous_tail = b""
        self._retain_matches(self._bounded(line))

    def scan_contiguous(
        self,
        chunk: bytes,
        *,
        reset: bool = False,
        final: bool = False,
    ) -> None:
        """Scan a chunk while retaining enough context to decide boundary matches.

        A non-final chunk deliberately withholds its trailing overlap.  Otherwise
        an end-anchored negative lookahead can accept an identifier merely because
        the following alphanumeric bytes have not been read yet.  ``reset`` marks
        a new independent value and prevents matches spanning adjacent rows.
        """
        if reset:
            self._contiguous_tail = b""
        bounded = self._bounded(chunk)
        combined = self._contiguous_tail + bounded
        if final:
            self._retain_matches(combined)
        else:
            safe_end = max(0, len(combined) - MAX_EVIDENCE_OVERLAP)
            self._retain_matches(combined, end_limit=safe_end)
        self._contiguous_tail = combined[-MAX_EVIDENCE_OVERLAP:]


@dataclass(slots=True)
class HarnessContext:
    """Mutable orchestration state scoped to exactly one discovery/refresh pass."""

    use_cache: bool
    patterns: tuple[re.Pattern[bytes], ...]
    pattern_signature: str
    evidence: dict[tuple[str, str], tuple[list[str], bool]] = field(default_factory=dict)
    origin_hints: dict[tuple[str, str], str] = field(default_factory=dict)
    platform: str = sys.platform
    process_snapshot: tuple[ProcessInfo, ...] = ()
    lock_map: dict[tuple[int, int, int], int] = field(default_factory=dict)
    liveness_ready: bool = False
    provider_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    adapter_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        use_cache: bool = True,
        provider_commands: Mapping[str, tuple[str, ...] | list[str]] | None = None,
    ) -> "HarnessContext":
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
        commands: dict[str, tuple[str, ...]] = {}
        for name, command in (provider_commands or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError("provider command names must be non-empty strings")
            if (
                isinstance(command, (str, bytes))
                or not isinstance(command, (tuple, list))
                or not command
                or not all(isinstance(value, str) and value for value in command)
            ):
                raise ValueError(f"provider command for {name} must be a non-empty string sequence")
            commands[name] = tuple(command)
        return cls(
            use_cache=use_cache,
            patterns=tuple(patterns),
            pattern_signature=digest.hexdigest(),
            provider_commands=commands,
        )

    def provider_command(self, name: str) -> tuple[str, ...]:
        command = self.provider_commands.get(name)
        if command is not None:
            return command
        return REGISTRY.get(name).default_command

    def accumulator(
        self,
        *,
        tokens: list[str] | None = None,
        truncated: bool = False,
        max_bytes: int | None = None,
    ) -> EvidenceAccumulator:
        result = EvidenceAccumulator.restored(self.patterns, tokens or [], truncated)
        result.max_bytes = max_bytes
        return result

    def publish(self, tool: str, session_id: str, evidence: EvidenceAccumulator) -> None:
        self.evidence[(tool, session_id)] = (list(evidence.tokens), evidence.truncated)

    def mark_cross_origin(self, tool: str, session_id: str, source_tool: str) -> None:
        self.origin_hints[(tool, session_id)] = source_tool
