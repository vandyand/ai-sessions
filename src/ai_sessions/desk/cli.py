"""Project desk commands; observation never sends team messages."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from ..harnesses.desk_support import DEFAULT_TOOL, TOOLS
from .runtime import Activity, ensure_observer, folder, serialized, status, stop_observer
from .store import Store


def resolve_sources(supervisor: str, additional: list[str]) -> tuple[list[dict], str]:
    from ..app import load_session_catalog, load_sessions
    from ..references import SlugStore, native_key, resolve_session

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
                "home": native_key(item.tool, item.session_id).home,
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


def watch(store, name: str, interval: float, stop: threading.Event, activity=None) -> None:
    from .observer import refresh

    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        if activity is not None:
            activity["busy"] = True
        try:
            result = refresh(store, name, stop=stop)
            failures = failures + 1 if result.get("error") else 0
        except (OSError, RuntimeError, ValueError) as error:
            failures += 1
            store.fail(name, f"Observer unavailable ({type(error).__name__}).")
        finally:
            if activity is not None:
                activity["busy"] = False
        delay = min(900, interval * (2 ** min(failures, 4))) if failures else interval
        stop.wait(max(1, delay - (time.monotonic() - started)))


def source_matches_target(source, target, members):
    """Match saved identity without guessing a legacy source's harness home."""
    from ..references import NativeKey

    if source.get("role", "supervisor") != "supervisor":
        return False
    if "home" in source:
        return NativeKey(source["tool"], source["session_id"], source["home"]) in members
    storage = Path(source["storage"]).expanduser().resolve()
    if (source["tool"], source["session_id"]) == (target.tool, target.session_id):
        if storage == Path(target.storage).expanduser().resolve():
            return True
    # Historical linked members may be absent from the current catalog. Only
    # their persisted homes can establish which native identity owned this path.
    if not Path(source["storage"]).expanduser().is_absolute():
        return False
    candidates = [
        member
        for member in members
        if (member.tool, member.session_id) == (source["tool"], source["session_id"])
        and storage.is_relative_to(Path(member.home).resolve())
    ]
    return len(candidates) == 1


def target_desks(store, item, state):
    from ..references import native_key

    handles = getattr(state, "handles", None)
    members = set(handles.lookup_members(item.handle) or ()) if handles and item.handle else set()
    members.add(native_key(item.tool, item.session_id))
    matches = [
        record
        for record in store.list()
        if any(source_matches_target(source, item, members) for source in record["sources"][:1])
    ]
    if len(matches) > 1:
        raise ValueError("Multiple desks observe this logical session; use a saved desk name")
    return matches


def update_target(store, record, item):
    from ..references import native_key

    source = dict(
        tool=item.tool,
        session_id=item.session_id,
        storage=item.storage,
        home=native_key(item.tool, item.session_id).home,
        title=item.title,
    )
    if any(
        record["sources"][0].get(key) != source[key]
        for key in ("tool", "session_id", "storage", "home")
    ):
        store.update_primary_source(record["name"], record["revision"], source)
        store.export(record["name"])


def ensure_record(store, item, state):
    """Both entry points serialize creation and preserve linked source membership."""
    from ..references import native_key

    with serialized(store.root / ".observers" / "records.lock"):
        matches = target_desks(store, item, state)
        if matches:
            update_target(store, matches[0], item)
            return matches[0]["name"]
        name = item.handle or item.slug or item.session_id
        store.create(
            name,
            [
                {
                    "tool": item.tool,
                    "session_id": item.session_id,
                    "storage": item.storage,
                    "home": native_key(item.tool, item.session_id).home,
                    "role": "supervisor",
                    "title": item.title,
                }
            ],
            str(Path(item.cwd or ".").resolve()),
        )
        store.export(name)
        return name


def _live_desk_member(members):
    from ..detached import _live_remembered, remembered
    from ..references import native_key

    live = set()
    for entry in remembered().values():
        tool, session_id, home = (entry.get(field) for field in ("tool", "session_id", "home"))
        if not all(isinstance(value, str) and value for value in (tool, session_id, home)):
            continue
        key = native_key(tool, session_id, home)
        if key in members and key == native_key(tool, session_id) and _live_remembered(entry):
            live.add(key)
    return next(iter(live)) if len(live) == 1 else None


def resolve_desk_name(store, reference, *, update=False, create=False):
    from ..handles import HandleStore

    try:
        saved = store.get(reference)
    except KeyError:
        saved = None
    # A saved name that is also a session handle must follow its linked copy.
    live = None
    if saved is not None:
        handles = HandleStore()
        try:
            current = handles.current_key(reference)
        except ValueError:
            # A retired/unavailable handle must not invalidate an existing desk name.
            return reference
        if current is None:
            return reference
        members = set(handles.lookup_members(reference) or ())
        if len(members) > 1:
            live = _live_desk_member(members)
    from ..handles_cli import catalog
    from ..references import native_key, resolve_session

    items, state = catalog()
    slugs = getattr(state, "slugs", None)
    target = f"{live.tool}:{live.session_id}" if live else reference
    try:
        item = resolve_session(target, items, store=slugs)
    except ValueError:
        # Switching harnesses can move the canonical handle before the saved
        # catalog sees the new copy. Refresh only for a known, missing key.
        key = live or HandleStore(slugs.path if slugs is not None else None).current_key(reference)
        if key is None or any(native_key(row.tool, row.session_id) == key for row in items):
            raise
        from ..app import load_sessions, save_session_catalog

        items = load_sessions(state=state)
        item = resolve_session(target, items, store=slugs)
        if native_key(item.tool, item.session_id) != key:
            raise ValueError(f"Handle {reference!r} changed during discovery; try again") from None
        save_session_catalog(items)
    if live and native_key(item.tool, item.session_id) != live:
        raise ValueError(f"The running copy of {reference!r} changed during discovery; try again")
    matches = target_desks(store, item, state)
    if not matches and create:
        if item.compressed or not item.storage:
            raise ValueError("Decompress the selected session before observing its desk")
        return ensure_record(store, item, state)
    if not matches:
        raise ValueError(f"No desk for this session yet; run ses desk {reference} first")
    record = matches[0]
    if saved is not None and saved["name"] != record["name"]:
        raise ValueError("Desk name conflicts with a session handle; use the qualified native ID")
    if update:
        if item.compressed or not item.storage:
            raise ValueError("Decompress the selected session before observing its desk")
        update_target(store, record, item)
    return record["name"]


def open_target(argv) -> int:
    from ..handles_cli import catalog
    from ..references import resolve_session
    from .launch import open_agent

    parser = argparse.ArgumentParser(prog="ses desk")
    parser.add_argument("target", help="Session handle, slug or native ID to observe")
    tool = parser.add_mutually_exclusive_group()
    tool.add_argument("--claude", dest="tool", action="store_const", const=TOOLS[0])
    tool.add_argument("--codex", dest="tool", action="store_const", const=TOOLS[1])
    tool.add_argument("--grok", dest="tool", action="store_const", const="grok")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        items, state = catalog()
        item = resolve_session(args.target, items, store=getattr(state, "slugs", None))
        if item.compressed or not item.storage:
            raise ValueError("Decompress the selected session before creating its desk")
        store = Store(readonly=args.dry_run)
        matches = target_desks(store, item, state)
        existing = next(iter(matches), None)
        name = existing["name"] if existing else item.handle or item.slug or item.session_id
        if args.dry_run and existing is None:
            print(
                json.dumps(
                    {
                        "desk": name,
                        "target": item.key,
                        "tool": args.tool or item.tool,
                        "create": True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if not args.dry_run:
            name = ensure_record(store, item, state)
            ensure_observer(store, name)
            print(
                f"Observer running. Status: ses desk status {args.target}; stop: ses desk stop {args.target}"
            )
        print(f"Briefing pane: ses desk view {args.target}")
        return open_agent(store, name, args.tool or item.tool, dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"ses desk: {error}", file=sys.stderr)
        return 2


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    actions = {
        "create",
        "show",
        "refresh",
        "watch",
        "view",
        "open",
        "list",
        "status",
        "stop",
        "check",
        "decisions",
        "history",
        "dismiss",
        "reply",
        "-h",
        "--help",
    }
    if raw and raw[0] not in actions:
        return open_target(raw)
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
    for action in (
        "show",
        "refresh",
        "watch",
        "view",
        "open",
        "status",
        "stop",
        "decisions",
        "history",
        "dismiss",
        "reply",
    ):
        sub = commands.add_parser(action)
        sub.add_argument("name", help="Session handle, native ID, slug, or saved desk name")
        if action in ("show", "decisions", "history"):
            sub.add_argument("--json", action="store_true")
        if action == "history":
            sub.add_argument("--limit", type=int, default=50)
            sub.add_argument("--offset", type=int, default=0)
        if action == "dismiss":
            sub.add_argument("number", type=int, help="Number from the decisions listing")
            sub.add_argument(
                "--revision", type=int, required=True, help="Revision shown in that listing"
            )
            sub.add_argument("--reason", required=True)
        if action == "reply":
            sub.add_argument("entry")
            sub.add_argument("--message", required=True)
        if action == "refresh":
            sub.add_argument(
                "--retry-context",
                action="store_true",
                help="Retry preparation of missing decision background",
            )
        if action in ("watch", "view"):
            sub.add_argument("--interval", type=positive_seconds, default=60.0)
        if action == "view":
            sub.add_argument(
                "--watch",
                action=argparse.BooleanOptionalAction,
                default=True,
                help="Ensure background Haiku updates (default); --no-watch reads saved briefings only",
            )
        if action == "open":
            sub.add_argument("--tool", choices=TOOLS, default=DEFAULT_TOOL)
            sub.add_argument("--dry-run", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    check = commands.add_parser("check")
    check.add_argument("name", help="Grok session id or updates.jsonl path")
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            from ..harnesses.grok import resolve
            from .grok_projection import check as grok_check

            path = Path(args.name)
            if not path.is_file():
                ref = resolve(args.name)
                if ref is None:
                    print(f"ses desk: unknown grok session {args.name}", file=sys.stderr)
                    return 2
                path = Path(ref.storage)
            from .grok_projection import sync as grok_sync

            if path.name == "updates.jsonl":
                grok_sync(path)
            code, text = grok_check(path)
            print(text)
            return code
        store = Store()
        if args.action in (
            "show",
            "refresh",
            "watch",
            "view",
            "open",
            "status",
            "stop",
            "decisions",
            "history",
            "dismiss",
            "reply",
        ):
            args.name = resolve_desk_name(
                store,
                args.name,
                update=args.action in ("refresh", "watch")
                or (args.action == "open" and not args.dry_run)
                or (args.action == "view" and args.watch),
                create=args.action == "view" and args.watch,
            )
        if args.action == "reply":
            from ..handle_lookup import current_identity
            from ..references import native_key
            from .discussion import agent_reply

            tool, ident = current_identity()
            native = native_key(tool, ident)
            agent_reply(
                store,
                args.name,
                args.entry,
                args.message,
                dict(tool=tool, session_id=ident, home=native.home),
            )
            ensure_observer(store, args.name)
            print("Context reply saved; clarification will resume in the background.")
            return 0
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
            print(f"In the adjacent pane: sessions desk view {args.name}")
        elif args.action in ("decisions", "dismiss"):
            from .backlog import audit
            from .view import sanitize

            snapshot = store.get(args.name)
            rows = audit(snapshot)
            if args.action == "dismiss":
                if args.number < 1 or args.number > len(rows):
                    raise ValueError("request number is unavailable; list decisions again")
                store.dismiss_decision(
                    args.name, rows[args.number - 1]["key"], args.revision, args.reason
                )
                store.export(args.name)
                print(
                    "Request dismissed; its original context and your reason are preserved in history."
                )
            elif args.json:
                print(
                    json.dumps(
                        dict(name=args.name, revision=snapshot["revision"], decisions=rows),
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"{args.name}: {len(rows)} pending requests; revision {snapshot['revision']}")
                if any(not row["age_known"] for row in rows):
                    print(
                        "Imported requests may have unknown original age; none are closed by age."
                    )
                for row in rows:
                    print(sanitize(f"{row['number']}. {row['summary']}"))
                    if row["flags"]:
                        print("   Review: " + "; ".join(row["flags"]))
                print("Dismiss with: ses desk dismiss HANDLE NUMBER --revision N --reason REASON")
        elif args.action == "history":
            from .store import _when
            from .view import sanitize

            rows = store.decision_history(args.name, limit=args.limit, offset=args.offset)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False))
            else:
                for row in rows:
                    title = (row.get("details") or {}).get("summary") or row["decision"]["question"]
                    print(
                        sanitize(
                            f"{_when(row['recorded_at'])} · {row['decision']['status']} · {title}"
                        )
                    )
                    if row.get("note"):
                        print("   " + sanitize(row["note"]))
                print(
                    f"{len(rows)} history entries; --offset {args.offset + len(rows)} for the next page."
                )
        elif args.action == "show":
            row = store.get(args.name)
            if args.json:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                from .view import sanitize

                print(
                    sanitize(row.get("review"))
                    or "No briefing yet. Run ses desk view to start live updates."
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

            result = (
                refresh(store, args.name, force_details=True)
                if args.retry_context
                else refresh(store, args.name)
            )
            if result.get("error"):
                raise RuntimeError(result["error"])
            print(f"Updated {args.name}.")
        elif args.action == "open":
            from .launch import open_agent

            if not args.dry_run:
                ensure_observer(store, args.name)
            return open_agent(store, args.name, args.tool, dry_run=args.dry_run)
        elif args.action == "watch":
            ensure_observer(store, args.name, args.interval)
            print(f"Observer running for {args.name}. Use ses desk stop {args.name} to stop.")
        elif args.action == "status":
            row = status(store, args.name)
            print(
                json.dumps(
                    dict(row, log=str(folder(store, args.name) / "observer.log")),
                    ensure_ascii=False,
                )
            )
        elif args.action == "stop":
            row = stop_observer(store, args.name)
            print(f"Observer {row['state']} for {args.name}.")
        elif args.action == "view":
            from .view import run

            store.get(args.name)
            if args.watch:
                ensure_observer(store, args.name, args.interval)
                run(store, args.name, activity=Activity(store, args.name))
            else:
                run(store, args.name)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"sessions desk: {error}", file=sys.stderr)
        return 2
