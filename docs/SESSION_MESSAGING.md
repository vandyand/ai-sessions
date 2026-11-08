# Messaging running sessions

`ai-sessions` 3.11 adds same-machine messages between running Codex and Claude
Code sessions. The initial supported setup is native Windows with Codex 0.166.0+
and Claude Code 2.1.319+. Claude requires the Windows DevSecrets vault; Codex uses
its own shared daemon. Desktop-private servers, WSL/native cross-boundary routing,
and offline automatic wakeup are not supported.

## Setup and restart

```powershell
sessions messaging install
```

Setup starts Codex's native daemon if needed and installs separate SessionStart
and SessionEnd hooks. Existing supervisor and third-party hooks are retained.
No global inbound permission setting is changed. Setup is idempotent.

Reopen each session you want to receive messages. For a tab originally opened
through `sessions`, use the shorthand:

```text
! sesres
```

`sesres` is an executable alias for `sessions --restart`; it forwards arguments
and exit codes. Like the original command, restarting requires a Windows session
that ai-sessions launched. Otherwise close and reopen the session normally.
Codex may ask you to trust the newly installed hooks on the next launch.

Since 3.11.1, same-harness restart/reopen pins the exact native branch the tab ran,
including forked/transpiled sessions. It does not select between divergent heads.
Normal resumes and explicitly switching harnesses still enforce divergence checks.
An older, already-running ai-sessions launcher keeps its imported code until it exits.
After upgrading, use a fresh `sessions --reopen` from PowerShell to reload that launcher;
restarting only its child harness cannot update the old launcher code.

New Codex launches through ai-sessions explicitly attach to the native local
daemon with `--remote unix://`. The terminal and messaging commands therefore
reach the same owner. Setup never restarts an existing daemon or moves an
already-running transcript onto a competing server. Explicit custom remote
launch arguments are preserved; private/custom remote endpoints are not discovered
by the local-daemon adapter. `--no-daemon` conflicts with managed messaging.

Codex supervisors keep the target project as their working directory. With managed
messaging, ai-sessions resumes the supervisor on the local daemon and grants its
own folder through the per-thread `thread/settings/update` API, then verifies the
result before opening the terminal. This avoids the native CLI's incompatible
`--remote` plus `--add-dir` combination. Existing writable roots, network settings,
approval policy and sandbox mode are retained unless explicitly selected in the
launch profile. Built-in dangerous mode and custom `--sandbox`/approval flags apply
only to this supervisor. Its preparation attachment stays open until the terminal
exits so a fresh thread cannot unload and lose its settings before the TUI joins.
No global roots are changed. Named configuration profiles and permission `-c`
overrides are not resolved by this preparation API and produce an explicit error.
Read-only profiles, busy supervisors needing settings changes, custom remote endpoints,
and unconfirmed folder access stop the launch with an explicit error. Dry runs
print the terminal command without loading a thread or changing permissions.
Without remote attachment, supervisors continue using `--add-dir`.

The active harness home determines authentication and the daemon. To install
for another Codex home, first set `CODEX_HOME` for that PowerShell process and
launch subsequent sessions with the same home. `--codex-home` must match that
active home; it does not switch authentication behind your back. The usage-only
`.codex-plan` login is unaffected unless you explicitly select it.

## Discover, send, and reply

```powershell
sessions messaging status
sessions messaging status --json
sessions send codex:<session-id> --message "Migration finished. You can continue."
sessions send claude:<session-id> --message-file C:\path\message.txt --json
```

Status lists only live registered Claude receivers and threads loaded on known
native Codex daemons. If names/IDs collide, use the qualified `harness:id@homehash`
address printed by status. Saved sessions are not resumed just to deliver a message.

Agents should include their own explicit address, supplied by the startup hook:

```powershell
sessions send claude:<target-id> --from codex:<sender-id> --message "Please review the migration."
sessions reply <incoming-message-id> --from claude:<my-id> --message "Reviewed; tests pass."
sessions messaging receipt <message-id> --json
```

Replies retain the original receiver home and runtime/registration generation.
If that concrete receiver has gone away, reply refuses rather than selecting
another harness copy or a replacement runtime. Sending from a plain human terminal
has no agent reply address; the receiving agent can respond in its own conversation.

Messages are text, limited to 65,536 UTF-8 bytes. `--message-file` avoids shell
quoting trouble. Text is not evaluated as a shell command by the transport.
Peer envelopes identify the sender and reply ID, but names are not cryptographic
agent identities. Messages never count as user consent or approval.

## Timing and delivery states

- Active Codex turn: `turn/steer` adds input without canceling a running tool.
- Idle loaded Codex thread: `turn/start` starts work. If it becomes busy during
  the state check, Codex joins the existing turn.
- Pending approval: steering can be accepted, but it does not answer the approval.
  The agent handles the message when it can continue.
- Idle `turn/steer`: rejected by Codex; explicit state-race rejections trigger a
  bounded state reread (three attempts). Ambiguous transport failures are not retried.
- Claude: native inbox handling retains explicit `accept`, `hold`, and `refuse`.
  The deliberately installed integration authenticates with the exported child token;
  its trust is that of a receiver-authorized helper, not an independent Claude peer.
  To require manual inbound control, set Claude's explicit `hold` policy.

`accepted` means the Codex runtime accepted input, not that the agent processed
or completed it. `submitted` means Claude's pipe write completed; that transport
does not expose acceptance/hold/refusal receipts. `acknowledged` requires a
correlated reply from the addressed receiver. Acknowledgment cannot be overwritten
by a slower original send result. `uncertain` means delivery may have happened:
check the receiving session before deciding whether to send again.

CLI exit codes: 0 for accepted/submitted/acknowledged; 2 for unavailable/rejected
or invalid input; 3 for uncertain submission. Agent failures after acceptance
remain visible in the receiving session, not as send-command failures.

## Storage and removal

Nonsecret receiver metadata and receipts live in the `messaging` directory
alongside ai-sessions configuration, independently of `state.json` and the catalog.
`AI_SESSIONS_MESSAGING_DIR` overrides that directory. Receipts keep IDs, addresses,
status, and timestamps, not message bodies; they are pruned to 1,000 entries and
seven days on writes. Expired receipt IDs cannot be used to reply.

Claude credentials are stored only through `Set-Secret` in DevSecrets. The helper
reads them internally and authenticates to the named pipe without outputting a
secret. No `auth.json` parsing, plaintext credential file, or permanent environment
variable is needed. SessionEnd and later starts clean up owned registrations;
failed vault deletions retain nonsecret retry records.

```powershell
sessions messaging uninstall
```

Uninstall removes only messaging hooks, disables local registration, and attempts
owned credential cleanup. It leaves supervisor hooks, transcripts, and the shared
Codex daemon alone. Restart existing sessions to unload their old hooks. A locked
vault can leave cleanup pending until a later install/start can retry it.
