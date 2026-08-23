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
- Cross-harness resume: continue any session in Codex or Claude regardless of where it was created
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
| `x` | Cycle launch harness for selected session (bridges a copy when needed) |
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
sessions --resume SESSION_ID --launch-tool claude
sessions --resume SESSION_ID --launch-tool codex
sessions --resume SESSION_ID --dry-run
```

## What is written, and when

Resuming a session in the harness that recorded it is a pure read: `sessions` runs
`codex resume ID` or `claude --resume ID` against the original id and touches nothing.
Sessions at rest are never rewritten, and no transcript is ever edited in place.

Only two actions write to provider storage, and both are additive:

- **Rename** (`r`) appends a title entry — a `custom-title` line to a Claude transcript, or
  a `thread_name` line to `~/.codex/session_index.jsonl`.
- **Bridging** creates a *new* session file next to the existing ones and appends its title.
  The source transcript is opened read-only and left byte for byte unchanged.

Everything else — hiding, sort order, per-session harness preference — stays in this
utility's own `state.json`.

## Cross-harness resume

Codex and Claude Code store transcripts in different formats, and neither recognises the
other's session id, so a conversation cannot simply be handed across by reference. Press
`x` (or pass `--launch-tool`) and `ai-sessions` bridges it instead: it reads the source
transcript, converts the conversation into the target harness's own on-disk format, and
writes it there as a new native session. That copy is an ordinary session — the target CLI
resumes it, appends to it, and lists it like any other.

```bash
sessions --resume CODEX_SESSION_ID --launch-tool claude
sessions --resume CLAUDE_SESSION_ID --launch-tool codex
```

The user/assistant conversation crosses over as messages. Tool calls cross over
*summarised*, folded into the turn that made them:

```text
⟦Bash⟧ python -m unittest discover -s tests
   → Ran 75 tests in 0.066s
     OK
```

They are deliberately not replayed as live tool calls: a `tool_use` block would name tools
the target harness does not have, and would need a matching result to stay a valid
conversation. Summarising keeps what was run and what it returned — usually the part worth
having — without inventing structure the target cannot honour. Arguments and output are
clipped, Codex's fixed result preamble is stripped, and a Codex `exec` snippet is reduced
to the shell command it actually ran. Reasoning and attachments are dropped entirely.

Because a summary is a record and not a result, the copy opens with a note saying where it
came from and warning that the filesystem state is unverified. The source transcript is
never modified, and the copy is named `<title> (from Codex)` or `<title> (from Claude)` so
the two are never confused in the list.

### Sessions that have been compacted

A long session is not one conversation but a chain of context windows. When a harness runs
out of room it summarises everything so far and carries on from the summary, so the
transcript on disk holds every superseded window *and* a summary of each.

Replaying all of that is both wasteful and untrue to where the session actually stands, so
a bridged copy starts at the most recent summary — exactly where the source session itself
picks up. On a real 7-compaction session here that is the difference between 949,000
characters with 739 messages silently dropped to fit, and 147,000 characters with nothing
dropped at all. Set `latest_window = false` to replay the whole transcript instead.

The two harnesses record this differently, and both are usable. Claude Code writes a
plain-text summary. Codex seals its summary as `encrypted_content` — unreadable to anyone
but the provider, including Codex itself — but records the messages it carries forward
beside it, so the copy resumes from those. What crosses is the conversation the source
kept, not a paraphrase of it; what cannot cross is the sealed summary of the assistant's
own work in that window. Only a compaction with no carried context falls back to replaying
the pre-compaction history. The handoff note says which happened, so an over-large copy is
always explainable.

### Budget

Whatever survives the above is fitted to a target-owned token policy. The default is a
conservative unknown-model allowance: about 150,000 tokens / 300,000 projected characters
for Claude Code and 192,000 / 384,000 for Codex. Selection happens at source-message
boundaries before same-role messages are assembled for the target. The first message of the
selected context and one contiguous newest suffix survive; oversized anchors carry an
explicit truncation marker, and the note reports dropped, truncated, source, and assembled
counts. These target defaults intentionally replace the previous global 950,000-character
ceiling; they are smaller so an unknown target model is less likely to compact immediately
on resume.

Set `max_tokens` in `config.toml` for an explicit token-denominated ceiling, or
`tool_calls = false` for a conversation-only copy. The deprecated `max_chars` remains an
exact override. Version-1 files automatically written with `max_chars = 950000` migrate to
the target default; add `version = 2` or use `max_tokens` to make an override explicit.

Every first bridge creates a utility-owned conversation id. The original and each native
copy are materializations of that conversation. Equivalent copies share a frontier; byte
cursors on their append-only transcripts reveal which materialization received real work
after that frontier. Timestamps are display data only and never decide which history wins.

Selecting an older row therefore does not resume an older history. `ai-sessions` follows
the conversation head, reuses an equivalent copy in the requested harness when one exists,
or creates a new native session from the head. Historical rows remain visible and are
labelled `superseded`. If two materializations advance independently, both are labelled
`diverged` and automatic resume stops instead of silently choosing one branch.

The conversation id is stored in `state.json` and included in new copies as a structured
`[ai-sessions-provenance v1]` marker. State is authoritative today; the marker makes copies
auditable and provides a future recovery path, but state reconstruction from transcripts is
not implemented yet. Version 5 bridge records migrate conservatively: a possibly edited
copy wins over its ancestor so old state cannot silently discard work.

### Adding a harness

Conversions run through a harness-neutral conversation rather than pairwise, so support
for another CLI costs one adapter rather than a converter per existing harness. The current
bridge registry requires a name and label, a conservative budget policy, plus four operations: read a transcript into
`Turn` objects, write `Turn` objects as a resumable native session, locate a native id, and
detect meaningful transcript changes after a byte cursor. Bridging and head tracking in
both directions then work without pairwise logic.

This seam covers bridging and conversation advancement. Discovery, resume commands,
naming, message counts, and open-session detection are still provider-specific in
`app.py`, because each CLI records them differently — Codex in a SQLite state database and
lock files, Claude Code in a PID registry and per-project transcript directories. The
target adapter contract and the migration sequence are specified in
[`specs/conversation-log/HARNESS_CONTRACT.md`](specs/conversation-log/HARNESS_CONTRACT.md).

A Codex writer has one non-obvious obligation. Codex records the model's context
(`response_item`) separately from what its TUI redraws (`user_message` and `agent_message`
events), and groups both into turns delimited by `task_started`/`task_complete`. A rollout
carrying only the first kind resumes with the full conversation in context but a blank
screen, which looks exactly like a failed bridge. Writers for other harnesses should expect
a similar split and check the resumed session visually, not just by asking the model what
it remembers.

One more wrinkle: Codex enumerates its sessions from a local state database rather than
from the rollout files, so a copy bridged into Codex is resumable immediately but only
appears in the `sessions` list after Codex itself has opened it once. Copies bridged into
Claude Code are listed straight away.

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
version = 2

[launch]
mode = "custom"
claude_command = ["claude"]
codex_command = ["codex"]

[launch.custom]
claude_args = ["--permission-mode", "acceptEdits"]
codex_args = ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]

[bridge]
max_tokens = 150000
tool_calls = true
latest_window = true
```

Rename/hide state is kept alongside the configuration as `state.json`. Per-session
launch-harness preferences, conversation ids, native members, equivalence frontiers, and
byte cursors are stored there too. Unset sessions default to the harness where they were
started. Caches use `~/.cache/ai-sessions` on
Linux and `%LOCALAPPDATA%\ai-sessions` on Windows. Environment overrides are available
through `AI_SESSIONS_CONFIG_FILE`, `AI_SESSIONS_STATE_FILE`, `CODEX_HOME`, and
`CLAUDE_CONFIG_DIR`.

## How open-session detection works

- Claude Code publishes a live PID/session registry.
- Codex on Linux holds per-thread writer locks.
- Codex on Windows records thread IDs alongside process IDs in its local log database.
- Linux focus support follows the process into tmux and then uses `wmctrl`/`xdotool` when available.

Detection is best-effort and read-only. See [What is written, and when](#what-is-written-and-when) for the complete list of operations that touch provider storage.

## Privacy

No transcripts, caches, credentials, local names, or hidden-session state belong in this repository. The defensive `.gitignore` excludes common provider and local data paths.

## Development

```bash
python -m unittest discover -s tests -v
python -m build
```

CI exercises Python 3.11–3.13 on Ubuntu and Windows.
