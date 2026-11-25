"""Minimal stdio MCP: bounded read/search of one explicit project root, no shell."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ..fastjson import write_atomic

BLOCKED = {
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".codex",
    ".claude",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "auth.json",
    "settings.json",
}
SECRET_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".sqlite", ".sqlite3", ".db", ".jsonl"}


class Reader:
    def __init__(self, root, audit=None):
        self.root = Path(root).resolve(strict=True)
        if (
            not self.root.is_dir()
            or self.root == Path.home().resolve()
            or self.root == Path(self.root.anchor)
        ):
            raise ValueError("Choose a project directory, not a home or drive root")
        self.audit = Path(audit) if audit else None
        self.entries = []
        self.calls = 0

    def path(self, relative):
        raw = Path(relative)
        path = (self.root / raw).resolve(strict=True)
        if raw.is_absolute() or not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Path is outside the project")
        parts = path.relative_to(self.root).parts
        for part in parts:
            value = part.casefold()
            if (
                value in BLOCKED
                or value.startswith(".env")
                or any(x in value for x in ("credential", "secret", "token", "password"))
            ):
                raise ValueError("Private or generated files are excluded")
        if path.suffix.casefold() in SECRET_SUFFIXES or not path.is_file():
            raise ValueError("Only project text files can be read")
        return path

    def read(self, relative, start=1):
        if type(start) is not int or not 1 <= start <= 100000:
            raise ValueError("start_line must be between 1 and 100000")
        path = self.path(relative)
        if path.stat().st_size > 512 * 1024:
            raise ValueError("File exceeds the 512 KiB read limit")
        with path.open("rb") as stream:
            data = stream.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise ValueError("File grew past the read limit")
        if b"\0" in data:
            raise ValueError("Binary files are excluded")
        text = data.decode("utf-8", errors="strict")
        excerpt = "\n".join(text.splitlines()[start - 1 : start + 79])[:2200]
        if not excerpt.strip():
            raise ValueError("No text at this line")
        if len(self.entries) >= 8:
            raise ValueError("The eight-excerpt context budget is exhausted")
        ident = f"code{len(self.entries) + 1}"
        entry = dict(
            id=ident,
            title=f"Project file: {path.relative_to(self.root).as_posix()} · line {start}",
            tool="project",
            session_id="project",
            home=str(self.root),
            storage=str(path),
            identity=hashlib.sha256(str(path).encode()).hexdigest(),
            start=start,
            end=start + len(excerpt.splitlines()),
            digest=hashlib.sha256(excerpt.encode()).hexdigest(),
            text=excerpt,
            context_only=True,
        )
        self.entries.append(entry)
        if self.audit:
            write_atomic(self.audit, self.entries)
        return entry

    def search(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 160:
            raise ValueError("Supply a short literal search phrase")
        deadline = time.monotonic() + 3
        results, scanned, total = [], 0, 0
        directories = 0
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            directories += 1
            if directories > 3000 or time.monotonic() > deadline:
                return dict(matches=results, partial=True)
            if not Path(folder).resolve().is_relative_to(self.root):
                dirs.clear()
                continue
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and d.casefold() not in BLOCKED
                and not (Path(folder) / d).is_symlink()
            )
            for name in sorted(files):
                scanned += 1
                if scanned > 3000 or total > 8 * 1024 * 1024 or time.monotonic() > deadline:
                    return dict(matches=results, partial=True)
                rel = (Path(folder) / name).relative_to(self.root).as_posix()
                try:
                    path = self.path(rel)
                    size = path.stat().st_size
                    if size > 128 * 1024:
                        continue
                    with path.open("rb") as stream:
                        data = stream.read(128 * 1024 + 1)
                    if len(data) > 128 * 1024:
                        continue
                    total += len(data)
                    if b"\0" in data:
                        continue
                    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
                        if query.casefold() in line.casefold():
                            results.append(dict(path=rel, line=number))
                            if len(results) >= 30:
                                return dict(matches=results, partial=True)
                except (OSError, UnicodeError, ValueError):
                    continue
        return dict(matches=results, partial=False)

    def call(self, name, args):
        self.calls += 1
        if self.calls > 20:
            raise ValueError("The twenty-call search budget is exhausted")
        if name == "search_project":
            return self.search(args["query"])
        if name == "read_project_file":
            return self.read(args["path"], args.get("start_line", 1))
        raise ValueError("Unknown read-only tool")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--audit", required=True)
    args = parser.parse_args()
    reader = Reader(args.root, args.audit)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = dict(
                    protocolVersion=request["params"].get("protocolVersion", "2024-11-05"),
                    capabilities={"tools": {}},
                    serverInfo={"name": "desk_context", "version": "1"},
                )
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = dict(
                    tools=[
                        dict(
                            name="search_project",
                            description="Find a literal phrase in bounded project text files. Returns paths/line numbers; read a match to cite it.",
                            inputSchema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        ),
                        dict(
                            name="read_project_file",
                            description="Read a bounded project-file excerpt. Returns an authoritative citation ID and text.",
                            inputSchema={
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "start_line": {"type": "integer"},
                                },
                                "required": ["path"],
                                "additionalProperties": False,
                            },
                        ),
                    ]
                )
            elif method == "tools/call":
                try:
                    value = reader.call(
                        request["params"]["name"], request["params"].get("arguments", {})
                    )
                    result = dict(
                        content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))]
                    )
                except (OSError, ValueError, KeyError, UnicodeError):
                    result = dict(
                        isError=True,
                        content=[
                            dict(
                                type="text",
                                text="Read/search unavailable or outside the project/budget. Use available evidence or request missing context.",
                            )
                        ],
                    )
            else:
                print(
                    json.dumps(
                        dict(
                            jsonrpc="2.0",
                            id=request["id"],
                            error=dict(code=-32601, message="Method not found"),
                        )
                    ),
                    flush=True,
                )
                continue
            print(json.dumps(dict(jsonrpc="2.0", id=request["id"], result=result)), flush=True)
        except (ValueError, KeyError, TypeError):
            continue


if __name__ == "__main__":
    main()
