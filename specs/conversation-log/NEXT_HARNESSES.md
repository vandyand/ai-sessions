# Recommended next harnesses

Research refreshed 2026-08-23 from provider documentation and source. The ranking favors a real
native identity, exact non-interactive resume, complete local history, a safe write/import path,
Windows/Linux support, and architectural value beyond duplicating an existing adapter.

## Recommendation

| Rank | Harness | Why it belongs next | Primary risk / Phase 0 gate |
| --- | --- | --- | --- |
| 1 | **Gemini CLI** | Automatic project-scoped JSONL sessions, stable UUID resume, machine-listable sessions, complete tool history, and open source. Current source also has a native session-file import path that validates a conversation, remints its ID for the target project, and writes the provider's own JSONL shape. | The import flag is present in current source but absent from the stable configuration reference. Pin a released version and prove the installed CLI exposes and preserves it before designing the writer. Retention defaults to 30 days. |
| 2 | **Qwen Code** | Project-scoped JSONL, exact headless `--resume`, JSON session listing, native export in four formats, explicit session IDs, Python/TypeScript SDKs, and unusually strong daemon APIs for persisted transcript, export, load, resume, archive, and liveness. It is the cleanest test of a provider-supported API-backed adapter. | Fast-moving surface and some lineage with Gemini reduce differentiated coverage. Prove one pinned release's CLI/daemon capabilities on Windows and Linux, and choose one authority (files or daemon) rather than mixing snapshots. |
| 3 | **GitHub Copilot CLI** | Large practical audience, exact ID/name resume, complete per-session `events.jsonl`, compaction checkpoints, local SQLite search index, and an official reindex recovery path. Its shared local-plus-cloud history would extend the architecture into optional remote synchronization. | There is no documented native import command. Session data syncs to GitHub by default, so a writer could create remote side effects. Phase 0 must use isolated config with `remoteExport: false` and prove a supported import/reindex path; otherwise ship read/discover/resume only and keep it out of bridge targets. |

Recommended execution order is **Gemini → Qwen → Copilot**. If user reach matters more than
delivery risk, Copilot can be researched in parallel with Qwen, but it should not jump the writer
gate on the strength of an undocumented `events.jsonl` mutation.

## Why Gemini is first

Gemini's official session-management documentation says every conversation is automatically saved
under `~/.gemini/tmp/<project_hash>/chats/`, including prompts, responses, tool inputs/outputs,
token usage, and available reasoning summaries. It supports `--list-sessions` and exact
`--resume <UUID>` on the command line. In current source, `resolveSessionId(..., sessionFileArg)`
loads a conversation record, filters it to native user/Gemini messages, remints the session/project
identity, and writes a new session JSONL. That is nearly the same safe writer posture that made
OpenCode viable: use a provider-owned import instead of guessing at a live store.

The first spike should capture one released session schema on native Windows and Linux, exercise
resume by full UUID, determine the installed name/stability of the session-file flag, and verify a
Claude/Codex/OpenCode transcript imported through it remains visible and model-readable. It must
also pin compaction/checkpoint behavior and retention cleanup before implementation.

## Why Qwen is second

Qwen exposes more supported integration surface than any other candidate found. `qwen sessions
list --json` returns identity, time, title, branch, first prompt, file path, and cwd. Headless mode
resumes an exact UUID and restores conversation, tool output, and compression checkpoints. Native
exports support HTML, Markdown, JSON, and JSONL. Its daemon separately exposes complete persisted
transcript paging and export without attaching a client, plus load/resume and exact live status.

The adapter should prefer CLI/file discovery when the daemon is absent and treat daemon use as one
prepared authority for the duration of a read/write operation. Phase 0 must decide whether a new
session can be populated through a supported SDK/CLI path without a paid model turn; if not, a
pinned native JSONL writer needs the same visible-scrollback/model-context proof used for Codex.

## Why Copilot is third

GitHub documents a complete local record at `~/.copilot/session-state/<session-id>/events.jsonl`
with workspace metadata, plans, compaction checkpoints, and artifacts. `--resume=<ID>` is exact and
non-interactive, while the separate `session-store.db` is reconstructible with `/chronicle reindex`.
This makes discovery and reading plausible and recovery testable.

Writing is the problem. The supported docs describe deletion, resume, and reindex, but not import.
They also state that sessions sync to the user's GitHub account by default. The feasibility spike
must therefore prove all work in an isolated home with remote export disabled, inspect event and
checkpoint semantics, and establish whether an official command can ingest a portable transcript.
No direct mutation of the index and no accidental cloud sync should be accepted.

## Deferred candidates

- **Cursor Agent CLI** supports listing and exact `--resume=<chat-id>` and exposes ACP
  `session/load`, but its public CLI documentation does not identify a complete local transcript,
  export, or import contract. Revisit when a provider-supported read/export path exists.
- **Aider** can restore `.aider.chat.history.md`, but that is one project-level Markdown history,
  not a catalog of independently identified native sessions. It does not currently satisfy the
  `(session_id, storage)` identity and exact-resume contract without inventing utility-owned
  identities for provider state.

## Primary sources

- Gemini CLI session management:
  https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md
- Gemini CLI import/resume implementation:
  https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/gemini.tsx
- Qwen Code session commands:
  https://qwenlm.github.io/qwen-code-docs/en/users/features/commands/
- Qwen Code headless resume/storage:
  https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- Qwen Code daemon transcript/export/resume:
  https://github.com/QwenLM/qwen-code/blob/main/docs/users/qwen-serve.md
- GitHub Copilot CLI session data:
  https://docs.github.com/en/copilot/concepts/agents/copilot-cli/chronicle
- GitHub Copilot CLI command reference:
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference
- GitHub Copilot CLI configuration directory:
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
- Cursor Agent CLI parameters:
  https://docs.cursor.com/en/cli/reference/parameters
- Aider history configuration:
  https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/aider_conf.md
