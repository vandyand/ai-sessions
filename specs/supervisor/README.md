---
title: "Supervisor Sessions"
status: completed
date: 2026-10-30
priority: 20
---
# Supervisor sessions

A supervisor is an ordinary, resumable Claude Code or Codex session that watches one *target*
conversation and helps its user judge how the target is doing: whether it is still aligned with
the starting objective, whether it is working efficiently, and what low-hanging improvements
exist. The supervisor never acts on the target. When the user decides the target needs a
correction, the supervisor drafts a message, the user reviews and revises it, and the user
pastes it into the target themselves.

## Why

The user runs long and sometimes indefinite sessions (algorithmic trading research, very large
builds). Checking in on the main session now and then does not give a rounded picture of how it
is going. Codex's `/side` conversation helps because it is a fork that already knows the main
session's context and is one keystroke away. But it lives only in memory and vanishes when Codex
restarts, and it goes stale the moment the main session moves on. A stopgap hook
(`C:\Users\vandy\agent-hooks\codex_side_memory.py`) replays old `/side` exchanges per working
directory, but it has to guess the parent session from the directory, because Codex hook payloads
carry no parent-thread ID.

What the user wants:

- **Persistent:** the supervisor survives restarts and resumes like any other session, keeping
  its own history.
- **Always current:** whenever the user asks it something, it has seen everything the target
  has done since the last time.
- **Cross-provider:** a Claude supervisor can watch a Codex target and vice versa. The user finds
  the providers complement each other.
- **Same in both harnesses:** it must work with complete reliability in both Codex and Claude
  Code.
- **One key away:** spawned from, and listed in, the `sessions` browser.

## How it works

Three pieces, and nothing runs between the user's prompts:

1. **Brief.** A single fixed recipe gives the supervisor its picture of the target:
   - a **digest** of the target's older history, capped at about 50k tokens;
   - a **cleaned tail** of the target's most recent activity, about 50k tokens.

   The brief is sent when the supervisor is created, and again after each of the supervisor's
   own compactions.
2. **Updates.** Before each user prompt in the supervisor, a hook adds the target's *new*
   activity since the last update, cleaned, then advances the checkpoint.
3. **Ledger.** The supervisor keeps a durable notes file: the objective, its running assessment,
   open concerns, and messages the user says they sent to the target. It is part of every brief,
   so the supervisor's judgment survives its own compactions.

The tail rolls in steps. A session's context only grows, so updates add to the end, and
between compactions the recent material accumulates. When the supervisor compacts (at the
user's existing 50% auto-compact setting), a fresh brief rebuilds the digest and the 50k tail
from the newest activity. The supervisor never starts a context window with more than about
100k tokens of target material, about 10% of a 1M window.

### Rejected alternatives

- **Fork the target once.** It goes stale immediately and starts half full.
- **An always-on summarizer agent writing a log.** Another nonstop process that costs tokens
  while nobody is looking, can stall unseen, and is only as good as its notes.
- **On-demand tools only.** The model sometimes will not look, and answers from stale memory.
- **A size cap with fallback layers on every update.** Unnecessary, because the brief and
  updates are bounded by construction (see Sizes).

## Cleaning

"Cleaned" target activity is rendered in the bridge's summarized style:

- User and assistant messages come through verbatim.
- Each tool call becomes one clipped line, `⟦Tool⟧ args → result`, at about 400 characters.
  This matches the bridge.
- Reasoning and attachments are dropped. The target's compaction points appear as one-line
  markers ("target compacted here"). Summary text is not included: the activity it summarizes is
  already in the digest or tail, and Codex's summaries are encrypted anyway.

The tail starts at a whole-turn boundary and may reach back across any number of target
compactions. That is what keeps pre-compaction context available, including for Codex targets.

## Digest

The digest covers every target turn before the tail starts, deterministically, with no LLM.
Per turn it keeps:

- the user prompt, with pasted content replaced (see Pasted content below) and the typed text
  clipped at about 6,000 characters;
- the final assistant message, clipped to about 2,000 characters;
- tool-call counts by tool, such as "Bash ×212, Edit ×37";
- one line for each failed tool call: non-zero exit, error result or timeout. Repeated identical
  commands collapse to "same command ×14".

Failures are kept because wasted effort is what the user most wants the supervisor to notice,
and it lives in the tool output that everything else drops.

### Pasted content

Pastes inflate prompts without being the user's own words, so the digest replaces each paste
with a marker: `[pasted 4,210 chars: <first ~300 chars> … <last ~500 chars>]`. The tail of the
paste is kept because words typed after an unclosed paste land at the end of the message.

**Detection:**

- **Claude Code (exact):** `~/.claude/history.jsonl` records each prompt as typed, with
  placeholders such as `[Pasted text #1 +32 lines]`, and keeps the pasted content separately
  under `pastedContents`. Match the transcript's user message to its history entry by session id
  and timestamp, and use the placeholders to find the pasted spans. If no history entry matches,
  fall back to the Codex rule.
- **Codex (fences only):** Codex records nothing about pastes. Checked on 2026-10-30: pastes are
  stored expanded inline, there are no placeholders, and `text_elements` is empty. The user
  conventionally wraps pastes in ```` ``` ````. The rule:
  1. Pair ```` ``` ```` occurrences in order, first with second, third with fourth.
  2. Text inside a pair is pasted.
  3. An unpaired final ```` ``` ```` means pasted through the end of the message.
  4. A ```` ``` ```` immediately followed by a language word (```` ```python ````) is treated as a
     block opener *inside* the paste, not as a closing fence.
- **Measured (2026-10-30):** across 1,782 Claude and 3,628 Codex prompts, typed text had a median
  of about 120–170 characters and a 99th percentile of about 2,100–2,700. The longest genuinely
  typed message was 5,463 characters, which is why the clip is 6,000.

**Known limit:** a forgotten closing fence followed by more typing cannot be resolved
deterministically. The 500-character paste tail and the 6,000-character clip bound the damage.
Detection affects only the digest: the cleaned tail and every update carry user messages
verbatim, pastes included.

### Cap

About 50k tokens. If the digest is over:

1. The very first user prompt (the starting objective) is always kept in full.
2. The oldest turns are shortened first.
3. If it is still over, the middle is cut and a note says how many turns were left out.

## Sizes

Measured on six real transcripts on this PC (2026-10-30). Characters are a proxy for tokens, at
about 3.5 per token:

| Session | Full history | Tool output share | Cleaned | Digest (unclipped) |
|---|---|---|---|---|
| Codex, 9,215 tool calls | 67.6M chars | 87% | 6.4% | 0.1% (38k chars) |
| Codex, 10,004 tool calls | 44.9M | 74% | 10.9% | 0.7% |
| Codex, 1,744 tool calls | 5.1M | 76% | 15.6% | 0.4% |
| Claude, elevate-futures | 13.1M | 92% | 4.9% | 2.8% (372k chars) |
| Claude, minecraft | 10.6M | 88% | 7.8% | 2.2% |
| Claude, ai-sessions (mostly talk) | 0.4M | 20% | 73% | 66% |

- Cleaning shrinks working sessions about 10×.
- Digest size depends on how much the user and the agent wrote, not on how much the tools
  printed. The largest measured digest, about 106k tokens, came from a 13M-character Claude
  session. Per-item clipping is expected to bring most digests well under the cap. The cap
  handles the rest.
- An ordinary update (an hour or a day of target work) is small.

**Long gap:** if the new activity since the last update would exceed the tail budget (about
50k tokens cleaned), for example after a weekend away, the hook sends a fresh brief instead of
the update. The checkpoint moves to the end of that brief. No other size handling exists.

## Goals

- `S` in the browser opens the selected conversation's supervisor, creating one on first use.
  `S` on a supervisor jumps to its target.
- A supervisor is bound to the target's **conversation**, not to one native session. If the user
  bridges the target to another harness, the supervisor follows the new head.
- Supervisor and target may use different harnesses in either direction.
- The target is only ever read: no writes to its transcript, its state or its files.
- A supervisor resumed outside `sessions` (plain `claude --resume` or `codex resume`) still gets
  updates.

## Non-goals (v1)

- Watching the target's subagent or child sessions. The supervisor watches the main
  conversation only.
- Sending messages to the target. Codex's `codex queue` and Claude Code's cross-session
  messaging could do it later. v1 is copy-and-paste by the user.
- Putting anything on the clipboard automatically.
- OpenCode as a supervisor or a target. Nothing should preclude it, but v1 is tested on Claude
  Code and Codex only.
- Several supervisors per target. There is one, and it can be reset.
- An always-on summarizer, or any background process.

## Anti-goals (failure even if it works)

- The supervisor changes the target's working files, transcript or process.
- The supervisor answers from stale information without saying so. If an update could not be
  read (the transcript is mid-write, the target diverged, storage is missing), the injected
  context must say so plainly.
- Supervisors clutter the normal session list or are mistaken for ordinary sessions.
- The design grows extra size-handling layers beyond the brief recipe and the long-gap rule.

## Behavior

### Identity and binding

- **Registry:** `state.json` gains a supervisor record `{record id → target conversation id,
  supervisor harness, folder, created}`. Only `sessions` writes it, and only at creation and
  reset. A target with no conversation id yet is assigned one when its supervisor is created, as
  the first bridge does today. That keeps the binding stable if the target is bridged later.
- **Hook-owned state lives in the supervisor's folder, not in `state.json` (my call).**
  `state.json` has no cross-process locking: `State.save()` replaces the whole file from memory,
  and the browser stays running under the harness it launched. If the hook wrote checkpoints
  there, the browser's next save would silently overwrite them. So the fast-changing data goes
  in `<folder>/record.json`, written atomically by the hook alone: the supervisor's native
  session id, the per-member checkpoint, the last update time, and a flag saying a brief is due.
- **Bootstrapping a new supervisor:** the launcher sets an environment variable naming the
  supervisor record, for example `AI_SESSIONS_SUPERVISOR=<record id>`. Hooks inherit it. On the
  first hook call, the hook records the supervisor's own native session id in `record.json`. For
  Claude, `sessions` can pass `--session-id` so the id is known up front.
- **From then on,** the hook identifies a supervisor by its own `session_id` from the hook
  payload. That is why a resume outside `sessions` still works.
- **Ordinary sessions:** a session that is not a registered supervisor is untouched. The hook
  must exit quickly and silently for every ordinary session.

### Updates

- **Every update:** on each `UserPromptSubmit` in a supervisor, the hook:
  1. resolves the target conversation's current **head** through the existing head-routing rules
     (`specs/conversation-log/HARNESS_CONTRACT.md`);
  2. reads the records after the supervisor's checkpoint and injects them cleaned, as
     `additionalContext`, with a header giving the target's liveness ("open", "idle for 3h");
  3. advances the checkpoint.
- **Head moved:** when the target has been bridged since the last update, the update says so
  ("target now runs in Claude Code as `<title>`") and continues from the bridge frontier in the
  new member. The carried-over history is not replayed.
- **Diverged:** when two members have advanced independently, the hook injects no activity. It
  says the target diverged and names both members, and the supervisor tells the user. The
  browser's divergence handling applies as usual.
- **Unreadable or partial tail:** inject what was safely read, and say what was not. The
  checkpoint advances only past fully read records.
- **Nothing new:** inject one short line ("no target activity since <time>"), so the supervisor
  knows it is current.
- **Long gap:** see Sizes. A fresh brief is sent in place of the update.

### Brief

- **When it is sent:** with the supervisor's first prompt, ahead of the user's question, and on
  the first prompt after each of the supervisor's own compactions.
- **Detecting the supervisor's compaction:** Claude Code fires `SessionStart` with source
  `compact`. For Codex, the builder confirms the equivalent signal, or falls back to spotting a
  compaction record in the supervisor's own transcript. Either way, the hook sets the brief-due
  flag.
- **A resume does not trigger a brief,** because the harness restores the supervisor's context
  itself.
- **What it contains:**
  - the target's identity, harness and working directory;
  - the starting objective: the target's first user prompt and its title;
  - the digest;
  - the cleaned tail;
  - the current ledger and its absolute path;
  - the standing instructions.
- **The standing instructions:**
  - your role is to assess and advise, and never to modify the target or its working files,
    transcript or process;
  - keep the ledger current;
  - when asked for a message to the target, produce one clean, paste-ready block for the user to
    review;
  - say plainly when your information is stale or partial;
  - `sessions --target-log` exists for looking further back.
- **Checkpoint:** after a brief, the checkpoint sits at the end of the tail.

### Ledger

- **Location:** one Markdown file per supervisor,
  `%APPDATA%\ai-sessions\supervisors\<record id>\ledger.md`, or the Linux equivalent.
- **Contents:** the objective, the current assessment, open concerns, a decisions log, and
  messages sent to the target, with dates.
- **Who writes it:** the supervisor itself, with its normal file tools. It updates the ledger when
  its assessment changes, and records a message as sent only when the user says they sent it.

### Browser (`sessions`)

- **`S` on an ordinary conversation:**
  - If it has a supervisor, resume that supervisor.
  - Otherwise, offer a harness picker that defaults to `[supervisor] default_tool`, or to the
    *other* harness when that is unset. Then create the supervisor and launch it.
- **`S` on a supervisor row** moves the selection to its target row (my call). If the target is
  open, the status line says "target is open in another tab, switch with Ctrl+Tab". On Windows,
  Enter on an open session offers to stop it, and this key must never lead there. If the target
  is not open, `S` launches it the way Enter would.
- **Where it launches (my call):** in the current terminal, like Enter. The user keeps the target
  in one Windows Terminal tab and `sessions` in another, and `Ctrl+Tab` switches between them.
- **How supervisors look:**
  - a new origin value, `supervisor`, added to the `o` cycle;
  - revised 2026-11-01 (user decision): a supervisor's row is listed directly under its target in
    every view that shows the target, because hiding it left nothing to tell the two apart;
  - revised 2026-11-01 (user decision): `◉` marks a target that has a supervisor, except on a
    superseded copy; a supervisor row shows `◎` and its own colour; no mark means no supervisor;
  - "supervises <target>" and "supervised by <supervisor>" in the `i` details;
  - no indentation, because indentation already means parent and child.
- **Title:** shown as `Supervisor: <target title>`, because a suffix was cut off in the narrow
  title column (revised 2026-11-01). It does not follow renames of the target (my call), and the
  user can rename it with `r`. The name is published to the supervisor's own harness, so its
  terminal tab shows it (added 2026-11-02, after the user found tabs titled by the harness from
  the first question): a Claude supervisor starts with `--name`, and a Codex supervisor is stored
  as an empty, already-named rollout and resumed, because Codex cannot name a session it starts
  and titles any unnamed thread itself.
- **Reset:** from the `i` details, a reset action archives the ledger and record, hides the old
  supervisor session, and the next `S` creates a fresh one.
- **Command line:** `sessions --supervise ID [--launch-tool claude|codex]` does the same as `S`.

### Read command for the supervisor

- `sessions --target-log SUPERVISOR_OR_TARGET_ID [--since TIME] [--tail N]` prints cleaned
  target activity without moving the supervisor's checkpoint.

### Hooks

- **Entry point:** `<python> -m ai_sessions_hook <event>`, registered for `SessionStart` and
  `UserPromptSubmit` in `~/.claude/settings.json` and `~/.codex/hooks.json`. As built
  (2026-10-31): `ai_sessions_hook` is a stdlib-only module shipped beside the package, so an
  ordinary session never imports `ai_sessions` (which would cost ~0.25 s per prompt); it hands
  over to `ai_sessions.supervisor.hook` only when the environment variable is set or a record
  names the session. The command has no `-I` (that hides user site-packages such as psutil)
  and an unquoted forward-slash interpreter path (Codex on Windows cannot run a quoted one). Both harnesses accept
  `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}`, as
  the existing Codex hook proves.
- **Install and uninstall:** `sessions --install-hooks` and `--uninstall-hooks` edit those files
  idempotently, preserving every existing hook, such as `block_mass_kill.py`. Changing a Codex
  hook command makes Codex ask to re-trust it, and the install message says so.
- **Never break a prompt:** on any internal error, the hook injects a one-line warning in
  supervisors, injects nothing elsewhere, logs the error, and exits 0. It must finish within each
  harness's hook timeout. Reads must reuse cached checkpoints, because real targets reach hundreds
  of MB.
- **Supersession:** `codex_side_memory.py` is superseded. Once supervisors ship, remove its
  registration from `~/.codex/hooks.json` and update the `codex-side-memory-hook` memory note.

## Constraints

- Build on the existing adapter contract: read, checkpoint, compare and relocate in
  `capabilities.py`, plus `jsonl_tracking.py`, head routing, and the bridge's tool-call
  summarizing. No pairwise logic. If the contract lacks "records after checkpoint as turns", add
  it for the Claude and Codex adapters.
- Never write to target storage. The supervisor registry in `state.json` follows the existing
  schema-migration pattern. This is a schema bump.
- Windows (primary) and Linux, as the rest of the package.
- **Permissions (user decision):** supervisors launch with the user's current launch mode
  (Safe, Dangerous or Custom), the same as any other session. Nothing enforces read-only. The
  standing instructions forbid modifying the target.
- Branch from `main` at or after `a2de2d4` (#33).

## Acceptance evidence

Evidence for the built feature (2026-10-31) is in `BUILD-ACCEPTANCE.md`.


- **Tests:**
  - a brief's composition and its caps, including the middle cut with a note;
  - paste detection: Claude placeholders; Codex paired, unclosed and language-tagged inner
    fences; and the forgotten-closing-fence case keeping its last 500 characters;
  - an update after target appends;
  - the nothing-new line;
  - the long-gap rule sending a brief instead of an update;
  - a brief after the supervisor's own compaction;
  - partial-tail handling;
  - the target bridged Codex → Claude mid-supervision, where the next update follows the new head
    without replay;
  - divergence, which produces a warning and no activity;
  - a resume outside `sessions`, which still gets updates;
  - a non-supervisor session, untouched and fast;
  - an idempotent hook install that preserves existing hooks.
- **Live checks** on all four supervisor/target pairs (Claude or Codex as either):
  - restart the supervisor, and confirm its history and ledger are intact;
  - ask "what did the target just do?" after a known target action, and confirm the answer is
    correct;
  - force a supervisor compaction, and confirm the next prompt gets a fresh brief.

## Hook facts (measured 2026-10-31)

Versions: Claude Code 2.1.316, Codex CLI 0.165.0. Spike scripts live in the session scratchpad (`hookspike`).

| Fact | Claude Code | Codex |
|---|---|---|
| `additionalContext` limit | 10,000 chars per string, fixed (no setting). Measured: 9,000 inline; 10,500, 40k, 200k, 400k replaced by a `<persisted-output>` note with a ~2,000-char preview and the path of a file holding the full text (400k saved complete). The model read the file when asked (200k sentinel found); with the preview alone it answered NONE. | Default ~2,500 tokens (~10,000 chars). Measured: 8,000 inline; 12,000+ becomes a head+tail truncation (~10,000 chars) plus "Full hook output saved to: %TEMP%\hook_outputs\<session>\<id>.txt". Per-handler `additionalContextLimit` (tokens; `0` = unlimited) lifts it: with 60000/200000/1000000 the 200k and 400k texts were recorded intact in the session history. |
| Model receipt | Verified end to end (model repeated the sentinel). | Not verified: the account returned "Quota exceeded" on every model call. Verified only that the harness stored the full text as a developer message (`hooks.additional_context`) in the rollout. |
| `SessionStart.source` | `startup`, `resume` (measured with `--resume`), `compact` (measured with `/compact`), also `clear`, `fork` (docs) | `startup`, `resume` (measured with `codex exec resume`); `clear`, `compact`, `fork` exist in the binary and docs. Compaction was not triggered (no model quota). Codex also has separate `PreCompact` / `PostCompact` events (`trigger`: `manual`/`auto`). |
| Stdin fields | Both events: `session_id`, `transcript_path`, `cwd`, `hook_event_name`. SessionStart adds `source`; UserPromptSubmit adds `prompt`, `prompt_id`, `permission_mode`. | Both events: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`. SessionStart adds `source`; UserPromptSubmit adds `prompt`, `turn_id`. `transcript_path` may be null per docs. |
| Default timeout | 600 s for command hooks, but 30 s for `UserPromptSubmit` (docs; not measured). | 600 s for most hooks (docs; not measured). |

Notes:
- Claude's compaction SessionStart (`compact`) fires on `/compact`; the transcript path and session id stay the same across resume and compact.
- Isolated Codex hook registration without touching `~/.codex`: `codex exec --dangerously-bypass-hook-trust -c 'hooks.UserPromptSubmit=[{hooks=[{type="command",command="..."}]}]'`. A project-local `.codex/hooks.json` did not run (untrusted, even with a project trust override).
- Decision (2026-10-31, my call from these facts): both harnesses use the same rule. Text of
  at most 9,000 characters is injected inline. Anything longer, brief or update, is written to
  `brief-<n>.md` or `update-<n>.md` in the supervisor's folder, and the injection is a short
  note naming that file and telling the supervisor to read all of it before answering. Sizes
  are unchanged.
- The hook must finish well inside Claude's 30 s `UserPromptSubmit` timeout; the installer sets
  an explicit 60 s timeout on both harnesses.

## Calls made without the user (check these)

- **Launch:** the supervisor opens in the current terminal, like Enter, not in a new tab.
- **Default harness:** the opposite of the target's, unless `[supervisor] default_tool` is set.
- **Title:** `Supervisor: <target title>`, published to the supervisor's harness; it does not
  follow renames of the target.
- **Hook-owned state:** the checkpoint and the supervisor's native id live in the supervisor's
  folder, not in `state.json`.
- **`S` on a supervisor:** selects the target, and never offers to stop an open target.
- **Digest details:**
  - pastes replaced by a marker with their first ~300 and last ~500 characters;
  - typed prompt text clipped at 6,000 characters;
  - final assistant messages clipped at about 2,000 characters;
  - failed-call lines, and repeated commands collapsed.
- **Language-word fence rule:** a ```` ``` ```` followed by a language word counts as a block
  inside the paste. This is a heuristic.
- **Resume:** a resume does not trigger a brief. Only creation and the supervisor's own
  compaction do.
- **Supersession:** `codex_side_memory.py` is removed from the Codex hooks once this ships.
- **No opening prompt** (user decision, 2026-10-31): a new supervisor starts with an empty
  prompt. The brief arrives with the user's first question.
- **Marker glyphs:** `◉` on supervised targets, `◎` on supervisors (revised 2026-11-01).
- **Liveness in update headers:** computed from the target transcript's last-modified time
  ("last target activity 3h ago"). Process detection is not run inside the hook.
