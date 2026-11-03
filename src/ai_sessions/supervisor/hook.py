"""Hook entry point: ``python -m ai_sessions.supervisor.hook <event>``.

The fast path for an ordinary session must stay light: nothing here imports ``app`` (or
``context``, which pulls in the readers) until a supervisor has been identified.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import traceback
from dataclasses import replace
from typing import BinaryIO, Mapping, TextIO

from ai_sessions import paths
from ai_sessions.supervisor import store
from ai_sessions.supervisor.store import HookState

ENV_VAR = "AI_SESSIONS_SUPERVISOR"
LOG_NAME = "supervisor-hook.log"
SCAN_LIMIT = 32 * 1024 * 1024
EVENT_NAMES = {"session-start": "SessionStart", "user-prompt-submit": "UserPromptSubmit"}
RESET_NOTICE = (
    "This supervisor was reset and no longer follows its target. Tell the user to open the "
    "new supervisor with S in the sessions browser. No target activity is provided here."
)

# Byte patterns that mark the supervisor's own compaction in its transcript.
COMPACTION_PATTERNS = (
    re.compile(rb'"subtype"\s*:\s*"compact_boundary"'),
    re.compile(rb'"isCompactSummary"\s*:\s*true'),
    re.compile(rb'"type"\s*:\s*"compacted"'),
)


def scan_for_compaction(path: str, offset: int) -> tuple[bool, int]:
    """Look for a compaction record after ``offset``; return (found, new offset)."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return False, offset
    if size < offset:
        offset = 0
    if size == offset:
        return False, offset
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(min(size - offset, SCAN_LIMIT))
    end = data.rfind(b"\n") + 1
    if not end and len(data) < SCAN_LIMIT:
        return False, offset  # a partial line; read it whole next time
    data = data[:end] if end else data
    found = any(pattern.search(data) for pattern in COMPACTION_PATTERNS)
    return found, offset + len(data)


def _log(error: BaseException) -> None:
    try:
        paths.APP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = "".join(traceback.format_exception(error))
        with open(paths.APP_CACHE_DIR / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def _emit(stdout: BinaryIO, event: str, text: str) -> None:
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    stdout.write(data + b"\n")
    stdout.flush()


def _user_state():
    from ai_sessions.app import UserState

    return UserState()


def _identify(payload: dict, env: Mapping[str, str]) -> HookState | None:
    session_id = str(payload.get("session_id") or "")
    found = store.find_by_native_id(session_id)
    if found is not None:
        return found
    record_id = env.get(ENV_VAR, "")
    if not record_id or not session_id:
        return None
    state = store.load_hook_state(record_id)
    if state is None:
        return None
    # A bound record keeps its session, so the variable a supervisor's own child sessions
    # inherit cannot steal it.  /clear is the exception: the harness starts a new session
    # in the same process, and that session is the supervisor now.
    cleared = payload.get("source") == "clear"
    if state.native_session_id and not cleared:
        return None
    try:
        offset = os.stat(str(payload.get("transcript_path") or "")).st_size
    except OSError:
        offset = 0
    state = replace(
        state,
        native_session_id=session_id,
        own_offset=offset,
        brief_due=state.brief_due or cleared,
    )
    store.save_hook_state(state)
    return state


def _save(state: HookState) -> None:
    """Save, keeping a reset the browser made while this run was working."""
    current = store.load_hook_state(state.record_id)
    if current is not None and current.reset:
        state = replace(state, reset=True)
    store.save_hook_state(state)


def _scan(payload: dict, state: HookState) -> HookState:
    path = str(payload.get("transcript_path") or "")
    if not path:
        return state
    found, offset = scan_for_compaction(path, state.own_offset)
    return replace(state, own_offset=offset, brief_due=state.brief_due or found)


def _handle(event: str, payload: dict, state: HookState) -> str:
    if state.reset:
        return RESET_NOTICE
    state = _scan(payload, state)
    if event == "session-start":
        if payload.get("source") == "compact":
            state = replace(state, brief_due=True)
        _save(state)
        return ""
    from ai_sessions.supervisor.context import prepare_context

    text, new = prepare_context(state.record_id, state=_user_state(), hook=state)
    _save(replace(new, own_offset=state.own_offset))
    return text


def main(argv: list[str], stdin: TextIO, stdout: BinaryIO, env: Mapping[str, str]) -> int:
    """Run one hook event.  Always returns 0 and never raises."""
    event = argv[0] if argv else ""
    if event not in EVENT_NAMES:
        return 0
    state: HookState | None = None
    try:
        payload = json.loads(stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        state = _identify(payload, env)
        if state is None:
            return 0
        text = _handle(event, payload, state)
        if text:
            _emit(stdout, EVENT_NAMES[event], text)
    except Exception as error:
        _log(error)
        if state is not None and event == "user-prompt-submit":
            try:
                message = " ".join(f"{type(error).__name__}: {error}".split())
                _emit(stdout, EVENT_NAMES[event], f"⚠ supervisor: {message}")
            except Exception:
                pass
    return 0


def run() -> int:
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    return main(sys.argv[1:], stdin, sys.stdout.buffer, os.environ)


if __name__ == "__main__":
    sys.exit(run())
