# Native session messaging

Status: approved by Andrew; implemented for 3.11.0. See docs/SESSION_MESSAGING.md for the shipped contract.

## Intent and selected approach

Andrew wants messages between independent Codex and Claude sessions to arrive in
the open conversation and be handled during ongoing work without interrupting a
running tool. Replies must reach the sending session without manual copying.
Andrew selected Codex turn/steer while active and turn/start while idle after the
live integration tests. This extends the existing Python ai-sessions project.

Alternatives considered:

1. Native Claude inbox plus the owning Codex app-server: selected. Live delivery
   and round trips are verified. Requires secure endpoint registration and routing.
2. Native codex queue: simpler submission but waits for the current whole turn;
   does not satisfy the requested default timing. Retain only as an explicit mode
   if useful; never silently substitute it for steering.
3. Custom Claude MCP channel: documented extension mechanism with a reply tool,
   but currently needs channel startup/preview configuration. Keep as a fallback
   if the native pipe cannot support a trustworthy production integration.

## User-facing contract

Proposed commands (syntax and defaults are implementation recommendations):

    sessions send <target> --message "Migration finished"
    sessions send <target> --message-file <file>
    sessions send <target> --message "Migration finished" --json
    sessions reply <message-id> --message "Received, rebasing now"
    sessions messaging status
    sessions messaging install
    sessions messaging uninstall

The send path is independent of browser discovery/refresh. Resolve targets by
explicit harness/native ID or unambiguous registered alias. Refuse ambiguity,
including multiple harness copies of one conversation; do not select a copy just
because it was most recently active. A reply addresses the original concrete
session, not whichever copy is currently preferred by the browser.

Use a shared send/reply command with lightweight agent guidance in both harnesses.
Expose sender label, sender harness/native ID, message ID, and reply address to
the receiving agent. A message must be described as peer input, not user consent.
Codex text input does not provide a proven native peer badge; use a clearly
delimited peer envelope and instructions rather than claim equivalent native roles.
Do not advertise a sender label as cryptographic proof of agent identity.

No automatic reply-on-receipt: reply only when requested or useful. Refuse replies
to a session that no longer has a live registered receiver. Messages remain text;
do not execute slash commands or interpolate message bodies into shell commands.

## Codex delivery state machine

1. Connect to the owning existing app-server. Prefer the native local daemon's
   stdio proxy; support explicitly configured remote endpoints as appropriate.
   Perform the initialize handshake and verify the target is loaded there.
2. Read authoritative live thread/turn state, not transcript-tail liveness or
   the cached browser catalog. Establish the exact available schema during implementation.
3. Active turn: call turn/steer with expectedTurnId and a stable client message ID.
4. Idle loaded thread: call turn/start with the same message ID.
   The additional two-client probe verified that turn/start against a thread that
   has become busy appends input to its existing turn. It does not start a parallel
   turn or resolve a pending approval. Preserve this tested behavior with an integration
   check for supported Codex versions; do not blindly retry a successful turn/start.
5. A permission/input wait is still an active turn. Steering may be accepted while
   waiting; report accepted/waiting when observable. Never answer or cancel approval
   or request-user-input requests on the recipient's behalf.
6. On explicit no-active-turn or expected-turn mismatch rejection, re-read live
   state and retry with the correct operation. Bound retries (three attempts is
   the proposed default) and fail clearly if state keeps changing.
7. On a connection loss/timeout after submission, delivery is uncertain. Do not
   blindly resubmit or assume client message IDs provide server deduplication.
   Reconcile if the protocol permits; otherwise report uncertain for that message ID.

Never start a second app-server and resume a saved copy to reach a running session.
Do not silently restart, resume, or unpause stopped sessions. An unloaded thread,
closed terminal without a registered receiver, --no-daemon/private server without
an exposed endpoint, or inaccessible desktop server is unavailable with a concrete
remediation. A process suspended by the OS cannot service delivery until resumed;
do not claim steering wakes it. Pausing message delivery explicitly in ai-sessions
must not be overridden by selecting turn/start.

## Claude receiving and credential handling

Register the live inbox through a minimal SessionStart hook. Register session
ID, harness, display name, config-home identity, PID/start time, inbox path,
launch generation, capabilities, and nonsecret credential reference. Refresh
registration on resume. Remove only the matching generation on SessionEnd;
also prune by PID/start time and live endpoint evidence after a crash.

Use the exported session token only through a deliberately enabled integration.
It can grant own-child trust on Windows: do not pretend it is a neutral peer key,
or use it to silently weaken inbound defaults. Check and preserve inbound policy;
the integration must honor refusal and hold states, or report an unsupported
policy instead of bypassing it. Verify this with real policy tests before shipping.
Do not globally set crossSessionInbound=accept during installation.

On this Windows PC, any persisted secret goes only through Set-Secret in DevSecrets.
Keep only a vault reference in the registry. Resolve it inside the transport helper,
without printing it, passing it as a CLI argument, or returning it in JSON/logs.
Pipe authentication and message submission happen inside that helper. No auth.json
parsing, plaintext credential files, permanent environment variables, or WSL copies.
Remove only integration-owned vault entries for expired registration generations.
Other operating systems need an approved secure credential backend before enabling
secret-dependent Claude registration there; existing app behavior stays supported.

Use named pipes/local OS-user access boundaries for local transport. The unauthenticated
HTTP adapter from the throwaway test is not the production service. Prefer direct
native transports and short-lived helpers; do not introduce an always-running
ai-sessions broker unless implementation evidence demonstrates that it is needed.

## State, receipts, and failure behavior

Use a separate messaging store; do not enlarge state.json or require catalog scans.
Atomic per-session registration files avoid whole-store last-writer-wins updates.
Keep delivery records bounded by age/count and store message bodies only as needed
for pending replies and explicit diagnostics. The exact bounded retention defaults
are an implementation choice to document; do not log credentials or full bodies by default.

Distinguish submitted, accepted by runtime, acknowledged by agent, unavailable,
held, refused, failed, and uncertain. Native pipe write success alone proves
submission, not acceptance or processing. Only claim acknowledgment after a correlated
reply. If a transport cannot expose a state, return unknown rather than invent it.
Machine-readable output includes message ID, concrete sender/recipient, transport,
state, and a concise reason. Use distinct nonzero exits for unavailable/failed/uncertain
and document them. A successful send must not claim the requested work was completed.

## Scope and integration boundaries

- Add a messaging package and adapter hooks using the existing registry/capability
  architecture. Keep transport code, vault helpers, receipts, and CLI dispatch out
  of browser rendering. Avoid heavy messaging imports at ordinary startup.
- Extend hook installation without replacing supervisor or third-party hooks.
  Preserve scoped settings, back up only nonsecret configuration as appropriate,
  support idempotent installation/uninstallation, and explain one restart needed
  for already-open Claude sessions to register.
- Update agent guidance with send/reply examples; preserve unrelated instruction content.
- Do not globally change active authentication homes, enable remote control,
  restart shared daemons, or expose network listeners as a setup shortcut.
- First release targets same-machine native Windows Codex/Claude sessions.
  Cross-machine/WSL bridges, attachments, offline automatic wakeup, background
  work orchestration, and a new browser messaging UI are separate work.

### Managed launch path

The standard local daemon is currently unreachable on this PC: daemon version
reported connection refused at the app-server-control socket, and the read-only
proxy probe could not initialize. This is an observed installation condition,
not proof that every Codex process here uses the same home or server.

Setup must therefore provide a managed native Codex launch path. Prefer starting
the vendor's local daemon when no reachable instance exists, verify its proxy,
and launch new/resumed sessions through that same native owner. If the Windows
daemon cannot provide a usable transport, use a managed loopback app-server plus
codex --remote for sessions newly opened through ai-sessions. Authenticate any
custom listener using a supported capability token kept in DevSecrets, with the
server receiving only its hash when that option is supported. Do not place a
plaintext token file on disk or expose an unauthenticated production listener.

This is the native Codex runtime needed for receiving, not an additional messaging
broker. Track and stop only processes owned by this integration. Never restart an
existing shared daemon or move an already-running session by resuming its transcript
elsewhere. Setup reports which existing sessions need to be closed and reopened
through the managed path; the user controls when to do that. Validate this route
before enabling the install on this PC. If neither native route works, report
the blocker rather than claim all sessions are messageable.

## Verification and finish line

Unit/integration tests cover target ambiguity, generation-safe cleanup, redaction,
vault unavailable behavior, structured errors, active/idle switching, wrong-turn
retry, bounded retry, and uncertain delivery without automatic duplicate sends.

Live tests must cover Codex idle, busy tool, approval wait, idle-steer rejection,
and a stale expectedTurnId while a different turn is active. Verify both cross-harness
round trips and Claude accept/hold/refuse policies. Verify terminal-visible receipt
in actual open test terminals connected to the tested native receiver. Desktop
support must be reported separately if its owning server cannot be reached.

Run the repository test suite and lint/format checks. Test installed commands from
both agents, setup/uninstall in sandbox configs, and setup on this PC without
disturbing existing sessions. User can list reachable receivers, send/reply from
either harness, and see truthful delivery state. Release through the repository's
normal PR/CI/release flow when the implementation is ready.

## Evidence and remaining implementation calls

SESSION_MESSAGING_TESTS.md records prior live tests. The additional approval-wait
probe observed: idle steer rejected; steering during a pending approval accepted;
approval still pending four seconds later; command declined and did not execute;
message acknowledged after decline; subsequent idle steer rejected again.

A second client could list the loaded target, read status active with
waitingOnApproval, and steer it without receiving its approval request. A wrong
expectedTurnId was explicitly rejected. turn/start while that approval remained
pending returned the same turn ID and the model acknowledged both messages only
after the approval decision. Claude explicit hold produced a held event and no
model delivery; refuse produced no model delivery in the five-second test window.
Live policy mutation/default-policy behavior remains to be checked in implementation.

Before implementation, review this design for buildability, especially native
daemon proxy ownership, Claude inbound-policy enforcement with exported tokens,
and receipt limits. Implementation must stop claiming support at any boundary
the vendor API cannot actually establish. The implementation and its tests now enforce these boundaries.
