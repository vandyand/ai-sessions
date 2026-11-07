"""Native Claude messaging. Persist credentials only through the DevSecrets helper."""

import hashlib
import json
import ntpath
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import psutil

_HELPER = Path(__file__).with_name("messaging_vault.ps1")
_PREFIX = "ai-sessions-claude-"


def _identity(session_id: str, pid: int, started: float) -> tuple[str, str]:
    generation = hashlib.sha256(
        json.dumps([session_id, pid, started], separators=(",", ":")).encode()
    ).hexdigest()
    return generation, _PREFIX + generation


def _owned(receiver: dict) -> bool:
    try:
        generation, key = _identity(receiver["session_id"], receiver["pid"], receiver["started"])
        return (
            receiver["tool"] == "claude"
            and receiver["generation"] == generation
            and receiver["vault_name"] == key
        )
    except (KeyError, TypeError, ValueError):
        return False


def _helper(request: dict) -> dict:
    if sys.platform != "win32":
        return {"status": "unavailable", "reason": "Claude messaging requires Windows DevSecrets."}
    # Do not propagate an exported token into the helper's environment as well as stdin.
    environment = {
        k: v for k, v in os.environ.items() if k.upper() != "CLAUDE_CODE_MESSAGING_TOKEN"
    }
    try:
        result = subprocess.run(
            ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(_HELPER)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=environment,
            check=False,
            creationflags=0x08000000,  # CREATE_NO_WINDOW; helper is Windows-only.
        )
        if result.returncode == 0:
            response = json.loads(result.stdout)
            if isinstance(response, dict) and response.get("status") in {
                "ok",
                "submitted",
                "held",
                "refused",
                "unavailable",
                "failed",
                "uncertain",
            }:
                # Never reflect helper stderr, arbitrary fields, or exception strings.
                return {"status": response["status"]}
    except subprocess.TimeoutExpired:
        return {
            "status": "uncertain" if request["operation"] == "send" else "unavailable",
            "reason": "Claude helper timed out; no automatic retry.",
        }
    except OSError:
        return {"status": "unavailable", "reason": "Secure Claude transport helper unavailable."}
    except (ValueError, TypeError):
        pass
    return {
        "status": "uncertain" if request["operation"] == "send" else "unavailable",
        "reason": "Claude helper result unavailable; no automatic retry.",
    }


def _claude_owner():
    for process in psutil.Process().parents():
        try:
            name = process.name().lower()
            command = process.cmdline()
            native = name in {"claude", "claude.exe"}
            node_cli = name in {"node", "node.exe"} and any(
                arg.replace("\\", "/").endswith("/@anthropic-ai/claude-code/cli.js")
                for arg in command[1:]
            )
            if native or node_cli:
                return process.pid, process.create_time()
        except (psutil.Error, OSError):
            continue
    return None


def register(payload: dict, env: Mapping[str, str]) -> dict | None:
    """Store a token securely and return metadata for the parent hook to save."""
    if sys.platform != "win32":
        return None
    token = env.get("CLAUDE_CODE_MESSAGING_TOKEN")
    address = (
        env.get("CLAUDE_CODE_MESSAGING_SOCKET")
        or env.get("CLAUDE_CODE_MESSAGING_PIPE")
        or env.get("CLAUDE_CODE_MESSAGING_ADDRESS")
    )
    session_id = payload.get("session_id")
    if address:
        address = address.removeprefix("uds:")
    if not token or not session_id or not address or not address.startswith("\\\\.\\pipe\\"):
        return None
    try:
        owner = _claude_owner()
    except (psutil.Error, OSError):
        return None
    if owner is None:
        return None
    pid, started = owner
    generation, vault_name = _identity(session_id, pid, started)
    receiver = {
        "tool": "claude",
        "session_id": session_id,
        "name": payload.get("name") or session_id,
        "cwd": payload.get("cwd", ""),
        "pid": pid,
        "started": started,
        "home": env.get("CLAUDE_CONFIG_DIR")
        or ntpath.join(env.get("USERPROFILE") or env.get("HOME", ""), ".claude"),
        "address": address,
        "vault_name": vault_name,
        "generation": generation,
    }
    # Persist a nonsecret recovery intent before creating the vault entry. If
    # registration fails or the hook dies, later cleanup can remove that entry.
    from ..messaging import store

    if not store.secure_register(
        receiver, lambda: _helper({"operation": "store", "vault_name": vault_name, "token": token})
    ):
        return None
    return receiver


def send(receiver: dict, text: str, message_id: str, sender: dict) -> dict:
    """Submit once; a successful pipe write is neither acceptance nor acknowledgment."""
    if sys.platform != "win32":
        result = _helper({"operation": "send"})
    elif not _owned(receiver):
        result = {"status": "unavailable", "reason": "Invalid Claude receiver registration."}
    else:
        result = _helper(
            {
                "operation": "send",
                "vault_name": receiver["vault_name"],
                "address": receiver.get("address"),
                "text": text,
                "message_id": message_id,
                "sender": sender,
            }
        )
        if result["status"] == "ok":
            result = {"status": "submitted"}
    return {
        **result,
        "message_id": message_id,
        "transport": "claude-native-pipe",
        "recipient": {"tool": "claude", "session_id": receiver.get("session_id")},
        "sender": sender,
    }


def forget(receiver: dict) -> dict:
    """Explicit stale cleanup only: remove precisely this registration's owned key."""
    if not _owned(receiver):
        return {"status": "unavailable", "reason": "Refusing an unowned vault reference."}
    return _helper({"operation": "remove", "vault_name": receiver["vault_name"]})
