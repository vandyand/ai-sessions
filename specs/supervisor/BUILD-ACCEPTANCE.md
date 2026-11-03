# Supervisor sessions: acceptance report

Date: 2026-10-31. Branch `supervisor-sessions` (local only, not pushed). Charter: `BUILD.md`.
Design: `README.md`. Plan: `plan.md`.

## Final state

- Final suite: `PYTHONPATH=src python -m unittest discover -s tests` gave **741 tests OK, 1
  skipped**, run on commit `0d943da`. `PYTHONPATH=src` is needed because the installed package
  shadows `src`.
- Lint: `uvx ruff check .` passes ("All checks passed!"), and `uvx ruff format --check .`
  reports "105 files already formatted". Ruff is not installed on this PC, so these ran through
  `uvx`.
- Commits:
  - `6013b3b`: Tasks 0–2.
  - `1daf3c0`: Tasks 3–5 and the installer library.
  - `8a014b3`: Task 9 and the install CLI.
  - `f5b757f`: Tasks 6–7.
  - `8711a4c`: Task 10, plus speed work for large targets.
  - `9a8775b`: fixes from the live runs.
  - `aba99b6`: fixes from the review.
  - `69cf96b`: Ruff.
  - `0d943da`: logging an import failure.

## Criteria

1. **Plan tasks 0–11 done, with their named tests.**
   - **Met.** Every box in `plan.md` is ticked.
   - The named tests exist. One plan test was renamed because the opening prompt was dropped
     (user decision): it is now `test_new_supervisor_argv_*_no_prompt`.
2. **Full suite and Ruff pass.** **Met**, as shown above.
3. **Each "Acceptance evidence" item in the spec maps to a test.** **Met.**

   | Spec item | Test(s) |
   |---|---|
   | Brief composition and caps, including the middle cut with a note | `test_first_prompt_gets_brief_with_all_sections_in_order`, `test_brief_tail_and_digest_partition_exchanges_without_overlap`, `test_still_over_cap_cuts_middle_with_note`, `test_over_cap_shortens_oldest_first_keeping_first_prompt_whole` |
   | Paste detection: Claude placeholders; Codex paired, unclosed and language-tagged fences; forgotten close | `test_claude_history_paste_spans`, `test_paired_fences`, `test_unclosed_final_fence_runs_to_end`, `test_language_tagged_fence_is_inner_block`, `test_forgotten_close_keeps_trailing_typed_words`, `test_marker_keeps_head_and_tail` |
   | Update after target appends | `test_update_contains_only_new_exchanges`, `test_claude_read_since_returns_only_appended_turns`, `test_codex_read_since_returns_only_appended_turns` |
   | Nothing-new line | `test_nothing_new_line` |
   | Long-gap rule | `test_long_gap_sends_brief_instead_of_update` |
   | Brief after the supervisor's own compaction | `test_session_start_compact_sets_brief_due`, `test_own_transcript_compaction_scan_sets_brief_due` |
   | Partial tail | `test_read_since_partial_tail_stops_before_incomplete_record` |
   | Bridged Codex → Claude mid-supervision, without replay | `test_head_moved_notes_new_harness_and_continues_from_member_checkpoint` |
   | Divergence | `test_diverged_injects_warning_and_no_activity` |
   | Resume outside `sessions` still gets updates (lookup by native id, no env var) | `test_user_prompt_submit_emits_additional_context_json` |
   | Non-supervisor session untouched and fast | `test_ordinary_session_outputs_nothing_and_skips_app_import`, `test_fast_entry_never_imports_the_package_for_an_ordinary_session` |
   | Idempotent install that preserves existing hooks | `test_install_is_idempotent`, `test_install_adds_entries_and_preserves_existing_hooks`, `test_reinstall_replaces_an_earlier_builds_command` |
   | Review Focus 1–5 | `test_unlocatable_checkpoint_rebriefs`, `test_unavailable_target_reports_and_keeps_checkpoint`, `test_reset_supervisor_gets_notice`, `test_hook_output_is_utf8_json_with_non_ascii`, `test_concurrent_record_writes_never_corrupt` |

4. **Live checks on the four pairs.**
   - **Met for every pair that could be driven. The rest are listed for Andrew below.**
   - **Method:**
     - Each check used isolated `APPDATA`, `LOCALAPPDATA` and state files.
     - Claude got its hooks through `--settings`.
     - Codex got its hooks through `-c` with `--dangerously-bypass-hook-trust`.
     - Throwaway sessions were created in the scratchpad's `live\work`, and no existing session
       was changed.

   | Pair (supervisor → target) | Result |
   |---|---|
   | Claude → Claude | **Passed all four checks.** The first answer names `marker-cc.txt`. After a second target action, the update names `second-cc.txt`. The resumed supervisor kept its history, and its ledger file was intact. After `/compact`, `brief_due` was set and the next prompt got brief n=2. A later check through the installed `ai_sessions_hook` entry delivered update `#3` (`third-cc.txt`). |
   | Claude → Codex | **Passed.** The target was a real Codex session, read only. Its 71,000-character brief was delivered as a file, and the supervisor described the target correctly (4 exchanges, matching the index). Bridged Codex → Claude via `prepare_launch`, the next update said "Target now runs in Claude Code" and contained only the new exchange (`last_seq_sent` 4 → 5). |
   | Codex → Claude | **Partly checked.** The hook bound the Codex native id on `SessionStart`, and the brief was recorded in the rollout. The model turn failed with "Quota exceeded", so the answer was not checked. |
   | Codex → Codex | **Not checked.** Codex has no model quota, so no target activity could be created. Codex reading is covered by the Claude → Codex pair. |

5. **Hooks installed into Andrew's real configs, with backups and existing hooks kept.** **Met.**
   - The branch wheel is installed into Python312.
   - `sessions --install-hooks` backed up both files to `.bak-20261031-160650`.
     `block_mass_kill.py` and the other hooks are still registered, and both files are valid
     JSON.
   - The `codex_side_memory.py` entries are removed. Their backup is
     `hooks.json.bak-*-side-memory`.
   - An ordinary `claude -p` session ran normally afterwards, and the hook logged nothing.
   - The memory notes are updated.

## Andrew's steps

- **Re-trust:** start Codex once and accept the re-trust prompt for the changed hooks.
- **Codex checks:** once Codex has quota, repeat the Codex-supervisor checks:
  1. Press `S` on a Claude target and pick Codex.
  2. Ask what the target did.
  3. Quit and resume.
  4. Run `/compact`. The hook log should show the next prompt getting a fresh brief.
- **Merge:** review and merge the branch when ready. It is not pushed. To roll back the
  install, run `sessions --uninstall-hooks`, then `pip install` from `main`.

## Known limitations

These are recorded in the `BUILD.md` scope ledger:
- A crash at the wrong moment can leave a duplicated exchange in the index.
- A tool result that arrives after the checkpoint is dropped.
- A rare empty-index head move repeats bridged history.
- For very large targets, the per-prompt cost is about 0.5 s.
- An interpreter path containing a space would break the Codex hook command.
- `--target-log --since` is accepted but ignored.
- The live checks found three defects, all fixed (`9a8775b`):
  - `AGENTS.md` was taken as the first prompt;
  - the supervisor could not read its own brief file;
  - the quoted hook command did not run under Codex.
- The review found two serious defects, both fixed (`aba99b6`):
  - the `-I` flag made the hook crash;
  - every ordinary prompt paid a 0.25 s import.

## Calls made without Andrew

These are in addition to the spec's list:
- **Delivery:** text over 9,000 characters goes as `brief-n.md` / `update-n.md`, based on the
  measured hook limits.
- **Folder access:** supervisors launch with `--add-dir=<their folder>`.
- **Reset:** `R` then `y`, from the `i` details.
- **New capabilities:** `pasted_contents`, `injected_prompt` and `extra_dir_args`.
- **Fast entry:** the hook's fast entry is a separate top-level module.
