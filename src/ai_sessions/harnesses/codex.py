"""Codex CLI harness adapter."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from ..capabilities import HarnessAdapter
from ..conversion import (
    CODEX_BUDGET_CONTEXT_TOKENS,
    CODEX_CONTEXT_WINDOW,
    DEFAULT_CHARS_PER_TOKEN,
    ONE_M_CONTEXT_USABLE_FRACTION,
    BridgeError,
    _block_text,
    _Conversation,
    _digest_arguments,
    _digest_codex_input,
    _iso,
    _jsonl_change_status,
    _records,
    _records_after,
    append_jsonl,
    complete_jsonl_cursor,
    scrub,
)
from ..diagnostics import record_warning
from ..discovery import HarnessContext, clean_prompt, normalize_space, timestamp
from ..liveness import LivenessContext
from ..model import (
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
from ..paths import APP_CACHE_DIR, HOME, env_path
from ..registry import REGISTRY

CODEX_CLI_VERSION = "0.147.0"
CODEX_HOME = env_path("CODEX_HOME", HOME / ".codex")
_RESULT_NOISE = re.compile(r"^Script completed\s*(Wall time[^\n]*)?\s*Output:\s*", re.I)
DISCOVERY_CACHE_VERSION = 7
DISCOVERY_CACHE_FILE = APP_CACHE_DIR / f"codex-discovery-v{DISCOVERY_CACHE_VERSION}.json"
_CACHE_SENTINEL_BYTES = 4096
_CACHE_SENTINEL_COUNT = 8


def _cached_prefix_identity(path: Path, end: int) -> str | None:
    """Digest bounded samples of a cached JSONL prefix before resuming it."""
    if end < 0:
        return None
    starts = {0, max(0, end - _CACHE_SENTINEL_BYTES)}
    if end > _CACHE_SENTINEL_BYTES:
        max_start = end - _CACHE_SENTINEL_BYTES
        for index in range(1, _CACHE_SENTINEL_COUNT - 1):
            starts.add(max_start * index // (_CACHE_SENTINEL_COUNT - 2))
    digest = hashlib.sha256(str(end).encode("ascii"))
    try:
        with path.open("rb") as handle:
            for start in sorted(starts):
                handle.seek(start)
                sample = handle.read(min(_CACHE_SENTINEL_BYTES, end - start))
                digest.update(start.to_bytes(8, "big"))
                digest.update(len(sample).to_bytes(4, "big"))
                digest.update(sample)
    except (OSError, OverflowError):
        return None
    return digest.hexdigest()


def _cached_prefix_matches(path: Path, cached: dict[str, Any]) -> bool:
    identity = cached.get("prefix_identity")
    try:
        end = int(cached.get("offset", -1))
    except (TypeError, ValueError):
        return False
    return isinstance(identity, str) and end >= 0 and identity == _cached_prefix_identity(path, end)


def _codex_activity_text(payload: dict[str, Any]) -> str:
    """Return text from a semantic message, excluding Codex scaffolding."""
    if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
        return ""
    text = scrub(_block_text(payload.get("content")))
    lowered = text.casefold()
    if lowered.startswith("<codex_internal_context") or lowered.startswith(
        "[ai-sessions-provenance v1]"
    ):
        return ""
    return text


def _codex_event_text(payload: dict[str, Any]) -> str:
    if payload.get("type") != "user_message":
        return ""
    value = payload.get("message")
    if not isinstance(value, str):
        return ""
    text = scrub(value)
    lowered = text.casefold()
    if lowered.startswith("<codex_internal_context") or lowered.startswith(
        "[ai-sessions-provenance v1]"
    ):
        return ""
    return text


def uuid7() -> str:
    """Create the time-ordered UUID shape Codex assigns to native sessions."""
    milliseconds = int(time.time() * 1000)
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = milliseconds.to_bytes(6, "big")
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def _codex_window_turns(payload: dict[str, Any]) -> tuple[list[Turn], bool] | None:
    """Read the plaintext replacement context while disclosing its sealed summary."""
    history = payload.get("replacement_history")
    if not isinstance(history, list) or not history:
        return None
    carried: list[Turn] = []
    sealed = False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("type") not in ("message", "compaction"):
            continue
        if entry.get("type") == "compaction":
            sealed = True
            continue
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue
        text = scrub(_block_text(entry.get("content")))
        if text:
            carried.append(Turn(role, text))
    if not carried:
        return None
    return [replace(carried[0], compaction=True), *carried[1:]], sealed


def read_codex(path: Path, *, latest_window: bool = True, end: int | None = None) -> Transcript:
    """Read Codex response items and honor replacement-history window semantics.

    With ``latest_window`` the newest carried context replaces superseded history;
    without it each window is appended as a marked boundary for whole-log replay.
    """
    conversation = _Conversation()
    for record in _records(path, end=end):
        if record.get("type") == "compacted":
            payload = record.get("payload")
            window = _codex_window_turns(payload) if isinstance(payload, dict) else None
            if window is None:
                conversation.opaque_compaction()
            elif latest_window:
                conversation.carry_window(*window)
            else:
                conversation.append_window(*window)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                conversation.message(role, scrub(_block_text(payload.get("content"))))
        elif kind in ("function_call", "custom_tool_call"):
            request = (
                _digest_arguments(payload.get("arguments"))
                if kind == "function_call"
                else _digest_codex_input(payload.get("input"))
            )
            conversation.call(
                str(payload.get("call_id", "")), str(payload.get("name", "")), request
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            text = output if isinstance(output, str) else _block_text(output)
            conversation.result(
                str(payload.get("call_id", "")), _RESULT_NOISE.sub("", text.strip())
            )
    return Transcript(
        conversation.finish(),
        conversation.opaque_compactions,
        conversation.carried_windows,
        conversation.sealed_summary,
        conversation.resumes_at_last_summary,
    )


def _exchanges(turns: list[Turn]) -> list[list[Turn]]:
    groups: list[list[Turn]] = []
    index = 0
    while index < len(turns):
        group = [turns[index]]
        following = index + 1
        if (
            turns[index].role == "user"
            and following < len(turns)
            and turns[following].role == "assistant"
        ):
            group.append(turns[following])
            index = following
        index += 1
        groups.append(group)
    return groups


def write_codex_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write context, TUI events, and task wrappers required for native resume.

    ``response_item`` alone loads model context but leaves Codex scrollback blank,
    so each exchange also receives user/agent events and a task wrapper.
    """
    session_id = uuid7()
    moment = time.time() if created is None else created
    local = time.localtime(moment)
    home = REGISTRY.get("codex").home
    directory = home / "sessions" / time.strftime("%Y/%m/%d", local)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BridgeError(f"could not create {directory}: {error}") from error
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", local)
    path = directory / f"rollout-{stamp}-{session_id}.jsonl"
    records: list[dict[str, Any]] = [
        {
            "timestamp": _iso(moment),
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": _iso(moment),
                "cwd": cwd,
                "originator": "codex-tui",
                "cli_version": CODEX_CLI_VERSION,
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
            },
        }
    ]

    def add(kind: str, payload: dict[str, Any], at: float) -> None:
        records.append(
            {"timestamp": _iso(at), "ordinal": len(records), "type": kind, "payload": payload}
        )

    for index, group in enumerate(_exchanges(turns)):
        turn_id = uuid7()
        started = moment + index
        add(
            "event_msg",
            {
                "type": "task_started",
                "turn_id": turn_id,
                "started_at": int(started),
                "model_context_window": CODEX_CONTEXT_WINDOW,
                "collaboration_mode_kind": "default",
            },
            started,
        )
        for offset, turn in enumerate(group, start=1):
            user = turn.role == "user"
            stamped = started + offset / 10
            add(
                "response_item",
                {
                    "type": "message",
                    "id": "msg_" + str(uuid.uuid4()),
                    "role": turn.role,
                    "content": [
                        {"type": "input_text" if user else "output_text", "text": turn.text}
                    ],
                },
                stamped,
            )
            event: dict[str, Any]
            if user:
                event = {"type": "user_message", "message": turn.text}
            else:
                event = {
                    "type": "agent_message",
                    "message": turn.text,
                    "phase": "commentary",
                    "memory_citation": None,
                }
            add("event_msg", event, stamped)
        replies = [turn.text for turn in group if turn.role == "assistant"]
        add(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": replies[-1] if replies else "",
            },
            started + 1,
        )
    body = "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records
    )
    try:
        path.write_text(body + "\n", encoding="utf-8")
    except OSError as error:
        raise BridgeError(f"could not write {path}: {error}") from error
    if title:
        append_jsonl(
            home / "session_index.jsonl",
            {"id": session_id, "thread_name": title, "updated_at": _iso(moment)},
        )
    return session_id, path


def _codex_exists(session_id: str) -> bool:
    return any((REGISTRY.get("codex").home / "sessions").rglob(f"rollout-*-{session_id}.jsonl"))


def _codex_resolve(session_id: str) -> NativeRef | None:
    try:
        paths = sorted(
            (REGISTRY.get("codex").home / "sessions").rglob(f"rollout-*-{session_id}.jsonl")
        )
    except OSError:
        return None
    return NativeRef(session_id, str(paths[0])) if paths else None


def _codex_availability(ref: NativeRef) -> Availability:
    path = Path(ref.storage)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return Availability.UNAVAILABLE
    except OSError:
        return Availability.UNKNOWN
    return Availability.AVAILABLE if stat.S_ISREG(metadata.st_mode) else Availability.UNAVAILABLE


def _codex_checkpoint(ref: NativeRef) -> int:
    return complete_jsonl_cursor(Path(ref.storage))


def _read_codex_snapshot(ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
    checkpoint = _codex_checkpoint(ref)
    return ReadSnapshot(
        read_codex(Path(ref.storage), latest_window=latest_window, end=checkpoint), checkpoint
    )


def _write_codex(
    *,
    cwd: str,
    turns: list[Turn],
    prepared: PreparedTarget,
    title: str = "",
    created: float | None = None,
) -> NativeWrite:
    del prepared
    session_id, path = write_codex_session(cwd=cwd, turns=turns, title=title, created=created)
    ref = NativeRef(session_id, str(path))
    return NativeWrite(ref, _codex_checkpoint(ref))


def _classify_codex_tail(ref: NativeRef, checkpoint: int | str, end: int) -> str:
    """Ignore UI metadata while treating messages and tool traffic as work."""
    if not isinstance(checkpoint, int) or isinstance(checkpoint, bool):
        return "unstable"
    changed = False
    for record in _records_after(Path(ref.storage), checkpoint, end=end):
        if record.get("type") in (
            "ai_sessions_replaced",
            "ai_sessions_missing",
            "ai_sessions_incomplete",
        ):
            return "unstable"
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "message" and payload.get("role") in ("user", "assistant"):
            changed = True
        if kind in (
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
        ):
            changed = True
    return "changed" if changed else "unchanged"


def _codex_change_status(ref: NativeRef, checkpoint: int | str) -> str:
    return _jsonl_change_status(ref, checkpoint, _classify_codex_tail, REGISTRY.generation)


def load_history() -> dict[str, dict[str, Any]]:
    """Load user-turn counts and latest prompts from Codex's global history."""
    sessions: dict[str, dict[str, Any]] = {}
    path = REGISTRY.get("codex").home / "history.jsonl"
    if not path.exists():
        return sessions
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = item.get("session_id") or item.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    entry = sessions.setdefault(session_id, {"count": 0, "latest": ""})
                    entry["count"] = int(entry["count"]) + 1
                    latest = clean_prompt(item.get("text"))
                    if latest:
                        entry["latest"] = latest
    except OSError:
        pass
    return sessions


class DiscoveryCache:
    """Incrementally index Codex rollouts and registry-sensitive ID evidence."""

    def __init__(self, context: HarnessContext) -> None:
        self.context = context
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if context.use_cache:
            try:
                payload = json.loads(DISCOVERY_CACHE_FILE.read_text(encoding="utf-8"))
                if payload.get("version") == DISCOVERY_CACHE_VERSION and isinstance(
                    payload.get("entries"), dict
                ):
                    self.entries = payload["entries"]
            except (OSError, json.JSONDecodeError, AttributeError):
                pass

    def scan(self, path: Path, source: SourceKind) -> tuple[int, int, int, str, Any]:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return 0, 0, 0, "", self.context.accumulator()
        cached = self.entries.get(key)
        signature_matches = bool(
            cached and cached.get("pattern_signature") == self.context.pattern_signature
        )
        prefix_matches = bool(cached and signature_matches and _cached_prefix_matches(path, cached))
        exact = bool(
            cached
            and signature_matches
            and cached.get("mode") == source.value
            and cached.get("inode") == stat.st_ino
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("ctime_ns", 0) == getattr(stat, "st_ctime_ns", 0)
            and prefix_matches
            and "user_messages" in cached
            and "turn_count" in cached
            and "compaction_count" in cached
            and "prompt_count" in cached
            and "latest_user_message" in cached
            and "candidate_ids" in cached
            and "evidence_truncated" in cached
        )
        if exact:
            evidence = self.context.accumulator(
                tokens=list(cached.get("candidate_ids", [])),
                truncated=bool(cached.get("evidence_truncated")),
            )
            return (
                int(cached.get("turn_count", 0)),
                int(cached.get("compaction_count", 0)),
                int(cached.get("prompt_count", cached.get("user_messages", 0))),
                clean_prompt(cached.get("latest_user_message")),
                evidence,
            )
        can_continue = bool(
            cached
            and signature_matches
            and cached.get("mode") == source.value
            and cached.get("inode") == stat.st_ino
            and prefix_matches
            and int(cached.get("mtime_ns", 0)) < stat.st_mtime_ns
            and 0 <= int(cached.get("offset", 0)) <= stat.st_size
            and int(cached.get("size", 0)) < stat.st_size
            and "user_messages" in cached
            and "latest_user_message" in cached
            and "candidate_ids" in cached
            and "evidence_truncated" in cached
        )
        turn_count = int(cached.get("turn_count", 0)) if can_continue and cached else 0
        compaction_count = int(cached.get("compaction_count", 0)) if can_continue and cached else 0
        prompt_count = (
            int(cached.get("prompt_count", cached.get("user_messages", 0)))
            if can_continue and cached
            else 0
        )
        latest = clean_prompt(cached.get("latest_user_message")) if can_continue and cached else ""
        latest_from_event = (
            bool(cached.get("latest_from_event")) if can_continue and cached else False
        )
        start = int(cached.get("offset", 0)) if can_continue and cached else 0
        evidence = self.context.accumulator(
            tokens=list(cached.get("candidate_ids", [])) if can_continue and cached else [],
            truncated=bool(cached.get("evidence_truncated")) if can_continue and cached else False,
        )
        offset = start
        last_kind = str(cached.get("last_activity_kind", "")) if can_continue and cached else ""
        last_text = (
            clean_prompt(cached.get("last_activity_text")) if can_continue and cached else ""
        )
        incomplete = False
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        incomplete = True
                        break
                    offset = handle.tell()
                    response_message = bool(
                        b"response_item" in line
                        and (b'"type":"message"' in line or b'"type": "message"' in line)
                    )
                    user_event = b"event_msg" in line and b"user_message" in line
                    compacted_event = (
                        b'"type":"compacted"' in line or b'"type": "compacted"' in line
                    )
                    if source is SourceKind.SUBAGENT:
                        if not response_message and not compacted_event:
                            continue
                    elif not response_message and not user_event and not compacted_event:
                        continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if item.get("type") == "compacted":
                        compaction_count += 1
                        last_kind = "compacted"
                        last_text = ""
                        continue
                    payload = item.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if item.get("type") == "response_item":
                        semantic_text = _codex_activity_text(payload)
                        if not semantic_text:
                            last_kind = ""
                            last_text = ""
                            continue
                        evidence.scan(semantic_text.encode("utf-8", "replace"))
                        role = payload.get("role")
                        if role == "assistant":
                            turn_count += 1
                            last_kind = "assistant"
                            last_text = semantic_text
                        elif role == "user":
                            paired = last_kind == "event_user" and last_text == semantic_text
                            if not paired:
                                turn_count += 1
                            if source is SourceKind.SUBAGENT:
                                prompt_count += 1
                                if not latest_from_event:
                                    latest = clean_prompt(semantic_text)
                            last_kind = "response_user"
                            last_text = semantic_text
                    elif item.get("type") == "event_msg":
                        value = _codex_event_text(payload)
                        if not value or source is SourceKind.SUBAGENT:
                            last_kind = ""
                            last_text = ""
                            continue
                        evidence.scan(value.encode("utf-8", "replace"))
                        prompt_count += 1
                        latest = clean_prompt(value)
                        latest_from_event = True
                        if not (last_kind == "response_user" and last_text == value):
                            turn_count += 1
                        last_kind = "event_user"
                        last_text = value
        except OSError:
            return turn_count, compaction_count, prompt_count, latest, evidence
        try:
            final_stat = path.stat()
        except OSError:
            final_stat = stat
        self.entries[key] = {
            "mode": source.value,
            "inode": final_stat.st_ino,
            # ``offset`` is the complete-record boundary.  A concurrent
            # writer's partial final record must be replayed next time.
            "size": offset,
            "mtime_ns": final_stat.st_mtime_ns,
            "ctime_ns": getattr(final_stat, "st_ctime_ns", 0),
            "offset": offset,
            "prefix_identity": _cached_prefix_identity(path, offset) or "",
            "user_messages": prompt_count,
            "turn_count": turn_count,
            "compaction_count": compaction_count,
            "prompt_count": prompt_count,
            "latest_user_message": latest,
            "latest_from_event": latest_from_event,
            "last_activity_kind": last_kind if not incomplete else last_kind,
            "last_activity_text": last_text if not incomplete else last_text,
            "candidate_ids": evidence.tokens,
            "evidence_truncated": evidence.truncated,
            "pattern_signature": self.context.pattern_signature,
        }
        self.dirty = True
        return turn_count, compaction_count, prompt_count, latest, evidence

    def save(self) -> None:
        if not self.context.use_cache or not self.dirty:
            return
        try:
            DISCOVERY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = DISCOVERY_CACHE_FILE.with_suffix(f".tmp-{os.getpid()}")
            temporary.write_text(
                json.dumps(
                    {"version": DISCOVERY_CACHE_VERSION, "entries": self.entries},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(DISCOVERY_CACHE_FILE)
        except OSError:
            pass


def discover(context: HarnessContext, *, use_cache: bool = True) -> list[NativeSession]:
    del use_cache
    home = REGISTRY.get("codex").home
    databases = list(home.glob("state_*.sqlite"))
    if not databases:
        if home.is_dir():
            record_warning(f"no Codex state database found in {home}")
        return []

    def database_version(path: Path) -> tuple[int, float]:
        match = re.search(r"state_(\d+)\.sqlite$", path.name)
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (int(match.group(1)) if match else 0, modified)

    database = max(databases, key=database_version)
    indexed_names: dict[str, str] = {}
    index_file = home / "session_index.jsonl"
    if index_file.exists():
        try:
            with index_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = item.get("id")
                    name = normalize_space(item.get("thread_name"))
                    if isinstance(session_id, str) and name:
                        indexed_names[session_id] = name
        except OSError:
            pass

    result: list[NativeSession] = []
    history = load_history()
    cache = DiscoveryCache(context)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT * FROM threads"):
            keys = set(row.keys())

            def field(name: str, default: Any = None) -> Any:
                return row[name] if name in keys else default

            session_id = str(field("id", ""))
            if not session_id:
                continue
            launch_source = str(field("source", "") or "")
            thread_source = str(field("thread_source", "") or "")
            is_subagent = (
                bool(field("agent_path"))
                or thread_source == "subagent"
                or "subagent" in launch_source
            )
            if is_subagent:
                source = SourceKind.SUBAGENT
            elif launch_source == "exec":
                source = SourceKind.NON_INTERACTIVE
            else:
                source = SourceKind.INTERACTIVE
            parent_id = ""
            nickname = normalize_space(field("agent_nickname"))
            if launch_source.startswith("{"):
                try:
                    spawn = json.loads(launch_source)["subagent"]["thread_spawn"]
                    parent_id = str(spawn.get("parent_thread_id") or "")
                    nickname = nickname or normalize_space(spawn.get("agent_nickname"))
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            name = indexed_names.get(session_id) or normalize_space(field("name"))
            fallback = field("title") or field("preview") or field("first_user_message")
            title = name or clean_prompt(fallback)
            indexed_preview = clean_prompt(field("preview") or field("first_user_message"))
            created = timestamp(field("created_at_ms") or field("created_at"))
            updated = timestamp(
                max(
                    int(field("updated_at_ms", 0) or 0),
                    int(field("recency_at_ms", 0) or 0),
                )
                or field("updated_at")
            )
            rollout_path = str(field("rollout_path", "") or "")
            if rollout_path and not Path(rollout_path).is_file():
                continue
            turn_count = 0
            compaction_count = 0
            prompt_count = 0
            latest = ""
            evidence = context.accumulator()
            if rollout_path:
                turn_count, compaction_count, prompt_count, latest, evidence = cache.scan(
                    Path(rollout_path), source
                )
                context.publish("codex", session_id, evidence)
            if source is SourceKind.INTERACTIVE and session_id in history:
                if not rollout_path:
                    prompt_count = int(history[session_id].get("count", 0))
                    turn_count = prompt_count
                if not latest:
                    latest = clean_prompt(history[session_id].get("latest"))
            cwd = str(field("cwd", "") or "")
            result.append(
                NativeSession(
                    tool="codex",
                    session_id=session_id,
                    title=title,
                    cwd=cwd,
                    updated=updated,
                    created=created,
                    preview=latest or indexed_preview,
                    named=bool(name),
                    storage=rollout_path,
                    source=source,
                    archived=bool(field("archived", False)),
                    resume_id=parent_id
                    if source is SourceKind.SUBAGENT and parent_id
                    else session_id,
                    parent_id=parent_id,
                    message_count=prompt_count,
                    turn_count=turn_count,
                    compaction_count=compaction_count,
                    prompt_count=prompt_count,
                    agent_nickname=nickname,
                )
            )
    except (sqlite3.Error, OSError) as error:
        record_warning(f"could not read Codex sessions from {database}: {error}")
        return []
    finally:
        if connection is not None:
            connection.close()
    cache.save()
    return result


def _numbered_database_key(path: Path) -> tuple[int, float]:
    try:
        number = int(path.stem.rsplit("_", 1)[-1])
    except ValueError:
        number = 0
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return number, modified


def _spawn_parents(home: Path) -> dict[str, str]:
    try:
        databases = list(home.glob("state_*.sqlite"))
    except OSError:
        return {}
    if not databases:
        return {}
    database = max(databases, key=_numbered_database_key)
    try:
        with closing(
            sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        ) as connection:
            rows = connection.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
            )
            return {str(child): str(parent) for parent, child in rows}
    except (sqlite3.Error, OSError) as error:
        record_warning(f"could not read Codex spawn edges from {database}: {error}")
        return {}


def _windows_liveness(context: LivenessContext, home: Path, eligible: set[str]) -> dict[str, int]:
    candidates = [
        process
        for process in context.process_snapshot
        if (
            process.name.casefold() in ("codex.exe", "codex")
            or "codex.exe" in " ".join(process.command).casefold()
        )
    ]
    if any(process.started_at <= 0 for process in candidates):
        record_warning("could not verify the start time of a live Codex process")
    live_processes = [process for process in candidates if process.started_at > 0]
    if not live_processes:
        return {}
    try:
        databases = list(home.glob("logs_*.sqlite"))
    except OSError:
        return {}
    if not databases:
        return {}
    database = max(databases, key=_numbered_database_key)
    parents = _spawn_parents(home)
    result: dict[str, int] = {}
    try:
        with closing(
            sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        ) as connection:
            for process in sorted(live_processes, key=lambda item: (item.started_at, item.pid)):
                # Codex logs store Unix epoch seconds plus a fractional nanosecond
                # column. Compare both so a recycled PID in the same second cannot
                # inherit the previous process instance's final thread.
                started_second = int(process.started_at)
                started_nanos = int((process.started_at - started_second) * 1_000_000_000)
                row = connection.execute(
                    """
                    SELECT thread_id FROM logs
                    WHERE process_uuid LIKE ? AND thread_id IS NOT NULL AND thread_id != ''
                        AND (ts > ? OR (ts = ? AND ts_nanos >= ?))
                    ORDER BY ts DESC, ts_nanos DESC LIMIT 1
                    """,
                    (
                        f"pid:{process.pid}:%",
                        started_second,
                        started_second,
                        started_nanos,
                    ),
                ).fetchone()
                if not row:
                    continue
                session_id = str(row[0])
                seen: set[str] = set()
                while session_id in parents and session_id not in seen:
                    seen.add(session_id)
                    session_id = parents[session_id]
                if session_id in eligible:
                    result[session_id] = process.pid
    except (sqlite3.Error, OSError) as error:
        record_warning(f"could not read Codex activity log {database}: {error}")
    return result


def inspect_liveness(
    context: LivenessContext, home: Path, sessions: Iterable[LivenessSession]
) -> dict[str, int]:
    """Resolve Codex activity using one shared process/lock snapshot."""
    eligible = {item.session_id for item in sessions if item.source is SourceKind.INTERACTIVE}
    if not eligible:
        return {}
    if context.platform == "win32":
        return _windows_liveness(context, home, eligible)
    if context.platform != "linux":
        return {}
    live_pids = {process.pid for process in context.process_snapshot}
    result: dict[str, int] = {}
    try:
        lock_files = sorted((home / "thread-writer-locks").glob("*.lock"))
    except OSError:
        lock_files = []
    for path in lock_files:
        try:
            stat = path.stat()
        except OSError:
            continue
        identity = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
        pid = context.lock_map.get(identity, 0)
        if path.stem in eligible and pid in live_pids:
            result[path.stem] = pid
    return result


def publish_name(session: Any, name: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    append_jsonl(
        REGISTRY.get("codex").home / "session_index.jsonl",
        {"id": session.session_id, "thread_name": name, "updated_at": stamp},
    )
    return ""


def resume_args(
    *,
    session_id: str,
    source: SourceKind,
    resume_id: str,
    parent_id: str,
    native: bool,
) -> list[str]:
    del parent_id
    target = resume_id if native and source is SourceKind.SUBAGENT else session_id
    result = ["resume"]
    if native and (
        source is SourceKind.NON_INTERACTIVE
        or (source is SourceKind.SUBAGENT and resume_id == session_id)
    ):
        result.append("--include-non-interactive")
    result.append(target)
    return result


ADAPTER = HarnessAdapter(
    name="codex",
    label="Codex",
    short_label="Codex",
    order=10,
    home=CODEX_HOME,
    default_command=("codex",),
    dangerous_args=("--dangerously-bypass-approvals-and-sandbox",),
    source_kinds=frozenset(
        (SourceKind.INTERACTIVE, SourceKind.NON_INTERACTIVE, SourceKind.SUBAGENT)
    ),
    id_patterns=(
        re.compile(
            rb"019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    scratch_patterns=(),
    read=_read_codex_snapshot,
    write=_write_codex,
    resolve=_codex_resolve,
    availability=_codex_availability,
    checkpoint=_codex_checkpoint,
    change_status=_codex_change_status,
    discover=discover,
    resume_args=resume_args,
    publish_name=publish_name,
    inspect_liveness=inspect_liveness,
    liveness_executables=frozenset(("codex", "codex.exe")),
    budget=BudgetPolicy(
        context_tokens=CODEX_BUDGET_CONTEXT_TOKENS,
        usable_fraction=ONE_M_CONTEXT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=("Codex 1M operational context policy, 2026-08-27"),
    ),
)
