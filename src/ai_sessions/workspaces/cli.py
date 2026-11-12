"""Named Terminal layouts and explicit, home-pinned native session bindings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from ..compression import CompressionError
from . import terminal
from .store import WorkspaceStore


def _parser():
    parser = argparse.ArgumentParser(prog="sessions workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("save")
    save.add_argument("name")
    save.add_argument("--window-index", type=int)
    save.add_argument("--tab-index", type=int)
    save.add_argument("--state-file", type=Path)
    save.add_argument("--force", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    bind = commands.add_parser("bind")
    bind.add_argument("name")
    bind.add_argument("slot")
    bind.add_argument("reference")
    bind.add_argument("--move", action="store_true")
    clone = commands.add_parser("clone")
    clone.add_argument("source")
    clone.add_argument("destination")
    restore = commands.add_parser("restore")
    restore.add_argument("name")
    restore.add_argument("--window", default="0")
    restore.add_argument("--allow-unbound", action="store_true")
    restore.add_argument("--dry-run", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("name")
    run.add_argument("slot")
    run.add_argument("--dry-run", action="store_true")
    return parser


def _state():
    from .. import app

    path = os.environ.get("AI_SESSIONS_STATE_FILE")
    return app.UserState(Path(path)) if path else app.UserState()


def _instruction(name, slot):
    return f"sessions workspace bind {name} {slot} ID_OR_SLUG"


def _save(args, store):
    tabs = [
        tab
        for tab in terminal.saved_tabs(args.state_file)
        if (args.window_index is None or tab["window"] == args.window_index)
        and (args.tab_index is None or tab["tab"] == args.tab_index)
    ]
    if not tabs:
        raise ValueError("no saved Terminal tab matches the requested window/tab indices")
    if len(tabs) > 1:
        if not sys.stdin.isatty():
            raise ValueError("ambiguous saved layout; specify --window-index and --tab-index")
        for number, tab in enumerate(tabs, 1):
            print(
                f"{number}. window {tab['window']} tab {tab['tab']}: "
                f"{tab['title']} ({tab['pane_count']} panes)"
            )
        try:
            choice = int(input("Select layout number: "))
        except (ValueError, EOFError):
            raise ValueError("choose a numbered saved layout") from None
        if not 1 <= choice <= len(tabs):
            raise ValueError("saved layout number is out of range")
        tab = tabs[choice - 1]
    else:
        tab = tabs[0]
    store.save(args.name, tab["layout"], replace=args.force)
    print(f"Saved {args.name}: window {tab['window']} tab {tab['tab']}")
    for action in tab["layout"]:
        if "slot" in action:
            print(f"{action['slot']}: {json.dumps(action, ensure_ascii=False)}")
            print(_instruction(args.name, action["slot"]))


def _bind(args, store):
    from .. import app
    from ..references import native_key, resolve_session

    state = _state()
    items = app.load_session_catalog(state=state)
    try:
        item = resolve_session(args.reference, items, store=state.slugs)
    except ValueError as error:
        # Ambiguity is not an invitation to choose a different catalog entry.
        if "ambiguous" in str(error).lower():
            raise
        items = app.load_sessions(state=state)
        item = resolve_session(args.reference, items, store=state.slugs)
    home = app.REGISTRY.get(item.tool).home
    if not home:
        raise ValueError("active harness does not expose its home; binding refused")
    key = native_key(item.tool, item.session_id, home)
    binding = dict(
        tool=key.tool,
        session_id=key.session_id,
        home=key.home,
        title=str(item.title or ""),
        cwd=str(item.cwd or ""),
        storage=str(item.storage or ""),
    )
    if args.move:
        store.bind(args.name, args.slot, binding, move=True)
    else:
        store.bind(args.name, args.slot, binding)
    print(f"Bound {args.name} {args.slot} to {item.key}")


@contextmanager
def _slot_environment(name, slot, store, state):
    values = dict(
        AI_SESSIONS_WORKSPACE=name,
        AI_SESSIONS_WORKSPACE_PANE=slot,
        AI_SESSIONS_WORKSPACE_FILE=str(store.path),
        AI_SESSIONS_STATE_FILE=str(state.path),
    )
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(args, store):
    from .. import app
    from ..capabilities import Unsupported
    from ..model import Session
    from ..references import native_key

    workspace = store.get(args.name)
    if args.slot not in workspace["panes"]:
        raise ValueError(f"unknown workspace slot {args.slot!r}")
    binding = workspace["panes"][args.slot]
    if binding is None:
        print(_instruction(args.name, args.slot))
        return 0
    adapter = app.REGISTRY.get(binding["tool"])
    if not adapter.home:
        raise ValueError("active harness does not expose its home; workspace run refused")
    active = native_key(binding["tool"], binding["session_id"], adapter.home)
    saved = native_key(binding["tool"], binding["session_id"], binding["home"])
    if active != saved:
        raise ValueError("binding home differs from active harness home; refusing account switch")
    resolve = adapter.resolve
    ref = None if isinstance(resolve, Unsupported) else resolve(saved.session_id)
    if ref is not None and ref.session_id != saved.session_id:
        raise ValueError("adapter resolved a different native session")
    storage = ref.storage if ref is not None else binding.get("storage", "")
    if not storage:
        raise ValueError("bound native session is unavailable; bind an existing native session")
    item = Session(
        saved.tool,
        saved.session_id,
        binding.get("title", ""),
        binding.get("cwd", ""),
        0,
        0,
        "",
        False,
        storage,
    )
    state = _state()
    state._apply_supervisors([item])
    known = app.detect_open_sessions([item])
    if item.is_open:
        print(f"Skipping already running session {item.key}")
        return 0
    if known is False:
        raise ValueError("could not establish whether bound session is running; refusing duplicate")
    if not args.dry_run:
        if ref is not None:
            if not app._restore_native_session(adapter, saved.tool, saved.session_id):
                return 2
            from ..compression import plain_path

            item.storage = str(plain_path(Path(item.storage)))
        else:
            from ..retention import decompress_session, needs_restore

            if needs_restore(item):
                note = decompress_session(item)
                if note:
                    print(note)
    config = app.LaunchConfig.load()
    with _slot_environment(args.name, args.slot, store, state):
        code = app.open_again(item, config, state, [item], dry_run=args.dry_run, exact_native=True)
        # Keep direct `workspace run` inside its slot for the entire restart
        # lifecycle, just like the managed PowerShell bootstrap does.
        while app._restart_into is not None:
            into, app._restart_into = app._restart_into, None
            code = app._main(["--reopen", *(["--launch-tool", into] if into else [])])
        return code


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        store = WorkspaceStore()
        if args.command == "save":
            _save(args, store)
        elif args.command == "bind":
            _bind(args, store)
        elif args.command == "clone":
            store.clone(args.source, args.destination)
            print(f"Cloned {args.source} to {args.destination}; bind its empty slots.")
        elif args.command in ("list", "show"):
            result = store.names() if args.command == "list" else store.get(args.name)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif args.command == "list":
                for entry in result:
                    print(json.dumps(entry, ensure_ascii=False))
            else:
                print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "restore":
            workspace = store.get(args.name)
            missing = [slot for slot, binding in workspace["panes"].items() if binding is None]
            if missing and not args.allow_unbound:
                raise ValueError(
                    "unbound slots: " + ", ".join(missing) + "; bind them or use --allow-unbound"
                )
            command = terminal.restore_argv(
                workspace,
                window=args.window,
                store_path=store.path,
                state_path=os.environ.get("AI_SESSIONS_STATE_FILE"),
            )
            if args.dry_run:
                print(json.dumps(command, ensure_ascii=False))
            else:
                subprocess.Popen(command)
        elif args.command == "run":
            return _run(args, store)
        return 0
    except (ValueError, KeyError, OSError, CompressionError) as error:
        print(f"sessions workspace: {error}", file=sys.stderr)
        return 2
