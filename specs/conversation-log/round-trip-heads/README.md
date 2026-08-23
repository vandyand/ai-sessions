---
title: "Round-trip Heads"
status: completed
date: 2026-08-22
priority: 15
---
# Round-trip heads

Status: complete on `conversation-log/round-trip-heads` (`7520b47`, hardened through
`6593173`).

This phase prevents Codex → Claude → Codex from returning to the original Codex ancestor.
It introduces utility-owned conversation identity and follows the newest native
materialization regardless of which historical row the user selected.

## Invariants

1. A native session belongs to at most one utility conversation.
2. Equivalent native materializations share both generation and frontier.
3. Only semantic appends advance a member; metadata-only appends do not.
4. No timestamp, title, or UUID found in prose determines ancestry.
5. Selecting a superseded row resolves through the conversation head.
6. Concurrent independent advances are divergence and automatic launch refuses to choose.
7. A bridge records no source byte beyond the frontier captured before reading began.
8. Migration may duplicate uncertain work but may not silently discard it.
9. Missing newest storage or a partially written tail fails conservatively.

## Acceptance evidence

Generated tests cover a complete Codex → Claude → work → Codex trip, equivalent-copy reuse,
two-head divergence, metadata-only Claude renames, arbitrary UUID text, schema 6 round-trip,
schema 5 migration, structured provenance, visible status labels, a transcript append that
races a bridge, completion/retry of a partial JSONL tail, cached validation of an unchanged
large append, and retry after transient read instability. The full Windows suite passes
with the checkout forced onto `src/`, and the repository's pinned Ruff lint and format
checks pass.

The implementation was also applied read-only to the two real local bridge pairs that
motivated the work. Each migrated to one current head and one superseded ancestor with no
spurious divergence.

## What remains

This phase tracks provenance per native materialization, not per message. The full
conversation log still needs ordered live segments, original-form projection, recovery
from transcript provenance markers, and a user-directed divergence workflow. See
[`../HARNESS_CONTRACT.md`](../HARNESS_CONTRACT.md) for the adapter boundary that lets a
third harness participate without pairwise conversion code.
