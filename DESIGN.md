---
product: "ai-sessions terminal browser"
owner: "vandyand"
status: "active"
last_reviewed: "2026-08-25"
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
  with large local histories and terminals as narrow as 60 columns.
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
