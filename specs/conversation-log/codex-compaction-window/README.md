---
title: "Codex Compaction Window"
status: planned
date: 2026-08-21
priority: 10
---
# Codex Compaction Window

Make `read_codex` use the plaintext window Codex already hands us, instead of declaring every compaction unreadable and falling through to character truncation.

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P1**.
Research and measurements: [research.md](research.md).
Phased tasks: [implementation-plan.md](implementation-plan.md).

## Problem

`read_codex` skips every `compacted` record because its *summary* is encrypted. The record also carries `replacement_history` — the plaintext context Codex itself resumes from — and that is discarded with it. No `Turn` is ever marked `compaction=True` for a Codex source, so `from_last_compaction` slices nothing and `fit()` truncates by character count.

Measured on `019f59af`: **8,860 of 10,077 turns dropped** by truncation while a 224,498-character semantic window sat unread.

## Approach

**Replace-on-boundary.** When `read_codex` meets a `compacted` record, discard the accumulated turns and reseed the conversation from `replacement_history`, marking the first reseeded turn as a compaction boundary. This matches the record's own semantics — it *replaces* prior context — and bounds peak memory at roughly one window rather than 366 of them.

At EOF the conversation holds the final window plus everything appended after it: exactly what Codex is still holding.

## Key Decisions

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| K1 | Replace accumulated turns on a boundary rather than appending every window | Matches `replacement_history` semantics; bounds memory at ~465 turns instead of ~170k | Append-all and let `from_last_compaction` slice — materializes every window before discarding all but one |
| K2 | Single pass, same shape as every other reader | Keeps Codex from being the one reader that needs a pre-scan | Two-pass: locate the final boundary, then parse from there — costs a second scan of 530 MB |
| K3 | Mark the **first** reseeded turn `compaction=True` | `from_last_compaction` keeps the boundary turn and everything after; verified every window's first record is a non-empty `user` message, so `finish()`'s filter will not drop it | Marking the last — inverts the slice and drops the window |
| K4 | Drop `developer`-role records from the window | Verified content is `<permissions instructions>` sandbox preamble — Codex harness configuration, meaningless and misleading in another harness | Map them to `user`, which pollutes the copy with foreign sandbox rules |
| K5 | Keep a fallback for a `compacted` record with no `replacement_history` | All 366 records on the local fixture carry it, but older Codex versions may not; the fallback must not corrupt accumulated state | Assume the field is always present |
| K6 | Redefine the reported counter and rewrite the provenance note | The note currently claims the full pre-compaction history is carried; that becomes false | Leave the note alone and ship a copy that misdescribes itself |

## Resolved Questions

Answered empirically against the local fixture during exploration — see [research.md](research.md) for the probes.

| Question | Answer |
|---|---|
| Do all `compacted` records carry `replacement_history`? | Yes — **366 of 366**. Sizes: min 12, median 463, max 465 records. K5's fallback is defensive only. |
| Would the boundary turn survive `finish()`'s `if turn.text or turn.calls` filter? | Yes — the first record of **every** window is `role: user`, and the newest has 872 chars of text. |
| What are the `developer` records? | `<permissions instructions>` — sandbox and filesystem-permission preamble. Dropped per K4. |
| Does `_Conversation` need a reset method? | Yes — reseeding must clear both `turns` and the `_pending` tool-call map. Adding `reset()` beats mutating internals from `read_codex`. |

## Success Criteria

1. `count_compactions` on a Codex source with compactions is **greater than 0**.
2. Reading `019f59af` yields the newest window's user spine plus the live tail — on the order of 470 turns, not 10,077 — and `fit()` drops **0** turns at the default budget.
3. Peak turn count during the read stays bounded at roughly one window.
4. Read time for the 530 MB fixture stays within a few seconds of today's ~6s.
5. `read_claude` behavior is unchanged; all existing tests pass.
6. The provenance note accurately describes what was carried.

## Non-Goals

- Changing `DEFAULT_MAX_CHARS` or its unit — that is P3.
- Window-aligned selection across multiple windows — that is P3.
- Round-trip resume and bridge-group head tracking — that is P2.
- Anything touching the Codex **writer**.

## Implementation Status

- [ ] Phase 0 — Spike: prove replace-on-boundary against the real rollout
- [ ] Phase 1 — `_Conversation.reset()` and the `read_codex` boundary path
- [ ] Phase 2 — Counter semantics and provenance note
- [ ] Phase 3 — Tests
- [ ] Phase 4 — Doc Sync
