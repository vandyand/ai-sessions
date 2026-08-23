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

## Phase 0: Measure the ratio and the ceiling

Nothing is designed on an assumed constant.

- [ ] Measure characters-per-token empirically on real carried windows for both harnesses, using whatever counting is available without adding a dependency; record the spread, not just the mean
- [ ] Establish what a target harness actually has available: model context minus its own system prompt and tool definitions, and where its own compaction triggers
- [ ] Decide the default budget from those two numbers and **write the derivation down** — Open Question 2 is answered here or not at all
- [ ] Record findings under `### Phase 0 findings`

**Bail condition:** if the ratio varies enough between harnesses that one constant is misleading, the config needs a per-harness ratio rather than a global one — decide before Phase 1.

---

## Phase 1: Token-denominated budget (Part A)

- [ ] Add a token-denominated key to `[bridge]` in `config.py`; keep reading `max_chars` and convert it, so an existing config does not silently change meaning (T2)
- [ ] Derive the effective ceiling per target harness rather than one global constant (T3)
- [ ] Thread the budget through `bridge()` into `fit`
- [ ] The handoff note states the budget and the unit actually applied (T6)
- [ ] Deprecation path for `max_chars` documented in the README config section

**Verification**

```python
# a config carrying only max_chars yields the same effective ceiling as before
# a config carrying the token key yields the derived ceiling
# the rendered note names the unit
```

---

## Phase 2: Boundary-aligned trimming (Part A)

- [ ] Selection drops **whole messages**; truncation with the existing marker remains the escape hatch for a single message that alone exceeds the budget (T4)
- [ ] The opening request survives whenever anything survives (T5)
- [ ] Confirm behavior is unchanged for transcripts that fit the budget — this phase must be a no-op for the common case
- [ ] Tests on generated transcripts, including one where a single message alone exceeds the budget — and an end-to-end test through `prepare_launch` → `bridge` → `fit` → `handoff_note` that fails if the character path is still live (T7)

**Verification**

```python
# no output message is a prefix or suffix of an input message
# turns[0] of the input is present in the output whenever output is non-empty
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
- [ ] Regenerate the spec index
- [ ] Commit: `docs(conversation-log): sync docs after window-aligned-selection`

---

## Rollback

The implementation touches `config.py` and `bridge.py`'s budget and selection plumbing. Reverting restores the character budget and character-slice selection with no data migration, since the legacy config key is read but provider data is never rewritten. Part B is closed and must not alter the reader.
