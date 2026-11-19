"""Transactional project and logical-session handles in the shared slug registry."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .paths import STATE_FILE
from .references import NativeKey, native_key


def _directory(value):
    def plain(path):
        if path[:8].casefold() == "\\\\?\\unc\\":
            return "\\\\" + path[8:]
        return path[4:] if path.startswith("\\\\?\\") else path

    return plain(os.path.normcase(str(Path(plain(str(value or "."))).expanduser().resolve())))


def _project_root(value):
    directory = Path(_directory(value))
    for parent in (directory, *directory.parents):
        marker = parent / ".git"
        if marker.is_dir():
            return _directory(parent)
        if marker.is_file():
            try:
                prefix, pointer = marker.read_text().strip().split(":", 1)
                if prefix != "gitdir":
                    break
                gitdir = (parent / pointer.strip()).resolve()
                common = gitdir / "commondir"
                if common.is_file():
                    return _directory((gitdir / common.read_text().strip()).resolve().parent)
                return _directory(parent)
            except (OSError, ValueError):
                break
    return str(directory)


_ROLES = {
    "supervisor": "sup",
    "sup": "sup",
    "manager": "mgr",
    "mgr": "mgr",
    "worker": "wrk",
    "helper": "wrk",
    "wrk": "wrk",
    "session": "session",
    "ses": "session",
}


def _role(item):
    if item.supervises:
        return "sup", 1
    match = (
        re.search(
            r"\b(supervisor|manager|worker|helper|sup|mgr|wrk)\b[\s_#:-]*(\d+)?", item.title, re.I
        )
        if item.named
        else None
    )
    return (_ROLES[match[1].lower()], max(1, int(match[2] or 1))) if match else ("session", 1)


def _default_handle(project, role, number):
    component = "ses" if role == "session" else role
    return f"{project}-{component}-{number}"


def _aliases(name):
    name = name.casefold()
    if name.endswith("-1"):
        return {name, name[:-2]}
    return {name} if re.search(r"-\d+$", name) else {name, name + "-1"}


def _validate(value, project=False):
    pattern = r"[A-Za-z0-9]{1,5}" if project else r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(
            "invalid project handle (1..5 alphanumeric)" if project else "invalid session handle"
        )
    return value.casefold()


class HandleStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else STATE_FILE.with_suffix(".slugs.sqlite3")

    @contextmanager
    def _connection(self, readonly=False, *, items=()):
        connection = None
        try:
            if readonly:
                if not self.path.exists():
                    yield None
                    return
                connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='handle_claims' AND type='table'"
                ).fetchone()
                yield connection if exists else None
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            for statement in (
                "CREATE TABLE IF NOT EXISTS handle_project_claims (name TEXT PRIMARY KEY COLLATE NOCASE, project_id TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS handle_projects (id TEXT PRIMARY KEY, directory TEXT UNIQUE NOT NULL, handle TEXT UNIQUE COLLATE NOCASE NOT NULL)",
                "CREATE TABLE IF NOT EXISTS handle_logicals (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL, number INTEGER NOT NULL, handle TEXT NOT NULL, preferred_tool TEXT, preferred_id TEXT, preferred_home TEXT)",
                "CREATE TABLE IF NOT EXISTS handle_members (tool TEXT NOT NULL, session_id TEXT NOT NULL, home TEXT NOT NULL, logical_id TEXT NOT NULL, PRIMARY KEY(tool,session_id,home))",
                "CREATE TABLE IF NOT EXISTS handle_claims (name TEXT PRIMARY KEY COLLATE NOCASE, logical_id TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS handle_conversations (conversation_id TEXT NOT NULL, logical_id TEXT NOT NULL, PRIMARY KEY(conversation_id,logical_id))",
                "CREATE TABLE IF NOT EXISTS handle_activity (tool TEXT NOT NULL, session_id TEXT NOT NULL, home TEXT NOT NULL, updated REAL NOT NULL, superseded INTEGER NOT NULL, PRIMARY KEY(tool,session_id,home))",
                "CREATE INDEX IF NOT EXISTS handle_members_owner ON handle_members(logical_id)",
            ):
                connection.execute(statement)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(handle_logicals)")}
            if "project_explicit" not in columns:
                connection.execute(
                    "ALTER TABLE handle_logicals ADD COLUMN project_explicit INTEGER NOT NULL DEFAULT 0"
                )
                # -1 means a legacy generated-shaped handle still needs native
                # roots. Project-only commands must not finalize it as automatic.
                for row in connection.execute(
                    "SELECT l.*,p.handle AS project_handle,p.directory FROM handle_logicals l "
                    "JOIN handle_projects p ON p.id=l.project_id"
                ).fetchall():
                    generated = {
                        _default_handle(row["project_handle"], row["role"], row["number"]),
                        f"{row['project_handle']}-{row['role']}-{row['number']}",
                    }
                    connection.execute(
                        "UPDATE handle_logicals SET project_explicit=? WHERE id=?",
                        (1 if row["handle"] not in generated else -1, row["id"]),
                    )
            pending = connection.execute(
                "SELECT l.id,p.directory FROM handle_logicals l "
                "JOIN handle_projects p ON p.id=l.project_id WHERE l.project_explicit=-1"
            ).fetchall()
            if pending:
                native_roots = {}
                for item in items:
                    owner = self._owner(connection, native_key(item.tool, item.session_id))
                    if owner is not None:
                        native_roots.setdefault(owner, set()).add(_project_root(item.cwd))
                for row in pending:
                    roots = native_roots.get(row["id"], set())
                    if roots:
                        connection.execute(
                            "UPDATE handle_logicals SET project_explicit=? WHERE id=?",
                            (int(row["directory"] not in roots), row["id"]),
                        )
            # Older assign writers do not know about project_explicit. Protect
            # their project moves too; automatic inheritance clears it below in
            # the same transaction, after this trigger has executed.
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS handle_project_assignment_explicit "
                "AFTER UPDATE OF project_id ON handle_logicals "
                "WHEN NEW.project_id <> OLD.project_id BEGIN "
                "UPDATE handle_logicals SET project_explicit=1 WHERE id=NEW.id; END"
            )
            connection.execute(
                "INSERT OR IGNORE INTO handle_project_claims SELECT handle,id FROM handle_projects"
            )
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                connection.rollback()
            raise ValueError(f"session handle registry is unavailable: {error}") from None
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _members(connection, owner):
        return {
            NativeKey(*row)
            for row in connection.execute(
                "SELECT tool,session_id,home FROM handle_members WHERE logical_id=?", (owner,)
            )
        }

    @staticmethod
    def _owner(connection, key):
        row = connection.execute(
            "SELECT logical_id FROM handle_members WHERE tool=? AND session_id=? AND home=?",
            (key.tool, key.session_id, key.home),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _native_names(connection, keys=()):
        """Snapshot the shared namespace once per transaction, using Python casefold."""
        names = {}

        def add(name, key):
            names.setdefault(name.casefold(), set()).add(key)

        for key in keys:
            add(key.session_id, key)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("claims", "sessions", "handle_members"):
            if table not in tables:
                continue
            columns = "tool,session_id,home" + (",slug" if table != "handle_members" else "")
            for row in connection.execute(f"SELECT {columns} FROM {table}"):
                key = NativeKey(*row[:3])
                add(key.session_id, key)
                if table != "handle_members":
                    add(row[3], key)
        return names

    def _available(self, connection, name, owner, native_ids=None):
        names = native_ids if native_ids is not None else self._native_names(connection)
        for alias in _aliases(name):
            claim = connection.execute(
                "SELECT logical_id FROM handle_claims WHERE name=?", (alias,)
            ).fetchone()
            if claim and claim[0] != owner:
                return False
            # Native IDs and slug claims keep their per-copy meaning, even for
            # members of this logical owner (including retired slug claims).
            if alias in names:
                return False
        return True

    def _claim(self, connection, owner, name, native_ids=None):
        if not self._available(connection, name, owner, native_ids):
            raise ValueError(f"handle {name!r} is already assigned or reserved")
        for alias in _aliases(name):
            connection.execute("INSERT OR IGNORE INTO handle_claims VALUES (?,?)", (alias, owner))
        connection.execute("UPDATE handle_logicals SET handle=? WHERE id=?", (name, owner))

    def _project(self, connection, directory, handle=None):
        row = connection.execute(
            "SELECT * FROM handle_projects WHERE directory=?", (directory,)
        ).fetchone()
        if row:
            if handle and row["handle"] != handle:
                raise ValueError("directory already has a project handle; use set_project")
            return row
        base = handle or (re.sub("[^a-z0-9]", "", Path(directory).name.casefold())[:4] or "proj")
        candidate, suffix = base, 1
        while connection.execute(
            "SELECT 1 FROM handle_project_claims WHERE name=?", (candidate,)
        ).fetchone():
            if handle:
                raise ValueError("project handle is already assigned")
            candidate = base[: max(1, 5 - len(str(suffix)))] + str(suffix)
            suffix += 1
        ident = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO handle_projects VALUES (?,?,?)", (ident, directory, candidate)
        )
        connection.execute("INSERT INTO handle_project_claims VALUES (?,?)", (candidate, ident))
        return connection.execute("SELECT * FROM handle_projects WHERE id=?", (ident,)).fetchone()

    def _annotate(self, connection, item, owner):
        row = connection.execute(
            "SELECT l.*,p.handle AS project_handle FROM handle_logicals l JOIN handle_projects p ON p.id=l.project_id WHERE l.id=?",
            (owner,),
        ).fetchone()
        item.handle, item.project_handle, item.session_role = (
            row["handle"],
            row["project_handle"],
            row["role"],
        )

    def _unchanged(self, items, keys):
        """Annotate a complete unchanged batch without acquiring SQLite's writer lock."""
        with self._connection(readonly=True) as connection:
            if connection is None:
                return False
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='handle_project_assignment_explicit'"
            ).fetchone():
                return False
            rows = {}
            for row in connection.execute(
                "SELECT m.*,l.*,p.handle AS project_handle,a.updated,a.superseded "
                "FROM handle_members m JOIN handle_logicals l ON l.id=m.logical_id "
                "JOIN handle_projects p ON p.id=l.project_id LEFT JOIN handle_activity a "
                "ON a.tool=m.tool AND a.session_id=m.session_id AND a.home=m.home"
            ):
                rows[(row["tool"], row["session_id"], row["home"])] = row
            conversations = {
                tuple(row)
                for row in connection.execute(
                    "SELECT conversation_id,logical_id FROM handle_conversations"
                )
            }
            metadata = []
            for item, key in zip(items, keys):
                row = rows.get((key.tool, key.session_id, key.home))
                if (
                    row is None
                    or row["updated"] != item.updated
                    or row["superseded"] != int(item.superseded)
                ):
                    return False
                if row["project_explicit"] == -1:
                    return False
                if (
                    row["role"] == "session"
                    and row["handle"] == f"{row['project_handle']}-session-{row['number']}"
                    and not ("project_explicit" in row.keys() and row["project_explicit"])
                ):
                    return False
                if (
                    item.conversation_id
                    and (item.conversation_id, row["logical_id"]) not in conversations
                ):
                    return False
                metadata.append((row["handle"], row["project_handle"], row["role"]))
            for item, values in zip(items, metadata):
                item.handle, item.project_handle, item.session_role = values
            return True

    def _migrate_generic_handles(self, connection, names):
        """Shorten generated generic defaults, retaining all retired claims."""
        for row in connection.execute(
            "SELECT l.*,p.handle AS project_handle FROM handle_logicals l "
            "JOIN handle_projects p ON p.id=l.project_id "
            "WHERE l.role='session' AND l.project_explicit<=0"
        ).fetchall():
            if row["handle"] != f"{row['project_handle']}-session-{row['number']}":
                continue
            number = row["number"]
            while not self._available(
                connection,
                _default_handle(row["project_handle"], row["role"], number),
                row["id"],
                names,
            ):
                number += 1
            self._claim(
                connection,
                row["id"],
                _default_handle(row["project_handle"], row["role"], number),
                names,
            )
            connection.execute(
                "UPDATE handle_logicals SET number=? WHERE id=?", (number, row["id"])
            )

    def ensure(self, items):
        items = list(items)
        roots = {}
        next_numbers = {}
        keys = [native_key(item.tool, item.session_id) for item in items]
        if not items:
            return {}
        if self._unchanged(items, keys):
            return {item.key: item.handle for item in items}
        representatives = {}
        for item in items:
            if item.conversation_id and not item.parent_id:
                rank = (
                    bool(item.named or item.supervises),
                    item.updated,
                    _role(item)[0] != "session",
                    item.key,
                )
                previous = representatives.get(item.conversation_id)
                if previous is None or rank > previous[0]:
                    representatives[item.conversation_id] = (rank, item)
        with self._connection(items=items) as connection:
            names = self._native_names(connection, keys)
            self._migrate_generic_handles(connection, names)
            for item, key in sorted(
                zip(items, keys), key=lambda pair: (pair[1].tool, pair[1].home, pair[1].session_id)
            ):
                owner = self._owner(connection, key)
                if not owner and item.conversation_id and not item.parent_id:
                    rows = connection.execute(
                        "SELECT DISTINCT logical_id FROM handle_conversations WHERE conversation_id=?",
                        (item.conversation_id,),
                    ).fetchall()
                    if len(rows) == 1:
                        owner = rows[0][0]
                fresh = not owner
                if fresh:
                    owner = uuid.uuid4().hex
                    representative = (
                        representatives[item.conversation_id][1]
                        if item.conversation_id and not item.parent_id
                        else item
                    )
                    if representative.cwd not in roots:
                        roots[representative.cwd] = _project_root(representative.cwd)
                    project = self._project(connection, roots[representative.cwd])
                    role, number = _role(representative)
                    connection.execute(
                        "INSERT INTO handle_logicals (id,project_id,role,number,handle) VALUES (?,?,?,?,?)",
                        (owner, project["id"], role, number, ""),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO handle_members VALUES (?,?,?,?)",
                    (key.tool, key.session_id, key.home, owner),
                )
                connection.execute(
                    "INSERT INTO handle_activity VALUES (?,?,?,?,?) ON CONFLICT(tool,session_id,home) DO UPDATE SET updated=excluded.updated,superseded=excluded.superseded WHERE handle_activity.updated<>excluded.updated OR handle_activity.superseded<>excluded.superseded",
                    (key.tool, key.session_id, key.home, item.updated, int(item.superseded)),
                )
                if fresh:
                    allocation = (project["id"], role, number)
                    # Explicit role numbers still start at their requested number.
                    number = next_numbers.get(allocation, number)
                    while not self._available(
                        connection, _default_handle(project["handle"], role, number), owner, names
                    ):
                        number += 1
                    self._claim(
                        connection, owner, _default_handle(project["handle"], role, number), names
                    )
                    next_numbers[allocation] = number + 1
                    connection.execute(
                        "UPDATE handle_logicals SET number=? WHERE id=?", (number, owner)
                    )
                if item.conversation_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO handle_conversations VALUES (?,?)",
                        (item.conversation_id, owner),
                    )
                self._annotate(connection, item, owner)
        return {item.key: item.handle for item in items}

    def inherit_projects(self, pairs):
        """Inherit default supervisors' projects from (supervisor, target) pairs.

        Call ensure(all_items) first, then supply every supervisor copy. A target
        is an ensured Session (its persisted logical project wins), or a directory
        string when the target is missing. Manual handles/assignments are preserved.
        Returns the supplied supervisors' key-to-handle mapping, like ensure().
        """
        pairs = list(pairs)
        if not pairs:
            return {}
        roots = {}
        prepared = []
        for supervisor, target in pairs:
            source_key = native_key(supervisor.tool, supervisor.session_id)
            if isinstance(target, str):
                if target not in roots:
                    roots[target] = _project_root(target)
                prepared.append((supervisor, source_key, None, roots[target]))
            else:
                prepared.append(
                    (
                        supervisor,
                        source_key,
                        native_key(target.tool, target.session_id),
                        None,
                    )
                )

        def inspect(connection):
            pending = []
            for supervisor, key, target_key, directory in prepared:
                owner = self._owner(connection, key)
                if owner is None:
                    raise ValueError("ensure all sessions before inheriting projects")
                row = connection.execute(
                    "SELECT l.*,p.handle AS project_handle FROM handle_logicals l "
                    "JOIN handle_projects p ON p.id=l.project_id WHERE l.id=?",
                    (owner,),
                ).fetchone()
                explicit = "project_explicit" in row.keys() and row["project_explicit"]
                generated = row["handle"] == _default_handle(
                    row["project_handle"], row["role"], row["number"]
                )
                if explicit or not generated:
                    continue
                if target_key is not None:
                    target_owner = self._owner(connection, target_key)
                    if target_owner is None:
                        raise ValueError("ensure all sessions before inheriting projects")
                    project = connection.execute(
                        "SELECT p.* FROM handle_projects p JOIN handle_logicals l "
                        "ON l.project_id=p.id WHERE l.id=?",
                        (target_owner,),
                    ).fetchone()
                else:
                    project = connection.execute(
                        "SELECT * FROM handle_projects WHERE directory=?", (directory,)
                    ).fetchone()
                if project is None or project["id"] != row["project_id"]:
                    pending.append((owner, row, project, directory))
            return pending

        def annotate(connection):
            for supervisor, key, _, _ in prepared:
                self._annotate(connection, supervisor, self._owner(connection, key))

        with self._connection(readonly=True) as connection:
            if connection is None:
                raise ValueError("ensure all sessions before inheriting projects")
            if not inspect(connection):
                annotate(connection)
                return {supervisor.key: supervisor.handle for supervisor, _ in pairs}
        with self._connection(items=[supervisor for supervisor, _ in pairs]) as connection:
            # Recheck after taking the writer lock, including manual overrides
            # made by another process since the read-only snapshot.
            names = self._native_names(connection)
            for owner, row, project, directory in inspect(connection):
                if project is None:
                    project = self._project(connection, directory)
                # Copies may share an owner. Recheck its current project so a
                # repeated pair cannot allocate another number for that owner.
                current = connection.execute(
                    "SELECT project_id FROM handle_logicals WHERE id=?", (owner,)
                ).fetchone()
                if current[0] == project["id"]:
                    continue
                number = row["number"]
                while not self._available(
                    connection,
                    _default_handle(project["handle"], row["role"], number),
                    owner,
                    names,
                ):
                    number += 1
                self._claim(
                    connection,
                    owner,
                    _default_handle(project["handle"], row["role"], number),
                    names,
                )
                connection.execute(
                    "UPDATE handle_logicals SET project_id=?,number=? WHERE id=?",
                    (project["id"], number, owner),
                )
                connection.execute(
                    "UPDATE handle_logicals SET project_explicit=0 WHERE id=?", (owner,)
                )
            annotate(connection)
        return {supervisor.key: supervisor.handle for supervisor, _ in pairs}

    def reserved_names(self, connection=None):
        if connection is None:
            with self._connection(readonly=True) as own:
                return self.reserved_names(own) if own is not None else {}
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='handle_claims'"
            ).fetchone()
            if not exists:
                return {}
            result = {}
            for name, tool, ident, home in connection.execute(
                "SELECT c.name,m.tool,m.session_id,m.home FROM handle_claims c JOIN handle_members m ON m.logical_id=c.logical_id"
            ):
                result.setdefault(name.casefold(), set()).add(NativeKey(tool, ident, home))
            return result
        except sqlite3.Error as error:
            raise ValueError(f"session handle registry is unavailable: {error}") from None

    def claimed(self, reference):
        return reference.rsplit(":", 1)[-1].casefold() in self.reserved_names()

    def lookup_members(self, reference):
        qualifier, separator, token = reference.partition(":")
        reference = token if separator else reference
        return (
            sorted(
                (
                    key
                    for key in self.reserved_names().get(reference.casefold(), ())
                    if not separator or key.tool.casefold() == qualifier.casefold()
                ),
                key=lambda k: (k.tool, k.home, k.session_id),
            )
            or None
        )

    @staticmethod
    def _qualified(reference, tool=None):
        qualifier, separator, token = reference.partition(":")
        if separator:
            if tool and tool.casefold() != qualifier.casefold():
                raise ValueError("conflicting harness qualifiers")
            return token, qualifier
        return reference, tool

    @staticmethod
    def _choose_current(row, members, reference, tool=None):
        """Select persisted activity first, then validate home; never select by reachability."""
        if reference.casefold() not in _aliases(row["handle"]):
            raise ValueError(f"handle {reference!r} is retired; use {row['handle']!r}")
        candidates = []
        for member in members:
            key = NativeKey(member["tool"], member["session_id"], member["home"])
            if tool and key.tool.casefold() != tool.casefold():
                continue
            if member["updated"] is None:
                raise ValueError(f"handle {reference!r} has unavailable activity metadata")
            preferred = (key.tool, key.session_id, key.home) == (
                row["preferred_tool"],
                row["preferred_id"],
                row["preferred_home"],
            )
            rank = (
                not member["superseded"],
                member["updated"],
                preferred,
                key.tool,
                key.session_id,
                key.home,
            )
            candidates.append((rank, key))
        if not candidates:
            raise ValueError(f"handle {reference!r} is unavailable in this harness home")
        chosen = max(candidates, key=lambda pair: pair[0])[1]
        try:
            active_home = native_key(chosen.tool, chosen.session_id).home
        except (KeyError, ValueError):
            raise ValueError(
                "canonical handle member is unavailable in this harness home"
            ) from None
        if active_home != chosen.home:
            raise ValueError("canonical handle member belongs to another harness home")
        return chosen

    @staticmethod
    def _activity(connection, owner=None):
        query = (
            "SELECT m.*,a.updated,a.superseded FROM handle_members m "
            "LEFT JOIN handle_activity a ON m.tool=a.tool "
            "AND m.session_id=a.session_id AND m.home=a.home"
        )
        if owner is not None:
            return connection.execute(query + " WHERE m.logical_id=?", (owner,))
        return connection.execute(query)

    def current_key(self, reference, tool=None):
        """Return the persisted canonical address; availability must never select a fallback."""
        reference, tool = self._qualified(reference, tool)
        with self._connection(readonly=True) as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT l.* FROM handle_claims c JOIN handle_logicals l "
                "ON l.id=c.logical_id WHERE c.name=? COLLATE NOCASE",
                (reference.casefold(),),
            ).fetchone()
            if row is None:
                return None
            return self._choose_current(row, self._activity(connection, row["id"]), reference, tool)

    def current_keys(self, references=None):
        """Read canonical handle -> native address in one snapshot.

        None selects every logical session. A supplied iterable selects claims (including
        unnumbered aliases and tool qualifiers); unknown claims are omitted. Retired claims
        and unavailable canonical homes raise ValueError, just as current_key does.
        """
        if references is not None:
            references = list(references)
            if not references:
                return {}
        with self._connection(readonly=True) as connection:
            if connection is None:
                return {}
            logicals = {
                row["id"]: row for row in connection.execute("SELECT * FROM handle_logicals")
            }
            members = {}
            for row in self._activity(connection):
                members.setdefault(row["logical_id"], []).append(row)
            if references is None:
                requests = [(row["handle"], None, row) for row in logicals.values()]
            else:
                claims = {
                    name.casefold(): owner
                    for name, owner in connection.execute(
                        "SELECT name,logical_id FROM handle_claims"
                    )
                }
                requests = []
                for reference in references:
                    token, tool = self._qualified(reference)
                    owner = claims.get(token.casefold())
                    if owner in logicals:
                        requests.append((token, tool, logicals[owner]))
            result = {}
            for token, tool, row in requests:
                key = self._choose_current(row, members.get(row["id"], ()), token, tool)
                if row["handle"] in result and result[row["handle"]] != key:
                    raise ValueError("conflicting canonical harness selections for one handle")
                result[row["handle"]] = key
            return result

    def metadata_for(self, keys):
        """Return persisted metadata for exact native keys, omitting unknown memberships."""
        keys = set(keys)
        if not keys:
            return {}
        with self._connection(readonly=True) as connection:
            if connection is None:
                return {}
            result = {}
            for row in connection.execute(
                "SELECT m.tool,m.session_id,m.home,l.handle,p.handle AS project_handle,l.role "
                "FROM handle_members m JOIN handle_logicals l ON l.id=m.logical_id "
                "JOIN handle_projects p ON p.id=l.project_id"
            ):
                key = NativeKey(row["tool"], row["session_id"], row["home"])
                if key in keys:
                    result[key] = dict(
                        handle=row["handle"],
                        project_handle=row["project_handle"],
                        session_role=row["role"],
                    )
            return result

    def resolve(self, reference, items, tool=None):
        key = self.current_key(reference, tool=tool)
        if key is None:
            return None
        matches = [item for item in items if native_key(item.tool, item.session_id) == key]
        if len(matches) != 1:
            raise ValueError(
                f"handle {reference!r} is unavailable or ambiguous in this harness home"
            )
        return matches[0]

    def add_project(self, handle, directory):
        with self._connection() as connection:
            return dict(
                self._project(connection, _project_root(directory), _validate(handle, True))
            )

    def projects(self):
        with self._connection(readonly=True) as connection:
            if connection is None:
                return []
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT p.*,COUNT(l.id) AS session_count FROM handle_projects p LEFT JOIN handle_logicals l ON l.project_id=p.id GROUP BY p.id ORDER BY p.handle"
                )
            ]

    def project_for(self, item):
        self.ensure([item])
        with self._connection() as connection:
            owner = self._owner(connection, native_key(item.tool, item.session_id))
            return dict(
                connection.execute(
                    "SELECT p.* FROM handle_projects p JOIN handle_logicals l ON l.project_id=p.id WHERE l.id=?",
                    (owner,),
                ).fetchone()
            )

    def set_session(self, item, value):
        value = _validate(value)
        self.ensure([item])
        with self._connection() as connection:
            owner = self._owner(connection, native_key(item.tool, item.session_id))
            self._claim(connection, owner, value)
            connection.execute("UPDATE handle_logicals SET project_explicit=1 WHERE id=?", (owner,))
            self._annotate(connection, item, owner)
        return value

    def set_project(self, project_handle, new_handle):
        new_handle = _validate(new_handle, True)
        with self._connection() as connection:
            project = connection.execute(
                "SELECT * FROM handle_projects WHERE handle=? COLLATE NOCASE", (project_handle,)
            ).fetchone()
            if not project:
                raise ValueError("unknown project")
            if connection.execute(
                "SELECT 1 FROM handle_project_claims WHERE name=? AND project_id<>?",
                (new_handle, project["id"]),
            ).fetchone():
                raise ValueError("project handle already assigned")
            names = self._native_names(connection)
            self._migrate_generic_handles(connection, names)
            for row in connection.execute(
                "SELECT * FROM handle_logicals WHERE project_id=?", (project["id"],)
            ).fetchall():
                if (
                    row["handle"] == _default_handle(project["handle"], row["role"], row["number"])
                    and row["project_explicit"] <= 0
                ):
                    self._claim(
                        connection,
                        row["id"],
                        _default_handle(new_handle, row["role"], row["number"]),
                        names,
                    )
            connection.execute(
                "INSERT OR IGNORE INTO handle_project_claims VALUES (?,?)",
                (new_handle, project["id"]),
            )
            connection.execute(
                "UPDATE handle_projects SET handle=? WHERE id=?", (new_handle, project["id"])
            )
        return new_handle

    def assign(self, item, project_handle, role=None, number=None):
        self.ensure([item])
        with self._connection() as connection:
            owner = self._owner(connection, native_key(item.tool, item.session_id))
            project = connection.execute(
                "SELECT * FROM handle_projects WHERE handle=? COLLATE NOCASE", (project_handle,)
            ).fetchone()
            if not project:
                raise ValueError("unknown project")
            role = _ROLES.get(role or item.session_role)
            if not role or (number is not None and (type(number) is not int or number < 1)):
                raise ValueError("invalid role or number")
            chosen = number or 1
            names = self._native_names(connection)
            while not self._available(
                connection, _default_handle(project["handle"], role, chosen), owner, names
            ):
                if number is not None:
                    raise ValueError("handle is already reserved")
                chosen += 1
            self._claim(connection, owner, _default_handle(project["handle"], role, chosen), names)
            connection.execute(
                "UPDATE handle_logicals SET project_id=?,role=?,number=?,project_explicit=1 WHERE id=?",
                (project["id"], role, chosen, owner),
            )
            self._annotate(connection, item, owner)
        return item.handle

    def link(self, source, target):
        self.ensure([source, target])
        with self._connection() as connection:
            owner = self._owner(connection, native_key(source.tool, source.session_id))
            other = self._owner(connection, native_key(target.tool, target.session_id))
            if owner != other:
                connection.execute(
                    "UPDATE handle_members SET logical_id=? WHERE logical_id=?", (owner, other)
                )
                connection.execute(
                    "UPDATE handle_claims SET logical_id=? WHERE logical_id=?", (owner, other)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO handle_conversations SELECT conversation_id,? FROM handle_conversations WHERE logical_id=?",
                    (owner, other),
                )
                connection.execute("DELETE FROM handle_conversations WHERE logical_id=?", (other,))
                connection.execute("DELETE FROM handle_logicals WHERE id=?", (other,))
            key = native_key(target.tool, target.session_id)
            connection.execute(
                "UPDATE handle_logicals SET preferred_tool=?,preferred_id=?,preferred_home=? WHERE id=?",
                (key.tool, key.session_id, key.home, owner),
            )
            self._annotate(connection, source, owner)
            self._annotate(connection, target, owner)
        return source.handle
