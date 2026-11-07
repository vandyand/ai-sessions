"""Separate, bounded nonsecret receiver and delivery records."""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .. import fastjson
from ..paths import APP_CONFIG_DIR, env_path

ROOT = env_path("AI_SESSIONS_MESSAGING_DIR", APP_CONFIG_DIR / "messaging")
RECEIVER_FIELDS = frozenset(
    "tool session_id name cwd pid started home address vault_name generation registered".split()
)
RECEIPT_FIELDS = frozenset(
    "message_id status reason sender recipient created transport turn_id reply_to acknowledged_by".split()
)
MAX_RECEIPTS = 1000
MAX_AGE = 7 * 86400


def _path(kind: str, identity: str) -> Path:
    return ROOT / kind / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def identity(row: dict) -> str:
    return f"{row['tool']}:{row['session_id']}"


def endpoint_id(row: dict) -> str:
    home = row.get("home")
    if not home:
        return identity(row)
    suffix = hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode()).hexdigest()[:10]
    return identity(row) + "@" + suffix


def _read(path: Path) -> dict:
    try:
        data = fastjson.read(path)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_receiver(row: dict) -> None:
    data = {k: v for k, v in row.items() if k in RECEIVER_FIELDS}
    data["registered"] = time.time()
    with locked():
        path = _path("receivers", identity(data))
        old = _read(path)
        if old and old.get("generation") != data.get("generation") and is_live(old):
            raise ValueError("another live receiver already owns this session ID")
        fastjson.write_atomic(path, data)


def remove_receiver(row: dict) -> bool:
    with locked():
        path = _path("receivers", identity(row))
        if _read(path).get("generation") != row.get("generation"):
            return False
        path.unlink(missing_ok=True)
        return True


def is_live(row: dict) -> bool:
    try:
        import psutil

        process = psutil.Process(int(row["pid"]))
        return process.is_running() and abs(process.create_time() - float(row["started"])) < 0.01
    except (KeyError, ValueError, TypeError, psutil.Error):
        return False


def receivers() -> list[dict]:
    return [r for p in (ROOT / "receivers").glob("*.json") if (r := _read(p)) and is_live(r)]


def stale_receivers() -> list[dict]:
    return [r for p in (ROOT / "receivers").glob("*.json") if (r := _read(p)) and not is_live(r)]


def resolve(target: str, rows: list[dict]) -> dict:
    exact = [
        r for r in rows if target in (endpoint_id(r), identity(r), str(r.get("session_id", "")))
    ]
    matches = exact or [r for r in rows if target == r.get("name")]
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous receiver {target!r}; use the qualified address from messaging status"
        )
    if not matches:
        raise ValueError(f"receiver {target!r} is unavailable; reopen it after messaging setup")
    return matches[0]


def save_receipt(row: dict) -> None:
    data = {k: v for k, v in row.items() if k in RECEIPT_FIELDS}
    data.setdefault("created", time.time())
    with locked():
        target = _path("receipts", str(data["message_id"]))
        previous = _read(target)
        if previous.get("status") == "acknowledged":
            data.update(status="acknowledged", acknowledged_by=previous.get("acknowledged_by"))
        fastjson.write_atomic(target, data)
        paths = sorted(
            (ROOT / "receipts").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        cutoff = time.time() - MAX_AGE
        for i, path in enumerate(paths):
            if i >= MAX_RECEIPTS or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def receipt(message_id: str) -> dict:
    return _read(_path("receipts", message_id))


def acknowledge(message_id: str, sender: dict, reply_id: str) -> None:
    with locked():
        path = _path("receipts", message_id)
        row = _read(path)
        if row and row.get("recipient") == sender:
            row.update(status="acknowledged", acknowledged_by=reply_id)
            fastjson.write_atomic(path, row)


def queue_cleanup(row: dict) -> None:
    data = {k: v for k, v in row.items() if k in RECEIVER_FIELDS}
    with locked():
        path = _path("cleanup", identity(data) + ":" + data["generation"])
        if not path.exists():
            fastjson.write_atomic(path, data)


def secure_register(row: dict, put) -> bool:
    """Serialize intent, vault creation, and publication against cleanup."""
    data = {k: v for k, v in row.items() if k in RECEIVER_FIELDS}
    data["registered"] = time.time()
    with locked():
        target = _path("receivers", identity(data))
        old = _read(target)
        if old and old.get("generation") != data.get("generation") and is_live(old):
            raise ValueError("another live receiver already owns this session ID")
        intent = _path("cleanup", identity(data) + ":" + data["generation"])
        fastjson.write_atomic(intent, data)
        if put().get("status") != "ok":
            return False
        fastjson.write_atomic(target, data)
        intent.unlink(missing_ok=True)
        return True


def clear_cleanup(row: dict) -> None:
    with locked():
        _path("cleanup", identity(row) + ":" + row["generation"]).unlink(missing_ok=True)


def cleanup_pending(tool: str, forget) -> None:
    if forget is None:
        return
    for row in stale_receivers():
        if row.get("tool") == tool:
            queue_cleanup(row)
    with locked():
        candidates = sorted((ROOT / "cleanup").glob("*.json"), key=lambda p: p.stat().st_mtime)
        attempts = 0
        for path in candidates:
            row = _read(path)
            if not row or row.get("tool") != tool:
                continue
            target = _path("receivers", identity(row))
            current = _read(target)
            if current.get("generation") == row.get("generation") and is_live(current):
                path.unlink(missing_ok=True)
                continue
            if attempts >= 4:
                break
            attempts += 1
            if forget(row).get("status") == "ok":
                if current.get("generation") == row.get("generation"):
                    target.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            else:
                # Oldest-first rotation prevents one locked/missing entry from
                # starving later cleanup records forever.
                os.utime(path, None)
