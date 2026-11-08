"""Stable native references and durable, globally unique session slugs."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .model import Session
from .paths import STATE_FILE


@lru_cache(maxsize=256)
def _canonical_home(value: str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


@dataclass(frozen=True, slots=True)
class NativeKey:
    tool: str
    session_id: str
    home: str

    def __post_init__(self):
        if not self.tool or not self.session_id or not self.home:
            raise ValueError("session identity requires harness, native ID and home")
        object.__setattr__(self, "home", _canonical_home(str(self.home)))


def native_key(tool: str, session_id: str, home: str | Path | None = None) -> NativeKey:
    if not home:
        from .registry import REGISTRY

        home = REGISTRY.get(tool).home
    return NativeKey(tool, session_id, str(home))


def _base_slug(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:48].rstrip("-") or "session"


def _validate_slug(value: str) -> str:
    value = value.lower()
    if len(value) > 64 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None:
        raise ValueError("slug must be 1-64 ASCII letters/digits separated by single hyphens")
    return value


class SlugRegistryError(ValueError):
    """A durable slug assignment could not be read or committed."""


class SlugStore:
    """Independent of state/catalog writers; SQLite serializes competing claims."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else STATE_FILE.with_suffix(".slugs.sqlite3")

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions (tool TEXT NOT NULL, session_id TEXT NOT NULL, "
                "home TEXT NOT NULL, slug TEXT NOT NULL UNIQUE COLLATE NOCASE, "
                "PRIMARY KEY(tool, session_id, home))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS claims (slug TEXT PRIMARY KEY COLLATE NOCASE, "
                "tool TEXT NOT NULL, session_id TEXT NOT NULL, home TEXT NOT NULL)"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = self._connect()
            yield connection
        except (sqlite3.Error, OSError) as error:
            raise SlugRegistryError(f"session slug registry is unavailable: {error}") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _assignments(connection):
        return {
            NativeKey(tool, ident, home): slug
            for tool, ident, home, slug in connection.execute(
                "SELECT tool, session_id, home, slug FROM sessions"
            )
        }

    @staticmethod
    def _claim(connection, key, slug):
        connection.execute(
            "INSERT OR IGNORE INTO claims VALUES (?, ?, ?, ?)",
            (slug, key.tool, key.session_id, key.home),
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tool, session_id, home) DO UPDATE SET slug=excluded.slug",
            (key.tool, key.session_id, key.home, slug),
        )

    def ensure(self, entries: Iterable[tuple[NativeKey, str]]) -> dict[NativeKey, str]:
        entries = dict(entries)
        if not entries:
            return {}
        with self._connection() as connection:
            assigned = self._assignments(connection)
            if entries.keys() <= assigned.keys():
                return {key: assigned[key] for key in entries}
            connection.execute("BEGIN IMMEDIATE")
            try:
                assigned = self._assignments(connection)
                reserved = {row[0] for row in connection.execute("SELECT slug FROM claims")}
                native_ids = {key.session_id.casefold() for key in assigned.keys() | entries.keys()}
                next_suffix: dict[str, int] = {}
                for key in sorted(entries, key=lambda key: (key.tool, key.home, key.session_id)):
                    if key in assigned:
                        continue
                    base = _base_slug(entries[key])
                    suffix = next_suffix.get(base, 1)
                    slug = base if suffix == 1 else f"{base}-{suffix}"
                    while slug in reserved or slug in native_ids:
                        suffix += 1
                        slug = f"{base}-{suffix}"
                    next_suffix[base] = suffix + 1
                    self._claim(connection, key, slug)
                    assigned[key] = slug
                    reserved.add(slug)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return {key: assigned[key] for key in entries}

    def lookup(self, slug: str) -> NativeKey | None:
        if not self.path.exists():
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tool, session_id, home FROM sessions WHERE slug=? COLLATE NOCASE", (slug,)
            ).fetchone()
        return NativeKey(*row) if row else None

    def claim_owner(self, slug: str) -> NativeKey | None:
        """Include retired claims, so a legacy title cannot steal an old command."""
        if not self.path.exists():
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tool, session_id, home FROM claims WHERE slug=? COLLATE NOCASE", (slug,)
            ).fetchone()
        return NativeKey(*row) if row else None

    def slug_for(self, key: NativeKey) -> str | None:
        if not self.path.exists():
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT slug FROM sessions WHERE tool=? AND session_id=? AND home=?",
                (key.tool, key.session_id, key.home),
            ).fetchone()
        return row[0] if row else None

    def set_slug(self, key: NativeKey, slug: str) -> str:
        slug = _validate_slug(slug)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owner = connection.execute(
                    "SELECT tool, session_id, home FROM claims WHERE slug=? COLLATE NOCASE", (slug,)
                ).fetchone()
                if owner and NativeKey(*owner) != key:
                    raise ValueError(f"slug {slug!r} is already assigned or reserved")
                conflicts = connection.execute(
                    "SELECT tool, session_id, home FROM sessions WHERE lower(session_id)=?", (slug,)
                ).fetchall()
                if any(NativeKey(*row) != key for row in conflicts):
                    raise ValueError(f"slug {slug!r} conflicts with a native session ID")
                self._claim(connection, key, slug)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return slug


def assign_sessions(items: Iterable[Session], store: SlugStore) -> None:
    items = list(items)
    keys = {item.key: native_key(item.tool, item.session_id) for item in items}
    assigned = store.ensure((keys[item.key], item.title) for item in items)
    for item in items:
        item.slug = assigned[keys[item.key]]


def guard_reference(
    reference: str, matched_keys: Iterable[NativeKey], store: SlugStore
) -> NativeKey | None:
    """Read current slug ownership; never fall through a retired or foreign claim."""
    tool, separator, token = reference.partition(":")
    if not separator:
        token, tool = tool, ""
    token, marker, home_hash = token.rpartition("@") if "@" in token else (token, "", "")
    owner = store.claim_owner(token)
    if owner is None:
        return None
    exact = set(matched_keys)
    if tool and owner.tool.casefold() != tool.casefold():
        raise ValueError(f"session slug {token!r} belongs to another harness")
    if marker:
        expected = hashlib.sha256(owner.home.encode()).hexdigest()[:10]
        if expected != home_hash.casefold():
            raise ValueError(f"session slug {token!r} belongs to another harness home")
    current = store.slug_for(owner)
    if current != token.casefold():
        # A native ID stays valid even if the user once chose it as its own slug.
        if token == owner.session_id and exact == {owner}:
            return None
        raise ValueError(f"session slug {token!r} is retired; use {current!r} or its native ID")
    if exact and exact != {owner}:
        raise ValueError(f"ambiguous session reference {reference!r}; use a unique current slug")
    return owner


def resolve_session(
    reference: str, items: Iterable[Session], *, store: SlugStore | None = None
) -> Session:
    items = list(items)
    if not reference:
        raise ValueError("session reference must not be empty")
    folded = reference.casefold()
    exact = [
        item
        for item in items
        if reference in (item.session_id, item.key)
        or (item.slug and folded in (item.slug.casefold(), f"{item.tool}:{item.slug}".casefold()))
    ]
    if store is not None:
        owner = guard_reference(
            reference, (native_key(item.tool, item.session_id) for item in exact), store
        )
        if owner is not None:
            exact = [item for item in items if native_key(item.tool, item.session_id) == owner]
            if not exact:
                raise ValueError(f"session slug {reference!r} is unavailable in this harness home")
    matches = exact or [item for item in items if item.named and item.title.casefold() == folded]
    if not matches:
        raise ValueError(f"no session with ID, slug or name {reference!r}")
    if len(matches) != 1:
        raise ValueError(
            f"ambiguous session {reference!r}; use its unique slug or qualified native ID"
        )
    return matches[0]
