"""Confirmed native Codex hard deletion; no model turn or daemon attachment."""

from __future__ import annotations

import json
import queue
import subprocess
import threading

from .. import __version__
from ..deletion import DeletionError
from ..registry import REGISTRY
from .codex import native_environment
from .messaging_rpc import native_executable

RPC_TIMEOUT = 30


def _delete(home, ident):
    process = subprocess.Popen(
        [native_executable(), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=native_environment(home),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    replies = queue.Queue()
    submitted = False

    def read():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(message, dict)
                    and message.get("id") in (1, 2)
                    and ("result" in message or "error" in message)
                ):
                    replies.put(message)
        except (OSError, ValueError):
            pass
        finally:
            replies.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(ident):
        reply = replies.get(timeout=RPC_TIMEOUT)
        if not isinstance(reply, dict) or reply.get("id") != ident:
            raise DeletionError("Codex deletion response was absent or not correlated")
        if "error" in reply:
            error = reply["error"]
            text = (
                error.get("message", "request rejected")
                if isinstance(error, dict)
                else "request rejected"
            )
            text = "".join(char if char.isprintable() else " " for char in str(text))[:500]
            raise DeletionError(f"Codex native deletion rejected/failed: {text}")
        if not isinstance(reply.get("result"), dict):
            raise DeletionError("Codex deletion returned an invalid result")

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ai_sessions_delete", "version": __version__},
                },
            }
        )
        response(1)
        send({"method": "initialized"})
        # Even an explicit native error can follow partial cleanup. From this
        # point onward never describe failure as proof that nothing changed.
        submitted = True
        send({"id": 2, "method": "thread/delete", "params": {"threadId": ident}})
        response(2)
    except BaseException as error:
        if submitted:
            raise DeletionError(
                f"PARTIAL/UNCERTAIN native deletion of {ident}: {error}. "
                "Stop and refresh the catalog; do not automatically retry."
            ) from error
        raise DeletionError(f"Codex deletion was not submitted: {error}") from error
    finally:
        # Own only this stdio child. Never stop a shared daemon or native client.
        try:
            try:
                process.stdin.close()
                process.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()
            reader.join(timeout=1)
        except Exception as error:
            status = (
                "PARTIAL/UNCERTAIN native deletion"
                if submitted
                else "Native deletion not submitted"
            )
            raise DeletionError(
                f"{status}: owned Codex child shutdown failed; inspect before retrying: {error}"
            ) from error


def execute_delete(plan):
    adapter = REGISTRY.get("codex")
    if (
        not plan.native_delete
        or plan.tool != adapter.name
        or adapter.resolve(plan.native.session_id) != plan.native
    ):
        raise DeletionError(
            "Codex native deletion plan no longer matches its authoritative identity"
        )
    for change in plan.files:
        if change.home.resolve() != adapter.home.resolve():
            raise DeletionError("Codex deletion guard belongs to another native home")
        change.validate()
    _delete(adapter.home, plan.native.session_id)
    if any(change.path.exists() for change in plan.files):
        raise DeletionError(
            "PARTIAL native deletion acknowledged, but known rollout history remains; refresh before further action"
        )
