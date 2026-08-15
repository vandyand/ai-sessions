# ai-sessions

`ai-sessions` is a searchable terminal browser for local [Codex CLI](https://developers.openai.com/codex/cli/) and Claude Code conversations. It indexes each provider's existing on-disk history. Browsing is read-only; an explicit rename appends the provider's supported title record so the name also appears in that provider.

It runs as `sessions` on Linux and native Windows PowerShell.

## Features

- One navigable list for Codex and Claude sessions
- Search and filters for provider, directory, origin, open state, and visibility
- Human, cross-provider, and subagent/automation origin labels
- Started and updated timestamps plus user-message counts across compactions
- Rename that carries through to Claude Code and Codex, plus utility-local hiding
- Nickname and parent labels that tell sibling subagent threads apart
- Detection of currently open sessions on Linux and Windows
- tmux pane and desktop-terminal focus on Linux when the environment exposes it
- Safe, dangerous, and custom launch profiles
- Native paths and argument handling on both operating systems

Windows Terminal does not expose a stable session-ID-to-tab interface. On Windows, open sessions are identified, but exact tab focusing is intentionally not attempted.

## Requirements

- Python 3.11 or newer
- Codex CLI, Claude Code, or both
- Linux or native Windows PowerShell

The Windows-only `windows-curses` dependency is installed automatically. `psutil` is used for portable process inspection.

## Install

From a checkout:

```bash
python -m pip install .
sessions
```

With `pipx`:

```bash
pipx install .
sessions
```

From PyPI:

```bash
pipx install ai-sessions
```

On Windows, `py -m pip` can be used in place of `python -m pip`.

## Everyday use

Run `sessions`, navigate with the arrow keys or `j`/`k`, and press Enter to resume the selected conversation.

| Key | Action |
| --- | --- |
| `Ctrl-F` or `/` | Start search mode |
| `Tab` | Cycle provider filter |
| `o` | Cycle Human, Cross, Agent, and All origins |
| `v` | Cycle visible, hidden, and all sessions |
| `d` | Choose a directory |
| `s` | Cycle sort order |
| `p` | Cycle Safe, Dangerous, and Custom launch modes |
| `r` | Rename in the utility and provider |
| `h` | Hide or restore locally |
| `Ctrl-R` | Refresh |
| `?` | Show complete help |

Useful noninteractive forms include:

```bash
sessions --list --tool codex
sessions --list --query "is:open dir:my-project"
sessions --list --visibility hidden
sessions --resume SESSION_ID
sessions --resume SESSION_ID --dry-run
```

## Launch safety

The package defaults to `safe`. This leaves approval and sandbox behavior to each provider's normal configuration:

```text
claude --resume SESSION_ID
codex resume SESSION_ID
```

Dangerous mode adds the providers' explicit bypass flags:

```text
claude --dangerously-skip-permissions --resume SESSION_ID
codex --dangerously-bypass-approvals-and-sandbox resume SESSION_ID
```

These options disable important protections. Use them only where you have consciously accepted that risk.

Set a persistent mode from the command line:

```bash
sessions --set-launch-mode safe
sessions --set-launch-mode dangerous
```

Use `--launch-mode` for a one-time override. The active mode is always displayed in the interface header.

## Configuration

Configuration is stored in:

- Linux: `~/.config/ai-sessions/config.toml`
- Windows: `%APPDATA%\ai-sessions\config.toml`

The optional custom profile uses structured argument arrays, avoiding shell interpolation:

```toml
[launch]
mode = "custom"
claude_command = ["claude"]
codex_command = ["codex"]

[launch.custom]
claude_args = ["--permission-mode", "acceptEdits"]
codex_args = ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
```

Rename/hide state is kept alongside the configuration as `state.json`. Caches use `~/.cache/ai-sessions` on Linux and `%LOCALAPPDATA%\ai-sessions` on Windows. Environment overrides are available through `AI_SESSIONS_CONFIG_FILE`, `AI_SESSIONS_STATE_FILE`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR`.

## How open-session detection works

- Claude Code publishes a live PID/session registry.
- Codex on Linux holds per-thread writer locks.
- Codex on Windows records thread IDs alongside process IDs in its local log database.
- Linux focus support follows the process into tmux and then uses `wmctrl`/`xdotool` when available.

Detection is best-effort and read-only. Renaming is the sole operation that writes to provider storage; hiding and all other utility state stay local to `ai-sessions`.

## Privacy

No transcripts, caches, credentials, local names, or hidden-session state belong in this repository. The defensive `.gitignore` excludes common provider and local data paths.

## Development

```bash
python -m unittest discover -s tests -v
python -m build
```

CI exercises Python 3.11–3.13 on Ubuntu and Windows.
