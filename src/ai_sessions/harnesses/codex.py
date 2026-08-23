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

ADAPTER = HarnessAdapter(
    name="codex",
    label="Codex",
    short_label="Codex",
    order=10,
    home=CODEX_HOME,
    default_command=("codex",),
    dangerous_args=("--dangerously-bypass-approvals-and-sandbox",),
    source_kinds=frozenset(SourceKind),
    id_patterns=(
        re.compile(
            rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            re.I,
        ),
    ),
    read=read_codex,
    write=write_codex_session,
    locate=_codex_exists,
    change_status=_codex_change_status,
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
