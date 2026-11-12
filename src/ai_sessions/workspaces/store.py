"""Transactional workspace metadata, separate from expiring terminal memory."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..paths import STATE_FILE, env_path

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_BINDING_FIELDS = {"tool", "session_id", "home", "title", "cwd", "storage"}


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("Use a name of 1–80 letters, numbers, dots, underscores or hyphens.")
    return value


def _binding(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - _BINDING_FIELDS:
        raise ValueError("Invalid session binding fields.")
    result = dict(value)
    if any(not isinstance(v, str) for v in result.values()):
        raise ValueError("Session binding values must be strings.")
    if any(not result.get(k) for k in ("tool", "session_id", "home")):
        raise ValueError("A binding requires tool, session_id and home.")
    result["home"] = os.path.normcase(str(Path(result["home"]).expanduser().resolve()))
    return result


def _identity(binding):
    return json.dumps([binding[k] for k in ("tool", "session_id", "home")])


def _alive(pid, started):
    import psutil

    try:
        return abs(psutil.Process(pid).create_time() - started) < 0.01
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


class WorkspaceStore:
    def __init__(self, path=None):
        self.path = (
            Path(path)
            if path is not None
            else env_path(
                "AI_SESSIONS_WORKSPACE_FILE", STATE_FILE.with_suffix(".workspaces.sqlite3")
            )
        )
        self.path = self.path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    name TEXT PRIMARY KEY, layout TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS panes (
                    workspace TEXT NOT NULL REFERENCES workspaces(name),
                    slot TEXT NOT NULL, binding TEXT,
                    PRIMARY KEY (workspace, slot)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    token TEXT PRIMARY KEY, workspace TEXT NOT NULL, slot TEXT NOT NULL,
                    identity TEXT NOT NULL UNIQUE, pid INTEGER NOT NULL, started REAL NOT NULL,
                    UNIQUE(workspace, slot)
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            raise ValueError(f"Workspace store unavailable: {exc}") from exc
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, name):
        row = db.execute("SELECT layout,updated FROM workspaces WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown workspace: {name}")
        panes = {
            slot: json.loads(binding) if binding else None
            for slot, binding in db.execute(
                "SELECT slot,binding FROM panes WHERE workspace=? ORDER BY rowid", (name,)
            )
        }
        return {"name": name, "layout": json.loads(row[0]), "panes": panes, "updated": row[1]}

    def get(self, name):
        with self._db() as db:
            return self._get(db, name)

    def names(self):
        with self._db() as db:
            return [
                {"name": name, "pane_count": count, "bound_count": bound, "updated": updated}
                for name, updated, count, bound in db.execute(
                    "SELECT w.name,w.updated,COUNT(p.slot),COUNT(p.binding) FROM workspaces w "
                    "LEFT JOIN panes p ON p.workspace=w.name GROUP BY w.name ORDER BY w.name"
                )
            ]

    def _save(self, db, name, layout, replace=False):
        from .terminal import validate_layout

        _name(name)
        layout = validate_layout(layout)
        if not isinstance(layout, list) or not layout or layout[0].get("action") != "newTab":
            raise ValueError("A workspace requires a layout starting with newTab.")
        slots = []
        for index, action in enumerate(layout):
            kind = action.get("action")
            if kind not in {"newTab", "splitPane", "moveFocus", "focusPane"}:
                raise ValueError("Unsupported layout action.")
            if kind == "newTab" and index != 0:
                raise ValueError("A workspace contains exactly one tab.")
            if kind in {"newTab", "splitPane"}:
                slots.append(_name(action.get("slot")))
        if len(slots) != len(set(slots)):
            raise ValueError("Pane slots must be unique.")
        exists = db.execute("SELECT 1 FROM workspaces WHERE name=?", (name,)).fetchone()
        if exists and not replace:
            raise ValueError(f"Workspace already exists: {name}")
        if exists:
            self._prune(db)
            if db.execute("SELECT 1 FROM leases WHERE workspace=?", (name,)).fetchone():
                raise ValueError("Cannot replace a workspace while its panes are running.")
            db.execute("DELETE FROM panes WHERE workspace=?", (name,))
            db.execute(
                "UPDATE workspaces SET layout=?,updated=? WHERE name=?",
                (json.dumps(layout), time.time(), name),
            )
        else:
            db.execute(
                "INSERT INTO workspaces VALUES (?,?,?)", (name, json.dumps(layout), time.time())
            )
        db.executemany("INSERT INTO panes VALUES (?,?,NULL)", [(name, slot) for slot in slots])

    def save(self, name, layout, replace=False):
        with self._db() as db:
            self._save(db, name, layout, replace)

    def clone(self, source, destination):
        with self._db() as db:
            self._save(db, destination, self._get(db, source)["layout"])

    def bind(self, name, slot, binding, move=False):
        binding = _binding(binding)
        with self._db() as db:
            workspace = self._get(db, name)
            if slot not in workspace["panes"]:
                raise ValueError(f"Unknown pane slot: {slot}")
            if binding:
                for other, saved in workspace["panes"].items():
                    if other != slot and saved and _identity(saved) == _identity(binding):
                        if not move:
                            raise ValueError(f"Session already assigned to {other}.")
                        db.execute(
                            "UPDATE panes SET binding=NULL WHERE workspace=? AND slot=?",
                            (name, other),
                        )
            db.execute(
                "UPDATE panes SET binding=? WHERE workspace=? AND slot=?",
                (json.dumps(binding) if binding else None, name, slot),
            )
            db.execute("UPDATE workspaces SET updated=? WHERE name=?", (time.time(), name))

    def _prune(self, db):
        for token, pid, started in db.execute("SELECT token,pid,started FROM leases").fetchall():
            if not _alive(pid, started):
                db.execute("DELETE FROM leases WHERE token=?", (token,))

    def acquire(self, name, slot, identity, pid, started):
        identity = _identity(_binding(identity))
        with self._db() as db:
            if slot not in self._get(db, name)["panes"]:
                raise ValueError(f"Unknown pane slot: {slot}")
            self._prune(db)
            token = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO leases VALUES (?,?,?,?,?,?)",
                    (token, name, slot, identity, pid, started),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("This pane or session is already running in a workspace.") from exc
            return token

    def release(self, token):
        with self._db() as db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))
