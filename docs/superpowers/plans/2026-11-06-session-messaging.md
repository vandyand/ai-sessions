# Session Messaging Implementation Plan

For agentic workers: execute task-by-task with failing tests first and a fresh whole-branch review.

Goal: reliable same-machine Codex/Claude send/reply, managed launch, and sesres shorthand.
Architecture: direct native Codex app-server transport, Claude authenticated inbox with
DevSecrets credentials, separate atomic registrations/receipts, early CLI dispatch.
Tech Stack: existing Python 3.11+ project, native Codex CLI, PowerShell SecretManagement.
Spec: docs/superpowers/specs/2026-11-06-session-messaging-design.md.

Andrew approved moving forward using best judgment; execute here without further plan gates.
Work in the existing clean feat/session-messaging branch. No extra worktree needed for this
single coordinated change. Existing source line endings must be preserved.

## Global constraints

- No secret values in files/logs/CLI arguments; persistent credentials only in DevSecrets.
- No restart of shared processes. Never reopen a running transcript on another server.
- Steer active, start idle; three state-race retries; never retry ambiguous submission.
- Claude explicit hold/refuse preserved; native UI verified; no silent permission changes.
- Maintain startup speed and supervisor hooks. Setup/uninstall idempotent.

## Review focus

- Ambiguous session aliases and cross-harness copies refuse before submission (task 3).
- Stale registrations/PID reuse cannot route to a new session (task 2).
- Shell metacharacters/unicode never become executable message text (tasks 1, 3).
- Dropped transport after send returns uncertain without duplicate delivery (task 1).
- Hook/vault failure must not block ordinary prompts or leak credentials (task 2).

## Task 1: Codex transport and native runtime

Files: harnesses/messaging_codex.py, harnesses/messaging_rpc.py; tests/test_messaging_codex.py.
Interface: send(thread_id, text, message_id, home) -> receipt dict; reachable(home) -> list.
- [x] Write failing tests for active/idle, stale turn, pending approval, uncertainty.
- [x] Establish native daemon attachment on this PC; prefer native local transport.
- [x] Implement bounded routing with clientUserMessageId and metadata state reads.
- [x] Verify two-client terminal delivery and preserve approval ownership.

## Task 2: Registrations, Claude, and hook setup

Files: messaging/store.py, harnesses/messaging_claude.py, messaging/hook.py, messaging/install.py;
tests/test_messaging_claude.py and tests/test_messaging_store.py.
Interface: register(payload, env), receivers(), send(receiver,text,message_id) -> receipt;
install()/uninstall() preserve unrelated settings and supervisor hooks.
- [x] Tests for generation-safe registrations, no credentials in JSON, vault failures.
- [x] Store exported token only in DevSecrets through PowerShell stdin; authenticated
  named-pipe submission stays inside helper with no secret output.
- [x] Install lightweight SessionStart registration and agent send/reply guidance.
- [x] Verify accept/hold/refuse using disposable sessions; preserve defaults explicitly.

## Task 3: CLI, replies, launch integration, sesres

Files: messaging/cli.py, messaging/__init__.py, restart.py, app.py, pyproject.toml;
tests/test_messaging_cli.py and tests/test_restart_alias.py.
Interface: sessions send/reply/messaging status/install/uninstall; sesres forwards --restart.
- [x] Failing tests for resolution, failed/uncertain states, reply routing, literal text.
- [x] Fast early dispatch before catalog/state initialization; bounded receipt history.
- [x] Messaging install enables native daemon use for future sessions, not a new broker.
- [x] Register sesres entrypoint and install a local executable shim immediately;
  verify forwarding with a mock sessions executable rather than restarting this session.

## Task 4: verification, documentation, delivery

- [x] Live installed send/reply from both agents, active/idle/approval cases, real terminal.
- [x] Run PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider and ruff 0.16.3.
- [x] Fresh whole-branch review; fix substantive findings and run relevant checks again.
- [x] Document commands, setup/restart, limits, retention, exit codes and recovery.
- [ ] Version/changelog, PR CI, squash merge, GitHub/PyPI release, local upgrade as
  established in this project. Install messaging locally and confirm reachable test receivers.

## Execution ledger

- Native daemon 0.166.0 attachment verified: proxy carries WebSocket frames, not JSONL.
- Ruling: reuse the vendor daemon and local unix:// terminal route; no new broker.
- Ruling: use the existing isolated feature branch; Andrew authorized execution using best judgment.
- Provider transports moved under harnesses/ and registered through optional MessagingHooks,
  preserving source-architecture checks and lazy normal startup.
- sesres executable installed and verified from PowerShell/Bash without restarting user sessions.
- Disk exhaustion interrupted work; after space recovery the failed source write was restored
  exactly from Git. Subsequent full suite passed 1,080 tests before review fixes.
- Live Windows ConPTY verified real Codex rendering and an agent acknowledgment.
- Installed Codex -> Claude -> Codex commands verified correlated acknowledgment in receipts.
- Fresh review fixes cover endpoint binding, monotonic receipts, configured launcher prefixes,
  custom-home mismatch rejection, and transactional/idempotent/fair vault cleanup.
- Final full suite: 1,087 passed, 2 skipped, 142 subtests. Ruff lint/format clean. No custom daemon or auth-home swap.

- Validation: 1,087 tests passed after final Unicode/cleanup regressions; live installed cross-harness round trip and correlated acknowledgment passed. CI/release use the established downstream workflow.
