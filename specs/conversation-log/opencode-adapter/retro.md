# OpenCode native harness adapter — retrospective

## Outcome

OpenCode became the third production harness without adding a pairwise converter or a provider-name
branch to core routing. Its native identity is `(session ID, authoritative database path)`, its
checkpoint is a session-local semantic digest, and its writer uses the official `opencode import`
command. All six ordered cross-harness bridges and all three exact native resumes use the same
registry, neutral conversation, budget, and head-routing paths.

The implementation landed in six staged commits before documentation:

- `a30e93d` — native references, opaque checkpoints, exact availability, dynamic preparation;
- `0a8c2bc` — authoritative OpenCode storage/discovery;
- `666c7d3` — semantic reader, revert, compaction, and checkpointing;
- `ac43383` — official-import writer, titles, models/budgets, resume, and liveness;
- `98c04e0` — six-direction, same-identity, UI, and routing matrix;
- `a5cd43d` — repeatable real Linux/Windows CLI and compaction release gates.

## What changed architecturally

The earlier seam assumed one append-only JSONL file and one integer byte cursor per session.
OpenCode's many-sessions-in-one-WAL-database design forced the correct abstraction:

- `NativeRef(session_id, storage)` identifies native material independently of storage layout.
- `Checkpoint = int | str` is adapter-owned and opaque to core.
- `read(ref)` returns content and checkpoint from one native snapshot.
- `availability(ref)` checks the exact reference; `resolve(id)` may search.
- `prepare_target(command, cwd, options)` freezes dynamic model/database/budget evidence before
  selection and write.
- Unknown, unavailable, and unstable native state retain or remove routing authority deliberately;
  shared-store existence is never inferred from `Path.is_file()`.

That work, rather than the SQLite query itself, is the lasting value of the adapter. The independent
fourth-format fixture still registers after application import and participates in every runtime
surface without core edits.

## What the reviews caught

Opus 5 review was not ceremonial. Across the storage, reader, writer/liveness, and integrated matrix
passes it found high- and medium-severity gaps that green self-authored fixtures missed, including:

- database-path authority could silently drift after import;
- shared WAL snapshots and checkpoint reads could observe different commits;
- an incomplete/failed compaction attempt could become an unsafe truncation anchor;
- writer success did not initially prove the exact persisted content or recover reminted IDs;
- timeout-after-commit and committed-but-uncheckpointed members needed explicit recovery states;
- Windows `.cmd` execution had not actually proved extensionless `PATHEXT` resolution;
- archived/child resume support lacked real-provider proof;
- the detail pane falsely said an exact-resume OpenCode child would open its parent;
- filtered six-direction assertions could hide extra/reordered native turns.

The Phase 4 review returned NO-GO with HIGH=2 and MEDIUM=6. After each finding was reproduced and
fixed, the follow-up reran the matrix and returned GO. This repeated the lesson from the earlier
Codex-window release: a time-limited or skipped adversarial pass is not graceful degradation when
the omitted evidence is the only thing capable of finding the blind spot.

## Provider-native evidence

All real-provider checks used disposable XDG stores and synthetic prompts. No user OpenCode store,
credential, or persistent installation was read or changed.

- OpenCode 1.18.21 created a root and a genuine task-tool child. Exact `--session <child>` advanced
  the child while retaining its `parent_id`; exact `--session <archived-root>` advanced the archived
  root while its archive timestamp remained set.
- `tools/verify_opencode_real.py` created/discovered/read a native session, imported and exported
  both Claude- and Codex-origin sessions, resumed one exact OpenCode copy with model output, observed
  its semantic checkpoint advance, bridged that work back to Codex, and proved head reuse.
- The same gate invoked native compaction. The export ended in a user `compaction` part followed by
  a completed summary; the adapter's latest window exactly matched that final native boundary while
  full mode retained the earlier exchange.
- `tools/verify_opencode_maintenance.py` ran against a temporarily extracted native Windows 1.18.21
  binary and proved `db path`, official import, discovery/reread, export, and exact safe/dangerous
  resume argv. The package was never installed and was recycled afterward.

## Deliberate limitations

- Tool calls cross as inert summaries, never executable target-native tool blocks.
- A future unknown semantic part kind blocks bridging and emits a warning until classified. Patch
  versions are not rejected solely by version string.
- OpenCode title publication is a bounded transactional metadata exception because 1.18.21 has no
  rename CLI. It is excluded from semantic checkpoints.
- Liveness is exact only when a process command line names the session ID; bare OpenCode processes
  are not guessed.
- The adapter completes harness/storage decoupling, not the later per-message provenance goal.

## Final gate

Before release, native Windows and Ubuntu WSL each passed 462 tests (one and three platform skips),
Ruff 0.16.3 lint/format and `git diff --check` passed, and the 3.2.0 sdist/wheel passed `twine check`.
The implementation plan retains the exact real-session IDs, timestamps, commands, and review
dispositions behind those aggregate numbers.

The final integrated Opus 5 pass returned GO with no HIGH findings and named one remaining MEDIUM
plus three LOW error-path mutations. Four additional tests killed those mutations; an Opus follow-up
ran mutant copies outside the repository and returned GO with no remaining findings.
