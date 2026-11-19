"""OpenCode bound-database deletion, without invoking its CLI or shared daemon."""

import os
import sqlite3
from pathlib import Path

from ..deletion import DeletePlan, DeletionError
from ..model import NativeRef
from ..registry import REGISTRY
from .deletion_storage import checked_database, scoped_database


def resolve_delete(session_id):
    """Use a proven binding or the explicit OPENCODE_DB setting; never probe CLI."""
    from . import opencode

    home = REGISTRY.get("opencode").home.absolute()
    with opencode._BINDING_LOCK:
        binding = opencode._LAST_BINDING
    explicit = os.environ.get("OPENCODE_DB", "")
    if explicit:
        if "\0" in explicit or explicit.casefold().startswith(("file:", ":memory:")):
            raise DeletionError("OpenCode explicit database path is not persistent")
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = home / path
        # An explicit native database override owns only this exact DB, not an
        # arbitrary transcript/catalog path. Its parent is the storage boundary.
        boundary = path.parent
        if binding is not None and binding.authoritative and binding.path != path:
            raise DeletionError("OpenCode explicit database conflicts with authoritative discovery")
    elif binding is not None and binding.authoritative:
        path = binding.path
        boundary = home
    else:
        return None
    checked_database(boundary, path)
    if not path.is_file():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        opencode._schema_columns(connection, semantic=True)
        present = connection.execute("SELECT id FROM session WHERE id=?", (session_id,)).fetchone()
    return NativeRef(session_id, str(path)) if present and present[0] == session_id else None


def plan_delete(ref):
    adapter = REGISTRY.get("opencode")
    home = adapter.home.absolute()
    if resolve_delete(ref.session_id) != ref:
        raise DeletionError("OpenCode authoritative database binding is unavailable; refresh first")
    if os.environ.get("OPENCODE_DB"):
        home = Path(ref.storage).parent
    checked_database(home, Path(ref.storage))
    # Legacy JSON stores may hold an additional copy outside the bound DB.
    if (home / "storage").exists():
        raise DeletionError("OpenCode legacy filesystem storage requires further deletion review")
    path = Path(ref.storage)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    guards = []
    if "event" in tables:
        # aggregate_id is the native session aggregate. A payload that disagrees
        # must not cause either a cross-session delete or orphaned selected history.
        ownership = ("$.sessionID", "$.info.sessionID", "$.part.sessionID")
        conflict = " OR ".join(
            f"(json_extract(data,'{key}') IS NOT NULL AND json_extract(data,'{key}')<>?)"
            for key in ownership
        )
        stray = " OR ".join(f"json_extract(data,'{key}')=?" for key in ownership)
        query = (
            "SELECT id FROM {schema}.event WHERE CASE WHEN json_valid(data) THEN "
            f"(aggregate_id=? AND (json_extract(data,'$.sessionID') IS NOT ? OR {conflict})) "
            f"OR (aggregate_id<>? AND ({stray})) ELSE aggregate_id=? END"
        )
        guards.append(
            (
                query,
                (ref.session_id,) * 10,
                "OpenCode event payload/aggregate ownership is inconsistent",
            )
        )
    db = scoped_database(
        home,
        path,
        ref.session_id,
        root="session",
        scopes=(
            ("event", "aggregate_id"),
            ("event_sequence", "aggregate_id"),
            ("part", "session_id"),
            ("message", "session_id"),
            ("session_context_epoch", "session_id"),
            ("session_input", "session_id"),
            ("session_message", "session_id"),
            ("todo", "session_id"),
            ("session_share", "session_id"),
        ),
        neutral=(
            "project",
            "workspace",
            "data_migration",
            "migration",
            "__drizzle_migrations",
            "account_state",
            "account",
            "control_account",
            "credential",
            "permission",
            "project_directory",
        ),
        child_column="parent_id",
        guards=tuple(guards),
    )
    return DeletePlan("opencode", ref, adapter.checkpoint(ref), (), (db,))
