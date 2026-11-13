"""Start native interactive sessions with exact identity tracking and restart."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path

from .capabilities import Unsupported
from .model import Session, SourceKind
from .registry import REGISTRY


def option_specs(help_text):
    """Read option arity from the native executable's own help, not guesses."""
    specs = {}
    for line in help_text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        header = re.split(r"\s{2,}", line, maxsplit=1)[0]
        names = re.findall(r"(?<!\w)--?[A-Za-z][A-Za-z0-9-]*", header)
        for name in names:
            specs[name] = ("<" in header or "[" in header, "..." in header, "[" in header)
    return specs


def restart_options(arguments, specs, discard):
    """Keep CLI options; omit positional prompts, selectors, and one-time actions."""
    result = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            continue
        key = token.split("=", 1)[0]
        attached = "=" in token
        if key not in specs and len(key) > 2 and key[:2] in specs and specs[key[:2]][0]:
            key, attached = key[:2], True
        if key not in specs:
            raise ValueError("Native options could not be preserved safely for restart.")
        takes, variadic, optional = specs[key]
        values = []
        if takes and not attached:
            while index < len(arguments):
                value = arguments[index]
                if value.startswith("-") and (optional or variadic):
                    break
                values.append(value)
                index += 1
                if not variadic:
                    break
            if not values and not optional:
                raise ValueError("A native option is missing its value.")
        if key not in discard:
            result.extend([token, *values])
    return result


@lru_cache(maxsize=16)
def _specs(executable, resume_command):
    specs = {}
    commands = [[executable, "--help"]]
    if resume_command and not resume_command.startswith("-"):
        commands.append([executable, resume_command, "--help"])
    for command in commands:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
        if result.returncode:
            raise ValueError("Native help is unavailable; restart options cannot be verified.")
        specs.update(option_specs(result.stdout))
    return specs


def _started(pid):
    from .platforms.windows import process_started

    return process_started(pid)


def _remember(item, tool, pid, started, restart_error):
    from .detached import remember

    remember(item, tool, pid, started, restart_error=restart_error, new_launcher=True)


def _closed(pid):
    from .detached import mark_closed

    return mark_closed(pid)


def _latest_registered(adapter, pid, started, cwd):
    from .detached import TERMINALS_FILE, _read

    for entry in _read(TERMINALS_FILE).values():
        if (
            entry.get("pid") != pid
            or entry.get("tool") != adapter.name
            or entry.get("started") != started
            or not entry.get("new_launcher")
        ):
            continue
        ref = adapter.resolve(str(entry.get("session_id", "")))
        if ref is not None:
            return Session(
                adapter.name,
                ref.session_id,
                str(entry.get("title") or adapter.label),
                str(entry.get("cwd") or cwd),
                0,
                0,
                "",
                False,
                ref.storage,
            )
    return None


def _identify(adapter, pid, started, cwd, known=None):
    ref = adapter.current_session(pid, started)
    if ref is None:
        return None
    if known is not None and known.session_id == ref.session_id and known.storage == ref.storage:
        return known
    return _from_ref(adapter, ref, cwd)


def _from_ref(adapter, ref, cwd):
    item = Session(
        adapter.name, ref.session_id, f"{adapter.label} session", cwd, 0, 0, "", False, ref.storage
    )
    if callable(adapter.rediscover):
        from .app import session_from_native
        from .discovery import HarnessContext

        rows = adapter.rediscover(HarnessContext.create(), [ref])
        exact = [row for row in rows if row.session_id == ref.session_id]
        if len(exact) == 1:
            item = session_from_native(exact[0])
    return item


def _positionals(arguments, specs):
    result = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == "--":
            result.extend(arguments[index:])
            break
        if not token.startswith("-") or token == "-":
            result.append(token)
            continue
        key = token.split("=", 1)[0]
        attached = "=" in token
        if key not in specs and key[:2] in specs and specs[key[:2]][0]:
            key, attached = key[:2], True
        if key not in specs:
            raise ValueError("Native options are unavailable.")
        takes, variadic, optional = specs[key]
        if takes and not attached:
            while index < len(arguments):
                if arguments[index].startswith("-") and (optional or variadic):
                    break
                index += 1
                if not variadic:
                    break
    return result


def _prepare_initial(adapter, executable, arguments, specs, cwd, resume):
    """Assign the conversation before launching when the provider supports it."""
    before_separator = arguments[: arguments.index("--")] if "--" in arguments else arguments
    for index, arg in enumerate(before_separator):
        remote = (
            arg.split("=", 1)[1]
            if arg.startswith("--remote=")
            else before_separator[index + 1]
            if arg == "--remote" and index + 1 < len(before_separator)
            else None
        )
        if remote is not None and remote != "unix://":
            raise ValueError(
                "Managed new sessions require local native storage; use the native CLI for a nonlocal --remote endpoint."
            )
    if not callable(getattr(adapter, "create_session", None)) or any(
        arg in ("--help", "-h", "--version", "-V") for arg in before_separator
    ):
        return [executable, *arguments], None
    effective_cwd = cwd
    for index, arg in enumerate(before_separator):
        if arg in ("--cd", "-C") and index + 1 < len(before_separator):
            effective_cwd = str((Path(cwd) / before_separator[index + 1]).resolve())
        elif arg.startswith("--cd=") or (arg.startswith("-C") and len(arg) > 2):
            value = arg.split("=", 1)[1] if arg.startswith("--") else arg[2:]
            effective_cwd = str((Path(cwd) / value).resolve())
    positional = _positionals(arguments, specs)
    command_positions = _positionals(before_separator, specs)
    help_text = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    ).stdout
    commands = set(re.findall(r"^\s{2}([a-z][a-z-]+)\s{2,}", help_text, re.MULTILINE))
    if command_positions and command_positions[0] in commands:
        reference = command_positions[1] if len(command_positions) > 1 else None
        ref = (
            adapter.resolve(reference) if command_positions[0] == resume[0] and reference else None
        )
        if command_positions[0] == resume[0] and ref is None:
            ref = _choose_resume(
                adapter,
                reference,
                effective_cwd,
                last="--last" in arguments,
                all_dirs="--all" in arguments,
                include_non_interactive="--include-non-interactive" in arguments,
            )
            if ref is None:
                ref = adapter.create_session(cwd=effective_cwd, name="")
            item = _from_ref(adapter, ref, effective_cwd)
            resume_args = adapter.resume_args(
                session_id=ref.session_id,
                source=item.source,
                resume_id=item.resume_id,
                parent_id=item.parent_id,
                native=True,
            )
            options = restart_options(
                arguments, specs, {"--last", "--all", "--include-non-interactive"}
            )
            prompt = positional[2:] if reference else []
            return [executable, *resume_args, *options, *(["--", *prompt] if prompt else [])], item
        return [executable, *arguments], _from_ref(adapter, ref, effective_cwd) if ref else None
    # Native arguments stay native. An empty provider-owned conversation gives
    # the launcher a stable identity before the shared server sees this client.
    ref = adapter.create_session(cwd=effective_cwd, name="")
    options = restart_options(arguments, specs, set())
    resume_args = adapter.resume_args(
        session_id=ref.session_id,
        source=SourceKind.INTERACTIVE,
        resume_id=ref.session_id,
        parent_id="",
        native=True,
    )
    return [
        executable,
        *resume_args,
        *options,
        *(["--", *positional] if positional else []),
    ], _from_ref(adapter, ref, effective_cwd)


def _choose_resume(
    adapter, reference, cwd, *, last=False, all_dirs=False, include_non_interactive=False
):
    """Resolve selection before launch when native UI ownership is not exposed."""
    from .app import UserState, session_from_native
    from .discovery import HarnessContext
    from .model import NativeRef
    from .references import resolve_session
    from .workspaces.setup import choose_session

    rows = adapter.discover(HarnessContext.create(), use_cache=True)
    items = [
        session_from_native(row)
        for row in rows
        if not row.archived and (include_non_interactive or row.source is SourceKind.INTERACTIVE)
    ]
    if not all_dirs and not reference:
        expected = os.path.normcase(os.path.abspath(cwd))
        items = [
            item for item in items if os.path.normcase(os.path.abspath(item.cwd or cwd)) == expected
        ]
    if reference:
        state = UserState()
        try:
            item = resolve_session(reference, items, store=state.slugs)
        except ValueError:
            named = [item for item in items if item.title == reference]
            if len(named) != 1:
                raise ValueError(
                    "Session reference is missing or ambiguous; use its ID or unique slug."
                ) from None
            item = named[0]
    elif last:
        if not items:
            raise ValueError("No saved session matches this directory.")
        item = max(items, key=lambda item: item.updated)
    else:
        print("Choose the conversation to resume, or type empty for a new conversation.")
        item = choose_session(items)
    return NativeRef(item.session_id, item.storage) if item is not None else None


def _abort_owned(process, job, tool, started):
    """Exceptional cleanup owns this client only, never a shared server."""
    if job is not None:
        job.close()
        return
    from .detached import stop_for_restart

    adapter = REGISTRY.get(tool)
    note = stop_for_restart(process.pid, started, adapter.liveness_executables)
    if note:
        print(
            "sessions: could not clean up the newly started client; close its pane manually.",
            file=sys.stderr,
        )


@contextmanager
def _workspace_context(item, tool):
    from .workspaces.launch import launch_slot

    context = launch_slot(item, tool)
    try:
        context.__enter__()
    except (ValueError, OSError):
        # The native child already exists. Keep waiting for it rather than
        # orphaning it when an optional workspace registration is refused.
        print(
            "sessions: workspace assignment unavailable; native session remains running.",
            file=sys.stderr,
        )
        yield
        return
    try:
        yield
    finally:
        context.__exit__(None, None, None)


def run(tool, arguments):
    adapter = REGISTRY.get(tool)
    if isinstance(adapter.native_executable, Unsupported) or isinstance(
        adapter.current_session, Unsupported
    ):
        raise ValueError("This harness does not support managed new sessions yet.")
    executable = adapter.native_executable()
    resume = adapter.resume_args(
        session_id="identity",
        source=SourceKind.INTERACTIVE,
        resume_id="identity",
        parent_id="",
        native=True,
    )
    try:
        specs = _specs(executable, resume[0])
        reusable = restart_options(arguments, specs, set(adapter.restart_discard_options))
        restart_error = ""
    except (OSError, ValueError, subprocess.SubprocessError):
        reusable = []
        restart_error = (
            "Native options could not be verified for restart; exit and resume manually."
        )
    command = [executable, *arguments]
    cwd = os.getcwd()
    seeded = None
    if not restart_error:
        command, seeded = _prepare_initial(adapter, executable, arguments, specs, cwd, resume)
    while True:
        if adapter.contain_launch and os.name == "nt":
            from .platforms.windows import KillOnCloseJob

            process, job = KillOnCloseJob.start(command, cwd=cwd)
        else:
            process, job = subprocess.Popen(command, cwd=cwd), None
        started = _started(process.pid)
        current = seeded
        remembered_at = time.monotonic()
        completed = False
        with ExitStack() as resources:
            try:
                if current is not None:
                    resources.enter_context(_workspace_context(current, tool))
                    _remember(current, tool, process.pid, started, restart_error)
                while True:
                    try:
                        item = (
                            _identify(adapter, process.pid, started, cwd, current)
                            if seeded is None
                            else None
                        )
                        if item is not None and (
                            current is None or current.session_id != item.session_id
                        ):
                            resources.close()
                            resources.enter_context(_workspace_context(item, tool))
                            current = item
                            _remember(item, tool, process.pid, started, restart_error)
                            remembered_at = time.monotonic()
                        elif current is not None and time.monotonic() - remembered_at >= 60:
                            ref = adapter.resolve(current.session_id)
                            if ref is not None:
                                current = _from_ref(adapter, ref, cwd)
                            _remember(current, tool, process.pid, started, restart_error)
                            remembered_at = time.monotonic()
                    except KeyboardInterrupt:
                        continue
                    except (OSError, ValueError):
                        pass  # Native startup/identity files may not exist yet.
                    try:
                        code = process.wait(timeout=0.25)
                        completed = True
                        break
                    except subprocess.TimeoutExpired:
                        continue
                    except KeyboardInterrupt:
                        continue  # The native CLI receives the same console event.
            finally:
                if not completed:
                    _abort_owned(process, job, tool, started)
                elif job is not None:
                    job.release()
        wanted = _closed(process.pid)
        if wanted is None:
            return code
        registered = _latest_registered(adapter, process.pid, started, cwd)
        if registered is not None:
            current = registered
        if current is None or restart_error:
            print(
                "sessions: native identity or restart options unavailable; nothing was resumed.",
                file=sys.stderr,
            )
            return 2
        if wanted and wanted != tool:
            from .app import main as sessions_main

            mode = (
                "dangerous" if any(flag in arguments for flag in adapter.dangerous_args) else "safe"
            )
            return sessions_main(["--reopen", "--launch-tool", wanted, "--launch-mode", mode])
        print(f"sessions: restarting {current.title!r}…", file=sys.stderr)
        resume = adapter.resume_args(
            session_id=current.session_id,
            source=current.source,
            resume_id=current.resume_id,
            parent_id=current.parent_id,
            native=True,
        )
        command = [executable, *reusable, *resume]
        cwd = current.cwd or cwd
        seeded = current


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    native = []
    if "--" in raw:
        split = raw.index("--")
        raw, native = raw[:split], raw[split + 1 :]
    parser = argparse.ArgumentParser(
        prog="sessions new",
        description="Start a native session with sesres support; native CLI arguments follow --.",
    )
    parser.add_argument(
        "tool",
        choices=[
            adapter.name for adapter in REGISTRY.adapters() if callable(adapter.current_session)
        ],
    )
    args = parser.parse_args(raw)
    try:
        return run(args.tool, native)
    except (OSError, ValueError) as error:
        print(f"sessions: {error}", file=sys.stderr)
        return 2
