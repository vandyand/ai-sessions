# Session messaging integration probe

Tested November 6, 2026, America/New_York (November 7 UTC), on native Windows.
Codex CLI 0.166.0; Claude Code 2.1.319. Live models: configured Codex
gpt-6.1-sol at low effort; Claude Haiku (reported claude-haiku-5-5).

## Results

| Test | Observed result |
|---|---|
| Codex queue to idle loaded thread | Started a turn immediately; model acknowledged the unique marker. |
| Codex queue to busy thread | Finished the original turn, then started a separate turn for the queued message. |
| Codex turn/steer during a sleeping command | Command finished normally; the same turn acknowledged the steering message. |
| Authenticated message to Claude's Windows inbox pipe while idle | Started a turn; response recorded origin.kind=peer and the supplied sender label. |
| Unauthenticated message to that pipe | No response or matching output during the 3-second negative-control window. |
| Message to Claude during a sleeping Bash command | Tool completed normally; Claude explicitly discussed the peer message in its next answer. |
| Claude -> Codex | Claude itself executed native codex queue; target Codex acknowledged. |
| Codex -> Claude | Codex itself posted to the disposable adapter, which authenticated and wrote to Claude's native inbox; Claude acknowledged. |
| Codex -> Claude -> Codex | Claude executed its reply command through the adapter; the original Codex thread acknowledged the reply. |
| Codex B -> Codex A -> Codex B | Both agents executed native codex queue commands; B acknowledged A's reply. |

These are observed model responses, not merely successful transport writes.
Both runs completed; the second included the busy and round-trip tests.
Only explicitly started test processes were terminated. The second test server's
TCP endpoint was independently verified closed afterward. Saved test transcripts remain.

## Timing evidence from the extended run

Times are seconds from probe startup, not latency guarantees.

- Idle Codex queue accepted at 18.98; model acknowledgment at 21.55.
- Codex steering sent at 34.55 during a tool; tool finished at 48.03;
  acknowledgment at 49.62 in that same turn.
- Busy Codex queue accepted at 104.31; tool finished at 117.50;
  original answer at 119.62, original turn completed at 121.56;
  queued turn began at 121.72 and acknowledged at 125.33.
- Claude busy pipe message sent at 129.39; tool result at 140.47;
  Claude discussed the message at 144.38.
- Cross-harness reply reached the adapter at 161.41; Codex acknowledged at 165.02.
- Codex-to-Codex round trip ended with B's acknowledgment at 191.38.

The Claude busy test demonstrated receipt rather than unconditional obedience:
Claude said it prioritized the original user's requested reply over the peer's
request to add a marker. The peer message is a separate source of instructions.

## Mechanisms exercised

Codex ran in an isolated app-server with a loopback WebSocket listener.
Two threads were created on that server. All queue calls specified that server
with --remote; steering requests targeted its active thread and turn IDs.
No saved conversation was reopened in a competing process.

Claude ran with long-lived stream-json stdin/stdout so it could remain alive and
idle between turns. A test-only SessionStart hook registered its exported inbox
address and authentication token with the probe over loopback HTTP. The token
stayed in process memory; it was neither printed nor written to a file by the probe.
Only this test invocation set crossSessionInbound=accept. Global settings were unchanged.

The pipe exchange used newline-delimited JSON: an auth frame, followed by a user
frame with message.role=user, message.content, from, and msg_id. Claude recorded
the resulting message as peer origin. The supplied from string is a label, not
proof of sender identity; production identity and permission handling remain design work.

The disposable adapter exposed /to-claude and /to-codex on loopback HTTP.
The former translated posts to authenticated pipe messages; the latter called
turn/start on the original Codex thread. Cross-harness replies used this adapter,
not Claude's native SendMessage routing to a registered Codex peer.

## What this does not establish

### Additional paused-session probe

A subsequent live test used Codex approvalPolicy=on-request and an explicitly
escalated harmless echo command. Both idle steering attempts returned JSON-RPC
error -32600, `no active turn to steer`. During a pending
item/commandExecution/requestApproval, turn/steer accepted the peer message and
returned the existing turn ID. Four seconds later that turn was still waiting.
The test host declined the command: its execution status became declined with
no process ID or output. Codex then acknowledged PAUSE_STEER_ACK and reported
that the command had not run. Steering after completion returned the same idle
error. Test processes were stopped. Evidence: results-paused.json and
paused_probe.py in the same temporary probe directory.

This verifies approval waits, not arbitrary OS process suspension or every
kind of pending user-input request.

The extended approval probe used a second initialized WebSocket client. It could
list the loaded target and read active status with waitingOnApproval. A wrong
expectedTurnId was rejected. Steering from that client succeeded; it did not
receive the first client's approval request. turn/start while busy returned the
same turn ID, preserved the approval wait, and its marker was also acknowledged
after decline. These results cover two clients on an isolated app-server.

Separate live Claude tests set explicit hold and refuse on test invocations.
Hold emitted system/peer_message_hold with state=held and no model delivery;
refuse produced no model delivery in the five-second observation window. Exported
inbox-token authentication therefore did not bypass these explicit policies.
Evidence is in results-policy.json and policy_probe.py in the probe directory.

A read-only connection to this PC's standard Codex daemon could not initialize.
`codex app-server daemon version` reported connection refused at the standard
app-server-control socket. Existing independent sessions cannot be assumed
reachable; managed-launch transport setup is still required. No daemon was started
or restarted by this check.

- Exact message rendering, badges, and live updates in terminal or desktop UIs:
  these tests observed the streaming interfaces, not those screens.
- Attaching to arbitrary already-running desktop or --no-daemon sessions.
- Native Claude SendMessage replies to a Codex address without an adapter.
- Delivery under Claude's default inbound settings or live policy changes.
- Restart persistence, offline delivery, replay handling, delivery acknowledgments,
  production sender authentication, or cross-machine operation.
- MCP channels: unnecessary for this native-inbox proof and not exercised.

## Implication for ai-sessions

The receiving mechanisms work. A bridge can use Claude's inbox and Codex's owning
app-server, with explicit sender metadata and reply routing. For the requested
mid-task behavior, use turn/steer when Codex is active and turn/start when idle;
handle the race where the active turn finishes before steering arrives. Offer
codex queue for delivery after the current turn instead.

Production work should first establish safe session/endpoint registration and
credential handling, distinguish sent/accepted/acknowledged states, and verify
the real terminal/desktop experience. A custom Claude channel is an alternative
if the native inbox cannot meet reply or integration requirements.

## Local evidence

Disposable files are under
C:\Users\vandy\AppData\Local\Temp\ai-sessions-messaging-probe-20261106:

- probe.py: throwaway Python integration harness for this existing Python project.
- results.json: first run, including detailed streaming responses.
- results-extended.json: second run, with compact Claude events and all extended tests.
- schema/: schemas generated by the installed Codex executable.

The probe uses installed Python websocket-client and pywin32 packages. It is not
a supported tool, installs no dependencies, and was not integrated into ai-sessions.
No product code was changed or released.

## Vendor references

- https://code.claude.com/docs/en/cross-session-messaging
- https://learn.chatgpt.com/docs/developer-commands
- https://learn.chatgpt.com/docs/app-server
