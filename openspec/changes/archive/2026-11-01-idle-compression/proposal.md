## Why

Andrew wants sessions not reopened for 30 days compressed automatically, hidden ones included, with
nothing deleted. Milestone 3 of `specs/retention/BUILD.md`.

## What Changes

- Record when `sessions` opens or decompresses a session, so that a reopened session restarts its 30
  days even if nothing was written to it.
- Compress idle Claude Code and Codex sessions in the background when `sessions` starts, at most once
  every six hours and by one process at a time.
- Add `sessions --compress-idle`, with `--dry-run`.
- Add `[retention] compress_after_days`, default 30, where 0 turns it off.
- Document retention, including Claude Code's cleanup setting.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `session-compression`: adds automatic idle compression.

## Impact

`retention.py`, `config.py`, `app.py`, README, DESIGN, CHANGELOG. The first run compresses every
qualifying session on this PC.
