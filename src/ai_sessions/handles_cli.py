"""Short project/session commands built on the shared handle resolver."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def catalog():
    from .app import UserState, load_session_catalog, load_sessions, sessions_are_known

    state = UserState()
    items = load_session_catalog(state)
    if not items:
        items = load_sessions(state=state)
        state.apply(items)
    state._handles_liveness_known = sessions_are_known(items)
    return items, state


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ses")
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list", help="Show session handles and current project activity")
    listing.add_argument("project", nargs="?")
    listing.add_argument("--json", action="store_true")
    opening = commands.add_parser(
        "open", help="Resume the actual session identified by a handle, slug or ID"
    )
    opening.add_argument("session")
    opening.add_argument("--dry-run", action="store_true")
    projects = commands.add_parser("project")
    actions = projects.add_subparsers(dest="operation", required=True)
    actions.add_parser("list")
    add = actions.add_parser("add")
    add.add_argument("handle")
    add.add_argument("--directory", required=True, type=Path)
    rename = actions.add_parser("set")
    rename.add_argument("old")
    rename.add_argument("new")
    handles = commands.add_parser("handle")
    edits = handles.add_subparsers(dest="operation", required=True)
    edit = edits.add_parser("set")
    edit.add_argument("session")
    edit.add_argument("handle")
    assign = edits.add_parser("assign")
    assign.add_argument("session")
    assign.add_argument("--project", required=True)
    assign.add_argument("--role", choices=("sup", "mgr", "wrk", "ses", "session"))
    assign.add_argument("--number", type=int)
    args = parser.parse_args(argv)
    if args.action == "open":
        from .app import main as app_main

        return app_main(["--resume", args.session, *(["--dry-run"] if args.dry_run else [])])
    try:
        from .handles import HandleStore
        from .references import native_key, resolve_session

        items, state = catalog()
        store = state.handles if hasattr(state, "handles") else HandleStore()
        if args.action == "project":
            if args.operation == "list":
                for project in store.projects():
                    print(
                        f"{project['handle']:<8} {project.get('directory', project.get('root', ''))}"
                    )
            elif args.operation == "add":
                store.add_project(args.handle, str(args.directory.resolve()))
                print(f"Project handle: {args.handle}")
            else:
                store.set_project(args.old, args.new)
                print(f"Project handle: {args.new}")
            return 0
        if args.action == "handle":
            item = resolve_session(args.session, items, store=state.slugs)
            if args.operation == "set":
                store.set_session(item, args.handle)
            else:
                store.assign(item, args.project, role=args.role, number=args.number)
            store.ensure([item])
            print(item.handle)
            return 0
        from .app import updated_age
        from .desk.view import sanitize

        selected = [
            item
            for item in items
            if not args.project or item.project_handle.casefold() == args.project.casefold()
        ]
        if args.project and not selected:
            raise ValueError(
                f"No sessions for project handle {args.project!r}; use ses project list"
            )
        canonical = store.current_keys({item.handle for item in selected if item.handle})
        counts = Counter(item.handle for item in selected)
        chosen = {}
        for item in selected:
            key = canonical.get(item.handle)
            if key is None or native_key(item.tool, item.session_id) == key:
                chosen[item.handle] = item
        rows = []
        for handle, item in sorted(chosen.items()):
            rows.append(
                {
                    "handle": handle,
                    "project": item.project_handle,
                    "role": item.session_role,
                    "tool": item.tool,
                    "session_id": item.session_id,
                    "title": item.title,
                    "open": item.is_open
                    if getattr(state, "_handles_liveness_known", False)
                    else None,
                    "updated": item.updated,
                    "compressed": item.compressed,
                    "copies": counts[handle],
                }
            )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            width = max(12, max((len(row["handle"]) for row in rows), default=0))
            print(f"{'HANDLE':<{width}} {'HARNESS':<9} {'OPEN':<7} {'UPDATED':<18} TITLE")
            for row in rows:
                status = "yes" if row["open"] else "no" if row["open"] is False else "?"
                print(
                    f"{row['handle']:<{width}} {row['tool']:<9} {status:<7} {updated_age(row['updated']):<18} {sanitize(row['title'])}"
                )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"ses: {error}", file=sys.stderr)
        return 2
