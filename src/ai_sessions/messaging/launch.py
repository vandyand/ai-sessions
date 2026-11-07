"""Connect future Codex terminal launches to the native shared owner."""

from __future__ import annotations

from . import install


def arguments(command: list[str], tool: str, *, prefix_length: int = 1) -> list[str]:
    from ..registry import REGISTRY

    hooks = REGISTRY.get(tool).messaging
    if not hooks or not hooks.remote_arg or not install.settings().get("enabled"):
        return command
    if "--no-daemon" in command:
        raise ValueError("messaging requires a shared Codex server; remove --no-daemon")
    if any(arg == "--remote" or arg.startswith("--remote=") for arg in command):
        return command
    return [*command[:prefix_length], "--remote", hooks.remote_arg, *command[prefix_length:]]


def prepare(command: list[str], tool: str) -> None:
    from ..registry import REGISTRY

    adapter = REGISTRY.get(tool)
    hooks = adapter.messaging
    if (
        hooks
        and hooks.prepare
        and hooks.remote_arg in command
        and install.settings().get("enabled")
    ):
        runtime = hooks.prepare(adapter.home)
        if runtime.get("status") not in ("running", "started"):
            raise ValueError(
                "Codex messaging server is unavailable; run sessions messaging install"
            )
