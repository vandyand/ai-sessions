"""Opt-in close/reopen probe using two already-created disposable native sessions.

Does not send prompts, drive terminal UI, or stop shared native servers. Requires
an isolated portable Terminal and explicit test session IDs. All metadata is kept
under --root. Close the owned Terminal window only via its exact executable path.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import psutil

from ai_sessions import app
from ai_sessions.workspaces import native
from ai_sessions.workspaces.store import WorkspaceStore


def live_identity(pid, birth):
    try:
        return abs(psutil.Process(pid).create_time() - birth) < 0.01
    except psutil.NoSuchProcess:
        return False


def close_owned(exe):
    records = [
        dict(pid=p.pid, birth=p.create_time())
        for p in psutil.process_iter(["exe"])
        if p.info["exe"] and Path(p.info["exe"]) == exe
    ]
    command = (
        "foreach ($record in ($env:PROBE_RECORDS | ConvertFrom-Json)) { "
        "$p = Get-Process -Id $record.pid -ErrorAction SilentlyContinue; "
        "if ($p -and $p.Path -eq $env:PROBE_TERMINAL -and "
        "[Math]::Abs(([DateTimeOffset]$p.StartTime).ToUnixTimeMilliseconds()/1000.0 "
        "- $record.birth) -lt 0.02) { $null = $p.CloseMainWindow() } }"
    )
    subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        env={**os.environ, "PROBE_TERMINAL": str(exe), "PROBE_RECORDS": json.dumps(records)},
        check=True,
        timeout=15,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--claude", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    exe = args.terminal.resolve(strict=True)
    if not (exe.parent / ".portable").is_file():
        raise ValueError("This probe requires an isolated portable Terminal")
    # Refuse to close any pre-existing windows from this executable.
    if any(p.info["exe"] and Path(p.info["exe"]) == exe for p in psutil.process_iter(["exe"])):
        raise ValueError("Close existing portable test windows before starting")
    state = root / "state.json"
    os.environ.update(
        AI_SESSIONS_STATE_FILE=str(state),
        AI_SESSIONS_CACHE_DIR=str(root / "cache"),
        AI_SESSIONS_MESSAGING_DIR=str(root / "messaging"),
    )
    store = WorkspaceStore(root / "workspaces.sqlite3")
    os.environ["AI_SESSIONS_WORKSPACE_FILE"] = str(store.path)
    layout = [
        dict(action="newTab", cwd=str(root)),
        dict(action="splitPane", split="right", size=0.5, cwd=str(root)),
    ]
    name = "native-probe-" + root.name[-8:]
    store.save(name, layout)
    for slot, tool, sid in [("pane-0", "codex", args.codex), ("pane-1", "claude", args.claude)]:
        adapter = app.REGISTRY.get(tool)
        ref = adapter.resolve(sid)
        if ref is None:
            raise ValueError(f"Missing disposable {tool} session")
        store.bind(
            name,
            slot,
            dict(
                tool=tool,
                session_id=sid,
                home=str(adapter.home),
                storage=ref.storage,
                cwd=str(root),
                title="Native workspace probe",
            ),
        )
    native.configure(store.path, exe)
    try:
        rounds = []
        previous = set()
        for iteration in range(2):
            native.launch(store.get(name), store.path, state)
            deadline = time.monotonic() + 35
            leases = []
            observed_clients = False
            while time.monotonic() < deadline:
                with closing(sqlite3.connect(store.path)) as db:
                    leases = db.execute(
                        "SELECT slot,identity,pid,started FROM leases WHERE workspace=?", (name,)
                    ).fetchall()
                if len(leases) == 2 and all(
                    live_identity(row[2], row[3])
                    and (row[2], row[3]) not in previous
                    and any(
                        child.name().casefold() == json.loads(row[1])[0] + ".exe"
                        and json.loads(row[1])[1] in child.cmdline()
                        for child in psutil.Process(row[2]).children(recursive=True)
                    )
                    for row in leases
                ):
                    observed_clients = True
                    break
                time.sleep(0.5)
            if (
                not observed_clients
                or len(leases) != 2
                or not all(
                    live_identity(row[2], row[3]) and (row[2], row[3]) not in previous
                    for row in leases
                )
            ):
                raise RuntimeError(f"Expected two live native assignments, found {leases}")
            time.sleep(2)
            if not all(live_identity(row[2], row[3]) for row in leases):
                raise RuntimeError("Native launcher exited before close")
            rounds.append(leases)
            previous.update((row[2], row[3]) for row in leases)
            close_owned(exe)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                live = [
                    p
                    for p in psutil.process_iter(["exe"])
                    if p.info["exe"] and Path(p.info["exe"]) == exe
                ]
                if not live:
                    break
                time.sleep(0.5)
            if live:
                raise RuntimeError("Test window did not close normally")
            time.sleep(1)
    finally:
        close_owned(exe)
    before = {row[0]: row[1] for row in rounds[0]}
    after = {row[0]: row[1] for row in rounds[1]}
    if before != after or {(r[2], r[3]) for r in rounds[0]} & {(r[2], r[3]) for r in rounds[1]}:
        raise RuntimeError("Close/reopen did not preserve assignments with fresh launchers")
    result = dict(workspace=name, rounds=rounds, same_assignments=True, fresh_launchers=True)
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
