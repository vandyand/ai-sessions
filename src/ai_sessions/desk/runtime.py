"""Lazy, per-desk observer lifetime, independent of either interactive client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil

from .. import __version__
from ..fastjson import write_atomic as write


@contextmanager
def claim(path):
    """Nonblocking OS lock: process death releases ownership, never unlink it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        # Windows mandatory byte locks prohibit reading the claimed byte too.
        # Append mode opens at EOF, so size inspection never touches that byte.
        # Region locks work beyond EOF. Initializing a byte before locking can
        # write into another owner's mandatory Windows lock during first use.
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def serialized(path):
    deadline = time.monotonic() + 15
    while True:
        with claim(path) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Desk startup is busy; retry shortly")
        time.sleep(0.05)


def folder(store, name):
    record = store.get(name)
    key = hashlib.sha256(record["name"].casefold().encode()).hexdigest()
    return store.root.resolve() / ".observers" / key


def command_matches(command, row):
    required = [
        "-m",
        "ai_sessions.desk.runtime",
        "--root",
        row["root"],
        "--name",
        row["name"],
        "--generation",
        row["generation"],
    ]
    for index in range(len(command)):
        candidate = command[index : index + len(required)]
        if len(candidate) != len(required):
            continue
        # Windows process readers can preserve doubled separators. Only the path
        # is a filesystem identity; module, flags, name and generation stay exact.
        if Path(candidate[3]).resolve() != Path(row["root"]).resolve():
            continue
        candidate = list(candidate)
        candidate[3] = required[3]
        if candidate == required:
            return True
    return False


def status(store, name):
    location = folder(store, name)
    stopped = {"state": "stopped", "busy": False}
    try:
        for attempt in range(3):
            try:
                row = json.loads((location / "status.json").read_text(encoding="utf-8"))
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
        if (
            Path(row["root"]).resolve() != store.root.resolve()
            or row["name"] != store.get(name)["name"]
        ):
            return stopped
        process = psutil.Process(row["pid"])
        if process.create_time() != row["started"] or not process.is_running():
            return stopped
        if not command_matches(process.cmdline(), row):
            return stopped
        with claim(location / "worker.lock") as available:
            if available:
                return stopped
        if row["state"] == "stopped":
            return dict(row, state="stopping", busy=False)
        return row
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return stopped


def clean_environment():
    env = dict(os.environ)
    for key in (
        "AI_SESSIONS_SUPERVISOR",
        "AI_SESSIONS_MESSAGE_FROM",
        "AI_SESSIONS_WORKSPACE",
        "AI_SESSIONS_WORKSPACE_PANE",
        "CODEX_THREAD_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    ):
        env.pop(key, None)
    source = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH", "")]))
    return env


def ensure_observer(store, name, interval=60.0):
    name = store.get(name)["name"]
    location = folder(store, name)
    with serialized(location / "startup.lock"):
        current = status(store, name)
        if current["state"] != "stopped":
            if current.get("version") == __version__:
                return current
            # Cooperative, generation-scoped shutdown loads updated observer
            # code without restarting any native agent or shared daemon.
            current = _stop_current(store, name, current)
            if current["state"] != "stopped":
                raise RuntimeError("Previous Desk observer is still stopping; retry shortly")
        generation = uuid.uuid4().hex
        command = [
            sys.executable,
            "-m",
            __name__,
            "--root",
            str(store.root.resolve()),
            "--name",
            name,
            "--generation",
            generation,
            "--interval",
            str(interval),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=store.root.resolve(),
            env=clean_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = status(store, name)
            if current["state"] != "stopped":
                return current
            if process.poll() is not None:
                break
            time.sleep(0.05)
        write(location / "stop.json", {"generation": generation})
        raise RuntimeError(
            "Desk observer could not start; inspect ses desk status and observer.log"
        )


def _stop_current(store, name, current):
    location = folder(store, name)
    if current["state"] == "stopped":
        return current
    write(location / "stop.json", {"generation": current["generation"]})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if status(store, name)["state"] == "stopped":
            return {"state": "stopped", "busy": False}
        time.sleep(0.1)
    return dict(current, state="stopping")


def stop_observer(store, name):
    location = folder(store, name)
    with serialized(location / "startup.lock"):
        return _stop_current(store, name, status(store, name))


class Activity(dict):
    """Read detached state at the viewer's existing polling boundary."""

    def __init__(self, store, name):
        super().__init__(detached=True)
        self.store, self.name = store, name
        self.checked, self.row = 0.0, {}

    def get(self, key, default=None):
        if time.monotonic() >= self.checked:
            self.row = status(self.store, self.name)
            self.checked = time.monotonic() + 1
        row = self.row
        if key == "busy":
            return row.get("busy", False)
        if key == "error":
            return (
                "Observer stopped; reopen the live view to restart"
                if row["state"] == "stopped"
                else None
            )
        return super().get(key, default)


def serve(store, name, generation, interval):
    from .cli import watch

    location = folder(store, name)
    with claim(location / "worker.lock") as acquired:
        if not acquired:
            return
        row = dict(
            root=str(store.root.resolve()),
            name=store.get(name)["name"],
            pid=os.getpid(),
            started=psutil.Process().create_time(),
            generation=generation,
            state="running",
            busy=True,
            interval=interval,
            version=__version__,
        )

        ending = threading.Event()

        class Stop:
            def is_set(self):
                if ending.is_set():
                    return True
                try:
                    return (
                        json.loads((location / "stop.json").read_text())["generation"] == generation
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    return False

            def wait(self, seconds):
                deadline = time.monotonic() + seconds
                while not self.is_set() and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                return self.is_set()

        class PublishedActivity(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                row[key] = value
                write(location / "status.json", row)

        def diagnostic(message):
            # Fixed, bounded lifecycle messages only: no prompts, output or exception text.
            path = location / "observer.log"
            prior = path.read_text(encoding="utf-8")[-16000:] if path.exists() else ""
            path.write_text(prior + message + "\n", encoding="utf-8")

        write(location / "status.json", row)
        diagnostic("Observer started")
        from .discussion_worker import serve as serve_discussion

        shared_stop = Stop()
        feedback = threading.Thread(
            target=serve_discussion, args=(store, name, shared_stop), daemon=True
        )
        feedback.start()
        try:
            watch(store, name, interval, shared_stop, PublishedActivity())
        except Exception:
            diagnostic("Observer exited unexpectedly; reopen the live view to restart")
            raise
        finally:
            ending.set()
            feedback.join(timeout=5)
            row.update(state="stopped", busy=False)
            write(location / "status.json", row)
            diagnostic("Observer stopped")


def main():
    from .store import Store

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if not 5 <= args.interval <= 3600:
        parser.error("invalid interval")
    serve(Store(args.root), args.name, args.generation, args.interval)


if __name__ == "__main__":
    main()
