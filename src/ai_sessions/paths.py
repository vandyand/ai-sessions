"""Platform-appropriate user data paths with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
IS_WINDOWS = os.name == "nt"


def env_path(variable: str, fallback: Path) -> Path:
    """Resolve a path override, treating an empty variable as unset."""
    value = os.environ.get(variable)
    return (Path(value) if value else fallback).expanduser()


if IS_WINDOWS:
    CONFIG_HOME = env_path("APPDATA", HOME / "AppData" / "Roaming")
    CACHE_HOME = env_path("LOCALAPPDATA", HOME / "AppData" / "Local")
else:
    CONFIG_HOME = env_path("XDG_CONFIG_HOME", HOME / ".config")
    CACHE_HOME = env_path("XDG_CACHE_HOME", HOME / ".cache")

APP_CONFIG_DIR = CONFIG_HOME / "ai-sessions"
APP_CACHE_DIR = CACHE_HOME / "ai-sessions"
STATE_FILE = env_path("AI_SESSIONS_STATE_FILE", APP_CONFIG_DIR / "state.json")
CONFIG_FILE = env_path("AI_SESSIONS_CONFIG_FILE", APP_CONFIG_DIR / "config.toml")
