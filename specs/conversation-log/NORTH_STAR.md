# The Conversation Log

A conversation carried across harnesses keeps its identity, its full user history, and its native fidelity. Concretely: bridging a Codex session into Claude and back returns every message you wrote — not an arbitrary character-window slice — the Codex-origin portion is never converted twice, resuming in either harness always continues from whichever side holds the newest work, and adding a third harness costs one reader and one writer with no changes to the neutral model.

## How to use this doc

This north star was distilled from a design pass measured against real local data (session `019f59af` and its Claude copy `776daa15`) — every figure below was observed, not estimated. The rendered version of that design pass lives at the artifact linked in References.

Priorities are ordered by evidence strength and independence, not by architectural elegance. P1 stands alone and produces real bridged output that makes P3 and P4 easier to judge. Resist planning P3/P4 in detail until P1 has shipped and been looked at.

## Current state

`ai-sessions` 3.1.3. Bridging exists and works session-to-session: a neutral `Turn` model, per-harness readers and writers, and a `HARNESSES` registry in `bridge.py`. The conversion layer is largely right. What is missing sits above it — identity — and what is broken sits beside it — selection.

Two failures are live today:

- **Round trips lose work.** Codex → Claude → Codex resumes the pre-bridge state. `prepare_launch` short-circuits when the target harness matches the session's own, and a bridged copy's provenance note makes `codex_refs` record the copy's own *ancestor* as its counterpart.
- **Selection is a character count.** `read_codex` skips every `compacted` record as unreadable, so no turn is marked as a boundary, `from_last_compaction` drops nothing, and `fit()` falls back to head-and-tail truncation.

Observed on one real Codex session (530 MB, 366 compactions). Cited as the evidence that motivated P1 — the priorities themselves are stated as format-level invariants, not as properties of this session:

| Observation | Value |
|---|---|
| Rollout size (sample) | 530 MB, 217,439 records |
| Compactions | 366 |
| Turns parsed | 10,077 |
| Compaction boundaries usable by the selector | 0 |
| Turns dropped by truncation | 8,860 (88%) |
| Newest compacted window | 465 records, 224,498 chars |
| User messages in that window | 461, of which 460 byte-identical to the raw log |
| Live window after the last boundary | 46 records — 6 assistant, 4 user |
| Conversation as a share of the file | 1.4% |

## Ordered priorities

### P1 — Read the Codex compaction window

`read_codex` emits the compacted window's `replacement_history` as turns and marks a real boundary, instead of calling `opaque_compaction()` and skipping the record. `from_last_compaction` then does the job it was written for.

Done when, for **any** Codex transcript containing compactions: the reader carries the newest window's plaintext spine plus the live tail, exactly one turn is marked as a boundary, `from_last_compaction` is a verified no-op, and peak turns held during the read is independent of how many times the session compacted. Verified on generated transcripts, so it holds on any machine and in CI.

Self-contained. No architecture. Highest evidence-to-effort ratio in the set.

### P2 — Stop the round trip losing work

Ignore bridge-ancestor back-references when resolving launch targets, and resume from whichever member of a bridge group carries the newest work. Mark non-head members superseded rather than hiding them.

Done when: Codex → Claude → work → Codex continues the Claude work, and selecting the copy's row no longer resumes the session it was copied from.

A bridge group is a poor conversation id, but it is the migration path to a real one.

### P3 — Window-aligned selection and a token budget

Replace head-and-tail character trimming with whole-window selection: always carry the newest boundary's carry-over, then add whole windows newest-first until the budget fills, trimming inside a single window only when that one window alone overflows. Re-express `DEFAULT_MAX_CHARS` — currently **950,000 characters**, roughly 240–270k tokens against 1M-token context windows — as a per-harness token budget.

Done when: the budget names its unit, is derived per target harness, and truncation is a documented backstop rather than the primary mechanism.

### P4 — The conversation log

Our own conversation id. Ordered segments held as live references, not snapshots. Per-message provenance, so a message is projected at most once and always from its original form. Verbatim carry-forward for same-origin runs. Fork and resume-from-a-point fall out.

Done when: a Codex → Claude → Codex trip keeps the Codex-origin portion in native form, and `specs/` records how a third harness would be added.

## Key decisions

- **D1 — A concept enters the neutral model only when two or more harnesses need it.** Compaction qualifies: Codex marks `compacted`, Claude marks `isCompactSummary`. `replacement_history` does not — it is one harness's expression of a shared idea and stays inside `read_codex`. *Alternative rejected:* a canonical format that is lossless for every harness. That path ends in tracking every harness's quirks forever.
- **D2 — Segments are live references, not copies.** Materialization re-reads a segment's tail, so work done by resuming a native session outside `sessions` is picked up automatically. *Alternative rejected:* snapshotting segment content at bridge time, which makes our id go stale the moment anyone bypasses the tool.
- **D3 — Always write a new native session; never edit an existing copy.** *Alternative rejected:* appending into a previously materialized session to avoid re-replaying. It saves context we do not need to save and breaks the never-rewrite rule.
- **D4 — Per-message provenance, not per-segment.** Contiguous same-origin runs become a derived optimization. *Alternative rejected:* segments as the primary unit, which assumes contiguity and needs special cases for forking and interleaving.
- **D5 — The encrypted blob never crosses.** Codex's `encrypted_content` is a Fernet token (`gAAAAAB…`, version byte `0x80`) holding reasoning state under zero-retention. Codex cannot read it either. What is sealed is the assistant's reasoning; the user-visible conversation sits in plaintext beside it. *Alternative rejected:* waiting for or building a decoder.

## Things we've explicitly decided NOT to do

- **Longer timeouts or retries on the SQLite readers.** Measured: WAL readers do not block on writers — 0 failures in 200 reads under a sustained writer, 0 in 617 reads across truncating checkpoints. The only way to force a failure was `journal_mode=DELETE` plus `BEGIN EXCLUSIVE`, which Codex never does.
- **Stale-data fallbacks when a load fails.** Masking an unknown failure is the wrong response to not understanding it. 3.1.3 made failures legible instead.
- **Our own LLM-based compaction.** It would add API keys, cost, latency, and an offline failure mode to a stdlib-plus-`psutil` tool. Codex already demonstrates a deterministic compaction worth copying: keep user messages verbatim, condense agent and tool activity.
- **Editing provider transcripts in place.** Renaming appends a title entry to append-only provider data; nothing else writes to provider storage.

## Red flags

- **The Codex writer is the risky surface.** Constructing valid records means honouring the `response_item` versus `user_message`/`agent_message` split and the `task_started`/`task_complete` framing. Getting it wrong produces a session that resumes with full context and a blank screen — indistinguishable from a failed bridge. Verify visually, not by asking the model what it remembers.
- **Window alignment is asymmetric.** 366 boundaries in Codex versus one in a 5,397-record Claude session. P3 is transformative for Codex sources and close to a no-op for Claude ones. It demotes the character budget; it does not retire it.
- **Session proliferation.** Each hop writes a new native session. Superseded members are marked, not hidden — whether a long chain should ever be collapsed is unresolved.
- **A failed load leaks its SQLite connection.** The exception traceback keeps the frame alive, so on Windows the file stays handle-locked until garbage collection. Wants a `finally: connection.close()`.

## Open questions

- The reader currently projects straight to text and discards native record references. Verbatim carry-forward (P4) needs them, and changing that contract touches every adapter.
- A Codex window whose spine is unreadable carries nothing across. Silently skipping it is wrong; the copy should say what is missing and where.
- Characters-per-token differs by harness and content. Worth measuring once per harness rather than assuming 4.
- Two harnesses resuming the same conversation concurrently produces two heads. Detectable; the resolution is not designed.

## Doc-sync protocol

Every `/feature plan` generated from this doc ends with a final Doc Sync phase that runs these steps:

1. Walk the repo README — audit and update.
2. Walk this north-star doc — mark the completed priority with its implementation SHA, add observations under a new `### PN observations` section, update red flags, re-evaluate decisions.
3. Regenerate the spec index: `python3 ~/.claude/skills/feature-specs/scripts/index.py ./specs`
   (on Windows use `python` — `python3` resolves to the Microsoft Store alias stub).
4. Commit with message format `docs(conversation-log): sync docs after <priority-name>`.

## References

- Design pass, rendered: https://claude.ai/code/artifact/d3dc3a4d-fbc8-48f5-bc7f-6fe2e26f8d17
- `src/ai_sessions/bridge.py` — `Turn`, `read_codex`, `read_claude`, `from_last_compaction`, `fit`, `HARNESSES`
- `src/ai_sessions/app.py` — `prepare_launch`, `command_for`, `UserState.bridge_for`, `codex_refs`
- Measured against `~/.codex/sessions/2026/07/13/rollout-…-019f59af-….jsonl` and `~/.claude/projects/…/776daa15-….jsonl`
- Released through 3.1.3: PRs #1, #2, #3, #5, #6, #7, #8, #9
