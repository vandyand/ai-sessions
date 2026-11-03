# Build charter: session retention and compression

Date: 2026-11-01. Requested by Andrew through `/goal`; his request is the definition of done, and
the choices he left open are recorded under Decisions. Branch: `supervisor-sessions` (not pushed).

## Objective

Nothing in Codex or Claude Code is ever deleted by age. Sessions not reopened for 30 days are
compressed instead, stay visible in `sessions`, and can be compressed and decompressed from it.

## What investigation established (2026-11-01)

- **Claude Code deletes.** `cleanupPeriodDays` defaults to 30 and removes transcripts, subagent
  logs, tool results and file history. Claude's own docs state this. It has no compressed format
  and cannot resume one. The oldest surviving transcript is Oct 2, while prompts go back to August,
  so older transcripts are already gone and cannot be recovered.
- **Codex does not delete by age.** Rollouts from May survive. `codex delete` is explicit only.
- **Codex compresses on its own in some builds.** The binary has a `rollout_compression` worker
  and a `local_thread_store_compression` feature, off in 0.165.0. The newest compressed file is
  from Oct 20. 2,387 rollouts are compressed and none also exists as plain `.jsonl`.
- **Codex's view of compression:** its database lists every thread at the plain `.jsonl` path.
  Measured in a throwaway `CODEX_HOME`: resuming with only `.jsonl.zst` present recreates `.jsonl`
  and removes the `.zst`; with both present, the plain file is used and the `.zst` is left alone.

## Acceptance criteria

1. **Nothing deleted, Claude.** `~/.claude/settings.json` has `cleanupPeriodDays` 36500, and Claude
   runs with it. Done 2026-11-01; re-check at acceptance.
2. **Nothing deleted, Codex.** No age-based deletion path exists; the evidence is written down.
3. **Idle compression.** Claude and Codex sessions not reopened for 30 days are compressed
   automatically, including hidden ones. Open sessions and sessions active in the window are left
   alone.
4. **Safe compression.** A compressed session decompresses to byte-identical content. A failure or
   a concurrent write leaves the original in place. An interrupted run leaves no session unreadable.
5. **Compressed sessions listed.** Every compressed Claude and Codex session is indexed, including
   the 2,387 existing rollouts, with correct title, directory and activity.
6. **Filter.** A compression filter offers uncompressed, compressed and both, defaulting to
   uncompressed, in the browser and on the command line, combining with the visibility filter.
7. **Actions.** Compress and decompress work from the browser and the command line.
8. **Transpile gate.** Bridging, transpiling and supervision refuse a compressed session and say how
   to proceed.
9. **Opening.** Opening a compressed session from `sessions` decompresses it first, after asking.
10. **Links survive.** Compressing and decompressing a member of a linked conversation neither
    loses nor corrupts the conversation, and its checkpoints still work after decompression.
11. **Speed.** Normal startup stays about as fast as today once compressed sessions are indexed.
12. **Live proof.** Items 3, 4, 7, 9 and 10 are shown on real harnesses with throwaway data, and the
    real session list is checked read-only, before any sweep runs on Andrew's real sessions.
13. **Full suite and Ruff pass.** Docs and changelog are updated.

## Non-goals

- OpenCode retention. It has a database, not transcript files.
- Recovering Claude transcripts that were already deleted.
- A scheduled task. The sweep runs when `sessions` starts.
- Compressing Claude `tool-results` or `file-history`, which Claude keeps once its cleanup is off.

## Decisions (made autonomously under `/goal`, reversible)

Dated rationale lives in each change's `design.md`.

- **Format:** `<name>.jsonl.zst` beside the original, which is what Codex writes and reads.
- **Idle clock:** the later of the transcript's last write and the last time `sessions` opened or
  decompressed it, so a decompressed session gets a fresh 30 days.
- **Automatic run:** in the background at `sessions` startup, one process at a time, plus
  `sessions --compress-idle`. `[retention] compress_after_days` sets the 30, and 0 turns it off.
- **Subagents:** a session and its subagents are idle together, so an old subagent row waits until
  its whole family has been quiet for 30 days. Added 2026-11-01 after the live sweep re-packed a
  subagent log straight after its parent was decompressed.
- **Key bindings:** `z` cycles the filter, `Z` compresses or decompresses.
- **Records:** OpenSpec only. Beads was not requested, so `tasks.md` is the task list.

## Constraints

- Follow `C:\Users\vandy\AGENTS.md`, especially process safety. Never kill processes by name.
- Never alter real sessions while testing: use throwaway homes until the final authorised sweep.
- `app.py` uses CRLF; use byte-preserving edits and check `git diff --stat`.
- Generic modules must not name harnesses (`tests/test_source_structure.py`).
- Tests need `PYTHONPATH=src`. Ruff runs through `uvx ruff`.

## Budget

Up to three concurrent workers. Sonnet for bounded implementation, the parent for integration and
acceptance. No external spending. Disk: first real sweep must not need more than 2 GB free on C:.

## Milestones

1. `index-compressed-sessions`: a verified compress/decompress core; compressed sessions discovered
   and listed; the filter. Ends with StarCraft-style rollouts visible.
2. `compress-actions`: `Z`, CLI commands, opening, the transpile and supervision gates, and linked
   conversations.
3. `idle-compression`: idle clock, automatic sweep, hidden sessions, config, docs, live proof.

## Scope ledger

(Items not needed for a criterion; reviewed with Andrew afterwards.)

- 2026-11-01: a stale lock taken by two runs at the same moment could let both sweep; each file
  operation is still safe alone, so this is harmless and left as it is.
- 2026-11-01: leftover `.partial` temporary files from a killed run are not cleaned up.
- 2026-11-01: a scheduled task would sweep on days `sessions` is not started; the charter rules it out.
- 2026-11-01: `--install-hooks` does not set `cleanupPeriodDays` on another machine.
- 2026-11-01: OpenCode retention stays a non-goal.
- 2026-11-01: a listing shows a truncated `.zst` as a short transcript with no warning; restoring refuses it.
- 2026-11-01: `Z` on a subagent row ignores the family rule; the exclusive open still guards it.
- 2026-11-01: a read-only plain file repeats the compression work on each sweep.

## Progress notes

- 2026-11-01: investigation done; criterion 1 met; OpenSpec adopted at Andrew's request.
- 2026-11-01: milestones 1 to 3 archived; two data-loss reviews fixed; all criteria met. See `BUILD-ACCEPTANCE.md`.
