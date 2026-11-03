## Context

`read_snapshot` and `read_since` already refuse compressed references. What remains is the user-facing
actions, and the places that act on a session without reading it: launch-tool choices, recorded
copies, `claude.publish_name` (which appends to the transcript), supervisors and conversation status.

## Goals / Non-Goals

**Goals:** one operation behind `Z` and the command line; no path that writes to a `.zst`; no
duplicate bridges.

**Non-Goals:** the idle sweep (milestone 3).

## Decisions

1. **`transcript_files` capability:** an adapter returns a session's transcript files, main file last.
   Claude Code adds its `subagents/*.jsonl`. `Unsupported` means the harness cannot be compressed.
2. **`retention.py`:** holds `compress_session` and `decompress_session`. Generic, so it names no
   harness. Children first, main last when compressing, so a plain main file means the operation is
   unfinished and a retry is safe. Main first when decompressing, so the session opens even if a
   subagent log fails.
3. **Conversation status:** a new `compressed` list, populated by the same rule as `unavailable`: a
   newest member whose frontier no readable member covers. `conversation_blocker` becomes
   `compressed`, so existing blocked-row logic applies, and Enter and `Z` handle that value first.
   *Alternative:* unblocking the row, rejected because the launch could not be verified.
4. **`bridge_for` raises on a compressed copy,** so a compressed copy is never treated as "no copy".
5. **`can_bridge` is false for a compressed session,** which removes the choices everywhere.
6. **`prepare_launch` refuses compressed sources and targets,** the common point for the browser and
   `--resume`. The browser decompresses before it gets there.
7. **`S` on a compressed supervisor decompresses without asking** (my call): `S` already means open,
   and restoring is lossless.
8. **Rename on a compressed Claude Code session stays local,** using the existing "kept local" note.
9. **`Z` does not confirm** (my call): both directions are lossless and reversible.

## Risks / Trade-offs

- [`Z` on a 300 MB session blocks the browser for seconds] → a message is drawn first.
- [A user runs `claude --resume` directly on a compressed session] → Claude cannot read it. Documented.

## Migration Plan

None beyond milestone 1.

## Open Questions

None.
