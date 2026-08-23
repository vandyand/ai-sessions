"""Neutral data shared by harness adapters and core orchestration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

LEGACY_DEFAULT_MAX_CHARS = 950_000
DEFAULT_MAX_CHARS = LEGACY_DEFAULT_MAX_CHARS
TRUNCATION_MARKER = "\n\n[... message truncated ...]"
HANDOFF_NOTE_RESERVE_CHARS = 4_096
MIN_BRIDGE_CHARS = HANDOFF_NOTE_RESERVE_CHARS + 2 * (len(TRUNCATION_MARKER) + 1) + 2
MIN_BUDGET_TOKENS = 4_096


class SourceKind(StrEnum):
    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non-interactive"
    SUBAGENT = "subagent"
    SDK = "sdk"


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
    compaction: bool = False


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    context_tokens: int
    usable_fraction: float
    chars_per_token: float
    source: str

    def __post_init__(self) -> None:
        if self.context_tokens <= 0:
            raise ValueError("budget context_tokens must be positive")
        if not 0 < self.usable_fraction <= 1:
            raise ValueError("budget usable_fraction must be in (0, 1]")
        if self.chars_per_token <= 0:
            raise ValueError("budget chars_per_token must be positive")
        if not self.source.strip():
            raise ValueError("budget policy source must not be empty")
        default_tokens = math.floor(self.context_tokens * self.usable_fraction)
        default_chars = math.floor(default_tokens * self.chars_per_token)
        if default_chars < MIN_BRIDGE_CHARS:
            raise ValueError("budget policy default is too small for bridge metadata")


@dataclass(frozen=True, slots=True)
class Budget:
    target: str
    tokens: int
    chars: int
    origin: str
    clamped: bool = False
    over_policy: bool = False

    def __post_init__(self) -> None:
        if not self.target or not self.origin:
            raise ValueError("budget target and origin must not be empty")
        if self.tokens <= 0 or self.chars < MIN_BRIDGE_CHARS:
            raise ValueError("resolved budget is below the bridge minimum")


@dataclass(frozen=True, slots=True)
class SelectionResult:
    turns: list[Turn]
    dropped: int
    truncated: int


@dataclass(frozen=True)
class SelectionMetric:
    item_cost: Callable[[Turn], int]
    join_cost: Callable[[Turn, Turn], int]
    truncate: Callable[[Turn, int], Turn]


@dataclass(frozen=True, slots=True)
class BridgeResult:
    tool: str
    session_id: str
    path: Path
    turns: int
    written_turns: int
    calls: int
    dropped: int
    truncated: int
    source_cursor: int
    budget: Budget


@dataclass(frozen=True, slots=True)
class Transcript:
    turns: list[Turn]
    opaque_compactions: int = 0
    carried_windows: int = 0
    sealed_summary: bool = False
    resumes_at_last_summary: bool = False


@dataclass(slots=True)
class Session:
    """Utility/UI state layered over a native session without registry dependencies."""

    tool: str
    session_id: str
    title: str
    cwd: str
    updated: float
    created: float
    preview: str
    named: bool
    storage: str
    source: SourceKind = SourceKind.INTERACTIVE
    auxiliary: bool = False
    origin: str = "human"
    resume_id: str = ""
    parent_id: str = ""
    original_title: str = ""
    original_named: bool = False
    renamed: bool = False
    hidden: bool = False
    message_count: int = 0
    is_open: bool = False
    open_pid: int = 0
    agent_nickname: str = ""
    launch_targets: dict[str, str] = field(default_factory=dict)
    launch_tool: str = ""
    conversation_id: str = ""
    superseded: bool = False
    diverged: bool = False

    def __post_init__(self) -> None:
        self.source = SourceKind(self.source)
        if not self.launch_targets:
            self.launch_targets = {self.tool: self.session_id}
        elif self.tool not in self.launch_targets:
            self.launch_targets = {self.tool: self.session_id, **self.launch_targets}

    @property
    def key(self) -> str:
        return f"{self.tool}:{self.session_id}"

    def launch_target(self, tool: str) -> str:
        return self.launch_targets.get(tool, "")

    @property
    def resume_target(self) -> str:
        return self.resume_id or self.session_id


@dataclass(frozen=True, slots=True)
class NativeSession:
    """Provider discovery output before utility-owned state is applied."""

    tool: str
    session_id: str
    title: str
    cwd: str
    updated: float
    created: float
    preview: str
    named: bool
    storage: str
    source: SourceKind = SourceKind.INTERACTIVE
    archived: bool = False
    resume_id: str = ""
    parent_id: str = ""
    message_count: int = 0
    agent_nickname: str = ""

    def __post_init__(self) -> None:
        if not self.tool or not self.session_id:
            raise ValueError("native session tool and session_id must not be empty")
        if not isinstance(self.source, SourceKind):
            raise ValueError("native session source must be a SourceKind")


@dataclass(frozen=True, slots=True)
class LivenessSession:
    """Immutable adapter view of one utility row during liveness inspection."""

    session_id: str
    source: SourceKind
    storage: str
