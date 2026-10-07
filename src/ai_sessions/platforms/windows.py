"""Provider-neutral Windows process helpers used by terminal focus and take-over."""

from __future__ import annotations

import subprocess
from typing import Any

import psutil

# Processes that stand between a launcher and the harness it runs: the npm
# .cmd shims start cmd.exe, which may start node.exe, which starts the CLI.
WRAPPERS = frozenset({"cmd.exe", "node.exe"})

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def process_exists(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except (psutil.Error, ValueError):
        return False


def process_started(pid: int) -> float:
    """The process's creation time, or 0 when it is gone or unreadable."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, ValueError):
        return 0.0


def stop_process_tree(pid: int, started: float, timeout: float = 5.0) -> str:
    """Stop one verified process and everything it started.

    ``started`` must match the creation time the caller showed the user, so a
    PID recycled since then is refused rather than stopped.  Returns an empty
    string on success, otherwise why the process is still running.
    """
    try:
        root = psutil.Process(pid)
        if abs(root.create_time() - started) > 1:
            return f"PID {pid} is now a different process; nothing was stopped."
        tree = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        return ""
    except (psutil.Error, ValueError) as error:
        return f"Could not inspect PID {pid}: {error}"
    for process in tree:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as error:
            return f"Could not stop PID {process.pid}: {error}"
    _, alive = psutil.wait_procs(tree, timeout=timeout)
    if any(process.pid == pid for process in alive):
        return f"PID {pid} did not exit within {timeout:g} seconds."
    return ""


def is_detached(pid: int) -> bool:
    """Whether the program that launched ``pid`` has exited underneath it.

    A console harness whose launcher died keeps reading the same terminal
    as the shell that got its prompt back, so neither can be used.  Only the
    launcher's absence counts: a harness typed straight into a shell is
    attached as long as that shell runs.  psutil's ``parent`` already refuses
    a recycled parent PID by comparing creation times.
    """
    try:
        process = psutil.Process(pid)
        while True:
            parent = process.parent()
            if parent is None:
                return True
            if parent.name().casefold() not in WRAPPERS:
                return False
            process = parent
    except (psutil.Error, ValueError):
        return False


def console_pids() -> tuple[int, ...]:
    """Every process attached to this process's console, i.e. this terminal tab."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        size = 64
        while True:
            buffer = (ctypes.c_uint32 * size)()
            count = kernel32.GetConsoleProcessList(buffer, size)
            if count == 0:
                return ()
            if count <= size:
                return tuple(int(buffer[index]) for index in range(count))
            size = count + 16
    except (AttributeError, OSError, ValueError):
        return ()


class KillOnCloseJob:
    """Ties a harness's whole process tree to the lifetime of this launcher.

    The job's only handle belongs to this process, so if ``sessions`` dies
    for any reason Windows closes the handle and stops the harness with it,
    rather than leaving it reading a terminal whose shell has its prompt
    back.  When the harness exits normally, ``release`` clears the limit
    first, so anything it deliberately left running keeps running.
    """

    def __init__(self, handle: int, kernel32: Any, information: Any) -> None:
        self.handle = handle
        self.kernel32 = kernel32
        self.information = information

    @classmethod
    def start(cls, argv: list[str]) -> tuple[subprocess.Popen[bytes], "KillOnCloseJob | None"]:
        """Start ``argv`` suspended, place it in a job, then let it run.

        Starting suspended means the .cmd shim cannot launch the real CLI
        before the job exists.  Every failure still resumes the process: an
        uncontained harness is better than one that never starts.
        """
        process = subprocess.Popen(argv, creationflags=CREATE_SUSPENDED)
        job: KillOnCloseJob | None = None
        try:
            job = cls._assign(int(process._handle))  # type: ignore[attr-defined]
        finally:
            _resume(int(process._handle))  # type: ignore[attr-defined]
        return process, job

    @classmethod
    def _assign(cls, process_handle: int) -> "KillOnCloseJob | None":
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
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "ReadOperationCount",
                        "WriteOperationCount",
                        "OtherOperationCount",
                        "ReadTransferCount",
                        "WriteTransferCount",
                        "OtherTransferCount",
                    )
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
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            if not (configured and kernel32.AssignProcessToJobObject(handle, process_handle)):
                kernel32.CloseHandle(handle)
                return None
            return cls(int(handle), kernel32, information)
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def release(self) -> None:
        """Close the job without stopping what is still inside it."""
        if not self.handle:
            return
        try:
            import ctypes

            self.information.BasicLimitInformation.LimitFlags = 0
            self.kernel32.SetInformationJobObject(
                self.handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(self.information),
                ctypes.sizeof(self.information),
            )
        finally:
            self.kernel32.CloseHandle(self.handle)
            self.handle = 0


def _resume(process_handle: int) -> None:
    import ctypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
    ntdll.NtResumeProcess(process_handle)
