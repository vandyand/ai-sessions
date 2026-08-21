---
title: "Codex Compaction Window"
status: completed
date: 2026-08-21
priority: 10
---
# Codex Compaction Window

Make `read_codex` use the plaintext window Codex already provides, instead of declaring every compaction unreadable and falling through to character truncation.

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P1**.
Format analysis and invariants: [research.md](research.md).
Phased tasks: [implementation-plan.md](implementation-plan.md).

## Problem

A Codex `compacted` record carries an encrypted summary *and* a plaintext `replacement_history` — the context Codex itself resumes from. `read_codex` skips the whole record because the summary is unreadable, discarding the plaintext with it.

For any transcript containing compactions this means no boundary is ever marked, `from_last_compaction` never engages, every superseded window's raw history is carried, and `fit()` reduces the result by head-and-tail character truncation. Severity scales with compaction count.

## Approach

**Replace-on-boundary.** On a `compacted` record with a readable window, discard the accumulated turns and reseed from that window, marking the first reseeded turn as a compaction boundary.

This matches the format's own semantics — `replacement_history` *replaces* prior context — and is the only option that keeps peak memory independent of how many times the session compacted.

## Key Decisions

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| K1 | Replace accumulated turns on a boundary rather than appending every window | Only option satisfying R5 (peak independent of window count); matches `replacement_history` semantics | Append-all and let `from_last_compaction` slice — peak scales with compaction count, and leaves one boundary per window so R3 fails |
| K2 | Single pass, same shape as every other reader | Keeps Codex from being the one reader needing a pre-scan, and generalizes to streaming harnesses | Two-pass: locate the last boundary, then parse from there |
| K3 | Mark the **first** reseeded turn `compaction=True` | `from_last_compaction` keeps the boundary turn and everything after, so marking the first makes it a verified no-op (R4) | Marking the last — inverts the slice and discards the window |
| K4 | Drop `developer`-role entries from a window | They are `<permissions instructions>` sandbox preamble — harness configuration that is meaningless, and actively misleading, in another harness | Map to `user`, importing foreign sandbox rules into the copy |
| K5 | Fall back to today's behavior when `replacement_history` is absent | Older Codex versions may predate the field; the fallback must not corrupt accumulated state (R6) | Assume the field is always present |
| K6 | Redefine the reported counters and rewrite the provenance note | The note currently asserts the full pre-compaction history is carried; that becomes false (R8) | Ship a copy that misdescribes its own contents |
| K7 | Verify against synthesized transcripts, never a real session file | Invariants are properties of the format; tying them to one machine's data makes them unverifiable elsewhere and untestable in CI | Use a local 530 MB rollout as the fixture |

## Success Criteria

Every criterion is checkable on a generated transcript, on any machine, in CI. `W`, `spine`, and `tail` are defined in [research.md](research.md#requirements).

1. **R1 content** — `read_codex(f).turns == spine(W[-1]) + tail` when `W` is non-empty.
2. **R2/R3 boundary** — exactly one turn has `compaction=True`; it is `spine(W[-1])[0]`; `count_compactions == 1`.
3. **R4 selection** — `from_last_compaction(result) == result`.
4. **R5 bounded peak** — peak turns held for a 200-window transcript equals that for a 2-window transcript with the same window and tail sizes.
5. **R6 fallback** — a `compacted` record without `replacement_history` leaves accumulated turns intact and increments `opaque_compactions`.
6. **R7 no regression** — `read_claude` output unchanged; existing suite green.
7. **R8 honest note** — the provenance note describes what was actually carried.

## Non-Goals

- `DEFAULT_MAX_CHARS`, its unit, or multi-window selection — that is P3.
- Round-trip resume and bridge-group head tracking — that is P2.
- Anything in the Codex **writer**.
- Decoding `encrypted_content` — impossible by construction.

## Implementation Status

- [x] Phase 0 — Fixture generator and spike
- [x] Phase 1 — `_Conversation.reset()` and the `read_codex` boundary path
- [x] Phase 2 — Counter semantics and provenance note
- [x] Phase 3 — Invariant tests
- [x] Phase 4 — Doc Sync
