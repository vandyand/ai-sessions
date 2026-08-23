"""Typed harness capability contract, independent of registry and application code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from typing import Any, Iterable, Mapping, Protocol

from .model import BudgetPolicy, NativeSession, SourceKind, Transcript, Turn


class ReadHook(Protocol):
    def __call__(self, path: Path, *, latest_window: bool = True) -> Transcript: ...


class WriteHook(Protocol):
    def __call__(
        self, *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
    ) -> tuple[str, Path]: ...


class LocateHook(Protocol):
    def __call__(self, session_id: str) -> bool: ...


class ChangeStatusHook(Protocol):
    def __call__(self, path: Path, offset: int) -> str: ...


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
    def __call__(self, context: Any, sessions: Iterable[Any]) -> Mapping[str, int]: ...


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
    locate: LocateHook | Unsupported
    change_status: ChangeStatusHook | Unsupported
    budget: BudgetPolicy
    discover: DiscoverHook | Unsupported = Unsupported("discovery is not installed")
    resume_args: ResumeArgsHook | Unsupported = Unsupported("resume is not installed")
    publish_name: PublishNameHook | Unsupported = Unsupported("rename is not supported")
    inspect_liveness: LivenessHook | Unsupported = Unsupported("liveness is not installed")

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
        if not self.id_patterns or not all(
            isinstance(pattern.pattern, bytes) for pattern in self.id_patterns
        ):
            raise ValueError("harness id_patterns must contain compiled byte patterns")
        if not isinstance(self.budget, BudgetPolicy):
            raise ValueError("harness budget must be a BudgetPolicy")
        required = (self.read, self.write, self.locate, self.change_status)
        optional = (self.discover, self.resume_args, self.publish_name, self.inspect_liveness)
        if not all(callable(hook) or isinstance(hook, Unsupported) for hook in required + optional):
            raise ValueError("harness capabilities must be callable or Unsupported")
        for hook in required + optional:
            if isinstance(hook, Unsupported) and not hook.reason.strip():
                raise ValueError("unsupported capability reason must not be empty")
