"""Keep files the tests write out of the real state folder.

Messaging and the Codex TUI override are read when their modules import.
Point both at a disabled temp folder before that import so a developer
machine with messaging turned on, or a verified Codex renderer installed,
still runs the stock-command assertions.
"""

import os
import shutil
import tempfile
from pathlib import Path

_ISOLATED = Path(tempfile.mkdtemp(prefix="ai-sessions-test-isolate-"))
_MESSAGING = _ISOLATED / "messaging"
_MESSAGING.mkdir(parents=True, exist_ok=True)
(_MESSAGING / "settings.json").write_text('{"enabled": false}\n', encoding="utf-8")
(_ISOLATED / "tui.json").write_text('{"schema": 1, "enabled": false}\n', encoding="utf-8")
os.environ["AI_SESSIONS_MESSAGING_DIR"] = str(_MESSAGING)
os.environ["AI_SESSIONS_CODEX_TUI_MANIFEST"] = str(_ISOLATED / "tui.json")
os.environ.setdefault("AI_SESSIONS_CONFIG_FILE", str(_ISOLATED / "config.toml"))
os.environ.setdefault("AI_SESSIONS_STATE_FILE", str(_ISOLATED / "state.json"))
os.environ.setdefault("AI_SESSIONS_CACHE_DIR", str(_ISOLATED / "cache"))

from ai_sessions import jsonl_tracking  # noqa: E402

_RECORDS = Path(tempfile.mkdtemp(prefix="ai-sessions-test-records-"))
jsonl_tracking.RECORDS_DIR = _RECORDS


def pytest_unconfigure(config):
    shutil.rmtree(_RECORDS, ignore_errors=True)
    shutil.rmtree(_ISOLATED, ignore_errors=True)
