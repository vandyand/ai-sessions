"""Project desk commands; observation never sends team messages."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from ..harnesses.desk_support import DEFAULT_TOOL, TOOLS
from .store import Store


def resolve_sources(supervisor: str, additional: list[str]) -> tuple[list[dict], str]:
    from ..app import load_session_catalog, load_sessions
    from ..references import SlugStore, resolve_session

    items = load_session_catalog()
    if not items:
        items = load_sessions()
    slugs = SlugStore()
    rows, seen = [], set()
    cwd = ""
    for i, reference in enumerate([supervisor, *additional]):
        item = resolve_session(reference, items, store=slugs)
        if item.compressed:
            raise ValueError(f"{item.title} is compressed; decompress it before observing")
        if not item.storage:
            raise ValueError(f"{item.title} has no readable native transcript")
        if i == 0:
            cwd = str(Path(item.cwd or ".").resolve())
        if item.key in seen:
            continue
        seen.add(item.key)
        rows.append(
            {
                "tool": item.tool,
                "session_id": item.session_id,
                "storage": str(Path(item.storage).resolve()),
                "title": item.title,
                "role": "supervisor" if i == 0 else "team",
            }
        )
    return rows, cwd


def positive_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a number") from None
    if not 5 <= number <= 3600:
        raise argparse.ArgumentTypeError("interval must be between 5 and 3600 seconds")
    return number


def watch(store, name: str, interval: float, stop: threading.Event) -> None:
    from .observer import refresh

    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = refresh(store, name, stop=stop)
            failures = failures + 1 if result.get("error") else 0
        except (OSError, RuntimeError, ValueError) as error:
            failures += 1
            store.fail(name, f"Observer unavailable ({type(error).__name__}).")
        delay = min(900, interval * (2 ** min(failures, 4))) if failures else interval
        stop.wait(max(1, delay - (time.monotonic() - started)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="sessions desk", description="A quiet project conversation and live briefing."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser(
        "create", help="Choose the operational supervisor and team to observe"
    )
    create.add_argument("name")
    create.add_argument("--supervisor", required=True)
    create.add_argument("--source", action="append", default=[])
    for action in ("show", "refresh", "watch", "view", "open"):
        sub = commands.add_parser(action)
        sub.add_argument("name")
        if action == "show":
            sub.add_argument("--json", action="store_true")
        if action in ("watch", "view"):
            sub.add_argument("--interval", type=positive_seconds, default=60.0)
        if action == "view":
            sub.add_argument(
                "--watch",
                action="store_true",
                help="Update the briefing with Haiku while this view is open",
            )
        if action == "open":
            sub.add_argument("--tool", choices=TOOLS, default=DEFAULT_TOOL)
            sub.add_argument("--dry-run", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        store = Store()
        if args.action == "list":
            rows = store.list()
            print(
                json.dumps(rows, ensure_ascii=False)
                if args.json
                else "\n".join(row["name"] for row in rows) or "No project desks yet."
            )
        elif args.action == "create":
            sources, cwd = resolve_sources(args.supervisor, args.source)
            store.create(args.name, sources, cwd)
            store.export(args.name)
            print(f"Created {args.name}. Open the conversation: sessions desk open {args.name}")
            print(f"In the adjacent pane: sessions desk view {args.name} --watch")
        elif args.action == "show":
            row = store.get(args.name)
            if args.json:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                from .view import sanitize

                print(
                    sanitize(row.get("review")) or "No briefing yet. Run refresh or view --watch."
                )
                pending = [d for d in row.get("decisions", []) if d["status"] == "pending"]
                for decision in pending:
                    print(
                        "\n"
                        + sanitize(decision["question"])
                        + "\n"
                        + sanitize(decision.get("context", ""))
                    )
                if row.get("error"):
                    print("\nUpdate unavailable: " + row["error"])
        elif args.action == "refresh":
            from .observer import refresh

            result = refresh(store, args.name)
            if result.get("error"):
                raise RuntimeError(result["error"])
            print(f"Updated {args.name}.")
        elif args.action == "open":
            from .launch import open_agent

            return open_agent(store, args.name, args.tool, dry_run=args.dry_run)
        elif args.action == "watch":
            store.get(args.name)
            watch(store, args.name, args.interval, threading.Event())
        elif args.action == "view":
            from .view import run

            store.get(args.name)
            stop = threading.Event()
            thread = None
            if args.watch:
                thread = threading.Thread(
                    target=watch, args=(store, args.name, args.interval, stop), daemon=True
                )
                thread.start()
            try:
                run(store, args.name)
            finally:
                stop.set()
                if thread:
                    thread.join(timeout=10)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"sessions desk: {error}", file=sys.stderr)
        return 2
