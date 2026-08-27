# Changelog

## 3.4.3 — 2026-08-27

### Changed

- Duplicate titles are disambiguated only when needed with stable, minimally unique session-ID
  suffixes in the interactive browser and plain list output.

### Fixed

- Tracked conversations now accept launch targets only from their recorded conversation members;
  transcript-derived IDs cannot redirect preparation or the final resume command to an unrelated
  existing session.
- Arbitrary transcript ID mentions no longer cross-link two human or subagent sessions. Legacy
  discovery remains available when at least one side is a writer-created bridge artifact.
- Ambiguous name errors now point to the browser detail and JSON output for the exact session ID.

## 3.4.2 — 2026-08-27

### Fixed

- Claude Code and Codex bridges now use the configured 1M-context operating policy, reserving 5%
  for target scaffolding instead of applying the obsolete 200k/256k compatibility floors. The
  complete latest native compaction window passes through whenever it fits that policy.

## 3.4.1 — 2026-08-27

### Fixed

- Bridges now refuse before the target writer when selection would drop messages or truncate an
  anchor; explicit `allow_lossy = true` preserves the legacy behavior while reporting exact losses.
- Moved legacy sources preserve conservative `advanced` status by removing the current checkpoint
  that shadowed `cursor = 0`, preventing stale ancestor reuse or misclassification.

## 3.4.0 — 2026-08-26

### Added

- Recover a live tmux session whose terminal died. When tmux reports no client on the session,
  `sessions` opens one attached to the exact window and pane instead of reporting a dead end.
  The terminal is resolved from `[terminal] command`, then `$TERMINAL`, then a probe list;
  kitty is a fallback default rather than a dependency.

### Fixed

- Only trust a `tmux:<session>` window title as an exact match on the title field, and only
  while a client really is on that session. Substring matching against the whole `wmctrl` line
  let session `2` focus a window titled `tmux:21`, and the title is fixed at launch, so a client
  that had since switched sessions could silently focus the wrong one.
- Every focus failure now names its recovery, including the literal `tmux attach-session`
  command. A session that is attached but unreachable explains why no second terminal was
  opened, since two clients on one session leave both sized to the smaller window.

## 3.3.0 — 2026-08-25

### Added

- A discoverable `t` tool picker can show any non-empty combination of registered harnesses while
  keeping `Tab` as a fast single-tool preset cycle.
- The interactive browser paints a persisted session catalog immediately and refreshes provider
  discovery and liveness in the background.

### Changed

- Process discovery collects expensive start-time identity only for possible harness processes.
- Codex, Claude Code, and OpenCode discovery extract cross-harness evidence from semantic messages
  instead of regex-scanning every serialized log byte; OpenCode discovery is persisted by exact
  database snapshot identity.
- Native references discovered during a provider scan are reused during reconciliation instead of
  repeatedly searching provider storage.

## 3.2.1 — 2026-08-25

### Fixed

- Resolve duplicate Claude liveness claims by refresh time instead of filename order. A Claude
  process that moves to another session without rewriting its registry entry keeps advertising
  the old one for as long as it runs, so two live processes can claim one session id. The claim
  still being refreshed now wins, and the collision is reported with the losing PID, because a
  stale claim is why focusing a session could open an unrelated terminal.

## 3.2.0 — 2026-08-23

### Added

- OpenCode as a first-class third harness: authoritative SQLite discovery, semantic full/latest
  reading, staged revert and completed-compaction handling, official-import writing, dynamic model
  budgets, exact resume, title publication, liveness, search/list/detail UI, and configuration.
- Storage-neutral `NativeRef` identities, opaque adapter-owned checkpoints, tri-state exact
  availability, and dynamic target preparation for shared mutable native stores.
- Repeatable isolated real-OpenCode Linux and Windows release verifiers.

### Changed

- All six Claude/Codex/OpenCode bridge directions and three same-harness identities now share one
  adapter-neutral conversion and conversation-head routing path.
- Conversation state records opaque checkpoints while preserving legacy integer cursors under
  schema 6 for rollback compatibility.
- Unknown OpenCode semantic part kinds fail conservatively with a user-visible warning rather than
  being silently ignored.

### Fixed

- Child-session details distinguish harnesses that resume a parent from those, including OpenCode,
  that resume the exact child ID.
- Missing or unavailable shared-store rows cannot be mistaken for available sessions merely because
  the database file still exists.
- Concurrent OpenCode writes, unrelated database activity, title-only changes, failed compaction
  attempts, import ID reminting, and timeout-after-commit recovery are classified without losing or
  silently promoting conversation work.
