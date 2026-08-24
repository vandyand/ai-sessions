"""Typed harness capability contract, independent of registry and application code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from typing import Any, Iterable, Mapping, Protocol

from .liveness import LivenessContext
from .model import (
    Availability,
    BudgetPolicy,
    Checkpoint,
    LivenessSession,
    NativeRef,
    NativeSession,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    SourceKind,
    Turn,
)


class ReadHook(Protocol):
    def __call__(self, ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot: ...


class WriteHook(Protocol):
    def __call__(
        self,
        *,
        cwd: str,
        turns: list[Turn],
        prepared: PreparedTarget,
        title: str = "",
        created: float | None = None,
    ) -> NativeWrite: ...


class ResolveHook(Protocol):
    def __call__(self, session_id: str) -> NativeRef | None: ...


class AvailabilityHook(Protocol):
    def __call__(self, ref: NativeRef) -> Availability: ...


class CheckpointHook(Protocol):
    def __call__(self, ref: NativeRef) -> Checkpoint: ...


class ChangeStatusHook(Protocol):
    def __call__(self, ref: NativeRef, checkpoint: Checkpoint) -> str: ...


class PrepareTargetHook(Protocol):
    def __call__(
        self, command: tuple[str, ...], cwd: str, options: Mapping[str, Any]
    ) -> PreparedTarget: ...


class DiscoverHook(Protocol):
    def __call__(self, context: Any, *, use_cache: bool = True) -> Iterable[NativeSession]: ...


class ResumeArgsHook(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        source: SourceKind,
        resume_id: str,
        parent_id: str,
        native: bool,
    ) -> list[str]: ...


class PublishNameHook(Protocol):
    def __call__(self, session: Any, name: str) -> str: ...


class LivenessHook(Protocol):
    def __call__(
        self, context: LivenessContext, home: Path, sessions: Iterable[LivenessSession]
    ) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class Unsupported:
    reason: str


@dataclass(frozen=True, slots=True)
class HarnessAdapter:
    name: str
    label: str
    short_label: str
    order: int
    home: Path
    default_command: tuple[str, ...]
    dangerous_args: tuple[str, ...]
    source_kinds: frozenset[SourceKind]
    id_patterns: tuple[Pattern[bytes], ...]
    read: ReadHook | Unsupported
    write: WriteHook | Unsupported
    resolve: ResolveHook | Unsupported
    availability: AvailabilityHook | Unsupported
    checkpoint: CheckpointHook | Unsupported
    change_status: ChangeStatusHook | Unsupported
    budget: BudgetPolicy
    liveness_source_kinds: frozenset[SourceKind] = frozenset((SourceKind.INTERACTIVE,))
    scratch_patterns: tuple[Pattern[str], ...] = ()
    discover: DiscoverHook | Unsupported = Unsupported("discovery is not installed")
    resume_args: ResumeArgsHook | Unsupported = Unsupported("resume is not installed")
    publish_name: PublishNameHook | Unsupported = Unsupported("rename is not supported")
    inspect_liveness: LivenessHook | Unsupported = Unsupported("liveness is not installed")
    prepare_target: PrepareTargetHook | Unsupported = Unsupported(
        "dynamic target preparation is not installed"
    )

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.short_label.strip():
            raise ValueError("harness labels must not be empty")
        if len(self.short_label) > 16:
            raise ValueError("harness short_label must be at most 16 characters")
        if not isinstance(self.order, int) or isinstance(self.order, bool):
            raise ValueError("harness order must be an integer")
        if not isinstance(self.home, Path):
            raise ValueError("harness home must be a Path")
        if not self.default_command or not all(
            isinstance(value, str) and value for value in self.default_command
        ):
            raise ValueError("harness default_command must contain non-empty strings")
        if not all(isinstance(value, str) and value for value in self.dangerous_args):
            raise ValueError("harness dangerous_args must contain non-empty strings")
        if not self.source_kinds or not all(
            isinstance(value, SourceKind) for value in self.source_kinds
        ):
            raise ValueError("harness source_kinds must contain SourceKind values")
        if (
            not self.id_patterns
            or not isinstance(self.id_patterns, tuple)
            or not all(
                isinstance(pattern, Pattern) and isinstance(pattern.pattern, bytes)
                for pattern in self.id_patterns
            )
        ):
            raise ValueError("harness id_patterns must contain compiled byte patterns")
        if not isinstance(self.scratch_patterns, tuple) or not all(
            isinstance(pattern, Pattern) and isinstance(pattern.pattern, str)
            for pattern in self.scratch_patterns
        ):
            raise ValueError("harness scratch_patterns must contain compiled text patterns")
        if not isinstance(self.budget, BudgetPolicy):
            raise ValueError("harness budget must be a BudgetPolicy")
        if not isinstance(self.liveness_source_kinds, frozenset) or not all(
            isinstance(value, SourceKind) for value in self.liveness_source_kinds
        ):
            raise ValueError("harness liveness source kinds must contain SourceKind values")
        if not isinstance(self.inspect_liveness, Unsupported) and not (
            self.liveness_source_kinds.issubset(self.source_kinds)
        ):
            raise ValueError("harness liveness source kinds must be declared source kinds")
        required = (
            self.read,
            self.write,
            self.resolve,
            self.availability,
            self.checkpoint,
            self.change_status,
        )
        optional = (
            self.discover,
            self.resume_args,
            self.publish_name,
            self.inspect_liveness,
            self.prepare_target,
        )
        if not all(callable(hook) or isinstance(hook, Unsupported) for hook in required + optional):
            raise ValueError("harness capabilities must be callable or Unsupported")
        for hook in required + optional:
            if isinstance(hook, Unsupported) and not hook.reason.strip():
                raise ValueError("unsupported capability reason must not be empty")
