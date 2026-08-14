"""Platform-appropriate user data paths with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
IS_WINDOWS = os.name == "nt"


def _windows_dir(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()

if IS_WINDOWS:
    CONFIG_HOME = _windows_dir("APPDATA", HOME / "AppData" / "Roaming")
    CACHE_HOME = _windows_dir("LOCALAPPDATA", HOME / "AppData" / "Local")
else:
    CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")).expanduser()
    CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")).expanduser()

APP_CONFIG_DIR = CONFIG_HOME / "ai-sessions"
APP_CACHE_DIR = CACHE_HOME / "ai-sessions"
STATE_FILE = Path(
    os.environ.get("AI_SESSIONS_STATE_FILE", APP_CONFIG_DIR / "state.json")
).expanduser()
CONFIG_FILE = Path(
    os.environ.get("AI_SESSIONS_CONFIG_FILE", APP_CONFIG_DIR / "config.toml")
).expanduser()
CACHE_FILE = APP_CACHE_DIR / "claude-metadata-v4.json"
CODEX_COUNT_CACHE_FILE = APP_CACHE_DIR / "codex-message-counts-v1.json"
