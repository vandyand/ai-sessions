"""OpenCode CLI harness adapter backed by its shared WAL-mode SQLite store."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..capabilities import HarnessAdapter
from ..conversion import DEFAULT_CHARS_PER_TOKEN, DEFAULT_USABLE_FRACTION, BridgeError
from ..diagnostics import record_warning
from ..discovery import EvidenceAccumulator, HarnessContext, clean_prompt, timestamp
from ..liveness import LivenessContext
from ..model import (
    Availability,
    BudgetPolicy,
    LivenessSession,
    NativeRef,
    NativeSession,
    NativeWrite,
    PreparedTarget,
    ReadSnapshot,
    SourceKind,
    Turn,
)
from ..paths import APP_CACHE_DIR, HOME, IS_WINDOWS, env_path
from ..registry import REGISTRY
from .opencode_semantics import (
    CHECKPOINT_SCHEME,
    StoredMessage,
    normalize_revert,
    project_view,
    select_compacted_newest,
    semantic_checkpoint,
)
from .opencode_write import (
    GeneratedExport,
    build_export,
    choose_model,
    parse_model_ids,
    parse_verbose_models,
)

OPENCODE_CONTEXT_FLOOR_TOKENS = 128_000
DB_PATH_TIMEOUT_SECONDS = 3.0
MAINTENANCE_OUTPUT_BYTES = 1_048_576
DISCOVERY_JSON_BYTES = 262_144
OPENCODE_EVIDENCE_BYTES = 16_777_216
DISCOVERY_PREVIEW_CHARS = 4_096
DISCOVERY_WARNING_CHARS = 512
SEMANTIC_BUSY_TIMEOUT_SECONDS = 2.0
MODEL_TIMEOUT_SECONDS = 10.0
IMPORT_TIMEOUT_SECONDS = 30.0
IMPORT_VERIFY_DELAYS = (0.0, 0.05, 0.1, 0.2, 0.4)
_DEFAULT_TITLE = re.compile(
    r"^(?:New session - |Child session - )\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_REQUIRED_COLUMNS = {
    "session": {
        "id",
        "parent_id",
        "directory",
        "title",
        "time_created",
        "time_updated",
        "time_archived",
    },
    "message": {"id", "session_id", "time_created", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}
_SEMANTIC_REQUIRED_COLUMNS = {"session": {"revert"}}
_SEMANTIC_CHECKPOINT = re.compile(rf"{re.escape(CHECKPOINT_SCHEME)}[0-9a-f]{{64}}\Z")
_IMPORTED_SESSION = re.compile(r"(?m)^Imported session:\s*(ses_[0-9A-Za-z]{26})\s*$")


def _default_home() -> Path:
    if IS_WINDOWS:
        data = env_path("LOCALAPPDATA", HOME / "AppData" / "Local")
    else:
        data = env_path("XDG_DATA_HOME", HOME / ".local" / "share")
    return data / "opencode"


OPENCODE_HOME = _default_home()


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class DatabaseBinding:
    path: Path
    command: tuple[str, ...]
    authoritative: bool
    cwd: str
    notice: str = ""


@dataclass(slots=True)
class _DiscoveryState:
    session_id: str
    parent_id: str
    directory: str
    native_title: str
    agent: str
    created: float
    updated: float
    archived: bool
    evidence: EvidenceAccumulator
    first_user: str = ""
    latest_user: str = ""
    user_messages: int = 0
    turn_count: int = 0
    compaction_count: int = 0
    prompt_count: int = 0
    first_user_key: tuple[float, str] | None = None
    latest_user_key: tuple[float, str] | None = None
    revert_message_id: str = ""
    revert_part_id: str = ""
    revert_seen: bool = False
    compaction_ids: set[str] = field(default_factory=set)
    completed_compaction_parents: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    sessions: tuple[NativeSession, ...]
    evidence: tuple[tuple[str, tuple[str, ...], bool], ...]
    malformed: bool
    oversized: bool


_BINDING_LOCK = threading.RLock()
_LAST_BINDING: DatabaseBinding | None = None
_AUTHORITATIVE_BINDINGS: dict[tuple[tuple[str, ...], str], DatabaseBinding] = {}
_DISCOVERY_CACHE_LOCK = threading.RLock()
_DISCOVERY_CACHE: dict[tuple[str, str, tuple[Any, ...]], _CachedDiscovery] = {}
_DISCOVERY_CACHE_SIZE = 8
_DISCOVERY_CACHE_VERSION = 2
_DISCOVERY_CACHE_MAX_BYTES = 64 * 1024 * 1024
OPENCODE_DISCOVERY_CACHE_FILE = APP_CACHE_DIR / "opencode-discovery-v1.json"


class _WindowsJob:
    """Best-effort kill-on-close job for one Windows maintenance process tree."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def assign(cls, process: subprocess.Popen[bytes]) -> "_WindowsJob | None":
        if not IS_WINDOWS:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            kernel32.AssignProcessToJobObject.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
            )
            kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = 0x00002000
            configured = kernel32.SetInformationJobObject(
                handle,
                9,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            assigned = configured and kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(int(process._handle))
            )
            if not assigned:
                kernel32.CloseHandle(handle)
                return None
            return cls(int(handle), kernel32)
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def close(self) -> None:
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)
            self.kernel32.CloseHandle(self.handle)
            self.handle = 0


def _adapter_home() -> Path:
    return REGISTRY.get("opencode").home


def _safe_expanduser(path: Path) -> Path:
    try:
        return path.expanduser()
    except (OSError, RuntimeError, ValueError):
        return path


def _resolved_executable(value: str) -> str | None:
    resolved = shutil.which(str(_safe_expanduser(Path(value))))
    return str(Path(resolved).absolute()) if resolved else None


def _cmd_quote(value: str) -> str:
    trailing = len(value) - len(value.rstrip("\\"))
    return '"' + value + "\\" * trailing + '"'


def _drain_bounded(
    stream: Any,
    output: bytearray,
    truncated: list[bool],
    stop: threading.Event,
) -> None:
    """Poll and drain a child pipe without an uninterruptible EOF wait."""
    fd = stream.fileno()
    peek: Any = None
    pipe_handle: Any = None
    pending_type: Any = None
    if IS_WINDOWS:
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            peek = kernel32.PeekNamedPipe
            peek.argtypes = (
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            )
            peek.restype = wintypes.BOOL
            pipe_handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
            pending_type = wintypes.DWORD
        except (AttributeError, OSError, ValueError):
            peek = None
    else:
        os.set_blocking(fd, False)
    stopping_reads = 0
    maximum_stopping_reads = (MAINTENANCE_OUTPUT_BYTES + 65_535) // 65_536
    try:
        while True:
            stopping = stop.is_set()
            available = 65_536
            if IS_WINDOWS:
                try:
                    pending = pending_type()
                    readable = (
                        peek(pipe_handle, None, 0, None, ctypes.byref(pending), None)
                        if peek
                        else False
                    )
                    available = min(65_536, pending.value) if readable else 0
                except (OSError, TypeError, ValueError):
                    available = 0
            try:
                chunk = os.read(fd, available) if available else b""
            except (BlockingIOError, OSError):
                chunk = b""
            if not chunk:
                if stopping:
                    break
                time.sleep(0.005)
                continue
            remaining = MAINTENANCE_OUTPUT_BYTES - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[0] = True
            if stopping:
                # Drain at most one full output allowance after the owner says
                # stop. This preserves already-buffered parent output without
                # following a detached descendant that inherited the pipe forever.
                stopping_reads += 1
                if truncated[0] or stopping_reads >= maximum_stopping_reads:
                    break
    finally:
        stream.close()


def _wait_posix_unreaped(process: subprocess.Popen[bytes], deadline: float) -> bool | None:
    """Wait without reaping; ``None`` means another handler already reaped it."""
    while True:
        try:
            status = os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOWAIT | os.WNOHANG,
            )
        except ChildProcessError:
            return None
        if status is not None and status.si_pid == process.pid:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))


def run_maintenance(
    command: tuple[str, ...],
    arguments: tuple[str, ...],
    *,
    cwd: str,
    timeout: float,
    return_timeout: bool = False,
) -> MaintenanceResult:
    """Run one bounded, non-interactive provider maintenance command."""
    if not command:
        raise BridgeError("OpenCode maintenance command is empty")
    if any(not isinstance(value, str) or not value for value in (*command, *arguments)):
        raise BridgeError("OpenCode maintenance command contains an invalid argument")
    if any("\0" in value for value in (*command, *arguments)):
        raise BridgeError("OpenCode maintenance command contains a NUL character")
    executable = _resolved_executable(command[0])
    if executable is None:
        raise BridgeError(f"OpenCode command {command[0]!r} is not available")
    argv = (executable, *command[1:], *arguments)
    invocation: list[str] | str = list(argv)
    creationflags = 0
    if IS_WINDOWS:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if Path(executable).suffix.casefold() in (".cmd", ".bat"):
            if any(
                any(character in value for character in ("%", '"', "\r", "\n")) for value in argv
            ):
                raise BridgeError(
                    "OpenCode .cmd commands cannot safely receive percent signs, quotes, "
                    "or line breaks"
                )
            command_line = " ".join(_cmd_quote(value) for value in argv)
            command_shell = os.environ.get("COMSPEC") or "cmd.exe"
            # Every value is quoted for cmd rather than with MSVCRT's incompatible
            # argv rules. Percent and quote are rejected above because cmd expands
            # them even inside quotes; the remaining metacharacters are inert there.
            invocation = (
                f'{subprocess.list2cmdline([command_shell])} /d /v:off /s /c "{command_line}"'
            )
    popen_options: dict[str, Any] = {}
    if not IS_WINDOWS:
        popen_options["start_new_session"] = True
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            invocation,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            **popen_options,
        )
    except OSError as error:
        raise BridgeError(f"OpenCode maintenance command could not start: {error}") from error
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise BridgeError("OpenCode maintenance command capture pipes were unavailable")
    stdout_raw = bytearray()
    stderr_raw = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]
    stop = threading.Event()
    job = _WindowsJob.assign(process)
    readers = (
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout_raw, stdout_truncated, stop),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr_raw, stderr_truncated, stop),
            daemon=True,
        ),
    )
    streams = (process.stdout, process.stderr)
    started: list[threading.Thread] = []
    timed_out = False
    returncode = -1
    posix_owned = True
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        if IS_WINDOWS:
            try:
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        else:
            posix_wait = _wait_posix_unreaped(process, deadline)
            posix_owned = posix_wait is not None
            timed_out = posix_wait is False
    finally:
        # A maintenance command must not leave a background server holding our
        # capture pipes. Preserve the POSIX PID as a zombie until its process
        # group is gone, and close a Windows job before the final wait.
        if job is not None:
            job.close()
        elif not IS_WINDOWS and posix_owned:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        elif IS_WINDOWS and process.poll() is None:
            process.kill()
        if not IS_WINDOWS:
            returncode = process.wait()
        elif process.poll() is None:
            try:
                returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
        elif process.returncode is not None:
            returncode = process.returncode
        stop.set()
        for reader, stream in zip(readers, streams, strict=True):
            if reader not in started:
                stream.close()
                continue
            reader.join(timeout=max(0.1, deadline - time.monotonic()))
            if reader.is_alive():
                # The reader owns this stream. Closing its descriptor here can
                # race descriptor reuse while it is inside os.read(). The pipe
                # is nonblocking/polled and stop is set, so give it one final
                # scheduling interval and report timeout if it still cannot exit.
                reader.join(timeout=0.1)
    timed_out = timed_out or any(reader.is_alive() for reader in readers)
    if timed_out and not return_timeout:
        raise BridgeError(f"OpenCode maintenance command timed out after {timeout:g} seconds")
    return MaintenanceResult(
        argv,
        returncode,
        stdout_raw.decode("utf-8", "replace"),
        stderr_raw.decode("utf-8", "replace"),
        stdout_truncated[0] or stderr_truncated[0],
        timed_out,
    )


def _fallback_database() -> Path:
    override = os.environ.get("OPENCODE_DB")
    if (
        override
        and "\0" not in override
        and not override.casefold().startswith((":memory:", "file:"))
    ):
        candidate = _safe_expanduser(Path(override))
        return candidate if candidate.is_absolute() else _adapter_home() / candidate
    return _adapter_home() / "opencode.db"


def _parse_database_path(output: str, cwd: str) -> Path:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or "\0" in lines[0] or lines[0].casefold().startswith((":memory:", "file:")):
        raise BridgeError("OpenCode db path returned no single persistent database path")
    candidate = _safe_expanduser(Path(lines[0]))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return candidate.resolve()


def _maintenance_detail(result: MaintenanceResult) -> str:
    if result.timed_out:
        return "maintenance command timed out"
    detail = clean_prompt(result.stderr) or clean_prompt(result.stdout)
    if len(detail) > DISCOVERY_WARNING_CHARS:
        detail = detail[: DISCOVERY_WARNING_CHARS - 2].rstrip() + " …"
    return detail or f"exit {result.returncode}"


def _command_database_path(command: tuple[str, ...], cwd: str) -> Path:
    result = run_maintenance(
        command,
        ("db", "path"),
        cwd=cwd,
        timeout=DB_PATH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or result.truncated:
        raise BridgeError(f"OpenCode db path failed: {_maintenance_detail(result)}")
    return _parse_database_path(result.stdout, cwd)


def _prepare_opencode_target(
    command: tuple[str, ...], cwd: str, options: dict[str, Any]
) -> PreparedTarget:
    operation_cwd = str(Path(cwd or os.getcwd()).resolve())
    if not Path(operation_cwd).is_dir():
        raise BridgeError(f"OpenCode target working directory does not exist: {operation_cwd}")
    configured_model = options.get("bridge_model", "")
    if configured_model is None:
        configured_model = ""
    if not isinstance(configured_model, str):
        raise BridgeError("OpenCode provider bridge_model must be a string")
    installed_result = run_maintenance(
        command,
        ("models",),
        cwd=operation_cwd,
        timeout=MODEL_TIMEOUT_SECONDS,
    )
    if installed_result.returncode != 0 or installed_result.truncated:
        raise BridgeError(
            f"OpenCode models failed while preparing the target: "
            f"{_maintenance_detail(installed_result)}"
        )
    installed = parse_model_ids(installed_result.stdout)
    if configured_model and configured_model not in installed:
        raise BridgeError(
            f"configured OpenCode bridge_model {configured_model!r} is not installed; "
            "choose a model listed by `opencode models`"
        )
    database = _command_database_path(command, operation_cwd)
    verbose_result = run_maintenance(
        command,
        ("models", "--verbose"),
        cwd=operation_cwd,
        timeout=MODEL_TIMEOUT_SECONDS,
    )
    notices: list[str] = []
    selected_full = configured_model or installed[0]
    provider_id, model_id = selected_full.split("/", 1)
    policy = BudgetPolicy(
        OPENCODE_CONTEXT_FLOOR_TOKENS,
        DEFAULT_USABLE_FRACTION,
        DEFAULT_CHARS_PER_TOKEN,
        "OpenCode 128k fallback because verbose model metadata was unavailable",
        hard_limit=True,
    )
    verbose_models = None
    verbose_error: BridgeError | None = None
    try:
        if verbose_result.returncode != 0 or verbose_result.truncated:
            raise BridgeError(_maintenance_detail(verbose_result))
        verbose_models = parse_verbose_models(verbose_result.stdout)
    except BridgeError as error:
        verbose_error = error
    if verbose_models is None:
        notices.append(
            "OpenCode verbose model metadata could not be used; applying the conservative "
            f"128,000-token fallback for {selected_full}; its real limit is unknown and may be "
            f"smaller ({verbose_error})"
        )
    else:
        selected = choose_model(
            installed,
            verbose_models,
            explicit=configured_model,
            minimum_input_tokens=OPENCODE_CONTEXT_FLOOR_TOKENS,
        )
        selected_full = selected.full_id
        provider_id = selected.provider_id
        model_id = selected.model_id
        try:
            policy = BudgetPolicy(
                selected.effective_input_tokens,
                DEFAULT_USABLE_FRACTION,
                DEFAULT_CHARS_PER_TOKEN,
                f"OpenCode model {selected.full_id} effective input limit",
                hard_limit=True,
            )
        except ValueError as error:
            raise BridgeError(
                f"OpenCode model {selected.full_id!r} has too little usable input capacity "
                "for required bridge metadata; configure a larger bridge_model"
            ) from error
    return PreparedTarget(
        command,
        policy,
        (
            ("database", str(database)),
            ("model", selected_full),
            ("provider_id", provider_id),
            ("model_id", model_id),
        ),
        tuple(notices),
        (
            f"OpenCode target model {selected_full}; prepared semantic input policy "
            f"{policy.context_tokens:,} tokens.",
        ),
    )


def _binding(context: HarnessContext) -> DatabaseBinding:
    global _LAST_BINDING
    cached = context.adapter_state.get("opencode.database")
    if isinstance(cached, DatabaseBinding):
        return cached
    command = context.provider_command("opencode")
    operation_cwd = str(Path.cwd().resolve())
    fallback_path = _fallback_database().resolve()
    authority_key = (command, operation_cwd)
    # A refresh creates a fresh HarnessContext. Remember a failed probe across
    # those contexts so an unavailable or wedged CLI does not cost three seconds
    # on every UI refresh. A changed command, cwd, or fallback path retries.
    with _BINDING_LOCK:
        proven = _AUTHORITATIVE_BINDINGS.get(authority_key)
        previous = _LAST_BINDING
        if (
            proven is None
            and previous is not None
            and not previous.authoritative
            and previous.command == command
            and previous.cwd == operation_cwd
            and previous.path == fallback_path
        ):
            context.adapter_state["opencode.database"] = previous
            return previous
    try:
        result = run_maintenance(
            command,
            ("db", "path"),
            cwd=operation_cwd,
            timeout=DB_PATH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or result.truncated:
            detail = clean_prompt(result.stderr)
            if len(detail) > DISCOVERY_WARNING_CHARS:
                detail = detail[: DISCOVERY_WARNING_CHARS - 2].rstrip() + " …"
            detail = detail or f"exit {result.returncode}"
            raise BridgeError(f"OpenCode db path failed: {detail}")
        binding = DatabaseBinding(
            _parse_database_path(result.stdout, operation_cwd), command, True, operation_cwd
        )
    except (BridgeError, OSError, RuntimeError, ValueError) as error:
        with _BINDING_LOCK:
            proven = _AUTHORITATIVE_BINDINGS.get(authority_key)
        if proven is not None:
            binding = DatabaseBinding(
                proven.path,
                command,
                True,
                operation_cwd,
                "OpenCode database path probe failed; continuing with the previously proven "
                f"authoritative binding {proven.path} ({error})",
            )
        else:
            notice = (
                f"OpenCode database binding is inferred as {fallback_path}; configured command "
                f"could not report db path ({error})"
            )
            binding = DatabaseBinding(fallback_path, command, False, operation_cwd, notice)
    context.adapter_state["opencode.database"] = binding
    with _BINDING_LOCK:
        if binding.authoritative and not binding.notice:
            _AUTHORITATIVE_BINDINGS[authority_key] = binding
        _LAST_BINDING = binding
    return binding


def _storage_state(path: Path) -> Availability:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        try:
            path.parent.stat()
        except FileNotFoundError:
            return Availability.UNAVAILABLE
        except OSError:
            return Availability.UNKNOWN
        return Availability.UNAVAILABLE
    except OSError:
        return Availability.UNKNOWN
    return Availability.AVAILABLE if stat.S_ISREG(metadata.st_mode) else Availability.UNKNOWN


def _store_stamp(path: Path) -> tuple[Any, ...] | None:
    values: list[Any] = []
    members = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    for index, member in enumerate(members):
        try:
            metadata = member.stat()
        except FileNotFoundError:
            values.append((str(member), False))
        except OSError:
            return None
        else:
            try:
                with member.open("rb") as handle:
                    if index == 0:
                        handle.seek(24)
                        header = handle.read(8)
                        handle.seek(40)
                        header += handle.read(4)
                        handle.seek(92)
                        header += handle.read(4)
                    elif index == 1:
                        header = handle.read(24)
                    else:
                        # Two WalIndexHdr copies carry mxFrame. Ordinary WAL
                        # commits can overwrite frames without changing the
                        # main DB, WAL size, or WAL file header.
                        header = handle.read(96)
            except OSError:
                return None
            values.append(
                (
                    str(member),
                    True,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    0 if index == 2 else metadata.st_mtime_ns,
                    header,
                )
            )
    return tuple(values)


def _discovery_cache_fingerprint(key: tuple[str, str, tuple[Any, ...]]) -> str:
    return hashlib.sha256(repr(key).encode("utf-8", "surrogatepass")).hexdigest()


def _load_persistent_discovery(
    key: tuple[str, str, tuple[Any, ...]],
) -> _CachedDiscovery | None:
    path = OPENCODE_DISCOVERY_CACHE_FILE
    try:
        if path.stat().st_size > _DISCOVERY_CACHE_MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("version") != _DISCOVERY_CACHE_VERSION
            or document.get("fingerprint") != _discovery_cache_fingerprint(key)
        ):
            return None
        raw_sessions = document.get("sessions")
        raw_evidence = document.get("evidence")
        malformed = document.get("malformed")
        oversized = document.get("oversized")
        if (
            not isinstance(raw_sessions, list)
            or not isinstance(raw_evidence, list)
            or not isinstance(malformed, bool)
            or not isinstance(oversized, bool)
            or len(raw_sessions) > 100_000
            or len(raw_evidence) > 100_000
        ):
            return None
        session_fields = {field.name for field in fields(NativeSession)}
        sessions: list[NativeSession] = []
        for raw in raw_sessions:
            if not isinstance(raw, dict) or set(raw) != session_fields:
                return None
            values = dict(raw)
            if values.get("tool") != "opencode" or values.get("storage") != key[0]:
                return None
            values["source"] = SourceKind(values["source"])
            sessions.append(NativeSession(**values))
        evidence: list[tuple[str, tuple[str, ...], bool]] = []
        for raw in raw_evidence:
            if (
                not isinstance(raw, list)
                or len(raw) != 3
                or not isinstance(raw[0], str)
                or not isinstance(raw[1], list)
                or not all(isinstance(token, str) for token in raw[1])
                or not isinstance(raw[2], bool)
            ):
                return None
            evidence.append((raw[0], tuple(raw[1]), raw[2]))
        if {item.session_id for item in sessions} != {item[0] for item in evidence}:
            return None
        return _CachedDiscovery(tuple(sessions), tuple(evidence), malformed, oversized)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _save_persistent_discovery(
    key: tuple[str, str, tuple[Any, ...]], cached: _CachedDiscovery
) -> None:
    path = OPENCODE_DISCOVERY_CACHE_FILE
    document = {
        "version": _DISCOVERY_CACHE_VERSION,
        "fingerprint": _discovery_cache_fingerprint(key),
        "sessions": [asdict(session) for session in cached.sessions],
        "evidence": [
            [session_id, list(tokens), truncated]
            for session_id, tokens, truncated in cached.evidence
        ],
        "malformed": cached.malformed,
        "oversized": cached.oversized,
    }
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _publish_cached_discovery(cached: _CachedDiscovery, context: HarnessContext) -> None:
    for session_id, tokens, truncated in cached.evidence:
        context.evidence[("opencode", session_id)] = (list(tokens), truncated)
    for session in cached.sessions:
        context.index_native("opencode", session.session_id, session.storage)
    context.complete_native_index("opencode")
    if cached.malformed:
        record_warning("OpenCode discovery skipped malformed session data")
    if cached.oversized:
        record_warning("OpenCode discovery omitted details from oversized session data")


def _cached_discovery(
    key: tuple[str, str, tuple[Any, ...]], context: HarnessContext
) -> list[NativeSession] | None:
    with _DISCOVERY_CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(key)
    if cached is None:
        cached = _load_persistent_discovery(key)
        if cached is None:
            return None
        with _DISCOVERY_CACHE_LOCK:
            _DISCOVERY_CACHE[key] = cached
            while len(_DISCOVERY_CACHE) > _DISCOVERY_CACHE_SIZE:
                _DISCOVERY_CACHE.pop(next(iter(_DISCOVERY_CACHE)))
    _publish_cached_discovery(cached, context)
    return list(cached.sessions)


def _remember_discovery(
    key: tuple[str, str, tuple[Any, ...]],
    sessions: list[NativeSession],
    context: HarnessContext,
    malformed: bool,
    oversized: bool,
) -> None:
    evidence = tuple(
        (
            session.session_id,
            tuple(context.evidence[("opencode", session.session_id)][0]),
            context.evidence[("opencode", session.session_id)][1],
        )
        for session in sessions
    )
    cached = _CachedDiscovery(tuple(sessions), evidence, malformed, oversized)
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE[key] = cached
        while len(_DISCOVERY_CACHE) > _DISCOVERY_CACHE_SIZE:
            _DISCOVERY_CACHE.pop(next(iter(_DISCOVERY_CACHE)))
    _save_persistent_discovery(key, cached)


def _sqlite_readonly_uri(path: Path) -> str:
    resolved = path.resolve()
    posix = resolved.as_posix()
    prefix = "file://" if posix.startswith("//") else "file:"
    return f"{prefix}{urllib.parse.quote(posix, safe='/:')}?mode=ro"


def _sqlite_readwrite_uri(path: Path) -> str:
    posix = path.resolve().as_posix()
    prefix = "file:/" if IS_WINDOWS and not posix.startswith("/") else "file:"
    return f"{prefix}{urllib.parse.quote(posix, safe='/:')}?mode=rw"


def _connect_existing(path: Path, *, timeout_seconds: float = 0.25) -> sqlite3.Connection:
    state = _storage_state(path)
    if state is Availability.UNAVAILABLE:
        raise FileNotFoundError(path)
    if state is Availability.UNKNOWN:
        raise OSError(f"OpenCode database path is inaccessible: {path}")
    connection = sqlite3.connect(
        _sqlite_readonly_uri(path),
        uri=True,
        timeout=timeout_seconds,
    )
    connection.text_factory = lambda raw: raw.decode("utf-8", "replace")
    try:
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1_000))}")
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


def _schema_columns(
    connection: sqlite3.Connection, *, semantic: bool = False
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        result[table] = columns
        semantic_required = _SEMANTIC_REQUIRED_COLUMNS.get(table, set()) if semantic else set()
        missing = (required | semantic_required) - columns
        if missing:
            raise BridgeError(
                f"OpenCode database schema is incompatible: {table} lacks "
                + ", ".join(sorted(missing))
            )
        if table in ("message", "part"):
            try:
                connection.execute(f"SELECT rowid FROM {table} LIMIT 0")
            except sqlite3.Error as error:
                raise BridgeError(
                    f"OpenCode database schema is incompatible: {table} is not rowid-addressable"
                ) from error
    return result


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, MemoryError, RecursionError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _strict_json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise BridgeError(f"OpenCode {label} is not valid UTF-8") from error
    if not isinstance(value, str):
        raise BridgeError(f"OpenCode {label} is not stored as JSON text")
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
    except (RecursionError, TypeError, ValueError) as error:
        raise BridgeError(f"OpenCode {label} is malformed JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise BridgeError(f"OpenCode {label} must be a JSON object")
    return parsed


def _prefix_bytes(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return None


def _read_data(
    connection: sqlite3.Connection,
    table: str,
    rowid: int,
    storage_type: str,
    evidence: EvidenceAccumulator | None,
) -> tuple[bytes, bool]:
    """Read bounded JSON and independently stream bounded identity evidence."""
    if storage_type in ("text", "blob"):
        with connection.blobopen(table, "data", rowid, readonly=True) as blob:
            total = len(blob)
            evidence_remaining = evidence.remaining_bytes() if evidence is not None else 0
            evidence_limit = total if evidence_remaining is None else min(total, evidence_remaining)
            read_limit = max(min(total, DISCOVERY_JSON_BYTES), evidence_limit)
            prefix = bytearray()
            offset = 0
            while offset < read_limit:
                chunk = blob.read(min(65_536, read_limit - offset))
                if not chunk:
                    break
                if len(prefix) < DISCOVERY_JSON_BYTES:
                    keep = DISCOVERY_JSON_BYTES - len(prefix)
                    prefix.extend(chunk[:keep])
                if evidence is not None and offset < evidence_limit:
                    evidence_chunk = chunk[: evidence_limit - offset]
                    evidence.scan_contiguous(
                        evidence_chunk,
                        reset=offset == 0,
                        final=offset + len(evidence_chunk) == total,
                    )
                offset += len(chunk)
            if evidence is not None and total > evidence_limit:
                evidence.truncated = True
            return bytes(prefix), total > len(prefix)
    value = connection.execute(f"SELECT data FROM {table} WHERE rowid=?", (rowid,)).fetchone()
    raw = _prefix_bytes(value[0]) if value is not None else None
    if raw is None:
        return b"", False
    if evidence is not None:
        evidence.scan(raw)
    return raw[:DISCOVERY_JSON_BYTES], len(raw) > DISCOVERY_JSON_BYTES


def _finish_user(
    state: _DiscoveryState | None,
    parts: list[str],
    order: tuple[float, str],
) -> None:
    if state is None:
        return
    text = clean_prompt(" ".join(parts))
    if not text:
        return
    if state.first_user_key is None or order < state.first_user_key:
        state.first_user = text
        state.first_user_key = order
    if state.latest_user_key is None or order > state.latest_user_key:
        state.latest_user = text
        state.latest_user_key = order


def _append_preview_part(parts: list[str], text: str, retained: int) -> int:
    separator = 1 if parts else 0
    remaining = DISCOVERY_PREVIEW_CHARS - retained - separator
    if remaining <= 0:
        return retained
    value = text[:remaining]
    if value:
        parts.append(value)
        retained += separator + len(value)
    return retained


def _native_identifier(value: Any) -> tuple[bytes, str] | None:
    if isinstance(value, bytes):
        raw = value
        try:
            text = value.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        text = value
        raw = value.encode("utf-8")
    else:
        return None
    return (raw, text) if text else None


def discover(context: HarnessContext, *, use_cache: bool = True) -> list[NativeSession]:
    """Discover roots, children, and archived OpenCode sessions from one authoritative DB."""
    cache_enabled = use_cache and context.use_cache
    connection: sqlite3.Connection | None = None
    malformed = False
    oversized = False
    cache_key: tuple[str, str, tuple[Any, ...]] | None = None
    cache_stable = False
    try:
        binding = _binding(context)
        state = _storage_state(binding.path)
        if state is Availability.UNAVAILABLE:
            try:
                installed_home = _adapter_home().is_dir()
            except OSError:
                installed_home = False
            if binding.notice and installed_home:
                record_warning(binding.notice)
            return []
        if state is Availability.UNKNOWN:
            if binding.notice:
                record_warning(binding.notice)
            record_warning(f"OpenCode database could not be inspected safely: {binding.path}")
            return []
        if binding.notice:
            record_warning(binding.notice)
        before = _store_stamp(binding.path) if cache_enabled else None
        connection = _connect_existing(binding.path)
        connection.execute("BEGIN")
        columns = _schema_columns(connection)
        pinned = _store_stamp(binding.path) if cache_enabled else None
        if before is not None and before == pinned:
            cache_key = (str(binding.path), context.pattern_signature, before)
            cached = _cached_discovery(cache_key, context)
            if cached is not None:
                return cached
        agent_column = "agent" if "agent" in columns["session"] else "''"
        states: dict[bytes, _DiscoveryState] = {}
        revert_column = "revert" if "revert" in columns["session"] else "NULL"
        for row in connection.execute(
            f"SELECT CAST(id AS BLOB), CAST(parent_id AS BLOB), directory, title, "
            f"{agent_column}, time_created, "
            f"time_updated, time_archived, {revert_column} FROM session "
            "ORDER BY time_updated DESC, id DESC"
        ):
            identity = _native_identifier(row[0])
            if identity is None:
                malformed = True
                continue
            raw_session_id, session_id = identity
            parent = _native_identifier(row[1]) if row[1] not in (None, b"", "") else None
            if row[1] not in (None, b"", "") and parent is None:
                malformed = True
            if raw_session_id in states:
                malformed = True
                continue
            revert_message_id = ""
            revert_part_id = ""
            raw_revert = row[8]
            if raw_revert not in (None, b"", ""):
                try:
                    boundary = normalize_revert(_strict_json_object(raw_revert, "session.revert"))
                except BridgeError:
                    malformed = True
                    boundary = None
                if boundary is not None:
                    revert_message_id = boundary["messageID"]
                    revert_part_id = str(boundary.get("partID") or "")
            states[raw_session_id] = _DiscoveryState(
                session_id=session_id,
                parent_id=parent[1] if parent is not None else "",
                directory=str(row[2] or ""),
                native_title=str(row[3] or ""),
                agent=str(row[4] or ""),
                created=timestamp(row[5]),
                updated=timestamp(row[6]),
                archived=row[7] is not None,
                evidence=context.accumulator(max_bytes=OPENCODE_EVIDENCE_BYTES),
                revert_message_id=revert_message_id,
                revert_part_id=revert_part_id,
            )
        part_rows = iter(
            connection.execute(
                "SELECT rowid, CAST(session_id AS BLOB), CAST(message_id AS BLOB), "
                "time_created, CAST(id AS BLOB), typeof(data) "
                # MessageV2.parts() orders native parts by their sortable ID.
                "FROM part ORDER BY session_id, message_id, id"
            )
        )
        part_row = next(part_rows, None)

        def part_key(row: tuple[Any, ...]) -> tuple[bytes, bytes]:
            session_key = row[1] if isinstance(row[1], bytes) else b""
            message_key = row[2] if isinstance(row[2], bytes) else b""
            return session_key, message_key

        def read_part(
            row: tuple[Any, ...],
            state: _DiscoveryState | None,
            *,
            semantic: bool = False,
        ) -> dict[str, Any] | None:
            nonlocal malformed, oversized
            if any(_native_identifier(row[index]) is None for index in (1, 2, 4)):
                malformed = True
            raw, row_oversized = _read_data(
                connection,
                "part",
                int(row[0]),
                str(row[5] or ""),
                None,
            )
            part = _json_object(raw)
            if part is None and not row_oversized:
                malformed = True
            if semantic and state is not None:
                part_text = (
                    part.get("text") if part is not None and part.get("type") == "text" else None
                )
                if isinstance(part_text, str):
                    state.evidence.scan(part_text.encode("utf-8", "replace"))
                elif row_oversized:
                    # The bounded prefix cannot be parsed, so retain conservative
                    # evidence from it and mark the negative conclusion incomplete.
                    state.evidence.scan(raw)
                if row_oversized:
                    state.evidence.truncated = True
            oversized = oversized or row_oversized
            return part

        for row in connection.execute(
            "SELECT rowid, CAST(session_id AS BLOB), CAST(id AS BLOB), "
            "time_created, typeof(data) "
            "FROM message ORDER BY session_id, id"
        ):
            rowid, session_id_raw, message_id_raw, created_raw, storage_type = row
            raw_session_id = session_id_raw if isinstance(session_id_raw, bytes) else b""
            raw_message_id = message_id_raw if isinstance(message_id_raw, bytes) else b""
            message_identity = _native_identifier(message_id_raw)
            if message_identity is None:
                malformed = True
            message_id = message_identity[1] if message_identity is not None else ""
            key = (raw_session_id, raw_message_id)
            while part_row is not None and part_key(part_row) < key:
                orphan_state = states.get(part_key(part_row)[0])
                read_part(part_row, orphan_state)
                malformed = True
                part_row = next(part_rows, None)
            current_state = states.get(raw_session_id)
            if current_state is None:
                malformed = True
            message_raw, message_oversized = _read_data(
                connection,
                "message",
                int(rowid),
                str(storage_type or ""),
                None,
            )
            message = _json_object(message_raw)
            if message is None and not message_oversized:
                malformed = True
            oversized = oversized or message_oversized
            role = str(message.get("role") or "") if message is not None else ""
            boundary_here = (
                current_state is not None
                and current_state.revert_message_id == message_id
                and not current_state.revert_seen
            )
            active_message = current_state is not None and not current_state.revert_seen
            if boundary_here and not current_state.revert_part_id:
                active_message = False
            current_parts: list[str] = []
            current_preview_chars = 0
            part_boundary_hit = False
            usable_part = False
            compaction_part = False
            while part_row is not None and part_key(part_row) == key:
                part = read_part(
                    part_row,
                    current_state,
                    semantic=active_message and role in ("user", "assistant"),
                )
                part_identity = _native_identifier(part_row[4])
                part_id = part_identity[1] if part_identity is not None else ""
                part_active = active_message and not part_boundary_hit
                if boundary_here and current_state.revert_part_id == part_id:
                    part_boundary_hit = True
                    part_active = False
                kind = part.get("type") if isinstance(part, dict) else None
                if part_active and kind == "compaction":
                    compaction_part = True
                if part_active and kind == "text":
                    part_text = part.get("text")
                    ignored = part.get("ignored") is True
                    if isinstance(part_text, str) and part_text and not ignored:
                        usable_part = True
                        current_preview_chars = _append_preview_part(
                            current_parts, part_text, current_preview_chars
                        )
                elif part_active and kind in ("file", "subtask", "agent"):
                    usable_part = True
                part_row = next(part_rows, None)
            if boundary_here and current_state is not None:
                current_state.revert_seen = True
            if active_message and current_state is not None and role in ("user", "assistant"):
                message_info = message if isinstance(message, dict) else {}
                summary = (
                    role == "assistant"
                    and message_info.get("summary") is True
                    and isinstance(message_info.get("finish"), str)
                    and bool(message_info["finish"])
                    and message_info.get("error") is None
                )
                if compaction_part and message_id:
                    if message_id in current_state.completed_compaction_parents:
                        current_state.compaction_count += 1
                        current_state.completed_compaction_parents.remove(message_id)
                    else:
                        current_state.compaction_ids.add(message_id)
                if summary:
                    parent_id = str(message_info.get("parentID") or "")
                    if parent_id in current_state.compaction_ids:
                        current_state.compaction_ids.remove(parent_id)
                        current_state.compaction_count += 1
                    elif parent_id:
                        current_state.completed_compaction_parents.add(parent_id)
                if usable_part and not compaction_part:
                    current_state.turn_count += 1
                    if role == "user":
                        current_state.prompt_count += 1
                        current_state.user_messages = current_state.prompt_count
            if role == "user" and active_message and usable_part and not compaction_part:
                _finish_user(
                    current_state,
                    current_parts,
                    (timestamp(created_raw), message_id),
                )
        while part_row is not None:
            orphan_state = states.get(part_key(part_row)[0])
            read_part(part_row, orphan_state)
            malformed = True
            part_row = next(part_rows, None)
        cache_stable = cache_key is not None and cache_key[2] == _store_stamp(binding.path)
    except (BridgeError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        record_warning(f"could not discover OpenCode sessions: {error}")
        return []
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()
    if malformed:
        record_warning("OpenCode discovery skipped malformed session data")
    if oversized:
        record_warning("OpenCode discovery omitted details from oversized session data")
    result: list[NativeSession] = []
    for item in states.values():
        context.publish("opencode", item.session_id, item.evidence)
        named = bool(item.native_title and not _DEFAULT_TITLE.fullmatch(item.native_title))
        title = (
            item.native_title
            if named
            else item.first_user or item.native_title or "untitled OpenCode session"
        )
        native = NativeSession(
            tool="opencode",
            session_id=item.session_id,
            title=title,
            cwd=item.directory,
            updated=item.updated,
            created=item.created,
            preview=item.latest_user,
            named=named,
            storage=str(binding.path),
            source=SourceKind.SUBAGENT if item.parent_id else SourceKind.INTERACTIVE,
            archived=item.archived,
            parent_id=item.parent_id,
            message_count=item.prompt_count,
            turn_count=item.turn_count,
            compaction_count=item.compaction_count,
            prompt_count=item.prompt_count,
            agent_nickname=item.agent,
        )
        result.append(native)
        context.index_native("opencode", native.session_id, native.storage)
    context.complete_native_index("opencode")
    if cache_key is not None and cache_stable:
        _remember_discovery(cache_key, result, context, malformed, oversized)
    return result


def _optional_strict_json_object(value: Any, label: str) -> dict[str, Any] | None:
    if value is None or (isinstance(value, (bytes, str)) and not value.strip()):
        return None
    return _strict_json_object(value, label)


@contextlib.contextmanager
def _semantic_snapshot(
    ref: NativeRef,
) -> Iterator[tuple[sqlite3.Connection, bytes, dict[str, Any] | None]]:
    """Pin one WAL snapshot and expose its exact session-local semantic state."""
    path = Path(ref.storage)
    connection: sqlite3.Connection | None = None
    try:
        expected_session = ref.session_id.encode("utf-8", "strict")
        connection = _connect_existing(path, timeout_seconds=SEMANTIC_BUSY_TIMEOUT_SECONDS)
        connection.execute("BEGIN")
        _schema_columns(connection, semantic=True)
        session_row = connection.execute(
            "SELECT CAST(id AS BLOB), CAST(revert AS BLOB) FROM session WHERE id=?",
            (ref.session_id,),
        ).fetchone()
        # The session SELECT above pins the deferred read transaction before any
        # digest or projection scan can observe a concurrent WAL commit.
        if session_row is None:
            raise BridgeError(
                f"OpenCode session {ref.session_id} is not present in the referenced database"
            )
        stored_session = _native_identifier(session_row[0])
        if stored_session is None or stored_session[0] != expected_session:
            raise BridgeError("OpenCode session identity is malformed in the referenced database")
        inconsistent = connection.execute(
            "SELECT part_id, missing FROM ("
            "SELECT CAST(p.id AS BLOB) AS part_id, m.id IS NULL AS missing "
            "FROM part AS p LEFT JOIN message AS m ON m.id=p.message_id "
            "WHERE p.session_id=? AND (m.id IS NULL OR m.session_id IS NOT p.session_id) "
            "UNION ALL "
            "SELECT CAST(p.id AS BLOB) AS part_id, 0 AS missing "
            "FROM message AS m JOIN part AS p ON p.message_id=m.id "
            "WHERE m.session_id=? AND p.session_id IS NOT m.session_id"
            ") LIMIT 1",
            (ref.session_id, ref.session_id),
        ).fetchone()
        if inconsistent is not None:
            identity = _native_identifier(inconsistent[0])
            label = identity[1] if identity is not None else "<malformed>"
            if inconsistent[1]:
                raise BridgeError(f"OpenCode part {label} refers to a missing message")
            raise BridgeError(
                f"OpenCode part {label} belongs to a different session than its message"
            )
        revert = _optional_strict_json_object(session_row[1], f"session {ref.session_id} revert")
        yield connection, expected_session, revert
    except BridgeError:
        raise
    except (
        OSError,
        RecursionError,
        RuntimeError,
        UnicodeError,
        ValueError,
        sqlite3.Error,
    ) as error:
        raise BridgeError(f"could not read OpenCode semantic state from {path}: {error}") from error
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()


def _message_parts(
    connection: sqlite3.Connection,
    ref: NativeRef,
    expected_session: bytes,
    message_id: str,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    part_ids: set[bytes] = set()
    for row in connection.execute(
        "SELECT CAST(id AS BLOB), CAST(message_id AS BLOB), CAST(session_id AS BLOB), "
        "CAST(data AS BLOB) FROM part WHERE session_id=? AND message_id=? ORDER BY id ASC",
        (ref.session_id, message_id),
    ):
        part_identity = _native_identifier(row[0])
        stored_message = _native_identifier(row[1])
        stored_session = _native_identifier(row[2])
        if part_identity is None or stored_message is None or stored_session is None:
            raise BridgeError("OpenCode part identity is malformed")
        raw_part_id, part_id = part_identity
        if stored_session[0] != expected_session or stored_message[1] != message_id:
            raise BridgeError(f"OpenCode part {part_id} belongs to a different message or session")
        if raw_part_id in part_ids:
            raise BridgeError(f"OpenCode part identity {part_id} is duplicated")
        part_ids.add(raw_part_id)
        part = _strict_json_object(row[3], f"part {part_id}")
        result.append(
            {
                **part,
                "id": part_id,
                "messageID": message_id,
                "sessionID": ref.session_id,
            }
        )
    return tuple(result)


def _iter_semantic_messages(
    connection: sqlite3.Connection,
    ref: NativeRef,
    expected_session: bytes,
    *,
    newest_first: bool = False,
    boundary: tuple[int, str, bool, str] | None = None,
) -> Iterator[StoredMessage]:
    clauses = ["session_id=?"]
    parameters: list[Any] = [ref.session_id]
    if boundary is not None:
        created, message_id, inclusive, _ = boundary
        operator = "<=" if inclusive else "<"
        clauses.append(f"(time_created < ? OR (time_created = ? AND id {operator} ?))")
        parameters.extend((created, created, message_id))
    direction = "DESC" if newest_first else "ASC"
    # OpenCode 1.18.21 MessageV2.stream() orders by time_created, then sortable id;
    # tests/fixtures/opencode-1.18.21-semantics.json pins the audited source.
    query = (
        "SELECT CAST(id AS BLOB), CAST(session_id AS BLOB), time_created, "
        "CAST(data AS BLOB) FROM message WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY time_created {direction}, id {direction}"
    )
    seen: set[bytes] = set()
    for row in connection.execute(query, parameters):
        message_identity = _native_identifier(row[0])
        stored_session = _native_identifier(row[1])
        if message_identity is None or stored_session is None:
            raise BridgeError("OpenCode message identity is malformed")
        raw_message_id, message_id = message_identity
        if stored_session[0] != expected_session:
            raise BridgeError(f"OpenCode message {message_id} belongs to a different session")
        if raw_message_id in seen:
            raise BridgeError(f"OpenCode message identity {message_id} is duplicated")
        seen.add(raw_message_id)
        created = row[2]
        if isinstance(created, bool) or not isinstance(created, int) or created < 0:
            raise BridgeError(f"OpenCode message {message_id} has an invalid creation time")
        info = _strict_json_object(row[3], f"message {message_id}")
        info = {**info, "id": message_id, "sessionID": ref.session_id}
        parts = _message_parts(connection, ref, expected_session, message_id)
        if boundary is not None and boundary[2] and message_id == boundary[1] and boundary[3]:
            part_index = next(
                (index for index, part in enumerate(parts) if part["id"] == boundary[3]), -1
            )
            # SessionRevert.cleanup retains the whole target when its part vanished.
            if part_index >= 0:
                parts = parts[:part_index]
        yield StoredMessage(message_id, created, info, parts)


def _staged_boundary(
    connection: sqlite3.Connection,
    ref: NativeRef,
    revert: dict[str, Any] | None,
) -> tuple[int, str, bool, str] | None:
    if revert is None:
        return None
    # The caller passes normalize_revert() output so this SQL projector has one
    # validation authority rather than a second, drifting schema implementation.
    message_id = revert["messageID"]
    part_id = revert.get("partID")
    row = connection.execute(
        "SELECT time_created, CAST(id AS BLOB) FROM message WHERE session_id=? AND id=?",
        (ref.session_id, message_id),
    ).fetchone()
    if row is None:
        return None
    identity = _native_identifier(row[1])
    if identity is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise BridgeError("OpenCode session revert message boundary is malformed")
    return row[0], identity[1], part_id is not None, part_id or ""


def _read_opencode_snapshot_inner(ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
    with _semantic_snapshot(ref) as (connection, expected_session, revert):
        normalized_revert = normalize_revert(revert)
        checkpoint = semantic_checkpoint(
            _iter_semantic_messages(connection, ref, expected_session), normalized_revert
        )
        boundary = _staged_boundary(connection, ref, normalized_revert)
        if latest_window:
            selection = select_compacted_newest(
                _iter_semantic_messages(
                    connection,
                    ref,
                    expected_session,
                    newest_first=True,
                    boundary=boundary,
                )
            )
            if selection.unresolved_tail:
                record_warning(
                    "OpenCode compaction retained-tail boundary is missing; native full-history "
                    "fallback was preserved"
                )
            viewed = selection.messages
            summary_ids = frozenset({selection.summary_id}) if selection.summary_id else frozenset()
        else:
            viewed = list(
                _iter_semantic_messages(connection, ref, expected_session, boundary=boundary)
            )
            summary_ids = None
        transcript = project_view(
            viewed,
            resumes_at_last_summary=True,
            summary_ids=summary_ids,
        )
        return ReadSnapshot(transcript, checkpoint)


def _read_opencode_snapshot(ref: NativeRef, *, latest_window: bool = True) -> ReadSnapshot:
    try:
        return _read_opencode_snapshot_inner(ref, latest_window=latest_window)
    except BridgeError as error:
        if ".type has unknown value" in str(error):
            record_warning(
                f"OpenCode session {ref.session_id} contains an unknown semantic part kind; "
                "the session was not bridged until that native kind can be classified safely"
            )
        raise


def _opencode_checkpoint(ref: NativeRef) -> str:
    with _semantic_snapshot(ref) as (connection, expected_session, revert):
        return semantic_checkpoint(
            _iter_semantic_messages(connection, ref, expected_session), revert
        )


class _UnstableSemanticSnapshot(RuntimeError):
    """Prevent a racing or failed SQLite classification from entering the cache."""


@functools.lru_cache(maxsize=128)
def _cached_opencode_change_status(
    registry_generation: int,
    ref: NativeRef,
    checkpoint: str,
    stamp: tuple[Any, ...],
) -> str:
    del registry_generation
    try:
        current = _opencode_checkpoint(ref)
    except (BridgeError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        raise _UnstableSemanticSnapshot from error
    if _store_stamp(Path(ref.storage)) != stamp:
        raise _UnstableSemanticSnapshot
    return "unchanged" if current == checkpoint else "changed"


def _opencode_change_status(ref: NativeRef, checkpoint: int | str) -> str:
    if not isinstance(checkpoint, str) or _SEMANTIC_CHECKPOINT.fullmatch(checkpoint) is None:
        record_warning(
            "OpenCode session checkpoint has an unknown or legacy format; expected "
            f"{CHECKPOINT_SCHEME.removesuffix(':')}"
        )
        return "unknown"
    stamp = _store_stamp(Path(ref.storage))
    if stamp is None:
        try:
            current = _opencode_checkpoint(ref)
        except (BridgeError, OSError, RuntimeError, ValueError, sqlite3.Error):
            return "unstable"
        return "unchanged" if current == checkpoint else "changed"
    try:
        return _cached_opencode_change_status(REGISTRY.generation, ref, checkpoint, stamp)
    except _UnstableSemanticSnapshot:
        return "unstable"


class _PersistedImportMismatch(BridgeError):
    """A proven imported row differs from the payload and must never be recorded."""


class _ImportNotVisible(BridgeError):
    """The authoritative database is readable but contains no import candidate."""


def _target_root(cwd: str) -> str:
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(Path(current.anchor)) if current.anchor else str(current)


def _chunked(values: tuple[str, ...], size: int = 400) -> Iterator[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _preflight_export(path: Path, generated: GeneratedExport) -> None:
    state = _storage_state(path)
    if state is Availability.UNAVAILABLE:
        return
    if state is not Availability.AVAILABLE:
        raise BridgeError(f"OpenCode target database cannot be inspected before import: {path}")
    connection = _connect_existing(path, timeout_seconds=SEMANTIC_BUSY_TIMEOUT_SECONDS)
    try:
        _schema_columns(connection, semantic=True)
        groups = (
            ("session", (generated.session_id,)),
            ("message", generated.message_ids),
            ("part", generated.part_ids),
        )
        for table, identifiers in groups:
            for chunk in _chunked(identifiers):
                placeholders = ",".join("?" for _ in chunk)
                row = connection.execute(
                    f"SELECT CAST(id AS BLOB) FROM {table} WHERE id IN ({placeholders}) LIMIT 1",
                    chunk,
                ).fetchone()
                if row is not None:
                    identity = _native_identifier(row[0])
                    label = identity[1] if identity is not None else "<malformed>"
                    raise BridgeError(
                        f"OpenCode import ID conflict: {table} identifier {label} already exists"
                    )
    except BridgeError:
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise BridgeError(f"could not preflight OpenCode import IDs in {path}: {error}") from error
    finally:
        connection.close()


def _candidate_import_session(
    connection: sqlite3.Connection,
    path: Path,
    generated: GeneratedExport,
    reported_id: str,
) -> str:
    direct = tuple(dict.fromkeys(value for value in (generated.session_id, reported_id) if value))
    for candidate in direct:
        row = connection.execute(
            "SELECT CAST(id AS BLOB) FROM session WHERE id=?", (candidate,)
        ).fetchone()
        if row is not None:
            identity = _native_identifier(row[0])
            if identity is None:
                raise BridgeError(f"OpenCode imported session identity is malformed in {path}")
            return identity[1]
    columns = _schema_columns(connection, semantic=True)["session"]
    if "metadata" not in columns:
        return ""
    rows = connection.execute(
        "SELECT CAST(id AS BLOB), CAST(metadata AS BLOB) FROM session "
        "WHERE time_created BETWEEN ? AND ? ORDER BY time_created, id LIMIT 1001",
        (generated.created_ms - 60_000, generated.created_ms + 60_000),
    )
    matches: list[str] = []
    for index, (raw_id, raw_metadata) in enumerate(rows):
        if index >= 1_000:
            raise BridgeError(
                "OpenCode import provenance window contains too many sessions to identify safely"
            )
        identity = _native_identifier(raw_id)
        if identity is None:
            raise BridgeError(f"OpenCode imported session identity is malformed in {path}")
        metadata = _optional_strict_json_object(raw_metadata, f"session {identity[1]} metadata")
        if metadata and metadata.get("ai_sessions_import_nonce") == generated.nonce:
            matches.append(identity[1])
    if len(matches) > 1:
        raise BridgeError("OpenCode import provenance marker matched multiple persisted sessions")
    return matches[0] if matches else ""


def _strict_identifier_rows(rows: Iterator[tuple[Any, ...]], label: str) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = []
    for row in rows:
        decoded: list[str] = []
        for value in row:
            identity = _native_identifier(value)
            if identity is None:
                raise BridgeError(f"OpenCode persisted {label} identity is malformed")
            decoded.append(identity[1])
        result.append(tuple(decoded))
    return result


def _verify_import(
    path: Path,
    generated: GeneratedExport,
    reported_id: str,
) -> NativeWrite:
    connection = _connect_existing(path, timeout_seconds=SEMANTIC_BUSY_TIMEOUT_SECONDS)
    try:
        _schema_columns(connection, semantic=True)
        actual_id = _candidate_import_session(connection, path, generated, reported_id)
        if not actual_id:
            raise _ImportNotVisible("OpenCode import row is not visible yet")
        message_rows = _strict_identifier_rows(
            iter(
                connection.execute(
                    "SELECT CAST(id AS BLOB) FROM message WHERE session_id=? "
                    "ORDER BY time_created, id",
                    (actual_id,),
                )
            ),
            "message",
        )
        if tuple(row[0] for row in message_rows) != generated.message_ids:
            raise _PersistedImportMismatch(
                f"OpenCode imported session {actual_id} has different message IDs/content"
            )
        part_rows = _strict_identifier_rows(
            iter(
                connection.execute(
                    "SELECT CAST(message_id AS BLOB), CAST(id AS BLOB) FROM part "
                    "WHERE session_id=? ORDER BY message_id, id",
                    (actual_id,),
                )
            ),
            "part",
        )
        expected_parts = tuple(sorted(zip(generated.message_ids, generated.part_ids, strict=True)))
        if tuple(part_rows) != expected_parts:
            raise _PersistedImportMismatch(
                f"OpenCode imported session {actual_id} has different part IDs/content"
            )
    except (_PersistedImportMismatch, BridgeError):
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise BridgeError(f"could not verify OpenCode import in {path}: {error}") from error
    finally:
        connection.close()
    ref = NativeRef(actual_id, str(path))
    checkpoint = _opencode_checkpoint(ref)
    if checkpoint != generated.expected_checkpoint:
        raise _PersistedImportMismatch(
            f"OpenCode imported session {actual_id} does not match the submitted semantic content"
        )
    notices = ()
    if actual_id != generated.session_id:
        notices = (
            f"OpenCode reminted imported session {generated.session_id} as {actual_id}; "
            "the verified persisted ID is authoritative",
        )
    return NativeWrite(ref, checkpoint, notices)


def _write_temporary_export(payload: dict[str, Any]) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="ai-sessions-opencode-", suffix=".json")
    path = Path(name)
    try:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def _run_import_and_cleanup(
    command: tuple[str, ...], operation_cwd: str, temporary: Path
) -> MaintenanceResult:
    result: MaintenanceResult | None = None
    failure: Exception | None = None
    try:
        result = run_maintenance(
            command,
            ("import", str(temporary)),
            cwd=operation_cwd,
            timeout=IMPORT_TIMEOUT_SECONDS,
            return_timeout=True,
        )
    except Exception as error:
        failure = error
    cleanup_error: OSError | None = None
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        cleanup_error = error
    if failure is not None:
        if cleanup_error is not None:
            raise BridgeError(
                f"{failure}; the temporary OpenCode import file also could not be removed: "
                f"{cleanup_error}"
            ) from failure
        raise failure
    if cleanup_error is not None:
        raise BridgeError(
            f"could not remove the temporary OpenCode import file: {cleanup_error}"
        ) from cleanup_error
    assert result is not None
    return result


def _verify_import_with_backoff(
    path: Path, generated: GeneratedExport, reported_id: str
) -> tuple[NativeWrite | None, BridgeError | None]:
    last_error: BridgeError | None = None
    mismatch: _PersistedImportMismatch | None = None
    for delay in IMPORT_VERIFY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return _verify_import(path, generated, reported_id), None
        except _PersistedImportMismatch as error:
            mismatch = error
            last_error = error
        except BridgeError as error:
            last_error = error
    if mismatch is not None:
        raise mismatch
    return None, last_error


def _write_opencode(
    *,
    cwd: str,
    turns: list[Turn],
    prepared: PreparedTarget,
    title: str = "",
    created: float | None = None,
) -> NativeWrite:
    operation_cwd = str(Path(cwd).resolve())
    if not Path(operation_cwd).is_dir():
        raise BridgeError(f"OpenCode target working directory does not exist: {operation_cwd}")
    database_option = prepared.option("database")
    if not database_option:
        raise BridgeError("OpenCode target preparation did not resolve an import database")
    database = Path(database_option)
    if (
        not prepared.option("model")
        or not prepared.option("provider_id")
        or not prepared.option("model_id")
    ):
        raise BridgeError("OpenCode target preparation did not select an import model")
    current_database = _command_database_path(prepared.command, operation_cwd)
    if current_database != database:
        raise BridgeError(
            "OpenCode authoritative database changed after target preparation; retry the bridge"
        )
    created_value = time.time() if created is None else created
    if isinstance(created_value, bool) or not isinstance(created_value, (int, float)):
        raise BridgeError("OpenCode import creation time must be numeric")
    if not (created_value >= 0 and created_value < float("inf")):
        raise BridgeError("OpenCode import creation time must be finite and non-negative")
    generated = build_export(
        cwd=operation_cwd,
        root=_target_root(operation_cwd),
        turns=turns,
        title=title,
        provider_id=prepared.option("provider_id"),
        model_id=prepared.option("model_id"),
        nonce=secrets.token_hex(16),
        created_ms=int(created_value * 1_000),
    )
    _preflight_export(database, generated)
    temporary = _write_temporary_export(generated.payload)
    result = _run_import_and_cleanup(prepared.command, operation_cwd, temporary)
    matches = tuple(dict.fromkeys(_IMPORTED_SESSION.findall(result.stdout)))
    reported_id = matches[0] if len(matches) == 1 else ""
    try:
        final_database = _command_database_path(prepared.command, operation_cwd)
    except BridgeError as authority_error:
        written, verification_error = _verify_import_with_backoff(database, generated, reported_id)
        if written is not None:
            return NativeWrite(
                written.native,
                None,
                (
                    "OpenCode import content was found in the previously authoritative database, "
                    "but authority could not be re-resolved; the new member remains unstable "
                    f"({authority_error})",
                ),
            )
        raise BridgeError(
            "OpenCode import completed without a verifiable authoritative database "
            f"({verification_error})"
        ) from authority_error
    written, last_error = _verify_import_with_backoff(final_database, generated, reported_id)
    if written is not None:
        notices = list(written.notices)
        if result.timed_out:
            notices.append("OpenCode import timed out after persisting and verifying the session")
        elif result.returncode != 0:
            notices.append(
                "OpenCode returned a nonzero import status after persisting and verifying the session"
            )
        if result.truncated:
            notices.append("OpenCode import output was truncated after the session was verified")
        if not reported_id or reported_id != written.native.session_id:
            notices.append(
                "OpenCode import output did not name the verified session ID; persisted content "
                "was used as authority"
            )
        return NativeWrite(written.native, written.checkpoint, tuple(notices))
    if result.timed_out or result.returncode != 0:
        raise BridgeError(f"OpenCode import failed: {_maintenance_detail(result)}") from last_error
    if isinstance(last_error, _ImportNotVisible):
        raise BridgeError(
            "OpenCode import returned success but no persisted session row became visible"
        ) from last_error
    if reported_id:
        return NativeWrite(
            NativeRef(reported_id, str(final_database)),
            None,
            (
                "OpenCode reported a successful import, but persisted content could not be "
                f"verified; this member remains unstable ({last_error})",
            ),
        )
    raise BridgeError(
        "OpenCode import returned success but no persisted session or recoverable ID could be verified"
    ) from last_error


def _availability(ref: NativeRef) -> Availability:
    path = Path(ref.storage)
    state = _storage_state(path)
    if state is not Availability.AVAILABLE:
        return state
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_existing(path)
        _schema_columns(connection)
        row = connection.execute("SELECT 1 FROM session WHERE id=?", (ref.session_id,)).fetchone()
        return Availability.AVAILABLE if row is not None else Availability.UNAVAILABLE
    except (BridgeError, OSError, ValueError, sqlite3.Error) as error:
        record_warning(
            f"OpenCode exact session {ref.session_id} in {path} could not be verified: {error}"
        )
        return Availability.UNKNOWN
    finally:
        if connection is not None:
            connection.close()


def _resolve(session_id: str) -> NativeRef | None:
    with _BINDING_LOCK:
        binding = _LAST_BINDING
    if binding is None:
        return None
    ref = NativeRef(session_id, str(binding.path))
    return ref if _availability(ref) is Availability.AVAILABLE else None


def publish_name(session: Any, name: str) -> str:
    """Publish one exact title through a bounded existing-database transaction."""
    ref = NativeRef(str(session.session_id), str(session.storage))
    path = Path(ref.storage)
    if _storage_state(path) is not Availability.AVAILABLE:
        raise BridgeError(f"OpenCode title database is not available: {path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _sqlite_readwrite_uri(path),
            uri=True,
            timeout=SEMANTIC_BUSY_TIMEOUT_SECONDS,
        )
        connection.execute(f"PRAGMA busy_timeout={round(SEMANTIC_BUSY_TIMEOUT_SECONDS * 1_000)}")
        _schema_columns(connection)
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT title FROM session WHERE id=?", (ref.session_id,)
        ).fetchone()
        if before is None:
            raise BridgeError(f"OpenCode session {ref.session_id} is not present in {path}")
        cursor = connection.execute(
            "UPDATE session SET title=? WHERE id=?", (str(name), ref.session_id)
        )
        if cursor.rowcount != 1:
            raise BridgeError(f"OpenCode title update affected {cursor.rowcount} rows")
        after = connection.execute(
            "SELECT title FROM session WHERE id=?", (ref.session_id,)
        ).fetchone()
        if after is None or after[0] != str(name):
            raise BridgeError("OpenCode title update could not be verified")
        connection.commit()
        visible = connection.execute(
            "SELECT title FROM session WHERE id=?", (ref.session_id,)
        ).fetchone()
    except BridgeError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        if connection is not None:
            connection.rollback()
        raise BridgeError(f"could not update OpenCode title in {path}: {error}") from error
    finally:
        if connection is not None:
            connection.close()
    if visible is None or visible[0] != str(name):
        return "provider accepted the title, but another OpenCode writer replaced it immediately"
    return "provider title updated; a running OpenCode TUI may refresh or replace it later"


def _process_basename(value: str) -> str:
    return re.split(r"[\\/]", value.strip(" \t\"'"))[-1].casefold()


_OPENCODE_PROCESS_NAMES = frozenset(
    {
        "opencode",
        "opencode.exe",
        "opencode.cmd",
        "opencode.ps1",
        "opencode-ai",
        "opencode.js",
        "opencode.mjs",
    }
)


def _opencode_process_identity(process: Any, before_session: tuple[str, ...]) -> int:
    """Return 2 for a native process, 1 for a recognized direct wrapper, or 0."""
    if not before_session:
        return 0
    executable = _process_basename(before_session[0])
    if (
        _process_basename(process.name) in _OPENCODE_PROCESS_NAMES
        or executable in _OPENCODE_PROCESS_NAMES
    ):
        return 2
    wrapped_index = -1
    if executable in ("node", "node.exe", "bun", "bun.exe", "deno", "deno.exe"):
        wrapped_index = 1
    elif executable in ("sh", "bash", "zsh", "env", "env.exe"):
        wrapped_index = 1
    elif executable in ("cmd", "cmd.exe"):
        wrapped_index = next(
            (
                index + 1
                for index, value in enumerate(before_session[:-1])
                if value.casefold() in ("/c", "/k")
            ),
            -1,
        )
    elif executable in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        wrapped_index = next(
            (
                index + 1
                for index, value in enumerate(before_session[:-1])
                if value.casefold() == "-file"
            ),
            -1,
        )
    if 0 <= wrapped_index < len(before_session):
        return (
            1 if _process_basename(before_session[wrapped_index]) in _OPENCODE_PROCESS_NAMES else 0
        )
    return 0


def _opencode_session_argument(process: Any) -> tuple[str, int]:
    command = process.command
    matches: list[tuple[int, str]] = []
    malformed = False
    for index, argument in enumerate(command):
        if argument == "--":
            break
        if argument in ("--session", "-s"):
            if index + 1 >= len(command) or not command[index + 1]:
                malformed = True
            else:
                matches.append((index, command[index + 1]))
        elif argument.startswith("--session=") or argument.startswith("-s="):
            value = argument.split("=", 1)[1]
            if value:
                matches.append((index, value))
            else:
                malformed = True
        elif argument.startswith("-s") and not argument.startswith("--"):
            matches.append((index, argument[2:]))
    if malformed or len(matches) != 1:
        return "", 0
    index, session_id = matches[0]
    if re.fullmatch(r"ses_[0-9A-Za-z]{26}", session_id) is None:
        return "", 0
    identity = _opencode_process_identity(process, command[:index])
    return (session_id, identity) if identity else ("", 0)


def inspect_liveness(
    context: LivenessContext, home: Path, sessions: Iterable[LivenessSession]
) -> dict[str, int]:
    """Match only live OpenCode processes whose argv names one exact session."""
    del home
    eligible = {item.session_id for item in sessions}
    candidates: dict[str, list[tuple[int, int]]] = {}
    for process in context.process_snapshot:
        # A start token prevents an inaccessible/raced process snapshot from being
        # treated as exact evidence. Both Linux and Windows populate this token.
        if not process.start_token and process.started_at <= 0:
            continue
        session_id, rank = _opencode_session_argument(process)
        if session_id in eligible:
            candidates.setdefault(session_id, []).append((rank, process.pid))
    result: dict[str, int] = {}
    for session_id, matches in candidates.items():
        best_rank = max(rank for rank, _ in matches)
        best_pids = {pid for rank, pid in matches if rank == best_rank}
        if len(best_pids) == 1:
            result[session_id] = best_pids.pop()
    return result


def resume_args(
    *,
    session_id: str,
    source: SourceKind,
    resume_id: str,
    parent_id: str,
    native: bool,
) -> list[str]:
    del source, resume_id, parent_id, native
    return ["--session", session_id]


ADAPTER = HarnessAdapter(
    name="opencode",
    label="OpenCode",
    short_label="OpenCode",
    order=30,
    home=OPENCODE_HOME,
    default_command=("opencode",),
    dangerous_args=("--auto",),
    source_kinds=frozenset((SourceKind.INTERACTIVE, SourceKind.SUBAGENT)),
    id_patterns=(re.compile(rb"(?<![0-9A-Za-z])ses_[0-9A-Za-z]{26}(?![0-9A-Za-z])"),),
    read=_read_opencode_snapshot,
    write=_write_opencode,
    resolve=_resolve,
    availability=_availability,
    checkpoint=_opencode_checkpoint,
    change_status=_opencode_change_status,
    liveness_source_kinds=frozenset((SourceKind.INTERACTIVE, SourceKind.SUBAGENT)),
    liveness_executables=_OPENCODE_PROCESS_NAMES,
    discover=discover,
    resume_args=resume_args,
    publish_name=publish_name,
    inspect_liveness=inspect_liveness,
    prepare_target=_prepare_opencode_target,
    budget=BudgetPolicy(
        context_tokens=OPENCODE_CONTEXT_FLOOR_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source="OpenCode 128k unknown-model compatibility floor, researched 2026-08-23",
    ),
)
