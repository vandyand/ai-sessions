# Project Desk

Updated November 22, 2026.

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

Either command can be first: each initializes the Desk and starts or attaches to
one background observer. The conversation uses the target's harness; the briefing
uses Haiku independently. The first briefing prepares in parallel, so opening the
conversation does not wait for a model response. Closing either pane leaves the
observer running. Add `--no-watch` to read an existing saved briefing without
starting observation. Existing saved names such as `elevate` also work.
`show`, `refresh`, `watch`, `open`, `status`, and `stop` also accept the session
handle, native ID, or slug.

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
- `3`: Decisions — each ask, its context, options and recommendation together.
- Left/Right and Tab/Shift+Tab switch views, wrapping at the ends.
- `n` / `p` opens the first/last pending decision from Overview or Review, or cycles
  pending decisions in Decisions. The shortcut appears only when useful.
- `d` shows or hides deeper background/source excerpts inline. The whole decision
  uses the same Up/Down/Page/Home/End scroll; there is no separate source reader.
- Up/Down and Page Up/Page Down scroll when content exceeds the pane. A line range
  appears at bottom right; no scroll hint is shown when everything fits.
- `h` or `?` opens help, including the exact watched sessions and status meanings.
  `h`, `?` or Esc returns. `q` closes the briefing.

Overview and Review include a short **Your decisions** section with the pending
count and at most two summaries from the same canonical decision records. `n` / `p`
opens Decisions. These teasers do not generate separate versions of the questions.

A decision shows **Background ready** or **Background needed**, followed by a precise description
of the ask or proposal. Its normal view includes project background/definitions,
why the question matters now, explicit options with benefits/risks/consequences,
and a recommendation with its reason. Missing definitions or numbers are listed
as specific missing information. The briefing does not invent yes/no choices or
recommend an option without support. Deferral consequences and deadlines appear
when supplied. Action requests and requests for information do not need choices.

Use `d Sources` for human-titled source excerpts. Earlier context is labeled;
explicit source timestamps appear in Eastern Time. Internal decision keys, native
IDs, paths, byte ranges and digests are not source labels. Essential context and
tradeoffs remain in the normal card. For older saved requests, meaningful context
and earlier suggestions stay visible while the background/options prepare; the
card says Preparing background rather than claiming it is ready.

Richer details prepare in the background, up to two pending decisions per observer
pass. With four older requests and the default 60-second interval, filling all
four can take roughly two minutes plus model time. Existing questions and saved
context remain readable during preparation; a missing detail card stays marked
Preparing background. Continue discussing the project while the remaining cards prepare.

If the latest background lookup fails, a request without details says
**Background unavailable**, retains its saved question/context, and offers a
retry step. It does not stay labeled Preparing background forever. If valid
details already exist, they remain readable with a small update-unavailable note.
Help includes the explicit retry command:

```powershell
ses desk refresh HANDLE --retry-context
```

Refresh preserves the viewed decision, disclosure fold and scroll when content
is unchanged. If the selected item resolves, becomes superseded, or disappears,
it remains visible with that status until you browse to another pending decision
or leave the view. A new request never silently replaces the one being read.
Press e to write a note, ask for clarification or submit an answer directly from Decisions; t opens Discussion. The Desk conversation remains available for broader project discussion.

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

The viewer polls saved state once a second and shows the detached observer's busy
state as `Updating briefing` (or `Preparing` before the first briefing). The
observer checks selected sources every 60 seconds by default. Model calls happen
only when conversation evidence needs a new synthesis; unchanged sources do not
cause unconditional model reruns. `--interval 120` (5–3600 seconds) selects the
interval when starting a worker; attaching preserves its existing interval.
Failed observations retain retry/backoff, up to 15 minutes. Closing the view does
not stop the observer, supervisor, or workers. The saved briefing stays available.

Manage only this Desk's observer with:

```powershell
ses desk status elev-sup
ses desk stop elev-sup
```

`status` reports running/stopped state, busy state, exact Desk root/name, PID and
process start identity when running. `stop` cooperatively cancels that observer
and its owned model process; it never stops the operational sessions. Reopening a
live view or conversation starts it again. No scheduled task, reboot autostart,
or global watcher is installed. Each worker belongs to this Desk until explicitly
stopped or its process exits. After reboot, open either pane to start it again.
Cross-process OS locks prevent duplicate workers even with simultaneous opens;
process exit releases ownership. Stale PID metadata alone cannot claim ownership.
The worker and model child start without a console window on Windows. Session
sender/parent identity is removed from the worker environment; configured provider
homes and login remain unchanged. Diagnostics are bounded lifecycle messages in
`DESK_ROOT/.observers/<desk-key>/observer.log`, alongside `status.json` and scoped
`stop.json`. They contain no prompts, transcripts, model output, or credentials.
The exact directory is included by `ses desk status`.

Ask follow-up questions and type answers in Decisions using e, or discuss the project in the adjacent conversation. Drafts are saved per decision in the view; submitted answers include an exact decision reference. When discussing an item directly in the conversation, refer to it by its words. The project agent reads current files before discussing current status; ordinary review refreshes stay silent. A newly added human request queues one compact informational notice for each explicitly bound Desk conversation. Edits, resolutions and reopened existing keys do not notify. Offline notices wait until the Desk is reachable; resolved questions are skipped and uncertain submissions are not retried.

```powershell
sessions desk list
sessions desk show my-project --json
sessions desk refresh my-project
sessions desk watch my-project --interval 120
sessions desk open my-project --tool claude --dry-run
```

`view --no-watch` and `open --dry-run` never start a worker or call a model. A previously running worker continues independently. Legacy `--watch` remains accepted. `watch` now ensures the same background worker and returns. `refresh` explicitly performs one observation; avoid invoking it concurrently with background work because it may duplicate a model call.

## Briefing format and recovery

The summarizer uses native structured output constrained to the briefing schema
and currently supplied citation IDs. Semantic evidence validation still runs before
publication. An unreadable reply preserves the previous review, pending requests,
and source cursor; the live observer retries with backoff. A complete legacy JSON
code fence is accepted, but surrounding commentary and malformed JSON are refused.

Review leads with progress, outcomes, and the next human action. Source citation
details remain in saved records rather than terminal prose. Partial-history details
are available in help. Desk display and Markdown timestamps use US Eastern Time (EST/EDT).

After upgrading, close an old view with `q` and rerun `ses desk view HANDLE`, or
open a new Desk entry with `ses desk HANDLE`. The new launcher checks the verified
observer version, gracefully stops an older worker through its scoped stop
protocol, and starts the updated observer. The refreshed view loads the new UI.
Neither the operational agent nor the native Desk conversation needs a restart;
there is no required manual observer-stop step.

## Observation and costs

The default summarizer is `claude -p --model haiku` using the CLI's existing authentication. It has no tools, MCP servers, custom hooks, skills or session persistence. It does not log in, read credentials itself, send operational messages, approve actions or edit source transcripts. It consumes the active Claude account's plan/API allowance; it is not free. Override the model for the current process with `AI_SESSIONS_DESK_MODEL`, for example `sonnet`. This initial implementation does not automatically escalate models or use Luna.

The observer reads bounded windows of plain Claude/Codex JSONL: at most 256 KiB per source, then at most 12,000 characters of user/assistant text, plus previous synthesis. It records cursors and source coverage. Tool output is excluded; compressed/non-JSONL storage is unsupported. Initial/omitted history is marked partial. Thus the review is a useful running synthesis, not a full overnight audit or independent evidence of completion. A large initial replay/backfill is a later improvement.

Pending requests are preserved if omitted from model output. Explicit current cited evidence can resolve or supersede them even when coverage is partial; missing questions alone never imply resolution. These are model-attributed judgments, not operational approvals. The current snapshot keeps every pending request plus the most recent 20 resolved/superseded records. Older terminal records leave the current view; this prototype is not an immutable decision audit. The underlying native conversations remain untouched.

Pending requests are stored independently of the model context. Each synthesis sees
at most 20 requests in a 24,000-character working set; the selection combines a
rotating portion with requests textually related to current source activity.
Large individual records are excerpted only in model input, with an explicit
excerpt marker. All original text remains stored and available in Decisions.
Unchanged sources still do not cause unconditional model calls.

Closed/superseded requests leave the live projection after its newest 20 entries,
but their complete recorded versions and available background details are archived
in SQLite. No unanswered request is aged out or silently removed. The archive is
local to the Desk database and is created automatically; no scheduled job is added.
Migration preserves requests still present when upgrading; entries already dropped
by earlier versions are not reconstructed.

Review the backlog without changing it:

```powershell
ses desk decisions elevate
ses desk decisions elevate --json
ses desk history elevate --limit 50
ses desk history elevate --offset 50 --json
```

The listing flags unchanged requests after seven days, exact duplicate wording
(using the canonical summary when available), and obvious worker/manager-addressed
questions as review candidates. Imported requests have unknown original age. These
flags are suggestions, not conclusions that a question has been answered.

To dismiss one request explicitly, use its number and the revision from that
listing. If the Desk changes in between, the command refuses and asks you to list
again, preventing a changed number from targeting a different question:

```powershell
ses desk dismiss elevate 3 --revision 123 --reason "Repeated request; already covered by decision 1"
```

Dismissal marks the request superseded and preserves the original, its context,
and your reason in history. It does not approve work, infer consent, or send a
message to operational sessions.

Errors preserve the last good synthesis and display its age/error. Observation time and synthesis time are separate. Source material is untrusted data, and a generated recommendation is never user consent. The model cannot send messages. The host sends new-request notices and explicitly requested human feedback/context questions only to bound human-facing Desk conversations.

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


## Write directly in Decisions

Press **e** on an open decision to enter a multiline note, clarification question or final answer. While typing, Enter adds a line; navigation letters remain text. Tab/Shift+Tab selects **Save note**, **Ask for clarification**, or **Submit decision**; Enter activates the selected action. Esc returns to the card with your draft saved. The input clears only after a successful durable submission. A changed card blocks submission and keeps your draft so you can close the editor, review the current proposal and reopen it.

**Save note** adds a comment without requesting work or sending a message. **Ask for clarification** saves your question and runs one bounded background model call. It uses the current Overview, Review, selected card, recent discussion and retrieved source context. Only a project-scoped read/search MCP server is enabled: no built-in shell, write tools, external MCP servers, hooks or skills. Reads reject paths outside the project, sensitive filenames and generated directories. Search has time/file/byte limits; at most eight bounded file excerpts and twenty tool calls are available per call. The normal briefing observer remains tools-disabled.

The clarification call returns a short reply and optionally a complete replacement of the decision presentation. One current card is displayed, followed by the latest question/reply. Press **t** to expand **Discussion**, including immutable prior cards, your comments, replies and source excerpts. Updating presentation never changes the decision's operational status or implies approval. An interrupted/failed clarification stays saved; **r** retries a failed clarification. A stale result remains in Discussion without replacing a changed card.

If evidence and code cannot answer, the app queues a scoped context question to one explicitly bound Desk conversation and shows **Waiting for project context**. It does not contact operational supervisors or workers. No model process remains running while waiting. The Desk agent responds from its own session using the exact command in the notice: `sessions desk reply HANDLE ENTRY --message "context"`. The native session ID and harness home must match the requested recipient. A one-off clarification resumes when that reply arrives; unresolved facts remain explicit rather than creating an automatic question loop.

**Submit decision** sends your exact answer, the canonical decision summary and a stable on-disk Discussion reference to the bound Desk conversation. The full discussion is not copied into the message. The view distinguishes saved/queued, submitted and unconfirmed delivery. Offline notifications wait; an uncertain transport result is not automatically retried. A submitted answer does not claim execution or automatically close the card. Open the Desk conversation if none is bound yet. Delivery is processed by the existing per-Desk observer and survives closing the view. `ses desk stop HANDLE` also stops pending clarification processing; reopening the live view or receiving an authenticated context reply resumes queued work.

Discussion schema version 1 stores each human entry with its original card/version, immutable text and creation time, plus separate background status/reply/result fields. Decision presentation keeps its existing schema version 1; the new discussion envelope adds versioned interaction data without changing legacy cores. SQLite is authoritative; per-decision `discussion-*.json` files are replaceable references for the bound agent. No human comment or version is garbage-collected by age.


### Input responsiveness

Keyboard navigation and rendering use the last published in-memory snapshot. A view-owned background reader polls SQLite, source files and observer process status; slow disk/process checks cannot queue arrow keys behind them. Mouse wheel events scroll the view body where the terminal supports mouse reporting. Native Windows smoke coverage includes rapid 20-key scroll bursts followed immediately by a tab switch, wheel up/down, and the existing 40/60/100-column input flow.
