## Why

`sessions` lists only plain `.jsonl` transcripts, so the 2,387 Codex rollouts compressed to
`.jsonl.zst` are invisible, including Andrew's StarCraft II session. Andrew also wants idle sessions
compressed instead of deleted, which needs compressed sessions to stay listed. This is the first of
three milestones in `specs/retention/BUILD.md`.

## What Changes

- Add a verified compress and decompress core that never loses a transcript.
- Discover compressed Claude Code and Codex sessions and list them with correct details.
- Add a compression filter (uncompressed, compressed, both), default uncompressed, in the browser
  and on the command line, alongside the visibility filter.
- Mark compressed rows, and show when the filter is hiding matching compressed sessions.
- Report a compressed transcript as its own availability state, not as missing.
- **New dependency:** `zstandard`.

## Capabilities

### New Capabilities

- `session-compression`: compressed sessions on disk, how they are found, listed and filtered.

### Modified Capabilities

None.

## Impact

`compression.py` (new), `model.py`, `conversion.py`, `app.py`, `harnesses/claude.py`,
`harnesses/codex.py`, `pyproject.toml`, tests and docs. The first refresh after install indexes the
existing compressed sessions once, in the background.
