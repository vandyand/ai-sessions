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
| K2 | Token-denominated config key; explicit legacy `max_chars` stays verbatim, except the exact version-1 machine default | Old versions auto-wrote 950,000, so schema-aware migration must distinguish that ambiguous default from version-2 explicit intent | Preserve every auto-written value and leave the target defect in place |
| K3 | Estimate with the 2.318 full-corpus mixture rounded down to 2.0 chars/token | Dense classes invalidate a prose ratio, while a pure-class minimum double-counts conservatism with the separate 0.75 harness margin; the OpenAI measurement is disclosed as a Claude proxy | Claim exact token counts or equate tokens with characters |
| K4 | Budget policy lives on each target `Harness` | The target model is unknown, so each adapter declares a conservative baseline, safety factor, and provenance without target-name branches in core | Infer private overhead or key policy by `if target == ...` |
| K5 | Select before same-role merging and drop at source-message boundaries | `prepare()` currently erases message boundaries before `fit()`; selection must retain a real unit and honest dropped count | Continue selecting merged runs |
| K6 | **Part B does not ship.** The newest window is what the source itself resumes from, so a copy matching it is correct by construction | Merging older windows creates a transcript no harness ever held, and there is no stable message identity to deduplicate on | Recover the 10–41% because the number is non-zero |
| K7 | One immutable resolved `Budget` flows from loaded config to selection and the note | This prevents config, applied ceiling, and user-facing explanation from diverging | Pass raw integers through parallel paths |
| K8 | Reserve a bounded 4,096 characters for the note and price conversation after same-role assembly; total projected payload stays under the ceiling | A fixed checked reserve is monotone, while a sequence cost includes every `\n\n` separator introduced after pre-merge selection | Treat metadata or assembly separators as free |
| K9 | A conversation that fits is byte-identical; anchor truncation exists only on overflow | The old half-budget cap changed content even when the whole conversation fit | Apply a per-message cap before checking total size |

## Success Criteria

Invariants **T1–T8** in [research.md](research.md#requirements). Each is checkable on a generated transcript, on any machine, in CI — per P1's K7.

P1's corrected **R5** still applies: anything claiming a memory property must be **measured**, not inferred from output length, using the `PeakRecorder` harness in `tests/test_codex_window.py`.

## Non-Goals

- The conversation log, per-message provenance, verbatim carry-forward — that is P4.
- Choosing native-versus-projected message cost — P3 exposes a selector cost hook; P4 supplies native provenance and target projection.
- Round-trip head tracking — that is completed P2 (`5e5503e` on `main`).
- Decoding `encrypted_content` — impossible by construction.
- Shipping a tokenizer.
- Adding a CLI budget flag. P3 has one configuration path; a future flag must resolve through the same `Budget` function rather than create another path.

## Open Questions

1. ~~Is Part B worth its risk?~~ **Answered on review: no.** Recorded in the north star as decided against.
2. ~~**What default budget per target?**~~ **Answered:** Claude 150,000 estimated tokens / 300,000 characters; Codex 192,000 / 384,000, derived from adapter baselines at a declared 0.75 usable fraction.
3. ~~**Which chars-per-token ratio?**~~ **Answered:** 2.0, below the generated full-corpus mixture of 2.318; runtime remains tokenizer-free and the Claude use is explicitly a proxy.
4. ~~**Which side should estimation error favor?**~~ **Answered:** underfill, with explicit `max_tokens` available when the user knows the target model.

## Implementation Status

- [x] Phase 3 — Adversarial review of the plan *(ran first; HIGH=4 MEDIUM=6, Part B closed)*
- [x] Phase 0 — Derive the ratio and the per-target ceiling
- [ ] Phase 1 — Per-target token budget (Part A)
- [ ] Phase 2 — Whole-message selection (Part A)
- [ ] ~~Phase 4 — Deduplicated union~~ *(decided against)*
- [ ] Phase 5 — Adversarial review of the implementation; gates release
- [ ] Phase 6 — Doc Sync
