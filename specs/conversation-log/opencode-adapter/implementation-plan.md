# OpenCode Native Harness Adapter — Implementation Plan

## Phase 0: Characterize and generalize native identity

- [x] Add tests proving the current JSONL reference/cursor behavior before changing signatures.
- [x] Add immutable `NativeRef(session_id, storage)`, opaque JSON-safe `Checkpoint = int | str`,
  `ReadSnapshot(transcript, checkpoint)`, tri-state `Availability`, and
  `NativeWrite(ref, checkpoint | None, notices)`
  to the neutral model. Core must never compare, order, increment, subtract, or test checkpoint
  truthiness. `BridgeResult` exposes `native` and `source_checkpoint`; remove `.path` and
  `.source_cursor` so every call site must migrate.
- [x] Split adapter hooks deliberately: `read_snapshot(ref, latest_window)` atomically returns
  content plus its checkpoint; `checkpoint(ref)` polls current semantic state; `resolve(id)` may
  search native stores; `availability(ref)` checks only that exact store and returns
  available/unavailable/unknown; `change_status(ref, checkpoint)` compares within the adapter.
  Writers return `NativeWrite` and accept a configured command argv. Required hooks remain explicit
  `Unsupported` where absent.
- [x] Adapt Claude, Codex, and the independent fixture with no behavior change: complete JSONL
  byte cursor, exact transcript path, and existing semantic-tail classifiers.
- [x] Replace every core `Path(storage).is_file()` authority decision with exact adapter
  `availability(ref)`. UNKNOWN (I/O, lock, schema, unavailable adapter) retains authority and blocks
  promotion; only proven UNAVAILABLE removes a candidate. Unknown preserved harnesses remain
  unknown and continue blocking unsafe promotion.
- [x] Change `read_transcript`, `conversation_change_status`, `bridge`, member creation/status,
  migration, bridge reuse, and target recording to carry `NativeRef` and opaque checkpoints. Delete
  the generic file-stat status cache; adapters may cache only successfully validated snapshots with
  adapter-correct tokens, and never cache unknown/unstable results.
- [x] Keep utility state schema 6 for rollback. Preserve legacy integer `cursor` for existing JSONL
  members; add an unknown-to-old-code `checkpoint` member field for new writes (int or string).
  New code prefers `checkpoint` and falls back to legacy `cursor`; rollback code preserves the
  unknown field and treats OpenCode's missing/non-integer legacy cursor as unstable.
- [x] Add `LaunchConfig.provider_command(provider)` as the single configured/default argv source;
  define `provider_prefix` only as that command plus launch-mode flags. Thread only
  `provider_command` to writers, resolve executables with `shutil.which`/Windows `PATHEXT`, execute
  with `shell=False`, and structurally forbid maintenance code from calling `provider_prefix`.
  Preserve schema-2 Claude/Codex fields and schema-3 unknown profiles exactly.
- [x] Add optional adapter `prepare_target(command, cwd)` returning a neutral
  `PreparedTarget(budget_policy, writer_options, notices)`. `prepare_launch` invokes it before
  `resolve_budget`; `bridge()` reuses the same immutable result for selection and write. Existing
  adapters return their static policy with empty options. OpenCode carries the exact selected model
  and authoritative command/DB evidence so selection and import cannot disagree.
  A direct `bridge()` without a prepared target invokes the hook exactly once using the adapter's
  default command; it never silently falls back to a different static policy/model.
- [x] Add a minimal shared mutable-storage fixture in Phase 0: two IDs in one store with exact
  availability, edit/delete, independent semantic checkpoints, and atomic read snapshots. Run
  bridge/head/state/cache behavior against it before any OpenCode adapter code.
- [x] Before choosing the member layout, run the previous merged build against a schema-6 state
  fixture containing an unknown OpenCode member. Prove it preserves the new `checkpoint` field,
  offers no unavailable-adapter resume, attempts no JSONL read of the DB, and blocks promotion. Let
  observed rollback behavior—not an assertion—decide whether legacy `cursor` is absent or sentinel.

### Phase 0 mutation gates

1. A shared storage path, `BridgeResult.storage`, equality, or dedupe makes two session IDs
   interchangeable.
2. A still-existing store makes a deleted row available, or a same-ID row in a different candidate
   store silently rebinds the exact ref.
3. A legacy integer cursor is dropped/coerced, or a new checkpoint is not preserved by the current
   schema-6 loader/save path.
4. A mixed integer/string conversation orders, compares, performs arithmetic on, or tests
   checkpoint truthiness; include integer zero.
5. A direct `bridge()` cannot use the adapter default command, or configured launch bridges ignore
   the configured command.
6. Dangerous/custom launch arguments leak into writer maintenance subprocesses.
7. A registry replacement reuses another adapter's cached status result.
8. Independent capture then read records a checkpoint for content from a different native snapshot.
9. A locked/corrupt/inaccessible store becomes unavailable instead of unknown and permits unsafe
   promotion.
10. Availability/read/title checks create a main database at a missing path. Existing WAL stores may
    create expected sidecars; read-only sidecar failure must be UNKNOWN, not unavailable.
11. A schema-6 migration derives a byte cursor from a shared `.db` path.
12. A maintenance writer calls `provider_prefix` and receives dangerous/custom launch flags.
13. An unverified committed write records an expected checkpoint and a later partial/mismatched
    import is misclassified as unchanged.

### Phase 0 validation

- The shared WAL fixture proves exact `(session id, store)` identity, row deletion, same-ID rows in
  another store, unrelated-session isolation, in-place edits, one-transaction content/checkpoint
  capture, head routing, opaque checkpoint persistence, and no duplicate creation on UNKNOWN.
- The previous merged build (`3485239`) was run in a detached temporary worktree against a schema-6
  state containing a newer unknown OpenCode member. Observed result: checkpoint preserved, legacy
  cursor absent, OpenCode not offered, zero JSONL reads of the database, and promotion blocked.
- Claude Opus 5 implementation reviews progressed `REVISE` → `REVISE` → `REVISE` → `GO`. Every
  accepted HIGH/MEDIUM finding has a focused regression test; two proposed UNKNOWN fallbacks were
  rejected because they contradicted the reviewed authority-preservation rule, and the final
  reviewer accepted that disposition.
- Final gate: 273 tests pass on native Windows (one Windows-only focus skip) and 273 on Ubuntu WSL;
  Ruff 0.16.3 lint and format checks pass.

## Phase 1: OpenCode storage and discovery

- [ ] Add `harnesses/opencode.py`, install it at order 30, and define `OPENCODE_HOME` from non-empty
  `XDG_DATA_HOME` / Windows `LOCALAPPDATA` / `~/.local/share`, plus `/opencode`.
- [ ] Put configured maintenance command argv in the shared discovery context. Resolve the one
  authoritative database by invoking `<command> db path` with the same cwd/environment contract as
  resume/import. If the command is unavailable, fall back only to explicit `OPENCODE_DB` or stable
  `opencode.db`, exclude `:memory:`, and mark the binding inferred. Never glob adjacent channel or
  backup databases into the active harness.
- [ ] Route discovery-time `db path` through one bounded subprocess helper: `shell=False`,
  `stdin=DEVNULL`, captured bounded output, explicit timeout, and no interactive console. Resolve
  and memoize it once per discovery pass. Missing command, timeout/hang, nonzero exit, or
  unparseable output all yield the inferred binding plus one provider notice; none abort or delay
  Claude/Codex discovery.
- [ ] Reuse that helper for every writer/preparation subprocess (`models`, `db path`, `import`) with
  `stdin=DEVNULL`, bounded output, no interactive console, and operation-specific timeouts. Hanging
  model/import fakes must fail actionably without freezing `prepare_launch`.
- [ ] Open WAL-aware, query-only connections with bounded busy timeout. Verify required
  `session/message/part` tables and columns and surface one provider warning on incompatible DBs.
- [ ] Classify storage before connecting: a stat-proven missing main DB with an accessible parent is
  UNAVAILABLE; an existing but unreadable/locked/corrupt DB or inaccessible parent is UNKNOWN.
  Never silently rebind an exact member. A missing newest member remains conservatively blocked,
  matching existing deleted-JSONL behavior, until the user resolves that routing authority.
- [ ] Query sessions and aggregate first/latest user preview and counts without materializing the
  whole DB. Parse JSON defensively. Evidence scans retain bounded bytes/IDs per session and mark
  truncation so an incomplete scan cannot support a negative cross-origin conclusion.
- [ ] Emit roots as interactive and `parent_id` rows as subagents, retaining archived evidence.
  Derive native default-title status, first/latest user preview, user-message count, timestamps,
  cwd, parent, and actual DB storage.
- [ ] Scan message/part JSON bytes through the pass's registry-sensitive bounded evidence
  accumulator and publish per-session evidence without provider-name coupling.
- [ ] Implement `resolve(id)` in the one authoritative/inferred DB and exact `availability(ref)`
  against only the referenced canonical DB. Missing row is unavailable; BUSY/CANTOPEN/permissions/corruption/schema
  mismatch are unknown with a diagnostic and retained routing authority.
- [ ] Add SQLite fixtures covering roots, children, archive, malformed JSON, missing schema,
  authoritative path versus adjacent backup/channel DBs, WAL-visible rows, OPENCODE_DB overrides,
  evidence overlap/truncation, empty env values, and both Windows/Linux home resolution in fresh
  interpreters. Add a large-store query/peak-memory gate; bounded evidence may trade completeness
  only by setting its persisted truncation flag.

## Phase 2: Reader, compaction, and semantic cursor

- [ ] Hydrate ordered current `message`/`part` rows in one read transaction and reconstruct exact
  native `info`/part objects plus the session's `revert` boundary.
- [ ] Apply staged revert before compaction exactly like `SessionRevert.cleanup`: no-part boundary
  removes that message and later messages; part boundary retains earlier parts of that message and
  removes the selected/later parts plus later messages. Revert/unrevert before and after compaction
  must change both exported context and checkpoint without waiting for physical row cleanup.
- [ ] Port the current `MessageV2.filterCompacted` algorithm with named tests copied as behavioral
  invariants—not TypeScript implementation dependencies—including no compaction, completed summary,
  retained tail reorder, incomplete compaction, error/no-finish summary, equal-time ID ordering, and
  `latest_window=false`.
- [ ] Project user/assistant text and completed/error/pending/running tool parts into neutral turns.
  Mark readable assistant summaries as compaction boundaries. Project file attachments, subtasks,
  and agent selectors as bounded inert placeholders/summaries; ignore only audited reasoning,
  snapshot, patch, step, and retry bookkeeping. Unknown part kinds fail conservatively.
- [ ] Implement `sqlite-semantic-v1:<sha256>` checkpoints over complete raw semantic state: ordered
  user/assistant text, compaction/revert fields, file/subtask/agent semantics, and tool
  ID/name/input/status/output/error. Exclude title, unrelated sessions, audited reasoning,
  snapshots, patches, step/retry bookkeeping, and display metadata. The digest input
  is intentionally richer than lossy neutral projection and covers both latest/full read modes.
  Verify unchanged, append, edit/delete before and after compaction, compaction, pending→running and
  terminal tool transitions, unrelated-session update, title-only update, row deletion, WAL
  activity, busy/error, and schema-replacement outcomes.
- [ ] Define digest closure mechanically: every native field consumed by compaction filtering or
  neutral projection is included—message role/ID/order key, summary flag, finish/error completion,
  compaction auto/overflow/tail start, revert message/part boundary, text, semantic attachment/task/
  agent fields, and listed tool state. A field may be excluded only with evidence that it cannot
  alter user-visible conversation content. Record the audited 1.18.21 part-kind list; any unknown
  kind changes the fixture and fails until classified.
- [ ] If status caching is justified by measurement, keep it inside the adapter and key it by exact
  ref, prior checkpoint, `latest_window`, main/WAL/SHM snapshot, registry generation, and hook identity. Prove an
  uncheckpointed second-connection WAL commit invalidates it. No cache is preferred until needed.
- [ ] Parse checkpoint scheme tags. Current scheme compares normally; unknown/old schemes return
  UNKNOWN, retain authority, block promotion without claiming divergence, and emit one bounded
  diagnostic.
- [ ] Pin OpenCode's status mapping: equal digest → unchanged; unequal current semantic digest →
  changed; missing/inaccessible/incompatible/unknown-scheme routes through the separate
  availability/unstable/unknown contracts. Core never asks the digest to order changes. A
  legitimate revert is changed and becomes the head even though projected turn count shrinks.
- [ ] Measure reader and digest memory on generated large SQLite sessions; query/streaming growth
  must follow live session records, not total database size or compaction count.

## Phase 3: Writer and native mutations

- [ ] Capture dated real 1.18.21 `db path` and `import` stdout/stderr/exit behavior plus persisted-row
  diffs as fixtures before implementing parsers. Current official source and isolated behavior
  preserve submitted session/message/part IDs; keep a remint recovery branch for future drift.
- [ ] Capture dated real `models --verbose` and non-verbose outputs as parser fixtures. Validate only
  fields the current CLI actually emits: model/provider IDs, status when present, context/input
  limits, and cost. Honor optional provider `bridge_model`; otherwise retain CLI order and use cost
  only as a deterministic tie-break. Unparseable verbose output falls back to non-verbose IDs plus
  the conservative 128k policy with a notice; it never guesses a numeric limit.
- [ ] Implement OpenCode `prepare_target`: choose the exact import model first and derive a dynamic
  `BudgetPolicy` from its declared input limit minus a bounded output reserve. Resolve/default-select
  against that policy before assembly. Reject an explicit budget above it with guidance to change
  `bridge_model`/budget. Thread the selected model unchanged into writer and provenance note.
- [ ] Generate strictly monotone native-shape sortable `ses_`/`msg_`/`prt_` IDs—including hundreds
  generated in one millisecond—and a minimal schema-valid export:
  session info, alternating user/assistant messages, text parts, parent links, model references,
  target cwd/root, timestamps, finish state, and zero accounting for imported history.
- [ ] Pin ID direction from the dated capture: session IDs use OpenCode's descending time encoding;
  message/part IDs use ascending encoding with a per-millisecond counter.
- [ ] Resolve `<command> db path` before import using the exact command/cwd/environment that import
  will use, preflight that the generated session ID is absent, write export JSON to a
  permission-restricted temporary file, and call configured `<command> import <file>` in target cwd. Persisted-row
  verification is authoritative; stdout ID text is a secondary consistency check and a verified
  row with changed/localized output succeeds with a warning. Re-resolve DB authority after import;
  require the row in that DB and verify exact generated message/part IDs plus a semantic checkpoint
  equal to the generated payload. ID/content conflict is a hard error naming the recoverable ID.
  Always remove the temporary file.
- [ ] If import returns or persists a different session ID, bind the actual persisted ID and verify
  its exact content. Locate it first from CLI output, then from a unique provenance marker plus the
  bounded creation window. The marker contains a per-attempt random nonce recorded before import,
  so a retry cannot match an earlier attempt. If neither identifies one row, fail without naming the requested ID as
  recoverable. Every error after commit names only an ID proven to exist.
- [ ] Detect nonexistent command, timeout, malformed model output, no suitable model, unwritable cwd,
  import rejection, wrong/reused ID, ambiguous DB path, and missing post-import row with actionable
  `BridgeError`s. Never expose subprocess environment or credentials in errors.
- [ ] Add bounded retry/backoff for post-import row/content/checkpoint verification while OpenCode's
  write lock settles. On exhausted UNKNOWN after a successful import, return a recorded
  `NativeWrite` ref with `checkpoint=None` plus warning so conversation state retains the
  recoverable ID and blocks promotion; never record an expected-but-unobserved checkpoint and never
  retry the import itself automatically.
- [ ] Implement explicitly best-effort title publication with URI `mode=rw`, bounded busy timeout,
  `BEGIN IMMEDIATE`, parameterized exact-ref update, exactly-one-row verification, commit/re-read,
  and a live-TUI caveat. Verify rollback on errors, no DB/sidecar creation for a missing ref, and
  semantic checkpoint unchanged. Local title remains authoritative.
- [ ] Add exact resume args `--session <id>`, source kinds, ID pattern, label/order, OpenCode-only
  dangerous `--auto`, scratch policy, and a 128k/0.75/2.0 warned fallback budget with dated
  provenance; prepared model policy is the normal path.
- [ ] Extend the shared Linux process snapshot to include name/cmdline safely (Windows already has
  them). Implement exact-only cross-platform
  OpenCode liveness for `--session`/`-s` syntaxes, executable/wrapper forms, PID reuse evidence, and
  roots/subagents; prove bare or ambiguous processes produce no match.

## Phase 4: Pairwise and conversation-routing validation

- [ ] Add deterministic fake OpenCode executables used on Windows and Linux—including a `.cmd`
  resolved through `PATHEXT` with `shell=False`—for
  models/import/db-path behavior, while all storage/read/discovery logic uses real SQLite fixtures.
- [ ] Give `.cmd`/`.bat` fakes metacharacter-bearing temp paths and assert argv fidelity under the
  platform's unavoidable command-shim parsing; generated IDs remain restricted to the enforced
  native-safe character set.
- [ ] Add a discovery fake whose `db path` never exits. The shared pass must return within the
  timeout, discover Claude/Codex normally, use an inferred OpenCode binding, and emit one notice.
- [ ] Run all six ordered bridge directions through production `bridge()` and `prepare_launch()`.
  For each, reread the target natively and compare semantic roles/text/tool summaries/order.
- [ ] Validate the three same-harness identities separately through production native resume:
  Claude→Claude, Codex→Codex, and OpenCode→OpenCode create no copy and no second member. The bridge
  API continues to reject same-harness materialization explicitly.
- [ ] For OpenCode as source and target, prove target budget selection, latest/full windows,
  provenance note, title suffix, custom command, safe/dangerous/custom resume argv, local/provider
  rename, discovery refresh, hidden/search/list/JSON/detail UI, and native exact resume.
- [ ] Exercise archived and parent-ID child sessions as sources and routing heads. Real CLI
  validation must prove exact resume/advance before either kind is offered as resumable; otherwise
  discovery marks it explicitly non-resumable instead of constructing an unverified command.
- [ ] Extend head-routing tests with OpenCode equivalents: equivalent target reuse, source advance,
  target advance, return hop, missing row, unavailable newest generation, concurrent source update,
  two-way divergence, title-only nonadvance, and unrelated-DB-write nonadvance.
- [ ] Generalize the shared third-harness matrix so a fourth adapter still requires no core edits;
  retain structural gates against provider-name branches and pairwise converters.

## Phase 5: Real CLI, review, and release gates

- [ ] Run an isolated real OpenCode integration on Linux: create a native session, discover/read it,
  bridge Claude and Codex copies into OpenCode, export/reread them, resume/append one via
  `opencode run --session`, observe semantic advancement, bridge back, and verify head routing.
- [ ] Create/observe a real compaction and differentially compare the adapter's latest-window order
  with current OpenCode behavior. Record tested CLI/session versions and warn on unknown semantic
  part kinds; do not reject every future patch version solely by its version string.
- [ ] Attribute the compaction port and behavioral fixtures in adapter/test comments to OpenCode
  1.18.21 `packages/opencode/src/session/message-v2.ts`; retain the source URL in documentation.
- [ ] Run the corresponding temporary native-Windows OpenCode import/discovery/read/resume-command
  checks without installing persistently or touching the user's provider data.
- [ ] Run full native Windows and WSL suites, Ruff lint/format, build/sdist/wheel/twine checks, and
  GitHub Actions on Python 3.11–3.13 for both OSes.
- [ ] Ask Claude Opus 5 for a follow-up plan verdict and focused reviews after Phase 0, Phase 2/3,
  and the final integrated diff.
  Resolve/disprove every HIGH/MEDIUM and rerun affected mutation gates.
- [ ] Update `HARNESS_CONTRACT.md`, `NORTH_STAR.md`, README/config examples/help, spec index, changelog,
  and an evidence-backed retro. Open a PR with the WSL `vandyand` account and complete CI/review.
- [ ] Perform a requirement-by-requirement completion audit against this plan and the active user
  objective before declaring the three-harness system complete.
- [ ] Prove digest/read closure in both window modes by changing an assistant summary from
  incomplete/errored to completed without changing its text; checkpoint and selected window must
  both change.
- [ ] Revert then unrevert a message and a part, before and after compaction. Each transition must
  change the checkpoint and selected context, and no reverted member may be promoted as unchanged.
- [ ] Round-trip a committed-but-unverified OpenCode member with `checkpoint=None` through schema-6
  state and prove every status decision is unstable/unknown, never unchanged or promotable.

## Rollback

State schema remains 6: legacy integer cursors remain valid; new opaque checkpoints live in an
additional member field that the current schema-6 loader/save path preserves. Rolling back may hide
or refuse OpenCode operations and treats its member as unstable, but does not erase routing authority.
Provider databases are never migrated. A failed writer may leave a successfully imported native
session only after OpenCode has committed it; the error names that ID so the user can recover it.
