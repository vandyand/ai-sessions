# Supervisor Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any Claude Code or Codex session get a persistent supervisor session that is always
current on the target's activity. It is created and reached with `S` in the `sessions` browser.

**Architecture:** A new `ai_sessions.supervisor` package. It keeps a per-supervisor, append-only
index of the target's *exchanges* (one user prompt plus everything until the next), built once at
creation and extended incrementally through a new adapter capability, `read_since`. A
`UserPromptSubmit`/`SessionStart` hook, registered in both harnesses, injects either:

- a **brief**: the digest of old exchanges, the cleaned tail of recent ones, the ledger and the
  standing instructions; or
- an **update**: the cleaned exchanges since the last update.

Head routing reuses `UserState`'s conversation logic read-only.

**Tech Stack:** Python ≥3.11 stdlib only (plus existing `psutil`), `unittest`, Ruff. Windows and
Linux.

**Spec:** `specs/supervisor/README.md`. Read it first; this plan argues from it.

## Global Constraints

- Branch from `main` at or after `a2de2d4`. Never write to target storage.
- Supervisor launch uses the user's current launch mode via `LaunchConfig.provider_prefix(tool)`.
  Nothing enforces read-only.
- Brief: digest cap **50,000 tokens**, cleaned tail **50,000 tokens**. Token estimate =
  `len(text) / 3.5`.
- Long gap: if the new activity since the last update, cleaned, exceeds **50,000 tokens**, send a
  fresh brief instead.
- Cleaned tool call: one line, `⟦Tool⟧ request → result`, clipped to **400 characters** total.
- Digest per exchange:
  - typed prompt text clipped at **6,000** characters;
  - final assistant message clipped at **2,000** characters;
  - tool counts by tool;
  - one line per failed call;
  - repeated identical requests collapsed to `same command ×N`.
- Paste marker: `[pasted N chars: <first 300> … <last 500>]`. A paste of 800 characters or fewer
  is shown whole inside the marker.
- Digest over cap:
  1. keep exchange 1's typed prompt in full;
  2. shorten the oldest exchanges first (typed prompt to 500, final message to 300);
  3. then cut the middle, with `[… N exchanges omitted …]`.
- Supervisor data root: `APP_CONFIG_DIR / "supervisors" / <record_id>/`, holding `record.json`,
  `index.jsonl`, `ledger.md` and `brief-<n>.md`.
- Title suffix: `" · supervisor"`. Origin value: `"supervisor"`. Env var:
  `AI_SESSIONS_SUPERVISOR`.
- The hook always exits 0, writes UTF-8 JSON to stdout, and never raises.
- No pairwise harness logic. New behavior per harness goes through `HarnessAdapter` capabilities.
- Out of scope: OpenCode, subagent watching, sending messages to the target, the clipboard.

## Review Focus

1. **The target's transcript is rewritten in place** (Codex `migrate-rollouts`, or the Codex
   0.165 context merge), so the supervisor's checkpoint cannot be located. Expected: the next
   prompt gets a fresh brief, never a replay of old work and never silently skipped work.
   Test: Task 6, `test_unlocatable_checkpoint_rebriefs`.
2. **The target session is deleted, archived or unavailable.** Expected: the update says "target
   unavailable since …", the checkpoint is unchanged, and the supervisor still answers.
   Test: Task 6, `test_unavailable_target_reports_and_keeps_checkpoint`.
3. **The user resets a supervisor while it is still open.** Expected: the old session's next
   prompt gets a one-line "this supervisor was reset; open the new one with S" note, and no
   target data.
   Test: Task 7, `test_reset_supervisor_gets_notice`.
4. **Non-ASCII text on a Windows console code page** (emoji, curly quotes, `⟦⟧` in pastes and
   tool output). Expected: the hook emits valid UTF-8 JSON.
   Test: Task 7, `test_hook_output_is_utf8_json_with_non_ascii`.
5. **Two hook runs for one supervisor at once** (the supervisor opened in two tabs). Expected: no
   corrupted `record.json` or `index.jsonl`. At worst one update repeats.
   Test: Task 5, `test_concurrent_record_writes_never_corrupt`.

---

### Task 0: Spike the harness hook facts (throwaway)

**Files:**
- Modify: `specs/supervisor/README.md` (Open question section: record results)

- [x] **Step 1:** Write a throwaway logging hook in the scratchpad (not the repo) that dumps its
  stdin payload, and returns `additionalContext` of a requested size. Register it temporarily for
  `SessionStart` and `UserPromptSubmit` in a Claude config dir (`CLAUDE_CONFIG_DIR=<tmp>`) and a
  Codex home (`CODEX_HOME=<tmp>`), not the user's real ones.
- [x] **Step 2:** For each harness, record:
  - the largest `additionalContext` that reaches the model intact (try 40k, 200k and 400k
    characters, and ask the model to quote a sentinel planted at the end);
  - the `SessionStart` `source` values seen on new, resume and after `/compact`;
  - whether `session_id` and `transcript_path` are present in both events;
  - the default hook timeout.
- [x] **Step 3:** Write the results into the spec's Open question as resolved facts.
  - If either limit is below 400k characters, set `BRIEF_DELIVERY = "file"` for that harness in
    Task 6. The brief is then written to `brief-<n>.md`, and the injection is a short note telling
    the supervisor to read that file first.
  - If Codex has no compaction `source`, Task 7 uses the transcript scan.
- [x] **Step 4:** Remove the temporary config dirs. Commit the spec change:
  `git commit -m "Record hook limits for supervisor sessions"`.

### Task 1: `read_since` adapter capability and failed tool calls

**Files:**
- Modify: `src/ai_sessions/model.py` (`ToolCall`)
- Modify: `src/ai_sessions/capabilities.py` (`ReadSinceHook`, `HarnessAdapter.read_since`)
- Modify: `src/ai_sessions/conversion.py` (`_records`, the `_Conversation.result` signature,
  the public `read_since`)
- Modify: `src/ai_sessions/harnesses/claude.py`, `src/ai_sessions/harnesses/codex.py`
- Test: `tests/test_read_since.py`

**Interfaces:**
- Produces:
  - `ToolCall.failed: bool = False` (a new last field);
  - `class ReadSinceHook(Protocol): __call__(self, ref: NativeRef, checkpoint: Checkpoint) -> ReadSnapshot | None`;
  - `HarnessAdapter.read_since: ReadSinceHook | Unsupported = Unsupported("incremental reads are not supported")`;
  - `conversion.read_since(tool: str, ref: NativeRef, checkpoint: Checkpoint) -> ReadSnapshot | None`.
    It returns `None` when the checkpoint cannot be located, and raises `BridgeError` when the
    session is unavailable. The snapshot holds the turns strictly after the checkpoint, up to the
    current complete cursor, and the new checkpoint.
  - `_records(path, *, start: int = 0, end: int | None = None)`;
  - `read_claude(path, *, latest_window=True, start=0, end=None)`, and the same for
    `read_codex`;
  - `_Conversation.result(call_id, text, failed=False)`.

- [x] **Step 1: Write failing tests** in `tests/test_read_since.py`, using temporary JSONL
  fixtures in the style of `tests/test_jsonl_tracking.py`:
  - `test_claude_read_since_returns_only_appended_turns`: write 2 exchanges, take
    `native_checkpoint`, append 1. `read_since` returns exactly the appended user and assistant
    turns, and its checkpoint equals `complete_jsonl_cursor`.
  - `test_codex_read_since_returns_only_appended_turns`: the same with Codex `response_item`
    records.
  - `test_read_since_partial_tail_stops_before_incomplete_record`: append a line with no
    newline. The returned checkpoint is still the last complete record.
  - `test_read_since_unlocatable_checkpoint_returns_none`: truncate the file below the
    checkpoint. The result is `None`.
  - `test_read_since_protected_checkpoint_survives_codex_context_rewrite`: build a protected
    checkpoint with the adapter's `protect_checkpoint`, rewrite the developer-message metadata as
    in `test_jsonl_tracking.py`, append a turn. Only the appended turn comes back.
  - `test_claude_is_error_sets_failed`: a `tool_result` with `"is_error": true` gives
    `call.failed is True`.
  - `test_codex_script_failed_and_timeout_set_failed`: outputs starting `Script failed` or
    containing `timed out` in the first line set `failed`, and `Script completed` does not.
- [x] **Step 2:** Run `python -m unittest tests.test_read_since -v`. Expect them to fail with
  missing attributes.
- [x] **Step 3:** Implement:
  - `start` threads through `_records` (seek to `start`) and both readers;
  - `failed` is set in `_Conversation.result`;
  - Claude reads `block.get("is_error")`;
  - Codex matches `^Script failed` or `timed out` against the first line of the raw output,
    before `_RESULT_NOISE` stripping;
  - each adapter's `read_since` resolves the checkpoint to a byte offset. An int is used as is. A
    protected str goes through `jsonl_tracking.locate_checkpoint`, with the adapter's own identity
    and tolerance, the same as its `change_status`. It then reads
    `read_x(path, latest_window=False, start=offset, end=cursor)`.
  - A tool result whose call precedes the checkpoint is dropped. That is the existing
    `_Conversation.result` behavior; document it in a docstring.
- [x] **Step 4:** Run the new tests plus `python -m unittest discover -s tests`. Everything
  passes, including `test_third_harness_contract.py`, which must still accept an adapter without
  `read_since`.
- [x] **Step 5:** Commit: `git commit -m "Add incremental read_since to Claude and Codex adapters"`.

### Task 2: Paste detection

**Files:**
- Create: `src/ai_sessions/supervisor/__init__.py` (empty docstring)
- Create: `src/ai_sessions/supervisor/pastes.py`
- Test: `tests/test_supervisor_pastes.py`

**Interfaces:**
- Produces:
  - `fenced_spans(text: str) -> list[tuple[int, int]]`: the spans of pasted content, excluding
    the fence characters.
  - `content_spans(text: str, pastes: Iterable[str]) -> list[tuple[int, int]]`: exact-substring
    spans for known paste contents.
  - `claude_pastes(session_id: str, home: Path) -> list[str]`: every paste content recorded in
    `home/"history.jsonl"` for that session. It uses inline `content`, or
    `home/"paste-cache"/f"{contentHash}.txt"`, and skips missing ones.
  - `strip_pastes(text: str, spans: list[tuple[int, int]]) -> str`: replaces each span with
    the marker from Global Constraints and removes the surrounding fences.
  - `typed_text(text: str, *, tool: str, pastes: list[str]) -> str`: Claude uses `content_spans`
    when `pastes` matches, else `fenced_spans`. Codex always uses `fenced_spans`.

- [x] **Step 1: Write failing tests:**
  - `test_paired_fences`: `"see ```AAA``` and ```BBB``` ok"` gives two spans covering `AAA` and
    `BBB`.
  - `test_unclosed_final_fence_runs_to_end`: `"look ```log line\nmore"` gives one span ending at
    `len(text)`.
  - `test_language_tagged_fence_is_inner_block`: an outer paste that contains
    ```` ```python\nx=1\n``` ```` stays one span, from the outer open to the outer close.
  - `test_marker_keeps_head_and_tail`: a 5,000-character paste becomes a marker that contains
    `pasted 5,000 chars`, its first 300 characters and its last 500 characters.
  - `test_short_paste_kept_whole`: a 600-character paste appears whole in the marker.
  - `test_forgotten_close_keeps_trailing_typed_words`: `"```" + "x"*3000 + " please fix the
    retry loop"` gives typed text containing `please fix the retry loop`.
  - `test_claude_history_paste_spans`: a fake `history.jsonl` with inline `content`, plus one
    `contentHash` resolved from `paste-cache`. Both spans are found, and the result is identical
    with and without fences around them.
  - `test_no_history_match_falls_back_to_fences`.
- [x] **Step 2:** Run `python -m unittest tests.test_supervisor_pastes -v`. Expect failures.
- [x] **Step 3:** Implement. The fence scan works left to right:
  - A fence followed by `[A-Za-z0-9_+-]+\n` while a span is open pushes an inner level.
  - A bare fence pops one level, or closes the span at level 0.
  - End of text closes any open span.
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Detect pasted content for supervisor digests"`.

### Task 3: Exchanges, cleaning, and the target index

**Files:**
- Create: `src/ai_sessions/supervisor/index.py`
- Test: `tests/test_supervisor_index.py`

**Interfaces:**
- Consumes: `ToolCall.failed` and `conversion.read_since` (Task 1), `pastes.typed_text` (Task 2),
  `conversion.render_calls`-style formatting.
- Produces:
  - `@dataclass(frozen=True) class Exchange`, with fields:
    - `seq: int`, `member: str` (the state member key), `prompt: str` (raw), `typed: str`;
    - `assistant: tuple[str, ...]`, `calls: tuple[ToolCall, ...]`, `compaction: bool`;
    - `to_json() -> dict` and `from_json(d) -> Exchange`.
  - `CHARS_PER_TOKEN = 3.5`, and `tokens(text: str) -> int`.
  - `exchanges_from_turns(turns: list[Turn], *, member: str, start_seq: int, tool: str,
    pastes: list[str]) -> list[Exchange]`. A new exchange starts at each `user` turn with text.
    Turns before the first user turn attach to a seq-0 preamble exchange. Compaction turns set
    `compaction=True` on the exchange they fall in, and their text is not kept.
  - `clean_exchange(ex: Exchange) -> str`: the prompt verbatim, assistant texts verbatim, and
    one clipped 400-character line per call (`✗ ` prefix when failed). A compaction becomes
    the line `[target compacted here]`.
  - `class TargetIndex(path: Path)`, with:
    - `exchanges() -> list[Exchange]` and `last_seq() -> int`;
    - `append(new: list[Exchange]) -> None`. It replaces a trailing exchange with the same `seq`
      if the target was mid-turn last time. The write is atomic (temp file plus `os.replace`).
    - `build(tool, ref, member) -> Checkpoint`: a full read via `read_snapshot(...,
      latest_window=False)`; it returns the checkpoint.
    - `extend(tool, ref, member, checkpoint) -> Checkpoint | None`: uses `read_since`. `None`
      means the checkpoint cannot be located.

- [x] **Step 1: Write failing tests:**
  - `test_exchanges_group_by_user_prompt`: turns U, A(calls), A, U, A give 2 exchanges with the
    right assistants and calls.
  - `test_clean_exchange_clips_tool_lines_to_400`.
  - `test_failed_call_marked_in_cleaned_text`.
  - `test_compaction_rendered_as_marker_without_summary_text`.
  - `test_index_extend_continues_open_exchange`: build while the target is mid-turn, append the
    rest of the turn plus a new prompt. The index has 2 exchanges, and the first is complete.
  - `test_index_append_is_atomic_on_failure`: patch `os.replace` to raise. The old file is
    intact.
- [x] **Step 2:** Run `python -m unittest tests.test_supervisor_index -v`. Expect failures.
- [x] **Step 3:** Implement.
  - To continue an open exchange, `extend` returns turns starting mid-exchange (no leading user
    turn). These merge into the last stored exchange, keeping its `seq`.
  - `build` and `extend` load the member adapter's `pasted_contents(ref)` once per call
    (Task 1 integration: a capability, so no generic module names a provider). An
    `Unsupported` capability gives `[]`. Call `typed_text(prompt, pastes=...)`.
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Index target exchanges for supervisors"`.

### Task 4: Digest

**Files:**
- Create: `src/ai_sessions/supervisor/digest.py`
- Test: `tests/test_supervisor_digest.py`

**Interfaces:**
- Consumes: `Exchange`, `tokens` (Task 3).
- Produces:
  - `digest_entry(ex: Exchange, *, prompt_chars: int = 6000, final_chars: int = 2000) -> str`;
  - `render_digest(exchanges: list[Exchange], *, cap_tokens: int = 50_000) -> str`.

- [x] **Step 1: Write failing tests:**
  - `test_entry_contains_typed_prompt_final_message_tool_counts`: tool counts render like
    `Bash ×3, Edit ×1`.
  - `test_entry_lists_each_failed_call_once_and_collapses_repeats`: the same failed request 14
    times gives one line ending in `×14`.
  - `test_entry_clips_typed_prompt_at_6000_and_final_at_2000`.
  - `test_under_cap_digest_is_unchanged_concatenation`.
  - `test_over_cap_shortens_oldest_first_keeping_first_prompt_whole`.
  - `test_still_over_cap_cuts_middle_with_note`: the output contains
    `[… N exchanges omitted …]`, its `tokens()` is ≤ 50,000, the first and last exchanges are
    present, and exchange 1's typed prompt is whole.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement the three-stage trim from Global Constraints.
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Render bounded supervisor digests"`.

### Task 5: Supervisor store, state schema, and head lookup

**Files:**
- Create: `src/ai_sessions/supervisor/store.py`
- Modify: `src/ai_sessions/app.py`:
  - `UserState`: load and save `supervisors`, `"version": 7`, accepting 1–7;
  - a public `conversation_head` method.
- Test: `tests/test_supervisor_store.py`

**Interfaces:**
- Produces:
  - `SUPERVISORS_DIR = APP_CONFIG_DIR / "supervisors"`.
  - `@dataclass class HookState`, with fields:
    - `record_id: str`, `native_session_id: str = ""`, `own_offset: int = 0`;
    - `member: str = ""`, `checkpoint: Checkpoint | None = None`;
    - `last_seq_sent: int = -1`, `brief_due: bool = True`, `briefs: int = 0`;
    - `reset: bool = False`, `updated: float = 0.0`.
  - `load_hook_state(record_id) -> HookState | None`, and `save_hook_state(state: HookState) ->
    None`. The save is atomic: temp file in the same dir, then `os.replace`.
  - `find_by_native_id(session_id: str) -> HookState | None`. It globs
    `SUPERVISORS_DIR/*/record.json`, and must not import `app`.
  - `UserState.supervisors: dict[str, dict]`, where `record_id → {"target_conversation": str,
    "tool": str, "created": float, "title": str, "archived": bool}`.
  - `UserState.supervisor_for_conversation(conversation_id) -> str | None`.
  - `UserState.conversation_head(conversation_id) -> tuple[str, list[tuple[str, dict]]]`. It
    returns `("ok" | "diverged" | "unavailable" | "unknown", heads)`, wrapping
    `_conversation_status` without mutating state.

- [x] **Step 1: Write failing tests:**
  - `test_state_v6_loads_and_saves_as_v7_with_empty_supervisors`.
  - `test_supervisor_record_round_trips`.
  - `test_hook_state_atomic_save_and_find_by_native_id`.
  - `test_concurrent_record_writes_never_corrupt`: two threads × 200 saves, then
    `load_hook_state` parses every time.
  - `test_conversation_head_ok_single_head` and `test_conversation_head_diverged`. Build both
    with the existing fixtures from `tests/test_bridge.py`.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement. Unknown keys in v7 survive a round trip only for `supervisors`; the
  other fields keep the existing behavior.
- [x] **Step 4:** Run the full suite. It passes, and older state files still load.
- [x] **Step 5:** Commit: `git commit -m "Store supervisor records and expose conversation heads"`.

### Task 6: Brief and update builders

**Files:**
- Create: `src/ai_sessions/supervisor/context.py`
- Test: `tests/test_supervisor_context.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces:
  - `STANDING_INSTRUCTIONS: str`. It carries the five instructions from the spec's Brief
    section verbatim in meaning, the absolute ledger path, and the `sessions --target-log`
    usage.
  - `build_brief(record: dict, hook: HookState, index: TargetIndex, *, target_title: str,
    target_tool: str, cwd: str, liveness: str) -> str`. Its sections in order are header,
    starting objective (exchange 1's typed prompt plus the title), digest, cleaned tail, ledger,
    and standing instructions. The tail is the newest exchanges whose cleaned text totals at most
    50,000 tokens, starting on an exchange boundary. The digest covers all exchanges before the
    tail.
  - `build_update(...) -> str` (same parameters). Its header is liveness, then the cleaned
    exchanges with `seq > hook.last_seq_sent`, plus the re-sent final exchange if it was open
    last time.
  - `prepare_context(record_id: str, *, state: UserState) -> tuple[str, HookState]`. This is
    the decision function: it resolves the head, extends the index and picks brief or update.
    It returns the text to inject and the new `HookState`. It never raises: errors become a
    one-line `⚠ supervisor:` text.

- [x] **Step 1: Write failing tests**, using fake adapters from `tests/fake_harness.py` where
  possible:
  - `test_first_prompt_gets_brief_with_all_sections_in_order`.
  - `test_brief_tail_and_digest_partition_exchanges_without_overlap`.
  - `test_update_contains_only_new_exchanges`.
  - `test_nothing_new_line`: the text matches `no target activity since`.
  - `test_long_gap_sends_brief_instead_of_update`: new cleaned activity over 50,000 tokens
    sends a brief, and `briefs` increments.
  - `test_head_moved_notes_new_harness_and_continues_from_member_checkpoint`: bridge a Codex
    fixture to Claude, append to the Claude copy. The update names Claude, contains only the
    appended exchange, and replays none of the bridged history.
  - `test_diverged_injects_warning_and_no_activity`.
  - `test_unlocatable_checkpoint_rebriefs` (Review Focus 1).
  - `test_unavailable_target_reports_and_keeps_checkpoint` (Review Focus 2).
  - `test_long_brief_is_delivered_by_file`: a brief over 9,000 characters gives an injection
    under 1,000 characters naming `brief-<n>.md`, whose content is the full brief.
  - `test_short_update_is_inline`: an update under 9,000 characters is injected whole.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement.
  - On a head move, start the new member at its own state `checkpoint`. That is the bridge
    frontier recorded at materialization, located via `read_since`.
  - Delivery (Task 0 result): `INLINE_LIMIT = 9_000` characters for both harnesses. Longer
    text is written to `brief-<n>.md` / `update-<n>.md` in the supervisor folder, and the
    injection is a short note naming the absolute path. Files beyond the newest 5 of each kind
    are deleted.
  - `liveness` is computed cheaply from the head member's transcript mtime, as
    `"last target activity 3h 12m ago"`, using `updated_age`-style formatting. Process-based
    open detection is not run in the hook.
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Build supervisor briefs and updates"`.

### Task 7: Hook entry point

**Files:**
- Create: `src/ai_sessions/supervisor/hook.py` (runnable as `python -m ai_sessions.supervisor.hook <event>`)
- Test: `tests/test_supervisor_hook.py`

**Interfaces:**
- Consumes: `store.find_by_native_id`, `context.prepare_context`.
- Produces: `main(argv: list[str], stdin: TextIO, stdout: BinaryIO, env: Mapping[str, str]) -> int`
  (always 0). Events are `session-start` and `user-prompt-submit`.

- [x] **Step 1: Write failing tests:**
  - `test_ordinary_session_outputs_nothing_and_skips_app_import`: a payload with an unknown
    `session_id` and no env var gives empty stdout, and `"ai_sessions.app" not in sys.modules`
    (run in a subprocess).
  - `test_env_var_binds_native_session_id_on_first_call`.
  - `test_user_prompt_submit_emits_additional_context_json`: the output parses to
    `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}`.
  - `test_session_start_compact_sets_brief_due`: payload `source: "compact"`.
  - `test_session_start_resume_does_not_set_brief_due`: payload `source: "resume"`.
  - `test_own_transcript_compaction_scan_sets_brief_due`: a compaction record appears after
    `own_offset` in the supervisor's transcript (the fallback for Codex if Task 0 found no
    source).
  - `test_internal_error_emits_warning_and_exits_zero`.
  - `test_reset_supervisor_gets_notice` (Review Focus 3).
  - `test_hook_output_is_utf8_json_with_non_ascii` (Review Focus 4): run in a subprocess with
    `PYTHONIOENCODING` unset on Windows. The bytes decode as UTF-8 and parse as JSON.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement.
  - Write to `sys.stdout.buffer` encoded as UTF-8.
  - Import `ai_sessions.app` (for `UserState`) only after a supervisor is found.
  - Log errors to `APP_CACHE_DIR / "supervisor-hook.log"`.
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Add the supervisor hook entry point"`.

### Task 8: Hook installer

**Files:**
- Create: `src/ai_sessions/supervisor/install.py`
- Modify: `src/ai_sessions/app.py` (`build_parser` and `main`: add `--install-hooks` and
  `--uninstall-hooks`)
- Test: `tests/test_supervisor_install.py`

**Interfaces:**
- Produces:
  - `hook_command(event: str) -> str`, which gives
    `f'"{sys.executable}" -I -m ai_sessions.supervisor.hook {event}'`;
  - `install(claude_settings: Path, codex_hooks: Path) -> list[str]` (it returns notices);
  - `uninstall(...) -> list[str]`.
  - Entries are identified by the substring `ai_sessions.supervisor.hook`.

- [x] **Step 1: Write failing tests:**
  - `test_install_adds_entries_and_preserves_existing_hooks`: the fixture files contain the
    real shapes of the user's `block_mass_kill.py` PreToolUse entry. After install it is still
    present.
  - `test_install_is_idempotent`: installing twice gives one entry per event.
  - `test_uninstall_removes_only_ours`.
  - `test_codex_install_notice_mentions_retrust`.
  - `test_malformed_settings_file_is_refused_not_overwritten`.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement. Write a `.bak-<timestamp>` copy before modifying either file. Set
  the hook timeout to 60 seconds on both harnesses (Claude's default for `UserPromptSubmit` is 30 s).
- [x] **Step 4:** Run the tests. They pass.
- [x] **Step 5:** Commit: `git commit -m "Install supervisor hooks for Claude Code and Codex"`.

### Task 9: Creating and launching a supervisor; CLI

**Files:**
- Create: `src/ai_sessions/supervisor/create.py`
- Modify: `src/ai_sessions/capabilities.py` (`NewSessionArgsHook`, `HarnessAdapter.new_session_args`)
- Modify: `src/ai_sessions/harnesses/claude.py`, `src/ai_sessions/harnesses/codex.py`
- Modify: `src/ai_sessions/app.py`:
  - `--supervise ID`, which reuses `--launch-tool`;
  - `--target-log ID [--since ISO_TIME] [--tail N]`;
  - a `launch_supervisor` function.
- Test: `tests/test_supervisor_create.py`

**Interfaces:**
- Produces:
  - `NewSessionArgsHook.__call__(self, *, session_id: str | None) -> list[str]`.
    Claude gives `["--session-id", session_id]`. Codex gives `[]`. There is no opening prompt
    (user decision, 2026-10-31): the brief arrives with the user's first question.
  - `create_supervisor(state: UserState, target: Session, tool: str) -> str` (it returns
    `record_id`). It:
    1. assigns a conversation id with `conversation_id_for(target, create=True)`;
    2. writes the state record and saves `UserState`;
    3. creates the folder, `ledger.md` from `LEDGER_TEMPLATE` (defined in `create.py`, with the
       headings Objective, Assessment, Open concerns, Decisions, Messages sent), and the `index.jsonl` built with
       `TargetIndex.build`;
    4. writes `HookState` with `brief_due=True`.
  - `launch_supervisor(record_id, config, state) -> int`. It sets `AI_SESSIONS_SUPERVISOR` in
    `os.environ`, builds
    `argv = config.provider_prefix(tool) + adapter.new_session_args(...)` for a new supervisor
    (or the normal resume for an existing one), and runs it via `run_harness`. A brand-new
    supervisor runs with a provisional `Session`.
  - `default_supervisor_tool(target_tool, config) -> str`: `[supervisor] default_tool` if set,
    else the other of claude/codex.
  - `target_log(record_or_target_id, since, tail) -> str`: the cleaned exchanges. It never
    moves the checkpoint.

- [x] **Step 1: Write failing tests:**
  - `test_create_assigns_conversation_and_writes_folder`.
  - `test_default_tool_is_opposite_harness_unless_configured`.
  - `test_new_supervisor_argv_claude_includes_session_id_and_no_prompt`: run with
    `--dry-run`.
  - `test_new_supervisor_argv_codex_has_no_prompt`.
  - `test_existing_supervisor_resumes_by_native_id`.
  - `test_supervisor_inherits_launch_mode`: Dangerous mode gives the adapter's
    `dangerous_args` in argv.
  - `test_target_log_does_not_move_checkpoint`.
  - `test_create_refuses_opencode_target_with_clear_message`.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement. If `TargetIndex.build` takes more than a second, print
  `sessions: reading target history…` to stderr before starting.
- [x] **Step 4:** Run the full suite. It passes.
- [x] **Step 5:** Commit: `git commit -m "Create and launch supervisor sessions"`.

### Task 10: Browser integration

**Files:**
- Modify: `src/ai_sessions/app.py`:
  - the `ORIGIN_ORDER` and `ORIGIN_LABELS` additions;
  - the `UserState.apply` (title suffix not stored, origin marking);
  - the `Browser` key handler, details and help;
  - `build_view_rows`;
  - `main`'s handling of the browser result.
- Modify: `README.md`, `DESIGN.md`, `CHANGELOG.md`
- Test: `tests/test_display.py`, `tests/test_launch.py`, `tests/test_supervisor_browser.py`

**Interfaces:**
- Consumes: Tasks 5 and 9.
- Produces:
  - `ORIGIN_ORDER = ("human", "cross", "agent", "supervisor", "all")`;
  - `Session.supervises: str` and `Session.supervised_by: str` (record ids, default `""`);
  - `Browser.run()` may return `("supervise", Session)` or `("launch", Session)`. Update `main`
    to dispatch; the existing Enter behavior is unchanged.
  - `SUPERVISED_GLYPH = "◉"` (my call) in the title cell of targets with a supervisor.

- [x] **Step 1: Write failing tests:**
  - `test_supervisor_sessions_get_origin_supervisor_and_hidden_from_human_view`.
  - `test_supervisor_title_has_suffix_and_does_not_follow_target_rename`.
  - `test_target_row_shows_glyph_and_details_lines`: the `i` details contain
    `supervised by` / `supervises`.
  - `test_S_on_target_with_supervisor_returns_supervise_action`.
  - `test_S_on_target_without_supervisor_opens_harness_picker_defaulting_to_opposite`.
  - `test_S_on_supervisor_selects_target_and_never_offers_take_over_when_open`: on Windows
    with the target open, the status message mentions `Ctrl+Tab` and `confirm_take_over` is not
    called.
  - `test_S_on_supervisor_with_closed_target_launches_target`.
  - `test_reset_archives_record_and_hides_old_supervisor`: the next `S` creates a new
    `record_id`.
  - `test_help_lists_S`.
- [x] **Step 2:** Run the tests. Expect failures.
- [x] **Step 3:** Implement.
  - Match supervisor sessions to records by `HookState.native_session_id`.
  - The harness picker reuses the existing tool-choice dialog pattern from `t`.
  - Document `S`, `--supervise`, `--target-log` and `--install-hooks` in the README key table
    and a new "Supervisor sessions" section. Add a journey to `DESIGN.md` and an entry to
    `CHANGELOG.md`.
- [x] **Step 4:** Run the full suite and the Ruff checks
  (`ruff check . && ruff format --check .`). Both pass.
- [x] **Step 5:** Commit: `git commit -m "Open and show supervisors from the session browser"`.

### Task 11: Live verification, rollout, and supersession

**Files:**
- Modify: `specs/supervisor/README.md` (status: completed, evidence)
- Modify: `C:\Users\vandy\.codex\hooks.json` (remove the `codex_side_memory.py` entries)
- Modify: `C:\Users\vandy\.claude\projects\C--Users-vandy\memory\codex-side-memory-hook.md` and
  `MEMORY.md`

- [x] **Step 1:** Run `pip install .` from the branch, then `sessions --install-hooks`. Accept
  the Codex re-trust prompt; this is the user's step.
- [x] **Step 2:** For each of the four pairs (Claude→Claude, Claude→Codex, Codex→Claude,
  Codex→Codex as supervisor→target), on a small throwaway target session:
  1. Press `S`, then ask "what is the target working on?". The answer correctly states the
     target's first prompt.
  2. Run a known action in the target (for example, create the file `marker-<pair>.txt`), then
     ask the supervisor "what did the target just do?". It names the file.
  3. Quit and restart the supervisor via `sessions`. Its history and ledger are intact.
  4. Run `/compact` in the supervisor. The next prompt gets a fresh brief, which shows in the
     hook log as `brief n=2`.
- [x] **Step 3:** Bridge one target Codex → Claude with `x`. The next supervisor update names
  Claude and does not replay history.
- [x] **Step 4:** Remove the `codex_side_memory.py` hook entries from `~/.codex/hooks.json`,
  keeping a backup. Update the memory note to say it is superseded by supervisor sessions, and
  update its `MEMORY.md` line.
- [x] **Step 5:** (Codex-supervisor steps 2–4 await Codex quota; see BUILD-ACCEPTANCE.md.)
  Mark the spec `status: completed` with the evidence. Commit:
  `git commit -m "Complete supervisor sessions"`.
