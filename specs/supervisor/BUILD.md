# Supervisor sessions: build charter

Approved by Andrew on 2026-10-31 (`/goal`: "the plan looks good … implement it fully and
comprehensively and verify everything"). No OpenSpec or Beads (Andrew declined OpenSpec for this
work); the task list is the checkbox list in `plan.md`, ticked only after each task's
verification passes.

## Objective

Ship the design in `README.md` as planned in `plan.md`: a persistent supervisor session for any
Claude Code or Codex target, created and reached with `S` in `sessions`.

## Definition of done

1. Every task 0–11 in `plan.md` is ticked, with its named tests present and passing.
2. `python -m unittest discover -s tests` passes on the final state, and
   `ruff check . && ruff format --check .` pass.
3. The spec's "Acceptance evidence" tests all exist (each mapped to a test name in
   `BUILD-ACCEPTANCE.md`).
4. Live checks: as many of the four supervisor/target pairs as can be driven non-interactively
   (headless `claude -p` / `codex exec` with isolated config homes) show: an update names a known
   target action, history and ledger survive a restart, and a compaction triggers a fresh brief.
   Anything that needs Andrew (Codex hook re-trust in his real config) is listed as his step.
5. Hooks are installed into Andrew's real configs only with backups, keeping existing hooks.

## Decisions since the plan

- 2026-10-31 (user decision): no opening prompt. A new supervisor starts empty; the brief arrives
  with the first question.
- 2026-10-31 (user decision): keep the `◉` marker.

## Non-goals

As in the spec: OpenCode, subagent watching, messaging the target, the clipboard.

## Constraints

- Branch `supervisor-sessions`. Commit per task; do not push.
- Never write to target session storage. Never kill processes by name.
- Budget: quota-conservation subagent policy (at most three workers, cheapest capable model).

## Milestones

1. Tasks 0–2 (spike, `read_since`, pastes).
2. Tasks 3–6 (index, digest, store, context).
3. Tasks 7–9 (hook, installer, create/launch/CLI).
4. Task 10 (browser) and Task 11 (live checks, rollout).

## Scope ledger

- 2026-10-31: Crash between the index append and the record save can leave a duplicated
  exchange in the index permanently (review finding 6); spec accepts one repeated update.
- 2026-10-31: A tool result arriving after the checkpoint for a call before it is dropped,
  so a failure there is not flagged (existing `_Conversation.result` behavior; finding 7).
- 2026-10-31: Head moved to a bridged member while the index is empty rebuilds from the new
  member and repeats bridged history (rare; finding 9).
- 2026-10-31: Per-prompt cost for supervisors of very large targets (~0.5 s index parse plus
  the conversation status check); a tail-only sidecar would remove it (finding 5).
- 2026-10-31: An interpreter path containing a space gives a quoted hook command that Codex's
  PowerShell cannot run; not the case on this PC.
- 2026-10-31: `--target-log --since` is accepted but not applied (exchanges carry no times).

## Progress

- 2026-10-31: Milestone 1 done (`6013b3b`): Tasks 0–2, full suite 647 OK. Hook facts: both
  harnesses cap hook text near 10k chars, so text over 9,000 chars is delivered by file. Codex
  account quota was exhausted during the spike, so Codex live checks may have to wait for quota.
  Claude pastes moved behind a `pasted_contents` adapter capability. Tests need `PYTHONPATH=src`
  (an installed 3.7.5 copy shadows `src`).
- 2026-10-31: Milestone 2 done (`1daf3c0`): Tasks 3–5 plus the installer library; suite 676 OK.
  Wave 3 running: Tasks 6+7 (context, hook) and Task 9 (+ install CLI).
- 2026-10-31: Milestone 3 and Task 10 done; suite 732 OK. Measured on real transcripts
  (read-only, temp output): a 209 MB Claude target gives a ~100k-token brief in 0.3 s; a 519 MB
  Codex target (4,444 exchanges, 21 MB index) took 14 s until the digest trim was made linear
  and the index cached per hook run; now 0.07 s for the brief and ~0 s for a no-change extend.
  Reset key: `R` then `y` in the `i` details (worker's call, spec said only "from the details").
- 2026-10-31: Live checks (isolated APPDATA/LOCALAPPDATA/state; Claude hooks via `--settings`,
  Codex hooks via `-c` + `--dangerously-bypass-hook-trust`; real harness storage only for new
  throwaway sessions in the scratchpad `live\work` folder):
  - Claude→Claude: brief on first question names `marker-cc.txt`; update after a second target
    action names `second-cc.txt`; resumed supervisor keeps its history; `/compact` sets
    `brief_due` and the next prompt gets brief n=2.
  - Claude→Codex (real 10-28 Codex session, read-only): the 71k-char brief went by file and the
    supervisor described the target correctly (4 exchanges, matching the index).
  - Bridge Codex→Claude via `prepare_launch`: next update said "Target now runs in Claude Code",
    contained only the new exchange (`last_seq_sent` 4→5), no replay.
  - Codex as supervisor: hook binds the native id on SessionStart and the brief is recorded in
    the rollout; model turns impossible (account "Quota exceeded"), so answer quality, restart
    and compaction for Codex supervisors are unverified (needs Codex quota).
  - Defects found and fixed (`9a8775b`): AGENTS.md as first prompt; supervisor could not read
    its brief file (now `--add-dir=<folder>`); quoted hook command failed under Codex.
- 2026-10-31: Independent whole-branch review: fixed the `-I` hook crash (psutil is in user
  site-packages here), moved the fast path to stdlib-only `ai_sessions_hook`, rebind on
  `/clear`, keep a reset made during a hook run, installer refuses non-list events, keeps
  non-ASCII, skips no-op writes, writes atomically. Suite 741 OK. Lesser findings in the ledger.
