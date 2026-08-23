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
