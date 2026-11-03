"""Fast entry for the supervisor hook: ``python -m ai_sessions_hook <event>``.

Both harnesses run this before every prompt of every session, so it lives outside the
``ai_sessions`` package: importing that package loads every harness adapter, which costs
a quarter of a second.  This module uses only the standard library and hands over to
``ai_sessions.supervisor.hook`` only when the session may be a supervisor.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

ENV_VAR = "AI_SESSIONS_SUPERVISOR"


def supervisors_dir(env: dict[str, str] | os._Environ[str]) -> Path:
    """Where ``ai_sessions.paths`` puts supervisor folders; kept in step with it."""
    home = Path.home()
    if os.name == "nt":
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "ai-sessions" / "supervisors"


def may_be_supervisor(payload: object, env: dict[str, str] | os._Environ[str]) -> bool:
    """True when the full hook must look: the variable is set or a record names the id."""
    if env.get(ENV_VAR):
        return True
    if not isinstance(payload, dict):
        return False
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return False
    needle = session_id.encode("utf-8")
    try:
        records = list(supervisors_dir(env).glob("*/record.json"))
    except OSError:
        return False
    for record in records:
        try:
            if needle in record.read_bytes():
                return True
        except OSError:
            continue
    return False


def _log_import_failure(error: BaseException) -> None:
    """Record why a supervisor got nothing; the prompt itself must still go through."""
    try:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        folder = base / "ai-sessions"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "supervisor-hook.log", "a", encoding="utf-8") as handle:
            handle.write(f"cannot import the supervisor hook: {type(error).__name__}: {error}\n")
    except Exception:
        pass


def run() -> int:
    data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(data or "{}")
    except ValueError:
        return 0
    if not may_be_supervisor(payload, os.environ):
        return 0
    try:
        from ai_sessions.supervisor import hook
    except Exception as error:
        _log_import_failure(error)
        return 0
    return hook.main(sys.argv[1:], io.StringIO(data), sys.stdout.buffer, os.environ)


if __name__ == "__main__":
    sys.exit(run())
