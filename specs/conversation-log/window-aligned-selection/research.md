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

### What the budget can honestly respect

The exact target model, injected system prompt, tool definitions, project instructions, and compaction threshold are not available at bridge time. They must not be presented as measurable inputs. The adapter instead declares a conservative operational policy:

```text
context_tokens × usable_fraction × conservative_chars_per_token
```

`context_tokens` is the smallest supported or observed baseline the adapter is willing to promise for an unknown target model. `usable_fraction` is an explicit safety factor, not a measurement of private harness overhead. A user who knows the target model can override the default with `max_tokens`.

Current policy, dated 2026-08-23:

| target | baseline | usable fraction | default tokens | chars/token | selection chars |
|---|---:|---:|---:|---:|---:|
| Claude Code | 200,000 | 0.75 | 150,000 | 2.0 | 300,000 |
| Codex | 256,000 | 0.75 | 192,000 | 2.0 | 384,000 |

Claude Opus 5 is documented at 1M tokens, and current GPT-5.6 models at 1.05M. Those are not safe universal harness defaults: both CLIs allow model selection, and the selected model is not part of the bridge request. Claude's official context-window guide still documents 200k models. Codex's 256,000 is a declared compatibility floor dated 2026-08-23, deliberately separate from the writer's observed `CODEX_CONTEXT_WINDOW = 258_400` metadata. The policy deliberately underfills current flagship models so an unknown-model bridge remains safe.

### Counting tokens without a runtime tokenizer

`ai-sessions` remains stdlib plus `psutil`; tokenizers do not ship with it. Development script `tools/measure_token_ratio.py` uses optional `tiktoken==0.13.0` `o200k_base` over a deterministic generated corpus of prose, Python, JSON, diffs, paths/UUIDs, tool output, Unicode, dense CJK, base64, Git hashes, and minified JSON. On 2026-08-23 it measured **1.143 minimum, 2.318 full-corpus mixture, 2.696 per-class median, 6.996 maximum chars/token**. Policy rounds the realistic mixture down to **2.0** and applies the separate 0.75 usable fraction. Pure dense classes may be below 2.0, so this remains an estimate rather than a hard tokenizer bound. The OpenAI tokenizer is only a proxy for Claude; the policy does not claim the providers share a tokenizer.

The default changes intentionally for a config with no budget key: Claude selects at 300,000 characters and Codex at 384,000 — reductions of 3.17× and 2.47× from 950,000. Version-1 configs are ambiguous because old versions automatically wrote `max_chars = 950000` whenever launch mode was saved. A missing or non-integer schema version is version 1. On load, that exact version-1 machine default becomes `target-default-migrated`; any other positive legacy value is preserved verbatim. Version-2 configs treat every positive `max_chars`, including 950,000, as explicit. The note explains the migration and the README documents how to reassert an override.

### Applied budget contract

```python
@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    context_tokens: int
    usable_fraction: float
    chars_per_token: float
    source: str

@dataclass(frozen=True, slots=True)
class Budget:
    target: str
    tokens: int
    chars: int
    origin: str  # config.max_tokens | config.max_chars | target-default | target-default-migrated
    clamped: bool = False
    over_policy: bool = False
```

`resolve_budget(target, max_tokens=None, max_chars=None, migrated=False)` is the only production path to a ceiling. `LaunchConfig.bridge_max_chars_migrated` carries the otherwise-lost schema-1 migration state.

Constants are defined once: `TRUNCATION_MARKER = "\n\n[... message truncated ...]"`, `HANDOFF_NOTE_RESERVE_CHARS = 4096`, and `MIN_BRIDGE_CHARS = HANDOFF_NOTE_RESERVE_CHARS + 2 × (len(TRUNCATION_MARKER) + 1) + 2`. The final `+2` reserves a possible same-role join between the two minimum anchors; the note reserve itself must hold `len(note) + 2` for its possible join to the first message.

1. A positive, non-boolean `max_tokens` wins. Its applied floor is `max(4096, ceil(MIN_BRIDGE_CHARS / chars_per_token))` tokens and any clamp is reported.
2. Otherwise, if `migrated` is true, resolve through target policy with `origin = "target-default-migrated"`; the discarded schema-1 machine default never reaches the explicit-character branch.
3. Otherwise, a positive, non-boolean legacy `max_chars` is kept exactly; `tokens = ceil(chars / chars_per_token)` is reporting metadata only.
4. Otherwise, `tokens = floor(context_tokens × usable_fraction)` from the target adapter and `chars = floor(tokens × chars_per_token)`. Adapter registration rejects a policy whose default cannot meet `MIN_BRIDGE_CHARS`.
5. Invalid, boolean, zero, or negative config values are treated as unset, matching current forgiving config behavior.
6. The resolved `Budget` is passed unchanged through launch, selection, and note rendering. No raw ceiling or fallback travels in parallel.

If both keys are present, `max_tokens` wins and the next config save removes the ignored deprecated key. A newly saved target-default config writes neither key and uses config schema version 2. A missing-version/version-1 `max_chars = 950000` becomes `target-default-migrated`; any other explicit legacy character budget is retained exactly. When it exceeds target policy, `over_policy` makes both the handoff note and launch notice say so and tell the user to delete `max_chars` or set `max_tokens`. A legacy ceiling below `MIN_BRIDGE_CHARS` fails with `BridgeError`; it is never silently enlarged and every constructed `Budget` validates non-empty target/origin, positive tokens, and `chars >= MIN_BRIDGE_CHARS`.

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
| T2 | Except for the exact version-1 machine default, an existing positive `max_chars` keeps its exact character ceiling; it is estimated into tokens for reporting but never round-tripped through the estimate. Too-small or over-policy values are surfaced explicitly rather than silently changed |
| T2b | With neither key set, each target receives the documented target default; the intentional default reduction is stated in the handoff note and release documentation |
| T3 | The effective ceiling is derived **per target harness**, not one global constant |
| T4 | Both fit-through and overflow use the same `SelectionMetric` item-plus-join cost. If the complete flattened list fits the conversation allowance under that metric it is byte-identical; otherwise both anchors are capped marker-inclusive with `anchor_share = (Budget.chars − HANDOFF_NOTE_RESERVE_CHARS − 2) // 2`, and survivors after the first are one contiguous suffix |
| T5 | The first message of the **selected source context** survives whenever anything survives; after compaction this is not necessarily the original conversation request |
| T6 | The note is rendered from the resolved `Budget`, states the applied token estimate and character ceiling, fits its reserve including the possible join, distinguishes source/assembled counts, and discloses both dropped-message and truncated-anchor counts |
| T7 | The conversion is exercised **end to end** — `LaunchConfig.load` → production launch path → `prepare_launch` → `bridge` → selection → note → written payload |
| T8 | Existing P1 invariants R1–R10 continue to hold, including R10 (`latest_window=False`) |
| T9 | Selection preserves order, is deterministic and monotone with increasing budgets, and reports dropped source-message count before same-role merging |
| T10 | Budget policy belongs to the target `Harness`; the core contains no target-name budget branches. Selection uses a linear `SelectionMetric` with non-negative `item_cost(turn)`, `join_cost(left, right)`, and matching `truncate(turn, limit)`, all denominated in `Budget.chars`; P3 validates hook results, and P4 may replace the metric for native projection/assembly sizing |

**T4 was wrong twice in earlier drafts.** `fit()` currently caps every prepared run at `max(1000, max_chars // 2)` before it even knows whether the conversation fits, and it runs after `merge_runs`, where source messages no longer exist as units. P3 deliberately changes this: a fits-within-allowance conversation is untouched; overflow selection operates on `flatten(turns)` before same-role merging; both anchors use the post-note allowance minus their worst-case join so first and newest context can survive without exceeding the ceiling.

**T7 exists because of a named mutation.** Adding a `max_tokens` key, printing it in the note, and leaving the character path active would pass every config-level and note-level test while behavior stayed in characters. That is the same shape as P1's proxy-tested R5, so the invariant is written to forbid it.

## Options Considered

### Part A

- **A1 — token key, character key deprecated (chosen).** Add `max_tokens`; keep explicit positive `max_chars` verbatim except the schema-1 auto-written default. Both config fields use `None` for unset, `max_tokens` wins if both occur, and a pure resolver returns one immutable applied `Budget`.
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
2. ~~**What default budget per target?**~~ **Answered:** 75% of the conservative unknown-model baseline in the table above.
3. ~~**Which characters-per-token ratio?**~~ **Answered:** the 2.318 full-corpus mixture rounded down to 2.0, with dense pure-class limits and the full spread disclosed above.
4. ~~**Which side should estimation error favor?**~~ **Answered:** underfill. Immediate target compaction can discard intentionally selected context; unused capacity can be raised explicitly with `max_tokens`.

## References

- `src/ai_sessions/bridge.py` — `fit`, `DEFAULT_MAX_CHARS`, `prepare`, `merge_runs`, `bridge`
- `src/ai_sessions/config.py` — `bridge_max_chars`, `bridge_latest_window`
- `tools/measure_token_ratio.py` — reproducible development-only ratio measurement
- P1: [`../codex-compaction-window/`](../codex-compaction-window/) — corrected R5, R10, and the `PeakRecorder` harness
- Measurements taken across 21 multi-window Codex sessions available locally, 2026-08-21
- Claude context windows: https://platform.claude.com/docs/en/build-with-claude/context-windows
- Current OpenAI model contexts: https://developers.openai.com/api/docs/models
