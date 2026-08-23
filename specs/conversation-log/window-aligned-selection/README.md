---
title: "Target-Aware Token Budget and Whole-Message Selection"
status: planned
date: 2026-08-23
priority: 20
---
# Target-Aware Token Budget and Whole-Message Selection

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P3**.
Measurements and invariants: [research.md](research.md).
Phased tasks: [implementation-plan.md](implementation-plan.md).

Depends on P1 ([`../codex-compaction-window/`](../codex-compaction-window/)), shipped in v3.1.4 and corrected in v3.1.5. P2 was subsequently merged to `main` in `5e5503e`.

## What this is

The original north star bundled two changes. Review showed that only the first should ship:

**Part A — one budget, in characters, for every target.** `DEFAULT_MAX_CHARS = 950_000` counts *characters* while the thing it protects is measured in tokens, and the same ceiling is applied whatever the target is. A Claude-bound copy is therefore governed by a ceiling derived for Codex instead of by an explicit target policy.

**Part B — recovering content from older windows. Decided against on review.** P1 made the reader adopt the newest window and discard what preceded it, so the north star's "add whole windows newest-first" was not implementable as written. The review answered whether the discarded windows were worth recovering: no. See [research.md](research.md#part-b--decided-against).

## What the rejected Part B measurements said

| Finding | Consequence |
|---|---|
| Windows are **not** nested — content is dropped and rewritten between them | Older windows can hold messages the newest lacks |
| **10.1%** of user messages exist only in older windows (12.2% of characters), across 21 sessions | Typically small |
| Worst observed: newest window holds **59%** of the union (195-window session) | Occasionally large |
| The union is only fractionally bigger than the newest window (482 vs 449; 217 vs 129) | Recovery is affordable — dedup costs ~one window, not ~366 |

Those measurements made Part B worth reviewing, but did not make it correct. They describe observed local sessions, not an invariant, and a deduplicated union would still create context no harness actually held.

## Key Decisions

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| K1 | Ship Part A first, independently | It is unambiguous, self-contained, and its correctness does not depend on Part B's riskier merge rule | Bundle both, and let a contested ordering question hold up a clear fix |
| K2 | Token-denominated config key, `max_chars` still read and converted | The config should not lie about what it measures; existing configs must not silently change meaning | Keep characters and just raise the number |
| K3 | Estimate tokens with a documented per-harness ratio | Shipping a tokenizer is a dependency this tool does not take, and every estimate is wrong at the margin anyway — say so rather than imply precision | Ship a tokenizer; or keep pretending characters are the unit |
| K4 | The ceiling is derived per target harness, not one global constant | The copy lands inside a harness that has already spent context on its own prompt and tools, and that will compact on its own if crowded | One global number for every target |
| K5 | Trim at message boundaries, never mid-message | A half-message is worse than an absent one, and P1 established boundaries as the unit of selection | Continue trimming by character position |
| K6 | **Part B does not ship.** The newest window is what the source itself resumes from, so a copy matching it is correct by construction | Merging older windows creates a transcript no harness ever held, and there is no stable message identity to deduplicate on | Recover the 10–41% because the number is non-zero |
| K7 | Every invariant is exercised end to end, not at the config boundary | Adding a token key, printing it in the note, and leaving the character path live would pass config and note tests while behavior stayed in characters | Test the seam and assume the pipeline follows |

## Success Criteria

Invariants **T1–T8** in [research.md](research.md#requirements). Each is checkable on a generated transcript, on any machine, in CI — per P1's K7.

P1's corrected **R5** still applies: anything claiming a memory property must be **measured**, not inferred from output length, using the `PeakRecorder` harness in `tests/test_codex_window.py`.

## Non-Goals

- The conversation log, per-message provenance, verbatim carry-forward — that is P4.
- Round-trip head tracking — that is completed P2 (`5e5503e` on `main`).
- Decoding `encrypted_content` — impossible by construction.
- Shipping a tokenizer.

## Open Questions

1. ~~Is Part B worth its risk?~~ **Answered on review: no.** Recorded in the north star as decided against.
2. **What default budget per target?** A derivation — `target context − harness overhead − compaction margin` — produced in Phase 0, or Part A does not proceed.
3. **Which chars-per-token ratio?** Per-harness is likely insufficient: engineering transcripts mix prose, code, JSON, diffs, and tool output. Measure the spread; prefer conservative over mean.
4. **Should the estimate fail one-sided** — underfill rather than risk overfilling and triggering the target's own compaction on arrival?

## Implementation Status

- [x] Phase 3 — Adversarial review of the plan *(ran first; HIGH=4 MEDIUM=6, Part B closed)*
- [ ] Phase 0 — Derive the ratio and the per-target ceiling
- [ ] Phase 1 — Per-target token budget (Part A)
- [ ] Phase 2 — Whole-message selection (Part A)
- [ ] ~~Phase 4 — Deduplicated union~~ *(decided against)*
- [ ] Phase 5 — Adversarial review of the implementation; gates release
- [ ] Phase 6 — Doc Sync
