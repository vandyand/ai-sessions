# Window-Aligned Selection and a Token Budget — Implementation Plan

See [README.md](README.md) for decisions and [research.md](research.md) for measurements and invariants.

## Overview

Six phases. Part A (Phases 0–2) ships independently. Part B (Phase 4) is gated on Phase 3.

## Prerequisites

- Branch off `main` at or after `0ea8e69` (v3.1.5).
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

- [ ] `fit` trims at message boundaries, never mid-message (T4)
- [ ] The opening request survives whenever anything survives (T5)
- [ ] Confirm behavior is unchanged for transcripts that fit the budget — this phase must be a no-op for the common case
- [ ] Tests on generated transcripts, including one where a single message alone exceeds the budget

**Verification**

```python
# no output message is a prefix or suffix of an input message
# turns[0] of the input is present in the output whenever output is non-empty
```

---

## Phase 3: Adversarial review gate on Part B

**Part B does not proceed until this phase returns an answer.** This is the process fix from P1, where the review that would have caught three regressions ran only after release.

- [ ] Put Open Question 1 to the reviewer with the measurements: is recovering content from superseded windows correct, or does matching the source's own window make the copy correct by construction?
- [ ] Put the merge-ordering rule to the reviewer specifically — it is the part most likely to be wrong
- [ ] Record the verdict and rationale under `### Phase 3 findings`
- [ ] If the answer is no, close Part B as decided-against in the north star's *explicitly decided NOT to do* section and skip to Phase 5

---

## Phase 4: Deduplicated union (Part B, gated)

Only if Phase 3 says yes.

- [ ] Accumulate a deduplicated, order-preserving union of carried window messages instead of replacing on every boundary
- [ ] Implement the merge rule Phase 3 endorsed; anchor on the newest window's order
- [ ] Peak measured with `PeakRecorder`, asserted independent of window count (U2, P1's corrected R5)
- [ ] `latest_window=False` still replays everything (U4)
- [ ] All P1 invariants still hold (U5)
- [ ] Tests: a message present only in an older window is recovered (U3); ordering is deterministic across runs

**Verification**

```python
# peak(2 windows) == peak(200 windows) with equal spans
# a message unique to window 1 of 200 appears in the output
```

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

Part A touches `config.py` and `bridge.py`'s budget plumbing; reverting restores the character budget with no data migration, since the config key is read, not written. Part B touches the reader's accumulation strategy; reverting restores P1's replace-on-boundary. Neither writes provider data.
