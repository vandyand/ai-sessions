---
title: "Codex Compaction Window — Retro"
date: 2026-08-21
spec: specs/conversation-log/codex-compaction-window
shipped: v3.1.4
---
# Codex Compaction Window — Retro

> Authored by Claude from the spec triad, the implementation commits, and the session that produced them. Codex audit: **skipped** — the reviewer timed out at 340s during planning (`xhigh` effort, empty result, no edits), so no independent audit ran on this priority.

## TL;DR — what to carry forward

**A comment that explains why something is impossible will be believed and never re-checked.** `read_codex` had a clear, confident comment saying Codex compaction summaries are encrypted with nothing readable to resume from. It was *half* true — the summary is sealed, the record around it is plaintext — and the half that was false cost every Codex bridge most of its conversation. When a comment asserts an external system's limitation, treat it as a claim with an expiry date, not a fact.

**Success criteria written against one machine's data are not criteria.** The first version of this spec defined done as "reading `019f59af` yields ~470 turns." Nobody else could check that and CI could not test it at all. Restating it as invariants over the *format*, verified on generated fixtures, is what made the work verifiable — and it surfaced the one invariant that actually constrained the design.

**The constraining invariant was invisible from a single measurement.** R5 — peak turns held must be independent of compaction count — is what rules out the obvious implementation. No amount of measuring one session would have revealed it, because one session has one compaction count.

## What we built

`read_codex` now adopts a compaction's `replacement_history` in place of everything before it, rather than skipping the record. The field is named for what it does: it *replaces* prior context. One boundary is marked, so `from_last_compaction` becomes a verified no-op rather than a feature that never engaged. Sandbox preamble is dropped, the sealed summary is counted rather than read, and a record without a carried window falls back to the previous behavior.

Shipped in **v3.1.4**. Twelve tests, all on generated transcripts.

## What worked

- **Measuring before designing.** Every number in the spec came from probing real output. The 88%-truncation figure is what made the priority obvious, and the `replacement_history` discovery came from dumping a record rather than trusting the comment above it.
- **Phase 0 as a real gate.** The spike ran against generated 2-window and 200-window transcripts and showed peak held at 54 for both. That confirmed K1 *before* production code, and it is the check that would have caught the wrong design.
- **Generated fixtures.** Not one committed transcript, no machine-specific path in any test, and the whole suite runs in CI in under four seconds.
- **Validating the shipped artifact, not the working tree.** Final validation ran the installed 3.1.4 against all 27 real compacted rollouts on disk — 27 passed, 0 failed, 0 turns dropped by truncation anywhere.

## What surprised us

- **Codex's compaction is better designed than expected.** It keeps every user message verbatim and seals only the assistant-side summary. That is close to what a good deterministic compaction would do, and it is a strong argument for reusing a harness's own artifact rather than writing our own compactor.
- **`encrypted_content` is ordinary cryptography, not a representation gap.** Fernet, version byte `0x80`. Codex cannot read it either. There was a real temptation to treat it as a parsing problem; checking the bytes ended that in one step.
- **`merge_runs` looked like a threat to the content invariant and was not.** It folds consecutive same-role turns *and* drops the `compaction` flag — but it runs after the reader, in `prepare()`/`bridge()`. Worth the ten minutes it took to confirm rather than assume.
- **The scale is wider than the motivating session.** 27 of 158 local rollouts had compacted, several with 100+ windows. This was not an edge case affecting one long session.

## What we would do differently

- **Write the spec as invariants first.** The session-specific version had to be rewritten before implementation, and the rewrite is what produced R5. Reaching for "what must be true of any input" earlier would have saved a pass.
- **Get the adversarial review to actually run.** It timed out at `xhigh` on a whole-repo prompt. A tighter prompt naming specific files, at lower effort, would likely have completed — and would have been the natural place for the "this is written against one session" objection to surface from something other than the user.
- **Use a quoted heredoc from the start.** An unquoted one let the shell eat backticked code spans in the doc-sync pass, silently emptying them. Caught and repaired, but it should not have happened twice in one session.

## Empirical metrics

Real 366-compaction session, before and after:

| | before | after |
|---|---:|---:|
| turns carried | 10,077 | 470 |
| boundaries usable by the selector | 0 | 1 |
| characters | 7,570,464 | 132,762 |
| turns dropped by `fit()` | 8,860 | 0 |
| read time | ~6s | ~7.3s |

Across all real compacted transcripts on this machine: **27/27 pass**, 1–366 windows, 0.7 MB–555 MB, zero truncation anywhere.

Cost: one file changed in `src/`, one new test file, +12 tests (93 → 105).

## Forward implications

- **P3 (window-aligned selection) is now reachable.** The reader marks a real boundary, so selection has something to align to. But note what P1 revealed: a carried window is *user-side only*. Any multi-window policy must decide what to do about assistant messages that exist in the live tail but not in carried windows.
- **P4's verbatim carry-forward wants native record references**, which this reader still discards — it projects straight to text. That contract change is the real cost of P4 and it touches every adapter.
- **The `encrypted_content` boundary is permanent.** Codex→anywhere will always lose the assistant-side summary of superseded windows. Worth stating plainly in user-facing docs rather than leaving it to be rediscovered.
- **Reading is ~20% slower on very large files** because a window is parsed and discarded once per compaction. Option B — locate the final boundary first — is the recorded escape hatch if that ever matters.

## References

- Spec: [README.md](README.md), [research.md](research.md), [implementation-plan.md](implementation-plan.md)
- North star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — P1
- Implementation: `5a8c8f3`, PR #10; release PR #11, tag `v3.1.4`
- Design pass: https://claude.ai/code/artifact/d3dc3a4d-fbc8-48f5-bc7f-6fe2e26f8d17
