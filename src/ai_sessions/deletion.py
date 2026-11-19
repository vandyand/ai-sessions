"""Confirmed permanent native deletion. Providers alone define owned resources.

Entrypoint: main([reference, '--all-copies', '--dry-run']). No force/yes option.
No agent turns, session stops, peer messages or background cleanup belong here.
Providers may own a bounded local lifecycle operation after typed confirmation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .capabilities import Unsupported
from .model import NativeRef
from .registry import REGISTRY


class DeletionError(ValueError):
    pass


def checked_path(home: Path, path: Path) -> Path:
    """Reject links, junctions, hard links, escapes and non-regular resources."""
    home, path = home.absolute(), path.absolute()
    try:
        path.relative_to(home)
    except ValueError:
        raise DeletionError(f"path escapes provider home: {path}") from None
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise DeletionError(f"linked path is unsafe: {part}")
    if not path.resolve().is_relative_to(home.resolve()):
        raise DeletionError(f"resolved path escapes provider home: {path}")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeletionError(f"resource is not an exclusively owned regular file: {path}")
    return path


def file_identity(path: Path) -> str:
    if not path.exists():
        return "absent"
    info = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DeletionError(f"resource changed while reading: {path}")
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{digest.hexdigest()}"


@dataclass(frozen=True)
class FileChange:
    home: Path
    path: Path
    before: str
    replacement: bytes | None = None  # None means remove; bytes means scoped metadata rewrite.
    merge_kind: str = ""

    @classmethod
    def capture(cls, home, path, replacement=None, merge_kind=""):
        path = checked_path(Path(home), Path(path))
        return cls(Path(home), path, file_identity(path), replacement, merge_kind)

    def validate(self):
        checked_path(self.home, self.path)
        if file_identity(self.path) != self.before:
            raise DeletionError(f"resource changed; rerun deletion preview: {self.path}")


@dataclass(frozen=True)
class DatabaseChange:
    home: Path
    path: Path
    fingerprint: str
    statements: tuple[tuple[str, tuple], ...]
    identity: tuple[int, int] = ()
    queries: tuple[tuple[str, tuple], ...] = ()


def database_fingerprint(connection, schema="main", queries=()):
    """Hash schema plus owned rows; unrelated shared-log appends are permitted."""
    digest = hashlib.sha256()
    schema = '"' + schema.replace('"', '""') + '"'
    definitions = connection.execute(
        f"SELECT type,name,tbl_name,sql FROM {schema}.sqlite_master ORDER BY type,name"
    ).fetchall()
    digest.update(repr(definitions).encode())
    if not queries:
        queries = tuple(
            (f'SELECT * FROM {{schema}}."{name.replace(chr(34), chr(34) * 2)}"', ())
            for kind, name, _, _ in definitions
            if kind == "table"
        )
    for query, parameters in queries:
        digest.update(repr((query, parameters)).encode())
        rows = connection.execute(query.replace("{schema}", schema), parameters)
        for row in sorted(repr(tuple(row)) for row in rows):
            digest.update(row.encode())
            digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class DeletePlan:
    tool: str
    native: NativeRef
    checkpoint: object
    files: tuple[FileChange, ...]
    databases: tuple[DatabaseChange, ...] = ()
    notes: tuple[str, ...] = ()
    # Native files are identity guards, never manual delete instructions.
    # The provider executor must acquire native writer ownership.
    native_delete: bool = False

    @property
    def fingerprint(self):
        return hashlib.sha256(repr(self).encode()).hexdigest()


def combine_plans(plans, cleanup=()):
    """Compose shared metadata removals before any selected copy is touched."""
    if any(plan.native_delete for plan in plans):
        raise DeletionError("native operations cannot be combined into manual storage deletion")
    files = {}
    databases = {}
    for plan in plans:
        for change in plan.files:
            previous = files.get(change.path)
            if previous is None:
                files[change.path] = change
                continue
            if previous.before != change.before or previous.home != change.home:
                raise DeletionError("shared native metadata changed while planning")
            if previous.replacement == change.replacement:
                continue
            if not previous.merge_kind or previous.merge_kind != change.merge_kind:
                raise DeletionError(
                    "overlapping native deletion resources cannot be safely composed"
                )
            if change.merge_kind == "jsonl":
                retained = set(change.replacement.splitlines(keepends=True))
                merged = b"".join(
                    line
                    for line in previous.replacement.splitlines(keepends=True)
                    if line in retained
                )
            elif change.merge_kind == "entries":
                left, right = json.loads(previous.replacement), json.loads(change.replacement)
                left["entries"] = [row for row in left["entries"] if row in right["entries"]]
                merged = json.dumps(left, ensure_ascii=False).encode()
            else:
                raise DeletionError("unsupported metadata composition")
            files[change.path] = replace(previous, replacement=merged)
        for db in plan.databases:
            previous = databases.get(db.path)
            if previous is not None:
                if previous.home != db.home or previous.identity != db.identity:
                    raise DeletionError("shared native database changed while planning")
                if previous != db:
                    checked_path(db.home, db.path)
                    with sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True) as connection:
                        connection.execute("BEGIN")
                        for checkpoint in (previous, db):
                            if (
                                database_fingerprint(connection, queries=checkpoint.queries)
                                != checkpoint.fingerprint
                            ):
                                raise DeletionError(
                                    "shared native database scope changed while planning"
                                )
                        queries = previous.queries + db.queries
                        db = replace(
                            previous,
                            statements=previous.statements + db.statements,
                            queries=queries,
                            fingerprint=database_fingerprint(connection, queries=queries),
                        )
            databases[db.path] = db
    for change in cleanup:
        if change.path in files:
            raise DeletionError("local state overlaps native resources")
        files[change.path] = change
    first = plans[0]
    return DeletePlan(
        first.tool,
        first.native,
        tuple(plan.fingerprint for plan in plans),
        tuple(files.values()),
        tuple(databases.values()),
    )


def commit_plan(plan: DeletePlan, *, revalidate=lambda: None):
    """Quarantine files and coordinate exact scopes in one attached-DB transaction.

    ATTACH coordinates rollback on normal failures, including WAL databases. It
    does not claim crash atomicity across multiple WAL files. After commit any
    recurrence/purge failure is explicitly partial and never rolls back DB history.
    """
    if plan.native_delete:
        raise DeletionError("native operations require the provider's deletion executor")
    connection = None
    schemas = []
    moved = []
    replacements = {}
    committed = False

    def verify_originals():
        for change, _ in moved:
            checked_path(change.home, change.path)
            expected = replacements.get(change.path, "absent")
            if file_identity(change.path) != expected:
                raise DeletionError(
                    f"native resource recreated/changed after quarantine: {change.path}"
                )
        for change in plan.files:
            if change.before == "absent":
                change.validate()

    try:
        for index, db in enumerate(plan.databases):
            checked_path(db.home, db.path)
            for suffix in ("-wal", "-shm", "-journal"):
                checked_path(db.home, Path(str(db.path) + suffix))
            info = db.path.stat()
            if db.identity and (info.st_dev, info.st_ino) != db.identity:
                raise DeletionError("native database file identity changed")
            if connection is None:
                connection = sqlite3.connect(db.path.as_uri() + "?mode=rw", uri=True, timeout=1)
                connection.execute("PRAGMA foreign_keys=ON")
                schemas.append("main")
            else:
                schema = f"deletion_{index}"
                connection.execute(
                    f'ATTACH DATABASE ? AS "{schema}"', (db.path.as_uri() + "?mode=rw",)
                )
                schemas.append(schema)
        if connection is not None:
            connection.execute("BEGIN IMMEDIATE")
            for schema, db in zip(schemas, plan.databases):
                if database_fingerprint(connection, schema, db.queries) != db.fingerprint:
                    raise DeletionError(f"native database changed in selected scope: {db.path}")
        revalidate()
        for change in plan.files:
            change.validate()
        for change in plan.files:
            if change.before == "absent":
                continue
            change.validate()
            quarantine = change.path.with_name(f".delete-{uuid.uuid4().hex[:16]}")
            os.rename(change.path, quarantine)
            moved.append((change, quarantine))
            if file_identity(quarantine) != change.before:
                raise DeletionError(f"resource changed while quarantining: {change.path}")
            if change.replacement is not None:
                with change.path.open("xb") as output:
                    output.write(change.replacement)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(change.path, stat.S_IMODE(quarantine.stat().st_mode))
                replacements[change.path] = file_identity(change.path)
        verify_originals()
        for change, quarantine in moved:
            if file_identity(quarantine) != change.before:
                raise DeletionError("quarantined native resource changed before commit")
        if connection is not None:
            for schema, db in zip(schemas, plan.databases):
                for sql, parameters in db.statements:
                    if schema != "main" and "{schema}" not in sql:
                        raise DeletionError(
                            "attached database statement lacks explicit schema scope"
                        )
                    connection.execute(sql.replace("{schema}", f'"{schema}"'), parameters)
                if connection.execute(f'PRAGMA "{schema}".foreign_key_check').fetchone():
                    raise DeletionError("native foreign key validation failed")
            verify_originals()
            connection.commit()
        committed = True
    except BaseException as error:
        failures = []
        if connection is not None:
            try:
                connection.rollback()
            except Exception as rollback_error:
                failures.append(
                    f"native database rollback failed; database state uncertain: {rollback_error}"
                )
        for change, quarantine in reversed(moved):
            try:
                if change.path.exists():
                    if (
                        change.path not in replacements
                        or file_identity(change.path) != replacements[change.path]
                    ):
                        raise DeletionError(f"concurrent recreation at {change.path}")
                    change.path.unlink()
                os.rename(quarantine, change.path)
            except (OSError, ValueError) as restore_error:
                failures.append(f"{quarantine} -> {change.path}: {restore_error}")
        if failures:
            raise DeletionError(
                f"PARTIAL rollback; recovery files: {'; '.join(failures)}"
            ) from error
        raise
    finally:
        if connection is not None:
            connection.close()
    if committed:
        try:
            verify_originals()
        except (OSError, ValueError) as error:
            recovery = ", ".join(str(path) for _, path in moved)
            raise DeletionError(
                f"PARTIAL deletion: native commit completed; {error}; quarantine retained: {recovery}"
            ) from error
        failures = []
        for change, quarantine in moved:
            try:
                checked_path(change.home, quarantine)
                if file_identity(quarantine) != change.before:
                    raise DeletionError("quarantine changed after native commit")
                quarantine.unlink()
            except (OSError, ValueError) as error:
                failures.append(f"{quarantine}: {error}")
        try:
            verify_originals()
        except (OSError, ValueError) as error:
            failures.append(str(error))
        if failures:
            raise DeletionError(
                "PARTIAL deletion: native commit completed, purge/recurrence failure: "
                + "; ".join(failures)
            )


def catalog():
    from .app import UserState, load_session_catalog

    state = UserState()
    # Catalog loading never launches native discovery commands. A missing catalog
    # must be refreshed in the browser first, rather than invoking a native CLI.
    return load_session_catalog(state), state


def deletion_snapshot():
    """Deletion-specific enumeration: never discard unreadable native processes."""
    import psutil

    from .liveness import LivenessContext, ProcessInfo, _wants_command, held_file_locks

    executables = frozenset(
        name for adapter in REGISTRY.adapters() for name in adapter.liveness_executables
    )
    rows = []
    try:
        for process in psutil.process_iter(["pid", "name"]):
            try:
                pid, name = int(process.info["pid"]), process.info.get("name")
                if not isinstance(name, str) or not name:
                    raise DeletionError(f"process {pid} has unreadable identity")
                command, started = (), 0.0
                if _wants_command(name, executables):
                    try:
                        command = tuple(process.cmdline())
                        started = float(process.create_time())
                    except psutil.AccessDenied:
                        # Retain a recognized native's PID/name. An unreadable
                        # interpreter could conceal a native wrapper: refuse.
                        if name.casefold() not in executables:
                            raise DeletionError(
                                f"process {pid} has unreadable native-wrapper ownership"
                            )
                rows.append(ProcessInfo(pid, name, command, "", started))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except psutil.Error as error:
        raise DeletionError("process enumeration is incomplete; liveness unknown") from error
    return LivenessContext(sys.platform, tuple(rows), held_file_locks())


def assert_idle(items, *, native_locked=frozenset()):
    from .app import sessions_are_known

    for item in items:
        adapter = REGISTRY.get(item.tool)
        if isinstance(adapter.inspect_liveness, Unsupported):
            raise DeletionError(f"liveness is unknown for {item.tool}:{item.session_id}")
        if item.source not in adapter.liveness_source_kinds:
            raise DeletionError("liveness is not established for this session source")
    if not sessions_are_known(items):
        raise DeletionError("refusing deletion: refreshed liveness is unknown")
    if any(item.is_open or item.open_pid for item in items):
        raise DeletionError("refusing deletion: a selected session is running")
    # A provider-owned hard-delete acquires its own interprocess writer locks.
    # The exact running-target precheck still applies; other native clients do
    # not make its ownership ambiguous. Manual file/DB deletion has no such lock.
    manual_tools = {item.tool for item in items if item.key not in native_locked}
    if not manual_tools:
        return
    snapshot = deletion_snapshot()
    if not snapshot.process_snapshot:
        raise DeletionError("refusing deletion: process snapshot is unavailable")
    for tool in manual_tools:
        adapter = REGISTRY.get(tool)
        executables = set(adapter.liveness_executables)
        executables.add(Path(adapter.default_command[0]).name.casefold())
        for process in snapshot.process_snapshot:
            names = {
                value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                for value in (process.name, *process.command)
            }
            if names & executables:
                # Launch argv and a last-touched-thread log cannot establish
                # current UI/daemon ownership after an in-process session switch.
                # Never interpret an empty exact-match map as a proof of idle.
                raise DeletionError(
                    f"refusing deletion: {adapter.label} process {process.pid} leaves live session ownership unknown; close native clients/daemons normally and retry"
                )


def build_plan(item):
    adapter = REGISTRY.get(item.tool)
    if isinstance(adapter.plan_delete, Unsupported):
        raise DeletionError(f"{adapter.label}: {adapter.plan_delete.reason}")
    resolver = (
        adapter.resolve_delete
        if not isinstance(adapter.resolve_delete, Unsupported)
        else adapter.resolve
    )
    if isinstance(resolver, Unsupported):
        raise DeletionError("authoritative native resolution is unavailable")
    ref = resolver(item.session_id)
    if ref is None or ref.session_id != item.session_id:
        raise DeletionError(
            "native session cannot be authoritatively resolved; refresh catalog first"
        )
    if str(Path(item.storage).resolve()).removesuffix(".zst") != str(
        Path(ref.storage).resolve()
    ).removesuffix(".zst"):
        raise DeletionError("catalog/native storage binding changed; refresh before deletion")
    plan = adapter.plan_delete(ref)
    if not isinstance(plan, DeletePlan) or plan.tool != item.tool or plan.native != ref:
        raise DeletionError("provider returned an invalid deletion plan")
    if not plan.files and not plan.databases:
        raise DeletionError("provider returned an empty deletion plan")
    if plan.native_delete and isinstance(adapter.execute_delete, Unsupported):
        raise DeletionError("native deletion executor is unavailable")
    if not isinstance(adapter.rediscover, Unsupported):
        from .discovery import HarnessContext

        rows = list(adapter.rediscover(HarnessContext.create(use_cache=False), [ref]))
        if len(rows) != 1 or (rows[0].tool, rows[0].session_id) != (item.tool, item.session_id):
            raise DeletionError("fresh selected native discovery did not match the deletion target")
        item.source = rows[0].source
        plan = replace(plan, notes=plan.notes + (f"Native title: {rows[0].title}",))
    return plan


def local_cleanup(items, state):
    """Remove exact menu/state references, retaining durable slug/handle claims.

    Keep handle membership/activity tombstones: deleting a canonical copy must
    leave its reference unavailable, never silently retarget it to another copy.
    """
    from . import app

    keys = {item.key for item in items}
    identities = {(item.tool, item.session_id) for item in items}

    def scrub(value):
        if isinstance(value, list):
            return [
                scrub(row)
                for row in value
                if not (isinstance(row, str) and row in keys) and not removed(row)
            ]
        if isinstance(value, dict):
            return {
                key: scrub(row)
                for key, row in value.items()
                if key not in keys and not removed(row, key)
            }
        return value

    def removed(row, tool=None):
        return (
            isinstance(row, dict) and (row.get("tool", tool), row.get("session_id")) in identities
        )

    changes = []
    if state.path.exists():
        payload = json.loads(state.path.read_bytes())
        changes.append(
            FileChange.capture(
                state.path.parent,
                state.path,
                json.dumps(scrub(payload), ensure_ascii=False).encode(),
            )
        )
    for path in (app.CATALOG_CACHE_FILE, app.LEGACY_CATALOG_FILE):
        if path.exists():
            payload = json.loads(path.read_bytes())
            payload["sessions"] = [row for row in payload.get("sessions", []) if not removed(row)]
            changes.append(
                FileChange.capture(
                    path.parent, path, json.dumps(payload, ensure_ascii=False).encode()
                )
            )
    return tuple(changes)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sessions delete")
    parser.add_argument("session", help="Session handle, native ID, or slug")
    parser.add_argument(
        "--all-copies",
        action="store_true",
        help="Delete linked conversation native copies; forks are separate sessions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the deletion plan and report liveness without deleting anything",
    )
    args = parser.parse_args(argv)
    completed = []
    chosen = []
    uncertain = ""
    try:
        from .references import native_key, resolve_session

        items, state = catalog()
        selected = resolve_session(args.session, items, store=state.slugs)
        members = state.handles.lookup_members(selected.handle) if selected.handle else None
        members = set(members or [native_key(selected.tool, selected.session_id)])
        chosen = (
            [item for item in items if native_key(item.tool, item.session_id) in members]
            if args.all_copies
            else [selected]
        )
        if args.all_copies and len(chosen) != len(members):
            raise DeletionError(
                "some linked copies are unavailable; refusing incomplete --all-copies"
            )
        plans = [build_plan(item) for item in chosen]
        native_locked = {item.key for item, plan in zip(chosen, plans) if plan.native_delete}

        def require_idle(targets):
            if native_locked:
                return assert_idle(targets, native_locked=native_locked)
            return assert_idle(targets)

        print(f"Linked native copies: {len(members)}; selected: {len(chosen)}")
        for item, plan in zip(chosen, plans):
            print(f"Tool: {plan.tool}\nNative ID: {plan.native.session_id}\nTitle: {item.title}")
            for change in plan.files:
                action = (
                    "guard absent resource"
                    if change.before == "absent"
                    else (
                        "rewrite metadata" if change.replacement is not None else "delete history"
                    )
                )
                print(f"  {action}: {change.path}")
            for db in plan.databases:
                print(f"  delete exact session rows: {db.path} [{plan.native.session_id}]")
            for note in plan.notes:
                print(f"  {note}")
            print(f"  Plan: {plan.fingerprint}")
        if args.dry_run:
            try:
                require_idle(chosen)
            except DeletionError as error:
                print(f"Liveness: deletion would be refused: {error}")
            else:
                print(
                    "Liveness precheck passed; native writer ownership is checked again at commit."
                )
            manual = [plan for plan in plans if not plan.native_delete]
            if manual:
                combine_plans(manual)
            print("Dry run: nothing deleted.")
            return 0
        require_idle(chosen)
        cleanup = local_cleanup(chosen, state)
        native_operations = any(plan.native_delete for plan in plans)
        combined = None if native_operations else combine_plans(plans, cleanup)
        if not sys.stdin.isatty():
            raise DeletionError(
                "interactive confirmation required; use --dry-run for noninteractive review"
            )
        token = selected.handle or f"{selected.tool}:{selected.session_id}"
        print(
            f"Permanently delete selected native history. Type exactly {token!r}: ",
            end="",
            flush=True,
        )
        if input() != token:
            raise DeletionError("confirmation did not match; nothing deleted")
        require_idle(chosen)
        # Re-resolve central ownership, linked membership, native identity and all
        # storage contents after the human has inspected and confirmed the plan.
        latest, latest_state = catalog()
        resolved = resolve_session(args.session, latest, store=latest_state.slugs)
        if (resolved.tool, resolved.session_id, resolved.handle) != (
            selected.tool,
            selected.session_id,
            selected.handle,
        ):
            raise DeletionError("session reference changed after confirmation")
        latest_members = (
            set(
                latest_state.handles.lookup_members(selected.handle)
                or [native_key(selected.tool, selected.session_id)]
            )
            if selected.handle
            else members
        )
        if latest_members != members:
            raise DeletionError("linked copy membership changed after confirmation")
        for item, plan in zip(chosen, plans):
            if build_plan(item).fingerprint != plan.fingerprint:
                raise DeletionError("native deletion plan changed after confirmation")
        for change in cleanup:
            change.validate()

        def validate():
            require_idle(chosen)
            for item, plan in zip(chosen, plans):
                if build_plan(item).fingerprint != plan.fingerprint:
                    raise DeletionError("native storage changed before commit")

        if native_operations:
            # Native providers own their locks, reference checks and changing
            # storage schemas. Their commits cannot be rolled back by this app.
            # Compose all manual copies first so shared history/index removals
            # cannot invalidate a second manual plan halfway through the batch.
            manual_pairs = [
                (item, plan) for item, plan in zip(chosen, plans) if not plan.native_delete
            ]
            if manual_pairs:
                manual_plan = combine_plans([plan for _, plan in manual_pairs])
                commit_plan(manual_plan, revalidate=validate)
                completed.extend(f"{item.tool}:{item.session_id}" for item, _ in manual_pairs)
            for item, original in zip(chosen, plans):
                if not original.native_delete:
                    continue
                require_idle([item])
                # Earlier native commits may update shared provider metadata.
                # Rebuild only this already-confirmed identity; no expansion to
                # a different session or switch back to manual storage deletion.
                plan = build_plan(item)
                if (
                    not plan.native_delete
                    or plan.native != original.native
                    or plan.files != original.files
                ):
                    raise DeletionError("confirmed native identity or rollout guards changed")
                uncertain = f"{item.tool}:{item.session_id}"
                REGISTRY.get(item.tool).execute_delete(plan)
                completed.append(uncertain)
                uncertain = ""
            # Local cleanup still guards the exact pre-confirmation snapshot.
            commit_plan(DeletePlan("local", plans[0].native, None, cleanup))
        else:
            commit_plan(combined, revalidate=validate)
            completed.extend(f"{item.tool}:{item.session_id}" for item in chosen)
        print(f"Permanently deleted {len(completed)} native session(s).")
        return 0
    except Exception as error:
        prefix = f"PARTIAL: deleted {', '.join(completed)}; " if completed else ""
        print(f"Deletion refused/failed: {prefix}{error}", file=sys.stderr)
        if uncertain:
            untouched = [
                f"{item.tool}:{item.session_id}"
                for item in chosen
                if f"{item.tool}:{item.session_id}" not in {*completed, uncertain}
            ]
            print(
                f"Unconfirmed native outcome: {uncertain}; no automatic retry. Untouched: {', '.join(untouched) or 'none'}",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
