"""Time what a person waits for in ``sessions``, against a copy of their own data.

    python tools/bench.py [--query TEXT] [--runs N] [--skip-refresh]

Session transcripts and harness databases are only read.  The utility's own state,
catalog and caches are copied into a temporary folder first and every write lands
there, so a run changes nothing the real ``sessions`` uses.  Each measurement runs in
a fresh interpreter so imports and file caches are counted the way a real start sees
them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"

# Run inside the child interpreter; prints one JSON object.
_CHILD = r"""
import json, sys, time
T = time.perf_counter()
mode, query = sys.argv[1], sys.argv[2]
from ai_sessions import app

class Screen:
    def __init__(self, keys):
        self.keys = list(keys)
    def keypad(self, _on): pass
    def getmaxyx(self): return (40, 160)
    def erase(self): pass
    def addnstr(self, *_args): pass
    def refresh(self): pass
    def timeout(self, _ms): pass
    def get_wch(self):
        if not self.keys:
            return "\x1b"
        return self.keys.pop(0)

result = {}
if mode == "start":
    original = app.Browser.draw
    def draw(self):
        original(self)
        if self.sessions and "first_frame" not in result:
            result["first_frame"] = time.perf_counter() - T
            result["sessions"] = len(self.sessions)
            rows = self.view_rows()
            check = getattr(self, "check_before_open", None)
            if rows and check is not None:
                started = time.perf_counter()
                check(rows[0])
                # What Enter waits for when the refresh has not finished.
                result["enter_check"] = time.perf_counter() - started
        if self.refresh is not None and "refresh_done" not in result:
            self.refresh.take(wait=True)
            result["refresh_done"] = time.perf_counter() - T
            raise SystemExit(0)
        if self.refresh is None and "first_frame" in result:
            result.setdefault("refresh_done", result["first_frame"])
            raise SystemExit(0)
    app.Browser.draw = draw
    app.curses.wrapper = lambda function: function(Screen(["\x1b"] * 5))
    app.start_background_sweep = lambda: None
    sys.stdin.isatty = lambda: True
    sys.stdout.isatty = lambda: True
    try:
        app._main(["-q", query] if query else [])
    except SystemExit:
        pass
elif mode == "keys":
    import curses
    state = app.UserState()
    sessions = app.load_session_catalog(state)
    config = app.LaunchConfig.load()
    for origin in ("human", "all"):
        for label, keys in (("arrow", [curses.KEY_DOWN] * 20), ("typed", list("session"))):
            browser = app.Browser(Screen(keys + ["\x1b", "\x1b", "\x1b"]), sessions, state, config)
            browser.origin = origin
            browser.searching = label == "typed"
            started = time.perf_counter()
            browser.run()
            elapsed = time.perf_counter() - started
            result[f"{origin}_{label}_ms"] = 1000 * elapsed / len(keys)
print(json.dumps(result))
"""


def sandbox(folder: Path) -> dict[str, str]:
    """Copy the utility's own files into ``folder`` and point a child at them."""
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    cache, config = local / "ai-sessions", roaming / "ai-sessions"
    (folder / "cache").mkdir()
    (folder / "config").mkdir()
    for path in cache.glob("*"):
        # Current-version caches only; leftovers from old versions are never read.
        if path.is_file() and ".tmp-" not in path.name:
            shutil.copy2(path, folder / "cache" / path.name)
    for name in ("state.json", "config.toml"):
        if (config / name).exists():
            shutil.copy2(config / name, folder / "config" / name)
    environment = dict(os.environ)
    environment.update(
        AI_SESSIONS_CACHE_DIR=str(folder / "cache"),
        AI_SESSIONS_STATE_FILE=str(folder / "config" / "state.json"),
        AI_SESSIONS_CONFIG_FILE=str(folder / "config" / "config.toml"),
        PYTHONPATH=str(SOURCE),
        PYTHONIOENCODING="utf-8",
    )
    return environment


def child(mode: str, query: str, environment: dict[str, str]) -> dict[str, float]:
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, mode, query],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"bench {mode} failed:\n{completed.stderr[-2000:]}")
    return json.loads(lines[-1])


def hook_import(environment: dict[str, str]) -> dict[str, float]:
    completed = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import ai_sessions.supervisor.hook"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    total = 0
    heavy = []
    for line in completed.stderr.splitlines():
        parts = line.split("|")
        if len(parts) == 3 and parts[0].startswith("import time:"):
            name = parts[2].strip()
            if not parts[2].startswith("  "):
                try:
                    total += int(parts[1])
                except ValueError:
                    pass
            if name in ("psutil", "ai_sessions.harnesses.codex", "ai_sessions.app"):
                heavy.append(name)
    return {"hook_import_ms": total / 1000, "hook_loads_heavy": len(heavy)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default="sessions")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--skip-refresh", action="store_true", help="only time keys and hooks")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ai-sessions-bench-") as directory:
        environment = sandbox(Path(directory))
        samples: dict[str, list[float]] = {}

        def add(values: dict[str, float]) -> None:
            for key, value in values.items():
                samples.setdefault(key, []).append(value)

        if not args.skip_refresh:
            # The first start may rebuild caches the copy lacked; it is not counted.
            child("start", args.query, environment)
            for _ in range(args.runs):
                add(child("start", args.query, environment))
        for _ in range(max(1, args.runs // 2)):
            add(child("keys", args.query, environment))
            add(hook_import(environment))
    width = max(len(key) for key in samples)
    for key, values in samples.items():
        unit = "" if key.endswith("_ms") or key in ("sessions", "hook_loads_heavy") else " s"
        print(f"{key:<{width}}  {statistics.median(values):8.2f}{unit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
