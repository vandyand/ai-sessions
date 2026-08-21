# Codex Compaction Window — Research

## North-Star Orientation

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — priority **P1**.

## Scope note

This spec is about the **Codex transcript format**, not about any particular session. Requirements and success criteria below are stated as invariants that must hold for *any* Codex rollout. A single real session is cited once, in [Field observation](#field-observation), purely as the evidence that motivated the work — nothing in the plan depends on that file existing.

## Intake assumptions

Answered from the originating conversation rather than re-asked. Recorded so the spec stands alone:

| Question | Answer |
|---|---|
| Problem type | Bug in an existing feature — the Codex reader discards readable data and falls through to truncation |
| Areas involved | `src/ai_sessions/bridge.py` (`read_codex`, `_Conversation`, `Transcript`, provenance note); `tests/test_bridge.py` |
| Existing patterns to build on | `read_claude` already marks compaction boundaries via `_Conversation.message(..., compaction=True)`; `from_last_compaction` already consumes them |
| Known constraints / non-goals | Stdlib + `psutil` only; never modify provider data; `DEFAULT_MAX_CHARS` is P3; round-trip resume is P2 |

## Problem Statement

`read_codex` treats every Codex `compacted` record as unreadable:

```python
# src/ai_sessions/bridge.py, read_codex
if record.get("type") == "compacted":
    conversation.opaque_compaction()
    continue
```

The justifying comment says the summary "is `encrypted_content` with no plaintext … there is nothing readable to resume from." That is true of the **summary** and false of the **record**: it also carries `replacement_history`, the plaintext context Codex itself resumes from.

Consequences, for any Codex transcript containing compactions:

1. No `Turn` is ever created with `compaction=True`, so `count_compactions` returns 0.
2. `from_last_compaction` therefore slices nothing — the feature it exists for never engages on a Codex source.
3. Every superseded window's raw history is carried, so total size grows with the whole file rather than the live context.
4. `fit()` then reduces that by head-and-tail character truncation, which has no knowledge of window boundaries or of what any turn said.

The severity scales with compaction count: a transcript that never compacted is unaffected; a long one loses most of its conversation to truncation while a plaintext window sits unread.

## The Codex compaction format

Verified against Codex CLI 0.148.0.

A compaction is a top-level record:

```
type: compacted
payload:
  message               always ""      <- likely why the record read as opaque
  replacement_history   list           <- the plaintext carried context
  window_number         int            monotonically increasing
  window_id / previous_window_id / first_window_id
```

`replacement_history` entries are of two kinds:

| Entry | Shape | Handling |
|---|---|---|
| Carried message | `{type: "message", role, content}` | Becomes a `Turn`; `developer` role is harness preamble and is dropped |
| Sealed summary | `{type: "compaction", id, encrypted_content, …}` | Fernet ciphertext — not decodable; skipped, counted |

`encrypted_content` is a Fernet token (`gAAAAAB…`, version byte `0x80`, AES-CBC + HMAC). It holds reasoning state under zero-retention and is decryptable only by the provider — Codex cannot read it either. The same encoding appears on `reasoning` records. **No decoder exists or will.**

The semantically important property is the field's own name: `replacement_history` *replaces* prior context. A reader that appends it is contradicting the format.

## Codebase Context

Line references against `30a311b` (3.1.3).

| Symbol | Location | Relevance |
|---|---|---|
| `Turn` | `bridge.py:97` | `role`, `text`, `calls`, `compaction: bool = False` — the boundary flag already exists |
| `Transcript` | `bridge.py:118` | `turns`, `opaque_compactions` |
| `_Conversation.message(role, text, compaction=False)` | `bridge.py:310` | Already accepts the boundary flag |
| `_Conversation.opaque_compaction()` | `bridge.py:306` | Increments a counter only |
| `_Conversation._pending` | `bridge.py:304` | Open tool-call index — must be cleared on reseed |
| `read_codex` | `bridge.py:337` | The skip is at `bridge.py:345` |
| `read_claude` | `bridge.py:374` | Reference implementation for boundary marking (`bridge.py:389`) |
| `count_compactions` | `bridge.py:439` | `sum(1 for turn in turns if turn.compaction)` |
| `from_last_compaction` | `bridge.py:443` | Slices `turns[boundaries[-1]:]` — keeps the boundary turn and everything after |
| provenance note | `bridge.py:518-558` | The `opaque_compactions` branch asserts full pre-compaction history is carried |
| `bridge()` | `bridge.py:826-852` | `if latest_window: turns, compacted = from_last_compaction(turns)` |

## Requirements

Stated as invariants over an arbitrary Codex transcript. Let **W** be the ordered `compacted` records carrying a non-empty `replacement_history`; **spine(w)** the carried plaintext turns of `w` (excluding sealed and `developer` entries); **tail** the turns parsed from records after `W[-1]`.

| # | Invariant |
|---|---|
| R1 | When `W` is non-empty, `read_codex(f).turns == spine(W[-1]) + tail` |
| R2 | Exactly one turn carries `compaction=True`, and it is the first turn of `spine(W[-1])` |
| R3 | `count_compactions(read_codex(f).turns) == 1` when `W` is non-empty, else `0` |
| R4 | `from_last_compaction` is a no-op on the result — the reader already starts at the boundary |
| R5 | Peak turns held during the read ≤ `max(len(spine(w)) for w in W) + len(tail)` — **independent of `len(W)`** |
| R6 | A `compacted` record without `replacement_history` does not reset; it increments `opaque_compactions` and leaves accumulated turns intact |
| R7 | `read_claude` output is byte-identical before and after this change |
| R8 | The provenance note never claims history was carried that was not |
| R9 | Stdlib + `psutil` only; no provider data written |

R5 is the one that rules out the naive implementation, and it is testable without any large file: generate transcripts with 2 and 200 windows and assert peak is identical.

## Options Considered

### A. Append every window's `replacement_history`, let `from_last_compaction` slice — rejected

Smallest diff, and R1–R4 would hold. Fails **R5**: it materializes every window before discarding all but one, so peak memory scales with compaction count. Also leaves `len(W)` redundant boundaries in the list, so R3 fails too.

### B. Two-pass — locate the final `compacted` record, then parse from there — rejected

Satisfies every invariant. Rejected on design grounds: it makes Codex the only reader needing a pre-scan, costs a second full pass over the file, and does not generalize to a harness that streams.

### C. Replace-on-boundary — recommended

On a `compacted` record with a readable window: discard accumulated turns, reseed from `spine(w)`, mark the first reseeded turn `compaction=True`.

Matches the format's own semantics, satisfies R1–R6 in a single pass, and bounds peak at one window plus the tail. `from_last_compaction` becomes a verified no-op for Codex rather than a contradiction, and is untouched for Claude.

Trade-off: assistant messages from superseded windows are dropped. That is correct — the source itself dropped them; they survive only inside the sealed blob.

## Recommendation

Option **C**, plus a correction to what gets reported. `opaque_compactions` currently means "a compaction we could not read at all." After this change two distinct facts need reporting:

- **windows superseded** — replaced by the source and deliberately not carried.
- **sealed summary** — the carried window's assistant-side summary could not cross.

The note branch at `bridge.py:552-558` claims "the full pre-compaction history is carried instead of the summary." That becomes false and must be rewritten (R8).

## Resolved Questions

Resolved by probing real Codex output during exploration. Answers are properties of the **format**; the sample that revealed them is noted for provenance only.

| Question | Answer | How |
|---|---|---|
| Is `replacement_history` reliably present? | Present on every `compacted` record observed (366/366 in one session, window sizes 12–465). R6's fallback is defensive, for older Codex versions | Direct scan |
| Would the boundary turn survive `finish()`'s `if turn.text or turn.calls` filter? | Yes — the first entry of every observed window is a non-empty `user` message | Direct scan |
| What are `developer`-role entries? | `<permissions instructions>` — sandbox/filesystem preamble. Harness configuration, meaningless in another harness. Drop them | Read contents |
| Does `_Conversation` need a reset method? | Yes — reseeding must clear `turns` **and** `_pending`, or a tool result from a superseded window can attach to a turn in the carried one | Code reading |

## Field observation

One real session, cited once as the motivating evidence. Nothing in the plan or tests depends on it.

A Codex session with 366 compactions (530 MB, 217,439 records) read through the current code:

```
turns parsed          : 10,077
opaque compactions    : 366
compaction boundaries : 0        <- what from_last_compaction can use
total text            : 7,570,464 chars   (budget 950,000)
after fit()           : 1,217 turns kept, 8,860 dropped by truncation
```

Its newest window held 465 entries / 224,498 chars — 461 real user messages, median 110 chars, 460 byte-identical to the raw log — and the live tail after it held 46 records with both sides intact. Carried together that is ≈247k chars, comfortably inside the budget, versus an arbitrary 12% slice today.

For contrast, a Claude transcript of 5,397 records had **one** boundary carrying a single 26,710-char generated summary. Window-aligned reading is therefore high-value for Codex sources and near-neutral for Claude ones.

## References

- `src/ai_sessions/bridge.py` — `read_codex:337`, skip at `:345`, `_Conversation:303`, `from_last_compaction:443`, note `:518-558`, `bridge():826`
- `tests/test_bridge.py` — existing Codex reader coverage
- Codex CLI 0.148.0 rollout format (`~/.codex/sessions/**/rollout-*.jsonl`)
- **No real transcript is a test dependency.** Tests synthesize rollouts; see [implementation-plan.md](implementation-plan.md) Phase 0.
