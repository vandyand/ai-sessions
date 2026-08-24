"""Mutable shared-store harness used to prove the storage-neutral contract."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Callable

from ai_sessions.capabilities import HarnessAdapter, Unsupported
from ai_sessions.conversion import BridgeError
from ai_sessions.model import (
    Availability,
    BudgetPolicy,
    NativeRef,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    SourceKind,
    Transcript,
    Turn,
)


class SharedStoreFixture:
    """Two-level native identity: session rows share one mutable SQLite store."""

    def __init__(self, root: Path, *, name: str = "shared") -> None:
        self.root = root
        self.name = name
        self.path = root / f"{name}.sqlite"
        self.prepared_commands: list[tuple[str, ...]] = []
        self.prepared_options: list[dict[str, object]] = []
        self.during_snapshot: Callable[[], None] | None = None

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message (
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    compaction INTEGER NOT NULL,
                    PRIMARY KEY (session_id, ordinal),
                    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _open_existing(path: Path) -> sqlite3.Connection:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            raise FileNotFoundError(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileNotFoundError(path)
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=0.1)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=100")
        return connection

    @staticmethod
    def _digest(rows: list[tuple[int, str, str, int]]) -> str:
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return "shared-semantic-v1:" + hashlib.sha256(payload.encode()).hexdigest()

    def put(self, session_id: str, *turns: Turn, title: str = "", cwd: str = "/work") -> NativeRef:
        self.initialize()
        now = time.time()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM session WHERE id=?", (session_id,))
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                (session_id, cwd, title, now, now),
            )
            connection.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                [
                    (session_id, index, turn.role, turn.text, int(turn.compaction))
                    for index, turn in enumerate(turns)
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return NativeRef(session_id, str(self.path))

    def append(self, session_id: str, turn: Turn) -> None:
        connection = sqlite3.connect(self.path)
        try:
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM message WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (session_id, ordinal, turn.role, turn.text, int(turn.compaction)),
            )
            connection.execute("UPDATE session SET updated=? WHERE id=?", (time.time(), session_id))
            connection.commit()
        finally:
            connection.close()

    def edit(self, session_id: str, ordinal: int, text: str) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE message SET text=? WHERE session_id=? AND ordinal=?",
                (text, session_id, ordinal),
            )
            connection.commit()
        finally:
            connection.close()

    def delete(self, session_id: str) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM session WHERE id=?", (session_id,))
            connection.commit()
        finally:
            connection.close()

    def _rows(self, ref: NativeRef, *, invoke_hook: bool) -> list[tuple[int, str, str, int]]:
        connection = self._open_existing(Path(ref.storage))
        try:
            connection.execute("BEGIN")
            found = connection.execute(
                "SELECT 1 FROM session WHERE id=?", (ref.session_id,)
            ).fetchone()
            if found is None:
                raise BridgeError(f"shared session {ref.session_id!r} is unavailable")
            if invoke_hook and self.during_snapshot is not None:
                hook, self.during_snapshot = self.during_snapshot, None
                hook()
            rows = [
                (int(row[0]), str(row[1]), str(row[2]), int(row[3]))
                for row in connection.execute(
                    "SELECT ordinal, role, text, compaction FROM message "
                    "WHERE session_id=? ORDER BY ordinal",
                    (ref.session_id,),
                )
            ]
            return rows
        except sqlite3.Error as error:
            raise BridgeError(f"shared store could not be read: {error}") from error
        finally:
            connection.close()

    def checkpoint(self, ref: NativeRef) -> str:
        return self._digest(self._rows(ref, invoke_hook=False))

    def read(self, ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
        rows = self._rows(ref, invoke_hook=True)
        turns = [
            Turn(role, text, compaction=bool(compaction)) for _, role, text, compaction in rows
        ]
        if latest_window:
            boundary = max(
                (index for index, turn in enumerate(turns) if turn.compaction), default=0
            )
            turns = turns[boundary:]
        return ReadSnapshot(Transcript(turns), self._digest(rows))

    def resolve(self, session_id: str) -> NativeRef | None:
        ref = NativeRef(session_id, str(self.path))
        return ref if self.availability(ref) == Availability.AVAILABLE else None

    def availability(self, ref: NativeRef) -> Availability:
        path = Path(ref.storage)
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return Availability.UNAVAILABLE
        except OSError:
            return Availability.UNKNOWN
        if not stat.S_ISREG(metadata.st_mode):
            return Availability.UNAVAILABLE
        try:
            connection = self._open_existing(path)
            try:
                connection.execute("SELECT id FROM session LIMIT 0")
                found = connection.execute(
                    "SELECT 1 FROM session WHERE id=?", (ref.session_id,)
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return Availability.UNKNOWN
        return Availability.AVAILABLE if found is not None else Availability.UNAVAILABLE

    def change_status(self, ref: NativeRef, checkpoint: int | str) -> str:
        if not isinstance(checkpoint, str) or not checkpoint.startswith("shared-semantic-v1:"):
            return "unstable"
        try:
            current = self.checkpoint(ref)
        except BridgeError:
            return "unstable"
        return "unchanged" if current == checkpoint else "changed"

    def prepare_target(
        self, command: tuple[str, ...], cwd: str, options: dict[str, object]
    ) -> PreparedTarget:
        del cwd
        self.prepared_commands.append(command)
        self.prepared_options.append(dict(options))
        return PreparedTarget(command, self.adapter().budget)

    def write(
        self,
        *,
        cwd: str,
        turns: list[Turn],
        prepared: PreparedTarget,
        title: str = "",
        created: float | None = None,
    ) -> NativeWrite:
        del prepared, created
        session_id = "shr-" + uuid.uuid4().hex
        ref = self.put(session_id, *turns, title=title, cwd=cwd)
        return NativeWrite(ref, self.checkpoint(ref))

    @staticmethod
    def resume_args(
        *,
        session_id: str,
        source: SourceKind,
        resume_id: str,
        parent_id: str,
        native: bool,
    ) -> list[str]:
        del source, resume_id, parent_id, native
        return ["--session", session_id]

    def adapter(self) -> HarnessAdapter:
        return HarnessAdapter(
            name=self.name,
            label="Shared Store Harness",
            short_label="SharedStore",
            order=30,
            home=self.root,
            default_command=("shared-cli",),
            dangerous_args=("--unsafe",),
            source_kinds=frozenset((SourceKind.INTERACTIVE,)),
            id_patterns=(re.compile(rb"shr-[0-9a-f]+"),),
            read=self.read,
            write=self.write,
            resolve=self.resolve,
            availability=self.availability,
            checkpoint=self.checkpoint,
            change_status=self.change_status,
            budget=BudgetPolicy(64_000, 0.75, 2.0, "shared-store test policy"),
            discover=Unsupported("not needed by shared-store contract tests"),
            resume_args=self.resume_args,
            prepare_target=self.prepare_target,
        )
