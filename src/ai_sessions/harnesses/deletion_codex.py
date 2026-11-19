"""Native hard-delete for modern Codex; conservative legacy storage scopes."""

import re
import sqlite3

from ..deletion import DeletePlan, DeletionError, FileChange, checked_path
from ..registry import REGISTRY
from .deletion_storage import checked_database, json_lines, scoped_database


def plan_delete(ref):
    from . import codex

    adapter = REGISTRY.get("codex")
    home = adapter.home.absolute()
    ident = ref.session_id
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", ident) or adapter.resolve(ident) != ref:
        raise DeletionError("Codex authoritative identity changed or is invalid")
    # Never reuse the provider's short-lived rollout listing for a destructive
    # operation: include every current plain/compressed copy of this identity.
    paths = sorted(
        set((home / "sessions").rglob(f"rollout-*-{ident}.jsonl"))
        | set((home / "sessions").rglob(f"rollout-*-{ident}.jsonl.zst"))
    )
    # Archived rollouts are another native location, never discovered via an
    # arbitrary Session.storage value.
    paths += sorted((home / "archived_sessions").glob(f"*{ident}.jsonl*"))
    files = []
    paginated = False
    for path in paths:
        checked_path(home, path)
        if not path.name.endswith((f"{ident}.jsonl", f"{ident}.jsonl.zst")):
            raise DeletionError("unrecognized Codex rollout filename")
        meta = codex._rollout_meta(path)
        if meta.get("id") != ident:
            raise DeletionError("Codex rollout native ID mismatch")
        if meta.get("history_mode") == codex.PAGINATED_HISTORY_MODE:
            paginated = True
        files.append(FileChange.capture(home, path))
    if not files:
        raise DeletionError("Codex has no verified rollout history")
    # New Codex installs retain projections and other thread-owned data even
    # for old legacy rollouts. Let Codex clean its own evolving stores. The
    # manual legacy path is only for homes without these newer stores.
    modern_stores = any(
        any(home.glob(pattern))
        for pattern in (
            "thread_history_*.sqlite",
            "goals_*.sqlite",
            "memories_*.sqlite",
            "queue_*.sqlite",
        )
    )
    if paginated or modern_stores:
        return DeletePlan(
            "codex",
            ref,
            adapter.checkpoint(ref),
            tuple(files),
            native_delete=True,
            notes=(
                "Provider operation: Codex thread/delete for this exact native ID.",
                "Codex owns associated rollouts, paginated history projections, diagnostics and durable thread data; these are not manually rewritten.",
                "Codex checks native writer locks and dependent fork references before deletion. A native failure can be partial and is never automatically retried.",
            ),
        )
    databases = []
    state_paths = sorted(home.glob("state_*.sqlite"))
    if len(state_paths) > 1:
        raise DeletionError(
            "multiple Codex state generations require explicit migration ownership review"
        )
    for path in state_paths:
        checked_database(home, path)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            tables = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        guards = []
        if "history_mode" in columns:
            guards.append(
                (
                    "SELECT id FROM {schema}.threads WHERE id=? AND history_mode='paginated'",
                    (ident,),
                    "Codex paginated history deletion requires further schema review",
                )
            )
        if "rollout_path" in columns:
            accepted = tuple(str(path) for path in paths) + tuple(
                str(path).removesuffix(".zst") for path in paths
            )
            markers = ",".join("?" for _ in accepted)
            guards.append(
                (
                    f"SELECT id FROM {{schema}}.threads WHERE id=? AND rollout_path NOT IN ({markers})",
                    (ident, *accepted),
                    "Codex indexed rollout path does not match verified native history",
                )
            )
        for column in ("parent_thread_id", "attached_thread_parent_id"):
            if column in columns:
                guards.append(
                    (
                        f'SELECT id FROM {{schema}}.threads WHERE "{column}"=? AND id<>?',
                        (ident, ident),
                        "unselected Codex child/fork depends on this session",
                    )
                )
        if "thread_spawn_edges" in tables:
            guards.append(
                (
                    "SELECT child_thread_id FROM {schema}.thread_spawn_edges WHERE parent_thread_id=? AND child_thread_id<>?",
                    (ident, ident),
                    "unselected Codex spawn child depends on this session",
                )
            )
        additional = []
        if "rollout_migration_skipped_rollouts" in tables:
            for rollout in sorted(set(str(path) for path in paths)):
                additional.append(
                    (
                        "DELETE FROM {schema}.rollout_migration_skipped_rollouts WHERE rollout_path=?",
                        (rollout,),
                    )
                )
        databases.append(
            scoped_database(
                home,
                path,
                ident,
                root="threads",
                root_required=False,
                scopes=(
                    ("thread_dynamic_tools", "thread_id"),
                    ("thread_attachments", "thread_id"),
                    ("guardian_review_feedback", "thread_id"),
                    ("thread_read_receipts", "thread_id"),
                    ("thread_spawn_edges", "child_thread_id"),
                ),
                neutral=(
                    "_sqlx_migrations",
                    "backfill_state",
                    "remote_control_enrollments",
                    "external_agent_config_imports",
                    "thread_sections",
                    "rollout_migration_state",
                    "rollout_migration_skipped_rollouts",
                    "projects",
                    "project_roots",
                    "project_idempotency_keys",
                ),
                guards=tuple(guards),
                additional=tuple(additional),
            )
        )
    for path in sorted(home.glob("logs_*.sqlite")):
        # process_uuid is shared by multiple conversations. Never broaden the
        # predicate to a PID/process/time range, or delete threadless rows.
        databases.append(
            scoped_database(
                home,
                path,
                ident,
                root="logs",
                root_column="thread_id",
                root_required=False,
                scopes=(),
                neutral=("_sqlx_migrations",),
                guards=(
                    (
                        "SELECT id FROM {schema}.logs WHERE (thread_id IS NULL OR thread_id='') AND instr(coalesce(feedback_log_body,''),?)>0",
                        (ident,),
                        "Codex shared threadless diagnostics mention this ID but have no authoritative session owner",
                    ),
                ),
            )
        )
    files.extend(
        (
            json_lines(home, home / "history.jsonl", ident, ("session_id",)),
            json_lines(home, home / "session_index.jsonl", ident, ("id",)),
        )
    )
    return DeletePlan(
        "codex",
        ref,
        adapter.checkpoint(ref),
        tuple(files),
        tuple(databases),
        notes=(
            "Diagnostics are removed only where thread_id exactly matches; shared threadless diagnostics and unrelated process history are preserved.",
        ),
    )
