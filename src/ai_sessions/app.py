#!/usr/bin/env python3
"""Browse and resume local Claude Code and Codex CLI sessions.

Core browsing uses only Python's standard library. Desktop focusing optionally
uses local tmux/wmctrl/xdotool commands. Browsing never modifies provider records;
SQLite may create derived WAL sidecars—an empty write-ahead log and initialized
shared index—while opening an existing OpenCode store read-only. Renaming is the
one content exception: it publishes a title when that harness supports it.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import functools
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterable

from . import __version__ as VERSION
from .capabilities import HarnessAdapter, Unsupported
from .config import LAUNCH_MODES, LaunchConfig
from .conversion import (
    BridgeError,
    bridge,
    bridge_tools,
    conversation_change_status,
    native_checkpoint,
    native_session_availability,
    native_session_exists,
    prepare_target,
    resolve_budget,
)
from .conversion import append_jsonl as append_jsonl
from .diagnostics import clear_warnings, record_warning
from .diagnostics import warnings as load_warnings
from .discovery import MAX_EXISTENCE_PROBES_PER_PASS, HarnessContext
from .discovery import clean_prompt as clean_prompt
from .discovery import normalize_space as normalize_space
from .discovery import prompt_text as prompt_text
from .discovery import timestamp as timestamp
from .liveness import immutable_context as immutable_liveness_context
from .liveness import populate_context as populate_liveness_context
from .liveness import process_start_token
from .model import Checkpoint, LivenessSession, NativeRef, NativeSession, Session, SourceKind
from .paths import (
    APP_CACHE_DIR,
    CONFIG_FILE,
    HOME,
    IS_WINDOWS,
    STATE_FILE,
)
from .registry import REGISTRY

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
_UNSET_CHECKPOINT = object()
CATALOG_CACHE_VERSION = 1
CATALOG_CACHE_FILE = APP_CACHE_DIR / f"session-catalog-v{CATALOG_CACHE_VERSION}.json"


def tool_label(name: str, *, short: bool = True) -> str:
    """Return a current registry label, preserving unknown forward-compatible names."""
    try:
        return REGISTRY.label(name, short=short)
    except KeyError:
        return name


def tool_order() -> tuple[str, ...]:
    return ("all", *REGISTRY.names())


def tool_filter_label(tools: Iterable[str]) -> str:
    selected = set(tools)
    ordered = [adapter for adapter in REGISTRY.adapters() if adapter.name in selected]
    if len(ordered) == len(REGISTRY.adapters()):
        return "All tools"
    if not ordered:
        return "No tools"
    return " + ".join(adapter.short_label for adapter in ordered)


def tool_column_width(minimum: int) -> int:
    return max((minimum, *(len(adapter.short_label) for adapter in REGISTRY.adapters())))


def available_launch_tools(item: Session) -> tuple[str, ...]:
    """Current native and bridgeable launch choices, with the native tool first."""
    ordered = [item.tool, *(tool for tool in item.launch_targets if tool != item.tool)]
    if can_bridge(item):
        ordered += [tool for tool in bridge_tools() if tool not in ordered]
    return tuple(ordered)


def can_bridge(item: Session) -> bool:
    if not item.storage:
        return False
    try:
        reader = REGISTRY.get(item.tool).read
    except KeyError:
        return False
    return not isinstance(reader, Unsupported)


def active_launch_tool(item: Session) -> str:
    options = available_launch_tools(item)
    return item.launch_tool if item.launch_tool in options else item.tool


def session_needs_bridge(item: Session, tool: str | None = None) -> bool:
    resolved = tool or active_launch_tool(item)
    return resolved != item.tool and resolved not in item.launch_targets


def cycle_session_launch_tool(item: Session, backwards: bool = False) -> None:
    options = available_launch_tools(item)
    if len(options) < 2:
        return
    selected = active_launch_tool(item)
    index = options.index(selected) if selected in options else 0
    item.launch_tool = options[(index + (-1 if backwards else 1)) % len(options)]


def supports_launch_tool(item: Session, value: str) -> bool:
    return value in available_launch_tools(item)


def session_searchable(item: Session) -> str:
    return " ".join(
        (
            item.tool,
            tool_label(item.tool),
            item.session_id,
            active_launch_tool(item),
            *available_launch_tools(item),
            item.title,
            item.cwd,
            item.preview,
            item.source,
            item.origin,
            item.parent_id,
            "hidden" if item.hidden else "visible",
            "open running active" if item.is_open else "closed inactive",
            item.original_title,
            str(item.message_count),
            str(item.open_pid) if item.open_pid else "",
        )
    ).casefold()


class UserState:
    """Utility-local names and visibility.

    Visibility never leaves this file.  Names are mirrored here so a rename
    survives even when the provider store cannot be written, but the rename
    itself is published to the provider by ``publish_name``.
    """

    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.names: dict[str, str] = {}
        self.original_names: dict[str, dict[str, Any]] = {}
        self.hidden: set[str] = set()
        self.launch_tools: dict[str, str] = {}
        self.bridges: dict[str, dict[str, dict[str, Any]]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.session_conversations: dict[str, str] = {}
        self._sessions_by_key: dict[str, Session] = {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") in (1, 2, 3, 4, 5, 6):
                names = payload.get("names", {})
                original_names = payload.get("original_names", {})
                hidden = payload.get("hidden", [])
                launch_tools = payload.get("launch_tools", {})
                bridges = payload.get("bridges", {})
                conversations = payload.get("conversations", {})
                session_conversations = payload.get("session_conversations", {})
                if isinstance(bridges, dict):
                    for key, value in bridges.items():
                        if not isinstance(value, dict):
                            continue
                        entries = {
                            tool: entry
                            for tool, entry in value.items()
                            if isinstance(tool, str)
                            and tool
                            and isinstance(entry, dict)
                            and isinstance(entry.get("session_id"), str)
                        }
                        if entries:
                            self.bridges[self.stable_key(str(key))] = entries
                if isinstance(conversations, dict):
                    for conversation_id, value in conversations.items():
                        if not isinstance(value, dict) or not isinstance(
                            value.get("members"), dict
                        ):
                            continue
                        members = {
                            self.stable_key(str(key)): member
                            for key, member in value["members"].items()
                            if isinstance(member, dict)
                            and isinstance(member.get("tool"), str)
                            and member.get("tool")
                            and isinstance(member.get("session_id"), str)
                        }
                        if members:
                            self.conversations[str(conversation_id)] = {"members": members}
                if isinstance(session_conversations, dict):
                    self.session_conversations = {
                        self.stable_key(str(key)): str(value)
                        for key, value in session_conversations.items()
                        if str(value) in self.conversations
                    }
                for conversation_id, conversation in self.conversations.items():
                    for key in conversation["members"]:
                        self.session_conversations.setdefault(key, conversation_id)
                if isinstance(names, dict):
                    self.names = {
                        self.stable_key(str(key)): normalize_space(value)
                        for key, value in names.items()
                        if normalize_space(value)
                    }
                if isinstance(original_names, dict):
                    for key, value in original_names.items():
                        if not isinstance(value, dict) or not isinstance(value.get("named"), bool):
                            continue
                        self.original_names[self.stable_key(str(key))] = {
                            "title": normalize_space(value.get("title")),
                            "named": value["named"],
                        }
                if isinstance(hidden, list):
                    self.hidden = {self.stable_key(str(key)) for key in hidden}
                if isinstance(launch_tools, dict):
                    for key, value in launch_tools.items():
                        if not isinstance(value, str):
                            continue
                        value = value.strip().lower()
                        if value:
                            self.launch_tools[self.stable_key(str(key))] = value
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    @staticmethod
    def stable_key(value: str) -> str:
        """Migrate v1 tool:origin:id keys to origin-independent v2 keys."""
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[1] in ("human", "agent"):
            return f"{parts[0]}:{parts[2]}"
        return value

    @staticmethod
    def _checkpoint(item: Session) -> Checkpoint | None:
        if not item.session_id or not item.storage:
            return None
        return native_checkpoint(item.tool, NativeRef(item.session_id, item.storage))

    @staticmethod
    def _member_key(tool: str, session_id: str) -> str:
        return f"{tool}:{session_id}"

    def _member_from_session(
        self,
        item: Session,
        *,
        generation: int,
        frontier: str,
        checkpoint: Checkpoint | None | object = _UNSET_CHECKPOINT,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tool": item.tool,
            "session_id": item.session_id,
            "storage": item.storage,
            "cwd": item.cwd,
            "title": item.title,
            "updated": item.updated,
            "generation": generation,
            "frontier": frontier,
        }
        resolved = self._checkpoint(item) if checkpoint is _UNSET_CHECKPOINT else checkpoint
        if resolved is not None:
            result["checkpoint"] = resolved
            if isinstance(resolved, int) and not isinstance(resolved, bool):
                result["cursor"] = resolved
        return result

    def _ensure_conversation(self, item: Session) -> tuple[str, dict[str, Any]]:
        key = self.stable_key(item.key)
        conversation_id = self.session_conversations.get(key, "")
        if conversation_id and conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            member = conversation["members"].get(key)
            if member is not None:
                member.update(
                    storage=item.storage,
                    cwd=item.cwd,
                    title=item.title,
                    updated=item.updated,
                )
                return conversation_id, member
        conversation_id = str(uuid.uuid4())
        frontier = str(uuid.uuid4())
        member = self._member_from_session(item, generation=0, frontier=frontier)
        self.conversations[conversation_id] = {"members": {key: member}}
        self.session_conversations[key] = conversation_id
        return conversation_id, member

    def conversation_id_for(self, item: Session, *, create: bool = False) -> str:
        conversation_id = self.session_conversations.get(self.stable_key(item.key), "")
        if conversation_id or not create:
            return conversation_id
        return self._ensure_conversation(item)[0]

    def _member_changed(self, member: dict[str, Any]) -> bool:
        return self._member_change_status(member) != "unchanged"

    def _member_change_status(self, member: dict[str, Any], availability: str | None = None) -> str:
        tool = str(member.get("tool", ""))
        session_id = str(member.get("session_id", ""))
        storage = str(member.get("storage", ""))
        if not tool or not session_id or not storage:
            return "unknown"
        checkpoint = member.get("checkpoint", member.get("cursor"))
        if isinstance(checkpoint, bool) or not isinstance(checkpoint, (int, str)):
            return "unstable"
        return conversation_change_status(
            tool,
            NativeRef(session_id, storage),
            checkpoint,
            availability=availability,
        )

    @staticmethod
    def _member_availability(member: dict[str, Any]) -> str:
        tool = str(member.get("tool", ""))
        session_id = str(member.get("session_id", ""))
        storage = str(member.get("storage", ""))
        if not tool or not session_id or not storage:
            return "unknown"
        return native_session_availability(tool, NativeRef(session_id, storage))

    def _conversation_status(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.conversations[conversation_id]
        all_members = list(conversation["members"].items())
        availability = {key: self._member_availability(member) for key, member in all_members}
        members = [pair for pair in all_members if availability[pair[0]] == "available"]
        maximum = max((int(member.get("generation", 0)) for _, member in all_members), default=0)
        changes = {
            key: self._member_change_status(member, availability[key]) for key, member in members
        }
        known_members = [
            pair for pair in members if changes[pair[0]] not in ("unknown", "unsupported")
        ]
        current = [
            (key, member)
            for key, member in known_members
            if int(member.get("generation", 0)) == maximum
        ]
        current_frontiers = {str(member.get("frontier", "")) for _, member in current}
        unavailable = [
            (key, member)
            for key, member in all_members
            if int(member.get("generation", 0)) == maximum
            and availability[key] == "unavailable"
            and str(member.get("frontier", "")) not in current_frontiers
        ]
        if unavailable:
            return {
                "conflict": False,
                "heads": current,
                "advanced": [],
                "unavailable": unavailable,
                "unstable": [],
                "unknown": [],
            }
        unknown = [
            pair
            for pair in all_members
            if availability[pair[0]] == "unknown"
            and int(pair[1].get("generation", 0)) == maximum
            and str(pair[1].get("frontier", "")) not in current_frontiers
        ]
        unknown.extend(
            pair
            for pair in members
            if changes[pair[0]] in ("unknown", "unsupported")
            and int(pair[1].get("generation", 0)) == maximum
            and str(pair[1].get("frontier", "")) not in current_frontiers
            and pair not in unknown
        )
        if unknown:
            return {
                "conflict": False,
                "heads": current,
                "advanced": [],
                "unavailable": [],
                "unstable": [],
                "unknown": unknown,
            }
        unstable = [pair for pair in known_members if changes[pair[0]] == "unstable"]
        if unstable:
            return {
                "conflict": False,
                "heads": current,
                "advanced": [],
                "unavailable": [],
                "unstable": unstable,
                "unknown": [],
            }
        advanced = [(key, member) for key, member in known_members if changes[key] == "changed"]
        if not advanced:
            return {
                "conflict": False,
                "heads": current,
                "advanced": [],
                "unavailable": [],
                "unstable": [],
                "unknown": [],
            }
        if len(advanced) == 1 and int(advanced[0][1].get("generation", 0)) == maximum:
            return {
                "conflict": False,
                "heads": advanced,
                "advanced": advanced,
                "unavailable": [],
                "unstable": [],
                "unknown": [],
            }
        # Two members changed independently, or an older materialization moved
        # after a newer frontier existed. Either is a fork, never a timestamp race.
        heads = list(advanced)
        advanced_keys = {key for key, _ in advanced}
        heads.extend((key, member) for key, member in current if key not in advanced_keys)
        return {
            "conflict": True,
            "heads": heads,
            "advanced": advanced,
            "unavailable": [],
            "unstable": [],
            "unknown": [],
        }

    def _session_for_member(self, member: dict[str, Any], template: Session) -> Session:
        key = self._member_key(str(member["tool"]), str(member["session_id"]))
        actual = self._sessions_by_key.get(key)
        if actual is not None:
            return actual
        return replace(
            template,
            tool=str(member["tool"]),
            session_id=str(member["session_id"]),
            storage=str(member.get("storage", "")),
            cwd=str(member.get("cwd", template.cwd)),
            title=str(member.get("title", template.title)),
            updated=float(member.get("updated", template.updated)),
            source="interactive",
            auxiliary=False,
            origin="cross",
            resume_id=str(member["session_id"]),
            parent_id="",
            launch_targets={str(member["tool"]): str(member["session_id"])},
            launch_tool="",
        )

    def resolve_launch(
        self, item: Session, target_tool: str
    ) -> tuple[Session, Session | None, str]:
        """Resolve a row to its conversation head and an equivalent target copy."""
        conversation_id = self.conversation_id_for(item)
        if not conversation_id:
            return item, item if item.tool == target_tool else None, ""
        status = self._conversation_status(conversation_id)
        if status["unknown"]:
            labels = ", ".join(key for key, _ in status["unknown"])
            raise BridgeError(
                f"conversation {conversation_id[:8]} has newest materialization(s) owned by "
                f"an unavailable harness ({labels}); refusing to resume an older generation"
            )
        if status["unstable"]:
            labels = ", ".join(key for key, _ in status["unstable"])
            raise BridgeError(
                f"conversation {conversation_id[:8]} has incomplete or unstable transcript "
                f"data ({labels}); wait for the current write to finish and retry"
            )
        if status["unavailable"]:
            labels = ", ".join(key for key, _ in status["unavailable"])
            raise BridgeError(
                f"conversation {conversation_id[:8]} has unavailable newest materialization(s) "
                f"({labels}); refusing to resume an older generation"
            )
        if status["conflict"]:
            labels = ", ".join(key for key, _ in status["heads"])
            raise BridgeError(
                f"conversation {conversation_id[:8]} has divergent heads ({labels}); "
                "choose a branch explicitly before resuming"
            )
        heads: list[tuple[str, dict[str, Any]]] = status["heads"]
        if not heads:
            return item, item if item.tool == target_tool else None, conversation_id
        # Equivalent materializations share a frontier. Prefer one already in
        # the requested harness, then the selected row, then a stable key order.
        requested = next((pair for pair in heads if pair[1].get("tool") == target_tool), None)
        selected = next((pair for pair in heads if pair[0] == item.key), None)
        _, source_member = requested or selected or sorted(heads, key=lambda pair: pair[0])[0]
        source = self._session_for_member(source_member, item)
        frontier = source_member.get("frontier")
        generation = int(source_member.get("generation", 0))
        target_member: dict[str, Any] | None = None
        if not status["advanced"]:
            for _, member in self.conversations[conversation_id]["members"].items():
                if (
                    member.get("tool") == target_tool
                    and member.get("frontier") == frontier
                    and int(member.get("generation", 0)) == generation
                    and not self._member_changed(member)
                ):
                    target_member = member
                    break
        if source_member.get("tool") == target_tool:
            target_member = source_member
        target = (
            self._session_for_member(target_member, item) if target_member is not None else None
        )
        return source, target, conversation_id

    def _apply_conversation_status(self, sessions: list[Session]) -> None:
        for item in sessions:
            item.conversation_id = ""
            item.superseded = False
            item.diverged = False
            item.conversation_blocker = ""
        by_key = {item.key: item for item in sessions}
        for conversation_id, conversation in self.conversations.items():
            status = self._conversation_status(conversation_id)
            blocker = next(
                (name for name in ("unknown", "unstable", "unavailable") if status[name]), ""
            )
            head_keys = {key for key, _ in status["heads"]}
            for key in conversation["members"]:
                item = by_key.get(key)
                if item is None:
                    continue
                item.conversation_id = conversation_id
                item.conversation_blocker = blocker
                item.diverged = bool(status["conflict"] and key in head_keys)
                item.superseded = key not in head_keys

    def _migrate_legacy_bridges(self, sessions: list[Session]) -> None:
        by_key = {item.key: item for item in sessions}
        for source_key, targets in self.bridges.items():
            if source_key in self.session_conversations:
                continue
            source = by_key.get(source_key)
            if source is None:
                continue
            conversation_id, source_member = self._ensure_conversation(source)
            source_updated = max(
                (
                    float(entry.get("source_updated", 0))
                    for entry in targets.values()
                    if isinstance(entry.get("source_updated"), (int, float))
                ),
                default=0.0,
            )
            if source_updated and source.updated > source_updated + 1:
                # The old schema did not store a byte frontier. Zero is a safe
                # migration: it reports the source as advanced rather than stale.
                source_member["cursor"] = 0
                source_member.pop("checkpoint", None)
            for tool, entry in targets.items():
                session_id = str(entry.get("session_id", ""))
                if not session_id:
                    continue
                key = self._member_key(tool, session_id)
                member = {
                    "tool": tool,
                    "session_id": session_id,
                    "storage": str(entry.get("storage", "")),
                    "cwd": source.cwd,
                    "title": source.title,
                    "updated": 0.0,
                    "generation": 0,
                    "frontier": source_member["frontier"],
                    # v5 cannot distinguish imported content from later work.
                    # Treat the copy as advanced so migration never resumes an
                    # older ancestor and silently loses possible work.
                    "cursor": 0,
                }
                self.conversations[conversation_id]["members"][key] = member
                self.session_conversations[key] = conversation_id

    def apply(self, sessions: Iterable[Session]) -> None:
        items = list(sessions)
        self._sessions_by_key = {item.key: item for item in items}
        self._migrate_legacy_bridges(items)
        for item in items:
            remembered = self.original_names.get(item.key)
            if remembered is not None:
                item.original_title = str(remembered["title"])
                item.original_named = bool(remembered["named"])
            elif not item.original_title:
                item.original_title = item.title
                item.original_named = item.named
            item.title = item.original_title
            item.named = item.original_named
            item.renamed = False
            item.launch_tool = self.launch_tools.get(item.key, "")
            if item.launch_tool and item.launch_tool not in available_launch_tools(item):
                item.launch_tool = ""
            custom = self.names.get(item.key, "")
            if custom:
                item.title = custom
                item.named = True
                item.renamed = True
            item.hidden = item.key in self.hidden
        self._apply_conversation_status(items)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".tmp-{os.getpid()}")
        temp.write_text(
            json.dumps(
                {
                    "version": 6,
                    "names": self.names,
                    "original_names": self.original_names,
                    "hidden": sorted(self.hidden),
                    "launch_tools": self.launch_tools,
                    "bridges": self.bridges,
                    "conversations": self.conversations,
                    "session_conversations": self.session_conversations,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temp.chmod(0o600)
        temp.replace(self.path)

    def remember_original_name(self, item: Session) -> None:
        """Keep the pre-rename provider title stable across provider reloads."""
        key = self.stable_key(item.key)
        if key not in self.original_names:
            self.original_names[key] = {
                "title": item.original_title or item.title,
                "named": item.original_named,
            }

    def original_name(self, item: Session) -> tuple[str, bool]:
        remembered = self.original_names.get(self.stable_key(item.key))
        if remembered is None:
            return item.original_title, item.original_named
        return str(remembered["title"]), bool(remembered["named"])

    def set_name(self, item: Session, name: str) -> None:
        value = normalize_space(name)[:200]
        key = self.stable_key(item.key)
        if value:
            self.names[key] = value
        else:
            self.names.pop(key, None)
        self.save()

    def set_hidden(self, item: Session, hidden: bool) -> None:
        key = self.stable_key(item.key)
        if hidden:
            self.hidden.add(key)
        else:
            self.hidden.discard(key)
        self.save()

    def set_launch_tool(self, item: Session, launch_tool: str) -> None:
        key = self.stable_key(item.key)
        if launch_tool and launch_tool in available_launch_tools(item) and launch_tool != item.tool:
            self.launch_tools[key] = launch_tool
        else:
            self.launch_tools.pop(key, None)
        self.save()

    def bridge_for(self, item: Session, tool: str) -> str:
        """The id of an existing bridged copy that is still worth resuming.

        A copy is a snapshot.  Once the source has moved on, resuming the old
        copy would silently continue from a stale conversation, so the record
        is discarded and the caller makes a fresh one.
        """
        entry = self.bridges.get(self.stable_key(item.key), {}).get(tool)
        if not entry:
            return ""
        storage = str(entry.get("storage", ""))
        session_id = str(entry.get("session_id", ""))
        if not session_id or not storage:
            return ""
        availability = native_session_availability(tool, NativeRef(session_id, storage))
        if availability == "unknown":
            raise BridgeError(
                f"the recorded {tool_label(tool)} copy {session_id} could not be verified; "
                "restore storage access or retry; refusing to create a duplicate"
            )
        if availability != "available":
            return ""
        recorded = entry.get("source_updated")
        if not isinstance(recorded, (int, float)) or item.updated > float(recorded) + 1:
            return ""
        return str(entry["session_id"])

    def set_bridge(
        self,
        item: Session,
        tool: str,
        session_id: str,
        storage: str,
        *,
        source_checkpoint: Checkpoint,
        target_checkpoint: Checkpoint | None,
    ) -> None:
        key = self.stable_key(item.key)
        self.bridges.setdefault(key, {})[tool] = {
            "session_id": session_id,
            "storage": storage,
            "source_updated": item.updated,
        }
        conversation_id, source_member = self._ensure_conversation(item)
        conversation = self.conversations[conversation_id]
        if self._member_changed(source_member):
            generation = (
                max(int(member.get("generation", 0)) for member in conversation["members"].values())
                + 1
            )
            frontier = str(uuid.uuid4())
        else:
            generation = int(source_member.get("generation", 0))
            frontier = str(source_member.get("frontier", "")) or str(uuid.uuid4())
        source_member.update(
            self._member_from_session(
                item,
                generation=generation,
                frontier=frontier,
                checkpoint=source_checkpoint,
            )
        )
        target_key = self._member_key(tool, session_id)
        target = replace(
            item,
            tool=tool,
            session_id=session_id,
            storage=storage,
            resume_id=session_id,
            source="interactive",
            auxiliary=False,
            origin="cross",
            parent_id="",
            launch_targets={tool: session_id},
            launch_tool="",
        )
        conversation["members"][target_key] = self._member_from_session(
            target,
            generation=generation,
            frontier=frontier,
            checkpoint=target_checkpoint,
        )
        self.session_conversations[target_key] = conversation_id
        self._sessions_by_key[item.key] = item
        self._sessions_by_key[target_key] = target
        self.save()


def publish_name(
    item: Session,
    name: str,
    *,
    original_title: str | None = None,
    original_named: bool | None = None,
) -> str:
    """Record a rename in the provider's own store; return a note or "".

    Claude honours the last ``custom-title`` entry in a transcript and Codex
    the last ``thread_name`` for a thread id, so a rename can be published by
    appending a single line to append-only data the provider already owns.
    """
    restored_generated = False
    if not name:
        original_title = item.original_title if original_title is None else original_title
        original_named = item.original_named if original_named is None else original_named
        # A published title can be superseded but never withdrawn, so a reset
        # republishes whatever the session showed before the rename.  Where the
        # provider only ever generated that title, republishing records it as an
        # explicit one -- the lesser evil, because the alternative leaves the
        # provider showing a name this utility has already dropped.
        if not original_title:
            return "provider title left unchanged; there is no earlier title to restore"
        name = original_title
        restored_generated = not original_named
    try:
        try:
            adapter = REGISTRY.get(item.tool)
        except KeyError:
            return f"unknown harness {item.tool!r}; name kept local only"
        publisher = adapter.publish_name
        if isinstance(publisher, Unsupported):
            return (
                f"{adapter.label} cannot publish titles: {publisher.reason}; name kept local only"
            )
        note = publisher(item, name)
    except (BridgeError, OSError) as error:
        return f"could not update the provider title: {error}"
    if note:
        return note
    if restored_generated:
        label = tool_label(item.tool)
        return f"restored {name!r}; {label} now records it as an explicit title"
    return note


def rename_session(state: UserState, item: Session, name: str) -> str:
    """Rename locally and in provider storage while preserving reset history."""
    value = normalize_space(name)[:200]
    state.remember_original_name(item)
    original_title, original_named = state.original_name(item)
    note = publish_name(
        item,
        value,
        original_title=original_title,
        original_named=original_named,
    )
    state.set_name(item, value)
    return note


def strip_extended_prefix(value: str) -> str:
    r"""Drop Win32's extended-length prefix from a recorded path.

    Codex stores most Windows thread directories as ``\\?\C:\...``.  The prefix
    is valid for Win32 file APIs but not for a process working directory:
    cmd.exe refuses it and silently falls back to the Windows directory.  Strip
    it before the path reaches a human, ``os.chdir``, or a rendered command.
    """
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def short_path(value: str) -> str:
    if not value:
        return "(unknown directory)"
    value = strip_extended_prefix(value)
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
        title = f"Untitled {tool_label(session.tool)} session"
    return title


def agent_tag(session: Session) -> str:
    """Label agent threads that borrowed their opening message.

    Codex copies the parent's first user message into every subagent it
    spawns, so a fan-out of five renders as five identical lines.  This legacy
    helper retains the parent fragment for callers that explicitly request the
    diagnostic tag; ordinary rows use ``_normal_agent_tag`` below.
    """
    if session.source == "subagent":
        nickname = session.agent_nickname or "sub"
        parent = session.parent_id[:8]
        return f"[{nickname}<{parent}] " if parent else f"[{nickname}] "
    if session.source == "non-interactive":
        return "[exec] "
    return ""


def _normal_agent_tag(session: Session) -> str:
    """Return the human-facing agent tag without exposing a parent ID."""
    if session.source == "subagent":
        return f"[{session.agent_nickname or 'sub'}] "
    return agent_tag(session)


def display_list_title(session: Session) -> str:
    """Make generated/unnamed titles explicit without a separate flag column."""
    title = display_title(session)
    return _normal_agent_tag(session) + (title if session.named else f"*- {title}")


def title_disambiguators(sessions: Iterable[Session]) -> dict[str, str]:
    """Return collision-only human labels without exposing native IDs."""
    groups: dict[str, list[Session]] = {}
    for item in sessions:
        groups.setdefault(display_title(item).casefold(), []).append(item)

    labels: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: (item.created, item.updated, item.tool, item.key))
        used: dict[str, int] = {}
        for item in ordered:
            stamp = exact_time(item.created)
            ordinal = used.get(stamp, 0) + 1
            used[stamp] = ordinal
            labels[item.key] = f"[{stamp}{f' · {ordinal}' if ordinal > 1 else ''}]"
    return labels


def conversation_status(session: Session) -> str:
    """A stable, non-title label for the session's conversation position."""
    if session.conversation_blocker:
        return session.conversation_blocker
    if session.diverged:
        return "diverged branch"
    if session.superseded:
        return "superseded copy"
    if session.conversation_id:
        return "lineage head"
    return "untracked"


@functools.lru_cache(maxsize=32_768)
def project_identity(value: str) -> str:
    """Return the presentation identity for a project directory.

    This deliberately normalizes a path, rather than reducing it to its basename:
    two repositories called ``project`` are still two different projects.  The only
    filesystem check is a bounded ``.git`` ancestor probe; it never mutates state
    and falls back to lexical normalization on any error.
    """
    if not normalize_space(value):
        return "(unknown project)"
    value = strip_extended_prefix(os.path.expanduser(value))
    lexical = os.path.normcase(os.path.abspath(os.path.normpath(value)))
    # A repository root is a better project identity than the current subdir,
    # but probing is deliberately limited to ancestors and failure-safe.
    try:
        candidate = Path(lexical).resolve(strict=False)
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return os.path.normcase(str(parent))
        return os.path.normcase(str(candidate))
    except (OSError, RuntimeError, ValueError):
        return lexical


def _projects_related(left: str, right: str) -> bool:
    """Whether two known project identities are equal or ancestor/descendant."""
    if not left or not right or left == "(unknown project)" or right == "(unknown project)":
        return False
    try:
        common = os.path.commonpath((left, right))
    except (OSError, ValueError):
        return False
    return common == left or common == right


def _common_project_path(projects: Iterable[str]) -> str:
    """Return the deterministic common project root for a presentation group."""
    known = sorted({project for project in projects if project and project != "(unknown project)"})
    if not known:
        return "(unknown project)"
    try:
        return os.path.commonpath(known)
    except (OSError, ValueError):
        return known[0]


def _independent_group_key(item: Session) -> tuple[str, str]:
    """Make unknown-cwd independent sessions permanently presentation-distinct."""
    project = project_identity(item.cwd)
    if project == "(unknown project)":
        project = f"{project}:{item.key}"
    return project, normalize_space(display_title(item)).casefold()


def activity_counts(session: Session) -> tuple[int, int, int]:
    """Read the neutral activity metrics, retaining compatibility with old models."""
    values: list[int] = []
    for name, fallback in (
        ("turn_count", getattr(session, "message_count", 0)),
        ("compaction_count", 0),
        ("prompt_count", getattr(session, "message_count", 0)),
    ):
        try:
            values.append(max(0, int(getattr(session, name, fallback))))
        except (TypeError, ValueError):
            values.append(0)
    return values[0], values[1], values[2]


def activity_label(session: Session) -> str:
    """Compact activity display: transcript turns, compactions, semantic prompts."""
    turns, compactions, prompts = activity_counts(session)
    return f"{turns}t {compactions}c {prompts}p"


@dataclass(frozen=True, slots=True)
class ViewRow:
    """An immutable presentation row backed by one or more exact native sessions."""

    row_id: str
    members: tuple[Session, ...]
    all_members: tuple[Session, ...]
    representative: Session
    target: Session | None
    status: str
    title: str
    project: str
    expandable: bool = False
    expanded: bool = False
    actionable: bool = True
    collision_label: str = ""

    @property
    def is_group(self) -> bool:
        return len(self.members) > 1 or self.expandable

    @property
    def session(self) -> Session:
        """The exact native session used for an actionable collapsed row."""
        if self.target is None:
            raise ValueError("synthetic view rows have no native target")
        return self.target


def _view_row_id(items: tuple[Session, ...], *, kind: str, key: str) -> str:
    if kind == "tracked":
        return f"conversation:{key}"
    if kind == "independent":
        return f"threads:{key}"
    return f"session:{items[0].key}"


def _view_representative(items: tuple[Session, ...]) -> Session:
    """Choose from state-provided lineage flags, never from title/path heuristics."""
    heads = [item for item in items if not item.superseded and not item.diverged]
    if not heads:
        heads = [item for item in items if not item.superseded]
    if not heads:
        heads = list(items)
    return sorted(heads, key=lambda item: (-item.updated, item.tool, item.session_id))[0]


def _tracked_target(items: tuple[Session, ...]) -> Session | None:
    """Return a deterministic native representative for a validated head set.

    Equivalent heads are safe to present as one actionable row: UserState still
    performs the authoritative materialization and target validation at launch
    time.  Divergence is different and must remain both non-actionable here and
    refused by UserState.resolve_launch().
    """
    if any(item.diverged or item.conversation_blocker for item in items):
        return None
    heads = tuple(item for item in items if not item.superseded and not item.diverged)
    return _view_representative(heads) if heads else None


def _tracked_status(items: tuple[Session, ...], target: Session | None) -> str:
    blocker = next((item.conversation_blocker for item in items if item.conversation_blocker), "")
    if blocker:
        return blocker
    if any(item.diverged for item in items):
        return "diverged branch"
    if target is not None:
        return "lineage head"
    if items and all(item.superseded for item in items):
        return "superseded copy"
    return conversation_status(_view_representative(items))


def build_view_rows(
    sessions: Iterable[Session],
    expanded: Iterable[str] = (),
    visible_keys: Iterable[str] | None = None,
) -> tuple[ViewRow, ...]:
    """Build stable conversation-centered rows without mutating any session.

    Tracked members group only by their utility conversation id.  After those
    logical rows are built, equal-title rows in the same project family may be
    wrapped in a presentation-only container.  A family is formed only by
    equal or ancestor/descendant project paths; it never creates lineage.
    """
    source = tuple(sessions)
    visible = None if visible_keys is None else frozenset(visible_keys)
    expanded_ids = frozenset(expanded)
    tracked: dict[str, list[Session]] = {}
    independent: dict[tuple[str, str], list[Session]] = {}
    order: list[tuple[str, str | tuple[str, str] | Session]] = []
    for item in source:
        if item.conversation_id:
            if item.conversation_id not in tracked:
                tracked[item.conversation_id] = []
            tracked[item.conversation_id].append(item)

    eligible = source if visible is None else tuple(item for item in source if item.key in visible)
    # Stage 2 projects eligible children without changing the full tracked
    # membership or its authoritative head. Stage 3 groups only this result.
    eligible_keys = {item.key for item in eligible}
    eligible_tracked = {
        conversation_id: tuple(item for item in members if item.key in eligible_keys)
        for conversation_id, members in tracked.items()
    }
    ordered_tracked: set[str] = set()
    for item in eligible:
        if item.conversation_id:
            if item.conversation_id not in ordered_tracked:
                order.append(("tracked", item.conversation_id))
                ordered_tracked.add(item.conversation_id)
            continue
        group_key = _independent_group_key(item)
        if group_key not in independent:
            independent[group_key] = []
            order.append(("independent", group_key))
        independent[group_key].append(item)

    # First build the real logical rows.  Presentation containers are applied
    # below so a mixed tracked/untracked group can contain a tracked aggregate
    # without changing that aggregate's conversation semantics.
    logical_rows: list[ViewRow] = []
    for kind, key in order:
        if kind == "tracked":
            all_members = tuple(tracked[key])  # type: ignore[index]
            members = eligible_tracked[key]  # type: ignore[index]
            if not members:
                continue
            row_id = _view_row_id(members, kind=kind, key=key)  # type: ignore[arg-type]
            representative = _view_representative(all_members)
            target = _tracked_target(all_members)
            logical_rows.append(
                ViewRow(
                    row_id,
                    members,
                    all_members,
                    representative,
                    target,
                    _tracked_status(all_members, target),
                    display_title(representative),
                    project_identity(representative.cwd),
                    expandable=len(all_members) > 1,
                    actionable=target is not None,
                )
            )
            continue

        members = tuple(independent[key])  # type: ignore[index]
        all_members = members
        if len(members) == 1:
            item = members[0]
            logical_rows.append(
                ViewRow(
                    _view_row_id(members, kind="single", key=""),
                    members,
                    all_members,
                    item,
                    item,
                    "untracked",
                    display_title(item),
                    project_identity(item.cwd),
                )
            )
            continue
        group_key = "\x1f".join(key)  # type: ignore[arg-type]
        row_id = _view_row_id(members, kind="independent", key=group_key)
        logical_rows.append(
            ViewRow(
                row_id,
                members,
                all_members,
                _view_representative(members),
                None,
                "independent thread",
                display_title(members[0]),
                project_identity(members[0].cwd),
                expandable=True,
                actionable=False,
            )
        )

    def add_logical_row(rows: list[ViewRow], logical: ViewRow) -> None:
        expanded_here = logical.expandable and logical.row_id in expanded_ids
        rows.append(replace(logical, expanded=expanded_here))
        if not expanded_here:
            return
        rows.extend(
            ViewRow(
                f"{logical.row_id}/member:{item.key}",
                (item,),
                (item,),
                item,
                None if item.conversation_blocker else item,
                conversation_status(item)
                if logical.status != "independent thread"
                else "independent thread",
                display_title(item),
                project_identity(item.cwd),
                actionable=not bool(item.conversation_blocker),
            )
            for item in logical.members
        )

    # Find connected components within each title.  This permits a project root
    # and a child such as ``root``/``root/harness`` to share one stable family,
    # while sibling directories with no direct ancestor relation stay separate.
    title_indexes: dict[str, list[int]] = {}
    for index, row in enumerate(logical_rows):
        title_indexes.setdefault(normalize_space(row.title).casefold(), []).append(index)

    components: dict[int, tuple[int, ...]] = {}
    for indexes in title_indexes.values():
        # Union exact and ancestor paths through a path index. Walking parent
        # chains is bounded by path depth, unlike comparing every same-title
        # row with every other row.
        parents = {index: index for index in indexes}

        def find(index: int) -> int:
            root = index
            while parents[root] != root:
                root = parents[root]
            while parents[index] != index:
                next_index = parents[index]
                parents[index] = root
                index = next_index
            return root

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        by_project: dict[str, int] = {}
        for index in indexes:
            project = logical_rows[index].project
            if project == "(unknown project)":
                continue
            duplicate = by_project.get(project)
            if duplicate is not None:
                union(index, duplicate)
            else:
                by_project[project] = index

        for project, index in by_project.items():
            for ancestor in Path(project).parents:
                ancestor_index = by_project.get(str(ancestor))
                if ancestor_index is not None:
                    union(index, ancestor_index)
                    break

        grouped: dict[int, list[int]] = {}
        for index in indexes:
            grouped.setdefault(find(index), []).append(index)
        for component in grouped.values():
            if len(component) > 1:
                ordered = tuple(sorted(component))
                components[ordered[0]] = ordered

    rows: list[ViewRow] = []
    consumed: set[int] = set()
    for index, logical in enumerate(logical_rows):
        if index in consumed:
            continue
        component = components.get(index)
        if component is None:
            add_logical_row(rows, logical)
            continue
        consumed.update(component)
        children = tuple(logical_rows[child] for child in component)
        if any(any(member.conversation_id for member in child.members) for child in children):
            flattened: list[ViewRow] = []
            for child in children:
                if child.status != "independent thread":
                    flattened.append(child)
                    continue
                flattened.extend(
                    ViewRow(
                        _view_row_id((member,), kind="single", key=""),
                        (member,),
                        (member,),
                        member,
                        member,
                        "untracked",
                        display_title(member),
                        project_identity(member.cwd),
                    )
                    for member in child.members
                )
            children = tuple(flattened)
        native_members = tuple(member for child in children for member in child.members)
        native_all_members = tuple(member for child in children for member in child.all_members)
        project = _common_project_path(child.project for child in children)
        title = display_title(_view_representative(native_members))
        row_id = "project:" + "\x1f".join((project, normalize_space(title).casefold()))
        expanded_here = row_id in expanded_ids
        rows.append(
            ViewRow(
                row_id,
                native_members,
                native_all_members,
                _view_representative(native_all_members),
                None,
                "independent thread",
                title,
                project,
                expandable=True,
                expanded=expanded_here,
                actionable=False,
            )
        )
        if expanded_here:
            for child in children:
                add_logical_row(rows, child)
    labels = view_collision_labels(row for row in rows if "/member:" not in row.row_id)
    return tuple(replace(row, collision_label=labels.get(row.row_id, "")) for row in rows)


def view_collision_labels(rows: Iterable[ViewRow]) -> dict[str, str]:
    """Use human start times for duplicate titles; native IDs never enter this label."""
    groups: dict[str, list[ViewRow]] = {}
    for row in rows:
        groups.setdefault(normalize_space(row.title).casefold(), []).append(row)
    labels: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda row: (
                row.representative.created,
                row.representative.updated,
                row.project,
                row.row_id,
            ),
        )
        used: dict[str, int] = {}
        for row in ordered:
            stamp = exact_time(row.representative.created)
            ordinal = used.get(stamp, 0) + 1
            used[stamp] = ordinal
            labels[row.row_id] = f"[{stamp}{f' · {ordinal}' if ordinal > 1 else ''}]"
    return labels


def codex_resume_target(session_id: str, source: str, parent_id: str) -> str:
    """Compatibility helper for callers that reason about Codex parent threads."""
    return parent_id if source == SourceKind.SUBAGENT and parent_id else session_id


def load_codex_sessions() -> list[Session]:
    """Compatibility wrapper around the registered Codex discovery capability."""
    context = HarnessContext.create()
    adapter = REGISTRY.get("codex")
    discover = adapter.discover
    if isinstance(discover, Unsupported):
        return []
    return [session_from_native(item) for item in discover(context, use_cache=True)]


def load_claude_sessions(
    use_cache: bool = True,
    codex_refs: dict[str, list[str]] | None = None,
) -> list[Session]:
    """Compatibility wrapper around the registered Claude discovery capability."""
    context = HarnessContext.create(use_cache=use_cache)
    adapter = REGISTRY.get("claude")
    discover = adapter.discover
    if isinstance(discover, Unsupported):
        return []
    native = list(discover(context, use_cache=use_cache))
    if codex_refs is not None:
        for (tool, session_id), (tokens, _) in context.evidence.items():
            if tool == "claude":
                codex_refs[session_id] = list(tokens)
    return [session_from_native(item) for item in native]


def session_from_native(item: NativeSession) -> Session:
    """Apply neutral utility defaults to one adapter-owned discovery record."""
    auxiliary = item.archived or item.source is not SourceKind.INTERACTIVE
    if item.source is SourceKind.INTERACTIVE:
        origin = "human"
    elif item.source is SourceKind.SDK:
        origin = "cross"
    else:
        origin = "agent"
    return Session(
        tool=item.tool,
        session_id=item.session_id,
        title=item.title,
        cwd=item.cwd,
        updated=item.updated,
        created=item.created,
        preview=item.preview,
        named=item.named,
        storage=item.storage,
        source=item.source,
        auxiliary=auxiliary,
        origin=origin,
        resume_id=item.resume_id or item.session_id,
        parent_id=item.parent_id,
        original_title=item.title,
        original_named=item.named,
        message_count=item.message_count,
        agent_nickname=item.agent_nickname,
        turn_count=item.turn_count,
        compaction_count=item.compaction_count,
        prompt_count=item.prompt_count,
    )


def reconcile_evidence(sessions: list[Session], context: HarnessContext) -> None:
    """Resolve candidate IDs against discovered rows or verified native existence."""
    by_key = {(item.tool, item.session_id): item for item in sessions}
    by_id: dict[str, list[Session]] = {}
    for item in sessions:
        by_id.setdefault(item.session_id, []).append(item)
    by_folded_id: dict[str, list[Session]] = {}
    for item in sessions:
        by_folded_id.setdefault(item.session_id.casefold(), []).append(item)
    for key in context.origin_hints:
        target = by_key.get(key)
        if target is not None and target.source is SourceKind.NON_INTERACTIVE:
            target.origin = "cross"
    locate_cache: dict[tuple[str, str], bool] = {}
    probes = 0
    probe_limit_reported = False
    incomplete_publishers: list[str] = []

    def evidence_rank(
        entry: tuple[tuple[str, str], tuple[list[str], bool]],
    ) -> tuple[bool, float, str, str]:
        publisher = by_key.get(entry[0])
        tool, session_id = entry[0]
        return (
            publisher is None,
            -(publisher.updated if publisher is not None else 0),
            tool,
            session_id,
        )

    for (publisher_tool, publisher_id), (tokens, truncated) in sorted(
        context.evidence.items(), key=evidence_rank
    ):
        publisher = by_key.get((publisher_tool, publisher_id))
        if publisher_tool not in REGISTRY:
            record_warning(
                f"ID evidence came from unavailable harness {publisher_tool}:{publisher_id}; "
                "ignored"
            )
            continue
        if truncated:
            incomplete_publishers.append(f"{publisher_tool}:{publisher_id}")
        for token in tokens:
            # Native writers always mint a fresh target id. An own-id token is
            # therefore transcript metadata, not evidence that an unrelated
            # harness with the same namespaced id is a causal counterpart.
            if token.casefold() == publisher_id.casefold():
                continue
            discovered = [item for item in by_id.get(token, []) if item.tool != publisher_tool]
            if not discovered:
                raw = token.encode("ascii", errors="ignore")
                discovered = [
                    item
                    for item in by_folded_id.get(token.casefold(), [])
                    if item.tool != publisher_tool
                    and any(
                        pattern.flags & re.I and pattern.fullmatch(raw)
                        for pattern in REGISTRY.get(item.tool).id_patterns
                    )
                ]
            # Transcript text is only a heuristic counterpart signal.  Keep
            # legacy bridge discovery for writer-created/imported artifacts,
            # but never let text alone join two human/agent sessions.
            if (
                discovered
                and not (publisher is not None and publisher.source is SourceKind.NON_INTERACTIVE)
                and not any(item.source is SourceKind.NON_INTERACTIVE for item in discovered)
            ):
                continue
            resolved: list[tuple[str, str, Session | None]] = [
                (item.tool, item.session_id, item) for item in discovered
            ]
            if not resolved:
                raw = token.encode("ascii", errors="ignore")
                for adapter in REGISTRY.adapters():
                    if adapter.name == publisher_tool or not any(
                        pattern.fullmatch(raw) for pattern in adapter.id_patterns
                    ):
                        continue
                    resolver = adapter.resolve
                    if isinstance(resolver, Unsupported):
                        continue
                    cache_key = (adapter.name, token)
                    if cache_key not in locate_cache:
                        indexed = context.native_refs.get(cache_key)
                        if indexed:
                            locate_cache[cache_key] = True
                        elif adapter.name in context.complete_native_indexes:
                            locate_cache[cache_key] = False
                        elif probes >= MAX_EXISTENCE_PROBES_PER_PASS:
                            if not probe_limit_reported:
                                record_warning(
                                    "native existence probes were capped for this discovery "
                                    "pass; some positive counterpart lookups were skipped"
                                )
                                probe_limit_reported = True
                            continue
                        else:
                            probes += 1
                            try:
                                locate_cache[cache_key] = resolver(token) is not None
                            except OSError:
                                locate_cache[cache_key] = False
                    exists = locate_cache[cache_key]
                    if exists:
                        resolved.append((adapter.name, token, by_key.get((adapter.name, token))))
            # An existence probe has no discovered target row to establish
            # the bridge-artifact side, so only a non-interactive publisher
            # may create this legacy mapping.
            if not resolved or (
                not discovered
                and (publisher is None or publisher.source is not SourceKind.NON_INTERACTIVE)
            ):
                continue
            identities = {(tool, session_id) for tool, session_id, _ in resolved}
            if len(identities) != 1:
                continue
            identity = next(iter(identities))
            target_tool, target_id = identity
            target = next(
                (item for tool, session_id, item in resolved if (tool, session_id) == identity),
                None,
            )
            if publisher is not None:
                publisher.launch_targets.setdefault(target_tool, target_id)
            if target is not None:
                if target.source is SourceKind.NON_INTERACTIVE:
                    target.origin = "cross"
                if publisher is not None:
                    target.launch_targets.setdefault(publisher_tool, publisher.session_id)
    if incomplete_publishers:
        examples = ", ".join(incomplete_publishers[:3])
        remainder = len(incomplete_publishers) - 3
        if remainder > 0:
            examples += f", and {remainder} more"
        record_warning(
            f"ID evidence was truncated or incomplete for {len(incomplete_publishers)} "
            f"session(s) ({examples}); negative counterpart conclusions were suppressed"
        )


def collect_scratch_origin_hints(sessions: list[Session], context: HarnessContext) -> None:
    """Match non-interactive cwd evidence against each publisher's declared scratch shape."""
    for publisher in REGISTRY.adapters():
        if not publisher.scratch_patterns:
            continue
        for item in sessions:
            if item.tool == publisher.name or item.source is not SourceKind.NON_INTERACTIVE:
                continue
            if any(pattern.search(item.cwd) for pattern in publisher.scratch_patterns):
                context.mark_cross_origin(item.tool, item.session_id, publisher.name)


def dedupe_sessions(sessions: Iterable[Session]) -> list[Session]:
    """Prefer the newest duplicate while retaining equal IDs from different harnesses."""
    unique: dict[tuple[str, str], Session] = {}
    for item in sessions:
        key = (item.tool, item.session_id)
        current = unique.get(key)
        item_rank = (item.updated, item.created, item.storage, item.title, item.cwd)
        current_rank = (
            (current.updated, current.created, current.storage, current.title, current.cwd)
            if current is not None
            else None
        )
        if current_rank is None or item_rank > current_rank:
            unique[key] = item
    return list(unique.values())


def load_sessions(
    use_cache: bool = True,
    state: UserState | None = None,
    config: LaunchConfig | None = None,
) -> list[Session]:
    clear_warnings()
    commands = (
        {name: tuple(config.provider_command(name)) for name in REGISTRY.names()}
        if config is not None
        else None
    )
    context = HarnessContext.create(use_cache=use_cache, provider_commands=commands)
    sessions: list[Session] = []
    for adapter in REGISTRY.adapters():
        discover = adapter.discover
        if isinstance(discover, Unsupported):
            continue
        try:
            native_rows = list(discover(context, use_cache=use_cache))
        except OSError as error:
            record_warning(f"could not discover {adapter.label} sessions: {error}")
            continue
        for native in native_rows:
            if native.tool != adapter.name:
                record_warning(
                    f"{adapter.label} discovery returned a foreign {native.tool!r} row; ignored"
                )
                continue
            if native.source not in adapter.source_kinds:
                record_warning(
                    f"{adapter.label} discovery returned undeclared source {native.source.value!r}; "
                    "ignored"
                )
                continue
            sessions.append(session_from_native(native))
    result = dedupe_sessions(sessions)
    collect_scratch_origin_hints(result, context)
    reconcile_evidence(result, context)
    if state is not None:
        state.apply(result)
    detect_open_sessions(result, context=context)
    return result


def _catalog_signature() -> list[dict[str, Any]]:
    return [
        {"name": adapter.name, "order": adapter.order, "home": str(adapter.home)}
        for adapter in REGISTRY.adapters()
    ]


def save_session_catalog(sessions: Iterable[Session], path: Path = CATALOG_CACHE_FILE) -> None:
    """Persist a safe first-paint catalog; live and utility-owned state is recomputed."""
    rows: list[dict[str, Any]] = []
    for item in sessions:
        payload = asdict(item)
        payload.update(
            renamed=False,
            hidden=False,
            launch_tool="",
            conversation_id="",
            superseded=False,
            diverged=False,
            conversation_blocker="",
            is_open=False,
            open_pid=0,
        )
        rows.append(payload)
    document = {
        "version": CATALOG_CACHE_VERSION,
        "registry": _catalog_signature(),
        "sessions": rows,
    }
    temporary = path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_session_catalog(
    state: UserState | None = None,
    path: Path = CATALOG_CACHE_FILE,
) -> list[Session]:
    """Load the last complete catalog without touching any provider-owned storage."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    if (
        not isinstance(document, dict)
        or document.get("version") != CATALOG_CACHE_VERSION
        or document.get("registry") != _catalog_signature()
        or not isinstance(document.get("sessions"), list)
    ):
        return []
    field_names = {field.name for field in fields(Session)}
    migratable_fields = {
        "turn_count",
        "compaction_count",
        "prompt_count",
        "conversation_blocker",
    }
    result: list[Session] = []
    for payload in document["sessions"][:100_000]:
        if not isinstance(payload, dict):
            continue
        keys = set(payload)
        missing = field_names - keys
        if keys - field_names or missing - migratable_fields:
            continue
        if missing:
            message_count = payload.get("message_count")
            if not isinstance(message_count, int) or isinstance(message_count, bool):
                continue
            defaults = {
                "turn_count": message_count,
                "compaction_count": 0,
                "prompt_count": message_count,
                "conversation_blocker": "",
            }
            payload = {**payload, **{name: defaults[name] for name in missing}}
        try:
            item = Session(**payload)
        except (TypeError, ValueError):
            continue
        if item.tool in REGISTRY:
            item.is_open = False
            item.open_pid = 0
            result.append(item)
    result = dedupe_sessions(result)
    if state is not None:
        state.apply(result)
    return result


class SessionRefresh:
    """One background provider refresh with a pollable, single-consumer result."""

    def __init__(
        self,
        *,
        use_cache: bool,
        state: UserState,
        config: LaunchConfig,
        catalog_path: Path = CATALOG_CACHE_FILE,
    ) -> None:
        self._use_cache = use_cache
        self._state = state
        self._config = config
        self._catalog_path = catalog_path
        self._done = threading.Event()
        self._taken = False
        self._sessions: list[Session] | None = None
        self._warnings: tuple[str, ...] = ()
        self._error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="ai-sessions-refresh",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            sessions = load_sessions(
                use_cache=self._use_cache,
                state=self._state,
                config=self._config,
            )
            save_session_catalog(sessions, self._catalog_path)
            self._sessions = sessions
            self._warnings = tuple(load_warnings())
        except Exception as error:
            self._error = f"refresh failed: {error}"
        finally:
            self._done.set()

    def ready(self) -> bool:
        return self._done.is_set()

    def take(self, *, wait: bool = False) -> tuple[list[Session] | None, tuple[str, ...], str]:
        if wait:
            self._done.wait()
        if not self._done.is_set() or self._taken:
            return None, (), ""
        self._taken = True
        return self._sessions, self._warnings, self._error


def detect_open_sessions(
    sessions: Iterable[Session], *, context: HarnessContext | None = None
) -> None:
    """Fan one shared host snapshot through registered adapter liveness hooks."""
    session_list = list(sessions)
    for item in session_list:
        item.is_open = False
        item.open_pid = 0
    pending: list[tuple[HarnessAdapter, list[Session]]] = []
    for adapter in REGISTRY.adapters():
        if isinstance(adapter.inspect_liveness, Unsupported):
            continue
        own = [
            item
            for item in session_list
            if item.tool == adapter.name and item.source in adapter.liveness_source_kinds
        ]
        if own:
            pending.append((adapter, own))
    if not pending:
        return
    shared = context or HarnessContext.create()
    populate_liveness_context(shared)
    snapshot = immutable_liveness_context(shared)
    live_pids = {process.pid for process in snapshot.process_snapshot}
    for adapter, own in pending:
        hook = adapter.inspect_liveness
        assert not isinstance(hook, Unsupported)
        views = tuple(LivenessSession(item.session_id, item.source, item.storage) for item in own)
        try:
            detected = hook(snapshot, adapter.home, views)
            if not isinstance(detected, Mapping):
                raise TypeError("liveness hook did not return a mapping")
            pairs = tuple(detected.items())
        except Exception as error:
            record_warning(f"could not inspect {adapter.label} liveness: {error}")
            continue
        by_id = {item.session_id: item for item in own}
        for session_id, pid in pairs:
            item = by_id.get(session_id) if isinstance(session_id, str) else None
            if (
                item is None
                or item.source not in adapter.liveness_source_kinds
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or pid not in live_pids
            ):
                continue
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


# Terminals that can run a command, in the order they are probed.  kitty is
# first because the bundled launcher uses it, and its --class/--title shape is
# what window-position restorers key off, so a respawned window lands where the
# user left it.
TERMINAL_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "kitty",
        (
            "--detach",
            "--class",
            "kitty-tmux-{session}",
            "--title",
            "tmux:{session}",
            "--",
            "bash",
            "-lc",
            "{script}",
        ),
    ),
    ("wezterm", ("start", "--", "bash", "-lc", "{script}")),
    ("ghostty", ("-e", "bash", "-lc", "{script}")),
    ("alacritty", ("-T", "tmux:{session}", "-e", "bash", "-lc", "{script}")),
    ("gnome-terminal", ("--title", "tmux:{session}", "--", "bash", "-lc", "{script}")),
    ("konsole", ("-e", "bash", "-lc", "{script}")),
    ("xterm", ("-T", "tmux:{session}", "-e", "bash", "-lc", "{script}")),
)


def tmux_attach_command(session: str, window_id: str, pane_id: str) -> str:
    """A shell command attaching to a session with the right pane selected.

    Selection travels with the attach rather than preceding it, so nothing
    else can move the session between the two steps.
    """
    parts = ["tmux", "attach-session", "-t", shlex.quote(session)]
    if window_id:
        parts += ["\\;", "select-window", "-t", shlex.quote(window_id)]
    if pane_id:
        parts += ["\\;", "select-pane", "-t", shlex.quote(pane_id)]
    return " ".join(parts)


def terminal_argv(
    config: LaunchConfig | None, session: str, window_id: str, pane_id: str
) -> list[str]:
    """Resolve the terminal to spawn: configured, then $TERMINAL, then probed."""
    script = tmux_attach_command(session, window_id, pane_id)
    fields = {"session": session, "window": window_id, "pane": pane_id, "script": script}

    def expand(template: Iterable[str]) -> list[str]:
        rendered: list[str] = []
        for argument in template:
            for key, value in fields.items():
                argument = argument.replace("{" + key + "}", value)
            rendered.append(argument)
        return rendered

    configured = list(config.terminal_command) if config and config.terminal_command else []
    if configured:
        # A configured command may name the session itself rather than take
        # the prepared script, so only append one when it asked for neither.
        if not any("{script}" in argument for argument in configured):
            configured = configured + ["bash", "-lc", script]
        return expand(configured)
    preferred = os.environ.get("TERMINAL", "").strip()
    if preferred and shutil.which(preferred):
        return expand([preferred, "-e", "bash", "-lc", "{script}"])
    for name, template in TERMINAL_PROBES:
        if shutil.which(name):
            return expand([name, *template])
    return []


def spawn_terminal(argv: list[str], env: dict[str, str]) -> bool:
    """Start a terminal detached from this process and from the curses screen."""
    try:
        subprocess.Popen(  # noqa: S603
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    return True


def focus_open_session(item: Session, config: LaunchConfig | None = None) -> tuple[bool, str]:
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
    window_id = ""
    roots: list[int] = []
    # How many clients tmux reports on this session, and whether it answered
    # at all.  Spawning a second client on a session that already has one
    # leaves two windows fighting over one pane, and tmux sizes to the
    # smallest, so the difference between "none attached" and "attached but
    # unreachable" decides whether recovery is safe.
    attached = 0
    clients_known = False

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
            clients_known = True
            for line in clients.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[0] == tmux_session:
                    attached += 1
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
            try:
                owner_pid = int(parts[2])
            except ValueError:
                continue
            if owner_pid in ancestors:
                candidates.append(parts[0])
            elif (
                attached and tmux_session and len(parts) > 8 and parts[8] == f"tmux:{tmux_session}"
            ):
                # The title is a launcher convention, not a tmux fact: it is
                # fixed at launch and survives switch-client, so it is only
                # worth trusting as a last resort while a client really is on
                # this session.  Equality on the title field keeps session 2
                # from matching a window titled tmux:21.
                title_fallback.append(parts[0])
        for desktop_window in candidates + title_fallback:
            focused = run_capture(["wmctrl", "-i", "-a", desktop_window], env=desktop_env)
            if focused and focused.returncode == 0:
                # wmctrl activation is asynchronous under Mutter. xdotool's
                # --sync makes Enter feel immediate when it is available.
                run_capture(
                    ["xdotool", "windowactivate", "--sync", desktop_window],
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
        manual = f"tmux attach-session -t {shlex.quote(tmux_session)}"
        if clients_known and not attached:
            # Nothing is displaying this session: the terminal died while the
            # server, the shells and the harness kept running.  This is the
            # recoverable case, and the only one where opening a client is
            # safe -- a second client on an already-attached session leaves
            # two windows fighting over one pane.
            display = displays[0] if displays else os.environ.get("DISPLAY", "")
            if not display:
                return False, (
                    f"tmux {tmux_session}:{tmux_window} is alive with no terminal attached, "
                    f"and there is no display to open one on. Run:  {manual}"
                )
            argv = terminal_argv(config, tmux_session, window_id, pane_id)
            if not argv:
                return False, (
                    f"tmux {tmux_session}:{tmux_window} is alive with no terminal attached, "
                    "and no terminal emulator was found to open one. Set [terminal] command "
                    f"in {CONFIG_FILE}, or run:  {manual}"
                )
            desktop_env = dict(os.environ)
            desktop_env["DISPLAY"] = display
            if spawn_terminal(argv, desktop_env):
                return True, (
                    f"tmux {tmux_session}:{tmux_window} had no terminal attached; "
                    f"opened {Path(argv[0]).name} on it."
                )
            return False, (
                f"tmux {tmux_session}:{tmux_window} is alive with no terminal attached, "
                f"and {Path(argv[0]).name} could not be started. Run:  {manual}"
            )
        return (
            False,
            f"Selected tmux {tmux_session}:{tmux_window} pane {pane_id}. A terminal is attached "
            f"to it but its window could not be raised, so no second one was opened — it may be "
            f"on another display, on Wayland, or remote. Switch to it, or run:  {manual}",
        )
    return False, (
        f"The session is open in terminal process {item.open_pid}, but its desktop window "
        "could not be identified and it is not in tmux, so there is nothing to reattach to."
    )


def query_match(session: Session, query: str) -> tuple[bool, int]:
    words = [word for word in query.casefold().split() if word]
    if not words:
        return True, 0
    haystack = session_searchable(session)
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
        if word.startswith("launch:"):
            wanted = word[7:]
            if wanted and not any(
                item.startswith(wanted) for item in available_launch_tools(session)
            ):
                return False, 0
            score += 20
            continue
        if word.startswith("harness:"):
            wanted = word[8:]
            if wanted and not any(
                item.startswith(wanted) for item in available_launch_tools(session)
            ):
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
    tools: Iterable[str] | None = None,
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
        if tools is not None:
            if item.tool not in tools:
                continue
        elif tool != "all" and item.tool != tool:
            continue
        if directory and item.cwd != directory:
            continue
        matched, score = query_match(item, query)
        if matched:
            matches.append((score, item))
    if query:
        return [
            item
            for _, item in sorted(
                matches,
                key=lambda pair: (
                    -pair[0],
                    -pair[1].updated,
                    pair[1].tool,
                    pair[1].session_id,
                ),
            )
        ]
    if sort_mode == "title":
        return [
            item
            for _, item in sorted(
                matches,
                key=lambda pair: (
                    display_title(pair[1]).casefold(),
                    -pair[1].updated,
                    pair[1].tool,
                    pair[1].session_id,
                ),
            )
        ]
    if sort_mode == "directory":
        return [
            item
            for _, item in sorted(
                matches,
                key=lambda pair: (
                    pair[1].cwd.casefold(),
                    -pair[1].updated,
                    pair[1].tool,
                    pair[1].session_id,
                ),
            )
        ]
    if sort_mode == "messages":
        return [
            item
            for _, item in sorted(
                matches,
                key=lambda pair: (
                    -pair[1].message_count,
                    -pair[1].updated,
                    pair[1].tool,
                    pair[1].session_id,
                ),
            )
        ]
    if sort_mode == "open":
        return [
            item
            for _, item in sorted(
                matches,
                key=lambda pair: (
                    not pair[1].is_open,
                    -pair[1].updated,
                    pair[1].tool,
                    pair[1].session_id,
                ),
            )
        ]
    return [
        item
        for _, item in sorted(
            matches,
            key=lambda pair: (-pair[1].updated, pair[1].tool, pair[1].session_id),
        )
    ]


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


def ellipsize_with_suffix(value: str, suffix: str, width: int) -> str:
    """Clip a title while keeping its collision suffix visible when space permits."""
    if not suffix:
        return ellipsize(value, width)
    if width <= len(suffix):
        if width <= 1:
            return ellipsize(suffix, width)
        # Collision ordinals live at the end; preserve that distinguishing
        # portion when an exceptionally narrow row cannot show the full date.
        return "…" + suffix[-(width - 1) :]
    separator = " "
    title_width = width - len(separator) - len(suffix)
    return ellipsize(value, title_width) + separator + suffix


class Browser:
    def __init__(
        self,
        screen: Any,
        sessions: list[Session],
        state: UserState,
        launch_config: LaunchConfig,
        use_cache: bool = True,
        refresh: SessionRefresh | None = None,
    ) -> None:
        self.screen = screen
        self.sessions = sessions
        self.state = state
        self.launch_config = launch_config
        self.use_cache = use_cache
        self.refresh = refresh
        self._tools = set(REGISTRY.names())
        self.origin = "human"
        self.visibility = "visible"
        self.directory = ""
        self.query = ""
        self.sort_mode = "recent"
        self.selected = 0
        self.offset = 0
        self.expanded: set[str] = set()
        self.project_focus = ""
        self.searching = False
        self.message = (
            "Loading sessions…"
            if refresh is not None and not sessions
            else "Refreshing sessions…"
            if refresh is not None
            else ""
        )
        self.result: Session | None = None
        self.pairs = self._pair_layout()
        self.setup_colors()

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self._tools)

    @tools.setter
    def tools(self, values: Iterable[str]) -> None:
        available = set(REGISTRY.names())
        selected = {value for value in values if value in available}
        self._tools = selected or available

    @property
    def tool(self) -> str:
        available = set(REGISTRY.names())
        if self._tools == available:
            return "all"
        if len(self._tools) == 1:
            return next(iter(self._tools))
        return "multiple"

    @tool.setter
    def tool(self, value: str) -> None:
        if value == "all":
            self._tools = set(REGISTRY.names())
        elif value in REGISTRY:
            self._tools = {value}

    def _set_input_timeout(self, milliseconds: int) -> None:
        timeout = getattr(self.screen, "timeout", None)
        if timeout is not None:
            timeout(milliseconds)

    def run_modal(self, action: Any) -> None:
        """Suspend refresh polling while a modal owns keyboard input."""
        self._set_input_timeout(-1)
        try:
            action()
        finally:
            if self.refresh is not None:
                self._set_input_timeout(100)

    def apply_refresh(self, *, wait: bool = False) -> bool:
        if self.refresh is None:
            return False
        sessions, warnings, error = self.refresh.take(wait=wait)
        if sessions is None and not error:
            return False
        previous = self.selected_id()
        previous_parent = previous.split("/member:", 1)[0] if "/member:" in previous else ""
        self.refresh = None
        self._set_input_timeout(-1)
        if error:
            self.message = error + "; press Ctrl-R to retry"
            return True
        assert sessions is not None
        self.sessions = sessions
        # A disappeared child cannot retain launch intent or selection. Restore
        # its aggregate only when that exact stable parent still exists.
        self.keep_selection(previous, fallback_id=previous_parent)
        live_ids = {row.row_id for row in self.view_rows()}
        self.expanded.intersection_update(row_id for row_id in live_ids if "/member:" not in row_id)
        self.message = "; ".join(warnings) or "Sessions refreshed."
        return True

    def finish_refresh(self) -> None:
        if self.refresh is None:
            return
        self.message = "Finishing session refresh…"
        self.draw()
        self.apply_refresh(wait=True)

    def begin_refresh(self) -> None:
        if self.refresh is not None:
            self.message = "A session refresh is already running."
            return
        self.refresh = SessionRefresh(
            use_cache=self.use_cache,
            state=self.state,
            config=self.launch_config,
        )
        self.message = "Refreshing sessions…"
        self._set_input_timeout(100)

    @staticmethod
    def _pair_layout() -> dict[str, int]:
        names = [
            "accent",
            *(adapter.name for adapter in REGISTRY.adapters()),
            "selected",
            "primary",
            "warning",
            "success",
            "muted",
            "human",
            "cross",
            "agent",
            "hidden",
            "timestamp",
            "messages",
        ]
        return {name: number for number, name in enumerate(names, 1)}

    def setup_colors(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        try:
            terminal_has_colors = curses.has_colors()
        except curses.error:
            terminal_has_colors = False
        self.colors_enabled = bool(
            not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb"
            and terminal_has_colors
        )
        self.initialized_pairs: set[int] = set()
        if not self.colors_enabled:
            return
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        provider_count = len(REGISTRY.adapters())
        if getattr(curses, "COLORS", 0) >= 256:
            provider_palette = (75, 176, 39, 208, 111, 141, 45)
            colors = [45]
            colors.extend(
                provider_palette[index % len(provider_palette)] for index in range(provider_count)
            )
            colors.extend((-1, -1, 214, 82, 245, 114, 215, 141, 245, 110, 223))
            backgrounds = [-1] * len(colors)
            backgrounds[self.pairs["selected"] - 1] = 45
            colors[self.pairs["selected"] - 1] = 16
        else:
            provider_palette = (curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_CYAN)
            colors = [curses.COLOR_CYAN]
            colors.extend(
                provider_palette[index % len(provider_palette)] for index in range(provider_count)
            )
            colors.extend(
                (
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
            )
            backgrounds = [-1] * len(colors)
            backgrounds[self.pairs["selected"] - 1] = curses.COLOR_CYAN
        for number, (foreground, background) in enumerate(zip(colors, backgrounds), 1):
            if number >= getattr(curses, "COLOR_PAIRS", 0):
                break
            try:
                curses.init_pair(number, foreground, background)
                self.initialized_pairs.add(number)
            except curses.error:
                pass

    def style(self, name: str = "primary", attrs: int = 0, selected: bool = False) -> int:
        pair = self.pairs.get("selected" if selected else name, self.pairs["primary"])
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
        items = filtered_sessions(
            self.sessions,
            tools=self.tools,
            directory=self.directory,
            query=self.query,
            origin=self.origin,
            visibility=self.visibility,
            sort_mode=self.sort_mode,
        )
        if self.project_focus:
            focus = project_identity(self.project_focus)
            items = [item for item in items if _projects_related(focus, project_identity(item.cwd))]
        return items

    def view_rows(self) -> tuple[ViewRow, ...]:
        eligible = self.current()
        return build_view_rows(
            self.sessions,
            expanded=self.expanded,
            visible_keys=(item.key for item in eligible),
        )

    def keep_selection(self, previous_id: str = "", *, fallback_id: str = "") -> None:
        items = self.view_rows()
        if previous_id:
            member_parent = previous_id.split("/member:", 1)[0] if "/member:" in previous_id else ""
            for index, item in enumerate(items):
                if item.row_id == previous_id:
                    self.selected = index
                    break
            else:
                parent_id = fallback_id or member_parent
                if parent_id:
                    for index, item in enumerate(items):
                        if item.row_id == parent_id:
                            self.selected = index
                            break
                    else:
                        self.selected = min(self.selected, max(0, len(items) - 1))
                else:
                    # Callers such as hide/unhide retain the stable native key,
                    # while expanded rows retain the /member row id.  all_members
                    # lets the former fall back to its surviving aggregate rather
                    # than whatever sibling happens to occupy the old index.
                    for index, item in enumerate(items):
                        if any(member.key == previous_id for member in item.all_members):
                            self.selected = index
                            break
                    else:
                        self.selected = min(self.selected, max(0, len(items) - 1))
        else:
            self.selected = min(self.selected, max(0, len(items) - 1))

    def selected_id(self) -> str:
        items = self.view_rows()
        if items and 0 <= self.selected < len(items):
            return items[self.selected].row_id
        return ""

    def selected_row(self) -> ViewRow | None:
        rows = self.view_rows()
        return rows[self.selected] if rows and 0 <= self.selected < len(rows) else None

    def selected_target(self) -> Session | None:
        row = self.selected_row()
        return row.target if row is not None and row.actionable else None

    def toggle_expansion(self, *, collapse: bool = False) -> bool:
        row = self.selected_row()
        if row is None or not row.expandable:
            return False
        if collapse or row.row_id in self.expanded:
            self.expanded.discard(row.row_id)
        else:
            self.expanded.add(row.row_id)
        self.keep_selection(row.row_id)
        return True

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        rows = self.view_rows()
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
        refresh_text = " · refreshing" if self.refresh is not None else ""
        count_text = (
            f"{open_count} open · {len(rows)} shown · {len(self.sessions)} indexed{refresh_text}"
        )
        self.add(0, max(1, width - len(count_text) - 2), count_text, self.style("muted"))

        tool_text = tool_filter_label(self.tools)
        dir_text = "All directories" if not self.directory else short_path(self.directory)
        filters = (
            f"{tool_text}  ·  {ORIGIN_LABELS[self.origin]}  ·  "
            f"{VISIBILITY_LABELS[self.visibility]}  ·  "
            f"{ellipsize(dir_text, max(10, width // 4))}  ·  {SORT_LABELS[self.sort_mode]}"
        )
        self.add(1, 1, filters, self.style("muted"), width - 2)
        if self.project_focus:
            focus = "PROJECT FOCUS " + short_path(self.project_focus)
            self.add(
                2,
                max(1, width - len(focus) - 2),
                focus,
                self.style("success", curses.A_BOLD),
                width - 2,
            )

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
        self.selected = min(self.selected, max(0, len(rows) - 1))
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + visible_rows:
            self.offset = self.selected - visible_rows + 1
        self.offset = min(self.offset, max(0, len(rows) - visible_rows))

        show_directory = width >= 100
        tool_width = tool_column_width(8)
        origin_width, open_width, activity_width, time_width, status_width = 7, 6, 10, 10, 18
        directory_width = min(34, max(16, width // 4)) if show_directory else 0
        title_width = (
            width
            - 5
            - tool_width
            - origin_width
            - open_width
            - activity_width
            - (time_width * 2)
            - status_width
            - directory_width
        )
        heading = (
            f"  {'TOOL':<{tool_width}}{'ORIGIN':<{origin_width}}"
            f"{'OPEN':<{open_width}}{'ACTIVITY':<{activity_width}}"
            f"{'STARTED':<{time_width}}{'UPDATED':<{time_width}}"
            f"{'STATUS':<{status_width}}TITLE"
        )
        self.add(3, 1, heading, self.style("muted", curses.A_BOLD), width - 2)

        if not rows:
            empty_text = (
                "Loading sessions from installed tools…"
                if self.refresh is not None and not self.sessions
                else "No sessions match these filters."
            )
            self.add(list_top + 1, 3, empty_text, self.style("warning"))
            self.add(
                list_top + 3,
                3,
                "Esc clears the search; t chooses tools; d changes directory.",
                self.style("muted"),
            )
        else:
            for row, view_row in enumerate(rows[self.offset : self.offset + visible_rows]):
                index = self.offset + row
                y = list_top + row
                selected = index == self.selected
                item = view_row.representative
                marker = (
                    "›"
                    if selected
                    else ("⊘" if any(member.hidden for member in view_row.members) else " ")
                )
                expander = "▾" if view_row.expanded else ("▸" if view_row.expandable else " ")
                item_tool_label = tool_label(item.tool)
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
                segment(
                    f"{marker}{expander} ",
                    marker_color,
                    curses.A_BOLD if marker.strip() else 0,
                )
                segment(f"{item_tool_label:<{tool_width}}", item.tool, curses.A_BOLD)
                segment(f"{origin_label:<{origin_width}}", item.origin, curses.A_BOLD)
                open_symbol = "Ⅱ" if paused else ("●" if item.is_open else "")
                segment(
                    f"{open_symbol:<{open_width}}",
                    "warning" if paused else "success",
                    curses.A_BOLD,
                )
                segment(f"{activity_label(item):<{activity_width}}", "messages")
                segment(f"{relative_time(item.created):<{time_width}}", "muted")
                segment(f"{relative_time(item.updated):<{time_width}}", "timestamp")
                status = view_row.status
                segment(
                    f"{status:<{status_width}}",
                    "warning" if status != "lineage head" else "success",
                    curses.A_BOLD,
                )
                title = _normal_agent_tag(item) + view_row.title
                if view_row.status == "independent thread":
                    title += f" ({len(view_row.members)} threads)"
                elif len(view_row.all_members) > 1:
                    title += f" ({len(view_row.all_members) - 1} copies)"
                if view_row.collision_label:
                    title += " " + view_row.collision_label
                if not item.named:
                    title = "*- " + title
                room = max(0, title_width)
                segment(ellipsize(title, room).ljust(room), "primary", curses.A_BOLD)
                if show_directory:
                    segment(ellipsize(directory, directory_width).ljust(directory_width), "muted")

        for x in range(1, width - 1):
            self.add(detail_top, x, "─", self.style("muted", curses.A_DIM))
        if rows:
            selected_row = rows[self.selected]
            item = selected_row.representative
            missing = bool(item.cwd and not Path(item.cwd).is_dir())
            name_badge = " · utility name" if item.renamed else (" · named" if item.named else "")
            source_badge = f" · {item.source}" if item.source != "interactive" else ""
            hidden_badge = " · hidden" if item.hidden else ""
            launch_tool = tool_label(active_launch_tool(item))
            if session_needs_bridge(item):
                launch_tool += " (bridged copy)"
            if item.is_open and process_state(item.open_pid) in ("T", "t"):
                open_badge = f" · PAUSED in terminal (PID {item.open_pid})"
            else:
                open_badge = f" · OPEN now (PID {item.open_pid})" if item.is_open else ""
            self.add(
                detail_top + 1,
                2,
                f"{tool_label(item.tool)} · {ORIGIN_LABELS[item.origin]} · "
                f"launch via {launch_tool} · "
                f"{activity_label(item)}{name_badge}{source_badge}{hidden_badge}{open_badge}",
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
            self.add(
                detail_top + 4,
                2,
                f"Status     {selected_row.status} · Activity {activity_label(item)}",
                self.style("muted"),
                width - 4,
            )
            latest_user_message = clean_prompt(item.preview)
            detail_row = detail_top + 5
            if item.parent_id and item.resume_target != item.session_id:
                self.add(
                    detail_row,
                    2,
                    "Opens      parent session " + item.resume_target,
                    self.style("warning"),
                    width - 4,
                )
                detail_row += 1
            elif item.parent_id:
                self.add(
                    detail_row,
                    2,
                    "Parent     " + item.parent_id + " · resumes this child directly",
                    self.style("muted"),
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

        footer = self.message or (
            "Enter open  t tools  i details  f focus  Space/Right expand  Left collapse  "
            "Ctrl-F search  r rename  h hide  q quit  ? help"
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
        order = tool_order()
        index = order.index(self.tool) if self.tool in order else 0
        self.tool = order[(index + (-1 if backwards else 1)) % len(order)]
        if self.directory and not any(
            item.cwd == self.directory and item.tool in self.tools for item in self.sessions
        ):
            self.directory = ""
        self.keep_selection(previous)

    def tool_picker(self) -> None:
        adapters = list(REGISTRY.adapters())
        if not adapters:
            self.message = "No session tools are registered."
            return
        previous = self.selected_id()
        pending = set(self.tools)
        selected = 0
        notice = ""
        while True:
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            box_width = min(70, max(30, width - 4))
            left = max(0, (width - box_width) // 2)
            visible = max(1, height - 8)
            offset = min(max(0, selected - visible + 1), max(0, len(adapters) - visible))
            self.add(1, left, "TOOLS — CHOOSE WHAT TO SHOW", self.style("accent", curses.A_BOLD))
            self.add(
                3,
                left,
                "Space toggle · a all · Enter apply · Esc cancel",
                self.style("muted"),
                box_width,
            )
            for row, adapter in enumerate(adapters[offset : offset + visible]):
                index = offset + row
                checked = "x" if adapter.name in pending else " "
                marker = "›" if index == selected else " "
                self.add(
                    5 + row,
                    left,
                    f"{marker} [{checked}] {adapter.label}",
                    self.style(adapter.name, curses.A_BOLD, selected=index == selected),
                    box_width,
                )
            summary = tool_filter_label(pending)
            self.add(
                min(height - 2, 5 + min(visible, len(adapters)) + 1),
                left,
                notice or f"Showing: {summary}",
                self.style("warning" if notice else "muted"),
                box_width,
            )
            self.screen.refresh()
            key = self.screen.get_wch()
            notice = ""
            if key == "\x1b":
                self.message = "Tool filter unchanged."
                return
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(adapters) - 1, selected + 1)
            elif key == " ":
                name = adapters[selected].name
                if name in pending:
                    pending.remove(name)
                else:
                    pending.add(name)
            elif key == "a":
                pending = {adapter.name for adapter in adapters}
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if not pending:
                    notice = "Choose at least one tool; press a to restore all tools."
                    continue
                self.tools = pending
                if self.directory and not any(
                    item.cwd == self.directory and item.tool in self.tools for item in self.sessions
                ):
                    self.directory = ""
                self.keep_selection(previous)
                self.message = f"Showing {tool_filter_label(self.tools)}."
                return

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

    def cycle_launch_tool(self) -> None:
        item = self.selected_target()
        if item is None:
            if self.selected_row() is not None:
                self.message = "Expand this group and choose a native session first."
            return
        before = active_launch_tool(item)
        cycle_session_launch_tool(item)
        selected = active_launch_tool(item)
        self.state.set_launch_tool(item, selected)
        if selected == before:
            self.message = (
                "Only one launch harness is available: this session has no readable "
                "transcript to copy across."
            )
        elif session_needs_bridge(item, selected):
            self.message = (
                f"Launching {tool_label(selected)}: a copy of this conversation will be "
                "created there, with tool calls summarised inline."
            )
        else:
            self.message = f"Launching {tool_label(selected)} for this session."

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
            if self.launch_config.custom_args_missing():
                self.message = (
                    "Custom launch mode has no arguments configured; provider defaults apply."
                )
            else:
                self.message = "Custom launch mode selected; arguments come from config.toml."
        else:
            self.message = "Safe launch mode: provider permission defaults apply."

    def directory_picker(self) -> None:
        counts: dict[str, int] = {}
        eligible = filtered_sessions(
            self.sessions,
            tools=self.tools,
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
                    self.project_focus = ""
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
                + ellipsize(
                    self.state.original_name(item)[0] or display_title(item), box_width - 14
                ),
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

    def focus_selected_project(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        if self.project_focus:
            previous = self.selected_id()
            self.project_focus = ""
            self.directory = ""
            self.keep_selection(previous)
            self.message = "Showing all projects."
            return
        previous = self.selected_id()
        self.project_focus = (
            row.project if row.project != "(unknown project)" else row.representative.cwd
        )
        if not self.project_focus:
            self.message = "This session has no known project directory."
            return
        self.directory = ""
        self.keep_selection(previous)
        self.message = f"Focused project: {short_path(self.project_focus)}. Press f to reset."

    def details(self) -> None:
        """Show inspectable lineage and native identity without putting IDs in rows."""
        row = self.selected_row()
        if row is None:
            return
        eligible = {item.key for item in self.current()}
        members = row.all_members
        lines = [
            "CONVERSATION DETAILS",
            f"Project    {row.project}",
            f"Thread     {row.title}",
            f"Status     {row.status}",
            f"Members    {len(members)} (shown {len(row.members)})",
            "Lineage / related threads",
            "",
        ]
        for item in members:
            filtered = " · filtered/hidden" if item.key not in eligible else ""
            prompt = ellipsize(clean_prompt(item.preview), max(1, self.screen.getmaxyx()[1] - 14))
            lines.extend(
                (
                    f"{conversation_status(item)} · {tool_label(item.tool)} · "
                    f"{exact_time(item.updated)}{filtered}",
                    f"Harness    {item.tool} · Native ID {item.session_id}",
                    f"Storage    {item.storage or '(unknown)'}",
                    f"Dates      {exact_time(item.created)} → {exact_time(item.updated)}",
                    f"Activity  {activity_label(item)}",
                    f"Prompt    {prompt}",
                    "",
                )
            )
        self._set_input_timeout(-1)
        try:
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            self.add(1, 2, lines[0], self.style("accent", curses.A_BOLD), width - 4)
            for index, line in enumerate(lines[1 : max(1, height - 3)], 2):
                line_style = (
                    "muted" if line.startswith(("Harness", "Storage", "Dates")) else "primary"
                )
                self.add(index, 2, line, self.style(line_style), width - 4)
            self.add(height - 2, 2, "Press any key to return", self.style("muted"), width - 4)
            self.screen.refresh()
            self.screen.get_wch()
        finally:
            if self.refresh is not None:
                self._set_input_timeout(100)

    def rename_selected(self) -> None:
        self.finish_refresh()
        item = self.selected_target()
        if item is None:
            if self.selected_row() is not None:
                self.message = "Expand this group and choose a native session first."
            return
        value = self.name_prompt(item)
        if value is None:
            return
        note = rename_session(self.state, item, value)
        self.state.apply(self.sessions)
        self.message = note or ("Name saved." if value else "Original name restored.")

    def toggle_hidden(self) -> None:
        self.finish_refresh()
        item = self.selected_target()
        if item is None:
            if self.selected_row() is not None:
                self.message = "Expand this group and choose a native session first."
            return
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
            ("Space/Right", "Expand a conversation or independent thread group"),
            ("Left", "Collapse the selected group"),
            ("i", "Show project, status, activity, lineage, native IDs, and storage"),
            ("f", "Focus the selected project; press again to show all projects"),
            ("↑/↓ or j/k", "Move through sessions"),
            ("PgUp/PgDn", "Move by a page; Home/End jump"),
            ("ACTIVITY", "xt yc zp = turns, compactions, semantic user prompts"),
            ("STARTED", "When the session began; UPDATED is its latest activity"),
            ("OPEN", "● running or Ⅱ paused in a live terminal"),
            ("Ctrl-F or /", "Enter search mode; command keys are then search text"),
            ("is:open", "Search syntax also supports tool:, dir:, name:, and origin:"),
            (
                "Tab",
                "Cycle single-tool presets: All → "
                + " → ".join(adapter.short_label for adapter in REGISTRY.adapters()),
            ),
            ("t", "Choose any combination of tools to show"),
            ("o", "Cycle Human → Cross → Agent → All origins"),
            ("v", "Cycle Visible → Hidden → Visible + hidden"),
            ("A", "Show every session: all origins, visible and hidden"),
            ("H", "Show every hidden session across all origins"),
            ("r", "Rename; an empty name restores the vendor/original title"),
            ("h", "Hide or unhide the selected native session (never deletes it)"),
            ("d", "Choose a directory from a searchable list"),
            ("s", "Sort by recent, title, directory, messages ↓, or open first"),
            ("p", "Cycle Safe → Dangerous → Custom launch mode"),
            ("Ctrl-R", "Refresh the session index"),
            ("x", "Cycle launch harness; the other one gets a bridged copy"),
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
        if self.refresh is not None:
            self._set_input_timeout(100)
        while True:
            self.apply_refresh()
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                if self.refresh is not None:
                    continue
                raise
            self.message = ""
            rows = self.view_rows()
            page = max(1, self.screen.getmaxyx()[0] - 12)

            if key in (curses.KEY_UP, "\x10") or (not self.searching and key == "k"):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, "\x0e") or (not self.searching and key == "j"):
                self.selected = min(max(0, len(rows) - 1), self.selected + 1)
            elif key == curses.KEY_PPAGE:
                self.selected = max(0, self.selected - page)
            elif key == curses.KEY_NPAGE:
                self.selected = min(max(0, len(rows) - 1), self.selected + page)
            elif key == curses.KEY_HOME or (not self.searching and key == "g"):
                self.selected = 0
            elif key == curses.KEY_END or (not self.searching and key == "G"):
                self.selected = max(0, len(rows) - 1)
            elif not self.searching and key in (" ", curses.KEY_RIGHT):
                if not self.toggle_expansion():
                    self.message = "This row has no expandable members."
            elif not self.searching and key == curses.KEY_LEFT:
                row = self.selected_row()
                if row is not None and "/member:" in row.row_id:
                    parent_id = row.row_id.split("/member:", 1)[0]
                    self.expanded.discard(parent_id)
                    self.keep_selection(parent_id)
                elif not self.toggle_expansion(collapse=True):
                    self.message = "This row is not expanded."
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.finish_refresh()
                rows = self.view_rows()
                if rows:
                    chosen_row = rows[self.selected]
                    chosen = chosen_row.target
                    if chosen is None:
                        if chosen_row.expandable:
                            self.expanded.add(chosen_row.row_id)
                            self.keep_selection(chosen_row.row_id)
                            self.message = "Group expanded; choose a native session."
                        else:
                            self.message = "No native session is available for this row."
                        continue
                    if chosen.is_open:
                        focused, message = focus_open_session(chosen, self.launch_config)
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
                self.run_modal(self.help)
            elif not self.searching and key == "t":
                self.run_modal(self.tool_picker)
            elif not self.searching and key == "d":
                self.run_modal(self.directory_picker)
            elif not self.searching and key == "s":
                self.cycle_sort()
            elif not self.searching and key == "p":
                self.cycle_launch_mode()
            elif not self.searching and key == "o":
                self.cycle_origin()
            elif not self.searching and key == "v":
                self.cycle_visibility()
            elif not self.searching and key == "x":
                self.cycle_launch_tool()
            elif not self.searching and key == "i":
                self.run_modal(self.details)
            elif not self.searching and key == "f":
                self.focus_selected_project()
            elif not self.searching and key == "r":
                self.rename_selected()
            elif not self.searching and key == "h":
                self.toggle_hidden()
            elif not self.searching and key == "A":
                self.tool = "all"
                self.origin = "all"
                self.visibility = "all"
                self.directory = ""
                self.project_focus = ""
                self.query = ""
                self.keep_selection()
            elif not self.searching and key == "H":
                self.tool = "all"
                self.origin = "all"
                self.visibility = "hidden"
                self.directory = ""
                self.project_focus = ""
                self.query = ""
                self.keep_selection()
            elif not self.searching and key == "a":
                # Kept as a quick v1-compatible All/Human toggle.
                previous = self.selected_id()
                self.origin = "all" if self.origin != "all" else "human"
                self.keep_selection(previous)
            elif key == "\x12":  # Ctrl-R
                self.begin_refresh()
            elif key == "\x06" or (not self.searching and key == "/"):  # Ctrl-F or /
                self.searching = True
            elif isinstance(key, str) and key.isprintable():
                previous = self.selected_id()
                self.searching = True
                self.query += key
                self.keep_selection(previous)


def custom_mode_notice(config: LaunchConfig, provider: str | None = None) -> str:
    """Explain why custom launch mode is adding nothing to the provider command."""
    if provider is not None:
        provider_label = tool_label(provider)
        setting = (
            f"launch.custom.{provider}_args"
            if provider in ("claude", "codex")
            else f"[launch.providers.{provider}] custom_args"
        )
        return (
            f"sessions: custom launch mode has no arguments configured for {provider_label}, "
            f"so it starts with provider defaults; set {setting} in {config.path}"
        )
    return (
        "sessions: custom launch mode has no arguments configured, so sessions "
        "start with provider defaults; set launch.custom.claude_args or "
        f"launch.custom.codex_args in {config.path}"
    )


def command_for(session: Session, config: LaunchConfig) -> list[str]:
    """Build the resume command; the session must already resume in that tool.

    Storage quirks such as subagent parents and non-interactive visibility
    describe how the *recording* provider filed the session, so they apply
    only when resuming there.  A bridged copy is an ordinary new session.
    """
    tool = active_launch_tool(session)
    if session.conversation_id and tool != session.tool:
        raise BridgeError(
            f"{tool_label(tool)} cannot trust a heuristic counterpart for tracked conversation "
            f"{session.conversation_id[:8]}; resolve and bridge the conversation first"
        )
    if session_needs_bridge(session, tool):
        raise BridgeError(
            f"{tool_label(tool)} cannot resume a "
            f"{tool_label(session.tool)} session id directly; "
            "bridge a copy across first"
        )
    native = tool == session.tool
    try:
        adapter = REGISTRY.get(tool)
    except KeyError:
        raise BridgeError(f"unknown harness: {tool}") from None
    resume = adapter.resume_args
    if isinstance(resume, Unsupported):
        raise BridgeError(f"{adapter.label} cannot resume sessions: {resume.reason}")
    try:
        source = SourceKind(session.source)
    except ValueError:
        raise BridgeError(f"invalid source kind for {session.tool}: {session.source!r}") from None
    try:
        source_adapter = REGISTRY.get(session.tool)
    except KeyError:
        raise BridgeError(f"unknown recording harness: {session.tool}") from None
    if source not in source_adapter.source_kinds:
        raise BridgeError(f"{session.tool} does not declare source kind {source.value!r}")
    argv = config.provider_prefix(tool)
    argv += resume(
        session_id=session.launch_target(tool),
        source=source,
        resume_id=session.resume_target,
        parent_id=session.parent_id,
        native=native,
    )
    return argv


def prepare_launch(
    session: Session,
    *,
    state: UserState | None = None,
    config: LaunchConfig | None = None,
    tool_calls: bool = True,
    latest_window: bool = True,
) -> tuple[Session, str]:
    """Ensure the selected harness has something to resume; return a note.

    Reuses a previously bridged copy while the source has not moved on, so
    repeatedly launching the same pairing continues one conversation rather
    than spawning a new snapshot each time.
    """
    tool = active_launch_tool(session)
    conversation_id = session.conversation_id
    if state is not None:
        source, target, conversation_id = state.resolve_launch(session, tool)
        if target is not None:
            target.launch_tool = ""
            if target.key == session.key:
                return target, ""
            return target, (
                f"Continuing the {tool_label(tool)} copy—the current materialization of "
                f"conversation {conversation_id[:8]} ({target.session_id})."
            )
        session = source
        session.launch_tool = tool
    if tool == session.tool:
        return session, ""
    stale = ""
    # Transcript-derived launch targets are only legacy counterpart hints. A
    # tracked conversation's members are authoritative, so never let an
    # unrelated hint override the head resolution above.
    recorded = session.launch_target(tool) if not conversation_id else ""
    if recorded:
        if native_session_exists(tool, recorded):
            return session, ""
        # The reference was matched out of transcript text, so it may never
        # have been a session at all.  Fall through and make a real one.
        stale = (
            f"The recorded {tool_label(tool)} counterpart {recorded} does not exist; "
            "copying the conversation across instead. "
        )

    def attach(session_id: str, storage: str = "") -> Session:
        if storage:
            return replace(
                session,
                tool=tool,
                session_id=session_id,
                storage=storage,
                source="interactive",
                auxiliary=False,
                origin="cross",
                resume_id=session_id,
                parent_id="",
                launch_targets={tool: session_id},
                launch_tool="",
            )
        return replace(
            session,
            launch_targets={**session.launch_targets, tool: session_id},
            launch_tool=tool,
        )

    if state is not None and not conversation_id:
        existing = state.bridge_for(session, tool)
        if existing:
            return attach(existing), (
                f"{stale}Continuing the {tool_label(tool)} copy of this session ({existing})."
            )
        conversation_id = state.conversation_id_for(session, create=True)
    target_cwd = strip_extended_prefix(session.cwd) or str(HOME)
    target_command = tuple(
        config.provider_command(tool) if config is not None else REGISTRY.get(tool).default_command
    )
    prepared = prepare_target(
        tool,
        command=target_command,
        cwd=target_cwd,
        options=config.provider_options(tool) if config is not None else None,
    )
    budget = resolve_budget(
        tool,
        max_tokens=config.bridge_max_tokens if config is not None else None,
        max_chars=config.bridge_max_chars if config is not None else None,
        migrated=config.bridge_max_chars_migrated if config is not None else False,
        policy=prepared.budget_policy,
    )
    result = bridge(
        source_tool=session.tool,
        target_tool=tool,
        session_id=session.session_id,
        storage=session.storage,
        cwd=target_cwd,
        title=session.title,
        budget=budget,
        prepared_target=prepared,
        target_command=target_command,
        tool_calls=tool_calls,
        latest_window=latest_window,
        conversation_id=conversation_id,
        allow_lossy=config.bridge_allow_lossy if config is not None else False,
    )
    if state is not None:
        state.set_bridge(
            session,
            tool,
            result.session_id,
            result.storage,
            source_checkpoint=result.source_checkpoint,
            target_checkpoint=result.target_checkpoint,
        )
    carried = (
        f"{result.turns} source message(s), assembled as {result.written_turns} target message(s)"
    )
    if result.calls:
        carried += f" and {result.calls} summarised tool call(s)"
    dropped = f", {result.dropped} older message(s) dropped to fit" if result.dropped else ""
    truncated = (
        f", {result.truncated} anchor message(s) truncated to fit" if result.truncated else ""
    )
    budget_notice = (
        f" Applied bridge budget: approximately {result.budget.tokens:,} tokens / "
        f"{result.budget.chars:,} projected characters ({result.budget.origin})."
    )
    if result.budget.clamped:
        budget_notice += " The configured token budget was raised to the safe bridge minimum."
    if result.budget.origin == "target-default":
        budget_notice += (
            " This target policy replaces the previous global 950,000-character ceiling; "
            "set bridge.max_tokens to choose a different ceiling."
        )
    elif result.budget.origin == "target-default-migrated":
        budget_notice += " Migrated the legacy 950,000-character default to target policy."
    elif result.budget.over_policy:
        budget_notice += (
            " The legacy max_chars override exceeds target policy; delete it or set "
            "max_tokens to change this."
        )
    note = (
        f"{stale}Copied {carried} into a new {tool_label(tool)} session "
        f"{result.session_id}{dropped}{truncated}.{budget_notice}"
    )
    if result.notices:
        note += " " + " ".join(result.notices)
    return attach(result.session_id, result.storage), note


def launch(
    session: Session,
    config: LaunchConfig,
    dry_run: bool = False,
    state: UserState | None = None,
) -> int:
    # A dry run still bridges, because the point of printing the command is
    # that it can be pasted and run, and it cannot name a copy that does not
    # exist yet.  Writing the copy is inert until something resumes it.
    try:
        session, note = prepare_launch(
            session,
            state=state,
            config=config,
            tool_calls=config.bridge_tool_calls,
            latest_window=config.bridge_latest_window,
        )
    except BridgeError as error:
        print(f"sessions: {error}", file=sys.stderr)
        return 2
    if note:
        print(f"sessions: {note}", file=sys.stderr)
    argv = command_for(session, config)
    cwd = strip_extended_prefix(session.cwd) or str(HOME)
    if config.custom_args_missing(active_launch_tool(session)):
        print(custom_mode_notice(config, active_launch_tool(session)), file=sys.stderr)
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
            # npm ships claude/codex as .cmd shims, and CreateProcess only ever
            # appends .exe when searching PATH -- it does not honour PATHEXT.
            # Resolve the shim the way the shell would before handing it over,
            # or the launch fails with WinError 2 for a command that plainly
            # works when typed.
            executable = shutil.which(argv[0])
            if executable is None:
                print(f"sessions: {argv[0]!r} is not on PATH", file=sys.stderr)
                return 127
            return subprocess.call([executable, *argv[1:]])
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
    conversation_width = 17
    activity_width = 18
    tool_width = tool_column_width(8)
    run_width = tool_column_width(6)
    title_width = max(32, terminal_width - 124 - (tool_width - 8) - (run_width - 6))
    title_suffixes = title_disambiguators(items)
    print(
        f"{'TOOL':<{tool_width}} {'RUN':<{run_width}} {'ORIGIN':<7} "
        f"{'OPEN':<5} {'ACTIVITY':<{activity_width}} {'STATE':<8} "
        f"{'HEAD / STATUS':<{conversation_width}} {'STARTED':<12} {'UPDATED':<12} "
        f"{'TITLE':<{title_width}} DIRECTORY"
    )
    for item in items:
        paused = item.is_open and process_state(item.open_pid) in ("T", "t")
        open_symbol = "Ⅱ" if paused else ("●" if item.is_open else "")
        rendered_title = ellipsize_with_suffix(
            display_list_title(item), title_suffixes.get(item.key, ""), title_width
        )
        print(
            f"{tool_label(item.tool):<{tool_width}} "
            f"{tool_label(active_launch_tool(item)):<{run_width}} "
            f"{ORIGIN_LABELS[item.origin]:<7} {open_symbol:<5} "
            f"{activity_label(item):<{activity_width}} "
            f"{('hidden' if item.hidden else 'visible'):<8} "
            f"{conversation_status(item):<{conversation_width}} "
            f"{relative_time(item.created):<12} {relative_time(item.updated):<12} "
            f"{rendered_title:<{title_width}} "
            f"{short_path(item.cwd)}"
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
            x switches launch harness for the selected session, Ctrl-F starts
            search, r renames, h hides/unhides, q quits, and ? shows all
            shortcuts.
            """
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="print matching sessions instead of opening the browser"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON (implies --list)")
    parser.add_argument("--tool", choices=tool_order(), default="all", help="initial tool filter")
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
        "--no-cache",
        action="store_true",
        help="rebuild provider metadata without discovery caches",
    )
    parser.add_argument(
        "--resume", metavar="ID_OR_NAME", help="resume an exact session ID or named session"
    )
    parser.add_argument(
        "--launch-tool",
        choices=REGISTRY.names(),
        help=(
            "resume in this harness instead of the recording one; a session from the "
            "other CLI is copied across first"
        ),
    )
    parser.add_argument(
        "--rename",
        nargs=2,
        metavar=("ID_OR_NAME", "NEW_NAME"),
        help="rename a session in Claude Code or Codex too; empty name resets it",
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
        if launch_config.custom_args_missing():
            print(custom_mode_notice(launch_config), file=sys.stderr)
        return 0
    if args.launch_mode:
        launch_config.mode = args.launch_mode
    tui_mode = bool(
        not (args.list or args.json or args.resume or args.rename or args.hide or args.unhide)
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    background_refresh: SessionRefresh | None = None
    if tui_mode:
        sessions = [] if args.no_cache else load_session_catalog(state)
        background_refresh = SessionRefresh(
            use_cache=not args.no_cache,
            state=state,
            config=launch_config,
        )
    else:
        sessions = load_sessions(
            use_cache=not args.no_cache,
            state=state,
            config=launch_config,
        )
        save_session_catalog(sessions)
        for note in load_warnings():
            print(f"sessions: {note}", file=sys.stderr)

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
                f"sessions: {target!r} matches more than one record; use the exact session ID "
                "shown in the browser detail or by --json",
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
        note = rename_session(state, item, new_name)
        state.apply(sessions)
        print(f"Session name: {display_title(item)}")
        if note:
            print(f"sessions: {note}", file=sys.stderr)
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
        if args.launch_tool and not supports_launch_tool(item, args.launch_tool):
            tools = ", ".join(sorted({tool_label(tool) for tool in available_launch_tools(item)}))
            print(
                f"sessions: --launch-tool={args.launch_tool} is not available for "
                f"that session (available: {tools})",
                file=sys.stderr,
            )
            return 2
        if args.launch_tool:
            item = replace(item, launch_tool=args.launch_tool)
        return launch(item, launch_config, dry_run=args.dry_run, state=state)

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
            refresh=background_refresh,
        )
        browser.tool = args.tool
        browser.origin = selected_origin
        browser.visibility = args.visibility
        browser.directory = directory
        browser.query = args.query
        browser.sort_mode = args.sort
        # stderr is unusable once curses owns the screen, so the same notes
        # ride in on the status line instead of vanishing.
        browser.message = "; ".join(load_warnings())
        return browser.run()

    selected = curses.wrapper(wrapped)
    if selected:
        return launch(selected, launch_config, dry_run=args.dry_run, state=state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
