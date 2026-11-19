# Permanent session deletion

Use an ordinary shell so you can review the plan and type its confirmation:

```powershell
ses delete mine-ses-2 --dry-run
ses delete mine-ses-2
```

Handles, native IDs, and slugs work. Qualify a native copy with its harness when
needed, for example `claude:ID`. The default deletes that resolved native copy.
`--all-copies` selects the explicitly linked native copies of its conversation;
it does not select unrelated forks or everything with a matching title.

The plan lists native IDs, titles, known history files, and either scoped metadata
changes or an explicit provider-owned deletion operation. Actual deletion requires typing the exact displayed handle (or qualified
native ID when no handle exists). There is no `--yes` or force option. A mismatch
cancels the operation. `--dry-run` does not require interactive input.

This removes native conversation history, rather than merely hiding a menu row.
It does not erase external backups, version-control history, exported documents,
or records held by remote services. Retired handle/slug reservations remain so
an old command cannot silently target a different session later.

## Safety and supported storage

Provider adapters author exact resource plans. Deletion requires authoritative
native identity and current liveness checks. Running sessions and uncertain
liveness are refused; no process is automatically stopped. Storage is checked
again after confirmation. Changed files, linked paths, unreviewed database
schemas, or unselected dependent children cause refusal.

Claude top-level JSONL and compressed JSONL histories are supported when there
are no untracked child resources. OpenCode session/message/part/event scopes are
checked against reviewed schemas. Unknown manual database formats and Claude
parents with child resources are refused.

Modern Codex homes, including paginated histories, use Codex's own `thread/delete`
operation after confirmation. A short-lived owned stdio app-server performs only
initialization and deletion: no model turn, session resume, or messaging. Codex
checks writer ownership and fork references, then coordinates its evolving
rollout, projection, diagnostic, and other durable thread data. The plan's known
rollouts are identity guards, not files manually removed by this app. See the
[native implementation](https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/local/delete_thread.rs).
Older homes without modern ancillary stores retain the reviewed manual path.

For OpenCode, a fresh deletion command needs an explicit `OPENCODE_DB` path, or
an authoritative native binding already established in that process. The command
does not launch OpenCode to guess its active database. The catalog storage must
match that explicit binding.

Manual deletion liveness checks are conservative. Another native client or daemon whose current
session cannot be established can block deletion even when the selected history
appears closed. A remembered managed launch or last-used-session log is not
sufficient evidence of its current ownership. Close native clients of that harness
normally and retry; there is no liveness bypass.
A dry run still shows the plan and reports that actual deletion would be refused.
Codex's provider-owned operation acquires native writer locks, so other Codex
clients may remain open. A selected running session is refused; native ownership
and dependent-reference checks still happen at the actual native commit.

Legacy manual Codex diagnostic removal is limited to rows with an exact matching `thread_id`.
Shared threadless process logs are preserved; ambiguous threadless diagnostic
references cause refusal. This command is not a guarantee of forensic erasure.

Exact files are staged beside their originals while scoped native database
changes are committed through one attached-database transaction. Normal failures
roll back together; multiple WAL databases do not provide crash atomicity across
files. A failure before commit attempts rollback; a purge or
rollback failure reports `PARTIAL` and recovery paths. This is not a filesystem
crash-recovery system. Do not delete recovery files without understanding the
reported failure.

Native deletion cannot be rolled back by this app. Mixed `--all-copies` requests
commit compatible manual copies together, then native operations sequentially,
then local menu/state cleanup. A later failure reports completed and unconfirmed
native IDs and stops. Even an explicit native error can follow partial cleanup;
there is no automatic retry. Refresh and inspect the reported IDs first.

Deletion uses the saved catalog. If it is missing or a session cannot be resolved,
open the browser and let its refresh finish first. After deleting from another
shell, refresh an already-open browser to discard its in-memory rows.

For an ordinary cleanup that should preserve conversation history, use `--hide`
instead.
