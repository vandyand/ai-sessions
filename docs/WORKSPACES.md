# Saved terminal workspaces

Updated November 12, 2026.

Use a workspace to save a Windows Terminal tab's pane arrangement and the exact
native session assigned to each pane. Restore opens a new tab; cloning copies the
arrangement with empty session slots for another project. These commands do not
grant permission to stop other sessions or change credentials.

## Save and bind once

Windows Terminal must have **Settings → Startup → When Terminal starts → Open
windows from a previous session** enabled. `workspace save` reads Terminal's
persisted layout, which can lag the arrangement currently visible on screen.
It never changes Terminal settings or its saved-state file.

```powershell
sessions workspace save elevate
sessions workspace show elevate
sessions workspace bind elevate pane-0 manager-session-slug
sessions workspace bind elevate pane-1 worker-session-slug
```

When several saved tabs exist, save shows a numbered picker. For scripts, use
`--window-index 0 --tab-index 5` to select explicitly. Indices are zero-based and
can change as Terminal tabs change. `--state-file PATH` on **save** selects a
Terminal saved-state input file, not ai-sessions' own state file.

Save prints each pane's slot, split information, and working directory. Slots
are numbered in pane-creation order, starting at `pane-0`; this may differ from
screen reading order. Bind IDs, harness-qualified IDs, or unique session slugs.
Titles alone are not unique references. Assign a native session only once per
workspace; `bind ... --move` explicitly moves an existing assignment.

Existing layouts on this PC do not record the agent session in each pane, so
the initial assignments must be explicit. Directory and title are not reliable
enough to infer them. An existing workspace is protected from accidental
overwrite; `save NAME --force` replaces its geometry and clears its bindings.

## Restore after a restart

```powershell
sessions workspace restore elevate --dry-run
sessions workspace restore elevate
```

Restore opens a new tab in the current or most recently active Terminal window.
Use `--window -1` for a separate new window. It starts PowerShell in each pane,
then resumes that slot's last successfully launched native session at its latest
saved turn. It preserves the chosen session's role, including supervisors,
rather than selecting a newer fork belonging to a different role.

Already-running sessions are skipped, leaving that pane at a shell prompt.
Concurrent managed restores cannot launch the same slot or native session twice.
Bindings include the harness home; a different active home is refused rather
than silently switching accounts. No authentication tokens are copied.

Every later `sessions` launch from one of these managed shells updates that
slot's saved assignment after the native process starts successfully. Closing
the agent leaves the shell usable. Bindings survive a restart and have no
seven-day expiry. This restores saved conversations, not in-flight tool calls
or unsaved terminal scrollback.

## Reuse the arrangement for another project

```powershell
sessions workspace clone elevate another-project
sessions workspace bind another-project pane-0 another-manager-slug
sessions workspace bind another-project pane-1 another-worker-slug
sessions workspace restore another-project
sessions workspace list
```

The original workspace keeps its assignments; the clone starts with every slot
empty. Unbound panes block normal restore so a forgotten binding is visible.
Use `restore NAME --allow-unbound` to open those slots as shells. Inside one,
launch a session with `sessions` to save its assignment automatically, or bind
it explicitly from any shell. Cloning retains the old layout's starting
directories; a bound agent launches in its saved session's working directory.

## Storage and troubleshooting

Workspace metadata lives beside ai-sessions' state as `state.workspaces.sqlite3`.
`AI_SESSIONS_WORKSPACE_FILE` overrides it; `AI_SESSIONS_STATE_FILE` selects the
ordinary app state. Terminal startup commands pass these paths explicitly so
subsequent pane launches use the same files. The managed PowerShell shell has
process-scoped `AI_SESSIONS_WORKSPACE` and `AI_SESSIONS_WORKSPACE_PANE` values;
no permanent Windows environment variables are changed.

If a slot is wrong, run `workspace show NAME`, then bind the correct native ID or
slug. If an old session is unavailable, bind an existing session. If save reports
an unsupported Terminal action, it refuses to guess the geometry. Only split
layouts with supported focus actions are imported; original shell command lines
are discarded and never replayed.

Windows Terminal 1.24.12741.0 and PowerShell 7 were used for the November 12 native
test: two managed panes, split and focus actions, correct slot environment,
shell survival after startup exit, and punctuation-bearing paths. Automated
tests cover cloning, explicit binding, exact-native resume, home mismatch,
supervisor metadata, live-session refusal, and durable launch tracking.

For messaging between the restored sessions, see [SESSION_MESSAGING.md](SESSION_MESSAGING.md).
