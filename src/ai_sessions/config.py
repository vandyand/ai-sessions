"""Launch profiles for provider CLIs.

The package default is deliberately safe: provider permission behavior comes
from the provider's own configuration.  Bypass flags require an explicit mode.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .model import LEGACY_DEFAULT_MAX_CHARS
from .paths import CONFIG_FILE
from .registry import REGISTRY

LAUNCH_MODES = ("safe", "dangerous", "custom")


@dataclass(slots=True)
class ProviderProfile:
    command: list[str] | None = None
    custom_args: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LaunchConfig:
    path: Path = CONFIG_FILE
    mode: str = "safe"
    claude_command: list[str] = field(default_factory=lambda: ["claude"])
    codex_command: list[str] = field(default_factory=lambda: ["codex"])
    custom_claude_args: list[str] = field(default_factory=list)
    custom_codex_args: list[str] = field(default_factory=list)
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    # How much conversation a bridged copy carries into the other harness.
    # It is replayed into the target's context window, so the ceiling is a
    # context budget rather than a storage one.
    bridge_max_tokens: int | None = None
    bridge_max_chars: int | None = None
    bridge_max_chars_migrated: bool = False
    # Whether tool calls cross over as inline summaries.  They are usually
    # the most valuable context in an engineering session, and also the
    # bulkiest, so they can be dropped for a conversation-only copy.
    bridge_tool_calls: bool = True
    # Whether a compacted session is carried from its most recent summary
    # rather than replayed from the beginning.  The summary is what the
    # source session itself resumes from.
    bridge_latest_window: bool = True

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "LaunchConfig":
        result = cls(path=path)
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return result
        version = payload.get("version", 1)
        schema = version if isinstance(version, int) and not isinstance(version, bool) else 1
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
        provider_tables = launch.get("providers", {})
        if schema >= 3 and isinstance(provider_tables, dict):
            for name, profile in provider_tables.items():
                if not isinstance(name, str) or not name or not isinstance(profile, dict):
                    continue
                command = _optional_string_list(profile.get("command"))
                custom_args = _optional_string_list(profile.get("custom_args"))
                extra = dict(profile)
                if command is not None:
                    extra.pop("command", None)
                if custom_args is not None:
                    extra.pop("custom_args", None)
                result.providers[name] = ProviderProfile(command, custom_args or [], extra)
        bridge = payload.get("bridge", {})
        if isinstance(bridge, dict):
            max_tokens = bridge.get("max_tokens")
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
                result.bridge_max_tokens = max_tokens
            max_chars = bridge.get("max_chars")
            if isinstance(max_chars, int) and not isinstance(max_chars, bool) and max_chars > 0:
                if (
                    result.bridge_max_tokens is None
                    and schema <= 1
                    and max_chars == LEGACY_DEFAULT_MAX_CHARS
                ):
                    result.bridge_max_chars_migrated = True
                else:
                    result.bridge_max_chars = max_chars
            tool_calls = bridge.get("tool_calls")
            if isinstance(tool_calls, bool):
                result.bridge_tool_calls = tool_calls
            latest_window = bridge.get("latest_window")
            if isinstance(latest_window, bool):
                result.bridge_latest_window = latest_window
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
        if provider is not None:
            return not self._profile(provider).custom_args
        names = set(REGISTRY.names()) | set(self.providers)
        return not any(self._profile(name).custom_args for name in names)

    def _profile(self, provider: str) -> ProviderProfile:
        # These literal fields are the schema-2 rollback contract. New harnesses use
        # keyed profiles, while built-in callers retain constructor compatibility.
        if provider == "claude":
            return ProviderProfile(list(self.claude_command), list(self.custom_claude_args))
        if provider == "codex":
            return ProviderProfile(list(self.codex_command), list(self.custom_codex_args))
        if provider in self.providers:
            profile = self.providers[provider]
            command = list(profile.command) if profile.command is not None else None
            return ProviderProfile(command, list(profile.custom_args), dict(profile.extra))
        try:
            adapter = REGISTRY.get(provider)
        except KeyError:
            raise ValueError(f"unknown harness: {provider}") from None
        return ProviderProfile(list(adapter.default_command))

    def provider_prefix(self, provider: str) -> list[str]:
        try:
            adapter = REGISTRY.get(provider)
        except KeyError:
            raise ValueError(f"unknown harness: {provider}") from None
        profile = self._profile(provider)
        result = list(profile.command or adapter.default_command)
        if self.mode == "dangerous":
            result.extend(adapter.dangerous_args)
        elif self.mode == "custom":
            result.extend(profile.custom_args)
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
        budget = ""
        if self.bridge_max_tokens is not None:
            budget = f"max_tokens = {self.bridge_max_tokens}\n"
        elif self.bridge_max_chars is not None:
            budget = f"max_chars = {self.bridge_max_chars}\n"
        provider_tables = ""
        for name in sorted(self.providers):
            profile = self.providers[name]
            provider_tables += f"\n[launch.providers.{_toml_key(name)}]\n"
            if profile.command is not None:
                provider_tables += f"command = {_toml_array(profile.command)}\n"
            provider_tables += f"custom_args = {_toml_array(profile.custom_args)}\n"
            for key, value in profile.extra.items():
                provider_tables += f"{_toml_key(key)} = {_toml_value(value)}\n"
        version = 3 if self.providers else 2
        provider_separator = "\n" if provider_tables else ""
        return (
            "# ai-sessions configuration\n"
            f"version = {version}\n\n"
            "[launch]\n"
            f"mode = {_toml_string(self.mode)}\n"
            f"claude_command = {_toml_array(self.claude_command)}\n"
            f"codex_command = {_toml_array(self.codex_command)}\n\n"
            "[launch.custom]\n"
            f"claude_args = {_toml_array(self.custom_claude_args)}\n"
            f"codex_args = {_toml_array(self.custom_codex_args)}\n\n"
            f"{provider_tables}"
            f"{provider_separator}"
            "[bridge]\n"
            f"{budget}"
            f"tool_calls = {str(self.bridge_tool_calls).lower()}\n"
            f"latest_window = {str(self.bridge_latest_window).lower()}\n"
        )


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        return list(fallback)
    return list(value)


def _optional_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return list(value)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_key(value: str) -> str:
    return value if value.replace("_", "").replace("-", "").isalnum() else _toml_string(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = ", ".join(
            f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items()
        )
        return "{" + fields + "}"
    return _toml_string(str(value))
