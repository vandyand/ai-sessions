# Native Terminal workspace integration

Goal: keep genuine Windows Terminal while resuming durable ai-sessions pane assignments.
The user approved this approach and asked to proceed without another design round.

Use the existing Python workspace store and PowerShell pane launcher. Add a
Terminal preference beside the workspace database, and `workspace open NAME`.
The first open constructs the saved layout in a named Preview window. Subsequent
opens use Terminal's persisted window and focus-tab action, preserving user layout
changes. Never edit Terminal settings/state or infer native agent IDs from titles.
Native window names are reserved with a sessions- prefix. Persisted pane commands
must still identify this workspace, store, state, Python and launcher before replay.
Existing restore/clone/bind behavior and live-agent duplication guards remain.

Alternatives: a desktop companion adds another app before it is needed; rewriting
Terminal's state risks racing its writers. Native launch commands need neither.

- [x] Write failing tests for preference isolation, first open, reopen, conflicting
  saved commands, invalid configuration, and repeated open before persistence.
- [x] Implement native.py and CLI routing, sharing managed argv construction.
- [x] Verify a real portable Preview close/reopen, including two distinct durable
  pane commands; test agent resume through the existing exact-native run path.
- [x] Update usage docs, run the relevant suite and full checks, review the diff.

Constraints: Python 3.11+, no new dependencies, no C#, no terminal UI automation,
no changes to stable Terminal or user panes, no process-name termination.
Actual reboot and graphical workspace flyout are outside automated verification.


Validation: 84 focused workspace tests passed; the full suite passed 1,295 tests
(two skips) with isolated messaging configuration. Ruff check/format passed.
Native Preview close/reopen restored exact Codex/Claude test IDs with new PID/birth
pairs and matching native client argv. First probe's stale-lease observation was
fixed by polling live generations, rather than accepting leftover lease rows.
Review fixes: database+full-name scoped window identity, shared pane argv, and
explicit --recover using a new window generation for an unsaved failed launch.
