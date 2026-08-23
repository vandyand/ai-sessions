"""Claude Code harness adapter."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from ..capabilities import HarnessAdapter
from ..conversion import (
    CLAUDE_BUDGET_CONTEXT_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_USABLE_FRACTION,
    BridgeError,
    _block_text,
    _Conversation,
    _digest_arguments,
    _iso,
    _records,
    _records_after,
    append_jsonl,
    scrub,
)
from ..discovery import HarnessContext, clean_prompt, normalize_space, prompt_text, timestamp
from ..model import BudgetPolicy, NativeSession, SourceKind, Transcript, Turn
from ..paths import APP_CACHE_DIR, CLAUDE_HOME
from ..registry import REGISTRY

CLAUDE_VERSION = "2.1.227"
DISCOVERY_CACHE_VERSION = 5
DISCOVERY_CACHE_FILE = APP_CACHE_DIR / f"claude-discovery-v{DISCOVERY_CACHE_VERSION}.json"


def claude_project_dir(cwd: str) -> Path:
    """Resolve Claude's cwd-flattened project directory from the live adapter."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd or str(Path.home()))
    return REGISTRY.get("claude").home / "projects" / slug


def read_claude(path: Path, *, latest_window: bool = True) -> Transcript:
    """Read mainline Claude messages, using sidechain only for an agent-only file."""
    main = _Conversation()
    sidechain = _Conversation()
    for record in _records(path):
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isMeta"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        conversation = sidechain if record.get("isSidechain") else main
        content = message.get("content")
        conversation.message(
            role, scrub(_block_text(content)), compaction=bool(record.get("isCompactSummary"))
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                conversation.call(
                    str(block.get("id", "")),
                    str(block.get("name", "")),
                    _digest_arguments(block.get("input")),
                )
            elif block.get("type") == "tool_result":
                body = block.get("content")
                conversation.result(
                    str(block.get("tool_use_id", "")),
                    body if isinstance(body, str) else _block_text(body),
                )
    del latest_window
    return Transcript(main.finish() or sidechain.finish(), main.opaque_compactions)


def write_claude_session(
    *, cwd: str, turns: list[Turn], title: str = "", created: float | None = None
) -> tuple[str, Path]:
    """Write a native appendable Claude transcript with linked message UUIDs."""
    session_id = str(uuid.uuid4())
    moment = time.time() if created is None else created
    directory = claude_project_dir(cwd)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BridgeError(f"could not create {directory}: {error}") from error
    path = directory / f"{session_id}.jsonl"
    lines: list[str] = []
    parent: str | None = None
    for index, turn in enumerate(turns):
        line_id = str(uuid.uuid4())
        record: dict[str, Any] = {
            "parentUuid": parent,
            "isSidechain": False,
            "type": turn.role,
            "uuid": line_id,
            "timestamp": _iso(moment + index),
            "cwd": cwd,
            "sessionId": session_id,
            "version": CLAUDE_VERSION,
            "userType": "external",
        }
        if turn.role == "user":
            record["message"] = {"role": "user", "content": turn.text}
            record["origin"] = {"kind": "human"}
        else:
            record["message"] = {
                "id": "msg_" + secrets.token_hex(12),
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": turn.text}],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        parent = line_id
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as error:
        raise BridgeError(f"could not write {path}: {error}") from error
    if title:
        append_jsonl(path, {"type": "custom-title", "customTitle": title, "sessionId": session_id})
    return session_id, path


def _claude_exists(session_id: str) -> bool:
    return any((REGISTRY.get("claude").home / "projects").rglob(f"{session_id}.jsonl"))


def _claude_change_status(path: Path, offset: int) -> str:
    """Ignore title/metadata appends and classify only semantic messages."""
    changed = False
    for record in _records_after(path, offset):
        if record.get("type") in (
            "ai_sessions_replaced",
            "ai_sessions_missing",
            "ai_sessions_incomplete",
        ):
            return "unstable"
        if record.get("type") not in ("user", "assistant") or record.get("isMeta"):
            continue
        message = record.get("message")
        if isinstance(message, dict) and message.get("content") not in (None, "", []):
            changed = True
    return "changed" if changed else "unchanged"


def load_history() -> dict[str, dict[str, Any]]:
    history_file = REGISTRY.get("claude").home / "history.jsonl"
    grouped: dict[str, dict[str, Any]] = {}
    if not history_file.exists():
        return grouped
    try:
        with history_file.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = item.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    continue
                value = clean_prompt(item.get("display"))
                event_time = timestamp(item.get("timestamp"))
                entry = grouped.setdefault(
                    session_id,
                    {
                        "first_prompt": "",
                        "last_prompt": "",
                        "cwd": "",
                        "created": 0.0,
                        "updated": 0.0,
                        "message_count": 0,
                    },
                )
                if value:
                    entry["message_count"] += 1
                    if not entry["first_prompt"]:
                        entry["first_prompt"] = value
                    entry["last_prompt"] = value
                project = item.get("project")
                if isinstance(project, str) and project:
                    entry["cwd"] = project
                if event_time:
                    if not entry["created"]:
                        entry["created"] = event_time
                    entry["updated"] = max(entry["updated"], event_time)
    except OSError:
        pass
    return grouped


class DiscoveryCache:
    """Incrementally extract Claude metadata and registry-sensitive ID evidence."""

    INTERESTING = (
        b'"type":"user"',
        b'"type": "user"',
        b'"type":"custom-title"',
        b'"type": "custom-title"',
        b'"type":"ai-title"',
        b'"type": "ai-title"',
        b'"type":"last-prompt"',
        b'"type": "last-prompt"',
    )

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

    @staticmethod
    def blank() -> dict[str, Any]:
        return {
            "offset": 0,
            "inode": 0,
            "size": 0,
            "mtime_ns": 0,
            "custom_title": "",
            "ai_title": "",
            "cwd": "",
            "first_prompt": "",
            "last_prompt": "",
            "agent_id": "",
            "parent_session_id": "",
            "slug": "",
            "entrypoint": "",
            "prompt_source": "",
            "candidate_ids": [],
            "evidence_truncated": False,
            "pattern_signature": "",
            "user_messages": 0,
            "created": 0.0,
            "updated": 0.0,
        }

    def scan(
        self,
        path: Path,
        *,
        allow_sidechain: bool = False,
        count_user_messages: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        key = str(path)
        stat = path.stat()
        cached = self.entries.get(key)
        signature_matches = bool(
            cached and cached.get("pattern_signature") == self.context.pattern_signature
        )
        needs_count = bool(count_user_messages and cached and "user_messages" not in cached)
        exact = bool(
            cached
            and signature_matches
            and not needs_count
            and cached.get("inode") == stat.st_ino
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and "candidate_ids" in cached
            and "evidence_truncated" in cached
        )
        if exact:
            evidence = self.context.accumulator(
                tokens=list(cached.get("candidate_ids", [])),
                truncated=bool(cached.get("evidence_truncated")),
            )
            return cached, evidence
        can_continue = bool(
            cached
            and signature_matches
            and not needs_count
            and cached.get("inode") == stat.st_ino
            and 0 <= int(cached.get("offset", 0)) <= stat.st_size
            and int(cached.get("size", 0)) < stat.st_size
            and "candidate_ids" in cached
            and "evidence_truncated" in cached
        )
        meta = dict(cached) if can_continue and cached else self.blank()
        start = int(meta.get("offset", 0)) if can_continue else 0
        evidence = self.context.accumulator(
            tokens=list(meta.get("candidate_ids", [])) if can_continue else [],
            truncated=bool(meta.get("evidence_truncated")) if can_continue else False,
        )
        read_stat = stat
        incomplete = False
        try:
            with path.open("rb") as handle:
                opened_stat = os.fstat(handle.fileno())
                if can_continue and (
                    opened_stat.st_ino != stat.st_ino or opened_stat.st_size < start
                ):
                    meta = self.blank()
                    start = 0
                    evidence = self.context.accumulator()
                handle.seek(start)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        incomplete = True
                        break
                    meta["offset"] = handle.tell()
                    evidence.scan(line)
                    if not any(marker in line for marker in self.INTERESTING):
                        continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    kind = item.get("type")
                    if allow_sidechain:
                        agent_id = item.get("agentId")
                        parent_id = item.get("sessionId")
                        slug = item.get("slug")
                        if isinstance(agent_id, str) and agent_id:
                            meta["agent_id"] = agent_id
                        if isinstance(parent_id, str) and parent_id:
                            meta["parent_session_id"] = parent_id
                        if isinstance(slug, str) and slug:
                            meta["slug"] = normalize_space(slug)
                    if kind == "custom-title":
                        value = normalize_space(item.get("customTitle"))
                        if value:
                            meta["custom_title"] = value
                    elif kind == "ai-title":
                        value = normalize_space(item.get("aiTitle"))
                        if value:
                            meta["ai_title"] = value
                    elif kind == "last-prompt":
                        value = clean_prompt(item.get("lastPrompt"))
                        if value:
                            meta["last_prompt"] = value
                    elif (
                        kind == "user"
                        and not item.get("isMeta")
                        and (allow_sidechain or not item.get("isSidechain"))
                    ):
                        entrypoint = normalize_space(item.get("entrypoint"))
                        prompt_source = normalize_space(item.get("promptSource"))
                        if entrypoint and not meta.get("entrypoint"):
                            meta["entrypoint"] = entrypoint
                        if prompt_source and not meta.get("prompt_source"):
                            meta["prompt_source"] = prompt_source
                        value = clean_prompt(prompt_text(item.get("message")))
                        if value:
                            if count_user_messages:
                                meta["user_messages"] = int(meta.get("user_messages", 0)) + 1
                            if not meta.get("first_prompt"):
                                meta["first_prompt"] = value
                            meta["last_prompt"] = value
                        cwd = item.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            meta["cwd"] = cwd
                        event_time = timestamp(item.get("timestamp"))
                        if event_time:
                            if not meta.get("created"):
                                meta["created"] = event_time
                            meta["updated"] = max(float(meta.get("updated", 0)), event_time)
                read_stat = os.fstat(handle.fileno())
        except OSError:
            incomplete_evidence = self.context.accumulator(tokens=evidence.tokens, truncated=True)
            return meta, incomplete_evidence
        offset = int(meta.get("offset", start))
        meta.update(
            inode=read_stat.st_ino,
            size=offset,
            mtime_ns=read_stat.st_mtime_ns,
            offset=offset,
            candidate_ids=evidence.tokens,
            evidence_truncated=evidence.truncated,
            pattern_signature=self.context.pattern_signature,
        )
        self.entries[key] = meta
        self.dirty = True
        if incomplete:
            evidence = self.context.accumulator(tokens=evidence.tokens, truncated=True)
        return meta, evidence

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
    projects = REGISTRY.get("claude").home / "projects"
    if not projects.exists():
        return []
    history = load_history()
    cache = DiscoveryCache(context)
    result: list[NativeSession] = []
    try:
        files = sorted(projects.glob("*/*.jsonl"))
    except OSError:
        files = []
    for path in files:
        session_id = path.stem
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", session_id):
            continue
        hist = history.get(session_id, {})
        try:
            meta, evidence = cache.scan(
                path,
                count_user_messages=not bool(hist.get("message_count")),
            )
            stat = path.stat()
        except OSError:
            continue
        context.publish("claude", session_id, evidence)
        custom = normalize_space(meta.get("custom_title"))
        automatic = normalize_space(meta.get("ai_title"))
        first = clean_prompt(meta.get("first_prompt") or hist.get("first_prompt"))
        last = clean_prompt(hist.get("last_prompt") or meta.get("last_prompt"))
        title = custom or automatic or first
        cwd = str(meta.get("cwd") or hist.get("cwd") or "")
        created = float(meta.get("created") or hist.get("created") or stat.st_ctime)
        updated = max(
            float(meta.get("updated") or 0),
            float(hist.get("updated") or 0),
            stat.st_mtime,
        )
        message_count = int(hist.get("message_count") or meta.get("user_messages") or 0)
        programmatic = meta.get("entrypoint") == "sdk-cli" or meta.get("prompt_source") == "sdk"
        result.append(
            NativeSession(
                tool="claude",
                session_id=session_id,
                title=title,
                cwd=cwd,
                updated=updated,
                created=created,
                preview=last or first,
                named=bool(custom),
                storage=str(path),
                source=SourceKind.SDK if programmatic else SourceKind.INTERACTIVE,
                resume_id=session_id,
                message_count=message_count,
            )
        )

    try:
        agent_files = sorted(
            path for path in projects.rglob("agent-*.jsonl") if "subagents" in path.parts
        )
    except OSError:
        agent_files = []
    for path in agent_files:
        fallback_agent_id = path.stem.removeprefix("agent-")
        try:
            fallback_parent_id = path.relative_to(projects).parts[1]
        except (ValueError, IndexError):
            fallback_parent_id = ""
        try:
            meta, evidence = cache.scan(path, allow_sidechain=True, count_user_messages=True)
            stat = path.stat()
        except OSError:
            continue
        agent_id = str(meta.get("agent_id") or fallback_agent_id)
        parent_id = str(meta.get("parent_session_id") or fallback_parent_id)
        if not agent_id or not parent_id:
            continue
        context.publish("claude", agent_id, evidence)
        first = clean_prompt(meta.get("first_prompt"))
        last = clean_prompt(meta.get("last_prompt"))
        slug = normalize_space(meta.get("slug"))
        title = slug or first or f"Claude subagent {agent_id[:8]}"
        parent_history = history.get(parent_id, {})
        result.append(
            NativeSession(
                tool="claude",
                session_id=agent_id,
                title=title,
                cwd=str(meta.get("cwd") or parent_history.get("cwd") or ""),
                updated=max(float(meta.get("updated") or 0), stat.st_mtime),
                created=float(meta.get("created") or stat.st_ctime),
                preview=last or first,
                named=False,
                storage=str(path),
                source=SourceKind.SUBAGENT,
                resume_id=parent_id,
                parent_id=parent_id,
                message_count=int(meta.get("user_messages") or 0),
            )
        )
    cache.save()
    return result


def publish_name(session: Any, name: str) -> str:
    transcript = Path(session.storage)
    if not transcript.is_file():
        return f"transcript for {session.session_id} is missing; name kept local only"
    append_jsonl(
        transcript,
        {"type": "custom-title", "customTitle": name, "sessionId": session.session_id},
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
    del source, resume_id, parent_id, native
    return ["--resume", session_id]


ADAPTER = HarnessAdapter(
    name="claude",
    label="Claude Code",
    short_label="Claude",
    order=20,
    home=CLAUDE_HOME,
    default_command=("claude",),
    dangerous_args=("--dangerously-skip-permissions",),
    source_kinds=frozenset((SourceKind.INTERACTIVE, SourceKind.SDK, SourceKind.SUBAGENT)),
    id_patterns=(
        re.compile(
            rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    scratch_patterns=(re.compile(r"(?:/tmp/claude-|\\Temp\\claude-)", re.I),),
    read=read_claude,
    write=write_claude_session,
    locate=_claude_exists,
    change_status=_claude_change_status,
    discover=discover,
    resume_args=resume_args,
    publish_name=publish_name,
    budget=BudgetPolicy(
        context_tokens=CLAUDE_BUDGET_CONTEXT_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=(
            "Claude 200k unknown-model floor from official context-window docs, "
            "2026-08-23; Opus 5 context is larger"
        ),
    ),
)
