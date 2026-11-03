"""Hook-owned supervisor state, stored in each supervisor's own folder.

This module must stay light: the hook's fast path imports it and nothing from ``app``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_sessions.model import Checkpoint
from ai_sessions.paths import APP_CONFIG_DIR

SUPERVISORS_DIR = APP_CONFIG_DIR / "supervisors"
RECORD_NAME = "record.json"
TITLE_SUFFIX = " · supervisor"
_RETRIES = 400
_RETRY_SLEEP = 0.005


@dataclass
class HookState:
    record_id: str
    native_session_id: str = ""
    own_offset: int = 0
    member: str = ""
    checkpoint: Checkpoint | None = None
    last_seq_sent: int = -1
    brief_due: bool = True
    briefs: int = 0
    reset: bool = False
    updated: float = 0.0


def _root(root: Path | None) -> Path:
    return SUPERVISORS_DIR if root is None else root


def _record_path(record_id: str, root: Path | None) -> Path:
    return _root(root) / record_id / RECORD_NAME


def _read_json(path: Path) -> Any:
    for attempt in range(_RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == _RETRIES - 1:
                return None
            time.sleep(_RETRY_SLEEP)
        except (OSError, ValueError):
            return None
    return None


def _from_payload(payload: Any, fallback_id: str) -> HookState | None:
    if not isinstance(payload, dict):
        return None
    defaults = HookState(record_id=str(payload.get("record_id") or fallback_id))
    values: dict[str, Any] = {}
    for key, default in asdict(defaults).items():
        value = payload.get(key, default)
        if key == "checkpoint":
            if isinstance(value, bool) or not isinstance(value, (int, str, type(None))):
                value = None
        elif isinstance(default, bool):
            value = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            value = value if isinstance(value, int) and not isinstance(value, bool) else default
        elif isinstance(default, float):
            value = float(value) if isinstance(value, (int, float)) else default
        elif not isinstance(value, str):
            value = default
        values[key] = value
    return HookState(**values)


def load_hook_state(record_id: str, root: Path | None = None) -> HookState | None:
    path = _record_path(record_id, root)
    return _from_payload(_read_json(path), record_id)


def save_hook_state(state: HookState, root: Path | None = None) -> None:
    path = _record_path(state.record_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{RECORD_NAME}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temp.write_text(json.dumps(asdict(state), ensure_ascii=False) + "\n", encoding="utf-8")
        for attempt in range(_RETRIES):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == _RETRIES - 1:
                    raise
                time.sleep(_RETRY_SLEEP)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def find_by_native_id(session_id: str, root: Path | None = None) -> HookState | None:
    if not session_id:
        return None
    for path in sorted(_root(root).glob(f"*/{RECORD_NAME}")):
        state = _from_payload(_read_json(path), path.parent.name)
        if state is not None and state.native_session_id == session_id:
            return state
    return None
