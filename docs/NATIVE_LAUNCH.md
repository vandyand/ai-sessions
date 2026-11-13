# New native sessions and alias restart

Updated November 13, 2026. Introduced in ai-sessions 3.15.0.

## Start a session

```powershell
sessions new codex
sessions new claude
sessions new codex -- --model MODEL "Initial task"
```

This starts the genuine native CLI in the current terminal. Standard input,
output, colors and keyboard handling go directly to that CLI. Native arguments
follow `--`; the launcher does not implement a terminal renderer or use a new PTY.
Permission behavior comes from the native CLI's configuration and the flags you
supply. `sessions new` does not add permission bypasses by default.

Inside the agent, run `! sesres` (or ask it to run `sesres`) to restart the current
conversation in the same terminal. The waiting launcher records the exact native
identity and client process generation. A normal exit returns to PowerShell.
Restart retains reusable options, omits the original initial prompt and images,
and resumes the exact conversation instead of making another session. One-time
fork/worktree creation and resume-picker options are not replayed.

## PowerShell aliases on Andrew's PC

`cdba` routes through `sessions new codex` with its existing
`--dangerously-bypass-approvals-and-sandbox` flag. `cdsp` routes through
`sessions new claude` with its existing `--dangerously-skip-permissions` flag.
Other native arguments are forwarded. This changes lifecycle support, not those
aliases' permission settings. Raw `codex` and `claude` commands are also available.

Reload existing PowerShell shells with `sb`, or open a new shell. A native session
already running from the previous direct aliases needs one ordinary exit and
relaunch to obtain a waiting launcher. Updating a child process cannot change
its parent shell's loaded function definitions.

The idempotent updater modifies only these two existing function bodies:

```powershell
pwsh -NoProfile -File tools/Set-ManagedSessionAliases.ps1 -ProfilePath "$HOME/.config/powershell/profile.ps1"
```

It preserves the other profile contents and refuses unrecognized bodies without
saving partial changes. It does not rewrite CLI authentication or hook settings.
No extra `scdba`/`scdsp` aliases are needed.

## Identity and limits

For fresh Codex sessions, the launcher uses the existing provider-owned empty
conversation creation path before launching `codex resume` with that exact ID.
This is necessary because native 0.167.1's shared-server UI logs can omit thread
IDs. Exact resume IDs are known before launch too; native resume picker requests
are resolved by the shared selector before the client starts. Claude exposes its
process/session registry, which the launcher verifies against its owned PID and
creation time. No source conversation is copied or transpiled for a fresh session.

The `sesres` tool context's native ID must match the recorded conversation before
the shared-server fallback is accepted. Unverifiable in-session Codex UI switches
are refused rather than restarting an unrelated client. Simultaneous managed
attachments to one conversation remain recorded as ambiguous; close the extra
attachment before restarting. PID reuse is rejected before stopping anything.
Shared native daemons are spared.

Nonlocal `--remote` endpoints need a remote-aware creation/resume API and are
not assigned locally created IDs. The default local `unix://` endpoint is supported.
Help/version and other native subcommands are forwarded. Noninteractive commands
retain native behavior but are not the interactive restart workflow.

Alias-launched sessions are recorded against `WT_SESSION`, helping workspace
capture identify them. A newly created conversation missing from the last browser
catalog is reread directly by native ID during live workspace save. See
[WORKSPACES.md](WORKSPACES.md) and [SESSION_MESSAGING.md](SESSION_MESSAGING.md).

## Verification

Disposable native Windows Terminal tests on November 13 used Codex 0.167.1 and
Claude Code 2.1.325. Each agent executed a one-time restart request from inside its
own native tool context. Both request calls returned zero; the client PID changed
and the native conversation ID stayed identical. Only the exact owned test clients
were stopped afterward; the shared native server was left running.

Tests also cover argument forwarding, literal prompt delimiters, retained permission
flags, initial-prompt omission, cwd selection, duplicate attachment ambiguity,
process-generation checks, exceptional owned-child cleanup and profile idempotence.
