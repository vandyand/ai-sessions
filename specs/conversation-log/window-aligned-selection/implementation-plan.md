# Target-Aware Token Budget and Whole-Message Selection — Implementation Plan

See [README.md](README.md) for decisions and [research.md](research.md) for measurements and invariants.

## Overview

Part A only. Part B was closed by the Phase 3 review before any code was written — see the findings below.

The plan review ran **first**, not after Phases 0–2 as originally sequenced. That was itself a review finding: gating only Part B left the budget, default, and trimming decisions — all Part A, and all wrong in the first draft — behind no gate at all.

## Prerequisites

- Branch off `main` at or after `5e5503e` (merged P2).
- **No external fixture.** Generated transcripts only, per P1's K7. `make_codex_rollout` and `PeakRecorder` already exist in `tests/test_codex_window.py` and should be reused rather than reinvented.
- Verification with `PYTHONPATH=$PWD/src`.

## Verification mechanism

Direct `python -` against the working tree. Each phase states the expression and expected result. Narrow command: `python -m unittest tests.test_codex_window tests.test_bridge -v`. Broad suite is a preflight/CI gate.

---

## Phase 0: Derive a reproducible conservative policy — COMPLETE

- [x] Add `tools/measure_token_ratio.py`, a deterministic generated-corpus measurement using optional development-only `tiktoken`; no runtime dependency
- [x] Record the spread by content class and choose a conservative value rather than a mean
- [x] Replace unknowable harness overhead/compaction internals with an explicit adapter-owned `usable_fraction`
- [x] Define the unknown-model baseline and source for both current adapters
- [x] Decide that estimation fails toward underfill and document the intentional unset-default migration

### Phase 0 findings

- `o200k_base` across prose, Python, JSON, diffs, paths/UUIDs, tool output, and Unicode measured 2.387 minimum, 2.572 p10, 3.788 median, and 6.996 maximum chars/token. Policy uses **2.5**.
- `usable_fraction = 0.75` is a declared safety factor, not a measurement of private harness state.
- Claude policy uses a 200,000-token unknown-model baseline; Codex uses the smaller 258,400-token context recorded by supported rollouts. Current flagship model limits are larger, but the bridge cannot know the model selected at resume time.
- Defaults: Claude **150,000 estimated tokens / 375,000 characters**; Codex **193,800 / 484,500**.
- A saved legacy `max_chars` remains verbatim. A truly unset config intentionally adopts the smaller target default and the handoff/release documentation says so.

---

## Phase 1: One resolved target budget (Part A)

- [ ] Add immutable `BudgetPolicy` and `Budget`; put one policy on every `Harness` and extend the registry self-consistency test (T3/T10)
- [ ] Make `bridge_max_tokens` and `bridge_max_chars` independently optional so unset differs from explicit legacy configuration (T2/T2b)
- [ ] Implement one pure resolver: positive `max_tokens` wins and is clamped to a documented 4,096-token minimum; otherwise positive `max_chars` is kept verbatim; otherwise use target policy. Invalid/bool/non-positive values are unset. No resolved budget is non-positive
- [ ] `LaunchConfig.as_toml()` writes `max_tokens` when set, otherwise preserves an explicitly loaded `max_chars`, and writes neither for a target-default config; never write both
- [ ] Add load → save → load tests that preserve the effective budget and do not resurrect `max_chars`
- [ ] Replace `prepare_launch(max_chars=...)` and `bridge(max_chars=...)` with the resolved `Budget`; remove the `or DEFAULT_MAX_CHARS` fallback so there is one path
- [ ] Render the handoff note from the same `Budget` object selection used; report origin, estimated tokens, character ceiling, and intentional target default (T6)
- [ ] Document `max_tokens`, precedence, legacy preservation, and default migration in the repo README

**Verification**

```python
# missing config resolves differently for Claude and Codex at documented defaults
# max_chars=950000 resolves to exactly 950000 chars for both targets
# max_tokens resolves through the target policy and wins if both keys exist
# load(max_tokens only) -> save -> load preserves the same Budget and never adds max_chars
# the note is rendered from the applied Budget, including a deliberately clamped case
```

---

## Phase 2: Source-message selection before merging (Part A)

- [ ] Split preparation into `flatten(turns)` → selection → `merge_runs(survivors)` so source messages remain the selection/counting unit (T4/T9)
- [ ] If the complete flattened conversation fits, return it byte-identical with zero drops; only the overflow path applies the half-budget anchor cap (T4/K9)
- [ ] On overflow keep the first message of selected source context and a contiguous newest tail in source order. Cap an anchor, marker included, only as needed to reserve room for both ends
- [ ] Count dropped pre-merge source messages exactly; summarised tool-call count comes only from surviving messages
- [ ] Include the exact rendered note and merge separators in total projected-payload cost. Bound displayed title/cwd fields, then use a monotone fixed-point/reservation loop so selection can only shrink until payload fits; raise `BridgeError` when an explicit legacy character ceiling cannot hold the required note (T6/K8)
- [ ] Give the selector a `cost: Callable[[Turn], int]` seam for P4 without adding provenance or native projection in P3 (T10)
- [ ] Add property-style cases for membership/truncation shape, order, head/tail, fit-through, exact count, monotonicity, determinism, empty/single/all-oversized inputs, and consecutive same-role runs
- [ ] Add an end-to-end test beginning at `LaunchConfig.load`, using the production launch budget path through the target writer, and assert the written projected payload plus note is within the applied ceiling (T7)

**Verification**

```python
# every survivor equals an input message or one input prefix plus the exact marker
# order is preserved; first selected-context message survives; newest survives when two fit
# dropped == input messages absent before merge_runs
# raising the budget never decreases survivors or increases drops
# same input + same Budget produces byte-identical output
```

---

## Phase 3: Adversarial review of the plan — COMPLETE

- [x] Put the Part B question to the reviewer with the measurements
- [x] Put the merge-ordering rule to the reviewer specifically
- [x] Record the verdict and rationale below
- [x] Close Part B as decided-against

### Phase 3 findings

**Verdict: HIGH=4 MEDIUM=6.** Ran 6m11s with no timeout, before any implementation.

- **Part B closed.** `replacement_history` is the context the source resumes from; merging windows creates a transcript no harness ever held and promotes superseded user text back into live context. B2 chosen; B3 acceptable only as a labelled export, never as merged context.
- **U2 and U3 were mutually false.** N windows each holding one unique older-only message force O(N) retained turns to satisfy U3, breaking U2. The local measurement that made the union look cheap describes observed sessions, not arbitrary ones — the same machine-specific reasoning P1 was corrected for.
- **No stable message identity exists to deduplicate on.** `Turn` has no provenance (that is P4); text-dedup collapses genuine repeats like "yes" or "continue".
- **T4 contradicted shipped behavior and T5.** `fit()` already truncates oversized turns with a marker (`bridge.py:601-608`) and a test blesses it (`tests/test_bridge.py:261`). Restated.
- **The Part A premise was wrong.** `CODEX_CONTEXT_WINDOW = 258_400` sits five lines below `DEFAULT_MAX_CHARS = 950_000`, and 950,000/258,400 = 3.68 chars/token. The budget was probably derived deliberately for a Codex-bound copy, not mis-typed from a 1M-token window. The real defect is one global ceiling for every target.
- **Token estimation is not merely imprecise.** Engineering transcripts mix prose, code, JSON, diffs, and tool output; a prose-derived ratio can overfill a target and trigger its compaction on arrival.
- **T1/T2/T3/T6 were proxy-prone.** Named mutation: add `max_tokens`, print it in the note, leave the character path live — config and note tests pass, behavior unchanged. Now forbidden by T7.
- **The gate was mis-placed.** Fixed: this review ran before Phase 1.

### Phase 3b — Opus 5 review after P2 merge

**Verdict: NOT CLEAN — HIGH=6 MEDIUM=10.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed commit `bbabab0` read-only on 2026-08-23. The review artifact is local at `C:\Users\vandy\.claude\plans\act-as-an-adversarial-valiant-milner.md`; all actionable findings are incorporated above.

- [x] Distinguish unset from explicit legacy config and cover default migration
- [x] Preserve token configuration through `save()`
- [x] Make the resolver total and keep non-positive values out of the applied path
- [x] Replace unreproducible private-overhead measurement with generated evidence plus a declared safety factor
- [x] Move selection before `merge_runs` and count real source messages
- [x] Correct fit-through, note-cost, note-honesty, adapter-policy, unknown-model, verification, compaction wording, production-call-path, content-variance, and P4-seam findings
- [ ] Re-run Opus 5 against the revised plan and require no HIGH or MEDIUM findings before Phase 1 implementation

---

## Phase 4: Deduplicated union — CLOSED, NOT IMPLEMENTED

Decided against in Phase 3. Recorded in the north star's *explicitly decided NOT to do* section so it is not rediscovered as an idea.

---

## Phase 5: Adversarial review of the implementation

**Nothing is released until this returns.** Phase 3 reviews the plan; this reviews the code that came out of it. Both gates exist because P1 ran neither before shipping and paid for it: a documented option silently stopped working, and the invariant the design rested on was both mis-stated and proxy-tested.

- [ ] Re-run the reviewer against the actual diff, with the spec as context, and with **no timeout** — the first P1 attempt died in a 340s wrapper and the completed run took 6m22s
- [ ] Ask specifically for a mutation the test suite would not catch. That question found the proxy-tested invariant in P1 and is the highest-yield thing to ask
- [ ] Ask whether the shipped behavior matches what the spec and the handoff note claim
- [ ] Fix every HIGH and MEDIUM before release; record LOW items in the retro
- [ ] Record the verdict under `### Phase 5 findings`

---

## Phase 6: Doc Sync

- [ ] Update the repo `README.md` — the Budget section states characters and will be wrong
- [ ] Update [`../NORTH_STAR.md`](../NORTH_STAR.md) — mark P3 complete with the SHA, add `### P3 observations`, and record Part B's outcome either way
- [ ] Update [`../HARNESS_CONTRACT.md`](../HARNESS_CONTRACT.md) with the adapter-owned budget policy and selector-cost seam
- [ ] Regenerate the spec index
- [ ] Commit: `docs(conversation-log): sync docs after window-aligned-selection`

---

## Rollback

The implementation touches `config.py` and `bridge.py`'s budget and selection plumbing. Reverting restores the character budget and character-slice selection with no data migration, since the legacy config key is read but provider data is never rewritten. Part B is closed and must not alter the reader.
