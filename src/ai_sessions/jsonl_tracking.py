"""Adapter-owned JSONL bookmarks resilient to formatting and metadata rewrites."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from .model import NativeRef
from .paths import APP_CACHE_DIR

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
        return PREFIX + json.dumps(
            {
                "offset": offset,
                "bytes": digest.hexdigest(),
                "records": fingerprints,
                "identity": identity_version,
            },
            separators=(",", ":"),
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
        locations.pop(key, None)
        locations[key] = entry
        while len(locations) > _LOCATION_ENTRIES:
            locations.pop(next(iter(locations)))
        try:
            LOCATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = LOCATIONS_FILE.with_suffix(f".tmp-{os.getpid()}")
            temporary.write_text(json.dumps(locations), encoding="utf-8")
            temporary.replace(LOCATIONS_FILE)
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
    if not isinstance(expected, list) or any(
        not isinstance(v, str) or len(v) != 64 for v in expected
    ):
        return None
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
