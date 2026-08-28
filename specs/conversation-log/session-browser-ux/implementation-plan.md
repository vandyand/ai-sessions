# Conversation-centered session browser — implementation plan

## Phase 1: Lock contracts with characterization tests

- [ ] Define `turn_count`, `compaction_count`, and `prompt_count`, including bridge provenance,
  compacted windows, tool results, summaries, malformed tails, and legacy `message_count`.
- [ ] Add representative Claude, Codex, and OpenCode fixtures with exact activity triples and
  latest-prompt assertions before implementation.
- [ ] Define tracked-conversation, independent-thread-group, and native-child row identities.
- [ ] Characterize filtered/hidden/missing/uncertain heads and prove filtering never promotes an
  older visible member or transfers launch intent between children.
- [ ] Characterize selection, launch, rename, hide, focus, filtering, sorting, refresh, CLI, and
  JSON behavior before replacing the flat list.

## Phase 2: Activity metrics

- [ ] Extend neutral discovery/session data with compatibility-safe activity metrics.
- [ ] Implement exact streaming metrics for Claude, Codex, and OpenCode using adapter-owned
  semantics and checkpoint/fingerprint cache invalidation.
- [ ] Prove partial-tail exclusion, append continuation, rewrite/truncate invalidation, cold/warm
  cache behavior, and bounded peak memory.
- [ ] Render and search/sort the compact `xt yc zp` value; retain deprecated message-count output
  where compatibility requires it.

## Phase 3: Conversation and thread view model

- [ ] Collapse tracked materializations by utility conversation ID and choose representative
  heads only through stored lineage state.
- [ ] Enforce the ordered pipeline: full-snapshot lineage/head derivation, eligible-child filter
  projection without head substitution, then presentation-only independent grouping. A filtered
  independent singleton is a native row, not a synthetic container.
- [ ] Group same-project/same-title independent sessions without asserting ancestry.
- [ ] Add expansion/collapse, stable selection across refresh, date/time collision labels, and
  safe synthetic-row behavior.
- [ ] Give each view row an immutable stable ID and an explicit `Session | None` action target;
  containers never launch and tracked aggregates never substitute a filtered historical child.

## Phase 4: Details, statuses, and project focus

- [ ] Add the `i` details/lineage modal with full native identity and related-thread context.
- [ ] Apply precise status vocabulary to rows, details, help, and machine-readable additions.
- [ ] Add one-action selected-project focus, a visible focus indicator, and reset behavior.

## Phase 5: Reliability and stress verification

- [ ] Add property/adversarial tests for false grouping, wrong-child launch, divergence, hidden
  members, unknown harnesses, malformed legacy state, active writers, refresh races, resize, and
  selection drift.
- [ ] Cover Unicode/case/whitespace titles, symlink/repository/missing/Windows project paths,
  filtered singleton groups, selected-child disappearance, unavailable/unknown/unstable heads,
  and exact legacy `--list`/JSON compatibility.
- [ ] Stress large compacted transcripts and warm/cold refresh paths; enforce bounded memory and
  unchanged-transcript reuse.
- [ ] Run the full supported Python test/lint/format/package matrix and real read-only Nautilus
  diagnostics without launching or mutating sessions.

## Phase 6: Adversarial review and release

- [ ] Run independent code and UX adversarial review; fix every high/medium reliability finding.
- [ ] Update README, CHANGELOG, and this topic's north star; regenerate `specs/README.md`.
- [ ] Build, inspect, publish, install, and smoke-test the released package and `sessions` command.
