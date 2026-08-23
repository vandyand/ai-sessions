"""Claude Code harness adapter."""

from __future__ import annotations

import re

from ..bridge import (
    CLAUDE_BUDGET_CONTEXT_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_USABLE_FRACTION,
    _claude_change_status,
    _claude_exists,
    read_claude,
    write_claude_session,
)
from ..capabilities import HarnessAdapter
from ..model import BudgetPolicy, SourceKind
from ..paths import CLAUDE_HOME

ADAPTER = HarnessAdapter(
    name="claude",
    label="Claude Code",
    short_label="Claude",
    order=20,
    home=CLAUDE_HOME,
    default_command=("claude",),
    dangerous_args=("--dangerously-skip-permissions",),
    source_kinds=frozenset(SourceKind),
    id_patterns=(
        re.compile(
            rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    read=read_claude,
    write=write_claude_session,
    locate=_claude_exists,
    change_status=_claude_change_status,
    budget=BudgetPolicy(
        context_tokens=CLAUDE_BUDGET_CONTEXT_TOKENS,
        usable_fraction=DEFAULT_USABLE_FRACTION,
        chars_per_token=DEFAULT_CHARS_PER_TOKEN,
        source=(
            "Claude 200k unknown-model floor from official context-window docs, "
            "2026-08-23; Opus 5 context is larger"
        ),
    ),
)
