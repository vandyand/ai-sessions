"""Codex CLI harness adapter."""

from __future__ import annotations

import re

from ..bridge import (
    CODEX_BUDGET_CONTEXT_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_USABLE_FRACTION,
    _codex_change_status,
    _codex_exists,
    read_codex,
    write_codex_session,
)
from ..capabilities import HarnessAdapter
from ..model import BudgetPolicy, SourceKind
from ..paths import CODEX_HOME


def resume_args(
    *,
    session_id: str,
    source: SourceKind,
    resume_id: str,
    parent_id: str,
    native: bool,
) -> list[str]:
    del parent_id
    target = resume_id if native and source is SourceKind.SUBAGENT else session_id
    result = ["resume"]
    if native and (
        source is SourceKind.NON_INTERACTIVE
        or (source is SourceKind.SUBAGENT and resume_id == session_id)
    ):
        result.append("--include-non-interactive")
    result.append(target)
    return result


ADAPTER = HarnessAdapter(
    name="codex",
    label="Codex",
    short_label="Codex",
    order=10,
    home=CODEX_HOME,
    default_command=("codex",),
    dangerous_args=("--dangerously-bypass-approvals-and-sandbox",),
    source_kinds=frozenset(
        (SourceKind.INTERACTIVE, SourceKind.NON_INTERACTIVE, SourceKind.SUBAGENT)
    ),
    id_patterns=(
        re.compile(
            rb"019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    read=read_codex,
    write=write_codex_session,
    locate=_codex_exists,
    change_status=_codex_change_status,
    resume_args=resume_args,
    budget=BudgetPolicy(
        context_tokens=CODEX_BUDGET_CONTEXT_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=(
            "declared Codex unknown-model compatibility floor, 2026-08-23; "
            "current flagship model context is larger"
        ),
    ),
)
