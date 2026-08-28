"""Neutral data shared by harness adapters and core orchestration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

Checkpoint = int | str


def is_checkpoint(value: object) -> bool:
    """Whether a value is an opaque, JSON-safe native checkpoint."""
    return not isinstance(value, bool) and isinstance(value, (int, str))


LEGACY_DEFAULT_MAX_CHARS = 950_000
DEFAULT_MAX_CHARS = LEGACY_DEFAULT_MAX_CHARS
TRUNCATION_MARKER = "\n\n[... message truncated ...]"
HANDOFF_NOTE_RESERVE_CHARS = 4_096
TARGET_CONTEXT_RESERVE_CHARS = 512
MIN_BRIDGE_CHARS = HANDOFF_NOTE_RESERVE_CHARS + 2 * (len(TRUNCATION_MARKER) + 1) + 2
MIN_BUDGET_TOKENS = 4_096


class SourceKind(StrEnum):
    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non-interactive"
    SUBAGENT = "subagent"
    SDK = "sdk"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NativeRef:
    """One native session in its adapter-owned storage."""

    session_id: str
    storage: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("native session id must not be empty")
        if not self.storage:
            raise ValueError("native session storage must not be empty")


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
    hard_limit: bool = False

    def __post_init__(self) -> None:
        if self.context_tokens <= 0:
            raise ValueError("budget context_tokens must be positive")
        if not 0 < self.usable_fraction <= 1:
            raise ValueError("budget usable_fraction must be in (0, 1]")
        if self.chars_per_token <= 0:
            raise ValueError("budget chars_per_token must be positive")
        if not self.source.strip():
            raise ValueError("budget policy source must not be empty")
        if not isinstance(self.hard_limit, bool):
            raise ValueError("budget policy hard_limit must be a boolean")
        default_tokens = math.floor(self.context_tokens * self.usable_fraction)
        default_chars = math.floor(default_tokens * self.chars_per_token)
        if default_tokens < MIN_BUDGET_TOKENS:
            raise ValueError("budget policy default is below the minimum token budget")
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
    native: NativeRef
    turns: int
    written_turns: int
    calls: int
    dropped: int
    truncated: int
    source_checkpoint: Checkpoint
    target_checkpoint: Checkpoint | None
    budget: Budget
    notices: tuple[str, ...] = ()

    @property
    def session_id(self) -> str:
        return self.native.session_id

    @property
    def storage(self) -> str:
        return self.native.storage


@dataclass(frozen=True, slots=True)
class Transcript:
    turns: list[Turn]
    opaque_compactions: int = 0
    carried_windows: int = 0
    sealed_summary: bool = False
    resumes_at_last_summary: bool = False


@dataclass(frozen=True, slots=True)
class ReadSnapshot:
    transcript: Transcript
    checkpoint: Checkpoint


@dataclass(frozen=True, slots=True)
class PreparedTarget:
    command: tuple[str, ...]
    budget_policy: BudgetPolicy
    writer_options: tuple[tuple[str, str], ...] = ()
    notices: tuple[str, ...] = ()
    handoff_context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.command or not all(isinstance(item, str) and item for item in self.command):
            raise ValueError("prepared target command must contain non-empty strings")
        if not isinstance(self.handoff_context, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.handoff_context
        ):
            raise ValueError("prepared target handoff context must contain non-empty strings")
        rendered_context = sum(
            len("Target preparation: ") + min(len(item), 300) + 1
            for item in self.handoff_context[:4]
        )
        if rendered_context > TARGET_CONTEXT_RESERVE_CHARS:
            raise ValueError("prepared target handoff context exceeds its reserved budget")

    def option(self, name: str, default: str = "") -> str:
        return next((value for key, value in self.writer_options if key == name), default)


@dataclass(frozen=True, slots=True)
class NativeWrite:
    native: NativeRef
    checkpoint: Checkpoint | None
    notices: tuple[str, ...] = ()


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
    turn_count: int = 0
    compaction_count: int = 0
    prompt_count: int = 0

    def __post_init__(self) -> None:
        self.source = SourceKind(self.source)
        # ``message_count`` predates the activity triple and remains the
        # serialized/UI compatibility value.  Old callers therefore provide
        # just it; new callers can provide prompt_count instead.
        if self.prompt_count <= 0 and self.message_count > 0:
            self.prompt_count = self.message_count
        elif self.message_count <= 0 and self.prompt_count > 0:
            self.message_count = self.prompt_count
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

    @property
    def activity(self) -> str:
        return f"{self.turn_count}t {self.compaction_count}c {self.prompt_count}p"


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
    turn_count: int = 0
    compaction_count: int = 0
    prompt_count: int = 0

    def __post_init__(self) -> None:
        if not self.tool or not self.session_id:
            raise ValueError("native session tool and session_id must not be empty")
        if not isinstance(self.source, SourceKind):
            raise ValueError("native session source must be a SourceKind")
        if self.prompt_count <= 0 and self.message_count > 0:
            object.__setattr__(self, "prompt_count", self.message_count)
        elif self.message_count <= 0 and self.prompt_count > 0:
            object.__setattr__(self, "message_count", self.prompt_count)

    @property
    def activity(self) -> str:
        return f"{self.turn_count}t {self.compaction_count}c {self.prompt_count}p"


@dataclass(frozen=True, slots=True)
class LivenessSession:
    """Immutable adapter view of one utility row during liveness inspection."""

    session_id: str
    source: SourceKind
    storage: str
