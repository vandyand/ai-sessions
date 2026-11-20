# Project and session handles

`ses` and `sessions` run the same application. A project handle is a short unique
project name such as `elev`. A session handle identifies a logical conversation,
such as `elev-sup-1`, `elev-mgr-2`, or `elev-wrk-11`.

```powershell
ses project list
ses list elev
ses open elev-mgr
ses desk elev-sup
ses desk elev-sup --codex
ses desk elev-sup --claude
```

An omitted number means **1**: `elev-mgr` and `elev-mgr-1` identify the same
session. `ses open` resumes that operational session. `ses desk` creates or reuses
its quiet human-facing Project Desk conversation, using the target's harness by
default. The harness override changes the human-facing conversation. The command
prints how to open the adjacent live briefing view.

## Identify the current session

Inside Claude Code or Codex, run `! ses whoami` or `! ses handle`. Both print
this session's handle, title, project handle, and harness. Use `--short` to print
only the handle, or `--json` for structured output.

Identity comes from the native thread or a verified running harness ancestor;
it is never inferred from the working directory. If the session has not been
indexed, open the sessions menu and let it refresh, then retry. An ordinary
separate shell has no current agent identity; use `ses -q "session title"` there.

## Defaults and editing

Project handles default to the first four alphanumeric characters of the Git
repository name, or working-directory name outside Git. Conflicts get a numeric
suffix. Git worktrees share their repository's project. Session roles are inferred
from supervisor metadata or explicitly named supervisor/manager/worker/helper
titles; other sessions use `ses` (an ordinary session with no team role assigned). These labels confer no permissions. Explicit
worker numbers such as 11 and 12 are preserved when available; collisions advance
to the next available number.

```powershell
ses project set ste vtran
ses project add lego --directory C:\projects\lego
ses handle assign NATIVE_ID --project elev --role wrk --number 11
ses handle set NATIVE_ID my-review
```

Choose project handles with 1â€“5 ASCII letters or digits. Session handles allow
ASCII letters and digits separated by single hyphens. They are case-insensitive.
Session handles cannot reuse a native ID or slug, even for the same copy, because a bridge would make those two addresses mean different things. Existing assignments remain stable when a title or directory changes. Use
`project set`, `handle assign`, or `handle set` to change them explicitly.
Old names remain reserved and report the current handle; they are never silently
given to another conversation.

## Identity and compatibility

Native IDs and existing per-copy slugs continue to work. Handles also work in
commands that accept session references, including messaging, workspace assignment,
supervision, and session editing. The menu and `--list` include a Handle column;
search includes project and session handles. `ses list PROJECT --json` includes
current harness, native ID, open status, update timestamp, compression, and copy count.

Only explicit bridge/conversation records link native copies. A fork gets its own
handle even when its title matches. An unqualified logical handle selects the most
recent nonsuperseded copy, with the explicit bridge target breaking a timestamp tie.
Use a native ID or `tool:HANDLE` when you deliberately need a particular harness.
Messaging refuses if the selected copy is unavailable; it does not send to an older
reachable copy. Run `ses messaging status --json` to inspect reachable addresses.

Assignments live alongside `state.json` in `state.slugs.sqlite3`. SQLite transactions
serialize concurrent allocation and reserve session handles against slug conflicts.
Older processes can continue reading their existing state/catalog formats; restart
them to use handles and the new commands.

## Menu inspection and project defaults

Press `m` on a highlighted row to read its native messages without opening an agent.
`u` and `a` jump to the latest user/assistant messages; `p` and `n` select older/newer
messages. Arrows, page keys and Home/End scroll the full selected message. `q` or
Esc returns. The view retains at most 200 complete messages, keeping the latest
user/assistant even when older than that tail. Native adapters can still read a
whole transcript internally. Compressed input uses bounded temporary extraction
(up to 128 MiB), leaves original storage compressed, and cleans up on exit.

`?` shows Key / Action / Description with scrolling and wrapped descriptions.
Confirmation keys appear before long names so narrow panes still show what to press.
Expanded thread/copy rows have indented titles; handles and other columns stay aligned.
The default footer shows handles; legacy slugs remain in Details and command support.

Supervisors inherit the project assigned to their recorded target conversation,
falling back to the target's saved working directory when it is missing. Their
internal ledger directory is not a project name. Generated legacy supervisor and
`-session-N` defaults are corrected; custom/manual choices are preserved. Generated
generic handles use `-ses-N`; this does not assign a worker role.

Working directory is only a fallback project hint. Generic names such as `output`,
`saves`, and user home are often poor project names. Use `ses project set OLD NEW`
and `ses handle assign SESSION --project PROJECT` for explicit persistent choices.
Model-assisted classification can suggest among known projects, but is not enabled
in this release; opening the menu makes no classification/model requests.
