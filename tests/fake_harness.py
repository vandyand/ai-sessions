"""Independent third-format harness used only by adapter contract tests."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Iterable

from ai_sessions.capabilities import HarnessAdapter
from ai_sessions.conversion import complete_jsonl_cursor
from ai_sessions.discovery import HarnessContext
from ai_sessions.liveness import LivenessContext
from ai_sessions.model import (
    Availability,
    BudgetPolicy,
    LivenessSession,
    NativeRef,
    NativeSession,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    SourceKind,
    Transcript,
    Turn,
)
from ai_sessions.registry import REGISTRY


def _encode(record: dict[str, object]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _records(path: Path) -> Iterable[tuple[bytes, dict[str, object]]]:
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                continue
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                yield raw, value


def make_adapter(home: Path) -> HarnessAdapter:
    def adapter_home() -> Path:
        return REGISTRY.get("fixture").home

    def path_for(session_id: str) -> Path:
        return adapter_home() / "threads" / f"{session_id}.thread"

    def read_path(path: Path, *, latest_window: bool = True, end: int | None = None) -> Transcript:
        turns: list[Turn] = []
        consumed = 0
        for raw, record in _records(path):
            consumed += len(raw)
            if end is not None and consumed > end:
                break
            if record.get("kind") != "utterance":
                continue
            role = {"person": "user", "machine": "assistant"}.get(record.get("actor"))
            payload = record.get("payload")
            text = payload.get("text") if isinstance(payload, dict) else None
            if role and isinstance(text, str) and text:
                turn = Turn(role, text, compaction=bool(record.get("summary")))
                if turn.compaction and latest_window:
                    turns = [turn]
                else:
                    turns.append(turn)
        return Transcript(turns)

    def checkpoint(ref: NativeRef) -> int:
        return complete_jsonl_cursor(Path(ref.storage))

    def read(ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
        captured = checkpoint(ref)
        return ReadSnapshot(
            read_path(Path(ref.storage), latest_window=latest_window, end=captured),
            captured,
        )

    def write(
        *,
        cwd: str,
        turns: list[Turn],
        prepared: PreparedTarget,
        title: str = "",
        created: float | None = None,
    ) -> NativeWrite:
        del prepared
        session_id = str(uuid.uuid4())
        path = path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = created if created is not None else time.time()
        records: list[dict[str, object]] = [
            {
                "kind": "thread",
                "thread": session_id,
                "directory": cwd,
                "created_us": int(timestamp * 1_000_000),
                "label": title,
            }
        ]
        records.extend(
            {
                "kind": "utterance",
                "actor": "person" if turn.role == "user" else "machine",
                "payload": {"text": turn.text},
                "summary": turn.compaction,
                "at_us": int((timestamp + index / 1_000_000) * 1_000_000),
            }
            for index, turn in enumerate(turns, 1)
        )
        with path.open("xb") as handle:
            for record in records:
                handle.write(_encode(record))
        ref = NativeRef(session_id, str(path))
        return NativeWrite(ref, checkpoint(ref))

    def resolve(session_id: str) -> NativeRef | None:
        path = path_for(session_id)
        return NativeRef(session_id, str(path)) if path.is_file() else None

    def availability(ref: NativeRef) -> Availability:
        try:
            return (
                Availability.AVAILABLE if Path(ref.storage).is_file() else Availability.UNAVAILABLE
            )
        except OSError:
            return Availability.UNKNOWN

    def change_status(ref: NativeRef, offset: int | str) -> str:
        if not isinstance(offset, int) or isinstance(offset, bool):
            return "unstable"
        path = Path(ref.storage)
        try:
            size = path.stat().st_size
            if offset < 0 or size < offset:
                return "unstable"
            with path.open("rb") as handle:
                handle.seek(offset)
                tail = handle.read()
        except OSError:
            return "unstable"
        if not tail:
            return "unchanged"
        if not tail.endswith(b"\n"):
            return "unstable"
        for raw in tail.split(b"\n"):
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return "unstable"
            if isinstance(record, dict) and record.get("kind") == "utterance":
                payload = record.get("payload")
                if (
                    record.get("actor") in ("person", "machine")
                    and isinstance(payload, dict)
                    and isinstance(payload.get("text"), str)
                    and payload["text"]
                ):
                    return "changed"
        return "unchanged"

    def discover(context: HarnessContext, *, use_cache: bool = True) -> list[NativeSession]:
        del use_cache
        result: list[NativeSession] = []
        threads = adapter_home() / "threads"
        try:
            paths = sorted(threads.glob("*.thread"))
        except OSError:
            paths = []
        for path in paths:
            evidence = context.accumulator()
            session_id = path.stem
            cwd = ""
            title = ""
            created = 0.0
            updated = 0.0
            messages: list[str] = []
            try:
                records = list(_records(path))
                stat = path.stat()
            except OSError:
                continue
            saw_thread = False
            for raw, record in records:
                evidence.scan(raw)
                kind = record.get("kind")
                if kind == "thread":
                    saw_thread = True
                    session_id = str(record.get("thread") or session_id)
                    cwd = str(record.get("directory") or "")
                    title = str(record.get("label") or "")
                    created = float(record.get("created_us") or 0) / 1_000_000
                elif kind == "label":
                    title = str(record.get("value") or title)
                elif kind == "utterance":
                    payload = record.get("payload")
                    text = payload.get("text") if isinstance(payload, dict) else None
                    if (
                        record.get("actor") in ("person", "machine")
                        and isinstance(text, str)
                        and text
                    ):
                        messages.append(text)
                    updated = max(updated, float(record.get("at_us") or 0) / 1_000_000)
            if not saw_thread:
                continue
            context.publish("fixture", session_id, evidence)
            result.append(
                NativeSession(
                    "fixture",
                    session_id,
                    title or (messages[0] if messages else "untitled fixture"),
                    cwd,
                    updated or stat.st_mtime,
                    created or stat.st_ctime,
                    messages[-1] if messages else "",
                    bool(title),
                    str(path),
                    message_count=len(messages),
                )
            )
        return result

    def resume_args(
        *,
        session_id: str,
        source: SourceKind,
        resume_id: str,
        parent_id: str,
        native: bool,
    ) -> list[str]:
        del source, resume_id, parent_id, native
        return ["thread", "--id", session_id]

    def publish_name(session: object, name: str) -> str:
        path = Path(str(getattr(session, "storage")))
        if not path.is_file() or path.parent != adapter_home() / "threads":
            return "fixture transcript is missing; name kept local only"
        with path.open("ab") as handle:
            handle.write(_encode({"kind": "label", "value": name}))
        return ""

    def inspect_liveness(
        context: LivenessContext,
        adapter_home: Path,
        sessions: Iterable[LivenessSession],
    ) -> dict[str, int]:
        live_pids = {process.pid for process in context.process_snapshot}
        result: dict[str, int] = {}
        for session in sessions:
            marker = adapter_home / "live" / f"{session.session_id}.pid"
            try:
                pid = int(marker.read_text(encoding="ascii"))
            except (OSError, ValueError):
                continue
            if pid in live_pids:
                result[session.session_id] = pid
        return result

    return HarnessAdapter(
        name="fixture",
        label="Fixture Harness",
        short_label="FixtureHarness",
        order=30,
        home=home,
        default_command=("fixture-cli",),
        dangerous_args=("--reckless",),
        source_kinds=frozenset((SourceKind.INTERACTIVE,)),
        id_patterns=(
            re.compile(
                rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                re.I,
            ),
        ),
        read=read,
        write=write,
        resolve=resolve,
        availability=availability,
        checkpoint=checkpoint,
        change_status=change_status,
        budget=BudgetPolicy(50_000, 0.5, 3.0, "test-only fixture policy"),
        discover=discover,
        resume_args=resume_args,
        publish_name=publish_name,
        inspect_liveness=inspect_liveness,
    )
