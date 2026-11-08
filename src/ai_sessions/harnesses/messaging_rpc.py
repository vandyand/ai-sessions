"""Bounded JSON-RPC over the native daemon's WebSocket byte proxy.

The proxy is NOT a JSONL app-server. Its pipes carry an HTTP Upgrade followed by
WebSocket frames. This module owns only the proxy child it creates.
"""

import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import websocket


def codex_home(home: Path | None = None) -> Path:
    from .codex import messaging_home

    return messaging_home(home)


def native_executable() -> str:
    """Resolve the native binary without ever executing a Windows cmd shim."""
    if sys.platform == "win32":
        relative = Path(
            "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
            "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
        )
        roots = []
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "npm")
        shim = shutil.which("codex")
        if shim:
            roots.append(Path(shim).parent)
        for root in roots:
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
        candidate = shutil.which("codex.exe")
        if candidate and Path(candidate).suffix.lower() == ".exe":
            return candidate
    else:
        candidate = shutil.which("codex")
        if candidate:
            return candidate
    raise OSError("Native Codex executable unavailable")


class RPCError(Exception):
    """An explicit server rejection, distinct from ambiguous transport loss.

    Keep the wire message private for classification; never expose it in receipts.
    """

    def __init__(self, code: int, message: str):
        super().__init__("Codex request rejected")
        self.code = code
        self.message = message


class RPCUnavailable(OSError):
    pass


class ProxySocket:
    """Socket-like binary pipe adapter for websocket-client, with bounded reads."""

    def __init__(self, process):
        self.process = process
        self.timeout = 5.0
        self.deadline = None
        self._queue = queue.Queue(maxsize=16)
        self._buffer = bytearray()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        try:
            while not self._closed.is_set():
                data = os.read(self.process.stdout.fileno(), 65536)
                if not self._put(data) or not data:
                    return
        except (OSError, ValueError):
            self._put(b"")

    def _put(self, data):
        while not self._closed.is_set():
            try:
                self._queue.put(data, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def _remaining(self):
        value = self.timeout
        if self.deadline is not None:
            value = min(value, self.deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError("Codex proxy deadline expired")
        return value

    def recv(self, size):
        timeout = self._remaining()
        if size == 0:
            return b""
        if not self._buffer:
            try:
                data = self._queue.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError("Codex proxy receive timed out") from None
            if not data:
                return b""
            self._buffer.extend(data)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def send(self, data):
        # Windows anonymous-pipe writes have no socket timeout. Run each write
        # under a deadline; the context manager kills our child on failure,
        # releasing a blocked writer. Never retry a possibly partial write here.
        timeout = self._remaining()
        result = queue.Queue(maxsize=1)

        def write():
            try:
                result.put(self.process.stdin.write(data))
            except (OSError, ValueError):
                result.put(OSError("Codex proxy write failed"))

        threading.Thread(target=write, daemon=True).start()
        try:
            value = result.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Codex proxy write timed out") from None
        if isinstance(value, Exception):
            raise value
        if not value:
            raise OSError("Codex proxy closed")
        return value

    def close(self):
        self._closed.set()


class CodexRPC:
    """One synchronous attachment. Never answers server requests or approvals."""

    def __init__(self, home: Path | None = None, timeout: float = 5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite RPC timeout is required")
        self.home = codex_home(home)
        self.timeout = timeout
        self._process = None
        self._socket = None
        self._ws = None
        self._next_id = 0
        self._drain_stop = threading.Event()
        self._drainer = None

    def __enter__(self):
        try:
            from .codex import messaging_environment

            env = messaging_environment(self.home)
            path = self.home / "app-server-control/app-server-control.sock"
            self._process = subprocess.Popen(
                [native_executable(), "app-server", "proxy", "--sock", str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._socket = ProxySocket(self._process)
            self._socket.deadline = time.monotonic() + self.timeout
            self._ws = websocket.WebSocket(enable_multithread=True)
            self._ws.connect("ws://localhost/", socket=self._socket, timeout=self.timeout)
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "ai_sessions_messaging", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._socket.deadline = time.monotonic() + self.timeout
            self._ws.send(json.dumps({"method": "initialized"}))
            return self
        except Exception:
            self.close()
            raise RPCUnavailable("Codex connection unavailable") from None

    def call(self, method: str, params: dict) -> dict:
        if self._drainer is not None:
            raise RuntimeError("Codex attachment is draining notifications")
        if self._ws is None:
            raise RPCUnavailable("Codex connection unavailable")
        self._next_id += 1
        request_id = self._next_id
        deadline = time.monotonic() + self.timeout
        if self._socket is not None:
            self._socket.deadline = deadline

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("Codex request timed out")
            self._ws.settimeout(value)

        try:
            remaining()
            self._ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            while True:
                remaining()
                message = json.loads(self._ws.recv())
                # A server request may also carry an id. Never answer it, even
                # when its id happens to equal this client's request id.
                if "method" in message or message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise RPCError(error.get("code"), str(error.get("message", "")))
                result = message["result"]
                if not isinstance(result, dict):
                    raise ValueError("Invalid RPC result")
                return result
        except RPCError:
            raise
        except Exception:
            raise RPCUnavailable("Codex request outcome unavailable") from None

    def drain_notifications(self):
        """Keep an idle attachment flowing without answering any server requests.

        This transfers receive ownership to a bounded-lifetime background reader;
        further calls are forbidden. It retains no messages or approval payloads.
        """
        if self._drainer is not None:
            return
        if self._ws is None:
            raise RPCUnavailable("Codex connection unavailable")
        if self._socket is not None:
            self._socket.deadline = None
        self._ws.settimeout(0.5)
        ws = self._ws

        def drain():
            while not self._drain_stop.is_set():
                try:
                    if not ws.recv():
                        return
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    continue
                except Exception:
                    return

        self._drainer = threading.Thread(target=drain, daemon=True)
        self._drainer.start()

    def close(self):
        self._drain_stop.set()
        if self._socket is not None:
            self._socket.close()
        # No WebSocket close handshake: bounded child cleanup also covers a
        # blocked write and cannot touch the daemon or another session.
        if self._process is not None:
            process, self._process = self._process, None
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            except OSError:
                pass
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
        if self._drainer is not None:
            self._drainer.join(timeout=1)
        self._ws = None

    def __exit__(self, *args):
        self.close()
