# Target-Aware Token Budget and Whole-Message Selection — Implementation Plan

See [README.md](README.md) for decisions and [research.md](research.md) for measurements and invariants.

## Overview

Part A only. Part B was closed by the Phase 3 review before any code was written — see the findings below.

The plan review ran **first**, not after Phases 0–2 as originally sequenced. That was itself a review finding: gating only Part B left the budget, default, and trimming decisions — all Part A, and all wrong in the first draft — behind no gate at all.

## Prerequisites

- Branch off `main` at or after `5e5503e` (merged P2).
- **No external fixture.** Generated transcripts only, per P1's K7. Reuse `make_codex_rollout`; retain `PeakRecorder` for reader-memory regression only. Selection needs its own counting metric wrapper plus `tracemalloc` because it does not touch `_Conversation`.
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

- `tiktoken==0.13.0` `o200k_base` across prose, Python, JSON, diffs, paths/UUIDs, tool output, Unicode, dense CJK, base64, Git hashes, and minified JSON measured 1.143 minimum, **2.318 full-corpus mixture**, 2.696 per-class median, and 6.996 maximum chars/token. Policy rounds the mixture down to **2.0**, records the pure-class limitation, and discloses that Claude uses it as a proxy.
- `usable_fraction = 0.75` is a declared safety factor, not a measurement of private harness state.
- Claude policy uses a documented 200,000-token unknown-model baseline; Codex declares an independent 256,000-token compatibility floor. Current flagship limits are larger, but the bridge cannot know the model selected at resume time.
- Defaults: Claude **150,000 estimated tokens / 300,000 characters**; Codex **192,000 / 384,000**, cuts of 3.17× / 2.47× from 950,000.
- Config schema 1 auto-wrote `max_chars = 950000`; missing/non-integer version is schema 1 and that exact ambiguous default migrates with origin `target-default-migrated`. Other positive schema-1 values remain verbatim, and schema 2 treats all positive values as explicit. Migration and over-policy values are reported in both notes.

---

## Phase 1: One resolved target budget (Part A) — COMPLETE

- [x] Add validated immutable `BudgetPolicy` and `Budget`; put one policy on every `Harness` and extend registry tests to require `context_tokens > 0`, `0 < usable_fraction <= 1`, `chars_per_token > 0`, and non-empty provenance (T3/T10)
- [x] Give Codex budget policy its own declared `CODEX_BUDGET_CONTEXT_TOKENS = 256_000` compatibility-floor constant and provenance; never reuse writer-only `CODEX_CONTEXT_WINDOW = 258_400`. Add a structural/source assertion that the registry policy names the budget constant
- [x] Make `bridge_max_tokens` and `bridge_max_chars` independently optional, plus `bridge_max_chars_migrated: bool`, so never-configured, explicit legacy, and migrated machine-default states remain distinguishable (T2/T2b)
- [x] Implement one pure resolver: positive `max_tokens` wins and is clamped; otherwise `migrated=True` uses target policy with its distinct origin; otherwise positive `max_chars` stays verbatim; otherwise use ordinary target policy. Invalid/bool/non-positive values are unset and no resolved budget is non-positive
- [x] Bump saved config schema to 2. Missing/non-integer version is schema 1; exact `max_chars = 950000` sets the migrated carrier, while other positive values remain explicit. `as_toml()` writes `max_tokens` when set, otherwise explicit `max_chars`, and neither default; never write both
- [x] Add load → save → load tests that preserve the effective budget and do not resurrect `max_chars`
- [x] Replace `prepare_launch(max_chars=...)` and `bridge(max_chars=...)` with the resolved `Budget`; remove the `or DEFAULT_MAX_CHARS` fallback so there is one path
- [x] Render the handoff note and returned launch notice from the same `Budget`; report origin, estimate/ceiling, clamping, and any legacy value above target policy with migration guidance (T6)
- [x] Define `TRUNCATION_MARKER`, `HANDOFF_NOTE_RESERVE_CHARS = 4096`, and `MIN_BRIDGE_CHARS = reserve + 2 × (len(marker) + 1) + 2` once. Remove `fit`'s unlimited sentinel. Validate every constructed `Budget` and every adapter default
- [x] Add a non-1.0 fixture policy (2.5 chars/token) proving both conversion directions apply the ratio and at least one shipped policy has `Budget.chars != Budget.tokens`
- [x] Document `max_tokens`, precedence, legacy preservation, and default migration in repo README prose and the copyable config example; the example gains `version = 2` and uses `max_tokens` rather than the old auto-default

**Verification**

```python
# missing config resolves differently for Claude and Codex at documented defaults
# missing-version/schema-1 max_chars=950000 migrates with a visible origin; schema-2 stays exact
# a different legacy max_chars stays exact and warns when it exceeds Claude policy
# max_tokens resolves through the target policy and wins if both keys exist
# load(max_tokens only) -> save -> load preserves the same Budget and never adds max_chars
# the note is rendered from the applied Budget, including a deliberately clamped case
# migrated and never-configured defaults render different note/launch explanations
```

---

## Phase 2: Source-message selection before merging (Part A) — COMPLETE

- [x] Split preparation into `flatten(turns)` → selection → `merge_runs(survivors)` so source messages remain the selection/counting unit (T4/T9)
- [x] Use the same `SelectionMetric` item-plus-join total for the fit-through check and overflow loop. If the complete flattened list fits the post-note allowance under that metric, return it byte-identical with zero drops; only overflow applies anchor shares (T4/K9)
- [x] On overflow set `conversation_limit = Budget.chars - HANDOFF_NOTE_RESERVE_CHARS` and `anchor_share = (conversation_limit - 2) // 2` with no old 1,000-character floor. Cap the head marker-inclusive at that share, give the newest anchor the remaining allowance after the capped head and the worst-case two-character anchor join, then scan the tail; survivors after the first must be one contiguous input suffix and survivor count must be monotone across the truncated/full boundary
- [x] Count dropped pre-merge source messages exactly. A truncated turn retains only `ToolCall` entries whose complete rendered blocks survive; the note count may never exceed summaries present in payload
- [x] Bound every interpolated note field; if `len(note) + 2 > 4096`, raise `BridgeError` rather than truncate metadata. Test the runtime error plus floor−1/floor/floor+1 with maximal metadata and two oversized **same-role** anchors (T6/K8)
- [x] Use a linear `SelectionMetric` with `item_cost(turn)`, `join_cost(left, right)`, and `truncate(turn, limit)`, all in `Budget.chars`. P3 uses `len(text)` plus conservative 2-character same-role joins, validates non-negative costs/truncation, and scans the tail once; P4 may replace all three operations (T10)
- [x] Replace the handoff claim "opening request" and report both units honestly: `N source message(s), assembled into M target message(s) below`; expose both counts in `BridgeResult`/launch notice. When anchors are capped, note and launch notice also report the truncated-anchor count even if dropped is zero
- [x] Add property-style cases for membership/truncation shape, order, head/tail, fit-through, exact count, monotonicity, determinism, empty/single/all-oversized inputs, `[short, oversized-newest]`, and consecutive same-role runs
- [x] Add an end-to-end test beginning at `LaunchConfig.load` and calling `launch(session, config, dry_run=True, state=...)`; force the opposite harness, read the member path from `state.set_bridge`, and use `max_tokens = 5000` on ~40,000 characters to assert payload ≤10,000, dropped notice, and note ≈5,000 tokens/10,000 chars. Without the key, the same fixture keeps every message (T7)
- [x] On one ~340,000-character generated transcript with unset config, assert Claude drops messages while Codex keeps them; this kills any hidden `DEFAULT_MAX_CHARS` selector path
- [x] Add two same-role stress fixtures dimensioned by survivors: a fits-through edge where raw text fits but item-plus-join cost does not, and an overflowing ≥2,000-message case retaining >1,500 survivors. A test-local oracle computes `sum(len) + 2 × same-role-adjacencies`, asserts selector agreement **and real merged `[note, *kept]` text ≤ `Budget.chars`**, and requires `2 × (len(kept) − 1) > reserve − len(note) − 2`; removing join cost must fail deterministically
- [x] On every overflow assert survivors are `[first] + input[j:]` for one index `j`; no gap-tail selection
- [x] Measure selection on a generated ~1M-character transcript with a counting `SelectionMetric` wrapper (calls bounded by a constant times input turns), a practical time bound, and `tracemalloc` peak `<= 2 × Budget.chars + 512 × len(turns) + 1 MiB`. `PeakRecorder` remains only for P1 reader-memory regressions; it cannot observe selection
- [x] Assert note and launch-notice text for `[short, oversized-newest]` (`dropped=0`, `truncated=1`) and for a consecutive same-role fixture where source-message count differs from assembled-target count

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
- [x] Re-run Opus 5 against the revised plan and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3c — Opus 5 second review

**Verdict: NOT CLEAN — HIGH=2 MEDIUM=6.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `183c6b8` read-only on 2026-08-23. Full local artifact: `C:\Users\vandy\.claude\plans\perform-the-second-adversarial-glimmering-iverson.md`.

- [x] Cap and retain an oversized newest anchor before the tail loop
- [x] Migrate the exact schema-1 machine-written 950,000 default; surface other over-policy legacy values
- [x] Add dense corpus classes, pin measurement version, govern on minimum, and disclose Claude proxy status
- [x] Separate Codex budget policy from writer metadata
- [x] Remove non-positive unlimited behavior and validate policy ranges
- [x] Define note reserve and legacy character floor
- [x] Change the P4 seam to sequence-cost and name its unit
- [x] Name `launch(..., dry_run=True)` and restore the cross-target kept-count differential
- [x] Re-run Opus 5 and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3d — Opus 5 third review

**Verdict: NOT CLEAN — HIGH=3 MEDIUM=6.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `b07949c` read-only on 2026-08-23. Full local artifact: `C:\Users\vandy\.claude\plans\third-adversarial-read-only-p3-distributed-lamport.md`.

- [x] Use the 2.318 realistic mixture rounded to 2.0 and add a non-1.0 conversion kill test
- [x] Restore exact same-role assembly separator accounting
- [x] Define anchor shares from post-note conversation allowance with no 1,000-character floor
- [x] Define missing-version migration and `target-default-migrated` notice
- [x] Give the Codex budget floor an independent value/provenance and structural registry check
- [x] Add matching sequence cost/truncation hooks and validation
- [x] Define/validate `MIN_BRIDGE_CHARS` for every origin and adapter default
- [x] Make truncated tool-call counts reflect complete rendered blocks only
- [x] Force a real opposite-harness dry-run bridge and bound every note field
- [x] Re-run Opus 5 and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3e — Opus 5 fourth review

**Verdict: NOT CLEAN — HIGH=1 MEDIUM=6.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `66c655b` directly in read-only mode on 2026-08-23; no artifact was written.

- [x] Restore a behavioral `max_tokens` assertion to the production dry-run test
- [x] Carry and surface the schema-1 migrated-default state
- [x] Reserve both possible assembly joins and correct `MIN_BRIDGE_CHARS`
- [x] Re-dimension same-role testing against the conversation allowance
- [x] Restore the contiguous newest-suffix invariant
- [x] Replace whole-sequence repricing with a linear item/join/truncate metric
- [x] Report source-message and assembled-target counts separately
- [x] Re-run Opus 5 and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3f — Opus 5 fifth review

**Verdict: NOT CLEAN — HIGH=1 MEDIUM=5.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `f05906d` directly in read-only mode on 2026-08-23; no artifact was written.

- [x] Make join-aware metric pricing normative in fit-through as well as overflow
- [x] Consume the migrated carrier in resolver precedence
- [x] Restore `BridgeError` as the note-reserve failure mode
- [x] Disclose truncated anchors even when no messages are dropped
- [x] Remove the superseded whole-sequence callback from the harness contract
- [x] Add the schema-1 carve-out and minimum default to shared adapter fixtures
- [x] Re-run Opus 5 and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3g — Opus 5 sixth review

**Verdict: NOT CLEAN — HIGH=1 MEDIUM=3.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `69f9e92` directly in read-only mode on 2026-08-23; no artifact was written.

- [x] Make join-cost mutation tests independent of production metric and note slack
- [x] Replace the inapplicable `PeakRecorder` selection gate with counting metric + `tracemalloc`
- [x] Verify truncated-anchor and source/assembled note/launch text
- [x] Correct success criteria to T1–T10 including T2b
- [x] Re-run Opus 5 and require no HIGH or MEDIUM findings before Phase 1 implementation

### Phase 3h — Opus 5 seventh review

**Verdict: NOT CLEAN — HIGH=0 MEDIUM=2.** Claude Opus 5 (`claude-opus-5`, max effort) reviewed `ec1c890` directly in read-only mode on 2026-08-23; no artifact was written.

- [x] Assert real assembled payload bytes alongside the independent join-cost oracle
- [x] Clarify reader-vs-selector memory instruments and give selection allocation a failing bound
- [x] Final focused Opus 5 confirmation returned **CLEAN and GO** on `e1ad304` using `claude-opus-5` at max effort; no artifact was written

---

## Phase 4: Deduplicated union — CLOSED, NOT IMPLEMENTED

Decided against in Phase 3. Recorded in the north star's *explicitly decided NOT to do* section so it is not rediscovered as an idea.

---

## Phase 5: Adversarial review of the implementation — COMPLETE

**Nothing is released until this returns.** Phase 3 reviews the plan; this reviews the code that came out of it. Both gates exist because P1 ran neither before shipping and paid for it: a documented option silently stopped working, and the invariant the design rested on was both mis-stated and proxy-tested.

- [x] Re-run the reviewer against the actual diff, with the spec as context, and with **no timeout** — the first P1 attempt died in a 340s wrapper and the completed run took 6m22s
- [x] Ask specifically for a mutation the test suite would not catch. That question found the proxy-tested invariant in P1 and is the highest-yield thing to ask
- [x] Ask whether the shipped behavior matches what the spec and the handoff note claim
- [x] Fix every HIGH and MEDIUM before release; record LOW items in the retro
- [x] Record the verdict under `### Phase 5 findings`

### Phase 5 findings

Claude Opus 5 (`claude-opus-5`, max effort) ran each pass read-only and without a timeout.

1. The initial implementation review found **HIGH=2 MEDIUM=5**. The production
   config-to-launch path was proxy-tested, and a uniform suffix fixture could not kill a
   `break` → `continue` mutation. It also requested exact anchor-floor, tool-call retention,
   source/assembled count, disclosure, reserve, cross-target, stress, and memory gates.
2. The second pass against `6b78900` found **HIGH=1 MEDIUM=4**. Its concrete
   `[1000, 3, 1000]` counterexample proved that two independent anchor caps were
   non-monotone at budgets 2,001 and 2,002. It also found invalid resolver values, missing
   head-truncation proof, incomplete default-reduction disclosure, and the format gate.
3. The third pass against `521e7af` found **HIGH=0 MEDIUM=2**: retained newest-anchor length
   and per-call separator accounting were correct but not pinned against exact mutations.
4. The final focused pass against `2bc6a12` returned **CLEAN — HIGH=0 MEDIUM=0**. Its four
   LOW observations were subsequently closed in `e15f73f`: truncated-head tail pricing,
   too-small derived policy validation, non-ASCII provenance bounds, and duplicate head-cost
   evaluation. Full details are in [retro.md](retro.md).

---

## Phase 6: Doc Sync — COMPLETE

- [x] Update the repo `README.md` — the Budget section states characters and will be wrong
- [x] Update [`../NORTH_STAR.md`](../NORTH_STAR.md) — mark P3 complete with the SHA, add `### P3 observations`, and record Part B's outcome either way
- [x] Update [`../HARNESS_CONTRACT.md`](../HARNESS_CONTRACT.md) with the adapter-owned budget policy and selector-cost seam
- [x] Regenerate the spec index
- [x] Commit: `docs(conversation-log): sync docs after window-aligned-selection`

---

## Rollback

The implementation touches `config.py` and `bridge.py`'s budget and selection plumbing. Reverting restores the character budget and character-slice selection with no data migration, since the legacy config key is read but provider data is never rewritten. Part B is closed and must not alter the reader.
