# Codex Compaction Window — Research

## North-Star Orientation

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P1**.

Rendered design pass: https://claude.ai/code/artifact/d3dc3a4d-fbc8-48f5-bc7f-6fe2e26f8d17

## Intake assumptions

Intake was answered from the originating conversation rather than re-asked, per `/feature plan` Step 2. Recorded here so the spec stands alone:

| Question | Answer |
|---|---|
| Problem type | Bug in an existing feature — the Codex reader discards readable data and falls through to truncation |
| Areas involved | `src/ai_sessions/bridge.py` (`read_codex`, `Transcript`, provenance note); `tests/test_bridge.py` |
| Existing patterns to build on | Yes — `read_claude` already marks compaction boundaries via `_Conversation.message(..., compaction=True)`, and `from_last_compaction` already consumes them |
| Known constraints / non-goals | Stdlib + `psutil` only; never modify provider data; do not change `DEFAULT_MAX_CHARS` (that is P3); do not touch round-trip resume (P2) |

## Problem Statement

`read_codex` treats every Codex `compacted` record as unreadable:

```python
# src/ai_sessions/bridge.py, read_codex
if record.get("type") == "compacted":
    conversation.opaque_compaction()
    continue
```

The comment justifying it says the summary "is `encrypted_content` with no plaintext … there is nothing readable to resume from." That is true of the **summary** and false of the **record**. The record also carries `replacement_history` — the context Codex itself resumes from — in plaintext.

Because the record is skipped, no `Turn` is ever created with `compaction=True` for a Codex source. `count_compactions` returns 0, `from_last_compaction` slices nothing, and `fit()` falls back to head-and-tail character truncation.

### Measured on `019f59af` (local, 2026-08-21)

```
turns parsed          : 10,077
opaque compactions    : 366
compaction boundaries : 0        <- what from_last_compaction can use
after from_last_compaction: 10,077 turns (dropped 0)
total text            : 7,570,464 chars   (budget 950,000)
after fit()           : 1,217 turns kept, 8,860 dropped by truncation
```

88% of the conversation is discarded by a rule with no knowledge of its content, while a semantically-chosen 224,498-character window sits unread.

## Codebase Context

All line references are against `30a311b` (3.1.3).

| Symbol | Location | Relevance |
|---|---|---|
| `Turn` | `bridge.py:97` | `role`, `text`, `calls`, `compaction: bool = False`. The boundary flag already exists. |
| `Transcript` | `bridge.py:118` | `turns: list[Turn]`, `opaque_compactions: int = 0` |
| `_Conversation.message(role, text, compaction=False)` | `bridge.py:310` | Already accepts the boundary flag |
| `_Conversation.opaque_compaction()` | `bridge.py:306` | Increments the counter only |
| `read_codex` | `bridge.py:337` | The `compacted` skip lives at `bridge.py:345` |
| `read_claude` | `bridge.py:374` | Reference implementation — passes `compaction=bool(record.get("isCompactSummary"))` at `bridge.py:389` |
| `count_compactions` | `bridge.py:439` | `sum(1 for turn in turns if turn.compaction)` |
| `from_last_compaction` | `bridge.py:443` | Slices `turns[boundaries[-1]:]` — keeps the boundary turn and everything after |
| `provenance note` | `bridge.py:518-558` | `compacted` and `opaque_compactions` both feed the opening note |
| `bridge()` | `bridge.py:826-852` | `if latest_window: turns, compacted = from_last_compaction(turns)` |

## Live findings

Probed directly against the real rollout, not inferred from source.

### The `compacted` record shape

```
type: compacted
payload keys: ['message', 'replacement_history', 'window_number',
               'first_window_id', 'previous_window_id', 'window_id']
message: ''                      <- always empty; likely why it read as opaque
window_number: 366
```

### `replacement_history` (newest window)

```
465 records, 224,498 chars
kinds: {'message': 464, 'compaction': 1}
roles: {'user': 461, 'developer': 3}
```

- **460 of 461** user messages are byte-identical to records in the raw log; the one exception is a synthesized `<environment_context>` block.
- Median length **110 chars** — these are real human turns. The raw log's 1,084 `user`-role records have a median of 5,471 chars because most are machine-injected context blocks that Codex drops.
- **No assistant messages.** The assistant side went into the sealed blob.
- The single `compaction` record is `{type, id, encrypted_content, internal_chat_message_metadata_passthrough}` — Fernet ciphertext (`gAAAAAB…`, first byte `0x80`, 36% printable). Not decodable. 46,154 reasoning records carry the same encoding.

### The live window

```
records after the last 'compacted' record : 46
response_items: {'reasoning': 5, 'message': 10, 'custom_tool_call': 1, 'custom_tool_call_output': 1}
message roles : {'assistant': 6, 'user': 4}   (22,484 chars)
```

Both sides are intact after the boundary, so a carried window plus the live tail gives complete user intent *and* recent two-sided detail. Combined ≈ 247k chars against the 950,000 budget — no truncation.

### Claude's equivalent, for contrast

`isCompactSummary: true`, role `user`, **one** generated prose summary of 26,710 chars — fully readable. One boundary in a 5,397-record session versus 366 in Codex. Window-aligned work is asymmetric between the two harnesses.

## Requirements

### Functional

1. `read_codex` must emit the plaintext content of a `compacted` record's `replacement_history` as `Turn`s.
2. Exactly one emitted turn per compaction must carry `compaction=True`, so `count_compactions > 0` and `from_last_compaction` slices at the right index.
3. The sealed `compaction` record inside `replacement_history` must be skipped, not emitted as text, and must remain countable for the provenance note.
4. The provenance note must stop claiming the full pre-compaction history is carried when it is not.
5. `read_claude` behavior must not change.
6. A `compacted` record lacking `replacement_history` (older Codex versions) must fall back to today's behavior — count it opaque, carry surrounding history.

### Non-functional

7. Stdlib + `psutil` only.
8. No provider data is written.
9. Peak memory must not scale with the number of windows. 366 windows × ~465 records is ~170k turns if every window is appended.
10. Reading `019f59af` (530 MB) must stay within a few seconds — current parse is ~6s.

## Options Considered

### A. Append every window's `replacement_history`, let `from_last_compaction` slice — rejected

Simplest diff. But it materializes ~170k `Turn` objects for this session before discarding all but the last window, violating requirement 9. Also leaves 365 redundant boundaries in the list.

### B. Two-pass — find the final `compacted` offset, then parse from there — rejected

Memory-safe and precise, but adds a full extra scan of a 530 MB file for a reader that is currently single-pass, and special-cases Codex against every other harness's reader shape.

### C. Replace-on-boundary (recommended)

On a `compacted` record, **discard the accumulated turns and reseed** from `replacement_history`, marking the first reseeded turn `compaction=True`.

This matches the record's own semantics — it is called *replacement*\_history because it replaces the prior context. At EOF the conversation naturally holds the final window plus everything appended after it, which is exactly what Codex is still holding.

- Memory bounded at roughly one window (~465 turns) plus the live tail.
- Single pass, same shape as every other reader.
- `from_last_compaction` becomes a no-op for Codex sources rather than a contradiction, and still works unchanged for Claude.

Trade-off: assistant messages from superseded windows are dropped. That is correct — Codex itself dropped them; they survive only inside the sealed blob.

## Recommendation

Option **C**, plus a redefinition of the reported counter. `opaque_compactions` currently means "compactions we could not read at all." After this change the accurate distinction is:

- **windows superseded** — how many earlier windows were replaced and are not carried (reportable as context for the copy's size).
- **sealed summary** — the carried window's assistant-side summary could not cross.

The provenance note text at `bridge.py:552-558` must be rewritten accordingly; its current wording ("the full pre-compaction history is carried instead of the summary") becomes false under this change.

## Open Questions

1. **Does `_Conversation` need a `reset()`?** Replace-on-boundary needs to clear `turns` and the `_pending` tool-call map. Adding a small method is cleaner than mutating internals from `read_codex`. — *resolve during init*
2. **Which reseeded turn carries the flag?** Marking the first preserves `from_last_compaction`'s slice semantics (it keeps the boundary turn). Confirm the resulting first turn is not dropped by `finish()`'s `if turn.text or turn.calls` filter — a `developer`-role record with empty text would be. — *resolve at Phase 0*
3. **Are `developer`-role records worth carrying?** 3 of 465. They are likely instruction preamble. Mapping them to `user` may pollute; dropping them may lose framing. — *resolve at Phase 0 by reading them*
4. **Do all 366 records carry `replacement_history`?** Only the newest was inspected. If early ones predate the field, requirement 6's fallback fires mid-file, which must not corrupt the accumulated state. — *resolve at Phase 0*

## References

- `src/ai_sessions/bridge.py` — `read_codex:337`, boundary skip at `:345`, `_Conversation:303-42`, `from_last_compaction:443`, note assembly `:518-558`, `bridge():826`
- `tests/test_bridge.py` — existing Codex reader coverage
- Local fixture: `~/.codex/sessions/2026/07/13/rollout-2026-07-13T00-15-26-019f59af-e300-7bd1-be75-e47599b5b593.jsonl` (530 MB — do **not** commit; tests must synthesize their own records)
- Claude contrast fixture: `~/.claude/projects/…/776daa15-39a3-4bd3-8fe4-86cdb7b2a5f8.jsonl`
- Codex CLI 0.148.0, `~/.codex/state_5.sqlite` (WAL)
