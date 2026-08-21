# Codex Compaction Window — Implementation Plan

See [README.md](README.md) for decisions and [research.md](research.md) for measurements.

## Overview

Four working phases plus doc sync. All code changes are confined to `src/ai_sessions/bridge.py`; tests go in `tests/test_bridge.py`.

## Prerequisites

- Branch `conversation-log/codex-compaction-window` off `main` at `30a311b` (3.1.3).
- Local Codex fixture at `~/.codex/sessions/2026/07/13/rollout-2026-07-13T00-15-26-019f59af-e300-7bd1-be75-e47599b5b593.jsonl` (530 MB). **Never commit it.** Committed tests synthesize their own records; the real fixture is for spike and acceptance checks only.
- Verification runs with `PYTHONPATH=$PWD/src`.

## Verification mechanism

This project has no REPL daemon. The equivalent live check is a direct `python -` invocation against the working tree with `PYTHONPATH=$PWD/src`. Each phase below states the exact expression and the expected result. Run those first; the narrow test command is secondary.

Narrow test command for this spec: `python -m unittest tests.test_bridge -v`
Broad suite (`python -m unittest discover -s tests`) is a preflight/CI gate, not per-phase verification.

---

## Phase 0: Spike — prove replace-on-boundary on the real rollout

Prototype in a scratch script, **not** in `bridge.py`. The goal is evidence, not code that ships.

- [ ] Write a throwaway reader that streams the fixture and, on each `compacted` record, resets an accumulator and reseeds it from `replacement_history` (skipping `developer` roles and the sealed `compaction` record)
- [ ] Record: final turn count, character total, peak accumulator size, wall-clock time
- [ ] Confirm the final accumulator equals *newest window spine + live tail* by spot-checking that its first turn matches `replacement_history[0]` of the last `compacted` record and its last turn matches the file's last `message` record
- [ ] Confirm at least one window in the middle of the file has a smaller `replacement_history` (min was 12) and that reseeding from it does not leave stale `_pending` tool-call state
- [ ] Record findings in this file under a `### Phase 0 findings` heading

**Verification**

```python
# expected: turns on the order of 470; peak bounded near one window;
# chars ~247,000; time within a few seconds of the current ~6s baseline
```

**Bail condition:** if peak accumulator size scales with window count, K1 is wrong — stop and reconsider option B (two-pass) before writing production code.

---

## Phase 1: `_Conversation.reset()` and the `read_codex` boundary path

- [ ] Add `_Conversation.reset()` at `src/ai_sessions/bridge.py` (near `opaque_compaction`, ~line 306) clearing both `self.turns` and `self._pending`
- [ ] Add a module-level helper `_codex_window_turns(payload) -> list[Turn] | None` that returns the plaintext turns of a `compacted` payload's `replacement_history`, or `None` when the field is absent or empty (K5 fallback)
  - Skip records whose `type` is `compaction` (the sealed blob)
  - Skip records whose `role` is `developer` (K4)
  - Map `role: user` records to `Turn("user", text)`
  - Mark the **first** returned turn `compaction=True` (K3)
- [ ] Replace the skip at `bridge.py:345` so that on `type == "compacted"`:
  - if `_codex_window_turns` returns turns → `conversation.reset()`, extend with those turns, and increment a superseded-window counter
  - if it returns `None` → keep today's behavior: `conversation.opaque_compaction()` and `continue`, leaving accumulated turns intact
- [ ] Leave `read_claude` untouched

**Verification**

```python
# PYTHONPATH=$PWD/src python -
from ai_sessions.bridge import read_transcript, count_compactions
import glob
p = glob.glob(r"C:\Users\vandy\.codex\sessions\**\*019f59af*", recursive=True)[0]
tr = read_transcript("codex", p, tool_calls=True)
print(len(tr.turns), count_compactions(tr.turns))
# expected: turn count on the order of 470 (NOT 10,077); compactions >= 1 (NOT 0)
```

---

## Phase 2: Counter semantics and provenance note

- [ ] Distinguish the two counts on `Transcript`: windows superseded and not carried, versus a sealed summary on the carried window. Keep `opaque_compactions` meaning "compactions we could not read at all" so the K5 fallback path still reports correctly
- [ ] Rewrite the note branch at `bridge.py:552-558` — its claim that "the full pre-compaction history is carried instead of the summary" is false once Phase 1 lands
- [ ] Add note wording for the new case: the window's user history crossed verbatim, earlier windows were superseded by the source itself, and the assistant-side summary for this window is sealed and did not cross
- [ ] Confirm `bridge()` at `bridge.py:826-852` still passes the right counters into `provenance`

**Verification**

```python
# Render the note for a codex source with a carried window and assert it does NOT
# contain "full pre-compaction history", and DOES explain the sealed summary.
```

---

## Phase 3: Tests

All fixtures synthesized in-test — no real transcripts committed.

- [ ] `test_codex_compaction_window_becomes_turns` — a `compacted` record with a 3-message `replacement_history` yields 3 turns, first marked `compaction=True`
- [ ] `test_codex_compaction_replaces_earlier_turns` — messages before a boundary do not survive it; messages after it do
- [ ] `test_codex_window_skips_sealed_and_developer_records` — a `compaction` record and a `developer` record in `replacement_history` produce no turns
- [ ] `test_codex_compacted_without_replacement_history_falls_back` — a `compacted` record with no such field leaves accumulated turns intact and increments `opaque_compactions`
- [ ] `test_codex_reset_clears_pending_tool_calls` — an unmatched tool call before a boundary does not attach its result to a turn after it
- [ ] `test_from_last_compaction_now_slices_codex_sources` — end-to-end: synthesized two-window transcript, only the newest window survives
- [ ] `test_read_claude_unchanged` — guard that Claude's single-boundary behavior is untouched
- [ ] Run `python -m unittest tests.test_bridge -v`

**Verification**

```bash
PYTHONPATH=$PWD/src python -m unittest tests.test_bridge -v   # all green
PYTHONPATH=$PWD/src python -m unittest discover -s tests      # 93+ green, no regressions
uvx ruff@0.16.3 check . && uvx ruff@0.16.3 format --check .
```

---

## Phase 4: Doc Sync

- [ ] Audit and update the repo `README.md` — the cross-harness section describes trimming behavior that this phase changes
- [ ] Update [`../NORTH_STAR.md`](../NORTH_STAR.md) — mark **P1** complete with the implementation commit SHA, add a `### P1 observations` section, update red flags, re-evaluate decisions
- [ ] Regenerate the spec index: `python3 ~/.claude/skills/feature-specs/scripts/index.py ./specs` (use `python` on Windows)
- [ ] Commit: `docs(conversation-log): sync docs after codex-compaction-window`

---

## Rollback

Single-file change plus tests. Revert the `bridge.py` commit; no state schema, config, or provider data is touched. A copy already bridged under the new reader remains valid — it is an ordinary session file.
