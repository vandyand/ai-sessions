# OpenCode Native Harness Adapter — Research

Research date: 2026-08-23. Official repository inspected at
`anomalyco/opencode@3a31c4ea801915c0b050df4b3842997ea62b6e93` (dev, 2026-08-22).
Behavioral checks used an isolated OpenCode 1.18.21 binary and isolated XDG data/config/cache/state
directories; the user's OpenCode home and credentials were not used.

## Public CLI behavior verified

The current [CLI documentation](https://opencode.ai/docs/cli/) and installed CLI expose:

- exact TUI resume with `opencode --session <id>` / `-s <id>`;
- `opencode session list --format json`, which returns root, non-archived sessions only and emits
  no output—not `[]`—when empty;
- `opencode export [sessionID]` and `opencode import <file>`;
- `opencode db path` and `opencode db <query> --format json`;
- `opencode models --verbose`; and
- `--auto`, which auto-approves permissions not explicitly denied.

An isolated `opencode run --model opencode/big-pickle` created
`ses_fd151ff55ffe63JWbXeAI57IvR`. Export contained a session info object and ordered
`{info, parts}` messages. Importing that export into a different isolated database/cwd retained
the session ID and content while OpenCode correctly replaced project/directory/path association.

## Persistence

Current storage is a WAL-mode SQLite database. `OPENCODE_DB` is absolute when given an absolute
path, otherwise relative to OpenCode's XDG data directory. Stable/latest/beta/prod channels use
`opencode.db`; named channels use `opencode-<channel>.db`.

The configured command's `db path` result, invoked with the same cwd and inherited environment as
resume/import, is authoritative. Adjacent channel-looking files are not equivalent: they can be
backups or databases owned by a different binary/channel. When the command is unavailable,
discovery falls back only to an explicit `OPENCODE_DB` or the stable default and reports that the
binding is inferred.

Relevant current tables are:

```text
session(id, project_id, parent_id, directory, path, title, version,
        time_created, time_updated, time_archived, ...)
message(id, session_id, time_created, time_updated, data JSON)
part(id, message_id, session_id, time_created, time_updated, data JSON)
```

The repository also contains newer `session_message` work, but current CLI export/import and
runtime `MessageV2` still read/write `message` and `part`. The adapter must verify the required
tables/columns and report an incompatible schema rather than silently returning an empty list.

Direct discovery is necessary: the supported session-list command deliberately requests
`roots: true` and excludes archived sessions. Direct writes are avoided except for the bounded
title update. Session materialization goes through official import so OpenCode—not this utility—
owns migrations, foreign keys, project association, and insert behavior.

## Session and message structure

Generated IDs are `ses_`, `msg_`, or `prt_` plus a 26-character sortable identifier: 12 lowercase
hex time/counter characters and 14 base-62 random characters. Import schemas require the
prefixes; the writer follows the native generator shape as well.

A minimal imported user message requires:

```json
{
  "role": "user",
  "time": {"created": 0},
  "agent": "build",
  "model": {"providerID": "opencode", "modelID": "big-pickle"}
}
```

An assistant message additionally requires `parentID`, `mode`, `agent`, `path`, `cost`, token
counters, `modelID`, and `providerID`. Plain content is a `text` part. Imported messages and parts
are insert-on-conflict-do-nothing, which makes preflight ID nonexistence and post-import
verification important.

## Compaction semantics

`MessageV2.stream()` reads newest-first. `filterCompacted()` walks that order until it finds the
newest completed compaction (a user `compaction` part whose assistant child has `summary=true`,
`finish`, and no error). It then reverses the result and, when `tail_start_id` is present, reorders
the live model context to:

```text
compaction request → assistant summary → retained tail → later continuation
```

The reader must reproduce this algorithm, including its time/ID ordering and incomplete-
compaction behavior. `latest_window=false` instead exposes the complete chronological transcript.
The summary is readable, unlike Codex's sealed summary, so it becomes a compaction-marked
assistant turn.

OpenCode undo/redo is also part of live conversation semantics. A staged undo persists
`session.revert = {messageID, partID?, snapshot?, diff?}` without immediately deleting rows.
`SessionRevert.cleanup()` performs the deletion only when the next prompt starts: without `partID`
it removes the boundary message and everything after it; with `partID` it retains the boundary
message's earlier parts and removes that part plus all later messages. The adapter applies the same
view before compaction filtering and hashes the semantic boundary fields. Unrevert clears the row
state and restores the original view, so both transitions change the checkpoint even when no
message row changes.

## Tool projection

Legacy/current tool parts contain `callID`, `tool`, and one of:

- `pending`: input/raw;
- `running`: input/title/metadata/start;
- `completed`: input/output/title/metadata/times/attachments; or
- `error`: input/error/metadata/times.

OpenCode itself converts incomplete calls into interrupted errors for model replay. The adapter
uses the same safe principle in neutral form: completed output is the result, errors are labelled,
and pending/running calls receive an interrupted marker. Reasoning, snapshots, patches, step
markers, and provider retry bookkeeping are not replayed as conversation text. File attachments,
subtasks, and agent selectors are semantic: they cross as bounded inert placeholders/summaries so
a message containing no plain text does not silently disappear. The tested part-kind list is a
fixture; an unknown kind fails conservatively until classified as semantic or metadata.

## Checkpoint and concurrency

A shared DB file size, mtime, WAL size, or byte offset is not a session checkpoint: another session
can change all of them. The semantic checkpoint is a versioned SHA-256 digest over the selected
session's complete raw semantic conversation/tool state: user/assistant text, compaction/revert
records, file/subtask/agent semantics, and tool identity/input/status/output/error. It deliberately
excludes the `session` row title, unrelated sessions, reasoning-only noise, snapshots, patches,
step/retry bookkeeping, and display metadata. The
checkpoint is a superset of both latest-window and full replay, so either mode detects edits and
deletes in content it can export. A checkpoint with an unknown scheme tag is UNKNOWN, never changed.

The adapter returns the transcript and checkpoint from one read-only SQLite transaction. After
materialization, the current checkpoint is recomputed and must still match. Any committed live
insert/update/delete or tool-state transition changes it; a title-only update and another session's
traffic do not. Missing rows are unavailable. Failed transactions, malformed JSON, schema mismatch,
or timeout are unknown/unstable and retain routing authority; they are never treated as absence.

## Target budget and model selection

OpenCode can expose arbitrary configured providers, so no single model context is universal.
The current isolated built-ins included 190k- and 200k-context free models; `big-pickle` reported
200,000 context / 160,000 input / 32,000 output. The adapter declares a conservative 128,000-token
unknown-model compatibility floor at the common 0.75 usable fraction and 2.0 chars/token:
96,000 estimated tokens / 192,000 selected characters.

At target-preparation time, `models --verbose` gives stronger evidence. An explicit provider
`bridge_model` wins when installed; otherwise the CLI's ordinary model order is retained and cost
is only a deterministic tie-break among adequate candidates. The selected model's declared input
limit, less a bounded output reserve, becomes the target `BudgetPolicy` before conversation
selection. An explicit `max_tokens` above that prepared policy fails with an actionable setting
error instead of silently overfilling. If verbose output drifts or is unavailable, non-verbose
model IDs plus the static 128k fallback create a conservative target with a warning.

## Liveness and title limits

OpenCode publishes no per-session lock or PID mapping. A process whose command line contains
`--session <id>`, `--session=<id>`, `-s <id>`, or `-s=<id>` is exact evidence; a bare `opencode`
process is not. Linux process snapshots therefore need the same read-only name/cmdline fields
already captured on Windows while retaining `/proc/<pid>/stat` start tokens.

There is no dedicated title CLI command. The server has a session patch API, but depending on a
running server would make local rename unreliable. Provider publication is therefore explicitly
best-effort: URI `mode=rw` (never creating a DB), bounded busy timeout, `BEGIN IMMEDIATE`, a
parameterized `UPDATE session SET title=? WHERE id=?`, exactly-one-row verification, commit, and
re-read. Active TUIs may display or later replace the value; the local utility name remains
authoritative either way. Title publication is ignored by the semantic checkpoint.

Opening an existing WAL database can legitimately create or refresh `-wal`/`-shm` sidecars, even
for a reader, when its directory is writable. Safety means never creating the main database at a
missing path. If required sidecars cannot be opened in a read-only directory, availability is
UNKNOWN rather than UNAVAILABLE; `immutable=1` is not used because it can hide live WAL data.

## Sources

- [OpenCode CLI](https://opencode.ai/docs/cli/)
- [OpenCode installation](https://opencode.ai/docs/)
- [OpenCode server API](https://dev.opencode.ai/docs/server/)
- [session CLI source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/session.ts)
- [export source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/export.ts)
- [import source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/import.ts)
- [database path source](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/database/database.ts)
- [session tables](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts)
- [compaction filtering](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts)
