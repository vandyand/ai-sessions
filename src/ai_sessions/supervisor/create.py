"""Create a supervisor record for a target session and read the target's log."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..capabilities import Unsupported
from ..model import NativeRef, Session
from ..registry import REGISTRY
from . import store
from .index import TargetIndex, clean_exchange
from .store import TITLE_SUFFIX, HookState, load_hook_state, save_hook_state

if TYPE_CHECKING:
    from ..config import LaunchConfig

ENV_VAR = "AI_SESSIONS_SUPERVISOR"
DEFAULT_TAIL = 20
SLOW_BUILD_SECONDS = 1.0

LEDGER_TEMPLATE = """# Supervisor ledger

Keep this file current. It survives your own compaction and is re-sent with every brief.

## Objective

<!-- What the target session is trying to achieve, in your words. -->

## Assessment

<!-- Where the target stands now: progress, quality, and whether it is on track. -->

## Open concerns

<!-- Risks, mistakes or unanswered questions you want the user to know about. -->

## Decisions

<!-- Decisions the user or the target has made that you should remember. -->

## Messages sent

<!-- One line per message the user relayed to the target, newest last. -->
"""


class SupervisorError(Exception):
    """A supervisor cannot be created or read; the message is meant for the user."""


def _root(root: Path | None) -> Path:
    return store.SUPERVISORS_DIR if root is None else root


def supports_supervising(tool: str) -> bool:
    """Whether ``tool`` can be the target of a supervisor (its history reads incrementally)."""
    return tool in REGISTRY and not isinstance(REGISTRY.get(tool).read_since, Unsupported)


def can_supervise(tool: str) -> bool:
    """Whether ``tool`` can run a supervisor (it can start a session with chosen arguments)."""
    return tool in REGISTRY and not isinstance(REGISTRY.get(tool).new_session_args, Unsupported)


def default_supervisor_tool(target_tool: str, config: LaunchConfig) -> str:
    """``[supervisor] default_tool`` if configured, else the other harness than the target."""
    try:
        payload = tomllib.loads(config.path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        payload = {}
    section = payload.get("supervisor", {})
    configured = section.get("default_tool") if isinstance(section, dict) else None
    if isinstance(configured, str) and can_supervise(configured):
        return configured
    candidates = [a.name for a in REGISTRY.adapters() if can_supervise(a.name)]
    others = [name for name in candidates if name != target_tool]
    if others:
        return others[0]
    return candidates[0] if candidates else target_tool


def create_supervisor(
    state: Any,
    target: Session,
    tool: str,
    *,
    root: Path | None = None,
    save: bool = True,
) -> str:
    """Register a supervisor for ``target`` and return its record id."""
    if not supports_supervising(target.tool):
        label = REGISTRY.get(target.tool).label if target.tool in REGISTRY else target.tool
        raise SupervisorError(
            f"{label} sessions cannot be supervised: its history cannot be read incrementally yet."
        )
    if not can_supervise(tool):
        raise SupervisorError(f"{tool} cannot run a supervisor session.")
    record_id = str(uuid.uuid4())
    hook = REGISTRY.get(tool).new_session_args
    native_id = str(uuid.uuid4())
    # A harness that accepts a chosen session id puts it in its arguments; the id is then
    # known up front and the hook need not discover it.
    chosen = native_id in hook(session_id=native_id)
    conversation_id = state.conversation_id_for(target, create=True)
    member = state.stable_key(target.key)
    folder = _root(root) / record_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / "ledger.md").write_text(LEDGER_TEMPLATE, encoding="utf-8")
        if target.message_count > 200:
            print("sessions: reading target history…", file=sys.stderr)
        checkpoint = TargetIndex(folder / "index.jsonl").build(
            target.tool, NativeRef(target.session_id, target.storage), member
        )
        save_hook_state(
            HookState(
                record_id=record_id,
                native_session_id=native_id if chosen else "",
                member=member,
                checkpoint=checkpoint,
                brief_due=True,
            ),
            root,
        )
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    state.supervisors[record_id] = {
        "target_conversation": conversation_id,
        "tool": tool,
        "created": time.time(),
        "title": target.title + TITLE_SUFFIX,
        "cwd": target.cwd,
        "archived": False,
    }
    if save:
        state.save()
    return record_id


def _resolve_record(state: Any, ident: str) -> str:
    if ident in state.supervisors:
        return ident
    conversation_ids = {ident}
    for key, cid in state.session_conversations.items():
        if key == ident or key.split(":", 1)[-1] == ident:
            conversation_ids.add(cid)
    for record_id, record in state.supervisors.items():
        if record.get("target_conversation") in conversation_ids and not record.get("archived"):
            return record_id
    raise SupervisorError(f"no supervisor found for {ident!r}")


def _member_ref(state: Any, record: dict[str, Any], hook_state: HookState) -> Any:
    conversation = state.conversations.get(record.get("target_conversation", ""), {})
    member = conversation.get("members", {}).get(hook_state.member)
    if not isinstance(member, dict) or not member.get("session_id") or not member.get("storage"):
        return None
    return member["tool"], NativeRef(member["session_id"], member["storage"])


def target_log(
    state: Any,
    ident: str,
    since: str | None = None,
    tail: int = DEFAULT_TAIL,
    *,
    root: Path | None = None,
) -> str:
    """The target's cleaned exchanges as the supervisor sees them.

    Reads the supervisor's stored index, extended in a scratch copy with anything newer in
    the target, so the stored index and the hook's checkpoint never move.  ``since`` is
    validated but not applied: exchanges carry no timestamps.
    """
    if since:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise SupervisorError(f"--since expects an ISO time, got {since!r}") from None
    record_id = _resolve_record(state, ident)
    hook_state = load_hook_state(record_id, root)
    if hook_state is None:
        raise SupervisorError(f"supervisor {record_id} has no stored state")
    stored = _root(root) / record_id / "index.jsonl"
    exchanges = TargetIndex(stored).exchanges()
    member = _member_ref(state, state.supervisors[record_id], hook_state)
    if member and hook_state.checkpoint is not None:
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "index.jsonl"
            if stored.exists():
                shutil.copyfile(stored, copy)
            scratch_index = TargetIndex(copy)
            try:
                tool, ref = member
                moved = scratch_index.extend(tool, ref, hook_state.member, hook_state.checkpoint)
                if moved is not None:
                    exchanges = scratch_index.exchanges()
            except Exception:  # noqa: BLE001 - fall back to the stored index
                pass
    chosen = exchanges[-tail:] if tail > 0 else exchanges
    lines = [f"--- exchange {ex.seq} ---\n{clean_exchange(ex)}" for ex in chosen]
    if since:
        lines.insert(0, "(--since is best-effort: exchanges carry no timestamps, so all shown)")
    return "\n\n".join(lines)
