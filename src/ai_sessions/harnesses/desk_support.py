"""Native provider details for the project conversation launcher."""

from __future__ import annotations

import uuid

from ..capabilities import Unsupported

TOOLS = ("claude", "codex", "grok")
DEFAULT_TOOL = "claude"


def arguments(tool, session_id, *, fresh, prompt):
    if tool == "claude":
        return (
            ["--session-id", session_id, "--name", "Project conversation", prompt]
            if fresh
            else ["--resume", session_id, prompt]
        )
    if tool == "codex":
        return ["resume", session_id, prompt]
    if tool == "grok":
        return ["--session-id", session_id, prompt] if fresh else ["--resume", session_id, prompt]
    raise ValueError("Project conversations support Claude Code or Codex")


def missing_can_start(tool):
    return tool in ("claude", "grok")


def create_id(adapter, folder, name):
    if adapter.name in ("claude", "grok"):
        return str(uuid.uuid4())
    if isinstance(adapter.create_session, Unsupported):
        raise ValueError("This harness cannot create a persistent project conversation")
    return adapter.create_session(cwd=str(folder), name=f"Project: {name}").session_id
