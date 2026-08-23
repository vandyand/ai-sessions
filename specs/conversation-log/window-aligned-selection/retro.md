# Target-Aware Budget and Whole-Message Selection Retro

P3 completed in `e15f73f` after three adversarial implementation-review passes by Claude
Opus 5. The final pass returned **CLEAN — HIGH=0, MEDIUM=0**. The full local gate at that
point was 164 unit tests plus Ruff 0.16.3 lint and format checks.

## What changed during implementation review

- The first implementation review found that tests stopped below the production
  `LaunchConfig.load → launch(dry_run=True) → bridge` path. End-to-end fixtures now prove a
  5,000-token setting produces a 10,000-character applied ceiling and that an unset setting
  uses the target default.
- A uniform suffix fixture could not distinguish `break` from `continue`. A nonuniform
  middle message now proves selection stops at the first non-fitting suffix boundary.
- Equal independent caps for the first and newest anchors violated monotonicity: at one
  boundary, adding one character of budget dropped a complete middle message. The first
  anchor now takes its initial share and the newest takes the remaining anchor allowance.
- Tests originally asserted only that a newest anchor was marked truncated, not how much of
  it survived. Exact retained length now pins the remaining-allowance rule.
- Tool-call metadata was tested only with a wide cut. A separator-boundary fixture now kills
  the one-character offset mutation that would count a partially rendered call as complete.

## Review lows

The final CLEAN pass reported four LOW observations. All were closed before doc sync:

- Tail pricing now has a truncated-head fixture, preventing accidental use of the original
  oversized head cost.
- `BudgetPolicy` tests cover a derived default below `MIN_BRIDGE_CHARS`.
- Provenance JSON uses the same non-ASCII rendering policy as its bounded fields, and the
  hostile-metadata fixture includes non-ASCII, quotes, slashes, controls, and newlines.
- The selected head cost is computed once and reused for newest allocation and tail pricing.

## What not to repeat

- Do not infer behavioral coverage from resolver or note tests when the user-facing launch
  path may bypass them.
- Do not test ordering invariants with uniform message sizes; uniform data hides gap-tail
  selectors.
- Do not treat a sampled monotonic sweep as proof at a cap transition. Add the exact adjacent
  budgets on both sides of the transition.
- Do not merge older Codex replacement windows. They are superseded native context without
  stable message identity, not free history waiting to be recovered.
