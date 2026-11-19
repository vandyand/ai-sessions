# Project Desk

Updated November 19, 2026. First local experiment.

Project Desk gives a busy project's human a quiet native Claude Code or Codex conversation, alongside a live terminal briefing. The operational supervisor continues coordinating workers and managers. The human-facing project agent discusses ideas, explains questions and conveys settled direction when asked; it does not receive a stream of team chat notifications.

## Start

Choose the exact supervisor session ID or unique slug. Add other sessions only when their activity should contribute to the briefing. No project-name matching or automatic team membership is inferred.

```powershell
sessions desk create my-project --supervisor SUPERVISOR_SLUG --source MANAGER_SLUG
sessions desk open my-project --tool codex
```

In an adjacent terminal pane:

```powershell
sessions desk view my-project --watch
```

`open` uses the current pane and preserves a native conversation per harness. Repeat the same command to resume it. Each open supplies the project-agent instructions again, including after an interrupted first startup. Claude and Codex have separate conversation histories; switching `--tool` starts/resumes the other history, while both read the same briefing. This first version does not automatically transpile the human conversation between harnesses. `sesres` uses the existing managed native-launch mechanism.

The project conversation runs in the desk folder so it can read the briefing. Its instructions identify the actual project directory and operational supervisor. Native permission settings remain in force. No bypass flags are added.

## Read the briefing

- `1`: Review, a running synopsis of observed work.
- `2`: Needs you, current questions and requested human actions with context.
- `3`: Next action, a focused recommendation for one pending item.
- `n` / `p`: next / previous pending item.
- Arrows and Page Up / Page Down: scroll. `q`: close the view.

The viewer polls saved state once a second. `--watch` runs the observer alongside it, checking selected sources every 60 seconds by default. Model calls happen only when new conversation text is found. Change the interval with `--interval 120` (5–3600 seconds). Failed observations back off, up to 15 minutes, rather than repeatedly consuming usage each minute. Closing the view stops its observer and its owned model process. It does not stop the supervisor or workers. The last saved briefing remains available.

Ask follow-up questions and type answers in the project conversation. This version does not share selection or drafts between the two panes. To avoid ambiguity, refer to a question by its words. The project agent reads current files before discussing current status; no observation is pushed into its chat automatically.

```powershell
sessions desk list
sessions desk show my-project --json
sessions desk refresh my-project
sessions desk watch my-project --interval 120
sessions desk open my-project --tool claude --dry-run
```

`view` without `--watch` is read-only and makes no model calls. `refresh` performs one observation. `watch` runs until interrupted. Do not run multiple watchers deliberately: stale concurrent results are discarded, but overlapping model calls still consume usage.

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
