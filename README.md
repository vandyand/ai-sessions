# ai-sessions

The terminal interaction contract is documented in [DESIGN.md](DESIGN.md).

`ai-sessions` is a searchable terminal browser for local
[Codex CLI](https://developers.openai.com/codex/cli/), Claude Code, and
[OpenCode](https://opencode.ai/) conversations. It indexes each harness's existing native history.
Browsing is read-only; explicit rename and bridge actions use the provider-specific operations
described below.

It runs as `sessions` on Linux and native Windows PowerShell.

## Features

- One conversation-centered list for Codex, Claude Code, and OpenCode sessions
- Expandable project/thread groups and collapsed cross-harness lineage copies
- Search and filters for provider, directory, origin, open state, and visibility
- Human, cross-provider, and subagent/automation origin labels
- Started/updated timestamps plus compact `turns / compactions / prompts` activity
- Rename that carries through to all three harnesses, plus utility-local hiding
- Nickname labels that tell sibling subagent threads apart without exposing native IDs
- Detection of currently open sessions on Linux and Windows
- tmux pane and desktop-terminal focus on Linux when the environment exposes it
- Cross-harness resume: continue any session in any of the three harnesses
- Safe, dangerous, and custom launch profiles
- Native paths and argument handling on both operating systems

Windows Terminal does not expose a stable session-ID-to-tab interface. On Windows, open sessions are identified, but exact tab focusing is intentionally not attempted.

## Requirements

- Python 3.11 or newer
- Codex CLI, Claude Code, OpenCode, or any combination
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
Tracked cross-harness copies collapse to their logical conversation. Same-title independent
threads in one project collapse to a presentation-only container; press Enter, Space, or Right to
expand it and then choose the exact thread. No title or directory grouping creates lineage.

| Key | Action |
| --- | --- |
| `Ctrl-F` or `/` | Start search mode |
| `Tab` | Cycle single-tool filter presets |
| `t` | Choose any combination of tools to show |
| `o` | Cycle Human, Cross, Agent, and All origins |
| `v` | Cycle visible, hidden, and all sessions |
| `d` | Choose a directory |
| `Space` / `Right` | Expand the selected conversation or thread group |
| `Left` | Collapse the selected conversation or thread group |
| `i` | Show full session, lineage, related-thread, storage, and native-ID details |
| `f` | Focus the selected project; press again to show all projects |
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
sessions --resume SESSION_ID --launch-tool opencode
sessions --resume SESSION_ID --dry-run
```

## What is written, and when

Resuming a session in the harness that recorded it is a pure read: `sessions` runs
`codex resume ID`, `claude --resume ID`, or `opencode --session ID` against the original ID and
touches nothing.
Sessions at rest are never rewritten, and no transcript is ever edited in place.

Only two actions write to provider storage:

- **Rename** (`r`) appends a `custom-title` record for Claude or a `thread_name` record for Codex.
  OpenCode has no rename command, so its adapter performs one bounded, transactional update of the
  exact existing session title and verifies the row afterward; semantic checkpoints exclude this
  metadata-only change.
- **Bridging** creates a *new* native target session. Claude and Codex receive new session files;
  OpenCode receives export JSON through its official `import` command. The source remains read-only.

Everything else — hiding, sort order, per-session harness preference — stays in this
utility's own `state.json`.

## Cross-harness resume

The three harnesses store sessions differently and do not recognise one another's native IDs, so a
conversation cannot simply be handed across by reference. Press
`x` (or pass `--launch-tool`) and `ai-sessions` bridges it instead: it reads the source
transcript, converts the conversation into the target harness's own on-disk format, and
writes it there as a new native session. That copy is an ordinary session — the target CLI
resumes it, appends to it, and lists it like any other.

```bash
sessions --resume CODEX_SESSION_ID --launch-tool claude
sessions --resume CLAUDE_SESSION_ID --launch-tool codex
sessions --resume OPENCODE_SESSION_ID --launch-tool claude
sessions --resume CLAUDE_SESSION_ID --launch-tool opencode
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
never modified, and the copy is named `<title> (from Codex)`, `<title> (from Claude)`, or
`<title> (from OpenCode)` so
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

The harnesses record this differently, and all three are usable. Claude Code writes a
plain-text summary. Codex seals its summary as `encrypted_content` — unreadable to anyone
but the provider, including Codex itself — but records the messages it carries forward
beside it, so the copy resumes from those. What crosses is the conversation the source
kept, not a paraphrase of it; what cannot cross is the sealed summary of the assistant's
own work in that window. Only a compaction with no carried context falls back to replaying
the pre-compaction history. The handoff note says which happened, so an over-large copy is
always explainable.

OpenCode stores a readable summary, compaction request, and retained-tail boundary in its shared
SQLite database. The adapter follows the newest *completed* summary. A failed-only attempt retains
full history, and a successful retry keeps the retained tail after its completed summary; this is a
deliberate safety correction to OpenCode 1.18.21's failed-attempt reorder behavior.

### Budget

Whatever survives the above is fitted to a target-owned token policy. Claude Code and Codex use
the 1M-context models configured for this deployment, reserving 5% for target scaffolding: about
950,000 tokens / 1,900,000 projected characters. OpenCode target preparation queries installed
models and uses the selected model's effective input limit; when verbose metadata is unavailable it
warns and falls back to a 128,000-token compatibility floor. Selection happens at source-message
boundaries before same-role messages are assembled for the target. The first message of the
selected context and one contiguous newest suffix survive; oversized anchors carry an
explicit truncation marker, and the note reports dropped, truncated, source, and assembled
counts. These target defaults intentionally replace the previous global 950,000-character
ceiling; they are smaller so an unknown target model is less likely to compact immediately
on resume.

The bridge refuses before calling the target writer if the selected context would drop messages or
truncate an anchor. `max_tokens` and `max_chars` choose capacity; they do not authorize omission.
The error reports projected source cost, the current budget, loss counts, and the minimum setting
needed to fit. Set `allow_lossy = true` under `[bridge]` only to deliberately restore the old
first-message-plus-newest-suffix behavior; this is dangerous and exact losses remain reported.
Starting at the source's latest native compaction summary under `latest_window` is not budget loss,
because that summary replaces earlier windows in the source context.

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
or creates a new native session from the head. Historical materializations remain available under
expansion and in details as `superseded copy`. The validated current materialization is a `lineage
head`. If two materializations advance independently, both are `diverged branch` and automatic
resume stops instead of silently choosing one branch. Same-title sessions without recorded lineage
are `independent thread` or `untracked`; their visual grouping never changes launch routing.

The conversation id is stored in `state.json` and included in new copies as a structured
`[ai-sessions-provenance v1]` marker. State is authoritative today; the marker makes copies
auditable and provides a future recovery path, but state reconstruction from transcripts is
not implemented yet. Version 5 bridge records migrate conservatively: a possibly edited
copy wins over its ancestor so old state cannot silently discard work.

### Adding a harness

Conversions run through a harness-neutral conversation rather than pairwise. OpenCode is the first
production proof: all six ordered cross-harness directions and all three same-harness identities use
one core, with no pairwise converter. Support for another CLI costs one adapter rather than a
converter per existing harness. The registry requires a name and label, a conservative budget
policy, plus operations to read a `NativeRef` into `Turn` objects with an opaque checkpoint, write
turns as a resumable native session, resolve a native ID, test exact availability, and compare
meaningful native state against that checkpoint. The same adapter also owns native
discovery, resume arguments, title publication, liveness evidence, native-id patterns, labels,
ordering, home-directory semantics, and its launch/budget policy. Bridging and head tracking in
both directions then work without pairwise logic.

The registry is dynamic: CLI choices, keyed schema-3 provider profiles, list/browser rendering,
discovery, liveness, naming, and conversion all query it at runtime. Unsupported capabilities
and unknown harnesses fail explicitly instead of borrowing built-in behavior. An independent
fourth-format fixture exercises the whole contract without reusing a built-in adapter. The
complete target contract is specified in
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
Claude Code and OpenCode are listed straight away.

## Launch safety

The package defaults to `safe`. This leaves approval and sandbox behavior to each provider's normal configuration:

```text
claude --resume SESSION_ID
codex resume SESSION_ID
opencode --session SESSION_ID
```

Dangerous mode adds the providers' explicit bypass flags:

```text
claude --dangerously-skip-permissions --resume SESSION_ID
codex --dangerously-bypass-approvals-and-sandbox resume SESSION_ID
opencode --auto --session SESSION_ID
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
version = 3

[launch]
mode = "custom"
claude_command = ["claude"]
codex_command = ["codex"]

[launch.providers.opencode]
command = ["opencode"]
bridge_model = "provider/model" # optional; must appear in `opencode models`

[launch.custom]
claude_args = ["--permission-mode", "acceptEdits"]
codex_args = ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]

[terminal]
# Opens a terminal on a tmux session whose own terminal died.
# {session}, {window}, {pane} and {script} are substituted.
# Empty means auto-detect: $TERMINAL, then kitty, wezterm, ghostty,
# alacritty, gnome-terminal, konsole, xterm.
command = []

[bridge]
max_tokens = 150000
allow_lossy = false
tool_calls = true
latest_window = true
```

By default, a bridge refuses before writing if its selected context would drop or truncate content;
increase `max_tokens`/`max_chars` to fit it. Set `allow_lossy = true` only as an explicit,
dangerous authorization for those losses; exact dropped and truncated counts are still shown.

Rename/hide state is kept alongside the configuration as `state.json`. Per-session
launch-harness preferences, conversation ids, native members, equivalence frontiers, and
opaque native checkpoints are stored there too. Unset sessions default to the harness where they were
started. Caches use `~/.cache/ai-sessions` on
Linux and `%LOCALAPPDATA%\ai-sessions` on Windows. Environment overrides are available
through `AI_SESSIONS_CONFIG_FILE`, `AI_SESSIONS_STATE_FILE`, `CODEX_HOME`,
`CLAUDE_CONFIG_DIR`, and `OPENCODE_DB`. Prefer the configured OpenCode command's authoritative
`db path`; `OPENCODE_DB` is the explicit fallback override when that command cannot report one.

## Recovering a session whose terminal died

A tmux session outlives its terminal. When a window manager restart or a crash takes the
terminal window but leaves the tmux server, the pane shells, and the harness process running,
the session is still there — `sessions` just had no way back to it.

Pressing Enter on such a session now opens a terminal attached to it, on the exact window and
pane. This happens only when tmux reports **no client** on the session. A session that already
has a client but whose window cannot be raised — different display, Wayland, remote — is not
given a second one, because two clients share a session and tmux sizes both to the smaller
window; the message explains that instead.

The terminal is resolved from `[terminal] command`, then `$TERMINAL`, then the first of kitty,
wezterm, ghostty, alacritty, gnome-terminal, konsole, xterm that is installed. With no display
at all, nothing is spawned and the message carries the exact `tmux attach-session` command.

## How open-session detection works

- Claude Code publishes a live PID/session registry.
- Codex on Linux holds per-thread writer locks.
- Codex on Windows records thread IDs alongside process IDs in its local log database.
- OpenCode is marked open only when a process command line names its exact `--session`/`-s` ID;
  a bare OpenCode process is never guessed to own a particular session.
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
