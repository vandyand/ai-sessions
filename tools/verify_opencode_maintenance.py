"""Verify a real OpenCode CLI without making a model request.

The gate uses an isolated temporary XDG tree and exercises database binding,
native import, discovery, semantic reread, export, and exact resume argv.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from verify_opencode_real import (
    exported_session,
    invoke,
    require,
    row,
    transcript_text,
    write_source,
)

from ai_sessions.app import UserState, command_for, prepare_launch
from ai_sessions.capabilities import Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.discovery import HarnessContext
from ai_sessions.model import NativeRef
from ai_sessions.registry import REGISTRY


def run_gate(binary: Path, model: str) -> dict[str, object]:
    require(binary.is_file(), f"OpenCode binary does not exist: {binary}")
    version = invoke(binary, ["--version"]).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="ai-sessions-opencode-maintenance-") as directory:
        root = Path(directory)
        work = root / "work"
        work.mkdir()
        xdg = {
            "XDG_CONFIG_HOME": root / "config",
            "XDG_DATA_HOME": root / "data",
            "XDG_CACHE_HOME": root / "cache",
            "XDG_STATE_HOME": root / "state",
        }
        previous = {name: os.environ.get(name) for name in xdg}
        try:
            for name, path in xdg.items():
                os.environ[name] = str(path)
            config_home = xdg["XDG_CONFIG_HOME"] / "opencode"
            config_home.mkdir(parents=True)
            (config_home / "opencode.jsonc").write_text(
                '{\n  "$schema": "https://opencode.ai/config.json"\n}\n', encoding="utf-8"
            )

            command = (str(binary),)
            database = Path(invoke(binary, ["db", "path"]).stdout.strip())
            require(database.is_absolute(), "OpenCode db path was not absolute")
            config = LaunchConfig(
                path=root / "sessions-config.toml",
                providers={
                    "opencode": ProviderProfile(
                        command=list(command), extra={"bridge_model": model}
                    )
                },
            )
            with contextlib.ExitStack() as stack:
                for tool in ("claude", "codex", "opencode"):
                    stack.enter_context(
                        REGISTRY.temporary(replace(REGISTRY.get(tool), home=root / tool))
                    )
                source_ref = write_source("claude", work, "WINDOWS-MAINTENANCE-SOURCE")
                source = row("claude", source_ref, work, "WINDOWS-MAINTENANCE-SOURCE", "opencode")
                state = UserState(root / "state.json")
                copied, note = prepare_launch(source, state=state, config=config)
                require(copied.tool == "opencode" and "Copied" in note, "real import failed")
                copied_ref = NativeRef(copied.session_id, copied.storage)
                require(
                    "WINDOWS-MAINTENANCE-SOURCE" in transcript_text("opencode", copied_ref),
                    "semantic reread lost imported content",
                )

                context = HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": command}
                )
                discover = REGISTRY.get("opencode").discover
                require(not isinstance(discover, Unsupported), "OpenCode discovery is unavailable")
                assert not isinstance(discover, Unsupported)
                discovered = discover(context, use_cache=False)
                require(
                    any(item.session_id == copied.session_id for item in discovered),
                    "real imported session was not discovered",
                )
                exported = exported_session(binary, copied.session_id)
                require(
                    exported.get("info", {}).get("id") == copied.session_id
                    and "WINDOWS-MAINTENANCE-SOURCE" in json.dumps(exported),
                    "real export did not preserve imported identity/content",
                )
                safe = command_for(copied, config)
                require(
                    safe == [str(binary), "--session", copied.session_id],
                    f"safe resume argv is not exact: {safe!r}",
                )
                dangerous = replace(config, mode="dangerous")
                dangerous_argv = command_for(copied, dangerous)
                require(
                    dangerous_argv == [str(binary), "--auto", "--session", copied.session_id],
                    f"dangerous resume argv is not exact: {dangerous_argv!r}",
                )
                return {
                    "opencode_version": version,
                    "database": str(database),
                    "imported_session": copied.session_id,
                    "checks": [
                        "db-path",
                        "real-import",
                        "discovery-semantic-reread",
                        "native-export",
                        "safe-dangerous-exact-resume-argv",
                    ],
                }
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--model", default="opencode/big-pickle")
    arguments = parser.parse_args()
    print(json.dumps(run_gate(arguments.opencode.resolve(), arguments.model), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
