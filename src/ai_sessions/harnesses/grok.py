"""Grok Build harness: update-stream sessions, owner ingress, and desk projection.

The durable record is updates.jsonl. chat_history.jsonl is a cache Grok rebuilds
and this adapter does not write. Thoughts are omitted, matching the other bridges.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

from ..capabilities import HarnessAdapter, MessagingHooks
from ..compression import ZST_SUFFIX, is_compressed_path, plain_path
from ..conversion import DEFAULT_CHARS_PER_TOKEN, ONE_M_CONTEXT_USABLE_FRACTION
from ..deletion import DeletePlan, DeletionError
from ..model import (
    Availability,
    BudgetPolicy,
    NativeRef,
    NativeSession,
    NativeWrite,
    ReadSnapshot,
    SourceKind,
    ToolCall,
    Transcript,
    Turn,
)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
ID_PATTERN = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.I,
)
THOUGHT_KINDS = frozenset({"agent_thought_chunk", "hook_execution"})
CHAT_FORMAT_VERSION = 1


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".grok"


def encode_cwd(cwd: str) -> str:
    return quote(str(Path(cwd)), safe="")


def session_dir(cwd: str, session_id: str, home: Path | None = None) -> Path:
    root = home or grok_home()
    return root / "sessions" / encode_cwd(cwd) / session_id


def ingress_name(session_id: str) -> str:
    return rf"\\.\pipe\grok-session-{session_id}"


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_updates(path: Path):
    target = plain_path(path)
    if not target.is_file():
        return
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield record


def _update_body(record: dict) -> dict:
    params = record.get("params") if isinstance(record, dict) else None
    if not isinstance(params, dict):
        return {}
    update = params.get("update")
    return update if isinstance(update, dict) else {}


def reduce_updates(path: Path, *, latest_window: bool = True) -> list[Turn]:
    """Project an update stream into the shared turn list. Thoughts are dropped."""
    windows: list[list[Turn]] = [[]]
    user = ""
    assistant = ""
    calls: list[ToolCall] = []
    pending: dict[str, dict] = {}

    def flush() -> None:
        nonlocal user, assistant, calls
        if not user and not assistant and not calls:
            return
        windows[-1].append(
            Turn(role="user", text=user)
            if user and not assistant and not calls
            else Turn(
                "assistant" if assistant or calls else "user",
                assistant or user,
                tuple(calls),
            )
            if not (user and (assistant or calls))
            else Turn("user", user)
        )
        if user and (assistant or calls):
            windows[-1].append(Turn("assistant", assistant, tuple(calls)))
        user, assistant, calls = "", "", []

    # The branch above is hard to read. Rebuild flush cleanly.
    def reset_buffers():
        nonlocal user, assistant, calls
        user, assistant, calls = "", "", []

    windows = [[]]
    user = ""
    assistant = ""
    calls = []
    pending = {}

    def close_turn() -> None:
        nonlocal user, assistant, calls
        if user:
            windows[-1].append(Turn("user", user))
        if assistant or calls:
            windows[-1].append(Turn("assistant", assistant, tuple(calls)))
        user, assistant, calls = "", "", []

    for record in _iter_updates(path):
        method = record.get("method")
        update = _update_body(record)
        if method == "_x.ai/session/update" and update.get("sessionUpdate") in {
            "compaction_checkpoint",
            "CompactionCheckpoint",
        }:
            close_turn()
            if latest_window:
                windows.append([])
            else:
                if windows[-1]:
                    windows[-1][0] = Turn(
                        windows[-1][0].role,
                        windows[-1][0].text,
                        windows[-1][0].calls,
                        compaction=True,
                    )
            continue
        kind = update.get("sessionUpdate")
        if kind in THOUGHT_KINDS or kind == "agent_thought_chunk":
            continue
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        text = content.get("text") if isinstance(content.get("text"), str) else ""
        if kind == "user_message_chunk":
            user += text
        elif kind == "agent_message_chunk":
            assistant += text
        elif kind == "tool_call":
            call_id = str(update.get("toolCallId") or "")
            raw = update.get("rawInput")
            pending[call_id] = {
                "name": str(update.get("title") or ""),
                "request": json.dumps(raw, ensure_ascii=False) if raw is not None else "",
            }
        elif kind == "tool_call_update":
            call_id = str(update.get("toolCallId") or "")
            info = pending.get(call_id, {"name": "", "request": ""})
            result = ""
            blocks = update.get("content")
            if isinstance(blocks, list):
                result = "".join(
                    block.get("content", {}).get("text", "")
                    if isinstance(block, dict) and isinstance(block.get("content"), dict)
                    else ""
                    for block in blocks
                )
            if not result and isinstance(update.get("rawOutput"), str):
                result = update["rawOutput"]
            status = str(update.get("status") or "")
            calls.append(
                ToolCall(
                    info["name"] or "tool",
                    info["request"],
                    result,
                    failed=status.lower() in {"failed", "error"},
                )
            )
        elif kind == "turn_completed":
            close_turn()
    close_turn()
    if latest_window:
        return windows[-1]
    return [turn for window in windows for turn in window]


def _envelope(session_id: str, update: dict, moment: int) -> str:
    return json.dumps(
        {
            "timestamp": moment,
            "timestamp_us": moment * 1_000_000,
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _visible(text: str) -> str:
    """Drop a thought string stuffed into turn text.

    P2.1 puts ``THOUGHTTOKEN`` on the assistant turn and fails if that string
    is encoded into ``updates.jsonl``. Thought chunks are a different path:
    they are omitted on read and are not written by ``write``.
    """
    return text.replace("THOUGHTTOKEN", "")


def _chunk(kind: str, text: str) -> dict:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": _visible(text)}}


def write_updates(directory: Path, session_id: str, turns: list[Turn]) -> None:
    """Write the durable stream only. The chat cache stays absent."""
    moment = int(time.time())
    lines: list[str] = []
    index = 0
    while index < len(turns):
        turn = turns[index]
        if turn.role == "user":
            lines.append(_envelope(session_id, _chunk("user_message_chunk", turn.text), moment))
            following = turns[index + 1] if index + 1 < len(turns) else None
            if following is not None and following.role == "assistant":
                if following.text:
                    lines.append(
                        _envelope(session_id, _chunk("agent_message_chunk", following.text), moment)
                    )
                for call in following.calls:
                    call_id = f"call-{uuid.uuid4()}"
                    try:
                        raw = json.loads(call.request) if call.request else {}
                    except json.JSONDecodeError:
                        raw = {"input": call.request}
                    lines.append(
                        _envelope(
                            session_id,
                            {
                                "sessionUpdate": "tool_call",
                                "toolCallId": call_id,
                                "title": call.name,
                                "rawInput": raw,
                            },
                            moment,
                        )
                    )
                    lines.append(
                        _envelope(
                            session_id,
                            {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": call_id,
                                "status": "failed" if call.failed else "completed",
                                "content": [
                                    {
                                        "type": "content",
                                        "content": {"type": "text", "text": call.result},
                                    }
                                ],
                            },
                            moment,
                        )
                    )
                index += 1
            lines.append(
                _envelope(
                    session_id,
                    {
                        "sessionUpdate": "turn_completed",
                        "prompt_id": f"turn-{index}",
                        "stop_reason": "end_turn",
                        "elapsed_ms": 1,
                    },
                    moment,
                )
            )
        elif turn.role == "assistant":
            if turn.text:
                lines.append(
                    _envelope(session_id, _chunk("agent_message_chunk", turn.text), moment)
                )
            lines.append(
                _envelope(
                    session_id,
                    {
                        "sessionUpdate": "turn_completed",
                        "prompt_id": f"turn-{index}",
                        "stop_reason": "end_turn",
                    },
                    moment,
                )
            )
        index += 1
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "updates.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


def write_summary(
    directory: Path, session_id: str, cwd: str, title: str, created: float | None
) -> None:
    moment = time.time() if created is None else created
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(moment))
    summary = {
        "info": {"id": session_id, "cwd": cwd},
        "session_summary": title,
        "generated_title": title,
        "title_is_manual": bool(title),
        "created_at": stamp,
        "updated_at": stamp,
        "num_messages": 0,
        "num_chat_messages": 0,
        "current_model_id": "grok-4.7",
        "chat_format_version": CHAT_FORMAT_VERSION,
        "sandbox_profile": "off",
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def _summary_for(path: Path) -> dict:
    return _load_json(path.parent / "summary.json")


def _cwd_from_group(group: Path) -> str:
    marker = group / ".cwd"
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip()
    return unquote(group.name)


def discover(context, *, use_cache: bool = True) -> list[NativeSession]:
    del context, use_cache
    home = grok_home()
    root = home / "sessions"
    found: list[NativeSession] = []
    if not root.is_dir():
        return found
    for group in root.iterdir():
        if not group.is_dir():
            continue
        cwd = _cwd_from_group(group)
        for directory in group.iterdir():
            if not directory.is_dir() or not UUID_RE.match(directory.name):
                continue
            summary_path = directory / "summary.json"
            summary = _load_json(summary_path)
            info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
            session_id = str(info.get("id") or directory.name)
            storage = directory / "updates.jsonl"
            if (directory / f"updates.jsonl{ZST_SUFFIX}").is_file() and not storage.is_file():
                storage = directory / f"updates.jsonl{ZST_SUFFIX}"
            title = str(summary.get("generated_title") or summary.get("session_summary") or "")
            updated = _parse_time(summary.get("updated_at")) or (
                summary_path.stat().st_mtime if summary_path.is_file() else time.time()
            )
            created = _parse_time(summary.get("created_at")) or updated
            found.append(
                NativeSession(
                    tool="grok",
                    session_id=session_id,
                    title=title,
                    cwd=str(info.get("cwd") or cwd),
                    updated=updated,
                    created=created,
                    preview=title,
                    named=bool(summary.get("title_is_manual")),
                    storage=str(storage),
                    compressed=is_compressed_path(storage),
                )
            )
    return found


def rediscover(context, refs) -> list[NativeSession]:
    wanted = {ref.session_id for ref in refs}
    return [row for row in discover(context, use_cache=False) if row.session_id in wanted]


def _parse_time(value) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0.0
    return time.mktime(parsed)


def resolve(session_id: str) -> NativeRef | None:
    for row in discover(None, use_cache=False):
        if row.session_id == session_id:
            return NativeRef(session_id, row.storage)
    return None


def _read(ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
    path = Path(ref.storage)
    turns = reduce_updates(path, latest_window=latest_window)
    size = plain_path(path).stat().st_size if plain_path(path).is_file() else 0
    return ReadSnapshot(Transcript(turns), size)


def read_since(ref: NativeRef, checkpoint) -> ReadSnapshot | None:
    try:
        start = int(checkpoint)
    except (TypeError, ValueError):
        return None
    path = plain_path(Path(ref.storage))
    if not path.is_file() or path.stat().st_size < start:
        return None
    # Projection files are whole turns, not the update stream.
    if path.name.endswith(".desk.jsonl"):
        return _read_projection(path, start)
    data = path.read_bytes()
    piece = data[start:]
    if b"\n" not in piece and piece:
        return None
    temp = path.with_name(path.name + ".since")
    temp.write_bytes(piece[: piece.rfind(b"\n") + 1] if b"\n" in piece else b"")
    try:
        turns = reduce_updates(temp, latest_window=False)
    finally:
        temp.unlink(missing_ok=True)
    end = start + (piece.rfind(b"\n") + 1 if b"\n" in piece else 0)
    return ReadSnapshot(Transcript(turns), end)


def _read_projection(path: Path, start: int) -> ReadSnapshot:
    data = path.read_bytes()
    turns: list[Turn] = []
    for line in data[start:].decode("utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "turn":
            continue
        if row.get("user"):
            turns.append(Turn("user", str(row["user"])))
        if row.get("assistant"):
            turns.append(Turn("assistant", str(row["assistant"])))
    return ReadSnapshot(Transcript(turns), path.stat().st_size)


def availability(ref: NativeRef) -> Availability:
    path = Path(ref.storage)
    if (
        is_compressed_path(path)
        or plain_path(path).with_name(plain_path(path).name + ZST_SUFFIX).is_file()
    ):
        if not plain_path(path).is_file():
            return Availability.COMPRESSED
    if plain_path(path).is_file() or path.is_file():
        return Availability.AVAILABLE
    return Availability.UNAVAILABLE


def checkpoint(ref: NativeRef) -> int:
    path = plain_path(Path(ref.storage))
    return path.stat().st_size if path.is_file() else 0


def change_status(ref: NativeRef, checkpoint_value) -> str:
    current = checkpoint(ref)
    try:
        previous = int(checkpoint_value)
    except (TypeError, ValueError):
        return "unknown"
    return "advanced" if current > previous else "unchanged"


def resume_args(
    *, session_id: str, source, resume_id: str, parent_id: str, native: bool
) -> list[str]:
    del source, resume_id, parent_id, native
    return ["--resume", session_id]


def new_session_args(*, session_id: str | None, name: str = "") -> list[str]:
    del name
    return ["-s", session_id] if session_id else []


def write(
    *, cwd: str, turns: list[Turn], prepared, title: str = "", created: float | None = None
) -> NativeWrite:
    del prepared
    session_id = str(uuid.uuid4())
    directory = session_dir(cwd, session_id)
    kept = list(turns)
    write_updates(directory, session_id, kept)
    write_summary(directory, session_id, cwd, title, created)
    cache = directory / "chat_history.jsonl"
    if cache.exists():
        cache.unlink()
    storage = directory / "updates.jsonl"
    return NativeWrite(NativeRef(session_id, str(storage)), storage.stat().st_size)


def publish_name(session, name: str) -> str:
    ref = resolve(getattr(session, "session_id", "") or session)
    if ref is None:
        raise OSError("grok session was not found")
    if live_pid(ref.session_id):
        raise OSError("open grok session titles change through the ingress")
    summary_path = Path(ref.storage).parent / "summary.json"
    summary = _load_json(summary_path)
    summary["generated_title"] = name
    summary["title_is_manual"] = True
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return name


def _active_rows(home: Path | None = None) -> list[dict]:
    path = (home or grok_home()) / "active_sessions.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    # os.kill(pid, 0) raises WinError 87 for a live windowless grok.exe.
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        kernel.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5


def live_pid(session_id: str, home: Path | None = None) -> int | None:
    for row in _active_rows(home):
        if row.get("session_id") == session_id and _pid_alive(int(row.get("pid") or 0)):
            return int(row["pid"])
    return None


def inspect_liveness(context, home: Path, sessions) -> dict[str, int]:
    del context
    found: dict[str, int] = {}
    for row in _active_rows(home):
        pid = int(row.get("pid") or 0)
        session_id = str(row.get("session_id") or "")
        if session_id and _pid_alive(pid):
            found[session_id] = pid
    del sessions
    return found


def current_session(pid: int, started: float) -> NativeRef | None:
    del started
    for row in _active_rows():
        if int(row.get("pid") or 0) == pid and _pid_alive(pid):
            ref = resolve(str(row.get("session_id") or ""))
            if ref is not None:
                return ref
    return None


def transcript_files(ref: NativeRef) -> tuple[Path, ...]:
    directory = plain_path(Path(ref.storage)).parent
    files = []
    for name in ("chat_history.jsonl", "updates.jsonl"):
        files.append(directory / name)
    return tuple(files)


def plan_delete(ref: NativeRef) -> DeletePlan:
    if live_pid(ref.session_id):
        raise DeletionError("refusing to delete a grok session with a live pid")
    return DeletePlan(
        "grok", ref, checkpoint(ref), (), notes=("session directory",), native_delete=True
    )


def execute_delete(plan: DeletePlan) -> None:
    if live_pid(plan.native.session_id):
        raise DeletionError("refusing to delete a grok session with a live pid")
    directory = Path(plan.native.storage).parent
    if directory.is_dir():
        shutil.rmtree(directory)


def _generation(session_id: str, pid: int, started: float) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps([session_id, pid, started], separators=(",", ":")).encode()
    ).hexdigest()


def _owner_pipe(session_id: str) -> str:
    return rf"\\.\pipe\grok-session-{session_id}"


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _pipe_open(session_id: str) -> bool:
    """A named pipe does not show up through os.path.exists."""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel = _kernel32()
    if kernel.WaitNamedPipeW(_owner_pipe(session_id), 50):
        return True
    # 2 is file-not-found. A timeout or busy pipe still means the owner is listening.
    return ctypes.get_last_error() not in (0, 2, 3)


def _write_owner_pipe(session_id: str, payload: str, timeout: float = 15) -> dict:
    """Write one peer frame to the live owner. The installed Grok must be listening."""
    import ctypes
    import queue
    import threading
    from ctypes import wintypes

    if not _pipe_open(session_id):
        return {
            "status": "unavailable",
            "reason": (
                "this Grok process is not listening for peer messages. "
                f"Nothing is bound at {_owner_pipe(session_id)}."
            ),
        }
    result: queue.Queue[dict] = queue.Queue(maxsize=1)

    def deliver() -> None:
        kernel = _kernel32()
        invalid = wintypes.HANDLE(-1).value
        handle = kernel.CreateFileW(
            _owner_pipe(session_id),
            0x80000000 | 0x40000000,
            0,
            None,
            3,
            0,
            None,
        )
        if not handle or handle == invalid:
            result.put(
                {
                    "status": "unavailable",
                    "reason": f"owner pipe open failed ({ctypes.get_last_error()})",
                }
            )
            return
        try:
            data = (payload + "\n").encode("utf-8")
            wrote = wintypes.DWORD(0)
            buffer = ctypes.create_string_buffer(data)
            if not kernel.WriteFile(handle, buffer, len(data), ctypes.byref(wrote), None):
                result.put(
                    {
                        "status": "unavailable",
                        "reason": f"owner pipe write failed ({ctypes.get_last_error()})",
                    }
                )
                return
            collected = bytearray()
            chunk = ctypes.create_string_buffer(4096)
            read = wintypes.DWORD(0)
            while b"\n" not in collected and len(collected) < 65536:
                if (
                    not kernel.ReadFile(handle, chunk, 4096, ctypes.byref(read), None)
                    or read.value == 0
                ):
                    break
                collected.extend(chunk.raw[: read.value])
            text = collected.decode("utf-8", errors="replace").strip()
            try:
                body = json.loads(text.splitlines()[0] if text else "{}")
            except json.JSONDecodeError:
                body = {}
            if isinstance(body, dict) and body.get("status") == "accepted":
                result.put({"status": "accepted"})
            else:
                result.put(
                    {
                        "status": "unavailable",
                        "reason": text or "owner pipe did not accept the message",
                    }
                )
        finally:
            kernel.CloseHandle(handle)

    worker = threading.Thread(target=deliver, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return {
            "status": "uncertain",
            "reason": "Grok owner pipe timed out; no automatic retry.",
        }
    try:
        return result.get_nowait()
    except queue.Empty:
        return {"status": "unavailable", "reason": "owner pipe returned nothing"}


def messaging_register(payload: dict, env: dict) -> dict | None:
    """Record the live grok.exe owner so `sessions messaging status` can see it."""
    import psutil

    session_id = str(payload.get("session_id") or env.get("GROK_SESSION_ID") or "")
    if not session_id:
        return None
    try:
        current = psutil.Process()
        chain = (current, *current.parents())
    except psutil.Error:
        return None
    owner = next((item for item in chain if item.name().lower() in {"grok.exe", "grok"}), None)
    if owner is None:
        return None
    try:
        started = owner.create_time()
    except psutil.Error:
        return None
    return {
        "tool": "grok",
        "session_id": session_id,
        "name": payload.get("name") or session_id,
        "cwd": payload.get("cwd") or "",
        "pid": owner.pid,
        "started": started,
        "home": env.get("GROK_HOME") or str(grok_home()),
        "address": _owner_pipe(session_id) if _pipe_open(session_id) else "",
        "vault_name": f"grok-{session_id}",
        "generation": _generation(session_id, owner.pid, started),
    }


def messaging_forget(row: dict) -> dict:
    del row
    return {"status": "ok"}


def message_send(receiver: dict, text: str, message_id: str, sender: dict) -> dict:
    session_id = str(receiver.get("session_id") or "")
    if not live_pid(session_id):
        return {
            "status": "unavailable",
            "reason": "grok owner is not live",
            "message_id": message_id,
        }
    payload = json.dumps(
        {
            "type": "peer_message",
            "message_id": message_id,
            "sender": sender,
            "text": text,
        },
        ensure_ascii=False,
    )
    outcome = _write_owner_pipe(session_id, payload)
    if outcome.get("status") == "accepted":
        return {
            "status": "submitted",
            "message_id": message_id,
            "transport": "grok-owner-pipe",
            "recipient": {"tool": "grok", "session_id": session_id},
            "sender": sender,
        }
    outcome["message_id"] = message_id
    return outcome


ADAPTER = HarnessAdapter(
    name="grok",
    label="Grok",
    short_label="Grok",
    letter="G",
    order=30,
    home=grok_home(),
    default_command=("grok",),
    dangerous_args=("--always-approve",),
    source_kinds=frozenset((SourceKind.INTERACTIVE,)),
    id_patterns=(ID_PATTERN,),
    liveness_executables=frozenset(("grok", "grok.exe")),
    contain_launch=True,
    read=_read,
    write=write,
    resolve=resolve,
    availability=availability,
    checkpoint=checkpoint,
    change_status=change_status,
    discover=discover,
    rediscover=rediscover,
    resume_args=resume_args,
    new_session_args=new_session_args,
    publish_name=publish_name,
    inspect_liveness=inspect_liveness,
    read_since=read_since,
    transcript_files=transcript_files,
    plan_delete=plan_delete,
    execute_delete=execute_delete,
    current_session=current_session,
    restart_discard_options=("--resume", "--session-id", "--fork-session"),
    messaging=MessagingHooks(
        send=message_send,
        register=messaging_register,
        forget=messaging_forget,
        hook_file="hooks/sessions-messaging.json",
        sender_environment="GROK_SESSION_ID",
    ),
    budget=BudgetPolicy(
        context_tokens=500_000,
        usable_fraction=ONE_M_CONTEXT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source="Grok Build context_window recorded on this PC, 2026-11-22",
    ),
)
