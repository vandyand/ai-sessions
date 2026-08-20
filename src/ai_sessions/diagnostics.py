"""Load failures that are worth telling the user about.

A provider store that cannot be read is not the same thing as a provider
with no sessions, but both used to render as an empty list: a locked or
unreadable database silently became "you have no Codex sessions", and the
one detail that would have explained it was discarded.

Loaders record what went wrong here and each interface decides how to show
it -- stderr for the command line, the status line for the browser.
"""

from __future__ import annotations

_WARNINGS: list[str] = []


def clear_warnings() -> None:
    """Start a fresh load; called before sessions are gathered."""
    _WARNINGS.clear()


def record_warning(message: str) -> None:
    """Note a failure, keeping the first of each distinct kind."""
    if message not in _WARNINGS:
        _WARNINGS.append(message)


def warnings() -> list[str]:
    return list(_WARNINGS)
