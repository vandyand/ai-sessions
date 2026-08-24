# OpenCode adapter completion audit

Audit date: 2026-08-23. Scope: the active user objective, this spec, every item in the
implementation plan, and the release state of PR #17. A passing test is cited only where its named
fixture or verifier exercises the requirement in question.

## Verdict

**Complete, subject to the final documentation head retaining green CI.** No implementation,
validation, review, documentation, or research requirement is missing. The code head `23099cf`
passed both GitHub Actions runs `32685523398` and `32685526738`; the final docs-only head must pass
the same required matrix before the active goal is closed.

## Active objective

| Requirement | Evidence | Result |
| --- | --- | --- |
| Work through every remaining planned phase | Every Phase 0–5 implementation item is checked; the detailed evidence below maps each phase to code, tests, real-provider gates, review, and delivery. | Proven |
| Implement OpenCode as a production third harness | Commits `a30e93d` through `a5cd43d` implement the generic native identity/checkpoint extension and the complete OpenCode adapter; `a01118d` completes release hardening and docs. | Proven |
| Fully exercise Claude, Codex, and OpenCode in every direction | `test_all_six_ordered_bridge_directions_reread_native_semantics` validates all six ordered cross-harness pairs without filtering extra native turns. Same-harness exact identity and the routing/head matrix cover all three native resumes. | Proven |
| Make cross-harness behavior reliable and robust | 462 tests cover malformed/partial/concurrent storage, compaction/revert transitions, import authority/remint/timeout/cleanup, exact availability, divergence, and unstable state. Native Windows and WSL suites plus both real-OpenCode verifiers pass. | Proven |
| Request regular Claude Opus 5 plan/code reviews | Reviews ran after the plan, storage, reader, writer/liveness, six-direction matrix, real CLI gates, and final integrated diff. The last two passes returned GO; the final follow-up killed every named remaining mutation and reported no findings. | Proven |
| Research and recommend two or three next harnesses | [`../NEXT_HARNESSES.md`](../NEXT_HARNESSES.md) ranks Gemini CLI, Qwen Code, and GitHub Copilot CLI from current provider docs/source and records candidate-specific Phase 0 gates. | Proven |

## Spec objective and harness contract

| Requirement | Authoritative evidence | Result |
| --- | --- | --- |
| Discover OpenCode roots, children, and archived sessions | `opencode.discover`; captured/temporary SQLite fixtures; native child/archive provider probes; matrix/UI discovery tests. | Proven |
| Search, list, detail, hide, rename, and configure OpenCode through shared UI paths | OpenCode matrix tests exercise filtered/list/JSON/detail surfaces, hidden state, publication, configuration, provider selection, and generic registry rendering. | Proven |
| Read full and latest native semantics | Reader fixtures and SQLite integration cover message/part order, text/reasoning/tools, staged revert, completed compaction, retained tails, unknown kinds, and atomic semantic checkpoints in both modes. | Proven |
| Write a visible, model-resumable native session | Writer uses official `opencode import`, dynamically selects the model/budget, verifies exact persisted content and authoritative ID/database, and rereads the result. Real Linux and Windows imports pass. | Proven |
| Resume the exact native identity | Command construction and real-provider checks prove exact root, child, archived-root, and imported-session IDs. OpenCode children never silently resume a parent. | Proven |
| Detect liveness without guessing | Exact command-line session-ID matching, wrapper/process-tree handling, bounded maintenance cleanup, and Windows/POSIX lifecycle tests; bare processes deliberately remain unknown. | Proven |
| Use one neutral route, no pairwise converter/provider branch | `test_source_structure.py`, the independent late-registered fourth harness, registry contract tests, and all-six-direction matrix prove the core remains adapter-neutral. | Proven |
| Preserve utility conversation identity and route the newest head | Schema-6 state, opaque checkpoints, equivalence reuse, source/target advancement, bridge-back, missing/unavailable rows, checkpoint-less members, and divergence are covered in matrix/shared-store tests. | Proven |

## Phase evidence

| Phase | Evidence inspected | Result |
| --- | --- | --- |
| 0 — contract extension | `NativeRef`, `ReadSnapshot`, `NativeWrite`, opaque `Checkpoint`, tri-state availability, dynamic preparation, schema-6 compatibility, and the independent shared-store harness; commit `a30e93d`. | Complete |
| 1 — storage/discovery | Command-authoritative `db path`, candidate isolation, schema/table validation, bounded reads, native ordering, malformed/locked/oversized behavior, cache generations, roots/children/archive; commit `0a8c2bc`. | Complete |
| 2 — semantic reader | Provider-pinned semantic parser, tool states, revert, completed-only compaction, full/latest projection, session-local digest, atomic snapshot/change status, fail-closed future kinds; commit `666c7d3`. | Complete |
| 3 — writer/resume/liveness | Dynamic model budget, provider-owned import, ID generation/remint recovery, preflight/conflict/authority verification, temporary-file hygiene, transactional title, exact resume, bounded process cleanup; commit `ac43383`. | Complete |
| 4 — three-harness matrix | Six ordered pairs, three native identities, UI/config/discovery, head reuse/advance/return/divergence, shared-store identity, unstable checkpoints, real child/archive resume; commit `98c04e0`. | Complete |
| 5 — real CLI/release | Repeatable Linux model-backed and Windows maintenance verifiers, real compaction differential, final mutation gates, Opus GO, v3.2.0 docs/artifacts, PR #17 and green code-head CI; commits `a5cd43d`, `a01118d`, `23099cf`. | Complete |

## Final validation evidence

- Native Windows: 462 tests passed, one expected platform skip.
- Ubuntu WSL: 462 tests passed, three expected platform skips.
- GitHub Actions code head `23099cf`: runs `32685523398` and `32685526738` each passed lint,
  package, Ubuntu Python 3.11/3.12/3.13, and Windows Python 3.11/3.12/3.13.
- Ruff 0.16.3 lint and format checks plus `git diff --check`: pass.
- v3.2.0 source and wheel builds plus Twine validation: pass; the source distribution contains the
  changelog and both release verifiers.
- `verify_opencode_real.py` against OpenCode 1.18.21 and `opencode/big-pickle`: native read,
  compaction order, Claude/Codex import/export/reread, exact semantic advancement, bridge-back, and
  head reuse all pass in disposable storage.
- `verify_opencode_maintenance.py` against native Windows OpenCode 1.18.21: database authority,
  official import, discovery/reread, export, and exact safe/dangerous resume arguments pass in
  disposable storage.
- Final Opus review: GO, no HIGH; follow-up mutation run: GO with no remaining findings.

## Deliberate non-goals and residual risk

- Per-message original-form provenance remains the separate conversation-log P4 goal; this release
  completes harness/storage decoupling and neutral rematerialization, as scoped.
- Tool calls cross as inert summaries, not executable target-native calls.
- Unknown future OpenCode semantic kinds block bridging with a warning until classified.
- Rollback to 3.1.5 is reasoned rather than executed against old code: schema remains 6, legacy
  cursors are preserved, and unknown checkpoint-less members fail conservatively.
- Cursor browser rendering has lower line coverage than the adapter core, but provider-neutral UI
  behavior is pinned by late-registration and OpenCode matrix tests.

These are documented design boundaries, not missing acceptance criteria.
