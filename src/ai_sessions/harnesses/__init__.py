"""Built-in harness adapters.

Adapters are installed explicitly from :mod:`ai_sessions` so the registry remains
process-local, ordered, and open to third-party or test adapters.
"""

from __future__ import annotations

from ..registry import REGISTRY


def install() -> None:
    """Install every built-in adapter exactly once."""
    from .claude import ADAPTER as CLAUDE
    from .codex import ADAPTER as CODEX
    from .opencode import ADAPTER as OPENCODE

    for adapter in (CODEX, CLAUDE, OPENCODE):
        if adapter.name not in REGISTRY:
            REGISTRY.register(adapter)
        elif REGISTRY.get(adapter.name) is not adapter:
            raise ValueError(f"built-in harness name is already registered: {adapter.name}")
