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

## Defaults and editing

Project handles default to the first four alphanumeric characters of the Git
repository name, or working-directory name outside Git. Conflicts get a numeric
suffix. Git worktrees share their repository's project. Session roles are inferred
from supervisor metadata or explicitly named supervisor/manager/worker/helper
titles; other sessions use `session`. These labels confer no permissions. Explicit
worker numbers such as 11 and 12 are preserved when available; collisions advance
to the next available number.

```powershell
ses project set ste vtran
ses project add lego --directory C:\projects\lego
ses handle assign NATIVE_ID --project elev --role wrk --number 11
ses handle set NATIVE_ID my-review
```

Choose project handles with 1–5 ASCII letters or digits. Session handles allow
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
