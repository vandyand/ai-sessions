# Saved terminal workspaces

Updated November 14, 2026. Version 3.17.0 adds prompt-free layout reuse and strict
noninteractive saves; 3.16.0 added native Terminal workspace reopening.

Use a workspace to save a Windows Terminal tab's pane arrangement and its agent
sessions, restore it after a restart, or reuse its arrangement for another
project. This guide does not authorize stopping other sessions or changing
credentials.

## Reopen from Terminal's workspace menu

Windows Terminal 1.26 or newer can remember named windows. ai-sessions supplies
the durable conversation assignment for each pane. Choose its executable once:

```powershell
sessions workspace terminal 'C:\path\WindowsTerminal.exe'
```

Portable mode is detected through its `.portable` marker. For a packaged
installation, supply its own state path explicitly:

```powershell
sessions workspace terminal 'C:\path\WindowsTerminal.exe' --state-file 'C:\path\LocalState\state.json'
```

Then open an already-saved ai-sessions workspace:

```powershell
sessions workspace open elevate
sessions workspace open elevate --dry-run
```

First use creates a separate named window with the saved pane arrangement. It
resumes each assigned conversation through the normal managed launcher. A session
already running elsewhere is skipped; quit it normally before trying to resume
it here. Unassigned panes offer the existing searchable picker.

Close the new window normally once so Terminal saves it. Thereafter reopen it
using the same command or Terminal's workspace menu. Its name begins
`sessions-elevate-` and ends with a database identifier. The suffix keeps separate
workspace stores from targeting the same live window. Terminal retains subsequent
split-size and position changes; ai-sessions retains the current pane assignments.
This restores conversations at saved turns, not interrupted tool execution.

The command checks that persisted pane launchers still belong to this workspace,
Python installation, app state and workspace database. If panes were added,
removed, or given unrelated shell commands, use a new workspace name. Rebinding
a conversation in an existing slot does not change its durable launcher.

Before the first successful close, a repeated open refuses to duplicate an
unsaved window. If that first launch failed, retry in a separate new window:

```powershell
sessions workspace open elevate --recover
```

Recovery uses a new window name; an existing agent is still protected by the
normal duplicate-launch checks. It does not close or append panes to the older
window. No Terminal settings or state are edited. The chosen executable and
native-window registrations live in the ai-sessions workspace SQLite database.
Regular `workspace restore` continues to use its original Terminal behavior.

On November 13, a portable Preview 1.26.3133.0 close/reopen probe restored one
Codex 0.167.1 and one Claude Code 2.1.325 conversation to their original slots,
with the same native IDs and new process generations. The probe sent no prompts
and closed only its own test window. Actual reboot recovery and graphical flyout
interaction remain unverified. `tools/verify_native_workspace.py` can repeat the
probe with explicitly supplied disposable native session IDs.

## Save only the layout, then open blank panes elsewhere

Run inside a pane of the tab whose arrangement you want to capture:

```powershell
sessions workspace save elevate-layout --layout-only
```

This never asks for input and does not resolve, assign or register the running
sessions. It captures geometry and saves an assignment-free layout. Capture can
still fail; failure reports an error instead of starting interactive setup. There
is no measured field success percentage for capture from agent shell commands.
A disposable 12-pane test with redirected stdin verified this path.

Open that layout as plain shells in a new Windows Terminal window:

```powershell
sessions workspace restore elevate-layout --empty --window -1
```

There are no agent launches or session pickers. Large layouts (eight or more
panes) open maximized in new windows so minimum pane sizes do not drop splits.
Existing windows are not resized. To keep a separate reusable copy:

```powershell
sessions workspace clone elevate-layout another-project
sessions workspace restore another-project --empty --window -1
```

The new shells clear inherited managed-pane assignments. They keep ordinary
PowerShell profiles and remain usable for launching whichever sessions you want.

## Save the layout and its identified sessions

```powershell
sessions workspace save elevate
sessions workspace save elevate --check
```

Default save never prompts. Every pane must have a verified session assignment;
otherwise it lists unidentified panes, exits with code 2, and saves nothing.
`--check` reports the current snapshot without saving; it is a preflight, not a
promise that a later capture cannot change. `--yes` is retained for compatibility
and also never prompts; it is unnecessary for ordinary noninteractive saves.

For manual assignment, run explicitly in an interactive PowerShell shell:

```powershell
sessions workspace save elevate --interactive
```

Only this mode offers session pickers and a final confirmation. Unidentified
panes are not silently treated as empty. Use `--layout-only` when you want only
geometry. `--force` replaces an existing workspace, unless its managed agents are
still running. Failures leave the saved workspace untouched.

Saving neither opens extra panes nor restarts agents. The existing native
session registrations supply exact identities; titles/directories are never
used to guess a conversation. No 99% capture or identification success rate has
been established. The guarantee is that ordinary saves never request input.

## Restore after a PC restart

```powershell
sessions workspace restore elevate
```

This opens a new tab with the saved arrangement and resumes each assigned native
session at its latest saved turn. An empty assignment opens a searchable picker
in that pane. Pick its session there, or choose `empty` to leave an ordinary shell.
Successful launches are remembered automatically.

A running session is skipped and left alone. Restore does not move an active
agent to another terminal or restart it. After quitting that agent normally,
restore again to resume it in the saved layout. Native session identity and
supervisor role are preserved, rather than picking a different fork with a newer
turn. Account/home mismatches are refused instead of switching credentials.

```powershell
sessions workspace restore elevate --dry-run
sessions workspace restore elevate --window -1
sessions workspace list
sessions workspace show elevate
```

`--dry-run` prints the launch arguments. `--window -1` opens a separate window;
the default opens a tab in the current or most recently active Terminal window.
PowerShell remains usable after an agent exits. This resumes conversations, not
in-flight tool calls, unsaved terminal scrollback, or an entire running process.

## Reuse the layout for another project

```powershell
sessions workspace clone elevate another-project
sessions workspace restore another-project
```

The clone has the same split arrangement and no session assignments. Its panes
open with pickers: choose the other project's manager, workers or supervisor in
the positions where you want them. Subsequent restore uses those assignments;
the original workspace keeps its own sessions. Cloning retains shell starting
directories; a chosen agent launches in its own saved session directory.

## How automatic assignment works

Live capture uses Windows console APIs and UI Automation. A separate helper
briefly refreshes unique temporary console-title markers while collecting multiple
samples. Exact matches are accumulated against stable UIA pane identities. Pane bounds
and those exact markers connect each visible pane to its recorded native session.
Original or newer application titles are preserved afterward, including capture
failures. Active agent title updates no longer silently erase a one-shot identity
read. Unregistered or otherwise unverifiable panes remain explicitly unidentified. No conversation text,
terminal buffer, screenshots, keystrokes, credentials or authentication files are
read by this mechanism. Other tabs are excluded from the saved snapshot.

Sessions launched through `sessions` have native identities recorded against
Terminal's per-pane `WT_SESSION` value. Legacy records lacking a harness home are
used only if the exact native session resolves in the active home. Sessions
launched directly with a harness, missing runtime records, and panes that expose
no identity may need a one-time picker selection. A title or shared directory is
never sufficient evidence for automatic assignment.

## Storage and explicit controls

Workspace geometry, assignments and existing-pane registrations live beside
ai-sessions' state as `state.workspaces.sqlite3`. `AI_SESSIONS_WORKSPACE_FILE`
overrides that path; `AI_SESSIONS_STATE_FILE` selects ordinary app state. Bindings
have no seven-day expiry. SQLite launch leases prevent two managed launches from
opening the same native session or pane simultaneously.

For scripts, manual binding remains available:

```powershell
sessions workspace bind elevate pane-0 SESSION_SLUG_OR_ID
```

IDs, harness-qualified IDs and unique slugs work. `--move` explicitly moves an
assignment already used in another slot of the same workspace. The old
`restore --allow-unbound` flag remains accepted; it is no longer required.

The older persisted-state importer remains available explicitly:

```powershell
sessions workspace save imported-layout --window-index 0 --tab-index 5
sessions workspace save imported-layout --state-file C:\path\terminal-state.json
```

Those flags import saved geometry only; they do not identify the current tab or
assign agents. The `--state-file` flag here selects Terminal's input, not the
ai-sessions state. Terminal's saved geometry may lag the screen. Normal live save
needs no Terminal settings change, never edits Terminal settings/state, and never
replays original shell command lines.

## Verified behavior

On November 12, Windows Terminal 1.24.12741.0 and PowerShell 7 passed disposable
native tests: cross-process identity markers, live bounds/splits, automatic
assignment of both tracked panes, exclusion of an unrelated tab, restoration of
original titles, atomic save and empty layout cloning. Paths included spaces,
apostrophes and semicolons. Automated tests cover cancellation, unknown-pane
selection, bounded narrow maps, profile/home refusal, exact-native and supervisor
resume, durable existing-pane tracking, and duplicate managed launch guards.

For messaging between sessions, see [SESSION_MESSAGING.md](SESSION_MESSAGING.md).

The November 12 follow-up reproduced lost identities in the real multi-pane tab:
all console marks were set, but several were overwritten before the one-shot UIA
read. Refreshing markers and collecting stable-pane samples recovered the missing
known agents. A disposable native test also passed with both applications changing
their titles every 10 ms. Missing identity is still distinct from an empty shell.
