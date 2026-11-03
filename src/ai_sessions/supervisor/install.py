"""Register the supervisor hooks in the Claude and Codex hook files."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "ai_sessions_hook"
# Commands an earlier build registered; still recognised so reinstalling replaces them.
OLD_MARKERS = ("ai_sessions.supervisor.hook",)
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "user-prompt-submit"}
TIMEOUT_SECONDS = 60
RETRUST = "Codex will ask to re-trust the changed hooks on next start."


def hook_command(event: str, executable: str | None = None) -> str:
    """The hook's command line, in the form both harnesses run on every platform.

    Codex runs hook commands through PowerShell on Windows, where a quoted program path
    followed by arguments is a parse error, so the path is written with forward slashes
    and quoted only when it contains a space.
    """
    program = (executable or sys.executable).replace("\\", "/")
    if " " in program:
        program = f'"{program}"'
    # No ``-I``: it hides the user site-packages, where dependencies such as psutil may live.
    return f"{program} -m {MARKER} {event}"


def _is_ours(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command", ""))
    return any(marker in command for marker in (MARKER, *OLD_MARKERS))


def _strip_ours(groups: list) -> list:
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        rest = [h for h in group["hooks"] if not _is_ours(h)]
        if rest or len(rest) == len(group["hooks"]):
            kept.append({**group, "hooks": rest})
    return kept


def _load(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"Refused to modify {path}: not valid JSON ({error})."
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return None, f"Refused to modify {path}: unexpected structure (hooks is not an object)."
    return data, None


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _apply(path: Path, add: bool, label: str) -> list[str]:
    notices: list[str] = []
    existed = path.exists()
    if existed:
        data, error = _load(path)
        if error:
            return [error]
    elif add:
        data = {}
    else:
        return [f"{path} does not exist; nothing to remove."]
    hooks = data.setdefault("hooks", {}) if add else data.get("hooks", {})
    for event in EVENTS:
        if not isinstance(hooks.get(event, []), list):
            return [f"Refused to modify {path}: hooks.{event} is not a list."]
    before = json.dumps(data, sort_keys=True)
    for event, arg in EVENTS.items():
        groups = _strip_ours(hooks.get(event, []))
        if add:
            groups.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command(arg),
                            "timeout": TIMEOUT_SECONDS,
                        }
                    ]
                }
            )
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not add and not hooks:
        data.pop("hooks", None)
    if existed and json.dumps(data, sort_keys=True) == before:
        return [f"{label}: {path} already up to date."]
    if existed:
        notices.append(f"Backed up {path} to {_backup(path)}.")
    _write(path, data)
    verb = "Installed hooks in" if add else "Removed hooks from"
    notices.append(f"{verb} {label}: {path}.")
    if add:
        notices.append(f"{label}: {RETRUST}" if label == "Codex" else "")
    return [n for n in notices if n]


def install(claude_settings: Path, codex_hooks: Path) -> list[str]:
    return _apply(claude_settings, True, "Claude") + _apply(codex_hooks, True, "Codex")


def uninstall(claude_settings: Path, codex_hooks: Path) -> list[str]:
    return _apply(claude_settings, False, "Claude") + _apply(codex_hooks, False, "Codex")
