---
product: "ai-sessions terminal browser"
owner: "vandyand"
status: "active"
last_reviewed: "2026-11-22"
implementation_roots:
  - "src/ai_sessions/app.py"
  - "src/ai_sessions/harnesses"
tokens:
  color:
    canvas: "terminal default"
    text: "primary"
    muted_text: "muted"
    primary: "accent"
    danger: "warning"
  spacing:
    row: "1 terminal cell"
    section: "1 blank row"
---

# ai-sessions Design Contract

## Audience And Jobs

- **Primary user:** A developer with conversation history in two or more AI coding harnesses.
- **Context and constraints:** Keyboard-first terminal use on Linux or Windows, sometimes over SSH,
  with large local histories and terminals as narrow as 40 columns.
- **Primary job:** Find the right conversation, understand where it lives and whether it is open,
  then focus, resume, or bridge it with minimal delay.
- **Success means:** The first useful list appears promptly; active filters and refresh status are
  visible; the selected session is revalidated before a consequential action.

## Experience Principles

1. Show useful cached information immediately, then label and apply fresh provider evidence.
2. Keep filters visible and reversible; combinations must not depend on remembering query syntax.
3. Preserve one-key paths for frequent actions while keeping complete help in `?`.
4. Treat provider storage as authoritative before focus, resume, rename, or bridge operations.

### Avoid

- Blocking first paint on liveness or full-history enrichment.
- Performing provider I/O during a render-only method.
- Using color as the only indication of selection, liveness, hidden state, or filtering.
- Allowing a filter dialog to apply an accidental empty tool set.

## Critical Journeys

### Find And Resume A Session

- **Entry:** Run `sessions` in an interactive terminal.
- **Actions and decisions:** Orient from the header, optionally search/filter, select a row, press
  Enter.
- **System feedback:** Cached rows appear first with a visible `refreshing` status; refreshed rows
  retain the prior selection where possible.
- **Completion and next action:** The open terminal is focused or the selected harness resumes.
- **Copy selection:** A conversation defaults to its most recently updated current copy.
  Expanded native-copy selection opens exactly that copy in its own harness. Divergence
  remains visible but does not block resume or discard other histories.
- **Recovery / exit:** `Esc` clears search, `Ctrl-R` retries refresh, and `q` exits.
- **Evidence:** `tests/test_startup.py`, `tests/test_display.py`, and launch tests.

### Inspect Before Opening

- **Entry:** Highlight a session and press `m`.
- **Actions:** Read complete messages, scroll with arrows/page keys, move with
  `p`/`n`, or jump to the latest user/assistant message with `u`/`a`.
- **Feedback:** The message role, position, and visible line range stay above the
  text; navigation controls wrap below it. History truncation is explicitly labeled.
- **Exit:** `q` or Esc returns to the same menu. Compressed preview uses a bounded
  temporary extraction and preserves native storage. No harness starts.
- **Evidence:** `tests/test_conversation_view.py`, `tools/verify_session_browser.py`.

### Choose Visible Tools

- **Entry:** Press `t` from the browser.
- **Actions and decisions:** Move with arrows or `j`/`k`, toggle with Space, apply with Enter.
- **System feedback:** Every registered tool has a visible checkbox; the proposed combination and
  the applied combination are named in text.
- **Completion and next action:** The main list and filter bar show exactly the selected tools.
- **Recovery / exit:** `Esc` cancels; `a` restores all tools; an empty selection cannot be applied.
- **Evidence:** Multi-select and empty-recovery tests in `tests/test_display.py`.

### Supervise A Session

- **Entry:** Select a session and press `S`.
- **Actions and decisions:** The first time, choose the supervisor's harness from a short list
  that starts on the sensible default; Enter accepts it. Later, `S` goes straight to the
  existing supervisor. On a supervisor row, `S` moves to its target instead.
- **System feedback:** The supervisor opens in this terminal. A supervised session carries `◉`,
  and `i` says "supervised by" / "supervises". An open target is never offered for stopping:
  the status line says to switch tabs with Ctrl+Tab.
- **Completion and next action:** The user asks the supervisor about the target; `S` on the
  supervisor returns to the target.
- **Recovery / exit:** `Esc` cancels the harness choice; `R` then `y` in `i` resets a supervisor
  so the next `S` starts fresh.
- **Evidence:** `tests/test_supervisor_browser.py`.

## Information Architecture And Language

- **Navigation model:** One session list, one detail region, and short modal pickers for filters or
  edits.
- **Core objects:** Session, tool/harness, conversation, copy, filter, launch mode.
- **User-facing verbs:** Show, search, choose, focus, resume, refresh, rename, hide, bridge.
- **Terms to avoid:** Adapter, capability registry, native reference, checkpoint, and provider
  binding in routine TUI copy.

## Visual System

- **Typography:** Terminal monospace; uppercase is reserved for stable headings and warnings.
- **Density and layout:** One row per session, a compact filter bar, and a fixed detail region.
- **Color roles:** Accent for actions, muted for secondary context, warning for risk or recovery,
  success for open/healthy state; every role also has a text or symbol cue.
- **Imagery and iconography:** Text and terminal-safe symbols only.
- **Responsive behavior:** At least 60×12. Tool, open state, title and updated age always fit.
  The title precedes other metadata and receives at least half the table width (30 cells minimum).
  Additional columns return in order: status, activity, origin, started, connection, history, directory.
  Hidden metadata remains available in the selected-session details (`i`). Compact counts and
  footer shortcuts keep launch mode, quit, tools and help visible at narrow widths.

## Component Contracts

### Session List

- Keeps selection stable across filtering and background refresh when the same session remains.
- Empty state names the relevant recovery controls.
- Rendering is side-effect-free and never starts provider or process discovery.

### Tool Picker

- Lists registered harnesses dynamically in registry order.
- States are checked, unchecked, focused, invalid-empty, applied, and cancelled.
- Space toggles, `a` selects all, Enter applies a non-empty set, and Esc cancels.

### Feedback

- **Progress:** Header and footer say `refreshing` or `Loading sessions…` during background work.
- **Empty state:** Distinguishes loading from a completed filter with no matches.
- **Error and retry:** A refresh error remains visible and points to `Ctrl-R`.
- **Destructive / undo:** Hiding still requires confirmation; tool filtering is reversible without
  confirmation.

## Accessibility And Quality Bar

- **Keyboard and focus:** Every action is keyboard-operated; picker focus uses both a marker and
  reverse/bold styling.
- **Semantic labels and status:** Checkboxes use `[x]`/`[ ]`; progress and empty states use text.
- **Contrast and non-color cues:** `NO_COLOR` and low-color terminals retain every state cue.
- **Motion:** No animation; background refresh updates once on completion.
- **Terminal constraints:** Required actions remain discoverable at 60 columns and 12 rows.

## Validation

- **Wide terminal:** 120×30 scripted render and picker journey.
- **Narrow terminal:** Positioned table assertions at 60, 65, 80, 100, 140 and 200 columns,
  including 60×12; native Windows ConPTY renders at 60, 80 and 140 columns with keyboard exit.
- **Automated journeys:** `tests/test_display.py`, `tests/test_startup.py`, and the full unit suite.
- **Visual checkpoints:** Main footer/filter bar, tool picker checked states, empty-filter recovery,
  loading status, and refreshed completion status.
- **Known research gaps:** Keyboard behavior is deterministically tested, but recognition and
  preference for `t` versus Tab require human use rather than agent inference.

## Retention

A compressed transcript is `<name>.jsonl.zst` beside where the plain file was, the form Codex writes
and reads. `compression.py` holds the file operations: the original is removed only after the copy is
read back and matches, and only while nothing else has it open: on Windows it holds the file with no
sharing, because an ordinary delete succeeds under a process that is appending. An existing `.zst` is
replaced only by a file that contains all of it, so a short file cannot destroy a full history; the
two are kept and reported instead. `retention.py` works on sessions:
harnesses name their files through `transcript_files`, and it decides what is idle, treating a
session and its subagents as one unit so that reopening a parent also protects its logs. A compressed
session's `storage` is the `.zst`, and `Availability.COMPRESSED` keeps it distinct from a missing
file, so a compressed copy can never be mistaken for no copy and bridged again. Requirements are in
`openspec/specs/session-compression/spec.md`.

### Save And Reuse A Terminal Workspace

- **Entry:** Run `sessions workspace save NAME` in the desired Terminal tab.
- **Feedback:** Capture the live pane geometry; show a spatial map with names of
  positively identified sessions. Do not make the user choose a tab index or
  infer pane creation order for the normal flow.
- **Decisions:** Automatically bind exact native sessions from verified pane
  identities. For missing identities, offer a bounded searchable picker with an
  explicit empty-shell choice. Titles alone never establish native identity.
- **Completion:** Confirm one preview, then save geometry, assignments and pane
  registrations in one transaction. Show the restore command.
- **Cloning:** Copy geometry with empty bindings; restore opens session selection
  inside each pane. Later successful launches remember those assignments.
- **Recovery:** Cancel leaves the store intact; capture errors restore temporary
  titles and never substitute an unrelated saved tab. Existing running agents
  are not stopped or sent input during capture or restore.


## Project Desk Briefing

The human's job is to regain project context, understand progress, and decide what
to discuss in the adjacent Desk conversation. The terminal briefing pane often
has only 40 columns. The approved navigation is **1 Overview / 2 Review / 3 Decisions**.
A single decision keeps the ask, context and recommendation together. Splitting
context and next action into separate views made understanding depend on recall;
a separate source reader would add another selection and scrolling mode.

### Journey and component contract

- Entry opens Overview (background, current position, direction, goal). Review is
  the rolling progress report. Decisions presents one self-contained ask at a time.
- Left/Right and Tab/Shift+Tab cycle three views; 1/2/3 select directly. n/p opens
  first/last pending decision from Overview/Review and browses pending decisions.
- A decision starts with its position and plain **Background ready** or **Background needed**.
  Summary explains the precise ask/proposal. Background carries the goal,
  definitions and prior story; Why now explains the trigger. Options include
  descriptions, benefits, risks and consequences. Recommendation identifies its
  option and reason, or explains why no option can be recommended yet. Specific
  missing facts, deferral consequences and deadlines appear when supplied.
- d reveals deeper background/source excerpts inline, in the same body scroll.
  The d Sources footer hint remains visible at 40 columns. No nested reader or secondary scroll.
  Essential context and tradeoffs never require opening the fold.
- Source excerpts use human titles and exact Eastern times when supplied as
  explicit timestamps. Byte start/end offsets are never treated as dates. Native
  IDs, paths, digests, citation IDs and decision keys are not presentation labels.
- Legacy questions retain meaningful saved context/recommendation but explicitly
  say Preparing background. Missing details never mean Background ready.
- Selection is keyed internally. Unchanged refreshes retain key, disclosure and
  scroll. A selected resolved or removed item stays visible, marked Resolved or
  No longer listed, until the user browses away; it never silently becomes a new ask.
- Empty, initial-preparing, failed-update and partial-history states qualify the
  absence of open decisions. Last good content survives read/update failures.
  Rich details prepare for at most two pending decisions per observer pass;
  remaining legacy cards retain saved context and say Preparing background while waiting.
- First row: Desk identity and source freshness plus summary age. Age alone never
  implies stale. Second row: three view names, active view bracketed. One blank
  row separates navigation from prose. The footer has contextual controls and an
  overflow-only line range. Help and Exit remain available. Help documents all
  keys, source scope, status meanings, history limits and exact Eastern times.
- Decisions provides notes, clarification and answer submission through the durable Discussion flow below.
  Background processing uses the existing observer lifetime; rendering performs no model or
  filesystem I/O. The viewer checks source stats at most once per second.
  After a package upgrade, q and reopening the view loads the new UI; either fresh
  Desk entry also replaces a verified older observer through the existing scoped
  stop protocol. Native project/operational agents need no restart or manual stop.

### Data boundary

The UI consumes the six-field core decision and `record['decision_details'][key]`
with schema_version 1: kind, readiness, summary, background, why_now, options,
recommendation, if_deferred, deadline, missing_information, citations and attached
backend-authoritative evidence. It never chooses source paths or synthesizes
missing project facts. The backend filters stale details by core fingerprint.
A reconstructed details.summary is the sole main ask when present; the lossy
legacy question is only a fallback, never a second contradictory title. In
particular, an already-run test with missing prior approval is an information
request about remembered consent, not a new approval action. Result files never
become approval evidence through UI wording.
The Discussion envelope extends this boundary without changing the legacy six-field cores.

### Implementation and validation plan

1. Pin three-view keys, complete cards, truthful legacy fallback, inline sources,
   stable folds/selection, resolved refresh and narrow layout in UI regression tests.
2. Implement pure decision formatting and state transitions in desk/view.py.
3. Update the finite native console fixture with rich decisions, disclosure and
   real keyboard transitions at 40/60/100 columns. Inspect captured console frames.
4. Run isolated UI/startup tests and lint, update the Project Desk guide, and commit
   only owned UI files for parent integration. No PR or version bump here.

Validation distinguishes deterministic keyboard/render evidence and native console
captures from human comprehension. Human use remains the measure of whether the
project explanation provides enough context; bounded evidence is not an audit.

Current validation: 221 isolated source Desk tests passed, 10 subtests passed,
1 skipped. The native Windows console fixture passes at 40/60/100 columns with
real arrow/Tab/Shift+Tab/Page/Home/End, disclosure, help and exit input. Captured
console frames were visually inspected at all three widths, including missing
context and expanded sources. `AI_SESSIONS_DESK_SMOKE_CAPTURE` saves the finite
fixture captures for integration review.

The consent-reconstruction regression passes in the isolated Desk suite,
replacing a false legacy premise without inferring prior approval.

Overview and Review append a bounded Your decisions section: total pending count,
at most two canonical hydrated summaries (selected/first), and n/p to open them.
The UI does not independently paraphrase questions. Resolved/removed retained
cards show their closed/saved status without background-readiness or a new answer
prompt. The d Sources / d Hide footer control replaces a permanent body row.

Decision enrichment status is read from decision_enrichment[key] for the latest
matching core. Failed enrichment without details says Background unavailable,
keeps saved context and directs the reader to the retry command in help:
`ses desk refresh HANDLE --retry-context`. Existing valid details survive a failed
refresh with a small note. Pending/in-progress hydration says Preparing background.
Raw exception messages are never presentation text.


## Decision backlog lifecycle

Pending request storage is independent of model context. A bounded rotating and
source-relevant working set (at most 20 requests / 24,000 characters) feeds each
synthesis. The complete pending ledger remains available in the UI; closed
versions move to paginated local history while the live projection keeps 20.
Missing model output never deletes pending records. Archive import is idempotent.

Seven-day unchanged, duplicate wording and explicit team-addressed questions are
review flags only. No age-based closure, inferred approval, or automatic semantic
merge is allowed. The Decisions card may show Review suggested; help points to
`ses desk decisions HANDLE` and `ses desk history HANDLE`. Explicit dismissal
requires the listing's revision, the selected number and a human reason. A changed
revision refuses the action. The original and reason remain in history.

Validation covers >200 pending requests, bounded prompt rotation, original-text
preservation, terminal-history retention, stale-selection rejection, canonical
summary audit, and unchanged-source model-call suppression.


### Decision input and Discussion

Decisions provides a keyboard-operated multiline editor with Save note, Ask for clarification and Submit decision. Drafts are persisted per decision; successful submission clears the input, failures preserve it. Each entry captures the exact card/version the human read. A materially changed card requires review before resubmission. One current card and latest exchange stay visible, with prior versions/comments under Discussion. Inline confirmations offer the existing n/p navigation rather than a modal.

Clarification is finite background work owned by the existing per-Desk observer. It may use only bounded root-confined read/search MCP tools; source/model content cannot execute commands or edit files. Unavailable intent/history is requested from one exact bound Desk agent, never workers or the operational supervisor. Waiting requires no running model. Replies are accepted only from the requested native identity/home. Model output is validated and cannot resolve decisions or alter Overview/Review. Changed-card results remain discussion history without replacing current presentation.

A submitted decision delivers exact human text plus canonical summary and a Discussion reference. Notes/questions are not approval. Durable transport claims prevent duplicate retries after uncertainty; offline work remains queued. Status distinguishes answer saved, notification submitted, delivery uncertain and clarification failed. All original human text and card versions remain retained.


### Decision completion and reading flow

The primary journey is question → response → explicit action → saved response/delivery status → next unanswered question. Completion is a focused inline pane, not a modal or the original unanswered card with stacked notices. The exact submitted text and Enter-to-continue control are visible before optional context. A saved human answer removes that item from unanswered navigation without claiming execution or changing the operational core. The current answered item stays selected until the user moves on. Saved answers remain available through a; b reviews the question, and e writes a further comment/correction. A materially changed core requires a new answer.

Clarification and notes stay distinct from definitive answers. Their completion panes show the saved text and live clarification state/reply, with Enter returning to the question. Delivery status distinguishes waiting, disconnected, sent, changed recipient and uncertain transport. The UI never claims a queued answer was sent. An unavailable exact recipient yields a useful next step, not raw queue terminology.

Discussion is its own reading stage. Comments/replies lead; full previous card versions and source excerpts stay behind v. Editor actions are Save note, Ask for clarification and Send answer, with visible focus and a short effect explanation. Global view navigation and the nonblocking polling/input contract remain intact.


## Answer synchronization and question order

The observer also reads the exact bound Desk conversation in its configured native home. Explicit human answers and superseding outcomes may resolve existing questions; copied briefings, agent suggestions and transport receipts cannot establish consent. Desk conversation evidence alone cannot create or reopen questions. The bounded synthesis prompt indexes pending question identities beyond the detailed twenty-item window so later answers can match older keys rather than create paraphrases.

Questions are numbered newest-first by persisted first-seen order, with insertion sequence breaking ties. Editing or hydrating an old question does not make it new. Imported history has a known relative order, not fabricated original timestamps. Saved answers survive first-time choice hydration; corrected information asks and changed underlying cores still require review. The view and generated decisions file exclude matching saved answers while preserving discussion and operational status separately.


## Decisions: visual hierarchy and explicit completion

Andrew's job is to understand the specific proposal, respond or close it, and move on. Success requires a self-contained ask with actual behavior, impact, options and tradeoffs, plus persistence of the user's exact input. A failed lookup must not leave an opaque approval request looking ready.

| Journey | Contract |
| --- | --- |
| Orient | Project/status and view tabs form chrome; the numbered question is a bold title. Primary reading controls remain in a separate action row. |
| Understand | Context, options and recommendation use accent headings; options are bold content, not pretend selectable controls. Required background/tradeoffs remain visible; secondary timing/source detail is disclosed with d. |
| Respond | e opens the response editor. User text is bold and visibly separate from model copy. Bracketed action buttons use a pointer plus reverse fill for keyboard focus. Tab/Shift+Tab cycle input and actions. |
| Complete | c opens the same editor at Mark as completed. Text is optional. Completion closes the question, archives the original card and exact comment, and cannot be reversed by model publication. It does not send an operational approval. |
| Recover | u undoes completion in place with visible confirmation. Original comments remain. Changed-card submission preserves the draft. Failed background has r Retry on the card, bounded automatic retries, and an explicit unavailable state. |
| Continue | Inline success offers Enter for the next unanswered question. Completed items remain accessible with saved responses and Discussion. |

Terminal roles are explicit rendering metadata, never guessed from user/model text: header/tabs = bold; question/option = bold; heading/action = cyan accent plus bold; user = bold with a visible comment rail; success = green plus bold; metadata/hints = subdued; focused action = reverse plus bold and a pointer. Colors use the terminal palette/default background and honor NO_COLOR. Weight, brackets, pointer, spacing and labels retain hierarchy without color. The pure frame carries roles through wrapping and scrolling, and the native paint uses those roles.

Validation uses isolated persistence/retry tests and actual Windows console input at 40/60/100 columns, including completion/undo, typed comments, focus, arrows, wheel and rapid navigation. Native character/attribute captures verify actual color, bold and reverse states. Renderings of those captured cells are visual QA artifacts, not claimed desktop pixel screenshots. They provide implementation evidence, not a substitute for Andrew's usability feedback.

Sources: IBM Carbon button hierarchy https://carbondesignsystem.com/components/button/usage/ and USWDS typography/spacing https://designsystem.digital.gov/components/typography/.
