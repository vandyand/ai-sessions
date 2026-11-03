"""Exchanges of a target session, their cleaned text, and the per-supervisor index."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .. import conversion
from ..capabilities import Unsupported
from ..model import Checkpoint, NativeRef, ToolCall, Turn
from ..registry import REGISTRY
from .pastes import typed_text

CHARS_PER_TOKEN = 3.5
CALL_LINE_CHARS = 400
COMPACTION_MARKER = "[target compacted here]"


# Requests are kept longer than one cleaned line so repeated commands still compare equal.
STORED_REQUEST_CHARS = 1_000


def tokens(text: str) -> int:
    """Estimate the token count of ``text``."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Exchange:
    seq: int
    member: str
    prompt: str
    typed: str
    assistant: tuple[str, ...] = ()
    calls: tuple[ToolCall, ...] = ()
    compaction: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "member": self.member,
            "prompt": self.prompt,
            "typed": self.typed,
            "assistant": list(self.assistant),
            "calls": [
                {"name": c.name, "request": c.request, "result": c.result, "failed": c.failed}
                for c in self.calls
            ],
            "compaction": self.compaction,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Exchange:
        return cls(
            seq=int(data["seq"]),
            member=str(data["member"]),
            prompt=str(data["prompt"]),
            typed=str(data["typed"]),
            assistant=tuple(str(t) for t in data.get("assistant", ())),
            calls=tuple(
                ToolCall(
                    str(c["name"]),
                    str(c.get("request", "")),
                    str(c.get("result", "")),
                    bool(c.get("failed", False)),
                )
                for c in data.get("calls", ())
            ),
            compaction=bool(data.get("compaction", False)),
        )


def exchanges_from_turns(
    turns: list[Turn],
    *,
    member: str,
    start_seq: int,
    tool: str,
    pastes: list[str],
    injected: Callable[[str], bool] | None = None,
) -> list[Exchange]:
    """Group turns into exchanges; each user turn with text starts a new one.

    Turns before the first user turn form a seq-0 preamble exchange.  ``tool`` is
    accepted for symmetry with the readers; paste detection needs only ``pastes``.
    """
    del tool
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seq = start_seq
    for turn in turns:
        if turn.role == "user" and turn.text and injected is not None and injected(turn.text):
            continue
        if not turn.compaction and turn.role == "user" and turn.text:
            current = {"seq": seq, "prompt": turn.text, "assistant": [], "calls": [], "c": False}
            groups.append(current)
            seq += 1
            continue
        if current is None:
            current = {"seq": 0, "prompt": "", "assistant": [], "calls": [], "c": False}
            groups.append(current)
        if turn.compaction:
            current["c"] = True
        elif turn.role != "user" and turn.text:
            current["assistant"].append(turn.text)
        current["calls"].extend(_stored_call(call) for call in turn.calls)
    return [
        Exchange(
            seq=g["seq"],
            member=member,
            prompt=g["prompt"],
            typed=typed_text(g["prompt"], pastes=pastes) if g["prompt"] else "",
            assistant=tuple(g["assistant"]),
            calls=tuple(g["calls"]),
            compaction=g["c"],
        )
        for g in groups
    ]


def _stored_call(call: ToolCall) -> ToolCall:
    """Keep only as much of a call as cleaning and the digest can use.

    Tool output is most of a working transcript; storing it whole would make the index
    nearly as large as the target itself.
    """
    return replace(
        call,
        request=clip(call.request, STORED_REQUEST_CHARS),
        result=clip(call.result, CALL_LINE_CHARS),
    )


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def call_line(call: ToolCall, *, limit: int = CALL_LINE_CHARS) -> str:
    """One clipped ``⟦Tool⟧ request → result`` line, ``✗ `` first when the call failed."""
    line = f"⟦{call.name}⟧"
    if call.request:
        line += f" {call.request}"
    if call.result:
        line += f" → {call.result}"
    line = " ".join(line.split())
    if call.failed:
        line = "✗ " + line
    return clip(line, limit)


def clean_exchange(ex: Exchange) -> str:
    """The exchange as the bridge would summarise it."""
    parts: list[str] = []
    if ex.prompt:
        parts.append(ex.prompt)
    parts.extend(ex.assistant)
    parts.extend(call_line(call) for call in ex.calls)
    if ex.compaction:
        parts.append(COMPACTION_MARKER)
    return "\n\n".join(parts)


def _pastes(tool: str, ref: NativeRef) -> list[str]:
    hook = REGISTRY.get(tool).pasted_contents
    if isinstance(hook, Unsupported):
        return []
    try:
        return list(hook(ref))
    except (OSError, ValueError):
        return []


def _injected(tool: str) -> Callable[[str], bool] | None:
    hook = REGISTRY.get(tool).injected_prompt
    return None if isinstance(hook, Unsupported) else hook


def _merge(base: Exchange, extra: Exchange) -> Exchange:
    return replace(
        base,
        assistant=base.assistant + extra.assistant,
        calls=base.calls + extra.calls,
        compaction=base.compaction or extra.compaction,
    )


class TargetIndex:
    """Append-only JSON-lines store of a target's exchanges."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # (stat key, raw lines, parsed exchanges): one hook run reads the index several times,
        # and a long target's index is tens of megabytes.
        self._cache: tuple[tuple[int, int], list[str], list[Exchange]] | None = None

    def _stat_key(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _load(self) -> tuple[list[str], list[Exchange]]:
        key = self._stat_key()
        if key is None:
            self._cache = None
            return [], []
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1], self._cache[2]
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        parsed = [Exchange.from_json(json.loads(line)) for line in lines]
        self._cache = (key, lines, parsed)
        return lines, parsed

    def exchanges(self) -> list[Exchange]:
        return list(self._load()[1])

    def last_seq(self) -> int:
        stored = self._load()[1]
        return stored[-1].seq if stored else 0

    def append(self, new: list[Exchange]) -> None:
        if not new:
            return
        lines, stored = self._load()
        lines, stored = list(lines), list(stored)
        if stored and new[0].seq == stored[-1].seq:
            lines.pop()
            stored.pop()
        lines.extend(json.dumps(ex.to_json(), ensure_ascii=False) for ex in new)
        stored.extend(new)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(line + "\n" for line in lines))
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        key = self._stat_key()
        self._cache = (key, lines, stored) if key is not None else None

    def build(self, tool: str, ref: NativeRef, member: str) -> Checkpoint:
        snapshot = conversion.read_snapshot(tool, ref, latest_window=False)
        built = exchanges_from_turns(
            snapshot.transcript.turns,
            member=member,
            start_seq=1,
            tool=tool,
            pastes=_pastes(tool, ref),
            injected=_injected(tool),
        )
        self.append(built)
        return snapshot.checkpoint

    def extend(
        self, tool: str, ref: NativeRef, member: str, checkpoint: Checkpoint
    ) -> Checkpoint | None:
        snapshot = conversion.read_since(tool, ref, checkpoint)
        if snapshot is None:
            return None
        stored = self._load()[1]
        last = stored[-1] if stored else None
        new = exchanges_from_turns(
            snapshot.transcript.turns,
            member=member,
            start_seq=(last.seq if last else 0) + 1,
            tool=tool,
            pastes=_pastes(tool, ref),
            injected=_injected(tool),
        )
        if new and new[0].seq == 0 and new[0].prompt == "" and last is not None:
            new = [_merge(last, new[0])] + new[1:]
        self.append(new)
        return snapshot.checkpoint
