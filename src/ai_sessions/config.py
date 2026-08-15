"""Launch profiles for provider CLIs.

The package default is deliberately safe: provider permission behavior comes
from the provider's own configuration.  Bypass flags require an explicit mode.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import CONFIG_FILE

LAUNCH_MODES = ("safe", "dangerous", "custom")


@dataclass(slots=True)
class LaunchConfig:
    path: Path = CONFIG_FILE
    mode: str = "safe"
    claude_command: list[str] = field(default_factory=lambda: ["claude"])
    codex_command: list[str] = field(default_factory=lambda: ["codex"])
    custom_claude_args: list[str] = field(default_factory=list)
    custom_codex_args: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "LaunchConfig":
        result = cls(path=path)
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return result
        launch = payload.get("launch", {})
        if not isinstance(launch, dict):
            return result
        mode = launch.get("mode")
        if mode in LAUNCH_MODES:
            result.mode = str(mode)
        result.claude_command = _string_list(launch.get("claude_command"), ["claude"])
        result.codex_command = _string_list(launch.get("codex_command"), ["codex"])
        custom = launch.get("custom", {})
        if isinstance(custom, dict):
            result.custom_claude_args = _string_list(custom.get("claude_args"), [])
            result.custom_codex_args = _string_list(custom.get("codex_args"), [])
        return result

    def set_mode(self, mode: str) -> None:
        if mode not in LAUNCH_MODES:
            raise ValueError(f"Unknown launch mode: {mode}")
        self.mode = mode
        self.save()

    def cycle_mode(self) -> str:
        self.set_mode(LAUNCH_MODES[(LAUNCH_MODES.index(self.mode) + 1) % len(LAUNCH_MODES)])
        return self.mode

    def custom_args_missing(self, provider: str | None = None) -> bool:
        """True when custom mode contributes no arguments for a provider.

        Custom mode with empty argument lists is indistinguishable from safe
        mode at the command line, so callers surface a notice rather than let
        the setting look effective when it is not.  Without a provider, this
        reports whether both custom profiles are empty.
        """
        if self.mode != "custom":
            return False
        if provider == "claude":
            return not self.custom_claude_args
        if provider == "codex":
            return not self.custom_codex_args
        return not (self.custom_claude_args or self.custom_codex_args)

    def provider_prefix(self, provider: str) -> list[str]:
        command = self.claude_command if provider == "claude" else self.codex_command
        result = list(command)
        if self.mode == "dangerous":
            result.append(
                "--dangerously-skip-permissions"
                if provider == "claude"
                else "--dangerously-bypass-approvals-and-sandbox"
            )
        elif self.mode == "custom":
            result.extend(
                self.custom_claude_args if provider == "claude" else self.custom_codex_args
            )
        return result

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}")
        temporary.write_text(self.as_toml(), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def as_toml(self) -> str:
        return (
            "# ai-sessions configuration\n"
            "version = 1\n\n"
            "[launch]\n"
            f"mode = {_toml_string(self.mode)}\n"
            f"claude_command = {_toml_array(self.claude_command)}\n"
            f"codex_command = {_toml_array(self.codex_command)}\n\n"
            "[launch.custom]\n"
            f"claude_args = {_toml_array(self.custom_claude_args)}\n"
            f"codex_args = {_toml_array(self.custom_codex_args)}\n"
        )


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        return list(fallback)
    return list(value)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
