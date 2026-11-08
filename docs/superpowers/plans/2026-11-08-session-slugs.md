# Session Slugs Implementation Plan

For agentic workers: implement tasks with the executing-plans or subagent-driven-development skill. Preserve the existing project release workflow.

**Goal:** Unique stable session slugs accepted interchangeably with IDs across the CLI.
**Architecture:** A shared, provider-neutral reference resolver and transactionally unique slug registry; thin integrations for ordinary actions, supervisor logs and native messaging.
**Tech Stack:** Existing Python 3.11+ project, standard-library SQLite, unittest.
**Spec:** ../specs/2026-11-08-session-slugs.md

## Global constraints

- No provider-name literals in generic modules. No secret handling or daemon restarts.
- Preserve CRLF where present and write UTF-8 explicitly.
- Keep title-first 60-column layout and fast cached startup; no writes during rendering.
- Never send messages to user sessions during tests. Use mocked delivery or exact disposable sessions only.
- Preserve qualified home/generation identity and existing branch divergence protections.

## Review focus

- Concurrent launches allocating the same title must not collide or change earlier slugs.
- Renames and old state writers must not reset slugs; retired slugs must not select another session.
- Duplicate IDs/homes or slug/name collisions must never cause an unintended action.
- Cached startup, hidden/compressed sessions and offline messaging must retain their behavior.
- Every session-reference argument must accept the same slug, while message IDs remain unchanged.

### Task 1: Shared identities, store and resolver (parent)
- [x] Add failing unittest cases for uniqueness, persistence, concurrency, validation and exact resolution.
- [x] Implement references.py and Session.slug using the API in the spec.
- [x] Verify focused tests, including no action on ambiguous references.

### Task 2: State/catalog/display integration (parent)
- [x] Batch-assign slugs during state application after display-name choices and supervisor naming.
- [x] Include slugs in search/JSON/details and make plain list output discoverable without losing titles.
- [x] Verify startup/navigation and display regression tests and registry throughput.

### Task 3: Ordinary CLI and supervisor log references (worker)
- [x] Add failing tests for every existing ID-taking action and custom slug assignment.
- [x] Use shared resolver, add --set-slug, translate target-log slugs without forcing discovery.
- [x] Verify same-harness IDs and divergence behavior remain unchanged.

### Task 4: Messaging references (worker)
- [x] Add failing slug receiver/sender/status tests, including duplicate homes and offline refusal.
- [x] Assign slugs to live rows using the common registry and preserve native receipt/envelope addresses.
- [x] Verify existing reply-correlation and generation safeguards.

### Task 5: Review and ship (parent)
- [x] Review combined changes and run full tests, lint and formatting. Package validation runs in CI.
- [ ] Update README/changelog and agent messaging reference.
- [ ] PR, green Windows/Linux CI, squash merge, release, verify PyPI and install locally.

## Verification record

2026-11-08: 1,168 tests passed, 2 platform skips, 239 subtests. Native CLI subprocesses with isolated provider/config/state homes proved duplicate names receive distinct slugs and that rename, resume dry-run, hide/unhide, compression, supervision, target logs and offline messaging use the expected identity. A 6,000-identical-name allocation benchmark took 111 ms initially and 23 ms warm after eliminating quadratic suffix searches. Review regressions cover retired-name fallback, stale loaded rows after another process changes a slug, native-ID collisions and copy slug inheritance.
