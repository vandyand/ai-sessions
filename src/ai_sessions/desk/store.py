"""Transactional desk state; Markdown files are replaceable projections of SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..harnesses.desk_support import TOOLS
from ..paths import APP_CONFIG_DIR, env_path
from .time_format import eastern_time

MAX_PENDING_COUNT = 80
MAX_PENDING_CHARS = 24000
MAX_TERMINAL_COUNT = 20
OVERVIEW_FIELDS = ("background", "current", "direction", "goal")
MAX_OVERVIEW_FIELD_CHARS = 6000


def validate_overview(overview):
    if not isinstance(overview, dict) or set(overview) != set(OVERVIEW_FIELDS):
        raise ValueError("overview requires background, current, direction and goal")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > MAX_OVERVIEW_FIELD_CHARS
        for value in overview.values()
    ):
        raise ValueError("overview values must be nonempty bounded strings")


def _overview_projection(overview):
    """Fixed Markdown headings and inert prose; never mutate the stored model response."""
    if not isinstance(overview, dict) or not overview:
        return ""
    sections = ["## Overview"]
    for field in OVERVIEW_FIELDS:
        value = overview.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        # Remove terminal CSI controls, flatten whitespace, and quote Markdown syntax
        # so model-supplied headings/fences cannot reshape the exported document.
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        value = "".join(ch for ch in value if not ord(ch) < 32 or ch in "\n\r\t")
        value = " ".join(value.split())
        value = re.sub(r"([\\`*_\[\]<>#])", r"\\\1", value)
        sections.append(f"### {field.title()}\n{value}")
    return "\n\n".join(sections)


class ConflictError(RuntimeError):
    """Another observation won the revision compare-and-swap."""


def _when(value):
    return (
        eastern_time(datetime.fromtimestamp(value, timezone.utc), seconds=True)
        if isinstance(value, (int, float))
        else "not observed"
    )


def validate_decisions(decisions):
    if not isinstance(decisions, list) or len(decisions) > 100:
        raise ValueError("decisions must be a list of at most 100 items")
    keys = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("decision must be an object")
        if set(item) != {"key", "question", "context", "recommendation", "status", "evidence"}:
            raise ValueError("decision must contain exactly the documented fields")
        for field in ("key", "question", "context", "recommendation", "status"):
            if not isinstance(item.get(field), str) or len(item[field]) > 8000:
                raise ValueError(f"invalid decision {field}")
        if not item["key"].strip() or not item["question"].strip() or item["key"] in keys:
            raise ValueError("decision keys must be nonempty and unique; questions required")
        keys.add(item["key"])
        if item["status"] not in {"pending", "resolved", "superseded"}:
            raise ValueError("invalid decision status")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence or len(evidence) > 100:
            raise ValueError("decision evidence must be a nonempty list")
        if any(not isinstance(e, str) or not e.strip() or len(e) > 1000 for e in evidence):
            raise ValueError("evidence must contain short citation strings")


def bounded_decisions(decisions):
    """Keep all pending records and a bounded current view of the newest closed ones."""
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("decisions must be a list of objects")
    pending = [d for d in decisions if d.get("status") == "pending"]
    if len(pending) > MAX_PENDING_COUNT:
        raise ValueError("pending decision count exceeds sustainable synthesis budget")
    serialized = json.dumps(pending, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(serialized) > MAX_PENDING_CHARS:
        raise ValueError("pending decision text exceeds sustainable synthesis budget")
    terminal = [d for d in decisions if d.get("status") != "pending"]
    bounded = pending + terminal[-MAX_TERMINAL_COUNT:]
    validate_decisions(bounded)
    return bounded


class Store:
    def __init__(self, root=None, *, readonly=False):
        self.root = (
            Path(root)
            if root is not None
            else env_path("AI_SESSIONS_DESK_DIR", APP_CONFIG_DIR / "desk")
        )
        self.path = self.root / "desk.sqlite3"
        self.readonly = readonly
        if readonly:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS desks "
                "(name TEXT PRIMARY KEY COLLATE NOCASE, data TEXT NOT NULL)"
            )
            from .notifications import create_tables

            create_tables(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS decision_details "
                "(desk TEXT COLLATE NOCASE, key TEXT, core_hash TEXT, context_hash TEXT, "
                "version INTEGER, data TEXT NOT NULL, PRIMARY KEY(desk,key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS decision_enrichment_attempts "
                "(desk TEXT COLLATE NOCASE, key TEXT, core_hash TEXT, context_hash TEXT, "
                "version INTEGER, status TEXT, checked_at REAL, source_hash TEXT, "
                "PRIMARY KEY(desk,key,core_hash,context_hash,version))"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro" if self.readonly else self.path,
            timeout=15,
            uri=self.readonly,
        )
        try:
            db.execute("PRAGMA busy_timeout=15000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _name(name):
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
        }
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[\w-]{1,80}", name, re.ASCII)
            or name.upper() in reserved
        ):
            raise ValueError("desk name must contain 1–80 ASCII letters, digits, _ or -")
        return name

    @staticmethod
    def _get(db, name):
        row = db.execute("SELECT data FROM desks WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        snapshot = json.loads(row[0])
        snapshot["decision_details"] = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='decision_details'").fetchone():
            from .decisions import core_fingerprint, validate_details, validate_evidence

            cores = {d["key"]: d for d in snapshot["decisions"]}
            for key, fingerprint, value in db.execute(
                "SELECT key,core_hash,data FROM decision_details WHERE desk=?", (name,)
            ):
                if key not in cores or core_fingerprint(cores[key]) != fingerprint:
                    continue
                try:
                    details = json.loads(value)
                    validate_details(details)
                    validate_evidence(details["evidence"], snapshot["sources"])
                    snapshot["decision_details"][key] = details
                except (ValueError, TypeError, KeyError):
                    continue
        return snapshot

    @staticmethod
    def _write(db, snapshot):
        db.execute(
            "UPDATE desks SET data=? WHERE name=?",
            (
                json.dumps(
                    {k: v for k, v in snapshot.items() if k != "decision_details"},
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                snapshot["name"],
            ),
        )

    def create(self, name, sources, cwd):
        self._name(name)
        if not isinstance(sources, list) or not sources or len(sources) > 32:
            raise ValueError("choose between 1 and 32 explicit native sources")
        chosen = []
        for source in sources:
            if not isinstance(source, dict) or any(
                not isinstance(source.get(k), str) or not source[k].strip()
                for k in ("tool", "session_id", "storage")
            ):
                raise ValueError("sources require tool, session_id and storage")
            ref = {k: source[k] for k in ("tool", "session_id", "storage")}
            for field in ("role", "title", "home"):
                if field in source:
                    if not isinstance(source[field], str) or len(source[field]) > 2000:
                        raise ValueError(f"source {field} must be short text")
                    ref[field] = source[field]
            if "home" in ref and not ref["home"].strip():
                raise ValueError("source home must be nonempty text")
            if not any(
                all(ref.get(k) == item.get(k) for k in ("tool", "session_id", "storage", "home"))
                for item in chosen
            ):
                chosen.append(ref)
        snapshot = dict(
            name=name,
            cwd=str(cwd),
            revision=0,
            sources=chosen,
            review="",
            overview={},
            decisions=[],
            decision_details={},
            observed_at=None,
            updated_at=time.time(),
            error=None,
            observation={},
            seen_at=None,
            agents={},
            attempted_at=None,
            attempted_coverage=[],
        )
        with self._connect() as db:
            try:
                db.execute("INSERT INTO desks VALUES (?, ?)", (name, json.dumps(snapshot)))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"desk already exists: {name}") from exc
        return snapshot

    def get(self, name):
        with self._connect() as db:
            return self._get(db, name)

    def list(self):
        if self.readonly and not self.path.exists():
            return []
        with self._connect() as db:
            return [
                self._get(db, row[0]) for row in db.execute("SELECT name FROM desks ORDER BY name")
            ]

    def update_primary_source(self, name, expected_revision, source):
        """Replace the operational target without disturbing dialogue or team sources."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(source, dict) or any(
            not isinstance(source.get(key), str) or not source[key].strip()
            for key in ("tool", "session_id", "storage")
        ):
            raise ValueError("sources require tool, session_id and storage")
        source = {
            key: source[key]
            for key in ("tool", "session_id", "storage", "title", "home")
            if key in source
        }
        if "home" in source and (
            not isinstance(source["home"], str)
            or not source["home"].strip()
            or len(source["home"]) > 2000
        ):
            raise ValueError("source home must be nonempty short text")
        if "title" in source and (
            not isinstance(source["title"], str) or len(source["title"]) > 2000
        ):
            raise ValueError("source title must be short text")
        source["role"] = "supervisor"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            if snapshot["revision"] != expected_revision:
                raise ConflictError(f"desk {name} changed during target selection")
            if snapshot["sources"][0] == source:
                return snapshot
            snapshot["sources"][0] = source
            snapshot.update(revision=expected_revision + 1, updated_at=time.time())
            self._write(db, snapshot)
        return snapshot

    def publish(self, name, expected_revision, observation, review, decisions, *, overview=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(review, str) or len(review) > 64000:
            raise ValueError("review must be text of at most 64000 characters")
        # Remember even terminal keys omitted from the bounded display projection.
        all_keys = (
            [item.get("key") for item in decisions if isinstance(item, dict)]
            if isinstance(decisions, list)
            else []
        )
        decisions = bounded_decisions(decisions)
        if any(not isinstance(key, str) or not key.strip() or len(key) > 8000 for key in all_keys):
            raise ValueError("invalid decision key")
        if not isinstance(observation, dict):
            raise ValueError("observation must be an object")
        # Copy caller-owned objects and reject non-JSON metadata before acquiring a write lock.
        observation, decisions = json.loads(json.dumps([observation, decisions], allow_nan=False))
        if overview is not None:
            validate_overview(overview)
            overview = json.loads(json.dumps(overview, allow_nan=False))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            if snapshot["revision"] != expected_revision:
                raise ConflictError(f"desk {name} changed during observation")
            from .notifications import record_publication

            record_publication(db, snapshot, decisions, all_keys)
            now = time.time()
            if overview is not None:
                snapshot["overview"] = overview
            snapshot.update(
                revision=expected_revision + 1,
                observation=observation,
                review=review,
                decisions=decisions,
                observed_at=observation.get("observed_at", now),
                updated_at=now,
                error=None,
                attempted_at=now,
                attempted_coverage=observation.get("sources", []),
            )
            self._write(db, snapshot)
            # An older publisher omits details. The sidecar remains authoritative;
            # material ask changes are filtered rather than copied into core JSON.
            snapshot = self._get(db, name)
            from .decisions import core_fingerprint

            hashes = {d["key"]: core_fingerprint(d) for d in decisions}
            for key, fingerprint in db.execute(
                "SELECT key,core_hash FROM decision_enrichment_attempts WHERE desk=?", (name,)
            ).fetchall():
                if hashes.get(key) != fingerprint:
                    db.execute(
                        "DELETE FROM decision_enrichment_attempts WHERE desk=? AND key=? AND core_hash=?",
                        (name, key, fingerprint),
                    )
            for (key,) in db.execute(
                "SELECT key FROM decision_details WHERE desk=?", (name,)
            ).fetchall():
                if key not in hashes:
                    db.execute("DELETE FROM decision_details WHERE desk=? AND key=?", (name, key))
        return snapshot

    def decision_enrichment_order(self, name):
        with self._connect() as db:
            return dict(
                db.execute(
                    "SELECT key,MAX(checked_at) FROM decision_enrichment_attempts WHERE desk=? GROUP BY key",
                    (name,),
                )
            )

    def claim_decision_enrichment(
        self, name, key, core_hash, context_hash, *, force=False, source_hash=None
    ):
        """Claim before calling the model; cached failures and interrupted claims stay cached."""
        from .decisions import SCHEMA_VERSION, core_fingerprint, source_fingerprint

        if not isinstance(context_hash, str) or not context_hash or len(context_hash) > 200:
            raise ValueError("invalid context fingerprint")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            core = next((d for d in snapshot["decisions"] if d["key"] == key), None)
            if core is None or core["status"] != "pending" or core_fingerprint(core) != core_hash:
                return False
            current_sources = source_fingerprint(snapshot)
            if source_hash is not None and source_hash != current_sources:
                return False
            args = (name, key, core_hash, context_hash, SCHEMA_VERSION)
            existing = db.execute(
                "SELECT status FROM decision_enrichment_attempts WHERE desk=? AND key=? "
                "AND core_hash=? AND context_hash=? AND version=?",
                args,
            ).fetchone()
            if existing and not force:
                db.execute(
                    "UPDATE decision_enrichment_attempts SET checked_at=? WHERE desk=? AND key=? "
                    "AND core_hash=? AND context_hash=? AND version=?",
                    (time.time(), *args),
                )
                return False
            db.execute(
                "INSERT OR REPLACE INTO decision_enrichment_attempts VALUES (?,?,?,?,?,?,?,?)",
                (*args, "claimed", time.time(), current_sources),
            )
            # Bound changing context histories while preserving every pending core/detail.
            db.execute(
                "DELETE FROM decision_enrichment_attempts WHERE desk=? AND key=? AND rowid NOT IN "
                "(SELECT rowid FROM decision_enrichment_attempts WHERE desk=? AND key=? "
                "ORDER BY checked_at DESC LIMIT 16)",
                (name, key, name, key),
            )
            return True

    def finish_decision_enrichment(self, name, key, core_hash, context_hash):
        from .decisions import SCHEMA_VERSION

        with self._connect() as db:
            db.execute(
                "UPDATE decision_enrichment_attempts SET status='failed' WHERE desk=? AND key=? "
                "AND core_hash=? AND context_hash=? AND version=? AND status!='success'",
                (name, key, core_hash, context_hash, SCHEMA_VERSION),
            )

    def publish_decision_details(
        self, name, expected_revision, key, core_hash, context_hash, details
    ):
        """Commit details and revision together, without touching core status or notifications."""
        from .decisions import (
            SCHEMA_VERSION,
            core_fingerprint,
            source_fingerprint,
            validate_details,
            validate_evidence,
        )

        validate_details(details)
        details = json.loads(json.dumps(details, allow_nan=False))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            core = next((d for d in snapshot["decisions"] if d["key"] == key), None)
            if (
                type(expected_revision) is not int
                or expected_revision < 0
                or expected_revision > snapshot["revision"]
                or core is None
                or core["status"] != "pending"
                or core_fingerprint(core) != core_hash
            ):
                raise ConflictError("decision changed during enrichment")
            validate_evidence(details["evidence"], snapshot["sources"])
            args = (name, key, core_hash, context_hash, SCHEMA_VERSION)
            claim = db.execute(
                "SELECT status,source_hash FROM decision_enrichment_attempts WHERE desk=? AND key=? "
                "AND core_hash=? AND context_hash=? AND version=?",
                args,
            ).fetchone()
            if not claim or claim[0] != "claimed" or claim[1] != source_fingerprint(snapshot):
                raise ConflictError("enrichment attempt is no longer claimed")
            db.execute(
                "INSERT OR REPLACE INTO decision_details VALUES (?,?,?,?,?,?)",
                (*args, json.dumps(details, ensure_ascii=False)),
            )
            db.execute(
                "UPDATE decision_enrichment_attempts SET status='success' WHERE desk=? AND key=? "
                "AND core_hash=? AND context_hash=? AND version=?",
                args,
            )
            snapshot.update(revision=snapshot["revision"] + 1, updated_at=time.time())
            self._write(db, snapshot)
            return self._get(db, name)

    def fail(self, name, error):
        return self._fail(name, error)

    def _fail(self, name, error, expected_revision=None, coverage=None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            if expected_revision is not None and snapshot["revision"] != expected_revision:
                return snapshot
            snapshot.update(
                error=str(error)[:2000],
                updated_at=time.time(),
                revision=snapshot["revision"] + 1,
                attempted_at=time.time(),
            )
            if coverage is not None:
                snapshot["attempted_coverage"] = json.loads(json.dumps(coverage, allow_nan=False))
            self._write(db, snapshot)
        return snapshot

    def seen(self, name):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            snapshot["seen_at"] = time.time()
            self._write(db, snapshot)
        return snapshot

    def folder(self, name):
        """Safe per-desk directory for projections and launcher instructions."""
        self._name(name)
        snapshot = self.get(name)
        directory = self.root / snapshot["name"]
        directory.mkdir(exist_ok=True)
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise ValueError("desk directory must stay inside the desk root")
        return directory

    def bind_agent(self, name, tool, session_id, home):
        """Bind a native human conversation once; concurrent alternative IDs refuse."""
        if tool not in TOOLS or not isinstance(session_id, str) or not session_id:
            raise ValueError("agent requires claude/codex and a nonempty session ID")
        binding = {"session_id": session_id, "home": str(home)}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            agents = snapshot.setdefault("agents", {})
            if tool in agents:
                if agents[tool] != binding:
                    raise ConflictError(f"desk {name} already has a {tool} conversation")
                return snapshot
            agents[tool] = binding
            snapshot.update(revision=snapshot["revision"] + 1, updated_at=time.time())
            self._write(db, snapshot)
        return snapshot

    def export(self, name):
        """Serialize exporters with writers; each UTF-8 projection is atomically replaced.

        The pair is not a filesystem transaction: revision headers allow detection of a
        crash between replacements. SQLite remains authoritative.
        """
        directory = self.folder(name)
        paths = {"review": directory / "review.md", "decisions": directory / "decisions.md"}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = self._get(db, name)
            header = (
                f"Desk: {name} | revision: {snapshot['revision']}\n"
                f"Observed: {_when(snapshot['observed_at'])} | "
                f"Updated: {_when(snapshot['updated_at'])}\n"
                f"Model synthesis: {_when(snapshot['observation'].get('synthesized_at'))}\n"
                "Model synthesis; source statements are not independent verification.\n"
            )
            if snapshot["error"]:
                header += f"Stale / error: {snapshot['error']}\n"
                header += f"Last attempt: {_when(snapshot.get('attempted_at'))}\n"
                for source in snapshot.get("attempted_coverage", []):
                    header += (
                        f"Source {source['tool']}:{source['session_id']}: "
                        f"{source.get('status', 'unknown')}\n"
                    )
            if snapshot["observation"].get("partial"):
                header += "Partial coverage: history was omitted or a source is unavailable.\n"
            if snapshot["observation"].get("limitations"):
                header += f"Coverage: {snapshot['observation']['limitations']}\n"
            for source in snapshot["observation"].get("sources", []):
                detail = "partial" if source.get("partial") else "incremental"
                header += (
                    f"Source {source['tool']}:{source['session_id']}: "
                    f"{source.get('status', 'unknown')}, {detail}; "
                    f"observed {_when(source.get('observed_at'))}; "
                    f"modified {_when(source.get('source_modified_at'))}\n"
                )
            pending = [d for d in snapshot["decisions"] if d["status"] == "pending"]
            text = (
                "\n\n".join(
                    f"## {d['question']}\n{d['context']}\nRecommendation: {d['recommendation']}\n"
                    + "Evidence: "
                    + ", ".join(d["evidence"])
                    for d in pending
                )
                or "No pending questions recorded."
            )
            if snapshot.get("decision_details"):
                from .decisions import markdown_details, markdown_text

                text = "\n\n".join(
                    "## "
                    + markdown_text(snapshot["decision_details"][d["key"]]["summary"])
                    + "\n"
                    + markdown_details(snapshot["decision_details"][d["key"]])
                    if d["key"] in snapshot["decision_details"]
                    else f"## {d['question']}\n{d['context']}\nRecommendation: {d['recommendation']}\n"
                    + "Evidence: "
                    + ", ".join(d["evidence"])
                    for d in pending
                )
            review = snapshot["review"] or "Not observed yet."
            overview_text = _overview_projection(snapshot.get("overview"))
            if overview_text:
                review = overview_text + "\n\n## Review\n" + review
            for kind, content in (
                ("review", review),
                ("decisions", text),
            ):
                fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                        handle.write(header + "\n" + content + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, paths[kind])
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        return paths
