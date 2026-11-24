"""Durable request history and bounded, non-destructive model working sets."""

from __future__ import annotations

import hashlib
import json
import re
import time

WINDOW_COUNT = 20
WINDOW_CHARS = 24000
STALE_SECONDS = 7 * 86400


def setup(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_archive_migrations (desk TEXT PRIMARY KEY COLLATE NOCASE)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_history ("
        "desk TEXT COLLATE NOCASE,key TEXT,version TEXT,data TEXT,details TEXT,"
        "recorded_at REAL,event TEXT,note TEXT,PRIMARY KEY(desk,key,version))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_lifecycle ("
        "desk TEXT COLLATE NOCASE,key TEXT,content_hash TEXT,first_seen REAL,"
        "last_changed REAL,imported INTEGER,PRIMARY KEY(desk,key))"
    )


def record(db, snapshot, *, imported=False, note=None):
    from .discussion import capture

    capture(db, snapshot)
    now = time.time()
    for decision in snapshot["decisions"]:
        key = decision["key"]
        details = snapshot.get("decision_details", {}).get(key)
        encoded = json.dumps(decision, ensure_ascii=False, sort_keys=True)
        content = json.dumps(
            {k: decision[k] for k in ("question", "context", "recommendation", "status")},
            sort_keys=True,
        )
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        version = hashlib.sha256(
            json.dumps([decision, details, note], sort_keys=True).encode()
        ).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO decision_history VALUES (?,?,?,?,?,?,?,?)",
            (
                snapshot["name"],
                key,
                version,
                encoded,
                json.dumps(details, ensure_ascii=False),
                now,
                "imported" if imported else decision["status"],
                note,
            ),
        )
        db.execute(
            "INSERT INTO decision_lifecycle VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(desk,key) DO UPDATE SET content_hash=excluded.content_hash,"
            "last_changed=excluded.last_changed,imported=0 "
            "WHERE content_hash!=excluded.content_hash",
            (snapshot["name"], key, content_hash, now, now, int(imported)),
        )


def audit(snapshot, *, now=None):
    now = time.time() if now is None else now
    seen, rows = {}, []
    for decision in snapshot.get("decisions", []):
        if decision["status"] != "pending":
            continue
        key = decision["key"]
        summary = (
            snapshot.get("decision_details", {}).get(key, {}).get("summary") or decision["question"]
        )
        normalized = " ".join(summary.casefold().split())
        flags = []
        if normalized in seen:
            flags.append("Duplicate wording")
        seen[normalized] = key
        meta = snapshot.get("decision_meta", {}).get(key, {})
        if meta.get("last_changed") is not None and now - meta["last_changed"] >= STALE_SECONDS:
            flags.append("Unchanged for at least 7 days")
        if re.match(r"^(?:worker|manager|agent)\s+[\w-]+\s*[:,]", summary, re.I):
            flags.append("Possible team coordination")
        rows.append(
            dict(
                number=len(rows) + 1,
                key=key,
                summary=summary,
                flags=flags,
                age_known=bool(meta and not meta.get("imported")),
            )
        )
    return rows


def working_set(snapshot, evidence=()):
    pending = [d for d in snapshot["decisions"] if d["status"] == "pending"]
    if not pending:
        return []
    offset = snapshot.get("observation", {}).get("decision_window_offset", 0) % len(pending)
    rotating = pending[offset:] + pending[:offset]
    # Reserve half the window for exact textual links to current conversation;
    # the rest rotates so old requests cannot be crowded out by newer ones.
    words = set(re.findall(r"\b\w{4,}\b", " ".join(e.get("text", "") for e in evidence).casefold()))
    stop = {"this", "that", "with", "from", "have", "what", "which", "should", "would", "please"}

    def score(d):
        text = (
            snapshot.get("decision_details", {}).get(d["key"], {}).get("summary") or d["question"]
        )
        return len((set(re.findall(r"\b\w{4,}\b", text.casefold())) - stop) & words)

    relevant = sorted(rotating, key=score, reverse=True)
    priority = [d for d in relevant if score(d)][: WINDOW_COUNT // 2]
    ordered = rotating[: WINDOW_COUNT // 2] + priority + rotating[WINDOW_COUNT // 2 :]
    result, seen, used = [], set(), 2
    for decision in ordered:
        if decision["key"] in seen:
            continue
        seen.add(decision["key"])
        compact = dict(decision)
        if len(json.dumps(compact, ensure_ascii=False)) > WINDOW_CHARS // 4:
            for field, limit in (("question", 1600), ("context", 1600), ("recommendation", 800)):
                if len(compact[field]) > limit:
                    compact[field] = compact[field][:limit] + " [excerpt; full request retained]"
            compact["evidence"] = [value[:500] for value in compact["evidence"][:2]]
        cost = len(json.dumps(compact, ensure_ascii=False)) + 1
        if used + cost > WINDOW_CHARS:
            continue
        result.append(compact)
        used += cost
        if len(result) == WINDOW_COUNT:
            break
    return result
