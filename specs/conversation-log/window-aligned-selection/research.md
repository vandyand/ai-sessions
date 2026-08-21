# Window-Aligned Selection and a Token Budget — Research

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P3**.
Depends on **P1** (`codex-compaction-window`, shipped v3.1.4/3.1.5).

## Scope note

Two changes the north star bundled together. They turn out to be independent, and the evidence below argues they should be judged separately.

## Part A — the budget is in the wrong unit

`DEFAULT_MAX_CHARS = 950_000` counts **characters**. The copy is replayed into the *target's* context window, which is measured in tokens. At roughly 3.5–4 characters per token that budget is ~240–270k tokens, against harnesses whose models carry 1M-token context windows.

The number also reads like a 1M-token budget written down in the wrong unit, which is how it ends up both mis-typed and ~4x conservative at once.

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
| T3 | The effective ceiling is derived per target harness, not one global constant |
| T4 | Trimming happens at message boundaries, never mid-message |
| T5 | The opening request survives trimming whenever anything survives |
| T6 | The note states the budget and unit actually applied |
| U1 | Carried content is the deduplicated union of all readable windows, in a deterministic order |
| U2 | Peak turns held does not grow with the number of windows — measured, per P1's corrected R5 |
| U3 | A message present in any window is present in the output, budget permitting |
| U4 | `latest_window=False` still replays the whole transcript (P1's R10 holds) |
| U5 | Existing P1 invariants R1–R10 continue to hold |

## Options Considered

### Part A

- **A1 — token key, character key deprecated (recommended).** Add `max_tokens`; keep reading `max_chars` and convert. Ratio per harness, documented as an estimate.
- **A2 — keep characters, fix only the value.** Cheapest, but leaves the config lying about what it measures and leaves the next person to rediscover it.
- **A3 — ship a tokenizer.** Rejected: dependency weight, provider disagreement, and it would still be an estimate for the target harness's overhead.

### Part B

- **B1 — deduplicated union (recommended, contingent on review).** Recovers the 10–41%, memory bounded by unique content. Cost: a merge-order rule that can produce a conversation ordering no window actually had.
- **B2 — leave P1's replace-on-boundary alone.** Zero risk, zero recovery. Defensible: the newest window is what the *source itself* is holding, and a copy that matches the source is easy to reason about.
- **B3 — carry older windows verbatim as an appendix** below the main conversation, clearly labelled, rather than merging. Recovers content without inventing an order. Ugly, but honest.

## Open Questions

1. **Is Part B worth its risk at all?** B2 has a real argument: the newest window is what Codex itself resumes from, so a copy that matches it is *correct by construction*, and recovering superseded messages may reintroduce content the source deliberately dropped. The 41% case argues the other way. **This is the main thing the adversarial review should decide.**
2. **What default token budget?** Fixing only the unit gives ~240k tokens. Anything higher is a separate judgement about target-harness headroom, and should be argued rather than assumed.
3. **Which characters-per-token ratio per harness**, and does it need measuring rather than assuming 4?
4. **Does U3 conflict with U2?** If the union is large for some session shape not present locally, budget-bounded recovery has to degrade predictably.

## References

- `src/ai_sessions/bridge.py` — `fit`, `DEFAULT_MAX_CHARS`, `prepare`, `merge_runs`, `bridge`
- `src/ai_sessions/config.py` — `bridge_max_chars`, `bridge_latest_window`
- P1: [`../codex-compaction-window/`](../codex-compaction-window/) — corrected R5, R10, and the `PeakRecorder` harness
- Measurements taken across 21 multi-window Codex sessions available locally, 2026-08-21
