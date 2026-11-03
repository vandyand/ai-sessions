## Context

`compress_session` is verified and refuses open sessions. Several `sessions` windows run at once,
and `state.json` is last-writer-wins, so it cannot hold a reliable last-opened time.

## Goals / Non-Goals

**Goals:** nothing old stays uncompressed for long; a session in use is never touched; startup does
not slow down.

**Non-Goals:** a scheduled task. Nothing is lost if `sessions` is not run for a while; compression
just waits.

## Decisions

1. **Last activity** is the latest of the session's `updated`, its transcript's modification time, and
   its last recorded open. Compression and decompression keep the modification time, so only the
   open log restarts the clock after a decompress.
2. **Open log:** `session-opens.jsonl`, append-only in the cache folder. Appends from several windows
   cannot overwrite each other, unlike `state.json`. *Alternative:* a field in `state.json`, rejected
   for that reason.
3. **A separate hidden process:** `python -m ai_sessions --compress-idle` with `CREATE_NO_WINDOW`, so
   it survives quitting the browser, and a killed daemon thread cannot strand work. Output goes to
   `retention.log`.
4. **Lock:** an exclusive-create lock file holding the PID. It is stale when that PID is gone or after
   12 hours.
5. **Six-hour gate:** a timestamp written before spawning, so windows opening together start one run.
6. **Start point:** immediately before the curses screen starts, a path only an interactive terminal
   reaches. No test reaches it, so a test run cannot start a real sweep.
7. **Re-check per session:** the open log and the transcript time are read again just before each
   session is compressed.
8. **Every row counts:** agent, subagent and supervisor sessions follow the same rule. A Claude Code
   parent takes its subagent logs with it.
9. **Claude Code's cleanup:** `cleanupPeriodDays` 36500 lives in Andrew's user settings, set
   2026-11-01 and documented, because `0` is rejected and no "forever" value exists.

## Risks / Trade-offs

- [A session opened in the instant between the re-check and removal] → the original is removed only
  after verification, so the `.zst` holds everything. The harness reports it missing; Enter restores.
- [First run on this PC compresses many sessions] → a dry run first, run once by hand.
- [`claude --resume` outside `sessions` cannot read a compressed session] → documented.

## Migration Plan

Install the wheel. The first automatic run happens at the next browser start. Roll back with
`compress_after_days = 0` and `sessions --decompress`.

## Open Questions

None.
