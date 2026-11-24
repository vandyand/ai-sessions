"""Durable human discussion, immutable card versions and leased clarification jobs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from ..fastjson import write_atomic
from .decisions import core_fingerprint, validate_details
from .store import ConflictError

LEASE_SECONDS = 240
MAX_TEXT = 12000


def setup(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS desk_card_versions (desk TEXT COLLATE NOCASE,key TEXT,version TEXT,data TEXT NOT NULL,created REAL,PRIMARY KEY(desk,key,version))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS desk_discussion (desk TEXT COLLATE NOCASE, id TEXT PRIMARY KEY, key TEXT, data TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS desk_discussion_key ON desk_discussion(desk,key)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS desk_decision_drafts (desk TEXT COLLATE NOCASE,key TEXT,data TEXT NOT NULL,PRIMARY KEY(desk,key))"
    )


def _rows(db, name, key=None):
    sql = "SELECT data FROM desk_discussion WHERE desk=?"
    args = (name,)
    if key is not None:
        sql += " AND key=?"
        args += (key,)
    return [json.loads(r[0]) for r in db.execute(sql + " ORDER BY rowid", args)]


def _put(db, name, row):
    db.execute(
        "INSERT INTO desk_discussion VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (name, row["id"], row["key"], json.dumps(row, ensure_ascii=False, allow_nan=False)),
    )


def card(snapshot, key):
    core = next((d for d in snapshot["decisions"] if d["key"] == key), None)
    if core is None:
        raise ConflictError("Decision is no longer listed; your draft is saved")
    return dict(decision=core, details=snapshot.get("decision_details", {}).get(key))


def card_version(snapshot, key):
    value = card(snapshot, key)
    # Evidence clocks do not invalidate a human's view of an unchanged question.
    content = [core_fingerprint(value["decision"]), value["decision"]["status"], value["details"]]
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def capture(db, snapshot):
    for core in snapshot["decisions"]:
        key = core["key"]
        db.execute(
            "INSERT OR IGNORE INTO desk_card_versions VALUES (?,?,?,?,?)",
            (
                snapshot["name"],
                key,
                card_version(snapshot, key),
                json.dumps(card(snapshot, key), ensure_ascii=False),
                time.time(),
            ),
        )


def versions(store, name, key):
    with store._connect() as db:
        return [
            dict(version=v, card=json.loads(data), created_at=stamp)
            for v, data, stamp in db.execute(
                "SELECT version,data,created FROM desk_card_versions WHERE desk=? AND key=? ORDER BY created,rowid",
                (name, key),
            )
        ]


def hydrate(db, snapshot):
    snapshot["decision_discussion"], snapshot["decision_drafts"] = {}, {}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='desk_discussion'").fetchone():
        return
    for row in _rows(db, snapshot["name"]):
        snapshot["decision_discussion"].setdefault(row["key"], []).append(row)
    for key, data in db.execute(
        "SELECT key,data FROM desk_decision_drafts WHERE desk=?", (snapshot["name"],)
    ):
        snapshot["decision_drafts"][key] = json.loads(data)
    for key, rows in snapshot["decision_discussion"].items():
        core = next((d for d in snapshot["decisions"] if d["key"] == key), None)
        if core is None:
            continue
        # A newer core invalidates an overlay, but never erases the discussion.
        latest = next(
            (r for r in reversed(rows) if r.get("updated_details") and r["status"] == "complete"),
            None,
        )
        if latest and latest["core_hash"] == core_fingerprint(core):
            snapshot["decision_details"][key] = latest["updated_details"]


def bind_waiting(db, snapshot):
    """Only an explicit Desk binding supplies a missing recipient, never transport lookup."""
    from .notifications import _targets

    targets = _targets(snapshot)
    if not targets:
        return
    for row in _rows(db, snapshot["name"]):
        if row.get("delivery") == "queued" and not row.get("recipient"):
            row["recipient"] = targets[0]
            _put(db, snapshot["name"], row)


def save_draft(store, name, key, text, version):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError("Notes are limited to 12,000 characters")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = store._get(db, name)
        card(snapshot, key)
        db.execute(
            "INSERT OR REPLACE INTO desk_decision_drafts VALUES (?,?,?)",
            (snapshot["name"], key, json.dumps(dict(text=text, version=version))),
        )


def submit(store, name, key, kind, text, version):
    if (
        kind not in ("note", "clarification", "decision")
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_TEXT
    ):
        raise ValueError("Enter a note, question or answer (up to 12,000 characters)")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = store._get(db, name)
        if (
            card_version(snapshot, key) != version
            or card(snapshot, key)["decision"]["status"] != "pending"
        ):
            raise ConflictError(
                "Decision changed; your draft is saved. Close the editor to review the latest card"
            )
        capture(db, snapshot)
        # Serialize clarification rounds for this decision; notes remain available.
        if kind == "clarification" and any(
            r["kind"] == kind and r["status"] in ("queued", "working", "waiting")
            for r in _rows(db, name, key)
        ):
            raise ConflictError("A clarification is already in progress; your draft is saved")
        row = dict(
            schema_version=1,
            id=uuid.uuid4().hex,
            key=key,
            kind=kind,
            text=text,
            card=card(snapshot, key),
            version=version,
            core_hash=core_fingerprint(card(snapshot, key)["decision"]),
            created_at=time.time(),
            status="saved" if kind == "note" else "queued",
            reply=None,
            updated_details=None,
            delivery="queued" if kind == "decision" else None,
        )
        from .notifications import _targets

        targets = _targets(snapshot)
        if targets and kind != "note":
            row["recipient"] = targets[0]
        _put(db, snapshot["name"], row)
        db.execute(
            "DELETE FROM desk_decision_drafts WHERE desk=? AND key=?", (snapshot["name"], key)
        )
        snapshot.update(revision=snapshot["revision"] + 1, updated_at=time.time())
        store._write(db, snapshot)
    try:
        export(store, name, row["id"])
    except OSError:
        pass  # SQLite already committed; the outbox will retry projection before delivery.
    return row


def claim(store, name):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        for row in _rows(db, name):
            if row["kind"] != "clarification":
                continue
            if row["status"] == "working" and row.get("started_at", 0) + LEASE_SECONDS < now:
                row.update(
                    status="failed", error="Clarification was interrupted. Retry when ready."
                )
                _put(db, name, row)
            if row["status"] != "queued":
                continue
            row.update(status="working", token=uuid.uuid4().hex, started_at=now)
            _put(db, name, row)
            return row
    return None


def finish(store, name, job, *, reply, details, question=None):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = next((r for r in _rows(db, name) if r["id"] == job["id"]), None)
        if not row or row["status"] != "working" or row.get("token") != job.get("token"):
            raise ConflictError("Clarification claim expired")
        snapshot = store._get(db, name)
        try:
            current = card_version(snapshot, job["key"]) == job["version"]
        except ConflictError:
            current = False
        if details is not None:
            validate_details(details)
        row.setdefault("responses", []).append(
            dict(
                created_at=time.time(),
                reply=reply,
                details=details,
                question_for_agent=question,
                outdated=not current,
            )
        )
        row.update(
            reply=reply, updated_details=details, status="complete" if current else "outdated"
        )
        if question and current and not row.get("agent_reply"):
            row.update(status="waiting", context_question=question, delivery="queued")
            # Preserve current card until the requested context arrives.
            row["updated_details"] = None
        _put(db, name, row)
        bind_waiting(db, snapshot)
        capture(db, store._get(db, name))
        snapshot.update(revision=snapshot["revision"] + 1, updated_at=time.time())
        store._write(db, snapshot)
    try:
        export(store, name, job["id"])
    except OSError:
        pass  # Never turn a committed reply into a failed/duplicate clarification.


def update(store, name, ident, **values):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = next(r for r in _rows(db, name) if r["id"] == ident)
        row.update(values)
        _put(db, name, row)
    return row


def fail_claim(store, name, job):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = next((r for r in _rows(db, name) if r["id"] == job["id"]), None)
        if row and row["status"] == "working" and row.get("token") == job.get("token"):
            row.update(
                status="failed",
                error="Clarification unavailable. Your question is saved; retry when ready.",
            )
            _put(db, name, row)


def retry(store, name, ident):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = next(r for r in _rows(db, name) if r["id"] == ident)
        if row["kind"] != "clarification" or row["status"] != "failed":
            raise ValueError("Only a failed clarification can be retried")
        if any(
            other["id"] != ident
            and other["key"] == row["key"]
            and other["kind"] == "clarification"
            and other["status"] in ("queued", "working", "waiting")
            for other in _rows(db, name)
        ):
            raise ConflictError("Another clarification is already in progress")
        snapshot = store._get(db, name)
        if card_version(snapshot, row["key"]) != row["version"]:
            raise ConflictError("Decision changed; ask a new question on the current card")
        row.update(status="queued", error=None)
        _put(db, name, row)


def agent_reply(store, name, ident, text, sender):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("Supply a reply of at most 12,000 characters")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        from .notifications import _targets

        row = next((r for r in _rows(db, name) if r["id"] == ident), None)
        if (
            not row
            or row["status"] != "waiting"
            or row.get("recipient") != sender
            or sender not in _targets(store._get(db, name))
        ):
            raise ValueError(
                "Reply must come from the bound Desk conversation requested for this question"
            )
        row.update(agent_reply=text, agent_replied_at=time.time(), status="queued")
        _put(db, name, row)


def export(store, name, ident):
    with store._connect() as db:
        rows = _rows(db, name)
    selected = next(r for r in rows if r["id"] == ident)
    path = store.folder(name) / (
        "discussion-" + hashlib.sha256(selected["key"].encode()).hexdigest()[:24] + ".json"
    )
    write_atomic(
        path,
        dict(
            schema_version=1,
            desk=name,
            entries=[r for r in rows if r["key"] == selected["key"]],
            versions=versions(store, name, selected["key"]),
        ),
    )
    return path
