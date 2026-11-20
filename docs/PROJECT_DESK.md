# Project Desk

Updated November 20, 2026.

Project Desk gives a busy project's human a quiet native Claude Code or Codex conversation, alongside a live terminal briefing. The operational supervisor continues coordinating workers and managers. The human-facing project agent discusses ideas, explains questions and conveys settled direction when asked; it does not receive a stream of team chat notifications.

## Quick start with session handles

Use the same supervisor handle in both panes:

```powershell
ses desk elev-sup
```

In the adjacent pane:

```powershell
ses desk view elev-sup
```

The first command creates or resumes the quiet conversation using the target's
harness. The second resolves its saved desk and updates the briefing with Haiku
while open. Add `--no-watch` to read saved briefings without model calls. Existing
saved names such as `elevate` also work; no rename or migration is required.
`show`, `refresh`, `watch`, and `open` also accept the session handle, native ID,
or slug. If no desk exists yet, start it with `ses desk HANDLE` first.

## Start

Choose the exact supervisor session ID or unique slug. Add other sessions only when their activity should contribute to the briefing. No project-name matching or automatic team membership is inferred.

```powershell
sessions desk create my-project --supervisor SUPERVISOR_SLUG --source MANAGER_SLUG
sessions desk open my-project --tool codex
```

In an adjacent terminal pane:

```powershell
sessions desk view my-project
```

`open` uses the current pane and preserves a native conversation per harness. Repeat the same command to resume it. Each open supplies the project-agent instructions again, including after an interrupted first startup. Claude and Codex have separate conversation histories; switching `--tool` starts/resumes the other history, while both read the same briefing. This first version does not automatically transpile the human conversation between harnesses. `sesres` uses the existing managed native-launch mechanism.

The project conversation runs in the desk folder so it can read the briefing. Its instructions identify the actual project directory and operational supervisor. Native permission settings remain in force. No bypass flags are added.

## Read the briefing

- `1`: Overview — background, current position, direction and goal (the opening view).
- `2`: Review — recent progress and outcomes.
- `3`: Needs you — open questions with context.
- `4`: Next action — a focused suggested response.
- Left/Right and Tab/Shift+Tab switch views, wrapping at the ends.
- `n` / `p` opens the first/last question from Overview or Review, or cycles questions
  from Needs you / Next action. The shortcut appears only when it is useful.
- Up/Down and Page Up/Page Down scroll when content exceeds the pane. A line range
  appears at bottom right; no scroll hint is shown when everything fits.
- `h` or `?` opens help, including the exact watched sessions and status meanings.
  `h`, `?` or Esc returns. `q` closes the briefing.

The top-right indicator separates summary age from source freshness:
`Up to date · 2h` means the summary last changed two hours ago and the current
session files still match it. `Supervisor has new content` means that source has
changed; `Updating briefing` appears while the local reviewer is working on it.
With multiple sources the label says `Watched sessions have new content` instead.
`Saved` means current sources have not been verified. Read/update failures remain
visible and preserve the saved briefing. Exact Eastern timestamps and history
limitations are available in help rather than a permanent metadata row.

Only the supervisor is watched by default. Managers and workers contribute only
when explicitly added with `--source`; team membership is not inferred.

The viewer polls saved state once a second. The view runs the observer alongside it by default, checking selected sources every 60 seconds by default. Model calls happen only when new conversation text is found. Change the interval with `--interval 120` (5–3600 seconds). Failed observations back off, up to 15 minutes, rather than repeatedly consuming usage each minute. Closing the view stops its observer and its owned model process. It does not stop the supervisor or workers. The last saved briefing remains available.

Ask follow-up questions and type answers in the project conversation. This version does not share selection or drafts between the two panes. To avoid ambiguity, refer to a question by its words. The project agent reads current files before discussing current status; no observation is pushed into its chat automatically.

```powershell
sessions desk list
sessions desk show my-project --json
sessions desk refresh my-project
sessions desk watch my-project --interval 120
sessions desk open my-project --tool claude --dry-run
```

`view --no-watch` reads saved briefings and makes no model calls. Legacy `--watch` remains accepted. `refresh` performs one observation. `watch` runs until interrupted. Do not run multiple watchers deliberately: stale concurrent results are discarded, but overlapping model calls still consume usage.

## Briefing format and recovery

The summarizer uses native structured output constrained to the briefing schema
and currently supplied citation IDs. Semantic evidence validation still runs before
publication. An unreadable reply preserves the previous review, pending requests,
and source cursor; the live observer retries with backoff. A complete legacy JSON
code fence is accepted, but surrounding commentary and malformed JSON are refused.

Review leads with progress, outcomes, and the next human action. Source citation
details remain in saved records rather than terminal prose. Partial-history details
are available in help. Desk display and Markdown timestamps use US Eastern Time (EST/EDT).

After upgrading, press `q` in an already-open briefing and rerun `ses desk view HANDLE`
to load the updated observer. The operational agent need not restart.

## Observation and costs

The default summarizer is `claude -p --model haiku` using the CLI's existing authentication. It has no tools, MCP servers, custom hooks, skills or session persistence. It does not log in, read credentials itself, send operational messages, approve actions or edit source transcripts. It consumes the active Claude account's plan/API allowance; it is not free. Override the model for the current process with `AI_SESSIONS_DESK_MODEL`, for example `sonnet`. This initial implementation does not automatically escalate models or use Luna.

The observer reads bounded windows of plain Claude/Codex JSONL: at most 256 KiB per source, then at most 12,000 characters of user/assistant text, plus previous synthesis. It records cursors and source coverage. Tool output is excluded; compressed/non-JSONL storage is unsupported. Initial/omitted history is marked partial. Thus the review is a useful running synthesis, not a full overnight audit or independent evidence of completion. A large initial replay/backfill is a later improvement.

Pending requests are preserved if omitted from model output. Explicit current cited evidence can resolve or supersede them even when coverage is partial; missing questions alone never imply resolution. These are model-attributed judgments, not operational approvals. The current snapshot keeps every pending request plus the most recent 20 resolved/superseded records. Older terminal records leave the current view; this prototype is not an immutable decision audit. The underlying native conversations remain untouched.

Pending state is limited to 80 requests and 24,000 serialized characters so it stays within the next synthesis budget. An oversized model result is refused before publication, preserving the last usable briefing and source cursor. The failure is visible; it never silently drops an unresolved request.

Errors preserve the last good synthesis and display its age/error. Observation time and synthesis time are separate. Source material is untrusted data, and a generated recommendation is never user consent. The observer itself never sends any messages.

## Storage

Nonsecret local state lives under `AI_SESSIONS_DESK_DIR`, defaulting to the ai-sessions config directory's `desk` folder. `desk.sqlite3` is authoritative. Each named subfolder contains atomic Markdown projections `review.md` and `decisions.md`, and launcher instructions `project-agent.md`. Both projections carry the same revision; if a crash leaves mismatched revisions, use `show --json` and regenerate. Do not hand-edit the Markdown as a way to change state.

Desk records pin exact source transcripts and native session IDs. Reopening with `ses desk HANDLE` reuses the desk through explicit handle membership and updates its primary source to the current bridged copy, preserving the human conversations and other selected sources. A fork is a separate conversation and does not reuse the old desk automatically. The observer can read offline transcripts; it does not wake those sessions. See [session handles](SESSION_HANDLES.md) for the shorthand and harness overrides.

## Validation

Unit tests use fictional transcripts and injected model responses. A finite native Windows console smoke test exercises all three views and exit (`tools/verify_project_desk.py`). A real Haiku smoke request and an initial observation of the explicitly selected Elevate supervisor and two managers succeeded on November 19. The first real-data observation and native launcher checks are recorded in the local agent-docs guide. Operational team messages are not sent as a test. User feedback on reading, moving between panes, stale questions and draft ambiguity should determine whether native Claude Mods / a Codex TUI patch is worth adding next.

## Project overview

Overview is retained separately from the rolling Review. Its four fields use
supplied evidence; unknown goals are identified as missing rather than invented.
On an existing desk without an overview, the next observer pass can read the
supervisor's first 16 KiB, stopping at a complete JSONL record and keeping at most
6,000 characters of conversation text. This supplements the bounded recent tail
once. It cannot resolve current human requests from old opening context.
Subsequent unchanged checks make no model call. The overview is stored atomically
with Review and the questions and included in the Markdown projection.
