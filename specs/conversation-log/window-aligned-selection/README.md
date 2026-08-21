---
title: "Window-Aligned Selection and a Token Budget"
status: planned
date: 2026-08-21
priority: 20
---
# Window-Aligned Selection and a Token Budget

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P3**.
Measurements and invariants: [research.md](research.md).
Phased tasks: [implementation-plan.md](implementation-plan.md).

Depends on P1 ([`../codex-compaction-window/`](../codex-compaction-window/)), shipped in v3.1.4 and corrected in v3.1.5.

## What this is

The north star bundled two changes. The evidence says they are independent and carry very different risk, so this spec keeps them separable and ships them in that order.

**Part A — the budget is in the wrong unit.** `DEFAULT_MAX_CHARS = 950_000` counts *characters* while the thing it is protecting — the target's context window — is measured in tokens. At ~3.5–4 chars/token that is ~240–270k tokens against 1M-token models. It reads like a token budget written down in the wrong unit.

**Part B — recovering content from older windows.** P1 made the reader adopt the newest window and discard what preceded it, so the north star's "add whole windows newest-first" is not implementable as written: by selection time there is one window and a tail. The open question is whether the discarded windows held anything worth recovering.

## What the measurements say

| Finding | Consequence |
|---|---|
| Windows are **not** nested — content is dropped and rewritten between them | Older windows can hold messages the newest lacks |
| **10.1%** of user messages exist only in older windows (12.2% of characters), across 21 sessions | Typically small |
| Worst observed: newest window holds **59%** of the union (195-window session) | Occasionally large |
| The union is only fractionally bigger than the newest window (482 vs 449; 217 vs 129) | Recovery is affordable — dedup costs ~one window, not ~366 |

That last row is what makes Part B thinkable at all: a deduplicated union does not reintroduce the memory problem P1's R5 exists to prevent.

## Key Decisions

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| K1 | Ship Part A first, independently | It is unambiguous, self-contained, and its correctness does not depend on Part B's riskier merge rule | Bundle both, and let a contested ordering question hold up a clear fix |
| K2 | Token-denominated config key, `max_chars` still read and converted | The config should not lie about what it measures; existing configs must not silently change meaning | Keep characters and just raise the number |
| K3 | Estimate tokens with a documented per-harness ratio | Shipping a tokenizer is a dependency this tool does not take, and every estimate is wrong at the margin anyway — say so rather than imply precision | Ship a tokenizer; or keep pretending characters are the unit |
| K4 | The ceiling is derived per target harness, not one global constant | The copy lands inside a harness that has already spent context on its own prompt and tools, and that will compact on its own if crowded | One global number for every target |
| K5 | Trim at message boundaries, never mid-message | A half-message is worse than an absent one, and P1 established boundaries as the unit of selection | Continue trimming by character position |
| K6 | **Part B is contingent on adversarial review** — see Open Question 1 | There is a real argument that the newest window is correct *by construction*, because it is what the source itself resumes from | Assume recovery is desirable because the number is non-zero |

## Success Criteria

Invariants T1–T6 (Part A) and U1–U5 (Part B) in [research.md](research.md#requirements). Each is checkable on a generated transcript, on any machine, in CI — per P1's K7.

Part B additionally inherits P1's corrected **R5**: peak turns held must be **measured**, not inferred from output length, using the `PeakRecorder` harness from `tests/test_codex_window.py`.

## Non-Goals

- The conversation log, per-message provenance, verbatim carry-forward — that is P4.
- Round-trip head tracking — that is P2, still unstarted.
- Decoding `encrypted_content` — impossible by construction.
- Shipping a tokenizer.

## Open Questions

1. **Is Part B worth its risk?** The newest window is what Codex itself resumes from, so a copy matching it is correct by construction, and recovering superseded messages may reintroduce content the source deliberately dropped. Against that: 41% missing in the worst observed session. **This is the question for review, and Part B does not proceed until it is answered.**
2. **What default budget?** Fixing only the unit yields ~240k tokens. Raising it is a separate judgement about headroom and must be argued.
3. **Which chars-per-token ratio per harness** — measured or assumed?
4. **How does U3 degrade** when the union does not fit the budget?

## Implementation Status

- [ ] Phase 0 — Measure the ratio and the real budget ceiling
- [ ] Phase 1 — Token-denominated budget (Part A)
- [ ] Phase 2 — Boundary-aligned trimming (Part A)
- [ ] Phase 3 — Adversarial review gate on Part B
- [ ] Phase 4 — Deduplicated union, only if Phase 3 says yes (Part B)
- [ ] Phase 5 — Doc Sync
