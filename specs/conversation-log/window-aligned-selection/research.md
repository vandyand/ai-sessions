# Target-Aware Token Budget and Whole-Message Selection — Research

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P3**.
Depends on **P1** (`codex-compaction-window`, shipped v3.1.4/3.1.5).

## Scope note

Two changes the north star bundled together. They turn out to be independent, and the evidence below argues they should be judged separately.

## Part A — one budget, in characters, for every target

`DEFAULT_MAX_CHARS = 950_000` counts **characters**. The copy is replayed into the *target's* context window, which is measured in tokens.

**Correction (adversarial review, 2026-08-21).** An earlier draft of this spec claimed the number "reads like a 1M-token budget written down in the wrong unit," making it both mis-typed and ~4x too conservative. That was asserted without checking, and it is probably false. Five lines below it sits `CODEX_CONTEXT_WINDOW = 258_400` (`src/ai_sessions/bridge.py:46`), and:

```
950,000 chars / 258,400 tokens = 3.68 chars/token
```

which is a very plausible deliberate derivation. The comment above the constant already says "the ceiling is a context budget rather than a storage one" — so it *was* meant as a context budget, and its magnitude looks right for a Codex-bound copy.

The defensible problems are narrower, and different:

1. **The unit is not named.** A budget whose comment says "context" and whose value says "characters" forces the next reader to measure it to find out. That is how the earlier draft of this spec got it wrong.
2. **One global ceiling is applied regardless of target.** A Claude-bound copy is capped at roughly Codex's context window. That is the real defect: the ceiling should follow the target.
3. **`CODEX_CONTEXT_WINDOW` is currently only session metadata** (`bridge.py:806`), not an input to budgeting, so the relationship between the two constants is implicit and undocumented.

### What the budget must actually respect

Not the model's raw context. The copy lands *inside* a harness that has already spent context on its own system prompt, tool definitions, and any project instructions, and that will run its own compaction when it gets close to full. A copy sized at the model's ceiling arrives pre-compacted, which defeats the point of choosing what crosses.

So the ceiling is: target model context, minus harness overhead, minus enough headroom that the copy does not immediately trigger the target's own compaction.

### Counting tokens without a tokenizer

`ai-sessions` is stdlib plus `psutil`. Shipping a tokenizer is out of scope, and per-provider tokenizers disagree anyway. The honest option is a documented characters-per-token ratio per harness, applied as an estimate, with the config naming its unit so the next reader does not have to measure it to find out.

## Part B — selection across windows

The north star says: carry the newest boundary's context, then add whole windows newest-first until the budget fills.

**After P1 that is not implementable as written.** `read_codex` adopts a window and discards what preceded it, so by the time selection runs there is exactly one window plus the live tail. There are no earlier windows to add.

The question is whether earlier windows hold anything worth recovering.

### Measured — do older windows hold unique content?

Across every multi-window Codex session available locally (21 sessions):

| | |
|---|---|
| user messages present **only** in older windows | **199 of 1,979 — 10.1%** |
| characters only in older windows | 97,856 of 800,834 — **12.2%** |

Per session it ranges from nothing to a lot:

| session | windows | newest window | union of all windows | newest as % of union |
|---|---:|---:|---:|---:|
| 195-window | 195 | 129 | 217 | **59%** |
| 114-window | 114 | 47 | 60 | 78% |
| 11-window | 11 | 42 | 53 | 79% |
| 366-window | 366 | 449 | 482 | 93% |
| most others | 7–18 | — | — | 89–96% |

So windows are **not** nested. They grow — 189 of 194 transitions in the largest session are non-shrinking — but content is dropped and rewritten between them, so a message present in window *k* may be absent from window *k+1*.

### The union is cheap

The decisive number is the last column above: the union is only fractionally larger than the newest window. 482 versus 449; 217 versus 129. Retaining a **deduplicated union of carried messages** therefore costs on the order of one window, and — critically — does **not** grow with the number of windows. 366 windows of ~465 messages is ~170,000 turns if accumulated naively, and 482 if deduplicated.

That is what makes recovering the missing 10–41% affordable without reintroducing the memory problem P1's R5 exists to prevent.

### The cost of the union: ordering

Deduplication needs a merge rule. Windows disagree about content, so they can disagree about order. A rule is needed that is deterministic and does not interleave a recovered old message into the middle of a later exchange in a way that reads as a conversation that never happened.

The conservative rule: **anchor on the newest window's order**, and insert messages found only in older windows at the position implied by their nearest surviving neighbour in the older window. Where that is ambiguous, prefer placing recovered content *before* the newest window's content rather than interleaving it.

This is the part of Part B most likely to be wrong, and it is why Part B is separable from Part A.

## Requirements

Invariants over an arbitrary transcript, in the style established by P1.

| # | Invariant |
|---|---|
| T1 | The budget names its unit. Config carries a token-denominated key |
| T2 | An existing `max_chars` config keeps working, converted, with no silent change of meaning |
| T3 | The effective ceiling is derived **per target harness**, not one global constant |
| T4 | **Selection** drops whole messages; a message is truncated only when it alone exceeds the budget, and then carries the existing marker |
| T5 | The opening request survives trimming whenever anything survives |
| T6 | The note states the budget and unit actually applied |
| T7 | The conversion is exercised **end to end** — `prepare_launch` → `bridge` → `fit` → `handoff_note` — not just at the config boundary |
| T8 | Existing P1 invariants R1–R10 continue to hold, including R10 (`latest_window=False`) |

**T4 was wrong in the first draft.** It said "never trim mid-message." `fit()` already caps any turn at `max(1000, max_chars // 2)` and truncates with a `[... message truncated ...]` marker (`src/ai_sessions/bridge.py:601-608`), and `test_oversized_single_turn_is_truncated_not_dropped` (`tests/test_bridge.py:261`) blesses that deliberately — a truncated oversized message beats an absent one. As written, T4 contradicted shipped behavior *and* T5, since an oversized opening request cannot both survive whole and be under budget. Restated above to say what actually matters: whole-message granularity for the *selection* decision, truncation only as the single-message escape hatch.

**T7 exists because of a named mutation.** Adding a `max_tokens` key, printing it in the note, and leaving the character path active would pass every config-level and note-level test while behavior stayed in characters. That is the same shape as P1's proxy-tested R5, so the invariant is written to forbid it.

## Options Considered

### Part A

- **A1 — token key, character key deprecated (recommended).** Add `max_tokens`; keep reading `max_chars` and convert. Ratio per harness, documented as an estimate.
- **A2 — keep characters, fix only the value.** Cheapest, but leaves the config lying about what it measures and leaves the next person to rediscover it.
- **A3 — ship a tokenizer.** Rejected: dependency weight, provider disagreement, and it would still be an estimate for the target harness's overhead.

### Part B — decided against

- **B1 — deduplicated union.** **Rejected on review.** It creates a transcript no harness ever held, promoting stale and superseded user text back into live context.
- **B2 — leave P1's replace-on-boundary alone. Chosen.** `replacement_history` is explicitly the context Codex resumes from, and P1 adopted it as replacement semantics deliberately. A copy that matches the source is correct by construction.
- **B3 — a labelled appendix of older windows.** Acceptable only as an explicit, clearly-marked export, never as merged conversation context. Out of scope here.

Two further objections made B1 unsalvageable even setting the semantics aside:

- **U2 and U3 were mutually false.** Given N windows each holding one unique older-only message, recovering every message (U3) requires O(N) retained turns, so peak-independent-of-window-count (U2) fails. The local measurement that made the union look cheap is a property of *observed* sessions, not of arbitrary transcripts — exactly the machine-specific reasoning P1 was corrected for.
- **There is no stable message identity to deduplicate on.** `Turn` carries role, text, calls, and a compaction flag; provenance is P4. Deduplicating by text collapses genuinely repeated messages — "yes", "continue", "retry" — and deduplicating by position fails across the rewrites that make windows non-nested in the first place.

## Open Questions

1. ~~**Is Part B worth its risk?**~~ **Answered: no.** B2 chosen. Recorded in the north star's *explicitly decided NOT to do* section.
2. **What default budget per target?** Not "fix the unit and maybe raise later" — a derivation: `target model context − harness overhead − compaction margin`. Phase 0 produces it or Part A does not proceed.
3. **Which characters-per-token ratio?** Per-harness is likely not enough. Engineering transcripts mix prose, code, JSON, paths, UUIDs, diffs, and tool output; a prose-derived ratio can overfill the target and trigger immediate compaction. Measure the spread, and prefer a conservative ratio over a mean.
4. **Where does the estimate cause real harm** rather than mild imprecision, and does the failure mode need to be one-sided (underfill rather than overfill)?

## References

- `src/ai_sessions/bridge.py` — `fit`, `DEFAULT_MAX_CHARS`, `prepare`, `merge_runs`, `bridge`
- `src/ai_sessions/config.py` — `bridge_max_chars`, `bridge_latest_window`
- P1: [`../codex-compaction-window/`](../codex-compaction-window/) — corrected R5, R10, and the `PeakRecorder` harness
- Measurements taken across 21 multi-window Codex sessions available locally, 2026-08-21
