# Changelog

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
