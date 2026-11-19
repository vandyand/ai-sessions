"""Claude Code exact transcript/history/index deletion; child trees fail closed."""

import json
import re

from ..compression import open_for_scan, plain_path
from ..deletion import DeletePlan, DeletionError, FileChange, checked_path
from ..registry import REGISTRY
from .deletion_storage import json_lines


def plan_delete(ref):
    from . import claude

    adapter = REGISTRY.get("claude")
    home = adapter.home.absolute()
    ident = ref.session_id
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", ident):
        raise DeletionError("Claude deletion supports only top-level native session IDs")
    authoritative = adapter.resolve(ident)
    if authoritative != ref:
        raise DeletionError("Claude authoritative identity changed")
    paths = claude._claude_session_files(ident)
    if not paths:
        raise DeletionError("Claude transcript is missing")
    files = []
    for path in paths:
        checked_path(home, path)
        if path.parent.parent != home / "projects" or plain_path(path).name != f"{ident}.jsonl":
            raise DeletionError("Claude transcript is outside its exact project layout")
        children = plain_path(path).with_suffix("")
        if children.exists():
            if children.is_symlink() or (
                hasattr(children, "is_junction") and children.is_junction()
            ):
                raise DeletionError("Claude child directory is linked")
            if any(children.iterdir()):
                raise DeletionError(
                    "Claude parent has untracked child resources; refusing to remove subagent logs"
                )
        matched = False
        with open_for_scan(path) as stream:
            for raw in iter(stream.readline, b""):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise DeletionError("invalid Claude transcript record")
                owner = row.get("sessionId")
                if owner and owner != ident:
                    raise DeletionError("Claude transcript native ID mismatch")
                matched |= owner == ident
        if not matched:
            raise DeletionError("Claude transcript has no verifiable native identity")
        files.append(FileChange.capture(home, path))
    for project in sorted({path.parent for path in paths}):
        index = project / "sessions-index.json"
        checked_path(home, index)
        if index.exists():
            original = index.read_bytes()
            document = json.loads(original)
            if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
                raise DeletionError("unrecognized Claude sessions index")
            if not all(isinstance(row, dict) for row in document["entries"]):
                raise DeletionError("malformed Claude session index entries")
            document["entries"] = [
                row for row in document["entries"] if row.get("sessionId") != ident
            ]
            change = FileChange.capture(
                home, index, json.dumps(document, ensure_ascii=False).encode(), "entries"
            )
            if index.read_bytes() != original:
                raise DeletionError("Claude index changed while planning")
            files.append(change)
        else:
            files.append(FileChange.capture(home, index))
    files.append(json_lines(home, home / "history.jsonl", ident, ("sessionId",)))
    return DeletePlan(
        "claude",
        ref,
        adapter.checkpoint(ref),
        tuple(files),
        notes=("Subagent logs are preserved; parents with child resources are refused.",),
    )
