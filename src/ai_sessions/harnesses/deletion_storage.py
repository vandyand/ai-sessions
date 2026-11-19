"""Conservative storage primitives for provider-authored deletion manifests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..deletion import (
    DatabaseChange,
    DeletionError,
    FileChange,
    checked_path,
    database_fingerprint,
)


def json_lines(home, path, ident, identity_fields):
    path = checked_path(home, path)
    if not path.exists():
        return FileChange.capture(home, path)
    original = path.read_bytes()
    remaining = []
    for raw in original.splitlines(keepends=True):
        if not raw.strip():
            remaining.append(raw)
            continue
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise DeletionError(f"unrecognized native metadata in {path}")
        if not any(row.get(field) == ident for field in identity_fields):
            remaining.append(raw)
    change = FileChange.capture(home, path, b"".join(remaining), "jsonl")
    if path.read_bytes() != original:
        raise DeletionError("native metadata changed while planning")
    return change


def checked_database(home, path):
    path = checked_path(home, Path(path))
    for suffix in ("-wal", "-shm", "-journal"):
        checked_path(home, Path(str(path) + suffix))
    return path


def scoped_database(
    home,
    path,
    ident,
    *,
    root,
    scopes,
    neutral=(),
    child_column=None,
    root_column="id",
    root_required=True,
    guards=(),
    additional=(),
):
    """Support only reviewed schemas, with no triggers or implicit cascades.

    scopes is provider-owned child-first (table, exact-session-column) pairs.
    Every table must be reviewed; unknown future tables require another adapter
    review. Foreign keys are enabled and checked before committing.
    """
    path = checked_database(home, path)
    if not path.exists():
        raise DeletionError(f"native database disappeared: {path}")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        allowed = {root, *neutral, *(table for table, _ in scopes)}
        if tables - allowed:
            raise DeletionError(
                f"unreviewed native database tables: {', '.join(sorted(tables - allowed))}"
            )
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
            raise DeletionError("native database triggers have not been reviewed for deletion")
        if root not in tables or (
            root_required
            and connection.execute(
                f'SELECT 1 FROM "{root}" WHERE "{root_column}"=?', (ident,)
            ).fetchone()
            is None
        ):
            raise DeletionError("exact native database session is missing")
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{root}")')}
        if child_column and child_column in columns:
            if connection.execute(
                f'SELECT 1 FROM "{root}" WHERE "{child_column}"=? AND id<>?', (ident, ident)
            ).fetchone():
                raise DeletionError("unselected native child/fork depends on this session")
        statements = []
        queries = []
        for query, parameters, reason in guards:
            if connection.execute(query.replace("{schema}", '"main"'), parameters).fetchone():
                raise DeletionError(reason)
            queries.append((query, parameters))
        for table, column in scopes:
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                raise DeletionError(f"unsupported native session scope in {table}")
            statements.append((f'DELETE FROM {{schema}}."{table}" WHERE "{column}"=?', (ident,)))
            queries.append((f'SELECT * FROM {{schema}}."{table}" WHERE "{column}"=?', (ident,)))
        # Cascades are safe only when every dependent row has the same exact
        # session scope and is explicitly deleted child-first. Check the joins:
        # a corrupt cross-session part must never vanish through message CASCADE.
        scoped = {root: root_column, **dict(scopes)}

        def quoted(value):
            return '"' + value.replace('"', '""') + '"'

        for table in tables:
            for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                target, source_column, target_column = fk[2:5]
                if target not in scoped:
                    continue  # No rows in this referenced table are deleted.
                if fk[1] != 0 or not target_column or table not in scoped:
                    raise DeletionError(
                        "unreviewed foreign key dependency on selected session resources"
                    )
                sql = (
                    f"SELECT 1 FROM {quoted(table)} AS child JOIN {quoted(target)} AS parent "
                    f"ON child.{quoted(source_column)}=parent.{quoted(target_column)} "
                    f"WHERE parent.{quoted(scoped[target])}=? "
                    f"AND (child.{quoted(scoped[table])} IS NULL OR child.{quoted(scoped[table])}<>?) LIMIT 1"
                )
                if connection.execute(sql, (ident, ident)).fetchone():
                    raise DeletionError("foreign key cascade would affect an unselected session")
                # New cross-session dependencies must invalidate the checkpoint.
                queries.append(
                    (
                        sql.replace("FROM ", "FROM {schema}.", 1).replace(
                            " JOIN ", " JOIN {schema}.", 1
                        ),
                        (ident, ident),
                    )
                )
        for sql, parameters in additional:
            if not sql.startswith("DELETE FROM {schema}."):
                raise DeletionError("additional native scope is not an explicit deletion predicate")
            statements.append((sql, parameters))
            queries.append((sql.replace("DELETE FROM", "SELECT * FROM", 1), parameters))
        statements.append((f'DELETE FROM {{schema}}."{root}" WHERE "{root_column}"=?', (ident,)))
        queries.append((f'SELECT * FROM {{schema}}."{root}" WHERE "{root_column}"=?', (ident,)))
        info = path.stat()
        fingerprint = database_fingerprint(connection, queries=queries)
        return DatabaseChange(
            home, path, fingerprint, tuple(statements), (info.st_dev, info.st_ino), tuple(queries)
        )
