"""Track successful native launches in a managed workspace shell."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar

from ..registry import REGISTRY
from .store import WorkspaceStore

_active = ContextVar("workspace_launch", default=None)


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
    name = os.environ.get("AI_SESSIONS_WORKSPACE", "")
    slot = os.environ.get("AI_SESSIONS_WORKSPACE_PANE", "")
    if not name and not slot:
        yield
        return
    if not name or not slot:
        raise ValueError("Workspace and pane must both be set in this shell.")
    import psutil

    store = WorkspaceStore()
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
