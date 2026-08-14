#!/usr/bin/env python3
"""Browse and resume local Claude Code and Codex CLI sessions.

Core browsing uses only Python's standard library. Desktop focusing optionally
uses local tmux/wmctrl/xdotool commands. Original transcripts and databases are
never modified.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import locale
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .config import LAUNCH_MODES, LaunchConfig
from .paths import (
    CACHE_FILE,
    CLAUDE_HOME,
    CODEX_COUNT_CACHE_FILE,
    CODEX_HOME,
    HOME,
    IS_WINDOWS,
    STATE_FILE,
)

VERSION = "3.0.0"

TOOL_LABELS = {"codex": "Codex", "claude": "Claude"}
TOOL_ORDER = ("all", "codex", "claude")
ORIGIN_ORDER = ("human", "cross", "agent", "all")
ORIGIN_LABELS = {
    "human": "Human",
    "cross": "Cross",
    "agent": "Agent",
    "all": "All origins",
}
VISIBILITY_ORDER = ("visible", "hidden", "all")
VISIBILITY_LABELS = {"visible": "Visible", "hidden": "Hidden", "all": "Visible + hidden"}
SORT_LABELS = {
    "recent": "Recent",
    "title": "Title",
    "directory": "Directory",
    "messages": "Messages ↓",
    "open": "Open first",
}
CODEX_ID_PATTERN = re.compile(
    rb"019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


@dataclass(slots=True)
class Session:
    tool: str
    session_id: str
    title: str
    cwd: str
    updated: float
    created: float
    preview: str
    named: bool
    storage: str
    source: str = "interactive"
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

    @property
    def key(self) -> str:
        # Origin is inferred metadata and can improve over time.  Keep utility
        # names/visibility attached to the stable vendor session identity.
        return f"{self.tool}:{self.session_id}"

    @property
    def resume_target(self) -> str:
        return self.resume_id or self.session_id

    def searchable(self) -> str:
        return " ".join(
            (
                self.tool,
                TOOL_LABELS.get(self.tool, self.tool),
                self.session_id,
                self.title,
                self.cwd,
                self.preview,
                self.source,
                self.origin,
                self.parent_id,
                "hidden" if self.hidden else "visible",
                "open running active" if self.is_open else "closed inactive",
                self.original_title,
                str(self.message_count),
                str(self.open_pid) if self.open_pid else "",
            )
        ).casefold()


class UserState:
    """Private, utility-local names and visibility; vendor data stays untouched."""

    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.names: dict[str, str] = {}
        self.hidden: set[str] = set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") in (1, 2):
                names = payload.get("names", {})
                hidden = payload.get("hidden", [])
                if isinstance(names, dict):
                    self.names = {
                        self.stable_key(str(key)): normalize_space(value)
                        for key, value in names.items()
                        if normalize_space(value)
                    }
                if isinstance(hidden, list):
                    self.hidden = {self.stable_key(str(key)) for key in hidden}
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    @staticmethod
    def stable_key(value: str) -> str:
        """Migrate v1 tool:origin:id keys to origin-independent v2 keys."""
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[1] in ("human", "agent"):
            return f"{parts[0]}:{parts[2]}"
        return value

    def apply(self, sessions: Iterable[Session]) -> None:
        for item in sessions:
            if not item.original_title:
                item.original_title = item.title
                item.original_named = item.named
            item.title = item.original_title
            item.named = item.original_named
            item.renamed = False
            custom = self.names.get(item.key, "")
            if custom:
                item.title = custom
                item.named = True
                item.renamed = True
            item.hidden = item.key in self.hidden

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".tmp-{os.getpid()}")
        temp.write_text(
            json.dumps(
                {"version": 2, "names": self.names, "hidden": sorted(self.hidden)},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temp.chmod(0o600)
        temp.replace(self.path)

    def set_name(self, item: Session, name: str) -> None:
        value = normalize_space(name)[:200]
        if value:
            self.names[item.key] = value
        else:
            self.names.pop(item.key, None)
        self.save()

    def set_hidden(self, item: Session, hidden: bool) -> None:
        if hidden:
            self.hidden.add(item.key)
        else:
            self.hidden.discard(item.key)
        self.save()


def normalize_space(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def clean_prompt(value: Any) -> str:
    """Turn a first prompt or generated title into a useful one-line label."""
    text = normalize_space(value)
    if not text:
        return ""
    # Codex sessions often prefix the actual request with machine context.
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


def short_path(value: str) -> str:
    if not value:
        return "(unknown directory)"
    # Codex uses Win32's extended-length prefix for some native paths. It is
    # useful internally but noisy in a human-facing directory column.
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    try:
        path = Path(value).expanduser()
        if path == HOME:
            return "~"
        return "~/" + str(path.relative_to(HOME))
    except (ValueError, OSError):
        return value


def display_title(session: Session) -> str:
    title = clean_prompt(session.title)
    if not title:
        title = f"Untitled {TOOL_LABELS.get(session.tool, session.tool)} session"
    return title


def display_list_title(session: Session) -> str:
    """Make generated/unnamed titles explicit without a separate flag column."""
    title = display_title(session)
    return title if session.named else f"*- {title}"


def load_codex_history() -> dict[str, dict[str, Any]]:
    """Load counts and genuine latest prompts from Codex's user history."""
    sessions: dict[str, dict[str, Any]] = {}
    path = CODEX_HOME / "history.jsonl"
    if not path.exists():
        return sessions
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = item.get("session_id") or item.get("sessionId")
                if isinstance(sid, str) and sid:
                    entry = sessions.setdefault(sid, {"count": 0, "latest": ""})
                    entry["count"] = int(entry["count"]) + 1
                    latest = clean_prompt(item.get("text"))
                    if latest:
                        entry["latest"] = latest
    except OSError:
        pass
    return sessions


class CodexMessageCountCache:
    """Incrementally index user turns in append-only Codex rollouts."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False
        try:
            payload = json.loads(CODEX_COUNT_CACHE_FILE.read_text(encoding="utf-8"))
            if payload.get("version") == 2 and isinstance(payload.get("entries"), dict):
                self.entries = payload["entries"]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    def scan(self, path: Path, source: str) -> tuple[int, str]:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return 0, ""
        cached = self.entries.get(key)
        exact = bool(
            cached
            and cached.get("mode") == source
            and cached.get("inode") == stat.st_ino
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and "user_messages" in cached
            and "latest_user_message" in cached
        )
        if exact:
            return (
                int(cached.get("user_messages", 0)),
                clean_prompt(cached.get("latest_user_message")),
            )
        can_continue = bool(
            cached
            and cached.get("mode") == source
            and cached.get("inode") == stat.st_ino
            and 0 <= int(cached.get("offset", 0)) <= stat.st_size
            and int(cached.get("size", 0)) < stat.st_size
            and "user_messages" in cached
            and "latest_user_message" in cached
        )
        count = int(cached.get("user_messages", 0)) if can_continue and cached else 0
        latest = clean_prompt(cached.get("latest_user_message")) if can_continue and cached else ""
        latest_from_event = (
            bool(cached.get("latest_from_event")) if can_continue and cached else False
        )
        start = int(cached.get("offset", 0)) if can_continue and cached else 0
        offset = start
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    offset = handle.tell()
                    if source == "subagent":
                        response_user = bool(
                            b"response_item" in line
                            and (b'"role":"user"' in line or b'"role": "user"' in line)
                        )
                        user_event = b"event_msg" in line and b"user_message" in line
                        if not response_user and not user_event:
                            continue
                    else:
                        if b"event_msg" not in line or b"user_message" not in line:
                            continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    payload = item.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if item.get("type") == "event_msg" and payload.get("type") == "user_message":
                        value = clean_prompt(payload.get("message"))
                        if value:
                            latest = value
                            latest_from_event = True
                        if source != "subagent":
                            count += 1
                    elif source == "subagent":
                        if (
                            item.get("type") == "response_item"
                            and payload.get("type") == "message"
                            and payload.get("role") == "user"
                        ):
                            count += 1
                            if not latest_from_event:
                                value = clean_prompt(prompt_text(payload))
                                if value and not value.startswith("<codex_internal_context"):
                                    latest = value
        except OSError:
            return count, latest
        try:
            final_stat = path.stat()
        except OSError:
            final_stat = stat
        self.entries[key] = {
            "mode": source,
            "inode": final_stat.st_ino,
            "size": final_stat.st_size,
            "mtime_ns": final_stat.st_mtime_ns,
            "offset": offset,
            "user_messages": count,
            "latest_user_message": latest,
            "latest_from_event": latest_from_event,
        }
        self.dirty = True
        return count, latest

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            CODEX_COUNT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = CODEX_COUNT_CACHE_FILE.with_suffix(f".tmp-{os.getpid()}")
            temp.write_text(
                json.dumps({"version": 2, "entries": self.entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.chmod(0o600)
            temp.replace(CODEX_COUNT_CACHE_FILE)
        except OSError:
            pass


def load_codex_sessions() -> list[Session]:
    databases = list(CODEX_HOME.glob("state_*.sqlite"))
    if not databases:
        return []

    def database_version(path: Path) -> tuple[int, float]:
        match = re.search(r"state_(\d+)\.sqlite$", path.name)
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (int(match.group(1)) if match else 0, modified)

    # Codex may advance the state schema in a future release. Prefer the newest
    # numbered database rather than pinning this utility to today's filename.
    db = max(databases, key=database_version)

    indexed_names: dict[str, str] = {}
    index_file = CODEX_HOME / "session_index.jsonl"
    if index_file.exists():
        try:
            with index_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid, name = item.get("id"), normalize_space(item.get("thread_name"))
                    if isinstance(sid, str) and name:
                        indexed_names[sid] = name
        except OSError:
            pass

    result: list[Session] = []
    history = load_codex_history()
    count_cache = CodexMessageCountCache()
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM threads")
        for row in rows:
            keys = set(row.keys())

            def field(name: str, default: Any = None) -> Any:
                return row[name] if name in keys else default

            sid = str(field("id", ""))
            if not sid:
                continue
            launch_source = str(field("source", "") or "")
            thread_source = str(field("thread_source", "") or "")
            is_subagent = (
                bool(field("agent_path"))
                or thread_source == "subagent"
                or "subagent" in launch_source
            )
            if is_subagent:
                source = "subagent"
            elif launch_source == "exec":
                source = "non-interactive"
            else:
                source = "interactive"
            auxiliary = source != "interactive" or bool(field("archived", False))
            # Interactive CLI threads are human-launched. Codex exec threads
            # and internal subagents are normally created by agent workflows or
            # automation, so the UI groups both under Agent.
            origin = "human" if source == "interactive" else "agent"
            name = indexed_names.get(sid) or normalize_space(field("name"))
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
            if source == "interactive" and sid in history:
                message_count = int(history[sid].get("count", 0))
                latest_user_message = clean_prompt(history[sid].get("latest"))
            else:
                message_count, latest_user_message = (
                    count_cache.scan(Path(rollout_path), source) if rollout_path else (0, "")
                )
            result.append(
                Session(
                    tool="codex",
                    session_id=sid,
                    title=title,
                    cwd=str(field("cwd", "") or ""),
                    updated=updated,
                    created=created,
                    preview=latest_user_message or indexed_preview,
                    named=bool(name),
                    storage=rollout_path,
                    source=source,
                    auxiliary=auxiliary,
                    origin=origin,
                    resume_id=sid,
                    original_title=title,
                    original_named=bool(name),
                    message_count=message_count,
                )
            )
        connection.close()
        count_cache.save()
    except (sqlite3.Error, OSError):
        return []
    return result


class ClaudeMetadataCache:
    """Incrementally extracts titles and paths from Claude transcript JSONL."""

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

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if enabled:
            try:
                payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if payload.get("version") == 4 and isinstance(payload.get("entries"), dict):
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
            "codex_session_refs": [],
            "user_messages": 0,
            "created": 0.0,
            "updated": 0.0,
        }

    def scan(
        self,
        path: Path,
        allow_sidechain: bool = False,
        count_user_messages: bool = False,
    ) -> dict[str, Any]:
        key = str(path)
        stat = path.stat()
        cached = self.entries.get(key)
        needs_count = bool(count_user_messages and cached and "user_messages" not in cached)
        exact = bool(
            cached
            and not needs_count
            and cached.get("inode") == stat.st_ino
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
        )
        if exact:
            return cached  # type: ignore[return-value]

        can_continue = bool(
            cached
            and not needs_count
            and cached.get("inode") == stat.st_ino
            and 0 <= int(cached.get("offset", 0)) <= stat.st_size
            and int(cached.get("size", 0)) < stat.st_size
        )
        meta = dict(cached) if can_continue else self.blank()
        start = int(meta.get("offset", 0)) if can_continue else 0
        codex_refs = {
            str(value) for value in meta.get("codex_session_refs", []) if isinstance(value, str)
        }
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    meta["offset"] = handle.tell()
                    codex_refs.update(
                        match.decode("ascii") for match in CODEX_ID_PATTERN.findall(line)
                    )
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
        except OSError:
            return meta

        meta.update(
            inode=stat.st_ino,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            offset=stat.st_size,
            codex_session_refs=sorted(codex_refs),
        )
        self.entries[key] = meta
        self.dirty = True
        return meta

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = CACHE_FILE.with_suffix(f".tmp-{os.getpid()}")
            temp.write_text(
                json.dumps({"version": 4, "entries": self.entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.chmod(0o600)
            temp.replace(CACHE_FILE)
        except OSError:
            pass


def load_claude_history() -> dict[str, dict[str, Any]]:
    history_file = CLAUDE_HOME / "history.jsonl"
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
                sid = item.get("sessionId")
                if not isinstance(sid, str) or not sid:
                    continue
                value = clean_prompt(item.get("display"))
                event_time = timestamp(item.get("timestamp"))
                entry = grouped.setdefault(
                    sid,
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


def load_claude_sessions(
    use_cache: bool = True,
    codex_refs: set[str] | None = None,
) -> list[Session]:
    projects = CLAUDE_HOME / "projects"
    if not projects.exists():
        return []
    history = load_claude_history()
    cache = ClaudeMetadataCache(enabled=use_cache)
    result: list[Session] = []
    try:
        files = sorted(projects.glob("*/*.jsonl"))
    except OSError:
        files = []
    for path in files:
        # Primary transcripts are UUID.jsonl directly inside an encoded project
        # directory.  Agent logs live deeper under subagents/ and are excluded.
        sid = path.stem
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", sid):
            continue
        hist = history.get(sid, {})
        try:
            meta = cache.scan(
                path,
                count_user_messages=not bool(hist.get("message_count")),
            )
            stat = path.stat()
        except OSError:
            continue
        if codex_refs is not None:
            codex_refs.update(str(value) for value in meta.get("codex_session_refs", []))
        custom = normalize_space(meta.get("custom_title"))
        automatic = normalize_space(meta.get("ai_title"))
        first = clean_prompt(meta.get("first_prompt") or hist.get("first_prompt"))
        # Claude's global history is the most direct record of prompts typed in
        # interactive sessions. Programmatic sessions fall back to transcript
        # user events because they are normally absent from that history.
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
            Session(
                tool="claude",
                session_id=sid,
                title=title,
                cwd=cwd,
                updated=updated,
                created=created,
                preview=last or first,
                named=bool(custom),
                storage=str(path),
                source="sdk" if programmatic else "interactive",
                auxiliary=programmatic,
                origin="cross" if programmatic else "human",
                resume_id=sid,
                original_title=title,
                original_named=bool(custom),
                message_count=message_count,
            )
        )

    # Claude subagents are stored as nested transcripts. They are useful for
    # finding delegated work, but Claude resumes them through their owning
    # top-level session rather than as independent conversations.
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
            meta = cache.scan(path, allow_sidechain=True, count_user_messages=True)
            stat = path.stat()
        except OSError:
            continue
        if codex_refs is not None:
            codex_refs.update(str(value) for value in meta.get("codex_session_refs", []))
        agent_id = str(meta.get("agent_id") or fallback_agent_id)
        parent_id = str(meta.get("parent_session_id") or fallback_parent_id)
        if not agent_id or not parent_id:
            continue
        first = clean_prompt(meta.get("first_prompt"))
        last = clean_prompt(meta.get("last_prompt"))
        slug = normalize_space(meta.get("slug"))
        title = slug or first or f"Claude subagent {agent_id[:8]}"
        parent_history = history.get(parent_id, {})
        cwd = str(meta.get("cwd") or parent_history.get("cwd") or "")
        created = float(meta.get("created") or stat.st_ctime)
        updated = max(float(meta.get("updated") or 0), stat.st_mtime)
        result.append(
            Session(
                tool="claude",
                session_id=agent_id,
                title=title,
                cwd=cwd,
                updated=updated,
                created=created,
                preview=last or first,
                named=False,
                storage=str(path),
                source="subagent",
                auxiliary=True,
                origin="agent",
                resume_id=parent_id,
                parent_id=parent_id,
                original_title=title,
                message_count=int(meta.get("user_messages") or 0),
            )
        )
    cache.save()
    return result


def load_sessions(use_cache: bool = True, state: UserState | None = None) -> list[Session]:
    codex_refs: set[str] = set()
    claude_sessions = load_claude_sessions(use_cache=use_cache, codex_refs=codex_refs)
    codex_sessions = load_codex_sessions()
    for item in codex_sessions:
        # Claude captures the Codex thread ID in its transcript when it invokes
        # `codex exec`.  The Claude scratchpad path is a second strong native
        # signal for older calls whose output omitted that ID.
        if item.source == "non-interactive" and (
            item.session_id in codex_refs
            or "/tmp/claude-" in item.cwd
            or "\\Temp\\claude-" in item.cwd
        ):
            item.origin = "cross"
    sessions = codex_sessions + claude_sessions
    # In the unlikely event of duplicate metadata, prefer the most recently
    # updated record for a given tool/session pair.
    unique: dict[tuple[str, str], Session] = {}
    for item in sessions:
        key = (item.tool, item.session_id)
        if key not in unique or item.updated > unique[key].updated:
            unique[key] = item
    result = list(unique.values())
    if state is not None:
        state.apply(result)
    detect_open_sessions(result)
    return result


def process_start_token(pid: int) -> str:
    """Return Linux /proc start ticks, which protects against PID reuse."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The executable name is parenthesized and may contain spaces.  Field
        # 22 (starttime) is index 19 after the closing parenthesis.
        fields = value[value.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return ""


def held_file_locks() -> dict[tuple[int, int, int], int]:
    """Map Linux locked-file device/inode triples to their owning PID."""
    result: dict[tuple[int, int, int], int] = {}
    try:
        lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major_text, minor_text, inode_text = fields[5].split(":", 2)
            result[(int(major_text, 16), int(minor_text, 16), int(inode_text))] = int(fields[4])
        except (ValueError, IndexError):
            continue
    return result


def detect_open_sessions(sessions: Iterable[Session]) -> None:
    """Mark sessions owned by live Claude/Codex processes anywhere on the host.

    Claude publishes a PID/session registry. Codex holds an advisory lock whose
    filename is the thread ID. Both mechanisms work independently of terminal,
    pseudoterminal, and tmux layout.
    """
    session_list = list(sessions)
    by_identity = {(item.tool, item.session_id): item for item in session_list}
    for item in session_list:
        item.is_open = False
        item.open_pid = 0

    if IS_WINDOWS:
        from .platforms.windows import detect_open_sessions as detect_windows_sessions

        detect_windows_sessions(session_list, CLAUDE_HOME, CODEX_HOME)
        return

    registry = CLAUDE_HOME / "sessions"
    try:
        registry_files = registry.glob("*.json")
    except OSError:
        registry_files = ()
    for path in registry_files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pid = int(record.get("pid", 0))
            sid = str(record.get("sessionId", ""))
            expected_start = str(record.get("procStart", ""))
            kind = str(record.get("kind", ""))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            continue
        if (
            not pid
            or not sid
            or not expected_start
            or (kind and kind != "interactive")
            or process_start_token(pid) != expected_start
        ):
            continue
        item = by_identity.get(("claude", sid))
        if item and item.source == "interactive":
            item.is_open = True
            item.open_pid = pid

    locks = held_file_locks()
    try:
        writer_locks = (CODEX_HOME / "thread-writer-locks").glob("*.lock")
    except OSError:
        writer_locks = ()
    for path in writer_locks:
        try:
            stat = path.stat()
        except OSError:
            continue
        identity = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
        pid = locks.get(identity, 0)
        if not pid or not process_start_token(pid):
            continue
        item = by_identity.get(("codex", path.stem))
        if item and item.source == "interactive":
            item.is_open = True
            item.open_pid = pid


def process_environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        result[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return result


def process_parent(pid: int) -> int:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = value[value.rfind(")") + 2 :].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return 0


def process_state(pid: int) -> str:
    if IS_WINDOWS:
        return ""
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = value[value.rfind(")") + 2 :].split()
        return fields[0]
    except (OSError, IndexError):
        return ""


def process_ancestry(pid: int) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        result.append(pid)
        pid = process_parent(pid)
    return result


def run_capture(
    argv: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def focus_open_session(item: Session) -> tuple[bool, str]:
    """Select an open session's tmux pane and raise its desktop terminal."""
    if IS_WINDOWS:
        from .platforms.windows import process_exists

        if not item.is_open or not item.open_pid or not process_exists(item.open_pid):
            return False, "That session is no longer open. Press Ctrl-R to refresh."
        return False, (
            f"Already open in Windows Terminal (PID {item.open_pid}); "
            "exact tab focusing is not supported."
        )
    if not item.is_open or not item.open_pid or not process_start_token(item.open_pid):
        return False, "That session is no longer open. Press Ctrl-R to refresh."

    process_env = process_environment(item.open_pid)
    pane_id = process_env.get("TMUX_PANE", "")
    tmux_session = ""
    tmux_window = ""
    roots: list[int] = []

    if pane_id:
        info = run_capture(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{session_name}\t#{window_id}\t#{window_index}\t#{pane_id}",
            ]
        )
        if not info or info.returncode != 0 or not info.stdout.strip():
            return False, f"Session is open, but tmux pane {pane_id} could not be located."
        fields = info.stdout.strip().split("\t")
        if len(fields) < 4:
            return (
                False,
                f"Session is open, but tmux returned incomplete pane metadata for {pane_id}.",
            )
        tmux_session, window_id, tmux_window, pane_id = fields[:4]
        selected_window = run_capture(["tmux", "select-window", "-t", window_id])
        selected_pane = run_capture(["tmux", "select-pane", "-t", pane_id])
        if (
            not selected_window
            or selected_window.returncode != 0
            or not selected_pane
            or selected_pane.returncode != 0
        ):
            return False, f"Could not select tmux target {tmux_session}:{tmux_window}.{pane_id}."

        clients = run_capture(["tmux", "list-clients", "-F", "#{client_session}\t#{client_pid}"])
        if clients and clients.returncode == 0:
            for line in clients.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[0] == tmux_session:
                    try:
                        roots.append(int(parts[1]))
                    except ValueError:
                        pass
    if not roots:
        roots.append(item.open_pid)

    # Match an ancestor process to _NET_WM_PID. This avoids guessing from a
    # title and works for Kitty, GNOME Terminal, and other EWMH terminals.
    ancestors: list[int] = []
    for root in roots:
        for pid in process_ancestry(root):
            if pid not in ancestors:
                ancestors.append(pid)
    displays: list[str] = []
    for pid in ancestors:
        display = process_environment(pid).get("DISPLAY", "")
        if display and display not in displays:
            displays.append(display)
    fallback_display = process_env.get("DISPLAY", "")
    if fallback_display and fallback_display not in displays:
        displays.append(fallback_display)

    for display in displays:
        desktop_env = dict(os.environ)
        desktop_env["DISPLAY"] = display
        windows = run_capture(["wmctrl", "-lpGx"], env=desktop_env)
        if not windows or windows.returncode != 0:
            continue
        candidates: list[str] = []
        title_fallback: list[str] = []
        for line in windows.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 3:
                continue
            window_id = parts[0]
            try:
                owner_pid = int(parts[2])
            except ValueError:
                continue
            if owner_pid in ancestors:
                candidates.append(window_id)
            elif tmux_session and f"tmux:{tmux_session}" in line:
                title_fallback.append(window_id)
        for window_id in candidates + title_fallback:
            focused = run_capture(["wmctrl", "-i", "-a", window_id], env=desktop_env)
            if focused and focused.returncode == 0:
                # wmctrl activation is asynchronous under Mutter. xdotool's
                # --sync makes Enter feel immediate when it is available.
                run_capture(
                    ["xdotool", "windowactivate", "--sync", window_id],
                    env=desktop_env,
                )
                location = (
                    f"tmux {tmux_session}:{tmux_window} pane {pane_id}"
                    if tmux_session
                    else f"terminal process {item.open_pid}"
                )
                if process_state(item.open_pid) in ("T", "t"):
                    note = (
                        f"AI Sessions: {display_title(item)} is suspended in {location}; "
                        "quit the current foreground program to Bash, run `jobs -l`, "
                        "then use `fg %N` with the listed job number."
                    )
                    if pane_id:
                        run_capture(["tmux", "display-message", "-t", pane_id, note])
                    return True, (
                        f"Focused {location}; suspended—quit its foreground program, "
                        "then run `jobs -l` and `fg %N` in that pane's Bash shell."
                    )
                return True, f"Focused {location}."

    if tmux_session:
        return (
            False,
            f"Selected tmux {tmux_session}:{tmux_window} pane {pane_id}, but could not raise its desktop window.",
        )
    return False, "The session is open, but its desktop terminal window could not be identified."


def query_match(session: Session, query: str) -> tuple[bool, int]:
    words = [word for word in query.casefold().split() if word]
    if not words:
        return True, 0
    haystack = session.searchable()
    title = session.title.casefold()
    cwd = session.cwd.casefold()
    score = 0
    for word in words:
        if word.startswith("tool:"):
            wanted = word[5:]
            if wanted and not session.tool.startswith(wanted):
                return False, 0
            score += 25
            continue
        if word.startswith("dir:"):
            wanted = word[4:]
            if wanted and wanted not in cwd:
                return False, 0
            score += 15
            continue
        if word.startswith("name:"):
            wanted = word[5:]
            if wanted and wanted not in title:
                return False, 0
            score += 20
            continue
        if word.startswith("origin:"):
            wanted = word[7:]
            if wanted and not session.origin.startswith(wanted):
                return False, 0
            score += 20
            continue
        if word in ("hidden:true", "is:hidden"):
            if not session.hidden:
                return False, 0
            score += 10
            continue
        if word in ("hidden:false", "is:visible"):
            if session.hidden:
                return False, 0
            score += 10
            continue
        if word in ("open:true", "is:open"):
            if not session.is_open:
                return False, 0
            score += 25
            continue
        if word in ("open:false", "is:closed"):
            if session.is_open:
                return False, 0
            score += 10
            continue
        if word not in haystack:
            return False, 0
        if title.startswith(word):
            score += 30
        elif word in title:
            score += 20
        elif word in cwd:
            score += 10
        else:
            score += 3
    return True, score


def filtered_sessions(
    sessions: Iterable[Session],
    tool: str = "all",
    directory: str = "",
    query: str = "",
    origin: str = "human",
    visibility: str = "visible",
    include_auxiliary: bool | None = None,
    sort_mode: str = "recent",
) -> list[Session]:
    # Backward compatibility for scripts written against v1.
    if include_auxiliary:
        origin = "all"
    matches: list[tuple[int, Session]] = []
    for item in sessions:
        if origin != "all" and item.origin != origin:
            continue
        if visibility == "visible" and item.hidden:
            continue
        if visibility == "hidden" and not item.hidden:
            continue
        if tool != "all" and item.tool != tool:
            continue
        if directory and item.cwd != directory:
            continue
        matched, score = query_match(item, query)
        if matched:
            matches.append((score, item))
    if query:
        return [item for _, item in sorted(matches, key=lambda pair: (-pair[0], -pair[1].updated))]
    if sort_mode == "title":
        return [
            item
            for _, item in sorted(
                matches, key=lambda pair: (display_title(pair[1]).casefold(), -pair[1].updated)
            )
        ]
    if sort_mode == "directory":
        return [
            item
            for _, item in sorted(
                matches, key=lambda pair: (pair[1].cwd.casefold(), -pair[1].updated)
            )
        ]
    if sort_mode == "messages":
        return [
            item
            for _, item in sorted(
                matches, key=lambda pair: (-pair[1].message_count, -pair[1].updated)
            )
        ]
    if sort_mode == "open":
        return [
            item
            for _, item in sorted(matches, key=lambda pair: (not pair[1].is_open, -pair[1].updated))
        ]
    return [item for _, item in sorted(matches, key=lambda pair: -pair[1].updated)]


def relative_time(value: float) -> str:
    if not value:
        return "unknown"
    seconds = max(0, dt.datetime.now().timestamp() - value)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 604_800:
        return f"{int(seconds // 86_400)}d ago"
    date = dt.datetime.fromtimestamp(value)
    if date.year == dt.datetime.now().year:
        return date.strftime("%b %d")
    return date.strftime("%Y-%m-%d")


def exact_time(value: float) -> str:
    if not value:
        return "unknown"
    return dt.datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def ellipsize(value: str, width: int) -> str:
    value = normalize_space(value)
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


class Browser:
    PAIRS = {
        "accent": 1,
        "codex": 2,
        "claude": 3,
        "selected": 4,
        "primary": 5,
        "warning": 6,
        "success": 7,
        "muted": 8,
        "human": 9,
        "cross": 10,
        "agent": 11,
        "hidden": 12,
        "timestamp": 13,
        "messages": 14,
    }

    def __init__(
        self,
        screen: Any,
        sessions: list[Session],
        state: UserState,
        launch_config: LaunchConfig,
        use_cache: bool = True,
    ) -> None:
        self.screen = screen
        self.sessions = sessions
        self.state = state
        self.launch_config = launch_config
        self.use_cache = use_cache
        self.tool = "all"
        self.origin = "human"
        self.visibility = "visible"
        self.directory = ""
        self.query = ""
        self.sort_mode = "recent"
        self.selected = 0
        self.offset = 0
        self.searching = False
        self.message = ""
        self.result: Session | None = None
        self.setup_colors()

    def setup_colors(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.colors_enabled = bool(
            not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb"
            and curses.has_colors()
        )
        self.initialized_pairs: set[int] = set()
        if not self.colors_enabled:
            return
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        if getattr(curses, "COLORS", 0) >= 256:
            # Theme-friendly xterm-256 colors: cool provider hues, green live
            # state, amber cross-provider state, and quiet neutral metadata.
            colors = (45, 75, 176, 16, -1, 214, 82, 245, 114, 215, 141, 245, 110, 223)
            backgrounds = (-1, -1, -1, 45, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1)
        else:
            colors = (
                curses.COLOR_CYAN,
                curses.COLOR_BLUE,
                curses.COLOR_MAGENTA,
                curses.COLOR_BLACK,
                -1,
                curses.COLOR_YELLOW,
                curses.COLOR_GREEN,
                curses.COLOR_WHITE,
                curses.COLOR_GREEN,
                curses.COLOR_YELLOW,
                curses.COLOR_CYAN,
                curses.COLOR_WHITE,
                curses.COLOR_CYAN,
                curses.COLOR_YELLOW,
            )
            backgrounds = (-1, -1, -1, curses.COLOR_CYAN) + (-1,) * 10
        for number, (foreground, background) in enumerate(zip(colors, backgrounds), 1):
            if number >= getattr(curses, "COLOR_PAIRS", 0):
                break
            try:
                curses.init_pair(number, foreground, background)
                self.initialized_pairs.add(number)
            except curses.error:
                pass

    def style(self, name: str = "primary", attrs: int = 0, selected: bool = False) -> int:
        pair = self.PAIRS["selected" if selected else name]
        if self.colors_enabled and pair in self.initialized_pairs:
            return attrs | curses.color_pair(pair)
        if selected:
            return attrs | curses.A_REVERSE | curses.A_BOLD
        return attrs

    def add(self, y: int, x: int, value: str, style: int = 0, width: int | None = None) -> None:
        height, total_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= total_width:
            return
        available = max(0, total_width - x - (1 if y == height - 1 else 0))
        if width is not None:
            available = min(available, max(0, width))
        try:
            self.screen.addnstr(y, x, value, available, style)
        except curses.error:
            pass

    def current(self) -> list[Session]:
        return filtered_sessions(
            self.sessions,
            tool=self.tool,
            directory=self.directory,
            query=self.query,
            origin=self.origin,
            visibility=self.visibility,
            sort_mode=self.sort_mode,
        )

    def keep_selection(self, previous_id: str = "") -> None:
        items = self.current()
        if previous_id:
            for index, item in enumerate(items):
                if item.key == previous_id:
                    self.selected = index
                    break
            else:
                self.selected = min(self.selected, max(0, len(items) - 1))
        else:
            self.selected = min(self.selected, max(0, len(items) - 1))

    def selected_id(self) -> str:
        items = self.current()
        if items and 0 <= self.selected < len(items):
            return items[self.selected].key
        return ""

    def draw(self) -> None:
        self.screen.erase()
        detect_open_sessions(self.sessions)
        height, width = self.screen.getmaxyx()
        items = self.current()
        if height < 12 or width < 65:
            self.add(0, 0, "Terminal too small", self.style("warning", curses.A_BOLD))
            self.add(2, 0, "Resize to at least 65 columns × 12 rows.")
            self.screen.refresh()
            return

        self.add(0, 1, "AI SESSIONS", self.style("accent", curses.A_BOLD))
        open_count = sum(item.is_open for item in self.sessions)
        mode_text = f"Launch: {self.launch_config.mode.upper()}"
        mode_style = "warning" if self.launch_config.mode == "dangerous" else "success"
        self.add(0, 14, mode_text, self.style(mode_style, curses.A_BOLD))
        count_text = f"{open_count} open · {len(items)} shown · {len(self.sessions)} indexed"
        self.add(0, max(1, width - len(count_text) - 2), count_text, self.style("muted"))

        tool_text = "All tools" if self.tool == "all" else TOOL_LABELS[self.tool]
        dir_text = "All directories" if not self.directory else short_path(self.directory)
        filters = (
            f"{tool_text}  ·  {ORIGIN_LABELS[self.origin]}  ·  "
            f"{VISIBILITY_LABELS[self.visibility]}  ·  "
            f"{ellipsize(dir_text, max(10, width // 4))}  ·  {SORT_LABELS[self.sort_mode]}"
        )
        self.add(1, 1, filters, self.style("muted"), width - 2)

        prompt_style = self.style("accent" if self.searching else "primary", curses.A_BOLD)
        prompt = "Search › " + self.query
        if not self.query and not self.searching:
            prompt += "Ctrl-F or / to search"
        self.add(2, 1, prompt, prompt_style, width - 2)

        detail_height = 7
        list_top = 4
        footer_row = height - 1
        detail_top = height - detail_height - 1
        visible_rows = max(1, detail_top - list_top)
        self.selected = min(self.selected, max(0, len(items) - 1))
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + visible_rows:
            self.offset = self.selected - visible_rows + 1
        self.offset = min(self.offset, max(0, len(items) - visible_rows))

        show_directory = width >= 100
        tool_width, origin_width, open_width, messages_width, time_width = 8, 7, 6, 6, 10
        directory_width = min(34, max(16, width // 4)) if show_directory else 0
        title_width = (
            width
            - 5
            - tool_width
            - origin_width
            - open_width
            - messages_width
            - (time_width * 2)
            - directory_width
        )
        heading = (
            f"  {'TOOL':<{tool_width}}{'ORIGIN':<{origin_width}}"
            f"{'OPEN':<{open_width}}{'MSGS':<{messages_width}}"
            f"{'STARTED':<{time_width}}{'UPDATED':<{time_width}}TITLE"
        )
        self.add(3, 1, heading, self.style("muted", curses.A_BOLD), width - 2)

        if not items:
            self.add(list_top + 1, 3, "No sessions match these filters.", self.style("warning"))
            self.add(
                list_top + 3,
                3,
                "Esc clears the search; Tab changes tools; d changes directory.",
                self.style("muted"),
            )
        else:
            for row, item in enumerate(items[self.offset : self.offset + visible_rows]):
                index = self.offset + row
                y = list_top + row
                selected = index == self.selected
                marker = "›" if selected else ("⊘" if item.hidden else " ")
                tool_label = TOOL_LABELS[item.tool]
                origin_label = ORIGIN_LABELS[item.origin]
                directory = short_path(item.cwd)
                paused = item.is_open and process_state(item.open_pid) in ("T", "t")
                dim = curses.A_DIM if item.hidden and not selected else 0
                x = 1

                def segment(value: str, color: str = "primary", attrs: int = 0) -> None:
                    nonlocal x
                    self.add(y, x, value, self.style(color, attrs | dim, selected=selected))
                    x += len(value)

                marker_color = "hidden" if item.hidden else "primary"
                segment(f"{marker} ", marker_color, curses.A_BOLD if marker.strip() else 0)
                segment(f"{tool_label:<{tool_width}}", item.tool, curses.A_BOLD)
                segment(f"{origin_label:<{origin_width}}", item.origin, curses.A_BOLD)
                open_symbol = "Ⅱ" if paused else ("●" if item.is_open else "")
                segment(
                    f"{open_symbol:<{open_width}}",
                    "warning" if paused else "success",
                    curses.A_BOLD,
                )
                segment(f"{item.message_count:<{messages_width}}", "messages")
                segment(f"{relative_time(item.created):<{time_width}}", "muted")
                segment(f"{relative_time(item.updated):<{time_width}}", "timestamp")
                if item.named:
                    title = ellipsize(display_title(item), title_width)
                    segment(f"{title:<{title_width}}", "primary", curses.A_BOLD)
                else:
                    prefix = "*- "
                    title = ellipsize(display_title(item), max(0, title_width - len(prefix)))
                    segment(prefix, "warning", curses.A_BOLD)
                    segment(f"{title:<{max(0, title_width - len(prefix))}}", "primary")
                if show_directory:
                    segment(ellipsize(directory, directory_width).ljust(directory_width), "muted")

        for x in range(1, width - 1):
            self.add(detail_top, x, "─", self.style("muted", curses.A_DIM))
        if items:
            item = items[self.selected]
            missing = bool(item.cwd and not Path(item.cwd).is_dir())
            name_badge = " · utility name" if item.renamed else (" · named" if item.named else "")
            source_badge = f" · {item.source}" if item.source != "interactive" else ""
            hidden_badge = " · hidden" if item.hidden else ""
            if item.is_open and process_state(item.open_pid) in ("T", "t"):
                open_badge = f" · PAUSED in terminal (PID {item.open_pid})"
            else:
                open_badge = f" · OPEN now (PID {item.open_pid})" if item.is_open else ""
            self.add(
                detail_top + 1,
                2,
                f"{TOOL_LABELS[item.tool]} · {ORIGIN_LABELS[item.origin]} · "
                f"{item.message_count} user msgs{name_badge}{source_badge}{hidden_badge}{open_badge}",
                self.style(item.origin, curses.A_BOLD),
                width - 4,
            )
            self.add(
                detail_top + 2,
                2,
                f"Started    {exact_time(item.created)}  ·  Updated {exact_time(item.updated)}",
                self.style("timestamp"),
                width - 4,
            )
            path_style = self.style("warning" if missing else "primary")
            path_note = "  [directory missing]" if missing else ""
            self.add(
                detail_top + 3,
                2,
                "Directory  " + short_path(item.cwd) + path_note,
                path_style,
                width - 4,
            )
            identity = "Agent log " if item.parent_id else "Session   "
            self.add(
                detail_top + 4, 2, identity + " " + item.session_id, self.style("muted"), width - 4
            )
            latest_user_message = clean_prompt(item.preview)
            detail_row = detail_top + 5
            if item.parent_id:
                self.add(
                    detail_row,
                    2,
                    "Opens      parent session " + item.resume_target,
                    self.style("warning"),
                    width - 4,
                )
                detail_row += 1
            if latest_user_message:
                label = "Latest user message  "
                self.add(
                    detail_row,
                    2,
                    label + ellipsize(latest_user_message, width - len(label) - 4),
                    self.style("primary"),
                    width - 4,
                )

        footer = (
            self.message
            or "Enter focus/resume  Ctrl-F search  p launch  o origin  v view  s sort  r rename  h hide  ? help"
        )
        self.add(footer_row, 1, footer, self.style("accent", curses.A_BOLD), width - 2)
        if self.searching:
            try:
                curses.curs_set(1)
                cursor_x = min(width - 2, len("Search › ") + len(self.query) + 1)
                self.screen.move(2, cursor_x)
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        self.screen.refresh()

    def cycle_tool(self, backwards: bool = False) -> None:
        previous = self.selected_id()
        index = TOOL_ORDER.index(self.tool)
        self.tool = TOOL_ORDER[(index + (-1 if backwards else 1)) % len(TOOL_ORDER)]
        if self.directory and not any(
            item.cwd == self.directory and (self.tool == "all" or item.tool == self.tool)
            for item in self.sessions
        ):
            self.directory = ""
        self.keep_selection(previous)

    def cycle_origin(self, backwards: bool = False) -> None:
        previous = self.selected_id()
        index = ORIGIN_ORDER.index(self.origin)
        self.origin = ORIGIN_ORDER[(index + (-1 if backwards else 1)) % len(ORIGIN_ORDER)]
        self.keep_selection(previous)

    def cycle_visibility(self, backwards: bool = False) -> None:
        previous = self.selected_id()
        index = VISIBILITY_ORDER.index(self.visibility)
        self.visibility = VISIBILITY_ORDER[
            (index + (-1 if backwards else 1)) % len(VISIBILITY_ORDER)
        ]
        self.keep_selection(previous)

    def cycle_sort(self) -> None:
        previous = self.selected_id()
        order = ("recent", "title", "directory", "messages", "open")
        self.sort_mode = order[(order.index(self.sort_mode) + 1) % len(order)]
        self.keep_selection(previous)

    def cycle_launch_mode(self) -> None:
        mode = self.launch_config.cycle_mode()
        if mode == "dangerous":
            self.message = "DANGEROUS launch mode: provider safeguards will be bypassed."
        elif mode == "custom":
            self.message = "Custom launch mode selected; arguments come from config.toml."
        else:
            self.message = "Safe launch mode: provider permission defaults apply."

    def directory_picker(self) -> None:
        counts: dict[str, int] = {}
        eligible = filtered_sessions(
            self.sessions,
            tool=self.tool,
            origin=self.origin,
            visibility=self.visibility,
        )
        for item in eligible:
            if item.cwd:
                counts[item.cwd] = counts.get(item.cwd, 0) + 1
        all_options = [""] + sorted(counts, key=lambda value: short_path(value).casefold())
        search = ""
        selected = 0
        try:
            selected = all_options.index(self.directory)
        except ValueError:
            pass
        while True:
            options = [
                option
                for option in all_options
                if not search or search.casefold() in short_path(option).casefold()
            ]
            if not options:
                selected = 0
            else:
                selected = min(selected, len(options) - 1)
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            box_width = min(max(54, width - 16), 86)
            box_height = min(max(10, height - 8), 24)
            top = max(0, (height - box_height) // 2)
            left = max(0, (width - box_width) // 2)
            self.add(top, left, "┌" + "─" * (box_width - 2) + "┐", self.style("accent"), box_width)
            for y in range(top + 1, top + box_height - 1):
                self.add(y, left, "│", self.style("accent"))
                self.add(y, left + box_width - 1, "│", self.style("accent"))
            self.add(
                top + box_height - 1,
                left,
                "└" + "─" * (box_width - 2) + "┘",
                self.style("accent"),
                box_width,
            )
            self.add(
                top + 1,
                left + 2,
                "CHOOSE DIRECTORY",
                self.style("accent", curses.A_BOLD),
                box_width - 4,
            )
            self.add(top + 2, left + 2, "Filter › " + search, self.style("primary"), box_width - 4)
            rows = box_height - 6
            offset = max(0, min(selected - rows + 1, len(options) - rows))
            for row, option in enumerate(options[offset : offset + rows]):
                index = offset + row
                label = "All directories" if not option else short_path(option)
                count = sum(counts.values()) if not option else counts.get(option, 0)
                count_text = f"{count:>3}"
                line = f"{'›' if index == selected else ' '} {ellipsize(label, box_width - 10):<{box_width - 10}}{count_text}"
                style = self.style("primary", curses.A_BOLD, selected=index == selected)
                self.add(top + 4 + row, left + 2, line, style, box_width - 4)
            self.add(
                top + box_height - 2,
                left + 2,
                "Type to filter · Enter choose · Esc cancel",
                self.style("muted"),
                box_width - 4,
            )
            self.screen.refresh()
            key = self.screen.get_wch()
            if key == "\x1b":
                return
            if key in ("\n", "\r", curses.KEY_ENTER):
                if options:
                    previous = self.selected_id()
                    self.directory = options[selected]
                    self.keep_selection(previous)
                return
            if key in (curses.KEY_UP, "\x10"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "\x0e"):
                selected = min(max(0, len(options) - 1), selected + 1)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                search = search[:-1]
                selected = 0
            elif isinstance(key, str) and key.isprintable():
                search += key
                selected = 0

    def name_prompt(self, item: Session) -> str | None:
        value = self.state.names.get(item.key, "")
        while True:
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            box_width = min(max(56, width - 12), 88)
            top = max(1, height // 2 - 4)
            left = max(0, (width - box_width) // 2)
            self.add(top, left, "┌" + "─" * (box_width - 2) + "┐", self.style("accent"), box_width)
            for y in range(top + 1, min(height - 1, top + 7)):
                self.add(y, left, "│", self.style("accent"))
                self.add(y, left + box_width - 1, "│", self.style("accent"))
            self.add(
                top + 7, left, "└" + "─" * (box_width - 2) + "┘", self.style("accent"), box_width
            )
            self.add(
                top + 1,
                left + 2,
                "RENAME SESSION",
                self.style("accent", curses.A_BOLD),
                box_width - 4,
            )
            self.add(
                top + 2,
                left + 2,
                "Original: "
                + ellipsize(item.original_title or display_title(item), box_width - 14),
                self.style("muted"),
                box_width - 4,
            )
            self.add(top + 4, left + 2, "Name › " + value, curses.A_BOLD, box_width - 4)
            self.add(
                top + 6,
                left + 2,
                "Enter save · empty restores original · Esc cancel",
                self.style("muted"),
                box_width - 4,
            )
            try:
                curses.curs_set(1)
                self.screen.move(top + 4, min(left + box_width - 3, left + 9 + len(value)))
            except curses.error:
                pass
            self.screen.refresh()
            key = self.screen.get_wch()
            if key == "\x1b":
                return None
            if key in ("\n", "\r", curses.KEY_ENTER):
                return normalize_space(value)[:200]
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif key == "\x15":
                value = ""
            elif key == "\x17":
                value = value.rstrip().rsplit(" ", 1)[0] if " " in value.rstrip() else ""
            elif isinstance(key, str) and key.isprintable() and len(value) < 200:
                value += key

    def rename_selected(self) -> None:
        items = self.current()
        if not items:
            return
        item = items[self.selected]
        value = self.name_prompt(item)
        if value is None:
            return
        self.state.set_name(item, value)
        self.state.apply(self.sessions)
        self.message = "Name saved." if value else "Original name restored."

    def toggle_hidden(self) -> None:
        items = self.current()
        if not items:
            return
        item = items[self.selected]
        previous = item.key
        if item.hidden:
            self.state.set_hidden(item, False)
            self.state.apply(self.sessions)
            self.keep_selection(previous)
            self.message = "Session restored to visible sessions."
            return
        self.message = f"Hide {ellipsize(display_title(item), 38)!r}? Press y to confirm."
        self.draw()
        key = self.screen.get_wch()
        if isinstance(key, str) and key.casefold() == "y":
            self.state.set_hidden(item, True)
            self.state.apply(self.sessions)
            self.keep_selection(previous)
            self.message = "Session hidden. Use v to view hidden sessions."
        else:
            self.message = "Hide cancelled."

    def help(self) -> None:
        lines = [
            ("Enter", "Focus an open session; otherwise resume it"),
            ("↑/↓ or j/k", "Move through sessions"),
            ("PgUp/PgDn", "Move by a page; Home/End jump"),
            ("MSGS", "User turns; compactions and tool/assistant chatter are excluded"),
            ("STARTED", "When the session began; UPDATED is its latest activity"),
            ("OPEN", "● running or Ⅱ paused in a live terminal"),
            ("Ctrl-F or /", "Enter search mode; command keys are then search text"),
            ("is:open", "Search syntax also supports tool:, dir:, name:, and origin:"),
            ("Tab", "Cycle All → Codex → Claude"),
            ("o", "Cycle Human → Cross → Agent → All origins"),
            ("v", "Cycle Visible → Hidden → Visible + hidden"),
            ("A", "Show every session: all origins, visible and hidden"),
            ("H", "Show every hidden session across all origins"),
            ("r", "Rename; an empty name restores the vendor/original title"),
            ("h", "Hide or unhide the selected session (never deletes it)"),
            ("d", "Choose a directory from a searchable list"),
            ("s", "Sort by recent, title, directory, messages ↓, or open first"),
            ("p", "Cycle Safe → Dangerous → Custom launch mode"),
            ("Ctrl-R", "Refresh the session index"),
            ("Esc", "Leave search mode, then clear an active search"),
            ("q", "Quit"),
        ]
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        box_width = min(78, width - 4)
        top = max(0, (height - len(lines) - 6) // 2)
        left = max(0, (width - box_width) // 2)
        self.add(top, left, "AI SESSIONS — HELP", self.style("accent", curses.A_BOLD), box_width)
        self.add(
            top + 1,
            left,
            "*- unnamed · › selected · ⊘ hidden · ● open · Ⅱ paused.",
            self.style("muted"),
            box_width,
        )
        for index, (key, description) in enumerate(lines):
            self.add(top + 3 + index, left, f"{key:<16}", self.style("accent", curses.A_BOLD), 16)
            self.add(top + 3 + index, left + 17, description, 0, box_width - 17)
        self.add(
            min(height - 1, top + len(lines) + 4),
            left,
            "Press any key to return",
            self.style("muted"),
            box_width,
        )
        self.screen.refresh()
        self.screen.get_wch()

    def confirm_missing(self, item: Session) -> Session | None:
        self.message = (
            "Saved directory is missing. Press y to resume from ~, or any other key to cancel."
        )
        self.draw()
        key = self.screen.get_wch()
        self.message = ""
        if isinstance(key, str) and key.casefold() == "y":
            return replace(item, cwd=str(HOME))
        return None

    def run(self) -> Session | None:
        while True:
            self.draw()
            key = self.screen.get_wch()
            self.message = ""
            items = self.current()
            page = max(1, self.screen.getmaxyx()[0] - 12)

            if key in (curses.KEY_UP, "\x10") or (not self.searching and key == "k"):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, "\x0e") or (not self.searching and key == "j"):
                self.selected = min(max(0, len(items) - 1), self.selected + 1)
            elif key == curses.KEY_PPAGE:
                self.selected = max(0, self.selected - page)
            elif key == curses.KEY_NPAGE:
                self.selected = min(max(0, len(items) - 1), self.selected + page)
            elif key == curses.KEY_HOME or (not self.searching and key == "g"):
                self.selected = 0
            elif key == curses.KEY_END or (not self.searching and key == "G"):
                self.selected = max(0, len(items) - 1)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if items:
                    chosen = items[self.selected]
                    if chosen.is_open:
                        focused, message = focus_open_session(chosen)
                        if focused:
                            # The desktop focus moves away from this browser, but
                            # keep it alive so returning to its terminal preserves
                            # the current query, filters, and selection.
                            self.message = message
                            detect_open_sessions(self.sessions)
                            continue
                        self.message = message
                        detect_open_sessions(self.sessions)
                        continue
                    if chosen.cwd and not Path(chosen.cwd).is_dir():
                        chosen = self.confirm_missing(chosen)
                    if chosen:
                        return chosen
            elif key == "\t":
                self.cycle_tool()
            elif key == curses.KEY_BTAB:
                self.cycle_tool(backwards=True)
            elif key == "\x1b":
                if self.searching:
                    self.searching = False
                elif self.query:
                    previous = self.selected_id()
                    self.query = ""
                    self.keep_selection(previous)
                else:
                    return None
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if self.query:
                    previous = self.selected_id()
                    self.query = self.query[:-1]
                    self.keep_selection(previous)
                self.searching = True
            elif key == "\x15":  # Ctrl-U
                self.query = ""
                self.selected = 0
            elif key == "\x17":  # Ctrl-W
                self.query = (
                    self.query.rstrip().rsplit(" ", 1)[0] if " " in self.query.rstrip() else ""
                )
                self.selected = 0
            elif not self.searching and key == "q":
                return None
            elif not self.searching and key == "?":
                self.help()
            elif not self.searching and key == "d":
                self.directory_picker()
            elif not self.searching and key == "s":
                self.cycle_sort()
            elif not self.searching and key == "p":
                self.cycle_launch_mode()
            elif not self.searching and key == "o":
                self.cycle_origin()
            elif not self.searching and key == "v":
                self.cycle_visibility()
            elif not self.searching and key == "r":
                self.rename_selected()
            elif not self.searching and key == "h":
                self.toggle_hidden()
            elif not self.searching and key == "A":
                self.tool = "all"
                self.origin = "all"
                self.visibility = "all"
                self.directory = ""
                self.query = ""
                self.keep_selection()
            elif not self.searching and key == "H":
                self.tool = "all"
                self.origin = "all"
                self.visibility = "hidden"
                self.directory = ""
                self.query = ""
                self.keep_selection()
            elif not self.searching and key == "a":
                # Kept as a quick v1-compatible All/Human toggle.
                previous = self.selected_id()
                self.origin = "all" if self.origin != "all" else "human"
                self.keep_selection(previous)
            elif key == "\x12":  # Ctrl-R
                previous = self.selected_id()
                self.message = "Refreshing…"
                self.draw()
                self.sessions = load_sessions(use_cache=self.use_cache, state=self.state)
                self.keep_selection(previous)
            elif key == "\x06" or (not self.searching and key == "/"):  # Ctrl-F or /
                self.searching = True
            elif isinstance(key, str) and key.isprintable():
                previous = self.selected_id()
                self.searching = True
                self.query += key
                self.keep_selection(previous)


def command_for(session: Session, config: LaunchConfig) -> list[str]:
    argv = config.provider_prefix(session.tool)
    if session.tool == "claude":
        argv += ["--resume", session.resume_target]
    else:
        argv += ["resume"]
        if session.source == "non-interactive":
            argv.append("--include-non-interactive")
        argv.append(session.resume_target)
    return argv


def launch(session: Session, config: LaunchConfig, dry_run: bool = False) -> int:
    argv = command_for(session, config)
    cwd = session.cwd or str(HOME)
    if dry_run:
        if IS_WINDOWS:
            rendered = subprocess.list2cmdline(argv)
            print(f"Set-Location -LiteralPath {subprocess.list2cmdline([cwd])}; {rendered}")
        else:
            print(f"cd -- {shlex.quote(cwd)} && " + shlex.join(argv))
        return 0
    try:
        os.chdir(cwd)
        if IS_WINDOWS:
            return subprocess.call(argv)
        os.execvp(argv[0], argv)
    except OSError as error:
        print(f"sessions: could not launch {argv[0]!r}: {error}", file=sys.stderr)
        return 127
    return 127


def list_output(items: list[Session], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([asdict(item) for item in items], indent=2, ensure_ascii=False))
        return
    if not items:
        print("No sessions found.")
        return
    try:
        terminal_width = os.get_terminal_size().columns if sys.stdout.isatty() else 120
    except OSError:
        terminal_width = 120
    title_width = max(24, terminal_width - 94)
    print(
        f"{'TOOL':<8} {'ORIGIN':<7} {'OPEN':<5} {'MSGS':>5} {'STATE':<8} "
        f"{'STARTED':<12} {'UPDATED':<12} {'TITLE':<{title_width}} DIRECTORY"
    )
    for item in items:
        paused = item.is_open and process_state(item.open_pid) in ("T", "t")
        open_symbol = "Ⅱ" if paused else ("●" if item.is_open else "")
        print(
            f"{TOOL_LABELS[item.tool]:<8} {ORIGIN_LABELS[item.origin]:<7} "
            f"{open_symbol:<5} {item.message_count:>5} "
            f"{('hidden' if item.hidden else 'visible'):<8} "
            f"{relative_time(item.created):<12} {relative_time(item.updated):<12} "
            f"{ellipsize(display_list_title(item), title_width):<{title_width}} {short_path(item.cwd)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sessions",
        description="Search, browse, and resume local Claude Code and Codex CLI sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Search examples:
              sessions --list --tool codex
              sessions --list --origin cross
              sessions --list --origin agent
              sessions --list --query is:open --sort open
              sessions --list --visibility hidden
              sessions --list --directory options-app --query "pricing"
              sessions --query "tool:claude origin:agent dir:ascolais"

            Interactive keys: o filters by origin, v filters hidden sessions,
            Ctrl-F starts search, r renames, h hides/unhides, and ? shows all shortcuts.
            """
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="print matching sessions instead of opening the browser"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON (implies --list)")
    parser.add_argument("--tool", choices=TOOL_ORDER, default="all", help="initial tool filter")
    parser.add_argument(
        "--origin", choices=ORIGIN_ORDER, default="human", help="filter by who launched the session"
    )
    parser.add_argument(
        "--visibility",
        choices=VISIBILITY_ORDER,
        default="visible",
        help="filter visible or hidden sessions",
    )
    parser.add_argument(
        "--directory", default="", help="filter to directories containing this text"
    )
    parser.add_argument("--query", "-q", default="", help="initial search query")
    parser.add_argument("--sort", choices=tuple(SORT_LABELS), default="recent", help="sort order")
    parser.add_argument("--include-auxiliary", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-cache", action="store_true", help="rebuild Claude metadata without using the cache"
    )
    parser.add_argument(
        "--resume", metavar="ID_OR_NAME", help="resume an exact session ID or named session"
    )
    parser.add_argument(
        "--rename",
        nargs=2,
        metavar=("ID_OR_NAME", "NEW_NAME"),
        help="set a utility-local session name; use an empty name to reset",
    )
    parser.add_argument("--hide", metavar="ID_OR_NAME", help="hide a session from the normal view")
    parser.add_argument("--unhide", metavar="ID_OR_NAME", help="restore a hidden session")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resume command instead of running it"
    )
    parser.add_argument(
        "--launch-mode", choices=LAUNCH_MODES, help="override the configured launch mode"
    )
    parser.add_argument(
        "--set-launch-mode", choices=LAUNCH_MODES, help="save the default launch mode and exit"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    if IS_WINDOWS:
        # Windows PowerShell and SSH sessions may still expose a legacy code
        # page even though Windows Terminal itself is Unicode-capable.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    args = build_parser().parse_args(argv)
    state = UserState()
    launch_config = LaunchConfig.load()
    if args.set_launch_mode:
        launch_config.set_mode(args.set_launch_mode)
        print(f"Launch mode saved: {launch_config.mode}")
        return 0
    if args.launch_mode:
        launch_config.mode = args.launch_mode
    sessions = load_sessions(use_cache=not args.no_cache, state=state)

    def resolve(target: str) -> Session | None:
        exact = [
            item
            for item in sessions
            if item.session_id == target
            or (item.named and item.title.casefold() == target.casefold())
        ]
        if not exact:
            print(f"sessions: no session with ID or name {target!r}", file=sys.stderr)
            return None
        if len(exact) > 1:
            print(
                f"sessions: {target!r} matches more than one record; use its displayed ID",
                file=sys.stderr,
            )
            return None
        return exact[0]

    for operation, target in (("hide", args.hide), ("unhide", args.unhide)):
        if target:
            item = resolve(target)
            if item is None:
                return 2
            state.set_hidden(item, operation == "hide")
            state.apply(sessions)
            print(f"{display_title(item)}: {'hidden' if operation == 'hide' else 'visible'}")
    if args.rename:
        target, new_name = args.rename
        item = resolve(target)
        if item is None:
            return 2
        state.set_name(item, new_name)
        state.apply(sessions)
        print(f"Session name: {display_title(item)}")
    if args.hide or args.unhide or args.rename:
        return 0

    directory = ""
    if args.directory:
        needle = str(Path(args.directory).expanduser()).casefold()
        candidates = {
            item.cwd
            for item in sessions
            if needle in item.cwd.casefold() or needle in short_path(item.cwd).casefold()
        }
        if len(candidates) == 1:
            directory = next(iter(candidates))
        else:
            # Multiple partial directory matches should remain visible, so fold
            # the directory term into the general search rather than choosing.
            args.query = (args.query + " dir:" + args.directory.casefold()).strip()

    selected_origin = "all" if args.include_auxiliary else args.origin
    items = filtered_sessions(
        sessions,
        tool=args.tool,
        directory=directory,
        query=args.query,
        origin=selected_origin,
        visibility=args.visibility,
        sort_mode=args.sort,
    )

    if args.resume:
        item = resolve(args.resume)
        if item is None:
            return 2
        return launch(item, launch_config, dry_run=args.dry_run)

    if args.list or args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            list_output(items, as_json=args.json)
        except BrokenPipeError:
            return 0
        return 0

    def wrapped(screen: Any) -> Session | None:
        browser = Browser(
            screen,
            sessions,
            state=state,
            launch_config=launch_config,
            use_cache=not args.no_cache,
        )
        browser.tool = args.tool
        browser.origin = selected_origin
        browser.visibility = args.visibility
        browser.directory = directory
        browser.query = args.query
        browser.sort_mode = args.sort
        return browser.run()

    selected = curses.wrapper(wrapped)
    if selected:
        return launch(selected, launch_config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
