## Why

Compressed sessions can be listed but not used. Andrew wants to compress and decompress from
`sessions`, and wants transpiling to work only on uncompressed sessions. Milestone 2 of
`specs/retention/BUILD.md`.

## What Changes

- `Z` in the browser and `--compress` / `--decompress` on the command line.
- Opening a compressed session asks, decompresses it, then opens it.
- Bridging, supervision and native writes refuse a compressed session with a clear message.
- A compressed member of a linked conversation is its own state and never triggers a duplicate
  bridge. Compressing and restoring a member keeps the conversation intact.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `session-compression`: adds actions and gates.

## Impact

`retention.py` (new), `app.py`, `capabilities.py`, `harnesses/claude.py`, `harnesses/codex.py`,
`supervisor/create.py`, `supervisor/context.py`, tests and docs. Depends on
`index-compressed-sessions`, which is archived first.
