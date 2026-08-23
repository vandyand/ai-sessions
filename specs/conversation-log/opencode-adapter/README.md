---
title: "OpenCode Native Harness Adapter"
status: planned
date: 2026-08-23
priority: 40
---
# OpenCode Native Harness Adapter

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md). This is the production acceptance
test for the completed [`generic-harness-adapter`](../generic-harness-adapter/) boundary.

## Objective

Make OpenCode a first-class third harness. A user can discover, search, rename, resume, and
bridge an OpenCode session through the same conversation/head-routing system as Claude Code
and Codex. Every ordered pair among the three harnesses must work through the neutral
conversation model; no pairwise converter or provider-name branch is permitted.

OpenCode is deliberately not represented as a fake per-session file. Its native identity is
the pair `(session id, database path)`: many sessions share one WAL-mode SQLite database. The
adapter seam is therefore generalized from path/byte-offset storage to a native reference and
an opaque, JSON-serializable semantic checkpoint before the OpenCode adapter is registered.

## Key decisions

| # | Decision | Why |
| --- | --- | --- |
| K1 | Introduce `NativeRef(session_id, storage)` and opaque `Checkpoint = int | str` | A database path alone does not identify an OpenCode session; byte offsets have no session-local meaning in a shared SQLite file. Core never orders, compares, increments, or tests checkpoint truthiness |
| K2 | `read_snapshot(ref)` returns transcript and checkpoint from one native snapshot; adapters compare checkpoints | JSONL adapters read only through a captured complete-record offset; OpenCode reads and hashes raw semantic conversation/tool state in one transaction, then both are rechecked after materialization |
| K3 | Use OpenCode's official `import` command for writes | The importer owns schema validation, migrations, project/worktree association, and transaction behavior; handcrafted database inserts would couple the utility to private write semantics |
| K4 | Read/discover through SQLite read-only transactions | `session list` intentionally omits children and archived sessions, while the database contains both; session-specific reads avoid exporting every session through subprocesses |
| K5 | Match OpenCode's `filterCompacted` ordering | A bridge must carry what OpenCode itself feeds the model: the completed summary, retained tail, then live continuation |
| K6 | Project tools as inert `ToolCall` summaries | Completed/error/interrupted tool states remain useful context without replaying unavailable target-native tool blocks |
| K7 | Prepare the OpenCode target before selection: choose an installed model, derive policy from its input limit, then resolve the bridge budget | Imported user messages require a real model reference and context/input limits differ. An explicit `bridge_model` wins; otherwise CLI order and cost are deterministic tie-breaks. The static 128k policy is a warned fallback only when verbose metadata cannot be parsed |
| K8 | Provider title publication is an explicit best-effort, bounded `mode=rw` exception; local rename remains authoritative | Current OpenCode has no rename CLI command. The update never creates storage, uses `BEGIN IMMEDIATE`, verifies exactly one row and re-reads it, and is excluded from semantic checkpoints; a live TUI may refresh it later |
| K9 | Liveness is exact only when the process command line names `--session`/`-s` | OpenCode exposes no session-to-PID lock. Never guess that a bare process owns a particular session |
| K10 | Keep state schema 6; retain legacy integer `cursor`, add `checkpoint` for new opaque values | The current schema-6 loader preserves unknown member fields but rejects a version-7 file. A rollback build sees an OpenCode member without a valid legacy cursor as unstable and preserves its routing authority |
| K11 | Separate `resolve(id)` from `availability(ref)` | Resolution may search candidates; exact availability never silently rebinds a member to a different database and distinguishes unavailable from unknown I/O/schema state |
| K12 | The database reported by the configured command with the operation's exact cwd/environment is authoritative | Scanning adjacent `opencode-*.db` files can discover backups or another channel that the configured command cannot resume; the writer rechecks authority after import |

## Acceptance criteria

1. OpenCode roots, child/subagent sessions, and archived sessions are discovered from the database
   authoritative for the configured command, with stable IDs, correct source kinds, previews,
   counts, timestamps, storage paths, and bounded cross-origin evidence. Adjacent backups and
   inactive channel databases never shadow the active store.
2. `opencode --session <id>` is constructed exactly in safe, dangerous, and custom modes;
   dangerous mode contributes OpenCode's own `--auto` flag and no other provider's flag.
3. The reader reproduces chronological live context, readable compaction summaries and retained
   tails, text, and all terminal tool states without reasoning/attachment noise.
4. The writer prepares an installed/context-adequate model and budget, produces schema-valid export
   JSON, imports it through the configured OpenCode command in the requested cwd, verifies the
   authoritative DB and exact persisted semantic content/IDs, and deletes its permission-restricted
   temporary file on success or failure.
5. Checkpoint capture/change classification detects insert, update, delete, compaction, and tool-state
   changes for one session; ignores title changes and unrelated sessions; and fails conservatively
   on missing, replaced, locked beyond timeout, malformed, or incompatible storage.
6. Conversation availability is adapter-resolved rather than inferred from `Path.is_file()`.
   Deleting one row from a still-existing database is unavailable, while unrelated DB activity is
   not a false advance.
7. Every ordered bridge pair—Claude→Codex, Claude→OpenCode, Codex→Claude, Codex→OpenCode,
   OpenCode→Claude, OpenCode→Codex—passes shared round-trip, budget, provenance, head reuse,
   advancement, and divergence tests without pairwise code.
8. A real isolated OpenCode installation passes create/discover/read/import/resume/advance/status
   validation without reading or writing the user's OpenCode data or credentials. Windows and
   Linux unit/integration suites, lint, formatting, packaging, and GitHub Actions are green.
## Non-goals

- Replaying tool calls as executable OpenCode tool blocks.
- Choosing or merging divergent conversation heads automatically.
- Treating OpenCode's shared database as a per-session transcript file.
- Depending on a running OpenCode HTTP server for ordinary local operations.
- Supporting unreleased storage tables before OpenCode itself switches its export/import path to
  them. The adapter detects incompatible schemas explicitly rather than guessing.

## Status

- [x] Current official CLI/source and isolated 1.18.21 behavior researched
- [x] Initial Opus 5 plan review *(REVISE: HIGH=8, MEDIUM=11; incorporated below)*
- [x] Follow-up Opus 5 plan review *(REVISE: HIGH=7, MEDIUM=9; incorporated below)*
- [x] Focused final review *(REVISE: HIGH=2, MEDIUM=4; two findings corrected, four disproved below)*
- [x] Revision-4 verdict *(REVISE: HIGH=0, MEDIUM=1; bounded discovery invocation added)*
- [x] Revision-5 verdict *(REVISE: HIGH=0, MEDIUM=1; revert semantics added from source)*
- [x] Final Opus 5 plan verdict *(GO: HIGH=0, MEDIUM=0)*
- [ ] Storage-neutral cursor/reference refinement
- [ ] OpenCode discovery/read/write/resume/rename/liveness adapter
- [ ] Six-direction and real-CLI validation
- [ ] Final Opus 5 reviews, documentation, PR/CI, and completion audit

## Initial Opus 5 review disposition

The first max-effort review rejected the initial seam. It correctly identified independent
capture/read transactions, exact availability versus ID resolution, WAL-unsafe core caching,
schema-7 rollback, shared-DB `.path` compatibility, direct-title ownership, and the absence of a
shared mutable fixture as HIGH gaps. Those findings produced K1, K2, K8, K10, K11 and the revised
Phase 0 gates.

The review also asserted that current core code numerically orders cursors; repository inspection
shows it does not. The plan nevertheless adopts the stronger requested invariant and a structural
test: checkpoint values are opaque outside adapters. Its proposed digest of only the projected
latest window was refined after follow-up: the OpenCode checkpoint hashes complete raw semantic
conversation/tool state, a superset of both latest-window and full replay.

The follow-up closed the revised seam but found OpenCode-specific gaps in import-content
verification, CLI-authoritative DB binding, full-versus-latest checkpoint coverage, lossy tool-state
digests, dynamic model budgeting, expected WAL sidecars, and checkpoint scheme drift. K2, K7, K12
and Phases 1–5 now make each correction executable.

The focused final review correctly required a reminted-ID recovery branch and an explicit unknown
checkpoint for a committed-but-unverified import. It also assumed capabilities the current routing
contract does not have. `change_status` deliberately returns only unchanged/changed/unstable;
divergence is inferred generically when multiple members, or a superseded member, change after their
own checkpoints. A whole-state digest is therefore sufficient and safer for OpenCode's in-place
tool updates. Likewise, same-harness `bridge()` is deliberately rejected: Claude→Claude,
Codex→Codex, and OpenCode→OpenCode continue by exact native resume, so the exhaustive conversion
matrix is the six distinct-harness directions plus three native-resume identities.

The final GO noted that a digest supplies equality, not prefix ordering. That is exactly the current
contract OpenCode needs: it emits unchanged/changed/unstable, and core derives head/divergence from
which members changed. A staged revert is intentional semantic work, so `changed` makes its
shortened context the head; it must never be mistaken for unchanged merely because turn count
decreased.
