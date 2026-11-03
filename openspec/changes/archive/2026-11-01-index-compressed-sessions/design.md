## Context

Measured 2026-11-01. Codex's database lists every thread at its plain `.jsonl` path, and Codex
resumes a `.jsonl.zst` by restoring the plain file. 2,387 rollouts exist only compressed, about half
of them without a recorded content size, which comes to roughly 13 GB once unpacked; the 50 largest
are half of that. Decompression runs at about 750 MB/s. Python 3.12 has no built-in zstd.

## Goals / Non-Goals

**Goals:** list every compressed session; keep startup fast once indexed; make "compressed" a state
of its own so later milestones can gate on it.

**Non-Goals:** the compress and decompress actions (milestone 2) and the idle sweep (milestone 3).

## Decisions

1. **On-disk form:** `<name>.jsonl.zst` beside the original, so a Codex session we compress is one
   Codex can still resume. *Alternative:* a separate archive folder, rejected because Codex would
   lose the session.
2. **`zstandard` package:** `compression.zstd` needs Python 3.14 and the project supports 3.11.
3. **`storage` names the real file:** a compressed session's `storage` is the `.zst` path, so nothing
   pretends the plain file exists. A stored reference is resolved through `resolve_storage`, and the
   reader gates normalise it to the plain path when that exists.
4. **`Availability.COMPRESSED`:** a compressed transcript is neither readable nor lost.
   `native_session_availability` returns `"compressed"`, and `read_snapshot` and `read_since` refuse
   it. *Alternative:* treating it as unavailable, rejected because `bridge_for` would then see "no
   recorded copy" and build a duplicate bridge.
5. **Scanning:** the discovery scans read the stream through `compression.open_for_scan`. A compressed
   file is one-shot: no prefix sampling and no incremental continue, and a cache entry is valid
   while its size and modification time match.
6. **Cache carry-over:** compression keeps the modification time, so a `.zst` with no cache entry may
   reuse the plain file's entry when `mtime_ns` matches. This avoids rescanning a session we just
   compressed.
7. **Filter is its own axis:** `compression` (`uncompressed`, `compressed`, `both`) beside
   `visibility`, because a session can be hidden and compressed. Key `z`, flag `--compression`.
8. **Marking:** the STATUS column reads `compressed`. Words over another glyph, after the `◉` lesson.

## Risks / Trade-offs

- [First index reads every compressed file] → it runs in the existing background refresh and the
  result is cached. Measured at acceptance.
- [A rollout absent from Codex's database] → 93 compressed rollouts and 3 plain ones have no thread
  row, mostly old `exec` runs. Codex resumes by file, so they are listed from their `session_meta`.
- [Interrupted temp files] → named `.…partial`, which no scan matches.

## Migration Plan

Install the wheel with its dependency. `Session.compressed` defaults to false, so cached catalogs
keep loading through `migratable_fields`. Rollback: the `.zst` files are untouched.

## Open Questions

None.
