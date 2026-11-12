"""Track successful native launches in a managed workspace shell."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar

from ..paths import STATE_FILE, env_path
from ..registry import REGISTRY
from .store import WorkspaceStore

_active = ContextVar("workspace_launch", default=None)


def pane_assignment():
    name = os.environ.get("AI_SESSIONS_WORKSPACE", "")
    slot = os.environ.get("AI_SESSIONS_WORKSPACE_PANE", "")
    terminal = os.environ.get("WT_SESSION", "")
    path = env_path("AI_SESSIONS_WORKSPACE_FILE", STATE_FILE.with_suffix(".workspaces.sqlite3"))
    if terminal and path.exists():
        store = WorkspaceStore(path)
        assigned = store.terminal_slot(terminal)
        if assigned:
            return store, *assigned
    if name or slot:
        if not name or not slot:
            raise ValueError("Workspace and pane must both be set in this shell.")
        return WorkspaceStore(), name, slot
    return None


def is_managed_pane():
    return (
        bool(
            os.environ.get("AI_SESSIONS_WORKSPACE") or os.environ.get("AI_SESSIONS_WORKSPACE_PANE")
        )
        or pane_assignment() is not None
    )


def _binding(session, tool):
    adapter = REGISTRY.get(tool)
    home = adapter.home
    if not home:
        raise ValueError("The active harness does not expose its home; workspace launch refused.")
    target = session.launch_target(tool)
    storage = session.storage
    if tool != session.tool:
        from ..capabilities import Unsupported

        if not target or isinstance(adapter.resolve, Unsupported):
            raise ValueError("Cannot resolve the target native session for this workspace pane.")
        ref = adapter.resolve(target)
        if ref is None or ref.session_id != target:
            raise ValueError("Cannot resolve the target native session for this workspace pane.")
        storage = ref.storage
    return {
        "tool": tool,
        "session_id": target or session.session_id,
        "home": str(home),
        "title": str(session.title or ""),
        "cwd": str(session.cwd or ""),
        "storage": str(storage or ""),
    }


@contextmanager
def launch_slot(session, tool):
    assigned = pane_assignment()
    if assigned is None:
        yield
        return
    import psutil

    store, name, slot = assigned
    binding = _binding(session, tool)
    lease = store.acquire(name, slot, binding, os.getpid(), psutil.Process().create_time())
    context = _active.set((store, name, slot, binding))
    try:
        yield
    finally:
        _active.reset(context)
        try:
            store.release(lease)
        except (OSError, ValueError) as exc:
            print(f"sessions: could not release workspace lease: {exc}", file=sys.stderr)


def remember_launch(session, tool):
    active = _active.get()
    if active is None:
        return
    store, name, slot, binding = active
    try:
        store.bind(name, slot, binding, move=True)
    except Exception as exc:
        # The child is already alive. Keep waiting for it so containment and
        # restart handling remain intact, while making the persistence failure visible.
        print(f"sessions: could not save workspace pane assignment: {exc}", file=sys.stderr)
