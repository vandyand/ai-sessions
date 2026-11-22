"""Provider-specific bounded desk reads and tools-disabled model process ownership."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..model import ReadSnapshot

BRIEFING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["review", "decisions", "overview"],
    "properties": {
        "review": {"type": "string", "minLength": 1, "maxLength": 64000},
        "overview": {
            "type": "object",
            "additionalProperties": False,
            "required": ["background", "current", "direction", "goal"],
            "properties": {
                key: {"type": "string", "minLength": 1, "maxLength": 6000}
                for key in ("background", "current", "direction", "goal")
            },
        },
        "decisions": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "question", "context", "recommendation", "status", "evidence"],
                "properties": {
                    **{
                        key: {"type": "string"}
                        for key in ("key", "question", "context", "recommendation")
                    },
                    "status": {"type": "string", "enum": ["pending", "resolved", "superseded"]},
                    "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
    },
}


class ObservationCancelled(Exception):
    """The owning viewer stopped; do not alter the briefing or its error state."""


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise ObservationCancelled()


def read_since(tool, ref, checkpoint, *, end):
    """Bounded adaptation of native read_since: fixed byte range, no tool output stored."""
    if tool == "claude":
        from ..harnesses.claude import read_claude

        transcript = read_claude(Path(ref.storage), start=checkpoint, end=end)
    elif tool == "codex":
        from ..harnesses.codex import read_codex

        transcript = read_codex(Path(ref.storage), start=checkpoint, end=end)
    else:
        raise ValueError("bounded observer supports plain Claude/Codex JSONL only")
    return ReadSnapshot(transcript, end)


def run_model(
    prompt, stop=None, *, max_output_bytes, timeout, system, citation_ids=None, output_schema=None
):
    """Existing CLI login only; bounded memory, execution time, and pipe writes.

    Safe mode disables custom instructions/plugins/hooks, while preserving normal
    authentication. --bare is deliberately absent because it disables OAuth.
    """
    _check_stop(stop)
    executable = shutil.which("claude")
    if os.name == "nt":
        from ..harnesses.claude import native_executable

        # Native argv preserves empty --tools and literal JSON. npm .cmd shims
        # route through cmd.exe, whose quoting rules do not preserve these safely.
        executable = native_executable()
    if not executable:
        raise RuntimeError("Claude CLI unavailable; install/sign in separately")
    schema = json.loads(json.dumps(output_schema if output_schema is not None else BRIEFING_SCHEMA))
    if output_schema is not None:
        pass  # A separately validated details schema cannot edit the legacy decision core.
    elif citation_ids:
        schema["properties"]["decisions"]["items"]["properties"]["evidence"]["items"]["enum"] = (
            citation_ids
        )
    elif citation_ids is not None:
        # Historical-prefix-only synthesis can describe Overview, not change decisions.
        schema["properties"]["decisions"]["maxItems"] = 0
    command = [
        executable,
        "-p",
        "--model",
        os.environ.get("AI_SESSIONS_DESK_MODEL", "haiku"),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true}',
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--system-prompt",
        system,
    ]
    # No credential files are inspected or copied. The CLI manages its existing login.
    with tempfile.TemporaryDirectory(prefix="desk-observer-") as cwd:
        job = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            from ..platforms.windows import CREATE_SUSPENDED, KillOnCloseJob, _resume

            flags |= CREATE_SUSPENDED
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        if os.name == "nt":
            # Assign before resuming: no descendant can escape the owning job.
            job = KillOnCloseJob._assign(int(process._handle))
            if job is None:
                process.kill()
                process.wait(timeout=2)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                raise RuntimeError("observer model process containment unavailable")
            _resume(int(process._handle))
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        pipe_error = threading.Event()

        def drain(pipe, target):
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    remaining = max_output_bytes - len(target)
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        process.kill()
                        break
            except (OSError, ValueError):
                pipe_error.set()
            finally:
                pipe.close()

        def write():
            try:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.close()
            except (OSError, ValueError):
                pipe_error.set()

        threads = [
            threading.Thread(target=drain, args=(process.stdout, buffers[0]), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, buffers[1]), daemon=True),
            threading.Thread(target=write, daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                _check_stop(stop)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("observer model timed out")
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
            _check_stop(stop)
        finally:
            # Cleanup on success too: a descendant must never keep pipes or model
            # work alive after the main process returns. Only our job/group dies.
            if job is not None:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=0.5)
        if overflow.is_set():
            raise ValueError("observer model output exceeded byte limit")
        if process.returncode:
            raise RuntimeError(f"observer model exited with status {process.returncode}")
        if pipe_error.is_set() or any(t.is_alive() for t in threads):
            raise RuntimeError("observer model pipe incomplete")
    envelope = json.loads(buffers[0].decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise ValueError("observer model returned an error envelope")
    result = envelope.get("structured_output", envelope.get("result"))
    if isinstance(result, dict):
        return json.dumps(result)
    if not isinstance(result, str):
        raise ValueError("observer model envelope has no result")
    return result
