# Predictable workspace save and layout-only reuse

The user requested closing the occupied-pane/layout-only usability gap and
rejected unsupported reliability claims for shell commands.

Default save and --yes must never read input. Unresolved assignments fail before
saving; manual selection requires --interactive. --layout-only bypasses session
catalogs/bindings. --check reports the current capture without saving. Restore
--empty launches only ordinary shells and clears inherited pane assignments.
Keep the existing Python/SQLite/native Terminal implementation. No measured
capture success percentage is available; do not invent one.

- [x] Write failing tests for no-input contracts and layout-only bypass.
- [x] Implement strict setup, flags, empty-shell launch and regression tests.
- [x] Verify child shells, review, and run the full isolated suite.
- [ ] Ship/install and update shared documentation with exact command contracts.

Preserve unrelated dirty main-checkout edits. Never terminate by process name.


A disposable 12-pane Terminal test captured from a redirected subprocess with
stdin closed. Layout-only saved all 12 without bindings; ordinary/--yes/check
saves with unresolved sessions exited 2 without saving or prompting. The cloned
layout restored 12 ordinary shells. A default-size restore initially dropped two
splits; maximized restoration passed, motivating the large-new-window sizing fix.

Added user request: the default table/list includes both compressed and
uncompressed sessions. Explicit filters and z cycling remain available.
