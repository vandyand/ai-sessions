"""Verified interactive Codex renderer; never used by native RPC resolution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def default_manifest() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "ai-sessions" / "codex-tui" / "active.json"


def manifest_path() -> Path:
    selected = os.environ.get("AI_SESSIONS_CODEX_TUI_MANIFEST")
    return Path(selected).expanduser().resolve() if selected else default_manifest().resolve()


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _metadata(digest: str, version: str) -> None:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Renderer digest must be a lowercase SHA-256 value.")
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version
    ):
        raise ValueError("Renderer version must be an explicit release version.")


def verify(path: Path, digest: str, version: str) -> None:
    _metadata(digest, version)
    try:
        if _digest(path) != digest:
            raise ValueError("Codex renderer digest mismatch; reinstall the verified artifact.")
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "Codex renderer is missing or unavailable; reinstall it or disable the override."
        ) from error
    if result.returncode or result.stdout.strip() != f"codex-cli {version}":
        raise ValueError("Codex renderer version mismatch; reinstall the verified artifact.")


def command(argv: list[str]) -> list[str]:
    """Replace only the TUI executable, preserving configured flags and argv length."""
    path = manifest_path()
    if not path.is_file() and not os.environ.get("AI_SESSIONS_CODEX_TUI_MANIFEST"):
        return list(argv)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError
        if data.get("enabled") is False:
            return list(argv)
        if data.get("enabled") is not True:
            raise ValueError
        executable = Path(data["executable"])
        if not executable.is_absolute() or executable.name.casefold() != "codex.exe":
            raise ValueError
        verify(executable, data["sha256"], data["version"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "Codex TUI renderer override is invalid: "
            + str(error)
            + " Use python -m ai_sessions.harnesses.codex_tui disable to restore stock Codex."
        ) from error
    return [str(executable), *argv[1:]]


def _publish(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".active-", suffix=".json", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(source: Path, digest: str, version: str) -> Path:
    """Stage and verify before activation; leave any working installation intact."""
    _metadata(digest, version)
    manifest = manifest_path()
    directory = manifest.parent / f"{version}-{digest}"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "codex.exe"
    fd, name = tempfile.mkstemp(prefix=".renderer-", suffix=".exe", dir=directory)
    os.close(fd)
    stage = Path(name)
    try:
        shutil.copyfile(source, stage)
        # Verify the supplied artifact even on reinstall; never trust its filename.
        verify(stage, digest, version)
        if destination.is_file() and _digest(destination) == digest:
            verify(destination, digest, version)
        else:
            os.replace(stage, destination)
        _publish(
            manifest,
            dict(
                schema=1, enabled=True, executable=str(destination), sha256=digest, version=version
            ),
        )
    finally:
        stage.unlink(missing_ok=True)
    return destination


def disable() -> None:
    _publish(manifest_path(), dict(schema=1, enabled=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    installing = actions.add_parser("install")
    installing.add_argument("source", type=Path)
    installing.add_argument("--sha256", required=True)
    installing.add_argument("--version", required=True)
    actions.add_parser("disable")
    actions.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            executable = install(args.source, args.sha256, args.version)
            print(f"Verified Codex TUI renderer installed: {executable}")
            print(
                "First activation: exit Codex normally, then run ses --reopen from a fresh shell "
                "using the updated sessions package. Existing Python launchers do not reload; "
                "subsequent sesres restarts use the new launcher."
            )
        elif args.action == "disable":
            disable()
            print(
                "Codex TUI renderer override disabled; interactive launches use stock configuration."
            )
        else:
            selected = command(["codex"])
            print(
                json.dumps(
                    dict(
                        manifest=str(manifest_path()),
                        executable=selected[0],
                        renderer=selected != ["codex"],
                    )
                )
            )
        return 0
    except (OSError, ValueError) as error:
        print(f"Codex TUI renderer: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
