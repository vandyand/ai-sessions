"""Capture one offline resolution's proposed lock change, then restore the lock.

Diagnostic only: never builds or installs anything, and never approves an updated
dependency graph. The workflow's compilation commands continue to require --locked.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

PINNED_COMMIT = "29eb79e9866b3262a777fdf8195c1b9e63684c91"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _packages(data):
    result = {}
    for package in tomllib.loads(data.decode("utf-8"))["package"]:
        key = "|".join(package.get(field, "") for field in ("name", "version", "source"))
        result[key] = package
    return result


def capture(source: Path, output: Path, *, runner=subprocess.run):
    source, output = source.resolve(), output.resolve()
    lock = source / "codex-rs" / "Cargo.lock"
    before = lock.read_bytes()
    output.mkdir(parents=True, exist_ok=True)
    (output / "Cargo.lock.before").write_bytes(before)
    command = ["cargo", "metadata", "--offline", "--format-version", "1"]
    summary = {
        "pinned_commit": PINNED_COMMIT,
        "command": command,
        "before_sha256": _sha(before),
        "purpose": "diagnostic only; unreviewed graph is never built",
    }
    try:
        with (
            (output / "metadata.stdout.json").open("wb") as stdout,
            (output / "metadata.stderr.log").open("wb") as stderr,
        ):
            try:
                result = runner(
                    command,
                    cwd=source / "codex-rs",
                    stdout=stdout,
                    stderr=stderr,
                    timeout=600,
                    check=False,
                )
                summary["returncode"] = result.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                summary["probe_error"] = type(exc).__name__
        after = lock.read_bytes() if lock.exists() else b""
        (output / "Cargo.lock.proposed").write_bytes(after)
        diff = "".join(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile="a/codex-rs/Cargo.lock",
                tofile="b/codex-rs/Cargo.lock",
            )
        )
        (output / "Cargo.lock.diff").write_text(diff, encoding="utf-8", newline="\n")
        summary.update(proposed_sha256=_sha(after), changed=before != after)
        try:
            old, new = _packages(before), _packages(after)
            changes = {
                "added": sorted(new.keys() - old.keys()),
                "removed": sorted(old.keys() - new.keys()),
                "modified": {
                    key: {"before": old[key], "after": new[key]}
                    for key in old.keys() & new.keys()
                    if old[key] != new[key]
                },
            }
            (output / "package-changes.json").write_text(
                json.dumps(changes, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
            )
        except (KeyError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            summary["inventory_error"] = type(exc).__name__
    finally:
        # Restoration is byte-exact even if resolution partially writes then fails.
        lock.write_bytes(before)
        summary["restored_original"] = lock.read_bytes() == before
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
        )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != PINNED_COMMIT:
        raise SystemExit("Refusing lock diagnostics against an unpinned source commit")
    summary = capture(args.source, args.output)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
