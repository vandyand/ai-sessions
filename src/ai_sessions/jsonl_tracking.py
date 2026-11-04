"""Adapter-owned JSONL bookmarks resilient to formatting and metadata rewrites."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import fastjson
from .model import NativeRef
from .paths import APP_CACHE_DIR, STATE_FILE

PREFIX = "jsonl-records-v1:"
Identity = Callable[[dict[str, Any]], Any]
Tolerance = Callable[[dict[str, Any]], bool]

# Verifying a bookmark rereads its transcript from the start, which grows with
# every message.  Where it was last found is remembered across launches, and
# trusted again only while the file is the same one and the bytes just before
# the verified end are unchanged, so an append costs nothing and a rewrite
# still gets the full check.
LOCATIONS_FILE = APP_CACHE_DIR / "bookmark-locations-v1.json"
_LOCATION_ENTRIES = 512
_WINDOW_BYTES = 4096
_locations: dict[str, dict[str, Any]] | None = None
_locations_lock = threading.Lock()


# A bookmark lists one fingerprint per record of its transcript, megabytes for a
# long one, and the user state holds every bookmark and is rewritten on each save.
# Long lists live beside the state instead, named by their own hash, and are read
# only when a bookmark has to be found again in a rewritten transcript.
RECORDS_DIR = STATE_FILE.parent / "checkpoint-records"
_INLINE_RECORDS = 64


def _store_records(records: list[str]) -> str:
    data = "\n".join(records).encode("ascii")
    reference = hashlib.sha256(data).hexdigest()
    path = RECORDS_DIR / f"{reference}.txt"
    try:
        os.utime(path)  # Already stored; touched so the sweep sees it is in use.
        return reference
    except OSError:
        pass
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return reference


def _load_records(reference: object) -> list[str] | None:
    if not isinstance(reference, str) or len(reference) != 64:
        return None
    try:
        data = (RECORDS_DIR / f"{reference}.txt").read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != reference:
        return None  # Damaged; the bookmark cannot be verified from it.
    return data.decode("ascii").split("\n") if data else []


def _encode(saved: dict[str, Any]) -> str:
    records = saved.get("records")
    if isinstance(records, list) and len(records) > _INLINE_RECORDS:
        try:
            saved = {
                **saved,
                "records": [],
                "records_ref": _store_records(records),
                "records_count": len(records),
            }
        except OSError:
            pass  # Kept inline: larger, but complete.
    return PREFIX + json.dumps(saved, separators=(",", ":"))


def externalize_checkpoint(checkpoint: object) -> object:
    """``checkpoint`` with a long inline record list moved beside the state.

    Anything else is returned unchanged, so this is safe on every stored value.
    """
    if not isinstance(checkpoint, str) or len(checkpoint) <= _INLINE_LIMIT:
        return checkpoint
    if not checkpoint.startswith(PREFIX):
        return checkpoint
    try:
        saved = json.loads(checkpoint[len(PREFIX) :])
    except ValueError:
        return checkpoint
    records = saved.get("records") if isinstance(saved, dict) else None
    if not isinstance(records, list) or len(records) <= _INLINE_RECORDS:
        return checkpoint
    return _encode(saved)


# Only a stored bookmark longer than this can hold more records than stay inline.
_INLINE_LIMIT = _INLINE_RECORDS * 67


def has_inline_records(value: object) -> bool:
    """Whether ``value`` (state, at any depth) holds a bookmark with long records inline."""
    if isinstance(value, str):
        return len(value) > _INLINE_LIMIT and value.startswith(PREFIX)
    if isinstance(value, dict):
        return any(has_inline_records(item) for item in value.values())
    if isinstance(value, list):
        return any(has_inline_records(item) for item in value)
    return False


def externalize_checkpoints(value: dict[Any, Any] | list[Any]) -> int:
    """Move every long inline bookmark in ``value`` beside the state, in place.

    Returns how many moved.  The containers themselves stay the same objects, so
    a caller holding one of them still sees the current values.
    """
    moved = 0
    items = value.items() if isinstance(value, dict) else enumerate(value)
    for key, item in list(items):
        if isinstance(item, str):
            if len(item) > _INLINE_LIMIT and item.startswith(PREFIX):
                smaller = externalize_checkpoint(item)
                if smaller != item:
                    value[key] = smaller
                    moved += 1
        elif isinstance(item, (dict, list)):
            moved += externalize_checkpoints(item)
    return moved


def referenced_records(value: object, found: set[str] | None = None) -> set[str]:
    """The record files the bookmarks in ``value`` (state, at any depth) point to."""
    found = set() if found is None else found
    if isinstance(value, str):
        if value.startswith(PREFIX) and '"records_ref"' in value:
            try:
                reference = json.loads(value[len(PREFIX) :]).get("records_ref")
            except (ValueError, AttributeError):
                reference = None
            if isinstance(reference, str):
                found.add(reference)
    elif isinstance(value, dict):
        for item in value.values():
            referenced_records(item, found)
    elif isinstance(value, list):
        for item in value:
            referenced_records(item, found)
    return found


def sweep_records(referenced: set[str], *, older_than: float = 7 * 86400) -> int:
    """Delete record files no bookmark points to and nothing has used for a while.

    Another process may hold a bookmark this state has not seen yet, so a file goes
    only once it is both unreferenced here and untouched for ``older_than`` seconds.
    """
    import time

    removed = 0
    cutoff = time.time() - older_than
    try:
        entries = list(os.scandir(RECORDS_DIR))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not name.endswith(".txt") or name[:-4] in referenced:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _stamp(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def protect_checkpoint(
    ref: NativeRef, offset: int | str, identity: Identity, *, identity_version: int = 1
) -> str | None:
    if isinstance(offset, str):
        return offset if offset.startswith(PREFIX) else None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    path = Path(ref.storage)
    try:
        before = _stamp(path)
        if offset > before[2]:
            return None
        digest = hashlib.sha256()
        fingerprints = []
        with path.open("rb") as handle:
            while handle.tell() < offset:
                raw = handle.readline(offset - handle.tell())
                if not raw.endswith(b"\n"):
                    return None
                record = json.loads(raw)
                if not isinstance(record, dict):
                    return None
                digest.update(raw)
                value = identity(record)
                if value is not None:
                    fingerprints.append(_fingerprint(value))
        if _stamp(path) != before:
            return None
        return _encode(
            {
                "offset": offset,
                "bytes": digest.hexdigest(),
                "records": fingerprints,
                "identity": identity_version,
            }
        )
    except (OSError, ValueError, TypeError):
        return None


def _window(path: Path, end: int) -> str:
    """Digest the head and the bytes just before ``end`` so a rewrite is not mistaken for an append."""
    with path.open("rb") as handle:
        head = handle.read(min(end, _WINDOW_BYTES))
        start = max(0, end - _WINDOW_BYTES)
        handle.seek(start)
        tail = handle.read(end - start)
    return hashlib.sha256(end.to_bytes(8, "big") + head + tail).hexdigest()


def _load_locations() -> dict[str, dict[str, Any]]:
    global _locations
    if _locations is None:
        try:
            document = json.loads(LOCATIONS_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            document = {}
        _locations = document if isinstance(document, dict) else {}
    return _locations


def _save_location(key: str, entry: dict[str, Any]) -> None:
    with _locations_lock:
        locations = _load_locations()
        if locations and next(reversed(locations)) == key and locations[key] == entry:
            return  # Already saved, and already the most recent.
        locations.pop(key, None)
        locations[key] = entry
        while len(locations) > _LOCATION_ENTRIES:
            locations.pop(next(iter(locations)))
        try:
            fastjson.write_atomic(LOCATIONS_FILE, locations)
        except OSError:
            pass


def _location_key(
    storage: str, checkpoint: str, identity: Identity, tolerate: Tolerance | None
) -> str:
    owner = ",".join(
        f"{getattr(hook, '__module__', '')}.{getattr(hook, '__qualname__', '')}"
        for hook in (identity, tolerate)
    )
    return hashlib.sha256(f"{owner}\0{storage}\0{checkpoint}".encode()).hexdigest()


def locate_checkpoint(
    ref: NativeRef, checkpoint: str, identity: Identity, tolerate: Tolerance | None = None
) -> int | None:
    """Where ``checkpoint`` now falls in ``ref``, or None when it cannot be verified.

    ``tolerate`` names records whose fingerprint may differ without breaking
    the bookmark, for rewrites the adapter knows carry no conversation work.
    Records are still matched one for one, so an added or removed record is
    never tolerated.
    """
    try:
        path = Path(ref.storage)
        stamp = _stamp(path)
        key = _location_key(str(path), checkpoint, identity, tolerate)
        with _locations_lock:
            known = _load_locations().get(key)
        if isinstance(known, dict) and known.get("offset") is None:
            # A failed check is only repeated once the file changes.
            if known.get("stamp") == list(stamp):
                return None
        elif (
            isinstance(known, dict)
            and known.get("inode") == stamp[1]
            and isinstance(known.get("end"), int)
            and isinstance(known.get("offset"), int)
            and known["offset"] <= known["end"] <= stamp[2]
            and known.get("window") == _window(path, known["end"])
        ):
            return known["offset"]
        offset = _locate(str(path), checkpoint, identity, tolerate, stamp)
        if offset is None:
            _save_location(key, {"offset": None, "stamp": list(stamp)})
        else:
            _save_location(
                key,
                {
                    "inode": stamp[1],
                    "offset": offset,
                    "end": stamp[2],
                    "window": _window(path, stamp[2]),
                },
            )
        return offset
    except (OSError, ValueError, TypeError, KeyError):
        return None


@functools.lru_cache(maxsize=256)
def _locate(
    storage: str,
    checkpoint: str,
    identity: Identity,
    tolerate: Tolerance | None,
    stamp: tuple[int, int, int, int],
) -> int | None:
    if not checkpoint.startswith(PREFIX):
        return None
    saved = json.loads(checkpoint[len(PREFIX) :])
    offset, expected = saved["offset"], saved["records"]
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if not isinstance(expected, list):
        return None
    reference = saved.get("records_ref")
    if not expected and reference is not None:
        # Read only if the byte prefix below does not match.
        expected = None
    path = Path(storage)
    with path.open("rb") as handle:
        # Ordinary appends preserve the byte prefix; hashing avoids reparsing it.
        remaining, digest = offset, hashlib.sha256()
        while remaining:
            raw = handle.read(min(remaining, 1024 * 1024))
            if not raw:
                break
            digest.update(raw)
            remaining -= len(raw)
        if not remaining and digest.hexdigest() == saved["bytes"]:
            return offset if _stamp(path) == stamp else None
        if expected is None:
            expected = _load_records(reference)
            if expected is None:
                return None  # The records are gone, so the bookmark cannot be checked.
        if any(not isinstance(v, str) or len(v) != 64 for v in expected):
            return None
        if not expected:
            return None  # No conversation identity from which to relocate.
        handle.seek(0)
        matched = 0
        for raw in handle:
            if not raw.endswith(b"\n"):
                return None
            record = json.loads(raw)
            if not isinstance(record, dict):
                return None
            value = identity(record)
            if value is None:
                continue
            if _fingerprint(value) != expected[matched] and not (tolerate and tolerate(record)):
                return None
            matched += 1
            if matched == len(expected):
                return handle.tell() if _stamp(path) == stamp else None
    return None
